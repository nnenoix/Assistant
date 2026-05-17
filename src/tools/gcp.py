"""Google Cloud Platform helpers — enable APIs, list projects, get project numbers.

Uses Service Usage API and Cloud Resource Manager API. Both require the
`cloud-platform` scope on the OAuth token AND project-level IAM role
(roles/serviceusage.admin to enable APIs; roles/viewer to list).

The `cloud-platform` scope alone is NOT enough — Google's IAM additionally
gates each call on the calling principal's role in the project. If the
calling user isn't a project member, expect 403 "Permission denied" even
with the scope. Grant via Cloud Console → IAM & Admin.
"""
from functools import lru_cache

from googleapiclient.discovery import build

from src.auth import get_credentials


DEFAULT_ACCOUNT = "main"
DEFAULT_PROJECT_NUMBER = "148389149001"  # Our OAuth client's GCP project


@lru_cache(maxsize=8)
def _serviceusage(account: str = DEFAULT_ACCOUNT):
    return build("serviceusage", "v1", credentials=get_credentials(account), cache_discovery=False)


@lru_cache(maxsize=8)
def _resourcemanager(account: str = DEFAULT_ACCOUNT):
    return build("cloudresourcemanager", "v1", credentials=get_credentials(account), cache_discovery=False)


def enable_api(
    api_name: str,
    project_number: str = DEFAULT_PROJECT_NUMBER,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Enable a Google Cloud API in `project_number`. `api_name` is the
    service hostname, e.g. 'driveactivity.googleapis.com', 'logging.googleapis.com',
    'sheets.googleapis.com', 'script.googleapis.com'. Idempotent — enabling
    an already-enabled API is a no-op.

    Replaces the "click Enable in Cloud Console" step. Returns the operation
    status or final state.
    """
    name = f"projects/{project_number}/services/{api_name}"
    svc = _serviceusage(account)
    op = svc.services().enable(name=name).execute()
    return {
        "api": api_name,
        "project_number": project_number,
        "operation": op.get("name"),
        "done": op.get("done", False),
        "raw": op,
    }


def list_enabled_apis(
    project_number: str = DEFAULT_PROJECT_NUMBER,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """List APIs currently enabled in `project_number`. Returns
    {project, count, apis: [<short_name>...]}."""
    svc = _serviceusage(account)
    parent = f"projects/{project_number}"
    apis: list[str] = []
    page = None
    while True:
        params = dict(parent=parent, filter="state:ENABLED", pageSize=200)
        if page:
            params["pageToken"] = page
        r = svc.services().list(**params).execute()
        for s in r.get("services", []):
            cfg = s.get("config", {})
            apis.append(cfg.get("name", s.get("name", "")))
        page = r.get("nextPageToken")
        if not page:
            break
    return {"project_number": project_number, "count": len(apis), "apis": sorted(apis)}


def list_projects(account: str = DEFAULT_ACCOUNT) -> dict:
    """List all GCP projects the calling account has access to. Returns
    [{project_id, project_number, name, state}].
    """
    svc = _resourcemanager(account)
    out: list[dict] = []
    page = None
    while True:
        params = {}
        if page:
            params["pageToken"] = page
        r = svc.projects().list(**params).execute()
        for p in r.get("projects", []):
            out.append({
                "project_id": p.get("projectId"),
                "project_number": p.get("projectNumber"),
                "name": p.get("name"),
                "state": p.get("lifecycleState"),
            })
        page = r.get("nextPageToken")
        if not page:
            break
    return {"count": len(out), "projects": out}


def project_number(
    project_id: str,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Look up the numeric project number for a project_id. Numbers are the
    stable internal identifier; project_id is the human-readable string.
    Returns {project_id, project_number, name}.
    """
    svc = _resourcemanager(account)
    p = svc.projects().get(projectId=project_id).execute()
    return {
        "project_id": p.get("projectId"),
        "project_number": p.get("projectNumber"),
        "name": p.get("name"),
    }
