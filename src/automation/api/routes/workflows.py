"""Workflow management routes."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from automation.api.deps import auth_required, get_context
from automation.controllers.workflow_engine import (
    Workflow,
    WorkflowStep,
    load_workflow,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


@router.get("")
async def list_workflows(request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    out = []
    p = Path(ctx.workflows_dir)
    if p.exists():
        for f in sorted(p.iterdir()):
            if f.suffix.lower() in (".json", ".yaml", ".yml"):
                out.append(f.name)
    return {"directory": ctx.workflows_dir, "workflows": out}


@router.get("/{name}")
async def get_workflow(name: str, request: Request, _: str = Depends(auth_required)) -> dict:
    ctx = get_context(request)
    p = Path(ctx.workflows_dir) / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="workflow not found")
    try:
        wf = load_workflow(p)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"workflow": wf.to_dict()}


@router.post("/{name}/run")
async def run_workflow(
    name: str,
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict:
    """Run a workflow asynchronously.

    Body:
      ``{"account_id": "<id>", "inputs": {...}}``

    If ``account_id`` is provided and a browser manager exists, the workflow
    runs against that account's isolated session. Otherwise, it runs without
    a browser (useful for non-browser steps).
    """
    ctx = get_context(request)
    if not ctx.workflow_engine:
        raise HTTPException(status_code=503, detail="workflow engine not configured")
    p = Path(ctx.workflows_dir) / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="workflow not found")
    try:
        wf = load_workflow(p)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    account_id = payload.get("account_id")
    inputs = payload.get("inputs") or {}
    ctx.record_audit(actor, "workflow.run", name)

    asyncio.create_task(
        _run_workflow_bg(ctx, wf, account_id, inputs), name=f"wf:{name}"
    )
    return {"status": "started", "workflow": name, "account_id": account_id}


@router.post("/run_inline")
async def run_inline_workflow(
    request: Request,
    payload: dict = Body(...),
    actor: str = Depends(auth_required),
) -> dict:
    """Run a workflow whose steps are provided in the request body."""
    ctx = get_context(request)
    if not ctx.workflow_engine:
        raise HTTPException(status_code=503, detail="workflow engine not configured")
    try:
        steps = [
            WorkflowStep(
                type=s["type"],
                name=s.get("name", s["type"]),
                params=s.get("params", {}) or {},
                on_error=s.get("on_error", "fail"),
                retries=int(s.get("retries", 0)),
                timeout_ms=int(s.get("timeout_ms", 30_000)),
                if_=s.get("if"),
            )
            for s in payload.get("steps", [])
        ]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"missing field: {exc}") from exc
    wf = Workflow(
        name=payload.get("name", "inline"),
        description=payload.get("description", ""),
        inputs=payload.get("inputs", {}) or {},
        steps=steps,
    )
    ctx.record_audit(actor, "workflow.run_inline", wf.name)
    asyncio.create_task(_run_workflow_bg(ctx, wf, payload.get("account_id"), {}))
    return {"status": "started", "workflow": wf.name}


@router.get("/results/recent")
async def recent_results(request: Request, _: str = Depends(auth_required), limit: int = 50) -> dict:
    ctx = get_context(request)
    return {"results": ctx.workflow_results[-limit:]}


# ----------------------------------------------------------------- background
async def _run_workflow_bg(ctx, wf, account_id, inputs):
    page = None
    try:
        if account_id and ctx.browser:
            session = await ctx.browser.get_or_create(account_id)
            page = session.page
        result = await ctx.workflow_engine.run(wf, page=page, inputs=inputs)
        ctx.workflow_results.append(result.to_dict())
        if len(ctx.workflow_results) > 200:
            del ctx.workflow_results[: len(ctx.workflow_results) - 200]
        if account_id and ctx.accounts:
            if result.status.value == "succeeded":
                await ctx.accounts.mark_completed(account_id)
            else:
                await ctx.accounts.mark_failed(
                    account_id, str(result.records[-1].error if result.records else "unknown")
                )
    except Exception as exc:  # noqa: BLE001
        log.exception("workflow background run failed")
        ctx.workflow_results.append({"workflow": wf.name, "status": "failed", "error": str(exc)})
