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

    Implementation note: we write a small .bat file to %TEMP% and run it,
    instead of building a `cmd /k "..."` argv. Quoting paths inside an
    argv-via-cmd string gets mangled by Windows (double quotes around an
    exe path become "C:\\path\\claude.EXE" which cmd treats as a command
    name, not a path). The .bat sidesteps it entirely.
    """
    exe = _find_claude_exe()
    if not exe:
        return {"ok": False, "error": "Claude Code not installed yet — finish step 1 first"}

    try:
        import tempfile
        bat = Path(tempfile.gettempdir()) / "workspace_agent_claude_login.bat"
        # newline='' disables Python's automatic \n→\r\n translation; we
        # emit \r\n explicitly so we get exactly ONE CRLF per line.
        with bat.open("w", encoding="utf-8", newline="") as f:
            f.write("@echo off\r\n")
            f.write("chcp 65001 > nul\r\n")
            f.write(f'"{exe}" setup-token\r\n')
            f.write("echo.\r\n")
            f.write("echo ============================================\r\n")
            f.write("echo  Готово! Можешь закрыть это окно.\r\n")
            f.write("echo  Workspace Agent сам подхватит вход через 5 сек.\r\n")
            f.write("echo ============================================\r\n")
            f.write("pause\r\n")
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", "cmd.exe", "/k", str(bat)],
            creationflags=_CREATE_NEW_CONSOLE,
            close_fds=True,
        )
        return {
            "ok": True,
            "spawned": True,
            "bat_path": str(bat),
            "message": "Открыл терминал с Claude. Следуй инструкциям там, потом возвращайся.",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


_NOT_LOGGED_IN_MARKERS = (
    "Not logged in",
    "Please run /login",
    "Authentication required",
    "No credentials",
)


def check_claude_auth() -> dict:
    """Probe whether claude is authenticated. The trap: `claude --print`
    returns exit code 0 EVEN when not authenticated — it just prints
    "Not logged in · Please run /login" and exits cleanly. So we must
    also grep the stdout/stderr for failure markers, not trust exit code
    alone.

    Returns {ok, exit_code, reply_preview, stderr_tail, hint?}.
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
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "auth check timed out (>30s) — try again"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    not_logged_in = any(m in combined for m in _NOT_LOGGED_IN_MARKERS)
    ok = (
        proc.returncode == 0
        and bool((proc.stdout or "").strip())
        and not not_logged_in
    )
    result = {
        "ok": ok,
        "exit_code": proc.returncode,
        "reply_preview": (proc.stdout or "")[:200].strip(),
        "stderr_tail": (proc.stderr or "")[-200:],
    }
    if not_logged_in:
        result["error"] = "Claude установлен но НЕ залогинен. Открой PowerShell, набери `claude`, потом `/login`, пройди OAuth."
        result["needs_login"] = True
    return result


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
