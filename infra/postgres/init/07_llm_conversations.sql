-- Phase 5: LLM copilot (ADR 0010). Read-only research surface.

CREATE TABLE IF NOT EXISTS research.llm_conversations (
    id           UUID PRIMARY KEY,
    session_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    model        TEXT NOT NULL,
    messages     JSONB NOT NULL,
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
