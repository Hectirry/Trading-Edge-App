#!/usr/bin/env python3
"""Human-friendly live dashboard for ``bb_residual_ofi_v1``.

Designed to live in a terminal next to your Polymarket browser tab.
Shows, for every 5 m window the strategy is currently looking at:

- the BTC spot price (open vs. now),
- what the **market** thinks (SÍ / NO probabilities on Polymarket),
- what **the model** thinks (after Brownian-bridge prior +
  microstructure ensemble shrinkage),
- which way it predicts,
- its **edge net of the convex fee**,
- its **confidence** (per-trade Sharpe vs. the required threshold),
- a one-line plain-Spanish recommendation: COMPRAR / NO COMPRAR
  with the human reason for the decision.

No dependencies — just ANSI escapes. Run:

    ./scripts/dashboard_bb_ofi.py

Press Ctrl+C to quit.

Implementation notes
--------------------
- Tails ``docker logs -f tea-engine`` for ``event=bb_ofi.decision``
  lines and keeps the latest snapshot per ``market_slug`` in memory.
- Markets idle for > ``MARKET_TIMEOUT_S`` seconds drop off the panel
  (handles window rollover when the slug changes).
- Layout is deliberately fixed-width so it doesn't reflow as numbers
  change. Tested at 100 cols; resize-tolerant down to ~80.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

# ---------- ANSI helpers ---------- #

COLOR = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    if not COLOR:
        return s
    return f"\033[{code}m{s}\033[0m"


RESET = "0"
BOLD = "1"
DIM = "2"
GREY = "37"
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
CYAN = "36"
BG_GREEN = "42;30"
BG_RED = "41;37"
BG_YELLOW = "43;30"

CLEAR_SCREEN = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CLEAR_LINE = "\033[K"


# ---------- reason → human Spanish + recommendation ---------- #

REASON_HUMAN: dict[str, tuple[str, str]] = {
    # (recomendación, motivo en español)
    "shadow_mode_no_model": (
        "ESPERANDO",
        "El modelo aún no está entrenado — solo se recolectan datos en este "
        "momento.",
    ),
    "shadow_mode": (
        "NO COMPRAR",
        "Modo shadow activo: el modelo SÍ daría señal pero los trades reales "
        "están deshabilitados.",
    ),
    "edge_net_below_floor": (
        "NO COMPRAR",
        "La comisión del mercado se come la ventaja del modelo.",
    ),
    "sharpe_below_threshold": (
        "NO COMPRAR",
        "Hay ventaja pero la confianza (Sharpe) no llega al mínimo "
        "requerido.",
    ),
    "model_predict_err": (
        "ERROR",
        "El modelo falló al hacer la predicción — revisar logs.",
    ),
    "spread_too_wide": (
        "NO COMPRAR",
        "El spread de Polymarket está demasiado ancho — cualquier orden "
        "pagaría slippage excesivo.",
    ),
    "insufficient_micro_data": (
        "ESPERANDO",
        "Datos insuficientes — esperando más ticks de spot Binance.",
    ),
    "insufficient_returns": (
        "ESPERANDO",
        "Returns insuficientes para estimar la volatilidad de la ventana.",
    ),
    "already_entered_this_window": (
        "EN POSICIÓN",
        "Ya entramos en esta ventana — solo permitimos un trade por ventana.",
    ),
    "ofi_coinbase_weight_must_be_zero_until_ingest_lands": (
        "CONFIG INVÁLIDA",
        "El peso de Coinbase OFI debe ser 0 hasta que llegue la ingesta.",
    ),
}


def _recommendation(action: str, reason: str, side: str) -> tuple[str, str, str]:
    """Returns (label, color_code, motivo) for the recommendation banner."""
    if action == "ENTER":
        dir_word = "subir" if side == "YES_UP" else "bajar"
        return ("✅ COMPRAR", BG_GREEN, f"Modelo predice que va a {dir_word} con confianza suficiente.")
    label, motivo = REASON_HUMAN.get(reason, ("NO COMPRAR", reason))
    if label == "ESPERANDO":
        color = BG_YELLOW
    elif label == "ERROR" or label == "CONFIG INVÁLIDA":
        color = BG_RED
    else:
        color = DIM
    return (f"⏸ {label}", color, motivo)


# ---------- multi-exchange spot price feed ---------- #


class ExchangePriceFeed:
    """Background poller for public BTC/USD spot endpoints.

    Each source is a (url, json_path) pair where ``json_path`` is the
    sequence of dict keys / list indices to walk to reach the price
    string. All endpoints below are public and unauthenticated;
    polling at ~3 s is well under every rate limit.

    The feed runs in a daemon thread so Ctrl+C in the main loop kills
    it cleanly. Failures (timeouts, JSON drift) silently drop the row
    until the next successful poll — no exceptions propagate to the
    UI thread.
    """

    SOURCES: dict[str, tuple[str, tuple]] = {
        "Binance": (
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            ("price",),
        ),
        "Coinbase": (
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            ("data", "amount"),
        ),
        "Kraken": (
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
            ("result", "XXBTZUSD", "c", 0),
        ),
        "Bitstamp": (
            "https://www.bitstamp.net/api/v2/ticker/btcusd/",
            ("last",),
        ),
        "OKX": (
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
            ("data", 0, "last"),
        ),
    }

    def __init__(self, interval_s: float = 3.0, enabled: list[str] | None = None) -> None:
        self.interval_s = interval_s
        self.prices: dict[str, tuple[float, float]] = {}  # name -> (price, ts_unix)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="exch-feed")
        if enabled is None:
            self._sources = dict(self.SOURCES)
        else:
            wanted = {name.lower() for name in enabled}
            self._sources = {
                k: v for k, v in self.SOURCES.items() if k.lower() in wanted
            }

    def start(self) -> None:
        if self._sources:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _walk(data: Any, path: tuple) -> Any:
        for key in path:
            data = data[key]
        return data

    def _fetch(self, url: str, path: tuple) -> float | None:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "tea-dashboard/1.0"}
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                payload = json.loads(resp.read())
            return float(self._walk(payload, path))
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            return None

    def _run(self) -> None:
        # Initial pass populates prices fast so the panel doesn't sit
        # blank for ``interval_s`` seconds on launch.
        while not self._stop.is_set():
            for name, (url, path) in self._sources.items():
                if self._stop.is_set():
                    return
                p = self._fetch(url, path)
                if p is not None:
                    self.prices[name] = (p, time.time())
            self._stop.wait(self.interval_s)


def _render_exchange_panel(feed: ExchangePriceFeed) -> list[str]:
    """Render one row per exchange with cross-exchange spread vs Binance."""
    if not feed.prices:
        return ["  " + _c(DIM, "Cargando precios de exchanges…")]
    out = [_c(BOLD, "  Precios live BTC/USD (otros exchanges):")]
    binance_price = feed.prices.get("Binance", (None, None))[0]
    now = time.time()
    # Sort: Binance first, then by name so the layout is stable.
    items = sorted(
        feed.prices.items(),
        key=lambda kv: (0 if kv[0] == "Binance" else 1, kv[0]),
    )
    for name, (price, ts) in items:
        diff_str = ""
        if binance_price is not None and name != "Binance":
            diff_pct = (price - binance_price) / binance_price * 100.0
            color = GREEN if diff_pct >= 0 else RED
            sign = "+" if diff_pct >= 0 else ""
            diff_str = "   " + _c(color, f"({sign}{diff_pct:.3f}% vs Binance)")
        age = now - ts
        if age < 5.0:
            age_str = _c(GREEN, "live")
        elif age < 30.0:
            age_str = _c(YELLOW, f"{int(age)}s")
        else:
            age_str = _c(RED, f"{int(age)}s stale")
        out.append(
            f"    {_c(CYAN, f'{name:<9}')}  "
            f"{_c(BOLD, f'${price:>11,.2f}')}  "
            f"{age_str}{diff_str}"
        )
    return out


# ---------- formatting ---------- #


def _pct(p: float) -> str:
    return f"{p * 100:5.1f}%"


def _signed_pp(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x * 100:.2f} pp"


def _time_left(t_in_window: float, T: float = 300.0) -> str:
    left = max(0.0, T - t_in_window)
    m, s = divmod(int(left), 60)
    return f"{m}:{s:02d}"


def _bps_delta(now: float, open_: float) -> str:
    if open_ <= 0:
        return "—"
    pct = (now - open_) / open_ * 100.0
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.3f}%"


def _direction_arrow(p_final: float, p_market: float, side: str) -> str:
    """Show direction the model prefers. ``side`` arrives as
    ``side_picked`` from the strategy (set even in shadow mode) so we
    don't fall back to ``—`` when no model is loaded.
    """
    if side == "YES_UP":
        return _c(GREEN + ";" + BOLD, "↑ SUBIRÁ")
    if side == "YES_DOWN":
        return _c(RED + ";" + BOLD, "↓ BAJARÁ")
    # No side annotation — derive from p_final vs p_market.
    diff = p_final - p_market
    if diff > 0.005:
        return _c(GREEN + ";" + BOLD, "↑ SUBIRÁ")
    if diff < -0.005:
        return _c(RED + ";" + BOLD, "↓ BAJARÁ")
    return _c(GREY, "↔ NEUTRAL")


# ---------- card rendering ---------- #


CARD_WIDTH = 90


def _line(s: str = "", color: str | None = None) -> str:
    """Pad to CARD_WIDTH inside the card frame, optionally coloured."""
    visible_len = _visible_len(s)
    pad = max(0, CARD_WIDTH - 4 - visible_len)
    body = s + " " * pad
    inner = _c(color, body) if color else body
    return f"│ {inner} │"


def _visible_len(s: str) -> int:
    """Length of a string ignoring ANSI escape sequences."""
    out = 0
    in_esc = False
    for ch in s:
        if ch == "\033":
            in_esc = True
            continue
        if in_esc:
            if ch == "m":
                in_esc = False
            continue
        out += 1
    return out


def _render_card(ev: dict[str, Any]) -> list[str]:
    slug = str(ev.get("slug", "?"))
    t = float(ev.get("t_in_window", 0.0))
    spot = float(ev.get("spot", 0.0))
    open_ = float(ev.get("open", 0.0))
    p_market = float(ev.get("p_market", 0.5))
    p_bm = float(ev.get("p_bm", 0.5))
    p_edge = float(ev.get("p_edge", 0.5))
    p_final = float(ev.get("p_final", 0.5))
    edge_net = float(ev.get("edge_net", 0.0))
    sharpe = float(ev.get("sharpe", 0.0))
    sharpe_th = float(ev.get("sharpe_th", 2.0))
    fee = float(ev.get("fee", 0.0))
    spread = float(ev.get("spread_bps", 0.0))
    ofi = float(ev.get("ofi", 0.0))
    intensity = float(ev.get("intensity", 1.0))
    large = int(ev.get("large_trade", 0))
    alpha = float(ev.get("alpha", 0.0))
    action = str(ev.get("action", "SKIP"))
    side = str(ev.get("side", "NONE"))
    reason = str(ev.get("reason", ""))
    shadow = bool(ev.get("shadow", False))

    # Diferencia entre lo que dice el modelo y lo que dice el mercado,
    # en el lado que el modelo prefiere (siempre |.|, nunca negativo —
    # el signo lo lleva la flecha de Predicción).
    edge_gross = abs(p_final - p_market)

    # ---- Header ---- #
    title = f" {slug}  ·  vence en {_time_left(t)}  ·  t={int(t)}s "
    bar_left = "─" * 2
    bar_right = "─" * (CARD_WIDTH - 2 - len(title) - 2)
    header_top = f"┌{bar_left}{title}{bar_right}┐"

    # ---- Spot move ---- #
    if open_ <= 0.0:
        # Driver hasn't surfaced the window's strike to this strategy
        # yet — the BB prior degrades to 50/50 as designed. Show that
        # honestly instead of "BTC abrió: $0.00".
        spot_line = (
            f"BTC ahora:  {_c(BOLD, f'${spot:>10,.2f}')}     "
            f"{_c(DIM, '(precio de apertura aún no disponible — prior degrada a 50/50)')}"
        )
    else:
        spot_line = (
            f"BTC abrió:  {_c(BOLD, f'${open_:>10,.2f}')}     "
            f"→  ahora:  {_c(BOLD, f'${spot:>10,.2f}')}     "
            f"({_c(GREEN if spot >= open_ else RED, _bps_delta(spot, open_))})"
        )

    # ---- Probabilities side-by-side ---- #
    # Reconstruct best bid/ask from mid + spread_bps (the strategy emits
    # both). Surfacing them makes it obvious when the binary book is
    # parked at the $0.01 tick boundary — otherwise the mid looks
    # "stuck at 50%" while the real signal is "no fresh trades hit the
    # book in N seconds".
    half_spread = p_market * (spread / 10000.0) / 2.0
    bid_yes = max(0.0, p_market - half_spread)
    ask_yes = min(1.0, p_market + half_spread)
    market_line = (
        f"  {_c(CYAN, 'Polymarket dice:')}    "
        f"SÍ {_c(BOLD, _pct(p_market))}      "
        f"NO {_c(BOLD, _pct(1 - p_market))}"
        f"   {_c(DIM, f'(libro: {bid_yes * 100:.1f}¢ bid / {ask_yes * 100:.1f}¢ ask)')}"
    )
    bm_line = (
        f"  {_c(BLUE, 'Brownian Bridge:')}    "
        f"SÍ {_c(BOLD, _pct(p_bm))}      "
        f"NO {_c(BOLD, _pct(1 - p_bm))}"
        f"   {_c(DIM, '(prior sin modelo)')}"
    )
    model_line = (
        f"  {_c(MAGENTA, 'Modelo (p_edge):')}    "
        f"SÍ {_c(BOLD, _pct(p_edge))}      "
        f"NO {_c(BOLD, _pct(1 - p_edge))}"
        + (_c(DIM, "   (= prior, sin modelo entrenado)") if shadow and p_edge == p_bm else "")
    )
    final_line = (
        f"  {_c(YELLOW, 'Combinado final:')}    "
        f"SÍ {_c(BOLD, _pct(p_final))}      "
        f"NO {_c(BOLD, _pct(1 - p_final))}"
        f"   {_c(DIM, f'(α={alpha:.2f})')}"
    )

    # ---- Direction + edge ---- #
    direction_line = f"  Predicción:    {_direction_arrow(p_final, p_market, side)}"
    edge_color = GREEN if edge_net > 0 else (YELLOW if abs(edge_net) < 0.005 else RED)
    fee_str = f"-{fee * 100:.2f} pp"
    edge_line = (
        f"  Diferencia con mercado:  {_c(BOLD, f'{edge_gross * 100:.2f} pp')}     "
        f"Comisión: {_c(BOLD, fee_str)}     "
        f"Edge neto: {_c(edge_color + ';' + BOLD, _signed_pp(edge_net))}"
    )

    sharpe_color = GREEN if sharpe >= sharpe_th else GREY
    sharpe_line = (
        f"  Confianza (Sharpe):  "
        f"{_c(sharpe_color + ';' + BOLD, f'{sharpe:.2f}')}  "
        f"/  {_c(BOLD, f'{sharpe_th:.1f}')} requerido"
    )

    # ---- Microstructure context ---- #
    ofi_color = GREEN if ofi > 0.05 else (RED if ofi < -0.05 else GREY)
    ofi_word = "compradores" if ofi > 0.05 else ("vendedores" if ofi < -0.05 else "equilibrado")
    micro_line = (
        f"  {_c(DIM, 'Microestructura:')}  "
        f"OFI={_c(ofi_color, f'{ofi:+.2f}')} ({ofi_word})  "
        f"·  intensidad×{intensity:.2f}"
        f"  ·  spread {spread:.0f}bp"
        + ("  ·  " + _c(YELLOW + ";" + BOLD, "TRADE GRANDE") if large else "")
    )

    # ---- Recommendation banner ---- #
    rec_label, rec_color, rec_motivo = _recommendation(action, reason, side)
    rec_line_top = "  " + _c(rec_color + ";" + BOLD, f" {rec_label:^28} ")
    rec_line_motivo = "  " + _c(DIM, rec_motivo)
    rec_line_reason = "  " + _c(DIM, f"motivo técnico: {reason}")

    return [
        header_top,
        _line(spot_line),
        _line(),
        _line(market_line),
        _line(bm_line),
        _line(model_line),
        _line(final_line),
        _line(),
        _line(direction_line),
        _line(edge_line),
        _line(sharpe_line),
        _line(micro_line),
        _line(),
        _line(rec_line_top),
        _line(rec_line_motivo),
        _line(rec_line_reason),
        f"└{'─' * (CARD_WIDTH - 2)}┘",
    ]


def _render_summary_row(ev: dict[str, Any]) -> str:
    slug = str(ev.get("slug", "?"))
    slug_tail = slug.rsplit("-", 1)[-1][-10:]
    t = float(ev.get("t_in_window", 0.0))
    p_market = float(ev.get("p_market", 0.5))
    p_final = float(ev.get("p_final", 0.5))
    edge_net = float(ev.get("edge_net", 0.0))
    side = str(ev.get("side", "NONE"))
    action = str(ev.get("action", "SKIP"))
    reason = str(ev.get("reason", ""))

    arrow = "↑" if side == "YES_UP" else ("↓" if side == "YES_DOWN" else "·")
    arrow_color = GREEN if side == "YES_UP" else (RED if side == "YES_DOWN" else GREY)

    edge_color = GREEN if edge_net > 0 else (YELLOW if abs(edge_net) < 0.005 else RED)
    rec_label, _, _ = _recommendation(action, reason, side)
    rec_color = GREEN if action == "ENTER" else GREY
    return (
        f"  {_c(DIM, slug_tail):>10}  "
        f"t={int(t):>3}s  "
        f"PM={_pct(p_market)}  "
        f"Modelo={_pct(p_final)}  "
        f"{_c(arrow_color, arrow)}  "
        f"edge={_c(edge_color, _signed_pp(edge_net)):>16}  "
        f"{_c(rec_color, rec_label)}"
    )


# ---------- live state + render loop ---------- #


def _stream_lines(args: argparse.Namespace):
    cmd = ["docker", "logs", "-f"]
    if args.since:
        cmd += ["--since", args.since]
    else:
        cmd += ["--tail", "0"]
    cmd.append(args.container)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            yield raw.decode("utf-8", errors="replace").rstrip("\n")
    finally:
        proc.terminate()


def _redraw(
    state: dict[str, dict[str, Any]],
    focus_slug: str | None,
    feed: ExchangePriceFeed | None = None,
) -> None:
    sys.stdout.write(CLEAR_SCREEN)
    width = shutil.get_terminal_size((100, 40)).columns
    title = " bb_residual_ofi_v1 — dashboard en vivo "
    bar = "═" * max(0, (width - len(title)) // 2)
    print(_c(BOLD + ";" + CYAN, f"{bar}{title}{bar}"))
    print(
        _c(
            DIM,
            f"  {time.strftime('%H:%M:%S')}  ·  "
            f"{len(state)} mercado(s) activo(s)  ·  "
            "Ctrl+C para salir",
        )
    )
    print()

    if not state:
        print(_c(DIM, "  Esperando primer tick del engine…"))
        sys.stdout.flush()
        return

    # Big card for the most recently updated market.
    if focus_slug and focus_slug in state:
        for line in _render_card(state[focus_slug]):
            print(line)
        print()

    # Multi-exchange spot panel — sits between the focused card and
    # the "otras ventanas" list so the user reads it alongside the
    # strategy's view of Binance.
    if feed is not None:
        for line in _render_exchange_panel(feed):
            print(line)
        print()

    # Compact list of the others (sorted by t_in_window descending so the
    # ones closest to settling are at top — those are the ones that
    # actually matter for trading decisions).
    others = [
        (s, ev)
        for s, ev in state.items()
        if s != focus_slug
    ]
    if others:
        others.sort(key=lambda kv: float(kv[1].get("t_in_window", 0.0)), reverse=True)
        print(_c(BOLD, "  Otras ventanas activas:"))
        for _, ev in others[:8]:
            print(_render_summary_row(ev))

    sys.stdout.flush()


MARKET_TIMEOUT_S = 15.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--container", default="tea-engine")
    ap.add_argument(
        "--since",
        default="1m",
        help='docker --since arg (e.g. "5m"). Default: 1m so the panel '
        "populates immediately from recent log buffer.",
    )
    ap.add_argument(
        "--exchanges",
        default="binance,coinbase,kraken,bitstamp,okx",
        help="Comma-separated list of exchanges to poll for BTC/USD spot. "
        "Set to empty string to disable the panel entirely. Public REST, "
        "no auth needed.",
    )
    ap.add_argument(
        "--exchange-interval",
        type=float,
        default=3.0,
        help="Seconds between exchange polls (default 3.0).",
    )
    args = ap.parse_args()

    state: dict[str, dict[str, Any]] = {}
    last_seen_at: dict[str, float] = {}
    focus_slug: str | None = None

    enabled_exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    feed: ExchangePriceFeed | None = None
    if enabled_exchanges:
        feed = ExchangePriceFeed(
            interval_s=args.exchange_interval, enabled=enabled_exchanges
        )
        feed.start()

    if COLOR:
        sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    try:
        for line in _stream_lines(args):
            try:
                ev = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if ev.get("event") != "bb_ofi.decision":
                continue

            slug = str(ev.get("slug", "?"))
            now = time.monotonic()
            state[slug] = ev
            last_seen_at[slug] = now
            focus_slug = slug

            # Garbage-collect stale markets (window rolled over and the
            # slug no longer ticks).
            for s in list(state.keys()):
                if now - last_seen_at.get(s, 0.0) > MARKET_TIMEOUT_S:
                    state.pop(s, None)
                    last_seen_at.pop(s, None)

            _redraw(state, focus_slug, feed=feed)
    except KeyboardInterrupt:
        return 0
    finally:
        if feed is not None:
            feed.stop()
        if COLOR:
            sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
