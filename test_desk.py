"""
Offline checks for the live desk: scanner maths, risk guards, the simulator,
the rider's decision path and the terminal UI itself (driven headlessly).

    ./.venv/bin/python test_desk.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config

# isolate state files before anything imports them
_tmp = Path(tempfile.mkdtemp())
config.WAVE_STATE_PATH = _tmp / "wave_state.json"

import risk                                   # noqa: E402
import scanner                                # noqa: E402
import wave_rider                             # noqa: E402
from ibkr_broker import Quote                 # noqa: E402

ET = ZoneInfo("America/New_York")


# ----------------------------------------------------------------- fixtures
def _bars(prev_close=5.0, last=5.9, n=60, vol_each=40_000, day=None):
    """n five-minute bars for 'today' that drift up to `last`, plus an ORB pop."""
    day = day or datetime.now(ET).date()
    start = datetime.combine(day, scanner.OPEN, tzinfo=ET)
    idx = pd.date_range(start, periods=n, freq="5min")
    closes = [prev_close + (last - prev_close) * (i + 1) / n for i in range(n)]
    df = pd.DataFrame({"Open": closes, "High": [c * 1.004 for c in closes],
                       "Low": [c * 0.996 for c in closes], "Close": closes,
                       "Volume": [vol_each] * n}, index=idx)
    return df


def _daily(close=5.0, n=30, vol=300_000, end=None):
    end = end or (datetime.now(ET).date() - timedelta(days=1))
    idx = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.02, "Low": close * 0.98,
                         "Close": close, "Volume": vol}, index=idx)


def _signal(ticker="WAVE", price=5.9, qty=0):
    return scanner.Signal(ticker=ticker, name="Wave Co", price=price, change_pct=18.0, rel_vol=6.2,
                          dollar_vol_m=14.2, breakout="20d_high", news_count=3,
                          headline="Wave Co wins contract", score=19.0, atr_pct=4.0,
                          stop_pct=6.0, trail_pct=8.0, source="test", suggested_qty=qty,
                          entry_limit=round(price * 1.004, 2),
                          headlines=[{"title": "Wave Co wins contract", "source": "t", "age_h": 0.5, "link": ""}])


class FakeQuoteSim(wave_rider.SimBroker):
    """SimBroker with scripted quotes instead of Yahoo."""

    def __init__(self, prices, **kw):
        super().__init__(**kw)
        self.prices = dict(prices)

    async def quote(self, ticker):
        px = self.prices.get(ticker)
        return Quote(ticker=ticker, last=px) if px else None


# ----------------------------------------------------------------- scanner
def test_wave_features_detects_breakout():
    now = datetime.combine(datetime.now(ET).date(), scanner.OPEN, tzinfo=ET) + timedelta(hours=5)
    f = scanner.wave_features(_bars(), _daily(), now=now)
    assert f is not None
    assert f["breakout"] == "20d_high", f
    assert f["change_pct"] > 15
    assert f["rel_vol"] > 3, f["rel_vol"]
    assert scanner.passes(f, news_count=0)
    assert scanner.score_wave(f, 3) > scanner.score_wave(f, 0)


def test_wave_features_rejects_quiet_name():
    now = datetime.combine(datetime.now(ET).date(), scanner.OPEN, tzinfo=ET) + timedelta(hours=5)
    f = scanner.wave_features(_bars(prev_close=5.0, last=5.05, vol_each=2_000), _daily(), now=now)
    assert f is not None and not scanner.passes(f, news_count=0)


def test_session_fraction():
    d = datetime.now(ET).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    assert scanner.session_fraction(datetime.combine(d, scanner.OPEN, tzinfo=ET)) == 0.0
    mid = datetime.combine(d, scanner.OPEN, tzinfo=ET) + timedelta(hours=3, minutes=15)
    assert abs(scanner.session_fraction(mid) - 0.5) < 1e-6
    assert scanner.session_fraction(datetime.combine(d, scanner.CLOSE, tzinfo=ET)) == 1.0


# ----------------------------------------------------------------- risk
def test_size_by_risk():
    # 1.5% of 4400 = 66 risked; 6% stop on a $6.20 stock = 0.372/sh -> 177 sh, but cash caps at 145
    assert risk.size_by_risk(4400, 900, 6.20, 6.0, risk_pct=1.5, max_position_pct=50) == 145
    assert risk.size_by_risk(4400, 5000, 6.20, 6.0, risk_pct=1.5, max_position_pct=50) == 177
    assert risk.size_by_risk(4400, 5000, 6.20, 6.0, risk_pct=1.5, max_position_pct=10) == 70
    assert risk.size_by_risk(4400, 50, 6.20, 6.0) == 0           # below min order


def test_risk_guard_cash_account_and_daily_loss():
    st = risk.RiskState()
    g = risk.RiskGuard(st, account_type="cash", max_open=3, max_daily_loss_pct=4)
    ok, why = g.can_enter(open_positions=0, equity=4400, cash=900)
    assert ok, why
    st.record_sale(proceeds=800, realized=-50, was_day_trade=True, ticker="X")
    assert g.spendable_cash(900) == 100            # unsettled proceeds excluded
    ok, why = g.can_enter(open_positions=0, equity=4400, cash=900)
    assert not ok and "settled" in why
    st.record_sale(proceeds=0, realized=-140, was_day_trade=False, ticker="Y")   # -190 = 4.3%
    ok, why = g.can_enter(open_positions=0, equity=4400, cash=5000)
    assert not ok and "daily loss" in why
    # next business day: proceeds settle, counters reset
    st.roll(date.fromisoformat(st.day) + timedelta(days=3))
    assert st.unsettled_proceeds == 0 and st.realized_today == 0 and st.halted_reason == ""


def test_risk_guard_pdt_margin():
    st = risk.RiskState()
    g = risk.RiskGuard(st, account_type="margin", max_open=5)
    for t in "ABC":
        st.record_sale(100, 1, True, t)
    ok, why = g.can_enter(open_positions=0, equity=4400, cash=4000)
    assert not ok and "day trades" in why
    ok, _ = g.can_enter(open_positions=0, equity=30000, cash=4000)
    assert ok


# ----------------------------------------------------------------- simulator + rider
async def _rider_flow():
    if config.WAVE_STATE_PATH.exists():
        config.WAVE_STATE_PATH.unlink()
    broker = FakeQuoteSim({"WAVE": 5.90}, starting_cash=4000)
    rider = wave_rider.WaveRider(broker, telegram=wave_rider.Telegram(token="", chat_id=""), mode="manual")
    events = []
    rider.on_event = lambda k, p: events.append(k)
    await rider.refresh()
    sig = _signal()
    rider.signals["WAVE"] = sig
    sig.suggested_qty = risk.size_by_risk(rider.account.net_liq, rider.account.cash, sig.price, sig.stop_pct)
    assert sig.suggested_qty > 0

    ride = await rider.enter("WAVE")
    assert ride is not None and ride.status == "open", ride
    pos = next(p for p in rider.positions if p.ticker == "WAVE")
    assert pos.quantity == ride.qty
    assert len(rider.orders) == 1 and rider.orders[0].order_type == "TRAIL"

    # price runs, high-water follows; then it falls through the trail -> exit
    broker.prices["WAVE"] = 7.00
    await broker.tick()
    await rider.refresh()
    assert abs(rider.rides["WAVE"].high_water - 7.00) < 1e-9
    stop = rider.rides["WAVE"].trail_stop
    assert abs(stop - 7.00 * 0.92) < 1e-6
    broker.prices["WAVE"] = stop - 0.01
    exits = await broker.tick()
    assert exits and exits[0]["reason"] == "trailing stop"
    r = rider.rides["WAVE"]
    r.exit_price, r.exit_reason = exits[0]["price"], exits[0]["reason"]
    await rider.refresh()
    assert rider.rides["WAVE"].status == "closed"
    assert not rider.positions and not rider.orders
    assert rider.risk_state.realized_today > 0          # sold above entry
    assert "signal" not in events                        # we injected, never announced
    assert "order" in events and "refresh" in events

    # guard: a second entry is refused while paused
    rider.set_paused(True)
    rider.signals["WAVE"] = _signal(qty=10)
    rider.seen.pop("WAVE", None)
    assert await rider.enter("WAVE") is None
    rider.set_paused(False)

    # telegram command parsing path (no network: tg disabled -> send is a no-op)
    await rider.handle_command("mode", ["auto"])
    assert rider.mode == "auto"
    await rider.handle_command("pause", [])
    assert rider.paused
    st = json.loads(config.WAVE_STATE_PATH.read_text())
    assert st["mode"] == "auto" and st["paused"] is True and "WAVE" in st["rides"]


def test_rider_flow():
    asyncio.run(_rider_flow())


# ----------------------------------------------------------------- terminal UI (headless)
async def _ui_flow():
    import terminal
    from textual.widgets import DataTable

    if config.WAVE_STATE_PATH.exists():
        config.WAVE_STATE_PATH.unlink()        # fresh rider state for the UI run
    broker = FakeQuoteSim({"WAVE": 5.90, "ZAP": 3.10}, starting_cash=4000)
    rider = wave_rider.WaveRider(broker, telegram=wave_rider.Telegram(token="", chat_id=""), mode="signal")

    async def fake_scan(*a, **k):
        return [_signal("WAVE"), _signal("ZAP", price=3.10)]
    scanner.scan = fake_scan          # rider.scan_once calls scanner.scan at call time

    app = terminal.WaveDesk(sim=True, broker=broker, rider=rider)
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.3)
        await pilot.pause()
        assert app.rider is rider and app.broker is broker
        await rider.scan_once()
        await pilot.pause()
        waves = app.query_one("#waves-table", DataTable)
        assert waves.row_count == 2, waves.row_count
        assert app._selected_wave in ("WAVE", "ZAP")
        app._selected_wave = "WAVE"
        app.render_detail()

        # b -> order dialog -> place
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, terminal.OrderModal)
        await pilot.press("enter")
        await pilot.pause()
        await asyncio.sleep(0.3)
        await pilot.pause()
        assert any(p.ticker == "WAVE" for p in rider.positions), rider.positions
        pos_table = app.query_one("#pos-table", DataTable)
        assert pos_table.row_count == 1

        # mode cycles signal -> manual -> (auto asks) ; space pauses
        await pilot.press("m")
        await pilot.pause()
        assert rider.mode == "manual"
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, terminal.ConfirmModal)
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()                     # let the modal finish popping
        assert rider.mode == "signal"
        await pilot.press("space")
        await pilot.pause()
        assert rider.paused

        # f on the position -> confirm -> flat
        pos_table.focus()
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        assert isinstance(app.screen, terminal.ConfirmModal)
        await pilot.press("y")
        await pilot.pause()
        await asyncio.sleep(0.3)
        await pilot.pause()
        assert not rider.positions
        assert rider.rides["WAVE"].status == "closed"

        app.save_screenshot(str(_tmp / "desk.svg"))
        rider.stop()
        await pilot.press("q")
    return _tmp / "desk.svg"


def test_ui_flow():
    path = asyncio.run(_ui_flow())
    assert path.exists() and path.stat().st_size > 1000
    print(f"    screenshot: {path}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all desk tests passed")
