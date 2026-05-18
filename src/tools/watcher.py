"""Watcher: detect Apps Script failures from Cloud Logging + alert queue.

Two layers:
  1. Stateless detector — `recent_failures(script_id, since_minutes)` queries
     Cloud Logging for ERROR/CRITICAL entries + specific Logger.log patterns
     that indicate WB rate-limit aborts, 429s, SyntaxErrors. Returns a list
     of structured failure records.
  2. Stateful watcher — `poll_known_scripts()` runs over the bound-script
     registry + the Mylib library, appends NEW failures (not yet seen in
     `.data/alerts.json`) to the alerts queue. Idempotent — calling it
     twice in a row produces no duplicates.

Requires:
  - `cloud-platform` OAuth scope (already in config.SCOPES)
  - Cloud Logging API enabled (Elena has Viewer role on the project)
  - The script's GCP project = our project (use browser_set_script_gcp_project
    to migrate scripts whose logs aren't in Cloud Logging yet)
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from src.config import DATA_DIR
from src.tools import cloud_logging


ALERTS_PATH = DATA_DIR / "alerts.json"
ALERTS_SEEN_PATH = DATA_DIR / "alerts_seen.json"

# Patterns we surface to the user. Each tuple = (severity, regex, label)
FAILURE_PATTERNS = [
    ("error", re.compile(r"\bExceptions?:", re.I), "Apps Script Exception"),
    ("error", re.compile(r"\bSyntaxError\b"), "SyntaxError"),
    ("warn",  re.compile(r"🛑 WB просит ждать (\d+)с"), "WB rate-limit abort"),
    ("warn",  re.compile(r"Код\s*(?:ошибки)?\s*[:=]\s*429"), "WB 429"),
    ("error", re.compile(r"\bTypeError\b"), "TypeError"),
    ("error", re.compile(r"\bReferenceError\b"), "ReferenceError"),
    ("warn",  re.compile(r"❌"), "Error log emoji"),
]


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat() + "Z"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _classify(message: str, severity: str) -> dict | None:
    """Return {kind, label, severity} if the message matches a known failure
    pattern, else None.

    The Cloud Logging severity is informative but not authoritative — we ALSO
    grep for textual patterns since Apps Script's Logger.log emits at INFO
    level even for fatal errors.
    """
    for default_sev, pat, label in FAILURE_PATTERNS:
        m = pat.search(message or "")
        if m:
            return {"kind": label, "severity": severity or default_sev, "match": m.group(0)[:80]}
    if severity in ("ERROR", "CRITICAL"):
        return {"kind": "generic_error", "severity": severity, "match": (message or "")[:80]}
    return None


def recent_failures(
    script_id: str,
    since_minutes: int = 60,
    account: str = "main",
) -> dict:
    """Detect failure-pattern Logger.log entries + structured errors for a
    given Apps Script. Returns {script_id, since_minutes, failures: [...]}.

    Each failure record:
      {timestamp, function_name, execution_id, severity, kind, label, message}
    """
    filter_expr = (
        f'resource.type="app_script_function" AND '
        f'resource.labels.script_id="{script_id}"'
    )
    logs = cloud_logging.read_logs(
        filter_expr=filter_expr,
        minutes_back=since_minutes,
        page_size=500,
        account=account,
    )

    failures: list[dict] = []
    for entry in logs.get("entries", []):
        msg = str(entry.get("message", "") or "")
        cl = _classify(msg, entry.get("severity"))
        if cl is None:
            continue
        labels = entry.get("resource_labels") or {}
        op = entry.get("operation") or {}
        failures.append({
            "timestamp": entry.get("timestamp"),
            "function_name": labels.get("function_name"),
            "execution_id": op.get("id"),
            "severity": entry.get("severity"),
            "kind": cl["kind"],
            "match_preview": cl["match"],
            "message": msg[:300],
        })

    return {
        "script_id": script_id,
        "since_minutes": since_minutes,
        "failures_count": len(failures),
        "failures": failures,
    }


def _alert_id(failure: dict, script_id: str) -> str:
    """Stable hash so we don't re-notify the same execution."""
    # execution_id alone isn't enough — one execution can produce multiple log
    # lines. (script_id, execution_id, timestamp, kind) makes a good fingerprint.
    keys = (
        script_id,
        failure.get("execution_id") or "",
        failure.get("timestamp") or "",
        failure.get("kind") or "",
    )
    return "|".join(str(k) for k in keys)


def list_alerts(unread_only: bool = False, limit: int = 50) -> dict:
    """Return the alerts queue. Newest first. If `unread_only`, hides items
    marked as `read=True`.
    """
    alerts = _load_json(ALERTS_PATH, [])
    if unread_only:
        alerts = [a for a in alerts if not a.get("read")]
    return {"count": len(alerts), "alerts": alerts[:limit]}


def mark_alerts_read(alert_ids: list[str] | None = None) -> dict:
    """Mark alert(s) as read. If `alert_ids` is None, marks ALL as read.
    Returns {marked, total}.
    """
    alerts = _load_json(ALERTS_PATH, [])
    marked = 0
    for a in alerts:
        if a.get("read"):
            continue
        if alert_ids is None or a.get("id") in alert_ids:
            a["read"] = True
            a["read_at"] = _now_iso()
            marked += 1
    _save_json(ALERTS_PATH, alerts)
    return {"marked": marked, "total": len(alerts)}


def clear_alerts(read_only: bool = True) -> dict:
    """Remove alerts from the queue. If `read_only`, keeps unread items."""
    alerts = _load_json(ALERTS_PATH, [])
    if read_only:
        kept = [a for a in alerts if not a.get("read")]
    else:
        kept = []
    _save_json(ALERTS_PATH, kept)
    return {"removed": len(alerts) - len(kept), "remaining": len(kept)}


def _known_scripts() -> list[dict]:
    """All script IDs we monitor: Mylib + everything in the bound-script
    registry. Returns [{script_id, label}].
    """
    from src.tools import apps_script_api
    out: list[dict] = []
    # Bound scripts the agent has learned about
    reg = apps_script_api._bound_registry_load()
    for sheet_id, entry in reg.items():
        out.append({
            "script_id": entry["script_id"],
            "label": f"bound:{sheet_id[:12]}…",
        })
    # Hard-coded Mylib (library) — always monitor
    MYLIB = "1iH0_Wcgn_Y8xQMvOinaVremt-e_Axmq6gDN1Dxx-ILROxv8PVQXDxKlN"
    if not any(s["script_id"] == MYLIB for s in out):
        out.append({"script_id": MYLIB, "label": "Mylib"})
    return out


def poll_known_scripts(
    since_minutes: int = 30,
    account: str = "main",
) -> dict:
    """Check all monitored scripts for new failures. Appends new ones (not
    yet seen) to .data/alerts.json. Idempotent across calls.

    Returns {checked, new_alerts, total_failures_seen, alerts_added: [...]}.
    """
    seen: list[str] = _load_json(ALERTS_SEEN_PATH, [])
    seen_set = set(seen)
    alerts = _load_json(ALERTS_PATH, [])

    checked = 0
    added: list[dict] = []
    total_failures = 0
    errors: list[dict] = []

    for script in _known_scripts():
        checked += 1
        sid = script["script_id"]
        try:
            r = recent_failures(sid, since_minutes=since_minutes, account=account)
        except Exception as e:
            errors.append({"script_id": sid, "error": f"{type(e).__name__}: {str(e)[:160]}"})
            continue

        for f in r["failures"]:
            total_failures += 1
            aid = _alert_id(f, sid)
            if aid in seen_set:
                continue
            seen_set.add(aid)
            alert = {
                "id": aid,
                "created_at": _now_iso(),
                "script_id": sid,
                "script_label": script["label"],
                "function": f.get("function_name"),
                "kind": f.get("kind"),
                "severity": f.get("severity"),
                "timestamp": f.get("timestamp"),
                "preview": f.get("match_preview"),
                "message": f.get("message"),
                "read": False,
            }
            alerts.append(alert)
            added.append(alert)

    # Keep last N alerts to avoid unbounded growth
    if len(alerts) > 500:
        alerts = sorted(alerts, key=lambda a: a.get("created_at",""), reverse=True)[:500]

    _save_json(ALERTS_PATH, alerts)
    _save_json(ALERTS_SEEN_PATH, list(seen_set))

    return {
        "checked_scripts": checked,
        "total_failures_seen": total_failures,
        "new_alerts": len(added),
        "alerts_added": added,
        "errors": errors,
        "next_check_in_min": since_minutes,
    }
