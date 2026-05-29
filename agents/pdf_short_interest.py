"""
pdf_short_interest.py — Short Interest report renderer.

Design: EODHD Direct style — white background, navy text/headers,
thin rule below table headers, no coloured background blocks.

Layout (1-2 pages):
  Page 1: Header + Short Interest Snapshot + 12-month % of Float chart
           + Historical monthly table (last 12 months)
"""

from __future__ import annotations
import io
import logging
from datetime import datetime, date, timedelta
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, Image,
)

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)

# ── Page geometry ──────────────────────────────────────────────────────────────
W, H = A4
ML = MR = 15 * mm
MT     = 26 * mm
MB     = 12 * mm
CW     = W - ML - MR

# ── Colour palette (EODHD Direct) ─────────────────────────────────────────────
NAVY   = HexColor("#003F54")
DGRAY  = HexColor("#333333")
MGRAY  = HexColor("#666666")
CGRAY  = HexColor("#999999")
GREEN  = HexColor("#1A7E3D")
RED    = HexColor("#C0392B")
ORANGE = HexColor("#C9843E")
RULE   = HexColor("#DDDDDD")
BORDER = HexColor("#CCCCCC")

NAVY_HEX  = "#003F54"
GREEN_HEX = "#1A7E3D"
RED_HEX   = "#C0392B"
AMBER_HEX = "#C9843E"
MGRAY_HEX = "#666666"

BF = "Helvetica-Bold"
NF = "Helvetica"


# ── Style factory ──────────────────────────────────────────────────────────────
def _styles() -> dict:
    def S(name, **kw):
        return ParagraphStyle(name, **kw)
    return {
        "section": S("sec", fontName=BF, fontSize=9, textColor=NAVY,
                     spaceBefore=7, spaceAfter=2, leading=11),
        "body":    S("body", fontName=NF, fontSize=8, textColor=DGRAY,
                     leading=11, spaceAfter=3),
        "small":   S("sml", fontName=NF, fontSize=7.5, textColor=MGRAY,
                     leading=9),
        "tiny":    S("tin", fontName=NF, fontSize=6.5, textColor=CGRAY,
                     leading=8),
        # Table header: navy bold, white bg
        "th":      S("th",  fontName=BF, fontSize=7, textColor=NAVY,
                     alignment=TA_CENTER, leading=9),
        "thl":     S("thl", fontName=BF, fontSize=7, textColor=NAVY,
                     alignment=TA_LEFT,   leading=9),
        # Table cells
        "td":      S("td",  fontName=NF, fontSize=7.5, textColor=DGRAY,
                     alignment=TA_RIGHT,  leading=9),
        "tdl":     S("tdl", fontName=NF, fontSize=7.5, textColor=DGRAY,
                     alignment=TA_LEFT,   leading=9),
        "tdc":     S("tdc", fontName=NF, fontSize=7.5, textColor=DGRAY,
                     alignment=TA_CENTER, leading=9),
        # KV grid
        "kv_lbl":  S("kvl", fontName=NF, fontSize=8, textColor=MGRAY,
                     alignment=TA_LEFT, leading=11),
        "kv_val":  S("kvv", fontName=BF, fontSize=8, textColor=DGRAY,
                     alignment=TA_RIGHT, leading=11),
    }


# ── Formatters ────────────────────────────────────────────────────────────────
def _f(v, dec=2, suf="") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{dec}f}{suf}"
    except Exception:
        return "—"


def _pct_float(v, dec=2) -> str:
    """Convert decimal (0.05) → '5.00%'."""
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.{dec}f}%"
    except Exception:
        return "—"


def _chg_pct(cur, prior) -> str:
    """Month-over-month change in shares short as %."""
    if cur is None or prior is None:
        return "—"
    try:
        c, p = float(cur), float(prior)
        if abs(p) < 1e-9:
            return "—"
        chg = (c - p) / abs(p) * 100
        sign = "+" if chg >= 0 else ""
        return f"{sign}{chg:.1f}%"
    except Exception:
        return "—"


def _signal_hex(pct_float: Optional[float]) -> str:
    """Colour based on short % of float level."""
    if pct_float is None:
        return MGRAY_HEX
    v = float(pct_float) * 100
    if v >= 20:
        return RED_HEX
    if v >= 10:
        return AMBER_HEX
    if v >= 5:
        return NAVY_HEX
    return GREEN_HEX


# ── Standard data table (EODHD Direct style) ──────────────────────────────────
def _data_table(rows: list, col_widths: list) -> Table:
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), white),
        ("FONTNAME",      (0, 0), (-1, -1), NF),
        ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        # Header: navy bold, thick rule below
        ("FONTNAME",      (0, 0), (-1, 0), BF),
        ("TEXTCOLOR",     (0, 0), (-1, 0), NAVY),
        ("LINEBELOW",     (0, 0), (-1, 0), 1.2, NAVY),
        # Thin gray lines between data rows
        ("LINEBELOW",     (0, 1), (-1, -2), 0.25, BORDER),
    ]))
    return t


# ── KV grid (4-column: label | value | label | value) ─────────────────────────
def _kv_grid(rows: list[tuple], styles: dict) -> Table:
    data = []
    for i in range(0, len(rows), 2):
        left  = rows[i]
        right = rows[i + 1] if i + 1 < len(rows) else ("", "")
        data.append([
            Paragraph(left[0],  styles["kv_lbl"]),
            Paragraph(str(left[1]),  styles["kv_val"]),
            Paragraph(right[0], styles["kv_lbl"]),
            Paragraph(str(right[1]), styles["kv_val"]),
        ])
    lw = CW * 0.28
    vw = CW * 0.22
    t = Table(data, colWidths=[lw, vw, lw, vw])
    t.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW",     (0, 0), (-1, -2), 0.25, RULE),
        ("BACKGROUND",    (0, 0), (-1, -1), white),
    ]))
    return t


# ── Section heading ────────────────────────────────────────────────────────────
def _sec(title: str, st: dict) -> list:
    return [
        Spacer(1, 3 * mm),
        Paragraph(title.upper(), st["section"]),
        HRFlowable(width=CW, thickness=0.4, color=RULE, spaceAfter=2),
    ]


# ── Page header / footer ──────────────────────────────────────────────────────
def _draw_header(canvas, doc, company: CompanyData, report_date: str) -> None:
    canvas.saveState()

    # Left: report name + company sub-line
    canvas.setFont(BF, 12)
    canvas.setFillColor(NAVY)
    canvas.drawString(ML, H - 10 * mm, "Short Interest")

    canvas.setFont(NF, 7.5)
    canvas.setFillColor(MGRAY)
    sub = " | ".join(filter(None, [
        company.name or company.ticker,
        company.sector,
        company.country,
    ]))
    canvas.drawString(ML, H - 15 * mm, sub[:90])

    # Right: ticker + price + date
    ccy   = company.currency or ""
    price = company.current_price
    ps    = f"{price:,.2f} {ccy}".strip() if price else "—"
    canvas.setFont(BF, 8.5)
    canvas.setFillColor(NAVY)
    canvas.drawRightString(W - MR, H - 10 * mm, ps)
    canvas.setFont(NF, 7.5)
    canvas.setFillColor(MGRAY)
    canvas.drawRightString(W - MR, H - 15 * mm,
                           f"{company.ticker or ''}  ·  {report_date}")

    # Thin navy rule
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.6)
    canvas.line(ML, H - 18 * mm, W - MR, H - 18 * mm)

    # Footer
    canvas.setFont(NF, 6.5)
    canvas.setFillColor(CGRAY)
    canvas.drawString(ML, 7 * mm,
                      "Short Interest data from EODHD. For informational purposes only.")
    canvas.drawRightString(W - MR, 7 * mm,
                           f"Page {doc.page}  |  {report_date}")
    canvas.restoreState()


# ── Short % of Float chart (12 months) ────────────────────────────────────────
def _render_short_chart(history: list, ticker: str) -> Optional[bytes]:
    """
    Render a bar chart of Short % of Float for the last 12 months.
    history: list of dicts [{date, shares_short_m, short_pct_float}, ...] newest first.
    Returns PNG bytes or None.
    """
    if not history:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime as _dt
    except ImportError:
        return None

    # Filter last 12 months and reverse to chronological
    cutoff = (date.today() - timedelta(days=366)).isoformat()
    rows = [r for r in history if r.get("date", "") >= cutoff
            and r.get("short_pct_float") is not None]
    rows = rows[:12]  # at most 12
    rows = list(reversed(rows))  # oldest → newest

    if not rows:
        return None

    dates = []
    pcts  = []
    for r in rows:
        try:
            dates.append(_dt.strptime(r["date"], "%Y-%m-%d"))
            pcts.append(float(r["short_pct_float"]) * 100)
        except (ValueError, TypeError):
            continue

    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(7.2, 2.8), dpi=130)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    # Bar chart
    bar_colors = [RED_HEX if p >= 20 else AMBER_HEX if p >= 10 else NAVY_HEX for p in pcts]
    bars = ax.bar(dates, pcts, color=bar_colors, width=20, alpha=0.85, zorder=3)

    # Value labels on bars
    for bar, val in zip(bars, pcts):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.1,
            f"{val:.1f}%",
            ha="center", va="bottom",
            fontsize=6.5, color="#333333",
        )

    # Threshold lines
    if max(pcts) > 5:
        ax.axhline(5, color=NAVY_HEX, linewidth=0.6, linestyle="--", alpha=0.5,
                   label="5% (elevated)")
    if max(pcts) > 10:
        ax.axhline(10, color=AMBER_HEX, linewidth=0.6, linestyle="--", alpha=0.7,
                   label="10% (high)")
    if max(pcts) > 20:
        ax.axhline(20, color=RED_HEX, linewidth=0.6, linestyle="--", alpha=0.7,
                   label="20% (extreme)")

    ax.set_title(f"{ticker} — Short % of Float (last 12 months)",
                 fontsize=9, color=NAVY_HEX, fontweight="bold",
                 loc="left", pad=4)
    ax.set_ylabel("Short % of Float", fontsize=7, color=MGRAY_HEX)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=7)
    ax.tick_params(axis="y", labelsize=7, colors=MGRAY_HEX, length=2)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#DDDDDD")
        ax.spines[sp].set_linewidth(0.5)
    ax.grid(True, axis="y", color="#DDDDDD", linewidth=0.4, alpha=0.7, zorder=0)
    ax.set_ylim(bottom=0)

    if max(pcts) > 5:
        ax.legend(fontsize=6.5, loc="upper left", framealpha=0.6,
                  edgecolor="#DDDDDD")

    fig.text(0.99, 0.01, "Source: EODHD",
             ha="right", va="bottom", fontsize=5.5, color=MGRAY_HEX,
             style="italic")
    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ── Main content builder ───────────────────────────────────────────────────────
def _build_story(company: CompanyData, styles: dict) -> list:
    elems = []

    pct_float  = company.short_percent_of_float
    shares_s   = company.shares_short
    shares_sp  = company.shares_short_prior_month
    shares_f   = company.shares_float
    short_rat  = company.short_ratio
    history    = company.short_interest_history or []

    sig_hex = _signal_hex(pct_float)

    # ── Short Interest Snapshot ───────────────────────────────────────────────
    elems += _sec("Short Interest Snapshot", styles)

    # Signal banner as inline coloured text
    if pct_float is not None:
        v = pct_float * 100
        if v >= 20:
            level = "EXTREME (≥20%)"
        elif v >= 10:
            level = "HIGH (≥10%)"
        elif v >= 5:
            level = "ELEVATED (≥5%)"
        else:
            level = "LOW (<5%)"
        elems.append(Paragraph(
            f'Short % of Float: <font color="{sig_hex}"><b>{v:.2f}%</b></font>'
            f'  —  Signal: <font color="{sig_hex}"><b>{level}</b></font>',
            styles["body"],
        ))
    else:
        elems.append(Paragraph(
            "Short % of Float: <b>—</b>  (not available from EODHD for this ticker)",
            styles["body"],
        ))

    # MoM change
    mom_chg = _chg_pct(shares_s, shares_sp)
    mom_hex = RED_HEX if (mom_chg.startswith("+") and mom_chg != "+0.0%") else GREEN_HEX
    if shares_s is not None and shares_sp is not None:
        elems.append(Paragraph(
            f'Month-over-month change in shares short: '
            f'<font color="{mom_hex}"><b>{mom_chg}</b></font>'
            f'  ({_f(shares_sp, 2)}M → {_f(shares_s, 2)}M)',
            styles["body"],
        ))

    elems.append(Spacer(1, 2 * mm))

    # KV grid
    rows = [
        ("Shares Short (current)",    f"{_f(shares_s, 2)}M"),
        ("Shares Short (prior month)", f"{_f(shares_sp, 2)}M"),
        ("Short % of Float",           _pct_float(pct_float, 2)),
        ("Days to Cover (Short Ratio)", _f(short_rat, 1)),
        ("Shares Float",               f"{_f(shares_f, 1)}M"),
        ("Shares Outstanding",         f"{_f(company.shares_outstanding, 1)}M"),
        ("% Held by Insiders",
         _pct_float(company.pct_insiders, 1) if company.pct_insiders else "—"),
        ("% Held by Institutions",
         _pct_float(company.pct_institutions, 1) if company.pct_institutions else "—"),
    ]
    elems.append(_kv_grid(rows, styles))

    # ── Interpretation note ───────────────────────────────────────────────────
    elems.append(Spacer(1, 2 * mm))
    elems.append(Paragraph(
        "<b>How to read this data:</b>  Short % of Float = shorted shares ÷ free-floating shares. "
        "A rising short interest may indicate growing bearish sentiment, but can also lead to a "
        "short squeeze if the stock rallies. Days to Cover = shares short ÷ average daily volume "
        "(higher = riskier unwind). Thresholds: <5% low · 5–10% elevated · 10–20% high · ≥20% extreme.",
        styles["small"],
    ))

    # ── Chart: 12-month Short % of Float ─────────────────────────────────────
    elems += _sec("Short % of Float — Last 12 Months", styles)

    chart_history = [r for r in history if r.get("short_pct_float") is not None]
    if chart_history:
        png = _render_short_chart(history, company.ticker or "")
        if png:
            img = Image(io.BytesIO(png), width=170 * mm, height=60 * mm,
                        kind="proportional")
            elems.append(img)
        else:
            elems.append(Paragraph(
                "Chart could not be rendered (matplotlib not available).",
                styles["small"],
            ))
    else:
        elems.append(Paragraph(
            "No historical short interest data available from EODHD for this ticker. "
            "Short interest history is typically available for US-listed equities only.",
            styles["small"],
        ))

    # ── Historical table (last 12 months) ─────────────────────────────────────
    cutoff = (date.today() - timedelta(days=366)).isoformat()
    hist_12 = [r for r in history if r.get("date", "") >= cutoff][:12]

    if hist_12:
        elems += _sec("Monthly Short Interest History (Last 12 Months)", styles)
        hdrs = ["Date", "Shares Short (M)", "Short % of Float", "MoM Change (Shares)"]
        cw   = [CW * 0.22, CW * 0.26, CW * 0.26, CW * 0.26]
        header_row = [
            Paragraph(hdrs[0], styles["thl"]),
            Paragraph(hdrs[1], styles["th"]),
            Paragraph(hdrs[2], styles["th"]),
            Paragraph(hdrs[3], styles["th"]),
        ]
        rows_data = [header_row]
        for i, r in enumerate(hist_12):
            # MoM change vs next record (which is prior month since newest-first)
            if i + 1 < len(hist_12):
                prev_s = hist_12[i + 1].get("shares_short_m")
                curr_s = r.get("shares_short_m")
                chg = _chg_pct(curr_s, prev_s)
                chg_hex = RED_HEX if (chg.startswith("+") and chg != "+0.0%") else GREEN_HEX
                chg_cell = f'<font color="{chg_hex}">{chg}</font>'
            else:
                chg_cell = "—"

            pct_v = r.get("short_pct_float")
            pct_hex = _signal_hex(pct_v)
            pct_str = (f'<font color="{pct_hex}"><b>'
                       f'{float(pct_v)*100:.2f}%</b></font>') if pct_v else "—"

            rows_data.append([
                Paragraph(str(r.get("date", "—"))[:10], styles["tdl"]),
                Paragraph(_f(r.get("shares_short_m"), 2), styles["td"]),
                Paragraph(pct_str, styles["td"]),
                Paragraph(chg_cell, styles["td"]),
            ])
        elems.append(_data_table(rows_data, cw))

    elif history:
        # Have history but not within 12 months — show what we have (up to 12)
        elems += _sec("Short Interest History (Available Records)", styles)
        hdrs = ["Date", "Shares Short (M)", "Short % of Float"]
        cw   = [CW * 0.25, CW * 0.375, CW * 0.375]
        header_row = [Paragraph(h, styles["thl"] if i == 0 else styles["th"])
                      for i, h in enumerate(hdrs)]
        rows_data = [header_row]
        for r in history[:12]:
            pct_v = r.get("short_pct_float")
            pct_hex = _signal_hex(pct_v)
            pct_str = (f'<font color="{pct_hex}"><b>'
                       f'{float(pct_v)*100:.2f}%</b></font>') if pct_v else "—"
            rows_data.append([
                Paragraph(str(r.get("date", "—"))[:10], styles["tdl"]),
                Paragraph(_f(r.get("shares_short_m"), 2), styles["td"]),
                Paragraph(pct_str, styles["td"]),
            ])
        elems.append(_data_table(rows_data, cw))

    # ── Footer note ───────────────────────────────────────────────────────────
    elems.append(Spacer(1, 3 * mm))
    elems.append(Paragraph(
        "Short interest data sourced from EODHD. Historical short interest "
        "is reported bi-monthly for US equities (FINRA requirement). "
        "Short % of Float = Shares Short ÷ Shares Float. "
        "Bar colour: green < 5% · navy 5–10% · amber 10–20% · red ≥ 20%.",
        styles["tiny"],
    ))

    return elems


# ── Main renderer ──────────────────────────────────────────────────────────────
class ShortInterestGenerator:

    def render(self, company: CompanyData, output_path: str) -> str:
        report_date = datetime.now().strftime("%d %b %Y")
        st = _styles()

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=f"Short Interest — {company.name or company.ticker}",
            author="EquityBot",
        )

        def _hdr(canvas, doc_inner):
            _draw_header(canvas, doc_inner, company, report_date)

        story = _build_story(company, st)
        doc.build(story, onFirstPage=_hdr, onLaterPages=_hdr)
        logger.info("Short Interest PDF: %s", output_path)
        return output_path
