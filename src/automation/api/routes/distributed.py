"""Distributed coordinator HTTP endpoints (worker protocol)."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Body, Depends, Request

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/distributed", tags=["distributed"])


def _coord(ctx):
    coord = getattr(ctx, "coordinator", None)
    if not coord:
        # lazy attach
        from automation.distributed.coordinator import Coordinator
        coord = Coordinator()
        ctx.coordinator = coord  # type: ignore[attr-defined]
    return coord


@router.get("/status")
async def coord_status(request: Request, _: str = Depends(auth_required)) -> dict:
    return _coord(get_context(request)).to_dict()


@router.post("/register")
async def register(
    request: Request,
    payload: dict = Body(...),
    _: str = Depends(auth_required),
) -> dict:
    coord = _coord(get_context(request))
    w = await coord.register_worker(
        host=payload.get("host", "unknown"),
        capabilities=payload.get("capabilities") or [],
    )
    return {"worker": asdict(w)}


@router.post("/heartbeat")
async def heartbeat(
    request: Request,
    payload: dict = Body(...),
    _: str = Depends(auth_required),
) -> dict:
    coord = _coord(get_context(request))
    ok = await coord.heartbeat(
        payload.get("worker_id", ""), metrics=payload.get("metrics") or {}
    )
    return {"ok": ok}


@router.post("/deregister")
async def deregister(
    request: Request,
    payload: dict = Body(...),
    _: str = Depends(auth_required),
) -> dict:
    coord = _coord(get_context(request))
    await coord.deregister(payload.get("worker_id", ""))
    return {"deregistered": True}


@router.post("/submit")
async def submit_assignment(
    request: Request,
    payload: dict = Body(...),
    _: str = Depends(auth_required),
) -> dict:
    coord = _coord(get_context(request))
    a = await coord.submit(
        kind=payload.get("kind", "task"),
        payload=payload.get("payload", {}) or {},
    )
    return {"assignment": {"id": a.id, "kind": a.kind, "status": a.status}}


@router.post("/claim")
async def claim(
    request: Request,
    payload: dict = Body(...),
    _: str = Depends(auth_required),
) -> dict:
    coord = _coord(get_context(request))
    a = await coord.claim(payload.get("worker_id", ""))
    return {"assignment": {"id": a.id, "kind": a.kind, "payload": a.payload} if a else None}


@router.post("/report")
async def report(
    request: Request,
    payload: dict = Body(...),
    _: str = Depends(auth_required),
) -> dict:
    coord = _coord(get_context(request))
    ok = await coord.report(
        worker_id=payload.get("worker_id", ""),
        assignment_id=payload.get("assignment_id", ""),
        status=payload.get("status", "done"),
        info=payload.get("info") or {},
    )
    return {"ok": ok}
