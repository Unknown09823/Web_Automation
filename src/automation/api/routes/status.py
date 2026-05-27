"""Status route: high-level engine status snapshot."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/status", tags=["status"])


@router.get("")
async def get_status(request: Request, _actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    health = ctx.engine.health.snapshot()
    return {
        "running": ctx.engine.running,
        "uptime_seconds": health.uptime_seconds,
        "components": health.components,
        "status": health.status,
        "plugins": [
            {
                "name": r.name,
                "enabled": r.enabled,
                "started": r.started,
                "version": r.metadata.get("version", ""),
                "error": r.error,
            }
            for r in ctx.engine.plugins.list_records()
        ],
        "scheduler": {
            "max_workers": ctx.engine.scheduler.max_workers,
            "jobs": [j.name for j in ctx.engine.scheduler.list_jobs()],
        },
        "queues": ctx.engine.queue_manager.stats(),
        "accounts": ctx.accounts.stats() if ctx.accounts else None,
        "browser_sessions": (
            list(ctx.browser.sessions.keys()) if ctx.browser else []
        ),
        "ai_enabled": bool(ctx.brain and ctx.brain.enabled),
    }
