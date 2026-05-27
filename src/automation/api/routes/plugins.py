"""Plugin management routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/plugins", tags=["plugins"])


@router.get("")
async def list_plugins(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    return {
        "plugins": [
            {
                "name": r.name,
                "enabled": r.enabled,
                "started": r.started,
                "module_path": r.module_path,
                "metadata": r.metadata,
                "error": r.error,
            }
            for r in ctx.engine.plugins.list_records()
        ]
    }


@router.post("/{name}/enable")
async def enable_plugin(name: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    ok = await ctx.engine.plugins.enable(name)
    if not ok:
        raise HTTPException(status_code=404, detail="plugin not found")
    ctx.record_audit(actor, "plugin.enable", name)
    return {"name": name, "enabled": True}


@router.post("/{name}/disable")
async def disable_plugin(name: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    ok = await ctx.engine.plugins.disable(name)
    if not ok:
        raise HTTPException(status_code=404, detail="plugin not found")
    ctx.record_audit(actor, "plugin.disable", name)
    return {"name": name, "enabled": False}


@router.post("/{name}/restart")
async def restart_plugin(name: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    ok = await ctx.engine.plugins.restart(name)
    if not ok:
        raise HTTPException(status_code=404, detail="plugin not found")
    ctx.record_audit(actor, "plugin.restart", name)
    return {"name": name, "restarted": True}


@router.post("/reload")
async def reload_all(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    await ctx.engine.plugins.reload_all()
    ctx.record_audit(actor, "plugin.reload_all")
    return {"reloaded": True}
