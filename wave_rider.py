"""
The wave rider: turns scanner signals into rides and keeps the rides honest.

    rider = WaveRider(broker, telegram, mode="signal", on_event=print)
    await rider.run()            # scans, manages trails, answers Telegram

A *ride* is one position opened on a wave: entry, trailing stop, high-water
mark, and why it ended. The trailing stop itself lives at the broker (an
IBKR TRAIL order) so a crash here does not leave a naked position; this
class mirrors it, reconciles against real positions, and adds the things the
broker will not do: size by risk, refuse entries the guards veto, exit before
the close if asked, and tell you what happened.

Modes:
    signal  - announce waves, place nothing
    manual  - announce and pre-size; you confirm (terminal key or /buy)
    auto    - enter every qualifying wave with a bracket, within the guards

SimBroker is a tiny in-memory broker with the same interface, so the whole
desk runs without TWS (``terminal.py --sim``). Fills at the last quote,
trailing stops honoured on every refresh. It is a rehearsal, not a backtest.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import config
import risk as risk_mod
import scanner
from ibkr_broker import Account, OpenOrder, Position, Quote
from telegram_bot import HELP, Telegram, esc, fmt_positions, fmt_signal

MODES = ("signal", "manual", "auto")


@dataclass
class Ride:
    ticker: str
    qty: int
    entry: float                     # intended / filled entry
    stop_pct: float
    trail_pct: float
    high_water: float
    opened: str
    status: str = "pending"          # pending | open | closed
    parent_order_id: int = 0
    trail_order_id: int = 0
    exit_price: float = 0.0
    exit_reason: str = ""
    closed: str = ""
    signal: dict = field(default_factory=dict)

    @property
    def trail_stop(self) -> float:
        return risk_mod.trail_price(self.high_water, self.trail_pct)


# ----------------------------------------------------------------- simulator
class SimBroker:
    """Same coroutine interface as IBKRBroker, no network for orders."""
    name = "sim"
    connected = True
    is_paper = True

    def __init__(self, starting_cash: float = 5000.0, state: Optional[dict] = None):
        st = state or {}
        self.cash = float(st.get("cash", starting_cash))
        self.pos: Dict[str, dict] = st.get("positions", {})      # ticker -> {qty, avg}
        self.orders: List[dict] = st.get("orders", [])           # working trails
        self.realized = float(st.get("realized", 0.0))
        self._quotes: Dict[str, Quote] = {}
        self._qts: Dict[str, float] = {}
        self._next_id = int(st.get("next_id", 1))

    def to_dict(self) -> dict:
        return {"cash": self.cash, "positions": self.pos, "orders": self.orders,
                "realized": self.realized, "next_id": self._next_id}

    async def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    async def quote(self, ticker: str) -> Optional[Quote]:
        now = time.time()
        if ticker in self._quotes and now - self._qts.get(ticker, 0) < 30:
            return self._quotes[ticker]
        loop = asyncio.get_running_loop()

        def fetch():
            import yfinance as yf
            fi = yf.Ticker(ticker).fast_info
            last = fi.get("lastPrice") or fi.get("last_price")
            prev = fi.get("previousClose") or fi.get("previous_close")
            return float(last) if last else None, float(prev) if prev else None
        try:
            last, prev = await loop.run_in_executor(None, fetch)
        except Exception:
            last, prev = None, None
        if not last:
            return self._quotes.get(ticker)
        q = Quote(ticker=ticker, last=last, close=prev)
        self._quotes[ticker], self._qts[ticker] = q, now
        return q

    async def account(self) -> Account:
        mv = 0.0
        unreal = 0.0
        for t, p in self.pos.items():
            q = await self.quote(t)
            last = q.last if q else p["avg"]
            mv += last * p["qty"]
            unreal += (last - p["avg"]) * p["qty"]
        return Account(net_liq=self.cash + mv, cash=self.cash, settled_cash=self.cash,
                       buying_power=self.cash, unrealized_pnl=unreal, realized_pnl=self.realized,
                       daily_pnl=unreal + self.realized, account_id="SIM")

    async def positions(self) -> List[Position]:
        out = []
        for t, p in self.pos.items():
            q = await self.quote(t)
            last = q.last if q else p["avg"]
            out.append(Position(ticker=t, quantity=p["qty"], avg_price=p["avg"], last=last,
                                market_value=last * p["qty"], unrealized_pnl=(last - p["avg"]) * p["qty"]))
        return out

    async def open_orders(self) -> List[OpenOrder]:
        return [OpenOrder(order_id=o["id"], ticker=o["ticker"], side="SELL", order_type="TRAIL",
                          quantity=o["qty"], limit=None, stop=o["stop"], trail_pct=o["trail_pct"],
                          status="Submitted") for o in self.orders]

    async def bars(self, ticker: str, duration: str = "2 D", size: str = "1 min"):
        raise RuntimeError("sim broker has no bars; scanner falls back to Yahoo")

    async def scanner(self, *a, **k) -> List[str]:
        return []

    async def _fill_buy(self, ticker: str, qty: int, limit: Optional[float]):
        q = await self.quote(ticker)
        if q is None:
            raise RuntimeError(f"no quote for {ticker}")
        px = q.last
        if limit is not None and px > limit:
            px = limit                    # pretend it fills at the limit a moment later
        cost = px * qty
        if cost > self.cash + 1e-9:
            raise RuntimeError(f"sim: ${cost:,.0f} needed, ${self.cash:,.0f} cash")
        self.cash -= cost
        p = self.pos.get(ticker, {"qty": 0, "avg": 0.0})
        tot = p["qty"] + qty
        p["avg"] = (p["avg"] * p["qty"] + px * qty) / tot
        p["qty"] = tot
        self.pos[ticker] = p
        return px

    async def buy_with_trail(self, ticker: str, qty: int, limit: Optional[float], trail_pct: float, **_):
        px = await self._fill_buy(ticker, qty, limit)
        oid = self._next_id
        self._next_id += 1
        self.orders.append({"id": oid, "ticker": ticker, "qty": qty, "trail_pct": trail_pct,
                            "high": px, "stop": risk_mod.trail_price(px, trail_pct)})
        return _SimTrade(oid - 1, px), _SimTrade(oid, px)

    async def attach_trail(self, ticker: str, qty: int, trail_pct: float):
        q = await self.quote(ticker)
        px = q.last if q else self.pos.get(ticker, {}).get("avg", 0)
        oid = self._next_id
        self._next_id += 1
        self.orders.append({"id": oid, "ticker": ticker, "qty": qty, "trail_pct": trail_pct,
                            "high": px, "stop": risk_mod.trail_price(px, trail_pct)})
        return _SimTrade(oid, px)

    async def order(self, ticker: str, side: str, qty: int, limit: Optional[float] = None, **_):
        if side == "BUY":
            px = await self._fill_buy(ticker, qty, limit)
        else:
            px = await self._fill_sell(ticker, qty)
        oid = self._next_id
        self._next_id += 1
        return _SimTrade(oid, px)

    async def _fill_sell(self, ticker: str, qty: int) -> float:
        p = self.pos.get(ticker)
        if not p or p["qty"] <= 0:
            raise RuntimeError(f"sim: no {ticker} to sell")
        q = await self.quote(ticker)
        px = q.last if q else p["avg"]
        qty = min(qty, p["qty"])
        self.cash += px * qty
        self.realized += (px - p["avg"]) * qty
        p["qty"] -= qty
        if p["qty"] <= 0:
            del self.pos[ticker]
        return px

    async def cancel(self, ticker: Optional[str] = None) -> int:
        before = len(self.orders)
        self.orders = [o for o in self.orders if ticker is not None and o["ticker"] != ticker]
        return before - len(self.orders)

    async def flatten(self, ticker: str):
        await self.cancel(ticker)
        if ticker in self.pos:
            return await self.order(ticker, "SELL", int(self.pos[ticker]["qty"]))
        return None

    async def fills_today(self) -> List[dict]:
        return []

    async def tick(self) -> List[dict]:
        """Advance trailing stops against fresh quotes; returns executed exits."""
        done = []
        for o in list(self.orders):
            q = await self.quote(o["ticker"])
            if q is None:
                continue
            if q.last > o["high"]:
                o["high"] = q.last
                o["stop"] = risk_mod.trail_price(q.last, o["trail_pct"])
            if q.last <= o["stop"] and o["ticker"] in self.pos:
                px = await self._fill_sell(o["ticker"], o["qty"])
                self.orders.remove(o)
                done.append({"ticker": o["ticker"], "price": px, "qty": o["qty"], "reason": "trailing stop"})
        return done


class _SimTrade:
    def __init__(self, oid: int, px: float):
        self.order = type("O", (), {"orderId": oid})()
        self.fill_price = px


# ----------------------------------------------------------------- rider
class WaveRider:
    def __init__(self, broker, telegram: Optional[Telegram] = None, mode: Optional[str] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 watchlist: Optional[List[str]] = None):
        self.broker = broker
        self.tg = telegram or Telegram()
        self.mode = (mode or config.WAVE_MODE).lower()
        if self.mode not in MODES:
            self.mode = "signal"
        self.on_event = on_event or (lambda kind, payload: None)
        self.watchlist: List[str] = [w.upper() for w in (watchlist or [])]

        st = self._load_state()
        self.risk_state = risk_mod.RiskState.load()
        self.guard = risk_mod.RiskGuard(self.risk_state)
        self.rides: Dict[str, Ride] = {t: Ride(**r) for t, r in st.get("rides", {}).items()}
        self.paused: bool = bool(st.get("paused", False))
        self.signals: Dict[str, scanner.Signal] = {}
        self.seen: Dict[str, float] = {}             # ticker -> last announce epoch
        self.account: Account = Account()
        self.positions: List[Position] = []
        self.orders: List[OpenOrder] = []
        self.last_scan: Optional[datetime] = None
        self.scanning = False
        self._stop = False

    # ------------------------------------------------------------ state
    def _load_state(self) -> dict:
        try:
            return json.loads(config.WAVE_STATE_PATH.read_text())
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        st = self._load_state()
        st["risk"] = self.risk_state.to_dict()
        st["rides"] = {t: asdict(r) for t, r in self.rides.items()}
        st["paused"] = self.paused
        st["mode"] = self.mode
        if isinstance(self.broker, SimBroker):
            st["sim"] = self.broker.to_dict()
        try:
            config.WAVE_STATE_PATH.write_text(json.dumps(st, indent=1, default=str))
        except OSError:
            pass

    def emit(self, kind: str, **payload) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:
            pass

    def log(self, msg: str) -> None:
        self.emit("log", msg=msg)

    # ------------------------------------------------------------ refresh
    async def refresh(self) -> None:
        try:
            self.account = await self.broker.account()
            self.positions = await self.broker.positions()
            self.orders = await self.broker.open_orders()
        except Exception as e:
            self.log(f"refresh failed: {e}")
            return
        held = {p.ticker: p for p in self.positions}
        for t, r in list(self.rides.items()):
            if r.status == "closed":
                continue
            p = held.get(t)
            if p:
                if r.status == "pending":
                    r.status, r.entry = "open", p.avg_price or r.entry
                    r.high_water = max(r.high_water, r.entry)
                    self.log(f"{t}: filled {p.quantity:.0f} @ {p.avg_price:.2f}, trail {r.trail_pct:.0f}%")
                    await self.tg.send_async(f"✅ <b>{esc(t)}</b> filled {p.quantity:.0f} @ {p.avg_price:.2f}, "
                                             f"trail {r.trail_pct:.0f}% (stop {r.trail_stop:.2f})")
                if p.last > r.high_water:
                    r.high_water = p.last
            elif r.status == "open":
                r.status, r.closed = "closed", datetime.now(timezone.utc).isoformat(timespec="seconds")
                r.exit_price = r.exit_price or r.trail_stop
                r.exit_reason = r.exit_reason or "stopped or sold"
                pnl = (r.exit_price - r.entry) * r.qty
                opened_today = r.opened[:10] == datetime.now(timezone.utc).isoformat()[:10]
                self.guard.note_exit(t, r.exit_price * r.qty, pnl, opened_today)
                self.log(f"{t}: ride over ({r.exit_reason}) ≈ {pnl:+.0f}")
                await self.tg.send_async(f"🏁 <b>{esc(t)}</b> ride over: {esc(r.exit_reason)}, "
                                         f"≈ {pnl:+.0f} ({(r.exit_price / r.entry - 1) * 100:+.1f}%)")
            elif r.status == "pending" and not any(o.ticker == t for o in self.orders):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(r.opened)).total_seconds()
                if age > 900:
                    r.status, r.exit_reason = "closed", "entry never filled"
                    self.log(f"{t}: entry order gone without a fill; ride dropped")
        self.save()
        self.emit("refresh")

    # ------------------------------------------------------------ scanning
    async def scan_once(self) -> List[scanner.Signal]:
        if self.scanning:
            return []
        self.scanning = True
        try:
            sigs = await scanner.scan(self.broker, extra=self.watchlist, log=self.log)
        except Exception as e:
            self.log(f"scan failed: {e}")
            sigs = []
        finally:
            self.scanning = False
        self.last_scan = datetime.now(timezone.utc)
        spendable = self.guard.spendable_cash(self.account.cash, self.account.settled_cash)
        held = {p.ticker for p in self.positions}
        now = time.time()
        fresh = []
        for s in sigs:
            s.suggested_qty = risk_mod.size_by_risk(self.account.net_liq, spendable, s.price, s.stop_pct)
            self.signals[s.ticker] = s
            if s.ticker in held:
                continue
            if now - self.seen.get(s.ticker, 0) < config.WAVE_SIGNAL_COOLDOWN_MIN * 60:
                continue
            self.seen[s.ticker] = now
            fresh.append(s)
        # drop stale signals from the board after a couple of hours
        for t in list(self.signals):
            if now - self.seen.get(t, now) > 7200 and t not in {s.ticker for s in sigs}:
                del self.signals[t]
        for s in fresh:
            self.emit("signal", signal=s)
            tag = {"signal": "🌊", "manual": "🌊 (reply /buy to ride)", "auto": "🌊 auto-entering"}[self.mode]
            await self.tg.send_async(f"{tag}\n{fmt_signal(s)}")
            if self.mode == "auto":
                await self.enter(s.ticker)
        self.emit("scan", count=len(sigs), fresh=len(fresh))
        return sigs

    # ------------------------------------------------------------ actions
    async def enter(self, ticker: str, qty: Optional[int] = None, limit: Optional[float] = None,
                    trail_pct: Optional[float] = None, force: bool = False) -> Optional[Ride]:
        ticker = ticker.upper()
        s = self.signals.get(ticker)
        open_n = sum(1 for r in self.rides.values() if r.status != "closed") or len(self.positions)
        ok, why = self.guard.can_enter(open_positions=open_n, equity=self.account.net_liq,
                                       cash=self.account.cash, settled_cash=self.account.settled_cash,
                                       day_trades_remaining=self.account.day_trades_remaining,
                                       paused=self.paused and not force)
        if not ok and not force:
            self.log(f"{ticker}: entry refused: {why}")
            await self.tg.send_async(f"⛔ {esc(ticker)} not entered: {esc(why)}")
            return None
        price = s.price if s else None
        if price is None:
            q = await self.broker.quote(ticker)
            price = q.last if q else None
        if not price:
            self.log(f"{ticker}: no price, cannot size")
            return None
        stop_pct = s.stop_pct if s else config.WAVE_STOP_PCT
        trail_pct = trail_pct if trail_pct is not None else (s.trail_pct if s else config.WAVE_TRAIL_PCT)
        if qty is None:
            spendable = self.guard.spendable_cash(self.account.cash, self.account.settled_cash)
            qty = risk_mod.size_by_risk(self.account.net_liq, spendable, price, stop_pct)
        if qty <= 0:
            self.log(f"{ticker}: sized to 0 shares (risk {config.WAVE_RISK_PCT}% / stop {stop_pct}%)")
            await self.tg.send_async(f"⛔ {esc(ticker)}: sized to 0 shares; cash or risk budget too small")
            return None
        if limit is None:
            limit = round(price * (1 + config.WAVE_ENTRY_BUFFER_PCT / 100), 2)
        try:
            t_parent, t_trail = await self.broker.buy_with_trail(ticker, qty, limit, trail_pct)
        except Exception as e:
            self.log(f"{ticker}: order failed: {e}")
            await self.tg.send_async(f"⛔ {esc(ticker)} order failed: {esc(e)}")
            return None
        ride = Ride(ticker=ticker, qty=qty, entry=limit, stop_pct=stop_pct, trail_pct=trail_pct,
                    high_water=limit, opened=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    parent_order_id=getattr(t_parent.order, "orderId", 0),
                    trail_order_id=getattr(t_trail.order, "orderId", 0),
                    signal=s.to_dict() if s else {})
        self.rides[ticker] = ride
        self.guard.note_entry(ticker)
        self.save()
        self.log(f"{ticker}: BUY {qty} lmt {limit:.2f} + trail {trail_pct:.0f}% sent ({self.broker.name})")
        await self.tg.send_async(f"🛒 <b>{esc(ticker)}</b> BUY {qty} @ {limit:.2f} limit, trail {trail_pct:.0f}% "
                                 f"({'paper' if getattr(self.broker, 'is_paper', True) else 'LIVE'})")
        self.emit("order", ticker=ticker)
        await self.refresh()
        return ride

    async def flat(self, ticker: str, reason: str = "manual flat") -> bool:
        ticker = ticker.upper()
        try:
            await self.broker.flatten(ticker)
        except Exception as e:
            self.log(f"{ticker}: flatten failed: {e}")
            return False
        r = self.rides.get(ticker)
        if r and r.status != "closed":
            q = await self.broker.quote(ticker)
            r.exit_price = q.last if q else r.trail_stop
            r.exit_reason = reason
        self.log(f"{ticker}: flattened ({reason})")
        await self.tg.send_async(f"🏳️ <b>{esc(ticker)}</b> flattened: {esc(reason)}")
        await self.refresh()
        return True

    async def trail(self, ticker: str, trail_pct: float) -> bool:
        ticker = ticker.upper()
        pos = next((p for p in self.positions if p.ticker == ticker), None)
        if not pos:
            self.log(f"{ticker}: no position to trail")
            return False
        try:
            await self.broker.cancel(ticker)
            t = await self.broker.attach_trail(ticker, int(pos.quantity), trail_pct)
        except Exception as e:
            self.log(f"{ticker}: trail failed: {e}")
            return False
        r = self.rides.get(ticker) or Ride(ticker=ticker, qty=int(pos.quantity), entry=pos.avg_price,
                                           stop_pct=config.WAVE_STOP_PCT, trail_pct=trail_pct,
                                           high_water=max(pos.last, pos.avg_price),
                                           opened=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                           status="open")
        r.trail_pct, r.status = trail_pct, "open"
        r.high_water = max(r.high_water, pos.last)
        r.trail_order_id = getattr(t.order, "orderId", 0)
        self.rides[ticker] = r
        self.save()
        self.log(f"{ticker}: trailing stop {trail_pct:.0f}% from {r.high_water:.2f} (stop {r.trail_stop:.2f})")
        await self.refresh()
        return True

    def set_mode(self, mode: str) -> str:
        mode = mode.lower()
        if mode in MODES:
            self.mode = mode
            self.save()
            self.log(f"mode -> {mode}")
            self.emit("mode", mode=mode)
        return self.mode

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.save()
        self.log("paused: no new entries" if paused else "resumed")
        self.emit("mode", mode=self.mode)

    def status_text(self) -> str:
        a = self.account
        open_rides = [r for r in self.rides.values() if r.status != "closed"]
        return (f"mode {self.mode}{' (paused)' if self.paused else ''} · {self.broker.name}"
                f"{' paper' if getattr(self.broker, 'is_paper', True) else ' LIVE'}\n"
                f"equity {a.net_liq:,.0f} · cash {a.cash:,.0f} · day P&L {a.daily_pnl:+,.0f}\n"
                f"rides open {len(open_rides)} · realised today {self.risk_state.realized_today:+,.0f}"
                f"{' · HALTED: ' + self.risk_state.halted_reason if self.risk_state.halted_reason else ''}")

    # ------------------------------------------------------------ loops
    async def run(self) -> None:
        """Main loop: refresh often, scan on the interval while the market is open."""
        await self.refresh()
        last_scan = 0.0
        while not self._stop:
            try:
                now = time.time()
                open_ = scanner.market_open()
                interval = config.WAVE_SCAN_INTERVAL if open_ else 300
                if now - last_scan >= interval and (open_ or not self.last_scan):
                    last_scan = now
                    await self.scan_once()
                if isinstance(self.broker, SimBroker):
                    for ex in await self.broker.tick():
                        r = self.rides.get(ex["ticker"])
                        if r:
                            r.exit_price, r.exit_reason = ex["price"], ex["reason"]
                if open_ and config.WAVE_EXIT_BEFORE_CLOSE_MIN and \
                        scanner.minutes_to_close() <= config.WAVE_EXIT_BEFORE_CLOSE_MIN:
                    for r in list(self.rides.values()):
                        if r.status == "open":
                            await self.flat(r.ticker, "exit before close")
                await self.refresh()
            except Exception as e:
                self.log(f"loop error: {e}")
            await asyncio.sleep(10 if scanner.market_open() else 60)

    def stop(self) -> None:
        self._stop = True

    async def telegram_loop(self) -> None:
        if not self.tg.enabled:
            return
        await self.tg.send_async(f"Desk online.\n{esc(self.status_text())}\n\n{HELP}", silent=True)
        async for cmd, args in self.tg.commands():
            try:
                await self.handle_command(cmd, args)
            except Exception as e:
                await self.tg.send_async(f"error: {esc(e)}")

    async def handle_command(self, cmd: str, args: List[str]) -> None:
        tg = self.tg
        if cmd in ("start", "help"):
            await tg.send_async(HELP)
        elif cmd == "status":
            await tg.send_async(esc(self.status_text()))
        elif cmd == "signals":
            sigs = sorted(self.signals.values(), key=lambda s: s.score, reverse=True)[:8]
            await tg.send_async("\n\n".join(fmt_signal(s) for s in sigs) if sigs else "No waves right now.")
        elif cmd == "positions":
            await tg.send_async(fmt_positions(self.positions, {t: r for t, r in self.rides.items()
                                                               if r.status == "open"}))
        elif cmd == "buy" and args:
            qty = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
            await self.enter(args[0], qty=qty, force=True)
        elif cmd == "flat" and args:
            await self.flat(args[0], "telegram /flat")
        elif cmd == "mode" and args:
            await tg.send_async(f"mode: {self.set_mode(args[0])}")
        elif cmd == "pause":
            self.set_paused(True)
            await tg.send_async("Paused: no new entries until /resume.")
        elif cmd == "resume":
            self.set_paused(False)
            self.risk_state.halted_reason = ""
            await tg.send_async("Resumed.")
        elif cmd == "scan":
            sigs = await self.scan_once()
            await tg.send_async(f"scanned: {len(sigs)} waves")
        else:
            await tg.send_async(f"unknown command /{esc(cmd)}\n\n{HELP}")


# ----------------------------------------------------------------- broker factory
async def make_broker(sim: bool, log=print):
    """IBKR via TWS/Gateway when reachable (and not --sim), else the simulator. Returns (broker, fell_back)."""
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


