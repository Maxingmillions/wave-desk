"""
News collection for the rotation terminal.

Pulls headlines from several free sources, de-duplicates them, tags each
headline with the tickers it mentions, and attaches a crude lexicon
sentiment score so the ranking has something to work with even when no LLM
is configured.

Sources (all best-effort; a source that fails is skipped, never fatal):
  * Yahoo Finance RSS, per ticker        finance.yahoo.com/rss/headline?s=X
  * Google News RSS, per ticker + market  news.google.com/rss/search?q=...
  * Finviz front-page news               finviz.com/news.ashx
  * WSJ Markets RSS                      feeds.a.dj.com/rss/RSSMarketsMain.xml
  * Alpha Vantage NEWS_SENTIMENT          only when ALPHAVANTAGE_API_KEY is set
  * yfinance Ticker.news                  fallback when Yahoo RSS is throttled
"""
from __future__ import annotations

import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 15


@dataclass
class Headline:
    title: str
    link: str
    source: str
    published: Optional[datetime]          # tz-aware UTC or None
    tickers: List[str] = field(default_factory=list)
    summary: str = ""
    sentiment: float = 0.0                  # -1 .. +1, lexicon-based
    provider_sentiment: Optional[float] = None   # e.g. Alpha Vantage's own score

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.published is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.published).total_seconds() / 3600)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat() if self.published else None
        return d


# ----------------------------------------------------------------- sentiment
# Deliberately small and finance-specific. This is a tiebreaker, not a model.
_POS = {
    "beat", "beats", "surge", "surges", "soar", "soars", "jump", "jumps", "rally",
    "rallies", "record", "upgrade", "upgrades", "upgraded", "outperform", "raises",
    "raised", "guidance raised", "buyback", "strong", "growth", "profit", "wins",
    "win", "approval", "approved", "contract", "partnership", "expands", "boost",
    "boosts", "bullish", "tops", "exceeds", "accelerat", "breakout", "momentum",
    "all-time high", "52-week high", "demand", "dividend increase",
}
_NEG = {
    "miss", "misses", "missed", "plunge", "plunges", "sink", "sinks", "fall",
    "falls", "drop", "drops", "slump", "slumps", "tumble", "tumbles", "downgrade",
    "downgrades", "downgraded", "cuts", "cut guidance", "lowers", "lowered",
    "warning", "warns", "lawsuit", "probe", "investigation", "recall", "layoffs",
    "bankrupt", "bankruptcy", "default", "fraud", "sec charges", "halted",
    "dilution", "offering", "secondary offering", "short report", "bearish",
    "weak", "loss", "losses", "underperform", "52-week low", "delisting",
    "going concern", "resigns", "sell-off", "selloff",
}


def lexicon_sentiment(text: str) -> float:
    t = text.lower()
    pos = sum(1 for w in _POS if w in t)
    neg = sum(1 for w in _NEG if w in t)
    if pos == neg:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / max(1, pos + neg)))


# ----------------------------------------------------------------- tickers
_EXCH_RE = re.compile(r"\((?:NASDAQ|NYSE|AMEX|NYSEARCA|NYSEAMERICAN|OTC|TSX|CBOE):\s*([A-Z]{1,5})\)")
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_STOPWORDS = {"A", "I", "AN", "THE", "AND", "OR", "FOR", "TO", "IN", "ON", "AT", "BY",
              "OF", "US", "USA", "CEO", "CFO", "IPO", "ETF", "AI", "EV", "GDP", "CPI",
              "FED", "SEC", "FDA", "DOW", "NYSE", "EPS", "Q1", "Q2", "Q3", "Q4", "YTD",
              "PCE", "FOMC", "OPEC", "EU", "UK", "UP", "IT", "IS", "AS", "VS", "ARE"}


def extract_tickers(text: str, known: Iterable[str] = ()) -> List[str]:
    found: List[str] = []
    for rx in (_EXCH_RE, _CASHTAG_RE):
        for m in rx.findall(text):
            if m not in _STOPWORDS and m not in found:
                found.append(m)
    # explicit mentions of tickers we already care about, as whole words
    for t in known:
        if t in found or t in _STOPWORDS or len(t) < 2:
            continue
        if re.search(rf"\b{re.escape(t)}\b", text):
            found.append(t)
    return found


# ----------------------------------------------------------------- helpers
def _get(url: str, **kw) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, **kw)
        if r.status_code == 200:
            return r
    except requests.RequestException:
        pass
    return None


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _rss(url: str, source: str, ticker: Optional[str] = None) -> List[Headline]:
    try:
        import feedparser
    except ImportError:
        return []
    r = _get(url)
    if r is None:
        return []
    feed = feedparser.parse(r.content)
    out = []
    for e in feed.entries:
        title = html.unescape(e.get("title", "")).strip()
        if not title:
            continue
        src = source
        if source == "google" and e.get("source") and e["source"].get("title"):
            src = f"google:{e['source']['title']}"
            # Google formats titles as "Headline - Publisher"
            title = re.sub(r"\s+-\s+[^-]+$", "", title)
        out.append(Headline(
            title=title,
            link=e.get("link", ""),
            source=src,
            published=_parse_date(e.get("published") or e.get("updated")),
            tickers=[ticker] if ticker else [],
            summary=re.sub(r"<[^>]+>", "", html.unescape(e.get("summary", "")))[:300],
        ))
    return out


# ----------------------------------------------------------------- sources
_YAHOO_LOCK = threading.Semaphore(2)
_YAHOO_FAILS = [0]          # consecutive empty/throttled responses; >= 6 trips the breaker


def yahoo_ticker_news(ticker: str) -> List[Headline]:
    # Yahoo returns 429 quickly when hit in parallel: two at a time, one
    # retry, and once it is clearly throttling us stop asking (Google News,
    # Finviz and WSJ still cover the pool).
    items: List[Headline] = []
    for attempt in range(2):
        if _YAHOO_FAILS[0] >= 6:
            break
        with _YAHOO_LOCK:
            items = _rss(f"https://finance.yahoo.com/rss/headline?s={ticker}", "yahoo", ticker)
            time.sleep(0.25)
        if items:
            _YAHOO_FAILS[0] = 0
            break
        time.sleep(1.5 * (attempt + 1))
    else:
        _YAHOO_FAILS[0] += 1
    if items:
        return items
    if _YAHOO_FAILS[0] >= 6:
        return []
    # Yahoo throttles the RSS endpoint aggressively from cloud IPs; the
    # yfinance JSON endpoint is a second route to the same stories.
    try:
        import yfinance as yf
        out = []
        for n in (yf.Ticker(ticker).news or [])[:15]:
            c = n.get("content", n)
            title = c.get("title") or ""
            if not title:
                continue
            link = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or ""
            pub = c.get("pubDate") or c.get("displayTime")
            if isinstance(pub, (int, float)):
                pub = datetime.fromtimestamp(pub, tz=timezone.utc).isoformat()
            out.append(Headline(title=title, link=link, source="yahoo:yf",
                                published=_parse_date(pub), tickers=[ticker],
                                summary=(c.get("summary") or "")[:300]))
        return out
    except Exception:
        return []


_CORP_WORDS = {"inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "ltd", "ltd.",
               "plc", "holdings", "group", "company", "the", "trust", "fund", "etf", "sa",
               "s.a.", "nv", "n.v.", "ag", "limited", "lp", "l.p.", "class", "series", "a", "b"}
_OPTION_RE = re.compile(r"\b[A-Z]{1,5}\d{6}[CP]\d{8}\b")


def clean_name(name: str) -> str:
    """'Hyperliquid Strategies Inc' -> 'Hyperliquid Strategies'; '' if nothing useful."""
    words = [w for w in re.split(r"[\s,]+", name or "") if w and w.lower() not in _CORP_WORDS]
    return " ".join(words[:3])


def google_ticker_news(ticker: str, name: str = "") -> List[Headline]:
    cname = clean_name(name)
    if cname and cname.upper() != ticker:
        q = f'"{cname}" OR "{ticker} stock" OR "{ticker} shares"'
    else:
        q = f'"{ticker} stock" OR "{ticker} shares" OR "NASDAQ:{ticker}" OR "NYSE:{ticker}"'
    q = requests.utils.quote(q)
    items = _rss(f"https://news.google.com/rss/search?q={q}+when:2d&hl=en-US&gl=US&ceid=US:en",
                 "google", ticker)
    # Google search is fuzzy; keep only stories that actually name the company.
    key = cname.lower().split(" ")[0] if cname else ""
    out = []
    for h in items:
        text = f"{h.title} {h.summary}"
        if _OPTION_RE.search(h.title):
            continue
        if re.search(rf"\b{re.escape(ticker)}\b", text) or (len(key) >= 4 and key in text.lower()):
            out.append(h)
    return out


def google_market_news() -> List[Headline]:
    out: List[Headline] = []
    for q in ("stock market today", "stock futures premarket", "earnings after the bell",
              "FDA approval stock", "upgraded to buy", "analyst upgrade stock"):
        out += _rss(f"https://news.google.com/rss/search?q={requests.utils.quote(q)}+when:1d"
                    "&hl=en-US&gl=US&ceid=US:en", "google")
    return out


def wsj_markets() -> List[Headline]:
    return _rss("https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "wsj")


_FINVIZ_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*class="nn-tab-link"[^>]*>([^<]+)</a>')


def finviz_news() -> List[Headline]:
    r = _get("https://finviz.com/news.ashx")
    if r is None:
        return []
    out = []
    now = datetime.now(timezone.utc)
    for link, title in _FINVIZ_RE.findall(r.text):
        out.append(Headline(title=html.unescape(title).strip(), link=link,
                            source="finviz", published=now))
    return out[:120]


def alpha_vantage_news(api_key: str, tickers: List[str]) -> List[Headline]:
    if not api_key:
        return []
    out: List[Headline] = []
    # AV allows up to ~50 tickers per call; chunk conservatively.
    for i in range(0, len(tickers), 20):
        chunk = ",".join(tickers[i:i + 20])
        r = _get("https://www.alphavantage.co/query",
                 params={"function": "NEWS_SENTIMENT", "tickers": chunk,
                         "limit": 50, "sort": "LATEST", "apikey": api_key})
        if r is None:
            continue
        try:
            data = r.json()
        except ValueError:
            continue
        for a in data.get("feed", []):
            ts = a.get("time_published", "")
            pub = None
            if len(ts) >= 15:
                pub = datetime.strptime(ts[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            mentioned = [t["ticker"] for t in a.get("ticker_sentiment", [])
                         if t.get("ticker") in tickers]
            out.append(Headline(
                title=a.get("title", ""), link=a.get("url", ""),
                source=f"av:{a.get('source', '')}", published=pub, tickers=mentioned,
                summary=(a.get("summary") or "")[:300],
                provider_sentiment=float(a.get("overall_sentiment_score", 0) or 0),
            ))
        time.sleep(1.0)   # free tier is rate-limited
    return out


# ----------------------------------------------------------------- orchestration
def _dedupe(items: List[Headline]) -> List[Headline]:
    seen: Dict[str, Headline] = {}
    for h in items:
        key = re.sub(r"[^a-z0-9]+", " ", h.title.lower()).strip()[:80]
        if not key:
            continue
        if key in seen:
            prev = seen[key]
            for t in h.tickers:
                if t not in prev.tickers:
                    prev.tickers.append(t)
            if prev.published is None and h.published:
                prev.published = h.published
        else:
            seen[key] = h
    return list(seen.values())


def collect(tickers: List[str], names: Optional[Dict[str, str]] = None,
            lookback_hours: int = 36, alpha_vantage_key: str = "",
            max_workers: int = 8, progress=None) -> List[Headline]:
    """
    Gather headlines for every ticker plus the market-wide feeds.
    Returns deduplicated headlines within the lookback window (undated items
    are kept -- Finviz does not carry timestamps in the page markup).
    """
    names = names or {}
    jobs = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        jobs.append(ex.submit(google_market_news))
        jobs.append(ex.submit(wsj_markets))
        jobs.append(ex.submit(finviz_news))
        for t in tickers:
            jobs.append(ex.submit(yahoo_ticker_news, t))
            jobs.append(ex.submit(google_ticker_news, t, names.get(t, "")))
        if alpha_vantage_key:
            jobs.append(ex.submit(alpha_vantage_news, alpha_vantage_key, tickers))
        items: List[Headline] = []
        for fut in as_completed(jobs):
            try:
                items += fut.result()
            except Exception:
                pass
            if progress:
                progress()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    fresh = [h for h in items if h.published is None or h.published >= cutoff]
    fresh = _dedupe(fresh)
    for h in fresh:
        text = f"{h.title}. {h.summary}"
        for t in extract_tickers(text, known=tickers):
            if t not in h.tickers:
                h.tickers.append(t)
        h.sentiment = lexicon_sentiment(text)
        if h.provider_sentiment is not None:
            # AV scores are roughly -0.35..+0.35 in practice; blend, don't replace
            h.sentiment = max(-1.0, min(1.0, 0.5 * h.sentiment + 2.0 * h.provider_sentiment))
    fresh.sort(key=lambda h: h.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return fresh


def by_ticker(headlines: List[Headline]) -> Dict[str, List[Headline]]:
    out: Dict[str, List[Headline]] = {}
    for h in headlines:
        for t in h.tickers:
            out.setdefault(t, []).append(h)
    return out


def ticker_sentiment(items: List[Headline]) -> float:
    """Recency-weighted average sentiment for one ticker, -1..+1."""
    if not items:
        return 0.0
    num = den = 0.0
    for h in items:
        age = h.age_hours()
        w = 1.0 if age is None else 1.0 / (1.0 + age / 12.0)
        num += w * h.sentiment
        den += w
    return num / den if den else 0.0
