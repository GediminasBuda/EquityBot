"""
overview.py — Investment Memo model ("Overview").

Produces a 3-page PDF investment memo in the style of the WKL sample:
  Page 1: Header + Financial Table + Investment Snapshot
  Page 2: Header + Bull Case + Bear Case + Recommendation
  Page 3: Header + Peer Comparison Table + Investment Checklist

Usage:
    from models.overview import OverviewModel
    path = OverviewModel().run("WKL.AS")
    print(f"Report saved to: {path}")

CLI:
    python models/overview.py WKL.AS
    python models/overview.py AAPL --peers MSFT GOOGL AMZN
"""

from __future__ import annotations
import json
import logging
import sys
import os
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from data_sources.data_manager import DataManager
from data_sources.base import CompanyData, AnnualFinancials
from agents.llm_client import LLMClient
from config import OUTPUTS_DIR, ADVERSARIAL_MODE

logger = logging.getLogger(__name__)


# ── System prompt: loaded from framework JSON, fallback to hardcoded ──────────

_SYSTEM_PROMPT_FALLBACK = """You are "Your Humble EquityBot" — a disciplined value investing analyst.

Your core philosophy:
- Truth-seeking over narrative comfort. Facts first, stories second.
- You operate like an owner-operator, not a trader. Decade-scale horizon.
- Separate temporary underperformance from permanent thesis breaks.
- Prioritize business quality + moat + valuation + management integrity.
- Devil's Advocate is not optional — you always stress-test your own thesis.
- Flag accounting red flags explicitly when present.
- Your language is precise, direct, and free of financial jargon padding.

You never speculate beyond the data. When data is unavailable, you say so.
You are not a cheerleader. Balanced analysis is your standard.
"""


def _load_system_prompt() -> str:
    try:
        from framework_manager import FrameworkManager
        return FrameworkManager().get_system_prompt("overview", _SYSTEM_PROMPT_FALLBACK)
    except Exception:
        return _SYSTEM_PROMPT_FALLBACK


SYSTEM_PROMPT = _load_system_prompt()


# ── Financial data formatter (data → LLM-readable text) ──────────────────────

def _format_financials_for_llm(company: CompanyData) -> str:
    """Build a clean financial summary string to include in the LLM prompt."""
    cur = company.currency or "USD"
    lines = []

    lines.append(f"COMPANY: {company.name or company.ticker} ({company.ticker})")
    lines.append(f"SECTOR:  {company.sector or 'n/a'} — {company.industry or 'n/a'}")
    lines.append(f"COUNTRY: {company.country or 'n/a'} | CURRENCY: {cur}")
    lines.append(f"EXCHANGE: {company.exchange or 'n/a'}")
    if company.description:
        desc = company.description[:600]
        lines.append(f"\nBUSINESS: {desc}{'...' if len(company.description) > 600 else ''}")

    lines.append(f"\nMARKET DATA (as of {company.as_of_date or 'n/a'}):")
    lines.append(f"  Current Price:   {company.current_price} {company.currency_price or cur}")
    lines.append(f"  Market Cap:      {_b(company.market_cap)} {cur}M")
    lines.append(f"  Enterprise Value:{_b(company.enterprise_value)} {cur}M")
    lines.append(f"  Shares Out.:     {_m(company.shares_outstanding)} M")

    lines.append(f"\nVALUATION MULTIPLES (current):")
    lines.append(f"  P/E:       {_x(company.pe_ratio)}  | Forward P/E: {_x(company.forward_pe)}")
    lines.append(f"  EV/EBIT:   {_x(company.ev_ebit)}  | EV/EBITDA: {_x(company.ev_ebitda)}")
    lines.append(f"  EV/Sales:  {_x(company.ev_sales)} | P/Book: {_x(company.price_to_book)}")
    lines.append(f"  Div Yield: {_pct(company.dividend_yield)} | FCF Yield: {_pct(company.fcf_yield)}")
    lines.append(f"  Beta:      {_x(company.beta, 2)}")

    lines.append(f"\nPROFITABILITY (TTM):")
    lines.append(f"  Gross Margin:  {_pct(company.gross_margin)}")
    lines.append(f"  EBIT Margin:   {_pct(company.ebit_margin)}")
    lines.append(f"  EBITDA Margin: {_pct(company.ebitda_margin)}")
    lines.append(f"  Net Margin:    {_pct(company.net_margin)}")
    lines.append(f"  ROE:           {_pct(company.roe)}")
    lines.append(f"  ROA:           {_pct(company.roa)}")
    lines.append(f"  Gearing:       {_x(company.gearing)} (Net Debt/EBITDA)")
    lines.append(f"  Net Debt:      {_b(company.net_debt)} {cur}M")

    years = company.sorted_years()[:6]
    if years:
        lines.append(f"\nANNUAL FINANCIALS ({cur} millions, most recent first):")
        header = f"  {'':22} " + " ".join(f"{y:>10}" for y in years)
        lines.append(header)
        lines.append("  " + "-" * (22 + 11 * len(years)))

        def row(label, getter, fmt="M"):
            vals = []
            for y in years:
                af = company.annual_financials.get(y)
                v  = getter(af) if af else None
                if fmt == "M":    vals.append(f"{v/1000:>9.1f}B" if v and abs(v) >= 1000 else (f"{v:>9.0f}M" if v is not None else "      n/a"))
                elif fmt == "%":  vals.append(f"{v*100:>9.1f}%" if v is not None else "      n/a")
                elif fmt == "x":  vals.append(f"{v:>9.1f}x" if v is not None else "      n/a")
                elif fmt == "ps": vals.append(f"{v:>9.2f}" if v is not None else "      n/a")
            return f"  {label:<22} " + " ".join(vals)

        lines.append(row("Revenue",         lambda a: a.revenue))
        lines.append(row("EBITDA",          lambda a: a.ebitda))
        lines.append(row("EBIT",            lambda a: a.ebit))
        lines.append(row("Net Income",      lambda a: a.net_income))
        lines.append(row("EPS (diluted)",   lambda a: a.eps_diluted, "ps"))
        lines.append(row("FCF",             lambda a: a.fcf))
        lines.append(row("Net Debt",        lambda a: a.net_debt))
        lines.append(row("Shares Out (M)",  lambda a: a.shares_outstanding, "ps"))
        lines.append(row("ROE",             lambda a: a.roe, "%"))
        lines.append(row("Net Margin",      lambda a: a.net_margin, "%"))
        lines.append(row("EBIT Margin",     lambda a: a.ebit_margin, "%"))

    # Revenue CAGRs
    c3 = company.revenue_cagr(3)
    c5 = company.revenue_cagr(5)
    lines.append(f"\nREVENUE CAGR: 3yr={_pct(c3)}  5yr={_pct(c5)}")

    # Forward estimates (analyst consensus)
    fe = company.forward_estimates
    if fe is not None:
        lines.append(
            f"\nANALYST CONSENSUS ESTIMATES ({fe.year}E, {fe.analyst_count or '?'} analysts)"
            f" [THESE ARE FUTURE FORECASTS — NOT REPORTED ACTUALS]:"
        )
        if fe.revenue is not None:
            lines.append(f"  Revenue {fe.year}E:  {_b(fe.revenue)} {cur}M"
                         + (f"  (growth: {_pct(fe.revenue_growth_yoy)})" if fe.revenue_growth_yoy else ""))
        if fe.eps_diluted is not None:
            lines.append(f"  EPS {fe.year}E:      {fe.eps_diluted:.2f}"
                         + (f"  (growth: {_pct(fe.eps_growth_yoy)})" if fe.eps_growth_yoy else ""))
        if fe.net_income is not None:
            lines.append(f"  Net Income {fe.year}E: {_b(fe.net_income)} {cur}M")
        if fe.pe_ratio is not None:
            lines.append(f"  Forward P/E:   {_x(fe.pe_ratio)}")
        if fe.ev_sales is not None:
            lines.append(f"  Fwd EV/Sales:  {_x(fe.ev_sales)}")

    return "\n".join(lines)


# ── LLM prompts ───────────────────────────────────────────────────────────────

# ── Cached prompt prefix — fixed framework instructions + output schema ────────
# This block never changes regardless of which company is analysed, so Anthropic
# can cache it after the first call (5-minute TTL, 90% cost reduction on re-reads).
# It must be > 1024 tokens combined with the system prompt to qualify for caching.
_OVERVIEW_CACHEABLE = """\
Produce a full Investment Memo analysis for the company data provided below.
Return a single JSON object with exactly these keys.

Required JSON output:
{
  "recommendation": "BUY or HOLD or SELL",

  "recommendation_rationale": "100-150 word rationale. State the key decision variable: what price or condition makes this attractive/unattractive. Include a rough fair value range if calculable from the data. Reference the most important financial metric that drives your view.",

  "suggested_peers": [
    {"ticker": "PEER1.EX", "name": "Peer Company Name", "exchange": "EXCHANGE_CODE"},
    {"ticker": "PEER2",    "name": "Peer 2",             "exchange": "NYSE"},
    {"ticker": "PEER3.EX", "name": "Peer 3",             "exchange": "EXCHANGE_CODE"},
    {"ticker": "PEER4.EX", "name": "Peer 4",             "exchange": "EXCHANGE_CODE"}
  ],

  "fun_facts": [
    "Specific, verifiable, surprising fact about the company (not just 'founded in X')",
    "Another genuinely interesting fact",
    "Third fact — can be about scale, market position, history, or culture"
  ],

  "snapshot": "800-1000 word Investment Snapshot. Cover: (1) business model and what the company actually does, (2) industry structure and value chain position, (3) competitive moat — be specific about sources, (4) key growth drivers and risks to them, (5) capital allocation quality and management track record, (6) current financial highlights and what has driven recent results. Write as a senior analyst briefing a long-term investor. Be factual, no padding.",

  "bull_case": "400-600 word bull case from a value investor's perspective. Structure: (1) why the current valuation is attractive relative to quality and growth, (2) what the market is underestimating or mispricing, (3) the primary moat that protects returns, (4) the specific scenario under which this becomes a 3-5x over a decade. Be specific — cite the multiples, margins, and growth rates in the financial data. No generic optimism.",

  "bear_case": "400-600 word Devil's Advocate bear case. Structure: (1) what the current price already assumes — what multiple compression or earnings miss would hurt, (2) structural risks to the moat that are not yet in financial statements, (3) hidden accounting or quality risks if any, (4) management or capital allocation concerns, (5) macro or regulatory threats specific to this business. This is the stress test. Be genuinely adversarial."
}

Rules:
- suggested_peers: 4-6 most relevant direct competitors or closest sector comparables.
  Use Yahoo Finance ticker format (e.g. RELX.L for London, SAP.DE for Frankfurt, WKL.AS for Amsterdam).
- All numbers you reference must come from the financial data provided — do not invent figures.
- Write in English. Professional analyst tone. No bullet points inside the text fields — prose only.
- CRITICAL: Clearly distinguish between historical actuals (from the ANNUAL FINANCIALS table)
  and forward estimates (from the ANALYST CONSENSUS ESTIMATES section, labelled with "E" suffix
  e.g. "2026E"). Never attribute a forward growth estimate to a past fiscal year.
- If RECENT NEWS is provided, incorporate the most relevant developments into snapshot,
  bull_case, and bear_case. Do not fabricate news items.
- If COUNTRY MACRO data is provided, use it to contextualize the operating environment
  in the snapshot and bear_case.

Analytical standards:
- Moat claims must cite specific evidence: pricing power data, market share trends,
  switching cost analysis, or network effect metrics. "Wide moat" unsupported by data
  is not acceptable.
- Valuation commentary must anchor to the specific multiples in the financial block.
  Generic statements like "trading at a discount" are not acceptable without citing
  the actual P/E, EV/EBIT, or FCF yield versus historical or peer averages.
- The bear case must be genuinely adversarial — if the bear case reads like a bull
  case with mild caveats, it has failed. Stress-test the thesis properly.
- Recommendation must be actionable and grounded: BUY means you believe the risk/reward
  is favourable at the current price. HOLD means fair value is approximately reached.
  SELL means the current price embeds too much optimism or the thesis is broken.

=== COMPANY DATA FOLLOWS ==="""


def _overview_prompt_parts(
    company: CompanyData,
    news_block: str = "",
    macro_country_block: str = "",
) -> tuple[str, str]:
    """
    Return (cacheable_prefix, dynamic_content).

    cacheable_prefix  — fixed framework instructions + schema (same for every company).
                        Pass as cacheable_prefix= to llm.generate_json() so Anthropic
                        caches it and re-reads at 90% discount on subsequent calls.
    dynamic_content   — company-specific financials, macro, news.
    """
    fin_data = _format_financials_for_llm(company)
    cur = company.currency or "USD"

    from data_sources.fred_adapter import get_macro_block
    macro_block = get_macro_block()
    macro_section = f"\n\n{macro_block}" if macro_block else ""
    news_section = f"\n\n{news_block}" if news_block else ""
    country_macro_section = f"\n\n{macro_country_block}" if macro_country_block else ""

    dynamic = (
        f"{fin_data}{macro_section}{news_section}{country_macro_section}\n\n"
        f"Currency is {cur} throughout.\n"
        f"Analyze the company data above and return the JSON."
    )
    return _OVERVIEW_CACHEABLE, dynamic


def _build_overview_prompt(company: CompanyData, news_block: str = "", macro_country_block: str = "") -> str:
    """Return the full prompt as a single string (used by adversarial mode)."""
    cacheable, dynamic = _overview_prompt_parts(company, news_block, macro_country_block)
    return cacheable + "\n\n" + dynamic


# ── Checklist calculator ──────────────────────────────────────────────────────

def _calculate_checklist(company: CompanyData) -> list[dict]:
    """
    Compute the investment checklist criteria.
    Returns list of {criterion, threshold, actual, pass} dicts.
    """
    la = company.latest_annual()
    checks = []

    # 1. Revenue 3Y CAGR > 5%
    cagr3 = company.revenue_cagr(3)
    checks.append({
        "criterion": "Sales 3Y CAGR > 5.0%",
        "threshold": 5.0,
        "actual": f"{cagr3*100:.1f}%" if cagr3 is not None else "n/a",
        "pass": cagr3 is not None and cagr3 > 0.05,
    })

    # 2. EBIT Margin > 10%
    em = company.ebit_margin or (la.ebit_margin if la else None)
    checks.append({
        "criterion": "EBIT Margin > 10.0%",
        "threshold": 10.0,
        "actual": f"{em*100:.1f}%" if em is not None else "n/a",
        "pass": em is not None and em > 0.10,
    })

    # 3. Last ROE > 10%
    roe = company.roe or (la.roe if la else None)
    checks.append({
        "criterion": "Last ROE > 10.0%",
        "threshold": 10.0,
        "actual": f"{roe*100:.1f}%" if roe is not None else "n/a",
        "pass": roe is not None and roe > 0.10,
    })

    # 4. FCF Yield > 10%
    fcfy = company.fcf_yield
    checks.append({
        "criterion": "FCF Yield > 10.0%",
        "threshold": 10.0,
        "actual": f"{fcfy*100:.1f}%" if fcfy is not None else "n/a",
        "pass": fcfy is not None and fcfy > 0.10,
    })

    # 5. Net Cash (net_debt < 0)
    nd = company.net_debt or (la.net_debt if la else None)
    is_net_cash = nd is not None and nd < 0
    checks.append({
        "criterion": "Net Cash (no net debt)",
        "threshold": None,
        "actual": f"Net {'Cash' if is_net_cash else 'Debt'} {abs(nd):.0f}M" if nd is not None else "n/a",
        "pass": is_net_cash,
    })

    # 6. Share buybacks (shares outstanding decreased vs 3 years ago)
    years = company.sorted_years()
    buyback = False
    if len(years) >= 3 and la:
        recent_shares = la.shares_outstanding or company.shares_outstanding
        older_af = company.annual_financials.get(years[2])
        older_shares = older_af.shares_outstanding if older_af else None
        if recent_shares and older_shares and older_shares > 0:
            buyback = recent_shares < older_shares * 0.99  # at least 1% reduction
    checks.append({
        "criterion": "Share Buybacks (3yr shrink)",
        "threshold": None,
        "actual": "Yes" if buyback else "No",
        "pass": buyback,
    })

    # 7. Dividend payer
    dy = company.dividend_yield
    is_payer = dy is not None and dy > 0.001  # > 0.1% yield
    checks.append({
        "criterion": "Dividend Payer",
        "threshold": None,
        "actual": f"{dy*100:.1f}%" if dy else "None",
        "pass": is_payer,
    })

    # 8. Founder still on board
    officers = getattr(company, "officers", []) or []
    if officers:
        founders = [
            o["name"] for o in officers
            if "founder" in (o.get("title") or "").lower()
        ]
        founder_on_board = bool(founders)
        actual_str = founders[0] if founders else "Not found"
    else:
        founder_on_board = False
        actual_str = "n/a"
    checks.append({
        "criterion": "Founder-led",
        "threshold": None,
        "actual": actual_str,
        "pass": founder_on_board,
    })

    return checks


# ── NOTE: OverviewModel + CLI entry point removed when the original
# "Investment Memo" framework was deleted. Investment Memo V2 (EODHD-only)
# now drives the dispatch in pages/report_generator.py directly, and it
# imports _overview_prompt_parts, _calculate_checklist and SYSTEM_PROMPT
# from this module — those are the only public surface that remains.


# ── Formatters ────────────────────────────────────────────────────────────────

def _b(v) -> str:
    if v is None: return "n/a"
    return f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:.0f}M"

def _m(v) -> str:
    return f"{v:,.0f}" if v is not None else "n/a"

def _pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"

def _x(v, dp=1) -> str:
    return f"{v:.{dp}f}x" if v is not None else "n/a"


# CLI entry point removed — see note near top of file.
