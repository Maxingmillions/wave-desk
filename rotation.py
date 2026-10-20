"""
The rotation brain.

    dossier = build_dossier(portfolio, universe, ...)   # news + prices + features
    plan    = decide(dossier)                           # Claude if configured, else rules

A *plan* is a JSON-serialisable dict (see PLAN_SCHEMA) describing what to do
at the next open: which holdings to keep / trim / sell, which names to buy
with what size and limit, and why. The user asked for a buy-leaning planner,
so sells need a stated reason; buys only need a thesis and room in the
account.

Nothing in this module places an order.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

import config
import market_data as md
import newsfeed

# ----------------------------------------------------------------- plan schema
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "market_summary": {"type": "string",
                           "description": "3-6 sentences: what moved today, what matters tomorrow."},
        "regime": {"type": "string", "enum": ["risk_on", "neutral", "risk_off"]},
        "holdings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "action": {"type": "string", "enum": ["HOLD", "TRIM", "SELL"]},
                    "quantity": {"type": "integer",
                                 "description": "shares to sell (0 for HOLD)"},
                    "limit_price": {"type": "number",
                                    "description": "sell limit; 0 means market-on-open"},
                    "reason": {"type": "string"},
                },
                "required": ["ticker", "action", "quantity", "limit_price", "reason"],
                "additionalProperties": False,
            },
        },
        "buys": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "limit_price": {"type": "number"},
                    "order_type": {"type": "string", "enum": ["LIMIT", "MARKET"]},
                    "thesis": {"type": "string"},
                    "catalyst": {"type": "string",
                                 "description": "the specific news / event driving the entry"},
                    "risk": {"type": "string"},
                    "stop_price": {"type": "number"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["ticker", "quantity", "limit_price", "order_type", "thesis",
                             "catalyst", "risk", "stop_price", "confidence"],
                "additionalProperties": False,
            },
        },
        "watchlist": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "note": {"type": "string"}},
                "required": ["ticker", "note"],
                "additionalProperties": False,
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "cash_after_plan": {"type": "number"},
    },
    "required": ["market_summary", "regime", "holdings", "buys", "watchlist", "risks",
                 "cash_after_plan"],
    "additionalProperties": False,
}


# ----------------------------------------------------------------- inputs
def load_portfolio(path=None) -> dict:
    path = path or config.PORTFOLIO_PATH
    if not path.exists():
        return {"asof": None, "cash": 0.0, "positions": []}
    with open(path) as f:
        p = json.load(f)
    p.setdefault("cash", 0.0)
    p.setdefault("positions", [])
    return p


def load_universe(path=None) -> List[str]:
    path = path or config.UNIVERSE_PATH
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line and line not in out:
            out.append(line)
    return out


# ----------------------------------------------------------------- scoring
def _nz(x, default=0.0):
    return default if x is None or (isinstance(x, float) and math.isnan(x)) else x


def pre_score(feat: dict, sentiment: float, n_news: int, held: bool) -> float:
    """
    Cheap composite used to (a) rank the rule-based fallback and (b) cut the
    candidate list down before it goes to the LLM. Higher = more buyable.
    Momentum + fresh news flow + trend alignment, minus overbought / dead names.
    """
    s = 0.0
    s += 0.6 * _nz(feat.get("ret_5d")) / 5
    s += 0.4 * _nz(feat.get("ret_20d")) / 10
    s += 2.5 * sentiment
    s += 0.4 * min(n_news, 6)
    if feat.get("above_sma20"):
        s += 1.0
    if feat.get("above_sma50"):
        s += 1.0
    if feat.get("above_sma200") is False:
        s -= 1.0
    vr = _nz(feat.get("vol_ratio"), 1.0)
    if vr > 1.5 and _nz(feat.get("ret_1d")) > 0:
        s += 1.0        # buyers showed up today
    rsi = _nz(feat.get("rsi14"), 50)
    if rsi > 80:
        s -= 2.0
    elif rsi > 78:
        s -= 1.5
    if _nz(feat.get("ret_1d")) > 12:
        s -= 1.5        # chasing a one-day pop is usually late
    elif rsi < 30 and _nz(feat.get("ret_1d")) < -4:
        s -= 1.0        # falling knife
    if _nz(feat.get("avg_dollar_vol_m"), 0) < 5:
        s -= 2.0        # too thin for a clean exit
    if held:
        s += 0.5        # mild inertia; rotating costs spread + taxes
    return round(s, 2)


# ----------------------------------------------------------------- dossier
META_CACHE_PATH = config.ROOT / "meta_cache.json"
META_TTL_DAYS = 3


def _load_meta_cache() -> dict:
    try:
        return json.loads(META_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def refresh_meta(tickers: List[str], log=print, enabled: bool = True) -> Dict[str, dict]:
    """Name / sector / earnings per ticker, cached on disk for META_TTL_DAYS."""
    cache = _load_meta_cache()
    today = date.today()
    stale = []
    for t in tickers:
        m = cache.get(t)
        if not m:
            stale.append(t)
            continue
        try:
            age = (today - date.fromisoformat(m.get("fetched", "1970-01-01"))).days
        except ValueError:
            age = 999
        if age > META_TTL_DAYS or (m.get("next_earnings") and m["next_earnings"] < today.isoformat()):
            stale.append(t)
    if stale and enabled:
        log(f"Fetching names / sectors / earnings dates for {len(stale)} tickers (cached afterwards)...")
        fresh = md.info_many(stale)
        for t, m in fresh.items():
            m["fetched"] = today.isoformat()
            cache[t] = m
        try:
            META_CACHE_PATH.write_text(json.dumps(cache, indent=0, sort_keys=True))
        except OSError:
            pass
    return {t: cache.get(t, {"name": t}) for t in tickers}


def build_dossier(portfolio: dict, universe: List[str], *, discover: bool = True,
                  lookback_hours: Optional[int] = None, with_info: bool = True,
                  log=print) -> dict:
    """Everything the decision step needs, as one dict (also what gets saved)."""
    lookback_hours = lookback_hours or config.ROTATION_NEWS_HOURS
    held = {p["ticker"].upper(): p for p in portfolio.get("positions", [])}
    tickers = list(dict.fromkeys(list(held) + [t.upper() for t in universe]))

    discovered: Dict[str, dict] = {}
    if discover:
        log("Scanning Yahoo screeners for fresh candidates...")
        try:
            discovered = md.discover()
        except Exception as e:  # discovery is a bonus, never a blocker
            log(f"  screener unavailable: {e}")
        for t in discovered:
            if t not in tickers:
                tickers.append(t)

    # Names sharpen the news queries, but Yahoo throttles its fundamentals
    # endpoint hard, so only the cache is consulted here; the network lookup
    # happens later for the ~40 names that survive pre-ranking.
    meta = refresh_meta(tickers, log=log, enabled=False)
    names = {t: (meta[t].get("name") or discovered.get(t, {}).get("name") or "") for t in tickers}

    log(f"Collecting news for {len(tickers)} tickers (last {lookback_hours}h)...")
    headlines = newsfeed.collect(tickers, names=names, lookback_hours=lookback_hours,
                                 alpha_vantage_key=config.ALPHAVANTAGE_API_KEY)
    news_map = newsfeed.by_ticker(headlines)

    # tickers that only showed up in headlines (e.g. "(NASDAQ:XYZ)") join the pool
    for t, items in news_map.items():
        if t not in tickers and len(items) >= 2 and t.isalpha() and len(t) <= 5:
            tickers.append(t)

    log(f"Downloading price history for {len(tickers)} tickers...")
    hist = md.download_history(tickers, period="1y")

    rows: Dict[str, dict] = {}
    for t in tickers:
        if t not in hist:
            continue
        try:
            feat = md.features(hist[t])
        except Exception:
            continue
        items = news_map.get(t, [])
        sent = newsfeed.ticker_sentiment(items)
        rows[t] = {
            "ticker": t,
            "held": t in held,
            "features": feat,
            "news_sentiment": round(sent, 3),
            "news_count": len(items),
            "screens": discovered.get(t, {}).get("screens", []),
            "pre_score": pre_score(feat, sent, len(items), t in held),
            "headlines": [
                {"title": h.title, "source": h.source, "age_h": None if h.age_hours() is None
                 else round(h.age_hours(), 1), "sentiment": round(h.sentiment, 2), "link": h.link}
                for h in items[:8]
            ],
        }

    ranked = sorted(rows.values(), key=lambda r: r["pre_score"], reverse=True)
    keep = set(held) | {r["ticker"] for r in ranked[:config.ROTATION_CANDIDATE_LIMIT]}
    candidates = [r for r in ranked if r["ticker"] in keep]

    meta.update(refresh_meta([r["ticker"] for r in candidates], log=log, enabled=with_info))
    for r in candidates:
        m = meta.get(r["ticker"], {})
        r["name"] = m.get("name") or names.get(r["ticker"]) or r["ticker"]
        r["sector"] = m.get("sector")
        r["industry"] = m.get("industry")
        r["market_cap_b"] = round(m["market_cap"] / 1e9, 2) if m.get("market_cap") else None
        r["next_earnings"] = m.get("next_earnings")
        r["days_to_earnings"] = md.days_to_earnings(m.get("next_earnings"))

    # account block, marked at last close
    positions = []
    mv_total = 0.0
    for t, p in held.items():
        last = rows.get(t, {}).get("features", {}).get("last")
        qty = float(p.get("quantity", 0))
        mv = (last or 0) * qty
        mv_total += mv
        avg = p.get("avg_price")
        positions.append({
            "ticker": t, "quantity": qty, "avg_price": avg, "last": last,
            "market_value": round(mv, 2),
            "pnl_pct": round((last / avg - 1) * 100, 2) if last and avg else None,
            "has_data": t in rows,
        })
    cash = float(portfolio.get("cash", 0.0))
    equity = cash + mv_total
    for p in positions:
        p["weight"] = round(p["market_value"] / equity, 3) if equity else None

    market = {t: rows[t]["features"] for t in ("SPY", "QQQ", "IWM") if t in rows}
    market_headlines = [
        {"title": h.title, "source": h.source,
         "age_h": None if h.age_hours() is None else round(h.age_hours(), 1),
         "tickers": h.tickers, "sentiment": round(h.sentiment, 2)}
        for h in headlines if not h.tickers or h.source in ("wsj", "finviz") or h.source.startswith("google")
    ][:40]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "for_session": _next_session(),
        "account": {"cash": round(cash, 2), "equity": round(equity, 2),
                    "positions_value": round(mv_total, 2), "positions": positions,
                    "asof": portfolio.get("asof")},
        "constraints": {
            "max_positions": config.ROTATION_MAX_POSITIONS,
            "max_weight_per_name": config.ROTATION_MAX_WEIGHT,
            "min_order_usd": config.ROTATION_MIN_ORDER_USD,
            "max_buys": config.ROTATION_MAX_BUYS,
            "stop_pct_below_cost": config.ROTATION_STOP_PCT,
            "earnings_buffer_days": config.ROTATION_EARNINGS_BUFFER_DAYS,
            "no_margin": True,
        },
        "market": market,
        "market_headlines": market_headlines,
        "candidates": candidates,
        "n_headlines": len(headlines),
        "n_tickers_scanned": len(rows),
    }


def _next_session() -> str:
    d = date.today()
    # after a weekday close the next session is tomorrow; Fri/Sat/Sun -> Monday
    step = {4: 3, 5: 2, 6: 1}.get(d.weekday(), 1)
    from datetime import timedelta
    return (d + timedelta(days=step)).isoformat()


# ----------------------------------------------------------------- rule-based plan
def rule_plan(dossier: dict) -> dict:
    """
    Deterministic fallback used when no API key is set or the LLM call fails.
    Sells only on a stop breach, a trend break with negative news, or an
    earnings print inside the buffer. Buys the top-ranked names it can afford.
    """
    acct = dossier["account"]
    cons = dossier["constraints"]
    cand = {r["ticker"]: r for r in dossier["candidates"]}
    equity = acct["equity"] or 1.0
    cash = acct["cash"]
    holdings_out, risks = [], []
    held_tickers = set()

    for p in acct["positions"]:
        t = p["ticker"]
        held_tickers.add(t)
        r = cand.get(t)
        f = r["features"] if r else {}
        last = p.get("last") or 0
        reason, action, qty = "no exit trigger; momentum and news flow acceptable", "HOLD", 0
        if not r:
            reason = "no price data available; left untouched"
        elif p.get("pnl_pct") is not None and p["pnl_pct"] <= -cons["stop_pct_below_cost"]:
            action, qty = "SELL", int(p["quantity"])
            reason = f"{p['pnl_pct']:+.1f}% vs cost breaches the {cons['stop_pct_below_cost']}% stop"
        elif f.get("above_sma20") is False and r["news_sentiment"] < -0.2:
            action, qty = "SELL", int(p["quantity"])
            reason = "closed below 20d SMA with negative news flow"
        elif r.get("days_to_earnings") is not None and 0 <= r["days_to_earnings"] <= cons["earnings_buffer_days"]:
            action, qty = "TRIM", int(p["quantity"] // 2)
            reason = f"earnings in {r['days_to_earnings']}d; halve exposure into the print"
        elif p.get("weight") and p["weight"] > cons["max_weight_per_name"] + 0.15:
            # Over-concentrated, but the owner prefers buying to selling: only
            # free enough cash for one minimum-size rotation, capped at a
            # quarter of the position, and only when cash alone cannot do it.
            if cash < cons["min_order_usd"] and last:
                need = cons["min_order_usd"] * 2 - cash
                qty = min(int(need // (last * 0.995)) + 1, int(p["quantity"] // 4))
                if qty > 0:
                    action = "TRIM"
                    reason = (f"{p['weight']*100:.0f}% of equity in one name and ${cash:,.0f} cash; "
                              f"trim {qty} sh to fund one rotation (optional)")
            else:
                reason = f"{p['weight']*100:.0f}% of equity in one name; hold, but add elsewhere first"
        holdings_out.append({"ticker": t, "action": action, "quantity": qty,
                             "limit_price": round(last * 0.995, 2) if qty and last else 0,
                             "reason": reason})
        if action != "HOLD" and last:
            cash += qty * last * 0.995

    n_open = sum(1 for h in holdings_out if h["action"] != "SELL")
    slots = max(0, cons["max_positions"] - n_open)
    buys = []
    for r in dossier["candidates"]:
        if len(buys) >= min(cons["max_buys"], slots):
            break
        t = r["ticker"]
        f = r["features"]
        if t in held_tickers or t in ("SPY", "QQQ", "IWM"):
            continue
        if r.get("days_to_earnings") is not None and 0 <= r["days_to_earnings"] <= cons["earnings_buffer_days"]:
            continue
        if r["pre_score"] < 2.0 or f.get("rsi14", 50) > 78:
            continue
        best = max(r["headlines"], key=lambda h: h["sentiment"], default=None)
        if best and best["sentiment"] < -0.3:
            continue    # the freshest story is bad; not a buy for a rules engine
        budget = min(cash, equity * cons["max_weight_per_name"])
        if budget < cons["min_order_usd"]:
            break
        limit = round(f["last"] * 1.005, 2)
        qty = int(budget // limit)
        if qty <= 0:
            continue
        atr = f.get("atr_pct") or 4.0
        buys.append({
            "ticker": t, "quantity": qty, "limit_price": limit, "order_type": "LIMIT",
            "thesis": f"ranked #{len(buys)+1} on momentum + news (score {r['pre_score']}): "
                      f"5d {f.get('ret_5d')}%, 20d {f.get('ret_20d')}%, RSI {f.get('rsi14')}",
            "catalyst": (best["title"] if best else "price momentum, no single headline"),
            "risk": f"ATR {atr}%/day; thin news coverage" if r["news_count"] < 2 else f"ATR {atr}%/day",
            "stop_price": round(f["last"] * (1 - max(2 * atr, 6) / 100), 2),
            "confidence": "medium" if r["news_count"] >= 2 else "low",
        })
        cash -= qty * limit

    if cash < cons["min_order_usd"] and not buys and slots:
        risks.append("Not enough free cash to open a new position without trimming something.")
    spy = dossier["market"].get("SPY", {})
    regime = "neutral"
    if spy.get("above_sma50") and (spy.get("ret_5d") or 0) > 0:
        regime = "risk_on"
    elif spy.get("above_sma50") is False:
        regime = "risk_off"
        risks.append("SPY below its 50d SMA; size new positions smaller or wait.")

    return {
        "market_summary": "Rule-based plan (no LLM configured). Regime from SPY trend; "
                          "ranking from momentum, trend and headline sentiment.",
        "regime": regime,
        "holdings": holdings_out,
        "buys": buys,
        "watchlist": [{"ticker": r["ticker"], "note": f"score {r['pre_score']}"}
                      for r in dossier["candidates"][:8]
                      if r["ticker"] not in {b["ticker"] for b in buys} and not r["held"]][:5],
        "risks": risks or ["None flagged by rules; read the headlines yourself before submitting."],
        "cash_after_plan": round(cash, 2),
        "_engine": "rules",
    }


# ----------------------------------------------------------------- LLM plan
SYSTEM_PROMPT = """You are a discretionary swing trader planning orders for the next US market open \
for a small self-directed brokerage account. You are given a dossier: the account, its holdings, \
market context, and a pre-ranked list of candidate stocks with technical features and the last \
day's headlines for each. Produce one concrete plan.

How to think:
- Read the headlines first. A real catalyst (earnings beat, guidance raise, contract, approval, \
upgrade, index inclusion, sector move with follow-through) beats a chart pattern. Ignore \
promotional or vague stories. Treat a secondary offering, going-concern language, SEC action, \
or guidance cut as disqualifying for a buy.
- The owner prefers buying over selling. SELL or TRIM only with a specific reason: the stop is \
breached, the thesis broke, earnings land inside the buffer, or the position is far above the \
per-name weight cap. Otherwise HOLD. Never sell just to raise cash for a marginally better idea.
- Buys must fit the constraints: whole shares, no margin, total cost at most cash plus proceeds \
of sells/trims you proposed, at most max_buys new names, at most max_weight_per_name of equity \
each, no buy inside earnings_buffer_days of an earnings date, no name with avg_dollar_vol_m under 5.
- Prefer liquid names with a fresh catalyst and a trend that is intact (above 20d/50d SMA) and \
not blown out (RSI under 78). Momentum chasing after a +20% day is usually late; say so.
- Limit prices: for buys a few tenths of a percent above last close unless the catalyst makes a \
gap likely, then say MARKET. For sells, slightly below last close. Stops ~2 ATR below entry.
- If nothing is worth buying, return an empty buys list and explain in market_summary. An honest \
"wait" is a valid plan.
- Be specific and brief in every text field: ticker, number, headline, reason. No hedging boilerplate.
"""


def llm_plan(dossier: dict, model: Optional[str] = None, effort: Optional[str] = None,
             log=print) -> dict:
    """Ask Claude for the plan. Raises on any failure; caller decides on fallback."""
    import anthropic

    model = model or config.ROTATION_MODEL
    effort = effort or config.ROTATION_EFFORT
    client = anthropic.Anthropic()

    slim = _slim_for_prompt(dossier)
    user = ("Today is {today} (UTC). Plan orders for the {sess} session.\n\n"
            "DOSSIER (JSON):\n{d}").format(
        today=dossier["generated_at"][:10], sess=dossier["for_session"],
        d=json.dumps(slim, separators=(",", ":"), default=str))

    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
        output_config={"effort": effort,
                       "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
    )
    log(f"Asking {model} (effort={effort}) for the plan...")
    try:
        # Server-side refusal fallback: if the primary model declines, the API
        # re-runs on a fallback model inside the same call.
        with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"],
                                         fallbacks="default", **kwargs) as s:
            resp = s.get_final_message()
    except anthropic.BadRequestError:
        # beta surface rejected something (older SDK / platform) -- plain call
        with client.messages.stream(**kwargs) as s:
            resp = s.get_final_message()

    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("plan truncated (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    plan = json.loads(text)
    plan["_engine"] = f"{resp.model}/{effort}"
    plan["_usage"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens,
                      "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0)}
    return _sanitise(plan, dossier)


def _slim_for_prompt(d: dict) -> dict:
    """Drop links and other bulk the model does not need to reason."""
    out = {k: d[k] for k in ("for_session", "account", "constraints", "market")}
    out["market_headlines"] = [{k: h[k] for k in ("title", "source", "age_h")}
                               for h in d["market_headlines"]]
    out["candidates"] = []
    for r in d["candidates"]:
        c = {k: r.get(k) for k in ("ticker", "name", "held", "sector", "market_cap_b",
                                   "next_earnings", "days_to_earnings", "news_sentiment",
                                   "news_count", "screens", "pre_score")}
        c["features"] = {k: v for k, v in r["features"].items() if k not in ("bars", "prev_close")}
        c["headlines"] = [{k: h[k] for k in ("title", "source", "age_h")} for h in r["headlines"]]
        out["candidates"].append(c)
    return out


def _sanitise(plan: dict, dossier: dict) -> dict:
    """Hard constraints the model is not trusted with: affordability and whole shares."""
    acct = dossier["account"]
    last = {r["ticker"]: r["features"]["last"] for r in dossier["candidates"]}
    cash = acct["cash"]
    for h in plan.get("holdings", []):
        if h["action"] == "HOLD":
            h["quantity"] = 0
            continue
        held_qty = next((p["quantity"] for p in acct["positions"] if p["ticker"] == h["ticker"]), 0)
        h["quantity"] = int(min(max(h["quantity"], 0), held_qty))
        px = h["limit_price"] or last.get(h["ticker"], 0)
        cash += h["quantity"] * px * 0.995
    kept = []
    for b in plan.get("buys", []):
        px = b["limit_price"] if b["order_type"] == "LIMIT" and b["limit_price"] else last.get(b["ticker"], 0)
        if not px:
            continue
        b["quantity"] = int(max(b["quantity"], 0))
        if b["quantity"] * px > cash:
            b["quantity"] = int(cash // px)
            b["risk"] = (b["risk"] + " | size cut to fit available cash").strip(" |")
        if b["quantity"] <= 0:
            continue
        cash -= b["quantity"] * px
        kept.append(b)
    plan["buys"] = kept
    plan["cash_after_plan"] = round(cash, 2)
    return plan


# ----------------------------------------------------------------- entry
def decide(dossier: dict, use_llm: Optional[bool] = None, log=print) -> dict:
    if use_llm is None:
        use_llm = bool(config.ANTHROPIC_API_KEY)
    if use_llm:
        try:
            return llm_plan(dossier, log=log)
        except Exception as e:
            log(f"LLM plan failed ({type(e).__name__}: {e}); falling back to rules.")
    return rule_plan(dossier)
