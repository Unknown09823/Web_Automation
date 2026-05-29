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


@router.get("/templates")
async def list_templates(
    request: Request,
    limit: int = 50,
    _: str = Depends(auth_required),
) -> dict:
    """Compact view of learned page templates the agent can replay.

    A "template" here is a page-signature pattern that the AI brain has
    encountered, plus the selectors it has learned for that pattern. When a
    template's confidence is high enough, the agent reuses it instead of
    paying the AI token cost again — that's the replay-first / AI-as-fallback
    behavior the planner already implements (see ``ActionPlanner._step_for``).

    Returns ``{"templates": [...], "stats": {...}}`` so callers (CLI,
    dashboard, Telegram bot) can render the same data without scraping the
    other ``/ai/memory/*`` endpoints.
    """
    ctx = get_context(request)
    if not ctx.brain or not ctx.brain.memory:
        return {"templates": [], "stats": {}, "enabled": False}

    pages = await ctx.brain.memory.known_pages(limit=limit)
    stats = await ctx.brain.memory.workflow_stats()
    templates: list[dict] = []
    # Common intents we want to surface counts for. Keep small to avoid
    # hammering memory for every page.
    probe_intents = (
        "login", "register", "username_field", "email_field",
        "password_field", "submit", "search",
    )
    for p in pages:
        sig = p.get("page_sig") or p.get("page_signature")
        if not sig:
            continue
        learned: dict[str, dict] = {}
        for intent in probe_intents:
            sels = await ctx.brain.memory.get_selectors(sig, intent, limit=1)
            if not sels:
                continue
            best = sels[0]
            learned[intent] = {
                "selector": best.selector,
                "strategy": best.strategy,
                "confidence": round(best.confidence, 3),
                "success_count": best.success_count,
                "fail_count": best.fail_count,
            }
        templates.append({
            "page_signature": sig,
            "title": p.get("title"),
            "url_pattern": p.get("url_pattern"),
            "seen_count": p.get("seen_count", 0),
            "last_seen": p.get("last_seen", 0),
            "intents": learned,
            "intent_count": len(learned),
        })

    return {"templates": templates, "stats": stats, "enabled": True}
