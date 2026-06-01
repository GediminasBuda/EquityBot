"""
universe_screener.py — Applies an investment framework across all constituents
of a market index, producing a ranked comparative report (like the PDF example).

Flow:
  1. Resolve index → constituent tickers  (ConstituentResolver)
  2. Batch-fetch CompanyData for each constituent  (DataManager.get_many)
  3. Build a comparative prompt with all financials + framework criteria
  4. Single Claude call → ranked JSON output
  5. Render self-contained HTML → save to outputs/
"""

from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# Max constituents to include in a single LLM call
_MAX_CONSTITUENTS = 60


class UniverseScreener:
    """
    Screen all constituents of an index through an investment framework.

    Usage:
        screener = UniverseScreener()
        html_path = screener.run(
            index_ticker="^OMXH25",
            framework_id="gravity",
            progress_cb=lambda pct, msg: print(f"{pct}% {msg}"),
        )
    """

    def run(
        self,
        index_ticker: str,
        framework_id: str,
        output_path: Optional[str] = None,
        force_refresh: bool = False,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        tickers: Optional[list[str]] = None,
    ) -> str:
        """
        Run the full pipeline. Returns path to the saved HTML report.
        progress_cb(pct: int, msg: str) is called at each step if provided.

        Args:
            index_ticker  — e.g. "^GSPC". Used as the report's label.
            framework_id  — which framework to apply (fisher, gravity, …)
            tickers       — OPTIONAL pre-filtered list of Yahoo-format tickers
                            (e.g. the top 10 from a screener). When provided,
                            the constituent-resolver step is skipped entirely.
        """
        from constituent_resolver import ConstituentResolver
        from data_sources.data_manager import DataManager
        from framework_manager import FrameworkManager

        # Initialise the usage attribute so callers can always read it
        # safely even if the LLM call never happens (e.g. <3 companies loaded).
        self.last_usage: dict = {}

        def _prog(pct: int, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info(f"[UniverseScreener] {pct}% — {msg}")

        # ── Load framework ─────────────────────────────────────────────────────
        fw = FrameworkManager().get(framework_id)
        if fw is None:
            raise ValueError(f"Framework '{framework_id}' not found.")

        # ── Resolve constituents — or use pre-filtered list ────────────────────
        if tickers:
            constituents = [t.strip().upper() for t in tickers if t and isinstance(t, str)]
            _prog(5, f"Using {len(constituents)} pre-filtered constituents…")
        else:
            _prog(5, f"Resolving {index_ticker} constituents…")
            resolver     = ConstituentResolver()
            constituents = resolver.resolve(index_ticker, force_refresh=force_refresh)

        if not constituents:
            raise ValueError(
                f"Could not resolve constituents for {index_ticker}. "
                "Check the ticker format or provide tickers manually."
            )

        constituents = constituents[:_MAX_CONSTITUENTS]
        n = len(constituents)
        _prog(10, f"Found {n} constituents — fetching financial data…")

        # ── Fetch company data for each constituent ────────────────────────────
        dm = DataManager()

        companies: dict = {}
        failed:    list = []

        for i, ticker in enumerate(constituents, 1):
            pct = 10 + int((i / n) * 40)   # 10% → 50%
            _prog(pct, f"Fetching {ticker} ({i}/{n})…")
            try:
                cd = dm.get(ticker, force_refresh=force_refresh)
                if cd.name:
                    companies[ticker] = cd
                else:
                    failed.append(ticker)
            except Exception as e:
                logger.warning(f"[UniverseScreener] {ticker} failed: {e}")
                failed.append(ticker)

        loaded = len(companies)
        _prog(52, f"Data loaded: {loaded}/{n} companies · {len(failed)} skipped")

        if loaded < 3:
            raise ValueError(
                f"Only {loaded} companies loaded from {index_ticker}. "
                "Not enough data to run a comparison."
            )

        # ── Build prompt ───────────────────────────────────────────────────────
        _prog(55, "Building comparative analysis prompt…")
        prompt = _build_universe_prompt(fw, index_ticker, companies)

        # ── Call Claude ────────────────────────────────────────────────────────
        _prog(60, f"Running {fw.name} analysis across {loaded} companies — typically 60–120 s…")
        raw, usage = _call_claude(fw.system_prompt, prompt)
        # Expose the usage dict to callers so the UI can render the cost block.
        # `_call_claude` uses the same shape as Anthropic's API response, so it
        # plugs straight into _claude_cost / _cost_block in report_generator.
        self.last_usage = usage
        cache_hit = usage.get("cache_read_input_tokens", 0)
        if cache_hit:
            _prog(88, f"Analysis complete (cache hit: {cache_hit:,} tokens saved) — rendering report…")
        else:
            _prog(88, "Analysis complete — rendering report…")
        logger.info(f"[UniverseScreener] Token usage: {usage}")

        # ── Parse + render ─────────────────────────────────────────────────────
        analysis = _parse_json(raw)

        # Determine output path — always PDF now
        if output_path is None:
            from config import OUTPUTS_DIR
            safe_idx = index_ticker.replace("^", "").replace(".", "_")
            safe_fw  = re.sub(r"[^a-z0-9]+", "_", fw.name.lower()).strip("_")[:20]
            date     = datetime.now().strftime("%Y-%m-%d")
            output_path = str(OUTPUTS_DIR / f"{safe_idx}_{safe_fw}_universe_{date}.pdf")
        else:
            # Caller may have passed an .html path — silently fix extension
            output_path = str(output_path).removesuffix(".html") + ".pdf"

        os.makedirs(Path(output_path).parent, exist_ok=True)

        import importlib
        import agents.pdf_universe_screener as _pus_mod
        importlib.reload(_pus_mod)
        from agents.pdf_universe_screener import render_universe_pdf
        render_universe_pdf(
            fw=fw,
            index_ticker=index_ticker,
            companies=companies,
            analysis=analysis,
            failed=failed,
            output_path=output_path,
        )

        _prog(100, "Report ready!")
        logger.info(f"[UniverseScreener] PDF saved → {output_path}")
        return output_path


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_universe_prompt(fw, index_ticker: str, companies: dict) -> str:
    """
    Build the comparative analysis prompt.
    Includes a compact financial table for every constituent.
    """
    # Build compact financial rows
    rows = []
    for ticker, cd in companies.items():
        la = cd.latest_annual()

        def _v(val, fmt=".1f", suffix=""):
            if val is None: return "n/a"
            try:
                return f"{val:{fmt}}{suffix}"
            except Exception:
                return str(val)

        row = (
            f"  {cd.name or ticker:<35} | {ticker:<12} | "
            f"MCap: {_v(cd.market_cap/1000 if cd.market_cap else None, '.1f')}B | "
            f"Rev: {_v(la.revenue/1000 if la and la.revenue else None, '.1f')}B | "
            f"NM: {_v(la.net_margin*100 if la and la.net_margin else None, '.1f')}% | "
            f"ROE: {_v(la.roe*100 if la and la.roe else None, '.1f')}% | "
            f"P/E: {_v(cd.pe_ratio, '.1f')}x | "
            f"EV/EBIT: {_v(cd.ev_ebit, '.1f')}x | "
            f"Sector: {cd.sector or 'n/a'}"
        )
        rows.append(row)

    financials_block = "\n".join(rows)

    # FRED macro context
    try:
        from data_sources.fred_adapter import get_macro_block
        macro_block = get_macro_block()
    except Exception:
        macro_block = ""
    macro_section = f"\n{macro_block}\n" if macro_block else ""

    # NOTE: sys_excerpt removed — the full framework system_prompt is already
    # passed as the system message in _call_claude(), so duplicating it here
    # wastes ~300 tokens per universe call.

    return f"""You are screening the constituents of {index_ticker} through the {fw.name} framework.
{macro_section}
UNIVERSE — {len(companies)} companies with key financial metrics:
{financials_block}

YOUR TASK:
1. Apply the {fw.name} framework criteria to EACH company above.
2. Identify the best fits (highest-quality matches to the framework's core thesis).
3. Rank the top picks from best to weakest match.
4. Group companies into meaningful categories if the framework supports it.
5. Explicitly list exclusions and briefly explain why each doesn't fit.
6. For the top 3–5 picks, give a brief rationale citing specific characteristics.

Respond ONLY with a valid JSON object. No prose outside the JSON.

JSON schema:
{{
  "universe_summary": "<1-2 sentences: overall quality of this universe through the {fw.name} lens>",
  "top_picks": [
    {{
      "rank": 1,
      "ticker": "<Yahoo Finance ticker>",
      "name": "<company name>",
      "score": "<float 1.0-10.0>",
      "choke_point_or_moat": "<key structural advantage>",
      "unavoidable_flow_or_thesis": "<what recurring flow or investment thesis applies>",
      "revenue_model": "<brief description>",
      "rationale": "<2-3 sentences: why this company fits the framework>"
    }}
  ],
  "groups": [
    {{
      "group_name": "<e.g. 'Tier 1 — Core Fits' or 'Adjacent Cousins'>",
      "tickers": ["<ticker1>", "<ticker2>"],
      "group_rationale": "<1 sentence>"
    }}
  ],
  "exclusions": [
    {{
      "ticker": "<ticker>",
      "name": "<name>",
      "reason": "<why it doesn't fit the framework>"
    }}
  ],
  "framework_observations": "<paragraph: what does this universe look like through the {fw.name} lens? Key patterns, overall quality, noteworthy characteristics>",
  "recommendation": "<SELECTIVE | BROADLY ATTRACTIVE | AVOID UNIVERSE>"
}}"""


# ── LLM call ──────────────────────────────────────────────────────────────────

def _call_claude(system_prompt: str, prompt: str) -> tuple[str, dict]:
    """
    Call Claude and return (response_text, usage_dict).
    System prompt is marked cacheable so repeated calls for the same framework
    benefit from Anthropic's prompt cache (90% read discount).
    usage_dict keys: input_tokens, output_tokens,
                     cache_creation_input_tokens, cache_read_input_tokens.
    """
    from config import ANTHROPIC_API_KEY, LLM_MODEL
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Mark the framework system prompt cacheable.
    # The 1024-token threshold is checked against (system + any cached user blocks).
    # Framework system prompts are 180–326 tokens, which is below the threshold on
    # their own, but Anthropic will silently skip caching rather than error.
    system_content = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=8000,
        temperature=0.3,
        system=system_content,
        messages=[{"role": "user", "content": prompt}],
    )

    u = message.usage
    usage = {
        "input_tokens":               u.input_tokens,
        "output_tokens":              u.output_tokens,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens":     getattr(u, "cache_read_input_tokens",     0) or 0,
    }
    return message.content[0].text, usage


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end   = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    logger.warning("[UniverseScreener] Could not parse JSON; returning empty dict")
    return {}


# ── HTML renderer ─────────────────────────────────────────────────────────────

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       margin: 0; padding: 24px; background: #F8FAFC; color: #1a1a2e; }
.container { max-width: 1100px; margin: 0 auto; }
h1 { color: #1B3F6E; font-size: 22px; margin-bottom: 4px; }
h2 { color: #1B3F6E; font-size: 16px; margin: 20px 0 8px; border-bottom: 2px solid #1B3F6E;
     padding-bottom: 4px; }
h3 { color: #1B3F6E; font-size: 14px; margin: 12px 0 4px; }
.subtitle { color: #666; font-size: 13px; margin-bottom: 20px; }
.summary-box { background: #EEF5FB; border-left: 4px solid #1B3F6E; padding: 12px 16px;
               border-radius: 4px; margin-bottom: 20px; font-size: 14px; }
.rec-banner { display: inline-block; padding: 6px 18px; border-radius: 4px;
              font-weight: 700; font-size: 14px; margin-bottom: 16px; }
.rec-selective { background: #FDEBD0; color: #D68910; }
.rec-broadly { background: #D4EDDA; color: #1A7E3D; }
.rec-avoid { background: #FADBD8; color: #C0392B; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-bottom: 20px; }
th { background: #1B3F6E; color: white; padding: 7px 10px; text-align: left; }
td { padding: 6px 10px; border-bottom: 1px solid #E8F0F8; vertical-align: top; }
tr:nth-child(even) td { background: #F4F9FF; }
.rank-badge { display: inline-block; background: #1B3F6E; color: white;
              border-radius: 12px; padding: 1px 8px; font-size: 11px; font-weight: 600; }
.score-badge { display: inline-block; background: #EEF5FB; color: #1B3F6E;
               border-radius: 4px; padding: 1px 7px; font-size: 11px; font-weight: 700; }
.exclusion-row td { color: #888; background: #FAFAFA; }
.group-chip { display: inline-block; background: #EEF5FB; border: 1px solid #B8D4ED;
              border-radius: 12px; padding: 2px 10px; font-size: 11px; margin: 2px; }
.observations { background: #FFF8EE; border-left: 4px solid #E59C00; padding: 12px 16px;
                border-radius: 4px; font-size: 13.5px; line-height: 1.6; }
.footer { color: #999; font-size: 11px; margin-top: 24px; border-top: 1px solid #ddd;
          padding-top: 10px; }
"""


def _render_universe_html(
    fw,
    index_ticker: str,
    companies: dict,
    analysis: dict,
    failed: list,
) -> str:
    date      = datetime.now().strftime("%Y-%m-%d")
    n_loaded  = len(companies)
    picks     = analysis.get("top_picks", [])
    exclusions = analysis.get("exclusions", [])
    groups    = analysis.get("groups", [])
    rec       = analysis.get("recommendation", "")
    summary   = analysis.get("universe_summary", "")
    obs       = analysis.get("framework_observations", "")

    # Recommendation banner class
    rec_class = "rec-broadly" if "ATTRACTIVE" in rec.upper() else (
        "rec-avoid" if "AVOID" in rec.upper() else "rec-selective"
    )

    # ── Top picks table ───────────────────────────────────────────────────────
    picks_rows = ""
    for p in picks:
        picks_rows += f"""
        <tr>
          <td><span class="rank-badge">#{p.get('rank','?')}</span></td>
          <td><strong>{p.get('name','')}</strong><br>
              <code style="font-size:11px">{p.get('ticker','')}</code></td>
          <td><span class="score-badge">{p.get('score','?')}/10</span></td>
          <td>{p.get('choke_point_or_moat','')}</td>
          <td>{p.get('unavoidable_flow_or_thesis','')}</td>
          <td style="font-size:12px">{p.get('rationale','')}</td>
        </tr>"""

    picks_table = f"""
    <table>
      <thead><tr>
        <th>Rank</th><th>Company</th><th>Score</th>
        <th>Key Advantage</th><th>Recurring Flow / Thesis</th><th>Rationale</th>
      </tr></thead>
      <tbody>{picks_rows}</tbody>
    </table>""" if picks_rows else "<p><em>No top picks returned by model.</em></p>"

    # ── Groups ────────────────────────────────────────────────────────────────
    groups_html = ""
    for g in groups:
        chips = " ".join(
            f'<span class="group-chip">{t}</span>'
            for t in g.get("tickers", [])
        )
        groups_html += f"""
        <h3>{g.get('group_name','Group')}</h3>
        <p style="font-size:12.5px;color:#555">{g.get('group_rationale','')}</p>
        <div style="margin-bottom:10px">{chips}</div>"""

    # ── Exclusions table ──────────────────────────────────────────────────────
    excl_rows = ""
    for e in exclusions:
        excl_rows += f"""
        <tr class="exclusion-row">
          <td>{e.get('name','')}</td>
          <td><code style="font-size:11px">{e.get('ticker','')}</code></td>
          <td>{e.get('reason','')}</td>
        </tr>"""

    excl_table = f"""
    <table>
      <thead><tr><th>Company</th><th>Ticker</th><th>Exclusion Reason</th></tr></thead>
      <tbody>{excl_rows}</tbody>
    </table>""" if excl_rows else ""

    # ── Consolidated data table ───────────────────────────────────────────────
    data_rows = ""
    for ticker, cd in companies.items():
        la = cd.latest_annual()
        def _v(v, fmt=".1f", suffix=""):
            if v is None: return "n/a"
            try: return f"{v:{fmt}}{suffix}"
            except: return str(v)

        data_rows += f"""
        <tr>
          <td>{cd.name or ticker}</td>
          <td><code>{ticker}</code></td>
          <td>{_v(cd.market_cap/1000 if cd.market_cap else None)} B</td>
          <td>{_v(la.revenue/1000 if la and la.revenue else None)} B</td>
          <td>{_v(la.net_margin*100 if la and la.net_margin else None)}%</td>
          <td>{_v(la.roe*100 if la and la.roe else None)}%</td>
          <td>{_v(cd.pe_ratio)}x</td>
          <td>{_v(cd.ev_ebit)}x</td>
          <td>{cd.sector or 'n/a'}</td>
        </tr>"""

    data_table = f"""
    <table>
      <thead><tr>
        <th>Company</th><th>Ticker</th><th>Mkt Cap</th><th>Revenue</th>
        <th>Net Margin</th><th>ROE</th><th>P/E</th><th>EV/EBIT</th><th>Sector</th>
      </tr></thead>
      <tbody>{data_rows}</tbody>
    </table>"""

    # ── Failed tickers note ───────────────────────────────────────────────────
    failed_note = ""
    if failed:
        failed_note = (
            f'<p style="font-size:12px;color:#999">'
            f"⚠ Data not available for: {', '.join(failed)}</p>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{fw.name} — {index_ticker} Universe Screen</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">

  <h1>{fw.icon} {fw.name} — {index_ticker} Universe Screen</h1>
  <p class="subtitle">
    Date: {date} &nbsp;·&nbsp; Universe: {n_loaded} companies
    &nbsp;·&nbsp; Framework: {fw.name}
  </p>

  <div class="summary-box">{summary}</div>

  <span class="rec-banner {rec_class}">{rec}</span>

  <h2>Top Picks</h2>
  {picks_table}

  {'<h2>Groups</h2>' + groups_html if groups_html else ''}

  {'<h2>Exclusions</h2>' + excl_table if excl_table else ''}

  <h2>Framework Observations</h2>
  <div class="observations">{obs}</div>

  <h2>Consolidated Data</h2>
  {data_table}
  {failed_note}

  <div class="footer">
    Generated by Your Humble EquityBot · {date} ·
    Framework: {fw.name} · Index: {index_ticker}
  </div>

</div>
</body>
</html>"""

    return html
