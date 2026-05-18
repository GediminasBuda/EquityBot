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
import hmac
import os
import sys
import time
from pathlib import Path
from typing import Optional

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


# ── Cookie-backed session persistence ────────────────────────────────────────
# Streamlit's st.session_state is tied to the browser's WebSocket session,
# so a plain F5 wipes it and the user has to log in again. To survive page
# refreshes we mint a signed (HMAC-SHA256) token, store it in a browser
# cookie, and verify it on subsequent loads. The signature prevents a user
# from forging cookies; the expiry timestamp limits how long a stolen
# cookie would remain valid.
_AUTH_COOKIE_NAME    = "eqbot_auth"
_AUTH_COOKIE_TTL_S   = 30 * 24 * 3600   # 30 days
_AUTH_TOKEN_VERSION  = "v1"             # bump to invalidate all sessions


def _session_secret() -> str:
    """
    Return the HMAC key used to sign session tokens. Prefers
    st.secrets["SESSION_SECRET"], then the SESSION_SECRET env var. Falls
    back to a key derived from existing API keys so the secret is stable
    across restarts even in dev (you can rotate by changing _AUTH_TOKEN_VERSION).
    """
    try:
        if "SESSION_SECRET" in st.secrets:
            return str(st.secrets["SESSION_SECRET"])
    except Exception:
        pass
    env_val = os.environ.get("SESSION_SECRET")
    if env_val:
        return env_val
    # Fallback: derive from API keys + version so it's at least
    # deterministic and non-trivial in dev mode.
    seed = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        + os.environ.get("OPENAI_API_KEY", "")
        + os.environ.get("EODHD_API_KEY", "")
        + _AUTH_TOKEN_VERSION
    ) or "eqbot-fallback-secret-change-me"
    return hashlib.sha256(seed.encode()).hexdigest()


def _mint_auth_token(username: str, ttl_s: int = _AUTH_COOKIE_TTL_S) -> str:
    expiry  = int(time.time()) + ttl_s
    payload = f"{_AUTH_TOKEN_VERSION}|{username}|{expiry}"
    sig     = hmac.new(
        _session_secret().encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}|{sig}"


def _verify_auth_token(token: str) -> Optional[str]:
    """Return the username if the token is valid + unexpired, else None."""
    if not token or not isinstance(token, str):
        return None
    try:
        parts = token.split("|")
        if len(parts) != 4:
            return None
        version, username, expiry_str, sig = parts
        if version != _AUTH_TOKEN_VERSION:
            return None
        expiry = int(expiry_str)
        if time.time() > expiry:
            return None
        payload      = f"{version}|{username}|{expiry}"
        expected_sig = hmac.new(
            _session_secret().encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return username
    except Exception:
        return None


def _cookie_controller():
    """Return the singleton CookieController, or None if the package isn't
    installed (the app keeps working with session-only auth in that case)."""
    if "_cookie_ctrl" in st.session_state:
        return st.session_state._cookie_ctrl
    try:
        from streamlit_cookies_controller import CookieController
        ctrl = CookieController(key="eqbot_cookie_ctrl")
        st.session_state._cookie_ctrl = ctrl
        return ctrl
    except Exception:
        return None


def _try_restore_session_from_cookie() -> None:
    """If a valid auth cookie is present, mark the session authenticated."""
    if st.session_state.get("authenticated"):
        return
    ctrl = _cookie_controller()
    if ctrl is None:
        return
    try:
        token = ctrl.get(_AUTH_COOKIE_NAME)
    except Exception:
        token = None
    if not token:
        return
    username = _verify_auth_token(token)
    if username:
        st.session_state["authenticated"] = True
        st.session_state["username"]      = username


def _persist_session_cookie(username: str) -> None:
    """Set the signed auth cookie so the browser remembers the login."""
    ctrl = _cookie_controller()
    if ctrl is None:
        return
    token = _mint_auth_token(username)
    try:
        ctrl.set(_AUTH_COOKIE_NAME, token, max_age=_AUTH_COOKIE_TTL_S)
    except Exception:
        pass


def _clear_session_cookie() -> None:
    ctrl = _cookie_controller()
    if ctrl is None:
        return
    try:
        ctrl.remove(_AUTH_COOKIE_NAME)
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
                # Set the signed cookie so a plain F5 keeps the user
                # logged in for the cookie's TTL.
                _persist_session_cookie(uname)
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
            _clear_session_cookie()
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
    # Try to restore the session from a signed auth cookie BEFORE
    # falling through to the login form — keeps the user signed in
    # across F5 / browser-reopen up to the cookie's TTL.
    _try_restore_session_from_cookie()

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
