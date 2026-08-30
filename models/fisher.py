"""
fisher.py — Fisher Alternatives model.

Produces a 3-page deep-dive PDF covering:
  Page 1: Business Overview + Philip Fisher 15-Point Scorecard (pts 1-8)
  Page 2: Fisher Points 9-15 + Score Summary + Helmer 7 Powers
  Page 3: Moat Assessment + Key Risks + Investment Conclusion + Recommendation

Framework references:
  - Philip Fisher, "Common Stocks and Uncommon Profits" (1958) — 15 qualitative tests
  - Hamilton Helmer, "7 Powers: The Foundations of Business Strategy" (2016)

Usage:
    from models.fisher import FisherModel
    path = FisherModel().run("WKL.AS")

CLI:
    python models/fisher.py WKL.AS
    python models/fisher.py AAPL
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from data_sources.data_manager import DataManager
from data_sources.base import CompanyData
from agents.llm_client import LLMClient
from config import OUTPUTS_DIR, ADVERSARIAL_MODE

logger = logging.getLogger(__name__)


# ── System prompt: loaded from framework JSON, fallback to hardcoded ──────────

_SYSTEM_PROMPT_FALLBACK = """You are "Your Humble EquityBot" — a disciplined, forensic business analyst
specialising in qualitative assessment of competitive moats and management quality.

Your analytical DNA:
- You apply Philip Fisher's 15-point scuttlebutt framework rigorously.
  Each of the 15 points receives an honest score — you do not pad weak points.
- You apply Hamilton Helmer's 7 Powers framework to assess structural advantages.
  A power only counts if it is genuinely durable and quantifiably significant.
- You do not confuse a good business with a wide-moat business.
- Your scores are calibrated: most companies score in the middle of the range.
  Exceptional companies (5/5) and poor ones (1/5) are rare and must be earned.
- You are a Devil's Advocate. For every strength, you ask: is this already priced in?
  For every moat, you ask: what erodes it over a decade?
- Precision over impressionism. Cite specific financials when scoring each point.
- You never invent data. When the data provided is insufficient, you say so explicitly.
"""


def _load_system_prompt() -> str:
    try:
        from framework_manager import FrameworkManager
        return FrameworkManager().get_system_prompt("fisher", _SYSTEM_PROMPT_FALLBACK)
    except Exception:
        return _SYSTEM_PROMPT_FALLBACK


SYSTEM_PROMPT = _load_system_prompt()


# ── Fisher 15 Questions (canonical) ──────────────────────────────────────────

FISHER_QUESTIONS = [
    (1,  "Market Growth Potential",
         "Does the company serve a large and growing addressable market? "
         "Can it grow sales materially for at least the next several years?"),
    (2,  "Management Innovation Drive",
         "Does management have the determination and capability to develop "
         "new products, services, or processes that will sustain long-term growth?"),
    (3,  "R&D Effectiveness",
         "How productive is R&D relative to the company's size? "
         "Does innovation translate into commercially successful new offerings?"),
    (4,  "Sales Organisation Quality",
         "Does the company have an above-average sales force? "
         "Can it effectively reach customers and convert its product advantage?"),
    (5,  "Profit Margin Quality",
         "Are profit margins above the industry average? "
         "Are they sustainable — not a temporary cyclical peak?"),
    (6,  "Margin Improvement Discipline",
         "What is management actively doing to maintain or improve margins? "
         "Is there a credible programme of cost efficiency and pricing power?"),
    (7,  "Labour & Personnel Relations",
         "Does the company treat employees well? "
         "Is there evidence of low churn, strong culture, or employer-of-choice status?"),
    (8,  "Executive & Leadership Relations",
         "Do senior executives work well together? "
         "Is there evidence of stability, low C-suite turnover, and coherent strategy?"),
    (9,  "Management Depth",
         "Does the company have capable management beyond the top two or three people? "
         "Would it survive the departure of the CEO?"),
    (10, "Cost Analysis & Accounting Quality",
         "How rigorous are the company's internal cost controls and financial reporting? "
         "Are there any accounting red flags or aggressive revenue recognition practices?"),
    (11, "Industry-Specific Competitive Edge",
         "Are there aspects of this specific industry or business model — "
         "network effects, switching costs, data assets — that reinforce its position?"),
    (12, "Long-Range Profit Outlook",
         "Does management optimise for the long run? "
         "Are decisions (capex, R&D, pricing) consistent with a multi-year horizon?"),
    (13, "Dilution & Capital Allocation Discipline",
         "Will future growth require significant equity issuance that dilutes shareholders? "
         "Alternatively, does the company return capital efficiently via buybacks/dividends?"),
    (14, "Management Transparency",
         "Does management communicate openly — including when things go wrong? "
         "Is guidance credible? Are annual reports frank about risks and setbacks?"),
    (15, "Management Integrity",
         "Is management of unquestionable integrity? "
         "Any history of related-party transactions, insider selling, or governance failures?"),
]

HELMER_POWERS = [
    ("Scale Economies",
     "Do unit costs fall materially as the company grows? "
     "Is there a structural cost advantage vs. smaller competitors?"),
    ("Network Economies",
     "Does the product or platform become more valuable as more users join? "
     "Are there direct or indirect network effects?"),
    ("Counter-Positioning",
     "Does the company have a business model that incumbents cannot copy "
     "without destroying their own profitability?"),
    ("Switching Costs",
     "How expensive — financially, operationally, or psychologically — "
     "is it for a customer to leave for a competitor?"),
    ("Branding",
     "Does the brand command a durable price premium or loyalty advantage "
     "that competitors cannot replicate by spending money?"),
    ("Cornered Resource",
     "Does the company have preferential access to a scarce resource "
     "(talent, data, IP, geography, regulatory licence) others cannot obtain?"),
    ("Process Power",
     "Does the company have embedded operational processes that are so complex "
     "or culturally ingrained that competitors cannot replicate them in a reasonable timeframe?"),
]


# ── Cached prompt prefix — built once at module load ─────────────────────────
# Includes all 15 Fisher questions + 7 Helmer powers + output schema.
# ~1400+ tokens of fixed content: qualifies for Anthropic's 1024-token cache
# threshold (combined with the ~256-token system prompt).

def _build_fisher_fixed_block() -> str:
    fisher_q = "\n".join(
        f"  Point {n:02d} — {title}: {question}"
        for n, title, question in FISHER_QUESTIONS
    )
    helmer_q = "\n".join(
        f"  {name}: {question}"
        for name, question in HELMER_POWERS
    )
    return f"""\
Perform a deep-dive Fisher Alternatives analysis for the company data provided below.
Apply Philip Fisher's 15-point framework and Hamilton Helmer's 7 Powers rigorously.
Return a single JSON object with exactly the structure shown.

== FISHER 15-POINT QUESTIONS ==
{fisher_q}

== HELMER 7 POWERS QUESTIONS ==
{helmer_q}

Required JSON output:
{{
  "business_overview": "250-350 word overview of the business model, competitive position, and what makes this company structurally interesting (or not) to a long-term owner. Cover: what they sell, to whom, the value-chain position, pricing power evidence, and capital intensity. Be specific and factual.",

  "fisher_points": [
    {{
      "number": 1,
      "title": "Market Growth Potential",
      "score": "<integer 1-5>",
      "assessment": "<PASS|PARTIAL|FAIL>",
      "rationale": "2-3 sentences. Be specific — cite growth rates, market sizes, or product trends from the data where possible. Score 5 = outstanding, 3 = average, 1 = poor."
    }},
    "... (repeat for all 15 points, numbers 1-15)"
  ],

  "fisher_total_score": "<integer, sum of all 15 scores, max 75>",
  "fisher_grade": "<A|B|C|D|F — A=65+, B=55-64, C=45-54, D=35-44, F=<35>",
  "fisher_summary": "100-150 word synthesis of the Fisher assessment. What are the 2-3 strongest points? What are the critical weaknesses? What does the total score tell a long-term investor?",

  "powers": [
    {{
      "name": "Scale Economies",
      "strength": "<Strong|Moderate|Weak|None>",
      "rationale": "2-3 sentences. Explain the mechanism or explain why this power is absent. Be honest — most companies do not have all 7 powers."
    }},
    "... (repeat for all 7 powers in order: Scale Economies, Network Economies, Counter-Positioning, Switching Costs, Branding, Cornered Resource, Process Power)"
  ],

  "active_powers_count": "<integer 0-7, count of powers rated Strong or Moderate>",
  "moat_width": "<Wide|Narrow|None>",
  "moat_rationale": "100-150 word moat assessment. Which specific powers drive the moat? How durable are they over a 10-year horizon? What could erode them?",

  "key_risks": [
    "Specific risk 1 — concise, actionable statement (not generic 'market risk')",
    "Specific risk 2",
    "Specific risk 3",
    "Specific risk 4"
  ],

  "conclusion": "200-300 word investment conclusion from a Fisher/Helmer perspective. Synthesise the 15-point score, the powers analysis, and current valuation. Is this a business worth owning at any price? At the current price? What would need to be true for this to be a 10-year compounder? Avoid repeating the moat rationale verbatim.",

  "recommendation": "<BUY|HOLD|SELL>",
  "recommendation_rationale": "100-150 word rationale grounded in the Fisher/Helmer analysis above. Reference the specific score, the dominant power (or lack thereof), and the valuation context. State what condition or price changes the recommendation."
}}

Scoring calibration:
- Score 5: Exceptional — this criterion is a clear competitive strength
- Score 4: Above average — clear evidence of quality above peers
- Score 3: Average — meets the bar, not a standout
- Score 2: Below average — visible weaknesses
- Score 1: Poor — a genuine concern that investors should flag

All 15 fisher_points entries must be present (numbers 1-15) and all 7 powers entries must be present.

=== COMPANY DATA FOLLOWS ==="""


# Precomputed once at import time — never changes between runs
_FISHER_CACHEABLE = _build_fisher_fixed_block()


# Note: the old _format_financials_for_llm helper was removed when Fisher
# moved to the shared EODHD-only context builder in models/_eodhd_context.py
# (10-year history + peers + officers + insider + ratings + sentiment +
#  news + forward estimates + country macro). See _fisher_prompt_parts.


# ── LLM prompt builder ─────────────────────────────────────────────────────────

def _fisher_prompt_parts(
    company: CompanyData,
    bundle: Optional[dict] = None,
    peers: Optional[dict] = None,
    country_macro_block: str = "",
) -> tuple[str, str]:
    """
    Return (cacheable_prefix, dynamic_content).

    cacheable_prefix  — all 15 Fisher questions + 7 Powers + full output schema.
                        ~1400 tokens — qualifies for Anthropic prompt caching.
    dynamic_content   — company financials + EODHD context (peers, officers,
                        insider, ratings, sentiment, news, forward estimates,
                        country macro). All EODHD-sourced.

    Args:
        company             — main subject CompanyData (EODHD-only build)
        bundle              — raw EODHD endpoint bundle (news, sentiment, etc.)
        peers               — dict[ticker → CompanyData] of EODHD-only peers
        country_macro_block — pre-formatted country macro string from
                               data_sources.eodhd_macro.fetch_country_macro_block
    """
    from models._eodhd_context import build_eodhd_context, FISHER_ROWS

    if bundle is None:
        # Legacy fallback: synthesise a minimal bundle from what's on the
        # company object alone. Used only when something calls the prompt
        # builder without first fetching the EODHD bundle.
        bundle = {}

    dynamic = build_eodhd_context(
        company,
        bundle,
        FISHER_ROWS,
        peers=peers,
        country_macro_block=country_macro_block,
        n_years=10,
    )
    return _FISHER_CACHEABLE, dynamic


def _build_fisher_prompt(
    company: CompanyData,
    bundle: Optional[dict] = None,
    peers: Optional[dict] = None,
    country_macro_block: str = "",
) -> str:
    """Return the full prompt as a single string (used by adversarial mode)."""
    cacheable, dynamic = _fisher_prompt_parts(
        company, bundle=bundle, peers=peers,
        country_macro_block=country_macro_block,
    )
    return cacheable + "\n\n" + dynamic


# ── Helper formatters ─────────────────────────────────────────────────────────

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

def _m(v) -> str:
    if v is None: return "n/a"
    return f"{v:,.1f}"


# ── Model orchestrator ────────────────────────────────────────────────────────

class FisherModel:
    """
    Runs the Fisher Alternatives analysis pipeline (EODHD-only data):
      1. Fetch EODHD bundle + EODHD-only CompanyData
      2. Fetch country macro from EODHD /macro-indicator
      3. Build LLM prompt (Fisher 15 + Helmer 7 Powers + rich EODHD context)
      4. Parse JSON response → PDF
    Note: this CLI path does not fetch peers — supply them in the UI dispatch.
    """

    def __init__(self):
        self.llm = LLMClient()

    def run(
        self,
        ticker: str,
        force_refresh: bool = False,   # kept for API compat; EODHD has no cache here
        output_path: Optional[str] = None,
    ) -> str:
        """
        Full pipeline. Returns path to the generated PDF.
        """
        logger.info(f"[Fisher] Starting for {ticker}")

        # ── Step 1: Data (EODHD only) ─────────────────────────────────────────
        print(f"\n  [1/4] Fetching EODHD bundle for {ticker}...")
        from data_sources.eodhd_only_builder import fetch_company_data_eodhd_only
        company, bundle = fetch_company_data_eodhd_only(ticker)
        print(f"         {company.name or ticker} | "
              f"{company.year_range()} | "
              f"endpoints: {bundle.get('endpoints_used', 0)}/9")

        # Country macro
        from data_sources.eodhd_macro import fetch_country_macro_block
        country_macro = fetch_country_macro_block(company.country)

        # ── Step 2: LLM Analysis ──────────────────────────────────────────────
        print(f"  [2/4] Running Fisher/Helmer analysis ({self.llm.provider}/{self.llm.model})...")
        prompt = _build_fisher_prompt(
            company, bundle=bundle, peers=None,
            country_macro_block=country_macro,
        )
        adv_result = None

        if ADVERSARIAL_MODE:
            print(f"         [Adversarial Mode] Running Claude + GPT-4o dual analysis…")
            from agents.adversarial import AdversarialEngine
            engine     = AdversarialEngine()
            adv_result = engine.run(prompt, SYSTEM_PROMPT, max_tokens=6000,
                                    report_type="fisher")
            analysis   = adv_result.merged
        else:
            analysis = self.llm.generate_json(prompt, SYSTEM_PROMPT, max_tokens=6000)

        if not analysis:
            raise RuntimeError("LLM returned empty analysis — check API key and prompt.")

        rec   = analysis.get("recommendation", "n/a")
        score = analysis.get("fisher_total_score", "?")
        grade = analysis.get("fisher_grade", "?")
        moat  = analysis.get("moat_width", "?")
        print(f"         Fisher Score: {score}/75 (Grade {grade}) | Moat: {moat} | Rec: {rec}"
              + (" [MERGED]" if adv_result else ""))

        # ── Step 3: Validate & fill defaults ─────────────────────────────────
        analysis = _validate_analysis(analysis)

        # ── Step 4: PDF ───────────────────────────────────────────────────────
        print(f"  [3/4] Generating PDF...")
        from agents.pdf_fisher import FisherPDFGenerator

        if output_path is None:
            from utils.report_naming import report_file_stem
            safe  = report_file_stem(ticker, company.name)
            date  = datetime.now().strftime("%Y-%m-%d")
            fname = f"{safe}_fisher_{date}.pdf"
            output_path = os.path.join(OUTPUTS_DIR, fname)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        gen = FisherPDFGenerator()
        gen.render(company, analysis, output_path, adv_result=adv_result)
        logger.info(f"[Fisher] Done. PDF at {output_path}")

        print(f"  [4/4] Done.")
        print(f"\n  Report saved: {output_path}")
        return output_path


# ── Analysis validator / default filler ──────────────────────────────────────

def _validate_analysis(a: dict) -> dict:
    """
    Ensure the analysis dict has all expected keys with sensible defaults.
    Guards against partial LLM responses.
    """
    # Fisher points: ensure all 15 present
    existing = {p["number"]: p for p in a.get("fisher_points", []) if isinstance(p, dict)}
    full_points = []
    for n, title, _ in FISHER_QUESTIONS:
        if n in existing:
            p = existing[n]
        else:
            p = {"number": n, "title": title, "score": 3,
                 "assessment": "PARTIAL", "rationale": "Insufficient data to assess."}
        # Coerce score to a valid int 1-5 — the LLM can omit it, return None
        # (e.g. thin/no fundamentals for the ticker), or return a string/float.
        # A bare `p.get("score", 3)` only defaults when the key is *missing*,
        # not when it's present but None, so downstream comparisons/sum()
        # would crash on a None score.
        try:
            s = int(round(float(p.get("score"))))
        except (TypeError, ValueError):
            s = 3
        p["score"] = max(1, min(5, s))
        # Normalise assessment from score if missing
        if "assessment" not in p or p["assessment"] not in ("PASS", "PARTIAL", "FAIL"):
            p["assessment"] = "PASS" if p["score"] >= 4 else ("FAIL" if p["score"] <= 2 else "PARTIAL")
        full_points.append(p)
    a["fisher_points"] = full_points

    # Recalculate total score from points (in case LLM got it wrong)
    a["fisher_total_score"] = sum(p["score"] for p in full_points)
    total = a["fisher_total_score"]
    if "fisher_grade" not in a or a["fisher_grade"] not in ("A","B","C","D","F"):
        a["fisher_grade"] = ("A" if total >= 65 else "B" if total >= 55 else
                              "C" if total >= 45 else "D" if total >= 35 else "F")

    # Powers: ensure all 7 present
    power_names = [name for name, _ in HELMER_POWERS]
    existing_p  = {p["name"]: p for p in a.get("powers", []) if isinstance(p, dict)}
    full_powers = []
    for name in power_names:
        if name in existing_p:
            full_powers.append(existing_p[name])
        else:
            full_powers.append({"name": name, "strength": "Weak",
                                 "rationale": "Insufficient data to assess."})
    a["powers"] = full_powers

    # Count active powers
    a["active_powers_count"] = sum(
        1 for p in full_powers
        if p.get("strength") in ("Strong", "Moderate")
    )

    # Defaults for other fields
    a.setdefault("business_overview",    "Business overview not available.")
    a.setdefault("fisher_summary",       "Fisher summary not available.")
    a.setdefault("moat_width",           "Narrow")
    a.setdefault("moat_rationale",       "Moat analysis not available.")
    a.setdefault("key_risks",            ["Key risks not analysed."])
    a.setdefault("conclusion",           "Conclusion not available.")
    a.setdefault("recommendation",       "HOLD")
    a.setdefault("recommendation_rationale", "Rationale not available.")

    return a


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Your Humble EquityBot — Fisher Alternatives Report"
    )
    parser.add_argument("ticker",  help="Yahoo Finance ticker  (e.g. WKL.AS, AAPL)")
    parser.add_argument("--out",   help="Output PDF path (default: outputs/<ticker>_fisher_<date>.pdf)")
    parser.add_argument("--force", action="store_true", help="Force refresh cached data")
    args = parser.parse_args()

    print("\nYour Humble EquityBot — Fisher Alternatives")
    print("=" * 50)
    print(f"Ticker: {args.ticker}")

    model = FisherModel()
    path  = model.run(
        ticker=args.ticker,
        force_refresh=args.force,
        output_path=args.out,
    )
    print(f"\nDone. Open: {path}")
