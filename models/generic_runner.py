"""
generic_runner.py — Runs any FrameworkConfig against a ticker.

Used for user-created and forked frameworks.  Built-in frameworks
(overview, fisher, gravity) continue to use their own model runners.

Pipeline:
  1. Fetch company data  (DataManager)
  2. Build prompt        (substitute placeholders in framework.prompt_template)
  3. Call LLM            (always Claude, per design decision)
  4. Render HTML report  (report_generic.render_html)
  5. Save HTML to outputs/

Placeholder substitution:
  {financials}          Full multi-year financial context block
  {forward_estimates}   Analyst consensus estimates block (if available)
  {company_name}        Company display name
  {ticker}              Ticker symbol
  {currency}            Reporting currency
  {sector}              Sector
  {industry}            Industry
  {country}             Country
  {current_price}       Last traded price
  {market_cap}          Market cap in millions
  {enterprise_value}    EV in millions
  {pe_ratio}            Trailing P/E
  {forward_pe}          Forward P/E
  {ev_ebitda}           EV/EBITDA
  {ev_sales}            EV/Sales
  {dividend_yield}      Dividend yield %
  {fcf_yield}           FCF yield %
  {roe}                 Return on equity TTM
  {ebit_margin}         EBIT margin TTM
  {net_margin}          Net margin TTM
  {revenue_cagr_3y}     3-year revenue CAGR
  {revenue_cagr_5y}     5-year revenue CAGR
  {description}         Business description
  {employees}           Number of employees
  {website}             Company website

Usage:
    from models.generic_runner import GenericRunner
    from framework_manager import FrameworkManager

    fw   = FrameworkManager().get("my_framework_abc123")
    path = GenericRunner().run("WKL.AS", fw)
    print(f"Report saved: {path}")
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class GenericRunner:
    """
    Executes any FrameworkConfig against a ticker symbol.
    Always uses Claude for LLM calls (independent of LLM_PROVIDER setting).
    Returns the path to the generated HTML report.
    """

    def __init__(self):
        from data_sources.data_manager import DataManager
        from config import ANTHROPIC_API_KEY, LLM_MODEL

        self.dm  = DataManager()
        self._api_key = ANTHROPIC_API_KEY
        self._model   = LLM_MODEL   # or fall back to claude-sonnet-4-5

    def run(
        self,
        ticker: str,
        framework,                          # FrameworkConfig
        peer_tickers: Optional[list] = None,
        force_refresh: bool = False,
        output_path: Optional[str] = None,
    ) -> str:
        """
        Full pipeline. Returns path to the generated HTML report.

        Args:
            ticker:        Yahoo Finance ticker
            framework:     FrameworkConfig (must not have is_builtin=True)
            peer_tickers:  Override peer list (optional)
            force_refresh: Bypass data cache
            output_path:   Custom output path (auto-generated if omitted)
        """
        from config import OUTPUTS_DIR

        logger.info(f"[GenericRunner] Starting '{framework.name}' for {ticker}")

        # ── Step 1: Data ──────────────────────────────────────────────────────
        print(f"  [1/4] Fetching data for {ticker}…")
        company = self.dm.get(ticker, force_refresh=force_refresh)

        if not company.name:
            raise ValueError(
                f"No data found for ticker '{ticker}'. "
                f"Check the ticker format (e.g. WKL.AS, AAPL, NOKIA.HE)."
            )
        print(f"         {company.name} | {company.year_range()} | "
              f"{company.completeness_pct()}% complete")

        # ── Step 2: Build prompt ──────────────────────────────────────────────
        print(f"  [2/4] Building prompt for framework '{framework.name}'…")
        prompt = _build_prompt(framework.prompt_template, company)

        # ── Step 3: LLM call (always Claude) ─────────────────────────────────
        print(f"  [3/4] Calling Claude ({self._model})…")
        analysis = self._call_claude(
            user_prompt=prompt,
            system_prompt=framework.system_prompt,
            max_tokens=5000,
        )
        if not analysis:
            raise RuntimeError(
                "LLM returned an empty response. "
                "Check that ANTHROPIC_API_KEY is set and the prompt template is valid."
            )

        rec = analysis.get("recommendation", "n/a") if isinstance(analysis, dict) else "n/a"
        print(f"         Recommendation: {rec}")

        # ── Step 4: Peer data (optional) ──────────────────────────────────────
        peers = {}
        raw_peers = peer_tickers or [
            p.get("ticker", "")
            for p in (analysis.get("suggested_peers", []) if isinstance(analysis, dict) else [])
        ]
        raw_peers = [t.strip().upper() for t in raw_peers if t.strip()][:6]
        if raw_peers:
            print(f"  [3b/4] Fetching {len(raw_peers)} peers…")
            for pt in raw_peers:
                try:
                    pd = self.dm.get(pt)
                    if pd.name:
                        peers[pt] = pd
                except Exception:
                    pass

        # ── Step 5: Render HTML ───────────────────────────────────────────────
        print(f"  [4/4] Rendering HTML report…")
        from agents.report_generic import render_html
        html = render_html(framework, analysis, company=company, peers=peers or None)

        # ── Save ──────────────────────────────────────────────────────────────
        if not output_path:
            from utils.report_naming import report_file_stem
            safe     = report_file_stem(ticker, company.name)
            fw_slug  = re.sub(r"[^a-z0-9]+", "_", framework.name.lower()).strip("_")[:20]
            date_str = datetime.now().strftime("%Y-%m-%d")
            fname    = f"{safe}_{fw_slug}_{date_str}.html"
            output_path = str(OUTPUTS_DIR / fname)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"\n  Report saved: {output_path}")
        logger.info(f"[GenericRunner] Done. HTML at {output_path}")
        return output_path

    # ── LLM call ──────────────────────────────────────────────────────────────

    def _call_claude(
        self, user_prompt: str, system_prompt: str, max_tokens: int = 5000
    ) -> dict:
        """
        Always calls Claude directly (not via LLMClient provider routing).
        Parses JSON from the response.
        """
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. "
                "The Framework Studio always uses Claude — add the key to .env."
            )
        try:
            import anthropic
        except ImportError:
            raise ImportError("Run: pip install anthropic")

        client = anthropic.Anthropic(api_key=self._api_key)

        json_instruction = (
            "\n\nIMPORTANT: Return ONLY valid JSON. "
            "No markdown, no code blocks, no commentary before or after the JSON."
        )

        try:
            msg = client.messages.create(
                model=self._model or "claude-sonnet-4-5",
                max_tokens=max_tokens,
                temperature=0.3,
                system=system_prompt or "",
                messages=[{"role": "user", "content": user_prompt + json_instruction}],
            )
            raw = msg.content[0].text
        except anthropic.AuthenticationError:
            raise RuntimeError("Invalid ANTHROPIC_API_KEY. Check your key.")
        except anthropic.RateLimitError:
            raise RuntimeError("Anthropic rate limit hit. Wait and retry.")
        except Exception as e:
            raise RuntimeError(f"Claude API error: {e}")

        return _parse_json(raw)


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(template: str, company) -> str:
    """
    Substitute all {placeholders} in the prompt template with real company data.
    Unknown placeholders are left as-is (the LLM sees them literally).
    """
    from data_sources.base import CompanyData

    cur = company.currency or "USD"

    # Build placeholder values
    subs = {
        "{company_name}":     company.name or company.ticker,
        "{ticker}":           company.ticker or "",
        "{currency}":         cur,
        "{sector}":           company.sector or "n/a",
        "{industry}":         company.industry or "n/a",
        "{country}":          company.country or "n/a",
        "{description}":      (company.description or "")[:800],
        "{employees}":        f"{company.employees:,}" if company.employees else "n/a",
        "{website}":          company.website or "n/a",
        "{current_price}":    f"{company.current_price} {company.currency_price or cur}" if company.current_price else "n/a",
        "{market_cap}":       _fmt_m(company.market_cap),
        "{enterprise_value}": _fmt_m(company.enterprise_value),
        "{pe_ratio}":         _fmt_x(company.pe_ratio),
        "{forward_pe}":       _fmt_x(company.forward_pe),
        "{ev_ebitda}":        _fmt_x(company.ev_ebitda),
        "{ev_sales}":         _fmt_x(company.ev_sales),
        "{dividend_yield}":   _fmt_pct(company.dividend_yield),
        "{fcf_yield}":        _fmt_pct(company.fcf_yield),
        "{roe}":              _fmt_pct(company.roe),
        "{ebit_margin}":      _fmt_pct(company.ebit_margin),
        "{net_margin}":       _fmt_pct(company.net_margin),
        "{revenue_cagr_3y}":  _fmt_pct(company.revenue_cagr(3)),
        "{revenue_cagr_5y}":  _fmt_pct(company.revenue_cagr(5)),
        "{financials}":       _build_financials_block(company),
        "{forward_estimates}": _build_estimates_block(company),
        "{macro_context}":    _build_macro_block(),
    }

    result = template
    for placeholder, value in subs.items():
        result = result.replace(placeholder, str(value))

    return result


def _build_financials_block(company) -> str:
    """Assemble the multi-year financial context block (same style as overview model)."""
    cur = company.currency or "USD"
    lines = []

    lines.append(f"COMPANY: {company.name or company.ticker} ({company.ticker})")
    lines.append(f"SECTOR:  {company.sector or 'n/a'} — {company.industry or 'n/a'}")
    lines.append(f"COUNTRY: {company.country or 'n/a'} | CURRENCY: {cur}")
    if company.description:
        desc = company.description[:600]
        lines.append(f"\nBUSINESS: {desc}{'...' if len(company.description) > 600 else ''}")

    lines.append(f"\nMARKET DATA (as of {company.as_of_date or 'n/a'}):")
    lines.append(f"  Price:           {company.current_price} {company.currency_price or cur}")
    lines.append(f"  Market Cap:      {_fmt_m(company.market_cap)} {cur}M")
    lines.append(f"  Enterprise Value:{_fmt_m(company.enterprise_value)} {cur}M")

    lines.append(f"\nVALUATION:")
    lines.append(f"  P/E:        {_fmt_x(company.pe_ratio)}  | Fwd P/E:   {_fmt_x(company.forward_pe)}")
    lines.append(f"  EV/EBIT:    {_fmt_x(company.ev_ebit)}  | EV/EBITDA: {_fmt_x(company.ev_ebitda)}")
    lines.append(f"  EV/Sales:   {_fmt_x(company.ev_sales)} | P/Book:    {_fmt_x(company.price_to_book)}")
    lines.append(f"  Div Yield:  {_fmt_pct(company.dividend_yield)} | FCF Yield: {_fmt_pct(company.fcf_yield)}")

    lines.append(f"\nPROFITABILITY (TTM):")
    lines.append(f"  Gross Margin:  {_fmt_pct(company.gross_margin)}")
    lines.append(f"  EBIT Margin:   {_fmt_pct(company.ebit_margin)}")
    lines.append(f"  Net Margin:    {_fmt_pct(company.net_margin)}")
    lines.append(f"  ROE:           {_fmt_pct(company.roe)}")
    lines.append(f"  Net Debt:      {_fmt_m(company.net_debt)} {cur}M | Gearing: {_fmt_x(company.gearing)}")

    years = company.sorted_years()[:6]
    if years:
        lines.append(f"\nANNUAL FINANCIALS ({cur}M, most recent first):")
        lines.append("  {:22} ".format("") + " ".join(f"{y:>10}" for y in years))
        lines.append("  " + "-" * (22 + 11 * len(years)))

        def row(label, getter, fmt="M"):
            vals = []
            for y in years:
                af = company.annual_financials.get(y)
                v  = getter(af) if af else None
                if fmt == "M":
                    vals.append(f"{v/1000:>9.1f}B" if v and abs(v) >= 1000
                                else (f"{v:>9.0f}M" if v is not None else "      n/a"))
                elif fmt == "%":
                    vals.append(f"{v*100:>9.1f}%" if v is not None else "      n/a")
                elif fmt == "x":
                    vals.append(f"{v:>9.1f}x" if v is not None else "      n/a")
                elif fmt == "ps":
                    vals.append(f"{v:>9.2f}" if v is not None else "      n/a")
            return "  {:<22} ".format(label) + " ".join(vals)

        lines.append(row("Revenue",       lambda a: a.revenue))
        lines.append(row("EBITDA",        lambda a: a.ebitda))
        lines.append(row("EBIT",          lambda a: a.ebit))
        lines.append(row("Net Income",    lambda a: a.net_income))
        lines.append(row("EPS (diluted)", lambda a: a.eps_diluted, "ps"))
        lines.append(row("FCF",           lambda a: a.fcf))
        lines.append(row("Net Debt",      lambda a: a.net_debt))
        lines.append(row("ROE",           lambda a: a.roe, "%"))
        lines.append(row("EBIT Margin",   lambda a: a.ebit_margin, "%"))
        lines.append(row("Net Margin",    lambda a: a.net_margin, "%"))

    c3 = company.revenue_cagr(3)
    c5 = company.revenue_cagr(5)
    lines.append(f"\nREVENUE CAGR: 3yr={_fmt_pct(c3)}  5yr={_fmt_pct(c5)}")

    macro = _build_macro_block()
    if macro:
        lines.append(f"\n{macro}")

    return "\n".join(lines)


def _build_macro_block() -> str:
    """Fetch and return the FRED macro context block (empty string on failure)."""
    try:
        from data_sources.fred_adapter import get_macro_block
        return get_macro_block()
    except Exception:
        return ""


def _build_estimates_block(company) -> str:
    """Build the forward estimates context block if available."""
    fe = company.forward_estimates
    if fe is None:
        return "No analyst consensus estimates available."

    cur = company.currency or "USD"
    lines = [f"ANALYST CONSENSUS ESTIMATES ({fe.year}E, {fe.analyst_count or '?'} analysts):"]

    if fe.revenue is not None:
        growth = f"  (growth: {_fmt_pct(fe.revenue_growth_yoy)})" if fe.revenue_growth_yoy else ""
        lines.append(f"  Revenue {fe.year}E:    {_fmt_m(fe.revenue)} {cur}M{growth}")
    if fe.eps_diluted is not None:
        growth = f"  (growth: {_fmt_pct(fe.eps_growth_yoy)})" if fe.eps_growth_yoy else ""
        lines.append(f"  EPS {fe.year}E:        {fe.eps_diluted:.2f}{growth}")
    if fe.net_income is not None:
        lines.append(f"  Net Income {fe.year}E: {_fmt_m(fe.net_income)} {cur}M")
    if fe.pe_ratio is not None:
        lines.append(f"  Forward P/E:       {_fmt_x(fe.pe_ratio)}")
    if fe.ev_sales is not None:
        lines.append(f"  Fwd EV/Sales:      {_fmt_x(fe.ev_sales)}")

    return "\n".join(lines)


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """Parse JSON from LLM output, stripping code fences if present."""
    import json
    import re as _re

    text = raw.strip()
    # Strip markdown code fences
    text = _re.sub(r"^`{3,}(?:json|JSON)?\s*\n?", "", text, flags=_re.MULTILINE)
    text = _re.sub(r"\n?`{3,}\s*$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Find first { to last }
        start = text.find("{")
        end   = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        logger.error(f"[GenericRunner] JSON parse failed. Raw (first 300):\n{raw[:300]}")
        return {}


# ── Formatters ────────────────────────────────────────────────────────────────

def _fmt_m(v) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:.0f}M"

def _fmt_x(v) -> str:
    return f"{v:.1f}x" if v is not None else "n/a"

def _fmt_pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "n/a"
