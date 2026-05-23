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

from src.tools._errors import _classify_exception, _classify_http_error

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
            # Delegate to the project-wide classifier so the agent sees
            # the same error_kind vocabulary as every other tool.
            "error_kind": _classify_http_error(e.code, str(e)),
            "_meta": {"http_status": e.code, "manifest_url": manifest_url},
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        kind, _ = _classify_exception(e)
        return {
            "ok": False,
            "error": f"network: {type(e).__name__}: {str(e)[:200]}",
            "error_kind": kind,
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


def download_update(download_url: str, target_path: str,
                    chunk_size: int = 64 * 1024,
                    timeout: int = 300,
                    expected_sha256: str | None = None,
                    progress_cb: Any = None) -> dict:
    """Stream the new build to `target_path`. Returns standard envelope.

    - Streams in `chunk_size`-byte chunks so a 50MB .exe doesn't go
      RAM-resident.
    - If `expected_sha256` is provided, verifies the SHA-256 of the
      downloaded bytes BEFORE writing the final path (we write to
      `target_path + ".part"` first, then atomically rename). A hash
      mismatch deletes the partial and returns `error_kind: "bad_input"`
      so the agent doesn't apply a corrupt / tampered binary.
    - `progress_cb(bytes_so_far, total_or_None)` is called per chunk if
      supplied (UI can update a progress bar). Errors in the callback
      are silently swallowed — never break the download.

    Returns:
        ok:   {ok: True, data: {bytes, sha256, path}, _meta}
        fail: {ok: False, error, error_kind, _meta}
    """
    import hashlib
    from pathlib import Path

    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")

    try:
        req = urllib.request.Request(
            download_url,
            headers={"Accept": "application/octet-stream"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = resp.headers.get("Content-Length")
            total_int = int(total) if total and total.isdigit() else None
            h = hashlib.sha256()
            bytes_so_far = 0
            with open(part, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    bytes_so_far += len(chunk)
                    if progress_cb is not None:
                        try:
                            progress_cb(bytes_so_far, total_int)
                        except Exception:
                            pass
            actual_sha = h.hexdigest()
    except urllib.error.HTTPError as e:
        if part.exists():
            part.unlink()
        return {
            "ok": False,
            "error": f"HTTP {e.code}: {str(e)[:200]}",
            "error_kind": _classify_http_error(e.code, str(e)),
            "_meta": {"http_status": e.code, "download_url": download_url},
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if part.exists():
            try:
                part.unlink()
            except Exception:
                pass
        kind, _ = _classify_exception(e)
        return {
            "ok": False,
            "error": f"network: {type(e).__name__}: {str(e)[:200]}",
            "error_kind": kind,
            "_meta": {"http_status": None, "download_url": download_url},
        }

    if expected_sha256 and actual_sha.lower() != expected_sha256.lower():
        # Refuse to atomically promote a hash-mismatched download —
        # this is the supply-chain attack surface (MITM swapped the
        # binary). Delete the partial; caller decides whether to retry.
        if part.exists():
            part.unlink()
        return {
            "ok": False,
            "error": (f"sha256 mismatch: expected {expected_sha256}, "
                      f"got {actual_sha}"),
            "error_kind": "bad_input",
            "_meta": {"download_url": download_url,
                      "actual_sha256": actual_sha,
                      "expected_sha256": expected_sha256},
        }

    # Atomically move into place
    if target.exists():
        target.unlink()
    part.rename(target)
    return {
        "ok": True,
        "data": {
            "bytes": bytes_so_far,
            "sha256": actual_sha,
            "path": str(target.resolve()),
        },
        "_meta": {"download_url": download_url},
    }


def apply_update(new_exe_path: str, current_exe_path: str | None = None,
                 relaunch: bool = True) -> dict:
    """Swap the running .exe with the freshly downloaded one + optionally
    relaunch the app.

    Strategy (Windows-aware): a running .exe can't replace itself, but
    it CAN be renamed while running. So:
        1. Rename current → current + ".old"
        2. Rename new     → current
        3. Optionally `os.startfile(current)` to launch the fresh build
        4. The user's existing process exits when its main loop quits
           (or via `sys.exit(0)` if `relaunch=True`)

    On non-Windows (Linux/macOS), an .exe rename still works the same —
    a Python script / binary can be renamed while executing. The "rename
    while running" trick is OS-agnostic; only the launch step differs.

    Returns standard envelope; never raises (the caller is mid-update
    and a clean error message matters more than a clean traceback).

    Caller is responsible for explicit `sys.exit(0)` AFTER this returns
    `{ok: True}` if they want the old process to die immediately. We
    don't call exit ourselves so tests can drive this in-process.
    """
    import os
    import shutil
    import sys
    from pathlib import Path

    new_path = Path(new_exe_path)
    if not new_path.exists():
        return {
            "ok": False,
            "error": f"new executable not found: {new_exe_path}",
            "error_kind": "not_found",
        }

    if current_exe_path is None:
        # Default: replace the binary that's running this Python.
        # For a frozen build (PyInstaller .exe), this is the .exe path.
        # For `uv run` from source, this is the python interpreter —
        # caller should pass an explicit `current_exe_path` instead.
        current_exe_path = sys.executable
    current = Path(current_exe_path)

    backup = current.with_suffix(current.suffix + ".old")
    try:
        # Clean up any leftover from a previous update attempt
        if backup.exists():
            try:
                backup.unlink()
            except Exception:
                pass
        # Step 1: move current → .old (allowed even while running on Windows)
        shutil.move(str(current), str(backup))
        # Step 2: move new → current
        shutil.move(str(new_path), str(current))
    except OSError as e:
        # Best-effort rollback
        if backup.exists() and not current.exists():
            try:
                shutil.move(str(backup), str(current))
            except Exception:
                pass
        return {
            "ok": False,
            "error": f"swap failed: {type(e).__name__}: {str(e)[:200]}",
            "error_kind": "server",
            "_meta": {"current_path": str(current), "backup_path": str(backup)},
        }

    if relaunch:
        # Launch the new binary in a detached process so we can exit
        # cleanly. Caller controls the actual exit via sys.exit.
        try:
            if sys.platform == "win32":
                os.startfile(str(current))  # detached by default on Windows
            else:
                # POSIX: fork+exec via subprocess so the child outlives us
                import subprocess
                subprocess.Popen(
                    [str(current)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except Exception as e:
            return {
                "ok": True,
                "data": {
                    "applied": True,
                    "backup_path": str(backup),
                    "relaunch_failed": f"{type(e).__name__}: {e}",
                },
            }
    return {
        "ok": True,
        "data": {
            "applied": True,
            "backup_path": str(backup),
            "current_path": str(current),
            "relaunched": relaunch,
        },
    }


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
    # Fallback for `uv run` from a checkout — read pyproject.toml via
    # stdlib `tomllib` (Python 3.11+; pyproject.toml requires 3.11).
    try:
        import tomllib
        from pathlib import Path
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return data["project"]["version"]
    except Exception:
        pass
    return "0.0.0"
