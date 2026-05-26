"""
valuemeter.py — ValueMeter model: Prudent Value Score vs. peer group.

Two-step LLM architecture:
  Call 1 (lightweight): LLM identifies 5-8 peer tickers.
  Python:               Fetches real EODHD data for each peer and extracts metrics.
  Call 2 (main):        LLM receives REAL data, computes weighted scores,
                        writes verdict + interpretation + conclusion.

This ensures the quantitative analysis is grounded in actual EODHD data,
not LLM training-time knowledge.
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
    "Return only valid JSON — no markdown, no text outside the JSON object."
)

# ─────────────────────────────────────────────────────────────────────────────
# CALL 1 — Peer identification
# ─────────────────────────────────────────────────────────────────────────────

_PEER_ID_SCHEMA = """\
Identify 5-8 listed peers for the subject company below.

Rules:
- Prioritise same industry, similar business model, geography, size, and accounting characteristics.
- Use Yahoo Finance ticker format (e.g. SAP.DE, AAPL, WKL.AS, RHM.DE).
- Include only publicly listed companies with meaningful trading volume.
- Exclude ETFs, indices, private companies, and clearly inappropriate comparisons.

Return a single JSON object — no markdown, no extra text:

{
  "peers": [
    {
      "ticker":  "<Yahoo Finance ticker>",
      "name":    "<full company name>",
      "reason":  "<1-2 sentences: why this peer is appropriate>"
    }
  ],
  "excluded": [
    {
      "name":   "<company name>",
      "reason": "<1 sentence: why excluded>"
    }
  ]
}

Include 2-3 excluded examples to show you considered alternatives.
=== SUBJECT COMPANY ==="""


def build_peer_id_prompt(company: CompanyData) -> tuple[str, str]:
    """Return (cacheable_prefix, dynamic) for the peer-identification LLM call."""
    dynamic = (
        f"Name: {company.name or company.ticker}\n"
        f"Ticker: {company.ticker}\n"
        f"Sector: {company.sector or 'n/a'}\n"
        f"Industry: {company.industry or 'n/a'}\n"
        f"Country: {company.country or 'n/a'}\n"
        f"Currency: {company.currency or 'n/a'}\n"
        f"Market Cap: {f'{company.market_cap/1000:.1f}B' if company.market_cap else 'n/a'}\n"
    )
    if company.description:
        dynamic += f"Description: {company.description[:400]}\n"
    return _PEER_ID_SCHEMA, dynamic


# ─────────────────────────────────────────────────────────────────────────────
# Python metric extraction from EODHD CompanyData
# ─────────────────────────────────────────────────────────────────────────────

def extract_metrics(company: CompanyData, is_subject: bool = False,
                    reason: str = "") -> dict:
    """
    Extract all ValueMeter metrics from a CompanyData object.
    All monetary values returned in billions of reporting currency.
    All percentages returned as floats (e.g. 18.4 for 18.4%).
    """
    la = company.latest_annual()

    def _sdiv(a, b):
        try:
            if a is None or b is None or abs(float(b)) < 1e-9:
                return None
            return float(a) / float(b)
        except Exception:
            return None

    def _pct_field(v):
        """Convert decimal roe/margin (0.xx) to percentage (xx.x)."""
        if v is None:
            return None
        try:
            f = float(v)
            # EODHD stores these as decimals (0.15 = 15%)
            return round(f * 100, 2)
        except Exception:
            return None

    mkt = company.market_cap      # millions
    ev  = company.enterprise_value  # millions

    rev      = la.revenue      if la else None  # millions
    ebit     = la.ebit         if la else None
    ebitda   = la.ebitda       if la else None
    ni       = la.net_income   if la else None
    fcf      = la.fcf          if la else None
    equity   = la.total_equity if la else None
    net_debt = la.net_debt     if la else None

    # Yield metrics (expressed as percentages)
    ebit_ev   = _sdiv(ebit,  ev)  # ebit/ev raw ratio → *100 for %
    fcf_ev    = _sdiv(fcf,   ev)
    sales_ev  = _sdiv(rev,   ev)
    book_price = _sdiv(equity, mkt)

    ebit_ev_pct  = round(ebit_ev  * 100, 2) if ebit_ev  is not None else None
    fcf_ev_pct   = round(fcf_ev   * 100, 2) if fcf_ev   is not None else None

    fcf_ni       = _sdiv(fcf, ni)
    nd_ebitda    = _sdiv(net_debt, ebitda)

    # ROE / ROIC — CompanyData stores as decimal
    roe  = _pct_field(company.roe  or (la.roe  if la else None))
    roic = _pct_field(getattr(company, "roic", None) or (la.roic if la else None))
    quality_return = roic if roic is not None else roe

    ebit_margin = _pct_field(company.ebit_margin or (la.ebit_margin if la else None))

    # Interest cover: not stored directly; estimate from ebit/interest
    # EODHD provides interest_expense on AnnualFinancials if available
    interest_cover = None
    if la and ebit and hasattr(la, "interest_expense") and la.interest_expense:
        try:
            interest_cover = abs(ebit / la.interest_expense)
        except Exception:
            pass

    # 6-month momentum: not available from static EODHD snapshot
    # Use None — LLM will note the gap
    momentum_6m = None

    return {
        "ticker":           company.ticker or "?",
        "name":             company.name   or "",
        "currency":         company.currency or "?",
        "is_subject":       is_subject,
        "reason_included":  reason,
        "price":            company.current_price,
        # Raw financials (billions)
        "market_cap_bn":    round(mkt    / 1000, 2) if mkt    else None,
        "ev_bn":            round(ev     / 1000, 2) if ev     else None,
        "revenue_bn":       round(rev    / 1000, 2) if rev    else None,
        "ebit_bn":          round(ebit   / 1000, 2) if ebit   else None,
        "ebitda_bn":        round(ebitda / 1000, 2) if ebitda else None,
        "net_income_bn":    round(ni     / 1000, 2) if ni     else None,
        "fcf_bn":           round(fcf    / 1000, 2) if fcf    else None,
        "book_value_bn":    round(equity / 1000, 2) if equity else None,
        "net_debt_bn":      round(net_debt / 1000, 2) if net_debt is not None else None,
        # Yield metrics (%)
        "ebit_ev":          ebit_ev_pct,
        "fcf_ev":           fcf_ev_pct,
        "book_price":       round(book_price, 3) if book_price is not None else None,
        "sales_ev":         round(sales_ev, 3)  if sales_ev  is not None else None,
        # Quality / safety
        "roe_pct":          roe,
        "roic_pct":         roic,
        "quality_return_pct": quality_return,   # roic if available, else roe
        "ebit_margin_pct":  ebit_margin,
        "net_debt_ebitda":  round(nd_ebitda, 2) if nd_ebitda is not None else None,
        "interest_cover":   round(interest_cover, 1) if interest_cover else None,
        # Conversion
        "fcf_net_income":   round(fcf_ni, 2) if fcf_ni is not None else None,
        # Momentum
        "momentum_6m_pct":  momentum_6m,
        # EV/Sales multiple (for summary table)
        "ev_sales_multiple": round(1 / sales_ev, 2) if sales_ev and sales_ev > 0 else (
            company.ev_sales if company.ev_sales else None
        ),
        # Data source
        "source": "EODHD",
    }


# ─────────────────────────────────────────────────────────────────────────────
# CALL 2 — Scoring + Interpretation
# ─────────────────────────────────────────────────────────────────────────────

_SCORING_SCHEMA = """\
You are given real EODHD financial data for the subject company and its peers.
Compute the Prudent Value Score and provide interpretation.

Scoring methodology:
1. For each metric, rank all companies (including the subject) within the group.
   Convert to percentile scores: 0 = worst, 100 = best.
2. For yield metrics (EBIT/EV, FCF/EV, Book/Price, Sales/EV): higher = better.
3. For quality (ROIC/ROE): higher = better.
4. For leverage (Net Debt/EBITDA): lower = better (invert rank).
5. For interest cover: higher = better.
6. For FCF/NI conversion: closer to 1.0 = better (penalise both <0.5 and >2.0).
7. For momentum: higher = better. If null for all companies, set all to 50.
8. Winsorise at 5th/95th percentile before ranking.
9. If a metric is null for a company, assign that company the group median percentile.
10. Do NOT reward negative EBIT, negative equity, or one-off distorted results.

Weights:
  EBIT/EV:              20%
  FCF/EV:               20%
  Book/Price:           10%
  Sales/EV:             10%
  ROIC or ROE:          15%
  Balance sheet safety: 10%  (combination of Net Debt/EBITDA + Interest Cover)
  FCF/NI conversion:     5%
  Momentum (6m):        10%
  TOTAL:               100%

Sub-scores (each 0-100):
  cheapness_score  = EBIT/EV 20% + FCF/EV 20% + Book/Price 10% + Sales/EV 10%  (out of 60% → rescale to 100)
  quality_score    = ROIC/ROE 15% + balance sheet 10% + FCF/NI 5%               (out of 30% → rescale to 100)
  momentum_score   = momentum 10%                                                (out of 10% → rescale to 100)
  total_score      = weighted combination of all factors (0-100)

Return a single JSON object:

{
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
      "cheapness_score":  <float 0-100>,
      "quality_score":    <float 0-100>,
      "momentum_score":   <float 0-100>,
      "total_score":      <float 0-100>,
      "percentile":       <integer 1-100>,
      "rank":             <integer, 1=best value>,
      "is_subject":       <true|false>
    }
  ],
  "subject_ticker": "<ticker of subject company>",
  "interpretation": {
    "is_cheap":            "<Yes | Marginally | No>",
    "cheap_reason":        "<2-3 sentences citing specific metrics that drive the signal>",
    "verdict":             "<value_opportunity | value_trap | cyclical_mirage | fairly_priced_compounder>",
    "verdict_explanation": "<3-4 sentences explaining the verdict>",
    "top_drivers":         ["<metric: specific explanation>", "<...>", "<...>"],
    "top_risks":           ["<risk 1>", "<risk 2>", "<risk 3>"]
  },
  "conclusion": {
    "rating":                  "<Attractive | Neutral | Unattractive>",
    "confidence":              "<High | Medium | Low>",
    "manual_checks":           "<2-3 sentences: what to verify before acting>",
    "what_changes_conclusion": "<2-3 sentences: what would flip the verdict>"
  }
}

All companies in the data below must appear in the scores array.
=== PEER DATA (EODHD) ==="""


def _fmt_metrics_block(m: dict) -> str:
    """Format one company's extracted metrics into a compact text block."""
    def _f(v, dec=2, suffix=""):
        if v is None:
            return "n/a"
        return f"{v:.{dec}f}{suffix}"

    lines = [
        f"  Ticker: {m['ticker']}  |  Name: {m['name']}  |  CCY: {m['currency']}"
        f"  |  {'SUBJECT ★' if m.get('is_subject') else 'Peer'}",
        f"  Price: {_f(m.get('price'), 2)}  |  Mkt Cap: {_f(m.get('market_cap_bn'), 1)}B"
        f"  |  EV: {_f(m.get('ev_bn'), 1)}B",
        f"  Revenue: {_f(m.get('revenue_bn'), 1)}B  |  EBIT: {_f(m.get('ebit_bn'), 1)}B"
        f"  |  EBITDA: {_f(m.get('ebitda_bn'), 1)}B  |  Net Income: {_f(m.get('net_income_bn'), 1)}B",
        f"  FCF: {_f(m.get('fcf_bn'), 1)}B  |  Book Equity: {_f(m.get('book_value_bn'), 1)}B"
        f"  |  Net Debt: {_f(m.get('net_debt_bn'), 1)}B",
        f"  EBIT/EV: {_f(m.get('ebit_ev'), 1)}%  |  FCF/EV: {_f(m.get('fcf_ev'), 1)}%"
        f"  |  Book/Price: {_f(m.get('book_price'), 3)}  |  Sales/EV: {_f(m.get('sales_ev'), 3)}",
        f"  ROIC/ROE: {_f(m.get('quality_return_pct'), 1)}%"
        f"  |  EBIT Margin: {_f(m.get('ebit_margin_pct'), 1)}%"
        f"  |  ND/EBITDA: {_f(m.get('net_debt_ebitda'), 1)}x"
        f"  |  Int. Cover: {_f(m.get('interest_cover'), 1)}x",
        f"  FCF/NI: {_f(m.get('fcf_net_income'), 2)}  |  Momentum 6m: {_f(m.get('momentum_6m_pct'), 1)}%",
        f"  Source: {m.get('source', 'EODHD')}",
    ]
    return "\n".join(lines)


def build_scoring_prompt(
    all_metrics: list[dict],
) -> tuple[str, str]:
    """
    Return (cacheable_prefix, dynamic_prompt) for the scoring LLM call.
    all_metrics: list of dicts from extract_metrics(), subject first.
    """
    blocks = [_fmt_metrics_block(m) for m in all_metrics]
    dynamic = "\n\n".join(blocks)
    return _SCORING_SCHEMA, dynamic
