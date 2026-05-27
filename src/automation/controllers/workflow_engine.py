"""Declarative workflow engine.

See ``automation.controllers.__init__`` for the supported step grammar.
Workflows are pure data: they can come from JSON, YAML, the API, or the
dashboard, and they never embed website-specific logic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from automation.ai.brain import AIBrain

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class WorkflowStep:
    """One step in a declarative workflow."""

    type: str
    name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    on_error: str = "fail"  # fail | continue | retry
    retries: int = 0
    timeout_ms: int = 30_000
    if_: str | None = None  # simple expression on context vars

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "params": self.params,
            "on_error": self.on_error,
            "retries": self.retries,
            "timeout_ms": self.timeout_ms,
            "if": self.if_,
        }


@dataclass(slots=True)
class Workflow:
    name: str
    steps: list[WorkflowStep]
    description: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputs": self.inputs,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass(slots=True)
class StepRecord:
    step: WorkflowStep
    status: WorkflowStatus
    duration_ms: int = 0
    error: str | None = None
    output: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step.to_dict(),
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "output": _safe(self.output),
        }


@dataclass(slots=True)
class WorkflowResult:
    workflow: str
    status: WorkflowStatus
    duration_ms: int
    records: list[StepRecord] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "context": _safe(self.context),
            "records": [r.to_dict() for r in self.records],
        }


def load_workflow(path: str | Path) -> Workflow:
    """Load a workflow from JSON or YAML."""
    p = Path(path)
    text = p.read_text()
    if p.suffix.lower() in (".yml", ".yaml"):
        if yaml is None:
            raise RuntimeError("YAML workflow requires PyYAML")
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    steps_raw = data.get("steps") or []
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
        for s in steps_raw
    ]
    return Workflow(
        name=data.get("name", p.stem),
        description=data.get("description", ""),
        inputs=data.get("inputs", {}) or {},
        steps=steps,
    )


class WorkflowEngine:
    """Run a declarative workflow against a Playwright page."""

    def __init__(self, brain: "AIBrain | None" = None) -> None:
        self.brain = brain

    async def run(
        self,
        workflow: Workflow,
        page: Any | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        ctx: dict[str, Any] = {**workflow.inputs, **(inputs or {}), "_found": {}}
        records: list[StepRecord] = []
        started = time.time()
        i = 0
        log.info("workflow start name=%s steps=%d", workflow.name, len(workflow.steps))
        while i < len(workflow.steps):
            step = workflow.steps[i]
            if step.if_ and not _eval_condition(step.if_, ctx):
                records.append(StepRecord(step=step, status=WorkflowStatus.SKIPPED))
                i += 1
                continue
            rec = await self._run_step(step, ctx, page)
            records.append(rec)
            if rec.status == WorkflowStatus.FAILED and step.on_error == "fail":
                duration = int((time.time() - started) * 1000)
                log.warning("workflow halted at step=%s", step.name)
                return WorkflowResult(
                    workflow=workflow.name,
                    status=WorkflowStatus.FAILED,
                    duration_ms=duration,
                    records=records,
                    context=ctx,
                )
            # branch step may set ctx["_jump"] to an index
            jump = ctx.pop("_jump", None)
            if isinstance(jump, int):
                i = jump
            else:
                i += 1
        duration = int((time.time() - started) * 1000)
        return WorkflowResult(
            workflow=workflow.name,
            status=WorkflowStatus.SUCCEEDED,
            duration_ms=duration,
            records=records,
            context=ctx,
        )

    # ---------------------------------------------------------------- per-step
    async def _run_step(
        self, step: WorkflowStep, ctx: dict[str, Any], page: Any | None
    ) -> StepRecord:
        attempts = 0
        last_error: str | None = None
        last_output: Any = None
        while True:
            attempts += 1
            started = time.time()
            try:
                last_output = await asyncio.wait_for(
                    self._dispatch(step, ctx, page), timeout=step.timeout_ms / 1000
                )
                return StepRecord(
                    step=step, status=WorkflowStatus.SUCCEEDED,
                    duration_ms=int((time.time() - started) * 1000),
                    output=last_output,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = repr(exc)
                log.warning("step %s attempt %d failed: %s", step.name, attempts, exc)
                if attempts <= step.retries:
                    await asyncio.sleep(min(2 ** attempts, 10))
                    continue
                if step.on_error == "continue":
                    return StepRecord(
                        step=step, status=WorkflowStatus.SKIPPED,
                        duration_ms=int((time.time() - started) * 1000),
                        error=last_error,
                    )
                return StepRecord(
                    step=step, status=WorkflowStatus.FAILED,
                    duration_ms=int((time.time() - started) * 1000),
                    error=last_error,
                )

    async def _dispatch(
        self, step: WorkflowStep, ctx: dict[str, Any], page: Any | None
    ) -> Any:
        t = step.type
        p = step.params
        if t == "navigate":
            _require_page(page)
            url = _interpolate(p.get("url", ""), ctx)
            await page.goto(url, timeout=step.timeout_ms)
            return {"url": url}
        if t == "analyze":
            _require_page(page)
            if not self.brain:
                raise RuntimeError("analyze step requires AI brain")
            snap = await self.brain.analyze(page, screenshot=p.get("screenshot", True))
            ctx["_snapshot"] = snap.to_dict()
            return ctx["_snapshot"]
        if t == "find":
            _require_page(page)
            if not self.brain:
                raise RuntimeError("find step requires AI brain")
            snap = await self.brain.analyze(page, screenshot=False)
            intent = p.get("intent", "")
            elements = snap.by_intent(intent, min_score=float(p.get("min_score", 0.4)))
            if not elements:
                raise RuntimeError(f"no element matched intent={intent}")
            best = elements[0]
            ctx["_found"][p.get("save_as", intent)] = best.to_dict()
            return best.to_dict()
        if t == "act":
            _require_page(page)
            return await self._act(page, p, ctx, step.timeout_ms)
        if t == "ai_goal":
            _require_page(page)
            if not self.brain:
                raise RuntimeError("ai_goal step requires AI brain")
            decision = await self.brain.run(
                page,
                goal=p.get("goal", ""),
                inputs={k: _interpolate(v, ctx) for k, v in (p.get("inputs") or {}).items()},
            )
            return decision.to_dict()
        if t == "wait":
            _require_page(page)
            if "selector" in p:
                sel = _interpolate(p["selector"], ctx)
                await page.wait_for_selector(sel, timeout=step.timeout_ms)
            elif "load_state" in p:
                await page.wait_for_load_state(p["load_state"], timeout=step.timeout_ms)
            else:
                await asyncio.sleep(float(p.get("seconds", 1)))
            return True
        if t == "screenshot":
            _require_page(page)
            path = _interpolate(p.get("path", "data/screenshots/wf-{ts}.png"), ctx)
            path = path.replace("{ts}", str(int(time.time() * 1000)))
            await page.screenshot(path=path, full_page=bool(p.get("full_page", False)))
            return {"path": path}
        if t == "verify":
            _require_page(page)
            return await self._verify(page, p, ctx)
        if t == "branch":
            cond = p.get("if", "")
            if _eval_condition(cond, ctx):
                ctx["_jump"] = int(p.get("goto", 0))
            return {"branched": "_jump" in ctx}
        if t == "set":
            for k, v in (p.get("vars") or {}).items():
                ctx[k] = _interpolate(v, ctx)
            return p.get("vars", {})
        if t == "log":
            log.info("workflow.log %s", _interpolate(p.get("message", ""), ctx))
            return None
        raise ValueError(f"unknown step type: {t}")

    async def _act(
        self, page: Any, p: dict[str, Any], ctx: dict[str, Any], timeout: int
    ) -> Any:
        action = p.get("action", "click")
        selector = _interpolate(p.get("selector"), ctx) if p.get("selector") else None
        if not selector and "from_found" in p:
            found = ctx["_found"].get(p["from_found"])
            if found:
                selector = found.get("selector")
        value = _interpolate(p.get("value"), ctx) if p.get("value") is not None else None
        if action == "click":
            await page.click(selector, timeout=timeout)
        elif action == "fill":
            await page.fill(selector, value or "", timeout=timeout)
        elif action == "select":
            await page.select_option(selector, value, timeout=timeout)
        elif action == "check":
            await page.check(selector, timeout=timeout)
        elif action == "press":
            await page.press(selector, value, timeout=timeout)
        elif action == "type":
            await page.type(selector, value or "", timeout=timeout)
        else:
            raise ValueError(f"unknown act action: {action}")
        return {"action": action, "selector": selector}

    async def _verify(self, page: Any, p: dict[str, Any], ctx: dict[str, Any]) -> Any:
        out: dict[str, Any] = {}
        if "url_contains" in p:
            url = page.url
            needle = _interpolate(p["url_contains"], ctx)
            ok = needle in url
            out["url"] = {"value": url, "ok": ok, "needle": needle}
            if not ok:
                raise AssertionError(f"url does not contain {needle!r}: {url}")
        if "title_contains" in p:
            title = await page.title()
            needle = _interpolate(p["title_contains"], ctx)
            ok = needle in title
            out["title"] = {"value": title, "ok": ok, "needle": needle}
            if not ok:
                raise AssertionError(f"title mismatch: {title}")
        if "selector_visible" in p:
            sel = _interpolate(p["selector_visible"], ctx)
            visible = await page.is_visible(sel)
            out["visible"] = {"selector": sel, "ok": visible}
            if not visible:
                raise AssertionError(f"selector not visible: {sel}")
        return out


# --------------------------------------------------------------------- helpers
def _require_page(page: Any | None) -> None:
    if page is None:
        raise RuntimeError("step requires a Playwright page")


def _interpolate(value: Any, ctx: dict[str, Any]) -> Any:
    """Replace ``${var}`` and ``${nested.key}`` with values from ``ctx``."""
    if not isinstance(value, str):
        return value
    out = value
    while "${" in out:
        start = out.index("${")
        end = out.index("}", start)
        if end == -1:
            break
        expr = out[start + 2 : end]
        out = out[:start] + str(_resolve(expr, ctx)) + out[end + 1 :]
    return out


def _resolve(expr: str, ctx: dict[str, Any]) -> Any:
    node: Any = ctx
    for part in expr.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return ""
    return node


def _eval_condition(expr: str, ctx: dict[str, Any]) -> bool:
    """Evaluate a tiny safe subset: ``var == 'x'``, ``var != 'y'``, ``var``."""
    expr = expr.strip()
    for op in ("==", "!="):
        if op in expr:
            left, right = (s.strip() for s in expr.split(op, 1))
            l_val = _resolve(left, ctx) if not (left.startswith(("'", '"'))) else left.strip("'\"")
            r_val = _resolve(right, ctx) if not (right.startswith(("'", '"'))) else right.strip("'\"")
            return (l_val == r_val) if op == "==" else (l_val != r_val)
    return bool(_resolve(expr, ctx))


def _safe(value: Any) -> Any:
    """Truncate large values for safe JSON output."""
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "...<truncated>"
    return value
