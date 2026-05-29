"""Standalone test harness for the autonomous browser agent layer.

Runs without pytest (the sandbox can't reach PyPI). Mirrors the structure
of tests/test_smoke.py but exercises every new agent module + the bug
fixes from the audit pass.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# ----------------------------------------------------------------- harness
PASS = 0
FAIL = 0
FAILURES: list[tuple[str, str]] = []


def test(name: str):
    """Decorator that runs a sync or async test and prints a pass/fail line."""
    def deco(fn):
        global PASS, FAIL
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.get_event_loop().run_until_complete(fn())
            else:
                fn()
            PASS += 1
            print(f"  PASS  {name}")
        except AssertionError as exc:
            FAIL += 1
            FAILURES.append((name, f"AssertionError: {exc}"))
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            FAIL += 1
            tb = traceback.format_exc()
            FAILURES.append((name, tb))
            print(f"  FAIL  {name}: {exc!r}")
        return fn
    return deco


# ----------------------------------------------------------------- imports
print("\n=== imports ===")


@test("agent package imports")
def _t_imports():
    from automation.agent import (  # noqa: F401
        AgentEvent, AgentGoal, GoalDecomposer, RunContext,
    )
    from automation.agent.agent import BrowserAgent  # noqa: F401
    from automation.agent.checkpoints import (  # noqa: F401
        Checkpoint, CheckpointManager,
    )
    from automation.agent.data_factory import (  # noqa: F401
        DataFactory, GeneratedAccount,
    )
    from automation.agent.events import AgentEventType  # noqa: F401
    from automation.agent.goals import (  # noqa: F401
        GoalStatus, GoalType,
    )
    from automation.agent.nl_planner import (  # noqa: F401
        AccountGenConfig, ExecutionPlan, NLPlanner,
    )
    from automation.agent.recorder import (  # noqa: F401
        ActionRecord, ActionRecorder, RecordType,
    )
    from automation.agent.recovery import (  # noqa: F401
        RecoveryAttempt, RecoveryResult, RecoveryStack,
    )
    from automation.agent.run import (  # noqa: F401
        RunContext, RunStatus, list_runs,
    )
    from automation.agent.verifier import (  # noqa: F401
        SuccessVerifier, VerificationResult,
    )
    from automation.agent.waiter import (  # noqa: F401
        AdaptiveWaiter, WaitCondition, WaitResult,
    )


# ----------------------------------------------------------------- NL planner
print("\n=== NL planner ===")


@test("nl: extracts count, url, password without trailing punctuation")
async def _t_nl_basic():
    from automation.agent.nl_planner import NLPlanner

    plan = await NLPlanner().parse(
        "Create 20 accounts on https://example.com/signup. "
        "Use random 10-digit mobile numbers. Password: Test@123. "
        "After registration, login and complete onboarding."
    )
    ac = plan.account_config
    assert ac.count == 20, ac.count
    assert plan.target_url == "https://example.com/signup", plan.target_url
    # BUG FIX: trailing period is stripped
    assert ac.password == "Test@123", repr(ac.password)
    assert ac.generate_numbers is True
    assert ac.number_length == 10
    assert plan.parallel is True
    types = [g.type.value for g in plan.goals]
    assert "register_account" in types
    assert "login" in types
    assert "complete_onboarding" in types


@test("nl: goals appear in text-position order")
async def _t_nl_order():
    from automation.agent.nl_planner import NLPlanner

    # logout BEFORE login in text — even though dict has logout listed earlier
    plan = await NLPlanner().parse("First logout, then login again.")
    types = [g.type.value for g in plan.goals]
    # logout text position < login text position
    assert types.index("logout") < types.index("login"), types


@test("nl: round-trips through to_dict / from_dict")
async def _t_nl_roundtrip():
    from automation.agent.nl_planner import ExecutionPlan, NLPlanner

    p1 = await NLPlanner().parse(
        "Register 5 users with random emails, password=Hunter2"
    )
    d = p1.to_dict()
    p2 = ExecutionPlan.from_dict(d)
    assert p2.account_config.count == p1.account_config.count
    assert p2.account_config.password == p1.account_config.password
    assert [g.type.value for g in p2.goals] == [g.type.value for g in p1.goals]
    # round-trip is stable
    assert p2.to_dict() == p1.to_dict()


@test("nl: empty string is handled gracefully")
async def _t_nl_empty():
    from automation.agent.nl_planner import NLPlanner

    plan = await NLPlanner().parse("")
    assert plan.account_config.count == 1
    assert len(plan.notes) >= 1  # warns about missing url


# ----------------------------------------------------------------- data factory
print("\n=== data factory ===")


@test("factory: deterministic with seed")
def _t_factory_seed():
    from automation.agent.data_factory import DataFactory

    a1 = DataFactory(seed=42).generate(
        3, password="x", generate_numbers=True, number_length=10,
    )
    a2 = DataFactory(seed=42).generate(
        3, password="x", generate_numbers=True, number_length=10,
    )
    assert [a.number for a in a1] == [a.number for a in a2]


@test("factory: number length honored, no leading zero in body")
def _t_factory_length():
    from automation.agent.data_factory import DataFactory

    accounts = DataFactory().generate(
        50, password="x", generate_numbers=True, number_length=10,
    )
    for a in accounts:
        assert len(a.number) == 10, a.number
        assert a.number[0] != "0", a.number


@test("factory: random password meets complexity")
def _t_factory_password():
    from automation.agent.data_factory import DataFactory

    accounts = DataFactory().generate(20)
    for a in accounts:
        # at least 12 chars; has letter, digit, and special
        assert len(a.password) >= 12
        assert any(c.isalpha() for c in a.password)
        assert any(c.isdigit() for c in a.password)
        assert any(c in "!@#$%" for c in a.password)


@test("factory: csv import")
def _t_factory_csv():
    from automation.agent.data_factory import DataFactory

    rows = DataFactory().from_csv(
        "number,password\n9876543210,Test@123\n8765432109,Pass@456"
    )
    assert len(rows) == 2
    assert rows[0].number == "9876543210"
    assert rows[1].password == "Pass@456"


@test("factory: list import")
def _t_factory_list():
    from automation.agent.data_factory import DataFactory

    rows = DataFactory().from_list([
        {"number": "1111111111", "password": "p1"},
        {"email": "a@b.com", "password": "p2"},
    ])
    assert rows[0].number == "1111111111"
    assert rows[1].email == "a@b.com"


# ----------------------------------------------------------------- goals
print("\n=== goals ===")


@test("goals: register_account decomposes to navigate + fill + submit")
def _t_goals_decompose():
    from automation.agent.goals import (
        AgentGoal, GoalDecomposer, GoalType,
    )

    goal = AgentGoal(type=GoalType.REGISTER_ACCOUNT)
    subs = GoalDecomposer().decompose(goal)
    types = [g.type.value for g in subs]
    assert types == ["navigate", "custom", "custom"], types
    assert subs[1].params.get("ai_goal") == "register"
    assert subs[2].params.get("ai_goal") == "submit"


@test("goals: round-trip via to_dict/from_dict, including nested params")
def _t_goals_roundtrip():
    from automation.agent.goals import AgentGoal, GoalType

    g1 = AgentGoal(
        type=GoalType.LOGIN,
        description="Login flow",
        params={"url": "https://x.test", "extra": {"foo": [1, 2]}},
        verification_hints=["dashboard"],
        max_retries=5,
    )
    g2 = AgentGoal.from_dict(g1.to_dict())
    assert g2.type == g1.type
    assert g2.params == g1.params
    assert g2.verification_hints == g1.verification_hints
    assert g2.max_retries == 5


# ----------------------------------------------------------------- run / checkpoints
print("\n=== run / checkpoints / recorder ===")


@test("run: lifecycle + screenshots/html/cookies/logs dirs")
def _t_run_lifecycle():
    from automation.agent.run import RunContext, RunStatus, list_runs

    with tempfile.TemporaryDirectory() as tmp:
        run = RunContext.create(runs_root=tmp, instruction="x", accounts=["a1"])
        d = run.account_dir("a1")
        for sub in ("screenshots", "html", "cookies", "logs"):
            assert (d / sub).is_dir()
        run.set_status(RunStatus.RUNNING)
        assert run.started_at is not None
        run.append_event({"k": "v"})
        events = run.load_events()
        assert events == [{"k": "v"}]
        run.save_memory("a1", {"step": 1})
        assert run.load_memory("a1")["step"] == 1
        run.set_status(RunStatus.COMPLETED)
        # reload from disk
        run2 = RunContext.load(run.base_dir)
        assert run2.status == RunStatus.COMPLETED
        # listing
        runs = list_runs(tmp)
        assert len(runs) == 1
        assert runs[0]["run_id"] == run.run_id


@test("checkpoints: resume_index points after last successful goal")
def _t_checkpoints():
    from automation.agent.checkpoints import Checkpoint, CheckpointManager
    from automation.agent.run import RunContext

    with tempfile.TemporaryDirectory() as tmp:
        run = RunContext.create(runs_root=tmp)
        cm = CheckpointManager(run.base_dir)
        cm.save(Checkpoint(
            account_id="a", goal_index=0, goal_type="navigate",
            goal_description="x", status="completed",
        ))
        cm.save(Checkpoint(
            account_id="a", goal_index=1, goal_type="custom",
            goal_description="y", status="completed",
        ))
        cm.save(Checkpoint(
            account_id="a", goal_index=2, goal_type="custom",
            goal_description="z", status="failed",
        ))
        assert cm.resume_index("a") == 2
        last = cm.last_successful("a")
        assert last and last.goal_index == 1


@test("recorder: round-trips through replay.json")
def _t_recorder():
    from automation.agent.recorder import ActionRecorder

    with tempfile.TemporaryDirectory() as tmp:
        rec = ActionRecorder(Path(tmp) / "replay.json")
        rec.record_navigate("https://x", 100)
        rec.record_click("#go", "https://x", 50)
        rec.record_fill("input", "v", "https://x", 5)
        rec.record_ai_decision("login", {"steps": []}, 0.9)
        rec.flush()
        # reload
        rec2 = ActionRecorder(Path(tmp) / "replay.json")
        assert len(rec2.records) == 4
        assert rec2.records[0].type.value == "navigate"
        assert rec2.records[3].data["confidence"] == 0.9


# ----------------------------------------------------------------- agent
print("\n=== agent ===")


@test("agent: prepare_run returns immediately with real run_id (no race)")
async def _t_agent_prepare_run():
    from automation.agent.agent import BrowserAgent
    from automation.agent.goals import AgentGoal, GoalType

    with tempfile.TemporaryDirectory() as tmp:
        agent = BrowserAgent(runs_root=tmp)
        goals = [AgentGoal(type=GoalType.LOGIN)]
        run = agent.prepare_run(
            goals=goals, account_ids=["a1"],
            target_url="https://x.test",
        )
        # synchronous return
        assert run.run_id.startswith("run_")
        assert run.run_id in agent.active_runs
        # plan persisted to disk
        plan_path = run.base_dir / "plan.json"
        assert plan_path.exists()
        data = json.loads(plan_path.read_text())
        # decomposed login -> 3 sub-goals
        assert len(data["goals"]) == 3
        # navigate goals got the target_url injected
        nav_goals = [g for g in data["goals"] if g["type"] == "navigate"]
        assert nav_goals[0]["params"]["url"] == "https://x.test"


@test("agent: cancel marks run as cancellation-requested and emits event")
async def _t_agent_cancel():
    from automation.agent.agent import BrowserAgent
    from automation.agent.goals import AgentGoal, GoalType

    with tempfile.TemporaryDirectory() as tmp:
        agent = BrowserAgent(runs_root=tmp)
        run = agent.prepare_run(
            goals=[AgentGoal(type=GoalType.CUSTOM, description="x")],
            account_ids=["a1"],
        )
        # cancel BEFORE execute_prepared starts work
        ok = await agent.cancel(run.run_id)
        assert ok is True
        # now execute — should short-circuit and emit RUN_CANCELLED
        await agent.execute_prepared(run)
        events = run.load_events()
        types = [e["type"] for e in events]
        assert "agent.run.started" in types
        assert "agent.run.cancelled" in types
        # RUN_COMPLETED should NOT be emitted
        assert "agent.run.completed" not in types


@test("agent: execute() with no browser fails goals gracefully (page=None)")
async def _t_agent_no_browser():
    from automation.agent.agent import BrowserAgent
    from automation.agent.goals import AgentGoal, GoalType

    with tempfile.TemporaryDirectory() as tmp:
        agent = BrowserAgent(runs_root=tmp)
        run = await agent.execute(
            goals=[AgentGoal(type=GoalType.CUSTOM, description="t")],
            account_ids=["a1"],
        )
        # no browser -> attempts return False -> goal fails -> run completes
        # (status is COMPLETED because run finished, not because goals succeeded)
        from automation.agent.run import RunStatus
        assert run.status in (RunStatus.COMPLETED, RunStatus.FAILED)
        events = run.load_events()
        types = {e["type"] for e in events}
        # goal_failed and account_failed should be emitted
        assert "agent.account.failed" in types
        assert "agent.goal.failed" in types


# ----------------------------------------------------------------- account manager
print("\n=== account manager add_accounts ===")


@test("accounts.add_accounts inserts directly without touching source_file")
async def _t_add_accounts():
    from automation.accounts.manager import AccountManager

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "accounts.json"
        src.write_text(json.dumps({"accounts": [
            {"number": "1111111111", "password": "p1"},
        ]}))
        am = AccountManager(
            source_file=src, state_file=Path(tmp) / "state.sqlite",
            max_attempts=2,
        )
        am.load()
        assert am.stats()["pending"] == 1

        # use add_accounts (the new API) — source_file is unchanged
        result = await am.add_accounts([
            {"number": "2222222222", "password": "p2"},
            {"number": "3333333333", "password": "p3"},
            # an invalid one to verify rejection still works
            {"number": "abc", "password": "p4"},
        ])
        assert len(result.valid) == 2
        assert len(result.rejected) == 1

        assert am.stats()["pending"] == 3  # original + 2 new
        # source file content is untouched
        assert src.read_text().count("1111111111") == 1
        assert "2222222222" not in src.read_text()
        am.close()


# ----------------------------------------------------------------- recovery
print("\n=== recovery (no browser) ===")


@test("recovery: returns not-recovered with a list of attempts")
async def _t_recovery_no_page():
    from automation.agent.recovery import RecoveryStack

    rs = RecoveryStack()

    class _StubPage:
        async def evaluate(self, *a, **k):
            raise RuntimeError("no page")

        async def is_visible(self, sel):
            return False

        async def wait_for_load_state(self, *a, **k):
            return None

        @property
        def keyboard(self):
            class K:
                async def press(self, *a):
                    return None
            return K()

    result = await rs.recover(_StubPage(), goal="login")
    # All strategies should fail without a real browser
    assert result.recovered is False
    assert len(result.attempts) >= 1


# ----------------------------------------------------------------- summary
print()
print(f"=== summary: {PASS} passed, {FAIL} failed ===")
if FAIL:
    print("\nFailures:")
    for name, tb in FAILURES:
        print(f"  - {name}")
        print("    " + tb.splitlines()[-1])
    sys.exit(1)
sys.exit(0)
