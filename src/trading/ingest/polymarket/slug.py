from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Pattern: btc-updown-5m-{close_ts_epoch_seconds}
# Rounded to 5-minute (300s) boundaries. Ported from polybot-btc5m/core/market.py.
SLUG_PREFIX = "btc-updown-5m-"
WINDOW_SECONDS = 300


@dataclass(frozen=True)
class Window:
    open_ts: int  # epoch seconds
    close_ts: int  # epoch seconds
    slug: str


def window_for(ts: datetime) -> Window:
    epoch = int(ts.astimezone(UTC).timestamp())
    open_ts = epoch - (epoch % WINDOW_SECONDS)
    close_ts = open_ts + WINDOW_SECONDS
    return Window(open_ts=open_ts, close_ts=close_ts, slug=f"{SLUG_PREFIX}{close_ts}")


def windows_between(since: datetime, until: datetime) -> list[Window]:
    w = window_for(since)
    out: list[Window] = []
    cursor_ts = w.close_ts
    until_epoch = int(until.astimezone(UTC).timestamp())
    while cursor_ts <= until_epoch:
        out.append(
            Window(
                open_ts=cursor_ts - WINDOW_SECONDS,
                close_ts=cursor_ts,
                slug=f"{SLUG_PREFIX}{cursor_ts}",
            )
        )
        cursor_ts += WINDOW_SECONDS
    return out


def current_window(now: datetime | None = None) -> Window:
    return window_for(now or datetime.now(tz=UTC))


def next_window(from_: datetime | None = None) -> Window:
    base = from_ or datetime.now(tz=UTC)
    future = base + timedelta(seconds=WINDOW_SECONDS)
    return window_for(future)
