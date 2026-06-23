"""
yahoo_fundamentals_fallback.py — Last-resort annual financials via Yahoo's
fundamentals-timeseries API.

yfinance's `Ticker.financials` / `.balance_sheet` / `.cashflow` properties
call Yahoo's quoteSummary endpoint under the hood, which is the endpoint
most often blocked on Streamlit Cloud IPs (see yahoo_chart_fallback.py for
the matching price-only fallback). The fundamentals-timeseries endpoint is
a separate, lighter-weight Yahoo API that frequently still responds when
quoteSummary is blocked — used here to rescue annual income statement /
cash flow data for tickers EODHD doesn't cover (e.g. Tokyo `.T` listings)
when yfinance's normal annual-history fetch comes back empty.

No API key required.
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TS_URL = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Yahoo "type" name → AnnualFinancials field name
_TYPES = {
    "annualTotalRevenue":          "revenue",
    "annualGrossProfit":           "gross_profit",
    "annualOperatingIncome":       "ebit",
    "annualNetIncome":             "net_income",
    "annualDilutedEPS":            "eps_diluted",
    "annualTotalDebt":             "total_debt",
    "annualCashAndCashEquivalents": "cash",
    "annualStockholdersEquity":    "total_equity",
    "annualOrdinarySharesNumber":  "shares_outstanding",
    "annualOperatingCashFlow":     "operating_cash_flow",
    "annualCapitalExpenditure":    "capex",
    "annualFreeCashFlow":          "fcf",
}


def fetch_yahoo_annual_financials(ticker: str, timeout: int = 15) -> dict[int, dict]:
    """
    Return {fiscal_year: {field_name: value_in_millions}} built from Yahoo's
    fundamentals-timeseries API. Empty dict on any failure.

    Values are converted to millions and sign-normalized (capex/EPS kept as
    reported; capex is returned positive, matching AnnualFinancials.capex).
    """
    url = _TS_URL.format(ticker=ticker)
    params = {
        "symbol": ticker,
        "type": ",".join(_TYPES.keys()),
        "period1": "493590046",   # 1985-08-30 — far enough back to capture all history
        "period2": str(int(datetime.utcnow().timestamp()) + 86400 * 365),
    }
    try:
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"[yahoo_fundamentals] {ticker} HTTP {resp.status_code}")
            return {}
        data = resp.json()
        results = (data.get("timeseries") or {}).get("result") or []
        if not results:
            logger.warning(f"[yahoo_fundamentals] {ticker} no results")
            return {}

        out: dict[int, dict] = {}
        for block in results:
            yahoo_type = (block.get("meta") or {}).get("type", [None])[0]
            field = _TYPES.get(yahoo_type)
            if not field:
                continue
            entries = block.get(yahoo_type) or []
            for entry in entries:
                if not entry:
                    continue
                as_of = entry.get("asOfDate")
                raw = (entry.get("reportedValue") or {}).get("raw")
                if not as_of or raw is None:
                    continue
                year = int(as_of[:4])
                # EPS/shares are already in natural units; everything else
                # is in the reporting currency — scale to millions.
                value = float(raw)
                if field not in ("eps_diluted",):
                    value = value / 1_000_000
                if field == "capex":
                    value = abs(value)
                out.setdefault(year, {})[field] = value

        logger.info(f"[yahoo_fundamentals] {ticker}: {len(out)} years recovered")
        return out
    except Exception as e:
        logger.warning(f"[yahoo_fundamentals] fetch failed for {ticker}: {e}")
        return {}
