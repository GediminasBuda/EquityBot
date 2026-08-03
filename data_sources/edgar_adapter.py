"""
edgar_adapter.py — Tier 1: SEC EDGAR XBRL API (US companies only).

Free, no API key. Primary source for US company 10-year financial history.
Data comes directly from official SEC filings — highest reliability for US stocks.

API docs: https://www.sec.gov/developer
XBRL API:  https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json
"""

from __future__ import annotations
import json
import time
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import requests

from .base import CompanyData, AnnualFinancials, DataSourceResult
from config import CACHE_DIR, REQUEST_HEADERS, REQUEST_DELAY

logger = logging.getLogger(__name__)

# EDGAR rate limit: max 10 requests/second, we stay polite at ~2/second
EDGAR_DELAY = 0.5

# Cache the CIK lookup table locally (refreshed weekly)
CIK_CACHE_FILE = CACHE_DIR / "edgar_company_tickers.json"
CIK_CACHE_TTL_DAYS = 7


# ── XBRL concept mappings ─────────────────────────────────────────────────────
# Maps our internal field names to the US-GAAP XBRL concept names used in EDGAR.
# Some metrics have multiple possible concepts (different companies use different ones).
XBRL_CONCEPTS = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "ebit": [
        "OperatingIncomeLoss",
    ],
    "ebitda": [
        # EBITDA is rarely reported as a single XBRL tag — usually calculated
        # We'll derive it from EBIT + D&A if needed
    ],
    "gross_profit": [
        "GrossProfit",
    ],
    "eps_diluted": [
        "EarningsPerShareDiluted",
    ],
    "total_assets": [
        "Assets",
    ],
    "total_debt": [
        "LongTermDebtAndCapitalLeaseObligation",
        "LongTermDebt",
        "DebtAndCapitalLeaseObligations",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsAndShortTermInvestments",
    ],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
    ],
    "dividends_per_share": [
        "CommonStockDividendsPerShareDeclared",
        "CommonStockDividendsPerShareCashPaid",
    ],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
}


class EdgarAdapter:
    """
    Fetches 10+ years of annual financial data from SEC EDGAR XBRL API.
    US companies only. Free. No API key required.

    Strategy:
    1. Resolve ticker → CIK using EDGAR's company tickers list
    2. Fetch all XBRL facts for the company
    3. Extract annual (FY) 10-K values for key financial metrics
    4. Construct AnnualFinancials objects for each fiscal year
    """

    SOURCE_NAME = "edgar"
    BASE_URL = "https://data.sec.gov/api/xbrl"
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

    def __init__(self):
        self._ticker_to_cik: Dict[str, str] = {}
        self._cik_facts_cache: Dict[str, dict] = {}  # in-memory for this session
        self._load_cik_table()

    def _load_cik_table(self) -> None:
        """Load or refresh the ticker→CIK mapping from SEC."""
        # Use local cache if fresh enough
        if CIK_CACHE_FILE.exists():
            age = datetime.utcnow() - datetime.utcfromtimestamp(CIK_CACHE_FILE.stat().st_mtime)
            if age < timedelta(days=CIK_CACHE_TTL_DAYS):
                try:
                    with open(CIK_CACHE_FILE) as f:
                        raw = json.load(f)
                    self._ticker_to_cik = {
                        v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                        for v in raw.values()
                    }
                    logger.debug(f"[edgar] Loaded {len(self._ticker_to_cik)} CIK mappings from cache.")
                    return
                except Exception as e:
                    logger.warning(f"[edgar] Cache load failed: {e}")

        # Fetch fresh from SEC
        try:
            logger.info("[edgar] Downloading company tickers list from SEC…")
            resp = requests.get(
                self.TICKERS_URL,
                headers={**REQUEST_HEADERS, "User-Agent": "EquityBot research@equitybot.local"},
                timeout=30,
            )
            resp.raise_for_status()
            raw = resp.json()
            CIK_CACHE_FILE.parent.mkdir(exist_ok=True)
            with open(CIK_CACHE_FILE, "w") as f:
                json.dump(raw, f)
            self._ticker_to_cik = {
                v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                for v in raw.values()
            }
            logger.info(f"[edgar] Loaded {len(self._ticker_to_cik)} tickers from SEC.")
        except Exception as e:
            logger.error(f"[edgar] Could not load CIK table: {e}")

    def _resolve_cik(self, ticker: str) -> Optional[str]:
        """
        Convert a ticker symbol to an SEC CIK number.
        Strips Yahoo Finance suffixes (e.g. "AAPL" from "AAPL" works directly).
        """
        clean = ticker.upper().split(".")[0].replace("-", ".")
        cik = self._ticker_to_cik.get(clean)
        if not cik:
            # Try without any suffix variations
            for sep in [".", "-", " "]:
                base = ticker.upper().split(sep)[0]
                cik = self._ticker_to_cik.get(base)
                if cik:
                    break
        return cik

    def resolve_cik(self, ticker: str) -> Optional[str]:
        """Public wrapper around _resolve_cik(), for use by other modules
        (e.g. data_sources/edgar_filing_fetcher.py) that need a CIK without
        pulling in the full XBRL-facts fetching machinery."""
        return self._resolve_cik(ticker)

    def _fetch_company_facts(self, cik: str) -> Optional[dict]:
        """Fetch all XBRL facts for a company from SEC EDGAR."""
        if cik in self._cik_facts_cache:
            return self._cik_facts_cache[cik]

        url = f"{self.BASE_URL}/companyfacts/CIK{cik}.json"
        try:
            time.sleep(EDGAR_DELAY)
            resp = requests.get(
                url,
                headers={**REQUEST_HEADERS, "User-Agent": "EquityBot research@equitybot.local"},
                timeout=30,
            )
            resp.raise_for_status()
            facts = resp.json()
            self._cik_facts_cache[cik] = facts
            return facts
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"[edgar] CIK {cik} not found (404).")
            else:
                logger.error(f"[edgar] HTTP error fetching facts for CIK {cik}: {e}")
            return None
        except Exception as e:
            logger.error(f"[edgar] Error fetching facts for CIK {cik}: {e}")
            return None

    def _extract_annual_values(
        self, facts: dict, concepts: List[str], unit: str = "USD"
    ) -> Dict[int, float]:
        """
        Search for annual 10-K values across a list of XBRL concept names.
        Returns {fiscal_year: value} using the first concept that has data.
        Values are in millions.
        """
        us_gaap = facts.get("facts", {}).get("us-gaap", {})

        for concept in concepts:
            concept_data = us_gaap.get(concept, {})
            unit_data = concept_data.get("units", {}).get(unit, [])

            if not unit_data:
                continue

            # Filter to annual (10-K) filings only
            annual_entries: Dict[int, Tuple[float, str]] = {}
            for entry in unit_data:
                form = entry.get("form", "")
                fy   = entry.get("fy")
                fp   = entry.get("fp", "")  # FY, Q1, Q2, Q3, Q4
                val  = entry.get("val")
                filed = entry.get("filed", "")

                if form in ("10-K", "10-K/A") and fp == "FY" and fy and val is not None:
                    # Keep the most recently filed entry for each year
                    if fy not in annual_entries or filed > annual_entries[fy][1]:
                        annual_entries[fy] = (float(val), filed)

            if annual_entries:
                return {yr: val / 1_000_000 for yr, (val, _) in annual_entries.items()}

        return {}

    def _extract_per_share_annual(
        self, facts: dict, concepts: List[str]
    ) -> Dict[int, float]:
        """
        Like _extract_annual_values but for per-share metrics (USD/shares unit).
        Returns raw value (not divided by 1M).
        """
        us_gaap = facts.get("facts", {}).get("us-gaap", {})

        for concept in concepts:
            concept_data = us_gaap.get(concept, {})
            # Per-share metrics use USD/shares or pure numbers
            for unit_key in ["USD/shares", "USD", "pure"]:
                unit_data = concept_data.get("units", {}).get(unit_key, [])
                if not unit_data:
                    continue

                annual_entries = {}
                for entry in unit_data:
                    form = entry.get("form", "")
                    fy   = entry.get("fy")
                    fp   = entry.get("fp", "")
                    val  = entry.get("val")
                    filed = entry.get("filed", "")

                    if form in ("10-K", "10-K/A") and fp == "FY" and fy and val is not None:
                        if fy not in annual_entries or filed > annual_entries[fy][1]:
                            annual_entries[fy] = (float(val), filed)

                if annual_entries:
                    return {yr: val for yr, (val, _) in annual_entries.items()}

        return {}

    def _extract_shares(self, facts: dict) -> Dict[int, float]:
        """Extract shares outstanding (in millions)."""
        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        for concept in XBRL_CONCEPTS["shares_outstanding"]:
            concept_data = us_gaap.get(concept, {})
            unit_data = concept_data.get("units", {}).get("shares", [])
            if not unit_data:
                continue

            annual_entries = {}
            for entry in unit_data:
                form  = entry.get("form", "")
                fy    = entry.get("fy")
                fp    = entry.get("fp", "")
                val   = entry.get("val")
                filed = entry.get("filed", "")

                if form in ("10-K", "10-K/A") and fp == "FY" and fy and val is not None:
                    if fy not in annual_entries or filed > annual_entries[fy][1]:
                        annual_entries[fy] = (float(val), filed)

            if annual_entries:
                return {yr: val / 1_000_000 for yr, (val, _) in annual_entries.items()}
        return {}

    def fetch(self, ticker: str) -> DataSourceResult:
        """
        Fetch 10-year annual financial history for a US-listed company.
        Returns DataSourceResult with CompanyData populated.

        Non-US tickers (those with exchange suffixes like .AS, .ST, .L)
        are gracefully declined — EDGAR covers US only.
        """
        start = time.time()

        # Quick check: EDGAR only covers US-listed companies
        if "." in ticker and not ticker.endswith((".A", ".B", ".C")):
            # Has a non-class suffix — likely non-US
            logger.debug(f"[edgar] Skipping non-US ticker: {ticker}")
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error="EDGAR covers US companies only.",
                duration_seconds=time.time() - start,
            )

        logger.info(f"[edgar] Fetching {ticker}…")

        # Resolve CIK
        cik = self._resolve_cik(ticker)
        if not cik:
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error=f"Could not find SEC CIK for ticker '{ticker}'. "
                      f"May not be a US-listed company.",
                duration_seconds=time.time() - start,
            )

        logger.info(f"[edgar] Resolved {ticker} → CIK {cik}")

        # Fetch all XBRL facts
        facts = self._fetch_company_facts(cik)
        if not facts:
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error=f"Could not fetch XBRL facts from EDGAR for CIK {cik}.",
                duration_seconds=time.time() - start,
            )

        # Company name from EDGAR
        entity_name = facts.get("entityName", "")

        # Extract all financial series
        revenues     = self._extract_annual_values(facts, XBRL_CONCEPTS["revenue"])
        net_incomes  = self._extract_annual_values(facts, XBRL_CONCEPTS["net_income"])
        ebits        = self._extract_annual_values(facts, XBRL_CONCEPTS["ebit"])
        gross_profits= self._extract_annual_values(facts, XBRL_CONCEPTS["gross_profit"])
        total_assets = self._extract_annual_values(facts, XBRL_CONCEPTS["total_assets"])
        total_debts  = self._extract_annual_values(facts, XBRL_CONCEPTS["total_debt"])
        cashes       = self._extract_annual_values(facts, XBRL_CONCEPTS["cash"])
        equities     = self._extract_annual_values(facts, XBRL_CONCEPTS["total_equity"])
        ocfs         = self._extract_annual_values(facts, XBRL_CONCEPTS["operating_cash_flow"])
        capexes      = self._extract_annual_values(facts, XBRL_CONCEPTS["capex"])
        deps         = self._extract_annual_values(facts, XBRL_CONCEPTS["depreciation_amortization"])
        shares       = self._extract_shares(facts)
        eps_diluted  = self._extract_per_share_annual(facts, XBRL_CONCEPTS["eps_diluted"])
        divs_ps      = self._extract_per_share_annual(facts, XBRL_CONCEPTS["dividends_per_share"])

        # Collect all years we have any data for
        all_years = set()
        for d in [revenues, net_incomes, ebits, gross_profits, total_assets,
                  total_debts, cashes, equities, ocfs, capexes, shares]:
            all_years.update(d.keys())

        if not all_years:
            return DataSourceResult(
                success=False,
                source_name=self.SOURCE_NAME,
                error=f"No annual XBRL financial data found for {ticker} (CIK {cik}).",
                duration_seconds=time.time() - start,
            )

        # Build CompanyData with annual history
        company = CompanyData(
            ticker=ticker,
            input_ticker=ticker,
            name=entity_name or None,
            cik=cik,
            fetch_timestamp=datetime.utcnow().isoformat(),
            as_of_date=datetime.utcnow().strftime("%Y-%m-%d"),
            data_sources=[self.SOURCE_NAME],
        )

        fields_filled = ["annual_financials"]
        if entity_name:
            fields_filled.append("name")

        # Filter to last 12 years maximum, build AnnualFinancials
        recent_years = sorted(all_years, reverse=True)[:12]
        for year in recent_years:
            af = AnnualFinancials(year=year)
            af.revenue       = revenues.get(year)
            af.net_income    = net_incomes.get(year)
            af.ebit          = ebits.get(year)
            af.gross_profit  = gross_profits.get(year)
            af.total_assets  = total_assets.get(year)
            af.total_debt    = total_debts.get(year)
            af.cash          = cashes.get(year)
            af.total_equity  = equities.get(year)
            af.operating_cash_flow = ocfs.get(year)
            af.shares_outstanding  = shares.get(year)
            af.eps_diluted   = eps_diluted.get(year)
            af.dividends_per_share = divs_ps.get(year)

            # CapEx is reported as a positive payment in XBRL
            capex_raw = capexes.get(year)
            if capex_raw is not None:
                af.capex = abs(capex_raw)

            # Compute EBITDA = EBIT + Depreciation & Amortization
            dep = deps.get(year)
            if af.ebit is not None and dep is not None:
                af.ebitda = af.ebit + dep

            af.calculate_derived()
            company.annual_financials[year] = af

        logger.info(
            f"[edgar] {ticker} (CIK {cik}) done. "
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


# ── Quick sanity test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    adapter = EdgarAdapter()

    for ticker, label in [("AAPL", "Apple"), ("MSFT", "Microsoft"), ("WKL.AS", "Wolters Kluwer (should fail)")]:
        print(f"\n{'='*60}")
        print(f"  {label} ({ticker})")
        result = adapter.fetch(ticker)
        if result.success:
            c = result.data
            print(f"  {c.summary()}")
            for yr in c.sorted_years()[:4]:
                af = c.annual_financials[yr]
                print(f"  {yr}: Rev={af.revenue}M, EBIT={af.ebit}M, NI={af.net_income}M, EPS={af.eps_diluted}")
        else:
            print(f"  FAILED: {result.error}")
