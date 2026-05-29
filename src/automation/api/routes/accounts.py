"""Account management routes.

Route ordering note
-------------------
FastAPI matches routes in registration order. The specific paths
(``/status``, ``/completed``, ``/failed``, ``/rejected``, ``/results``,
``/reload``) are registered **before** the dynamic ``/{account_id}`` route
so they are not swallowed by the path parameter.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from automation.accounts.manager import AccountStatus
from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _scrub(account) -> dict:
    """Strip the password from an account dict before returning it."""
    d = account.to_dict()
    d.pop("password", None)
    return d


# ----------------------------------------------------------------- collection
@router.get("")
async def list_accounts(
    request: Request,
    status_filter: str | None = None,
    limit: int = 200,
    _: str = Depends(auth_required),
) -> dict:
    """List accounts. Optional ``status_filter`` matches an ``AccountStatus``."""
    ctx = get_context(request)
    if not ctx.accounts:
        return {"accounts": [], "stats": {}}
    if status_filter:
        try:
            s = AccountStatus(status_filter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid status") from exc
        accounts = ctx.accounts.by_status(s, limit=limit)
    else:
        accounts = ctx.accounts.all()[:limit]
    return {
        "stats": ctx.accounts.progress(),
        "accounts": [_scrub(a) for a in accounts],
    }


# ------------------------------------------------------------------ summaries
@router.get("/status")
async def accounts_status(request: Request, _: str = Depends(auth_required)) -> dict:
    """Aggregate status snapshot for the dashboard / Telegram bot."""
    ctx = get_context(request)
    if not ctx.accounts:
        return {"configured": False}
    progress = ctx.accounts.progress()
    return {
        "configured": True,
        "source_file": str(ctx.accounts.source_file),
        "last_loaded_at": ctx.accounts.last_loaded_at,
        "last_load_count": ctx.accounts.last_load_count,
        "rejected_count": ctx.accounts.last_rejected_count,
        "lease_seconds": ctx.accounts.lease_seconds,
        "max_attempts": ctx.accounts.max_attempts,
        "progress": progress,
    }


@router.get("/completed")
async def accounts_completed(
    request: Request,
    limit: int = 200,
    _: str = Depends(auth_required),
) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        return {"accounts": []}
    rows = ctx.accounts.by_status(AccountStatus.COMPLETED, limit=limit)
    return {"count": len(rows), "accounts": [_scrub(a) for a in rows]}


@router.get("/failed")
async def accounts_failed(
    request: Request,
    limit: int = 200,
    _: str = Depends(auth_required),
) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        return {"accounts": []}
    rows = ctx.accounts.by_status(AccountStatus.FAILED, limit=limit)
    return {"count": len(rows), "accounts": [_scrub(a) for a in rows]}


@router.get("/rejected")
async def accounts_rejected(
    request: Request,
    limit: int = 200,
    _: str = Depends(auth_required),
) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        return {"rejected": []}
    return {"rejected": ctx.accounts.rejected(limit=limit)}


@router.get("/results")
async def accounts_results(
    request: Request,
    account_id: str | None = None,
    limit: int = 100,
    _: str = Depends(auth_required),
) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        return {"results": []}
    return {"results": ctx.accounts.results(account_id=account_id, limit=limit)}


@router.post("/reload")
async def reload_accounts(request: Request, actor: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    n = await ctx.accounts.reload()
    ctx.record_audit(actor, "accounts.reload", str(n))
    return {
        "loaded": n,
        "rejected": ctx.accounts.last_rejected_count,
        "stats": ctx.accounts.stats(),
    }


@router.post("/locks/reap")
async def reap_locks(request: Request, actor: str = Depends(auth_required)) -> dict:
    """Force-clear expired account locks. Useful after a worker crash."""
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    n = await ctx.accounts.reset_stuck_locks()
    ctx.record_audit(actor, "accounts.locks.reap", str(n))
    return {"released": n}


# ------------------------------------------------------------------ per-account
# Registered *after* the static routes above so they don't capture
# ``/status`` etc. as account IDs.
@router.get("/{account_id}")
async def get_account(account_id: str, request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    acc = ctx.accounts.get(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")
    return {"account": _scrub(acc)}


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


@router.post("/{account_id}/release")
async def release_lock(account_id: str, request: Request, actor: str = Depends(auth_required)) -> dict:
    """Release the worker lock on a single account without changing status."""
    ctx = get_context(request)
    if not ctx.accounts:
        raise HTTPException(status_code=404, detail="account manager not configured")
    ok = await ctx.accounts.release_lock(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="account not found or not locked")
    ctx.record_audit(actor, "account.release", account_id)
    return {"released": True}
