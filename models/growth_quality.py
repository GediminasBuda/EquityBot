"""
growth_quality.py — Growth Quality Score (GQS) model.

Evaluates whether a company exhibits the structural traits of a durable
long-term compounder — not whether it is cheap today. The model runs in two
sequential phases, Phase 2 only ever running after Phase 1 has produced
evidence for all 6 capabilities:

  Phase 1 — Build the Evidence: for each capability, construct historical
  data tables, identify long-term trends, and discuss both positive and
  negative evidence. No scoring in this phase.

  Phase 2 — Scoring: a second, separate LLM call is given the full Phase 1
  evidence (discussion Q&A + why-it-matters narrative for all 6
  capabilities) and asked to assign a 0-100 score with a 3-5 sentence
  justification per capability, plus an Overall Verdict synthesis. The
  weighted composite Growth Quality Score (GQS) is never trusted to LLM
  arithmetic — it is computed deterministically in Python from the
  per-capability scores using GQ_PHASE2_WEIGHTS, the same "verify, don't
  trust the model's math" pattern used throughout this codebase.

There are 6 capabilities total in the final model. GQ_CAPABILITY_META below
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
# Single source of truth for which of the 6 capabilities are currently live.
# "sub_questions" are the specific discussion questions the LLM must answer,
# in order, for this capability — used both to build the prompt instructions
# and as validation defaults if the model omits one.
GQ_CAPABILITY_META = {
    "demand_strength": {
        "number": 1,
        "title": "Demand Strength",
        "question": "Does the world increasingly want this company's products?",
        "interpretation": "Is demand durable, broad-based, and accelerating?",
        "sub_questions": [
            "Is demand accelerating?",
            "Is growth becoming more diversified?",
            "Is growth broad-based or concentrated?",
            "Is the company gaining market relevance?",
            "Is demand cyclical or structural?",
        ],
        "tables": ["customers_table", "momentum_table"],
        "table_labels": {
            "customers_table": "Customers",
            "momentum_table": "Commercial Momentum",
        },
    },
    "economic_engine": {
        "number": 2,
        "title": "Economic Engine",
        "question": "Does each additional customer create increasing economic value?",
        "interpretation": "Are unit economics healthy and improving?",
        "sub_questions": [
            "Are unit economics improving?",
            "Is profitability emerging naturally?",
            "Is scale improving economics?",
        ],
        "tables": ["unit_economics_table"],
        "table_labels": {
            "unit_economics_table": "Unit Economics",
        },
    },
    "operating_leverage": {
        "number": 3,
        "title": "Operating Leverage",
        "question": "Does scale make the business stronger?",
        "interpretation": "Is scale translating into efficiency and profitability?",
        "sub_questions": [
            "Is operating leverage emerging?",
            "Is the company becoming more efficient?",
            "Which expenses scale?",
            "Which expenses do not?",
        ],
        "tables": ["opex_detail_table"],
        "table_labels": {
            "opex_detail_table": "Operating Expense Detail",
        },
    },
    "capital_allocation": {
        "number": 4,
        "title": "Capital Allocation",
        "question": "Does management allocate capital intelligently?",
        "interpretation": "Is management investing capital intelligently and creating long-term value?",
        "sub_questions": [
            "Is management creating value?",
            "Are investments producing measurable returns?",
            "Is dilution justified?",
        ],
        "tables": ["capital_deployment_table"],
        "table_labels": {
            "capital_deployment_table": "Capital Deployment (SBC, Acquisitions)",
        },
    },
    "competitive_position": {
        "number": 5,
        "title": "Competitive Position",
        "question": "Is the competitive moat strengthening?",
        "interpretation": "Is the company's moat strengthening or eroding?",
        "sub_questions": [
            "What evidence suggests a moat?",
            "What evidence weakens the moat?",
        ],
        "tables": ["moat_table"],
        "table_labels": {
            "moat_table": "Moat Indicators",
        },
    },
    "management_governance": {
        "number": 6,
        "title": "Management & Governance Quality",
        "question": "Can shareholders trust management to maximize long-term intrinsic value rather than quarterly earnings?",
        "interpretation": "Should long-term owners trust this management team to "
                           "compound capital over decades?",
        "sub_questions": [
            "Does management think like owners?",
            "Do incentives align with shareholders?",
            "Are they building intrinsic value or maximizing short-term stock price?",
            "Would you be comfortable owning this company for ten years with this management team?",
        ],
        "tables": ["ownership_governance_table", "guidance_execution_table"],
        "table_labels": {
            "ownership_governance_table": "Ownership & Governance",
            "guidance_execution_table": "Guidance vs. Actual Execution",
        },
    },
}

GQ_CAPABILITY_ORDER = list(GQ_CAPABILITY_META.keys())
GQ_CAPABILITIES_TOTAL = 6  # total planned capabilities in the final model

# ── Phase 2 — Scoring weights ───────────────────────────────────────────────
# Demand Strength and Management & Governance Quality are weighted highest
# (20% each) per the model's explicit design: durable demand and trustworthy
# management are treated as the two highest-conviction predictors of a
# decades-long compounder. The remaining four capabilities are weighted
# evenly at 15% each. Sums to exactly 1.0 — enforced by an assertion below so
# a future edit that breaks the weighting is caught immediately, not silently.
GQ_PHASE2_WEIGHTS = {
    "demand_strength": 0.20,
    "economic_engine": 0.15,
    "operating_leverage": 0.15,
    "capital_allocation": 0.15,
    "competitive_position": 0.15,
    "management_governance": 0.20,
}
assert abs(sum(GQ_PHASE2_WEIGHTS.values()) - 1.0) < 1e-9
assert set(GQ_PHASE2_WEIGHTS.keys()) == set(GQ_CAPABILITY_META.keys())


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
    "economic_engine": """\
CAPABILITY 2 — ECONOMIC ENGINE
Question: Does each additional customer create increasing economic value?

A verified Gross Margin / EBITDA Margin / Operating Margin / Gross Profit Growth \
table (computed from the company's own reported financials) is already provided \
to you in the data block below — treat those figures as ground truth, do not \
recompute or contradict them, and do not repeat them in your JSON response.

For the "economic_engine" key, return the following, covering approximately the \
last ten fiscal years (or since IPO if shorter) wherever the company plausibly \
discloses or you can reliably derive the data, oldest fiscal year first, using \
exactly the same fiscal years and order as the verified revenue table in the \
data block below.

Only include a row for a metric the company actually discloses, or that you can \
reliably derive, in at least one of those years. If a metric is never available \
at all across the whole history (e.g. this company has never disclosed CAC or \
LTV, common outside subscription/SaaS businesses), OMIT that row entirely — do \
not include a row filled entirely with "Data unavailable". If literally none of \
this table's metrics are available for this company, return an empty "rows" \
list — do not invent placeholder rows.

1. "unit_economics_table": an object {"years": [...], "rows": [{"metric": "...", \
"values": [...]}, ...]} where "years" is the same year list described above and \
each row's "values" list has exactly one entry per year. Candidate metrics \
(include only those disclosed or reliably derivable — never invent a figure): \
"Contribution Margin", "Gross Profit per Customer", "LTV", "CAC", "LTV/CAC", \
"CAC Payback (months)", "Revenue per Customer", "Revenue per Employee". Derive \
"Gross Profit per Customer" and "Revenue per Customer" by dividing the verified \
Gross Profit / Revenue figures by whatever customer or subscriber count is \
disclosed elsewhere in this report (e.g. Capability 1's customers_table) — show \
your derived figure rather than a raw disclosure, and prefix it with a tilde if \
the underlying customer count itself was tilde-marked. Derive "Revenue per \
Employee" from the verified revenue figures and the employee count supplied in \
the data block (current year is verified; prior years may use your own \
recalled knowledge, tilde-prefixed). LTV, CAC, LTV/CAC and CAC Payback are \
usually disclosed only by subscription/SaaS-style businesses — if this company \
has never disclosed or discussed these, omit those rows entirely rather than \
guessing.

2. "discussion": an array of exactly 3 objects, each {"question": "...", \
"answer": "..."}, addressing IN THIS EXACT ORDER:
   a. Are unit economics improving?
   b. Is profitability emerging naturally?
   c. Is scale improving economics?
Each answer should be 60-120 words, cite specific numbers from the data \
provided, and discuss both positive and negative evidence — do not present only \
a bullish case.

3. "why_it_matters": a single 100-180 word paragraph explaining how the evidence \
above supports — or contradicts — the existence of an economic engine where \
each additional customer creates increasing economic value for this company. \
This is evidence-gathering only: do NOT assign a score, grade, or rating of any \
kind.
""",
    "operating_leverage": """\
CAPABILITY 3 — OPERATING LEVERAGE
Question: Does scale make the business stronger?

A verified SG&A % / R&D % / Operating Expenses % / Incremental Operating \
Margin / Incremental EBITDA Margin / Incremental Free Cash Flow Margin table \
(computed from the company's own reported financials) is already provided to \
you in the data block below — treat those figures as ground truth, do not \
recompute or contradict them, and do not repeat them in your JSON response.

For the "operating_leverage" key, return the following, covering approximately \
the last ten fiscal years (or since IPO if shorter) wherever the company \
plausibly discloses or you can reliably derive the data, oldest fiscal year \
first, using exactly the same fiscal years and order as the verified revenue \
table in the data block below.

Only include a row for a metric the company actually discloses, or that you can \
reliably derive, in at least one of those years. Most companies report SG&A as \
a single combined line item without splitting Sales & Marketing from General & \
Administrative — only include those two rows if the company's own disclosures \
(e.g. 10-K MD&A, investor presentations) actually break the expense out \
separately. If a metric is never available at all across the whole history, \
OMIT that row entirely — do not include a row filled entirely with "Data \
unavailable". If literally none of this table's metrics are available for this \
company, return an empty "rows" list — do not invent placeholder rows.

1. "opex_detail_table": an object {"years": [...], "rows": [{"metric": "...", \
"values": [...]}, ...]} where "years" is the same year list described above and \
each row's "values" list has exactly one entry per year. Candidate metrics \
(include only those disclosed or reliably derivable — never invent a figure): \
"Sales & Marketing %", "General & Administrative %". These are the only two \
metrics this table should ever contain — SG&A %, R&D %, Operating Expenses, and \
the three incremental-margin metrics are already covered by the verified table \
in the data block and must not be repeated here.

2. "discussion": an array of exactly 4 objects, each {"question": "...", \
"answer": "..."}, addressing IN THIS EXACT ORDER:
   a. Is operating leverage emerging?
   b. Is the company becoming more efficient?
   c. Which expenses scale?
   d. Which expenses do not?
Each answer should be 60-120 words, cite specific numbers from the data \
provided, and discuss both positive and negative evidence — do not present only \
a bullish case.

3. "why_it_matters": a single 100-180 word paragraph explaining how the evidence \
above supports — or contradicts — the existence of operating leverage where \
scale makes this company structurally stronger over time. This is \
evidence-gathering only: do NOT assign a score, grade, or rating of any kind.
""",
    "capital_allocation": """\
CAPABILITY 4 — CAPITAL ALLOCATION
Question: Does management allocate capital intelligently?

A verified R&D Spending / CapEx / Share Count / Dilution / ROIC / FCF Conversion / \
Cash Balance table (computed from the company's own reported financials) is already \
provided to you in the data block below — treat those figures as ground truth, do \
not recompute or contradict them, and do not repeat them in your JSON response.

For the "capital_allocation" key, return the following, covering approximately the \
last ten fiscal years (or since IPO if shorter) wherever the company plausibly \
discloses or you can reliably estimate the data, oldest fiscal year first, using \
exactly the same fiscal years and order as the verified revenue table in the data \
block below.

Only include a row for a metric the company actually discloses, or that you can \
reliably estimate, in at least one of those years. If a metric is never available \
at all across the whole history (e.g. this company has never made an acquisition), \
OMIT that row entirely — do not include a row filled entirely with "Data \
unavailable". If literally none of this table's metrics are available for this \
company, return an empty "rows" list — do not invent placeholder rows.

1. "capital_deployment_table": an object {"years": [...], "rows": [{"metric": "...", \
"values": [...]}, ...]} where "years" is the same year list described above and \
each row's "values" list has exactly one entry per year. Candidate metrics \
(include only those disclosed or reliably estimable — never invent a figure): \
"Stock-Based Compensation", "Cash Paid for Acquisitions", "Acquired Revenue \
(disclosed or estimated)", "Return on Acquisitions (if estimable)". R&D Spending, \
CapEx, Share Count, Dilution, ROIC, FCF Conversion and Cash Balance are already \
covered by the verified table in the data block and must not be repeated here — do \
not add rows for them. "Return on Acquisitions" should only be populated when the \
company's own disclosures (or credible outside analysis in your general knowledge) \
give enough to estimate acquired revenue/earnings against the price paid — \
otherwise omit the row entirely rather than guessing at a return figure.

2. "discussion": an array of exactly 3 objects, each {"question": "...", \
"answer": "..."}, addressing IN THIS EXACT ORDER:
   a. Is management creating value?
   b. Are investments producing measurable returns?
   c. Is dilution justified?
Each answer should be 60-120 words, cite specific numbers from the data \
provided, and discuss both positive and negative evidence — do not present only \
a bullish case.

3. "why_it_matters": a single 100-180 word paragraph explaining how the evidence \
above supports — or contradicts — the case that management allocates capital \
intelligently on behalf of shareholders. This is evidence-gathering only: do NOT \
assign a score, grade, or rating of any kind.
""",
    "competitive_position": """\
CAPABILITY 5 — COMPETITIVE POSITION
Question: Is the competitive moat strengthening?

A verified Gross Margin Stability table (year-over-year gross margin change and \
3-year rolling gross margin volatility, computed from the company's own reported \
financials) is already provided to you in the data block below — treat those \
figures as ground truth, do not recompute or contradict them, and do not repeat \
them in your JSON response. A flat-to-widening margin over time is evidence of \
pricing power / a durable moat; a narrowing or volatile margin is evidence of \
competitive pressure.

For the "competitive_position" key, return the following, covering approximately \
the last ten fiscal years (or since IPO if shorter) wherever the company plausibly \
discloses or you can reliably estimate the data, oldest fiscal year first, using \
exactly the same fiscal years and order as the verified revenue table in the data \
block below.

Only include a row for a metric the company actually discloses, or that you can \
reliably estimate from your own general knowledge (10-K disclosures, competitor \
comparisons, industry/analyst reports), in at least one of those years. If a \
metric is never available at all across the whole history, OMIT that row \
entirely — do not include a row filled entirely with "Data unavailable". If \
literally none of this table's metrics are available for this company, return an \
empty "rows" list — do not invent placeholder rows.

1. "moat_table": an object {"years": [...], "rows": [{"metric": "...", \
"values": [...]}, ...]} where "years" is the same year list described above and \
each row's "values" list has exactly one entry per year. Candidate metrics \
(include only those disclosed or reliably estimable — never invent a figure): \
"Market Share (%)", "Pricing / Average Selling Price Trend", "Customer Retention \
Rate", "Net Revenue Retention (NRR)", "Gross Revenue Retention (GRR)", "Churn \
Rate", "Ecosystem Partnerships (count)", "Developer / Platform Ecosystem Growth". \
Brand Strength and Switching Costs are rarely disclosed as a time-series figure — \
do NOT force them into this table; address them narratively in the discussion \
and why_it_matters text instead, citing specific qualitative evidence (10-K \
disclosures, competitor comparisons, industry reports).

2. "discussion": an array of exactly 2 objects, each {"question": "...", \
"answer": "..."}, addressing IN THIS EXACT ORDER:
   a. What evidence suggests a moat?
   b. What evidence weakens the moat?
Each answer should be 120-200 words, cite specific numbers from the data \
provided plus qualitative evidence from 10-K disclosures, competitor \
comparisons, and industry reports where relevant (this includes Gross Margin \
Stability, Pricing, Market Share, Customer Retention, NRR, Churn, Ecosystem \
Expansion, Partnerships, Developer Growth, Brand Strength, and Switching Costs \
— cover whichever of these are actually evidenced for this company), and \
discuss both sides honestly — do not present only a bullish case.

3. "why_it_matters": a single 100-180 word paragraph explaining how the evidence \
above supports — or contradicts — a strengthening competitive moat, and why moat \
durability matters for this company's prospects as a long-term compounder. This \
is evidence-gathering only: do NOT assign a score, grade, or rating of any kind.
""",
    "management_governance": """\
CAPABILITY 6 — MANAGEMENT & GOVERNANCE QUALITY
Question: Can shareholders trust management to maximize long-term intrinsic \
value rather than quarterly earnings?

A verified Ownership & Governance Snapshot (current insider ownership %, \
institutional ownership %, and named executive officers, sourced from EODHD — \
or from yfinance when EODHD/EDGAR did not have the figure) is already provided \
to you in the data block below — treat those figures as ground truth for the \
most recent year, do not recompute or contradict them, and do not repeat them \
verbatim in your JSON response. Note that unlike the financial tables used by \
earlier capabilities, this snapshot is CURRENT-YEAR ONLY — EODHD, EDGAR, and \
yfinance do not expose a historical time series of insider or institutional \
ownership percentages, so every earlier year in "ownership_governance_table" \
below must come from your own general knowledge (proxy statements, 10-K \
disclosures, investor-relations history), tilde-prefixed per the rules below. \
If neither EODHD nor yfinance had a value and you also do not know it, rely \
entirely on your own general knowledge for this capability, per the standard \
"Data unavailable" rule — never leave a field blank without that marker.

For the "management_governance" key, return the following, covering \
approximately the last ten fiscal years (or since IPO if shorter) wherever you \
can reliably determine or recall the data, oldest fiscal year first, using \
exactly the same fiscal years and order as the verified revenue table in the \
data block below.

Only include a row for a metric you can reliably determine or recall, in at \
least one of those years. A firm "No" (e.g. this company has never had a \
dual-class structure) is a real, informative answer and should be kept, not \
omitted — only omit a row if you have no reliable basis for any year at all. \
If literally nothing about this company's governance is known to you, return \
an empty "rows" list for that table — do not invent placeholder values.

1. "ownership_governance_table": an object {"years": [...], "rows": \
[{"metric": "...", "values": [...]}, ...]} where "years" is the same year list \
described above and each row's "values" list has exactly one entry per year. \
Candidate metrics (include only those you can reliably determine or recall — \
never invent a figure): "Founder-Led (Yes/No)", "CEO Tenure (Years)", "CFO \
Tenure (Years)", "Insider Ownership %", "Institutional Ownership %", "Net \
Insider Buying/Selling", "Dual-Class Shares (Yes/No)", "Voting Structure". For \
"Insider Ownership %" and "Institutional Ownership %", use the verified \
current-year snapshot figure for the most recent year and your own recalled \
knowledge (tilde-prefixed) for earlier years. "Voting Structure" should be a \
short descriptor (e.g. "One share, one vote" or "Class A/B, founder \
super-voting rights"), not a long explanation — save detail for the discussion.

2. "guidance_execution_table": an object with the same shape as above. \
Candidate metrics (include only those you can reliably recall — never invent a \
figure): "Revenue Guidance (Company-Issued)", "Revenue Actual", "Revenue \
Guidance Beat/Miss (%)", "Margin Guidance (Company-Issued)", "Margin Actual". \
Populate from your own knowledge of this company's quarterly/annual guidance \
history versus what it subsequently reported — tilde-prefix every value here, \
since none of it is in the verified data block. If this company does not issue \
formal forward guidance (common outside the US, and for many smaller or newer \
companies), return an empty "rows" list for this table rather than guessing.

3. "discussion": an array of exactly 4 objects, each {"question": "...", \
"answer": "..."}, addressing IN THIS EXACT ORDER:
   a. Does management think like owners?
   b. Do incentives align with shareholders?
   c. Are they building intrinsic value or maximizing short-term stock price?
   d. Would you be comfortable owning this company for ten years with this \
management team?
Each answer should be 100-170 words and cite specific evidence from the tables \
above plus qualitative evidence from 10-K/proxy disclosures, competitor \
comparisons, and industry reports where relevant. Across the four answers, \
collectively address whether long-term targets have been achieved, whether \
strategy has been consistent over time, how well past M&A has been integrated, \
whether capital allocation has shown discipline, and whether management has \
been shareholder-friendly (buybacks/dividends vs. dilution, related-party \
transactions, executive compensation structure) — you do not need one answer \
per theme; weave each theme into whichever question it most naturally \
supports. Discuss both sides honestly — do not present only a bullish case.

4. "why_it_matters": a single 100-180 word paragraph explaining why management \
and governance quality matters for whether this company can be trusted to \
compound intrinsic value over a ten-year holding period. This is \
evidence-gathering only: do NOT assign a score, grade, or rating of any kind.
""",
}


def _build_gq_cacheable() -> str:
    def _cap_schema(cap_id: str) -> str:
        tables_json = ",\n".join(
            f'      "{t}": {{"years": [...], "rows": '
            f'[{{"metric": "...", "values": [...]}}]}}'
            for t in GQ_CAPABILITY_META[cap_id]["tables"]
        )
        return (
            f'    "{cap_id}": {{\n'
            f'{tables_json},\n'
            f'      "discussion": [{{"question": "...", "answer": "..."}}, ...],\n'
            f'      "why_it_matters": "..."\n'
            f'    }}'
        )

    schema_example = ",\n".join(_cap_schema(cap_id) for cap_id in GQ_CAPABILITY_ORDER)
    all_table_names = [
        t for cap_id in GQ_CAPABILITY_ORDER for t in GQ_CAPABILITY_META[cap_id]["tables"]
    ]
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
        f"- For the LLM-built tables ({', '.join(all_table_names)} only) in a "
        "fiscal year NOT covered by those verified blocks — e.g. a single "
        "annual report excerpt typically only states the most recent 1-2 "
        "years verbatim, per standard SEC MD&A practice — you may still "
        "supply a value from your own general knowledge, but you MUST prefix "
        "it with a tilde (\"~\") to mark it as recalled/unverified rather "
        "than sourced from the data provided (e.g. \"~480 million\"). Never "
        "prefix a figure with \"~\" if it IS present in the verified data "
        "blocks.\n"
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


def compute_profitability_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    """
    Return a chronological (oldest-first) list of
    {"year", "gross_margin", "ebitda_margin", "operating_margin",
    "gross_profit_growth"} dicts computed from company.annual_financials.
    Growth rate is None when the required historical gross-profit figure
    isn't available. Feeds capability 2 (Economic Engine) the same way
    compute_revenue_table() feeds capability 1 (Demand Strength).
    """
    years = get_history_years(company, max_years=max_years)
    rows = []
    for i, y in enumerate(years):
        af = company.annual_financials.get(y)
        gp = af.gross_profit if af else None

        gp_growth = None
        if i > 0 and gp is not None:
            prev = company.annual_financials.get(years[i - 1])
            prev_gp = prev.gross_profit if prev else None
            if prev_gp:
                gp_growth = (gp / prev_gp) - 1

        rows.append({
            "year": y,
            "gross_margin": af.gross_margin if af else None,
            "ebitda_margin": af.ebitda_margin if af else None,
            "operating_margin": af.ebit_margin if af else None,
            "gross_profit_growth": gp_growth,
        })
    return rows


def compute_operating_leverage_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    """
    Return a chronological (oldest-first) list of
    {"year", "sga_pct", "rd_pct", "opex_pct", "incremental_operating_margin",
    "incremental_ebitda_margin", "incremental_fcf_margin"} dicts computed from
    company.annual_financials. Incremental margins are the change in EBIT /
    EBITDA / FCF divided by the change in revenue vs. the prior year — None
    when either figure isn't available or the revenue delta is zero. Feeds
    capability 3 (Operating Leverage) the same way compute_profitability_table()
    feeds capability 2 (Economic Engine).
    """
    years = get_history_years(company, max_years=max_years)
    rows = []
    for i, y in enumerate(years):
        af = company.annual_financials.get(y)
        rev = af.revenue if af else None
        sga = af.sga if af else None
        # getattr() defends against stale AnnualFinancials class instances
        # still in memory from before these fields were added (see CLAUDE.md
        # "AttributeError on ttm_revenue for cached objects").
        rd = getattr(af, "research_development", None) if af else None
        opex = getattr(af, "total_operating_expenses", None) if af else None
        if opex is None and (sga is not None or rd is not None):
            opex = (sga or 0) + (rd or 0)

        sga_pct = (sga / rev) if (sga is not None and rev) else None
        rd_pct = (rd / rev) if (rd is not None and rev) else None
        opex_pct = (opex / rev) if (opex is not None and rev) else None

        inc_op_margin = inc_ebitda_margin = inc_fcf_margin = None
        if i > 0 and rev is not None:
            prev = company.annual_financials.get(years[i - 1])
            prev_rev = prev.revenue if prev else None
            d_rev = (rev - prev_rev) if prev_rev is not None else None
            if d_rev:
                ebit, prev_ebit = (af.ebit if af else None), (prev.ebit if prev else None)
                if ebit is not None and prev_ebit is not None:
                    inc_op_margin = (ebit - prev_ebit) / d_rev

                ebitda, prev_ebitda = (af.ebitda if af else None), (prev.ebitda if prev else None)
                if ebitda is not None and prev_ebitda is not None:
                    inc_ebitda_margin = (ebitda - prev_ebitda) / d_rev

                fcf, prev_fcf = (af.fcf if af else None), (prev.fcf if prev else None)
                if fcf is not None and prev_fcf is not None:
                    inc_fcf_margin = (fcf - prev_fcf) / d_rev

        rows.append({
            "year": y,
            "sga_pct": sga_pct,
            "rd_pct": rd_pct,
            "opex_pct": opex_pct,
            "incremental_operating_margin": inc_op_margin,
            "incremental_ebitda_margin": inc_ebitda_margin,
            "incremental_fcf_margin": inc_fcf_margin,
        })
    return rows


def compute_capital_allocation_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    """
    Return a chronological (oldest-first) list of
    {"year", "rd", "capex", "shares_outstanding", "dilution", "roic",
    "fcf_conversion", "cash"} dicts computed from company.annual_financials.
    Feeds capability 4 (Capital Allocation) the same way
    compute_operating_leverage_table() feeds capability 3 (Operating
    Leverage).

    - "dilution" is the YoY % change in diluted share count (positive =
      share count grew i.e. dilution; negative = net buybacks).
    - "roic" = NOPAT / invested capital, where NOPAT = EBIT * (1 - effective
      tax rate) and invested capital = total_debt + total_equity - cash.
      None unless EBIT, tax_provision, a positive income_before_tax (needed
      for a meaningful effective tax rate), total_debt, total_equity and cash
      are all available, and invested capital is positive.
    - "fcf_conversion" = FCF / net_income. None unless net_income is
      positive — a GAAP loss makes the ratio not meaningful.
    """
    years = get_history_years(company, max_years=max_years)
    rows = []
    for i, y in enumerate(years):
        af = company.annual_financials.get(y)
        # getattr() defends against stale AnnualFinancials class instances
        # still in memory from before this field was added (see CLAUDE.md
        # "AttributeError on ttm_revenue for cached objects").
        rd = getattr(af, "research_development", None) if af else None
        capex = af.capex if af else None
        shares = af.shares_outstanding if af else None
        cash = af.cash if af else None

        dilution = None
        if i > 0 and shares is not None:
            prev = company.annual_financials.get(years[i - 1])
            prev_shares = prev.shares_outstanding if prev else None
            if prev_shares:
                dilution = (shares / prev_shares) - 1

        roic = None
        if (af and af.ebit is not None and af.income_before_tax is not None
                and af.income_before_tax > 0 and af.tax_provision is not None
                and af.total_debt is not None and af.total_equity is not None
                and cash is not None):
            tax_rate = af.tax_provision / af.income_before_tax
            nopat = af.ebit * (1 - tax_rate)
            invested_capital = af.total_debt + af.total_equity - cash
            if invested_capital > 0:
                roic = nopat / invested_capital

        fcf_conversion = None
        if af and af.fcf is not None and af.net_income is not None and af.net_income > 0:
            fcf_conversion = af.fcf / af.net_income

        rows.append({
            "year": y,
            "rd": rd,
            "capex": capex,
            "shares_outstanding": shares,
            "dilution": dilution,
            "roic": roic,
            "fcf_conversion": fcf_conversion,
            "cash": cash,
        })
    return rows


def compute_competitive_position_table(company: CompanyData, max_years: int = 10) -> list[dict]:
    """
    Return a chronological (oldest-first) list of
    {"year", "gross_margin_delta_yoy", "gross_margin_3y_stdev"} dicts,
    operationalizing "Gross Margin Stability" as verified ground truth for
    capability 5 (Competitive Position). A flat-to-widening margin over time
    is classic evidence of pricing power / a durable moat; a narrowing or
    volatile margin signals competitive pressure. Feeds capability 5 the same
    way compute_capital_allocation_table() feeds capability 4.
    """
    years = get_history_years(company, max_years=max_years)
    margins = []
    for y in years:
        af = company.annual_financials.get(y)
        margins.append(af.gross_margin if af else None)

    rows = []
    for i, y in enumerate(years):
        gm = margins[i]

        delta = None
        if i > 0 and gm is not None and margins[i - 1] is not None:
            delta = gm - margins[i - 1]

        stdev3 = None
        if i >= 2:
            window = [m for m in margins[i - 2:i + 1] if m is not None]
            if len(window) == 3:
                mean = sum(window) / 3
                var = sum((m - mean) ** 2 for m in window) / 3
                stdev3 = var ** 0.5

        rows.append({
            "year": y,
            "gross_margin_delta_yoy": delta,
            "gross_margin_3y_stdev": stdev3,
        })
    return rows


def compute_governance_snapshot(company: CompanyData) -> dict:
    """
    Verified, current-year-only ownership/governance facts read directly from
    `company` — populated by EODHD (primary; see eodhd_only_builder.py) and
    backfilled from yfinance by fetch_governance_fallback() below when EODHD/
    EDGAR did not have a value, per capability 6's explicit
    "EODHD/EDGAR first, yfinance/LLM as fallback" requirement.

    Unlike every other compute_*_table() function above, this is NOT a
    chronological history — EODHD/EDGAR/yfinance only ever expose a current
    snapshot of insider/institutional ownership, never a historical time
    series, so it can't be built as a year-columns table. Returns only the
    keys that have real data; never fabricates a value.
    """
    out = {}
    if company.pct_insiders is not None:
        out["pct_insiders"] = company.pct_insiders
    if company.pct_institutions is not None:
        out["pct_institutions"] = company.pct_institutions
    if company.officers:
        out["officers"] = company.officers
    return out


def fetch_governance_fallback(ticker: str) -> dict:
    """
    Best-effort yfinance fallback for ownership/governance data, used only to
    fill in fields EODHD/EDGAR did not provide (capability 6's data-source
    waterfall: EODHD -> EDGAR -> yfinance -> LLM general knowledge).

    Never raises — any failure (bad ticker, no data, network error, missing
    yfinance columns) is swallowed and results in an empty/partial dict, so a
    fallback lookup can never break report generation. Caller is responsible
    for fill-only merging the results onto an already-built CompanyData
    object (never overwrite a value EODHD already populated).
    """
    out: dict = {}
    try:
        import yfinance as yf
        yt = yf.Ticker(ticker)
        info = yt.info or {}

        pct_insiders = info.get("heldPercentInsiders")
        if pct_insiders is not None:
            out["pct_insiders"] = float(pct_insiders)

        pct_institutions = info.get("heldPercentInstitutions")
        if pct_institutions is not None:
            out["pct_institutions"] = float(pct_institutions)

        officers_raw = info.get("companyOfficers")
        if officers_raw:
            officers = []
            for o in officers_raw[:10]:
                name = o.get("name")
                title = o.get("title")
                if name:
                    officers.append({"name": name, "title": title or ""})
            if officers:
                out["officers"] = officers
    except Exception as e:
        logger.warning(f"[growth_quality] yfinance governance fallback failed for {ticker}: {e}")
        return {}

    try:
        insider_tx = yt.insider_transactions
        if insider_tx is not None and not insider_tx.empty:
            text_col = next(
                (c for c in ("Transaction", "transactionText") if c in insider_tx.columns),
                None,
            )
            shares_col = next(
                (c for c in ("Shares", "shares") if c in insider_tx.columns),
                None,
            )
            if text_col is not None:
                recent = insider_tx.head(20)
                purchases = recent[recent[text_col].astype(str).str.contains(
                    "purchase|buy", case=False, na=False)]
                sales = recent[recent[text_col].astype(str).str.contains(
                    "sale|sell", case=False, na=False)]
                n_buys, n_sells = len(purchases), len(sales)
                if n_buys or n_sells:
                    summary = (f"Last {len(recent)} insider transactions on record: "
                               f"{n_buys} purchase(s), {n_sells} sale(s)")
                    if shares_col is not None:
                        net_shares = purchases[shares_col].astype(float).sum() \
                            - sales[shares_col].astype(float).sum()
                        summary += f"; net insider shares {'bought' if net_shares >= 0 else 'sold'}: " \
                                   f"{abs(net_shares):,.0f}"
                    out["insider_activity_summary"] = summary
    except Exception as e:
        logger.warning(f"[growth_quality] yfinance insider_transactions fallback failed for {ticker}: {e}")

    return out


def get_history_years(company: CompanyData, max_years: int = 10) -> list[int]:
    """Chronological (oldest-first) fiscal years used across every GQS table,
    so the Revenue table (Python-computed) and the LLM's own tables (Customers,
    Commercial Momentum) all line up on identical year columns."""
    return list(reversed(company.sorted_years()[:max_years]))


def _b(v) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:.1f}B" if abs(v) >= 1000 else f"{v:.1f}M"


def format_growth_financials(company: CompanyData, insider_activity_summary: str = "") -> str:
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
                     f"for every LLM-built table in this report: {years}")
    else:
        lines.append("\nVERIFIED REVENUE HISTORY: no historical revenue data available for this ticker.")

    prof_rows = compute_profitability_table(company)
    if prof_rows and any(
        r["gross_margin"] is not None or r["ebitda_margin"] is not None
        or r["operating_margin"] is not None for r in prof_rows
    ):
        lines.append(f"\nVERIFIED PROFITABILITY HISTORY (oldest to newest, computed from "
                      f"reported financials — treat as ground truth):")
        header = (f"  {'Fiscal Year':<12} {'Gross Margin':>13} {'EBITDA Margin':>14} "
                  f"{'Oper. Margin':>13} {'GP Growth':>11}")
        lines.append(header)
        lines.append("  " + "-" * len(header))
        for r in prof_rows:
            gm_s = f"{r['gross_margin']*100:.1f}%" if r["gross_margin"] is not None else "n/a"
            em_s = f"{r['ebitda_margin']*100:.1f}%" if r["ebitda_margin"] is not None else "n/a"
            om_s = f"{r['operating_margin']*100:.1f}%" if r["operating_margin"] is not None else "n/a"
            gg_s = f"{r['gross_profit_growth']*100:.1f}%" if r["gross_profit_growth"] is not None else "n/a"
            lines.append(f"  {r['year']:<12} {gm_s:>13} {em_s:>14} {om_s:>13} {gg_s:>11}")

    opex_rows = compute_operating_leverage_table(company)
    if opex_rows and any(
        r["sga_pct"] is not None or r["rd_pct"] is not None or r["opex_pct"] is not None
        or r["incremental_operating_margin"] is not None
        or r["incremental_ebitda_margin"] is not None
        or r["incremental_fcf_margin"] is not None
        for r in opex_rows
    ):
        lines.append(f"\nVERIFIED OPERATING LEVERAGE HISTORY (oldest to newest, computed from "
                      f"reported financials — treat as ground truth):")
        header = (f"  {'Fiscal Year':<12} {'SG&A %':>9} {'R&D %':>8} {'Opex %':>9} "
                  f"{'Inc.Op.Mgn':>11} {'Inc.EBITDA Mgn':>15} {'Inc.FCF Mgn':>12}")
        lines.append(header)
        lines.append("  " + "-" * len(header))
        for r in opex_rows:
            sga_s = f"{r['sga_pct']*100:.1f}%" if r["sga_pct"] is not None else "n/a"
            rd_s = f"{r['rd_pct']*100:.1f}%" if r["rd_pct"] is not None else "n/a"
            opex_s = f"{r['opex_pct']*100:.1f}%" if r["opex_pct"] is not None else "n/a"
            iom_s = (f"{r['incremental_operating_margin']*100:.1f}%"
                     if r["incremental_operating_margin"] is not None else "n/a")
            iem_s = (f"{r['incremental_ebitda_margin']*100:.1f}%"
                     if r["incremental_ebitda_margin"] is not None else "n/a")
            ifm_s = (f"{r['incremental_fcf_margin']*100:.1f}%"
                     if r["incremental_fcf_margin"] is not None else "n/a")
            lines.append(f"  {r['year']:<12} {sga_s:>9} {rd_s:>8} {opex_s:>9} "
                         f"{iom_s:>11} {iem_s:>15} {ifm_s:>12}")

    capalloc_rows = compute_capital_allocation_table(company)
    if capalloc_rows and any(
        r["rd"] is not None or r["capex"] is not None or r["shares_outstanding"] is not None
        or r["dilution"] is not None or r["roic"] is not None or r["fcf_conversion"] is not None
        or r["cash"] is not None
        for r in capalloc_rows
    ):
        lines.append(f"\nVERIFIED CAPITAL ALLOCATION HISTORY (oldest to newest, computed from "
                      f"reported financials — treat as ground truth):")
        header = (f"  {'Fiscal Year':<12} {'R&D':>10} {'CapEx':>10} {'Shares(M)':>10} "
                  f"{'Dilution':>9} {'ROIC':>7} {'FCF Conv.':>10} {'Cash':>10}")
        lines.append(header)
        lines.append("  " + "-" * len(header))
        for r in capalloc_rows:
            rd_s = _b(r["rd"]) if r["rd"] is not None else "n/a"
            capex_s = _b(r["capex"]) if r["capex"] is not None else "n/a"
            shares_s = f"{r['shares_outstanding']:.1f}" if r["shares_outstanding"] is not None else "n/a"
            dil_s = f"{r['dilution']*100:.1f}%" if r["dilution"] is not None else "n/a"
            roic_s = f"{r['roic']*100:.1f}%" if r["roic"] is not None else "n/a"
            fcfc_s = f"{r['fcf_conversion']*100:.0f}%" if r["fcf_conversion"] is not None else "n/a"
            cash_s = _b(r["cash"]) if r["cash"] is not None else "n/a"
            lines.append(f"  {r['year']:<12} {rd_s:>10} {capex_s:>10} {shares_s:>10} "
                         f"{dil_s:>9} {roic_s:>7} {fcfc_s:>10} {cash_s:>10}")

    moat_rows = compute_competitive_position_table(company)
    if moat_rows and any(
        r["gross_margin_delta_yoy"] is not None or r["gross_margin_3y_stdev"] is not None
        for r in moat_rows
    ):
        lines.append(f"\nVERIFIED COMPETITIVE POSITION HISTORY — GROSS MARGIN STABILITY "
                      f"(oldest to newest, computed from reported financials — treat as "
                      f"ground truth):")
        header = f"  {'Fiscal Year':<12} {'GM Δ YoY (ppts)':>16} {'GM 3Y Stdev (ppts)':>19}"
        lines.append(header)
        lines.append("  " + "-" * len(header))
        for r in moat_rows:
            d_s = (f"{r['gross_margin_delta_yoy']*100:+.1f}"
                   if r["gross_margin_delta_yoy"] is not None else "n/a")
            s_s = (f"{r['gross_margin_3y_stdev']*100:.1f}"
                   if r["gross_margin_3y_stdev"] is not None else "n/a")
            lines.append(f"  {r['year']:<12} {d_s:>16} {s_s:>19}")

    gov = compute_governance_snapshot(company)
    if gov or insider_activity_summary:
        lines.append(f"\nVERIFIED OWNERSHIP & GOVERNANCE SNAPSHOT (current year only — "
                      f"sourced from EODHD, or yfinance when EODHD/EDGAR had no value; "
                      f"treat as ground truth for the current year; no historical time "
                      f"series is available from these sources, so earlier years must "
                      f"come from your own general knowledge, tilde-prefixed):")
        if "pct_insiders" in gov:
            lines.append(f"  Insider Ownership %: {gov['pct_insiders']*100:.1f}%")
        if "pct_institutions" in gov:
            lines.append(f"  Institutional Ownership %: {gov['pct_institutions']*100:.1f}%")
        if gov.get("officers"):
            names = "; ".join(
                f"{o.get('name', 'n/a')} ({o.get('title', 'n/a')})" for o in gov["officers"][:8]
            )
            lines.append(f"  Named Officers: {names}")
        if insider_activity_summary:
            lines.append(f"  Recent Insider Activity: {insider_activity_summary}")

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
    subject: CompanyData, annual_report_excerpts: str = "", insider_activity_summary: str = ""
) -> tuple[str, str]:
    """Return (cacheable_prefix, dynamic_content) for a single-company GQS run."""
    dynamic = (
        f"SUBJECT COMPANY TICKER: {subject.ticker}\n\n"
        + format_growth_financials(subject, insider_activity_summary=insider_activity_summary)
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
        cap_out = {}
        for table_name in meta.get("tables", []):
            cap_out[table_name] = _coerce_metric_table(raw.get(table_name), years)
        cap_out["discussion"] = _coerce_discussion(raw.get("discussion"), meta["sub_questions"])
        cap_out["why_it_matters"] = (str(raw.get("why_it_matters")).strip()
                                      if raw.get("why_it_matters") else
                                      "Insufficient data was returned by the model for this section.")
        capabilities[cap_id] = cap_out

    analysis["capabilities"] = capabilities
    analysis["phase"] = "Phase 1 — Building the Evidence"
    analysis["capabilities_completed"] = len(GQ_CAPABILITY_META)
    analysis["capabilities_total"] = GQ_CAPABILITIES_TOTAL
    return analysis


# ── Phase 2 — Scoring ────────────────────────────────────────────────────────
# Runs only after Phase 1 has produced evidence for all 6 capabilities. A
# separate LLM call is given the Phase 1 discussion/why-it-matters narrative
# (not the raw data tables — those were only ever inputs to that narrative,
# and re-sending them here would just burn tokens on numbers already digested
# into the Phase 1 text) and asked to score each capability 0-100 with a
# brief justification, then synthesize an Overall Verdict. The weighted
# composite score is computed deterministically in Python from
# GQ_PHASE2_WEIGHTS — never trust the model to do that arithmetic itself.

PHASE2_SYSTEM_PROMPT = """You are an institutional equity analyst specializing in evaluating \
high-growth public companies as potential long-term compounders.

You are now in Phase 2 of the Growth Quality Score model: Scoring. You have \
already been given, in the data block below, the completed Phase 1 evidence \
base for this company — the discussion questions/answers and "why it \
matters" synthesis for all 6 capabilities. Treat that evidence as ground \
truth; do not contradict it, and do not re-derive or restate the underlying \
figures it already cites.

Your task is to convert that qualitative evidence into a disciplined 0-100 \
score per capability, then synthesize an investment-oriented Overall \
Verdict. Score honestly — a company with genuinely weak or deteriorating \
evidence on a capability should score low on it (well below 50), even if \
other capabilities are strong. Do not default to a narrow 60-80 "safe" \
range; use the full 0-100 scale where the evidence warrants it. Do not \
compute or state a composite/weighted score yourself — that is calculated \
separately from your per-capability scores.
"""


def _gq_phase2_dynamic_prompt(subject: CompanyData, analysis: dict) -> str:
    """
    Build the Phase 2 dynamic prompt: subject identity + the Phase 1
    discussion/why_it_matters narrative for all 6 capabilities (not the raw
    tables — see module-level comment above) + the required JSON schema.
    """
    lines = [f"SUBJECT COMPANY: {subject.name or subject.ticker} ({subject.ticker})",
             f"SECTOR: {subject.sector or 'n/a'} — {subject.industry or 'n/a'}", ""]

    capabilities = analysis.get("capabilities") or {}
    for cap_id, meta in GQ_CAPABILITY_META.items():
        cap = capabilities.get(cap_id) or {}
        lines.append(f"=== CAPABILITY {meta['number']} — {meta['title'].upper()} ===")
        lines.append(f"Question: {meta['question']}")
        for item in cap.get("discussion") or []:
            q = item.get("question") or ""
            a = item.get("answer") or ""
            lines.append(f"Q: {q}\nA: {a}")
        why = cap.get("why_it_matters") or ""
        if why:
            lines.append(f"Why it matters: {why}")
        lines.append("")

    schema_example = ",\n".join(
        f'    "{cap_id}": {{"score": <0-100 integer>, "justification": "..."}}'
        for cap_id in GQ_CAPABILITY_ORDER
    )
    lines.append(
        "Return JSON in exactly this shape:\n"
        "{\n"
        '  "scores": {\n' + schema_example + "\n  },\n"
        '  "verdict": {\n'
        '    "compounder_reasons": ["...", "...", "..."],\n'
        '    "key_risks": ["...", "...", "..."],\n'
        '    "capability_to_monitor": "<one of: '
        + ", ".join(GQ_CAPABILITY_ORDER) + '>",\n'
        '    "capability_to_monitor_rationale": "...",\n'
        '    "trajectory_paragraph": "..."\n'
        "  }\n"
        "}\n\n"
        "Rules:\n"
        "- \"score\": an integer 0-100 for each of the 6 capabilities listed above.\n"
        "- \"justification\": exactly 3-5 sentences explaining that score, citing "
        "specific evidence from that capability's Q&A/why-it-matters text above — "
        "do not write generic boilerplate that could apply to any company.\n"
        "- \"compounder_reasons\": exactly 3 strings, each 1-2 sentences, the most "
        "compelling reasons this company could become a long-term compounder.\n"
        "- \"key_risks\": exactly 3 strings, each 1-2 sentences, the most "
        "significant risks that could prevent that outcome.\n"
        "- \"capability_to_monitor\": the single capability id (from the 6 listed "
        "in the schema above) that most deserves close monitoring over the next "
        "3-5 years — usually the capability with the most uncertainty or the "
        "widest range of future outcomes, not necessarily the lowest score.\n"
        "- \"capability_to_monitor_rationale\": 1-2 sentences explaining that choice.\n"
        "- \"trajectory_paragraph\": one paragraph (120-200 words) explaining "
        "whether this business appears to be STRENGTHENING, PLATEAUING, or "
        "WEAKENING as a compounding engine, synthesizing evidence across all 6 "
        "capabilities rather than restating any single one.\n"
        "- Write in English, professional institutional-equity-research tone. "
        "Prose only — no markdown formatting.\n"
        "- Do not wrap the JSON in markdown code fences.\n"
        "- Do not include a composite/weighted score anywhere in your response — "
        "that is computed separately."
    )
    return "\n".join(lines)


def _coerce_phase2_score(raw) -> int:
    """Clamp an LLM-returned score to a valid 0-100 integer, defaulting to 50
    (neutral) on any parse failure — mirrors the score-coercion pattern in
    models/fisher.py's _validate_analysis (int(round(float(...))) wrapped in
    try/except, clamped to the valid range)."""
    try:
        v = int(round(float(raw)))
    except (TypeError, ValueError):
        return 50
    return max(0, min(100, v))


def _validate_growth_quality_phase2(raw: dict, analysis: dict) -> dict:
    """
    Defensively coerce the LLM's Phase-2 response and compute the weighted
    composite Growth Quality Score deterministically in Python — never trust
    the model's own arithmetic for the weighted total.
    """
    if not isinstance(raw, dict):
        raw = {}
    scores_raw = raw.get("scores")
    scores_raw = scores_raw if isinstance(scores_raw, dict) else {}

    scores = {}
    for cap_id, meta in GQ_CAPABILITY_META.items():
        entry = scores_raw.get(cap_id) if isinstance(scores_raw.get(cap_id), dict) else {}
        score = _coerce_phase2_score(entry.get("score"))
        justification = str(entry.get("justification") or "").strip() or \
            "Insufficient data was returned by the model for this dimension."
        scores[cap_id] = {
            "number": meta["number"],
            "title": meta["title"],
            "interpretation": meta["interpretation"],
            "score": score,
            "justification": justification,
            "weight": GQ_PHASE2_WEIGHTS[cap_id],
        }

    gqs = sum(scores[cap_id]["score"] * GQ_PHASE2_WEIGHTS[cap_id]
               for cap_id in GQ_CAPABILITY_ORDER)

    verdict_raw = raw.get("verdict")
    verdict_raw = verdict_raw if isinstance(verdict_raw, dict) else {}

    def _str_list(v, n: int) -> list[str]:
        out = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
        while len(out) < n:
            out.append("Not provided by the model.")
        return out[:n]

    monitor_id = str(verdict_raw.get("capability_to_monitor") or "").strip()
    if monitor_id not in GQ_CAPABILITY_META:
        # Fall back to the lowest-scoring capability — a defensible,
        # deterministic default when the model omits/misnames this field.
        monitor_id = min(GQ_CAPABILITY_ORDER, key=lambda c: scores[c]["score"])

    verdict = {
        "compounder_reasons": _str_list(verdict_raw.get("compounder_reasons"), 3),
        "key_risks": _str_list(verdict_raw.get("key_risks"), 3),
        "capability_to_monitor": monitor_id,
        "capability_to_monitor_title": GQ_CAPABILITY_META[monitor_id]["title"],
        "capability_to_monitor_rationale": str(
            verdict_raw.get("capability_to_monitor_rationale") or ""
        ).strip() or "Not provided by the model.",
        "trajectory_paragraph": str(verdict_raw.get("trajectory_paragraph") or "").strip()
            or "Insufficient data was returned by the model for this section.",
    }

    return {
        "scores": scores,
        "weights": dict(GQ_PHASE2_WEIGHTS),
        "gqs": round(gqs, 1),
        "verdict": verdict,
        "phase": "Phase 2 — Scoring",
    }
