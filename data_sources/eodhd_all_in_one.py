"""
eodhd_all_in_one.py — Standalone fetcher for the EODHD All-In-One plan.

Unlike eodhd_adapter.py (which feeds the unified CompanyData pipeline), this
module talks to EODHD directly and bundles every endpoint into a single
dict consumed by the "EODHD Direct" PDF generator. The report is meant
to display 100% of the data EODHD's All-In-One subscription exposes —
nothing is merged or transformed against other sources.

Endpoints covered (per https://eodhd.com/financial-apis/):
  /fundamentals/{ticker}              full fundamentals snapshot
  /real-time/{ticker}                 live / last-trade quote
  /eod/{ticker}                       daily OHLCV history
  /div/{ticker}                       historical dividends
  /splits/{ticker}                    historical stock splits
  /insider-transactions               recent insider buys/sells
  /news                               company news feed
  /sentiments                         aggregated sentiment time series
  /upgrades-downgrades                analyst rating changes

Every fetch is wrapped in try/except so a single endpoint failure doesn't
break the whole report. The result dict carries `errors` listing whatever
failed, plus an `endpoints_used` count, so the PDF can print a status
footer.
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
DEFAULT_DELAY = 0.4   # seconds between calls — polite + avoids rate limits


def _convert_ticker(yf_ticker: str) -> str:
    """Convert a Yahoo Finance ticker to EODHD format (e.g. RHM.DE → RHM.XETRA)."""
    dot = yf_ticker.rfind(".")
    if dot == -1:
        return f"{yf_ticker}.US"
    suffix = yf_ticker[dot:]
    base = yf_ticker[:dot]
    eodhd_suffix = _YF_TO_EODHD.get(suffix, suffix)
    if eodhd_suffix == ".HK" and base.isdigit():
        base = base.zfill(4)
    if eodhd_suffix in (".KO", ".KQ") and base.isdigit():
        base = base.zfill(6)
    return f"{base}{eodhd_suffix}"


class EODHDAllInOneFetcher:
    """
    Fetches every public EODHD endpoint relevant to fundamental + market
    analysis for a single ticker. Returns a bundle dict the PDF generator
    can render section-by-section.
    """

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or EODHD_API_KEY
        if not self.api_key:
            logger.warning("[eodhd-all] No API key configured.")

    # ── Low-level GET helper ──────────────────────────────────────────────────
    def _get(self, path: str, params: dict = None,
             timeout: int = 30) -> Optional[Any]:
        if not self.api_key:
            return None
        p = {"api_token": self.api_key, "fmt": "json"}
        if params:
            p.update(params)
        try:
            time.sleep(DEFAULT_DELAY)
            url = f"{EODHD_BASE}{path}"
            r = requests.get(url, params=p, headers=REQUEST_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            logger.warning(f"[eodhd-all] {path} HTTP {r.status_code}: {r.text[:120]}")
            return None
        except Exception as e:
            logger.warning(f"[eodhd-all] {path} request failed: {e}")
            return None

    # ── Individual endpoint fetchers ──────────────────────────────────────────
    def fetch_fundamentals(self, eodhd_ticker: str) -> Optional[dict]:
        return self._get(f"/fundamentals/{eodhd_ticker}")

    def fetch_realtime(self, eodhd_ticker: str) -> Optional[dict]:
        return self._get(f"/real-time/{eodhd_ticker}")

    def fetch_forex_rate(self, base_ccy: str, quote_ccy: str) -> Optional[float]:
        """
        Current spot rate for 1 unit of base_ccy in quote_ccy (e.g.
        base_ccy="USD", quote_ccy="KZT" → how many KZT per USD).

        Used for dual-currency ADRs (e.g. KSPI: price/market-cap quoted in
        USD, financial statements reported in KZT) where EV must be
        converted into the statements' reporting currency before dividing
        by revenue/gross profit. Returns None on any failure — callers must
        treat a missing rate as "cannot convert, skip the derived ratio"
        rather than guessing.
        """
        if not base_ccy or not quote_ccy or base_ccy == quote_ccy:
            return None
        result = self._get(f"/real-time/{base_ccy}{quote_ccy}.FOREX")
        if isinstance(result, dict):
            rate = result.get("close") or result.get("previousClose")
            try:
                rate_f = float(rate)
                if rate_f > 0:
                    return rate_f
            except (TypeError, ValueError):
                pass
        return None

    def fetch_eod_history(self, eodhd_ticker: str, years_back: int = 5
                          ) -> Optional[list]:
        """Daily OHLCV for the past N years, ascending chronological order."""
        end = datetime.utcnow().date()
        start = end - timedelta(days=years_back * 365 + 30)
        result = self._get(
            f"/eod/{eodhd_ticker}",
            params={"from": start.isoformat(), "to": end.isoformat(),
                    "period": "d", "order": "a"},
            timeout=45,
        )
        if isinstance(result, list):
            return result
        return None

    def fetch_dividends(self, eodhd_ticker: str) -> Optional[list]:
        """Full dividend history. Returns list of {date, value, declarationDate, ...}."""
        result = self._get(f"/div/{eodhd_ticker}")
        if isinstance(result, list):
            return result
        return None

    def fetch_splits(self, eodhd_ticker: str) -> Optional[list]:
        result = self._get(f"/splits/{eodhd_ticker}")
        if isinstance(result, list):
            return result
        return None

    def fetch_insider(self, eodhd_ticker: str, limit: int = 50
                      ) -> Optional[list]:
        """Recent insider transactions for the ticker."""
        result = self._get(
            "/insider-transactions",
            params={"code": eodhd_ticker, "limit": limit, "order": "d"},
        )
        if isinstance(result, list):
            return result
        return None

    def fetch_news(self, eodhd_ticker: str, limit: int = 20
                   ) -> Optional[list]:
        """Latest news items, newest first."""
        result = self._get(
            "/news",
            params={"s": eodhd_ticker, "limit": limit, "offset": 0},
        )
        if isinstance(result, list):
            return result
        return None

    def fetch_sentiments(self, eodhd_ticker: str) -> Optional[dict]:
        """Aggregated sentiment time series. Returns {ticker: [{date,count,normalized},...]}."""
        result = self._get(
            "/sentiments",
            params={"s": eodhd_ticker},
        )
        if isinstance(result, dict):
            return result
        return None

    def fetch_upgrades_downgrades(self, eodhd_ticker: str,
                                  months_back: int = 12) -> Optional[list]:
        """Analyst rating changes. EODHD requires from/to date params."""
        end = datetime.utcnow().date()
        start = end - timedelta(days=months_back * 31)
        result = self._get(
            "/upgrades-downgrades",
            params={"from": start.isoformat(), "to": end.isoformat(),
                    "symbols": eodhd_ticker},
        )
        if isinstance(result, list):
            return result
        return None

    # ── Bundle everything together ────────────────────────────────────────────
    def fetch_all(self, yf_ticker: str) -> dict:
        """
        Fetch every supported endpoint for `yf_ticker`. Returns:
          {
            "ticker": "RHM.DE",
            "eodhd_ticker": "RHM.XETRA",
            "fetched_at": "2026-05-15T11:00:00Z",
            "fundamentals": {...} | None,
            "realtime":     {...} | None,
            "eod":          [...] | None,
            "dividends":    [...] | None,
            "splits":       [...] | None,
            "insider":      [...] | None,
            "news":         [...] | None,
            "sentiments":   {...} | None,
            "upgrades":     [...] | None,
            "errors":       ["endpoint_name", ...],
            "endpoints_used": N,
          }
        """
        eodhd_ticker = _convert_ticker(yf_ticker)
        bundle = {
            "ticker": yf_ticker,
            "eodhd_ticker": eodhd_ticker,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "errors": [],
            "endpoints_used": 0,
        }

        endpoints = [
            ("fundamentals", lambda: self.fetch_fundamentals(eodhd_ticker)),
            ("realtime",     lambda: self.fetch_realtime(eodhd_ticker)),
            # 11 years so historical year-end closes cover the full 10-year
            # financial table in Investment Memo V2.
            ("eod",          lambda: self.fetch_eod_history(eodhd_ticker, 11)),
            ("dividends",    lambda: self.fetch_dividends(eodhd_ticker)),
            ("splits",       lambda: self.fetch_splits(eodhd_ticker)),
            ("insider",      lambda: self.fetch_insider(eodhd_ticker, 50)),
            ("news",         lambda: self.fetch_news(eodhd_ticker, 15)),
            ("sentiments",   lambda: self.fetch_sentiments(eodhd_ticker)),
            ("upgrades",     lambda: self.fetch_upgrades_downgrades(eodhd_ticker, 12)),
        ]

        for name, fn in endpoints:
            try:
                data = fn()
                if data is None or (isinstance(data, (list, dict)) and not data):
                    bundle[name] = None
                    bundle["errors"].append(name)
                else:
                    bundle[name] = data
                    bundle["endpoints_used"] += 1
                    logger.info(f"[eodhd-all] {yf_ticker}: {name} OK")
            except Exception as e:
                bundle[name] = None
                bundle["errors"].append(name)
                logger.warning(f"[eodhd-all] {yf_ticker}: {name} failed — {e}")

        logger.info(
            f"[eodhd-all] {yf_ticker}: "
            f"{bundle['endpoints_used']}/{len(endpoints)} endpoints "
            f"({len(bundle['errors'])} failed)"
        )
        return bundle
