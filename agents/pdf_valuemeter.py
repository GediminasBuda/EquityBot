"""
pdf_valuemeter.py — ReportLab PDF renderer for the ValueMeter model.

Produces a 3-page PDF:
  Page 1: Header + Peer Group Definition (included + excluded) + Raw Data Table
  Page 2: Score & Rankings Table + Interpretation (verdict, drivers, risks)
  Page 3: Conclusion + Final Summary Table (Ticker, CCY, Price, Mcap, ROE,
           EBIT Margin, EV/Sales, Value Score)

Visual style: consistent with pdf_gravity.py and pdf_fisher.py.

ReportLab rules (DO NOT violate):
  - Never use Unicode sub/superscript characters — use <sub>/<super> XML tags.
  - Never call HexColor.hexval() for markup — use plain "#RRGGBB" string constants.
  - Color in Paragraph XML: <font color="#RRGGBB">text</font>.
"""

from __future__ import annotations
import logging
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

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)

# ── Page geometry ─────────────────────────────────────────────────────────────
W, H = A4
ML = MR = 15 * mm
MT = 36 * mm
MB = 14 * mm
CW = W - ML - MR

# ── Colours — plain string constants (never use .hexval()) ────────────────────
NAVY_HEX    = "#003F54"
BLUE_HEX    = "#2E75B6"
GREEN_HEX   = "#1A7E3D"
RED_HEX     = "#C0392B"
AMBER_HEX   = "#D68910"
MGRAY_HEX   = "#555555"
LGRAY_HEX   = "#F0F0F0"
DGRAY_HEX   = "#333333"
WHITE_HEX   = "#FFFFFF"
LGREEN_HEX  = "#D4EDDA"
LRED_HEX    = "#FADBD8"
LAMBER_HEX  = "#FDEBD0"
LBLUE_HEX   = "#D6E8F7"
BORDER_HEX  = "#BBCCDD"

NAVY   = HexColor(NAVY_HEX)
BLUE   = HexColor(BLUE_HEX)
GREEN  = HexColor(GREEN_HEX)
RED    = HexColor(RED_HEX)
AMBER  = HexColor(AMBER_HEX)
MGRAY  = HexColor(MGRAY_HEX)
LGRAY  = HexColor(LGRAY_HEX)
LGREEN = HexColor(LGREEN_HEX)
LRED   = HexColor(LRED_HEX)
LAMBER = HexColor(LAMBER_HEX)
LBLUE  = HexColor(LBLUE_HEX)
BORDER = HexColor(BORDER_HEX)

BASE_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"


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
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor(DGRAY_HEX),
            leading=13, alignment=TA_JUSTIFY, spaceAfter=4,
        ),
        "body_small": S("body_small",
            fontName=BASE_FONT, fontSize=7.5, textColor=MGRAY,
            leading=11, alignment=TA_JUSTIFY,
        ),
        "table_header": S("th",
            fontName=BOLD_FONT, fontSize=7, textColor=white,
            alignment=TA_CENTER, leading=9,
        ),
        "table_header_l": S("thl",
            fontName=BOLD_FONT, fontSize=7, textColor=white,
            alignment=TA_LEFT, leading=9,
        ),
        "table_cell": S("tc",
            fontName=BASE_FONT, fontSize=7, textColor=HexColor(DGRAY_HEX),
            alignment=TA_RIGHT, leading=9,
        ),
        "table_cell_l": S("tcl",
            fontName=BASE_FONT, fontSize=7, textColor=HexColor(DGRAY_HEX),
            alignment=TA_LEFT, leading=9,
        ),
        "table_cell_c": S("tcc",
            fontName=BASE_FONT, fontSize=7, textColor=HexColor(DGRAY_HEX),
            alignment=TA_CENTER, leading=9,
        ),
        "table_bold": S("tb",
            fontName=BOLD_FONT, fontSize=7, textColor=NAVY,
            alignment=TA_LEFT, leading=9,
        ),
        "table_bold_r": S("tbr",
            fontName=BOLD_FONT, fontSize=7, textColor=NAVY,
            alignment=TA_RIGHT, leading=9,
        ),
        "bullet": S("bullet",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor(DGRAY_HEX),
            leading=13, leftIndent=12, spaceAfter=3,
        ),
        "verdict_title": S("verdict_title",
            fontName=BOLD_FONT, fontSize=10, textColor=white,
            alignment=TA_CENTER, leading=13,
        ),
        "verdict_body": S("verdict_body",
            fontName=BASE_FONT, fontSize=8, textColor=white,
            alignment=TA_JUSTIFY, leading=12,
        ),
        "conclusion_label": S("conclusion_label",
            fontName=BOLD_FONT, fontSize=8, textColor=NAVY,
            leading=11, spaceAfter=2,
        ),
        "conclusion_body": S("conclusion_body",
            fontName=BASE_FONT, fontSize=8.5, textColor=HexColor(DGRAY_HEX),
            leading=13, alignment=TA_JUSTIFY, spaceAfter=6,
        ),
    }


# ── Formatters ────────────────────────────────────────────────────────────────

def _n(v, decimals=1, suffix="") -> str:
    """Format a float nicely; returns '—' for None."""
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _pct(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "—"


def _bn(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        return f"{f:.1f}B"
    except (TypeError, ValueError):
        return "—"


def _score_color(score) -> HexColor:
    """Green for high scores, amber for mid, red for low."""
    try:
        s = float(score)
        if s >= 65:
            return GREEN
        if s >= 40:
            return AMBER
        return RED
    except (TypeError, ValueError):
        return MGRAY


def _rating_colors(rating: str) -> tuple:
    """(bg, text) colours for conclusion rating banner."""
    r = (rating or "").lower()
    if r == "attractive":
        return GREEN, white
    if r == "unattractive":
        return RED, white
    return AMBER, white


def _verdict_color(verdict: str) -> HexColor:
    v = (verdict or "").lower()
    if "opportunity" in v:
        return GREEN
    if "trap" in v or "mirage" in v:
        return RED
    return AMBER


def _verdict_label(verdict: str) -> str:
    mapping = {
        "value_opportunity":        "VALUE OPPORTUNITY",
        "value_trap":               "VALUE TRAP",
        "cyclical_mirage":          "CYCLICAL MIRAGE",
        "fairly_priced_compounder": "FAIRLY PRICED COMPOUNDER",
    }
    return mapping.get((verdict or "").lower().strip(), (verdict or "UNKNOWN").upper())


# ── Page header / footer ──────────────────────────────────────────────────────

def _page_header(canvas, doc, company: CompanyData, report_date: str) -> None:
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, H - MT, W, MT, fill=1, stroke=0)

    # Title left
    canvas.setFillColor(white)
    canvas.setFont(BOLD_FONT, 14)
    canvas.drawString(ML, H - 18 * mm, "ValueMeter")

    canvas.setFont(BASE_FONT, 9)
    canvas.setFillColor(HexColor("#AACCDD"))
    canvas.drawString(ML, H - 24 * mm, "Prudent Value Score — Peer-Relative Valuation")

    # Company name right
    name = (company.name or company.ticker or "")[:40]
    ccy = company.currency or ""
    canvas.setFillColor(white)
    canvas.setFont(BOLD_FONT, 11)
    canvas.drawRightString(W - MR, H - 18 * mm, name)
    canvas.setFont(BASE_FONT, 8)
    canvas.setFillColor(HexColor("#AACCDD"))
    canvas.drawRightString(
        W - MR, H - 24 * mm,
        f"{company.ticker or ''}  ·  {ccy}  ·  {report_date}"
    )

    # Footer
    canvas.setFillColor(NAVY)
    canvas.rect(0, 0, W, MB, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#AACCDD"))
    canvas.setFont(BASE_FONT, 7)
    canvas.drawString(ML, 5 * mm, "ValueMeter — screening tool only, not an investment recommendation")
    canvas.drawRightString(W - MR, 5 * mm, f"Page {doc.page}")
    canvas.restoreState()


# ── Section heading helper ────────────────────────────────────────────────────

def _section(title: str, st: dict) -> list:
    return [
        Spacer(1, 3 * mm),
        Paragraph(title.upper(), st["section_title"]),
        HRFlowable(width=CW, thickness=0.5, color=BORDER, spaceAfter=3),
    ]


# ── Page 1: Peer Group + Raw Data Table ──────────────────────────────────────

def _build_page1(analysis: dict, st: dict, company: CompanyData) -> list:
    elems = []
    peer_group = analysis.get("peer_group") or []
    excluded   = analysis.get("excluded_peers") or []

    # ── Included peers ────────────────────────────────────────────────────────
    elems += _section("Peer Group — Included", st)

    if peer_group:
        for p in peer_group:
            is_subj = p.get("is_subject", False)
            ticker  = p.get("ticker", "?")
            name    = p.get("name", "")
            reason  = p.get("reason_included", "")
            label_color = NAVY_HEX if is_subj else DGRAY_HEX
            marker  = " ★" if is_subj else ""
            elems.append(Paragraph(
                f'<font color="{label_color}"><b>{ticker}{marker}  {name}</b></font>  '
                f'<font color="{MGRAY_HEX}">— {reason}</font>',
                st["body_small"],
            ))
    else:
        elems.append(Paragraph("No peer group data returned.", st["body"]))

    # ── Excluded peers ────────────────────────────────────────────────────────
    if excluded:
        elems += _section("Excluded Peers", st)
        for ex in excluded:
            elems.append(Paragraph(
                f'<b>{ex.get("name", "?")}:</b>  {ex.get("reason", "")}',
                st["body_small"],
            ))

    # ── Raw data table ────────────────────────────────────────────────────────
    elems += _section("Raw Financial Data", st)

    if peer_group:
        # Columns: Company | CCY | MCap | EV | Rev | EBIT | EBITDA | FCF | Net Debt | EBIT Mgn | ROE/ROIC
        col_headers = ["Company", "CCY", "MCap\n(B)", "EV\n(B)", "Rev\n(B)",
                       "EBIT\n(B)", "EBITDA\n(B)", "FCF\n(B)", "Net Debt\n(B)",
                       "EBIT\nMgn%", "ROE\n%"]
        # Column widths — total = CW
        cw = CW
        col_w = [cw*0.17, cw*0.05, cw*0.08, cw*0.08, cw*0.08,
                 cw*0.08, cw*0.08, cw*0.08, cw*0.08, cw*0.07, cw*0.07]

        header_row = [Paragraph(h, st["table_header"]) for h in col_headers]

        rows = [header_row]
        for p in peer_group:
            is_subj = p.get("is_subject", False)
            sty_name = st["table_bold"] if is_subj else st["table_cell_l"]
            sty_num  = st["table_bold_r"] if is_subj else st["table_cell"]
            ticker   = p.get("ticker", "?")
            name_str = (p.get("name") or "")[:22]
            label    = f"{ticker} ★" if is_subj else ticker

            row = [
                Paragraph(f"{label}  {name_str}", sty_name),
                Paragraph(p.get("currency") or "—", st["table_cell_c"]),
                Paragraph(_bn(p.get("market_cap_bn")), sty_num),
                Paragraph(_bn(p.get("ev_bn")), sty_num),
                Paragraph(_bn(p.get("revenue_bn")), sty_num),
                Paragraph(_bn(p.get("ebit_bn")), sty_num),
                Paragraph(_bn(p.get("ebitda_bn")), sty_num),
                Paragraph(_bn(p.get("fcf_bn")), sty_num),
                Paragraph(_bn(p.get("net_debt_bn")), sty_num),
                Paragraph(_pct(p.get("ebit_margin_pct")), sty_num),
                Paragraph(_pct(p.get("roe_pct")), sty_num),
            ]
            rows.append(row)

        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        ts = TableStyle([
            # Header
            ("BACKGROUND",  (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",   (0, 0), (-1, 0), white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LGRAY]),
            ("GRID",        (0, 0), (-1, -1), 0.3, BORDER),
            ("LEFTPADDING",  (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING",   (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ])
        # Highlight subject row
        for i, p in enumerate(peer_group, start=1):
            if p.get("is_subject"):
                ts.add("BACKGROUND", (0, i), (-1, i), LBLUE)
        tbl.setStyle(ts)
        elems.append(tbl)

    # ── Valuation yields table ─────────────────────────────────────────────────
    elems += _section("Valuation Yields", st)

    if peer_group:
        col_headers2 = ["Company", "EBIT/EV\n%", "FCF/EV\n%", "Book/\nPrice",
                        "Sales/\nEV", "ROIC/\nROE%", "ND/\nEBITDA", "Int.\nCover",
                        "FCF/\nNI", "Mom.\n6m%"]
        col_w2 = [cw*0.18, cw*0.09, cw*0.09, cw*0.08, cw*0.08,
                  cw*0.09, cw*0.09, cw*0.09, cw*0.08, cw*0.09]

        header_row2 = [Paragraph(h, st["table_header"]) for h in col_headers2]
        rows2 = [header_row2]
        for p in peer_group:
            is_subj = p.get("is_subject", False)
            sty_name = st["table_bold"] if is_subj else st["table_cell_l"]
            sty_num  = st["table_bold_r"] if is_subj else st["table_cell"]
            label    = (p.get("ticker") or "?") + (" ★" if is_subj else "")

            row2 = [
                Paragraph(label, sty_name),
                Paragraph(_pct(p.get("ebit_ev")), sty_num),
                Paragraph(_pct(p.get("fcf_ev")), sty_num),
                Paragraph(_n(p.get("book_price"), 2), sty_num),
                Paragraph(_n(p.get("sales_ev"), 2), sty_num),
                Paragraph(_pct(p.get("roic_pct") or p.get("roe_pct")), sty_num),
                Paragraph(_n(p.get("net_debt_ebitda"), 1, "x"), sty_num),
                Paragraph(_n(p.get("interest_cover"), 1, "x"), sty_num),
                Paragraph(_n(p.get("fcf_net_income"), 2), sty_num),
                Paragraph(_pct(p.get("momentum_6m_pct")), sty_num),
            ]
            rows2.append(row2)

        tbl2 = Table(rows2, colWidths=col_w2, repeatRows=1)
        ts2 = TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",   (0, 0), (-1, 0), white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LGRAY]),
            ("GRID",        (0, 0), (-1, -1), 0.3, BORDER),
            ("LEFTPADDING",  (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING",   (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ])
        for i, p in enumerate(peer_group, start=1):
            if p.get("is_subject"):
                ts2.add("BACKGROUND", (0, i), (-1, i), LBLUE)
        tbl2.setStyle(ts2)
        elems.append(tbl2)

    return elems


# ── Page 2: Scores + Interpretation ──────────────────────────────────────────

def _build_page2(analysis: dict, st: dict) -> list:
    elems = [PageBreak()]
    scores       = analysis.get("scores") or []
    interp       = analysis.get("interpretation") or {}
    subject_tick = analysis.get("subject_ticker", "")

    # ── Score & Rankings table ────────────────────────────────────────────────
    elems += _section("Prudent Value Score — Rankings", st)

    if scores:
        col_headers = ["Rank", "Company", "Cheapness\nScore", "Quality\nScore",
                       "Momentum\nScore", "Total\nScore", "Percentile"]
        col_w = [CW*0.07, CW*0.28, CW*0.13, CW*0.13, CW*0.13, CW*0.13, CW*0.13]
        header_row = [Paragraph(h, st["table_header_l"]) for h in col_headers]
        rows = [header_row]

        sorted_scores = sorted(scores, key=lambda x: x.get("rank") or 999)
        for s in sorted_scores:
            is_subj = s.get("is_subject", False) or s.get("ticker") == subject_tick
            sty_l   = st["table_bold"] if is_subj else st["table_cell_l"]
            sty_c   = st["table_bold_r"] if is_subj else st["table_cell_c"]
            ticker  = s.get("ticker", "?")
            name    = (s.get("name") or "")[:28]
            label   = f"{ticker} ★" if is_subj else ticker

            total   = s.get("total_score")
            cheap   = s.get("cheapness_score")
            qual    = s.get("quality_score")
            mom     = s.get("momentum_score")
            pct     = s.get("percentile")
            rank    = s.get("rank")

            row = [
                Paragraph(str(rank) if rank else "—", sty_c),
                Paragraph(f"{label}  {name}", sty_l),
                Paragraph(_n(cheap, 1), sty_c),
                Paragraph(_n(qual,  1), sty_c),
                Paragraph(_n(mom,   1), sty_c),
                Paragraph(_n(total, 1), sty_c),
                Paragraph(f"{int(pct)}th" if pct else "—", sty_c),
            ]
            rows.append(row)

        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        ts = TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",   (0, 0), (-1, 0), white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LGRAY]),
            ("GRID",        (0, 0), (-1, -1), 0.3, BORDER),
            ("LEFTPADDING",  (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING",   (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ])
        # Highlight subject + colour code total score
        for i, s in enumerate(sorted_scores, start=1):
            is_subj = s.get("is_subject") or s.get("ticker") == subject_tick
            if is_subj:
                ts.add("BACKGROUND", (0, i), (-1, i), LBLUE)
            total = s.get("total_score")
            if total is not None:
                c = _score_color(total)
                ts.add("TEXTCOLOR", (5, i), (5, i), c)
                ts.add("FONTNAME",  (5, i), (5, i), BOLD_FONT)
        tbl.setStyle(ts)
        elems.append(tbl)
        elems.append(Spacer(1, 2 * mm))
        elems.append(Paragraph(
            "Score 0–100 · Cheapness 50% weight · Quality 30% weight · Momentum 10% weight · "
            "All metrics winsorised at 5th/95th percentile · Higher = more attractive.",
            st["body_small"],
        ))

    # ── Verdict banner ────────────────────────────────────────────────────────
    verdict = interp.get("verdict", "")
    verdict_label = _verdict_label(verdict)
    verdict_color = _verdict_color(verdict)
    verdict_expl  = interp.get("verdict_explanation", "")

    elems.append(Spacer(1, 4 * mm))
    verdict_data = [[
        Paragraph(verdict_label, st["verdict_title"]),
        Paragraph(verdict_expl or "", st["verdict_body"]),
    ]]
    verdict_tbl = Table(verdict_data, colWidths=[CW * 0.28, CW * 0.72])
    verdict_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), verdict_color),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 8),
        ("ROUNDEDCORNERS", [4]),
    ]))
    elems.append(verdict_tbl)

    # ── Is it cheap? ──────────────────────────────────────────────────────────
    elems += _section("Cheapness Assessment", st)
    is_cheap    = interp.get("is_cheap", "")
    cheap_reason = interp.get("cheap_reason", "")
    cheap_color = GREEN_HEX if is_cheap == "Yes" else (RED_HEX if is_cheap == "No" else AMBER_HEX)
    elems.append(Paragraph(
        f'<font color="{cheap_color}"><b>Cheap relative to peers: {is_cheap}</b></font>  '
        f'— {cheap_reason}',
        st["body"],
    ))

    # ── Top drivers ───────────────────────────────────────────────────────────
    elems += _section("Top 3 Score Drivers", st)
    for d in (interp.get("top_drivers") or [])[:3]:
        elems.append(Paragraph(f"• {d}", st["bullet"]))

    # ── Top risks ─────────────────────────────────────────────────────────────
    elems += _section("Top 3 Risks to Score", st)
    for r in (interp.get("top_risks") or [])[:3]:
        elems.append(Paragraph(f"• {r}", st["bullet"]))

    return elems


# ── Page 3: Conclusion + Final Summary Table ──────────────────────────────────

def _build_page3(analysis: dict, st: dict, company: CompanyData) -> list:
    elems = [PageBreak()]
    concl  = analysis.get("conclusion") or {}
    scores = analysis.get("scores") or []

    # ── Conclusion banner ─────────────────────────────────────────────────────
    rating = concl.get("rating", "Neutral")
    confidence = concl.get("confidence", "Medium")
    bg_color, txt_color = _rating_colors(rating)

    banner_data = [[
        Paragraph(
            f"<b>{rating.upper()}</b>",
            ParagraphStyle("b_title", fontName=BOLD_FONT, fontSize=16,
                           textColor=txt_color, alignment=TA_CENTER),
        ),
        Paragraph(
            f"<b>Confidence: {confidence}</b>",
            ParagraphStyle("b_conf", fontName=BASE_FONT, fontSize=10,
                           textColor=txt_color, alignment=TA_CENTER),
        ),
    ]]
    banner = Table(banner_data, colWidths=[CW * 0.5, CW * 0.5])
    banner.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, -1), bg_color),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 10),
    ]))
    elems.append(banner)
    elems.append(Spacer(1, 4 * mm))

    # ── Manual checks ─────────────────────────────────────────────────────────
    elems += _section("What to Check Manually Before Acting", st)
    elems.append(Paragraph(concl.get("manual_checks") or "—", st["conclusion_body"]))

    # ── What changes the conclusion ───────────────────────────────────────────
    elems += _section("What Would Change the Conclusion", st)
    elems.append(Paragraph(concl.get("what_changes_conclusion") or "—", st["conclusion_body"]))

    # ── Disclaimer ────────────────────────────────────────────────────────────
    elems.append(Spacer(1, 3 * mm))
    elems.append(Paragraph(
        "The ValueMeter score is a screening tool, not an investment decision. "
        "Cheapness is evidence, not proof. Do not let the model replace judgment.",
        st["body_small"],
    ))

    # ── Final summary table ───────────────────────────────────────────────────
    elems += _section("Summary Table — All Companies", st)

    if scores:
        col_headers = ["Ticker", "CCY", "Price", "Mkt Cap\n(B)",
                       "ROE %", "EBIT Mgn %", "EV/Sales", "Value\nScore", "Rank"]
        col_w = [CW*0.11, CW*0.06, CW*0.09, CW*0.10,
                 CW*0.09, CW*0.11, CW*0.09, CW*0.10, CW*0.07]
        # Remainder for "Name" column
        name_w = CW - sum(col_w)
        col_headers = ["Ticker", "Name"] + col_headers[1:]
        col_w = [CW*0.09, CW*0.19, CW*0.06, CW*0.09, CW*0.10,
                 CW*0.09, CW*0.11, CW*0.09, CW*0.10, CW*0.08]

        header_row = [Paragraph(h, st["table_header"]) for h in col_headers]
        rows = [header_row]

        sorted_scores = sorted(scores, key=lambda x: x.get("rank") or 999)
        subject_tick  = analysis.get("subject_ticker", "")

        for s in sorted_scores:
            is_subj = s.get("is_subject") or s.get("ticker") == subject_tick
            sty_l   = st["table_bold"] if is_subj else st["table_cell_l"]
            sty_r   = st["table_bold_r"] if is_subj else st["table_cell"]
            sty_c   = st["table_cell_c"]

            ticker   = s.get("ticker", "?") + (" ★" if is_subj else "")
            name     = (s.get("name") or "")[:22]
            ccy      = s.get("currency") or "—"
            price    = _n(s.get("price"), 2)
            mcap     = _bn(s.get("market_cap_bn"))
            roe      = _pct(s.get("roe_pct"))
            ebit_mgn = _pct(s.get("ebit_margin_pct"))
            ev_sales = _n(s.get("ev_sales"), 2, "x")
            total    = s.get("total_score")
            rank     = s.get("rank")

            row = [
                Paragraph(ticker, sty_l),
                Paragraph(name, sty_l),
                Paragraph(ccy, sty_c),
                Paragraph(price, sty_r),
                Paragraph(mcap, sty_r),
                Paragraph(roe, sty_r),
                Paragraph(ebit_mgn, sty_r),
                Paragraph(ev_sales, sty_r),
                Paragraph(_n(total, 1), sty_r),
                Paragraph(str(rank) if rank else "—", sty_c),
            ]
            rows.append(row)

        tbl = Table(rows, colWidths=col_w, repeatRows=1)
        ts = TableStyle([
            ("BACKGROUND",  (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR",   (0, 0), (-1, 0), white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, LGRAY]),
            ("GRID",        (0, 0), (-1, -1), 0.3, BORDER),
            ("LEFTPADDING",  (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING",   (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ])
        for i, s in enumerate(sorted_scores, start=1):
            is_subj = s.get("is_subject") or s.get("ticker") == subject_tick
            if is_subj:
                ts.add("BACKGROUND", (0, i), (-1, i), LBLUE)
            total = s.get("total_score")
            if total is not None:
                c = _score_color(total)
                ts.add("TEXTCOLOR", (8, i), (8, i), c)
                ts.add("FONTNAME",  (8, i), (8, i), BOLD_FONT)
        tbl.setStyle(ts)
        elems.append(tbl)

    # ── Weighting legend ──────────────────────────────────────────────────────
    elems.append(Spacer(1, 3 * mm))
    elems.append(Paragraph(
        "Weights: EBIT/EV 20% · FCF/EV 20% · Book/Price 10% · Sales/EV 10% · "
        "ROIC/ROE 15% · Balance sheet safety 10% · FCF conversion 5% · Momentum 10%",
        st["body_small"],
    ))

    return elems


# ── Main renderer ─────────────────────────────────────────────────────────────

class ValueMeterGenerator:
    """Entry point: call .render(company, analysis, output_path)."""

    def render(
        self,
        company: CompanyData,
        analysis: dict,
        output_path: str,
    ) -> str:
        report_date = datetime.now().strftime("%d %b %Y")
        st = _styles()

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"ValueMeter — {company.name or company.ticker}",
            author="EquityBot",
        )

        def _header(canvas, doc_inner):
            _page_header(canvas, doc_inner, company, report_date)

        # ── Build content ──────────────────────────────────────────────────────
        story = []
        story += _build_page1(analysis, st, company)
        story += _build_page2(analysis, st)
        story += _build_page3(analysis, st, company)

        doc.build(story, onFirstPage=_header, onLaterPages=_header)
        logger.info("ValueMeter PDF written: %s", output_path)
        return output_path
