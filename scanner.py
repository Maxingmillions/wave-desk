"""
Small-cap wave scanner: low-priced names with a volume influx and a breakout.

    sigs = await scan(broker)            # broker may be None -> Yahoo-only mode

A wave, for this desk, is a stock between WAVE_MIN_PRICE and WAVE_MAX_PRICE
that is (a) trading at several times its normal volume for this point in the
session, (b) up on the day, and (c) pushing through a level: the 20-day high,
the high of the day, or the opening range. A news influx raises the score; a
one-day +60% move lowers it (that is a lottery ticket, not a wave).

Candidates come from IBKR's server-side scanner when connected (top %
gainers, hot by volume, most active in the price band) and from Yahoo's
day-gainers / small-cap-gainers / most-actives screeners otherwise. Bars are
1-minute from IBKR or 5-minute from Yahoo.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

import config
import market_data as md
import newsfeed

ET = ZoneInfo("America/New_York")
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
BREAKOUT_LABEL = {"20d_high": "20d high", "hod": "high of day", "orb": "opening range", "": "volume"}


@dataclass
class Signal:
    ticker: str
    name: str
    price: float
    change_pct: float
    rel_vol: float
    dollar_vol_m: float
    breakout: str                 # "20d_high" | "hod" | "orb" | ""
    news_count: int
    headline: str
    score: float
    atr_pct: float
    stop_pct: float
    trail_pct: float
    source: str                   # "ibkr" | "yahoo"
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    suggested_qty: int = 0
    entry_limit: float = 0.0
    headlines: List[dict] = field(default_factory=list)

    @property
    def breakout_label(self) -> str:
        return BREAKOUT_LABEL.get(self.breakout, self.breakout)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- session clock
def now_et() -> datetime:
    return datetime.now(ET)


def session_fraction(now: Optional[datetime] = None) -> float:
    """0 before the open, 1 after the close, linear in between (regular hours)."""
    now = now or now_et()
    if now.weekday() >= 5:
        return 1.0
    o = now.replace(hour=OPEN.hour, minute=OPEN.minute, second=0, microsecond=0)
    c = now.replace(hour=CLOSE.hour, minute=CLOSE.minute, second=0, microsecond=0)
    if now <= o:
        return 0.0
    if now >= c:
        return 1.0
    return (now - o).total_seconds() / (c - o).total_seconds()


def market_open(now: Optional[datetime] = None) -> bool:
    now = now or now_et()
    return now.weekday() < 5 and OPEN <= now.time() < CLOSE


def minutes_to_close(now: Optional[datetime] = None) -> float:
    now = now or now_et()
    c = now.replace(hour=CLOSE.hour, minute=CLOSE.minute, second=0, microsecond=0)
    return (c - now).total_seconds() / 60


# ----------------------------------------------------------------- features
def wave_features(intraday: pd.DataFrame, daily: pd.DataFrame, now: Optional[datetime] = None) -> Optional[dict]:
    """
    intraday: today's (and maybe yesterday's) 1m/5m bars, tz-aware index.
    daily:    ~30 daily bars ending yesterday or today.
    """
    if intraday is None or intraday.empty or daily is None or len(daily) < 10:
        return None
    now = now or now_et()
    idx = intraday.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    intraday = intraday.copy()
    intraday.index = idx.tz_convert(ET)
    today = now.date()
    day_bars = intraday[intraday.index.date == today]
    if day_bars.empty:                         # pre-market / Yahoo lag: use the latest session
        last_day = intraday.index.date.max()
        day_bars = intraday[intraday.index.date == last_day]
        if day_bars.empty:
            return None
    last = float(day_bars["Close"].iloc[-1])
    hod = float(day_bars["High"].max())
    vol_today = float(day_bars["Volume"].sum())

    # daily context, excluding today if it is already in the daily frame
    d = daily.copy()
    d_dates = pd.to_datetime(d.index).date
    hist = d[d_dates < today] if (d_dates == today).any() else d
    if len(hist) < 5:
        return None
    prev_close = float(hist["Close"].iloc[-1])
    avg_vol20 = float(hist["Volume"].tail(20).mean())
    high20 = float(hist["High"].tail(20).max())
    tr = pd.concat([hist["High"] - hist["Low"], (hist["High"] - hist["Close"].shift()).abs(),
                    (hist["Low"] - hist["Close"].shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())

    frac = session_fraction(now) if day_bars.index[-1].date() == today else 1.0
    frac = max(frac, 0.08)                     # first minutes are always "huge" otherwise
    rel_vol = vol_today / (avg_vol20 * frac) if avg_vol20 else 0.0
    change_pct = (last / prev_close - 1) * 100 if prev_close else 0.0

    first = day_bars.index[0]
    orb = day_bars[day_bars.index < first + timedelta(minutes=30)]
    orb_high = float(orb["High"].max()) if not orb.empty else hod
    before = day_bars.iloc[:-3] if len(day_bars) > 3 else day_bars.iloc[:0]
    hod_before = float(before["High"].max()) if not before.empty else None

    breakout = ""
    if last >= high20 * 0.999 and last > prev_close:
        breakout = "20d_high"
    elif hod_before and last >= hod_before * 0.999 and len(day_bars) > 6:
        breakout = "hod"
    elif last >= orb_high * 0.999 and len(day_bars) > len(orb):
        breakout = "orb"

    return {
        "last": last, "prev_close": prev_close, "change_pct": change_pct, "rel_vol": rel_vol,
        "vol_today": vol_today, "dollar_vol_m": vol_today * last / 1e6, "hod": hod,
        "orb_high": orb_high, "high20": high20, "atr_pct": atr / last * 100 if last else 0.0,
        "breakout": breakout, "bars_today": int(len(day_bars)),
    }


def score_wave(f: dict, news_count: int) -> float:
    s = min(f["rel_vol"], 10.0)                                   # 0..10
    s += min(max(f["change_pct"], 0.0), 30.0) / 5                 # 0..6
    s += {"20d_high": 3.0, "hod": 2.0, "orb": 1.5}.get(f["breakout"], 0.0)
    s += min(news_count, 5) * 0.8                                 # 0..4
    if f["change_pct"] > 40:
        s -= 3.0
    if f["rel_vol"] < 2:
        s -= 3.0
    if f["atr_pct"] > 15:
        s -= 1.0                                                  # too wild to stop sanely
    return round(s, 2)


def passes(f: dict, news_count: int) -> bool:
    if not (config.WAVE_MIN_PRICE <= f["last"] <= config.WAVE_MAX_PRICE):
        return False
    if f["dollar_vol_m"] < config.WAVE_MIN_DOLLAR_VOL_M:
        return False
    if f["change_pct"] < config.WAVE_MIN_CHANGE_PCT or f["change_pct"] > config.WAVE_MAX_CHANGE_PCT:
        return False
    if f["rel_vol"] < config.WAVE_MIN_REL_VOL:
        return False
    return bool(f["breakout"]) or news_count >= 2


# ----------------------------------------------------------------- candidates
async def candidates(broker=None, extra: Optional[List[str]] = None) -> Dict[str, dict]:
    """{ticker: {name, source}} within the price band."""
    out: Dict[str, dict] = {}
    if broker is not None and getattr(broker, "connected", False):
        for code in ("TOP_PERC_GAIN", "HOT_BY_VOLUME", "MOST_ACTIVE"):
            try:
                syms = await broker.scanner(code, above_price=config.WAVE_MIN_PRICE,
                                            below_price=config.WAVE_MAX_PRICE,
                                            mcap_below_m=config.WAVE_MAX_MCAP_B * 1000)
            except Exception:
                syms = []
            for s in syms:
                out.setdefault(s, {"name": s, "source": "ibkr"})
    if not out:
        loop = asyncio.get_running_loop()
        try:
            disc = await loop.run_in_executor(
                None, lambda: md.discover(min_price=config.WAVE_MIN_PRICE, min_avg_volume=200_000,
                                          min_market_cap=50e6, per_screen=50))
        except Exception:
            disc = {}
        for t, d in disc.items():
            if not d.get("price") or d["price"] > config.WAVE_MAX_PRICE:
                continue
            if d.get("market_cap") and d["market_cap"] > config.WAVE_MAX_MCAP_B * 1e9:
                continue                      # small caps only; IBKR's scanner filters this server-side
            out.setdefault(t, {"name": d.get("name") or t, "source": "yahoo"})
    for t in extra or []:
        out.setdefault(t.upper(), {"name": t.upper(), "source": "watch"})
    return out


async def _yahoo_bars(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """5-minute bars for the last 2 days, batched, off the event loop."""
    import yfinance as yf
    loop = asyncio.get_running_loop()

    def dl():
        raw = yf.download(tickers, period="2d", interval="5m", prepost=False, progress=False,
                          group_by="ticker", threads=True, auto_adjust=False)
        out = {}
        if raw is None or raw.empty:
            return out
        for t in tickers:
            try:
                df = raw[t].dropna(how="all") if len(tickers) > 1 else raw.dropna(how="all")
            except KeyError:
                continue
            if not df.empty:
                out[t] = df
        return out
    return await loop.run_in_executor(None, dl)


async def scan(broker=None, extra: Optional[List[str]] = None, with_news: bool = True,
               now: Optional[datetime] = None, log=None) -> List[Signal]:
    """Full pass: candidates -> bars -> features -> news -> scored signals, best first."""
    log = log or (lambda m: None)
    cands = await candidates(broker, extra)
    tickers = list(cands)
    if not tickers:
        return []
    log(f"scanner: {len(tickers)} candidates")
    loop = asyncio.get_running_loop()
    daily = await loop.run_in_executor(None, lambda: md.download_history(tickers, period="2mo"))

    intraday: Dict[str, pd.DataFrame] = {}
    use_ib = broker is not None and getattr(broker, "connected", False)
    if use_ib:
        sem = asyncio.Semaphore(4)

        async def one(t):
            async with sem:
                try:
                    intraday[t] = await broker.bars(t, duration="2 D", size="1 min")
                except Exception:
                    pass
        await asyncio.gather(*(one(t) for t in tickers if t in daily))
    missing = [t for t in tickers if t in daily and t not in intraday]
    if missing:
        intraday.update(await _yahoo_bars(missing))

    prelim = []
    for t in tickers:
        if t not in daily or t not in intraday:
            continue
        try:
            f = wave_features(intraday[t], daily[t], now)
        except Exception:
            f = None
        if f and config.WAVE_MIN_PRICE <= f["last"] <= config.WAVE_MAX_PRICE and f["change_pct"] > 0:
            prelim.append((t, f))
    prelim.sort(key=lambda x: score_wave(x[1], 0), reverse=True)
    prelim = prelim[:25]

    news: Dict[str, list] = {}
    if with_news and prelim:
        names = {t: cands[t]["name"] for t, _ in prelim}

        def fetch():
            hs = newsfeed.collect([t for t, _ in prelim], names=names,
                                  lookback_hours=config.WAVE_NEWS_HOURS, max_workers=6)
            return newsfeed.by_ticker(hs)
        try:
            news = await loop.run_in_executor(None, fetch)
        except Exception:
            news = {}

    out = []
    for t, f in prelim:
        items = news.get(t, [])
        n = len(items)
        if not passes(f, n):
            continue
        atr = f["atr_pct"]
        stop_pct = max(config.WAVE_STOP_PCT, min(1.5 * atr, 12.0)) if atr else config.WAVE_STOP_PCT
        trail_pct = max(config.WAVE_TRAIL_PCT, min(2.0 * atr, 15.0)) if atr else config.WAVE_TRAIL_PCT
        out.append(Signal(
            ticker=t, name=cands[t]["name"], price=round(f["last"], 4),
            change_pct=round(f["change_pct"], 2), rel_vol=round(f["rel_vol"], 2),
            dollar_vol_m=round(f["dollar_vol_m"], 2), breakout=f["breakout"], news_count=n,
            headline=items[0].title if items else "", score=score_wave(f, n),
            atr_pct=round(atr, 2), stop_pct=round(stop_pct, 1), trail_pct=round(trail_pct, 1),
            source=cands[t]["source"], entry_limit=round(f["last"] * (1 + config.WAVE_ENTRY_BUFFER_PCT / 100), 2),
            headlines=[{"title": h.title, "source": h.source, "age_h": h.age_hours(), "link": h.link}
                       for h in items[:5]],
        ))
    out.sort(key=lambda s: s.score, reverse=True)
    log(f"scanner: {len(out)} waves")
    return out
