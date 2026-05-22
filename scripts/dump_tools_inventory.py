"""One-shot generator: walk the tool registry and emit docs/TOOLS_INVENTORY.md.

Usage:
    uv run python scripts/dump_tools_inventory.py

The output is a single markdown file with:
  - top-level header + counts per category
  - one section per category, tools sorted alphabetically
  - each tool: name, description, params table (name | type | required)
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

# Allow running directly: `python scripts/dump_tools_inventory.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.registry import TOOLS


CATEGORY_TITLES = {
    "aliases": "Aliases (local name → account)",
    "analytics": "Analytics",
    "apps": "Apps Script",
    "auth": "Auth / Accounts",
    "bank": "Bank statement parsers",
    "browser": "Browser automation (Playwright)",
    "bulk": "Bulk payloads",
    "calendar": "Google Calendar",
    "chats": "Chat history",
    "cloud": "Cloud Logging",
    "contacts": "Google Contacts (People API)",
    "docs": "Google Docs",
    "drive": "Google Drive",
    "excel": "Excel (.xlsx local)",
    "files": "File analyze / extract",
    "forms": "Google Forms",
    "fx": "Currency / FX",
    "gcp": "GCP project management",
    "gmail": "Gmail",
    "local": "Local filesystem",
    "notes": "Agent notes (persistent memory)",
    "open": "Open external app",
    "pdf": "PDF generation",
    "reply": "Reply lint",
    "report": "Reports",
    "self": "Self-heal / introspection",
    "sheets": "Google Sheets",
    "slides": "Google Slides",
    "tasks": "Google Tasks",
    "translate": "Translation",
    "verify": "Claim verification",
    "vision": "Vision (image analysis)",
    "watcher": "Drive watcher",
    "wb": "Wildberries (WB)",
    "web": "Web fetch",
}


def _format_type(prop: dict) -> str:
    """Render a JSON-schema property's type compactly."""
    if "oneOf" in prop:
        kinds = []
        for o in prop["oneOf"]:
            t = o.get("type")
            if t == "array":
                inner = o.get("items", {}).get("type", "?")
                kinds.append(f"array<{inner}>")
            elif t:
                kinds.append(t)
        return " \\| ".join(kinds) if kinds else "any"
    t = prop.get("type", "any")
    if t == "array":
        inner = prop.get("items", {}).get("type", "?")
        return f"array<{inner}>"
    if t == "object":
        return "object"
    return t


def _format_params(schema: dict) -> str:
    """Return a markdown table for the params, or 'No parameters.' if empty."""
    props = schema.get("properties", {}) or {}
    if not props:
        return "_No parameters._"
    required = set(schema.get("required", []) or [])
    rows = ["| Param | Type | Required | Description |", "|---|---|---|---|"]
    for name in sorted(props.keys()):
        prop = props[name]
        typ = _format_type(prop)
        req = "yes" if name in required else "no"
        desc = (prop.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 160:
            desc = desc[:157] + "..."
        # escape pipes inside description
        desc = desc.replace("|", "\\|")
        rows.append(f"| `{name}` | {typ} | {req} | {desc} |")
    return "\n".join(rows)


def _normalize_description(text: str) -> str:
    """Collapse internal whitespace but keep paragraph breaks."""
    return "\n\n".join(
        " ".join(part.split()) for part in text.split("\n\n") if part.strip()
    )


def main() -> int:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for tool in TOOLS:
        by_cat[tool["category"]].append(tool)
    for tools in by_cat.values():
        tools.sort(key=lambda t: t["name"])

    out_lines: list[str] = []
    out_lines.append("# Workspace Agent — Tools Inventory")
    out_lines.append("")
    out_lines.append(
        f"Auto-generated from `src/tools/registry.py`. "
        f"**{len(TOOLS)} tools** across **{len(by_cat)} categories**."
    )
    out_lines.append("")
    out_lines.append(
        "Each tool is exposed to Claude as `mcp__gworkagent__<name>`. "
        "Tools marked with an `account` param accept a Google account alias "
        "(`main` by default); some Drive tools also accept `\"*\"` or a list "
        "of aliases for multi-account fan-out."
    )
    out_lines.append("")
    out_lines.append("## Categories")
    out_lines.append("")
    for cat in sorted(by_cat):
        title = CATEGORY_TITLES.get(cat, cat.title())
        anchor = cat.lower().replace(" ", "-")
        out_lines.append(f"- [{title}](#{anchor}) — {len(by_cat[cat])} tools")
    out_lines.append("")
    out_lines.append("---")
    out_lines.append("")

    for cat in sorted(by_cat):
        title = CATEGORY_TITLES.get(cat, cat.title())
        out_lines.append(f"## {cat}")
        out_lines.append("")
        out_lines.append(f"_{title}_ — {len(by_cat[cat])} tools.")
        out_lines.append("")
        for tool in by_cat[cat]:
            name = tool["name"]
            schema = tool["schema"]
            description = _normalize_description(schema.get("description", "") or "")
            params_md = _format_params(schema.get("input_schema", {}))
            policy_op = tool["policy_op"]
            out_lines.append(f"### `{name}`")
            out_lines.append("")
            out_lines.append(f"_Policy op:_ `{policy_op}`")
            out_lines.append("")
            if description:
                out_lines.append(description)
                out_lines.append("")
            out_lines.append(params_md)
            out_lines.append("")
        out_lines.append("---")
        out_lines.append("")

    out_path = Path(__file__).resolve().parent.parent / "docs" / "TOOLS_INVENTORY.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"Wrote {out_path} — {len(TOOLS)} tools, {len(by_cat)} categories.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
