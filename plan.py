"""
Rotation planner -- read the day's news, decide tomorrow's orders.

    python plan.py                    # full run: news -> prices -> plan, pretty output
    python plan.py --no-llm           # skip Claude, use the rule-based planner
    python plan.py --dossier-only     # gather + save the dossier, decide nothing
    python plan.py --positions PURR:457:8.99 --cash 176   # override portfolio.json
    python plan.py --json             # print the plan as JSON only (for piping)
    python plan.py --show plans/2026-10-21.json           # re-render a saved plan

The interactive desk (scanner, live positions, order entry) is terminal.py;
it can run this planner from the plan screen.

Reads holdings from portfolio.json (see portfolio.example.json) and the
candidate pool from universe.txt. Writes plans/<session-date>.json plus a
markdown twin, and the raw dossier next to it.

This never sends an order. Review the plan, then submit it yourself (or let
the IBKR connector turn it into review-and-approve instructions).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import config
import rotation


# ----------------------------------------------------------------- rendering
def _console():
    try:
        import os
        from rich.console import Console
        # when piped (CI logs, a file) there is no terminal width to respect
        return Console(width=None if sys.stdout.isatty() else int(os.getenv("COLUMNS", "150")))
    except ImportError:
        return None


def _fmt(x, nd=1, suffix=""):
    if x is None:
        return "-"
    try:
        return f"{x:.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(x)


def render_rich(dossier: dict, plan: dict, con) -> None:
    from rich.panel import Panel
    from rich.table import Table
    from rich import box

    acct = dossier["account"]
    con.rule(f"[bold]ROTATION TERMINAL[/]  plan for {dossier['for_session']}  "
             f"(generated {dossier['generated_at'][:16]}Z, engine {plan.get('_engine', '?')})")

    # account
    t = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    for c in ("ticker", "qty", "avg", "last", "P&L%", "value", "weight", "action", "reason"):
        t.add_column(c, justify="right" if c not in ("ticker", "action", "reason") else "left")
    actions = {h["ticker"]: h for h in plan.get("holdings", [])}
    for p in acct["positions"]:
        a = actions.get(p["ticker"], {})
        act = a.get("action", "HOLD")
        colour = {"SELL": "red", "TRIM": "yellow"}.get(act, "green")
        t.add_row(p["ticker"], _fmt(p["quantity"], 0), _fmt(p.get("avg_price"), 2),
                  _fmt(p.get("last"), 2), _fmt(p.get("pnl_pct"), 1, "%"),
                  _fmt(p["market_value"], 0), _fmt((p.get("weight") or 0) * 100, 0, "%"),
                  f"[{colour}]{act}" + (f" {a['quantity']}" if a.get("quantity") else "") + "[/]",
                  a.get("reason", ""))
    con.print(Panel(t, title=f"Account  cash ${acct['cash']:,.2f}  equity ${acct['equity']:,.2f}  "
                             f"regime [bold]{plan.get('regime', '?')}[/]", border_style="cyan"))

    # market
    con.print(Panel(plan.get("market_summary", ""), title="Market read", border_style="blue"))
    mh = dossier.get("market_headlines", [])[:10]
    if mh:
        t = Table(box=None, show_header=False, pad_edge=False)
        for h in mh:
            t.add_row(f"[dim]{_fmt(h.get('age_h'), 0, 'h')}[/]", h["title"][:120],
                      f"[dim]{h['source'][:18]}[/]")
        con.print(Panel(t, title="Top headlines", border_style="blue"))

    # buys
    if plan.get("buys"):
        t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
        for c in ("ticker", "qty", "limit", "type", "stop", "conf", "catalyst / thesis / risk"):
            t.add_column(c, justify="right" if c in ("qty", "limit", "stop") else "left")
        for b in plan["buys"]:
            t.add_row(f"[bold green]{b['ticker']}[/]", str(b["quantity"]), _fmt(b["limit_price"], 2),
                      b["order_type"], _fmt(b["stop_price"], 2), b["confidence"],
                      f"[bold]{b['catalyst']}[/]\n{b['thesis']}\n[yellow]risk:[/] {b['risk']}")
        con.print(Panel(t, title=f"BUY  (cash after plan ${plan.get('cash_after_plan', 0):,.2f})",
                        border_style="green"))
    else:
        con.print(Panel("No buys proposed for this session.", title="BUY", border_style="green"))

    # candidates
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False, title="Candidate board (pre-rank)")
    for c in ("#", "ticker", "score", "last", "1d%", "5d%", "20d%", "RSI", "vol x", "news", "sent", "earn", "top headline"):
        t.add_column(c, justify="left" if c in ("ticker", "top headline") else "right",
                     no_wrap=True, overflow="ellipsis", max_width=60 if c == "top headline" else None)
    for i, r in enumerate(dossier["candidates"][:20], 1):
        f = r["features"]
        hl = r["headlines"][0]["title"][:70] if r["headlines"] else ""
        mark = "[cyan]*[/]" if r["held"] else ""
        t.add_row(str(i), f"{r['ticker']}{mark}", _fmt(r["pre_score"], 1), _fmt(f["last"], 2),
                  _fmt(f["ret_1d"]), _fmt(f["ret_5d"]), _fmt(f["ret_20d"]), _fmt(f["rsi14"], 0),
                  _fmt(f.get("vol_ratio")), str(r["news_count"]), _fmt(r["news_sentiment"], 2),
                  "-" if r.get("days_to_earnings") is None else f"{r['days_to_earnings']}d", hl)
    con.print(t)

    if plan.get("watchlist"):
        con.print(Panel("\n".join(f"[bold]{w['ticker']}[/]  {w['note']}" for w in plan["watchlist"]),
                        title="Watch", border_style="magenta"))
    if plan.get("risks"):
        con.print(Panel("\n".join(f"- {r}" for r in plan["risks"]), title="Risks", border_style="red"))
    con.print("[dim]Nothing has been sent to a broker. Review, then submit.[/]")


def render_plain(dossier: dict, plan: dict) -> str:
    """Markdown twin of the rich view; also what gets saved next to the JSON."""
    acct = dossier["account"]
    out = [f"# Rotation plan for {dossier['for_session']}",
           f"_generated {dossier['generated_at']} · engine {plan.get('_engine', '?')} · "
           f"regime **{plan.get('regime', '?')}**_", "",
           f"**Cash** ${acct['cash']:,.2f} · **Equity** ${acct['equity']:,.2f} · "
           f"**Cash after plan** ${plan.get('cash_after_plan', 0):,.2f}", "",
           "## Market", plan.get("market_summary", ""), "",
           "## Holdings", "", "| ticker | qty | avg | last | P&L% | weight | action | reason |",
           "|---|---:|---:|---:|---:|---:|---|---|"]
    actions = {h["ticker"]: h for h in plan.get("holdings", [])}
    for p in acct["positions"]:
        a = actions.get(p["ticker"], {})
        out.append(f"| {p['ticker']} | {_fmt(p['quantity'], 0)} | {_fmt(p.get('avg_price'), 2)} | "
                   f"{_fmt(p.get('last'), 2)} | {_fmt(p.get('pnl_pct'), 1)} | "
                   f"{_fmt((p.get('weight') or 0) * 100, 0)}% | {a.get('action', 'HOLD')}"
                   f"{' ' + str(a['quantity']) if a.get('quantity') else ''} | {a.get('reason', '')} |")
    out += ["", "## Buys", ""]
    if plan.get("buys"):
        out += ["| ticker | qty | limit | type | stop | conf | catalyst | thesis | risk |",
                "|---|---:|---:|---|---:|---|---|---|---|"]
        for b in plan["buys"]:
            out.append(f"| **{b['ticker']}** | {b['quantity']} | {_fmt(b['limit_price'], 2)} | "
                       f"{b['order_type']} | {_fmt(b['stop_price'], 2)} | {b['confidence']} | "
                       f"{b['catalyst']} | {b['thesis']} | {b['risk']} |")
    else:
        out.append("_none_")
    if plan.get("watchlist"):
        out += ["", "## Watch", ""] + [f"- **{w['ticker']}** {w['note']}" for w in plan["watchlist"]]
    out += ["", "## Risks", ""] + [f"- {r}" for r in plan.get("risks", [])]
    out += ["", "## Candidate board", "",
            "| # | ticker | score | last | 1d% | 5d% | 20d% | RSI | news | sent | earnings | top headline |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for i, r in enumerate(dossier["candidates"][:25], 1):
        f = r["features"]
        hl = r["headlines"][0]["title"].replace("|", "/") if r["headlines"] else ""
        out.append(f"| {i} | {r['ticker']}{'*' if r['held'] else ''} | {r['pre_score']} | "
                   f"{_fmt(f['last'], 2)} | {_fmt(f['ret_1d'])} | {_fmt(f['ret_5d'])} | "
                   f"{_fmt(f['ret_20d'])} | {_fmt(f['rsi14'], 0)} | {r['news_count']} | "
                   f"{_fmt(r['news_sentiment'], 2)} | {r.get('next_earnings') or '-'} | {hl} |")
    out += ["", "_Nothing has been sent to a broker._", ""]
    return "\n".join(out)


# ----------------------------------------------------------------- main
def _parse_positions(spec: str) -> list:
    out = []
    for item in spec.split(","):
        parts = item.strip().split(":")
        if len(parts) < 2:
            raise SystemExit(f"bad --positions entry {item!r}; use TICKER:QTY[:AVG]")
        out.append({"ticker": parts[0].upper(), "quantity": float(parts[1]),
                    "avg_price": float(parts[2]) if len(parts) > 2 else None})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions", help="TICKER:QTY[:AVG],... overrides portfolio.json")
    ap.add_argument("--cash", type=float, help="available cash, overrides portfolio.json")
    ap.add_argument("--watch", help="extra tickers to consider, comma-separated")
    ap.add_argument("--hours", type=int, help=f"news lookback (default {config.ROTATION_NEWS_HOURS})")
    ap.add_argument("--no-discover", action="store_true", help="skip Yahoo screener discovery")
    ap.add_argument("--no-info", action="store_true", help="skip sector/earnings lookups (faster)")
    ap.add_argument("--no-llm", action="store_true", help="rule-based plan only")
    ap.add_argument("--dossier-only", action="store_true", help="gather data, make no decision")
    ap.add_argument("--json", action="store_true", help="print plan JSON only")
    ap.add_argument("--show", metavar="PLAN.json", help="re-render a saved plan and exit")
    ap.add_argument("--out", help="output directory (default plans/)")
    args = ap.parse_args(argv)

    con = None if args.json else _console()
    log = (lambda m: con.print(f"[dim]{m}[/]")) if con else (lambda m: print(m, file=sys.stderr))
    out_dir = Path(args.out) if args.out else config.PLANS_DIR
    out_dir.mkdir(exist_ok=True)

    if args.show:
        plan = json.loads(Path(args.show).read_text())
        dossier = json.loads(Path(args.show).with_name(
            Path(args.show).name.replace(".json", ".dossier.json")).read_text())
        if con:
            render_rich(dossier, plan, con)
        else:
            print(json.dumps(plan, indent=2))
        return

    portfolio = rotation.load_portfolio()
    if args.positions:
        portfolio["positions"] = _parse_positions(args.positions)
    if args.cash is not None:
        portfolio["cash"] = args.cash
    if not portfolio["positions"] and not portfolio["cash"]:
        log("No portfolio.json and no --positions/--cash given; planning for an empty account.")

    universe = rotation.load_universe()
    if args.watch:
        universe += [t.strip().upper() for t in args.watch.split(",") if t.strip()]

    dossier = rotation.build_dossier(portfolio, universe, discover=not args.no_discover,
                                     lookback_hours=args.hours, with_info=not args.no_info, log=log)
    stem = out_dir / dossier["for_session"]
    (stem.with_suffix(".dossier.json")).write_text(json.dumps(dossier, indent=1, default=str))
    log(f"Dossier: {dossier['n_headlines']} headlines, {dossier['n_tickers_scanned']} tickers priced "
        f"-> {stem.with_suffix('.dossier.json')}")
    if args.dossier_only:
        if args.json:
            print(json.dumps(dossier, default=str))
        return

    plan = rotation.decide(dossier, use_llm=False if args.no_llm else None, log=log)
    plan["for_session"] = dossier["for_session"]
    plan["generated_at"] = dossier["generated_at"]
    stem.with_suffix(".json").write_text(json.dumps(plan, indent=1))
    stem.with_suffix(".md").write_text(render_plain(dossier, plan))

    if args.json:
        print(json.dumps(plan, indent=2))
    elif con:
        render_rich(dossier, plan, con)
        con.print(f"[dim]Saved {stem.with_suffix('.json')} and .md[/]")
    else:
        print(render_plain(dossier, plan))


if __name__ == "__main__":
    main()
