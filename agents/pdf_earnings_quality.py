"""
pdf_earnings_quality.py — Earnings Quality Score PDF renderer.

Same header/footer structure, colour palette, and typography as
Investment Memo V2 (agents/pdf_overview_v2.py) — copied verbatim per
design requirement, with only the subtitle literal changed.

Pages:
  Page 1: Universe ranking table + subject grade box
  Page 2: Subject detail — subscores, explanation, strengths/risks/red flags
  Page 3+: Peer detail blocks (flow naturally across pages)
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
from models.earnings_quality import EQ_WEIGHTS, EQ_DIMENSION_LABELS

logger = logging.getLogger(__name__)

# ── Dimensions ───────────────────────────────────────────────────────────
W, H    = A4
ML = MR = 18*mm
MT      = 28*mm
MB      = 14*mm
CW      = W - ML - MR

# ── Colour palette (identical to Investment Memo V2) ────────────────────
NAVY    = HexColor('#003F54')
BLUE    = HexColor('#003F54')
LBLUE   = HexColor('#D6E8F7')
LLBLUE  = HexColor('#FFFFFF')
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


def _styles(company_name: str = "") -> dict:
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
        "bullet": S("bullet",
            fontName=BASE_FONT, fontSize=8, textColor=HexColor('#222222'),
            leading=11, leftIndent=10, spaceAfter=3, alignment=TA_JUSTIFY,
        ),
        "bullet_red": S("bullet_red",
            fontName=BASE_FONT, fontSize=8, textColor=RED,
            leading=11, leftIndent=10, spaceAfter=3, alignment=TA_JUSTIFY,
        ),
        "peer_name": S("peer_name",
            fontName=BOLD_FONT, fontSize=9.5, textColor=NAVY, leading=12,
        ),
    }


# ── Page header (drawn on canvas, verbatim from pdf_overview_v2.py except
#    the subtitle literal) ───────────────────────────────────────────────

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
        "Earnings Quality Score",
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


def _grade_colour_hex(grade: str) -> str:
    if not grade or grade in ("N/A",):
        return MGRAY_HEX
    letter = grade[0]
    if letter in ("A",):
        return GREEN_HEX
    if letter in ("B", "C"):
        return AMBER_HEX
    return RED_HEX


def _grade_box(entry: dict, styles: dict) -> Table:
    """Subject headline box — same visual language as the Investment Memo
    recommendation box (white body, thick coloured border)."""
    grade = entry.get("letter_grade") or "N/A"
    score = entry.get("earnings_quality_score")
    pct = entry.get("percentile")
    conf = entry.get("confidence") or "n/a"
    colour_hex = _grade_colour_hex(grade)
    colour = HexColor(colour_hex)

    score_str = f"{score}/100" if score is not None else "n/a"
    pct_str = f"{pct}th percentile" if pct is not None else "percentile n/a"

    head = Paragraph(
        f'<font color="{colour_hex}">EARNINGS QUALITY GRADE: <b>{grade}</b>'
        f'  &nbsp;&nbsp;|&nbsp;&nbsp;  Score: <b>{score_str}</b></font>',
        ParagraphStyle("eqh", fontName=BOLD_FONT, fontSize=12,
                       alignment=TA_CENTER, leading=15)
    )
    sub = Paragraph(
        f"{pct_str} within analyzed universe &nbsp;|&nbsp; Confidence: {conf}",
        ParagraphStyle("eqs", fontName=BASE_FONT, fontSize=8,
                       textColor=HexColor("#333333"),
                       alignment=TA_CENTER, leading=12)
    )

    t = Table([[head], [sub]], colWidths=[CW])
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


def _bullets(items: list[str], styles: dict, style_key="bullet") -> list:
    if not items:
        return [Paragraph("<i>None noted.</i>", styles["body_small"])]
    return [Paragraph(f"&bull;&nbsp; {i}", styles[style_key]) for i in items]


def _subscores_table(subscores: dict, styles: dict) -> Table:
    keys = list(EQ_WEIGHTS.keys())
    header = [Paragraph("Dimension", styles["table_header"]),
              Paragraph("Weight", styles["table_header"]),
              Paragraph("Score", styles["table_header"])]
    rows = [header]
    for k in keys:
        v = subscores.get(k)
        rows.append([
            Paragraph(EQ_DIMENSION_LABELS.get(k, k), styles["table_label"]),
            Paragraph(f"{EQ_WEIGHTS[k]*100:.0f}%", styles["table_cell"]),
            Paragraph(f"{v}/100" if v is not None else "n/a", styles["table_cell"]),
        ])
    t = Table(rows, colWidths=[CW*0.55, CW*0.2, CW*0.25])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), LBLUE),
        ('LINEBELOW', (0,0), (-1,0), 0.8, NAVY),
        ('LINEBELOW', (0,1), (-1,-1), 0.4, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    return t


def _ranking_table(companies: list[dict], styles: dict) -> Table:
    header = [Paragraph(h, styles["table_header"]) for h in
              ["Rank", "Company", "Ticker", "Score", "Grade", "Percentile", "Confidence"]]
    rows = [header]
    for c in companies:
        rank = c.get("rank")
        score = c.get("earnings_quality_score")
        pct = c.get("percentile")
        is_subj = c.get("is_subject")
        name_style = "table_label"
        row = [
            Paragraph(str(rank) if rank is not None else "—", styles["table_cell"]),
            Paragraph(("<b>" + (c.get("name") or c.get("ticker") or "") + "</b>") if is_subj
                       else (c.get("name") or c.get("ticker") or ""), styles[name_style]),
            Paragraph(c.get("ticker") or "", styles["table_cell"]),
            Paragraph(f"{score}/100" if score is not None else "n/a", styles["table_cell"]),
            Paragraph(c.get("letter_grade") or "N/A", styles["table_cell"]),
            Paragraph(f"{pct}th" if pct is not None else "n/a", styles["table_cell"]),
            Paragraph(c.get("confidence") or "n/a", styles["table_cell"]),
        ]
        rows.append(row)

    t = Table(rows, colWidths=[CW*0.07, CW*0.33, CW*0.13, CW*0.13, CW*0.11, CW*0.13, CW*0.10],
               repeatRows=1)
    style = [
        ('BACKGROUND', (0,0), (-1,0), LBLUE),
        ('LINEBELOW', (0,0), (-1,0), 0.8, NAVY),
        ('LINEBELOW', (0,1), (-1,-1), 0.4, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]
    for i, c in enumerate(companies):
        if c.get("is_subject"):
            style.append(('BACKGROUND', (0, i+1), (-1, i+1), HexColor('#EAF3FA')))
    t.setStyle(TableStyle(style))
    return t


def _fmt_b(v) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.2f}B" if abs(v) >= 1000 else f"{v:.2f}M"


class EarningsQualityPDFGenerator:
    """Renders the Earnings Quality Score PDF using ReportLab."""

    def render(self, subject: CompanyData, analysis: dict, output_path: str) -> None:
        report_date = datetime.utcnow().strftime("%Y-%m-%d")
        styles = _styles(subject.name or "")
        companies = analysis.get("companies") or []

        def _page_header(canvas, doc):
            _draw_header(canvas, doc, subject, report_date)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=f"{subject.name or subject.ticker} — Earnings Quality Score",
            author="Your Humble EquityBot",
            subject="Earnings Quality Score",
        )

        story = []

        # ── PAGE 1: Ranking + subject grade box ──────────────────────────
        story += self._page1(subject, analysis, companies, styles)
        story.append(PageBreak())

        # ── PAGE 2: Subject detail ───────────────────────────────────────
        subj_entry = next((c for c in companies if c.get("is_subject")), None)
        if subj_entry:
            story += self._detail_block(subj_entry, styles, is_subject=True)
        story.append(PageBreak())

        # ── PAGE 3+: Peer detail blocks ──────────────────────────────────
        peer_entries = [c for c in companies if not c.get("is_subject")]
        for i, entry in enumerate(peer_entries):
            story += self._detail_block(entry, styles, is_subject=False)
            if i < len(peer_entries) - 1:
                story.append(Spacer(1, 6*mm))

        story.append(Spacer(1, 6*mm))
        story.append(Paragraph(
            f"<i>Analysis generated by {LLM_MODEL} ({LLM_PROVIDER}). "
            f"Data source: EODHD, with yfinance used as a fallback where EODHD "
            f"data is unavailable.</i>",
            ParagraphStyle("llm_disc", fontName="Helvetica-Oblique", fontSize=7,
                           textColor=HexColor("#888888"), leading=9),
        ))

        doc.build(story, onFirstPage=_page_header, onLaterPages=_page_header)
        logger.info(f"[PDF EarningsQuality] Saved: {output_path}")

    # ── Page 1 ────────────────────────────────────────────────────────────
    def _page1(self, subject: CompanyData, analysis: dict, companies: list[dict], styles: dict) -> list:
        flow = []
        subj_entry = next((c for c in companies if c.get("is_subject")), None)
        if subj_entry:
            flow.append(_grade_box(subj_entry, styles))
            flow.append(Spacer(1, 8*mm))

        n = analysis.get("universe_size", len(companies))
        flow.append(section_title(f"Earnings Quality Ranking — Universe of {n}", styles))
        flow.append(Spacer(1, 2*mm))
        flow.append(_ranking_table(companies, styles))
        flow.append(Spacer(1, 4*mm))

        weight_line = " · ".join(
            f"{EQ_DIMENSION_LABELS[k]} {v*100:.0f}%" for k, v in EQ_WEIGHTS.items()
        )
        flow.append(Paragraph(
            f"<b>Weighting:</b> {weight_line}",
            styles["body_small"],
        ))
        flow.append(Paragraph(
            "Scores reflect the reliability, sustainability, and cash-backed nature of "
            "reported earnings — not profitability alone. Percentile and rank are computed "
            "from the scores above within this analyzed universe.",
            styles["body_small"],
        ))
        return flow

    # ── Detail block (subject or peer) ──────────────────────────────────
    def _detail_block(self, entry: dict, styles: dict, is_subject: bool) -> list:
        flow = []
        label = "SUBJECT COMPANY" if is_subject else "PEER"
        title = f"{entry.get('name') or entry.get('ticker')} ({entry.get('ticker')}) — {label}"
        flow.append(section_title(title, styles))

        score = entry.get("earnings_quality_score")
        grade = entry.get("letter_grade") or "N/A"
        pct = entry.get("percentile")
        conf = entry.get("confidence") or "n/a"
        colour_hex = _grade_colour_hex(grade)
        flow.append(Paragraph(
            f'Score: <font color="{colour_hex}"><b>{score}/100</b></font> &nbsp;|&nbsp; '
            f'Grade: <font color="{colour_hex}"><b>{grade}</b></font> &nbsp;|&nbsp; '
            f'Percentile: {pct if pct is not None else "n/a"} &nbsp;|&nbsp; '
            f'Confidence: {conf}',
            styles["rec_body"],
        ))
        flow.append(Spacer(1, 2*mm))

        flow.append(_subscores_table(entry.get("subscores") or {}, styles))
        flow.append(Spacer(1, 3*mm))

        flow.append(Paragraph(entry.get("explanation") or "", styles["body"]))

        flow.append(Paragraph("<b>Top Strengths</b>", styles["table_label"]))
        flow += _bullets(entry.get("top_strengths") or [], styles)
        flow.append(Spacer(1, 2*mm))

        flow.append(Paragraph("<b>Top Risks</b>", styles["table_label"]))
        flow += _bullets(entry.get("top_risks") or [], styles)
        flow.append(Spacer(1, 2*mm))

        red_flags = entry.get("red_flags") or []
        if red_flags:
            flow.append(Paragraph("<b>Accounting Red Flags</b>", styles["table_label"]))
            flow += _bullets(red_flags, styles, style_key="bullet_red")

        return [KeepTogether(flow[:3])] + flow[3:]
