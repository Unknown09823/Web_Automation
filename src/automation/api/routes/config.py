"""Configuration view + reload routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/config", tags=["config"])

# Keys whose values must never appear in API responses.
_SECRET_KEYS = {"password", "token", "secret", "api_key", "telegram_token"}


def _redact(node):
    if isinstance(node, dict):
        return {
            k: ("***" if any(s in k.lower() for s in _SECRET_KEYS) else _redact(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_redact(v) for v in node]
    return node


@router.get("")
async def get_config(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    return {"config": _redact(ctx.engine.config.as_dict())}


@router.post("/reload")
async def reload_config(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    ctx.record_audit(actor, "config.reload")
    await ctx.engine.config.reload()
    return {"reloaded": True}
