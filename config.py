"""
Wave desk configuration.

Everything tunable lives here, read from .env (or the environment).
Nothing in this file talks to a network.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _secret(name: str, default: str = "") -> str:
    """
    Read config from, in order:
      1. environment / .env      (local runs, GitHub Actions)
      2. st.secrets              (Streamlit Community Cloud)
      3. the supplied default

    Streamlit Cloud does not export secrets as env vars, so the dashboard needs
    the second lookup. Wrapped in try/except so engine.py never needs streamlit.
    """
    val = os.getenv(name)
    if val:
        return val
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default

# ---------------------------------------------------------------- paths
ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "logs"
LOG_PATH.mkdir(exist_ok=True)


# ---------------------------------------------------------------- general
# Starting cash for the simulator (terminal.py --sim) when no state exists.
STARTING_EQUITY = float(_secret("STARTING_EQUITY", "5000"))

# ---------------------------------------------------------------- rotation terminal
# Settings for plan.py (news-driven next-day rotation planner).
ANTHROPIC_API_KEY = _secret("ANTHROPIC_API_KEY", "")
ALPHAVANTAGE_API_KEY = _secret("ALPHAVANTAGE_API_KEY", "")
ROTATION_MODEL = _secret("ROTATION_MODEL", "claude-opus-5-5")
ROTATION_EFFORT = _secret("ROTATION_EFFORT", "high")          # low|medium|high|xhigh|max

PORTFOLIO_PATH = ROOT / "portfolio.json"       # holdings snapshot (gitignored)
UNIVERSE_PATH = ROOT / "universe.txt"           # one ticker per line, # comments ok
PLANS_DIR = ROOT / "plans"

ROTATION_NEWS_HOURS = int(_secret("ROTATION_NEWS_HOURS", "36"))
ROTATION_MAX_POSITIONS = int(_secret("ROTATION_MAX_POSITIONS", "4"))
ROTATION_MAX_WEIGHT = float(_secret("ROTATION_MAX_WEIGHT", "0.35"))   # of equity, per name
ROTATION_MIN_ORDER_USD = float(_secret("ROTATION_MIN_ORDER_USD", "150"))
ROTATION_STOP_PCT = float(_secret("ROTATION_STOP_PCT", "8"))          # sell flag below avg cost
ROTATION_MAX_BUYS = int(_secret("ROTATION_MAX_BUYS", "3"))
ROTATION_EARNINGS_BUFFER_DAYS = int(_secret("ROTATION_EARNINGS_BUFFER_DAYS", "2"))
ROTATION_CANDIDATE_LIMIT = 40     # most candidates sent to the LLM after pre-ranking


# ---------------------------------------------------------------- live desk (terminal.py)
# IBKR via TWS / IB Gateway on this machine. 7497 = paper, 7496 = live (TWS);
# 4002 = paper, 4001 = live (Gateway). Enable "ActiveX and Socket Clients" in
# TWS > Global Configuration > API > Settings and add 127.0.0.1 as trusted.
IBKR_HOST = _secret("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(_secret("IBKR_PORT", "7497"))
IBKR_CLIENT_ID = int(_secret("IBKR_CLIENT_ID", "7"))
IBKR_ACCOUNT = _secret("IBKR_ACCOUNT", "")            # blank = first account on the login
IBKR_MARKET_DATA = _secret("IBKR_MARKET_DATA", "delayed")   # live | delayed (no subscription needed)

# Telegram: create a bot with @BotFather, message it once, then put your chat id here.
TELEGRAM_BOT_TOKEN = _secret("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _secret("TELEGRAM_CHAT_ID", "")

# How the wave rider acts on a signal:
#   signal = tell me (terminal + Telegram), place nothing
#   manual = tell me and pre-fill the order; I confirm with one key / one reply
#   auto   = enter with a bracket (entry + trailing stop) without asking
WAVE_MODE = _secret("WAVE_MODE", "signal")
ACCOUNT_TYPE = _secret("ACCOUNT_TYPE", "cash")          # cash | margin (PDT rules apply to margin < $25k)

# --- what counts as a wave
WAVE_MIN_PRICE = float(_secret("WAVE_MIN_PRICE", "1.0"))
WAVE_MAX_PRICE = float(_secret("WAVE_MAX_PRICE", "20.0"))
WAVE_MAX_MCAP_B = float(_secret("WAVE_MAX_MCAP_B", "3.0"))    # small caps only
WAVE_MIN_REL_VOL = float(_secret("WAVE_MIN_REL_VOL", "3.0"))  # vs 20d avg, session-adjusted
WAVE_MIN_CHANGE_PCT = float(_secret("WAVE_MIN_CHANGE_PCT", "5.0"))
WAVE_MAX_CHANGE_PCT = float(_secret("WAVE_MAX_CHANGE_PCT", "60.0"))  # beyond this it is a lottery ticket
WAVE_MIN_DOLLAR_VOL_M = float(_secret("WAVE_MIN_DOLLAR_VOL_M", "2.0"))  # $ traded today, millions
WAVE_NEWS_HOURS = int(_secret("WAVE_NEWS_HOURS", "4"))
WAVE_SCAN_INTERVAL = int(_secret("WAVE_SCAN_INTERVAL", "60"))        # seconds between scans
WAVE_SIGNAL_COOLDOWN_MIN = int(_secret("WAVE_SIGNAL_COOLDOWN_MIN", "45"))

# --- how a wave is ridden
WAVE_RISK_PCT = float(_secret("WAVE_RISK_PCT", "1.5"))          # % of equity risked per trade
WAVE_MAX_POSITION_PCT = float(_secret("WAVE_MAX_POSITION_PCT", "25"))
WAVE_MAX_OPEN = int(_secret("WAVE_MAX_OPEN", "3"))
WAVE_STOP_PCT = float(_secret("WAVE_STOP_PCT", "6"))            # initial stop below entry
WAVE_TRAIL_PCT = float(_secret("WAVE_TRAIL_PCT", "8"))          # trailing stop distance
WAVE_MAX_DAILY_LOSS_PCT = float(_secret("WAVE_MAX_DAILY_LOSS_PCT", "4"))
WAVE_EXIT_BEFORE_CLOSE_MIN = int(_secret("WAVE_EXIT_BEFORE_CLOSE_MIN", "0"))  # 0 = hold overnight
WAVE_ENTRY_BUFFER_PCT = float(_secret("WAVE_ENTRY_BUFFER_PCT", "0.4"))        # limit above last
WAVE_STATE_PATH = ROOT / "wave_state.json"
