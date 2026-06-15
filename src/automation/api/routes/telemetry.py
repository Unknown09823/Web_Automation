"""Telemetry retention API.

REST surface for the new operator-facing cleanup controls.

Endpoints:
  GET  /telemetry/settings
  POST /telemetry/settings
  GET  /telemetry/usage
  POST /telemetry/clear/screenshots
  POST /telemetry/clear/reasoning
  POST /telemetry/clear/logs
  POST /telemetry/clear/runs
  POST /telemetry/clear/chat
  POST /telemetry/apply

Both the Telegram bot and the dashboard call these. Keeping the
endpoints generic (a single body shape with optional ``run_id`` and
``account_id``) means we don't have to expand the surface every
time a new control is added — a /clear_screenshots button and
/clear_screenshots <run> [acc] command both go through the same
route with different bodies.

All operations are protected by the framework's standard
``auth_required`` token check. The retention manager itself is
defensive against missing directories and partial failures, so
handlers don't need to wrap calls in try blocks.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from automation.agent.telemetry_retention import (
    AUTO_DELETE_PRESETS_MIN,
    KEEP_RUNS_PRESETS,
    REASONING_PRESETS,
    SCREENSHOT_PRESETS,
    TelemetryRetention,
)
from automation.api.deps import auth_required, get_context

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
log = logging.getLogger(__name__)


# --------------------------------------------------------------- helpers
def _get_retention(ctx) -> TelemetryRetention:
    """Get-or-build a retention manager bound to the agent's runs root.

    We attach it to the context lazily on first access so existing
    deployments without retention data still work — the manager
    itself never writes anything until an operator triggers a
    cleanup or settings change.
    """
    existing: TelemetryRetention | None = getattr(ctx, "telemetry_retention", None)
    if existing is not None:
        return existing
    runs_root = "data/runs"
    agent = getattr(ctx, "agent", None)
    if agent is not None and getattr(agent, "runs_root", None):
        runs_root = agent.runs_root
    rr = TelemetryRetention(runs_root=runs_root)
    # Attach so the next request reuses the same manager (and its
    # cached settings). ``ctx`` is a dataclass(slots=True), so a
    # plain attribute set would fail; we go through ``object.__setattr__``
    # to allow extension.
    try:
        object.__setattr__(ctx, "telemetry_retention", rr)
    except (AttributeError, TypeError):
        # Slot dataclass — fall back to a per-call instance. The
        # JSON file on disk is the source of truth, so this isn't
        # a correctness issue, just a minor cache miss.
        pass
    return rr


# --------------------------------------------------------------- settings
@router.get("/settings")
async def get_settings(
    request: Request, _: str = Depends(auth_required),
) -> dict[str, Any]:
    """Return the persisted retention settings + UI presets.

    Presets are echoed back to the client so a future Telegram bot
    or dashboard can render the option grid without hard-coding the
    same numbers in two places.
    """
    ctx = get_context(request)
    rr = _get_retention(ctx)
    return {
        "settings": rr.settings.to_dict(),
        "presets": {
            "screenshot_limit": list(SCREENSHOT_PRESETS),
            "reasoning_limit": list(REASONING_PRESETS),
            "auto_delete_minutes": list(AUTO_DELETE_PRESETS_MIN),
            "keep_completed_runs": list(KEEP_RUNS_PRESETS),
        },
    }


@router.post("/settings")
async def update_settings(
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict[str, Any]:
    """Update one or more retention settings.

    Body accepts any subset of:
      * ``screenshot_limit`` (int; 0 = unlimited)
      * ``reasoning_limit``  (int; 0 = unlimited)
      * ``auto_delete_minutes`` (int; 0 = never)
      * ``keep_completed_runs`` (int; 0 = keep all)
    Unknown keys are dropped silently. The full normalized state
    after the merge is returned.
    """
    ctx = get_context(request)
    rr = _get_retention(ctx)
    new_settings = rr.update_settings(**payload)
    ctx.record_audit(actor, "telemetry.settings.update", str(payload))
    return {"settings": new_settings.to_dict()}


@router.post("/settings/reset")
async def reset_settings(
    request: Request, actor: str = Depends(auth_required),
) -> dict[str, Any]:
    """Restore the retention settings to defaults (= unlimited)."""
    ctx = get_context(request)
    rr = _get_retention(ctx)
    new_settings = rr.reset_settings()
    ctx.record_audit(actor, "telemetry.settings.reset", "")
    return {"settings": new_settings.to_dict()}


# --------------------------------------------------------------- usage
@router.get("/usage")
async def usage(
    request: Request, _: str = Depends(auth_required),
) -> dict[str, Any]:
    """Aggregate disk usage across all runs.

    Cheap — walks the run tree with ``stat`` calls only. Useful as
    the first thing a Telegram /storage command shows so the
    operator can decide whether cleanup is even warranted.
    """
    ctx = get_context(request)
    rr = _get_retention(ctx)
    return rr.usage_summary()


# --------------------------------------------------------------- clear ops
def _scope(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull (run_id, account_id) from a request body, with light validation.

    Empty strings → ``None`` so a Telegram client passing literal ``""``
    still gets a global sweep.
    """
    run_id = (payload.get("run_id") or None) or None
    if isinstance(run_id, str) and not run_id.strip():
        run_id = None
    account_id = (payload.get("account_id") or None) or None
    if isinstance(account_id, str) and not account_id.strip():
        account_id = None
    return run_id, account_id


def _opt_int(payload: dict[str, Any], key: str) -> int | None:
    """Coerce ``payload[key]`` to int or ``None``.

    Returns ``None`` when the key is absent or unparseable, so
    callers can use ``"`` to mean "use the persisted setting".
    """
    if key not in payload or payload[key] is None:
        return None
    try:
        return int(payload[key])
    except (TypeError, ValueError):
        raise HTTPException(400, f"invalid integer for {key!r}: {payload[key]!r}")


@router.post("/clear/screenshots")
async def clear_screenshots(
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict[str, Any]:
    """Trim screenshots.

    Body:
      ``run_id``      — optional, scope to a single run.
      ``account_id``  — optional, scope to a single account inside the run.
      ``keep_last``   — optional override for ``screenshot_limit``.
                        ``0`` deletes every screenshot in scope.
    """
    ctx = get_context(request)
    rr = _get_retention(ctx)
    run_id, account_id = _scope(payload)
    keep_last = _opt_int(payload, "keep_last")
    result = rr.clear_screenshots(
        run_id=run_id, account_id=account_id, keep_last=keep_last,
    )
    ctx.record_audit(
        actor, "telemetry.clear.screenshots",
        f"run={run_id or '*'} account={account_id or '*'} keep={keep_last}",
    )
    return result.to_dict()


@router.post("/clear/reasoning")
async def clear_reasoning(
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict[str, Any]:
    ctx = get_context(request)
    rr = _get_retention(ctx)
    run_id, account_id = _scope(payload)
    keep_last = _opt_int(payload, "keep_last")
    result = rr.clear_reasoning(
        run_id=run_id, account_id=account_id, keep_last=keep_last,
    )
    ctx.record_audit(
        actor, "telemetry.clear.reasoning",
        f"run={run_id or '*'} account={account_id or '*'} keep={keep_last}",
    )
    return result.to_dict()


@router.post("/clear/logs")
async def clear_logs(
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict[str, Any]:
    ctx = get_context(request)
    rr = _get_retention(ctx)
    run_id, account_id = _scope(payload)
    result = rr.clear_logs(run_id=run_id, account_id=account_id)
    ctx.record_audit(
        actor, "telemetry.clear.logs",
        f"run={run_id or '*'} account={account_id or '*'}",
    )
    return result.to_dict()


@router.post("/clear/runs")
async def clear_runs(
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict[str, Any]:
    """Delete entire run directories.

    Body:
      ``run_id``         — optional, target a specific run.
      ``keep_last``      — keep N most recent matching runs (default:
                            persisted ``keep_completed_runs``).
      ``only_completed`` — when true (default) protects active /
                            paused / failed runs from accidental
                            deletion.
    """
    ctx = get_context(request)
    rr = _get_retention(ctx)
    run_id, _ = _scope(payload)
    keep_last = _opt_int(payload, "keep_last")
    only_completed = bool(payload.get("only_completed", True))
    result = rr.clear_runs(
        keep_last=keep_last,
        only_completed=only_completed,
        only_run_id=run_id,
    )
    ctx.record_audit(
        actor, "telemetry.clear.runs",
        f"run={run_id or '*'} keep={keep_last} only_completed={only_completed}",
    )
    return result.to_dict()


@router.post("/clear/chat")
async def clear_chat(
    request: Request,
    payload: dict = Body(default_factory=dict),
    actor: str = Depends(auth_required),
) -> dict[str, Any]:
    """Composite clear: screenshots + reasoning + logs.

    Convenience for the Telegram /clear_chat button — operators
    rarely want to issue three separate cleanups when they just
    want to "make this run quiet".
    """
    ctx = get_context(request)
    rr = _get_retention(ctx)
    run_id, account_id = _scope(payload)
    result = rr.clear_chat(run_id=run_id, account_id=account_id)
    ctx.record_audit(
        actor, "telemetry.clear.chat",
        f"run={run_id or '*'} account={account_id or '*'}",
    )
    return result.to_dict()


@router.post("/apply")
async def apply_settings(
    request: Request, actor: str = Depends(auth_required),
) -> dict[str, Any]:
    """Apply persisted retention settings now (one-shot sweep).

    The same routine runs automatically when a periodic task is
    configured; this endpoint is for the dashboard "Apply now"
    button and the Telegram /clear_apply command.
    """
    ctx = get_context(request)
    rr = _get_retention(ctx)
    result = rr.apply_settings()
    ctx.record_audit(actor, "telemetry.apply", "")
    return result.to_dict()
