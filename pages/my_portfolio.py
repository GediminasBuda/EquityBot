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
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Any

import altair as alt
import pandas as pd
import requests
import streamlit as st
from streamlit_searchbox import st_searchbox

import config as _config
from config import REQUEST_HEADERS
# Read EODHD_API_KEY at runtime (not module-level) so secrets injected by
# app.py are always visible, even if config was imported early by Streamlit.
EODHD_API_KEY = os.environ.get("EODHD_API_KEY", "") or _config.EODHD_API_KEY
from data_sources.eodhd_adapter import _YF_TO_EODHD

# Reverse mapping: EODHD exchange code → Yahoo Finance suffix.
# Built once from _YF_TO_EODHD. Some collisions are inevitable (e.g. both
# ".VX" and ".SW" map to EODHD ".SW") — the last entry wins, which for our
# use case (showing results to the user) is fine.
_EODHD_TO_YF = {v: k for k, v in _YF_TO_EODHD.items()}

# ── Storage ───────────────────────────────────────────────────────────────────
# Streamlit Cloud's container filesystem is ephemeral — the local
# data/portfolio.json gets wiped every time the infra restarts the
# container (typically once a day or on deploy / wake-from-sleep).
# To keep the portfolio across restarts we mirror it to a private
# GitHub Gist via the REST API; the local file stays as a fast
# in-session cache / dev fallback.
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
_PORTFOLIO_FILE = _DATA_DIR / "portfolio.json"

EODHD_BASE = "https://eodhistoricaldata.com/api"

_GIST_DESC      = "EquityBot Portfolio (do not delete)"
_GIST_FILENAME  = "portfolio.json"
_GIST_API       = "https://api.github.com"
_GIST_TIMEOUT   = 15


def _gist_token() -> Optional[str]:
    """Return a Classic PAT with the `gist` scope, or None if unset."""
    try:
        if "GITHUB_GIST_TOKEN" in st.secrets:
            return str(st.secrets["GITHUB_GIST_TOKEN"]) or None
    except Exception:
        pass
    return os.environ.get("GITHUB_GIST_TOKEN") or None


def _gist_headers(token: str) -> dict:
    return {
        "Authorization":          f"Bearer {token}",
        "Accept":                 "application/vnd.github+json",
        "X-GitHub-Api-Version":   "2022-11-28",
        "User-Agent":             "EquityBot-Portfolio/1.0",
    }


def _portfolio_gist_id(token: str) -> Optional[str]:
    """
    Resolve the portfolio Gist's ID. On first call per session we scan
    the user's gists for one whose description == _GIST_DESC; if none
    exists we create a new private gist and cache its ID in
    st.session_state._pf_gist_id.
    """
    cached = st.session_state.get("_pf_gist_id")
    if cached:
        return cached

    # 1) Look up an existing portfolio gist (max 100 personal gists,
    # which is enough for any normal user).
    try:
        r = requests.get(
            f"{_GIST_API}/gists",
            headers=_gist_headers(token),
            params={"per_page": 100},
            timeout=_GIST_TIMEOUT,
        )
        if r.status_code == 200:
            for g in r.json():
                if g.get("description") == _GIST_DESC:
                    st.session_state._pf_gist_id = g["id"]
                    return g["id"]
        else:
            # Common: 401 (invalid token) / 403 (wrong scope: fine-
            # grained PATs cannot access /gists, only Classic PATs with
            # the `gist` scope can). Fall through to local-only mode.
            return None
    except Exception:
        return None

    # 2) Not found — create a fresh private gist seeded with empty list.
    try:
        r = requests.post(
            f"{_GIST_API}/gists",
            headers=_gist_headers(token),
            json={
                "description": _GIST_DESC,
                "public":      False,
                "files": {
                    _GIST_FILENAME: {
                        "content": json.dumps({"tickers": []}, indent=2),
                    },
                },
            },
            timeout=_GIST_TIMEOUT,
        )
        if r.status_code == 201:
            gist_id = r.json()["id"]
            st.session_state._pf_gist_id = gist_id
            return gist_id
    except Exception:
        pass

    return None


def _load_portfolio() -> list[str]:
    """
    Load portfolio tickers. Tries the GitHub Gist first (the
    persistent source of truth across container restarts), then falls
    back to the local file if the Gist is unreachable / unauthorised.
    """
    token = _gist_token()
    if token:
        gist_id = _portfolio_gist_id(token)
        if gist_id:
            try:
                r = requests.get(
                    f"{_GIST_API}/gists/{gist_id}",
                    headers=_gist_headers(token),
                    timeout=_GIST_TIMEOUT,
                )
                if r.status_code == 200:
                    files = r.json().get("files") or {}
                    f = files.get(_GIST_FILENAME) or {}
                    content = f.get("content") or ""
                    if content:
                        try:
                            data = json.loads(content)
                            tickers = list(data.get("tickers", []))
                            # Mirror to local file so dev / offline reads work.
                            try:
                                _PORTFOLIO_FILE.write_text(
                                    json.dumps({"tickers": tickers}, indent=2),
                                    encoding="utf-8",
                                )
                            except Exception:
                                pass
                            return tickers
                        except Exception:
                            pass
            except Exception:
                pass

    # Fallback: local file (works in dev mode without a token,
    # and as a last resort if the Gist API is unreachable).
    if not _PORTFOLIO_FILE.exists():
        return []
    try:
        raw = json.loads(_PORTFOLIO_FILE.read_text(encoding="utf-8"))
        return list(raw.get("tickers", []))
    except Exception:
        return []


def _save_portfolio(tickers: list[str]) -> None:
    """Persist the portfolio to the GitHub Gist (primary, durable)
    and mirror to the local file (in-session cache / dev fallback)."""
    payload_json = json.dumps({"tickers": tickers}, indent=2)

    # 1) Local file — always write so dev / fallback path stays warm.
    try:
        _PORTFOLIO_FILE.write_text(payload_json, encoding="utf-8")
    except Exception:
        pass

    # 2) Gist — the only copy that survives a Streamlit Cloud restart.
    token = _gist_token()
    if not token:
        return
    gist_id = _portfolio_gist_id(token)
    if not gist_id:
        return
    try:
        requests.patch(
            f"{_GIST_API}/gists/{gist_id}",
            headers=_gist_headers(token),
            json={
                "files": {
                    _GIST_FILENAME: {"content": payload_json},
                },
            },
            timeout=_GIST_TIMEOUT,
        )
    except Exception:
        pass


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


# ── yfinance helpers for Japan / TSE stocks ───────────────────────────────────
# EODHD has no coverage for the Tokyo Stock Exchange. These helpers provide
# the same data shapes that the EODHD-based functions return, so the rest of
# the page works unchanged.

def _jp_correct_name(yf_ticker: str, raw_name: str) -> str:
    """Fix stale/wrong company name yfinance sometimes returns for new Japanese listings."""
    suspicious = (
        not raw_name
        or raw_name == yf_ticker
        or "godo kaisha" in raw_name.lower()
    )
    if not suspicious:
        return raw_name
    try:
        import re as _rjp
        from data_sources.japan_tickers import search_japan
        code = yf_ticker.upper().replace(".T", "")
        hits = search_japan(code, max_results=1)
        if hits:
            m = _rjp.search(r'\.T\s+(.+?)\s*(?:\(|\·|$)', hits[0][0])
            if m:
                corrected = m.group(1).strip()
                if corrected and len(corrected) > 3:
                    return corrected
    except Exception:
        pass
    return raw_name or yf_ticker


def _snapshot_yf(yf_ticker: str) -> dict:
    """yfinance snapshot for a ticker not covered by EODHD (Japan/TSE, Baltic)."""
    _empty = {
        "eodhd_ticker": yf_ticker, "name": yf_ticker,
        "currency": "JPY", "sector": "",
        "price": None, "market_cap": None,
        "pe": None, "roe": None, "ebit_margin": None, "ytd_pct": None,
    }
    try:
        import yfinance as _yf_pf
        t    = _yf_pf.Ticker(yf_ticker)
        info = t.info or {}

        price = (_to_float(info.get("currentPrice"))
                 or _to_float(info.get("regularMarketPrice"))
                 or _to_float(info.get("previousClose")))
        name     = _jp_correct_name(yf_ticker,
                       info.get("longName") or info.get("shortName") or "")
        sector   = info.get("sector") or ""
        currency = info.get("currency") or "JPY"
        mkt_cap  = _to_float(info.get("marketCap"))
        pe       = _to_float(info.get("trailingPE") or info.get("forwardPE"))
        roe      = _to_float(info.get("returnOnEquity"))      # decimal
        ebit_mg  = _to_float(info.get("operatingMargins"))    # decimal

        ytd_pct: Optional[float] = None
        if price:
            try:
                hist = t.history(period="ytd")
                if not hist.empty:
                    first = float(hist["Close"].iloc[0])
                    if first > 0:
                        ytd_pct = price / first - 1
            except Exception:
                pass

        week_52_high = _to_float(info.get("fiftyTwoWeekHigh"))
        week_52_low  = _to_float(info.get("fiftyTwoWeekLow"))
        forward_pe   = _to_float(info.get("forwardPE"))
        q_rev_growth = _to_float(info.get("revenueGrowth"))

        return {
            "eodhd_ticker": yf_ticker,
            "name": name, "currency": currency, "sector": sector,
            "price": price, "market_cap": mkt_cap,
            "pe": pe, "roe": roe, "ebit_margin": ebit_mg, "ytd_pct": ytd_pct,
            "week_52_high": week_52_high, "week_52_low": week_52_low,
            "forward_pe": forward_pe, "q_rev_growth": q_rev_growth,
        }
    except Exception:
        return _empty


def _history_yf(yf_ticker: str, period: str) -> Optional[pd.DataFrame]:
    """yfinance price history for a Japanese (TSE) ticker."""
    _YF_MAP = {
        "1d": ("1d",  "5m"),
        "1y": ("1y",  "1d"),
        "YTD": ("ytd", "1d"),
        "5y": ("5y",  "1wk"),
        "All": ("max", "1mo"),
    }
    try:
        import yfinance as _yf_pf
        yf_p, yf_i = _YF_MAP.get(period, ("1y", "1d"))
        df = _yf_pf.Ticker(yf_ticker).history(period=yf_p, interval=yf_i)
        if df.empty:
            return None
        df = df[["Close"]].copy()
        df.index = pd.to_datetime(df.index)
        if period == "1d":
            df.index.name = "Time"
        else:
            df.index = df.index.normalize()
            df.index.name = "Date"
        df = df.dropna(subset=["Close"])
        return df if not df.empty else None
    except Exception:
        return None


def _next_earnings_yf(yf_ticker: str) -> Optional[str]:
    """Next earnings date for a Japanese (TSE) ticker via yfinance."""
    try:
        import yfinance as _yf_pf
        cal = _yf_pf.Ticker(yf_ticker).calendar
        if cal:
            ed = cal.get("Earnings Date")
            if ed:
                ed = ed[0] if isinstance(ed, list) else ed
                return str(ed)[:10]
    except Exception:
        pass
    return None


# ── Snapshot (cached) ─────────────────────────────────────────────────────────
@st.cache_data(ttl=900, show_spinner=False)   # 15-minute cache
def _fetch_snapshot(yf_ticker: str) -> dict:
    """
    All snapshot metrics for one ticker from EODHD (or yfinance for Japan), including YTD%.
    """
    # Japan / TSE and Baltic: EODHD has no reliable coverage → use yfinance
    if yf_ticker.upper().endswith((".T", ".VS", ".TL", ".RG")):
        return _snapshot_yf(yf_ticker)

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

    technicals   = fund.get("Technicals") or {}
    week_52_high = _to_float(technicals.get("52WeekHigh"))
    week_52_low  = _to_float(technicals.get("52WeekLow"))
    valuation    = fund.get("Valuation") or {}
    forward_pe   = _to_float(valuation.get("ForwardPE")) or _to_float(highlights.get("ForwardPE"))
    q_rev_growth = _to_float(highlights.get("QuarterlyRevenueGrowthYOY"))

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
        "week_52_high": week_52_high,
        "week_52_low":  week_52_low,
        "forward_pe":   forward_pe,
        "q_rev_growth": q_rev_growth,
    }


# ── History (cached, period-aware) ────────────────────────────────────────────
PERIODS = ["1d", "YTD", "1y", "5y", "All"]
DEFAULT_PERIOD = "5y"


def _period_range(period: str) -> tuple[Optional[datetime.date], datetime.date]:
    """Return (from_date, to_date). from_date=None means 'as far back as possible'."""
    today = datetime.utcnow().date()
    if period == "1d":
        # Last 5 trading days — we'll filter to 1 day's worth in fetch
        return (today - timedelta(days=7), today)
    if period == "1y":
        return (today - timedelta(days=365 + 3), today)
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
    Japanese (TSE) tickers use yfinance history.
    """
    # Japan / TSE and Baltic: use yfinance
    if yf_ticker.upper().endswith((".T", ".VS", ".TL", ".RG")):
        return _history_yf(yf_ticker, period)

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
    forex and ETFs return None. Japanese (TSE) tickers use yfinance calendar.
    """
    # Japan / TSE and Baltic: use yfinance
    if yf_ticker.upper().endswith((".T", ".VS", ".TL", ".RG")):
        return _next_earnings_yf(yf_ticker)

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
    # Japan / Baltic: EODHD has no news → use yfinance
    if yf_ticker.upper().endswith((".T", ".VS", ".TL", ".RG")):
        try:
            import yfinance as _yf_news
            raw = _yf_news.Ticker(yf_ticker).news or []
            out = []
            for item in raw[:limit]:
                content = item.get("content") or {}
                title = content.get("title") or item.get("title") or ""
                url   = (content.get("canonicalUrl") or {}).get("url") or item.get("link") or ""
                date  = content.get("pubDate") or item.get("providerPublishTime") or ""
                if isinstance(date, int):
                    from datetime import timezone
                    date = datetime.fromtimestamp(date, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                out.append({"title": title, "link": url, "date": date, "text": ""})
            return out
        except Exception:
            return []

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
    # Bloomberg palette: bullish blue, bearish red, neutral amber.
    if avg >= 1.0:  return "BUY",  "#4D9FFF"
    if avg <= -0.5: return "SELL", "#FF3030"
    return "HOLD", "#FFA028"


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
    """Return (text, color) — blue (bullish) / red (bearish) Bloomberg style."""
    if v is None:
        return "—", "#8a6a30"
    try:
        pct = float(v) * 100
    except Exception:
        return "—", "#8a6a30"
    color = "#4D9FFF" if pct >= 0 else "#FF3030"
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

    Search layers:
      1. EODHD /search — covers most global exchanges.
      2. Japan / TSE (3 sub-layers):
         a. Pattern match: 3-4 digit input → instant TSE suggestion.
         b. Local Japan seed list (~220 major + EODHD exchange-symbol-list cache).
         c. yfinance Search fallback for new / small Japanese stocks.
    """
    import re as _re_pf
    q = (query or "").strip()
    if not q:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    # ── Layer 1: EODHD global search ─────────────────────────────────────────
    for item in _search_eodhd_raw(q):
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
        meta_bits = [b for b in (exch, country, ttype) if b]
        meta = " · ".join(meta_bits)
        label = f"{yf_ticker:<14}  {name[:48]}"
        if meta:
            label += f"  ({meta})"
        out.append((label, yf_ticker))

    # ── Layer 2a: Japan pattern match (3-4 digit code → instant TSE) ─────────
    _jp_direct = _re_pf.match(r'^(\d{3,4})(\.T)?$', q.strip().upper())
    if _jp_direct:
        _jp_code   = _jp_direct.group(1).zfill(4)
        _jp_ticker = f"{_jp_code}.T"
        if _jp_ticker not in seen:
            seen.add(_jp_ticker)
            try:
                from data_sources.japan_tickers import get_japan_tickers
                _jp_name = next(
                    (item.get("name", "") for item in get_japan_tickers(EODHD_API_KEY or "")
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
                f"🇯🇵 {_jp_ticker}  · TSE (enter to add)"
            )
            out.insert(0, (_jp_label, _jp_ticker))

    # ── Layer 2b: Japan seed / cache search ──────────────────────────────────
    try:
        from data_sources.japan_tickers import search_japan
        _jp_slots = max(0, 12 - len(out))
        if _jp_slots > 0:
            for _jp_label, _jp_ticker in search_japan(
                q, api_key=EODHD_API_KEY or "", max_results=_jp_slots
            ):
                if _jp_ticker not in seen:
                    seen.add(_jp_ticker)
                    out.append((f"🇯🇵 {_jp_label}", _jp_ticker))
    except Exception:
        pass

    # ── Layer 2c: yfinance search for unlisted / new Japanese stocks ──────────
    if len(q) >= 3 and len(out) < 10:
        try:
            import yfinance as _yf_pf
            _yf_res = _yf_pf.Search(q + " japan", max_results=6, news_count=0)
            for _yf_item in (_yf_res.quotes or []):
                _sym = (_yf_item.get("symbol") or "").strip().upper()
                if not _sym.endswith(".T") or _sym in seen:
                    continue
                seen.add(_sym)
                _yf_name = (
                    _yf_item.get("shortname")
                    or _yf_item.get("longname")
                    or ""
                )
                out.append((
                    f"🇯🇵 {_sym:<10}  {_yf_name[:50]}  · TSE",
                    _sym,
                ))
                if len(out) >= 12:
                    break
        except Exception:
            pass

    # ── Baltic direct-entry ───────────────────────────────────────────────────
    _BALTIC_PF = {"VS": ("🇱🇹", "NASDAQ Vilnius"), "TL": ("🇪🇪", "NASDAQ Tallinn"), "RG": ("🇱🇻", "NASDAQ Riga")}
    _BALTIC_ALIASES_PF = {"VSE": "VS", "VLN": "VS", "TAL": "TL", "RIG": "RG"}
    _q_raw = q.strip()
    _q_norm = _re_pf.sub(r':([A-Z]+)$', r'.\1', _q_raw.upper())
    _is_ticker_query = bool(_q_raw == _q_raw.upper() and " " not in _q_raw)
    if _is_ticker_query:
        _baltic_match = _re_pf.match(r'^([A-Z0-9]{2,6})(\.(?:VS|TL|RG|VSE|VLN|TAL|RIG))?$', _q_norm)
        if _baltic_match:
            _b_code = _baltic_match.group(1)
            _b_raw_sfx = (_baltic_match.group(2) or "").lstrip(".")
            _b_exch = _BALTIC_ALIASES_PF.get(_b_raw_sfx, _b_raw_sfx)
            if _b_exch and _b_exch in _BALTIC_PF:
                _b_flag, _b_name = _BALTIC_PF[_b_exch]
                _b_ticker = f"{_b_code}.{_b_exch}"
                if _b_ticker not in seen:
                    seen.add(_b_ticker)
                    out.insert(0, (f"{_b_flag} {_b_ticker}  · {_b_name} (enter to add)", _b_ticker))
            elif any(c.isalpha() for c in _b_code):
                for _b_sfx, (_b_flag, _b_name) in _BALTIC_PF.items():
                    _b_ticker = f"{_b_code}.{_b_sfx}"
                    if _b_ticker not in seen and len(out) < 12:
                        seen.add(_b_ticker)
                        out.append((f"{_b_flag} {_b_ticker}  · {_b_name} (enter to add)", _b_ticker))

    # ── Baltic name search ────────────────────────────────────────────────────
    try:
        from data_sources.baltic_tickers import search_baltic
        _bl_slots = max(0, 12 - len(out))
        if _bl_slots > 0:
            for _bl_label, _bl_ticker in search_baltic(q, api_key=EODHD_API_KEY or "", max_results=_bl_slots):
                if _bl_ticker not in seen:
                    seen.add(_bl_ticker)
                    out.append((_bl_label, _bl_ticker))
    except Exception:
        pass

    return out[:12]


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
    # ── Collapse the JS-injection iframe wrappers so they don't add
    # an empty row between the searchbox and the first portfolio
    # card. Hidden via display:none on both wrappers — the iframe's
    # script still runs (display:none does not stop iframe scripts
    # in Chrome/Firefox/Safari). Applies on every viewport because
    # the user wants the styling consistent. */
    ".pf-style-iframe-anchor { display: none; }"
    "div[data-testid=\"stElementContainer\"]:has(.pf-style-iframe-anchor),"
    "div[data-testid=\"element-container\"]:has(.pf-style-iframe-anchor),"
    "div.stElementContainer:has(.pf-style-iframe-anchor),"
    "div.element-container:has(.pf-style-iframe-anchor),"
    "div[data-testid=\"stElementContainer\"]:has(.pf-style-iframe-anchor) + div[data-testid=\"stElementContainer\"],"
    "div[data-testid=\"element-container\"]:has(.pf-style-iframe-anchor) + div[data-testid=\"element-container\"],"
    "div.stElementContainer:has(.pf-style-iframe-anchor) + div.stElementContainer,"
    "div.element-container:has(.pf-style-iframe-anchor) + div.element-container {"
    "  display: none !important;"
    "}"
    "</style>",
    unsafe_allow_html=True,
)
st.markdown(
    "<div class='eq-page-title' "
    "style='display:flex;align-items:center;gap:8px;margin:0;'>"
    "<span style='font-size:20px;'>📁</span>"
    "<span style='font-size:16px;font-weight:700;color:#FFA028;"
    "font-family:monospace;letter-spacing:1px;text-transform:uppercase;'>"
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
st.markdown("<div style='margin-top:18px'></div>", unsafe_allow_html=True)
selected_ticker = st_searchbox(
    search_function=_ticker_search,
    placeholder="",
    label=None,
    clear_on_submit=True,
    key="ticker_searchbox",
)

# ── Inject red styling into this page's streamlit_searchbox iframes ────
# Same approach as the Report Generator: a parent-page <script> reaches
# every searchbox iframe (same-origin) and appends a <style> block.
# Repaints every 800ms in case Streamlit re-creates the iframe on a
# rerender. The anchor div lets CSS collapse the iframe wrapper's
# default padding so it doesn't add an empty gap below the searchbox.
st.markdown("<div class='pf-style-iframe-anchor'></div>",
            unsafe_allow_html=True)
st.iframe(
    """
    <script>
    (function () {
      const STYLE_ID = 'eqbot-searchbox-red';
      const CSS = `
        input, .css-1d391kg input, [class*="control"] input {
          background-color: #000000 !important;
          color: #FF3030 !important;
          caret-color: #FF3030 !important;
          -webkit-text-fill-color: #FF3030 !important;
          font-family: monospace !important;
        }
        [class*="control"], [class*="-control"] {
          background-color: #000000 !important;
          border: 1px solid #FF3030 !important;
          border-radius: 0 !important;
          box-shadow: none !important;
          min-height: 38px !important;
        }
        [class*="control"]:hover, [class*="-control"]:hover,
        [class*="control--is-focused"], [class*="-control--is-focused"] {
          border-color: #FF3030 !important;
          box-shadow: 0 0 0 1px #FF3030 !important;
        }
        [class*="placeholder"] { color: transparent !important; }
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
          border: 1px solid #FF3030 !important;
          border-radius: 0 !important;
        }
        [class*="option"] {
          background-color: #000000 !important;
          color: #FFA028 !important;
          font-family: monospace !important;
        }
        [class*="option--is-focused"], [class*="option"]:hover {
          background-color: #1a0606 !important;
          color: #FF3030 !important;
        }
        [class*="singleValue"] { color: #FF3030 !important; }
        body { background-color: #000000 !important; }
      `;

      function paint(doc) {
        try {
          if (!doc) return;
          if (doc.getElementById(STYLE_ID)) return;
          const s = doc.createElement('style');
          s.id = STYLE_ID;
          s.textContent = CSS;
          doc.head.appendChild(s);
        } catch (e) { /* same-origin race / not ready */ }
      }

      function scan() {
        const parent = window.parent || window;
        const iframes = parent.document.querySelectorAll('iframe');
        iframes.forEach(f => {
          const title = (f.title || '').toLowerCase();
          const src   = (f.src   || '').toLowerCase();
          if (title.includes('searchbox') || src.includes('searchbox')) {
            try { paint(f.contentDocument); } catch (e) {}
          }
        });
      }

      scan();
      setInterval(scan, 800);
    })();
    </script>
    """,
    height=1,
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
top_l, top_r = st.columns([6, 1])
with top_l:
    if st.session_state.portfolio_tickers:
        st.markdown(
            f"**{len(st.session_state.portfolio_tickers)}** ticker"
            f"{'s' if len(st.session_state.portfolio_tickers) != 1 else ''} tracked"
        )
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
      /* ── Card container (Bloomberg terminal) ─────────────────────── */
      .pf-card {
        background: #000000;
        border: 1px solid #2a1f10;
        border-radius: 2px;
        padding: 6px 12px 11px;
        position: relative;
        z-index: 0;
        overflow: hidden;
        transition: border-color 0.15s ease;
      }
      .pf-card:hover {
        border-color: #FFA028;
        z-index: 1;
      }

      /* ── Summary grid: name + 11 metrics ─────────────────────────── */
      .pf-summary {
        display: grid;
        grid-template-columns:
          minmax(0, 2fr)     /* name + ticker */
          repeat(11, minmax(0, 0.9fr));   /* earnings · price · mcap · pe · fpe · roe · ebit · qrev · ytd · 52wh · 52wl */
        gap: 6px 4px;
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
        color: #FFA028;
        font-family: monospace;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        font-size: 14px;
        line-height: 1.2;
        white-space: nowrap;
        overflow: hidden;
      }
      .pf-sub {
        font-size: 11px;
        color: #8a6a30;
        font-family: monospace;
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
        color: #8a6a30;
        font-family: monospace;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        line-height: 1.1;
      }
      .pf-metric-value {
        font-size: 13px;
        color: #FFA028;
        font-family: monospace;
        font-weight: 500;
        line-height: 1.2;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 100%;
      }
      .pf-metric-value.bold { font-weight: 700; }
      .pf-metric-value.muted { color: #5a4a25; }

      /* ── Tablet / Mobile (≤768px): card layout matches the user's
         mockup: name + trash on top row, Earnings full-width, then a
         strict 2-col grid of (Price | Mkt Cap), (P/E | EBIT M.),
         (ROE | YTD), and finally the "Chart ↓" toggle button below. */
      @media (max-width: 768px) {
        .pf-summary {
          grid-template-columns: 1fr 1fr;
          gap: 8px 12px;
        }
        /* Name + sub-line centred on mobile. */
        .pf-name-cell {
          grid-column: 1 / -1;
          border-bottom: 1px solid #2a1f10;
          padding-bottom: 6px;
          align-items: center;
          text-align: center;
        }
        .pf-name {
          font-size: 15px;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
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

        /* Earnings spans the full width and is centred — its own row
           directly under the name. */
        .pf-m-earnings {
          grid-column: 1 / -1;
          justify-content: center !important;
          text-align: center;
          border-bottom: 1px solid #2a1f10;
          padding-bottom: 6px;
        }
        .pf-m-earnings .pf-metric-value { text-align: center; }

        /* Explicit display order so the 6 lower metrics fall into the
           rows the user specified, regardless of HTML source order:
              Row 1: Price | Mkt Cap
              Row 2: P/E   | EBIT M.
              Row 3: ROE   | YTD
           (Name cell + Earnings are above thanks to grid-column span;
           order on grid items still flows them left→right, top→bottom.) */
        .pf-m-price { order: 1; }
        .pf-m-mcap  { order: 2; }
        .pf-m-pe    { order: 3; }
        .pf-m-fpe   { order: 4; }
        .pf-m-roe   { order: 5; }
        .pf-m-ebit  { order: 6; }
        .pf-m-qrev  { order: 7; }
        .pf-m-ytd   { order: 8; }
        .pf-m-52wh  { order: 9; }
        .pf-m-52wl  { order: 10; }
      }

      /* ── Phone (≤380px): keep the 2-col metric grid (so the 6 lower
         metrics still pair up as the user laid out); only widen
         spacing slightly. */
      @media (max-width: 380px) {
        .pf-summary { gap: 6px 10px; }
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

      /* Anchor markers collapse to zero so they don't affect layout. */
      .pf-remove-anchor { display: none; }


      /* ── Name row: company name + arrow left, trash icon right ──── */
      .pf-name-top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 20px;
        min-width: 0;
      }
      .pf-name-top .pf-name { flex: 1; min-width: 0; }
      .pf-trash-icon {
        font-size: 15px;
        color: #8a6a30;
        cursor: pointer;
        flex-shrink: 0;
        padding: 3px 5px;
        border-radius: 3px;
        line-height: 1;
        transition: color 0.15s ease, background 0.15s ease;
      }
      .pf-trash-icon:hover {
        color: #FF3030 !important;
        background: #200505 !important;
      }

      /* ── Mobile: float trash to top-right corner of the card ─────
         Absolutely positioned so it never crowds the name text. */
      @media (max-width: 768px) {
        .pf-card { position: relative; }
        .pf-trash-icon {
          position: absolute;
          top: 8px;
          right: 8px;
          font-size: 16px;
          padding: 4px 6px;
        }
      }

      /* Hidden delete button (same pattern as toggle button) */
      .pf-del-anchor { display: none; }
      div.element-container:has(.pf-del-anchor),
      div.stElementContainer:has(.pf-del-anchor),
      div[data-testid="element-container"]:has(.pf-del-anchor),
      div[data-testid="stElementContainer"]:has(.pf-del-anchor) {
        display: none !important;
      }
      div[data-testid="stElementContainer"]:has(.pf-del-anchor)
        + div[data-testid="stElementContainer"],
      div[data-testid="element-container"]:has(.pf-del-anchor)
        + div[data-testid="element-container"],
      div.stElementContainer:has(.pf-del-anchor) + div.stElementContainer,
      div.element-container:has(.pf-del-anchor) + div.element-container {
        display: none !important;
      }

      /* ── Name cell is the click target for expand/collapse ─────── */
      .pf-name-cell { cursor: pointer; }
      .pf-name-cell:hover .pf-name {
        color: #FFFFFF !important;
        transition: color 0.15s ease;
      }
      .pf-name-cell:hover .pf-sub {
        color: #FFA028 !important;
        transition: color 0.15s ease;
      }
      /* Active (expanded) state: slightly lighter amber */
      .pf-name-cell.pf-name-active .pf-name { color: #FFD080 !important; }
      .pf-name-cell.pf-name-active .pf-sub  { color: #FFA028 !important; }

      /* ── Expand/collapse arrow indicator ────────────────────────── */
      .pf-toggle-arrow {
        font-size: 9px;
        color: #8a6a30;
        margin-left: 5px;
        display: inline-block;
        transition: transform 0.2s ease, color 0.15s ease;
        vertical-align: middle;
        line-height: 1;
      }
      .pf-name-cell:hover .pf-toggle-arrow { color: #FFFFFF; }
      .pf-name-cell.pf-name-active .pf-toggle-arrow {
        transform: rotate(180deg);
        color: #FFD080;
      }

      /* ── Hidden toggle button + its anchor marker ────────────────
         Both the anchor div's container and the adjacent button
         container are collapsed to zero. CSS display:none still
         allows document.querySelector and JS .click() to work. */
      .pf-toggle-anchor { display: none; }
      div.element-container:has(.pf-toggle-anchor),
      div.stElementContainer:has(.pf-toggle-anchor),
      div[data-testid="element-container"]:has(.pf-toggle-anchor),
      div[data-testid="stElementContainer"]:has(.pf-toggle-anchor) {
        display: none !important;
      }
      div[data-testid="stElementContainer"]:has(.pf-toggle-anchor)
        + div[data-testid="stElementContainer"],
      div[data-testid="element-container"]:has(.pf-toggle-anchor)
        + div[data-testid="element-container"],
      div.stElementContainer:has(.pf-toggle-anchor) + div.stElementContainer,
      div.element-container:has(.pf-toggle-anchor) + div.element-container {
        display: none !important;
      }

      /* ── Zero out Streamlit's inter-element gap on the portfolio list ─ */
      /* Target the stVerticalBlock that wraps the portfolio cards and
         set its flex gap to 0. Scope via :has(.pf-card) so we only
         affect the portfolio list, not every stVerticalBlock on the page. */
      div[data-testid="stVerticalBlock"]:has(.pf-card) {
        gap: 9px !important;
        row-gap: 9px !important;
      }
      div[data-testid="stVerticalBlock"]:has(.pf-card)
        > div[data-testid="stElementContainer"],
      div[data-testid="stVerticalBlock"]:has(.pf-card)
        > div[data-testid="element-container"] {
        margin-top: 0 !important;
        margin-bottom: 0 !important;
        padding-top: 0 !important;
        padding-bottom: 0 !important;
      }
      div[data-testid="stVerticalBlock"]:has(.pf-card)
        > div[data-testid="stElementContainer"] > div,
      div[data-testid="stVerticalBlock"]:has(.pf-card)
        > div[data-testid="element-container"] > div {
        margin-top: 0 !important;
        margin-bottom: 0 !important;
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
      .pf-strip-meta { color: #8a6a30; font-family: monospace; font-size: 13px; min-width: 0; }
      .pf-strip-rec  { font-size: 14px; font-weight: 700; white-space: nowrap;
                       font-family: monospace; }
      .pf-strip-rec .lbl { color: #8a6a30; font-size: 12px; font-weight: 400;
                           margin-right: 4px; text-transform: uppercase; }

      .pf-banner {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        justify-content: space-between;
        gap: 4px 12px;
        margin: 8px 0 4px 0;
        padding: 8px 12px;
        background: #0a0a0a;
        border: 1px solid #2a1f10;
        border-radius: 2px;
      }
      .pf-banner-l { display: flex; flex-wrap: wrap; align-items: baseline;
                     gap: 4px 10px; min-width: 0; }
      .pf-banner-period { color: #8a6a30; font-family: monospace; font-size: 13px;
                          text-transform: uppercase; }
      .pf-banner-pct    { font-weight: 700; font-size: 22px; line-height: 1.1;
                          font-family: monospace; }
      .pf-banner-abs    { font-weight: 600; font-size: 15px; font-family: monospace; }
      .pf-banner-r      { color: #8a6a30; font-family: monospace; font-size: 12px;
                          white-space: nowrap; }

      .pf-lowhigh {
        display: flex;
        flex-wrap: wrap;
        gap: 4px 24px;
        color: #8a6a30;
        font-family: monospace;
        font-size: 13px;
        margin-top: 4px;
        text-transform: uppercase;
      }
      .pf-lowhigh b { color: #FFA028; }
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

    # ── Name-cell → hidden-button click forwarder ─────────────────────────────
    # An iframe script (same-origin, window.parent access) scans for name
    # cells carrying data-ticker and wires each to the matching hidden
    # Streamlit toggle button. setInterval re-binds after every st.rerun().
    st.iframe(
        """
        <script>
        (function () {
          function findAndClick(anchorClass, ticker) {
            try {
              var doc = window.parent.document;
              var anchor = doc.querySelector(
                '.' + anchorClass + '[data-ticker="' + CSS.escape(ticker) + '"]'
              );
              if (!anchor) return;
              var wrap = anchor.parentElement;
              while (wrap && !wrap.matches(
                '[data-testid="stElementContainer"],[data-testid="element-container"]'
              )) { wrap = wrap.parentElement; }
              if (!wrap) return;
              var btnWrap = wrap.nextElementSibling;
              if (!btnWrap) return;
              var btn = btnWrap.querySelector('button');
              if (btn) btn.click();
            } catch(e) {}
          }

          function bind() {
            try {
              var doc = window.parent.document;
              // Name cell → expand/collapse toggle
              doc.querySelectorAll('.pf-name-cell[data-ticker]').forEach(function(cell) {
                if (cell._pfClickBound) return;
                cell._pfClickBound = true;
                cell.addEventListener('click', function () {
                  findAndClick('pf-toggle-anchor', this.dataset.ticker);
                });
              });
              // Trash icon → delete (stopPropagation so expand doesn't also fire)
              doc.querySelectorAll('.pf-trash-icon[data-ticker]').forEach(function(icon) {
                if (icon._pfTrashBound) return;
                icon._pfTrashBound = true;
                icon.addEventListener('click', function(e) {
                  e.stopPropagation();
                  findAndClick('pf-del-anchor', this.dataset.ticker);
                });
              });
            } catch(e) {}
          }
          bind();
          setInterval(bind, 500);
        })();
        </script>
        """,
        height=1,
    )

    def _metric_html(label: str, value: str, *,
                     color: str = "#222", bold: bool = False,
                     muted: bool = False, extra_cls: str = "") -> str:
        cls = "pf-metric-value"
        if bold:  cls += " bold"
        if muted: cls += " muted"
        style = f"color:{color};" if color != "#222" else ""
        wrap_cls = f"pf-metric {extra_cls}".strip()
        return (
            f"<div class='{wrap_cls}'>"
            f"<div class='pf-metric-label'>{label}</div>"
            f"<div class='{cls}' style='{style}'>{value}</div>"
            f"</div>"
        )

    for ticker in list(st.session_state.portfolio_tickers):
        snap = _fetch_snapshot(ticker)
        rec_label, rec_color = _recommendation(snap)
        is_expanded = ticker in st.session_state.portfolio_expanded
        ytd_text, ytd_color = _fmt_signed_pct(snap.get("ytd_pct"))

        # Forward P/E
        fpe_raw = snap.get("forward_pe")
        fpe_text = _fmt_ratio(fpe_raw) if fpe_raw else "—"

        # Quarterly revenue growth YoY
        qrev_raw = snap.get("q_rev_growth")
        if qrev_raw is not None:
            qrev_text  = f"{qrev_raw*100:+.1f}%"
            qrev_color = "#4D9FFF" if qrev_raw >= 0 else "#FF3030"
        else:
            qrev_text, qrev_color = "—", "#8a6a30"

        # 52-week high/low relative to current price
        price_val   = snap.get("price")
        wk52h       = snap.get("week_52_high")
        wk52l       = snap.get("week_52_low")
        if price_val and wk52h and wk52h > 0:
            wk52h_pct   = price_val / wk52h - 1
            wk52h_text  = f"{wk52h_pct*100:+.1f}%"
            wk52h_color = "#4D9FFF" if wk52h_pct >= 0 else "#FF3030"
        else:
            wk52h_text, wk52h_color = "—", "#8a6a30"
        if price_val and wk52l and wk52l > 0:
            wk52l_pct   = price_val / wk52l - 1
            wk52l_text  = f"{wk52l_pct*100:+.1f}%"
            wk52l_color = "#4D9FFF" if wk52l_pct >= 0 else "#FF3030"
        else:
            wk52l_text, wk52l_color = "—", "#8a6a30"
        next_earnings = _fetch_next_earnings(ticker)

        # Name + ticker + (sector if available) at left
        name_full = snap["name"] or ticker
        sector = snap.get("sector") or ""
        sub_bits = [ticker] + ([sector] if sector else [])
        sub_line = " · ".join(sub_bits)

        # Earnings text (slightly muted when no date scheduled)
        if next_earnings:
            # Compact date: YY/MM/DD saves ~2 chars vs YYYY-MM-DD
            try:
                _ed = datetime.strptime(next_earnings[:10], "%Y-%m-%d")
                earn_fmt = _ed.strftime("%y/%m/%d")
            except Exception:
                earn_fmt = next_earnings[:10]
            earn_value = f"📅 {earn_fmt}"
            earn_kw = {}
        else:
            earn_value = "—"
            earn_kw = {"muted": True}

        # ── Card row (full-width): clicking the name cell expands/collapses ──
        active_cls = " pf-name-active" if is_expanded else ""
        ticker_attr = _html.escape(ticker)
        card_html = (
            "<div class='pf-card'>"
            "<div class='pf-summary'>"
            # Name cell — carries data-ticker so the JS click forwarder
            # can find it and route the click to the hidden toggle button.
            f"<div class='pf-name-cell{active_cls}' data-ticker='{ticker_attr}'>"
            f"<div class='pf-name-top'>"
            f"<div class='pf-name' title='{_html.escape(name_full)}'>"
            f"{_html.escape(name_full)}"
            f"<span class='pf-toggle-arrow'>&#9660;</span></div>"
            f"<span class='pf-trash-icon' data-ticker='{ticker_attr}'>&#128465;</span>"
            f"</div>"
            f"<div class='pf-sub'>{_html.escape(sub_line)}</div>"
            "</div>"
            f"{_metric_html('Earnings', earn_value, extra_cls='pf-m-earnings', **earn_kw)}"
            f"{_metric_html('Price', _fmt_price(snap['price'], snap['currency']), extra_cls='pf-m-price')}"
            f"{_metric_html('Mkt Cap', _fmt_money(snap['market_cap']), extra_cls='pf-m-mcap')}"
            f"{_metric_html('P/E', _fmt_ratio(snap['pe']), extra_cls='pf-m-pe')}"
            f"{_metric_html('F P/E', fpe_text, extra_cls='pf-m-fpe')}"
            f"{_metric_html('ROE', _fmt_pct(snap['roe']), extra_cls='pf-m-roe')}"
            f"{_metric_html('EBIT M.', _fmt_pct(snap['ebit_margin']), extra_cls='pf-m-ebit')}"
            f"{_metric_html('Q REV YoY', qrev_text, color=qrev_color, extra_cls='pf-m-qrev')}"
            f"{_metric_html('YTD', ytd_text, color=ytd_color, bold=True, extra_cls='pf-m-ytd')}"
            f"{_metric_html('52WH', wk52h_text, color=wk52h_color, extra_cls='pf-m-52wh')}"
            f"{_metric_html('52WL', wk52l_text, color=wk52l_color, extra_cls='pf-m-52wl')}"
            "</div></div>"
        )
        st.markdown(card_html, unsafe_allow_html=True)

        # Hidden toggle button — zero-height via CSS, JS-clickable.
        st.markdown(f"<div class='pf-toggle-anchor' data-ticker='{ticker_attr}'></div>",
                    unsafe_allow_html=True)
        if st.button("·", key=f"toggle_{ticker}", use_container_width=False):
            if is_expanded:
                st.session_state.portfolio_expanded.discard(ticker)
            else:
                st.session_state.portfolio_expanded.add(ticker)
            st.rerun()

        # Hidden delete button — zero-height via CSS, JS-clickable via trash icon.
        st.markdown(f"<div class='pf-del-anchor' data-ticker='{ticker_attr}'></div>",
                    unsafe_allow_html=True)
        if st.button("x", key=f"del_{ticker}", use_container_width=False):
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

                        # Line + heading colour: Bloomberg bullish blue / bearish red
                        line_color = "#4D9FFF" if chg_pct >= 0 else "#FF3030"
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
                        # For multi-year periods show year labels; shorter periods use Altair defaults
                        if sel_period in ("5y", "All"):
                            x_axis = alt.Axis(format="%Y", tickCount="year")
                        elif sel_period in ("1y", "YTD"):
                            x_axis = alt.Axis(format="%b %Y", tickCount="month")
                        else:
                            x_axis = alt.Axis()
                        chart = (
                            alt.Chart(df_chart)
                               .mark_line(strokeWidth=2)
                               .encode(
                                   x=alt.X(f"{time_col}:{x_type}", title="", axis=x_axis),
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
                        st.warning("No news available for this ticker.")
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
                                    f" <span style='background:#0a1f30;color:#4D9FFF;"
                                    f"padding:1px 6px;border:1px solid #1f4a70;"
                                    f"border-radius:2px;font-family:monospace;"
                                    f"font-size:11px;font-weight:700;'>+ {polarity:.2f}</span>"
                                )
                            elif polarity <= -0.15:
                                sent_badge = (
                                    f" <span style='background:#200505;color:#FF3030;"
                                    f"padding:1px 6px;border:1px solid #5a1010;"
                                    f"border-radius:2px;font-family:monospace;"
                                    f"font-size:11px;font-weight:700;'>− {polarity:.2f}</span>"
                                )
                            else:
                                sent_badge = (
                                    f" <span style='background:#0a0a0a;color:#8a6a30;"
                                    f"padding:1px 6px;border:1px solid #2a1f10;"
                                    f"border-radius:2px;font-family:monospace;"
                                    f"font-size:11px;'>~ {polarity:.2f}</span>"
                                )

                            title_html = (
                                f"<a href='{link}' target='_blank' "
                                f"style='color:#FFA028;font-weight:700;"
                                f"font-family:monospace;text-decoration:none;'>"
                                f"{title}</a>"
                                if link else
                                f"<span style='color:#FFA028;font-weight:700;"
                                f"font-family:monospace;'>{title}</span>"
                            )
                            st.markdown(
                                f"<div style='margin-bottom:4px;'>"
                                f"<span style='color:#8a6a30;font-family:monospace;"
                                f"font-size:12px;'>{date_short}</span>"
                                f"{sent_badge}<br>{title_html}"
                                f"</div>",
                                unsafe_allow_html=True,
                            )
                            if content:
                                snippet = content.strip().replace("\n", " ")
                                if len(snippet) > 300:
                                    snippet = snippet[:300].rstrip() + "…"
                                st.markdown(
                                    f"<div style='color:#a87f30;font-family:monospace;"
                                    f"font-size:13px;margin-bottom:12px;line-height:1.4;'>"
                                    f"{snippet}</div>",
                                    unsafe_allow_html=True,
                                )
                            else:
                                st.markdown("<div style='margin-bottom:12px;'></div>",
                                            unsafe_allow_html=True)



st.markdown("&nbsp;", unsafe_allow_html=True)
# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(
    "<div style='margin-top:24px;padding-top:8px;"
    "border-top:1px solid #2a1f10;color:#8a6a30;font-family:monospace;"
    "font-size:12px;line-height:1.5;text-align:center;'>"
    "Personal watchlist powered by <b style='color:#FFA028;'>EODHD only</b>. "
    "Add any ticker — stocks (AAPL, RHM.DE), indices (^GSPC, ^DJI), "
    "ETFs (SPY) or forex (EURUSD=X). Cards are collapsed by default — "
    "click ▾ to expand."
    "<br><span style='font-size:11px;color:#5a4a25;'>"
    "Snapshot cached 15 min · History 30 min · News 30 min · "
    "Earnings dates 6 h · Recommendation is a rule-based heuristic on "
    "P/E + ROE + EBIT margin — not investment advice."
    "</span></div>",
    unsafe_allow_html=True,
)
