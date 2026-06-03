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

# Index name → Yahoo Finance index ticker (for ConstituentResolver)
# Exchange-level queries: keywords → (EODHD exchange code, display name)
# Used by Path A2 to run the screener directly for a whole exchange.
_EXCHANGE_QUERY_MAP: list[tuple[list[str], str, str]] = [
    (["brazil", "bovespa", "b3", "são paulo", "sao paulo", "san paulo", "sa exchange", "b3 exchange"], "SA", "B3 · São Paulo"),
    (["mexico", "bmv", "bolsa mexicana"],                                  "MX", "BMV · Mexico"),
    (["south africa", "johannesburg", "jse"],                              "JSE", "JSE · Johannesburg"),
    (["turkey", "istanbul", "bist"],                                       "IS", "Borsa Istanbul"),
    (["israel", "tel aviv", "tase"],                                       "TLV", "TASE · Tel Aviv"),
    (["warsaw", "gpw", "poland exchange"],                                 "WAR", "GPW · Warsaw"),
    (["budapest", "hungary exchange"],                                     "BUD", "BSE · Budapest"),
    (["oslo", "norway exchange"],                                          "OL", "Oslo Børs"),
    (["copenhagen", "denmark exchange"],                                   "CO", "Nasdaq Copenhagen"),
    (["helsinki exchange", "finland exchange"],                            "HE", "Nasdaq Helsinki"),
    (["stockholm exchange", "sweden exchange"],                            "ST", "Nasdaq Stockholm"),
    (["vienna exchange", "austria exchange"],                              "VI", "Vienna Stock Exchange"),
    (["zurich", "swiss exchange", "six exchange"],                         "SW", "SIX Swiss Exchange"),
    (["euronext amsterdam", "netherlands exchange"],                       "AS", "Euronext Amsterdam"),
    (["euronext brussels", "belgium exchange"],                            "BR", "Euronext Brussels"),
    (["milan", "borsa italiana", "italy exchange"],                        "MI", "Borsa Italiana"),
    (["madrid", "spain exchange", "bme"],                                  "MC", "BME Madrid"),
    (["euronext paris", "france exchange"],                                "PA", "Euronext Paris"),
    (["xetra", "frankfurt exchange", "germany exchange"],                  "XETRA", "Xetra · Germany"),
    (["london exchange", "lse exchange"],                                  "LSE", "London Stock Exchange"),
    (["toronto exchange", "tsx exchange", "canada exchange"],              "TO", "TSX · Toronto"),
    (["asx", "australia exchange"],                                        "AU", "ASX · Australia"),
    (["hong kong exchange", "hkex"],                                       "HK", "HKEX · Hong Kong"),
    (["shanghai exchange", "sse"],                                         "SHG", "Shanghai Stock Exchange"),
    (["shenzhen exchange", "szse"],                                        "SHE", "Shenzhen Stock Exchange"),
    (["kospi exchange", "korea exchange"],                                 "KO", "KOSPI · South Korea"),
    (["nse", "india exchange", "national stock exchange"],                 "NSE", "NSE · India"),
]


def _detect_exchange_query(q: str) -> tuple[str, str] | None:
    """Return (eodhd_exchange_code, display_name) if query targets a whole exchange."""
    q_low = q.lower()
    for keywords, code, display in _EXCHANGE_QUERY_MAP:
        if any(kw in q_low for kw in keywords):
            return code, display
    return None


_INDEX_QUERY_MAP: list[tuple[list[str], str, str]] = [
    # keywords,                           yf_index,    display_name
    (["dax", "dax40", "dax 40"],         "^GDAXI",    "DAX 40 · XETRA"),
    (["mdax"],                            "^MDAXI",    "MDAX · XETRA"),
    (["cac 40", "cac40", "cac"],         "^FCHI",     "CAC 40 · Euronext Paris"),
    (["ftse 100", "ftse100"],            "^FTSE",     "FTSE 100 · LSE"),
    (["ftse 250", "ftse250"],            "^FTMC",     "FTSE 250 · LSE"),
    (["aex"],                             "^AEX",      "AEX · Euronext Amsterdam"),
    (["smi"],                             "^SSMI",     "SMI · SIX Swiss"),
    (["ibex 35", "ibex35", "ibex"],      "^IBEX",     "IBEX 35 · BME Madrid"),
    (["ftse mib", "mib"],                "^MIB",      "FTSE MIB · Borsa Italiana"),
    (["omx helsinki", "omxh25"],         "^OMXH25",   "OMX Helsinki 25"),
    (["omx stockholm", "omxs30"],        "^OMXS30",   "OMX Stockholm 30"),
    (["omx copenhagen", "omxc25"],       "^OMXC25",   "OMX Copenhagen 25"),
    (["obx", "omx oslo"],                "^OBX",      "OBX · Oslo Børs"),
    (["atx"],                             "^ATX",      "ATX · Vienna"),
    (["wig20", "wig 20"],                "^WIG20",    "WIG 20 · Warsaw"),
    (["bux"],                             "^BUX",      "BUX · Budapest"),
    (["s&p 500", "s&p500", "sp500"],     "^GSPC",     "S&P 500"),
    (["nasdaq 100", "nasdaq100", "ndx"], "^NDX",      "Nasdaq 100"),
    (["dow jones", "dow"],               "^DJI",      "Dow Jones"),
    (["tsx", "s&p/tsx"],                 "^GSPTSE",   "S&P/TSX 60"),
    (["asx 200", "asx200"],              "^AXJO",     "ASX 200"),
    (["nikkei"],                          "^N225",     "Nikkei 225"),
    (["hang seng"],                       "^HSI",      "Hang Seng"),
    (["kospi"],                           "^KS11",     "KOSPI"),
    (["stoxx 50", "stoxx50", "euro stoxx"], "^STOXX50E", "Euro Stoxx 50"),
]


def _detect_index_query(q: str) -> tuple[str, str] | None:
    """Return (yf_index_ticker, display_name) if query mentions a known index."""
    q_low = q.lower()
    for keywords, yf_ticker, display in _INDEX_QUERY_MAP:
        if any(kw in q_low for kw in keywords):
            return yf_ticker, display
    return None


# YF suffix → EODHD exchange-symbol-list code
_YF_SUFFIX_TO_EXCH = {
    "DE": "XETRA", "F": "F", "L": "LSE", "PA": "PA", "AS": "AS",
    "BR": "BR", "MI": "MI", "MC": "MC", "HE": "HE", "ST": "ST",
    "OL": "OL", "CO": "CO", "SW": "SW", "VI": "VI", "WA": "WAR",
    "TO": "TO", "AX": "AU", "HK": "HK", "SS": "SHG", "SZ": "SHE",
    "KS": "KO", "NS": "NSE", "SA": "SA", "MX": "MX", "JO": "JSE",
    "IS": "IS", "TA": "TLV", "VS": "VS", "TL": "TL", "RG": "RG",
}

import requests as _scr_requests
from config import EODHD_API_KEY as _SCR_EODHD_KEY, REQUEST_HEADERS as _SCR_HEADERS

@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_exchange_names(exch_code: str) -> dict[str, str]:
    """Fetch code→name map from EODHD exchange-symbol-list. Tries fallback codes."""
    if not _SCR_EODHD_KEY or not exch_code:
        return {}
    # Some EODHD exchange-symbol-list codes differ from screener/real-time codes
    _FALLBACKS: dict[str, list[str]] = {
        "IS":  ["IS", "BIST", "IST"],
        "JSE": ["JSE", "JO"],
        "SA":  ["BVMF", "NEO", "SAO", "SA"],   # Brazil B3; plain "SA" returns wrong data
        "WAR": ["WAR", "WA"],
        "MX":  ["MX", "BMV"],
    }
    candidates = _FALLBACKS.get(exch_code.upper(), [exch_code])
    for code in candidates:
        try:
            r = _scr_requests.get(
                f"https://eodhistoricaldata.com/api/exchange-symbol-list/{code}",
                params={"api_token": _SCR_EODHD_KEY, "fmt": "json"},
                headers=_SCR_HEADERS,
                timeout=20,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list) or len(data) < 5:
                continue
            return {item["Code"]: item.get("Name", "") for item in data
                    if isinstance(item, dict) and item.get("Code")}
        except Exception:
            continue
    return {}


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_bulk_prices(eodhd_tickers: tuple[str, ...]) -> dict[str, dict]:
    """Bulk real-time price fetch from EODHD. Returns code→{price, change_p}."""
    if not _SCR_EODHD_KEY or not eodhd_tickers:
        return {}
    try:
        first = eodhd_tickers[0]
        rest  = ",".join(eodhd_tickers[1:])
        params = {"api_token": _SCR_EODHD_KEY, "fmt": "json"}
        if rest:
            params["s"] = rest
        r = _scr_requests.get(
            f"https://eodhistoricaldata.com/api/real-time/{first}",
            params=params,
            headers=_SCR_HEADERS,
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        if isinstance(data, dict):
            data = [data]
        return {item["code"]: item for item in data if isinstance(item, dict)}
    except Exception:
        return {}


def _constituents_to_rows(tickers: list[str], name_map: dict | None = None,
                           price_map: dict | None = None) -> list[dict]:
    """Convert a list of YF tickers into screener-row dicts."""
    rows = []
    for t in tickers:
        dot      = t.rfind(".")
        code     = t[:dot] if dot != -1 else t
        yf_sfx   = t[dot+1:] if dot != -1 else "US"
        exch     = _YF_SUFFIX_TO_EXCH.get(yf_sfx.upper(), yf_sfx)
        name     = (name_map or {}).get(code, "")
        pdata    = (price_map or {}).get(code) or {}
        # EODHD real-time uses EODHD exchange suffix in the code key, e.g. "BMW.XETRA"
        if not pdata:
            pdata = (price_map or {}).get(f"{code}.{exch}") or {}
        price    = pdata.get("close") or pdata.get("adjusted_close")
        change_p = pdata.get("change_p")
        rows.append({
            "code": code, "exchange": yf_sfx,
            "name": name, "sector": "",
            "market_capitalization": None,
            "earnings_share": None,
            "dividend_yield": None,
            "adjusted_close": price,
            "change_p": change_p,
            "_yf_ticker": t,
        })
    return rows


# ── Run search ────────────────────────────────────────────────────────────────
if search_clicked and query.strip():
    st.session_state.scr_query = query.strip()
    st.session_state.scr_results = []
    st.session_state.scr_selected = set()

    with st.status("🔍 Interpreting query…", expanded=True) as _status:
        try:
            import importlib

            # ── Path A: known stock index → ConstituentResolver (like Gravity Taxers)
            _idx = _detect_index_query(query.strip())
            if _idx:
                _yf_idx, _idx_name = _idx
                st.write(f"📋 Recognised index: **{_idx_name}** — fetching constituents…")
                from constituent_resolver import ConstituentResolver
                _resolver = ConstituentResolver()
                _tickers  = _resolver.resolve(_yf_idx)

                if not _tickers:
                    st.warning(
                        f"Could not fetch constituents for **{_idx_name}** "
                        f"(Wikipedia source unavailable). "
                        f"Try a broader query like 'largest German companies'."
                    )
                    _status.update(label="⚠ No constituents found", state="error", expanded=True)
                    st.stop()

                # Apply limit from query text
                import re as _re_lim
                _lim_match = _re_lim.search(r'\b(\d+)\b', query)
                _limit = int(_lim_match.group(1)) if _lim_match else 40
                _tickers = _tickers[:_limit]

                # Enrich: exchange names + bulk prices + screener fundamentals
                _sfx = _tickers[0].rsplit(".", 1)[-1] if "." in _tickers[0] else "US"
                _exch_code = _YF_SUFFIX_TO_EXCH.get(_sfx.upper(), _sfx)
                st.write(f"📡 Fetching names, prices & fundamentals from EODHD ({_exch_code})…")
                _name_map  = _fetch_exchange_names(_exch_code)
                # Build EODHD-format tickers for bulk real-time
                _eodhd_tix = tuple(
                    f"{t.rsplit('.', 1)[0]}.{_exch_code}" if "." in t else f"{t}.US"
                    for t in _tickers
                )
                _price_map = _fetch_bulk_prices(_eodhd_tix)

                rows = _constituents_to_rows(_tickers, _name_map, _price_map)
                intent = {
                    "title": f"{_idx_name} · {len(rows)} companies",
                    "notes": "",
                    "filters": [], "signals": [], "sort": None, "limit": len(rows),
                }

            # ── Path A2: whole-exchange query → bulk_last_day (sorted by volume)
            elif _detect_exchange_query(query.strip()):
                _exch_code2, _exch_name2 = _detect_exchange_query(query.strip())
                import re as _re_lim2
                _lim2 = int(_re_lim2.search(r'\b(\d+)\b', query).group(1)) if _re_lim2.search(r'\b(\d+)\b', query) else 20
                _lim2 = min(_lim2, 100)
                st.write(f"📡 Fetching **{_exch_name2}** last-day EOD data from EODHD…")
                try:
                    _bulk_r = _scr_requests.get(
                        f"https://eodhistoricaldata.com/api/eod/bulk_last_day/{_exch_code2}",
                        params={"api_token": _SCR_EODHD_KEY, "fmt": "json", "type": "eod"},
                        headers=_SCR_HEADERS,
                        timeout=30,
                    )
                    _bulk_data = _bulk_r.json() if _bulk_r.status_code == 200 else []
                except Exception:
                    _bulk_data = []

                if not isinstance(_bulk_data, list) or not _bulk_data:
                    st.warning(f"No data returned for **{_exch_name2}** (code: `{_exch_code2}`). EODHD may not support this exchange in bulk EOD.")
                    _status.update(label="⚠ No data", state="error", expanded=True)
                    st.stop()

                # Sort by volume descending (best liquidity proxy without market cap)
                _bulk_data.sort(key=lambda x: x.get("volume") or 0, reverse=True)
                rows = []
                for _bd in _bulk_data[:_lim2]:
                    _c2 = _bd.get("code", "")
                    rows.append({
                        "code": _c2,
                        "exchange": _exch_code2,
                        "name": _bd.get("name", "") or _c2,
                        "sector": "",
                        "market_capitalization": None,
                        "earnings_share": None,
                        "dividend_yield": None,
                        "adjusted_close": _bd.get("adjusted_close") or _bd.get("close"),
                        "change_p": _bd.get("change_p"),
                        "_yf_ticker": _to_yf(_c2, _exch_code2),
                    })
                intent = {
                    "title": f"{_exch_name2} · top {len(rows)} by volume",
                    "notes": "Sorted by last-day volume. Sector/MCap/DivYield not available for whole-exchange queries.",
                    "filters": [], "signals": [], "sort": None, "limit": len(rows),
                }

            # ── Path B: general NL query → LLM → EODHD screener API
            else:
                import models.screener_intent as _si_mod
                importlib.reload(_si_mod)
                from models.screener_intent import parse_screener_intent

                st.write("🤖 Parsing query with LLM…")
                intent = parse_screener_intent(query.strip(), llm)

                if intent.get("notes"):
                    st.caption(f"💡 {intent['notes']}")

                st.write(
                    f"📡 EODHD Screener API · "
                    f"{len(intent.get('filters', []))} filter(s) · "
                    f"limit {intent.get('limit', 20)}…"
                )
                import data_sources.eodhd_screener_api as _sa_mod
                importlib.reload(_sa_mod)
                from data_sources.eodhd_screener_api import run_screener

                rows = run_screener(
                    filters=intent.get("filters") or [],
                    signals=intent.get("signals") or [],
                    sort=intent.get("sort") or "market_capitalization.desc",
                    limit=intent.get("limit") or 20,
                )

            st.session_state.scr_intent   = intent
            st.session_state.scr_results  = rows

            n = len(rows)
            if n:
                st.write(f"✓ {n} companies found.")
                _status.update(
                    label=f"✅ {n} results · {intent.get('title', query)}",
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
    _all_yf = [r.get("_yf_ticker") or _to_yf(r.get("code", ""), r.get("exchange", "")) for r in results]

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
    _H = [0.25, 0.7, 2.5, 1.1, 1.1, 1.1, 1.0, 0.8, 0.8]
    hdr = st.columns(_H)
    for col, label in zip(hdr, ["", "Ticker", "Name", "Sector",
                                  "MCap", "Div Yield",
                                  "Price", "Chg%", "Exch"]):
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
        # ConstituentResolver rows carry _yf_ticker directly; EODHD rows need conversion
        yf_tick  = row.get("_yf_ticker") or _to_yf(code, exchange)
        name     = (row.get("name") or "")[:32]
        sector   = (row.get("sector") or "")[:14]
        mcap     = row.get("market_capitalization")
        div_y    = row.get("dividend_yield")
        price    = row.get("adjusted_close")
        chg_p    = row.get("change_p")

        is_sel = yf_tick in st.session_state.scr_selected

        cols = st.columns(_H)
        if cols[0].checkbox("Select", value=is_sel, key=f"scr_chk_{i}_{yf_tick}",
                            label_visibility="collapsed"):
            st.session_state.scr_selected.add(yf_tick)
        else:
            st.session_state.scr_selected.discard(yf_tick)

        cols[1].markdown(f"<span class='scr-ticker'>{yf_tick}</span>", unsafe_allow_html=True)
        cols[2].markdown(f"<span class='scr-name'>{name}</span>",      unsafe_allow_html=True)
        cols[3].markdown(f"<span class='scr-muted'>{sector}</span>",   unsafe_allow_html=True)
        cols[4].markdown(f"<span class='scr-val'>{_fmt_mcap(mcap)}</span>",  unsafe_allow_html=True)
        cols[5].markdown(f"<span class='scr-val'>{_fmt_pct(div_y)}</span>",  unsafe_allow_html=True)
        cols[6].markdown(f"<span class='scr-val'>{_fmt_num(price)}</span>",  unsafe_allow_html=True)

        # Change% — colour coded green/red
        if chg_p is not None:
            try:
                _c = float(chg_p)
                _col = "#4D9FFF" if _c >= 0 else "#FF3030"
                _sign = "+" if _c >= 0 else ""
                cols[7].markdown(
                    f"<span style='color:{_col};font-family:monospace;font-size:12px;'>"
                    f"{_sign}{_c:.2f}%</span>", unsafe_allow_html=True)
            except Exception:
                cols[7].markdown("<span class='scr-muted'>—</span>", unsafe_allow_html=True)
        else:
            cols[7].markdown("<span class='scr-muted'>—</span>", unsafe_allow_html=True)

        cols[8].markdown(f"<span class='scr-muted'>{exchange}</span>", unsafe_allow_html=True)

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
            f"✓ {n_sel} selected: "
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

    st.button(
        f"⚖ Run Gravity Taxers  ·  {n_sel} selected" if n_sel
        else "⚖ Run Gravity Taxers  ·  select companies above",
        type="primary",
        use_container_width=True,
        key="scr_gravity_btn",
        disabled=(n_sel == 0),
        on_click=_launch,
        args=("gravity",),
    )


