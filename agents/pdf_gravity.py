"""
pdf_gravity.py — ReportLab PDF renderer for the Gravity Taxers model.

Produces a 3-page PDF:
  Page 1: Header + Gravity Profile + Revenue Model Summary Card + Dimensions 1-5
  Page 2: Dimensions 6-10 + Gravity Score Summary + Canonical Comparison + Flywheel
  Page 3: Key Risks + Investment Conclusion + Recommendation

Visual style: consistent with pdf_overview.py and pdf_fisher.py.
"""

from __future__ import annotations
import logging
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak, KeepTogether,
)
from reportlab.platypus.flowables import Flowable

from config import LLM_PROVIDER, LLM_MODEL
from data_sources.base import CompanyData

logger = logging.getLogger(__name__)

# ── Dimensions ────────────────────────────────────────────────────────────────
W, H    = A4
ML = MR = 18 * mm
MT      = 38 * mm
MB      = 14 * mm
CW      = W - ML - MR

# ── Colours ───────────────────────────────────────────────────────────────────
NAVY    = HexColor('#003F54')   # Pantone 303 — ink-saving brand colour
BLUE    = HexColor('#2E75B6')
LBLUE   = HexColor('#D6E8F7')
LLBLUE  = HexColor('#FFFFFF')   # alt row fill — kept white to save ink
GREEN   = HexColor('#1A7E3D')
LGREEN  = HexColor('#D4EDDA')
RED     = HexColor('#C0392B')
LRED    = HexColor('#FADBD8')
AMBER   = HexColor('#D68910')
LAMBER  = HexColor('#FDEBD0')
TEAL    = HexColor('#117A8B')
LTEAL   = HexColor('#D1ECF1')
MGRAY   = HexColor('#555555')
LGRAY   = HexColor('#F0F0F0')
BORDER  = HexColor('#BBCCDD')

BASE_FONT   = 'Helvetica'
BOLD_FONT   = 'Helvetica-Bold'
ITALIC_FONT = 'Helvetica-Oblique'


# ── Styles ────────────────────────────────────────────────────────────────────

def _styles() -> dict:
    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    return {
        "section_title": S("section_title",
            fontName=BOLD_FONT, fontSize=9, textColor=NAVY,
            spaceBefore=8, spaceAfter=3, leading=11,
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
            alignment=TA_LEFT, leading=10,
        ),
        "table_cell_c": S("tcc",
            fontName=BASE_FONT, fontSize=7.5, textColor=HexColor('#111111'),
            alignment=TA_CENTER, leading=10,
        ),
        "rec_title": S("rec_title",
            fontName=BOLD_FONT, fontSize=11, textColor=white,
            alignment=TA_CENTER, leading=14,
        ),
        "rec_body": S("rec_body",
            fontName=BASE_FONT, fontSize=8, textColor=white,
            alignment=TA_JUSTIFY, leading=12,
        ),
        "card_value": S("card_value",
            fontName=BOLD_FONT, fontSize=14, textColor=NAVY,
            alignment=TA_CENTER, leading=17,
        ),
        "card_label": S("card_label",
            fontName=BASE_FONT, fontSize=7, textColor=MGRAY,
            alignment=TA_CENTER, leading=9,
        ),
        "risk_bullet": S("risk_bullet",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor('#222222'),
            leading=13, leftIndent=10, spaceAfter=3,
        ),
    }


# ── Page header ───────────────────────────────────────────────────────────────

def _draw_header(canvas, doc, company: CompanyData, report_date: str):
    """Ink-saving header: white background, navy (Pantone 303) text."""
    canvas.saveState()

    NAME_Y     = H - 11*mm
    SUBTITLE_Y = H - 17*mm
    LINE_Y     = H - 21*mm

    # Row 1: company name (left) | price (right)
    canvas.setFont(BOLD_FONT, 14)
    canvas.setFillColor(NAVY)
    canvas.drawString(ML, NAME_Y, company.name or company.ticker)

    cur = company.currency_price or company.currency or ""
    price_str = (f"Price: {company.current_price:,.2f} {cur}"
                 if company.current_price else "Price n/a")
    canvas.setFont(BOLD_FONT, 8.5)
    canvas.drawRightString(W - MR, NAME_Y, price_str)

    # Row 2: subtitle (left) | mcap (right)
    subtitle = " | ".join(filter(None, [
        "Gravity Score",
        company.sector, company.country,
        company.exchange, company.ticker, report_date,
    ]))
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, SUBTITLE_Y, subtitle)

    mcap_str = (f"MCap: {company.market_cap/1e9:,.2f}B {company.currency or ''}"
                if company.market_cap else "")
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(W - MR, SUBTITLE_Y, mcap_str)

    # Separator line
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.8)
    canvas.line(ML, LINE_Y, W - MR, LINE_Y)

    # Footer
    canvas.setFont(BASE_FONT, 7)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(W - MR, 8*mm,
                           f"Page {doc.page}  |  Your Humble EquityBot  |  {LLM_MODEL}")

    canvas.restoreState()


# ── Revenue model summary card ────────────────────────────────────────────────

def _revenue_model_card(rm: dict, styles: dict) -> Table:
    """
    A horizontal 4-cell info card showing key revenue model attributes.
    """
    recurring  = rm.get("recurring_pct_estimate", "?")
    visibility = rm.get("revenue_visibility", "?")
    pricing    = rm.get("pricing_power", "?")
    capex      = rm.get("capex_intensity", "?")

    def colour_for(val, positive_vals, negative_vals):
        if val in positive_vals: return GREEN
        if val in negative_vals: return RED
        return AMBER

    vis_col  = colour_for(visibility, ["High"], ["Low"])
    pp_col   = colour_for(pricing,    ["Strong"], ["Weak"])
    cx_col   = colour_for(capex,      ["Asset-Light"], ["Capital-Heavy"])
    rec_col  = GREEN if isinstance(recurring, int) and recurring >= 70 else (
               AMBER if isinstance(recurring, int) and recurring >= 40 else RED)

    def metric(value, label, colour):
        val_p = ParagraphStyle("cv", fontName=BOLD_FONT, fontSize=13,
                               textColor=colour, alignment=TA_CENTER, leading=16)
        lbl_p = ParagraphStyle("cl", fontName=BASE_FONT, fontSize=7,
                               textColor=MGRAY, alignment=TA_CENTER, leading=9)
        inner = Table(
            [[Paragraph(str(value), val_p)],
             [Paragraph(label,      lbl_p)]],
            colWidths=[CW/4 - 6],
        )
        inner.setStyle(TableStyle([
            ('TOPPADDING',    (0,0),(-1,-1), 7),
            ('BOTTOMPADDING', (0,0),(-1,-1), 7),
            ('ALIGN',         (0,0),(-1,-1), 'CENTER'),
        ]))
        return inner

    cells = [
        metric(f"~{recurring}%", "Recurring Revenue",  rec_col),
        metric(visibility,       "Revenue Visibility",  vis_col),
        metric(pricing,          "Pricing Power",       pp_col),
        metric(capex,            "CapEx Intensity",     cx_col),
    ]

    t = Table([cells], colWidths=[CW/4]*4)
    t.setStyle(TableStyle([
        ('BACKGROUND',  (0,0),(-1,-1), LLBLUE),
        ('GRID',        (0,0),(-1,-1), 0.5, BORDER),
        ('ALIGN',       (0,0),(-1,-1), 'CENTER'),
        ('VALIGN',      (0,0),(-1,-1), 'MIDDLE'),
        ('TOPPADDING',  (0,0),(-1,-1), 0),
        ('BOTTOMPADDING',(0,0),(-1,-1),0),
        ('LEFTPADDING', (0,0),(-1,-1), 0),
        ('RIGHTPADDING',(0,0),(-1,-1), 0),
    ]))
    return t


# ── Gravity dimensions table ──────────────────────────────────────────────────

def _gravity_table(dims: list[dict], styles: dict, start: int, end: int) -> Table:
    header = [
        Paragraph("#",          styles["table_header"]),
        Paragraph("Dimension",  styles["table_header"]),
        Paragraph("Score",      styles["table_header"]),
        Paragraph("Assessment", styles["table_header"]),
    ]

    rows = [header]
    style_cmds = [
        # Ink-saving header: white bg, navy text, thick navy underline
        ('BACKGROUND',   (0,0), (-1,0), white),
        ('TEXTCOLOR',    (0,0), (-1,0), NAVY),
        ('FONTNAME',     (0,0), (-1,0), BOLD_FONT),
        ('FONTSIZE',     (0,0), (-1,0), 7.5),
        ('ALIGN',        (0,0), (-1,0), 'CENTER'),
        ('VALIGN',       (0,0), (-1,-1),'MIDDLE'),
        ('GRID',         (0,0), (-1,-1), 0.3, BORDER),
        ('LINEBELOW',    (0,0), (-1,0),  1.4, NAVY),
        ('TOPPADDING',   (0,0), (-1,-1), 4),
        ('BOTTOMPADDING',(0,0), (-1,-1), 4),
        ('LEFTPADDING',  (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('ALIGN',        (0,1), (2,-1), 'CENTER'),
        # Label column: bold but no background fill (saves ink)
        ('FONTNAME',     (0,1), (0,-1), BOLD_FONT),
    ]

    subset = [d for d in dims if start <= d.get("number", 0) <= end]

    for row_idx, dim in enumerate(subset, start=1):
        score = int(dim.get("score", 3) or 3)
        score_colour = GREEN if score >= 4 else RED if score <= 2 else AMBER

        # Score dot indicator
        score_style = ParagraphStyle("ss", fontName=BOLD_FONT, fontSize=10,
                                      textColor=score_colour, alignment=TA_CENTER, leading=12)

        # Stars out of 5
        stars = "★" * score + "☆" * (5 - score)
        stars_style = ParagraphStyle("st", fontName=BASE_FONT, fontSize=7.5,
                                      textColor=score_colour, alignment=TA_CENTER, leading=10)

        row = [
            Paragraph(str(dim.get("number", row_idx)), styles["table_cell_c"]),
            Paragraph(f"<b>{dim.get('title','')}</b><br/>"
                      f"<font size='7' color='#555555'>{dim.get('rationale','')}</font>",
                      styles["table_label"]),
            Paragraph(str(score), score_style),
            Paragraph(stars, stars_style),
        ]
        rows.append(row)

        bg = LLBLUE if row_idx % 2 == 0 else white
        style_cmds.append(('BACKGROUND', (0, row_idx), (-1, row_idx), bg))

    # Column widths: # | Dimension+Rationale | Score | Stars
    col_widths = [22, CW - 22 - 30 - 70, 30, 70]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(style_cmds))
    return t


# ── Gravity score summary ─────────────────────────────────────────────────────

class GravityBar(Flowable):
    """Coloured horizontal score bar for the Gravity total."""

    def __init__(self, score: int, max_score: int = 50, width: float = CW, height: float = 12):
        super().__init__()
        self.score      = score
        self.max_score  = max_score
        self.bar_width  = width
        self.bar_height = height
        self.width      = width
        self.height     = height + 4

    def draw(self):
        fraction = max(0.0, min(1.0, self.score / self.max_score))
        self.canv.setFillColor(LGRAY)
        self.canv.rect(0, 2, self.bar_width, self.bar_height, fill=1, stroke=0)
        colour = (GREEN if fraction >= 0.90 else TEAL if fraction >= 0.76 else
                  AMBER if fraction >= 0.60 else RED)
        self.canv.setFillColor(colour)
        fill_w = self.bar_width * fraction
        self.canv.rect(0, 2, fill_w, self.bar_height, fill=1, stroke=0)
        self.canv.setFillColor(white)
        self.canv.setFont(BOLD_FONT, 8)
        self.canv.drawCentredString(fill_w / 2, 4, f"{self.score} / {self.max_score}")


def _score_summary(analysis: dict, styles: dict) -> Table:
    total = analysis.get("total_gravity_score", 0)
    grade = analysis.get("gravity_grade", "?")
    rm    = analysis.get("revenue_model", {})
    pp    = rm.get("pricing_power", "?")
    rec   = rm.get("recurring_pct_estimate", "?")

    grade_col = (GREEN if grade == "A" else TEAL if grade == "B" else
                 AMBER if grade == "C" else RED)

    def cell(value, label, colour=NAVY):
        vp = ParagraphStyle("v", fontName=BOLD_FONT, fontSize=18,
                             textColor=colour, alignment=TA_CENTER, leading=22)
        lp = ParagraphStyle("l", fontName=BASE_FONT, fontSize=7,
                             textColor=MGRAY, alignment=TA_CENTER, leading=9)
        inner = Table([[Paragraph(str(value), vp)], [Paragraph(label, lp)]],
                      colWidths=[CW/4 - 8])
        inner.setStyle(TableStyle([
            ('TOPPADDING',   (0,0),(-1,-1), 6),
            ('BOTTOMPADDING',(0,0),(-1,-1), 6),
            ('ALIGN',        (0,0),(-1,-1), 'CENTER'),
        ]))
        return inner

    pp_col  = GREEN if pp == "Strong" else AMBER if pp == "Moderate" else RED
    rec_col = GREEN if isinstance(rec, int) and rec >= 70 else (
              AMBER if isinstance(rec, int) and rec >= 40 else AMBER)

    cells = [
        cell(f"{total}/50",  "Gravity Score",    NAVY),
        cell(grade,          "Grade",            grade_col),
        cell(pp,             "Pricing Power",    pp_col),
        cell(f"~{rec}%",     "Recurring Rev.",   rec_col),
    ]

    t = Table([cells], colWidths=[CW/4]*4)
    t.setStyle(TableStyle([
        ('BACKGROUND',   (0,0),(-1,-1), LLBLUE),
        ('GRID',         (0,0),(-1,-1), 0.5, BORDER),
        ('ALIGN',        (0,0),(-1,-1), 'CENTER'),
        ('VALIGN',       (0,0),(-1,-1), 'MIDDLE'),
        ('TOPPADDING',   (0,0),(-1,-1), 0),
        ('BOTTOMPADDING',(0,0),(-1,-1), 0),
        ('LEFTPADDING',  (0,0),(-1,-1), 0),
        ('RIGHTPADDING', (0,0),(-1,-1), 0),
    ]))
    return t


# ── Recommendation box ────────────────────────────────────────────────────────

def _recommendation_box(rec: str, rationale: str, styles: dict) -> Table:
    colour = GREEN if rec == "BUY" else RED if rec == "SELL" else AMBER
    label  = {"BUY": "BUY — Toll Booth Worth Owning",
               "SELL": "SELL — Exit Position",
               "HOLD": "HOLD — Monitor the Choke Point"}.get(rec, rec)
    t = Table(
        [[Paragraph(label, styles["rec_title"])],
         [Paragraph(rationale, styles["rec_body"])]],
        colWidths=[CW],
    )
    t.setStyle(TableStyle([
        ('BACKGROUND',   (0,0),(-1,-1), colour),
        ('TOPPADDING',   (0,0),(-1,-1), 8),
        ('BOTTOMPADDING',(0,0),(-1,-1), 8),
        ('LEFTPADDING',  (0,0),(-1,-1), 12),
        ('RIGHTPADDING', (0,0),(-1,-1), 12),
    ]))
    return t


# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(title: str, styles: dict) -> list:
    return [
        Paragraph(title, styles["section_title"]),
        HRFlowable(width=CW, thickness=1, color=BLUE, spaceAfter=4),
    ]


def _paras(text: str, styles: dict, key: str = "body") -> list:
    if not text: return []
    return [Paragraph(p.strip(), styles[key])
            for p in text.split("\n\n") if p.strip()]


# ── Main generator ────────────────────────────────────────────────────────────

class GravityPDFGenerator:

    def render(self, company: CompanyData, analysis: dict, output_path: str, adv_result=None) -> None:
        report_date = datetime.now().strftime("%Y-%m-%d")
        st = _styles()

        def on_page(canvas, doc):
            _draw_header(canvas, doc, company, report_date)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"{company.name or company.ticker} — Gravity Taxers",
            author="Your Humble EquityBot",
        )

        dims = analysis.get("gravity_dimensions", [])
        story = []

        # ── PAGE 1: Profile + Revenue Model Card + Dimensions 1-5 ────────────
        story += _section("Gravity Profile", st)
        story += _paras(analysis.get("gravity_profile", ""), st)
        story.append(Spacer(1, 6))

        story += _section("Revenue Model", st)
        story.append(_revenue_model_card(analysis.get("revenue_model", {}), st))

        # Pricing evidence sub-note
        ev = analysis.get("revenue_model", {}).get("pricing_evidence", "")
        if ev:
            story.append(Spacer(1, 4))
            story.append(Paragraph(ev, st["body_small"]))
        story.append(Spacer(1, 6))

        story += _section("Gravity Dimensions  (1–5)", st)
        story.append(_gravity_table(dims, st, 1, 5))

        story.append(PageBreak())

        # ── PAGE 2: Dimensions 6-10 + Score + Canonical + Flywheel ───────────
        story += _section("Gravity Dimensions  (6–10)", st)
        story.append(_gravity_table(dims, st, 6, 10))
        story.append(Spacer(1, 8))

        story += _section("Gravity Score Summary", st)
        story.append(_score_summary(analysis, st))
        story.append(Spacer(1, 4))
        story.append(GravityBar(analysis.get("total_gravity_score", 0)))
        story.append(Spacer(1, 4))
        story += _paras(analysis.get("gravity_summary", ""), st, "body_small")
        story.append(Spacer(1, 8))

        story += _section("Canonical Comparison", st)
        story += _paras(analysis.get("canonical_comparison", ""), st)
        story.append(Spacer(1, 6))

        story += _section("Revenue Flywheel", st)
        story += _paras(analysis.get("revenue_flywheel", ""), st)

        story.append(PageBreak())

        # ── PAGE 3: Risks + Conclusion + Recommendation ───────────────────────
        story += _section("Key Risks", st)
        for risk in analysis.get("key_risks", []):
            if risk and risk.strip():
                story.append(Paragraph(f"•  {risk}", st["risk_bullet"]))
        story.append(Spacer(1, 6))

        story += _section("Investment Conclusion", st)
        story += _paras(analysis.get("conclusion", ""), st)
        story.append(Spacer(1, 8))

        story.append(KeepTogether([
            _recommendation_box(
                analysis.get("recommendation", "HOLD"),
                analysis.get("recommendation_rationale", ""),
                st,
            )
        ]))

        story.append(Spacer(1, 8))
        sources = ", ".join(company.data_sources) if company.data_sources else "yfinance"
        story.append(Paragraph(
            f"<i>Data sources: {sources}  |  "
            f"Framework: Gravity Taxers — Choke-Point Business Analysis  |  "
            f"Generated: {report_date}</i>",
            ParagraphStyle("fn", fontName=ITALIC_FONT, fontSize=6.5,
                           textColor=MGRAY, alignment=TA_CENTER, leading=9),
        ))

        # ── PAGE 4 (optional): Adversarial Review ────────────────────────────
        if adv_result is not None:
            from agents.pdf_adversarial import build_adversarial_page
            story.append(PageBreak())
            story += build_adversarial_page(adv_result)

        story.append(Spacer(1, 6*mm))
        story.append(Paragraph(
            f"<i>Analysis generated by {LLM_MODEL} ({LLM_PROVIDER}).</i>",
            ParagraphStyle("llm_disc", fontName="Helvetica-Oblique", fontSize=7,
                           textColor=HexColor("#888888"), leading=9),
        ))
        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        logger.info(f"[PDF Gravity] Saved: {output_path}")
