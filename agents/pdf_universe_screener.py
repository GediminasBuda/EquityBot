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
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak,
)

logger = logging.getLogger(__name__)

W, H    = A4
ML = MR = 16 * mm
MT      = 22 * mm
MB      = 14 * mm
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
        "section":  S("sc", fontName=BOLD_FONT, fontSize=9, textColor=white,
                       backColor=NAVY, spaceBefore=10, spaceAfter=4,
                       leftIndent=-2, rightIndent=-2, leading=13),
        "body":     S("b",  fontName=BASE_FONT, fontSize=8.5, textColor=DGRAY,
                       leading=13, alignment=TA_JUSTIFY, spaceAfter=4),
        "small":    S("sm", fontName=BASE_FONT, fontSize=7.5, textColor=MGRAY,
                       leading=10),
        "label":    S("lbl",fontName=BOLD_FONT, fontSize=8, textColor=NAVY,
                       leading=10),
        "caption":  S("cp", fontName=BASE_FONT, fontSize=6.5, textColor=MGRAY,
                       leading=9, alignment=TA_RIGHT),
    }


def _sec(title: str, st: dict) -> Paragraph:
    return Paragraph(f"&nbsp; {title} &nbsp;", st["section"])


def _rule() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=RULE, spaceAfter=3)


def _rec_color(rec: str) -> str:
    r = rec.upper()
    if "ATTRACT" in r: return GREEN_HEX
    if "AVOID"   in r: return RED_HEX
    return AMBER_HEX


# ── PDF generator ─────────────────────────────────────────────────────────────

def render_universe_pdf(
    fw,
    index_ticker: str,
    companies: dict,
    analysis: dict,
    failed: list,
    output_path: str,
) -> None:
    """
    Render the universe screen results to a PDF at `output_path`.
    Uses the same (fw, index_ticker, companies, analysis, failed) inputs as
    _render_universe_html() in models/universe_screener.py.
    """
    st = _styles()
    date      = datetime.now().strftime("%Y-%m-%d")
    picks     = analysis.get("top_picks") or []
    exclusions = analysis.get("exclusions") or []
    groups    = analysis.get("groups") or []
    rec       = _s(analysis.get("recommendation"), "—")
    summary   = _s(analysis.get("universe_summary"), "")
    obs       = _s(analysis.get("framework_observations"), "")

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
        f"{index_ticker}  ·  {len(companies)} companies analysed"
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
    story.append(_sec("Top Picks", st))

    if picks:
        th_style = ParagraphStyle("th", fontName=BOLD_FONT, fontSize=7,
                                  textColor=white, leading=9)
        td_style = ParagraphStyle("td", fontName=BASE_FONT, fontSize=7,
                                  textColor=DGRAY, leading=9)
        td_small = ParagraphStyle("tds", fontName=BASE_FONT, fontSize=6.5,
                                  textColor=MGRAY, leading=8.5)

        header = [
            Paragraph("#",          th_style),
            Paragraph("Ticker",     th_style),
            Paragraph("Company",    th_style),
            Paragraph("Score",      th_style),
            Paragraph("Key Advantage / Thesis",  th_style),
            Paragraph("Rationale",  th_style),
        ]
        rows = [header]
        for p in picks:
            score_str = _s(p.get("score"), "—")
            sc = _score_color(score_str)
            moat = _s(p.get("choke_point_or_moat") or p.get("moat") or
                      p.get("unavoidable_flow_or_thesis"), "")
            rationale = _s(p.get("rationale"), "")
            rows.append([
                Paragraph(str(p.get("rank", "")), td_style),
                Paragraph(f"<b>{_s(p.get('ticker'))}</b>", td_style),
                Paragraph(_s(p.get("name"))[:30], td_small),
                Paragraph(
                    f'<font color="{sc}"><b>{score_str}</b></font>',
                    td_style,
                ),
                Paragraph(moat[:80], td_small),
                Paragraph(rationale[:200], td_small),
            ])

        col_w = [8*mm, 16*mm, 35*mm, 13*mm, 45*mm, CW - 8-16-35-13-45-3*mm]
        t = Table(rows, colWidths=col_w, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND",     (0,0), (-1,0), NAVY),
            ("ROWBACKGROUNDS",  (0,1), (-1,-1), [white, LGRAY]),
            ("VALIGN",          (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",      (0,0), (-1,-1), 2),
            ("BOTTOMPADDING",   (0,0), (-1,-1), 2),
            ("GRID",            (0,0), (-1,-1), 0.3, BORDER),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No top picks returned by the LLM.", st["small"]))
    story.append(Spacer(1, 6))

    # ── Groups ────────────────────────────────────────────────────────────────
    if groups:
        story.append(_sec("Company Groups", st))
        for g in groups:
            gname = _s(g.get("group_name"), "Group")
            tickers_str = ", ".join(g.get("tickers") or [])
            grational = _s(g.get("group_rationale"), "")
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
        story.append(_sec("Framework Observations", st))
        story.append(Paragraph(obs, st["body"]))
        story.append(Spacer(1, 4))

    # ── Exclusions ────────────────────────────────────────────────────────────
    if exclusions:
        story.append(_sec(f"Exclusions ({len(exclusions)})", st))
        ex_th = ParagraphStyle("eth", fontName=BOLD_FONT, fontSize=6.5,
                               textColor=white, leading=9)
        ex_td = ParagraphStyle("etd", fontName=BASE_FONT, fontSize=6.5,
                               textColor=MGRAY, leading=9)
        ex_hdr = [
            Paragraph("Ticker", ex_th),
            Paragraph("Company", ex_th),
            Paragraph("Reason", ex_th),
        ]
        ex_rows = [ex_hdr]
        for e in exclusions:
            ex_rows.append([
                Paragraph(_s(e.get("ticker")), ex_td),
                Paragraph(_s(e.get("name"))[:28], ex_td),
                Paragraph(_s(e.get("reason"))[:120], ex_td),
            ])
        ex_cw = [18*mm, 35*mm, CW - 53*mm]
        et = Table(ex_rows, colWidths=ex_cw, repeatRows=1)
        et.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), MGRAY),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [white, LGRAY]),
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",    (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("GRID",          (0,0), (-1,-1), 0.3, BORDER),
        ]))
        story.append(et)
        story.append(Spacer(1, 6))

    # ── Company data snapshot table ───────────────────────────────────────────
    if companies:
        story.append(PageBreak())
        story.append(_sec("Company Data Snapshot (EODHD / yfinance)", st))

        snap_th = ParagraphStyle("sth", fontName=BOLD_FONT, fontSize=6,
                                 textColor=white, leading=8)
        snap_td = ParagraphStyle("std", fontName=BASE_FONT, fontSize=6,
                                 textColor=DGRAY, leading=8)

        def _fv(v, mult=1, dec=1, sfx=""):
            if v is None: return "—"
            try: return f"{float(v)*mult:.{dec}f}{sfx}"
            except: return "—"

        snap_hdr = [Paragraph(h, snap_th) for h in
                    ["#", "Ticker", "Name", "Sector", "MCap B",
                     "Rev B", "Net M%", "ROE%", "P/E", "EV/EBIT"]]
        snap_rows = [snap_hdr]
        for i, (ticker, cd) in enumerate(companies.items(), 1):
            la = cd.latest_annual() if hasattr(cd, "latest_annual") else None
            snap_rows.append([
                Paragraph(str(i), snap_td),
                Paragraph(f"<b>{ticker}</b>", snap_td),
                Paragraph((cd.name or "")[:22], snap_td),
                Paragraph((cd.sector or "")[:14], snap_td),
                Paragraph(_fv(cd.market_cap, 1/1e3), snap_td),
                Paragraph(_fv(la.revenue if la else None, 1/1e3), snap_td),
                Paragraph(_fv(la.net_margin if la else None, 100, sfx="%"), snap_td),
                Paragraph(_fv(la.roe if la else None, 100, sfx="%"), snap_td),
                Paragraph(_fv(cd.pe_ratio, sfx="×"), snap_td),
                Paragraph(_fv(cd.ev_ebit, sfx="×"), snap_td),
            ])
        snap_cw = [7*mm, 15*mm, 35*mm, 27*mm, 15*mm, 15*mm,
                   15*mm, 14*mm, 13*mm, CW - 156*mm]
        st_tbl = Table(snap_rows, colWidths=snap_cw, repeatRows=1)
        st_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), NAVY),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [white, LGRAY]),
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",    (0,0), (-1,-1), 1),
            ("BOTTOMPADDING", (0,0), (-1,-1), 1),
            ("GRID",          (0,0), (-1,-1), 0.3, BORDER),
        ]))
        story.append(st_tbl)

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(_rule())
    story.append(Paragraph(
        f"Framework: {fw.name}  ·  Universe: {index_ticker}  ·  "
        f"Companies: {len(companies)}  ·  Generated: {date}  ·  EquityBot",
        st["caption"],
    ))

    doc.build(story)
    logger.info(f"[pdf_universe] Saved: {output_path}")
