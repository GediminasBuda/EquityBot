"""
japan_tickers.py — Japanese TSE/JPX ticker search for EquityBot.

Strategy:
  1. Try to load from local cache (data/japan_tickers.json, 7-day TTL).
  2. If cache is missing/stale, fetch full TSE symbol list from EODHD
     /api/exchange-symbol-list endpoint — returns ~3 800 active listings.
  3. While EODHD fetch is running (or if it fails) fall back to the
     built-in seed list (~220 major Japanese companies).

Search is by ticker code (numeric part) OR company name (partial, case-insensitive).
Returns Yahoo Finance format tickers: {CODE}.T  e.g. "7203.T" for Toyota.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

_HERE      = Path(__file__).parent
_CACHE     = _HERE.parent / "data" / "japan_tickers.json"
_CACHE_TTL = 7 * 24 * 3600        # 1 week in seconds

# ── Seed list — top ~220 Japanese companies (instant fallback) ─────────────────
# Format: [code_without_suffix, name, sector]
_SEED: List[List[str]] = [
    # ── Automotive ──────────────────────────────────────────────────────────────
    ["7203", "Toyota Motor Corporation",          "Consumer Cyclical"],
    ["7267", "Honda Motor Co Ltd",                "Consumer Cyclical"],
    ["7201", "Nissan Motor Co Ltd",               "Consumer Cyclical"],
    ["7261", "Mazda Motor Corporation",           "Consumer Cyclical"],
    ["7270", "Subaru Corporation",                "Consumer Cyclical"],
    ["7269", "Suzuki Motor Corporation",          "Consumer Cyclical"],
    ["7272", "Yamaha Motor Co Ltd",               "Consumer Cyclical"],
    ["7202", "Isuzu Motors Limited",              "Consumer Cyclical"],
    ["7211", "Mitsubishi Motors Corporation",     "Consumer Cyclical"],
    ["6902", "Denso Corporation",                 "Consumer Cyclical"],
    ["7259", "Aisin Corporation",                 "Consumer Cyclical"],
    ["5108", "Bridgestone Corporation",           "Consumer Cyclical"],
    ["6201", "Toyota Industries Corporation",     "Industrials"],
    # ── Electronics / Technology ─────────────────────────────────────────────
    ["6758", "Sony Group Corporation",            "Technology"],
    ["6861", "Keyence Corporation",               "Technology"],
    ["8035", "Tokyo Electron Limited",            "Technology"],
    ["6981", "Murata Manufacturing Co Ltd",       "Technology"],
    ["6954", "FANUC Corporation",                 "Industrials"],
    ["6594", "Nidec Corporation",                 "Industrials"],
    ["6752", "Panasonic Holdings Corporation",    "Technology"],
    ["6501", "Hitachi Ltd",                       "Industrials"],
    ["6702", "Fujitsu Limited",                   "Technology"],
    ["6701", "NEC Corporation",                   "Technology"],
    ["6753", "Sharp Corporation",                 "Technology"],
    ["6963", "ROHM Co Ltd",                       "Technology"],
    ["6273", "SMC Corporation",                   "Industrials"],
    ["6645", "Omron Corporation",                 "Technology"],
    ["6806", "Hirose Electric Co Ltd",            "Technology"],
    ["7741", "Hoya Corporation",                  "Technology"],
    ["6367", "Daikin Industries Ltd",             "Industrials"],
    ["7751", "Canon Inc",                         "Technology"],
    ["7752", "Ricoh Company Ltd",                 "Technology"],
    ["6448", "Brother Industries Ltd",            "Technology"],
    ["6971", "Kyocera Corporation",               "Technology"],
    ["4543", "Terumo Corporation",                "Healthcare"],
    ["7733", "Olympus Corporation",               "Healthcare"],
    ["6762", "TDK Corporation",                   "Technology"],
    ["6479", "Minebea Mitsumi Inc",               "Technology"],
    ["6504", "Fuji Electric Co Ltd",              "Industrials"],
    ["6508", "Meidensha Corporation",             "Industrials"],
    ["6674", "GS Yuasa Corporation",              "Industrials"],
    ["6723", "Renesas Electronics Corporation",   "Technology"],
    ["6770", "Alps Alpine Co Ltd",                "Technology"],
    ["8053", "Sumitomo Corporation",              "Industrials"],
    ["4901", "Fujifilm Holdings Corporation",     "Technology"],
    # ── Semiconductors / Equipment ───────────────────────────────────────────
    ["4063", "Shin-Etsu Chemical Co Ltd",         "Basic Materials"],
    ["8031", "Mitsui & Co Ltd",                   "Industrials"],
    ["3659", "Nexon Co Ltd",                      "Communication Services"],
    # ── Pharma / Healthcare ──────────────────────────────────────────────────
    ["4502", "Takeda Pharmaceutical Company Ltd", "Healthcare"],
    ["4503", "Astellas Pharma Inc",               "Healthcare"],
    ["4568", "Daiichi Sankyo Company Ltd",        "Healthcare"],
    ["4523", "Eisai Co Ltd",                      "Healthcare"],
    ["4519", "Chugai Pharmaceutical Co Ltd",      "Healthcare"],
    ["4507", "Shionogi & Co Ltd",                 "Healthcare"],
    ["4506", "Sumitomo Pharma Co Ltd",            "Healthcare"],
    ["4528", "Ono Pharmaceutical Co Ltd",         "Healthcare"],
    ["4578", "Otsuka Holdings Co Ltd",            "Healthcare"],
    ["4151", "Kyowa Kirin Co Ltd",                "Healthcare"],
    ["4452", "Kao Corporation",                   "Consumer Defensive"],
    ["4911", "Shiseido Company Ltd",              "Consumer Defensive"],
    ["4536", "Santen Pharmaceutical Co Ltd",      "Healthcare"],
    ["4540", "Tsumura & Co",                      "Healthcare"],
    ["4541", "Nichi-Iko Pharmaceutical Co Ltd",   "Healthcare"],
    ["4547", "Kissei Pharmaceutical Co Ltd",      "Healthcare"],
    ["4555", "Sawai Group Holdings Co Ltd",       "Healthcare"],
    ["4563", "AnGes Inc",                         "Healthcare"],
    # ── Financial ─────────────────────────────────────────────────────────────
    ["8306", "Mitsubishi UFJ Financial Group Inc","Financial Services"],
    ["8316", "Sumitomo Mitsui Financial Group Inc","Financial Services"],
    ["8411", "Mizuho Financial Group Inc",        "Financial Services"],
    ["8604", "Nomura Holdings Inc",               "Financial Services"],
    ["8601", "Daiwa Securities Group Inc",        "Financial Services"],
    ["8591", "Orix Corporation",                  "Financial Services"],
    ["8766", "Tokio Marine Holdings Inc",         "Financial Services"],
    ["8725", "MS&AD Insurance Group Holdings Inc","Financial Services"],
    ["8795", "T&D Holdings Inc",                  "Financial Services"],
    ["8655", "Sompo Holdings Inc",                "Financial Services"],
    ["8697", "Japan Exchange Group Inc",          "Financial Services"],
    ["6178", "Japan Post Holdings Co Ltd",        "Financial Services"],
    ["8308", "Resona Holdings Inc",               "Financial Services"],
    ["8309", "Sumitomo Mitsui Trust Holdings Inc","Financial Services"],
    ["8750", "Dai-ichi Life Insurance Company Ltd","Financial Services"],
    ["8253", "Credit Saison Co Ltd",              "Financial Services"],
    ["8334", "Gunma Bank Ltd",                    "Financial Services"],
    ["8355", "Shizuoka Financial Group Inc",      "Financial Services"],
    ["8359", "Hachijuni Bank Ltd",                "Financial Services"],
    ["8418", "Hyakujushi Bank Ltd",               "Financial Services"],
    ["8473", "SBI Holdings Inc",                  "Financial Services"],
    ["8002", "Marubeni Corporation",              "Industrials"],
    ["8001", "Itochu Corporation",                "Industrials"],
    ["8058", "Mitsubishi Corporation",            "Industrials"],
    ["8053", "Sumitomo Corporation",              "Industrials"],
    # ── Telecom ──────────────────────────────────────────────────────────────
    ["9432", "Nippon Telegraph and Telephone Corp","Communication Services"],
    ["9433", "KDDI Corporation",                  "Communication Services"],
    ["9434", "SoftBank Corp",                     "Communication Services"],
    ["9984", "SoftBank Group Corp",               "Technology"],
    ["9613", "NTT Data Group Corporation",        "Technology"],
    # ── Retail / Consumer ─────────────────────────────────────────────────────
    ["9983", "Fast Retailing Co Ltd",             "Consumer Cyclical"],
    ["7974", "Nintendo Co Ltd",                   "Communication Services"],
    ["3382", "Seven & I Holdings Co Ltd",         "Consumer Defensive"],
    ["8267", "AEON Co Ltd",                       "Consumer Defensive"],
    ["2503", "Kirin Holdings Company Ltd",        "Consumer Defensive"],
    ["2502", "Asahi Group Holdings Ltd",          "Consumer Defensive"],
    ["2587", "Suntory Beverage & Food Limited",   "Consumer Defensive"],
    ["2802", "Ajinomoto Co Inc",                  "Consumer Defensive"],
    ["2897", "Nissin Foods Holdings Co Ltd",      "Consumer Defensive"],
    ["2269", "Meiji Holdings Co Ltd",             "Consumer Defensive"],
    ["6098", "Recruit Holdings Co Ltd",           "Industrials"],
    ["3086", "J Front Retailing Co Ltd",          "Consumer Cyclical"],
    ["3099", "Isetan Mitsukoshi Holdings Ltd",    "Consumer Cyclical"],
    ["3197", "Skylark Holdings Co Ltd",           "Consumer Cyclical"],
    ["3288", "Open House Group Co Ltd",           "Real Estate"],
    ["2651", "Lawson Inc",                        "Consumer Defensive"],
    ["9843", "Nitori Holdings Co Ltd",            "Consumer Cyclical"],
    ["3291", "Iida Group Holdings Co Ltd",        "Real Estate"],
    # ── Materials / Chemicals ────────────────────────────────────────────────
    ["3407", "Asahi Kasei Corporation",           "Basic Materials"],
    ["3402", "Toray Industries Inc",              "Basic Materials"],
    ["3401", "Teijin Limited",                    "Basic Materials"],
    ["4612", "Nippon Paint Holdings Co Ltd",      "Basic Materials"],
    ["4613", "Kansai Paint Co Ltd",               "Basic Materials"],
    ["5401", "Nippon Steel Corporation",          "Basic Materials"],
    ["5411", "JFE Holdings Inc",                  "Basic Materials"],
    ["5406", "Kobe Steel Ltd",                    "Basic Materials"],
    ["5713", "Sumitomo Metal Mining Co Ltd",      "Basic Materials"],
    ["5711", "Mitsubishi Materials Corporation",  "Basic Materials"],
    ["5803", "Furukawa Electric Co Ltd",          "Basic Materials"],
    ["5802", "Sumitomo Electric Industries Ltd",  "Industrials"],
    ["5019", "Idemitsu Kosan Co Ltd",             "Energy"],
    ["5020", "ENEOS Holdings Inc",                "Energy"],
    ["4004", "Resonac Holdings Corporation",      "Basic Materials"],
    ["4005", "Sumitomo Chemical Company Ltd",     "Basic Materials"],
    ["4041", "Nippon Soda Co Ltd",                "Basic Materials"],
    ["4042", "Tosoh Corporation",                 "Basic Materials"],
    ["4043", "Tokuyama Corporation",              "Basic Materials"],
    ["4061", "Denka Company Limited",             "Basic Materials"],
    ["4183", "Mitsui Chemicals Inc",              "Basic Materials"],
    ["4188", "Mitsubishi Chemical Group Corporation","Basic Materials"],
    ["4208", "UBE Industries Ltd",                "Basic Materials"],
    ["4631", "DIC Corporation",                   "Basic Materials"],
    # ── Heavy Industry / Engineering ─────────────────────────────────────────
    ["6301", "Komatsu Ltd",                       "Industrials"],
    ["6326", "Kubota Corporation",                "Industrials"],
    ["6503", "Mitsubishi Electric Corporation",   "Industrials"],
    ["7012", "Kawasaki Heavy Industries Ltd",     "Industrials"],
    ["7011", "Mitsubishi Heavy Industries Ltd",   "Industrials"],
    ["7013", "IHI Corporation",                   "Industrials"],
    ["7003", "Mitsui Engineering & Shipbuilding", "Industrials"],
    ["1963", "JGC Holdings Corporation",          "Industrials"],
    # ── Real Estate ──────────────────────────────────────────────────────────
    ["8801", "Mitsui Fudosan Co Ltd",             "Real Estate"],
    ["8802", "Mitsubishi Estate Co Ltd",          "Real Estate"],
    ["8830", "Sumitomo Realty & Development Co Ltd","Real Estate"],
    ["1925", "Daiwa House Industry Co Ltd",       "Real Estate"],
    ["1928", "Sekisui House Ltd",                 "Real Estate"],
    ["3003", "Hulic Co Ltd",                      "Real Estate"],
    ["3289", "Tokyu Fudosan Holdings Corporation","Real Estate"],
    # ── Utilities ─────────────────────────────────────────────────────────────
    ["9501", "Tokyo Electric Power Company Holdings","Utilities"],
    ["9502", "Chubu Electric Power Co Inc",       "Utilities"],
    ["9503", "Kansai Electric Power Co Inc",      "Utilities"],
    ["9531", "Tokyo Gas Co Ltd",                  "Utilities"],
    ["9532", "Osaka Gas Co Ltd",                  "Utilities"],
    # ── Transportation / Logistics ──────────────────────────────────────────
    ["9020", "East Japan Railway Company",        "Industrials"],
    ["9022", "Central Japan Railway Company",     "Industrials"],
    ["9021", "West Japan Railway Company",        "Industrials"],
    ["9064", "Yamato Holdings Co Ltd",            "Industrials"],
    ["9143", "SG Holdings Co Ltd",                "Industrials"],
    ["9101", "Nippon Yusen Kabushiki Kaisha",     "Industrials"],
    ["9104", "Mitsui OSK Lines Ltd",              "Industrials"],
    ["9107", "Kawasaki Kisen Kaisha Ltd",         "Industrials"],
    # ── Construction ─────────────────────────────────────────────────────────
    ["1802", "Obayashi Corporation",              "Industrials"],
    ["1803", "Shimizu Corporation",               "Industrials"],
    ["1812", "Kajima Corporation",                "Industrials"],
    ["1821", "Sumitomo Mitsui Construction Co Ltd","Industrials"],
    ["1824", "Maeda Corporation",                 "Industrials"],
    # ── Internet / Software ──────────────────────────────────────────────────
    ["4689", "LY Corporation",                    "Communication Services"],
    ["4755", "Rakuten Group Inc",                 "Consumer Cyclical"],
    ["4385", "Mercari Inc",                       "Consumer Cyclical"],
    ["3668", "COLOPL Inc",                        "Communication Services"],
    ["2432", "DeNA Co Ltd",                       "Communication Services"],
    ["3765", "GungHo Online Entertainment Inc",   "Communication Services"],
    ["4307", "Nomura Research Institute Ltd",     "Technology"],
    ["9719", "SCSK Corporation",                  "Technology"],
    ["4722", "Future Corporation",                "Technology"],
    # ── Food & Beverages (additional) ───────────────────────────────────────
    ["2871", "Nichirei Corporation",              "Consumer Defensive"],
    ["2282", "NH Foods Ltd",                      "Consumer Defensive"],
    ["2002", "Nisshin Seifun Group Inc",          "Consumer Defensive"],
    ["2201", "Morinaga & Co Ltd",                 "Consumer Defensive"],
    ["2212", "Yamazaki Baking Co Ltd",            "Consumer Defensive"],
    ["2801", "Kikkoman Corporation",              "Consumer Defensive"],
    ["2871", "Nichirei Corporation",              "Consumer Defensive"],
    # ── Mining & Resources ───────────────────────────────────────────────────
    ["1605", "Inpex Corporation",                 "Energy"],
    ["3405", "Kuraray Co Ltd",                    "Basic Materials"],
]


def _parse_eodhd_item(item: dict) -> Optional[dict]:
    """Extract code + name + type from an EODHD exchange-symbol-list record."""
    code = (item.get("Code") or item.get("code") or "").strip()
    name = (item.get("Name") or item.get("name") or "").strip()
    itype = (item.get("Type") or item.get("type") or "").strip().lower()
    if not code or not name:
        return None
    # Keep only common stocks (exclude ETFs, warrants, preferred, etc.)
    if itype and itype not in ("common stock", "stock", "equity", ""):
        return None
    return {"code": code, "name": name, "sector": ""}


def _fetch_from_eodhd(api_key: str) -> List[dict]:
    """Fetch full TSE symbol list from EODHD. Returns list of {code, name, sector}."""
    try:
        import requests as _r
    except ImportError:
        return []

    base = "https://eodhistoricaldata.com/api/exchange-symbol-list"
    for exch in ("T", "TSE", "JPX"):
        try:
            resp = _r.get(
                f"{base}/{exch}",
                params={"api_token": api_key, "fmt": "json"},
                timeout=60,
            )
            if resp.status_code == 200:
                raw = resp.json()
                if isinstance(raw, list) and raw:
                    out = []
                    for item in raw:
                        parsed = _parse_eodhd_item(item)
                        if parsed:
                            out.append(parsed)
                    logger.info("Fetched %d Japan tickers from EODHD (exchange=%s)",
                                len(out), exch)
                    return out
        except Exception as e:
            logger.warning("EODHD Japan fetch failed (exchange=%s): %s", exch, e)
    return []


def _load_cache() -> Optional[List[dict]]:
    if not _CACHE.exists():
        return None
    try:
        age = time.time() - _CACHE.stat().st_mtime
        if age > _CACHE_TTL:
            return None
        with open(_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) and data else None
    except Exception:
        return None


def _save_cache(data: List[dict]) -> None:
    _CACHE.parent.mkdir(exist_ok=True)
    try:
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        logger.warning("Japan tickers cache save failed: %s", e)


def _seed_list() -> List[dict]:
    """Return the built-in seed list as {code, name, sector} dicts."""
    return [{"code": c, "name": n, "sector": s} for c, n, s in _SEED]


# ── Public API ─────────────────────────────────────────────────────────────────

def get_japan_tickers(api_key: str = "") -> List[dict]:
    """
    Return the full list of TSE-listed companies as [{code, name, sector}].
    Uses a 7-day local cache. Falls back to seed list if EODHD unavailable.
    """
    cached = _load_cache()
    if cached:
        return cached

    if api_key:
        fetched = _fetch_from_eodhd(api_key)
        if fetched:
            _save_cache(fetched)
            return fetched

    return _seed_list()


def search_japan(
    query: str,
    api_key: str = "",
    max_results: int = 5,
) -> List[tuple[str, str]]:
    """
    Search TSE tickers by ticker code or company name.

    Args:
        query:       User search query (partial ticker or name).
        api_key:     EODHD API key for cache population.
        max_results: Max suggestions to return.

    Returns:
        List of (display_label, yf_ticker) tuples.
        yf_ticker is in Yahoo Finance format: "7203.T"
    """
    q = (query or "").strip().lower()
    if not q or len(q) < 2:
        return []

    tickers = get_japan_tickers(api_key)
    results: List[tuple[str, str]] = []

    for item in tickers:
        code = item.get("code", "").strip()
        name = item.get("name", "").strip()
        if not code:
            continue
        if q in code.lower() or q in name.lower():
            yf_ticker = f"{code}.T"
            sector = item.get("sector", "")
            label = f"{yf_ticker:<10}  {name[:50]}"
            if sector:
                label += f"  ({sector})"
            label += "  · TSE"
            results.append((label, yf_ticker))
        if len(results) >= max_results:
            break

    return results


def refresh_cache(api_key: str) -> int:
    """Force-refresh the Japan tickers cache from EODHD. Returns count fetched."""
    fetched = _fetch_from_eodhd(api_key)
    if fetched:
        _save_cache(fetched)
        return len(fetched)
    return 0
