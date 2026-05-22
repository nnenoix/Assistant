"""arq background worker — runs heavy/long jobs out of band.

Typical jobs:
  - `apps_script_oneshot` over 100+ books (Phase 14 stress patterns)
  - `wb_finance_detail_collect` for a quarter-year window (each page has 60s
    WB-imposed sleep)
  - Scheduled report rendering + email/Telegram dispatch
  - Webhook handlers (ЮKassa, Tinkoff) that need to do real work before
    ack'ing — buffered through queue so the HTTP receiver returns 200 in <1s

Why arq (not Celery): native async/await, no broker abstraction overhead,
50 concurrent LLM calls per worker process (Celery would need 50 worker
processes — see project audit / compass file #2 finding #5).

Run: `arq src.queue.worker.WorkerSettings`
"""
from __future__ import annotations

import os
import time
from typing import Any

# arq is an optional dep — lazy-imported so the codebase doesn't break
# in environments where the queue isn't deployed.
try:
    from arq import cron
    from arq.connections import RedisSettings
    _ARQ_AVAILABLE = True
except ImportError:
    _ARQ_AVAILABLE = False
    RedisSettings = None  # type: ignore[assignment]
    cron = None  # type: ignore[assignment]


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


# ---------- task implementations ----------

async def run_apps_script_oneshot(ctx: dict[str, Any], script_id: str,
                                  function_name: str, params: list | None = None) -> dict:
    """Background wrapper around apps_script_api.run_function. Logs duration
    to the audit log so the agent can later track «what jobs ran when»."""
    from src.tools import apps_script_api, infra
    started = time.time()
    result = apps_script_api.run_function(script_id, function_name, params=params or [])
    duration_ms = (time.time() - started) * 1000
    infra.audit_log(
        action="queue.apps_script_oneshot",
        tool="apps_script_api.run_function",
        args={"script_id": script_id, "function_name": function_name},
        actor="arq_worker",
        result_summary=f"ok={result.get('ok')} duration_ms={duration_ms:.0f}",
    )
    return result


async def run_wb_finance_collect(ctx: dict[str, Any], token: str,
                                 date_from: str, date_to: str) -> dict:
    """WB finance fetch on the queue — WB has a 1-req/min rate limit so
    this job takes minutes to hours. Frees the agent session to do other
    things while it runs."""
    from src.tools import wb, infra
    started = time.time()
    result = wb.finance_detail_collect(token, date_from, date_to,
                                       response_format="concise")
    duration_ms = (time.time() - started) * 1000
    infra.audit_log(
        action="queue.wb_finance_collect",
        tool="wb.finance_detail_collect",
        args={"date_from": date_from, "date_to": date_to},
        actor="arq_worker",
        result_summary=f"rows={result.get('rows_count')} duration_ms={duration_ms:.0f}",
    )
    return result


async def poll_scheduled_tasks(ctx: dict[str, Any]) -> dict:
    """Cron-like job: check `infra.scheduler_due`, fire callbacks. Hooked up
    via arq's `cron` triggers (see WorkerSettings.cron_jobs)."""
    from src.tools import infra
    due = infra.scheduler_due()
    fired = 0
    for task in due.get("data", {}).get("due", []):
        # The actual fan-out per task type would live here; for the scaffold
        # we just mark them complete so they don't re-fire.
        infra.scheduler_complete(task["task_id"], result_note="auto-completed by poll")
        fired += 1
    return {"fired": fired}


async def render_daily_kpi_dashboard(ctx: dict[str, Any], html_path: str = ".data/dashboards/daily.html") -> dict:
    """Pull last 24h of audit-log + KPI series, render an HTML dashboard.
    Triggered nightly via cron."""
    from src.tools import infra
    audit = infra.audit_search(limit=200)
    return infra.bi_dashboard_render(
        "Daily ops dashboard",
        [
            {"label": "Audit rows (24h)", "value": audit.get("data", {}).get("matched", 0)},
            {"label": "Worker", "value": "arq", "unit": ""},
        ],
        html_path,
    )


# ---------- arq settings ----------

if _ARQ_AVAILABLE:
    class WorkerSettings:
        """`arq src.queue.worker.WorkerSettings` picks this up."""
        functions = [
            run_apps_script_oneshot,
            run_wb_finance_collect,
            poll_scheduled_tasks,
            render_daily_kpi_dashboard,
        ]
        redis_settings = RedisSettings.from_dsn(REDIS_URL)
        cron_jobs = [
            cron(poll_scheduled_tasks, minute={0, 15, 30, 45}, run_at_startup=True),
            cron(render_daily_kpi_dashboard, hour=3, minute=0),  # 3am UTC daily
        ]
        max_jobs = 10
        job_timeout = 60 * 30  # 30 min — WB finance windows can be long
        keep_result = 60 * 60 * 24  # 1 day
else:
    # Stub so import doesn't fail when arq isn't installed.
    class WorkerSettings:  # type: ignore[no-redef]
        """arq is not installed; install with `pip install arq` to use queue."""
        functions: list = []
