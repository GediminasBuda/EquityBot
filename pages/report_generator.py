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

/* Report type cards */
.report-card {
    border: 1px solid #BBCCDD;
    border-radius: 8px;
    padding: 14px 16px;
    background: #EEF5FB;
    margin-bottom: 6px;
    cursor: pointer;
}
.report-card h4 { color: #1B3F6E; margin: 0 0 4px 0; font-size: 14px; }
.report-card p  { color: #555555; margin: 0; font-size: 12px; line-height: 1.4; }

/* Metric chips */
.metric-chip {
    display: inline-block;
    background: #D6E8F7;
    color: #1B3F6E;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 12px;
    font-weight: 600;
    margin: 2px;
}
.rec-buy  { background: #D4EDDA; color: #1A7E3D; }
.rec-hold { background: #FDEBD0; color: #D68910; }
.rec-sell { background: #FADBD8; color: #C0392B; }

/* Divider */
hr { margin: 12px 0; border-color: #BBCCDD; }
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
) -> None:
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
        return

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
        f"<div style='background:#F8FAFC;border:1px solid #D0DFF0;border-radius:6px;"
        f"padding:8px 14px;margin:6px 0;font-size:12px;color:#334155;line-height:1.8;'>"
        f"💰 <b>LLM cost this report</b><br>"
        f"{rows_html}"
        + (f"<br><span style='color:#1B3F6E'>{total_html}</span>" if total_html else "")
        + "</div>",
        unsafe_allow_html=True,
    )


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
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


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
      • Always also return ticker autocomplete suggestions from EODHD /search.

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

    # Ticker autocomplete (also helpful even when query is NL — they may
    # mention a real ticker that the EODHD search recognises).
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

    return suggestions[:12]


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
    return result

REPORT_TYPES = _build_report_types()

# Builtin framework ids (for runner dispatch logic)
_BUILTIN_IDS = {"fisher", "fisher_peers", "gravity",
                "eodhd_full", "overview_v2", "index_overview",
                "industry_analysis"}

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
.wf-tier   { font-size:11px; font-weight:700; color:#888; letter-spacing:.06em;
             text-transform:uppercase; margin:10px 0 2px 0; }
.wf-row    { display:flex; align-items:flex-start; gap:7px; margin:3px 0; }
.wf-badge  { flex-shrink:0; font-size:10px; font-weight:700; padding:1px 6px;
             border-radius:3px; margin-top:1px; }
.wf-paid   { background:#1B3F6E; color:#fff; }
.wf-free   { background:#D6E8F7; color:#1B3F6E; }
.wf-ctx    { background:#EEF5FB; color:#555; }
.wf-body   { font-size:12px; line-height:1.4; color:#333; }
.wf-body b { color:#111; }
.wf-arrow  { color:#BBCCDD; font-size:13px; margin:1px 0 1px 10px; }
.wf-miss   { font-size:11px; color:#888; margin:4px 0 0 0; }
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
    "  background: #FFFFFF;"
    "  padding: 4px 0 6px 0;"
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
    "<span style='font-size:16px;font-weight:600;color:#1B3F6E;'>"
    "Report Generator</span></div>",
    unsafe_allow_html=True,
)

# ── Input form ────────────────────────────────────────────────────────────────
col_left, col_right = st.columns([1.4, 1], gap="large")

with col_left:
    st.markdown("#### Ticker  *or describe what you want in plain language*")

    # ── Smart searchbar (autocomplete + NL prompt) ────────────────────────────
    selected = st_searchbox(
        search_function=_smart_search,
        placeholder=(
            "🔍 e.g.  AAPL  ·  Rheinmetall  ·  "
            "'top 10 SP500 by market cap'  ·  'Fisher on RHM.DE'"
        ),
        label=None,
        clear_on_submit=False,
        key="rg_searchbox",
    )

    # ── Mobile-only Peer Tickers (CSS hides this block on desktop) ────────
    # The user wants Peer Tickers to appear right after the ticker input on
    # phones. Render a parallel input here with a distinct key, then merge
    # its value with the canonical col_right input further down.
    st.markdown("<div class='rg-peer-mobile-anchor'></div>",
                unsafe_allow_html=True)
    st.markdown("#### Peer Tickers  *(optional)*")
    st.text_input(
        "Peer tickers (mobile)",
        placeholder="REL.L  TRI.TO  MSFT  (space-separated, up to 6)",
        label_visibility="collapsed",
        key="peers_input_mobile",
    )

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
        _universe = _intent_for_render.get("universe") or "—"
        _sort_by  = _intent_for_render.get("sort_by") or "market_cap"
        _sort_dir = _intent_for_render.get("sort_dir") or "desc"
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
        _fw_options = [k for k in REPORT_TYPES if k != "index_overview"]
    else:
        _fw_options = [k for k in REPORT_TYPES if k != "index_overview"]

    st.markdown(
        "#### Report Framework"
        if not (_is_index_ticker and index_mode == "index_overview")
        else "#### Framework  *(auto-selected)*"
    )

    # ── Reorder UI (collapsed by default) ─────────────────────────────────────
    # Only shown when the user is in normal equity / screening mode (where
    # they actually pick from the list). Hidden when index_overview locks
    # the selection automatically.
    if not (_is_index_ticker and index_mode == "index_overview"):
        with st.expander("↕ Reorder framework list", expanded=False):
            st.caption(
                "Use the arrows to move a framework up or down. The order "
                "is saved automatically and applies everywhere."
            )
            fm_reorder = FrameworkManager()
            # Show ALL frameworks (excluding index_overview which is implicit)
            reorder_ids = [k for k in REPORT_TYPES if k != "index_overview"]
            for i, fid in enumerate(reorder_ids):
                row_left, row_up, row_down = st.columns([8, 1, 1])
                with row_left:
                    st.markdown(
                        f"<div style='padding-top:6px;'>"
                        f"{REPORT_TYPES[fid]['label']}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with row_up:
                    if st.button("↑", key=f"order_up_{fid}",
                                 disabled=(i == 0),
                                 use_container_width=True):
                        fm_reorder.move(fid, -1)
                        st.rerun()
                with row_down:
                    if st.button("↓", key=f"order_down_{fid}",
                                 disabled=(i == len(reorder_ids) - 1),
                                 use_container_width=True):
                        fm_reorder.move(fid, +1)
                        st.rerun()

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
    builtin_badge = "" if rt.get("is_builtin", True) else "  ·  *Custom*"
    st.caption(f"*{rt['desc']}*  ·  {rt['pages']}{builtin_badge}")

with col_right:
    # Peer tickers — only relevant for Overview
    # Anchor lets CSS hide the desktop copy on mobile (the mobile copy
    # lives in col_left right after the ticker searchbox).
    st.markdown("<div class='rg-peer-desktop-anchor'></div>",
                unsafe_allow_html=True)
    st.markdown("#### Peer Tickers  *(optional)*")
    peers_input = st.text_input(
        "Peer tickers",
        placeholder="REL.L  TRI.TO  MSFT  (space-separated, up to 6)",
        label_visibility="collapsed",
        disabled=(report_type not in (
            "overview_v2", "fisher", "fisher_peers", "gravity",
        )),
        key="peers_input",
    )
    # Merge whichever copy actually carries a user-entered value so the
    # rest of the page consumes a single peers_input string.
    peers_input = (
        st.session_state.get("peers_input_mobile", "").strip()
        or peers_input
        or ""
    )

    st.markdown("#### Options")
    force_refresh = st.checkbox(
        "Force refresh data cache",
        value=False,
        help="Bypass the 24-hour cache and re-fetch all data from source.",
    )

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

st.divider()


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

                from models.universe_screener import UniverseScreener
                _safe_uni = _b_universe.replace("^", "").replace(".", "_") or "custom"
                _safe_fw  = _b_framework.replace("_", "-")
                _date     = datetime.now().strftime("%Y-%m-%d")
                _pdf_path = str(
                    OUTPUTS_DIR /
                    f"{_safe_uni}_{_safe_fw}_universe_{_date}.html"
                )
                _screener  = UniverseScreener()
                _html_path = _screener.run(
                    index_ticker=_b_universe,
                    framework_id=_b_framework,
                    output_path=_pdf_path,
                    tickers=_b_tickers,
                    progress_cb=_bulk_progress,
                )
                _bprog.progress(100, text="✅  Report ready!")
                with open(_html_path, "r", encoding="utf-8") as _f:
                    _html_content = _f.read()

                _bulk_status.update(
                    label=f"✅  {_b_fw_short} Universe Screen complete "
                          f"({len(_b_tickers)} companies)",
                    state="complete", expanded=False,
                )
                # Capture LLM usage so the Result viewer can render the
                # cost block. Universe screens use Claude under the hood
                # (no OpenAI adversarial path) — so usage_openai stays None.
                _b_usage = getattr(_screener, "last_usage", {}) or {}
                _show_token_usage(_b_usage)
                st.session_state.report_result = {
                    "pdf_path":    _html_path,
                    "company":     None,
                    "index_data":  None,
                    "analysis":    {},
                    "report_type": f"universe_{_b_framework}",
                    "rec":         "n/a",
                    "extra":       {"html_content": _html_content},
                    "adversarial": None,
                    "usage_claude": _b_usage,
                    "usage_openai": None,
                }
                _bulk_label_chip = f"{_b_label} · {_b_fw_short} · {_date}"
                st.session_state.recent_reports.append({
                    "label": _bulk_label_chip,
                    "path":  _html_path,
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

                from models.universe_screener import UniverseScreener
                safe_idx = ticker_input.replace("^", "").replace(".", "_")
                safe_fw  = report_type.replace("_", "-")
                date     = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe_idx}_{safe_fw}_universe_{date}.html")

                screener  = UniverseScreener()
                html_path = screener.run(
                    index_ticker=ticker_input,
                    framework_id=report_type,
                    output_path=pdf_path,
                    force_refresh=force_refresh,
                    progress_cb=_universe_progress,
                )
                _prog.progress(100, text="✅  Report ready!")
                with open(html_path, "r", encoding="utf-8") as _f:
                    _html_content = _f.read()

                status.update(
                    label=f"✅  {rt['short']} Universe Screen complete for {ticker_input}",
                    state="complete", expanded=False,
                )
                # Capture LLM usage for the cost block
                _uni_usage = getattr(screener, "last_usage", {}) or {}
                _show_token_usage(_uni_usage)
                st.session_state.report_result = {
                    "pdf_path":    html_path,
                    "company":     None,
                    "index_data":  None,
                    "analysis":    {},
                    "report_type": f"universe_{report_type}",
                    "rec":         "n/a",
                    "extra":       {"html_content": _html_content},
                    "adversarial": None,
                    "usage_claude": _uni_usage,
                    "usage_openai": None,
                }
                label = f"{ticker_input} · {rt['short']} Screen · {date}"
                st.session_state.recent_reports.append({
                    "label": label, "path": html_path,
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
                st.write("💎  Fetching EODHD bundle (fundamentals + /eod)…")
                company, _v2_bundle = fetch_company_data_eodhd_only(ticker_input)
                st.write(f"✓  EODHD endpoints used: {_v2_bundle.get('endpoints_used',0)}/9")

                # Build the LLM prompt with EODHD context only — no news,
                # no macro blocks (they would be sourced outside EODHD).
                cacheable_pfx, dynamic_prompt = _overview_prompt_parts(
                    company, news_block="", macro_country_block="",
                )
                _prog.progress(35, text="🤖  Running LLM (EODHD-only context)…")
                st.write("🤖  Running LLM on EODHD-only context…")
                analysis = llm.generate_json(dynamic_prompt, SYS,
                                             max_tokens=5000,
                                             cacheable_prefix=cacheable_pfx)
                rec = analysis.get("recommendation", "n/a")
                st.write(f"✓  Recommendation: **{rec}**")
                _show_token_usage(llm.last_usage)
                _prog.progress(65, text="✓  AI analysis complete")

                # Peers — fetch each EODHD-only too
                _prog.progress(68, text="🔍  Fetching EODHD peer data…")
                st.write("🔍  Fetching EODHD-only peer data…")
                peers: dict[str, CompanyData] = {}
                raw_peers = peer_list or [
                    p.get("ticker", "")
                    for p in analysis.get("suggested_peers", [])
                ]
                raw_peers = [t.strip().upper() for t in raw_peers if t.strip()][:6]
                for pt in raw_peers:
                    try:
                        pd_, _ = fetch_company_data_eodhd_only(pt)
                        # Only keep peers EODHD actually returned data for —
                        # market_cap is the cheapest "real data" signal.
                        # Drop hallucinated tickers that yield empty payloads.
                        la_check = pd_.latest_annual()
                        has_rev = bool(la_check and la_check.revenue)
                        if pd_.name and (pd_.market_cap or has_rev):
                            peers[pt] = pd_
                        else:
                            st.write(f"   ⚠ Peer {pt} returned no usable EODHD data — skipped")
                    except Exception as e:
                        st.write(f"   ⚠ Peer {pt} fetch failed: {e}")
                st.write(f"✓  {len(peers)} EODHD peers loaded: "
                         f"{', '.join(peers.keys()) or 'none'}")
                _prog.progress(78, text=f"✓  {len(peers)} peers")

                checklist = _calculate_checklist(company)
                passed = sum(1 for c in checklist if c["pass"])
                st.write(f"✓  Checklist: {passed}/{len(checklist)} criteria met")
                _prog.progress(84)

                _prog.progress(88, text="📄  Rendering V2 PDF…")
                st.write("📄  Rendering V2 PDF…")
                import importlib, agents.pdf_overview_v2 as _v2mod
                importlib.reload(_v2mod)
                from agents.pdf_overview_v2 import OverviewV2PDFGenerator
                # Wire the EODHD /eod data into the price chart by stuffing
                # it onto company so pdf_overview_v2 can pick it up.
                setattr(company, "_eod_data_v2", _v2_bundle.get("eod") or [])
                safe = ticker_input.replace(".", "_").replace("-", "_")
                date = datetime.now().strftime("%Y-%m-%d")
                pdf_path = str(OUTPUTS_DIR / f"{safe}_overview_v2_{date}.pdf")
                os.makedirs(OUTPUTS_DIR, exist_ok=True)
                OverviewV2PDFGenerator().render(company, analysis, peers,
                                                 checklist, pdf_path)
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
                st.write("🔬  Fetching EODHD bundle (fundamentals + news + insider)…")
                company, _fpr_bundle = fetch_company_data_eodhd_only(ticker_input)
                st.write(f"✓  EODHD endpoints used: {_fpr_bundle.get('endpoints_used',0)}/9")

                # Step 2: Peers — required for this framework. If the user
                # didn't supply any, ask the LLM to suggest some so the
                # peer page always has content.
                fpr_peers: dict = {}
                _fpr_suggest_usage: dict = {}
                if peer_list:
                    _peer_tickers_to_fetch = [
                        p.strip().upper() for p in peer_list if p.strip()
                    ][:6]
                    _prog.progress(35, text="🔍  Fetching EODHD peer data…")
                    st.write(f"🔍  Fetching {len(_peer_tickers_to_fetch)} peer(s) from EODHD…")
                else:
                    _prog.progress(30, text="🤝  Asking LLM to suggest peers…")
                    st.write("🤝  No peers supplied — asking LLM for peer suggestions…")
                    try:
                        from models.fisher_peers import suggest_peers as _suggest_peers
                        _peer_tickers_to_fetch, _fpr_suggest_usage = _suggest_peers(
                            company, max_peers=6,
                        )
                    except Exception as _se:
                        _peer_tickers_to_fetch = []
                        st.warning(f"Peer suggestion failed: {_se}")
                    if _fpr_suggest_usage:
                        _show_token_usage(_fpr_suggest_usage)
                    if _peer_tickers_to_fetch:
                        st.write(
                            f"💡  LLM suggested peers: "
                            f"{', '.join(_peer_tickers_to_fetch)}"
                        )
                    else:
                        st.warning(
                            "⚠ LLM could not suggest peers automatically. "
                            "The peer comparison page will be empty — try "
                            "adding peers manually in the **Peer Tickers** "
                            "field."
                        )

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

                # Step 1: EODHD-only subject data
                _prog.progress(20, text="🏛️  Fetching EODHD-only subject data…")
                st.write("🏛️  Fetching EODHD bundle (10y financials + news + sentiment)…")
                company, _ia_bundle = fetch_company_data_eodhd_only(ticker_input)
                st.write(f"✓  EODHD endpoints used: {_ia_bundle.get('endpoints_used',0)}/9")

                # Step 2: Country macro
                _prog.progress(40, text="🌍  Fetching country macro from EODHD…")
                ia_country_macro = fetch_country_macro_block(company.country)

                # Step 3: Main LLM call — 2,000-3,000 word Porter analysis
                cacheable_pfx, dynamic_prompt = _industry_prompt_parts(
                    company,
                    bundle=_ia_bundle,
                    country_macro_block=ia_country_macro,
                )

                _prog.progress(55, text="🧠  Running Porter 5 Forces analysis…")
                st.write("🧠  Running Porter 5 Forces + Competitive Advantage "
                         "analysis (~2,000-3,000 words; typically 45-90 s)…")

                if adversarial_on:
                    full_prompt = cacheable_pfx + "\n\n" + dynamic_prompt
                    adv_result = _adv_engine.run(
                        full_prompt, IA_SYS, max_tokens=8000,
                        report_type="overview",  # adversarial reuses overview merger
                    )
                    raw_analysis = adv_result.merged
                else:
                    raw_analysis = llm.generate_json(
                        dynamic_prompt, IA_SYS, max_tokens=8000,
                        cacheable_prefix=cacheable_pfx,
                    )
                    _show_token_usage(llm.last_usage)

                # ── Diagnostics: detect empty / mostly-empty responses ──
                _ia_raw_for_debug = getattr(llm, "last_raw_response", "") or ""
                _ia_top_keys = list(raw_analysis.keys()) if isinstance(raw_analysis, dict) else []
                _ia_filled = (
                    isinstance(raw_analysis, dict) and
                    isinstance(raw_analysis.get("forces"), list) and
                    len([f for f in raw_analysis["forces"]
                         if isinstance(f, dict) and f.get("state_2026")
                         and len(f.get("state_2026", "")) > 80]) >= 3
                )
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

                _prog.progress(90, text="📄  Rendering Industry Analysis PDF…")
                st.write("📄  Rendering Industry Analysis PDF…")
                import importlib, agents.pdf_industry_analysis as _iamod
                importlib.reload(_iamod)
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
            elif report_type == "eodhd_full":
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
                f"<span style='background:#1B3F6E;color:white;padding:2px 8px;"
                f"border-radius:4px;font-size:12px;font-weight:600;'>⚔ Adversarial</span>  "
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
        _cost_block(
            res.get("usage_claude") or {},
            res.get("usage_openai"),
            _prompt_u,
        )

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
