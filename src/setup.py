"""First-run setup detection + actions.

Detection: cheap polls the wizard runs on every mount and after each click.
Actions: install Claude Code natively, drive `claude login`, all without
a visible terminal window.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from src.config import CLIENT_SECRET_PATH, DATA_DIR


TOKENS_DIR = DATA_DIR / "tokens"

# Windows-only: hide the cmd window of subprocesses we spawn.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _refresh_path_from_registry() -> None:
    """After Claude installer modifies the user's PATH in HKCU, our running
    Python process still has the OLD os.environ['PATH']. Re-read the user
    + machine PATH from registry and stitch them into the current env so
    shutil.which() finds the freshly-installed claude.exe.
    """
    if sys.platform != "win32":
        return
    try:
        import winreg
        parts: list[str] = []
        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    parts.append(val)
            except FileNotFoundError:
                continue
        new_path = ";".join(parts)
        if new_path:
            os.environ["PATH"] = new_path
    except Exception:
        pass  # best-effort; user can still restart the app


def _find_claude_exe() -> str | None:
    """Look for claude.exe / claude.cmd / claude (Unix) in PATH first, then
    well-known install locations the Anthropic installer uses. Covers the
    case where PATH wasn't refreshed yet after a fresh install or where
    the user moved/renamed the binary."""
    found = shutil.which("claude")
    if found:
        return found
    if sys.platform != "win32":
        return None
    # Anthropic's native installer drops claude.exe in one of these:
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        Path(os.environ.get("USERPROFILE", "")) / ".claude" / "bin" / "claude.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def check_claude_cli() -> dict:
    """Is Claude Code installed and runnable? Returns {installed, exe, version}."""
    exe = _find_claude_exe()
    if not exe:
        return {"installed": False, "exe": None}
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        if proc.returncode == 0:
            # Add exe's directory to PATH so subsequent shutil.which() calls
            # (e.g. from claude-agent-sdk subprocess) find it without restart.
            exe_dir = str(Path(exe).parent)
            if exe_dir not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = exe_dir + os.pathsep + os.environ.get("PATH", "")
            return {"installed": True, "exe": exe, "version": proc.stdout.strip()}
        return {"installed": False, "exe": exe, "error": proc.stderr.strip()[:200]}
    except subprocess.TimeoutExpired:
        return {"installed": False, "exe": exe, "error": "timed out (>8s)"}
    except Exception as e:
        return {"installed": False, "exe": exe, "error": f"{type(e).__name__}: {e}"}


def check_oauth_client() -> dict:
    """Is a Google OAuth client_secret_*.json present?"""
    return {
        "present": CLIENT_SECRET_PATH is not None,
        "path": str(CLIENT_SECRET_PATH) if CLIENT_SECRET_PATH else None,
    }


def check_main_token() -> dict:
    """Has the user OAuth'd at least one Google account (alias='main')?"""
    main = TOKENS_DIR / "main.json"
    return {"present": main.exists(), "path": str(main) if main.exists() else None}


def install_claude_cli() -> dict:
    """Run Anthropic's official native installer: `irm https://claude.ai/
    install.ps1 | iex` via PowerShell. No console window, no Node.js
    requirement. Blocks until done (~30-90 sec). After success, refreshes
    our PATH from the Windows registry so the freshly-installed claude.exe
    is immediately discoverable by shutil.which().

    Returns {ok, exit_code, output, stderr?, claude_path?}.
    """
    if sys.platform != "win32":
        return {"ok": False, "error": "only Windows supported for now"}

    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-Command", "irm https://claude.ai/install.ps1 | iex",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=600,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "install timed out after 10 minutes"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    _refresh_path_from_registry()
    exe = shutil.which("claude")
    return {
        "ok": proc.returncode == 0 and exe is not None,
        "exit_code": proc.returncode,
        "output": (proc.stdout or "")[-1200:],
        "stderr": (proc.stderr or "")[-600:] if proc.returncode != 0 else None,
        "claude_path": exe,
    }


def login_claude() -> dict:
    """Trigger Claude's browser-based OAuth via `claude auth login`. This
    is a HEADLESS subcommand — it just opens the user's default browser
    to claude.com/cai/oauth/authorize and prints the URL as fallback,
    then exits 0. No TUI, no console window, no user typing.

    The OAuth completes asynchronously: Anthropic's server captures the
    callback and the claude CLI background-syncs the resulting token to
    ~/.claude/. We poll `claude auth status` to know when it's done.
    """
    exe = _find_claude_exe()
    if not exe:
        return {"ok": False, "error": "Claude Code not installed yet — finish step 1 first"}

    try:
        proc = subprocess.run(
            [exe, "auth", "login", "--claudeai"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude auth login timed out (>15s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # The subprocess exits 0 after opening the browser. We extract the
    # fallback URL so the UI can offer "click here if browser didn't open".
    fallback_url = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("https://"):
            fallback_url = line
            break

    return {
        "ok": proc.returncode == 0,
        "spawned": True,
        "fallback_url": fallback_url,
        "stdout_tail": (proc.stdout or "")[-300:],
        "stderr_tail": (proc.stderr or "")[-300:] if proc.returncode != 0 else None,
    }


def check_claude_auth() -> dict:
    """Probe auth state via `claude auth status` — returns a tiny JSON
    document with {loggedIn, authMethod, apiProvider}. NO model call,
    NO token cost, finishes in <1 sec.

    Returns {ok, logged_in, auth_method, raw}.
    """
    exe = _find_claude_exe()
    if not exe:
        return {"ok": False, "error": "Claude not installed"}
    try:
        proc = subprocess.run(
            [exe, "auth", "status"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "auth status timed out"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # NB: `claude auth status` exits 1 when NOT logged in but still prints
    # valid JSON to stdout. Parse the JSON regardless of returncode and
    # use loggedIn as truth.
    import json as _json
    try:
        data = _json.loads((proc.stdout or "").strip())
    except Exception:
        return {
            "ok": False,
            "error": (proc.stderr or "").strip()[:200] or "could not parse auth status",
            "raw_stdout": (proc.stdout or "")[:200],
        }

    return {
        "ok": bool(data.get("loggedIn")),
        "logged_in": bool(data.get("loggedIn")),
        "auth_method": data.get("authMethod"),
        "api_provider": data.get("apiProvider"),
        "raw": data,
    }


def check_setup_status(probe_auth: bool = True) -> dict:
    """One-shot: returns the full first-run state plus a `complete` flag.

    `complete = True` means the chat is ready to use. Wizard renders if
    `complete = False` and falls back into main UI as soon as missing
    pieces show up.

    `probe_auth` runs check_claude_auth (which costs ~few tokens). UI polls
    this endpoint regularly; the cheap path (probe_auth=False) only checks
    installation, used when nothing's likely to have changed mid-flight.
    """
    claude = check_claude_cli()
    oauth_client = check_oauth_client()
    main_token = check_main_token()

    claude_authed = None
    if probe_auth and claude.get("installed"):
        claude_authed = check_claude_auth()

    complete = bool(
        claude.get("installed")
        and oauth_client.get("present")
        and main_token.get("present")
        and (claude_authed is None or claude_authed.get("ok"))
    )
    return {
        "claude_cli": claude,
        "claude_auth": claude_authed,
        "oauth_client": oauth_client,
        "main_token": main_token,
        "complete": complete,
    }
