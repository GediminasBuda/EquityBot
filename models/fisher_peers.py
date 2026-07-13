"""
fisher_peers.py — Peer Fisher batch scorer for "Fisher Alternatives + Peers".

The Fisher Alternatives + Peers framework keeps everything the standard
Fisher Alternatives framework produces for the subject company (15 scored
points, 7 Helmer powers, moat, key risks, conclusion, recommendation) and
adds one more page comparing the subject against up to 6 peer companies
on the same 15-point Fisher scorecard.

This module owns the peer-side LLM call:
  • takes up to 6 EODHD-only `CompanyData` peers
  • packs compact financial blocks for each into a single prompt
  • asks the LLM to return one short Fisher score block per peer
    (15 ints + total + grade + 3-6 sentence summary)
  • validates / coerces the response

Costs ~1 extra LLM call per report (compact prompt + ~600 output tokens
per peer × 6 peers ≈ 3,600 output tokens total).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from data_sources.base import CompanyData
from models.fisher import FISHER_QUESTIONS, SYSTEM_PROMPT

logger = logging.getLogger(__name__)


# ── Peer scoring system prompt ────────────────────────────────────────────────
# Re-uses the same persona as the main Fisher framework, plus a peer-batch
# scoring discipline.

_PEERS_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + "\n\n"
    + "When scoring multiple peer companies in one call:\n"
    + "- Apply the same calibration across every peer — a 5 must mean the\n"
    + "  same thing for company A and company B.\n"
    + "- Use the data provided for each peer. Where data is thin (smaller\n"
    + "  peer, limited history, missing fields), note that explicitly in the\n"
    + "  summary under 'difficult-to-score points'.\n"
    + "- Do NOT score a peer 3 by default for a missing point — instead\n"
    + "  state the assumption you made (e.g. 'assumed average for the\n"
    + "  industry'). Honesty over coverage."
)


# ── Cacheable instructions block ──────────────────────────────────────────────

def _build_peers_cacheable_block() -> str:
    questions = "\n".join(
        f"  Point {n:02d} — {title}: {q}"
        for n, title, q in FISHER_QUESTIONS
    )
    return f"""\
For each peer company provided below, score the 15 Philip Fisher questions
(1-5 each) using the same scoring discipline as the subject company's
Fisher assessment. Return a single JSON object with the structure shown.

== FISHER 15-POINT QUESTIONS (apply to every peer) ==
{questions}

Required JSON output:
{{
  "peers": [
    {{
      "ticker": "<peer's Yahoo Finance ticker>",
      "name":   "<peer name>",
      "fisher_scores": [<score for Point 1>, <Point 2>, …, <Point 15>],
                      // exactly 15 integers, each 1-5
      "total_score":   <int, sum of the 15 scores, max 75>,
      "grade":         "<A|B|C|D|F (A=65+, B=55-64, C=45-54, D=35-44, F<35)>",
      "summary":       "<3-6 sentences. Mention 2-3 strongest points by\
 number (e.g. 'P5, P6'), 2-3 weakest points by number, and which points\
 were difficult to score with explanation (e.g. 'P14 hard — limited\
 management commentary in public filings').>"
    }},
    "... (one entry per peer, in the same order as the data below)"
  ]
}}

Scoring calibration: 5 = exceptional · 4 = above average · 3 = average ·
2 = below average · 1 = poor. Most peers should sit in the 2-4 range.

All 15 scores must be present per peer. Do NOT skip questions — if data
is missing, use the most-likely-but-conservative score and call it out
in the summary's "difficult to calculate" section.

=== PEER DATA FOLLOWS ==="""


_PEERS_CACHEABLE = _build_peers_cacheable_block()


# ── Compact financial block per peer ──────────────────────────────────────────

def _format_peer_data(peer: CompanyData) -> str:
    """Compact ~30-line financial snapshot for one peer."""
    cur = peer.currency or "USD"
    lines: list[str] = []

    lines.append(f"--- PEER: {peer.name or peer.ticker} ({peer.ticker}) ---")
    lines.append(
        f"Sector: {peer.sector or 'n/a'} | Industry: {peer.industry or 'n/a'} "
        f"| Country: {peer.country or 'n/a'}"
    )
    if peer.employees:
        lines.append(f"Employees: {peer.employees:,}")
    if peer.description:
        # Short business description so the LLM has something to weigh
        # qualitative points (R&D, sales, integrity) against.
        desc = peer.description[:600]
        lines.append(f"Business: {desc}{'…' if len(peer.description) > 600 else ''}")

    # Market + valuation
    lines.append(
        f"Market cap: {_b(peer.market_cap)} {cur}M | EV: {_b(peer.enterprise_value)} {cur}M"
    )
    lines.append(
        f"P/E: {_x(peer.pe_ratio)} | EV/EBIT: {_x(peer.ev_ebit)} | "
        f"EV/Sales: {_x(peer.ev_sales)} | EV/EBITDA: {_x(peer.ev_ebitda)} | "
        f"P/B: {_x(peer.price_to_book)}"
    )
    lines.append(
        f"Margins (TTM): Gross {_pct(peer.gross_margin)} | EBIT {_pct(peer.ebit_margin)} | "
        f"Net {_pct(peer.net_margin)} | ROE {_pct(peer.roe)}"
    )
    lines.append(
        f"Balance: Net debt {_b(peer.net_debt)} {cur}M | Gearing {_x(peer.gearing)} "
        f"x Net Debt/EBITDA | Dividend yield {_pct(peer.dividend_yield)}"
    )

    # Growth
    c3 = peer.revenue_cagr(3) if hasattr(peer, "revenue_cagr") else None
    c5 = peer.revenue_cagr(5) if hasattr(peer, "revenue_cagr") else None
    lines.append(f"Growth: 3yr CAGR {_pct(c3)} | 5yr CAGR {_pct(c5)}")

    # 4-year mini history of revenue + EBIT margin
    years = peer.sorted_years()[:4] if hasattr(peer, "sorted_years") else []
    if years:
        rev_row = "Revenue history: " + ", ".join(
            f"{y}: {_b(peer.annual_financials.get(y).revenue) if peer.annual_financials.get(y) else 'n/a'}"
            for y in reversed(years)
        )
        lines.append(rev_row)
        marg_row = "EBIT margin history: " + ", ".join(
            f"{y}: {_pct(peer.annual_financials.get(y).ebit_margin) if peer.annual_financials.get(y) else 'n/a'}"
            for y in reversed(years)
        )
        lines.append(marg_row)

    return "\n".join(lines)


# ── Public entry point ────────────────────────────────────────────────────────

def build_peer_prompt(
    subject: CompanyData,
    peers: dict[str, CompanyData],
) -> tuple[str, str]:
    """
    Return (cacheable_prefix, dynamic_block) for the peer-Fisher LLM call.
    """
    if not peers:
        return _PEERS_CACHEABLE, "No peer data available."

    subject_line = (
        f"=== SUBJECT (already scored separately, included only for context) ===\n"
        f"{subject.name or subject.ticker} ({subject.ticker}) — "
        f"sector {subject.sector or 'n/a'} — "
        f"{subject.country or 'n/a'}\n"
    )

    peer_blocks = []
    for ticker, p in peers.items():
        peer_blocks.append(_format_peer_data(p))

    dynamic = subject_line + "\n" + "\n\n".join(peer_blocks)
    dynamic += (
        "\n\n"
        "Score every peer above. Return JSON with one entry per peer in the "
        "'peers' array — same order as listed."
    )
    return _PEERS_CACHEABLE, dynamic


# ── Validation / coercion ─────────────────────────────────────────────────────

def validate_peer_analysis(raw: dict, expected_tickers: list[str]) -> list[dict]:
    """
    Validate the LLM's `peers` array and align it with the expected ticker order.
    Missing peers get a placeholder entry so the PDF renders an "n/a" row.
    """
    if not isinstance(raw, dict):
        return []
    peers = raw.get("peers")
    if not isinstance(peers, list):
        return []

    # Build a lookup by ticker (case-insensitive)
    by_ticker: dict[str, dict] = {}
    for p in peers:
        if not isinstance(p, dict):
            continue
        tk = (p.get("ticker") or "").strip().upper()
        if tk:
            by_ticker[tk] = p

    out: list[dict] = []
    for exp in expected_tickers:
        exp_u = exp.upper()
        p = by_ticker.get(exp_u)
        if p is None:
            out.append({
                "ticker": exp_u, "name": exp_u,
                "fisher_scores": [3] * 15,
                "total_score":   45,
                "grade":         "C",
                "summary":       "Peer was not scored — likely no usable EODHD data was returned for this ticker.",
                "missing":       True,
            })
            continue

        # Coerce scores: must be 15 ints in 1..5
        scores_raw = p.get("fisher_scores") or []
        scores: list[int] = []
        for s in scores_raw[:15]:
            try:
                v = int(round(float(s)))
            except Exception:
                v = 3
            scores.append(max(1, min(5, v)))
        while len(scores) < 15:
            scores.append(3)

        total = sum(scores)
        # Recompute grade from total (LLM may have got the bands wrong)
        if total >= 65:    grade = "A"
        elif total >= 55:  grade = "B"
        elif total >= 45:  grade = "C"
        elif total >= 35:  grade = "D"
        else:              grade = "F"

        out.append({
            "ticker":        exp_u,
            "name":          (p.get("name") or exp_u).strip(),
            "fisher_scores": scores,
            "total_score":   total,
            "grade":         grade,
            "summary":       (p.get("summary") or "").strip()[:1200]
                              or "No summary available.",
        })
    return out


# ── LLM-suggested peers (when the user supplies none) ────────────────────────

_PEER_SUGGEST_SYSTEM = (
    "You are a buy-side analyst with deep sector knowledge. Given a subject "
    "company, suggest its 4-6 most relevant publicly-listed peer companies. "
    "Peers must trade on a major exchange and have audited financials. "
    "Return only Yahoo Finance ticker symbols — e.g. AAPL (US), RHM.DE "
    "(Germany), BA.L (London), LMT (US). Do not invent tickers."
)


_PEER_SUGGEST_PROMPT_TEMPLATE = """\
Suggest 4-6 publicly-traded peer companies for the subject below. Pick
peers that compete in the same product / customer / geographic markets,
have comparable scale (within ~5×), and would be benchmarked against
each other on Philip Fisher's 15-point qualitative framework.

== SUBJECT COMPANY ==
Name:     {name}
Ticker:   {ticker}
Sector:   {sector}
Industry: {industry}
Country:  {country}
Description: {description}

Return ONLY this JSON shape — no prose, no markdown:

{{
  "peers": [
    {{"ticker": "AAPL",  "name": "Apple Inc.",     "rationale": "Why this peer (1 short sentence)"}},
    {{"ticker": "MSFT",  "name": "Microsoft Corp.", "rationale": "…"}},
    "... up to 6 entries"
  ]
}}

Rules:
- Yahoo Finance format only — exact suffix matters (.DE, .L, .HE, .TO, .SS, etc.).
- Do NOT include the subject itself.
- Do NOT include private companies, mutual funds, ETFs, or indices.
- If you genuinely can't find peers, return an empty array.
"""


def suggest_peers(subject: CompanyData, max_peers: int = 6
                  ) -> tuple[list[str], dict]:
    """
    Ask the LLM to suggest peer tickers when the user supplied none.

    Returns:
        (tickers, usage)
          tickers — list of Yahoo Finance ticker strings (up to max_peers)
          usage   — Claude/OpenAI usage dict (or {} on failure)
    """
    try:
        from agents.llm_client import LLMClient
    except Exception:
        return [], {}

    desc = (subject.description or "")[:600]
    user_prompt = _PEER_SUGGEST_PROMPT_TEMPLATE.format(
        name=subject.name or subject.ticker or "?",
        ticker=subject.ticker or "?",
        sector=subject.sector or "n/a",
        industry=subject.industry or "n/a",
        country=subject.country or "n/a",
        description=desc or "No description available.",
    )

    try:
        llm = LLMClient()
        ready, _ = llm.check_configured()
        if not ready:
            logger.warning("[fisher_peers] LLM not configured — no peer suggestions")
            return [], {}
        raw = llm.generate_json(
            # 500 tokens was too tight — verbose/reasoning-heavy models (e.g.
            # Opus) can spend most of the budget on preamble text despite
            # "no commentary" instructions, truncating before any JSON is
            # emitted and silently yielding zero suggested peers.
            user_prompt, _PEER_SUGGEST_SYSTEM, max_tokens=1500,
        )
        usage = dict(getattr(llm, "last_usage", {}) or {})
    except Exception as e:
        logger.warning(f"[fisher_peers] suggest_peers LLM call failed: {e}")
        return [], {}

    if not isinstance(raw, dict):
        logger.warning(
            f"[fisher_peers] suggest_peers got non-dict JSON: {raw!r}. "
            f"Raw response: {getattr(llm, 'last_raw_response', '')[:500]}"
        )
        return [], usage
    peers = raw.get("peers")
    if not isinstance(peers, list):
        logger.warning(
            f"[fisher_peers] suggest_peers response had no 'peers' list: {raw!r}. "
            f"Raw response: {getattr(llm, 'last_raw_response', '')[:500]}"
        )
        return [], usage

    out: list[str] = []
    subject_u = (subject.ticker or "").upper()
    seen: set[str] = {subject_u}
    for p in peers:
        if not isinstance(p, dict):
            continue
        tk = (p.get("ticker") or "").strip().upper()
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
        if len(out) >= max_peers:
            break
    return out, usage


# ── Local formatters (kept tiny — same conventions as models/fisher.py) ───────

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
