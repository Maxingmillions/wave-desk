"""
Wave desk -- the interactive terminal.

    python terminal.py              # connect to TWS / IB Gateway (see .env), fall back to --sim
    python terminal.py --sim        # no broker: Yahoo data, in-memory fills, trailing stops simulated
    python terminal.py --mode auto  # signal | manual | auto (overrides WAVE_MODE)
    python terminal.py --watch ABCD,EFGH

Left: the scanner board (waves) and the detail of the selected wave.
Right: positions with their trailing stops, working orders, the log.

Keys:  b buy selected wave   f flat selected position   t set trailing stop
       m cycle mode          space pause/resume          r scan now
       p rotation plan       w add watch ticker          c cancel orders on selected
       q quit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Static

import config
import scanner
from telegram_bot import Telegram
from wave_rider import MODES, SimBroker, WaveRider

GAIN, LOSS, WAVE, MUTED, TEXT = "#3ddc97", "#ff6b6b", "#f5b544", "#7c8ba3", "#d6deea"


def pnl_text(v: Optional[float], fmt: str = "{:+.1f}%") -> Text:
    if v is None:
        return Text("-", style=MUTED, justify="right")
    return Text(fmt.format(v), style=GAIN if v > 0 else LOSS if v < 0 else TEXT, justify="right")


def num(v, fmt: str = "{:.2f}", style: str = TEXT) -> Text:
    """Right-aligned number cell; '-' when missing."""
    if v is None:
        return Text("-", style=MUTED, justify="right")
    return Text(fmt.format(v), style=style, justify="right")


def age_text(ts_iso: str) -> Text:
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts_iso)).total_seconds() / 60
    except ValueError:
        return Text("-", style=MUTED)
    s = f"{age:.0f}m" if age < 90 else f"{age / 60:.1f}h"
    return Text(s, style=WAVE if age < 5 else TEXT if age < 30 else MUTED, justify="right")


# ----------------------------------------------------------------- widgets
class Pane(Container):
    """A bordered region with a title; focus moves the highlight."""

    def __init__(self, title: str, *children, **kw):
        super().__init__(*children, **kw)
        self.border_title = title


class OrderModal(ModalScreen[Optional[dict]]):
    """Buy with an optional trailing stop. Returns the order dict or None."""

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, ticker: str, price: float, qty: int, limit: float, trail_pct: float,
                 live: bool, note: str = ""):
        super().__init__()
        self.ticker, self.price, self.qty, self.limit, self.trail_pct = ticker, price, qty, limit, trail_pct
        self.live, self.note = live, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"Buy {self.ticker}", classes="title")
            if self.live:
                yield Static("This is a LIVE account. The order is real.", classes="live-banner")
            yield Static(self.note or f"last {self.price:.2f}", classes="hint")
            yield Label("Shares")
            yield Input(str(self.qty or ""), id="qty", type="integer")
            yield Label("Limit price (blank = market)")
            yield Input(f"{self.limit:.2f}" if self.limit else "", id="limit", type="number")
            yield Label("Trailing stop % (0 = none)")
            yield Input(f"{self.trail_pct:g}", id="trail", type="number")
            yield Static("", id="cost", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Place order", id="place", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#qty", Input).focus()
        self._update_cost()

    @on(Input.Changed)
    def _update_cost(self, _=None) -> None:
        try:
            q = int(self.query_one("#qty", Input).value or 0)
            lim = self.query_one("#limit", Input).value
            px = float(lim) if lim else self.price
            t = float(self.query_one("#trail", Input).value or 0)
            txt = f"≈ ${q * px:,.0f}"
            if t:
                txt += f"   first trail stop ≈ {px * (1 - t / 100):.2f}"
            self.query_one("#cost", Static).update(txt)
        except ValueError:
            self.query_one("#cost", Static).update("")

    @on(Button.Pressed, "#place")
    @on(Input.Submitted)
    def _place(self, _=None) -> None:
        try:
            qty = int(self.query_one("#qty", Input).value or 0)
            lim = self.query_one("#limit", Input).value
            limit = float(lim) if lim else None
            trail = float(self.query_one("#trail", Input).value or 0)
        except ValueError:
            self.query_one("#cost", Static).update(Text("Numbers only.", style=LOSS))
            return
        if qty <= 0:
            self.query_one("#cost", Static).update(Text("Shares must be at least 1.", style=LOSS))
            return
        self.dismiss({"ticker": self.ticker, "qty": qty, "limit": limit, "trail_pct": trail})

    @on(Button.Pressed, "#cancel")
    def action_cancel(self, _=None) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "no"), Binding("y", "yes", "yes"), Binding("n", "no", "no")]

    def __init__(self, title: str, body: str, danger: bool = False, ok_label: str = "Confirm"):
        super().__init__()
        self.title_text, self.body, self.danger, self.ok_label = title, body, danger, ok_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="title")
            yield Static(self.body)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="no", variant="default")
                yield Button(self.ok_label, id="yes", variant="error" if self.danger else "primary")

    @on(Button.Pressed, "#yes")
    def action_yes(self, _=None) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self, _=None) -> None:
        self.dismiss(False)


class TextModal(ModalScreen[Optional[str]]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title: str, hint: str = "", value: str = ""):
        super().__init__()
        self.title_text, self.hint, self.value = title, hint, value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="title")
            if self.hint:
                yield Static(self.hint, classes="hint")
            yield Input(self.value, id="text")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#text", Input).focus()

    @on(Button.Pressed, "#ok")
    @on(Input.Submitted)
    def _ok(self, _=None) -> None:
        self.dismiss(self.query_one("#text", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def action_cancel(self, _=None) -> None:
        self.dismiss(None)


class PlanModal(ModalScreen[Optional[dict]]):
    """Shows a rotation plan; pick a buy to pre-fill the order dialog."""

    BINDINGS = [Binding("escape", "close", "close"), Binding("enter", "pick", "buy selected")]

    def __init__(self, plan: dict):
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        p = self.plan
        with Vertical(classes="dialog dialog-wide"):
            yield Static(f"Rotation plan for {p.get('for_session', '?')}  ·  regime {p.get('regime', '?')}"
                         f"  ·  {p.get('_engine', '')}", classes="title")
            yield Static(p.get("market_summary", ""), classes="hint")
            t = DataTable(id="plan-buys", cursor_type="row", zebra_stripes=False)
            yield t
            yield Static("Holdings: " + "; ".join(
                f"{h['ticker']} {h['action']}{' ' + str(h['quantity']) if h.get('quantity') else ''}"
                for h in p.get("holdings", [])) or "Holdings: none", classes="hint")
            yield Static("Enter buys the selected row. Esc closes.", classes="hint")

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("ticker", "qty", "limit", "stop", "conf", "catalyst")
        for b in self.plan.get("buys", []):
            t.add_row(Text(b["ticker"], style=f"bold {WAVE}"), str(b["quantity"]),
                      f"{b['limit_price']:.2f}", f"{b['stop_price']:.2f}", b["confidence"],
                      b["catalyst"][:60], key=b["ticker"])
        if not self.plan.get("buys"):
            t.add_row(Text("no buys proposed", style=MUTED), "", "", "", "", "")
        t.focus()

    def action_pick(self) -> None:
        t = self.query_one(DataTable)
        if not self.plan.get("buys") or t.row_count == 0:
            return
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        b = next((x for x in self.plan["buys"] if x["ticker"] == key), None)
        self.dismiss(b)

    def action_close(self) -> None:
        self.dismiss(None)


# ----------------------------------------------------------------- app
class WaveDesk(App):
    CSS_PATH = "terminal.tcss"
    TITLE = "Wave desk"
    BINDINGS = [
        Binding("b", "buy", "buy"),
        Binding("f", "flat", "flat"),
        Binding("t", "trail", "trail"),
        Binding("m", "mode", "mode"),
        Binding("space", "pause", "pause"),
        Binding("r", "scan", "scan"),
        Binding("p", "plan", "plan"),
        Binding("w", "watch", "watch"),
        Binding("c", "cancel_orders", "cancel"),
        Binding("tab", "focus_next", "next pane", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, sim: bool = False, mode: Optional[str] = None,
                 watch: Optional[list] = None, broker=None, rider: Optional[WaveRider] = None):
        super().__init__()
        self.sim, self.mode_arg, self.watch = sim, mode, watch or []
        # broker / rider are normally built in _boot (inside the app's loop);
        # tests may inject ready-made ones.
        self.broker = broker
        self.rider = rider
        self._selected_wave: Optional[str] = None

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Pane("Waves", DataTable(id="waves-table", cursor_type="row"), id="waves")
                yield Pane("Selected wave", Static("Pick a wave to see its headlines and sizing.",
                                                   id="detail-body", classes="detail-body"), id="detail")
            with Vertical(id="right"):
                yield Pane("Positions", DataTable(id="pos-table", cursor_type="row"), id="positions")
                yield Pane("Working orders", DataTable(id="ord-table", cursor_type="row"), id="orders")
                yield Pane("Log", RichLog(id="log-body", markup=True, wrap=True, min_width=20), id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#waves-table", DataTable).add_columns(
            "ticker", "last", "chg", "rvol", "$vol", "break", "news", "score", "size", "age")
        self.query_one("#pos-table", DataTable).add_columns(
            "ticker", "qty", "avg", "last", "P&L", "$", "trail", "stop")
        self.query_one("#ord-table", DataTable).add_columns(
            "id", "ticker", "side", "type", "qty", "price", "status")
        self.query_one("#waves-table", DataTable).focus()
        self.set_interval(1.0, self.render_topbar)
        self.run_worker(self._boot(), name="boot", exit_on_error=False)

    async def _boot(self) -> None:
        if self.broker is None:
            self.log_line("connecting…" if not self.sim else "starting the simulator…")
            self.broker, fell_back = await make_broker(self.sim, log=self.log_line)
        if self.rider is None:
            self.rider = WaveRider(self.broker, Telegram(), mode=self.mode_arg, watchlist=self.watch)
        self.rider.on_event = self.on_rider_event
        self.log_line(f"connected to [bold]{self.broker.name}[/] "
                      f"({'paper' if getattr(self.broker, 'is_paper', True) else '[bold red]LIVE[/]'})"
                      f" · mode {self.rider.mode} · Telegram {'on' if self.rider.tg.enabled else 'off'}")
        if self.broker.name == "sim":
            self.log_line("[dim]simulator: fills at Yahoo's last quote, trailing stops checked every refresh[/]")
        self.render_topbar()
        self.run_worker(self.rider.run(), name="rider", exit_on_error=False)
        self.run_worker(self.rider.telegram_loop(), name="telegram", exit_on_error=False)

    # ------------------------------------------------------------ rendering
    def render_topbar(self) -> None:
        if self.rider is None or self.broker is None:
            self.query_one("#topbar", Static).update(Text("● connecting…", style=MUTED))
            return
        a = self.rider.account
        r = self.rider
        conn = Text()
        conn.append("● ", style=GAIN if self.broker.connected else LOSS)
        conn.append(f"{self.broker.name} {'paper' if getattr(self.broker, 'is_paper', True) else 'LIVE'}",
                    style=TEXT if getattr(self.broker, "is_paper", True) else f"bold {LOSS}")
        conn.append("    mode ", style=MUTED)
        conn.append(r.mode, style=f"bold {WAVE}" if r.mode == "auto" else TEXT)
        if r.paused:
            conn.append(" paused", style=LOSS)
        if r.risk_state.halted_reason:
            conn.append("  halted", style=f"bold {LOSS}")
        conn.append(f"    equity ", style=MUTED)
        conn.append(f"{a.net_liq:,.0f}", style=TEXT)
        conn.append("  cash ", style=MUTED)
        conn.append(f"{a.cash:,.0f}", style=TEXT)
        conn.append("  day ", style=MUTED)
        conn.append_text(pnl_text(a.daily_pnl, "{:+,.0f}"))
        now = scanner.now_et()
        mkt = "open" if scanner.market_open(now) else "closed"
        scan = r.last_scan.astimezone(scanner.ET).strftime("%H:%M") if r.last_scan else "-"
        conn.append(f"    {now:%H:%M:%S} ET market {mkt}", style=MUTED)
        conn.append(f"   scan {scan}{' …' if r.scanning else ''}", style=WAVE if r.scanning else MUTED)
        self.query_one("#topbar", Static).update(conn)

    def render_waves(self) -> None:
        t = self.query_one("#waves-table", DataTable)
        keep = self._selected_wave
        t.clear()
        sigs = sorted(self.rider.signals.values(), key=lambda s: s.score, reverse=True)
        held = {p.ticker for p in self.rider.positions}
        for s in sigs:
            style = f"bold {WAVE}" if s.ticker not in held else f"bold {GAIN}"
            t.add_row(Text(s.ticker, style=style), num(s.price), pnl_text(s.change_pct),
                      num(s.rel_vol, "{:.1f}x"), num(s.dollar_vol_m, "{:.1f}M"), s.breakout_label,
                      num(s.news_count or None, "{:d}"), num(s.score, "{:.1f}", "bold"),
                      num(s.suggested_qty or None, "{:d}"), age_text(s.ts), key=s.ticker)
        if not sigs:
            msg = "scanning…" if self.rider.scanning else "no waves yet — r scans now"
            t.add_row(Text(msg, style=MUTED), *[""] * 9)
        if keep and keep in self.rider.signals:
            try:
                t.move_cursor(row=t.get_row_index(keep))
            except Exception:
                pass
        elif sigs:
            self._selected_wave = sigs[0].ticker
        self.render_detail()

    def render_detail(self) -> None:
        body = self.query_one("#detail-body", Static)
        s = self.rider.signals.get(self._selected_wave or "")
        if not s:
            body.update(Text("Pick a wave to see its headlines and sizing.", style=MUTED))
            return
        tx = Text()
        tx.append(f"{s.ticker}", style=f"bold {WAVE}")
        tx.append(f"  {s.name}\n", style=MUTED)
        tx.append(f"{s.price:.2f}  ")
        tx.append_text(pnl_text(s.change_pct))
        tx.append(f"  rvol {s.rel_vol:.1f}x  ${s.dollar_vol_m:.1f}M traded  ATR {s.atr_pct:.1f}%  "
                  f"via {s.breakout_label}  ({s.source})\n")
        tx.append(f"size {s.suggested_qty} sh ≈ ${s.suggested_qty * s.price:,.0f}  ·  "
                  f"limit {s.entry_limit:.2f}  ·  stop {s.stop_pct:.0f}%  ·  trail {s.trail_pct:.0f}%\n",
                  style=TEXT)
        if s.headlines:
            tx.append("\n")
            for h in s.headlines[:5]:
                age = h.get("age_h")
                tx.append(f"{age:>4.1f}h  " if age is not None else "   -   ", style=MUTED)
                tx.append(h["title"][:110] + "\n")
        else:
            tx.append("\nno headlines in the window\n", style=MUTED)
        tx.append("\nb buys this wave with the sizing above", style=MUTED)
        body.update(tx)

    def render_positions(self) -> None:
        t = self.query_one("#pos-table", DataTable)
        keep = self._cursor_key(t)
        t.clear()
        rides = self.rider.rides
        for p in sorted(self.rider.positions, key=lambda p: p.ticker):
            r = rides.get(p.ticker)
            on = bool(r and r.status == "open")
            t.add_row(Text(p.ticker, style="bold"), num(p.quantity, "{:.0f}"), num(p.avg_price),
                      num(p.last), pnl_text(p.pnl_pct), pnl_text(p.unrealized_pnl, "{:+,.0f}"),
                      num(r.trail_pct if on else None, "{:.0f}%"), num(r.trail_stop if on else None),
                      key=p.ticker)
        if not self.rider.positions:
            t.add_row(Text("flat", style=MUTED), *[""] * 7)
        self._restore_cursor(t, keep)

    def render_orders(self) -> None:
        t = self.query_one("#ord-table", DataTable)
        keep = self._cursor_key(t)
        t.clear()
        for o in self.rider.orders:
            px = (f"trail {o.trail_pct:g}%" if o.trail_pct else f"stop {o.stop:.2f}" if o.stop
                  else f"lmt {o.limit:.2f}" if o.limit else "mkt")
            t.add_row(num(o.order_id, "{:d}", MUTED), Text(o.ticker, style="bold"),
                      Text(o.side, style=GAIN if o.side == "BUY" else LOSS), o.order_type,
                      num(o.quantity, "{:.0f}"), px, o.status, key=str(o.order_id))
        if not self.rider.orders:
            t.add_row(Text("none", style=MUTED), *[""] * 6)
        self._restore_cursor(t, keep)

    @staticmethod
    def _cursor_key(t: DataTable) -> Optional[str]:
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    @staticmethod
    def _restore_cursor(t: DataTable, key: Optional[str]) -> None:
        if key is None:
            return
        try:
            t.move_cursor(row=t.get_row_index(key))
        except Exception:
            pass

    def _rider_ready(self) -> bool:
        if self.rider is None:
            self.notify("Still connecting.", severity="warning")
            return False
        return True

    def log_line(self, msg: str) -> None:
        ts = scanner.now_et().strftime("%H:%M:%S")
        self.query_one("#log-body", RichLog).write(f"[dim]{ts}[/] {msg}")

    # ------------------------------------------------------------ rider events
    def on_rider_event(self, kind: str, payload: dict) -> None:
        if kind == "log":
            self.log_line(payload["msg"])
        elif kind == "signal":
            s = payload["signal"]
            self.log_line(f"[bold {WAVE}]wave[/] {s.ticker} {s.price:.2f} {s.change_pct:+.1f}% "
                          f"rvol {s.rel_vol:.1f}x via {s.breakout_label}")
            if self.rider.mode == "manual" and s.suggested_qty:
                self.notify(f"{s.ticker} {s.change_pct:+.1f}% rvol {s.rel_vol:.1f}x — press b to ride",
                            title="Wave", timeout=8)
            self.render_waves()
        elif kind in ("scan", "refresh", "order", "mode"):
            self.render_waves()
            self.render_positions()
            self.render_orders()
            self.render_topbar()

    # ------------------------------------------------------------ selection
    @on(DataTable.RowHighlighted, "#waves-table")
    def _wave_highlighted(self, ev: DataTable.RowHighlighted) -> None:
        key = ev.row_key.value if ev.row_key else None
        if key in self.rider.signals:
            self._selected_wave = key
            self.render_detail()

    def _selected_position(self) -> Optional[str]:
        key = self._cursor_key(self.query_one("#pos-table", DataTable))
        return key if key and any(p.ticker == key for p in self.rider.positions) else None

    def _ticker_in_focus(self) -> Optional[str]:
        focused = self.focused
        if focused is not None and focused.id == "pos-table":
            return self._selected_position()
        if focused is not None and focused.id == "ord-table":
            key = self._cursor_key(focused)
            o = next((o for o in self.rider.orders if str(o.order_id) == key), None)
            return o.ticker if o else None
        return self._selected_wave

    # ------------------------------------------------------------ actions
    def action_buy(self) -> None:
        if not self._rider_ready():
            return
        ticker = self._ticker_in_focus() or self._selected_wave
        if not ticker:
            self.notify("Select a wave first (or w to watch a ticker).", severity="warning")
            return
        s = self.rider.signals.get(ticker)
        price = s.price if s else 0.0
        qty = s.suggested_qty if s else 0
        limit = s.entry_limit if s else 0.0
        trail = s.trail_pct if s else config.WAVE_TRAIL_PCT
        note = (f"last {price:.2f} · suggested {qty} sh (risk {config.WAVE_RISK_PCT}% of equity, "
                f"stop {s.stop_pct:.0f}%)" if s else "")
        self.push_screen(OrderModal(ticker, price, qty, limit, trail,
                                    live=not getattr(self.broker, "is_paper", True), note=note),
                         self._place_from_modal)

    def _place_from_modal(self, order: Optional[dict]) -> None:
        if not order:
            return
        self.run_worker(self._place(order), exclusive=False)

    async def _place(self, order: dict) -> None:
        t = order["ticker"]
        if order["trail_pct"] and order["trail_pct"] > 0:
            await self.rider.enter(t, qty=order["qty"], limit=order["limit"],
                                   trail_pct=order["trail_pct"], force=True)
        else:
            try:
                await self.broker.order(t, "BUY", order["qty"], order["limit"])
                self.log_line(f"{t}: BUY {order['qty']} "
                              f"{'lmt ' + format(order['limit'], '.2f') if order['limit'] else 'mkt'} sent (no trail)")
            except Exception as e:
                self.log_line(f"[{LOSS}]{t}: order failed: {e}[/]")
            await self.rider.refresh()

    def action_flat(self) -> None:
        if not self._rider_ready():
            return
        ticker = self._selected_position() or self._ticker_in_focus()
        if not ticker or not any(p.ticker == ticker for p in self.rider.positions):
            self.notify("Select a position to flatten.", severity="warning")
            return
        p = next(p for p in self.rider.positions if p.ticker == ticker)
        self.push_screen(
            ConfirmModal(f"Flatten {ticker}?",
                         f"Cancel working orders and sell {p.quantity:.0f} shares at market "
                         f"(last {p.last:.2f}, {p.pnl_pct:+.1f}%).", danger=True, ok_label="Sell all"),
            lambda ok: ok and self.run_worker(self.rider.flat(ticker, "terminal flat")))

    def action_trail(self) -> None:
        if not self._rider_ready():
            return
        ticker = self._selected_position() or self._ticker_in_focus()
        if not ticker or not any(p.ticker == ticker for p in self.rider.positions):
            self.notify("Select a position to trail.", severity="warning")
            return
        r = self.rider.rides.get(ticker)
        cur = f"{r.trail_pct:g}" if r else f"{config.WAVE_TRAIL_PCT:g}"
        self.push_screen(TextModal(f"Trailing stop for {ticker}",
                                   "Percent below the high-water mark. Replaces any working orders on the name.",
                                   cur),
                         lambda v: v and self._set_trail(ticker, v))

    def _set_trail(self, ticker: str, v: str) -> None:
        try:
            pct = float(v)
        except ValueError:
            self.notify("Enter a number.", severity="error")
            return
        if pct <= 0:
            self.notify("Trail must be above 0.", severity="error")
            return
        self.run_worker(self.rider.trail(ticker, pct))

    def action_mode(self) -> None:
        if not self._rider_ready():
            return
        nxt = MODES[(MODES.index(self.rider.mode) + 1) % len(MODES)]
        if nxt == "auto":
            self.push_screen(
                ConfirmModal("Switch to auto?",
                             "Every qualifying wave will be entered with a bracket (limit entry + trailing stop) "
                             "within the risk guards, without asking. Space pauses at any time.",
                             danger=not getattr(self.broker, "is_paper", True), ok_label="Go auto"),
                lambda ok: self._set_mode("auto" if ok else "signal"))
        else:
            self._set_mode(nxt)

    def _set_mode(self, mode: str) -> None:
        self.rider.set_mode(mode)
        self.render_topbar()

    def action_pause(self) -> None:
        if not self._rider_ready():
            return
        self.rider.set_paused(not self.rider.paused)
        self.render_topbar()

    def action_scan(self) -> None:
        if not self._rider_ready():
            return
        if self.rider.scanning:
            self.notify("Scan already running.")
            return
        self.log_line("scan requested")
        self.run_worker(self.rider.scan_once(), exclusive=False)
        self.render_topbar()

    def action_watch(self) -> None:
        if not self._rider_ready():
            return
        self.push_screen(TextModal("Watch a ticker", "Added to every scan; press r to scan it now."),
                         self._add_watch)

    def _add_watch(self, v: Optional[str]) -> None:
        if not v:
            return
        for t in v.replace(",", " ").split():
            t = t.upper()
            if t not in self.rider.watchlist:
                self.rider.watchlist.append(t)
        self.log_line(f"watching {', '.join(self.rider.watchlist)}")

    def action_cancel_orders(self) -> None:
        if not self._rider_ready():
            return
        ticker = self._ticker_in_focus()
        if not ticker:
            self.notify("Select an order or position.", severity="warning")
            return
        self.push_screen(ConfirmModal(f"Cancel orders on {ticker}?",
                                      "Working orders on this name are cancelled; the position stays.",
                                      ok_label="Cancel orders"),
                         lambda ok: ok and self.run_worker(self._cancel(ticker)))

    async def _cancel(self, ticker: str) -> None:
        n = await self.broker.cancel(ticker)
        self.log_line(f"{ticker}: {n} order(s) cancelled")
        await self.rider.refresh()

    def action_plan(self) -> None:
        if not self._rider_ready():
            return
        self.log_line("rotation plan: gathering news and prices (1-3 min)…")
        self.notify("Building the rotation plan; keep trading, it will pop up when ready.", timeout=6)
        self._build_plan()

    @work(thread=True, exclusive=True, name="plan")
    def _build_plan(self) -> None:
        import rotation
        try:
            portfolio = {"cash": self.rider.account.cash,
                         "positions": [{"ticker": p.ticker, "quantity": p.quantity, "avg_price": p.avg_price}
                                       for p in self.rider.positions]}
            dossier = rotation.build_dossier(portfolio, rotation.load_universe(),
                                             log=lambda m: self.call_from_thread(self.log_line, f"[dim]{m}[/]"))
            plan = rotation.decide(dossier, log=lambda m: self.call_from_thread(self.log_line, f"[dim]{m}[/]"))
            plan["for_session"] = dossier["for_session"]
            config.PLANS_DIR.mkdir(exist_ok=True)
            import json
            (config.PLANS_DIR / f"{dossier['for_session']}.json").write_text(json.dumps(plan, indent=1))
            (config.PLANS_DIR / f"{dossier['for_session']}.dossier.json").write_text(
                json.dumps(dossier, indent=1, default=str))
        except Exception as e:
            self.call_from_thread(self.log_line, f"[{LOSS}]plan failed: {e}[/]")
            return
        self.call_from_thread(self._show_plan, plan)

    def _show_plan(self, plan: dict) -> None:
        self.log_line(f"plan ready: {len(plan.get('buys', []))} buys, regime {plan.get('regime')}")
        self.push_screen(PlanModal(plan), self._buy_from_plan)

    def _buy_from_plan(self, b: Optional[dict]) -> None:
        if not b:
            return
        self.push_screen(OrderModal(b["ticker"], b["limit_price"], b["quantity"], b["limit_price"],
                                    config.WAVE_TRAIL_PCT, live=not getattr(self.broker, "is_paper", True),
                                    note=f"from plan: {b['catalyst'][:80]}"),
                         self._place_from_modal)

    async def action_quit(self) -> None:
        if self.rider is not None:
            self.rider.stop()
            self.rider.save()
        try:
            if self.broker is not None:
                self.broker.disconnect()
        except Exception:
            pass
        self.exit()


# ----------------------------------------------------------------- entry
async def make_broker(sim: bool, log=print):
    if not sim:
        try:
            from ibkr_broker import IBKRBroker
            b = IBKRBroker()
            if await b.connect():
                return b, False
            log(f"TWS / IB Gateway not reachable at {config.IBKR_HOST}:{config.IBKR_PORT}; using the simulator.")
        except RuntimeError as e:
            log(f"{e}; using the simulator.")
    st = {}
    try:
        import json
        st = json.loads(config.WAVE_STATE_PATH.read_text()).get("sim", {})
    except (OSError, ValueError):
        pass
    return SimBroker(starting_cash=float(config.STARTING_EQUITY), state=st), True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="no broker; simulate fills and trailing stops")
    ap.add_argument("--mode", choices=MODES, help="signal | manual | auto (default from .env)")
    ap.add_argument("--watch", help="extra tickers to scan every pass, comma-separated")
    ap.add_argument("--headless", action="store_true",
                    help="no UI: run the rider + Telegram loop in the foreground (for a server)")
    args = ap.parse_args(argv)
    watch = [t.strip().upper() for t in args.watch.split(",") if t.strip()] if args.watch else []

    if args.headless:
        async def headless():
            broker, _ = await make_broker(args.sim)
            rider = WaveRider(broker, Telegram(), mode=args.mode, watchlist=watch)
            rider.on_event = lambda k, p: print(f"[{k}] {p.get('msg', p.get('ticker', ''))}", flush=True)
            await asyncio.gather(rider.run(), rider.telegram_loop())
        asyncio.run(headless())
        return

    WaveDesk(sim=args.sim, mode=args.mode, watch=watch).run()


if __name__ == "__main__":
    main()
