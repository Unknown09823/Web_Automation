"""Smoke tests covering the framework's pure-Python surface.

These tests do not require any third-party packages beyond the standard
library + ``pytest``. Browser tests live elsewhere (and require Playwright).
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from automation.accounts.manager import AccountManager, AccountStatus
from automation.ai.brain import AIBrain
from automation.ai.intents import IntentMatcher
from automation.ai.memory import AIMemory
from automation.ai.perception import PagePerception
from automation.ai.planner import ActionPlanner
from automation.controllers.workflow_engine import (
    Workflow,
    WorkflowEngine,
    WorkflowStep,
)
from automation.core.event_bus import Event, EventBus
from automation.core.task_manager import Task, TaskManager
from automation.distributed.coordinator import Coordinator


@pytest.mark.asyncio
async def test_event_bus_isolates_handler_failures():
    bus = EventBus()
    received: list[str] = []

    async def good(event: Event) -> None:
        received.append(event.name)

    async def bad(event: Event) -> None:
        raise RuntimeError("boom")

    await bus.subscribe("hi", good)
    await bus.subscribe("hi", bad)
    await bus.publish(Event("hi"))
    assert received == ["hi"]


@pytest.mark.asyncio
async def test_task_manager_runs_to_completion():
    tm = TaskManager(EventBus())

    async def work() -> int:
        return 1

    t = await tm.submit(Task(name="t", func=work))
    for _ in range(20):
        if t.status.value in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    assert t.status.value == "completed"
    assert t.result == 1


@pytest.mark.asyncio
async def test_account_manager_retry_then_fail(tmp_path: Path):
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps([{"id": "u1", "username": "a", "password": "p"}]))
    am = AccountManager(source_file=src, state_file=tmp_path / "state.json", max_attempts=2)
    am.load()
    a = await am.claim_next()
    assert a and a.status == AccountStatus.RUNNING
    await am.mark_failed(a.id, "oops")
    assert am.get(a.id).status == AccountStatus.PENDING
    a2 = await am.claim_next()
    assert a2 is not None
    await am.mark_failed(a2.id, "oops")
    assert am.get(a2.id).status == AccountStatus.FAILED


def test_intent_matcher_synonyms():
    matcher = IntentMatcher()
    sign_in = matcher.match(text="Sign In", role="button")
    assert any(m.intent == "login" for m in sign_in)
    create = matcher.match(text="Create Account", role="link")
    assert any(m.intent == "register" for m in create)
    pwd = matcher.match(placeholder="Password", role="textbox")
    assert any(m.intent == "password_field" for m in pwd)


def test_perception_offline_html():
    p = PagePerception()
    snap = p.capture_from_html(
        '<button>Sign In</button>'
        '<input type="email" name="email" placeholder="Email">'
        '<input type="password" name="password" placeholder="Password">',
        url="https://x.test/login",
        title="Login",
    )
    assert snap.signature
    assert snap.by_intent("login")
    assert snap.by_intent("password_field")


@pytest.mark.asyncio
async def test_planner_login_template():
    p = PagePerception()
    snap = p.capture_from_html(
        '<input type="email" name="email" placeholder="Email">'
        '<input type="password" name="password" placeholder="Password">'
        '<button type="submit">Sign In</button>',
        url="https://x.test/login",
        title="Login",
    )
    planner = ActionPlanner()
    plan = await planner.plan("login", snap, inputs={"username": "u", "password": "p"})
    actions = [s.action.value for s in plan.steps]
    assert "fill" in actions
    assert "click" in actions


@pytest.mark.asyncio
async def test_memory_persists_selectors(tmp_path: Path):
    m = AIMemory(path=tmp_path / "mem.sqlite")
    await m.remember_selector("sig", "login", "#login", "css", success=True)
    await m.remember_selector("sig", "login", "#login", "css", success=True)
    rows = await m.get_selectors("sig", "login")
    assert rows and rows[0].success_count == 2


@pytest.mark.asyncio
async def test_brain_dry_run_decides_without_browser():
    brain = AIBrain(enabled=True, dry_run=True)
    p = PagePerception()
    snap = p.capture_from_html(
        '<button>Sign In</button><input type="password" name="p">',
        url="https://x.test", title="Login",
    )
    plan = await brain.decide("login", snap, inputs={"password": "pw"})
    assert plan.steps


@pytest.mark.asyncio
async def test_workflow_set_and_log():
    we = WorkflowEngine()
    wf = Workflow(name="t", steps=[
        WorkflowStep(type="set", params={"vars": {"x": "hello"}}),
        WorkflowStep(type="log", params={"message": "value=${x}"}),
    ])
    res = await we.run(wf)
    assert res.status.value == "succeeded"


@pytest.mark.asyncio
async def test_coordinator_assignment_lifecycle():
    c = Coordinator()
    w = await c.register_worker(host="h", capabilities=["browser"])
    a = await c.submit("workflow", {"name": "login"})
    claimed = await c.claim(w.id)
    assert claimed and claimed.id == a.id
    assert await c.report(w.id, claimed.id, "done")
    assert c.stats()["workers"] == 1




# --------------------------------------------------------------------------
# Account manager: new envelope format, validation, locking, hot reload,
# result history, processing speed.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_envelope_format(tmp_path: Path):
    """Source can be {"accounts": [...]} envelope with number+password."""
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps({"accounts": [
        {"number": "9999999999", "password": "p1"},
        {"number": "8888888888", "password": "p2"},
    ]}))
    am = AccountManager(
        source_file=src, state_file=tmp_path / "accounts.sqlite", max_attempts=2,
    )
    n = am.load()
    assert n == 2
    by_num = {a.number: a for a in am.all()}
    assert "9999999999" in by_num
    assert by_num["9999999999"].password == "p1"
    assert am.stats()["pending"] == 2
    am.close()


@pytest.mark.asyncio
async def test_account_validation_rejects_bad_rows(tmp_path: Path):
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps({"accounts": [
        {"number": "9999999999", "password": "p1"},   # ok
        {"number": "9999999999", "password": "p2"},   # duplicate
        {"number": "9999999999", "password": ""},     # empty password
        {"password": "p3"},                           # no identifier
        "not-an-object",                              # wrong type
        {"number": "abc", "password": "p4"},          # invalid number
    ]}))
    am = AccountManager(
        source_file=src, state_file=tmp_path / "accounts.sqlite", max_attempts=2,
    )
    n = am.load()
    assert n == 1
    rejected = am.rejected()
    assert len(rejected) == 5
    reasons = " ".join(r["reason"] for r in rejected)
    assert "duplicate" in reasons
    assert "missing or empty password" in reasons
    assert "missing identifier" in reasons
    assert "invalid number format" in reasons
    am.close()


@pytest.mark.asyncio
async def test_account_concurrent_claims_no_duplicate(tmp_path: Path):
    """Two workers calling claim_next concurrently get different accounts."""
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps({"accounts": [
        {"number": "1111111111", "password": "p1"},
        {"number": "2222222222", "password": "p2"},
        {"number": "3333333333", "password": "p3"},
    ]}))
    am = AccountManager(
        source_file=src, state_file=tmp_path / "accounts.sqlite", max_attempts=3,
    )
    am.load()
    a, b, c, d = await asyncio.gather(
        am.claim_next("worker-A"),
        am.claim_next("worker-B"),
        am.claim_next("worker-C"),
        am.claim_next("worker-D"),  # nothing left
    )
    claimed = [x for x in (a, b, c, d) if x]
    assert len(claimed) == 3
    ids = {x.id for x in claimed}
    assert len(ids) == 3, "two workers got the same account"
    locked_by = {x.id: x.locked_by for x in claimed}
    assert set(locked_by.values()) == {"worker-A", "worker-B", "worker-C"}
    am.close()


@pytest.mark.asyncio
async def test_account_lease_expires_and_reclaim(tmp_path: Path):
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps({"accounts": [
        {"number": "1111111111", "password": "p1"},
    ]}))
    am = AccountManager(
        source_file=src,
        state_file=tmp_path / "accounts.sqlite",
        max_attempts=5,
        lease_seconds=0.1,  # tiny lease
    )
    am.load()
    first = await am.claim_next("worker-A")
    assert first is not None
    # second claim immediately must fail (still leased)
    none = await am.claim_next("worker-B", lease_seconds=0.1)
    assert none is None
    await asyncio.sleep(0.2)
    # after lease expiry it becomes claimable again
    again = await am.claim_next("worker-B", lease_seconds=0.1)
    assert again is not None
    assert again.id == first.id
    assert again.locked_by == "worker-B"
    am.close()


@pytest.mark.asyncio
async def test_account_reload_preserves_completed(tmp_path: Path):
    """A hot reload must not reset completed/failed accounts."""
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps({"accounts": [
        {"number": "1111111111", "password": "p1"},
        {"number": "2222222222", "password": "p2"},
    ]}))
    am = AccountManager(
        source_file=src, state_file=tmp_path / "accounts.sqlite", max_attempts=2,
    )
    am.load()
    a = await am.claim_next("w1")
    assert a is not None
    await am.mark_completed(a.id, workflow="login")
    assert am.get(a.id).status == AccountStatus.COMPLETED
    # rewrite the source file with the same accounts plus one more
    src.write_text(json.dumps({"accounts": [
        {"number": "1111111111", "password": "CHANGED"},
        {"number": "2222222222", "password": "p2"},
        {"number": "3333333333", "password": "p3"},
    ]}))
    n = await am.reload()
    assert n == 3
    # completed account stays completed
    assert am.get(a.id).status == AccountStatus.COMPLETED
    # but the static field (password) was refreshed
    assert am.get(a.id).password == "CHANGED"
    am.close()


@pytest.mark.asyncio
async def test_account_results_and_speed(tmp_path: Path):
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps({"accounts": [
        {"number": "1111111111", "password": "p"},
    ]}))
    am = AccountManager(
        source_file=src, state_file=tmp_path / "accounts.sqlite", max_attempts=2,
    )
    am.load()
    a = await am.claim_next("w")
    assert a is not None
    started = a.started_at or 0.0
    await am.mark_completed(
        a.id, workflow="register", started_at=started, result={"k": 1},
    )
    rows = am.results()
    assert len(rows) == 1
    r = rows[0]
    assert r["account_id"] == a.id
    assert r["success"] is True
    assert r["workflow"] == "register"
    assert r["duration_ms"] >= 0
    speed = am.processing_speed(window_seconds=60.0)
    assert speed["completed"] == 1
    assert speed["per_minute"] > 0
    am.close()


@pytest.mark.asyncio
async def test_account_release_lock(tmp_path: Path):
    src = tmp_path / "accounts.json"
    src.write_text(json.dumps({"accounts": [
        {"number": "1111111111", "password": "p"},
    ]}))
    am = AccountManager(
        source_file=src, state_file=tmp_path / "accounts.sqlite", max_attempts=3,
    )
    am.load()
    a = await am.claim_next("w1")
    assert a and a.locked_by == "w1"
    # wrong worker cannot release
    assert not await am.release_lock(a.id, worker_id="other")
    # right worker can; status unchanged
    assert await am.release_lock(a.id, worker_id="w1")
    s = am.get(a.id)
    assert s.locked_by is None
    assert s.status == AccountStatus.RUNNING
    am.close()
