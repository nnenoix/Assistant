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


_CREATE_NEW_CONSOLE = 0x00000010  # Windows-only flag for Popen


def login_claude() -> dict:
    """Open a NEW visible terminal window running `claude setup-token`.

    Why visible: `claude setup-token` uses Ink (interactive React TUI) which
    requires a real TTY. A hidden subprocess fails with "Raw mode is not
    supported on the current process.stdin". So we spawn a fresh cmd.exe
    window where the user can follow the prompts.

    Returns immediately (spawned process runs independently). UI shows the
    user a "Done" button — clicking it calls check_claude_auth() to verify.
    """
    exe = _find_claude_exe()
    if not exe:
        return {"ok": False, "error": "Claude Code not installed yet — finish step 1 first"}

    try:
        # Wrap in cmd /k so the window stays open after setup-token exits
        # (lets the user see "Done" before closing). Detached from us.
        subprocess.Popen(
            ["cmd.exe", "/k", f'"{exe}" setup-token & echo. & echo Готово! Можешь закрыть это окно. & pause'],
            creationflags=_CREATE_NEW_CONSOLE,
            close_fds=True,
        )
        return {
            "ok": True,
            "spawned": True,
            "message": "Открыл терминал с Claude. Следуй инструкциям там, потом нажми «Готово» здесь.",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def check_claude_auth() -> dict:
    """Probe whether claude is authenticated by running a tiny --print call.
    `claude --print` makes a real API request, so success = auth works.
    Costs a few tokens. Takes 3-15 sec.

    Returns {ok, exit_code, stderr_tail}. ok=True iff Claude responded.
    """
    exe = _find_claude_exe()
    if not exe:
        return {"ok": False, "error": "Claude not installed"}
    try:
        proc = subprocess.run(
            [exe, "--print", "--max-turns=1", "say ok and nothing else"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        ok = proc.returncode == 0 and bool((proc.stdout or "").strip())
        return {
            "ok": ok,
            "exit_code": proc.returncode,
            "reply_preview": (proc.stdout or "")[:120].strip(),
            "stderr_tail": (proc.stderr or "")[-200:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "auth check timed out (>30s) — try again"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def check_setup_status() -> dict:
    """One-shot: returns the full first-run state plus a `complete` flag.

    `complete = True` means the chat is ready to use. Wizard renders if
    `complete = False` and falls back into main UI as soon as missing
    pieces show up.
    """
    claude = check_claude_cli()
    oauth_client = check_oauth_client()
    main_token = check_main_token()
    return {
        "claude_cli": claude,
        "oauth_client": oauth_client,
        "main_token": main_token,
        "complete": bool(claude.get("installed") and oauth_client.get("present") and main_token.get("present")),
    }
