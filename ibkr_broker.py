"""
Interactive Brokers through TWS / IB Gateway running on this machine.

All methods are coroutines so they can live inside the terminal's event loop.
From a plain script: ``asyncio.run(main())``.

    b = IBKRBroker()
    await b.connect()
    acct = await b.account()
    await b.buy_with_trail("ABCD", qty=120, limit=6.25, trail_pct=8)

Orders placed here are real orders on whichever port you connected to.
7497 / 4002 are the paper ports; 7496 / 4001 are live.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

import config

try:
    from ib_async import (IB, LimitOrder, MarketOrder, Order, ScannerSubscription, Stock,
                          TagValue, util)
except ImportError:  # pragma: no cover
    IB = None


@dataclass
class Account:
    net_liq: float = 0.0
    cash: float = 0.0
    settled_cash: Optional[float] = None
    buying_power: float = 0.0
    daily_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    day_trades_remaining: Optional[int] = None
    account_id: str = ""


@dataclass
class Position:
    ticker: str
    quantity: float
    avg_price: float
    last: float
    market_value: float
    unrealized_pnl: float

    @property
    def pnl_pct(self) -> Optional[float]:
        return (self.last / self.avg_price - 1) * 100 if self.avg_price else None


@dataclass
class OpenOrder:
    order_id: int
    ticker: str
    side: str
    order_type: str
    quantity: float
    limit: Optional[float]
    stop: Optional[float]
    trail_pct: Optional[float]
    status: str
    filled: float = 0.0
    parent_id: int = 0


@dataclass
class Quote:
    ticker: str
    last: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    close: Optional[float] = None


class IBKRBroker:
    name = "ibkr"

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 client_id: Optional[int] = None, account: Optional[str] = None):
        if IB is None:
            raise RuntimeError("ib_async is not installed: pip install ib_async")
        self.host = host or config.IBKR_HOST
        self.port = port or config.IBKR_PORT
        self.client_id = client_id or config.IBKR_CLIENT_ID
        self.account_id = account or config.IBKR_ACCOUNT
        self.ib = IB()
        self._contracts: Dict[str, Stock] = {}
        self._pnl = None

    # ------------------------------------------------------------ connection
    @property
    def connected(self) -> bool:
        return self.ib.isConnected()

    @property
    def is_paper(self) -> bool:
        return self.port in (7497, 4002)

    async def connect(self, timeout: float = 8.0) -> bool:
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=timeout)
        except Exception:
            return False
        if not self.account_id:
            accts = self.ib.managedAccounts()
            self.account_id = accts[0] if accts else ""
        self.ib.reqMarketDataType(1 if config.IBKR_MARKET_DATA == "live" else 3)
        try:
            self._pnl = self.ib.reqPnL(self.account_id)
        except Exception:
            self._pnl = None
        return True

    def disconnect(self) -> None:
        if self.connected:
            self.ib.disconnect()

    # ------------------------------------------------------------ account
    async def account(self) -> Account:
        vals = await self.ib.accountSummaryAsync(self.account_id)
        m: Dict[str, str] = {}
        for v in vals:
            if v.currency in ("USD", "BASE", ""):
                m[v.tag] = v.value

        def f(tag, default=0.0):
            try:
                return float(m.get(tag, default))
            except (TypeError, ValueError):
                return default

        a = Account(
            net_liq=f("NetLiquidation"), cash=f("TotalCashValue"), buying_power=f("BuyingPower"),
            unrealized_pnl=f("UnrealizedPnL"), realized_pnl=f("RealizedPnL"),
            account_id=self.account_id,
        )
        if "SettledCash" in m:
            a.settled_cash = f("SettledCash")
        if "DayTradesRemaining" in m:
            try:
                dtr = int(float(m["DayTradesRemaining"]))
                a.day_trades_remaining = None if dtr < 0 else dtr    # -1 = unlimited
            except ValueError:
                pass
        if self._pnl is not None and not math.isnan(self._pnl.dailyPnL or math.nan):
            a.daily_pnl = float(self._pnl.dailyPnL)
        return a

    async def positions(self) -> List[Position]:
        out = []
        for item in self.ib.portfolio(self.account_id):
            if item.contract.secType != "STK" or not item.position:
                continue
            out.append(Position(
                ticker=item.contract.symbol, quantity=float(item.position),
                avg_price=float(item.averageCost), last=float(item.marketPrice or 0),
                market_value=float(item.marketValue), unrealized_pnl=float(item.unrealizedPNL or 0),
            ))
        return out

    async def open_orders(self) -> List[OpenOrder]:
        out = []
        for t in self.ib.openTrades():
            o, c = t.order, t.contract
            if c.secType != "STK":
                continue
            out.append(OpenOrder(
                order_id=o.orderId, ticker=c.symbol, side=o.action, order_type=o.orderType,
                quantity=float(o.totalQuantity),
                limit=_num(o.lmtPrice), stop=_num(o.auxPrice) or _num(o.trailStopPrice),
                trail_pct=_num(o.trailingPercent), status=t.orderStatus.status,
                filled=float(t.orderStatus.filled or 0), parent_id=o.parentId or 0,
            ))
        return out

    # ------------------------------------------------------------ market data
    async def _contract(self, ticker: str) -> Stock:
        if ticker not in self._contracts:
            c = Stock(ticker, "SMART", "USD")
            await self.ib.qualifyContractsAsync(c)
            self._contracts[ticker] = c
        return self._contracts[ticker]

    async def quote(self, ticker: str) -> Optional[Quote]:
        try:
            c = await self._contract(ticker)
            (tk,) = await self.ib.reqTickersAsync(c)
        except Exception:
            return None
        last = tk.last if _num(tk.last) else tk.marketPrice()
        if not _num(last):
            return None
        return Quote(ticker=ticker, last=float(last), bid=_num(tk.bid), ask=_num(tk.ask),
                     close=_num(tk.close))

    async def bars(self, ticker: str, duration: str = "2 D", size: str = "1 min",
                   rth: bool = True) -> pd.DataFrame:
        c = await self._contract(ticker)
        bars = await self.ib.reqHistoricalDataAsync(
            c, endDateTime="", durationStr=duration, barSizeSetting=size,
            whatToShow="TRADES", useRTH=rth, formatDate=2)
        df = util.df(bars)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"date": "Datetime", "open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"}).set_index("Datetime")
        return df[["Open", "High", "Low", "Close", "Volume"]]

    async def scanner(self, scan_code: str = "TOP_PERC_GAIN", above_price: float = 1.0,
                      below_price: float = 20.0, above_volume: int = 300_000,
                      mcap_below_m: Optional[float] = 3000.0, rows: int = 50) -> List[str]:
        """Symbols from IBKR's server-side scanner (US stocks, major exchanges)."""
        sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR",
                                  scanCode=scan_code, numberOfRows=rows,
                                  abovePrice=above_price, belowPrice=below_price,
                                  aboveVolume=above_volume)
        if mcap_below_m:
            sub.marketCapBelow = mcap_below_m
        filt = [TagValue("usdPriceAbove", str(above_price)), TagValue("usdPriceBelow", str(below_price))]
        try:
            data = await self.ib.reqScannerDataAsync(sub, [], filt)
        except Exception:
            return []
        out = []
        for d in data:
            sym = d.contractDetails.contract.symbol
            if sym.isalpha() and sym not in out:
                out.append(sym)
        return out

    # ------------------------------------------------------------ orders
    async def buy_with_trail(self, ticker: str, qty: int, limit: Optional[float],
                             trail_pct: float, tif: str = "DAY", outside_rth: bool = False):
        """
        Entry (limit, or market when limit is None) with an IBKR-native trailing
        stop attached as a child. The trail lives on IBKR's servers, so it keeps
        working if this program dies.
        """
        c = await self._contract(ticker)
        parent = (LimitOrder("BUY", qty, round(limit, 2)) if limit else MarketOrder("BUY", qty))
        parent.orderId = self.ib.client.getReqId()
        parent.tif = tif
        parent.outsideRth = outside_rth
        parent.transmit = False
        trail = Order(action="SELL", orderType="TRAIL", totalQuantity=qty,
                      trailingPercent=round(trail_pct, 2), tif="GTC",
                      parentId=parent.orderId, orderId=self.ib.client.getReqId(), transmit=True)
        t_parent = self.ib.placeOrder(c, parent)
        t_trail = self.ib.placeOrder(c, trail)
        return t_parent, t_trail

    async def attach_trail(self, ticker: str, qty: int, trail_pct: float):
        """Trailing stop on shares you already hold."""
        c = await self._contract(ticker)
        o = Order(action="SELL", orderType="TRAIL", totalQuantity=qty,
                  trailingPercent=round(trail_pct, 2), tif="GTC")
        return self.ib.placeOrder(c, o)

    async def order(self, ticker: str, side: str, qty: int, limit: Optional[float] = None,
                    tif: str = "DAY", outside_rth: bool = False):
        c = await self._contract(ticker)
        o = (LimitOrder(side, qty, round(limit, 2)) if limit else MarketOrder(side, qty))
        o.tif = tif
        o.outsideRth = outside_rth
        return self.ib.placeOrder(c, o)

    async def cancel(self, ticker: Optional[str] = None) -> int:
        n = 0
        for t in list(self.ib.openTrades()):
            if ticker is None or t.contract.symbol == ticker:
                self.ib.cancelOrder(t.order)
                n += 1
        return n

    async def flatten(self, ticker: str) -> Optional[object]:
        """Cancel working orders on the name, then sell the whole position at market."""
        await self.cancel(ticker)
        await asyncio.sleep(0.5)
        pos = next((p for p in await self.positions() if p.ticker == ticker), None)
        if not pos or pos.quantity <= 0:
            return None
        return await self.order(ticker, "SELL", int(pos.quantity))

    async def fills_today(self) -> List[dict]:
        out = []
        for f in self.ib.fills():
            e = f.execution
            out.append({"ticker": f.contract.symbol, "side": "BUY" if e.side == "BOT" else "SELL",
                        "qty": float(e.shares), "price": float(e.price), "time": e.time,
                        "order_id": e.orderId})
        return out


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or v >= 1.7e308:        # IB's UNSET_DOUBLE
        return None
    return v
