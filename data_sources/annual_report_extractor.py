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
    "revenue by country", "net sales by country", "sales by country",
    "net revenue retention", "dollar-based net retention",
    "key operating metrics", "key business metrics",
    # Capability 2 (Economic Engine) — unit economics. Same stems-over-exact-
    # phrases rule as above: "acquisition cost" (not "customer acquisition
    # cost") also matches "cost to acquire a customer", "cost of acquiring
    # customers", etc.
    "acquisition cost", "CAC", "lifetime value", "LTV",
    "contribution margin", "unit economics", "payback period",
    "gross margin", "operating leverage", "per employee",
    # Capability 3 (Operating Leverage) — expense scaling. "sales and
    # marketing" / "general and administrative" catch the common 10-K MD&A
    # split even when SG&A is reported combined elsewhere in the filing.
    "sales and marketing", "general and administrative", "research and development",
    "operating expense", "fixed cost", "variable cost", "cost discipline",
    "expense leverage", "scale efficienc", "margin expansion",
    # Capability 5 (Competitive Position) — moat evidence. "market share" /
    # "competitive advantage" / "moat" catch the "Competition" risk-factor
    # and MD&A sections most filers use to discuss positioning; "switching
    # cost" and "retention" stems catch both consumer and enterprise
    # phrasing without needing separate B2B/B2C variants.
    "market share", "competitive advantage", "moat", "switching cost",
    "customer retention", "renewal rate", "churn", "logo retention",
    "gross revenue retention", "ecosystem", "partner network", "developer",
    "brand recognition", "brand loyalty", "pricing power", "price increase",
]


def _keyword_pattern(kw: str) -> str:
    """Regex pattern for a single keyword. Shared by extract_relevant_excerpts
    and the fast PDF pre-scan so both agree on what counts as a "hit" — short
    keywords (MAU, DAU, ARR) require a word boundary plus an optional
    trailing "s" (filings glue plurals directly onto the acronym, e.g.
    "MAUs"); longer phrases are distinctive enough to match as-is."""
    if len(kw) <= 5:
        return r"\b" + re.escape(kw) + r"s?\b"
    return re.escape(kw)


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


# Above this page count, pdfplumber's per-page layout analysis (needed for
# clean table extraction) becomes slow enough across the WHOLE document to
# risk tripping a platform-level request timeout — confirmed when uploading
# Nintendo's annual report (a large "integrated report" style PDF with many
# photo/graphics-heavy pages) caused the Streamlit app to silently reset
# mid-upload, the same failure signature documented elsewhere in this
# codebase for other long blocking calls. Below this threshold the full scan
# is fast enough that the optimization below isn't worth the complexity.
_FAST_SCAN_PAGE_THRESHOLD = 30
# Pages of buffer to include on each side of a candidate page, so a table or
# sentence that spans a page break isn't cut off mid-thought.
_PAGE_CONTEXT_BUFFER = 1


def _scan_pdf_candidate_pages(
    data: bytes, keywords: list[str], max_pages: int
) -> tuple[set[int], int]:
    """Fast first pass over a PDF (PyPDF2, no layout analysis) to find which
    pages contain any keyword hit at all — used to avoid running pdfplumber's
    much slower per-page layout analysis across an entire large document when
    only a handful of pages actually matter.

    Returns (candidate_page_indices, total_chars_extracted). The char count
    lets the caller detect a failed/garbled extraction (e.g. a scanned,
    image-only PDF that PyPDF2 can't read at all) and fall back to scanning
    every page instead of trusting a false "no matches" result."""
    candidates: set[int] = set()
    total_chars = 0
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(data))
        patterns = [re.compile(_keyword_pattern(kw), re.IGNORECASE) for kw in keywords]
        for i, page in enumerate(reader.pages[:max_pages]):
            try:
                text = page.extract_text() or ""
            except Exception:
                continue
            total_chars += len(text)
            if any(p.search(text) for p in patterns):
                candidates.add(i)
    except Exception as e:
        logger.warning(f"[annual_report_extractor] Fast PDF pre-scan failed: {e}")
    return candidates, total_chars


def extract_text_from_pdf_full(
    data: bytes,
    max_pages: int = 250,
    max_chars: int = 400_000,
    keywords: list[str] | None = None,
) -> str:
    """Extract text from a full annual-report PDF (pdfplumber, PyPDF2 fallback).
    Sized for a whole filing rather than utils/file_parser.py's 20-page cap,
    which exists for the unrelated Gravity Taxers universe-upload feature.

    For documents above _FAST_SCAN_PAGE_THRESHOLD pages, runs a fast PyPDF2
    pre-scan first to find which pages actually contain a keyword hit, then
    only runs pdfplumber's slower, layout-aware extraction on those pages
    (plus a page of buffer on each side) — preserving pdfplumber's better
    table-layout fidelity exactly where the target disclosures live, while
    keeping total processing time bounded regardless of document length."""
    keywords = keywords if keywords is not None else KEYWORDS

    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_count = len(pdf.pages)
            pages_to_scan: set[int] | None = None
            if page_count > _FAST_SCAN_PAGE_THRESHOLD:
                candidates, prescan_chars = _scan_pdf_candidate_pages(
                    data, keywords, max_pages
                )
                # A near-empty pre-scan usually means PyPDF2 couldn't read
                # this PDF at all (e.g. scanned/image-only pages) rather than
                # a genuine "no keyword matches" — don't trust it in that
                # case, fall back to scanning every page with pdfplumber.
                if prescan_chars >= 500:
                    expanded: set[int] = set()
                    for p in candidates:
                        for d in range(-_PAGE_CONTEXT_BUFFER, _PAGE_CONTEXT_BUFFER + 1):
                            if 0 <= p + d < page_count:
                                expanded.add(p + d)
                    pages_to_scan = expanded

            indices = (
                sorted(pages_to_scan) if pages_to_scan is not None
                else list(range(min(page_count, max_pages)))
            )
            pages = []
            for i in indices:
                if i >= len(pdf.pages):
                    continue
                text = pdf.pages[i].extract_text() or ""
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
    single scattered mention.

    Pure hit-density ranking has a failure mode: a keyword that is genuinely
    disclosed but only mentioned once in the whole document (e.g. a
    "Revenue by country" footnote table, mentioned in exactly one place) can
    be ranked dead last and fall entirely outside max_total_chars, crowded
    out by a keyword that happens to recur dozens of times in unrelated
    prose elsewhere (e.g. "subscriber" mentioned throughout the MD&A). After
    the density-based fill, a bounded top-up pass guarantees every distinct
    matched keyword has at least one representative window included, so a
    rare-but-real disclosure is never silently dropped just because it isn't
    repeated.

    Final output is restored to document order for readability. Returns ""
    if no keywords match or input is empty."""
    if not full_text:
        return ""
    keywords = keywords if keywords is not None else KEYWORDS

    spans: list[tuple[int, int, str]] = []
    text_len = len(full_text)
    for kw in keywords:
        pattern = _keyword_pattern(kw)
        for m in re.finditer(pattern, full_text, flags=re.IGNORECASE):
            start = max(0, m.start() - window_chars // 2)
            end = min(text_len, m.end() + window_chars // 2)
            spans.append((start, end, kw))

    if not spans:
        return ""

    spans.sort(key=lambda s: s[0])
    merged: list[list[int]] = []
    hit_counts: list[int] = []
    kw_hits: list[set] = []
    for start, end, kw in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            hit_counts[-1] += 1
            kw_hits[-1].add(kw)
        else:
            merged.append([start, end])
            hit_counts.append(1)
            kw_hits.append({kw})

    priority_order = sorted(range(len(merged)), key=lambda i: hit_counts[i], reverse=True)

    selected: list[int] = []
    trimmed_ends: dict[int, int] = {}
    total = 0
    for i in priority_order:
        start, end = merged[i]
        if total + (end - start) > max_total_chars:
            remaining = max_total_chars - total
            if remaining <= 0:
                break
            end = start + remaining
            trimmed_ends[i] = end
        selected.append(i)
        total += (end - start)
        if total >= max_total_chars:
            break

    # Top-up pass: guarantee every keyword that matched anywhere in the
    # document has at least one window in the final selection, bounded by a
    # secondary budget so a long tail of rare keywords can't blow up the
    # output size. Each missing keyword's single densest not-yet-selected
    # window is added.
    selected_set = set(selected)
    covered_keywords: set = set()
    for i in selected_set:
        covered_keywords |= kw_hits[i]
    all_keywords_present: set = set().union(*kw_hits) if kw_hits else set()
    missing_keywords = all_keywords_present - covered_keywords

    if missing_keywords:
        top_up_cap = max_total_chars // 4
        top_up_used = 0
        best_for_kw: dict[str, int] = {}
        for i, kws in enumerate(kw_hits):
            if i in selected_set:
                continue
            for kw in kws & missing_keywords:
                if kw not in best_for_kw or hit_counts[i] > hit_counts[best_for_kw[kw]]:
                    best_for_kw[kw] = i

        for i in sorted(set(best_for_kw.values()), key=lambda x: hit_counts[x], reverse=True):
            if i in selected_set:
                continue
            start, end = merged[i]
            size = end - start
            if top_up_used + size > top_up_cap:
                remaining = top_up_cap - top_up_used
                if remaining <= 0:
                    break
                end = start + remaining
                trimmed_ends[i] = end
            selected.append(i)
            selected_set.add(i)
            top_up_used += (end - start)

    selected.sort()
    parts = []
    for i in selected:
        start, end = merged[i]
        end = trimmed_ends.get(i, end)
        parts.append(full_text[start:end].strip())
    parts = [p for p in parts if p]

    return "\n\n[...]\n\n".join(parts)
