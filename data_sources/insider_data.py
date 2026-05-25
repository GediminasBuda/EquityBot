"""
insider_data.py — Unified insider-transaction data source.

Combines:
  1. EODHD /insider-transactions endpoint (primary, free + reliable for
     US tickers, decent for major EU listings).
  2. insidertrades.info HTML scraper (fallback — only used when EODHD
     returns 0 transactions for the ticker; covers more European names
     EODHD doesn't track).

Returns a single normalised list of dicts the PDF generator can render.
"""

from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional, Any

import requests

from config import EODHD_API_KEY, REQUEST_HEADERS
from data_sources.eodhd_adapter import _YF_TO_EODHD
from data_sources.insidertrades_scraper import (
    fetch_insider_transactions as _scrape_insider,
)

logger = logging.getLogger(__name__)

_EODHD_BASE = "https://eodhistoricaldata.com/api"
_TIMEOUT    = 30


def _yf_to_eodhd(yf_ticker: str) -> str:
    """Convert Yahoo Finance ticker → EODHD format (e.g. RHM.DE → RHM.XETRA)."""
    t = (yf_ticker or "").strip().upper()
    dot = t.rfind(".")
    if dot == -1:
        return f"{t}.US"
    suffix   = t[dot:]
    base     = t[:dot]
    eod_suf  = _YF_TO_EODHD.get(suffix, suffix)
    if eod_suf == ".HK" and base.isdigit():
        base = base.zfill(4)
    if eod_suf in (".KO", ".KQ") and base.isdigit():
        base = base.zfill(6)
    return f"{base}{eod_suf}"


def _fetch_eodhd_insider(eodhd_ticker: str, months_back: int) -> list[dict]:
    """Call EODHD /insider-transactions for the past `months_back` months."""
    if not EODHD_API_KEY:
        return []
    end   = datetime.utcnow().date()
    start = end - timedelta(days=months_back * 31)
    try:
        r = requests.get(
            f"{_EODHD_BASE}/insider-transactions",
            params={
                "api_token": EODHD_API_KEY,
                "fmt":       "json",
                "code":      eodhd_ticker,
                "from":      start.isoformat(),
                "to":        end.isoformat(),
                "limit":     1000,
                "order":     "d",
            },
            headers=REQUEST_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            logger.info(
                f"[eodhd-insider] {eodhd_ticker} → HTTP {r.status_code}"
            )
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return [_normalise_eodhd_row(x) for x in data if isinstance(x, dict)]
    except Exception as e:
        logger.warning(f"[eodhd-insider] {eodhd_ticker} request failed: {e}")
        return []


def _normalise_eodhd_row(row: dict) -> dict:
    """Reshape an EODHD /insider-transactions row into the unified format
    the report pages consume.

    EODHD's response field names differ from what some other vendors use:
      shares-count → transactionAmount          (NOT transactionShares)
      per-share $  → transactionPrice           (NOT transactionPricePerShare)
      total $      → transactionAmountValue     (NOT transactionValue)
    We accept both spellings so the row works whether EODHD ever renames
    them or whether the scraper produces the longer names.
    """
    def _f(v) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    shares = (
        _f(row.get("transactionAmount"))
        or _f(row.get("transactionShares"))
        or _f(row.get("shares"))
        or _f(row.get("amount"))
    )
    price = (
        _f(row.get("transactionPrice"))
        or _f(row.get("transactionPricePerShare"))
        or _f(row.get("price"))
    )
    value = (
        _f(row.get("transactionAmountValue"))
        or _f(row.get("transactionValue"))
        or _f(row.get("value"))
        or _f(row.get("total"))
    )
    if value is None and shares is not None and price is not None:
        value = shares * price

    return {
        "transactionDate":          row.get("transactionDate") or row.get("date"),
        "ownerName":                row.get("ownerName") or row.get("name") or "",
        "ownerRelationship":        row.get("ownerRelationship")
                                    or row.get("ownerType")
                                    or row.get("relationship")
                                    or "",
        "transactionCode":          (row.get("transactionCode") or "?").strip() or "?",
        "transactionShares":        shares,
        "transactionPricePerShare": price,
        "transactionValue":         value,
        "source":                   "eodhd",
    }


def fetch_insider_data(
    yf_ticker: str,
    company_name: str = "",
    months_back: int = 60,
) -> dict:
    """
    Top-level orchestrator. Tries EODHD first; if it returns nothing, falls
    back to the insidertrades.info scraper. Returns:
        {
            "ticker":       "BAS.DE",
            "eodhd_ticker": "BAS.XETRA",
            "transactions": [ ...normalised rows... ],
            "source_used":  "eodhd" | "insidertrades.info" | "none",
            "months_back":  60,
        }
    """
    eodhd_ticker = _yf_to_eodhd(yf_ticker)

    txns = _fetch_eodhd_insider(eodhd_ticker, months_back)
    source_used = "eodhd" if txns else "none"

    if not txns:
        # Fallback: scrape insidertrades.info
        scraped = _scrape_insider(yf_ticker, company_name, months_back)
        if scraped:
            txns = scraped
            source_used = "insidertrades.info"

    # Final sort by date desc, drop rows without a date.
    def _date_key(r: dict) -> str:
        return r.get("transactionDate") or ""

    txns = [r for r in txns if r.get("transactionDate")]
    txns.sort(key=_date_key, reverse=True)

    return {
        "ticker":       yf_ticker,
        "eodhd_ticker": eodhd_ticker,
        "transactions": txns,
        "source_used":  source_used,
        "months_back":  months_back,
    }
