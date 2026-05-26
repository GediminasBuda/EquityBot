"""
config.py — Central configuration for Your Humble EquityBot.
All settings live here. Adapters and agents import from this module.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── 1. Load .env for local development ───────────────────────────────────────
# Use explicit path so it works regardless of the working directory.
# On Streamlit Cloud there is no .env file — this is a safe no-op.
_ENV_FILE = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_FILE, override=True)

# ── 2. Bridge Streamlit Cloud secrets → os.environ ────────────────────────────
# On Streamlit Community Cloud, secrets live in st.secrets (set via the
# dashboard). We copy them into os.environ so the rest of the codebase can
# use os.getenv() unchanged.  setdefault() means local .env always wins.
try:
    import streamlit as st
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ[_k] = _v  # always override — secrets win over any stale env var
except Exception:
    pass  # Not in a Streamlit context, or no secrets configured — that's fine.

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
CACHE_DIR      = BASE_DIR / "cache"
OUTPUTS_DIR    = BASE_DIR / "outputs"
TEMPLATES_DIR  = BASE_DIR / "templates"
FRAMEWORKS_DIR = BASE_DIR / "frameworks"

CACHE_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
FRAMEWORKS_DIR.mkdir(exist_ok=True)

# Cache TTL: how many hours before data is considered stale and re-fetched
CACHE_TTL_HOURS = 24

# ── API Keys ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
FRED_API_KEY         = os.getenv("FRED_API_KEY", "")
FMP_API_KEY          = os.getenv("FMP_API_KEY", "")
EODHD_API_KEY        = os.getenv("EODHD_API_KEY", "")
SIMFIN_API_KEY       = os.getenv("SIMFIN_API_KEY",  "")
NEWS_API_KEY         = os.getenv("NEWS_API_KEY",    "")

# ── LLM Provider ─────────────────────────────────────────────────────────────
LLM_PROVIDER     = os.getenv("LLM_PROVIDER", "claude")       # "claude" | "openai"
LLM_MODEL        = os.getenv("LLM_MODEL", "claude-sonnet-4-5")
ADVERSARIAL_MODE = os.getenv("ADVERSARIAL_MODE", "false").lower() == "true"

# ── Data Source Tier Control ──────────────────────────────────────────────────
# Set to False to disable a tier (useful for testing or if a source is down)
ENABLE_YFINANCE       = True   # Tier 1 — always on
ENABLE_EDGAR          = True   # Tier 1 — US stocks only
ENABLE_FRED           = True   # Tier 1 — macroeconomic data
ENABLE_ALPHA_VANTAGE  = bool(ALPHA_VANTAGE_API_KEY)  # Tier 2 — needs free key
ENABLE_FMP            = bool(FMP_API_KEY) and \
                        os.getenv("ENABLE_FMP", "true").lower() != "false"
# Tier 4 — needs paid key. Set ENABLE_FMP=false in .env / Streamlit secrets
# to disable the FMP fallback entirely. Even when enabled, FMP only runs
# for US-listed tickers (its free plan doesn't cover EU/Asia exchanges).
ENABLE_EODHD          = bool(EODHD_API_KEY)           # Tier 1b — non-US, needs paid key

# Years of historical annual data to target
HISTORICAL_YEARS = 10

# ── Exchange Ticker Suffixes for Yahoo Finance ────────────────────────────────
# Used to normalize tickers when the user provides bare symbols
EXCHANGE_SUFFIXES = {
    "NYSE":      "",        # e.g. WKL  → no suffix (but WKL is Amsterdam…)
    "NASDAQ":    "",        # e.g. CSCO
    "AMS":       ".AS",     # Amsterdam
    "STO":       ".ST",     # Stockholm
    "LSE":       ".L",      # London
    "XETRA":     ".DE",     # Germany
    "HEL":       ".HE",     # Helsinki
    "CPH":       ".CO",     # Copenhagen
    "OSL":       ".OL",     # Oslo
    "EPA":       ".PA",     # Paris
    "MIL":       ".MI",     # Milan
    "BME":       ".MC",     # Madrid
    "TSX":       ".TO",     # Toronto
    "ASX":       ".AX",     # Australia
    "HKG":       ".HK",     # Hong Kong
    "TYO":       ".T",      # Tokyo
    "KRX":       ".KS",     # South Korea
    "SGX":       ".SI",     # Singapore
}

# ── HTTP Request Headers ──────────────────────────────────────────────────────
# Polite user-agent for web scraping
REQUEST_HEADERS = {
    "User-Agent": (
        "EquityBot/1.0 (financial research tool; "
        "contact: equitybot@research.local)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Rate limit: seconds between requests to the same host
REQUEST_DELAY = 1.0

# ── Report Settings ───────────────────────────────────────────────────────────
REPORT_CURRENCY_DEFAULT = "USD"
REPORT_LOCALE = "en_US"
