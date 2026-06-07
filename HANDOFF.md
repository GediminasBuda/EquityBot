# HANDOFF — Session Continuation Document

**Last updated:** 2026-06-07 (session 8)  
**Author:** Claude session, written for the next one.

If you're a Claude agent that just opened this repo on a fresh machine, **read this file end-to-end before touching anything**. Then read `CLAUDE.md` for broader project documentation.

---

## 1 · TL;DR — where we are right now

* **Active branch:** `Final-design-V3`
* **Repo:** https://github.com/GediminasBuda/EquityBot
* **Live URL:** https://equity-oracle.streamlit.app/
* **GitHub account:** `GediminasBuda` / `gediminas.buda1@gmail.com`

**Every commit must be pushed to:**
```
https://github.com/GediminasBuda/EquityBot  (branch: Final-design-V3)
```

**First commands to run when you start:**
```bash
git remote -v             # should show GediminasBuda/EquityBot
git branch --show-current # should print "Final-design-V3"
git log --oneline -10
```

---

## 2 · Migration history (important context)

The project was previously hosted at:
* **Old repo:** `https://github.com/martynasusas-ux/EquityBot`
* **Old branch:** `design-v2`
* **Old URLs:** `equitybot.streamlit.app` (master), `botukas-2.streamlit.app` (design-v2)

All of those are now **abandoned**. The canonical repo is `GediminasBuda/EquityBot`, branch `Final-design-V3`, deployed at `equity-oracle.streamlit.app`.

---

## 3 · Setup on a new machine

```bash
# 1. Clone
git clone https://github.com/GediminasBuda/EquityBot
cd EquityBot
git checkout Final-design-V3

# 2. Python env
python -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

# 3. Dependencies
pip install -r requirements.txt

# 4. Secrets — copy your .env file OR populate .streamlit/secrets.toml

# 5. Run
streamlit run app.py
```

---

## 4 · What's been built (in chronological-ish order)

### Phase A — base app
The original EquityBot is a Streamlit equity-research tool. Frameworks:
* **Overview V2** — investment memo (LLM)
* **Fisher** — 15-question framework (LLM)
* **Gravity Score** — multi-dimension scoring (LLM)
* **EODHD Direct** — pure EODHD data dump (no LLM)
* **Index Overview** — index-level snapshot
* **Industry Analysis** — sector deep-dive (LLM)

### Phase B — Bloomberg terminal aesthetic (design-v2 / Final-design-V3)
Big visual overhaul. **Read this before changing colours / layout:**
* Background: `#000000` (true black)
* Default text: `#FFA028` (Bloomberg amber)
* Muted text: `#8a6a30` / `#5a4a25`
* Bullish (positive deltas, BUY, up-chart): `#4D9FFF` (bright blue)
* Bearish (negative deltas, SELL, down-chart, delete buttons): `#FF3030` (sharper red)
* Borders / rules: `#2a1f10` / `#4a3818`
* **Input fields:** red border + red typed text + transparent placeholder. Implemented via JS injection (`st.iframe(html, height=1)`) into the streamlit_searchbox iframe.
* **No italic anywhere** — `em, i { font-style: normal !important }` global guard.
* **Font:** monospace everywhere via Streamlit theme + per-component overrides.

### Phase C — Insider Transactions framework
New report type. Files:
* `frameworks/insider_transactions.json` — registry entry
* `data_sources/insider_data.py` — orchestrator (tries EODHD → openinsider → insidertrades.info)
* `data_sources/openinsider_scraper.py` — SEC Form 4 feed for US tickers
* `data_sources/insidertrades_scraper.py` — EU + US fallback (only 5 anonymous rows)
* `agents/pdf_insider.py` — PDF generator

**Known limitation:** EODHD `/insider-transactions` returns 0 rows for EU exchanges. US tickers work via openinsider.com fallback. EU coverage is limited to 5 anonymous rows from insidertrades.info.

---

## 5 · Locked design decisions (DO NOT touch without explicit ask)

| Item | Status | Where |
|---|---|---|
| Mobile layout of `pages/my_portfolio.py` | **FROZEN** by user after long iteration | `pages/my_portfolio.py` mobile @media |
| Bloomberg colour palette | Active | `.streamlit/config.toml`, `app.py` global CSS |
| No italic in UI | Hard rule | `app.py` global CSS guard |
| Red inputs (border + typed text) | Hard rule | `app.py` global CSS + JS injection for searchbox |
| Login session persistence | File-backed (`data/last_auth.json`) | `app.py` |
| Portfolio persistence | Private GitHub Gist | `pages/my_portfolio.py` |

---

## 6 · Workflow conventions

* **Branch:** all work on `Final-design-V3`. Never push to `master` without explicit approval.
* **Commits:** descriptive subject (under 70 chars), explanatory body, trailer:
  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```
* **Always syntax-check before commit:** `python -c "import ast; ast.parse(open('FILE').read())"`
* **Ask before destructive ops** (file deletion, force-push, rebase)
* **Don't echo back what was just done** — terse confirmations preferred
* User communicates in **English only** — always respond in English

---

## 7 · Required Streamlit secrets

```toml
# ── AI providers ────────────────────────────────────────────────
ANTHROPIC_API_KEY = "sk-ant-..."
OPENAI_API_KEY    = "sk-proj-..."
LLM_PROVIDER      = "openai"
LLM_MODEL         = "gpt-4o"
ADVERSARIAL_MODE  = "false"

# ── Financial data ──────────────────────────────────────────────
EODHD_API_KEY         = "..."
FRED_API_KEY          = "..."
FMP_API_KEY           = "..."
ALPHA_VANTAGE_API_KEY = "..."

# ── Auth ────────────────────────────────────────────────────────
[users]
gediminas = "<sha256_of_password>"

# ── Session / Portfolio ─────────────────────────────────────────
SESSION_SECRET    = "<random_hex_64_chars>"
GITHUB_GIST_TOKEN = "ghp_..."
```

---

## 8 · Implementation notes

### yfinance rate-limiting
Yahoo throttles Streamlit Cloud IPs aggressively. yfinance errors in logs are **safe to ignore** — DataManager continues without it. The insider report dispatch fetches EODHD `/fundamentals` and `/real-time` directly to back-fill the header.

### EODHD coverage gotchas
* `/insider-transactions` works (HTTP 200) but returns **0 rows for EU exchanges**.
* For US tickers EODHD sometimes prefers bare symbol (`AAPL`) over `AAPL.US` — `insider_data._fetch_eodhd_insider` retries both.

### Session 4 changes (2026-06-02)

#### Industry Analysis — SWOT page
* SWOT is now generated in a **dedicated second LLM call** (`max_tokens=2500`) after the main Porter JSON call. The main call (`max_tokens=9500`) was hitting token limits before reaching the SWOT field.
* `build_swot_prompt()` in `models/industry_analysis.py` — builds SWOT prompt using competitive_advantage_detail + 5-forces summary as context.
* Step 4 in `report_generator.py` industry_analysis dispatch: separate `llm.generate_json()` with `logger.info/exception` for Cloud diagnostics.
* **Never use `importlib.reload()`** on Streamlit Cloud — causes `OSError: inotify instance limit reached` (EMFILE), silently aborts code after the reload.
* Bug fixed: `company.market_cap_usd` → `company.market_cap` (AttributeError in `build_swot_prompt`).
* **Deployment lesson:** all `st.write()` calls go to browser UI only — not visible in manage-app server logs. Use `logger.info()` for any Cloud diagnostics.

#### Baltic stock support
* `data_sources/baltic_tickers.py` — seed list of ~50 companies (VS/TL/RG) + EODHD exchange-symbol-list fetch (7-day cache). Search by name or ticker code.
* Integrated as Layer 2a in `suggest_tickers()` autocomplete.
* Baltic direct-entry pattern now only triggers for all-uppercase queries (no spaces) — prevents phantom Baltic ticker suggestions for name queries like "genda".
* Accepts `:VSE`/`:VLN` → `.VS`, `:TAL` → `.TL`, `:RIG` → `.RG` format aliases.
* `_is_baltic = ticker_input.upper().endswith((".VS", ".TL", ".RG"))` — same pattern as `_is_japan`.
* `_BALTIC_BUNDLE` defined **after** the `if _is_japan:` block (same outer scope). CRITICAL: never put it inside the Japan if-block.
* All 6 EODHD-bundle dispatches have `elif _is_baltic: bundle = _BALTIC_BUNDLE`: overview_v2, fisher, fisher_peers, industry_analysis, valuemeter, gravity.

---

### insidertrades.info hard limit
Anonymous HTTP GET returns **only 5 rows** per ticker. Don't try to make the scraper smarter — data isn't there for anonymous requests.

### Searchbox styling (red border + red typed text)
`streamlit_searchbox` renders in iframe → parent CSS can't reach it. Solution: `st.iframe` with `<script>` that injects `<style>` into `iframe.contentDocument` on 800ms interval. Two copies: `pages/report_generator.py` and `pages/my_portfolio.py` — if you change one, change both.

### Streamlit deprecations
* `st.components.v1.html` → use `st.iframe(html, height=N)`. Rejects `height=0` — use `height=1`.

### ReportLab gotchas
* Never use Unicode subscript/superscript characters — use `<sub>` and `<super>` XML tags.
* Never call `HexColor.hexval()` for markup — returns `"0xRRGGBB"` not `"#RRGGBB"`. Use plain string constants.

---

## 9 · Open / Incomplete items

* **EU insider data:** EODHD returns 0 rows for EU exchanges. insidertrades.info capped at 5 anonymous rows. No decision yet on how to improve EU coverage (TradingView scraper / per-country regulators / leave as-is).
* **Adversarial mode:** Only implemented for Overview and Gravity. Fisher does not have adversarial support.
* **Screener emerging markets:** ConstituentResolver depends on Wikipedia. If Wikipedia page format changes or is unavailable, Path A fails with a clear error. No fallback for exchanges without a Wikipedia constituent list.
* **Screener Path B (NL):** Still global only — no exchange/sector filtering. MCap values from EODHD screener are in local currency, not USD — global sort by market_cap is unreliable.

---

## 11 · Screener page — architecture & known limits

### Files
| File | Purpose |
|---|---|
| `pages/screener.py` | Main Streamlit page. Bloomberg CSS, form input, results table, Gravity Taxers launch button. |
| `models/screener_intent.py` | LLM NL→filter parser. Returns JSON: filters (numeric only), signals, sort, limit, title, notes. |
| `data_sources/eodhd_screener_api.py` | Thin wrapper around `GET /api/screener`. EXCHANGE_CODES dict used in UI hint expander. |

### Two search paths (critical — read before touching)

**Path A — Known index query** (detected by `_detect_index_query()` in `screener.py`):
- Triggered by: "DAX 40", "CAC 40", "FTSE 100", "OMX Helsinki", "S&P 500", "WIG 20", etc.
- Uses `ConstituentResolver` → Wikipedia scrape → real tickers with correct exchange suffixes
- Enriches with: `_fetch_exchange_names()` (names) + `_fetch_bulk_prices()` (prices) + `_fetch_fundamentals_batch()` (sector/mcap/div yield, 24h cache)
- Table shows: Ticker, Name, Sector, MCap, Div Yield, Price, Chg%, Exchange
- If ConstituentResolver returns empty (Wikipedia scrape failed) → clear error message, no "No results" spinner

**Path B — General NL query** (everything else):
- LLM parses query → `screener_intent.py` returns filters
- **CRITICAL:** String filters (exchange, sector, industry) are STRIPPED before the API call — EODHD screener API ignores them and returns empty. Only numeric filters work: `dividend_yield`, `market_capitalization`, `earnings_share`, `adjusted_close`, `refund_1d_p`, `refund_5d_p`, `avgvol_1d`, `avgvol_200d`.
- Results are global (no country/sector filtering). A note is shown in UI.

### What works / what doesn't
```
✅ "top 20 DAX 40 companies"          → Path A, real DAX tickers + names + prices
✅ "CAC 40 largest stocks"             → Path A
✅ "OMX Helsinki 25"                   → Path A, with sector/MCap/DivYield
✅ "top 20 brazil" / "ibovespa"        → Path A (IBOVESPA ^BVSP)
✅ "istanbul" / "bist"                 → Path A (BIST 100 ^XU100)
✅ "sensex" / "nifty"                  → Path A (BSE/NSE India)
✅ "dividend yield above 4%"           → Path B, numeric filter works
✅ "large cap profitable companies"    → Path B, market_cap + earnings_share filters
❌ "German banks div yield > 4%"       → Path B, exchange+sector stripped, only div filter kept (global results)
❌ "WIG 20 Poland"                     → Path A attempted, but ConstituentResolver may fail to scrape WIG20 Wikipedia
```

### UI behaviour
- **Select All / Deselect All**: explicitly sets all `scr_chk_*` session_state keys — required because Streamlit keyed widgets ignore `value=` after first render.
- **Run Gravity Taxers button**: always visible below results table; disabled when 0 selected. Hands off to `report_generator.py` via `st.session_state.rg_bulk_run`.
- **Search**: wrapped in `st.form` so Enter key and button click both trigger search.

### Baltic stocks (added this session)
Report Generator search now detects Baltic ticker patterns (APG1L, APG1L.VS etc.) and suggests all three Baltic exchanges (🇱🇹 VS, 🇪🇪 TL, 🇱🇻 RG). Added `.VS`, `.TL`, `.RG` to `_YF_TO_EODHD` in `eodhd_adapter.py`. Data pipeline passes Baltic tickers as-is to EODHD (yfinance silently fails, EODHD handles them correctly).

---

## 12 · Session 5 changes (2026-06-03)

### Ticker search fixes (Report Generator + My Portfolio)

**Baltic phantom ticker bug fixed:**
- `_smart_search()` in `report_generator.py`: the "no suffix" Baltic branch now requires `any(c.isalpha() for c in _b_code)`. Purely numeric codes like `6752` no longer generate phantom `.VS/.TL/.RG` suggestions.
- Same fix applied to `_ticker_search()` in `my_portfolio.py`.

**Japan company name in direct code entry:**
- Layer 1 (pattern match for `\d{3,4}(\.T)?`) now looks up the company name from `get_japan_tickers()` cache before building the label. `6752` → `🇯🇵 6752.T  Panasonic Holdings Corporation · TSE`.
- Same fix in both `report_generator.py` and `my_portfolio.py`.

**Baltic name search added to My Portfolio:**
- `_ticker_search()` now calls `search_baltic()` (same as Report Generator). Searching "apranga" returns Baltic results.

### My Portfolio — Baltic + Japan data fixes

**Snapshot / history / earnings routing:**
- `_fetch_snapshot`, `_fetch_history`, `_next_earnings` in `my_portfolio.py` now route `.VS/.TL/.RG` tickers to yfinance helpers (same as `.T` Japan). EODHD returns no reliable data for most Baltic stocks; yfinance does (Yahoo Finance covers Apranga etc.).

**News for Japan/Baltic:**
- `_fetch_news()` now routes `.T/.VS/.TL/.RG` to `yfinance.Ticker.news` instead of EODHD. Response normalised to `{title, link, date}` shape the renderer expects.

**Chart X-axis labels:**
- 5y/All periods: `format="%Y"`, `tickCount="year"` — shows `2021 2022 2023…` not months.
- 6m/YTD/1m periods: `format="%b %Y"`, `tickCount="month"`.

### Screener — index coverage + fundamentals

**Path A fundamentals (sector/MCap/DivYield):**
- `_fetch_fundamentals_batch(eodhd_tickers)` — new function, `@st.cache_data(ttl=86400)`. Calls `/api/fundamentals/{ticker}` per constituent (no filter param — EODHD `::` filter syntax unreliable). Extracts `General.Sector`, `Highlights.MarketCapitalizationMln`, `Highlights.DividendYield`. 25 tickers ≈ 4s first load, instant on cache hit.
- Called in Path A after `_constituents_to_rows()` to merge sector/mcap/div_yield into rows.
- **CRITICAL:** EODHD screener exchange string filter does NOT work (returns empty). Never use it as primary data source. `_fetch_fundamentals_batch` is the correct path.

**Emerging market indices added to `_INDEX_QUERY_MAP`:**
- IBOVESPA (`^BVSP`) — keywords: `brazil`, `ibovespa`, `b3`, `sao paulo`, `san paulo`, `bovespa`
- IPC Mexico (`^MXX`), BIST 100 (`^XU100`), BSE Sensex (`^BSESN`), Nifty 50 (`^NSEI`)
- TASI Saudi (`^TASI.SR`), TA-35 Israel (`^TA35.TA`), JSE Top 40 (`^J203.JO`)
- IDX Composite Indonesia (`^JKSE`), WIG 20 Poland (`^WIG20`)

**What was tried and failed for whole-exchange queries:**
- `_exchange_query_map` + exchange-symbol-list: "SA" returned Vietnamese stocks, "IS" returned nothing. Exchange codes differ between real-time API and symbol-list API.
- `bulk_last_day/{exchange}` endpoint: returned nothing for "SA" (Brazil) and "IS" (Turkey).
- EODHD screener `exchange=SA` filter: returns empty (string filters unsupported).
- **Conclusion (updated session 6):** Path C (exchange-symbol-list) is now the standard path for exchange queries. Works well for small/medium exchanges (Budapest, Warsaw). Some EODHD codes return wrong data (PSE → Vietnamese stocks) — unfixable on our side.

**Checkbox label warning fixed:**
- `cols[0].checkbox("", ...)` → `cols[0].checkbox("Select", label_visibility="collapsed")`. Streamlit now warns on empty labels.

**Git workflow confirmed:**
- Always commit and push to `Final-design-V3`. Streamlit Cloud deploys from that branch.
- `master` branch is irrelevant for deployment.

---

## 14 · Session 6 changes (2026-06-03) — Screener overhaul

### Three-path screener architecture (final design)

**Priority: Path A → Path C → Path B**

| Path | Trigger | Mechanism |
|---|---|---|
| A | Known index keyword (dax, ftse100, nikkei, etc.) | ConstituentResolver → Wikipedia / FMP |
| C | Exchange code word (BUD, WAR, LSE) or NL phrase (budapest, greek stocks) | EODHD exchange-symbol-list → fundamentals batch → sort MCap |
| B | Everything else | LLM parses query → EODHD screener API (numeric filters only) |

### Path A — expanded keywords
- Added country-level keywords: `australia/asx/xasx → ASX 200`, `germany/german → DAX`, `uk/british → FTSE 100`, `japan/japanese → Nikkei`, etc.
- Added SDAX (`^SDAXI`) and TecDAX (`^TECDAX`)

### Path C — new (exchange-level screening)
**Files:** `pages/screener.py` — `_detect_exchange_query()`, `_fetch_all_eodhd_exchanges()`, `_EXCH_CODE_STOPWORDS`, `_EXCH_MANUAL_ALIASES`, `_EXCH_NL_PHRASES`

**How it works:**
1. `_fetch_all_eodhd_exchanges()` — calls `/api/exchanges-list`, cached 24h — returns every EODHD exchange code automatically. No hardcoding needed for new exchanges.
2. Detection checks: (1) manual aliases (ATHEX→AT, BIST→IS, GPW→WAR…), (2) live code match on uppercase words, (3) NL phrases ("athens", "budapest stocks"…)
3. `_fetch_exchange_names(code)` → exchange-symbol-list → all listed symbols
4. `_fetch_fundamentals_batch` on first 120 symbols → sector, MCap, DivYield
5. Sort by MCap desc, return top N

**Stopwords:** short codes that are also common English words (AT, IS, ST, CO, BR, PA, PR, HE, TO, AS…) are blocked from auto-detection. Reach them via manual alias (ATHEX, BIST, XCSE…) or NL phrase ("athens", "istanbul"…).

**Known EODHD data quality issues (unfixable):**
- `PSE` exchange-symbol-list returns Vietnamese stocks instead of Philippine stocks
- Some obscure exchange codes return empty or wrong data
- These are EODHD API bugs — nothing to fix on our side

### Path A → C fallback
When Path A ConstituentResolver fails (Wikipedia unavailable), instead of showing error + stop, now automatically falls back to Path C using `_IDX_EXCH_FB` dict:
```python
_IDX_EXCH_FB = {
    "^XU100": "IS",   # BIST Istanbul
    "^TASI.SR": "XSAU",
    "^N225": "TSE",
    "^FTSE": "LSE",
    "^GDAXI": "XETRA",
    # ... 20+ entries
}
```
Example: "turkish stocks" → BIST 100 → Wikipedia fails → falls back to IS exchange → Path C → real Turkish stocks.

### Routing logic rewrite
The routing block was rewritten with an explicit `_path = "A" / "C" / "B"` variable. Previous version had a broken `elif/else` chain where `else` (Path B) was attached to the wrong `if`, causing Path B to run over already-successful Path A results.

---

## 13 · Industry Analysis — SWOT addition (2026-06-02)

### What was added
New **SWOT page** in the Industry Analysis PDF, after Competitive Advantage detail, before Key Uncertainties.

**Files changed:**

| File | Change |
|---|---|
| `models/industry_analysis.py` | New `swot` object in `_CACHEABLE` JSON schema + `_validate_analysis()` defaults |
| `agents/pdf_industry_analysis.py` | `_swot_page()` + 4 new styles + inserted into `render()` |
| `pages/report_generator.py` | `max_tokens` 8000 → 9500 (both normal + adversarial paths) |

**SWOT format (buyside analyst style):**
- `summary` — < 150 words, overall investment read
- `strengths` / `weaknesses` / `opportunities` / `threats` — each ≤ 200 words, data-backed

**LLM instructions:** build on Porter/CompAdv insights already generated + additional sources. Every claim requires a stat or financial data point.

**PDF layout:** summary text → 2×2 grid: 🟢 S (dark green) · 🔴 W (dark red) · 🔵 O (dark blue) · 🟡 T (dark amber).

**Graceful degradation:** if all swot fields empty (old cached analysis), page is silently skipped.

---

## 15 · Session 7 changes (2026-06-05) — Japan ticker search fixes

### Investment Memo Bull/Bear description update
- `frameworks/overview_v2.json` — updated description to plain English: overview, snapshot, bull/bear, recommendation, peer comparison, EODHD + yfinance fallback. Removed duplicate "· PDF report" label (UI appends it automatically from framework metadata).

### Japan autocomplete fixes (`data_sources/japan_tickers.py`, `pages/report_generator.py`)

**Bug fixed — `.T` suffix in `search_japan()`:**
- Typing `6061.T` lowercased to `"6061.t"` was never matching code `"6061"` — `"6061.t" in "6061"` is False.
- Fix: strip `.t` from query at the top of `search_japan()` before matching.

**Seed list expanded to full TOPIX (~1 650 companies):**
- Replaced the old ~220-company seed with all TOPIX index constituents from `topixweight_e.csv` (JPX data file).
- Covers 1 650 companies across all sectors. Alphanumeric codes (e.g. `167A`, `212A`) for newer IPOs included.
- Non-TOPIX tickers (e.g. `5951`, `6061`) still won't have names in the seed — handled by the fallback below.

**yfinance name fallback for non-TOPIX tickers (Layer 1 in `_smart_search`):**
- When Layer 1 direct-code match can't find a name in seed/cache, fires one `yfinance.Search(ticker, max_results=3, news_count=0)` call.
- Only uses result on exact symbol match (e.g. result symbol == `"6061.T"`) to avoid wrong names.
- Fast enough for autocomplete (< 1s on Streamlit Cloud). Confirmed working in live test.

### Communication preference
- User communicates in **English only**. Always respond in English.

---

## 16 · Session 8 changes (2026-06-07) — EODHD price chart split-adjustment fix

### Bug: EODHD Direct price chart showed unadjusted prices

**File:** `agents/pdf_eodhd_full.py`

**Symptom:** CPRT chart showed a spike to $159 followed by a crash to ~$31, making it look like a 80% drawdown. Reality: two 2:1 splits in Nov-2022 and Aug-2023 were not reflected in the chart because raw `close` was used instead of `adjusted_close`.

**Root cause (line 705):**
```python
# WRONG — close is always non-null so adjusted_close was never used
p = row.get("close") or row.get("adjusted_close")
```

**Fix — same pattern already used in `agents/price_chart.py`:**
```python
adj = row.get("adjusted_close")
raw = row.get("close")
p = adj if adj not in (None, "", "NA", 0) else raw
```

**Also fixed:**
- Summary stats (Period High/Low, First/Last Close, Period Return) now also use `adjusted_close`-first logic via helper `_adj_close()`.
- Chart footnote updated: "split-adjusted close" so reader knows prices are adjusted.

**Commit:** `894eb54` on `Final-design-V3`

---

## 13 · How to greet the user when you start

Don't summarise this whole document. Just confirm you've read it, name current state in one line, ask what to do.

Always respond in **English only**. Terse, technical, no-fluff.
