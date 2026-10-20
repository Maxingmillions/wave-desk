"""
Telegram: push signals and fills, accept a handful of commands back.

    tg = Telegram()                       # token + chat id from .env
    tg.send("ABCD breaking out, rvol 6.1")
    async for cmd in tg.commands():       # yields ("buy", ["ABCD", "100"]) etc.

Plain HTTPS calls to api.telegram.org; no extra dependency. Everything is
best-effort: a Telegram outage must never stop the desk.
"""
from __future__ import annotations

import asyncio
import html
from typing import AsyncIterator, List, Optional, Tuple

import requests

import config

API = "https://api.telegram.org/bot{token}/{method}"

HELP = (
    "<b>Commands</b>\n"
    "/signals - current waves\n"
    "/positions - open positions and trails\n"
    "/buy TICKER [qty] - enter with trailing stop\n"
    "/flat TICKER - cancel orders and sell\n"
    "/mode signal|manual|auto\n"
    "/pause  /resume\n"
    "/status - account and guard state"
)


class Telegram:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        self.chat_id = str(chat_id or config.TELEGRAM_CHAT_ID)
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # ------------------------------------------------------------ outbound
    def _post(self, method: str, **data) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            r = requests.post(API.format(token=self.token, method=method), json=data, timeout=20)
            j = r.json()
            return j if j.get("ok") else None
        except (requests.RequestException, ValueError):
            return None

    def send(self, text: str, silent: bool = False) -> bool:
        """HTML-formatted message. Returns False when disabled or failed."""
        if not self.enabled:
            return False
        return self._post("sendMessage", chat_id=self.chat_id, text=text[:4000],
                          parse_mode="HTML", disable_web_page_preview=True,
                          disable_notification=silent) is not None

    async def send_async(self, text: str, silent: bool = False) -> bool:
        return await asyncio.get_running_loop().run_in_executor(None, self.send, text, silent)

    # ------------------------------------------------------------ inbound
    def _poll(self, timeout: int = 25) -> List[dict]:
        if not self.enabled:
            return []
        try:
            r = requests.get(API.format(token=self.token, method="getUpdates"),
                             params={"offset": self._offset, "timeout": timeout,
                                     "allowed_updates": ["message"]},
                             timeout=timeout + 10)
            j = r.json()
        except (requests.RequestException, ValueError):
            return []
        if not j.get("ok"):
            return []
        updates = j.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def commands(self) -> AsyncIterator[Tuple[str, List[str]]]:
        """Long-poll loop. Only messages from the configured chat are honoured."""
        loop = asyncio.get_running_loop()
        while True:
            updates = await loop.run_in_executor(None, self._poll)
            for u in updates:
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                parts = text.split()
                cmd = parts[0][1:].split("@")[0].lower()
                yield cmd, [p.upper() if i == 0 and cmd in ("buy", "flat") else p for i, p in enumerate(parts[1:])]
            if not updates:
                await asyncio.sleep(0.5)


# ------------------------------------------------------------ message shapes
def esc(s) -> str:
    return html.escape(str(s))


def fmt_signal(sig) -> str:
    """One wave, as a Telegram message."""
    brk = {"20d_high": "20-day high", "hod": "new high of day", "orb": "opening-range breakout"}.get(
        sig.breakout, "volume influx")
    lines = [f"<b>{esc(sig.ticker)}</b>  ${sig.price:.2f}  {sig.change_pct:+.1f}%  rvol {sig.rel_vol:.1f}x  -  {brk}",
             f"score {sig.score:.1f} · ${sig.dollar_vol_m:.1f}M traded · stop {sig.stop_pct:.0f}% · trail {sig.trail_pct:.0f}%"]
    if sig.news_count:
        lines.append(f"news ({sig.news_count}): {esc(sig.headline)}")
    if sig.suggested_qty:
        lines.append(f"size {sig.suggested_qty} sh ≈ ${sig.suggested_qty * sig.price:,.0f}  →  /buy {esc(sig.ticker)} {sig.suggested_qty}")
    return "\n".join(lines)


def fmt_positions(positions, rides) -> str:
    if not positions:
        return "No open positions."
    rows = []
    for p in positions:
        r = rides.get(p.ticker)
        trail = f"trail {r.trail_pct:.0f}% from {r.high_water:.2f}" if r else "no trail"
        pnl = p.pnl_pct
        rows.append(f"<b>{esc(p.ticker)}</b> {p.quantity:.0f} @ {p.avg_price:.2f} → {p.last:.2f} "
                    f"({pnl:+.1f}%)  {trail}" if pnl is not None else
                    f"<b>{esc(p.ticker)}</b> {p.quantity:.0f} @ {p.avg_price:.2f}  {trail}")
    return "\n".join(rows)
