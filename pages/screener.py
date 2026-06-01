"""
screener.py — Stock Screener page for EquityBot.

NL prompt → LLM interprets → EODHD Screener API → results table with
checkboxes → Run any framework on selected companies.

Supports Lithuanian + English + mixed queries.
"""
from __future__ import annotations
import sys
import os
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.auth import require_auth
require_auth()

from agents.llm_client import LLMClient
from config import LLM_PROVIDER, LLM_MODEL

# ── Helper functions ──────────────────────────────────────────────────────────

_EODHD_TO_YF_SUFFIX: dict[str, str] = {
    "XETRA": ".DE", "F": ".F", "LSE": ".L",
    "PA": ".PA", "AS": ".AS", "BR": ".BR",
    "MI": ".MI", "MC": ".MC", "HE": ".HE",
    "ST": ".ST", "OL": ".OL", "CO": ".CO",
    "SW": ".SW", "VI": ".VI", "WAR": ".WA",
    "BUD": ".BD", "PR": ".PR", "TO": ".TO",
    "AU": ".AX", "TSE": ".T", "KO": ".KS",
    "HK": ".HK", "SHG": ".SS", "SHE": ".SZ",
    "NSE": ".NS", "BSE": ".BO", "SA": ".SA",
    "MX": ".MX", "JSE": ".JO", "IS": ".IS",
    "TLV": ".TA",
    "NYSE": "", "NASDAQ": "", "AMEX": "",
}


def _to_yf(code: str, exchange: str) -> str:
    suffix = _EODHD_TO_YF_SUFFIX.get(exchange.upper(), "")
    return f"{code}{suffix}" if suffix else code


def _fmt_mcap(v) -> str:
    if v is None: return "—"
    try:
        f = float(v)
        if f >= 1e12: return f"{f/1e12:.2f}T"
        if f >= 1e9:  return f"{f/1e9:.2f}B"
        if f >= 1e6:  return f"{f/1e6:.1f}M"
        return f"{f:,.0f}"
    except: return "—"


def _fmt_num(v, dec: int = 2) -> str:
    if v is None: return "—"
    try:   return f"{float(v):,.{dec}f}"
    except: return "—"


def _fmt_pct(v) -> str:
    if v is None: return "—"
    try:   return f"{float(v)*100:.2f}%"
    except: return "—"


# ── Bloomberg terminal CSS ────────────────────────────────────────────────────
st.markdown("""
<style>
.block-container { padding-top: 1.2rem; }
em, i { font-style: normal !important; }

/* Input fields */
input, textarea, .stTextInput input {
    background: #000000 !important;
    color: #FF3030 !important;
    border: 1px solid #FF3030 !important;
    font-family: monospace !important;
    caret-color: #FF3030;
}
::placeholder { color: #5a1010 !important; opacity: 1 !important; }

/* Tables */
.scr-header { color: #8a6a30; font-size: 11px; font-family: monospace; }
.scr-ticker { color: #FFA028; font-weight: 700; font-family: monospace; }
.scr-name   { color: #e8c070; font-family: monospace; font-size: 12px; }
.scr-val    { color: #FFA028; font-family: monospace; text-align: right; }
.scr-muted  { color: #5a4a25; font-family: monospace; font-size: 11px; }

/* Metric cards */
div[data-testid="metric-container"] {
    background: #0a0a0a;
    border: 1px solid #2a1f10;
    border-radius: 2px;
    padding: 6px 10px;
}
div[data-testid="metric-container"] label {
    color: #8a6a30 !important;
    font-family: monospace;
    font-size: 11px;
}
div[data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: #FFA028 !important;
    font-family: monospace;
}

/* Buttons */
button[kind="primary"] {
    background: #0a1f10 !important;
    border: 1px solid #4D9FFF !important;
    color: #4D9FFF !important;
    font-family: monospace;
}
button[kind="secondary"] {
    background: #000000 !important;
    border: 1px solid #2a1f10 !important;
    color: #8a6a30 !important;
    font-family: monospace;
}

/* Checkbox styling */
input[type="checkbox"] { accent-color: #FFA028; }

/* Dividers */
hr { border-color: #2a1f10; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "scr_results"  not in st.session_state: st.session_state.scr_results  = []
if "scr_intent"   not in st.session_state: st.session_state.scr_intent   = {}
if "scr_selected" not in st.session_state: st.session_state.scr_selected = set()
if "scr_query"    not in st.session_state: st.session_state.scr_query    = ""

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown(
    "<h2 style='color:#FFA028;font-family:monospace;margin-bottom:0;'>"
    "🔍 Stock Screener</h2>",
    unsafe_allow_html=True,
)
st.markdown(
    "<p style='color:#5a4a25;font-family:monospace;font-size:12px;margin-top:2px;'>"
    "Natural language → EODHD global universe filter → "
    "launch any framework on selected companies</p>",
    unsafe_allow_html=True,
)
st.divider()

# ── LLM status ────────────────────────────────────────────────────────────────
llm = LLMClient()
ok, _msg = llm.check_configured()

# ── Exchange hints ────────────────────────────────────────────────────────────
with st.expander("🌍 Exchange codes reference", expanded=False):
    from data_sources.eodhd_screener_api import EXCHANGE_CODES
    cols = st.columns(3)
    items = list(EXCHANGE_CODES.items())
    chunk = len(items) // 3 + 1
    for i, col in enumerate(cols):
        for code, desc in items[i*chunk:(i+1)*chunk]:
            col.markdown(
                f"<span style='color:#FFA028;font-family:monospace;font-weight:700;'>"
                f"{code}</span>"
                f"<span style='color:#5a4a25;font-family:monospace;font-size:11px;'>"
                f" — {desc}</span>",
                unsafe_allow_html=True,
            )

st.markdown("")

# ── Query input ───────────────────────────────────────────────────────────────
with st.form("scr_form", border=False):
    _col_q, _col_btn = st.columns([5, 1])
    with _col_q:
        query = st.text_input(
            "Query",
            value=st.session_state.scr_query,
            placeholder="e.g.  European defense companies, market cap > 2B   |   "
                        "High dividend US banks   |   Didžiausios Lenkijos kompanijos",
            label_visibility="collapsed",
            key="scr_query_input",
        )
    with _col_btn:
        search_clicked = st.form_submit_button(
            "Search",
            type="primary",
            use_container_width=True,
            disabled=not ok,
        )

if not ok:
    st.caption(f"⚠ LLM not configured: {_msg}")

# ── Run search ────────────────────────────────────────────────────────────────
if search_clicked and query.strip():
    st.session_state.scr_query = query.strip()
    st.session_state.scr_results = []
    st.session_state.scr_selected = set()

    with st.status("🔍 Interpreting query…", expanded=True) as _status:
        try:
            import importlib
            import models.screener_intent as _si_mod
            importlib.reload(_si_mod)
            from models.screener_intent import parse_screener_intent

            st.write("🤖 Parsing query with LLM…")
            intent = parse_screener_intent(query.strip(), llm)
            st.session_state.scr_intent = intent

            if intent.get("notes"):
                st.caption(f"💡 {intent['notes']}")

            st.write(
                f"📡 Calling EODHD Screener API · "
                f"{len(intent.get('filters', []))} filter(s) · "
                f"limit {intent.get('limit', 20)}…"
            )
            import importlib
            import data_sources.eodhd_screener_api as _sa_mod
            importlib.reload(_sa_mod)
            from data_sources.eodhd_screener_api import run_screener

            rows = run_screener(
                filters=intent.get("filters") or [],
                signals=intent.get("signals") or [],
                sort=intent.get("sort") or "market_capitalization.desc",
                limit=intent.get("limit") or 20,
            )
            st.session_state.scr_results = rows

            n = len(rows)
            if n:
                st.write(f"✓ {n} companies found.")
                _status.update(
                    label=f"✅ {n} results for: {intent.get('title', query)}",
                    state="complete", expanded=False,
                )
            else:
                st.warning(
                    "No results. Try broader criteria — fewer filters, "
                    "higher market cap limit, or different sector/exchange."
                )
                _status.update(label="⚠ No results", state="error", expanded=True)

        except Exception as _e:
            st.error(f"Search failed: {_e}")
            _status.update(label="❌ Search failed", state="error", expanded=True)

# ── Results table ─────────────────────────────────────────────────────────────
results = st.session_state.scr_results
intent  = st.session_state.scr_intent

if results:
    title = intent.get("title") or st.session_state.scr_query
    st.markdown(
        f"<h4 style='color:#FFA028;font-family:monospace;margin-bottom:4px;'>"
        f"Results — {title}"
        f"<span style='color:#5a4a25;font-size:13px;'> · {len(results)} companies</span>"
        f"</h4>",
        unsafe_allow_html=True,
    )

    # ── Select all / deselect all ─────────────────────────────────────────────
    # Build the full yf_tick list once so Select/Deselect can also update
    # the individual checkbox session_state keys (Streamlit keyed widgets
    # ignore the `value=` param after first render — must set the key directly).
    _all_yf = [_to_yf(r.get("code", ""), r.get("exchange", "")) for r in results]

    _sa_col, _da_col, _info_col = st.columns([1, 1, 4])
    with _sa_col:
        if st.button("☑ Select All", use_container_width=True):
            st.session_state.scr_selected = set(_all_yf)
            for _i, _t in enumerate(_all_yf):
                st.session_state[f"scr_chk_{_i}_{_t}"] = True
            st.rerun()
    with _da_col:
        if st.button("☐ Deselect All", use_container_width=True):
            st.session_state.scr_selected = set()
            for _i, _t in enumerate(_all_yf):
                st.session_state[f"scr_chk_{_i}_{_t}"] = False
            st.rerun()
    with _info_col:
        n_sel = len(st.session_state.scr_selected)
        if n_sel:
            st.markdown(
                f"<span style='color:#4D9FFF;font-family:monospace;font-size:13px;'>"
                f"✓ {n_sel} selected</span>",
                unsafe_allow_html=True,
            )

    st.markdown("")

    # ── Table header ──────────────────────────────────────────────────────────
    _H = [0.25, 0.6, 1.8, 1.2, 1.1, 1.1, 1.0, 0.9, 0.5]
    hdr = st.columns(_H)
    for col, label in zip(hdr, ["", "Ticker", "Name", "Sector",
                                  "MCap", "EPS", "Div Yield",
                                  "Price", "Exchange"]):
        col.markdown(
            f"<span class='scr-header'>{label}</span>",
            unsafe_allow_html=True,
        )

    st.markdown(
        "<hr style='margin:2px 0 4px 0;border-color:#2a1f10;'>",
        unsafe_allow_html=True,
    )

    # ── Table rows ────────────────────────────────────────────────────────────
    for i, row in enumerate(results):
        code     = row.get("code") or ""
        exchange = row.get("exchange") or ""
        yf_tick  = _to_yf(code, exchange)
        name     = (row.get("name") or "")[:28]
        sector   = (row.get("sector") or "")[:16]
        mcap     = row.get("market_capitalization")
        eps      = row.get("earnings_share")
        div_y    = row.get("dividend_yield")
        price    = row.get("adjusted_close")

        is_sel = yf_tick in st.session_state.scr_selected

        cols = st.columns(_H)
        # Checkbox
        if cols[0].checkbox("", value=is_sel, key=f"scr_chk_{i}_{yf_tick}",
                            label_visibility="collapsed"):
            st.session_state.scr_selected.add(yf_tick)
        else:
            st.session_state.scr_selected.discard(yf_tick)

        cols[1].markdown(
            f"<span class='scr-ticker'>{yf_tick}</span>",
            unsafe_allow_html=True,
        )
        cols[2].markdown(
            f"<span class='scr-name'>{name}</span>",
            unsafe_allow_html=True,
        )
        cols[3].markdown(
            f"<span class='scr-muted'>{sector}</span>",
            unsafe_allow_html=True,
        )
        cols[4].markdown(
            f"<span class='scr-val'>{_fmt_mcap(mcap)}</span>",
            unsafe_allow_html=True,
        )
        cols[5].markdown(
            f"<span class='scr-val'>{_fmt_num(eps)}</span>",
            unsafe_allow_html=True,
        )
        cols[6].markdown(
            f"<span class='scr-val'>{_fmt_pct(div_y)}</span>",
            unsafe_allow_html=True,
        )
        cols[7].markdown(
            f"<span class='scr-val'>{_fmt_num(price)}</span>",
            unsafe_allow_html=True,
        )
        cols[8].markdown(
            f"<span class='scr-muted'>{exchange}</span>",
            unsafe_allow_html=True,
        )

    st.markdown(
        "<hr style='margin:6px 0;border-color:#2a1f10;'>",
        unsafe_allow_html=True,
    )

    # ── Run framework on selected ─────────────────────────────────────────────
    selected_list = sorted(st.session_state.scr_selected)
    n_sel = len(selected_list)

    if n_sel:
        st.markdown(
            f"<p style='color:#4D9FFF;font-family:monospace;font-size:13px;"
            f"margin-bottom:6px;'>"
            f"✓ {n_sel} companies selected: "
            f"{', '.join(selected_list[:6])}"
            f"{'…' if n_sel > 6 else ''}"
            f"</p>",
            unsafe_allow_html=True,
        )

        def _launch(framework_id: str) -> None:
            label = intent.get("title") or st.session_state.scr_query or "Screener"
            st.session_state.rg_bulk_run = {
                "tickers":      selected_list,
                "universe":     label,
                "framework_id": framework_id,
                "label":        label,
            }
            st.switch_page("pages/report_generator.py")

        if st.button(
            f"⚖ Run Gravity Taxers  ·  {n_sel} companies",
            type="primary",
            use_container_width=True,
            key="scr_gravity_btn",
        ):
            _launch("gravity")

    else:
        st.markdown(
            "<span style='color:#5a4a25;font-family:monospace;font-size:12px;'>"
            "Select companies above to run a framework analysis.</span>",
            unsafe_allow_html=True,
        )


