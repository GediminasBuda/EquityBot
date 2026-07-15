"""
earnings_quality.py — Earnings Quality Score model.

Forensic-accounting assessment of the reliability, sustainability, and
cash-backed nature of reported earnings for the subject company and up to
6 peers, combining the approaches of Buffett (owner earnings), Sloan
(accrual anomaly), Schilit (Financial Shenanigans), Mauboussin (capital
allocation) and Quality Investing (Novy-Marx, Asness).

The subject and every fetched peer are scored together in a single LLM
call so the ranking/percentile within the universe is internally
consistent. Percentile and rank are then recomputed deterministically in
Python from the returned scores (not trusted from the LLM) — see
_validate_earnings_quality().

Data source: same EODHD-then-yfinance waterfall as Investment Memo V2
(driven from pages/report_generator.py's earnings_quality dispatch block).
"""

from __future__ import annotations
import logging
from typing import Optional

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)


# ── Subcategory weights (must sum to 1.00) ─────────────────────────────────
EQ_WEIGHTS = {
    "cash_conversion":              0.25,
    "accrual_quality":              0.20,
    "recurring_earnings":           0.15,
    "balance_sheet_support":        0.15,
    "earnings_stability":           0.10,
    "accounting_conservatism":      0.10,
    "management_reporting_quality": 0.05,
}

EQ_DIMENSION_LABELS = {
    "cash_conversion":              "Cash Conversion",
    "accrual_quality":              "Accrual Quality",
    "recurring_earnings":           "Recurring Earnings",
    "balance_sheet_support":        "Balance Sheet Support",
    "earnings_stability":           "Earnings Stability",
    "accounting_conservatism":      "Accounting Conservatism",
    "management_reporting_quality": "Mgmt Reporting Quality",
}


SYSTEM_PROMPT = """You are a value-quality forensic accounting analyst combining the \
approaches of Warren Buffett (owner earnings), Richard Sloan (accrual anomaly), \
Howard Schilit (Financial Shenanigans), Michael Mauboussin (capital allocation), \
and Quality Investing (Novy-Marx, Asness).

You measure the reliability, sustainability, and cash-backed nature of reported \
earnings — not simply profitability. You never invent numbers: every figure you \
cite must come from the financial data provided. Where a data point needed for a \
dimension is missing, say so explicitly and lower your confidence level rather \
than guessing. You are not a cheerleader — a high score must be earned by \
demonstrated cash conversion and conservative accounting, and you penalize \
aggressive accounting practices even for companies with strong reported growth.
"""


# ── Cached prompt prefix — fixed framework instructions + output schema ────
# Identical for every run of this framework, so Anthropic can cache it after
# the first call. Company-specific data goes in the dynamic prompt.
_EQ_CACHEABLE = """\
Objective: Assign every company in the universe below an Earnings Quality Score \
(0-100) that measures the reliability, sustainability, and cash-backed nature of \
reported earnings.

Universe: The subject company plus its peer group (manually supplied or \
LLM-suggested, max 6 peers), analyzed together using the latest fiscal year plus \
the previous five years wherever available. Score every company provided in the \
COMPANIES DATA section — do not skip any, and do not invent additional companies.

Evaluate the following seven dimensions for every company:

1. Cash Conversion (weight 25%): Operating Cash Flow / Net Income, Free Cash \
Flow / Net Income, multi-year cash conversion trend, CFO consistency.
2. Accrual Quality (weight 20%): Sloan Accrual Ratio = (Net Income − Operating \
Cash Flow) / Total Assets, Working Capital Accruals, degree to which earnings \
are supported by cash rather than accounting estimates.
3. Recurring Earnings (weight 15%): frequency and magnitude of restructuring \
charges, impairments, acquisition costs, litigation adjustments, and recurring \
"one-time" items (use extraordinary/non-recurring items and margin volatility as \
evidence).
4. Balance Sheet Support (weight 15%): receivables growth vs revenue growth, \
inventory growth vs revenue growth, deferred revenue trends, goodwill intensity \
(goodwill + intangibles / total assets), capitalized assets, working-capital \
quality.
5. Earnings Stability (weight 10%): five-year stability of gross margin, \
operating margin, ROIC/ROE, EPS, and operating cash flow.
6. Accounting Conservatism (weight 10%): revenue recognition practices, \
capitalization policies (R&D/software), depreciation assumptions, write-down \
history, effective tax-rate consistency (tax provision / pre-tax income).
7. Management Reporting Quality (weight 5%): gap between reported and \
underlying/adjusted earnings, dilution (shares outstanding trend), dividend and \
buyback behaviour relative to FCF, guidance/estimate reliability where evident.

The overall Earnings Quality Score should approximate the weighted combination \
of the seven subcategory scores (weights above) adjusted by your holistic \
judgment — do not just mechanically average if the evidence points to a \
materially different quality assessment.

Letter grade mapping (apply consistently from the numeric score):
97-100 A+ | 93-96 A | 90-92 A- | 87-89 B+ | 83-86 B | 80-82 B- | 77-79 C+ | \
73-76 C | 70-72 C- | 60-69 D | below 60 F

For EVERY company (subject and each peer) return:
- ticker, name
- earnings_quality_score (0-100 integer)
- letter_grade (per the mapping above)
- subscores: object with the 7 keys below, each 0-100 integer:
  cash_conversion, accrual_quality, recurring_earnings, balance_sheet_support,
  earnings_stability, accounting_conservatism, management_reporting_quality
- top_strengths: exactly 3 short strings (specific, cite numbers where possible)
- top_risks: exactly 3 short strings (specific, cite numbers where possible)
- red_flags: 0-5 short strings — the most important accounting red flags found;
  return an empty array if genuinely none are present (do not pad with filler)
- confidence: "High", "Medium", or "Low" — lower this when historical depth or
  key data (e.g. operating cash flow, balance sheet detail) is thin or missing
- explanation: 200-300 word prose paragraph explaining why this score was
  assigned, referencing the specific dimensions and numbers that drove it

Required JSON output shape:
{
  "companies": [
    {
      "ticker": "TICK.EX",
      "name": "Company Name",
      "earnings_quality_score": 78,
      "letter_grade": "B+",
      "subscores": {
        "cash_conversion": 80,
        "accrual_quality": 75,
        "recurring_earnings": 70,
        "balance_sheet_support": 82,
        "earnings_stability": 75,
        "accounting_conservatism": 80,
        "management_reporting_quality": 65
      },
      "top_strengths": ["...", "...", "..."],
      "top_risks": ["...", "...", "..."],
      "red_flags": ["..."],
      "confidence": "High",
      "explanation": "..."
    }
  ]
}

Do NOT include a percentile or rank field — those are computed programmatically \
after your response. Do NOT include a separate summary/ranking table in your \
JSON — it is built programmatically from the "companies" array.

Scoring principles: favor companies whose profits consistently convert into \
cash, whose earnings rely minimally on accounting estimates, whose margins and \
returns are durable, whose accounting is conservative, and whose reported \
earnings require few adjustments. Penalize aggressive revenue recognition, \
large accruals, repeated "non-recurring" charges, poor cash conversion, \
heavy dilution, serial acquisition accounting, and financial reporting \
practices that obscure underlying economics.

Rules:
- All numbers you reference must come from the financial data provided — do not
  invent figures.
- Write in English. Professional forensic-accounting analyst tone. Prose only in
  explanation and strength/risk/red-flag strings — no markdown formatting.
- If historical depth for a company is thin (e.g. fewer than 3 fiscal years of
  data), say so in the explanation and cap confidence at "Medium" or lower.

=== COMPANIES DATA FOLLOWS ==="""


# ── Financial data formatter — earnings-quality-specific fields ────────────

def _b(v) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:.1f}M"


def _pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"


def _x(v, dp=1) -> str:
    return f"{v:.{dp}f}x" if v is not None else "n/a"


def _ratio(numer, denom) -> str:
    if numer is None or denom is None or denom == 0:
        return "n/a"
    return f"{numer/denom:.2f}x"


def format_company_for_eq(company: CompanyData) -> str:
    """Build the earnings-quality financial data block for one company."""
    cur = company.currency or "USD"
    lines = []

    lines.append(f"COMPANY: {company.name or company.ticker} ({company.ticker})")
    lines.append(f"SECTOR:  {company.sector or 'n/a'} — {company.industry or 'n/a'}")
    lines.append(f"COUNTRY: {company.country or 'n/a'} | CURRENCY: {cur}")
    if company.description:
        desc = company.description[:500]
        lines.append(f"BUSINESS: {desc}{'...' if len(company.description) > 500 else ''}")

    lines.append("\nCURRENT PROFITABILITY & QUALITY-RELEVANT RATIOS (TTM):")
    lines.append(f"  Gross Margin: {_pct(company.gross_margin)} | EBIT Margin: {_pct(company.ebit_margin)}"
                  f" | Net Margin: {_pct(company.net_margin)}")
    lines.append(f"  ROE: {_pct(company.roe)} | ROA: {_pct(company.roa)} | ROIC: {_pct(company.roic)}")
    lines.append(f"  FCF Yield: {_pct(company.fcf_yield)} | Gearing (NetDebt/EBITDA): {_x(company.gearing)}"
                  f" | Beta: {_x(company.beta, 2)}")

    years = company.sorted_years()[:6]
    if years:
        lines.append(f"\nANNUAL FINANCIALS ({cur} millions, most recent first, up to 6 fiscal years):")
        header = f"  {'':26} " + " ".join(f"{y:>10}" for y in years)
        lines.append(header)
        lines.append("  " + "-" * (26 + 11 * len(years)))

        def row(label, getter, fmt="M"):
            vals = []
            for y in years:
                af = company.annual_financials.get(y)
                v = getter(af) if (af and getter) else None
                if fmt == "M":
                    vals.append(f"{v/1000:>9.1f}B" if v and abs(v) >= 1000
                                else (f"{v:>9.0f}M" if v is not None else "      n/a"))
                elif fmt == "%":
                    vals.append(f"{v*100:>9.1f}%" if v is not None else "      n/a")
                elif fmt == "x":
                    vals.append(f"{v:>9.2f}x" if v is not None else "      n/a")
                elif fmt == "ps":
                    vals.append(f"{v:>9.2f}" if v is not None else "      n/a")
                elif fmt == "ratio_ocf_ni":
                    a = company.annual_financials.get(y)
                    ni = a.net_income if a else None
                    ocf = a.operating_cash_flow if a else None
                    vals.append(f"{ocf/ni:>9.2f}x" if (ocf is not None and ni not in (None, 0)) else "      n/a")
                elif fmt == "ratio_fcf_ni":
                    a = company.annual_financials.get(y)
                    ni = a.net_income if a else None
                    fcf = a.fcf if a else None
                    vals.append(f"{fcf/ni:>9.2f}x" if (fcf is not None and ni not in (None, 0)) else "      n/a")
                elif fmt == "ratio_accrual":
                    a = company.annual_financials.get(y)
                    ni = a.net_income if a else None
                    ocf = a.operating_cash_flow if a else None
                    ta = a.total_assets if a else None
                    vals.append(f"{(ni-ocf)/ta*100:>8.1f}%" if (ni is not None and ocf is not None and ta) else "      n/a")
                elif fmt == "ratio_tax":
                    a = company.annual_financials.get(y)
                    tax = a.tax_provision if a else None
                    ebt = a.income_before_tax if a else None
                    vals.append(f"{tax/ebt*100:>8.1f}%" if (tax is not None and ebt not in (None, 0)) else "      n/a")
                else:
                    vals.append(str(v) if v is not None else "n/a")
            return f"  {label:<26} " + " ".join(vals)

        lines.append(row("Revenue", lambda a: a.revenue))
        lines.append(row("Net Income", lambda a: a.net_income))
        lines.append(row("Operating Cash Flow", lambda a: a.operating_cash_flow))
        lines.append(row("CapEx", lambda a: a.capex))
        lines.append(row("Free Cash Flow", lambda a: a.fcf))
        lines.append(row("OCF / Net Income", None, "ratio_ocf_ni"))
        lines.append(row("FCF / Net Income", None, "ratio_fcf_ni"))
        lines.append(row("Sloan Accrual Ratio (NI-OCF)/Assets", None, "ratio_accrual"))
        lines.append(row("Total Assets", lambda a: a.total_assets))
        lines.append(row("Net Receivables", lambda a: a.net_receivables))
        lines.append(row("Inventory", lambda a: a.inventory))
        lines.append(row("Accounts Payable", lambda a: a.accounts_payable))
        lines.append(row("Goodwill", lambda a: a.goodwill))
        lines.append(row("Intangible Assets", lambda a: a.intangible_assets))
        lines.append(row("Chg. in Working Capital", lambda a: a.change_in_working_capital))
        lines.append(row("Current Assets", lambda a: a.current_assets))
        lines.append(row("Current Liabilities", lambda a: a.current_liabilities))
        lines.append(row("Income Before Tax", lambda a: a.income_before_tax))
        lines.append(row("Tax Provision", lambda a: a.tax_provision))
        lines.append(row("Effective Tax Rate", None, "ratio_tax"))
        lines.append(row("Depreciation & Amortization", lambda a: a.depreciation_amortization))
        lines.append(row("Extraordinary Items", lambda a: a.extraordinary_items))
        lines.append(row("Dividends Paid", lambda a: a.dividends_paid))
        lines.append(row("Gross Margin", lambda a: a.gross_margin, "%"))
        lines.append(row("EBIT Margin", lambda a: a.ebit_margin, "%"))
        lines.append(row("Net Margin", lambda a: a.net_margin, "%"))
        lines.append(row("ROE", lambda a: a.roe, "%"))
        lines.append(row("Shares Outstanding (M)", lambda a: a.shares_outstanding, "ps"))
    else:
        lines.append("\nANNUAL FINANCIALS: no historical data available for this ticker.")

    return "\n".join(lines)


def _earnings_quality_prompt_parts(
    subject: CompanyData,
    peers: dict[str, CompanyData],
) -> tuple[str, str]:
    """
    Return (cacheable_prefix, dynamic_content).

    dynamic_content concatenates the subject's and every peer's financial
    data block, preceded by an explicit universe ticker list so the model
    doesn't skip or invent companies.
    """
    tickers = [subject.ticker] + list(peers.keys())
    universe_line = "UNIVERSE (analyze exactly these companies, in this order): " + ", ".join(tickers)

    blocks = [format_company_for_eq(subject)]
    for pdata in peers.values():
        blocks.append(format_company_for_eq(pdata))

    dynamic = (
        f"{universe_line}\n\n"
        + "\n\n".join(f"--- {i+1} of {len(blocks)} ---\n{b}" for i, b in enumerate(blocks))
        + "\n\nAnalyze the companies above and return the JSON."
    )
    return _EQ_CACHEABLE, dynamic


# ── Grade / validation helpers ──────────────────────────────────────────────

def _grade_from_score(score: Optional[float]) -> str:
    if score is None:
        return "N/A"
    if score >= 97: return "A+"
    if score >= 93: return "A"
    if score >= 90: return "A-"
    if score >= 87: return "B+"
    if score >= 83: return "B"
    if score >= 80: return "B-"
    if score >= 77: return "C+"
    if score >= 73: return "C"
    if score >= 70: return "C-"
    if score >= 60: return "D"
    return "F"


def _coerce_score(v, lo=0, hi=100) -> Optional[int]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, int(round(f))))


def _coerce_str_list(v, max_items: int) -> list[str]:
    if not isinstance(v, list):
        return []
    out = []
    for item in v[:max_items]:
        if item is None:
            continue
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _validate_earnings_quality(
    analysis: dict,
    subject: CompanyData,
    peers: dict[str, CompanyData],
) -> dict:
    """
    Defensively coerce the LLM's response and recompute rank/percentile
    deterministically in Python (never trusted from the LLM). Mirrors the
    defensive-coercion pattern used in models/fisher.py's _validate_analysis
    (never assume a field the LLM returns has the expected type).
    """
    if not isinstance(analysis, dict):
        analysis = {}
    companies_raw = analysis.get("companies")
    if not isinstance(companies_raw, list):
        companies_raw = []

    by_ticker: dict[str, dict] = {}
    for entry in companies_raw:
        if not isinstance(entry, dict):
            continue
        tk = str(entry.get("ticker") or "").strip().upper()
        if tk:
            by_ticker[tk] = entry

    order = [subject.ticker] + list(peers.keys())
    names = {subject.ticker: subject.name or subject.ticker}
    names.update({t: (p.name or t) for t, p in peers.items()})

    normalized: list[dict] = []
    for tk in order:
        tk_u = (tk or "").strip().upper()
        raw = by_ticker.get(tk_u, {})
        score = _coerce_score(raw.get("earnings_quality_score"))
        subscores_raw = raw.get("subscores") if isinstance(raw.get("subscores"), dict) else {}
        subscores = {k: _coerce_score(subscores_raw.get(k)) for k in EQ_WEIGHTS}

        grade = raw.get("letter_grade")
        if not isinstance(grade, str) or not grade.strip():
            grade = _grade_from_score(score)

        confidence = raw.get("confidence")
        if confidence not in ("High", "Medium", "Low"):
            confidence = "Low" if score is None else "Medium"

        explanation = raw.get("explanation")
        explanation = str(explanation).strip() if explanation else (
            "Insufficient data was returned by the model for this company."
        )

        normalized.append({
            "ticker": tk,
            "name": raw.get("name") or names.get(tk, tk),
            "earnings_quality_score": score,
            "letter_grade": grade,
            "subscores": subscores,
            "top_strengths": _coerce_str_list(raw.get("top_strengths"), 3),
            "top_risks": _coerce_str_list(raw.get("top_risks"), 3),
            "red_flags": _coerce_str_list(raw.get("red_flags"), 6),
            "confidence": confidence,
            "explanation": explanation,
            "is_subject": tk_u == subject.ticker.strip().upper(),
        })

    # Rank by score (highest first); entries with no score sort last, in
    # their original (universe) order.
    scored = [c for c in normalized if c["earnings_quality_score"] is not None]
    unscored = [c for c in normalized if c["earnings_quality_score"] is None]
    scored.sort(key=lambda c: c["earnings_quality_score"], reverse=True)

    n_scored = len(scored)
    for i, c in enumerate(scored):
        c["rank"] = i + 1
        c["percentile"] = 100 if n_scored <= 1 else round(100 * (n_scored - 1 - i) / (n_scored - 1))
    for c in unscored:
        c["rank"] = None
        c["percentile"] = None

    ranked = scored + unscored
    analysis["companies"] = ranked
    analysis["universe_size"] = len(ranked)

    subject_entry = next((c for c in ranked if c["is_subject"]), None)
    if subject_entry:
        analysis["subject_score"] = subject_entry["earnings_quality_score"]
        analysis["subject_grade"] = subject_entry["letter_grade"]
        analysis["subject_percentile"] = subject_entry["percentile"]
        analysis["subject_confidence"] = subject_entry["confidence"]
        # Reused by the generic UI metric bar ("Rec." column / recent-reports rec).
        analysis["recommendation"] = subject_entry["letter_grade"]

    return analysis
