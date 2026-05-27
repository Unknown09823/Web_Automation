"""AI brain inspection routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/status")
async def ai_status(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.brain:
        return {"enabled": False}
    last = ctx.brain.last_decision
    return {
        "enabled": ctx.brain.enabled,
        "dry_run": ctx.brain.dry_run,
        "memory": ctx.brain.memory is not None,
        "last_decision": last.to_dict() if last else None,
    }


@router.get("/intents")
async def list_intents(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.brain:
        raise HTTPException(status_code=404, detail="AI brain not configured")
    return {
        "intents": [
            {
                "name": i.name,
                "description": i.description,
                "keywords": list(i.keywords),
                "roles": list(i.roles),
            }
            for i in ctx.brain.matcher.intents
        ]
    }


@router.get("/memory/pages")
async def memory_pages(request: Request, limit: int = 50, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.brain or not ctx.brain.memory:
        return {"pages": []}
    return {"pages": await ctx.brain.memory.known_pages(limit=limit)}


@router.get("/memory/stats")
async def memory_stats(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.brain or not ctx.brain.memory:
        return {"stats": {}}
    return {"stats": await ctx.brain.memory.workflow_stats()}


@router.get("/memory/selectors")
async def memory_selectors(
    request: Request,
    page_signature: str,
    intent: str,
    _: str = Depends(auth_required),
) -> dict:
    ctx = get_context(request)
    if not ctx.brain or not ctx.brain.memory:
        return {"selectors": []}
    res = await ctx.brain.memory.get_selectors(page_signature, intent)
    return {
        "selectors": [
            {
                "selector": s.selector,
                "strategy": s.strategy,
                "confidence": s.confidence,
                "success_count": s.success_count,
                "fail_count": s.fail_count,
            }
            for s in res
        ]
    }
