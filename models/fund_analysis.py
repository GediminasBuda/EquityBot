"""
fund_analysis.py — LLM prompt builder for the Fund Fundamentals factsheet narrative.

Returns (cacheable_prefix, dynamic_prompt) consumed by LLMClient.generate_json().

The LLM produces a factsheet-style commentary covering:
  - Fund strategy & investment objective
  - Key cost and structural metrics
  - Holdings analysis (concentration, sector tilts, geographic exposure)
  - Risk & performance profile
  - Suitability summary (investor type)
"""

from __future__ import annotations
import json
from typing import Any


def _s(v: Any, fallback: str = "N/A") -> str:
    if v is None or v == "" or v == "NA": return fallback
    return str(v).strip()


def _build_holdings_summary(holdings: dict, limit: int = 30) -> str:
    if not holdings:
        return "No holdings data available."
    lines = []
    items = list(holdings.items())[:limit]
    for code, h in items:
        if isinstance(h, dict):
            name   = h.get("Name") or code
            sector = h.get("Sector") or "—"
            weight = h.get("Assets_%")
            w_str  = f"{float(weight):.2f}%" if weight is not None else "—"
            lines.append(f"  {name} ({sector}): {w_str}")
        else:
            lines.append(f"  {code}: {h}")
    if len(holdings) > limit:
        lines.append(f"  ... and {len(holdings) - limit} more positions")
    return "\n".join(lines)


def build_fund_prompt(bundle: dict) -> tuple[str, str]:
    """
    Returns (cacheable_prefix, dynamic_prompt).

    cacheable_prefix  — static JSON schema + instructions (same for all funds)
    dynamic_prompt    — fund-specific data (changes per ticker)
    """
    fund_type = bundle.get("fund_type", "UNKNOWN")
    f         = bundle.get("fundamentals") or {}
    gen       = f.get("General")         or {}
    etf       = f.get("ETF_Data")        or {}
    mf        = f.get("MutualFund_Data") or {}
    rt        = bundle.get("realtime")   or {}

    # ── cacheable_prefix (schema + instructions) ──────────────────────────────
    cacheable_prefix = """You are a professional fund analyst writing a concise factsheet commentary for an investment research platform.

Analyse the provided fund data and return a JSON object with EXACTLY these fields:

{
  "fund_strategy":      "<2-3 sentences: investment objective, benchmark tracked or active strategy, key philosophy>",
  "holdings_analysis":  "<2-3 sentences: portfolio concentration, dominant sectors/regions, notable positions, diversification quality>",
  "cost_risk_profile":  "<2-3 sentences: expense ratio assessment (cheap/fair/expensive vs peers), beta/volatility, key risk factors>",
  "performance_note":   "<1-2 sentences: performance highlights, returns vs category if available, dividend profile>",
  "suitability":        "<1-2 sentences: which investor type this fund suits, time horizon, portfolio role>",
  "one_liner":          "<single sentence summary — the elevator pitch for this fund>"
}

Rules:
- Use only data provided below. Do NOT invent figures.
- If a field's data is missing, write a best-effort qualitative assessment from what IS available.
- Output raw JSON only — no markdown, no code fences, no extra keys.
- Each value is a plain string (no nested objects).
"""

    # ── dynamic_prompt (fund-specific data) ───────────────────────────────────
    if fund_type == "ETF":
        holdings_raw = etf.get("Holdings") or etf.get("Top_10_Holdings") or {}
        holdings_str = _build_holdings_summary(holdings_raw, limit=40)

        aa   = etf.get("Asset_Allocation")  or {}
        sw   = etf.get("Sector_Weights")    or {}
        wr   = etf.get("World_Regions")     or {}
        perf = etf.get("Performance")       or {}
        vg   = etf.get("Valuations_Growth") or {}

        aa_str  = json.dumps({k: v for k, v in list(aa.items())[:8]},  indent=2) if aa else "N/A"
        sw_str  = json.dumps({k: v for k, v in list(sw.items())[:12]}, indent=2) if sw else "N/A"
        wr_str  = json.dumps({k: v for k, v in list(wr.items())[:12]}, indent=2) if wr else "N/A"

        dynamic_prompt = f"""FUND TYPE: ETF
TICKER: {bundle.get('ticker')}
NAME: {_s(gen.get('Name'))}
EXCHANGE: {_s(gen.get('Exchange'))}
CURRENCY: {_s(gen.get('CurrencyCode'))}
DESCRIPTION: {_s(gen.get('Description') or '')[:500]}

CATEGORY: {_s(etf.get('Category_Name') or gen.get('Category'))}
INCEPTION DATE: {_s(etf.get('Inception_Date'))}
TOTAL NET ASSETS: {_s(etf.get('Total_Net_Assets'))}
HOLDINGS COUNT: {_s(etf.get('Holdings_Count'))}
NET EXPENSE RATIO: {_s(etf.get('Net_Expense_Ratio'))}%
YIELD: {_s(etf.get('Yield'))}%
ANNUAL HOLDINGS TURNOVER: {_s(etf.get('Annual_Holdings_Turnover'))}%
AVERAGE MARKET CAP (MIL): {_s(etf.get('Average_Mkt_Cap_Mil'))}
DIVIDEND FREQUENCY: {_s(etf.get('Dividend_Paying_Frequency'))}

TECHNICALS:
  Beta: {_s((f.get('Technicals') or {}).get('Beta'))}
  52-Week High: {_s((f.get('Technicals') or {}).get('52WeekHigh'))}
  52-Week Low: {_s((f.get('Technicals') or {}).get('52WeekLow'))}
  50-Day MA: {_s((f.get('Technicals') or {}).get('50DayMA'))}
  200-Day MA: {_s((f.get('Technicals') or {}).get('200DayMA'))}
  Last Price: {_s(rt.get('close') or rt.get('previousClose'))}

PERFORMANCE:
  Volatility 1Y: {_s(perf.get('1y_Volatility'))}
  Volatility 3Y: {_s(perf.get('3y_Volatility'))}
  Expected Return 3Y: {_s(perf.get('3y_ExpReturn'))}
  Sharpe Ratio 3Y: {_s(perf.get('3y_SharpRatio'))}
  Return YTD: {_s(perf.get('Returns_YTD'))}%
  Return 1Y: {_s(perf.get('Returns_1Y'))}%
  Return 3Y: {_s(perf.get('Returns_3Y'))}%
  Return 5Y: {_s(perf.get('Returns_5Y'))}%
  Return 10Y: {_s(perf.get('Returns_10Y'))}%

ASSET ALLOCATION:
{aa_str}

SECTOR WEIGHTS:
{sw_str}

WORLD REGIONS:
{wr_str}

TOP HOLDINGS (up to 40):
{holdings_str}

VALUATION & GROWTH (portfolio vs category):
{json.dumps(vg, indent=2)[:600] if vg else 'N/A'}
"""

    else:  # FUND or UNKNOWN
        holdings_raw = mf.get("Top_Holdings") or mf.get("Holdings") or {}
        if isinstance(holdings_raw, list):
            holdings_str = "\n".join(
                f"  {h.get('Name','?')}: {h.get('Weight') or h.get('Assets_%','—')}"
                for h in holdings_raw[:40]
            )
        else:
            holdings_str = _build_holdings_summary(holdings_raw, limit=40)

        aa = mf.get("Asset_Allocation") or {}
        sw = mf.get("Sector_Weights") or mf.get("Sector_Weightings") or {}
        wr = mf.get("World_Regions") or {}
        vg = mf.get("Value_Growth") or mf.get("Valuations_Growth") or {}

        dynamic_prompt = f"""FUND TYPE: MUTUAL FUND
TICKER: {bundle.get('ticker')}
NAME: {_s(gen.get('Name'))}
FUND FAMILY: {_s(mf.get('Fund_Family'))}
CURRENCY: {_s(mf.get('Currency') or gen.get('CurrencyCode'))}
FUND CATEGORY: {_s(mf.get('Fund_Category') or gen.get('Category'))}
FUND STYLE: {_s(mf.get('Fund_Style'))}
INCEPTION DATE: {_s(mf.get('Inception_Date'))}
DOMICILE: {_s(mf.get('Domicile') or gen.get('CountryName'))}
YIELD: {_s(mf.get('Yield'))}%
TOTAL NET ASSETS: {_s(mf.get('Total_Net_Assets') or mf.get('Net_Assets'))}

FUND SUMMARY: {_s(mf.get('Fund_Summary') or gen.get('Description') or '')[:500]}

TECHNICALS:
  Beta: {_s((f.get('Technicals') or {}).get('Beta'))}
  52-Week High: {_s((f.get('Technicals') or {}).get('52WeekHigh'))}
  52-Week Low: {_s((f.get('Technicals') or {}).get('52WeekLow'))}
  Last NAV: {_s(rt.get('close') or rt.get('previousClose'))}

ASSET ALLOCATION:
{json.dumps(aa, indent=2)[:400] if aa else 'N/A'}

SECTOR WEIGHTINGS:
{json.dumps(sw, indent=2)[:600] if sw else 'N/A'}

WORLD REGIONS:
{json.dumps(wr, indent=2)[:400] if wr else 'N/A'}

VALUE & GROWTH MEASURES:
{json.dumps(vg, indent=2)[:500] if vg else 'N/A'}

TOP HOLDINGS (up to 40):
{holdings_str}
"""

    return cacheable_prefix, dynamic_prompt
