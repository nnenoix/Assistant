"""In-process infrastructure helpers: MDM tables, approvals, audit log,
BI dashboard render, scheduler hints, skill registry, ZPL print labels.

These are local-file-backed primitives that the agent can use to build
team workflows without spinning up Postgres/Redis. Storage paths under
`.data/infra/`. Each function returns {ok, data | result, _meta}.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import DATA_DIR


_INFRA_DIR = DATA_DIR / "infra"
_INFRA_DIR.mkdir(parents=True, exist_ok=True)
_MDM_DIR = _INFRA_DIR / "mdm"
_MDM_DIR.mkdir(parents=True, exist_ok=True)
_APPROVALS_PATH = _INFRA_DIR / "approvals.jsonl"
_AUDIT_PATH = _INFRA_DIR / "audit.jsonl"
_BI_DIR = _INFRA_DIR / "bi"
_BI_DIR.mkdir(parents=True, exist_ok=True)
_SCHED_PATH = _INFRA_DIR / "scheduler.jsonl"
_SKILLS_PATH = _INFRA_DIR / "skills.jsonl"


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# MDM (Master Data Management) tables
# ============================================================
# Each MDM table is a JSON file under .data/infra/mdm/<table>.json
# carrying a list of {id, external_ids:{wb_nm, ozon_sku, ymarket_id, ...},
# canonical_name, attributes:{...}}. The agent merges marketplace IDs into
# a single product / supplier / contractor identity so cross-source joins
# don't need a fresh LLM dedup pass every time.

def mdm_table_get(table: str) -> dict:
    """Read entire MDM table. table examples: products, suppliers, contractors."""
    path = _MDM_DIR / f"{table}.json"
    if not path.exists():
        return {"ok": True, "data": {"table": table, "records": [], "count": 0}}
    records = json.loads(path.read_text(encoding="utf-8"))
    return {"ok": True, "data": {"table": table, "records": records, "count": len(records)}}


def mdm_record_upsert(table: str, record_id: str, fields: dict,
                      external_ids: dict | None = None) -> dict:
    """Insert or merge an MDM record by id. external_ids carries marketplace
    cross-refs (wb_nm, ozon_sku, etc.). Existing fields are merged shallowly."""
    path = _MDM_DIR / f"{table}.json"
    records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    rec = next((r for r in records if r.get("id") == record_id), None)
    if rec is None:
        rec = {"id": record_id, "external_ids": external_ids or {}, "fields": dict(fields), "created_at": _now_iso(), "updated_at": _now_iso()}
        records.append(rec)
        action = "created"
    else:
        rec["fields"].update(fields)
        if external_ids:
            rec.setdefault("external_ids", {}).update(external_ids)
        rec["updated_at"] = _now_iso()
        action = "updated"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "data": {"id": record_id, "action": action}}


def mdm_resolve(table: str, external_key: str, external_value: str) -> dict:
    """Find a record by its external id. Example: find product by wb_nm.

    Returns the FIRST match (MDM should be 1:1 after deduplication)."""
    path = _MDM_DIR / f"{table}.json"
    if not path.exists():
        return {"ok": True, "data": {"found": False}}
    records = json.loads(path.read_text(encoding="utf-8"))
    for r in records:
        if r.get("external_ids", {}).get(external_key) == external_value:
            return {"ok": True, "data": {"found": True, "record": r}}
    return {"ok": True, "data": {"found": False}}


def mdm_delete(table: str, record_id: str) -> dict:
    """Remove a record by id."""
    path = _MDM_DIR / f"{table}.json"
    if not path.exists():
        return {"ok": False, "error": f"no table {table!r}"}
    records = json.loads(path.read_text(encoding="utf-8"))
    n_before = len(records)
    records = [r for r in records if r.get("id") != record_id]
    if len(records) == n_before:
        return {"ok": False, "error": f"id {record_id!r} not found"}
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "data": {"deleted": record_id}}


# ============================================================
# Approval workflows
# ============================================================
# Append-only JSONL of approval requests / decisions. The agent stages a
# destructive op as `pending`, a human (or another agent) flips it to
# `approved` / `denied`, then the agent re-runs the op when approved.

def approval_request(action: str, args: dict,
                     requested_by: str = "agent", reason: str | None = None) -> dict:
    """Stage an approval request. Returns {approval_id}. Use the id to
    later check `approval_status` and, if approved, run the real action."""
    aid = uuid.uuid4().hex
    record = {
        "approval_id": aid,
        "status": "pending",
        "action": action,
        "args": args,
        "requested_by": requested_by,
        "reason": reason,
        "requested_at": _now_iso(),
        "decided_at": None,
        "decided_by": None,
    }
    _append_jsonl(_APPROVALS_PATH, record)
    return {"ok": True, "data": record}


def approval_decide(approval_id: str, status: str, decided_by: str = "user",
                    note: str | None = None) -> dict:
    """Approve or deny a pending request. status: approved | denied."""
    if status not in {"approved", "denied"}:
        return {"ok": False, "error": "status must be approved|denied"}
    records = _read_jsonl(_APPROVALS_PATH)
    target = next((r for r in records if r.get("approval_id") == approval_id), None)
    if target is None:
        return {"ok": False, "error": f"approval_id {approval_id!r} not found"}
    if target["status"] != "pending":
        return {"ok": False, "error": f"already {target['status']!r}"}
    decision = {
        **target,
        "status": status,
        "decided_at": _now_iso(),
        "decided_by": decided_by,
        "note": note,
    }
    _append_jsonl(_APPROVALS_PATH, decision)
    return {"ok": True, "data": decision}


def approval_status(approval_id: str) -> dict:
    """Latest status of an approval."""
    records = _read_jsonl(_APPROVALS_PATH)
    matches = [r for r in records if r.get("approval_id") == approval_id]
    if not matches:
        return {"ok": False, "error": "not found"}
    return {"ok": True, "data": matches[-1]}


def approval_list(status: str | None = None, limit: int = 50) -> dict:
    """List recent approvals. status: pending | approved | denied | any."""
    records = _read_jsonl(_APPROVALS_PATH)
    # Reduce to latest per approval_id
    latest: dict[str, dict] = {}
    for r in records:
        latest[r["approval_id"]] = r
    items = list(latest.values())
    if status and status != "any":
        items = [r for r in items if r["status"] == status]
    items.sort(key=lambda r: r.get("decided_at") or r.get("requested_at") or "", reverse=True)
    return {"ok": True, "data": {"approvals": items[:limit], "total": len(items)}}


# ============================================================
# Audit log — every destructive action
# ============================================================

def audit_log(action: str, tool: str, args: dict,
              actor: str = "agent", result_summary: str | None = None,
              correlation_id: str | None = None) -> dict:
    """Append an audit row. Caller decides when to log (typically inside
    destructive tools just before/after the API call). correlation_id ties
    multiple log entries to one user request."""
    record = {
        "ts": _now_iso(),
        "actor": actor,
        "action": action,
        "tool": tool,
        "args_summary": {k: (v if isinstance(v, (int, float, bool)) else str(v)[:200]) for k, v in args.items()},
        "result_summary": (result_summary or "")[:300] if result_summary else None,
        "correlation_id": correlation_id or uuid.uuid4().hex[:8],
    }
    _append_jsonl(_AUDIT_PATH, record)
    return {"ok": True, "data": {"audit_id": record["correlation_id"]}}


def audit_search(actor: str | None = None, tool: str | None = None,
                 action: str | None = None, since_iso: str | None = None,
                 limit: int = 100) -> dict:
    """Search audit log by actor / tool / action / since timestamp. Returns
    latest-first up to `limit`."""
    records = _read_jsonl(_AUDIT_PATH)
    if actor:
        records = [r for r in records if r.get("actor") == actor]
    if tool:
        records = [r for r in records if r.get("tool") == tool]
    if action:
        records = [r for r in records if r.get("action") == action]
    if since_iso:
        records = [r for r in records if (r.get("ts") or "") >= since_iso]
    records.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return {"ok": True, "data": {"rows": records[:limit], "matched": len(records)}}


# ============================================================
# BI dashboard render — HTML
# ============================================================

def bi_dashboard_render(title: str, kpis: list[dict], html_path: str) -> dict:
    """Render a one-page HTML dashboard. kpis = [{label, value, delta?, unit?}].
    Writes to `html_path`. Self-contained — no external CSS/JS. Returns
    {path, bytes}."""
    def _card(k: dict) -> str:
        lbl = k.get("label", "")
        val = k.get("value", "")
        unit = (" " + k["unit"]) if k.get("unit") else ""
        delta_html = ('<div class="d">' + str(k["delta"]) + "</div>") if "delta" in k else ""
        return (
            "<div class='kpi'>"
            f"<div class='lbl'>{lbl}</div>"
            f"<div class='val'>{val}{unit}</div>"
            f"{delta_html}"
            "</div>"
        )
    cards = "\n".join(_card(k) for k in kpis)
    html = f"""<!doctype html><meta charset=utf-8><title>{title}</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;background:#0f1115;color:#eaeaea}}
h1{{margin:0 0 20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}}
.kpi{{background:#1a1d24;border-radius:10px;padding:18px}}
.lbl{{font-size:12px;color:#888;text-transform:uppercase;letter-spacing:.05em}}
.val{{font-size:28px;font-weight:600;margin-top:6px}}
.d{{font-size:13px;color:#88e}}
</style>
<h1>{title}</h1>
<div class=grid>{cards}</div>
"""
    Path(html_path).parent.mkdir(parents=True, exist_ok=True)
    Path(html_path).write_text(html, encoding="utf-8")
    size = Path(html_path).stat().st_size
    return {"ok": True, "data": {"path": html_path, "bytes": size, "kpi_count": len(kpis)}}


def bi_kpi_history_log(name: str, value: float, ts: str | None = None,
                       tags: dict | None = None) -> dict:
    """Append a KPI value to history. Use to build trend charts. ts ISO8601
    (default: now)."""
    record = {"name": name, "value": value, "ts": ts or _now_iso(), "tags": tags or {}}
    _append_jsonl(_INFRA_DIR / "kpi_history.jsonl", record)
    return {"ok": True, "data": record}


def bi_kpi_history_get(name: str, limit: int = 1000) -> dict:
    """Recent KPI history."""
    records = _read_jsonl(_INFRA_DIR / "kpi_history.jsonl")
    records = [r for r in records if r.get("name") == name][-limit:]
    return {"ok": True, "data": {"points": records, "count": len(records)}}


# ============================================================
# Scheduler hints — tasks the agent wants to revisit later
# ============================================================

def scheduler_enqueue(task: str, run_at_iso: str, payload: dict | None = None) -> dict:
    """Record a scheduled task for the agent to come back to. This is a
    hint, not an actual scheduler — the harness needs to poll. Returns
    {task_id}."""
    record = {
        "task_id": uuid.uuid4().hex,
        "task": task,
        "run_at": run_at_iso,
        "payload": payload or {},
        "status": "pending",
        "created_at": _now_iso(),
    }
    _append_jsonl(_SCHED_PATH, record)
    return {"ok": True, "data": record}


def scheduler_due(until_iso: str | None = None) -> dict:
    """List pending tasks whose run_at is ≤ until_iso (default: now)."""
    cutoff = until_iso or _now_iso()
    records = _read_jsonl(_SCHED_PATH)
    # Reduce to latest status per task_id
    latest: dict[str, dict] = {}
    for r in records:
        latest[r["task_id"]] = r
    due = [r for r in latest.values()
           if r["status"] == "pending" and (r.get("run_at") or "") <= cutoff]
    due.sort(key=lambda r: r.get("run_at") or "")
    return {"ok": True, "data": {"due": due, "count": len(due)}}


def scheduler_complete(task_id: str, result_note: str | None = None) -> dict:
    """Mark a task done."""
    records = _read_jsonl(_SCHED_PATH)
    target = next((r for r in records if r.get("task_id") == task_id), None)
    if target is None:
        return {"ok": False, "error": "task_id not found"}
    record = {**target, "status": "completed", "completed_at": _now_iso(), "result_note": result_note}
    _append_jsonl(_SCHED_PATH, record)
    return {"ok": True, "data": record}


def scheduler_cancel(task_id: str) -> dict:
    """Cancel a pending task."""
    records = _read_jsonl(_SCHED_PATH)
    target = next((r for r in records if r.get("task_id") == task_id), None)
    if target is None:
        return {"ok": False, "error": "task_id not found"}
    record = {**target, "status": "cancelled", "cancelled_at": _now_iso()}
    _append_jsonl(_SCHED_PATH, record)
    return {"ok": True, "data": record}


# ============================================================
# Skill registry — pluggable «навыки» the agent advertises
# ============================================================

def skill_register(name: str, description: str, tools: list[str],
                   tags: list[str] | None = None) -> dict:
    """Register a named skill — a bundle of tool names plus prose. The agent
    can later call `skill_list` to see what high-level capabilities are
    declared (e.g. «sla_monitor» = check WB+Ozon queues every hour)."""
    record = {
        "name": name,
        "description": description,
        "tools": list(tools),
        "tags": list(tags or []),
        "created_at": _now_iso(),
    }
    _append_jsonl(_SKILLS_PATH, record)
    return {"ok": True, "data": record}


def skill_list(tag: str | None = None) -> dict:
    """List registered skills. Optional tag filter."""
    records = _read_jsonl(_SKILLS_PATH)
    latest: dict[str, dict] = {}
    for r in records:
        latest[r["name"]] = r  # last write wins
    items = list(latest.values())
    if tag:
        items = [r for r in items if tag in (r.get("tags") or [])]
    return {"ok": True, "data": {"skills": items, "count": len(items)}}


def skill_remove(name: str) -> dict:
    """Remove a skill from the registry."""
    records = _read_jsonl(_SKILLS_PATH)
    if not any(r.get("name") == name for r in records):
        return {"ok": False, "error": f"skill {name!r} not found"}
    _append_jsonl(_SKILLS_PATH, {"name": name, "removed_at": _now_iso(), "tombstone": True})
    return {"ok": True, "data": {"removed": name}}


# ============================================================
# Label printing (ZPL / TSPL)
# ============================================================

def zpl_render_label(template: str, fields: dict, out_path: str) -> dict:
    """Substitute `{field}` placeholders in a ZPL template and write to disk.
    Send the resulting file to a Zebra ZPL printer (LAN/USB) via standard
    print queue."""
    raw = template
    for k, v in fields.items():
        raw = raw.replace("{" + k + "}", str(v))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(raw, encoding="utf-8")
    return {"ok": True, "data": {"path": out_path, "bytes": len(raw.encode("utf-8")),
                                 "fields_filled": list(fields.keys())}}


def tspl_render_label(template: str, fields: dict, out_path: str) -> dict:
    """Same as zpl_render_label but for TSPL (Godex / TSC) printers."""
    raw = template
    for k, v in fields.items():
        raw = raw.replace("{" + k + "}", str(v))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(raw, encoding="utf-8")
    return {"ok": True, "data": {"path": out_path, "bytes": len(raw.encode("utf-8")),
                                 "fields_filled": list(fields.keys())}}


def zpl_render_wb_label(barcode: str, sku: str, supplier: str, weight_g: int,
                       out_path: str) -> dict:
    """Pre-baked WB FBS shipping label template."""
    template = (
        "^XA\n"
        "^FO50,30^A0N,40,40^FDWB FBS^FS\n"
        "^FO50,80^A0N,30,30^FDSKU: {sku}^FS\n"
        "^FO50,120^A0N,30,30^FDПоставщик: {supplier}^FS\n"
        "^FO50,160^A0N,30,30^FDВес: {weight_g} г^FS\n"
        "^FO50,210^BCN,80,Y,N,N^FD{barcode}^FS\n"
        "^XZ\n"
    )
    return zpl_render_label(template, {
        "barcode": barcode, "sku": sku, "supplier": supplier, "weight_g": weight_g,
    }, out_path)
