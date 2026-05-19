import json
from pathlib import Path
from typing import Any


# Built-in defaults merged into the user's allowlist.json on load. The user
# can override any category by listing it in their file — these only fill
# in MISSING categories. Fresh installs (where .data/allowlist.json doesn't
# exist yet) get a sane policy out of the box: reads are wildcard-allowed,
# writes/deletes prompt via approval modal.
_DEFAULTS: dict[str, dict] = {
    "drive":       {"read": "*", "create": [], "update": [], "delete": []},
    "sheets":      {"read": "*", "write": []},
    "local":       {"read": [".", ".data/staging", ".data/uploads"], "write": [".data/staging", ".data/uploads"]},
    "apps_script": {"edit": [], "run": []},
    "auth":        {"list": "*", "add": [], "remove": []},
    "chats":       {"read": "*"},
    "notes":       {"read": "*", "write": "*"},
    "people":      {"read": "*", "write": "*"},
    "gmail":       {"read": "*", "draft": "*", "send": []},
    "calendar":    {"read": "*", "write": "*", "delete": []},
    "self": {
        "read": ["src", "static"],
        "test": "*",
        "diff": "*",
        "edit": [],
        "commit": [],
        "revert": [],
    },
}


def _apply_defaults(rules: dict) -> dict:
    out = dict(rules)
    for cat, actions in _DEFAULTS.items():
        if cat not in out:
            out[cat] = dict(actions)
        else:
            merged = dict(out[cat])
            for action, default_val in actions.items():
                if action not in merged:
                    merged[action] = default_val
            out[cat] = merged
    # Resolve relative paths in self.read + local.read/write against PROJECT_ROOT
    from src.config import PROJECT_ROOT
    for cat, action in (("self", "read"), ("local", "read"), ("local", "write")):
        val = out.get(cat, {}).get(action)
        if isinstance(val, list):
            out[cat][action] = [
                str((PROJECT_ROOT / p).resolve()) if not Path(p).is_absolute() else p
                for p in val
            ]
    return out


class Policy:
    def __init__(self, rules: dict[str, dict[str, list[str] | str]]):
        self._rules = _apply_defaults(rules)

    @classmethod
    def load(cls, path: Path) -> "Policy":
        if not path.exists():
            return cls({})
        return cls(json.loads(path.read_text()))

    def is_allowed(self, operation: str, args: dict[str, Any]) -> bool:
        """operation format: 'category.action' e.g. 'drive.create'."""
        if "." not in operation:
            return False
        category, action = operation.split(".", 1)
        category_rules = self._rules.get(category, {})
        allow = category_rules.get(action, [])

        if allow == "*":
            return True
        if not isinstance(allow, list) or not allow:
            return False

        return self._matches(category, action, args, allow)

    @staticmethod
    def _matches(category: str, action: str, args: dict, allow: list[str]) -> bool:
        if category == "drive":
            if action == "read":
                # Read ops use either file_id (get/download) or folder_id (list); search has no ID.
                return args.get("file_id") in allow or args.get("folder_id") in allow
            key = {"create": "parent_id", "update": "file_id", "delete": "file_id"}.get(action)
            return args.get(key) in allow if key else False
        if category == "sheets":
            return args.get("spreadsheet_id") in allow
        if category == "local":
            # Different local_* tools use different param names — check all
            # the common ones (path, file_path, local_path) so policy doesn't
            # accidentally allow a read that the user meant to gate.
            target = args.get("path") or args.get("file_path") or args.get("local_path") or ""
            if not target:
                return False
            target_norm = Path(target).resolve().as_posix().lower()
            for root in allow:
                root_norm = Path(root).resolve().as_posix().lower()
                if target_norm == root_norm or target_norm.startswith(root_norm + "/"):
                    return True
            return False
        if category == "apps_script":
            return args.get("script_id") in allow
        if category == "self":
            # Self-healing tools work on `path` inside src/ — the tool itself
            # already enforces the src/static boundary, so here we only check
            # the same path-prefix model used for local.*.
            target = args.get("path", "")
            if not target:
                return action == "read"  # status/diff w/o path = ok if read allowed
            target_norm = Path(target).resolve().as_posix().lower()
            for root in allow:
                root_norm = Path(root).resolve().as_posix().lower()
                if target_norm == root_norm or target_norm.startswith(root_norm + "/"):
                    return True
            return False
        return False
