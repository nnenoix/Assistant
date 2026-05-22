"""Google Tasks tools.

Requires OAuth scope `tasks` (already in config.SCOPES) and GCP project
must have `tasks.googleapis.com` enabled.
"""
from functools import lru_cache

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build(
        "tasks", "v1",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def list_lists(account: str = DEFAULT_ACCOUNT) -> dict:
    """List all task lists. Returns {lists, _meta}. Each list has id, title."""
    resp = _service(account).tasklists().list(maxResults=100).execute()
    items = resp.get("items", []) or []
    return {
        "lists": [{"id": l["id"], "title": l["title"]} for l in items],
        "_meta": {
            "count": len(items),
            "empty_reason": None if items else "no_lists",
        },
    }


def create_list(title: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Create a new task list. Returns {list_id, title}."""
    resp = _service(account).tasklists().insert(body={"title": title}).execute()
    return {"list_id": resp["id"], "title": resp["title"]}


def list_tasks(
    list_id: str,
    show_completed: bool = False,
    due_min: str | None = None,
    due_max: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """List tasks in a list. By default hides completed tasks.

    `due_min`/`due_max` accept RFC3339 timestamps.
    Returns {tasks, _meta}.
    """
    kwargs = {"tasklist": list_id, "maxResults": 100, "showCompleted": show_completed}
    if due_min:
        kwargs["dueMin"] = due_min
    if due_max:
        kwargs["dueMax"] = due_max
    resp = _service(account).tasks().list(**kwargs).execute()
    items = resp.get("items", []) or []
    out = []
    for t in items:
        out.append({
            "id": t["id"],
            "title": t.get("title"),
            "notes": t.get("notes"),
            "due": t.get("due"),
            "status": t.get("status"),
            "completed": t.get("completed"),
            "updated": t.get("updated"),
        })
    return {
        "tasks": out,
        "_meta": {
            "list_id": list_id,
            "count": len(out),
            "show_completed": show_completed,
            "empty_reason": None if out else "no_tasks",
        },
    }


def create(
    list_id: str,
    title: str,
    notes: str | None = None,
    due: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new task. `due` accepts 'YYYY-MM-DD' or RFC3339. Returns
    {task_id, title, due}.
    """
    body: dict = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        # Google Tasks API requires RFC3339 with explicit zone
        if "T" not in due:
            due = f"{due}T00:00:00Z"
        body["due"] = due
    resp = _service(account).tasks().insert(tasklist=list_id, body=body).execute()
    return {
        "task_id": resp["id"],
        "title": resp.get("title"),
        "due": resp.get("due"),
    }


def complete(task_id: str, list_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Mark a task as completed."""
    resp = _service(account).tasks().patch(
        tasklist=list_id, task=task_id, body={"status": "completed"},
    ).execute()
    return {"ok": True, "task_id": task_id, "status": resp.get("status")}


def uncomplete(task_id: str, list_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Mark a completed task back as needsAction."""
    resp = _service(account).tasks().patch(
        tasklist=list_id, task=task_id, body={"status": "needsAction", "completed": None},
    ).execute()
    return {"ok": True, "task_id": task_id, "status": resp.get("status")}


def delete(task_id: str, list_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Permanently delete a task."""
    _service(account).tasks().delete(tasklist=list_id, task=task_id).execute()
    return {"ok": True, "task_id": task_id}
