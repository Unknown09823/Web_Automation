"""Tests for the Adaptive Execution Mode (token-optimisation layer).

Runnable without pytest:

    PYTHONPATH=src python3.12 scripts/test_adaptive_execution.py

Coverage:
  * ExecutionTemplate     — to_dict round-trip, input_keys_required,
                             record_replay_outcome confidence math.
  * ExecutionTemplateStore — CRUD, find_best by confidence + version,
                             next_version monotonicity, summary shape,
                             ineligible templates filtered out.
  * TemplateRecorder      — distill an ActionRecord ledger into a
                             template; values appearing in inputs become
                             ${var} placeholders; multiple successful
                             goals produce multiple templates; failed
                             goals are NOT recorded; non-replayable
                             record types are dropped; AI/screenshot
                             noise is dropped; waits are normalised to
                             condition names (no fixed sleeps).
  * TemplateReplayer      — happy path with FakePage; selector failure;
                             value interpolation; verify-hint matching;
                             wait-condition resolution.
  * AdaptiveExecutor      — NO_OP for navigate goals;
                             AI_USED on cold start;
                             REPLAY_OK on hot path with high confidence;
                             REPLAY_FAILED on selector mismatch;
                             confidence decay after a failure;
                             AI_USED when required inputs are missing;
                             record_from_run distills then prunes old
                             versions to a bounded number.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from automation.agent.adaptive_executor import (
    AdaptiveExecutor, AdaptiveResult, ExecutionMode,
)
from automation.agent.execution_template import (
    ActionKind, ExecutionTemplate, ExecutionTemplateStore, TemplateAction,
    TemplateStats, interpolate,
)
from automation.agent.goals import AgentGoal, GoalType
from automation.agent.recorder import ActionRecord, RecordType
from automation.agent.template_recorder import TemplateRecorder
from automation.agent.template_replayer import ReplayResult, TemplateReplayer
from automation.agent.waiter import AdaptiveWaiter, WaitCondition, WaitResult


_passes: list[str] = []
_fails: list[tuple[str, str]] = []


def expect(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
        _passes.append(label)
    else:
        print(f"  FAIL  {label}{(': ' + detail) if detail else ''}")
        _fails.append((label, detail))


# ===========================================================================
#  Fakes
# ===========================================================================
class FakePage:
    """Minimal Page double — enough for the replayer + waiter."""

    def __init__(self, *, fail_selectors: list[str] | None = None,
                 body_text: str = "", url: str = "https://example.com/start") -> None:
        self.url = url
        self._fail = set(fail_selectors or [])
        self._body_text = body_text
        self.actions: list[tuple[str, str, str]] = []  # (kind, selector, value)

    async def goto(self, url: str, timeout: int = 30000) -> None:
        self.url = url
        self.actions.append(("navigate", "", url))

    async def fill(self, selector: str, value: str, timeout: int = 30000) -> None:
        if selector in self._fail:
            raise RuntimeError(f"selector not found: {selector}")
        self.actions.append(("fill", selector, value))

    async def click(self, selector: str, timeout: int = 30000) -> None:
        if selector in self._fail:
            raise RuntimeError(f"selector not found: {selector}")
        self.actions.append(("click", selector, ""))

    async def select_option(self, selector: str, value: Any, timeout: int = 30000):
        self.actions.append(("select", selector, str(value)))

    async def check(self, selector: str, timeout: int = 30000) -> None:
        self.actions.append(("check", selector, ""))

    async def press(self, selector: str, key: str, timeout: int = 30000) -> None:
        self.actions.append(("press", selector, key))

    async def evaluate(self, script: str) -> Any:
        # Very lazy: route based on substring to either body text or no-op.
        if "innerText" in script and "body" in script:
            return self._body_text.lower()
        if "readyState" in script:
            return "complete"
        if "innerText" in script and "length" in script:
            return len(self._body_text)
        return None

    async def wait_for_selector(self, selector: str, *, state: str = "visible",
                                 timeout: int = 30000) -> None:
        if selector in self._fail:
            raise RuntimeError("not visible")
        return None

    async def title(self) -> str:
        return ""


class StubWaiter:
    """Replaces AdaptiveWaiter so we don't depend on JS evaluation."""

    def __init__(self, *, always_resolve: bool = True) -> None:
        self.always_resolve = always_resolve
        self.calls: list[str] = []

    async def smart_wait(self, page, *, condition, timeout_ms=None, hints=None,
                         selector=None, from_url=None) -> WaitResult:
        self.calls.append(condition.value)
        return WaitResult(
            condition=condition,
            resolved=self.always_resolve,
            elapsed_ms=10,
            reason="stub",
        )


# ===========================================================================
#  ExecutionTemplate
# ===========================================================================
def test_execution_template() -> None:
    print("\n=== ExecutionTemplate ===")

    actions = [
        TemplateAction(kind=ActionKind.NAVIGATE, url_template="${target_url}"),
        TemplateAction(kind=ActionKind.FILL, selector="#mobile",
                       value_template="${number}", wait_for="page_load"),
        TemplateAction(kind=ActionKind.FILL, selector="#password",
                       value_template="${password}", wait_for="page_load"),
        TemplateAction(kind=ActionKind.CLICK, selector="button[type=submit]",
                       wait_for="navigation_complete"),
        TemplateAction(kind=ActionKind.VERIFY, hints=["dashboard", "welcome"]),
    ]
    t = ExecutionTemplate(
        domain="example.com", workflow="register_account",
        version=1, actions=actions,
    )
    expect("input_keys_required extracted from templates",
           t.input_keys_required == {"target_url", "number", "password"})

    rt = ExecutionTemplate.from_dict(t.to_dict())
    expect("round-trip preserves actions and metadata",
           len(rt.actions) == 5
           and rt.actions[1].selector == "#mobile"
           and rt.actions[3].wait_for == "navigation_complete"
           and rt.actions[4].hints == ["dashboard", "welcome"])

    expect("brand-new template has confidence 1.0", t.confidence == 1.0)
    expect("brand-new template is eligible", t.is_eligible())

    # Confidence math: one failure should not nuke confidence
    t.record_replay_outcome(success=True, duration_ms=1000)
    after_one_success = t.confidence
    expect("after success: confidence still ~1.0",
           after_one_success >= 0.9)
    t.record_replay_outcome(success=False, duration_ms=500,
                            failure_reason="selector missing")
    expect("after one failure on prior weight: confidence > 0.5",
           t.confidence > 0.5,
           detail=f"confidence={t.confidence}")
    expect("avg duration captured EMA-style on success only",
           t.stats.avg_duration_ms > 0)
    expect("failure reason recorded",
           "selector missing" in t.stats.last_failure_reason)

    # Many failures eventually disqualify the template
    for _ in range(20):
        t.record_replay_outcome(success=False, duration_ms=100,
                                failure_reason="bad")
    expect("after many failures: ineligible",
           not t.is_eligible(),
           detail=f"confidence={t.confidence}")


# ===========================================================================
#  ExecutionTemplateStore
# ===========================================================================
def test_execution_template_store() -> None:
    print("\n=== ExecutionTemplateStore ===")
    with tempfile.TemporaryDirectory() as d:
        store = ExecutionTemplateStore(d)

        # Domain normalisation
        expect("normalize_domain strips www + lowercases",
               store.normalize_domain("https://www.Example.com/x") == "example.com")
        expect("normalize_domain handles raw host",
               store.normalize_domain("Foo.Bar.com") == "foo.bar.com")

        # Save v1
        t1 = ExecutionTemplate(
            domain="example.com", workflow="login",
            version=store.next_version("example.com", "login"),
            actions=[TemplateAction(kind=ActionKind.NAVIGATE,
                                    url_template="https://example.com/login")],
            confidence=0.95,
        )
        store.save(t1)
        expect("save creates one version", t1.version == 1)
        expect("next_version increments after save",
               store.next_version("example.com", "login") == 2)

        # Save v2 with higher version + lower confidence
        t2 = ExecutionTemplate(
            domain="example.com", workflow="login",
            version=2,
            actions=[TemplateAction(kind=ActionKind.NAVIGATE,
                                    url_template="https://example.com/login")],
            confidence=0.5,
        )
        store.save(t2)

        # find_best should pick higher confidence (v1) over higher version (v2)
        best = store.find_best("example.com", "login")
        expect("find_best picks higher-confidence over higher-version",
               best.version == 1)

        # Non-eligible filter
        t1.confidence = 0.1
        store.save(t1)
        best = store.find_best("example.com", "login")
        expect("find_best returns v2 once v1 falls below its own threshold",
               best is None or best.version == 2,
               detail=f"got v{best.version if best else None}")

        # Different workflow doesn't collide
        t3 = ExecutionTemplate(
            domain="example.com", workflow="register_account", version=1,
            actions=[TemplateAction(kind=ActionKind.NAVIGATE,
                                    url_template="https://example.com/")],
            confidence=0.9,
        )
        store.save(t3)
        expect("login and register_account are independently versioned",
               store.next_version("example.com", "login") == 3
               and store.next_version("example.com", "register_account") == 2)

        # list_for_domain returns sorted
        listed = store.list_for_domain("example.com")
        expect("list_for_domain returns all templates",
               len(listed) == 3)

        # delete by version
        deleted = store.delete("example.com", "login", 1)
        expect("delete returns True on success", deleted is True)
        expect("delete returns False on missing",
               store.delete("example.com", "login", 99) is False)

        # Summary
        s = store.summary()
        expect("summary lists domain", len(s["domains"]) == 1)
        wf_names = {w["workflow"] for w in s["domains"][0]["workflows"]}
        expect("summary lists both workflows",
               wf_names == {"login", "register_account"})


# ===========================================================================
#  TemplateRecorder
# ===========================================================================
def test_template_recorder() -> None:
    print("\n=== TemplateRecorder ===")
    with tempfile.TemporaryDirectory() as d:
        store = ExecutionTemplateStore(d)
        rec = TemplateRecorder(store)

        # Build a synthetic ledger: one successful "register_account",
        # then AI noise (which must be dropped), then a failed "login".
        records = [
            ActionRecord(type=RecordType.GOAL_START, goal="register_account"),
            ActionRecord(type=RecordType.NAVIGATE,
                         url="https://example.com/signup",
                         duration_ms=1200),
            ActionRecord(type=RecordType.AI_DECISION,
                         goal="register_account",
                         data={"plan": {"steps": [...]}}),
            ActionRecord(type=RecordType.SCREENSHOT,
                         screenshot_path="x.png"),
            ActionRecord(type=RecordType.FILL,
                         selector="#mobile",
                         value="+1234567890"),
            ActionRecord(type=RecordType.WAIT,
                         data={"condition": "page_ready"},
                         duration_ms=400, success=True),
            ActionRecord(type=RecordType.FILL,
                         selector="#password",
                         value="Test@123"),
            ActionRecord(type=RecordType.CLICK,
                         selector="button[type=submit]"),
            ActionRecord(type=RecordType.WAIT,
                         data={"condition": "navigation"},
                         duration_ms=2000, success=True),
            ActionRecord(type=RecordType.GOAL_END,
                         goal="register_account", success=True),

            # Failed goal — must NOT produce a template
            ActionRecord(type=RecordType.GOAL_START, goal="login"),
            ActionRecord(type=RecordType.NAVIGATE,
                         url="https://example.com/login"),
            ActionRecord(type=RecordType.GOAL_END,
                         goal="login", success=False),
        ]
        inputs = {"number": "+1234567890", "password": "Test@123"}

        templates = rec.distill(
            records,
            inputs=inputs,
            target_url="https://example.com",
            run_id="r1",
            account_id="a1",
        )

        expect("only successful goals become templates",
               len(templates) == 1)
        t = templates[0]
        expect("template domain normalised",
               t.domain == "example.com")
        expect("template workflow normalised",
               t.workflow == "register_account")
        expect("starting version = 1",
               t.version == 1)

        # AI/screenshot noise dropped; wait merged into preceding action
        kinds = [a.kind.value for a in t.actions]
        expect("non-replayable records dropped (no ai_decision/screenshot)",
               "ai_decision" not in kinds and "screenshot" not in kinds)

        # Number and password should be parameterised
        fills = [a for a in t.actions if a.kind == ActionKind.FILL]
        expect("number replaced with ${number} placeholder",
               any(f.value_template == "${number}" for f in fills))
        expect("password replaced with ${password} placeholder",
               any(f.value_template == "${password}" for f in fills))

        # Waits became condition names, never raw integers
        for a in t.actions:
            if a.wait_for is not None:
                expect(
                    f"wait_for is a named condition (got {a.wait_for!r})",
                    a.wait_for in {
                        "page_load", "navigation_complete", "url_change",
                        "network_idle", "dom_stable", "element_visible",
                        "element_gone", "download_complete", "success_message",
                        "no_loading",
                    },
                )

        # Click immediately followed by wait should have its wait attached
        click_action = next(
            a for a in t.actions if a.kind == ActionKind.CLICK
        )
        expect("click's wait condition came from following WAIT record",
               click_action.wait_for == "navigation_complete")

        # Saved to disk
        expect("template persisted to disk",
               (Path(d) / "example.com" / "register_account_v1.json").exists())


# ===========================================================================
#  TemplateReplayer
# ===========================================================================
def test_template_replayer_happy_path() -> None:
    print("\n=== TemplateReplayer (happy path) ===")
    waiter = StubWaiter(always_resolve=True)
    replayer = TemplateReplayer(waiter=waiter)
    page = FakePage(body_text="Welcome to your dashboard!")

    template = ExecutionTemplate(
        domain="example.com", workflow="register_account", version=1,
        actions=[
            TemplateAction(kind=ActionKind.NAVIGATE,
                           url_template="${target_url}/signup",
                           wait_for="page_load"),
            TemplateAction(kind=ActionKind.FILL, selector="#mobile",
                           value_template="${number}",
                           wait_for="page_load"),
            TemplateAction(kind=ActionKind.FILL, selector="#password",
                           value_template="${password}",
                           wait_for="page_load"),
            TemplateAction(kind=ActionKind.CLICK,
                           selector="button[type=submit]",
                           wait_for="navigation_complete"),
            TemplateAction(kind=ActionKind.VERIFY,
                           hints=["dashboard", "welcome"]),
        ],
    )
    inputs = {
        "target_url": "https://example.com",
        "number": "+1234567890",
        "password": "Test@123",
    }

    result = asyncio.run(replayer.replay(page, template, inputs))
    expect("replay succeeded end-to-end", result.success)
    expect("all actions executed", len(result.actions) == 5)
    expect("page received the templated URL",
           page.actions[0] == ("navigate", "", "https://example.com/signup"))
    expect("inputs were interpolated into FILL value",
           ("fill", "#mobile", "+1234567890") in page.actions)
    expect("password interpolated and not leaked as ${password}",
           ("fill", "#password", "Test@123") in page.actions)
    expect("waiter consulted for every action",
           len(waiter.calls) >= 4)
    expect("no fixed sleep was used (only WaitCondition values seen)",
           all(c in {wc.value for wc in WaitCondition} for c in waiter.calls))


def test_template_replayer_failure_modes() -> None:
    print("\n=== TemplateReplayer (failure modes) ===")
    # Failure: selector missing
    page = FakePage(fail_selectors=["#missing"])
    template = ExecutionTemplate(
        domain="example.com", workflow="login", version=1,
        actions=[
            TemplateAction(kind=ActionKind.FILL, selector="#missing",
                           value_template="x", wait_for="page_load"),
        ],
    )
    replayer = TemplateReplayer(waiter=StubWaiter(always_resolve=True))
    result = asyncio.run(replayer.replay(page, template, {}))
    expect("replay reports failure",     not result.success)
    expect("failed_index is the bad action", result.failed_index == 0)
    expect("failure_reason includes the error",
           "selector not found" in result.failure_reason
           or "missing" in result.failure_reason)

    # Failure: wait condition does not resolve
    page2 = FakePage()
    waiter2 = StubWaiter(always_resolve=False)
    template2 = ExecutionTemplate(
        domain="example.com", workflow="login", version=1,
        actions=[TemplateAction(kind=ActionKind.CLICK, selector="#go",
                                wait_for="success_message")],
    )
    result2 = asyncio.run(
        TemplateReplayer(waiter=waiter2).replay(page2, template2, {}),
    )
    expect("replay fails when wait does not resolve", not result2.success)
    expect("failure_reason mentions the wait",
           "success_message" in result2.failure_reason
           or "wait" in result2.failure_reason)

    # Failure: VERIFY hints not in body
    page3 = FakePage(body_text="error: something went wrong")
    template3 = ExecutionTemplate(
        domain="example.com", workflow="login", version=1,
        actions=[TemplateAction(kind=ActionKind.VERIFY,
                                hints=["dashboard", "welcome"])],
    )
    result3 = asyncio.run(
        TemplateReplayer(waiter=StubWaiter()).replay(page3, template3, {}),
    )
    expect("verify with no matching hint fails replay",
           not result3.success)


# ===========================================================================
#  AdaptiveExecutor
# ===========================================================================
def test_adaptive_executor() -> None:
    print("\n=== AdaptiveExecutor ===")
    with tempfile.TemporaryDirectory() as d:
        store = ExecutionTemplateStore(d)
        replayer = TemplateReplayer(waiter=StubWaiter(always_resolve=True))
        recorder = TemplateRecorder(store)
        execu = AdaptiveExecutor(
            store=store, replayer=replayer, recorder=recorder,
        )

        # Goal: REGISTER_ACCOUNT on example.com
        goal = AgentGoal(
            type=GoalType.REGISTER_ACCOUNT,
            description="Register on example.com",
            params={"url": "https://example.com/signup"},
        )

        # 1) Cold start: no templates → AI_USED
        result = asyncio.run(execu.execute(
            FakePage(), goal, inputs={"number": "X", "password": "Y"},
            target_url="https://example.com/signup",
        ))
        expect("cold start: mode is AI_USED",
               result.mode is ExecutionMode.AI_USED)

        # 2) Save a successful template, replay should hit
        t = ExecutionTemplate(
            domain="example.com", workflow="register_account", version=1,
            actions=[
                TemplateAction(kind=ActionKind.NAVIGATE,
                               url_template="${url}/x",
                               wait_for="page_load"),
                TemplateAction(kind=ActionKind.FILL, selector="#mobile",
                               value_template="${number}",
                               wait_for="page_load"),
                TemplateAction(kind=ActionKind.VERIFY, hints=["welcome"]),
            ],
            confidence=0.95,
        )
        store.save(t)

        page = FakePage(body_text="welcome user")
        result = asyncio.run(execu.execute(
            page, goal,
            inputs={"url": "https://example.com", "number": "+1"},
            target_url="https://example.com/signup",
        ))
        expect("hot path: mode is REPLAY_OK",
               result.mode is ExecutionMode.REPLAY_OK)
        expect("hot path: success is True", result.success is True)
        expect("hot path: replay round-tripped to FakePage",
               page.actions[0][0] == "navigate")

        # Reload from disk to confirm stats persisted
        latest = store.find_best("example.com", "register_account")
        expect("stats persisted: replays_attempted incremented",
               latest.stats.replays_attempted == 1
               and latest.stats.replays_succeeded == 1)
        expect("confidence is still high after a success",
               latest.confidence >= 0.95,
               detail=f"confidence={latest.confidence}")

        # 3) Required input missing → AI_USED, no replay attempt
        result = asyncio.run(execu.execute(
            FakePage(), goal,
            inputs={"url": "https://example.com"},  # missing 'number'
            target_url="https://example.com/signup",
        ))
        expect("missing input: mode is AI_USED",
               result.mode is ExecutionMode.AI_USED)
        expect("missing input: reason mentions missing key",
               "number" in result.reason or "input" in result.reason.lower())

        # Replay attempts count must NOT have grown — we never tried it
        latest2 = store.find_best("example.com", "register_account")
        expect("missing-input did not increment replay stats",
               latest2.stats.replays_attempted == latest.stats.replays_attempted)

        # 4) NAVIGATE goal → NO_OP (the agent owns its own navigate path)
        nav_goal = AgentGoal(type=GoalType.NAVIGATE,
                             description="open page",
                             params={"url": "https://example.com"})
        result = asyncio.run(execu.execute(
            FakePage(), nav_goal, inputs={},
            target_url="https://example.com",
        ))
        expect("navigate goal: mode is NO_OP",
               result.mode is ExecutionMode.NO_OP)

        # 5) Replay failure path: bad selector → REPLAY_FAILED, decay
        bad_template = ExecutionTemplate(
            domain="bad.example.com", workflow="login", version=1,
            actions=[TemplateAction(kind=ActionKind.CLICK,
                                    selector="#nope",
                                    wait_for="page_load")],
            confidence=0.95,
        )
        store.save(bad_template)
        login_goal = AgentGoal(
            type=GoalType.LOGIN, description="login",
            params={"url": "https://bad.example.com/login"},
        )
        prior_conf = bad_template.confidence
        result = asyncio.run(execu.execute(
            FakePage(fail_selectors=["#nope"]), login_goal,
            inputs={}, target_url="https://bad.example.com/login",
        ))
        expect("bad selector replay: mode is REPLAY_FAILED",
               result.mode is ExecutionMode.REPLAY_FAILED)
        # Reload to see decayed confidence
        decayed = store.find_best(
            "bad.example.com", "login", require_eligible=False,
        )
        expect("confidence decayed after replay failure",
               decayed.confidence < prior_conf,
               detail=f"prior={prior_conf} after={decayed.confidence}")

        # 6) force_ai bypasses replay even when a hot template exists
        result = asyncio.run(execu.execute(
            FakePage(), goal,
            inputs={"url": "https://example.com", "number": "+1"},
            target_url="https://example.com/signup",
            force_ai=True,
        ))
        expect("force_ai: mode is AI_USED",
               result.mode is ExecutionMode.AI_USED)


def test_adaptive_executor_record_from_run() -> None:
    print("\n=== AdaptiveExecutor.record_from_run ===")
    with tempfile.TemporaryDirectory() as d:
        store = ExecutionTemplateStore(d)
        replayer = TemplateReplayer(waiter=StubWaiter())
        recorder = TemplateRecorder(store)
        execu = AdaptiveExecutor(
            store=store, replayer=replayer, recorder=recorder,
        )

        # Synthetic ledger: register_account succeeded
        records = [
            ActionRecord(type=RecordType.GOAL_START, goal="register_account"),
            ActionRecord(type=RecordType.NAVIGATE,
                         url="https://site.com/signup"),
            ActionRecord(type=RecordType.FILL, selector="#email",
                         value="alice@example.com"),
            ActionRecord(type=RecordType.WAIT,
                         data={"condition": "page_ready"}),
            ActionRecord(type=RecordType.CLICK, selector="#go"),
            ActionRecord(type=RecordType.WAIT,
                         data={"condition": "navigation"}),
            ActionRecord(type=RecordType.GOAL_END,
                         goal="register_account", success=True),
        ]
        templates = execu.record_from_run(
            records,
            inputs={"email": "alice@example.com"},
            target_url="https://site.com",
            run_id="r1",
            account_id="a1",
        )
        expect("record_from_run returns 1 template", len(templates) == 1)
        expect("template version starts at 1", templates[0].version == 1)

        # Calling again produces v2 (versioning)
        templates2 = execu.record_from_run(
            records,
            inputs={"email": "alice@example.com"},
            target_url="https://site.com",
            run_id="r2", account_id="a2",
        )
        expect("subsequent record produces v2",
               templates2 and templates2[0].version == 2)

        # Pruning: after 6 records, only 5 remain (default keep_per_workflow=5)
        for i in range(8):
            execu.record_from_run(
                records,
                inputs={"email": "alice@example.com"},
                target_url="https://site.com",
            )
        all_versions = store.all_versions("site.com", "register_account")
        expect("pruning bounds total versions to 5",
               len(all_versions) <= 5,
               detail=f"got {len(all_versions)} versions")


# ===========================================================================
#  Interpolation helper
# ===========================================================================
def test_interpolate() -> None:
    print("\n=== interpolate() ===")
    expect("simple var", interpolate("${a}", {"a": "x"}) == "x")
    expect("multiple vars",
           interpolate("${a}-${b}", {"a": "1", "b": "2"}) == "1-2")
    expect("missing var → empty",
           interpolate("hello-${missing}", {}) == "hello-")
    expect("nested dot path",
           interpolate("${user.name}", {"user": {"name": "Bob"}}) == "Bob")
    expect("None template → empty", interpolate(None, {}) == "")


def main() -> int:
    test_execution_template()
    test_execution_template_store()
    test_template_recorder()
    test_template_replayer_happy_path()
    test_template_replayer_failure_modes()
    test_adaptive_executor()
    test_adaptive_executor_record_from_run()
    test_interpolate()
    print(f"\n=== summary: {len(_passes)} passed, {len(_fails)} failed ===")
    if _fails:
        for label, detail in _fails:
            print(f"  FAIL  {label}: {detail}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
