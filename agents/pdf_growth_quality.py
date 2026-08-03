"""
pdf_growth_quality.py — Growth Quality Score (GQS) PDF renderer.

Same header/footer structure, colour palette, and typography as Investment
Memo V2 / Earnings Quality Score (agents/pdf_earnings_quality.py) — copied
verbatim per design requirement, with only the subtitle literal changed.

Phase 1 (Build the Evidence) only: renders, per capability, a verified
precomputed table (Python-derived, e.g. Revenue/YoY/CAGR), the LLM-built
customers/momentum tables, the discussion Q&A, and the "why it matters"
paragraph. No scoring is rendered yet — that arrives in a later phase.

Rendering is capability-agnostic: it iterates models.growth_quality's
GQ_CAPABILITY_META/GQ_CAPABILITY_ORDER, so adding capabilities 2-8 later
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
    compute_revenue_table,
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

def _revenue_table(company: CompanyData, styles: dict) -> Table:
    rows_data = compute_revenue_table(company)
    header = [Paragraph(h, styles["table_header"]) for h in
              ["Fiscal Year", "Revenue", "YoY Growth", "3Y CAGR", "5Y CAGR",
               "Organic Revenue Growth"]]
    rows = [header]
    for r in rows_data:
        rows.append([
            Paragraph(str(r["year"]), styles["table_cell"]),
            Paragraph(_fmt_b(r["revenue"]), styles["table_cell"]),
            Paragraph(_pct(r["yoy"]), styles["table_cell"]),
            Paragraph(_pct(r["cagr3"]), styles["table_cell"]),
            Paragraph(_pct(r["cagr5"]), styles["table_cell"]),
            Paragraph("Data unavailable", styles["table_cell"]),
        ])
    if len(rows) == 1:
        rows.append([Paragraph("Data unavailable", styles["table_cell"])] * 6)

    t = Table(rows, colWidths=[CW*0.13, CW*0.16, CW*0.16, CW*0.13, CW*0.13, CW*0.29],
               repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), LBLUE),
        ('LINEBELOW', (0,0), (-1,0), 0.8, NAVY),
        ('LINEBELOW', (0,1), (-1,-1), 0.4, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    return t


# ── Generic LLM-built table renderer (customers_table / momentum_table) ─

def _render_generic_table(table: dict, styles: dict) -> Table | None:
    cols = table.get("columns") or []
    rows_data = table.get("rows") or []
    if not cols:
        return None

    header = [Paragraph(c, styles["table_header"]) for c in cols]
    rows = [header]
    for r in rows_data:
        rows.append([Paragraph(str(cell), styles["table_cell"]) for cell in r])
    if len(rows) == 1:
        rows.append([Paragraph("Data unavailable", styles["table_cell"])] +
                    [Paragraph("", styles["table_cell"])] * (len(cols) - 1))

    n = len(cols)
    first_w = CW * 0.16
    other_w = (CW - first_w) / max(n - 1, 1)
    col_widths = [first_w] + [other_w] * (n - 1)

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), LBLUE),
        ('LINEBELOW', (0,0), (-1,0), 0.8, NAVY),
        ('LINEBELOW', (0,1), (-1,-1), 0.4, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    return t


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

        if cap_id == "demand_strength":
            flow.append(Paragraph("Revenue (verified, computed from reported financials)",
                                   styles["table_label"]))
            flow.append(Spacer(1, 1*mm))
            flow.append(_revenue_table(subject, styles))
            flow.append(Spacer(1, 3*mm))

            cust_t = _render_generic_table(cap_data.get("customers_table") or {}, styles)
            if cust_t:
                flow.append(Paragraph("Customers", styles["table_label"]))
                flow.append(Spacer(1, 1*mm))
                flow.append(cust_t)
                flow.append(Spacer(1, 3*mm))

            mom_t = _render_generic_table(cap_data.get("momentum_table") or {}, styles)
            if mom_t:
                flow.append(Paragraph("Commercial Momentum", styles["table_label"]))
                flow.append(Spacer(1, 1*mm))
                flow.append(mom_t)
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
