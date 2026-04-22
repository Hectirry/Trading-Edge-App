"""Interactive Telegram command poller (ADR 0009).

Long-polls getUpdates, dispatches 9 commands to tea-api. Auth by CSV of
user_ids in ``TEA_TELEGRAM_AUTHORIZED_USERS``; all others get a polite
reject. The destructive /killswitch is behind a two-step FSM that requires
the exact phrase ``sí lo entiendo`` as confirmation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from trading.common.config import get_settings
from trading.common.logging import get_logger

log = get_logger("bots.telegram.commands")

POLL_TIMEOUT_S = 25          # long-poll upper bound
HTTP_TIMEOUT_S = 30.0        # httpx read timeout (> POLL_TIMEOUT_S)
KILLSWITCH_CONFIRM = "sí lo entiendo"
KILLSWITCH_FSM_TTL_S = 120   # pending confirmation expires after 2 min


def _fmt_pct(v: float | None) -> str:
    return "-" if v is None else f"{v*100:.1f}%"


def _fmt_money(v: float | None) -> str:
    return "-" if v is None else f"${v:,.2f}"


class CommandPoller:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.bot_token = self.settings.telegram_bot_token
        self.api_base = self.settings.api_base_url.rstrip("/")
        self.api_token = self.settings.api_token
        authorized_csv = self.settings.telegram_authorized_users or ""
        self.authorized: set[int] = {
            int(x) for x in authorized_csv.split(",") if x.strip().isdigit()
        }
        self._offset: int = 0
        self._http = httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
        # killswitch FSM: user_id -> (prompted_at UTC datetime)
        self._killswitch_pending: dict[int, datetime] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.authorized)

    async def run(self) -> None:
        if not self.enabled:
            log.warning(
                "commands.disabled",
                has_token=bool(self.bot_token),
                n_authorized=len(self.authorized),
            )
            return
        log.info("commands.started", n_authorized=len(self.authorized))
        while True:
            try:
                updates = await self._get_updates()
            except Exception as e:
                log.warning("commands.getupdates_err", err=str(e))
                await asyncio.sleep(3)
                continue
            for upd in updates:
                try:
                    await self._handle_update(upd)
                except Exception as e:
                    log.exception("commands.handle_update_err", err=str(e))

    async def _get_updates(self) -> list[dict]:
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params: dict[str, Any] = {
            "timeout": POLL_TIMEOUT_S,
            "allowed_updates": '["message"]',
        }
        if self._offset:
            params["offset"] = self._offset
        r = await self._http.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            log.warning("commands.getupdates_not_ok", body=str(data)[:200])
            return []
        updates = data.get("result", []) or []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not text or user_id is None or chat_id is None:
            return

        if user_id not in self.authorized:
            log.warning("commands.unauthorized", user_id=user_id, text=text[:60])
            await self._reply(chat_id, "⛔ not authorized")
            return

        if user_id in self._killswitch_pending:
            prompted_at = self._killswitch_pending[user_id]
            if datetime.now(tz=UTC) - prompted_at < timedelta(seconds=KILLSWITCH_FSM_TTL_S):
                self._killswitch_pending.pop(user_id, None)
                await self._cmd_killswitch_confirm(chat_id, user_id, text)
                return
            self._killswitch_pending.pop(user_id, None)

        if not text.startswith("/"):
            return

        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        args = rest.strip()

        handlers = {
            "/status": self._cmd_status,
            "/positions": self._cmd_positions,
            "/trades": self._cmd_trades,
            "/pnl": self._cmd_pnl,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/killswitch": self._cmd_killswitch,
            "/backtest": self._cmd_backtest,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler is None:
            await self._reply(chat_id, f"unknown command: {cmd}\n/help for list")
            return
        log.info("commands.dispatch", cmd=cmd, user_id=user_id)
        await handler(chat_id, user_id, args)

    async def _reply(self, chat_id: int, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            await self._http.post(
                url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        except Exception as e:
            log.warning("commands.reply_err", err=str(e))

    async def _api_get(self, path: str, params: dict | None = None) -> tuple[int, Any]:
        url = f"{self.api_base}{path}"
        headers = {"X-TEA-Token": self.api_token}
        r = await self._http.get(url, params=params or {}, headers=headers)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return r.status_code, body

    async def _api_post(self, path: str, json_body: dict | None = None) -> tuple[int, Any]:
        url = f"{self.api_base}{path}"
        headers = {"X-TEA-Token": self.api_token}
        r = await self._http.post(url, json=json_body or {}, headers=headers)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return r.status_code, body

    # ---------- command handlers ----------

    async def _cmd_help(self, chat_id: int, user_id: int, args: str) -> None:
        await self._reply(
            chat_id,
            "TEA bot — available commands:\n"
            "/status              engine + kill switch\n"
            "/positions           open paper positions\n"
            "/trades [N]          last N paper trades (default 10)\n"
            "/pnl [hours]         pnl over last H hours (default 24)\n"
            "/pause <strategy>    pause a strategy\n"
            "/resume <strategy>   resume a strategy\n"
            "/killswitch          arm KILL_SWITCH (two-step)\n"
            "/backtest <strategy> <params_file> <from_ts> <to_ts> "
            "[source] [polybot_db] [slug_encodes_open_ts]\n"
            "/help                this message",
        )

    async def _cmd_status(self, chat_id: int, user_id: int, args: str) -> None:
        code, body = await self._api_get("/api/v1/status")
        if code != 200:
            await self._reply(chat_id, f"api error {code}: {body}")
            return
        ks = "ON" if body.get("kill_switch_active") else "OFF"
        hb = body.get("heartbeat") or {}
        lines = [
            f"engine heartbeat: age={hb.get('age_s', '-')}s",
            f"kill switch: {ks}",
            f"open positions: {body.get('open_positions', '-')}",
            f"trades today: {body.get('trades_today', '-')}",
            f"pnl today: {_fmt_money(body.get('pnl_today'))}",
        ]
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_positions(self, chat_id: int, user_id: int, args: str) -> None:
        code, body = await self._api_get("/api/v1/positions")
        if code != 200:
            await self._reply(chat_id, f"api error {code}: {body}")
            return
        pos = body.get("positions", [])
        if not pos:
            await self._reply(chat_id, "no open positions")
            return
        lines = ["open positions:"]
        for p in pos[:15]:
            lines.append(
                f"· {p.get('strategy_id','?')} {p.get('instrument_id','?')[:24]} "
                f"qty={p.get('qty')} avg={p.get('avg_price')}"
            )
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_trades(self, chat_id: int, user_id: int, args: str) -> None:
        n = 10
        if args:
            try:
                n = min(50, max(1, int(args.split()[0])))
            except ValueError:
                pass
        code, body = await self._api_get("/api/v1/trades/recent", params={"n": n})
        if code != 200:
            await self._reply(chat_id, f"api error {code}: {body}")
            return
        trades = body.get("trades", [])
        if not trades:
            await self._reply(chat_id, "no recent trades")
            return
        lines = [f"last {len(trades)} trades:"]
        for t in trades:
            pnl = t.get("pnl")
            pnl_str = "-" if pnl is None else f"{float(pnl):+.4f}"
            lines.append(
                f"· {t.get('ts_submit','?')} {t.get('strategy_id','?')} "
                f"{t.get('instrument_id','?')[:16]} pnl={pnl_str}"
            )
        await self._reply(chat_id, "\n".join(lines))

    async def _cmd_pnl(self, chat_id: int, user_id: int, args: str) -> None:
        hours = 24
        if args:
            try:
                hours = min(24 * 30, max(1, int(args.split()[0])))
            except ValueError:
                pass
        code, body = await self._api_get("/api/v1/pnl", params={"hours": hours})
        if code != 200:
            await self._reply(chat_id, f"api error {code}: {body}")
            return
        await self._reply(
            chat_id,
            f"pnl last {hours}h: {_fmt_money(body.get('pnl'))}  "
            f"(n={body.get('n_trades')})",
        )

    async def _cmd_pause(self, chat_id: int, user_id: int, args: str) -> None:
        name = args.split()[0] if args else ""
        if not name:
            await self._reply(chat_id, "usage: /pause <strategy>")
            return
        code, body = await self._api_post(f"/api/v1/strategies/{name}/pause")
        if code != 200:
            await self._reply(chat_id, f"pause failed ({code}): {body}")
            return
        await self._reply(chat_id, f"⏸ {name} paused")

    async def _cmd_resume(self, chat_id: int, user_id: int, args: str) -> None:
        name = args.split()[0] if args else ""
        if not name:
            await self._reply(chat_id, "usage: /resume <strategy>")
            return
        code, body = await self._api_post(f"/api/v1/strategies/{name}/resume")
        if code != 200:
            await self._reply(chat_id, f"resume failed ({code}): {body}")
            return
        await self._reply(chat_id, f"▶️ {name} resumed")

    async def _cmd_killswitch(self, chat_id: int, user_id: int, args: str) -> None:
        self._killswitch_pending[user_id] = datetime.now(tz=UTC)
        await self._reply(
            chat_id,
            "⚠️ KILL_SWITCH arming. Reply with the exact phrase to confirm:\n"
            f"    {KILLSWITCH_CONFIRM}\n"
            f"(expires in {KILLSWITCH_FSM_TTL_S}s; any other reply cancels)",
        )

    async def _cmd_killswitch_confirm(
        self, chat_id: int, user_id: int, text: str
    ) -> None:
        if text.strip().lower() != KILLSWITCH_CONFIRM:
            await self._reply(chat_id, "phrase did not match. kill switch NOT armed.")
            return
        code, body = await self._api_post(
            "/api/v1/killswitch", json_body={"confirm": KILLSWITCH_CONFIRM}
        )
        if code != 200:
            await self._reply(chat_id, f"killswitch failed ({code}): {body}")
            return
        await self._reply(
            chat_id,
            f"⛔ KILL_SWITCH ARMED at {body.get('path','?')}. "
            "engine will refuse new orders until removed.",
        )

    async def _cmd_backtest(self, chat_id: int, user_id: int, args: str) -> None:
        parts = args.split()
        if len(parts) < 4:
            await self._reply(
                chat_id,
                "usage: /backtest <strategy> <params_file> <from_ts> <to_ts> "
                "[source] [polybot_db] [slug_encodes_open_ts]",
            )
            return
        payload: dict[str, Any] = {
            "strategy": parts[0],
            "params_file": parts[1],
            "from_ts": parts[2],
            "to_ts": parts[3],
            "requested_by": f"telegram:{user_id}",
        }
        if len(parts) >= 5:
            payload["source"] = parts[4]
        if len(parts) >= 6:
            payload["polybot_db"] = parts[5]
        if len(parts) >= 7:
            payload["slug_encodes_open_ts"] = parts[6].lower() in ("1", "true", "yes")
        code, body = await self._api_post("/api/v1/backtests", json_body=payload)
        if code != 200:
            await self._reply(chat_id, f"backtest submit failed ({code}): {body}")
            return
        job_id = body.get("job_id", "?")
        # Schedule the follow-up link poster (non-blocking).
        asyncio.create_task(self._followup_backtest(chat_id, job_id))
        await self._reply(
            chat_id,
            f"🧪 backtest queued. job_id={job_id}\n"
            "I'll DM when it finishes with a link to the report.",
        )

    async def _followup_backtest(self, chat_id: int, job_id: str) -> None:
        """Poll the job every 10 s for up to 20 min, then send the report link."""
        deadline = asyncio.get_event_loop().time() + 20 * 60
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(10)
            code, body = await self._api_get(f"/api/v1/jobs/{job_id}")
            if code != 200:
                continue
            status = body.get("status")
            if status in ("completed", "failed", "timeout"):
                bt_id = body.get("backtest_id")
                if bt_id:
                    base = self.settings.dashboard_public_url.rstrip("/")
                    link = f"{base}/research/{bt_id}"
                    await self._reply(
                        chat_id,
                        f"✅ backtest {status}. job={job_id}\nreport: {link}",
                    )
                else:
                    await self._reply(
                        chat_id,
                        f"{'⚠️' if status != 'completed' else '✅'} "
                        f"backtest {status}. job={job_id}\n"
                        f"error: {body.get('error_message') or '-'}",
                    )
                return
        await self._reply(
            chat_id,
            f"⏱ backtest {job_id} still running after 20 min — check /research",
        )
