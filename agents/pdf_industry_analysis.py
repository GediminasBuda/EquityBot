"""
pdf_industry_analysis.py — Multi-page PDF for "Industry Analysis" framework.

Layout:
  Page  1-2 : Executive Summary + 5-Forces Scorecard table
  Pages 3-12: One detailed page per force × 5
              (state_2026 + historical evolution + confidence + sources)
  Page 13   : Competitive Advantage — Summary banner + summary text
  Page 14-15: Competitive Advantage — Detailed analysis
  Page 16   : Key Uncertainties + adversarial appendix (if present)

Visual language matches the rest of the EquityBot suite (Pantone 303
ink-saving palette).
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, PageBreak, KeepTogether, HRFlowable,
)

from data_sources.base import CompanyData
from agents.pdf_fisher import (
    _styles, _section, _split_paragraphs, _draw_header,
    ML, MR, MT, MB, CW,
    NAVY, BLUE, LBLUE, MGRAY, ITALIC_FONT,
)

logger = logging.getLogger(__name__)


# ── Intensity palette ────────────────────────────────────────────────────────

_INTENSITY_COLOR = {
    "Strong":   HexColor("#C0392B"),     # red — high pressure on profits
    "Moderate": HexColor("#F1C40F"),     # yellow
    "Weak":     HexColor("#1A7E3D"),     # green — favourable
}

_ATTRACT_COLOR = {
    "Very Unattractive": HexColor("#C0392B"),
    "Unattractive":      HexColor("#E67E22"),
    "Neutral":           HexColor("#F1C40F"),
    "Attractive":        HexColor("#52BE80"),
    "Very Attractive":   HexColor("#1A7E3D"),
}

_TRAJ_ICON = {
    "Improved Materially":     "▲▲",
    "Improved":                "▲",
    "Stable":                  "→",
    "Deteriorated":            "▼",
    "Deteriorated Materially": "▼▼",
}

_ADV_COLOR = {
    "None":   HexColor("#C0392B"),
    "Small":  HexColor("#F1C40F"),
    "Large":  HexColor("#1A7E3D"),
}

_ADV_EVOL_ICON = {
    "Eroded Materially":     "▼▼",
    "Eroded":                "▼",
    "Stable":                "→",
    "Strengthened":          "▲",
    "Strengthened Materially": "▲▲",
}


# ── Extra styles for the industry report ─────────────────────────────────────

def _extend_styles(styles: dict) -> dict:
    if "ia_force_title" in styles:
        return styles
    styles["ia_force_title"] = ParagraphStyle(
        "ia_force_title", fontName="Helvetica-Bold", fontSize=12,
        textColor=NAVY, alignment=TA_LEFT, spaceBefore=2, spaceAfter=6,
        leading=14,
    )
    styles["ia_sub_title"] = ParagraphStyle(
        "ia_sub_title", fontName="Helvetica-Bold", fontSize=9.5,
        textColor=BLUE, alignment=TA_LEFT, spaceBefore=4, spaceAfter=2,
        leading=12,
    )
    styles["ia_body"] = ParagraphStyle(
        "ia_body", fontName="Helvetica", fontSize=9.5,
        textColor=HexColor("#222"), alignment=TA_JUSTIFY,
        leading=13, spaceAfter=4,
    )
    styles["ia_source"] = ParagraphStyle(
        "ia_source", fontName=ITALIC_FONT, fontSize=8,
        textColor=MGRAY, alignment=TA_LEFT, leading=11, spaceAfter=2,
    )
    styles["ia_score_cell"] = ParagraphStyle(
        "ia_score_cell", fontName="Helvetica-Bold", fontSize=9,
        textColor=white, alignment=TA_CENTER, leading=11,
    )
    styles["ia_score_label"] = ParagraphStyle(
        "ia_score_label", fontName="Helvetica", fontSize=9,
        textColor=NAVY, alignment=TA_LEFT, leading=11,
    )
    styles["ia_score_take"] = ParagraphStyle(
        "ia_score_take", fontName="Helvetica", fontSize=8.5,
        textColor=HexColor("#444"), alignment=TA_LEFT, leading=11,
    )
    styles["ia_banner_label"] = ParagraphStyle(
        "ia_banner_label", fontName="Helvetica", fontSize=9.5,
        textColor=white, alignment=TA_LEFT, leading=12,
    )
    styles["ia_banner_value"] = ParagraphStyle(
        "ia_banner_value", fontName="Helvetica-Bold", fontSize=15,
        textColor=white, alignment=TA_LEFT, leading=17,
    )
    styles["ia_swot_header"] = ParagraphStyle(
        "ia_swot_header", fontName="Helvetica-Bold", fontSize=10,
        textColor=white, alignment=TA_CENTER, leading=13,
    )
    styles["ia_swot_body"] = ParagraphStyle(
        "ia_swot_body", fontName="Helvetica", fontSize=8.5,
        textColor=HexColor("#111"), alignment=TA_JUSTIFY,
        leading=12, spaceAfter=2,
    )
    styles["ia_swot_summary"] = ParagraphStyle(
        "ia_swot_summary", fontName="Helvetica", fontSize=9.5,
        textColor=HexColor("#222"), alignment=TA_JUSTIFY,
        leading=13, spaceAfter=4,
    )
    return styles


# ── Attractiveness banner ────────────────────────────────────────────────────

def _attractiveness_banner(rating: str, trajectory: str, styles: dict) -> Table:
    colour = _ATTRACT_COLOR.get(rating, HexColor("#888"))
    arrow  = _TRAJ_ICON.get(trajectory, "→")
    rows = [[
        Paragraph(f"<b>Industry attractiveness</b><br/>"
                  f"<font size='15'><b>{rating}</b></font><br/>"
                  f"<font size='9'>Trajectory 2015 → 2026:  <b>{arrow}  {trajectory}</b></font>",
                  styles["ia_banner_label"]),
    ]]
    t = Table(rows, colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), colour),
        ("TEXTCOLOR",    (0, 0), (-1, -1), white),
        ("LEFTPADDING",  (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING",   (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
    ]))
    return t


# ── 5-Forces scorecard table ─────────────────────────────────────────────────

def _scorecard_table(scorecard: list, styles: dict) -> Table:
    header = [
        Paragraph("<b>Force</b>", styles["peer_hdr"] if "peer_hdr" in styles else styles["ia_score_label"]),
        Paragraph("<b>Intensity</b>", styles["ia_score_label"]),
        Paragraph("<b>Takeaway</b>", styles["ia_score_label"]),
    ]
    rows = [header]
    for s in scorecard:
        intensity = s.get("intensity", "Moderate")
        intensity_cell = Paragraph(intensity, styles["ia_score_cell"])
        rows.append([
            Paragraph(s.get("force_name", ""), styles["ia_score_label"]),
            intensity_cell,
            Paragraph(s.get("one_line_takeaway", ""), styles["ia_score_take"]),
        ])

    col_widths = [CW * 0.30, CW * 0.18, CW * 0.52]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND",     (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",      (0, 0), (-1, 0), white),
        ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0), 9),
        ("TOPPADDING",     (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING",  (0, 0), (-1, 0), 5),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
        ("TOPPADDING",     (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING",  (0, 1), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#F8FAFC"), white]),
    ]
    for i, s in enumerate(scorecard, start=1):
        intensity = s.get("intensity", "Moderate")
        style.append(("BACKGROUND", (1, i), (1, i),
                      _INTENSITY_COLOR.get(intensity, HexColor("#888"))))
    t.setStyle(TableStyle(style))
    return t


# ── Force page (detailed) ────────────────────────────────────────────────────

def _force_page(force: dict, styles: dict) -> list:
    out = []
    intensity = force.get("current_assessment", "Moderate")
    colour = _INTENSITY_COLOR.get(intensity, HexColor("#888"))

    # Header strip with intensity badge
    header_tbl = Table(
        [[
            Paragraph(f"<b>{force.get('name', '')}</b>",
                      styles["ia_force_title"]),
            Paragraph(f"<b>{intensity}</b>", styles["ia_score_cell"]),
        ]],
        colWidths=[CW * 0.75, CW * 0.25],
    )
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (1, 0), (1, 0), colour),
        ("TEXTCOLOR",    (1, 0), (1, 0), white),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",        (1, 0), (1, 0), "CENTER"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
    ]))
    out.append(header_tbl)
    out.append(HRFlowable(width="100%", thickness=0.5, color=NAVY,
                          spaceBefore=2, spaceAfter=6))

    # 2026 state
    out.append(Paragraph("Current state (2026)", styles["ia_sub_title"]))
    out += _split_paragraphs(force.get("state_2026", ""), styles, "ia_body")

    out.append(Spacer(1, 4))

    # Historical evolution
    out.append(Paragraph("Historical evolution (2015 → 2026)", styles["ia_sub_title"]))
    out += _split_paragraphs(force.get("historical_evolution", ""), styles, "ia_body")

    out.append(Spacer(1, 4))

    # Evidence quality
    conf = force.get("confidence_level", "Medium")
    conf_color = {"High": "#1A7E3D", "Medium": "#D68910",
                  "Low": "#C0392B"}.get(conf, "#888")
    out.append(Paragraph(
        f"<b>Evidence quality:</b> "
        f"<font color='{conf_color}'><b>{conf} confidence</b></font>"
        + (f"  ·  <font color='#666'>{force.get('data_gaps', '')}</font>"
           if force.get('data_gaps') else ""),
        styles["ia_body"],
    ))

    # Sources
    sources = force.get("sources") or []
    if sources:
        out.append(Spacer(1, 3))
        out.append(Paragraph("Sources cited:", styles["ia_sub_title"]))
        for s in sources:
            out.append(Paragraph(f"•  {s}", styles["ia_source"]))

    return out


# ── Competitive advantage banner ─────────────────────────────────────────────

def _advantage_banner(size: str, evolution: str, styles: dict) -> Table:
    colour = _ADV_COLOR.get(size, HexColor("#888"))
    icon   = _ADV_EVOL_ICON.get(evolution, "→")
    rows = [[
        Paragraph(f"<b>Competitive advantage</b><br/>"
                  f"<font size='15'><b>{size}</b></font><br/>"
                  f"<font size='9'>Evolution over last 10 years:  "
                  f"<b>{icon}  {evolution}</b></font>",
                  styles["ia_banner_label"]),
    ]]
    t = Table(rows, colWidths=[CW])
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), colour),
        ("TEXTCOLOR",    (0, 0), (-1, -1), white),
        ("LEFTPADDING",  (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING",   (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
    ]))
    return t


# ── SWOT colours ─────────────────────────────────────────────────────────────
_SWOT_S_COLOR = HexColor("#1A5E2A")   # dark green  — Strengths
_SWOT_W_COLOR = HexColor("#922B21")   # dark red    — Weaknesses
_SWOT_O_COLOR = HexColor("#1A4A7A")   # dark blue   — Opportunities
_SWOT_T_COLOR = HexColor("#7D6608")   # dark amber  — Threats
_SWOT_S_BG    = HexColor("#EAF7EE")
_SWOT_W_BG    = HexColor("#FDECEA")
_SWOT_O_BG    = HexColor("#EAF1FB")
_SWOT_T_BG    = HexColor("#FEF9E7")


def _swot_page(swot: dict, company_name: str, styles: dict) -> list:
    """Render the SWOT analysis page."""
    out = []

    # Page title
    out += _section(f"SWOT Analysis — {company_name}", styles)

    # Summary box
    summary = swot.get("summary", "")
    if summary:
        out.append(Paragraph("<b>Summary</b>", styles["ia_sub_title"]))
        out += _split_paragraphs(summary, styles, "ia_swot_summary")
        out.append(Spacer(1, 10))

    # Build 2×2 SWOT table
    def _quadrant(header: str, text: str, hdr_color, bg_color) -> Table:
        hdr_para  = Paragraph(header, styles["ia_swot_header"])
        body_para = Paragraph(
            text.replace("\n", "<br/>") if text else "—",
            styles["ia_swot_body"],
        )
        t = Table(
            [[hdr_para], [body_para]],
            colWidths=[CW / 2 - 3],
        )
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (0, 0), hdr_color),
            ("BACKGROUND",    (0, 1), (0, 1), bg_color),
            ("TEXTCOLOR",     (0, 0), (0, 0), white),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 7),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    sw_tbl = _quadrant("S   STRENGTHS",     swot.get("strengths", ""),
                        _SWOT_S_COLOR, _SWOT_S_BG)
    wk_tbl = _quadrant("W   WEAKNESSES",    swot.get("weaknesses", ""),
                        _SWOT_W_COLOR, _SWOT_W_BG)
    op_tbl = _quadrant("O   OPPORTUNITIES", swot.get("opportunities", ""),
                        _SWOT_O_COLOR, _SWOT_O_BG)
    th_tbl = _quadrant("T   THREATS",       swot.get("threats", ""),
                        _SWOT_T_COLOR, _SWOT_T_BG)

    grid = Table(
        [[sw_tbl, wk_tbl],
         [op_tbl, th_tbl]],
        colWidths=[CW / 2, CW / 2],
        spaceBefore=4,
    )
    grid.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    out.append(grid)
    return out


# ── Main generator ───────────────────────────────────────────────────────────

class IndustryAnalysisPDFGenerator:
    """Multi-page PDF for the Porter 5 Forces + Competitive Advantage report."""

    def render(
        self,
        company: CompanyData,
        analysis: dict,
        output_path: str,
        adv_result=None,
    ) -> None:
        report_date = datetime.now().strftime("%Y-%m-%d")
        st = _extend_styles(_styles())

        def on_page(canvas, doc):
            _draw_header(canvas, doc, company, report_date)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"{company.name or company.ticker} — Industry Analysis",
            author="Your Humble EquityBot",
        )
        story = []

        # ── PAGE 1: Attractiveness banner + Executive Summary ────────────────
        story.append(_attractiveness_banner(
            analysis.get("industry_attractiveness", "Neutral"),
            analysis.get("trajectory", "Stable"),
            st,
        ))
        story.append(Spacer(1, 10))

        story += _section("Executive Summary", st)
        story += _split_paragraphs(analysis.get("executive_summary", ""),
                                   st, "ia_body")
        story.append(PageBreak())

        # ── PAGE 2: 5-Forces Scorecard + Structural Shifts + Strategic Implications ──
        story += _section("5-Forces Scorecard", st)
        story.append(_scorecard_table(analysis.get("scorecard", []), st))
        story.append(Spacer(1, 12))

        story += _section("Key Structural Shifts (2015 → 2026)", st)
        for s in analysis.get("structural_shifts", []):
            if s:
                story.append(Paragraph(f"•  {s}", st["risk_bullet"]))
        story.append(Spacer(1, 8))

        story += _section("Strategic Implications", st)
        story += _split_paragraphs(analysis.get("strategic_implications", ""),
                                   st, "ia_body")
        story.append(PageBreak())

        # ── PAGES 3-7: One page per force ────────────────────────────────────
        for i, f in enumerate(analysis.get("forces", [])):
            story += _force_page(f, st)
            if i < len(analysis.get("forces", [])) - 1:
                story.append(PageBreak())

        # ── PAGE 8: Competitive Advantage — banner + summary ────────────────
        story.append(PageBreak())
        story.append(_advantage_banner(
            analysis.get("competitive_advantage_size", "Small"),
            analysis.get("competitive_advantage_evolution", "Stable"),
            st,
        ))
        story.append(Spacer(1, 10))

        story += _section("Competitive Advantage — Summary", st)
        story += _split_paragraphs(
            analysis.get("competitive_advantage_summary", ""), st, "ia_body",
        )
        story.append(PageBreak())

        # ── PAGE 9: Competitive Advantage detail (Porter 1985) ──────────────
        story += _section("Competitive Advantage — Detailed Assessment", st)
        story += _split_paragraphs(
            analysis.get("competitive_advantage_detail", ""), st, "ia_body",
        )

        ca_sources = analysis.get("competitive_advantage_sources") or []
        if ca_sources:
            story.append(Spacer(1, 6))
            story.append(Paragraph("Sources cited:", st["ia_sub_title"]))
            for s in ca_sources:
                story.append(Paragraph(f"•  {s}", st["ia_source"]))

        # ── SWOT page ────────────────────────────────────────────────────────
        swot = analysis.get("swot") or {}
        if any(swot.get(k) for k in ("strengths", "weaknesses",
                                      "opportunities", "threats")):
            story.append(PageBreak())
            story += _swot_page(swot, company.name or company.ticker, st)

        # ── FINAL PAGE: Uncertainties + footer ──────────────────────────────
        unc = analysis.get("key_uncertainties") or []
        if unc:
            story.append(PageBreak())
            story += _section("Key Uncertainties & Data Gaps", st)
            for u in unc:
                if u:
                    story.append(Paragraph(f"•  {u}", st["risk_bullet"]))

        # Footnote
        story.append(Spacer(1, 12))
        story.append(Paragraph(
            f"<i>Data sources: EODHD (financial / corporate data), "
            f"LLM training (industry context & citations).  |  "
            f"Framework: Porter's 5 Forces (1979) + Competitive Advantage (1985).  |  "
            f"Generated: {report_date}.  |  "
            f"Note: industry citations are drawn from the LLM's training "
            f"data through 2024; recent (2025+) claims are hedged.</i>",
            ParagraphStyle("fn", fontName=ITALIC_FONT, fontSize=6.5,
                           textColor=MGRAY, alignment=TA_CENTER, leading=9),
        ))

        if adv_result is not None:
            from agents.pdf_adversarial import build_adversarial_page
            story.append(PageBreak())
            story += build_adversarial_page(adv_result)

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        logger.info(f"[PDF Industry] Saved: {output_path}")
