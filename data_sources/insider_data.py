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
from data_sources.openinsider_scraper import (
    fetch_insider_transactions as _scrape_openinsider,
    _is_us_ticker as _openinsider_supports,
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
        logger.warning("[eodhd-insider] EODHD_API_KEY not set — skipping")
        return []
    end   = datetime.utcnow().date()
    start = end - timedelta(days=months_back * 31)
    params = {
        "api_token": EODHD_API_KEY,
        "fmt":       "json",
        "code":      eodhd_ticker,
        "from":      start.isoformat(),
        "to":        end.isoformat(),
        "limit":     1000,
        "order":     "d",
    }
    try:
        r = requests.get(
            f"{_EODHD_BASE}/insider-transactions",
            params=params,
            headers=REQUEST_HEADERS,
            timeout=_TIMEOUT,
        )
        # Elevated to WARNING so Streamlit Cloud's default log filter
        # (which hides INFO) still shows the diagnostic line — without
        # it we can't tell whether EODHD returned nothing because the
        # endpoint isn't on the user's plan, the ticker code is wrong,
        # or the date window is off.
        if r.status_code != 200:
            logger.warning(
                f"[eodhd-insider] {eodhd_ticker} → HTTP {r.status_code} "
                f"body[:200]={r.text[:200]!r}"
            )
            return []
        data = r.json()
        if not isinstance(data, list):
            logger.warning(
                f"[eodhd-insider] {eodhd_ticker} → non-list response "
                f"type={type(data).__name__} preview={str(data)[:200]}"
            )
            return []
        rows = [_normalise_eodhd_row(x) for x in data if isinstance(x, dict)]
        logger.warning(
            f"[eodhd-insider] {eodhd_ticker} → {len(rows)} rows "
            f"(window {start} → {end})"
        )
        return rows
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
    Top-level orchestrator. Tries EODHD first (with a couple of ticker
    spellings, since EODHD's /insider-transactions sometimes wants the
    bare US symbol "AAPL" rather than "AAPL.US"); if it still returns
    nothing, falls back to the insidertrades.info scraper.

    Returns:
        {
            "ticker":       "BAS.DE",
            "eodhd_ticker": "BAS.XETRA",
            "transactions": [ ...normalised rows... ],
            "source_used":  "eodhd" | "insidertrades.info" | "none",
            "months_back":  60,
        }
    """
    eodhd_ticker = _yf_to_eodhd(yf_ticker)

    # Try the canonical EODHD code first, then the bare US symbol if
    # the canonical one didn't return anything.
    eodhd_candidates: list[str] = [eodhd_ticker]
    if eodhd_ticker.endswith(".US"):
        bare = eodhd_ticker[:-3]
        if bare and bare not in eodhd_candidates:
            eodhd_candidates.append(bare)

    txns: list[dict] = []
    for code in eodhd_candidates:
        rows = _fetch_eodhd_insider(code, months_back)
        if rows:
            txns = rows
            eodhd_ticker = code   # remember the spelling that actually worked
            break
    source_used = "eodhd" if txns else "none"

    # Fallback chain (in order, stop at first non-empty source):
    #   1. openinsider.com (US only, SEC Form 4, free, full history)
    #   2. insidertrades.info (EU + US, free, anonymous limit = 5 rows)
    if not txns and _openinsider_supports(yf_ticker):
        rows = _scrape_openinsider(yf_ticker, months_back)
        if rows:
            txns = rows
            source_used = "openinsider.com"

    if not txns:
        rows = _scrape_insider(yf_ticker, company_name, months_back)
        if rows:
            txns = rows
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


# ── Yahoo Finance insider transactions (via yfinance) ─────────────────────────

def _classify_yahoo_text(text: str) -> str:
    """Map Yahoo's free-text transaction description to P / S / A / ?.

    Examples: "Purchase at price 12.30 per share.", "Buy at price 45.6",
    "Sale at price 340.06 per share.", "Stock Award(Grant) at price 0",
    "Buy Back at price 45.67 per share." (company buyback — NOT an insider
    purchase, so it must not count as insider buying).
    """
    s = (text or "").strip().lower()
    if not s:
        return "?"
    if "buy back" in s or "buyback" in s:
        return "?"
    if s.startswith("purchase") or s.startswith("buy"):
        return "P"
    if s.startswith("sale") or s.startswith("sell"):
        return "S"
    if "award" in s or "grant" in s or "option" in s or "gift" in s:
        return "A"
    return "?"


def _fetch_yahoo_insider(yf_ticker: str, months_back: int) -> list[dict]:
    """Yahoo Finance insider transactions (quoteSummary insiderTransactions).

    Covers US Form 4 filings and UK/some EU director dealings. Uses a fresh
    yf.Ticker so no other adapter's Ticker cache is touched.
    """
    try:
        import yfinance as yf
        df = yf.Ticker(yf_ticker).insider_transactions
    except Exception as e:
        logger.warning(f"[yahoo-insider] {yf_ticker} request failed: {e}")
        return []
    if df is None or getattr(df, "empty", True):
        return []

    cutoff = datetime.utcnow() - timedelta(days=months_back * 31)
    out: list[dict] = []
    for _, r in df.iterrows():
        try:
            d = r.get("Start Date")
            d = d.to_pydatetime() if hasattr(d, "to_pydatetime") else datetime.fromisoformat(str(d)[:10])
        except Exception:
            continue
        if d.replace(tzinfo=None) < cutoff:
            continue
        text = str(r.get("Text") or r.get("Transaction") or "")
        out.append({
            "transactionDate":          d.strftime("%Y-%m-%d"),
            "ownerName":                str(r.get("Insider") or ""),
            "ownerRelationship":        str(r.get("Position") or ""),
            "transactionCode":          _classify_yahoo_text(text),
            "transactionShares":        r.get("Shares"),
            "transactionPricePerShare": None,
            "transactionValue":         r.get("Value"),
            "source":                   "yahoo",
        })
    logger.warning(f"[yahoo-insider] {yf_ticker} → {len(out)} rows (last {months_back}m)")
    return out


# ── Investment Memo checklist: insider buying in the last N months ────────────

def check_insider_buying(
    yf_ticker: str,
    company_name: str = "",
    months_back: int = 6,
) -> dict:
    """
    Did any insider make an open-market PURCHASE in the last `months_back`
    months?

    No single source is complete, so every source is consulted in order
    and the check stops at the first one showing a purchase:
      1. EODHD /insider-transactions  (primary — US-only and its feed was
         found stale as of 2026-09, latest record 2026-04-24)
      2. openinsider.com              (US only, live SEC Form 4)
      3. Yahoo Finance via yfinance   (US + UK/some EU director dealings)
      4. insidertrades.info           (EU large caps; anonymous = 5 rows)

    Returns:
        {
            "buying":  True | False,
            "source":  source that showed the buy, or the sources that
                       returned data ("none" if no source had any rows),
            "buys":    purchase count in the window (from the deciding source),
            "sells":   sale count across sources consulted,
        }
    A missing ticker in every source yields buying=False ("No"): the
    checklist only answers "Yes" on positive evidence of buying.
    """
    cutoff = (datetime.utcnow() - timedelta(days=months_back * 31)).strftime("%Y-%m-%d")

    def _eodhd() -> list[dict]:
        code = _yf_to_eodhd(yf_ticker)
        rows = _fetch_eodhd_insider(code, months_back)
        if not rows and code.endswith(".US"):
            rows = _fetch_eodhd_insider(code[:-3], months_back)
        return rows

    sources = [("eodhd", _eodhd)]
    if _openinsider_supports(yf_ticker):
        sources.append(("openinsider.com", lambda: _scrape_openinsider(yf_ticker, months_back)))
    sources.append(("yahoo", lambda: _fetch_yahoo_insider(yf_ticker, months_back)))
    sources.append(("insidertrades.info", lambda: _scrape_insider(yf_ticker, company_name, months_back)))

    sells = 0
    with_data: list[str] = []
    for name, fetch in sources:
        try:
            rows = fetch() or []
        except Exception as e:
            logger.warning(f"[insider-buying] {name} failed for {yf_ticker}: {e}")
            continue
        rows = [r for r in rows if (r.get("transactionDate") or "") >= cutoff]
        if not rows:
            continue
        with_data.append(name)
        buys = sum(1 for r in rows if r.get("transactionCode") == "P")
        sells += sum(1 for r in rows if r.get("transactionCode") == "S")
        if buys:
            logger.warning(f"[insider-buying] {yf_ticker}: {buys} buy(s) via {name}")
            return {"buying": True, "source": name, "buys": buys, "sells": sells}

    logger.warning(
        f"[insider-buying] {yf_ticker}: no buys in last {months_back}m "
        f"(sources with data: {with_data or 'none'}, sells={sells})"
    )
    return {
        "buying": False,
        "source": ", ".join(with_data) or "none",
        "buys":   0,
        "sells":  sells,
    }
