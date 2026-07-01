"""
pdf_fisher.py — ReportLab PDF renderer for the Fisher Alternatives model.

Produces a 3-page PDF:
  Page 1: Header + Business Overview + Fisher 15-Point Scorecard (pts 1-8)
  Page 2: Fisher Points 9-15 + Score Summary + Helmer 7 Powers
  Page 3: Moat Assessment + Key Risks + Investment Conclusion + Recommendation

Visual language: same palette and typography as pdf_overview.py for consistency.
"""

from __future__ import annotations
import logging
import re
from datetime import datetime
from typing import Optional

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
CW      = W - ML - MR          # ≈ 481 pts

# ── Colour palette (shared with pdf_overview) ─────────────────────────────────
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
MGRAY   = HexColor('#555555')
LGRAY   = HexColor('#F0F0F0')
BORDER  = HexColor('#BBCCDD')
GOLD    = HexColor('#C9A84C')

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
            leading=13, alignment=TA_JUSTIFY, spaceAfter=8,
        ),
        "body_small": S("body_small",
            fontName=BASE_FONT, fontSize=7.8, textColor=MGRAY,
            leading=11, alignment=TA_JUSTIFY, spaceAfter=6,
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
        "badge_pass": S("bp",
            fontName=BOLD_FONT, fontSize=7, textColor=GREEN,
            alignment=TA_CENTER, leading=9,
        ),
        "badge_partial": S("bpa",
            fontName=BOLD_FONT, fontSize=7, textColor=AMBER,
            alignment=TA_CENTER, leading=9,
        ),
        "badge_fail": S("bf",
            fontName=BOLD_FONT, fontSize=7, textColor=RED,
            alignment=TA_CENTER, leading=9,
        ),
        "power_strong": S("pws",
            fontName=BOLD_FONT, fontSize=7.5, textColor=GREEN,
            alignment=TA_CENTER, leading=10,
        ),
        "power_moderate": S("pwm",
            fontName=BOLD_FONT, fontSize=7.5, textColor=BLUE,
            alignment=TA_CENTER, leading=10,
        ),
        "power_weak": S("pww",
            fontName=BOLD_FONT, fontSize=7.5, textColor=AMBER,
            alignment=TA_CENTER, leading=10,
        ),
        "power_none": S("pwn",
            fontName=BASE_FONT, fontSize=7.5, textColor=MGRAY,
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
        "score_big": S("score_big",
            fontName=BOLD_FONT, fontSize=28, textColor=NAVY,
            alignment=TA_CENTER, leading=32,
        ),
        "score_label": S("score_label",
            fontName=BASE_FONT, fontSize=7.5, textColor=MGRAY,
            alignment=TA_CENTER, leading=10,
        ),
        "risk_bullet": S("risk_bullet",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor('#222222'),
            leading=13, leftIndent=10, spaceAfter=8,
        ),
    }


# ── Page header (canvas callback) ────────────────────────────────────────────

def _draw_header(canvas, doc, company: CompanyData, report_date: str):
    """Ink-saving header: white background, navy (Pantone 303) text."""
    canvas.saveState()

    # Company name — navy on white
    canvas.setFillColor(NAVY)
    canvas.setFont(BOLD_FONT, 13)
    canvas.drawString(ML, H - 10*mm, company.name or company.ticker)

    # Subtitle
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, H - 15.5*mm,
        f"Fisher Alternatives  |  {company.sector or ''}  |  {company.exchange or ''}")

    # Report date tag
    canvas.setFont(BASE_FONT, 7.5)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, H - 20.5*mm, f"Report date: {report_date}")

    # Right: price + ticker
    price_str = (f"{company.current_price:.2f} {company.currency_price or ''}"
                 if company.current_price else "Price n/a")
    cap_str   = (f"Mkt Cap {company.market_cap/1000:,.1f}B {company.currency or ''}"
                 if company.market_cap else "")
    canvas.setFont(BOLD_FONT, 9)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(W - MR, H - 10*mm, price_str)
    canvas.setFont(BASE_FONT, 7.5)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(W - MR, H - 15.5*mm, cap_str)
    canvas.drawRightString(W - MR, H - 20.5*mm, company.ticker)

    # Page number
    canvas.setFont(BASE_FONT, 7)
    canvas.setFillColor(MGRAY)
    canvas.drawCentredString(W / 2, MB / 2, f"Page {doc.page}")

    canvas.restoreState()


# ── Score bar (visual progress bar for Fisher total) ──────────────────────────

class ScoreBar(Flowable):
    """A coloured horizontal bar representing the Fisher score out of 75."""

    def __init__(self, score: int, max_score: int = 75, width: float = CW, height: float = 12):
        super().__init__()
        self.score     = score
        self.max_score = max_score
        self.bar_width = width
        self.bar_height= height
        self.width     = width
        self.height    = height + 4

    def draw(self):
        fraction = max(0.0, min(1.0, self.score / self.max_score))
        # Background track
        self.canv.setFillColor(LGRAY)
        self.canv.rect(0, 2, self.bar_width, self.bar_height, fill=1, stroke=0)
        # Filled portion — colour by score
        if fraction >= 0.87:  colour = GREEN
        elif fraction >= 0.73: colour = BLUE
        elif fraction >= 0.60: colour = AMBER
        else:                  colour = RED
        fill_w = self.bar_width * fraction
        self.canv.setFillColor(colour)
        self.canv.rect(0, 2, fill_w, self.bar_height, fill=1, stroke=0)
        # Score label in bar
        self.canv.setFillColor(white)
        self.canv.setFont(BOLD_FONT, 8)
        label = f"{self.score} / {self.max_score}"
        self.canv.drawCentredString(fill_w / 2, 4, label)


# ── Fisher points table ───────────────────────────────────────────────────────

def _fisher_table(points: list[dict], styles: dict, start: int, end: int) -> Table:
    """
    Build a table for Fisher points[start:end] (1-indexed, inclusive).
    Columns: # | Criterion | Score | Assessment | Rationale
    """
    header = [
        Paragraph("#",           styles["table_header"]),
        Paragraph("Criterion",   styles["table_header"]),
        Paragraph("Score",       styles["table_header"]),
        Paragraph("Assessment",  styles["table_header"]),
        Paragraph("Rationale",   styles["table_header"]),
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
        ('ALIGN',        (0,1), (2,-1),  'CENTER'),
        ('BACKGROUND',   (0,1), (0,-1),  LGRAY),
        ('FONTNAME',     (0,1), (0,-1),  BOLD_FONT),
    ]

    subset = [p for p in points if start <= p.get("number", 0) <= end]

    for row_idx, pt in enumerate(subset, start=1):
        assessment = pt.get("assessment", "PARTIAL").upper()
        score_val  = pt.get("score", 3)

        # Assessment badge style
        if assessment == "PASS":
            badge_style = styles["badge_pass"]
            badge_bg    = LGREEN
        elif assessment == "FAIL":
            badge_style = styles["badge_fail"]
            badge_bg    = LRED
        else:
            badge_style = styles["badge_partial"]
            badge_bg    = LAMBER

        # Score colour
        score_colour = (GREEN if score_val >= 4 else RED if score_val <= 2 else AMBER)
        score_style  = ParagraphStyle("sc", fontName=BOLD_FONT, fontSize=9,
                                       textColor=score_colour, alignment=TA_CENTER, leading=11)

        row = [
            Paragraph(str(pt.get("number", row_idx)), styles["table_cell_c"]),
            Paragraph(f"<b>{pt.get('title','')}</b>", styles["table_label"]),
            Paragraph(str(score_val), score_style),
            Paragraph(assessment, badge_style),
            Paragraph(pt.get("rationale", ""), styles["table_cell"]),
        ]
        rows.append(row)

        # Alternate row background + assessment cell colour
        bg = LLBLUE if row_idx % 2 == 0 else white
        style_cmds.append(('BACKGROUND', (0, row_idx), (-1, row_idx), bg))
        style_cmds.append(('BACKGROUND', (3, row_idx), (3, row_idx), badge_bg))

    # Column widths: # | Criterion | Score | Assessment | Rationale
    col_widths = [22, 100, 32, 55, CW - 22 - 100 - 32 - 55]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(style_cmds))
    return t


# ── Helmer 7 Powers table ─────────────────────────────────────────────────────

def _powers_table(powers: list[dict], styles: dict) -> Table:
    header = [
        Paragraph("Power",      styles["table_header"]),
        Paragraph("Strength",   styles["table_header"]),
        Paragraph("Assessment", styles["table_header"]),
    ]

    strength_colours = {
        "Strong":   (GREEN,  LGREEN),
        "Moderate": (BLUE,   LBLUE),
        "Weak":     (AMBER,  LAMBER),
        "None":     (MGRAY,  LGRAY),
    }

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
        ('ALIGN',        (1,1), (1,-1),  'CENTER'),
    ]

    for row_idx, pw in enumerate(powers, start=1):
        strength = pw.get("strength", "Weak")
        fg_col, bg_col = strength_colours.get(strength, (MGRAY, LGRAY))

        strength_style = ParagraphStyle(
            "ps", fontName=BOLD_FONT, fontSize=7.5,
            textColor=fg_col, alignment=TA_CENTER, leading=10,
        )

        row = [
            Paragraph(f"<b>{pw.get('name','')}</b>", styles["table_label"]),
            Paragraph(strength, strength_style),
            Paragraph(pw.get("rationale", ""), styles["table_cell"]),
        ]
        rows.append(row)

        alt_bg = LLBLUE if row_idx % 2 == 0 else white
        style_cmds.append(('BACKGROUND', (0, row_idx), (-1, row_idx), alt_bg))
        style_cmds.append(('BACKGROUND', (1, row_idx), (1, row_idx), bg_col))

    col_widths = [110, 65, CW - 110 - 65]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(style_cmds))
    return t


# ── Fisher score summary box ──────────────────────────────────────────────────

def _score_summary_table(analysis: dict, styles: dict) -> Table:
    """
    Compact summary: Score / Grade / Moat Width / Active Powers side by side.
    """
    total   = analysis.get("fisher_total_score", 0)
    grade   = analysis.get("fisher_grade", "?")
    moat    = analysis.get("moat_width", "?")
    n_pow   = analysis.get("active_powers_count", 0)

    grade_colour = (GREEN if grade == "A" else BLUE if grade == "B" else
                    AMBER if grade == "C" else RED)
    moat_colour  = (GREEN if moat == "Wide" else AMBER if moat == "Narrow" else RED)

    def metric_cell(value, label, colour=NAVY):
        val_style = ParagraphStyle("vs", fontName=BOLD_FONT, fontSize=20,
                                    textColor=colour, alignment=TA_CENTER, leading=24)
        lbl_style = ParagraphStyle("ls", fontName=BASE_FONT, fontSize=7,
                                    textColor=MGRAY, alignment=TA_CENTER, leading=9)
        inner = Table(
            [[Paragraph(str(value), val_style)],
             [Paragraph(label,      lbl_style)]],
            colWidths=[CW/4 - 8],
        )
        inner.setStyle(TableStyle([
            ('TOPPADDING',   (0,0),(-1,-1), 6),
            ('BOTTOMPADDING',(0,0),(-1,-1), 6),
            ('ALIGN',        (0,0),(-1,-1), 'CENTER'),
        ]))
        return inner

    cells = [
        metric_cell(f"{total}/75", "Fisher Score",   NAVY),
        metric_cell(grade,         "Grade",          grade_colour),
        metric_cell(moat,          "Moat Width",     moat_colour),
        metric_cell(f"{n_pow}/7",  "Active Powers",  BLUE),
    ]

    t = Table([cells], colWidths=[CW/4]*4)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), LLBLUE),
        ('GRID',       (0,0), (-1,-1), 0.5, BORDER),
        ('ALIGN',      (0,0), (-1,-1), 'CENTER'),
        ('VALIGN',     (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING',(0,0),(-1,-1),0),
        ('LEFTPADDING', (0,0),(-1,-1), 0),
        ('RIGHTPADDING',(0,0),(-1,-1), 0),
    ]))
    return t


# ── Recommendation box ────────────────────────────────────────────────────────

def _recommendation_box(rec: str, rationale: str, styles: dict) -> Table:
    colour = GREEN if rec == "BUY" else RED if rec == "SELL" else AMBER
    label  = {"BUY": "BUY — Long-Term Owner",
               "SELL": "SELL — Exit Position",
               "HOLD": "HOLD — Monitor Closely"}.get(rec, rec)

    title_p     = Paragraph(label,     styles["rec_title"])
    rationale_p = Paragraph(rationale, styles["rec_body"])

    t = Table([[title_p], [rationale_p]], colWidths=[CW])
    t.setStyle(TableStyle([
        ('BACKGROUND',   (0,0), (-1,-1), colour),
        ('TOPPADDING',   (0,0), (-1,-1), 8),
        ('BOTTOMPADDING',(0,0), (-1,-1), 8),
        ('LEFTPADDING',  (0,0), (-1,-1), 12),
        ('RIGHTPADDING', (0,0), (-1,-1), 12),
    ]))
    return t


# ── Section heading helper ────────────────────────────────────────────────────

def _section(title: str, styles: dict) -> list:
    return [
        Paragraph(title, styles["section_title"]),
        HRFlowable(width=CW, thickness=1, color=BLUE, spaceAfter=4),
    ]


def _split_paragraphs(text: str, styles: dict, style_key: str = "body",
                       sentences_per_para: int = 4) -> list:
    """Split LLM text into readable paragraph flowables.

    Strategy (in order):
    1. Double-newline split — respects LLM paragraph structure.
    2. Single-newline split — when LLM uses \\n but not \\n\\n.
    3. Sentence-grouping fallback — groups every `sentences_per_para`
       sentences when the entire response is one wall of text.
    """
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) <= 1 and len(text) > 250:
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    if len(paras) <= 1 and len(text) > 400:
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        paras = [
            " ".join(sentences[i:i + sentences_per_para])
            for i in range(0, len(sentences), sentences_per_para)
            if sentences[i:i + sentences_per_para]
        ]
    return [Paragraph(p, styles[style_key]) for p in paras if p]


# ── Main generator ────────────────────────────────────────────────────────────

class FisherPDFGenerator:
    """
    Renders the Fisher Alternatives 3-page PDF.
    """

    def render(
        self,
        company: CompanyData,
        analysis: dict,
        output_path: str,
        adv_result=None,  # Optional[AdversarialResult]
    ) -> None:
        report_date = datetime.now().strftime("%Y-%m-%d")
        st = _styles()

        def on_page(canvas, doc):
            _draw_header(canvas, doc, company, report_date)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"{company.name or company.ticker} — Fisher Alternatives",
            author="Your Humble EquityBot",
        )

        story = []
        fisher_pts = analysis.get("fisher_points", [])

        # ── PAGE 1: Overview + Fisher Points 1-8 ─────────────────────────────
        story += _section("Business Overview", st)
        story += _split_paragraphs(analysis.get("business_overview", ""), st)
        story.append(Spacer(1, 6))

        story += _section("Philip Fisher 15-Point Analysis  (Points 1–8)", st)
        story.append(_fisher_table(fisher_pts, st, 1, 8))

        story.append(PageBreak())

        # ── PAGE 2: Fisher Points 9-15 + Score Summary + 7 Powers ────────────
        story += _section("Philip Fisher 15-Point Analysis  (Points 9–15)", st)
        story.append(_fisher_table(fisher_pts, st, 9, 15))
        story.append(Spacer(1, 8))

        # Score bar
        story += _section("Fisher Score Summary", st)
        story.append(_score_summary_table(analysis, st))
        story.append(Spacer(1, 4))

        # Score bar visual
        total = analysis.get("fisher_total_score", 0)
        story.append(ScoreBar(total))
        story.append(Spacer(1, 4))

        # Fisher narrative summary
        story += _split_paragraphs(analysis.get("fisher_summary", ""), st, "body_small")
        story.append(Spacer(1, 8))

        story += _section("Helmer 7 Powers Analysis", st)
        story.append(_powers_table(analysis.get("powers", []), st))

        story.append(PageBreak())

        # ── PAGE 3: Moat + Risks + Conclusion + Recommendation ───────────────
        story += _section("Economic Moat Assessment", st)
        story += _split_paragraphs(analysis.get("moat_rationale", ""), st)
        story.append(Spacer(1, 6))

        story += _section("Key Risks", st)
        for risk in analysis.get("key_risks", []):
            if risk and risk.strip():
                story.append(Paragraph(f"•  {risk}", st["risk_bullet"]))
        story.append(Spacer(1, 6))

        story += _section("Investment Conclusion", st)
        story += _split_paragraphs(analysis.get("conclusion", ""), st)
        story.append(Spacer(1, 8))

        # Recommendation box
        story.append(KeepTogether([
            _recommendation_box(
                analysis.get("recommendation", "HOLD"),
                analysis.get("recommendation_rationale", ""),
                st,
            )
        ]))

        story.append(Spacer(1, 8))

        # Data sources footnote
        sources = ", ".join(company.data_sources) if company.data_sources else "yfinance"
        story.append(Paragraph(
            f"<i>Data sources: {sources}  |  "
            f"Framework: Philip Fisher (1958) + Hamilton Helmer (2016)  |  "
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
        logger.info(f"[PDF Fisher] Saved: {output_path}")
