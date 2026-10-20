"""
Wave desk, in the browser.

    python web.py                 # http://127.0.0.1:8750 - connects to TWS / IB Gateway
    python web.py --sim           # simulator instead of IBKR
    python web.py --mode manual   # signal | manual | auto
    python web.py --port 8800

Serves the dashboard on localhost only. The backend is the same rider,
scanner and IBKR broker the terminal uses; the page talks to it over a
WebSocket (state pushes) and a few POST endpoints (actions).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
import scanner
from telegram_bot import Telegram
from wave_rider import MODES, WaveRider, make_broker

STATIC = Path(__file__).parent / "static"

ARGS = {"sim": False, "mode": None, "watch": []}


class Desk:
    """One rider, one broker, many browser tabs."""

    def __init__(self):
        self.broker = None
        self.rider: Optional[WaveRider] = None
        self.log: deque = deque(maxlen=300)
        self.clients: List[WebSocket] = []
        self.plan: Optional[dict] = None
        self.plan_running = False
        self._tasks: List[asyncio.Task] = []
        self._dirty = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        self.broker, _ = await make_broker(ARGS["sim"], log=self.add_log)
        self.rider = WaveRider(self.broker, Telegram(), mode=ARGS["mode"], watchlist=ARGS["watch"],
                               on_event=self.on_event)
        self.add_log(f"connected to {self.broker.name} "
                     f"({'paper' if getattr(self.broker, 'is_paper', True) else 'LIVE'}) · mode {self.rider.mode}"
                     f" · Telegram {'on' if self.rider.tg.enabled else 'off'}")
        self._tasks = [asyncio.create_task(self.rider.run()),
                       asyncio.create_task(self.rider.telegram_loop()),
                       asyncio.create_task(self._broadcaster())]

    async def stop(self):
        if self.rider:
            self.rider.stop()
            self.rider.save()
        for t in self._tasks:
            t.cancel()
        if self.broker:
            try:
                self.broker.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------ events
    def add_log(self, msg: str):
        self.log.append({"t": scanner.now_et().strftime("%H:%M:%S"), "msg": str(msg)})
        self._dirty.set()

    def on_event(self, kind: str, payload: dict):
        if kind == "log":
            self.add_log(payload["msg"])
            return
        if kind == "signal":
            s = payload["signal"]
            self.add_log(f"wave {s.ticker} {s.price:.2f} {s.change_pct:+.1f}% rvol {s.rel_vol:.1f}x via {s.breakout_label}")
        self._dirty.set()

    async def _broadcaster(self):
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await asyncio.sleep(0.15)          # coalesce bursts
            await self.broadcast()

    async def broadcast(self):
        if not self.clients:
            return
        msg = json.dumps({"type": "state", "state": self.snapshot()}, default=str)
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.remove(ws)

    # ------------------------------------------------------------ state
    def snapshot(self) -> dict:
        r = self.rider
        b = self.broker
        if r is None or b is None:
            return {"ready": False, "log": list(self.log)}
        a = r.account
        rides = {t: x for t, x in r.rides.items() if x.status != "closed"}
        positions = []
        for p in r.positions:
            ride = rides.get(p.ticker)
            positions.append({
                "ticker": p.ticker, "quantity": p.quantity, "avg_price": p.avg_price, "last": p.last,
                "market_value": p.market_value, "unrealized_pnl": p.unrealized_pnl, "pnl_pct": p.pnl_pct,
                "trail_pct": ride.trail_pct if ride and ride.status == "open" else None,
                "trail_stop": ride.trail_stop if ride and ride.status == "open" else None,
                "high_water": ride.high_water if ride else None,
            })
        return {
            "ready": True,
            "broker": {"name": b.name, "paper": bool(getattr(b, "is_paper", True)),
                       "connected": bool(getattr(b, "connected", False)),
                       "host": f"{config.IBKR_HOST}:{config.IBKR_PORT}" if b.name == "ibkr" else "simulator"},
            "mode": r.mode, "paused": r.paused, "halted": r.risk_state.halted_reason,
            "account": asdict(a),
            "risk": {"realized_today": r.risk_state.realized_today,
                     "unsettled": r.risk_state.unsettled_proceeds,
                     "day_trades": len(r.risk_state.day_trades),
                     "max_open": r.guard.max_open, "account_type": r.guard.account_type,
                     "risk_pct": config.WAVE_RISK_PCT},
            "clock": {"now_et": scanner.now_et().strftime("%H:%M:%S"), "market_open": scanner.market_open(),
                      "last_scan": r.last_scan.astimezone(scanner.ET).strftime("%H:%M:%S") if r.last_scan else None,
                      "scanning": r.scanning, "interval": config.WAVE_SCAN_INTERVAL},
            "signals": [s.to_dict() | {"breakout_label": s.breakout_label}
                        for s in sorted(r.signals.values(), key=lambda s: s.score, reverse=True)],
            "positions": positions,
            "orders": [asdict(o) for o in r.orders],
            "rides": {t: asdict(x) | {"trail_stop": x.trail_stop} for t, x in r.rides.items()},
            "watchlist": r.watchlist,
            "log": list(self.log),
            "plan": self.plan, "plan_running": self.plan_running,
            "limits": {"min_price": config.WAVE_MIN_PRICE, "max_price": config.WAVE_MAX_PRICE,
                       "min_rel_vol": config.WAVE_MIN_REL_VOL, "min_change": config.WAVE_MIN_CHANGE_PCT,
                       "trail": config.WAVE_TRAIL_PCT, "stop": config.WAVE_STOP_PCT},
        }

    # ------------------------------------------------------------ actions
    def need(self) -> WaveRider:
        if self.rider is None:
            raise HTTPException(503, "still connecting")
        return self.rider

    async def build_plan(self):
        if self.plan_running:
            return
        self.plan_running = True
        self._dirty.set()
        r = self.need()
        loop = asyncio.get_running_loop()

        def work():
            import rotation
            portfolio = {"cash": r.account.cash,
                         "positions": [{"ticker": p.ticker, "quantity": p.quantity, "avg_price": p.avg_price}
                                       for p in r.positions]}
            dossier = rotation.build_dossier(portfolio, rotation.load_universe(), log=self.add_log)
            plan = rotation.decide(dossier, log=self.add_log)
            plan["for_session"] = dossier["for_session"]
            config.PLANS_DIR.mkdir(exist_ok=True)
            (config.PLANS_DIR / f"{dossier['for_session']}.json").write_text(json.dumps(plan, indent=1))
            (config.PLANS_DIR / f"{dossier['for_session']}.dossier.json").write_text(
                json.dumps(dossier, indent=1, default=str))
            return plan
        try:
            self.plan = await loop.run_in_executor(None, work)
            self.add_log(f"plan ready: {len(self.plan.get('buys', []))} buys, regime {self.plan.get('regime')}")
        except Exception as e:
            self.add_log(f"plan failed: {e}")
        finally:
            self.plan_running = False
            self._dirty.set()


desk = Desk()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await desk.start()
    try:
        yield
    finally:
        await desk.stop()


app = FastAPI(title="Wave desk", lifespan=lifespan, docs_url=None, redoc_url=None)


# ----------------------------------------------------------------- pages + stream
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def state():
    return JSONResponse(json.loads(json.dumps(desk.snapshot(), default=str)))


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    desk.clients.append(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "state", "state": desk.snapshot()}, default=str))
        while True:
            # keep-alive; clients send "ping" every so often, the server pushes a fresh clock
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=5)
            except asyncio.TimeoutError:
                pass
            await websocket.send_text(json.dumps({"type": "state", "state": desk.snapshot()}, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in desk.clients:
            desk.clients.remove(websocket)


# ----------------------------------------------------------------- actions
class BuyReq(BaseModel):
    ticker: str
    qty: int
    limit: Optional[float] = None
    trail_pct: float = 0.0
    confirm_live: bool = False


class TickerReq(BaseModel):
    ticker: str


class TrailReq(BaseModel):
    ticker: str
    trail_pct: float


class ModeReq(BaseModel):
    mode: str


class PauseReq(BaseModel):
    paused: bool


@app.post("/api/buy")
async def buy(req: BuyReq):
    r = desk.need()
    live = not getattr(desk.broker, "is_paper", True)
    if live and not req.confirm_live:
        raise HTTPException(400, "live account: confirm_live required")
    if req.qty <= 0:
        raise HTTPException(400, "qty must be at least 1")
    t = req.ticker.upper()
    if req.trail_pct and req.trail_pct > 0:
        ride = await r.enter(t, qty=req.qty, limit=req.limit, trail_pct=req.trail_pct, force=True)
        return {"ok": ride is not None}
    try:
        await desk.broker.order(t, "BUY", req.qty, req.limit)
        desk.add_log(f"{t}: BUY {req.qty} {'lmt %.2f' % req.limit if req.limit else 'mkt'} sent (no trail)")
    except Exception as e:
        desk.add_log(f"{t}: order failed: {e}")
        raise HTTPException(500, str(e))
    await r.refresh()
    return {"ok": True}


@app.post("/api/flat")
async def flat(req: TickerReq):
    r = desk.need()
    return {"ok": await r.flat(req.ticker, "dashboard flat")}


@app.post("/api/trail")
async def trail(req: TrailReq):
    r = desk.need()
    if req.trail_pct <= 0:
        raise HTTPException(400, "trail must be above 0")
    return {"ok": await r.trail(req.ticker, req.trail_pct)}


@app.post("/api/cancel")
async def cancel(req: TickerReq):
    desk.need()
    n = await desk.broker.cancel(req.ticker.upper())
    desk.add_log(f"{req.ticker.upper()}: {n} order(s) cancelled")
    await desk.rider.refresh()
    return {"ok": True, "cancelled": n}


@app.post("/api/mode")
async def mode(req: ModeReq):
    r = desk.need()
    if req.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")
    return {"mode": r.set_mode(req.mode)}


@app.post("/api/pause")
async def pause(req: PauseReq):
    r = desk.need()
    r.set_paused(req.paused)
    if not req.paused:
        r.risk_state.halted_reason = ""
    return {"paused": r.paused}


@app.post("/api/scan")
async def scan():
    r = desk.need()
    if r.scanning:
        return {"ok": False, "reason": "already scanning"}
    asyncio.create_task(r.scan_once())
    desk._dirty.set()
    return {"ok": True}


@app.post("/api/watch")
async def watch(req: TickerReq):
    r = desk.need()
    for t in req.ticker.replace(",", " ").split():
        t = t.upper()
        if t not in r.watchlist:
            r.watchlist.append(t)
    desk.add_log(f"watching {', '.join(r.watchlist)}")
    return {"watchlist": r.watchlist}


@app.post("/api/plan")
async def plan():
    desk.need()
    asyncio.create_task(desk.build_plan())
    return {"ok": True}


# ----------------------------------------------------------------- entry
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="simulator instead of TWS / IB Gateway")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--watch", help="extra tickers, comma-separated")
    ap.add_argument("--port", type=int, default=8750)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (keep it on localhost)")
    args = ap.parse_args(argv)
    ARGS.update(sim=args.sim, mode=args.mode,
                watch=[t.strip().upper() for t in args.watch.split(",") if t.strip()] if args.watch else [])
    print(f"Wave desk  ->  http://{args.host}:{args.port}   ({'simulator' if args.sim else 'IBKR via TWS/Gateway on ' + config.IBKR_HOST + ':' + str(config.IBKR_PORT)})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
