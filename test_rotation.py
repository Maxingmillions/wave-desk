"""
Offline checks for the rotation terminal: no network, no API key.

    ./.venv/bin/python test_rotation.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import newsfeed
import rotation
from rotation import PLAN_SCHEMA


def _feat(last, **kw):
    f = {"last": last, "prev_close": last, "gap_pct": 0.0, "ret_1d": 1.0, "ret_5d": 4.0,
         "ret_20d": 8.0, "ret_60d": 15.0, "rsi14": 60.0, "above_sma20": True,
         "above_sma50": True, "above_sma200": True, "sma50_vs_200": 5.0, "atr_pct": 3.0,
         "vol_ratio": 1.2, "avg_dollar_vol_m": 50.0, "pct_from_52w_high": -5.0,
         "pct_from_52w_low": 40.0, "bars": 250, "asof": "2026-10-20"}
    f.update(kw)
    return f


def _cand(t, last, held=False, news=3, sent=0.3, score=5.0, **kw):
    return {"ticker": t, "name": t, "held": held, "features": _feat(last, **kw),
            "news_sentiment": sent, "news_count": news, "screens": [], "pre_score": score,
            "headlines": [{"title": f"{t} beats estimates", "source": "test", "age_h": 2.0,
                           "sentiment": 0.5, "link": ""}] * min(news, 1),
            "sector": None, "market_cap_b": 10.0, "next_earnings": None, "days_to_earnings": None}


def _dossier(cash=500.0, positions=None, candidates=None):
    positions = positions or []
    equity = cash + sum(p["market_value"] for p in positions)
    for p in positions:
        p["weight"] = p["market_value"] / equity
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "for_session": "2026-10-21",
        "account": {"cash": cash, "equity": equity, "positions": positions,
                    "positions_value": equity - cash, "asof": "2026-10-20"},
        "constraints": {"max_positions": 4, "max_weight_per_name": 0.35, "min_order_usd": 150,
                        "max_buys": 3, "stop_pct_below_cost": 8, "earnings_buffer_days": 2,
                        "no_margin": True},
        "market": {"SPY": _feat(600)}, "market_headlines": [],
        "candidates": candidates or [], "n_headlines": 0, "n_tickers_scanned": 0,
    }


def _validate(plan):
    """Minimal structural check against PLAN_SCHEMA (keys + enums)."""
    for k in PLAN_SCHEMA["required"]:
        assert k in plan, f"plan missing {k}"
    assert plan["regime"] in ("risk_on", "neutral", "risk_off")
    for h in plan["holdings"]:
        assert h["action"] in ("HOLD", "TRIM", "SELL")
        assert isinstance(h["quantity"], int)
    for b in plan["buys"]:
        assert b["order_type"] in ("LIMIT", "MARKET") and b["confidence"] in ("low", "medium", "high")
        assert isinstance(b["quantity"], int) and b["quantity"] > 0


def test_rule_plan_buys_within_cash():
    cands = [_cand("AAA", 20.0, score=8), _cand("BBB", 50.0, score=7), _cand("CCC", 10.0, score=6)]
    plan = rotation.rule_plan(_dossier(cash=500.0, candidates=cands))
    _validate(plan)
    spent = sum(b["quantity"] * b["limit_price"] for b in plan["buys"])
    assert spent <= 500.0 + 1e-6, spent
    assert plan["cash_after_plan"] >= 0
    assert all(b["quantity"] * b["limit_price"] <= 0.35 * 500 + 1 for b in plan["buys"])
    assert len(plan["buys"]) <= 3


def test_rule_plan_sells_on_stop_breach():
    pos = [{"ticker": "XYZ", "quantity": 100, "avg_price": 10.0, "last": 9.0,
            "market_value": 900.0, "pnl_pct": -10.0, "has_data": True}]
    cands = [_cand("XYZ", 9.0, held=True)]
    plan = rotation.rule_plan(_dossier(cash=100.0, positions=pos, candidates=cands))
    _validate(plan)
    h = plan["holdings"][0]
    assert h["action"] == "SELL" and h["quantity"] == 100, h


def test_rule_plan_holds_concentrated_winner():
    pos = [{"ticker": "PURR", "quantity": 457, "avg_price": 8.99, "last": 9.3,
            "market_value": 4250.0, "pnl_pct": 3.4, "has_data": True}]
    cands = [_cand("PURR", 9.3, held=True, score=2.0), _cand("NEW", 25.0, score=9)]
    plan = rotation.rule_plan(_dossier(cash=400.0, positions=pos, candidates=cands))
    _validate(plan)
    assert plan["holdings"][0]["action"] == "HOLD"          # cash covers a min order
    plan = rotation.rule_plan(_dossier(cash=50.0, positions=pos, candidates=cands))
    h = plan["holdings"][0]
    assert h["action"] == "TRIM" and 0 < h["quantity"] <= 457 // 4, h   # only to fund one rotation


def test_rule_plan_skips_earnings_and_overbought():
    cands = [_cand("HOT", 20.0, score=9, rsi14=85.0),
             dict(_cand("ERN", 20.0, score=9), days_to_earnings=1, next_earnings="2026-10-21"),
             _cand("OK", 20.0, score=8)]
    plan = rotation.rule_plan(_dossier(cash=500.0, candidates=cands))
    assert [b["ticker"] for b in plan["buys"]] == ["OK"], plan["buys"]


def test_sanitise_caps_llm_sizes():
    d = _dossier(cash=300.0, candidates=[_cand("AAA", 100.0), _cand("BBB", 10.0)])
    plan = {"market_summary": "", "regime": "neutral", "holdings": [], "watchlist": [],
            "risks": [], "cash_after_plan": 0,
            "buys": [{"ticker": "AAA", "quantity": 10, "limit_price": 100.0, "order_type": "LIMIT",
                      "thesis": "", "catalyst": "", "risk": "", "stop_price": 90.0,
                      "confidence": "high"},
                     {"ticker": "BBB", "quantity": 5, "limit_price": 10.0, "order_type": "LIMIT",
                      "thesis": "", "catalyst": "", "risk": "", "stop_price": 9.0,
                      "confidence": "low"}]}
    out = rotation._sanitise(plan, d)
    assert out["buys"][0]["quantity"] == 3            # 300 / 100, cut from 10
    assert "size cut" in out["buys"][0]["risk"]
    assert len(out["buys"]) == 1                      # nothing left for BBB
    assert abs(out["cash_after_plan"] - 0.0) < 1e-6


def test_newsfeed_helpers():
    assert newsfeed.lexicon_sentiment("ACME beats estimates, raises guidance") > 0
    assert newsfeed.lexicon_sentiment("ACME plunges after guidance cut and SEC probe") < 0
    assert newsfeed.extract_tickers("Braze (NASDAQ:BRZE) soars; $MNDY too", known=["AAPL"]) == ["BRZE", "MNDY"]
    assert "AAPL" in newsfeed.extract_tickers("AAPL hits a record", known=["AAPL"])
    assert newsfeed.clean_name("Hyperliquid Strategies Inc") == "Hyperliquid Strategies"
    assert newsfeed.clean_name("Klaviyo, Inc. Series A") == "Klaviyo"
    assert json.dumps(PLAN_SCHEMA)   # schema is serialisable


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all rotation tests passed")
