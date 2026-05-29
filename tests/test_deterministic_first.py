"""Tests for the deterministic-first agent stack.

These tests stay inside the boundary that ``pytest`` alone (no
``pytest-asyncio``) can run: every async API is exercised through
:py:func:`asyncio.run` inside a sync test function. That keeps the
suite green in CI configurations that ship a minimal pytest install
*and* still gives us a meaningful seam for the new modules.

Coverage map
============

* ``reasoning.py``         — entry round-trip + tail() ordering
* ``popup_guard.py``       — accept/escape paths against a fake page
* ``heuristics.py``        — synonym groups for register / claim / fields
* ``rule_engine.py``       — built-in rule firing (captcha, popup,
                              session-expired, JSON load)
* ``site_memory.py``       — ranked button retrieval, persistence
* ``site_templates.py``    — Account #1 records, Account #2 replays
* ``deterministic_engine`` — priority order replay > heuristic > human
* ``nl_planner.py``        — multi-step instruction → ordered goals

Each test is short and asserts only the specific contract the module
promises, so a future refactor can move implementation details
without ripping the suite apart.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from automation.agent.deterministic_engine import DeterministicEngine
from automation.agent.goals import AgentGoal, GoalDecomposer, GoalType
from automation.agent.heuristics import Heuristics
from automation.agent.nl_planner import NLPlanner
from automation.agent.popup_guard import (
    DEFAULT_RULES,
    PopupGuard,
    PopupKind,
)
from automation.agent.reasoning import (
    DecisionSource,
    ReasoningEntry,
    ReasoningLog,
)
from automation.agent.rule_engine import (
    ActionKind,
    Observation,
    Rule,
    RuleEngine,
)
from automation.agent.site_memory import SiteMemory, _host_of, _path_of
from automation.agent.site_templates import (
    TemplateStep,
    TemplateStore,
    _url_to_pattern,
)
from automation.ai.perception import PagePerception


# --------------------------------------------------------------- reasoning
def test_reasoning_entry_round_trip(tmp_path: Path) -> None:
    """An entry written to the JSONL log should come back equal."""
    log = ReasoningLog(tmp_path / "r.jsonl")
    entry = ReasoningEntry(
        run_id="run_x",
        account_id="acc_001",
        goal_index=0,
        goal="register",
        observation="form visible",
        reasoning="heuristic match",
        action="fill+click",
        verification="ok",
        source=DecisionSource.HEURISTIC,
        confidence=0.91,
    )
    log.append(entry)
    log.append(entry)
    tail = log.tail(n=5)
    assert len(tail) == 2
    # tail() returns newest-first; both entries are identical so we
    # only need to check one fully.
    e = tail[0]
    assert e.run_id == "run_x"
    assert e.source is DecisionSource.HEURISTIC
    assert e.confidence == pytest.approx(0.91)


def test_reasoning_log_render_panel(tmp_path: Path) -> None:
    """``render_panel`` should be in chronological order, not newest-first."""
    log = ReasoningLog(tmp_path / "r.jsonl")
    for i in range(3):
        log.append(
            ReasoningEntry(
                run_id="r", account_id="a", goal_index=i,
                goal=f"goal-{i}",
            )
        )
    panel = log.render_panel(n=3)
    # Earliest goal must appear before the latest in the chat panel.
    assert panel.index("goal-0") < panel.index("goal-2")


# --------------------------------------------------------------- popup_guard
class _FakePage:
    """Minimum surface the popup_guard touches.

    Real Playwright pages already satisfy this; we use the stub so
    the tests run without browser binaries.
    """

    def __init__(self, visible: set[str]) -> None:
        self._visible = set(visible)
        self.clicks: list[str] = []
        self.keys: list[str] = []

    @property
    def keyboard(self) -> "_FakePage":
        return self

    async def is_visible(self, selector: str, *, timeout: int | None = None) -> bool:
        return selector in self._visible

    async def click(self, selector: str, *, timeout: int = 0) -> None:
        self.clicks.append(selector)
        # Mimic the side-effect of dismissing the overlay.
        self._visible.discard(selector)

    async def press(self, key: str) -> None:
        self.keys.append(key)


def test_popup_guard_accepts_cookie_banner() -> None:
    page = _FakePage({
        '[id*="cookie" i]',
        'button:has-text("Accept all")',
    })

    async def go():
        return await PopupGuard().scan_and_dismiss(page)

    result = asyncio.run(go())
    assert PopupKind.COOKIE_BANNER in result.found
    assert any(d.kind is PopupKind.COOKIE_BANNER for d in result.dismissed)
    assert any(d.strategy == "accept" for d in result.dismissed)
    # The "Accept all" click should be the first thing the guard did.
    assert page.clicks[0] == 'button:has-text("Accept all")'


def test_popup_guard_modal_falls_back_to_escape() -> None:
    # Container visible, no close selectors visible → keyboard fallback.
    page = _FakePage({'[role="dialog"]'})

    async def go():
        return await PopupGuard().scan_and_dismiss(page)

    result = asyncio.run(go())
    assert PopupKind.MODAL in result.found
    dismissed_kinds = {d.kind for d in result.dismissed}
    assert PopupKind.MODAL in dismissed_kinds
    assert any(d.strategy == "escape" for d in result.dismissed)
    assert page.keys == ["Escape"]


def test_popup_guard_clean_page_is_noop() -> None:
    page = _FakePage(set())
    result = asyncio.run(PopupGuard().scan_and_dismiss(page))
    assert not result.any_dismissed
    assert page.clicks == []
    # All 7 default rules should at least have been *checked* without
    # producing any "found" entries.
    assert len(DEFAULT_RULES) == 7


# --------------------------------------------------------------- heuristics
@pytest.mark.parametrize(
    "label",
    [
        "Claim Gift", "Claim Reward", "Get Bonus", "Redeem Reward",
        "Spin the Wheel", "Free Spin", "Collect Bonus",
    ],
)
def test_heuristics_claim_synonyms_all_resolve_to_claim_reward(label: str) -> None:
    p = PagePerception()
    snap = p.capture_from_html(f"<button>{label}</button>")
    best = Heuristics().best(snap, "claim_reward")
    assert best is not None, f"no candidate for {label!r}"
    assert best.score >= 0.55, f"low confidence {best.score:.2f} for {label!r}"


@pytest.mark.parametrize(
    "label",
    [
        "Sign Up", "Register", "Create Account", "Join Now", "Get Started",
    ],
)
def test_heuristics_register_synonyms(label: str) -> None:
    p = PagePerception()
    snap = p.capture_from_html(f"<button>{label}</button>")
    best = Heuristics().best(snap, "register")
    assert best is not None and best.score >= 0.55


def test_heuristics_form_plan_for_login() -> None:
    p = PagePerception()
    snap = p.capture_from_html(
        '<input type="email" name="email" placeholder="Email">'
        '<input type="password" name="password" placeholder="Password">'
        '<button>Sign In</button>',
    )
    plan = Heuristics().build_form_plan(
        snap,
        fields={"email_field": "u@x.com", "password_field": "pw"},
        submit_group="login",
    )
    actions = [s.action.value for s in plan.steps]
    assert plan.usable
    assert actions.count("fill") == 2
    assert "click" in actions
    # The submit step must be last.
    assert plan.steps[-1].action.value == "click"


# --------------------------------------------------------------- rule_engine
def test_rule_engine_captcha_marks_human() -> None:
    eng = RuleEngine()
    obs = Observation(
        url="https://x.test/login",
        body_text="Please complete the captcha to continue.",
    )
    ev = eng.evaluate(obs)
    triggered = ev.by_kind(ActionKind.MARK_HUMAN)
    assert triggered, "captcha should trigger MARK_HUMAN"
    # captcha rule has the lowest priority number; it should fire first.
    assert ev.triggered[0].action.kind is ActionKind.MARK_HUMAN


def test_rule_engine_popup_dismiss_recommended() -> None:
    eng = RuleEngine()
    obs = Observation(
        url="https://x.test/", popup_kinds=("cookie_banner",),
        body_text="welcome",
    )
    ev = eng.evaluate(obs)
    assert ev.by_kind(ActionKind.DISMISS_POPUP)


def test_rule_engine_session_expired_navigate() -> None:
    eng = RuleEngine()
    obs = Observation(
        url="https://x.test/account",
        body_text="Your session has timed out. Please log in.",
    )
    ev = eng.evaluate(obs)
    nav = ev.by_kind(ActionKind.NAVIGATE)
    assert nav and nav[0].action.params_dict.get("target") == "login"


def test_rule_engine_loads_user_rules_from_json(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps([{
            "name": "kyc_required",
            "priority": 25,
            "conditions": [
                {"kind": "body_contains_any",
                 "value": ["identity verification", "kyc"]},
            ],
            "actions": [{"kind": "mark_human", "params": {"reason": "kyc"}}],
        }])
    )
    eng = RuleEngine()
    loaded = eng.load(rules_path)
    assert loaded == 1
    obs = Observation(body_text="Please complete identity verification.")
    ev = eng.evaluate(obs)
    triggered = [t for t in ev.triggered if t.rule_name == "kyc_required"]
    assert triggered
    assert triggered[0].action.params_dict.get("reason") == "kyc"


# --------------------------------------------------------------- site_memory
def test_site_memory_buttons_ranked_by_confidence(tmp_path: Path) -> None:
    sm = SiteMemory(root=tmp_path)
    # 3 successes and 0 failures → high confidence.
    for _ in range(3):
        sm.record_button("https://x.test/login", "login", "#good", success=True)
    # 1 success → lower confidence.
    sm.record_button("https://x.test/login", "login", "#meh", success=True)
    # All failures → confidence 0.
    sm.record_button("https://x.test/login", "login", "#bad", success=False)

    btns = sm.get_buttons("https://x.test/login", "login")
    selectors = [b.selector for b in btns]
    assert selectors[0] == "#good"
    # The all-fail selector should rank last.
    assert selectors[-1] == "#bad"


def test_site_memory_round_trips_on_disk(tmp_path: Path) -> None:
    sm = SiteMemory(root=tmp_path)
    sm.record_button(
        "https://x.test/login", "login", "#login-btn", success=True,
    )
    sm.flush_all()

    sm2 = SiteMemory(root=tmp_path)
    btns = sm2.get_buttons("https://x.test/login", "login")
    assert btns and btns[0].selector == "#login-btn"
    assert btns[0].success_count == 1


def test_url_helpers_strip_query_and_lowercase_host() -> None:
    assert _host_of("https://Example.COM/path") == "example.com"
    assert _path_of("https://x.test/a/b?c=1#x") == "/a/b"


# --------------------------------------------------------------- site_templates
def test_template_store_records_and_replays(tmp_path: Path) -> None:
    store = TemplateStore(root=tmp_path)

    # Account #1 records its successful flow.
    rec = store.start_recording(
        host="example.com", goal="register",
        initial_url="https://example.com/signup",
        initial_signature="sig_abc",
    )
    rec.add_step(TemplateStep(
        action="fill", selector="#email", intent="email_field",
    ))
    rec.add_step(TemplateStep(
        action="click", selector="#submit", intent="register",
    ))
    template = store.commit(rec)
    assert template is not None
    assert template.version == 1

    # Account #2 finds the template via URL match.
    found = store.find_template(
        host="example.com", goal="register",
        url="https://example.com/signup",
    )
    assert found is not None
    assert [s.action for s in found.steps] == ["fill", "click"]


def test_template_store_dedupes_identical_recordings(tmp_path: Path) -> None:
    store = TemplateStore(root=tmp_path)

    def _record():
        rec = store.start_recording(host="x.test", goal="login")
        rec.add_step(TemplateStep(action="fill", selector="#u",
                                  intent="username_field"))
        rec.add_step(TemplateStep(action="click", selector="#go",
                                  intent="login"))
        return store.commit(rec)

    first = _record()
    second = _record()
    # Identical step shape → store treats the second commit as a
    # success bump on the first template, not as a v2. The first
    # commit starts at success_count=0 (it's the initial recording,
    # not a replay), so a single dedupe bumps the counter to 1.
    assert first is second
    assert first.success_count == 1


def test_url_to_pattern_handles_numeric_ids() -> None:
    pat = _url_to_pattern("https://example.com/users/42/profile")
    import re as _re
    assert _re.search(pat, "https://example.com/users/42/profile")
    assert _re.search(pat, "https://example.com/users/9001/profile")
    assert not _re.search(pat, "https://example.com/users/abc/profile")


# --------------------------------------------------------------- engine
def test_deterministic_engine_prefers_replay_over_heuristic(
    tmp_path: Path,
) -> None:
    """Account #1 should drive heuristics; Account #2 should hit the template."""
    sm = SiteMemory(root=tmp_path / "sm")
    ts = TemplateStore(root=tmp_path / "tpl")
    eng = DeterministicEngine(
        rules=RuleEngine(), heuristics=Heuristics(),
        site_memory=sm, templates=ts, brain=None,
    )

    p = PagePerception()
    snap = p.capture_from_html(
        '<input type="email" name="email" placeholder="Email">'
        '<input type="password" name="password" placeholder="Password">'
        '<button>Sign In</button>',
        url="https://example.com/login",
    )

    async def first_account():
        plan = await eng.decide(
            goal="login",
            page_url="https://example.com/login",
            page_signature=snap.signature,
            snapshot=snap,
            observation=Observation(url="https://example.com/login"),
            inputs={"username": "u", "password": "p"},
            host="example.com",
        )
        # Drive every step as success so the recording becomes a template.
        for step in plan.steps:
            eng.record_step_success(plan, step)
        eng.finalize(
            plan, success=True,
            final_url="https://example.com/welcome",
        )
        return plan

    async def second_account():
        return await eng.decide(
            goal="login",
            page_url="https://example.com/login",
            page_signature=snap.signature,
            snapshot=snap,
            observation=Observation(url="https://example.com/login"),
            inputs={"username": "u2", "password": "p2"},
            host="example.com",
        )

    first = asyncio.run(first_account())
    second = asyncio.run(second_account())

    assert first.source is DecisionSource.HEURISTIC
    assert second.source is DecisionSource.REPLAY
    # Per-account credentials should override the recorded ``value``.
    fill_steps = [s for s in second.steps if s.action.value == "fill"]
    assert any(s.value == "u2" for s in fill_steps)


def test_deterministic_engine_captcha_short_circuits_to_human(
    tmp_path: Path,
) -> None:
    eng = DeterministicEngine(
        rules=RuleEngine(),
        heuristics=Heuristics(),
        site_memory=SiteMemory(root=tmp_path / "sm"),
        templates=TemplateStore(root=tmp_path / "tpl"),
        brain=None,
    )
    p = PagePerception()
    snap = p.capture_from_html("<button>Sign In</button>")

    async def go():
        return await eng.decide(
            goal="login",
            page_url="https://x.test/login",
            page_signature=snap.signature,
            snapshot=snap,
            observation=Observation(
                url="https://x.test/login",
                body_text="please solve the captcha",
            ),
            inputs={},
            host="x.test",
        )

    plan = asyncio.run(go())
    assert plan.source is DecisionSource.HUMAN
    assert plan.steps[0].metadata.get("rule") == "captcha_human_handoff"


# --------------------------------------------------------------- nl_planner
def test_nl_planner_parses_full_user_example() -> None:
    text = (
        "Create 5 accounts using random 10-digit numbers. "
        "Password: Test@123. "
        "Visit website. Register. Login. Open rewards page. "
        "Claim gift. Open betting page. Place minimum bet."
    )
    plan = asyncio.run(NLPlanner().parse(text))
    assert plan.account_config.count == 5
    assert plan.account_config.password == "Test@123"
    assert plan.account_config.generate_numbers
    assert plan.account_config.number_length == 10
    types = [g.type for g in plan.goals]
    assert types == [
        GoalType.REGISTER_ACCOUNT,
        GoalType.LOGIN,
        GoalType.OPEN_REWARDS_PAGE,
        GoalType.CLAIM_REWARD,
        GoalType.OPEN_BETTING_PAGE,
        GoalType.PLACE_BET,
    ]


def test_nl_planner_word_boundary_avoids_false_positives() -> None:
    plan = asyncio.run(NLPlanner().parse(
        "I have already registered users that need to log in.",
    ))
    types = {g.type for g in plan.goals}
    # "registered" must NOT match the "register" keyword.
    assert GoalType.REGISTER_ACCOUNT not in types
    assert GoalType.LOGIN in types


def test_goal_decomposer_full_plan_is_flat_and_ordered() -> None:
    """build_plan flattens a v2 multi-goal sequence to single sub-goals
    that the deterministic engine knows how to execute."""
    decomposer = GoalDecomposer()
    plan = decomposer.build_plan([
        AgentGoal(type=GoalType.REGISTER_ACCOUNT,
                  params={"url": "https://x.test/signup"}),
        AgentGoal(type=GoalType.OPEN_REWARDS_PAGE),
        AgentGoal(type=GoalType.CLAIM_REWARD),
    ])
    # REGISTER_ACCOUNT decomposes to navigate + 2 customs; the two
    # atomic goals add 1 custom each → 5 sub-goals total.
    assert len(plan) == 5
    # The atomic goals should resolve to the engine's GOAL_SHAPES keys.
    ai_goals = [g.params.get("ai_goal") for g in plan if g.params.get("ai_goal")]
    assert "open_rewards_page" in ai_goals
    assert "claim_reward" in ai_goals
