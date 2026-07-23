"""
report_generator.py — Report Generator page for Your Humble EquityBot.
"""
from __future__ import annotations
import base64
import json
import logging
import os
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Auth guard — must be first, blocks unauthenticated direct URL access ──────
from utils.auth import require_auth
require_auth()

from config import LLM_PROVIDER, LLM_MODEL, OUTPUTS_DIR, ADVERSARIAL_MODE as _CFG_ADV_MODE
from agents.llm_client import LLMClient
from data_sources.data_manager import DataManager
from data_sources.base import CompanyData
from framework_manager import FrameworkManager
from streamlit_searchbox import st_searchbox

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Tighten top padding */
.block-container { padding-top: 1.5rem; }

/* Report type cards (Bloomberg terminal — black / amber) */
.report-card {
    border: 1px solid #2a1f10;
    border-radius: 2px;
    padding: 14px 16px;
    background: #0a0a0a;
    margin-bottom: 6px;
    cursor: pointer;
    font-family: monospace;
}
.report-card h4 {
    color: #FFA028; margin: 0 0 4px 0; font-size: 14px;
    text-transform: uppercase; letter-spacing: 0.5px;
}
.report-card p  { color: #a87f30; margin: 0; font-size: 12px; line-height: 1.4; }

/* Metric chips */
.metric-chip {
    display: inline-block;
    background: #0a0a0a;
    color: #FFA028;
    border: 1px solid #4a3818;
    border-radius: 2px;
    padding: 2px 8px;
    font-size: 12px;
    font-weight: 700;
    font-family: monospace;
    margin: 2px;
}
.rec-buy  { background: #0a1f30; color: #4D9FFF; border-color: #1f4a70; }
.rec-hold { background: #1a1208; color: #FFA028; border-color: #4a3818; }
.rec-sell { background: #200505; color: #FF3030; border-color: #5a1010; }

/* Divider */
hr { margin: 12px 0; border-color: #2a1f10; }
</style>
""", unsafe_allow_html=True)


# ── Utility formatters ───────────────────────────────────────────────────────
def _fmt_b(v) -> str:
    if v is None: return "n/a"
    return f"{v/1000:,.1f}B" if abs(v) >= 1000 else f"{v:,.0f}M"


# ── Pricing constants ─────────────────────────────────────────────────────────
# Claude Sonnet 4.6
_C_INPUT       = 3.00  / 1_000_000
_C_CACHE_WRITE = 3.75  / 1_000_000
_C_CACHE_READ  = 0.30  / 1_000_000
_C_OUTPUT      = 15.00 / 1_000_000
# GPT-4o
_O_INPUT       = 2.50  / 1_000_000
_O_CACHE_READ  = 1.25  / 1_000_000   # GPT-4o cached input
_O_OUTPUT      = 10.00 / 1_000_000


def _claude_cost(u: dict) -> float:
    return (
        (u.get("input_tokens", 0) or 0)                * _C_INPUT +
        (u.get("cache_creation_input_tokens", 0) or 0) * _C_CACHE_WRITE +
        (u.get("cache_read_input_tokens", 0) or 0)     * _C_CACHE_READ +
        (u.get("output_tokens", 0) or 0)               * _C_OUTPUT
    )


def _openai_cost(u: dict) -> float:
    return (
        (u.get("input_tokens", 0) or 0)            * _O_INPUT +
        (u.get("cache_read_input_tokens", 0) or 0) * _O_CACHE_READ +
        (u.get("output_tokens", 0) or 0)           * _O_OUTPUT
    )


def _show_token_usage(usage: dict) -> None:
    """Compact token line shown inline during generation (Claude single-model)."""
    if not usage:
        return
    inp  = usage.get("input_tokens", 0) or 0
    out  = usage.get("output_tokens", 0) or 0
    hit  = usage.get("cache_read_input_tokens", 0) or 0
    cost = _claude_cost(usage)
    parts = [f"📥 {inp+hit:,} in", f"📤 {out:,} out"]
    if hit:
        parts.append(f"⚡ {hit:,} cached")
    parts.append(f"💰 ~${cost:.4f}")
    st.caption("🪙 " + "  ·  ".join(parts))


def _cost_block(
    usage_claude: dict,
    usage_openai: dict | None = None,
    usage_prompt: dict | None = None,
) -> float:
    """
    Render a styled cost summary card after report generation.
    Called once and stored in report_result for persistent display.

    Args:
        usage_claude  — Claude usage from the report itself (or {} if free)
        usage_openai  — GPT-4o usage (adversarial only, optional)
        usage_prompt  — Claude/OpenAI usage from the NL intent parser
                        that interpreted the user's prompt (optional)
    """
    c_cost = _claude_cost(usage_claude)
    o_cost = _openai_cost(usage_openai) if usage_openai else 0.0
    p_cost = _claude_cost(usage_prompt) if usage_prompt else 0.0
    total  = c_cost + o_cost + p_cost

    c_in  = usage_claude.get("input_tokens", 0) or 0
    c_cw  = usage_claude.get("cache_creation_input_tokens", 0) or 0
    c_cr  = usage_claude.get("cache_read_input_tokens", 0) or 0
    c_out = usage_claude.get("output_tokens", 0) or 0

    # ── Free report (no LLM at all, not even prompt) ─────────────────────────
    if not usage_claude and not usage_openai and not usage_prompt:
        st.markdown(
            "<div style='background:#F0FFF4;border:1px solid #BBE0C8;border-radius:6px;"
            "padding:8px 14px;margin:6px 0;font-size:12px;color:#1A7E3D;line-height:1.8;'>"
            "💰 <b>LLM cost this report: $0.00</b>  ·  "
            "This report uses no AI — pure data, no LLM call."
            "</div>",
            unsafe_allow_html=True,
        )
        return 0.0

    lines = []

    # Prompt parsing row (only when an NL prompt was interpreted)
    if usage_prompt:
        p_in  = usage_prompt.get("input_tokens", 0) or 0
        p_cw  = usage_prompt.get("cache_creation_input_tokens", 0) or 0
        p_cr  = usage_prompt.get("cache_read_input_tokens", 0) or 0
        p_out = usage_prompt.get("output_tokens", 0) or 0
        p_parts = [f"in {p_in+p_cw+p_cr:,}"]
        if p_cr:
            p_parts.append(f"⚡ {p_cr:,} cached")
        p_parts.append(f"out {p_out:,}")
        lines.append(
            f"<b>🔮 Prompt interpretation</b>  ·  "
            + "  ·  ".join(p_parts)
            + f"  →  <b>${p_cost:.4f}</b>"
        )

    # Report — Claude row
    if usage_claude:
        c_parts = [f"in {c_in+c_cw+c_cr:,}"]
        if c_cr:
            c_parts.append(f"⚡ {c_cr:,} cached")
        if c_cw:
            c_parts.append(f"✍ {c_cw:,} written")
        c_parts.append(f"out {c_out:,}")
        lines.append(
            f"<b>📊 Report (Claude Sonnet 4.6)</b>  ·  "
            + "  ·  ".join(c_parts)
            + f"  →  <b>${c_cost:.4f}</b>"
        )

    # GPT-4o row (adversarial only)
    if usage_openai:
        o_in  = usage_openai.get("input_tokens", 0) or 0
        o_cr  = usage_openai.get("cache_read_input_tokens", 0) or 0
        o_out = usage_openai.get("output_tokens", 0) or 0
        o_parts = [f"in {o_in+o_cr:,}"]
        if o_cr:
            o_parts.append(f"⚡ {o_cr:,} cached")
        o_parts.append(f"out {o_out:,}")
        lines.append(
            f"<b>⚔ Report (GPT-4o)</b>  ·  "
            + "  ·  ".join(o_parts)
            + f"  →  <b>${o_cost:.4f}</b>"
        )

    rows_html = "<br>".join(lines)
    # Show grand total when there are 2+ rows, so user can see prompt + report
    # combined or claude + adversarial combined.
    show_total = (len(lines) >= 2)
    total_html = (
        f"<b>Total: ${total:.4f}</b>"
        if show_total else ""
    )

    st.markdown(
        f"<div style='background:#0a0a0a;border:1px solid #2a1f10;border-radius:2px;"
        f"padding:8px 14px;margin:6px 0;font-size:12px;color:#a87f30;"
        f"font-family:monospace;line-height:1.8;'>"
        f"💰 <b style='color:#FFA028;'>LLM cost this report</b><br>"
        f"{rows_html}"
        + (f"<br><span style='color:#FFA028;font-weight:700;'>{total_html}</span>" if total_html else "")
        + "</div>",
        unsafe_allow_html=True,
    )
    return total


# ── Ticker search helper ──────────────────────────────────────────────────────
def _search_tickers(query: str, max_results: int = 5) -> list[dict]:
    """
    Search yfinance for tickers matching a company name or partial ticker.
    Returns list of {symbol, name, exchange} dicts. Empty list on any error.

    Kept as a legacy helper — newer code paths prefer the EODHD-based
    _smart_search() below for autocomplete.
    """
    if not query or len(query) < 2:
        return []
    try:
        import yfinance as yf
        results = yf.Search(query, max_results=max_results)
        quotes  = results.quotes if hasattr(results, "quotes") else []
        out = []
        for q in quotes:
            sym   = q.get("symbol", "")
            name  = q.get("shortname") or q.get("longname") or ""
            exch  = q.get("exchDisp") or q.get("exchange") or ""
            qtype = q.get("quoteType", "")
            if sym and qtype in ("EQUITY", "ETF", "INDEX", ""):
                out.append({"symbol": sym, "name": name, "exchange": exch})
        return out[:max_results]
    except Exception:
        return []


# ── EODHD search + smart NL detection ────────────────────────────────────────
import requests as _rg_requests
from config import EODHD_API_KEY as _RG_EODHD_KEY, REQUEST_HEADERS as _RG_HEADERS
from data_sources.eodhd_adapter import _YF_TO_EODHD as _RG_YF_TO_EODHD
_RG_EODHD_TO_YF = {v: k for k, v in _RG_YF_TO_EODHD.items()}
_RG_EODHD_BASE  = "https://eodhistoricaldata.com/api"


def _rg_eodhd_to_yf(code: str, exchange: str) -> str:
    """EODHD (Code, Exchange) → Yahoo Finance ticker."""
    code = (code or "").strip().upper()
    exch = (exchange or "").strip().upper()
    if not code:
        return ""
    if exch in ("", "US"):
        return code
    if exch == "INDX":
        return "^" + code
    if exch == "FOREX":
        return code + "=X"
    eodhd_suffix = f".{exch}"
    yf_suffix = _RG_EODHD_TO_YF.get(eodhd_suffix, eodhd_suffix)
    return f"{code}{yf_suffix}"


@st.cache_data(ttl=300, show_spinner=False)
def _rg_eodhd_search(query: str) -> list[dict]:
    if not _RG_EODHD_KEY or not query or len(query.strip()) < 1:
        return []
    try:
        r = _rg_requests.get(
            f"{_RG_EODHD_BASE}/search/{query.strip()}",
            params={"api_token": _RG_EODHD_KEY, "fmt": "json", "limit": 15},
            headers=_RG_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning("EODHD search HTTP %s for query %r", r.status_code, query)
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as _e:
        logger.warning("EODHD search exception for query %r: %s", query, _e)
        return []


@st.cache_data(ttl=60, show_spinner=False)
def _rg_yfinance_search(query: str) -> list[tuple[str, str]]:
    """yfinance fallback for autocomplete when EODHD search returns nothing."""
    results = _search_tickers(query, max_results=8)
    return [
        (f"{r['symbol']:<14}  {r['name'][:48]}  ({r['exchange']})", r["symbol"])
        for r in results if r.get("symbol")
    ]


# Trigger words / patterns that suggest a natural-language prompt
_NL_TRIGGERS = (
    # English
    "top ", "first ", "last ", "compare ", "screen ", "list ", "find ",
    "show ", "filter ", "run ", "analyse", "analyze",
    " by ", " from ", " with ", " in ", " for ",
    # Lithuanian
    "trauk", "rask", "rodyk", "palyginti", "palygink", "iš ", " pagal ",
    " geriausi", " didžiausi", " pigiausi", " mažiausi",
)


def _looks_like_nl(q: str) -> bool:
    """Heuristic — return True if the query looks like a natural-language prompt."""
    if not q:
        return False
    q_low = q.lower()
    word_count = len(q_low.split())
    if word_count >= 4:
        return True
    if word_count >= 2 and any(tr in q_low for tr in _NL_TRIGGERS):
        return True
    # Mentions of an index name
    if any(idx in q_low for idx in
           ("s&p", "sp500", "nasdaq", "dow", "dax", "ftse",
            "cac", "ibex", "nikkei", "hang seng", "stoxx")):
        return True
    return False


def _smart_search(query: str) -> list[tuple[str, str]]:
    """
    Smart-search callback for st_searchbox:
      • If query looks NL → return one "🔮 Run as prompt" option first.
      • EODHD /search autocomplete for most global exchanges.
      • Japan/TSE supplement — EODHD doesn't cover TSE fundamentals so
        we search our local Japan ticker cache instead.

    Each returned tuple is (display_label, value_to_return). The value
    becomes the searchbox's selection result.
    """
    q = (query or "").strip()
    if not q:
        return []

    suggestions: list[tuple[str, str]] = []

    if _looks_like_nl(q):
        truncated = (q[:70] + "…") if len(q) > 70 else q
        suggestions.append(
            (f"🔮  Run as prompt: \"{truncated}\"", f"NL::{q}")
        )

    # ── EODHD ticker autocomplete ─────────────────────────────────────────────
    seen: set[str] = set()
    for item in _rg_eodhd_search(q):
        if not isinstance(item, dict):
            continue
        code = item.get("Code", "")
        exch = item.get("Exchange", "")
        name = item.get("Name", "") or ""
        ttype = (item.get("Type") or "").strip()
        country = (item.get("Country") or "").strip()
        if not code:
            continue
        yf_ticker = _rg_eodhd_to_yf(code, exch)
        if not yf_ticker or yf_ticker in seen:
            continue
        seen.add(yf_ticker)
        meta_bits = [b for b in (exch, country, ttype) if b]
        meta = " · ".join(meta_bits)
        label = f"{yf_ticker:<14}  {name[:48]}"
        if meta:
            label += f"  ({meta})"
        suggestions.append((label, yf_ticker))

    # ── yfinance fallback when EODHD search returned nothing ─────────────────
    if not suggestions and not _looks_like_nl(q):
        for label, val in _rg_yfinance_search(q):
            if val not in seen:
                seen.add(val)
                suggestions.append((label, val))

    # ── Japan / TSE supplement ────────────────────────────────────────────────
    # Three-layer approach (EODHD doesn't cover TSE fundamentals):
    #
    # Layer 1 — Pattern match: if query is 4 digits (or 4digits.T), instantly
    #            suggest it as a TSE ticker. Handles direct code entry like
    #            "2914" → 2914.T  or  "9166.T" → 9166.T.
    #
    # Layer 2 — Static cache: local seed list (~220 major) + EODHD exchange-
    #            symbol-list (~3 800) cached 7 days. Handles name-based search
    #            for well-known companies.
    #
    # Layer 3 — yfinance search: dynamic fallback for companies not in cache
    #            (newer IPOs, smaller mid-caps). Searches "{query} japan" and
    #            filters for .T tickers.

    import re as _re_jp

    # Layer 1: direct TSE code pattern  (e.g. "2914" or "9166.T")
    _jp_direct = _re_jp.match(r'^(\d{3,4})(\.T)?$', q.strip().upper())
    if _jp_direct:
        _jp_code   = _jp_direct.group(1).zfill(4)
        _jp_ticker = f"{_jp_code}.T"
        if _jp_ticker not in seen:
            seen.add(_jp_ticker)
            # Try to resolve a company name from the Japan cache
            try:
                from data_sources.japan_tickers import get_japan_tickers
                _jp_name = next(
                    (item.get("name", "") for item in get_japan_tickers(_RG_EODHD_KEY or "")
                     if item.get("code", "").zfill(4) == _jp_code),
                    "",
                )
            except Exception:
                _jp_name = ""
            # Fallback: yfinance Search for exact ticker (fast, one call)
            if not _jp_name:
                try:
                    import yfinance as _yf_name
                    _yf_sr = _yf_name.Search(_jp_ticker, max_results=3, news_count=0)
                    for _yf_q in (_yf_sr.quotes or []):
                        if (_yf_q.get("symbol") or "").upper() == _jp_ticker:
                            _jp_name = _yf_q.get("shortname") or _yf_q.get("longname") or ""
                            break
                except Exception:
                    pass
            _jp_label = (
                f"🇯🇵 {_jp_ticker:<10}  {_jp_name[:50]}  · TSE"
                if _jp_name else
                f"🇯🇵 {_jp_ticker}  · TSE (enter to analyse)"
            )
            suggestions.insert(0, (
                _jp_label,
                _jp_ticker,
            ))

    # ── Hong Kong supplement ──────────────────────────────────────────────────
    # EODHD's /search/ endpoint doesn't index HKEX listings (even though the
    # /fundamentals/ endpoint fully supports them — see eodhd_adapter.py).
    # Same two-layer pattern as Japan above:
    #
    # Layer 1 — Pattern match: 1-5 digit input (e.g. "700" or "0700.HK")
    #            → instant HK ticker suggestion.
    #
    # Layer 2 — yfinance Search fallback for company-name queries
    #            (e.g. "Tencent"), filtered to symbols ending in .HK.

    _hk_direct = _re_jp.match(r'^0*(\d{1,5})(\.HK)?$', q.strip().upper())
    if _hk_direct:
        _hk_code   = _hk_direct.group(1).zfill(4)
        _hk_ticker = f"{_hk_code}.HK"
        if _hk_ticker not in seen:
            seen.add(_hk_ticker)
            _hk_name = ""
            try:
                import yfinance as _yf_hk_name
                _yf_sr = _yf_hk_name.Search(_hk_ticker, max_results=3, news_count=0)
                for _yf_q in (_yf_sr.quotes or []):
                    if (_yf_q.get("symbol") or "").upper() == _hk_ticker:
                        _hk_name = _yf_q.get("shortname") or _yf_q.get("longname") or ""
                        break
            except Exception:
                pass
            _hk_label = (
                f"🇭🇰 {_hk_ticker:<10}  {_hk_name[:50]}  · HKEX"
                if _hk_name else
                f"🇭🇰 {_hk_ticker}  · HKEX (enter to analyse)"
            )
            suggestions.append((_hk_label, _hk_ticker))

    if len(q) >= 3 and len(suggestions) < 10:
        try:
            import yfinance as _yf_hk
            _yf_hk_results = _yf_hk.Search(q + " hong kong", max_results=8, news_count=0)
            for _yf_item in (_yf_hk_results.quotes or []):
                _sym = (_yf_item.get("symbol") or "").strip().upper()
                if not _sym.endswith(".HK") or _sym in seen:
                    continue
                seen.add(_sym)
                _yf_name = _yf_item.get("shortname") or _yf_item.get("longname") or ""
                suggestions.append((
                    f"🇭🇰 {_sym:<10}  {_yf_name[:50]}  · HKEX",
                    _sym,
                ))
                if len(suggestions) >= 12:
                    break
        except Exception:
            pass

    # ── Baltic direct-entry ───────────────────────────────────────────────────
    # EODHD /search/ doesn't index NASDAQ Baltic (VS/TL/RG) stocks in its
    # autocomplete API. Detect Baltic ticker patterns and suggest them directly.
    #
    # Rules:
    #  • Only trigger when the query looks like a TICKER (no lowercase, no
    #    spaces). A name search like "genda" or "apranga" must NOT generate
    #    phantom Baltic tickers.
    #  • Accept both EODHD format (APG1L.VS) and VSE/Yahoo format
    #    (APG1L:VSE, APG1L:TL, APG1L:RG).
    _BALTIC = {
        "VS": ("🇱🇹", "NASDAQ Vilnius"),
        "TL": ("🇪🇪", "NASDAQ Tallinn"),
        "RG": ("🇱🇻", "NASDAQ Riga"),
    }
    # Map alternative exchange suffixes → canonical EODHD suffix
    _BALTIC_ALIASES = {
        "VSE": "VS", "VLN": "VS",
        "TAL": "TL",
        "RIG": "RG",
    }
    import re as _re_baltic
    _q_raw = q.strip()
    # Normalise :EXCHANGE → .EXCHANGE so the regex below handles both
    _q_norm = _re_baltic.sub(r':([A-Z]+)$', r'.\1', _q_raw.upper())
    # Only proceed if the ORIGINAL query has no lowercase and no spaces
    # (i.e. user is typing a ticker code, not a company name)
    _is_ticker_query = bool(_q_raw == _q_raw.upper() and " " not in _q_raw)
    if _is_ticker_query:
        _baltic_match = _re_baltic.match(
            r'^([A-Z0-9]{2,6})(\.(?:VS|TL|RG|VSE|VLN|TAL|RIG))?$', _q_norm
        )
        if _baltic_match:
            _b_code = _baltic_match.group(1)
            _b_raw_sfx = (_baltic_match.group(2) or "").lstrip(".")
            # Resolve alias (VSE→VS, TAL→TL, etc.)
            _b_exch = _BALTIC_ALIASES.get(_b_raw_sfx, _b_raw_sfx)
            if _b_exch and _b_exch in _BALTIC:
                # Exchange explicitly given — suggest just that exchange
                _b_flag, _b_name = _BALTIC[_b_exch]
                _b_ticker = f"{_b_code}.{_b_exch}"
                if _b_ticker not in seen:
                    seen.add(_b_ticker)
                    suggestions.insert(0, (
                        f"{_b_flag} {_b_ticker}  · {_b_name} (enter to analyse)",
                        _b_ticker,
                    ))
            elif any(c.isalpha() for c in _b_code):
                # No exchange suffix — suggest all three Baltic exchanges.
                # Guard: Baltic codes always contain letters (e.g. APG1L).
                # Purely numeric queries (e.g. "6752") must not generate phantoms.
                for _b_sfx, (_b_flag, _b_name) in _BALTIC.items():
                    _b_ticker = f"{_b_code}.{_b_sfx}"
                    if _b_ticker not in seen and len(suggestions) < 12:
                        seen.add(_b_ticker)
                        suggestions.append((
                            f"{_b_flag} {_b_ticker}  · {_b_name} (enter to analyse)",
                            _b_ticker,
                        ))

    # Layer 2a: Baltic name + code search (seed list + EODHD exchange-symbol-list)
    try:
        from data_sources.baltic_tickers import search_baltic
        _bl_slots = max(0, 12 - len(suggestions))
        if _bl_slots > 0:
            for _bl_label, _bl_ticker in search_baltic(
                q, api_key=_RG_EODHD_KEY or "", max_results=_bl_slots
            ):
                if _bl_ticker not in seen:
                    seen.add(_bl_ticker)
                    suggestions.append((_bl_label, _bl_ticker))
    except Exception:
        pass

    # Layer 2b: static/cached name + code search (Japan)
    try:
        from data_sources.japan_tickers import search_japan
        _jp_slots = max(0, 12 - len(suggestions))
        if _jp_slots > 0:
            for _jp_label, _jp_ticker in search_japan(
                q, api_key=_RG_EODHD_KEY or "", max_results=_jp_slots
            ):
                if _jp_ticker not in seen:
                    seen.add(_jp_ticker)
                    suggestions.append((f"🇯🇵 {_jp_label}", _jp_ticker))
    except Exception:
        pass

    # Layer 3: yfinance search fallback for TSE tickers not in local cache
    # (runs only if we still have empty slots and query is ≥3 chars)
    if len(q) >= 3 and len(suggestions) < 10:
        try:
            import yfinance as _yf_jp
            _yf_results = _yf_jp.Search(
                q + " japan",
                max_results=8,
                news_count=0,
            )
            for _yf_item in (_yf_results.quotes or []):
                _sym = (_yf_item.get("symbol") or "").strip().upper()
                if not _sym.endswith(".T"):
                    continue
                if _sym in seen:
                    continue
                seen.add(_sym)
                _yf_name = (
                    _yf_item.get("shortname")
                    or _yf_item.get("longname")
                    or ""
                )
                suggestions.append((
                    f"🇯🇵 {_sym:<10}  {_yf_name[:50]}  · TSE",
                    _sym,
                ))
                if len(suggestions) >= 12:
                    break
        except Exception:
            pass

    return suggestions[:12]


def _peer_search(query: str) -> list[tuple[str, str]]:
    """
    Autocomplete callback for the Peers picker. Reuses _smart_search but
    filters out:
      - NL-prompt suggestions (Peers must be real tickers).
      - Tickers the user has already added (no duplicates in the tag row).
    """
    rows = _smart_search(query)
    already = set(st.session_state.get("rg_peers_list", []))
    out: list[tuple[str, str]] = []
    for label, value in rows:
        if value.startswith("NL::"):
            continue
        if value in already:
            continue
        out.append((label, value))
    return out


def _render_peer_picker(scope: str) -> None:
    """
    Render the autocomplete Peers picker for one viewport scope.

    UX:
      • st_searchbox suggests tickers as the user types.
      • Picking one appends it to st.session_state.rg_peers_list (max 6).
      • Each selected peer renders below as a "TICKER ✕" button — clicking
        removes it from the list.

    Two scopes ("mobile", "desktop") share the same rg_peers_list so the
    selection is identical on both viewports; only the visible picker
    differs (CSS hides one of the two via the anchor pattern).
    """
    if "rg_peers_list" not in st.session_state:
        st.session_state.rg_peers_list = []

    pick_key = f"peers_pick_{scope}"
    picked = st_searchbox(
        search_function=_peer_search,
        placeholder="add peer",
        label=None,
        clear_on_submit=True,
        key=pick_key,
    )
    if picked and picked not in st.session_state.rg_peers_list:
        if len(st.session_state.rg_peers_list) < 6:
            st.session_state.rg_peers_list.append(picked)
            st.rerun()

    # Tag row — one removable button per selected peer.
    peers_now = list(st.session_state.rg_peers_list)
    if peers_now:
        n = len(peers_now)
        cols = st.columns(n)
        for i, p in enumerate(peers_now):
            if cols[i].button(
                f"{p} ✕",
                key=f"peers_rm_{scope}_{p}",
                help="Remove from peers list",
                use_container_width=True,
            ):
                st.session_state.rg_peers_list.remove(p)
                # streamlit_searchbox keeps the last selected value in
                # session_state across reruns. If we don't clear it here,
                # the next render reads picked == p, sees p is no longer
                # in rg_peers_list, and silently re-adds it — the user
                # then can't remove the last peer at all. Wipe every
                # picker's session_state key for both viewports so the
                # next render starts from an empty searchbox.
                for sc in ("mobile", "desktop"):
                    prefix = f"peers_pick_{sc}"
                    for k in list(st.session_state.keys()):
                        if k.startswith(prefix):
                            del st.session_state[k]
                st.rerun()
        st.caption(f"{n}/6 peers selected")


# ── Add ticker to My Portfolio (from screener row) ───────────────────────────
def _rg_add_to_portfolio(ticker: str) -> bool:
    """
    Persist a ticker into data/portfolio.json so it shows up in the
    My Portfolio page. Returns True if added, False if it was already there.
    """
    from pathlib import Path as _P
    pf = _P(__file__).resolve().parent.parent / "data" / "portfolio.json"
    pf.parent.mkdir(exist_ok=True)
    existing = []
    if pf.exists():
        try:
            existing = list(json.loads(pf.read_text(encoding="utf-8")).get("tickers", []))
        except Exception:
            existing = []
    if ticker in existing:
        return False
    existing.append(ticker)
    pf.write_text(json.dumps({"tickers": existing}, indent=2),
                  encoding="utf-8")
    return True


# ── Framework registry (loaded dynamically from frameworks/ directory) ────────
def _build_report_types() -> dict:
    """Build the REPORT_TYPES dict from the FrameworkManager."""
    fm = FrameworkManager()
    frameworks = fm.list()
    result = {}
    for fw in frameworks:
        result[fw.id] = {
            "label": f"{fw.icon} {fw.name}",
            "short": fw.name,
            "desc":  fw.description,
            "pages": "PDF report" if fw.uses_builtin_runner else "HTML report",
            "is_builtin": fw.is_builtin,
            "uses_builtin_runner": fw.uses_builtin_runner,
        }
    # Relocate Earnings Quality Score to right after Fisher Alternatives +
    # Peers (2026-07-23 user request). Done here rather than via
    # FrameworkManager.set_order() because data/framework_order.json is
    # gitignored (user-instance runtime state) and wouldn't survive a
    # redeploy on Streamlit Cloud.
    if "earnings_quality" in result and "fisher_peers" in result:
        eq = result.pop("earnings_quality")
        reordered = {}
        for k, v in result.items():
            reordered[k] = v
            if k == "fisher_peers":
                reordered["earnings_quality"] = eq
        result = reordered
    return result

REPORT_TYPES = _build_report_types()

# Builtin framework ids (for runner dispatch logic)
_BUILTIN_IDS = {"fisher", "fisher_peers", "gravity",
                "eodhd_full", "overview_v2", "index_overview",
                "industry_analysis", "insider_transactions",
                "valuemeter", "short_interest",
                "fund_fundamentals", "earnings_quality"}

# Temporarily hidden from the Valuation Models picker — these need
# significant adjustments before they're ready for use again. Framework
# files are untouched, so re-enabling is just removing an id from this set.
_HIDDEN_FW_IDS = {"fisher", "valuemeter"}

EXCHANGE_HINTS = {
    "Amsterdam (AEX)":   ".AS  e.g. WKL.AS, ASML.AS",
    "London (LSE)":      ".L   e.g. AZN.L, SHEL.L, INF.L",
    "Stockholm (STO)":   ".ST  e.g. ATCO-A.ST, SWED-A.ST",
    "Frankfurt (XETRA)": ".DE  e.g. SAP.DE, BAYN.DE",
    "Helsinki (OMX)":    ".HE  e.g. NOKIA.HE, SAMPO.HE",
    "Paris (EPA)":       ".PA  e.g. MC.PA, SAN.PA",
    "Toronto (TSX)":     ".TO  e.g. TRI.TO, RY.TO",
    "Tokyo (TSE)":       ".T   e.g. 7203.T, 6758.T",
    "US (NYSE/NASDAQ)":  "No suffix — AAPL, MSFT, V, MCO",
}


# ── Natural-language intent parser ────────────────────────────────────────────

def _parse_intent_regex(q: str) -> dict:
    """Fast regex-only fallback: extract ticker, framework and mode."""
    import re
    q_up = q.upper()
    q_lo = q.lower()
    result: dict = {"ticker": None, "framework_id": None,
                    "mode": "equity", "force_refresh": False}

    # Ticker: index (^OMXH25) > suffix (NOKIA.HE) > bare caps
    m = re.search(r"\^[A-Z0-9]+", q_up)
    if m:
        result["ticker"] = m.group()
    else:
        m = re.search(r"\b([A-Z]{1,6}\.[A-Z]{1,3})\b", q_up)
        if m:
            result["ticker"] = m.group(1)

    # Framework keywords
    fw_hints = {
        "gravity":     ["gravity", "taxer", "choke", "toll"],
        "fisher":      ["fisher", "scuttlebutt", "philip fisher"],
        "overview_v2": ["overview", "memo", "investment memo", "helmer",
                        "7 power", "seven power"],
        "earnings_quality": ["earnings quality", "earnings quality score",
                              "forensic accounting", "accrual", "sloan"],
    }
    for fw_id, kws in fw_hints.items():
        if any(k in q_lo for k in kws):
            result["framework_id"] = fw_id
            break

    # Mode
    if result["ticker"] and result["ticker"].startswith("^"):
        screen_kws = ["screen", "constituent", "compan", "stock", "member",
                      "all ", "each ", "list of", "run "]
        result["mode"] = (
            "universe_screen"
            if any(k in q_lo for k in screen_kws)
            else "index_overview"
        )

    # Force refresh
    if any(k in q_lo for k in ["careful", "fresh data", "re-fetch",
                                "refetch", "new data"]):
        result["force_refresh"] = True

    return result


def _parse_intent(query: str) -> dict:
    """
    Parse a free-text query into structured intent via regex + LLM fallback.
    Returns dict: {ticker, framework_id, mode, force_refresh}.
    Returns {} if the input looks like a plain ticker symbol (no whitespace).
    """
    import re
    q = query.strip()
    if not q or not re.search(r"\s", q):
        return {}   # plain ticker — nothing to parse

    # Regex pass
    intent = _parse_intent_regex(q)

    # Enough info from regex alone?
    if intent.get("ticker") and intent.get("framework_id"):
        return intent

    # LLM pass for ambiguous queries
    fw_list = "\n".join(
        f"  {k}: {REPORT_TYPES[k]['short']}" for k in REPORT_TYPES
    )
    system = (
        "You extract structured financial analysis intent from natural language. "
        "Reply with valid JSON only — no markdown, no explanation."
    )
    prompt = (
        f'Query: "{q}"\n\n'
        f'Available frameworks:\n{fw_list}\n\n'
        'Return JSON:\n'
        '{\n'
        '  "ticker": "<Yahoo Finance ticker or null>",\n'
        '  "framework_id": "<exact framework id from list or null>",\n'
        '  "mode": "<equity|index_overview|universe_screen>",\n'
        '  "force_refresh": <true|false>\n'
        '}\n\n'
        'Rules:\n'
        '- Index ticker + analyse/screen its companies → mode=universe_screen\n'
        '- Index overview/performance/composition only → mode=index_overview\n'
        '- Single stock analysis → mode=equity\n'
        '- "carefully", "fresh data", "re-fetch" → force_refresh=true\n'
        '- framework_id must exactly match one of the ids listed'
    )
    try:
        llm = LLMClient()
        if not llm.check_configured()[0]:
            return intent
        result = llm.generate_json(prompt, system, max_tokens=200)
        # Merge: LLM wins, fill gaps with regex
        for k in ("ticker", "framework_id", "mode", "force_refresh"):
            if not result.get(k):
                result[k] = intent.get(k)
        if result.get("framework_id") not in REPORT_TYPES:
            result["framework_id"] = intent.get("framework_id")
        return result
    except Exception:
        return intent


# ── Session state ─────────────────────────────────────────────────────────────
if "report_result" not in st.session_state:
    st.session_state.report_result = None   # {pdf_path, company, analysis, report_type}
if "recent_reports" not in st.session_state:
    st.session_state.recent_reports = []    # list of {label, path, ts}
if "error_msg" not in st.session_state:
    st.session_state.error_msg = None


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📊 Your Humble EquityBot")
    st.caption("Value investing · Decade-scale horizon · Three frameworks")
    st.divider()

    # LLM status
    llm = LLMClient()
    ok, msg = llm.check_configured()
    if ok:
        st.success(f"✓ {msg}", icon="🤖")
    else:
        st.error(f"⚠ {msg}", icon="🔑")

    st.divider()

    # Exchange suffix reference
    with st.expander("🌍 Exchange ticker formats", expanded=False):
        for exch, hint in EXCHANGE_HINTS.items():
            st.markdown(f"**{exch}**  \n`{hint}`")

    st.divider()

    # Recent reports
    if st.session_state.recent_reports:
        st.markdown("#### Recent Reports")
        for r in reversed(st.session_state.recent_reports[-5:]):
            with open(r["path"], "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            st.download_button(
                label=r["label"],
                data=base64.b64decode(b64),
                file_name=Path(r["path"]).name,
                mime="application/pdf",
                key=f"dl_{r['ts']}",
                use_container_width=True,
            )
        st.divider()

    # ── Data waterfall ────────────────────────────────────────────────────
    with st.expander("📡 Data Sources & Waterfall", expanded=True):
        st.markdown("""
<style>
.wf-tier   { font-size:11px; font-weight:700; color:#8a6a30; letter-spacing:.06em;
             text-transform:uppercase; margin:10px 0 2px 0; font-family:monospace; }
.wf-row    { display:flex; align-items:flex-start; gap:7px; margin:3px 0; }
.wf-badge  { flex-shrink:0; font-size:10px; font-weight:700; padding:1px 6px;
             border-radius:2px; margin-top:1px; font-family:monospace; }
.wf-paid   { background:#FFA028; color:#000000; }
.wf-free   { background:#000000; color:#FFA028; border:1px solid #4a3818; }
.wf-ctx    { background:#0a0a0a; color:#8a6a30; border:1px solid #2a1f10; }
.wf-body   { font-size:12px; line-height:1.4; color:#FFA028; font-family:monospace; }
.wf-body b { color:#FFD89C; }
.wf-arrow  { color:#4a3818; font-size:13px; margin:1px 0 1px 10px; }
.wf-miss   { font-size:11px; color:#8a6a30; margin:4px 0 0 0; font-family:monospace; }
</style>

<div class="wf-tier">① Always — market skeleton</div>
<div class="wf-row">
  <span class="wf-badge wf-free">FREE</span>
  <div class="wf-body"><b>yfinance</b><br>Current price · shares · live ratios</div>
</div>

<div class="wf-arrow">↓</div>

<div class="wf-tier">② Primary fundamentals — all markets</div>
<div class="wf-row">
  <span class="wf-badge wf-paid">PAID</span>
  <div class="wf-body"><b>EODHD</b> Fundamentals Feed<br>
  Overrides annual income · balance sheet · cash flow<br>
  65+ exchanges · 20–40 yr history</div>
</div>

<div class="wf-arrow">↓</div>

<div class="wf-tier">③ US depth — fill-only after EODHD</div>
<div class="wf-row">
  <span class="wf-badge wf-free">FREE</span>
  <div class="wf-body"><b>SEC EDGAR</b><br>US only · direct SEC filings</div>
</div>

<div class="wf-arrow">↓</div>

<div class="wf-tier">④ Last resort — if EODHD unavailable</div>
<div class="wf-row">
  <span class="wf-badge wf-free">FREE</span>
  <div class="wf-body"><b>Alpha Vantage</b><br>25 calls/day · non-US only</div>
</div>

<hr style="margin:10px 0; border-color:#EEE;">

<div class="wf-tier">Context — injected into every report</div>
<div class="wf-row">
  <span class="wf-badge wf-ctx">FREE</span>
  <div class="wf-body"><b>FRED</b> — rates · CPI · credit spreads</div>
</div>
<div class="wf-row">
  <span class="wf-badge wf-ctx">FREE</span>
  <div class="wf-body"><b>NewsAPI</b> — 8 recent headlines</div>
</div>
<div class="wf-row">
  <span class="wf-badge wf-ctx">FREE</span>
  <div class="wf-body"><b>World Bank</b> — GDP · inflation · debt</div>
</div>

<hr style="margin:10px 0; border-color:#EEE;">

<div class="wf-tier">EODHD global coverage</div>
<div class="wf-body" style="font-size:11px; line-height:1.6;">
✅ US · EU · Korea · Taiwan · China<br>
✅ HK · Brazil · Canada · SE Asia · Africa<br>
⚠️ Japan · India · Singapore → yfinance only
</div>
""", unsafe_allow_html=True)

    # ── Frameworks & LLM ──────────────────────────────────────────────────
    with st.expander("🔍 Frameworks & AI", expanded=False):
        st.markdown("""
**Frameworks**
- Philip Fisher (Common Stocks, 1958)
- Hamilton Helmer (7 Powers, 2016)
- Gravity Taxers (choke-point businesses)

**LLM providers**
- Anthropic Claude (primary)
- OpenAI GPT-4o (fallback / adversarial)
""")


# ── Main area ─────────────────────────────────────────────────────────────────
# CSS: lift our compact title above Streamlit's stAppToolbar so the
# toolbar doesn't cover the heading. The toolbar uses z-index ≈ 999999,
# so we go one higher. position:relative is required for z-index to take
# effect on a flow-positioned element.
st.markdown(
    "<style>"
    ".eq-page-title {"
    "  position: relative;"
    "  z-index: 1000001;"
    "  background: #000000;"
    "  padding: 4px 0 6px 0;"
    "  border-bottom: 1px solid #2a1f10;"
    "}"
    # Centre the title on mobile so it doesn't sit hard-left under
    # the burger / sidebar icon.
    "@media (max-width: 768px) {"
    "  .eq-page-title { justify-content: center !important; }"
    "}"
    # Shrink the auto-styled h4 headings Streamlit wraps subheaders /
    # #### markdown blocks in, so they don't dominate the layout.
    ".st-emotion-cache-1dy2t46 h4 { font-size: 1rem !important; }"
    ".st-emotion-cache-1dy2t46 { margin-bottom: -10px; }"
    # ── Peer Tickers visibility ──────────────────────────────────
    # Two parallel Peer Tickers inputs are rendered: a mobile copy
    # in col_left (anchored .rg-peer-mobile-anchor) so it sits right
    # after the ticker searchbox on phones, and a desktop copy in
    # col_right (anchored .rg-peer-desktop-anchor) for the original
    # side-by-side desktop form. Each anchor sits as the FIRST of a
    # 3-element sequence (anchor markdown → header markdown → input)
    # so the rules hide the anchor element-container and its next
    # two siblings together. Both Streamlit testid spellings are
    # listed for compatibility.
    ".rg-peer-mobile-anchor { display: none; }"
    ".rg-peer-desktop-anchor { display: none; }"
    ".rg-peers-mobile-wrap { display: none; }"
    ".rg-peers-desktop-wrap { display: none; }"
    ".rg-style-iframe-anchor { display: none; }"
    # ── Always hide the rg-style-iframe-anchor wrapper and its
    # immediately-following stElementContainer (the JS-injection
    # iframe). display:none still allows the iframe script to run.
    "div[data-testid=\"stElementContainer\"]:has(.rg-style-iframe-anchor),"
    "div[data-testid=\"element-container\"]:has(.rg-style-iframe-anchor),"
    "div.stElementContainer:has(.rg-style-iframe-anchor),"
    "div.element-container:has(.rg-style-iframe-anchor),"
    "div[data-testid=\"stElementContainer\"]:has(.rg-style-iframe-anchor) + div[data-testid=\"stElementContainer\"],"
    "div[data-testid=\"element-container\"]:has(.rg-style-iframe-anchor) + div[data-testid=\"element-container\"],"
    "div.stElementContainer:has(.rg-style-iframe-anchor) + div.stElementContainer,"
    "div.element-container:has(.rg-style-iframe-anchor) + div.element-container {"
    "  display: none !important;"
    "}"
    # ── Collapse the anchor's stElementContainer wrapper too.
    # Without this, the wrapper still renders with Streamlit's
    # default ~1rem padding and leaves a visible gap above the
    # Peers section even though the inner anchor div is hidden.
    # :has()-based sibling rules below still match because
    # display:none keeps the element in the DOM. */
    "div[data-testid=\"stElementContainer\"]:has(.rg-peer-mobile-anchor),"
    "div[data-testid=\"stElementContainer\"]:has(.rg-peer-desktop-anchor),"
    "div[data-testid=\"stElementContainer\"]:has(.rg-peers-mobile-wrap),"
    "div[data-testid=\"stElementContainer\"]:has(.rg-peers-desktop-wrap),"
    "div[data-testid=\"element-container\"]:has(.rg-peer-mobile-anchor),"
    "div[data-testid=\"element-container\"]:has(.rg-peer-desktop-anchor),"
    "div[data-testid=\"element-container\"]:has(.rg-peers-mobile-wrap),"
    "div[data-testid=\"element-container\"]:has(.rg-peers-desktop-wrap),"
    "div.stElementContainer:has(.rg-peer-mobile-anchor),"
    "div.stElementContainer:has(.rg-peer-desktop-anchor),"
    "div.stElementContainer:has(.rg-peers-mobile-wrap),"
    "div.stElementContainer:has(.rg-peers-desktop-wrap),"
    "div.element-container:has(.rg-peer-mobile-anchor),"
    "div.element-container:has(.rg-peer-desktop-anchor),"
    "div.element-container:has(.rg-peers-mobile-wrap),"
    "div.element-container:has(.rg-peers-desktop-wrap) {"
    "  display: none !important;"
    "}"
    # ── Hide the inactive viewport's entire Peers container ─────
    # Each Peers section is wrapped in a st.container() (which
    # produces an inner stVerticalBlock). The container-anchor div
    # (.rg-peers-mobile-wrap / .rg-peers-desktop-wrap) is rendered
    # as the FIRST child element-container inside that vblock, so
    # the :has(> child .anchor) selector matches ONLY the inner
    # st.container's vblock — not the outer column vblock that
    # also (deep-) contains the anchor. Hiding the entire vblock
    # nukes label, searchbox, tag-row columns, and caption together.
    "@media (min-width: 769px) {"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div[data-testid=\"stElementContainer\"] .rg-peers-mobile-wrap),"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div[data-testid=\"element-container\"] .rg-peers-mobile-wrap),"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div.stElementContainer .rg-peers-mobile-wrap),"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div.element-container .rg-peers-mobile-wrap) {"
    "    display: none !important;"
    "  }"
    "}"
    "@media (max-width: 768px) {"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div[data-testid=\"stElementContainer\"] .rg-peers-desktop-wrap),"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div[data-testid=\"element-container\"] .rg-peers-desktop-wrap),"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div.stElementContainer .rg-peers-desktop-wrap),"
    "  div[data-testid=\"stVerticalBlock\"]:has(> div.element-container .rg-peers-desktop-wrap) {"
    "    display: none !important;"
    "  }"
    "}"
    "@media (min-width: 769px) {"
    "  div[data-testid=\"stElementContainer\"]:has(.rg-peer-mobile-anchor),"
    "  div[data-testid=\"element-container\"]:has(.rg-peer-mobile-anchor),"
    "  div.stElementContainer:has(.rg-peer-mobile-anchor),"
    "  div.element-container:has(.rg-peer-mobile-anchor),"
    "  div[data-testid=\"stElementContainer\"]:has(.rg-peer-mobile-anchor) + div[data-testid=\"stElementContainer\"],"
    "  div[data-testid=\"element-container\"]:has(.rg-peer-mobile-anchor) + div[data-testid=\"element-container\"],"
    "  div.stElementContainer:has(.rg-peer-mobile-anchor) + div.stElementContainer,"
    "  div.element-container:has(.rg-peer-mobile-anchor) + div.element-container,"
    "  div[data-testid=\"stElementContainer\"]:has(.rg-peer-mobile-anchor) + div[data-testid=\"stElementContainer\"] + div[data-testid=\"stElementContainer\"],"
    "  div[data-testid=\"element-container\"]:has(.rg-peer-mobile-anchor) + div[data-testid=\"element-container\"] + div[data-testid=\"element-container\"],"
    "  div.stElementContainer:has(.rg-peer-mobile-anchor) + div.stElementContainer + div.stElementContainer,"
    "  div.element-container:has(.rg-peer-mobile-anchor) + div.element-container + div.element-container {"
    "    display: none !important;"
    "  }"
    "}"
    "@media (max-width: 768px) {"
    "  div[data-testid=\"stElementContainer\"]:has(.rg-peer-desktop-anchor),"
    "  div[data-testid=\"element-container\"]:has(.rg-peer-desktop-anchor),"
    "  div.stElementContainer:has(.rg-peer-desktop-anchor),"
    "  div.element-container:has(.rg-peer-desktop-anchor),"
    "  div[data-testid=\"stElementContainer\"]:has(.rg-peer-desktop-anchor) + div[data-testid=\"stElementContainer\"],"
    "  div[data-testid=\"element-container\"]:has(.rg-peer-desktop-anchor) + div[data-testid=\"element-container\"],"
    "  div.stElementContainer:has(.rg-peer-desktop-anchor) + div.stElementContainer,"
    "  div.element-container:has(.rg-peer-desktop-anchor) + div.element-container,"
    "  div[data-testid=\"stElementContainer\"]:has(.rg-peer-desktop-anchor) + div[data-testid=\"stElementContainer\"] + div[data-testid=\"stElementContainer\"],"
    "  div[data-testid=\"element-container\"]:has(.rg-peer-desktop-anchor) + div[data-testid=\"element-container\"] + div[data-testid=\"element-container\"],"
    "  div.stElementContainer:has(.rg-peer-desktop-anchor) + div.stElementContainer + div.stElementContainer,"
    "  div.element-container:has(.rg-peer-desktop-anchor) + div.element-container + div.element-container {"
    "    display: none !important;"
    "  }"
    # ── Tighten the vertical gap between the end of col_left
    # (Report Framework) and the start of col_right (Options) when
    # the form columns wrap on mobile. The columns were created with
    # gap=\"large\" which leaves ~2rem of empty space; 22px keeps the
    # form compact. Scoped via the mobile-peer anchor so only the
    # form's outer horizontal block is affected.
    "  div[data-testid=\"stHorizontalBlock\"]:has(.rg-peer-mobile-anchor) {"
    "    row-gap: 22px !important;"
    "  }"
    "}"
    "</style>",
    unsafe_allow_html=True,
)
st.markdown(
    "<div class='eq-page-title' "
    "style='display:flex;align-items:center;gap:8px;margin:0;'>"
    "<span style='font-size:20px;'>📊</span>"
    "<span style='font-size:16px;font-weight:700;color:#FFA028;"
    "font-family:monospace;letter-spacing:1px;text-transform:uppercase;'>"
    "Report Generator</span></div>",
    unsafe_allow_html=True,
)

# ── Input form ────────────────────────────────────────────────────────────────
col_left, col_right = st.columns([1.4, 1], gap="large")

with col_left:
    st.markdown("#### Ticker or Description")

    # ── Smart searchbar (autocomplete + NL prompt) ────────────────────────────
    selected = st_searchbox(
        search_function=_smart_search,
        placeholder="find ticker",
        label=None,
        clear_on_submit=False,
        key="rg_searchbox",
    )

    # ── Inject red styling into the streamlit_searchbox iframe ─────
    # The searchbox is a custom Streamlit component rendered inside
    # an iframe served from the same origin as the app, so parent-
    # page JS can reach iframe.contentDocument and inject a <style>
    # block directly. Re-runs on a timer because Streamlit re-creates
    # the iframe on each rerender.
    # Anchor lets CSS hide this iframe's element-container on mobile
    # so it doesn't add empty vertical space between Ticker and Peers.
    st.markdown("<div class='rg-style-iframe-anchor'></div>",
                unsafe_allow_html=True)
    st.iframe(
        """
        <script>
        (function () {
          const STYLE_ID = 'eqbot-searchbox-red';
          const CSS = `
            input, .css-1d391kg input, [class*="control"] input {
              background-color: #000000 !important;
              color: #8B0000 !important;
              caret-color: #8B0000 !important;
              -webkit-text-fill-color: #8B0000 !important;
              font-family: monospace !important;
            }
            [class*="control"], [class*="-control"] {
              background-color: #000000 !important;
              border: none !important;
              box-shadow: inset 0 0 0 2px #8B0000 !important;
              border-radius: 0 !important;
              min-height: 38px !important;
            }
            [class*="control"]:hover, [class*="-control"]:hover,
            [class*="control--is-focused"], [class*="-control--is-focused"] {
              box-shadow: inset 0 0 0 2px #8B0000 !important;
            }
            input::placeholder {
              color: #804020 !important;
              -webkit-text-fill-color: #804020 !important;
              opacity: 1 !important;
            }
            [class*="placeholder"] { color: transparent !important; }
            /* Hide the entire indicators column (dropdown arrow,
               separator, loading spinner) — Ticker / Peers fields
               look cleaner without the caret on the right. The
               user-confirmed react-select class is .css-1wy0on6
               (the IndicatorsContainer wrapper); kept the broader
               [class*=...] selectors too as a safety net in case
               the hashed class changes after a react-select bump. */
            .css-1wy0on6,
            [class*="indicatorContainer"],
            [class*="IndicatorsContainer"],
            [class*="indicator-container"],
            [class*="dropdownIndicator"],
            [class*="DropdownIndicator"],
            [class*="indicatorSeparator"],
            [class*="IndicatorSeparator"],
            [class*="loadingIndicator"],
            [class*="LoadingIndicator"] {
              display: none !important;
            }
            [class*="menu"] {
              background-color: #000000 !important;
              border: 2px solid #8B0000 !important;
              border-radius: 0 !important;
            }
            [class*="option"] {
              background-color: #000000 !important;
              color: #FFA028 !important;
              font-family: monospace !important;
            }
            [class*="option--is-focused"], [class*="option"]:hover {
              background-color: #0d0000 !important;
              color: #8B0000 !important;
            }
            [class*="singleValue"] { color: #8B0000 !important; }
            body { background-color: #000000 !important; }
          `;

          function paint(doc, placeholder) {
            try {
              if (!doc) return;
              if (!doc.getElementById(STYLE_ID)) {
                const s = doc.createElement('style');
                s.id = STYLE_ID;
                s.textContent = CSS;
                doc.head.appendChild(s);
              }
              if (placeholder) {
                const inp = doc.querySelector('input');
                if (inp && !inp.value) inp.setAttribute('placeholder', placeholder);
              }
            } catch (e) { /* same-origin race / not ready */ }
          }

          function scan() {
            const parent = window.parent || window;
            // Collect matching iframes in DOM order. The ticker searchbox
            // (key="rg_searchbox") is always rendered first in the Python
            // script; all subsequent searchboxes are peer pickers.
            const matches = [];
            parent.document.querySelectorAll('iframe').forEach(f => {
              const title = (f.title || '').toLowerCase();
              const src   = (f.src   || '').toLowerCase();
              if (title.includes('searchbox') || src.includes('searchbox')) {
                matches.push(f);
              }
            });
            matches.forEach(function(f, idx) {
              const ph = idx === 0 ? 'find ticker' : 'add peer';
              try { paint(f.contentDocument, ph); } catch (e) {}
            });
          }

          scan();
          setInterval(scan, 800);
        })();
        </script>
        """,
        height=1,
    )

    # ── Mobile-only Peer Tickers (CSS hides this block on desktop) ────────
    # The user wants Peer Tickers to appear right after the ticker input on
    # phones. Render a parallel input here with a distinct key, then merge
    # its value with the canonical col_right input further down.
    # Wrap mobile peers in a st.container so we can hide the entire
    # block (label + searchbox + tag row + caption) on desktop with
    # a single CSS rule. The container-anchor div lets CSS identify
    # this specific st.container's stVerticalBlock via :has(> child).
    with st.container():
        st.markdown("<div class='rg-peers-mobile-wrap'></div>",
                    unsafe_allow_html=True)
        st.markdown("<div class='rg-peer-mobile-anchor'></div>",
                    unsafe_allow_html=True)
        st.markdown("#### Peers")
        _render_peer_picker("mobile")

    # Persist the selected ticker (or the parsed intent / screener rows) in
    # session state so the rest of the page can read it without rerunning
    # the searchbox.
    if "rg_active_ticker" not in st.session_state:
        st.session_state.rg_active_ticker = ""
    if "rg_intent" not in st.session_state:
        st.session_state.rg_intent = None
    if "rg_screener_rows" not in st.session_state:
        st.session_state.rg_screener_rows = None
    if "rg_nl_query" not in st.session_state:
        st.session_state.rg_nl_query = ""

    # Handle a fresh selection from the searchbox
    if selected and selected != st.session_state.get("_rg_last_selected"):
        st.session_state._rg_last_selected = selected
        if selected.startswith("NL::"):
            # ── Natural-language path ────────────────────────────────────────
            nl_q = selected[4:].strip()
            st.session_state.rg_nl_query = nl_q
            with st.spinner("🔮 Interpreting your query with the LLM…"):
                try:
                    from models.nl_intent import parse_intent as _parse_nl
                    intent, _intent_usage = _parse_nl(nl_q)
                except Exception as _e:
                    intent = {}
                    _intent_usage = {}
                    st.error(f"Intent parsing failed: {_e}")
            st.session_state.rg_intent = intent
            # Persist the prompt-parser's LLM usage so the cost block
            # under the eventual report can show how much the prompt
            # interpretation cost. Stays in session until a new prompt
            # is interpreted.
            st.session_state.rg_prompt_usage = _intent_usage
            # Show inline cost line for the prompt itself
            if _intent_usage:
                _show_token_usage(_intent_usage)

            action = (intent or {}).get("action")
            if action == "screen" and intent.get("universe"):
                # Run the EODHD screener
                _universe = intent["universe"]
                with st.spinner(
                    f"📊 Screening {_universe} "
                    f"by {intent.get('sort_by') or 'market_cap'} "
                    f"({intent.get('sort_dir') or 'desc'})…  this can take ~1 min the first time"
                ):
                    try:
                        from data_sources.screener_eodhd import screen_index
                        rows = screen_index(
                            _universe,
                            sort_by=intent.get("sort_by") or "market_cap",
                            sort_dir=intent.get("sort_dir") or "desc",
                            limit=intent.get("limit") or 10,
                        )
                    except Exception as _e:
                        rows = []
                        st.error(f"Screener failed: {_e}")
                if not rows:
                    st.warning(
                        f"⚠ Could not get constituents for **{_universe}** "
                        f"from EODHD. The index may not have components data "
                        f"available, or the ticker code might differ. Try a "
                        f"different index name or check {_universe} on EODHD."
                    )
                st.session_state.rg_screener_rows = rows
                st.session_state.rg_active_ticker = ""   # no single ticker yet

            elif action == "screen" and intent.get("thematic_query"):
                # ── Thematic universe — no index, LLM generates ticker list ──
                _tq    = intent["thematic_query"]
                _limit = intent.get("limit") or 10
                with st.spinner(
                    f"🤖  Resolving thematic universe: **{_tq}** "
                    f"(asking LLM for {_limit} tickers)…"
                ):
                    try:
                        import importlib
                        import models.thematic_resolver as _tr_mod
                        importlib.reload(_tr_mod)
                        from models.thematic_resolver import resolve_thematic
                        rows = resolve_thematic(_tq, _limit, llm)
                    except Exception as _e:
                        rows = []
                        st.error(f"Thematic resolver failed: {_e}")
                if rows:
                    # Pre-select framework if LLM detected one
                    _fid = intent.get("framework_id")
                    if _fid and _fid in REPORT_TYPES:
                        st.session_state["report_type"] = _fid
                    _fw_label = REPORT_TYPES.get(_fid or "", {}).get("label", "")
                    _action_hint = (
                        f"Click **Run {_fw_label} on all {len(rows)}** below ↓"
                        if _fw_label
                        else f"Select a framework below and click **Run [Framework] on all {len(rows)}**"
                    )
                    st.success(f"✓  Found **{len(rows)}** companies for: *{_tq}*  ·  {_action_hint}")
                else:
                    st.warning(
                        f"⚠ Could not resolve companies for: **{_tq}**. "
                        "Try rephrasing or use a specific index ticker (e.g. ^STOXX50E)."
                    )
                st.session_state.rg_screener_rows = rows
                st.session_state.rg_active_ticker = ""

            elif action in ("report", "compare") and intent.get("tickers"):
                # Pre-select the first ticker; if framework provided, set it too
                st.session_state.rg_active_ticker = intent["tickers"][0]
                st.session_state.rg_screener_rows = None
                fid = intent.get("framework_id")
                if fid:
                    st.session_state["report_type"] = fid
            else:
                # Could not parse → show notes and treat as fallback ticker
                st.warning(
                    f"Couldn't fully interpret the prompt"
                    + (f" — {intent.get('notes')}" if intent and intent.get("notes")
                       else "") + ". Try a ticker or rephrase."
                )
                st.session_state.rg_active_ticker = ""
                st.session_state.rg_screener_rows = None
        else:
            # ── Plain ticker pick ────────────────────────────────────────────
            st.session_state.rg_active_ticker = selected.strip().upper()
            st.session_state.rg_intent = None
            st.session_state.rg_screener_rows = None

    # Render screener result table inline (if any)
    if st.session_state.get("rg_screener_rows"):
        _intent_for_render = st.session_state.rg_intent or {}
        _rows_for_render = st.session_state.rg_screener_rows
        _universe  = _intent_for_render.get("universe") or ""
        _thematic  = _intent_for_render.get("thematic_query") or ""
        _sort_by   = _intent_for_render.get("sort_by") or "market_cap"
        _sort_dir  = _intent_for_render.get("sort_dir") or "desc"
        if _thematic:
            st.markdown(
                f"##### 🤖 {_thematic} · {len(_rows_for_render)} companies (LLM-resolved)"
            )
        else:
            st.markdown(
                f"##### 🔍 {_universe} · top {len(_rows_for_render)} by "
                f"**{_sort_by}** ({_sort_dir})"
            )
        if _intent_for_render.get("notes"):
            st.caption(f"💡 {_intent_for_render['notes']}")

        # Header
        sh = st.columns([0.3, 1.0, 2.1, 1.3, 1.2, 1.0, 0.9, 0.9, 0.45, 0.45])
        sh[0].markdown("<small style='color:#888;'>#</small>", unsafe_allow_html=True)
        sh[1].markdown("<small style='color:#888;'>Ticker</small>", unsafe_allow_html=True)
        sh[2].markdown("<small style='color:#888;'>Name</small>", unsafe_allow_html=True)
        sh[3].markdown("<small style='color:#888;'>Sector</small>", unsafe_allow_html=True)
        sh[4].markdown(f"<small style='color:#888;'><b>{_sort_by}</b></small>",
                       unsafe_allow_html=True)
        sh[5].markdown("<small style='color:#888;'>Price</small>", unsafe_allow_html=True)
        sh[6].markdown("<small style='color:#888;'>P/E</small>", unsafe_allow_html=True)
        sh[7].markdown("<small style='color:#888;'>ROE</small>", unsafe_allow_html=True)
        sh[8].markdown("<small style='color:#888;'>&nbsp;</small>", unsafe_allow_html=True)
        sh[9].markdown("<small style='color:#888;'>&nbsp;</small>", unsafe_allow_html=True)

        def _fmt_sort_val(metric, v):
            if v is None: return "—"
            try:
                v = float(v)
            except Exception:
                return "—"
            if metric in ("market_cap", "revenue"):
                if abs(v) >= 1e12: return f"{v/1e12:.2f}T"
                if abs(v) >= 1e9:  return f"{v/1e9:.2f}B"
                if abs(v) >= 1e6:  return f"{v/1e6:.2f}M"
                return f"{v:,.0f}"
            if metric in ("roe", "ebit_margin", "net_margin", "div_yield", "fcf_yield"):
                return f"{v*100:.2f}%"
            if metric in ("pe_ratio", "ev_ebit"):
                return f"{v:.2f}×"
            return f"{v:,.2f}"

        for _row in _rows_for_render:
            r = st.columns([0.3, 1.0, 2.1, 1.3, 1.2, 1.0, 0.9, 0.9, 0.45, 0.45])
            r[0].markdown(f"<small>{_row.get('rank', '')}</small>",
                          unsafe_allow_html=True)
            r[1].markdown(f"**{_row['ticker']}**")
            r[2].markdown(f"<small>{(_row.get('name') or '')[:34]}</small>",
                          unsafe_allow_html=True)
            r[3].markdown(f"<small style='color:#666;'>"
                          f"{(_row.get('sector') or '')[:18]}</small>",
                          unsafe_allow_html=True)
            r[4].markdown(f"<b>{_fmt_sort_val(_sort_by, _row.get(_sort_by))}</b>",
                          unsafe_allow_html=True)
            _px = _row.get('price')
            r[5].markdown(f"<small>{_px:,.2f}</small>" if _px else "—",
                          unsafe_allow_html=True)
            _pe = _row.get('pe_ratio')
            r[6].markdown(f"<small>{_pe:.1f}×</small>" if _pe else "—",
                          unsafe_allow_html=True)
            _roe = _row.get('roe')
            r[7].markdown(f"<small>{_roe*100:.1f}%</small>" if _roe is not None else "—",
                          unsafe_allow_html=True)
            with r[8]:
                if st.button("📊", key=f"scr_use_{_row['ticker']}",
                             help="Use this ticker — picks it for report generation"):
                    st.session_state.rg_active_ticker = _row['ticker']
                    st.session_state.rg_screener_rows = None
                    st.rerun()
            with r[9]:
                if st.button("➕", key=f"scr_add_{_row['ticker']}",
                             help="Add to My Portfolio"):
                    added = _rg_add_to_portfolio(_row['ticker'])
                    if added:
                        st.toast(f"✅ Added {_row['ticker']} to portfolio",
                                 icon="✅")
                    else:
                        st.toast(f"{_row['ticker']} already in portfolio",
                                 icon="ℹ️")

        # ── Bulk action: run currently selected framework on ALL rows ────────
        # Frameworks like Gravity Taxers and Fisher are explicitly designed
        # for multi-company comparison — running the analysis once and
        # producing a side-by-side HTML report.
        _current_fw_id = (
            (_intent_for_render.get("framework_id"))
            or st.session_state.get("report_type")
            or "overview_v2"
        )
        _current_fw_label = REPORT_TYPES.get(_current_fw_id, {}).get(
            "label", _current_fw_id
        )
        ba1, ba2 = st.columns([3, 2])
        with ba1:
            st.markdown(
                "<div style='padding-top:8px;color:#666;font-size:13px;'>"
                "💡 Pick a framework below, then run it on the whole list "
                "to get a side-by-side comparison report."
                "</div>",
                unsafe_allow_html=True,
            )
        with ba2:
            _bulk_label = (
                f"🚀 Run {_current_fw_label} on all {len(_rows_for_render)}"
            )
            if st.button(_bulk_label, use_container_width=True,
                         type="primary", key="scr_run_bulk"):
                st.session_state.rg_bulk_run = {
                    "tickers":      [r["ticker"] for r in _rows_for_render],
                    "universe":     _universe,
                    "framework_id": _current_fw_id,
                    "label":        f"{_universe} top {len(_rows_for_render)}",
                }
                st.rerun()

        st.markdown("<hr style='margin:6px 0;'>", unsafe_allow_html=True)

    # Compute working ticker_input from session_state — drives all the
    # existing form/dispatch logic below unchanged.
    ticker_input = st.session_state.get("rg_active_ticker", "") or ""
    _is_nl_query = False     # NL path now handled above; downstream form is
                             # always single-ticker once we reach this point.
    ticker_input = ticker_input.upper() if ticker_input else ""

    # ── Index / ETF detection ─────────────────────────────────────────────────
    # Quick heuristic: ^ prefix = definitely an index.
    # For NL queries, check if a ^ ticker is embedded in the text.
    import re as _re_idx
    _is_index_ticker = (
        ticker_input.startswith("^")
        or bool(_re_idx.search(r"\^[A-Z0-9]+", ticker_input.upper()))
    )

    if _is_index_ticker:
        st.info(
            "📈 **Market index detected.**  \n"
            "Choose how to analyse it below.",
            icon="📈",
        )
        index_mode = st.radio(
            "Index analysis mode",
            options=["index_overview", "universe_screen"],
            format_func=lambda k: {
                "index_overview":  "📊 Analyse the index  (performance · valuation · composition)",
                "universe_screen": "🔍 Screen constituents through a framework",
            }[k],
            label_visibility="collapsed",
            key="index_mode",
        )
    else:
        index_mode = None

    # ── Report Framework picker ───────────────────────────────────────────────
    # Hide index_overview for equity tickers; hide it also in universe-screen
    # mode (it's selected automatically); show all stock frameworks for screening.
    if _is_index_ticker and index_mode == "index_overview":
        _fw_options = ["index_overview"]
    elif _is_index_ticker and index_mode == "universe_screen":
        _fw_options = [k for k in REPORT_TYPES
                       if k != "index_overview" and k not in _HIDDEN_FW_IDS]
    else:
        _fw_options = [k for k in REPORT_TYPES
                       if k != "index_overview" and k not in _HIDDEN_FW_IDS]

    st.markdown(
        "#### Valuation Models"
        if not (_is_index_ticker and index_mode == "index_overview")
        else "#### Valuation Models (auto-selected)"
    )

    report_type = st.radio(
        "Report type",
        options=_fw_options,
        format_func=lambda k: REPORT_TYPES[k]["label"],
        label_visibility="collapsed",
        key="report_type",
        horizontal=False,
        disabled=(_is_index_ticker and index_mode == "index_overview"),
    )
    rt = REPORT_TYPES[report_type]
    builtin_badge = "" if rt.get("is_builtin", True) else "  ·  Custom"
    st.caption(f"{rt['desc']}  ·  {rt['pages']}{builtin_badge}")

    # ── Gravity Taxers: file upload for universe definition ───────────────────
    if report_type == "gravity":
        st.markdown(
            "<div style='margin-top:10px;'></div>",
            unsafe_allow_html=True,
        )
        st.markdown("#### 📂 Universe from File")
        st.caption(
            "Upload a PDF, CSV, or Excel file with company names or tickers. "
            "Or paste a list of company names / tickers in the text box below."
        )

        _grav_tab_file, _grav_tab_paste = st.tabs(["📎 Upload file", "✏️ Paste list"])

        _gravity_source_label = ""
        _gravity_raw_rows: list[dict] | None = None

        with _grav_tab_file:
            _uploaded = st.file_uploader(
                "Upload company list",
                type=["pdf", "csv", "xls", "xlsx"],
                key="gravity_file_upload",
                label_visibility="collapsed",
            )
            if _uploaded is not None:
                _upload_key = f"{_uploaded.name}_{_uploaded.size}"
                if st.session_state.get("_gravity_upload_key") != _upload_key:
                    with st.spinner(f"📂 Parsing **{_uploaded.name}**…"):
                        try:
                            import importlib
                            import utils.file_parser as _fp_mod
                            importlib.reload(_fp_mod)
                            from utils.file_parser import parse_file as _parse_file
                            _file_bytes = _uploaded.read()
                            _fp_rows = _parse_file(
                                _file_bytes,
                                _uploaded.name,
                                llm_client=llm,
                            )
                        except Exception as _fe:
                            _fp_rows = []
                            st.error(f"File parse error: {_fe}")
                    st.session_state["_gravity_upload_key"] = _upload_key
                    st.session_state["_gravity_parsed_rows"] = _fp_rows
                    st.session_state["_gravity_source_label"] = _uploaded.name

                _gravity_raw_rows = st.session_state.get("_gravity_parsed_rows") or []
                _gravity_source_label = st.session_state.get("_gravity_source_label", "")

                if not _gravity_raw_rows:
                    st.warning(
                        "⚠ Could not extract company data from this file.  \n"
                        "**Possible causes:** PDF text extraction library not yet "
                        "installed (app may still be redeploying — try again in a minute), "
                        "or the PDF is image-based.  \n"
                        "**Workaround:** use the **Paste list** tab and copy-paste "
                        "the company names/tickers directly."
                    )

        with _grav_tab_paste:
            _paste_text = st.text_area(
                "Paste company names or tickers (one per line, or comma-separated)",
                height=200,
                key="gravity_paste_text",
                placeholder="Taiwan Semiconductor Manufacturing\nSamsung Electronics\nTSMC\nAAPL, MSFT, GOOG",
            )
            _paste_btn = st.button(
                "🔍 Resolve tickers from pasted list",
                key="gravity_paste_resolve",
                use_container_width=True,
            )
            if _paste_btn and _paste_text.strip():
                with st.spinner("🤖 Resolving tickers with LLM…"):
                    try:
                        import importlib
                        import utils.file_parser as _fp_mod2
                        importlib.reload(_fp_mod2)
                        from utils.file_parser import _screener_row as _sr, \
                            _looks_like_ticker as _llt, _resolve_tickers as _rt

                        # Split on newlines and commas
                        import re as _re_paste
                        _raw_entries = [
                            e.strip()
                            for e in _re_paste.split(r"[,\n]+", _paste_text)
                            if e.strip()
                        ]
                        _paste_rows = [
                            _sr(i + 1, e if _llt(e) else e, e)
                            for i, e in enumerate(_raw_entries)
                        ]
                        # Resolve names to tickers
                        _paste_rows = _rt(_paste_rows, llm)
                    except Exception as _pe:
                        _paste_rows = []
                        st.error(f"Resolve failed: {_pe}")

                st.session_state["_gravity_parsed_rows"] = _paste_rows
                st.session_state["_gravity_source_label"] = "pasted list"
                st.session_state["_gravity_upload_key"] = None
                _gravity_raw_rows = _paste_rows
                _gravity_source_label = "pasted list"

            # Show previously pasted rows even without clicking resolve
            if _gravity_raw_rows is None:
                _gravity_raw_rows = st.session_state.get("_gravity_parsed_rows") or []
                _gravity_source_label = st.session_state.get("_gravity_source_label", "")

        # ── Common results display ────────────────────────────────────────────
        _parsed_rows = _gravity_raw_rows or []
        if _parsed_rows:
            st.success(
                f"✓ **{len(_parsed_rows)} companies** from {_gravity_source_label}"
            )
            with st.expander(
                f"📋 Companies extracted ({len(_parsed_rows)})", expanded=True
            ):
                for _pr in _parsed_rows:
                    _note = f" — {_pr['note']}" if _pr.get("note") else ""
                    st.markdown(
                        f"**{_pr['rank']}.** `{_pr['ticker']}`  {_pr['name']}{_note}"
                    )

            if st.button(
                f"⚖️ Load {len(_parsed_rows)} companies into Gravity Taxers",
                type="primary",
                use_container_width=True,
                key="gravity_load_from_file",
            ):
                st.session_state.rg_screener_rows = _parsed_rows
                st.session_state.rg_intent = {
                    "framework_id": "gravity",
                    "thematic_query": f"from {_gravity_source_label}",
                    "universe": None,
                    "action": "screen",
                    "sort_by": None,
                    "sort_dir": None,
                    "limit": len(_parsed_rows),
                    "notes": f"Universe loaded from: {_gravity_source_label}",
                }
                st.session_state["report_type"] = "gravity"
                st.session_state.rg_active_ticker = ""
                st.rerun()

with col_right:
    # Peer tickers — only relevant for Overview-style reports.
    # Anchor lets CSS hide the desktop copy on mobile (mobile copy lives
    # in col_left right after the ticker searchbox).
    # Same container wrap pattern as the mobile copy — lets CSS hide
    # the whole desktop peers block (label + searchbox + tag row +
    # caption) on mobile in a single rule.
    with st.container():
        st.markdown("<div class='rg-peers-desktop-wrap'></div>",
                    unsafe_allow_html=True)
        st.markdown("<div class='rg-peer-desktop-anchor'></div>",
                    unsafe_allow_html=True)
        st.markdown("#### Peers")
        _render_peer_picker("desktop")

    # Downstream code consumes peers_input as a space-separated string
    # of tickers. Build it from the shared session_state list so both
    # viewport pickers feed the same source of truth.
    peers_input = " ".join(st.session_state.get("rg_peers_list", []))

    st.markdown("#### Options")
    force_refresh = st.checkbox(
        "Force refresh data cache",
        value=False,
        help="Bypass the 24-hour cache and re-fetch all data from source.",
    )

    # Insider Transactions: period selector (only shown when that
    # framework is selected). The 12-month monthly summary table in the
    # PDF always uses the past year regardless of this — the selector
    # only controls how far back the individual-transaction log goes.
    if report_type == "insider_transactions":
        _period_choice = st.radio(
            "Transaction log window",
            options=("1y", "2y", "5y"),
            index=2,                       # default = 5 years
            horizontal=True,
            key="rg_insider_period_choice",
        )
        st.session_state.rg_insider_period = {
            "1y": 12, "2y": 24, "5y": 60,
        }[_period_choice]

    # Adversarial mode — needs both Claude and OpenAI keys
    _adv_available = bool(
        os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("OPENAI_API_KEY")
    )
    adversarial_on = st.checkbox(
        "⚔ Adversarial Mode  (Claude + GPT-4o)",
        value=_CFG_ADV_MODE,
        disabled=not _adv_available,
        help=(
            "Run the analysis twice — once with Claude, once with GPT-4o — "
            "then cross-critique and merge. Adds an extra review page to the PDF. "
            "Requires both ANTHROPIC_API_KEY and OPENAI_API_KEY."
            if _adv_available
            else "Requires both ANTHROPIC_API_KEY and OPENAI_API_KEY in .env."
        ),
    )
    st.markdown(" ")

    # Generate button — label adapts to mode
    if _is_index_ticker and index_mode == "index_overview":
        _btn_label = f"Generate Index Overview"
    elif _is_index_ticker and index_mode == "universe_screen":
        _btn_label = f"Screen {ticker_input or 'Index'} Constituents"
    else:
        _btn_label = f"Generate {rt['short']} Report"

    generate_clicked = st.button(
        _btn_label,
        type="primary",
        use_container_width=True,
        disabled=not ticker_input.strip() or not ok,
    )
    if not ok:
        st.caption("⚠ Add your API key to .env to enable report generation.")
    if not ticker_input.strip():
        st.caption("Enter a ticker or describe what to analyse above.")
    elif _is_nl_query:
        st.caption("💡 Natural language detected — click Generate to interpret and run.")


# ── Bulk run from screener table ──────────────────────────────────────────────
# Triggered by the "🚀 Run [Framework] on all N" button in the screener
# result section. We bypass the normal generate-button form entirely and
# dispatch a Universe Screen using the pre-filtered tickers from the table.
_bulk = st.session_state.pop("rg_bulk_run", None)
if _bulk:
    _b_tickers      = _bulk.get("tickers") or []
    _b_framework    = _bulk.get("framework_id") or "overview_v2"
    _b_universe     = _bulk.get("universe") or "CUSTOM"
    _b_label        = _bulk.get("label") or "Custom selection"
    if _b_tickers:
        st.session_state.report_result = None
        st.session_state.error_msg = None
        _b_fw_short = REPORT_TYPES.get(_b_framework, {}).get("short", _b_framework)
        with st.status(
            f"🚀 Running **{_b_fw_short}** on {len(_b_tickers)} companies "
            f"({_b_label})…",
            expanded=True,
        ) as _bulk_status:
            try:
                _bprog = st.progress(0, text="Initializing…")
                _bstep = st.empty()
                def _bulk_progress(pct: int, msg: str) -> None:
                    _bprog.progress(min(pct, 99), text=msg)
                    _bstep.write(msg)

                import importlib
                import models.universe_screener as _us_mod
                importlib.reload(_us_mod)
                from models.universe_screener import UniverseScreener
                _safe_uni = _b_universe.replace("^", "").replace(".", "_") or "custom"
                _safe_fw  = _b_framework.replace("_", "-")
                _date     = datetime.now().strftime("%Y-%m-%d")
                _pdf_path = str(
                    OUTPUTS_DIR /
                    f"{_safe_uni}_{_safe_fw}_universe_{_date}.pdf"
                )
                _screener  = UniverseScreener()
                _out_path = _screener.run(
                    index_ticker=_b_universe,
                    framework_id=_b_framework,
                    output_path=_pdf_path,
                    tickers=_b_tickers,
                    progress_cb=_bulk_progress,
                )
                _bprog.progress(100, text="✅  Report ready!")

                _bulk_status.update(
                    label=f"✅  {_b_fw_short} Universe Screen complete "
                          f"({len(_b_tickers)} companies)",
                    state="complete", expanded=False,
                )
                _b_usage = getattr(_screener, "last_usage", {}) or {}
                _show_token_usage(_b_usage)
                st.session_state.report_result = {
                    "pdf_path":    _out_path,
                    "company":     None,
                    "index_data":  None,
                    "analysis":    {},
                    "report_type": f"universe_{_b_framework}",
                    "rec":         "n/a",
                    "extra":       {},
                    "adversarial": None,
                    "usage_claude": _b_usage,
                    "usage_openai": None,
                }
                _bulk_label_chip = f"{_b_label} · {_b_fw_short} · {_date}"
                st.session_state.recent_reports.append({
                    "label": _bulk_label_chip,
                    "path":  _out_path,
                    "ts":    datetime.now().timestamp(),
                })
            except Exception as _e:
                _bulk_status.update(
                    label=f"❌  Bulk run failed",
                    state="error", expanded=True,
                )
                st.error(f"**Error:** {_e}")
                logger.exception("Bulk universe run failed")


# ── Report generation ─────────────────────────────────────────────────────────
if generate_clicked and ticker_input:
    st.session_state.report_result = None
    st.session_state.error_msg = None

    # ── Natural-language interpretation ───────────────────────────────────────
    _orig_query   = ticker_input
    _nl_intent: dict = {}

    if " " in _orig_query:          # has spaces → treat as NL
        with st.spinner("🔍 Interpreting query…"):
            _nl_intent = _parse_intent(_orig_query)

    # Compute effective values (parsed wins over form defaults)
    _eff_ticker  = (_nl_intent.get("ticker") or _orig_query).strip().upper()
    _eff_fw      = (
        _nl_intent["framework_id"]
        if _nl_intent.get("framework_id") in REPORT_TYPES
        else report_type
    )
    _eff_refresh = force_refresh or bool(_nl_intent.get("force_refresh"))

    # Mode: parsed intent > form radio > auto-detect from ticker
    _parsed_mode = _nl_intent.get("mode") if _nl_intent else None
    if _parsed_mode:
        _eff_mode = _parsed_mode if _eff_ticker.startswith("^") else None
    elif _eff_ticker.startswith("^"):
        _eff_mode = index_mode or "universe_screen"
    else:
        _eff_mode = None

    # Shadow outer variables — all downstream code uses these
    ticker_input  = _eff_ticker
    report_type   = _eff_fw
    index_mode    = _eff_mode
    force_refresh = _eff_refresh
    rt            = REPORT_TYPES[report_type]

    # Show interpretation summary when NL was used
    if _nl_intent and _nl_intent.get("ticker"):
        _mode_lbl = {
            "equity":          "single equity",
            "index_overview":  "index overview",
            "universe_screen": "screen all constituents",
        }.get(_eff_mode or "equity", _eff_mode or "equity")
        st.info(
            f"🔍 **Interpreted as:** `{_eff_ticker}`  ·  "
            f"**{rt['short']}**  ·  {_mode_lbl}"
            + ("  ·  🔄 force refresh" if _eff_refresh and not force_refresh else ""),
            icon="✅",
        )

    peer_list = [t.strip().upper() for t in peers_input.split() if t.strip()][:6]

    # ── INDEX OVERVIEW mode ───────────────────────────────────────────────────
    if index_mode == "index_overview":
        with st.status(
            f"Generating Index Overview for **{ticker_input}**...",
            expanded=True,
        ) as status:
            try:
                _prog = st.progress(0, text="Initializing…")
                _prog.progress(8, text="📡  Fetching index data…")
                st.write(f"📡  Fetching index/ETF data for **{ticker_input}**…")

                from models.index_runner import IndexRunner
                from data_sources.data_manager import DataManager as _DM

                _idx_data = _DM().get_index(ticker_input, force_refresh=force_refresh)
                st.write(f"✓  **{_idx_data.name}** · {_idx_data.index_type} "
                         f"· Level: {_idx_data.current_level}")
                _prog.progress(20, text="🤖  Running AI analysis — typically 20–60 s…")
                st.write("🤖  Running Index Overview analysis (Claude)…")

                safe = ticker_input.replace("^", "").replace(".", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_index_overview_{date}.html")

                runner = IndexRunner()
                html_path, _idx_analysis = runner.run(
                    ticker_input,
                    output_path=pdf_path,
                    force_refresh=force_refresh,
                )
                _idx_rec = _idx_analysis.get("recommendation", "n/a")
                _prog.progress(95, text="📄  Rendering report…")
                with open(html_path, "r", encoding="utf-8") as _f:
                    _html_content = _f.read()
                _prog.progress(100, text="✅  Report ready!")

                status.update(
                    label=f"✅  Index Overview ready for **{_idx_data.name}** · {_idx_rec}",
                    state="complete", expanded=False,
                )
                st.session_state.report_result = {
                    "pdf_path":    html_path,
                    "company":     None,
                    "index_data":  _idx_data,
                    "analysis":    _idx_analysis,
                    "report_type": "index_overview",
                    "rec":         _idx_rec,
                    "extra":       {"html_content": _html_content},
                    "adversarial": None,
                }
                label = f"{ticker_input} · Index Overview · {date}"
                st.session_state.recent_reports.append({
                    "label": label, "path": html_path,
                    "ts": datetime.now().timestamp(),
                })

            except Exception as e:
                st.session_state.error_msg = str(e)
                status.update(label="❌  Error", state="error", expanded=True)
                st.error(f"**Error:** {e}")

    # ── UNIVERSE SCREEN mode ──────────────────────────────────────────────────
    elif index_mode == "universe_screen":
        with st.status(
            f"Screening **{ticker_input}** constituents through {rt['short']}…",
            expanded=True,
        ) as status:
            try:
                _prog = st.progress(0, text="Initializing…")
                _step_text = st.empty()

                def _universe_progress(pct: int, msg: str) -> None:
                    _prog.progress(min(pct, 99), text=msg)
                    _step_text.write(msg)

                import importlib
                import models.universe_screener as _us2_mod
                importlib.reload(_us2_mod)
                from models.universe_screener import UniverseScreener
                safe_idx = ticker_input.replace("^", "").replace(".", "_")
                safe_fw  = report_type.replace("_", "-")
                date     = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe_idx}_{safe_fw}_universe_{date}.pdf")

                screener  = UniverseScreener()
                out_path = screener.run(
                    index_ticker=ticker_input,
                    framework_id=report_type,
                    output_path=pdf_path,
                    force_refresh=force_refresh,
                    progress_cb=_universe_progress,
                )
                _prog.progress(100, text="✅  Report ready!")

                status.update(
                    label=f"✅  {rt['short']} Universe Screen complete for {ticker_input}",
                    state="complete", expanded=False,
                )
                _uni_usage = getattr(screener, "last_usage", {}) or {}
                _show_token_usage(_uni_usage)
                st.session_state.report_result = {
                    "pdf_path":    out_path,
                    "company":     None,
                    "index_data":  None,
                    "analysis":    {},
                    "report_type": f"universe_{report_type}",
                    "rec":         "n/a",
                    "extra":       {},
                    "adversarial": None,
                    "usage_claude": _uni_usage,
                    "usage_openai": None,
                }
                label = f"{ticker_input} · {rt['short']} Screen · {date}"
                st.session_state.recent_reports.append({
                    "label": label, "path": out_path,
                    "ts": datetime.now().timestamp(),
                })

            except Exception as e:
                st.session_state.error_msg = str(e)
                status.update(label="❌  Error", state="error", expanded=True)
                st.error(f"**Error:** {e}")

    # ── EQUITY mode (existing flow) ───────────────────────────────────────────
    else:
      with st.status(
        f"Generating {rt['short']} report for **{ticker_input}**...",
        expanded=True,
      ) as status:
        try:
            dm = DataManager()

            # ── Progress bar ──────────────────────────────────────────────────
            _prog = st.progress(0, text="Initializing…")

            # ── Step 1: Data ──────────────────────────────────────────────────
            _prog.progress(5, text="📡  Fetching financial data…")
            st.write(f"📡  Fetching financial data for **{ticker_input}**...")
            company = dm.get(ticker_input, force_refresh=force_refresh)

            # Check we got meaningful data. A missing name alone is not fatal —
            # EODHD now fills it, but if everything is empty we should error out.
            _has_data = bool(
                company.annual_financials
                or company.current_price
                or company.market_cap
            )
            if not _has_data:
                # Genuinely empty — suggest alternatives
                _suggestions = _search_tickers(ticker_input, max_results=4)
                if _suggestions:
                    _hint = ", ".join(
                        f"**{s['symbol']}** ({s['name']})" for s in _suggestions[:3]
                    )
                    raise ValueError(
                        f"No data found for **{ticker_input}**. "
                        f"Did you mean: {_hint}?"
                    )
                else:
                    raise ValueError(
                        f"No data found for ticker **{ticker_input}**. "
                        f"Check the format — e.g. AAPL, WKL.AS, NOKIA.HE. "
                        f"Use the Yahoo Finance ticker symbol, not the company name."
                    )
            # If name is still missing after all sources, fall back to the ticker
            if not company.name:
                company.name = ticker_input

            # ── Japan / TSE detection ─────────────────────────────────────────
            # EODHD has no coverage for TSE-listed Japanese stocks. All
            # "EODHD-only" report pipelines (Overview V2, Fisher, Gravity,
            # Industry Analysis, ValueMeter) call fetch_company_data_eodhd_only()
            # which would replace `company` with empty data and confuse the LLM.
            # Flag Japanese tickers so each dispatch block can skip the EODHD
            # re-fetch and stay with the DataManager / yfinance company object.
            _is_japan = ticker_input.upper().endswith(".T")
            _is_baltic = ticker_input.upper().endswith((".VS", ".TL", ".RG"))

            if _is_japan:
                # Minimal bundle used instead of EODHD bundle for all Japan reports.
                _JAPAN_BUNDLE: dict = {
                    "endpoints_used": 1,   # yfinance only
                    "errors": ["EODHD does not cover TSE (Japan). Using yfinance data."],
                    "fundamentals": {}, "news": [], "insider_trades": [],
                    "sentiment": None, "eod": [], "events": [],
                    "financials_annual": {}, "financials_quarterly": {},
                }
                # yfinance sometimes returns stale company names for new Japanese
                # listings (e.g. 9166.T returned as "Godo Kaisha LINE" instead of
                # "Genda Inc"). Try to correct from our local Japan seed list.
                _jp_name_suspect = (
                    not company.name
                    or "godo kaisha" in (company.name or "").lower()
                    or "godo" in (company.name or "").lower()
                    or company.name == ticker_input
                )
                if _jp_name_suspect:
                    try:
                        from data_sources.japan_tickers import search_japan
                        _code = ticker_input.upper().replace(".T", "")
                        _jp_hits = search_japan(_code, max_results=1)
                        if _jp_hits:
                            # Label format: "9166.T    Genda Inc  (Consumer Cyclical)  · TSE"
                            _jp_raw_name = _jp_hits[0][0]
                            import re as _re_jp_name
                            _jp_name_m = _re_jp_name.search(r'\.T\s+(.+?)\s+(?:\(|\·|$)', _jp_raw_name)
                            if _jp_name_m:
                                _jp_corrected = _jp_name_m.group(1).strip()
                                if _jp_corrected and len(_jp_corrected) > 3:
                                    company.name = _jp_corrected
                                    st.write(f"🇯🇵  Company name corrected to: **{company.name}**")
                    except Exception:
                        pass
                # Set country if yfinance didn't populate it
                if not company.country:
                    company.country = "Japan"
                st.write(
                    "🇯🇵  **Japanese stock (TSE)** — EODHD not available. "
                    "Analysis uses yfinance data (price, financials, estimates). "
                    "Report will be less detailed than for EODHD-covered markets."
                )

            # Baltic bundle — used as fallback when EODHD-only fetchers are called.
            # EODHD lists VS/TL/RG but many smaller Baltic stocks are not indexed.
            # company is already populated by dm.get() (yfinance waterfall above).
            _BALTIC_BUNDLE: dict = {
                "endpoints_used": 1,
                "errors": ["EODHD may not cover this Baltic stock. Using yfinance data."],
                "fundamentals": {}, "news": [], "insider_trades": [],
                "sentiment": None, "eod": [], "events": [],
                "financials_annual": {}, "financials_quarterly": {},
            }

            yrs   = company.year_range()
            compl = company.completeness_pct()
            st.write(f"✓  **{company.name}** · {yrs} · {compl}% complete · "
                     f"sources: {', '.join(company.data_sources)}")
            _prog.progress(18, text="✓  Data loaded")

            # ── Fetch news + country macro ────────────────────────────────────
            _prog.progress(19, text="📰  Fetching recent news…")
            _news_articles = dm.get_news(company.name or ticker_input, ticker_input, max_articles=8)
            _news_block = dm._news.format_for_prompt(_news_articles) if _news_articles else ""
            if _news_articles:
                st.write(f"📰  {len(_news_articles)} recent news articles fetched")

            _country_macro_block = ""
            if company.country:
                # Map full country name to ISO2 code
                _COUNTRY_MAP = {
                    "Germany": "DE", "Finland": "FI", "France": "FR", "Sweden": "SE",
                    "Netherlands": "NL", "United Kingdom": "GB", "Italy": "IT",
                    "Spain": "ES", "Poland": "PL", "Norway": "NO", "Denmark": "DK",
                    "Switzerland": "CH", "Austria": "AT", "Belgium": "BE",
                    "United States": "US", "Japan": "JP", "South Korea": "KR",
                    "China": "CN", "India": "IN", "Brazil": "BR", "Canada": "CA",
                    "Australia": "AU",
                }
                _iso2 = _COUNTRY_MAP.get(company.country, company.country[:2].upper() if company.country else "")
                if _iso2:
                    try:
                        _cmacro = dm.get_country_macro(_iso2)
                        _country_macro_block = dm._wb.format_for_prompt(_cmacro)
                    except Exception:
                        _country_macro_block = ""

            # ── Step 2: LLM ───────────────────────────────────────────────────
            adv_label = " ⚔ adversarial" if adversarial_on else ""
            _prog.progress(22, text=f"🤖  Running AI analysis — typically 30–90 s…")
            st.write(f"🤖  Running {rt['short']} analysis "
                     f"({LLM_PROVIDER}/{LLM_MODEL}{adv_label})...")

            # Shared adversarial engine (instantiated once if needed)
            adv_result = None
            if adversarial_on:
                from agents.adversarial import AdversarialEngine
                _adv_engine = AdversarialEngine()
                st.write("⚔  Adversarial Mode: Claude + GPT-4o will run independently, "
                         "then cross-critique and merge...")

            if report_type == "overview_v2":
                # ── Investment Memo V2 — 100% EODHD data ───────────────────
                # Override `company` with an EODHD-only CompanyData built
                # directly from /fundamentals + /eod (no yfinance/Stooq/AV
                # ever runs). Peers are LLM-suggested but each peer's data
                # is also fetched EODHD-only.
                from data_sources.eodhd_only_builder import (
                    fetch_company_data_eodhd_only,
                )
                from models.overview import (
                    _overview_prompt_parts, _calculate_checklist,
                    SYSTEM_PROMPT as SYS,
                )
                _prog.progress(25, text="💎  Fetching EODHD-only data for V2…")
                if _is_japan:
                    _v2_bundle = _JAPAN_BUNDLE
                    st.write("🇯🇵  Using yfinance data for Japanese stock (EODHD not available)")
                elif _is_baltic:
                    _v2_bundle = _BALTIC_BUNDLE
                    st.write("🇧🇦  Using yfinance data for Baltic stock (EODHD may be incomplete)")
                else:
                    st.write("💎  Fetching EODHD bundle (fundamentals + /eod)…")
                    _eodhd_company, _v2_bundle = fetch_company_data_eodhd_only(ticker_input)
                    # Check whether EODHD actually returned usable data.
                    # If not (e.g. Indian .NS/.BO, Singapore .SI — exchanges EODHD
                    # does not cover), the returned object is an almost-empty shell.
                    # In that case keep the yfinance `company` that dm.get() already
                    # populated above, and use an empty bundle (same pattern as Japan).
                    _eodhd_usable = bool(
                        _eodhd_company.name
                        and (_eodhd_company.market_cap or _eodhd_company.annual_financials)
                    )
                    if _eodhd_usable:
                        company = _eodhd_company
                        st.write(f"✓  EODHD endpoints used: {_v2_bundle.get('endpoints_used',0)}/9")
                    else:
                        # EODHD has no data — fall back gracefully to yfinance
                        _v2_bundle = {
                            "endpoints_used": 0,
                            "errors": [f"EODHD returned no data for {ticker_input}. Using yfinance."],
                            "fundamentals": {}, "news": [], "insider_trades": [],
                            "sentiment": None, "eod": [], "events": [],
                            "financials_annual": {}, "financials_quarterly": {},
                        }
                        st.write(
                            f"⚠  EODHD has no data for **{ticker_input}**. "
                            "Falling back to yfinance — report will be less detailed."
                        )

                # Build the LLM prompt with EODHD context only — no news,
                # no macro blocks (they would be sourced outside EODHD).
                cacheable_pfx, dynamic_prompt = _overview_prompt_parts(
                    company, news_block="", macro_country_block="",
                )
                _prog.progress(35, text="🤖  Running LLM (EODHD-only context)…")
                st.write("🤖  Running LLM on EODHD-only context…")
                analysis = llm.generate_json(dynamic_prompt, SYS,
                                             max_tokens=12000,
                                             cacheable_prefix=cacheable_pfx)
                rec = analysis.get("recommendation", "n/a")
                st.write(f"✓  Recommendation: **{rec}**")
                _show_token_usage(llm.last_usage)
                _prog.progress(65, text="✓  AI analysis complete")

                # Peers — EODHD-only for non-Japan tickers; yfinance for .T peers
                _prog.progress(68, text="🔍  Fetching peer data…")
                st.write("🔍  Fetching peer data (EODHD or yfinance for TSE peers)…")
                peers: dict[str, CompanyData] = {}
                _llm_peers = [
                    p.get("ticker", "")
                    for p in analysis.get("suggested_peers", [])
                ]
                _seen: set[str] = set()
                raw_peers: list[str] = []
                for _t in list(peer_list) + _llm_peers:
                    _t = _t.strip().upper()
                    if _t and _t not in _seen:
                        _seen.add(_t)
                        raw_peers.append(_t)
                    if len(raw_peers) == 6:
                        break
                for pt in raw_peers:
                    try:
                        pd_ = None
                        src_label = "unknown"
                        _is_balt_peer = any(pt.upper().endswith(s)
                                            for s in (".VS", ".TL", ".RG"))
                        if pt.endswith(".T") or _is_balt_peer:
                            # Japan / Baltic → yfinance only
                            pd_ = dm.get(pt, force_refresh=False)
                            src_label = "yfinance (TSE)" if pt.endswith(".T") else "yfinance (Baltic)"
                        else:
                            # Try EODHD first, fall back to yfinance
                            try:
                                _eodhd_pd, _ = fetch_company_data_eodhd_only(pt)
                                _la = _eodhd_pd.latest_annual() if _eodhd_pd else None
                                if (_eodhd_pd and _eodhd_pd.name
                                        and (_eodhd_pd.market_cap or (_la and _la.revenue))):
                                    pd_ = _eodhd_pd
                                    src_label = "EODHD"
                            except Exception:
                                pass
                            if pd_ is None:
                                # EODHD unavailable or returned empty — fall back to yfinance
                                pd_ = dm.get(pt, force_refresh=False)
                                src_label = "yfinance"
                        # Keep only if we got meaningful data
                        la_check = pd_.latest_annual() if pd_ else None
                        has_rev = bool(la_check and la_check.revenue)
                        if pd_ and pd_.name and (pd_.market_cap or has_rev):
                            peers[pt] = pd_
                            st.write(f"   ✓ {pt}: {pd_.name} [{src_label}]")
                        else:
                            st.write(f"   ⚠ Peer {pt} returned no usable data — skipped")
                    except Exception as e:
                        st.write(f"   ⚠ Peer {pt} fetch failed: {e}")
                st.write(f"✓  {len(peers)} peers loaded: "
                         f"{', '.join(peers.keys()) or 'none'}")
                _prog.progress(78, text=f"✓  {len(peers)} peers")

                checklist = _calculate_checklist(company)
                passed = sum(1 for c in checklist if c["pass"])
                st.write(f"✓  Checklist: {passed}/{len(checklist)} criteria met")
                _prog.progress(84)

                # ── Current News — web-search narrative via LLM ───────────────
                _news_summary = {}
                _prog.progress(86, text="📰  Searching web for news…")
                st.write("📰  Searching web for current news…")
                try:
                    _news_narrative = llm.generate_web_news(
                        company.name or ticker_input, ticker_input, company=company
                    )
                    if _news_narrative:
                        _news_summary = {"narrative": _news_narrative}
                        st.write("✓  Current News: web search complete")
                    else:
                        st.write("⚠  Current News: no results returned")
                except Exception as _ne:
                    st.write(f"⚠  Current News skipped: {_ne}")

                _prog.progress(88, text="📄  Rendering V2 PDF…")
                st.write("📄  Rendering V2 PDF…")
                import importlib, agents.pdf_overview_v2 as _v2mod
                importlib.reload(_v2mod)
                from agents.pdf_overview_v2 import OverviewV2PDFGenerator
                # Wire EODHD bundle data onto company so pdf_overview_v2 can
                # pick it up without changing the render() signature.
                setattr(company, "_eod_data_v2", _v2_bundle.get("eod") or [])
                setattr(company, "_rt_data_v2",  _v2_bundle.get("realtime") or {})
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_overview_v2_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                OverviewV2PDFGenerator().render(company, analysis, peers,
                                                 checklist, pdf_path,
                                                 news_summary=_news_summary)
                extra = {"checklist": checklist, "passed": passed}

            elif report_type == "fisher":
                # ── Fisher Alternatives — EODHD-only data pipeline ────────────
                from data_sources.eodhd_only_builder import (
                    fetch_company_data_eodhd_only, fetch_peers_eodhd_only,
                )
                from data_sources.eodhd_macro import fetch_country_macro_block
                from models.fisher import (
                    _build_fisher_prompt, _fisher_prompt_parts,
                    _validate_analysis, SYSTEM_PROMPT as SYS,
                )

                # Step 1: EODHD-only company data (overrides the waterfall
                # company built earlier in this run).
                _prog.progress(25, text="🔬  Fetching EODHD-only Fisher data…")
                if _is_japan:
                    _fisher_bundle = _JAPAN_BUNDLE
                    st.write("🇯🇵  Using yfinance data for Japanese stock (EODHD not available)")
                elif _is_baltic:
                    _fisher_bundle = _BALTIC_BUNDLE
                    st.write("🇧🇦  Using yfinance data for Baltic stock (EODHD may be incomplete)")
                else:
                    st.write("🔬  Fetching EODHD bundle (fundamentals + /eod + news + sentiment + insider)…")
                    company, _fisher_bundle = fetch_company_data_eodhd_only(ticker_input)
                    st.write(f"✓  EODHD endpoints used: {_fisher_bundle.get('endpoints_used',0)}/9")

                # Step 2: Peers — user-provided list, EODHD-only fetch
                fisher_peers: dict = {}
                if peer_list:
                    _prog.progress(45, text="🔍  Fetching EODHD peer data…")
                    st.write(f"🔍  Fetching {len(peer_list)} peer(s) from EODHD…")
                    fisher_peers = fetch_peers_eodhd_only(
                        [p.strip().upper() for p in peer_list if p.strip()][:6]
                    )
                    st.write(f"✓  {len(fisher_peers)} peer(s) loaded: "
                             f"{', '.join(fisher_peers.keys()) or 'none'}")

                # Step 3: Country macro from EODHD /macro-indicator
                _prog.progress(55, text="🌍  Fetching country macro from EODHD…")
                fisher_country_macro = fetch_country_macro_block(company.country)
                if fisher_country_macro:
                    st.write(f"✓  EODHD macro for {company.country} loaded")

                # Step 4: Build prompt + run LLM
                cacheable_pfx, dynamic_prompt = _fisher_prompt_parts(
                    company,
                    bundle=_fisher_bundle,
                    peers=fisher_peers,
                    country_macro_block=fisher_country_macro,
                )

                if adversarial_on:
                    full_prompt = cacheable_pfx + "\n\n" + dynamic_prompt
                    adv_result = _adv_engine.run(full_prompt, SYS, max_tokens=6000,
                                                  report_type="fisher")
                    analysis = _validate_analysis(adv_result.merged)
                    score = analysis.get("fisher_total_score", "?")
                    grade = analysis.get("fisher_grade", "?")
                    st.write(f"✓  Merged Fisher Score: **{score}/75** (Grade {grade}) · "
                             f"Claude: {adv_result.primary_rec} / "
                             f"GPT-4o: {adv_result.secondary_rec}")
                    st.write(f"   Consensus: {len(adv_result.consensus_fields)} fields  ·  "
                             f"Contested: {len(adv_result.contested_fields)} fields")
                else:
                    analysis = llm.generate_json(dynamic_prompt, SYS, max_tokens=6000,
                                                 cacheable_prefix=cacheable_pfx)
                    analysis = _validate_analysis(analysis)
                    score = analysis.get("fisher_total_score", "?")
                    grade = analysis.get("fisher_grade", "?")
                    rec   = analysis.get("recommendation", "n/a")
                    st.write(f"✓  Fisher Score: **{score}/75** (Grade {grade}) · Rec: **{rec}**")
                    _show_token_usage(llm.last_usage)
                _prog.progress(75, text="✓  Fisher analysis complete")

                _prog.progress(88, text="📄  Rendering PDF…")
                st.write("📄  Rendering PDF...")
                from agents.pdf_fisher import FisherPDFGenerator
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_fisher_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                FisherPDFGenerator().render(company, analysis, pdf_path,
                                            adv_result=adv_result)
                extra = {"score": score, "grade": grade}

            elif report_type == "fisher_peers":
                # ── Fisher Alternatives + Peers — main Fisher then peer batch
                from data_sources.eodhd_only_builder import (
                    fetch_company_data_eodhd_only, fetch_peers_eodhd_only,
                )
                from data_sources.eodhd_macro import fetch_country_macro_block
                from models.fisher import (
                    _fisher_prompt_parts, _validate_analysis,
                    SYSTEM_PROMPT as SYS,
                )

                # Step 1: EODHD-only subject data
                _prog.progress(20, text="🔬  Fetching EODHD-only Fisher data…")
                if _is_japan:
                    _fpr_bundle = _JAPAN_BUNDLE
                    st.write("🇯🇵  Using yfinance data for Japanese stock (EODHD not available)")
                elif _is_baltic:
                    _fpr_bundle = _BALTIC_BUNDLE
                    st.write("🇧🇦  Using yfinance data for Baltic stock (EODHD may be incomplete)")
                else:
                    st.write("🔬  Fetching EODHD bundle (fundamentals + news + insider)…")
                    company, _fpr_bundle = fetch_company_data_eodhd_only(ticker_input)
                    st.write(f"✓  EODHD endpoints used: {_fpr_bundle.get('endpoints_used',0)}/9")

                # Step 2: Peers — user-selected peers fill slots first; LLM
                # suggestions backfill remaining slots up to 6 total.
                fpr_peers: dict = {}
                _fpr_suggest_usage: dict = {}
                _user_peer_tickers = [
                    p.strip().upper() for p in peer_list if p.strip()
                ]
                _slots_remaining = 6 - len(_user_peer_tickers)

                if _slots_remaining > 0:
                    _prog.progress(30, text="🤝  Asking LLM to suggest peers…")
                    if _user_peer_tickers:
                        st.write(
                            f"🤝  {len(_user_peer_tickers)} peer(s) supplied — "
                            f"asking LLM to fill up to {_slots_remaining} more…"
                        )
                    else:
                        st.write("🤝  No peers supplied — asking LLM for peer suggestions…")
                    try:
                        from models.fisher_peers import suggest_peers as _suggest_peers
                        _llm_suggested, _fpr_suggest_usage = _suggest_peers(
                            company, max_peers=_slots_remaining,
                        )
                    except Exception as _se:
                        _llm_suggested = []
                        st.warning(f"Peer suggestion failed: {_se}")
                    if _fpr_suggest_usage:
                        _show_token_usage(_fpr_suggest_usage)
                    # Deduplicate: LLM suggestions that aren't already user-picked
                    _user_set = set(_user_peer_tickers)
                    _llm_new = [t for t in _llm_suggested if t not in _user_set]
                    _peer_tickers_to_fetch = (_user_peer_tickers + _llm_new)[:6]
                    if _llm_new:
                        st.write(f"💡  LLM added peers: {', '.join(_llm_new)}")
                    elif not _user_peer_tickers:
                        st.warning(
                            "⚠ LLM could not suggest peers automatically. "
                            "The peer comparison page will be empty — try "
                            "adding peers manually in the **Peer Tickers** "
                            "field."
                        )
                else:
                    _peer_tickers_to_fetch = _user_peer_tickers[:6]

                if _peer_tickers_to_fetch:
                    _prog.progress(40, text="🔍  Fetching EODHD peer data…")
                    fpr_peers = fetch_peers_eodhd_only(_peer_tickers_to_fetch)
                    st.write(f"✓  {len(fpr_peers)} peer(s) loaded: "
                             f"{', '.join(fpr_peers.keys()) or 'none'}")

                # Step 3: Country macro for subject
                _prog.progress(45, text="🌍  Fetching country macro from EODHD…")
                fpr_country_macro = fetch_country_macro_block(company.country)

                # Step 4: Main Fisher LLM call (same as Fisher framework)
                cacheable_pfx, dynamic_prompt = _fisher_prompt_parts(
                    company,
                    bundle=_fpr_bundle,
                    peers=fpr_peers,
                    country_macro_block=fpr_country_macro,
                )

                if adversarial_on:
                    full_prompt = cacheable_pfx + "\n\n" + dynamic_prompt
                    adv_result = _adv_engine.run(
                        full_prompt, SYS, max_tokens=6000,
                        report_type="fisher",
                    )
                    analysis = _validate_analysis(adv_result.merged)
                    score = analysis.get("fisher_total_score", "?")
                    grade = analysis.get("fisher_grade", "?")
                    st.write(f"✓  Merged Fisher Score: **{score}/75** (Grade {grade})")
                else:
                    analysis = llm.generate_json(
                        dynamic_prompt, SYS, max_tokens=6000,
                        cacheable_prefix=cacheable_pfx,
                    )
                    analysis = _validate_analysis(analysis)
                    score = analysis.get("fisher_total_score", "?")
                    grade = analysis.get("fisher_grade", "?")
                    rec   = analysis.get("recommendation", "n/a")
                    st.write(f"✓  Fisher Score: **{score}/75** (Grade {grade}) · "
                             f"Rec: **{rec}**")
                    _show_token_usage(llm.last_usage)

                # Track main-Fisher Claude usage for the combined cost block.
                # Includes the peer-suggestion call too (when it happened).
                _fpr_main_usage = dict(llm.last_usage or {}) if not adversarial_on else {}
                if not adversarial_on and _fpr_suggest_usage:
                    for _k, _v in _fpr_suggest_usage.items():
                        _fpr_main_usage[_k] = (
                            (_fpr_main_usage.get(_k) or 0) + (_v or 0)
                        )

                # Step 5: Peer-batch Fisher LLM call
                peer_analyses: list = []
                if fpr_peers:
                    _prog.progress(75, text="🧮  Scoring peers (single LLM call)…")
                    st.write("🧮  Scoring peers with Fisher 15-point framework…")
                    try:
                        from models.fisher_peers import (
                            build_peer_prompt, validate_peer_analysis,
                            _PEERS_SYSTEM_PROMPT,
                        )
                        peer_cache_pfx, peer_dynamic = build_peer_prompt(
                            company, fpr_peers,
                        )
                        peer_raw = llm.generate_json(
                            peer_dynamic, _PEERS_SYSTEM_PROMPT,
                            max_tokens=4500,
                            cacheable_prefix=peer_cache_pfx,
                        )
                        peer_analyses = validate_peer_analysis(
                            peer_raw, list(fpr_peers.keys()),
                        )
                        # Show peer-batch token usage
                        _peer_usage = dict(llm.last_usage or {})
                        _show_token_usage(_peer_usage)
                        # Combine main + peer-batch usage so the cost
                        # block reflects everything Claude spent here.
                        if not adversarial_on:
                            for k, v in _peer_usage.items():
                                _fpr_main_usage[k] = (
                                    (_fpr_main_usage.get(k) or 0) +
                                    (v or 0)
                                )
                        st.write(f"✓  Scored {len(peer_analyses)} peer(s).")
                    except Exception as _pe:
                        st.warning(f"Peer batch scoring failed: {_pe}")
                        peer_analyses = []
                else:
                    _prog.progress(75, text="✓  Skipped peer scoring (no peers).")

                # Step 6: Render PDF
                _prog.progress(90, text="📄  Rendering Fisher + Peers PDF…")
                st.write("📄  Rendering Fisher + Peers PDF...")
                import importlib, agents.pdf_fisher_peers as _fprmod
                importlib.reload(_fprmod)
                from agents.pdf_fisher_peers import FisherPeersPDFGenerator
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_fisher_peers_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                FisherPeersPDFGenerator().render(
                    company, analysis, peer_analyses, fpr_peers,
                    pdf_path, adv_result=adv_result,
                )
                # Make the combined Claude usage available to the result
                # viewer's cost block. Stash on the llm client so the
                # generic capture path below picks it up.
                if not adversarial_on:
                    try:
                        llm.last_usage = _fpr_main_usage
                    except Exception:
                        pass
                extra = {
                    "score":      score,
                    "grade":      grade,
                    "peer_count": len(peer_analyses),
                }

            elif report_type == "industry_analysis":
                # ── Industry Analysis — Porter 5 Forces + Competitive Advantage
                # No peer fetching — this framework focuses on the industry
                # structure and the subject's own advantage, not a peer
                # comparison.
                from data_sources.eodhd_only_builder import (
                    fetch_company_data_eodhd_only,
                )
                from data_sources.eodhd_macro import fetch_country_macro_block
                from models.industry_analysis import (
                    _industry_prompt_parts, _validate_analysis,
                    SYSTEM_PROMPT as IA_SYS,
                )

                # Step 1: EODHD-only subject data (skipped for Japanese stocks)
                _prog.progress(20, text="🏛️  Fetching subject data…")
                if _is_japan:
                    _ia_bundle = _JAPAN_BUNDLE
                    st.write("🇯🇵  Using yfinance data for Japanese stock (EODHD not available)")
                elif _is_baltic:
                    _ia_bundle = _BALTIC_BUNDLE
                    st.write("🇧🇦  Using yfinance data for Baltic stock (EODHD may be incomplete)")
                else:
                    st.write("🏛️  Fetching EODHD bundle (10y financials + news + sentiment)…")
                    company, _ia_bundle = fetch_company_data_eodhd_only(ticker_input)
                    st.write(f"✓  EODHD endpoints used: {_ia_bundle.get('endpoints_used',0)}/9")

                # Step 2: Country macro
                _prog.progress(40, text="🌍  Fetching country macro from EODHD…")
                ia_country_macro = fetch_country_macro_block(company.country)

                # Step 3: Main LLM call — 1,300-1,700 word Porter analysis
                cacheable_pfx, dynamic_prompt = _industry_prompt_parts(
                    company,
                    bundle=_ia_bundle,
                    country_macro_block=ia_country_macro,
                )

                _prog.progress(55, text="🧠  Running Porter 5 Forces analysis…")
                st.write("🧠  Running Porter 5 Forces + Competitive Advantage "
                         "analysis (~1,300-1,700 words; typically 30-75 s)…")

                def _ia_run_main_call():
                    if adversarial_on:
                        full_prompt = cacheable_pfx + "\n\n" + dynamic_prompt
                        _adv = _adv_engine.run(
                            full_prompt, IA_SYS, max_tokens=13000,
                            report_type="overview",  # adversarial reuses overview merger
                        )
                        return _adv.merged, _adv
                    ra = llm.generate_json(
                        dynamic_prompt, IA_SYS, max_tokens=13000,
                        cacheable_prefix=cacheable_pfx,
                    )
                    _show_token_usage(llm.last_usage)
                    return ra, None

                def _ia_is_filled(ra) -> bool:
                    return (
                        isinstance(ra, dict) and
                        isinstance(ra.get("forces"), list) and
                        len([f for f in ra["forces"]
                             if isinstance(f, dict) and f.get("state_2026")
                             and len(f.get("state_2026", "")) > 80]) >= 3
                    )

                raw_analysis, adv_result = _ia_run_main_call()

                # "Thinking" models (Gemini/Kimi) can occasionally burn their
                # entire completion budget on hidden reasoning and emit zero
                # visible JSON — more likely for data/training-thin subjects
                # (e.g. small non-US caps) even with a generous token cap.
                # This is stochastic per-call, so a single retry often
                # succeeds where the first attempt returned nothing at all.
                if not _ia_is_filled(raw_analysis):
                    logger.warning(
                        "[IndustryAnalysis] Empty/incomplete response on first "
                        "attempt for %s (raw len=%d) — retrying once.",
                        ticker_input, len(getattr(llm, "last_raw_response", "") or ""),
                    )
                    st.write("⚠  First analysis attempt came back empty/incomplete — retrying once…")
                    raw_analysis, adv_result = _ia_run_main_call()

                # ── Diagnostics: detect empty / mostly-empty responses ──
                _ia_raw_for_debug = getattr(llm, "last_raw_response", "") or ""
                _ia_top_keys = list(raw_analysis.keys()) if isinstance(raw_analysis, dict) else []
                _ia_filled = _ia_is_filled(raw_analysis)
                if not raw_analysis or not _ia_top_keys or not _ia_filled:
                    # Surface a clear, actionable error with the raw LLM
                    # response so the user can see exactly what came back.
                    st.error(
                        "⚠ The LLM returned an empty or unparseable analysis. "
                        "Most fields will be blank in the PDF. See the raw "
                        "response below — common causes: model truncation, "
                        "JSON formatting issue, or model refusal."
                    )
                    st.write(
                        f"**Debug:** keys returned = `{_ia_top_keys[:8]}` · "
                        f"raw length = {len(_ia_raw_for_debug):,} chars"
                    )
                    with st.expander("📋 Raw LLM response (first 3,000 chars)",
                                     expanded=False):
                        st.code(_ia_raw_for_debug[:3000] or "(empty)",
                                language="json")
                    with st.expander("📋 Parsed dict", expanded=False):
                        st.json(raw_analysis if isinstance(raw_analysis, dict)
                                else {"_parse_failed": True})

                analysis = _validate_analysis(raw_analysis if isinstance(raw_analysis, dict) else {})
                st.write(
                    f"✓  Industry: **{analysis.get('industry_attractiveness')}** · "
                    f"Trajectory: **{analysis.get('trajectory')}** · "
                    f"Advantage: **{analysis.get('competitive_advantage_size')}**"
                )

                # Step 4: Dedicated SWOT call (separate from main analysis to
                # avoid token-limit truncation of the large Porter JSON).
                logger.info("[SWOT] Starting dedicated SWOT call for %s", ticker_input)
                _prog.progress(75, text="📊  Generating SWOT analysis…")
                st.write("📊  Generating SWOT analysis (separate call, ~15-30 s)…")
                try:
                    from models.industry_analysis import build_swot_prompt as _build_swot
                    logger.info("[SWOT] build_swot_prompt imported OK")
                    _swot_sys, _swot_user = _build_swot(company, analysis)
                    logger.info("[SWOT] Prompt built, calling LLM (max_tokens=2500)…")
                    _swot_raw = llm.generate_json(
                        _swot_user, _swot_sys, max_tokens=2500,
                    )
                    logger.info("[SWOT] LLM returned keys: %s", list(_swot_raw.keys()) if isinstance(_swot_raw, dict) else type(_swot_raw))
                    _show_token_usage(llm.last_usage)
                    # DeepSeek (and some OpenAI responses) may wrap the fields
                    # in a nested "swot" key or return non-string values.
                    # Unwrap nested key if the expected fields are absent.
                    if isinstance(_swot_raw, dict) and "swot" in _swot_raw and isinstance(_swot_raw["swot"], dict):
                        if not any(k in _swot_raw for k in ("summary", "strengths", "weaknesses")):
                            _swot_raw = _swot_raw["swot"]

                    def _swot_str(val) -> str:
                        if val is None:
                            return ""
                        if isinstance(val, str):
                            return val.strip()
                        if isinstance(val, list):
                            return " ".join(str(item) for item in val).strip()
                        return str(val).strip()

                    _swot_valid = {
                        "summary":       _swot_str(_swot_raw.get("summary")),
                        "strengths":     _swot_str(_swot_raw.get("strengths")),
                        "weaknesses":    _swot_str(_swot_raw.get("weaknesses")),
                        "opportunities": _swot_str(_swot_raw.get("opportunities")),
                        "threats":       _swot_str(_swot_raw.get("threats")),
                    }
                    logger.info("[SWOT] Validated fields: %s", {k: bool(v) for k, v in _swot_valid.items()})
                    if any(_swot_valid.values()):
                        analysis["swot"] = _swot_valid
                        logger.info("[SWOT] SWOT populated successfully.")
                        st.write("✓  SWOT analysis generated successfully.")
                    else:
                        logger.warning("[SWOT] All SWOT fields empty. Raw keys: %s", list(_swot_raw.keys()) if isinstance(_swot_raw, dict) else type(_swot_raw))
                        st.warning("⚠  SWOT call returned empty fields — SWOT page will be omitted.")
                        with st.expander("📋 Raw SWOT response (debug)", expanded=False):
                            st.code(getattr(llm, "last_raw_response", "(not available)")[:2000], language="json")
                except Exception as _swot_err:
                    import traceback
                    logger.exception("[SWOT] Exception during SWOT generation")
                    st.warning(f"⚠  SWOT generation failed — continuing without SWOT.")
                    st.code(traceback.format_exc(), language="python")

                _prog.progress(90, text="📄  Rendering Industry Analysis PDF…")
                st.write("📄  Rendering Industry Analysis PDF…")
                from agents.pdf_industry_analysis import IndustryAnalysisPDFGenerator
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_industry_analysis_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                IndustryAnalysisPDFGenerator().render(
                    company, analysis, pdf_path, adv_result=adv_result,
                )
                # We set rec field to a sensible mapping so the results
                # viewer doesn't show "n/a" for the recommendation chip.
                _ia_rec_map = {
                    "Very Attractive": "BUY",  "Attractive":   "BUY",
                    "Neutral":         "HOLD",
                    "Unattractive":    "SELL", "Very Unattractive": "SELL",
                }
                analysis["recommendation"] = _ia_rec_map.get(
                    analysis.get("industry_attractiveness", "Neutral"), "HOLD",
                )
                extra = {
                    "attractiveness": analysis.get("industry_attractiveness"),
                    "trajectory":     analysis.get("trajectory"),
                    "advantage":      analysis.get("competitive_advantage_size"),
                }

            elif report_type == "eodhd_full":
                # ── EODHD All-In-One full data dump ────────────────────────────
                # Standalone fetcher; NO other data sources. Fetches every
                # EODHD endpoint live and renders a 10-13 page PDF.
                _prog.progress(30, text="🗂️  Fetching all EODHD endpoints…")
                st.write("🗂️  Fetching all EODHD endpoints…")
                import importlib, agents.pdf_eodhd_full as _efullmod
                import data_sources.eodhd_all_in_one as _eaiomod
                importlib.reload(_eaiomod)
                importlib.reload(_efullmod)
                from data_sources.eodhd_all_in_one import EODHDAllInOneFetcher
                from agents.pdf_eodhd_full import EODHDFullGenerator
                bundle = EODHDAllInOneFetcher().fetch_all(ticker_input)
                _prog.progress(75, text=f"✓  {bundle['endpoints_used']}/9 endpoints OK")
                st.write(f"✓  {bundle['endpoints_used']}/9 endpoints OK"
                         + (f" — missing: {', '.join(bundle['errors'])}" if bundle['errors'] else ""))
                _prog.progress(85, text="📄  Rendering EODHD Full PDF…")
                st.write("📄  Rendering EODHD Full PDF…")
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_eodhd_full_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                EODHDFullGenerator().render(bundle, pdf_path)

                # The preview metric bar (Price/MCap/P/E/ROE/EBIT Margin)
                # reads from `company`, but DataManager occasionally leaves
                # current_price / roe / ebit_margin / currency unset (e.g.
                # yfinance blocked on Streamlit Cloud, EODHD Highlights
                # margins not back-filled into the dataclass). The bundle
                # we just fetched has all of these — back-fill the missing
                # scalars so the preview shows real numbers instead of
                # "n/a".
                def _to_float(v):
                    try:
                        if v is None or v == "" or v == "NA":
                            return None
                        return float(v)
                    except (ValueError, TypeError):
                        return None

                _fund = (bundle.get("fundamentals") or {}) if bundle else {}
                _general    = _fund.get("General")    or {}
                _highlights = _fund.get("Highlights") or {}
                _realtime   = (bundle.get("realtime") or {}) if bundle else {}

                if not company.current_price:
                    company.current_price = (
                        _to_float(_realtime.get("close"))
                        or _to_float(_realtime.get("previousClose"))
                    )
                if not company.currency_price:
                    company.currency_price = (
                        _general.get("CurrencyCode")
                        or _general.get("CurrencySymbol")
                        or _realtime.get("currency")
                        or company.currency
                        or ""
                    )
                if not company.market_cap:
                    _mc_mln = _to_float(_highlights.get("MarketCapitalizationMln"))
                    company.market_cap = (
                        _mc_mln * 1e6 if _mc_mln is not None
                        else _to_float(_highlights.get("MarketCapitalization"))
                    )
                if not company.pe_ratio:
                    company.pe_ratio = _to_float(_highlights.get("PERatio"))
                if not company.roe:
                    company.roe = _to_float(_highlights.get("ReturnOnEquityTTM"))
                if not company.ebit_margin:
                    company.ebit_margin = _to_float(
                        _highlights.get("OperatingMarginTTM")
                    )

                analysis = {}
                extra = {}

            elif report_type == "insider_transactions":
                # ── Insider Transactions report ─────────────────────────────
                # Small header (name · ticker · price · mcap) + 12-month
                # monthly summary table + individual-transaction log filtered
                # by the user's period selector (1y / 2y / 5y). Primary data
                # from EODHD /insider-transactions; falls back to
                # insidertrades.info for EU tickers EODHD doesn't cover.
                _prog.progress(40, text="👥  Fetching insider transactions…")
                st.write("👥  Fetching insider transactions…")
                import importlib
                import data_sources.insider_data as _ind_mod
                import data_sources.insidertrades_scraper as _its_mod
                import data_sources.openinsider_scraper as _oi_mod
                import agents.pdf_insider as _pi_mod
                importlib.reload(_its_mod)
                importlib.reload(_oi_mod)
                importlib.reload(_ind_mod)
                importlib.reload(_pi_mod)
                from data_sources.insider_data import fetch_insider_data
                from agents.pdf_insider import InsiderTransactionsGenerator

                # period_months chosen via the radio next to the framework
                # picker; defaults to 60 (5y) if the selector wasn't shown.
                _period_months = int(
                    st.session_state.get("rg_insider_period", 60)
                )

                # ── Back-fill the company header from EODHD ───────────────
                # DataManager.get() can leave company.market_cap / price
                # blank or wrong (e.g. units bug: SAP.DE came back as
                # "177.21K EUR" instead of "177.21B EUR"). Hit EODHD's
                # /fundamentals and /real-time directly so the header
                # shows correct numbers regardless of DataManager state.
                from data_sources.eodhd_all_in_one import EODHDAllInOneFetcher
                _ihdr_fetcher = EODHDAllInOneFetcher()
                try:
                    _ihdr_eod_ticker = _ihdr_fetcher._get and None  # noqa
                    from data_sources.eodhd_all_in_one import (
                        _convert_ticker as _ihdr_convert,
                    )
                    _eod_t = _ihdr_convert(ticker_input)
                    _ihdr_fund     = _ihdr_fetcher.fetch_fundamentals(_eod_t) or {}
                    _ihdr_realtime = _ihdr_fetcher.fetch_realtime(_eod_t) or {}
                except Exception:
                    _ihdr_fund, _ihdr_realtime = {}, {}

                def _ihdr_float(v):
                    if v is None or v == "" or v == "NA":
                        return None
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return None

                _ihdr_general    = _ihdr_fund.get("General")    or {}
                _ihdr_highlights = _ihdr_fund.get("Highlights") or {}

                # Always overwrite with the EODHD values when present, so a
                # bad DataManager value doesn't leak into the header.
                _rt_close = _ihdr_float(_ihdr_realtime.get("close"))
                if _rt_close and _rt_close > 0:
                    company.current_price = _rt_close
                else:
                    _rt_prev = _ihdr_float(_ihdr_realtime.get("previousClose"))
                    if _rt_prev and _rt_prev > 0:
                        company.current_price = _rt_prev

                _curr = (_ihdr_general.get("CurrencyCode")
                         or _ihdr_general.get("CurrencySymbol")
                         or _ihdr_realtime.get("currency"))
                if _curr:
                    company.currency_price = _curr
                    if not company.currency:
                        company.currency = _curr

                _mc_mln = _ihdr_float(_ihdr_highlights.get("MarketCapitalizationMln"))
                if _mc_mln is not None:
                    company.market_cap = _mc_mln * 1e6
                else:
                    _mc_raw = _ihdr_float(_ihdr_highlights.get("MarketCapitalization"))
                    if _mc_raw is not None:
                        company.market_cap = _mc_raw

                _name = _ihdr_general.get("Name")
                if _name and not company.name:
                    company.name = _name

                insider_bundle = fetch_insider_data(
                    yf_ticker=ticker_input,
                    company_name=getattr(company, "name", "") or "",
                    months_back=_period_months,
                )
                _prog.progress(
                    75,
                    text=(f"✓  {len(insider_bundle.get('transactions', []))} "
                          f"txns via {insider_bundle.get('source_used', 'none')}"),
                )
                st.write(
                    f"✓  {len(insider_bundle.get('transactions', []))} "
                    f"transactions via "
                    f"**{insider_bundle.get('source_used', 'none')}**"
                )

                _prog.progress(88, text="📄  Rendering Insider PDF…")
                st.write("📄  Rendering Insider PDF…")
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(
                    OUTPUTS_DIR
                    / f"{safe}_insider_{_period_months}m_{date}.pdf"
                )
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                InsiderTransactionsGenerator().render(
                    company=company,
                    insider_bundle=insider_bundle,
                    period_months=_period_months,
                    output_path=pdf_path,
                )
                analysis = {}
                extra = {
                    "insider_count": len(insider_bundle.get("transactions", [])),
                    "insider_source": insider_bundle.get("source_used", "none"),
                }

            elif report_type == "valuemeter":
                # ── ValueMeter — Prudent Value Score vs. peer group ───────────
                # Architecture:
                #   Call 1 (small): LLM identifies peer tickers.
                #   Python:         Fetches EODHD data for each peer, extracts metrics.
                #   Call 2 (main):  LLM receives REAL data, computes scores + interpretation.
                import importlib
                import models.valuemeter as _vm_mod
                import agents.pdf_valuemeter as _vmpdf_mod
                importlib.reload(_vm_mod)
                importlib.reload(_vmpdf_mod)
                from models.valuemeter import (
                    build_peer_id_prompt, extract_metrics,
                    build_scoring_prompt, SYSTEM_PROMPT as VM_SYS,
                )
                from agents.pdf_valuemeter import ValueMeterGenerator
                from data_sources.eodhd_only_builder import fetch_company_data_eodhd_only

                # ── Step 1: EODHD-only subject data ──────────────────────────
                _prog.progress(20, text="💎  Fetching subject data…")
                if _is_japan:
                    _vm_bundle = _JAPAN_BUNDLE
                    st.write(f"🇯🇵  Using yfinance data for Japanese stock: {company.name}")
                elif _is_baltic:
                    _vm_bundle = _BALTIC_BUNDLE
                    st.write(f"🇧🇦  Using yfinance data for Baltic stock: {company.name}")
                else:
                    st.write("💎  Fetching EODHD bundle for subject company…")
                    company, _vm_bundle = fetch_company_data_eodhd_only(ticker_input)
                    st.write(f"✓  Subject: {company.name}  ·  "
                             f"{_vm_bundle.get('endpoints_used', 0)}/9 EODHD endpoints")

                # ── Step 2: LLM Call 1 — identify peers ──────────────────────
                _prog.progress(30, text="🔍  Identifying peer group…")
                st.write("🔍  LLM Call 1: identifying peer group…")
                _pid_pfx, _pid_dyn = build_peer_id_prompt(company)
                _peer_id = llm.generate_json(
                    _pid_dyn, VM_SYS,
                    max_tokens=1000,
                    cacheable_prefix=_pid_pfx,
                )
                _peer_tickers = [
                    p["ticker"].strip().upper()
                    for p in (_peer_id.get("peers") or [])
                    if p.get("ticker")
                ]
                _peer_reasons = {
                    p["ticker"].strip().upper(): p.get("reason", "")
                    for p in (_peer_id.get("peers") or [])
                    if p.get("ticker")
                }
                _excluded_peers = _peer_id.get("excluded") or []
                st.write(f"✓  {len(_peer_tickers)} peers identified: "
                         f"{', '.join(_peer_tickers)}")
                _prog.progress(38, text=f"✓  {len(_peer_tickers)} peers identified")

                # ── Step 3: Fetch EODHD data for each peer ────────────────────
                _prog.progress(40, text="📡  Fetching EODHD peer data…")
                st.write(f"📡  Fetching EODHD data for {len(_peer_tickers)} peers…")
                _peer_data: dict[str, CompanyData] = {}
                for _pt in _peer_tickers[:8]:
                    try:
                        _pd, _ = fetch_company_data_eodhd_only(_pt)
                        if _pd.name or _pd.market_cap:
                            _peer_data[_pt] = _pd
                            st.write(f"   ✓ {_pt}: {_pd.name}")
                        else:
                            st.write(f"   ⚠ {_pt}: no EODHD data — skipped")
                    except Exception as _pe:
                        st.write(f"   ⚠ {_pt}: fetch error ({_pe}) — skipped")
                _prog.progress(58, text=f"✓  {len(_peer_data)} peers loaded from EODHD")
                st.write(f"✓  {len(_peer_data)} peers with EODHD data")

                # ── Step 4: Extract metrics for all companies ─────────────────
                _subj_metrics = extract_metrics(
                    company, is_subject=True,
                    reason="Subject company under analysis",
                )
                _all_metrics = [_subj_metrics] + [
                    extract_metrics(_pd, is_subject=False,
                                    reason=_peer_reasons.get(_pt, ""))
                    for _pt, _pd in _peer_data.items()
                ]

                # ── Step 5: LLM Call 2 — scoring + interpretation ─────────────
                _prog.progress(62, text="🤖  Running ValueMeter scoring — typically 30–60 s…")
                st.write(
                    f"🤖  LLM Call 2: scoring {len(_all_metrics)} companies "
                    f"with real EODHD data ({LLM_PROVIDER}/{LLM_MODEL})…"
                )
                _score_pfx, _score_dyn = build_scoring_prompt(_all_metrics)
                _score_result = llm.generate_json(
                    _score_dyn, VM_SYS,
                    max_tokens=4000,
                    cacheable_prefix=_score_pfx,
                )
                _show_token_usage(llm.last_usage)

                # ── Merge into final analysis dict ────────────────────────────
                # peer_group = EODHD metrics + reasons from LLM Call 1
                _pg_lookup = {m["ticker"]: m for m in _all_metrics}
                analysis = {
                    "peer_group":    _all_metrics,
                    "excluded_peers": _excluded_peers,
                    "scores":        _score_result.get("scores") or [],
                    "subject_ticker": ticker_input,
                    "interpretation": _score_result.get("interpretation") or {},
                    "conclusion":     _score_result.get("conclusion") or {},
                }

                # Validate
                if not analysis["scores"]:
                    st.warning("⚠ ValueMeter scoring returned no scores — PDF may be sparse.")

                n_peers  = len(_peer_data)
                n_scores = len(analysis["scores"])
                rating   = (analysis["conclusion"] or {}).get("rating", "n/a")
                verdict  = (analysis["interpretation"] or {}).get("verdict", "n/a")
                st.write(
                    f"✓  {n_peers} peers · {n_scores} scored · "
                    f"Rating: **{rating}** · Verdict: **{verdict}**"
                )
                _prog.progress(85, text="✓  Scoring complete")

                # ── Step 6: Render PDF ─────────────────────────────────────────
                _prog.progress(88, text="📄  Rendering ValueMeter PDF…")
                st.write("📄  Rendering ValueMeter PDF…")
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_valuemeter_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                ValueMeterGenerator().render(company, analysis, pdf_path)

                extra = {"n_peers": n_peers, "verdict": verdict, "rating": rating}
                _vm_rec_map = {
                    "Attractive": "BUY", "Neutral": "HOLD", "Unattractive": "SELL",
                }
                analysis["recommendation"] = _vm_rec_map.get(rating, "HOLD")

            elif report_type == "short_interest":
                # ── Short Interest report ──────────────────────────────────────
                # Snapshot (current short %, shares short, days to cover) comes
                # from the company object already fetched via DataManager/EODHD.
                # Historical bi-monthly data is fetched HERE directly from the
                # EODHD /shorts/ endpoint — bypasses cache/merge pipeline so it
                # always returns the latest records.
                import importlib
                import agents.pdf_short_interest as _si_mod
                importlib.reload(_si_mod)
                from agents.pdf_short_interest import ShortInterestGenerator

                # ── Fetch /shorts/ historical data directly ────────────────────
                # ── Historical short interest: FINRA OAuth (US) ───────────────
                # FINRA is the SEC-mandated source for US equity short interest
                # (bi-monthly). Access requires a free FINRA developer account:
                #   1. Register at https://developer.finra.org/
                #   2. Create a "Technical Application" → get Client ID + Secret
                #   3. Add FINRA_CLIENT_ID + FINRA_CLIENT_SECRET to Streamlit secrets
                # Non-US tickers have no equivalent free historical source.
                _prog.progress(60, text="📊  Fetching short interest history from FINRA…")
                st.write("📊  Fetching short interest history from FINRA…")

                import requests as _req
                import json    as _json
                import base64  as _b64

                _si_ticker    = ticker_input.upper()
                _is_us_ticker = "." not in _si_ticker   # bare ticker = US
                _si_history   = []

                # Read FINRA credentials: try os.environ first, then st.secrets
                # directly (in case the app.py whitelist injection hasn't fired yet).
                def _get_secret(key: str) -> str:
                    val = os.environ.get(key, "")
                    if not val:
                        try:
                            val = str(st.secrets.get(key, "") or "")
                        except Exception:
                            pass
                    return val

                _finra_id  = _get_secret("FINRA_CLIENT_ID")
                _finra_sec = _get_secret("FINRA_CLIENT_SECRET")

                # Diagnostic: show all st.secrets keys so we can verify what landed
                try:
                    _sk = [k for k in st.secrets.keys()]
                    print(f"[SI-DIAG] st.secrets keys: {_sk}", flush=True)
                except Exception as _ske:
                    print(f"[SI-DIAG] st.secrets unavailable: {_ske}", flush=True)

                print(f"[SI-DIAG] ticker={_si_ticker} is_us={_is_us_ticker} "
                      f"finra_id_len={len(_finra_id)} finra_sec_len={len(_finra_sec)}", flush=True)

                # ── Historical short interest: NASDAQ public API (US) ─────────
                # FINRA API only covers OTC; exchange-listed stocks (AAPL/NASDAQ)
                # need a different source. NASDAQ's public website API returns
                # bi-monthly short interest JSON — same data their site displays,
                # no authentication required.
                if _is_us_ticker:
                    _nasdaq_url = (
                        f"https://api.nasdaq.com/api/quote/{_si_ticker}"
                        f"/short-interest?type=SHORT_INTEREST&limit=16"
                    )
                    print(f"[SI-DIAG] NASDAQ API {_nasdaq_url}", flush=True)
                    try:
                        _nq_resp = _req.get(
                            _nasdaq_url,
                            headers={
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0.0.0 Safari/537.36"
                                ),
                                "Accept":          "application/json, text/plain, */*",
                                "Accept-Language": "en-US,en;q=0.9",
                                "Referer":         "https://www.nasdaq.com/",
                                "Origin":          "https://www.nasdaq.com",
                            },
                            timeout=20,
                        )
                        print(f"[SI-DIAG] NASDAQ HTTP {_nq_resp.status_code}: "
                              f"{_nq_resp.text[:300]}", flush=True)

                        if _nq_resp.status_code == 200:
                            _nq = _nq_resp.json()
                            # NASDAQ API response: data.shortInterestTable.rows
                            _rows = (
                                (_nq.get("data") or {})
                                   .get("shortInterestTable", {})
                                   .get("rows", [])
                                or []
                            )
                            print(f"[SI-DIAG] NASDAQ rows={len(_rows)} "
                                  f"first={_rows[0] if _rows else 'none'}", flush=True)

                            _float_m = company.shares_float
                            if not _float_m or _float_m <= 0:
                                if (company.shares_short and company.shares_short > 0
                                        and company.short_percent_of_float
                                        and company.short_percent_of_float > 0):
                                    _float_m = (company.shares_short
                                                / company.short_percent_of_float)

                            for _row in _rows:
                                _d_raw = (_row.get("settlementDate")
                                          or _row.get("date") or _row.get("Date") or "")
                                # NASDAQ date format: "05/15/2026" → convert to "2026-05-15"
                                if "/" in _d_raw:
                                    _dp = _d_raw.split("/")
                                    if len(_dp) == 3:
                                        _d = f"{_dp[2]}-{_dp[0].zfill(2)}-{_dp[1].zfill(2)}"
                                    else:
                                        _d = _d_raw
                                else:
                                    _d = _d_raw[:10]
                                # NASDAQ field name is "interest", not "shortInterest"
                                _raw_s = (
                                    _row.get("interest")
                                    or _row.get("shortInterest")
                                    or _row.get("sharesShort")
                                    or ""
                                )
                                if not _d or not _raw_s:
                                    continue
                                try:
                                    _sm = float(str(_raw_s).replace(",", "")) / 1_000_000
                                    _pf = (_sm / _float_m
                                           if _float_m and _float_m > 0 else None)
                                    _si_history.append({
                                        "date":            _d,
                                        "shares_short_m":  round(_sm, 3),
                                        "short_pct_float": round(_pf, 5) if _pf else None,
                                    })
                                except (ValueError, TypeError):
                                    continue

                            if _si_history:
                                _si_history.sort(key=lambda x: x["date"], reverse=True)
                                print(f"[SI-DIAG] Parsed {len(_si_history)} NASDAQ records. "
                                      f"Sample: {_si_history[0]}", flush=True)
                                st.write(f"✓  {len(_si_history)} records from NASDAQ")
                            else:
                                print(f"[SI-DIAG] 0 records parsed", flush=True)
                        else:
                            print(f"[SI-DIAG] NASDAQ non-200", flush=True)
                    except Exception as _nqe:
                        print(f"[SI-DIAG] NASDAQ exception: {type(_nqe).__name__}: {_nqe}",
                              flush=True)

                else:
                    print(f"[SI-DIAG] Non-US ticker — no historical short data", flush=True)

                # Inject history into company object for PDF renderer
                company.short_interest_history = _si_history

                _prog.progress(85, text="📄  Rendering Short Interest PDF…")
                st.write("📄  Rendering Short Interest PDF…")

                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_short_interest_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                ShortInterestGenerator().render(company, pdf_path)

                analysis = {}
                extra    = {}

            elif report_type == "fund_fundamentals":
                # ── Fund Fundamentals — ETF & Mutual Fund EODHD + LLM factsheet
                _prog.progress(25, text="🏦  Fetching EODHD Fund data…")
                st.write("🏦  Fetching EODHD Fund data…")
                import importlib
                import data_sources.fund_fetcher as _ff_mod
                import agents.pdf_fund_fundamentals as _pff_mod
                import models.fund_analysis as _fa_mod
                importlib.reload(_ff_mod)
                importlib.reload(_pff_mod)
                importlib.reload(_fa_mod)
                from data_sources.fund_fetcher import FundFetcher
                from agents.pdf_fund_fundamentals import FundFundamentalsPDFGenerator
                from models.fund_analysis import build_fund_prompt

                fund_bundle = FundFetcher().fetch(ticker_input)
                fund_type   = fund_bundle.get("fund_type", "UNKNOWN")
                _prog.progress(55, text=f"✓  {fund_bundle['endpoints_used']} endpoints OK — type: {fund_type}")
                st.write(
                    f"✓  {fund_bundle['endpoints_used']} endpoints OK · Fund type: {fund_type}"
                    + (f" — errors: {', '.join(fund_bundle['errors'])}" if fund_bundle["errors"] else "")
                )

                # ── LLM factsheet commentary ──────────────────────────────────
                _prog.progress(60, text="🤖  Generating factsheet commentary…")
                st.write("🤖  Generating factsheet commentary (LLM)…")
                _fund_analysis = {}
                try:
                    cacheable_pfx, dynamic_p = build_fund_prompt(fund_bundle)
                    _fund_analysis = llm.generate_json(
                        dynamic_p,
                        system_prompt=cacheable_pfx,
                        max_tokens=1200,
                    )
                except Exception as _fa_err:
                    st.warning(f"LLM factsheet skipped: {_fa_err}")

                _prog.progress(82, text="📄  Rendering Fund Fundamentals PDF…")
                st.write("📄  Rendering Fund Fundamentals PDF…")
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_fund_fundamentals_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                FundFundamentalsPDFGenerator().render(fund_bundle, pdf_path,
                                                      analysis=_fund_analysis)

                # Back-fill company scalars for preview bar
                def _to_float(v):
                    try:
                        if v is None or v == "" or v == "NA": return None
                        return float(v)
                    except (ValueError, TypeError): return None

                _ff_gen  = (fund_bundle.get("fundamentals") or {}).get("General")    or {}
                _ff_rt   = fund_bundle.get("realtime") or {}
                _ff_etf  = (fund_bundle.get("fundamentals") or {}).get("ETF_Data")   or {}
                _ff_mf   = (fund_bundle.get("fundamentals") or {}).get("MutualFund_Data") or {}

                if not company.current_price:
                    company.current_price = (
                        _to_float(_ff_rt.get("close"))
                        or _to_float(_ff_rt.get("previousClose"))
                    )
                if not company.currency_price:
                    company.currency_price = (
                        _ff_gen.get("CurrencyCode")
                        or _ff_rt.get("currency")
                        or company.currency or ""
                    )
                if not company.name:
                    company.name = _ff_gen.get("Name") or ticker_input
                if not company.market_cap:
                    tna = (_ff_etf.get("Total_Net_Assets") or _ff_mf.get("Total_Net_Assets")
                           or _ff_mf.get("Net_Assets"))
                    company.market_cap = _to_float(tna)

                analysis = _fund_analysis
                extra    = {"fund_bundle": fund_bundle, "fund_type": fund_type}

            elif report_type == "earnings_quality":
                # ── Earnings Quality Score — forensic-accounting scoring ──────
                # Subject + peers must all be fetched BEFORE the single scoring
                # LLM call, since the score is comparative across the whole
                # universe in one shot (unlike overview_v2, where peers are
                # only fetched after the main LLM call for display purposes).
                from data_sources.eodhd_only_builder import (
                    fetch_company_data_eodhd_only,
                )
                from models.earnings_quality import (
                    _earnings_quality_prompt_parts, _validate_earnings_quality,
                    SYSTEM_PROMPT as SYS,
                )

                # Step 1: EODHD-only subject data (Japan/Baltic/empty-data fallback)
                _prog.progress(20, text="🧮  Fetching EODHD-only data…")
                if _is_japan:
                    st.write("🇯🇵  Using yfinance data for Japanese stock (EODHD not available)")
                elif _is_baltic:
                    st.write("🇧🇦  Using yfinance data for Baltic stock (EODHD may be incomplete)")
                else:
                    st.write("🧮  Fetching EODHD bundle (fundamentals + financial statements)…")
                    _eq_company, _eq_bundle = fetch_company_data_eodhd_only(ticker_input)
                    _eq_usable = bool(
                        _eq_company.name
                        and (_eq_company.market_cap or _eq_company.annual_financials)
                    )
                    if _eq_usable:
                        company = _eq_company
                        st.write(f"✓  EODHD endpoints used: {_eq_bundle.get('endpoints_used',0)}/9")
                    else:
                        st.write(
                            f"⚠  EODHD has no data for **{ticker_input}**. "
                            "Falling back to yfinance — report will be less detailed."
                        )

                # Step 2: Peers — user-selected peers fill slots first; LLM
                # suggestions backfill remaining slots up to 3 total. Kept
                # tighter than the other report types' 6-peer cap since every
                # extra peer here adds a full company's worth of forensic-
                # accounting scoring to the single combined LLM call.
                eq_peers: dict = {}
                _eq_suggest_usage: dict = {}
                _user_peer_tickers = [p.strip().upper() for p in peer_list if p.strip()]
                if len(_user_peer_tickers) > 3:
                    st.info(
                        f"ℹ️  Earnings Quality analyzes up to 3 peers — using the first 3 "
                        f"of the {len(_user_peer_tickers)} supplied."
                    )
                    _user_peer_tickers = _user_peer_tickers[:3]
                _slots_remaining = 3 - len(_user_peer_tickers)

                if _slots_remaining > 0:
                    _prog.progress(30, text="🤝  Asking LLM to suggest peers…")
                    if _user_peer_tickers:
                        st.write(
                            f"🤝  {len(_user_peer_tickers)} peer(s) supplied — "
                            f"asking LLM to fill up to {_slots_remaining} more…"
                        )
                    else:
                        st.write("🤝  No peers supplied — asking LLM for peer suggestions…")
                    try:
                        from models.fisher_peers import suggest_peers as _suggest_peers
                        _llm_suggested, _eq_suggest_usage = _suggest_peers(
                            company, max_peers=_slots_remaining,
                        )
                    except Exception as _se:
                        _llm_suggested = []
                        st.warning(f"Peer suggestion failed: {_se}")
                    if _eq_suggest_usage:
                        _show_token_usage(_eq_suggest_usage)
                    _user_set = set(_user_peer_tickers)
                    _llm_new = [t for t in _llm_suggested if t not in _user_set]
                    _peer_tickers_to_fetch = (_user_peer_tickers + _llm_new)[:3]
                    if _llm_new:
                        st.write(f"💡  LLM added peers: {', '.join(_llm_new)}")
                else:
                    _peer_tickers_to_fetch = _user_peer_tickers[:3]

                if _peer_tickers_to_fetch:
                    _prog.progress(40, text="🔍  Fetching peer data…")
                    st.write("🔍  Fetching peer data (EODHD or yfinance fallback)…")
                    for pt in _peer_tickers_to_fetch:
                        try:
                            pd_ = None
                            src_label = "unknown"
                            _is_balt_peer = any(pt.upper().endswith(s)
                                                for s in (".VS", ".TL", ".RG"))
                            if pt.endswith(".T") or _is_balt_peer:
                                pd_ = dm.get(pt, force_refresh=False)
                                src_label = "yfinance (TSE)" if pt.endswith(".T") else "yfinance (Baltic)"
                            else:
                                try:
                                    _eodhd_pd, _ = fetch_company_data_eodhd_only(pt)
                                    _la = _eodhd_pd.latest_annual() if _eodhd_pd else None
                                    if (_eodhd_pd and _eodhd_pd.name
                                            and (_eodhd_pd.market_cap or (_la and _la.revenue))):
                                        pd_ = _eodhd_pd
                                        src_label = "EODHD"
                                except Exception:
                                    pass
                                if pd_ is None:
                                    pd_ = dm.get(pt, force_refresh=False)
                                    src_label = "yfinance"
                            la_check = pd_.latest_annual() if pd_ else None
                            has_rev = bool(la_check and la_check.revenue)
                            if pd_ and pd_.name and (pd_.market_cap or has_rev):
                                eq_peers[pt] = pd_
                                st.write(f"   ✓ {pt}: {pd_.name} [{src_label}]")
                            else:
                                st.write(f"   ⚠ Peer {pt} returned no usable data — skipped")
                        except Exception as e:
                            st.write(f"   ⚠ Peer {pt} fetch failed: {e}")
                st.write(f"✓  {len(eq_peers)} peer(s) loaded: "
                         f"{', '.join(eq_peers.keys()) or 'none'}")
                _prog.progress(55, text=f"✓  {len(eq_peers)} peers")

                # Step 3: Build prompt (subject + peers combined) and run LLM
                cacheable_pfx, dynamic_prompt = _earnings_quality_prompt_parts(company, eq_peers)
                _prog.progress(60, text="🤖  Running forensic-accounting analysis…")
                st.write(f"🤖  Scoring {1 + len(eq_peers)} companies for earnings quality…")
                analysis = llm.generate_json(dynamic_prompt, SYS, max_tokens=16000,
                                             cacheable_prefix=cacheable_pfx)
                analysis = _validate_earnings_quality(analysis, company, eq_peers)
                score = analysis.get("subject_score", "?")
                grade = analysis.get("subject_grade", "?")
                pct   = analysis.get("subject_percentile", "?")
                st.write(f"✓  Earnings Quality Score: **{score}/100** "
                         f"(Grade {grade}, {pct}th percentile)")
                _show_token_usage(llm.last_usage)
                _prog.progress(85, text="✓  Analysis complete")

                _prog.progress(88, text="📄  Rendering PDF…")
                st.write("📄  Rendering PDF…")
                import importlib, agents.pdf_earnings_quality as _eqmod
                importlib.reload(_eqmod)
                from agents.pdf_earnings_quality import EarningsQualityPDFGenerator
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_earnings_quality_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                EarningsQualityPDFGenerator().render(company, analysis, pdf_path)
                extra = {"score": score, "grade": grade, "percentile": pct,
                         "peer_count": len(eq_peers)}

            elif report_type not in _BUILTIN_IDS:
                # ── User-created / custom framework ───────────────────────────
                from models.generic_runner import GenericRunner
                fw_config = FrameworkManager().get(report_type)
                if fw_config is None:
                    raise ValueError(f"Framework '{report_type}' not found.")

                _prog.progress(25, text=f"🤖  Running '{fw_config.name}' AI analysis — typically 30–90 s…")
                st.write(f"🤖  Running '{fw_config.name}' analysis (Claude)…")
                runner = GenericRunner()
                safe   = ticker_input.replace(".", "_").replace("-", "_")
                import re as _re
                fw_slug = _re.sub(r"[^a-z0-9]+", "_", fw_config.name.lower()).strip("_")[:20]
                date   = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_{fw_slug}_{date}.html")

                html_path = runner.run(
                    ticker_input, fw_config,
                    peer_tickers=peer_list or None,
                    force_refresh=force_refresh,
                    output_path=pdf_path,
                )
                _prog.progress(88, text="📄  Rendering report…")
                # Read HTML for inline display
                with open(html_path, "r", encoding="utf-8") as _f:
                    _html_content = _f.read()
                analysis = {}     # no structured analysis for custom frameworks yet
                extra    = {"html_content": _html_content}

            else:  # gravity
                # ── Gravity Taxers — EODHD-only data pipeline ─────────────────
                from data_sources.eodhd_only_builder import (
                    fetch_company_data_eodhd_only, fetch_peers_eodhd_only,
                )
                from data_sources.eodhd_macro import fetch_country_macro_block
                from models.gravity import (
                    _build_gravity_prompt, _gravity_prompt_parts,
                    _validate_analysis, SYSTEM_PROMPT as SYS,
                )

                # Step 1: EODHD-only company data
                _prog.progress(25, text="⚖️  Fetching EODHD-only Gravity data…")
                if _is_japan:
                    _gravity_bundle = _JAPAN_BUNDLE
                    st.write("🇯🇵  Using yfinance data for Japanese stock (EODHD not available)")
                elif _is_baltic:
                    _gravity_bundle = _BALTIC_BUNDLE
                    st.write("🇧🇦  Using yfinance data for Baltic stock (EODHD may be incomplete)")
                else:
                    st.write("⚖️  Fetching EODHD bundle (fundamentals + /eod + news + sentiment + insider)…")
                    company, _gravity_bundle = fetch_company_data_eodhd_only(ticker_input)
                    st.write(f"✓  EODHD endpoints used: {_gravity_bundle.get('endpoints_used',0)}/9")

                # Step 2: Peers
                gravity_peers: dict = {}
                if peer_list:
                    _prog.progress(45, text="🔍  Fetching EODHD peer data…")
                    st.write(f"🔍  Fetching {len(peer_list)} peer(s) from EODHD…")
                    gravity_peers = fetch_peers_eodhd_only(
                        [p.strip().upper() for p in peer_list if p.strip()][:6]
                    )
                    st.write(f"✓  {len(gravity_peers)} peer(s) loaded: "
                             f"{', '.join(gravity_peers.keys()) or 'none'}")

                # Step 3: Country macro
                _prog.progress(55, text="🌍  Fetching country macro from EODHD…")
                gravity_country_macro = fetch_country_macro_block(company.country)
                if gravity_country_macro:
                    st.write(f"✓  EODHD macro for {company.country} loaded")

                # Step 4: Build prompt + run LLM
                cacheable_pfx, dynamic_prompt = _gravity_prompt_parts(
                    company,
                    bundle=_gravity_bundle,
                    peers=gravity_peers,
                    country_macro_block=gravity_country_macro,
                )

                if adversarial_on:
                    full_prompt = cacheable_pfx + "\n\n" + dynamic_prompt
                    adv_result = _adv_engine.run(full_prompt, SYS, max_tokens=6000,
                                                  report_type="gravity")
                    analysis = _validate_analysis(adv_result.merged)
                    score = analysis.get("total_gravity_score", "?")
                    grade = analysis.get("gravity_grade", "?")
                    pp    = analysis.get("revenue_model", {}).get("pricing_power", "?")
                    st.write(f"✓  Merged Gravity Score: **{score}/50** (Grade {grade}) · "
                             f"Pricing Power: {pp}")
                    st.write(f"   Claude: {adv_result.primary_rec} / "
                             f"GPT-4o: {adv_result.secondary_rec}  ·  "
                             f"Contested: {len(adv_result.contested_fields)} fields")
                else:
                    analysis = llm.generate_json(dynamic_prompt, SYS, max_tokens=6000,
                                                 cacheable_prefix=cacheable_pfx)
                    analysis = _validate_analysis(analysis)
                    score = analysis.get("total_gravity_score", "?")
                    grade = analysis.get("gravity_grade", "?")
                    rec   = analysis.get("recommendation", "n/a")
                    pp    = analysis.get("revenue_model", {}).get("pricing_power", "?")
                    st.write(f"✓  Gravity Score: **{score}/50** (Grade {grade}) · "
                             f"Pricing Power: {pp} · Rec: **{rec}**")
                    _show_token_usage(llm.last_usage)
                _prog.progress(75, text="✓  Gravity analysis complete")

                _prog.progress(88, text="📄  Rendering PDF…")
                st.write("📄  Rendering PDF...")
                from agents.pdf_gravity import GravityPDFGenerator
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_gravity_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                GravityPDFGenerator().render(company, analysis, pdf_path,
                                             adv_result=adv_result)
                extra = {"score": score, "grade": grade}

            # ── Done ──────────────────────────────────────────────────────────
            _prog.progress(100, text="✅  Report ready!")
            status.update(
                label=f"✅  {rt['short']} report ready for **{company.name}**",
                state="complete",
                expanded=False,
            )

            # ── Collect token usage for cost display ─────────────────────────
            if adv_result is not None:
                _usage_claude = adv_result.claude_usage
                _usage_openai = adv_result.openai_usage
            elif report_type in ("eodhd_full", "insider_transactions", "short_interest"):
                _usage_claude = {}
                _usage_openai = None
            else:
                _usage_claude = llm.last_usage if hasattr(llm, "last_usage") else {}
                _usage_openai = None

            # Store result
            st.session_state.report_result = {
                "pdf_path":     pdf_path,
                "company":      company,
                "analysis":     analysis,
                "report_type":  report_type,
                "rec":          analysis.get("recommendation", "HOLD"),
                "extra":        extra,
                "adversarial":  adv_result,
                "usage_claude": _usage_claude,
                "usage_openai": _usage_openai,
            }

            # Add to recent reports
            label = f"{ticker_input} · {rt['short']} · {date}"
            st.session_state.recent_reports.append({
                "label": label,
                "path":  pdf_path,
                "ts":    datetime.now().timestamp(),
            })

        except Exception as e:
            st.session_state.error_msg = str(e)
            status.update(
                label=f"❌  Error generating report",
                state="error",
                expanded=True,
            )
            st.error(f"**Error:** {e}")
            logger.exception("Report generation failed")


# ── Results display ───────────────────────────────────────────────────────────
if st.session_state.report_result:
    res     = st.session_state.report_result
    company = res["company"]
    rec     = res["rec"]
    rtype   = res["report_type"]
    extra   = res["extra"]

    # ── Key metrics bar ───────────────────────────────────────────────────────
    if rtype == "index_overview":
        # Index metrics bar
        idx_data = res.get("index_data")
        if idx_data:
            col1, col2, col3, col4, col5, col6 = st.columns(6)
            col1.metric("Level",       f"{idx_data.current_level:,.2f}"        if idx_data.current_level       else "n/a")
            col2.metric("YTD",         f"{idx_data.return_ytd*100:+.1f}%"      if idx_data.return_ytd          else "n/a")
            col3.metric("1Y Return",   f"{idx_data.return_1y*100:+.1f}%"       if idx_data.return_1y           else "n/a")
            col4.metric("Volatility",  f"{idx_data.volatility_1y_ann*100:.1f}%" if idx_data.volatility_1y_ann  else "n/a")
            col5.metric("Wtd P/E",     f"{idx_data.weighted_pe:.1f}x"          if idx_data.weighted_pe         else "n/a")
            col6.metric("Div Yield",   f"{idx_data.dividend_yield*100:.2f}%"   if idx_data.dividend_yield      else "n/a")
            st.caption(
                f"**{idx_data.name or idx_data.ticker}**  ·  "
                f"{idx_data.index_type}  ·  "
                f"Currency: {idx_data.currency or 'n/a'}  ·  "
                f"As of: {idx_data.as_of_date or 'n/a'}  ·  "
                f"Rec: **{rec}**"
            )

    elif rtype.startswith("universe_"):
        # Universe screen — lightweight caption only
        fw_id    = rtype.removeprefix("universe_")
        fw_short = REPORT_TYPES.get(fw_id, {}).get("short", fw_id)
        st.caption(f"🔍 **Universe Screen** · Framework: **{fw_short}** · {Path(res['pdf_path']).name}")

    else:
        # ── Equity metrics bar ────────────────────────────────────────────────
        col1, col2, col3, col4, col5, col6 = st.columns(6)

        price_str  = (f"{company.current_price:.2f} {company.currency_price or ''}"
                      if company.current_price else "n/a")
        cap_str    = (_fmt_b(company.market_cap) + f" {company.currency or ''}"
                      if company.market_cap else "n/a")
        pe_str     = f"{company.pe_ratio:.1f}x" if company.pe_ratio else "n/a"
        roe_str    = f"{company.roe*100:.1f}%"  if company.roe else "n/a"
        margin_str = f"{company.ebit_margin*100:.1f}%" if company.ebit_margin else "n/a"

        col1.metric("Price",       price_str)
        col2.metric("Market Cap",  cap_str)
        col3.metric("P/E",         pe_str)
        col4.metric("ROE",         roe_str)
        col5.metric("EBIT Margin", margin_str)
        col6.metric("Rec.",        rec)

        # Adversarial badge
        adv = res.get("adversarial")
        if adv is not None:
            agree_icon = "✓" if adv.recs_agree else "⚠"
            st.markdown(
                f"<span style='background:#FFA028;color:#000000;padding:2px 8px;"
                f"border-radius:2px;font-size:12px;font-weight:700;font-family:monospace;'>⚔ Adversarial</span>  "
                f"Claude: **{adv.primary_rec}**  ·  GPT-4o: **{adv.secondary_rec}**  ·  "
                f"{agree_icon} {'Agree' if adv.recs_agree else 'Contested'}  ·  "
                f"Consensus: {len(adv.consensus_fields)} fields  ·  "
                f"Contested: {len(adv.contested_fields)} fields",
                unsafe_allow_html=True,
            )

        # Extra framework metrics
        if rtype == "overview_v2":
            passed = extra.get("passed", 0)
            total  = len(extra.get("checklist", []))
            st.caption(f"Checklist: **{passed}/{total}** criteria met  ·  "
                       f"Data: {company.year_range()}  ·  "
                       f"Sources: {', '.join(company.data_sources)}")
        elif rtype == "fisher":
            st.caption(f"Fisher Score: **{extra.get('score','?')}/75**  ·  "
                       f"Grade: **{extra.get('grade','?')}**  ·  "
                       f"Moat: {res['analysis'].get('moat_width','?')}  ·  "
                       f"Active Powers: {res['analysis'].get('active_powers_count','?')}/7")
        elif rtype == "fisher_peers":
            st.caption(
                f"Fisher Score: **{extra.get('score','?')}/75**  ·  "
                f"Grade: **{extra.get('grade','?')}**  ·  "
                f"Moat: {res['analysis'].get('moat_width','?')}  ·  "
                f"Peers analysed: **{extra.get('peer_count', 0)}**"
            )
        elif rtype == "industry_analysis":
            st.caption(
                f"Industry attractiveness: **{extra.get('attractiveness','?')}**  ·  "
                f"Trajectory: **{extra.get('trajectory','?')}**  ·  "
                f"Competitive advantage: **{extra.get('advantage','?')}**"
            )
        elif rtype == "gravity":
            rm = res["analysis"].get("revenue_model", {})
            st.caption(f"Gravity Score: **{extra.get('score','?')}/50**  ·  "
                       f"Grade: **{extra.get('grade','?')}**  ·  "
                       f"Recurring: ~{rm.get('recurring_pct_estimate','?')}%  ·  "
                       f"Pricing Power: {rm.get('pricing_power','?')}")
        elif rtype == "earnings_quality":
            st.caption(
                f"Earnings Quality Score: **{extra.get('score','?')}/100**  ·  "
                f"Grade: **{extra.get('grade','?')}**  ·  "
                f"Percentile: **{extra.get('percentile','?')}**  ·  "
                f"Peers analysed: **{extra.get('peer_count', 0)}**"
            )
        else:
            fw_label = REPORT_TYPES.get(rtype, {}).get("short", rtype)
            st.caption(f"Framework: **{fw_label}**  ·  "
                       f"Data: {company.year_range()}  ·  "
                       f"Sources: {', '.join(company.data_sources)}")

    # ── LLM cost summary (always shown for equity reports) ───────────────────
    # Prompt-interpretation usage lives in session_state (set by the NL
    # parser dispatch) — it persists across the rerun that follows
    # report generation, so we read it here and feed it into _cost_block.
    _prompt_u = (
        res.get("usage_prompt")
        or st.session_state.get("rg_prompt_usage")
        or None
    )
    if "usage_claude" in res or _prompt_u:
        _report_cost = _cost_block(
            res.get("usage_claude") or {},
            res.get("usage_openai"),
            _prompt_u,
        )
        if _report_cost and not res.get("_cost_counted"):
            try:
                from utils.cost_tracker import increment as _ct_inc
                _ct_inc(_report_cost)
                res["_cost_counted"] = True  # prevent double-counting on reruns
            except Exception:
                pass

    st.divider()

    # ── Report viewer + download ──────────────────────────────────────────────
    pdf_path = res["pdf_path"]
    is_html_report = pdf_path.endswith(".html")

    col_view, col_dl = st.columns([5, 1])

    # Build a sensible header label for the viewer
    if company:
        _viewer_label = f"**{company.name}** — {REPORT_TYPES.get(rtype, {}).get('label', rtype)}"
    elif rtype == "index_overview":
        _idx = res.get("index_data")
        _idx_name = _idx.name if _idx else res["pdf_path"].split("\\")[-1]
        _viewer_label = f"**{_idx_name}** — Index Overview"
    else:
        _viewer_label = f"**{Path(pdf_path).stem}** — Universe Screen"

    with col_view:
        st.markdown(_viewer_label)

    with col_dl:
        st.markdown("&nbsp;")
        with open(pdf_path, "rb") as f:
            report_bytes = f.read()
        mime = "text/html" if is_html_report else "application/pdf"
        label = "⬇ Download HTML" if is_html_report else "⬇ Download PDF"
        st.download_button(
            label=label,
            data=report_bytes,
            file_name=Path(pdf_path).name,
            mime=mime,
            use_container_width=True,
            type="primary",
        )
        st.caption(f"{len(report_bytes)//1024} KB")

elif st.session_state.error_msg:
    st.error(st.session_state.error_msg)

# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(
    "<div style='margin-top:32px;padding-top:8px;"
    "border-top:1px solid #E0E5EC;color:#888;font-size:12px;"
    "line-height:1.5;text-align:center;'>"
    "Enter a ticker, pick a framework, and generate a professional "
    "investment report. Reports use real financial data + LLM analysis "
    "calibrated to value investing principles."
    "</div>",
    unsafe_allow_html=True,
)
