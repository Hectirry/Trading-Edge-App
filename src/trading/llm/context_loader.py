"""Read-only context loader for the LLM copilot (ADR 0010).

Given a list of ``ContextRef``, produces a text payload that the message
builder wraps in XML-ish ``<context type=".." id="..">`` fences. Every
query is parametrized. No writes. Strategy source inclusion is gated by
``llm_include_source`` and defaults to False.

Per-type budgets (bytes, ~4 chars/token heuristic):
- backtest       → 16 000 bytes  (~4 k tok)
- strategy       → 24 000 bytes  (~6 k tok)
- recent_trades  →  6 000 bytes  (~1.5 k tok)
- paper_stats    →  2 000 bytes  (~0.5 k tok)
- adr            →  8 000 bytes  (~2 k tok)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from trading.common.config import get_settings
from trading.common.db import acquire
from trading.common.logging import get_logger

log = get_logger(__name__)

# Chars per token is a rough heuristic; the hard cap in the endpoint uses
# the model's token count from OpenRouter. This file enforces byte caps to
# keep a single ref from dominating the context window.
BYTE_BUDGET: dict[str, int] = {
    "backtest": 16_000,
    "strategy": 24_000,
    "recent_trades": 6_000,
    "paper_stats": 2_000,
    "adr": 8_000,
}

ALLOWED_TYPES: tuple[str, ...] = tuple(BYTE_BUDGET.keys())

# Defence-in-depth strip: control chars that could be used to inject
# escape sequences into logs or misbehaving terminals. Leaves \n and \t.
_STRIP_CHARS = {chr(c) for c in range(0, 32) if c not in (9, 10, 13)} | {"\x7f"}


@dataclass(frozen=True)
class ContextRef:
    type: str
    id: str

    @classmethod
    def parse(cls, obj: dict) -> ContextRef:
        t = (obj.get("type") or "").strip().lower()
        i = (obj.get("id") or "").strip()
        if t not in ALLOWED_TYPES:
            raise ValueError(f"context type not allowed: {t!r}")
        if not i:
            raise ValueError("context id empty")
        return cls(type=t, id=i)


@dataclass
class LoadedContext:
    ref: ContextRef
    body: str
    truncated_bytes: int = 0

    def render(self) -> str:
        attr = f'type="{self.ref.type}" id="{_esc_attr(self.ref.id)}"'
        suffix = ""
        if self.truncated_bytes:
            suffix = f"\n[truncated {self.truncated_bytes} bytes]"
        return f"<context {attr}>\n{self.body}{suffix}\n</context>"


def _esc_attr(v: str) -> str:
    return v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _sanitize(s: str) -> str:
    return "".join(ch for ch in s if ch not in _STRIP_CHARS)


def _budget_trim(body: str, budget: int) -> tuple[str, int]:
    if len(body) <= budget:
        return body, 0
    head = body[:budget]
    return head, len(body) - budget


async def load_contexts(refs: list[ContextRef]) -> list[LoadedContext]:
    out: list[LoadedContext] = []
    for ref in refs:
        try:
            loaded = await _load_one(ref)
        except Exception as e:
            log.warning("llm.context.load_fail", type=ref.type, id=ref.id, err=str(e))
            loaded = LoadedContext(ref=ref, body=f"[load failed: {e}]")
        out.append(loaded)
    return out


async def _load_one(ref: ContextRef) -> LoadedContext:
    if ref.type == "backtest":
        return await _load_backtest(ref)
    if ref.type == "strategy":
        return await _load_strategy(ref)
    if ref.type == "recent_trades":
        return await _load_recent_trades(ref)
    if ref.type == "paper_stats":
        return await _load_paper_stats(ref)
    if ref.type == "adr":
        return _load_adr(ref)
    raise ValueError(f"unknown context type {ref.type}")


async def _load_backtest(ref: ContextRef) -> LoadedContext:
    try:
        bt_uuid = uuid.UUID(ref.id)
    except Exception as e:
        raise ValueError(f"backtest id is not a uuid: {ref.id}") from e
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, strategy_name, started_at, ended_at, status, "
            "dataset_from, dataset_to, params_hash, metrics, report_path "
            "FROM research.backtests WHERE id = $1",
            bt_uuid,
        )
        if row is None:
            return LoadedContext(ref=ref, body="[not found]")
        trades = await conn.fetch(
            "SELECT trade_idx, instrument, strategy_side, entry_ts, entry_price, "
            "exit_ts, exit_price, pnl, fees, edge_bps "
            "FROM research.backtest_trades WHERE backtest_id = $1 "
            "ORDER BY trade_idx ASC LIMIT 10",
            bt_uuid,
        )
    payload = {
        "id": str(row["id"]),
        "strategy_name": row["strategy_name"],
        "started_at": str(row["started_at"]),
        "ended_at": str(row["ended_at"]) if row["ended_at"] else None,
        "status": row["status"],
        "dataset_from": str(row["dataset_from"]),
        "dataset_to": str(row["dataset_to"]),
        "params_hash": row["params_hash"],
        "metrics": row["metrics"],
        "top_trades": [
            dict(r)
            | {k: str(v) for k, v in dict(r).items() if hasattr(v, "isoformat")}
            for r in trades
        ],
    }
    # Stringify datetimes and Decimals from asyncpg.
    body = json.dumps(payload, default=str, ensure_ascii=False, indent=2)

    # Optional report summary (first N lines) if the path is reachable.
    report_path = row["report_path"]
    if report_path:
        rp = Path(report_path)
        if rp.is_file():
            try:
                lines = rp.read_text(errors="replace").splitlines()[:50]
                body += "\n\n[report summary — first 50 lines]\n" + "\n".join(lines)
            except Exception as e:
                body += f"\n\n[report unreachable: {e}]"
    body, trunc = _budget_trim(_sanitize(body), BYTE_BUDGET["backtest"])
    return LoadedContext(ref=ref, body=body, truncated_bytes=trunc)


async def _load_strategy(ref: ContextRef) -> LoadedContext:
    """Load metadata-only by default; source only if the setting allows it."""
    include_source = get_settings().llm_include_source
    toml_path = Path("config/strategies") / f"pbt5m_{ref.id}.toml"
    src_path = Path("src/trading/strategies") / f"{ref.id}.py"

    parts: list[str] = []
    parts.append(f"[strategy id] {ref.id}")
    if toml_path.is_file():
        parts.append(f"[params toml] {toml_path}\n{toml_path.read_text(errors='replace')}")
    else:
        parts.append(f"[params toml missing at {toml_path}]")
    if include_source:
        if src_path.is_file():
            parts.append(f"[source] {src_path}\n{src_path.read_text(errors='replace')}")
        else:
            parts.append(f"[source missing at {src_path}]")
    else:
        parts.append(
            "[source omitted — llm_include_source=false. Only params + metadata"
            " are shared with the provider.]"
        )
    body, trunc = _budget_trim(_sanitize("\n\n".join(parts)), BYTE_BUDGET["strategy"])
    return LoadedContext(ref=ref, body=body, truncated_bytes=trunc)


async def _load_recent_trades(ref: ContextRef) -> LoadedContext:
    """id format: ``<strategy_id>:<N>``, N capped at 50."""
    strategy_id, _, n_str = ref.id.partition(":")
    try:
        n = max(1, min(50, int(n_str))) if n_str else 20
    except ValueError:
        n = 20
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT o.ts_submit, o.instrument_id, o.price AS entry_price, "
            "(f.metadata::jsonb->>'pnl')::numeric AS pnl, "
            "(f.metadata::jsonb->>'resolution') AS resolution "
            "FROM trading.orders o "
            "LEFT JOIN trading.fills f "
            "  ON f.order_id = o.order_id "
            "  AND f.metadata::jsonb->>'kind' = 'settle' "
            "WHERE o.mode = 'paper' AND o.strategy_id = $1 "
            "ORDER BY o.ts_submit DESC LIMIT $2",
            strategy_id,
            n,
        )
    payload = [
        {k: (str(v) if v is not None else None) for k, v in dict(r).items()} for r in rows
    ]
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    body, trunc = _budget_trim(_sanitize(body), BYTE_BUDGET["recent_trades"])
    return LoadedContext(ref=ref, body=body, truncated_bytes=trunc)


async def _load_paper_stats(ref: ContextRef) -> LoadedContext:
    """id format: ``<strategy_id>:<days>`` (days default 7, capped 60)."""
    strategy_id, _, d_str = ref.id.partition(":")
    try:
        days = max(1, min(60, int(d_str))) if d_str else 7
    except ValueError:
        days = 7
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE f.metadata::jsonb->>'kind' = 'settle') AS n_trades, "
            "COALESCE(SUM((f.metadata::jsonb->>'pnl')::numeric) "
            "  FILTER (WHERE f.metadata::jsonb->>'kind' = 'settle'), 0) AS total_pnl, "
            "COALESCE(AVG((f.metadata::jsonb->>'pnl')::numeric) "
            "  FILTER (WHERE f.metadata::jsonb->>'kind' = 'settle' "
            "          AND (f.metadata::jsonb->>'pnl')::numeric > 0), 0) AS avg_win, "
            "COALESCE(AVG((f.metadata::jsonb->>'pnl')::numeric) "
            "  FILTER (WHERE f.metadata::jsonb->>'kind' = 'settle' "
            "          AND (f.metadata::jsonb->>'pnl')::numeric < 0), 0) AS avg_loss "
            "FROM trading.fills f JOIN trading.orders o ON o.order_id = f.order_id "
            "WHERE f.mode = 'paper' AND o.strategy_id = $1 "
            "AND f.ts > now() - ($2::int * interval '1 day')",
            strategy_id,
            days,
        )
    payload = {
        "strategy_id": strategy_id,
        "window_days": days,
        "n_trades": int(row["n_trades"] or 0),
        "total_pnl": float(row["total_pnl"] or 0.0),
        "avg_win": float(row["avg_win"] or 0.0),
        "avg_loss": float(row["avg_loss"] or 0.0),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    body, trunc = _budget_trim(_sanitize(body), BYTE_BUDGET["paper_stats"])
    return LoadedContext(ref=ref, body=body, truncated_bytes=trunc)


def _load_adr(ref: ContextRef) -> LoadedContext:
    n = ref.id.zfill(4)
    matches = sorted(Path("Docs/decisions").glob(f"{n}-*.md"))
    if not matches:
        return LoadedContext(ref=ref, body="[not found]")
    body = matches[0].read_text(errors="replace")
    body, trunc = _budget_trim(_sanitize(body), BYTE_BUDGET["adr"])
    return LoadedContext(ref=ref, body=body, truncated_bytes=trunc)
