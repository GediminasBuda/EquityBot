"""
pdf_valuemeter.py — ReportLab PDF renderer for the ValueMeter model.

Design: matches EODHD Direct style — white background, navy text,
thin rule below table headers, no coloured background blocks.

Layout (2 pages):
  Page 1: Header + Peer group + Raw financial data + Valuation yields
  Page 2: Score rankings + Verdict + Interpretation + Conclusion + Summary table
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

from config import LLM_MODEL
from data_sources.base import CompanyData

logger = logging.getLogger(__name__)

# ── Page geometry ─────────────────────────────────────────────────────────────
W, H  = A4
ML = MR = 15 * mm
MT     = 28 * mm
MB     = 12 * mm
CW     = W - ML - MR

# ── Colour palette (EODHD Direct style) ──────────────────────────────────────
NAVY   = HexColor("#003F54")
DGRAY  = HexColor("#333333")
MGRAY  = HexColor("#666666")
CGRAY  = HexColor("#999999")
GREEN  = HexColor("#1A7E3D")
RED    = HexColor("#C0392B")
ORANGE = HexColor("#C9843E")
RULE   = HexColor("#DDDDDD")
BORDER = HexColor("#CCCCCC")
LGRAY  = HexColor("#F5F5F5")

# Plain hex strings for Paragraph XML markup
NAVY_HEX  = "#003F54"
GREEN_HEX = "#1A7E3D"
RED_HEX   = "#C0392B"
MGRAY_HEX = "#666666"
AMBER_HEX = "#C9843E"

BF = "Helvetica-Bold"
NF = "Helvetica"


# ── Styles ────────────────────────────────────────────────────────────────────

def _styles() -> dict:
    def S(name, **kw):
        return ParagraphStyle(name, **kw)
    return {
        "section":  S("sec",  fontName=BF, fontSize=9, textColor=NAVY,
                      spaceBefore=7, spaceAfter=2, leading=11),
        "body":     S("body", fontName=NF, fontSize=8, textColor=DGRAY,
                      leading=11, alignment=TA_JUSTIFY, spaceAfter=3),
        "small":    S("sml",  fontName=NF, fontSize=7, textColor=MGRAY,
                      leading=9),
        "tiny":     S("tin",  fontName=NF, fontSize=6.5, textColor=CGRAY,
                      leading=8),
        # Table header: navy text, left or centre aligned — white background
        "th":       S("th",   fontName=BF, fontSize=7, textColor=NAVY,
                      alignment=TA_CENTER, leading=9),
        "thl":      S("thl",  fontName=BF, fontSize=7, textColor=NAVY,
                      alignment=TA_LEFT,   leading=9),
        # Table cells
        "td":       S("td",   fontName=NF, fontSize=7, textColor=DGRAY,
                      alignment=TA_RIGHT,  leading=9),
        "tdl":      S("tdl",  fontName=NF, fontSize=7, textColor=DGRAY,
                      alignment=TA_LEFT,   leading=9),
        "tdc":      S("tdc",  fontName=NF, fontSize=7, textColor=DGRAY,
                      alignment=TA_CENTER, leading=9),
        # Subject row — navy bold
        "tdb":      S("tdb",  fontName=BF, fontSize=7, textColor=NAVY,
                      alignment=TA_LEFT,   leading=9),
        "tdbr":     S("tdbr", fontName=BF, fontSize=7, textColor=NAVY,
                      alignment=TA_RIGHT,  leading=9),
        "tdbc":     S("tdbc", fontName=BF, fontSize=7, textColor=NAVY,
                      alignment=TA_CENTER, leading=9),
        "bullet":   S("bul",  fontName=NF, fontSize=8, textColor=DGRAY,
                      leading=11, leftIndent=10, spaceAfter=2),
        "verdict":  S("ver",  fontName=BF, fontSize=9, textColor=NAVY,
                      leading=11, spaceBefore=2, spaceAfter=2),
        "rating":   S("rat",  fontName=BF, fontSize=11, textColor=NAVY,
                      leading=14),
        "lbl":      S("lbl",  fontName=NF, fontSize=8, textColor=MGRAY,
                      leading=10),
        "val":      S("val",  fontName=BF, fontSize=8, textColor=DGRAY,
                      alignment=TA_RIGHT, leading=10),
    }


# ── Formatters ────────────────────────────────────────────────────────────────

def _f(v, dec=1, suf="") -> str:
    if v is None: return "—"
    try:    return f"{float(v):,.{dec}f}{suf}"
    except: return "—"

def _pct(v) -> str: return _f(v, 1, "%")
def _bn(v)  -> str: return _f(v, 1, "B")
def _x(v)   -> str: return _f(v, 2, "x")


def _score_color_hex(score) -> str:
    try:
        s = float(score)
        if s >= 65: return GREEN_HEX
        if s >= 40: return AMBER_HEX
        return RED_HEX
    except: return MGRAY_HEX

def _verdict_label(verdict: str) -> str:
    return {
        "value_opportunity":        "Value Opportunity",
        "value_trap":               "Value Trap",
        "cyclical_mirage":          "Cyclical Mirage",
        "fairly_priced_compounder": "Fairly Priced Compounder",
    }.get((verdict or "").lower().strip(), (verdict or "Unknown"))

def _rating_hex(rating: str) -> str:
    r = (rating or "").lower()
    if r == "attractive":   return GREEN_HEX
    if r == "unattractive": return RED_HEX
    return AMBER_HEX


# ── Standard data table (EODHD Direct style) ──────────────────────────────────
# White background, navy text in header, 1.2pt navy rule below header,
# 0.25pt gray rule between rows.

def _data_table(rows: list, col_widths: list,
                subject_rows: list[int] | None = None) -> Table:
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    ts_cmds = [
        ("BACKGROUND",    (0, 0), (-1, -1), white),
        ("FONTNAME",      (0, 0), (-1, -1), NF),
        ("FONTSIZE",      (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        # Header: navy bold, thick rule below
        ("FONTNAME",      (0, 0), (-1, 0),  BF),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  NAVY),
        ("LINEBELOW",     (0, 0), (-1, 0),  1.2, NAVY),
        # Thin gray lines between data rows
        ("LINEBELOW",     (0, 1), (-1, -2), 0.25, BORDER),
    ]
    if subject_rows:
        for r in subject_rows:
            ts_cmds += [
                ("FONTNAME", (0, r), (-1, r), BF),
                ("TEXTCOLOR",(0, r), (-1, r), NAVY),
            ]
    t.setStyle(TableStyle(ts_cmds))
    return t


# ── Page header / footer ──────────────────────────────────────────────────────

def _draw_header(canvas, doc, company: CompanyData, report_date: str) -> None:
    canvas.saveState()

    NAME_Y     = H - 11 * mm
    SUBTITLE_Y = H - 17 * mm
    LINE_Y     = H - 21 * mm

    # Row 1: company name (left) | price (right)
    canvas.setFont(BF, 14)
    canvas.setFillColor(NAVY)
    canvas.drawString(ML, NAME_Y, company.name or company.ticker)

    ccy = company.currency_price or company.currency or ""
    price = company.current_price
    price_str = (f"Price: {price:,.2f} {ccy}".strip() if price else "Price n/a")
    canvas.setFont(BF, 8.5)
    canvas.drawRightString(W - MR, NAME_Y, price_str)

    # Row 2: subtitle (left) | mcap (right)
    subtitle = " | ".join(filter(None, [
        "ValueMeter",
        company.sector, company.country,
        company.exchange, company.ticker, report_date,
    ]))
    canvas.setFont(NF, 8)
    canvas.setFillColor(MGRAY)
    canvas.drawString(ML, SUBTITLE_Y, subtitle)

    mcap_str = (f"MCap: {company.market_cap/1e9:,.2f}B {company.currency or ''}"
                if company.market_cap else "")
    canvas.setFont(NF, 8)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(W - MR, SUBTITLE_Y, mcap_str)

    # Separator line
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.8)
    canvas.line(ML, LINE_Y, W - MR, LINE_Y)

    # Footer
    canvas.setFont(NF, 7)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(W - MR, 7 * mm,
        f"Page {doc.page}  |  Your Humble EquityBot  |  {LLM_MODEL}")
    canvas.restoreState()


def _sec(title: str, st: dict) -> list:
    return [
        Spacer(1, 3*mm),
        Paragraph(title.upper(), st["section"]),
        HRFlowable(width=CW, thickness=0.4, color=RULE, spaceAfter=2),
    ]


# ── Page 1 ────────────────────────────────────────────────────────────────────

def _page1(analysis: dict, st: dict) -> list:
    elems = []
    peers    = analysis.get("peer_group") or []
    excluded = analysis.get("excluded_peers") or []

    # ── Peer group: included ──────────────────────────────────────────────────
    elems += _sec("Peer Group — Included", st)
    for p in peers:
        is_s   = p.get("is_subject", False)
        ticker = p.get("ticker", "?")
        name   = p.get("name", "")
        reason = p.get("reason_included") or p.get("reason", "")
        star   = " ★" if is_s else ""
        color  = NAVY_HEX if is_s else "#333333"
        elems.append(Paragraph(
            f'<font color="{color}"><b>{ticker}{star}  {name}</b></font>'
            f'<font color="{MGRAY_HEX}"> — {reason}</font>',
            st["small"],
        ))

    # ── Excluded peers ────────────────────────────────────────────────────────
    if excluded:
        elems += _sec("Excluded Peers", st)
        for ex in excluded:
            elems.append(Paragraph(
                f'<b>{ex.get("name","?")}:</b>  {ex.get("reason","")}',
                st["small"],
            ))

    # ── Raw financial data ────────────────────────────────────────────────────
    elems += _sec("Raw Financial Data (EODHD)", st)
    if peers:
        subj_rows = [i+1 for i, p in enumerate(peers) if p.get("is_subject")]
        hdrs = ["Company", "CCY", "MCap B", "EV B", "Rev B",
                "EBIT B", "EBITDA B", "FCF B", "Net Debt B", "EBIT Mgn%", "ROE%"]
        cw = [CW*.17, CW*.05, CW*.08, CW*.08, CW*.07,
              CW*.07, CW*.08, CW*.07, CW*.09, CW*.08, CW*.07]
        header = [Paragraph(h, st["th"]) for h in hdrs]
        header[0] = Paragraph(hdrs[0], st["thl"])
        rows = [header]
        for p in peers:
            is_s = p.get("is_subject", False)
            sl = st["tdb"]  if is_s else st["tdl"]
            sr = st["tdbr"] if is_s else st["td"]
            t  = (p.get("ticker") or "?") + (" ★" if is_s else "")
            rows.append([
                Paragraph(f"{t} {(p.get('name') or '')[:16]}", sl),
                Paragraph(p.get("currency") or "—", st["tdc"]),
                Paragraph(_bn(p.get("market_cap_bn")), sr),
                Paragraph(_bn(p.get("ev_bn")), sr),
                Paragraph(_bn(p.get("revenue_bn")), sr),
                Paragraph(_bn(p.get("ebit_bn")), sr),
                Paragraph(_bn(p.get("ebitda_bn")), sr),
                Paragraph(_bn(p.get("fcf_bn")), sr),
                Paragraph(_bn(p.get("net_debt_bn")), sr),
                Paragraph(_pct(p.get("ebit_margin_pct")), sr),
                Paragraph(_pct(p.get("roe_pct")), sr),
            ])
        elems.append(_data_table(rows, cw, subj_rows))

    # ── Valuation yields ──────────────────────────────────────────────────────
    elems += _sec("Valuation Yields", st)
    if peers:
        subj_rows = [i+1 for i, p in enumerate(peers) if p.get("is_subject")]
        hdrs2 = ["Company", "EBIT/EV %", "FCF/EV %", "Book/Price",
                 "Sales/EV", "ROIC/ROE%", "ND/EBITDA", "Int. Cover",
                 "FCF/NI", "Mom. 6m%"]
        cw2 = [CW*.17, CW*.09, CW*.09, CW*.09, CW*.09,
               CW*.10, CW*.09, CW*.09, CW*.09, CW*.09]
        header2 = [Paragraph(h, st["th"]) for h in hdrs2]
        header2[0] = Paragraph(hdrs2[0], st["thl"])
        rows2 = [header2]
        for p in peers:
            is_s = p.get("is_subject", False)
            sl = st["tdb"]  if is_s else st["tdl"]
            sr = st["tdbr"] if is_s else st["td"]
            t  = (p.get("ticker") or "?") + (" ★" if is_s else "")
            rows2.append([
                Paragraph(t, sl),
                Paragraph(_pct(p.get("ebit_ev")), sr),
                Paragraph(_pct(p.get("fcf_ev")), sr),
                Paragraph(_f(p.get("book_price"), 3), sr),
                Paragraph(_f(p.get("sales_ev"), 3), sr),
                Paragraph(_pct(p.get("quality_return_pct") or p.get("roe_pct")), sr),
                Paragraph(_f(p.get("net_debt_ebitda"), 1, "x"), sr),
                Paragraph(_f(p.get("interest_cover"), 1, "x"), sr),
                Paragraph(_f(p.get("fcf_net_income"), 2), sr),
                Paragraph(_pct(p.get("momentum_6m_pct")), sr),
            ])
        elems.append(_data_table(rows2, cw2, subj_rows))

    return elems


# ── Page 2 ────────────────────────────────────────────────────────────────────

def _page2(analysis: dict, st: dict) -> list:
    elems = [PageBreak()]
    scores  = sorted(analysis.get("scores") or [], key=lambda x: x.get("rank") or 999)
    interp  = analysis.get("interpretation") or {}
    concl   = analysis.get("conclusion") or {}
    sub_t   = analysis.get("subject_ticker", "")

    # ── Score rankings ────────────────────────────────────────────────────────
    elems += _sec("Prudent Value Score — Rankings", st)
    if scores:
        subj_rows = [i+1 for i, s in enumerate(scores)
                     if s.get("is_subject") or s.get("ticker") == sub_t]
        hdrs = ["Rank", "Company", "Cheapness (0-100)", "Quality (0-100)",
                "Momentum (0-100)", "TOTAL SCORE", "Percentile"]
        cw = [CW*.06, CW*.30, CW*.13, CW*.13, CW*.13, CW*.13, CW*.12]
        header = [Paragraph(h, st["thl"] if i <= 1 else st["th"])
                  for i, h in enumerate(hdrs)]
        rows = [header]
        for s in scores:
            is_s = s.get("is_subject") or s.get("ticker") == sub_t
            sl   = st["tdb"]  if is_s else st["tdl"]
            sr   = st["tdbr"] if is_s else st["td"]
            sc   = st["tdbc"] if is_s else st["tdc"]
            total = s.get("total_score")
            pct   = s.get("percentile")
            # Ordinal suffix
            def _ord(n):
                if n is None: return "—"
                n = int(n)
                if 11 <= n % 100 <= 13: return f"{n}th"
                return f"{n}{['th','st','nd','rd','th'][min(n%10,4)]}"
            rows.append([
                Paragraph(str(s.get("rank") or "—"), sc),
                Paragraph(
                    f'{s.get("ticker","?")}{"★" if is_s else ""}  '
                    f'{(s.get("name") or "")[:28]}',
                    sl,
                ),
                Paragraph(_f(s.get("cheapness_score"), 1), sr),
                Paragraph(_f(s.get("quality_score"),   1), sr),
                Paragraph(_f(s.get("momentum_score"),  1), sr),
                Paragraph(
                    f'<font color="{_score_color_hex(total)}"><b>{_f(total,1)}</b></font>'
                    if total is not None else "—",
                    sr,
                ),
                Paragraph(_ord(pct), sc),
            ])
        elems.append(_data_table(rows, cw, subj_rows))
        elems.append(Spacer(1, 1*mm))
        elems.append(Paragraph(
            "Weights: EBIT/EV 20% · FCF/EV 20% · Book/Price 10% · Sales/EV 10% · "
            "ROIC/ROE 15% · Balance sheet 10% · FCF/NI 5% · Momentum 10%",
            st["tiny"],
        ))

    # ── Verdict ───────────────────────────────────────────────────────────────
    verdict  = interp.get("verdict", "")
    v_label  = _verdict_label(verdict)
    v_expl   = interp.get("verdict_explanation", "")
    is_cheap = interp.get("is_cheap", "")
    chp_hex  = GREEN_HEX if is_cheap == "Yes" else (RED_HEX if is_cheap == "No" else AMBER_HEX)

    elems += _sec("Verdict", st)
    elems.append(Paragraph(
        f'<font color="{NAVY_HEX}"><b>{v_label}</b></font>  —  {v_expl}',
        st["body"],
    ))

    # ── Interpretation ────────────────────────────────────────────────────────
    elems += _sec("Interpretation", st)
    elems.append(Paragraph(
        f'<font color="{chp_hex}"><b>Cheap vs. peers: {is_cheap}</b></font>'
        f' — {interp.get("cheap_reason", "")}',
        st["body"],
    ))

    # Drivers + Risks as two-column table
    drivers = (interp.get("top_drivers") or [])[:3]
    risks   = (interp.get("top_risks")   or [])[:3]
    dr_text = "<b>Top 3 score drivers</b><br/>" + "<br/>".join(f"• {d}" for d in drivers)
    ri_text = "<b>Top 3 risks to score</b><br/>"  + "<br/>".join(f"• {r}" for r in risks)
    side = Table(
        [[Paragraph(dr_text, st["small"]), Paragraph(ri_text, st["small"])]],
        colWidths=[CW*.5, CW*.5],
    )
    side.setStyle(TableStyle([
        ("VALIGN",      (0,0),(-1,-1), "TOP"),
        ("LEFTPADDING", (0,0),(-1,-1), 0),
        ("RIGHTPADDING",(0,0),(0,-1),  6),
        ("LINEAFTER",   (0,0),(0,-1),  0.4, RULE),
    ]))
    elems.append(side)

    # ── Conclusion ────────────────────────────────────────────────────────────
    elems += _sec("Conclusion", st)

    rating     = concl.get("rating", "Neutral")
    confidence = concl.get("confidence", "Medium")
    r_hex      = _rating_hex(rating)

    elems.append(Paragraph(
        f'<font color="{r_hex}"><b>{rating.upper()}</b></font>'
        f'  ·  Confidence: <b>{confidence}</b>',
        st["rating"],
    ))
    elems.append(Spacer(1, 2*mm))

    if concl.get("manual_checks"):
        elems.append(Paragraph(
            f'<b>Check before acting:</b> {concl["manual_checks"]}',
            st["body"],
        ))
    if concl.get("what_changes_conclusion"):
        elems.append(Paragraph(
            f'<b>What changes the conclusion:</b> {concl["what_changes_conclusion"]}',
            st["body"],
        ))

    # ── Summary table ─────────────────────────────────────────────────────────
    elems += _sec("Summary Table", st)
    if scores:
        subj_rows2 = [i+1 for i, s in enumerate(scores)
                      if s.get("is_subject") or s.get("ticker") == sub_t]
        hdrs2 = ["Rank", "Ticker", "Name", "CCY", "Price",
                 "Mkt Cap B", "ROE %", "EBIT Mgn %", "EV/Sales", "Value Score"]
        cw2 = [CW*.06, CW*.09, CW*.20, CW*.05, CW*.08,
               CW*.10, CW*.08, CW*.10, CW*.09, CW*.11]
        header2 = [Paragraph(h, st["thl"] if i <= 2 else st["th"])
                   for i, h in enumerate(hdrs2)]
        rows2 = [header2]
        for s in scores:
            is_s  = s.get("is_subject") or s.get("ticker") == sub_t
            sl    = st["tdb"]  if is_s else st["tdl"]
            sr    = st["tdbr"] if is_s else st["td"]
            sc    = st["tdbc"] if is_s else st["tdc"]
            total = s.get("total_score")
            rows2.append([
                Paragraph(str(s.get("rank") or "—"), sc),
                Paragraph((s.get("ticker") or "?") + ("★" if is_s else ""), sl),
                Paragraph((s.get("name") or "")[:26], sl),
                Paragraph(s.get("currency") or "—", sc),
                Paragraph(_f(s.get("price"), 2), sr),
                Paragraph(_bn(s.get("market_cap_bn")), sr),
                Paragraph(_pct(s.get("roe_pct")), sr),
                Paragraph(_pct(s.get("ebit_margin_pct")), sr),
                Paragraph(_x(s.get("ev_sales")), sr),
                Paragraph(
                    f'<font color="{_score_color_hex(total)}"><b>{_f(total,1)}</b></font>'
                    if total is not None else "—",
                    sr,
                ),
            ])
        elems.append(_data_table(rows2, cw2, subj_rows2))

    elems.append(Spacer(1, 2*mm))
    elems.append(Paragraph(
        "★ = subject company  ·  All financial data from EODHD  ·  "
        "Score colour: green ≥65 · amber 40–64 · red <40",
        st["tiny"],
    ))

    return elems


# ── Main renderer ─────────────────────────────────────────────────────────────

class ValueMeterGenerator:

    def render(self, company: CompanyData, analysis: dict, output_path: str) -> str:
        report_date = datetime.now().strftime("%d %b %Y")
        st = _styles()

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=f"ValueMeter — {company.name or company.ticker}",
            author="EquityBot",
        )

        def _hdr(canvas, doc_inner):
            _draw_header(canvas, doc_inner, company, report_date)

        story = _page1(analysis, st) + _page2(analysis, st)
        doc.build(story, onFirstPage=_hdr, onLaterPages=_hdr)
        logger.info("ValueMeter PDF: %s", output_path)
        return output_path
