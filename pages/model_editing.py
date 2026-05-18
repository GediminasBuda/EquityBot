"""
model_editing.py — Framework Studio page for Your Humble EquityBot.

AI chat that understands the full codebase context and edits frameworks directly.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Auth guard ────────────────────────────────────────────────────────────────
from utils.auth import require_auth
require_auth()


def _inject_secrets() -> None:
    try:
        for k in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_MODEL", "LLM_PROVIDER"]:
            if k in st.secrets:
                os.environ[k] = str(st.secrets[k])
    except Exception:
        pass


_inject_secrets()

from framework_manager import FrameworkManager, FrameworkConfig, BUILTIN_IDS
from config import ANTHROPIC_API_KEY, LLM_MODEL

logger = logging.getLogger(__name__)
fm = FrameworkManager()

st.markdown("""
<style>
.block-container { padding-top: 1.2rem; }
.fw-badge {
    display: inline-block; font-size: 10px; font-weight: 600;
    padding: 1px 7px; border-radius: 3px; margin-left: 5px; vertical-align: middle;
}
.badge-builtin { background:#D6E8F7; color:#1B3F6E; }
.badge-custom  { background:#D4EDDA; color:#1A7E3D; }
.badge-forked  { background:#FFF3CD; color:#856404; }
.applied-badge {
    display:inline-block; background:#D4EDDA; color:#1A7E3D;
    font-size:11px; font-weight:600; padding:2px 8px;
    border-radius:4px; margin-bottom:4px;
}
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
def _ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

_ss("selected_fw_id",    "overview_v2")
_ss("studio_chat",       [])    # list of {role, content, applied_fields?}
_ss("studio_pending",    None)  # only used for builtin fork-then-apply flow
_ss("confirm_delete_id", None)
_ss("studio_usage",      {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})

# ── Pricing: Claude Sonnet 4.6 ────────────────────────────────────────────────
_PRICE_INPUT       = 3.00  / 1_000_000   # $3 / MTok
_PRICE_CACHE_WRITE = 3.75  / 1_000_000   # $3.75 / MTok
_PRICE_CACHE_READ  = 0.30  / 1_000_000   # $0.30 / MTok
_PRICE_OUTPUT      = 15.00 / 1_000_000   # $15 / MTok


def _add_usage(usage) -> None:
    """Accumulate token usage from an API response into session state."""
    u = st.session_state.studio_usage
    u["input"]       += getattr(usage, "input_tokens",                0) or 0
    u["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
    u["cache_read"]  += getattr(usage, "cache_read_input_tokens",     0) or 0
    u["output"]      += getattr(usage, "output_tokens",               0) or 0


def _usage_cost(u: dict) -> float:
    return (
        u["input"]       * _PRICE_INPUT +
        u["cache_write"] * _PRICE_CACHE_WRITE +
        u["cache_read"]  * _PRICE_CACHE_READ +
        u["output"]      * _PRICE_OUTPUT
    )


def _usage_bar() -> None:
    """Render a compact token + cost summary line."""
    u    = st.session_state.studio_usage
    cost = _usage_cost(u)
    total_in = u["input"] + u["cache_write"] + u["cache_read"]
    parts = [f"📥 {total_in:,} in", f"📤 {u['output']:,} out"]
    if u["cache_read"]:
        parts.append(f"⚡ {u['cache_read']:,} cached")
    parts.append(f"💰 ~${cost:.4f}")
    st.caption("  ·  ".join(parts))


# ═══════════════════════════════════════════════════════════════════════════════
# Context loaders
# ═══════════════════════════════════════════════════════════════════════════════

# ── Focused framework-creation context (replaces full CLAUDE.md) ─────────────
_FRAMEWORK_CONTEXT = """
You are the Framework Studio AI inside EquityBot — a private AI equity research tool.
Your expertise is writing LLM system prompts and prompt templates for investment analysis frameworks.

━━━ HOW CUSTOM FRAMEWORKS WORK ━━━
Frameworks are JSON configs in frameworks/. When a report is generated:
  1. The user's prompt_template has placeholders substituted with live company data.
  2. The filled template is sent to the LLM (Claude or GPT-4o) with system_prompt as persona.
  3. The LLM returns JSON matching the output_schema field names.
  4. The HTML renderer iterates report_sections to build the final report.

Built-in frameworks (overview_v2, fisher, gravity, eodhd_full, index_overview)
have hardcoded Python PDF renderers and use __builtin__ as their prompt_template — they
cannot be run as custom HTML frameworks.

━━━ ALL AVAILABLE DATA FIELDS ━━━
Everything below is available on the CompanyData object and exposed via placeholders or {financials}.

IDENTITY
  ticker, name, exchange, currency, sector, industry, country, isin
  description (business paragraph), website, employees

CURRENT MARKET
  current_price, market_cap (millions), shares_outstanding (millions), enterprise_value (millions)
  as_of_date

VALUATION MULTIPLES
  pe_ratio (trailing), forward_pe, price_to_book (P/B), ev_ebit, ev_ebitda, ev_sales
  fcf_yield (decimal 0.05 = 5%), dividend_yield (decimal)
  peg_ratio

PROFITABILITY — TTM
  net_margin, ebit_margin, ebitda_margin, gross_margin  (all decimals)
  roe, roa, roic  (all decimals)

BALANCE SHEET HEALTH
  gearing (Net Debt / EBITDA), net_debt (millions), debt_to_equity
  current_ratio, interest_coverage, beta

TECHNICAL / PRICE LEVELS
  week_52_high, week_52_low, ma_50, ma_200

OWNERSHIP
  shares_float (millions), pct_insiders (decimal), pct_institutions (decimal)

DIVIDENDS
  dividend_yield, forward_dividend_rate, forward_dividend_yield, payout_ratio
  ex_dividend_date, dividend_date, five_year_avg_dividend_yield

ANNUAL HISTORY — AnnualFinancials dict keyed by fiscal year (7–10 years)
  P&L:     revenue, gross_profit, ebitda, ebit, net_income, net_income_underlying,
           eps_diluted, dividends_per_share
  Margins: gross_margin, ebit_margin, ebitda_margin, net_margin  (decimals)
  Balance: total_assets, total_debt, cash, net_debt, total_equity, shares_outstanding
  CF:      operating_cash_flow, capex, fcf
  Returns: roe, roa, roic
  Market:  price_year_end, market_cap, enterprise_value, pe_ratio, ev_ebit, ev_sales,
           fcf_yield, div_yield

FORWARD ESTIMATES — ForwardEstimates (next fiscal year)
  revenue, net_income, eps_diluted, ebitda
  revenue_growth_yoy, eps_growth_yoy, net_margin (derived)
  pe_ratio (forward), ev_sales, analyst_count

━━━ PROMPT TEMPLATE PLACEHOLDERS ━━━
{financials}          full annual history table — all AnnualFinancials fields, 7+ years
{forward_estimates}   analyst consensus for next fiscal year
{macro_context}       FRED rates (Fed Funds, CPI, 10Y yield) + World Bank country GDP/inflation

{company_name}  {ticker}  {currency}  {sector}  {industry}  {country}
{description}   {employees}  {website}
{current_price}  {market_cap}  {enterprise_value}
{pe_ratio}  {forward_pe}  {ev_ebitda}  {ev_sales}
{dividend_yield}  {fcf_yield}  {roe}  {ebit_margin}  {net_margin}
{revenue_cagr_3y}  {revenue_cagr_5y}

━━━ OUTPUT SCHEMA FIELD TYPES ━━━
  string   number   boolean
  enum     — add "enum_values": ["BUY","HOLD","SELL"]
  list     — array of strings
  object   — nested dict

━━━ REPORT SECTION TYPES ━━━
  text_block            long-form text (LLM narrative)
  bullet_list           list field rendered as bullets
  recommendation_banner coloured BUY/HOLD/SELL banner
  score_table           table of scored dimensions
  key_value             simple label: value table
  financial_table       numbers table
  checklist             boolean checklist
  peer_table            competitor comparison

━━━ WRITING GOOD PROMPTS ━━━
- system_prompt: analyst persona, what analytical lens the framework applies
- prompt_template: company data blocks + explicit JSON schema the LLM must return
- Always list the exact JSON keys the LLM must return (with types and constraints)
- Monetary values in {financials} are in millions (except per-share figures like eps_diluted, dps)
- Margins / yields / ratios are decimals (0.15 = 15%) — tell the LLM this
- For scoring frameworks: define the scale clearly (e.g. 1–5 where 5 = best) in system_prompt
- For recommendation fields: constrain to exact strings ("BUY", "HOLD", "SELL")
"""


def _fw_to_json(fw: FrameworkConfig) -> str:
    """Serialize full framework content for the system prompt."""
    return json.dumps({
        "id":               fw.id,
        "name":             fw.name,
        "icon":             fw.icon,
        "description":      fw.description,
        "is_builtin":       fw.is_builtin,
        "base_id":          fw.base_id,
        "version":          fw.version,
        "system_prompt":    fw.system_prompt,
        "prompt_template":  fw.prompt_template,
        "output_schema":    fw.output_schema,
        "report_sections":  fw.report_sections,
    }, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Claude helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _call_claude(system: str, messages: list, max_tokens: int = 4000) -> str:
    """Always calls Claude (Framework Studio always uses Claude regardless of LLM_PROVIDER)."""
    if not ANTHROPIC_API_KEY:
        return (
            "⚠️ ANTHROPIC_API_KEY is not configured. "
            "Add it to .env (or Streamlit secrets) to use the Framework Studio."
        )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=max_tokens,
            temperature=0.3,
            system=system,
            messages=messages,
        )
        _add_usage(resp.usage)
        return resp.content[0].text
    except Exception as e:
        return f"⚠️ Claude API error: {e}"


def _extract_changes(text: str) -> Optional[dict]:
    """Pull a ```changes { ... } ``` JSON block out of Claude's response."""
    m = re.search(r"```changes\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _strip_changes_block(text: str) -> str:
    return re.sub(r"```changes\s*\n.*?\n```", "", text, flags=re.DOTALL).strip()


# ═══════════════════════════════════════════════════════════════════════════════
# System prompts — rich context
# ═══════════════════════════════════════════════════════════════════════════════

def _system_prompt_edit(fw: FrameworkConfig) -> str:
    fw_json = _fw_to_json(fw)
    return f"""{_FRAMEWORK_CONTEXT}

━━━ CURRENT FRAMEWORK — FULL CONTENT ━━━
{fw_json}

━━━ HOW TO OUTPUT CHANGES ━━━
When making a change, emit a ```changes block with ONLY the fields that change.
Always output the COMPLETE new value — never a partial diff.

```changes
{{
  "system_prompt": "full new system prompt here",
  "prompt_template": "full new template here"
}}
```

Allowed fields: system_prompt, prompt_template, name, description, icon, output_schema, report_sections

Rules:
- If the framework is built-in (is_builtin: true), still propose the changes — the UI will
  offer a "Fork & Apply" button.
- Clean JSON only — no comments, no trailing commas.
- Brief explanation before the block is fine; nothing needed after.
- When only answering a question (no change), reply in plain text — no ```changes block.
"""


def _system_prompt_new() -> str:
    return f"""{_FRAMEWORK_CONTEXT}

━━━ HOW TO PROPOSE A NEW FRAMEWORK ━━━
When you are ready, emit a ```changes block:

```changes
{{
  "name": "Framework Name",
  "icon": "📊",
  "description": "One-line description shown in the report selector",
  "system_prompt": "You are an analyst specialising in…",
  "prompt_template": "Analyse {{company_name}} ({{ticker}}).\\n\\n{{financials}}\\n\\nReturn JSON with:\\n...",
  "output_schema": [
    {{"name": "summary",            "type": "string", "description": "...", "required": true}},
    {{"name": "recommendation",     "type": "enum",   "description": "BUY/HOLD/SELL", "required": true, "enum_values": ["BUY","HOLD","SELL"]}},
    {{"name": "recommendation_rationale", "type": "string", "description": "...", "required": true}}
  ],
  "report_sections": [
    {{"id": "s1", "type": "text_block",           "title": "Analysis",       "field": "summary",                  "order": 1}},
    {{"id": "s2", "type": "recommendation_banner","title": "Recommendation", "field": "recommendation",           "order": 2}},
    {{"id": "s3", "type": "text_block",           "title": "Rationale",     "field": "recommendation_rationale", "order": 3}}
  ]
}}
```

Always include recommendation + recommendation_rationale unless the user explicitly wants a
pure data / no-recommendation framework.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Framework actions
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_changes(fw: FrameworkConfig, changes: dict) -> list[str]:
    """Apply changes to a framework. Returns list of changed field names."""
    changed = []
    for field in ("system_prompt", "prompt_template", "name", "description", "icon",
                  "output_schema", "report_sections"):
        if field in changes:
            setattr(fw, field, changes[field])
            changed.append(field)
    fm.save(fw)
    return changed


def _do_fork(fw: FrameworkConfig, auto_apply: Optional[dict] = None) -> FrameworkConfig:
    forked = fm.fork(fw.id, f"{fw.name} (my version)")
    if auto_apply:
        _apply_changes(forked, auto_apply)
    st.session_state.selected_fw_id = forked.id
    st.session_state.studio_chat    = []
    st.session_state.studio_usage   = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    st.session_state.studio_pending = None
    return forked


def _save_new_framework(d: dict) -> FrameworkConfig:
    name   = d.get("name", "New Framework")
    new_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] + "_" + uuid.uuid4().hex[:6]
    now    = datetime.utcnow().isoformat()
    config = FrameworkConfig(
        id=new_id, name=name, icon=d.get("icon", "📋"),
        description=d.get("description", ""), is_builtin=False,
        version=0, created_at=now, modified_at=now,
        system_prompt=d.get("system_prompt", ""),
        prompt_template=d.get("prompt_template", ""),
        output_schema=d.get("output_schema", []),
        report_sections=d.get("report_sections", []),
    )
    fm.save(config)
    return config


# ═══════════════════════════════════════════════════════════════════════════════
# Tab renderers
# ═══════════════════════════════════════════════════════════════════════════════

def render_chat_tab(fw: FrameworkConfig) -> None:
    if fw.is_builtin:
        st.info(
            "💡 **Built-in framework — read-only.** "
            "Chat to explore or plan changes. When Claude proposes a change you'll see a "
            "**Fork & Apply** button — that creates your own editable copy and applies the change."
        )

    # ── Chat history ──────────────────────────────────────────────────────────
    chat_box = st.container(height=460)
    with chat_box:
        if not st.session_state.studio_chat:
            st.markdown(
                f"*Tell me what to change in the **{fw.name}** framework — "
                f"I'll update it immediately. Or ask me anything about how it works.*"
            )

        for msg in st.session_state.studio_chat:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("applied_fields"):
                    fields_str = ", ".join(f"`{f}`" for f in msg["applied_fields"])
                    st.markdown(
                        f'<span class="applied-badge">✓ Applied: {fields_str}</span>',
                        unsafe_allow_html=True,
                    )

        # Pending block — only appears for built-in frameworks (needs fork first)
        pending = st.session_state.studio_pending
        if pending and fw.is_builtin:
            st.divider()
            st.markdown("**📝 Proposed changes (fork to apply):**")
            for k, v in pending.items():
                if k in ("output_schema", "report_sections"):
                    st.markdown(f"**{k}:** *(structured data — {len(v)} items)*")
                elif isinstance(v, str) and len(v) > 300:
                    with st.expander(f"**{k}** (click to expand)"):
                        st.code(v[:2000] + ("…" if len(v) > 2000 else ""), language="text")
                else:
                    st.markdown(f"**{k}:** {v}")

            if st.button("🍴 Fork & Apply changes", type="primary", key="fork_apply"):
                forked = _do_fork(fw, auto_apply=pending)
                st.session_state.studio_pending = None
                st.success(f"✅ Forked to **{forked.name}** and changes applied.")
                st.rerun()
            if st.button("❌ Discard", key="discard_pending"):
                st.session_state.studio_pending = None
                st.rerun()

    # ── Usage bar ─────────────────────────────────────────────────────────────
    _usage_bar()

    # ── Input ─────────────────────────────────────────────────────────────────
    user_input = st.chat_input(
        "Tell me what to change, or ask a question…",
        key=f"chat_input_{fw.id}",
    )
    if user_input:
        st.session_state.studio_chat.append({"role": "user", "content": user_input})

        sys_prompt = _system_prompt_edit(fw)
        msgs = [{"role": m["role"], "content": m["content"]}
                for m in st.session_state.studio_chat]

        with st.spinner("Thinking…"):
            response = _call_claude(sys_prompt, msgs)

        changes      = _extract_changes(response)
        clean_text   = _strip_changes_block(response) if changes else response
        applied_flds = None

        if changes:
            if not fw.is_builtin:
                # ── Auto-apply immediately for editable frameworks ────────────
                applied_flds = _apply_changes(fw, changes)
                assistant_msg = clean_text or f"Done — updated {', '.join(applied_flds)}."
            else:
                # ── For built-in: stage as pending (user must fork first) ─────
                st.session_state.studio_pending = changes
                assistant_msg = (
                    clean_text
                    or "I've proposed changes above. Fork the framework to apply them."
                )
        else:
            assistant_msg = response

        st.session_state.studio_chat.append({
            "role":           "assistant",
            "content":        assistant_msg,
            "applied_fields": applied_flds,
        })
        st.rerun()


def render_editor_tab(fw: FrameworkConfig) -> None:
    if fw.is_builtin:
        st.info("Fork this framework (button above) to enable direct editing.")

    st.markdown("#### System Prompt")
    st.caption("The LLM's persona and analytical philosophy.")
    new_system = st.text_area(
        "system_prompt", value=fw.system_prompt, height=280,
        disabled=fw.is_builtin, label_visibility="collapsed",
        key=f"ed_sys_{fw.id}",
    )

    st.markdown("#### Prompt Template")
    st.caption(
        "The user prompt assembled per analysis run. "
        "Use `{financials}`, `{currency}`, `{company_name}`, `{ticker}`, "
        "`{forward_estimates}` as the main dynamic placeholders."
    )

    with st.expander("📋 Available placeholders", expanded=False):
        cols = st.columns(2)
        placeholders = list(FrameworkManager.available_placeholders().items())
        for i, (ph, desc) in enumerate(placeholders):
            with cols[i % 2]:
                st.markdown(f"**`{ph}`**  \n<small>{desc}</small>", unsafe_allow_html=True)

    tmpl_display = (
        fw.prompt_template
        if fw.prompt_template != "__builtin__"
        else "# This framework uses a built-in code-generated prompt.\n"
             "# Fork it and replace this with your own template.\n\n"
             "Analyse {company_name} ({ticker}).\n\n{financials}\n\n"
             "Return a JSON object with your analysis."
    )
    new_template = st.text_area(
        "prompt_template", value=tmpl_display, height=380,
        disabled=fw.is_builtin, label_visibility="collapsed",
        key=f"ed_tmpl_{fw.id}",
    )

    if not fw.is_builtin:
        sys_changed  = new_system != fw.system_prompt
        tmpl_changed = (
            new_template != fw.prompt_template
            and not new_template.startswith("# This framework uses a built-in")
        )
        if sys_changed or tmpl_changed:
            if st.button("💾 Save changes", type="primary", key=f"save_ed_{fw.id}"):
                if sys_changed:
                    fw.system_prompt = new_system
                if tmpl_changed:
                    fw.prompt_template = new_template
                fm.save(fw)
                st.success("✓ Saved.")
                st.rerun()
        else:
            st.button("💾 Save changes", disabled=True, key=f"save_ed_dis_{fw.id}")
            st.caption("No unsaved changes.")


def render_schema_tab(fw: FrameworkConfig) -> None:
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("#### Output Schema")
        st.caption("Fields the LLM returns in its JSON response.")
        if fw.output_schema:
            for field in fw.output_schema:
                name  = field.get("name", "")
                ftype = field.get("type", "string")
                req   = "required" if field.get("required") else "optional"
                desc  = field.get("description", "")
                ev    = field.get("enum_values", [])
                ev_str = f"  ·  `{'|'.join(ev)}`" if ev else ""
                st.markdown(
                    f"**`{name}`** `{ftype}` *{req}*{ev_str}  \n"
                    f"<small style='color:#555'>{desc}</small>",
                    unsafe_allow_html=True,
                )
                st.divider()
        else:
            st.caption("No output schema defined yet.")

    with c2:
        st.markdown("#### Report Sections")
        st.caption("Ordered list of sections rendered in the HTML report.")
        if fw.report_sections:
            for sec in sorted(fw.report_sections, key=lambda s: s.get("order", 99)):
                order  = sec.get("order", "?")
                stype  = sec.get("type", "")
                stitle = sec.get("title", "")
                sfield = sec.get("field", "")
                st.markdown(
                    f"**{order}.** {stitle}  \n"
                    f"<small style='color:#555'>`{stype}` → field `{sfield}`</small>",
                    unsafe_allow_html=True,
                )
                st.divider()
        else:
            st.caption("No sections defined yet.")


def render_preview_tab(fw: FrameworkConfig) -> None:
    st.markdown("#### Live Preview")
    st.caption(
        "Generate a sample HTML report to see how the framework renders. "
        "Built-in frameworks use their dedicated PDF generators — preview works only for custom frameworks."
    )

    p1, p2 = st.columns([3, 1])
    with p1:
        preview_ticker = st.text_input(
            "Ticker", value="WKL.AS",
            placeholder="e.g. WKL.AS, AAPL, V",
            key=f"prev_ticker_{fw.id}",
        )
    with p2:
        st.markdown("&nbsp;")
        run_btn = st.button(
            "▶ Run Preview",
            type="primary",
            use_container_width=True,
            key=f"prev_btn_{fw.id}",
            disabled=(not ANTHROPIC_API_KEY or fw.is_builtin),
        )
        if fw.is_builtin:
            st.caption("Not available for built-in frameworks")
        elif not ANTHROPIC_API_KEY:
            st.caption("Needs ANTHROPIC_API_KEY")

    if run_btn and preview_ticker:
        tmpl = fw.prompt_template
        if tmpl == "__builtin__" or tmpl.startswith("# This framework uses a built-in"):
            st.warning(
                "This framework still has the built-in prompt placeholder. "
                "Edit the **Prompt Template** in the Editor tab first."
            )
            return

        with st.spinner(f"Generating '{fw.name}' preview for {preview_ticker}…"):
            try:
                from models.generic_runner import GenericRunner
                runner  = GenericRunner()
                tf      = tempfile.NamedTemporaryFile(suffix=".html", delete=False)
                tmp_path = tf.name
                tf.close()

                html_path = runner.run(
                    preview_ticker.strip().upper(),
                    fw,
                    output_path=tmp_path,
                )
                with open(html_path, "r", encoding="utf-8") as f:
                    html_content = f.read()
                try:
                    os.unlink(html_path)
                except Exception:
                    pass

                st.success(f"Preview ready for **{preview_ticker.upper()}**")
                b64 = base64.b64encode(html_content.encode()).decode()
                st.markdown(
                    f'<iframe src="data:text/html;base64,{b64}" '
                    f'width="100%" height="720px" '
                    f'style="border:1px solid #BBCCDD;border-radius:4px"></iframe>',
                    unsafe_allow_html=True,
                )
                st.download_button(
                    "⬇ Download HTML",
                    data=html_content.encode("utf-8"),
                    file_name=f"{preview_ticker.lower().replace('.','_')}_{fw.id}.html",
                    mime="text/html",
                    key=f"dl_prev_{fw.id}",
                )
            except Exception as e:
                st.error(f"Preview failed: {e}")
                logger.exception("Preview error")


def render_new_framework_page() -> None:
    """Full-page UI for creating a new framework via AI chat or manual form."""
    st.markdown("### ＋ Create a New Framework")
    st.markdown(
        "Describe the framework you want and I'll design it. "
        "Or use the **Manual** tab to build from scratch."
    )
    st.divider()

    tab_ai, tab_manual = st.tabs(["💬 AI-Assisted", "✏️ Manual"])

    with tab_ai:
        st.markdown(
            "**Examples:**\n"
            "- *Create a dividend growth framework focused on 10-year payout history, "
            "earnings coverage, and FCF conversion*\n"
            "- *Build a simple 5-question quality scorecard for small-cap companies*\n"
            "- *Design a capital allocation framework scoring buybacks, M&A, and dividends*"
        )

        chat_box = st.container(height=380)
        with chat_box:
            if not st.session_state.studio_chat:
                st.markdown("*Describe the framework and I'll generate a full design.*")
            for msg in st.session_state.studio_chat:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

            pending = st.session_state.studio_pending
            if pending and "name" in pending and "system_prompt" in pending:
                st.divider()
                st.markdown(f"**📋 Proposed framework: {pending.get('name', '?')}**")
                st.markdown(f"*{pending.get('description', '')}*")
                with st.expander("System Prompt (preview)"):
                    st.code(str(pending.get("system_prompt", ""))[:800], language="text")
                with st.expander("Output Schema"):
                    for f in pending.get("output_schema", []):
                        st.markdown(f"- `{f.get('name')}` ({f.get('type')})")

                sa, sb = st.columns(2)
                with sa:
                    if st.button("✅ Save this framework", type="primary",
                                 use_container_width=True, key="save_new_ai"):
                        fw = _save_new_framework(pending)
                        st.session_state.studio_pending = None
                        st.success(f"✅ Created: **{fw.name}**")
                        st.rerun()
                with sb:
                    if st.button("🔄 Keep chatting",
                                 use_container_width=True, key="iterate_new"):
                        st.session_state.studio_pending = None
                        st.rerun()

        user_input = st.chat_input(
            "Describe your framework…", key="new_fw_input"
        )
        if user_input:
            st.session_state.studio_chat.append({"role": "user", "content": user_input})
            msgs = [{"role": m["role"], "content": m["content"]}
                    for m in st.session_state.studio_chat]
            with st.spinner("Designing your framework…"):
                response = _call_claude(_system_prompt_new(), msgs)
            changes = _extract_changes(response)
            if changes:
                st.session_state.studio_pending = changes
                clean = _strip_changes_block(response)
                st.session_state.studio_chat.append({
                    "role":    "assistant",
                    "content": clean or f"I've designed **{changes.get('name','the framework')}** — review above.",
                })
            else:
                st.session_state.studio_chat.append({"role": "assistant", "content": response})
            st.rerun()

    with tab_manual:
        name   = st.text_input("Framework name *", placeholder="e.g. Dividend Growth Analyser")
        icon   = st.text_input("Icon (emoji)", value="📈", max_chars=4)
        desc   = st.text_input("Short description", placeholder="One sentence shown in the report selector")
        sys_p  = st.text_area("System prompt *", height=160, label_visibility="visible",
                               placeholder="You are an analyst specialising in…")
        st.caption("Prompt template — use `{financials}`, `{company_name}`, `{ticker}`, `{currency}` as placeholders")
        tmpl   = st.text_area(
            "Prompt template *", height=260, label_visibility="collapsed",
            placeholder=(
                "Analyse {company_name} ({ticker}). Currency: {currency}.\n\n"
                "{financials}\n\n"
                "Return a JSON object with:\n"
                "{\n"
                '  "summary": "200 word analysis...",\n'
                '  "key_risks": ["risk 1", "risk 2"],\n'
                '  "recommendation": "BUY or HOLD or SELL",\n'
                '  "recommendation_rationale": "100 word rationale"\n'
                "}"
            ),
        )

        if st.button("💾 Create Framework", type="primary",
                     disabled=not name, key="create_manual"):
            if not sys_p.strip():
                st.warning("System prompt is required.")
            elif not tmpl.strip():
                st.warning("Prompt template is required.")
            else:
                config = _save_new_framework({
                    "name": name, "icon": icon or "📋",
                    "description": desc or "",
                    "system_prompt": sys_p,
                    "prompt_template": tmpl,
                    "output_schema": [
                        {"name": "summary", "type": "string",
                         "description": "Analysis summary", "required": True},
                        {"name": "key_risks", "type": "list",
                         "description": "Key risks", "required": True},
                        {"name": "recommendation", "type": "enum",
                         "description": "Recommendation", "required": True,
                         "enum_values": ["BUY", "HOLD", "SELL"]},
                        {"name": "recommendation_rationale", "type": "string",
                         "description": "Rationale", "required": True},
                    ],
                    "report_sections": [
                        {"id": "s1", "type": "text_block", "title": "Analysis",
                         "field": "summary", "order": 1},
                        {"id": "s2", "type": "bullet_list", "title": "Key Risks",
                         "field": "key_risks", "order": 2},
                        {"id": "s3", "type": "recommendation_banner",
                         "title": "Recommendation", "field": "recommendation", "order": 3},
                    ],
                })
                st.success(f"✅ Created: **{config.name}**")
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## ⚙️ Model Editing")
    st.caption("Edit · Create · Export · Import")
    st.divider()

    # Import
    uploaded = st.file_uploader(
        "📥 Import framework (.json)",
        type=["json"],
        label_visibility="collapsed",
        key="fw_upload",
    )
    if uploaded is not None:
        try:
            imported = fm.import_from_bytes(uploaded.read())
            st.success(f"Imported: **{imported.name}**")
            st.session_state.selected_fw_id = imported.id
            st.session_state.studio_chat    = []
            st.session_state.studio_usage   = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
            st.session_state.studio_pending = None
            st.rerun()
        except Exception as e:
            st.error(f"Import failed: {e}")

    if st.button("＋ New Framework", use_container_width=True, key="btn_new_fw"):
        st.session_state.selected_fw_id = "__new__"
        st.session_state.studio_chat    = []
        st.session_state.studio_usage   = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        st.session_state.studio_pending = None
        st.rerun()

    st.divider()
    st.markdown("#### Frameworks")
    st.caption("Use ↑ / ↓ to reorder. Saved automatically across the app.")

    _fw_list = fm.list()
    for i, fw in enumerate(_fw_list):
        is_sel = (fw.id == st.session_state.selected_fw_id)
        sel_col, up_col, down_col = st.columns([6, 1, 1])
        with sel_col:
            if st.button(
                f"{fw.icon}  {fw.name}",
                key=f"sel_{fw.id}",
                use_container_width=True,
                type="primary" if is_sel else "secondary",
            ):
                if not is_sel:
                    st.session_state.selected_fw_id = fw.id
                    st.session_state.studio_chat    = []
                    st.session_state.studio_usage   = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
                    st.session_state.studio_pending = None
                st.rerun()
        with up_col:
            if st.button("↑", key=f"me_up_{fw.id}",
                         disabled=(i == 0),
                         use_container_width=True,
                         help="Move up"):
                fm.move(fw.id, -1)
                st.rerun()
        with down_col:
            if st.button("↓", key=f"me_down_{fw.id}",
                         disabled=(i == len(_fw_list) - 1),
                         use_container_width=True,
                         help="Move down"):
                fm.move(fw.id, +1)
                st.rerun()
        tag = "built-in" if fw.is_builtin else ("forked" if fw.base_id else "custom")
        st.caption(f"  {tag}  ·  v{fw.version}")

    st.divider()
    st.markdown(
        "<small>Built-in frameworks are protected. "
        "Chat proposes changes — you'll get a **Fork & Apply** button. "
        "For your own frameworks changes apply immediately.</small>",
        unsafe_allow_html=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main area
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("# ⚙️ Model Editing")

sel_id = st.session_state.selected_fw_id

if sel_id == "__new__":
    render_new_framework_page()

else:
    fw = fm.get(sel_id)
    if fw is None:
        st.error(f"Framework '{sel_id}' not found. Select one from the sidebar.")
        st.stop()

    # ── Header row ────────────────────────────────────────────────────────────
    h1, h2 = st.columns([3, 2])
    with h1:
        tag    = "built-in" if fw.is_builtin else ("forked" if fw.base_id else "custom")
        origin = f" *(forked from {fw.base_id})*" if fw.base_id and not fw.is_builtin else ""
        st.markdown(f"## {fw.icon} {fw.name}")
        st.caption(f"{fw.description}  ·  {tag}  ·  v{fw.version}{origin}")

    with h2:
        a1, a2, a3 = st.columns(3)
        with a1:
            if fw.is_builtin:
                if st.button("🍴 Fork", use_container_width=True,
                             help="Create an editable personal copy", key="btn_fork"):
                    forked = _do_fork(fw)
                    st.success(f"✅ Forked to **{forked.name}**")
                    st.rerun()
        with a2:
            st.download_button(
                "⬇ Export",
                data=fm.export_bytes(fw.id),
                file_name=f"{fw.id}.json",
                mime="application/json",
                use_container_width=True,
                key=f"exp_{fw.id}",
            )
        with a3:
            if not fw.is_builtin:
                if st.button("🗑 Delete", use_container_width=True,
                             type="secondary", key="btn_del"):
                    st.session_state.confirm_delete_id = fw.id

        # Confirm delete
        if st.session_state.confirm_delete_id == fw.id:
            st.warning(f"Permanently delete **{fw.name}**?")
            dc1, dc2 = st.columns(2)
            with dc1:
                if st.button("Yes, delete", type="primary",
                             use_container_width=True, key="confirm_del"):
                    fm.delete(fw.id)
                    # Fall back to the first remaining framework (built-ins
                    # appear first, so this is normally overview_v2).
                    remaining = fm.list()
                    st.session_state.selected_fw_id = (
                        remaining[0].id if remaining else "overview_v2"
                    )
                    st.session_state.confirm_delete_id = None
                    st.rerun()
            with dc2:
                if st.button("Cancel", use_container_width=True, key="cancel_del"):
                    st.session_state.confirm_delete_id = None
                    st.rerun()

    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    t_chat, t_editor, t_schema, t_preview = st.tabs(
        ["💬 Chat", "✏️ Editor", "📐 Schema & Sections", "👁 Preview"]
    )

    with t_chat:
        render_chat_tab(fw)

    with t_editor:
        render_editor_tab(fw)

    with t_schema:
        render_schema_tab(fw)

    with t_preview:
        render_preview_tab(fw)
