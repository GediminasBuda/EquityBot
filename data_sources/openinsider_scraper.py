"""
openinsider_scraper.py — Free SEC Form 4 insider-transactions feed.

openinsider.com aggregates the SEC's Form 4 filings (US-only) into a
single screener page per ticker. Anonymous requests return the full
historical list — no subscription, no JS rendering required, so it's
a perfect second-tier fallback for US tickers when EODHD's
/insider-transactions endpoint comes back empty.

URL pattern:
    http://openinsider.com/screener?s=<TICKER>&fd=<DAYS_BACK>&cnt=500&sortcol=0

Columns on the response page (header row):
    X | Filing Date | Trade Date | Ticker | Insider Name | Title |
    Trade Type | Price | Qty | Owned | ΔOwn | Value | 1d | 1w | 1m | 6m

Returns rows in the same normalised dict shape the rest of the
insider report consumes, so the orchestrator can splice the source
in without the PDF generator caring.
"""

from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import requests

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

logger = logging.getLogger(__name__)

_BASE_URL = "http://openinsider.com"
_TIMEOUT  = 25

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def _parse_money(raw: str) -> Optional[float]:
    """Parse "$290.00" / "-$369,460" / "$71,189,722" → float (with sign)."""
    if not raw:
        return None
    s = str(raw).strip()
    sign = -1.0 if s.startswith("-") else 1.0
    # Strip sign, currency, whitespace
    s = re.sub(r"^[+-]", "", s)
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if not s or s in (".", "-"):
        return None
    try:
        return sign * float(s)
    except ValueError:
        return None


def _parse_qty(raw: str) -> Optional[float]:
    """Parse "-1,274" → -1274.0 (US comma-thousands, signed)."""
    if not raw:
        return None
    s = str(raw).strip().replace(",", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def _classify_trade_type(raw: str) -> str:
    """
    openinsider trade-type strings are like "S - Sale", "P - Purchase",
    "A - Grant", "F - Tax-related", etc. We collapse to the single
    letter EODHD-style code: P / S / A / ?.
    """
    if not raw:
        return "?"
    s = raw.strip().upper()
    if s.startswith("S") or "SALE" in s or "SELL" in s:
        return "S"
    if s.startswith("P") or "PURCHASE" in s or "BUY" in s:
        return "P"
    if s.startswith("A") or "GRANT" in s or "AWARD" in s:
        return "A"
    return "?"


def _is_us_ticker(yf_ticker: str) -> bool:
    """openinsider only covers SEC filings — US listings. A ticker is
    considered US if it has no exchange suffix, or ends with .US."""
    t = (yf_ticker or "").strip().upper()
    if not t:
        return False
    if t.endswith(".US"):
        return True
    return "." not in t   # bare symbol like AAPL, MSFT, V, MCO


def fetch_insider_transactions(
    yf_ticker: str,
    months_back: int = 60,
) -> list[dict]:
    """
    Fetch insider transactions from openinsider.com for `yf_ticker`.
    Returns [] if the ticker isn't a US listing, the page errors,
    bs4 is missing, or the screener returned no rows.

    The site's screener URL takes a `fd` parameter in DAYS — the
    number of calendar days back to include. We pass months_back * 31
    so the window matches the user's selected 1y / 2y / 5y exactly.
    """
    if not _is_us_ticker(yf_ticker):
        return []
    if not _HAS_BS4:
        logger.warning("[openinsider] beautifulsoup4 not installed; cannot scrape")
        return []

    # openinsider expects the bare US symbol (no .US suffix).
    sym = yf_ticker.strip().upper()
    if sym.endswith(".US"):
        sym = sym[:-3]

    days_back = max(months_back * 31, 31)
    url = (
        f"{_BASE_URL}/screener"
        f"?s={sym}&fd={days_back}&cnt=500&sortcol=0&sortorder=desc"
    )

    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except Exception as e:
        logger.warning(f"[openinsider] GET {url} failed: {e}")
        return []

    if r.status_code != 200:
        logger.warning(
            f"[openinsider] {sym} → HTTP {r.status_code} "
            f"body[:200]={r.text[:200]!r}"
        )
        return []

    soup = BeautifulSoup(r.text, "lxml" if _has_lxml() else "html.parser")

    # The relevant table is the longest one on the page — the screener
    # results list. All other tables are nav / filter scaffolding.
    tables = soup.find_all("table")
    if not tables:
        return []
    target = max(tables, key=lambda t: len(t.find_all("tr")))
    trs = target.find_all("tr")
    if len(trs) < 2:
        return []

    # Map header → column index (header is row 0).
    header_cells = trs[0].find_all(["th", "td"])
    headers_lc   = [
        c.get_text(strip=True).replace("\xa0", " ").lower()
        for c in header_cells
    ]

    def _col(*keys: str) -> Optional[int]:
        for k in keys:
            for i, h in enumerate(headers_lc):
                if k in h:
                    return i
        return None

    i_trade_date = _col("trade date")
    i_insider    = _col("insider name", "insider")
    i_title      = _col("title", "position", "role")
    i_type       = _col("trade type", "type")
    i_price      = _col("price")
    i_qty        = _col("qty", "quantity", "shares")
    i_value      = _col("value", "total")

    cutoff = datetime.utcnow() - timedelta(days=months_back * 31)
    out: list[dict] = []

    for tr in trs[1:]:
        tds = tr.find_all("td")
        if not tds:
            continue

        def _get(idx: Optional[int]) -> str:
            if idx is None or idx >= len(tds):
                return ""
            return tds[idx].get_text(strip=True).replace("\xa0", " ")

        date_raw = _get(i_trade_date)
        try:
            date_dt = datetime.strptime(date_raw[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if date_dt < cutoff:
            continue

        code   = _classify_trade_type(_get(i_type))
        qty    = _parse_qty(_get(i_qty))
        price  = _parse_money(_get(i_price))
        value  = _parse_money(_get(i_value))

        # openinsider signs Qty and Value (negative for sales). The
        # PDF generator expects positive magnitudes — the BUY/SELL
        # code carries the direction. Absolute-value both.
        shares = abs(qty)   if qty   is not None else None
        value  = abs(value) if value is not None else None
        if value is None and shares is not None and price is not None:
            value = shares * price

        out.append({
            "transactionDate":          date_dt.strftime("%Y-%m-%d"),
            "ownerName":                _get(i_insider),
            "ownerRelationship":        _get(i_title),
            "transactionCode":          code,
            "transactionShares":        shares,
            "transactionPricePerShare": price,
            "transactionValue":         value,
            "source":                   "openinsider.com",
        })

    logger.warning(
        f"[openinsider] {sym} → {len(out)} rows (window {cutoff.date()} → now)"
    )
    return out
