"""
annual_report_extractor.py — Generic helpers for turning a full annual report
(10-K HTML from SEC EDGAR, or an uploaded PDF) into a bounded, keyword-relevant
text excerpt suitable for an LLM prompt.

Capability-agnostic: KEYWORDS below is tuned for the Growth Quality Score
"Demand Strength" capability (customer counts, geographic revenue mix,
bookings, etc.), but every function here accepts an explicit `keywords` list
so future GQS capabilities can reuse the same extraction machinery with their
own terms.
"""
from __future__ import annotations

import io
import logging
import re

logger = logging.getLogger(__name__)

# Keyword set for Demand Strength: customer counts + commercial momentum.
#
# Two generalizable lessons learned from real misses (Duolingo, then Amazon),
# both worth keeping in mind for any future capability's own keyword list:
#
# 1. Prefer word STEMS over exact multi-word phrases wherever a qualifier word
#    is optional or variable. "remaining performance obligation" never matched
#    Amazon's 10-K, which instead writes "we have performance obligations ...
#    commitments not yet recognized were approximately $244 billion" — no
#    "remaining" adjacent at all. "performance obligation" (stem, no qualifier)
#    matches both phrasings. Likewise "geographic" (adjective) doesn't match a
#    filing that writes "geography of revenues" (noun form) — "geograph"
#    matches every surface form at once. Reach for the shortest distinctive
#    stem first; only keep a full phrase when the concept is conventionally
#    always phrased that way (e.g. "monthly active user", which still safely
#    matches the plural "users" since there's no trailing anchor on the match).
#
# 2. For breakdown-style disclosures (geographic mix, segment mix, etc.), the
#    prose describing the table varies enormously between filers — Duolingo
#    writes "geography of revenues", Amazon writes "Net sales are attributed
#    to countries..." with no "geographic"/"geography" anywhere nearby. But
#    the actual TABLE ROW LABELS converge much more reliably across unrelated
#    companies (both of the above tables use "Rest of World" as the catch-all
#    row) — matching on the structural label is often a more universal signal
#    than matching on whatever prose happens to introduce it. Apply this same
#    idea to future capabilities: if a keyword phrase search is missing an
#    otherwise well-known disclosure, check whether the underlying table has a
#    conventional row/column label worth matching on directly instead.
KEYWORDS: list[str] = [
    "monthly active user", "daily active user", "MAU", "DAU",
    "subscriber", "paying customer", "paid customer", "customer count",
    "net customer", "enterprise customer", "active customer", "total customer",
    "booking", "backlog", "performance obligation",
    "annual recurring revenue", "ARR", "deferred revenue", "unearned revenue",
    "geograph", "disaggregat", "region", "rest of world", "rest of the world",
    "net revenue retention", "dollar-based net retention",
    "key operating metrics", "key business metrics",
]


def strip_html_to_text(html: str) -> str:
    """Convert a 10-K's primary HTML document to plain text, keeping table
    content (operating-metrics/revenue-mix data is frequently tabular)."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception as e:
        logger.warning(f"[annual_report_extractor] HTML parse failed: {e}")
        return ""

    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def extract_text_from_pdf_full(data: bytes, max_pages: int = 250, max_chars: int = 400_000) -> str:
    """Extract text from a full annual-report PDF (pdfplumber, PyPDF2 fallback).
    Sized for a whole filing rather than utils/file_parser.py's 20-page cap,
    which exists for the unrelated Gravity Taxers universe-upload feature."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = []
            for page in pdf.pages[:max_pages]:
                text = page.extract_text() or ""
                pages.append(text)
            result = "\n".join(pages)
            if result.strip():
                return result[:max_chars]
    except Exception as e:
        logger.warning(f"[annual_report_extractor] pdfplumber failed: {e}")

    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(data))
        result = "\n".join(page.extract_text() or "" for page in reader.pages[:max_pages])
        return result[:max_chars]
    except Exception as e:
        logger.warning(f"[annual_report_extractor] PyPDF2 failed: {e}")

    return ""


def extract_relevant_excerpts(
    full_text: str,
    keywords: list[str] | None = None,
    window_chars: int = 1500,
    max_total_chars: int = 40_000,
) -> str:
    """Find every keyword hit in full_text, take a window of surrounding text
    around each, and merge overlapping windows. Merged windows with more
    keyword hits packed into them (e.g. a "Key Operating Metrics" table that
    mentions MAU/DAU/bookings/subscribers all in one place) are prioritized
    over isolated single-hit windows (e.g. a stray "geographic" mention in a
    risk-factor paragraph) when trimming to the hard length cap — a dense
    cluster is a much stronger signal of an actual metrics/data table than a
    single scattered mention. Final output is restored to document order for
    readability. Returns "" if no keywords match or input is empty."""
    if not full_text:
        return ""
    keywords = keywords if keywords is not None else KEYWORDS

    spans: list[tuple[int, int]] = []
    text_len = len(full_text)
    for kw in keywords:
        # Short keywords (MAU, DAU, ARR) are common substrings of unrelated
        # words in dense accounting text ("ARR" inside "arrangement",
        # "warranty", "carrying value", etc.) — require a word boundary
        # before the keyword to avoid flooding the excerpt budget with
        # false-positive noise. Longer phrases are distinctive enough that
        # this isn't a risk.
        #
        # Trailing boundary must allow an optional "s": filings almost
        # always write these as plurals glued directly onto the acronym —
        # "MAUs", "DAUs" — with no space or hyphen before the "s", so a
        # bare trailing \b (found via Spotify's 20-F: "751 million MAUs")
        # never matches at all, silently making the MAU/DAU keywords dead
        # weight in every filing that doesn't happen to write the bare
        # singular form.
        if len(kw) <= 5:
            pattern = r"\b" + re.escape(kw) + r"s?\b"
        else:
            pattern = re.escape(kw)
        for m in re.finditer(pattern, full_text, flags=re.IGNORECASE):
            start = max(0, m.start() - window_chars // 2)
            end = min(text_len, m.end() + window_chars // 2)
            spans.append((start, end))

    if not spans:
        return ""

    spans.sort()
    merged: list[list[int]] = []
    hit_counts: list[int] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            hit_counts[-1] += 1
        else:
            merged.append([start, end])
            hit_counts.append(1)

    priority_order = sorted(range(len(merged)), key=lambda i: hit_counts[i], reverse=True)

    selected: list[list[int]] = []
    total = 0
    for i in priority_order:
        start, end = merged[i]
        if total + (end - start) > max_total_chars:
            remaining = max_total_chars - total
            if remaining <= 0:
                break
            end = start + remaining
        selected.append([start, end])
        total += (end - start)
        if total >= max_total_chars:
            break

    selected.sort()
    parts = [full_text[start:end].strip() for start, end in selected]
    parts = [p for p in parts if p]

    return "\n\n[...]\n\n".join(parts)
