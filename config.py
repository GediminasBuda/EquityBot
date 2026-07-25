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
DEEPSEEK_API_KEY     = os.getenv("DEEPSEEK_API_KEY", "")
MOONSHOT_API_KEY      = os.getenv("MOONSHOT_API_KEY", "")
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY", "")
XAI_API_KEY            = os.getenv("XAI_API_KEY", "")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
FRED_API_KEY         = os.getenv("FRED_API_KEY", "")
FMP_API_KEY          = os.getenv("FMP_API_KEY", "")
EODHD_API_KEY        = os.getenv("EODHD_API_KEY", "")
SIMFIN_API_KEY       = os.getenv("SIMFIN_API_KEY",  "")
NEWS_API_KEY         = os.getenv("NEWS_API_KEY",    "")

# ── LLM Provider ─────────────────────────────────────────────────────────────
LLM_PROVIDER     = os.getenv("LLM_PROVIDER", "claude")       # "claude" | "openai" | "deepseek" | "kimi" | "gemini" | "xai"
LLM_MODEL        = os.getenv("LLM_MODEL", "claude-sonnet-4-5")
ADVERSARIAL_MODE = os.getenv("ADVERSARIAL_MODE", "false").lower() == "true"

# Kimi/Moonshot "thinking" models can spend a large, variable share of their
# completion budget on hidden reasoning before writing the final JSON answer,
# so every call's requested max_tokens is scaled up before being sent — every
# report type benefits automatically, and no other provider is affected.
# Cap kept moderate (not the original 32000): a single un-timed-out blocking
# call that runs many minutes can get the whole Streamlit Cloud worker killed
# by the platform with no Python traceback at all (silent "app collapse").
KIMI_MAX_TOKENS_MULTIPLIER = float(os.getenv("KIMI_MAX_TOKENS_MULTIPLIER", "2.5"))
KIMI_MAX_TOKENS_CAP        = int(os.getenv("KIMI_MAX_TOKENS_CAP", "16000"))
# Hard ceiling on how long a single Kimi API call may block. Without this the
# openai SDK's own default (very long) lets a slow/hung "thinking" call run
# until the hosting platform kills the process outright — this way it fails
# with a catchable, loggable error and a message shown in the Streamlit UI.
KIMI_REQUEST_TIMEOUT_SECONDS = int(os.getenv("KIMI_REQUEST_TIMEOUT_SECONDS", "240"))

# Gemini 2.5 Pro is also a "thinking" model (hidden reasoning tokens before
# the final answer) — same failure class as Kimi above, so it gets the same
# two safety nets pre-emptively rather than discovering the truncation/silent-
# kill failure mode again via a live test.
# Cap must stay comfortably above the largest base max_tokens any caller
# requests (Earnings Quality requests 16000, Industry Analysis requests
# 13000) — if a caller's base value is >= cap / multiplier, the multiplier
# is silently defeated (min() just returns the cap, unscaled). This exact
# collapse caused Earnings Quality to return a fully empty/unparseable
# response on gemini-3.1-pro-preview (16000*2.0=32000 clipped to the old
# 16000 cap), and — after that fix raised the cap to 24000 — it silently
# re-triggered for Industry Analysis (13000*2.0=26000, still clipped by the
# 24000 cap) on a data/training-thin subject (GENDA Inc. / 9166.T), where
# the model burned its entire clipped budget on hidden reasoning and
# emitted zero visible JSON. Cap raised to comfortably exceed 16000*2.0 and
# 13000*2.0 both — do this every time a new large max_tokens caller is added.
GEMINI_MAX_TOKENS_MULTIPLIER = float(os.getenv("GEMINI_MAX_TOKENS_MULTIPLIER", "2.0"))
GEMINI_MAX_TOKENS_CAP        = int(os.getenv("GEMINI_MAX_TOKENS_CAP", "36000"))
GEMINI_REQUEST_TIMEOUT_SECONDS = int(os.getenv("GEMINI_REQUEST_TIMEOUT_SECONDS", "300"))

# Raising the token ceiling above (GEMINI_MAX_TOKENS_CAP) only gives the model
# MORE room to spend on invisible reasoning — it does not stop the model from
# spending an unpredictable, sometimes-total share of the budget there, which
# is what caused Industry Analysis to return a fully empty response for GENDA
# (9166.T) even at a 24000-token ceiling, and recurred AGAIN on a second Japan
# ticker (Fujitsu / 6702.T) after the ceiling was raised to 36000. Google's
# Gemini OpenAI-compatible endpoint accepts the standard "reasoning_effort"
# param ("low" | "medium" | "high", same as OpenAI's o-series) and maps it to
# an internal hidden-thinking-token budget — capping this directly attacks the
# actual mechanism instead of just hoping the ceiling is big enough. "low"
# still produces solid analytical quality for a structured-JSON extraction
# task like this; it trades away speculative extra reasoning depth that this
# app's prompts don't ask for anyway.
GEMINI_REASONING_EFFORT = os.getenv("GEMINI_REASONING_EFFORT", "low")

# xAI's Grok (e.g. grok-4.5) is routed through the same generic "thinking model"
# safety-net pattern as Kimi/Gemini pre-emptively, rather than waiting to
# rediscover the same truncation/silent-timeout bugs via a live test — see the
# Kimi and Gemini history above for the failure modes these constants prevent.
# XAI_REASONING_EFFORT defaults to "" (not sent) because it is unconfirmed
# whether grok-4.5 accepts the OpenAI-style reasoning_effort param — the
# _openai() retry loop already drops the param automatically if the API
# rejects it, so setting this later (via env/secret) is safe either way.
XAI_MAX_TOKENS_MULTIPLIER = float(os.getenv("XAI_MAX_TOKENS_MULTIPLIER", "2.0"))
XAI_MAX_TOKENS_CAP        = int(os.getenv("XAI_MAX_TOKENS_CAP", "36000"))
XAI_REQUEST_TIMEOUT_SECONDS = int(os.getenv("XAI_REQUEST_TIMEOUT_SECONDS", "300"))
XAI_REASONING_EFFORT = os.getenv("XAI_REASONING_EFFORT", "")

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
