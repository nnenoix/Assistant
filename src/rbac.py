"""Role-based access control (Casbin-backed when available, simple matcher
fallback). Maps {user_groups, tool_policy_op} → allow|deny.

Phase 0 scaffold: ships a static policy file at `config/rbac.csv` with
sensible defaults (finance team can read sheets+gmail but not delete;
ops team can run apps_script but not send gmail; admin can do everything).

Production: hook into Authentik groups + dynamic policies, RBAC + ABAC
(by-tenant scoping). For now the matcher is enough to start enforcing
team boundaries.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

from src.config import PROJECT_ROOT

DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "rbac.csv"


_policy_cache: list[tuple[str, str, str]] = []
_loaded_path: str | None = None


def _load_policy(path: Path | None = None) -> list[tuple[str, str, str]]:
    """Read CSV rows: (subject_glob, action_glob, effect)."""
    global _policy_cache, _loaded_path
    p = path or DEFAULT_POLICY_PATH
    key = str(p)
    if _loaded_path == key:
        return _policy_cache
    if not p.exists():
        # Fallback default policy: admin = *, everyone else = read.*
        _policy_cache = [
            ("admin", "*", "allow"),
            ("*", "*.read", "allow"),
            ("*", "*.search", "allow"),
            ("*", "*.list", "allow"),
            ("*", "*", "deny"),  # default-deny everything not explicitly allowed
        ]
        _loaded_path = key
        return _policy_cache
    rules: list[tuple[str, str, str]] = []
    with open(p, encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith("#"):
                continue
            if len(row) < 3:
                continue
            rules.append((row[0].strip(), row[1].strip(), row[2].strip()))
    _policy_cache = rules
    _loaded_path = key
    return rules


def _matches(pattern: str, value: str) -> bool:
    """Glob-ish match for tool policy_op patterns:
      - `*`        — anything
      - `prefix.*` — anything starting with `prefix.`
      - `*.suffix` — anything ending with `.suffix` (e.g. `*.read` matches `sheets.read`)
      - exact      — equal strings
    """
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return value.startswith(pattern[:-1])
    if pattern.startswith("*."):
        return value.endswith(pattern[1:])
    return pattern == value


def check_permission(user_groups: list[str], policy_op: str,
                     policy_path: Path | None = None) -> dict:
    """Return {allowed: bool, matched_rule: (subj, action, effect) | None,
    reason: str}. First-match-wins; rules are evaluated in file order so
    callers can put deny-overrides at the top.

    `user_groups`: the user's group memberships from OIDC claims.
    `policy_op`: the tool's policy_op string, e.g. 'sheets.read'.
    """
    rules = _load_policy(policy_path)
    # Include implicit "*" group so universal rules apply to everyone.
    effective_groups = list(user_groups) + ["*"]
    for subj, action, effect in rules:
        if subj not in effective_groups and subj != "*":
            continue
        if not _matches(action, policy_op):
            continue
        return {
            "allowed": effect == "allow",
            "matched_rule": (subj, action, effect),
            "reason": f"rule {subj!r}/{action!r} → {effect}",
        }
    return {"allowed": False, "matched_rule": None, "reason": "no matching rule (default-deny)"}
