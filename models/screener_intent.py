"""
screener_intent.py — LLM-powered natural-language → EODHD screener filter parser.

Converts free-text queries like:
  "European defense companies, market cap above 2 billion"
  "High dividend US banks, yield over 4%"
  "Small cap German industrials"
  "Technology companies on NASDAQ with positive EPS"
  "Didžiausios Lenkijos kompanijos"

…into structured EODHD Screener API parameters.

Returns:
  {
    "filters":  [["field", "op", value], ...],
    "signals":  ["200d_new_hi"],            # optional
    "sort":     "market_capitalization.desc",
    "limit":    20,
    "title":    "Human-readable description of the search",
    "notes":    "Any clarification or assumptions made"
  }
"""

from __future__ import annotations
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a financial data expert. Convert natural-language stock screening "
    "queries into structured EODHD Screener API parameters. "
    "Return ONLY valid JSON, no prose, no markdown."
)

_INSTRUCTIONS = """\
Convert the user's query into EODHD Screener API parameters.

Return a JSON object with EXACTLY these keys:

{
  "filters": [["field", "operation", value], ...],
  "signals": ["signal_name"] | [],
  "sort":    "field_name.asc" | "field_name.desc" | null,
  "limit":   integer (5-100, default 20),
  "title":   "Short human-readable description of this search",
  "notes":   "Assumptions or ambiguities"
}

AVAILABLE FILTER FIELDS:
  String fields (operations: "=" or "match"):
    "exchange"  — EODHD exchange code (see mappings below)
    "sector"    — GICS sector name
    "industry"  — industry sub-category
    "code"      — ticker symbol
    "name"      — company name

  Numeric fields (operations: "=", ">", "<", ">=", "<="):
    "market_capitalization" — in USD (e.g. 1000000000 for $1B)
    "earnings_share"        — EPS (e.g. > 0 for profitable)
    "dividend_yield"        — fraction (e.g. 0.04 for 4%)
    "adjusted_close"        — last close price
    "refund_1d_p"           — 1-day return %
    "refund_5d_p"           — 5-day return %
    "avgvol_1d"             — last-day volume
    "avgvol_200d"           — 200-day avg volume

AVAILABLE SIGNALS (pre-calculated, use in "signals" array):
  "200d_new_hi"   — near 200-day high
  "200d_new_lo"   — near 200-day low
  "bookvalue_pos" — positive book value
  "bookvalue_neg" — negative book value

EXCHANGE CODE MAPPINGS (use EODHD codes, NOT Yahoo Finance suffixes):
  US:      NYSE, NASDAQ, AMEX
  Germany: XETRA (primary), F (Frankfurt)
  UK:      LSE
  France:  PA
  Netherlands: AS
  Belgium: BR
  Italy:   MI
  Spain:   MC
  Finland: HE
  Sweden:  ST
  Norway:  OL
  Denmark: CO
  Switzerland: SW
  Poland:  WAR
  Hungary: BUD
  Austria: VI
  Czech:   PR
  Canada:  TO
  Australia: AU
  Japan:   TSE
  Korea:   KO
  HK:      HK
  Brazil:  SA

SECTOR NAMES (exact EODHD values):
  "Technology", "Healthcare", "Financials", "Consumer Discretionary",
  "Consumer Staples", "Energy", "Materials", "Industrials", "Utilities",
  "Real Estate", "Communication Services"

STOCK INDEX → EXCHANGE MAPPING (critical — always apply):
  DAX / DAX 40        → exchange = XETRA      (Germany)
  MDAX / SDAX / TecDAX → exchange = XETRA     (Germany)
  CAC 40              → exchange = PA          (France)
  FTSE 100 / FTSE 250 → exchange = LSE         (UK)
  AEX                 → exchange = AS          (Netherlands)
  SMI                 → exchange = SW          (Switzerland)
  IBEX 35             → exchange = MC          (Spain)
  MIB / FTSE MIB      → exchange = MI          (Italy)
  OMX Helsinki / OMXH → exchange = HE          (Finland)
  OMX Stockholm       → exchange = ST          (Sweden)
  OMX Oslo / OBX      → exchange = OL          (Norway)
  OMX Copenhagen      → exchange = CO          (Denmark)
  ATX                 → exchange = VI          (Austria)
  WIG 20 / WIG        → exchange = WAR         (Poland)
  BUX                 → exchange = BUD         (Hungary)
  PX / Prague         → exchange = PR          (Czech Republic)
  S&P 500 / Dow / Nasdaq 100 → exchange = NYSE or NASDAQ
  TSX / TSX 60        → exchange = TO          (Canada)
  ASX 200             → exchange = AU          (Australia)
  Kospi               → exchange = KO          (South Korea)
  Hang Seng           → exchange = HK          (Hong Kong)
  Shanghai / CSI      → exchange = SHG         (China)
  OMX Vilnius / NasdaqVilnius → exchange = VS  (Lithuania)
  OMX Tallinn         → exchange = TL          (Estonia)
  OMX Riga            → exchange = RG          (Latvia)

RULES:
1. ALWAYS apply the index→exchange mapping above first. "DAX 40 companies" = exchange XETRA.
2. For "European" with no specific country/index → omit exchange filter, use sector/industry.
3. For country-specific (e.g. "German", "Polish"): use the primary exchange code.
4. Market cap: "large cap" > 10B, "mid cap" 2B–10B, "small cap" 200M–2B, "micro" < 200M
5. "high dividend" → dividend_yield >= 0.03
6. "profitable" / "positive earnings" → earnings_share > 0
7. For "top N" or "largest" → sort by market_capitalization.desc
8. For "cheapest" / "value" → sort by earnings_share.desc (no PE in screener)
9. limit: default 20; if user says "top 10" → 10; "top 50" → 50; max 100
10. If a filter cannot be expressed with available fields → omit it and note it
11. Return null for "sort" if not implied by the query

EXAMPLES:

Input: "European defense companies, market cap above 2 billion"
Output: {
  "filters": [["sector","=","Industrials"],["industry","match","Defense"],
              ["market_capitalization",">=",2000000000]],
  "signals": [],
  "sort": "market_capitalization.desc",
  "limit": 20,
  "title": "European Defense Companies · MCap > $2B",
  "notes": "Filtered to Industrials/Defense sector globally; exchange filter omitted to include all European exchanges"
}

Input: "High dividend US banks, yield over 4%"
Output: {
  "filters": [["exchange","=","NYSE"],["sector","=","Financials"],
              ["industry","match","Bank"],["dividend_yield",">=",0.04]],
  "signals": [],
  "sort": "dividend_yield.desc",
  "limit": 20,
  "title": "US Banks · Dividend Yield > 4%",
  "notes": "NASDAQ banks also excluded — add exchange=NASDAQ filter for broader coverage"
}

Input: "Small cap Polish companies"
Output: {
  "filters": [["exchange","=","WAR"],
              ["market_capitalization",">=",200000000],
              ["market_capitalization","<=",2000000000]],
  "signals": [],
  "sort": "market_capitalization.desc",
  "limit": 20,
  "title": "Small Cap Polish Companies · MCap $200M–$2B",
  "notes": ""
}

Input: "Nasdaq tech stocks near 200-day highs"
Output: {
  "filters": [["exchange","=","NASDAQ"],["sector","=","Technology"]],
  "signals": ["200d_new_hi"],
  "sort": "market_capitalization.desc",
  "limit": 20,
  "title": "Nasdaq Tech · Near 200-Day Highs",
  "notes": ""
}

Input: "Top 30 German industrial companies"
Output: {
  "filters": [["exchange","=","XETRA"],["sector","=","Industrials"]],
  "signals": [],
  "sort": "market_capitalization.desc",
  "limit": 30,
  "title": "Top 30 German Industrials · XETRA",
  "notes": ""
}

Input: "Didžiausios Lenkijos kompanijos"
Output: {
  "filters": [["exchange","=","WAR"]],
  "signals": [],
  "sort": "market_capitalization.desc",
  "limit": 20,
  "title": "Didžiausios Lenkijos kompanijos · GPW",
  "notes": ""
}

Input: "top 20 DAX 40 companies"
Output: {
  "filters": [["exchange","=","XETRA"]],
  "signals": [],
  "sort": "market_capitalization.desc",
  "limit": 20,
  "title": "Top 20 DAX 40 Companies · XETRA",
  "notes": "DAX 40 = German XETRA exchange; sorted by market cap descending"
}

Input: "largest CAC 40 stocks"
Output: {
  "filters": [["exchange","=","PA"]],
  "signals": [],
  "sort": "market_capitalization.desc",
  "limit": 20,
  "title": "Largest CAC 40 Stocks · Euronext Paris",
  "notes": ""
}

User query:
"""


# Hardcoded index-name → EODHD exchange code.
# Checked BEFORE and AFTER LLM — LLM cannot override this.
_INDEX_TO_EXCHANGE: list[tuple[list[str], str]] = [
    (["dax", "dax40", "dax 40"],                          "XETRA"),
    (["mdax", "sdax", "tecdax"],                          "XETRA"),
    (["cac 40", "cac40", "cac"],                          "PA"),
    (["ftse 100", "ftse100", "ftse 250", "ftse250",
      "ftse"],                                            "LSE"),
    (["aex"],                                             "AS"),
    (["smi"],                                             "SW"),
    (["ibex 35", "ibex35", "ibex"],                       "MC"),
    (["ftse mib", "mib"],                                 "MI"),
    (["omx helsinki", "omxh", "helsinki"],                "HE"),
    (["omx stockholm", "stockholm"],                      "ST"),
    (["omx oslo", "obx", "oslo"],                         "OL"),
    (["omx copenhagen", "copenhagen"],                    "CO"),
    (["atx", "vienna"],                                   "VI"),
    (["wig20", "wig 20", "wig"],                          "WAR"),
    (["bux", "budapest"],                                 "BUD"),
    (["omx vilnius", "nasdaqvilnius", "nasdaq vilnius",
      "vilnius"],                                         "VS"),
    (["omx tallinn", "tallinn"],                          "TL"),
    (["omx riga", "riga"],                                "RG"),
    (["tsx", "tsx 60", "tsx60", "s&p/tsx"],               "TO"),
    (["asx 200", "asx200", "asx"],                        "AU"),
    (["kospi"],                                           "KO"),
    (["hang seng", "hsi"],                                "HK"),
    (["shanghai", "csi 300", "csi300"],                   "SHG"),
]


def _detect_index_exchange(query: str) -> str | None:
    """Return forced EODHD exchange code if query mentions a known index, else None."""
    q = query.lower()
    for keywords, exchange in _INDEX_TO_EXCHANGE:
        if any(kw in q for kw in keywords):
            return exchange
    return None


def _enforce_exchange(intent: dict, exchange: str) -> dict:
    """Remove any existing exchange filters and insert the correct one."""
    intent["filters"] = [
        f for f in intent.get("filters", [])
        if not (isinstance(f, list) and len(f) >= 1 and f[0] == "exchange")
    ]
    intent["filters"].insert(0, ["exchange", "=", exchange])
    return intent


def parse_screener_intent(query: str, llm_client) -> dict:
    """
    Parse a natural-language screening query into EODHD Screener API params.

    Args:
        query       — free-text query from the user
        llm_client  — LLMClient instance

    Returns dict with keys: filters, signals, sort, limit, title, notes
    """
    prompt = _INSTRUCTIONS + f'"{query}"'

    try:
        result = llm_client.generate_json(prompt, _SYSTEM, max_tokens=600)
    except Exception as e:
        logger.warning(f"[screener_intent] LLM failed: {e}")
        return _fallback(query)

    if not isinstance(result, dict):
        return _fallback(query)

    return _normalise(result, query)


def _normalise(raw: dict, original_query: str) -> dict:
    out = {
        "filters": [],
        "signals": [],
        "sort":    None,
        "limit":   20,
        "title":   original_query[:80],
        "notes":   "",
    }

    # filters
    # EODHD screener API only reliably supports numeric filters.
    # String filters (exchange, sector, industry) are often ignored or
    # cause empty results. Strip them from the API call; keep numeric only.
    # Sector / industry are kept separately for post-fetch display notes.
    _NUMERIC = {
        "market_capitalization", "earnings_share", "dividend_yield",
        "adjusted_close", "refund_1d_p", "refund_5d_p",
        "avgvol_1d", "avgvol_200d",
    }
    filters = raw.get("filters")
    if isinstance(filters, list):
        api_filters = []
        skipped = []
        for f in filters:
            if not (isinstance(f, (list, tuple)) and len(f) == 3
                    and isinstance(f[0], str)):
                continue
            if f[0] in _NUMERIC:
                api_filters.append([str(f[0]), str(f[1]), f[2]])
            else:
                skipped.append(f[0])
        out["filters"] = api_filters
        if skipped:
            existing = out.get("notes") or ""
            note = f"Note: {', '.join(set(skipped))} filter(s) not supported by screener API — results are global, sorted by your criteria."
            out["notes"] = (existing + " " + note).strip()

    # signals
    signals = raw.get("signals")
    if isinstance(signals, list):
        from data_sources.eodhd_screener_api import SIGNALS
        out["signals"] = [s for s in signals if s in SIGNALS]

    # sort
    sort = raw.get("sort")
    if isinstance(sort, str) and "." in sort:
        field, _, direction = sort.partition(".")
        if direction in ("asc", "desc"):
            out["sort"] = sort

    # limit
    try:
        lim = int(raw.get("limit") or 20)
        out["limit"] = max(5, min(100, lim))
    except Exception:
        pass

    # title / notes
    t = raw.get("title")
    if isinstance(t, str) and t.strip():
        out["title"] = t.strip()[:120]
    n = raw.get("notes")
    if isinstance(n, str) and n.strip():
        out["notes"] = n.strip()[:300]

    return out


def _fallback(query: str) -> dict:
    """Minimal fallback when LLM is unavailable."""
    return {
        "filters": [],
        "signals": [],
        "sort":    "market_capitalization.desc",
        "limit":   20,
        "title":   query[:80],
        "notes":   "LLM unavailable — no filters applied, showing largest companies globally.",
    }
