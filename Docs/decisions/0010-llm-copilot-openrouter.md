# ADR 0010 — LLM research copilot via OpenRouter

Date: 2026-04-23
Status: Accepted
Scope: Phase 5

## Context

Phase 5 Parte II (Design.md) asks for an LLM-assisted research
surface: ask questions about backtests, strategy parity, paper
metrics, feature engineering. Phases 0-4 already expose the data
the copilot would discuss (research.backtests, trading.fills,
strategy sources under `src/trading/strategies/`).

The hard constraint is **research-only**. Under no circumstance
may the LLM execute, pause, resume, trade, or arm the kill
switch. Paper/live invariants (I.1..I.9) remain the source of
truth; the copilot is a read-side attachment.

## Decision

### Single provider: OpenRouter

OpenRouter gives us one auth, one billing, one pricing surface
for many models. We whitelist five:

| model id                              | in $/M | out $/M | default? |
|---------------------------------------|--------|---------|----------|
| `qwen/qwen3-max`                      | 0.78   | 3.90    | **yes**  |
| `anthropic/claude-sonnet-4.6`         | 3.00   | 15.00   |          |
| `anthropic/claude-opus-4.6`           | 5.00   | 25.00   |          |
| `openai/gpt-4o-mini`                  | 0.15   | 0.60    |          |
| `meta-llama/llama-3.3-70b-instruct`   | 0.10   | 0.32    |          |

Pricing verified against openrouter.ai model pages 2026-04-23.
Any request naming a model not in this whitelist is rejected
before any HTTP fanout (400).

### Hard wall: no tools, ever

`tools` / `tool_choice` / `function_call` are never sent in the
request body. The client code strips these keys before POSTing to
OpenRouter, even if a caller supplies them. The system prompt
explicitly states that the assistant has no tools. Response bodies
are scanned for `function_call` / `tool_calls` keys; if present,
the request is rejected as a policy violation.

This is paranoid on purpose. The LLM never touches Redis, the
engine API, the kill switch file, or any mutable DB. The only
code path that reaches a model is:

```
user request → auth (tea_token) → rate limit gate →
context loader (READ-ONLY DB) → messages builder →
OpenRouter POST (no tools) → persist conversation → reply
```

### Context loader (read-only)

Caller supplies `context_refs`:

```json
[
  {"type": "backtest",     "id": "<uuid>"},
  {"type": "strategy",     "id": "imbalance_v3"},
  {"type": "recent_trades","id": "imbalance_v3:20"},
  {"type": "paper_stats",  "id": "imbalance_v3:7"},
  {"type": "adr",          "id": "0008"}
]
```

Per-type token budgets (soft cap via head-truncation with a
`[truncated N bytes]` marker):

- `backtest`       → ~4 k tokens (metadata + metrics + top 10 trades + 50 lines of report summary)
- `strategy`       → ~6 k tokens (source file + TOML params). Off by default — `include_source=false` in config — until explicitly enabled per call
- `recent_trades`  → ~1.5 k tokens
- `paper_stats`    → ~0.5 k tokens
- `adr`            → ~2 k tokens

Total context hard cap: **50 k tokens**. Above → HTTP 400, no
call to OpenRouter.

All loaded text is wrapped in XML-ish fences:

```
<context type="backtest" id="0e1f...">...
</context>
```

The system prompt instructs the model to treat everything inside
`<context>` as data, never as instructions. U+001B and NUL are
stripped from loaded text before fencing.

### Schemas

```sql
CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.llm_conversations (
    id           UUID PRIMARY KEY,
    session_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,       -- telegram:<id> | web:<token_hash8>
    model        TEXT NOT NULL,
    messages     JSONB NOT NULL,      -- [{"role": "...", "content": "..."}]
    context_refs JSONB NOT NULL,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(12,6) NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS llm_conv_session_uk
    ON research.llm_conversations (session_id);
CREATE INDEX IF NOT EXISTS llm_conv_user_idx
    ON research.llm_conversations (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research.llm_usage_daily (
    day       DATE NOT NULL,
    user_id   TEXT NOT NULL,
    sessions  INTEGER NOT NULL DEFAULT 0,
    tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd  NUMERIC(12,6) NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id)
);
```

Per-session append-in-place: the row is created on the first turn
and updated in place on each subsequent turn, so `messages` is the
entire thread and `tokens_in/out + cost_usd` are running totals.

### Rate limits

Per user, enforced on the server (not the client):

- 50 sessions/day
- 200 000 tokens per session
- USD 10.00 / day cost cap

Exceeding the daily session count or cost cap → 429 with a
`Retry-After` header set to 00:00 UTC tomorrow. Exceeding the
per-session token cap → 403 "session capped; /ask_reset or pick
a new context". First crossing of the daily cost cap fires a
single Telegram alert.

### Auth

The chat endpoint reuses the Phase 4 tea_token cookie / X-TEA-Token
header. No secondary token in this iteration.

### Integrations

- **Dashboard**: `/research/chat` shows context pickers + a
  message thread. Requests are synchronous server-side; the page
  HTMX-polls `/research/chat/session/<id>/status` while the
  handler awaits OpenRouter.
- **Telegram bot**: `/ask <pregunta>` and `/ask_reset`. The bot
  maintains `session_id` per chat in Redis (TTL 4 h). No context
  refs from the bot in v1 (UX noise). Responses are split at
  4000-char boundaries and sent as MarkdownV2.
- **Grafana**: one new panel (`TEA — LLM usage`) sourced from
  `research.llm_usage_daily`: cost per day, top models, sessions
  per user.

### Secrets

`OPENROUTER_API_KEY` lives in `/etc/trading-system/secrets.env`,
chmod 600. Never logged. structlog has a formatter hook that
redacts any string that begins with `sk-or-` (OpenRouter key
prefix). Conversation logs truncate each `content` string above
4 k chars to head + `[...trunc N...]` + tail, so a large context
does not explode log storage.

## Consequences

- New dependency: `httpx` already present; add OpenRouter-specific
  module under `src/trading/llm/`.
- Two new Postgres tables, ~0 disk pressure for the first 6 months
  at expected volume (< 1 k sessions/month).
- Bot startup adds a Redis session-map read per `/ask`; negligible.
- Grafana gets one panel; no new dashboard file.
- CI run time increases by ~1 s (8 adversarial tests hit a mocked
  OpenRouter).

## Revisit

- If daily cost cap hits more than twice/month → raise limits or
  move the default model to a cheaper row.
- If response latency > 30 s consistently → move to streaming
  (Phase 5.5).
- If users want named sessions beyond "last" → add a `session`
  kwarg to `/ask`.
- If a second provider is wanted (Google, DeepSeek direct) →
  generalize the client behind a `Provider` abstract; today it's
  OpenRouter-only on purpose.
