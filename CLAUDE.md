# Your Humble EquityBot — Agent Handoff Documentation

**Last updated:** 2026-06-07  
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
