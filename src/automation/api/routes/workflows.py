"""Workflow management routes.

Workflows are dispatched as background asyncio tasks. When an
``account_id`` is provided, the runner:

* fetches the account from :class:`AccountManager` and exposes its fields
  to the workflow as ``${account.id}``, ``${account.number}``,
  ``${account.username}``, ``${account.email}``, ``${account.password}``,
  and ``${account.metadata.<key>}`` for any custom metadata,
* derives :class:`BrowserOverrides` from ``account.metadata`` (proxy,
  user_agent, viewport, locale, timezone_id, geolocation,
  extra_http_headers, permissions, color_scheme, device_scale_factor,
  profile_id) and passes them to :py:meth:`BrowserManager.get_or_create`,
* records the run as an account result (with workflow name and duration).

The same primitives drive the matrix runner exposed at
``POST /workflows/{name}/run_for_accounts`` — it iterates over a list of
account IDs (sequentially by default so accounts that share a ``profile_id``
do not collide).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

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
    """Run a workflow asynchronously against one account (or none).

    Body::

        {"account_id": "<id>", "inputs": {...}}

    When ``account_id`` is provided:

    * the account's data is exposed to the workflow as ``${account.*}``,
    * ``account.metadata`` controls the per-session browser overrides
      (proxy, user-agent, viewport, locale, timezone, geolocation,
      headers, permissions, profile_id), and
    * the run is recorded under the account's result history.

    Otherwise the workflow runs without a browser session, useful for
    non-browser steps (logs, set, branch, etc.).
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


@router.post("/{name}/run_for_accounts")
async def run_for_accounts(
    name: str,
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict:
    """Run one workflow once per account in a list.

    Body::

        {
          "account_ids": ["a", "b", ...],
          "inputs": {...},
          "parallel": false,
          "max_parallel": 1,
          "stop_on_failure": false
        }

    Defaults are sequential (``parallel=false``). Sequential is required for
    detection-matrix tests where multiple accounts share a ``profile_id``,
    because Chromium does not allow concurrent use of the same profile dir.

    When ``parallel=true``, ``max_parallel`` (default 4) bounds concurrency.
    Each account still gets its own :class:`BrowserSession`; pass distinct
    profile dirs across accounts to avoid lock contention.

    When ``stop_on_failure=true`` (sequential mode only), the runner halts
    after the first failed account.
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

    account_ids = payload.get("account_ids") or []
    if not isinstance(account_ids, list) or not account_ids:
        raise HTTPException(status_code=400, detail="account_ids must be a non-empty list")

    inputs = payload.get("inputs") or {}
    parallel = bool(payload.get("parallel", False))
    max_parallel = max(1, int(payload.get("max_parallel", 4)))
    stop_on_failure = bool(payload.get("stop_on_failure", False))
    ctx.record_audit(
        actor, "workflow.batch", f"{name} accounts={len(account_ids)} parallel={parallel}",
    )

    asyncio.create_task(
        _run_batch_bg(
            ctx, wf, account_ids, inputs,
            parallel=parallel, max_parallel=max_parallel,
            stop_on_failure=stop_on_failure,
        ),
        name=f"wf-batch:{name}",
    )
    return {
        "status": "started",
        "workflow": name,
        "accounts": account_ids,
        "parallel": parallel,
        "max_parallel": max_parallel,
        "stop_on_failure": stop_on_failure,
    }


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


# ----------------------------------------------------------------- helpers
def _account_inputs(account: Any) -> dict[str, Any]:
    """Project an Account dataclass into a workflow-friendly dict.

    The password is deliberately included because workflows pass it to
    ``ai_goal`` for form filling. It never leaves the local process.
    """
    return {
        "id": account.id,
        "number": account.number,
        "username": account.username,
        "email": account.email,
        "password": account.password,
        "metadata": dict(account.metadata or {}),
    }


def _scrub_account_for_log(account_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip secrets before recording the run."""
    return {k: ("***" if k == "password" else v) for k, v in account_dict.items()}


async def _open_session_for_account(ctx, account_id: str, account):
    """Open or reuse a browser session honoring account.metadata overrides."""
    if not ctx.browser:
        return None
    from automation.browser.manager import BrowserOverrides

    overrides = BrowserOverrides.from_metadata(account.metadata if account else None)
    session = await ctx.browser.get_or_create(account_id, overrides=overrides)
    return session


def _record_run(ctx, payload: dict[str, Any]) -> None:
    ctx.workflow_results.append(payload)
    # cap the in-memory ring buffer
    if len(ctx.workflow_results) > 500:
        del ctx.workflow_results[: len(ctx.workflow_results) - 500]


# ----------------------------------------------------------------- background
async def _run_workflow_bg(
    ctx, wf, account_id: str | None, inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run a single workflow against an optional account.

    Returns a result-summary dict (also appended to ``ctx.workflow_results``)
    so :func:`_run_batch_bg` can aggregate across many accounts.
    """
    page = None
    started_at = time.time()
    inputs = dict(inputs or {})
    account = None
    summary: dict[str, Any] = {
        "workflow": wf.name,
        "account_id": account_id,
        "status": "failed",
        "started_at": started_at,
    }

    try:
        if account_id and ctx.accounts:
            account = ctx.accounts.get(account_id)
            if not account:
                raise RuntimeError(f"account not found: {account_id}")
            inputs.setdefault("account", _account_inputs(account))
            summary["account"] = _scrub_account_for_log(inputs["account"])

        if account_id and ctx.browser:
            session = await _open_session_for_account(ctx, account_id, account)
            if session:
                page = session.page
                summary["profile_id"] = session.profile_id
                summary["proxy"] = session.proxy

        result = await ctx.workflow_engine.run(wf, page=page, inputs=inputs)
        summary.update({
            "status": result.status.value,
            "duration_ms": result.duration_ms,
            "steps": len(result.records),
            "ended_at": time.time(),
        })
        # store the full result for the dashboard
        full_result = result.to_dict()
        full_result.update({
            "account_id": account_id,
            "profile_id": summary.get("profile_id"),
            "proxy": summary.get("proxy"),
        })
        _record_run(ctx, full_result)

        if account_id and ctx.accounts:
            if result.status.value == "succeeded":
                await ctx.accounts.mark_completed(
                    account_id, workflow=wf.name, started_at=started_at,
                    result={"steps": len(result.records)},
                )
            else:
                last_err = ""
                for r in reversed(result.records):
                    if r.error:
                        last_err = r.error
                        break
                await ctx.accounts.mark_failed(
                    account_id, last_err or "workflow failed",
                    workflow=wf.name, started_at=started_at,
                )
        return summary
    except Exception as exc:  # noqa: BLE001
        log.exception("workflow background run failed account=%s wf=%s",
                      account_id, wf.name)
        summary.update({"status": "error", "error": repr(exc), "ended_at": time.time()})
        _record_run(ctx, summary)
        if account_id and ctx.accounts:
            await ctx.accounts.mark_failed(
                account_id, repr(exc), workflow=wf.name, started_at=started_at,
            )
        return summary


async def _run_batch_bg(
    ctx, wf, account_ids: list[str], inputs: dict[str, Any],
    *,
    parallel: bool, max_parallel: int, stop_on_failure: bool,
) -> None:
    """Drive ``_run_workflow_bg`` across a list of accounts.

    Sequential by default. In parallel mode, a semaphore bounds concurrency
    so accounts with their own profile dirs don't overwhelm the host.
    """
    summaries: list[dict[str, Any]] = []
    if not parallel:
        for aid in account_ids:
            res = await _run_workflow_bg(ctx, wf, aid, dict(inputs))
            summaries.append(res)
            if stop_on_failure and res.get("status") not in ("succeeded",):
                log.warning("batch halted at account=%s status=%s",
                            aid, res.get("status"))
                break
    else:
        sem = asyncio.Semaphore(max_parallel)

        async def _one(aid: str) -> dict[str, Any]:
            async with sem:
                return await _run_workflow_bg(ctx, wf, aid, dict(inputs))

        results = await asyncio.gather(
            *(_one(aid) for aid in account_ids), return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException):
                summaries.append({"status": "error", "error": repr(r)})
            else:
                summaries.append(r)

    batch_summary = {
        "workflow": wf.name,
        "kind": "batch",
        "total": len(account_ids),
        "succeeded": sum(1 for s in summaries if s.get("status") == "succeeded"),
        "failed": sum(1 for s in summaries if s.get("status") not in ("succeeded",)),
        "ended_at": time.time(),
        "accounts": [
            {
                "account_id": s.get("account_id"),
                "status": s.get("status"),
                "profile_id": s.get("profile_id"),
                "proxy": s.get("proxy"),
                "duration_ms": s.get("duration_ms"),
                "error": s.get("error"),
            }
            for s in summaries
        ],
    }
    _record_run(ctx, batch_summary)
    log.info(
        "batch %s done total=%d succeeded=%d failed=%d",
        wf.name, batch_summary["total"],
        batch_summary["succeeded"], batch_summary["failed"],
    )
