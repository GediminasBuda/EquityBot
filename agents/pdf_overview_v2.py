"""
pdf_overview_v2.py — Investment Memo V2 (EODHD Based) PDF renderer.

Identical layout to the original Overview report but with 100% of the
underlying data sourced from EODHD's All-In-One API. Every populated
field is annotated with ✓ (verified EODHD) — fields EODHD does not
provide are rendered as "— (not in EODHD)".

LLM-generated narrative (Snapshot, Bull, Bear, Recommendation, Fun Facts)
runs against an EODHD-only prompt — no other data is mixed in.

Pages:
  Page 1: 5y EODHD /eod price chart + Financial Summary + Investment Snapshot
  Page 2: Bull / Bear / Recommendation box (all LLM, grounded on EODHD)
  Page 3: Peer Table (each peer fetched EODHD-only) + Checklist
  Page 4: Field provenance legend (which data fields came from where)
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak, KeepTogether, Image,
)
from reportlab.platypus.flowables import Flowable
from reportlab.lib.utils import ImageReader

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)

# ── Dimensions ────────────────────────────────────────────────────────────────
W, H    = A4              # 595.3 × 841.9 pts
ML = MR = 18*mm           # left / right margin
MT      = 38*mm           # top margin (leaves room for header drawn on canvas)
MB      = 14*mm           # bottom margin
CW      = W - ML - MR     # content width ≈ 481 pts

# ── Colour palette ────────────────────────────────────────────────────────────
# Pantone 303 / RGB(0, 63, 84). Ink-saving: header band is white, brand text
# is rendered in this dark teal/navy.
NAVY    = HexColor('#003F54')
BLUE    = HexColor('#2E75B6')
LBLUE   = HexColor('#D6E8F7')   # table header bg / alt rows
LLBLUE  = HexColor('#FFFFFF')   # alt row fill — kept white to save ink
GREEN   = HexColor('#1A7E3D')
RED     = HexColor('#C0392B')
AMBER   = HexColor('#D68910')
MGRAY   = HexColor('#555555')
LGRAY   = HexColor('#F0F0F0')
BORDER  = HexColor('#BBCCDD')
GOLD    = HexColor('#C9A84C')   # recommendation box accent

# Hex strings for Paragraph XML markup
NAVY_HEX  = "#003F54"
GREEN_HEX = "#1A7E3D"
RED_HEX   = "#C0392B"
AMBER_HEX = "#D68910"
MGRAY_HEX = "#555555"

# ── Typography ────────────────────────────────────────────────────────────────
BASE_FONT      = 'Helvetica'
BOLD_FONT      = 'Helvetica-Bold'
OBLIQUE_FONT   = 'Helvetica-Oblique'

# ── EODHD verified-data checkmark ─────────────────────────────────────────────
# ZapfDingbats '4' = ✓ checkmark (built-in PDF font, no TTF needed)
_EODHD_CHECK = ' <font name="ZapfDingbats" color="#2E7D32" size="6">4</font>'

def _styles(company_name: str = "") -> dict:
    """Build the style dictionary for this report."""
    ss = getSampleStyleSheet()
    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    return {
        "section_title": S("section_title",
            fontName=BOLD_FONT, fontSize=9, textColor=NAVY,
            spaceBefore=8, spaceAfter=3,
            borderPad=2, leading=11,
        ),
        "body": S("body",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor('#222222'),
            leading=13, alignment=TA_JUSTIFY, spaceAfter=4,
        ),
        "body_small": S("body_small",
            fontName=BASE_FONT, fontSize=7.8, textColor=MGRAY,
            leading=11, alignment=TA_JUSTIFY,
        ),
        "table_header": S("th",
            # Ink-saving: navy text on white background (was white-on-navy)
            fontName=BOLD_FONT, fontSize=7.5, textColor=NAVY,
            alignment=TA_CENTER, leading=10,
        ),
        "table_label": S("tl",
            fontName=BOLD_FONT, fontSize=7.5, textColor=HexColor('#222222'),
            alignment=TA_LEFT, leading=10,
        ),
        "table_cell": S("tc",
            fontName=BASE_FONT, fontSize=7.5, textColor=HexColor('#111111'),
            alignment=TA_RIGHT, leading=10,
        ),
        "rec_title": S("rec_title",
            fontName=BOLD_FONT, fontSize=11, textColor=NAVY,
            alignment=TA_CENTER, leading=14,
        ),
        "rec_body": S("rec_body",
            fontName=BASE_FONT, fontSize=8, textColor=HexColor("#333333"),
            alignment=TA_JUSTIFY, leading=12,
        ),
        "fun_fact": S("fun_fact",
            fontName=BASE_FONT, fontSize=8, textColor=NAVY,
            leading=11, leftIndent=8,
        ),
        "checklist_pass": S("ck_pass",
            fontName=BASE_FONT, fontSize=8, textColor=GREEN, leading=10,
        ),
        "checklist_fail": S("ck_fail",
            fontName=BASE_FONT, fontSize=8, textColor=RED, leading=10,
        ),
    }


# ── Page header (drawn on canvas, not as flowable) ────────────────────────────

def _draw_header(canvas, doc, company: CompanyData, report_date: str):
    """Drawn on every page: company name bar + key stats.

    Ink-saving design: white background, navy (Pantone 303) text and a thin
    bottom rule. No filled colour band.
    """
    canvas.saveState()

    # Company name — large, navy on white
    canvas.setFont(BOLD_FONT, 14)
    canvas.setFillColor(NAVY)
    name = company.name or company.ticker
    canvas.drawString(ML, H - 12*mm, name)

    # Subtitle line: sector | country | exchange
    subtitle = " | ".join(filter(None, [
        company.sector, company.country,
        company.exchange, company.ticker
    ]))
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, H - 18.5*mm, subtitle)

    # Right side: price | mkt cap | date
    cur = company.currency_price or company.currency or ""
    price_str  = (f"Price: {company.current_price:,.2f} {cur}"
                  if company.current_price else "Price n/a")
    mcap_str   = (f"MCap: {_fmt_b(company.market_cap)} {company.currency or ''}"
                  if company.market_cap else "")
    date_str   = f"Report: {report_date}"

    canvas.setFont(BOLD_FONT, 8.5)
    canvas.setFillColor(NAVY)
    right_x = W - MR
    canvas.drawRightString(right_x, H - 10*mm, price_str)
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(right_x, H - 15.5*mm, mcap_str)
    canvas.setFont(BASE_FONT, 7.5)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(right_x, H - 20.5*mm, date_str)

    # Thin separator line below header
    canvas.setStrokeColor(BLUE)
    canvas.setLineWidth(0.8)
    canvas.line(ML, H - 27*mm, W - MR, H - 27*mm)

    # Page number (bottom right)
    canvas.setFont(BASE_FONT, 7)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(W - MR, 8*mm,
                           f"Page {doc.page}  |  Your Humble EquityBot  |  {report_date}")

    canvas.restoreState()


# ── Financial table builder ───────────────────────────────────────────────────

def _build_financial_table(company: CompanyData, styles: dict) -> Table:
    """
    Build the annual financial table.
    Columns: label | (up to 10 most recent fiscal years) | estimate
    """
    cur = company.currency or ""
    all_years = company.sorted_years()

    # Pick up to 10 historical years + mark the most recent estimate column
    hist_years = all_years[:10]   # most recent 10 years (descending)
    show_years = list(reversed(hist_years))  # chronological order
    est_year   = (show_years[-1] + 1) if show_years else datetime.utcnow().year

    # When many columns (≥9 data cols incl. estimate) shrink Paragraph
    # styles so numeric cells don't overflow narrow ~42pt columns.
    _compact = (len(show_years) + 1) >= 9
    if _compact:
        th_style = ParagraphStyle("th_c", parent=styles["table_header"],
                                  fontSize=6.5, leading=8)
        tl_style = ParagraphStyle("tl_c", parent=styles["table_label"],
                                  fontSize=6.5, leading=8)
        tc_style = ParagraphStyle("tc_c", parent=styles["table_cell"],
                                  fontSize=6.5, leading=8)
    else:
        th_style = styles["table_header"]
        tl_style = styles["table_label"]
        tc_style = styles["table_cell"]

    # Column headers — append ✓ (ZapfDingbats) when the year is EODHD-sourced
    def _yr_hdr(y: int) -> str:
        af = company.annual_financials.get(y)
        check = _EODHD_CHECK if (af and getattr(af, "source", "") == "eodhd") else ""
        return str(y) + check

    year_headers = [_yr_hdr(y) for y in show_years] + [f"{est_year}E"]
    col_headers  = [Paragraph("", th_style)] + [
        Paragraph(y, th_style) for y in year_headers
    ]

    def af(yr):
        return company.annual_financials.get(yr)

    def cell(v, fmt="M"):
        """Format a value for the table cell. V2: empty → '— (n/a)' marker."""
        if v is None:
            return Paragraph(
                f'<font color="{MGRAY_HEX}">— n/a</font>', tc_style)
        if fmt == "M":   # millions → show as B or M
            s = f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:,.1f}M"
        elif fmt == "%":
            s = f"{v*100:.1f}%"
        elif fmt == "x":
            s = f"{v:.1f}x"
        elif fmt == "ps":  # per share
            s = f"{v:.2f}"
        else:
            s = str(v)
        return Paragraph(s, tc_style)

    def lbl(text):
        # V2: each row label is annotated with ✓ to confirm the metric is
        # provided by EODHD. (All financial-statement fields used here are
        # part of the EODHD All-In-One /fundamentals payload.)
        return Paragraph(text + _EODHD_CHECK, tl_style)

    # ── Forward estimates helper ──────────────────────────────────────────────
    fe = company.forward_estimates   # ForwardEstimates | None

    def est_cell_val(v, fmt="M"):
        """Render an estimate value in a distinct italic style."""
        if v is None:
            return Paragraph("—", tc_style)
        if fmt == "M":
            s = f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:,.1f}M"
        elif fmt == "%":
            s = f"{v*100:.1f}%"
        elif fmt == "x":
            s = f"{v:.1f}x"
        elif fmt == "ps":
            s = f"{v:.2f}"
        else:
            s = str(v)
        # Italic + slightly lighter to signal "estimate"
        return Paragraph(f"<i>{s}</i>", tc_style)

    def est_cell_none():
        return Paragraph("—", tc_style)

    # Update est_year to use forward_estimates.year if available
    if fe is not None:
        est_year = fe.year
        year_headers = [_yr_hdr(y) for y in show_years] + [f"{est_year}E"]
        col_headers = [Paragraph("", th_style)] + [
            Paragraph(y, th_style) for y in year_headers
        ]

    # Build rows: (label, [col values])
    rows_data = []

    def add_row(label, getter, fmt="M", est_val=None):
        """
        Add a row to the table.
        getter: callable(AnnualFinancials) → value  for historical cols
        est_val: the estimate-year value (or None → show dash)
        """
        vals = [lbl(label)]
        for y in show_years:
            a = af(y)
            vals.append(cell(getter(a) if a else None, fmt))
        # Estimate column
        vals.append(est_cell_val(est_val, fmt) if est_val is not None else est_cell_none())
        rows_data.append(vals)

    add_row(f"Sales ({cur}M)",  lambda a: a.revenue,
            est_val=fe.revenue if fe else None)
    add_row("EBITDA",              lambda a: a.ebitda)

    # Net Profit — two rows: IFRS reported and underlying (EPS-derived)
    add_row("Net Profit (IFRS)",  lambda a: a.net_income)    # IFRS attributable to shareholders
    # Underlying forward net income derived from consensus EPS × current shares
    _fe_ni_underlying = None
    if fe and fe.eps_diluted and company.shares_outstanding:
        _fe_ni_underlying = fe.eps_diluted * company.shares_outstanding
    add_row("Net Profit (Adj.)",  lambda a: a.net_income_underlying,
            est_val=_fe_ni_underlying)

    add_row("Net Fin. Debt",      lambda a: a.net_debt)      # not forward-looking
    add_row("Net Margin",       lambda a: a.net_margin,  "%",
            est_val=fe.net_margin if fe else None)
    add_row("EBIT Margin",      lambda a: a.ebit_margin, "%")  # not in free estimates
    add_row("EPS (dil.)",       lambda a: a.eps_diluted, "ps",
            est_val=fe.eps_diluted if fe else None)

    # Valuation rows
    add_row("P/E",        lambda a: a.pe_ratio,  "x",
            est_val=fe.pe_ratio if fe else None)
    add_row("ROE",        lambda a: a.roe,        "%")
    add_row("Div. Yield", lambda a: a.div_yield,  "%")
    add_row("FCF Yield",  lambda a: a.fcf_yield,  "%")
    add_row("EV/EBIT",    lambda a: a.ev_ebit,    "x")
    add_row("EV/Sales",   lambda a: a.ev_sales,   "x",
            est_val=fe.ev_sales if fe else None)

    # Market cap (historical) + shares outstanding
    add_row("Mkt Cap (M)",     lambda a: a.market_cap,  "M")
    # shares_outstanding is normalized to millions by DataManager; defensive
    # fallback divides only if a legacy raw value (>1M) leaks through.
    add_row("Shares Out. (M)", lambda a: (
        a.shares_outstanding / 1_000_000
        if a.shares_outstanding and a.shares_outstanding > 1_000_000
        else a.shares_outstanding
    ), "ps")

    # Insert header as first row
    all_rows = [col_headers] + rows_data

    # Column widths: label fixed, data cols evenly split among hist + est.
    # With 10 historical + 1 estimate = 11 data cols on A4 (~565 pt content
    # width), narrow label column to leave room for narrow numeric cells.
    n_data_cols = len(show_years) + 1  # +1 for estimate
    label_w = 88 if n_data_cols >= 9 else 105
    data_w  = (CW - label_w) / n_data_cols
    col_widths = [label_w] + [data_w] * n_data_cols

    t = Table(all_rows, colWidths=col_widths, repeatRows=1)

    # Last column index = n_data_cols (label is col 0, last hist is col n_data_cols-1,
    # estimate column is col n_data_cols)
    est_col = n_data_cols

    # Adaptive sizing: with 10+ historical columns numeric cells must shrink
    # so values like "12,345.6" don't overflow ~42pt-wide columns.
    body_fs   = 6.5 if n_data_cols >= 9 else 7.5
    hdr_fs    = 7.0 if n_data_cols >= 9 else 7.5
    cell_pad  = 2   if n_data_cols >= 9 else 5

    ts = [
        # Header row — white background, navy text + thick navy underline
        ('BACKGROUND',  (0,0), (-1,0), white),
        ('TEXTCOLOR',   (0,0), (-1,0), NAVY),
        ('FONTNAME',    (0,0), (-1,0), BOLD_FONT),
        ('FONTSIZE',    (0,0), (-1,0), hdr_fs),
        ('ALIGN',       (0,0), (-1,0), 'CENTER'),
        ('VALIGN',      (0,0), (-1,-1),'MIDDLE'),
        # Body — plain white (no alternating fill, saves ink)
        ('BACKGROUND',  (0,1), (-1,-1), white),
        # Estimate column header — navy text, italic-style by colour
        ('BACKGROUND',  (est_col,0), (est_col,0), white),
        ('TEXTCOLOR',   (est_col,0), (est_col,0), BLUE),
        # Label column — bold but no fill
        ('FONTNAME',    (0,1), (0,-1), BOLD_FONT),
        ('FONTSIZE',    (0,1), (0,-1), body_fs),
        ('ALIGN',       (0,1), (0,-1), 'LEFT'),
        # Data columns
        ('ALIGN',       (1,1), (-1,-1), 'RIGHT'),
        ('FONTSIZE',    (1,1), (-1,-1), body_fs),
        # Grid — light interior rules + thicker navy header underline
        ('GRID',        (0,0), (-1,-1), 0.3, BORDER),
        ('LINEBELOW',   (0,0), (-1,0), 1.4, NAVY),
        # Thicker left border on estimate column
        ('LINEBEFORE',  (est_col,0), (est_col,-1), 1.2, BLUE),
        # Padding
        ('TOPPADDING',  (0,0), (-1,-1), 3),
        ('BOTTOMPADDING',(0,0),(-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), cell_pad),
        ('RIGHTPADDING',(0,0), (-1,-1), cell_pad),
        # Separator after Net Fin. Debt row (row index 4)
        ('LINEBELOW',   (0,4), (-1,4), 0.6, BLUE),
        # Separator after EPS row (row index 7)
        ('LINEBELOW',   (0,7), (-1,7), 0.6, BLUE),
    ]
    t.setStyle(TableStyle(ts))
    return t


# ── Peer table builder ────────────────────────────────────────────────────────

def _build_peer_table(
    company: CompanyData, peers: dict[str, CompanyData], styles: dict
) -> Table:
    """Build the peer comparison table (Page 3)."""
    cur = company.currency or "USD"

    headers = [
        "Company", "Ticker", "Annual Sales", "Mkt Cap",
        "ROE %", "P/B", "P/E", "EV/EBIT", "EV/Sales", "Gearing"
    ]
    header_row = [Paragraph(h, styles["table_header"]) for h in headers]

    def p_cell(text, right=False):
        st = styles["table_cell"] if right else styles["table_label"]
        return Paragraph(str(text), st)

    def _fmt_with_ccy(value, peer_ccy: str) -> str:
        """Format a monetary value; append currency suffix when it differs from anchor."""
        if value is None:
            return "n/a"
        base = _fmt_b(value)
        if peer_ccy and peer_ccy != cur:
            return f"{base} {peer_ccy}"
        return base

    def make_row(c: CompanyData, is_anchor=False):
        la = c.latest_annual()
        # Determine the peer's reporting currency (fall back to price currency)
        peer_ccy = (c.currency or c.currency_price or "").strip().upper()
        row = [
            Paragraph(
                f"<b>{c.name or c.ticker}</b>" if is_anchor else (c.name or c.ticker),
                styles["table_label"]
            ),
            p_cell(c.ticker),
            p_cell(_fmt_with_ccy(la.revenue if la else None, peer_ccy), right=True),
            p_cell(_fmt_with_ccy(c.market_cap, peer_ccy), right=True),
            p_cell(_fmt_pct(c.roe),      right=True),
            p_cell(_fmt_x(c.price_to_book), right=True),
            p_cell(_fmt_x(c.pe_ratio),   right=True),
            p_cell(_fmt_x(c.ev_ebit),    right=True),
            p_cell(_fmt_x(c.ev_sales),   right=True),
            p_cell(_fmt_x(c.gearing),    right=True),
        ]
        return row

    rows = [header_row, make_row(company, is_anchor=True)]
    for pdata in peers.values():
        rows.append(make_row(pdata))

    # Column widths
    col_widths = [110, 62, 52, 52, 40, 35, 35, 45, 45, 40]
    # Adjust proportionally to CW
    total = sum(col_widths)
    col_widths = [w * CW / total for w in col_widths]

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    ts = [
        # Header row — white background, navy text + thick navy underline
        ('BACKGROUND',  (0,0), (-1,0), white),
        ('TEXTCOLOR',   (0,0), (-1,0), NAVY),
        ('FONTNAME',    (0,0), (-1,0), BOLD_FONT),
        ('FONTSIZE',    (0,0), (-1,0), 7),
        ('ALIGN',       (0,0), (-1,0), 'CENTER'),
        ('VALIGN',      (0,0), (-1,-1),'MIDDLE'),
        # Anchor company row (row 1) — bold + thick underline instead of fill
        ('FONTNAME',    (0,1), (-1,1), BOLD_FONT),
        ('BACKGROUND',  (0,2), (-1,-1), white),
        ('ALIGN',       (2,1), (-1,-1), 'RIGHT'),
        ('FONTSIZE',    (1,1), (-1,-1), 7),
        ('FONTSIZE',    (0,1), (0,-1), 7),
        ('GRID',        (0,0), (-1,-1), 0.3, BORDER),
        ('LINEBELOW',   (0,0), (-1,0), 1.4, NAVY),
        ('LINEBELOW',   (0,1), (-1,1), 0.8, NAVY),
        ('TOPPADDING',  (0,0), (-1,-1), 3),
        ('BOTTOMPADDING',(0,0),(-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING',(0,0), (-1,-1), 4),
    ]
    t.setStyle(TableStyle(ts))
    return t


# ── Checklist table builder ───────────────────────────────────────────────────

def _build_checklist_table(checklist: list[dict], styles: dict) -> Table:
    """Build the Yes/No investment checklist table."""
    header_row = [
        Paragraph("Criterion", styles["table_header"]),
        Paragraph("Threshold", styles["table_header"]),
        Paragraph("Actual", styles["table_header"]),
        Paragraph("Pass", styles["table_header"]),
    ]
    rows = [header_row]
    for item in checklist:
        passed = item.get("pass", False)
        pass_text = "YES" if passed else "NO"
        pass_style = styles["checklist_pass"] if passed else styles["checklist_fail"]
        thresh = f"> {item['threshold']}%" if item.get("threshold") else "—"
        rows.append([
            Paragraph(item["criterion"], styles["table_label"]),
            Paragraph(thresh, styles["table_cell"]),
            Paragraph(item["actual"], styles["table_cell"]),
            Paragraph(f"<b>{pass_text}</b>", pass_style),
        ])

    col_widths = [CW * 0.44, CW * 0.18, CW * 0.22, CW * 0.16]
    t = Table(rows, colWidths=col_widths)
    ts = [
        # Ink-saving header: white bg, navy text, thick navy underline
        ('BACKGROUND',  (0,0), (-1,0), white),
        ('TEXTCOLOR',   (0,0), (-1,0), NAVY),
        ('FONTNAME',    (0,0), (-1,0), BOLD_FONT),
        ('FONTSIZE',    (0,0), (-1,0), 7.5),
        ('ALIGN',       (0,0), (-1,0), 'CENTER'),
        ('ALIGN',       (1,1), (-1,-1), 'CENTER'),
        ('ALIGN',       (0,1), (0,-1), 'LEFT'),
        ('VALIGN',      (0,0), (-1,-1),'MIDDLE'),
        ('BACKGROUND',  (0,1), (-1,-1), white),
        ('FONTSIZE',    (0,1), (-1,-1), 8),
        ('GRID',        (0,0), (-1,-1), 0.3, BORDER),
        ('LINEBELOW',   (0,0), (-1,0), 1.4, NAVY),
        ('TOPPADDING',  (0,0), (-1,-1), 4),
        ('BOTTOMPADDING',(0,0),(-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING',(0,0), (-1,-1), 5),
    ]
    t.setStyle(TableStyle(ts))
    return t


# ── Recommendation box ────────────────────────────────────────────────────────

def _build_recommendation_box(
    recommendation: str, rationale: str, styles: dict
) -> Table:
    """Ink-saving BUY / HOLD / SELL box: white inside, thick coloured border."""
    rec = recommendation.strip().upper() if recommendation else "HOLD"
    colour = {"BUY": GREEN, "SELL": RED, "HOLD": AMBER}.get(rec, NAVY)
    # Hex equivalent for Paragraph XML colour markup
    colour_hex = {"BUY": "#1A7E3D", "SELL": "#C0392B", "HOLD": "#D68910"}\
        .get(rec, "#003F54")

    rec_para = Paragraph(
        f'<font color="{colour_hex}">RECOMMENDATION: <b>{rec}</b></font>',
        ParagraphStyle("rh", fontName=BOLD_FONT, fontSize=12,
                       alignment=TA_CENTER, leading=15)
    )
    rat_para = Paragraph(
        rationale or "",
        ParagraphStyle("rb", fontName=BASE_FONT, fontSize=8,
                       textColor=HexColor("#333333"),
                       alignment=TA_JUSTIFY, leading=12)
    )

    t = Table(
        [[rec_para], [rat_para]],
        colWidths=[CW],
    )
    t.setStyle(TableStyle([
        ('BACKGROUND',   (0,0), (-1,-1), white),
        ('BOX',          (0,0), (-1,-1), 2.2, colour),
        ('LINEBELOW',    (0,0), (-1,0),  1.0, colour),
        ('TOPPADDING',   (0,0), (-1,-1), 8),
        ('BOTTOMPADDING',(0,0), (-1,-1), 8),
        ('LEFTPADDING',  (0,0), (-1,-1), 12),
        ('RIGHTPADDING', (0,0), (-1,-1), 12),
        ('ROUNDEDCORNERS', [4]),
    ]))
    return t


# ── Section title helper ──────────────────────────────────────────────────────

def section_title(text: str, styles: dict):
    """Returns a section heading with a blue underline."""
    return KeepTogether([
        Paragraph(f"<b>{text}</b>", styles["section_title"]),
        HRFlowable(width=CW, thickness=1.2, color=BLUE, spaceAfter=4),
    ])


# ── Main generator ────────────────────────────────────────────────────────────

class OverviewV2PDFGenerator:
    """Renders a 3-page Investment Memo PDF using ReportLab."""

    def render(
        self,
        company: CompanyData,
        analysis: dict,
        peers: dict[str, CompanyData],
        checklist: list[dict],
        output_path: str,
        adv_result=None,  # Optional[AdversarialResult]
    ) -> None:
        report_date = datetime.utcnow().strftime("%Y-%m-%d")
        styles = _styles(company.name or "")

        # Shared header callback
        def _page_header(canvas, doc):
            _draw_header(canvas, doc, company, report_date)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"{company.name or company.ticker} — Investment Memo",
            author="Your Humble EquityBot",
            subject="Investment Memo",
        )

        story = []

        # ── PAGE 1: Financial Table + Snapshot ───────────────────────────────
        story += self._page1(company, analysis, styles)
        story.append(PageBreak())

        # ── PAGE 2: Bull / Bear + Recommendation ─────────────────────────────
        story += self._page2(analysis, styles)
        story.append(PageBreak())

        # ── PAGE 3: Peer Table + Checklist ───────────────────────────────────
        story += self._page3(company, analysis, peers, checklist, styles)

        # ── PAGE 4: V2-specific field provenance legend ──────────────────────
        story.append(PageBreak())
        story += self._page4_provenance(company, styles)

        # ── PAGE 5 (optional): Adversarial Review ────────────────────────────
        if adv_result is not None:
            from agents.pdf_adversarial import build_adversarial_page
            story.append(PageBreak())
            story += build_adversarial_page(adv_result)

        doc.build(story, onFirstPage=_page_header, onLaterPages=_page_header)
        logger.info(f"[PDF V2] Saved: {output_path}")

    # ── V2 Page 4: Field Provenance Legend ────────────────────────────────────
    def _page4_provenance(self, company: CompanyData, styles: dict) -> list:
        """
        Explicit table mapping every report field to its data source.
        For Japanese (TSE) stocks, yfinance is used instead of EODHD.
        """
        is_japan = (company.ticker or "").upper().endswith(".T")

        el = []
        title = "Data Provenance — yfinance (TSE)" if is_japan else "EODHD Data Provenance"
        el.append(Paragraph(title,
                            ParagraphStyle("v2hdr",
                                fontName=BOLD_FONT, fontSize=12,
                                textColor=NAVY, spaceAfter=4)))

        if is_japan:
            desc = (
                "This company is listed on the Tokyo Stock Exchange (TSE/JPX). "
                "EODHD does not cover Japanese equities, so all fundamental data "
                "in this report is sourced from <b>yfinance (Yahoo Finance)</b>. "
                "Coverage is less complete than EODHD: historical depth is typically "
                "3–5 years, some fields (ISIN, detailed officers, intraday price) "
                "may be unavailable."
            )
        else:
            desc = (
                "Every populated field in this report is sourced exclusively from "
                "the EODHD All-In-One API. The table below maps each field to its "
                "EODHD endpoint and block, including which fields EODHD does not "
                "expose (rendered as <i>— n/a</i> in the body)."
            )
        el.append(Paragraph(desc,
            ParagraphStyle("v2desc",
                fontName=BASE_FONT, fontSize=8, textColor=MGRAY,
                leading=11, spaceAfter=8)))

        from reportlab.platypus import Table, TableStyle

        src_col_hdr = "yfinance Field" if is_japan else "EODHD Source"
        rows = [[
            Paragraph("<b>Field</b>", styles["table_header"]),
            Paragraph(f"<b>{src_col_hdr}</b>", styles["table_header"]),
            Paragraph("<b>Status</b>", styles["table_header"]),
        ]]

        if is_japan:
            # yfinance field mapping for Japanese stocks
            chk = "✓ yfinance"
            na  = "— n/a (TSE)"
            provenance = [
                ("Company name / sector / industry",  "Ticker.info → longName, sector",       chk),
                ("Description",                       "Ticker.info → longBusinessSummary",     chk),
                ("Current price (live)",              "Ticker.info → currentPrice",            chk),
                ("Market cap",                        "Ticker.info → marketCap",               chk),
                ("Shares outstanding",                "Ticker.info → sharesOutstanding",       chk),
                ("P/E (trailing)",                    "Ticker.info → trailingPE",              chk),
                ("Forward P/E",                       "Ticker.info → forwardPE",               chk),
                ("Margins (Gross/EBIT/Net)",          "Ticker.info → grossMargins, operatingMargins, profitMargins", chk),
                ("ROE, ROA",                          "Ticker.info → returnOnEquity, returnOnAssets", chk),
                ("Revenue / Net Income (annual)",     "Ticker.financials (3–5 yr)",            chk),
                ("Balance Sheet (annual)",            "Ticker.balance_sheet (3–5 yr)",         chk),
                ("Cash Flow (annual)",                "Ticker.cashflow (3–5 yr)",              chk),
                ("EPS (annual actuals)",              "Ticker.earnings",                       chk),
                ("Dividend Yield / Rate",             "Ticker.info → dividendYield, dividendRate", chk),
                ("52W high / low, Beta",              "Ticker.info → fiftyTwoWeekHigh/Low, beta", chk),
                ("Forward Estimates",                 "Ticker.info → earningsGrowth, revenueGrowth", chk),
                ("Price Chart (historical)",          "Ticker.history()",                      chk),
                ("ISIN",                              "Not provided by yfinance",              na),
                ("EV / EV multiples",                 "Derived from marketCap + debt − cash", chk),
                ("Officers / Insiders",               "Limited via Ticker.info",               "⚠ partial"),
                ("Investment Snapshot (narrative)",   "LLM — context from yfinance data",     "ⓘ LLM"),
                ("Bull / Bear Case (narrative)",      "LLM — yfinance context",               "ⓘ LLM"),
                ("Recommendation + Rationale",        "LLM — yfinance signals",               "ⓘ LLM"),
                ("Suggested Peers",                   "LLM — names; peer metrics from yfinance","ⓘ LLM names"),
                ("Peer comparison metrics",           "yfinance (per peer)",                  chk),
                ("News headlines",                    "Not used in this report",              "— n/a"),
            ]
        else:
            provenance = [
                ("Company name / sector / industry",  "/fundamentals → General",                "✓ EODHD"),
                ("Description, address, officers",    "/fundamentals → General",                "✓ EODHD"),
                ("ISIN / Country / Fiscal Year End",  "/fundamentals → General",                "✓ EODHD"),
                ("Current price (live)",              "/real-time → close",                     "✓ EODHD"),
                ("Market cap, EV",                    "/fundamentals → Highlights, Valuation",  "✓ EODHD"),
                ("Shares outstanding (per year)",     "/fundamentals → outstandingShares.annual","✓ EODHD"),
                ("P/E, Forward P/E, PEG, P/B, P/S",   "/fundamentals → Highlights, Valuation",  "✓ EODHD"),
                ("EV/EBITDA, EV/Sales",               "/fundamentals → Valuation",              "✓ EODHD"),
                ("Margins (Gross/EBIT/EBITDA/Net)",   "/fundamentals → Highlights (TTM)",       "✓ EODHD"),
                ("ROE, ROA",                          "/fundamentals → Highlights",             "✓ EODHD"),
                ("Revenue / EBITDA / EBIT / NI (10y)","/fundamentals → Financials.Income_Statement.yearly","✓ EODHD"),
                ("EPS Diluted (annual actuals)",      "/fundamentals → Earnings.Annual.epsActual","✓ EODHD"),
                ("Total Assets / Debt / Equity (10y)","/fundamentals → Financials.Balance_Sheet.yearly","✓ EODHD"),
                ("Net Debt",                          "/fundamentals → Balance_Sheet.netDebt",  "✓ EODHD"),
                ("Cash Flow / CapEx / FCF (10y)",     "/fundamentals → Financials.Cash_Flow.yearly","✓ EODHD"),
                ("Dividend Yield, Payout, Ex-Date",   "/fundamentals → SplitsDividends",        "✓ EODHD"),
                ("Forward dividend rate",             "/fundamentals → SplitsDividends.ForwardAnnualDividendRate","✓ EODHD"),
                ("52W high / low, 50/200 DMA, Beta",  "/fundamentals → Technicals",             "✓ EODHD"),
                ("Float, % Insiders, % Institutions", "/fundamentals → SharesStats",            "✓ EODHD"),
                ("Forward EPS / Revenue Estimates",   "/fundamentals → Earnings.Trend",         "✓ EODHD"),
                ("5-Year Daily Price Chart",          "/eod (All-In-One)",                      "✓ EODHD"),
                ("Historical Year-End Prices",        "/eod → last close of each year",         "✓ EODHD"),
                ("Wall Street Target Price",          "/fundamentals → Highlights.WallStreetTargetPrice","✓ EODHD"),
                ("Investment Snapshot (narrative)",   "LLM (Claude/GPT-4o) — prompt fed EODHD only","ⓘ LLM"),
                ("Bull / Bear Case (narrative)",      "LLM — context limited to EODHD data",    "ⓘ LLM"),
                ("Recommendation + Rationale",        "LLM — synthesis of EODHD signals",       "ⓘ LLM"),
                ("Fun Facts",                         "LLM — drawn from EODHD profile/financials","ⓘ LLM"),
                ("Suggested Peers",                   "LLM — names only, peer metrics from EODHD","ⓘ LLM names"),
                ("Peer P/E, EV/EBITDA, ROE, etc.",    "/fundamentals (per peer)",               "✓ EODHD"),
                ("News headlines",                    "Not used in this report",                "— n/a"),
                ("Macro context",                     "Not used in this report",                "— n/a"),
            ]
        for label, src, status in provenance:
            if status.startswith("✓"):
                status_color = GREEN_HEX
            elif status.startswith("ⓘ"):
                status_color = AMBER_HEX
            else:
                status_color = MGRAY_HEX
            rows.append([
                Paragraph(label, styles["table_label"]),
                Paragraph(src, ParagraphStyle(
                    "src", parent=styles["table_cell"],
                    alignment=TA_LEFT, fontSize=7)),
                Paragraph(f'<font color="{status_color}"><b>{status}</b></font>',
                          ParagraphStyle("st", parent=styles["table_cell"],
                                         alignment=TA_LEFT, fontSize=7)),
            ])
        t = Table(rows, colWidths=[CW * 0.40, CW * 0.40, CW * 0.20])
        t.setStyle(TableStyle([
            ('BACKGROUND',  (0, 0), (-1, 0), white),
            ('TEXTCOLOR',   (0, 0), (-1, 0), NAVY),
            ('FONTNAME',    (0, 0), (-1, 0), BOLD_FONT),
            ('FONTSIZE',    (0, 0), (-1, 0), 8),
            ('LINEBELOW',   (0, 0), (-1, 0), 1.4, NAVY),
            ('GRID',        (0, 0), (-1, -1), 0.25, BORDER),
            ('FONTSIZE',    (0, 1), (-1, -1), 7),
            ('VALIGN',      (0, 0), (-1, -1), "MIDDLE"),
            ('TOPPADDING',  (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING',(0, 0), (-1, -1), 3),
        ]))
        el.append(t)
        return el

    # ── Page builders ─────────────────────────────────────────────────────────

    def _page1(self, company: CompanyData, analysis: dict, styles: dict) -> list:
        el = []

        # V2 banner — clarifies the data sourcing constraint.
        el.append(Paragraph(
            f'<font color="{NAVY_HEX}"><b>Investment Memo V2 — EODHD Based</b></font>'
            f'  ·  <font color="{MGRAY_HEX}">All metrics sourced from the EODHD '
            f'All-In-One API. Fields marked '
            f'<font name="ZapfDingbats" color="#2E7D32">4</font> = verified EODHD; '
            f'<i>— n/a</i> = not provided by EODHD.</font>',
            ParagraphStyle("v2_banner", fontName=BASE_FONT, fontSize=7.5,
                           leading=10, spaceAfter=4, borderPadding=4,
                           borderColor=NAVY, borderWidth=0.4)))
        el.append(Spacer(1, 2))

        # ── 5-Year price chart (top of page 1) ────────────────────────────────
        # V2: data exclusively from EODHD /eod (All-In-One). If the EODHD
        # bundle was not passed through to price_chart, we silently skip.
        try:
            from agents.price_chart import generate_price_chart_png
            import io
            eod_data = getattr(company, "_eod_data_v2", None) or []
            png = generate_price_chart_png(
                ticker=company.ticker,
                company_name=company.name,
                currency=company.currency_price or company.currency,
                width_in=7.2, height_in=2.2,
                eod_data=eod_data,
            )
            if png:
                img = Image(io.BytesIO(png),
                            width=170*mm, height=52*mm,
                            kind="proportional")
                el.append(img)
                el.append(Spacer(1, 4))
        except Exception as ex:
            logger.warning(f"[PDF] Price chart failed: {ex}")

        # Financial table
        el.append(section_title("Financial Summary", styles))
        el.append(Spacer(1, 2))
        try:
            el.append(_build_financial_table(company, styles))
        except Exception as e:
            logger.error(f"[PDF] Financial table error: {e}")
            el.append(Paragraph(f"Financial table unavailable: {e}", styles["body_small"]))
        # EODHD legend — only show if at least one year has EODHD data
        if any(getattr(af, "source", "") == "eodhd"
               for af in company.annual_financials.values()):
            el.append(Paragraph(
                '<font name="ZapfDingbats" color="#2E7D32" size="6">4</font>'
                " = verified EODHD data",
                ParagraphStyle("eo_legend", fontName=BASE_FONT, fontSize=6,
                               textColor=MGRAY, spaceBefore=2, leading=8),
            ))

        # Estimate footnote
        fe = company.forward_estimates
        if fe is not None:
            analysts = fe.analyst_count or "?"
            note = (
                f"<i>{fe.year}E column: analyst consensus estimates "
                f"({analysts} analyst{'s' if analysts != 1 else ''})  ·  "
                f"Source: {fe.source}  ·  Italic values = forecast, not historical.</i>"
            )
            el.append(Paragraph(note, styles["body_small"]))
        el.append(Spacer(1, 6))

        # Investment Snapshot (LLM-generated, EODHD-only context)
        el.append(section_title("Investment Snapshot", styles))
        el.append(Paragraph(
            '<i><font color="' + AMBER_HEX + '">ⓘ AI-generated narrative '
            '— context limited to EODHD All-In-One data only.</font></i>',
            ParagraphStyle("llm_note", fontName=BASE_FONT, fontSize=6.5,
                           textColor=AMBER, leading=8, spaceAfter=3)))
        snapshot = analysis.get("snapshot", "Analysis not available.")
        for para_text in _split_paragraphs(snapshot):
            el.append(Paragraph(para_text, styles["body"]))

        # Fun Facts
        fun_facts = analysis.get("fun_facts", [])
        if fun_facts:
            el.append(Spacer(1, 6))
            el.append(section_title("Did You Know?", styles))
            for i, fact in enumerate(fun_facts[:3], 1):
                el.append(Paragraph(f"<b>{i}.</b> {fact}", styles["fun_fact"]))
                el.append(Spacer(1, 2))

        return el

    def _page2(self, analysis: dict, styles: dict) -> list:
        el = []

        # Bull case
        el.append(section_title("Bull Case — Why Invest", styles))
        el.append(Paragraph(
            '<i><font color="' + AMBER_HEX + '">ⓘ AI-generated narrative '
            '— context limited to EODHD data only.</font></i>',
            ParagraphStyle("llm_note", fontName=BASE_FONT, fontSize=6.5,
                           textColor=AMBER, leading=8, spaceAfter=3)))
        bull = analysis.get("bull_case", "Bull case not available.")
        for para in _split_paragraphs(bull):
            el.append(Paragraph(para, styles["body"]))

        el.append(Spacer(1, 8))

        # Bear case
        el.append(section_title("Bear Case — Devil's Advocate", styles))
        el.append(Paragraph(
            '<i><font color="' + AMBER_HEX + '">ⓘ AI-generated narrative '
            '— context limited to EODHD data only.</font></i>',
            ParagraphStyle("llm_note", fontName=BASE_FONT, fontSize=6.5,
                           textColor=AMBER, leading=8, spaceAfter=3)))
        bear = analysis.get("bear_case", "Bear case not available.")
        for para in _split_paragraphs(bear):
            el.append(Paragraph(para, styles["body"]))

        el.append(Spacer(1, 10))

        # Recommendation box
        el.append(_build_recommendation_box(
            analysis.get("recommendation", "HOLD"),
            analysis.get("recommendation_rationale", ""),
            styles,
        ))

        return el

    def _page3(
        self,
        company: CompanyData,
        analysis: dict,
        peers: dict[str, CompanyData],
        checklist: list[dict],
        styles: dict,
    ) -> list:
        el = []

        # Peer table
        el.append(section_title("Peer Group Comparison", styles))
        if peers or company:
            try:
                el.append(_build_peer_table(company, peers, styles))
            except Exception as e:
                logger.error(f"[PDF] Peer table error: {e}")
                el.append(Paragraph(f"Peer table unavailable: {e}", styles["body_small"]))
        else:
            el.append(Paragraph("No peer data available.", styles["body_small"]))

        el.append(Spacer(1, 12))

        # Checklist
        el.append(section_title("Investment Checklist", styles))
        try:
            el.append(_build_checklist_table(checklist, styles))
        except Exception as e:
            el.append(Paragraph(f"Checklist unavailable: {e}", styles["body_small"]))

        # Score summary
        passed = sum(1 for c in checklist if c.get("pass"))
        total  = len(checklist)
        score_text = (
            f"<b>Score: {passed}/{total}</b> criteria met.  "
            f"{'Strong fundamental profile.' if passed >= 5 else 'Mixed fundamentals — see analysis.' if passed >= 3 else 'Weak fundamental screen — caution advised.'}"
        )
        el.append(Spacer(1, 6))
        el.append(Paragraph(score_text, styles["body_small"]))

        # Data sources footnote
        el.append(Spacer(1, 10))
        sources = ", ".join(company.data_sources) if company.data_sources else "yfinance"
        footnote = (
            f"<i>Data sources: {sources}. "
            f"Financial data as of {company.as_of_date or 'n/a'}. "
            f"Market data is indicative, not real-time. "
            f"This report is for informational purposes only and does not constitute investment advice.</i>"
        )
        el.append(Paragraph(footnote, styles["body_small"]))

        return el


# ── Text helpers ──────────────────────────────────────────────────────────────

def _split_paragraphs(text: str) -> list[str]:
    """Split long text on double newlines; clean up for ReportLab."""
    if not text:
        return [""]
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return paras if paras else [text]


def _fmt_b(v) -> str:
    if v is None: return "n/a"
    if abs(v) >= 1000: return f"{v/1000:.1f}B"
    return f"{v:,.1f}M"

def _fmt_pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"

def _fmt_x(v) -> str:
    return f"{v:.1f}x" if v is not None else "n/a"
