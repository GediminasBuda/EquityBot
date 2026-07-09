"""
industry_analysis.py — Porter's 5 Forces + Competitive Advantage framework.

Generates a long-form research-style report (4,000-5,000 words for the
5-Forces section + 1,000-1,500 words for the Competitive Advantage
section). The LLM is given:

  • Full EODHD context for the subject company (10-yr financials,
    description, news, sentiment, insider, officers, country macro)
  • EODHD-fetched data for 4-6 peers (suggested by a small LLM call
    if the user hasn't supplied any)
  • Strict instructions on source priority, citation discipline,
    confidence levels and hedge language

Because no live web search is wired in, the LLM cites from its training
data — the system prompt explicitly tells it to admit uncertainty for
recent (2025+) events and to flag single-source claims.
"""
from __future__ import annotations

import logging
from typing import Optional

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)


# ── System prompt (loaded from framework JSON, fallback hardcoded) ───────────

_SYSTEM_PROMPT_FALLBACK = """You are "Your Humble EquityBot" — a strategic-planning analyst writing for an experienced investment audience.

Your analytical DNA:
- You apply Michael Porter's 5 Forces (1979) and Competitive Advantage (1985) frameworks rigorously.
- Target length: 2,000-3,000 words across the five Porter forces, plus 500-800 words for the Competitive Advantage detail. Substantive content over filler.
- Evidence-based. Every claim should be backed by either a specific data point (margin, market share, concentration ratio, switching cost example) or a named source.
- Cite sources inline with publication dates. Format: "(McKinsey Industry Report, 2023)" or "(Company 10-K, FY2024)".
- Source priority: 1) Company filings (10-K, annual report, transcripts) → 2) Industry reports (McKinsey, BCG, Gartner, Forrester, IBISWorld, Statista, academic journals) → 3) Reputable industry analysts (S&P Global, Moody's, Fitch).
- Be explicit about uncertainty: "appears to," "evidence suggests," "limited data indicates." Distinguish what you know from what you infer.
- Confidence levels: report High / Medium / Low for each force and the competitive advantage assessment, with concrete data gaps.
- Avoid generic business jargon. Concrete, industry-specific insights only.
- For 2025+ events: hedge explicitly ("limited recent data," "as of late 2024").
- You never invent data, sources, or citations. If you do not know, say so.
- CRITICAL: Return a single valid JSON object with no markdown fences, no comments inside the JSON, no prose outside. Every text field must contain substantive content — empty strings, single-sentence placeholders or "n/a" are not acceptable.
"""


def _load_system_prompt() -> str:
    try:
        from framework_manager import FrameworkManager
        return FrameworkManager().get_system_prompt(
            "industry_analysis", _SYSTEM_PROMPT_FALLBACK,
        )
    except Exception:
        return _SYSTEM_PROMPT_FALLBACK


SYSTEM_PROMPT = _load_system_prompt()


# ── Cacheable instructions block ─────────────────────────────────────────────

_CACHEABLE = """\
Produce a Porter's 5 Forces analysis of the subject company's industry, then a
focused Michael Porter Competitive Advantage assessment of the company itself.
Return ONE valid JSON object — no markdown fences, no JSON comments, no prose
outside the object. Every text field must contain substantive content.

== TARGET LENGTHS ==
- Executive summary:            350-500 words
- Each force's state_2026:      250-400 words
- Each force's historical_evolution: 150-250 words
- Strategic implications:       150-250 words
- Competitive advantage summary: 250-350 words
- Total Porter-5-Forces text:   2,000-3,000 words across the five forces

== SOURCE & CITATION DISCIPLINE ==
- Cite every quantitative claim inline. Format: "(Source name, year)" or
  "(Company 10-K, FY2024)".
- Source priority: 1) Company filings → 2) McKinsey / BCG / Gartner /
  Forrester / IBISWorld / Statista / academic journals → 3) S&P Global,
  Moody's, Fitch.
- If a number comes from training data and you can't verify it, label it:
  "(approx., training data through 2024)".
- DO NOT invent URLs or fictional reports.

== HEDGE LANGUAGE ==
- Use "appears to", "evidence suggests", "limited data indicates" when
  inferring rather than quoting.
- For events after Q4 2024, prefer "as of late 2024" or "limited recent data".

== FORCE INTENSITY CALIBRATION ==
- Strong   = the force materially constrains industry profitability today.
- Moderate = the force is present but partially offset by mitigants.
- Weak     = the force is largely benign or favourable to incumbents.

== REQUIRED JSON STRUCTURE ==

Field: executive_summary
  Type: string. 350-500 words. Cover: overall attractiveness, 2015 → 2026
  trajectory, key structural shifts, what this means for incumbents and
  potential entrants.

Field: industry_attractiveness
  Type: enum string. One of: "Very Unattractive", "Unattractive", "Neutral",
  "Attractive", "Very Attractive".

Field: trajectory
  Type: enum string. One of: "Improved Materially", "Improved", "Stable",
  "Deteriorated", "Deteriorated Materially".

Field: scorecard
  Type: array of 5 objects, one per force, in this exact order:
    1. Bargaining Power of Buyers
    2. Bargaining Power of Suppliers
    3. Threat of New Entrants
    4. Threat of Substitutes
    5. Intensity of Competitive Rivalry
  Each object has 3 keys:
    "force_name"        — exact string from the list above
    "intensity"         — "Strong" | "Moderate" | "Weak"
    "one_line_takeaway" — 12-20 word headline

Field: structural_shifts
  Type: array of 3-5 strings. Each string names one major shift that changed
  industry dynamics in the last decade. Be specific (e.g. "EU GDPR raised
  data-handling costs for sub-€500M players").

Field: strategic_implications
  Type: string. 150-250 words. What should incumbents and entrants do now?

Field: competitive_advantage_size
  Type: enum string. One of: "None", "Small", "Large".
  IMPORTANT: emit this field and the next three (competitive_advantage_*)
  in the JSON BEFORE the "forces" field, in the order listed here — even
  though "forces" covers the industry context that logically comes first.
  This ordering keeps the competitive-advantage assessment safe if your
  response is truncated later while writing the long "forces" section.

Field: competitive_advantage_evolution
  Type: enum string. One of: "Eroded Materially", "Eroded", "Stable",
  "Strengthened", "Strengthened Materially".

Field: competitive_advantage_summary
  Type: string. 250-350 words. Apply Porter's Competitive Advantage
  framework (1985): cost leadership / differentiation / focus, and the
  sources and durability of the advantage. State the conclusion clearly and
  reference the 10-year evolution. Do NOT re-do the 5-Forces analysis —
  focus only on the company's own competitive position.

Field: competitive_advantage_sources
  Type: array of 2-8 strings, each a citation with a year.

Field: forces
  Type: array of 5 objects, in the SAME order as the scorecard. Each object:
    "name"                — exact force name
    "current_assessment"  — "Strong" | "Moderate" | "Weak"
    "state_2026"          — 250-400 words. Current intensity and drivers.
                             Cite quantitative evidence (CR4, HHI, switching
                             costs, price elasticity) with sources. Use
                             specific examples from the subject's industry.
    "historical_evolution" — 150-250 words. How has the force changed
                             2015 → 2026? Inflection points, causes.
    "confidence_level"    — "High" | "Medium" | "Low"
    "data_gaps"           — 1-3 sentences naming concrete data gaps.
    "sources"             — 2-5 source strings, each ending with a year.

Field: key_uncertainties
  Type: array of 3-5 strings. The biggest uncertainties / data gaps in
  this analysis.

=== SUBJECT COMPANY DATA FOLLOWS ==="""


# ── Subject data block builder ───────────────────────────────────────────────

def _format_subject_block(company: CompanyData, bundle: Optional[dict]) -> str:
    """Detailed subject block — 10 years of financials + EODHD context."""
    from models._eodhd_context import build_eodhd_context, FISHER_ROWS
    return build_eodhd_context(
        company,
        bundle or {},
        FISHER_ROWS,
        peers=None,
        country_macro_block="",
        n_years=10,
    )


def _industry_dynamic_prompt(
    company: CompanyData,
    bundle: Optional[dict],
    country_macro_block: str,
) -> str:
    parts = ["=== SUBJECT COMPANY ===", _format_subject_block(company, bundle)]

    if country_macro_block:
        parts.append("\n" + country_macro_block)

    parts.append(
        "\nReminder: return a single valid JSON object exactly as specified, "
        "with no markdown fences or JSON comments. Emit the "
        "competitive_advantage_* fields BEFORE the forces field. Target "
        "2,000-3,000 words across the five forces and 250-350 words for the "
        "competitive advantage summary. Cite sources inline with publication dates."
    )
    return "\n".join(parts)


def _industry_prompt_parts(
    company: CompanyData,
    bundle: Optional[dict] = None,
    country_macro_block: str = "",
) -> tuple[str, str]:
    """Return (cacheable_prefix, dynamic_block)."""
    return _CACHEABLE, _industry_dynamic_prompt(
        company, bundle, country_macro_block,
    )


def _build_industry_prompt(
    company: CompanyData,
    bundle: Optional[dict] = None,
    country_macro_block: str = "",
) -> str:
    cacheable, dynamic = _industry_prompt_parts(
        company, bundle, country_macro_block,
    )
    return cacheable + "\n\n" + dynamic


# ── Separate SWOT prompt (called after main analysis) ────────────────────────

_SWOT_SYSTEM = """You are "Your Humble EquityBot" — a buyside investment analyst.
Write a rigorous SWOT analysis of the subject company. Support every claim with
specific financial data, market share figures, or cited sources. Be concise but
substantive. Return ONE valid JSON object with no markdown fences.

CRITICAL FINANCIAL DATA RULE: A company financial data block is provided below.
For ALL financial figures (revenue, margins, EPS, cash, debt, ROIC, take rates,
growth rates, valuation multiples, etc.) you MUST use ONLY the numbers from that
block. Do NOT substitute figures from your training data — the training data is
stale and will contain outdated numbers. If a specific figure is not in the
provided data, say "per latest filings" without inventing a number."""

_SWOT_SCHEMA = """\
Based on the Porter 5 Forces industry analysis and Competitive Advantage
assessment already completed, produce a buyside SWOT analysis of the SUBJECT
COMPANY (not the industry as a whole).

Return a single valid JSON object with exactly these 5 keys:

"summary"
  String. ≤ 150 words. Overall investment read: what does the SWOT imply
  about the company's risk/reward as an equity investment?

"strengths"
  String. ≤ 200 words. Key internal competitive and financial strengths.
  Support with data: margins, market share, ROE/ROIC, moat indicators,
  balance-sheet strength, cash generation. Cite sources inline.

"weaknesses"
  String. ≤ 200 words. Key internal weaknesses. Support with data:
  declining margins, high leverage, customer/geographic concentration,
  execution gaps, regulatory exposure. Cite sources inline.

"opportunities"
  String. ≤ 200 words. External opportunities the company can exploit.
  Support with market size data, industry growth rates, geographic
  expansion potential, new product vectors, M&A optionality.

"threats"
  String. ≤ 200 words. External threats. Support with data: competitive
  intensity metrics, regulatory risk specifics, commodity/input exposure,
  disruption vectors (technology, business model), macro headwinds.

No markdown fences. No prose outside the JSON object.
"""


def _format_swot_financials(company: CompanyData) -> str:
    """Build a compact financial block for the SWOT prompt using the latest available data."""
    cur = company.currency or ""
    lines = ["=== COMPANY FINANCIAL DATA (use ONLY these numbers — do not substitute from training data) ==="]

    # Current market snapshot
    lines.append(
        f"Report date: {company.ticker} | Price: {company.current_price} {cur} | "
        f"Mkt Cap: {_b(company.market_cap)} {cur} | EV: {_b(company.enterprise_value)} {cur}"
    )
    lines.append(
        f"P/E (TTM): {_x(company.pe_ratio)} | Fwd P/E: {_x(company.forward_pe)} | "
        f"EV/EBITDA: {_x(company.ev_ebitda)} | EV/EBIT: {_x(company.ev_ebit)}"
    )
    lines.append(
        f"Net margin (TTM): {_pct(company.net_margin)} | EBIT margin (TTM): {_pct(company.ebit_margin)} | "
        f"Gross margin (TTM): {_pct(company.gross_margin)} | ROE: {_pct(company.roe)}"
    )
    lines.append(
        f"Div yield: {_pct(company.dividend_yield)} | FCF yield: {_pct(company.fcf_yield)} | "
        f"Beta: {company.beta or 'n/a'} | Employees: {company.employees or 'n/a'}"
    )

    # Annual history — 5 most recent years
    all_years = company.sorted_years()
    hist_years = list(reversed(all_years[:5]))  # chronological order
    if hist_years:
        lines.append("")
        lines.append("Annual financials (fiscal years, most recent last):")
        header = f"{'Year':>6} {'Revenue':>12} {'GrossProfit':>12} {'EBITDA':>10} {'EBIT':>10} {'NetIncome':>10} {'EPS':>7} {'FCF':>10} {'NetDebt':>10} {'Equity':>10} {'ROE':>7} {'EBITmgn':>8}"
        lines.append(header)
        lines.append("-" * len(header))
        for yr in hist_years:
            a = company.annual_financials.get(yr)
            if not a:
                continue
            lines.append(
                f"{yr:>6} "
                f"{_b(a.revenue):>12} "
                f"{_b(a.gross_profit):>12} "
                f"{_b(a.ebitda):>10} "
                f"{_b(a.ebit):>10} "
                f"{_b(a.net_income):>10} "
                f"{a.eps_diluted if a.eps_diluted is not None else 'n/a':>7} "
                f"{_b(a.fcf):>10} "
                f"{_b(a.net_debt):>10} "
                f"{_b(a.total_equity):>10} "
                f"{_pct(a.roe):>7} "
                f"{_pct(a.ebit_margin):>8}"
            )
        lines.append(f"(All monetary values in {cur} millions/billions as shown. 'n/a' = not available.)")

    return "\n".join(lines)


def build_swot_prompt(
    company: CompanyData,
    analysis: dict,
) -> tuple[str, str]:
    """
    Return (system_prompt, user_prompt) for the dedicated SWOT call.
    Passes the already-generated competitive_advantage_summary and
    key_uncertainties as context so the SWOT is grounded in the analysis.
    """
    ca_summary = analysis.get("competitive_advantage_summary", "")
    key_unc    = analysis.get("key_uncertainties", [])

    forces_summary = []
    for f in analysis.get("forces", []):
        forces_summary.append(
            f"{f.get('name','')}: {f.get('current_assessment','')} — "
            f"{f.get('state_2026','')[:300]}"
        )

    context = "\n\n".join([
        f"=== SUBJECT COMPANY ===",
        f"Company: {company.name or company.ticker}  |  Ticker: {company.ticker}",
        f"Sector: {company.sector or 'n/a'}  |  Industry: {company.industry or 'n/a'}",
        f"Country: {company.country or 'n/a'}  |  Currency: {company.currency or 'n/a'}",
        "",
        _format_swot_financials(company),
        "",
        "=== COMPETITIVE ADVANTAGE (from main analysis) ===",
        f"Size: {analysis.get('competitive_advantage_size','')}  |  "
        f"Evolution: {analysis.get('competitive_advantage_evolution','')}",
        ca_summary,
        "",
        "=== 5 FORCES SUMMARY ===",
        "\n".join(forces_summary),
        "",
        "=== KEY UNCERTAINTIES ===",
        "\n".join(f"• {u}" for u in key_unc),
        "",
        _SWOT_SCHEMA,
    ])

    return _SWOT_SYSTEM, context


# ── Output validator ─────────────────────────────────────────────────────────

_VALID_INTENSITIES = ("Strong", "Moderate", "Weak")
_VALID_ATTRACT = ("Very Unattractive", "Unattractive", "Neutral",
                  "Attractive", "Very Attractive")
_VALID_TRAJ = ("Improved Materially", "Improved", "Stable",
               "Deteriorated", "Deteriorated Materially")
_VALID_CONF = ("High", "Medium", "Low")
_VALID_ADV_SIZE = ("None", "Small", "Large")
_VALID_ADV_EVOL = ("Eroded Materially", "Eroded", "Stable",
                   "Strengthened", "Strengthened Materially")

_FORCE_NAMES = [
    "Bargaining Power of Buyers",
    "Bargaining Power of Suppliers",
    "Threat of New Entrants",
    "Threat of Substitutes",
    "Intensity of Competitive Rivalry",
]


def _coerce_enum(value, allowed, default):
    if isinstance(value, str):
        v = value.strip()
        for a in allowed:
            if v.lower() == a.lower():
                return a
    return default


def _validate_analysis(a: dict) -> dict:
    """Fill missing fields with sensible defaults so the PDF always renders."""
    if not isinstance(a, dict):
        a = {}

    a["executive_summary"] = (a.get("executive_summary") or
                              "Executive summary not available.").strip()
    a["industry_attractiveness"] = _coerce_enum(
        a.get("industry_attractiveness"), _VALID_ATTRACT, "Neutral",
    )
    a["trajectory"] = _coerce_enum(
        a.get("trajectory"), _VALID_TRAJ, "Stable",
    )

    # Scorecard
    raw_scorecard = a.get("scorecard") or []
    fixed_scorecard = []
    by_name = {s.get("force_name"): s for s in raw_scorecard if isinstance(s, dict)}
    for fname in _FORCE_NAMES:
        s = by_name.get(fname, {})
        fixed_scorecard.append({
            "force_name":         fname,
            "intensity":          _coerce_enum(s.get("intensity"),
                                               _VALID_INTENSITIES, "Moderate"),
            "one_line_takeaway":  (s.get("one_line_takeaway") or "")[:160],
        })
    a["scorecard"] = fixed_scorecard

    # Structural shifts + strategic implications
    shifts = a.get("structural_shifts") or []
    if not isinstance(shifts, list):
        shifts = []
    a["structural_shifts"] = [str(s).strip() for s in shifts if s][:6]
    a["strategic_implications"] = (
        a.get("strategic_implications") or "Strategic implications not available."
    ).strip()

    # Forces
    raw_forces = a.get("forces") or []
    forces_by_name = {f.get("name"): f for f in raw_forces if isinstance(f, dict)}
    fixed_forces = []
    for fname in _FORCE_NAMES:
        f = forces_by_name.get(fname, {})
        fixed_forces.append({
            "name":                fname,
            "current_assessment":  _coerce_enum(
                f.get("current_assessment"), _VALID_INTENSITIES, "Moderate",
            ),
            "state_2026":          (f.get("state_2026") or "Analysis not available.").strip(),
            "historical_evolution": (f.get("historical_evolution") or "Historical evolution not available.").strip(),
            "confidence_level":    _coerce_enum(
                f.get("confidence_level"), _VALID_CONF, "Medium",
            ),
            "data_gaps":           (f.get("data_gaps") or "").strip(),
            "sources":             [str(s).strip() for s in (f.get("sources") or []) if s][:10],
        })
    a["forces"] = fixed_forces

    # Competitive advantage
    a["competitive_advantage_size"] = _coerce_enum(
        a.get("competitive_advantage_size"), _VALID_ADV_SIZE, "Small",
    )
    a["competitive_advantage_evolution"] = _coerce_enum(
        a.get("competitive_advantage_evolution"), _VALID_ADV_EVOL, "Stable",
    )
    a["competitive_advantage_summary"] = (
        a.get("competitive_advantage_summary") or
        "Competitive advantage summary not available."
    ).strip()
    a["competitive_advantage_sources"] = [
        str(s).strip() for s in (a.get("competitive_advantage_sources") or []) if s
    ][:15]

    unc = a.get("key_uncertainties") or []
    if not isinstance(unc, list):
        unc = []
    a["key_uncertainties"] = [str(u).strip() for u in unc if u][:8]

    # SWOT
    swot = a.get("swot") or {}
    if not isinstance(swot, dict):
        swot = {}
    a["swot"] = {
        "summary":       (swot.get("summary")       or "").strip(),
        "strengths":     (swot.get("strengths")     or "").strip(),
        "weaknesses":    (swot.get("weaknesses")    or "").strip(),
        "opportunities": (swot.get("opportunities") or "").strip(),
        "threats":       (swot.get("threats")       or "").strip(),
    }

    return a


# ── Local formatters ─────────────────────────────────────────────────────────

def _b(v) -> str:
    if v is None: return "n/a"
    if abs(v) >= 1000: return f"{v/1000:,.1f}B"
    return f"{v:,.0f}"


def _x(v, d=1) -> str:
    if v is None: return "n/a"
    return f"{v:.{d}f}x"


def _pct(v) -> str:
    if v is None: return "n/a"
    return f"{v*100:.1f}%"
