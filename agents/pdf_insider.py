"""
pdf_insider.py — "Insider Transactions" report.

Renders:
  Page 1 — Compact header (name · ticker · price · market cap · currency)
           + 12-month monthly summary table
              (Month | # Buys | # Sells | Net shares | Net value)
  Page 2+ — Full individual-transaction log filtered by user-selected
           period (1y / 2y / 5y), sorted by date descending.

Data comes from data_sources/insider_data.fetch_insider_data() which
combines EODHD's insider-transactions endpoint with an
insidertrades.info scraper fallback for European tickers EODHD doesn't
track. The PDF generator treats both sources uniformly — only the
"Data source" footer note differs.
"""

from __future__ import annotations
import io
import logging
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak,
)

logger = logging.getLogger(__name__)

# ── Page geometry ────────────────────────────────────────────────────────────
W, H   = A4
ML = MR = 15 * mm
MT      = 28 * mm
MB      = 12 * mm
CW      = W - ML - MR

# ── Palette (matches pdf_eodhd_full.py) ──────────────────────────────────────
NAVY     = HexColor("#003F54")
TEAL     = HexColor("#1A6E5A")
DGRAY    = HexColor("#333333")
MGRAY    = HexColor("#666666")
CGRAY    = HexColor("#999999")
RULE     = HexColor("#DDDDDD")
BORDER   = HexColor("#CCCCCC")
GREEN    = HexColor("#1A7E3D")
RED      = HexColor("#C0392B")
LGRAY    = HexColor("#F5F5F5")
LBLUE    = HexColor("#E0EEF4")
ROW_ALT  = HexColor("#FAFAFA")

# Plain-string hex constants for Paragraph XML markup. We cannot use
# HexColor.hexval() inside `<font color='...'>` — that helper returns
# "0xRRGGBB" which ReportLab then rejects as an invalid colour string.
GREEN_HEX = "#1A7E3D"
RED_HEX   = "#C0392B"
MGRAY_HEX = "#666666"
DGRAY_HEX = "#333333"

BASE_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"


# ── Formatters ───────────────────────────────────────────────────────────────
def _fmt_num(v: Any, dec: int = 0) -> str:
    """Format a number with thousands separator, or '—' for missing."""
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.{dec}f}"
    except (ValueError, TypeError):
        return "—"


def _fmt_money(v: Any, currency: str = "") -> str:
    """Compact money: 4.2M / 1.3B / 850K / 42 with optional currency."""
    if v is None:
        return "—"
    try:
        x = float(v)
    except (ValueError, TypeError):
        return "—"
    sign = "-" if x < 0 else ""
    x = abs(x)
    suf = ""
    if   x >= 1e12: x, suf = x / 1e12, "T"
    elif x >= 1e9:  x, suf = x / 1e9,  "B"
    elif x >= 1e6:  x, suf = x / 1e6,  "M"
    elif x >= 1e3:  x, suf = x / 1e3,  "K"
    txt = f"{sign}{x:,.2f}{suf}"
    return f"{txt} {currency}".strip()


def _fmt_signed(v: Any, currency: str = "") -> tuple[str, str]:
    """Return (text, hex_color_str) — green for positive, red for negative.

    The colour is returned as a plain "#RRGGBB" string, ready to drop
    into a Paragraph `<font color='...'>` markup (HexColor.hexval()
    returns "0xRRGGBB" which Paragraph rejects).
    """
    if v is None:
        return "—", DGRAY_HEX
    try:
        x = float(v)
    except (ValueError, TypeError):
        return "—", DGRAY_HEX
    txt = _fmt_money(x, currency)
    if x > 0:
        return f"+{txt}", GREEN_HEX
    if x < 0:
        return txt, RED_HEX
    return txt, DGRAY_HEX


# ── Styles ───────────────────────────────────────────────────────────────────
def _styles() -> dict:
    return {
        "title":     ParagraphStyle("t",  fontName=BOLD_FONT, fontSize=15,
                                    textColor=NAVY, leading=18, alignment=TA_LEFT),
        "subtitle":  ParagraphStyle("s",  fontName=BASE_FONT, fontSize=9,
                                    textColor=MGRAY, leading=11, alignment=TA_LEFT),
        "section":   ParagraphStyle("se", fontName=BOLD_FONT, fontSize=10,
                                    textColor=NAVY, leading=14, alignment=TA_LEFT,
                                    spaceBefore=8, spaceAfter=4),
        "hdr":       ParagraphStyle("h",  fontName=BOLD_FONT, fontSize=8,
                                    textColor=MGRAY, leading=10, alignment=TA_CENTER),
        "hdrL":      ParagraphStyle("hl", fontName=BOLD_FONT, fontSize=8,
                                    textColor=MGRAY, leading=10, alignment=TA_LEFT),
        "cell":      ParagraphStyle("c",  fontName=BASE_FONT, fontSize=8.5,
                                    textColor=DGRAY, leading=11, alignment=TA_CENTER),
        "cellL":     ParagraphStyle("cl", fontName=BASE_FONT, fontSize=8.5,
                                    textColor=DGRAY, leading=11, alignment=TA_LEFT),
        "cellR":     ParagraphStyle("cr", fontName=BASE_FONT, fontSize=8.5,
                                    textColor=DGRAY, leading=11, alignment=TA_RIGHT),
        "small":     ParagraphStyle("sm", fontName=BASE_FONT, fontSize=7.5,
                                    textColor=CGRAY, leading=10, alignment=TA_LEFT),
    }


# ── Header (small: name, ticker, price, market cap, currency) ────────────────
def _header_block(company, styles: dict) -> list:
    name     = getattr(company, "name", None) or getattr(company, "ticker", "?")
    ticker   = getattr(company, "ticker", "")
    price    = getattr(company, "current_price", None)
    mcap     = getattr(company, "market_cap", None)
    currency = (getattr(company, "currency_price", None)
                or getattr(company, "currency", "")
                or "")

    title_html = f"{name}  <font color='#666666' size='10'>· {ticker}</font>"
    sub_bits = []
    if price is not None:
        sub_bits.append(f"Price: <b>{_fmt_num(price, 2)} {currency}</b>".strip())
    if mcap is not None:
        sub_bits.append(f"Market Cap: <b>{_fmt_money(mcap, currency)}</b>")
    sub_html = "  ·  ".join(sub_bits) if sub_bits else ""

    el = [
        Paragraph(title_html, styles["title"]),
    ]
    if sub_html:
        el.append(Paragraph(sub_html, styles["subtitle"]))
    el.append(Spacer(1, 4))
    el.append(HRFlowable(width="100%", thickness=0.6, color=RULE,
                         spaceBefore=2, spaceAfter=8))
    return el


# ── 12-month monthly summary table ───────────────────────────────────────────
_MONTH_LABELS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _last_12_month_buckets(now: Optional[datetime] = None) -> list[tuple[int, int]]:
    """Return [(year, month), ...] for the past 12 months in chronological
    order (oldest first)."""
    now = now or datetime.utcnow()
    buckets: list[tuple[int, int]] = []
    y, m = now.year, now.month
    for _ in range(12):
        buckets.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    buckets.reverse()
    return buckets


def _monthly_summary_section(
    txns: list[dict],
    currency: str,
    styles: dict,
) -> list:
    """
    Build the 12-month rollup table.
      Month | # Buys | # Sells | Net shares | Net value
    """
    el: list = []
    el.append(Paragraph("12-Month Monthly Summary", styles["section"]))

    buckets = _last_12_month_buckets()
    # Pre-seed every bucket so even empty months render a row.
    rollup: "OrderedDict[tuple[int, int], dict]" = OrderedDict(
        ((y, m), {"buys": 0, "sells": 0, "net_shares": 0.0, "net_value": 0.0})
        for (y, m) in buckets
    )

    earliest_key = buckets[0]

    for t in txns:
        try:
            d = datetime.strptime(t["transactionDate"], "%Y-%m-%d")
        except Exception:
            continue
        key = (d.year, d.month)
        if key not in rollup or key < earliest_key:
            continue
        code   = (t.get("transactionCode") or "").upper()
        shares = float(t.get("transactionShares") or 0)
        value  = float(t.get("transactionValue") or 0)
        if code == "P":  # Purchase / Buy
            rollup[key]["buys"]      += 1
            rollup[key]["net_shares"] += shares
            rollup[key]["net_value"]  += value
        elif code == "S":  # Sale / Sell
            rollup[key]["sells"]     += 1
            rollup[key]["net_shares"] -= shares
            rollup[key]["net_value"]  -= value
        # Award/Grant (A) and unknown (?) don't contribute to net direction.

    # ── Build the table ──────────────────────────────────────────────────────
    col_widths = [
        CW * 0.20,   # Month
        CW * 0.12,   # # Buys
        CW * 0.12,   # # Sells
        CW * 0.26,   # Net shares
        CW * 0.30,   # Net value
    ]
    rows = [[
        Paragraph("Month",        styles["hdrL"]),
        Paragraph("# Buys",       styles["hdr"]),
        Paragraph("# Sells",      styles["hdr"]),
        Paragraph("Net Shares",   styles["hdr"]),
        Paragraph(f"Net Value ({currency or '—'})", styles["hdr"]),
    ]]
    for (y, m), agg in rollup.items():
        label = f"{_MONTH_LABELS[m - 1]} {y}"
        net_shares_text = _fmt_num(agg["net_shares"], 0)
        if agg["net_shares"] > 0:
            net_shares_text = f"+{net_shares_text}"
            net_shares_html = (
                f"<font color='{GREEN_HEX}'>{net_shares_text}</font>"
            )
        elif agg["net_shares"] < 0:
            net_shares_html = (
                f"<font color='{RED_HEX}'>{net_shares_text}</font>"
            )
        else:
            net_shares_html = net_shares_text

        net_value_text, net_value_hex = _fmt_signed(
            agg["net_value"], currency,
        )
        net_value_html = (
            f"<font color='{net_value_hex}'>{net_value_text}</font>"
        )

        rows.append([
            Paragraph(label, styles["cellL"]),
            Paragraph(str(agg["buys"]),  styles["cell"]),
            Paragraph(str(agg["sells"]), styles["cell"]),
            Paragraph(net_shares_html, styles["cellR"]),
            Paragraph(net_value_html,  styles["cellR"]),
        ])

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), LGRAY),
        ("LINEBELOW",    (0, 0), (-1, 0), 0.6, NAVY),
        ("LINEBELOW",    (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ])
    # Zebra striping on data rows
    for i in range(1, len(rows)):
        if i % 2 == 0:
            style.add("BACKGROUND", (0, i), (-1, i), ROW_ALT)
    t.setStyle(style)
    el.append(t)
    return el


# ── Individual transactions log ──────────────────────────────────────────────
def _txn_log_section(
    txns: list[dict],
    period_label: str,
    currency: str,
    styles: dict,
) -> list:
    el: list = []
    el.append(Paragraph(
        f"Individual Transactions — {period_label}", styles["section"],
    ))

    if not txns:
        el.append(Paragraph(
            "No insider transactions returned by either EODHD or "
            "insidertrades.info for this ticker in the selected period.",
            styles["small"],
        ))
        return el

    col_widths = [
        CW * 0.10,   # Date
        CW * 0.24,   # Insider
        CW * 0.18,   # Role
        CW * 0.08,   # Type
        CW * 0.14,   # Shares
        CW * 0.12,   # Price
        CW * 0.14,   # Value
    ]
    rows = [[
        Paragraph("Date",     styles["hdrL"]),
        Paragraph("Insider",  styles["hdrL"]),
        Paragraph("Role",     styles["hdrL"]),
        Paragraph("Type",     styles["hdr"]),
        Paragraph("Shares",   styles["hdr"]),
        Paragraph(f"Price ({currency or '—'})", styles["hdr"]),
        Paragraph(f"Value ({currency or '—'})", styles["hdr"]),
    ]]
    for t in txns:
        code = (t.get("transactionCode") or "?").upper()
        if code == "P":
            type_label, type_hex = "BUY",  GREEN_HEX
        elif code == "S":
            type_label, type_hex = "SELL", RED_HEX
        elif code == "A":
            type_label, type_hex = "GRANT", MGRAY_HEX
        else:
            type_label, type_hex = code or "—", MGRAY_HEX

        type_html = f"<font color='{type_hex}'><b>{type_label}</b></font>"

        rows.append([
            Paragraph(t.get("transactionDate") or "—", styles["cellL"]),
            Paragraph(t.get("ownerName") or "—",       styles["cellL"]),
            Paragraph(t.get("ownerRelationship") or "—", styles["cellL"]),
            Paragraph(type_html, styles["cell"]),
            Paragraph(_fmt_num(t.get("transactionShares"), 0),       styles["cellR"]),
            Paragraph(_fmt_num(t.get("transactionPricePerShare"), 2), styles["cellR"]),
            Paragraph(_fmt_money(t.get("transactionValue"), currency), styles["cellR"]),
        ])

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), LGRAY),
        ("LINEBELOW",    (0, 0), (-1, 0), 0.6, NAVY),
        ("LINEBELOW",    (0, 1), (-1, -1), 0.25, RULE),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2.5),
    ])
    for i in range(1, len(rows)):
        if i % 2 == 0:
            style.add("BACKGROUND", (0, i), (-1, i), ROW_ALT)
    t.setStyle(style)
    el.append(t)
    return el


# ── Page header (top-of-every-page banner) ───────────────────────────────────
def _draw_header(canvas, doc, company, report_date: str):
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.setFont(BOLD_FONT, 8.5)
    title = (getattr(company, "name", None)
             or getattr(company, "ticker", "?"))
    canvas.drawString(ML, H - 14 * mm, f"{title} — Insider Transactions")
    canvas.setFillColor(MGRAY)
    canvas.setFont(BASE_FONT, 7.5)
    canvas.drawRightString(W - MR, H - 14 * mm, report_date)
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.3)
    canvas.line(ML, H - 17 * mm, W - MR, H - 17 * mm)
    # Page number bottom-right
    canvas.setFont(BASE_FONT, 7.5)
    canvas.drawRightString(W - MR, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


# ── Main entry point ─────────────────────────────────────────────────────────
class InsiderTransactionsGenerator:
    """Render the Insider Transactions report to a PDF file."""

    def render(
        self,
        company,
        insider_bundle: dict,
        period_months: int,
        output_path: str,
    ) -> str:
        """
        company         — populated CompanyData (used only for header)
        insider_bundle  — dict from data_sources.insider_data.fetch_insider_data()
        period_months   — user-selected period (12 / 24 / 60) for the log table
        output_path     — where to write the PDF
        """
        styles      = _styles()
        report_date = datetime.now().strftime("%d %b %Y")
        currency    = (getattr(company, "currency_price", None)
                       or getattr(company, "currency", "")
                       or "")

        all_txns    = insider_bundle.get("transactions") or []
        source_used = insider_bundle.get("source_used", "none")

        # Period-filtered log
        cutoff_dt = datetime.utcnow()
        from datetime import timedelta as _td
        cutoff    = cutoff_dt - _td(days=period_months * 31)
        log_txns: list[dict] = []
        for t in all_txns:
            try:
                d = datetime.strptime(t["transactionDate"], "%Y-%m-%d")
                if d >= cutoff:
                    log_txns.append(t)
            except Exception:
                continue

        # The monthly-summary table always uses the past 12 months,
        # regardless of period_months.
        monthly_txns: list[dict] = []
        twelve_cutoff = cutoff_dt - _td(days=370)
        for t in all_txns:
            try:
                d = datetime.strptime(t["transactionDate"], "%Y-%m-%d")
                if d >= twelve_cutoff:
                    monthly_txns.append(t)
            except Exception:
                continue

        period_label_map = {12: "Last 1 year",
                            24: "Last 2 years",
                            60: "Last 5 years"}
        period_label = period_label_map.get(period_months,
                                            f"Last {period_months} months")

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT,  bottomMargin=MB,
            title=f"{getattr(company, 'ticker', '?')} — Insider Transactions",
            author="Your Humble EquityBot",
        )

        def _on_page(canvas, doc_):
            _draw_header(canvas, doc_, company, report_date)

        story: list = []
        story.extend(_header_block(company, styles))
        story.extend(_monthly_summary_section(monthly_txns, currency, styles))
        story.append(Spacer(1, 6))
        story.extend(_txn_log_section(log_txns, period_label, currency, styles))

        # Footer note: source attribution
        story.append(Spacer(1, 8))
        src_human = {
            "eodhd":              "EODHD /insider-transactions",
            "insidertrades.info": "insidertrades.info (fallback scrape)",
            "none":               "no source returned data",
        }.get(source_used, source_used)
        story.append(Paragraph(
            f"<i>Data source: {src_human}</i>",
            styles["small"],
        ))

        doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
        logger.info(f"Insider Transactions PDF written: {output_path}")
        return output_path
