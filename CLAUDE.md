# Your Humble EquityBot — Agent Handoff Documentation

**Last updated:** 2026-07-01  
**Stack:** Python 3.11 · Streamlit · ReportLab · Claude / GPT-4o · EODHD · yfinance  
**Deployment:** Streamlit Community Cloud (auto-deploys on push to `Final-design-V3`)  
**Repo:** https://github.com/GediminasBuda/EquityBot

---

## 1. What This App Does

A private AI-powered equity research tool. The user enters a stock ticker, picks a report framework, and the app:
1. Fetches financial data from up to 5 data sources (waterfall architecture)
2. Optionally calls an LLM (Claude or GPT-4o) to generate analysis
3. Renders a professional PDF report and offers a download

The app is used by one person (the owner) for personal investment research. It is not a commercial product.

---

## 2. Project Structure

```
EquityBot/
├── app.py                          # Entry point, auth gate, page routing
├── config.py                       # All settings, API keys, paths
├── framework_manager.py            # CRUD for report framework JSON configs
├── constituent_resolver.py         # Resolves index → constituent tickers
│
├── data_sources/
│   ├── base.py                     # CompanyData, AnnualFinancials, ForwardEstimates dataclasses
│   ├── data_manager.py             # Waterfall orchestrator (yfinance → EODHD → EDGAR → AV → FMP)
│   ├── yfinance_adapter.py         # Tier 1a: current price, shares, dividends, annual history
│   ├── eodhd_adapter.py            # Tier 1b: full fundamentals for 70k+ global companies
│   ├── edgar_adapter.py            # Tier 1c: US SEC filings (fill-only)
│   ├── alpha_vantage_adapter.py    # Tier 2: fallback for non-US when EODHD fails
│   ├── fmp_adapter.py              # Tier 4: paid fallback for critical missing fields
│   ├── fred_adapter.py             # US macro data (Fed Funds, CPI, yields)
│   ├── news_adapter.py             # Recent news headlines per company
│   ├── worldbank_adapter.py        # Country-level macro (GDP, inflation, etc.)
│   └── index_adapter.py            # Index/ETF data (separate from company waterfall)
│
├── models/
│   ├── overview.py                 # LLM prompt builder for Overview report
│   ├── fisher.py                   # LLM prompt builder for Fisher 15Q report
│   ├── gravity.py                  # LLM prompt builder for Gravity Score report
│   ├── generic_runner.py           # Runs user-created custom frameworks
│   ├── universe_screener.py        # Multi-ticker screening / universe comparison
│   └── index_runner.py             # Index overview report runner
│
├── agents/
│   ├── llm_client.py               # Provider-agnostic LLM wrapper (Claude + OpenAI)
│   ├── adversarial.py              # Dual-model adversarial review engine
│   ├── pdf_overview.py             # ReportLab PDF renderer for Overview
│   ├── pdf_fisher.py               # ReportLab PDF renderer for Fisher
│   ├── pdf_gravity.py              # ReportLab PDF renderer for Gravity Score
│   ├── pdf_eodhd_sheet.py          # ReportLab PDF renderer for EODHD Data Sheet (no LLM)
│   ├── pdf_adversarial.py          # Adversarial report appendix renderer
│   └── report_generic.py           # HTML report renderer for custom frameworks
│
├── pages/
│   ├── report_generator.py         # Main UI page — the full generate pipeline
│   └── model_editing.py            # Framework editor / studio page
│
├── frameworks/                     # JSON config files for each report type
│   ├── overview.json
│   ├── fisher.json
│   ├── gravity.json
│   ├── eodhd_sheet.json
│   └── index_overview.json
│
├── cache/                          # Auto-generated: cached CompanyData JSON (24h TTL)
├── outputs/                        # Auto-generated: saved PDF/HTML reports
├── utils/
│   └── auth.py                     # Auth helpers
└── .env                            # Local dev secrets (gitignored — NEVER commit)
```

---

## 3. Environment & Secrets

### Local Development
Secrets live in `.env` (gitignored). Copy and fill:
```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...
ALPHA_VANTAGE_API_KEY=...
FRED_API_KEY=...
FMP_API_KEY=...
EODHD_API_KEY=...
SIMFIN_API_KEY=...
NEWS_API_KEY=...
LLM_PROVIDER=openai          # or "claude"
LLM_MODEL=gpt-4o             # or "claude-sonnet-4-5"
ADVERSARIAL_MODE=false
```

### Streamlit Cloud
Secrets are set in the Streamlit dashboard Secrets manager (same key names).  
**Critical:** `app.py` injects secrets into `os.environ` BEFORE `config.py` is imported. Secrets always unconditionally override `.env` values (`os.environ[k] = str(st.secrets[k])`).  
After changing secrets → manually reboot the app in the Streamlit dashboard, OR push any commit (triggers auto-redeploy).

### Current Active Provider
As of 2026-05-11: **OpenAI GPT-4o** (`LLM_PROVIDER=openai`, `LLM_MODEL=gpt-4o`)

---

## 4. Authentication

`app.py` checks `st.secrets["users"]` for `{username: sha256_password}` pairs.  
- If `[users]` section exists → login gate is shown
- If no `[users]` section → dev mode, gate bypassed entirely

Generate a password hash:
```python
import hashlib
print(hashlib.sha256("your_password".encode()).hexdigest())
```

---

## 5. Data Pipeline — How It Works

### Entry point
```python
company = DataManager().get("RHM.DE", force_refresh=False)
```

### Cache
- Location: `cache/<TICKER>.json` (ticker dots/dashes replaced with underscores)
- TTL: 24 hours (`CACHE_TTL_HOURS` in config.py)
- "Force refresh" checkbox in UI bypasses cache
- Cache is invalidated by deleting the file or TTL expiry
- **Important:** After adding new fields to `CompanyData`, old cached files won't have those fields. Force-refresh to get them.

### Waterfall (in order)

| Tier | Source | Condition | Mode |
|------|--------|-----------|------|
| 1a | yfinance | Always | Override (creates base object) |
| 1b | EODHD | If `EODHD_API_KEY` set | Full override for all statement fields |
| 1c | SEC EDGAR | US tickers only, if < 7 years history | Fill-only |
| 2 | Alpha Vantage | If < 7 years history OR EODHD failed non-US | Fill-only / override |
| 4 | FMP | If critical fields still None | Fill-only for critical fields |

### Merge semantics
The `_merge()` method in `data_manager.py` copies fields from source into target:
- **Scalar fields**: only copied if target is `None` OR target is `[]` (empty list)
- **annual_financials**: per-year merge — `_override_annual()` (EODHD) or `_merge_annual()` (fill-only)
- **EODHD full override**: replaces all income/balance/cashflow fields. Exception: `net_income` and `eps_diluted` are fill-only (yfinance IFRS figures are more reliable for consolidated net income)
- After any override: derived ratio fields (`roe`, `pe_ratio`, `ev_ebit`, etc.) are reset to `None` so `calculate_derived()` recomputes them from the corrected source data
- **Whitelist**: the `fields=[]` list in the EODHD `_merge()` call must explicitly include every field you want merged. Adding a new field to `CompanyData` is not enough — it must also be in this list.

### Post-merge
After all tiers:
1. `company.calculate_current_ratios()` — derives current EV, EV multiples, FCF yield, gearing
2. Margins and ROE are re-derived from `latest_annual()` (overrides TTM scalars from EODHD Highlights to keep report tables consistent)
3. Result saved to cache

---

## 6. Key Data Models

### `CompanyData` (data_sources/base.py)
The master container. ~80 fields covering:
- Identity: name, ticker, ISIN, exchange, sector, industry, country, description, website, employees, IPO date, fiscal year end, address, phone, officers list
- Current market: price, market cap, shares outstanding, enterprise value
- Technicals: 52-week high/low, 50/200-day MAs, beta
- Ownership: shares float, % insiders, % institutions
- Dividends: yield, forward div rate/yield, payout ratio, dates, split history
- Valuation multiples: P/E, forward P/E, PEG, P/B, P/S, EV/Sales, EV/EBITDA, EV/EBIT
- Per-share TTM: EPS, book value/share, revenue/share
- Margins: gross, EBITDA, EBIT, net (all TTM)
- Returns: ROE, ROA, ROIC
- Annual history: `Dict[int, AnnualFinancials]` keyed by fiscal year
- Forward estimates: `ForwardEstimates` object

### `AnnualFinancials` (data_sources/base.py)
One row per fiscal year (~40 fields):
- P&L: revenue, gross profit, EBITDA, EBIT, net income, EPS diluted, DPS
- Margins: gross, EBIT, EBITDA, net (as decimals: 0.15 = 15%)
- Balance sheet: total assets, debt, cash, net debt, equity, shares outstanding
- Cash flow: operating CF, capex, FCF
- Returns: ROE, ROA, ROIC
- Market-based (computed from year-end prices): price_year_end, market_cap, EV, P/E, EV/EBIT, EV/Sales, FCF yield, div yield
- `calculate_derived()` fills ratios from raw values

### `ForwardEstimates` (data_sources/base.py)
Analyst consensus for the next fiscal year: revenue, EPS, net income, EBITDA, growth rates, forward P/E, EV/Sales.

---

## 7. Known Bugs Fixed (History)

These were real bugs that were investigated and fixed. Knowing they exist helps if similar issues appear:

### EODHD `commonStock` is NOT share count
`EODHD.General.Balance_Sheet.commonStock` = subscribed capital (Grundkapital) in EUR, NOT shares. For German companies like RHM.DE this is ~€112M which incorrectly becomes 112M shares (actual: ~46M). **Fix:** removed the commonStock share override block in `eodhd_adapter.py`. yfinance provides correct `Ordinary Shares Number`.

### German company DPS timing (spring payer detection)
German companies pay the prior fiscal year's dividend in April-June of the following year. yfinance groups dividends by *payment* year, not *declared* year. **Fix:** in `yfinance_adapter.py`, if ≥70% of dividend payments fall in months 4-6, the adapter detects this as a "spring payer" and shifts all DPS assignments back 1 year (payment year Y+1 = declared for fiscal year Y).

### EODHD overrides causing stale derived fields
After EODHD overrides revenue/equity/etc., derived fields (roe, pe_ratio, ev_ebit, etc.) computed by yfinance earlier are stale but still set (non-None), so `calculate_derived()`'s `if None` guards skip them. **Fix:** `_override_annual()` explicitly resets all derived fields to `None` after overriding source data.

### HexColor.hexval() returns "0xRRGGBB" not "#RRGGBB"
`HexColor("#1A7E3D").hexval()` returns `"0x1a7e3d"`. Slicing `[1:]` gives `"x1a7e3d"`. Prepending `"#"` gives `"#x1a7e3d"` — invalid in ReportLab Paragraph XML markup, throws exception. **Fix:** defined plain string constants `GREEN_HEX = "#1A7E3D"` etc. for use in markup. Never call `.hexval()`.

### Date range showing oldest years instead of newest
`company.sorted_years()` returns years in descending order (newest first). Double-reversing it (`list(reversed(company.sorted_years()))`) produces ascending order (oldest first). Then `[:8]` takes the 8 *oldest* years. **Fix:** `all_hist = company.sorted_years()` (descending), then `list(reversed(all_hist[:8]))` = take 8 newest, reverse to chronological.

### OpenAI ignoring prompt schema (cacheable_prefix)
All report prompts are split into: `cacheable_prefix` (static schema + instructions) + `dynamic_prompt` (company data). Claude receives both via separate content blocks. The `_openai()` method was silently ignoring `cacheable_prefix`, so GPT-4o only saw raw financial numbers with no JSON schema — all fields came back empty. **Fix:** `_openai()` now prepends `cacheable_prefix` to `user_prompt` before sending.

### EODHD new fields not merging
Added ~20 new fields to `CompanyData` (52-week levels, ownership %, officers, etc.) and fetched them in `eodhd_adapter.py`. However, `_merge()` in `data_manager.py` uses an explicit whitelist (`fields=[]`). New fields not in the whitelist are fetched but silently discarded. **Fix:** all new fields added to the whitelist in the EODHD `_merge()` call.

### Streamlit secrets not overriding .env
`_inject_cloud_secrets()` in `app.py` had `if k in st.secrets and not os.environ.get(k)` — only writing a secret if the env var wasn't already set. But `config.py` loads `.env` with `override=True` first, so `LLM_PROVIDER=claude` from `.env` blocked the `openai` secret. **Fix:** removed the guard; secrets now unconditionally override: `os.environ[k] = str(st.secrets[k])`.

### `analysis` NameError in eodhd_sheet branch
The `eodhd_sheet` report type skips the LLM entirely but the code after all branches referenced `analysis` variable. **Fix:** added `analysis = {}` in the `eodhd_sheet` dispatch block.

### Investment Memo peer list ignoring LLM suggestions when user adds peers
In `pages/report_generator.py`, the peer list was built with `peer_list or llm_peers` — if the user selected even one peer, LLM suggestions were skipped entirely, leaving only 1 peer in the comparison table. **Fix:** changed to a merge strategy: user-selected peers fill slots first, then LLM-suggested peers (`suggested_peers` from analysis JSON) backfill the remaining slots, deduplicating throughout, up to 6 total.

### Fisher Alternatives + Peers ignoring LLM suggestions when user adds peers
Same bug as above, in the `fisher_peers` dispatch block of `pages/report_generator.py`. The `if peer_list: ... else: <LLM suggest>` branch meant any user-supplied peer(s) skipped the LLM suggestion entirely. **Fix:** same merge strategy — user peers fill slots first, then `suggest_peers()` from `models/fisher_peers.py` is always called to backfill remaining slots (up to `6 - len(user_peers)`), deduplicating throughout, max 6 total. If the user supplies 6 peers the LLM call is skipped.

### Investment Memo V2 — data inconsistency across tables (ROE, EV/Sales)
`CompanyData` scalar fields (`company.roe`, `company.ev_sales`, etc.) come from yfinance TTM or EODHD Highlights and use the **current live price** for EV-based multiples. `AnnualFinancials[year]` ratios use the **year-end stock price**. For EODHD companies these stay in sync; for yfinance-only companies (Japanese `.T` stocks) they can diverge significantly — e.g. ROE 15.4% (annual-derived) vs 6.3% (TTM scalar), or EV/Sales 8.8x (year-end price) vs 20.4x (current price). **Fix (2026-06-08):** added a dedicated **TTM column** to the Financial Summary table (between the most-recent year and the forward-estimate column), populated from `CompanyData` scalars. The Peer table and Investment Checklist continue to use TTM scalars, with clear footnotes stating this. Footnotes added under both tables in `_page3`.

### Investment Memo V2 — annual EV incorrectly equals market cap for Japanese stocks
`yfinance_adapter.py` computes historical `AnnualFinancials.enterprise_value = market_cap + net_debt`. If the annual balance sheet keys don't match (common for `.T` stocks — yfinance uses different row names), `net_debt` stays `None`, and the fallback `af.enterprise_value = af.market_cap` silently ignores the cash position. This makes the EV column show an inflated value (year-end market cap only, no cash deduction). **Fix (2026-06-08):** the `_annual_ev()` lambda in `_build_financial_table` now returns `None` when `net_debt` cannot be determined from balance sheet data, showing `— n/a` instead of a misleading value. The TTM column always shows the correct `company.enterprise_value` (sourced from `info["enterpriseValue"]` which is reliable). **Known open issue:** the underlying balance-sheet key mismatch in `yfinance_adapter._parse_annual_history` for `.T` tickers has not been fixed — investigate which key names yfinance actually uses for Japanese stock balance sheets.

### Investment Memo V2 — banner hardcoded as EODHD even for yfinance-only stocks
The page-1 banner always said "Investment Memo V2 — EODHD Based" regardless of the actual data source used. **Fix (2026-06-08):** banner now detects whether annual data has `source == "eodhd"` or `"eodhd"` is in `company.data_sources`, and displays the appropriate source text (EODHD or yfinance).

### Investment Memo V2 — AttributeError on ttm_revenue for cached objects
`_build_financial_table` in `pdf_overview_v2.py` accessed `company.ttm_revenue` (and `ttm_ebitda`, `ttm_ebit`, `ttm_net_income`) directly. If the Streamlit app still had the old `CompanyData` class in memory (before restarting after the field was added to `base.py`), instances created from that old class don't have the attribute, raising `AttributeError` and crashing the entire Financial Summary table. **Fix (2026-06-08):** all four new TTM fields now accessed via `getattr(company, 'ttm_revenue', None)` etc. — defensive against old cached class instances.

### Investment Memo V2 — EV/Sales stale in TTM column and peer table (yfinance scalar)
`company.ev_sales` is populated from yfinance's pre-computed `enterpriseToRevenue` ratio. For Japanese TSE stocks this value is internally inconsistent (different price reference) and could be 20x instead of the correct ~3x. The TTM column in the Financial Summary and the Peer Group table both displayed this stale scalar. **Fix (2026-06-08):**
- TTM column: compute `ttm_ev_sales = enterprise_value / (ttm_revenue or latest_annual.revenue)` — uses annual revenue as denominator fallback so result stays consistent with the peer table.
- Peer table `make_row`: compute `ev_sales = peer_ev / la.revenue` from stored EV and latest-annual revenue, falling back to `c.ev_sales` only if both unavailable.
- Peer table `make_row`: compute `ev_sales = peer_ev / la.revenue` from the stored EV and latest-annual revenue, falling back to `c.ev_sales` only if those are unavailable. This self-corrects even without clearing the peer cache.

### Investment Memo V2 — TTM column empty for yfinance-only stocks (no quarterly data fetched)
Sales, EBITDA, Net Income rows in the TTM column showed `— n/a` for Japanese stocks. Root cause: `ttm_revenue` etc. were defined in `CompanyData` but never populated. **Fix (2026-06-08):** added `_compute_ttm_metrics()` to `YFinanceAdapter` that sums the last 4 quarters from `yt.quarterly_financials` (columns sorted descending, annualises partial data via `total * 4/count`). Revenue fallback chain: quarterly sum → `latest_annual().revenue`. Do NOT use `info["totalRevenue"]` — for TSE stocks yfinance returns the most recent single quarter's revenue in that field (e.g. 604M instead of 2,295M TTM). Do NOT use `totalNonCurrentLiabilities` as net-debt proxy — NCL includes non-financial items and overstates debt. Use `_fix_net_debt_from_balance_sheet()` which reads the most recent quarterly balance sheet with the same multi-key cash lookup as the annual parser (`"Cash Cash Equivalents And Short Term Investments"` captures full liquid assets vs `info["totalCash"]` cash-only).

### `yt.income_stmt` breaks the entire yfinance fetch ("No data found")
Calling `yt.income_stmt` (a newer yfinance attribute alias) makes a **separate HTTP request** after the main Ticker fetch. This second request corrupts yfinance's internal Ticker cache for that symbol. As a result, every subsequent call on the same `yt` object — `yt.financials`, `yt.quarterly_financials`, `yt.balance_sheet`, etc. — returns empty DataFrames, leaving `company.annual_financials` empty. The report generator then raises "No data found for 2477.T". **Fix (2026-06-08):** removed `yt.income_stmt` entirely from `yfinance_adapter.py`. To read the TTM column, reuse the already-fetched `financials` DataFrame (`yt.financials`): sort its columns descending; if the first column is newer than the second, it is the TTM period — read `"Total Revenue"` / `"Operating Income"` / `"Net Income"` from that column. This avoids any extra HTTP call.

### Investment Memo V2 — TTM Revenue/EBITDA never populated (wrong code path)
`eodhd_adapter.py` and `data_manager.py` do NOT run for Investment Memo V2. V2 uses its own dedicated builder: `data_sources/eodhd_only_builder.py` → `fetch_company_data_eodhd_only()`. Fixes applied to `eodhd_adapter.py` are completely invisible to V2. The V2 builder never read `Highlights.RevenueTTM` or `Highlights.EBITDA`. **Fix (2026-06-09):** in `eodhd_only_builder.py`, read `_to_m(h.get("RevenueTTM"))` and `_to_m(h.get("EBITDA"))` into `company.ttm_revenue` / `company.ttm_ebitda`. Added `ttm_last_quarter_date` from `Financials.Income_Statement.quarterly` most-recent key. Added `ttm_fcf` from quarterly `Cash_Flow` sum. Added fallbacks that sum last 4 quarterly rows when Highlights fields are null. **Critical rule: when debugging V2 data issues, always look in `eodhd_only_builder.py` first.**

### Investment Memo V2 — EODHD fallback architecture (exchanges not covered)
V2 runs in three modes depending on the ticker suffix, decided in `pages/report_generator.py`:
1. **`.T` (Japan):** explicitly detected → `_JAPAN_BUNDLE` (empty fundamentals); yfinance `company` from `dm.get()` is preserved unchanged.
2. **`.VS` / `.TL` / `.RG` (Baltic):** explicitly detected → `_BALTIC_BUNDLE`; same pattern as Japan.
3. **Everything else:** calls `fetch_company_data_eodhd_only()`. If EODHD returns usable data (has `name` + `market_cap` or `annual_financials`) → replaces `company` with the EODHD object. **If EODHD returns empty/no data** (e.g. India `.NS`/`.BO`, Singapore `.SI`) → keeps the yfinance `company` from `dm.get()` and uses an empty bundle, same as Japan. A warning is shown in the Streamlit UI. This prevents the report from collapsing for any exchange EODHD does not cover. Exchanges known to be unsupported by EODHD: Japan (`.T`), India (`.NS`, `.BO`), Singapore (`.SI`).

### Investment Memo V2 — historical EPS not split-adjusted
EODHD's `Financials.Income_Statement.yearly.eps` stores **as-reported** (pre-split) EPS. For a company with a 3:1 stock split, years before the split show 3× the correct EPS. There was a post-hoc override from `Earnings.Annual.epsActual` (which IS split-adjusted), but it ran AFTER `calculate_derived()` — so P/E was computed from the wrong EPS, then EPS was patched but P/E remained stale. **Fix (2026-06-09):** in `eodhd_only_builder.py`, pre-parse `Earnings.Annual.epsActual` into a `year → value` dict BEFORE the income-statement loop. Use it as the PRIMARY EPS source inside the loop so `calculate_derived()` sees the correct split-adjusted figure from the start. Fallback to `Income_Statement.eps` only when `epsActual` is absent. Removed the redundant post-hoc override block.

### Investment Memo V2 — historical Market Cap and EV inflated for split stocks
`ye_prices` (year-end price lookup from EODHD EOD data) used `row.get("close") or row.get("adjusted_close")` — taking `close` (raw, unadjusted) first. For a stock with a 3:1 split, the 2022 `close` is ~$90 while `adjusted_close` is ~$30. Market cap computed from `close × shares` was inflated 3×, and EV = market_cap + net_debt inherited that error. **Fix (2026-06-09):** reversed preference: `row.get("adjusted_close") or row.get("close")`. Always prefer `adjusted_close` which EODHD retroactively adjusts for splits and dividends.

### Investment Memo V2 — PDF header, colors, and table formatting (2026-06-09)
Multiple formatting fixes applied to `agents/pdf_overview_v2.py` and `agents/price_chart.py`:
- **Header layout:** company name and Price now on the same baseline (`H-11mm`); subtitle and MCap on the same baseline (`H-17mm`); separator line at `H-21mm` (snug below subtitle). Top margin reduced 38mm → 28mm (tighter gap to chart).
- **All line colors → #003F54:** `BLUE = HexColor('#003F54')` (was `#2E75B6`). All table grid separators, TTM column borders, and the header rule now use brand navy consistently.
- **Removed:** framed V2 disclaimer banner (replaced by small unbordered footnote under Financial Summary); section-title `HRFlowable` underlines; chart footnote "Source: EODHD…"; date from footer (date is in header subtitle).
- **Section titles:** `section_title()` now returns a plain `Paragraph` — no `HRFlowable` underline.
- **TTM column:** uses `cell()` not `italic_cell()` — TTM is factual historical data. Only estimate column keeps italic.

### Investment Memo V2 — TTM Net Income, EPS, Sales estimate, formatting (2026-06-09 session 2)
- **TTM Net Income**: populated in `eodhd_only_builder.py` by summing last 4 quarterly `netIncomeApplicableToCommonShares` rows from `Financials.Income_Statement.quarterly`. Fallback: `ProfitMargin × RevenueTTM`. Added to `data_manager.py` EODHD merge whitelist.
- **EPS split-adjustment**: EODHD's `Income_Statement.yearly.eps` is as-reported (pre-split). EODHD's `outstandingShares.annual` IS retroactively split-adjusted (e.g. shows 967M for 2022 after a 3:1 split in 2023). **Fix:** compute `af.eps_diluted = af.net_income / af.shares_outstanding` in the final recompute loop — both in millions, both on consistent post-split basis. Removed the flawed split-factor adjustment block that was dividing already-wrong EPS by the split ratio.
- **2026E Sales unit bug**: `Earnings.Trend.revenueEstimateAvg` is in full dollars. Changed `_f()` → `_to_m()` for `fe.revenue` so it is stored in millions like all other revenue fields. Was displaying `29375108.94B` instead of `29.38B`.
- **Number formatting**: `cell()`, `italic_cell()`, `_fmt_b()` in `pdf_overview_v2.py` changed from `:.1f` to `:.2f` for B and M suffixes. Percentages remain `:.1f`.
- **Chart title**: removed "5-Year" from `price_chart.py` — now shows `"{Company Name} · Daily Close"` only (chart period can vary).

### Investment Memo V2 — Net Profit table cleanup and FCF row (2026-06-09)
- "Net Profit (IFRS)" renamed to **"Net Income"** (reflects that the field `a.net_income` is used for both EODHD and yfinance data sources).
- "Net Profit (Adj.)" row removed — `net_income_underlying` (EPS × shares) was computed and displayed alongside reported net income, causing confusion. Forward estimate now uses `fe.eps_diluted × shares` as the Net Income estimate directly.
- New **"FCF (M)"** row added between Div. Yield and FCF Yield, showing `a.fcf` from EODHD cashflow statement for historical years and `company.ttm_fcf` (quarterly sum) for the TTM column.
- Row separator `LINEBELOW` indices updated to match new row layout.

### My Portfolio — company name as expand/collapse click target (2026-06-11)
Removed the "Chart ▾/▴" button column from each portfolio card. The company name cell is now the click/tap target to expand or collapse the detail panel (chart, news, Remove button).

**Hover behaviour:** name turns white on hover (cursor: pointer); expanded state shows name in lighter amber (`#FFD080`) so open cards are visually distinct.

**Mechanism:** the name cell HTML carries `data-ticker="{ticker}"` and optional `pf-name-active` class (when expanded). A hidden zero-height Streamlit toggle button still drives Streamlit's state — a same-origin `st.iframe` script (same pattern as the searchbox styling) uses `setInterval(500ms)` to bind click handlers on `.pf-name-cell[data-ticker]` elements and forward them to the hidden button via JS `.click()`. Re-binds automatically after every `st.rerun()` as Streamlit rebuilds the DOM.

**CSS rules added:** `.pf-name-cell { cursor: pointer }` + `:hover` and `.pf-name-active` colour overrides; anchor container and adjacent button container collapsed to `display:none` / `height:0` (JS `.click()` still fires on `display:none` elements).

---

### My Portfolio — 52WH and 52WL metrics added (2026-06-11)
Two new metrics appended after YTD on every portfolio card:
- **52WH**: `(price / 52_week_high) − 1` — how far the current price sits below (or above) the 52-week high. Usually negative (red). Positive (blue) only if at a new high.
- **52WL**: `(price / 52_week_low) − 1` — how far the current price is above the 52-week low. Always positive (blue).

**Data source:** `fund["Technicals"]["52WeekHigh"]` / `["52WeekLow"]` from the EODHD fundamentals endpoint (already fetched in `_fetch_snapshot`). Japan/Baltic (yfinance path): `info["fiftyTwoWeekHigh"]` / `["fiftyTwoWeekLow"]`.

**Layout:**
- Desktop: CSS grid `grid-template-columns: minmax(0, 2fr) repeat(11, minmax(0, 0.9fr))` — 11 metric columns (EARNINGS · PRICE · MKT CAP · P/E · F P/E · ROE · EBIT M. · Q REV YoY · YTD · 52WH · 52WL).
- Mobile: the two new cells carry `order: 7` / `order: 8` in the 2-col mobile grid, forming a new 4th row (52WH | 52WL) below the existing ROE | YTD row.

---

### My Portfolio — additional metrics, UX and layout polish (2026-06-11)

**New metrics added to snapshot:**
- **F P/E** (Forward P/E): between P/E and ROE. Source: `fund["Valuation"]["ForwardPE"]` (EODHD), fallback `highlights["ForwardPE"]`. Note: `Highlights.ForwardPE` is empty for most tickers — always read from `Valuation.ForwardPE` first.
- **Q REV YoY** (Quarterly Revenue Growth YoY): between EBIT M. and YTD. Source: `highlights["QuarterlyRevenueGrowthYOY"]`.
- Earnings date format changed from `YYYY-MM-DD` to `YY/MM/DD` on desktop cards.
- Chart periods: removed 1m and 6m; added 1y. Available: `["1d", "YTD", "1y", "5y", "All"]`, default `"5y"`.

**Name cell as expand/collapse target (replaces "Chart ▾/▴" button):**
- Chart button column removed. Company name cell (`pf-name-cell`) is the click target.
- A small flip arrow (`▼`) sits inline after the name; rotates 180° when card is expanded (`pf-name-active` class).
- Trash icon (🗑) sits inline at the right of the name row, always visible. Uses `event.stopPropagation()` so it doesn't trigger expand. On mobile: `position: absolute; top: 8px; right: 8px`.
- JS forwarder in `st.iframe` binds click handlers every 500ms via `setInterval`. Clicks `.pf-name-cell` → fires hidden toggle button; clicks `.pf-trash-icon` → fires hidden delete button.
- Hidden buttons use `display: none` — removes them from flex flow (no phantom gap). JS `.click()` fires on `display:none` elements.

**Card layout / spacing (final values 2026-06-11):**
- Card padding: `6px 12px 11px` (top / sides / bottom)
- Internal grid row gap: `6px 4px` (row / column)
- Inter-card gap: `9px` on the `stVerticalBlock` flex container
- Card hover: `border-color: #FFA028`, `position: relative`, `z-index: 1`, `overflow: hidden` — lifts card above neighbor so all 4 border sides light up; `overflow: hidden` prevents content bleeding.

---

### My Portfolio — price color + sortable columns (2026-06-11)

**Price color by daily change:**
- Blue (`#4D9FFF`) — price up today vs previous close
- Red (`#FF3030`) — price down today
- Amber (default) — market closed or no data

**Market-closed detection (critical):** Uses `rt["timestamp"]` from EODHD real-time endpoint (or `info["regularMarketTime"]` from yfinance). If that timestamp's UTC date ≠ today → market did not trade today → `change_pct = None` → amber. Without this check, EODHD always returns a `close`/`previousClose` even when closed, causing the previous session's move to incorrectly colorize the price. Uses EODHD's own `change_p` field directly (more accurate than computing from close/previousClose).

**Sortable column headers:**
- A muted header row (`pf-sort-header`) sits above the cards, matching the card grid columns exactly.
- Clicking a label sorts all cards by that column descending; clicking again flips to ascending. Active column shows ▼/▲ and lights amber.
- Sort state: `st.session_state.pf_sort_col` (None = original order), `st.session_state.pf_sort_asc`.
- Each column has a hidden `pf-sort-anchor` button; JS in the existing `st.iframe` forwarder binds `.pf-sort-header-cell[data-sortcol]` clicks to fire them.
- Sorting uses already-cached snapshots — no extra API calls.
- Column label "F P/E" renamed to "Forward P/E".

### My Portfolio — IR / Ad Hoc announcement links added to Latest News (2026-08-07)
Each portfolio card's "📰 Latest news" expander only pulled from EODHD `/news` (or yfinance `.news` for Japan/Baltic) — both are third-party aggregators whose most recent item can be days or weeks stale for thinly-covered tickers, which isn't useful for spotting a fresh regulatory disclosure or press release. **First attempt:** added `LLMClient.find_ir_announcements(company_name, ticker, min_items=5)` — used the provider's **native web-search tool** (`_claude_web_search()` for Claude, `_openai_web_search()` for OpenAI) to locate and enumerate the company's own IR page items via search-tool recall. Only `claude`/`openai` were supported; every other provider returned `[]` immediately.

**User re-test exposed this as ineffective (GENDA Inc., 9166.T):** the user compared the app's output against the company's real IR page (`genda.jp/en/ir/news/`, showing items dated up to 2026-08-05) and found the app still only showing a stale 2026-01-20 item — no visible improvement at all. Root cause: (1) this app's `LLM_PROVIDER` production secret has been switched repeatedly (kimi → claude → gemini → xai, see Section 10) with no fixed record of what's active at any given time — if production isn't currently `claude`/`openai`, `find_ir_announcements()` silently returns `[]` on every call with no distinguishable symptom from "found nothing"; (2) even on a supported provider, an LLM's web-search recall of a specific small/foreign-cap company's exact IR article URLs from search snapshots is inherently less reliable than reading the real page.

**Fix (scraper-first rewrite, same day):** added `data_sources/ir_news_scraper.py` — fetches the company's own website directly via `requests` + `BeautifulSoup`/`lxml` (same defensive, never-raise pattern as `insidertrades_scraper.py`). `find_ir_news_page(website)` fetches the homepage, scans `<a href>` tags for one whose href/text combines an IR-ish word and a news-ish word (word-boundary regex `_IR_RE`/`_NEWS_RE` — plain substring matching was rejected during self-review since e.g. `"ir"` as a bare substring false-positives on "direct"/"circle"), preferring links where the IR word is in the href itself; falls back to trying ~24 common path suffixes (`/en/ir/news/`, `/investor-relations/news/`, `/press-releases/`, etc.) directly if the homepage scan finds nothing. `scrape_recent_announcements(ir_url)` parses that page's real anchor tags into `{title, link, date}`, extracting a date from the anchor text or up to 3 ancestor levels via 4 regex date-format patterns (`YYYY.MM.DD`, `DD.MM.YYYY`, `Month DD, YYYY`, `DD Month YYYY`), sorts newest-first, falls back to DOM order (already newest-first on real IR pages) for undated items.

Added `LLMClient.refine_scraped_announcements(raw_items, company_name, ticker, min_items=5)` — unlike `find_ir_announcements()`, this works with **every** provider since it's plain reasoning (`self.generate()`) over already-scraped candidates, not a live web-search tool. It filters the scraped list (which can include nav/footer noise like "Contact Us") down to genuine announcements, but is constrained to reuse only the exact `link` values it was given (validated against `{it.get("link") for it in raw_items}` after parsing) — the model can select or discard, never invent a URL. Falls back to the raw scraped list on any parse/call failure.

`pages/my_portfolio.py`'s `_fetch_ir_announcements()` (`st.cache_data(ttl=21600)`) now runs: scrape the real site (`website` field, added to both `_fetch_snapshot()` and `_snapshot_yf()` from EODHD `General.WebURL` / yfinance `info["website"]`) → LLM-refine the scraped candidates (any provider) → only if there's no website on file or the scrape found nothing, fall back to the original native-web-search `find_ir_announcements()` (Claude/OpenAI only) as a last resort. Rendering (blue `IR` badge block above the existing feed, relabeled "OTHER NEWS" beneath) is unchanged from the first attempt. **General rule: prefer scraping the actual source over LLM web-search recall whenever a concrete site/URL is known — recall-based search is provider-dependent and less reliable for non-mainstream-press companies; use LLM calls for judgment/filtering over real fetched data, not as the primary discovery mechanism.**

### DeepSeek — SWOT Analysis missing from Industry Analysis report (2026-07-01)
When `LLM_PROVIDER=deepseek`, the SWOT Analysis pages were silently absent from Industry Analysis reports. The SWOT is a **separate** `llm.generate_json()` call after the main Porter analysis (see Section 7 "Industry Analysis report"). Root cause: DeepSeek can return SWOT fields as non-string types (nested dicts, lists) or wrap all fields under a `"swot"` top-level key, causing the `.strip()` calls in the field-extraction block of `pages/report_generator.py` to throw `AttributeError`, which was caught by `except Exception` and silently swallowed — leaving `analysis["swot"]` unset and the PDF renderer skipping the SWOT pages. **Fix (2026-07-01):** (1) Added an unwrap step: if the response has a `"swot"` top-level key with a dict value and none of the expected flat keys, use the nested dict instead. (2) Replaced bare `.strip()` with a `_swot_str()` helper that safely coerces `None → ""`, `list → " ".join(...)`, `other → str()`. (3) Added a debug expander showing the raw SWOT response when all fields are empty. If you add more SWOT fields in the future, update `_swot_str()` extractions in the same block.

### DeepSeek — Current News section missing from Investment Memo (2026-07-01)
When `LLM_PROVIDER=deepseek`, the "Current News" section was silently absent from Investment Memo V2 reports. Root cause: `generate_web_news()` in `agents/llm_client.py` only dispatched on `"openai"` and `"claude"` — the `deepseek` branch fell through to `return ""`, so `_news_summary` was always empty and the PDF renderer rendered nothing. OpenAI and Claude have provider-native web search tools; DeepSeek does not. **Fix (2026-07-01):** added `elif self.provider == "deepseek": return self._deepseek_news_summary(company_name, ticker)`. The new `_deepseek_news_summary()` method fetches up to 12 headlines from `NewsAdapter` (NewsAPI.org), then calls DeepSeek's chat API to synthesise the analyst narrative in the same format as the native web-search path. Requires `NEWS_API_KEY` in Streamlit secrets; logs a warning and returns `""` gracefully if absent.

### Hong Kong (HKEX) tickers missing from ticker search boxes (2026-07-09)
Typing "700"/"0700.HK" or a HK company name (e.g. "Tencent") into the report generator or My Portfolio search box returned no suggestions, even though EODHD's `/fundamentals/` endpoint fully supports HKEX. Root cause: EODHD's `/search/` autocomplete endpoint doesn't index HKEX listings — same class of gap as NASDAQ Baltic. `pages/report_generator.py`'s `_smart_search()` has a generic "yfinance search if EODHD returned nothing" fallback, but it's skipped whenever EODHD returns *any* match for the query (even an unrelated one from another exchange), so HK never surfaced. `pages/my_portfolio.py`'s `_ticker_search()` (a separate, duplicated copy of the same logic) has no generic fallback at all — only dedicated Japan/Baltic layers. **Fix (2026-07-09):** added a dedicated Hong Kong layer to both files, mirroring the existing Japan pattern exactly: (1) direct digit-code pattern match (`^0*(\d{1,5})(\.HK)?$` → zero-padded to 4 digits, e.g. "700" → `0700.HK`), resolving the company name via a single `yfinance.Search()` call; (2) a `yfinance.Search(query + " hong kong")` backfill for name searches, filtered to symbols ending in `.HK`. Runs unconditionally (not gated on EODHD's result count), so it always adds HK suggestions alongside whatever EODHD/Japan/Baltic already found. **Note:** `pages/report_generator.py` and `pages/my_portfolio.py` maintain independent copies of this search logic — any future fix to one must be mirrored in the other.

### PDF page header "MCap: 0.00B" on every page (Fisher, Gravity, ValueMeter, Short Interest, Industry Analysis) (2026-07-09)
The page-header MCap figure showed "0.00B" regardless of the company's actual size. Root cause: `CompanyData.market_cap` is stored **in millions** (see `data_sources/base.py` field comment), but the shared `_draw_header()` helper (defined once in `agents/pdf_fisher.py`, imported by `pdf_gravity.py`, `pdf_valuemeter.py`, `pdf_short_interest.py`, and `pdf_industry_analysis.py`) computed `company.market_cap / 1e9` — treating it as raw currency units. For any realistic market cap this rounds to "0.00B" (e.g. a €2.7B company has `market_cap = 2700`, so `2700 / 1e9 ≈ 0.0000027`). The body-table helper `_b()` used elsewhere in the same reports correctly divides by `1000` (millions → billions), which is why the body's Market Cap figure was always right while the header was always wrong — this bug has likely been present in all five report types since they were built, not just Industry Analysis. **Fix (2026-07-09):** changed the divisor from `1e9` to `1000` in all four `_draw_header()`/`mcap_str` call sites (`agents/pdf_fisher.py`, `agents/pdf_gravity.py`, `agents/pdf_valuemeter.py`, `agents/pdf_short_interest.py`). `pdf_industry_analysis.py` imports `_draw_header` from `pdf_fisher.py`, so it's fixed automatically.

### Industry Analysis — Competitive Advantage summary/detail placeholders + detail section removed (2026-07-09)
The "Competitive Advantage — Summary" and "Competitive Advantage — Detailed Assessment" sections often rendered fallback placeholder text ("...not available.") instead of real content. Root cause: in `models/industry_analysis.py`'s `_CACHEABLE` prompt schema, the `competitive_advantage_*` fields were requested **after** the `forces` field — 5 force objects each with a 250-400 word `state_2026` + 150-250 word `historical_evolution` + sources (~3,000+ words total). With `max_tokens=9500` for the single main LLM call (`pages/report_generator.py` industry_analysis dispatch), a long/verbose response for the forces section could exhaust the token budget before the model ever reached the competitive-advantage fields, silently truncating the JSON and leaving `_validate_analysis()` in `models/industry_analysis.py` to fall back to its placeholder defaults — with no error surfaced, since the existing truncation diagnostic (`_ia_filled`) only checks that ≥3 forces are populated, not the competitive-advantage fields. **Fix (2026-07-09):** (1) Reordered the JSON schema in `_CACHEABLE` so `competitive_advantage_size` / `_evolution` / `_summary` / `_sources` are requested immediately after `strategic_implications` and **before** `forces`, with an explicit instruction telling the model to emit them in that order — this protects the competitive-advantage content from truncation even if the model runs out of budget deep in the forces section. (2) Removed the `competitive_advantage_detail` field entirely — from the prompt schema, `_validate_analysis()`'s defaults, `build_swot_prompt()`'s context block, and the PDF's "Competitive Advantage — Detailed Assessment" page (`agents/pdf_industry_analysis.py`) — per explicit request, since it was deemed redundant with the summary. Expanded `competitive_advantage_summary`'s target from 150-250 to 250-350 words to absorb the Porter's-framework reasoning (cost leadership/differentiation/focus, durability) that the detail section used to carry, and moved `competitive_advantage_sources` to render under the Summary section instead of the removed Detail section. Also updated `frameworks/industry_analysis.json` (`system_prompt`, `output_schema`, `report_sections`) to match, since `_load_system_prompt()` reads the live system prompt from this file.

### Fisher Alternatives + Peers — LLM peer suggestion silently returning empty (2026-07-13)
Reports with no user-supplied peers rendered "No peers were analysed for this report" and showed the warning "⚠ LLM could not suggest peers automatically", even though the subject clearly has viable public peers (e.g. RMV.L — Rightmove — has CoStar Group, Auto Trader, Scout24, REA Group, Zillow as obvious comparables). Root cause: `suggest_peers()` in `models/fisher_peers.py` called `llm.generate_json(..., max_tokens=500)` — the tightest budget of any LLM call in the whole app (every other call is ≥1200, most are 4500-9500). A 4-6 peer JSON array with a short rationale sentence per peer runs ~350-600+ output tokens on its own, and more verbose/reasoning-oriented models (e.g. Opus) can also spend part of the budget on preamble text despite the "no commentary" instruction. When generation got cut off before any `{` was ever emitted, `generate_json()`'s truncation-repair had nothing to salvage and returned `{}`, which `suggest_peers()` treated identically to "the LLM genuinely found no peers" — no error surfaced, since the failure path doesn't distinguish a parse failure from a real empty response. **Fix (2026-07-13):** raised `max_tokens` from 500 to 1500 in `suggest_peers()`. Also added `logger.warning(...)` calls that log `llm.last_raw_response` whenever the response isn't a dict or has no usable `peers` list, so a repeat of this failure mode (or a genuinely empty LLM response) is distinguishable in logs instead of being silently swallowed.

### Kimi (Moonshot AI) — 401 Invalid Authentication when set via `LLM_PROVIDER=openai` + `OPENAI_BASE_URL` (2026-07-23)
User tried to switch the main LLM to Kimi by setting `LLM_PROVIDER=openai`, `LLM_MODEL=kimi-k3`, `OPENAI_API_KEY=<moonshot key>`, and a new `OPENAI_BASE_URL` secret pointing at `https://api.moonshot.ai/v1`, expecting the OpenAI client to pick up the custom endpoint. Got `openai.AuthenticationError: 401 Invalid Authentication` in `_openai()`. Root cause (two independent problems): (1) `config.py` has never read an `OPENAI_BASE_URL` env var — it doesn't exist anywhere in the codebase — so `LLM_PROVIDER=openai` always instantiates `OpenAI(api_key=OPENAI_API_KEY)` with **no** `base_url` override, meaning every "openai" request hits `api.openai.com` regardless of any `OPENAI_BASE_URL` secret set in Streamlit. A Moonshot key sent to `api.openai.com` is rejected with 401. (2) Separately, the user's `OPENAI_API_KEY` secret was left as the literal placeholder string `"your_moonshot_api_key"` rather than the real key. **Fix (2026-07-23):** added a proper `kimi` provider, mirroring the existing `deepseek` pattern (which already proved custom OpenAI-compatible endpoints work via `_openai(base_url=..., api_key=...)`): `MOONSHOT_API_KEY` added to `config.py`; `LLMClient.generate()`, `_api_key()`, `check_configured()`, and `generate_web_news()` (news falls back to NewsAdapter headlines + Moonshot synthesis, same as DeepSeek, since Moonshot has no native web-search tool) all gained a `kimi` branch that hardcodes `base_url="https://api.moonshot.ai/v1"`. Correct Streamlit secrets going forward: `LLM_PROVIDER="kimi"`, `LLM_MODEL="<moonshot model id>"`, `MOONSHOT_API_KEY="<real key>"` — no `OPENAI_BASE_URL`/`OPENAI_API_KEY` involved at all. **General rule: `LLM_PROVIDER=openai` is reserved for real OpenAI and never respects a custom base URL — any new OpenAI-compatible provider needs its own `elif self.provider == "<name>"` branch, not an env-var override of the `openai` branch.**

### Fisher (+ Peers) — crash `TypeError` in `_validate_analysis` on thin-data tickers (e.g. ALLEG.AS) (2026-07-13)
Report generation crashed with a traceback bottoming out at `a["fisher_total_score"] = sum(p.get("score", 3) for p in full_points)` in `models/fisher.py`. Preceded in the logs by `WARNING:data_sources.eodhd_only_builder:[eodhd-only] No fundamentals for ALLEG.AS` — when EODHD has no fundamentals for a ticker, the Fisher prompt is data-starved, and the LLM is more likely to return `"score": null` (or omit the key with the field still present as `None`) for one or more of the 15 points instead of a normal 1-5 int. `p.get("score", 3)` only substitutes the default `3` when the `"score"` key is **missing** from the dict — when the key exists but its value is `None` (or a string/float), `.get()` returns that value unchanged, so `sum()` crashed on `int + None`. The same unguarded pattern also fed the `assessment` fallback (`s = p.get("score", 3)` then `s >= 4`), which would raise the same class of error if `assessment` was also missing. **Fix (2026-07-13):** in the `fisher_points` loop of `_validate_analysis()`, every point's `score` is now coerced through `int(round(float(p.get("score"))))` wrapped in `try/except (TypeError, ValueError)` (defaulting to `3` on failure) and clamped to `1-5`, before it's used for the `assessment` fallback or summed into `fisher_total_score`. `models/gravity.py` already guarded this correctly (`int(d.get("score", 3) or 3)`) — `models/fisher_peers.py`'s peer-batch validator was also already safe; only the main `fisher.py` single-company validator had the gap.

### Kimi (Moonshot AI) — max_tokens too tight + silent Streamlit Cloud worker kill + 429 overload (2026-07-23)
Follow-up to the `kimi` provider work above, across three rounds of live testing on the Investment Memo (`overview_v2`) report:
1. **Empty analysis text ("Analysis not available." etc.), long generation time.** `kimi-k3` appears to be a reasoning/"thinking" model that spends a large, variable share of its completion-token budget on hidden reasoning before writing the final JSON answer. At the existing per-call `max_tokens` values (tuned for Claude/GPT-4o), the model was getting cut off before emitting valid JSON, so `generate_json()`'s parse fallback chain exhausted and returned `{}`, which every `_validate_analysis()` filled with its default placeholder text — no error surfaced. **Fix:** added `KIMI_MAX_TOKENS_MULTIPLIER` (default `2.5`) and `KIMI_MAX_TOKENS_CAP` (default `16000`) to `config.py`. In `LLMClient.generate()`'s `kimi` branch only, every caller's requested `max_tokens` is scaled by the multiplier (capped) before the call — this benefits every report type automatically (including Industry Analysis) without touching the `claude`/`openai`/`deepseek` code paths at all.
2. **App "collapsed" with zero traceback** after raising the token cap. This was a Streamlit Cloud platform-level worker kill, not a Python exception: the `openai` SDK's client had no `timeout` set anywhere in this codebase, so an unbounded blocking call on a slow "thinking" model with a large token budget could run long enough for the hosting platform to kill the whole process outright, leaving no log output at all. **Fix:** added `KIMI_REQUEST_TIMEOUT_SECONDS` (default `240`) to `config.py`, threaded through a new `request_timeout` parameter on `_openai()` (only passed by the `kimi` branch), passed to the `OpenAI(...)` client constructor as `timeout=`. This converts a silent platform kill into a normal catchable Python exception. Also lowered `KIMI_MAX_TOKENS_CAP` from an initial 32000 to 16000 as a second line of defense against very long calls. **General rule: any new provider branch added to `_openai()` that might run long (reasoning models, large max_tokens) should pass an explicit `request_timeout` — the SDK default is effectively unbounded.**
3. **`openai.RateLimitError: 429 "The engine is currently overloaded, please try again later"`** — this was the *expected proof the timeout fix worked*: a real, catchable, loggable exception instead of a silent collapse. This is a transient capacity issue on Moonshot's infrastructure, not an app bug. **Fix:** `_openai()`'s retry loop now catches `RateLimitError` specifically and retries up to 3 times with exponential backoff (10s, 20s, 40s) before giving up and raising. This is implemented generically in the shared `_openai()` method (so `openai`/`deepseek`/`kimi` all benefit), which is safe because it only changes behavior in an already-broken failure path — everything that previously raised immediately on a 429 now gets ~70s of bounded retry headroom first.

### Kimi abandoned, reverted to Claude; Gemini provider added (2026-07-23)
Even with the retry-with-backoff fix above, `kimi-k3` kept returning `429 engine_overloaded_error` on live Investment Memo test runs — a persistent capacity problem on Moonshot's infrastructure at the time, not something fixable from the app side. **Decision:** stopped testing Kimi and reverted Streamlit secrets to `LLM_PROVIDER="claude"`, `LLM_MODEL="claude-opus-4-8"`. No code changes were needed for the revert — the `claude` branch of `llm_client.py` (`_claude()`) was never touched by any of the Kimi work, since every Kimi-specific change lived inside `elif self.provider == "kimi":` branches or `KIMI_*` config constants. The `kimi` provider itself was left in place (not removed) in case Moonshot's capacity improves later.

Separately, added a `gemini` provider to test `gemini-2.5-pro`. Google exposes an OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`), so this reuses `_openai()` with a custom `base_url`, exactly the DeepSeek/Kimi pattern — no new HTTP client code. `GEMINI_API_KEY` added to `config.py`; `LLMClient.generate()`, `_api_key()`, `check_configured()`, `generate_web_news()` (falls back to the same NewsAdapter+synthesis path as DeepSeek/Kimi, since Gemini has no native web-search tool on this OpenAI-compatible surface), and `_openai()`'s missing-key error hint all gained a `gemini` branch. Since Gemini 2.5 Pro is also a "thinking" model with hidden reasoning tokens — the same failure class that caused the Kimi truncation and silent-worker-kill bugs above — `GEMINI_MAX_TOKENS_MULTIPLIER` (default `2.0`), `GEMINI_MAX_TOKENS_CAP` (default `16000`), and `GEMINI_REQUEST_TIMEOUT_SECONDS` (default `240`) were added pre-emptively, mirroring the `KIMI_*` constants, rather than waiting to rediscover the same bugs via a live test. Correct Streamlit secrets: `LLM_PROVIDER="gemini"`, `LLM_MODEL="gemini-2.5-pro"`, `GEMINI_API_KEY="<key>"`. Not yet live-tested as of this writing — if the actual failure modes differ from Kimi's, tune the `GEMINI_*` constants rather than touching other providers' code paths.

### Investment Memo V2 — Current News section truncated mid-sentence on Gemini (2026-07-23)
After a successful live test with `gemini-3.1-pro-preview` (see above), the "Current News" section on page 2 of the Investment Memo rendered only one theme heading followed by a paragraph that cut off mid-sentence, with the rest of the page blank. Root cause: `generate_web_news()` routes DeepSeek/Kimi/Gemini through `_deepseek_news_summary()` (NewsAdapter headlines → LLM synthesis), which calls `self._openai(...)` **directly** with a hardcoded `max_tokens=1500` and no `request_timeout` — it never goes through `generate()`'s provider-dispatch branches, so the `KIMI_MAX_TOKENS_MULTIPLIER`/`GEMINI_MAX_TOKENS_MULTIPLIER` scaling (and the `*_REQUEST_TIMEOUT_SECONDS` values) that exist specifically to give "thinking" models enough budget for hidden reasoning were silently bypassed for this one call site. At a flat, unscaled 1500-token ceiling, `gemini-3.1-pro-preview` burned most of the budget on hidden reasoning and got cut off partway through the visible narrative — with no error, since the partial string is still a valid non-empty return value. **Fix (2026-07-23):** `_deepseek_news_summary()` now branches on `self.provider` the same way `generate()` does — for `kimi` and `gemini` it scales the 1500-token base by the matching `*_MAX_TOKENS_MULTIPLIER` (capped at `*_MAX_TOKENS_CAP`) and passes the matching `*_REQUEST_TIMEOUT_SECONDS` as `request_timeout`; `deepseek` (not a reasoning model) keeps the original flat 1500/no-timeout behavior. **General rule: any call site that invokes `_openai()` directly instead of going through `generate()` must re-apply the Kimi/Gemini token-scaling and timeout logic itself — it is not global to `_openai()`, only to `generate()`'s dispatch branches.**

Separately noticed while investigating: the page-4 "EODHD Data Provenance" table always listed "News headlines: Not used in this report — n/a" regardless of whether the Current News section actually rendered — this row (and the yfinance/Japan-path equivalent) was a static hardcoded list with no awareness of the `news_summary` dict actually passed to `render()`. **Fix:** `_page4_provenance()` now accepts `news_summary` (threaded from `render()`'s existing `_page1(..., news_summary=news_summary)` call) and sets the "News headlines" row dynamically based on whether `news_summary.get("narrative")` is non-empty.

### Earnings Quality — every company scored "n/a" / "Insufficient data" on Gemini (2026-07-23)
The Earnings Quality report generated cleanly with `gemini-3.1-pro-preview` but every company (subject + all 6 peers) came back with `Score: None/100`, `Grade: N/A`, and "Insufficient data was returned by the model for this company." — i.e. the whole JSON response failed to parse, not just individual fields. Root cause: `pages/report_generator.py`'s `earnings_quality` dispatch calls `llm.generate_json(dynamic_prompt, SYS, max_tokens=16000, ...)` — by far the largest `max_tokens` request anywhere in the app (next highest is 12000 for the Investment Memo main call), because it scores the subject plus up to 6 peers (7 companies × 7 subscores + 3 strengths + 3 risks + up to 5 red flags + a 200-300 word explanation each) in one combined call. `GEMINI_MAX_TOKENS_CAP` was `16000` — exactly equal to this call's base request — so `generate()`'s gemini branch computed `min(16000 * 2.0, 16000) = 16000`: the multiplier was silently a no-op for this one call site only (every other call in the app requests ≤12000, so `base * 2.0` stayed under the old cap and the multiplier worked as intended everywhere else). With no extra headroom, `gemini-3.1-pro-preview` spent its hidden reasoning budget scoring 7 companies against 7 forensic-accounting dimensions each and never emitted a complete JSON object, so `generate_json()`'s parse fallback chain exhausted and returned `{}`. **Fix (2026-07-23):** two changes. (1) Reduced the Earnings Quality peer cap from 6 to 3 (`_slots_remaining = 3 - len(_user_peer_tickers)`, both `[:3]` slices in the dispatch block, `max 3 peers` in `models/earnings_quality.py`'s cacheable prompt and docstring, and the framework description in `frameworks/earnings_quality.json`) — per user request, this also roughly halves the per-run LLM cost/time since it was analyzing 7 companies where 4 is now the max. A `st.info()` now tells the user if more than 3 peers were supplied and only the first 3 were used. (2) Raised `GEMINI_MAX_TOKENS_CAP` from `16000` to `24000` in `config.py` so the multiplier has real headroom above the largest existing base request instead of being silently defeated at exactly the old cap. **General rule: `GEMINI_MAX_TOKENS_CAP`/`KIMI_MAX_TOKENS_CAP` must stay comfortably above `(largest caller's base max_tokens)` — if any call site's own requested `max_tokens` is already ≥ `cap / multiplier`, `min(base * multiplier, cap)` collapses to the unscaled cap and that call gets zero benefit from the reasoning-model headroom the multiplier exists to provide. Check this whenever adding a new large `max_tokens` call for a report type.**

### Earnings Quality — added per-dimension scoring methodology to page 1 (2026-07-23)
The PDF only showed a compact `Weighting: Cash Conversion 25% · Accrual Quality 20% · ...` line — a reader had no way to see *what inputs* drove each subscore without reading the source code. **Fix (2026-07-23):** added `EQ_DIMENSION_DESCRIPTIONS` to `models/earnings_quality.py` — one plain-English sentence per dimension listing the specific ratios/metrics the LLM is instructed to weigh (e.g. `cash_conversion`: "Operating Cash Flow / Net Income, Free Cash Flow / Net Income, the multi-year cash conversion trend, and CFO consistency"), adapted directly from the numbered dimension list already in `_EQ_CACHEABLE` (the LLM prompt) so the PDF text and the actual scoring instructions never drift apart. Rendered via a new `_methodology_block()` in `agents/pdf_earnings_quality.py`, called from `_page1()` right after the existing weighting/disclaimer paragraphs (i.e. under the ranking table, on page 1) — a "Methodology — How Each Dimension Is Scored" section title followed by 7 numbered paragraphs (`"1. Cash Conversion (25%): <description>"`, etc.), each pulling its weight from `EQ_WEIGHTS` and label from `EQ_DIMENSION_LABELS` so all three stay in sync automatically if a weight ever changes. **General rule: if `_EQ_CACHEABLE`'s numbered dimension descriptions are ever reworded, update the matching entry in `EQ_DIMENSION_DESCRIPTIONS` too — the PDF text is a hand-adapted copy, not a shared string.**

---

### Fisher Alternatives / ValueMeter hidden from the Valuation Models picker (2026-07-23)
`fisher` (Fisher Alternatives) and `valuemeter` (ValueMeter) need significant adjustments before they're ready for use again, so they're temporarily hidden from the front-page model picker per user request. **Fix:** `_HIDDEN_FW_IDS = {"fisher", "valuemeter"}` added in `pages/report_generator.py` (near `_BUILTIN_IDS`), filtered out of `_fw_options` in the Report Framework picker block. The framework JSON files, PDF renderers, and prompt builders are untouched — Studio (`pages/model_editing.py`) calls `FrameworkManager.list()` directly and is unaffected, so both remain fully editable there. **To re-enable either model: just remove its id from `_HIDDEN_FW_IDS`.**

Also relocated **Earnings Quality Score** (`earnings_quality`) to sort right after **Fisher Alternatives + Peers** (`fisher_peers`) in the picker list. `_build_report_types()` normally orders frameworks via `FrameworkManager.list()` (persisted user order from `data/framework_order.json`, falling back to alphabetical-by-name for built-ins). `data/` is gitignored (it holds per-instance runtime state like `portfolio.json`), so writing a `framework_order.json` locally would not survive a push/redeploy on Streamlit Cloud. **Fix:** the reorder is done in code instead — after building the `result` dict, `earnings_quality` is popped and reinserted immediately after `fisher_peers` — so it's part of the committed source and survives every deploy. **General rule: any front-page model ordering/visibility change must live in code (or be explicitly justified as instance-local), not in `data/framework_order.json` — that file does not survive a Streamlit Cloud redeploy.**

---

### Industry Analysis — entire Porter's 5 Forces section rendered "not available" on Gemini (2026-07-23)
User ran Industry Analysis on HIGH.DE (Cantourage Group SE, a thinly-traded German cannabis micro-cap) with `LLM_PROVIDER=gemini` / `gemini-3.1-pro-preview`. The PDF's Executive Summary, 5-Forces Scorecard takeaways, Key Structural Shifts, Strategic Implications, all 5 individual force pages (Current State + Historical Evolution), and Competitive Advantage Summary all rendered generic "not available" placeholder text — while the separate SWOT Analysis call (page 10) rendered fully and correctly, matching the documented pattern that SWOT is a smaller, independent `llm.generate_json()` call made after the main Porter's-analysis call. The app log showed `ERROR:agents.llm_client:[LLMClient] JSON parse failed` on a response that was well-formed JSON up to the point shown, meaning the response was truncated mid-generation and every one of `generate_json()`'s fallback parse strategies — including its dedicated truncated-JSON repair step — failed to salvage it, so `generate_json()` returned `{}` and `_validate_analysis()` in `models/industry_analysis.py` filled every field with its placeholder default.

Two independent bugs contributed:
1. **Insufficient token headroom for a reasoning model on a long, multi-section prompt.** The Industry Analysis main call (`pages/report_generator.py`'s `industry_analysis` dispatch) requested `max_tokens=9500` — scaled by `GEMINI_MAX_TOKENS_MULTIPLIER` (2.0) to 19,000, well under `GEMINI_MAX_TOKENS_CAP` (24,000). The prompt schema (`models/industry_analysis.py`'s `_CACHEABLE`) asks for ~2,000-3,000 words across 5 forces (`state_2026` + `historical_evolution` each) plus a 350-500 word executive summary, 150-250 word strategic implications, and 250-350 word competitive advantage summary — comparable in scope to the Earnings Quality call (which already gets scaled to the full 24,000-token cap). `gemini-3.1-pro-preview` is a "thinking" model that spends a large, variable share of its completion budget on hidden reasoning before writing the visible answer (the same failure class documented above for Earnings Quality and for Kimi) — 19,000 tokens left too little room for a strategic-reasoning-heavy task like Porter's 5 Forces. **Fix:** raised the call's own `max_tokens` from `9500` to `13000` (both the adversarial and non-adversarial branches) so the Gemini-scaled budget now pins at the same 24,000-token ceiling Earnings Quality already gets, rather than stopping short at 19,000.
2. **A real bug in the JSON truncation-repair helper meant even a truncated response couldn't be salvaged.** `_try_repair_truncated_json()` in `agents/llm_client.py` is supposed to be the last-resort fallback for exactly this situation — trim a truncated response back to the last fully-completed field and close the open braces/brackets. Its char-by-char walk already tracks `last_safe`, the index of the last comma that occurs **outside** a string (i.e., a genuine JSON structural boundary) — but that variable was computed and then never used. Instead, the code fell back to `candidate.rfind(",", 0, last_quote)`, a raw substring search with no awareness of whether the comma is inside string content. Industry Analysis's output is long, comma-heavy analytical prose ("Germany, France, and Italy…", "revenue, EBIT, and net income…") — exactly the case where a raw `rfind(",")` lands on a comma inside an already-complete field's text instead of a real JSON boundary, corrupting that earlier field when the code trims to it. This produced JSON that still failed to parse, so the repair silently returned `None` and the caller fell through to the fully-empty `{}` result. **Fix:** replaced the buggy `rfind(",", 0, last_quote)` lookup with the already-tracked, string-aware `last_safe` index. **General rule: `_try_repair_truncated_json()` is the safety net for every long-form JSON call in the app (Industry Analysis, Earnings Quality, Overview, Gravity, etc.) — any future change to it should be tested against verbose, comma-rich prose fields specifically, since that's the content type most likely to expose string-vs-structural-boundary bugs.**

Separately, also fixed a genuine data bug noticed in the same report: **page 9's Market Snapshot showed "P/E (TTM): 0.0x" and "EV/EBITDA: 0.0x"** instead of "n/a" (while EV/EBIT correctly showed "n/a"). Root cause: `data_sources/eodhd_only_builder.py`'s `_f()` helper parses EODHD's raw field values into floats, and EODHD returns the literal string `"0"` for valuation multiples it can't compute (e.g. P/E or EV/EBITDA for a loss-making/pre-profit company like Cantourage) instead of `null` — `_f("0")` faithfully returns `0.0`, which is a legitimate-looking (non-`None`) value that the PDF's `_x()` formatter renders as "0.0x" rather than "n/a". **Fix:** added `_fz()` (float-or-none, treating an exact `0` as missing) and switched `pe_ratio`, `forward_pe`, `price_to_book`, `price_to_sales`, `peg_ratio`, `ev_sales`, and `ev_ebitda` in `eodhd_only_builder.py` to use it instead of `_f()` — these are all ratio fields where a real 0.0 is never a legitimate value. **General rule: any new EODHD valuation-multiple field added to `eodhd_only_builder.py` should use `_fz()`, not `_f()` — plain `_f()` is still correct for fields where 0 is a real, meaningful value (e.g. share counts, percentages, revenue).**

The Alpha Vantage failure warning and the `/upgrades-downgrades` 404 the user also pasted are expected, benign fallback noise for a thinly-covered ticker like HIGH.DE — `eodhd_all_in_one.py`'s endpoint bundle already wraps every fetch in try/except specifically so a single missing endpoint (most micro-caps have no analyst upgrade/downgrade history) doesn't break the report; no fix needed there.

### Industry Analysis — Porter's 5 Forces still blank on a Japan ticker (GENDA / 9166.T) after the HIGH.DE fix — "cap collapse" recurrence + added retry (2026-07-23)
Follow-up to the HIGH.DE fix directly above. User ran Industry Analysis on GENDA Inc. (9166.T, Japan/TSE — the `_is_japan` code path, which skips `fetch_company_data_eodhd_only()` and uses `_JAPAN_BUNDLE` + the yfinance-sourced `company` object instead) with Gemini. Same overall symptom as HIGH.DE (all Porter content — Executive Summary, scorecard, structural shifts, strategic implications, all 5 force pages, competitive advantage summary — rendered "not available" placeholders while SWOT and the financial-data page rendered correctly), but *worse*: even the short `one_line_takeaway` scorecard fields were completely blank, and every enum field showed its exact `_validate_analysis()` default (`"Moderate"` intensity, `"Neutral"` attractiveness, `"Stable"` trajectory, `"Small"` advantage size) — the unambiguous signature of `raw_analysis` being fully `{}`, i.e. the LLM emitted zero usable JSON, not a partial/truncated response the repair helper could salvage.

Investigated whether this was Japan-specific (thinner subject-data block, since `_JAPAN_BUNDLE` has no EODHD news/sentiment/insider/ratings data) — `models/_eodhd_context.py`'s `build_eodhd_context()` degrades gracefully section-by-section when `bundle` is near-empty (each block function returns `""` for missing data and empty sections are dropped, no crash), so the subject block still carries the same real financials that populated page 9 correctly. Ruled out as the primary cause.

**Actual root cause: the exact "cap collapse" bug documented in the HIGH.DE fix above recurred for a second call site.** Industry Analysis's own `max_tokens` request is `13000`. `GEMINI_MAX_TOKENS_CAP` was `24000` (raised from 16000 for Earnings Quality's 16000-token base call). `13000 * GEMINI_MAX_TOKENS_MULTIPLIER (2.0) = 26000`, which exceeds `24000` — so `generate()`'s `min(base * multiplier, cap)` clipped Industry Analysis to the flat 24000 ceiling instead of the intended 26000, losing real headroom. For a well-known, well-covered subject (large-cap German company) this clipped budget was still enough; for a subject the model has less confident training data about and needs to reason harder over (GENDA — a small-cap Japanese arcade/anime-IP company), the model burned the entire clipped budget on hidden reasoning and emitted no visible answer at all. **Fix (`config.py`):** raised `GEMINI_MAX_TOKENS_CAP` from `24000` to `36000` — comfortably above both `16000*2.0=32000` (Earnings Quality) and `13000*2.0=26000` (Industry Analysis), so neither caller's multiplier collapses to a flat cap. Also raised `GEMINI_REQUEST_TIMEOUT_SECONDS` from `240` to `300` to match the larger possible completion size.

**Second, complementary fix — automatic one-retry on empty response (`pages/report_generator.py`, `industry_analysis` dispatch):** reasoning-token consumption is stochastic per call even at a fixed budget, so no static cap can *guarantee* a non-empty answer every time. The main LLM call (and its "is this response usable?" check, `_ia_is_filled()` — requires ≥3 forces with a `state_2026` longer than 80 chars) were factored into two small closures, `_ia_run_main_call()` and `_ia_is_filled()`. If the first attempt's response isn't filled, the code now retries the SAME call once (same `max_tokens`, same prompt) before falling through to the existing empty-response diagnostics (`st.error` + raw-response debug expander) and `_validate_analysis()`'s placeholder defaults. A second sampling attempt frequently succeeds where the first produced nothing, at the cost of one extra LLM call only in the failure case. **General rule: `GEMINI_MAX_TOKENS_CAP`/`KIMI_MAX_TOKENS_CAP` must be re-checked (not just re-verified once) every time ANY call site's own `max_tokens` changes — the cap has to stay above `(largest current base max_tokens) * multiplier` for every caller simultaneously, not just the one that most recently triggered the bug. When adding a new large `max_tokens` call, also consider whether the caller should have a retry-on-empty safety net if it's on a code path (like Japan/thin-training-data subjects) more likely to make a reasoning model spend its entire budget without emitting an answer.**

### Industry Analysis — Porter's 5 Forces STILL blank on a third ticker (Fujitsu / 6702.T) after the cap-raise-to-36000 + retry fix — root cause was never the token ceiling (2026-07-23)
Directly contradicting the two fixes above: the user re-ran Industry Analysis on Fujitsu Limited (6702.T, Japan, Gemini) and got the exact same failure signature as GENDA — every Porter's-5-Forces field, the executive summary, strategic implications, and the competitive-advantage summary all rendered `_validate_analysis()`'s placeholder defaults, while SWOT (page 10) and the financial data page rendered perfectly, confirming this was `raw_analysis` coming back essentially `{}` again, not a data-availability problem. The user pushed back directly on the token-ceiling theory: "I do not think that max token number is the problem, as we did not have the same issue with the claude LLM model" — and proposed shrinking the prompt's own word-count target as the real fix.

That pushback was correct. **Raising `GEMINI_MAX_TOKENS_CAP` was treating the symptom, not the cause.** The actual mechanism: `gemini-3.1-pro-preview` is a "thinking" model that spends a variable, model-controlled share of its completion budget on *hidden* reasoning tokens before it ever writes visible JSON — and that share is not capped by `max_tokens` at all, it's a separate internal allocation the model decides on its own. A bigger `max_tokens` ceiling only gives the model more room to spend on invisible reasoning; it does nothing to stop the model from occasionally spending the *entire* budget there for a demanding, multi-section analytical task (5 forces × current-state + historical-evolution + a long executive summary + a competitive-advantage assessment, ~2,000-3,000 words of required visible output), especially for a subject the model has to reason harder about. This is exactly why Claude never hit this: Claude's `max_tokens` governs its actual visible+tool-use output directly, with no separate hidden-reasoning budget carved out of the same ceiling on this codepath.

**Fix — two complementary changes, both attacking the real mechanism instead of the ceiling:**
1. **Cut the prompt's own required output roughly in half**, per the user's direct suggestion, in `models/industry_analysis.py` (`_CACHEABLE`, `_SYSTEM_PROMPT_FALLBACK`, `_industry_dynamic_prompt()`) **and** `frameworks/industry_analysis.json` (`system_prompt`, `output_schema` descriptions, top-level `description` — this file is what `_load_system_prompt()` actually reads at runtime, so both copies must be edited together or they drift, same rule as the earlier Competitive-Advantage-detail removal). New targets: executive summary 220-300 words (was 350-500), each force's `state_2026` 150-220 words (was 250-400), `historical_evolution` 80-130 words (was 150-250), strategic implications 100-150 words (was 150-250), competitive advantage summary 200-280 words (was 250-350), total Porter-5-Forces text 1,300-1,700 words (was 2,000-3,000). A shorter required visible-output length means less of the total token budget needs to remain after reasoning, directly reducing the odds that a bad reasoning-token draw leaves zero budget for the answer.
2. **Added direct hidden-reasoning-budget control**, since capping required *output* only reduces the odds, it doesn't eliminate the mechanism. Google's Gemini OpenAI-compatible endpoint (the same `https://generativelanguage.googleapis.com/v1beta/openai/` surface this app already calls via `_openai()`) accepts the standard `reasoning_effort` parameter (`"low"` / `"medium"` / `"high"`, same name OpenAI uses for its o-series models) and maps it to an internal thinking-token budget — this caps the *actual* mechanism that was causing the failures, rather than hoping a bigger ceiling gives the model enough leftover room. Added `GEMINI_REASONING_EFFORT` (default `"low"`) to `config.py`. Threaded a new `reasoning_effort: Optional[str]` parameter through `_openai()` in `agents/llm_client.py` (added to `kwargs` only when truthy), passed from `generate()`'s `gemini` branch and from `_deepseek_news_summary()`'s `gemini` branch (the Current News fallback path, which also calls `_openai()` directly and needed the same treatment the token-multiplier/timeout got earlier). The `_openai()` retry loop's unsupported-param handling (which already drops `temperature` if a model rejects it) gained a matching branch that drops `reasoning_effort` and retries if any provider/model rejects the param, so this is safe even if a future non-Gemini caller accidentally sets it.

Left `GEMINI_MAX_TOKENS_CAP` at `36000` unchanged — more ceiling headroom is still harmless now that hidden-reasoning consumption is directly bounded by `reasoning_effort="low"`, it just isn't the load-bearing fix anymore. **General rule: for any "thinking"/reasoning-model provider, prefer capping reasoning effort directly (`reasoning_effort` or the provider's equivalent thinking-budget parameter) over indefinitely raising `max_tokens` — a token ceiling only bounds the *sum* of hidden reasoning + visible output, it does not stop the model from allocating an unpredictable, sometimes-total share of that sum to the invisible part. If a future report type shows the same "empty JSON, placeholder defaults everywhere, SWOT/other-small-calls still work" signature on Gemini/Kimi, check `reasoning_effort` and prompt length before touching the token cap again.**

**Confirmed fixed (2026-07-23):** live-tested by the user after deploy — Industry Analysis on Gemini now returns a complete, populated report. Both changes (reasoning_effort="low" + shorter word-count targets) verified working together; no further action needed unless a similar empty-JSON signature resurfaces on a different report type or provider.

### Industry Analysis — word-count targets raised back up, 1,300-1,700 → 1,700-2,200 (2026-07-23)
With the Gemini fix confirmed working, the user found the resulting report too compact and asked to raise the target length — this is explicitly the **final** length adjustment to this model per the user's request. Raised proportionally (~+30%) across all three source-of-truth locations, keeping totals internally consistent: executive summary 220-300 → 280-380 words, each force's `state_2026` 150-220 → 200-260 words, `historical_evolution` 80-130 → 140-180 words, strategic implications 100-150 → 130-190 words, competitive advantage summary 200-280 → 260-350 words, total Porter-5-Forces text 1,300-1,700 → 1,700-2,200 words (5 × (200-260 + 140-180) = 1,700-2,200, exact). Edited in `models/industry_analysis.py` (docstring, `_SYSTEM_PROMPT_FALLBACK`, `_CACHEABLE` target-lengths block and per-field descriptions, `_industry_dynamic_prompt()`'s reminder text), `frameworks/industry_analysis.json` (the live runtime `system_prompt`/`output_schema`/`description` — same "must edit both copies" rule as every prior length change to this report), and `pages/report_generator.py`'s UI progress text/comment.

Also raised the main call's own `max_tokens` from `13000` to `17000` (both adversarial and non-adversarial `_ia_run_main_call()` branches) to keep headroom proportional to the larger required output — `17000 * GEMINI_MAX_TOKENS_MULTIPLIER (2.0) = 34000`, still safely under `GEMINI_MAX_TOKENS_CAP` (36000), so the "cap collapse" bug documented above does not recur. **General rule: any future change to this report's target word count must scale `max_tokens=13000`/now `17000` proportionally too — leaving it flat while lengthening required output shrinks the reasoning-model headroom margin and can reintroduce the empty-JSON failure documented above.**

---

## 8. Report Types

### Built-in Reports (hardcoded Python renderers)

#### Overview (`overview`)
3-page investment memo. LLM generates: Investment Snapshot (~900 words), Bull Case (~500 words), Bear Case (~500 words), Recommendation + rationale, suggested peers, fun facts. Checklist of 7 criteria computed from data (no LLM). Peer comparison table fetches live data for up to 6 peers.
- Model: `models/overview.py`
- PDF: `agents/pdf_overview.py`
- LLM output: `snapshot`, `fun_facts`, `bull_case`, `bear_case`, `recommendation`, `recommendation_rationale`, `suggested_peers`
- Token cost: ~5000 max_tokens output

#### Fisher (`fisher`)
Philip Fisher "15 Questions" qualitative analysis.
- Model: `models/fisher.py`
- PDF: `agents/pdf_fisher.py`

#### Gravity Score (`gravity`)
Multi-dimension scoring framework (0–50 points, A+/A/B/C/D grade). Dimensions: revenue model, growth engine, profitability, balance sheet, competitive moat, capital allocation, management, valuation, ESG/regulatory, macro.
- Model: `models/gravity.py`
- PDF: `agents/pdf_gravity.py`
- LLM output: scored JSON ~6000 tokens
- Supports adversarial mode (Claude + GPT-4o cross-review)

#### EODHD Data Sheet (`eodhd_sheet`)
4-page comprehensive data dump. **No LLM call.** Pure EODHD data.
- PDF: `agents/pdf_eodhd_sheet.py`
- Pages: Company Profile (identity + market snapshot + technicals + ownership + dividends + officers + valuation multiples + profitability) | Income Statement | Balance Sheet | Cash Flow
- Column range: 8 most recent historical years

### Custom / User-created Frameworks
Any framework not in `_BUILTIN_IDS` is handled by `models/generic_runner.py`. Uses the framework's `prompt_template` with `{placeholder}` substitution. Renders an HTML report (not PDF) via `agents/report_generic.py`.

---

## 9. Framework System

Frameworks are JSON files in `frameworks/`. Built-in frameworks (`is_builtin: true`) cannot be deleted. Editing a built-in creates a fork (new file with `base_id` pointing to the original).

```python
from framework_manager import FrameworkManager
fm = FrameworkManager()
fw = fm.get("overview")          # load one
all_fw = fm.list()               # all, built-ins first
fork = fm.fork("overview", "My Overview")   # create editable copy
fm.delete("my_overview_abc123")  # delete custom (not built-in)
```

Available prompt placeholders for custom frameworks: `{financials}`, `{forward_estimates}`, `{company_name}`, `{ticker}`, `{currency}`, `{sector}`, `{industry}`, `{country}`, `{current_price}`, `{market_cap}`, `{enterprise_value}`, `{pe_ratio}`, `{forward_pe}`, `{ev_ebitda}`, `{ev_sales}`, `{dividend_yield}`, `{fcf_yield}`, `{roe}`, `{ebit_margin}`, `{net_margin}`, `{revenue_cagr_3y}`, `{revenue_cagr_5y}`, `{description}`, `{employees}`, `{website}`, `{macro_context}`

---

## 10. LLM Client

### Provider switching
Change `LLM_PROVIDER` and `LLM_MODEL` in Streamlit secrets (or `.env` locally). No code changes needed. Valid combinations:
- `claude` + `claude-sonnet-4-5` (or `claude-opus-4-5`, `claude-haiku-4-5`)
- `openai` + `gpt-4o` (or `gpt-4o-mini`, `gpt-4-turbo`)
- `deepseek` + `deepseek-chat` (or `deepseek-reasoner`) — requires `DEEPSEEK_API_KEY`
- `kimi` + a Moonshot model id (e.g. `kimi-k2-0711-preview`, `kimi-k3`, or whatever Moonshot currently publishes) — requires `MOONSHOT_API_KEY`. Routes through the OpenAI-compatible client at `https://api.moonshot.ai/v1` (hardcoded in `llm_client.py`, same pattern as DeepSeek). **`LLM_PROVIDER=openai` never reads an `OPENAI_BASE_URL` secret — there is no such mechanism in this codebase.** To point at a non-OpenAI OpenAI-compatible endpoint you must use (or add) a dedicated provider branch like `kimi`/`deepseek`, not repurpose `LLM_PROVIDER=openai`.
  - **Kimi token budget scaling:** every call's `max_tokens` is multiplied by `KIMI_MAX_TOKENS_MULTIPLIER` (default `2.5`, overridable via Streamlit secret/env) and capped at `KIMI_MAX_TOKENS_CAP` (default `16000`) inside `LLMClient.generate()`'s `kimi` branch — applies automatically to every report type (Investment Memo, Industry Analysis, Gravity, Fisher+Peers, SWOT, peer suggestions, etc.) since they all call through `generate()`/`generate_json()`. Other providers are untouched — the scaling only fires when `self.provider == "kimi"`. Rationale: "thinking"-style Kimi models (e.g. `kimi-k3`) may spend a large, variable share of the completion budget on hidden reasoning before the final JSON answer, so the caller-requested budget (tuned for non-reasoning models) can be too tight, causing empty/truncated responses (see the ACN Investment Memo case, 2026-07-23, where `kimi-k3` returned nothing usable at the un-scaled `max_tokens=12000` and every narrative field fell back to its "not available" default).
  - **Kimi request timeout — silent Streamlit Cloud worker kill (2026-07-23):** raising the cap to `32000` (the first attempt at the fix above) caused the *entire app process* to vanish mid-request with zero Python traceback and zero log output — the Streamlit UI just reset to its default state. Root cause: `_openai()` built the `OpenAI(...)` client with no `timeout`, so a slow/hung completion (a "thinking" model burning many minutes on hidden reasoning at a large token budget, blocking Streamlit's single request thread) can run long enough that Streamlit Community Cloud's platform-level watchdog kills and restarts the whole worker — which produces no catchable exception and no error message at all, unlike a normal API error. **Fix:** added `KIMI_REQUEST_TIMEOUT_SECONDS` (default `240`), passed as `request_timeout` through `_openai()` only on the `kimi` branch → sets `OpenAI(..., timeout=...)`. A slow Kimi call now raises a normal catchable `openai` timeout exception well before the platform would intervene, which surfaces as a visible error in Streamlit instead of a silent collapse. Also lowered `KIMI_MAX_TOKENS_CAP` from `32000` to `16000` as a second line of defense — smaller worst-case budget means a shorter worst-case blocking time. **General rule: any new provider branch added to `_openai()` that might run long (reasoning models, huge max_tokens) should pass an explicit `request_timeout` — the openai SDK's own default has no practical bound, and an unbounded blocking call on Streamlit Cloud fails silently at the platform level rather than raising a debuggable Python error.**
  - Kimi testing was ultimately abandoned in favor of reverting to `claude` (2026-07-23, `claude-opus-4-8`) after repeated `RateLimitError: 429 engine overloaded` responses from Moonshot's infrastructure persisted even with the retry-with-backoff fix (see "Rate-limit retry" below) — a capacity issue on Moonshot's side, not an app bug. The `kimi` provider branch and all its safety nets remain in the codebase for future use if Moonshot's capacity improves.
- `gemini` + a Gemini model id — requires `GEMINI_API_KEY`. Routes through Google's OpenAI-compatible endpoint at `https://generativelanguage.googleapis.com/v1beta/openai/` (hardcoded in `llm_client.py`, `_openai()` with a custom `base_url`, same pattern as DeepSeek/Kimi). Added 2026-07-23, pre-emptively carrying the same two safety nets built for Kimi, since Gemini's "Pro" reasoning models are also "thinking" models with hidden reasoning tokens before the final answer: `GEMINI_MAX_TOKENS_MULTIPLIER` (default `2.0`) / `GEMINI_MAX_TOKENS_CAP` (default `16000`) scale every call's `max_tokens` inside the `gemini` branch of `generate()`; `GEMINI_REQUEST_TIMEOUT_SECONDS` (default `240`) bounds each call so a slow/hung completion raises a catchable timeout instead of risking a silent Streamlit Cloud worker kill.
  - **`gemini-2.5-pro` returns `404 "no longer available to new users"` (2026-07-23):** Google restricts this model for new API keys/projects even though it's still listed as "Stable" in Google's own docs — a known, documented Google-side policy (see their developer forum), not an app bug; nothing to fix in this codebase. **Use `gemini-3.1-pro-preview` instead** (current flagship Pro-tier reasoning model as of 2026-07-23) — or `gemini-2.5-flash` as a definitely-unrestricted stable fallback if the preview model also needs separate allowlisting. **General rule: Gemini model availability shifts frequently and independently of this codebase — if a `LLM_MODEL` value 404s, check `https://ai.google.dev/gemini-api/docs/models` for the current model list rather than assuming a code bug.**
- `xai` + a Grok model id (e.g. `grok-4.5`) — requires `XAI_API_KEY`. Routes through xAI's OpenAI-compatible endpoint at `https://api.x.ai/v1` (hardcoded in `llm_client.py`, `_openai()` with a custom `base_url`, same pattern as DeepSeek/Kimi/Gemini). Added 2026-07-25, purely at the `LLMClient` layer — no report-specific code (Investment Memo, Fisher, Industry Analysis, etc.) was touched, since every report already goes through the provider-agnostic `generate()`/`generate_json()` interface. Pre-emptively carries the same "thinking model" safety nets built for Kimi/Gemini, since Grok's reasoning models can also spend hidden reasoning tokens before the final answer: `XAI_MAX_TOKENS_MULTIPLIER` (default `2.0`) / `XAI_MAX_TOKENS_CAP` (default `36000`) scale every call's `max_tokens` inside the `xai` branch of `generate()`; `XAI_REQUEST_TIMEOUT_SECONDS` (default `300`) bounds each call so a slow/hung completion raises a catchable timeout instead of risking a silent Streamlit Cloud worker kill; `XAI_REASONING_EFFORT` defaults to `""` (param not sent) since it's unconfirmed whether grok-4.5 accepts the OpenAI-style `reasoning_effort` param — `_openai()`'s retry loop already drops the param automatically if the API rejects it, so it's safe to set later via secret/env if xAI's docs confirm support. No Investment-Memo-specific (or any other report-specific) prompt adjustment was made pre-emptively — per the pattern established by every prior provider addition, report-level prompt changes (e.g. the Industry Analysis word-count cut) were only ever made *after* a live test exposed a concrete failure signature (empty JSON / placeholder defaults), never speculatively. **If grok-4.5 shows the same "SWOT/small-calls work, main Porter/narrative call comes back empty" signature documented under Kimi/Gemini above, check `XAI_MAX_TOKENS_CAP` headroom and consider setting `XAI_REASONING_EFFORT=low` before touching any report's prompt length.**

**Confirmed working (2026-07-25):** live-tested by the user across all report types (Investment Memo, Fisher, Industry Analysis, Gravity, Earnings Quality, etc.) — all ran smoothly with no issues, no report-level adjustment needed anywhere. The "add the provider only at the `LLMClient` layer, adjust report prompts only if a live test proves it's needed" approach held for grok-4.5 across the board, same as it did for Claude/OpenAI/DeepSeek before it (unlike Kimi/Gemini, which did need the reasoning-model safety nets exercised).

### Web news search per provider
`generate_web_news()` in `agents/llm_client.py` fetches a current-news narrative for the "Current News" section of Investment Memo reports. Each provider uses a different mechanism:
- **OpenAI**: Responses API with `web_search_preview` tool (native web search)
- **Claude**: Messages API with `web_search_20250305` tool (native web search)
- **DeepSeek**: No native web search tool. Falls back to `NewsAdapter` (NewsAPI.org) to fetch up to 12 recent headlines, then passes them to DeepSeek's chat API to synthesise the analyst narrative (`_deepseek_news_summary()`). Requires `NEWS_API_KEY` in Streamlit secrets. If `NEWS_API_KEY` is absent, the Current News section is silently omitted from the report.
- **Kimi / Gemini / xAI**: Same NewsAdapter fallback as DeepSeek (`_deepseek_news_summary()`, generalized to accept a `base_url`/`api_key` override) — none of them have a native web-search tool wired up through the OpenAI-compatible chat-completions surface used here. (xAI's own API does offer a native "Live Search" server-side tool separate from this chat-completions surface — not wired up; would need its own dedicated method like `_openai_web_search()`/`_claude_web_search()` if wanted later.)

**"Latest Earnings Report" grounding (2026-07-23):** `generate_web_news()` now accepts an optional `company: CompanyData` param (passed from `pages/report_generator.py`'s Investment Memo dispatch as `company=company`). `_format_earnings_grounding(company)` (module-level helper in `llm_client.py`) builds a verified-figures block from the company's own already-fetched data — latest fiscal year revenue/EBIT/net income with YoY % deltas (computed from the two most recent `AnnualFinancials` rows), TTM revenue/net income, forward-consensus revenue/EPS from `ForwardEstimates`, and `next_earnings_date`. Both the native-web-search prompt and the NewsAdapter-fallback prompt (`_deepseek_news_summary()`) now explicitly require a "Latest Earnings Report" theme and are told to treat these figures as the source of truth for revenue/profit/YoY numbers, only using search/headlines to add color (market reaction, guidance detail, analyst commentary) on top. Rationale: web search can miss the exact reported figures or be limited by training cutoff, and NewsAPI headlines routinely don't carry detailed financials at all — grounding on the app's own fetched data (same data already used elsewhere in the report) guarantees this theme is accurate regardless of provider. `_deepseek_news_summary()`'s early-return-on-empty guard was also loosened: it now only returns `""` when **both** NewsAPI headlines and grounding data are unavailable — grounding alone is enough to write a "Latest Earnings Report" theme even if NewsAPI returns nothing (e.g. thinly-covered non-US tickers). **General rule: any future news/narrative call site should pass `company=company` through to `generate_web_news()` if the report has already fetched `CompanyData` — the grounding call is cheap (pure formatting of already-fetched fields, no extra API call) and meaningfully improves accuracy for every provider that doesn't have native web search.**

### Current News section disappeared entirely on Claude after the earnings-grounding change (2026-07-23)
Immediately after the grounding fix above shipped, the "Current News" section vanished completely (not truncated — entirely absent) from Investment Memo reports on `LLM_PROVIDER=claude` (e.g. SNEX/StoneX). Two compounding issues, both in `agents/llm_client.py`:
1. **Root cause:** `_claude_web_search()` calls Anthropic's Messages API with the server-side `web_search_20250305` tool at a hardcoded `max_tokens=2048` — a budget that covers the *entire* multi-turn tool-use exchange (search queries + tool results + final prose), not just the visible answer. The new prompt requires an additional, numerically-detailed "Latest Earnings Report" theme (specific revenue/profit/YoY figures, forward guidance, market reaction) on top of the existing 3-5 themes, which both lengthens the required output and tends to prompt more search tool calls (e.g. a dedicated query for "market reaction to earnings"). For less mainstream-press tickers this pushed the exchange past the 2048-token ceiling before any final text block was ever emitted — `msg.content` ends up with only tool-use/tool-result blocks and no `text` block, so `parts` is `[]`, `"\n\n".join(parts)` is `""`, and `generate_web_news()` returns `""` with **no exception raised at all** (this is a clean, silent empty return, not a caught error) — the caller in `pages/report_generator.py` sees a falsy narrative and shows "⚠ Current News: no results returned", omitting the section from the PDF exactly as reported.
2. **Compounding structural risk (not yet triggered, but latent):** `_format_earnings_grounding(company)` and the `earnings_instruction`/`prompt` string-building in `generate_web_news()` executed **before** the method's own `try/except Exception` block (which only wraps the provider-dispatch branches) — any future exception while building the grounding block would propagate uncaught out of `generate_web_news()` entirely, relying solely on the caller's try/except and producing the same silent-omission symptom for a different reason.

**Fix (2026-07-23):** (1) Raised `_claude_web_search()`'s `max_tokens` from `2048` to `4096`, giving real headroom for the extra required theme plus search-tool overhead. (2) Added a `logger.warning(...)` in `_claude_web_search()` when `parts` comes back empty, logging `stop_reason` and the response's block types, so a repeat of this failure mode is diagnosable from logs instead of silently invisible. (3) Wrapped the `_format_earnings_grounding(company)` call itself in a `try/except Exception` inside `generate_web_news()` (falls back to `grounding = ""`, i.e. the pre-grounding prompt behavior, and logs a warning) so a future data-formatting edge case degrades gracefully instead of taking down the entire Current News feature. **General rule: any server-side-tool Anthropic call (web search, code execution, etc.) needs a `max_tokens` budget sized for the whole tool-use exchange, not just the expected prose length — when lengthening a prompt's requirements for such a call, check whether the existing budget still has headroom.**

### Prompt caching (Claude only)
All report prompts split into:
- `cacheable_prefix` = static schema + instructions (same for every company in same framework)
- `dynamic_prompt` = company-specific financial data, news, macro

For Claude: `cacheable_prefix` is sent as a separate content block with `cache_control: ephemeral`. Anthropic caches it for 5 minutes. ~90% token cost reduction on re-reads.  
For OpenAI: `cacheable_prefix` is prepended to `user_prompt` (no server-side caching, full token cost every call).

### Adversarial mode
Set `ADVERSARIAL_MODE=true`. Both Claude (primary) and GPT-4o (secondary) run independently on the same prompt, then each critiques the other. The merged result flags contested fields. Only works for Overview and Gravity reports. Does NOT respect `LLM_PROVIDER` — always uses Claude as primary and GPT-4o as secondary.

### `generate_json()` reliability
Three fallback strategies if the model wraps JSON in markdown despite instructions:
1. Direct `json.loads()`
2. Extract first `{` to last `}` then parse
3. Regex `\{.*\}` with DOTALL

---

## 11. PDF Generation

All PDF generators use **ReportLab Platypus**. Key patterns:

### ReportLab gotchas (do not violate)
- **Never use Unicode subscript/superscript characters** (₀₁₂, ⁰¹²) — built-in fonts lack these glyphs, renders as black boxes. Use `<sub>` and `<super>` XML tags inside Paragraph objects instead.
- **Never call `HexColor.hexval()`** for XML markup colors — returns `"0xRRGGBB"` not `"#RRGGBB"`. Define plain string constants: `GREEN_HEX = "#1A7E3D"`.
- **Color in Paragraph XML**: `<font color="#1A7E3D">text</font>` — must be a plain `#RRGGBB` string.

### Adding a new report type
1. Create `frameworks/<id>.json` with `is_builtin: true`
2. Add `<id>` to `_BUILTIN_IDS` set in `pages/report_generator.py`
3. Create `agents/pdf_<id>.py` with a `<Name>Generator` class and `.render(company, analysis, path)` method
4. Optionally create `models/<id>.py` with prompt builder returning `(cacheable_prefix, dynamic_prompt)`
5. Add dispatch block in `pages/report_generator.py` (copy pattern from `eodhd_sheet`)
6. Add `importlib.reload()` calls in the dispatch block so live code edits take effect without restarting

---

## 12. Adding New Data Fields

When adding a field that EODHD provides:

1. **`data_sources/base.py`** — add field to `CompanyData` with `Optional[...] = None` (or `List[...] = field(default_factory=list)` for lists)
2. **`data_sources/eodhd_adapter.py`** — fetch the field in `fetch()` and assign to `company.<field>`
3. **`data_sources/data_manager.py`** — add the field name to the `fields=[...]` list in the EODHD `_merge()` call (THIS IS CRITICAL — without it the field is fetched but discarded)
4. Clear the cache (`cache/*.json`) so the new field is populated in fresh fetches

---

## 13. Running Locally

```bash
# Install dependencies
pip install streamlit reportlab yfinance requests python-dotenv anthropic openai

# Fill in .env file (copy from section 3)

# Start the app
streamlit run app.py
```

App opens at `http://localhost:8501`. No login gate in dev mode (no `[users]` in secrets).

---

## 14. Deployment (Streamlit Cloud)

1. Push repo to GitHub: `git push origin master`
2. Connect at https://share.streamlit.io
3. Set secrets in the Streamlit dashboard (all keys from section 3)
4. Every push to `master` triggers automatic redeploy
5. To apply secrets changes without a code push: Streamlit dashboard → app → "..." → "Reboot app"

---

## 15. EODHD Ticker Format Conversion

yfinance uses Yahoo Finance format. EODHD uses its own exchange codes. The mapping is in `eodhd_adapter.py` (`_YF_TO_EODHD` dict).

Key conversions:
- `RHM.DE` → `RHM.XETRA`
- `BA.L` → `BA.LSE`
- `AAPL` (no suffix) → `AAPL.US`
- `005930.KS` → `005930.KO`
- `600519.SS` → `600519.SHG`

Exchanges NOT covered by EODHD (returns 404, gracefully falls back): Japan (`.T`), India (`.NS`, `.BO`), Singapore (`.SI`).

---

## 16. Cache Management

```python
from data_sources.data_manager import DataManager
dm = DataManager()

# Clear one ticker
dm.clear_cache("RHM.DE")

# Clear everything
dm.clear_cache()

# Force fresh fetch in UI: tick "Force refresh data cache" checkbox
```

Cache files: `cache/RHM_DE.json` (dots and dashes replaced with underscores).

---

## 17. Testing a New Report Type End-to-End

```bash
cd /path/to/EquityBot
python -c "
from data_sources.data_manager import DataManager
from agents.pdf_eodhd_sheet import EODHDSheetGenerator

dm = DataManager()
company = dm.get('RHM.DE', force_refresh=True)
print(company.summary())
print('52w high:', company.week_52_high)
print('Officers:', company.officers[:2])
EODHDSheetGenerator().render(company, '/tmp/test.pdf')
print('PDF written')
"
```

Syntax-check all PDF modules:
```bash
python -c "import agents.pdf_eodhd_sheet, agents.pdf_overview; print('OK')"
```

---

## 18. Open / Incomplete Items

- **Adversarial mode**: Only implemented for Overview and Gravity. Fisher does not have adversarial support.
- **Universe screener**: Exists (`models/universe_screener.py`) but is not covered in this documentation. Runs a framework against multiple tickers and produces an HTML comparison.

---

## 19. My Portfolio — Multi-Portfolio Feature (2026-06-12)

### Architecture

Multiple named portfolios. Stored as `{"portfolios": {"My Portfolio": [...], "GB": [...]}}` in the GitHub Gist (and mirrored to the local file as a fallback).

**Session state keys:**
- `all_portfolios` — `dict[str, list[str]]`: full portfolio map, loaded from Gist once per session
- `active_portfolio` — `str`: name of the currently visible portfolio
- `portfolio_tickers` — `list[str]`: re-derived from `all_portfolios[active_portfolio]` on **every rerun** (not guarded by `if not in session_state`). This is intentional — prevents aliasing bugs where `portfolio_tickers` drifts from the ground truth in `all_portfolios`.

**Flow when switching portfolios:** JS click on `pf-dd-option` → fires hidden `pf-sw-anchor` Streamlit button → `active_portfolio` updated → `st.rerun()`. On rerun, `portfolio_tickers` is re-derived from `all_portfolios[active_portfolio]`.

**Flow when creating a portfolio:** JS `doCreate()` → stores name in `?_pf_new=<name>` URL query param via `history.replaceState` → fires hidden `pf-create-anchor` Streamlit button → Python reads `_cname_qp = st.query_params.get("_pf_new")` → creates entry in `all_portfolios` → saves → `st.rerun()`.

### JS–CSS Bridge Pattern

Streamlit renders hidden buttons (text = `"·"`) whose containers are collapsed to zero via CSS. JavaScript running inside a `st.iframe` reaches up into the parent Streamlit page, finds the hidden button by traversing up from a nearby marker `<div>`, and calls `.click()` on it. This triggers a Streamlit rerun as if the user clicked the button.

Key components:
- `anchorBtn(cls, attr, val)` — finds the hidden button adjacent to the marker div with class `cls`
- Marker divs: `.pf-sw-anchor[data-pfidx]`, `.pf-create-anchor`, `.pf-del-pf-anchor`, `.pf-toggle-anchor[data-ticker]`, `.pf-del-anchor[data-ticker]`, `.pf-sort-anchor[data-sortcol]`
- `setInterval(bind, 100)` — rebinds click handlers every 100ms because Streamlit rebuilds the DOM on each rerun

### Ticker-Add Dedup Guard (`_pf_sb_done`)

`st_searchbox` with `clear_on_submit=True` can echo the last selected ticker on subsequent reruns (React component state persistence). Guard: `st.session_state["_pf_sb_done"]` stores the last processed ticker value. A new ticker selection is only processed when `selected_ticker != _pf_sb_done`. The guard resets to `None` when `selected_ticker` becomes `None` (searchbox cleared), allowing the same ticker to be intentionally added to a different portfolio later.

**Critical: track by ticker value only, NOT `(ticker, portfolio)` pair.** A tuple key changes when the portfolio switches even if the searchbox is showing a stale value from the previous portfolio, causing the stale ticker to bleed into the new portfolio.

### Dropdown JS Iframe — Hidden via CSS Anchor Pattern

The dropdown iframe (`st.iframe(height=1)`) is hidden using the same anchor pattern as other hidden controls:

```python
st.markdown("<div class='pf-dd-js-anchor'></div>", unsafe_allow_html=True)
st.iframe(dropdown_js, height=1)
```

CSS:
```css
.pf-dd-js-anchor { display: none; }
div[data-testid="stElementContainer"]:has(.pf-dd-js-anchor),
div[data-testid="stElementContainer"]:has(.pf-dd-js-anchor) + div[data-testid="stElementContainer"] {
  display: none !important;
}
```

`display: none` on the **parent container** does NOT prevent an iframe from loading and executing scripts in Chrome/Firefox/Safari. The dropdown JS keeps running even though its container is hidden.

### Searchbox Style Injection (merged into dropdown iframe)

The red-border CSS for `st_searchbox` is injected by `scanSB()`/`paintSB()` functions inside the **dropdown JS iframe** (not a separate iframe). `scanSB()` runs every 400ms, finds all iframes on the parent page whose `title` or `src` contains `"searchbox"`, and appends a `<style id="eqbot-searchbox-red">` block to their `contentDocument`. The style id prevents duplicate injection.

**Why merged:** A separate `st.iframe` for style injection was unreliable. After a Streamlit rerun, Streamlit creates a NEW iframe node. If its container is `display:none`, some browsers defer loading it. The dropdown iframe is always guaranteed to load (needed for dropdown functionality), so merging is more robust.

### Known Bugs Fixed (this session)

#### Ticker bleeding across all portfolios
`portfolio_tickers` was initialised with `if not in session_state` guard, allowing it to drift from `all_portfolios` across reruns. Fixed by always re-deriving on every rerun (no guard). The `_pf_sb_done` dedup guard prevents stale searchbox echoes from adding tickers to wrong portfolios after portfolio switches.

#### Delete button non-functional on newly created portfolios
`setInterval(bind, 500)` in the dropdown iframe left a 500ms window after a switch where new DOM elements had no click handlers. Fixed by reducing to 100ms.

#### Create form persisting after portfolio creation (searchbox gap)
When the user clicks OK, JS sets `display:flex` on the create form as inline styles. If React reuses the DOM node across a Streamlit rerun (which it does when the component position doesn't change), the inline style persists, leaving the create form visible and displacing the searchbox. Fixed by resetting the inline styles inside `doCreate()` **before** triggering the Streamlit button click.

#### Searchbox invisible after rerun (black on black)
The separate style-injection iframe's container was `display:none`. A newly-inserted iframe in a `display:none` parent can be deferred by the browser, so the CSS injection script never ran after reruns. The searchbox rendered with default (dark) styling — invisible against the black page. Fixed by merging style injection into the always-running dropdown iframe and removing the separate iframe.

#### Phantom gap above searchbox (dropdown iframe not hidden)
The dropdown JS iframe had no CSS hiding its container. Streamlit's default element spacing gave it ~20–40px height. Fixed by adding `pf-dd-js-anchor` marker and corresponding `display:none` CSS (same pattern as all other hidden Streamlit controls).

### Gotchas for Future Development

- **Never use a `(ticker, portfolio)` tuple as the `_pf_sb_done` key.** It causes the stale-searchbox ticker to bleed into the new portfolio on every switch.
- **Always re-derive `portfolio_tickers` from `all_portfolios` every rerun** (no `if not in session_state` guard). A guarded init causes the in-memory list to drift from the persisted map.
- **`display:none` on a parent DOES allow iframe scripts to run** in Chrome/Firefox/Safari. Don't change the dropdown iframe's hiding CSS to `height:0/overflow:hidden` — `display:none` is the correct and tested approach.
- **Gist save is synchronous and blocks the rerun.** `_GIST_TIMEOUT = 5` (reduced from 15). Don't increase it; the UI hangs for the full timeout duration on slow connections.
- **`pf-dd-js-anchor + div` CSS targets the dropdown iframe container.** If you add any new `st.markdown` or `st.` element between the anchor and the iframe, it will hide that element instead of the iframe. Keep anchor and iframe adjacent with nothing between them.
- **The `_pfBound` flag pattern prevents duplicate event listeners** on dropdown elements across the 100ms bind() interval. When adding new interactive elements to the dropdown HTML, always check `if (el && !el._pfBound)` before attaching listeners.
