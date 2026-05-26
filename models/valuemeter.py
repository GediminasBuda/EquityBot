"""
valuemeter.py — ValueMeter model: Prudent Value Score vs. peer group.

Builds the LLM prompt that:
1. Defines an appropriate peer group (5-10 listed companies).
2. Collects/estimates raw financial data for each peer.
3. Calculates EBIT/EV, FCF/EV, Book/Price, Sales/EV, ROIC/ROE,
   leverage, FCF conversion, and momentum metrics.
4. Winsorizes, standardises (z-scores / percentile ranks), weights,
   and produces a total Prudent Value Score.
5. Interprets the result and gives an investment-style conclusion.

LLM output: JSON matching the schema below (see _VALUEMETER_SCHEMA).
"""

from __future__ import annotations
import logging
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an institutional value-investing research agent. "
    "You produce rigorous, explainable, peer-relative valuation scores. "
    "You prefer useful simplicity over false precision. "
    "You never let cheapness substitute for judgment. "
    "You always return a single valid JSON object — no markdown, no commentary outside the JSON."
)

# ── Cacheable prefix (static schema + instructions) ──────────────────────────
# Sent as a separate content block for Anthropic prompt caching.

_VALUEMETER_SCHEMA = """\
Create a Prudent Value Score for the subject company versus an appropriate peer group.
Return a single JSON object with EXACTLY this structure — no markdown, no keys outside it:

{
  "peer_group": [
    {
      "ticker":            "<exchange ticker, e.g. SAP.DE>",
      "name":              "<full company name>",
      "currency":          "<reporting currency, e.g. EUR>",
      "reason_included":   "<1-2 sentences: why this peer is appropriate>",
      "market_cap_bn":     <float, market cap in billions of reporting currency, or null>,
      "ev_bn":             <float, enterprise value in billions, or null>,
      "revenue_bn":        <float, latest annual revenue in billions, or null>,
      "ebit_bn":           <float, latest annual EBIT or operating income in billions, or null>,
      "ebitda_bn":         <float, latest annual EBITDA in billions, or null>,
      "net_income_bn":     <float, latest annual net income in billions, or null>,
      "fcf_bn":            <float, latest annual free cash flow in billions, or null>,
      "book_value_bn":     <float, latest book value of equity in billions, or null>,
      "net_debt_bn":       <float, net debt in billions (negative = net cash), or null>,
      "roic_pct":          <float, ROIC as percentage e.g. 12.5, or null>,
      "roe_pct":           <float, ROE as percentage, or null>,
      "interest_cover":    <float, EBIT / interest expense ratio, or null>,
      "net_debt_ebitda":   <float, Net Debt / EBITDA ratio, or null>,
      "ebit_ev":           <float, EBIT / EV as percentage e.g. 8.3, or null>,
      "fcf_ev":            <float, FCF / EV as percentage, or null>,
      "book_price":        <float, Book Value / Market Cap ratio, or null>,
      "sales_ev":          <float, Revenue / EV ratio, or null>,
      "fcf_net_income":    <float, FCF / Net Income ratio, or null>,
      "momentum_6m_pct":   <float, 6-month share price return as percentage, or null>,
      "price":             <float, current share price, or null>,
      "ebit_margin_pct":   <float, EBIT margin as percentage e.g. 18.4, or null>,
      "source":            "<data source citation e.g. 'FY2024 annual report, Bloomberg'>",
      "is_subject":        <true if this is the subject company, false for peers>
    }
  ],

  "excluded_peers": [
    {
      "name":   "<company name>",
      "reason": "<1 sentence: why excluded>"
    }
  ],

  "scores": [
    {
      "ticker":           "<ticker>",
      "name":             "<name>",
      "currency":         "<currency>",
      "price":            <float or null>,
      "market_cap_bn":    <float or null>,
      "roe_pct":          <float or null>,
      "ebit_margin_pct":  <float or null>,
      "ev_sales":         <float or null, EV/Sales multiple>,
      "cheapness_score":  <float 0-100: EBIT/EV 20% + FCF/EV 20% + Book/Price 10% + Sales/EV 10%>,
      "quality_score":    <float 0-100: ROIC/ROE 15% + balance sheet safety 10% + FCF conversion 5%>,
      "momentum_score":   <float 0-100: estimate revision or 6-12m momentum 10%>,
      "total_score":      <float 0-100: weighted combination of all factors>,
      "percentile":       <integer 1-100: percentile rank within peer group>,
      "rank":             <integer: rank within peer group, 1=best>,
      "is_subject":       <true if this is the subject company>
    }
  ],

  "subject_ticker": "<ticker of subject company>",

  "interpretation": {
    "is_cheap":            "<Yes | Marginally | No>",
    "cheap_reason":        "<2-3 sentences: which metrics drive the cheapness signal>",
    "verdict":             "<value_opportunity | value_trap | cyclical_mirage | fairly_priced_compounder>",
    "verdict_explanation": "<3-4 sentences explaining the verdict>",
    "top_drivers":         ["<metric: specific explanation>", "<metric: ...>", "<metric: ...>"],
    "top_risks":           ["<risk 1>", "<risk 2>", "<risk 3>"]
  },

  "conclusion": {
    "rating":                    "<Attractive | Neutral | Unattractive>",
    "confidence":                "<High | Medium | Low>",
    "manual_checks":             "<2-3 sentences: what a human analyst should verify before acting>",
    "what_changes_conclusion":   "<2-3 sentences: what data or events would flip the verdict>"
  }
}

Scoring methodology:
- For each metric, rank all companies within the peer group (higher = better value).
- Convert ranks to percentile scores (0=worst, 100=best).
- Apply weights: EBIT/EV 20%, FCF/EV 20%, Book/Price 10%, Sales/EV 10%,
  ROIC/ROE 15%, Balance sheet safety (low leverage) 10%, FCF/NI conversion 5%,
  Momentum 10%. Total = 100%.
- Winsorise extreme outliers at 5th/95th percentile before ranking.
- For leverage metrics: lower net_debt_ebitda = better score (invert ranking).
- If a metric is null for a company, exclude it from that metric's ranking
  (company gets the group median score for that metric).
- The subject company IS included in the peer group for ranking purposes.

Important rules:
- Do NOT mechanically reward negative earnings, negative book value,
  or distorted one-off results.
- Mark data as null when it is genuinely unavailable or meaningless.
- The scores table must include ALL companies from peer_group (peers + subject).
- Total score must be a weighted combination matching the methodology above.

=== SUBJECT COMPANY FINANCIAL DATA FOLLOWS ==="""


def _build_valuemeter_prompt(company: CompanyData) -> tuple[str, str]:
    """
    Return (cacheable_prefix, dynamic_prompt).

    cacheable_prefix — static schema + instructions (same for all companies).
    dynamic_prompt   — company-specific EODHD data.
    """
    # ── Dynamic section: subject company data ─────────────────────────────────
    lines: list[str] = []

    # Identity
    ccy = company.currency or "?"
    lines.append(f"Subject company: {company.name or company.ticker}")
    lines.append(f"Ticker: {company.ticker}")
    lines.append(f"Sector: {company.sector or 'n/a'} | Industry: {company.industry or 'n/a'}")
    lines.append(f"Country: {company.country or 'n/a'} | Currency: {ccy}")
    if company.description:
        desc = company.description[:600].replace("\n", " ")
        lines.append(f"Business description: {desc}")
    lines.append("")

    # Current market data
    def _b(v):
        if v is None: return "n/a"
        return f"{v/1e9:,.2f}B {ccy}" if abs(v) >= 1e9 else f"{v/1e6:,.0f}M {ccy}"

    def _pct(v):
        if v is None: return "n/a"
        return f"{v*100:.1f}%"

    def _x(v):
        if v is None: return "n/a"
        return f"{v:.2f}x"

    lines.append("== Current Market Data ==")
    lines.append(f"Price: {company.current_price} {ccy}")
    lines.append(f"Market Cap: {_b(company.market_cap)}")
    lines.append(f"Enterprise Value: {_b(company.enterprise_value)}")
    lines.append(f"Shares Outstanding: {company.shares_outstanding:,.0f}" if company.shares_outstanding else "Shares Outstanding: n/a")
    lines.append(f"Beta: {company.beta:.2f}" if company.beta else "Beta: n/a")
    lines.append("")

    lines.append("== Valuation Multiples (current) ==")
    lines.append(f"P/E: {_x(company.pe_ratio)}")
    lines.append(f"Forward P/E: {_x(company.forward_pe)}")
    lines.append(f"EV/EBITDA: {_x(company.ev_ebitda)}")
    lines.append(f"EV/EBIT: {_x(company.ev_ebit)}")
    lines.append(f"EV/Sales: {_x(company.ev_sales)}")
    lines.append(f"P/B: {_x(company.price_to_book)}")
    lines.append(f"FCF Yield: {_pct(company.fcf_yield)}")
    lines.append(f"Dividend Yield: {_pct(company.dividend_yield)}")
    lines.append("")

    lines.append("== TTM Profitability ==")
    lines.append(f"Gross Margin: {_pct(company.gross_margin)}")
    lines.append(f"EBIT Margin: {_pct(company.ebit_margin)}")
    lines.append(f"EBITDA Margin: {_pct(company.ebitda_margin)}")
    lines.append(f"Net Margin: {_pct(company.net_margin)}")
    lines.append(f"ROE: {_pct(company.roe)}")
    lines.append(f"ROA: {_pct(company.roa)}")
    lines.append(f"ROIC: {_pct(company.roic)}" if hasattr(company, 'roic') else "")
    lines.append("")

    # Annual history
    years = company.sorted_years()
    recent = list(reversed(years[:8]))  # 8 most recent, chronological
    if recent:
        lines.append("== Annual Financial History ==")
        header_cols = ["Revenue", "EBIT", "EBITDA", "Net Income", "FCF",
                       "Net Debt", "ROE", "EBIT Margin"]
        lines.append("Year | " + " | ".join(header_cols))
        for yr in recent:
            af = company.annual_financials.get(yr)
            if not af:
                continue
            def _m(v):
                if v is None: return "n/a"
                return f"{v/1000:.0f}B" if abs(v) >= 1000 else f"{v:.0f}M"
            def _p(v):
                if v is None: return "n/a"
                return f"{v*100:.1f}%"
            row = [
                _m(af.revenue), _m(af.ebit), _m(af.ebitda), _m(af.net_income),
                _m(af.fcf), _m(af.net_debt), _p(af.roe), _p(af.ebit_margin),
            ]
            lines.append(f"{yr} | " + " | ".join(row))
        lines.append("")

    # Forward estimates
    fe = company.forward_estimates
    if fe:
        lines.append("== Forward Estimates (next FY) ==")
        if fe.revenue_estimate:
            lines.append(f"Revenue estimate: {_b(fe.revenue_estimate)}")
        if fe.eps_estimate:
            lines.append(f"EPS estimate: {fe.eps_estimate:.2f}")
        if fe.ebitda_estimate:
            lines.append(f"EBITDA estimate: {_b(fe.ebitda_estimate)}")
        if fe.revenue_growth_est:
            lines.append(f"Revenue growth est: {_pct(fe.revenue_growth_est)}")
        if fe.forward_pe:
            lines.append(f"Forward P/E: {_x(fe.forward_pe)}")
        lines.append("")

    # Debt and balance sheet
    latest = company.latest_annual()
    if latest:
        lines.append("== Balance Sheet (latest annual) ==")
        lines.append(f"Total Assets: {_b(latest.total_assets)}" if latest.total_assets else "")
        lines.append(f"Total Debt: {_b(latest.total_debt)}" if latest.total_debt else "")
        lines.append(f"Cash: {_b(latest.cash)}" if latest.cash else "")
        lines.append(f"Net Debt: {_b(latest.net_debt)}" if latest.net_debt else "")
        lines.append(f"Equity: {_b(latest.equity)}" if latest.equity else "")
        if latest.total_debt and latest.ebitda and latest.ebitda != 0:
            nd_ebitda = (latest.net_debt or 0) / latest.ebitda
            lines.append(f"Net Debt/EBITDA: {nd_ebitda:.1f}x")
        lines.append("")

    lines.append(
        "Using the subject company data above plus your knowledge of this industry, "
        "identify 5-10 listed peers, collect their financial data, and produce the "
        "Prudent Value Score analysis as specified in the schema."
    )

    dynamic = "\n".join(l for l in lines if l is not None)
    return _VALUEMETER_SCHEMA, dynamic
