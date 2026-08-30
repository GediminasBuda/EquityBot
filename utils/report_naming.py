"""
report_naming.py — shared filename-stem builder for generated reports.

Every generated report filename includes a short form of the company name
alongside the ticker (e.g. "GRVY_GreenTree_overview_2026-07-01.pdf",
"7974.T_Nintendo_fisher_2026-07-01.pdf") so files stay identifiable at a
glance without opening them — this matters most for pure-numeric tickers
(Japan, Korea, China, Taiwan, Hong Kong, etc., e.g. "7974.T", "005930.KS")
but is applied uniformly to every ticker.
"""

from __future__ import annotations
import re

# Legal-entity suffixes stripped from the end of a company name, e.g.
# "Nintendo Co Ltd" -> "Nintendo", "Samsung Electronics Co., Ltd." ->
# "Samsung Electronics". Matched repeatedly so multi-part suffixes like
# "Co., Ltd." are fully removed.
_SUFFIXES = [
    "co", "ltd", "limited", "corp", "corporation", "inc", "incorporated",
    "plc", "holdings", "holding", "group", "company", "kk", "berhad",
    "bhd", "pcl", "tbk", "gmbh", "ag", "sa", "nv", "spa", "ab", "oyj", "asa",
]
_SUFFIX_RE = re.compile(
    r"(?:[,.]?\s+(?:" + "|".join(_SUFFIXES) + r")\b\.?)+[,.\s]*$",
    re.IGNORECASE,
)


def short_company_name(name: str) -> str:
    """Strip trailing legal-entity suffixes, e.g. 'Nintendo Co Ltd' -> 'Nintendo'."""
    if not name:
        return ""
    stripped = _SUFFIX_RE.sub("", name).strip(" ,.")
    return stripped or name.strip()


def sanitize_for_filename(text: str) -> str:
    """Collapse a string into underscore-joined, filesystem-safe tokens."""
    if not text:
        return ""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def report_file_stem(ticker: str, company_name: str | None = None) -> str:
    """
    Build the ticker[+company] portion of a report filename.

    The '.' between a ticker root and its exchange suffix (e.g. 'RHM.DE',
    'CNQ.TO', '7974.T', 'TEP.PA') is kept as a literal '.' in the filename
    rather than converted to '_' — it reads more naturally and matches how
    the ticker is written everywhere else in the app. Hyphens (e.g. share
    classes like 'BRK-B') still become '_'. A short form of the company
    name is appended whenever it's known, for every ticker (not just
    numeric ones), e.g. 'GRVY' + 'GreenTree Hospitality Group Ltd' ->
    'GRVY_GreenTree', '7974.T' + 'Nintendo Co Ltd' -> '7974.T_Nintendo'.
    """
    safe_ticker = (ticker or "").replace("-", "_")
    if company_name:
        short = sanitize_for_filename(short_company_name(company_name))
        if short:
            return f"{safe_ticker}_{short}"
    return safe_ticker
