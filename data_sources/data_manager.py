"""
data_manager.py — Data waterfall orchestrator for Your Humble EquityBot.

This is the single entry point for all data fetching.
Call DataManager().get(ticker) and receive a fully populated CompanyData object.

Waterfall logic:
  Tier 1a: yfinance         → always runs first (current price, shares, market data skeleton)
  Tier 1b: EODHD            → ALL tickers (paid Fundamentals Feed, 70k+ companies worldwide;
                               overrides yfinance annual income + balance + cash flow)
  Tier 1c: SEC EDGAR        → US tickers only, fill-only after EODHD (authoritative SEC depth)
  Tier 2:  Alpha Vantage    → fill-only if EODHD succeeded; override if EODHD failed
  Tier 4:  FMP (paid)       → runs only if critical fields still missing after Tiers 1-2

Merge strategy:
  - yfinance provides the market data skeleton (current price, shares, real-time ratios)
  - EODHD overrides ALL annual statement fields for every ticker (global paid data)
  - EDGAR adds/fills any US history gaps not covered by EODHD
  - Alpha Vantage fills remaining gaps (fill-only when EODHD ran, override as last resort)
  - FMP fills any remaining critical gaps
  - Calculated fields are derived last, after all sources are merged

Caching:
  - Results are saved to cache/<ticker>.json with a 24-hour TTL
  - On a cache hit, no API calls are made at all
"""

from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

from .base import CompanyData, AnnualFinancials, ForwardEstimates, DataSourceResult
from .index_adapter import IndexAdapter, IndexData
from .yfinance_adapter import YFinanceAdapter
from .edgar_adapter import EdgarAdapter
from .alpha_vantage_adapter import AlphaVantageAdapter
from .fmp_adapter import FMPAdapter
from .eodhd_adapter import EODHDAdapter
from .fred_adapter import FredAdapter, MacroSnapshot
from .news_adapter import NewsAdapter
from .worldbank_adapter import WorldBankAdapter, CountryMacro
from config import (
    CACHE_DIR, CACHE_TTL_HOURS, HISTORICAL_YEARS,
    ENABLE_YFINANCE, ENABLE_EDGAR, ENABLE_ALPHA_VANTAGE, ENABLE_FMP, ENABLE_FRED,
    ENABLE_EODHD,
)

logger = logging.getLogger(__name__)

# Minimum years of annual history we consider "sufficient" before skipping Tier 2
MIN_HISTORY_YEARS = 7

# Fields we consider "critical" — if any are None after all tiers, we flag them
CRITICAL_FIELDS = [
    "name", "current_price", "market_cap",
    "net_margin", "roe", "ev_ebit", "pe_ratio",
]


class DataManager:
    """
    Single entry point for all company data fetching.

    Usage:
        dm = DataManager()
        company = dm.get("WKL.AS")          # single company
        companies = dm.get_many(["AAPL", "MSFT", "GOOGL"])  # batch
    """

    def __init__(self):
        self._yf    = YFinanceAdapter()    if ENABLE_YFINANCE      else None
        self._edgar = EdgarAdapter()       if ENABLE_EDGAR         else None
        self._av    = AlphaVantageAdapter() if ENABLE_ALPHA_VANTAGE else None
        self._fmp   = FMPAdapter()         if ENABLE_FMP           else None
        self._eodhd = EODHDAdapter()       if ENABLE_EODHD         else None
        self._fred  = FredAdapter()        if ENABLE_FRED          else None
        self._idx   = IndexAdapter()
        self._news  = NewsAdapter()
        self._wb    = WorldBankAdapter()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def classify_ticker(ticker: str) -> str:
        """
        Return "equity", "etf", or "index" for the given ticker.
        Pure heuristic first (fast), then yfinance confirmation.
        """
        t = ticker.strip().upper()
        # Pure index tickers always start with ^ or contain common index patterns
        if t.startswith("^"):
            return "index"
        try:
            import yfinance as yf
            qt = (yf.Ticker(t).info or {}).get("quoteType", "").upper()
            if qt == "ETF":
                return "etf"
            if qt == "INDEX":
                return "index"
        except Exception:
            pass
        return "equity"

    def get_macro(self, force_refresh: bool = False) -> MacroSnapshot:
        """Return the latest FRED macro snapshot (cached 6 hours)."""
        if self._fred:
            return self._fred.fetch(force_refresh=force_refresh)
        return MacroSnapshot()

    def get_news(self, company_name: str, ticker: str, max_articles: int = 8) -> list[dict]:
        """Fetch recent news for a company. Returns [] if unavailable."""
        if self._news:
            return self._news.fetch_company_news(company_name, ticker, max_articles)
        return []

    def get_country_macro(self, country_code: str) -> "CountryMacro":
        """Fetch World Bank macro data for a country (ISO2 code e.g. 'DE')."""
        return self._wb.fetch_country(country_code)

    def get_index(self, ticker: str, force_refresh: bool = False) -> IndexData:
        """Fetch index / ETF data. Always uses IndexAdapter (no company waterfall)."""
        ticker = ticker.strip().upper()
        logger.info(f"[DataManager] Index requested: {ticker}")
        return self._idx.fetch(ticker)

    def get(self, ticker: str, force_refresh: bool = False) -> CompanyData:
        """
        Fetch and return a fully populated CompanyData for one ticker.
        Uses cache if available and fresh. Pass force_refresh=True to bypass cache.
        """
        ticker = ticker.strip().upper()
        logger.info(f"[DataManager] Requested: {ticker}")

        # ── Cache check ───────────────────────────────────────────────────────
        if not force_refresh:
            cached = self._load_cache(ticker)
            if cached:
                logger.info(f"[DataManager] Cache hit: {ticker} ({cached.completeness_pct()}% complete)")
                # If the cached payload predates the Stooq fallback and so has
                # no price, try Stooq inline before returning. Cheap (~1s) and
                # rescues users who never force-refreshed after deploy.
                if cached.current_price is None:
                    try:
                        from .stooq_adapter import fetch_stooq_close
                        sp = fetch_stooq_close(ticker)
                        if sp is not None:
                            cached.current_price = sp
                            if cached.market_cap is None and cached.shares_outstanding:
                                cached.market_cap = sp * cached.shares_outstanding
                            self._save_cache(ticker, cached)
                            logger.info(f"[DataManager] Patched cached price via Stooq: {ticker} = {sp}")
                    except Exception as e:
                        logger.warning(f"[DataManager] Stooq patch failed for cached {ticker}: {e}")
                return cached

        # ── Tier 1a: yfinance ─────────────────────────────────────────────────
        company: Optional[CompanyData] = None

        if self._yf and ENABLE_YFINANCE:
            result = self._yf.fetch(ticker)
            if result.success and result.data:
                company = result.data
                logger.info(f"[DataManager] yfinance OK: {company.completeness_pct()}% complete, "
                            f"years: {company.year_range()}")
            else:
                logger.warning(f"[DataManager] yfinance failed for {ticker}: {result.error}")

        # If yfinance completely failed, create an empty skeleton
        if company is None:
            company = CompanyData(
                ticker=ticker,
                input_ticker=ticker,
                fetch_timestamp=datetime.utcnow().isoformat(),
                as_of_date=datetime.utcnow().strftime("%Y-%m-%d"),
            )

        is_us_ticker = _is_us_ticker(ticker)

        # ── Tier 1b: EODHD — primary source for ALL tickers ──────────────────
        # The paid Fundamentals Data Feed covers 70,000+ companies worldwide:
        # US, EU, Asia, LatAm, Middle East — all in one consistent dataset.
        # Runs first (after yfinance skeleton) for every ticker and overrides
        # yfinance's annual data across all statement types.
        # Alpha Vantage / EDGAR are then fallbacks / depth-fillers only.
        eodhd_succeeded = False
        if self._eodhd:
            logger.info(f"[DataManager] Running EODHD for {ticker}…")
            result = self._eodhd.fetch(ticker)
            if result.success and result.data:
                eodhd_succeeded = True
                self._merge(
                    company, result.data, prefer_source="eodhd",
                    fields=[
                        "annual_financials",
                        # Identity
                        "name", "exchange", "currency", "currency_price",
                        "sector", "industry", "country", "isin",
                        "description", "website", "employees",
                        "ipo_date", "fiscal_year_end", "address", "phone",
                        "officers",
                        # Current market — must merge so they survive when
                        # yfinance fails (common on Streamlit Cloud due to
                        # Yahoo IP blocking)
                        "current_price", "market_cap", "shares_outstanding",
                        # Technicals / price levels
                        "beta", "week_52_high", "week_52_low", "ma_50", "ma_200",
                        # Ownership structure
                        "shares_float", "pct_insiders", "pct_institutions",
                        # Short interest
                        "shares_short", "shares_short_prior_month",
                        "short_percent_of_float", "short_ratio",
                        "short_interest_history",
                        # Dividends & splits
                        "payout_ratio",
                        "forward_annual_dividend_rate", "forward_annual_dividend_yield",
                        "dividend_date", "ex_dividend_date",
                        "last_split_factor", "last_split_date",
                        "dividend_yield",
                        # Valuation multiples — EODHD is primary source
                        "pe_ratio", "forward_pe", "price_to_book", "price_to_sales",
                        "peg_ratio", "ev_sales", "ev_ebitda", "enterprise_value",
                        # Forward analyst estimates
                        "forward_estimates",
                        # Per-share & TTM metrics
                        "book_value_per_share", "revenue_per_share", "eps_ttm",
                        # Profitability / growth — EODHD Highlights is the primary source
                        "net_margin", "roe", "roa",
                        "ebit_margin", "ebitda_margin", "gross_margin",
                        "quarterly_revenue_growth_yoy", "quarterly_earnings_growth_yoy",
                        "eps_estimate_next_year",
                        # TTM P&L scalars
                        "ttm_revenue", "ttm_ebitda", "ttm_net_income", "ttm_fcf", "ttm_last_quarter_date",
                    ],
                    override_financials=True,
                    full_override=True,       # EODHD paid: trust all statement types
                    override_scalars=True,    # EODHD scalars override yfinance stubs
                )
                logger.info(
                    f"[DataManager] After EODHD: "
                    f"{len(company.annual_financials)} years, "
                    f"{company.completeness_pct()}% complete"
                )
            else:
                logger.info(f"[DataManager] EODHD unavailable for {ticker}: {result.error}")

        # ── Reset derived compound scalars after EODHD ────────────────────────
        # EODHD just replaced the annual_financials with more accurate data.
        # Any compound scalars yfinance computed earlier (EV multiples, gearing,
        # margins, ROE/ROA) were derived from yfinance's annual stubs and are now
        # stale. Reset them to None so calculate_current_ratios() and the
        # re-derivation block below recompute them from EODHD annual data.
        # enterprise_value and net_debt are also reset so EV recalculates from
        # yfinance market_cap (live) + EODHD net_debt (accurate balance sheet).
        if eodhd_succeeded:
            # Reset only fields that depend on current market data and must be
            # recomputed after EODHD. Do NOT reset margin/roe/roa — EODHD's
            # Highlights block (ProfitMargin, OperatingMarginTTM, ReturnOnEquityTTM)
            # is now the authoritative source for those and must not be discarded.
            for _f in [
                "net_debt",
                "ev_ebit",
                "fcf_yield", "gearing",
            ]:
                setattr(company, _f, None)

        # ── Tier 1c: SEC EDGAR (US tickers — fill-only after EODHD) ──────────
        # EDGAR provides direct-from-SEC filings for US companies.
        # Runs in fill-only mode so it adds depth / fills any EODHD gaps
        # without overwriting EODHD data.  Skipped when EODHD already gave
        # sufficient history (≥ MIN_HISTORY_YEARS).
        if self._edgar and ENABLE_EDGAR and is_us_ticker:
            if len(company.annual_financials) < MIN_HISTORY_YEARS:
                logger.info(
                    f"[DataManager] Running EDGAR for US {ticker} "
                    f"(have {len(company.annual_financials)} years, need {MIN_HISTORY_YEARS})…"
                )
                result = self._edgar.fetch(ticker)
                if result.success and result.data:
                    self._merge(company, result.data, prefer_source="edgar",
                                fields=["annual_financials", "name", "cik"])
                    logger.info(f"[DataManager] After EDGAR: {len(company.annual_financials)} years")
                else:
                    logger.warning(f"[DataManager] EDGAR failed: {result.error}")

        # ── Tier 2: Alpha Vantage (fill gaps or override when EODHD failed) ──
        # When EODHD succeeded → fill-only (don't overwrite accurate paid data)
        # When EODHD failed    → override yfinance stubs for non-US tickers
        # Skipped entirely when EODHD provided sufficient history
        if self._av and ENABLE_ALPHA_VANTAGE:
            needs_history = len(company.annual_financials) < MIN_HISTORY_YEARS
            # Run AV if we still need history OR if EODHD failed for non-US
            run_av = needs_history or (not eodhd_succeeded and not is_us_ticker)
            if run_av:
                av_override = not eodhd_succeeded and not is_us_ticker
                logger.info(
                    f"[DataManager] Running Alpha Vantage for {ticker} "
                    f"({'override fallback' if av_override else 'fill-only'}, "
                    f"have {len(company.annual_financials)} years)…"
                )
                result = self._av.fetch(ticker)
                if result.success and result.data:
                    self._merge(company, result.data, prefer_source="alpha_vantage",
                                fields=["annual_financials"],
                                override_financials=av_override)
                    logger.info(f"[DataManager] After Alpha Vantage: "
                                f"{len(company.annual_financials)} years")
                else:
                    logger.warning(f"[DataManager] Alpha Vantage failed: {result.error}")

        # ── Tier 4: FMP (paid — only if critical fields still missing) ─────────
        # FMP's free / standard plans only cover US-listed equities. Calling
        # FMP for European or Asian tickers produces noisy 404s and never
        # returns useful data, so we restrict the tier to US tickers.
        if self._fmp and ENABLE_FMP and is_us_ticker:
            missing = self._find_missing_critical(company)
            if missing:
                logger.info(f"[DataManager] Running FMP for {ticker} — "
                            f"missing critical: {missing}")
                result = self._fmp.fetch(ticker)
                if result.success and result.data:
                    self._merge(company, result.data, prefer_source="fmp",
                                fields=["annual_financials"] + missing)
                    logger.info(f"[DataManager] After FMP: {company.completeness_pct()}% complete")
                else:
                    logger.warning(f"[DataManager] FMP failed: {result.error}")
        elif self._fmp and ENABLE_FMP and not is_us_ticker:
            logger.debug(f"[DataManager] Skipping FMP for non-US ticker {ticker}")

        # ── Stooq fallback for current_price ──────────────────────────────────
        # When yfinance fails (Streamlit Cloud → Yahoo IP block) and the EODHD
        # plan doesn't include live quotes, current_price stays None. Stooq
        # provides free EOD close prices for global exchanges with no API key
        # — typically 1 trading day fresh, far better than no price at all.
        if company.current_price is None:
            try:
                from .stooq_adapter import fetch_stooq_close
                stooq_price = fetch_stooq_close(ticker)
                if stooq_price is not None:
                    company.current_price = stooq_price
                    logger.info(f"[DataManager] Stooq fallback: {ticker} price = {stooq_price}")
                    # Recompute market_cap if shares are available and EODHD
                    # didn't supply one. (When EODHD provides market_cap, we
                    # keep that — it's coherent with EODHD's snapshot.)
                    if company.market_cap is None and company.shares_outstanding:
                        # shares_outstanding stored in millions
                        company.market_cap = stooq_price * company.shares_outstanding
            except Exception as e:
                logger.debug(f"[DataManager] Stooq fallback failed: {e}")

        # ── Historical year-end prices via yfinance + Alpha Vantage ───────────
        # On Streamlit Cloud Yahoo Finance often blocks .info() but
        # Ticker.history() uses a different endpoint that sometimes still
        # responds. If yfinance fails entirely, Alpha Vantage's
        # TIME_SERIES_MONTHLY endpoint covers 20+ years and works on cloud
        # (free tier, user already has key configured).
        missing_pye_years = [
            yr for yr, af in company.annual_financials.items()
            if af.price_year_end is None
        ]
        if missing_pye_years:
            ye_closes = self._fetch_historical_year_end_closes(ticker)
            if ye_closes:
                filled = 0
                for yr in missing_pye_years:
                    px = ye_closes.get(yr)
                    if px:
                        company.annual_financials[yr].price_year_end = px
                        filled += 1
                logger.info(
                    f"[DataManager] Year-end prices backfilled: "
                    f"{filled}/{len(missing_pye_years)} years"
                )

        # ── Normalize shares_outstanding units across all rows ────────────────
        # Some legacy cached payloads (and a previous yfinance code path)
        # stored per-year shares in raw units (e.g. 46,540,000) while EODHD
        # and the current code path use millions (46.54). Force everything
        # to millions so display sites and derived calculations like
        # total_equity / shares_outstanding produce sensible numbers.
        # Threshold > 1,000,000: no real company has > 1M million shares
        # (that would be a trillion shares), so anything that large is raw.
        for af in company.annual_financials.values():
            if af.shares_outstanding is not None and af.shares_outstanding > 1_000_000:
                af.shares_outstanding = af.shares_outstanding / 1_000_000

        # ── Final derived calculations ─────────────────────────────────────────
        company.calculate_current_ratios()
        for af in company.annual_financials.values():
            # Both shares_outstanding and total_equity are in millions now,
            # so deriving per-share book value, market_cap etc. is uniform.
            if af.market_cap is None and af.price_year_end and af.shares_outstanding:
                af.market_cap = af.price_year_end * af.shares_outstanding
            af.calculate_derived()

        # ── Fill scalar margins from latest annual data — only when EODHD didn't ──
        # EODHD Highlights (ProfitMargin, OperatingMarginTTM, ReturnOnEquityTTM etc.)
        # is the primary source and was already set above. Only derive from annual
        # data here if EODHD didn't provide a value (e.g. EODHD unavailable, or the
        # company is not covered by the Highlights block).
        la = company.latest_annual()
        if la and la.revenue and la.revenue > 0:
            if company.ebit_margin is None and la.ebit is not None:
                company.ebit_margin = la.ebit / la.revenue
            if company.net_margin is None and la.net_income is not None:
                company.net_margin = la.net_income / la.revenue
            if company.ebitda_margin is None and la.ebitda is not None:
                company.ebitda_margin = la.ebitda / la.revenue
            if company.gross_margin is None and la.gross_profit is not None:
                company.gross_margin = la.gross_profit / la.revenue
            if company.roe is None:
                ni_roe = la.net_income_underlying if la.net_income_underlying is not None else la.net_income
                if la.total_equity and la.total_equity > 0 and ni_roe is not None:
                    company.roe = ni_roe / la.total_equity

        # ── Record what's still missing ────────────────────────────────────────
        company.missing_fields = self._find_missing_critical(company)
        if company.missing_fields:
            logger.warning(f"[DataManager] {ticker} still missing: {company.missing_fields}")

        # ── Save to cache ──────────────────────────────────────────────────────
        self._save_cache(ticker, company)

        logger.info(f"[DataManager] Final: {company.summary()}")
        return company

    def get_many(
        self, tickers: List[str], force_refresh: bool = False
    ) -> dict[str, CompanyData]:
        """
        Fetch data for multiple tickers. Returns {ticker: CompanyData}.
        Adds a small delay between tickers to be polite to APIs.
        """
        results = {}
        total = len(tickers)
        for i, ticker in enumerate(tickers, 1):
            logger.info(f"[DataManager] Processing {i}/{total}: {ticker}")
            results[ticker] = self.get(ticker, force_refresh=force_refresh)
            if i < total:
                time.sleep(1.0)  # brief pause between companies
        return results

    # ──────────────────────────────────────────────────────────────────────────
    # Merge logic
    # ──────────────────────────────────────────────────────────────────────────

    def _merge(
        self,
        target: CompanyData,
        source: CompanyData,
        prefer_source: str,
        fields: List[str],
        override_financials: bool = False,
        full_override: bool = False,
        override_scalars: bool = False,
    ) -> None:
        """
        Merge fields from source into target (in-place).

        For annual_financials:
          - override_financials=False (default): fill-only — copy source values
            into target only where target currently has None.
          - override_financials=True, full_override=False: replace income-statement
            fields (revenue, margins, net income, etc.) so that a more reliable
            source overwrites potentially wrong yfinance data.  Balance-sheet
            fields stay fill-only.
          - override_financials=True, full_override=True: replace ALL fields
            (income statement + balance sheet + cash flow) from source.
            Use for EODHD paid data which is trusted for all statement types.

        For scalar fields:
          - override_scalars=False (default): fill-only — only copy if target is None.
          - override_scalars=True: unconditionally overwrite target with source value,
            except for fields in _SCALAR_PROTECTED (live market data from yfinance
            that is more up-to-date than any paid fundamental feed).
        """
        # Fields that must always come from yfinance regardless of override_scalars.
        # current_price and market_cap are real-time; shares_outstanding has a known
        # EODHD bug (commonStock = par-value capital, not share count).
        _SCALAR_PROTECTED = {"current_price", "market_cap", "shares_outstanding",
                             "as_of_date", "input_ticker"}

        if "annual_financials" in fields:
            for year, src_af in source.annual_financials.items():
                if year not in target.annual_financials:
                    # New year — add it wholesale regardless of mode
                    target.annual_financials[year] = src_af
                elif override_financials:
                    # Override mode: replace fields from source
                    tgt_af = target.annual_financials[year]
                    self._override_annual(tgt_af, src_af, full_override=full_override)
                else:
                    # Fill-only mode: only populate fields that are still None
                    tgt_af = target.annual_financials[year]
                    self._merge_annual(tgt_af, src_af)

        # Scalar (and list) fields
        scalar_fields = [f for f in fields if f != "annual_financials"]
        for field in scalar_fields:
            if not hasattr(target, field) or not hasattr(source, field):
                continue
            src_val = getattr(source, field)
            src_has = (src_val is not None and
                       not (isinstance(src_val, list) and len(src_val) == 0))
            if not src_has:
                continue

            if override_scalars and field not in _SCALAR_PROTECTED:
                # EODHD paid data takes priority — overwrite unconditionally
                setattr(target, field, src_val)
                # Track this field as EODHD-sourced for PDF checkmark indicators
                if hasattr(target, "eodhd_fields") and field not in target.eodhd_fields:
                    target.eodhd_fields.append(field)
            else:
                # Fill-only: only write when target is empty
                tgt_val = getattr(target, field)
                tgt_empty = (tgt_val is None or
                             (isinstance(tgt_val, list) and len(tgt_val) == 0))
                if tgt_empty:
                    setattr(target, field, src_val)

        # Track sources
        if prefer_source not in target.data_sources:
            target.data_sources.append(prefer_source)

    def _merge_annual(self, target: AnnualFinancials, source: AnnualFinancials) -> None:
        """Fill None fields in target AnnualFinancials from source."""
        fields = [
            "revenue", "gross_profit", "ebitda", "ebit", "net_income",
            "eps_diluted", "dividends_per_share",
            "gross_margin", "ebit_margin", "ebitda_margin", "net_margin",
            "total_assets", "total_debt", "cash", "net_debt", "total_equity",
            "shares_outstanding", "operating_cash_flow", "capex", "fcf",
            "roe", "roa",
        ]
        for f in fields:
            if getattr(target, f, None) is None and getattr(source, f, None) is not None:
                setattr(target, f, getattr(source, f))

    def _override_annual(
        self,
        target: AnnualFinancials,
        source: AnnualFinancials,
        full_override: bool = False,
    ) -> None:
        """
        Override annual financial fields in target with source values.

        full_override=False (default):
            Income-statement + cash flow fields are replaced unconditionally.
            Balance-sheet fields are fill-only (yfinance usually has these right).
            Use for Alpha Vantage (non-US fallback).

        full_override=True:
            ALL fields are replaced when source has a value.
            Use for EODHD paid data which is trusted across all statement types.
        """
        # Income statement + cash flow: always override from EODHD (full_override=True).
        # net_income is handled separately below (override for EODHD, fill-only otherwise).
        income_cf_fields = [
            "revenue", "gross_profit", "ebitda", "ebit",
            "dividends_per_share",
            "gross_margin", "ebit_margin", "ebitda_margin", "net_margin",
            "operating_cash_flow", "capex", "fcf",
        ]
        for f in income_cf_fields:
            src_val = getattr(source, f, None)
            if src_val is not None:
                setattr(target, f, src_val)

        # net_income: override when full_override=True (EODHD is authoritative);
        # fill-only for other sources (Alpha Vantage etc.)
        src_ni = getattr(source, "net_income", None)
        if src_ni is not None:
            if full_override or getattr(target, "net_income", None) is None:
                target.net_income = src_ni

        # eps_diluted: ALWAYS override with EODHD Earnings.Annual epsActual.
        # yfinance reports IFRS EPS which can be significantly lower than the
        # underlying EPS that analyst consensus tracks (e.g. RHM FY2025:
        # yfinance 15.16 vs actual underlying 25.72). EODHD is authoritative here.
        src_eps = getattr(source, "eps_diluted", None)
        if src_eps is not None:
            target.eps_diluted = src_eps

        # Balance sheet: override unconditionally if full_override, else fill-only
        balance_fields = [
            "total_assets", "total_debt", "cash", "net_debt", "total_equity",
            "shares_outstanding", "roe", "roa",
        ]
        for f in balance_fields:
            src_val = getattr(source, f, None)
            if src_val is not None:
                if full_override or getattr(target, f, None) is None:
                    setattr(target, f, src_val)

        # Mark this annual row as EODHD-sourced so PDF generators can show ✓
        if full_override:
            target.source = "eodhd"

        # ── Reset derived fields so calculate_derived() recomputes them ──────────
        # After EODHD overrides income/balance fields, any derived ratios that
        # yfinance previously computed (roe, pe_ratio, ev_ebit, etc.) are now stale
        # because they were based on pre-override data. Resetting them to None
        # forces the final calculate_derived() call in DataManager.get() to
        # recompute them using the updated source values.
        for derived in (
            "roe", "roa", "net_margin", "pe_ratio",
            "ev_ebit", "ev_sales", "fcf_yield", "div_yield",
            "enterprise_value",
            "net_income_underlying",  # recomputed from updated eps_diluted × shares
        ):
            setattr(target, derived, None)

    # ──────────────────────────────────────────────────────────────────────────
    # Cache
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_historical_year_end_closes(self, ticker: str) -> dict:
        """
        Return {year: last_close_price} for the longest history we can get.

        Tries yfinance.Ticker.history(period='max') first — works locally and
        sometimes on Streamlit Cloud since .history uses Yahoo's chart endpoint
        which is less restricted than the quoteSummary endpoint that .info
        relies on. Falls back to Alpha Vantage TIME_SERIES_MONTHLY with a few
        candidate symbol formats for non-US exchanges.
        Returns an empty dict on total failure.
        """
        # Tier 1: yfinance
        try:
            import yfinance as yf
            df = yf.Ticker(ticker).history(period="max", interval="1mo")
            if df is not None and not df.empty and "Close" in df.columns:
                ye = {}
                for ts, row in df.iterrows():
                    yr = ts.year
                    close = float(row["Close"])
                    if close > 0:
                        ye[yr] = close   # later months overwrite earlier ones
                if ye:
                    logger.info(f"[DataManager] yfinance year-end: {len(ye)} years for {ticker}")
                    return ye
        except Exception as e:
            logger.debug(f"[DataManager] yfinance year-end fetch failed: {e}")

        # Tier 2: Alpha Vantage TIME_SERIES_MONTHLY
        try:
            import requests
            from config import ALPHA_VANTAGE_API_KEY
            if not ALPHA_VANTAGE_API_KEY:
                return {}

            # AV symbol candidates: stripped base for US, +.DEX/.FRK/.LON/etc.
            # for European listings. Yahoo suffix mapping ↦ AV suffix.
            yf_to_av = {
                ".DE": "DEX", ".F": "FRK", ".L": "LON", ".PA": "PAR",
                ".AS": "AMS", ".BR": "BRU", ".MI": "MIL", ".MC": "MAD",
                ".HE": "HEL", ".ST": "STO", ".OL": "OSL", ".CO": "CPH",
                ".SW": "SWX", ".VI": "VIE", ".WA": "WSE", ".LS": "LIS",
                ".TO": "TRT", ".HK": "HKG", ".AX": "ASX",
            }
            dot = ticker.rfind(".")
            if dot == -1:
                candidates = [ticker]              # US — base symbol
            else:
                base, suffix = ticker[:dot], ticker[dot:]
                av_suffix = yf_to_av.get(suffix.upper())
                candidates = [f"{base}.{av_suffix}"] if av_suffix else []
                candidates.append(base)            # final fallback

            for sym in candidates:
                params = {
                    "function": "TIME_SERIES_MONTHLY",
                    "symbol": sym,
                    "apikey": ALPHA_VANTAGE_API_KEY,
                }
                r = requests.get("https://www.alphavantage.co/query",
                                 params=params, timeout=20)
                if r.status_code != 200:
                    continue
                data = r.json()
                series = data.get("Monthly Time Series")
                if not isinstance(series, dict) or not series:
                    continue
                ye = {}
                for date_str, row in series.items():
                    try:
                        yr = int(date_str[:4])
                        close = float(row.get("4. close", 0))
                        if close > 0:
                            # AV is sorted desc, so first hit per year is the
                            # latest available month → year-end close.
                            ye.setdefault(yr, close)
                    except (ValueError, TypeError):
                        continue
                if ye:
                    logger.info(
                        f"[DataManager] AV year-end ({sym}): {len(ye)} years"
                    )
                    return ye
        except Exception as e:
            logger.debug(f"[DataManager] AV year-end fetch failed: {e}")

        return {}

    def _cache_path(self, ticker: str) -> Path:
        safe = ticker.replace(".", "_").replace("/", "_")
        return CACHE_DIR / f"{safe}.json"

    def _save_cache(self, ticker: str, company: CompanyData) -> None:
        """Serialize CompanyData to JSON and save to disk."""
        try:
            path = self._cache_path(ticker)
            data = _company_to_dict(company)
            data["_cached_at"] = datetime.utcnow().isoformat()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            logger.debug(f"[DataManager] Cached {ticker} → {path}")
        except Exception as e:
            logger.warning(f"[DataManager] Cache write failed for {ticker}: {e}")

    def _load_cache(self, ticker: str) -> Optional[CompanyData]:
        """Load and deserialize cached CompanyData if still fresh."""
        path = self._cache_path(ticker)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            cached_at_str = data.get("_cached_at")
            if not cached_at_str:
                return None

            cached_at = datetime.fromisoformat(cached_at_str)
            age = datetime.utcnow() - cached_at
            if age > timedelta(hours=CACHE_TTL_HOURS):
                logger.debug(f"[DataManager] Cache expired for {ticker} (age: {age})")
                return None

            return _dict_to_company(data)
        except Exception as e:
            logger.warning(f"[DataManager] Cache read failed for {ticker}: {e}")
            return None

    def clear_cache(self, ticker: str = None) -> int:
        """
        Clear cache for one ticker, or all tickers if ticker is None.
        Returns number of files deleted.
        """
        if ticker:
            path = self._cache_path(ticker)
            if path.exists():
                path.unlink()
                return 1
            return 0
        else:
            count = 0
            for f in CACHE_DIR.glob("*.json"):
                f.unlink()
                count += 1
            logger.info(f"[DataManager] Cleared {count} cache files.")
            return count

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _find_missing_critical(self, company: CompanyData) -> List[str]:
        """Return list of critical field names that are still None."""
        return [f for f in CRITICAL_FIELDS if getattr(company, f, None) is None]


# ──────────────────────────────────────────────────────────────────────────────
# Serialization helpers (CompanyData ↔ dict for JSON cache)
# ──────────────────────────────────────────────────────────────────────────────

def _company_to_dict(c: CompanyData) -> dict:
    """Convert CompanyData to a JSON-serializable dict."""
    import dataclasses
    d = {}
    for k, v in c.__dict__.items():
        if k == "annual_financials":
            continue
        elif k == "forward_estimates" and v is not None:
            # Explicitly serialize ForwardEstimates as a plain dict
            d[k] = {fld.name: getattr(v, fld.name)
                    for fld in dataclasses.fields(v)}
        else:
            d[k] = v
    d["annual_financials"] = {
        str(yr): af.__dict__
        for yr, af in c.annual_financials.items()
    }
    return d


def _dict_to_company(d: dict) -> CompanyData:
    """Reconstruct CompanyData from a cached dict."""
    import dataclasses
    annual_raw = d.pop("annual_financials", {})
    fe_raw     = d.pop("forward_estimates", None)
    d.pop("_cached_at", None)

    # Only pass fields that CompanyData actually has
    valid_fields = {f.name for f in dataclasses.fields(CompanyData)}
    clean = {k: v for k, v in d.items() if k in valid_fields}

    company = CompanyData(**clean)

    # Reconstruct ForwardEstimates if present
    if isinstance(fe_raw, dict) and "year" in fe_raw:
        try:
            valid_fe = {f.name for f in dataclasses.fields(ForwardEstimates)}
            clean_fe = {k: v for k, v in fe_raw.items() if k in valid_fe}
            company.forward_estimates = ForwardEstimates(**clean_fe)
        except Exception:
            pass

    for yr_str, af_dict in annual_raw.items():
        try:
            yr = int(yr_str)
            valid_af = {f.name for f in dataclasses.fields(AnnualFinancials)}
            clean_af = {k: v for k, v in af_dict.items() if k in valid_af}
            company.annual_financials[yr] = AnnualFinancials(**clean_af)
        except Exception:
            pass

    return company


def _is_us_ticker(ticker: str) -> bool:
    """
    Heuristic: ticker is US-listed if it has no exchange suffix
    (or has a class-share suffix like .A, .B which are still US).
    """
    parts = ticker.split(".")
    if len(parts) == 1:
        return True
    suffix = parts[-1].upper()
    # Class-share suffixes used on US exchanges
    if suffix in ("A", "B", "C", "K", "P", "W", "R", "U", "WS"):
        return True
    return False


# ── Quick integration test ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    dm = DataManager()

    test_cases = [
        ("AAPL",      "US  — Apple Inc."),
        ("MSFT",      "US  — Microsoft"),
        ("WKL.AS",    "EU  — Wolters Kluwer (Amsterdam)"),
        ("NOKIA.HE",  "EU  — Nokia (Helsinki)"),
        ("ATCO-A.ST", "EU  — Atlas Copco (Stockholm)"),
    ]

    # Allow overriding test tickers from CLI: python data_manager.py AAPL MSFT
    if len(sys.argv) > 1:
        test_cases = [(t, t) for t in sys.argv[1:]]

    for ticker, label in test_cases:
        print(f"\n{'='*65}")
        print(f"  {label}")
        print(f"{'='*65}")
        c = dm.get(ticker)
        print(f"  {c.summary()}")
        print(f"  Missing fields: {c.missing_fields or 'none'}")
        print(f"  Annual history ({c.year_range()}):")
        for yr in c.sorted_years()[:5]:
            af = c.annual_financials[yr]
            print(
                f"    {yr}: Rev={_fmt(af.revenue)}M | EBIT={_fmt(af.ebit)}M | "
                f"NI={_fmt(af.net_income)}M | EPS={af.eps_diluted} | "
                f"FCF={_fmt(af.fcf)}M"
            )
        print(f"  Ratios: P/E={c.pe_ratio} | EV/EBIT={c.ev_ebit} | "
              f"EV/Sales={c.ev_sales} | ROE={_pct(c.roe)} | "
              f"NetMargin={_pct(c.net_margin)} | DivYield={_pct(c.dividend_yield)}")


def _fmt(v) -> str:
    return f"{v:,.0f}" if v is not None else "n/a"

def _pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"
