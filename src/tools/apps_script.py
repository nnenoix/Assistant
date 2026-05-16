import json
import shutil
import subprocess
from pathlib import Path

from src.config import SCRIPTS_DIR


class ClaspError(RuntimeError):
    pass


_CLASP_BIN = shutil.which("clasp")


def _run_clasp(args: list[str], cwd: Path | None = None) -> str:
    if _CLASP_BIN is None:
        raise ClaspError("clasp not found on PATH. Install via `npm install -g @google/clasp`.")
    proc = subprocess.run(
        [_CLASP_BIN, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    if proc.returncode != 0:
        raise ClaspError(f"clasp {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def _project_dir(script_id: str) -> Path:
    return SCRIPTS_DIR / script_id


def _safe_path(script_id: str, relpath: str) -> Path:
    project = _project_dir(script_id).resolve()
    target = (project / relpath).resolve()
    try:
        target.relative_to(project)
    except ValueError:
        raise ClaspError(f"path traversal rejected: {relpath!r}")
    return target


def clone(script_id: str) -> dict:
    """Clone a script project into .data/scripts/<id>/. Idempotent — pulls if dir exists."""
    target = _project_dir(script_id)
    if target.exists() and any(target.iterdir()):
        return pull(script_id)
    target.mkdir(parents=True, exist_ok=True)
    output = _run_clasp(["clone", script_id, "--rootDir", "."], cwd=target)
    return {"script_id": script_id, "local_dir": str(target), "stdout": output.strip()}


def pull(script_id: str) -> dict:
    output = _run_clasp(["pull"], cwd=_project_dir(script_id))
    return {"script_id": script_id, "local_dir": str(_project_dir(script_id)), "stdout": output.strip()}


def push(script_id: str) -> dict:
    output = _run_clasp(["push", "--force"], cwd=_project_dir(script_id))
    return {"script_id": script_id, "stdout": output.strip()}


def list_files(script_id: str) -> list[str]:
    p = _project_dir(script_id)
    if not p.exists():
        raise ClaspError(f"project not cloned: {script_id}")
    return sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file() and not f.name.startswith("."))


def read_file(script_id: str, relpath: str) -> str:
    return _safe_path(script_id, relpath).read_text(encoding="utf-8")


def write_file(script_id: str, relpath: str, content: str) -> dict:
    target = _safe_path(script_id, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}


def run_function(script_id: str, function_name: str, params: list | None = None) -> dict:
    """Run a function in the deployed API executable. Requires deployment first."""
    args = ["run", function_name]
    if params:
        args += ["--params", json.dumps(params)]
    output = _run_clasp(args, cwd=_project_dir(script_id))
    return {"script_id": script_id, "function": function_name, "output": output.strip()}
