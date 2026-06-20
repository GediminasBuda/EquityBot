"""
file_parser.py — Parse uploaded files (PDF, CSV, Excel) to extract company
names / tickers for Gravity Taxers universe definition.

Returns a list of screener-compatible row dicts:
  {rank, ticker, name, sector, note, price=None, market_cap=None, ...}

Ticker resolution strategy:
  1. If a column looks like tickers (short uppercase, may have .), use it directly.
  2. Otherwise pass company names to the LLM to resolve Yahoo Finance tickers.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,7}(\.[A-Z]{1,4})?$")
_SKIP_WORDS = {"name", "company", "ticker", "symbol", "isin", "n/a", "—", "-", ""}


def _looks_like_ticker(val: str) -> bool:
    v = val.strip().upper()
    return bool(_TICKER_RE.match(v)) and v not in _SKIP_WORDS


def _screener_row(rank: int, ticker: str, name: str, note: str = "") -> dict:
    return {
        "rank": rank,
        "ticker": ticker.strip().upper(),
        "name": name,
        "sector": "—",
        "note": note,
        "price": None,
        "currency": None,
        "market_cap": None,
        "pe_ratio": None,
        "roe": None,
        "ebit_margin": None,
        "net_margin": None,
        "div_yield": None,
        "sort_value": None,
    }


# ── CSV / Excel ───────────────────────────────────────────────────────────────

def _parse_dataframe(df) -> list[dict]:
    """Extract rows from a DataFrame — detect ticker/name columns heuristically."""
    import pandas as pd

    # Normalise column names
    df.columns = [str(c).strip() for c in df.columns]

    ticker_col = None
    name_col = None

    col_lower = {c.lower(): c for c in df.columns}

    # Prefer explicit 'ticker' / 'symbol' column
    for kw in ("ticker", "symbol", "code"):
        if kw in col_lower:
            ticker_col = col_lower[kw]
            break

    # Prefer explicit 'name' / 'company' column
    for kw in ("name", "company", "company name", "issuer"):
        if kw in col_lower:
            name_col = col_lower[kw]
            break

    # If no ticker column found, scan every column for ticker-like values
    if ticker_col is None:
        for col in df.columns:
            sample = df[col].dropna().astype(str).head(10)
            ticker_hits = sum(_looks_like_ticker(v) for v in sample)
            if ticker_hits >= min(3, len(sample)):
                ticker_col = col
                break

    # Fall back to first column for name if still nothing
    if name_col is None and ticker_col is not None:
        other_cols = [c for c in df.columns if c != ticker_col]
        name_col = other_cols[0] if other_cols else ticker_col

    if ticker_col is None and name_col is None:
        # Last resort: use first two columns
        cols = list(df.columns)
        name_col = cols[0] if cols else None
        ticker_col = cols[1] if len(cols) > 1 else None

    rows = []
    seen: set[str] = set()
    for i, (_, row) in enumerate(df.iterrows()):
        ticker = str(row.get(ticker_col, "") or "").strip().upper() if ticker_col else ""
        name   = str(row.get(name_col, "") or "").strip() if name_col else ticker

        if not ticker and not name:
            continue
        if ticker in seen or name.lower() in _SKIP_WORDS:
            continue
        seen.add(ticker or name)

        rows.append(_screener_row(i + 1, ticker or name, name or ticker,
                                  f"from file row {i+1}"))
    return rows


def _parse_csv(data: bytes, filename: str) -> list[dict]:
    import pandas as pd
    try:
        df = pd.read_csv(io.BytesIO(data))
        return _parse_dataframe(df)
    except Exception as e:
        logger.warning(f"[file_parser] CSV parse failed: {e}")
        return []


def _parse_excel(data: bytes, filename: str) -> list[dict]:
    import pandas as pd
    try:
        df = pd.read_excel(io.BytesIO(data))
        return _parse_dataframe(df)
    except Exception as e:
        logger.warning(f"[file_parser] Excel parse failed: {e}")
        return []


# ── PDF ───────────────────────────────────────────────────────────────────────

def _extract_pdf_text(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = []
            for page in pdf.pages[:20]:  # max 20 pages
                text = page.extract_text() or ""
                pages.append(text)
            return "\n".join(pages)
    except Exception as e:
        logger.warning(f"[file_parser] pdfplumber failed: {e}")

    # Fallback: PyPDF2
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(data))
        return "\n".join(
            page.extract_text() or "" for page in reader.pages[:20]
        )
    except Exception as e:
        logger.warning(f"[file_parser] PyPDF2 failed: {e}")

    return ""


def _parse_pdf(data: bytes, filename: str, llm_client=None) -> list[dict]:
    text = _extract_pdf_text(data)
    if not text.strip():
        logger.warning("[file_parser] Could not extract text from PDF")
        return []

    # Truncate to ~8000 chars to stay within token budget
    text_snippet = text[:8000]

    if llm_client is None:
        logger.warning("[file_parser] No LLM client — cannot resolve PDF companies")
        return []

    _SYSTEM = (
        "You are a financial data extraction assistant. "
        "Return ONLY valid JSON. No markdown, no code blocks."
    )
    _PROMPT = f"""\
Extract all company names and/or stock tickers mentioned in the following document text.
Return a JSON array. Each element must have:
  "rank"   : integer starting at 1
  "ticker" : Yahoo Finance format if known (e.g. "RHM.DE", "AAPL", "7203.T"), else ""
  "name"   : full company name (as written in the document)
  "note"   : one brief phrase about why this company appears in the document (max 8 words)

Rules:
- Include every distinct company / issuer mentioned
- Omit generic organisations (central banks, index providers, etc.) unless they are listed companies
- If only a ticker is given (no full name), use the ticker as both ticker and name
- Return raw JSON array only

Document text:
{text_snippet}
"""
    try:
        raw = llm_client.generate_json(_PROMPT, _SYSTEM, max_tokens=2000)
    except Exception as e:
        logger.warning(f"[file_parser] LLM call failed: {e}")
        return []

    rows_raw: list[Any] = []
    if isinstance(raw, list):
        rows_raw = raw
    elif isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                rows_raw = v
                break

    rows = []
    seen: set[str] = set()
    for i, r in enumerate(rows_raw):
        if not isinstance(r, dict):
            continue
        ticker = str(r.get("ticker") or "").strip().upper()
        name   = str(r.get("name") or "").strip()
        if not ticker and not name:
            continue
        key = ticker or name
        if key in seen:
            continue
        seen.add(key)
        rows.append(_screener_row(
            int(r.get("rank") or i + 1),
            ticker or name,
            name or ticker,
            str(r.get("note") or ""),
        ))
    return rows


# ── Ticker resolution for name-only rows ─────────────────────────────────────

def _resolve_tickers(rows: list[dict], llm_client) -> list[dict]:
    """
    For rows where ticker looks like a company name (not a real ticker),
    ask the LLM to resolve them to Yahoo Finance tickers.
    """
    unresolved = [r for r in rows if not _looks_like_ticker(r["ticker"])]
    if not unresolved:
        return rows

    names = [r["name"] or r["ticker"] for r in unresolved]
    _SYSTEM = "You are a financial data assistant. Return ONLY valid JSON."
    _PROMPT = f"""\
Convert the following company names to Yahoo Finance ticker symbols.
Return a JSON array with one element per input company:
  {{"name": "...", "ticker": "AAPL", "exchange": "US"}}

Rules:
- ticker: Yahoo Finance format (e.g. AAPL, RHM.DE, 7203.T, BA.L, 005930.KS)
- If you cannot determine the ticker with confidence, set ticker to ""
- Return array only, no prose

Companies:
{chr(10).join(f"{i+1}. {n}" for i, n in enumerate(names))}
"""
    try:
        raw = llm_client.generate_json(_PROMPT, _SYSTEM, max_tokens=800)
    except Exception:
        return rows

    resolved_list: list[Any] = []
    if isinstance(raw, list):
        resolved_list = raw
    elif isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                resolved_list = v
                break

    ticker_map: dict[str, str] = {}
    for item in resolved_list:
        if isinstance(item, dict):
            n = str(item.get("name") or "").strip()
            t = str(item.get("ticker") or "").strip().upper()
            if n and t:
                ticker_map[n.lower()] = t

    result = []
    for r in rows:
        if not _looks_like_ticker(r["ticker"]):
            resolved = ticker_map.get(r["name"].lower(), "")
            if resolved:
                r = {**r, "ticker": resolved,
                     "note": r.get("note", "") + " [ticker resolved by LLM]"}
        result.append(r)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def parse_file(
    data: bytes,
    filename: str,
    llm_client=None,
) -> list[dict]:
    """
    Parse an uploaded file and return a list of screener-compatible row dicts.

    Supported formats: .csv, .xls, .xlsx, .pdf
    llm_client: agents.llm_client.LLMClient instance (required for PDF, optional for CSV/Excel)
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "csv":
        rows = _parse_csv(data, filename)
    elif ext in ("xls", "xlsx"):
        rows = _parse_excel(data, filename)
    elif ext == "pdf":
        rows = _parse_pdf(data, filename, llm_client)
    else:
        logger.warning(f"[file_parser] Unsupported extension: {ext}")
        return []

    if not rows:
        return []

    # Resolve company names → tickers for non-PDF sources (PDF already has LLM)
    if ext != "pdf" and llm_client is not None:
        rows = _resolve_tickers(rows, llm_client)

    # Re-number by rank
    rows = sorted(rows, key=lambda r: r.get("rank") or 999)
    for i, r in enumerate(rows):
        r["rank"] = i + 1

    return rows
