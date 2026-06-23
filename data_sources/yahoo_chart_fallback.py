"""
yahoo_chart_fallback.py — Last-resort price source via Yahoo's chart API.

yfinance's `Ticker.info` calls Yahoo's quoteSummary endpoint, which is the
most aggressively rate-limited/blocked endpoint on Streamlit Cloud IPs.
The chart endpoint (`/v8/finance/chart/<ticker>`) is lighter-weight and
frequently still responds even when quoteSummary is blocked.

Stooq (stooq_adapter.py) is tried first as a fallback, but it has no
coverage for many Asian exchanges (e.g. Tokyo `.T` tickers) — this is the
fallback of last resort for those markets.

No API key required.
"""

from __future__ import annotations
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def fetch_yahoo_chart_snapshot(ticker: str, timeout: int = 15) -> Optional[dict]:
    """
    Return a small dict of live fields from Yahoo's chart API, or None on
    any failure: {price, currency, name, week_52_high, week_52_low}.
    """
    url = _CHART_URL.format(ticker=ticker)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"[yahoo_chart] {ticker} HTTP {resp.status_code}")
            return None
        data = resp.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            logger.warning(f"[yahoo_chart] {ticker} no result in response")
            return None
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            logger.warning(f"[yahoo_chart] {ticker} no regularMarketPrice")
            return None
        out = {
            "price": float(price),
            "currency": meta.get("currency"),
            "name": meta.get("longName") or meta.get("shortName"),
            "week_52_high": meta.get("fiftyTwoWeekHigh"),
            "week_52_low": meta.get("fiftyTwoWeekLow"),
        }
        logger.info(f"[yahoo_chart] {ticker} → price={out['price']} {out['currency']}")
        return out
    except Exception as e:
        logger.warning(f"[yahoo_chart] fetch failed for {ticker}: {e}")
        return None
