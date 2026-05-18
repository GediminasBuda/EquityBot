"""
app_editing.py — App Editor page for Your Humble EquityBot.

Chat with Claude to make direct changes to the app's Python files.
Claude has read/edit/write/list tools and full CLAUDE.md context.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import streamlit as st

# ── Path setup ────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

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

from config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

# ── Styles ────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.block-container { padding-top: 1.2rem; }
.tool-chip {
    display:inline-block; background:#EEF5FB; color:#1B3F6E;
    font-size:11px; font-weight:600; padding:2px 8px;
    border-radius:4px; margin:2px 2px 2px 0;
}
.tool-edit  { background:#FFF3CD; color:#856404; }
.tool-write { background:#D4EDDA; color:#1A7E3D; }
.tool-read  { background:#E2E8F0; color:#334155; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
def _ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

_ss("app_chat",  [])   # list of {role, content, tool_calls?}
_ss("app_usage", {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})

# ── Pricing: Claude Sonnet 4.6 ────────────────────────────────────────────────
_PRICE_INPUT       = 3.00  / 1_000_000
_PRICE_CACHE_WRITE = 3.75  / 1_000_000
_PRICE_CACHE_READ  = 0.30  / 1_000_000
_PRICE_OUTPUT      = 15.00 / 1_000_000


def _add_usage(usage) -> None:
    u = st.session_state.app_usage
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
    u    = st.session_state.app_usage
    cost = _usage_cost(u)
    total_in = u["input"] + u["cache_write"] + u["cache_read"]
    parts = [f"📥 {total_in:,} in", f"📤 {u['output']:,} out"]
    if u["cache_read"]:
        parts.append(f"⚡ {u['cache_read']:,} cached")
    parts.append(f"💰 ~${cost:.4f}")
    st.caption("  ·  ".join(parts))


# ═══════════════════════════════════════════════════════════════════════════════
# File tools — execute on behalf of Claude
# ═══════════════════════════════════════════════════════════════════════════════

# Files Claude is NOT allowed to touch (safety)
_PROTECTED = {".env", "CLAUDE.md"}
_ALLOWED_EXTENSIONS = {".py", ".json", ".toml", ".md", ".txt", ".css"}


def _safe_path(rel_path: str) -> Optional[Path]:
    """Resolve a relative path inside the project. Returns None if unsafe."""
    try:
        p = (_ROOT / rel_path).resolve()
        _ROOT.resolve()  # ensure root is resolved
        if not str(p).startswith(str(_ROOT.resolve())):
            return None   # path escape attempt
        if p.name in _PROTECTED:
            return None
        return p
    except Exception:
        return None


def _tool_read_file(path: str) -> str:
    p = _safe_path(path)
    if p is None:
        return f"ERROR: path '{path}' is not allowed."
    if not p.exists():
        return f"ERROR: '{path}' does not exist."
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        return f"ERROR reading '{path}': {e}"


def _tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    p = _safe_path(path)
    if p is None:
        return f"ERROR: path '{path}' is not allowed."
    if not p.exists():
        return f"ERROR: '{path}' does not exist."
    if p.suffix not in _ALLOWED_EXTENSIONS:
        return f"ERROR: editing '{p.suffix}' files is not allowed."
    try:
        content = p.read_text(encoding="utf-8")
        if old_string not in content:
            return f"ERROR: old_string not found in '{path}'. Check for exact match including whitespace."
        new_content = content.replace(old_string, new_string, 1)
        p.write_text(new_content, encoding="utf-8")
        lines_changed = abs(new_string.count("\n") - old_string.count("\n"))
        return f"OK: '{path}' edited ({lines_changed:+d} lines)."
    except Exception as e:
        return f"ERROR editing '{path}': {e}"


def _tool_write_file(path: str, content: str) -> str:
    p = _safe_path(path)
    if p is None:
        return f"ERROR: path '{path}' is not allowed."
    if p.suffix not in _ALLOWED_EXTENSIONS:
        return f"ERROR: writing '{p.suffix}' files is not allowed."
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: '{path}' written ({content.count(chr(10))+1} lines)."
    except Exception as e:
        return f"ERROR writing '{path}': {e}"


def _tool_list_files(directory: str = "", pattern: str = "**/*.py") -> str:
    base = _safe_path(directory) if directory else _ROOT
    if base is None:
        return "ERROR: directory not allowed."
    try:
        files = sorted(base.glob(pattern))
        rel   = [str(f.relative_to(_ROOT)) for f in files if f.is_file()]
        if not rel:
            return f"No files found matching '{pattern}' in '{directory or '.'}'"
        return "\n".join(rel[:80])
    except Exception as e:
        return f"ERROR listing files: {e}"


def _tool_grep(pattern: str, directory: str = "", file_glob: str = "**/*.py") -> str:
    """Simple grep — find lines matching pattern."""
    import re
    base = _safe_path(directory) if directory else _ROOT
    if base is None:
        return "ERROR: directory not allowed."
    results = []
    try:
        rx = re.compile(pattern)
        for f in sorted(base.glob(file_glob)):
            if not f.is_file():
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{f.relative_to(_ROOT)}:{i}: {line.rstrip()}")
                        if len(results) >= 60:
                            results.append("... (truncated at 60 matches)")
                            return "\n".join(results)
            except Exception:
                pass
        return "\n".join(results) if results else f"No matches for '{pattern}'"
    except re.error as e:
        return f"ERROR: invalid regex: {e}"


_TOOL_DISPATCH = {
    "read_file":  lambda inp: _tool_read_file(inp["path"]),
    "edit_file":  lambda inp: _tool_edit_file(inp["path"], inp["old_string"], inp["new_string"]),
    "write_file": lambda inp: _tool_write_file(inp["path"], inp["content"]),
    "list_files": lambda inp: _tool_list_files(inp.get("directory",""), inp.get("pattern","**/*.py")),
    "grep":       lambda inp: _tool_grep(inp["pattern"], inp.get("directory",""), inp.get("file_glob","**/*.py")),
}

_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the full contents of a project file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to project root, e.g. 'agents/pdf_gravity.py'"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file. "
            "old_string must match exactly (including indentation and newlines). "
            "Only the first occurrence is replaced. "
            "Read the file first if you are not certain of the exact text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string", "description": "Relative path from project root"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"}
            },
            "required": ["path", "old_string", "new_string"]
        }
    },
    {
        "name": "write_file",
        "description": "Write (overwrite) an entire file. Use for new files or complete rewrites.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Relative path from project root"},
                "content": {"type": "string", "description": "Full file content"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in the project matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Sub-directory to search in (optional, default: root)"},
                "pattern":   {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'agents/*.py'"}
            }
        }
    },
    {
        "name": "grep",
        "description": "Search for a regex pattern across project files. Returns matching lines with file:line context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern":   {"type": "string", "description": "Python regex pattern to search for"},
                "directory": {"type": "string", "description": "Sub-directory to limit search (optional)"},
                "file_glob": {"type": "string", "description": "File glob to limit search, e.g. '**/*.py'"}
            },
            "required": ["pattern"]
        }
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# System prompt
# ═══════════════════════════════════════════════════════════════════════════════

def _load_claude_md() -> str:
    try:
        return (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    except Exception:
        return "(CLAUDE.md not found)"


def _system_prompt() -> str:
    claude_md = _load_claude_md()
    # Build a compact project tree
    tree_lines = []
    for ext in ["*.py", "**/*.py"]:
        for f in sorted(_ROOT.glob(ext)):
            rel = str(f.relative_to(_ROOT))
            if "cache" not in rel and "outputs" not in rel and "__pycache__" not in rel:
                tree_lines.append(rel)
    tree = "\n".join(sorted(set(tree_lines))[:60])

    return f"""You are the App Editor AI for EquityBot — a private AI equity research tool built with Streamlit + Python.
The user talks to you in natural language to make changes to the app's source code.
You have tools to read, edit, write, and search files. Use them to implement the requested changes.

IMPORTANT RULES:
- Always read a file before editing it if you are not certain of the exact content.
- Use edit_file (not write_file) for targeted changes — it's safer.
- After making a change, confirm what you did in plain language.
- If a change requires a Streamlit restart to take effect, mention it.
- Do not edit .env or CLAUDE.md (they are protected).
- Keep changes minimal — only touch what is needed.

━━━ FULL PROJECT DOCUMENTATION ━━━
{claude_md}

━━━ PROJECT FILE TREE ━━━
{tree}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Agentic loop
# ═══════════════════════════════════════════════════════════════════════════════

def _run_agent(messages: list) -> tuple[str, list[dict]]:
    """
    Run Claude with file tools until it stops calling tools.
    Returns (final_text, list_of_tool_calls).
    Each tool_call: {name, input, result, is_edit}.
    """
    if not ANTHROPIC_API_KEY:
        return (
            "⚠️ ANTHROPIC_API_KEY is not configured. Add it to .env or Streamlit secrets.",
            []
        )

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    tool_calls: list[dict] = []
    current_messages = [{"role": m["role"], "content": m["content"]}
                        for m in messages]

    MAX_ITERS = 20  # safety cap
    for _ in range(MAX_ITERS):
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=8000,
                temperature=0,
                system=_system_prompt(),
                tools=_TOOLS,
                messages=current_messages,
            )
        except Exception as e:
            return f"⚠️ Claude API error: {e}", tool_calls

        _add_usage(resp.usage)

        if resp.stop_reason == "end_turn":
            text = "".join(
                b.text for b in resp.content if hasattr(b, "text")
            )
            return text, tool_calls

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                fn   = _TOOL_DISPATCH.get(block.name)
                result = fn(block.input) if fn else f"ERROR: unknown tool '{block.name}'"
                is_edit = block.name in ("edit_file", "write_file")
                tool_calls.append({
                    "name":    block.name,
                    "input":   block.input,
                    "result":  result,
                    "is_edit": is_edit,
                })
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })

            current_messages.append({"role": "assistant", "content": resp.content})
            current_messages.append({"role": "user",      "content": tool_results})

        else:
            # Unexpected stop reason
            text = "".join(
                b.text for b in resp.content if hasattr(b, "text")
            )
            return text or "(stopped unexpectedly)", tool_calls

    return "⚠️ Reached maximum iteration limit.", tool_calls


# ═══════════════════════════════════════════════════════════════════════════════
# UI helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _tool_chip_html(call: dict) -> str:
    name     = call["name"]
    inp      = call["input"]
    result   = call["result"]
    is_err   = result.startswith("ERROR")
    css      = "tool-edit" if call["is_edit"] else "tool-read"
    if is_err:
        css = "tool-chip" + " tool-edit"
    label    = name

    if name == "read_file":
        label = f"📖 read {inp.get('path','?')}"
    elif name == "edit_file":
        label = f"✏️ edit {inp.get('path','?')}"
    elif name == "write_file":
        label = f"💾 write {inp.get('path','?')}"
    elif name == "list_files":
        label = f"📁 list {inp.get('pattern','**/*.py')}"
    elif name == "grep":
        label = f"🔍 grep '{inp.get('pattern','?')}'"

    status = "❌" if is_err else "✓"
    return f'<span class="tool-chip {css}">{status} {label}</span>'


def _render_tool_calls(tool_calls: list[dict]) -> None:
    if not tool_calls:
        return
    chips_html = " ".join(_tool_chip_html(c) for c in tool_calls)
    st.markdown(chips_html, unsafe_allow_html=True)

    edit_calls = [c for c in tool_calls if c["is_edit"]]
    if edit_calls:
        with st.expander(f"📝 {len(edit_calls)} file(s) changed", expanded=False):
            for c in edit_calls:
                st.markdown(f"**{c['name']}** → `{c['input'].get('path','?')}`")
                st.caption(c["result"])

    read_calls = [c for c in tool_calls if not c["is_edit"]]
    if read_calls:
        with st.expander(f"🔎 {len(read_calls)} read/search operation(s)", expanded=False):
            for c in read_calls:
                inp = c["input"]
                label = inp.get("path") or inp.get("pattern") or c["name"]
                with st.expander(f"`{c['name']}` — {label}", expanded=False):
                    result_preview = c["result"][:1200]
                    if len(c["result"]) > 1200:
                        result_preview += "\n… (truncated)"
                    st.code(result_preview, language="python")


# ═══════════════════════════════════════════════════════════════════════════════
# Main page
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("# 🛠️ App Editor")
st.caption(
    "Chat to make changes to the app. "
    "Claude reads and edits Python files directly. "
    "**Restart the app after changes** (Streamlit reruns automatically if you're running locally)."
)

if not ANTHROPIC_API_KEY:
    st.error("ANTHROPIC_API_KEY is not configured. Add it to .env or Streamlit secrets.")
    st.stop()

# ── Clear chat ────────────────────────────────────────────────────────────────
col_title, col_clear = st.columns([5, 1])
with col_clear:
    if st.button("🗑 Clear", use_container_width=True, help="Clear chat history"):
        st.session_state.app_chat  = []
        st.session_state.app_usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        st.rerun()

st.divider()

# ── Chat history ──────────────────────────────────────────────────────────────
chat_box = st.container(height=500)
with chat_box:
    if not st.session_state.app_chat:
        st.markdown(
            "**Examples:**\n"
            "- *Add a new column to the Fisher income statement table*\n"
            "- *The gravity score shows wrong stars — fix the score coercion*\n"
            "- *Add ROE to the EODHD data sheet profitability section*\n"
            "- *Show me what fields are in the Overview PDF summary page*\n"
            "- *Rename the 'Net Fin. Debt' label to 'Net Debt' in the Fisher report*"
        )

    for msg in st.session_state.app_chat:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("tool_calls"):
                _render_tool_calls(msg["tool_calls"])

# ── Usage bar ─────────────────────────────────────────────────────────────────
_usage_bar()

# ── Input ─────────────────────────────────────────────────────────────────────
user_input = st.chat_input("Describe the change you want…")
if user_input:
    st.session_state.app_chat.append({"role": "user", "content": user_input})

    with st.spinner("Working…"):
        final_text, tool_calls = _run_agent(st.session_state.app_chat)

    st.session_state.app_chat.append({
        "role":       "assistant",
        "content":    final_text or "Done.",
        "tool_calls": tool_calls,
    })
    st.rerun()
