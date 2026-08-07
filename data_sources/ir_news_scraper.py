"""
ir_news_scraper.py — Fetch the most recent items from a company's OWN
Investor Relations "News" / "Ad Hoc Announcements" page.

Why this exists: EODHD's /news endpoint and yfinance's .news are both
third-party aggregators whose most recent item can be days or weeks stale
for thinly-covered tickers (e.g. Japanese small-caps). The company's own IR
site is always the freshest, authoritative source. This module fetches it
directly with requests + BeautifulSoup instead of relying on an LLM's web
search tool to enumerate exact article URLs from memory/search snapshots,
which is unreliable for less mainstream-press companies.

Two-step process:
  1. find_ir_news_page(website)   — locate the IR news/announcements page,
                                     starting from the company's homepage.
  2. scrape_recent_announcements(url) — parse that page's HTML for the most
                                     recent {title, link, date} items.

Both functions are defensive: they never raise, just return None / [] on
any failure (network error, blocked, layout not recognised, etc.).
"""

from __future__ import annotations
import logging
import re
from datetime import date, datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

logger = logging.getLogger(__name__)

_TIMEOUT = 12

# Polite-bot User-Agent so IR sites don't reject us outright.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.7",
}

# Path suffixes tried directly against the domain, in likelihood order,
# when the homepage itself doesn't link to a recognisable IR-news page.
_CANDIDATE_PATHS = [
    "/en/ir/news/", "/ir/news/", "/en/ir/news", "/ir/news",
    "/en/investor-relations/news/", "/investor-relations/news/",
    "/en/investors/news/", "/investors/news/",
    "/en/investor-relations/press-releases/", "/investor-relations/press-releases/",
    "/en/investors/press-releases/", "/investors/press-releases/",
    "/en/investors/news-and-events/", "/investors/news-and-events/",
    "/en/investor-relations/news-and-events/", "/investor-relations/news-and-events/",
    "/en/ir/press-releases/", "/ir/press-releases/",
    "/en/ir/", "/ir/", "/en/investor-relations/", "/investor-relations/",
    "/en/investors/", "/investors/",
    "/en/news/", "/news/", "/en/newsroom/", "/newsroom/",
    "/en/press-releases/", "/press-releases/", "/en/press/", "/press/",
]

# Anchor text/href must combine one match from each pair to count as an
# IR-news link discovered from the homepage/nav. Word-boundary regex (not
# plain substring) — "ir" as a bare substring hits false positives like
# "direct" or "circle".
_IR_RE   = re.compile(r"\b(ir|investors?|investor[\-_]relations?)\b", re.IGNORECASE)
_NEWS_RE = re.compile(r"\b(news|releases?|announcements?|disclosures?)\b", re.IGNORECASE)

_SKIP_TEXT = {
    "home", "contact", "contact us", "privacy", "privacy policy", "cookie",
    "cookies", "sitemap", "login", "log in", "search", "about", "about us",
    "careers", "faq", "terms", "terms of use", "language", "japanese",
    "english", "日本語", "menu", "close", "back", "top",
}

_MONTHS = {
    m.lower(): i for i, m in enumerate(
        ["", "January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    ) if m
}
for _abbr_i, _abbr in enumerate(
    ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
):
    if _abbr:
        _MONTHS[_abbr.lower()] = _abbr_i

_DATE_PATTERNS = [
    # 2026.08.05 / 2026-08-05 / 2026/08/05
    re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"),
    # 05.08.2026 / 05-08-2026 / 05/08/2026  (day.month.year — most IR sites
    # outside the US use this order)
    re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})\b"),
    # August 5, 2026  /  Aug 5, 2026
    re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b"),
    # 5 August 2026
    re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(20\d{2})\b"),
]


def _parse_date(text: str) -> Optional[date]:
    """Best-effort extraction of a date from a short text snippet."""
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if g[0].isdigit() and len(g[0]) == 4:          # YYYY, X, X
                y, a, b = int(g[0]), int(g[1]), int(g[2])
                return date(y, a, b) if 1 <= a <= 12 else date(y, b, a)
            if g[2].isdigit() and len(g[2]) == 4 and g[0].isdigit():  # D, M, YYYY
                d1, d2, y = int(g[0]), int(g[1]), int(g[2])
                # Ambiguous D/M vs M/D — prefer D.M.Y (non-US convention),
                # fall back to M.D.Y if the first number can't be a month.
                if d2 <= 12:
                    return date(y, d2, d1)
                if d1 <= 12:
                    return date(y, d1, d2)
                continue
            if g[2].isdigit() and len(g[2]) == 4:            # Month D, YYYY
                mo = _MONTHS.get(g[0].lower()[:3]) or _MONTHS.get(g[0].lower())
                if mo:
                    return date(int(g[2]), mo, int(g[1]))
            # D Month YYYY
            mo = _MONTHS.get(g[1].lower()[:3]) or _MONTHS.get(g[1].lower())
            if mo:
                return date(int(g[2]), mo, int(g[0]))
        except (ValueError, IndexError):
            continue
    return None


def find_ir_news_page(website: str) -> Optional[str]:
    """
    Locate the company's IR "News"/"Announcements" listing page.

    Strategy: try the fixed list of common IR-news path patterns FIRST —
    these are precise, unambiguous URLs (the path itself literally
    contains "ir"/"investor"), so a 200 response here is far more
    trustworthy than a nav-link guess that might land on a generic,
    non-IR-scoped "Newsroom"/"News" page blending product/marketing news
    in with real investor disclosures. Only if none of those patterns
    resolve does this fall back to scanning the homepage nav for a link
    whose own href (not just its visible text) carries an IR-ish word
    combined with a news-ish word. Returns None if nothing plausible is
    found — the caller is expected to fall back to a different source
    rather than accept a low-confidence match.
    """
    if not website or not _HAS_BS4:
        return None
    url = website.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"

    for path in _CANDIDATE_PATHS:
        try:
            resp = requests.get(base + path, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
                return resp.url
        except Exception:
            continue

    try:
        resp = requests.get(base + "/", headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            haystack = f"{href.lower()} {text}"
            # Require the IR word IN THE HREF ITSELF (not just the anchor
            # text) — a loose text-only match (e.g. a nav item worded
            # "Investors & News") too often points at a page that blends
            # general corporate news in with real IR disclosures.
            if _IR_RE.search(href.lower()) and _NEWS_RE.search(haystack):
                candidate = urljoin(base, href)
                if urlparse(candidate).netloc == parsed.netloc:
                    return candidate
    except Exception as e:
        logger.info(f"[ir_news_scraper] homepage fetch failed for {base}: {e}")

    return None


def _ir_scope_prefix(ir_url: str) -> str:
    """
    Derive the URL-path prefix that a genuine IR item link must fall
    under, so the scraper doesn't pick up site-wide nav/footer links to
    unrelated pages (Products, About, Careers, general Newsroom, etc.)
    that happen to sit on the same IR listing page.

    Prefers the path up to and including the deepest "ir"/"investor(s)"
    segment (e.g. "/en/ir/news/" -> "/en/ir/"). If the resolved page has
    no such segment in its own path (only possible via the homepage
    nav-scan fallback in find_ir_news_page), scope to that page's own
    directory only — stricter, since there's no URL evidence this page
    is IR-specific rather than a general news page.
    """
    path = urlparse(ir_url).path
    segments = [s for s in path.split("/") if s]
    ir_idx = None
    for i, seg in enumerate(segments):
        if _IR_RE.search(seg):
            ir_idx = i
    if ir_idx is not None:
        return "/" + "/".join(segments[: ir_idx + 1]) + "/"
    if path.endswith("/"):
        return path
    return path.rsplit("/", 1)[0] + "/"


def scrape_recent_announcements(ir_url: str, limit: int = 8) -> list[dict]:
    """
    Parse an IR news/announcements listing page for its most recent items.

    Returns a list of {"title": str, "link": str, "date": "YYYY-MM-DD" or ""}
    ordered newest-first (best effort — falls back to DOM order for items
    whose date couldn't be parsed, which is correct for the great majority
    of IR pages since they're already published newest-first).
    """
    if not ir_url or not _HAS_BS4:
        return []
    try:
        resp = requests.get(ir_url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        logger.info(f"[ir_news_scraper] fetch failed for {ir_url}: {e}")
        return []

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        soup = BeautifulSoup(resp.text, "html.parser")

    scope = _ir_scope_prefix(ir_url)
    seen_links: set[str] = set()
    dated: list[tuple[date, dict]] = []
    undated: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 6 or text.lower() in _SKIP_TEXT:
            continue

        link = urljoin(ir_url, href)
        if link in seen_links or link.rstrip("/") == ir_url.rstrip("/"):
            continue
        # Only keep links nested under the IR section itself — excludes
        # site-wide nav/footer links to unrelated pages (Products, About,
        # Careers, a general Newsroom, etc.) that sit on the same page.
        if not urlparse(link).path.startswith(scope):
            continue

        # Look for a date in the anchor text itself, then widen to the
        # nearest ancestor block (typical markup: <li><span>2026.08.05</span>
        # <a>Title</a></li>).
        found = _parse_date(text)
        parent_text = ""
        if found is None:
            node = a
            for _ in range(3):
                node = node.parent
                if node is None or getattr(node, "name", None) in (None, "body", "html"):
                    break
                parent_text = node.get_text(" ", strip=True)
                found = _parse_date(parent_text)
                if found:
                    break

        title = text if len(text) <= 220 else text[:220].rstrip() + "…"
        item = {"title": title, "link": link, "date": found.isoformat() if found else ""}

        seen_links.add(link)
        if found:
            dated.append((found, item))
        else:
            undated.append(item)

    dated.sort(key=lambda t: t[0], reverse=True)
    ordered = [it for _, it in dated] + undated
    return ordered[:limit]
