"""
app.py — Your Humble EquityBot — Navigation router.

Local:
    streamlit run app.py

Cloud deployment:
    1. Push repo to GitHub
    2. Connect at share.streamlit.io
    3. Add API keys + auth credentials in the Streamlit Secrets manager

Authentication:
    Add to Streamlit Secrets (or .streamlit/secrets.toml locally):

        [users]
        alice = "sha256_hex_of_password"
        bob   = "sha256_hex_of_password"

    Generate a hash in Python:
        import hashlib
        print(hashlib.sha256("your_password".encode()).hexdigest())

    If no [users] section is present, auth is skipped (dev mode).
"""
from __future__ import annotations
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import streamlit as st

# ── Cloud secret injection ────────────────────────────────────────────────────
# Must happen before any local module imports (which trigger config.py).
def _inject_cloud_secrets() -> None:
    """Copy Streamlit secrets → os.environ so config.py picks them up.

    Uses unconditional override so secrets always win over any .env file
    or pre-existing env vars — critical when rotating keys or switching
    LLM_PROVIDER/LLM_MODEL without redeploying.
    """
    try:
        secret_keys = [
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "ALPHA_VANTAGE_API_KEY", "FRED_API_KEY",
            "FMP_API_KEY", "EODHD_API_KEY",
            "NEWS_API_KEY", "SIMFIN_API_KEY",
            "LLM_PROVIDER", "LLM_MODEL", "ADVERSARIAL_MODE",
            "FINRA_CLIENT_ID", "FINRA_CLIENT_SECRET",
            "GITHUB_GIST_TOKEN",
        ]
        for k in secret_keys:
            if k in st.secrets:
                os.environ[k] = str(st.secrets[k])   # always override
    except Exception:
        pass  # Running locally — .env handles it

_inject_cloud_secrets()
sys.path.insert(0, str(Path(__file__).parent))


# ── Authentication ────────────────────────────────────────────────────────────

def _load_users() -> dict[str, str]:
    """
    Return {username: password_sha256} from st.secrets["users"].
    Returns empty dict if no [users] section exists (dev/local mode — no gate).
    """
    try:
        return dict(st.secrets["users"])
    except Exception:
        return {}


def _check_password(username: str, password: str, users: dict[str, str]) -> bool:
    given_hash = hashlib.sha256(password.encode()).hexdigest()
    stored     = users.get(username.strip().lower(), "")
    return given_hash == stored and stored != ""


# ── File-backed session persistence ─────────────────────────────────────────
# Streamlit's st.session_state is tied to the browser's WebSocket session,
# so a plain F5 wipes it and the user has to log in again. Two earlier
# attempts — a browser cookie via streamlit-cookies-controller and a
# signed token in st.query_params — both failed in deployment: the
# cookie component mounts asynchronously after st.stop() has already
# short-circuited to the login screen, and st.query_params writes don't
# always survive a Streamlit multipage rerun.
#
# Final approach: persist the most recent successful sign-in to a
# server-side JSON file at data/last_auth.json. On every page load we
# read that file first; if it carries a non-expired username we mark
# the session authenticated. The file is only written after the
# password check passes and is removed on Sign out. Because this is a
# single-user private deployment (only the owner has the URL), trading
# the cookie/token complexity for a flat file is acceptable.
_AUTH_TOKEN_TTL_S    = 30 * 24 * 3600   # 30 days

_DATA_DIR_AUTH = Path(__file__).resolve().parent / "data"
_DATA_DIR_AUTH.mkdir(exist_ok=True)
_LAST_AUTH_FILE = _DATA_DIR_AUTH / "last_auth.json"


def _persist_session_file(username: str, ttl_s: int = _AUTH_TOKEN_TTL_S) -> None:
    """Save the successful sign-in to the on-disk auth file."""
    try:
        payload = {
            "username":   username,
            "expires_at": int(time.time()) + ttl_s,
        }
        _LAST_AUTH_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def _try_restore_session_from_file() -> None:
    """If the on-disk auth file is fresh, mark session authenticated."""
    if st.session_state.get("authenticated"):
        return
    if not _LAST_AUTH_FILE.exists():
        return
    try:
        data = json.loads(_LAST_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    expires_at = data.get("expires_at", 0)
    username   = data.get("username")
    if not username:
        return
    if time.time() > expires_at:
        # Expired — clean up so we don't keep reading it.
        try:
            _LAST_AUTH_FILE.unlink()
        except Exception:
            pass
        return
    # Validate that the recorded username is still in the user table —
    # avoids resurrecting a session for a user that was removed from
    # the secrets [users] block.
    if username not in _load_users():
        try:
            _LAST_AUTH_FILE.unlink()
        except Exception:
            pass
        return
    st.session_state["authenticated"] = True
    st.session_state["username"]      = username


def _clear_session_file() -> None:
    try:
        if _LAST_AUTH_FILE.exists():
            _LAST_AUTH_FILE.unlink()
    except Exception:
        pass


def _show_login() -> None:
    """Render a centered login form. Sets st.session_state.authenticated on success."""
    st.markdown("""
    <style>
    .login-wrap {
        max-width: 380px;
        margin: 80px auto 0 auto;
        padding: 36px 40px 32px;
        background: #000000;
        border: 1px solid #FFA028;
        border-radius: 4px;
        box-shadow: 0 0 24px rgba(255,160,40,0.18);
    }
    .login-logo {
        text-align: center;
        font-size: 42px;
        margin-bottom: 4px;
        color: #FFA028;
    }
    .login-title {
        text-align: center;
        color: #FFA028;
        font-family: monospace;
        font-size: 18px;
        font-weight: 700;
        letter-spacing: 1.5px;
        text-transform: uppercase;
        margin-bottom: 2px;
    }
    .login-sub {
        text-align: center;
        color: #C97A1E;
        font-family: monospace;
        font-size: 11px;
        letter-spacing: 0.5px;
        margin-bottom: 24px;
    }
    </style>

    <div class="login-wrap">
      <div class="login-logo">📊</div>
      <div class="login-title">EquityBot Terminal</div>
      <div class="login-sub">Private research tool · sign in</div>
    </div>
    """, unsafe_allow_html=True)

    # Center the form inputs under the card
    col = st.columns([1, 2, 1])[1]
    with col:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", use_container_width=True)

        if submitted:
            users = _load_users()
            if _check_password(username, password, users):
                uname = username.strip().lower()
                st.session_state["authenticated"] = True
                st.session_state["username"]      = uname
                # Persist the sign-in to disk so a plain F5 (which
                # resets st.session_state) can restore the session
                # for up to _AUTH_TOKEN_TTL_S seconds.
                _persist_session_file(uname)
                st.rerun()
            else:
                st.error("Incorrect username or password.", icon="🔒")


def _logout_button() -> None:
    """Small logout button shown in the sidebar."""
    with st.sidebar:
        st.markdown("---")
        user = st.session_state.get("username", "")
        st.caption(f"Signed in as **{user}**")
        if st.button("Sign out", use_container_width=True):
            st.session_state["authenticated"] = False
            st.session_state["username"]      = ""
            _clear_session_file()
            st.rerun()


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Your Humble EquityBot",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS overrides ──────────────────────────────────────────────────────
# NB. .st-emotion-cache-zy6yx3 is a Streamlit-generated hash class — it can
# change when Streamlit is upgraded. Re-inspect in DevTools and update the
# selector if the padding stops applying after a version bump.
st.markdown(
    """
    <style>
      /* ── Existing layout overrides (unchanged) ─────────────────── */
      .st-emotion-cache-zy6yx3 { padding: 3rem 1rem 4rem; }
      .st-emotion-cache-1yu3o6t { padding: 0.95rem 0.75rem; }

      @media (max-width: 768px) {
        .stSidebar { z-index: 999999999999 !important; }
        .st-emotion-cache-1yu3o6t { padding: 0rem 0.75rem; }
      }

      @media (max-width: 640px) {
        .st-emotion-cache-hua6f6 {
          min-width: calc(50% - 1.5rem);
          margin: 0px 5px 0px 5px;
          height: 65px;
        }
      }

      /* ── Bloomberg-terminal global tone ───────────────────────────
         Streamlit's theme handles black bg + amber text by default,
         but a few elements need explicit overrides to read as a
         terminal: buttons, inputs, dividers, metrics, captions. */
      body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
      [data-testid="stSidebar"], [data-testid="stHeader"] {
        background-color: #000000 !important;
        color: #FFA028 !important;
        font-family: monospace !important;
      }
      [data-testid="stSidebar"] {
        background-color: #050505 !important;
        border-right: 1px solid #1a1208;
      }
      h1, h2, h3, h4, h5, h6 {
        color: #FFA028 !important;
        font-family: monospace !important;
        letter-spacing: 0.5px;
      }
      a, a:visited { color: #FFA028 !important; }
      a:hover { color: #FFD89C !important; }

      hr, .stDivider, [data-testid="stDivider"] {
        border-color: #2a1f10 !important;
        background-color: #2a1f10 !important;
      }

      /* Buttons: bordered amber on idle, filled amber on primary */
      div[data-testid="stButton"] > button,
      div[data-testid="stDownloadButton"] > button,
      div[data-testid="stFormSubmitButton"] > button {
        background: #000000 !important;
        color: #FFA028 !important;
        border: 1px solid #FFA028 !important;
        border-radius: 2px !important;
        font-family: monospace !important;
        letter-spacing: 0.3px !important;
        text-transform: uppercase;
      }
      div[data-testid="stButton"] > button:hover,
      div[data-testid="stDownloadButton"] > button:hover,
      div[data-testid="stFormSubmitButton"] > button:hover {
        background: #1a1208 !important;
        border-color: #FFD89C !important;
        color: #FFD89C !important;
      }
      div[data-testid="stButton"] > button[kind="primary"],
      div[data-testid="stFormSubmitButton"] > button[kind="primary"] {
        background: #FFA028 !important;
        color: #000000 !important;
      }
      div[data-testid="stButton"] > button[kind="primary"]:hover,
      div[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {
        background: #FFD89C !important;
        color: #000000 !important;
      }

      /* ── Kill Streamlit's per-corner border-radius utilities ──────
         Streamlit ships .st-au/-av/-aw/-ax helpers that set each
         corner of an element to 0.5rem; that rounding is what was
         softening the input rims. Zero them out so the red rim
         renders as sharp 90° corners. */
      .st-au { border-top-left-radius:     0 !important; }
      .st-av { border-top-right-radius:    0 !important; }
      .st-aw { border-bottom-right-radius: 0 !important; }
      .st-ax { border-bottom-left-radius:  0 !important; }

      /* ── Inputs (red border + red typed text) ──────────────────────
         Aggressive override so every Streamlit / BaseWeb input layer
         renders with a red 1px border and red typed text. Uses the
         `border` shorthand so width/style/colour all land together;
         strips inner borders so the visible red rim doesn't double up.
         Targets every plausible DOM path: native input, Streamlit
         testid wrappers, BaseWeb wrappers, the custom searchbox. */

      /* Typed text + caret = red, monospace, no placeholder leak */
      input, textarea, select,
      [data-baseweb="input"] input,
      [data-baseweb="select"] input,
      [data-baseweb="search"] input,
      [data-baseweb="textarea"] textarea,
      [data-testid="stTextInput"] input,
      [data-testid="stTextArea"] textarea,
      [data-testid="stSelectbox"] [role="combobox"],
      [data-testid="stSearchbox"] input {
        background-color: #000000 !important;
        color: #FF3030 !important;
        font-family: monospace !important;
        caret-color: #FF3030 !important;
        -webkit-text-fill-color: #FF3030 !important;
      }
      input::placeholder, textarea::placeholder {
        color: transparent !important;
        -webkit-text-fill-color: transparent !important;
      }

      /* Visible red rim — neutralise outer wrapper first, then paint
         the canonical inner div. Both layers carry the override so
         whichever Streamlit version is running ends up red. */
      [data-baseweb="input"],
      [data-baseweb="select"],
      [data-baseweb="textarea"],
      [data-baseweb="search"] {
        border: none !important;
        background-color: transparent !important;
        box-shadow: none !important;
      }
      [data-baseweb="input"] > div,
      [data-baseweb="select"] > div,
      [data-baseweb="search"] > div,
      [data-baseweb="textarea"] > div,
      [data-testid="stTextInput"] > div > div,
      [data-testid="stTextArea"] > div > div,
      [data-testid="stSelectbox"] > div > div,
      [data-testid="stSearchbox"] > div > div {
        border: 1px solid #FF3030 !important;
        border-radius: 2px !important;
        background-color: #000000 !important;
        box-shadow: none !important;
      }
      [data-baseweb="input"] > div:focus-within,
      [data-baseweb="select"] > div:focus-within,
      [data-baseweb="search"] > div:focus-within,
      [data-baseweb="textarea"] > div:focus-within,
      [data-testid="stTextInput"] > div > div:focus-within,
      [data-testid="stTextArea"] > div > div:focus-within,
      [data-testid="stSelectbox"] > div > div:focus-within,
      [data-testid="stSearchbox"] > div > div:focus-within {
        border-color: #FF3030 !important;
        box-shadow: 0 0 0 1px #FF3030 !important;
      }

      /* Metrics */
      [data-testid="stMetricValue"] {
        color: #FFA028 !important;
        font-family: monospace !important;
      }
      [data-testid="stMetricLabel"] {
        color: #8a6a30 !important;
        font-family: monospace !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      [data-testid="stMetricDelta"] svg { display: none; }
      [data-testid="stMetricDelta"] {
        color: #4D9FFF !important;
        font-family: monospace !important;
      }

      /* Captions / small text */
      [data-testid="stCaptionContainer"], small, .stCaption {
        color: #8a6a30 !important;
        font-family: monospace !important;
      }

      /* No italic anywhere in the v2 UI — Bloomberg terminals don't
         italicise. Forces every markdown <em>/<i> and CSS italic
         declaration to render upright. */
      em, i, .stMarkdown em, .stMarkdown i,
      [data-testid="stMarkdownContainer"] em,
      [data-testid="stMarkdownContainer"] i,
      [data-testid="stCaptionContainer"] em,
      [data-testid="stCaptionContainer"] i {
        font-style: normal !important;
      }

      /* Radio / checkbox labels */
      [data-testid="stRadio"] label, [data-testid="stCheckbox"] label,
      [data-baseweb="radio"] div { color: #FFA028 !important; }

      /* Expander headers */
      [data-testid="stExpander"] summary { color: #FFA028 !important; }

      /* Code blocks */
      code, pre {
        background: #0a0a0a !important;
        color: #FFA028 !important;
        border: 1px solid #2a1f10;
      }

      /* Tables */
      table { color: #FFA028 !important; font-family: monospace !important; }
      th { background: #0e0e0e !important; color: #FFA028 !important;
           border-bottom: 1px solid #4a3818 !important; }
      td { border-bottom: 1px solid #1a1208 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Cost counter badge (fixed top-right) ─────────────────────────────────────
def _render_cost_badge() -> None:
    """Inject a fixed-position cost counter into the top-right header area."""
    try:
        from utils.cost_tracker import load as _ct_load
        data = _ct_load()
        n    = data.get("reports", 0)
        usd  = data.get("total_usd", 0.0)
        label = f"📊 {n} reports · ${usd:.2f}"
    except Exception:
        label = "📊 — reports · $—"

    st.markdown(
        f"""
        <div id="eq-cost-badge" style="
            position:fixed; top:8px; right:72px; z-index:9999;
            background:#0a0a0a; border:1px solid #2a1f10;
            border-radius:2px; padding:3px 10px;
            font-family:monospace; font-size:11px; color:#a87f30;
            white-space:nowrap; pointer-events:none;
        ">{label}</div>
        """,
        unsafe_allow_html=True,
    )


# ── Auth gate ─────────────────────────────────────────────────────────────────
_users = _load_users()
if _users:
    # Try to restore the session from the on-disk last_auth file BEFORE
    # falling through to the login form — keeps the user signed in
    # across F5 / browser-reopen up to _AUTH_TOKEN_TTL_S.
    _try_restore_session_from_file()

    # Credentials configured — enforce login
    if not st.session_state.get("authenticated"):
        _show_login()
        st.stop()
    else:
        _logout_button()
# else: no [users] in secrets → dev mode, gate bypassed

_render_cost_badge()

# ── Navigation ────────────────────────────────────────────────────────────────
pg = st.navigation([
    st.Page("pages/report_generator.py", title="Report Generator", icon="📊"),
    st.Page("pages/my_portfolio.py",     title="My Portfolio",     icon="📁"),
    st.Page("pages/screener.py",         title="Screener",         icon="🔍"),
    # ── Hidden for now (kept on disk in case we bring them back later) ──
    # st.Page("pages/model_editing.py",    title="Model Editing",    icon="⚙️"),
    # st.Page("pages/app_editing.py",      title="App Editor",       icon="🛠️"),
])
pg.run()
