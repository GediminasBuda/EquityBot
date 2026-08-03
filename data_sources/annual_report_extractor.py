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
KEYWORDS: list[str] = [
    "monthly active user", "daily active user", "MAU", "DAU",
    "subscriber", "paying customer", "paid customer", "customer count",
    "net customer", "enterprise customer", "active customer", "total customer",
    "bookings", "backlog", "remaining performance obligation",
    "annual recurring revenue", "ARR",
    "geographic", "disaggregation of revenue", "revenue by region",
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
    max_total_chars: int = 20_000,
) -> str:
    """Find every keyword hit in full_text, take a window of surrounding text
    around each, merge overlapping windows, and concatenate in document order
    up to a hard length cap. Returns "" if no keywords match or input is empty."""
    if not full_text:
        return ""
    keywords = keywords if keywords is not None else KEYWORDS

    spans: list[tuple[int, int]] = []
    text_len = len(full_text)
    for kw in keywords:
        pattern = re.escape(kw)
        for m in re.finditer(pattern, full_text, flags=re.IGNORECASE):
            start = max(0, m.start() - window_chars // 2)
            end = min(text_len, m.end() + window_chars // 2)
            spans.append((start, end))

    if not spans:
        return ""

    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    parts = []
    total = 0
    for start, end in merged:
        chunk = full_text[start:end].strip()
        if not chunk:
            continue
        if total + len(chunk) > max_total_chars:
            remaining = max_total_chars - total
            if remaining <= 0:
                break
            chunk = chunk[:remaining]
        parts.append(chunk)
        total += len(chunk)
        if total >= max_total_chars:
            break

    return "\n\n[...]\n\n".join(parts)
