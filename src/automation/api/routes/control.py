"""Control route: start / stop / restart / reload commands."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/control", tags=["control"])
log = logging.getLogger(__name__)


@router.post("/start")
async def start(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if ctx.engine.running:
        return {"status": "already_running"}
    ctx.record_audit(actor, "engine.start")
    asyncio.create_task(ctx.engine.start())
    return {"status": "starting"}


@router.post("/stop")
async def stop(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.engine.running:
        return {"status": "already_stopped"}
    ctx.record_audit(actor, "engine.stop")
    asyncio.create_task(ctx.engine.stop())
    return {"status": "stopping"}


@router.post("/restart")
async def restart(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    ctx.record_audit(actor, "engine.restart")
    asyncio.create_task(ctx.engine.restart())
    return {"status": "restarting"}


@router.post("/reload")
async def reload(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    ctx.record_audit(actor, "engine.reload")
    try:
        await ctx.engine.reload()
    except Exception as exc:  # noqa: BLE001
        log.exception("reload failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "reloaded"}


@router.get("/audit")
async def audit(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    return {"entries": ctx.audit[-200:]}
