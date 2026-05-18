"""
price_chart.py — Generate 5-year daily close-price chart as PNG bytes.

Used by the Overview / Investment Memo report to embed an accurate
visualisation of price history right at the top of page 1.

Data source priority:
  1. yfinance.Ticker.history(period="5y")        — works locally, often
                                                    blocked on Streamlit Cloud
  2. Stooq daily-history CSV (free, no API key)  — works on cloud, less
                                                    granular ticker coverage

Output: PNG byte string suitable for ReportLab's Image flowable.
Returns None on any failure so the caller can render the page without the
chart instead of breaking the whole report.
"""

from __future__ import annotations
import io
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

import matplotlib
matplotlib.use("Agg")           # headless backend — no display required
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests

logger = logging.getLogger(__name__)


# ── Visual styling ────────────────────────────────────────────────────────────
LINE_COLOR    = "#003F54"   # Pantone 303 — ink-saving brand colour
FILL_COLOR    = "#003F54"
GRID_COLOR    = "#DDDDDD"
TICK_COLOR    = "#666666"
LABEL_COLOR   = "#333333"
TITLE_COLOR   = "#003F54"
LATEST_COLOR  = "#C9843E"   # orange highlight for the final marker


# ── Public API ────────────────────────────────────────────────────────────────

def generate_price_chart_png(
    ticker: str,
    company_name: Optional[str] = None,
    currency: Optional[str] = None,
    width_in: float = 7.5,
    height_in: float = 2.4,
    dpi: int = 130,
    eod_data: Optional[list] = None,
) -> Optional[bytes]:
    """
    Return PNG bytes of a 5-year daily close price chart, or None on failure.

    `eod_data` (optional) — list of {date, close, ...} dicts in EODHD /eod
    format. When supplied, the chart is rendered exclusively from this data
    (used by Investment Memo V2 to keep the chart 100% EODHD-sourced).
    Otherwise we fall back to yfinance → Stooq.
    """
    dates: list = []
    prices: list = []
    src = ""

    # Priority 1: explicit EODHD /eod bundle passed by caller.
    # Prefer adjusted_close so the line is split-adjusted (otherwise stocks
    # like DECK show an artificial cliff at the split date, e.g. 6-for-1 in
    # Sep-2024 drops the unadjusted close from ~$1000 to ~$160).
    if eod_data and isinstance(eod_data, list):
        from datetime import datetime as _dt
        for row in eod_data:
            if not isinstance(row, dict): continue
            d = row.get("date")
            adj = row.get("adjusted_close")
            raw = row.get("close")
            p = adj if adj not in (None, "", "NA", 0) else raw
            if d and p not in (None, "", "NA"):
                try:
                    dates.append(_dt.strptime(str(d), "%Y-%m-%d"))
                    prices.append(float(p))
                except (ValueError, TypeError):
                    continue
        if dates:
            src = "EODHD /eod (split-adjusted)"

    # Priority 2: yfinance live fetch (V1 path)
    if not dates:
        dates, prices = _fetch_5y_yfinance(ticker)
        src = "yfinance"

    # Priority 3: Stooq fallback
    if not dates:
        dates, prices = _fetch_5y_stooq(ticker)
        src = "stooq"

    if not dates or not prices:
        logger.warning(f"[price_chart] No price history for {ticker}")
        return None

    try:
        return _render_chart(
            dates, prices, ticker, company_name, currency,
            width_in, height_in, dpi, source=src,
        )
    except Exception as e:
        logger.warning(f"[price_chart] Render failed for {ticker}: {e}")
        return None


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_5y_yfinance(ticker: str) -> Tuple[List[datetime], List[float]]:
    """Fetch ~5 years of daily close prices via yfinance.

    auto_adjust=True so the 'Close' column is split + dividend adjusted
    — required for tickers like DECK that did a stock split in the 5y
    window. Without this the chart shows an artificial cliff at the split.
    """
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="5y", interval="1d", auto_adjust=True)
        if df is None or df.empty or "Close" not in df.columns:
            return [], []
        dates = [d.to_pydatetime() for d in df.index]
        prices = [float(v) for v in df["Close"].values]
        # Drop NaN rows defensively
        clean = [(d, p) for d, p in zip(dates, prices) if p == p and p > 0]
        if not clean:
            return [], []
        dates, prices = zip(*clean)
        return list(dates), list(prices)
    except Exception as e:
        logger.warning(f"[price_chart] yfinance fetch failed for {ticker}: {e}")
        return [], []


# Yahoo Finance suffix → Stooq suffix mapping for historical CSV.
# Same convention as stooq_adapter; duplicated here to keep this module
# self-contained (it's imported lazily by pdf_overview only).
_YF_TO_STOOQ = {
    "":     ".us",   ".DE": ".de", ".F": ".f",   ".L": ".uk",
    ".PA":  ".fr",   ".AS": ".nl", ".BR": ".be", ".MI": ".it",
    ".MC":  ".es",   ".HE": ".fi", ".ST": ".se", ".OL": ".no",
    ".CO":  ".dk",   ".SW": ".ch", ".VI": ".at", ".WA": ".pl",
    ".LS":  ".pt",   ".AT": ".gr", ".IR": ".ie", ".TO": ".ca",
    ".V":   ".cv",   ".SA": ".br", ".MX": ".mx", ".HK": ".hk",
    ".T":   ".jp",   ".AX": ".au",
}


def _yf_to_stooq(yf_ticker: str) -> str:
    dot = yf_ticker.rfind(".")
    if dot == -1:
        base, suffix = yf_ticker, ""
    else:
        base, suffix = yf_ticker[:dot], yf_ticker[dot:]
    stooq_suffix = _YF_TO_STOOQ.get(suffix.upper(), suffix.lower())
    return f"{base.lower()}{stooq_suffix}"


_STOOQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}


def _fetch_5y_stooq(ticker: str) -> Tuple[List[datetime], List[float]]:
    """
    Fetch 5 years of daily closes from Stooq's historical CSV endpoint.
    Format: Date,Open,High,Low,Close,Volume
    """
    try:
        stooq_ticker = _yf_to_stooq(ticker)
        end = datetime.utcnow()
        start = end - timedelta(days=5 * 365 + 10)
        url = (
            f"https://stooq.com/q/d/l/?s={stooq_ticker}&i=d"
            f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
        )
        resp = requests.get(url, headers=_STOOQ_HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning(f"[price_chart] Stooq history HTTP {resp.status_code}")
            return [], []
        lines = resp.text.strip().splitlines()
        if len(lines) < 2:
            return [], []
        # First line is header
        header = [c.strip().lower() for c in lines[0].split(",")]
        if "date" not in header or "close" not in header:
            return [], []
        i_date = header.index("date")
        i_close = header.index("close")
        dates: List[datetime] = []
        prices: List[float] = []
        for ln in lines[1:]:
            cols = ln.split(",")
            if len(cols) <= max(i_date, i_close):
                continue
            try:
                d = datetime.strptime(cols[i_date], "%Y-%m-%d")
                p = float(cols[i_close])
                if p > 0:
                    dates.append(d)
                    prices.append(p)
            except (ValueError, IndexError):
                continue
        if not dates:
            return [], []
        return dates, prices
    except Exception as e:
        logger.warning(f"[price_chart] Stooq history failed for {ticker}: {e}")
        return [], []


# ── Renderer ──────────────────────────────────────────────────────────────────

def _render_chart(
    dates: List[datetime],
    prices: List[float],
    ticker: str,
    company_name: Optional[str],
    currency: Optional[str],
    width_in: float,
    height_in: float,
    dpi: int,
    source: str,
) -> bytes:
    """Produce a single PNG of the price line; returns bytes."""
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    # Price line + light fill underneath for visual depth
    ax.plot(dates, prices, color=LINE_COLOR, linewidth=1.1, antialiased=True)
    ax.fill_between(dates, prices, min(prices) * 0.98,
                    color=FILL_COLOR, alpha=0.08, linewidth=0)

    # Highlight the latest price
    last_d, last_p = dates[-1], prices[-1]
    ax.scatter([last_d], [last_p], color=LATEST_COLOR, s=18, zorder=5,
               edgecolors="white", linewidths=0.6)
    cur_label = currency or ""
    ax.annotate(
        f"{last_p:,.2f} {cur_label}".strip(),
        xy=(last_d, last_p),
        xytext=(-6, 6), textcoords="offset points",
        fontsize=7.5, color=LATEST_COLOR, fontweight="bold",
        ha="right",
    )

    # Min / max markers (subtle)
    pmin_idx = prices.index(min(prices))
    pmax_idx = prices.index(max(prices))
    for idx, label_prefix in [(pmin_idx, "L"), (pmax_idx, "H")]:
        ax.annotate(
            f"{prices[idx]:,.2f}",
            xy=(dates[idx], prices[idx]),
            xytext=(0, -10 if label_prefix == "L" else 6),
            textcoords="offset points",
            fontsize=6, color=TICK_COLOR, ha="center",
        )

    # Title
    title_text = f"{company_name or ticker}  ·  5-Year Daily Close"
    ax.set_title(title_text, fontsize=9, color=TITLE_COLOR,
                 fontweight="bold", loc="left", pad=4)

    # Axis formatting
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(axis="x", labelsize=7, colors=TICK_COLOR, length=2)
    ax.tick_params(axis="y", labelsize=7, colors=TICK_COLOR, length=2)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID_COLOR)
        ax.spines[spine].set_linewidth(0.5)

    ax.grid(True, axis="y", color=GRID_COLOR, linewidth=0.4, alpha=0.7)
    ax.grid(False, axis="x")

    # Pad y-range slightly so labels don't touch top/bottom
    p_min, p_max = min(prices), max(prices)
    span = p_max - p_min if p_max > p_min else max(p_max * 0.05, 1.0)
    ax.set_ylim(p_min - span * 0.06, p_max + span * 0.12)

    # Footnote (data source)
    fig.text(0.99, 0.02, f"Source: {source}", ha="right", va="bottom",
             fontsize=5.5, color=TICK_COLOR, style="italic")

    fig.tight_layout(pad=0.4)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    return buf.getvalue()
