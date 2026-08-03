"""
edgar_filing_fetcher.py — Fetches the latest 10-K (narrative text, not XBRL
facts) for a US-listed ticker from SEC EDGAR, and extracts a bounded,
keyword-relevant excerpt for use in LLM prompts.

This is separate from data_sources/edgar_adapter.py, which only fetches
structured XBRL financial facts (data.sec.gov/api/xbrl/companyfacts/...) and
never touches the actual filing document text. This module hits a different
SEC endpoint (the submissions/filing-index API) to find and download the
primary 10-K document itself.

Public entry point: get_10k_excerpts(ticker, force_refresh=False) -> Optional[str]
Never raises — returns None on any failure (no CIK, no 10-K found, HTTP/parse
error), so callers can treat "no annual report data" as a normal, expected case.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from config import CACHE_DIR, REQUEST_HEADERS, ANNUAL_REPORT_CACHE_TTL_DAYS, ENABLE_EDGAR
from data_sources.edgar_adapter import EdgarAdapter, EDGAR_DELAY
from data_sources.annual_report_extractor import (
    strip_html_to_text,
    extract_relevant_excerpts,
)

logger = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{primary_doc}"

EXCERPT_CACHE_DIR = CACHE_DIR / "annual_report_excerpts"

_EDGAR_HEADERS = {**REQUEST_HEADERS, "User-Agent": "EquityBot research@equitybot.local"}

# Reused across calls within a process so we don't re-parse the CIK table per fetch.
_adapter: Optional[EdgarAdapter] = None


def _get_adapter() -> EdgarAdapter:
    global _adapter
    if _adapter is None:
        _adapter = EdgarAdapter()
    return _adapter


def _safe_ticker(ticker: str) -> str:
    return ticker.upper().replace(".", "_").replace("-", "_").replace(" ", "_")


def _cache_path(ticker: str) -> Path:
    return EXCERPT_CACHE_DIR / f"{_safe_ticker(ticker)}.json"


def _read_cache(ticker: str) -> Optional[str]:
    path = _cache_path(ticker)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if datetime.utcnow() - fetched_at > timedelta(days=ANNUAL_REPORT_CACHE_TTL_DAYS):
            return None
        return data.get("excerpt") or None
    except Exception as e:
        logger.warning(f"[edgar_filing_fetcher] Cache read failed for {ticker}: {e}")
        return None


def _write_cache(ticker: str, excerpt: str) -> None:
    path = _cache_path(ticker)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "ticker": ticker,
                "excerpt": excerpt,
                "fetched_at": datetime.utcnow().isoformat(),
            }, f)
    except Exception as e:
        logger.warning(f"[edgar_filing_fetcher] Cache write failed for {ticker}: {e}")


def _find_latest_10k(submissions: dict) -> Optional[tuple]:
    """Returns (accession_number, primary_doc, filing_date) for the newest
    10-K (falling back to 10-K/A) in the submissions' recent-filings list."""
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])

    best = None
    for i, form in enumerate(forms):
        if form not in ("10-K", "10-K/A"):
            continue
        if i >= len(accessions) or i >= len(docs):
            continue
        date = dates[i] if i < len(dates) else ""
        # Prefer a plain 10-K over 10-K/A, then prefer the most recent filing date.
        rank = (1 if form == "10-K" else 0, date)
        if best is None or rank > best[0]:
            best = (rank, accessions[i], docs[i], date)

    if best is None:
        return None
    return best[1], best[2], best[3]


def get_10k_excerpts(ticker: str, force_refresh: bool = False) -> Optional[str]:
    """Fetch and return a bounded, keyword-relevant excerpt of the latest 10-K
    for a US-listed ticker. Returns None if unavailable for any reason
    (non-US ticker, no CIK, no 10-K on file, network/parse error)."""
    if not ENABLE_EDGAR:
        return None

    if not force_refresh:
        cached = _read_cache(ticker)
        if cached is not None:
            return cached

    try:
        adapter = _get_adapter()
        cik = adapter.resolve_cik(ticker)
        if not cik:
            logger.info(f"[edgar_filing_fetcher] No CIK found for {ticker} — skipping 10-K fetch.")
            return None

        time.sleep(EDGAR_DELAY)
        resp = requests.get(
            SUBMISSIONS_URL.format(cik=cik),
            headers=_EDGAR_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        submissions = resp.json()

        found = _find_latest_10k(submissions)
        if not found:
            logger.info(f"[edgar_filing_fetcher] No 10-K found for {ticker} (CIK {cik}).")
            return None
        accession, primary_doc, filing_date = found

        cik_int = int(cik)
        accession_nodash = accession.replace("-", "")
        doc_url = ARCHIVES_URL.format(
            cik_int=cik_int, accession_nodash=accession_nodash, primary_doc=primary_doc
        )

        time.sleep(EDGAR_DELAY)
        doc_resp = requests.get(doc_url, headers=_EDGAR_HEADERS, timeout=60)
        doc_resp.raise_for_status()
        html = doc_resp.text

        full_text = strip_html_to_text(html)
        excerpt = extract_relevant_excerpts(full_text)

        if not excerpt:
            logger.info(f"[edgar_filing_fetcher] No keyword matches in 10-K for {ticker} (filed {filing_date}).")

        _write_cache(ticker, excerpt)
        return excerpt or None

    except Exception as e:
        logger.warning(f"[edgar_filing_fetcher] Failed to fetch 10-K for {ticker}: {e}")
        return None
