"""
my_portfolio.py — Personal watchlist / portfolio tracker (EODHD-only).

Stores a user-chosen list of tickers in `data/portfolio.json` so the list
persists across app sessions.

Compact rendering — one card = one row by default:
  Name · Price · Mkt Cap · P/E · ROE · EBIT Margin · YTD% · ▼ Expand

Clicking the expand toggle reveals:
  • Recommendation badge (Buy / Hold / Sell, rule-based)
  • Period-selectable price chart (1d / 1m / 6m / YTD / 5y / All)
  • Latest news from EODHD /news

Tickers are entered in Yahoo Finance format (RHM.DE, AAPL, ^GSPC, ...) and
converted to EODHD format via _convert_ticker(). Indices/forex without
fundamentals fall back to real-time + EOD only.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Any

import altair as alt
import pandas as pd
import requests
import streamlit as st
from streamlit_searchbox import st_searchbox

from config import EODHD_API_KEY, REQUEST_HEADERS
from data_sources.eodhd_adapter import _YF_TO_EODHD

# Reverse mapping: EODHD exchange code → Yahoo Finance suffix.
# Built once from _YF_TO_EODHD. Some collisions are inevitable (e.g. both
# ".VX" and ".SW" map to EODHD ".SW") — the last entry wins, which for our
# use case (showing results to the user) is fine.
_EODHD_TO_YF = {v: k for k, v in _YF_TO_EODHD.items()}

# ── Storage ───────────────────────────────────────────────────────────────────
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_PORTFOLIO_FILE = _DATA_DIR / "portfolio.json"

EODHD_BASE = "https://eodhistoricaldata.com/api"


def _load_portfolio() -> list[str]:
    if not _PORTFOLIO_FILE.exists():
        return []
    try:
        raw = json.loads(_PORTFOLIO_FILE.read_text(encoding="utf-8"))
        return list(raw.get("tickers", []))
    except Exception:
        return []


def _save_portfolio(tickers: list[str]) -> None:
    _PORTFOLIO_FILE.write_text(
        json.dumps({"tickers": tickers}, indent=2),
        encoding="utf-8",
    )


# ── Ticker conversion (Yahoo → EODHD) ─────────────────────────────────────────
def _convert_ticker(yf_ticker: str) -> str:
    t = yf_ticker.strip().upper()
    if t.endswith("=X"):
        return t.replace("=X", "") + ".FOREX"
    if t.startswith("^"):
        return t[1:] + ".INDX"
    dot = t.rfind(".")
    if dot == -1:
        return f"{t}.US"
    suffix = t[dot:]
    base = t[:dot]
    eodhd_suffix = _YF_TO_EODHD.get(suffix, suffix)
    if eodhd_suffix == ".HK" and base.isdigit():
        base = base.zfill(4)
    if eodhd_suffix in (".KO", ".KQ") and base.isdigit():
        base = base.zfill(6)
    return f"{base}{eodhd_suffix}"


# ── Low-level EODHD GET ───────────────────────────────────────────────────────
def _eodhd_get(path: str, params: dict | None = None, timeout: int = 30) -> Optional[Any]:
    if not EODHD_API_KEY:
        return None
    p = {"api_token": EODHD_API_KEY, "fmt": "json"}
    if params:
        p.update(params)
    try:
        url = f"{EODHD_BASE}{path}"
        r = requests.get(url, params=p, headers=REQUEST_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _to_float(v) -> Optional[float]:
    if v is None or v == "" or v == "NA":
        return None
    try:
        return float(v)
    except Exception:
        return None


# ── Snapshot (cached) ─────────────────────────────────────────────────────────
@st.cache_data(ttl=900, show_spinner=False)   # 15-minute cache
def _fetch_snapshot(yf_ticker: str) -> dict:
    """
    All snapshot metrics for one ticker from EODHD, including YTD%.
    """
    eodhd_ticker = _convert_ticker(yf_ticker)

    # ── Real-time price ──────────────────────────────────────────────────────
    rt = _eodhd_get(f"/real-time/{eodhd_ticker}") or {}
    price = _to_float(rt.get("close"))
    if price is None or price < 0:
        price = _to_float(rt.get("previousClose"))

    # ── Fundamentals (may be missing for indices/forex) ──────────────────────
    time.sleep(0.2)
    fund = _eodhd_get(f"/fundamentals/{eodhd_ticker}") or {}
    general    = fund.get("General")    or {}
    highlights = fund.get("Highlights") or {}

    name = general.get("Name") or yf_ticker
    sector = general.get("Sector") or general.get("Type") or ""
    currency = (
        general.get("CurrencyCode")
        or general.get("CurrencySymbol")
        or rt.get("currency")
        or ""
    )

    mc_mln = _to_float(highlights.get("MarketCapitalizationMln"))
    market_cap = mc_mln * 1e6 if mc_mln is not None else _to_float(highlights.get("MarketCapitalization"))

    pe          = _to_float(highlights.get("PERatio"))
    roe         = _to_float(highlights.get("ReturnOnEquityTTM"))
    ebit_margin = _to_float(highlights.get("OperatingMarginTTM"))

    # ── YTD: pull the first trading day of the current year close ────────────
    ytd_pct: Optional[float] = None
    if price is not None:
        today = datetime.utcnow().date()
        year_start = datetime(today.year, 1, 1).date()
        eod = _eodhd_get(
            f"/eod/{eodhd_ticker}",
            params={"from": year_start.isoformat(),
                    "to":   today.isoformat(),
                    "period": "d", "order": "a"},
            timeout=30,
        )
        if isinstance(eod, list) and eod:
            for row in eod:
                if not isinstance(row, dict):
                    continue
                # Prefer split/dividend-adjusted close so a YoY split
                # (e.g. AAPL Aug 2020) doesn't break the YTD%.
                c = (_to_float(row.get("adjusted_close"))
                     or _to_float(row.get("adjusted"))
                     or _to_float(row.get("close")))
                if c and c > 0:
                    try:
                        ytd_pct = (float(price) / c - 1)
                    except Exception:
                        pass
                    break

    return {
        "eodhd_ticker": eodhd_ticker,
        "name":         name,
        "currency":     currency,
        "sector":       sector,
        "price":        price,
        "market_cap":   market_cap,
        "pe":           pe,
        "roe":          roe,
        "ebit_margin":  ebit_margin,
        "ytd_pct":      ytd_pct,
    }


# ── History (cached, period-aware) ────────────────────────────────────────────
PERIODS = ["1d", "1m", "6m", "YTD", "5y", "All"]
DEFAULT_PERIOD = "5y"


def _period_range(period: str) -> tuple[Optional[datetime.date], datetime.date]:
    """Return (from_date, to_date). from_date=None means 'as far back as possible'."""
    today = datetime.utcnow().date()
    if period == "1d":
        # Last 5 trading days — we'll filter to 1 day's worth in fetch
        return (today - timedelta(days=7), today)
    if period == "1m":
        return (today - timedelta(days=35), today)
    if period == "6m":
        return (today - timedelta(days=185), today)
    if period == "YTD":
        return (datetime(today.year, 1, 1).date(), today)
    if period == "5y":
        return (today - timedelta(days=5 * 365 + 7), today)
    if period == "All":
        return (None, today)
    # Fallback
    return (today - timedelta(days=365), today)


@st.cache_data(ttl=1800, show_spinner=False)   # 30-min cache
def _fetch_history(yf_ticker: str, period: str) -> Optional[pd.DataFrame]:
    """
    Price history for the requested period.

    For "1d" we use the EODHD intraday endpoint with 5-minute bars so the
    chart actually shows the day's price action — daily-OHLC would give
    only 1-2 points for that range. All other periods use the daily /eod
    endpoint.
    """
    eodhd_ticker = _convert_ticker(yf_ticker)

    # ── 1-day chart: intraday 5-minute bars ──────────────────────────────────
    if period == "1d":
        # EODHD intraday: timestamp params are Unix seconds
        now_utc = datetime.utcnow()
        # Fetch the last 24h (covers extended-hours bars on US tickers)
        from_ts = int((now_utc - timedelta(hours=24)).timestamp())
        to_ts   = int(now_utc.timestamp())
        data = _eodhd_get(
            f"/intraday/{eodhd_ticker}",
            params={"interval": "5m", "from": from_ts, "to": to_ts},
            timeout=30,
        )
        if not isinstance(data, list) or not data:
            return None
        try:
            df = pd.DataFrame(data)
            ts_col = "datetime" if "datetime" in df.columns else "timestamp"
            if ts_col not in df.columns or "close" not in df.columns:
                return None
            if ts_col == "timestamp":
                df[ts_col] = pd.to_datetime(df[ts_col], unit="s")
            else:
                df[ts_col] = pd.to_datetime(df[ts_col])
            df = df.set_index(ts_col)[["close"]].rename(columns={"close": "Close"})
            df.index.name = "Time"
            # Keep only the most recent trading session
            return df
        except Exception:
            return None

    # ── All other periods: daily OHLC ────────────────────────────────────────
    # We prefer EODHD's `adjusted_close` (split- and dividend-adjusted) so
    # multi-year charts (especially "All") don't show a phantom dip on the
    # day of a stock split — e.g. AAPL's 4-for-1 in Aug 2020.
    start, end = _period_range(period)
    params = {"period": "d", "order": "a", "to": end.isoformat()}
    if start is not None:
        params["from"] = start.isoformat()
    data = _eodhd_get(f"/eod/{eodhd_ticker}", params=params, timeout=45)
    if not isinstance(data, list) or not data:
        return None
    try:
        df = pd.DataFrame(data)
        if "date" not in df.columns:
            return None
        # Choose the best price column: adjusted_close → adjusted → close
        price_col = None
        for cand in ("adjusted_close", "adjusted", "close"):
            if cand in df.columns:
                price_col = cand
                break
        if price_col is None:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")[[price_col]].rename(columns={price_col: "Close"})
        df.index.name = "Date"
        # Drop any rows where the price came back as None / NaN
        df = df.dropna(subset=["Close"])
        return df if not df.empty else None
    except Exception:
        return None


# ── Upcoming earnings date (cached) ───────────────────────────────────────────
@st.cache_data(ttl=6 * 3600, show_spinner=False)   # 6-hour cache
def _fetch_next_earnings(yf_ticker: str) -> Optional[str]:
    """
    Return the next upcoming earnings report date (YYYY-MM-DD) for the
    ticker, or None if EODHD doesn't have one scheduled in the next 180 days.

    Uses /calendar/earnings — works for most US + EU listings. Indices,
    forex and ETFs return None.
    """
    eodhd_ticker = _convert_ticker(yf_ticker)
    today = datetime.utcnow().date()
    end   = today + timedelta(days=180)
    data = _eodhd_get(
        "/calendar/earnings",
        params={
            "symbols": eodhd_ticker,
            "from":    today.isoformat(),
            "to":      end.isoformat(),
        },
        timeout=30,
    )
    if not isinstance(data, dict):
        return None
    rows = data.get("earnings")
    if not isinstance(rows, list) or not rows:
        return None
    upcoming: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = row.get("report_date") or row.get("date")
        if not d:
            continue
        try:
            dt = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if dt >= today:
            upcoming.append(str(d)[:10])
    if not upcoming:
        return None
    return min(upcoming)


# ── News (cached) ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_news(yf_ticker: str, limit: int = 15) -> list[dict]:
    eodhd_ticker = _convert_ticker(yf_ticker)
    data = _eodhd_get(
        "/news",
        params={"s": eodhd_ticker, "limit": limit, "offset": 0},
        timeout=30,
    )
    if not isinstance(data, list):
        return []
    return data


# ── Recommendation heuristic ──────────────────────────────────────────────────
def _recommendation(snap: dict) -> tuple[str, str]:
    pe   = snap.get("pe")
    roe  = snap.get("roe")
    ebit = snap.get("ebit_margin")
    if pe is None and roe is None and ebit is None:
        return "—", "#888888"
    score = 0
    used = 0
    if pe is not None:
        used += 1
        if pe <= 0:      score -= 1
        elif pe < 15:    score += 2
        elif pe < 25:    score += 1
        elif pe < 35:    score += 0
        else:            score -= 1
    if roe is not None:
        used += 1
        if roe >= 0.20:   score += 2
        elif roe >= 0.12: score += 1
        elif roe >= 0.05: score += 0
        elif roe >= 0:    score -= 1
        else:             score -= 2
    if ebit is not None:
        used += 1
        if ebit >= 0.20:   score += 2
        elif ebit >= 0.10: score += 1
        elif ebit >= 0.05: score += 0
        elif ebit >= 0:    score -= 1
        else:              score -= 2
    if used == 0:
        return "—", "#888888"
    avg = score / used
    if avg >= 1.0:  return "BUY",  "#1A7E3D"
    if avg <= -0.5: return "SELL", "#B83227"
    return "HOLD", "#C49102"


# ── Formatters ────────────────────────────────────────────────────────────────
def _fmt_money(v) -> str:
    if v is None: return "—"
    try: v = float(v)
    except Exception: return "—"
    if abs(v) >= 1e12: return f"{v/1e12:.2f}T"
    if abs(v) >= 1e9:  return f"{v/1e9:.2f}B"
    if abs(v) >= 1e6:  return f"{v/1e6:.2f}M"
    if abs(v) >= 1e3:  return f"{v/1e3:.2f}K"
    return f"{v:.2f}"


def _fmt_price(v, ccy: str = "") -> str:
    if v is None: return "—"
    try: return f"{float(v):,.2f} {ccy}".strip()
    except Exception: return "—"


def _fmt_ratio(v) -> str:
    if v is None: return "—"
    try: return f"{float(v):.2f}×"
    except Exception: return "—"


def _fmt_pct(v) -> str:
    if v is None: return "—"
    try: return f"{float(v)*100:.1f}%"
    except Exception: return "—"


def _fmt_signed_pct(v) -> tuple[str, str]:
    """Return (text, color) — green/red based on sign."""
    if v is None:
        return "—", "#888888"
    try:
        pct = float(v) * 100
    except Exception:
        return "—", "#888888"
    color = "#1A7E3D" if pct >= 0 else "#B83227"
    return f"{pct:+.2f}%", color


def _normalize_ticker(raw: str) -> str:
    return raw.strip().upper().replace(" ", "")


# ── Reverse ticker conversion (EODHD → Yahoo Finance) ────────────────────────
def _eodhd_to_yf(code: str, exchange: str) -> str:
    """
    Convert EODHD (Code, Exchange) → Yahoo Finance ticker so it can be
    stored in the portfolio the same way users normally enter it.

    Examples:
      ("AAPL",   "US")    → "AAPL"
      ("RHM",    "XETRA") → "RHM.DE"
      ("005930", "KO")    → "005930.KS"
      ("GSPC",   "INDX")  → "^GSPC"
      ("EURUSD", "FOREX") → "EURUSD=X"
    """
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
    yf_suffix = _EODHD_TO_YF.get(eodhd_suffix, eodhd_suffix)
    return f"{code}{yf_suffix}"


# ── EODHD search (cached) ────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)   # 5-minute cache per query
def _search_eodhd_raw(query: str) -> list[dict]:
    """Hit EODHD /search/{query} — returns the raw list of matches."""
    q = (query or "").strip()
    if len(q) < 1:
        return []
    data = _eodhd_get(f"/search/{q}", params={"limit": 15})
    return data if isinstance(data, list) else []


def _ticker_search(query: str) -> list[tuple[str, str]]:
    """
    Callback for st_searchbox. Returns a list of (display_label, yf_ticker)
    tuples. yf_ticker is the value stored in the portfolio on selection.

    Empty / very short queries return [] — searchbox simply shows nothing.
    """
    if not query or len(query.strip()) < 1:
        return []
    rows = _search_eodhd_raw(query)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        code = item.get("Code", "")
        exch = item.get("Exchange", "")
        name = item.get("Name", "") or ""
        ttype = (item.get("Type") or "").strip()
        country = (item.get("Country") or "").strip()
        if not code:
            continue
        yf_ticker = _eodhd_to_yf(code, exch)
        if not yf_ticker or yf_ticker in seen:
            continue
        seen.add(yf_ticker)
        # Build a compact, readable label
        meta_bits = [b for b in (exch, country, ttype) if b]
        meta = " · ".join(meta_bits)
        label = f"{yf_ticker:<14}  {name[:48]}"
        if meta:
            label += f"  ({meta})"
        out.append((label, yf_ticker))
    return out


# ── Page header (compact) ─────────────────────────────────────────────────────
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
    # Shrink the auto-styled h4 headings Streamlit wraps subheaders /
    # #### markdown blocks in, so they don't dominate the layout.
    ".st-emotion-cache-1dy2t46 h4 { font-size: 1rem !important; }"
    "</style>",
    unsafe_allow_html=True,
)
st.markdown(
    "<div class='eq-page-title' "
    "style='display:flex;align-items:center;gap:8px;margin:0;'>"
    "<span style='font-size:20px;'>📁</span>"
    "<span style='font-size:16px;font-weight:600;color:#1B3F6E;'>"
    "My Portfolio</span></div>",
    unsafe_allow_html=True,
)

if not EODHD_API_KEY:
    st.error(
        "❌ `EODHD_API_KEY` is not configured. This page requires an EODHD "
        "subscription (All-In-One plan). Set it in `.env` locally or in "
        "Streamlit Cloud secrets."
    )
    st.stop()

# ── Session state ─────────────────────────────────────────────────────────────
if "portfolio_tickers" not in st.session_state:
    st.session_state.portfolio_tickers = _load_portfolio()
if "portfolio_expanded" not in st.session_state:
    st.session_state.portfolio_expanded = set()        # tickers currently expanded
if "portfolio_periods" not in st.session_state:
    st.session_state.portfolio_periods = {}            # ticker -> selected period

# ── Add-ticker searchbox (type-as-you-go, EODHD /search) ──────────────────────
# st_searchbox calls _ticker_search() on every keystroke (debounced) and
# shows the returned suggestions in a dropdown beneath the input. Picking
# one adds it to the portfolio immediately — no extra confirm click.
selected_ticker = st_searchbox(
    search_function=_ticker_search,
    placeholder="🔍 Type a ticker or company name (e.g. AAPL, Rheinmetall, S&P 500)",
    label=None,
    clear_on_submit=True,
    key="ticker_searchbox",
)

if selected_ticker:
    norm = _normalize_ticker(selected_ticker)
    if norm and norm not in st.session_state.portfolio_tickers:
        st.session_state.portfolio_tickers.append(norm)
        _save_portfolio(st.session_state.portfolio_tickers)
        st.success(f"Added **{norm}** to portfolio.")
        st.rerun()
    elif norm in st.session_state.portfolio_tickers:
        st.info(f"**{norm}** is already in your portfolio.")

# ── Top bar ───────────────────────────────────────────────────────────────────
top_l, top_m, top_r = st.columns([5, 1, 1])
with top_l:
    if st.session_state.portfolio_tickers:
        st.markdown(
            f"**{len(st.session_state.portfolio_tickers)}** ticker"
            f"{'s' if len(st.session_state.portfolio_tickers) != 1 else ''} tracked"
        )
with top_m:
    if st.button("⏷ Expand all", use_container_width=True,
                 disabled=not st.session_state.portfolio_tickers):
        st.session_state.portfolio_expanded = set(st.session_state.portfolio_tickers)
        st.rerun()
with top_r:
    if st.button("🔄 Refresh", use_container_width=True):
        _fetch_snapshot.clear()
        _fetch_history.clear()
        _fetch_news.clear()
        _fetch_next_earnings.clear()
        st.rerun()

# Responsive card CSS — every holding renders as a single .pf-card with
# a CSS Grid inside. The grid reflows at narrow viewports:
#   ≥769px : 1 name cell + 7 metric cells in one horizontal row
#   ≤768px : name spans 2 cols, metrics stack into 2-col grid
#   ≤380px : metrics collapse to 1 col
# Action buttons live in a separate Streamlit column that wraps below the
# card content on mobile (Streamlit columns wrap at ~640px).
st.markdown(
    """
    <style>
      /* ── Card container ──────────────────────────────────────────── */
      .pf-card {
        background: #FFFFFF;
        border: 1px solid #E5EAF0;
        border-radius: 10px;
        padding: 12px 14px;
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
      }
      .pf-card:hover {
        border-color: #C0CDD8;
        box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
      }

      /* ── Summary grid: name + 7 metrics ──────────────────────────── */
      .pf-summary {
        display: grid;
        grid-template-columns:
          minmax(0, 1.8fr)   /* name + ticker */
          repeat(7, minmax(0, 1fr));   /* earnings · price · mcap · pe · roe · ebit · ytd */
        gap: 6px 10px;
        align-items: center;
      }

      .pf-name-cell {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .pf-name {
        font-weight: 700;
        color: #1B3F6E;
        font-size: 14px;
        line-height: 1.2;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .pf-sub {
        font-size: 11px;
        color: #8A92A0;
        line-height: 1.2;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .pf-metric {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        gap: 2px;
        min-width: 0;
      }
      .pf-metric-label {
        font-size: 10px;
        color: #8A92A0;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        line-height: 1.1;
      }
      .pf-metric-value {
        font-size: 13px;
        color: #222;
        font-weight: 500;
        line-height: 1.2;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 100%;
      }
      .pf-metric-value.bold { font-weight: 700; }
      .pf-metric-value.muted { color: #8A92A0; font-style: italic; }

      /* ── Tablet (≤768px): name spans full width, metrics in 2 cols, label inline ── */
      @media (max-width: 768px) {
        .pf-summary {
          grid-template-columns: 1fr 1fr;
          gap: 8px 12px;
        }
        .pf-name-cell {
          grid-column: 1 / -1;
          border-bottom: 1px solid #EDF1F5;
          padding-bottom: 6px;
          align-items: center;
          text-align: center;
        }
        .pf-name { font-size: 15px; }
        .pf-metric {
          flex-direction: row;
          align-items: baseline;
          justify-content: space-between;
          text-align: left;
          gap: 8px;
        }
        .pf-metric-label {
          font-size: 11px;
          letter-spacing: 0.3px;
          flex-shrink: 0;
        }
        .pf-metric-value {
          font-size: 14px;
          font-weight: 600;
          text-align: right;
        }
      }

      /* ── Phone (≤380px): single-column stack ─────────────────────── */
      @media (max-width: 380px) {
        .pf-summary { grid-template-columns: 1fr; }
      }

      /* ── Action buttons (▾ toggle / ✕ delete) ────────────────────── */
      /* All buttons inside a horizontal block (i.e. the action column)
         get rounded squares; on mobile the column wraps and the two
         nested-column buttons sit side-by-side at full width. */
      div[data-testid="stButton"] > button {
        border-radius: 8px !important;
        min-height: 36px !important;
        line-height: 1 !important;
      }

      /* Red styling for any ✕ "remove" button — both the small glyph
         button up top (when card is collapsed) and the labelled
         "✕ Remove from portfolio" button at the bottom of an expanded
         card. We use an invisible anchor div rendered right before the
         button and target the next sibling element-container's button
         via :has(). Modern browsers (Chrome 105+, Safari 15.4+, FF 121+)
         all support :has(); older fallback: button stays default style. */
      div[data-testid="element-container"]:has(.pf-del-anchor)
        + div[data-testid="element-container"]
        div[data-testid="stButton"] > button {
        color: #B83227 !important;
        border-color: #E8C0BC !important;
      }
      div[data-testid="element-container"]:has(.pf-del-anchor)
        + div[data-testid="element-container"]
        div[data-testid="stButton"] > button:hover {
        background: #FBE5E2 !important;
        border-color: #B83227 !important;
        color: #B83227 !important;
      }
      /* The .pf-del-anchor wrapper itself collapses to zero height
         so it doesn't push the layout. */
      .pf-del-anchor { display: none; }

      /* ── Tighter inter-card gap (the wrapping st.container has its
            own gap; pull it down a bit so cards don't feel sparse) ─── */
      div[data-testid="stVerticalBlock"] > div[data-testid="element-container"]
        + div[data-testid="element-container"] {
        margin-top: 6px;
      }

      /* ── Detail panel internal flex strips (sector / rec, low / high) ── */
      .pf-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 8px 16px;
        align-items: baseline;
        justify-content: space-between;
        margin: 4px 0 8px 0;
      }
      .pf-strip-meta { color: #888; font-size: 13px; min-width: 0; }
      .pf-strip-rec  { font-size: 14px; font-weight: 600; white-space: nowrap; }
      .pf-strip-rec .lbl { color: #888; font-size: 12px; font-weight: 400;
                           margin-right: 4px; }

      .pf-banner {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        justify-content: space-between;
        gap: 4px 12px;
        margin: 8px 0 4px 0;
        padding: 8px 12px;
        background: #F4F8FC;
        border-radius: 6px;
      }
      .pf-banner-l { display: flex; flex-wrap: wrap; align-items: baseline;
                     gap: 4px 10px; min-width: 0; }
      .pf-banner-period { color: #666; font-size: 13px; }
      .pf-banner-pct    { font-weight: 700; font-size: 22px; line-height: 1.1; }
      .pf-banner-abs    { font-weight: 600; font-size: 15px; }
      .pf-banner-r      { color: #888; font-size: 12px; white-space: nowrap; }

      .pf-lowhigh {
        display: flex;
        flex-wrap: wrap;
        gap: 4px 24px;
        color: #666;
        font-size: 13px;
        margin-top: 4px;
      }
      .pf-lowhigh b { color: #333; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Portfolio rendering ───────────────────────────────────────────────────────
if not st.session_state.portfolio_tickers:
    st.info(
        "Your portfolio is empty. Add a ticker above to get started.\n\n"
        "Examples: `AAPL`, `MSFT`, `RHM.DE`, `^GSPC` (S&P 500), "
        "`SPY` (ETF), `EURUSD=X` (forex)."
    )
else:
    import html as _html

    def _metric_html(label: str, value: str, *,
                     color: str = "#222", bold: bool = False,
                     muted: bool = False) -> str:
        cls = "pf-metric-value"
        if bold:  cls += " bold"
        if muted: cls += " muted"
        style = f"color:{color};" if color != "#222" else ""
        return (
            f"<div class='pf-metric'>"
            f"<div class='pf-metric-label'>{label}</div>"
            f"<div class='{cls}' style='{style}'>{value}</div>"
            f"</div>"
        )

    for ticker in list(st.session_state.portfolio_tickers):
        snap = _fetch_snapshot(ticker)
        rec_label, rec_color = _recommendation(snap)
        is_expanded = ticker in st.session_state.portfolio_expanded
        ytd_text, ytd_color = _fmt_signed_pct(snap.get("ytd_pct"))
        next_earnings = _fetch_next_earnings(ticker)

        # Name + ticker + (sector if available) at left
        name_full = snap["name"] or ticker
        sector = snap.get("sector") or ""
        sub_bits = [ticker] + ([sector] if sector else [])
        sub_line = " · ".join(sub_bits)

        # Earnings text (slightly muted when no date scheduled)
        if next_earnings:
            earn_value = f"📅 {next_earnings}"
            earn_kw = {}
        else:
            earn_value = "—"
            earn_kw = {"muted": True}

        # ── Card row: main HTML block + action button column ─────────────
        # On desktop the column ratio holds; on mobile (<640px) Streamlit
        # wraps the second column below the first, so the buttons end up
        # under the card content as a small horizontal pair.
        main_col, btn_col = st.columns([1, 0.14], gap="small")

        with main_col:
            card_html = (
                "<div class='pf-card'>"
                "<div class='pf-summary'>"
                # Name cell
                f"<div class='pf-name-cell'>"
                f"<div class='pf-name' title='{_html.escape(name_full)}'>"
                f"{_html.escape(name_full)}</div>"
                f"<div class='pf-sub'>{_html.escape(sub_line)}</div>"
                "</div>"
                # 7 metrics
                f"{_metric_html('Earnings', earn_value, **earn_kw)}"
                f"{_metric_html('Price', _fmt_price(snap['price'], snap['currency']))}"
                f"{_metric_html('Mkt Cap', _fmt_money(snap['market_cap']))}"
                f"{_metric_html('P/E', _fmt_ratio(snap['pe']))}"
                f"{_metric_html('ROE', _fmt_pct(snap['roe']))}"
                f"{_metric_html('EBIT M.', _fmt_pct(snap['ebit_margin']))}"
                f"{_metric_html('YTD', ytd_text, color=ytd_color, bold=True)}"
                "</div></div>"
            )
            st.markdown(card_html, unsafe_allow_html=True)

        with btn_col:
            # Top-right always has only the ▾/▴ toggle. The destructive
            # "✕ Remove from portfolio" button is always rendered with
            # its full label at the bottom-right (below the card when
            # collapsed; at the bottom of the detail panel when expanded)
            # so it's far from the toggle in both states.
            arrow = "▴" if is_expanded else "▾"
            if st.button(arrow, key=f"toggle_{ticker}",
                         help="Collapse" if is_expanded else "Expand",
                         use_container_width=True):
                if is_expanded:
                    st.session_state.portfolio_expanded.discard(ticker)
                else:
                    st.session_state.portfolio_expanded.add(ticker)
                st.rerun()

        # ── Labelled remove button (only rendered here when card is
        #     collapsed; when expanded it lives at the bottom of the
        #     detail panel further down). Right-aligned via empty
        #     left column so it sits under the action column above. ──
        if not is_expanded:
            _, rem_col_top = st.columns([0.6, 0.4])
            with rem_col_top:
                st.markdown("<div class='pf-del-anchor'></div>",
                            unsafe_allow_html=True)
                if st.button("✕ Remove from portfolio",
                             key=f"del_top_{ticker}",
                             help="Permanently remove this holding",
                             use_container_width=True):
                    st.session_state.portfolio_tickers.remove(ticker)
                    st.session_state.portfolio_expanded.discard(ticker)
                    _save_portfolio(st.session_state.portfolio_tickers)
                    st.rerun()

        # ── Expanded detail section ──────────────────────────────────────────
        if is_expanded:
            with st.container(border=True):
                # Top strip — sector + recommendation badge (single flex row,
                # wraps onto two lines on narrow viewports).
                sector_label = snap.get("sector") or ""
                meta_html = (
                    _html.escape(ticker)
                    + (f"  ·  {_html.escape(sector_label)}" if sector_label else "")
                )
                st.markdown(
                    "<div class='pf-strip'>"
                    f"<div class='pf-strip-meta'>{meta_html}</div>"
                    f"<div class='pf-strip-rec'>"
                    f"<span class='lbl'>Rec:</span>"
                    f"<span style='color:{rec_color};'>{rec_label}</span>"
                    f"</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )

                # Period selector
                current_period = st.session_state.portfolio_periods.get(ticker, DEFAULT_PERIOD)
                if current_period not in PERIODS:
                    current_period = DEFAULT_PERIOD
                sel_period = st.radio(
                    "Chart period",
                    options=PERIODS,
                    index=PERIODS.index(current_period),
                    horizontal=True,
                    label_visibility="collapsed",
                    key=f"period_{ticker}",
                )
                if sel_period != current_period:
                    st.session_state.portfolio_periods[ticker] = sel_period

                # Chart — use Altair so the Y axis fits the price range
                # (st.line_chart anchors Y at 0, which makes intraday charts
                # look like a flat line).
                hist = _fetch_history(ticker, sel_period)
                if hist is None or hist.empty:
                    st.warning(f"No EODHD price history available for **{sel_period}**.")
                else:
                    try:
                        df_chart = hist.reset_index()
                        time_col = df_chart.columns[0]   # "Date" or "Time"
                        low_p  = float(df_chart["Close"].min())
                        high_p = float(df_chart["Close"].max())
                        first_px = float(df_chart["Close"].iloc[0])
                        last_px  = float(df_chart["Close"].iloc[-1])
                        chg_pct  = (last_px / first_px - 1) * 100 if first_px else 0
                        abs_chg  = last_px - first_px

                        # Add a small padding around the range so the line
                        # doesn't hug the chart edges.
                        span = max(high_p - low_p, abs(low_p) * 0.001)
                        pad  = span * 0.08
                        y_min = low_p - pad
                        y_max = high_p + pad

                        # Line + heading colour: green if up, red if down
                        line_color = "#1A7E3D" if chg_pct >= 0 else "#B83227"
                        arrow      = "▲" if chg_pct >= 0 else "▼"

                        # ── Prominent period-change banner above the chart ──
                        # Flex with wrap so on narrow viewports the
                        # first→last value drops onto a second line
                        # beneath the % change.
                        ccy = snap.get("currency") or ""
                        st.markdown(
                            f"<div class='pf-banner' "
                            f"style='border-left:4px solid {line_color};'>"
                            f"<div class='pf-banner-l'>"
                            f"<span class='pf-banner-period'>"
                            f"{sel_period} change</span>"
                            f"<span class='pf-banner-pct' "
                            f"style='color:{line_color};'>"
                            f"{arrow} {chg_pct:+.2f}%</span>"
                            f"<span class='pf-banner-abs' "
                            f"style='color:{line_color};'>"
                            f"({abs_chg:+,.2f} {ccy})</span>"
                            f"</div>"
                            f"<div class='pf-banner-r'>"
                            f"{first_px:,.2f} → {last_px:,.2f}"
                            f"</div>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

                        x_type = "T"   # temporal works for both Date + Time
                        chart = (
                            alt.Chart(df_chart)
                               .mark_line(strokeWidth=2)
                               .encode(
                                   x=alt.X(f"{time_col}:{x_type}", title=""),
                                   y=alt.Y(
                                       "Close:Q",
                                       title="",
                                       scale=alt.Scale(
                                           domain=[y_min, y_max],
                                           zero=False,
                                           nice=False,
                                       ),
                                   ),
                                   tooltip=[
                                       alt.Tooltip(f"{time_col}:{x_type}",
                                                   title="Time"),
                                       alt.Tooltip("Close:Q",
                                                   title="Price",
                                                   format=",.2f"),
                                   ],
                                   color=alt.value(line_color),
                               )
                               .properties(height=280)
                               .configure_view(strokeWidth=0)
                        )
                        st.altair_chart(chart, use_container_width=True)

                        st.markdown(
                            f"<div class='pf-lowhigh'>"
                            f"<span><b>{sel_period} low:</b> {low_p:,.2f}</span>"
                            f"<span><b>{sel_period} high:</b> {high_p:,.2f}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                    except Exception as e:
                        st.warning(f"Chart rendering failed: {e}")

                # News
                with st.expander("📰 Latest news", expanded=False):
                    news_items = _fetch_news(ticker, limit=15)
                    if not news_items:
                        st.warning("No EODHD news available for this ticker.")
                    else:
                        for n in news_items:
                            title   = n.get("title")   or "(no title)"
                            link    = n.get("link")    or ""
                            date    = n.get("date")    or ""
                            content = n.get("content") or ""
                            date_short = str(date)[:16].replace("T", " ")

                            sent = n.get("sentiment") or {}
                            polarity = sent.get("polarity") if isinstance(sent, dict) else None
                            if polarity is None:
                                sent_badge = ""
                            elif polarity >= 0.15:
                                sent_badge = (
                                    f" <span style='background:#E0F2E5;color:#1A7E3D;"
                                    f"padding:1px 6px;border-radius:8px;font-size:11px;"
                                    f"font-weight:600;'>+ {polarity:.2f}</span>"
                                )
                            elif polarity <= -0.15:
                                sent_badge = (
                                    f" <span style='background:#FBE5E2;color:#B83227;"
                                    f"padding:1px 6px;border-radius:8px;font-size:11px;"
                                    f"font-weight:600;'>− {polarity:.2f}</span>"
                                )
                            else:
                                sent_badge = (
                                    f" <span style='background:#F0F0F0;color:#666;"
                                    f"padding:1px 6px;border-radius:8px;font-size:11px;'>"
                                    f"~ {polarity:.2f}</span>"
                                )

                            title_html = (
                                f"<a href='{link}' target='_blank' "
                                f"style='color:#1B3F6E;font-weight:600;text-decoration:none;'>"
                                f"{title}</a>"
                                if link else
                                f"<span style='color:#1B3F6E;font-weight:600;'>{title}</span>"
                            )
                            st.markdown(
                                f"<div style='margin-bottom:4px;'>"
                                f"<span style='color:#888;font-size:12px;'>{date_short}</span>"
                                f"{sent_badge}<br>{title_html}"
                                f"</div>",
                                unsafe_allow_html=True,
                            )
                            if content:
                                snippet = content.strip().replace("\n", " ")
                                if len(snippet) > 300:
                                    snippet = snippet[:300].rstrip() + "…"
                                st.markdown(
                                    f"<div style='color:#444;font-size:13px;"
                                    f"margin-bottom:12px;line-height:1.4;'>{snippet}</div>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                st.markdown("<div style='margin-bottom:12px;'></div>",
                                            unsafe_allow_html=True)

                # ── Bottom-right "Remove from portfolio" button ──────────────
                # Sits at the very bottom of the expanded panel, far away
                # from the ▴ collapse toggle, so the destructive ✕ action
                # is unambiguous.
                _, rem_col = st.columns([0.6, 0.4])
                with rem_col:
                    # Anchor sibling so CSS paints the next button red.
                    st.markdown("<div class='pf-del-anchor'></div>",
                                unsafe_allow_html=True)
                    if st.button("✕ Remove from portfolio",
                                 key=f"del_bottom_{ticker}",
                                 help="Permanently remove this holding",
                                 use_container_width=True):
                        st.session_state.portfolio_tickers.remove(ticker)
                        st.session_state.portfolio_expanded.discard(ticker)
                        _save_portfolio(st.session_state.portfolio_tickers)
                        st.rerun()

        # Small vertical gap between cards (the card itself now carries
        # the visual separation via its border + rounded corners).
        st.markdown("<div style='height:6px;'></div>",
                    unsafe_allow_html=True)

st.markdown("&nbsp;", unsafe_allow_html=True)
# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(
    "<div style='margin-top:24px;padding-top:8px;"
    "border-top:1px solid #E0E5EC;color:#888;font-size:12px;"
    "line-height:1.5;text-align:center;'>"
    "Personal watchlist powered by <b>EODHD only</b>. Add any ticker — "
    "stocks (AAPL, RHM.DE), indices (^GSPC, ^DJI), ETFs (SPY) or forex "
    "(EURUSD=X). Cards are collapsed by default — click ▾ to expand."
    "<br><span style='font-size:11px;color:#999;'>"
    "Snapshot cached 15 min · History 30 min · News 30 min · "
    "Earnings dates 6 h · Recommendation is a rule-based heuristic on "
    "P/E + ROE + EBIT margin — not investment advice."
    "</span></div>",
    unsafe_allow_html=True,
)
