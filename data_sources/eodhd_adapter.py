"""
eodhd_adapter.py — Tier 1b: EODHD (End of Day Historical Data) API.

High-quality annual financial data for European and global exchanges.
Requires a paid plan for fundamentals access.
Plans: https://eodhistoricaldata.com/pricing

EODHD provides:
- 30+ years of annual fundamentals for 70,000+ companies worldwide
- All major EU exchanges: XETRA, LSE, Euronext, Helsinki, Stockholm, Oslo, etc.
- Asia, LatAm, Middle East coverage
- Balance sheet, income statement, cash flow in one endpoint
- Real-time and delayed price quotes

Usage in waterfall:
- Runs for non-US tickers after yfinance (Tier 1a)
- Overrides yfinance annual financials with EODHD's more accurate data
- Silently skips if free tier (returns 403) or key missing
"""

from __future__ import annotations
import time
import logging
from datetime import datetime
from typing import Optional

import requests

from .base import CompanyData, AnnualFinancials, ForwardEstimates, DataSourceResult
from config import EODHD_API_KEY, REQUEST_HEADERS

logger = logging.getLogger(__name__)

EODHD_BASE = "https://eodhistoricaldata.com/api"
EODHD_DELAY = 0.5  # seconds between calls

# Yahoo Finance exchange suffix → EODHD exchange code
#
# EODHD Fundamentals Data Feed covers 73 exchanges worldwide.
# Exchanges confirmed NOT covered by EODHD (will 404 → graceful fallback):
#   Japan (.T → TSE), India (.NS → NSE, .BO → BSE), Singapore (.SI → SGX)
#
_YF_TO_EODHD = {
    # ── Europe ────────────────────────────────────────────────────────────────
    ".DE": ".XETRA",   # Germany — Xetra (primary, highest liquidity)
    ".F":  ".F",       # Germany — Frankfurt alternative listing
    ".BE": ".BE",      # Germany — Berlin
    ".MU": ".MU",      # Germany — Munich
    ".L":  ".LSE",     # UK — London Stock Exchange
    ".PA": ".PA",      # France — Euronext Paris
    ".AS": ".AS",      # Netherlands — Euronext Amsterdam
    ".BR": ".BR",      # Belgium — Euronext Brussels
    ".MI": ".MI",      # Italy — Borsa Italiana Milan
    ".MC": ".MC",      # Spain — BME Madrid
    ".HE": ".HE",      # Finland — Nasdaq Helsinki
    ".ST": ".ST",      # Sweden — Nasdaq Stockholm
    ".OL": ".OL",      # Norway — Oslo Børs
    ".CO": ".CO",      # Denmark — Nasdaq Copenhagen
    ".SW": ".SW",      # Switzerland — SIX Swiss Exchange
    ".VX": ".SW",      # Switzerland — older Yahoo suffix, maps to EODHD .SW
    ".VI": ".VI",      # Austria — Vienna Exchange
    ".WA": ".WAR",     # Poland — Warsaw Stock Exchange (EODHD code: WAR)
    ".LS": ".LS",      # Portugal — Euronext Lisbon
    ".AT": ".AT",      # Greece — Athens Exchange
    ".IR": ".IR",      # Ireland — Irish Stock Exchange
    ".TA": ".TA",      # Israel — Tel Aviv Stock Exchange
    ".PR": ".PR",      # Czech Republic — Prague Stock Exchange
    ".BU": ".BUD",     # Hungary — Budapest Stock Exchange

    # ── Americas ──────────────────────────────────────────────────────────────
    ".TO": ".TO",      # Canada — Toronto Stock Exchange (TSX)
    ".V":  ".V",       # Canada — TSX Venture Exchange
    ".SA": ".SA",      # Brazil — Sao Paulo (B3)
    ".MX": ".MX",      # Mexico — Mexican Stock Exchange
    ".SN": ".SN",      # Chile — Santiago Stock Exchange
    ".BA": ".BA",      # Argentina — Buenos Aires Exchange

    # ── Asia-Pacific (EODHD coverage) ─────────────────────────────────────────
    ".HK": ".HK",      # Hong Kong — HKEX (confirmed working)
    ".TW": ".TW",      # Taiwan — Taiwan Stock Exchange
    ".TWO":".TWO",     # Taiwan — Taiwan OTC Exchange
    ".KS": ".KO",      # South Korea — KOSPI (Yahoo .KS → EODHD .KO)
    ".KQ": ".KQ",      # South Korea — KOSDAQ
    ".SS": ".SHG",     # China — Shanghai Stock Exchange (Yahoo .SS → EODHD .SHG)
    ".SZ": ".SHE",     # China — Shenzhen Stock Exchange (Yahoo .SZ → EODHD .SHE)
    ".AX": ".AU",      # Australia — ASX (Yahoo .AX → EODHD .AU)
    ".BK": ".BK",      # Thailand — Stock Exchange of Thailand
    ".JK": ".JK",      # Indonesia — Jakarta Exchange
    ".KL": ".KLSE",    # Malaysia — Kuala Lumpur Exchange
    ".VN": ".VN",      # Vietnam — Ho Chi Minh Stock Exchange
    ".PSE":".PSE",     # Philippines — Philippine Stock Exchange

    # ── Africa / Middle East ──────────────────────────────────────────────────
    ".JO": ".JSE",     # South Africa — Johannesburg Stock Exchange
    ".CA": ".CA",      # Egypt — Egyptian Exchange

    # ── Baltic (NASDAQ Baltic) ───────────────────────────────────────────────
    ".VS": ".VS",      # Lithuania — NASDAQ Vilnius
    ".TL": ".TL",      # Estonia   — NASDAQ Tallinn
    ".RG": ".RG",      # Latvia    — NASDAQ Riga

    # NOT COVERED by EODHD — these will 404 and fall back to yfinance/AV:
    # ".T"  → Japan (TSE) — not in EODHD exchange list
    # ".NS" → India NSE   — not in EODHD exchange list
    # ".BO" → India BSE   — not in EODHD exchange list
    # ".SI" → Singapore SGX — not in EODHD exchange list
}


class EODHDAdapter:
    """
    EODHD fundamentals adapter — accurate annual financial data for global stocks.

    Data returned (in millions where monetary):
    - Company profile: name, exchange, currency, sector, industry, country, ISIN
    - Current highlights: market cap, P/E, EPS, dividend yield, ROE, profit margin
    - Annual history: up to 20+ years of income stmt, balance sheet, cash flow
    """

    SOURCE_NAME = "eodhd"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or EODHD_API_KEY
        if not self.api_key:
            logger.warning("[eodhd] No API key configured. EODHD adapter disabled.")

    def _is_available(self) -> bool:
        return bool(self.api_key)

    def _convert_ticker(self, yf_ticker: str) -> str:
        """
        Convert Yahoo Finance ticker format to EODHD format.

        Examples:
            RHM.DE      → RHM.XETRA     (Germany Xetra)
            NOKIA.HE    → NOKIA.HE      (Helsinki, same code)
            AAPL        → AAPL.US       (US stock, no suffix)
            BA.L        → BA.LSE        (London)
            SAP.DE      → SAP.XETRA     (Germany Xetra)
            005930.KS   → 005930.KO     (Korea KOSPI)
            2330.TW     → 2330.TW       (Taiwan, same code)
            600519.SS   → 600519.SHG    (China Shanghai)
            700.HK      → 700.HK        (Hong Kong, same code)

        Note: Japan (.T), India (.NS/.BO), Singapore (.SI) are NOT covered
        by EODHD and will return 404 — the adapter handles this gracefully.
        """
        dot_pos = yf_ticker.rfind(".")
        if dot_pos == -1:
            # No suffix — US stock
            return f"{yf_ticker}.US"

        suffix = yf_ticker[dot_pos:]          # e.g. ".DE"
        base   = yf_ticker[:dot_pos]          # e.g. "RHM"

        eodhd_suffix = _YF_TO_EODHD.get(suffix)
        if not eodhd_suffix:
            # Suffix not in mapping — might already be EODHD format, keep as-is
            return yf_ticker

        # ── Exchange-specific code normalisation ──────────────────────────────
        # Hong Kong: HKEX codes are officially 4-5 digits; EODHD requires the
        # leading zeros (e.g. 700 → 0700, 5 → 0005).
        if eodhd_suffix == ".HK" and base.isdigit():
            base = base.zfill(4)

        # Korea KRX: codes are 6 digits (e.g. 5930 → 005930).
        if eodhd_suffix in (".KO", ".KQ") and base.isdigit():
            base = base.zfill(6)

        return f"{base}{eodhd_suffix}"

    def fetch(self, ticker: str) -> DataSourceResult:
        """
        Fetch company fundamentals from EODHD.

        Converts the Yahoo Finance ticker to EODHD format, calls the
        /fundamentals endpoint, then maps the JSON to CompanyData.
        Returns DataSourceResult with success=False on any error.
        """
        start = time.time()

        if not self._is_available():
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error="EODHD API key not configured. Add EODHD_API_KEY to .env.",
                duration_seconds=time.time() - start,
            )

        eodhd_ticker = self._convert_ticker(ticker)
        logger.info(f"[eodhd] Fetching {ticker} (as {eodhd_ticker})…")

        try:
            time.sleep(EODHD_DELAY)
            url    = f"{EODHD_BASE}/fundamentals/{eodhd_ticker}"
            params = {"api_token": self.api_key, "fmt": "json"}
            resp   = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=30)

            if resp.status_code == 403:
                return DataSourceResult(
                    success=False,
                    source_name=self.SOURCE_NAME,
                    error=(
                        "EODHD fundamentals require a paid plan. "
                        "Visit eodhistoricaldata.com to upgrade."
                    ),
                    duration_seconds=time.time() - start,
                )

            if resp.status_code == 404:
                # Exchange not covered by EODHD (Japan, India, Singapore, etc.)
                return DataSourceResult(
                    success=False,
                    source_name=self.SOURCE_NAME,
                    error=f"EODHD: ticker '{eodhd_ticker}' not found (exchange may not be covered).",
                    duration_seconds=time.time() - start,
                )

            resp.raise_for_status()
            raw = resp.json()

        except requests.RequestException as e:
            # Only log as error for unexpected failures (not 404 — those are
            # handled above and just mean the exchange isn't covered)
            logger.warning(f"[eodhd] Request failed for {eodhd_ticker}: {e}")
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error=str(e),
                duration_seconds=time.time() - start,
            )

        # Validate we got real data (EODHD returns {} for unknown tickers)
        if not raw or not isinstance(raw, dict):
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error=f"EODHD returned empty response for '{eodhd_ticker}'. Check ticker format.",
                duration_seconds=time.time() - start,
            )

        general = raw.get("General") or {}
        if not general.get("Name"):
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error=f"EODHD: No company found for '{eodhd_ticker}'.",
                duration_seconds=time.time() - start,
            )

        fields_filled: list[str] = []

        company = CompanyData(
            ticker=ticker,
            input_ticker=ticker,
            fetch_timestamp=datetime.utcnow().isoformat(),
            as_of_date=datetime.utcnow().strftime("%Y-%m-%d"),
            data_sources=[self.SOURCE_NAME],
        )

        # ── General / Identity ────────────────────────────────────────────────
        company.name        = general.get("Name")
        company.exchange    = general.get("Exchange")
        company.currency    = general.get("CurrencyCode")
        company.currency_price = general.get("CurrencyCode")
        company.sector      = general.get("Sector") or None
        company.industry    = general.get("Industry") or None
        company.country     = general.get("CountryName") or general.get("Country")
        company.isin        = general.get("ISIN") or None
        company.description = general.get("Description") or None
        company.website     = general.get("WebURL") or None

        emp_raw = general.get("FullTimeEmployees")
        if emp_raw:
            try:
                company.employees = int(str(emp_raw).replace(",", ""))
            except (ValueError, TypeError):
                pass

        for f in ["name", "sector", "industry", "country", "description", "isin"]:
            if getattr(company, f):
                fields_filled.append(f)

        # ── Highlights (current-period market & ratio data) ───────────────────
        highlights = raw.get("Highlights") or {}

        # Market cap: prefer the pre-calculated Mln version, fall back to raw
        mkt_cap_mln = self._parse_float(highlights.get("MarketCapitalizationMln"))
        if mkt_cap_mln is not None:
            company.market_cap = mkt_cap_mln
        else:
            company.market_cap = self._to_m(highlights.get("MarketCapitalization"))

        # NOTE: We deliberately do NOT set company.current_price here.
        # EODHD's fundamentals endpoint doesn't return a live quote, and the
        # most-recent MarketCapitalization is a snapshot that may be days old
        # — deriving price = MarketCap/Shares from it produces a value that
        # looks live but can be off by several percent from the actual quote.
        # current_price is set by yfinance (Tier 1a) or, if yfinance fails,
        # by the Stooq fallback in data_manager (free EOD close, ~1 day fresh).

        company.pe_ratio      = self._parse_float(highlights.get("PERatio"))
        company.roe           = self._parse_float(highlights.get("ReturnOnEquityTTM"))
        company.roa           = self._parse_float(highlights.get("ReturnOnAssetsTTM"))
        company.net_margin    = self._parse_float(highlights.get("ProfitMargin"))
        company.ebit_margin   = self._parse_float(highlights.get("OperatingMarginTTM"))
        company.ebitda_margin = self._parse_float(
            highlights.get("EBITDAMargin") or highlights.get("EbitdaMargin")
        )

        # Dividend yield: EODHD returns as a decimal already (0.02 = 2%)
        company.dividend_yield = self._parse_float(highlights.get("DividendYield"))

        # TTM figures — store for EV ratio derivation below
        _rev_ttm    = self._to_m(highlights.get("RevenueTTM"))
        _ebitda_ttm = self._to_m(highlights.get("EBITDA"))
        if _rev_ttm is not None:
            company.ttm_revenue = _rev_ttm
        if _ebitda_ttm is not None:
            company.ttm_ebitda = _ebitda_ttm

        # Forward analyst EPS estimates
        _eps_est_cy = self._parse_float(highlights.get("EPSEstimateCurrentYear"))
        _eps_est_ny = self._parse_float(highlights.get("EPSEstimateNextYear"))
        if _eps_est_ny is not None:
            company.eps_estimate_next_year = _eps_est_ny
        if _eps_est_cy is not None and company.eps_estimate_next_year is None:
            company.eps_estimate_next_year = _eps_est_cy

        for f in ["market_cap", "pe_ratio", "roe", "roa", "net_margin",
                  "ebit_margin", "dividend_yield"]:
            if getattr(company, f) is not None:
                fields_filled.append(f)

        # ── Valuation Block (EV multiples & book ratios) ──────────────────────
        valuation = raw.get("Valuation") or {}

        # Forward P/E
        company.forward_pe = self._parse_float(valuation.get("ForwardPE"))

        # Price/Book
        company.price_to_book = self._parse_float(
            valuation.get("PriceBookMRQ") or valuation.get("PriceBook")
        )

        # EV multiples — EODHD calculates these directly
        company.ev_sales  = self._parse_float(
            valuation.get("EnterpriseValueRevenue") or valuation.get("EVRevenue")
        )
        company.ev_ebitda = self._parse_float(
            valuation.get("EnterpriseValueEbitda") or valuation.get("EVEbitda")
        )

        # Current enterprise value (absolute, in millions)
        company.enterprise_value = self._to_m(valuation.get("EnterpriseValue"))

        for f in ["forward_pe", "price_to_book", "ev_sales", "ev_ebitda", "enterprise_value"]:
            if getattr(company, f) is not None:
                fields_filled.append(f)

        # ── Technicals ────────────────────────────────────────────────────────
        technicals = raw.get("Technicals") or {}
        company.beta      = self._parse_float(technicals.get("Beta"))
        company.week_52_high = self._parse_float(technicals.get("52WeekHigh"))
        company.week_52_low  = self._parse_float(technicals.get("52WeekLow"))
        company.ma_50     = self._parse_float(technicals.get("50DayMA"))
        company.ma_200    = self._parse_float(technicals.get("200DayMA"))

        # ── Shares outstanding & float ────────────────────────────────────────
        shares_stats = raw.get("SharesStats") or {}
        shares_raw = shares_stats.get("SharesOutstanding")
        if shares_raw is not None:
            company.shares_outstanding = self._to_m(shares_raw)
        float_raw = shares_stats.get("SharesFloat")
        if float_raw is not None:
            company.shares_float = self._to_m(float_raw)
        company.pct_insiders      = self._parse_float(shares_stats.get("PercentInsiders"))
        company.pct_institutions  = self._parse_float(shares_stats.get("PercentInstitutions"))

        # ── Short Interest ─────────────────────────────────────────────────────
        # Primary source: Technicals section — always populated for US equities.
        # Verified field names from live EODHD API (2026):
        #   Technicals.SharesShort            = raw share count (int)
        #   Technicals.SharesShortPriorMonth  = raw share count (int)
        #   Technicals.ShortRatio             = days to cover (float)
        #   Technicals.ShortPercent           = short % of float (decimal, 0.0092 = 0.92%)
        # Fallback: SharesStats section (same fields, but often null for non-US).
        #   SharesStats.ShortPercentFloat     = short % of float (decimal)
        def _tech_or_stats(tech_key, stats_key=None):
            v = technicals.get(tech_key)
            if v is None and stats_key:
                v = shares_stats.get(stats_key)
            return v

        t_short_raw = _tech_or_stats("SharesShort")
        if t_short_raw is not None:
            company.shares_short = self._to_m(t_short_raw)

        t_short_prior_raw = _tech_or_stats("SharesShortPriorMonth")
        if t_short_prior_raw is not None:
            company.shares_short_prior_month = self._to_m(t_short_prior_raw)

        # ShortPercent in Technicals = short % of float (decimal)
        # ShortPercentFloat in SharesStats = same metric, same decimal format
        company.short_percent_of_float = self._parse_float(
            _tech_or_stats("ShortPercent", "ShortPercentFloat")
        )
        company.short_ratio = self._parse_float(
            _tech_or_stats("ShortRatio")
        )

        # Note: short_interest_history (historical /shorts endpoint) is NOT fetched
        # here. It is fetched on-demand in report_generator.py when the Short Interest
        # report is requested — this avoids DataManager cache/merge issues and ensures
        # the chart always shows fresh bi-monthly data.

        # ── Splits & Dividends ────────────────────────────────────────────────
        splits_divs = raw.get("SplitsDividends") or {}
        company.forward_annual_dividend_rate  = self._parse_float(
            splits_divs.get("ForwardAnnualDividendRate"))
        company.forward_annual_dividend_yield = self._parse_float(
            splits_divs.get("ForwardAnnualDividendYield"))
        company.payout_ratio     = self._parse_float(splits_divs.get("PayoutRatio"))
        company.dividend_date    = splits_divs.get("DividendDate") or None
        company.ex_dividend_date = splits_divs.get("ExDividendDate") or None
        company.last_split_factor = splits_divs.get("LastSplitFactor") or None
        company.last_split_date   = splits_divs.get("LastSplitDate") or None

        # ── Extended company identity ─────────────────────────────────────────
        company.ipo_date         = general.get("IPODate") or None
        company.fiscal_year_end  = general.get("FiscalYearEnd") or None
        company.address          = general.get("Address") or None
        company.phone            = general.get("Phone") or None

        # Officers list (keep top 10)
        officers_raw = general.get("Officers") or {}
        if isinstance(officers_raw, dict):
            for _, off in list(officers_raw.items())[:10]:
                if isinstance(off, dict):
                    name  = off.get("Name") or ""
                    title = off.get("Title") or ""
                    if name:
                        company.officers.append({"name": name, "title": title})
        elif isinstance(officers_raw, list):
            for off in officers_raw[:10]:
                if isinstance(off, dict):
                    name  = off.get("Name") or off.get("name") or ""
                    title = off.get("Title") or off.get("title") or ""
                    if name:
                        company.officers.append({"name": name, "title": title})

        # ── Additional per-share & TTM metrics ───────────────────────────────
        company.peg_ratio              = self._parse_float(highlights.get("PEGRatio"))
        company.book_value_per_share   = self._parse_float(highlights.get("BookValue"))
        company.revenue_per_share      = self._parse_float(highlights.get("RevenuePerShareTTM"))
        company.eps_ttm                = self._parse_float(
            highlights.get("EarningsShare") or highlights.get("EPSTTMDiluted"))
        company.quarterly_revenue_growth_yoy   = self._parse_float(
            highlights.get("QuarterlyRevenueGrowthYOY"))
        company.quarterly_earnings_growth_yoy  = self._parse_float(
            highlights.get("QuarterlyEarningsGrowthYOY"))

        # Price-to-Sales (from Valuation block)
        company.price_to_sales = self._parse_float(
            valuation.get("PriceSalesTTM") or valuation.get("PriceToSales"))

        # ── Annual Financial History ──────────────────────────────────────────
        try:
            financials_block = raw.get("Financials") or {}
            self._parse_annual_history(company, financials_block, fields_filled)
            # Extract last quarterly date and compute TTM from quarters as fallback
            self._parse_quarterly_ttm(company, financials_block)
        except Exception as e:
            logger.warning(f"[eodhd] Could not parse annual financials for {ticker}: {e}")

        # ── Actual EPS from Earnings.Annual (underlying EPS, more reliable than
        #    the income statement eps field which is often None in EODHD) ───────
        try:
            self._parse_earnings_annual_eps(raw, company)
        except Exception as e:
            logger.warning(f"[eodhd] Could not parse Earnings.Annual for {ticker}: {e}")

        # ── Per-year shares outstanding from outstandingShares.annual ─────────
        # EODHD's balance-sheet "commonStock" is par-value capital (wrong as a
        # share count). The dedicated outstandingShares block has correct
        # per-year totals back to ~2009 — populate them on every annual row.
        try:
            self._parse_per_year_shares(raw, company)
        except Exception as e:
            logger.warning(f"[eodhd] Could not parse outstandingShares for {ticker}: {e}")

        # ── Forward dividend: fill most recent year's DPS if missing ──────────
        # Spring-payer fix: for companies that pay the prior fiscal year's
        # dividend after fiscal year-end (e.g. German companies paying in May),
        # yfinance won't have the entry yet. Use EODHD ForwardAnnualDividendRate
        # as the declared dividend for the most recent fiscal year.
        try:
            fwd_div_rate = self._parse_float(splits_divs.get("ForwardAnnualDividendRate"))
            if fwd_div_rate and company.annual_financials:
                latest_yr = max(company.annual_financials.keys())
                if company.annual_financials[latest_yr].dividends_per_share is None:
                    company.annual_financials[latest_yr].dividends_per_share = fwd_div_rate
                    logger.debug(
                        f"[eodhd] Set FY{latest_yr} DPS={fwd_div_rate} "
                        f"from ForwardAnnualDividendRate"
                    )
        except Exception as e:
            logger.warning(f"[eodhd] Could not apply forward DPS for {ticker}: {e}")

        # ── Forward Estimates (Earnings.Trend) ────────────────────────────────
        try:
            fe = self._parse_forward_estimates(raw)
            if fe is not None:
                company.forward_estimates = fe
                fields_filled.append("forward_estimates")
        except Exception as e:
            logger.warning(f"[eodhd] Could not parse forward estimates for {ticker}: {e}")

        # ── Derived Calculations ──────────────────────────────────────────────
        company.calculate_current_ratios()

        logger.info(
            f"[eodhd] {ticker} done. "
            f"Fields: {len(fields_filled)}, "
            f"Years: {company.year_range()}, "
            f"Completeness: {company.completeness_pct()}%"
        )

        return DataSourceResult(
            success=True,
            source_name=self.SOURCE_NAME,
            data=company,
            fields_filled=fields_filled,
            duration_seconds=time.time() - start,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Annual history parser
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_annual_history(
        self,
        company: CompanyData,
        financials_block: dict,
        fields_filled: list,
    ) -> None:
        """
        Parse EODHD Financials block into AnnualFinancials objects.

        EODHD structure:
            Financials.Income_Statement.annual  → dict keyed by "YYYY-MM-DD"
            Financials.Balance_Sheet.annual     → dict keyed by "YYYY-MM-DD"
            Financials.Cash_Flow.annual         → dict keyed by "YYYY-MM-DD"

        All monetary values arrive as strings (or None); raw units (not millions).
        We convert to millions via _to_m().
        """
        # EODHD uses "yearly" as the key for annual data (not "annual")
        def _annual(block_key: str) -> dict:
            blk = financials_block.get(block_key) or {}
            return blk.get("yearly") or blk.get("annual") or {}

        income_annual   = _annual("Income_Statement")
        balance_annual  = _annual("Balance_Sheet")
        cashflow_annual = _annual("Cash_Flow")

        if not income_annual:
            logger.debug(f"[eodhd] No annual income statement data available.")
            return

        # Index balance sheet and cash flow by year for O(1) lookup
        balance_by_year:  dict[int, dict] = {}
        cashflow_by_year: dict[int, dict] = {}

        for date_str, row in balance_annual.items():
            yr = _year_from_date(date_str)
            if yr:
                balance_by_year[yr] = row

        for date_str, row in cashflow_annual.items():
            yr = _year_from_date(date_str)
            if yr:
                cashflow_by_year[yr] = row

        for date_str, inc in income_annual.items():
            yr = _year_from_date(date_str)
            if not yr:
                continue

            af = company.annual_financials.get(yr, AnnualFinancials(year=yr))
            # Mark this row as EODHD-sourced so the green ✓ shows in PDFs
            # even when this is a brand-new year (wholesale merge path).
            af.source = "eodhd"

            # ── Income Statement ──────────────────────────────────────────────
            af.revenue      = self._to_m(inc.get("totalRevenue"))
            af.gross_profit = self._to_m(inc.get("grossProfit"))
            af.ebit         = self._to_m(inc.get("ebit"))
            af.ebitda       = self._to_m(inc.get("ebitda"))
            af.net_income   = self._to_m(inc.get("netIncome"))

            # EPS is per-share — parse_float only (no /1M conversion)
            af.eps_diluted  = self._parse_float(inc.get("eps") or inc.get("epsDiluted"))

            # Extended IS fields
            af.cost_of_revenue           = self._to_m(inc.get("costOfRevenue"))
            af.depreciation_amortization = self._to_m(
                inc.get("depreciationAndAmortization")
                or inc.get("reconciledDepreciation")
            )
            af.interest_expense          = self._to_m(inc.get("interestExpense"))
            af.interest_income           = self._to_m(inc.get("interestIncome"))
            af.income_before_tax         = self._to_m(inc.get("incomeBeforeTax"))
            af.tax_provision             = self._to_m(
                inc.get("incomeTaxExpense") or inc.get("taxProvision")
            )
            af.minority_interest         = self._to_m(inc.get("minorityInterest"))
            af.net_income_continuing_ops = self._to_m(inc.get("netIncomeFromContinuingOps"))
            af.sga                       = self._to_m(inc.get("sellingGeneralAdministrative"))
            af.extraordinary_items       = self._to_m(inc.get("extraordinaryItems"))

            # ── Balance Sheet ─────────────────────────────────────────────────
            b = balance_by_year.get(yr, {})
            if b:
                af.total_assets = self._to_m(b.get("totalAssets"))
                af.total_equity = self._to_m(
                    b.get("totalStockholderEquity")
                    or b.get("totalEquity")
                )
                # Net debt: read EODHD's pre-computed field first — it uses the
                # same definition as management reporting (total financial debt
                # minus all liquid financial assets, including short-term deposits
                # and money-market funds beyond just narrow cash equivalents).
                # Fall back to total_debt - cash derivation only when absent.
                nd_direct = self._parse_float(b.get("netDebt"))
                if nd_direct is not None:
                    af.net_debt = self._to_m(b.get("netDebt"))

                # Debt: EODHD uses several field names depending on company/period.
                # shortLongTermDebtTotal  = total of short + long term (best)
                # shortLongTermDebt       = usually just current portion
                # longTermDebt            = long-term only (minimum fallback)
                total_debt_val = (
                    self._to_m(b.get("shortLongTermDebtTotal"))
                    or self._to_m(b.get("shortLongTermDebt"))
                    or self._to_m(b.get("longTermDebt"))
                )
                if total_debt_val is not None:
                    af.total_debt = total_debt_val

                af.cash = self._to_m(
                    b.get("cashAndEquivalents")
                    or b.get("cashAndCashEquivalentsAtCarryingValue")
                    or b.get("cash")
                )

                # Extended Balance Sheet fields
                af.goodwill               = self._to_m(b.get("goodWill"))
                af.intangible_assets      = self._to_m(b.get("intangibleAssets"))
                af.inventory              = self._to_m(b.get("inventory"))
                af.net_receivables        = self._to_m(b.get("netReceivables"))
                af.accounts_payable       = self._to_m(b.get("accountsPayable"))
                af.ppe_net                = self._to_m(
                    b.get("propertyPlantAndEquipmentNet")
                    or b.get("propertyPlantEquipment")
                )
                af.retained_earnings      = self._to_m(b.get("retainedEarnings"))
                af.capital_lease_obligations = self._to_m(b.get("capitalLeaseObligations"))
                af.net_working_capital    = self._to_m(b.get("netWorkingCapital"))
                af.current_assets         = self._to_m(b.get("totalCurrentAssets"))
                af.current_liabilities    = self._to_m(b.get("totalCurrentLiabilities"))

                # NOTE: EODHD's "commonStock" field is the subscribed capital
                # (Grundkapital / par-value capital in EUR), NOT the actual share count.
                # For German companies this is ~€112M for Rheinmetall, which when
                # divided by 1M gives 112.0 — wildly wrong (actual ~46M shares).
                # We intentionally do NOT read shares from EODHD balance sheet.
                # yfinance provides "Ordinary Shares Number" which is the correct count.

            # ── Cash Flow ─────────────────────────────────────────────────────
            cf = cashflow_by_year.get(yr, {})
            if cf:
                af.operating_cash_flow = self._to_m(
                    cf.get("totalCashFromOperatingActivities")
                )
                # FCF: use reported value if available
                fcf_raw = cf.get("freeCashFlow")
                if fcf_raw is not None:
                    af.fcf = self._to_m(fcf_raw)

                # CapEx: EODHD typically reports as a negative number
                capex_raw = self._to_m(cf.get("capitalExpenditures"))
                if capex_raw is not None:
                    af.capex = abs(capex_raw)

                # Extended Cash Flow fields
                div_paid = self._to_m(cf.get("dividendsPaid"))
                if div_paid is not None:
                    af.dividends_paid = abs(div_paid)   # store as positive
                af.change_in_working_capital = self._to_m(cf.get("changeInWorkingCapital"))
                af.investing_cash_flow       = self._to_m(
                    cf.get("totalCashflowsFromInvestingActivities")
                )
                af.net_borrowings            = self._to_m(cf.get("netBorrowings"))

            # Derive margins, net debt, ROE, etc.
            af.calculate_derived()
            company.annual_financials[yr] = af

        if company.annual_financials:
            fields_filled.append("annual_financials")
            logger.debug(
                f"[eodhd] Annual data years: {list(company.annual_financials.keys())}"
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Quarterly TTM helper
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_quarterly_ttm(self, company: "CompanyData", financials_block: dict) -> None:
        """Set ttm_last_quarter_date and fill ttm_revenue/ttm_ebitda from quarters
        if the Highlights block did not provide them."""
        def _quarterly(block_key: str) -> dict:
            blk = financials_block.get(block_key) or {}
            return blk.get("quarterly") or blk.get("quarter") or {}

        income_q = _quarterly("Income_Statement")
        if not income_q:
            return

        # Sort quarterly dates descending to find the most recent 4 quarters
        sorted_dates = sorted(income_q.keys(), reverse=True)
        if sorted_dates:
            # Store as "YYYY-MM" (trim day)
            latest = sorted_dates[0]
            company.ttm_last_quarter_date = latest[:7] if len(latest) >= 7 else latest

        # Fallback: compute TTM revenue/EBITDA by summing last 4 quarters
        recent_4 = sorted_dates[:4]
        if company.ttm_revenue is None and recent_4:
            total = 0.0
            count = 0
            for d in recent_4:
                v = self._to_m(income_q[d].get("totalRevenue") or income_q[d].get("revenue"))
                if v is not None:
                    total += v
                    count += 1
            if count > 0:
                company.ttm_revenue = total * 4 / count  # annualise if partial

        if company.ttm_ebitda is None and recent_4:
            total = 0.0
            count = 0
            for d in recent_4:
                row = income_q[d]
                v = self._to_m(row.get("ebitda") or row.get("EBITDA"))
                if v is None:
                    # derive: EBIT + D&A
                    ebit = self._to_m(row.get("ebit") or row.get("operatingIncome"))
                    da   = self._to_m(row.get("depreciationAndAmortization") or row.get("dAndA"))
                    if ebit is not None and da is not None:
                        v = ebit + da
                if v is not None:
                    total += v
                    count += 1
            if count > 0:
                company.ttm_ebitda = total * 4 / count

    # ──────────────────────────────────────────────────────────────────────────
    # Actual EPS from Earnings.Annual
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_per_year_shares(self, raw: dict, company: CompanyData) -> None:
        """
        Populate AnnualFinancials.shares_outstanding from EODHD's
        outstandingShares.annual block.

        Block structure (EODHD returns it as an ordered dict):
            {"0": {"date": "2026", "dateFormatted": "2026-12-31",
                   "sharesMln": "46.6700", "shares": 46670000}, ...}

        We prefer the `shares` integer (full units) and convert to millions
        to match the AnnualFinancials convention (which historically stored
        per-year shares in millions on rows yfinance populated).
        """
        block = (raw.get("outstandingShares") or {}).get("annual") or {}
        if not isinstance(block, dict):
            return
        for _key, row in block.items():
            if not isinstance(row, dict):
                continue
            yr = _year_from_date(row.get("dateFormatted") or row.get("date"))
            if not yr:
                continue
            shares = self._parse_float(row.get("shares"))
            if shares is None:
                shares_mln = self._parse_float(row.get("sharesMln"))
                if shares_mln is None:
                    continue
                shares_in_m = shares_mln
            else:
                shares_in_m = shares / 1_000_000
            # Lazy-create the AnnualFinancials row if there's no income-
            # statement data for this year (rare — but defensive).
            af = company.annual_financials.get(yr)
            if af is None:
                from .base import AnnualFinancials
                af = AnnualFinancials(year=yr)
                af.source = "eodhd"
                company.annual_financials[yr] = af
            af.shares_outstanding = shares_in_m
            logger.debug(f"[eodhd] FY{yr} shares={shares_in_m:.3f}M")

    def _parse_earnings_annual_eps(self, raw: dict, company: CompanyData) -> None:
        """
        Apply epsActual from EODHD Earnings.Annual to matching annual_financials rows.

        EODHD Earnings.Annual contains the actual underlying/adjusted EPS per fiscal
        year as reported by management (add-backs for purchase price amortisation,
        restructuring etc.). This is the metric analyst consensus tracks, so it must
        be the basis for eps_diluted — NOT the IFRS EPS that yfinance may report,
        which can be significantly lower (e.g. RHM 2025: yfinance 15.16 vs actual 25.72).
        """
        earnings = raw.get("Earnings") or {}
        annual   = earnings.get("Annual") or {}
        if not isinstance(annual, dict) or not annual:
            return

        for date_str, entry in annual.items():
            if not isinstance(entry, dict):
                continue
            yr = _year_from_date(date_str)
            if not yr or yr not in company.annual_financials:
                continue
            eps_actual = self._parse_float(entry.get("epsActual"))
            if eps_actual is not None:
                company.annual_financials[yr].eps_diluted = eps_actual
                logger.debug(f"[eodhd] Set FY{yr} eps_diluted={eps_actual} from Earnings.Annual")

    # ──────────────────────────────────────────────────────────────────────────
    # Forward estimates parser
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_forward_estimates(self, raw: dict) -> Optional[ForwardEstimates]:
        """
        Parse EODHD Earnings.Trend for the next fiscal year analyst consensus.

        Picks the nearest future fiscal year-end date entry (relative to today).
        Falls back to the most recent entry if no future dates exist.
        Returns None if no usable data found.
        """
        earnings = raw.get("Earnings") or {}
        trend = earnings.get("Trend") or {}
        if not isinstance(trend, dict) or not trend:
            return None

        current_year = datetime.utcnow().year

        # Collect (year, entry) pairs, prefer future years
        candidates: list[tuple[int, dict]] = []
        for date_str, entry in trend.items():
            if not isinstance(entry, dict):
                continue
            yr = _year_from_date(date_str)
            if yr and yr >= current_year:
                candidates.append((yr, entry))

        if not candidates:
            # All entries are historical — take the most recent
            for date_str, entry in trend.items():
                if not isinstance(entry, dict):
                    continue
                yr = _year_from_date(date_str)
                if yr:
                    candidates.append((yr, entry))

        if not candidates:
            return None

        # Pick the nearest year (smallest year number in the candidate set)
        candidates.sort(key=lambda x: x[0])
        target_year, entry = candidates[0]

        # revenueEstimateAvg in Earnings.Trend is already in millions (confirmed by
        # RHM.DE returning ~13 instead of 13_432_000_000). Use _parse_float, not _to_m.
        rev = self._parse_float(entry.get("revenueEstimateAvg"))
        eps = self._parse_float(entry.get("earningsEstimateAvg"))

        if rev is None and eps is None:
            return None

        fe = ForwardEstimates(year=target_year, source="eodhd")
        fe.revenue    = rev
        fe.eps_diluted = eps

        rev_growth = self._parse_float(entry.get("revenueEstimateGrowth"))
        if rev_growth is not None:
            fe.revenue_growth_yoy = rev_growth

        eps_growth = self._parse_float(entry.get("earningsEstimateGrowth"))
        if eps_growth is not None:
            fe.eps_growth_yoy = eps_growth

        # Analyst count: use the higher of revenue / earnings analyst count
        rev_n = self._parse_float(entry.get("revenueEstimateNumberOfAnalysts"))
        eps_n = self._parse_float(entry.get("earningsEstimateNumberOfAnalysts"))
        counts = [c for c in [rev_n, eps_n] if c is not None]
        if counts:
            fe.analyst_count = int(max(counts))

        return fe

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_float(self, val) -> Optional[float]:
        """Parse EODHD string values safely. Returns None for None/empty/invalid."""
        if val is None or val in ("", "None", "null"):
            return None
        try:
            return float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            return None

    def _to_m(self, val) -> Optional[float]:
        """Convert raw (full-unit) value to millions."""
        v = self._parse_float(val)
        return v / 1_000_000 if v is not None else None



# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _year_from_date(val) -> Optional[int]:
    """Extract 4-digit year from a date string like '2024-12-31'."""
    if not val:
        return None
    try:
        return int(str(val)[:4])
    except (ValueError, TypeError):
        return None


# ── Quick sanity test (run this file directly) ────────────────────────────────
if __name__ == "__main__":
    import os
    import sys
    logging.basicConfig(level=logging.INFO)

    # Ticker conversion tests
    adapter = EODHDAdapter(api_key="demo")
    test_conversions = [
        ("RHM.DE",   "RHM.XETRA"),
        ("NOKIA.HE", "NOKIA.HE"),
        ("AAPL",     "AAPL.US"),
        ("BA.L",     "BA.LSE"),
        ("SAP.DE",   "SAP.XETRA"),
    ]
    print("Ticker conversion tests:")
    all_ok = True
    for yf_ticker, expected in test_conversions:
        result = adapter._convert_ticker(yf_ticker)
        status = "OK" if result == expected else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  {status}  {yf_ticker:15s} → {result:15s}  (expected {expected})")
    print(f"\nAll conversions correct: {all_ok}")

    # Live API test (only if real key provided)
    key = os.getenv("EODHD_API_KEY", "")
    if not key or key == "demo":
        print("\nSet EODHD_API_KEY env variable to test live API calls.")
        sys.exit(0)

    adapter = EODHDAdapter(api_key=key)
    for ticker, label in [
        ("RHM.DE",   "Rheinmetall (XETRA)"),
        ("NOKIA.HE", "Nokia (Helsinki)"),
        ("BA.L",     "BAE Systems (LSE)"),
    ]:
        print(f"\n{'='*60}")
        print(f"  {label} ({ticker})")
        res = adapter.fetch(ticker)
        if res.success and res.data:
            c = res.data
            print(f"  {c.summary()}")
            for yr in c.sorted_years()[:3]:
                af = c.annual_financials[yr]
                print(f"  {yr}: Rev={af.revenue}M EBIT={af.ebit}M NI={af.net_income}M")
        else:
            print(f"  FAILED: {res.error}")
