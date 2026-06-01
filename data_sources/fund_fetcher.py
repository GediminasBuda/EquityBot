"""
fund_fetcher.py — EODHD data fetcher for ETFs and Mutual Funds.

Calls the EODHD /fundamentals endpoint (which returns ETF_Data or
MutualFund_Data depending on the instrument type), plus /real-time and
/eod for the price chart.  No other data sources are used.

Returns a bundle dict consumed by agents/pdf_fund_fundamentals.py.
"""

from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Any

import requests

from config import EODHD_API_KEY, REQUEST_HEADERS
from .eodhd_adapter import _YF_TO_EODHD

logger = logging.getLogger(__name__)

EODHD_BASE = "https://eodhistoricaldata.com/api"
_DELAY = 0.4


def _convert_ticker(yf_ticker: str) -> str:
    """Convert Yahoo Finance ticker to EODHD format."""
    dot = yf_ticker.rfind(".")
    if dot == -1:
        return f"{yf_ticker}.US"
    suffix = yf_ticker[dot:]
    base = yf_ticker[:dot]
    eodhd_suffix = _YF_TO_EODHD.get(suffix, suffix)
    return f"{base}{eodhd_suffix}"


class FundFetcher:
    """Fetches EODHD fundamentals + price data for a single ETF or Mutual Fund."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or EODHD_API_KEY

    def _get(self, path: str, params: dict = None, timeout: int = 30) -> Optional[Any]:
        if not self.api_key:
            return None
        p = {"api_token": self.api_key, "fmt": "json"}
        if params:
            p.update(params)
        try:
            time.sleep(_DELAY)
            url = f"{EODHD_BASE}{path}"
            r = requests.get(url, params=p, headers=REQUEST_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            logger.warning(f"[fund] {path} HTTP {r.status_code}: {r.text[:120]}")
            return None
        except Exception as e:
            logger.warning(f"[fund] {path} request failed: {e}")
            return None

    def fetch(self, yf_ticker: str) -> dict:
        """
        Fetch all relevant data for the fund.

        Returns:
          {
            "ticker":       "SPY",
            "eodhd_ticker": "SPY.US",
            "fund_type":    "ETF" | "FUND" | "UNKNOWN",
            "fetched_at":   "2026-06-01T12:00:00Z",
            "fundamentals": {...} | None,
            "realtime":     {...} | None,
            "eod":          [...] | None,
            "dividends":    [...] | None,
            "errors":       [...],
            "endpoints_used": N,
          }
        """
        eodhd_ticker = _convert_ticker(yf_ticker)
        bundle: dict = {
            "ticker":         yf_ticker,
            "eodhd_ticker":   eodhd_ticker,
            "fund_type":      "UNKNOWN",
            "fetched_at":     datetime.utcnow().isoformat() + "Z",
            "fundamentals":   None,
            "realtime":       None,
            "eod":            None,
            "dividends":      None,
            "errors":         [],
            "endpoints_used": 0,
        }

        # 1 — Fundamentals (the main payload)
        fund = self._get(f"/fundamentals/{eodhd_ticker}")
        if fund:
            bundle["fundamentals"] = fund
            bundle["endpoints_used"] += 1
            g_type = (fund.get("General") or {}).get("Type", "")
            if g_type == "ETF":
                bundle["fund_type"] = "ETF"
            elif g_type in ("FUND", "Mutual Fund", "MUTUALFUND"):
                bundle["fund_type"] = "FUND"
            elif fund.get("ETF_Data"):
                bundle["fund_type"] = "ETF"
            elif fund.get("MutualFund_Data"):
                bundle["fund_type"] = "FUND"
        else:
            bundle["errors"].append("fundamentals")

        # 2 — Real-time quote
        rt = self._get(f"/real-time/{eodhd_ticker}")
        if rt:
            bundle["realtime"] = rt
            bundle["endpoints_used"] += 1
        else:
            bundle["errors"].append("realtime")

        # 3 — EOD history for price chart (5 years)
        end = datetime.utcnow().date()
        start = end - timedelta(days=5 * 365 + 30)
        eod = self._get(
            f"/eod/{eodhd_ticker}",
            params={"from": start.isoformat(), "to": end.isoformat(),
                    "period": "d", "order": "a"},
            timeout=45,
        )
        if isinstance(eod, list) and eod:
            bundle["eod"] = eod
            bundle["endpoints_used"] += 1
        else:
            bundle["errors"].append("eod")

        # 4 — Dividend history
        divs = self._get(f"/div/{eodhd_ticker}")
        if isinstance(divs, list):
            bundle["dividends"] = divs
            bundle["endpoints_used"] += 1
        else:
            bundle["errors"].append("dividends")

        return bundle
