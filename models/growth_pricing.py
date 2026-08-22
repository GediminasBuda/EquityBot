"""
growth_pricing.py — Growth Pricing & Peers: data gathering + prompt builder.

Evaluates the relative pricing of high-growth companies (including pre-profit,
high revenue-growth names): given the quality of this business, what
expectations are already embedded in today's stock price?

Every ratio in this report is computed deterministically in Python from
verified financial data — never by the LLM. The LLM is used only to write a
short narrative discussion per dimension (Revenue & Gross Profit, Earnings,
Cash Flow) plus a closing synthesis, grounded in the verified tables it is
given. The LLM's peer role is limited to suggesting peer tickers (via
models.fisher_peers.suggest_peers) when the user hasn't supplied enough —
the actual peer comparison metrics are computed in Python, same as
everything else in this module.

History: up to 10 years (or since IPO if shorter), sourced from the same
EODHD-only builder used by Growth Quality / Earnings Quality Score. For US
tickers, SEC EDGAR XBRL facts (data_sources/edgar_adapter.py) are used to
fill in additional years beyond what EODHD/yfinance return, fill-only —
never overwriting a year already present. EDGAR-sourced years carry no
market-price data, so every valuation-multiple ratio (EV/Sales, EV/Gross
Profit, P/E, EV/EBITDA, EV/EBIT, EV/NOPAT, EV/FCF, FCF Yield) is "n/a" for
those years; only fundamental ratios (Sales, Sales Growth, Gross Margin, Net
Margin, Gross Profit CAGR, and ROIC/FCF Margin/Conversion when tax and
cash-flow detail are present) can be populated.
"""
from __future__ import annotations

import logging
from typing import Optional

from data_sources.base import CompanyData, AnnualFinancials
from data_sources.edgar_adapter import EdgarAdapter

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# History helpers
# ─────────────────────────────────────────────────────────────────────────

def get_history_years(company: CompanyData, max_years: int = 10) -> list[int]:
    """Chronological (oldest-first) list of the most recent `max_years` fiscal years."""
    return list(reversed(company.sorted_years()[:max_years]))


def extend_history_with_edgar(company: CompanyData, ticker: str) -> int:
    """
    Fill-only merge: for US tickers, pull SEC EDGAR XBRL annual facts and add
    any fiscal years NOT already present in company.annual_financials. Never
    overwrites an existing year. Never raises — returns the count of years
    added (0 on any failure, or for non-US tickers EdgarAdapter declines).
    """
    added = 0
    try:
        result = EdgarAdapter().fetch(ticker)
    except Exception:
        logger.exception("[growth_pricing] EDGAR fetch raised for %s", ticker)
        return 0

    if not result.success or not result.data or not result.data.annual_financials:
        return 0

    for year, af in result.data.annual_financials.items():
        if year in company.annual_financials:
            continue
        af.source = "edgar"
        company.annual_financials[year] = af
        added += 1

    return added


def _cagr(company: CompanyData, years: list[int], i: int, field: str, lookback: int) -> Optional[float]:
    """CAGR of `field` over `lookback` years ending at years[i], or None if unavailable."""
    if i < lookback:
        return None
    af_end = company.annual_financials.get(years[i])
    af_base = company.annual_financials.get(years[i - lookback])
    end_v = getattr(af_end, field, None) if af_end else None
    base_v = getattr(af_base, field, None) if af_base else None
    if end_v is None or base_v is None or base_v <= 0:
        return None
    return (end_v / base_v) ** (1 / lookback) - 1


def _peg_fallback_from_history(company: CompanyData, pe_ratio: Optional[float]) -> Optional[float]:
    """PEG using trailing P/E over trailing 3-year EPS CAGR, for companies
    where EODHD has no forward-consensus EPS growth estimate (a common gap
    for thinly-covered foreign ADRs). eps_diluted is a per-share figure in
    whatever currency net_income/shares happen to be in for that company —
    since this is a ratio of two same-currency EPS values, the CAGR itself
    is currency-invariant, so this is safe to use even for dual-currency
    ADRs where the price-based ratios need special handling."""
    if pe_ratio is None or pe_ratio <= 0:
        return None
    years = get_history_years(company, max_years=10)
    if len(years) < 4:
        return None
    eps_cagr3 = _cagr(company, years, len(years) - 1, "eps_diluted", 3)
    if eps_cagr3 is None or eps_cagr3 <= 0:
        return None
    return pe_ratio / (eps_cagr3 * 100)


# ─────────────────────────────────────────────────────────────────────────
# Dimension 1 — Revenue & Gross Profit
# ─────────────────────────────────────────────────────────────────────────

def compute_revenue_gp_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    years = get_history_years(company, max_years=max_years)
    rows = []
    for i, y in enumerate(years):
        af = company.annual_financials.get(y)
        revenue = af.revenue if af else None

        yoy = None
        if i > 0 and revenue is not None:
            prev = company.annual_financials.get(years[i - 1])
            prev_rev = prev.revenue if prev else None
            if prev_rev:
                yoy = (revenue / prev_rev) - 1

        ev_sales = af.ev_sales if af else None

        ev_gross_profit = None
        if af and af.enterprise_value is not None and af.gross_profit and af.gross_profit > 0:
            ev_gross_profit = af.enterprise_value / af.gross_profit

        gross_margin = af.gross_margin if af else None
        net_margin = af.net_margin if af else None
        gp_cagr3 = _cagr(company, years, i, "gross_profit", 3)
        gp_cagr5 = _cagr(company, years, i, "gross_profit", 5)

        rows.append({
            "year": y,
            "revenue": revenue,
            "sales_yoy": yoy,
            "ev_sales": ev_sales,
            "ev_gross_profit": ev_gross_profit,
            "gross_margin": gross_margin,
            "gross_profit_cagr3": gp_cagr3,
            "gross_profit_cagr5": gp_cagr5,
            "net_margin": net_margin,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Dimension 2 — Earnings
# ─────────────────────────────────────────────────────────────────────────

def _nopat(af: AnnualFinancials) -> Optional[float]:
    """NOPAT = EBIT x (1 - effective tax rate). Same formula as
    models/growth_quality.py's compute_capital_allocation_table()."""
    if (af.ebit is not None and af.income_before_tax is not None and af.income_before_tax > 0
            and af.tax_provision is not None):
        tax_rate = af.tax_provision / af.income_before_tax
        return af.ebit * (1 - tax_rate)
    return None


def _roic(af: AnnualFinancials, nopat: Optional[float]) -> Optional[float]:
    """ROIC = NOPAT / Invested Capital, Invested Capital = total_debt + total_equity - cash.
    Same formula as models/growth_quality.py's compute_capital_allocation_table()."""
    if (nopat is not None and af.total_debt is not None and af.total_equity is not None
            and af.cash is not None):
        invested_capital = af.total_debt + af.total_equity - af.cash
        if invested_capital > 0:
            return nopat / invested_capital
    return None


def compute_earnings_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    years = get_history_years(company, max_years=max_years)
    rows = []
    for i, y in enumerate(years):
        af = company.annual_financials.get(y)

        market_cap = af.market_cap if af else None
        ev = af.enterprise_value if af else None
        pe_ratio = af.pe_ratio if af else None
        ev_ebitda = af.ev_ebitda if af else None
        ev_ebit = af.ev_ebit if af else None

        nopat = _nopat(af) if af else None
        ev_nopat = None
        if af and ev is not None and nopat is not None and nopat > 0:
            ev_nopat = ev / nopat
        roic = _roic(af, nopat) if af else None

        eps_cagr3 = _cagr(company, years, i, "eps_diluted", 3)
        peg_eps_growth = None
        if pe_ratio is not None and pe_ratio > 0 and eps_cagr3 is not None and eps_cagr3 > 0:
            # PEG convention: P/E divided by the growth rate expressed as a
            # whole number (20% growth -> divisor 20, not 0.20).
            peg_eps_growth = pe_ratio / (eps_cagr3 * 100)

        rows.append({
            "year": y,
            "market_cap": market_cap,
            "enterprise_value": ev,
            "pe_ratio": pe_ratio,
            "ev_ebitda": ev_ebitda,
            "ev_ebit": ev_ebit,
            "ev_nopat": ev_nopat,
            "roic": roic,
            "peg_eps_growth": peg_eps_growth,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Dimension 3 — Cash Flow
# ─────────────────────────────────────────────────────────────────────────

def compute_cash_flow_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    years = get_history_years(company, max_years=max_years)
    rows = []
    for y in years:
        af = company.annual_financials.get(y)
        ev = af.enterprise_value if af else None
        fcf = af.fcf if af else None

        ev_fcf = None
        if ev is not None and fcf is not None and fcf > 0:
            ev_fcf = ev / fcf

        fcf_yield = af.fcf_yield if af else None

        fcf_margin = None
        if af and fcf is not None and af.revenue and af.revenue > 0:
            fcf_margin = fcf / af.revenue

        fcf_conversion = None
        if af and fcf is not None and af.net_income is not None and af.net_income > 0:
            fcf_conversion = fcf / af.net_income

        rows.append({
            "year": y,
            "ev_fcf": ev_fcf,
            "fcf_yield": fcf_yield,
            "fcf_margin": fcf_margin,
            "fcf_conversion": fcf_conversion,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Rule of 40 — Growth vs. Profitability Balance
# ─────────────────────────────────────────────────────────────────────────

def compute_rule_of_40_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    """Revenue Growth Rate + Profit Margin, using three common profitability
    measures (EBITDA Margin, FCF Margin, Net Margin) separately. Classic
    threshold: combined result >= 40% is considered a healthy balance
    between growth and profitability for high-growth/software companies."""
    years = get_history_years(company, max_years=max_years)
    rows = []
    for i, y in enumerate(years):
        af = company.annual_financials.get(y)

        revenue_yoy = None
        if i > 0 and af and af.revenue is not None:
            prev = company.annual_financials.get(years[i - 1])
            prev_rev = prev.revenue if prev else None
            if prev_rev:
                revenue_yoy = (af.revenue / prev_rev) - 1

        ebitda_margin = af.ebitda_margin if af else None
        net_margin = af.net_margin if af else None

        fcf_margin = None
        if af and af.fcf is not None and af.revenue and af.revenue > 0:
            fcf_margin = af.fcf / af.revenue

        def _rule40(margin):
            if revenue_yoy is None or margin is None:
                return None
            return revenue_yoy + margin

        rows.append({
            "year": y,
            "revenue_yoy": revenue_yoy,
            "ebitda_margin": ebitda_margin,
            "rule40_ebitda": _rule40(ebitda_margin),
            "fcf_margin": fcf_margin,
            "rule40_fcf": _rule40(fcf_margin),
            "net_margin": net_margin,
            "rule40_net": _rule40(net_margin),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Current / forward-looking snapshot (not chronological)
# ─────────────────────────────────────────────────────────────────────────

def compute_current_snapshot(company: CompanyData) -> dict:
    """Non-chronological current/forward-looking valuation snapshot — Forward
    P/E and the standard PEG Ratio only exist for the current moment (next
    fiscal year's consensus), not as a per-year historical series."""
    fe = company.forward_estimates
    forward_eps_growth = fe.eps_growth_yoy if fe else None

    peg_ratio = company.peg_ratio
    if (peg_ratio is None and company.forward_pe is not None
            and forward_eps_growth is not None and forward_eps_growth > 0):
        peg_ratio = company.forward_pe / (forward_eps_growth * 100)
    if peg_ratio is None:
        peg_ratio = _peg_fallback_from_history(company, company.pe_ratio)

    # Dual-currency ADRs (e.g. KSPI: USD trading currency, KZT statement
    # currency): company.market_cap stays in the TRADING currency (it's a
    # pure price x shares figure, fine to show as-is as long as it's
    # labeled). company.enterprise_value is also trading-currency, but EV
    # has no meaning without a currency-consistent net-debt add-on for a
    # dual-currency company — enterprise_value_financials_ccy is the
    # reporting-currency-consistent EV built for exactly this (see
    # eodhd_only_builder.py); prefer it whenever it's available so the
    # snapshot table shows a currency-correct EV instead of a mismatched one.
    cur_price = company.currency_price or company.currency or ""
    cur_fin = getattr(company, "currency_financials", None) or cur_price
    _ev_ccy = getattr(company, "enterprise_value_financials_ccy", None)
    enterprise_value = _ev_ccy if _ev_ccy is not None else company.enterprise_value

    return {
        "market_cap": company.market_cap,
        "market_cap_currency": cur_price,
        "enterprise_value": enterprise_value,
        "enterprise_value_currency": cur_fin if _ev_ccy is not None else cur_price,
        "pe_ratio_ttm": company.pe_ratio,
        "forward_pe": company.forward_pe,
        "peg_ratio": peg_ratio,
        "ev_ebitda_ttm": company.ev_ebitda,
        "ev_sales_ttm": company.ev_sales,
        "fcf_yield_ttm": company.fcf_yield,
    }


# ─────────────────────────────────────────────────────────────────────────
# Peer Comparison — most-recent-data cross-sectional snapshot
# ─────────────────────────────────────────────────────────────────────────

def _sales_cagr3(company: CompanyData) -> Optional[float]:
    """3-year Sales CAGR ending at the latest available fiscal year."""
    years = company.sorted_years()
    if not years:
        return None
    latest_y = years[0]
    af_latest = company.annual_financials.get(latest_y)
    af_base = company.annual_financials.get(latest_y - 3)
    end_v = af_latest.revenue if af_latest else None
    base_v = af_base.revenue if af_base else None
    if end_v is None or base_v is None or base_v <= 0:
        return None
    return (end_v / base_v) ** (1 / 3) - 1


def _latest_revenue_yoy(company: CompanyData) -> Optional[float]:
    """Most recent fiscal year's Revenue Growth YoY."""
    years = company.sorted_years()
    if len(years) < 2:
        return None
    af0 = company.annual_financials.get(years[0])
    af1 = company.annual_financials.get(years[1])
    rev0 = af0.revenue if af0 else None
    rev1 = af1.revenue if af1 else None
    if rev0 is None or not rev1:
        return None
    return (rev0 / rev1) - 1


def compute_peer_snapshot_row(company: CompanyData, is_subject: bool = False) -> dict:
    """Most-recent-data row for the peer comparison table: Sales YoY Growth,
    Sales 3Y CAGR, P/E, PEG, EV/Gross Profit, EV/Sales, Rule of 40 (EBITDA
    and Net Margin variants). Every figure is computed here in Python from
    the company's own current scalars and latest fiscal year — never by the
    LLM."""
    years = company.sorted_years()
    latest = company.annual_financials.get(years[0]) if years else None

    sales_cagr3 = _sales_cagr3(company)
    pe_ratio = company.pe_ratio

    fe = company.forward_estimates
    forward_eps_growth = fe.eps_growth_yoy if fe else None
    peg_ratio = company.peg_ratio
    if (peg_ratio is None and company.forward_pe is not None
            and forward_eps_growth is not None and forward_eps_growth > 0):
        peg_ratio = company.forward_pe / (forward_eps_growth * 100)
    if peg_ratio is None:
        peg_ratio = _peg_fallback_from_history(company, pe_ratio)

    # For dual-currency ADRs (price/market cap in the trading currency,
    # financial statements in a different reporting currency — e.g. KSPI),
    # company.enterprise_value is in the TRADING currency and cannot be
    # divided by latest.gross_profit/revenue (reporting currency) without
    # producing a meaningless ratio. enterprise_value_financials_ccy is a
    # currency-consistent EV built specifically for this (see
    # eodhd_only_builder.py); it's None for the overwhelming majority of
    # (non-dual-currency) companies, in which case enterprise_value is used
    # exactly as before.
    _ev_ccy = getattr(company, "enterprise_value_financials_ccy", None)
    _ev_for_ratios = _ev_ccy if _ev_ccy is not None else company.enterprise_value

    ev_gross_profit = None
    if (_ev_for_ratios is not None and latest
            and latest.gross_profit and latest.gross_profit > 0):
        ev_gross_profit = _ev_for_ratios / latest.gross_profit

    # EV/Sales: prefer EODHD's own precomputed ratio (company.ev_sales) —
    # it's already currency-consistent on EODHD's side even for dual-currency
    # ADRs, so recomputing it ourselves would be redundant and riskier for
    # the TTM-revenue currency ambiguity noted above. Only fall back to a
    # manual calc when EODHD didn't provide one.
    ev_sales = company.ev_sales
    if ev_sales is None:
        denom_revenue = company.ttm_revenue or (latest.revenue if latest else None)
        if _ev_for_ratios is not None and denom_revenue:
            ev_sales = _ev_for_ratios / denom_revenue

    revenue_yoy = _latest_revenue_yoy(company)
    ebitda_margin = latest.ebitda_margin if latest else None
    net_margin = latest.net_margin if latest else None
    rule40_ebitda = (revenue_yoy + ebitda_margin
                      if revenue_yoy is not None and ebitda_margin is not None else None)
    rule40_net = (revenue_yoy + net_margin
                  if revenue_yoy is not None and net_margin is not None else None)

    return {
        "ticker": company.ticker,
        "name": company.name or company.ticker,
        "is_subject": is_subject,
        "sales_yoy_growth": revenue_yoy,
        "sales_cagr3": sales_cagr3,
        "pe_ratio": pe_ratio,
        "peg_ratio": peg_ratio,
        "ev_gross_profit": ev_gross_profit,
        "ev_sales": ev_sales,
        "rule40_ebitda": rule40_ebitda,
        "rule40_net": rule40_net,
    }


def compute_peer_comparison_table(subject: CompanyData, peers: dict) -> list[dict]:
    """Subject + up to 5 peers, one most-recent-data snapshot row each.
    Subject is always first; `peers` is a {ticker: CompanyData} dict in the
    order peers should appear as columns."""
    rows = [compute_peer_snapshot_row(subject, is_subject=True)]
    for peer_company in peers.values():
        rows.append(compute_peer_snapshot_row(peer_company, is_subject=False))
    return rows


# Direction of "good" per peer-table metric: True = higher value scores more
# points (growth/profitability metrics), False = lower value scores more
# points (valuation multiples — cheaper is better).
PEER_METRIC_DIRECTIONS = {
    "sales_yoy_growth": True,
    "sales_cagr3": True,
    "pe_ratio": False,
    "peg_ratio": False,
    "ev_gross_profit": False,
    "ev_sales": False,
    "rule40_ebitda": True,
    "rule40_net": True,
}


def compute_peer_scores(rows: list[dict]) -> list[int]:
    """Composite valuation score per company, same order as `rows`. For each
    metric, companies with a valid (non-None) value are ranked and awarded
    points from 1 (worst) to N (best) — N being the count of companies with
    data for that metric — where 'best' means cheaper valuation multiples or
    stronger growth/profitability, per PEER_METRIC_DIRECTIONS. Points are
    summed across all metrics per company. A company missing a given
    metric simply contributes 0 points for that metric (not penalised
    beyond forfeiting the points it could have earned)."""
    totals = [0] * len(rows)
    for metric, higher_is_better in PEER_METRIC_DIRECTIONS.items():
        valid = [(i, r[metric]) for i, r in enumerate(rows) if r.get(metric) is not None]
        if not valid:
            continue
        valid.sort(key=lambda pair: pair[1], reverse=higher_is_better)
        n = len(valid)
        for rank, (i, _v) in enumerate(valid):
            totals[i] += n - rank
    return totals


def compute_valuation_verdict(subject: CompanyData) -> dict:
    """Simple rules-based valuation verdict — Attractive / Moderate /
    Expensive — from the subject's own most-recent-data snapshot (Sales 3Y
    CAGR, EV/Gross Profit, Rule of 40 with Net Margin):

    Attractive: Sales 3Y CAGR > 15% AND EV/Gross Profit < 15x
                AND Rule of 40 (+Net Margin) > 40%
    Expensive:  Sales 3Y CAGR > 15% AND EV/Gross Profit > 25x
                AND Rule of 40 (+Net Margin) > 40%
    Moderate:   everything else (including insufficient data to evaluate)

    This is a deterministic Python computation, not an LLM judgement."""
    row = compute_peer_snapshot_row(subject, is_subject=True)
    cagr = row["sales_cagr3"]
    ev_gp = row["ev_gross_profit"]
    r40 = row["rule40_net"]

    verdict = "Moderate"
    if cagr is not None and ev_gp is not None and r40 is not None:
        if cagr > 0.15 and ev_gp < 15 and r40 > 0.40:
            verdict = "Attractive"
        elif cagr > 0.15 and ev_gp > 25 and r40 > 0.40:
            verdict = "Expensive"

    return {
        "verdict": verdict,
        "sales_cagr3": cagr,
        "ev_gross_profit": ev_gp,
        "rule40_net": r40,
    }


# ─────────────────────────────────────────────────────────────────────────
# Formatting helpers for the LLM dynamic prompt
# ─────────────────────────────────────────────────────────────────────────

def _b(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:.1f}M"


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v*100:.1f}%"


def _x(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.1f}x"


def _num(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _format_revenue_gp_table(rows: list[dict]) -> str:
    lines = [
        "REVENUE & GROSS PROFIT (verified, computed from source data — treat as ground truth):",
        f"{'Year':<7}{'Sales':<10}{'YoY':<9}{'EV/Sales':<10}{'EV/GP':<9}"
        f"{'GrossM':<9}{'GPCAGR3':<10}{'GPCAGR5':<10}{'NetM':<9}",
    ]
    for r in rows:
        lines.append(
            f"{r['year']:<7}{_b(r['revenue']):<10}{_pct(r['sales_yoy']):<9}"
            f"{_x(r['ev_sales']):<10}{_x(r['ev_gross_profit']):<9}"
            f"{_pct(r['gross_margin']):<9}{_pct(r['gross_profit_cagr3']):<10}"
            f"{_pct(r['gross_profit_cagr5']):<10}{_pct(r['net_margin']):<9}"
        )
    return "\n".join(lines)


def _format_earnings_table(rows: list[dict]) -> str:
    lines = [
        "EARNINGS (verified, computed from source data — treat as ground truth):",
        f"{'Year':<7}{'MktCap':<10}{'EV':<10}{'P/E':<8}{'EV/EBITDA':<11}"
        f"{'EV/EBIT':<10}{'EV/NOPAT':<10}{'ROIC':<8}{'PEG(EPSg)':<10}",
    ]
    for r in rows:
        lines.append(
            f"{r['year']:<7}{_b(r['market_cap']):<10}{_b(r['enterprise_value']):<10}"
            f"{_x(r['pe_ratio']):<8}{_x(r['ev_ebitda']):<11}{_x(r['ev_ebit']):<10}"
            f"{_x(r['ev_nopat']):<10}{_pct(r['roic']):<8}{_num(r['peg_eps_growth']):<10}"
        )
    return "\n".join(lines)


def _format_cash_flow_table(rows: list[dict]) -> str:
    lines = [
        "CASH FLOW (verified, computed from source data — treat as ground truth):",
        f"{'Year':<7}{'EV/FCF':<9}{'FCF Yield':<11}{'FCF Margin':<12}{'FCF Conv.':<10}",
    ]
    for r in rows:
        lines.append(
            f"{r['year']:<7}{_x(r['ev_fcf']):<9}{_pct(r['fcf_yield']):<11}"
            f"{_pct(r['fcf_margin']):<12}{_pct(r['fcf_conversion']):<10}"
        )
    return "\n".join(lines)


def format_growth_pricing_financials(company: CompanyData, max_years: int = 10) -> tuple[str, dict]:
    years = get_history_years(company, max_years=max_years)
    rev_gp_rows = compute_revenue_gp_table(company, max_years=max_years)
    earnings_rows = compute_earnings_table(company, max_years=max_years)
    cash_flow_rows = compute_cash_flow_table(company, max_years=max_years)
    snapshot = compute_current_snapshot(company)

    header = (
        f"COMPANY: {company.name or company.ticker} ({company.ticker})\n"
        f"SECTOR: {company.sector or 'n/a'}   INDUSTRY: {company.industry or 'n/a'}\n"
        f"COUNTRY: {company.country or 'n/a'}   CURRENCY: {company.currency or 'n/a'}\n"
        f"EXCHANGE: {company.exchange or 'n/a'}   IPO DATE: {company.ipo_date or 'n/a'}\n"
        f"BUSINESS: {(company.description or 'n/a')[:600]}\n"
    )

    snapshot_text = (
        "\nCURRENT / FORWARD-LOOKING SNAPSHOT (not chronological — TTM and next-FY consensus):\n"
        f"Market Cap: {_b(snapshot['market_cap'])}   Enterprise Value: {_b(snapshot['enterprise_value'])}\n"
        f"P/E (TTM): {_x(snapshot['pe_ratio_ttm'])}   Forward P/E: {_x(snapshot['forward_pe'])}   "
        f"PEG Ratio: {_num(snapshot['peg_ratio'])}\n"
        f"EV/EBITDA (TTM): {_x(snapshot['ev_ebitda_ttm'])}   EV/Sales (TTM): {_x(snapshot['ev_sales_ttm'])}   "
        f"FCF Yield (TTM): {_pct(snapshot['fcf_yield_ttm'])}\n"
    )

    text = (
        header
        + snapshot_text
        + "\n" + _format_revenue_gp_table(rev_gp_rows)
        + "\n\n" + _format_earnings_table(earnings_rows)
        + "\n\n" + _format_cash_flow_table(cash_flow_rows)
        + f"\n\nUse exactly these fiscal years, in this order, when referencing history: {years}\n"
        + "\n'n/a' means the underlying data point was not available (e.g. no market price for a "
          "SEC-EDGAR-only year, or missing tax/cash-flow detail for ROIC/EV-NOPAT/FCF Conversion) "
          "— never invent a number to fill it; note the gap instead if relevant."
    )
    tables = {
        "years": years,
        "revenue_gross_profit": rev_gp_rows,
        "earnings": earnings_rows,
        "cash_flow": cash_flow_rows,
        "snapshot": snapshot,
    }
    return text, tables


# ─────────────────────────────────────────────────────────────────────────
# LLM prompt — narrative only, no tables/scoring
# ─────────────────────────────────────────────────────────────────────────

_CACHEABLE = """You are a senior equity research analyst producing the "Growth Pricing & Peers" report.

CENTRAL QUESTION
This report evaluates the relative pricing of high-growth companies — including pre-profit /
negative-earnings names in a high revenue-growth phase. It asks: given the quality of this
business, what expectations are already embedded in today's stock price?

You will be given VERIFIED, PRE-COMPUTED financial and valuation tables, grouped into three
dimensions (Revenue & Gross Profit, Earnings, Cash Flow), plus a current/forward-looking
snapshot. Every figure is ground truth — do NOT recompute, contradict, or invent any number.
Where a cell is "n/a", say so plainly rather than guessing a value; for younger/loss-making
companies this is expected and often itself the interesting observation (e.g. earnings-based
multiples are meaningless while the company is unprofitable, so the market must be relying on
revenue growth, gross margin trajectory, and cash generation instead).

Return ONLY a JSON object (no markdown fences, no commentary) with exactly these four keys:

{
  "revenue_gross_profit_narrative": "250-350 words. Discuss the trend in sales growth, gross
      margin trajectory, and Gross Profit CAGR, and how the EV/Sales and EV/Gross Profit
      multiples have moved relative to that growth and margin trend over the available history.",
  "earnings_narrative": "250-350 words. Discuss earnings-based multiples (P/E, EV/EBITDA,
      EV/EBIT, EV/NOPAT, ROIC, PEG) over time, explicitly noting any periods where the company
      was loss-making (multiples n/a) and what the market's willingness to pay elevated
      revenue-based multiples during those periods implies about embedded expectations.",
  "cash_flow_narrative": "200-300 words. Discuss FCF margin, FCF conversion, and how EV/FCF and
      FCF Yield have evolved. Cash flow is often the most reliable dimension for pre-profit
      growth companies since it is less distorted by non-cash accounting items (stock comp,
      impairments, deferred revenue timing) than GAAP/IFRS net income.",
  "priced_in_synthesis": "250-350 words. Directly answer the central question above: given the
      growth durability, margin trajectory, and cash generation evidenced by the tables, what
      expectations for future growth and profitability are already embedded in today's stock
      price? Be specific about what would need to be true for the current valuation to be
      justified, and name the key risks to that embedded expectation."
}

Every word count is a target, not a hard limit — prioritize substance and precision over hitting
an exact count. Ground every claim in the specific numbers provided; avoid generic boilerplate.
"""


def _growth_pricing_prompt_parts(company: CompanyData, max_years: int = 10) -> tuple[str, str, dict]:
    dynamic_text, tables = format_growth_pricing_financials(company, max_years=max_years)
    dynamic = (
        f"SUBJECT COMPANY TICKER: {company.ticker}\n\n"
        + dynamic_text
        + "\n\nWrite the narrative discussion described above and return the JSON."
    )
    return _CACHEABLE, dynamic, tables


def _validate_growth_pricing(analysis: dict) -> dict:
    """Defensive coercion of the LLM's narrative-only JSON response — no
    tables or scored fields here, just four prose strings."""
    if not isinstance(analysis, dict):
        analysis = {}

    defaults = {
        "revenue_gross_profit_narrative": "Narrative not available.",
        "earnings_narrative": "Narrative not available.",
        "cash_flow_narrative": "Narrative not available.",
        "priced_in_synthesis": "Synthesis not available.",
    }
    out = {}
    for key, default in defaults.items():
        v = analysis.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
        elif isinstance(v, list):
            joined = " ".join(str(x) for x in v).strip()
            out[key] = joined if joined else default
        else:
            out[key] = default
    return out
