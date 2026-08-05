"""
base.py — Unified data models for Your Humble EquityBot.

Every data source adapter fills in what it can on CompanyData.
The DataManager merges results and tracks what came from where.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from datetime import datetime


@dataclass
class AnnualFinancials:
    """
    One row of historical annual financial data.
    All monetary values are in the company's reporting currency (millions).
    None = data not available from any source yet.
    """
    year: int

    # Income Statement
    revenue: Optional[float] = None         # Total revenue / net sales
    gross_profit: Optional[float] = None
    ebitda: Optional[float] = None          # Earnings before interest, tax, D&A
    ebit: Optional[float] = None            # Operating income (= EBIT)
    net_income: Optional[float] = None
    net_income_underlying: Optional[float] = None  # EPS-derived underlying net income (adj. EPS × shares)
    eps_diluted: Optional[float] = None     # Underlying/adjusted EPS (from EODHD Earnings.Annual)
    dividends_per_share: Optional[float] = None

    # Margins (as decimals: 0.15 = 15%)
    gross_margin: Optional[float] = None
    ebit_margin: Optional[float] = None
    ebitda_margin: Optional[float] = None
    net_margin: Optional[float] = None

    # Balance Sheet
    total_assets: Optional[float] = None
    total_debt: Optional[float] = None      # Short + long term debt
    cash: Optional[float] = None            # Cash & equivalents
    net_debt: Optional[float] = None        # total_debt - cash
    total_equity: Optional[float] = None
    shares_outstanding: Optional[float] = None  # in millions

    # Income Statement — extended fields
    cost_of_revenue: Optional[float] = None          # Cost of goods sold / cost of revenue
    depreciation_amortization: Optional[float] = None  # D&A (from IS or CF)
    interest_expense: Optional[float] = None         # Interest expense (positive)
    interest_income: Optional[float] = None          # Interest income
    income_before_tax: Optional[float] = None        # Pre-tax income (EBT)
    tax_provision: Optional[float] = None            # Income tax expense
    minority_interest: Optional[float] = None        # Minority / non-controlling interest in NI
    net_income_continuing_ops: Optional[float] = None  # NI from continuing operations
    sga: Optional[float] = None                      # Selling, General & Administrative
    research_development: Optional[float] = None     # R&D expense
    total_operating_expenses: Optional[float] = None  # SG&A + R&D + other opex (excl. COGS)
    extraordinary_items: Optional[float] = None      # One-off items (can be negative)

    # Balance Sheet — extended fields
    goodwill: Optional[float] = None                 # Goodwill from acquisitions
    intangible_assets: Optional[float] = None        # Intangible assets (excl. goodwill)
    inventory: Optional[float] = None               # Inventories
    net_receivables: Optional[float] = None          # Accounts receivable (net)
    accounts_payable: Optional[float] = None         # Accounts payable
    ppe_net: Optional[float] = None                  # PP&E net of depreciation
    retained_earnings: Optional[float] = None        # Retained earnings
    capital_lease_obligations: Optional[float] = None  # Finance lease liabilities
    net_working_capital: Optional[float] = None      # Current assets - current liabilities
    current_assets: Optional[float] = None           # Total current assets
    current_liabilities: Optional[float] = None      # Total current liabilities

    # Cash Flow — extended fields
    dividends_paid: Optional[float] = None           # Dividends paid (positive, from CF stmt)
    change_in_working_capital: Optional[float] = None  # WC change in operating CF
    investing_cash_flow: Optional[float] = None      # Total cash from investing activities
    net_borrowings: Optional[float] = None           # Net debt raised / (repaid)

    # Cash Flow
    operating_cash_flow: Optional[float] = None
    capex: Optional[float] = None           # Capital expenditures (positive number)
    fcf: Optional[float] = None             # Free cash flow = OCF - CapEx

    # Returns & Ratios (calculated or reported)
    roe: Optional[float] = None             # Return on Equity
    roa: Optional[float] = None             # Return on Assets
    roic: Optional[float] = None            # Return on Invested Capital

    # Calculated ratios (derived in calculate_derived)
    current_ratio: Optional[float] = None            # current_assets / current_liabilities
    interest_coverage: Optional[float] = None        # EBIT / interest_expense
    ev_ebitda: Optional[float] = None                # enterprise_value / ebitda (historical)

    # Data provenance — which source filled this row
    source: str = ""   # "eodhd" | "yfinance" | "edgar" | "alpha_vantage" | ""

    # Historical market-based metrics (derived from year-end stock price)
    # Populated by yfinance_adapter after fetching monthly price history.
    price_year_end: Optional[float] = None          # Year-end stock price (local currency)
    market_cap: Optional[float] = None              # In millions  (price × shares_M)
    enterprise_value: Optional[float] = None        # market_cap + net_debt  (millions)
    pe_ratio: Optional[float] = None                # price / eps_diluted
    ev_ebit: Optional[float] = None                 # enterprise_value / ebit
    ev_sales: Optional[float] = None                # enterprise_value / revenue
    fcf_yield: Optional[float] = None               # fcf / market_cap  (decimal)
    div_yield: Optional[float] = None               # dividends_per_share / price (decimal)

    def calculate_derived(self) -> None:
        """Fill in fields that can be derived from other fields."""
        # Net debt
        if self.net_debt is None and self.total_debt is not None and self.cash is not None:
            self.net_debt = self.total_debt - self.cash

        # FCF from OCF - CapEx
        if self.fcf is None and self.operating_cash_flow is not None and self.capex is not None:
            self.fcf = self.operating_cash_flow - self.capex

        # EBITDA from EBIT + D&A (rough proxy if EBITDA missing)
        # We skip this since D&A isn't always available; better to leave None

        # Margins
        if self.revenue and self.revenue > 0:
            if self.net_margin is None and self.net_income is not None:
                self.net_margin = self.net_income / self.revenue
            if self.ebit_margin is None and self.ebit is not None:
                self.ebit_margin = self.ebit / self.revenue
            if self.ebitda_margin is None and self.ebitda is not None:
                self.ebitda_margin = self.ebitda / self.revenue
            if self.gross_margin is None and self.gross_profit is not None:
                self.gross_margin = self.gross_profit / self.revenue

        # Underlying net income = adjusted EPS × shares (analyst-comparable basis)
        # Compute FIRST so it's available for ROE/ROA below.
        # shares_outstanding in AnnualFinancials is stored in full units (e.g. 43_381_664),
        # not millions — divide by 1e6 to get the same millions unit as net_income.
        if self.net_income_underlying is None and self.eps_diluted is not None and self.shares_outstanding is not None:
            if self.shares_outstanding > 0:
                # shares_outstanding is normally stored in millions. Defensive
                # check: if the value is suspiciously huge (> 1M), it must be
                # raw units left over from legacy cache.
                shares_m = (self.shares_outstanding / 1_000_000
                            if self.shares_outstanding > 1_000_000
                            else self.shares_outstanding)
                self.net_income_underlying = self.eps_diluted * shares_m

        # ROE — prefer underlying NI (analyst-comparable) over IFRS NI
        ni_for_roe = self.net_income_underlying if self.net_income_underlying is not None else self.net_income
        if self.roe is None and ni_for_roe is not None and self.total_equity is not None:
            if self.total_equity > 0:
                self.roe = ni_for_roe / self.total_equity

        # ROA — prefer underlying NI
        ni_for_roa = self.net_income_underlying if self.net_income_underlying is not None else self.net_income
        if self.roa is None and ni_for_roa is not None and self.total_assets is not None:
            if self.total_assets > 0:
                self.roa = ni_for_roa / self.total_assets

        # ── Market-cap based historical ratios ────────────────────────────────
        # These are computed once price_year_end and market_cap are populated
        # by the yfinance adapter after fetching monthly price history.

        # P/E  =  year-end price / diluted EPS
        if self.pe_ratio is None and self.price_year_end is not None:
            if self.eps_diluted is not None and self.eps_diluted > 0:
                self.pe_ratio = self.price_year_end / self.eps_diluted

        # EV/EBIT and EV/Sales  (need enterprise_value populated first)
        # Recompute enterprise_value from market_cap + net_debt if missing
        # (yfinance sets market_cap from year-end price; EODHD resets enterprise_value to None)
        if self.enterprise_value is None and self.market_cap is not None:
            nd = self.net_debt
            if nd is None and self.total_debt is not None and self.cash is not None:
                nd = self.total_debt - self.cash
            self.enterprise_value = self.market_cap + (nd or 0)

        ev = self.enterprise_value
        if ev is not None and ev > 0:
            if self.ev_ebit is None and self.ebit and self.ebit > 0:
                self.ev_ebit = ev / self.ebit
            if self.ev_sales is None and self.revenue and self.revenue > 0:
                self.ev_sales = ev / self.revenue

        # FCF Yield  =  FCF / market cap
        if self.fcf_yield is None and self.fcf is not None:
            if self.market_cap and self.market_cap > 0:
                self.fcf_yield = self.fcf / self.market_cap

        # Dividend Yield  =  DPS / year-end price
        if self.div_yield is None and self.dividends_per_share is not None:
            if self.price_year_end and self.price_year_end > 0:
                self.div_yield = self.dividends_per_share / self.price_year_end

        # Current Ratio  =  current_assets / current_liabilities
        if self.current_ratio is None and self.current_assets is not None:
            if self.current_liabilities and self.current_liabilities > 0:
                self.current_ratio = self.current_assets / self.current_liabilities

        # Interest Coverage  =  EBIT / interest_expense
        if self.interest_coverage is None and self.ebit is not None:
            if self.interest_expense and self.interest_expense > 0:
                self.interest_coverage = self.ebit / self.interest_expense

        # Historical EV/EBITDA
        if self.ev_ebitda is None and ev is not None and ev > 0:
            if self.ebitda and self.ebitda > 0:
                self.ev_ebitda = ev / self.ebitda

        # D&A from EBITDA - EBIT (if not directly available)
        if self.depreciation_amortization is None:
            if self.ebitda is not None and self.ebit is not None:
                self.depreciation_amortization = self.ebitda - self.ebit

        # Net working capital from current assets/liabilities if not directly set
        if self.net_working_capital is None and self.current_assets is not None:
            if self.current_liabilities is not None:
                self.net_working_capital = self.current_assets - self.current_liabilities


@dataclass
class ForwardEstimates:
    """
    Analyst consensus estimates for the next fiscal year.
    Populated by yfinance_adapter from yt.earnings_estimate / yt.revenue_estimate.
    Monetary values in millions (same convention as AnnualFinancials).
    """
    year: int                                   # Fiscal year this estimate covers
    revenue: Optional[float] = None             # Consensus revenue (millions)
    net_income: Optional[float] = None          # Consensus net income (millions)
    eps_diluted: Optional[float] = None         # Consensus EPS (per share)
    ebitda: Optional[float] = None              # Rarely available from free sources
    revenue_growth_yoy: Optional[float] = None  # Implied YoY growth (decimal)
    eps_growth_yoy: Optional[float] = None      # Implied EPS growth (decimal)
    net_margin: Optional[float] = None          # Derived: net_income / revenue
    pe_ratio: Optional[float] = None            # Forward P/E (current price / fwd EPS)
    ev_sales: Optional[float] = None            # Current EV / forward revenue
    analyst_count: Optional[int] = None         # Number of analysts contributing
    source: str = "yfinance"


@dataclass
class CompanyData:
    """
    Unified company data container populated by one or more adapters.
    The DataManager merges adapter results into this object.

    Monetary values: in company reporting currency, millions unless noted.
    Ratios: as decimals (0.15 = 15%) unless explicitly noted as multiples (e.g. P/E).
    """

    # ── Identity ─────────────────────────────────────────────────────────────
    ticker: str                             # Normalized Yahoo Finance ticker
    input_ticker: str = ""                  # Original ticker as user typed it
    name: Optional[str] = None
    exchange: Optional[str] = None
    currency: Optional[str] = None          # Reporting currency (USD, EUR, SEK…)
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    isin: Optional[str] = None
    description: Optional[str] = None       # Business description
    website: Optional[str] = None
    employees: Optional[int] = None
    cik: Optional[str] = None              # SEC EDGAR CIK (US companies only)

    # ── Current Market Data ───────────────────────────────────────────────────
    current_price: Optional[float] = None
    currency_price: Optional[str] = None    # Currency of the price quote
    market_cap: Optional[float] = None      # In millions
    shares_outstanding: Optional[float] = None  # In millions
    enterprise_value: Optional[float] = None    # In millions
    as_of_date: Optional[str] = None        # Date of market data snapshot

    # ── Current Valuation Multiples ───────────────────────────────────────────
    pe_ratio: Optional[float] = None        # Price / Earnings (TTM)
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None   # P/B
    ev_ebit: Optional[float] = None         # EV / EBIT
    ev_ebitda: Optional[float] = None       # EV / EBITDA
    ev_sales: Optional[float] = None        # EV / Revenue
    fcf_yield: Optional[float] = None       # FCF / Market Cap (decimal)
    dividend_yield: Optional[float] = None  # Dividend / Price (decimal)

    # ── Current Profitability & Quality ──────────────────────────────────────
    net_margin: Optional[float] = None      # TTM
    ebit_margin: Optional[float] = None     # TTM
    ebitda_margin: Optional[float] = None   # TTM
    gross_margin: Optional[float] = None    # TTM
    roe: Optional[float] = None             # TTM
    roa: Optional[float] = None             # TTM
    roic: Optional[float] = None            # TTM

    # ── Current Balance Sheet Health ─────────────────────────────────────────
    gearing: Optional[float] = None         # Net Debt / EBITDA
    net_debt: Optional[float] = None        # In millions
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = None
    interest_coverage: Optional[float] = None
    beta: Optional[float] = None

    # ── Technical / Price Levels ──────────────────────────────────────────────
    week_52_high: Optional[float] = None    # 52-week high price
    week_52_low: Optional[float] = None     # 52-week low price
    ma_50: Optional[float] = None           # 50-day moving average
    ma_200: Optional[float] = None          # 200-day moving average

    # ── Ownership Structure ───────────────────────────────────────────────────
    shares_float: Optional[float] = None        # Float shares in millions
    pct_insiders: Optional[float] = None        # % held by insiders (decimal)
    pct_institutions: Optional[float] = None    # % held by institutions (decimal)

    # ── Short Interest ────────────────────────────────────────────────────────
    shares_short: Optional[float] = None            # Shares sold short (millions)
    shares_short_prior_month: Optional[float] = None # Prior month short (millions)
    short_percent_of_float: Optional[float] = None  # Short % of float (decimal: 0.05=5%)
    short_ratio: Optional[float] = None             # Days to cover (shares_short / avg vol)
    # Historical monthly short interest (list of dicts, newest first):
    # [{"date": "2024-06-15", "shares_short_m": 12.3, "short_pct_float": 0.045}, ...]
    short_interest_history: List[dict] = field(default_factory=list)

    # ── Dividends & Corporate Actions ────────────────────────────────────────
    payout_ratio: Optional[float] = None
    forward_annual_dividend_rate: Optional[float] = None   # Per-share annual div
    forward_annual_dividend_yield: Optional[float] = None  # Decimal
    dividend_date: Optional[str] = None
    ex_dividend_date: Optional[str] = None
    last_split_factor: Optional[str] = None    # e.g. "2:1"
    last_split_date: Optional[str] = None

    # ── Extended Company Identity ─────────────────────────────────────────────
    ipo_date: Optional[str] = None
    fiscal_year_end: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    officers: List[dict] = field(default_factory=list)  # [{name, title}, ...]

    # ── Additional Per-Share & TTM Metrics ────────────────────────────────────
    peg_ratio: Optional[float] = None
    price_to_sales: Optional[float] = None
    book_value_per_share: Optional[float] = None
    revenue_per_share: Optional[float] = None
    eps_ttm: Optional[float] = None
    quarterly_revenue_growth_yoy: Optional[float] = None
    quarterly_earnings_growth_yoy: Optional[float] = None

    # ── TTM P&L (sum of last 4 reported quarters) ─────────────────────────────
    ttm_revenue: Optional[float] = None      # millions
    ttm_ebitda: Optional[float] = None       # millions
    ttm_ebit: Optional[float] = None         # millions (operating income)
    ttm_net_income: Optional[float] = None   # millions (IFRS attributable)
    ttm_fcf: Optional[float] = None          # millions (free cash flow)
    ttm_last_quarter_date: Optional[str] = None  # "YYYY-MM" of most recent quarter
    next_earnings_date: Optional[str] = None     # "YYYY-MM-DD" from EODHD Highlights

    # ── 10-Year Annual History ────────────────────────────────────────────────
    # Keys are fiscal year integers: {2024: AnnualFinancials(...), 2023: ...}
    annual_financials: Dict[int, AnnualFinancials] = field(default_factory=dict)

    # ── Analyst Forward Estimates ─────────────────────────────────────────────
    # Structured consensus estimates for the next full fiscal year.
    forward_estimates: Optional[ForwardEstimates] = None
    # Legacy flat fields (kept for backwards compat, prefer forward_estimates)
    revenue_estimate_next_year: Optional[float] = None
    eps_estimate_next_year: Optional[float] = None
    revenue_growth_estimate: Optional[float] = None  # YoY %

    # ── Data Provenance ───────────────────────────────────────────────────────
    data_sources: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    fetch_timestamp: Optional[str] = None
    # Scalar field names that were set / overridden by EODHD (authoritative paid data).
    # Used by PDF generators to display a green ✓ indicator next to EODHD-verified values.
    eodhd_fields: List[str] = field(default_factory=list)

    # ─────────────────────────────────────────────────────────────────────────
    # Convenience helpers
    # ─────────────────────────────────────────────────────────────────────────

    def sorted_years(self, descending: bool = True) -> List[int]:
        """Return fiscal years for which annual data is available."""
        return sorted(self.annual_financials.keys(), reverse=descending)

    def latest_annual(self) -> Optional[AnnualFinancials]:
        yrs = self.sorted_years()
        return self.annual_financials[yrs[0]] if yrs else None

    def year_range(self) -> str:
        yrs = self.sorted_years(descending=False)
        if not yrs:
            return "n/a"
        return f"{yrs[0]}–{yrs[-1]}" if len(yrs) > 1 else str(yrs[0])

    def revenue_cagr(self, years: int = 3) -> Optional[float]:
        """
        Calculate revenue CAGR over the last N years.
        Returns decimal (0.05 = 5% CAGR) or None if insufficient data.
        """
        yrs = self.sorted_years()
        if len(yrs) < years + 1:
            return None
        latest = self.annual_financials[yrs[0]].revenue
        base   = self.annual_financials[yrs[years]].revenue
        if not latest or not base or base <= 0:
            return None
        return (latest / base) ** (1 / years) - 1

    def calculate_current_ratios(self) -> None:
        """
        Derive current-period ratios from available market + latest annual data.
        Called after all adapters have run.
        """
        la = self.latest_annual()
        if la:
            la.calculate_derived()

        # Enterprise Value = Market Cap + Net Debt
        if self.enterprise_value is None and self.market_cap is not None:
            nd = self.net_debt if self.net_debt is not None else (la.net_debt if la else None)
            if nd is not None:
                self.enterprise_value = self.market_cap + nd

        ev = self.enterprise_value

        # EV/EBIT
        if self.ev_ebit is None and ev and la and la.ebit and la.ebit > 0:
            self.ev_ebit = ev / la.ebit

        # EV/EBITDA
        if self.ev_ebitda is None and ev and la and la.ebitda and la.ebitda > 0:
            self.ev_ebitda = ev / la.ebitda

        # EV/Sales
        if self.ev_sales is None and ev and la and la.revenue and la.revenue > 0:
            self.ev_sales = ev / la.revenue

        # FCF Yield
        if self.fcf_yield is None and self.market_cap and la and la.fcf and self.market_cap > 0:
            self.fcf_yield = la.fcf / self.market_cap

        # Gearing (Net Debt / EBITDA)
        if self.gearing is None:
            nd = self.net_debt if self.net_debt is not None else (la.net_debt if la else None)
            ebitda = la.ebitda if la else None
            if nd is not None and ebitda and ebitda > 0:
                self.gearing = nd / ebitda

        # Current-period profitability from latest annual
        if la:
            if self.net_margin is None:   self.net_margin   = la.net_margin
            if self.ebit_margin is None:  self.ebit_margin  = la.ebit_margin
            if self.ebitda_margin is None:self.ebitda_margin = la.ebitda_margin
            if self.gross_margin is None: self.gross_margin  = la.gross_margin
            if self.roe is None:          self.roe           = la.roe
            if self.roa is None:          self.roa           = la.roa
            if self.net_debt is None:     self.net_debt      = la.net_debt

    def completeness_pct(self) -> float:
        """Rough data completeness score 0-100%."""
        key_fields = [
            self.name, self.current_price, self.market_cap,
            self.pe_ratio, self.ev_ebit, self.ev_sales,
            self.net_margin, self.roe, self.gearing,
            self.dividend_yield, self.description,
        ]
        filled = sum(1 for f in key_fields if f is not None)
        annual_years = len(self.annual_financials)
        annual_score = min(annual_years / 10, 1.0) * 40  # 40% weight
        field_score  = (filled / len(key_fields)) * 60   # 60% weight
        return round(annual_score + field_score, 1)

    def summary(self) -> str:
        """One-line data summary for logging."""
        return (
            f"{self.ticker} | {self.name or 'Unknown'} | "
            f"Price: {self.current_price} {self.currency_price or ''} | "
            f"MCap: {self.market_cap}M | "
            f"Years of data: {self.year_range()} | "
            f"Sources: {', '.join(self.data_sources)} | "
            f"Completeness: {self.completeness_pct()}%"
        )


@dataclass
class DataSourceResult:
    """
    Returned by each adapter's fetch() method.
    Contains a (possibly partial) CompanyData plus metadata.
    """
    success: bool
    source_name: str
    data: Optional[CompanyData] = None
    error: Optional[str] = None
    fields_filled: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
