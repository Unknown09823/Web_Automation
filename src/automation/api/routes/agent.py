"""Agent API routes: REST + SSE for the autonomous browser agent.

Endpoints:
  POST /agent/plan         - Parse NL instruction -> plan preview
  POST /agent/run          - Execute a plan (or instruction directly)
  GET  /agent/runs         - List runs
  GET  /agent/runs/{id}    - Run summary + state
  GET  /agent/runs/{id}/events - SSE stream of live events
  GET  /agent/runs/{id}/replay/{account_id} - Replay JSON
  POST /agent/runs/{id}/resume - Resume from checkpoint
  POST /agent/runs/{id}/cancel - Cancel a running execution
  POST /agent/chat         - Conversational endpoint
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/agent", tags=["agent"])
log = logging.getLogger(__name__)


@router.post("/plan")
async def create_plan(
    request: Request,
    payload: dict = Body(...),
    actor: str = Depends(auth_required),
) -> dict:
    """Parse a natural language instruction into an execution plan preview."""
    from automation.agent.nl_planner import NLPlanner

    instruction = payload.get("instruction", "").strip()
    if not instruction:
        raise HTTPException(400, "instruction is required")

    planner = NLPlanner()
    plan = await planner.parse(instruction)

    return {
        "plan": plan.to_dict(),
        "preview": {
            "instruction": instruction,
            "goals": [g.to_dict() for g in plan.goals],
            "account_count": plan.account_config.count,
            "target_url": plan.target_url,
            "estimated_time_seconds": plan.estimated_time_seconds,
            "parallel": plan.parallel,
            "notes": plan.notes,
        },
    }



@router.post("/run")
async def start_run(
    request: Request,
    payload: dict = Body(...),
    actor: str = Depends(auth_required),
) -> dict:
    """Start an agent execution.

    Body options:
      - {"instruction": "..."} - parse NL and execute
      - {"goals": [...], "account_ids": [...], "target_url": "..."}
      - {"plan": <plan_dict>} - execute a previously generated plan

    Returns the real ``run_id`` synchronously (no race) and schedules the
    actual work as a background task.
    """
    ctx = get_context(request)
    agent = _get_agent(ctx)

    instruction = payload.get("instruction", "")
    plan_data = payload.get("plan")
    goals_raw = payload.get("goals")
    account_ids = payload.get("account_ids")
    target_url = payload.get("target_url", "")
    parallel = payload.get("parallel", False)
    max_parallel = payload.get("max_parallel")

    from automation.agent.data_factory import DataFactory
    from automation.agent.goals import AgentGoal
    from automation.agent.nl_planner import NLPlanner

    generated_preview: list[dict] = []

    # Option 1: Natural language instruction
    if instruction and not goals_raw and not plan_data:
        planner = NLPlanner()
        plan = await planner.parse(instruction)
        goals = plan.goals
        target_url = target_url or plan.target_url

        if not account_ids:
            factory = DataFactory()
            ac = plan.account_config
            generated = factory.generate(
                ac.count,
                password=ac.password,
                generate_numbers=ac.generate_numbers,
                number_length=ac.number_length,
                generate_emails=ac.generate_emails,
                generate_usernames=ac.generate_usernames,
            )
            if ctx.accounts:
                await _register_generated_accounts(ctx, generated)
            account_ids = [a.id for a in generated]
            # Return non-secret preview (no password) so caller can audit
            generated_preview = [
                {k: v for k, v in a.to_dict().items() if k != "password"}
                for a in generated
            ]

        # Honor explicit payload overrides for parallel / max_parallel.
        if "parallel" not in payload:
            parallel = plan.parallel
        if max_parallel is None:
            max_parallel = plan.max_parallel

    elif plan_data:
        goals = [AgentGoal.from_dict(g) for g in plan_data.get("goals", [])]
        target_url = target_url or plan_data.get("target_url", "")
        if not account_ids:
            account_ids = plan_data.get("accounts") or ["default"]

    elif goals_raw:
        goals = [AgentGoal.from_dict(g) for g in goals_raw]
        if not account_ids:
            account_ids = ["default"]

    else:
        raise HTTPException(400, "Provide instruction, plan, or goals")

    if not goals:
        raise HTTPException(400, "Could not derive any goals from input")
    if not account_ids:
        raise HTTPException(400, "No account IDs provided or derived")

    # Synchronously create the run so we know the real run_id, then
    # schedule the actual execution. No race, no 100ms sleep.
    run = agent.prepare_run(
        goals=goals,
        account_ids=list(account_ids),
        target_url=target_url,
        instruction=instruction,
        parallel=bool(parallel),
        max_parallel=int(max_parallel) if max_parallel else None,
    )
    asyncio.create_task(
        agent.execute_prepared(run), name=f"agent-run-{run.run_id}",
    )
    ctx.record_audit(actor, "agent.run", run.run_id)

    return {
        "status": "started",
        "run_id": run.run_id,
        "accounts": list(account_ids),
        "goals": len(goals),
        "target_url": target_url,
        "generated_accounts": generated_preview,
    }


@router.get("/runs")
async def list_runs(
    request: Request, limit: int = 50, _: str = Depends(auth_required)
) -> dict:
    """List recent agent runs."""
    from automation.agent.run import list_runs as _list_runs

    ctx = get_context(request)
    agent = _get_agent(ctx)
    stored = _list_runs(agent.runs_root, limit=limit)
    active = agent.active_runs
    return {"runs": stored, "active": active}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str, request: Request, _: str = Depends(auth_required)
) -> dict:
    """Get details of a specific run."""
    ctx = get_context(request)
    agent = _get_agent(ctx)

    if run_id in agent.active_runs:
        return {"run": agent.active_runs[run_id], "active": True}

    from automation.agent.run import RunContext
    run_dir = Path(agent.runs_root) / run_id
    if not run_dir.exists():
        raise HTTPException(404, f"run not found: {run_id}")
    run = RunContext.load(run_dir)
    return {"run": run.summary(), "active": False}



@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str, request: Request, _: str = Depends(auth_required)
) -> StreamingResponse:
    """SSE stream of live agent events for a run."""
    ctx = get_context(request)
    agent = _get_agent(ctx)

    async def event_generator():
        runs_root = Path(agent.runs_root)
        events_file = runs_root / run_id / "events.jsonl"
        sent = 0
        if events_file.exists():
            for line in events_file.read_text().splitlines():
                if line.strip():
                    yield f"data: {line}\n\n"
                    sent += 1

        while True:
            await asyncio.sleep(0.5)
            if events_file.exists():
                lines = events_file.read_text().splitlines()
                new_lines = lines[sent:]
                for line in new_lines:
                    if line.strip():
                        yield f"data: {line}\n\n"
                        sent += 1
                status_file = runs_root / run_id / "status.json"
                if status_file.exists():
                    try:
                        status = json.loads(status_file.read_text())
                        if status.get("status") in ("completed", "failed", "cancelled"):
                            yield f"data: {json.dumps({'type': 'stream.end', 'status': status.get('status')})}\n\n"
                            return
                    except json.JSONDecodeError:
                        pass
            else:
                if run_id not in agent.active_runs:
                    yield f"data: {json.dumps({'type': 'stream.end', 'status': 'not_found'})}\n\n"
                    return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}/replay/{account_id}")
async def get_replay(
    run_id: str, account_id: str, request: Request, _: str = Depends(auth_required)
) -> dict:
    """Get the replay.json for a specific account in a run."""
    ctx = get_context(request)
    agent = _get_agent(ctx)
    from automation.agent.run import RunContext
    run_dir = Path(agent.runs_root) / run_id
    if not run_dir.exists():
        raise HTTPException(404, "run not found")
    run = RunContext.load(run_dir)
    replay = run.load_replay(account_id)
    return {"run_id": run_id, "account_id": account_id, "replay": replay}


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str, request: Request, _: str = Depends(auth_required)
) -> dict:
    """Resume a run from its last checkpoint."""
    ctx = get_context(request)
    agent = _get_agent(ctx)
    run_dir = Path(agent.runs_root) / run_id
    if not run_dir.exists():
        raise HTTPException(404, "run not found")
    asyncio.create_task(agent.resume_run(str(run_dir)), name=f"agent-resume-{run_id}")
    return {"status": "resuming", "run_id": run_id}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str, request: Request, _: str = Depends(auth_required)
) -> dict:
    """Cancel a running agent execution."""
    ctx = get_context(request)
    agent = _get_agent(ctx)
    cancelled = await agent.cancel(run_id)
    if not cancelled:
        raise HTTPException(404, "run not active")
    return {"status": "cancelling", "run_id": run_id}


@router.post("/chat")
async def agent_chat(
    request: Request,
    payload: dict = Body(...),
    actor: str = Depends(auth_required),
) -> dict:
    """Conversational endpoint for the agent."""
    from automation.agent.nl_planner import NLPlanner

    message = payload.get("message", "").strip()
    if not message:
        raise HTTPException(400, "message is required")

    lower = message.lower()
    if any(cmd in lower for cmd in ["proceed", "execute", "go", "start", "run it"]):
        return {
            "reply": "Starting execution. Use POST /agent/run with your instruction to begin.",
            "action": "suggest_run",
        }

    planner = NLPlanner()
    plan = await planner.parse(message)
    return {
        "reply": _format_plan_reply(plan),
        "plan": plan.to_dict(),
        "action": "plan_ready",
    }


# ----------------------------------------------------------------- helpers

def _get_agent(ctx) -> Any:
    agent = getattr(ctx, "agent", None)
    if agent is None:
        raise HTTPException(503, "Agent not configured. Ensure AI brain is enabled.")
    return agent


async def _register_generated_accounts(ctx, generated) -> None:
    """Register auto-generated accounts via :py:meth:`AccountManager.add_accounts`.

    Goes through the manager's lock-protected ``add_accounts`` API instead of
    mutating ``source_file`` (which would race with the file-change watcher).
    Existing runtime state is preserved on conflicts.
    """
    raw = [a.to_dict() for a in generated]
    try:
        result = await ctx.accounts.add_accounts(raw)
        if result.rejected:
            log.warning(
                "auto-generated accounts rejected: count=%d first=%r",
                len(result.rejected), result.rejected[0][2],
            )
    except Exception:  # noqa: BLE001
        log.exception("failed to register generated accounts")


def _format_plan_reply(plan) -> str:
    lines = ["**Plan Generated:**", ""]
    lines.append(f"**Goal:** {plan.instruction}")
    lines.append("")
    lines.append("**Steps:**")
    for i, goal in enumerate(plan.goals, 1):
        lines.append(f"  {i}. {goal.description or goal.type.value}")
    lines.append("")
    lines.append(f"**Accounts:** {plan.account_config.count}")
    if plan.target_url:
        lines.append(f"**URL:** {plan.target_url}")
    lines.append(f"**Estimated time:** {plan.estimated_time_seconds:.0f}s")
    if plan.notes:
        lines.append("")
        lines.append("**Notes:**")
        for note in plan.notes:
            lines.append(f"  - {note}")
    lines.append("")
    lines.append("Say **proceed** to execute or provide corrections.")
    return "\n".join(lines)
