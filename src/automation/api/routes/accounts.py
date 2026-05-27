"""Account management routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from automation.accounts.manager import AccountStatus
from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _scrub(account) -> dict:
    d = account.to_dict()
    d.pop("password", None)
    return d


@router.get("")
async def list_accounts(
    request: Request,
    status_filter: str | None = None,
    limit: int = 200,
    _: str = Depends(auth_required),
) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        return {"accounts": [], "stats": {}}
    accounts = ctx.accounts.all()
    if status_filter:
        try:
            s = AccountStatus(status_filter)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid status")
        accounts = [a for a in accounts if a.status == s]
    return {
        "stats": ctx.accounts.progress(),
        "accounts": [_scrub(a) for a in accounts[:limit]],
    }


@router.get("/{account_id}")
async def get_account(account_id: str, request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    acc = ctx.accounts.get(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    return {"account": _scrub(acc)}


@router.post("/reload")
async def reload_accounts(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    n = await ctx.accounts.reload()
    ctx.record_audit(actor, "accounts.reload", str(n))
    return {"loaded": n}


@router.post("/{account_id}/reset")
async def reset_account(account_id: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    if not ctx.accounts.get(account_id):
        raise HTTPException(status_code=404, detail="account not found")
    await ctx.accounts.reset(account_id)
    if ctx.browser:
        await ctx.browser.reset_profile(account_id)
    ctx.record_audit(actor, "account.reset", account_id)
    return {"reset": True}


@router.post("/{account_id}/pause")
async def pause_account(account_id: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    await ctx.accounts.pause(account_id)
    ctx.record_audit(actor, "account.pause", account_id)
    return {"paused": True}


@router.post("/{account_id}/resume")
async def resume_account(account_id: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    await ctx.accounts.resume(account_id)
    ctx.record_audit(actor, "account.resume", account_id)
    return {"resumed": True}
