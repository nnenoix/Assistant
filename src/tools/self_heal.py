"""Self-healing: tools for the agent to read/edit/test/commit its own code.

Threat model: the agent can read all of `src/`, but every WRITE (edit,
commit, revert) is policy-gated and surfaces an approval modal to the user.
The agent never auto-applies fixes; the human is always in the loop.

Workflow (system-prompt rule #17):
    1. User reports a bug or asks for an improvement.
    2. Agent reads relevant files via self_read_source().
    3. Agent stages the fix locally, runs self_smoke_test() to verify it
       still imports clean.
    4. Agent shows self_git_diff() to the user.
    5. User clicks "Allow" on the self_edit_source approval → fix lands.
    6. Agent runs self_smoke_test() again, then self_git_commit() with a
       descriptive message (approval again).
    7. Tells the user to restart the app (a frozen exe can't hot-reload).

Restart: the running process won't see code changes until restart. For
uvicorn dev mode use `--reload`; for the frozen exe the user closes the
window and double-clicks again. The agent doesn't auto-restart.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.config import PROJECT_ROOT


SRC_ROOT = PROJECT_ROOT / "src"
STATIC_ROOT = PROJECT_ROOT / "static"


def _resolve_inside_src(path: str) -> Path:
    """Resolve `path` and assert it lives under src/ or static/. Raises on
    any escape attempt (absolute outside, .. shenanigans, symlink jumps).
    """
    p = Path(path).resolve()
    src = SRC_ROOT.resolve()
    static = STATIC_ROOT.resolve()
    if not (str(p).startswith(str(src)) or str(p).startswith(str(static))):
        raise ValueError(
            f"Self-edit forbidden outside src/ or static/: {path} resolved to {p}"
        )
    return p


def self_read_source(path: str) -> dict:
    """Read a source file from this project (under src/ or static/).
    Returns {path, content, lines, bytes}. Distinct from local_read_file
    only in that it enforces the src/static boundary explicitly — use this
    when self-editing so the agent never accidentally reads .data/ tokens.
    """
    p = _resolve_inside_src(path)
    text = p.read_text(encoding="utf-8")
    return {
        "path": str(p),
        "content": text,
        "lines": text.count("\n") + 1,
        "bytes": len(text.encode("utf-8")),
    }


def self_edit_source(path: str, new_content: str) -> dict:
    """Replace the contents of a project source file. POLICY-GATED — the
    user gets an approval modal for every call. Returns {path, before_bytes,
    after_bytes, before_lines, after_lines}.

    After applying, call self_smoke_test() to verify the app still imports.
    The change won't be live in the running process until the user restarts.
    """
    p = _resolve_inside_src(path)
    before = p.read_text(encoding="utf-8") if p.exists() else ""
    p.write_text(new_content, encoding="utf-8")
    return {
        "path": str(p),
        "before_bytes": len(before.encode("utf-8")),
        "after_bytes": len(new_content.encode("utf-8")),
        "before_lines": before.count("\n") + 1,
        "after_lines": new_content.count("\n") + 1,
        "needs_restart": True,
    }


def self_smoke_test() -> dict:
    """Spawn a fresh Python process and verify `src.app` imports cleanly.
    Returns {ok, stdout, stderr, exit_code}. The current process keeps the
    old code in memory — this catches syntax errors / missing imports in
    the new file before commit.
    """
    cmd = [sys.executable, "-c",
           "import sys; sys.path.insert(0, '.'); import src.app; print('IMPORT_OK')"]
    proc = subprocess.run(
        cmd, cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    return {
        "ok": proc.returncode == 0 and "IMPORT_OK" in (proc.stdout or ""),
        "exit_code": proc.returncode,
        "stdout": (proc.stdout or "")[-800:],
        "stderr": (proc.stderr or "")[-800:],
    }


def self_git_diff(staged: bool = False, path: str | None = None) -> dict:
    """Show pending changes vs HEAD. `staged=True` shows only what's added
    to the index; default shows working tree. `path` narrows to one file.
    Returns {diff, files_changed}.
    """
    args = ["git", "diff"]
    if staged:
        args.append("--cached")
    if path:
        args.append("--")
        args.append(str(_resolve_inside_src(path).relative_to(PROJECT_ROOT)))
    proc = subprocess.run(
        args, cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    diff = proc.stdout or ""
    # Count files (lines starting with "diff --git")
    files_changed = diff.count("\ndiff --git ") + (1 if diff.startswith("diff --git ") else 0)
    return {
        "diff": diff[:8000],  # cap so we don't blow the tool-output budget
        "truncated": len(diff) > 8000,
        "files_changed": files_changed,
        "bytes": len(diff),
    }


def self_git_status() -> dict:
    """`git status --short` — list modified / untracked files under src/.
    Cheap, read-only, no approval needed."""
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return {"status": proc.stdout, "clean": not proc.stdout.strip()}


def self_git_commit(message: str, paths: list[str] | None = None) -> dict:
    """Stage `paths` (or all changed files if None) and commit with `message`.
    POLICY-GATED — needs user approval. Returns {ok, commit_sha, stdout}.

    Append a Claude co-author line automatically — the user can spot
    self-edited commits in `git log`.
    """
    add_args = ["git", "add"]
    if paths:
        for p in paths:
            _resolve_inside_src(p)  # validates
            add_args.append(str(Path(p).resolve().relative_to(PROJECT_ROOT)))
    else:
        add_args.append("-u")  # all tracked changes
    subprocess.run(add_args, cwd=str(PROJECT_ROOT), check=False, capture_output=True)

    full_msg = f"{message}\n\nCo-Authored-By: Claude (self-healing) <noreply@anthropic.com>"
    proc = subprocess.run(
        ["git", "commit", "-m", full_msg],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    sha = ""
    if proc.returncode == 0:
        sha_proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
        )
        sha = sha_proc.stdout.strip()
    return {
        "ok": proc.returncode == 0,
        "commit_sha": sha,
        "stdout": (proc.stdout or "")[-600:],
        "stderr": (proc.stderr or "")[-600:],
    }


def self_git_revert(path: str) -> dict:
    """Revert `path` to HEAD (drop unstaged changes). POLICY-GATED. Use
    when a self_edit broke something and smoke-test failed."""
    p = _resolve_inside_src(path).relative_to(PROJECT_ROOT)
    proc = subprocess.run(
        ["git", "checkout", "HEAD", "--", str(p)],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return {
        "ok": proc.returncode == 0,
        "path": str(p),
        "stderr": (proc.stderr or "")[-300:],
    }


def self_run_tests(pattern: str = "tests/test_*.py", deselect: list[str] | None = None) -> dict:
    """Run pytest on the given path/pattern. Beyond self_smoke_test (which
    only verifies imports), this actually runs the test suite. Use after a
    self_edit before committing.

    `pattern` is passed to pytest as the positional arg (file/dir/pattern).
    `deselect` is a list of `--deselect <nodeid>` items — useful for skipping
    known-failing pre-existing tests.

    Returns {ok, passed, failed, skipped, exit_code, summary, output}.
    """
    cmd = ["uv", "run", "pytest", pattern, "-q", "--tb=line"]
    for d in deselect or []:
        cmd.extend(["--deselect", d])
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    out = proc.stdout or ""
    err = proc.stderr or ""
    # Parse pytest summary line, e.g. "115 passed, 3 skipped in 2.28s"
    import re
    summary_match = re.search(
        r"(\d+) passed(?:, (\d+) failed)?(?:, (\d+) skipped)?",
        out,
    )
    passed = int(summary_match.group(1)) if summary_match else 0
    failed = int(summary_match.group(2)) if summary_match and summary_match.group(2) else 0
    skipped = int(summary_match.group(3)) if summary_match and summary_match.group(3) else 0
    # Final summary line (last non-empty line of pytest output is usually most useful)
    last_lines = [ln for ln in out.splitlines() if ln.strip()][-5:]
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "summary": "\n".join(last_lines),
        "stderr": err[-500:] if err else "",
        "pattern": pattern,
    }


def self_list_tools() -> dict:
    """Introspect every registered agent tool. Returns {tools: [{name, policy_op,
    description, has_account_param}], _meta:{count}}.

    Useful for the agent's own self-orientation: «какие у меня инструменты
    под X» without scanning the registry source.
    """
    from src.tools import registry

    out = []
    for spec in registry.TOOLS:
        has_acct = (
            spec.get("schema", {})
            .get("input_schema", {})
            .get("properties", {})
            .get("account") is not None
        )
        out.append({
            "name": spec["name"],
            "policy_op": spec["policy_op"],
            "description": spec["schema"]["description"][:200],
            "has_account_param": has_acct,
        })
    return {
        "tools": out,
        "_meta": {
            "count": len(out),
            "policy_ops": sorted({t["policy_op"] for t in out}),
        },
    }
