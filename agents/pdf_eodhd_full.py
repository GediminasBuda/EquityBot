"""
pdf_eodhd_full.py — "EODHD Direct" comprehensive report.

Renders every section EODHD's All-In-One API exposes for a single ticker
into a 10-12 page PDF. The input is the dict produced by
data_sources/eodhd_all_in_one.py — no other data sources are mixed in.

Pages:
  1   Company Profile          (General + Officers)
  2   Market Snapshot          (Real-time quote · Technicals · 52W · MAs)
  3   Valuation & Profitability (Valuation block · Highlights · margins)
  4-5 Income Statement         (10-year P&L, full IS fields)
  6   Balance Sheet            (10-year BS, full BS fields)
  7   Cash Flow                (10-year CF, full CF fields)
  8   Price Chart              (5-year EOD daily close)
  9   Dividends                (history table + per-year chart)
  10  Splits + Shares History
  11  Insider Transactions     (recent buys/sells)
  12  News + Sentiments
  13  Analyst Upgrades / Downgrades + Earnings Trend
"""

from __future__ import annotations
import io
import logging
from datetime import datetime
from typing import Optional, Any

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

# ── Pantone 303 colour palette ────────────────────────────────────────────────
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
ESTCOL  = HexColor("#2E4A8A")
LGRAY   = HexColor("#F5F5F5")

GREEN_HEX = "#1A7E3D"
RED_HEX   = "#C0392B"
NAVY_HEX  = "#003F54"
MGRAY_HEX = "#666666"

BASE_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"
ITALIC_FONT = "Helvetica-Oblique"


# ── Formatters ────────────────────────────────────────────────────────────────
def _f(v: Any, dec: int = 2) -> str:
    if v is None or v == "": return "—"
    try: return f"{float(v):,.{dec}f}"
    except (ValueError, TypeError): return "—"

def _f0(v): return _f(v, 0)
def _f1(v): return _f(v, 1)
def _f2(v): return _f(v, 2)

def _m(v: Any, dec: int = 1) -> str:
    """Raw → millions display."""
    if v is None or v == "": return "—"
    try:
        f = float(v)
        return f"{f / 1_000_000:,.{dec}f}"
    except (ValueError, TypeError):
        return "—"

def _m_already(v: Any, dec: int = 1) -> str:
    """Value already in millions."""
    if v is None or v == "": return "—"
    try: return f"{float(v):,.{dec}f}"
    except (ValueError, TypeError): return "—"

def _pct(v: Any, dec: int = 1) -> str:
    if v is None or v == "": return "—"
    try: return f"{float(v) * 100:.{dec}f}%"
    except (ValueError, TypeError): return "—"

def _pct_raw(v: Any, dec: int = 2) -> str:
    if v is None or v == "": return "—"
    try: return f"{float(v):.{dec}f}%"
    except (ValueError, TypeError): return "—"

def _x(v: Any, dec: int = 1) -> str:
    if v is None or v == "": return "—"
    try: return f"{float(v):.{dec}f}x"
    except (ValueError, TypeError): return "—"

def _str(v) -> str:
    return str(v) if v not in (None, "", "NA") else "—"

def _date_short(d: str) -> str:
    if not d: return "—"
    return str(d)[:10]


# ── Style factory ─────────────────────────────────────────────────────────────
def _S(name, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, **kw)


def _styles() -> dict:
    return {
        "title": _S("title", fontName=BOLD_FONT, fontSize=14, textColor=NAVY,
                    leading=18, spaceAfter=4),
        "section": _S("sec", fontName=BOLD_FONT, fontSize=10, textColor=NAVY,
                      spaceBefore=8, spaceAfter=2, leading=13),
        "subsec": _S("ss", fontName=BOLD_FONT, fontSize=8, textColor=NAVY,
                     spaceBefore=4, spaceAfter=1, leading=10),
        "body": _S("body", fontName=BASE_FONT, fontSize=8, textColor=DGRAY,
                   leading=11, spaceAfter=3),
        "small": _S("sml", fontName=BASE_FONT, fontSize=7, textColor=MGRAY,
                    leading=9),
        "tiny": _S("tin", fontName=BASE_FONT, fontSize=6, textColor=CGRAY,
                   leading=8),
        "lbl": _S("lbl", fontName=BASE_FONT, fontSize=7.5, textColor=DGRAY,
                  alignment=TA_LEFT, leading=10),
        "lbl_bold": _S("lblb", fontName=BOLD_FONT, fontSize=7.5,
                       textColor=DGRAY, alignment=TA_LEFT, leading=10),
        "cell": _S("cel", fontName=BASE_FONT, fontSize=7.5, textColor=DGRAY,
                   alignment=TA_RIGHT, leading=10),
        "cell_l": _S("cell", fontName=BASE_FONT, fontSize=7.5, textColor=DGRAY,
                     alignment=TA_LEFT, leading=10),
        "cell_c": _S("celc", fontName=BASE_FONT, fontSize=7.5,
                     textColor=DGRAY, alignment=TA_CENTER, leading=10),
        "kv_lbl": _S("kvl", fontName=BASE_FONT, fontSize=8, textColor=MGRAY,
                     alignment=TA_LEFT, leading=11),
        "kv_val": _S("kvv", fontName=BOLD_FONT, fontSize=8, textColor=DGRAY,
                     alignment=TA_RIGHT, leading=11),
        "col_hdr": _S("ch", fontName=BOLD_FONT, fontSize=7,
                      textColor=NAVY, alignment=TA_CENTER, leading=9),
        "col_hdr_l": _S("chl", fontName=BOLD_FONT, fontSize=7,
                        textColor=NAVY, alignment=TA_LEFT, leading=9),
        "news_title": _S("nt", fontName=BOLD_FONT, fontSize=8,
                         textColor=NAVY, leading=10, spaceAfter=1),
        "news_meta": _S("nm", fontName=BASE_FONT, fontSize=6.5,
                        textColor=MGRAY, leading=8),
    }


# ── Page header / footer ──────────────────────────────────────────────────────
def _draw_header(canvas, doc, bundle: dict, report_date: str):
    canvas.saveState()
    f = bundle.get("fundamentals") or {}
    g = f.get("General") or {}
    name = g.get("Name") or bundle.get("ticker") or ""
    sub = " | ".join(filter(None, [
        g.get("CountryName"), g.get("Sector"), g.get("Industry"),
        f"ISIN: {g.get('ISIN')}" if g.get("ISIN") else None,
    ]))

    canvas.setFont(BOLD_FONT, 13)
    canvas.setFillColor(NAVY)
    canvas.drawString(ML, H - 12 * mm, name)

    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, H - 17 * mm, sub)

    # Right: price (real-time if available, otherwise last close)
    rt = bundle.get("realtime") or {}
    price = rt.get("close") or rt.get("previousClose")
    cur = g.get("CurrencyCode") or ""
    if price not in (None, "", "NA"):
        try:
            ps = f"{float(price):,.2f} {cur}".strip()
        except (ValueError, TypeError):
            ps = "—"
    else:
        ps = "—"
    canvas.setFont(BOLD_FONT, 8.5)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(W - MR, H - 11 * mm, ps)
    canvas.setFont(BASE_FONT, 7.5)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(W - MR, H - 16 * mm, report_date)

    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.6)
    canvas.line(ML, H - 20 * mm, W - MR, H - 20 * mm)

    canvas.setFont(BASE_FONT, 6.5)
    canvas.setFillColor(CGRAY)
    canvas.drawString(ML, 7 * mm,
        "Your Humble EquityBot | EODHD All-In-One data | For internal use only.")
    canvas.drawRightString(W - MR, 7 * mm,
        f"Page {doc.page} | {report_date}")
    canvas.restoreState()


def _sec(label: str, styles: dict):
    return Paragraph(label.upper(), styles["section"])


# ── Key-value 4-col grid ──────────────────────────────────────────────────────
def _kv_grid(rows: list[tuple], col_w: float, styles: dict) -> Table:
    """Build a 2x2 col key/value grid. rows = [(label, value), ...]"""
    if not rows:
        return Paragraph("No data available.", styles["small"])
    data = []
    for i in range(0, len(rows), 2):
        left = rows[i]
        right = rows[i + 1] if i + 1 < len(rows) else ("", "")
        data.append([
            Paragraph(left[0], styles["kv_lbl"]),
            Paragraph(str(left[1]), styles["kv_val"]),
            Paragraph(right[0], styles["kv_lbl"]),
            Paragraph(str(right[1]), styles["kv_val"]),
        ])
    lw = col_w * 0.42
    vw = col_w * 0.58
    t = Table(data, colWidths=[lw, vw, lw, vw])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
    ]))
    return t


def _data_table(rows: list[list], col_widths: list[float],
                styles: dict, with_header: bool = True) -> Table:
    """Standard ink-saving data table: white bg, navy underline below header.

    Side-padding is intentionally tiny (1.5pt vs ReportLab's default 6pt)
    so wide numeric values like "46,660,000" fit in the narrow per-year
    columns used by the 10-year financial statements without wrapping.
    """
    t = Table(rows, colWidths=col_widths)
    ts = [
        ("FONTNAME", (0, 0), (-1, -1), BASE_FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 1.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
        ("BACKGROUND", (0, 0), (-1, -1), white),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, BORDER),
    ]
    if with_header:
        ts += [
            ("FONTNAME", (0, 0), (-1, 0), BOLD_FONT),
            ("TEXTCOLOR", (0, 0), (-1, 0), NAVY),
            ("LINEBELOW", (0, 0), (-1, 0), 1.2, NAVY),
        ]
    t.setStyle(TableStyle(ts))
    return t


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — Company Profile
# ══════════════════════════════════════════════════════════════════════════════
def _page_profile(bundle: dict, styles: dict) -> list:
    el = []
    f = bundle.get("fundamentals") or {}
    g = f.get("General") or {}

    # Title + description
    el.append(_sec("Company Profile", styles))
    desc = g.get("Description") or "No description available."
    desc_short = desc[:1100] + ("…" if len(desc) > 1100 else "")
    el.append(Paragraph(desc_short, styles["body"]))
    el.append(Spacer(1, 6))

    # Identity block
    el.append(Paragraph("Identity & Listing", styles["subsec"]))
    ipo = g.get("IPODate") or "—"
    rows = [
        ("Code", _str(g.get("Code"))),
        ("Type", _str(g.get("Type"))),
        ("Name", _str(g.get("Name"))),
        ("Exchange", _str(g.get("Exchange"))),
        ("Primary Ticker", _str(g.get("PrimaryTicker"))),
        ("ISIN", _str(g.get("ISIN"))),
        ("LEI", _str(g.get("LEI"))),
        ("CIK", _str(g.get("CIK"))),
        ("Currency", _str(g.get("CurrencyCode"))),
        ("Country", _str(g.get("CountryName"))),
        ("Sector", _str(g.get("Sector"))),
        ("Industry", _str(g.get("Industry"))),
        ("GIC Sector", _str(g.get("GicSector"))),
        ("GIC Sub-Industry", _str(g.get("GicSubIndustry"))),
        ("Fiscal Year End", _str(g.get("FiscalYearEnd"))),
        ("IPO Date", _str(ipo)),
        ("Employees", _f0(g.get("FullTimeEmployees"))),
        ("Last EODHD Update", _str(g.get("UpdatedAt"))),
    ]
    el.append(_kv_grid(rows, CW / 2, styles))
    el.append(Spacer(1, 6))

    # Address
    el.append(Paragraph("Address & Contact", styles["subsec"]))
    addr = g.get("AddressData") or {}
    addr_rows = [
        ("Street", _str(addr.get("Street") or g.get("Address"))),
        ("City", _str(addr.get("City"))),
        ("ZIP", _str(addr.get("ZIP"))),
        ("Country", _str(addr.get("Country"))),
        ("Phone", _str(g.get("Phone"))),
        ("Website", _str(g.get("WebURL"))),
    ]
    el.append(_kv_grid(addr_rows, CW / 2, styles))
    el.append(Spacer(1, 6))

    # Officers
    officers = g.get("Officers") or {}
    if isinstance(officers, dict) and officers:
        el.append(Paragraph("Officers", styles["subsec"]))
        off_rows = [[
            Paragraph("Name", styles["col_hdr_l"]),
            Paragraph("Title", styles["col_hdr_l"]),
            Paragraph("Year Born", styles["col_hdr"]),
        ]]
        for k, o in list(officers.items())[:12]:
            if not isinstance(o, dict): continue
            off_rows.append([
                Paragraph(_str(o.get("Name")), styles["cell_l"]),
                Paragraph(_str(o.get("Title")), styles["cell_l"]),
                Paragraph(_str(o.get("YearBorn")), styles["cell_c"]),
            ])
        el.append(_data_table(off_rows, [CW * 0.30, CW * 0.55, CW * 0.15],
                              styles, with_header=True))

    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — Market Snapshot (Real-time + Technicals + 52W + Shares)
# ══════════════════════════════════════════════════════════════════════════════
def _page_market(bundle: dict, styles: dict) -> list:
    el = []
    f = bundle.get("fundamentals") or {}
    g = f.get("General") or {}
    rt = bundle.get("realtime") or {}
    tech = f.get("Technicals") or {}
    ss = f.get("SharesStats") or {}
    sd = f.get("SplitsDividends") or {}
    cur = g.get("CurrencyCode") or ""

    el.append(_sec("Real-Time Quote (/real-time)", styles))
    rt_rows = [
        ("Code", _str(rt.get("code"))),
        ("Timestamp", _str(rt.get("timestamp"))),
        ("GMT Offset", _str(rt.get("gmtoffset"))),
        ("Open", f"{_f(rt.get('open'))} {cur}".strip()),
        ("High", f"{_f(rt.get('high'))} {cur}".strip()),
        ("Low", f"{_f(rt.get('low'))} {cur}".strip()),
        ("Close", f"{_f(rt.get('close'))} {cur}".strip()),
        ("Previous Close", f"{_f(rt.get('previousClose'))} {cur}".strip()),
        ("Change", _f(rt.get("change"))),
        ("Change %", _pct_raw(rt.get("change_p"))),
        ("Volume", _f0(rt.get("volume"))),
    ]
    el.append(_kv_grid(rt_rows, CW / 2, styles))
    el.append(Spacer(1, 6))

    el.append(_sec("Technical Levels", styles))
    tech_rows = [
        ("52-Week High", f"{_f(tech.get('52WeekHigh'))} {cur}".strip()),
        ("52-Week Low",  f"{_f(tech.get('52WeekLow'))} {cur}".strip()),
        ("50-Day MA",    _f(tech.get("50DayMA"))),
        ("200-Day MA",   _f(tech.get("200DayMA"))),
        ("Beta",         _f2(tech.get("Beta"))),
        ("Shares Short", _f0(tech.get("SharesShort"))),
        ("Short Ratio",  _f2(tech.get("ShortRatio"))),
        ("Short %",      _pct_raw(tech.get("ShortPercent"))),
    ]
    el.append(_kv_grid(tech_rows, CW / 2, styles))
    el.append(Spacer(1, 6))

    el.append(_sec("Share Statistics", styles))
    shares_rows = [
        ("Shares Outstanding", _f0(ss.get("SharesOutstanding"))),
        ("Float",              _f0(ss.get("SharesFloat"))),
        ("% Insiders",         _pct_raw(ss.get("PercentInsiders"))),
        ("% Institutions",     _pct_raw(ss.get("PercentInstitutions"))),
        ("Shares Short",       _f0(ss.get("SharesShort"))),
        ("Short Prior Month",  _f0(ss.get("SharesShortPriorMonth"))),
    ]
    el.append(_kv_grid(shares_rows, CW / 2, styles))
    el.append(Spacer(1, 6))

    el.append(_sec("Dividend & Splits Snapshot", styles))
    div_rows = [
        ("Forward Annual Dividend Rate",  _f(sd.get("ForwardAnnualDividendRate"))),
        ("Forward Annual Dividend Yield", _pct(sd.get("ForwardAnnualDividendYield"))),
        ("Payout Ratio",                  _pct(sd.get("PayoutRatio"))),
        ("Dividend Date",                 _str(sd.get("DividendDate"))),
        ("Ex-Dividend Date",              _str(sd.get("ExDividendDate"))),
        ("Last Split Factor",             _str(sd.get("LastSplitFactor"))),
        ("Last Split Date",               _str(sd.get("LastSplitDate"))),
    ]
    el.append(_kv_grid(div_rows, CW / 2, styles))

    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 3 — Valuation & Profitability (Highlights + Valuation)
# ══════════════════════════════════════════════════════════════════════════════
def _page_valuation(bundle: dict, styles: dict) -> list:
    el = []
    f = bundle.get("fundamentals") or {}
    h = f.get("Highlights") or {}
    v = f.get("Valuation") or {}
    cur = (f.get("General") or {}).get("CurrencyCode") or ""

    el.append(_sec("Highlights (TTM)", styles))
    h_rows = [
        ("Market Cap",        f"{_m(h.get('MarketCapitalization'))} M {cur}".strip()),
        ("Market Cap (Mln)",  _f1(h.get("MarketCapitalizationMln"))),
        ("EBITDA",            f"{_m(h.get('EBITDA'))} M".strip()),
        ("Revenue TTM",       f"{_m(h.get('RevenueTTM'))} M".strip()),
        ("Gross Profit TTM",  f"{_m(h.get('GrossProfitTTM'))} M".strip()),
        ("PE Ratio",          _x(h.get("PERatio"))),
        ("PEG Ratio",         _f2(h.get("PEGRatio"))),
        ("Wall Street Target Price", _f(h.get("WallStreetTargetPrice"))),
        ("Book Value (per share)", _f2(h.get("BookValue"))),
        ("Dividend Share",    _f(h.get("DividendShare"))),
        ("Dividend Yield",    _pct(h.get("DividendYield"))),
        ("Earnings Share (TTM)",   _f2(h.get("EarningsShare"))),
        ("Diluted EPS TTM",   _f2(h.get("DilutedEpsTTM"))),
        ("EPS Estimate Current Year", _f2(h.get("EPSEstimateCurrentYear"))),
        ("EPS Estimate Next Year",    _f2(h.get("EPSEstimateNextYear"))),
        ("EPS Estimate Current Q",    _f2(h.get("EPSEstimateCurrentQuarter"))),
        ("EPS Estimate Next Q",       _f2(h.get("EPSEstimateNextQuarter"))),
        ("Most Recent Quarter", _str(h.get("MostRecentQuarter"))),
        ("Profit Margin",     _pct(h.get("ProfitMargin"))),
        ("Operating Margin TTM", _pct(h.get("OperatingMarginTTM"))),
        ("Return on Assets TTM", _pct(h.get("ReturnOnAssetsTTM"))),
        ("Return on Equity TTM", _pct(h.get("ReturnOnEquityTTM"))),
        ("Revenue / Share TTM",  _f2(h.get("RevenuePerShareTTM"))),
        ("Q Revenue Growth YoY",  _pct(h.get("QuarterlyRevenueGrowthYOY"))),
        ("Q Earnings Growth YoY", _pct(h.get("QuarterlyEarningsGrowthYOY"))),
    ]
    el.append(_kv_grid(h_rows, CW / 2, styles))
    el.append(Spacer(1, 8))

    el.append(_sec("Valuation Multiples", styles))
    v_rows = [
        ("Trailing P/E",        _x(v.get("TrailingPE"))),
        ("Forward P/E",         _x(v.get("ForwardPE"))),
        ("Price / Sales TTM",   _x(v.get("PriceSalesTTM"))),
        ("Price / Book (MRQ)",  _x(v.get("PriceBookMRQ"))),
        ("Enterprise Value",    f"{_m(v.get('EnterpriseValue'))} M".strip()),
        ("EV / Revenue",        _x(v.get("EnterpriseValueRevenue"))),
        ("EV / EBITDA",         _x(v.get("EnterpriseValueEbitda"))),
    ]
    el.append(_kv_grid(v_rows, CW / 2, styles))
    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGES 4-7 — Full Financial Statements (10-year)
# ══════════════════════════════════════════════════════════════════════════════
def _financials_section(
    bundle: dict, styles: dict, block_key: str, title: str,
    field_specs: list[tuple], n_periods: int = 10,
    period: str = "yearly",
    highlight_fields: Optional[list] = None,
) -> list:
    """
    Render a financial statement block (IS / BS / CF) as a multi-page table.

    field_specs: [(eodhd_field_name, display_label, formatter), ...]
                 formatter takes the raw string value, returns display string.
    period:      "yearly" or "quarterly" — which EODHD sub-block to read.
    """
    el = []
    f = bundle.get("fundamentals") or {}
    fin = f.get("Financials") or {}
    block = (fin.get(block_key) or {}).get(period) or {}
    if not block:
        el.append(_sec(title, styles))
        el.append(Paragraph("No data returned by EODHD.", styles["small"]))
        return el

    # Most recent N periods, chronological order (oldest → newest in columns)
    periods_desc = sorted(block.keys(), reverse=True)[:n_periods]
    years = list(reversed(periods_desc))     # asc for column display
    if not years:
        el.append(_sec(title, styles))
        el.append(Paragraph("No data returned by EODHD.", styles["small"]))
        return el

    n_cols = len(years)
    # Tighter label column when there are many years so every numeric
    # cell has room for full raw values like "46,660,000" without
    # wrapping onto a second line.
    if   n_cols <= 6:  label_w = 130
    elif n_cols <= 8:  label_w = 105
    else:              label_w = 80     # 9-10 cols: shrink hard
    data_w = (CW - label_w) / n_cols
    col_widths = [label_w] + [data_w] * n_cols

    el.append(_sec(title, styles))

    # Header row
    hdr = [Paragraph("Field (M)", styles["col_hdr_l"])]
    for y in years:
        hdr.append(Paragraph(y[:7], styles["col_hdr"]))   # show YYYY-MM
    rows = [hdr]

    fs = 6.5 if n_cols >= 9 else 7.5
    cell_style = ParagraphStyle("c", parent=styles["cell"], fontSize=fs, leading=9)
    lbl_style = ParagraphStyle("l", parent=styles["lbl"], fontSize=fs, leading=9)

    highlight_set = set(highlight_fields or [])
    highlight_row_indices: list[int] = []

    for i, (raw_field, label, fmt) in enumerate(field_specs):
        row = [Paragraph(label, lbl_style)]
        for y in years:
            val = (block.get(y) or {}).get(raw_field)
            row.append(Paragraph(fmt(val) if fmt else _m(val), cell_style))
        if raw_field in highlight_set:
            # +1 because header row is index 0
            highlight_row_indices.append(i + 1)
        rows.append(row)

    t = _data_table(rows, col_widths, styles)
    if highlight_row_indices:
        # Very-pale Pantone-303 tint — readable highlight, minimal toner
        ROW_HIGHLIGHT = HexColor("#E0EEF4")
        extras = []
        for idx in highlight_row_indices:
            extras.append(("BACKGROUND", (0, idx), (-1, idx), ROW_HIGHLIGHT))
        t.setStyle(TableStyle(extras))
    el.append(t)
    return el


def _page_income_stmt(bundle: dict, styles: dict) -> list:
    fields = [
        ("totalRevenue",                "Total Revenue", _m),
        ("costOfRevenue",               "Cost of Revenue", _m),
        ("grossProfit",                 "Gross Profit", _m),
        ("researchDevelopment",         "R&D", _m),
        ("sellingGeneralAdministrative","Selling, G&A", _m),
        ("sellingAndMarketingExpenses", "Selling & Marketing", _m),
        ("totalOperatingExpenses",      "Total Operating Expenses", _m),
        ("operatingIncome",             "Operating Income", _m),
        ("ebit",                        "EBIT", _m),
        ("ebitda",                      "EBITDA", _m),
        ("depreciationAndAmortization", "D&A", _m),
        ("interestIncome",              "Interest Income", _m),
        ("interestExpense",             "Interest Expense", _m),
        ("netInterestIncome",           "Net Interest Income", _m),
        ("incomeBeforeTax",             "Income Before Tax", _m),
        ("taxProvision",                "Tax Provision", _m),
        ("incomeTaxExpense",            "Income Tax Expense", _m),
        ("netIncomeFromContinuingOps",  "Net Income (Continuing Ops)", _m),
        ("discontinuedOperations",      "Discontinued Ops", _m),
        ("extraordinaryItems",          "Extraordinary Items", _m),
        ("minorityInterest",            "Minority Interest", _m),
        ("netIncome",                   "Net Income", _m),
        ("netIncomeApplicableToCommonShares", "NI Applicable to Common", _m),
    ]
    annual_el = _financials_section(
        bundle, styles, "Income_Statement",
        "Income Statement — 10y (millions, reporting currency)",
        fields, n_periods=10, period="yearly",
    )
    quarterly_el = _financials_section(
        bundle, styles, "Income_Statement",
        "Income Statement — Last 8 Quarters (millions, reporting currency)",
        fields, n_periods=8, period="quarterly",
    )
    return annual_el + [PageBreak()] + quarterly_el


def _page_balance_sheet(bundle: dict, styles: dict) -> list:
    fields = [
        ("totalAssets",                "Total Assets", _m),
        ("totalCurrentAssets",         "Current Assets", _m),
        ("cash",                       "Cash", _m),
        ("shortTermInvestments",       "ST Investments", _m),
        ("netReceivables",             "Net Receivables", _m),
        ("inventory",                  "Inventory", _m),
        ("otherCurrentAssets",         "Other Current Assets", _m),
        ("nonCurrentAssetsTotal",      "Non-Current Assets", _m),
        ("propertyPlantEquipment",     "PP&E (net)", _m),
        ("goodWill",                   "Goodwill", _m),
        ("intangibleAssets",           "Intangibles", _m),
        ("longTermInvestments",        "LT Investments", _m),
        ("otherAssets",                "Other Assets", _m),
        ("totalLiab",                  "Total Liabilities", _m),
        ("totalCurrentLiabilities",    "Current Liabilities", _m),
        ("accountsPayable",            "Accounts Payable", _m),
        ("shortTermDebt",              "Short-Term Debt", _m),
        ("longTermDebt",               "Long-Term Debt", _m),
        ("shortLongTermDebtTotal",     "Total Debt (ST+LT)", _m),
        ("capitalLeaseObligations",    "Capital Leases", _m),
        ("nonCurrentLiabilitiesTotal", "Non-Current Liabilities", _m),
        ("netDebt",                    "Net Debt", _m),
        ("totalStockholderEquity",     "Stockholder Equity", _m),
        ("retainedEarnings",           "Retained Earnings", _m),
        ("commonStock",                "Common Stock (par)", _m),
        ("commonStockSharesOutstanding","Shares Outstanding", _f0),
        ("netTangibleAssets",          "Net Tangible Assets", _m),
        ("netWorkingCapital",          "Net Working Capital", _m),
        ("netInvestedCapital",         "Net Invested Capital", _m),
    ]
    annual_el = _financials_section(
        bundle, styles, "Balance_Sheet",
        "Balance Sheet — 10y (millions, reporting currency)",
        fields, n_periods=10, period="yearly",
        # Light blue tint behind Shares Outstanding so the eye locks
        # onto the only-non-monetary row in the table.
        highlight_fields=["commonStockSharesOutstanding"],
    )
    quarterly_el = _financials_section(
        bundle, styles, "Balance_Sheet",
        "Balance Sheet — Last 8 Quarters (millions, reporting currency)",
        fields, n_periods=8, period="quarterly",
        highlight_fields=["commonStockSharesOutstanding"],
    )
    return annual_el + [PageBreak()] + quarterly_el


def _page_cash_flow(bundle: dict, styles: dict) -> list:
    fields = [
        ("totalCashFromOperatingActivities", "Cash from Operations", _m),
        ("netIncome",                        "Net Income (CF stmt)", _m),
        ("depreciation",                     "Depreciation", _m),
        ("stockBasedCompensation",           "Stock-Based Comp", _m),
        ("changeInWorkingCapital",           "Change in WC", _m),
        ("changeToAccountReceivables",       "Δ Receivables", _m),
        ("changeToInventory",                "Δ Inventory", _m),
        ("changeToLiabilities",              "Δ Liabilities", _m),
        ("otherNonCashItems",                "Other Non-Cash", _m),
        ("totalCashflowsFromInvestingActivities", "Cash from Investing", _m),
        ("capitalExpenditures",              "CapEx", _m),
        ("investments",                      "Investments", _m),
        ("otherCashflowsFromInvestingActivities", "Other Investing CF", _m),
        ("totalCashFromFinancingActivities", "Cash from Financing", _m),
        ("dividendsPaid",                    "Dividends Paid", _m),
        ("netBorrowings",                    "Net Borrowings", _m),
        ("salePurchaseOfStock",              "Sale/Purchase Stock", _m),
        ("issuanceOfCapitalStock",           "Issuance Capital Stock", _m),
        ("otherCashflowsFromFinancingActivities", "Other Financing CF", _m),
        ("exchangeRateChanges",              "FX Changes", _m),
        ("changeInCash",                     "Change in Cash", _m),
        ("beginPeriodCashFlow",              "Begin Period Cash", _m),
        ("endPeriodCashFlow",                "End Period Cash", _m),
        ("freeCashFlow",                     "Free Cash Flow", _m),
    ]
    annual_el = _financials_section(
        bundle, styles, "Cash_Flow",
        "Cash Flow — 10y (millions, reporting currency)",
        fields, n_periods=10, period="yearly",
    )
    quarterly_el = _financials_section(
        bundle, styles, "Cash_Flow",
        "Cash Flow — Last 8 Quarters (millions, reporting currency)",
        fields, n_periods=8, period="quarterly",
    )
    return annual_el + [PageBreak()] + quarterly_el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 8 — Price chart (5-year daily close from EOD endpoint)
# ══════════════════════════════════════════════════════════════════════════════
def _render_price_chart_png(eod_data: list, ticker: str,
                            currency: str = "") -> Optional[bytes]:
    if not eod_data: return None
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
        adj = row.get("adjusted_close")
        raw = row.get("close")
        p = adj if adj not in (None, "", "NA", 0) else raw
        if d and p is not None:
            try:
                dates.append(_dt.strptime(d, "%Y-%m-%d"))
                prices.append(float(p))
            except (ValueError, TypeError):
                continue
    if not dates: return None

    fig, ax = plt.subplots(figsize=(7.2, 2.4), dpi=130)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.plot(dates, prices, color=NAVY_HEX, linewidth=1.1)
    ax.fill_between(dates, prices, min(prices) * 0.98,
                    color=NAVY_HEX, alpha=0.08, linewidth=0)

    last_p = prices[-1]
    ax.scatter([dates[-1]], [last_p], color="#C9843E", s=18, zorder=5,
               edgecolors="white", linewidths=0.6)
    ax.annotate(f"{last_p:,.2f} {currency}".strip(),
                xy=(dates[-1], last_p),
                xytext=(-6, 6), textcoords="offset points",
                fontsize=7.5, color="#C9843E", fontweight="bold", ha="right")

    ax.set_title(f"{ticker} · 5-Year Daily Close (EODHD /eod)",
                 fontsize=9, color=NAVY_HEX, fontweight="bold",
                 loc="left", pad=4)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(axis="both", labelsize=7, colors="#666666", length=2)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#DDDDDD")
        ax.spines[sp].set_linewidth(0.5)
    ax.grid(True, axis="y", color="#DDDDDD", linewidth=0.4, alpha=0.7)
    fig.text(0.99, 0.02, f"Source: EODHD /eod — split-adjusted close ({len(dates)} obs)",
             ha="right", va="bottom", fontsize=5.5, color="#666666",
             style="italic")
    fig.tight_layout(pad=0.4)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _page_price_chart(bundle: dict, styles: dict) -> list:
    el = []
    el.append(_sec("Price History — 5-Year Daily Close (/eod)", styles))
    eod = bundle.get("eod") or []
    if not eod:
        el.append(Paragraph("No EOD data returned by EODHD.", styles["small"]))
        return el

    f = bundle.get("fundamentals") or {}
    cur = (f.get("General") or {}).get("CurrencyCode") or ""
    png = _render_price_chart_png(eod, bundle.get("ticker", ""), cur)
    if png:
        img = Image(io.BytesIO(png), width=170 * mm, height=52 * mm,
                    kind="proportional")
        el.append(img)
        el.append(Spacer(1, 6))

    # Summary statistics from EOD data — prefer split-adjusted close
    def _adj_close(r):
        adj = r.get("adjusted_close")
        return float(adj) if adj not in (None, "", "NA", 0) else (
            float(r["close"]) if r.get("close") not in (None, "", "NA") else None
        )
    closes = [c for r in eod if isinstance(r, dict) for c in [_adj_close(r)] if c is not None]
    volumes = [
        float(r["volume"]) for r in eod
        if isinstance(r, dict) and r.get("volume") not in (None, "", "NA", 0)
    ]
    if closes:
        first, last = closes[0], closes[-1]
        ret = (last / first - 1) * 100 if first > 0 else None
        rows = [
            ("Observations",     str(len(closes))),
            ("First Date",       _date_short((eod[0] or {}).get("date"))),
            ("Last Date",        _date_short((eod[-1] or {}).get("date"))),
            ("First Close",      f"{first:,.2f} {cur}"),
            ("Last Close",       f"{last:,.2f} {cur}"),
            ("Period Return",    f"{ret:+.1f}%" if ret is not None else "—"),
            ("Period High",      f"{max(closes):,.2f} {cur}"),
            ("Period Low",       f"{min(closes):,.2f} {cur}"),
            ("Average Volume",   _f0(sum(volumes) / len(volumes)) if volumes else "—"),
            ("Total Volume",     _f0(sum(volumes)) if volumes else "—"),
        ]
        el.append(Spacer(1, 4))
        el.append(Paragraph("Summary Statistics", styles["subsec"]))
        el.append(_kv_grid(rows, CW / 2, styles))

    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 9 — Dividends history table + chart
# ══════════════════════════════════════════════════════════════════════════════
def _render_dividend_chart_png(divs: list, ticker: str,
                               currency: str = "") -> Optional[bytes]:
    if not divs: return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    # Aggregate by calendar year
    year_totals: dict[int, float] = {}
    for r in divs:
        if not isinstance(r, dict): continue
        d = r.get("date") or r.get("paymentDate")
        v = r.get("value")
        if not d or v in (None, "", "NA"): continue
        try:
            yr = int(str(d)[:4])
            year_totals[yr] = year_totals.get(yr, 0.0) + float(v)
        except (ValueError, TypeError):
            continue
    if not year_totals: return None

    years = sorted(year_totals.keys())
    values = [year_totals[y] for y in years]

    fig, ax = plt.subplots(figsize=(7.2, 2.2), dpi=130)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.bar(years, values, color=NAVY_HEX, alpha=0.85,
           edgecolor=NAVY_HEX, linewidth=0.5)
    ax.set_title(f"{ticker} · Dividend per Share by Year (/div)",
                 fontsize=9, color=NAVY_HEX, fontweight="bold",
                 loc="left", pad=4)
    ax.tick_params(axis="both", labelsize=7, colors="#666666", length=2)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#DDDDDD")
        ax.spines[sp].set_linewidth(0.5)
    ax.grid(True, axis="y", color="#DDDDDD", linewidth=0.4, alpha=0.7)
    if currency:
        ax.set_ylabel(currency, fontsize=7, color="#666666")
    fig.tight_layout(pad=0.4)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _page_dividends(bundle: dict, styles: dict) -> list:
    el = []
    el.append(_sec("Dividend History (/div)", styles))
    divs = bundle.get("dividends") or []
    if not divs:
        el.append(Paragraph("No dividend data returned by EODHD.", styles["small"]))
        return el

    f = bundle.get("fundamentals") or {}
    cur = (f.get("General") or {}).get("CurrencyCode") or ""

    png = _render_dividend_chart_png(divs, bundle.get("ticker", ""), cur)
    if png:
        img = Image(io.BytesIO(png), width=170 * mm, height=48 * mm,
                    kind="proportional")
        el.append(img)
        el.append(Spacer(1, 4))

    # Recent dividend table (last 20)
    hdr = [
        Paragraph("Ex-Date", styles["col_hdr_l"]),
        Paragraph("Pay-Date", styles["col_hdr_l"]),
        Paragraph("Declaration", styles["col_hdr_l"]),
        Paragraph("Record", styles["col_hdr_l"]),
        Paragraph("Currency", styles["col_hdr_l"]),
        Paragraph("Period", styles["col_hdr_l"]),
        Paragraph("Value", styles["col_hdr"]),
        Paragraph("Unadj.", styles["col_hdr"]),
    ]
    rows = [hdr]
    for r in divs[:25]:
        if not isinstance(r, dict): continue
        rows.append([
            Paragraph(_date_short(r.get("date")), styles["cell_l"]),
            Paragraph(_date_short(r.get("paymentDate")), styles["cell_l"]),
            Paragraph(_date_short(r.get("declarationDate")), styles["cell_l"]),
            Paragraph(_date_short(r.get("recordDate")), styles["cell_l"]),
            Paragraph(_str(r.get("currency")), styles["cell_l"]),
            Paragraph(_str(r.get("period")), styles["cell_l"]),
            Paragraph(_f(r.get("value")), styles["cell"]),
            Paragraph(_f(r.get("unadjustedValue")), styles["cell"]),
        ])
    col_w = [
        CW * 0.13, CW * 0.13, CW * 0.13, CW * 0.13,
        CW * 0.10, CW * 0.12, CW * 0.13, CW * 0.13,
    ]
    el.append(_data_table(rows, col_w, styles))
    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 10 — Splits
# ══════════════════════════════════════════════════════════════════════════════
def _page_splits(bundle: dict, styles: dict) -> list:
    el = []
    el.append(_sec("Stock Splits (/splits)", styles))
    splits = bundle.get("splits") or []
    if not splits:
        el.append(Paragraph("No stock split data returned by EODHD.", styles["small"]))
        return el

    hdr = [
        Paragraph("Date", styles["col_hdr_l"]),
        Paragraph("Split Factor", styles["col_hdr_l"]),
    ]
    rows = [hdr]
    for r in splits[:40]:
        if not isinstance(r, dict): continue
        rows.append([
            Paragraph(_date_short(r.get("date")), styles["cell_l"]),
            Paragraph(_str(r.get("split")), styles["cell_l"]),
        ])
    el.append(_data_table(rows, [CW * 0.4, CW * 0.6], styles))

    # Outstanding shares history from fundamentals (annual + quarterly)
    fund = bundle.get("fundamentals") or {}
    os_block = fund.get("outstandingShares") or {}
    annual = os_block.get("annual") or {}
    if annual:
        el.append(Spacer(1, 8))
        el.append(_sec("Outstanding Shares — Annual History", styles))
        rows2 = [[
            Paragraph("Year", styles["col_hdr_l"]),
            Paragraph("Date", styles["col_hdr_l"]),
            Paragraph("Shares (mln)", styles["col_hdr"]),
            Paragraph("Shares (full)", styles["col_hdr"]),
        ]]
        # Sort by date desc
        entries = sorted(
            [(v.get("date"), v) for v in annual.values() if isinstance(v, dict)],
            key=lambda x: str(x[0] or ""), reverse=True,
        )
        for date, row in entries[:25]:
            rows2.append([
                Paragraph(_str(date), styles["cell_l"]),
                Paragraph(_str(row.get("dateFormatted")), styles["cell_l"]),
                Paragraph(_f1(row.get("sharesMln")), styles["cell"]),
                Paragraph(_f0(row.get("shares")), styles["cell"]),
            ])
        el.append(_data_table(rows2, [CW * 0.2, CW * 0.3, CW * 0.25, CW * 0.25],
                              styles))
    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 11 — Insider Transactions
# ══════════════════════════════════════════════════════════════════════════════
def _page_insider(bundle: dict, styles: dict) -> list:
    el = []
    el.append(_sec("Insider Transactions (/insider-transactions)", styles))
    rows_raw = bundle.get("insider") or []
    if not rows_raw:
        el.append(Paragraph(
            "No insider transactions returned by EODHD. "
            "(This endpoint primarily covers US-listed equities.)",
            styles["small"]))
        return el

    hdr = [
        Paragraph("Date", styles["col_hdr_l"]),
        Paragraph("Owner", styles["col_hdr_l"]),
        Paragraph("Title", styles["col_hdr_l"]),
        Paragraph("Tx Type", styles["col_hdr_l"]),
        Paragraph("Shares", styles["col_hdr"]),
        Paragraph("Price", styles["col_hdr"]),
        Paragraph("Value", styles["col_hdr"]),
    ]
    rows = [hdr]
    for r in rows_raw[:35]:
        if not isinstance(r, dict): continue
        rows.append([
            Paragraph(_date_short(r.get("transactionDate") or r.get("reportDate")),
                      styles["cell_l"]),
            Paragraph(_str(r.get("ownerName") or r.get("owner")),
                      styles["cell_l"]),
            Paragraph(_str(r.get("ownerRelationship") or r.get("title")),
                      styles["cell_l"]),
            Paragraph(_str(r.get("transactionCode") or r.get("transactionAcquiredDisposedCode")),
                      styles["cell_l"]),
            Paragraph(_f0(r.get("transactionAmount") or r.get("shares")),
                      styles["cell"]),
            Paragraph(_f2(r.get("transactionPrice") or r.get("price")),
                      styles["cell"]),
            Paragraph(_f0(r.get("transactionAmountValue") or r.get("value")),
                      styles["cell"]),
        ])
    col_w = [CW * 0.11, CW * 0.20, CW * 0.18, CW * 0.10,
             CW * 0.13, CW * 0.13, CW * 0.15]
    el.append(_data_table(rows, col_w, styles))
    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 12 — News + Sentiments
# ══════════════════════════════════════════════════════════════════════════════
def _page_news(bundle: dict, styles: dict) -> list:
    el = []
    el.append(_sec("News (/news)", styles))
    news = bundle.get("news") or []
    if not news:
        el.append(Paragraph("No news returned by EODHD.", styles["small"]))
    else:
        for n in news[:10]:
            if not isinstance(n, dict): continue
            title = n.get("title") or "—"
            link = n.get("link") or ""
            date = _date_short(n.get("date"))
            sym = n.get("symbols") or ""
            if isinstance(sym, list):
                sym = ", ".join(sym[:3])
            polarity = (n.get("sentiment") or {}).get("polarity") if isinstance(n.get("sentiment"), dict) else None
            pol_str = ""
            if polarity not in (None, "", "NA"):
                try:
                    p = float(polarity)
                    if p > 0.1:
                        pol_str = f' <font color="{GREEN_HEX}">▲ {p:+.2f}</font>'
                    elif p < -0.1:
                        pol_str = f' <font color="{RED_HEX}">▼ {p:+.2f}</font>'
                    else:
                        pol_str = f' <font color="{MGRAY_HEX}">≈ {p:+.2f}</font>'
                except (ValueError, TypeError):
                    pol_str = ""
            el.append(Paragraph(title[:180], styles["news_title"]))
            meta = f"{date} · {sym}{pol_str}"
            el.append(Paragraph(meta, styles["news_meta"]))
            if link:
                el.append(Paragraph(f'<font color="{MGRAY_HEX}" size="6">{link[:120]}</font>',
                                     styles["tiny"]))
            el.append(Spacer(1, 2))

    el.append(Spacer(1, 6))
    el.append(_sec("Sentiments — Time Series (/sentiments)", styles))
    sent = bundle.get("sentiments") or {}
    if not sent:
        el.append(Paragraph("No sentiment data returned by EODHD.", styles["small"]))
        return el

    # EODHD returns { ticker: [{date, count, normalized}, ...] }
    first_key = next(iter(sent.keys()), None)
    series = sent.get(first_key) if first_key else None
    if not isinstance(series, list) or not series:
        el.append(Paragraph("Sentiment series unavailable.", styles["small"]))
        return el

    hdr = [
        Paragraph("Date", styles["col_hdr_l"]),
        Paragraph("Article Count", styles["col_hdr"]),
        Paragraph("Normalized", styles["col_hdr"]),
    ]
    rows = [hdr]
    # Most recent 20 days
    for s in series[-20:]:
        if not isinstance(s, dict): continue
        rows.append([
            Paragraph(_date_short(s.get("date")), styles["cell_l"]),
            Paragraph(_f0(s.get("count")), styles["cell"]),
            Paragraph(_f2(s.get("normalized")), styles["cell"]),
        ])
    el.append(_data_table(rows, [CW * 0.4, CW * 0.3, CW * 0.3], styles))
    return el


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 13 — Analyst Upgrades/Downgrades + Earnings Trend
# ══════════════════════════════════════════════════════════════════════════════
def _page_analysts(bundle: dict, styles: dict) -> list:
    el = []
    el.append(_sec("Analyst Upgrades / Downgrades (/upgrades-downgrades)", styles))
    ud = bundle.get("upgrades") or []
    if not ud:
        el.append(Paragraph("No analyst rating changes returned by EODHD.",
                            styles["small"]))
    else:
        hdr = [
            Paragraph("Date", styles["col_hdr_l"]),
            Paragraph("Firm", styles["col_hdr_l"]),
            Paragraph("Action", styles["col_hdr_l"]),
            Paragraph("From Rating", styles["col_hdr_l"]),
            Paragraph("To Rating", styles["col_hdr_l"]),
            Paragraph("Price Target", styles["col_hdr"]),
        ]
        rows = [hdr]
        for r in ud[:35]:
            if not isinstance(r, dict): continue
            rows.append([
                Paragraph(_date_short(r.get("date")), styles["cell_l"]),
                Paragraph(_str(r.get("analyst") or r.get("firm")),
                          styles["cell_l"]),
                Paragraph(_str(r.get("action")), styles["cell_l"]),
                Paragraph(_str(r.get("from_grade")), styles["cell_l"]),
                Paragraph(_str(r.get("to_grade")), styles["cell_l"]),
                Paragraph(_f(r.get("price_target")), styles["cell"]),
            ])
        el.append(_data_table(rows, [CW*0.12, CW*0.22, CW*0.14,
                                     CW*0.18, CW*0.18, CW*0.16], styles))

    el.append(Spacer(1, 8))

    # Earnings trend from fundamentals
    el.append(_sec("Earnings Trend (Fundamentals.Earnings)", styles))
    earnings = (bundle.get("fundamentals") or {}).get("Earnings") or {}
    trend = earnings.get("Trend") or {}
    if not trend:
        el.append(Paragraph("No earnings trend data.", styles["small"]))
    else:
        hdr = [
            Paragraph("Period", styles["col_hdr_l"]),
            Paragraph("Date", styles["col_hdr_l"]),
            Paragraph("Type", styles["col_hdr_l"]),
            Paragraph("EPS Avg", styles["col_hdr"]),
            Paragraph("EPS High", styles["col_hdr"]),
            Paragraph("EPS Low", styles["col_hdr"]),
            Paragraph("# Analysts", styles["col_hdr"]),
            Paragraph("Growth", styles["col_hdr"]),
        ]
        rows = [hdr]
        sorted_trend = sorted(trend.items(), key=lambda x: str(x[0] or ""),
                              reverse=True)
        for date, t in sorted_trend[:15]:
            if not isinstance(t, dict): continue
            rows.append([
                Paragraph(_str(t.get("period")), styles["cell_l"]),
                Paragraph(_date_short(t.get("date")), styles["cell_l"]),
                Paragraph(_str(t.get("period")), styles["cell_l"]),
                Paragraph(_f2(t.get("earningsEstimateAvg")), styles["cell"]),
                Paragraph(_f2(t.get("earningsEstimateHigh")), styles["cell"]),
                Paragraph(_f2(t.get("earningsEstimateLow")), styles["cell"]),
                Paragraph(_f0(t.get("earningsEstimateNumberOfAnalysts")),
                          styles["cell"]),
                Paragraph(_pct(t.get("earningsEstimateGrowth")), styles["cell"]),
            ])
        el.append(_data_table(rows, [CW*0.10, CW*0.12, CW*0.10, CW*0.11,
                                     CW*0.11, CW*0.11, CW*0.12, CW*0.13],
                              styles))

    # Quarterly earnings history with surprise
    el.append(Spacer(1, 8))
    el.append(_sec("Earnings History — Quarterly Actuals + Surprise", styles))
    hist = earnings.get("History") or {}
    if hist:
        hdr = [
            Paragraph("Report Date", styles["col_hdr_l"]),
            Paragraph("Period End", styles["col_hdr_l"]),
            Paragraph("Before/After", styles["col_hdr_l"]),
            Paragraph("EPS Actual", styles["col_hdr"]),
            Paragraph("EPS Estimate", styles["col_hdr"]),
            Paragraph("Difference", styles["col_hdr"]),
            Paragraph("Surprise %", styles["col_hdr"]),
        ]
        rows = [hdr]
        sorted_hist = sorted(hist.items(), key=lambda x: str(x[0] or ""),
                             reverse=True)
        for date, h in sorted_hist[:12]:
            if not isinstance(h, dict): continue
            rows.append([
                Paragraph(_date_short(h.get("reportDate")), styles["cell_l"]),
                Paragraph(_date_short(h.get("date")), styles["cell_l"]),
                Paragraph(_str(h.get("beforeAfterMarket")), styles["cell_l"]),
                Paragraph(_f2(h.get("epsActual")), styles["cell"]),
                Paragraph(_f2(h.get("epsEstimate")), styles["cell"]),
                Paragraph(_f2(h.get("epsDifference")), styles["cell"]),
                Paragraph(_pct_raw(h.get("surprisePercent")), styles["cell"]),
            ])
        el.append(_data_table(rows, [CW*0.14, CW*0.13, CW*0.14, CW*0.14,
                                     CW*0.15, CW*0.13, CW*0.17], styles))
    return el


# ══════════════════════════════════════════════════════════════════════════════
# Status / endpoint summary footer page
# ══════════════════════════════════════════════════════════════════════════════
def _page_status(bundle: dict, styles: dict) -> list:
    el = []
    el.append(_sec("Data Source Status", styles))
    used = bundle.get("endpoints_used", 0)
    errors = bundle.get("errors", [])
    rows = [
        ("EODHD Ticker",      _str(bundle.get("eodhd_ticker"))),
        ("Yahoo Ticker",      _str(bundle.get("ticker"))),
        ("Fetched At (UTC)",  _str(bundle.get("fetched_at"))),
        ("Endpoints OK",      str(used)),
        ("Endpoints Failed",  str(len(errors))),
    ]
    el.append(_kv_grid(rows, CW / 2, styles))

    if errors:
        el.append(Spacer(1, 4))
        el.append(Paragraph(
            f"<b>Missing data:</b> {', '.join(errors)}",
            styles["small"]))
    el.append(Spacer(1, 8))
    el.append(Paragraph(
        "<i>All data fetched live from EODHD's All-In-One API "
        "subscription. No other data sources were consulted.</i>",
        styles["small"]))
    return el


# ══════════════════════════════════════════════════════════════════════════════
# Main render entry point
# ══════════════════════════════════════════════════════════════════════════════
class EODHDFullGenerator:
    """Render the full EODHD All-In-One report to a PDF file."""

    def render(self, bundle: dict, output_path: str) -> str:
        report_date = datetime.now().strftime("%d %b %Y")
        styles = _styles()
        ticker = bundle.get("ticker") or "?"

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"{ticker} — EODHD All-In-One",
            author="Your Humble EquityBot",
        )

        def _on_page(canvas, doc):
            _draw_header(canvas, doc, bundle, report_date)

        story = []
        # Page sequence
        pages = [
            _page_profile,
            _page_market,
            _page_valuation,
            _page_income_stmt,
            _page_balance_sheet,
            _page_cash_flow,
            _page_price_chart,
            _page_dividends,
            _page_splits,
            _page_insider,
            _page_news,
            _page_analysts,
            _page_status,
        ]
        for i, page_fn in enumerate(pages):
            try:
                els = page_fn(bundle, styles)
                if els:
                    story.extend(els)
                    if i < len(pages) - 1:
                        story.append(PageBreak())
            except Exception as e:
                logger.warning(f"[pdf_eodhd_full] page {page_fn.__name__} failed: {e}")
                story.append(Paragraph(
                    f"<i>Could not render section: {e}</i>",
                    styles["small"]))
                if i < len(pages) - 1:
                    story.append(PageBreak())

        doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
        logger.info(f"EODHD Full PDF written: {output_path}")
        return output_path
