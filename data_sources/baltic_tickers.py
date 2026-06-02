"""
baltic_tickers.py — NASDAQ Baltic ticker search for EquityBot.

Covers NASDAQ Vilnius (.VS / Lithuania), NASDAQ Tallinn (.TL / Estonia),
NASDAQ Riga (.RG / Latvia).

Strategy:
  1. Try to load from local cache (data/baltic_tickers.json, 7-day TTL).
  2. If cache is missing/stale, fetch full symbol lists from EODHD
     /api/exchange-symbol-list for VS, TL, RG exchanges.
  3. While EODHD fetch is running (or if it fails) fall back to the
     built-in seed list (~80 major Baltic companies).

Search is by ticker code OR company name (partial, case-insensitive).
Returns EODHD-format tickers: {CODE}.VS / {CODE}.TL / {CODE}.RG
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

_HERE      = Path(__file__).parent
_CACHE     = _HERE.parent / "data" / "baltic_tickers.json"
_CACHE_TTL = 7 * 24 * 3600   # 1 week

# ── Seed list ─────────────────────────────────────────────────────────────────
# Format: [ticker_code, exchange_suffix, company_name, sector]
# exchange_suffix: "VS" | "TL" | "RG"
_SEED: List[List[str]] = [
    # ── Lithuania — NASDAQ Vilnius (.VS) ──────────────────────────────────────
    ["APG1L", "VS", "Apranga AB",                          "Consumer Cyclical"],
    ["IGN1L", "VS", "Ignitis Group AB",                    "Utilities"],
    ["LNR1L", "VS", "Linas Agro Group AB",                 "Consumer Defensive"],
    ["SAB1L", "VS", "Siauliu Bankas AB",                   "Financial Services"],
    ["GRG1L", "VS", "Grigeo AB",                           "Industrials"],
    ["KNF1L", "VS", "Klaipedos Nafta AB",                  "Energy"],
    ["EST1L", "VS", "Energijos Skirstymo Operatorius AB",  "Utilities"],
    ["AMB1L", "VS", "Amber Grid AB",                       "Utilities"],
    ["ZMP1L", "VS", "Zemaitijos Pienas AB",                "Consumer Defensive"],
    ["RST1L", "VS", "Rokiskio Suris AB",                   "Consumer Defensive"],
    ["PIK1L", "VS", "Pieno Zvaigzdes AB",                  "Consumer Defensive"],
    ["AUG1L", "VS", "Auga Group AB",                       "Consumer Defensive"],
    ["VBL1L", "VS", "Vilniaus Baldai AB",                  "Consumer Cyclical"],
    ["TEO1L", "VS", "Telia Lietuva AB",                    "Communication Services"],
    ["UTR1L", "VS", "Utenos Trikotazas AB",                "Consumer Cyclical"],
    ["VLP1L", "VS", "Vilniaus Prekyba AB",                 "Consumer Defensive"],
    ["ACG1L", "VS", "Acola Group AB",                      "Industrials"],
    ["SRS1L", "VS", "Siauliu Bankas subordinated bonds",   "Financial Services"],
    ["LTS1L", "VS", "Lietuvos Tiekimas AB",                "Industrials"],
    ["TLT1L", "VS", "Tele2 Lietuva AB",                   "Communication Services"],
    ["CAC1L", "VS", "Cactus Capital AB",                   "Financial Services"],
    # ── Estonia — NASDAQ Tallinn (.TL) ───────────────────────────────────────
    ["TAL1T", "TL", "Tallink Grupp AS",                    "Industrials"],
    ["HAE1T", "TL", "Harju Elekter AS",                    "Industrials"],
    ["MRK1T", "TL", "Merko Ehitus AS",                     "Industrials"],
    ["LHV1T", "TL", "LHV Group AS",                        "Financial Services"],
    ["NCN1T", "TL", "Nordecon AS",                         "Industrials"],
    ["ARV1T", "TL", "Arco Vara AS",                        "Real Estate"],
    ["EFT1T", "TL", "EfTEN Real Estate Fund AS",           "Real Estate"],
    ["OEG1T", "TL", "Enefit Green AS",                     "Utilities"],
    ["ACG1T", "TL", "Acola Group AS",                      "Industrials"],
    ["SKN1T", "TL", "Skano Group AS",                      "Industrials"],
    ["PRK1T", "TL", "Pro Kapital Grupp AS",                "Real Estate"],
    ["TVA1T", "TL", "Trigon Property Development AS",      "Real Estate"],
    ["EEG1T", "TL", "Eesti Energia AS",                    "Utilities"],
    ["TVEAT", "TL", "AS Tallinna Vesi",                    "Utilities"],
    ["EPTV",  "TL", "Elering AS",                          "Utilities"],
    # ── Latvia — NASDAQ Riga (.RG) ────────────────────────────────────────────
    ["GZE1R", "RG", "Latvenergo AS",                       "Utilities"],
    ["SAF1R", "RG", "SAF Tehnika AS",                      "Technology"],
    ["GRD1R", "RG", "Grindex AS",                          "Healthcare"],
    ["LES1R", "RG", "Latvijas Elektriskie Tikli AS",       "Utilities"],
    ["BAL1R", "RG", "Baltika AS",                          "Consumer Cyclical"],
    ["RJR1R", "RG", "Rietumu Banka AS",                    "Financial Services"],
    ["ACO1R", "RG", "Acola Group AS",                      "Industrials"],
    ["DPK1R", "RG", "Daugavpils Lokomotivju Remonta Rupnica AS", "Industrials"],
    ["LFI1R", "RG", "Latvijas Finieris AS",                "Industrials"],
]

_EXCH_FLAGS = {"VS": "🇱🇹", "TL": "🇪🇪", "RG": "🇱🇻"}
_EXCH_NAMES = {"VS": "NASDAQ Vilnius", "TL": "NASDAQ Tallinn", "RG": "NASDAQ Riga"}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache() -> Optional[List[dict]]:
    try:
        if not _CACHE.exists():
            return None
        if time.time() - _CACHE.stat().st_mtime > _CACHE_TTL:
            return None
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) > 10:
            return data
    except Exception:
        pass
    return None


def _save_cache(records: List[dict]) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("[Baltic] Cache write failed: %s", e)


def _seed_records() -> List[dict]:
    return [
        {"code": c, "exchange": e, "name": n, "sector": s}
        for c, e, n, s in _SEED
    ]


def _fetch_from_eodhd(api_key: str) -> List[dict]:
    """Fetch full symbol lists from EODHD for VS, TL, RG exchanges."""
    import urllib.request, urllib.error
    records: List[dict] = []
    for exch in ("VS", "TL", "RG"):
        url = (f"https://eodhd.com/api/exchange-symbol-list/{exch}"
               f"?api_token={api_key}&fmt=json&type=common_stock")
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            for item in (data if isinstance(data, list) else []):
                code = (item.get("Code") or "").strip()
                name = (item.get("Name") or "").strip()
                sector = (item.get("Sector") or item.get("Industry") or "").strip()
                if code and name:
                    records.append({"code": code, "exchange": exch,
                                    "name": name, "sector": sector})
        except Exception as e:
            logger.debug("[Baltic] EODHD fetch failed for %s: %s", exch, e)
    return records


def _get_records(api_key: str = "") -> List[dict]:
    cached = _load_cache()
    if cached:
        return cached
    records = _seed_records()
    if api_key:
        try:
            fetched = _fetch_from_eodhd(api_key)
            if len(fetched) > len(_SEED):
                records = fetched
                _save_cache(records)
                logger.info("[Baltic] Fetched %d Baltic tickers from EODHD", len(records))
        except Exception as e:
            logger.debug("[Baltic] EODHD fetch error: %s", e)
    return records


# ── Public search API ─────────────────────────────────────────────────────────

def search_baltic(
    query: str,
    api_key: str = "",
    max_results: int = 8,
) -> List[Tuple[str, str]]:
    """
    Search Baltic tickers by name or code (partial, case-insensitive).

    Returns list of (display_label, eodhd_ticker) tuples.
    E.g.: ("🇱🇹 APG1L.VS · NASDAQ Vilnius · Apranga AB", "APG1L.VS")
    """
    q = query.strip().upper()
    if not q or len(q) < 2:
        return []

    records = _get_records(api_key)
    results: List[Tuple[str, str]] = []
    seen: set = set()

    q_lower = query.strip().lower()

    for rec in records:
        code     = rec.get("code", "")
        exchange = rec.get("exchange", "")
        name     = rec.get("name", "") or ""
        sector   = rec.get("sector", "") or ""
        ticker   = f"{code}.{exchange}"

        if ticker in seen:
            continue

        # Match: ticker code starts with query, OR name contains query
        if (code.upper().startswith(q) or
                q_lower in name.lower() or
                q_lower in (sector.lower())):
            flag = _EXCH_FLAGS.get(exchange, "🌍")
            exch_name = _EXCH_NAMES.get(exchange, exchange)
            label = f"{flag} {ticker}  ·  {exch_name}  ·  {name}"
            results.append((label, ticker))
            seen.add(ticker)
            if len(results) >= max_results:
                break

    return results
