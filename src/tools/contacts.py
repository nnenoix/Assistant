"""Google Contacts tools (People API).

NAMESPACE NOTE: src/tools/people.py is an internal name→account alias
registry — these `contacts_*` tools are the real Google Contacts via
People API. They live side-by-side without collision.

Requires OAuth scopes `contacts.readonly` (read) and/or `contacts`
(write). GCP project must have `people.googleapis.com` enabled.
"""
from functools import lru_cache

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials


DEFAULT_ACCOUNT = "main"

_DEFAULT_FIELDS = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays,biographies,memberships"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build(
        "people", "v1",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def _flatten_person(p: dict) -> dict:
    """Squash a People API resource into a flat dict for the agent."""
    names = p.get("names", []) or []
    primary_name = names[0] if names else {}
    return {
        "resource_name": p.get("resourceName"),
        "display_name": primary_name.get("displayName"),
        "given_name": primary_name.get("givenName"),
        "family_name": primary_name.get("familyName"),
        "emails": [e.get("value") for e in (p.get("emailAddresses") or [])],
        "phones": [ph.get("value") for ph in (p.get("phoneNumbers") or [])],
        "organizations": [
            {"name": o.get("name"), "title": o.get("title")}
            for o in (p.get("organizations") or [])
        ],
        "etag": p.get("etag"),
    }


def search(query: str, max_results: int = 10, account: str = DEFAULT_ACCOUNT) -> dict:
    """Search the user's contacts. Returns {contacts, _meta}.

    People API requires the search index to be warm — if you get empty
    results unexpectedly, call list() once first to prime it.
    """
    # `otherContacts.search` and `people.search` exist; `people.searchContacts`
    # is the modern endpoint (Q1 2024+).
    resp = _service(account).people().searchContacts(
        query=query,
        pageSize=min(max(max_results, 1), 30),
        readMask=_DEFAULT_FIELDS,
    ).execute()
    raw = resp.get("results", []) or []
    contacts = [_flatten_person(r.get("person", {})) for r in raw]
    return {
        "contacts": contacts,
        "_meta": {
            "query": query,
            "count": len(contacts),
            "empty_reason": None if contacts else "no_matches",
        },
    }


def get(resource_name: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Get full details for one contact by resource_name (e.g. 'people/c12345').

    Returns the flattened person dict + _meta.
    """
    resp = _service(account).people().get(
        resourceName=resource_name,
        personFields=_DEFAULT_FIELDS,
    ).execute()
    return {**_flatten_person(resp), "_meta": {"resource_name": resource_name}}


def list_all(
    max_results: int = 100,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """List all contacts. Paginated by Google internally; returns up to
    `max_results` (capped at 1000 per page).
    """
    resp = _service(account).people().connections().list(
        resourceName="people/me",
        pageSize=min(max(max_results, 1), 1000),
        personFields=_DEFAULT_FIELDS,
    ).execute()
    raw = resp.get("connections", []) or []
    contacts = [_flatten_person(p) for p in raw]
    return {
        "contacts": contacts,
        "_meta": {
            "returned_count": len(contacts),
            "truncated": bool(resp.get("nextPageToken")),
            "truncation_reason": (
                "more results — pass higher max_results or paginate"
                if resp.get("nextPageToken") else None
            ),
            "empty_reason": None if contacts else "no_contacts",
        },
    }


def create(
    given_name: str,
    family_name: str | None = None,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    organization: str | None = None,
    notes: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new contact. Requires the `contacts` (write) scope.

    Returns the created contact's resource_name + flattened fields.
    """
    body: dict = {"names": [{"givenName": given_name}]}
    if family_name:
        body["names"][0]["familyName"] = family_name
    if emails:
        body["emailAddresses"] = [{"value": e} for e in emails]
    if phones:
        body["phoneNumbers"] = [{"value": p} for p in phones]
    if organization:
        body["organizations"] = [{"name": organization}]
    if notes:
        body["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]

    resp = _service(account).people().createContact(body=body).execute()
    return _flatten_person(resp)


def delete(resource_name: str, dry_run: bool = False, account: str = DEFAULT_ACCOUNT) -> dict:
    """Permanently delete a contact by resource_name. Requires `contacts` scope.

    With `dry_run=True` fetches display name + emails and returns a preview
    WITHOUT deleting — People API contact deletes are NOT reversible."""
    svc = _service(account)
    if dry_run:
        try:
            person = svc.people().get(
                resourceName=resource_name,
                personFields="names,emailAddresses,phoneNumbers",
            ).execute()
        except Exception as e:
            return {
                "dry_run": True,
                "executed": False,
                "plan": {
                    "would_call": "people.people.deleteContact",
                    "resource_name": resource_name,
                    "preview_error": str(e)[:200],
                },
                "_meta": {"native_preview": True},
            }
        display = ((person.get("names") or [{}])[0].get("displayName"))
        emails = [e.get("value") for e in (person.get("emailAddresses") or [])]
        phones = [p.get("value") for p in (person.get("phoneNumbers") or [])]
        return {
            "dry_run": True,
            "executed": False,
            "plan": {
                "would_call": "people.people.deleteContact",
                "resource_name": resource_name,
                "display_name": display,
                "emails": emails,
                "phones": phones,
                "reversibility": (
                    "NOT REVERSIBLE — People API delete is permanent. "
                    "Export via contacts.google.com first if recovery may "
                    "be needed."
                ),
            },
            "_meta": {"native_preview": True},
        }
    svc.people().deleteContact(resourceName=resource_name).execute()
    return {"ok": True, "resource_name": resource_name}
