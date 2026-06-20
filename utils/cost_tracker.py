"""
Persistent LLM cost counter backed by the portfolio GitHub Gist.

Stores {"reports": N, "total_usd": X.XX} as cost_tracker.json within
the existing "EquityBot Portfolio (do not delete)" Gist — no new Gist needed.

Usage:
    from utils.cost_tracker import load, increment

    data = load()          # {"reports": 12, "total_usd": 0.48}
    increment(0.034)       # adds one report + its cost, saves to Gist
"""
from __future__ import annotations
import json
import os
from typing import Optional

import requests
import streamlit as st

_GIST_DESC     = "EquityBot Portfolio (do not delete)"
_COST_FILENAME = "cost_tracker.json"
_GIST_API      = "https://api.github.com"
_TIMEOUT       = 5


def _token() -> Optional[str]:
    try:
        if "GITHUB_GIST_TOKEN" in st.secrets:
            return str(st.secrets["GITHUB_GIST_TOKEN"]) or None
    except Exception:
        pass
    return os.environ.get("GITHUB_GIST_TOKEN") or None


def _headers(tok: str) -> dict:
    return {
        "Authorization":        f"Bearer {tok}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_gist_id(tok: str) -> Optional[str]:
    """Find the portfolio Gist ID; caches result in session_state."""
    cached = st.session_state.get("_ct_gist_id")
    if cached:
        return cached
    try:
        r = requests.get(
            f"{_GIST_API}/gists",
            headers=_headers(tok),
            params={"per_page": 100},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            for g in r.json():
                if g.get("description") == _GIST_DESC:
                    st.session_state._ct_gist_id = g["id"]
                    return g["id"]
    except Exception:
        pass
    return None


def load() -> dict:
    """Return {"reports": N, "total_usd": X.XX} from the Gist, or zeros on failure."""
    cached = st.session_state.get("_ct_data")
    if cached is not None:
        return cached

    tok = _token()
    if not tok:
        result = {"reports": 0, "total_usd": 0.0}
        st.session_state._ct_data = result
        return result

    gist_id = _find_gist_id(tok)
    if not gist_id:
        result = {"reports": 0, "total_usd": 0.0}
        st.session_state._ct_data = result
        return result

    try:
        r = requests.get(
            f"{_GIST_API}/gists/{gist_id}",
            headers=_headers(tok),
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            files = r.json().get("files", {})
            if _COST_FILENAME in files:
                content = files[_COST_FILENAME].get("content", "{}")
                raw = json.loads(content)
                result = {
                    "reports":   int(float(raw.get("reports",   0))),
                    "total_usd": float(raw.get("total_usd", 0.0)),
                }
                st.session_state._ct_data = result
                return result
    except Exception:
        pass

    result = {"reports": 0, "total_usd": 0.0}
    st.session_state._ct_data = result
    return result


def increment(cost_usd: float) -> None:
    """Add one report and cost_usd to the running total, then save to Gist."""
    current = load()
    updated = {
        "reports":   current["reports"] + 1,
        "total_usd": round(current["total_usd"] + cost_usd, 6),
    }
    st.session_state._ct_data = updated  # update cache immediately for live display

    tok = _token()
    if not tok:
        return
    gist_id = _find_gist_id(tok)
    if not gist_id:
        return

    try:
        requests.patch(
            f"{_GIST_API}/gists/{gist_id}",
            headers=_headers(tok),
            json={"files": {_COST_FILENAME: {"content": json.dumps(updated)}}},
            timeout=_TIMEOUT,
        )
    except Exception:
        pass
