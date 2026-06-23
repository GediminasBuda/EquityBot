"""
yfinance_adapter.py — Tier 1 primary data source.

Covers: prices, market cap, financials, ratios for US + global exchanges.
Free, no API key. Returns ~4 years of annual data.
Global coverage: US, EU (AMS/STO/LSE/XETRA/HEL…), Asia, LatAm.
"""

from __future__ import annotations
import time
import logging
from datetime import datetime
from typing import Optional

import yfinance as yf
import pandas as pd

from .base import CompanyData, AnnualFinancials, ForwardEstimates, DataSourceResult

logger = logging.getLogger(__name__)


def _safe(val, cast=None, scale=1.0):
    """Return val (optionally cast and scaled), or None if missing/NaN."""
    if val is None:
        return None
    try:
        if isinstance(val, float) and pd.isna(val):
            return None
        v = cast(val) if cast else val
        return v * scale if scale != 1.0 else v
    except Exception:
        return None


def _df_val(df: pd.DataFrame, row_key: str, col_idx: int = 0) -> Optional[float]:
    """
    Safely pull a value from a yfinance financial DataFrame.
    Rows are metric names, columns are dates (most recent = col 0).
    Values from yfinance are in raw units (dollars, not millions).
    We convert to millions for consistency.
    """
    if df is None or df.empty:
        return None
    # yfinance sometimes uses slightly different key names — try a few
    candidates = [row_key, row_key.replace(" ", ""), row_key.title()]
    for key in candidates:
        if key in df.index:
            try:
                raw = df.loc[key].iloc[col_idx]
                if pd.isna(raw):
                    return None
                return float(raw) / 1_000_000  # convert to millions
            except Exception:
                return None
    return None


class YFinanceAdapter:
    """
    Fetches company data from Yahoo Finance via the yfinance library.

    Data returned (in millions where monetary):
    - Company profile: name, sector, industry, country, description
    - Current market: price, market cap, shares outstanding
    - Current ratios: P/E, EV/EBIT (calculated), EV/Sales, ROE, margins, etc.
    - Annual history: up to 4 years of income stmt, balance sheet, cash flow
    """

    SOURCE_NAME = "yfinance"

    def fetch(self, ticker: str) -> DataSourceResult:
        """
        Main entry point. ticker should be Yahoo Finance format
        (e.g. "WKL.AS", "ATCO-A.ST", "AAPL", "NOKIA.HE").
        """
        start = time.time()
        logger.info(f"[yfinance] Fetching {ticker}…")

        try:
            yt = yf.Ticker(ticker)
            # Ticker.info calls Yahoo's quoteSummary endpoint via a crumb/cookie
            # handshake that is frequently rate-limited on Streamlit Cloud's
            # shared IPs (YFRateLimitError). That used to take down this whole
            # fetch() — info={} lets us fall through to the chart/fundamentals-
            # timeseries fallbacks (lighter-weight, separate endpoints) instead
            # of failing the ticker outright.
            try:
                info = yt.info or {}
            except Exception as e:
                logger.warning(f"[yfinance] .info failed for {ticker}, continuing with fallbacks: {e}")
                info = {}

            # Only fail outright when info AND the chart fallback both have
            # nothing — i.e. genuinely no signal that this ticker exists.
            if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
                if not info.get("longName") and not info.get("shortName"):
                    chart_snap = None
                    try:
                        from .yahoo_chart_fallback import fetch_yahoo_chart_snapshot
                        chart_snap = fetch_yahoo_chart_snapshot(ticker)
                    except Exception:
                        chart_snap = None
                    if chart_snap is None:
                        return DataSourceResult(
                            success=False,
                            source_name=self.SOURCE_NAME,
                            error=f"No data returned for ticker '{ticker}'. Check ticker format.",
                            duration_seconds=time.time() - start,
                        )
                    logger.info(f"[yfinance] .info empty for {ticker} but chart API confirms it exists; continuing")

            company = CompanyData(
                ticker=ticker,
                input_ticker=ticker,
                fetch_timestamp=datetime.utcnow().isoformat(),
            )

            fields_filled = []

            # ── Identity ─────────────────────────────────────────────────────
            company.name     = info.get("longName") or info.get("shortName")
            company.exchange = info.get("exchange") or info.get("exchangeShortName")
            company.currency = info.get("financialCurrency") or info.get("currency")
            company.currency_price = info.get("currency")
            company.sector   = info.get("sector")
            company.industry = info.get("industry")
            company.country  = info.get("country")
            company.website  = info.get("website")
            company.description = info.get("longBusinessSummary")
            company.employees = _safe(info.get("fullTimeEmployees"), int)

            for f in ["name", "sector", "industry", "country", "description"]:
                if getattr(company, f):
                    fields_filled.append(f)

            # ── Current Market Data ───────────────────────────────────────────
            price = (
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or info.get("previousClose")
            )
            company.current_price = _safe(price, float)

            # Market cap from info (in raw units → millions)
            mc_raw = info.get("marketCap")
            company.market_cap = _safe(mc_raw, float, 1 / 1_000_000)

            shares_raw = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
            company.shares_outstanding = _safe(shares_raw, float, 1 / 1_000_000)

            ev_raw = info.get("enterpriseValue")
            company.enterprise_value = _safe(ev_raw, float, 1 / 1_000_000)

            company.as_of_date = datetime.utcnow().strftime("%Y-%m-%d")

            for f in ["current_price", "market_cap", "shares_outstanding"]:
                if getattr(company, f) is not None:
                    fields_filled.append(f)

            # ── Current Valuation Multiples ───────────────────────────────────
            company.pe_ratio      = _safe(info.get("trailingPE"), float)
            company.forward_pe    = _safe(info.get("forwardPE"), float)
            company.price_to_book = _safe(info.get("priceToBook"), float)
            company.ev_ebitda     = _safe(info.get("enterpriseToEbitda"), float)
            company.ev_sales      = _safe(info.get("enterpriseToRevenue"), float)
            company.beta          = _safe(info.get("beta"), float)

            # Dividend yield: yfinance always returns this as a percentage-style
            # float (e.g. 0.38 means 0.38%, 1.23 means 1.23%), unlike margins/ROE
            # which are returned as true decimals (0.272 = 27.2%).
            # We normalise to decimal by dividing by 100 for consistent storage.
            dy = _safe(info.get("dividendYield"), float)
            if dy is not None:
                dy = dy / 100   # 0.38 → 0.0038 (0.38%), 1.23 → 0.0123 (1.23%)
            company.dividend_yield = dy

            for f in ["pe_ratio", "ev_ebitda", "ev_sales", "dividend_yield"]:
                if getattr(company, f) is not None:
                    fields_filled.append(f)

            # ── Current Profitability ─────────────────────────────────────────
            company.gross_margin  = _safe(info.get("grossMargins"), float)
            company.ebit_margin   = _safe(info.get("operatingMargins"), float)
            company.net_margin    = _safe(info.get("profitMargins"), float)
            company.roe           = _safe(info.get("returnOnEquity"), float)
            company.roa           = _safe(info.get("returnOnAssets"), float)

            for f in ["net_margin", "ebit_margin", "roe"]:
                if getattr(company, f) is not None:
                    fields_filled.append(f)

            # ── Current Balance Sheet Snapshot ────────────────────────────────
            total_debt_raw = info.get("totalDebt")
            total_cash_raw = info.get("totalCash")
            total_debt = _safe(total_debt_raw, float, 1 / 1_000_000)
            total_cash = _safe(total_cash_raw, float, 1 / 1_000_000)

            if total_debt is not None and total_cash is not None:
                company.net_debt = total_debt - total_cash
            elif total_debt is not None:
                company.net_debt = total_debt
            elif total_cash is not None:
                # No totalDebt in info → treat as zero financial debt.
                # We intentionally do NOT use totalNonCurrentLiabilities as a
                # proxy: NCL includes lease liabilities, deferred taxes, and
                # other non-debt items which grossly overstate financial debt
                # for capital-light companies (e.g. Japanese platform stocks).
                # The annual balance sheet path has its own targeted key search.
                company.net_debt = -total_cash
                logger.debug(f"[yfinance] net_debt: no totalDebt, assuming zero financial debt → {company.net_debt:.1f}M")

            # Recompute enterprise_value from MCap + net_debt for reliability.
            # yfinance's info["enterpriseValue"] and info["enterpriseToRevenue"]
            # can be stale or inconsistent (especially for TSE stocks). Using
            # MCap + net_debt gives consistent, current-price-based EV.
            if company.market_cap is not None and company.net_debt is not None:
                company.enterprise_value = company.market_cap + company.net_debt
            # else: keep yfinance's enterpriseValue if we couldn't compute net_debt

            company.current_ratio  = _safe(info.get("currentRatio"), float)
            company.debt_to_equity = _safe(info.get("debtToEquity"), float)

            # TTM EPS from trailingEps (more reliable than quarterly sum)
            company.eps_ttm = _safe(info.get("trailingEps"), float)
            # revenuePerShare from info is intentionally NOT stored as a fallback
            # for TTM revenue: for TSE stocks it returns the most recent single
            # quarter's per-share revenue, not the trailing twelve-month total.
            # TTM revenue is fetched below via info["totalRevenue"] instead.
            company.revenue_per_share = _safe(info.get("revenuePerShare"), float)

            # NOTE: info["totalRevenue"] is intentionally NOT used here.
            # For TSE stocks yfinance returns the most recent single quarter's
            # revenue in this field (e.g. 604M instead of TTM 2,295M for 2477.T).
            # TTM revenue is derived from quarterly_financials sum or latest annual.

            # ── Annual Financial History ──────────────────────────────────────
            q_financials = q_cashflow = q_balance = None
            try:
                financials = yt.financials          # Income statement
                balance    = yt.balance_sheet       # Balance sheet
                cashflow   = yt.cashflow            # Cash flows

                # Quarterly data used as cross-check for partial-year annual figures
                # and for computing TTM sums from last 4 quarters.
                try:
                    q_financials = yt.quarterly_financials
                    q_cashflow   = yt.quarterly_cashflow
                    q_balance    = yt.quarterly_balance_sheet
                except Exception:
                    q_financials = q_cashflow = q_balance = None
                self._parse_annual_history(
                    company, financials, balance, cashflow, fields_filled,
                    q_financials=q_financials,
                    q_cashflow=q_cashflow,
                    q_balance=q_balance,
                )
            except Exception as e:
                logger.warning(f"[yfinance] Could not fetch annual history for {ticker}: {e}")

            # ── Fundamentals-timeseries fallback (Streamlit Cloud IP block) ─────
            # yt.financials/.balance_sheet/.cashflow call Yahoo's quoteSummary
            # endpoint, which is the endpoint most often blocked on Streamlit
            # Cloud. If that left us with no annual history at all, try Yahoo's
            # separate fundamentals-timeseries API directly — it's lighter-
            # weight and frequently still responds when quoteSummary doesn't.
            if not company.annual_financials:
                try:
                    from .yahoo_fundamentals_fallback import fetch_yahoo_annual_financials
                    fallback_years = fetch_yahoo_annual_financials(ticker)
                    if fallback_years:
                        for year, fields in fallback_years.items():
                            af = company.annual_financials.get(year, AnnualFinancials(year=year))
                            for field, value in fields.items():
                                setattr(af, field, value)
                            af.calculate_derived()
                            company.annual_financials[year] = af
                        fields_filled.append("annual_financials")
                        logger.info(
                            f"[yfinance] Fundamentals-timeseries fallback recovered "
                            f"{len(fallback_years)} years for {ticker}"
                        )
                        if company.shares_outstanding is None:
                            latest_year = max(fallback_years)
                            latest_shares = fallback_years[latest_year].get("shares_outstanding")
                            if latest_shares:
                                company.shares_outstanding = latest_shares
                except Exception as e:
                    logger.warning(f"[yfinance] Fundamentals-timeseries fallback failed for {ticker}: {e}")

            # ── TTM from financials first column (reuses already-fetched data) ──
            # yt.financials is already in memory. Its first (most recent) column
            # is the TTM period when it post-dates the last fiscal year end —
            # identical to the "TTM" column Yahoo Finance shows alongside annual
            # figures. We read this BEFORE the quarterly-sum so it takes priority.
            # We never call yt.income_stmt: it makes a separate HTTP request that
            # can corrupt yfinance's internal cache and break the whole fetch.
            try:
                if financials is not None and not financials.empty:
                    _fcols = sorted(financials.columns, reverse=True)
                    if len(_fcols) >= 2 and _fcols[0] > _fcols[1]:
                        _ttm_col = _fcols[0]

                        def _fval(key):
                            try:
                                v = financials.loc[key, _ttm_col]
                                return float(v) / 1_000_000 if not pd.isna(v) else None
                            except Exception:
                                return None

                        for _rk in ["Total Revenue", "Operating Revenue"]:
                            _rv = _fval(_rk)
                            if _rv and _rv > 0:
                                company.ttm_revenue = _rv
                                logger.debug(f"[yfinance] TTM revenue from financials[{_rk}]: {_rv:.1f}M")
                                break
                        for _ek in ["Operating Income", "EBIT"]:
                            _ev2 = _fval(_ek)
                            if _ev2 is not None:
                                company.ttm_ebit = _ev2
                                break
                        for _nk in ["Net Income", "Net Income Common Stockholders"]:
                            _nv = _fval(_nk)
                            if _nv is not None:
                                company.ttm_net_income = _nv
                                break
            except Exception as e:
                logger.debug(f"[yfinance] financials TTM column read failed: {e}")

            # ── TTM metrics from last 4 reported quarters ─────────────────────
            try:
                self._compute_ttm_metrics(company, q_financials, q_cashflow, fields_filled)
            except Exception as e:
                logger.warning(f"[yfinance] TTM computation failed for {ticker}: {e}")

            # ── TTM last quarter date + next earnings date ────────────────────
            try:
                if q_financials is not None and not q_financials.empty:
                    _qcols = sorted(q_financials.columns, reverse=True)
                    if _qcols:
                        company.ttm_last_quarter_date = str(_qcols[0].date())
            except Exception:
                pass
            try:
                _ed = info.get("earningsDate") or info.get("earningsTimestamp")
                if _ed:
                    if isinstance(_ed, (list, tuple)):
                        _ed = _ed[0]
                    import datetime as _dt
                    if isinstance(_ed, (int, float)):
                        _ed = _dt.datetime.utcfromtimestamp(_ed).strftime("%Y-%m-%d")
                    elif hasattr(_ed, "strftime"):
                        _ed = _ed.strftime("%Y-%m-%d")
                    company.next_earnings_date = str(_ed)
            except Exception:
                pass

            # ── Re-derive net_debt and EV from quarterly balance sheet ────────
            # info["totalCash"] = cash-and-equivalents ONLY. For companies that
            # hold significant short-term financial investments (common for
            # Japanese stocks), this understates liquid assets by several billion.
            # The quarterly balance sheet key "Cash Cash Equivalents And Short
            # Term Investments" captures the full liquid asset base — same key
            # used in the annual history parser, which gives the correct -6.6B
            # annual figures. We redo the scalar net_debt/EV with this better source.
            try:
                self._fix_net_debt_from_balance_sheet(company, q_balance)
            except Exception as e:
                logger.debug(f"[yfinance] quarterly balance sheet net_debt fix failed: {e}")

            # ── TTM Revenue fallback to latest annual ─────────────────────────
            # If info["totalRevenue"] returned None AND quarterly sum failed,
            # use the most recent annual revenue as best available approximation.
            if getattr(company, 'ttm_revenue', None) is None:
                la = company.latest_annual()
                if la and la.revenue:
                    company.ttm_revenue = la.revenue
                    logger.debug(f"[yfinance] TTM revenue fallback to latest annual: {la.revenue:.1f}M")

            # ── Historical Dividends Per Share (sum payments by calendar year) ───
            try:
                divs = yt.dividends
                if divs is not None and not divs.empty:
                    # Group all dividend payments by calendar year and sum
                    dps_by_year: dict[int, float] = {}
                    for ts, amount in divs.items():
                        try:
                            yr = pd.Timestamp(ts).year
                            dps_by_year[yr] = dps_by_year.get(yr, 0.0) + float(amount)
                        except Exception:
                            pass

                    # ── Spring-payer detection (Continental European style) ──────
                    # Many European companies declare a dividend for fiscal year Y
                    # but pay it in April-June of year Y+1 (e.g. German AGM in May).
                    # yfinance groups by PAYMENT year, so those payments land in
                    # calendar year Y+1 — one year too late.
                    # Fix: if ≥70% of all dividend payments fall in months 4-6,
                    # treat each payment as belonging to fiscal year = payment_year - 1.
                    payment_months = []
                    for ts in divs.index:
                        try:
                            payment_months.append(pd.Timestamp(ts).month)
                        except Exception:
                            pass

                    spring_count = sum(1 for m in payment_months if 4 <= m <= 6)
                    is_spring_payer = (
                        len(payment_months) > 0
                        and spring_count / len(payment_months) >= 0.7
                    )

                    if is_spring_payer:
                        # Payment in calendar year Y+1 → fiscal year Y
                        dps_assign: dict[int, float] = {}
                        for ts, amount in divs.items():
                            try:
                                fiscal_yr = pd.Timestamp(ts).year - 1
                                dps_assign[fiscal_yr] = dps_assign.get(fiscal_yr, 0.0) + float(amount)
                            except Exception:
                                pass
                        logger.debug(
                            f"[yfinance] Spring-payer detected — shifted DPS: {dps_assign}"
                        )
                    else:
                        dps_assign = dps_by_year

                    # Assign to matching AnnualFinancials records
                    for year, af in company.annual_financials.items():
                        if year in dps_assign and af.dividends_per_share is None:
                            # yfinance .L dividends are in pence; convert to GBP
                            _dps_gbx = 100.0 if ticker.upper().endswith(".L") else 1.0
                            af.dividends_per_share = dps_assign[year] / _dps_gbx
                    logger.debug(f"[yfinance] DPS by year: {dps_by_year}")
            except Exception as e:
                logger.warning(f"[yfinance] Could not fetch dividend history for {ticker}: {e}")

            # ── Historical Year-End Prices → per-year valuation ratios ──────────
            # Fetch monthly price history and assign Dec-31 (or last-of-year)
            # closing price to each AnnualFinancials record, then derive
            # historical P/E, EV/EBIT, EV/Sales, FCF Yield, Div Yield, Mkt Cap.
            try:
                hist = yt.history(period="max", interval="1mo")
                if hist is not None and not hist.empty:
                    # current shares in millions (from CompanyData, set above)
                    current_shares_m = company.shares_outstanding  # millions

                    # LSE stocks quote in GBX (pence); financials are in GBP.
                    # Divide price by 100 so market cap and P/E are consistent.
                    _gbx_factor = 100.0 if ticker.upper().endswith(".L") else 1.0

                    for year, af in company.annual_financials.items():
                        year_prices = hist[hist.index.year == year]
                        if year_prices.empty:
                            continue
                        # last available monthly close for that calendar year
                        af.price_year_end = float(year_prices["Close"].iloc[-1]) / _gbx_factor

                        # shares this year are already stored in millions
                        # (matches the convention used by EODHD).
                        if af.shares_outstanding is not None and af.shares_outstanding > 0:
                            shares_m = af.shares_outstanding
                        elif current_shares_m is not None and current_shares_m > 0:
                            shares_m = current_shares_m
                        else:
                            shares_m = None

                        if shares_m:
                            af.market_cap = af.price_year_end * shares_m  # → millions

                        # Enterprise Value = market cap + net debt
                        if af.market_cap is not None:
                            nd = af.net_debt  # already in millions
                            if nd is not None:
                                af.enterprise_value = af.market_cap + nd
                            else:
                                af.enterprise_value = af.market_cap  # rough if no debt data

                        # Derive P/E, EV/EBIT, EV/Sales, FCF Yield, Div Yield
                        af.calculate_derived()

                    fields_filled.append("historical_valuations")
                    logger.debug(f"[yfinance] Year-end prices fetched for {ticker}")
            except Exception as e:
                logger.warning(f"[yfinance] Could not compute historical valuations for {ticker}: {e}")

            # ── Analyst Forward Estimates ─────────────────────────────────────
            try:
                self._parse_forward_estimates(company, yt, info)
            except Exception as e:
                logger.warning(f"[yfinance] Could not fetch forward estimates for {ticker}: {e}")

            # ── Derived Calculations ──────────────────────────────────────────
            company.calculate_current_ratios()

            # EV/EBIT — yfinance doesn't provide this directly, we calculate it.
            # Prefer TTM EBIT for currency; fall back to latest annual.
            ev = company.enterprise_value
            if ev:
                if company.ttm_ebit and company.ttm_ebit > 0:
                    company.ev_ebit = ev / company.ttm_ebit
                    fields_filled.append("ev_ebit")
                elif company.ev_ebit is None and company.latest_annual():
                    la = company.latest_annual()
                    if la and la.ebit and la.ebit > 0:
                        company.ev_ebit = ev / la.ebit
                        fields_filled.append("ev_ebit")

            # EV/Sales — always compute from our EV and TTM revenue rather than
            # using yfinance's pre-computed enterpriseToRevenue, which can be
            # stale or internally inconsistent (seen on TSE stocks).
            if ev and company.ttm_revenue and company.ttm_revenue > 0:
                company.ev_sales = ev / company.ttm_revenue
                fields_filled.append("ev_sales")
            elif company.ev_sales is None:
                # Fallback: use latest annual revenue
                la = company.latest_annual()
                if la and la.revenue and la.revenue > 0 and ev:
                    company.ev_sales = ev / la.revenue

            # FCF Yield
            if company.fcf_yield is None and company.market_cap and company.latest_annual():
                la = company.latest_annual()
                if la and la.fcf and company.market_cap > 0:
                    company.fcf_yield = la.fcf / company.market_cap
                    fields_filled.append("fcf_yield")

            company.data_sources = [self.SOURCE_NAME]

            logger.info(
                f"[yfinance] {ticker} done. "
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

        except Exception as e:
            logger.error(f"[yfinance] Error fetching {ticker}: {e}", exc_info=True)
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error=str(e),
                duration_seconds=time.time() - start,
            )

    # ── Quarterly cross-validation helpers ───────────────────────────────────

    @staticmethod
    def _sum_quarterly(
        q_df: pd.DataFrame,
        row_key: str,
        year: int,
        fiscal_month_end: int = 12,
    ) -> Optional[float]:
        """
        Sum up to 4 quarterly values whose period-end dates fall within the
        fiscal year ending in *fiscal_month_end* of *year*.

        For a Dec-31 year (fiscal_month_end=12) this is Q1–Q4 of calendar year.
        For a Sep-30 year (fiscal_month_end=9) this is Oct(y-1)–Sep(y).

        Returns the sum in millions, or None if fewer than 3 quarters found.
        """
        if q_df is None or q_df.empty:
            return None
        # Collect columns whose date falls in the fiscal year window
        fy_end   = pd.Timestamp(year=year,          month=fiscal_month_end, day=1) + pd.offsets.MonthEnd(0)
        fy_start = pd.Timestamp(year=year - 1,      month=fiscal_month_end, day=1) + pd.offsets.MonthEnd(0) + pd.Timedelta(days=1)

        candidates = [c for c in q_df.columns
                      if fy_start <= pd.Timestamp(c) <= fy_end]
        if len(candidates) < 3:
            return None          # not enough quarters — don't override

        total = 0.0
        for c in candidates:
            v = _df_val(q_df, row_key, q_df.columns.get_loc(c))
            if v is None:
                return None      # any missing quarter → can't sum reliably
            total += v
        return total

    @staticmethod
    def _detect_fiscal_month_end(financials: pd.DataFrame) -> int:
        """
        Inspect the annual financials columns to guess fiscal year-end month.
        e.g. columns 2024-12-31 → 12,  2024-09-30 → 9.
        Default: 12 (Dec).
        """
        if financials is None or financials.empty:
            return 12
        try:
            return int(pd.Timestamp(financials.columns[0]).month)
        except Exception:
            return 12

    # ─────────────────────────────────────────────────────────────────────────

    def _parse_annual_history(
        self,
        company: CompanyData,
        financials: pd.DataFrame,
        balance: pd.DataFrame,
        cashflow: pd.DataFrame,
        fields_filled: list,
        q_financials: Optional[pd.DataFrame] = None,
        q_cashflow: Optional[pd.DataFrame] = None,
        q_balance: Optional[pd.DataFrame] = None,
    ) -> None:
        """
        Parse yfinance DataFrames into AnnualFinancials objects.
        yfinance returns up to 4 years. Columns are datetime objects.
        Values are in raw units — we convert to millions.

        Quarterly cross-validation: for each year, sum the 4 quarterly figures
        for key income-statement rows. If the quarterly sum exceeds the reported
        annual figure by >10% (common for European stocks where yfinance returns
        a partial/stub annual period), replace the annual value with the quarterly
        sum so the data reflects the true full fiscal year.
        """
        if financials is None or financials.empty:
            logger.debug(f"[yfinance] No annual financials available.")
            return

        fiscal_month_end = self._detect_fiscal_month_end(financials)

        for col in financials.columns:
            try:
                year = pd.Timestamp(col).year
            except Exception:
                continue

            af = company.annual_financials.get(year, AnnualFinancials(year=year))

            # Income statement
            af.revenue     = _df_val(financials, "Total Revenue", financials.columns.get_loc(col))
            af.gross_profit= _df_val(financials, "Gross Profit", financials.columns.get_loc(col))
            af.ebit        = _df_val(financials, "Operating Income", financials.columns.get_loc(col))
            af.ebitda      = _df_val(financials, "EBITDA", financials.columns.get_loc(col))
            af.net_income  = _df_val(financials, "Net Income", financials.columns.get_loc(col))
            af.eps_diluted = _df_val(financials, "Diluted EPS",
                                     financials.columns.get_loc(col))

            # EPS is per-share, not in millions — correct the scale
            if af.eps_diluted is not None:
                af.eps_diluted = af.eps_diluted * 1_000_000  # undo /1M scaling

            # ── Quarterly cross-validation for income statement ───────────────
            # yfinance sometimes returns a stub annual period (e.g. 9-month) for
            # non-US stocks. Summing the 4 individual quarters gives the true FY.
            if q_financials is not None and not q_financials.empty:
                _overrides: list[str] = []
                for row_key, attr in [
                    ("Total Revenue",    "revenue"),
                    ("Gross Profit",     "gross_profit"),
                    ("Operating Income", "ebit"),
                    ("EBITDA",           "ebitda"),
                    ("Net Income",       "net_income"),
                ]:
                    annual_val = getattr(af, attr)
                    q_sum = self._sum_quarterly(
                        q_financials, row_key, year, fiscal_month_end
                    )
                    if q_sum is not None and (
                        annual_val is None
                        or (annual_val > 0 and q_sum > annual_val * 1.10)
                        or (annual_val < 0 and q_sum < annual_val * 1.10)
                    ):
                        setattr(af, attr, q_sum)
                        _overrides.append(
                            f"{attr}: {annual_val:.0f}→{q_sum:.0f}"
                            if annual_val is not None else f"{attr}→{q_sum:.0f}"
                        )
                if _overrides:
                    logger.info(
                        f"[yfinance] {year} partial-year fix applied "
                        f"(fiscal_end={fiscal_month_end}): {', '.join(_overrides)}"
                    )

            # Cash flow cross-validation
            if q_cashflow is not None and not q_cashflow.empty:
                for row_key, attr in [
                    ("Operating Cash Flow", "operating_cash_flow"),
                    ("Free Cash Flow",      "fcf"),
                ]:
                    annual_val = getattr(af, attr, None)
                    q_sum = self._sum_quarterly(
                        q_cashflow, row_key, year, fiscal_month_end
                    )
                    if q_sum is not None and (
                        annual_val is None
                        or (annual_val > 0 and q_sum > annual_val * 1.10)
                        or (annual_val < 0 and q_sum < annual_val * 1.10)
                    ):
                        setattr(af, attr, q_sum)

            # Balance sheet
            if balance is not None and not balance.empty and col in balance.columns:
                idx = balance.columns.get_loc(col)
                af.total_assets  = _df_val(balance, "Total Assets", idx)
                af.total_equity  = _df_val(balance, "Stockholders Equity", idx)
                # Try multiple key names for debt
                for debt_key in ["Total Debt", "Long Term Debt And Capital Lease Obligation",
                                 "Long Term Debt"]:
                    val = _df_val(balance, debt_key, idx)
                    if val is not None:
                        af.total_debt = val
                        break
                for cash_key in ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]:
                    val = _df_val(balance, cash_key, idx)
                    if val is not None:
                        af.cash = val
                        break
                # Net Debt fallback: if no standard debt key matched, use
                # Non-Current Liabilities as a proxy for financial debt.
                # net_debt = NCL - Cash  (negative = net cash position).
                if af.total_debt is None and af.cash is not None:
                    for ncl_key in ["Total Non Current Liabilities Net Minority Interest",
                                    "Non Current Liabilities"]:
                        val = _df_val(balance, ncl_key, idx)
                        if val is not None:
                            af.total_debt = val
                            break
                # Store shares in MILLIONS to match EODHD convention. _df_val
                # already divided by 1M, so no further conversion needed. This
                # ensures derived calculations like total_equity / shares (both
                # in millions → result is per-share value in reporting currency)
                # work uniformly across all data sources.
                af.shares_outstanding = _df_val(balance, "Ordinary Shares Number", idx)

            # Cash flow (annual, only if not already overridden by quarterly sum above)
            if cashflow is not None and not cashflow.empty and col in cashflow.columns:
                idx = cashflow.columns.get_loc(col)
                if af.operating_cash_flow is None:
                    af.operating_cash_flow = _df_val(cashflow, "Operating Cash Flow", idx)
                if af.fcf is None:
                    af.fcf = _df_val(cashflow, "Free Cash Flow", idx)
                capex = _df_val(cashflow, "Capital Expenditure", idx)
                if capex is not None:
                    af.capex = abs(capex)  # yfinance reports as negative

            # Derive what we can
            af.calculate_derived()
            company.annual_financials[year] = af

        if company.annual_financials:
            fields_filled.append("annual_financials")
            logger.debug(f"[yfinance] Annual data years: {list(company.annual_financials.keys())}")

    def _fix_net_debt_from_balance_sheet(self, company: CompanyData, q_balance) -> None:
        """
        Re-derive current net_debt and enterprise_value from the most recent
        quarterly balance sheet, using the same cash keys as the annual parser.

        Problem: info["totalCash"] = cash-and-equivalents only. For companies
        holding significant short-term financial investments (e.g. Japanese
        platform stocks), the full liquid position is under "Cash Cash Equivalents
        And Short Term Investments". info["totalCash"] can show 1.6B when the
        true liquid asset base is 6.6B, making EV = MCap + wrong_net_debt.
        """
        if q_balance is None or q_balance.empty:
            return

        # Sort columns so most recent quarter is first
        try:
            cols = sorted(q_balance.columns, reverse=True)
        except TypeError:
            cols = list(q_balance.columns)
        if not cols:
            return
        recent_col = cols[0]

        # Full cash: prefer broad key (includes ST investments), then narrow
        cash_m = None
        for ck in ["Cash Cash Equivalents And Short Term Investments",
                   "Cash And Cash Equivalents"]:
            if ck in q_balance.index:
                v = q_balance.loc[ck, recent_col]
                try:
                    if not pd.isna(v) and float(v) > 0:
                        cash_m = float(v) / 1_000_000
                        break
                except Exception:
                    pass

        if cash_m is None:
            return  # couldn't improve on info["totalCash"]

        # Financial debt: look for standard debt keys
        debt_m = 0.0
        for dk in ["Total Debt",
                   "Long Term Debt And Capital Lease Obligation",
                   "Long Term Debt",
                   "Short Long Term Debt Total"]:
            if dk in q_balance.index:
                v = q_balance.loc[dk, recent_col]
                try:
                    if not pd.isna(v):
                        debt_m = float(v) / 1_000_000
                        break
                except Exception:
                    pass

        new_net_debt = debt_m - cash_m
        logger.debug(
            f"[yfinance] net_debt corrected via q_balance: "
            f"cash={cash_m:.0f}M, debt={debt_m:.0f}M → net_debt={new_net_debt:.0f}M"
        )
        company.net_debt = new_net_debt
        if company.market_cap is not None:
            company.enterprise_value = company.market_cap + new_net_debt

    def _compute_ttm_metrics(
        self,
        company: CompanyData,
        q_financials,
        q_cashflow,
        fields_filled: list,
    ) -> None:
        """
        Compute TTM (trailing twelve months) P&L by summing the last 4 reported
        quarters from yfinance's quarterly_financials DataFrame.

        Stores results in company.ttm_revenue / ttm_ebitda / ttm_ebit /
        ttm_net_income. These are used by the Financial Summary TTM column and
        for computing reliable EV/Sales and EV/EBIT multiples.
        """
        def _ttm_sum(df, *keys):
            """Sum most-recent 4 quarters for the first matching row key.

            Columns in yfinance quarterly DataFrames are timestamps; we sort
            descending so [:4] always picks the 4 most-recent periods even if
            the DataFrame arrives in a different order for some exchanges.
            If fewer than 4 non-NaN quarters are available we annualise the
            available data (multiply by 4/count) so at least 1 quarter yields
            a reasonable estimate rather than returning None.
            """
            if df is None or df.empty:
                return None
            # Sort columns descending (most recent quarter first)
            try:
                sorted_cols = sorted(df.columns, reverse=True)
            except TypeError:
                sorted_cols = list(df.columns)
            recent_cols = sorted_cols[:4]
            for key in keys:
                if key in df.index:
                    total, count = 0.0, 0
                    for col in recent_cols:
                        try:
                            v = df.loc[key, col]
                            if not pd.isna(v):
                                total += float(v) / 1_000_000  # raw → millions
                                count += 1
                        except Exception:
                            pass
                    if count > 0:
                        # Annualise when fewer than 4 quarters available
                        return total * (4 / count) if count < 4 else total
            return None

        if q_financials is not None and not q_financials.empty:
            # Only fill fields not already populated by income_stmt TTM column
            if getattr(company, 'ttm_revenue', None) is None:
                company.ttm_revenue = _ttm_sum(
                    q_financials, "Total Revenue", "Operating Revenue")
            if getattr(company, 'ttm_ebit', None) is None:
                company.ttm_ebit = _ttm_sum(
                    q_financials, "Operating Income", "Total Operating Income As Reported")
            company.ttm_ebitda = _ttm_sum(
                q_financials, "EBITDA", "Normalized EBITDA",
                "EBITDA including unusual items")
            # Fallback: derive EBITDA from EBIT + D&A (quarterly D&A from cashflow)
            if getattr(company, 'ttm_ebitda', None) is None:
                _ebit_ttm = _ttm_sum(
                    q_financials, "Operating Income",
                    "Total Operating Income As Reported")
                _da_ttm = _ttm_sum(
                    q_cashflow,
                    "Depreciation & Amortization",
                    "Depreciation And Amortization",
                    "Reconciled Depreciation",
                    "Depreciation")
                if _ebit_ttm is not None and _da_ttm is not None:
                    company.ttm_ebitda = _ebit_ttm + _da_ttm
            if getattr(company, 'ttm_net_income', None) is None:
                company.ttm_net_income = _ttm_sum(
                q_financials, "Net Income", "Net Income Common Stockholders",
                "Net Income Including Noncontrolling Interests")

            if any(v is not None for v in [
                company.ttm_revenue, company.ttm_net_income
            ]):
                fields_filled.append("ttm_financials")
                logger.debug(
                    f"[yfinance] TTM: Rev={company.ttm_revenue}, "
                    f"EBIT={company.ttm_ebit}, NI={company.ttm_net_income}"
                )

    def _parse_forward_estimates(
        self,
        company: CompanyData,
        yt,           # yf.Ticker instance
        info: dict,
    ) -> None:
        """
        Fetch analyst consensus estimates and populate company.forward_estimates.

        Strategy:
        - Use yfinance's earnings_estimate / revenue_estimate DataFrames.
        - Row '0y' = current fiscal year in progress.
        - Row '+1y' = next full fiscal year.
        - We pick the row that best matches show_years[-1]+1 (the est column).
        - Fallback: use info['forwardEps'] / info['forwardPE'] if DataFrames empty.
        """
        # ── Determine target estimate year ────────────────────────────────────
        sorted_years = company.sorted_years()
        if not sorted_years:
            return
        est_year = sorted_years[0] + 1   # e.g. 2024 → 2025E, 2025 → 2026E

        # ── Fetch DataFrames ──────────────────────────────────────────────────
        try:
            ee = yt.earnings_estimate   # per-share EPS estimates
        except Exception:
            ee = None
        try:
            re_ = yt.revenue_estimate   # revenue estimates (raw units)
        except Exception:
            re_ = None

        # ── Helper: pick best row (0y or +1y) closest to est_year ─────────────
        def _pick_row(df, candidates=("0y", "+1y")):
            """Return the first non-empty row from the candidates list."""
            if df is None or df.empty:
                return None, None
            for period in candidates:
                if period in df.index:
                    row = df.loc[period]
                    avg = _safe(row.get("avg"), float)
                    if avg is not None and avg > 0:
                        return row, period
            return None, None

        eps_row, eps_period = _pick_row(ee)
        rev_row, rev_period = _pick_row(re_)

        # ── Build ForwardEstimates ────────────────────────────────────────────
        fe = ForwardEstimates(year=est_year)

        # Revenue (raw units → millions)
        if rev_row is not None:
            rev_avg = _safe(rev_row.get("avg"), float)
            if rev_avg and rev_avg > 0:
                fe.revenue = rev_avg / 1_000_000
            fe.revenue_growth_yoy = _safe(rev_row.get("growth"), float)
            fe.analyst_count = _safe(rev_row.get("numberOfAnalysts"), int)

        # EPS
        if eps_row is not None:
            fe.eps_diluted = _safe(eps_row.get("avg"), float)
            fe.eps_growth_yoy = _safe(eps_row.get("growth"), float)
            if fe.analyst_count is None:
                fe.analyst_count = _safe(eps_row.get("numberOfAnalysts"), int)

        # Fallback to info fields if DataFrames were empty
        if fe.eps_diluted is None:
            fe.eps_diluted = _safe(info.get("forwardEps"), float)

        # Net income from EPS × shares (shares in millions → NI in millions)
        shares_m = company.shares_outstanding
        if fe.eps_diluted is not None and shares_m and shares_m > 0:
            fe.net_income = fe.eps_diluted * shares_m

        # Net margin (derived)
        if fe.net_income is not None and fe.revenue and fe.revenue > 0:
            fe.net_margin = fe.net_income / fe.revenue

        # Forward P/E: current price / forward EPS
        if fe.eps_diluted is not None and company.current_price:
            if fe.eps_diluted > 0:
                # Handle cross-currency: current_price is in price currency
                # forward_pe from info is already computed correctly by Yahoo
                fpe = _safe(info.get("forwardPE"), float)
                if fpe and fpe > 0:
                    fe.pe_ratio = fpe
                else:
                    fe.pe_ratio = company.current_price / fe.eps_diluted

        # EV/Sales (current EV / forward revenue)
        if company.enterprise_value and fe.revenue and fe.revenue > 0:
            fe.ev_sales = company.enterprise_value / fe.revenue

        # Only store if we got at least some useful data
        if fe.revenue is not None or fe.eps_diluted is not None:
            company.forward_estimates = fe
            logger.debug(
                f"[yfinance] Forward estimates for {est_year}: "
                f"Rev={fe.revenue:.0f}M, EPS={fe.eps_diluted}, "
                f"Analysts={fe.analyst_count}"
                if fe.revenue else f"[yfinance] Forward EPS only: {fe.eps_diluted}"
            )


# ── Quick sanity test (run this file directly) ────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    adapter = YFinanceAdapter()

    test_tickers = [
        ("AAPL",     "US — Apple"),
        ("WKL.AS",   "EU — Wolters Kluwer (Amsterdam)"),
        ("NOKIA.HE", "EU — Nokia (Helsinki)"),
        ("ATCO-A.ST","EU — Atlas Copco (Stockholm)"),
        ("7203.T",   "Asia — Toyota (Tokyo)"),
    ]

    for ticker, label in test_tickers:
        print(f"\n{'='*60}")
        print(f"  {label}")
        result = adapter.fetch(ticker)
        if result.success:
            print(f"  {result.data.summary()}")
            la = result.data.latest_annual()
            if la:
                print(f"  Latest annual ({la.year}): "
                      f"Revenue={la.revenue:.0f}M, EBIT={la.ebit}M, "
                      f"Net Income={la.net_income}M")
        else:
            print(f"  FAILED: {result.error}")
