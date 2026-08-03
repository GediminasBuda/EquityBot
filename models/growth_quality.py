"""
growth_quality.py — Growth Quality Score (GQS) model.

Evaluates whether a company exhibits the structural traits of a durable
long-term compounder — not whether it is cheap today. The full model is
being built incrementally, one capability at a time, in two phases:

  Phase 1 — Build the Evidence: for each capability, construct historical
  data tables, identify long-term trends, and discuss both positive and
  negative evidence. No scoring in this phase.

  Phase 2 — Scoring (not yet implemented): will assign a score per
  capability and a composite Growth Quality Score once all 8 capabilities
  have evidence built out.

There are 8 capabilities total in the final model. GQ_CAPABILITY_META below
is the single source of truth for which capabilities are currently live —
adding a new capability means adding an entry to GQ_CAPABILITY_META and a
matching prompt block to GQ_CAPABILITY_PROMPTS; nothing else needs to
change (the cacheable prompt, JSON schema instructions, validation, and PDF
rendering all iterate these dicts generically).

Single-company analysis (no peers) — driven from
pages/report_generator.py's growth_quality dispatch block, using the same
EODHD-then-yfinance waterfall as Earnings Quality Score / Investment Memo
V2 (via data_sources/eodhd_only_builder.py).
"""

from __future__ import annotations
import logging
from typing import Optional

from data_sources.base import CompanyData

logger = logging.getLogger(__name__)


# ── Capability registry ─────────────────────────────────────────────────────
# Single source of truth for which of the 8 capabilities are currently live.
# "sub_questions" are the specific discussion questions the LLM must answer,
# in order, for this capability — used both to build the prompt instructions
# and as validation defaults if the model omits one.
GQ_CAPABILITY_META = {
    "demand_strength": {
        "number": 1,
        "title": "Demand Strength",
        "question": "Does the world increasingly want this company's products?",
        "sub_questions": [
            "Is demand accelerating?",
            "Is growth becoming more diversified?",
            "Is growth broad-based or concentrated?",
            "Is the company gaining market relevance?",
            "Is demand cyclical or structural?",
        ],
    },
}

GQ_CAPABILITY_ORDER = list(GQ_CAPABILITY_META.keys())
GQ_CAPABILITIES_TOTAL = 8  # total planned capabilities in the final model


SYSTEM_PROMPT = """You are an institutional equity analyst specializing in evaluating \
high-growth public companies, particularly businesses that may still report GAAP \
losses but are investing to build durable competitive advantages.

Your objective is NOT to determine whether the company is cheap today. Your \
objective is to determine whether the company possesses the characteristics of a \
future long-term compounder.

Use up to 10 years of historical data (or since IPO if shorter) wherever it is \
provided. Verified financial figures are supplied to you in the data block below \
and must be treated as ground truth — never recompute or contradict them. You may \
supplement your qualitative judgment with your own knowledge of the company's \
10-K/annual-report/investor-presentation/earnings-transcript disclosures, but you \
must NEVER invent numbers. Whenever a specific data point is not available to you, \
write exactly "Data unavailable" for that value — do not estimate, guess, or \
approximate.

You are currently in Phase 1 of the analysis: Build the Evidence. For each \
capability you are given, you construct historical data tables, identify \
long-term trends, explain what the numbers imply, and discuss both positive and \
negative evidence. You do NOT assign a score or grade in this phase — scoring \
happens in a later phase of this model.
"""


# ── Per-capability prompt instructions ──────────────────────────────────────
GQ_CAPABILITY_PROMPTS = {
    "demand_strength": """\
CAPABILITY 1 — DEMAND STRENGTH
Question: Does the world increasingly want this company's products?

A verified Revenue / YoY Growth / 3-Year CAGR / 5-Year CAGR table (computed from \
the company's own reported financials) is already provided to you in the data \
block below — treat those figures as ground truth, do not recompute or contradict \
them, and do not repeat them in your JSON response.

For the "demand_strength" key, return the following, covering approximately the \
last ten fiscal years (or since IPO if shorter) wherever the company plausibly \
discloses the data, oldest fiscal year first:

Tables are laid out with metrics as ROWS and fiscal years as COLUMNS (years \
across the top, metric names down the left side) — use exactly the same fiscal \
years, in the same oldest-to-newest order, as the verified revenue table in the \
data block below, so every table in this report lines up on the same year \
columns.

Only include a row for a metric the company actually discloses in at least one \
of those years. If a metric is never disclosed at all across the whole history \
(e.g. this company has never reported a customer count), OMIT that row entirely \
— do not include a row filled entirely with "Data unavailable". Within a row \
you do keep, individual years the company didn't disclose that specific metric \
for should still read exactly "Data unavailable". If literally none of a \
table's metrics are disclosed by this company, return an empty "rows" list for \
that table — do not invent placeholder rows.

1. "customers_table": an object {"years": [...], "rows": [{"metric": "...", \
"values": [...]}, ...]} where "years" is the same year list described above and \
each row's "values" list has exactly one entry per year. Candidate metrics \
(include only those disclosed): "Customer Count", "Net Customer Additions", \
"Enterprise Customers", "Active Users", "Subscribers". Never invent a customer \
count.

2. "momentum_table": same shape as customers_table. Candidate metrics (include \
only those disclosed): "Bookings", "ARR", "Backlog". If the company discloses a \
geographic revenue breakdown (e.g. United States vs. Rest of World, or a \
regional split), include it as multiple rows per the breakdown-metric rule \
below — do not try to fit it into one row called "Geographic Revenue Mix".

3. "discussion": an array of exactly 5 objects, each {"question": "...", \
"answer": "..."}, addressing IN THIS EXACT ORDER:
   a. Is demand accelerating?
   b. Is growth becoming more diversified?
   c. Is growth broad-based or concentrated?
   d. Is the company gaining market relevance?
   e. Is demand cyclical or structural?
Each answer should be 60-120 words, cite specific numbers from the data \
provided, and discuss both positive and negative evidence — do not present only \
a bullish case.

4. "why_it_matters": a single 100-180 word paragraph explaining how the evidence \
above supports — or contradicts — the existence of durable, structural demand \
for this company's products. This is evidence-gathering only: do NOT assign a \
score, grade, or rating of any kind.
""",
}


def _build_gq_cacheable() -> str:
    schema_example = ",\n".join(
        f'    "{cap_id}": {{\n'
        f'      "customers_table": {{"years": [...], "rows": '
        f'[{{"metric": "...", "values": [...]}}]}},\n'
        f'      "momentum_table": {{"years": [...], "rows": '
        f'[{{"metric": "...", "values": [...]}}]}},\n'
        f'      "discussion": [{{"question": "...", "answer": "..."}}, ...],\n'
        f'      "why_it_matters": "..."\n'
        f'    }}'
        for cap_id in GQ_CAPABILITY_ORDER
    )
    parts = [
        "Objective: build the historical-evidence base for the capabilities listed "
        "below, for the single subject company supplied in the data block. This is "
        "Phase 1 (Build the Evidence) of the Growth Quality Score model — do not "
        "score or grade anything.",
    ]
    for cap_id in GQ_CAPABILITY_ORDER:
        parts.append(GQ_CAPABILITY_PROMPTS[cap_id])
    parts.append(
        "Required JSON output shape (include exactly these capability keys, no "
        "others):\n{\n  \"capabilities\": {\n" + schema_example + "\n  }\n}\n\n"
        "Rules:\n"
        "- For the verified data blocks supplied below (Verified Revenue "
        "History, and any 10-K/20-F Annual Report Excerpts) — figures "
        "explicitly present there are ground truth: always prefer them, and "
        "never contradict them.\n"
        "- For customer/user-count and commercial-momentum metrics "
        "(customers_table, momentum_table only) in a fiscal year NOT covered "
        "by those verified blocks — e.g. a single annual report excerpt "
        "typically only states the most recent 1-2 years verbatim, per "
        "standard SEC MD&A practice — you may still supply a value from your "
        "own general knowledge, but you MUST prefix it with a tilde (\"~\") "
        "to mark it as recalled/unverified rather than sourced from the data "
        "provided (e.g. \"~480 million\"). Never prefix a figure with \"~\" "
        "if it IS present in the verified data blocks.\n"
        "- If you have no reliable knowledge at all for a cell (verified or "
        "otherwise), write exactly \"Data unavailable\" — do not guess just "
        "to fill a cell.\n"
        "- Never include a table row for a metric that is never disclosed at "
        "all — omit the row instead of filling it entirely with \"Data "
        "unavailable\". This keeps the report focused on data that actually "
        "exists and avoids wasting space/output on empty rows.\n"
        "- Breakdown-style metrics (a disclosed total split across 2+ "
        "categories — geographic mix, segment mix, product-line mix, revenue "
        "type mix, etc.) do not fit in a single table row. Represent each "
        "component as its own row with a specific, self-explanatory name (e.g. "
        "\"United States Revenue\", \"Rest of World Revenue\", \"US % of Total "
        "Revenue\" — not a single ambiguous row named after the breakdown as a "
        "whole). This applies to every table in every capability, not just the "
        "examples named above.\n"
        "- This is Phase 1 (Build the Evidence) only. Do NOT include any score, "
        "grade, or rating field anywhere in your response.\n"
        "- Write in English, professional institutional-equity-research tone. "
        "Prose only in discussion/why_it_matters text — no markdown formatting.\n"
        "- Do not wrap the JSON in markdown code fences.\n\n"
        "=== SUBJECT COMPANY DATA FOLLOWS ==="
    )
    return "\n\n".join(parts)


_GQ_CACHEABLE = _build_gq_cacheable()


# ── Verified revenue table — computed deterministically from real data ─────
# Never trust an LLM to do this arithmetic; it is passed to the model as
# ground-truth context and rendered directly in the PDF.

def compute_revenue_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    """
    Return a chronological (oldest-first) list of
    {"year", "revenue", "yoy", "cagr3", "cagr5"} dicts computed from
    company.annual_financials. Growth rates are None when the required
    historical revenue figure isn't available.
    """
    years = get_history_years(company, max_years=max_years)
    rows = []
    for i, y in enumerate(years):
        af = company.annual_financials.get(y)
        rev = af.revenue if af else None

        yoy = None
        if i > 0 and rev is not None:
            prev = company.annual_financials.get(years[i - 1])
            prev_rev = prev.revenue if prev else None
            if prev_rev:
                yoy = (rev / prev_rev) - 1

        cagr3 = None
        if i >= 3 and rev is not None and rev > 0:
            base = company.annual_financials.get(years[i - 3])
            base_rev = base.revenue if base else None
            if base_rev and base_rev > 0:
                cagr3 = (rev / base_rev) ** (1 / 3) - 1

        cagr5 = None
        if i >= 5 and rev is not None and rev > 0:
            base = company.annual_financials.get(years[i - 5])
            base_rev = base.revenue if base else None
            if base_rev and base_rev > 0:
                cagr5 = (rev / base_rev) ** (1 / 5) - 1

        rows.append({"year": y, "revenue": rev, "yoy": yoy, "cagr3": cagr3, "cagr5": cagr5})
    return rows


def get_history_years(company: CompanyData, max_years: int = 10) -> list[int]:
    """Chronological (oldest-first) fiscal years used across every GQS table,
    so the Revenue table (Python-computed) and the LLM's own tables (Customers,
    Commercial Momentum) all line up on identical year columns."""
    return list(reversed(company.sorted_years()[:max_years]))


def _b(v) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:.1f}M"


def format_growth_financials(company: CompanyData) -> str:
    """Build the subject-company data block: identity + verified revenue history."""
    cur = company.currency or "USD"
    lines = []

    lines.append(f"COMPANY: {company.name or company.ticker} ({company.ticker})")
    lines.append(f"SECTOR:  {company.sector or 'n/a'} — {company.industry or 'n/a'}")
    lines.append(f"COUNTRY: {company.country or 'n/a'} | CURRENCY: {cur} | "
                 f"EXCHANGE: {company.exchange or 'n/a'}")
    lines.append(f"IPO DATE: {company.ipo_date or 'Data unavailable'} | "
                 f"EMPLOYEES: {company.employees or 'Data unavailable'}")
    if company.description:
        desc = company.description[:600]
        lines.append(f"BUSINESS: {desc}{'...' if len(company.description) > 600 else ''}")

    rows = compute_revenue_table(company)
    if rows:
        cur_label = f"{cur} millions"
        lines.append(f"\nVERIFIED REVENUE HISTORY ({cur_label}, oldest to newest, "
                      f"computed from reported financials — treat as ground truth):")
        header = f"  {'Fiscal Year':<12} {'Revenue':>12} {'YoY Growth':>12} {'3Y CAGR':>10} {'5Y CAGR':>10}"
        lines.append(header)
        lines.append("  " + "-" * len(header))
        for r in rows:
            yoy_s = f"{r['yoy']*100:.1f}%" if r["yoy"] is not None else "n/a"
            c3_s = f"{r['cagr3']*100:.1f}%" if r["cagr3"] is not None else "n/a"
            c5_s = f"{r['cagr5']*100:.1f}%" if r["cagr5"] is not None else "n/a"
            lines.append(f"  {r['year']:<12} {_b(r['revenue']):>12} {yoy_s:>12} {c3_s:>10} {c5_s:>10}")
        years = [r["year"] for r in rows]
        lines.append(f"\nUse exactly these fiscal years, in this order, as the column headers "
                     f"for the customers_table and momentum_table: {years}")
    else:
        lines.append("\nVERIFIED REVENUE HISTORY: no historical revenue data available for this ticker.")

    return "\n".join(lines)


def format_annual_report_excerpts(excerpt_text: str) -> str:
    """Wrap a pre-extracted 10-K/annual-report excerpt block for inclusion in
    the dynamic prompt. Returns "" (no-op) if no excerpt text is available."""
    if not excerpt_text or not excerpt_text.strip():
        return ""
    return (
        "\n\n10-K/20-F ANNUAL REPORT EXCERPTS (verbatim, ground-truth for the "
        "years and figures it explicitly contains — prioritize over any other "
        "knowledge for those years; covers operating metrics not already in "
        "the verified revenue history above, such as customer/user counts, "
        "geographic revenue mix, bookings, and ARR. For years this excerpt "
        "does not cover, follow the tilde-prefix rule in the instructions "
        "above rather than treating the gap as if nothing is known):\n"
        + excerpt_text.strip()
    )


def _growth_quality_prompt_parts(
    subject: CompanyData, annual_report_excerpts: str = ""
) -> tuple[str, str]:
    """Return (cacheable_prefix, dynamic_content) for a single-company GQS run."""
    dynamic = (
        f"SUBJECT COMPANY TICKER: {subject.ticker}\n\n"
        + format_growth_financials(subject)
        + format_annual_report_excerpts(annual_report_excerpts)
        + "\n\nAnalyze the capabilities listed above for this company and return the JSON."
    )
    return _GQ_CACHEABLE, dynamic


# ── Validation helpers ──────────────────────────────────────────────────────

def _is_missing(v) -> bool:
    if v is None:
        return True
    s = str(v).strip().lower()
    return s in ("", "data unavailable", "n/a", "na", "none", "-", "null")


def _coerce_metric_table(v, years: list[int]) -> dict:
    """
    Coerce an LLM-returned {"years": [...], "rows": [{"metric","values"}]}
    table. Rows with zero real data across every year are dropped here as a
    safety net (the prompt already instructs the model to omit them) — if a
    table ends up with no rows at all, the PDF renderer skips it entirely
    rather than showing an empty grid.
    """
    year_labels = [str(y) for y in years]
    if not isinstance(v, dict):
        return {"years": year_labels, "rows": []}
    rows_raw = v.get("rows")
    rows = []
    if isinstance(rows_raw, list):
        for r in rows_raw:
            if not isinstance(r, dict):
                continue
            metric = str(r.get("metric") or "").strip()
            if not metric:
                continue
            vals_raw = r.get("values")
            vals = list(vals_raw) if isinstance(vals_raw, list) else []
            if len(vals) < len(year_labels):
                vals = vals + [None] * (len(year_labels) - len(vals))
            elif len(vals) > len(year_labels):
                vals = vals[:len(year_labels)]
            vals = [("Data unavailable" if _is_missing(x) else str(x)) for x in vals]
            if all(_is_missing(x) for x in vals):
                continue
            rows.append({"metric": metric, "values": vals})
    return {"years": year_labels, "rows": rows}


def _coerce_discussion(v, sub_questions: list[str]) -> list[dict]:
    raw_by_q = {}
    if isinstance(v, list):
        for item in v:
            if isinstance(item, dict) and item.get("question"):
                raw_by_q[str(item["question"]).strip()] = str(item.get("answer") or "").strip()
    out = []
    for q in sub_questions:
        out.append({"question": q, "answer": raw_by_q.get(q) or "Data unavailable."})
    return out


def _validate_growth_quality(analysis: dict, subject: CompanyData) -> dict:
    """
    Defensively coerce the LLM's Phase-1 response. Mirrors the defensive-
    coercion pattern used in models/earnings_quality.py's
    _validate_earnings_quality — never assume a field the LLM returns has
    the expected type or is even present.
    """
    if not isinstance(analysis, dict):
        analysis = {}
    caps_raw = analysis.get("capabilities")
    caps_raw = caps_raw if isinstance(caps_raw, dict) else {}
    years = get_history_years(subject)

    capabilities = {}
    for cap_id, meta in GQ_CAPABILITY_META.items():
        raw = caps_raw.get(cap_id) if isinstance(caps_raw.get(cap_id), dict) else {}
        capabilities[cap_id] = {
            "customers_table": _coerce_metric_table(raw.get("customers_table"), years),
            "momentum_table": _coerce_metric_table(raw.get("momentum_table"), years),
            "discussion": _coerce_discussion(raw.get("discussion"), meta["sub_questions"]),
            "why_it_matters": (str(raw.get("why_it_matters")).strip()
                                if raw.get("why_it_matters") else
                                "Insufficient data was returned by the model for this section."),
        }

    analysis["capabilities"] = capabilities
    analysis["phase"] = "Phase 1 — Building the Evidence"
    analysis["capabilities_completed"] = len(GQ_CAPABILITY_META)
    analysis["capabilities_total"] = GQ_CAPABILITIES_TOTAL
    return analysis
