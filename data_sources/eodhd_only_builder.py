"""
eodhd_only_builder.py — Build CompanyData purely from EODHD All-In-One data.

Used by the "Investment Memo V2 (EODHD Based)" framework to guarantee that
every populated field originated from an EODHD endpoint — no yfinance,
Stooq, Alpha Vantage, EDGAR or FMP data is ever merged in.

A field-provenance map is also returned so the PDF generator can stamp
each cell with ✓ (verified EODHD) or — (not provided by EODHD).
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from .base import CompanyData, AnnualFinancials, ForwardEstimates
from .eodhd_all_in_one import EODHDAllInOneFetcher, _convert_ticker

logger = logging.getLogger(__name__)


def _f(v) -> Optional[float]:
    if v is None or v == "" or v == "NA":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _to_m(v) -> Optional[float]:
    """Raw unit → millions."""
    f = _f(v)
    return f / 1_000_000 if f is not None else None


def _fz(v) -> Optional[float]:
    """Like _f() but treats an exact 0 as missing.

    EODHD returns the literal string "0" for valuation multiples it can't
    compute (e.g. P/E or EV/EBITDA for a loss-making company) instead of
    null. A real P/E or EV/EBITDA of exactly 0.0 is never meaningful, so
    _f() silently converting "0" to 0.0 renders as a misleading "0.0x" in
    reports instead of "n/a". Only use this for ratio fields where 0.0 is
    never a legitimate value.
    """
    f = _f(v)
    return None if f == 0 else f


def _year_from_date(s) -> Optional[int]:
    if not s:
        return None
    try:
        return int(str(s)[:4])
    except (ValueError, TypeError):
        return None


def fetch_company_data_eodhd_only(yf_ticker: str
                                  ) -> tuple[CompanyData, dict]:
    """
    Fetch the full EODHD bundle for `yf_ticker` and project it into a
    CompanyData object.

    Returns:
        (company, bundle)
            company  — populated CompanyData (only EODHD-sourced fields set)
            bundle   — the raw bundle dict (so the PDF can also show news,
                       sentiment, analyst rating changes, etc.)
    """
    fetcher = EODHDAllInOneFetcher()
    bundle = fetcher.fetch_all(yf_ticker)
    company = build_company_data_from_bundle(yf_ticker, bundle, fetcher=fetcher)
    return company, bundle


def fetch_peers_eodhd_only(yf_tickers: list[str]
                            ) -> dict[str, CompanyData]:
    """
    Fetch EODHD-only CompanyData for a list of peer tickers.

    Drops peers whose fetch returns no usable data (no name AND no
    market_cap AND no revenue). Logs warnings rather than raising —
    one bad peer should not break the whole report.

    Returns:
        dict mapping the original yf_ticker → CompanyData
    """
    out: dict[str, CompanyData] = {}
    for tk in yf_tickers:
        if not tk:
            continue
        try:
            pd_, _ = fetch_company_data_eodhd_only(tk)
        except Exception as e:
            logger.warning(f"[eodhd-only] Peer {tk} fetch failed: {e}")
            continue
        la = pd_.latest_annual()
        has_rev = bool(la and la.revenue)
        if pd_.name and (pd_.market_cap or has_rev):
            out[tk] = pd_
        else:
            logger.info(f"[eodhd-only] Peer {tk} returned no usable EODHD data — skipped")
    return out


def build_company_data_from_bundle(yf_ticker: str, bundle: dict,
                                    fetcher: Optional["EODHDAllInOneFetcher"] = None
                                    ) -> CompanyData:
    """
    Project an EODHD bundle dict into a CompanyData object. Every field
    written to the company is also tracked in `company.eodhd_fields` so the
    PDF can render the green ✓ next to EODHD-sourced cells.

    `fetcher` is optional — when a caller already has an EODHDAllInOneFetcher
    instance (e.g. fetch_company_data_eodhd_only), pass it through so the
    dual-currency FX lookup (see below) reuses it instead of constructing a
    fresh one. A fresh one is created lazily if omitted and actually needed.
    """
    company = CompanyData(
        ticker=yf_ticker,
        input_ticker=yf_ticker,
        fetch_timestamp=datetime.utcnow().isoformat(),
        as_of_date=datetime.utcnow().strftime("%Y-%m-%d"),
        data_sources=["eodhd"],
    )

    fund = bundle.get("fundamentals") or {}
    if not fund:
        logger.warning(f"[eodhd-only] No fundamentals for {yf_ticker}")
        return company

    g  = fund.get("General")        or {}
    h  = fund.get("Highlights")     or {}
    v  = fund.get("Valuation")      or {}
    ss = fund.get("SharesStats")    or {}
    tech = fund.get("Technicals")   or {}
    sd = fund.get("SplitsDividends") or {}
    rt = bundle.get("realtime")     or {}

    # ── Identity ─────────────────────────────────────────────────────────────
    company.name        = g.get("Name") or None
    company.exchange    = g.get("Exchange") or None
    company.currency    = g.get("CurrencyCode") or None
    company.currency_price = g.get("CurrencyCode") or None
    company.sector      = g.get("Sector") or None
    company.industry    = g.get("Industry") or None
    company.country     = g.get("CountryName") or g.get("Country") or None
    company.isin        = g.get("ISIN") or None
    company.description = g.get("Description") or None
    company.website     = g.get("WebURL") or None
    company.ipo_date    = g.get("IPODate") or None
    company.fiscal_year_end = g.get("FiscalYearEnd") or None
    company.address     = g.get("Address") or None
    company.phone       = g.get("Phone") or None
    company.cik         = g.get("CIK") or None

    emp = g.get("FullTimeEmployees")
    if emp not in (None, "", "NA"):
        try: company.employees = int(str(emp).replace(",", ""))
        except (ValueError, TypeError): pass

    # Officers
    officers_raw = g.get("Officers") or {}
    if isinstance(officers_raw, dict):
        for _k, o in list(officers_raw.items())[:10]:
            if isinstance(o, dict) and o.get("Name"):
                company.officers.append({"name": o.get("Name"),
                                          "title": o.get("Title") or ""})

    # ── GBX detection: LSE stocks quote prices in pence (GBX), not pounds (GBP).
    # Financial statements are in GBP, so all price-derived ratios need /100.
    _gbx_exchange = (company.exchange or "").upper() in {"LSE"}
    _gbx_factor = 100.0 if _gbx_exchange else 1.0

    # ── Current market (price from real-time if available) ───────────────────
    price = rt.get("close") or rt.get("previousClose")
    raw_price = _f(price)
    company.current_price = (raw_price / _gbx_factor) if raw_price is not None else None
    if _gbx_exchange:
        company.currency_price = "GBP"  # we've converted; label consistently

    # Market cap from Highlights MarketCapitalizationMln (already in millions)
    mc_mln = _f(h.get("MarketCapitalizationMln"))
    if mc_mln is not None:
        company.market_cap = mc_mln
    else:
        company.market_cap = _to_m(h.get("MarketCapitalization"))

    # Shares outstanding (full units → millions)
    shares_raw = _f(ss.get("SharesOutstanding"))
    if shares_raw is not None:
        company.shares_outstanding = shares_raw / 1_000_000

    # Enterprise value. EODHD returns the literal string "0" for tickers it
    # can't compute EV for (e.g. some foreign ADRs) instead of null — _fz()
    # on the raw value (before the /1e6 conversion) treats that as missing so
    # a real fallback (market_cap + net_debt, below) can fill it instead of
    # silently locking in a misleading 0.0 that then propagates into
    # EV/Sales, EV/Gross Profit, etc. as "0.0x" rather than "n/a".
    _ev_raw = _fz(v.get("EnterpriseValue"))
    company.enterprise_value = _ev_raw / 1_000_000 if _ev_raw is not None else None

    # Float, % insider/inst
    company.shares_float = _to_m(ss.get("SharesFloat"))
    company.pct_insiders = _f(ss.get("PercentInsiders"))
    company.pct_institutions = _f(ss.get("PercentInstitutions"))

    # Note: short interest fields (SharesShort, ShortRatio, ShortPercent) are read
    # from the Technicals block below — SharesStats.SharesShort is null for most tickers.

    # ── Valuation multiples ──────────────────────────────────────────────────
    company.pe_ratio       = _fz(h.get("PERatio")) or _fz(v.get("TrailingPE"))
    company.forward_pe     = _fz(v.get("ForwardPE"))
    company.price_to_book  = _fz(v.get("PriceBookMRQ"))
    company.price_to_sales = _fz(v.get("PriceSalesTTM"))
    company.peg_ratio      = _fz(h.get("PEGRatio"))
    company.ev_sales       = _fz(v.get("EnterpriseValueRevenue"))
    company.ev_ebitda      = _fz(v.get("EnterpriseValueEbitda"))

    # ── Profitability TTM ────────────────────────────────────────────────────
    company.net_margin    = _f(h.get("ProfitMargin"))
    company.ebit_margin   = _f(h.get("OperatingMarginTTM"))
    company.roe           = _f(h.get("ReturnOnEquityTTM"))
    company.roa           = _f(h.get("ReturnOnAssetsTTM"))

    # ── TTM absolute P&L figures ─────────────────────────────────────────────
    # RevenueTTM is in full units (not millions) — convert with _to_m().
    # EBITDA from Highlights is also full units.
    company.ttm_revenue = _to_m(h.get("RevenueTTM"))
    company.ttm_ebitda  = _to_m(h.get("EBITDA"))
    # Next earnings date: try Highlights first, fall back to Earnings.History
    _ned = h.get("NextEarningsDate")
    if _ned and str(_ned).strip() not in ("", "0000-00-00", "None"):
        company.next_earnings_date = str(_ned).strip()
    else:
        # Scan Earnings.History for the first future date where epsActual is null
        from datetime import date as _date_cls
        _today_str = str(_date_cls.today())
        _earn_hist = (fund.get("Earnings") or {}).get("History") or {}
        if isinstance(_earn_hist, dict):
            _future = sorted(
                [k for k, v in _earn_hist.items()
                 if isinstance(v, dict)
                 and v.get("epsActual") is None
                 and k > _today_str],
            )
            if _future:
                company.next_earnings_date = _future[0]
    # EBIT TTM: derive from EBITDA margin × revenue when direct field absent
    _ebit_margin_ttm = _f(h.get("OperatingMarginTTM"))
    if company.ttm_revenue and _ebit_margin_ttm is not None:
        company.ttm_ebit = company.ttm_revenue * _ebit_margin_ttm

    # ── TTM date from quarterly income statement ─────────────────────────────
    _q_inc = ((fund.get("Financials") or {})
              .get("Income_Statement", {})
              .get("quarterly") or
              (fund.get("Financials") or {})
              .get("Income_Statement", {})
              .get("quarter") or {})
    if _q_inc:
        _sorted_q = sorted(_q_inc.keys(), reverse=True)
        if _sorted_q:
            _latest_q = _sorted_q[0]
            company.ttm_last_quarter_date = (
                _latest_q[:10] if len(_latest_q) >= 10 else _latest_q
            )
        # Fallback: sum last 4 quarters when Highlights TTM is missing
        if company.ttm_revenue is None:
            _rev_sum, _rev_n = 0.0, 0
            for _qd in _sorted_q[:4]:
                _rv = _to_m(_q_inc[_qd].get("totalRevenue") or _q_inc[_qd].get("revenue"))
                if _rv is not None:
                    _rev_sum += _rv; _rev_n += 1
            if _rev_n:
                company.ttm_revenue = _rev_sum * 4 / _rev_n
        if company.ttm_ebitda is None:
            _ebi_sum, _ebi_n = 0.0, 0
            for _qd in _sorted_q[:4]:
                _row = _q_inc[_qd]
                _eb = _to_m(_row.get("ebitda") or _row.get("EBITDA"))
                if _eb is None:
                    _ebit = _to_m(_row.get("ebit") or _row.get("operatingIncome"))
                    _da   = _to_m(_row.get("depreciationAndAmortization"))
                    if _ebit is not None and _da is not None:
                        _eb = _ebit + _da
                if _eb is not None:
                    _ebi_sum += _eb; _ebi_n += 1
            if _ebi_n:
                company.ttm_ebitda = _ebi_sum * 4 / _ebi_n

    # ── TTM Net Income ───────────────────────────────────────────────────────
    # Primary: sum last 4 quarterly net income rows directly from EODHD
    # quarterly income statement — reported figures, no derivation needed.
    # Fallback: ProfitMargin × RevenueTTM only when quarterly data is absent.
    if _q_inc:
        _ni_sum, _ni_n = 0.0, 0
        for _qd in _sorted_q[:4]:
            _row = _q_inc[_qd]
            _ni = (_to_m(_row.get("netIncomeApplicableToCommonShares"))
                   or _to_m(_row.get("netIncome")))
            if _ni is not None:
                _ni_sum += _ni; _ni_n += 1
        if _ni_n:
            company.ttm_net_income = _ni_sum * 4 / _ni_n
    if company.ttm_net_income is None and company.ttm_revenue and company.net_margin is not None:
        company.ttm_net_income = company.ttm_revenue * company.net_margin

    # ── TTM FCF from quarterly cash flow ────────────────────────────────────
    _q_cf = ((fund.get("Financials") or {})
             .get("Cash_Flow", {})
             .get("quarterly") or
             (fund.get("Financials") or {})
             .get("Cash_Flow", {})
             .get("quarter") or {})
    if _q_cf:
        _sorted_qcf = sorted(_q_cf.keys(), reverse=True)
        _fcf_sum, _fcf_n = 0.0, 0
        for _qd in _sorted_qcf[:4]:
            _row = _q_cf[_qd]
            _fcf_q = _to_m(_row.get("freeCashFlow"))
            if _fcf_q is None:
                _ocf = _to_m(_row.get("totalCashFromOperatingActivities"))
                _cap = _to_m(_row.get("capitalExpenditures"))
                if _ocf is not None and _cap is not None:
                    _fcf_q = _ocf - abs(_cap)
            if _fcf_q is not None:
                _fcf_sum += _fcf_q; _fcf_n += 1
        if _fcf_n:
            company.ttm_fcf = _fcf_sum * 4 / _fcf_n

    company.book_value_per_share = _f(h.get("BookValue"))
    company.revenue_per_share    = _f(h.get("RevenuePerShareTTM"))
    company.eps_ttm              = _f(h.get("EarningsShare")) or _f(h.get("DilutedEpsTTM"))
    company.quarterly_revenue_growth_yoy  = _f(h.get("QuarterlyRevenueGrowthYOY"))
    company.quarterly_earnings_growth_yoy = _f(h.get("QuarterlyEarningsGrowthYOY"))

    # ── Technicals ───────────────────────────────────────────────────────────
    company.beta         = _f(tech.get("Beta"))
    company.week_52_high = _f(tech.get("52WeekHigh"))
    company.week_52_low  = _f(tech.get("52WeekLow"))
    company.ma_50        = _f(tech.get("50DayMA"))
    company.ma_200       = _f(tech.get("200DayMA"))
    # Short interest lives in Technicals, NOT SharesStats (SharesStats.SharesShort is null)
    _t_short = _f(tech.get("SharesShort"))
    if _t_short is not None:
        company.shares_short = _t_short / 1_000_000           # full units → millions
    _t_short_pm = _f(tech.get("SharesShortPriorMonth"))
    if _t_short_pm is not None:
        company.shares_short_prior_month = _t_short_pm / 1_000_000
    company.short_ratio = _f(tech.get("ShortRatio"))
    # Technicals.ShortPercent is a decimal (0.0019 = 0.19%) — store as-is for pct_d renderer
    _t_spct = _f(tech.get("ShortPercent"))
    if _t_spct is not None:
        company.short_percent_of_float = _t_spct

    # ── Dividends ────────────────────────────────────────────────────────────
    company.dividend_yield = _f(h.get("DividendYield"))
    _fdr = _f(sd.get("ForwardAnnualDividendRate"))
    company.forward_annual_dividend_rate  = (_fdr / _gbx_factor) if _fdr is not None else None
    company.forward_annual_dividend_yield = _f(sd.get("ForwardAnnualDividendYield"))
    company.payout_ratio    = _f(sd.get("PayoutRatio"))
    company.dividend_date   = sd.get("DividendDate") or None
    company.ex_dividend_date= sd.get("ExDividendDate") or None
    company.last_split_factor = sd.get("LastSplitFactor") or None
    company.last_split_date   = sd.get("LastSplitDate") or None

    # ── Forward estimates ────────────────────────────────────────────────────
    company.eps_estimate_next_year  = (
        _f(h.get("EPSEstimateNextYear")) or _f(h.get("EPSEstimateCurrentYear"))
    )

    # Build ForwardEstimates from Earnings.Trend
    earnings = fund.get("Earnings") or {}
    trend = earnings.get("Trend") or {}
    if isinstance(trend, dict):
        # We need to know the latest historical year BEFORE picking the
        # forecast — otherwise EODHD's `period: "0y"` entries (which can
        # represent a fiscal year that has already been reported) leak into
        # the table as a duplicate estimate column.
        latest_hist_year_for_fe = None
        inc_dict = (fund.get("Financials") or {}).get("Income_Statement", {}) \
                     .get("yearly") or {}
        if isinstance(inc_dict, dict) and inc_dict:
            inc_years = [
                _year_from_date(k) for k in inc_dict.keys()
                if _year_from_date(k)
            ]
            if inc_years:
                latest_hist_year_for_fe = max(inc_years)

        candidates = []
        for date_str, entry in trend.items():
            if not isinstance(entry, dict): continue
            yr = _year_from_date(date_str)
            period = (entry.get("period") or "").strip()
            if not (yr and period and period.endswith("y")):
                continue
            # Only fiscal years strictly newer than the latest reported year.
            if latest_hist_year_for_fe is not None and yr <= latest_hist_year_for_fe:
                continue
            candidates.append((yr, entry))
        candidates.sort(key=lambda x: x[0])
        if candidates:
            target_year, entry = candidates[0]
            fe = ForwardEstimates(year=target_year, source="eodhd")
            fe.revenue     = _to_m(entry.get("revenueEstimateAvg"))  # full units → millions
            fe.eps_diluted = _f(entry.get("earningsEstimateAvg"))
            fe.revenue_growth_yoy = _f(entry.get("revenueEstimateGrowth"))
            fe.eps_growth_yoy     = _f(entry.get("earningsEstimateGrowth"))
            rev_n = _f(entry.get("revenueEstimateNumberOfAnalysts"))
            eps_n = _f(entry.get("earningsEstimateNumberOfAnalysts"))
            counts = [c for c in [rev_n, eps_n] if c is not None]
            if counts: fe.analyst_count = int(max(counts))
            company.forward_estimates = fe

    # ── Annual history: Income Statement / Balance Sheet / Cash Flow ─────────
    fin = fund.get("Financials") or {}
    inc_a = (fin.get("Income_Statement") or {}).get("yearly") or {}
    bs_a  = (fin.get("Balance_Sheet")    or {}).get("yearly") or {}
    cf_a  = (fin.get("Cash_Flow")        or {}).get("yearly") or {}

    # ── Dual-currency ADR detection ───────────────────────────────────────────
    # Some foreign ADRs (e.g. KSPI — Kaspi.kz, Nasdaq-listed) quote price,
    # market cap and EPS in the trading currency (General.CurrencyCode, USD
    # for a US listing) while EODHD's Financials.*.currency_symbol shows the
    # underlying statements (revenue, gross profit, net debt, etc.) are
    # reported in the company's home currency (KZT for KSPI). Dividing a
    # trading-currency EV by a reporting-currency Sales/Gross Profit figure
    # produces a meaningless ratio (observed as "0.0x" or wildly-wrong
    # historical Enterprise Value). See CLAUDE.md for the KSPI case history.
    _stmt_ccy = (
        (fin.get("Income_Statement") or {}).get("currency_symbol")
        or (fin.get("Balance_Sheet") or {}).get("currency_symbol")
        or (fin.get("Cash_Flow") or {}).get("currency_symbol")
    ) or None
    _trading_ccy = company.currency_price
    _dual_currency = bool(_stmt_ccy and _trading_ccy
                          and str(_stmt_ccy).upper() != str(_trading_ccy).upper())
    _fx_rate: Optional[float] = None
    if _dual_currency:
        company.currency_financials = _stmt_ccy
        try:
            _fetcher_for_fx = fetcher or EODHDAllInOneFetcher()
            _fx_rate = _fetcher_for_fx.fetch_forex_rate(_trading_ccy, _stmt_ccy)
        except Exception as e:
            logger.warning(f"[eodhd-only] {yf_ticker}: forex fetch failed: {e}")
            _fx_rate = None
        company.fx_rate_price_to_financials = _fx_rate
        if _fx_rate:
            logger.info(f"[eodhd-only] {yf_ticker}: dual-currency ADR detected "
                        f"(price/mcap in {_trading_ccy}, statements in {_stmt_ccy}); "
                        f"FX rate {_trading_ccy}->{_stmt_ccy} = {_fx_rate}")
        else:
            logger.warning(f"[eodhd-only] {yf_ticker}: dual-currency ADR detected "
                           f"({_trading_ccy} vs {_stmt_ccy}) but FX rate unavailable — "
                           f"price-derived historical ratios (Market Cap, EV, P/E, "
                           f"FCF Yield) will be left as n/a rather than currency-mismatched")
    else:
        company.currency_financials = _trading_ccy

    bs_by_year = {_year_from_date(k): v for k, v in bs_a.items()
                  if _year_from_date(k)}
    cf_by_year = {_year_from_date(k): v for k, v in cf_a.items()
                  if _year_from_date(k)}

    # ── Pre-parse Earnings.Annual into a year→epsActual lookup ───────────────
    # EODHD's Earnings.Annual stores split-adjusted EPS (epsActual). This is
    # more reliable than Income_Statement.eps which reflects the as-reported
    # (pre-split) figures. We build the lookup here so the income-statement
    # loop can use it as the PRIMARY EPS source rather than a post-hoc override.
    earnings = fund.get("Earnings") or {}
    _annual_eps_raw = earnings.get("Annual") or {}
    # Determine fiscal-year-end months so we can skip quarterly stub entries
    # that EODHD occasionally mixes into the Annual block.
    _fy_months: set[int] = set()
    for ds in inc_a.keys():
        if isinstance(ds, str) and len(ds) >= 7:
            try: _fy_months.add(int(ds[5:7]))
            except (ValueError, TypeError): pass
    eps_by_year: dict[int, float] = {}
    if isinstance(_annual_eps_raw, dict):
        for date_str, entry in _annual_eps_raw.items():
            if not isinstance(entry, dict): continue
            yr = _year_from_date(date_str)
            if not yr: continue
            if _fy_months and isinstance(date_str, str) and len(date_str) >= 7:
                try:
                    if int(date_str[5:7]) not in _fy_months:
                        continue
                except (ValueError, TypeError):
                    pass
            eps_val = _f(entry.get("epsActual"))
            if eps_val is not None:
                eps_by_year[yr] = eps_val

    for date_str, inc in inc_a.items():
        yr = _year_from_date(date_str)
        if not yr or not isinstance(inc, dict):
            continue
        af = AnnualFinancials(year=yr)
        af.source = "eodhd"

        # Income Statement
        af.revenue       = _to_m(inc.get("totalRevenue"))
        af.gross_profit  = _to_m(inc.get("grossProfit"))
        af.ebit          = _to_m(inc.get("ebit"))
        af.ebitda        = _to_m(inc.get("ebitda"))
        # Prefer netIncomeApplicableToCommonShares — it nets out the
        # minority-interest share so it matches the EPS denominator.
        # In years like RHM 2020 the consolidated `netIncome` is +€1M
        # but the parent-attributable income is -€26M, which is what EPS
        # reflects. Using the common-shares figure keeps the row internally
        # consistent (NI sign matches EPS sign).
        af.net_income    = (_to_m(inc.get("netIncomeApplicableToCommonShares"))
                            or _to_m(inc.get("netIncome")))
        # EPS: prefer Earnings.Annual.epsActual (split-adjusted) over the
        # income statement figure (which reflects as-reported pre-split values).
        af.eps_diluted = (eps_by_year.get(yr)
                          or _f(inc.get("eps") or inc.get("epsDiluted")))
        af.cost_of_revenue = _to_m(inc.get("costOfRevenue"))
        af.depreciation_amortization = _to_m(
            inc.get("depreciationAndAmortization") or inc.get("reconciledDepreciation")
        )
        af.interest_expense  = _to_m(inc.get("interestExpense"))
        af.interest_income   = _to_m(inc.get("interestIncome"))
        af.income_before_tax = _to_m(inc.get("incomeBeforeTax"))
        af.tax_provision     = _to_m(inc.get("incomeTaxExpense") or inc.get("taxProvision"))
        af.minority_interest = _to_m(inc.get("minorityInterest"))
        af.net_income_continuing_ops = _to_m(inc.get("netIncomeFromContinuingOps"))
        af.sga               = _to_m(inc.get("sellingGeneralAdministrative"))
        af.research_development = _to_m(inc.get("researchDevelopment"))
        af.total_operating_expenses = _to_m(inc.get("totalOperatingExpenses"))
        af.extraordinary_items = _to_m(inc.get("extraordinaryItems"))

        # Balance Sheet
        b = bs_by_year.get(yr) or {}
        if b:
            af.total_assets = _to_m(b.get("totalAssets"))
            af.total_equity = _to_m(b.get("totalStockholderEquity")
                                    or b.get("totalEquity"))
            nd_direct = _f(b.get("netDebt"))
            if nd_direct is not None:
                af.net_debt = _to_m(b.get("netDebt"))
            td_val = (_to_m(b.get("shortLongTermDebtTotal"))
                      or _to_m(b.get("shortLongTermDebt"))
                      or _to_m(b.get("longTermDebt")))
            if td_val is not None:
                af.total_debt = td_val
            af.cash = _to_m(b.get("cashAndEquivalents") or b.get("cash"))
            af.goodwill = _to_m(b.get("goodWill"))
            af.intangible_assets = _to_m(b.get("intangibleAssets"))
            af.inventory = _to_m(b.get("inventory"))
            af.net_receivables = _to_m(b.get("netReceivables"))
            af.accounts_payable = _to_m(b.get("accountsPayable"))
            af.ppe_net = _to_m(b.get("propertyPlantAndEquipmentNet")
                               or b.get("propertyPlantEquipment"))
            af.retained_earnings = _to_m(b.get("retainedEarnings"))
            af.capital_lease_obligations = _to_m(b.get("capitalLeaseObligations"))
            af.net_working_capital = _to_m(b.get("netWorkingCapital"))
            af.current_assets = _to_m(b.get("totalCurrentAssets"))
            af.current_liabilities = _to_m(b.get("totalCurrentLiabilities"))

        # Cash Flow
        cf = cf_by_year.get(yr) or {}
        if cf:
            af.operating_cash_flow = _to_m(cf.get("totalCashFromOperatingActivities"))
            fcf_v = _to_m(cf.get("freeCashFlow"))
            if fcf_v is not None:
                af.fcf = fcf_v
            capex_v = _to_m(cf.get("capitalExpenditures"))
            if capex_v is not None:
                af.capex = abs(capex_v)
            div_paid = _to_m(cf.get("dividendsPaid"))
            if div_paid is not None:
                af.dividends_paid = abs(div_paid)
            af.change_in_working_capital = _to_m(cf.get("changeInWorkingCapital"))
            af.investing_cash_flow = _to_m(cf.get("totalCashflowsFromInvestingActivities"))
            af.net_borrowings = _to_m(cf.get("netBorrowings"))

        af.calculate_derived()
        company.annual_financials[yr] = af

    # ── Per-year shares outstanding from outstandingShares.annual ────────────
    # Only update years that ALREADY have income-statement data. Otherwise
    # EODHD's forward 2026 entry creates a half-populated row that pollutes
    # the 10-year table (no revenue / NI / EPS but a shares figure).
    shares_block = (fund.get("outstandingShares") or {}).get("annual") or {}
    if isinstance(shares_block, dict):
        for _k, row in shares_block.items():
            if not isinstance(row, dict): continue
            yr = _year_from_date(row.get("dateFormatted") or row.get("date"))
            if not yr: continue
            shares = _f(row.get("shares"))
            if shares is None:
                shares = _f(row.get("sharesMln"))
                shares_in_m = shares if shares is not None else None
            else:
                shares_in_m = shares / 1_000_000
            if shares_in_m is None: continue
            af = company.annual_financials.get(yr)
            if af is None:
                # Skip years that don't have an income-statement row.
                continue
            af.shares_outstanding = shares_in_m

    # ── Backfill missing per-year shares from nearest known year ─────────────
    # Some exchanges (e.g. Mexico .MX) only expose the most-recent entry in
    # outstandingShares.annual, leaving all historical years at None → market
    # cap, EV, FCF yield and EV multiples all become n/a in the table.
    # Backfill from the closest known year (forward-fill for older years,
    # backward-fill for newer years). This is an approximation for companies
    # that have changed their share count, but always beats blank cells.
    _known_shares: dict[int, float] = {
        yr: af.shares_outstanding
        for yr, af in company.annual_financials.items()
        if af.shares_outstanding is not None
    }
    if not _known_shares and company.shares_outstanding is not None:
        # No annual data at all — use current scalar for every year.
        for af in company.annual_financials.values():
            if af.shares_outstanding is None:
                af.shares_outstanding = company.shares_outstanding
    elif _known_shares:
        _syk = sorted(_known_shares.keys())
        for yr, af in company.annual_financials.items():
            if af.shares_outstanding is not None:
                continue
            before = [y for y in _syk if y <= yr]
            after  = [y for y in _syk if y > yr]
            if before:
                af.shares_outstanding = _known_shares[before[-1]]
            elif after:
                af.shares_outstanding = _known_shares[after[0]]

    # (EPS from Earnings.Annual is now applied inline during income-statement
    # parsing above via eps_by_year lookup — no post-hoc override needed.)

    # ── Per-year DPS from /div endpoint (full dividend history) ──────────────
    # Sum every dividend record into the matching fiscal year so the
    # historical Div Yield row populates for every year EODHD covers.
    divs = bundle.get("dividends") or []
    if isinstance(divs, list) and divs:
        dps_by_year: dict[int, float] = {}
        for d in divs:
            if not isinstance(d, dict): continue
            dt = d.get("date") or d.get("paymentDate")
            val = _f(d.get("value"))
            yr = _year_from_date(dt)
            if yr is not None and val is not None:
                dps_by_year[yr] = dps_by_year.get(yr, 0.0) + val
        # German "spring payer" detection: dividends often paid in months
        # 4-6 of the following fiscal year. If >=70% of dividends fall in
        # months 4-6, shift each year's total back by one fiscal year.
        spring_count = 0
        total_count = 0
        for d in divs:
            if not isinstance(d, dict): continue
            dt = d.get("date") or d.get("paymentDate")
            if not dt or len(str(dt)) < 7: continue
            try:
                m = int(str(dt)[5:7])
                total_count += 1
                if 4 <= m <= 6:
                    spring_count += 1
            except (ValueError, TypeError):
                continue
        if total_count and spring_count / total_count >= 0.7:
            shifted = {yr - 1: v for yr, v in dps_by_year.items()}
            dps_by_year = shifted

        for yr, dps in dps_by_year.items():
            af = company.annual_financials.get(yr)
            if af and af.dividends_per_share is None:
                # EODHD dividends for LSE stocks are in GBX (pence); convert to GBP.
                af.dividends_per_share = dps / _gbx_factor

    # ── Historical year-end prices from /eod history ─────────────────────────
    eod = bundle.get("eod") or []
    if isinstance(eod, list) and eod:
        # Take last close per calendar year (EOD is ascending order)
        ye_prices: dict[int, float] = {}
        for row in eod:
            if not isinstance(row, dict): continue
            d = row.get("date")
            # adjusted_close is split/dividend-adjusted → correct for historical
            # market-cap and EV calculations. Fall back to close only if absent.
            p = row.get("adjusted_close") or row.get("close")
            yr = _year_from_date(d)
            pv = _f(p)
            if yr is not None and pv is not None and pv > 0:
                ye_prices[yr] = pv / _gbx_factor  # convert GBX→GBP for LSE; no-op for others
        for yr, af in company.annual_financials.items():
            if af.price_year_end is None and yr in ye_prices:
                af.price_year_end = ye_prices[yr]

    # ── Spring-payer DPS fix (German calendar reporters) ─────────────────────
    fwd_div_raw = _f(sd.get("ForwardAnnualDividendRate"))
    fwd_div = (fwd_div_raw / _gbx_factor) if fwd_div_raw is not None else None
    if fwd_div and company.annual_financials:
        latest_yr = max(company.annual_financials.keys())
        if company.annual_financials[latest_yr].dividends_per_share is None:
            company.annual_financials[latest_yr].dividends_per_share = fwd_div

    # ── Recompute market_cap / EV / EPS / ratios on every annual row ─────────
    # EPS strategy: EODHD's outstandingShares.annual provides retroactively
    # split-adjusted share counts (e.g. 967M for 2022 even after a 3:1 split).
    # The Income_Statement stores as-reported (pre-split) EPS which is wrong
    # for years before a stock split. Computing EPS = net_income / shares is
    # the most reliable method — both figures are in millions and already on
    # the same post-split-adjusted basis, so the result is split-adjusted EPS
    # consistent with what a data provider like Bloomberg/FactSet would show.
    # Fall back to the income-statement eps only when NI or shares are absent.
    for af in company.annual_financials.values():
        # af.price_year_end (from /eod) is in the TRADING currency. For a
        # dual-currency ADR that's a different currency than af.gross_profit/
        # af.revenue/af.net_debt (reporting currency, from Financials.yearly)
        # — combining them directly (as calculate_derived() does) produces
        # nonsense (huge/negative Enterprise Value, near-zero P/E). Convert
        # the price into reporting-currency terms first when an FX rate is
        # available; when it isn't, deliberately leave the price-derived
        # fields as n/a rather than silently mixing currencies.
        _price_for_calc = af.price_year_end
        if _dual_currency:
            _price_for_calc = (af.price_year_end * _fx_rate
                                if (_fx_rate and af.price_year_end is not None)
                                else None)

        if (af.market_cap is None and _price_for_calc
                and af.shares_outstanding):
            shares_m = (af.shares_outstanding / 1_000_000
                        if af.shares_outstanding > 1_000_000
                        else af.shares_outstanding)
            af.market_cap = _price_for_calc * shares_m
        # Recompute EPS from NI / shares (split-adjusted, consistent units).
        # Currency-neutral: both NI and shares are already in reporting
        # currency / share-count units regardless of price currency.
        shares_m = (af.shares_outstanding / 1_000_000
                    if af.shares_outstanding and af.shares_outstanding > 1_000_000
                    else af.shares_outstanding)
        if af.net_income is not None and shares_m:
            af.eps_diluted = af.net_income / shares_m
        # P/E needs price and EPS in the same currency — precompute it here
        # from the (possibly FX-converted) price so calculate_derived()'s own
        # price_year_end-based P/E (which would use the raw, mismatched price)
        # never fires for dual-currency companies.
        if (_dual_currency and af.pe_ratio is None and _price_for_calc
                and af.eps_diluted and af.eps_diluted > 0):
            af.pe_ratio = _price_for_calc / af.eps_diluted
        if _dual_currency:
            # Temporarily blank the raw (trading-currency) price so
            # calculate_derived()'s market-cap/P-E/div-yield fallbacks can't
            # combine it with reporting-currency fields; restore afterward
            # since the raw year-end price is still legitimate to display.
            _raw_price = af.price_year_end
            af.price_year_end = None
            af.calculate_derived()
            af.price_year_end = _raw_price
        else:
            af.calculate_derived()

    # Fallback EV = Market Cap + Net Debt when EODHD's own EnterpriseValue
    # field is missing/zeroed-out (see _fz() note above). company.market_cap
    # is always in the TRADING currency, while la.net_debt is a raw balance-
    # sheet figure in the REPORTING currency — for a dual-currency ADR these
    # must not be added together (same class of bug already fixed for EV/
    # Sales, EV/Gross-Profit, EV/EBIT, FCF Yield below). Skipped here when
    # dual-currency; company.calculate_current_ratios() (called at the end
    # of this function) carries the identical currency-aware guard, so
    # company.enterprise_value correctly stays None ("n/a") rather than a
    # silently wrong cross-currency figure, unless the dual-currency block
    # below successfully pre-populates enterprise_value_financials_ccy.
    if company.enterprise_value is None and company.market_cap is not None and not _dual_currency:
        _la = company.latest_annual()
        _nd = _la.net_debt if _la else None
        if _nd is not None:
            company.enterprise_value = company.market_cap + _nd

    # ── Dual-currency current/TTM Enterprise Value (reporting currency) ──────
    # company.enterprise_value/market_cap stay in the TRADING currency — that
    # matches EODHD's own EnterpriseValueRevenue/EnterpriseValueEbitda ratios
    # (company.ev_sales/ev_ebitda), which are already internally currency-
    # consistent on EODHD's side and should NOT be recomputed. But there is no
    # EODHD-native EV/Gross-Profit ratio, so a reporting-currency EV is built
    # here specifically for ratios that must divide by a statement-currency
    # figure (EV/Gross Profit, and EV/Sales as a fallback if EODHD's own
    # ev_sales is ever missing) — see compute_peer_snapshot_row() in
    # models/growth_pricing.py.
    if (_dual_currency and _fx_rate and company.shares_outstanding
            and company.current_price):
        _mc_financials = (company.shares_outstanding * company.current_price
                           * _fx_rate)
        _la2 = company.latest_annual()
        _nd_financials = _la2.net_debt if _la2 else None
        if _nd_financials is not None:
            company.enterprise_value_financials_ccy = _mc_financials + _nd_financials
            # EV/EBIT has no EODHD-native precomputed equivalent (unlike
            # EV/Sales, EV/EBITDA — see comment above), so it is always left
            # for CompanyData.calculate_current_ratios()'s fallback to fill
            # in later (`if self.ev_ebit is None: ev / la.ebit`), which would
            # divide the TRADING-currency `enterprise_value` by a reporting-
            # currency EBIT for a dual-currency ADR — the same class of bug
            # fixed for EV/Gross Profit and EV/Sales. Pre-populate it here
            # with the currency-consistent EV so that fallback's `is None`
            # guard skips it.
            if _la2 and _la2.ebit and _la2.ebit > 0:
                company.ev_ebit = company.enterprise_value_financials_ccy / _la2.ebit
        # FCF Yield = TTM FCF (reporting currency) / Market Cap also has no
        # currency-consistent EODHD-native equivalent — CompanyData's own
        # fallback divides `la.fcf` by the TRADING-currency `market_cap`,
        # which is likewise wrong for a dual-currency ADR. Pre-populate
        # using the reporting-currency market cap computed above. Prefer
        # company.ttm_fcf (actual trailing-twelve-month FCF) over the latest
        # FISCAL YEAR's la.fcf — this is a "TTM" scalar (paired with TTM
        # price/shares in _mc_financials), so it must be divided by TTM FCF,
        # not last-fiscal-year FCF, or the ratio mixes periods as well as
        # nearly-matching-but-different currencies.
        _fcf_for_yield = getattr(company, "ttm_fcf", None) or (_la2.fcf if _la2 else None)
        if _fcf_for_yield and _mc_financials > 0:
            company.fcf_yield = _fcf_for_yield / _mc_financials

    # ── Final touch: mark which scalar fields are EODHD-sourced ──────────────
    eodhd_fields_filled = [
        "name", "exchange", "currency", "currency_price", "currency_financials",
        "fx_rate_price_to_financials", "enterprise_value_financials_ccy", "sector",
        "industry", "country", "isin", "description", "website",
        "ipo_date", "fiscal_year_end", "address", "phone", "employees",
        "officers", "cik",
        "current_price", "market_cap", "shares_outstanding", "enterprise_value",
        "shares_float", "pct_insiders", "pct_institutions",
        "pe_ratio", "forward_pe", "price_to_book", "price_to_sales",
        "peg_ratio", "ev_sales", "ev_ebitda",
        "net_margin", "ebit_margin", "roe", "roa",
        "book_value_per_share", "revenue_per_share", "eps_ttm",
        "quarterly_revenue_growth_yoy", "quarterly_earnings_growth_yoy",
        "ttm_revenue", "ttm_ebitda", "ttm_ebit", "ttm_fcf", "ttm_last_quarter_date",
        "shares_short", "shares_short_prior_month", "short_ratio", "short_percent_of_float",
        "beta", "week_52_high", "week_52_low", "ma_50", "ma_200",
        "dividend_yield", "forward_annual_dividend_rate",
        "forward_annual_dividend_yield", "payout_ratio",
        "dividend_date", "ex_dividend_date",
        "last_split_factor", "last_split_date",
        "eps_estimate_next_year", "forward_estimates",
    ]
    for f in eodhd_fields_filled:
        v = getattr(company, f, None)
        if v not in (None, "", [], {}):
            if f not in company.eodhd_fields:
                company.eodhd_fields.append(f)

    company.calculate_current_ratios()
    return company
