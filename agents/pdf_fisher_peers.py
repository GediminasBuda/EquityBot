"""
pdf_fisher_peers.py — Renderer for "Fisher Alternatives + Peers".

Produces the same 3-page Fisher Alternatives report (subject company —
business overview, 15-point scorecard, 7 Powers, moat, risks,
conclusion, recommendation) PLUS one additional page:

  Page 4: Peer Group Fisher Comparison
            ┌────────────┬──────┬───────────────┬───────┬──────┬─────┬────────┬─────────┬─────────┐
            │ Company    │ Tk   │ 15-point heat │ Price │ MCap │ P/E │ EV/EBIT│ EV/Sales│ Gearing │
            ├────────────┼──────┼───────────────┼───────┼──────┼─────┼────────┼─────────┼─────────┤
            │ Lockheed   │ LMT  │ ■■■■▣▣▣▤▤▤... │  …    │  …   │  …  │   …    │   …     │   …     │
            ...

            Per-peer 3-6 sentence summaries below the table.

The heat-map cells use a red→yellow→green palette so a glance tells the
reader which Fisher points are each peer's strongest / weakest.
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
    Spacer, PageBreak, KeepTogether,
)

from data_sources.base import CompanyData
from agents.pdf_fisher import (
    FisherPDFGenerator,
    _styles, _section, _split_paragraphs, _fisher_table,
    _score_summary_table, _powers_table, _recommendation_box,
    ScoreBar, _draw_header,
    ML, MR, MT, MB, CW,
    NAVY, BLUE, LBLUE, MGRAY, ITALIC_FONT,
)

logger = logging.getLogger(__name__)


# ── Heat-map palette (score 1-5) ─────────────────────────────────────────────
# Five steps from red (1) → green (5). Chosen to print legibly even on a
# black-and-white printer (luminance increases monotonically with score).
_HEAT = {
    1: HexColor("#C0392B"),   # dark red
    2: HexColor("#E67E22"),   # orange
    3: HexColor("#F1C40F"),   # yellow
    4: HexColor("#52BE80"),   # light green
    5: HexColor("#1A7E3D"),   # dark green
}


def _heat_color(score: int) -> HexColor:
    try:
        s = max(1, min(5, int(score)))
    except Exception:
        s = 3
    return _HEAT[s]


# ── Formatters (compact for the peer table) ──────────────────────────────────

def _b(v) -> str:
    """Billions / millions formatter."""
    if v is None: return "—"
    try: v = float(v)
    except Exception: return "—"
    if abs(v) >= 1_000_000: return f"{v/1_000_000:.2f}T"
    if abs(v) >= 1_000:     return f"{v/1_000:.1f}B"
    return f"{v:,.0f}M"


def _x(v, d=1) -> str:
    if v is None: return "—"
    try: return f"{float(v):.{d}f}×"
    except Exception: return "—"


def _px(v, ccy: str = "") -> str:
    if v is None: return "—"
    try: return f"{float(v):,.2f} {ccy}".strip()
    except Exception: return "—"


# ── Score badge (single coloured cell with the total + grade) ────────────────

# Total-score → colour band. Matches the Fisher grading bands used in
# _validate_analysis: A=65+, B=55-64, C=45-54, D=35-44, F<35.
_SCORE_BANDS = [
    (65, HexColor("#1A7E3D")),   # A — dark green
    (55, HexColor("#52BE80")),   # B — light green
    (45, HexColor("#F1C40F")),   # C — yellow
    (35, HexColor("#E67E22")),   # D — orange
    (0,  HexColor("#C0392B")),   # F — dark red
]


def _score_band_color(total: int | float) -> HexColor:
    try:
        t = int(total)
    except Exception:
        return _SCORE_BANDS[-1][1]
    for threshold, colour in _SCORE_BANDS:
        if t >= threshold:
            return colour
    return _SCORE_BANDS[-1][1]


def _build_score_badge(total: int | float, grade: str = "") -> Table:
    """
    A single coloured cell displaying the peer's total Fisher score
    (e.g. "62  B"). Replaces the previous 15-cell heat-map strip so
    the column carries just the headline number.
    """
    try:
        total_int = int(total)
    except Exception:
        total_int = 0
    label = f"{total_int} / 75"
    if grade:
        label += f"   {grade}"
    t = Table([[label]], colWidths=[CW * 0.13], rowHeights=[14])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, 0), _score_band_color(total_int)),
        ("TEXTCOLOR",     (0, 0), (0, 0), white),
        ("FONTNAME",      (0, 0), (0, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (0, 0), 8.5),
        ("ALIGN",         (0, 0), (0, 0), "CENTER"),
        ("VALIGN",        (0, 0), (0, 0), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (0, 0), 2),
        ("RIGHTPADDING",  (0, 0), (0, 0), 2),
        ("TOPPADDING",    (0, 0), (0, 0), 0),
        ("BOTTOMPADDING", (0, 0), (0, 0), 0),
    ]))
    return t


# ── Peer comparison table ────────────────────────────────────────────────────

_PEER_COLS = ("Company", "Ticker", "Fisher Score", "Price", "MCap",
              "P/E", "EV/EBIT", "EV/Sales", "Gearing")

# Column widths (must sum to 1.0). Fisher Score is a coloured badge so
# it doesn't need much horizontal room.
_PEER_WIDTHS = [
    0.24, 0.10, 0.13, 0.11, 0.11,
    0.08, 0.08, 0.08, 0.07,
]


def _peer_data_row(idx: int, subject: bool, peer_dict: dict,
                   cd: Optional[CompanyData], styles: dict) -> list:
    cur = (cd.currency if cd else None) or ""
    name = peer_dict.get("name") or (cd.name if cd else "") or peer_dict.get("ticker", "")
    name = (name[:22] + "…") if len(name) > 23 else name
    ticker = peer_dict.get("ticker") or (cd.ticker if cd else "")
    total = peer_dict.get("total_score", 0)
    grade = peer_dict.get("grade", "")
    score_badge = _build_score_badge(total, grade)
    price    = _px(cd.current_price, cur) if cd else "—"
    mcap     = _b(cd.market_cap) + (f" {cur}" if cd and cur else "") if cd else "—"
    pe       = _x(cd.pe_ratio) if cd else "—"
    ev_ebit  = _x(cd.ev_ebit) if cd else "—"
    ev_sales = _x(cd.ev_sales) if cd else "—"
    gearing  = _x(cd.gearing) if cd else "—"

    # Bold-up the subject row so reader doesn't lose it in the table
    name_para = Paragraph(
        f"<b>{name}</b>" + (" <font color='#888' size='6'> (subject)</font>" if subject else ""),
        styles["peer_cell"]
    )
    return [
        name_para,
        Paragraph(ticker, styles["peer_cell_mono"]),
        score_badge,
        Paragraph(price, styles["peer_cell_num"]),
        Paragraph(mcap, styles["peer_cell_num"]),
        Paragraph(pe, styles["peer_cell_num"]),
        Paragraph(ev_ebit, styles["peer_cell_num"]),
        Paragraph(ev_sales, styles["peer_cell_num"]),
        Paragraph(gearing, styles["peer_cell_num"]),
    ]


def _peer_comparison_table(
    subject_company: CompanyData,
    subject_analysis: dict,
    peer_analyses: list[dict],
    peer_companies: dict[str, CompanyData],
    styles: dict,
) -> Table:
    # Header row
    header = [Paragraph(f"<b>{h}</b>", styles["peer_hdr"]) for h in _PEER_COLS]
    rows = [header]

    # Build the subject's heat scores from its fisher_points so it sits in
    # the same comparison table — that's how the reader sees the gap.
    subj_scores = []
    for pt in subject_analysis.get("fisher_points") or []:
        if isinstance(pt, dict):
            try:
                subj_scores.append(int(round(float(pt.get("score") or 3))))
            except Exception:
                subj_scores.append(3)
    while len(subj_scores) < 15:
        subj_scores.append(3)
    subj_total = subject_analysis.get("fisher_total_score") or sum(subj_scores)

    subj_dict = {
        "ticker": subject_company.ticker,
        "name":   subject_company.name or subject_company.ticker,
        "fisher_scores": subj_scores[:15],
        "total_score":   subj_total,
        "grade":         subject_analysis.get("fisher_grade", ""),
    }
    rows.append(_peer_data_row(0, True, subj_dict, subject_company, styles))

    for i, pa in enumerate(peer_analyses, start=1):
        cd = peer_companies.get(pa.get("ticker", "").upper())
        rows.append(_peer_data_row(i, False, pa, cd, styles))

    col_widths = [w * CW for w in _PEER_WIDTHS]
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    ts = TableStyle([
        # Header strip
        ("BACKGROUND",  (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR",   (0, 0), (-1, 0), white),
        ("ALIGN",       (0, 0), (-1, 0), "CENTER"),
        ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, 0), 7.5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("TOPPADDING",    (0, 0), (-1, 0), 4),
        # Body
        ("VALIGN",         (0, 1), (-1, -1), "MIDDLE"),
        ("FONTSIZE",       (0, 1), (-1, -1), 7.5),
        ("LEFTPADDING",    (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 2),
        ("TOPPADDING",     (0, 1), (-1, -1), 2),
        ("BOTTOMPADDING",  (0, 1), (-1, -1), 2),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#F8FAFC"), white]),
        ("BACKGROUND",     (0, 1), (-1, 1), LBLUE),   # subject row highlight
        ("LINEBELOW",      (0, 0), (-1, 0), 0.6, NAVY),
        ("LINEBELOW",      (0, -1), (-1, -1), 0.4, MGRAY),
    ])
    t.setStyle(ts)
    return t


# ── Heat-map legend (small) ──────────────────────────────────────────────────

def _heat_legend(styles: dict) -> Table:
    """Score-band legend. Maps grade letter ↔ colour ↔ total range."""
    grade_cells = [
        ("A", "65+",     _SCORE_BANDS[0][1]),   # dark green
        ("B", "55-64",   _SCORE_BANDS[1][1]),   # light green
        ("C", "45-54",   _SCORE_BANDS[2][1]),   # yellow
        ("D", "35-44",   _SCORE_BANDS[3][1]),   # orange
        ("F", "< 35",    _SCORE_BANDS[4][1]),   # red
    ]
    cells_row: list = [
        Paragraph("Fisher score band:", styles["peer_legend_label"]),
    ]
    for letter, _r, _c in grade_cells:
        cells_row.append(Paragraph(letter, styles["peer_cell_legend"]))
    cells_row.append(Paragraph(
        "A = 65+ · B = 55-64 · C = 45-54 · D = 35-44 · F < 35",
        styles["peer_legend_label"],
    ))

    col_widths = [CW * 0.18] + [16] * 5 + [CW * 0.55]
    t = Table([cells_row], colWidths=col_widths, rowHeights=[12])
    style = [
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE",     (0, 0), (-1, -1), 7),
        ("TEXTCOLOR",    (1, 0), (5, 0), white),
        ("FONTNAME",     (1, 0), (5, 0), "Helvetica-Bold"),
        ("ALIGN",        (1, 0), (5, 0), "CENTER"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]
    for i, (_letter, _rng, colour) in enumerate(grade_cells, start=1):
        style.append(("BACKGROUND", (i, 0), (i, 0), colour))
    t.setStyle(TableStyle(style))
    return t


# ── Peer summaries section ───────────────────────────────────────────────────

def _peer_summary_block(peer_analyses: list[dict], styles: dict) -> list:
    """Build the per-peer 3-6 sentence summaries below the peer table."""
    out = []
    if not peer_analyses:
        return out
    out += _section("Peer-by-peer Fisher commentary", styles)
    for pa in peer_analyses:
        name   = pa.get("name") or pa.get("ticker", "?")
        ticker = pa.get("ticker", "")
        total  = pa.get("total_score", "—")
        grade  = pa.get("grade", "—")
        summary = pa.get("summary") or "No summary available."
        header = (f"<b>{name}</b>  <font color='#666' size='8'>({ticker})</font>"
                  f"  ·  Total <b>{total}</b>/75  ·  Grade <b>{grade}</b>")
        out.append(Paragraph(header, styles["peer_summary_hdr"]))
        out.append(Paragraph(summary, styles["peer_summary"]))
        out.append(Spacer(1, 4))
    return out


# ── Extra styles for the peer page ───────────────────────────────────────────

def _extend_styles(styles: dict) -> dict:
    """Add peer-specific paragraph styles on top of the Fisher base set."""
    if "peer_hdr" in styles:
        return styles   # already extended
    styles["peer_hdr"] = ParagraphStyle(
        "peer_hdr", fontName="Helvetica-Bold", fontSize=7.5,
        textColor=white, alignment=TA_CENTER, leading=9,
    )
    styles["peer_cell"] = ParagraphStyle(
        "peer_cell", fontName="Helvetica", fontSize=7.5,
        textColor=HexColor("#222"), alignment=TA_LEFT, leading=10,
    )
    styles["peer_cell_mono"] = ParagraphStyle(
        "peer_cell_mono", fontName="Courier", fontSize=7,
        textColor=HexColor("#1B3F6E"), alignment=TA_CENTER, leading=10,
    )
    styles["peer_cell_num"] = ParagraphStyle(
        "peer_cell_num", fontName="Helvetica", fontSize=7.5,
        textColor=HexColor("#222"), alignment=TA_CENTER, leading=10,
    )
    styles["peer_cell_legend"] = ParagraphStyle(
        "peer_cell_legend", fontName="Helvetica-Bold", fontSize=7,
        textColor=white, alignment=TA_CENTER, leading=10,
    )
    styles["peer_legend_label"] = ParagraphStyle(
        "peer_legend_label", fontName="Helvetica", fontSize=7,
        textColor=MGRAY, alignment=TA_LEFT, leading=10,
    )
    styles["peer_summary_hdr"] = ParagraphStyle(
        "peer_summary_hdr", fontName="Helvetica", fontSize=9,
        textColor=NAVY, alignment=TA_LEFT, spaceBefore=4, spaceAfter=1,
        leading=12,
    )
    styles["peer_summary"] = ParagraphStyle(
        "peer_summary", fontName="Helvetica", fontSize=8.5,
        textColor=HexColor("#333"), alignment=TA_JUSTIFY,
        leading=12, spaceAfter=4,
    )
    return styles


# ── Main generator ────────────────────────────────────────────────────────────

class FisherPeersPDFGenerator:
    """
    Renders the 4-page "Fisher Alternatives + Peers" PDF.

    Pages 1-3 reuse the standard Fisher Alternatives layout. Page 4 is the
    peer comparison page (heat-map table + per-peer summaries).
    """

    def render(
        self,
        company: CompanyData,
        analysis: dict,
        peer_analyses: list[dict],
        peer_companies: dict[str, CompanyData],
        output_path: str,
        adv_result=None,
    ) -> None:
        report_date = datetime.now().strftime("%Y-%m-%d")
        st = _extend_styles(_styles())

        def on_page(canvas, doc):
            _draw_header(canvas, doc, company, report_date,
                         model_name="Fisher Alternatives + Peers")

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"{company.name or company.ticker} — Fisher Alternatives + Peers",
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

        story += _section("Fisher Score Summary", st)
        story.append(_score_summary_table(analysis, st))
        story.append(Spacer(1, 4))

        total = analysis.get("fisher_total_score", 0)
        story.append(ScoreBar(total))
        story.append(Spacer(1, 4))

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

        story.append(KeepTogether([
            _recommendation_box(
                analysis.get("recommendation", "HOLD"),
                analysis.get("recommendation_rationale", ""),
                st,
            )
        ]))
        story.append(PageBreak())

        # ── PAGE 4: Peer Group Fisher Comparison ─────────────────────────────
        story += _section("Peer Group Fisher Comparison", st)
        if not peer_analyses:
            story.append(Paragraph(
                "<i>No peers were analysed for this report — supply peer "
                "tickers in the Peer Tickers field on the Report Generator "
                "page to populate this section.</i>",
                st["body"],
            ))
        else:
            story.append(_peer_comparison_table(
                company, analysis, peer_analyses, peer_companies, st
            ))
            story.append(Spacer(1, 4))
            story.append(_heat_legend(st))
            story.append(Spacer(1, 8))
            story += _peer_summary_block(peer_analyses, st)

        # Footnote
        sources = ", ".join(company.data_sources) if company.data_sources else "yfinance"
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"<i>Data sources: {sources}  |  "
            f"Framework: Philip Fisher (1958) + Hamilton Helmer (2016) + Peer Comparison  |  "
            f"Generated: {report_date}</i>",
            ParagraphStyle("fn", fontName=ITALIC_FONT, fontSize=6.5,
                           textColor=MGRAY, alignment=TA_CENTER, leading=9),
        ))

        # Optional adversarial review on yet another page
        if adv_result is not None:
            from agents.pdf_adversarial import build_adversarial_page
            story.append(PageBreak())
            story += build_adversarial_page(adv_result)

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        logger.info(f"[PDF Fisher+Peers] Saved: {output_path}")
