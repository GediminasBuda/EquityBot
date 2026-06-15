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
MT      = 28*mm           # top margin (leaves room for compact header drawn on canvas)
MB      = 14*mm           # bottom margin
CW      = W - ML - MR     # content width ≈ 481 pts

# ── Colour palette ────────────────────────────────────────────────────────────
# Pantone 303 / RGB(0, 63, 84). Ink-saving: header band is white, brand text
# is rendered in this dark teal/navy.
NAVY    = HexColor('#003F54')
BLUE    = HexColor('#003F54')   # all lines use brand navy (was #2E75B6)
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
def _setup_fonts() -> tuple:
    """Try to register Calibri Light fonts; fall back to Helvetica on Linux/cloud."""
    import os
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        font_dirs = [
            r"C:\Windows\Fonts",
            os.path.expanduser("~/Library/Fonts"),
            "/usr/share/fonts/truetype",
        ]
        for d in font_dirs:
            light_path = os.path.join(d, "calibril.ttf")
            if not os.path.exists(light_path):
                continue
            pdfmetrics.registerFont(TTFont("CalibriLight", light_path))
            # Bold: use Calibri Regular (medium weight suits a Light theme)
            bold_path = os.path.join(d, "calibri.ttf")
            pdfmetrics.registerFont(TTFont("CalibriLight-Bold",
                                           bold_path if os.path.exists(bold_path) else light_path))
            # Italic
            ital_path = os.path.join(d, "calibrili.ttf")
            pdfmetrics.registerFont(TTFont("CalibriLight-Italic",
                                           ital_path if os.path.exists(ital_path) else light_path))
            from reportlab.pdfbase.pdfmetrics import registerFontFamily
            registerFontFamily("CalibriLight",
                normal="CalibriLight", bold="CalibriLight-Bold",
                italic="CalibriLight-Italic", boldItalic="CalibriLight-Bold")
            logger.debug("Calibri Light fonts registered")
            return "CalibriLight", "CalibriLight-Bold", "CalibriLight-Italic"
    except Exception as e:
        logger.debug(f"Calibri font setup skipped: {e}")
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"

BASE_FONT, BOLD_FONT, OBLIQUE_FONT = _setup_fonts()

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
            leading=13, alignment=TA_JUSTIFY, spaceAfter=8,
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
            leading=11, leftIndent=0, alignment=TA_JUSTIFY,
        ),
        "news_sub": S("news_sub",
            fontName=BOLD_FONT, fontSize=8, textColor=NAVY,
            spaceBefore=8, spaceAfter=3, leading=10, leftIndent=0,
        ),
        "news_item": S("news_item",
            fontName=BASE_FONT, fontSize=8, textColor=HexColor('#222222'),
            leading=12, leftIndent=0, spaceAfter=6, alignment=TA_JUSTIFY,
        ),
        "news_meta": S("news_meta",
            fontName=OBLIQUE_FONT, fontSize=6.5, textColor=MGRAY,
            leading=9, leftIndent=8, spaceAfter=3,
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

    # Row 1: company name (left) aligned with price (right)
    NAME_Y    = H - 11*mm
    # Row 2: subtitle (left) aligned with mcap (right)
    SUBTITLE_Y = H - 17*mm
    # Separator line — close below subtitle
    LINE_Y    = H - 21*mm

    # Company name
    canvas.setFont(BOLD_FONT, 14)
    canvas.setFillColor(NAVY)
    name = company.name or company.ticker
    canvas.drawString(ML, NAME_Y, name)

    # Subtitle: sector | country | exchange | ticker | date
    subtitle = " | ".join(filter(None, [
        company.sector, company.country,
        company.exchange, company.ticker, report_date
    ]))
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, SUBTITLE_Y, subtitle)

    # Right side: price (row 1) | mcap (row 2)
    cur = company.currency_price or company.currency or ""
    price_str = (f"Price: {company.current_price:,.2f} {cur}"
                 if company.current_price else "Price n/a")
    mcap_str  = (f"MCap: {_fmt_b(company.market_cap)} {company.currency or ''}"
                 if company.market_cap else "")

    right_x = W - MR
    canvas.setFont(BOLD_FONT, 8.5)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(right_x, NAME_Y, price_str)
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(right_x, SUBTITLE_Y, mcap_str)

    # Thin separator line — snug below subtitle
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.8)
    canvas.line(ML, LINE_Y, W - MR, LINE_Y)

    # Page number (bottom right) — no date (date is in header)
    canvas.setFont(BASE_FONT, 7)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(W - MR, 8*mm,
                           f"Page {doc.page}  |  Your Humble EquityBot")

    canvas.restoreState()


# ── Financial table builder ───────────────────────────────────────────────────

def _build_financial_table(company: CompanyData, styles: dict) -> Table:
    """
    Build the annual financial table.
    Columns: label | (up to 10 most recent fiscal years) | TTM | forward estimate
    """
    cur = company.currency or ""
    all_years = company.sorted_years()

    hist_years = all_years[:10]          # most recent 10 years (descending)
    show_years = list(reversed(hist_years))  # chronological order

    fe = company.forward_estimates
    est_year = (show_years[-1] + 1) if show_years else datetime.utcnow().year
    if fe is not None:
        est_year = fe.year

    # n_data_cols = historical years + TTM col + estimate col
    n_data_cols = len(show_years) + 2
    _compact = n_data_cols >= 9
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

    def _yr_hdr(y: int) -> str:
        a = company.annual_financials.get(y)
        check = _EODHD_CHECK if (a and getattr(a, "source", "") == "eodhd") else ""
        return str(y) + check

    year_headers = [_yr_hdr(y) for y in show_years] + ["TTM", f"{est_year}E"]
    col_headers  = [Paragraph("", th_style)] + [
        Paragraph(h, th_style) for h in year_headers
    ]

    def af(yr):
        return company.annual_financials.get(yr)

    def cell(v, fmt="M"):
        if v is None:
            return Paragraph(f'<font color="{MGRAY_HEX}">— n/a</font>', tc_style)
        if fmt == "M":
            s = f"{v/1000:.2f}B" if abs(v) >= 1000 else f"{v:,.2f}M"
        elif fmt == "%":
            s = f"{v*100:.1f}%"
        elif fmt == "x":
            s = f"{v:.1f}x"
        elif fmt == "ps":
            s = f"{v:.2f}"
        else:
            s = str(v)
        return Paragraph(s, tc_style)

    def italic_cell(v, fmt="M", dash="—"):
        """Italic cell for TTM and estimate columns."""
        if v is None:
            return Paragraph(f'<font color="{MGRAY_HEX}">{dash}</font>', tc_style)
        if fmt == "M":
            s = f"{v/1000:.2f}B" if abs(v) >= 1000 else f"{v:,.2f}M"
        elif fmt == "%":
            s = f"{v*100:.1f}%"
        elif fmt == "x":
            s = f"{v:.1f}x"
        elif fmt == "ps":
            s = f"{v:.2f}"
        else:
            s = str(v)
        return Paragraph(f"<i>{s}</i>", tc_style)

    _uses_eodhd_tbl = (
        any(getattr(af, "source", "") == "eodhd" for af in company.annual_financials.values())
        or "eodhd" in (company.data_sources or [])
    )
    _lbl_check = _EODHD_CHECK if _uses_eodhd_tbl else ""

    def lbl(text):
        return Paragraph(text + _lbl_check, tl_style)

    # ── TTM values — use getattr() defensively: old cached CompanyData objects
    # (created before these fields existed) won't have the attribute at all.
    # Do NOT fall back to revenue_per_share * shares: for TSE stocks yfinance
    # returns revenuePerShare as the most recent single quarter's figure, not TTM.
    ttm_revenue  = getattr(company, 'ttm_revenue', None)
    ttm_ebitda   = getattr(company, 'ttm_ebitda', None)
    ttm_ebit_val = getattr(company, 'ttm_ebit', None)
    ttm_ni_ifrs  = getattr(company, 'ttm_net_income', None)

    # EV/Sales TTM: compute from actual EV + TTM revenue (avoids stale cached
    # yfinance enterpriseToRevenue scalar which can be wildly wrong for TSE stocks)
    _ev = company.enterprise_value
    if _ev is None and company.market_cap is not None and company.net_debt is not None:
        _ev = company.market_cap + company.net_debt
    # EV/Sales TTM: use ttm_revenue when available, otherwise fall back to
    # latest annual revenue — same denominator the peer table uses, so both
    # tables stay consistent. Never trust company.ev_sales (stale yfinance scalar).
    _latest_af = company.latest_annual()
    _annual_rev = _latest_af.revenue if _latest_af else None
    _rev_denom = ttm_revenue or _annual_rev
    ttm_ev_sales = (_ev / _rev_denom) if (_ev and _rev_denom and _rev_denom > 0) else None

    rows_data = []

    def add_row(label, getter, fmt="M", ttm_val=None, est_val=None):
        """getter: callable(AnnualFinancials) → value for historical cols."""
        vals = [lbl(label)]
        for y in show_years:
            a = af(y)
            vals.append(cell(getter(a) if a else None, fmt))
        vals.append(cell(ttm_val, fmt))              # TTM — factual, not italic
        vals.append(italic_cell(est_val, fmt))        # Estimate column (forecast → italic)
        rows_data.append(vals)

    add_row(f"Sales ({cur}M)", lambda a: a.revenue,
            ttm_val=ttm_revenue,
            est_val=fe.revenue if fe else None)
    add_row("EBITDA", lambda a: a.ebitda,
            ttm_val=ttm_ebitda)

    _fe_ni = None
    if fe and fe.eps_diluted and company.shares_outstanding:
        _fe_ni = fe.eps_diluted * company.shares_outstanding
    add_row("Net Income", lambda a: a.net_income,
            ttm_val=ttm_ni_ifrs,
            est_val=_fe_ni)

    add_row("Net Fin. Debt", lambda a: a.net_debt,
            ttm_val=company.net_debt)
    add_row("Net Margin", lambda a: a.net_margin, "%",
            ttm_val=company.net_margin,
            est_val=fe.net_margin if fe else None)
    add_row("EBIT Margin", lambda a: a.ebit_margin, "%",
            ttm_val=company.ebit_margin)
    add_row("EPS (dil.)", lambda a: a.eps_diluted, "ps",
            ttm_val=company.eps_ttm,
            est_val=fe.eps_diluted if fe else None)

    add_row("P/E", lambda a: a.pe_ratio, "x",
            ttm_val=company.pe_ratio,
            est_val=fe.pe_ratio if fe else None)
    add_row("ROE", lambda a: a.roe, "%",
            ttm_val=company.roe)
    add_row("Div. Yield", lambda a: a.div_yield, "%",
            ttm_val=company.dividend_yield)
    _ttm_fcf = getattr(company, 'ttm_fcf', None)
    add_row(f"FCF ({cur}M)", lambda a: a.fcf, "M",
            ttm_val=_ttm_fcf)
    add_row("FCF Yield", lambda a: a.fcf_yield, "%",
            ttm_val=company.fcf_yield)

    # EV — only show historical when balance sheet data is available.
    # If net_debt is None, the annual EV was computed as just market_cap
    # (ignoring cash), which is wrong. Show n/a in that case; the TTM
    # column uses company.enterprise_value which comes from yfinance/EODHD
    # directly and is always correct.
    def _annual_ev(a):
        if a is None or a.market_cap is None:
            return None
        if a.net_debt is not None:
            return a.market_cap + a.net_debt
        if a.total_debt is not None and a.cash is not None:
            return a.market_cap + (a.total_debt - a.cash)
        return None

    add_row("EV", _annual_ev, "M",
            ttm_val=company.enterprise_value)
    add_row("EV/EBIT", lambda a: a.ev_ebit, "x",
            ttm_val=company.ev_ebit)
    add_row("EV/Sales", lambda a: a.ev_sales, "x",
            ttm_val=ttm_ev_sales,
            est_val=fe.ev_sales if fe else None)

    add_row("Mkt Cap (M)", lambda a: a.market_cap, "M",
            ttm_val=company.market_cap)
    # For TTM shares, prefer latest annual (more reliable than info["sharesOutstanding"]
    # which can be the float-adjusted count and differs from total issued shares).
    _la_raw = _latest_af.shares_outstanding if _latest_af and _latest_af.shares_outstanding else None
    _ttm_shares = (
        _la_raw / 1_000_000 if _la_raw and _la_raw > 1_000_000 else _la_raw
    ) if _la_raw else company.shares_outstanding
    add_row("Shares Out. (M)", lambda a: (
        a.shares_outstanding / 1_000_000
        if a.shares_outstanding and a.shares_outstanding > 1_000_000
        else a.shares_outstanding
    ), "ps",
        ttm_val=_ttm_shares)

    all_rows = [col_headers] + rows_data

    ttm_col = len(show_years) + 1   # TTM column index
    est_col = len(show_years) + 2   # estimate column index

    label_w  = 88 if n_data_cols >= 9 else 105
    data_w   = (CW - label_w) / n_data_cols
    col_widths = [label_w] + [data_w] * n_data_cols

    t = Table(all_rows, colWidths=col_widths, repeatRows=1)

    body_fs  = 6.5 if n_data_cols >= 9 else 7.5
    hdr_fs   = 7.0 if n_data_cols >= 9 else 7.5
    cell_pad = 2   if n_data_cols >= 9 else 5

    # Alternating row stripe colors (same palette as EODHD data table on page 6)
    ROW_WHITE = HexColor("#FFFFFF")
    ROW_STRIPE = HexColor("#F6F8FA")
    # Header row is white; data rows alternate starting with stripe on row index 1
    row_bgs = []
    for i in range(len(all_rows)):
        if i == 0:
            row_bgs.append(ROW_WHITE)           # header
        elif i % 2 == 1:
            row_bgs.append(ROW_STRIPE)          # odd data rows → stripe
        else:
            row_bgs.append(ROW_WHITE)           # even data rows → white

    ts = [
        # Row stripe backgrounds (covers entire table including label column)
        ('ROWBACKGROUNDS', (0,0), (-1,-1), row_bgs),
        # Header row overrides
        ('TEXTCOLOR',   (0,0), (-1,0), NAVY),
        ('FONTNAME',    (0,0), (-1,0), BOLD_FONT),
        ('FONTSIZE',    (0,0), (-1,0), hdr_fs),
        ('ALIGN',       (0,0), (-1,0), 'CENTER'),
        ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
        # Header underline (thinner than before) + row separators matching EODHD data table
        ('LINEBELOW',   (0,0), (-1,0),  0.8, NAVY),
        ('LINEBELOW',   (0,1), (-1,-1), 0.3, HexColor("#DDDDDD")),
        # TTM and estimate column headers — navy text, no special background
        ('TEXTCOLOR',   (ttm_col,0), (ttm_col,0), NAVY),
        ('TEXTCOLOR',   (est_col,0), (est_col,0), NAVY),
        # Label column
        ('FONTNAME',    (0,1), (0,-1), BOLD_FONT),
        ('FONTSIZE',    (0,1), (0,-1), body_fs),
        ('ALIGN',       (0,1), (0,-1), 'LEFT'),
        # Data columns
        ('ALIGN',       (1,1), (-1,-1), 'RIGHT'),
        ('FONTSIZE',    (1,1), (-1,-1), body_fs),
        # Padding
        ('TOPPADDING',  (0,0), (-1,-1), 3),
        ('BOTTOMPADDING',(0,0),(-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), cell_pad),
        ('RIGHTPADDING',(0,0), (-1,-1), cell_pad),
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
        peer_ccy = (c.currency or c.currency_price or "").strip().upper()
        # Recompute EV/Sales from EV + latest-annual revenue to bypass stale cached
        # yfinance enterpriseToRevenue scalars (especially wrong for TSE stocks).
        _peer_ev = c.enterprise_value
        if _peer_ev is None and c.market_cap is not None and c.net_debt is not None:
            _peer_ev = c.market_cap + c.net_debt
        _peer_evsales = None
        if _peer_ev and la and la.revenue and la.revenue > 0:
            _peer_evsales = _peer_ev / la.revenue
        else:
            _peer_evsales = c.ev_sales
        row = [
            Paragraph(
                f"<b>{c.name or c.ticker}</b>" if is_anchor else (c.name or c.ticker),
                styles["table_label"]
            ),
            p_cell(c.ticker),
            p_cell(_fmt_with_ccy(la.revenue if la else None, peer_ccy), right=True),
            p_cell(_fmt_with_ccy(c.market_cap, peer_ccy), right=True),
            p_cell(_fmt_pct(c.roe),         right=True),
            p_cell(_fmt_x(c.price_to_book), right=True),
            p_cell(_fmt_x(c.pe_ratio),      right=True),
            p_cell(_fmt_x(c.ev_ebit),       right=True),
            p_cell(_fmt_x(_peer_evsales),   right=True),
            p_cell(_fmt_x(c.gearing),       right=True),
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
    """Returns a section heading (no underline)."""
    return Paragraph(f"<b>{text}</b>", styles["section_title"])


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
        adv_result=None,       # Optional[AdversarialResult]
        news_summary=None,     # Optional[dict] — structured news from LLM
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

        _uses_eodhd = (
            any(getattr(af, "source", "") == "eodhd"
                for af in company.annual_financials.values())
            or "eodhd" in (company.data_sources or [])
        )

        # ── PAGE 1: Financial Table + Snapshot + Current News ────────────────
        story += self._page1(company, analysis, styles, news_summary=news_summary)
        story.append(PageBreak())

        # ── PAGE 2: Bull / Bear + Recommendation ─────────────────────────────
        story += self._page2(analysis, styles)
        story.append(PageBreak())

        # ── PAGE 3: Peer Table + Checklist ───────────────────────────────────
        story += self._page3(company, analysis, peers, checklist, styles)

        # ── PAGE 4 & 5: Data pages depend on source ───────────────────────────
        story.append(PageBreak())
        if _uses_eodhd:
            story += self._page4_eodhd_data(company, styles)
            story.append(PageBreak())
            story += self._page4_provenance(company, styles)
        else:
            story += self._page4_yfinance(company, styles)

        # ── PAGE 5 (optional): Adversarial Review ────────────────────────────
        if adv_result is not None:
            from agents.pdf_adversarial import build_adversarial_page
            story.append(PageBreak())
            story += build_adversarial_page(adv_result)

        doc.build(story, onFirstPage=_page_header, onLaterPages=_page_header)
        logger.info(f"[PDF V2] Saved: {output_path}")

    # ── V2 Page 4: yfinance Market Data (fallback when EODHD not available) ──
    def _page4_yfinance(self, company: CompanyData, styles: dict) -> list:
        """Single combined data + provenance page for non-EODHD reports."""
        from reportlab.platypus import Table, TableStyle

        el = []

        el.append(Paragraph(
            "Market Data — yfinance (Yahoo Finance)",
            ParagraphStyle("yf4title", fontName=BOLD_FONT, fontSize=12,
                           textColor=NAVY, spaceAfter=2, leading=15),
        ))
        is_uk   = (company.ticker or "").upper().endswith(".L")
        is_balt = any((company.ticker or "").upper().endswith(s)
                      for s in (".VS", ".TL", ".RG"))
        exch_note = (
            "London Stock Exchange (LSE) — prices in GBP (converted from GBX)." if is_uk
            else "Baltic exchange — limited EODHD coverage; data from yfinance." if is_balt
            else "EODHD does not cover this exchange. Data sourced from yfinance (Yahoo Finance)."
        )
        el.append(Paragraph(
            f"Data source: <b>yfinance</b>. {exch_note} "
            "Historical depth is typically 3–5 years. Some fields "
            "(ISIN, detailed officers, intraday prices) may be unavailable.",
            ParagraphStyle("yf4sub", fontName=BASE_FONT, fontSize=7.5,
                           textColor=MGRAY, leading=11, spaceAfter=8),
        ))

        def _kv2(pairs):
            ls = ParagraphStyle("kl2", fontName=BOLD_FONT, fontSize=8,
                                textColor=NAVY, leading=10)
            vs = ParagraphStyle("kv2", fontName=BASE_FONT, fontSize=8,
                                textColor=HexColor("#111111"), leading=10)
            if len(pairs) % 2:
                pairs = list(pairs) + [("", "")]
            rows = []
            for i in range(0, len(pairs), 2):
                l1, v1 = pairs[i]; l2, v2 = pairs[i + 1]
                rows.append([
                    Paragraph(l1, ls), Paragraph(str(v1), vs),
                    Paragraph(l2, ls), Paragraph(str(v2), vs),
                ])
            cw = CW / 4
            t = Table(rows, colWidths=[cw*0.85, cw*1.15, cw*0.85, cw*1.15])
            t.setStyle(TableStyle([
                ("VALIGN",        (0,0), (-1,-1), "TOP"),
                ("TOPPADDING",    (0,0), (-1,-1), 2),
                ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                ("LINEBELOW",     (0,0), (-1,-1), 0.3, HexColor("#DDDDDD")),
                ("LEFTPADDING",   (0,0), (-1,-1), 2),
                ("RIGHTPADDING",  (0,0), (-1,-1), 4),
                ("ROWBACKGROUNDS",(0,0),(-1,-1), [HexColor("#FFFFFF"), HexColor("#F6F8FA")]),
            ]))
            return t

        def _v(val, fmt=None):
            if val is None or val == "" or val == "NA":
                return "—"
            try:
                f = float(val)
                if fmt == "price": return f"{f:,.2f}"
                if fmt == "pct":   return f"{f:.2f}%"
                if fmt == "b":
                    if abs(f) >= 1e9: return f"{f/1e9:,.2f}B"
                    if abs(f) >= 1e6: return f"{f/1e6:,.1f}M"
                    return f"{f:,.0f}"
                return str(val)
            except (ValueError, TypeError):
                return str(val) if val else "—"

        def _sec_hdr(title):
            return Paragraph(title,
                ParagraphStyle("yfdh", fontName=BOLD_FONT, fontSize=9.5,
                               textColor=NAVY, spaceBefore=10, spaceAfter=3, leading=12))

        def _rule():
            return HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=4)

        # Identity
        el.append(_sec_hdr("Company Identity & Listing"))
        el.append(_rule())
        el.append(_kv2([
            ("Company Name",      company.name or "—"),
            ("Ticker",            company.ticker or "—"),
            ("Exchange",          company.exchange or "—"),
            ("Currency",          (company.currency_price or company.currency) or "—"),
            ("Sector",            company.sector or "—"),
            ("Industry",          company.industry or "—"),
            ("Country",           company.country or "—"),
            ("Employees",         _v(company.employees, "b") if company.employees else "—"),
            ("Website",           company.website or "—"),
            ("IPO Date",          company.ipo_date or "—"),
            ("Fiscal Year End",   company.fiscal_year_end or "—"),
            ("ISIN",              company.isin or "—"),
        ]))

        # Trading snapshot
        el.append(_sec_hdr("Trading Snapshot"))
        el.append(_rule())
        el.append(_kv2([
            ("Price",             _v(company.current_price, "price")),
            ("Market Cap",        _v(company.market_cap, "b")),
            ("Enterprise Value",  _v(company.enterprise_value, "b")),
            ("52-Week High",      _v(company.week_52_high, "price")),
            ("52-Week Low",       _v(company.week_52_low, "price")),
            ("50-Day MA",         _v(company.ma_50, "price")),
            ("200-Day MA",        _v(company.ma_200, "price")),
            ("Beta",              _v(company.beta)),
            ("Div Yield",         _v(company.dividend_yield, "pct") if company.dividend_yield else "—"),
            ("Shares Out.",       _v(company.shares_outstanding, "b")),
            ("Shares Float",      _v(company.shares_float, "b")),
            ("% Insiders",        _v(company.pct_insiders, "pct") if company.pct_insiders else "—"),
        ]))

        # Provenance table
        el.append(_sec_hdr("Data Provenance — yfinance Fields"))
        el.append(_rule())
        chk = "✓ yfinance"
        na  = "— n/a"
        rows = [[
            Paragraph("<b>Field</b>", styles["table_header"]),
            Paragraph("<b>yfinance Source</b>", styles["table_header"]),
            Paragraph("<b>Status</b>", styles["table_header"]),
        ]]
        for row in [
            ("Company name / sector / industry",  "Ticker.info → longName, sector",        chk),
            ("Current price",                     "Ticker.info → currentPrice / regularMarketPrice", chk),
            ("Market cap / shares outstanding",   "Ticker.info → marketCap, sharesOutstanding", chk),
            ("Enterprise value",                  "Ticker.info → enterpriseValue",          chk),
            ("P/E (TTM) / Forward P/E",           "Ticker.info → trailingPE / forwardPE",   chk),
            ("EV/EBITDA / EV/Revenue",            "Ticker.info → enterpriseToEbitda / enterpriseToRevenue", chk),
            ("Annual revenue / net income",       "Ticker.financials → Total Revenue, Net Income", chk),
            ("Annual EPS (diluted)",              "Ticker.financials → Diluted EPS",        chk),
            ("TTM revenue / EBITDA",              "Ticker.quarterly_financials → last 4Q sum", chk),
            ("Dividends per share",               "Ticker.dividends (by fiscal year)",      chk),
            ("52-Week High / Low",                "Ticker.info → fiftyTwoWeekHigh/Low",     chk),
            ("50 / 200-Day MA",                   "Ticker.info → fiftyDayAverage, twoHundredDayAverage", chk),
            ("Beta",                              "Ticker.info → beta",                     chk),
            ("Next earnings date",                "Ticker.info → earningsDate",             chk),
            ("ISIN",                              "Ticker.isin",                            chk),
            ("Intraday price / real-time data",   "Not available via yfinance free tier",   na),
            ("EODHD fundamentals depth (10yr+)",  "EODHD subscription required",            "— EODHD"),
        ]:
            rows.append([
                Paragraph(row[0], styles["table_body"]),
                Paragraph(row[1], styles["table_body"]),
                Paragraph(row[2], ParagraphStyle("yf_st", parent=styles["table_body"],
                    fontName=BOLD_FONT,
                    textColor=HexColor("#2E7D32") if row[2].startswith("✓") else MGRAY)),
            ])
        col_w = [CW * 0.35, CW * 0.47, CW * 0.18]
        t = Table(rows, colWidths=col_w)
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), HexColor("#003F54")),
            ("TEXTCOLOR",     (0,0), (-1,0), colors.white),
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",    (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
            ("LINEBELOW",     (0,0), (-1,-2), 0.3, HexColor("#DDDDDD")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [HexColor("#FFFFFF"), HexColor("#F6F8FA")]),
        ]))
        el.append(t)

        return el

    # ── V2 Page 4: EODHD Direct Data ─────────────────────────────────────────
    def _page4_eodhd_data(self, company: CompanyData, styles: dict) -> list:
        """
        Identity & Listing, Trading Data, and Technical Levels pulled directly
        from EODHD — displayed as-is without LLM processing.
        """
        rt  = getattr(company, "_rt_data_v2", {}) or {}
        eod = getattr(company, "_eod_data_v2", []) or []

        el = []

        # ── helpers ───────────────────────────────────────────────────────────
        def _v(val, fmt=None):
            if val is None or val == "" or val == "NA":
                return "—"
            try:
                f = float(val)
                if fmt == "int":    return f"{f:,.0f}"
                if fmt == "price":  return f"{f:,.2f}"
                if fmt == "pct":    return f"{f:.2f}%"
                if fmt == "pct_d":  return f"{f * 100:.2f}%"   # decimal → %
                if fmt == "b":
                    if abs(f) >= 1e9: return f"{f/1e9:,.2f}B"
                    if abs(f) >= 1e6: return f"{f/1e6:,.1f}M"
                    return f"{f:,.0f}"
                return str(val)
            except (ValueError, TypeError):
                return str(val) if val else "—"

        def _sec_hdr(title):
            return Paragraph(
                title,
                ParagraphStyle("dh", fontName=BOLD_FONT, fontSize=9.5,
                               textColor=NAVY, spaceBefore=10, spaceAfter=3,
                               borderPad=0, leading=12),
            )

        def _rule():
            return HRFlowable(width="100%", thickness=0.5,
                              color=BORDER, spaceAfter=4)

        # two-column KV table — label bold navy, value plain dark
        def _kv2(pairs: list[tuple]) -> Table:
            """pairs = [(label, value), ...] — rendered in 2 side-by-side columns."""
            ls = ParagraphStyle("kl", fontName=BOLD_FONT, fontSize=8,
                                textColor=NAVY, leading=10)
            vs = ParagraphStyle("kv", fontName=BASE_FONT, fontSize=8,
                                textColor=HexColor("#111111"), leading=10)
            # Fill to even number
            if len(pairs) % 2:
                pairs = list(pairs) + [("", "")]
            rows = []
            for i in range(0, len(pairs), 2):
                l1, v1 = pairs[i]
                l2, v2 = pairs[i + 1]
                rows.append([
                    Paragraph(l1, ls), Paragraph(str(v1), vs),
                    Paragraph(l2, ls), Paragraph(str(v2), vs),
                ])
            cw = CW / 4
            t = Table(rows, colWidths=[cw * 0.85, cw * 1.15, cw * 0.85, cw * 1.15])
            t.setStyle(TableStyle([
                ("VALIGN",       (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING",   (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
                ("LINEBELOW",    (0, 0), (-1, -1), 0.3, HexColor("#DDDDDD")),
                ("LEFTPADDING",  (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("ROWBACKGROUNDS",(0,0),(-1,-1), [HexColor("#FFFFFF"), HexColor("#F6F8FA")]),
            ]))
            return t

        # ── Page title ────────────────────────────────────────────────────────
        el.append(Paragraph(
            "EODHD Market Data",
            ParagraphStyle("pg4title", fontName=BOLD_FONT, fontSize=12,
                           textColor=NAVY, spaceAfter=2, leading=15),
        ))
        el.append(Paragraph(
            "Direct data from EODHD /fundamentals and /real-time endpoints — no LLM processing.",
            ParagraphStyle("pg4sub", fontName=BASE_FONT, fontSize=7.5,
                           textColor=MGRAY, spaceAfter=6, leading=10),
        ))
        el.append(HRFlowable(width="100%", thickness=1, color=NAVY, spaceAfter=8))

        # ── SECTION 1: Identity & Listing ────────────────────────────────────
        el.append(_sec_hdr("IDENTITY & LISTING"))
        el.append(_rule())

        cur = company.currency or company.currency_price or ""
        identity_pairs = [
            ("Company Name",    company.name or "—"),
            ("Ticker",          company.ticker or "—"),
            ("ISIN",            _v(company.isin)),
            ("Exchange",        _v(company.exchange)),
            ("Sector",          _v(company.sector)),
            ("Industry",        _v(company.industry)),
            ("Country",         _v(company.country)),
            ("Currency",        cur or "—"),
            ("IPO Date",        _v(company.ipo_date)),
            ("Fiscal Year End", _v(company.fiscal_year_end)),
            ("Employees",       _v(company.employees, "int")),
            ("Website",         _v(company.website)),
        ]
        el.append(_kv2(identity_pairs))
        el.append(Spacer(1, 6))

        # ── SECTION 2: Trading Data ───────────────────────────────────────────
        el.append(_sec_hdr("TRADING DATA"))
        el.append(_rule())

        # Compute avg volume from EOD last 30 trading days
        avg_vol = None
        if eod:
            vols = [float(r["volume"]) for r in eod[-30:]
                    if isinstance(r, dict) and r.get("volume") not in (None, "", 0)]
            if vols:
                avg_vol = sum(vols) / len(vols)

        change_v   = rt.get("change")
        change_p_v = rt.get("change_p")
        if change_v is not None and change_p_v is not None:
            try:
                chg_sign = "+" if float(change_v) >= 0 else ""
                change_str = f"{chg_sign}{float(change_v):,.2f} ({chg_sign}{float(change_p_v):.2f}%)"
            except (ValueError, TypeError):
                change_str = "—"
        else:
            change_str = "—"

        mc  = company.market_cap
        ev  = company.enterprise_value
        trading_pairs = [
            ("Last Price",      _v(rt.get("close") or rt.get("previousClose"), "price") + f" {cur}"),
            ("Prev. Close",     _v(rt.get("previousClose"), "price") + f" {cur}"),
            ("Open",            _v(rt.get("open"), "price") + f" {cur}"),
            ("Change",          change_str),
            ("Day High",        _v(rt.get("high"), "price") + f" {cur}"),
            ("Day Low",         _v(rt.get("low"), "price") + f" {cur}"),
            ("Volume",          _v(rt.get("volume"), "int")),
            ("Avg Volume (30d)",_v(avg_vol, "int") if avg_vol else "—"),
            ("Market Cap",      (_v(mc * 1e6, "b") + f" {cur}") if mc else "—"),
            ("Enterprise Value",(_v(ev * 1e6, "b") + f" {cur}") if ev else "—"),
            ("Shares Outstanding", (_v(company.shares_outstanding, "int") + "M") if company.shares_outstanding else "—"),
            ("Shares Float",    (_v(company.shares_float, "int") + "M") if company.shares_float else "—"),
        ]
        el.append(_kv2(trading_pairs))
        el.append(Spacer(1, 6))

        # ── SECTION 3: Technical Levels ───────────────────────────────────────
        el.append(_sec_hdr("TECHNICAL LEVELS"))
        el.append(_rule())

        sh = company.shares_short
        sh_str = _v(sh * 1e6, "int") if sh else "—"   # stored in millions → full units

        tech_pairs = [
            ("52-Week High",    _v(company.week_52_high, "price") + f" {cur}"),
            ("52-Week Low",     _v(company.week_52_low, "price") + f" {cur}"),
            ("50-Day MA",       _v(company.ma_50, "price") + f" {cur}"),
            ("200-Day MA",      _v(company.ma_200, "price") + f" {cur}"),
            ("Beta",            _v(company.beta, "price")),
            ("Short Ratio",     (_v(company.short_ratio, "price") + " days") if company.short_ratio else "—"),
            ("Shares Short",    sh_str),
            ("Short % Float",   _v(company.short_percent_of_float, "pct_d")),
        ]
        el.append(_kv2(tech_pairs))

        return el

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

    def _page1(self, company: CompanyData, analysis: dict, styles: dict, news_summary=None) -> list:
        el = []

        _uses_eodhd = (
            any(getattr(af, "source", "") == "eodhd"
                for af in company.annual_financials.values())
            or "eodhd" in (company.data_sources or [])
        )

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
        # Small footnotes under Financial Summary (no border)
        _note_style = ParagraphStyle("tbl_note", fontName=BASE_FONT, fontSize=6,
                                     textColor=MGRAY, spaceBefore=2, leading=8)
        # Data source line
        if _uses_eodhd:
            _src_note = (
                'Investment Memo V2  ·  Data source: EODHD All-In-One API  ·  '
                'Fields marked <font name="ZapfDingbats" color="#2E7D32" size="6">4</font>'
                ' = EODHD verified.'
            )
        else:
            _src_note = (
                'Investment Memo V2  ·  Data source: yfinance (Yahoo Finance) — '
                'EODHD does not cover this exchange. Depth typically 3–5 years.'
            )
        el.append(Paragraph(_src_note, _note_style))

        # TTM date footnote — each line is independent
        _note_bold_ttm = ParagraphStyle(
            "tbl_note_bold_ttm", parent=_note_style, fontName=BOLD_FONT,
        )
        _ttm_date = getattr(company, 'ttm_last_quarter_date', None)
        if _ttm_date:
            el.append(Paragraph(
                f"<b>TTM / Most Recent Quarter: {_ttm_date}</b>",
                _note_bold_ttm,
            ))
        _next_ed = getattr(company, 'next_earnings_date', None) or "—"
        el.append(Paragraph(
            f"Next earnings report: {_next_ed}",
            _note_bold_ttm,
        ))

        el.append(Spacer(1, 6))

        # Investment Snapshot
        el.append(section_title("Investment Snapshot", styles))
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

        # Current News — web-search narrative (markdown with **bold** headers)
        _narrative = (news_summary or {}).get("narrative", "").strip()
        if _narrative:
            el.append(Spacer(1, 6))
            el.append(section_title("Current News", styles))
            import re as _re
            # 1. Strip citation markers [1], [2], [1,2] etc.
            _clean = _re.sub(r'\s*\[\d+(?:,\s*\d+)*\]', '', _narrative)
            # 2. Strip markdown document headings (# or ##)
            _clean = _re.sub(r'^#{1,3}\s+.*$', '', _clean, flags=_re.MULTILINE)
            # 3. Split on bold headers — produces alternating [body, header, body, ...]
            _parts = _re.split(r'(\*\*[^*\n]+\*\*)', _clean)
            for _part in _parts:
                _part = _part.strip()
                if not _part:
                    continue
                if _part.startswith("**") and _part.endswith("**"):
                    # Bold section header
                    _hdr = _part[2:-2].strip().rstrip(";:—- ")
                    if _hdr:
                        el.append(Paragraph(_hdr, styles["news_sub"]))
                else:
                    # Body: collapse all newlines into spaces, clean stray punctuation
                    _body = _re.sub(r'\s*\n\s*', ' ', _part)      # newlines → space
                    _body = _re.sub(r'\s{2,}', ' ', _body)         # multiple spaces → one
                    _body = _re.sub(r'^\s*[,\.;]\s*', '', _body)   # leading punctuation
                    _body = _re.sub(r'\s+([.!?,;:])', r'\1', _body) # "word ." → "word."
                    _body = _body.strip()
                    if len(_body) > 4:  # skip stray punctuation fragments
                        el.append(Paragraph(_body, styles["news_item"]))

        return el

    def _page2(self, analysis: dict, styles: dict) -> list:
        el = []

        # Bull case
        el.append(section_title("Bull Case — Why Invest", styles))
        bull = analysis.get("bull_case", "Bull case not available.")
        for para in _split_paragraphs(bull):
            el.append(Paragraph(para, styles["body"]))

        el.append(Spacer(1, 8))

        # Bear case
        el.append(section_title("Bear Case — Devil's Advocate", styles))
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
        el.append(Paragraph(
            "<i>* ROE, P/E, EV/EBIT, EV/Sales and Gearing are TTM (trailing twelve months) "
            "or most recent available. Market Cap reflects current price. "
            "Annual Sales is from the latest reported fiscal year.</i>",
            ParagraphStyle("peer_fn", parent=styles["body_small"],
                           fontSize=6.5, leading=9, spaceAfter=4)
        ))

        el.append(Spacer(1, 8))

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
        el.append(Paragraph(
            "<i>* Checklist metrics use TTM (trailing twelve months) data from the "
            "primary data source (EODHD or yfinance). Where TTM is unavailable, "
            "the most recent annual value is used as a fallback.</i>",
            ParagraphStyle("ck_fn", parent=styles["body_small"],
                           fontSize=6.5, leading=9, spaceAfter=4)
        ))

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

def _split_paragraphs(text: str, sentences_per_para: int = 4) -> list[str]:
    """Split long text into readable paragraphs for ReportLab.

    Tries double-newline splits first. If the LLM returned a single block
    (common for snapshot/bull/bear), falls back to grouping every
    `sentences_per_para` sentences into one paragraph.
    """
    import re as _re
    if not text:
        return [""]
    # Prefer explicit paragraph breaks from the LLM
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    if len(paras) >= 2:
        return paras
    # Fallback: split on sentence endings then group
    sentences = _re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]
    grouped = []
    for i in range(0, len(sentences), sentences_per_para):
        grouped.append(" ".join(sentences[i:i + sentences_per_para]))
    return grouped if grouped else [text]


def _fmt_b(v) -> str:
    if v is None: return "n/a"
    if abs(v) >= 1000: return f"{v/1000:.2f}B"
    return f"{v:,.2f}M"

def _fmt_pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"

def _fmt_x(v) -> str:
    return f"{v:.1f}x" if v is not None else "n/a"
