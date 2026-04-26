#!/usr/bin/env python3
"""Live decision viewer for ``bb_residual_ofi_v1``.

Tails the ``tea-engine`` container and pretty-prints every
``bb_ofi.decision`` log line as one row in a colour-coded table —
designed to live in a terminal next to a Polymarket browser tab so
you can see, in real time:

- which slug the strategy is looking at,
- where in the 5 m window it is (``t``),
- the no-drift Brownian-bridge prior on Binance spot (``p_bm``),
- the composite OFI from Binance trades (``ofi``),
- the model edge (``p_edge``) and shrinkage-blended ``p_final``,
- the convex-fee-net edge (``edge_net``) and per-trade Sharpe,
- and which gate fired (or that the strategy ENTERED).

Usage
-----
    ./scripts/watch_bb_ofi.py                # tail live
    ./scripts/watch_bb_ofi.py --since 5m     # last 5 min then live
    ./scripts/watch_bb_ofi.py --reasons sharpe_below_threshold,shadow_mode
    ./scripts/watch_bb_ofi.py --action ENTER # only entries
    ./scripts/watch_bb_ofi.py --raw          # raw JSON, no formatting

The tail uses ``docker logs -f`` under the hood; if the container
restarts you'll see the new run immediately.

This is a runtime debugging surface — not committed test infrastructure.
Keep flag/column changes additive so existing terminals don't break.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any

# ANSI colour helpers — keep monochrome fallback by setting ``COLOR=False``
# below if your terminal doesn't render escapes.
COLOR = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    if not COLOR:
        return s
    return f"\033[{code}m{s}\033[0m"


DIM = "90"
GREY = "37"
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
BOLD = "1"

# How to colour each (action, reason) pair. ENTER is the only
# action with a value-on-the-wire today (everything else is SKIP),
# but we keep the structure ready for the eventual real entries.
REASON_PALETTE: dict[str, str] = {
    "shadow_mode_no_model": DIM,
    "shadow_mode": YELLOW,
    "edge_net_below_floor": GREY,
    "sharpe_below_threshold": GREY,
    "model_predict_err": RED,
    "spread_too_wide": YELLOW,
    "outside_entry_window": DIM,
    "insufficient_micro_data": DIM,
    "insufficient_returns": DIM,
    "ofi_coinbase_weight_must_be_zero_until_ingest_lands": RED,
    "already_entered_this_window": MAGENTA,
}


def _fmt_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _fmt_edge(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x * 100:5.2f}pp"


def _fmt_row(ev: dict[str, Any]) -> str:
    """One column-aligned row per decision."""
    action = str(ev.get("action", "?"))
    reason = str(ev.get("reason", ""))
    side = str(ev.get("side", "NONE"))
    slug = str(ev.get("slug", "?"))
    # Show only the trailing 6 chars of the slug — most window slugs
    # are ``btc-updown-5m-<unix>``; the tail uniquely identifies it
    # without taking the whole row.
    slug_tail = slug.rsplit("-", 1)[-1][-6:] if "-" in slug else slug[-6:]

    t = float(ev.get("t_in_window", 0.0))
    p_market = float(ev.get("p_market", 0.0))
    p_bm = float(ev.get("p_bm", 0.0))
    p_edge = float(ev.get("p_edge", 0.0))
    p_final = float(ev.get("p_final", 0.0))
    edge_net = float(ev.get("edge_net", 0.0))
    sharpe = float(ev.get("sharpe", 0.0))
    sharpe_th = float(ev.get("sharpe_th", 0.0))
    ofi = float(ev.get("ofi", 0.0))
    intensity = float(ev.get("intensity", 1.0))
    large = int(ev.get("large_trade", 0))
    alpha = float(ev.get("alpha", 0.0))
    spread = float(ev.get("spread_bps", 0.0))

    ts = str(ev.get("timestamp", ""))[11:19]  # HH:MM:SS
    color = REASON_PALETTE.get(reason, GREY)
    if action == "ENTER":
        color = GREEN
        action_cell = _c(BOLD + ";" + GREEN, "ENTER")
    else:
        action_cell = _c(color, "SKIP ")

    side_cell = _c(BLUE if side == "YES_UP" else MAGENTA, f"{side:8}")
    sharpe_cell = (
        _c(GREEN, f"{sharpe:5.2f}") if sharpe >= sharpe_th else _c(GREY, f"{sharpe:5.2f}")
    )
    edge_cell = (
        _c(GREEN, _fmt_edge(edge_net))
        if edge_net > 0
        else _c(RED, _fmt_edge(edge_net))
    )
    large_cell = _c(YELLOW, "L") if large else " "

    return (
        f"{_c(DIM, ts)} "
        f"{_c(BLUE, slug_tail):>6}  "
        f"t={int(t):3d}s  "
        f"p_mkt={_fmt_pct(p_market)} "
        f"p_bm={_fmt_pct(p_bm)} "
        f"p_e={_fmt_pct(p_edge)} "
        f"p_f={_fmt_pct(p_final)}  "
        f"ofi={ofi:+5.2f} "
        f"int={intensity:4.2f} "
        f"{large_cell}  "
        f"α={alpha:.2f}  "
        f"edge={edge_cell}  "
        f"sh={sharpe_cell}/{sharpe_th:.1f}  "
        f"sp={spread:5.1f}bp  "
        f"{action_cell} {side_cell} "
        f"{_c(color, reason)}"
    )


def _print_header() -> None:
    width = shutil.get_terminal_size((120, 40)).columns
    title = " bb_residual_ofi_v1 — live decisions "
    bar = "─" * max(0, (width - len(title)) // 2)
    print(_c(DIM, f"{bar}{title}{bar}"))
    print(
        _c(
            DIM,
            "time     slug    t       p_mkt  p_bm   p_e    p_f    ofi   int  L  α     "
            "edge       sh           sp        action  side     reason",
        )
    )
    print(_c(DIM, "─" * width))


def _stream_lines(args: argparse.Namespace):
    cmd = ["docker", "logs", "-f"]
    if args.since:
        cmd += ["--since", args.since]
    if not args.no_tail0 and not args.since:
        cmd += ["--tail", "0"]
    cmd.append(args.container)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            yield raw.decode("utf-8", errors="replace").rstrip("\n")
    finally:
        proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--container", default="tea-engine")
    ap.add_argument(
        "--since",
        default=None,
        help='docker --since arg (e.g. "5m", "1h"). Default: tail live only.',
    )
    ap.add_argument(
        "--reasons",
        default=None,
        help="comma-separated allow-list of reasons; others are dropped.",
    )
    ap.add_argument(
        "--action",
        default=None,
        choices=["ENTER", "SKIP"],
        help="filter by action.",
    )
    ap.add_argument("--raw", action="store_true", help="print raw JSON, no formatting.")
    ap.add_argument(
        "--no-tail0",
        action="store_true",
        help="show full container log buffer on start (default: only new lines).",
    )
    args = ap.parse_args()

    reasons_filter = (
        {r.strip() for r in args.reasons.split(",") if r.strip()} if args.reasons else None
    )

    if not args.raw:
        _print_header()

    try:
        for line in _stream_lines(args):
            try:
                ev = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if ev.get("event") != "bb_ofi.decision":
                continue
            if args.action and ev.get("action") != args.action:
                continue
            if reasons_filter and ev.get("reason") not in reasons_filter:
                continue
            if args.raw:
                print(json.dumps(ev, separators=(",", ":")))
            else:
                print(_fmt_row(ev))
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
