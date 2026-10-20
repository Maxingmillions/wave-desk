"""
Position sizing and the guards that keep a small account alive.

Pure functions plus one small state object; nothing here touches a broker.

    qty = size_by_risk(equity=4400, cash=900, price=6.20, stop_pct=6)
    ok, why = RiskGuard(...).can_enter(...)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import config


def size_by_risk(equity: float, cash: float, price: float, stop_pct: float,
                 risk_pct: Optional[float] = None, max_position_pct: Optional[float] = None,
                 min_order_usd: float = 100.0) -> int:
    """
    Shares such that a stop-out loses ~risk_pct of equity, capped by the
    per-name weight and by cash. Whole shares; 0 if the result is below the
    minimum order size (a $40 position is not worth the spread).
    """
    risk_pct = config.WAVE_RISK_PCT if risk_pct is None else risk_pct
    max_position_pct = config.WAVE_MAX_POSITION_PCT if max_position_pct is None else max_position_pct
    if price <= 0 or stop_pct <= 0 or equity <= 0:
        return 0
    risk_usd = equity * risk_pct / 100
    by_risk = risk_usd / (price * stop_pct / 100)
    by_weight = equity * max_position_pct / 100 / price
    by_cash = cash / price
    qty = int(math.floor(min(by_risk, by_weight, by_cash)))
    return qty if qty * price >= min_order_usd else 0


def trail_price(high_water: float, trail_pct: float) -> float:
    return round(high_water * (1 - trail_pct / 100), 4)


@dataclass
class DayTrade:
    ticker: str
    day: str          # ISO date


@dataclass
class RiskState:
    """Persisted between runs: today's realised P&L, sales (for settlement), day trades."""
    day: str = field(default_factory=lambda: date.today().isoformat())
    realized_today: float = 0.0
    unsettled_proceeds: float = 0.0           # cash-account: sales not yet settled (T+1)
    unsettled_from: str = ""
    day_trades: List[DayTrade] = field(default_factory=list)
    halted_reason: str = ""

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RiskState":
        path = path or config.WAVE_STATE_PATH
        st = cls()
        try:
            raw = json.loads(path.read_text()).get("risk", {})
            st.day = raw.get("day", st.day)
            st.realized_today = raw.get("realized_today", 0.0)
            st.unsettled_proceeds = raw.get("unsettled_proceeds", 0.0)
            st.unsettled_from = raw.get("unsettled_from", "")
            st.day_trades = [DayTrade(**d) for d in raw.get("day_trades", [])]
            st.halted_reason = raw.get("halted_reason", "")
        except (OSError, ValueError, TypeError):
            pass
        st.roll(date.today())
        return st

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def roll(self, today: date) -> None:
        """New day: reset daily counters, settle yesterday's sales, age out day trades."""
        if self.day != today.isoformat():
            self.day = today.isoformat()
            self.realized_today = 0.0
            self.halted_reason = ""
        if self.unsettled_from:
            try:
                sold = date.fromisoformat(self.unsettled_from)
            except ValueError:
                sold = today
            if _business_days_between(sold, today) >= 1:      # T+1 settlement
                self.unsettled_proceeds = 0.0
                self.unsettled_from = ""
        cutoff = today - timedelta(days=7)        # 5 business days, generously
        self.day_trades = [t for t in self.day_trades if date.fromisoformat(t.day) >= cutoff]

    def record_sale(self, proceeds: float, realized: float, was_day_trade: bool, ticker: str) -> None:
        self.realized_today += realized
        self.unsettled_proceeds += proceeds
        self.unsettled_from = self.day
        if was_day_trade:
            self.day_trades.append(DayTrade(ticker=ticker, day=self.day))


def _business_days_between(a: date, b: date) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


class RiskGuard:
    """Answers "may I open another position right now?" with a reason."""

    def __init__(self, state: RiskState, account_type: Optional[str] = None,
                 max_open: Optional[int] = None, max_daily_loss_pct: Optional[float] = None):
        self.state = state
        self.account_type = (account_type or config.ACCOUNT_TYPE).lower()
        self.max_open = config.WAVE_MAX_OPEN if max_open is None else max_open
        self.max_daily_loss_pct = (config.WAVE_MAX_DAILY_LOSS_PCT if max_daily_loss_pct is None
                                   else max_daily_loss_pct)

    def spendable_cash(self, cash: float, settled_cash: Optional[float] = None) -> float:
        """In a cash account only settled funds may buy without a good-faith violation."""
        if self.account_type != "cash":
            return max(cash, 0.0)
        if settled_cash is not None:                     # broker told us; trust it
            return max(min(cash, settled_cash), 0.0)
        return max(cash - self.state.unsettled_proceeds, 0.0)

    def can_enter(self, *, open_positions: int, equity: float, cash: float,
                  settled_cash: Optional[float] = None, day_trades_remaining: Optional[int] = None,
                  paused: bool = False) -> Tuple[bool, str]:
        st = self.state
        if paused:
            return False, "paused"
        if st.halted_reason:
            return False, st.halted_reason
        if equity > 0 and st.realized_today < 0 and -st.realized_today / equity * 100 >= self.max_daily_loss_pct:
            st.halted_reason = (f"daily loss limit hit ({st.realized_today:+.0f} = "
                                f"{-st.realized_today / equity * 100:.1f}% of equity)")
            return False, st.halted_reason
        if open_positions >= self.max_open:
            return False, f"already {open_positions} open positions (limit {self.max_open})"
        if self.account_type == "margin" and equity < 25_000:
            remaining = day_trades_remaining
            if remaining is None:
                remaining = 3 - len(st.day_trades)
            if remaining <= 0:
                return False, "no day trades left (PDT rule, account under $25k)"
        if self.spendable_cash(cash, settled_cash) < config.ROTATION_MIN_ORDER_USD:
            return False, (f"only ${self.spendable_cash(cash, settled_cash):,.0f} settled cash "
                           f"(unsettled ${st.unsettled_proceeds:,.0f})")
        return True, "ok"

    def note_entry(self, ticker: str) -> None:
        pass   # entries alone do not consume anything the guard tracks

    def note_exit(self, ticker: str, proceeds: float, realized: float, opened_today: bool) -> None:
        self.state.record_sale(proceeds, realized, was_day_trade=opened_today, ticker=ticker)
