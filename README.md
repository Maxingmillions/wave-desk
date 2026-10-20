# Wave desk

A trading terminal for one person and one Interactive Brokers account.

It does two things:

1. **Rides waves during the session.** Scans for low-priced small caps with a
   volume influx and a breakout, sizes a position by risk, enters with an
   IBKR-native trailing stop attached, and tells you on Telegram. By hand,
   by reply, or automatically.
2. **Plans rotations after the close.** Reads the day's stock news, prices a
   candidate pool, and writes a buy-leaning plan for the next session. You
   can buy straight from the plan inside the desk.

It never places an order you did not ask for unless you put it in `auto`
mode, and it shows **LIVE** in red whenever it is connected to a live port.

---

## Setup (5 minutes)

```
git clone https://github.com/Maxingmillions/wave-desk.git
cd wave-desk
bash setup.sh
```

`setup.sh` finds a Python 3.10+, creates `.venv`, installs the
requirements, creates `.env` from the example, and runs the offline tests.
Then edit `.env`:

| Key | What it is |
|---|---|
| `IBKR_PORT` | 7497 paper / 7496 live (TWS); 4002 / 4001 (IB Gateway) |
| `WAVE_MODE` | `signal`, `manual` or `auto` (see below) |
| `ACCOUNT_TYPE` | `cash` or `margin`; picks which settlement / day-trade rule applies |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional; signals and commands |
| `ANTHROPIC_API_KEY` | optional; lets `plan.py` ask Claude for the rotation plan |

**TWS, once:** Global Configuration → API → Settings: enable *ActiveX and
Socket Clients*, add `127.0.0.1` to trusted IPs, note the port. Log in to
the paper account first.

---

## Run

```
./.venv/bin/python terminal.py --sim        # rehearse: Yahoo data, simulated fills + trails
./.venv/bin/python terminal.py              # connect to TWS / IB Gateway (falls back to --sim)
./.venv/bin/python terminal.py --mode auto  # override WAVE_MODE
./.venv/bin/python terminal.py --headless   # no UI: rider + Telegram only (for a server)
./.venv/bin/python plan.py                  # tomorrow's rotation plan, standalone
```

### The desk

Left: the scanner board and the selected wave's headlines and sizing.
Right: positions with their trailing stops, working orders, the log.

| Key | Does |
|---|---|
| `b` | buy the selected wave; the dialog shows shares, limit, trail and cost before you confirm |
| `f` | flatten the selected position (cancel its orders, sell at market) |
| `t` | set or change a trailing stop on a position |
| `m` | cycle mode signal → manual → auto (auto asks first) |
| `space` | pause / resume new entries |
| `r` | scan now |
| `p` | build the rotation plan; pick a buy to pre-fill the order dialog |
| `w` | watch a ticker (added to every scan) |
| `c` | cancel working orders on a name |
| `q` | quit (trailing stops stay at IBKR) |

### What a wave is

Thresholds in `.env`, defaults in `config.py`.

- price between `WAVE_MIN_PRICE` and `WAVE_MAX_PRICE`, market cap under `WAVE_MAX_MCAP_B`
- relative volume ≥ `WAVE_MIN_REL_VOL` versus the 20-day average, adjusted for how much of the session has elapsed
- up at least `WAVE_MIN_CHANGE_PCT` on the day and not more than `WAVE_MAX_CHANGE_PCT`
- pushing through a level: the 20-day high, the high of the day, or the opening range; or carrying a news influx (2+ fresh headlines)

Candidates come from IBKR's server-side scanner (top % gainers, hot by
volume, most active, in the price band) or, without TWS, from Yahoo's
screeners. Bars are 1-minute from IBKR, 5-minute from Yahoo. Each wave gets
a score, an ATR-based stop and trail, and a suggested size.

### How a wave is ridden

- **Size by risk.** A stop-out loses `WAVE_RISK_PCT` of equity, capped by `WAVE_MAX_POSITION_PCT` and by settled cash. Whole shares.
- **Entry + trail in one bracket.** A limit a touch above last, with a `TRAIL` sell attached at IBKR (GTC). The stop keeps working if the desk is closed.
- **Guards.** `WAVE_MAX_OPEN` positions; a daily loss halt at `WAVE_MAX_DAILY_LOSS_PCT`; only settled cash in a cash account (T+1); the day-trade count in a margin account under $25k. Refusals are logged and sent to Telegram with the reason.
- **Optional exit before the close** (`WAVE_EXIT_BEFORE_CLOSE_MIN`); default is to hold.

| Mode | When the scanner finds a wave |
|---|---|
| `signal` | logged and sent to Telegram; nothing placed |
| `manual` | same, pre-sized; press **b** or reply `/buy ABCD [qty]` to enter |
| `auto` | bracket placed within the guards; **space** pauses, **f** flattens |

### Telegram

Create a bot with @BotFather, message it once, put the token and your chat
id in `.env`. Pushed: waves, fills, stops, refusals. Commands: `/signals`,
`/positions`, `/buy ABCD [qty]`, `/flat ABCD`, `/mode auto`, `/pause`,
`/resume`, `/status`, `/scan`. Only your chat id is honoured.

---

## The rotation planner (`plan.py`)

Runs after the close. News from Yahoo Finance RSS, Google News, Finviz,
WSJ Markets and (with a key) Alpha Vantage; prices and technicals from
yfinance; candidates from `universe.txt`, your holdings, Yahoo screeners
and tickers named in headlines. With `ANTHROPIC_API_KEY` set, Claude reads
the dossier and returns a structured plan (HOLD / TRIM / SELL per holding
with a reason, sized buys with limit, stop, catalyst and risk); without it,
a rule-based planner runs. Sizes are re-checked against cash either way.

```
./.venv/bin/python plan.py                      # holdings from portfolio.json
./.venv/bin/python plan.py --positions PURR:457:8.99 --cash 176
./.venv/bin/python plan.py --show plans/2026-10-21.json
```

`.github/workflows/rotation.yml` runs it weekdays after the close and
uploads the plan as an artifact (secrets: `PORTFOLIO_JSON`, optional
`ANTHROPIC_API_KEY`, `ALPHAVANTAGE_API_KEY`).

In a Claude Code session with the IBKR connector, `/rotate` pulls live
positions, runs the gather step, and stages the chosen orders as
review-and-approve instructions.

---

## Files

| File | Role |
|---|---|
| `terminal.py`, `terminal.tcss` | the desk (Textual) |
| `scanner.py` | wave detection and scoring |
| `wave_rider.py` | rides, sizing, reconciliation, Telegram commands, simulator |
| `risk.py` | size by risk, guards, settlement / PDT state |
| `ibkr_broker.py` | TWS / IB Gateway via ib_async |
| `telegram_bot.py` | push + long-poll commands |
| `plan.py`, `rotation.py` | the rotation planner |
| `newsfeed.py`, `market_data.py` | news sources, prices, technicals, screeners |
| `test_desk.py`, `test_rotation.py` | offline tests; the desk is driven headlessly |

State lives in `wave_state.json`, `portfolio.json`, `meta_cache.json` and
`plans/`, all gitignored.

---

## Before you trust `auto`

Run `--sim` for a few sessions, then the paper port, and read the log.
The simulator fills at Yahoo's last quote with no spread; real small caps
are wider and faster. The IBKR order path is written against the ib_async
API and tested against the simulator, not a live gateway, so your first
paper orders are the real test.
