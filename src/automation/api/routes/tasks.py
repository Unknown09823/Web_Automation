"""Task management routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from automation.api.deps import auth_required, get_context
from automation.core.task_manager import TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(
    request: Request,
    status_filter: str | None = None,
    _: str = Depends(auth_required),
) -> dict:
    ctx = get_context(request)
    sf = TaskStatus(status_filter) if status_filter else None
    tasks = ctx.engine.task_manager.list(sf)
    return {
        "tasks": [
            {
                "id": t.id, "name": t.name, "status": t.status.value,
                "attempts": t.attempts, "error": t.error,
                "started_at": t.started_at, "completed_at": t.completed_at,
            }
            for t in tasks
        ]
    }


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    t = ctx.engine.task_manager.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "id": t.id, "name": t.name, "status": t.status.value, "attempts": t.attempts,
        "error": t.error, "result": str(t.result)[:1000] if t.result is not None else None,
        "started_at": t.started_at, "completed_at": t.completed_at,
        "metadata": t.metadata,
    }


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    ok = await ctx.engine.task_manager.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="task not running")
    ctx.record_audit(actor, "task.cancel", task_id)
    return {"cancelled": True}
