"""Telegram alerts — thin wrapper over Bot API with rate limit + dedupe.

Reads TEA_TELEGRAM_BOT_TOKEN and TEA_TELEGRAM_CHAT_ID from env
(forwarded by docker-compose env_file). If either is empty, alerts
silently log-only so local dev runs don't need a bot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from time import time

import httpx
from aiolimiter import AsyncLimiter

from trading.common.logging import get_logger

log = get_logger(__name__)

DEDUPE_SECONDS = 60
CIRCUIT_BREAKER_WINDOW_S = 60
CIRCUIT_BREAKER_MAX = 20
CIRCUIT_BREAKER_COOLDOWN_S = 15 * 60


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRIT = "CRIT"


@dataclass
class AlertEvent:
    kind: str
    text: str
    severity: Severity = Severity.INFO


class TelegramClient:
    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = token or os.environ.get("TEA_TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TEA_TELEGRAM_CHAT_ID", "")
        self._client = httpx.AsyncClient(timeout=10.0)
        self._rate = AsyncLimiter(max_rate=1, time_period=1.0)
        self._last_seen: dict[str, float] = {}  # kind -> ts of last send
        self._sent_window: list[float] = []
        self._suppressed_until: float = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _circuit_open(self) -> bool:
        now = time()
        if now < self._suppressed_until:
            return True
        cutoff = now - CIRCUIT_BREAKER_WINDOW_S
        self._sent_window = [t for t in self._sent_window if t > cutoff]
        if len(self._sent_window) >= CIRCUIT_BREAKER_MAX:
            self._suppressed_until = now + CIRCUIT_BREAKER_COOLDOWN_S
            log.warning("telegram.circuit_breaker.tripped", cooldown_s=CIRCUIT_BREAKER_COOLDOWN_S)
            return True
        return False

    def _dedupe(self, kind: str) -> bool:
        now = time()
        last = self._last_seen.get(kind, 0.0)
        if now - last < DEDUPE_SECONDS:
            return True
        self._last_seen[kind] = now
        return False

    async def send(self, event: AlertEvent) -> None:
        if not self.enabled:
            log.info("telegram.disabled.logged_only", kind=event.kind, text=event.text)
            return
        if self._dedupe(event.kind):
            log.debug("telegram.dedupe.drop", kind=event.kind)
            return
        if self._circuit_open():
            log.warning("telegram.suppressed", kind=event.kind)
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = {
            "chat_id": self.chat_id,
            "text": f"[{event.severity.value}] {event.text}",
            "disable_web_page_preview": True,
        }
        async with self._rate:
            try:
                r = await self._client.post(url, json=body)
                if r.status_code == 200:
                    self._sent_window.append(time())
                else:
                    log.warning(
                        "telegram.send_non_200",
                        status=r.status_code,
                        body=r.text[:200],
                    )
            except Exception as e:
                log.warning("telegram.send_err", err=str(e))

    async def aclose(self) -> None:
        await self._client.aclose()


# Convenience builders for the alert kinds specified in the Phase 3 plan.


def trade_open(slug: str, side: str, price: float, stake_usd: float) -> AlertEvent:
    return AlertEvent(
        kind="TRADE_OPEN",
        text=f"📈 entry {side} {slug} @{price:.4f} stake=${stake_usd:.0f}",
    )


def trade_close(resolution: str, pnl: float, slug: str) -> AlertEvent:
    symbol = "✅ win" if resolution == "win" else "❌ loss"
    return AlertEvent(
        kind="TRADE_CLOSE",
        text=f"{symbol} {pnl:+.2f} USD ({slug})",
    )


def loss_threshold(daily_pnl: float, pct: float) -> AlertEvent:
    return AlertEvent(
        kind="LOSS_THRESHOLD",
        text=f"🟡 daily loss ${daily_pnl:+.2f} ({pct*100:+.2f}%)",
        severity=Severity.WARN,
    )


def engine_stopped(task: str, reason: str) -> AlertEvent:
    return AlertEvent(
        kind="ENGINE_STOPPED",
        text=f"🔴 engine stopped, task={task} reason={reason}",
        severity=Severity.CRIT,
    )


def heartbeat_lost(age_s: int) -> AlertEvent:
    return AlertEvent(
        kind="HEARTBEAT_LOST",
        text=f"🔴 heartbeat lost, last seen {age_s}s ago",
        severity=Severity.CRIT,
    )


def heartbeat_recovered() -> AlertEvent:
    return AlertEvent(
        kind="HEARTBEAT_RECOVERED",
        text="🟢 heartbeat recovered",
    )


def kill_switch_on(at_iso: str) -> AlertEvent:
    return AlertEvent(
        kind="KILL_SWITCH_ON",
        text=f"⛔ KILL_SWITCH active at {at_iso}",
        severity=Severity.CRIT,
    )


def kill_switch_off(at_iso: str) -> AlertEvent:
    return AlertEvent(
        kind="KILL_SWITCH_OFF",
        text=f"✅ KILL_SWITCH removed at {at_iso}",
        severity=Severity.WARN,
    )


def reconciliation_fail(detail: str) -> AlertEvent:
    return AlertEvent(
        kind="RECONCILIATION_FAIL",
        text=f"🔴 reconciliation failed — {detail}",
        severity=Severity.CRIT,
    )
