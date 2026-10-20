"""
Offline checks for the browser dashboard backend (web.py): the API surface
against the simulator with a scripted scanner. No network, no TWS.

    ./.venv/bin/python test_web.py
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import config

config.WAVE_STATE_PATH = Path(tempfile.mkdtemp()) / "wave_state.json"

import scanner                                  # noqa: E402
import wave_rider                               # noqa: E402
import test_desk                                # noqa: E402
import web                                      # noqa: E402


async def _flow():
    import httpx

    broker = test_desk.FakeQuoteSim({"WAVE": 5.90, "ZAP": 3.10}, starting_cash=4400)

    async def fake_make_broker(sim, log=print):
        return broker, True

    async def fake_scan(*a, **k):
        return [test_desk._signal("WAVE"), test_desk._signal("ZAP", price=3.10)]

    web.make_broker = fake_make_broker
    scanner.scan = fake_scan
    web.ARGS.update(sim=True, mode="manual", watch=[])

    async with web.lifespan(web.app):
        transport = httpx.ASGITransport(app=web.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://desk") as c:
            r = await c.get("/")
            assert r.status_code == 200 and "Wave desk" in r.text

            s = (await c.get("/api/state")).json()
            assert s["ready"] and s["broker"]["name"] == "sim" and s["mode"] == "manual"

            assert (await c.post("/api/scan")).json()["ok"]
            await asyncio.sleep(0.5)
            s = (await c.get("/api/state")).json()
            assert [x["ticker"] for x in s["signals"]] == ["WAVE", "ZAP"]
            assert all(x["suggested_qty"] > 0 for x in s["signals"])

            r = await c.post("/api/buy", json={"ticker": "ZAP", "qty": 300, "limit": 3.11, "trail_pct": 8})
            assert r.status_code == 200 and r.json()["ok"]
            s = (await c.get("/api/state")).json()
            assert s["positions"][0]["ticker"] == "ZAP" and s["positions"][0]["trail_pct"] == 8
            assert s["orders"][0]["order_type"] == "TRAIL"

            r = await c.post("/api/buy", json={"ticker": "ZAP", "qty": 0})
            assert r.status_code == 400

            r = await c.post("/api/trail", json={"ticker": "ZAP", "trail_pct": 10})
            assert r.json()["ok"]
            s = (await c.get("/api/state")).json()
            assert s["positions"][0]["trail_pct"] == 10

            assert (await c.post("/api/mode", json={"mode": "auto"})).json()["mode"] == "auto"
            assert (await c.post("/api/mode", json={"mode": "bogus"})).status_code == 400
            assert (await c.post("/api/pause", json={"paused": True})).json()["paused"] is True
            assert (await c.post("/api/watch", json={"ticker": "abc, def"})).json()["watchlist"] == ["ABC", "DEF"]

            r = await c.post("/api/cancel", json={"ticker": "ZAP"})
            assert r.json()["cancelled"] == 1
            r = await c.post("/api/flat", json={"ticker": "ZAP"})
            assert r.json()["ok"]
            s = (await c.get("/api/state")).json()
            assert s["positions"] == [] and s["orders"] == []
            assert any("flattened" in l["msg"] for l in s["log"])


def test_web_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_web_flow()
    print("ok  test_web_flow")
    print("all web tests passed")
