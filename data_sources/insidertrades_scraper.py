"""
insidertrades_scraper.py — Fallback insider-transaction source.

Used by the Insider Transactions report when EODHD's /insider-transactions
endpoint returns no data for a ticker (typical for European listings).

URL pattern observed:
    https://www.insidertrades.info/company/{TICKER}-{COMPANY_SHORT_NAME}
    e.g. https://www.insidertrades.info/company/BAS.DE-BASF

Returns a normalised list of dicts in the same shape the EODHD endpoint
would have returned, so the rest of the report code can stay source-
agnostic:

    [
      {
        "transactionDate":          "2025-04-12",
        "ownerName":                "Schmidt, Hans",
        "ownerRelationship":        "Director",
        "transactionCode":          "P",     # P = Purchase, S = Sale
        "transactionShares":        1000.0,
        "transactionPricePerShare": 42.50,
        "transactionValue":         42500.0,
        "source":                   "insidertrades.info",
      },
      ...
    ]

This scraper is *defensive*: it never raises, just returns [] on any
failure (404, layout change, network error, blocked by Cloudflare, etc.).
The PDF generator handles the empty case explicitly.
"""

from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Any

import requests

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.insidertrades.info"
_TIMEOUT  = 20

# Polite-bot User-Agent so the site doesn't 403 us instantly.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9,lt;q=0.8",
}

# Suffixes commonly trailing company names (EODHD General.Name format).
# Stripped so the URL slug matches the site's own short name.
_NAME_SUFFIX_RE = re.compile(
    r"\b(SE|AG|GmbH|S\.A\.|S\.A|SA|SAS|N\.V\.|NV|BV|PLC|LLC|LTD|"
    r"INC|INC\.|CORP|CORP\.|CO|CO\.|HOLDINGS?|GROUP|COMPANY|"
    r"TRUST|FUND|LIMITED)\b\.?",
    re.IGNORECASE,
)


def _slugify_company_name(name: str) -> str:
    """
    Convert "BASF SE" / "Volkswagen AG" / "Bayer AG" → "BASF" / "Volkswagen"
    / "Bayer" so the URL slug matches the site's short name. Best-effort.
    """
    s = (name or "").strip()
    if not s:
        return ""
    s = _NAME_SUFFIX_RE.sub("", s).strip()
    # Strip any trailing punctuation / multiple spaces
    s = re.sub(r"[,\s]+$", "", s)
    s = re.sub(r"\s+", " ", s)
    # The site appears to use the bare company name unspaced; try the
    # first word as the most likely match.
    return s


def _candidate_urls(yf_ticker: str, company_name: str) -> list[str]:
    """Return URLs to try, in order of likelihood of being correct."""
    ticker = (yf_ticker or "").strip().upper()
    name   = _slugify_company_name(company_name)
    candidates: list[str] = []
    if name:
        # Most common pattern: TICKER-Name (mixed case kept)
        candidates.append(f"{_BASE_URL}/company/{ticker}-{name}")
        # Lowercase variant (some directories normalise)
        candidates.append(f"{_BASE_URL}/company/{ticker}-{name.lower()}")
        # First word only (in case full name has more parts)
        first_word = name.split()[0]
        if first_word and first_word != name:
            candidates.append(f"{_BASE_URL}/company/{ticker}-{first_word}")
    # Ticker-only fallback (some sites redirect)
    candidates.append(f"{_BASE_URL}/company/{ticker}")
    return candidates


def _parse_date(raw: str) -> Optional[str]:
    """
    Try common date formats; return ISO YYYY-MM-DD or None.

    insidertrades.info is a US-focused site that renders dates as
    M/D/YYYY (e.g. "5/8/2026" = May 8, 2026). The previous order tried
    %d/%m/%Y first and silently mis-parsed those dates as 5-Aug. M/D/Y
    is now first; EU formats stay as fallbacks for any rare ambiguous
    row. ISO comes first because it's unambiguous.
    """
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y",
                "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y",
                "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_number(raw: str) -> Optional[float]:
    """Extract a float from a money/number string.

    insidertrades.info is US-formatted: comma = thousands, dot =
    decimal ("$369,460" or "$290.0"). Default to that. Only treat
    comma as decimal separator when the string clearly follows the
    European 1.234.567,89 pattern (multiple dots + a final comma).
    """
    if not raw:
        return None
    s = str(raw).strip()
    # Strip currency symbols / letters / spaces
    s = re.sub(r"[€$£¥]", "", s)
    s = re.sub(r"[a-zA-Z\s]", "", s)
    if not s:
        return None
    dot_count   = s.count(".")
    comma_count = s.count(",")
    # EU pattern: comma is the LAST separator AND there are multiple
    # dots before it (e.g. "1.234.567,89") — typical European number.
    if comma_count == 1 and dot_count >= 2 and s.rfind(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        # US (and most other) — comma = thousands. Drop them.
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _classify_transaction_code(raw_type: str) -> str:
    """
    Best-effort mapping of the site's transaction label to EODHD's
    single-letter code:
      P = Purchase / Buy
      S = Sale     / Sell
      A = Award / Grant
      ?           = unknown
    """
    if not raw_type:
        return "?"
    s = raw_type.strip().lower()
    if any(x in s for x in ("buy", "purchase", "kauf", "pirkimas", "acqu")):
        return "P"
    if any(x in s for x in ("sell", "sale", "verkauf", "pardav", "dispos")):
        return "S"
    if any(x in s for x in ("grant", "award", "vest", "option")):
        return "A"
    return "?"


def _scrape_url(url: str) -> Optional[list[dict]]:
    """
    Fetch + parse one candidate URL. Returns a list of normalised
    transaction dicts on success, [] if the page loaded but contained
    no transactions, None if the URL itself didn't work (404, network).
    """
    if not _HAS_BS4:
        logger.warning("[insidertrades] beautifulsoup4 not installed; cannot scrape")
        return None
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except Exception as e:
        logger.warning(f"[insidertrades] GET {url} failed: {e}")
        return None
    if r.status_code != 200:
        logger.info(f"[insidertrades] {url} → HTTP {r.status_code}")
        return None

    soup = BeautifulSoup(r.text, "lxml" if _has_lxml() else "html.parser")

    # The site renders multiple tables (e.g. recent activity strip + the
    # full historical table). Collect rows from EVERY <table> whose
    # header row looks like an insider-transaction table — not just the
    # first match. De-duplicate by (date, owner, price) at the end.
    all_rows: list[dict] = []

    for table in soup.find_all("table"):
        ths = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not ths:
            first_tr = table.find("tr")
            if first_tr:
                ths = [td.get_text(strip=True).lower()
                       for td in first_tr.find_all("td")]
        header_text = " ".join(ths)
        if not any(k in header_text for k in
                   ("insider", "shares", "transaction", "filer",
                    "trade", "owner", "officer", "director")):
            continue

        # Map column names → index for THIS table (each table may have
        # a different schema).
        def _col(*keys: str) -> Optional[int]:
            for k in keys:
                for i, h in enumerate(ths):
                    if k in h:
                        return i
            return None

        i_date   = _col("date")
        i_owner  = _col("insider", "name", "filer", "owner", "officer")
        i_role   = _col("role", "relation", "position", "title")
        i_type   = _col("transaction", "type", "action", "trade")
        # On insidertrades.info "Volume" is the total $ amount of the
        # trade (e.g. "$71,189,722") — i.e. the trade VALUE, not a
        # share count. Map volume to the value column and leave the
        # shares column for the rarer "shares"/"qty" headers other
        # vendors use. Shares for this site are then reverse-derived
        # from value / price further down.
        i_value  = _col("volume", "worth", "value", "total", "net",
                        "eur", "usd", "amount value", "trade value")
        i_shares = _col("shares", "quantity", "qty", "amount", "units")
        i_price  = _col("price", "per share", "share price")

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not cells:
                continue

            def _get(idx: Optional[int]) -> str:
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx]

            date_iso = _parse_date(_get(i_date))
            if not date_iso:
                continue

            shares = _parse_number(_get(i_shares))
            price  = _parse_number(_get(i_price))
            value  = _parse_number(_get(i_value))

            # Fill the missing leg of the value=shares*price triangle.
            if value is None and shares is not None and price is not None:
                value = shares * price
            if shares is None and value is not None and price is not None and price > 0:
                shares = value / price

            all_rows.append({
                "transactionDate":          date_iso,
                "ownerName":                _get(i_owner),
                "ownerRelationship":        _get(i_role),
                "transactionCode":          _classify_transaction_code(_get(i_type)),
                "transactionShares":        shares,
                "transactionPricePerShare": price,
                "transactionValue":         value,
                "source":                   "insidertrades.info",
            })

    if not all_rows:
        return []

    # De-duplicate by (date, owner, price) — the site repeats rows in
    # multiple tables (recent strip + historical table). Keep the first
    # one we saw.
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in all_rows:
        key = (
            r["transactionDate"],
            (r.get("ownerName") or "").lower().strip(),
            r.get("transactionPricePerShare"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    logger.info(
        f"[insidertrades] parsed {len(all_rows)} raw rows → "
        f"{len(out)} unique after de-dup ({url})"
    )
    return out


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def fetch_insider_transactions(
    yf_ticker: str,
    company_name: str = "",
    months_back: int = 60,
) -> list[dict]:
    """
    Try every candidate URL, return the first non-empty list of
    normalised insider transactions. Filtered to the past `months_back`
    months (default = 5 years).

    Returns [] on any failure or if the page exists but carries no
    transactions; the caller is expected to treat that as "no data".
    """
    if not yf_ticker:
        return []

    cutoff = datetime.utcnow() - timedelta(days=months_back * 31)

    for url in _candidate_urls(yf_ticker, company_name):
        rows = _scrape_url(url)
        if rows is None:
            # Hard error on this URL — try the next candidate
            continue
        if not rows:
            # URL worked but no transactions on the page; record it
            # and continue trying the other candidates (the wrong slug
            # may serve an empty page rather than 404).
            continue
        # Filter to the window
        filtered = []
        for r in rows:
            try:
                d = datetime.strptime(r["transactionDate"], "%Y-%m-%d")
                if d >= cutoff:
                    filtered.append(r)
            except Exception:
                continue
        if filtered:
            logger.info(
                f"[insidertrades] {yf_ticker}: {len(filtered)} txns via {url}"
            )
            return filtered

    logger.info(f"[insidertrades] {yf_ticker}: no transactions found via any URL")
    return []
