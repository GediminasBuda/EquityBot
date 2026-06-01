"""
nl_intent.py — LLM-powered natural-language intent parser.

The Report Generator's search bar accepts both ticker symbols and free-form
prompts like "trauk pirmas 10 imoniu is SP500 pagal market cap" or
"Run Fisher on AAPL, MSFT, GOOG". This module parses the prompt and
returns a structured Intent dict that the dispatch logic can act on.

Supports Lithuanian + English + mixed phrasing.

Intent schema:
  {
    "action":       "report" | "screen" | "compare",
    "tickers":        ["AAPL", "MSFT"] | null,    # Yahoo-Finance format
    "universe":       "^GSPC" | "^NDX" | "^GDAXI" | null,
    "thematic_query": "Top 10 European defense companies" | null,
    "sort_by":        "market_cap" | "pe_ratio" | "roe" | "ebit_margin"
                      | "revenue" | "price" | "div_yield" | "fcf_yield" | null,
    "sort_dir":       "asc" | "desc" | null,
    "limit":          10 | null,
    "framework_id":   "fisher" | "gravity" | "overview_v2"
                      | "eodhd_full" | null,
    "notes":          ""    # free-text the LLM uses to flag ambiguity
  }
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ── Defaults / vocabulary ────────────────────────────────────────────────────

VALID_ACTIONS    = {"report", "screen", "compare"}
VALID_SORT_BYS   = {
    "market_cap", "pe_ratio", "roe", "ebit_margin", "revenue",
    "price", "div_yield", "fcf_yield", "net_margin", "ev_ebit",
}
VALID_SORT_DIRS  = {"asc", "desc"}
VALID_FRAMEWORKS = {
    "fisher", "gravity", "overview_v2",
    "eodhd_full", "index_overview",
}


def _empty_intent() -> dict:
    return {
        "action": None, "tickers": None, "universe": None,
        "thematic_query": None,
        "sort_by": None, "sort_dir": None, "limit": None,
        "framework_id": None, "notes": "",
    }


# ── LLM prompt (cacheable prefix + dynamic) ──────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a precise intent parser for a financial research app. "
    "Convert the user's natural-language query into a strict JSON intent. "
    "Return ONLY valid JSON, no prose, no markdown."
)


_INTENT_INSTRUCTIONS = """\
Convert the user's natural-language query into a JSON intent object with
EXACTLY these keys (use null when not specified):

{
  "action":         "report" | "screen" | "compare",
  "tickers":        ["AAPL", "MSFT", …] | null,
  "universe":       "^GSPC" | "^NDX" | "^DJI" | "^GDAXI" | "^FTSE" |
                    "^FCHI" | "^IBEX" | "^OMX" | "^N225" | "^HSI" |
                    "^KS11" | "^STOXX50E" | … | null,
  "thematic_query": "<verbatim description when no matching index exists>" | null,
  "sort_by":        "market_cap" | "pe_ratio" | "roe" | "ebit_margin" |
                    "revenue" | "price" | "div_yield" | "fcf_yield" |
                    "net_margin" | "ev_ebit" | null,
  "sort_dir":       "asc" | "desc" | null,
  "limit":          positive integer | null,
  "framework_id":   "fisher" | "gravity" | "overview_v2" |
                    "eodhd_full" | null,
  "notes":          "1 short sentence flagging ambiguity, or empty string"
}

ACTIONS
- "screen"   : user wants a filtered/sorted list of companies (top N, first N, …)
- "report"   : user wants a single research report for one or more named tickers
- "compare"  : user lists 2+ specific tickers and asks to compare them

THEMATIC UNIVERSE (critical rule)
If the user describes a thematic/sector/geographic group of companies WITHOUT
naming a known index, set action="screen", universe=null, and put the original
description verbatim into thematic_query. Examples:
  "Top 10 European defense companies"  → thematic_query="Top 10 European defense companies", universe=null
  "Largest Japanese automakers"        → thematic_query="Largest Japanese automakers", universe=null
  "Top 5 US semiconductor companies"   → thematic_query="Top 5 US semiconductor companies", universe=null
  "Best value banks in Poland"         → thematic_query="Best value banks in Poland", universe=null
  "WIG 20 top companies" → this IS an index → universe="^WIG20", thematic_query=null

TICKER NORMALISATION
- Always Yahoo Finance format: AAPL, RHM.DE, BA.L, 7203.T, ^GSPC.
- "Apple" → "AAPL"; "Rheinmetall" → "RHM.DE"; "BMW" → "BMW.DE";
  "Lockheed" → "LMT"; "S&P 500" → universe "^GSPC", NOT tickers.

UNIVERSE / INDEX RECOGNITION
Use Yahoo Finance index format (prefix with "^"). Common mappings:

  US:
    "S&P 500" / "SP500" / "SPX"                     → "^GSPC"
    "Nasdaq 100" / "NDX"                            → "^NDX"
    "Nasdaq Composite" / "Nasdaq"                   → "^IXIC"
    "Dow" / "Dow Jones" / "DJIA"                    → "^DJI"
    "Russell 1000"                                  → "^RUI"
    "Russell 2000"                                  → "^RUT"

  Europe:
    "DAX" / "DAX 40" / "Xetra DAX"                  → "^GDAXI"
    "MDAX"                                          → "^MDAXI"
    "TecDAX"                                        → "^TECDAX"
    "FTSE 100" / "FTSE" / "Footsie"                 → "^FTSE"
    "FTSE 250"                                      → "^FTMC"
    "CAC 40" / "CAC"                                → "^FCHI"
    "IBEX 35" / "IBEX"                              → "^IBEX"
    "AEX"                                           → "^AEX"
    "BEL 20"                                        → "^BFX"
    "SMI" / "Swiss Market Index"                    → "^SSMI"
    "ATX" / "Vienna ATX"                            → "^ATX"
    "PSI 20" / "Lisbon PSI"                         → "^PSI20"
    "ISEQ"                                          → "^ISEQ"
    "OMX Stockholm 30" / "OMXS30" / "OMX"           → "^OMX"
    "OMX Helsinki" / "OMXH25"                       → "^OMXH25"
    "OMX Copenhagen" / "OMXC25"                     → "^OMXC25"
    "OBX" / "Oslo OBX"                              → "^OBX"
    "STOXX 50" / "EURO STOXX 50"                    → "^STOXX50E"
    "STOXX 600"                                     → "^STOXX"
    "WIG 20" / "WIG20"                              → "^WIG20"
    "WIG" / "WIG All"                               → "^WIG"
    "BUX" / "Budapest BUX"                          → "^BUX"
    "PX" / "Prague PX"                              → "^PX"
    "ATHEX" / "ASE General"                         → "^ATG"

  Asia:
    "Nikkei 225" / "Nikkei" / "N225"                → "^N225"
    "TOPIX"                                         → "^TPX"
    "Hang Seng" / "HSI"                             → "^HSI"
    "Hang Seng Tech" / "HSTECH"                     → "^HSTECH"
    "Shanghai Composite" / "SSE"                    → "000001.SS"
    "Shenzhen Composite"                            → "399001.SZ"
    "CSI 300"                                       → "000300.SS"
    "KOSPI" / "KOSPI 200"                           → "^KS11"
    "TWII" / "Taiwan Weighted"                      → "^TWII"
    "NIFTY 50" / "NIFTY"                            → "^NSEI"
    "SENSEX" / "BSE Sensex"                         → "^BSESN"
    "STI" / "Straits Times"                         → "^STI"
    "SET" / "Bangkok SET"                           → "^SET.BK"

  Americas (ex-US):
    "TSX" / "TSX Composite"                         → "^GSPTSE"
    "BOVESPA" / "IBOV"                              → "^BVSP"
    "MERVAL" / "S&P MERVAL"                         → "^MERV"
    "IPC" / "Mexico IPC"                            → "^MXX"

  Africa / Middle East:
    "JTOPI" / "JSE Top 40"                          → "^J200"
    "TA-35" / "Tel Aviv 35"                         → "^TA35.TA"
    "BIST 100" / "XU100"                            → "^XU100"
    "TASI" / "Saudi TASI"                           → "^TASI.SR"

  Australia / NZ:
    "ASX 200" / "ASX"                               → "^AXJO"
    "NZX 50"                                        → "^NZ50"

For ANY other index the user mentions by a recognisable name or
abbreviation, return your best guess of the Yahoo Finance ticker
(prefix "^"). It's better to attempt a guess than to refuse —
the downstream screener will report a clean error if the ticker
turns out to be wrong. Only set universe to null and add a note
when the user's query genuinely doesn't reference any index at all.

SORT METRIC MAPPING (Lithuanian + English)
- "market cap", "pagal market cap", "by mkt cap", "kapitalizacij*"  → "market_cap"
- "P/E", "PE ratio", "pe", "pagal pe"                               → "pe_ratio"
- "ROE", "return on equity"                                         → "roe"
- "EBIT margin", "ebit", "operating margin"                         → "ebit_margin"
- "net margin", "grynas pelnas"                                     → "net_margin"
- "revenue", "sales", "pajamos", "apyvarta"                         → "revenue"
- "price", "kaina"                                                  → "price"
- "dividend yield", "div yield", "dividendai"                       → "div_yield"
- "FCF yield", "free cash flow yield"                               → "fcf_yield"
- "EV/EBIT"                                                         → "ev_ebit"

DIRECTION DEFAULTS
- "top N", "biggest", "largest", "highest", "pirmas N", "didžiausi"  → desc
- "smallest", "lowest", "cheapest", "mažiausi", "pigiausi"            → asc
- For P/E and EV multiples, "best value" or "pigiausi" still → asc
- If unclear, prefer "desc".

FRAMEWORK MAPPING
- "Fisher", "15 Fisher", "Helmer", "Fisher Alternatives"    → "fisher"
- "Gravity", "Taxer", "Gravity Taxers", "choke-point"       → "gravity"
- "Overview", "Memo", "Investment Memo"                     → "overview_v2"
- "EODHD Direct", "EODHD duomenys", "data sheet", "raw data" → "eodhd_full"

EXAMPLES
Input: "trauk pirmas 10 imoniu is SP500 pagal market cap"
Output: {"action":"screen","universe":"^GSPC","sort_by":"market_cap","sort_dir":"desc","limit":10,"tickers":null,"framework_id":null,"notes":""}

Input: "Top 5 Nasdaq by P/E"
Output: {"action":"screen","universe":"^NDX","sort_by":"pe_ratio","sort_dir":"asc","limit":5,"tickers":null,"framework_id":null,"notes":""}

Input: "DAX top 20 by ROE"
Output: {"action":"screen","universe":"^GDAXI","sort_by":"roe","sort_dir":"desc","limit":20,"tickers":null,"framework_id":null,"notes":""}

Input: "10 didziausiu imoniu wig 20 indekse"
Output: {"action":"screen","universe":"^WIG20","sort_by":"market_cap","sort_dir":"desc","limit":10,"tickers":null,"framework_id":null,"notes":""}

Input: "Top 5 NIFTY companies by revenue"
Output: {"action":"screen","universe":"^NSEI","sort_by":"revenue","sort_dir":"desc","limit":5,"tickers":null,"framework_id":null,"notes":""}

Input: "biggest 15 Bovespa names"
Output: {"action":"screen","universe":"^BVSP","sort_by":"market_cap","sort_dir":"desc","limit":15,"tickers":null,"framework_id":null,"notes":""}

Input: "Fisher analysis on Apple"
Output: {"action":"report","tickers":["AAPL"],"framework_id":"fisher","universe":null,"sort_by":null,"sort_dir":null,"limit":null,"notes":""}

Input: "Compare RHM.DE, LMT, GD on Gravity"
Output: {"action":"compare","tickers":["RHM.DE","LMT","GD"],"framework_id":"gravity","universe":null,"sort_by":null,"sort_dir":null,"limit":null,"notes":""}

Input: "AAPL"
Output: {"action":"report","tickers":["AAPL"],"framework_id":null,"universe":null,"sort_by":null,"sort_dir":null,"limit":null,"notes":""}

Input: "cheapest 8 DAX names"
Output: {"action":"screen","universe":"^GDAXI","sort_by":"pe_ratio","sort_dir":"asc","limit":8,"tickers":null,"framework_id":null,"notes":"interpreted 'cheapest' as lowest P/E"}

Input: "blah blah random stuff"
Output: {"action":null,"tickers":null,"universe":null,"thematic_query":null,"sort_by":null,"sort_dir":null,"limit":null,"framework_id":null,"notes":"could not interpret query"}

Input: "Top 10 European defense companies"
Output: {"action":"screen","universe":null,"thematic_query":"Top 10 European defense companies","sort_by":null,"sort_dir":null,"limit":10,"tickers":null,"framework_id":null,"notes":""}

Input: "Run Gravity on top 15 Japanese automakers"
Output: {"action":"screen","universe":null,"thematic_query":"top 15 Japanese automakers","sort_by":null,"sort_dir":null,"limit":15,"tickers":null,"framework_id":"gravity","notes":""}

Input: "Top 20 companies in WIG index"
Output: {"action":"screen","universe":"^WIG","sort_by":"market_cap","sort_dir":"desc","limit":20,"tickers":null,"thematic_query":null,"framework_id":null,"notes":""}

Input: "didžiausios Lenkijos bankai"
Output: {"action":"screen","universe":null,"thematic_query":"didžiausios Lenkijos bankai","sort_by":null,"sort_dir":null,"limit":10,"tickers":null,"framework_id":null,"notes":""}

User query:
"""


# ── Validation ───────────────────────────────────────────────────────────────

def _validate(intent: dict) -> dict:
    """Coerce LLM output into a clean intent dict (drops invalid values)."""
    out = _empty_intent()
    if not isinstance(intent, dict):
        return out

    action = intent.get("action")
    if action in VALID_ACTIONS:
        out["action"] = action

    tickers = intent.get("tickers")
    if isinstance(tickers, list):
        cleaned = [str(t).strip().upper() for t in tickers if t and isinstance(t, str)]
        out["tickers"] = cleaned[:20] or None

    universe = intent.get("universe")
    if isinstance(universe, str) and universe.strip():
        u = universe.strip().upper()
        # Coerce common variants
        if not u.startswith("^"):
            u = "^" + u.lstrip("^")
        out["universe"] = u

    sb = intent.get("sort_by")
    if sb in VALID_SORT_BYS:
        out["sort_by"] = sb

    sd = intent.get("sort_dir")
    if sd in VALID_SORT_DIRS:
        out["sort_dir"] = sd

    lim = intent.get("limit")
    try:
        if lim is not None:
            lim_int = int(lim)
            if 1 <= lim_int <= 200:
                out["limit"] = lim_int
    except Exception:
        pass

    fid = intent.get("framework_id")
    if fid in VALID_FRAMEWORKS:
        out["framework_id"] = fid

    notes = intent.get("notes")
    if isinstance(notes, str):
        out["notes"] = notes.strip()[:240]

    tq = intent.get("thematic_query")
    if isinstance(tq, str) and tq.strip():
        out["thematic_query"] = tq.strip()[:300]

    # Sensible defaults: if it's a screen and sort_by is set but direction
    # isn't, default to desc (except for P/E / EV multiples → asc).
    if out["action"] == "screen" and out["sort_by"] and not out["sort_dir"]:
        if out["sort_by"] in ("pe_ratio", "ev_ebit"):
            out["sort_dir"] = "asc"
        else:
            out["sort_dir"] = "desc"

    # Thematic default limit
    if out["action"] == "screen" and out.get("thematic_query") and not out["limit"]:
        out["limit"] = 10

    # If it's a screen and limit isn't set, default to 10
    if out["action"] == "screen" and out["limit"] is None:
        out["limit"] = 10

    return out


# ── Main entry point ─────────────────────────────────────────────────────────

def parse_intent(query: str) -> tuple[dict, dict]:
    """
    Parse a free-text query into a structured intent dict.

    Strategy:
      1. Quick regex pre-check — single ticker? Empty? Return immediately
         with usage={}, no LLM call billed.
      2. LLM call (LLMClient) with the instruction prompt above.
      3. Validate + coerce.

    Returns:
        (intent, usage)
          intent  — validated intent dict (all None when un-parseable)
          usage   — Anthropic-style usage dict (or {} if no LLM call)
                    with input_tokens, output_tokens, cache_*_tokens.
                    Callers can plug it straight into _cost_block().
    """
    q = (query or "").strip()
    if not q:
        return _empty_intent(), {}

    # Quick pre-check: looks like a plain ticker (no spaces, has caps + maybe a dot)?
    if " " not in q and re.fullmatch(r"\^?[A-Za-z0-9\.\-=]+", q):
        out = _empty_intent()
        out["action"] = "report"
        out["tickers"] = [q.upper()]
        return out, {}

    # ── LLM path ─────────────────────────────────────────────────────────────
    try:
        from agents.llm_client import LLMClient
        llm = LLMClient()
        ready, _ = llm.check_configured()
        if not ready:
            logger.warning("[nl_intent] LLM not configured — returning empty intent")
            return _empty_intent(), {}

        full_user_prompt = _INTENT_INSTRUCTIONS + q
        raw = llm.generate_json(
            full_user_prompt,
            _SYSTEM_PROMPT,
            max_tokens=400,
        )
        usage = getattr(llm, "last_usage", {}) or {}
    except Exception as e:
        logger.warning(f"[nl_intent] LLM call failed: {e}")
        return _empty_intent(), {}

    if not isinstance(raw, dict):
        return _empty_intent(), usage
    return _validate(raw), usage
