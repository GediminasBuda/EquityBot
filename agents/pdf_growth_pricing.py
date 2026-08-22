"""
pdf_growth_pricing.py — Growth Pricing & Peers PDF renderer.

Same header/footer structure, colour palette, and typography as Investment
Memo V2 / Growth Quality Score (agents/pdf_growth_quality.py) — copied
verbatim per design requirement, with only the subtitle literal changed.

Renders: a rules-based Valuation Verdict banner (Attractive / Moderate /
Expensive, computed deterministically in Python), then a current/forward-
looking snapshot, then three dimensions (Revenue & Gross Profit, Earnings,
Cash Flow) — each a verified Python-computed table (years as columns, never
LLM output) followed by the LLM's grounded narrative for that dimension —
then a verified Rule of 40 table (Revenue Growth vs. EBITDA/FCF/Net Margin,
static explanatory text, no LLM narrative) — then a verified Peer Comparison
table (subject + up to 5 peers, most-recent-data snapshot across 8 metrics
plus a composite Valuation Score row, static explanatory text, no LLM
narrative) — and finally a closing "What's Priced In?" synthesis section.
All scoring/ranking is computed in Python, never by the LLM.
"""

from __future__ import annotations
import logging
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, Color
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak,
)

from config import LLM_PROVIDER, LLM_MODEL
from data_sources.base import CompanyData
from models.growth_pricing import (
    compute_revenue_gp_table, compute_earnings_table, compute_cash_flow_table,
    compute_current_snapshot, compute_rule_of_40_table, compute_peer_comparison_table,
    compute_peer_scores, compute_valuation_verdict,
)

logger = logging.getLogger(__name__)

# ── Dimensions ───────────────────────────────────────────────────────────
W, H    = A4
ML = MR = 18*mm
MT      = 28*mm
MB      = 14*mm
CW      = W - ML - MR

# ── Colour palette (identical to Investment Memo V2 / Growth Quality) ────
NAVY    = HexColor('#003F54')
LBLUE   = HexColor('#D6E8F7')
GREEN   = HexColor('#1A7E3D')
RED     = HexColor('#C0392B')
AMBER   = HexColor('#D68910')
MGRAY   = HexColor('#555555')
BORDER  = HexColor('#BBCCDD')

NAVY_HEX  = "#003F54"
GREEN_HEX = "#1A7E3D"
RED_HEX   = "#C0392B"
AMBER_HEX = "#D68910"
MGRAY_HEX = "#555555"


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
            bold_path = os.path.join(d, "calibri.ttf")
            pdfmetrics.registerFont(TTFont("CalibriLight-Bold",
                                           bold_path if os.path.exists(bold_path) else light_path))
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


def _styles() -> dict:
    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    return {
        "section_title": S("section_title",
            fontName=BOLD_FONT, fontSize=9, textColor=NAVY,
            spaceBefore=8, spaceAfter=3, leading=11,
        ),
        "dim_title": S("dim_title",
            fontName=BOLD_FONT, fontSize=11, textColor=NAVY,
            spaceBefore=4, spaceAfter=2, leading=14,
        ),
        "dim_question": S("dim_question",
            fontName=OBLIQUE_FONT, fontSize=9, textColor=MGRAY,
            spaceAfter=6, leading=12,
        ),
        "body": S("body",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor('#222222'),
            leading=13, alignment=TA_JUSTIFY, spaceAfter=8,
        ),
        "table_header": S("th",
            fontName=BOLD_FONT, fontSize=7.3, textColor=NAVY,
            alignment=TA_CENTER, leading=9.5,
        ),
        "table_label": S("tl",
            fontName=BOLD_FONT, fontSize=7.5, textColor=HexColor('#222222'),
            alignment=TA_LEFT, leading=10,
        ),
        "table_cell": S("tc",
            fontName=BASE_FONT, fontSize=7.3, textColor=HexColor('#111111'),
            alignment=TA_CENTER, leading=9.5,
        ),
        "narrative_body": S("narrative_body",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor('#222222'),
            leading=13, alignment=TA_JUSTIFY, spaceAfter=6,
        ),
        "synthesis_heading": S("synthesis_heading",
            fontName=BOLD_FONT, fontSize=12, textColor=NAVY,
            spaceBefore=4, spaceAfter=6, leading=15,
        ),
    }


# ── Page header (drawn on canvas, verbatim from pdf_growth_quality.py
#    except the subtitle literal) ────────────────────────────────────────

def _draw_header(canvas, doc, company: CompanyData, report_date: str):
    canvas.saveState()

    NAME_Y     = H - 11*mm
    SUBTITLE_Y = H - 17*mm
    LINE_Y     = H - 21*mm

    canvas.setFont(BOLD_FONT, 14)
    canvas.setFillColor(NAVY)
    name = company.name or company.ticker
    canvas.drawString(ML, NAME_Y, name)

    subtitle = " | ".join(filter(None, [
        "Growth Pricing & Peers",
        company.sector, company.country,
        company.exchange, company.ticker, report_date
    ]))
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, SUBTITLE_Y, subtitle)

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

    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.8)
    canvas.line(ML, LINE_Y, W - MR, LINE_Y)

    canvas.setFont(BASE_FONT, 7)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(W - MR, 8*mm,
                           f"Page {doc.page}  |  Your Humble EquityBot  |  {LLM_MODEL}")

    canvas.restoreState()


def section_title(text: str, styles: dict):
    return Paragraph(f"<b>{text}</b>", styles["section_title"])


def _fmt_b(v) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.2f}B" if abs(v) >= 1000 else f"{v:.2f}M"


def _pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"


def _x(v) -> str:
    return f"{v:.1f}x" if v is not None else "n/a"


def _num(v) -> str:
    return f"{v:.2f}" if v is not None else "n/a"


# ── Verified tables (Python-computed, ground truth) ─────────────────────

def _table_style() -> TableStyle:
    return TableStyle([
        ('BACKGROUND', (0,0), (-1,0), LBLUE),
        ('LINEBELOW', (0,0), (-1,0), 0.8, NAVY),
        ('LINEBELOW', (0,1), (-1,-1), 0.4, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ])


def _metric_rows_table(years: list[str], metric_rows: list[tuple[str, list[str]]],
                        styles: dict, label_frac: float = 0.22) -> Table:
    """Shared builder: metrics as rows (left label column), fiscal years as
    column headers across the top — same visual layout used across every
    verified table in this report."""
    header = [Paragraph("Metric", styles["table_header"])] + \
             [Paragraph(y, styles["table_header"]) for y in years]
    rows = [header]
    for label, values in metric_rows:
        rows.append([Paragraph(label, styles["table_label"])] +
                    [Paragraph(v, styles["table_cell"]) for v in values])

    n = len(years)
    first_w = CW * label_frac
    other_w = (CW - first_w) / max(n, 1)
    col_widths = [first_w] + [other_w] * n

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(_table_style())
    return t


def _revenue_gp_table(company: CompanyData, styles: dict) -> Table | None:
    rows_data = compute_revenue_gp_table(company)
    if not rows_data:
        return None

    candidates = [
        ("Sales", [_fmt_b(r["revenue"]) for r in rows_data],
         any(r["revenue"] is not None for r in rows_data)),
        ("Sales Growth YoY", [_pct(r["sales_yoy"]) for r in rows_data],
         any(r["sales_yoy"] is not None for r in rows_data)),
        ("EV / Sales", [_x(r["ev_sales"]) for r in rows_data],
         any(r["ev_sales"] is not None for r in rows_data)),
        ("EV / Gross Profit", [_x(r["ev_gross_profit"]) for r in rows_data],
         any(r["ev_gross_profit"] is not None for r in rows_data)),
        ("Gross Margin", [_pct(r["gross_margin"]) for r in rows_data],
         any(r["gross_margin"] is not None for r in rows_data)),
        ("Gross Profit 3Y CAGR", [_pct(r["gross_profit_cagr3"]) for r in rows_data],
         any(r["gross_profit_cagr3"] is not None for r in rows_data)),
        ("Gross Profit 5Y CAGR", [_pct(r["gross_profit_cagr5"]) for r in rows_data],
         any(r["gross_profit_cagr5"] is not None for r in rows_data)),
        ("Net Profit Margin", [_pct(r["net_margin"]) for r in rows_data],
         any(r["net_margin"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _earnings_table(company: CompanyData, styles: dict) -> Table | None:
    rows_data = compute_earnings_table(company)
    if not rows_data:
        return None

    candidates = [
        ("Market Capitalization", [_fmt_b(r["market_cap"]) for r in rows_data],
         any(r["market_cap"] is not None for r in rows_data)),
        ("Enterprise Value", [_fmt_b(r["enterprise_value"]) for r in rows_data],
         any(r["enterprise_value"] is not None for r in rows_data)),
        ("P/E", [_x(r["pe_ratio"]) for r in rows_data],
         any(r["pe_ratio"] is not None for r in rows_data)),
        ("EV / EBITDA", [_x(r["ev_ebitda"]) for r in rows_data],
         any(r["ev_ebitda"] is not None for r in rows_data)),
        ("EV / EBIT", [_x(r["ev_ebit"]) for r in rows_data],
         any(r["ev_ebit"] is not None for r in rows_data)),
        ("EV / NOPAT", [_x(r["ev_nopat"]) for r in rows_data],
         any(r["ev_nopat"] is not None for r in rows_data)),
        ("ROIC", [_pct(r["roic"]) for r in rows_data],
         any(r["roic"] is not None for r in rows_data)),
        ("PEG (using EPS Growth)", [_num(r["peg_eps_growth"]) for r in rows_data],
         any(r["peg_eps_growth"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _cash_flow_table(company: CompanyData, styles: dict) -> Table | None:
    rows_data = compute_cash_flow_table(company)
    if not rows_data:
        return None

    candidates = [
        ("EV / Free Cash Flow", [_x(r["ev_fcf"]) for r in rows_data],
         any(r["ev_fcf"] is not None for r in rows_data)),
        ("FCF Yield", [_pct(r["fcf_yield"]) for r in rows_data],
         any(r["fcf_yield"] is not None for r in rows_data)),
        ("FCF Margin", [_pct(r["fcf_margin"]) for r in rows_data],
         any(r["fcf_margin"] is not None for r in rows_data)),
        ("FCF Conversion", [_pct(r["fcf_conversion"]) for r in rows_data],
         any(r["fcf_conversion"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _rule_of_40_table(company: CompanyData, styles: dict) -> Table | None:
    rows_data = compute_rule_of_40_table(company)
    if not rows_data:
        return None

    candidates = [
        ("Revenue Growth YoY", [_pct(r["revenue_yoy"]) for r in rows_data],
         any(r["revenue_yoy"] is not None for r in rows_data)),
        ("EBITDA Margin", [_pct(r["ebitda_margin"]) for r in rows_data],
         any(r["ebitda_margin"] is not None for r in rows_data)),
        ("Rule of 40 (Growth + EBITDA Margin)", [_pct(r["rule40_ebitda"]) for r in rows_data],
         any(r["rule40_ebitda"] is not None for r in rows_data)),
        ("FCF Margin", [_pct(r["fcf_margin"]) for r in rows_data],
         any(r["fcf_margin"] is not None for r in rows_data)),
        ("Rule of 40 (Growth + FCF Margin)", [_pct(r["rule40_fcf"]) for r in rows_data],
         any(r["rule40_fcf"] is not None for r in rows_data)),
        ("Net Profit Margin", [_pct(r["net_margin"]) for r in rows_data],
         any(r["net_margin"] is not None for r in rows_data)),
        ("Rule of 40 (Growth + Net Margin)", [_pct(r["rule40_net"]) for r in rows_data],
         any(r["rule40_net"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _blend_color(c1: HexColor, c2: HexColor, t: float) -> Color:
    """Linear-interpolate between two colors; t=0 -> c1, t=1 -> c2."""
    t = max(0.0, min(1.0, t))
    return Color(
        c1.red + (c2.red - c1.red) * t,
        c1.green + (c2.green - c1.green) * t,
        c1.blue + (c2.blue - c1.blue) * t,
    )


def _score_hex(t: float) -> str:
    """t=1 -> highest-contrast green (best-ranked / cheapest relative
    value), t=0 -> palest green (worst-ranked / most expensive relative
    value). Returns a plain #RRGGBB string for use inside Paragraph XML
    markup (never HexColor.hexval() — see CLAUDE.md gotcha, it returns
    '0xRRGGBB' not '#RRGGBB')."""
    pale = HexColor('#DCEEE1')
    c = _blend_color(pale, GREEN, t)
    r = int(round(c.red * 255))
    g = int(round(c.green * 255))
    b = int(round(c.blue * 255))
    return f"#{r:02X}{g:02X}{b:02X}"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _ranks_from_scores(scores: list[int]) -> list[int]:
    """Standard competition ranking (ties share a rank; the next distinct
    rank skips accordingly) — rank 1 = highest score = best overall
    (cheapest valuation multiples / strongest growth-profitability mix)."""
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    ranks = [0] * len(scores)
    prev_score = None
    prev_rank = 0
    for pos, i in enumerate(order, start=1):
        if scores[i] != prev_score:
            prev_rank = pos
            prev_score = scores[i]
        ranks[i] = prev_rank
    return ranks


def _peer_comparison_table(subject: CompanyData, peers: dict, styles: dict) -> Table | None:
    """Subject + up to 5 peers as columns, 8 most-recent-data metrics as
    rows (Sales YoY Growth, Sales 3Y CAGR, P/E, PEG, EV/Gross Profit,
    EV/Sales, Rule of 40 with EBITDA Margin, Rule of 40 with Net Margin),
    plus a bottom Valuation Rank row: for each metric, companies are
    ranked and awarded points (cheaper multiples / stronger growth score
    higher), points are summed per company into a composite score, and the
    resulting overall rank (1st, 2nd, 3rd, …) is shown large and bold in a
    green font whose contrast scales with relative standing — the most
    saturated green marks the best-ranked (cheapest relative to growth)
    company, palest marks the worst. All ranking/scoring is computed in
    Python (models.growth_pricing.compute_peer_scores), never by the LLM."""
    rows_data = compute_peer_comparison_table(subject, peers)
    if not rows_data:
        return None

    candidates = [
        ("sales_yoy_growth", "Sales YoY Growth", _pct),
        ("sales_cagr3", "Sales 3Y CAGR", _pct),
        ("pe_ratio", "P/E", _x),
        ("peg_ratio", "PEG Ratio", _num),
        ("ev_gross_profit", "EV / Gross Profit", _x),
        ("ev_sales", "EV / Sales", _x),
        ("rule40_ebitda", "Rule of 40 (+ EBITDA Margin)", _pct),
        ("rule40_net", "Rule of 40 (+ Net Margin)", _pct),
    ]
    metric_rows = [
        (label, [fmt(r[key]) for r in rows_data])
        for key, label, fmt in candidates
        if any(r[key] is not None for r in rows_data)
    ]
    if not metric_rows:
        return None

    columns = [f"{r['ticker']}*" if r["is_subject"] else r["ticker"] for r in rows_data]

    header = [Paragraph("Metric", styles["table_header"])] + \
             [Paragraph(c, styles["table_header"]) for c in columns]
    table_rows = [header]
    for label, values in metric_rows:
        table_rows.append([Paragraph(label, styles["table_label"])] +
                           [Paragraph(v, styles["table_cell"]) for v in values])

    scores = compute_peer_scores(rows_data)
    ranks = _ranks_from_scores(scores)
    max_score, min_score = max(scores), min(scores)
    spread = max_score - min_score
    rank_style = ParagraphStyle(
        "rank_cell", fontName=BOLD_FONT, fontSize=11, leading=13,
        alignment=styles["table_cell"].alignment,
    )
    score_cells = [Paragraph("<b>Valuation Rank</b>", styles["table_label"])]
    for score, rank in zip(scores, ranks):
        # t=1 -> highest score (best rank) -> highest-contrast green; t=0 -> palest
        t = (score - min_score) / spread if spread > 0 else 1.0
        score_cells.append(Paragraph(
            f'<font color="{_score_hex(t)}">{_ordinal(rank)}</font>', rank_style))
    table_rows.append(score_cells)

    n = len(columns)
    first_w = CW * 0.28
    other_w = (CW - first_w) / max(n, 1)
    col_widths = [first_w] + [other_w] * n

    t = Table(table_rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), LBLUE),
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, NAVY),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, BORDER),
        ('LINEABOVE', (0, -1), (-1, -1), 0.8, NAVY),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return t


def _snapshot_table(company: CompanyData, styles: dict) -> Table | None:
    """Current/forward-looking snapshot — Metric/Value layout (not
    years-as-columns), since Forward P/E and standard PEG Ratio only exist
    for the current moment (next-FY consensus), not as a historical series."""
    snap = compute_current_snapshot(company)
    mcap_ccy = snap.get("market_cap_currency") or ""
    ev_ccy = snap.get("enterprise_value_currency") or ""
    mcap_label = f"Market Capitalization (current, {mcap_ccy})" if mcap_ccy else "Market Capitalization (current)"
    ev_label = f"Enterprise Value (current, {ev_ccy})" if ev_ccy else "Enterprise Value (current)"
    candidates = [
        (mcap_label, _fmt_b(snap["market_cap"])),
        (ev_label, _fmt_b(snap["enterprise_value"])),
        ("P/E (TTM)", _x(snap["pe_ratio_ttm"])),
        ("Forward P/E", _x(snap["forward_pe"])),
        ("PEG Ratio", _num(snap["peg_ratio"])),
        ("EV / EBITDA (TTM)", _x(snap["ev_ebitda_ttm"])),
        ("EV / Sales (TTM)", _x(snap["ev_sales_ttm"])),
        ("FCF Yield (TTM)", _pct(snap["fcf_yield_ttm"])),
    ]
    rows_data = [(label, value) for label, value in candidates if value != "n/a"]
    if not rows_data:
        return None

    header = [Paragraph("Metric", styles["table_header"]),
              Paragraph("Current / Forward", styles["table_header"])]
    rows = [header]
    for label, value in rows_data:
        rows.append([Paragraph(label, styles["table_label"]),
                     Paragraph(value, styles["table_cell"])])

    col_widths = [CW * 0.6, CW * 0.4]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(_table_style())
    return t


def _verdict_banner(subject: CompanyData, styles: dict) -> list:
    """Prominent rules-based Valuation Verdict banner — Attractive (green) /
    Moderate (amber) / Expensive (red) — computed deterministically in
    Python by models.growth_pricing.compute_valuation_verdict from the
    subject's own Sales 3Y CAGR, EV/Gross Profit, and Rule of 40
    (+Net Margin). Rendered first in the report, ahead of every other
    section, per design requirement."""
    v = compute_valuation_verdict(subject)
    verdict = v["verdict"]
    color_map = {"Attractive": GREEN, "Moderate": AMBER, "Expensive": RED}
    color = color_map.get(verdict, AMBER)

    title_style = ParagraphStyle(
        "verdict_title", fontName=BOLD_FONT, fontSize=13,
        textColor=HexColor('#FFFFFF'), alignment=TA_CENTER, leading=16,
    )
    detail_style = ParagraphStyle(
        "verdict_detail", fontName=BASE_FONT, fontSize=7.8,
        textColor=HexColor('#FFFFFF'), alignment=TA_CENTER, leading=10,
    )

    cell = [
        Paragraph(f"Valuation Verdict: {verdict.upper()}", title_style),
        Paragraph(
            f"Sales 3Y CAGR {_pct(v['sales_cagr3'])}  ·  "
            f"EV / Gross Profit {_x(v['ev_gross_profit'])}  ·  "
            f"Rule of 40 (+ Net Margin) {_pct(v['rule40_net'])}",
            detail_style,
        ),
    ]
    t = Table([[cell]], colWidths=[CW])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), color),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return [t, Spacer(1, 4*mm)]


class GrowthPricingPDFGenerator:
    """Renders the Growth Pricing & Peers PDF: a rules-based Valuation
    Verdict banner, current/forward snapshot, then three dimensions
    (Revenue & Gross Profit, Earnings, Cash Flow), each a verified table
    followed by the LLM's grounded narrative, a verified Rule of 40 table,
    a verified Peer Comparison table with a composite Valuation Score row
    (subject + up to 5 peers), and a closing 'What's Priced In?' synthesis.
    All scoring/verdict logic is computed in Python, never by the LLM."""

    def render(self, subject: CompanyData, analysis: dict, output_path: str,
               peers: dict | None = None) -> None:
        peers = peers or {}
        report_date = datetime.utcnow().strftime("%Y-%m-%d")
        styles = _styles()

        def _page_header(canvas, doc):
            _draw_header(canvas, doc, subject, report_date)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=f"{subject.name or subject.ticker} — Growth Pricing & Peers",
            author="Your Humble EquityBot",
            subject="Growth Pricing & Peers",
        )

        story = []
        story += _verdict_banner(subject, styles)
        story += self._intro(subject, styles)
        story.append(Spacer(1, 4*mm))

        snap_table = _snapshot_table(subject, styles)
        if snap_table:
            story.append(Paragraph("Current / Forward-Looking Snapshot", styles["table_label"]))
            story.append(Spacer(1, 1*mm))
            story.append(snap_table)
            story.append(Spacer(1, 5*mm))

        story += self._dimension_block(
            "Revenue & Gross Profit",
            "How has the market priced growth and gross profitability over time?",
            _revenue_gp_table(subject, styles),
            "Revenue & Gross Profit (verified, computed from reported financials)",
            analysis.get("revenue_gross_profit_narrative"),
            styles,
        )
        story.append(PageBreak())

        story += self._dimension_block(
            "Earnings",
            "How has the market priced this company's earnings power, including periods of unprofitability?",
            _earnings_table(subject, styles),
            "Earnings (verified, computed from reported financials)",
            analysis.get("earnings_narrative"),
            styles,
        )
        story.append(PageBreak())

        story += self._dimension_block(
            "Cash Flow",
            "How has the market priced this company's cash generation?",
            _cash_flow_table(subject, styles),
            "Cash Flow (verified, computed from reported financials)",
            analysis.get("cash_flow_narrative"),
            styles,
        )

        r40_table = _rule_of_40_table(subject, styles)
        if r40_table:
            story.append(PageBreak())
            story.append(Paragraph("Rule of 40 — Growth vs. Profitability Balance", styles["dim_title"]))
            story.append(Paragraph(
                "Is management striking a sensible balance between growth and profitability? "
                "A young company can create value either by growing quickly while sacrificing "
                "profits, or by growing more slowly while already highly profitable — the Rule "
                "of 40 treats either strategy as acceptable as long as the combined result is "
                "strong: Revenue Growth Rate + Profit Margin ≥ 40%. The table below applies "
                "this against three commonly used profitability measures for high-growth / "
                "software companies: EBITDA Margin (the most common choice), Free Cash Flow "
                "Margin, and Net Profit Margin.",
                styles["body"],
            ))
            story.append(Paragraph(
                "Rule of 40 (verified, computed from reported financials)", styles["table_label"]))
            story.append(Spacer(1, 1*mm))
            story.append(r40_table)
            story.append(Spacer(1, 3*mm))

        peer_table = _peer_comparison_table(subject, peers, styles)
        if peer_table:
            story.append(PageBreak())
            story.append(Paragraph("Peer Comparison", styles["dim_title"]))
            story.append(Paragraph(
                "How does this pricing compare to peers of similar scale and business model? "
                "The table below places the subject company (marked *) alongside up to five "
                "peers — selected by the user and/or suggested by the LLM — across the same "
                "most-recent-data metrics: Sales YoY Growth, 3-year Sales CAGR, P/E, PEG Ratio, "
                "EV/Gross Profit, EV/Sales, and the Rule of 40 using both EBITDA Margin and Net "
                "Margin. The bottom Valuation Rank row ranks every company across all metrics "
                "(cheaper multiples and stronger growth/profitability earn more points) and sums "
                "the result into a composite score, then shows each company's overall placing "
                "(1st, 2nd, 3rd, …) in bold green — the highest-contrast green marks the "
                "best-ranked, most attractively valued company.",
                styles["body"],
            ))
            story.append(Paragraph(
                "Peer Comparison — most recent data (verified, computed from reported financials)",
                styles["table_label"]))
            story.append(Spacer(1, 1*mm))
            story.append(peer_table)
            story.append(Spacer(1, 3*mm))

        story.append(Spacer(1, 4*mm))
        story.append(Paragraph("What's Priced In?", styles["synthesis_heading"]))
        story.append(Paragraph(
            analysis.get("priced_in_synthesis") or "Synthesis not available.",
            styles["narrative_body"],
        ))

        story.append(Spacer(1, 6*mm))
        disclaimer = (
            f"Analysis generated by {LLM_MODEL} ({LLM_PROVIDER}). "
            f"Data source: EODHD, with SEC EDGAR XBRL facts used to extend history for US "
            f"tickers and yfinance used as a fallback where EODHD data is unavailable. All "
            f"ratios, tables, the peer Valuation Rank, and the Valuation Verdict banner in this "
            f"report are computed in Python from verified financial data; the LLM's role is "
            f"limited to narrative discussion and, when the user supplies fewer than 5 peers, "
            f"suggesting additional peer tickers."
        )
        story.append(Paragraph(
            f"<i>{disclaimer}</i>",
            ParagraphStyle("gp_disc", fontName=OBLIQUE_FONT, fontSize=7,
                           textColor=HexColor("#888888"), leading=9),
        ))

        doc.build(story, onFirstPage=_page_header, onLaterPages=_page_header)
        logger.info(f"[PDF GrowthPricing] Saved: {output_path}")

    def _intro(self, subject: CompanyData, styles: dict) -> list:
        flow = [section_title("Growth Pricing & Peers", styles)]
        flow.append(Paragraph(
            f"This report evaluates the relative pricing of {subject.name or subject.ticker} as a "
            f"high-growth company, including any periods of unprofitability along the way. "
            f"Given the quality of this business, what expectations are already embedded in "
            f"today's stock price? Historical valuation tables below cover up to 10 years (or "
            f"since IPO, if shorter), followed by a peer comparison across the same current "
            f"pricing metrics.",
            styles["body"],
        ))
        return flow

    def _dimension_block(self, title: str, question: str, table: Table | None,
                          table_label: str, narrative: str | None, styles: dict) -> list:
        flow = [Paragraph(title, styles["dim_title"]),
                Paragraph(question, styles["dim_question"])]
        if table:
            flow.append(Paragraph(table_label, styles["table_label"]))
            flow.append(Spacer(1, 1*mm))
            flow.append(table)
            flow.append(Spacer(1, 3*mm))
        flow.append(Paragraph(narrative or "Narrative not available.", styles["narrative_body"]))
        return flow
