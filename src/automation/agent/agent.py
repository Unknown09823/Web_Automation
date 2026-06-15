"""BrowserAgent: the autonomous outer loop.

Ties together all agent subsystems into a persistent, goal-driven
execution engine that:
  - Decomposes high-level goals into sub-goals
  - Executes each sub-goal using the deterministic-first stack
    (replay → site memory → heuristics → rules → AI → human handoff)
  - Waits adaptively between steps
  - Recovers from failures using the recovery stack
  - Verifies success using multi-signal verification
  - Records every action for replay
  - Checkpoints progress for resume capability
  - Emits events for live dashboard observation

Each account gets its own isolated browser session, its own
:class:`ReasoningLog`, its own :class:`StepLoop`, and a private
view onto the shared :class:`DeterministicEngine`. The engine's
caches (templates, site memory) are deliberately shared so account
#2 benefits from what account #1 learned.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from automation.agent.checkpoints import Checkpoint, CheckpointManager
from automation.agent.deterministic_engine import DeterministicEngine
from automation.agent.events import AgentEvent, AgentEventType
from automation.agent.goals import (
    AgentGoal,
    GoalDecomposer,
    GoalStatus,
    GoalType,
)
from automation.agent.heuristics import Heuristics
from automation.agent.loop import StepLoop, StepLoopResult
from automation.agent.popup_guard import PopupGuard
from automation.agent.reasoning import ReasoningLog
from automation.agent.recorder import ActionRecorder
from automation.agent.recovery import RecoveryStack
from automation.agent.rule_engine import RuleEngine
from automation.agent.run import RunContext, RunStatus
from automation.agent.site_memory import SiteMemory
from automation.agent.site_templates import TemplateStore
from automation.agent.verifier import SuccessVerifier
from automation.agent.waiter import AdaptiveWaiter

log = logging.getLogger(__name__)



@dataclass(slots=True)
class AccountRun:
    """Tracks one account's execution within a run."""

    account_id: str
    goals: list[AgentGoal]
    current_goal_index: int = 0
    status: str = "pending"  # pending | running | completed | failed
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "current_goal_index": self.current_goal_index,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "goals_total": len(self.goals),
        }


class BrowserAgent:
    """Autonomous browser agent — the main outer loop.

    Usage:
        agent = BrowserAgent(brain=brain, browser=browser, event_bus=bus)
        run_ctx = await agent.execute(
            goals=[...],
            accounts=["acc_001", "acc_002"],
            target_url="https://example.com/signup",
        )
    """

    def __init__(
        self,
        *,
        brain: Any = None,
        browser: Any = None,
        accounts_manager: Any = None,
        event_bus: Any = None,
        runs_root: str = "data/runs",
        max_parallel: int = 4,
        # ----- v2 deterministic-first stack ---------------------------
        # Pass any subset of these in to override the defaults. When a
        # component is None and ``deterministic_first`` is True the
        # agent constructs a sensible default itself, so the call site
        # only needs to opt out by setting ``deterministic_first=False``
        # to get the legacy brain-driven flow.
        deterministic_first: bool = True,
        site_memory: SiteMemory | None = None,
        template_store: TemplateStore | None = None,
        popup_guard: PopupGuard | None = None,
        rule_engine: RuleEngine | None = None,
        heuristics: Heuristics | None = None,
        deterministic_engine: DeterministicEngine | None = None,
        # Path roots for the new stores; defaults under data/learning/.
        site_memory_root: str = "data/learning/sites",
        template_store_root: str = "data/learning/templates",
        # User-supplied rules JSON file; loaded on top of built-ins.
        rules_path: str | None = None,
        site_memory: Any = None,
        llm: Any = None,
        adaptive_executor: Any = None,
    ) -> None:
        self.brain = brain
        self.browser = browser
        self.accounts_manager = accounts_manager
        self.event_bus = event_bus
        self.runs_root = runs_root
        self.max_parallel = max_parallel
        # site_memory: SiteMemory — per-domain self-learning store. Optional.
        # llm:         LLMBackend — used for fuzzy goal decomposition + the
        #              supervisor's recovery brainstorming. Optional.
        # adaptive_executor: AdaptiveExecutor — replay-first / AI-fallback
        #              dispatcher. When set, every goal first tries to
        #              replay a previously-learned ExecutionTemplate
        #              before invoking the brain. This is the token-cost
        #              optimisation that turns "1 LLM call per goal per
        #              account" into "1 LLM call per goal across N
        #              accounts". Optional — when None, every goal runs
        #              through the brain (the original behaviour).
        self.site_memory = site_memory
        self.llm = llm
        self.adaptive = adaptive_executor

        self.waiter = AdaptiveWaiter()
        self.verifier = SuccessVerifier()
        self.decomposer = GoalDecomposer()
        self.recovery = RecoveryStack(brain=brain)

        # ----- deterministic-first stack -------------------------------
        # All four stores are best-instantiated lazily so test harnesses
        # don't pay the cost when they don't use the agent. The flag
        # ``self.deterministic_first`` ultimately decides whether
        # ``_attempt_goal`` routes a non-navigate goal through the
        # StepLoop (engine-driven) or through the legacy brain.run flow.
        self.deterministic_first = bool(deterministic_first)
        self.site_memory: SiteMemory | None = site_memory
        self.template_store: TemplateStore | None = template_store
        self.popup_guard: PopupGuard | None = popup_guard
        self.rule_engine: RuleEngine | None = rule_engine
        self.heuristics: Heuristics | None = heuristics
        self.deterministic_engine: DeterministicEngine | None = deterministic_engine

        if self.deterministic_first:
            if self.site_memory is None:
                self.site_memory = SiteMemory(root=site_memory_root)
            if self.template_store is None:
                self.template_store = TemplateStore(root=template_store_root)
            if self.popup_guard is None:
                self.popup_guard = PopupGuard()
            if self.rule_engine is None:
                self.rule_engine = RuleEngine()
                if rules_path:
                    try:
                        self.rule_engine.load(rules_path)
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "agent: failed to load rules from %s", rules_path,
                            exc_info=True,
                        )
            if self.heuristics is None:
                self.heuristics = Heuristics()
            if self.deterministic_engine is None:
                self.deterministic_engine = DeterministicEngine(
                    rules=self.rule_engine,
                    heuristics=self.heuristics,
                    site_memory=self.site_memory,
                    templates=self.template_store,
                    brain=self.brain,
                )

        # Form filler — always available regardless of deterministic_first.
        # The executor will use it for all FILL actions when present.
        from automation.agent.form_filler import FormFiller
        self.form_filler = FormFiller()

        # Obstruction detector — applied to every CLICK that goes
        # through the executor when present. We share the agent's
        # PopupGuard so the detector's "ask the popup engine to
        # dismiss the blocker" path uses the same rule catalog the
        # observer ran in the loop's pre-think phase. This makes the
        # error-recovery story symmetric: whatever the popup_guard
        # would have dismissed up front, it can still dismiss
        # mid-step when an overlay sneaks in.
        from automation.agent.obstruction import ObstructionDetector
        self.obstruction_detector = ObstructionDetector(
            popup_guard=self.popup_guard,
        )

        # Form engine — orchestrator over Heuristics + FormFiller. Not
        # currently invoked by the loop directly; exposed on the agent
        # so plugins / workflows that want a one-shot
        # "discover-and-fill-everything" call have a high-level API
        # without re-deriving classification rules per caller.
        from automation.agent.form_engine import FormEngine
        self.form_engine = FormEngine(
            heuristics=self.heuristics,
            form_filler=self.form_filler,
        )

        # Captcha handler — available for the loop's recovery path.
        from automation.agent.captcha_handler import CaptchaHandler
        self.captcha_handler = CaptchaHandler()

        # AI cost tracker — shared across all accounts and runs.
        from automation.agent.cost_tracker import CostTracker
        self.cost_tracker = CostTracker(
            persist_path=Path("data/state/cost_tracker.json"),
        )
        self.cost_tracker.load()  # restore from disk if available

        self._active_runs: dict[str, RunContext] = {}
        self._cancelled: set[str] = set()
        # Pause/resume per run. Event is *set* when running, cleared when
        # paused — _run_account awaits between goals via _await_unpaused.
        self._pause_events: dict[str, asyncio.Event] = {}
        # Per-account reasoning logs, used by the loop *and* exposed to
        # external readers (Telegram bot, API) via ``reasoning_for``.
        self._reasoning_logs: dict[tuple[str, str], ReasoningLog] = {}


    # ---------------------------------------------------------------- public API
    def prepare_run(
        self,
        *,
        goals: list[AgentGoal],
        account_ids: list[str],
        target_url: str = "",
        instruction: str = "",
        parallel: bool = False,
        max_parallel: int | None = None,
    ) -> RunContext:
        """Synchronously create the RunContext, decompose goals, and persist
        the plan. Returns the RunContext immediately so callers (e.g. the API
        layer) can return the run_id without racing the background task.

        The actual execution must be started by passing the returned run to
        :py:meth:`execute_prepared` (typically via ``asyncio.create_task``).
        """
        run = RunContext.create(
            runs_root=self.runs_root,
            instruction=instruction,
            goals=[g.to_dict() for g in goals],
            accounts=account_ids,
        )
        run.metadata["target_url"] = target_url
        run.metadata["parallel"] = parallel
        if max_parallel is not None:
            run.metadata["max_parallel"] = max_parallel
        self._active_runs[run.run_id] = run

        plan = self.decomposer.build_plan(goals)
        if target_url:
            for g in plan:
                if g.type == GoalType.NAVIGATE and "url" not in g.params:
                    g.params["url"] = target_url

        run.save_plan({
            "goals": [g.to_dict() for g in plan],
            "accounts": account_ids,
            "target_url": target_url,
            "parallel": parallel,
            "max_parallel": max_parallel,
        })
        # stash plan on context so execute_prepared can read it back
        run.metadata["_plan_size"] = len(plan)
        return run

    async def execute_prepared(self, run: RunContext) -> RunContext:
        """Execute a previously prepared run. Loads the plan from disk."""
        import json as _json
        plan_path = run.base_dir / "plan.json"
        plan_data = _json.loads(plan_path.read_text())
        plan = [AgentGoal.from_dict(g) for g in plan_data.get("goals", [])]
        account_ids = plan_data.get("accounts", run.accounts)
        parallel = bool(plan_data.get("parallel", False))
        max_parallel = plan_data.get("max_parallel") or self.max_parallel

        await self._emit(AgentEvent(
            type=AgentEventType.RUN_STARTED,
            run_id=run.run_id,
            message=f"Starting run with {len(account_ids)} accounts, "
                    f"{len(plan)} goals",
            data={"accounts": len(account_ids), "goals": len(plan)},
        ))
        run.set_status(RunStatus.RUNNING)

        try:
            if parallel and len(account_ids) > 1:
                await self._run_parallel(run, plan, account_ids, int(max_parallel))
            else:
                await self._run_sequential(run, plan, account_ids)

            if run.run_id in self._cancelled:
                run.set_status(RunStatus.CANCELLED)
                await self._emit(AgentEvent(
                    type=AgentEventType.RUN_CANCELLED,
                    run_id=run.run_id,
                    message="Run cancelled",
                ))
            else:
                run.set_status(RunStatus.COMPLETED)
                await self._emit(AgentEvent(
                    type=AgentEventType.RUN_COMPLETED,
                    run_id=run.run_id,
                    message="Run completed",
                ))
        except Exception as exc:  # noqa: BLE001
            log.exception("agent run failed: %s", run.run_id)
            run.set_status(RunStatus.FAILED, error=repr(exc))
            await self._emit(AgentEvent(
                type=AgentEventType.RUN_FAILED,
                run_id=run.run_id,
                message=f"Run failed: {exc!r}",
            ))
        finally:
            self._active_runs.pop(run.run_id, None)
            self._cancelled.discard(run.run_id)

        return run

    async def execute(
        self,
        *,
        goals: list[AgentGoal],
        account_ids: list[str],
        target_url: str = "",
        instruction: str = "",
        parallel: bool = False,
        max_parallel: int | None = None,
        resume: bool = False,
    ) -> RunContext:
        """Convenience wrapper: prepare + execute in one call.

        Used by callers that don't need the run_id before execution starts.
        Equivalent to ``execute_prepared(prepare_run(...))``.
        """
        run = self.prepare_run(
            goals=goals,
            account_ids=account_ids,
            target_url=target_url,
            instruction=instruction,
            parallel=parallel,
            max_parallel=max_parallel,
        )
        return await self.execute_prepared(run)


    async def cancel(self, run_id: str) -> bool:
        """Cancel a running agent execution."""
        if run_id in self._active_runs:
            self._cancelled.add(run_id)
            # Unpause so the per-account loop can observe the cancel
            # and exit promptly rather than waiting on a stale gate.
            ev = self._pause_events.get(run_id)
            if ev is not None and not ev.is_set():
                ev.set()
            return True
        return False

    async def pause(self, run_id: str) -> bool:
        """Pause an active run between goals.

        Pausing does not interrupt a goal that is already executing —
        the loop checks the gate *between* goals so that mid-form-fill
        state never gets stranded. Returns ``True`` if the run is now
        paused, ``False`` if no such run is active.
        """
        if run_id not in self._active_runs:
            return False
        ev = self._pause_events.get(run_id)
        if ev is None:
            ev = asyncio.Event()
            ev.set()  # default: running
            self._pause_events[run_id] = ev
        if ev.is_set():
            ev.clear()
        await self._emit(AgentEvent(
            type=AgentEventType.WAITING,
            run_id=run_id,
            message="run paused (gate cleared)",
        ))
        return True

    async def resume_paused(self, run_id: str) -> bool:
        """Lift a pause on an active run.

        Distinct from :py:meth:`resume_run`, which restarts a previously
        terminated run from a checkpoint. ``resume_paused`` simply opens
        the gate that ``pause`` closed.
        """
        if run_id not in self._active_runs:
            return False
        ev = self._pause_events.get(run_id)
        if ev is None or ev.is_set():
            return True
        ev.set()
        await self._emit(AgentEvent(
            type=AgentEventType.RUN_RESUMED,
            run_id=run_id,
            message="run resumed (gate opened)",
        ))
        return True

    def is_paused(self, run_id: str) -> bool:
        ev = self._pause_events.get(run_id)
        return ev is not None and not ev.is_set()

    # ---------------------------------------------------------- read-only
    def reasoning_for(
        self, run_id: str, account_id: str, *, n: int = 10,
    ) -> list[dict[str, Any]]:
        """Return the most recent ``n`` reasoning entries for an account.

        Entries are returned newest-first. Reads from the on-disk log
        when there's no in-memory handle (e.g. for a completed run).
        """
        log_obj = self._reasoning_logs.get((run_id, account_id))
        if log_obj is None:
            path = (
                Path(self.runs_root)
                / run_id
                / _safe_segment(account_id)
                / "reasoning.jsonl"
            )
            log_obj = ReasoningLog(path)
        try:
            entries = log_obj.tail(n=n)
        except Exception:  # noqa: BLE001
            return []
        return [e.to_dict() for e in entries]

    def screenshots_for(
        self, run_id: str, account_id: str, *, limit: int = 50,
    ) -> list[str]:
        """Return paths of saved screenshots for an account, newest first."""
        d = (
            Path(self.runs_root)
            / run_id
            / _safe_segment(account_id)
            / "screenshots"
        )
        if not d.exists():
            return []
        files = sorted(
            (str(p) for p in d.iterdir() if p.is_file()),
            reverse=True,
        )
        return files[:limit]

    async def resume_run(self, run_dir: str) -> RunContext:
        """Resume a previously interrupted run from its last checkpoint."""
        import json as _json

        run = RunContext.load(run_dir)
        run.set_status(RunStatus.RESUMING)
        plan_path = run.base_dir / "plan.json"
        if not plan_path.exists():
            run.set_status(RunStatus.FAILED, error="no plan.json found")
            return run
        plan_data = _json.loads(plan_path.read_text())
        goals = [AgentGoal.from_dict(g) for g in plan_data.get("goals", [])]
        account_ids = plan_data.get("accounts", run.accounts)

        self._active_runs[run.run_id] = run
        await self._emit(AgentEvent(
            type=AgentEventType.RUN_RESUMED,
            run_id=run.run_id,
            message="Resuming run from checkpoint",
        ))

        run.set_status(RunStatus.RUNNING)
        try:
            await self._run_sequential(
                run, goals, account_ids, resume=True,
            )
            if run.run_id in self._cancelled:
                run.set_status(RunStatus.CANCELLED)
                await self._emit(AgentEvent(
                    type=AgentEventType.RUN_CANCELLED,
                    run_id=run.run_id,
                    message="Resumed run cancelled",
                ))
            else:
                run.set_status(RunStatus.COMPLETED)
                await self._emit(AgentEvent(
                    type=AgentEventType.RUN_COMPLETED,
                    run_id=run.run_id,
                    message="Resumed run completed",
                ))
        except Exception as exc:  # noqa: BLE001
            log.exception("agent resume failed: %s", run.run_id)
            run.set_status(RunStatus.FAILED, error=repr(exc))
            await self._emit(AgentEvent(
                type=AgentEventType.RUN_FAILED,
                run_id=run.run_id,
                message=f"Resume failed: {exc!r}",
            ))
        finally:
            self._active_runs.pop(run.run_id, None)
            self._cancelled.discard(run.run_id)
        return run

    @property
    def active_runs(self) -> dict[str, dict[str, Any]]:
        return {rid: r.summary() for rid, r in self._active_runs.items()}

    # ---------------------------------------------------------------- runners
    async def _run_sequential(
        self,
        run: RunContext,
        goals: list[AgentGoal],
        account_ids: list[str],
        resume: bool = False,
    ) -> None:
        """Run each account sequentially through the goal list."""
        for account_id in account_ids:
            if run.run_id in self._cancelled:
                break
            await self._run_account(run, goals, account_id, resume=resume)

    async def _run_parallel(
        self,
        run: RunContext,
        goals: list[AgentGoal],
        account_ids: list[str],
        max_parallel: int,
    ) -> None:
        """Run accounts in parallel with bounded concurrency."""
        sem = asyncio.Semaphore(max_parallel)

        async def _one(aid: str) -> None:
            async with sem:
                if run.run_id in self._cancelled:
                    return
                await self._run_account(run, goals, aid)

        await asyncio.gather(*(_one(aid) for aid in account_ids))


    # ---------------------------------------------------------------- per-account
    async def _run_account(
        self,
        run: RunContext,
        goals: list[AgentGoal],
        account_id: str,
        resume: bool = False,
    ) -> None:
        """Execute all goals for a single account with its own session."""
        await self._emit(AgentEvent(
            type=AgentEventType.ACCOUNT_STARTED,
            run_id=run.run_id,
            account_id=account_id,
            message=f"Starting account {account_id}",
        ))

        # Set up isolated session
        page = None
        session = None
        if self.browser:
            from automation.browser.manager import BrowserOverrides
            account = None
            if self.accounts_manager:
                account = self.accounts_manager.get(account_id)
            metadata = account.metadata if account else {}
            overrides = BrowserOverrides.from_metadata(metadata)
            session = await self.browser.get_or_create(account_id, overrides=overrides)
            page = session.page

        # Set up per-account recorder, checkpoint manager, and reasoning log.
        # The reasoning log is registered on the agent so external readers
        # (Telegram bot, API) can tail it without re-parsing the file on
        # every poll.
        account_dir = run.account_dir(account_id)
        recorder = ActionRecorder(account_dir / "replay.json")
        checkpoints = CheckpointManager(run.base_dir)
        reasoning_log = ReasoningLog(account_dir / "reasoning.jsonl")
        self._reasoning_logs[(run.run_id, account_id)] = reasoning_log

        # Build a fresh StepLoop per account so the reasoning_log is
        # bound to the right destination. Components that *can* be
        # shared across accounts (engine, popup_guard) are reused;
        # everything per-account lives on the loop instance.
        step_loop: StepLoop | None = None
        if self.deterministic_first and self.deterministic_engine is not None:
            from automation.ai.executor import ActionExecutor
            executor = ActionExecutor(
                screenshot_dir=account_dir / "screenshots",
                form_filler=self.form_filler,
                obstruction_detector=self.obstruction_detector,
            )
            step_loop = StepLoop(
                popup_guard=self.popup_guard,
                deterministic_engine=self.deterministic_engine,
                executor=executor,
                verifier=self.verifier,
                waiter=self.waiter,
                reasoning_log=reasoning_log,
                on_event=self._loop_event_emitter(run.run_id),
            )

        # Determine start index (for resume)
        start_index = 0
        if resume:
            start_index = checkpoints.resume_index(account_id)
            if start_index > 0:
                log.info(
                    "resuming account %s from goal index %d", account_id, start_index
                )

        # Execute goals sequentially
        success = True
        for i, goal in enumerate(goals):
            if run.run_id in self._cancelled:
                break
            if i < start_index:
                continue

            # Pause gate: between every goal we honour ``pause()``.
            await self._await_unpaused(run.run_id)
            if run.run_id in self._cancelled:
                break

            goal_success = await self._execute_goal(
                run=run,
                page=page,
                session=session,
                account_id=account_id,
                goal=goal,
                goal_index=i,
                recorder=recorder,
                checkpoints=checkpoints,
                step_loop=step_loop,
            )

            if not goal_success:
                success = False
                break

        # Flush recorder
        recorder.flush()

        # Adaptive Execution Mode: if this account succeeded end-to-end and
        # we have an adaptive executor, distill its successful action ledger
        # into one or more replayable ExecutionTemplates. This is what
        # account #2..N will replay deterministically (no LLM calls).
        if success and self.adaptive is not None:
            try:
                target_url = run.metadata.get("target_url", "") or ""
                inputs_for_template: dict[str, Any] = {}
                if self.accounts_manager:
                    account = self.accounts_manager.get(account_id)
                    if account:
                        inputs_for_template = {
                            "username": (
                                account.username or account.number or account.email
                            ),
                            "email":    account.email,
                            "password": account.password,
                            "number":   account.number,
                        }
                self.adaptive.record_from_run(
                    recorder.records,
                    inputs=inputs_for_template,
                    target_url=target_url,
                    run_id=run.run_id,
                    account_id=account_id,
                )
            except Exception:  # noqa: BLE001
                # Recording is best-effort — never fail an account because
                # we couldn't write a template.
                log.exception("template recording failed (non-fatal)")

        # Save final memory state
        run.save_memory(account_id, {
            "goals_total": len(goals),
            "goals_completed": start_index + sum(
                1 for g in goals[start_index:] if g.status == GoalStatus.COMPLETED
            ),
            "success": success,
            "completed_at": time.time(),
        })

        # Save cookies for resume capability
        if session:
            await run.save_cookies(session.context, account_id)

        event_type = (
            AgentEventType.ACCOUNT_COMPLETED if success
            else AgentEventType.ACCOUNT_FAILED
        )
        await self._emit(AgentEvent(
            type=event_type,
            run_id=run.run_id,
            account_id=account_id,
            message=f"Account {account_id} {'completed' if success else 'failed'}",
        ))

        # Persist cost tracker after each account completes
        self.cost_tracker.flush()


    # ---------------------------------------------------------------- per-goal
    async def _execute_goal(
        self,
        *,
        run: RunContext,
        page: Any,
        session: Any,
        account_id: str,
        goal: AgentGoal,
        goal_index: int,
        recorder: ActionRecorder,
        checkpoints: CheckpointManager,
        step_loop: StepLoop | None = None,
    ) -> bool:
        """Execute a single goal with waiting, recovery, and verification."""
        goal.status = GoalStatus.RUNNING
        goal.started_at = time.time()
        goal_desc = goal.description or goal.type.value

        await self._emit(AgentEvent(
            type=AgentEventType.GOAL_STARTED,
            run_id=run.run_id,
            account_id=account_id,
            goal=goal_desc,
            message=f"Goal: {goal_desc}",
            data={"index": goal_index, "type": goal.type.value},
        ))

        recorder.record_goal_start(goal_desc, goal_index)
        retries = 0
        success = False

        while retries <= goal.max_retries and not success:
            if run.run_id in self._cancelled:
                goal.status = GoalStatus.FAILED
                return False

            attempt_error: str | None = None
            try:
                success = await self._attempt_goal(
                    run=run, page=page, account_id=account_id,
                    goal=goal, goal_index=goal_index,
                    recorder=recorder, step_loop=step_loop,
                )
            except Exception as exc:  # noqa: BLE001
                attempt_error = repr(exc)
                log.warning(
                    "goal %s attempt %d raised: %s",
                    goal_desc, retries + 1, exc,
                )
                recorder.record_error(repr(exc), goal=goal_desc)

            # Trigger recovery on either an exception OR a falsy return
            # (verification failure). Skip on the final attempt.
            if not success and retries < goal.max_retries:
                await self._emit(AgentEvent(
                    type=AgentEventType.RECOVERY_STARTED,
                    run_id=run.run_id,
                    account_id=account_id,
                    goal=goal_desc,
                    retry_count=retries + 1,
                    message=attempt_error or "verification failed",
                ))
                recovery_result = await self.recovery.recover(
                    page,
                    goal=goal.params.get("ai_goal", goal_desc),
                    last_error=attempt_error or "",
                )
                if recovery_result.recovered:
                    await self._emit(AgentEvent(
                        type=AgentEventType.RECOVERY_SUCCEEDED,
                        run_id=run.run_id,
                        account_id=account_id,
                        goal=goal_desc,
                        message=f"Recovered via {recovery_result.final_strategy}",
                    ))
                else:
                    await self._emit(AgentEvent(
                        type=AgentEventType.RECOVERY_FAILED,
                        run_id=run.run_id,
                        account_id=account_id,
                        goal=goal_desc,
                    ))

            retries += 1

        # Record outcome
        duration_ms = int((time.time() - (goal.started_at or time.time())) * 1000)
        recorder.record_goal_end(goal_desc, success, duration_ms)

        if success:
            goal.status = GoalStatus.COMPLETED
            goal.completed_at = time.time()
            # Save checkpoint
            url = ""
            try:
                url = page.url if page else ""
            except Exception:  # noqa: BLE001
                pass
            screenshot_path = await run.save_screenshot(
                page, account_id, f"goal_{goal_index}_done"
            ) if page else None
            checkpoints.save(Checkpoint(
                account_id=account_id,
                goal_index=goal_index,
                goal_type=goal.type.value,
                goal_description=goal_desc,
                status="completed",
                url=url,
                screenshot_path=screenshot_path,
            ))
            # Update per-site self-learning memory so future runs against this
            # domain can pick known-good patterns. Best-effort — never blocks
            # the agent on a learning-store failure.
            self._remember_site_success(
                run, goal, account_id, url, duration_ms,
            )
            await self._emit(AgentEvent(
                type=AgentEventType.GOAL_COMPLETED,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                confidence=1.0,
            ))
        else:
            goal.status = GoalStatus.FAILED
            goal.error = "max retries exceeded"
            checkpoints.save(Checkpoint(
                account_id=account_id,
                goal_index=goal_index,
                goal_type=goal.type.value,
                goal_description=goal_desc,
                status="failed",
            ))
            self._remember_site_failure(
                run, goal, account_id, duration_ms,
            )
            await self._emit(AgentEvent(
                type=AgentEventType.GOAL_FAILED,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                retry_count=retries,
            ))

        return success


    # ---------------------------------------------------------------- attempt
    async def _attempt_goal(
        self,
        *,
        run: RunContext,
        page: Any,
        account_id: str,
        goal: AgentGoal,
        goal_index: int = 0,
        recorder: ActionRecorder,
        step_loop: StepLoop | None = None,
    ) -> bool:
        """Single attempt at executing a goal."""
        if page is None:
            log.warning("no page available for goal %s", goal.description)
            return False

        goal_desc = goal.description or goal.type.value

        # Step 1: Navigate if this is a navigate goal
        if goal.type == GoalType.NAVIGATE:
            url = goal.params.get("url", "")
            if url:
                await self._emit(AgentEvent(
                    type=AgentEventType.STEP_STARTED,
                    run_id=run.run_id,
                    account_id=account_id,
                    goal=goal_desc,
                    step="navigate",
                    message=f"Navigating to {url}",
                ))
                started = time.time()
                await page.goto(url, timeout=int(goal.timeout_seconds * 1000))
                recorder.record_navigate(url, int((time.time() - started) * 1000))

            # Wait for page ready
            await self._emit(AgentEvent(
                type=AgentEventType.WAITING,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                message="Waiting for page to load...",
            ))
            wait_result = await self.waiter.wait_for_page_ready(
                page, timeout_ms=int(goal.timeout_seconds * 1000)
            )
            recorder.record_wait("page_ready", wait_result.resolved, wait_result.elapsed_ms)

            await self._emit(AgentEvent(
                type=AgentEventType.WAIT_RESOLVED,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                message=wait_result.reason,
            ))

            # Verify: page loaded successfully
            verification = await self.verifier.verify(
                page, goal=goal_desc, hints=goal.verification_hints,
            )
            return verification.passed

        # ---------- non-navigate goal -----------------------------------
        # Take a pre-action screenshot. The path is also returned so the
        # reasoning panel / replay tools can find it without scanning.
        ai_goal = goal.params.get("ai_goal", goal.type.value)
        await run.save_screenshot(page, account_id, f"before_{ai_goal}")

        # Resolve per-account inputs once, both paths use the same dict.
        inputs = self._resolve_inputs(account_id)

        if step_loop is not None:
            # ---- v2 deterministic-first path ---------------------------
            return await self._attempt_via_loop(
                run=run, page=page, account_id=account_id,
                goal=goal, goal_index=goal_index,
                ai_goal=ai_goal, inputs=inputs,
                recorder=recorder, step_loop=step_loop,
            )

        # ---- legacy brain-driven path ----------------------------------
        return await self._attempt_via_brain(
            run=run, page=page, account_id=account_id,
            goal=goal, ai_goal=ai_goal, inputs=inputs,
            recorder=recorder,
        )

    # ---------------------------------------------------------------- v2 attempt
    async def _attempt_via_loop(
        self,
        *,
        run: RunContext,
        page: Any,
        account_id: str,
        goal: AgentGoal,
        goal_index: int,
        ai_goal: str,
        inputs: dict[str, str],
        recorder: ActionRecorder,
        step_loop: StepLoop,
    ) -> bool:
        """Execute one non-navigate goal via the deterministic StepLoop.

        Translates the loop's :class:`StepLoopResult` into the agent's
        bool / event protocol so the rest of ``_execute_goal`` is
        agnostic to which path produced the result.
        """
        goal_desc = goal.description or goal.type.value
        try:
            before_url = page.url
        except Exception:  # noqa: BLE001
            before_url = ""
        host = _host_from_url(before_url)

        result: StepLoopResult = await step_loop.execute_goal(
            page=page,
            run_id=run.run_id,
            account_id=account_id,
            goal_name=ai_goal,
            goal_description=goal_desc,
            goal_index=goal_index,
            inputs=inputs,
            verification_hints=goal.verification_hints,
            before_url=before_url,
            host=host,
            cancel_check=lambda: run.run_id in self._cancelled,
        )

        # Mirror the loop's plan + action summary into the legacy
        # recorder so existing replay.json consumers still see what
        # happened.
        recorder.record_ai_decision(
            ai_goal,
            result.plan.to_dict(),
            confidence=result.plan.confidence,
        )

        # Track AI cost based on which level resolved the goal
        from automation.agent.reasoning import DecisionSource
        source = result.plan.source
        if source is DecisionSource.AI:
            self.cost_tracker.record_ai_call(goal=ai_goal)
        elif source is DecisionSource.REPLAY:
            self.cost_tracker.record_template_replay(goal=ai_goal)
        elif source is DecisionSource.HEURISTIC:
            self.cost_tracker.record_heuristic_hit(goal=ai_goal)
        elif source is DecisionSource.SITE_MEMORY:
            self.cost_tracker.record_site_memory_hit(goal=ai_goal)
        elif source is DecisionSource.RULE:
            self.cost_tracker.record_rule_hit(goal=ai_goal)
        elif source is DecisionSource.HUMAN:
            self.cost_tracker.record_human_handoff(goal=ai_goal)
        if result.popup_result and result.popup_result.any_dismissed:
            recorder.record_wait(
                "popup_dismiss", True, result.popup_result.duration_ms,
            )

        # Emit a verification-style event so the dashboard / Telegram
        # bot keeps the same lifecycle it had on the legacy path.
        await self._emit(AgentEvent(
            type=(
                AgentEventType.VERIFICATION_PASSED
                if result.success
                else AgentEventType.VERIFICATION_FAILED
            ),
            run_id=run.run_id,
            account_id=account_id,
            goal=goal_desc,
            confidence=(
                result.verification.confidence if result.verification else 0.0
            ),
            message=(
                result.verification.reason if result.verification
                else (result.error or result.plan.rationale)
            ),
            data={"source": result.plan.source.value},
        ))

        await run.save_screenshot(page, account_id, f"after_{ai_goal}")
        return result.success

    # ---------------------------------------------------------------- legacy attempt
    async def _attempt_via_brain(
        self,
        *,
        run: RunContext,
        page: Any,
        account_id: str,
        goal: AgentGoal,
        ai_goal: str,
        inputs: dict[str, str],
        recorder: ActionRecorder,
    ) -> bool:
        """Original brain-only path. Preserved for ``deterministic_first=False``
        and as the safety net when ``deterministic_engine`` is unavailable.
        """
        goal_desc = goal.description or goal.type.value
        try:
            before_url = page.url
        except Exception:  # noqa: BLE001
            before_url = ""

        # ---------- Adaptive Execution Mode (token optimisation) -----------
        # Try replay first; brain only runs if no template exists or replay
        # fails. The first successful account on a domain teaches the
        # framework via TemplateRecorder; accounts #2..N replay deterministically
        # (no LLM calls). See agent/adaptive_executor.py for the dispatch logic.
        used_replay = False
        if self.adaptive is not None:
            from automation.agent.adaptive_executor import ExecutionMode
            adaptive_result = await self.adaptive.execute(
                page, goal, inputs,
                target_url=run.metadata.get("target_url", ""),
                recorder=recorder,
            )
            await self._emit(AgentEvent(
                type=AgentEventType.AI_DECISION,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                message=adaptive_result.reason or adaptive_result.mode.value,
                data=adaptive_result.to_dict(),
            ))
            if adaptive_result.mode is ExecutionMode.REPLAY_OK:
                used_replay = True
            elif adaptive_result.mode is ExecutionMode.REPLAY_FAILED:
                # Decay event — caller's brain path will now retry. We do
                # not raise; the brain gets a fresh shot at the goal.
                await self._emit(AgentEvent(
                    type=AgentEventType.RECOVERY_STARTED,
                    run_id=run.run_id,
                    account_id=account_id,
                    goal=goal_desc,
                    message=f"Replay failed: {adaptive_result.reason}",
                ))
            # NO_OP / AI_USED both fall through to the brain path.

        # Use AI brain to perceive + plan + act (skipped on REPLAY_OK)
        if self.brain and not used_replay:
            await self._emit(AgentEvent(
                type=AgentEventType.AI_DECISION,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                message=f"AI deciding actions for: {ai_goal}",
            ))

            decision = await self.brain.run(page, goal=ai_goal, inputs=inputs)

            recorder.record_ai_decision(
                ai_goal,
                decision.plan.to_dict() if decision.plan else {},
                confidence=decision.plan.steps[0].confidence if decision.plan and decision.plan.steps else 0,
            )

            if not decision.success:
                raise RuntimeError(
                    f"AI brain failed for goal {ai_goal}: "
                    f"{decision.plan_result.failed_steps if decision.plan_result else 'no result'}"
                )

        # Wait for effects to settle
        await self._emit(AgentEvent(
            type=AgentEventType.WAITING,
            run_id=run.run_id,
            account_id=account_id,
            goal=goal_desc,
            message="Waiting for action effects...",
        ))

        # Check if we need to wait for specific things
        wait_for = goal.params.get("wait_for")
        if wait_for == "download_complete":
            wait_result = await self.waiter.wait_for_download(page)
        else:
            wait_result = await self.waiter.wait_for_success_signal(
                page,
                hints=goal.verification_hints,
                timeout_ms=int(goal.timeout_seconds * 1000),
            )

        recorder.record_wait(
            wait_for or "success_signal", wait_result.resolved, wait_result.elapsed_ms
        )

        # Take post-action screenshot
        await run.save_screenshot(page, account_id, f"after_{ai_goal}")

        # Verify success
        await self._emit(AgentEvent(
            type=AgentEventType.VERIFICATION_STARTED,
            run_id=run.run_id,
            account_id=account_id,
            goal=goal_desc,
        ))

        verification = await self.verifier.verify(
            page,
            goal=goal_desc,
            hints=goal.verification_hints,
            before_url=before_url,
        )

        if verification.passed:
            await self._emit(AgentEvent(
                type=AgentEventType.VERIFICATION_PASSED,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                confidence=verification.confidence,
                message=verification.reason,
            ))
        else:
            await self._emit(AgentEvent(
                type=AgentEventType.VERIFICATION_FAILED,
                run_id=run.run_id,
                account_id=account_id,
                goal=goal_desc,
                confidence=verification.confidence,
                message=verification.reason,
            ))

        return verification.passed

    # ---------------------------------------------------------------- learning
    def _remember_site_success(
        self,
        run: RunContext,
        goal: AgentGoal,
        account_id: str,
        url: str,
        duration_ms: int,
    ) -> None:
        """Record a successful goal in the per-site memory store. Optional."""
        if not self.site_memory:
            return
        target_url = (
            url
            or goal.params.get("url")
            or run.metadata.get("target_url", "")
        )
        if not target_url:
            return
        try:
            self.site_memory.remember_success(
                target_url,
                workflow=goal.type.value,
                duration_seconds=duration_ms / 1000,
                # The brain's last_decision exposes resolved selectors which
                # we can persist as known-good for this site. Best-effort.
                login_selector=_extract_selector(self.brain, intent="login"),
                submit_selector=_extract_selector(self.brain, intent="submit"),
                landing_url=url or None,
            )
        except Exception:  # noqa: BLE001
            log.exception("site memory: remember_success failed")

    def _remember_site_failure(
        self,
        run: RunContext,
        goal: AgentGoal,
        account_id: str,
        duration_ms: int,
    ) -> None:
        if not self.site_memory:
            return
        target_url = (
            goal.params.get("url")
            or run.metadata.get("target_url", "")
        )
        if not target_url:
            return
        try:
            self.site_memory.remember_failure(
                target_url,
                workflow=goal.type.value,
                duration_seconds=duration_ms / 1000,
                note=(goal.error or "")[:200],
            )
        except Exception:  # noqa: BLE001
            log.exception("site memory: remember_failure failed")

    # ---------------------------------------------------------------- events
    async def _emit(self, event: AgentEvent) -> None:
        """Emit an event to the EventBus and append to run log."""
        if event.run_id in self._active_runs:
            self._active_runs[event.run_id].append_event(event.to_dict())

        if self.event_bus:
            from automation.core.event_bus import Event as BusEvent
            try:
                await self.event_bus.publish(BusEvent(
                    name=event.type.value,
                    payload=event.to_dict(),
                    source="agent",
                ))
            except Exception:  # noqa: BLE001
                log.debug("event publish failed for %s", event.type.value)



def _extract_selector(brain: Any, *, intent: str) -> str | None:
    """Pull a known-good selector out of the brain's last decision, if any.

    The :class:`AIBrain` records a ``BrainDecision`` per ``run()`` call, and
    each ``ActionPlan.steps`` entry has the ``selector`` it actually used.
    We look for a step whose intent / step name contains the requested
    intent (e.g. ``login``, ``submit``) and return its selector. Returns
    ``None`` if the brain hasn't run, or if no matching step is found.
    """
    if brain is None:
        return None
    decision = getattr(brain, "last_decision", None)
    if decision is None or decision.plan is None:
        return None
    for step in getattr(decision.plan, "steps", []) or []:
        step_intent = (getattr(step, "intent", "") or "").lower()
        step_name = (getattr(step, "name", "") or "").lower()
        if intent in step_intent or intent in step_name:
            sel = getattr(step, "selector", "")
            if sel:
                return str(sel)
    return None
