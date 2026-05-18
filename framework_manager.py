"""
framework_manager.py — Loads, saves, forks, and manages report framework configs.

Frameworks are stored as JSON files in the frameworks/ directory.
Built-in frameworks (overview, fisher, gravity) ship with the app and are protected
from deletion — editing them creates a fork, keeping the original untouched.

Framework JSON schema:
  id                  Unique slug (e.g. "overview", "my_overview_abc123")
  name                Display name
  icon                Emoji icon
  description         One-line description shown in the UI
  is_builtin          True for the 3 shipped frameworks
  base_id             Source id if forked, else null
  version             Integer, incremented on every save
  created_at / modified_at  ISO-format UTC timestamps
  system_prompt       The LLM "persona" system message
  prompt_template     User prompt template; use {financials}, {currency}, {ticker},
                      {company_name}, {forward_estimates} as placeholders.
                      Built-ins also accept {fisher_questions}, {helmer_questions},
                      {gravity_dimensions}.  Set to "__builtin__" only for the
                      3 shipped frameworks to signal the legacy Python builder is used.
  output_schema       List of field definitions the LLM should return
  report_sections     Ordered list of sections to render in the HTML/PDF report
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FRAMEWORKS_DIR = Path(__file__).parent / "frameworks"
FRAMEWORKS_DIR.mkdir(exist_ok=True)

# Persistent user-defined ordering — saved to data/framework_order.json
_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(exist_ok=True)
ORDER_FILE = _DATA_DIR / "framework_order.json"

# Legacy set kept for backwards compatibility (delete-protection now reads
# is_builtin from each FrameworkConfig directly). Updated to reflect the
# current set of shipped built-ins.
BUILTIN_IDS = frozenset({
    "overview_v2", "fisher", "gravity",
    "eodhd_full", "index_overview",
})


def _load_order() -> list[str]:
    """Return the user-saved framework-id order (empty list if none)."""
    if not ORDER_FILE.exists():
        return []
    try:
        raw = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
        order = raw.get("order", [])
        return [str(x) for x in order if isinstance(x, str)]
    except Exception:
        return []


def _save_order(order: list[str]) -> None:
    """Persist the framework-id order to disk."""
    ORDER_FILE.write_text(
        json.dumps({"order": order}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class FrameworkConfig:
    id: str
    name: str
    icon: str
    description: str
    is_builtin: bool
    version: int
    created_at: str
    modified_at: str
    system_prompt: str
    prompt_template: str        # "__builtin__" for shipped frameworks
    output_schema: list         # list of field dicts
    report_sections: list       # ordered list of section dicts
    base_id: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FrameworkConfig":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def is_forked_builtin(self) -> bool:
        return (not self.is_builtin) and (self.base_id in BUILTIN_IDS)

    @property
    def uses_builtin_runner(self) -> bool:
        """True when this framework should be executed by the legacy Python runner."""
        return self.is_builtin and self.prompt_template == "__builtin__"


# ── Manager ───────────────────────────────────────────────────────────────────

class FrameworkManager:
    """CRUD + import/export for report framework configs."""

    def __init__(self, frameworks_dir: Path = FRAMEWORKS_DIR):
        self.dir = Path(frameworks_dir)
        self.dir.mkdir(exist_ok=True)

    # ── List / Get ────────────────────────────────────────────────────────────

    def list(self) -> list[FrameworkConfig]:
        """
        All frameworks, ordered by:
          1. Persistent user-defined order from data/framework_order.json
             (frameworks listed there appear in that exact sequence)
          2. Frameworks not in the saved order go at the end:
             built-ins first, then user-created — both alphabetical by name.
        """
        configs = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    configs.append(FrameworkConfig.from_dict(json.load(f)))
            except Exception as e:
                logger.warning(f"[FrameworkManager] Could not load {path.name}: {e}")

        saved_order = _load_order()
        order_idx = {fw_id: i for i, fw_id in enumerate(saved_order)}
        BIG = 10**6

        configs.sort(
            key=lambda c: (
                order_idx.get(c.id, BIG),               # 1) saved order wins
                0 if c.is_builtin else 1,               # 2) built-ins first
                c.name.lower(),                          # 3) alphabetical
            )
        )
        return configs

    # ── Ordering helpers ──────────────────────────────────────────────────────

    def get_order(self) -> list[str]:
        """Return current saved order (framework ids) — empty list if none."""
        return _load_order()

    def set_order(self, order: list[str]) -> None:
        """Persist a new framework order. Caller passes the full id list."""
        # Defensive: drop ids that no longer exist on disk
        valid_ids = {p.stem for p in self.dir.glob("*.json")}
        cleaned = [fw_id for fw_id in order if fw_id in valid_ids]
        _save_order(cleaned)
        logger.info(f"[FrameworkManager] Saved new order ({len(cleaned)} ids)")

    def move(self, framework_id: str, direction: int) -> None:
        """
        Move a framework up (-1) or down (+1) by one slot in the saved order.
        If no order is saved yet, initialises it from the current list().
        """
        if direction not in (-1, 1):
            raise ValueError("direction must be -1 (up) or +1 (down)")

        order = _load_order()
        if not order:
            # Initialise order from the current default list
            order = [fw.id for fw in self.list()]

        if framework_id not in order:
            # Framework is new — append, then attempt move.
            order.append(framework_id)

        idx = order.index(framework_id)
        new_idx = idx + direction
        if 0 <= new_idx < len(order):
            order[idx], order[new_idx] = order[new_idx], order[idx]
            self.set_order(order)

    def get(self, framework_id: str) -> Optional[FrameworkConfig]:
        path = self.dir / f"{framework_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return FrameworkConfig.from_dict(json.load(f))
        except Exception as e:
            logger.error(f"[FrameworkManager] Failed to load {framework_id}: {e}")
            return None

    def get_system_prompt(self, framework_id: str, fallback: str = "") -> str:
        """Convenience: return the system prompt, or fallback if not found."""
        fw = self.get(framework_id)
        return fw.system_prompt if fw and fw.system_prompt else fallback

    # ── Save ──────────────────────────────────────────────────────────────────

    def save(self, config: FrameworkConfig) -> None:
        """Write a framework to disk. Bumps version + modified_at automatically."""
        config.modified_at = datetime.utcnow().isoformat()
        config.version = max(config.version + 1, 1)
        path = self.dir / f"{config.id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info(f"[FrameworkManager] Saved '{config.id}' v{config.version}")

    def update_system_prompt(self, framework_id: str, new_prompt: str) -> FrameworkConfig:
        """Update only the system prompt of an existing framework."""
        config = self.get(framework_id)
        if config is None:
            raise ValueError(f"Framework '{framework_id}' not found.")
        if config.is_builtin:
            raise ValueError(
                f"Cannot modify built-in framework '{framework_id}' directly. "
                f"Fork it first with fork()."
            )
        config.system_prompt = new_prompt
        self.save(config)
        return config

    def update_prompt_template(self, framework_id: str, new_template: str) -> FrameworkConfig:
        """Update only the prompt template of an existing framework."""
        config = self.get(framework_id)
        if config is None:
            raise ValueError(f"Framework '{framework_id}' not found.")
        if config.is_builtin:
            raise ValueError(
                f"Cannot modify built-in framework '{framework_id}' directly. "
                f"Fork it first with fork()."
            )
        config.prompt_template = new_template
        self.save(config)
        return config

    # ── Fork ──────────────────────────────────────────────────────────────────

    def fork(self, source_id: str, new_name: str) -> FrameworkConfig:
        """
        Copy source_id to a new user-owned framework.
        The copy is NOT builtin — it's fully editable and deletable.
        prompt_template is inherited; if the source uses '__builtin__',
        the fork gets an empty template string so the Studio can fill it in.
        """
        source = self.get(source_id)
        if source is None:
            raise ValueError(f"Framework '{source_id}' not found.")

        new_id = _slugify(new_name) + "_" + uuid.uuid4().hex[:6]
        now = datetime.utcnow().isoformat()

        # Inherited prompt template — if builtin, give fork a clean editable copy
        inherited_template = source.prompt_template
        if inherited_template == "__builtin__":
            inherited_template = (
                "# This framework was forked from the built-in "
                f"'{source.name}' framework.\n"
                "# Replace this with your custom prompt template.\n"
                "# Use {financials} to inject company financial data,\n"
                "# {currency}, {company_name}, {ticker} for company metadata,\n"
                "# and {forward_estimates} for analyst consensus estimates.\n\n"
                f"Analyse the company below using the {source.name} framework.\n\n"
                "{financials}\n\n"
                "Return a JSON object with the following fields:\n"
                + _schema_to_prompt_hint(source.output_schema)
            )

        forked = FrameworkConfig(
            id=new_id,
            name=new_name,
            icon=source.icon,
            description=source.description,
            is_builtin=False,
            version=0,
            created_at=now,
            modified_at=now,
            system_prompt=source.system_prompt,
            prompt_template=inherited_template,
            output_schema=list(source.output_schema),
            report_sections=list(source.report_sections),
            base_id=source_id,
        )
        self.save(forked)
        logger.info(f"[FrameworkManager] Forked '{source_id}' → '{new_id}' ('{new_name}')")
        return forked

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete(self, framework_id: str) -> None:
        """Delete a user-created framework. Raises if it's a built-in."""
        config = self.get(framework_id)
        if config is None:
            raise ValueError(f"Framework '{framework_id}' not found.")
        if config.is_builtin:
            raise ValueError(
                f"Cannot delete built-in framework '{framework_id}'. "
                f"Built-ins are protected. You can fork and modify them instead."
            )
        path = self.dir / f"{config.id}.json"
        path.unlink(missing_ok=True)
        logger.info(f"[FrameworkManager] Deleted '{framework_id}'")

    # ── Export / Import ───────────────────────────────────────────────────────

    def export_bytes(self, framework_id: str) -> bytes:
        """Return the framework JSON as UTF-8 bytes (for st.download_button)."""
        config = self.get(framework_id)
        if config is None:
            raise ValueError(f"Framework '{framework_id}' not found.")
        return json.dumps(config.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")

    def import_from_bytes(self, data: bytes, force_name: str = "") -> FrameworkConfig:
        """
        Import a framework from raw JSON bytes (e.g. from st.file_uploader).
        Always assigns a fresh id to avoid collisions with existing frameworks.
        The imported framework is always non-builtin.

        Args:
            data:       Raw JSON bytes
            force_name: Override the imported name (optional)
        """
        try:
            raw = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"Invalid JSON in imported file: {e}")

        required = {"name", "system_prompt"}
        missing = required - set(raw.keys())
        if missing:
            raise ValueError(f"Framework JSON is missing required fields: {missing}")

        name = force_name or raw["name"]
        new_id = _slugify(name) + "_" + uuid.uuid4().hex[:6]
        now = datetime.utcnow().isoformat()

        config = FrameworkConfig(
            id=new_id,
            name=name,
            icon=raw.get("icon", "📋"),
            description=raw.get("description", ""),
            is_builtin=False,
            version=0,
            created_at=now,
            modified_at=now,
            system_prompt=raw["system_prompt"],
            prompt_template=raw.get("prompt_template", ""),
            output_schema=raw.get("output_schema", []),
            report_sections=raw.get("report_sections", []),
            base_id=raw.get("base_id"),
        )
        self.save(config)
        logger.info(f"[FrameworkManager] Imported '{name}' as '{new_id}'")
        return config

    # ── Placeholders ──────────────────────────────────────────────────────────

    @staticmethod
    def available_placeholders() -> dict[str, str]:
        """Map of placeholder name → description, for the Studio editor UI."""
        return {
            "{financials}":         "Full multi-year financial data block (income statement, "
                                    "balance sheet ratios, market data)",
            "{forward_estimates}":  "Analyst consensus estimates for the current/next fiscal year",
            "{company_name}":       "Company display name",
            "{ticker}":             "Yahoo Finance ticker symbol",
            "{currency}":           "Reporting currency (e.g. EUR, USD)",
            "{sector}":             "Sector classification",
            "{industry}":           "Industry classification",
            "{country}":            "Country of domicile",
            "{current_price}":      "Last traded price",
            "{market_cap}":         "Market capitalisation in millions",
            "{enterprise_value}":   "Enterprise value in millions",
            "{pe_ratio}":           "Trailing P/E ratio",
            "{forward_pe}":         "Forward P/E ratio",
            "{ev_ebitda}":          "EV/EBITDA multiple",
            "{ev_sales}":           "EV/Sales multiple",
            "{dividend_yield}":     "Dividend yield as a percentage",
            "{fcf_yield}":          "Free cash flow yield as a percentage",
            "{roe}":                "Return on equity (TTM)",
            "{ebit_margin}":        "EBIT margin (TTM)",
            "{net_margin}":         "Net margin (TTM)",
            "{revenue_cagr_3y}":    "3-year revenue CAGR",
            "{revenue_cagr_5y}":    "5-year revenue CAGR",
            "{description}":        "Company business description (up to 800 chars)",
            "{employees}":          "Number of employees",
            "{website}":            "Company website URL",
            "{macro_context}":      "FRED macro snapshot: US rates, inflation, unemployment, credit spreads, USD/EUR (auto-injected if omitted)",
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convert a display name to a safe lowercase filename slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:40]


def _schema_to_prompt_hint(output_schema: list) -> str:
    """Generate a simple JSON template hint from the output_schema list."""
    lines = ["{"]
    for field in output_schema:
        name = field.get("name", "field")
        ftype = field.get("type", "string")
        desc = field.get("description", "")
        if ftype == "list":
            lines.append(f'  "{name}": ["..."],  // {desc}')
        elif ftype in ("integer", "number"):
            lines.append(f'  "{name}": 0,  // {desc}')
        elif ftype == "enum":
            vals = "|".join(field.get("enum_values", []))
            lines.append(f'  "{name}": "{vals}",  // {desc}')
        else:
            lines.append(f'  "{name}": "...",  // {desc}')
    lines.append("}")
    return "\n".join(lines)
