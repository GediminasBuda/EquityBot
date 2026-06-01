"""
pdf_fund_fundamentals.py — Fund Fundamentals PDF renderer.

Handles both ETF and Mutual Fund layouts based on bundle["fund_type"].

ETF layout  (3 pages):
  1  General Info · Key Stats · Technicals · Dividends
  2  Price Chart · Market Cap Breakdown · Asset Allocation · World Regions · Sector Weights
  3  Top 10 Holdings · Valuation & Growth vs Category · Performance

Mutual Fund layout  (3 pages):
  1  General Info · Asset Allocation · Value/Growth Measures
  2  Price Chart · Sector Weightings · World Regions · Market Classification
  3  Top Countries (bond funds) · Top Holdings
"""

from __future__ import annotations
import io
import logging
from datetime import datetime
from typing import Any, Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak, Image,
)

logger = logging.getLogger(__name__)

# ── Page geometry ─────────────────────────────────────────────────────────────
W, H = A4
ML = MR = 15 * mm
MT = 28 * mm
MB = 12 * mm
CW = W - ML - MR

# ── Colour palette ─────────────────────────────────────────────────────────────
NAVY    = HexColor("#003F54")
TEAL    = HexColor("#1A6E5A")
DGRAY   = HexColor("#333333")
MGRAY   = HexColor("#666666")
CGRAY   = HexColor("#999999")
RULE    = HexColor("#DDDDDD")
BORDER  = HexColor("#CCCCCC")
GREEN   = HexColor("#1A7E3D")
RED     = HexColor("#C0392B")
ORANGE  = HexColor("#C9843E")
LGRAY   = HexColor("#F5F5F5")

GREEN_HEX  = "#1A7E3D"
RED_HEX    = "#C0392B"
NAVY_HEX   = "#003F54"
TEAL_HEX   = "#1A6E5A"
MGRAY_HEX  = "#666666"
ORANGE_HEX = "#C9843E"

BASE_FONT   = "Helvetica"
BOLD_FONT   = "Helvetica-Bold"


# ── Formatters ────────────────────────────────────────────────────────────────
def _f(v: Any, dec: int = 2) -> str:
    if v is None or v == "" or v == "NA": return "—"
    try: return f"{float(v):,.{dec}f}"
    except: return str(v) if v else "—"

def _f0(v): return _f(v, 0)
def _f2(v): return _f(v, 2)

def _pct(v: Any, dec: int = 2) -> str:
    if v is None or v == "" or v == "NA": return "—"
    try: return f"{float(v):.{dec}f}%"
    except: return "—"

def _pct_raw(v: Any, dec: int = 2) -> str:
    """Value is already a decimal (0.05 → 5.00%)."""
    if v is None or v == "" or v == "NA": return "—"
    try: return f"{float(v) * 100:.{dec}f}%"
    except: return "—"

def _millions(v: Any) -> str:
    if v is None or v == "" or v == "NA": return "—"
    try:
        f = float(v)
        if abs(f) >= 1e9:
            return f"{f/1e9:,.2f}B"
        return f"{f/1e6:,.1f}M"
    except: return "—"

def _s(v: Any) -> str:
    if v is None or v == "" or v == "NA" or v == "None": return "—"
    return str(v).strip()


# ── Style helpers ─────────────────────────────────────────────────────────────
def _build_styles() -> dict:
    def ps(name, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    return {
        "title": ps("title", fontName=BOLD_FONT, fontSize=16, textColor=NAVY,
                    spaceAfter=2, leading=20),
        "subtitle": ps("subtitle", fontName=BASE_FONT, fontSize=9, textColor=MGRAY,
                       spaceAfter=6, leading=12),
        "section": ps("section", fontName=BOLD_FONT, fontSize=9, textColor=white,
                      backColor=NAVY, spaceBefore=8, spaceAfter=4,
                      leftIndent=-2, rightIndent=-2, leading=13),
        "sub_section": ps("sub_section", fontName=BOLD_FONT, fontSize=8,
                          textColor=NAVY, spaceBefore=6, spaceAfter=2, leading=11),
        "body": ps("body", fontName=BASE_FONT, fontSize=8, textColor=DGRAY,
                   leading=11, spaceAfter=2),
        "small": ps("small", fontName=BASE_FONT, fontSize=7, textColor=MGRAY,
                    leading=10),
        "caption": ps("caption", fontName=BASE_FONT, fontSize=6.5, textColor=CGRAY,
                      leading=9, alignment=TA_RIGHT),
        "label": ps("label", fontName=BOLD_FONT, fontSize=7.5, textColor=NAVY,
                    leading=10),
        "value": ps("value", fontName=BASE_FONT, fontSize=8, textColor=DGRAY,
                    leading=10),
    }


def _sec(title: str, styles: dict) -> Paragraph:
    return Paragraph(f"&nbsp; {title} &nbsp;", styles["section"])


def _rule() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=4)


def _kv_table(rows: list[tuple], col_widths=None) -> Table:
    """Two-column label/value table."""
    if col_widths is None:
        col_widths = [55 * mm, CW - 55 * mm]
    data = [[Paragraph(f"<b>{k}</b>", ParagraphStyle("kl", fontName=BOLD_FONT,
             fontSize=7.5, textColor=NAVY, leading=10)),
             Paragraph(str(v), ParagraphStyle("kv", fontName=BASE_FONT,
             fontSize=8, textColor=DGRAY, leading=10))]
            for k, v in rows]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",  (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0,0), (-1, -1), 2),
        ("LINEBELOW",   (0, 0), (-1, -1), 0.3, RULE),
    ]))
    return t


def _data_table(headers: list, rows: list, col_widths=None,
                right_cols: list = None) -> Table:
    """Generic data table with header row."""
    right_cols = right_cols or []
    header_style = ParagraphStyle("th", fontName=BOLD_FONT, fontSize=7,
                                  textColor=white, leading=9)
    cell_style   = ParagraphStyle("td", fontName=BASE_FONT, fontSize=7,
                                  textColor=DGRAY, leading=9)

    table_data = [[Paragraph(h, header_style) for h in headers]]
    for row in rows:
        table_data.append([Paragraph(str(c), cell_style) for c in row])

    if col_widths is None:
        col_widths = [CW / len(headers)] * len(headers)

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND",    (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LGRAY]),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
    ]
    for c in right_cols:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _bar_table(label: str, pct_val: float, color: HexColor,
               width_pts: float = 120) -> Table:
    """Horizontal bar representing a percentage (0-100)."""
    filled = max(0.0, min(100.0, pct_val)) / 100.0
    bar_w   = width_pts * filled
    empty_w = width_pts * (1.0 - filled)
    bar_cells = [["", ""]]
    t = Table(bar_cells,
              colWidths=[bar_w if bar_w > 0 else 0.1,
                         empty_w if empty_w > 0 else 0.1],
              rowHeights=[5])
    ts = [("BACKGROUND", (0, 0), (0, 0), color),
          ("BACKGROUND", (1, 0), (1, 0), RULE),
          ("TOPPADDING",  (0,0),(-1,-1), 0),
          ("BOTTOMPADDING",(0,0),(-1,-1), 0),
          ("LEFTPADDING", (0,0),(-1,-1), 0),
          ("RIGHTPADDING",(0,0),(-1,-1), 0)]
    t.setStyle(TableStyle(ts))
    return t


# ── Price chart ───────────────────────────────────────────────────────────────
def _render_price_chart(eod_data: list, ticker: str,
                        currency: str = "") -> Optional[bytes]:
    if not eod_data:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime as _dt
    except ImportError:
        return None

    dates, prices = [], []
    for row in eod_data:
        if not isinstance(row, dict): continue
        d = row.get("date")
        p = row.get("adjusted_close") or row.get("close")
        if d and p is not None:
            try:
                dates.append(_dt.strptime(d, "%Y-%m-%d"))
                prices.append(float(p))
            except: continue
    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(7.2, 2.4), dpi=130)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.plot(dates, prices, color=NAVY_HEX, linewidth=1.1)
    ax.fill_between(dates, prices, min(prices) * 0.98,
                    color=NAVY_HEX, alpha=0.08, linewidth=0)
    last_p = prices[-1]
    ax.scatter([dates[-1]], [last_p], color=ORANGE_HEX, s=18, zorder=5,
               edgecolors="white", linewidths=0.6)
    ax.annotate(f"{last_p:,.2f} {currency}".strip(),
                xy=(dates[-1], last_p), xytext=(-6, 6),
                textcoords="offset points", fontsize=7.5,
                color=ORANGE_HEX, fontweight="bold", ha="right")
    ax.set_title(f"{ticker} · 5-Year Daily Close (EODHD /eod)",
                 fontsize=9, color=NAVY_HEX, fontweight="bold", loc="left", pad=4)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(axis="both", labelsize=7, colors="#666666", length=2)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#DDDDDD")
        ax.spines[sp].set_linewidth(0.5)
    ax.grid(True, axis="y", color="#DDDDDD", linewidth=0.4, alpha=0.7)
    fig.text(0.99, 0.02, f"Source: EODHD /eod  ({len(dates)} observations)",
             ha="right", va="bottom", fontsize=5.5, color="#666666")
    fig.tight_layout(pad=0.4)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# ETF PAGES
# ══════════════════════════════════════════════════════════════════════════════

def _etf_page1(bundle: dict, styles: dict) -> list:
    el = []
    f      = bundle.get("fundamentals") or {}
    gen    = f.get("General")    or {}
    etf    = f.get("ETF_Data")   or {}
    tech   = f.get("Technicals") or {}
    rt     = bundle.get("realtime") or {}
    divs   = bundle.get("dividends") or []

    # Title
    name = _s(gen.get("Name") or etf.get("Company_Name") or bundle.get("ticker"))
    el.append(Paragraph(name, styles["title"]))
    el.append(Paragraph(
        f"ETF · {_s(gen.get('Exchange'))} · {_s(gen.get('CurrencyCode'))} · "
        f"ISIN: {_s(etf.get('ISIN'))} · Updated: {_s(gen.get('UpdatedAt'))}",
        styles["subtitle"]
    ))
    el.append(_rule())

    # ── General / Cost Structure ──────────────────────────────────────────────
    el.append(_sec("General Information", styles))
    url = _s(etf.get("Company_URL"))
    url_str = f'<link href="{url}" color="{TEAL_HEX}">{url}</link>' if url != "—" else "—"
    kv_gen = [
        ("Fund Company",         _s(etf.get("Company_Name"))),
        ("Company URL",          url_str),
        ("Fund Category",        _s(etf.get("Category_Name") or gen.get("Category"))),
        ("Investment Style",     _s(etf.get("Fund_Style") or etf.get("Investment_Style"))),
        ("Inception Date",       _s(etf.get("Inception_Date"))),
        ("Country",              _s(gen.get("CountryName"))),
        ("Description",          _s(gen.get("Description") or "")[:300]),
    ]
    el.append(_kv_table(kv_gen))
    el.append(Spacer(1, 4))

    el.append(_sec("Cost Structure & Key Metrics", styles))
    kv_cost = [
        ("Current Yield",             _pct(etf.get("Yield"))),
        ("Dividend Paying Frequency", _s(etf.get("Dividend_Paying_Frequency"))),
        ("Net Expense Ratio",         _pct(etf.get("Net_Expense_Ratio"))),
        ("Ongoing Charge (OGC)",      _pct(etf.get("Net_Expense_Ratio"))),
        ("Average Market Cap (Mil)",  _s(etf.get("Average_Mkt_Cap_Mil"))),
        ("Annual Holdings Turnover",  _pct(etf.get("Annual_Holdings_Turnover"))),
        ("Total Net Assets",          _s(etf.get("Total_Net_Assets"))),
        ("Holdings Count",            _s(etf.get("Holdings_Count"))),
    ]
    el.append(_kv_table(kv_cost))
    el.append(Spacer(1, 4))

    # ── Technicals ────────────────────────────────────────────────────────────
    el.append(_sec("Technicals", styles))
    price_rt  = rt.get("close") or rt.get("previousClose")
    price_chg = rt.get("change_p")
    chg_str   = f" ({_f2(price_chg)}%)" if price_chg is not None else ""
    kv_tech = [
        ("Last Price",       f"{_f2(price_rt)}{chg_str}"),
        ("Beta",             _f2(tech.get("Beta"))),
        ("52-Week High",     _f2(tech.get("52WeekHigh"))),
        ("52-Week Low",      _f2(tech.get("52WeekLow"))),
        ("50-Day MA",        _f2(tech.get("50DayMA"))),
        ("200-Day MA",       _f2(tech.get("200DayMA"))),
        ("Volume",           _f0(rt.get("volume"))),
    ]
    el.append(_kv_table(kv_tech))
    el.append(Spacer(1, 4))

    # ── Recent Dividends ──────────────────────────────────────────────────────
    if divs:
        el.append(_sec("Recent Dividend Payments", styles))
        recent = sorted(divs, key=lambda x: x.get("date", ""), reverse=True)[:10]
        rows = [(_s(d.get("date")), _f2(d.get("value")), _s(d.get("currency", "")))
                for d in recent]
        el.append(_data_table(
            ["Date", "Amount", "Currency"],
            rows,
            col_widths=[50*mm, 50*mm, CW - 100*mm],
            right_cols=[1],
        ))

    return el


def _etf_page2(bundle: dict, styles: dict) -> list:
    el = []
    f   = bundle.get("fundamentals") or {}
    etf = f.get("ETF_Data") or {}
    gen = f.get("General")  or {}
    eod = bundle.get("eod") or []

    el.append(PageBreak())

    # ── Price Chart ───────────────────────────────────────────────────────────
    el.append(_sec("Price History — 5-Year Daily Close", styles))
    cur = _s(gen.get("CurrencyCode"))
    png = _render_price_chart(eod, bundle.get("ticker", ""), cur if cur != "—" else "")
    if png:
        el.append(Image(io.BytesIO(png), width=170*mm, height=52*mm, kind="proportional"))
    else:
        el.append(Paragraph("Price chart not available.", styles["small"]))
    el.append(Spacer(1, 6))

    # ── Market Cap Breakdown ──────────────────────────────────────────────────
    mk = etf.get("Market_Capitalisation") or etf.get("Market_Capitalization") or {}
    if mk:
        el.append(_sec("Market Capitalisation Breakdown", styles))
        mk_rows = []
        for label, val in mk.items():
            pct_v = val if isinstance(val, (int, float)) else (
                val.get("Equity_%") or val.get("Amount_%") or val.get("Net_Assets_%")
                if isinstance(val, dict) else None)
            if pct_v is not None:
                try:
                    mk_rows.append((label, f"{float(pct_v):.2f}%"))
                except: pass
        if mk_rows:
            el.append(_data_table(
                ["Category", "Weight %"],
                mk_rows,
                col_widths=[100*mm, CW - 100*mm],
                right_cols=[1],
            ))
        el.append(Spacer(1, 4))

    # ── Asset Allocation ──────────────────────────────────────────────────────
    aa = etf.get("Asset_Allocation") or {}
    if aa:
        el.append(_sec("Asset Allocation", styles))
        aa_rows = []
        for cat, vals in aa.items():
            if isinstance(vals, dict):
                pct_v = vals.get("Net_Assets_%") or vals.get("Long_%") or vals.get("Short_%")
            else:
                pct_v = vals
            try:
                pct_f = float(pct_v) if pct_v is not None else None
            except: pct_f = None
            aa_rows.append((cat, f"{pct_f:.2f}%" if pct_f is not None else "—"))
        el.append(_data_table(
            ["Asset Class", "Net Assets %"],
            aa_rows,
            col_widths=[100*mm, CW - 100*mm],
            right_cols=[1],
        ))
        el.append(Spacer(1, 4))

    # ── World Regions ─────────────────────────────────────────────────────────
    wr = etf.get("World_Regions") or {}
    if wr:
        el.append(_sec("World Regions", styles))
        wr_rows = []
        for region, vals in wr.items():
            pct_v = vals.get("Equity_%") if isinstance(vals, dict) else vals
            try: pct_f = float(pct_v)
            except: pct_f = None
            wr_rows.append((region, f"{pct_f:.2f}%" if pct_f is not None else "—"))
        el.append(_data_table(
            ["Region", "Equity %"],
            wr_rows,
            col_widths=[100*mm, CW - 100*mm],
            right_cols=[1],
        ))
        el.append(Spacer(1, 4))

    # ── Sector Weights ────────────────────────────────────────────────────────
    sw = etf.get("Sector_Weights") or {}
    if sw:
        el.append(_sec("Sector Weights", styles))
        sw_rows = []
        for sector, vals in sw.items():
            pct_v = vals.get("Equity_%") or vals.get("Amount_%") if isinstance(vals, dict) else vals
            try: pct_f = float(pct_v)
            except: pct_f = None
            sw_rows.append((sector, f"{pct_f:.2f}%" if pct_f is not None else "—"))
        sw_rows.sort(key=lambda x: float(x[1].replace("%", "")) if x[1] != "—" else 0, reverse=True)
        el.append(_data_table(
            ["Sector", "Weight %"],
            sw_rows,
            col_widths=[100*mm, CW - 100*mm],
            right_cols=[1],
        ))

    return el


def _etf_page3(bundle: dict, styles: dict) -> list:
    el = []
    f   = bundle.get("fundamentals") or {}
    etf = f.get("ETF_Data") or {}

    el.append(PageBreak())

    # ── Top 10 Holdings ───────────────────────────────────────────────────────
    holdings = etf.get("Top_10_Holdings") or {}
    if holdings:
        el.append(_sec("Top 10 Holdings", styles))
        h_rows = []
        items = list(holdings.items())
        for code, hdata in items[:10]:
            if isinstance(hdata, dict):
                h_rows.append((
                    _s(hdata.get("Name") or code),
                    _s(hdata.get("Sector") or "—"),
                    _s(hdata.get("Country") or "—"),
                    f"{float(hdata.get('Assets_%', 0)):.2f}%" if hdata.get("Assets_%") else "—",
                ))
            else:
                h_rows.append((code, "—", "—", _s(hdata)))
        el.append(_data_table(
            ["Name", "Sector", "Country", "Weight %"],
            h_rows,
            col_widths=[70*mm, 45*mm, 30*mm, CW - 145*mm],
            right_cols=[3],
        ))
        el.append(Spacer(1, 6))

    # ── Valuation & Growth ────────────────────────────────────────────────────
    vg = etf.get("Valuations_Growth") or {}
    if vg:
        el.append(_sec("Valuation & Growth — Portfolio vs Category", styles))
        port   = vg.get("Valuations_Rates_Portfolio") or {}
        cat    = vg.get("Valuations_Rates_To_Category") or {}
        g_port = vg.get("Growth_Rates_Portfolio") or {}
        g_cat  = vg.get("Growth_Rates_To_Category") or {}

        vg_rows = []
        all_keys = list(dict.fromkeys(list(port.keys()) + list(cat.keys())))
        for k in all_keys:
            vg_rows.append((k, _f2(port.get(k)), _f2(cat.get(k))))
        if vg_rows:
            el.append(Paragraph("Valuation Rates", styles["sub_section"]))
            el.append(_data_table(
                ["Metric", "Portfolio", "vs Category"],
                vg_rows,
                col_widths=[80*mm, 50*mm, CW - 130*mm],
                right_cols=[1, 2],
            ))
            el.append(Spacer(1, 4))

        gr_rows = []
        all_gkeys = list(dict.fromkeys(list(g_port.keys()) + list(g_cat.keys())))
        for k in all_gkeys:
            gr_rows.append((k, _f2(g_port.get(k)), _f2(g_cat.get(k))))
        if gr_rows:
            el.append(Paragraph("Growth Rates", styles["sub_section"]))
            el.append(_data_table(
                ["Metric", "Portfolio", "vs Category"],
                gr_rows,
                col_widths=[80*mm, 50*mm, CW - 130*mm],
                right_cols=[1, 2],
            ))
            el.append(Spacer(1, 6))

    # ── Morning Star ──────────────────────────────────────────────────────────
    ms = etf.get("Morning_Star") or {}
    if ms:
        el.append(_sec("Morningstar", styles))
        ms_rows = [
            ("Category",    _s(ms.get("Category"))),
            ("Rating",      _s(ms.get("Ratio"))),
            ("Ratio Date",  _s(ms.get("Ratio_Date"))),
            ("3-Year Sustainability Rating", _s(ms.get("Sustainability_Ratio"))),
        ]
        el.append(_kv_table(ms_rows))
        el.append(Spacer(1, 6))

    # ── Performance ───────────────────────────────────────────────────────────
    perf = etf.get("Performance") or {}
    if perf:
        el.append(_sec("Performance", styles))
        perf_rows = []
        label_map = {
            "1y_Volatility":        "1-Year Volatility",
            "3y_Volatility":        "3-Year Volatility",
            "3y_ExpReturn":         "3-Year Expected Return",
            "3y_SharpRatio":        "3-Year Sharpe Ratio",
            "Returns_YTD":          "Return YTD",
            "Returns_1Y":           "Return 1 Year",
            "Returns_3Y":           "Return 3 Years",
            "Returns_5Y":           "Return 5 Years",
            "Returns_10Y":          "Return 10 Years",
        }
        for key, label in label_map.items():
            v = perf.get(key)
            if v is not None:
                perf_rows.append((label, _f2(v)))
        if perf_rows:
            el.append(_data_table(
                ["Metric", "Value"],
                perf_rows,
                col_widths=[100*mm, CW - 100*mm],
                right_cols=[1],
            ))

    return el


# ══════════════════════════════════════════════════════════════════════════════
# MUTUAL FUND PAGES
# ══════════════════════════════════════════════════════════════════════════════

def _fund_page1(bundle: dict, styles: dict) -> list:
    el = []
    f   = bundle.get("fundamentals") or {}
    gen = f.get("General")         or {}
    mf  = f.get("MutualFund_Data") or {}
    rt  = bundle.get("realtime")   or {}

    # Title
    name = _s(gen.get("Name") or bundle.get("ticker"))
    el.append(Paragraph(name, styles["title"]))
    el.append(Paragraph(
        f"Mutual Fund · {_s(gen.get('Exchange'))} · {_s(gen.get('CurrencyCode'))} · "
        f"Updated: {_s(gen.get('UpdatedAt'))}",
        styles["subtitle"]
    ))
    el.append(_rule())

    # ── General Information ───────────────────────────────────────────────────
    el.append(_sec("General Information", styles))
    nav = rt.get("close") or rt.get("previousClose")
    kv_gen = [
        ("Fund Family",         _s(mf.get("Fund_Family"))),
        ("Fund Category",       _s(mf.get("Fund_Category") or gen.get("Category"))),
        ("Fund Style",          _s(mf.get("Fund_Style") or mf.get("Investment_Style"))),
        ("Inception Date",      _s(mf.get("Inception_Date") or gen.get("IPODate"))),
        ("Currency",            _s(mf.get("Currency") or gen.get("CurrencyCode"))),
        ("Domicile",            _s(mf.get("Domicile") or gen.get("CountryName"))),
        ("Current NAV",         _f2(nav)),
        ("Yield",               _pct(mf.get("Yield"))),
        ("Total Net Assets",    _s(mf.get("Total_Net_Assets") or mf.get("Net_Assets"))),
    ]
    el.append(_kv_table(kv_gen))
    el.append(Spacer(1, 4))

    # Fund Summary (description text)
    summary = _s(mf.get("Fund_Summary") or gen.get("Description") or "")
    if summary and summary != "—":
        el.append(_sec("Fund Summary", styles))
        el.append(Paragraph(summary[:600], styles["body"]))
        el.append(Spacer(1, 4))

    # ── Asset Allocation ──────────────────────────────────────────────────────
    aa = mf.get("Asset_Allocation") or {}
    if aa:
        el.append(_sec("Asset Allocation", styles))
        aa_map = {
            "Cash":           aa.get("Cash") or aa.get("cash"),
            "US Stocks":      aa.get("US Stocks") or aa.get("us_stock"),
            "Non-US Stocks":  aa.get("Non US Stocks") or aa.get("non_us_stock"),
            "Bonds":          aa.get("Bond") or aa.get("bonds"),
            "Other":          aa.get("Other") or aa.get("other"),
        }
        aa_rows = []
        for cat, vals in aa_map.items():
            if vals is None: continue
            if isinstance(vals, dict):
                pct_v = vals.get("Net_Assets_%") or vals.get("Long_%")
            else:
                pct_v = vals
            try: pct_f = float(pct_v)
            except: pct_f = None
            aa_rows.append((cat, f"{pct_f:.2f}%" if pct_f is not None else "—"))
        if aa_rows:
            el.append(_data_table(
                ["Asset Class", "Net Assets %"],
                aa_rows,
                col_widths=[100*mm, CW - 100*mm],
                right_cols=[1],
            ))
        el.append(Spacer(1, 4))

    # ── Value / Growth Measures ───────────────────────────────────────────────
    vg = mf.get("Value_Growth") or mf.get("Valuations_Growth") or {}
    if vg:
        el.append(_sec("Value & Growth Measures", styles))
        vg_rows = []
        label_map = {
            "Price/Prospective Earnings": "Price/Prospective Earnings",
            "Price/Book":                 "Price/Book",
            "Price/Sales":                "Price/Sales",
            "Price/Cash Flow":            "Price/Cash Flow",
            "Dividend-Yield Factor":      "Dividend Yield Factor",
            "Long-Term Earnings %":       "Long-Term Earnings %",
            "Historical Earnings %":      "Historical Earnings %",
            "Sales Growth %":             "Sales Growth %",
            "Cash-Flow Growth %":         "Cash-Flow Growth %",
            "Book-Value Growth %":        "Book-Value Growth %",
        }
        port = vg.get("Valuations_Rates_Portfolio") or vg
        cat  = vg.get("Valuations_Rates_To_Category") or {}
        for key, label in label_map.items():
            pv = port.get(key)
            cv = cat.get(key)
            if pv is not None or cv is not None:
                vg_rows.append((label, _f2(pv), _f2(cv) if cv is not None else "—"))
        if vg_rows:
            el.append(_data_table(
                ["Metric", "Fund", "Category"],
                vg_rows,
                col_widths=[90*mm, 45*mm, CW - 135*mm],
                right_cols=[1, 2],
            ))

    return el


def _fund_page2(bundle: dict, styles: dict) -> list:
    el = []
    f   = bundle.get("fundamentals") or {}
    gen = f.get("General")         or {}
    mf  = f.get("MutualFund_Data") or {}
    eod = bundle.get("eod")        or []

    el.append(PageBreak())

    # ── Price Chart ───────────────────────────────────────────────────────────
    el.append(_sec("NAV History — 5-Year Daily", styles))
    cur = _s(gen.get("CurrencyCode"))
    png = _render_price_chart(eod, bundle.get("ticker", ""), cur if cur != "—" else "")
    if png:
        el.append(Image(io.BytesIO(png), width=170*mm, height=52*mm, kind="proportional"))
    else:
        el.append(Paragraph("Chart not available.", styles["small"]))
    el.append(Spacer(1, 6))

    # ── Sector Weightings ─────────────────────────────────────────────────────
    sw = mf.get("Sector_Weights") or mf.get("Sector_Weightings") or {}
    if sw:
        el.append(_sec("Sector Weightings", styles))
        group_order = ["Cyclical", "Sensitive", "Defensive"]
        for group in group_order:
            group_data = sw.get(group) or {}
            if not group_data: continue
            el.append(Paragraph(group, styles["sub_section"]))
            sub_rows = []
            for sub_sector, vals in group_data.items():
                pct_v = vals.get("Amount_%") or vals.get("Equity_%") if isinstance(vals, dict) else vals
                try: pct_f = float(pct_v)
                except: pct_f = None
                sub_rows.append((sub_sector, f"{pct_f:.2f}%" if pct_f is not None else "—"))
            if sub_rows:
                el.append(_data_table(
                    ["Sub-Sector", "Weight %"],
                    sub_rows,
                    col_widths=[100*mm, CW - 100*mm],
                    right_cols=[1],
                ))
            el.append(Spacer(1, 3))
        el.append(Spacer(1, 4))

    # ── World Regions ─────────────────────────────────────────────────────────
    wr = mf.get("World_Regions") or {}
    if wr:
        el.append(_sec("World Regions", styles))
        wr_rows = []
        for region, vals in wr.items():
            pct_v = vals.get("Equity_%") or vals.get("Amount_%") if isinstance(vals, dict) else vals
            try: pct_f = float(pct_v)
            except: pct_f = None
            wr_rows.append((region, f"{pct_f:.2f}%" if pct_f is not None else "—"))
        if wr_rows:
            el.append(_data_table(
                ["Region", "Equity %"],
                wr_rows,
                col_widths=[100*mm, CW - 100*mm],
                right_cols=[1],
            ))
        el.append(Spacer(1, 4))

    # ── Market Classification ─────────────────────────────────────────────────
    mc = mf.get("Market_Classification") or {}
    if mc:
        el.append(_sec("Market Classification", styles))
        mc_rows = []
        for mkt_class, vals in mc.items():
            pct_v = vals.get("Equity_%") or vals.get("Amount_%") if isinstance(vals, dict) else vals
            try: pct_f = float(pct_v)
            except: pct_f = None
            mc_rows.append((mkt_class, f"{pct_f:.2f}%" if pct_f is not None else "—"))
        if mc_rows:
            el.append(_data_table(
                ["Classification", "Equity %"],
                mc_rows,
                col_widths=[100*mm, CW - 100*mm],
                right_cols=[1],
            ))

    return el


def _fund_page3(bundle: dict, styles: dict) -> list:
    el = []
    f   = bundle.get("fundamentals") or {}
    mf  = f.get("MutualFund_Data") or {}

    el.append(PageBreak())

    # ── Top Countries (bond funds) ────────────────────────────────────────────
    tc = mf.get("Top_Countries") or {}
    if tc:
        el.append(_sec("Top Countries (Bond Allocation)", styles))
        tc_rows = []
        for country, vals in tc.items():
            pct_v = vals.get("Bond_%") or vals.get("Amount_%") if isinstance(vals, dict) else vals
            try: pct_f = float(pct_v)
            except: pct_f = None
            tc_rows.append((country, f"{pct_f:.2f}%" if pct_f is not None else "—"))
        tc_rows.sort(key=lambda x: float(x[1].replace("%","")) if x[1] != "—" else 0, reverse=True)
        el.append(_data_table(
            ["Country", "Bond %"],
            tc_rows[:20],
            col_widths=[100*mm, CW - 100*mm],
            right_cols=[1],
        ))
        el.append(Spacer(1, 6))

    # ── Top Holdings ──────────────────────────────────────────────────────────
    holdings = mf.get("Top_Holdings") or mf.get("Holdings") or {}
    if holdings:
        el.append(_sec("Top Holdings", styles))
        h_rows = []
        items = list(holdings.items()) if isinstance(holdings, dict) else []
        for _, hdata in items[:20]:
            if isinstance(hdata, dict):
                h_rows.append((
                    _s(hdata.get("Name") or hdata.get("name")),
                    _s(hdata.get("Country") or "—"),
                    _s(hdata.get("Type") or "—"),
                    f"{float(hdata.get('Assets_%') or hdata.get('weight', 0)):.2f}%"
                    if (hdata.get("Assets_%") or hdata.get("weight")) else "—",
                ))
        if h_rows:
            el.append(_data_table(
                ["Name", "Country", "Type", "Weight %"],
                h_rows,
                col_widths=[80*mm, 35*mm, 30*mm, CW - 145*mm],
                right_cols=[3],
            ))
        el.append(Spacer(1, 6))

    # ── Fixed Income Details (for bond funds) ─────────────────────────────────
    fi = mf.get("Fixed_Income") or mf.get("Bond_Information") or {}
    if fi:
        el.append(_sec("Fixed Income Details", styles))
        fi_rows = [(k, _s(v)) for k, v in fi.items() if v not in (None, "", "NA")]
        if fi_rows:
            el.append(_kv_table(fi_rows))

    return el


# ══════════════════════════════════════════════════════════════════════════════
# UNKNOWN FUND — Dump all available fields
# ══════════════════════════════════════════════════════════════════════════════

def _unknown_page1(bundle: dict, styles: dict) -> list:
    el = []
    f   = bundle.get("fundamentals") or {}
    gen = f.get("General") or {}
    rt  = bundle.get("realtime") or {}

    name = _s(gen.get("Name") or bundle.get("ticker"))
    el.append(Paragraph(name, styles["title"]))
    el.append(Paragraph(
        f"Type: {_s(gen.get('Type'))} · {_s(gen.get('Exchange'))} · {_s(gen.get('CurrencyCode'))}",
        styles["subtitle"]
    ))
    el.append(_rule())

    el.append(_sec("All Available Top-Level Sections", styles))
    el.append(Paragraph(
        "Fund type could not be determined as ETF or Mutual Fund. "
        "All retrieved data sections are shown below.",
        styles["body"]
    ))
    el.append(Spacer(1, 4))

    for section_key, section_val in f.items():
        if not isinstance(section_val, dict): continue
        el.append(Paragraph(section_key, styles["sub_section"]))
        rows = []
        for k, v in section_val.items():
            if not isinstance(v, (dict, list)):
                rows.append((str(k), str(v)[:120]))
        if rows:
            el.append(_kv_table(rows[:20]))
        el.append(Spacer(1, 3))

    return el


# ══════════════════════════════════════════════════════════════════════════════
# MAIN GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

class FundFundamentalsPDFGenerator:
    """Renders a Fund Fundamentals report for either an ETF or a Mutual Fund."""

    def render(self, bundle: dict, output_path: str) -> None:
        styles = _build_styles()
        fund_type = bundle.get("fund_type", "UNKNOWN")

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"Fund Fundamentals — {bundle.get('ticker', '')}",
        )

        story = []

        if fund_type == "ETF":
            story += _etf_page1(bundle, styles)
            story += _etf_page2(bundle, styles)
            story += _etf_page3(bundle, styles)
        elif fund_type == "FUND":
            story += _fund_page1(bundle, styles)
            story += _fund_page2(bundle, styles)
            story += _fund_page3(bundle, styles)
        else:
            story += _unknown_page1(bundle, styles)

        # Footer note
        story.append(Spacer(1, 8))
        story.append(HRFlowable(width="100%", thickness=0.5, color=RULE))
        story.append(Paragraph(
            f"Source: EODHD All-In-One · Fetched: {bundle.get('fetched_at', '')} · "
            f"Endpoints OK: {bundle.get('endpoints_used', 0)}"
            + (f" · Errors: {', '.join(bundle.get('errors', []))}" if bundle.get("errors") else ""),
            ParagraphStyle("footer", fontName=BASE_FONT, fontSize=6,
                           textColor=CGRAY, alignment=TA_RIGHT),
        ))

        doc.build(story)
