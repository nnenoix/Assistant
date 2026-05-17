"""High-level orchestration tools that combine several lower-level tools.

The agent CAN reproduce these as a sequence — but a one-shot wrapper saves
multi-step approvals and reduces the chance of stepping in the wrong order.
"""
import uuid
from pathlib import Path

from src.config import SCRIPTS_DIR
from src.tools import apps_script


def apps_script_oneshot(
    code: str,
    function_name: str = "main",
    keep_project: bool = False,
    alias: str | None = None,
) -> dict:
    """Run an Apps Script function ONE-OFF: create a standalone script project,
    push the code, attempt to run it, return the output.

    Notes:
    - Uses clasp (which has its own Google OAuth, set up via `clasp login`).
    - `clasp run` requires the script to be deployed as API executable. If the
      project is brand-new, `run` will fail with a 'not deployed' error — the
      tool then returns the script's URL so the user can deploy it once. After
      first deployment, subsequent calls work.
    - `keep_project=True` retains the cloned project in .data/scripts/ for
      re-runs; otherwise the local dir is cleaned up but the Google project
      stays in user's Drive.

    Code template:
        function main() {
          const id = '<spreadsheet-id>';
          const sh = SpreadsheetApp.openById(id).getSheetByName('Orders');
          const data = sh.getDataRange().getValues();
          let total = 0;
          for (let i = 1; i < data.length; i++) total += data[i][4];
          return { total: total, rows: data.length - 1 };
        }
    Return value of the function is JSON-serializable.
    """
    alias = alias or ("oneshot-" + uuid.uuid4().hex[:8])
    project_dir = SCRIPTS_DIR / alias
    project_dir.mkdir(parents=True, exist_ok=True)

    # Use `clasp create` for a standalone script. We can't do this through
    # the wrapper because it's a new project; build it inline.
    import subprocess, shutil, json as _json
    clasp = shutil.which("clasp")
    if not clasp:
        raise RuntimeError("clasp not on PATH")

    # 1. Create standalone script
    proc = subprocess.run(
        [clasp, "create", "--type", "standalone", "--title", alias, "--rootDir", "."],
        cwd=project_dir,
        capture_output=True, text=True, encoding="utf-8", shell=False,
    )
    if proc.returncode != 0 and "already exists" not in (proc.stderr or "") + (proc.stdout or ""):
        return {"ok": False, "stage": "create", "error": (proc.stderr or proc.stdout).strip()}

    # 2. Write the code as Code.gs and ensure appsscript.json has the runtime
    (project_dir / "Code.gs").write_text(code, encoding="utf-8")
    manifest = project_dir / "appsscript.json"
    if not manifest.exists():
        manifest.write_text(_json.dumps({
            "timeZone": "Etc/UTC",
            "exceptionLogging": "STACKDRIVER",
            "runtimeVersion": "V8",
        }, indent=2), encoding="utf-8")

    # 3. Push
    try:
        push_result = apps_script._run_clasp(["push", "--force"], cwd=project_dir)
    except apps_script.ClaspError as e:
        return {"ok": False, "stage": "push", "error": str(e)}

    # Read .clasp.json to get the scriptId for the URL
    clasp_json = project_dir / ".clasp.json"
    script_id = None
    if clasp_json.exists():
        try:
            script_id = _json.loads(clasp_json.read_text(encoding="utf-8")).get("scriptId")
        except Exception:
            pass
    script_url = f"https://script.google.com/d/{script_id}/edit" if script_id else None

    # 4. Try to run
    try:
        run_output = apps_script._run_clasp(["run", function_name], cwd=project_dir)
        result = {
            "ok": True,
            "function": function_name,
            "output": run_output.strip(),
            "script_url": script_url,
            "alias": alias,
        }
    except apps_script.ClaspError as e:
        # Most likely cause: not deployed as API executable. Return helpful info.
        result = {
            "ok": False,
            "stage": "run",
            "error": str(e),
            "script_url": script_url,
            "alias": alias,
            "hint": (
                "Open the script in the browser via script_url and deploy it as an "
                "API Executable (Deploy → New deployment → Type: API executable). "
                "After that, retry apps_script_oneshot with the same alias and "
                "keep_project=True, OR call apps_script_run(script_id, function_name) "
                "directly."
            ),
        }

    # Cleanup local dir if not keeping
    if not keep_project:
        try:
            import shutil as _sh
            _sh.rmtree(project_dir, ignore_errors=True)
        except Exception:
            pass

    return result
