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

import re

from src.config import DATA_DIR
from src.json_store import now_iso_z, read_json, write_json
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
    script_id: str | None = None,
    since_minutes: int = 60,
    account: str = "main",
    function_filter: list[str] | None = None,
) -> dict:
    """Detect failure-pattern Logger.log entries + structured errors.

    Note (May 2026): Apps Script Cloud Logging no longer emits
    `resource.labels.script_id` — only `function_name` + `project_id`. So
    `script_id` is now used only as a label in the returned records; the
    actual filter scans ALL Apps Script logs in our GCP project and
    optionally narrows to specific function names.

    Returns {script_id, since_minutes, failures: [...]}.
    Each failure: {timestamp, function_name, execution_id, severity, kind,
    label, message}.
    """
    # Build filter — function names if given, else all app_script_function logs
    parts = ['resource.type="app_script_function"']
    if function_filter:
        clauses = " OR ".join(
            f'resource.labels.function_name="{fn}"' for fn in function_filter
        )
        parts.append(f"({clauses})")
    filter_expr = " AND ".join(parts)
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
    alerts = read_json(ALERTS_PATH, [])
    if unread_only:
        alerts = [a for a in alerts if not a.get("read")]
    return {"count": len(alerts), "alerts": alerts[:limit]}


def mark_alerts_read(alert_ids: list[str] | None = None) -> dict:
    """Mark alert(s) as read. If `alert_ids` is None, marks ALL as read.
    Returns {marked, total}.
    """
    alerts = read_json(ALERTS_PATH, [])
    marked = 0
    for a in alerts:
        if a.get("read"):
            continue
        if alert_ids is None or a.get("id") in alert_ids:
            a["read"] = True
            a["read_at"] = now_iso_z()
            marked += 1
    write_json(ALERTS_PATH, alerts)
    return {"marked": marked, "total": len(alerts)}


def clear_alerts(read_only: bool = True) -> dict:
    """Remove alerts from the queue. If `read_only`, keeps unread items."""
    alerts = read_json(ALERTS_PATH, [])
    if read_only:
        kept = [a for a in alerts if not a.get("read")]
    else:
        kept = []
    write_json(ALERTS_PATH, kept)
    return {"removed": len(alerts) - len(kept), "remaining": len(kept)}


MYLIB_SCRIPT_ID = "1iH0_Wcgn_Y8xQMvOinaVremt-e_Axmq6gDN1Dxx-ILROxv8PVQXDxKlN"


def _known_scripts() -> list[dict]:
    """All script IDs we monitor: Mylib + everything in the bound-script
    registry. Returns [{script_id, label}].
    """
    from src.tools import apps_script_api
    out: list[dict] = []
    for sheet_id, entry in apps_script_api.list_bound_scripts().items():
        out.append({
            "script_id": entry["script_id"],
            "label": f"bound:{sheet_id[:12]}…",
        })
    if not any(s["script_id"] == MYLIB_SCRIPT_ID for s in out):
        out.append({"script_id": MYLIB_SCRIPT_ID, "label": "Mylib"})
    return out


def poll_known_scripts(
    since_minutes: int = 30,
    account: str = "main",
) -> dict:
    """Scan ALL Apps Script logs in our GCP project for failures and append
    new ones to .data/alerts.json. Idempotent.

    Since Cloud Logging no longer emits `script_id` in labels, we do a single
    project-wide scan and tag each alert with whatever info IS available
    (function name + the first known-script's label as a best-effort hint).

    Returns {checked, new_alerts, total_failures_seen, alerts_added: [...]}.
    """
    seen: list[str] = read_json(ALERTS_SEEN_PATH, [])
    seen_set = set(seen)
    alerts = read_json(ALERTS_PATH, [])

    known = _known_scripts()
    errors: list[dict] = []
    added: list[dict] = []

    try:
        r = recent_failures(since_minutes=since_minutes, account=account)
    except Exception as e:
        from src.tools._errors import _classify_exception
        kind, status = _classify_exception(e)
        errors.append({
            "step": "recent_failures",
            "kind": type(e).__name__,
            "error_kind": kind,
            "http_status": status,
            "message": str(e)[:200],
        })
        return {
            "checked_scripts": len(known),
            "total_failures_seen": 0,
            "new_alerts": 0,
            "alerts_added": [],
            "errors": errors,
            "next_check_in_min": since_minutes,
            "_meta": {"error_kind": kind, "http_status": status},
        }

    for f in r["failures"]:
        # No script_id in modern Apps Script logs — best-effort label by function
        fname = f.get("function_name") or "?"
        label = f"function {fname}"
        aid = _alert_id(f, label)
        if aid in seen_set:
            continue
        seen_set.add(aid)
        alert = {
            "id": aid,
            "created_at": now_iso_z(),
            "script_id": None,  # Cloud Logging doesn't expose it anymore
            "script_label": label,
            "function": fname,
            "kind": f.get("kind"),
            "severity": f.get("severity"),
            "timestamp": f.get("timestamp"),
            "preview": f.get("match_preview"),
            "message": f.get("message"),
            "read": False,
        }
        alerts.append(alert)
        added.append(alert)

    if len(alerts) > 500:
        alerts = sorted(alerts, key=lambda a: a.get("created_at",""), reverse=True)[:500]

    # Prune seen_set to the IDs of kept alerts. Older fingerprints can't
    # re-trigger because Cloud Logging only goes back `since_minutes` (≤ a
    # few hours in practice); without this, seen_set grows forever.
    write_json(ALERTS_PATH, alerts)
    write_json(ALERTS_SEEN_PATH, [a["id"] for a in alerts])

    return {
        "checked_scripts": len(known),
        "total_failures_seen": r["failures_count"],
        "new_alerts": len(added),
        "alerts_added": added,
        "errors": errors,
        "next_check_in_min": since_minutes,
    }
