import json
from pathlib import Path
from typing import Any


class Policy:
    def __init__(self, rules: dict[str, dict[str, list[str] | str]]):
        self._rules = rules

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
            target = args.get("path", "")
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
        return False
