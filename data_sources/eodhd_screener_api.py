"""
eodhd_screener_api.py — Thin wrapper around EODHD /api/screener endpoint.

The Screener API filters the full EODHD universe (73 000+ tickers, 60+ exchanges)
in a single request.  Each call costs 5 API credits.

Filterable fields:
  String  (operations: =, match):
    code, name, exchange, sector, industry
  Numeric (operations: =, >, <, >=, <=):
    market_capitalization  (USD)
    earnings_share         (EPS, USD)
    dividend_yield         (fraction, e.g. 0.04 = 4%)
    adjusted_close         (last EOD close)
    refund_1d_p            (1-day return %)
    refund_5d_p            (5-day return %)
    avgvol_1d              (last-day volume)
    avgvol_200d            (200-day avg volume)
  Signals (pre-calculated boolean filters):
    200d_new_hi, 200d_new_lo, bookvalue_neg, bookvalue_pos,
    wallstreet_lo, wallstreet_hi

Sorting: any filterable numeric/string field, .asc or .desc
Limit  : 1–100 per request (default 50); offset 0–999 for pagination

Returns list of dicts — each dict has the fields above plus any additional
fields EODHD includes in the response.
"""

from __future__ import annotations
import json
import logging
import time
from typing import Any, Optional

import requests

from config import EODHD_API_KEY, REQUEST_HEADERS

logger = logging.getLogger(__name__)

EODHD_BASE = "https://eodhistoricaldata.com/api"
_DELAY = 0.3

# Known numeric fields the API accepts in filters
NUMERIC_FIELDS = {
    "market_capitalization", "earnings_share", "dividend_yield",
    "adjusted_close", "refund_1d_p", "refund_5d_p",
    "avgvol_1d", "avgvol_200d",
}
# Known string fields
STRING_FIELDS = {"code", "name", "exchange", "sector", "industry"}

# Valid signals
SIGNALS = {
    "200d_new_hi", "200d_new_lo",
    "bookvalue_neg", "bookvalue_pos",
    "wallstreet_lo", "wallstreet_hi",
}

# EODHD exchange codes (for reference / hints UI)
EXCHANGE_CODES = {
    "NYSE":    "US — New York Stock Exchange",
    "NASDAQ":  "US — Nasdaq",
    "AMEX":    "US — NYSE American",
    "XETRA":   "Germany — Xetra (Frankfurt)",
    "F":       "Germany — Frankfurt (alternative)",
    "LSE":     "UK — London Stock Exchange",
    "PA":      "France — Euronext Paris",
    "AS":      "Netherlands — Euronext Amsterdam",
    "BR":      "Belgium — Euronext Brussels",
    "MI":      "Italy — Borsa Italiana",
    "MC":      "Spain — BME Madrid",
    "HE":      "Finland — Nasdaq Helsinki",
    "ST":      "Sweden — Nasdaq Stockholm",
    "OL":      "Norway — Oslo Børs",
    "CO":      "Denmark — Nasdaq Copenhagen",
    "SW":      "Switzerland — SIX Swiss Exchange",
    "VI":      "Austria — Vienna Stock Exchange",
    "PR":      "Czech Republic — Prague Stock Exchange",
    "WAR":     "Poland — Warsaw Stock Exchange (GPW)",
    "BUD":     "Hungary — Budapest Stock Exchange",
    "BUC":     "Romania — Bucharest Stock Exchange",
    "TO":      "Canada — Toronto Stock Exchange",
    "V":       "Canada — TSX Venture",
    "AU":      "Australia — ASX",
    "TSE":     "Japan — Tokyo Stock Exchange",
    "KO":      "South Korea — KOSPI",
    "HK":      "Hong Kong — HKEX",
    "SHG":     "China — Shanghai",
    "SHE":     "China — Shenzhen",
    "NSE":     "India — National Stock Exchange",
    "BSE":     "India — Bombay Stock Exchange",
    "SA":      "Brazil — B3",
    "MX":      "Mexico — BMV",
    "JSE":     "South Africa — Johannesburg",
    "IS":      "Turkey — Borsa Istanbul",
    "TLV":     "Israel — Tel Aviv",
}


def run_screener(
    filters: list[list] | None = None,
    signals: list[str] | None = None,
    sort: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """
    Call the EODHD Screener API and return matching tickers.

    Args:
        filters  : list of [field, operation, value] triples, e.g.
                   [["market_capitalization", ">", 1_000_000_000],
                    ["sector", "=", "Technology"]]
        signals  : list of signal names, e.g. ["200d_new_hi"]
        sort     : e.g. "market_capitalization.desc" or "dividend_yield.desc"
        limit    : 1–100
        offset   : pagination start (0–999)

    Returns list of result dicts with these keys (when available):
        code, name, exchange, sector, industry,
        market_capitalization, earnings_share, dividend_yield,
        adjusted_close, refund_1d_p, refund_5d_p,
        avgvol_1d, avgvol_200d
    """
    if not EODHD_API_KEY:
        raise RuntimeError("EODHD_API_KEY is not configured.")

    params: dict[str, Any] = {
        "api_token": EODHD_API_KEY,
        "fmt":       "json",
        "limit":     max(1, min(100, limit)),
        "offset":    max(0, offset),
    }

    if filters:
        params["filters"] = json.dumps(filters)
    if signals:
        params["signals"] = ",".join(s for s in signals if s in SIGNALS)
    if sort:
        params["sort"] = sort

    time.sleep(_DELAY)
    try:
        r = requests.get(
            f"{EODHD_BASE}/screener",
            params=params,
            headers=REQUEST_HEADERS,
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            # EODHD wraps results in {"data": [...], "total": N}
            if isinstance(data, dict):
                return data.get("data") or []
            if isinstance(data, list):
                return data
            return []
        logger.warning(f"[screener-api] HTTP {r.status_code}: {r.text[:200]}")
        return []
    except Exception as e:
        logger.warning(f"[screener-api] request failed: {e}")
        raise
