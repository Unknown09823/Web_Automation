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
