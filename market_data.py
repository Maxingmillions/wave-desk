"""
Price history, technical features and candidate discovery via yfinance.

Nothing here decides anything; it turns tickers into numbers the rotation
logic (and the LLM) can reason about.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

try:
    import yfinance as yf
    import logging
    # yfinance logs every throttled fundamentals call at ERROR; they are
    # expected and handled (we fall back to the ticker symbol), so mute them.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
except ImportError:  # pragma: no cover - surfaced by terminal.py
    yf = None


def _require_yf():
    if yf is None:
        raise SystemExit("yfinance is not installed: ./.venv/bin/pip install yfinance")


# ----------------------------------------------------------------- history
def download_history(tickers: List[str], period: str = "1y") -> Dict[str, pd.DataFrame]:
    """One batched download; returns {ticker: OHLCV frame} for those that came back."""
    _require_yf()
    tickers = sorted(set(t for t in tickers if t))
    if not tickers:
        return {}
    raw = yf.download(tickers, period=period, interval="1d", auto_adjust=True,
                      progress=False, group_by="ticker", threads=True)
    out: Dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    if len(tickers) == 1:
        df = raw.dropna(how="all")
        if not df.empty:
            out[tickers[0]] = df
        return out
    for t in tickers:
        try:
            df = raw[t].dropna(how="all")
        except KeyError:
            continue
        if len(df) >= 25:
            out[t] = df
    return out


def _rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff().dropna()
    if len(d) < n + 1:
        return float("nan")
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    v = rsi.iloc[-1]
    return float(v) if not math.isnan(v) else 100.0


def _ret(close: pd.Series, n: int) -> float:
    if len(close) <= n:
        return float("nan")
    return float(close.iloc[-1] / close.iloc[-1 - n] - 1)


def features(df: pd.DataFrame) -> dict:
    """Technical snapshot from a daily OHLCV frame. All returns are fractions."""
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    lo = df["Low"].astype(float)
    v = df["Volume"].astype(float)
    last = float(c.iloc[-1])
    prev = float(c.iloc[-2]) if len(c) > 1 else last
    tr = pd.concat([(h - lo), (h - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else float("nan")
    sma20 = float(c.rolling(20).mean().iloc[-1]) if len(c) >= 20 else float("nan")
    sma50 = float(c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else float("nan")
    sma200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else float("nan")
    vol20 = float(v.tail(21).head(20).mean()) if len(v) >= 21 else float("nan")
    hi52 = float(c.max())
    lo52 = float(c.min())
    opn = float(df["Open"].iloc[-1])
    return {
        "last": round(last, 4),
        "prev_close": round(prev, 4),
        "gap_pct": round((opn / prev - 1) * 100, 2) if prev else None,
        "ret_1d": round(_ret(c, 1) * 100, 2),
        "ret_5d": round(_ret(c, 5) * 100, 2),
        "ret_20d": round(_ret(c, 20) * 100, 2),
        "ret_60d": round(_ret(c, 60) * 100, 2),
        "rsi14": round(_rsi(c), 1),
        "above_sma20": bool(last > sma20) if not math.isnan(sma20) else None,
        "above_sma50": bool(last > sma50) if not math.isnan(sma50) else None,
        "above_sma200": bool(last > sma200) if not math.isnan(sma200) else None,
        "sma50_vs_200": round((sma50 / sma200 - 1) * 100, 2)
        if not (math.isnan(sma50) or math.isnan(sma200)) else None,
        "atr_pct": round(atr / last * 100, 2) if last and not math.isnan(atr) else None,
        "vol_ratio": round(float(v.iloc[-1]) / vol20, 2) if vol20 and not math.isnan(vol20) else None,
        "avg_dollar_vol_m": round(float((c * v).tail(20).mean()) / 1e6, 1),
        "pct_from_52w_high": round((last / hi52 - 1) * 100, 1) if hi52 else None,
        "pct_from_52w_low": round((last / lo52 - 1) * 100, 1) if lo52 else None,
        "bars": int(len(c)),
        "asof": str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1]),
    }


# ----------------------------------------------------------------- metadata
def info(ticker: str) -> dict:
    """Name / sector / cap / next earnings date. Slow (one HTTP call each) -- call sparingly."""
    _require_yf()
    out = {"ticker": ticker, "name": ticker, "sector": None, "industry": None,
           "market_cap": None, "next_earnings": None}
    try:
        t = yf.Ticker(ticker)
        i = t.info or {}
        out["name"] = i.get("shortName") or i.get("longName") or ticker
        out["sector"] = i.get("sector")
        out["industry"] = i.get("industry")
        out["market_cap"] = i.get("marketCap")
        cal = t.calendar or {}
        ed = cal.get("Earnings Date")
        if isinstance(ed, list) and ed:
            ed = ed[0]
        if isinstance(ed, (date, datetime)):
            out["next_earnings"] = ed.isoformat()[:10]
    except Exception:
        pass
    return out


def info_many(tickers: List[str], max_workers: int = 3) -> Dict[str, dict]:
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return dict(zip(tickers, ex.map(info, tickers)))


def days_to_earnings(next_earnings: Optional[str], today: Optional[date] = None) -> Optional[int]:
    if not next_earnings:
        return None
    try:
        d = date.fromisoformat(next_earnings[:10])
    except ValueError:
        return None
    return (d - (today or date.today())).days


# ----------------------------------------------------------------- discovery
SCREENS = ("day_gainers", "most_actives", "growth_technology_stocks",
           "undervalued_growth_stocks", "small_cap_gainers")


def discover(min_price: float = 3.0, min_avg_volume: int = 500_000,
             min_market_cap: float = 300e6, per_screen: int = 25) -> Dict[str, dict]:
    """
    Pull Yahoo's predefined screeners for fresh candidates. Filters out the
    illiquid and the tiny so the LLM is not asked to opine on penny stocks.
    Returns {ticker: {name, screens:[...], price, change_pct}}.
    """
    _require_yf()
    found: Dict[str, dict] = {}
    for s in SCREENS:
        try:
            r = yf.screen(s, count=per_screen)
        except Exception:
            continue
        for q in (r or {}).get("quotes", []):
            sym = q.get("symbol", "")
            if not sym or not sym.isalpha() or len(sym) > 5:
                continue
            price = q.get("regularMarketPrice") or 0
            avol = q.get("averageDailyVolume3Month") or 0
            cap = q.get("marketCap") or 0
            if price < min_price or avol < min_avg_volume or cap < min_market_cap:
                continue
            e = found.setdefault(sym, {"name": q.get("shortName") or sym, "screens": [],
                                       "price": price, "market_cap": cap,
                                       "change_pct": q.get("regularMarketChangePercent")})
            if s not in e["screens"]:
                e["screens"].append(s)
    return found
