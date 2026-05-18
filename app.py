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
            "LLM_PROVIDER", "LLM_MODEL", "ADVERSARIAL_MODE",
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
        background: #fff;
        border: 1px solid #D0DFF0;
        border-radius: 12px;
        box-shadow: 0 4px 24px rgba(27,63,110,0.10);
    }
    .login-logo {
        text-align: center;
        font-size: 42px;
        margin-bottom: 4px;
    }
    .login-title {
        text-align: center;
        color: #1B3F6E;
        font-size: 20px;
        font-weight: 700;
        margin-bottom: 2px;
    }
    .login-sub {
        text-align: center;
        color: #888;
        font-size: 13px;
        margin-bottom: 24px;
    }
    </style>

    <div class="login-wrap">
      <div class="login-logo">📊</div>
      <div class="login-title">Your Humble EquityBot</div>
      <div class="login-sub">Private research tool — please sign in</div>
    </div>
    """, unsafe_allow_html=True)

    # Center the form inputs under the card
    col = st.columns([1, 2, 1])[1]
    with col:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Username", placeholder="username")
            password = st.text_input("Password", type="password", placeholder="••••••••")
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
      .st-emotion-cache-zy6yx3 { padding: 3rem 1rem 4rem; }
      .st-emotion-cache-1yu3o6t { padding: 0.95rem 0.75rem; }

      /* On mobile the sidebar drawer must overlay every other layer
         (toolbar, page-title sticky banner, modal-style elements).
         Streamlit's own toolbar uses z-index ~999999, so push the
         sidebar far above it. */
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
    </style>
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

# ── Navigation ────────────────────────────────────────────────────────────────
pg = st.navigation([
    st.Page("pages/report_generator.py", title="Report Generator", icon="📊"),
    st.Page("pages/my_portfolio.py",     title="My Portfolio",     icon="📁"),
    st.Page("pages/model_editing.py",    title="Model Editing",    icon="⚙️"),
    st.Page("pages/app_editing.py",      title="App Editor",       icon="🛠️"),
])
pg.run()
