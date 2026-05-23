"""Minimal app-update checker.

Walks an HTTP-published manifest (JSON) and tells the caller whether
a newer build of the agent is available. NO auto-download, no
self-replace, no Windows-restart wizardry — that work lives downstream
in the installer / UI layer and varies wildly by deployment channel
(GitHub Releases, S3, internal artifact registry, …).

Manifest shape (publish at e.g. `https://github.com/.../releases/latest/download/manifest.json`):

    {
        "latest_version": "0.2.0",
        "download_url":   "https://.../workspace-agent-0.2.0-windows.exe",
        "release_notes":  "Fixed X, added Y.",
        "min_compatible_version": "0.1.0"   // optional
    }

Caller flow:

    from src.updater import check_for_updates
    out = check_for_updates(current_version="0.1.0",
                            manifest_url="https://…/manifest.json")
    if out["ok"] and out["update_available"]:
        notify_user(out["latest_version"], out["download_url"])

Returns:
    on success: {ok: True, update_available: bool, current_version,
                 latest_version, download_url?, release_notes?,
                 _meta: {http_status, manifest_url}}
    on error:   {ok: False, error: str, error_kind, _meta}
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def _parse_version(s: str) -> tuple[int, ...] | None:
    """Parse a `MAJOR.MINOR.PATCH[…]` string into a comparable tuple.

    Strips a leading `v`, drops semver pre-release (`-rc1`) and build
    metadata (`+build.5`) suffixes BEFORE splitting on dots, so we
    tolerate the common decorators correctly enough for "newer?"
    decisions without dragging in `packaging`. Returns None on garbage."""
    if not isinstance(s, str):
        return None
    s = s.strip().lstrip("v")
    # Semver: pre-release starts at `-`, build metadata at `+`. Both
    # are unordered for our purposes (we compare core MAJOR.MINOR.PATCH).
    for sep in ("+", "-"):
        if sep in s:
            s = s.split(sep, 1)[0]
    parts: list[int] = []
    for chunk in s.split("."):
        if not chunk:
            return None
        # Take leading-digit run only (defensive: `1rc2` → 1)
        digits = []
        for ch in chunk:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        if not digits:
            return None
        parts.append(int("".join(digits)))
    return tuple(parts) if parts else None


def _is_newer(latest: str, current: str) -> bool:
    """True if `latest` parses as a higher semver than `current`. If
    either side fails to parse, we conservatively return False so a
    malformed manifest doesn't nag the user with a phantom update."""
    a = _parse_version(latest)
    b = _parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def check_for_updates(current_version: str, manifest_url: str,
                      timeout: int = 10) -> dict:
    """Fetch the manifest at `manifest_url` and compare versions.

    Network failures degrade gracefully — we return `{ok: False, ...}`
    with an `error_kind` so the caller can decide whether to surface
    the failure or silently continue.

    `timeout` defaults to a tight 10s so a slow CDN can't stall app
    boot if the caller wires this into startup."""
    try:
        req = urllib.request.Request(
            manifest_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "error": f"HTTP {e.code}: {str(e)[:200]}",
            "error_kind": (
                "not_found" if e.code == 404
                else "rate_limit" if e.code == 429
                else "server" if e.code >= 500
                else "bad_input"
            ),
            "_meta": {"http_status": e.code, "manifest_url": manifest_url},
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {
            "ok": False,
            "error": f"network: {type(e).__name__}: {str(e)[:200]}",
            "error_kind": "server",
            "_meta": {"http_status": None, "manifest_url": manifest_url},
        }

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return {
            "ok": False,
            "error": f"manifest not JSON: {e}",
            "error_kind": "bad_input",
            "_meta": {"http_status": status, "manifest_url": manifest_url},
        }

    latest = manifest.get("latest_version")
    if not latest:
        return {
            "ok": False,
            "error": "manifest missing required 'latest_version' field",
            "error_kind": "bad_input",
            "_meta": {"http_status": status, "manifest_url": manifest_url},
        }

    update_available = _is_newer(latest, current_version)
    result = {
        "ok": True,
        "update_available": update_available,
        "current_version": current_version,
        "latest_version": latest,
        "_meta": {"http_status": status, "manifest_url": manifest_url},
    }
    if update_available:
        # Only echo download_url / release_notes when there's actually
        # something to download — keeps the payload small in the
        # "you're up to date" hot path.
        if "download_url" in manifest:
            result["download_url"] = manifest["download_url"]
        if "release_notes" in manifest:
            result["release_notes"] = manifest["release_notes"]
    return result


def get_current_version() -> str:
    """Return the running build's version string. Tries
    `importlib.metadata.version` first (works for installed packages
    and PyInstaller .exe builds that bake metadata), falls back to
    parsing `pyproject.toml` for a `uv run` / source checkout."""
    try:
        from importlib import metadata
        return metadata.version("google-work-agent")
    except Exception:
        pass
    # Fallback for `uv run` from a checkout — read pyproject.toml.
    try:
        from pathlib import Path
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("version") and "=" in line:
                # version = "0.1.0"
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value
    except Exception:
        pass
    return "0.0.0"
