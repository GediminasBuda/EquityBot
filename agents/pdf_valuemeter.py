"""
pdf_valuemeter.py — ReportLab PDF renderer for the ValueMeter model.

Compact layout — fits all required content in 2 pages (may spill to 3 if
the peer group is large, but 6-8 peers typically stay within 2 pages):

  Page 1: Header + Peer group table (included + excluded) +
           Raw financial data table + Valuation yields table

  Page 2: Score & Rankings table + Verdict + Interpretation +
           Conclusion + Final summary table (Ticker, CCY, Price,
           MCap, ROE, EBIT Margin, EV/Sales, Value Score, Rank)

ReportLab rules (do not violate):
  - Never use Unicode sub/superscript — use <sub>/<super> XML tags.
  - Never call HexColor.hexval() for markup — use plain "#RRGGBB" constants.
"""

from __future__ import annotations
import logging
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak, KeepTogether,
)

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)

# ── Geometry ──────────────────────────────────────────────────────────────────
W, H  = A4
ML = MR = 14 * mm
MT     = 34 * mm
MB     = 12 * mm
CW     = W - ML - MR

# ── Colours (plain string constants — never use .hexval()) ───────────────────
NAVY_HEX   = "#003F54"
BLUE_HEX   = "#2E75B6"
GREEN_HEX  = "#1A7E3D"
RED_HEX    = "#C0392B"
AMBER_HEX  = "#D68910"
MGRAY_HEX  = "#666666"
DGRAY_HEX  = "#222222"
LGRAY_HEX  = "#F2F2F2"
LBLUE_HEX  = "#D6E8F7"
BORDER_HEX = "#CCDDEE"

NAVY   = HexColor(NAVY_HEX)
BLUE   = HexColor(BLUE_HEX)
GREEN  = HexColor(GREEN_HEX)
RED    = HexColor(RED_HEX)
AMBER  = HexColor(AMBER_HEX)
MGRAY  = HexColor(MGRAY_HEX)
LGRAY  = HexColor(LGRAY_HEX)
LBLUE  = HexColor(LBLUE_HEX)
BORDER = HexColor(BORDER_HEX)

BF = "Helvetica-Bold"
NF = "Helvetica"


# ── Styles ────────────────────────────────────────────────────────────────────

def _S() -> dict:
    def mk(name, **kw):
        return ParagraphStyle(name, **kw)
    return {
        "sec":    mk("sec",  fontName=BF, fontSize=8, textColor=NAVY,
                     spaceBefore=5, spaceAfter=2, leading=10),
        "body":   mk("body", fontName=NF, fontSize=7.5, textColor=HexColor(DGRAY_HEX),
                     leading=11, alignment=TA_JUSTIFY, spaceAfter=3),
        "small":  mk("small", fontName=NF, fontSize=6.5, textColor=MGRAY,
                     leading=9,  alignment=TA_JUSTIFY),
        "th":     mk("th",  fontName=BF, fontSize=6.5, textColor=white,
                     alignment=TA_CENTER, leading=8),
        "thl":    mk("thl", fontName=BF, fontSize=6.5, textColor=white,
                     alignment=TA_LEFT,   leading=8),
        "td":     mk("td",  fontName=NF, fontSize=6.5, textColor=HexColor(DGRAY_HEX),
                     alignment=TA_RIGHT,  leading=8),
        "tdl":    mk("tdl", fontName=NF, fontSize=6.5, textColor=HexColor(DGRAY_HEX),
                     alignment=TA_LEFT,   leading=8),
        "tdc":    mk("tdc", fontName=NF, fontSize=6.5, textColor=HexColor(DGRAY_HEX),
                     alignment=TA_CENTER, leading=8),
        "tdb":    mk("tdb", fontName=BF, fontSize=6.5, textColor=NAVY,
                     alignment=TA_LEFT,   leading=8),
        "tdbr":   mk("tdbr", fontName=BF, fontSize=6.5, textColor=NAVY,
                     alignment=TA_RIGHT,  leading=8),
        "bullet": mk("bullet", fontName=NF, fontSize=7.5, textColor=HexColor(DGRAY_HEX),
                     leading=11, leftIndent=10, spaceAfter=2),
    }


# ── Formatters ────────────────────────────────────────────────────────────────

def _f(v, dec=1, suf="") -> str:
    if v is None: return "—"
    try:    return f"{float(v):,.{dec}f}{suf}"
    except: return "—"

def _pct(v) -> str:
    return _f(v, 1, "%")

def _bn(v) -> str:
    return _f(v, 1, "B")

def _score_color(score) -> HexColor:
    try:
        s = float(score)
        if s >= 65: return GREEN
        if s >= 40: return AMBER
        return RED
    except: return MGRAY

def _verdict_color(verdict: str) -> HexColor:
    v = (verdict or "").lower()
    if "opportunity" in v:  return GREEN
    if "trap" in v or "mirage" in v: return RED
    return AMBER

def _verdict_label(verdict: str) -> str:
    return {
        "value_opportunity":        "VALUE OPPORTUNITY",
        "value_trap":               "VALUE TRAP",
        "cyclical_mirage":          "CYCLICAL MIRAGE",
        "fairly_priced_compounder": "FAIRLY PRICED COMPOUNDER",
    }.get((verdict or "").lower().strip(), (verdict or "UNKNOWN").upper())

def _rating_color(rating: str) -> HexColor:
    r = (rating or "").lower()
    if r == "attractive":   return GREEN
    if r == "unattractive": return RED
    return AMBER


# ── Standard table style ──────────────────────────────────────────────────────

def _base_ts(subject_rows: list[int] | None = None) -> TableStyle:
    cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0),  NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  white),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.25, BORDER),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    if subject_rows:
        for r in subject_rows:
            cmds.append(("BACKGROUND", (0, r), (-1, r), LBLUE))
    return TableStyle(cmds)


# ── Page header / footer ──────────────────────────────────────────────────────

def _header(canvas, doc, company: CompanyData, report_date: str) -> None:
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, H - MT, W, MT, fill=1, stroke=0)

    canvas.setFillColor(white)
    canvas.setFont(BF, 13)
    canvas.drawString(ML, H - 16*mm, "ValueMeter")
    canvas.setFont(NF, 8)
    canvas.setFillColor(HexColor("#AACCDD"))
    canvas.drawString(ML, H - 22*mm, "Prudent Value Score — Peer-Relative Valuation")

    name = (company.name or company.ticker or "")[:42]
    canvas.setFillColor(white)
    canvas.setFont(BF, 10)
    canvas.drawRightString(W - MR, H - 16*mm, name)
    canvas.setFont(NF, 7.5)
    canvas.setFillColor(HexColor("#AACCDD"))
    canvas.drawRightString(W - MR, H - 22*mm,
        f"{company.ticker or ''}  ·  {company.currency or ''}  ·  {report_date}")

    canvas.setFillColor(NAVY)
    canvas.rect(0, 0, W, MB, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#AACCDD"))
    canvas.setFont(NF, 6.5)
    canvas.drawString(ML, 4*mm,
        "ValueMeter is a screening tool — not an investment recommendation.")
    canvas.drawRightString(W - MR, 4*mm, f"Page {doc.page}")
    canvas.restoreState()


def _sec(title: str, st: dict) -> list:
    return [
        Spacer(1, 2*mm),
        Paragraph(title.upper(), st["sec"]),
        HRFlowable(width=CW, thickness=0.4, color=BORDER, spaceAfter=2),
    ]


# ── Page 1 ────────────────────────────────────────────────────────────────────

def _page1(analysis: dict, st: dict) -> list:
    elems = []
    peers    = analysis.get("peer_group") or []
    excluded = analysis.get("excluded_peers") or []

    # ── Peer group: included ──────────────────────────────────────────────────
    elems += _sec("Peer Group — Included", st)
    if peers:
        for p in peers:
            is_s   = p.get("is_subject", False)
            ticker = p.get("ticker", "?")
            name   = p.get("name", "")
            reason = p.get("reason_included") or p.get("reason", "")
            star   = " ★" if is_s else ""
            color  = NAVY_HEX if is_s else DGRAY_HEX
            elems.append(Paragraph(
                f'<font color="{color}"><b>{ticker}{star}  {name}</b></font>'
                f'<font color="{MGRAY_HEX}"> — {reason}</font>',
                st["small"],
            ))
    else:
        elems.append(Paragraph("No peer group data.", st["body"]))

    # ── Excluded ──────────────────────────────────────────────────────────────
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
        hdrs = ["Company", "CCY", "MCap\nB", "EV\nB", "Rev\nB",
                "EBIT\nB", "EBITDA\nB", "FCF\nB", "Net Debt\nB", "EBIT\nMgn%", "ROE%"]
        cw = [CW*.16, CW*.05, CW*.08, CW*.08, CW*.07,
              CW*.07, CW*.08, CW*.07, CW*.09, CW*.08, CW*.07]
        rows = [[Paragraph(h, st["th"]) for h in hdrs]]
        for p in peers:
            is_s = p.get("is_subject", False)
            sl   = st["tdb"]  if is_s else st["tdl"]
            sr   = st["tdbr"] if is_s else st["td"]
            t    = (p.get("ticker") or "?") + (" ★" if is_s else "")
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
        t1 = Table(rows, colWidths=cw, repeatRows=1)
        t1.setStyle(_base_ts(subj_rows))
        elems.append(t1)

    # ── Valuation yields ──────────────────────────────────────────────────────
    elems += _sec("Valuation Yields", st)
    if peers:
        subj_rows = [i+1 for i, p in enumerate(peers) if p.get("is_subject")]
        hdrs2 = ["Company", "EBIT/EV\n%", "FCF/EV\n%", "Book/\nPrice",
                 "Sales/\nEV", "ROIC/\nROE%", "ND/\nEBITDA", "Int.\nCover",
                 "FCF/\nNI", "Mom.\n6m%"]
        cw2 = [CW*.17, CW*.09, CW*.09, CW*.09, CW*.09,
               CW*.10, CW*.09, CW*.09, CW*.09, CW*.09]
        rows2 = [[Paragraph(h, st["th"]) for h in hdrs2]]
        for p in peers:
            is_s = p.get("is_subject", False)
            sl   = st["tdb"]  if is_s else st["tdl"]
            sr   = st["tdbr"] if is_s else st["td"]
            t    = (p.get("ticker") or "?") + (" ★" if is_s else "")
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
        t2 = Table(rows2, colWidths=cw2, repeatRows=1)
        t2.setStyle(_base_ts(subj_rows))
        elems.append(t2)

    return elems


# ── Page 2 ────────────────────────────────────────────────────────────────────

def _page2(analysis: dict, st: dict) -> list:
    elems = [PageBreak()]
    scores  = sorted(analysis.get("scores") or [], key=lambda x: x.get("rank") or 999)
    interp  = analysis.get("interpretation") or {}
    concl   = analysis.get("conclusion") or {}
    sub_t   = analysis.get("subject_ticker", "")

    # ── Score & Rankings ──────────────────────────────────────────────────────
    elems += _sec("Prudent Value Score — Rankings", st)

    if scores:
        subj_rows = [i+1 for i, s in enumerate(scores)
                     if s.get("is_subject") or s.get("ticker") == sub_t]
        hdrs = ["Rank", "Company", "Cheapness\n(0-100)", "Quality\n(0-100)",
                "Momentum\n(0-100)", "TOTAL\nSCORE", "Pctile"]
        cw = [CW*.07, CW*.28, CW*.13, CW*.13, CW*.13, CW*.14, CW*.12]
        rows = [[Paragraph(h, st["thl"] if i == 1 else st["th"])
                 for i, h in enumerate(hdrs)]]
        for s in scores:
            is_s = s.get("is_subject") or s.get("ticker") == sub_t
            sl   = st["tdb"]  if is_s else st["tdl"]
            sr   = st["tdbr"] if is_s else st["td"]
            sc   = st["tdc"]
            rows.append([
                Paragraph(str(s.get("rank") or "—"), sc),
                Paragraph(
                    f'{(s.get("ticker") or "?")}{"★" if is_s else ""}  {(s.get("name") or "")[:26]}',
                    sl,
                ),
                Paragraph(_f(s.get("cheapness_score"),  1), sr),
                Paragraph(_f(s.get("quality_score"),    1), sr),
                Paragraph(_f(s.get("momentum_score"),   1), sr),
                Paragraph(_f(s.get("total_score"),      1), sr),
                Paragraph(f'{int(s["percentile"])}th' if s.get("percentile") else "—", sc),
            ])
        tbl = Table(rows, colWidths=cw, repeatRows=1)
        ts  = _base_ts(subj_rows)
        # Colour-code total score column
        for i, s in enumerate(scores, start=1):
            c = _score_color(s.get("total_score"))
            ts.add("TEXTCOLOR", (5, i), (5, i), c)
            ts.add("FONTNAME",  (5, i), (5, i), BF)
        tbl.setStyle(ts)
        elems.append(tbl)
        elems.append(Paragraph(
            "Weights: EBIT/EV 20% · FCF/EV 20% · Book/Price 10% · Sales/EV 10% · "
            "ROIC/ROE 15% · Balance sheet 10% · FCF/NI 5% · Momentum 10%",
            st["small"],
        ))

    # ── Verdict banner ────────────────────────────────────────────────────────
    verdict = interp.get("verdict", "")
    v_color = _verdict_color(verdict)
    v_label = _verdict_label(verdict)
    v_expl  = interp.get("verdict_explanation", "")

    elems.append(Spacer(1, 3*mm))
    vdata = [[
        Paragraph(v_label, ParagraphStyle("vt", fontName=BF, fontSize=9,
                  textColor=white, alignment=TA_CENTER, leading=11)),
        Paragraph(v_expl,  ParagraphStyle("vb", fontName=NF, fontSize=7,
                  textColor=white, alignment=TA_JUSTIFY, leading=10)),
    ]]
    vtbl = Table(vdata, colWidths=[CW*.28, CW*.72])
    vtbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), v_color),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0),(-1,-1), 7),
        ("RIGHTPADDING", (0,0),(-1,-1), 7),
        ("TOPPADDING",   (0,0),(-1,-1), 7),
        ("BOTTOMPADDING",(0,0),(-1,-1), 7),
    ]))
    elems.append(vtbl)

    # ── Interpretation ────────────────────────────────────────────────────────
    elems += _sec("Interpretation", st)

    is_cheap = interp.get("is_cheap", "")
    chp_col  = GREEN_HEX if is_cheap == "Yes" else (RED_HEX if is_cheap == "No" else AMBER_HEX)
    elems.append(Paragraph(
        f'<font color="{chp_col}"><b>Cheap vs. peers: {is_cheap}</b></font>'
        f' — {interp.get("cheap_reason", "")}',
        st["body"],
    ))

    # Drivers + Risks side by side
    drivers = (interp.get("top_drivers") or [])[:3]
    risks   = (interp.get("top_risks")   or [])[:3]

    dr_text = "<b>Top 3 score drivers</b><br/>" + "<br/>".join(f"• {d}" for d in drivers)
    ri_text = "<b>Top 3 risks to score</b><br/>"  + "<br/>".join(f"• {r}" for r in risks)
    dr_para = Paragraph(dr_text, st["small"])
    ri_para = Paragraph(ri_text, st["small"])

    side = Table([[dr_para, ri_para]], colWidths=[CW*.5, CW*.5])
    side.setStyle(TableStyle([
        ("VALIGN",      (0,0),(-1,-1), "TOP"),
        ("LEFTPADDING", (0,0),(-1,-1), 0),
        ("RIGHTPADDING",(0,0),(-1,-1), 4),
    ]))
    elems.append(side)

    # ── Conclusion ────────────────────────────────────────────────────────────
    rating     = concl.get("rating", "Neutral")
    confidence = concl.get("confidence", "Medium")
    r_color    = _rating_color(rating)

    elems.append(Spacer(1, 3*mm))
    banner = Table([[
        Paragraph(
            f"<b>{rating.upper()}</b>",
            ParagraphStyle("rt", fontName=BF, fontSize=12,
                           textColor=white, alignment=TA_CENTER),
        ),
        Paragraph(
            f"<b>Confidence: {confidence}</b>",
            ParagraphStyle("rc", fontName=NF, fontSize=8,
                           textColor=white, alignment=TA_CENTER),
        ),
    ]], colWidths=[CW*.4, CW*.6])
    banner.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), r_color),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",   (0,0),(-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
    ]))
    elems.append(banner)
    elems.append(Spacer(1, 2*mm))

    mc_text = concl.get("manual_checks", "")
    wc_text = concl.get("what_changes_conclusion", "")
    if mc_text or wc_text:
        mc_para = Paragraph(
            f"<b>Check before acting:</b> {mc_text}", st["small"])
        wc_para = Paragraph(
            f"<b>What changes the conclusion:</b> {wc_text}", st["small"])
        elems.append(mc_para)
        elems.append(Spacer(1, 1*mm))
        elems.append(wc_para)

    # ── Final summary table ───────────────────────────────────────────────────
    elems += _sec("Summary Table", st)

    if scores:
        hdrs2 = ["Rank", "Ticker", "Name", "CCY", "Price",
                 "Mkt Cap B", "ROE %", "EBIT Mgn %", "EV/Sales", "Value Score"]
        cw2 = [CW*.06, CW*.09, CW*.19, CW*.05, CW*.08,
               CW*.09, CW*.08, CW*.10, CW*.08, CW*.10]
        # Remaining for rank column
        rows2 = [[Paragraph(h, st["th"]) for h in hdrs2]]
        subj_rows2 = [i+1 for i, s in enumerate(scores)
                      if s.get("is_subject") or s.get("ticker") == sub_t]
        for s in scores:
            is_s = s.get("is_subject") or s.get("ticker") == sub_t
            sl   = st["tdb"]  if is_s else st["tdl"]
            sr   = st["tdbr"] if is_s else st["td"]
            sc   = st["tdc"]
            rows2.append([
                Paragraph(str(s.get("rank") or "—"), sc),
                Paragraph((s.get("ticker") or "?") + ("★" if is_s else ""), sl),
                Paragraph((s.get("name") or "")[:24], sl),
                Paragraph(s.get("currency") or "—", sc),
                Paragraph(_f(s.get("price"), 2), sr),
                Paragraph(_bn(s.get("market_cap_bn")), sr),
                Paragraph(_pct(s.get("roe_pct")), sr),
                Paragraph(_pct(s.get("ebit_margin_pct")), sr),
                Paragraph(_f(s.get("ev_sales"), 2, "x"), sr),
                Paragraph(_f(s.get("total_score"), 1), sr),
            ])
        t3 = Table(rows2, colWidths=cw2, repeatRows=1)
        ts3 = _base_ts(subj_rows2)
        for i, s in enumerate(scores, start=1):
            c = _score_color(s.get("total_score"))
            ts3.add("TEXTCOLOR", (9, i), (9, i), c)
            ts3.add("FONTNAME",  (9, i), (9, i), BF)
        t3.setStyle(ts3)
        elems.append(t3)

    elems.append(Spacer(1, 2*mm))
    elems.append(Paragraph(
        "★ = subject company  ·  All data from EODHD  ·  "
        "Score 0–100 (green ≥65, amber 40–65, red <40)  ·  "
        "Screening tool only — not an investment recommendation.",
        st["small"],
    ))

    return elems


# ── Main renderer ─────────────────────────────────────────────────────────────

class ValueMeterGenerator:

    def render(self, company: CompanyData, analysis: dict, output_path: str) -> str:
        report_date = datetime.now().strftime("%d %b %Y")
        st = _S()

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=f"ValueMeter — {company.name or company.ticker}",
            author="EquityBot",
        )

        def _hdr(canvas, doc_inner):
            _header(canvas, doc_inner, company, report_date)

        story = _page1(analysis, st) + _page2(analysis, st)
        doc.build(story, onFirstPage=_hdr, onLaterPages=_hdr)
        logger.info("ValueMeter PDF: %s", output_path)
        return output_path
