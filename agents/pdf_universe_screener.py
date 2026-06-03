"""
pdf_universe_screener.py — ReportLab PDF renderer for Universe Screen results.

Replaces the HTML report produced by models/universe_screener.py with a
professional PDF using the same colour palette as the rest of EquityBot.

Input: the same (fw, index_ticker, companies, analysis, failed) tuple that
_render_universe_html() accepts.
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak,
)

logger = logging.getLogger(__name__)

W, H    = A4
ML = MR = 16 * mm
MT      = 22 * mm
MB      = 18 * mm          # extra space for footer line
CW      = W - ML - MR

NAVY   = HexColor("#003F54")
BLUE   = HexColor("#1B3F6E")
TEAL   = HexColor("#1A6E5A")
GREEN  = HexColor("#1A7E3D")
RED    = HexColor("#C0392B")
AMBER  = HexColor("#D68910")
DGRAY  = HexColor("#333333")
MGRAY  = HexColor("#666666")
LGRAY  = HexColor("#F5F5F5")
BORDER = HexColor("#CCCCCC")
RULE   = HexColor("#DDDDDD")

NAVY_HEX  = "#003F54"
GREEN_HEX = "#1A7E3D"
RED_HEX   = "#C0392B"
AMBER_HEX = "#D68910"
MGRAY_HEX = "#666666"

BASE_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"


# ── Formatters ────────────────────────────────────────────────────────────────
def _s(v: Any, fallback: str = "—") -> str:
    if v is None or str(v).strip() in ("", "None", "null"): return fallback
    return str(v).strip()

def _score_color(score_str: str) -> str:
    try:
        s = float(score_str)
        if s >= 7.5: return GREEN_HEX
        if s >= 5.0: return AMBER_HEX
        return RED_HEX
    except: return MGRAY_HEX


# ── Styles ────────────────────────────────────────────────────────────────────
def _styles() -> dict:
    def S(name, **kw): return ParagraphStyle(name, **kw)
    return {
        "title":    S("t",  fontName=BOLD_FONT, fontSize=16, textColor=NAVY,
                       spaceAfter=3, leading=20),
        "subtitle": S("st", fontName=BASE_FONT, fontSize=9, textColor=MGRAY,
                       spaceAfter=8, leading=12),
        "section":  S("sc", fontName=BOLD_FONT, fontSize=9, textColor=NAVY,
                       spaceBefore=10, spaceAfter=2, leading=13),
        "body":     S("b",  fontName=BASE_FONT, fontSize=8.5, textColor=DGRAY,
                       leading=13, alignment=TA_JUSTIFY, spaceAfter=4),
        "small":    S("sm", fontName=BASE_FONT, fontSize=7.5, textColor=MGRAY,
                       leading=10),
        "label":    S("lbl",fontName=BOLD_FONT, fontSize=8, textColor=NAVY,
                       leading=10),
        "caption":  S("cp", fontName=BASE_FONT, fontSize=6.5, textColor=MGRAY,
                       leading=9, alignment=TA_CENTER),
        # Ink-saving table headers: navy text on white
        "th":       S("th", fontName=BOLD_FONT, fontSize=7,
                       textColor=NAVY, leading=9),
        "th_sm":    S("thsm", fontName=BOLD_FONT, fontSize=6.5,
                       textColor=NAVY, leading=8),
        "th_xs":    S("thxs", fontName=BOLD_FONT, fontSize=6,
                       textColor=NAVY, leading=8),
        "td":       S("td", fontName=BASE_FONT, fontSize=7,
                       textColor=DGRAY, leading=9),
        "td_sm":    S("tdsm", fontName=BASE_FONT, fontSize=6.5,
                       textColor=MGRAY, leading=8.5),
        "td_xs":    S("tdxs", fontName=BASE_FONT, fontSize=6,
                       textColor=DGRAY, leading=8),
    }


# ── Section header: navy text + navy rule (no background fill) ───────────────
def _sec(title: str, st: dict) -> list:
    return [
        Paragraph(title, st["section"]),
        HRFlowable(width="100%", thickness=1.0, color=NAVY, spaceAfter=4),
    ]


def _rule() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=3)


def _rec_color(rec: str) -> str:
    r = rec.upper()
    if "ATTRACT" in r: return GREEN_HEX
    if "AVOID"   in r: return RED_HEX
    return AMBER_HEX


# ── Per-page footer ───────────────────────────────────────────────────────────
def _draw_footer(canvas, doc, fw_name: str, index_ticker: str,
                 company_count: int, date: str) -> None:
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(ML, MB - 4*mm, W - MR, MB - 4*mm)
    canvas.setFont(BOLD_FONT, 7)
    canvas.setFillColor(NAVY)
    canvas.drawString(ML, MB - 7.5*mm, "@EquityOracle")
    canvas.setFont(BASE_FONT, 6.5)
    canvas.setFillColor(MGRAY)
    canvas.drawCentredString(
        W / 2, MB - 7.5*mm,
        f"{fw_name}  ·  {index_ticker}  ·  {company_count} companies  ·  {date}",
    )
    canvas.drawRightString(W - MR, MB - 7.5*mm, f"Page {doc.page}")
    canvas.restoreState()


# ── Table style helpers ───────────────────────────────────────────────────────
def _tbl_style(extra: list | None = None) -> TableStyle:
    """Base ink-saving table style: white header row with navy underline."""
    cmds = [
        ("BACKGROUND",    (0, 0), (-1,  0), white),
        ("TEXTCOLOR",     (0, 0), (-1,  0), NAVY),
        ("LINEBELOW",     (0, 0), (-1,  0), 1.2, NAVY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LGRAY]),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("GRID",          (0, 0), (-1, -1), 0.3, BORDER),
    ]
    if extra:
        cmds += extra
    return TableStyle(cmds)


# ── PDF generator ─────────────────────────────────────────────────────────────

def render_universe_pdf(
    fw,
    index_ticker: str,
    companies: dict,
    analysis: dict,
    failed: list,
    output_path: str,
) -> None:
    st = _styles()
    date         = datetime.now().strftime("%Y-%m-%d")
    picks        = analysis.get("top_picks") or []
    exclusions   = analysis.get("exclusions") or []
    groups       = analysis.get("groups") or []
    rec          = _s(analysis.get("recommendation"), "—")
    summary      = _s(analysis.get("universe_summary"), "")
    obs          = _s(analysis.get("framework_observations"), "")
    company_count = len(companies)

    def on_page(canvas, doc):
        _draw_footer(canvas, doc, fw.name, index_ticker, company_count, date)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=ML, rightMargin=MR,
        topMargin=MT,  bottomMargin=MB,
        title=f"Universe Screen — {index_ticker} · {fw.name}",
    )

    story = []

    # ── Title ─────────────────────────────────────────────────────────────────
    story.append(Paragraph(f"{fw.name} Universe Screen", st["title"]))
    story.append(Paragraph(
        f"{index_ticker}  ·  {company_count} companies analysed"
        + (f"  ·  {len(failed)} skipped" if failed else "")
        + f"  ·  {date}",
        st["subtitle"],
    ))
    story.append(_rule())

    # ── Recommendation banner ─────────────────────────────────────────────────
    rec_col = _rec_color(rec)
    story.append(Paragraph(
        f'<font color="{rec_col}"><b>{rec}</b></font>',
        ParagraphStyle("rec", fontName=BOLD_FONT, fontSize=13,
                       textColor=HexColor(rec_col), spaceAfter=4, leading=17),
    ))

    # ── Universe summary ──────────────────────────────────────────────────────
    if summary:
        story.append(Paragraph(summary, st["body"]))
    story.append(Spacer(1, 4))

    # ── Top Picks table ───────────────────────────────────────────────────────
    story += _sec("Top Picks", st)

    if picks:
        header = [
            Paragraph("#",                       st["th"]),
            Paragraph("Ticker",                  st["th"]),
            Paragraph("Company",                 st["th"]),
            Paragraph("Score",                   st["th"]),
            Paragraph("Key Advantage / Thesis",  st["th"]),
            Paragraph("Rationale",               st["th"]),
        ]
        rows = [header]
        for p in picks:
            score_str = _s(p.get("score"), "—")
            sc = _score_color(score_str)
            moat = _s(p.get("choke_point_or_moat") or p.get("moat") or
                      p.get("unavoidable_flow_or_thesis"), "")
            rationale = _s(p.get("rationale"), "")
            rows.append([
                Paragraph(str(p.get("rank", "")),             st["td"]),
                Paragraph(f"<b>{_s(p.get('ticker'))}</b>",    st["td"]),
                Paragraph(_s(p.get("name"))[:30],             st["td_sm"]),
                Paragraph(f'<font color="{sc}"><b>{score_str}</b></font>', st["td"]),
                Paragraph(moat[:80],                          st["td_sm"]),
                Paragraph(rationale[:200],                    st["td_sm"]),
            ])

        col_w = [8*mm, 16*mm, 35*mm, 13*mm, 45*mm, CW - (8+16+35+13+45)*mm]
        story.append(Table(rows, colWidths=col_w, repeatRows=1,
                           style=_tbl_style()))
    else:
        story.append(Paragraph("No top picks returned by the LLM.", st["small"]))
    story.append(Spacer(1, 6))

    # ── Groups ────────────────────────────────────────────────────────────────
    if groups:
        story += _sec("Company Groups", st)
        for g in groups:
            gname       = _s(g.get("group_name"), "Group")
            tickers_str = ", ".join(g.get("tickers") or [])
            grational   = _s(g.get("group_rationale"), "")
            story.append(Paragraph(
                f"<b>{gname}</b>  ·  {tickers_str}",
                ParagraphStyle("gn", fontName=BOLD_FONT, fontSize=8,
                               textColor=NAVY, leading=11),
            ))
            if grational:
                story.append(Paragraph(grational, st["small"]))
            story.append(Spacer(1, 3))
        story.append(Spacer(1, 4))

    # ── Framework observations ────────────────────────────────────────────────
    if obs:
        story += _sec("Framework Observations", st)
        story.append(Paragraph(obs, st["body"]))
        story.append(Spacer(1, 4))

    # ── Exclusions ────────────────────────────────────────────────────────────
    if exclusions:
        story += _sec(f"Exclusions ({len(exclusions)})", st)
        ex_hdr = [
            Paragraph("Ticker",  st["th_sm"]),
            Paragraph("Company", st["th_sm"]),
            Paragraph("Reason",  st["th_sm"]),
        ]
        ex_rows = [ex_hdr]
        for e in exclusions:
            ex_rows.append([
                Paragraph(_s(e.get("ticker")),         st["td_sm"]),
                Paragraph(_s(e.get("name"))[:28],      st["td_sm"]),
                Paragraph(_s(e.get("reason"))[:120],   st["td_sm"]),
            ])
        ex_cw = [18*mm, 35*mm, CW - 53*mm]
        story.append(Table(ex_rows, colWidths=ex_cw, repeatRows=1,
                           style=_tbl_style()))
        story.append(Spacer(1, 6))

    # ── Company data snapshot table ───────────────────────────────────────────
    if companies:
        story.append(PageBreak())
        story += _sec("Company Data Snapshot (EODHD / yfinance)", st)

        def _fv(v, mult=1, dec=1, sfx=""):
            if v is None: return "—"
            try: return f"{float(v)*mult:.{dec}f}{sfx}"
            except: return "—"

        snap_hdr = [Paragraph(h, st["th_xs"]) for h in
                    ["#", "Ticker", "Name", "Sector", "MCap B",
                     "Rev B", "Net M%", "ROE%", "P/E", "EV/EBIT"]]
        snap_rows = [snap_hdr]
        for i, (ticker, cd) in enumerate(companies.items(), 1):
            la = cd.latest_annual() if hasattr(cd, "latest_annual") else None
            snap_rows.append([
                Paragraph(str(i),                     st["td_xs"]),
                Paragraph(f"<b>{ticker}</b>",         st["td_xs"]),
                Paragraph((cd.name   or "")[:22],     st["td_xs"]),
                Paragraph((cd.sector or "")[:14],     st["td_xs"]),
                Paragraph(_fv(cd.market_cap, 1/1e3),  st["td_xs"]),
                Paragraph(_fv(la.revenue    if la else None, 1/1e3), st["td_xs"]),
                Paragraph(_fv(la.net_margin if la else None, 100, sfx="%"), st["td_xs"]),
                Paragraph(_fv(la.roe        if la else None, 100, sfx="%"), st["td_xs"]),
                Paragraph(_fv(cd.pe_ratio,  sfx="×"), st["td_xs"]),
                Paragraph(_fv(cd.ev_ebit,   sfx="×"), st["td_xs"]),
            ])
        snap_cw = [7*mm, 15*mm, 35*mm, 27*mm, 15*mm, 15*mm,
                   15*mm, 14*mm, 13*mm, CW - 156*mm]
        story.append(Table(snap_rows, colWidths=snap_cw, repeatRows=1,
                           style=_tbl_style()))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    logger.info(f"[pdf_universe] Saved: {output_path}")
