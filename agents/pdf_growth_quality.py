"""
pdf_growth_quality.py — Growth Quality Score (GQS) PDF renderer.

Same header/footer structure, colour palette, and typography as Investment
Memo V2 / Earnings Quality Score (agents/pdf_earnings_quality.py) — copied
verbatim per design requirement, with only the subtitle literal changed.

Phase 1 (Build the Evidence) only: renders, per capability, a verified
precomputed table (Python-derived, e.g. Revenue/YoY/CAGR, or Gross/EBITDA/
Operating Margin), the LLM-built tables declared in that capability's own
GQ_CAPABILITY_META["tables"] (e.g. Customers/Momentum, or Unit Economics),
the discussion Q&A, and the "why it matters" paragraph. No scoring is
rendered yet — that arrives in a later phase.

Rendering is capability-agnostic: it iterates models.growth_quality's
GQ_CAPABILITY_META/GQ_CAPABILITY_ORDER, so adding capabilities 2-6 later
requires no changes here as long as the analysis dict carries matching
keys under "capabilities".
"""

from __future__ import annotations
import logging
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, PageBreak, KeepTogether,
)

from config import LLM_PROVIDER, LLM_MODEL
from data_sources.base import CompanyData
from models.growth_quality import (
    GQ_CAPABILITY_META, GQ_CAPABILITY_ORDER, GQ_CAPABILITIES_TOTAL,
    compute_revenue_table, compute_profitability_table,
    compute_operating_leverage_table, compute_capital_allocation_table,
    compute_competitive_position_table, compute_governance_snapshot,
)

logger = logging.getLogger(__name__)

# ── Dimensions ───────────────────────────────────────────────────────────
W, H    = A4
ML = MR = 18*mm
MT      = 28*mm
MB      = 14*mm
CW      = W - ML - MR

# ── Colour palette (identical to Investment Memo V2 / Earnings Quality) ──
NAVY    = HexColor('#003F54')
BLUE    = HexColor('#003F54')
LBLUE   = HexColor('#D6E8F7')
GREEN   = HexColor('#1A7E3D')
RED     = HexColor('#C0392B')
AMBER   = HexColor('#D68910')
MGRAY   = HexColor('#555555')
LGRAY   = HexColor('#F0F0F0')
BORDER  = HexColor('#BBCCDD')
GOLD    = HexColor('#C9A84C')

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
        "cap_title": S("cap_title",
            fontName=BOLD_FONT, fontSize=11, textColor=NAVY,
            spaceBefore=4, spaceAfter=2, leading=14,
        ),
        "cap_question": S("cap_question",
            fontName=OBLIQUE_FONT, fontSize=9, textColor=MGRAY,
            spaceAfter=6, leading=12,
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
        "q_label": S("q_label",
            fontName=BOLD_FONT, fontSize=8.3, textColor=NAVY,
            leading=11, spaceBefore=4, spaceAfter=1,
        ),
        "a_body": S("a_body",
            fontName=BASE_FONT, fontSize=8.3, textColor=HexColor('#222222'),
            leading=12, alignment=TA_JUSTIFY, spaceAfter=4,
        ),
        "why_body": S("why_body",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor('#222222'),
            leading=13, alignment=TA_JUSTIFY,
        ),
    }


# ── Page header (drawn on canvas, verbatim from pdf_earnings_quality.py
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
        "Growth Quality Score",
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


# ── Verified revenue table (Python-computed, ground truth) ──────────────

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
                        styles: dict, label_frac: float = 0.20) -> Table:
    """Shared builder: metrics as rows (left label column), fiscal years as
    column headers across the top — used by both the verified Revenue table
    and the LLM-built Customers/Commercial Momentum tables so every table in
    the report shares one visual layout."""
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


def _revenue_table(company: CompanyData, styles: dict) -> Table | None:
    """
    Verified Revenue / YoY / CAGR table, computed in Python from real
    reported financials (never LLM output). Metrics that have no data at
    all across the whole history (e.g. 3Y/5Y CAGR when fewer than 3-5 years
    of history exist) are dropped entirely rather than shown as a row of
    "n/a" — and if there is no revenue history at all, the whole table (and
    its section) is skipped.
    """
    rows_data = compute_revenue_table(company)
    if not rows_data:
        return None

    candidates = [
        ("Revenue", [_fmt_b(r["revenue"]) for r in rows_data],
         any(r["revenue"] is not None for r in rows_data)),
        ("YoY Growth", [_pct(r["yoy"]) for r in rows_data],
         any(r["yoy"] is not None for r in rows_data)),
        ("3Y CAGR", [_pct(r["cagr3"]) for r in rows_data],
         any(r["cagr3"] is not None for r in rows_data)),
        ("5Y CAGR", [_pct(r["cagr5"]) for r in rows_data],
         any(r["cagr5"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _profitability_table(company: CompanyData, styles: dict) -> Table | None:
    """
    Verified Gross/EBITDA/Operating Margin + Gross Profit Growth table,
    computed in Python from real reported financials (never LLM output) —
    feeds Capability 2 (Economic Engine) the same way _revenue_table() feeds
    Capability 1 (Demand Strength).
    """
    rows_data = compute_profitability_table(company)
    if not rows_data:
        return None

    candidates = [
        ("Gross Margin", [_pct(r["gross_margin"]) for r in rows_data],
         any(r["gross_margin"] is not None for r in rows_data)),
        ("EBITDA Margin", [_pct(r["ebitda_margin"]) for r in rows_data],
         any(r["ebitda_margin"] is not None for r in rows_data)),
        ("Operating Margin", [_pct(r["operating_margin"]) for r in rows_data],
         any(r["operating_margin"] is not None for r in rows_data)),
        ("Gross Profit Growth", [_pct(r["gross_profit_growth"]) for r in rows_data],
         any(r["gross_profit_growth"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _operating_leverage_table(company: CompanyData, styles: dict) -> Table | None:
    """
    Verified SG&A % / R&D % / Operating Expenses % + the three incremental-
    margin metrics, computed in Python from real reported financials (never
    LLM output) — feeds Capability 3 (Operating Leverage) the same way
    _profitability_table() feeds Capability 2 (Economic Engine).
    """
    rows_data = compute_operating_leverage_table(company)
    if not rows_data:
        return None

    candidates = [
        ("SG&A %", [_pct(r["sga_pct"]) for r in rows_data],
         any(r["sga_pct"] is not None for r in rows_data)),
        ("R&D %", [_pct(r["rd_pct"]) for r in rows_data],
         any(r["rd_pct"] is not None for r in rows_data)),
        ("Operating Expenses %", [_pct(r["opex_pct"]) for r in rows_data],
         any(r["opex_pct"] is not None for r in rows_data)),
        ("Incremental Operating Margin", [_pct(r["incremental_operating_margin"]) for r in rows_data],
         any(r["incremental_operating_margin"] is not None for r in rows_data)),
        ("Incremental EBITDA Margin", [_pct(r["incremental_ebitda_margin"]) for r in rows_data],
         any(r["incremental_ebitda_margin"] is not None for r in rows_data)),
        ("Incremental FCF Margin", [_pct(r["incremental_fcf_margin"]) for r in rows_data],
         any(r["incremental_fcf_margin"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _capital_allocation_table(company: CompanyData, styles: dict) -> Table | None:
    """
    Verified R&D Spending / CapEx / Share Count / Dilution / ROIC / FCF
    Conversion / Cash Balance table, computed in Python from real reported
    financials (never LLM output) — feeds Capability 4 (Capital Allocation)
    the same way _operating_leverage_table() feeds Capability 3 (Operating
    Leverage).
    """
    rows_data = compute_capital_allocation_table(company)
    if not rows_data:
        return None

    candidates = [
        ("R&D Spending", [_fmt_b(r["rd"]) for r in rows_data],
         any(r["rd"] is not None for r in rows_data)),
        ("CapEx", [_fmt_b(r["capex"]) for r in rows_data],
         any(r["capex"] is not None for r in rows_data)),
        ("Share Count (M)",
         [f"{r['shares_outstanding']:.1f}" if r["shares_outstanding"] is not None else "n/a"
          for r in rows_data],
         any(r["shares_outstanding"] is not None for r in rows_data)),
        ("Dilution (YoY)", [_pct(r["dilution"]) for r in rows_data],
         any(r["dilution"] is not None for r in rows_data)),
        ("ROIC", [_pct(r["roic"]) for r in rows_data],
         any(r["roic"] is not None for r in rows_data)),
        ("FCF Conversion", [_pct(r["fcf_conversion"]) for r in rows_data],
         any(r["fcf_conversion"] is not None for r in rows_data)),
        ("Cash Balance", [_fmt_b(r["cash"]) for r in rows_data],
         any(r["cash"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _competitive_position_table(company: CompanyData, styles: dict) -> Table | None:
    """
    Verified Gross Margin Stability (YoY Δ + 3Y rolling stdev) table,
    computed in Python from real reported financials (never LLM output) —
    feeds Capability 5 (Competitive Position) the same way
    _capital_allocation_table() feeds Capability 4 (Capital Allocation).
    """
    rows_data = compute_competitive_position_table(company)
    if not rows_data:
        return None

    candidates = [
        ("Gross Margin Δ YoY", [_pct(r["gross_margin_delta_yoy"]) for r in rows_data],
         any(r["gross_margin_delta_yoy"] is not None for r in rows_data)),
        ("Gross Margin 3Y Stdev", [_pct(r["gross_margin_3y_stdev"]) for r in rows_data],
         any(r["gross_margin_3y_stdev"] is not None for r in rows_data)),
    ]
    metric_rows = [(label, values) for label, values, has_data in candidates if has_data]
    if not metric_rows:
        return None

    years = [str(r["year"]) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles)


def _governance_snapshot_table(company: CompanyData, styles: dict) -> Table | None:
    """
    Verified Ownership & Governance snapshot — Metric/Value layout (NOT the
    years-as-columns layout used by every table above), since EODHD/yfinance
    only ever expose a CURRENT snapshot of insider/institutional ownership,
    never a historical time series — feeds Capability 6 (Management &
    Governance Quality). Named officers are rendered separately as a
    paragraph (see _officers_paragraph below), not as a table row, since
    name/title strings don't fit this metric-grid layout well.
    """
    gov = compute_governance_snapshot(company)
    rows_data = []
    if "pct_insiders" in gov:
        rows_data.append(("Insider Ownership %", _pct(gov["pct_insiders"])))
    if "pct_institutions" in gov:
        rows_data.append(("Institutional Ownership %", _pct(gov["pct_institutions"])))
    if not rows_data:
        return None

    header = [Paragraph("Metric", styles["table_header"]),
              Paragraph("Current", styles["table_header"])]
    rows = [header]
    for label, value in rows_data:
        rows.append([Paragraph(label, styles["table_label"]),
                     Paragraph(value, styles["table_cell"])])

    col_widths = [CW * 0.55, CW * 0.45]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(_table_style())
    return t


def _officers_paragraph(company: CompanyData, styles: dict) -> Paragraph | None:
    """Named executive officers, rendered as a short paragraph below the
    Ownership & Governance snapshot table (see _governance_snapshot_table)."""
    gov = compute_governance_snapshot(company)
    officers = gov.get("officers")
    if not officers:
        return None
    names = "; ".join(
        f"{o.get('name', 'n/a')} ({o.get('title', 'n/a')})" for o in officers[:8]
    )
    return Paragraph(f"<b>Named Officers (verified):</b> {names}", styles["body_small"])


# ── Generic LLM-built table renderer (customers_table / momentum_table / …) ─

def _render_generic_table(table: dict, styles: dict) -> Table | None:
    """
    Renders an LLM-built {"years": [...], "rows": [{"metric","values"}]}
    table. Returns None (skip entirely — no section is rendered) when there
    are no rows, i.e. the company discloses none of that table's candidate
    metrics — avoids wasting a page on an all-"Data unavailable" grid.
    """
    years = table.get("years") or []
    rows_data = table.get("rows") or []
    if not years or not rows_data:
        return None

    metric_rows = [(r.get("metric") or "", r.get("values") or []) for r in rows_data]
    return _metric_rows_table(years, metric_rows, styles, label_frac=0.24)


def _table_has_unverified(table: dict) -> bool:
    """True if any cell in an LLM-built table carries the "~" unverified/
    recalled-from-general-knowledge marker (see models/growth_quality.py's
    tilde-prefix rule) — used to decide whether to render the legend."""
    for row in (table.get("rows") or []):
        for v in (row.get("values") or []):
            if isinstance(v, str) and v.strip().startswith("~"):
                return True
    return False


def _unverified_legend(styles: dict) -> Paragraph:
    return Paragraph(
        "~ figures are recalled from general knowledge, not sourced from the "
        "verified financial data or annual-report excerpt used in this report.",
        ParagraphStyle("gq_legend", fontName=OBLIQUE_FONT, fontSize=7,
                       textColor=HexColor("#888888"), leading=9),
    )


def _render_discussion(discussion: list[dict], styles: dict) -> list:
    flow = []
    for item in discussion:
        q = item.get("question") or ""
        a = item.get("answer") or "Data unavailable."
        flow.append(Paragraph(q, styles["q_label"]))
        flow.append(Paragraph(a, styles["a_body"]))
    return flow


class GrowthQualityPDFGenerator:
    """Renders the Growth Quality Score PDF (Phase 1 — Build the Evidence)."""

    def render(self, subject: CompanyData, analysis: dict, output_path: str) -> None:
        report_date = datetime.utcnow().strftime("%Y-%m-%d")
        styles = _styles()

        def _page_header(canvas, doc):
            _draw_header(canvas, doc, subject, report_date)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=f"{subject.name or subject.ticker} — Growth Quality Score",
            author="Your Humble EquityBot",
            subject="Growth Quality Score",
        )

        story = []
        story += self._intro(subject, analysis, styles)
        story.append(Spacer(1, 4*mm))

        capabilities = analysis.get("capabilities") or {}
        for i, cap_id in enumerate(GQ_CAPABILITY_ORDER):
            cap_data = capabilities.get(cap_id) or {}
            story += self._capability_block(subject, cap_id, cap_data, styles)
            if i < len(GQ_CAPABILITY_ORDER) - 1:
                story.append(PageBreak())

        story.append(Spacer(1, 6*mm))
        story.append(Paragraph(
            f"<i>Analysis generated by {LLM_MODEL} ({LLM_PROVIDER}). "
            f"Data source: EODHD, with yfinance used as a fallback where EODHD "
            f"data is unavailable. Phase 1 — Build the Evidence: no scores or "
            f"grades are assigned in this phase.</i>",
            ParagraphStyle("llm_disc", fontName=OBLIQUE_FONT, fontSize=7,
                           textColor=HexColor("#888888"), leading=9),
        ))

        doc.build(story, onFirstPage=_page_header, onLaterPages=_page_header)
        logger.info(f"[PDF GrowthQuality] Saved: {output_path}")

    # ── Intro block ──────────────────────────────────────────────────────
    def _intro(self, subject: CompanyData, analysis: dict, styles: dict) -> list:
        flow = [section_title("Growth Quality Score — Phase 1: Building the Evidence", styles)]
        completed = analysis.get("capabilities_completed", len(GQ_CAPABILITY_META))
        total = analysis.get("capabilities_total", GQ_CAPABILITIES_TOTAL)
        flow.append(Paragraph(
            f"This report evaluates whether {subject.name or subject.ticker} exhibits the "
            f"structural traits of a durable long-term compounder — not whether it is cheap "
            f"today. It is built incrementally: this version covers {completed} of "
            f"{total} planned capabilities. No scores or grades are assigned in this phase; "
            f"the objective here is solely to build and discuss the historical evidence.",
            styles["body"],
        ))
        return flow

    # ── One capability's full block (table(s) + discussion + why it matters) ─
    def _capability_block(self, subject: CompanyData, cap_id: str, cap_data: dict, styles: dict) -> list:
        meta = GQ_CAPABILITY_META[cap_id]
        flow = []
        flow.append(Paragraph(f"Capability {meta['number']}: {meta['title']}", styles["cap_title"]))
        flow.append(Paragraph(meta["question"], styles["cap_question"]))

        # Verified, Python-computed table (never LLM output) — one per
        # capability, keyed by cap_id since each capability's ground-truth
        # metrics come from a different deterministic calculation.
        verified_table, verified_label = None, None
        if cap_id == "demand_strength":
            verified_table = _revenue_table(subject, styles)
            verified_label = "Revenue (verified, computed from reported financials)"
        elif cap_id == "economic_engine":
            verified_table = _profitability_table(subject, styles)
            verified_label = "Profitability (verified, computed from reported financials)"
        elif cap_id == "operating_leverage":
            verified_table = _operating_leverage_table(subject, styles)
            verified_label = "Operating Leverage (verified, computed from reported financials)"
        elif cap_id == "capital_allocation":
            verified_table = _capital_allocation_table(subject, styles)
            verified_label = "Capital Allocation (verified, computed from reported financials)"
        elif cap_id == "competitive_position":
            verified_table = _competitive_position_table(subject, styles)
            verified_label = "Competitive Position — Gross Margin Stability (verified, computed from reported financials)"
        elif cap_id == "management_governance":
            verified_table = _governance_snapshot_table(subject, styles)
            verified_label = "Ownership & Governance — Verified Snapshot (current, from EODHD/yfinance)"
        if verified_table:
            flow.append(Paragraph(verified_label, styles["table_label"]))
            flow.append(Spacer(1, 1*mm))
            flow.append(verified_table)
            if cap_id == "management_governance":
                officers_para = _officers_paragraph(subject, styles)
                if officers_para:
                    flow.append(Spacer(1, 1*mm))
                    flow.append(officers_para)
            flow.append(Spacer(1, 3*mm))

        # LLM-built tables — generic over whatever table names this
        # capability declares in GQ_CAPABILITY_META["tables"].
        for table_name in meta.get("tables", []):
            table = cap_data.get(table_name) or {}
            t = _render_generic_table(table, styles)
            if t:
                label = meta.get("table_labels", {}).get(table_name, table_name)
                flow.append(Paragraph(label, styles["table_label"]))
                flow.append(Spacer(1, 1*mm))
                flow.append(t)
                if _table_has_unverified(table):
                    flow.append(_unverified_legend(styles))
                flow.append(Spacer(1, 3*mm))

        discussion = cap_data.get("discussion") or []
        if discussion:
            flow.append(Paragraph("Discussion", styles["table_label"]))
            flow += _render_discussion(discussion, styles)
            flow.append(Spacer(1, 2*mm))

        why = cap_data.get("why_it_matters")
        if why:
            flow.append(Paragraph("Why This Matters", styles["table_label"]))
            flow.append(Paragraph(why, styles["why_body"]))

        return flow
