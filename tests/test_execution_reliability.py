"""Tests for the execution-reliability upgrade.

Covers the four new modules introduced in this change:

  * :mod:`automation.agent.form_engine`
  * :mod:`automation.agent.obstruction`
  * :mod:`automation.agent.visual_locator`
  * :mod:`automation.agent.telemetry_retention`

Plus the popup_guard ``NOTIFICATION_REQUEST`` rule and the
ActionExecutor's obstruction integration.

These tests use no Playwright; everything is exercised either with
the offline ``PagePerception.capture_from_html`` adapter (for the
form engine) or with small fake page objects defined inline.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from automation.agent.form_engine import (
    FormEngine,
    KNOWN_FIELDS,
)
from automation.agent.form_filler import FillStrategy
from automation.agent.obstruction import (
    ObstructionDetector,
    ObstructionStatus,
)
from automation.agent.popup_guard import DEFAULT_RULES, PopupKind
from automation.agent.telemetry_retention import (
    AUTO_DELETE_PRESETS_MIN,
    SCREENSHOT_PRESETS,
    TelemetryRetention,
)
from automation.agent.visual_locator import (
    OCRWord,
    VisualLocator,
    _fuzzy_score,
    _group_phrases,
    _infer_role,
    _normalize,
)
from automation.ai.executor import ActionExecutor
from automation.ai.perception import PagePerception
from automation.ai.planner import ActionStep, ActionType


# ---------------------------------------------------------------- FormEngine
def test_form_engine_classifies_register_form():
    """A standard register form classifies email + password + confirm.

    The input has labels the heuristics already understand, so we
    expect classifications without falling back to the rule layer.
    """
    p = PagePerception()
    snap = p.capture_from_html(
        '<input name="email" placeholder="Email" />'
        '<input name="password" type="password" placeholder="Password" />'
        '<input name="password2" type="password" placeholder="Confirm password" />'
        '<button type="submit">Sign Up</button>',
        url="https://x.test/signup",
        title="Sign Up",
    )
    fe = FormEngine()
    fields = fe.classify(snap)
    by_name = {c.name for c in fields}
    assert "email" in by_name
    assert "password" in by_name
    assert "confirm_password" in by_name


def test_form_engine_otp_and_invitation_via_rules():
    """Long-tail rules catch OTP + invitation-code labels.

    The Heuristics layer doesn't have a group for these, but the
    classifier's regex rules do.
    """
    p = PagePerception()
    snap = p.capture_from_html(
        '<input name="otp" placeholder="6-digit verification code" />'
        '<input name="ref" placeholder="Invitation code" />',
        url="https://x.test/redeem",
        title="Redeem",
    )
    fe = FormEngine()
    by_name = {c.name: c for c in fe.classify(snap)}
    # OTP rule wins over username/etc.
    assert "otp" in by_name
    assert by_name["otp"].matched_on.startswith("rule:otp")
    # Invitation code matches via the dedicated rule
    assert "invitation_code" in by_name
    assert by_name["invitation_code"].matched_on.startswith("rule:invitation_code")


def test_form_engine_single_password_fallback():
    """If only one password input is on the page, classify it as password.

    Some sites label their only password field "Confirm password" — the
    fallback collapses that case so we don't refuse to fill the form.
    """
    p = PagePerception()
    snap = p.capture_from_html(
        '<input name="email" placeholder="Email" />'
        '<input name="pw" type="password" placeholder="Confirm password" />',
        url="https://x.test/login", title="Login",
    )
    fe = FormEngine()
    by_name = {c.name for c in fe.classify(snap)}
    assert "password" in by_name
    assert "confirm_password" not in by_name


def test_form_engine_custom_field_via_caller_keys():
    """Caller-supplied custom keys map onto matching DOM names."""
    p = PagePerception()
    snap = p.capture_from_html(
        '<input name="company_code" placeholder="Company code" />'
        '<input name="email" placeholder="Email" />',
        url="https://x.test/portal", title="Portal",
    )
    fe = FormEngine()
    fields = fe.classify(snap, custom_field_keys=["company_code"])
    by_sel = {c.element.name: c for c in fields}
    assert by_sel["company_code"].name == "custom"
    assert by_sel["company_code"].custom_key == "company_code"


def test_form_engine_known_fields_stable():
    """KNOWN_FIELDS is the canonical bucket list used by clients.

    Locking the tuple shape so client code can rely on it.
    """
    expected = {
        "email", "username", "phone", "password", "confirm_password",
        "otp", "invitation_code", "search",
    }
    assert set(KNOWN_FIELDS) == expected


async def test_form_engine_fill_form_full_pipeline():
    """End-to-end: classify → map → fill → verify, against a fake page."""
    p = PagePerception()
    snap = p.capture_from_html(
        '<input name="email" placeholder="Email" />'
        '<input name="password" type="password" placeholder="Password" />',
        url="https://x.test/login", title="Login",
    )
    fe = FormEngine()
    page = _FakeFillPage()
    inputs = {"email": "user@example.com", "password": "secret123"}
    report = await fe.fill_form(page, snap, inputs)
    assert report.success
    # Both fields filled and verified
    by_name = {f.field.name: f for f in report.filled if f.fill is not None}
    assert "email" in by_name and by_name["email"].fill.success
    assert "password" in by_name and by_name["password"].fill.success
    # Standard strategy picks up first when fill() works on first try
    assert by_name["email"].fill.strategy_used is FillStrategy.STANDARD


async def test_form_engine_fill_form_skips_when_no_value():
    """Fields without a matching value are reported as skipped, not failed."""
    p = PagePerception()
    snap = p.capture_from_html(
        '<input name="email" placeholder="Email" />'
        '<input name="password" type="password" placeholder="Password" />'
        '<input name="otp" placeholder="OTP" />',
        url="https://x.test/login", title="Login",
    )
    fe = FormEngine()
    page = _FakeFillPage()
    # No "otp" key — engine should fill email + password and skip otp.
    report = await fe.fill_form(page, snap, {"email": "u@e.com", "password": "p"})
    assert report.success  # success measured against fields with values
    skipped = [f for f in report.filled if f.skipped_reason]
    assert any(f.field.name == "otp" for f in skipped)


# ---------------------------------------------------------------- ObstructionDetector
class _FakeProbePage:
    """Page-like object that returns canned probe payloads.

    Tracks evaluate / click / mouse calls so individual tests can
    assert exactly which strategies the detector tried.
    """

    def __init__(self, payloads: list[dict], *, repeat_last: bool = False) -> None:
        # ``payloads`` is consumed in order — one per probe call.
        # When ``repeat_last`` is True, the last payload sticks (useful
        # for "always blocked" tests). When False (default), an empty
        # queue yields a synthetic OK payload — useful for happy-path
        # tests where the unblock loop is expected to converge.
        self._payloads = list(payloads)
        self._repeat_last = repeat_last
        self.evaluate_calls: list[tuple[str, tuple]] = []
        self.click_calls: list[str] = []

    @property
    def url(self) -> str:
        return "https://x.test"

    async def evaluate(self, expr: str, *args):
        self.evaluate_calls.append((expr, args))
        # The probe JS is very long; identify it by a fragment.
        if "elementFromPoint" in expr:
            if not self._payloads:
                if self._repeat_last:
                    # Detector saw the same blocker every probe — common
                    # when an overlay refuses to dismiss.
                    return self.evaluate_calls[-2][0] if False else (
                        # We re-emit a generic blocked payload.
                        {
                            "status": "blocked", "visible": True,
                            "inViewport": True, "clickable": True,
                            "bbox": [0, 0, 100, 30],
                            "obstruction": {
                                "tag": "div", "id": "overlay",
                                "classes": "modal", "role": "",
                                "text": "X", "zIndex": "9999",
                                "x": 0, "y": 0, "w": 100, "h": 100,
                            },
                        }
                    )
                # Default: ok status. Emulates a clean page after dismiss.
                return {
                    "status": "ok", "visible": True, "inViewport": True,
                    "clickable": True, "bbox": [0, 0, 100, 30],
                    "obstruction": None,
                }
            payload = self._payloads.pop(0)
            return payload
        # Scroll / hide / generic eval — just acknowledge.
        return True

    async def click(self, selector: str, *, timeout: int = 0):
        self.click_calls.append(selector)


async def test_obstruction_probe_ok():
    """A clean page yields an OK probe result."""
    page = _FakeProbePage([
        {
            "status": "ok", "visible": True, "inViewport": True,
            "clickable": True, "bbox": [10, 20, 100, 30],
            "obstruction": None,
        },
    ])
    det = ObstructionDetector()
    res = await det.probe(page, "#btn")
    assert res.ok
    assert res.status is ObstructionStatus.OK
    assert res.bbox == (10.0, 20.0, 100.0, 30.0)


async def test_obstruction_probe_blocked_reports_blocker():
    """Blocked status carries the obstruction's tag/id."""
    page = _FakeProbePage([
        {
            "status": "blocked", "visible": True, "inViewport": True,
            "clickable": True, "bbox": [0, 0, 100, 30],
            "obstruction": {
                "tag": "div", "id": "cookie-banner", "classes": "banner",
                "role": "", "text": "Accept all cookies", "zIndex": "9999",
                "x": 0, "y": 0, "w": 1280, "h": 80,
            },
        },
    ])
    det = ObstructionDetector()
    res = await det.probe(page, "#btn")
    assert res.status is ObstructionStatus.BLOCKED
    assert res.obstruction is not None
    assert res.obstruction.id == "cookie-banner"
    assert "cookie-banner" in res.obstruction.description


async def test_obstruction_safe_click_unblocks_offscreen_then_clicks():
    """Off-screen → scroll → ok → click."""
    page = _FakeProbePage([
        # initial probe: offscreen
        {
            "status": "offscreen", "visible": True, "inViewport": False,
            "clickable": True, "bbox": [0, 2000, 100, 30],
            "obstruction": None,
        },
        # after scroll: ok
        {
            "status": "ok", "visible": True, "inViewport": True,
            "clickable": True, "bbox": [10, 200, 100, 30],
            "obstruction": None,
        },
    ])
    det = ObstructionDetector()
    clicked, probe, unblock = await det.safe_click(page, "#btn")
    assert clicked
    assert probe.ok
    assert unblock is not None and unblock.cleared
    # First strategy was scroll
    strategies = [a.strategy for a in unblock.attempts]
    assert "scroll" in strategies
    assert page.click_calls == ["#btn"]


async def test_obstruction_safe_click_gives_up_on_unclickable():
    """Disabled buttons fail fast — no scrolling / hiding helps."""
    page = _FakeProbePage([
        {
            "status": "unclickable", "visible": True, "inViewport": True,
            "clickable": False, "bbox": [0, 0, 100, 30],
            "obstruction": None,
        },
    ])
    det = ObstructionDetector()
    clicked, probe, unblock = await det.safe_click(page, "#btn")
    assert not clicked
    assert probe.status is ObstructionStatus.UNCLICKABLE
    assert page.click_calls == []


# ---------------------------------------------------------------- VisualLocator
def test_visual_locator_default_is_unavailable():
    """No backend → locator reports unavailable, never blows up."""
    vl = VisualLocator()
    assert vl.available is False


def test_visual_normalize_and_fuzzy():
    """Normalization strips punctuation; fuzzy tolerates OCR noise."""
    assert _normalize("Sign In!") == "sign in"
    assert _normalize(" Click Here  ") == "click here"
    # Exact match
    assert _fuzzy_score("sign in", "sign in") == 1.0
    # Substring match scores high
    assert _fuzzy_score("please sign in", "sign in") >= 0.85
    # OCR-ish similarity (S1gn vs Sign) still scores OK via difflib
    assert _fuzzy_score("s1gn in", "sign in") > 0.7


def test_visual_group_phrases_joins_adjacent_words():
    """Adjacent words on the same line group into one phrase."""
    words = [
        OCRWord("Continue", x=10, y=100, w=80, h=20, confidence=0.95),
        OCRWord("with",     x=95, y=100, w=40, h=20, confidence=0.92),
        OCRWord("Google",   x=140, y=100, w=70, h=20, confidence=0.93),
        # Different line
        OCRWord("Cancel",   x=10, y=200, w=60, h=20, confidence=0.9),
    ]
    phrases = _group_phrases(words)
    texts = [p["text"] for p in phrases]
    assert "Continue with Google" in texts
    assert "Cancel" in texts


def test_visual_infer_role_button_vs_textbox():
    """Aspect-ratio role inference correctly separates buttons from textboxes."""
    # 100x40 → aspect 2.5 → button
    assert _infer_role((0, 0, 100, 40)) == "button"
    # 600x36 → aspect ~16 → textbox
    assert _infer_role((0, 0, 600, 36)) == "textbox"
    # Square icon → no signal
    assert _infer_role((0, 0, 24, 24)) == ""


async def test_visual_locator_no_backend_locate_returns_unavailable():
    """``locate`` with no backend returns a clean unavailable result."""
    vl = VisualLocator()
    page = _FakeProbePage([])  # any page-like
    res = await vl.locate(page, "Sign in")
    assert res.available is False
    assert res.matches == []
    assert any("not available" in n for n in res.notes)


# ---------------------------------------------------------------- TelemetryRetention
def _seed_fake_run(runs_root: Path, run_id: str, *,
                   accounts: list[str] = ("acc1",),
                   status: str = "completed",
                   screenshots_per_account: int = 5,
                   reasoning_lines: int = 20) -> Path:
    """Create a realistic fake run directory with screenshots + reasoning + events."""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # status.json with the requested terminal state
    (run_dir / "status.json").write_text(json.dumps({
        "run_id": run_id, "status": status, "instruction": "test",
        "goals": [], "accounts": list(accounts),
        "created_at": time.time(), "started_at": time.time(),
        "completed_at": time.time() if status != "running" else None,
    }))
    (run_dir / "plan.json").write_text(json.dumps({"goals": []}))
    # events.jsonl with a few entries — half "old" and half "fresh"
    now = time.time()
    events = []
    for i in range(6):
        ts = now - (3600 if i < 3 else 1)  # first 3 are 1h old
        events.append(json.dumps({"ts": ts, "type": "agent.step", "i": i}))
    (run_dir / "events.jsonl").write_text("\n".join(events) + "\n")
    # per-account
    for acc in accounts:
        acc_dir = run_dir / acc
        (acc_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        (acc_dir / "logs").mkdir(parents=True, exist_ok=True)
        for i in range(screenshots_per_account):
            shot = acc_dir / "screenshots" / f"shot_{i:03d}.png"
            shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
            # Stagger mtimes so newest-first ordering is deterministic.
            t = time.time() - (screenshots_per_account - i)
            import os
            os.utime(shot, (t, t))
        # reasoning.jsonl
        lines = "\n".join(json.dumps({"i": i, "msg": f"step {i}"}) for i in range(reasoning_lines))
        (acc_dir / "reasoning.jsonl").write_text(lines + "\n")
        # one log file
        (acc_dir / "logs" / "activity.log").write_text("log " * 10)
    return run_dir


def test_retention_settings_round_trip(tmp_path: Path):
    """update_settings persists then reloads the same values."""
    rr = TelemetryRetention(
        runs_root=tmp_path / "runs",
        settings_path=tmp_path / "settings.json",
    )
    s = rr.update_settings(screenshot_limit=50, auto_delete_minutes=30)
    assert s.screenshot_limit == 50
    assert s.auto_delete_minutes == 30

    # Fresh manager, same path → settings load from disk
    rr2 = TelemetryRetention(
        runs_root=tmp_path / "runs",
        settings_path=tmp_path / "settings.json",
    )
    assert rr2.settings.screenshot_limit == 50
    assert rr2.settings.auto_delete_minutes == 30


def test_retention_clear_screenshots_keep_last(tmp_path: Path):
    """clear_screenshots(keep_last=2) keeps newest 2 per account."""
    runs_root = tmp_path / "runs"
    _seed_fake_run(runs_root, "run_a", screenshots_per_account=5)
    _seed_fake_run(runs_root, "run_b", screenshots_per_account=4)
    rr = TelemetryRetention(runs_root=runs_root, settings_path=tmp_path / "s.json")
    res = rr.clear_screenshots(keep_last=2)
    assert res.ok
    # Each run had one account; expected removed = (5-2) + (4-2) = 5
    assert res.files_removed == 5
    # Verify what's left
    for run_id, expected in (("run_a", 2), ("run_b", 2)):
        left = sorted((runs_root / run_id / "acc1" / "screenshots").iterdir())
        assert len(left) == expected


def test_retention_clear_reasoning_trims_to_last_n(tmp_path: Path):
    """clear_reasoning(keep_last=5) keeps the last 5 lines of reasoning.jsonl."""
    runs_root = tmp_path / "runs"
    _seed_fake_run(runs_root, "run_a", reasoning_lines=30)
    rr = TelemetryRetention(runs_root=runs_root, settings_path=tmp_path / "s.json")
    res = rr.clear_reasoning(keep_last=5)
    assert res.ok
    path = runs_root / "run_a" / "acc1" / "reasoning.jsonl"
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 5
    # last 5 are the highest-indexed entries
    indices = [json.loads(l)["i"] for l in lines]
    assert indices == [25, 26, 27, 28, 29]


def test_retention_clear_runs_protects_active(tmp_path: Path):
    """A run with status='running' is *not* deleted by clear_runs."""
    runs_root = tmp_path / "runs"
    _seed_fake_run(runs_root, "run_done", status="completed")
    _seed_fake_run(runs_root, "run_live", status="running")
    rr = TelemetryRetention(runs_root=runs_root, settings_path=tmp_path / "s.json")
    res = rr.clear_runs(keep_last=0, only_completed=True)
    assert res.ok
    assert (runs_root / "run_live").exists()
    assert not (runs_root / "run_done").exists()


def test_retention_clear_runs_keep_last(tmp_path: Path):
    """keep_last=1 retains the single most-recent completed run."""
    runs_root = tmp_path / "runs"
    _seed_fake_run(runs_root, "run_oldest", status="completed")
    time.sleep(0.01)
    _seed_fake_run(runs_root, "run_middle", status="completed")
    time.sleep(0.01)
    _seed_fake_run(runs_root, "run_newest", status="completed")
    rr = TelemetryRetention(runs_root=runs_root, settings_path=tmp_path / "s.json")
    res = rr.clear_runs(keep_last=1)
    assert res.ok
    remaining = {p.name for p in runs_root.iterdir()}
    assert "run_newest" in remaining
    assert "run_oldest" not in remaining
    assert "run_middle" not in remaining


def test_retention_auto_delete_old_events(tmp_path: Path):
    """auto-delete drops events older than the threshold; preserves fresh ones."""
    runs_root = tmp_path / "runs"
    _seed_fake_run(runs_root, "run_a", status="completed")
    rr = TelemetryRetention(runs_root=runs_root, settings_path=tmp_path / "s.json")
    rr.update_settings(auto_delete_minutes=30)  # 30-min threshold
    res = rr.apply_settings()
    assert res.ok
    # Events file should now contain only the 3 fresh entries.
    events = (runs_root / "run_a" / "events.jsonl").read_text().splitlines()
    events = [e for e in events if e.strip()]
    assert len(events) == 3


def test_retention_clear_chat_composite(tmp_path: Path):
    """clear_chat removes screenshots + reasoning + logs but preserves status.json."""
    runs_root = tmp_path / "runs"
    _seed_fake_run(runs_root, "run_a", screenshots_per_account=3, reasoning_lines=5)
    rr = TelemetryRetention(runs_root=runs_root, settings_path=tmp_path / "s.json")
    res = rr.clear_chat(run_id="run_a")
    assert res.ok
    assert res.files_removed >= 3  # at least the screenshots
    # status.json must survive — operators still need to see the run's outcome.
    assert (runs_root / "run_a" / "status.json").exists()
    # Reasoning is truncated, not removed.
    reasoning = runs_root / "run_a" / "acc1" / "reasoning.jsonl"
    assert reasoning.exists() and reasoning.read_text().strip() == ""
    # Screenshots dir is empty (or all files gone)
    shots = list((runs_root / "run_a" / "acc1" / "screenshots").iterdir())
    assert shots == []


def test_retention_usage_summary(tmp_path: Path):
    """usage_summary aggregates counts + bytes."""
    runs_root = tmp_path / "runs"
    _seed_fake_run(runs_root, "run_a", screenshots_per_account=3, reasoning_lines=4)
    _seed_fake_run(runs_root, "run_b", screenshots_per_account=2, reasoning_lines=2)
    rr = TelemetryRetention(runs_root=runs_root, settings_path=tmp_path / "s.json")
    summary = rr.usage_summary()
    assert summary["runs"] == 2
    assert summary["screenshots"] == 5
    assert summary["reasoning_lines"] == 6
    assert summary["total_bytes"] > 0
    assert "settings" in summary


def test_retention_presets_match_spec():
    """Presets in code match the values shown to operators in the UI brief.

    Locks the contract so a future refactor doesn't silently drift.
    """
    assert SCREENSHOT_PRESETS == (10, 50, 100, 0)
    assert AUTO_DELETE_PRESETS_MIN == (5, 30, 60, 0)


# ---------------------------------------------------------------- popup_guard
def test_popup_guard_notification_request_rule_present():
    """The popup catalog now includes the in-page push prompt rule."""
    kinds = {rule.kind for rule in DEFAULT_RULES}
    assert PopupKind.NOTIFICATION_REQUEST in kinds
    rule = next(r for r in DEFAULT_RULES if r.kind is PopupKind.NOTIFICATION_REQUEST)
    # Sanity: at least one selector targets the "Don't allow" close path
    sels = " ".join(rule.close_selectors).lower()
    assert "don" in sels or "block" in sels or "not now" in sels


# ---------------------------------------------------------------- ActionExecutor
async def test_executor_click_uses_obstruction_detector():
    """When an obstruction detector is configured, CLICK goes through safe_click.

    Verifies the diagnostics land on step.metadata so reasoning panels
    can show the probe result without re-running it.
    """
    page = _FakeProbePage([
        {
            "status": "ok", "visible": True, "inViewport": True,
            "clickable": True, "bbox": [10, 20, 100, 30],
            "obstruction": None,
        },
    ])
    det = ObstructionDetector()
    ex = ActionExecutor(obstruction_detector=det)
    step = ActionStep(
        action=ActionType.CLICK, selector="#submit",
        intent="submit",
    )
    result = await ex._run_step(page, step)
    assert result.success
    assert page.click_calls == ["#submit"]
    assert step.metadata is not None
    assert step.metadata.get("obstruction_probe", {}).get("status") == "ok"


async def test_executor_click_fails_when_blocked_and_unblock_fails():
    """A persistently-blocked target surfaces as a step error, not a hang."""
    blocked_payload = {
        "status": "blocked", "visible": True, "inViewport": True,
        "clickable": True, "bbox": [10, 20, 100, 30],
        "obstruction": {
            "tag": "div", "id": "overlay", "classes": "modal",
            "role": "", "text": "X", "zIndex": "9999",
            "x": 0, "y": 0, "w": 100, "h": 100,
        },
    }
    # ``repeat_last=True`` makes every probe — even after the unblock
    # loop has exhausted its initial queue — return the same blocked
    # state. That's what a stubborn site does in practice.
    page = _FakeProbePage([blocked_payload], repeat_last=True)
    det = ObstructionDetector(hide_blockers=True)
    ex = ActionExecutor(obstruction_detector=det)
    step = ActionStep(action=ActionType.CLICK, selector="#submit")
    result = await ex._run_step(page, step)
    assert not result.success
    assert "safe_click refused" in (result.error or "")
    # The click was never issued.
    assert page.click_calls == []


async def test_executor_click_without_detector_uses_plain_click():
    """No detector configured → behaviour is the legacy ``page.click``."""
    page = _FakeProbePage([])  # no probe payloads needed
    ex = ActionExecutor()  # no obstruction_detector
    step = ActionStep(action=ActionType.CLICK, selector="#go")
    result = await ex._run_step(page, step)
    assert result.success
    assert page.click_calls == ["#go"]
    # No probe metadata when the detector isn't wired.
    assert not (step.metadata or {}).get("obstruction_probe")


# ---------------------------------------------------------------- helpers
class _FakeFillPage:
    """Page-like object for FormEngine tests.

    Tracks fill calls + serves them back on read so the form filler's
    read-back verification succeeds. ``page.fill`` is what the
    STANDARD strategy calls; we never reach the other strategies in
    these tests because STANDARD always succeeds.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fill_calls: list[tuple[str, str]] = []

    @property
    def url(self) -> str:
        return "https://x.test"

    async def fill(self, selector: str, value: str, *, timeout: int = 0):
        self.values[selector] = value
        self.fill_calls.append((selector, value))

    async def evaluate(self, expr: str, *args):
        # The form filler reads back via:
        #   "(document.querySelector(<sel>) || {}).value || ''"
        # We answer by finding which stored selector appears in the
        # JS expression. Selectors like ``input[name="email"]`` get
        # their inner double-quotes escaped by ``_js_str`` — the
        # naïve substring check has to look for the *escaped* form,
        # so we normalize both sides.
        norm_expr = expr.replace('\\"', '"').replace("\\'", "'")
        for sel, val in self.values.items():
            if sel in norm_expr:
                return val
        # Generic checks (errors, blur, etc.) → falsy / no-op
        if "error" in expr.lower() or "alert" in expr.lower():
            return False
        if "blur" in expr.lower():
            return None
        return None

    async def click(self, selector: str, *, timeout: int = 0):
        # Used by the CLEAR_TYPE strategy; never reached in these tests.
        pass

    async def type(self, selector: str, value: str, *, delay: int = 0):
        self.values[selector] = value

    @property
    def keyboard(self):
        return _FakeKeyboard()


class _FakeKeyboard:
    """No-op keyboard used by clipboard/clear-type strategies."""

    async def press(self, key: str) -> None:  # noqa: ARG002
        pass
