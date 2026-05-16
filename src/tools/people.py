"""Agent-facing tools for the people registry (see src/people.py)."""
from src import people as _impl


def list_people() -> list[dict]:
    """All registered people. Each entry has id, account, names, email, note."""
    return _impl.list_people()


def add(
    account: str,
    names: list[str] | str,
    email: str | None = None,
    note: str | None = None,
) -> dict:
    """Register a name (or list of names) as belonging to a Google account
    alias. If the account is already known, the names/email/note are merged.
    """
    return _impl.add(account=account, names=names, email=email, note=note)


def resolve(hint: str) -> list[dict]:
    """Match a free-text reference ('Лена', 'partner', 'elena@...') against the
    registry and return all matching people. If exactly one match, the agent
    can confidently use that person's account. If multiple, ask the user.
    If none, ask the user to register the person first.
    """
    return _impl.resolve(hint)


def remove(account: str) -> dict:
    """Drop a person from the registry by account alias."""
    return _impl.remove(account=account)
