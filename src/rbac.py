"""Role-based access control. Two modes:

  1. **Casbin** (when `pip install casbin` is available + the model file
     `config/rbac_model.conf` exists): full policy evaluation engine with
     deny-overrides effect, keyMatch2 globs, supports `g`/`g2` role
     hierarchies if you extend the model.
  2. **Fallback** (in-process matcher): first-match-wins over the CSV.
     Identical semantics for the simple wildcards used in `config/rbac.csv`.

The model + policy file ship together; either mode works against the
same `config/rbac.csv`.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

from src.config import PROJECT_ROOT

DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "rbac.csv"
DEFAULT_MODEL_PATH = PROJECT_ROOT / "config" / "rbac_model.conf"


_casbin_enforcer = None  # lazy-initialised when casbin is available

def _try_load_casbin():
    """Return a casbin Enforcer or None if casbin / model file missing."""
    global _casbin_enforcer
    if _casbin_enforcer is not None:
        return _casbin_enforcer
    try:
        import casbin  # type: ignore
    except ImportError:
        return None
    if not DEFAULT_MODEL_PATH.exists() or not DEFAULT_POLICY_PATH.exists():
        return None
    try:
        _casbin_enforcer = casbin.Enforcer(str(DEFAULT_MODEL_PATH), str(DEFAULT_POLICY_PATH))
        return _casbin_enforcer
    except Exception:
        return None


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
    reason: str, engine: 'casbin' | 'fallback'}.

    First tries Casbin (real policy engine with deny-overrides). Falls back
    to in-process first-match-wins glob matcher when casbin isn't installed.

    `user_groups`: the user's group memberships from OIDC claims.
    `policy_op`: the tool's policy_op string, e.g. 'sheets.read'.
    """
    enforcer = _try_load_casbin() if policy_path is None else None
    if enforcer is not None:
        effective_groups = list(user_groups) + ["*"]
        for g in effective_groups:
            try:
                if enforcer.enforce(g, policy_op):
                    return {"allowed": True, "matched_rule": (g, policy_op, "allow"),
                            "reason": f"casbin: {g} → allow", "engine": "casbin"}
            except Exception:
                continue
        return {"allowed": False, "matched_rule": None,
                "reason": "casbin: no allow rule matched", "engine": "casbin"}

    # Fallback path — first-match-wins over the CSV.
    rules = _load_policy(policy_path)
    effective_groups = list(user_groups) + ["*"]
    for subj, action, effect in rules:
        if subj not in effective_groups and subj != "*":
            continue
        if not _matches(action, policy_op):
            continue
        return {
            "allowed": effect == "allow",
            "matched_rule": (subj, action, effect),
            "reason": f"fallback rule {subj!r}/{action!r} → {effect}",
            "engine": "fallback",
        }
    return {"allowed": False, "matched_rule": None,
            "reason": "fallback: no matching rule (default-deny)",
            "engine": "fallback"}
