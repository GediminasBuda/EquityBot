# HANDOFF — Session Continuation Document

**Last updated:** 2026-05-26  
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
* User speaks **Lithuanian + English mixed** — match their language

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

---

## 10 · How to greet the user when you start

Don't summarise this whole document. Just confirm you've read it, name current state in one line, ask what to do.

The user speaks Lithuanian + English mixed. Match their language. Terse, technical, no-fluff.
