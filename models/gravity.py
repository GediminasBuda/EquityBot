"""
gravity.py — Gravity Taxers model.

"Gravity Taxers" are businesses that sit at essential choke points in a value chain
and extract a toll from every transaction or workflow that passes through them.
Think Visa processing every card swipe, Wolters Kluwer embedded in every tax filing,
Moody's rating every bond, or a port operator handling every container.

The model scores 10 structural dimensions of choke-point strength, assesses the
revenue model (recurring vs. transactional), and evaluates how the business compounds.

Produces a 3-page PDF:
  Page 1: Header + Gravity Profile + Revenue Model Summary + Dimensions 1-5
  Page 2: Dimensions 6-10 + Gravity Score Summary + Canonical Comparison + Flywheel
  Page 3: Key Risks + Investment Conclusion + Recommendation

Usage:
    from models.gravity import GravityModel
    path = GravityModel().run("WKL.AS")

CLI:
    python models/gravity.py WKL.AS
    python models/gravity.py V          # Visa
    python models/gravity.py MCO        # Moody's
"""

from __future__ import annotations
import argparse
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

_SYSTEM_PROMPT_FALLBACK = """You are "Your Humble EquityBot" — a specialist in identifying and
evaluating businesses that function as economic choke points, or "Gravity Taxers."

Your analytical DNA:
- You think like a toll-road owner: what is the volume of economic activity flowing
  through this business, and how durably can it extract a fee from that flow?
- You distinguish between businesses that are merely important and those that are
  genuinely irreplaceable within their value chain.
- You are sceptical of "sticky" claims — stickiness must be evidenced by pricing
  data, churn rates, or switching cost analysis. Anecdotes don't count.
- You probe revenue quality ruthlessly: recurring subscription revenue trading at
  a premium to one-time transactional revenue is a core tenet of your framework.
- You compare every candidate against canonical Gravity Taxers: Visa, Mastercard,
  Moody's, S&P Global, MSCI, FactSet, Verisk, CoStar, Wolters Kluwer, RELX.
  Most companies do not clear this bar.
- Capital efficiency is non-negotiable: a true Gravity Taxer generates high ROIC
  because the "toll booth" requires little reinvestment to maintain its position.
- You never over-score: most businesses are not choke points. A score of 7-8/10
  on Choke Point Essentialness requires genuine, near-irreplaceable positioning.
"""


def _load_system_prompt() -> str:
    try:
        from framework_manager import FrameworkManager
        return FrameworkManager().get_system_prompt("gravity", _SYSTEM_PROMPT_FALLBACK)
    except Exception:
        return _SYSTEM_PROMPT_FALLBACK


SYSTEM_PROMPT = _load_system_prompt()


# ── The 10 Gravity Dimensions ─────────────────────────────────────────────────

GRAVITY_DIMENSIONS = [
    (1,  "Value Chain Essentialness",
         "How necessary is this company in its industry value chain? "
         "Can the relevant business workflow proceed without it, or does it occupy "
         "a near-mandatory junction that participants cannot bypass?"),

    (2,  "Pricing Power & Toll Extraction",
         "Can the company raise prices above inflation without losing meaningful volume? "
         "Is there evidence of consistent price increases passing through to revenue "
         "without customer churn or substitution?"),

    (3,  "Revenue Recurrence & Predictability",
         "What fraction of revenue is subscription-based or recurring? "
         "How visible is next year's revenue — do customers pay upfront or on contract? "
         "Is revenue predictability improving over time?"),

    (4,  "Customer Switching Cost",
         "How painful — financially, operationally, and psychologically — "
         "is it for a customer to replace this product with a competitor's? "
         "Are there integration, retraining, data migration, or contractual lock-in barriers?"),

    (5,  "Capital Efficiency & ROIC",
         "How little capital does the business need to sustain and grow its revenue base? "
         "Does the company generate high and improving ROIC? "
         "Is the incremental return on new investment above the cost of capital?"),

    (6,  "Data & Intellectual Property Flywheel",
         "Does the business accumulate proprietary data, benchmarks, indices, or IP "
         "over time that become more valuable as they grow and that competitors "
         "cannot easily replicate? Is there a compounding data asset?"),

    (7,  "Regulatory & Licensing Moat",
         "Is the business model insulated by regulation, licensing requirements, "
         "government mandates, or official designations (e.g., NRSRO status for credit "
         "rating agencies, statutory audit requirements) that limit competition?"),

    (8,  "Network Density & Ecosystem Lock-in",
         "Does the platform become more valuable as more participants join — buyers "
         "and sellers, publishers and readers, filers and reviewers? "
         "Does the company sit at the centre of a multi-sided network that compounds?"),

    (9,  "Operating Leverage & Margin Expansion",
         "As revenue grows, does the cost structure allow margins to expand materially? "
         "Is there evidence of operating leverage — i.e., incremental revenue dropping "
         "through to profit at a higher rate than the reported margin?"),

    (10, "Competitive Insulation & Entry Barriers",
         "How hard is it for a well-funded competitor to replicate this business? "
         "Consider: time to build (years, not months), capital required, customer "
         "relationships, data assets, and brand trust needed to displace the incumbent."),
]


# ── Cached prompt prefix — built once at module load ─────────────────────────
# Includes all 10 Gravity dimensions + full output schema.
# ~1200+ tokens of fixed content: qualifies for Anthropic's 1024-token cache
# threshold (combined with the ~326-token system prompt).

def _build_gravity_fixed_block() -> str:
    dims = "\n".join(
        f"  Dimension {n:02d} — {title}:\n  {question}"
        for n, title, question in GRAVITY_DIMENSIONS
    )
    return f"""\
Perform a Gravity Taxers analysis for the company data provided below.
A "Gravity Taxer" is a business that occupies an essential choke point in a value chain
and extracts a durable, recurring toll from economic activity flowing through it.
Score the company on 10 structural dimensions of choke-point strength.
Return a single JSON object with exactly the structure shown.

== GRAVITY DIMENSIONS TO SCORE ==
{dims}

Required JSON output:
{{
  "gravity_profile": "200-300 word description of this company as a potential Gravity Taxer. Cover: (1) what economic flow it sits at the centre of, (2) why customers cannot easily bypass it, (3) evidence of toll-extraction in the financials (margin trends, pricing history, recurring revenue), (4) how it compares in character to canonical choke-point businesses like Visa, Moody's, or MSCI. Be specific and critical — not all businesses qualify.",

  "revenue_model": {{
    "recurring_pct_estimate": "<integer 0-100, estimated % of revenue that is recurring or subscription>",
    "revenue_visibility": "<High|Medium|Low>",
    "pricing_power": "<Strong|Moderate|Weak>",
    "capex_intensity": "<Asset-Light|Moderate|Capital-Heavy>",
    "pricing_evidence": "2-3 sentences describing specific evidence of pricing power from the financial data: price increases, ARPU trends, margin expansion, or lack thereof."
  }},

  "gravity_dimensions": [
    {{
      "number": 1,
      "title": "Value Chain Essentialness",
      "score": "<integer 1-5>",
      "rationale": "2-3 sentences. Cite specific evidence. Score 5 = near-irreplaceable, 3 = important but substitutable, 1 = easily bypassed."
    }},
    "... (repeat for all 10 dimensions, numbers 1-10)"
  ],

  "total_gravity_score": "<integer, sum of all 10 scores, max 50>",
  "gravity_grade": "<A|B|C|D|F — A=45+, B=38+, C=30+, D=22+, F<22>",
  "gravity_summary": "100-150 word synthesis. Is this a genuine Gravity Taxer? Where does it sit on the spectrum from pure toll-booth to commoditised service provider? What would need to change for it to score higher?",

  "canonical_comparison": "100-150 word comparison to the canonical set of Gravity Taxers (Visa, Mastercard, Moody's, S&P Global, MSCI, FactSet, Verisk, CoStar, Wolters Kluwer, RELX, Bloomberg, SS&C). How does this company's choke-point strength compare? Is it in the same league, a tier below, or categorically different?",

  "revenue_flywheel": "100-150 word description of any self-reinforcing dynamics in the revenue model. Does accumulated data, installed base, or network density create a flywheel that makes the toll booth progressively harder to compete with? Or is it a static advantage without compounding characteristics?",

  "key_risks": [
    "Specific risk 1 — what structural change could undermine the choke-point position?",
    "Specific risk 2 — pricing or competitive risk",
    "Specific risk 3 — technology disruption or disintermediation risk",
    "Specific risk 4 — regulatory or macro risk"
  ],

  "conclusion": "200-300 word investment conclusion from the Gravity Taxer perspective. Synthesise the gravity score, revenue model quality, and current valuation. Is this a business worth paying a premium for? What is the margin of safety at current prices? What specific condition or metric would change the investment case?",

  "recommendation": "<BUY|HOLD|SELL>",
  "recommendation_rationale": "100-150 word rationale grounded in the gravity analysis. Reference the gravity score, the dominant choke-point characteristic, and whether the current valuation reflects the quality of the toll booth. State the key upside and downside triggers."
}}

Scoring calibration:
- Score 5: Exceptional — near-perfect expression of this dimension (e.g. Visa on Switching Costs)
- Score 4: Above average — clear advantage, durable
- Score 3: Average — meets the bar, not a standout
- Score 2: Below average — visible weakness
- Score 1: Poor — this dimension actively hurts the investment case

All 10 gravity_dimensions entries must be present (numbers 1-10).

=== COMPANY DATA FOLLOWS ==="""


# Precomputed once at import time — never changes between runs
_GRAVITY_CACHEABLE = _build_gravity_fixed_block()


# Note: the old _format_financials_for_llm helper was removed when Gravity
# moved to the shared EODHD-only context builder in models/_eodhd_context.py
# (10-year history + peers + officers + insider + ratings + sentiment +
#  news + forward estimates + country macro). See _gravity_prompt_parts.


# ── LLM prompt builder ────────────────────────────────────────────────────────

def _gravity_prompt_parts(
    company: CompanyData,
    bundle: Optional[dict] = None,
    peers: Optional[dict] = None,
    country_macro_block: str = "",
) -> tuple[str, str]:
    """
    Return (cacheable_prefix, dynamic_content).

    cacheable_prefix  — all 10 Gravity dimensions + full output schema.
                        ~1200 tokens — qualifies for Anthropic prompt caching.
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
    from models._eodhd_context import build_eodhd_context, GRAVITY_ROWS

    if bundle is None:
        bundle = {}

    dynamic = build_eodhd_context(
        company,
        bundle,
        GRAVITY_ROWS,
        peers=peers,
        country_macro_block=country_macro_block,
        n_years=10,
    )
    return _GRAVITY_CACHEABLE, dynamic


def _build_gravity_prompt(
    company: CompanyData,
    bundle: Optional[dict] = None,
    peers: Optional[dict] = None,
    country_macro_block: str = "",
) -> str:
    """Return the full prompt as a single string (used by adversarial mode)."""
    cacheable, dynamic = _gravity_prompt_parts(
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


# ── Analysis validator ────────────────────────────────────────────────────────

def _validate_analysis(a: dict) -> dict:
    """Ensure all keys present and scores sane."""
    # Gravity dimensions: ensure all 10 present
    existing = {d["number"]: d for d in a.get("gravity_dimensions", []) if isinstance(d, dict)}
    full_dims = []
    for n, title, _ in GRAVITY_DIMENSIONS:
        if n in existing:
            d = existing[n]
        else:
            d = {"number": n, "title": title, "score": 3,
                 "rationale": "Insufficient data to assess."}
        full_dims.append(d)
    a["gravity_dimensions"] = full_dims

    # Coerce all scores to int (LLM may return strings or floats)
    for d in full_dims:
        d["score"] = int(d.get("score", 3) or 3)

    # Recalculate total
    a["total_gravity_score"] = sum(d["score"] for d in full_dims)
    t = a["total_gravity_score"]
    if "gravity_grade" not in a or a["gravity_grade"] not in ("A","B","C","D","F"):
        a["gravity_grade"] = ("A" if t >= 45 else "B" if t >= 38 else
                               "C" if t >= 30 else "D" if t >= 22 else "F")

    # Revenue model defaults
    rm = a.setdefault("revenue_model", {})
    rm.setdefault("recurring_pct_estimate", 0)
    rm.setdefault("revenue_visibility", "Medium")
    rm.setdefault("pricing_power", "Moderate")
    rm.setdefault("capex_intensity", "Moderate")
    rm.setdefault("pricing_evidence", "No pricing evidence available.")

    a.setdefault("gravity_profile",    "Gravity profile not available.")
    a.setdefault("gravity_summary",    "Gravity summary not available.")
    a.setdefault("canonical_comparison","Canonical comparison not available.")
    a.setdefault("revenue_flywheel",   "Revenue flywheel analysis not available.")
    a.setdefault("key_risks",          ["Key risks not analysed."])
    a.setdefault("conclusion",         "Conclusion not available.")
    a.setdefault("recommendation",     "HOLD")
    a.setdefault("recommendation_rationale", "Rationale not available.")

    return a


# ── Model orchestrator ────────────────────────────────────────────────────────

class GravityModel:
    """
    Runs the Gravity Taxers analysis pipeline (EODHD-only data):
      1. Fetch EODHD bundle + EODHD-only CompanyData
      2. Fetch country macro from EODHD /macro-indicator
      3. Build LLM prompt (10 Gravity Dimensions + revenue model + rich context)
      4. Parse and validate JSON → PDF
    Note: this CLI path does not fetch peers — supply them in the UI dispatch.
    """

    def __init__(self):
        self.llm = LLMClient()

    def run(
        self,
        ticker: str,
        force_refresh: bool = False,
        output_path: Optional[str] = None,
    ) -> str:
        logger.info(f"[Gravity] Starting for {ticker}")

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
        print(f"  [2/4] Running Gravity Taxers analysis ({self.llm.provider}/{self.llm.model})...")
        prompt = _build_gravity_prompt(
            company, bundle=bundle, peers=None,
            country_macro_block=country_macro,
        )
        adv_result = None

        if ADVERSARIAL_MODE:
            print(f"         [Adversarial Mode] Running Claude + GPT-4o dual analysis…")
            from agents.adversarial import AdversarialEngine
            engine     = AdversarialEngine()
            adv_result = engine.run(prompt, SYSTEM_PROMPT, max_tokens=6000,
                                    report_type="gravity")
            analysis   = adv_result.merged
        else:
            analysis = self.llm.generate_json(prompt, SYSTEM_PROMPT, max_tokens=6000)

        if not analysis:
            raise RuntimeError("LLM returned empty analysis — check API key and prompt.")

        analysis = _validate_analysis(analysis)

        score   = analysis.get("total_gravity_score", "?")
        grade   = analysis.get("gravity_grade", "?")
        rec     = analysis.get("recommendation", "n/a")
        pp      = analysis.get("revenue_model", {}).get("pricing_power", "?")
        rec_pct = analysis.get("revenue_model", {}).get("recurring_pct_estimate", "?")
        print(f"         Gravity Score: {score}/50 (Grade {grade}) | "
              f"Recurring: ~{rec_pct}% | Pricing Power: {pp} | Rec: {rec}"
              + (" [MERGED]" if adv_result else ""))

        # ── Step 3: PDF ───────────────────────────────────────────────────────
        print(f"  [3/4] Generating PDF...")
        from agents.pdf_gravity import GravityPDFGenerator

        if output_path is None:
            from utils.report_naming import report_file_stem
            safe  = report_file_stem(ticker, company.name)
            date  = datetime.now().strftime("%Y-%m-%d")
            fname = f"{safe}_gravity_{date}.pdf"
            output_path = os.path.join(OUTPUTS_DIR, fname)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        GravityPDFGenerator().render(company, analysis, output_path,
                                     adv_result=adv_result)

        print(f"  [4/4] Done.")
        print(f"\n  Report saved: {output_path}")
        logger.info(f"[Gravity] Done. PDF at {output_path}")
        return output_path


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Your Humble EquityBot — Gravity Taxers Report"
    )
    parser.add_argument("ticker", help="Yahoo Finance ticker  (e.g. WKL.AS, V, MCO)")
    parser.add_argument("--out",  help="Output PDF path")
    parser.add_argument("--force", action="store_true", help="Force refresh cached data")
    args = parser.parse_args()

    print("\nYour Humble EquityBot — Gravity Taxers")
    print("=" * 50)
    print(f"Ticker: {args.ticker}")

    model = GravityModel()
    path  = model.run(
        ticker=args.ticker,
        force_refresh=args.force,
        output_path=args.out,
    )
    print(f"\nDone. Open: {path}")
