"""Cloud Logging client — read Apps Script Logger.log output without browser.

Apps Script's `Logger.log` calls go to Stackdriver/Cloud Logging when the
script is linked to a real GCP project (not Google's default hidden one).
Reading these via API gives us full execution traces without Playwright
scraping the Apps Script editor.

Requires:
  - `cloud-platform` OAuth scope (or `logging.read`)
  - The script's GCP project = our project (use browser_set_script_gcp_project)
  - Cloud Logging API enabled in our GCP project
  - The calling user has `roles/logging.viewer` (or higher) on the GCP project
    — `cloud-platform` SCOPE alone isn't enough, project-level IAM is required.
    Grant via Cloud Console → IAM & Admin → Add → <email> → 'Logs Viewer'.
"""
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials
from src.tools.gcp import DEFAULT_PROJECT_NUMBER


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _logging(account: str = DEFAULT_ACCOUNT):
    return build(
        "logging", "v2",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def read_logs(
    filter_expr: str | None = None,
    project_id: str = DEFAULT_PROJECT_NUMBER,
    minutes_back: int = 60,
    page_size: int = 100,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Read recent Cloud Logging entries. `filter_expr` is a Cloud Logging
    advanced filter (see https://cloud.google.com/logging/docs/view/logging-query-language).

    Common filters:
      - 'resource.type="app_script_function"' — Apps Script Logger.log entries
      - 'severity>=ERROR' — anything that failed
      - 'resource.labels.script_id="<id>"' — only one script

    Defaults to last `minutes_back` minutes. Returns
    {entries_count, entries: [{timestamp, severity, message, ...}]}.
    """
    svc = _logging(account)
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
    time_filter = f'timestamp>="{since.isoformat()}"'
    full_filter = f"{filter_expr} AND {time_filter}" if filter_expr else time_filter

    body = {
        "resourceNames": [f"projects/{project_id}"],
        "filter": full_filter,
        "pageSize": page_size,
        "orderBy": "timestamp desc",
    }
    try:
        r = svc.entries().list(body=body).execute()
    except Exception as e:
        msg = str(e)
        if "Permission denied" in msg or "403" in msg:
            raise PermissionError(
                f"Cloud Logging read denied for project {project_id}. "
                f"Account {account!r} needs roles/logging.viewer (or higher) on the project. "
                f"Grant via Cloud Console → IAM & Admin → Add. Underlying: {msg[:200]}"
            ) from e
        raise
    entries = r.get("entries", [])
    out: list[dict] = []
    for e in entries:
        # textPayload / jsonPayload / protoPayload — pick whichever has data
        msg = e.get("textPayload")
        if msg is None:
            jp = e.get("jsonPayload")
            if jp:
                msg = jp.get("message") or str(jp)[:300]
        if msg is None:
            pp = e.get("protoPayload")
            if pp:
                msg = pp.get("status", {}).get("message") or str(pp)[:300]
        out.append({
            "timestamp": e.get("timestamp"),
            "severity": e.get("severity"),
            "resource_type": e.get("resource", {}).get("type"),
            "resource_labels": e.get("resource", {}).get("labels", {}),
            "message": msg,
            "operation": e.get("operation"),
        })
    return {"entries_count": len(out), "entries": out, "filter_used": full_filter}


def script_executions(
    script_id: str,
    minutes_back: int = 60,
    project_id: str = DEFAULT_PROJECT_NUMBER,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """List recent Apps Script function executions for `script_id`. Returns
    {executions: [{function_name, status, started_at, duration_sec}]}.

    Requires the script's GCP project to match `project_id` AND Cloud Logging
    API to be enabled.
    """
    filter_expr = (
        f'resource.type="app_script_function" AND '
        f'resource.labels.script_id="{script_id}"'
    )
    raw = read_logs(filter_expr=filter_expr, project_id=project_id,
                    minutes_back=minutes_back, page_size=500, account=account)

    # Group by execution_id (operation.id from Cloud Logging)
    by_exec: dict[str, dict] = {}
    for e in raw["entries"]:
        op = e.get("operation") or {}
        exec_id = op.get("id")
        if not exec_id:
            continue
        if exec_id not in by_exec:
            by_exec[exec_id] = {
                "execution_id": exec_id,
                "function": e["resource_labels"].get("function_name"),
                "first_entry": e["timestamp"],
                "last_entry": e["timestamp"],
                "log_count": 0,
                "max_severity": e["severity"] or "INFO",
            }
        entry = by_exec[exec_id]
        entry["last_entry"] = min(entry["last_entry"], e["timestamp"])  # last (= earliest in desc order)
        entry["first_entry"] = max(entry["first_entry"], e["timestamp"])
        entry["log_count"] += 1
        # Track most-severe status seen
        sev_order = {"DEBUG": 0, "INFO": 1, "NOTICE": 2, "WARNING": 3, "ERROR": 4, "CRITICAL": 5}
        if sev_order.get(e["severity"] or "INFO", 1) > sev_order.get(entry["max_severity"], 1):
            entry["max_severity"] = e["severity"]

    return {
        "script_id": script_id,
        "executions_count": len(by_exec),
        "executions": list(by_exec.values()),
    }
