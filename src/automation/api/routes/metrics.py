"""Metrics route: combined system + framework metrics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("")
async def metrics_json(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    health = ctx.engine.health.snapshot()
    accounts_stats = ctx.accounts.stats() if ctx.accounts else {}
    tm = ctx.engine.task_manager
    tasks_by_status: dict[str, int] = {}
    for t in tm.tasks.values():
        tasks_by_status[t.status.value] = tasks_by_status.get(t.status.value, 0) + 1
    return {
        "system": {
            "cpu_percent": health.cpu_percent,
            "memory_percent": health.memory_percent,
            "memory_used_mb": health.memory_used_mb,
            "disk_percent": health.disk_percent,
            "uptime_seconds": health.uptime_seconds,
            "status": health.status,
        },
        "tasks": {"by_status": tasks_by_status, "total": len(tm.tasks)},
        "accounts": accounts_stats,
        "queues": ctx.engine.queue_manager.stats(),
        "browser_sessions": (
            len(ctx.browser.sessions) if ctx.browser else 0
        ),
        "workflow_runs_recent": len(ctx.workflow_results),
    }


@router.get("/prometheus", response_class=Response)
async def metrics_prometheus(request: Request, _: str = Depends(auth_required)) -> Response:
    """Expose key gauges in Prometheus text format."""
    ctx = get_context(request)
    h = ctx.engine.health.snapshot()
    lines: list[str] = [
        "# HELP automation_cpu_percent CPU percent",
        "# TYPE automation_cpu_percent gauge",
        f"automation_cpu_percent {h.cpu_percent}",
        "# HELP automation_memory_percent Memory percent",
        "# TYPE automation_memory_percent gauge",
        f"automation_memory_percent {h.memory_percent}",
        "# HELP automation_uptime_seconds Process uptime",
        "# TYPE automation_uptime_seconds counter",
        f"automation_uptime_seconds {h.uptime_seconds}",
        "# HELP automation_running 1 if engine running",
        "# TYPE automation_running gauge",
        f"automation_running {1 if ctx.engine.running else 0}",
    ]
    if ctx.accounts:
        for k, v in ctx.accounts.stats().items():
            lines.append(f'automation_accounts{{status="{k}"}} {v}')
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
