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
import os
import sys
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
                st.session_state["authenticated"] = True
                st.session_state["username"]      = username.strip().lower()
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
      .st-emotion-cache-1permvm { gap: 0; }

      /* On mobile the sidebar drawer must overlay every other layer
         (toolbar, page-title sticky banner, modal-style elements).
         Streamlit's own toolbar uses z-index ~999999, so push the
         sidebar far above it. */
      @media (max-width: 768px) {
        .stSidebar { z-index: 999999999999 !important; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Auth gate ─────────────────────────────────────────────────────────────────
_users = _load_users()
if _users:
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
