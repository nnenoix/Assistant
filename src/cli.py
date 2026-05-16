"""CLI for managing Google account OAuth tokens.

Usage (from project root):
    uv run python -m src.cli list
    uv run python -m src.cli add <alias>
    uv run python -m src.cli remove <alias>

`add` opens a browser for the OAuth consent flow. Pick the Google account you
want this alias to point to — the resulting token is saved under
`.data/tokens/<alias>.json`.
"""
import sys

from src import auth


def _usage() -> None:
    print("Usage: uv run python -m src.cli {list|add|remove} [alias]")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        _usage()
        return 1

    cmd = args[0]

    if cmd == "list":
        accounts = auth.list_accounts()
        if not accounts:
            print("(no accounts configured)")
        else:
            for a in accounts:
                print(a)
        return 0

    if cmd == "add":
        if len(args) < 2:
            _usage()
            return 1
        alias = args[1]
        print(f"Starting OAuth flow for account '{alias}'…")
        print("A browser window will open. Log in to the Google account you want to bind to this alias.")
        result = auth.add_account(alias)
        print(f"Saved token: {result['saved_to']}")
        return 0

    if cmd == "remove":
        if len(args) < 2:
            _usage()
            return 1
        result = auth.remove_account(args[1])
        if result["removed"]:
            print(f"Removed '{args[1]}'")
        else:
            print(f"Account '{args[1]}' not found")
        return 0

    print(f"Unknown command: {cmd}")
    _usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
