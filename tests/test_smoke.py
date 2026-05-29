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



# --------------------------------------------------------------------------
# Browser overrides: proxy URL parsing, fingerprint fields, profile sharing.
# These cover everything that runs without Playwright installed; the
# session-creation path is covered by the integration tests we run when
# Playwright is available.
# --------------------------------------------------------------------------


def test_browser_overrides_from_metadata_full():
    from automation.browser.manager import BrowserOverrides

    ov = BrowserOverrides.from_metadata({
        "proxy": "http://user:secret@proxy.example.com:8080",
        "user_agent": "UA/1.0",
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "extra_http_headers": {"X-Test": "yes"},
        "geolocation": {"latitude": 40.7, "longitude": -74.0, "accuracy": 50},
        "permissions": ["geolocation"],
        "color_scheme": "dark",
        "device_scale_factor": 2,
        "profile_id": "device-X",
        # unknown keys are tolerated:
        "group": "matrix",
    })
    assert ov.proxy == {
        "server": "http://proxy.example.com:8080",
        "username": "user",
        "password": "secret",
    }
    assert ov.user_agent == "UA/1.0"
    assert ov.viewport == (1366, 768)
    assert ov.locale == "en-US"
    assert ov.timezone_id == "America/New_York"
    assert ov.extra_http_headers == {"X-Test": "yes"}
    assert ov.geolocation == {"latitude": 40.7, "longitude": -74.0, "accuracy": 50.0}
    assert ov.permissions == ["geolocation"]
    assert ov.color_scheme == "dark"
    assert ov.device_scale_factor == 2.0
    assert ov.profile_id == "device-X"


def test_browser_overrides_proxy_variants():
    from automation.browser.manager import BrowserOverrides

    # plain URL, no creds, no port
    o = BrowserOverrides.from_metadata({"proxy": "http://proxy.example.com"})
    assert o.proxy == {"server": "http://proxy.example.com"}

    # socks5 with creds
    o = BrowserOverrides.from_metadata({"proxy": "socks5://u:p@s.example:1080"})
    assert o.proxy == {
        "server": "socks5://s.example:1080",
        "username": "u",
        "password": "p",
    }

    # already-shaped dict passes through
    o = BrowserOverrides.from_metadata({
        "proxy": {"server": "http://x.example", "username": "a", "password": "b"},
    })
    assert o.proxy == {"server": "http://x.example", "username": "a", "password": "b"}

    # malformed → None (no crash)
    o = BrowserOverrides.from_metadata({"proxy": "not-a-url"})
    assert o.proxy is None
    o = BrowserOverrides.from_metadata({"proxy": ""})
    assert o.proxy is None
    o = BrowserOverrides.from_metadata({"proxy": {"username": "x"}})  # missing server
    assert o.proxy is None


def test_browser_overrides_empty_is_noop():
    """Empty metadata → every field is None and merge_into doesn't touch base."""
    from automation.browser.manager import BrowserOverrides

    ov = BrowserOverrides.from_metadata({})
    assert all(getattr(ov, f) is None for f in (
        "proxy", "user_agent", "viewport", "locale", "timezone_id",
        "extra_http_headers", "geolocation", "permissions",
        "color_scheme", "device_scale_factor", "profile_id",
    ))
    base = {"headless": True, "user_agent": "default-UA", "viewport": {"width": 800, "height": 600}}
    out = ov.merge_into(dict(base))
    assert out == base  # nothing was overwritten


def test_browser_overrides_merge_partial():
    """Only non-None fields overwrite the base config."""
    from automation.browser.manager import BrowserOverrides

    ov = BrowserOverrides.from_metadata({
        "proxy": "http://p.example",
        "viewport": {"width": 1024, "height": 768},
    })
    base = {
        "headless": True,
        "user_agent": "default-UA",
        "viewport": {"width": 800, "height": 600},
        "locale": "en-US",
    }
    out = ov.merge_into(dict(base))
    # overridden:
    assert out["proxy"] == {"server": "http://p.example"}
    assert out["viewport"] == {"width": 1024, "height": 768}
    # untouched:
    assert out["headless"] is True
    assert out["user_agent"] == "default-UA"
    assert out["locale"] == "en-US"


def test_browser_overrides_viewport_shapes():
    from automation.browser.manager import BrowserOverrides

    assert BrowserOverrides.from_metadata({"viewport": [800, 600]}).viewport == (800, 600)
    assert BrowserOverrides.from_metadata(
        {"viewport": {"width": 1280, "height": 800}}
    ).viewport == (1280, 800)
    # malformed → None
    assert BrowserOverrides.from_metadata({"viewport": "1280x800"}).viewport is None
    assert BrowserOverrides.from_metadata({"viewport": [800]}).viewport is None
    assert BrowserOverrides.from_metadata({"viewport": {"width": 0, "height": 0}}).viewport is None


def test_browser_manager_resolves_profile_dir(tmp_path: Path):
    """profile_id overrides the directory key; default is account_id."""
    from automation.browser.manager import (
        BrowserConfig, BrowserManager, BrowserOverrides,
    )

    cfg = BrowserConfig(profiles_root=tmp_path / "profiles")
    bm = BrowserManager(cfg)
    # default: account_id is the dir
    p1 = bm._resolve_profile_dir("acct-1", BrowserOverrides())  # noqa: SLF001
    assert p1 == cfg.profiles_root / "acct-1"
    # override: shared profile_id wins
    p2 = bm._resolve_profile_dir(  # noqa: SLF001
        "acct-2", BrowserOverrides(profile_id="device-X"),
    )
    p3 = bm._resolve_profile_dir(  # noqa: SLF001
        "acct-3", BrowserOverrides(profile_id="device-X"),
    )
    assert p2 == p3 == cfg.profiles_root / "device-X"


def test_browser_manager_build_launch_kwargs_strips_none(tmp_path: Path):
    from automation.browser.manager import (
        BrowserConfig, BrowserManager, BrowserOverrides,
    )

    cfg = BrowserConfig(
        profiles_root=tmp_path / "p",
        downloads_dir=tmp_path / "dl",
        viewport=(800, 600),
        # user_agent / locale / timezone unset -> shouldn't appear in kwargs
    )
    bm = BrowserManager(cfg)
    ov = BrowserOverrides.from_metadata({
        "proxy": "http://p.example",
        "locale": "fr-FR",
    })
    k = bm._build_launch_kwargs(ov)  # noqa: SLF001
    # base config supplied
    assert k["headless"] is True
    assert k["viewport"] == {"width": 800, "height": 600}
    # override applied
    assert k["proxy"] == {"server": "http://p.example"}
    assert k["locale"] == "fr-FR"
    # None values were filtered out
    for none_key in ("user_agent", "timezone_id"):
        assert none_key not in k


# --------------------------------------------------------------------------
# Workflow engine: interpolation now works inside wait.selector and verify
# (regression test for the fix introduced alongside identity_test_register).
# --------------------------------------------------------------------------


class _FakePage:
    """Minimal stub satisfying the workflow engine's `verify` step."""

    def __init__(self, url: str = "https://x.test/welcome", title: str = "Welcome",
                 visible: dict[str, bool] | None = None) -> None:
        self.url = url
        self._title = title
        self._visible = visible or {}
        self.waited_for: list[str] = []

    async def title(self) -> str:
        return self._title

    async def is_visible(self, sel: str) -> bool:
        return self._visible.get(sel, False)

    async def wait_for_selector(self, sel: str, timeout: int = 0) -> None:
        self.waited_for.append(sel)


@pytest.mark.asyncio
async def test_workflow_verify_interpolates_inputs():
    from automation.controllers.workflow_engine import (
        Workflow, WorkflowEngine, WorkflowStep,
    )
    we = WorkflowEngine()
    page = _FakePage(url="https://x.test/welcome")
    wf = Workflow(name="t", steps=[
        WorkflowStep(
            type="verify",
            params={"url_contains": "${success_url_part}"},
        ),
    ])
    res = await we.run(wf, page=page, inputs={"success_url_part": "/welcome"})
    assert res.status.value == "succeeded"


@pytest.mark.asyncio
async def test_workflow_wait_interpolates_selector():
    from automation.controllers.workflow_engine import (
        Workflow, WorkflowEngine, WorkflowStep,
    )
    we = WorkflowEngine()
    page = _FakePage()
    wf = Workflow(name="t", steps=[
        WorkflowStep(
            type="wait",
            params={"selector": "${duplicate_selector}"},
            timeout_ms=1000,
        ),
    ])
    res = await we.run(wf, page=page, inputs={"duplicate_selector": ".error-banner"})
    assert res.status.value == "succeeded"
    assert page.waited_for == [".error-banner"]


@pytest.mark.asyncio
async def test_workflow_skip_when_input_empty():
    """Conditional `if` reads inputs and lets empty strings short-circuit steps."""
    from automation.controllers.workflow_engine import (
        Workflow, WorkflowEngine, WorkflowStep, WorkflowStatus,
    )
    we = WorkflowEngine()
    page = _FakePage()
    wf = Workflow(name="t", steps=[
        WorkflowStep(
            type="wait",
            params={"selector": "${duplicate_selector}"},
            if_="duplicate_selector != ''",
            timeout_ms=1000,
        ),
    ])
    res = await we.run(wf, page=page, inputs={"duplicate_selector": ""})
    assert res.status.value == "succeeded"
    assert res.records[0].status == WorkflowStatus.SKIPPED
    assert page.waited_for == []
