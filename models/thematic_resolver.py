"""
thematic_resolver.py — LLM-powered thematic universe resolver.

When the user types something like "Top 10 European Defense Companies"
or "Largest Japanese automakers", this module asks the LLM to produce
a ranked list of publicly traded tickers matching the description.

The returned row format matches screener_eodhd.screen_index() so the
existing screener table UI and "Run [framework] on all N" button work
without changes.
"""

from __future__ import annotations
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an expert financial analyst with comprehensive knowledge of "
    "global publicly listed companies. Return ONLY valid JSON, no prose."
)

_PROMPT_TPL = """\
The user wants to analyse: "{description}"
Requested limit: {limit} companies.

Return a JSON array of the {limit} most relevant publicly traded companies
that match this description, ranked from most to least relevant/prominent.

Each element must have EXACTLY these keys:
  "rank"   : integer starting at 1
  "ticker" : Yahoo Finance format (e.g. "RHM.DE", "LMT", "7203.T", "BA.L")
  "name"   : full company name
  "sector" : GICS sector (e.g. "Industrials", "Technology", "Energy")
  "note"   : one short phrase explaining why this company fits (max 10 words)

Rules:
- Include exchange suffix for non-US stocks (e.g. .DE .PA .L .MI .AS .ST .HE .CO)
- Only include companies that are ACTUALLY publicly listed
- If fewer than {limit} clearly fit, return fewer — do NOT invent companies
- Return raw JSON array only — no markdown, no code fences, no extra keys

Example output for "Top 3 German automakers":
[
  {{"rank":1,"ticker":"BMW.DE","name":"Bayerische Motoren Werke AG","sector":"Consumer Cyclical","note":"Germany's largest premium car group"}},
  {{"rank":2,"ticker":"MBG.DE","name":"Mercedes-Benz Group AG","sector":"Consumer Cyclical","note":"Luxury cars and vans, Daimler successor"}},
  {{"rank":3,"ticker":"VOW3.DE","name":"Volkswagen AG","sector":"Consumer Cyclical","note":"World's largest automaker by volume"}}
]
"""


def resolve_thematic(
    description: str,
    limit: int,
    llm_client,          # agents.llm_client.LLMClient
) -> list[dict]:
    """
    Ask the LLM for a list of tickers matching `description`.

    Returns a list of dicts compatible with screener_eodhd row format:
      {rank, ticker, name, sector, note, price=None, market_cap=None, ...}

    Raises ValueError if the LLM returns no usable tickers.
    """
    limit = max(1, min(limit or 10, 50))
    prompt = _PROMPT_TPL.format(description=description, limit=limit)

    raw = llm_client.generate_json(prompt, _SYSTEM, max_tokens=1200)

    # generate_json returns a dict; but we expect a list here.
    # Handle both: if it returned a dict with a list value, unwrap it.
    rows: list[Any] = []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        # LLM sometimes wraps the array: {"companies": [...]} or {"result": [...]}
        for v in raw.values():
            if isinstance(v, list):
                rows = v
                break

    if not rows:
        raise ValueError(
            f"LLM returned no ticker list for: {description!r}"
        )

    # Normalise rows to screener-compatible format
    result = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        ticker = str(r.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        result.append({
            "rank":       int(r.get("rank") or i + 1),
            "ticker":     ticker,
            "name":       str(r.get("name") or ticker),
            "sector":     str(r.get("sector") or "—"),
            "note":       str(r.get("note") or ""),
            # Placeholders — filled by Gravity/Fisher when they fetch data
            "price":       None,
            "currency":    None,
            "market_cap":  None,
            "pe_ratio":    None,
            "roe":         None,
            "ebit_margin": None,
            "net_margin":  None,
            "div_yield":   None,
            "sort_value":  None,
        })

    if not result:
        raise ValueError(
            f"LLM returned rows but none had valid tickers for: {description!r}"
        )

    return sorted(result, key=lambda x: x["rank"])
