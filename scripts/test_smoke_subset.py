"""Run a minimal subset of tests/test_smoke.py without pytest.

Ensures the modifications to accounts/manager.py (added ``add_accounts``)
and the new agent layer have not regressed the existing public surface.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = 0
FAIL = 0


def run(name, coro_or_fn):
    global PASS, FAIL
    try:
        if asyncio.iscoroutine(coro_or_fn):
            asyncio.get_event_loop().run_until_complete(coro_or_fn)
        elif asyncio.iscoroutinefunction(coro_or_fn):
            asyncio.get_event_loop().run_until_complete(coro_or_fn())
        else:
            coro_or_fn()
        PASS += 1
        print(f"  PASS  {name}")
    except Exception as exc:
        FAIL += 1
        import traceback
        print(f"  FAIL  {name}: {exc!r}")
        traceback.print_exc()


def t1_existing_validation():
    from automation.accounts.manager import AccountManager

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "accounts.json"
        src.write_text(json.dumps({"accounts": [
            {"number": "9999999999", "password": "p1"},
            {"number": "9999999999", "password": "p2"},  # duplicate
            {"number": "9999999999", "password": ""},  # empty password
            {"password": "p3"},  # no identifier
            "not-an-object",
            {"number": "abc", "password": "p4"},  # invalid format
        ]}))
        am = AccountManager(
            source_file=src, state_file=Path(tmp) / "state.sqlite",
            max_attempts=2,
        )
        n = am.load()
        assert n == 1
        rejected = am.rejected()
        assert len(rejected) == 5
        am.close()


async def t2_existing_concurrent_claims():
    from automation.accounts.manager import AccountManager

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "accounts.json"
        src.write_text(json.dumps({"accounts": [
            {"number": "1111111111", "password": "p"},
            {"number": "2222222222", "password": "p"},
            {"number": "3333333333", "password": "p"},
        ]}))
        am = AccountManager(
            source_file=src, state_file=Path(tmp) / "state.sqlite",
            max_attempts=3,
        )
        am.load()
        a, b, c, d = await asyncio.gather(
            am.claim_next("A"),
            am.claim_next("B"),
            am.claim_next("C"),
            am.claim_next("D"),
        )
        claimed = [x for x in (a, b, c, d) if x]
        assert len(claimed) == 3
        ids = {x.id for x in claimed}
        assert len(ids) == 3
        am.close()


async def t3_existing_reload_preserves_completed():
    from automation.accounts.manager import AccountManager, AccountStatus

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "accounts.json"
        src.write_text(json.dumps({"accounts": [
            {"number": "1111111111", "password": "p1"},
            {"number": "2222222222", "password": "p2"},
        ]}))
        am = AccountManager(
            source_file=src, state_file=Path(tmp) / "state.sqlite",
            max_attempts=2,
        )
        am.load()
        a = await am.claim_next("w")
        await am.mark_completed(a.id, workflow="login")
        assert am.get(a.id).status == AccountStatus.COMPLETED
        # rewrite source with same accounts + a third
        src.write_text(json.dumps({"accounts": [
            {"number": "1111111111", "password": "CHANGED"},
            {"number": "2222222222", "password": "p2"},
            {"number": "3333333333", "password": "p3"},
        ]}))
        n = await am.reload()
        assert n == 3
        assert am.get(a.id).status == AccountStatus.COMPLETED
        assert am.get(a.id).password == "CHANGED"
        am.close()


def t4_browser_overrides_unaffected():
    """Ensures my changes did not regress browser overrides parsing."""
    from automation.browser.manager import BrowserOverrides

    ov = BrowserOverrides.from_metadata({
        "proxy": "http://user:secret@proxy.example.com:8080",
        "user_agent": "UA/1.0",
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "profile_id": "device-X",
    })
    assert ov.proxy == {
        "server": "http://proxy.example.com:8080",
        "username": "user",
        "password": "secret",
    }
    assert ov.viewport == (1366, 768)
    assert ov.profile_id == "device-X"


def t5_existing_intent_matcher():
    from automation.ai.intents import IntentMatcher

    matcher = IntentMatcher()
    sign_in = matcher.match(text="Sign In", role="button")
    assert any(m.intent == "login" for m in sign_in)
    pwd = matcher.match(placeholder="Password", role="textbox")
    assert any(m.intent == "password_field" for m in pwd)


def t6_existing_perception_offline():
    from automation.ai.perception import PagePerception

    p = PagePerception()
    snap = p.capture_from_html(
        '<button>Sign In</button>'
        '<input type="password" name="password" placeholder="Password">',
        url="https://x.test/login", title="Login",
    )
    assert snap.signature
    assert snap.by_intent("login")
    assert snap.by_intent("password_field")


print("=== regression check on existing tests ===")
run("validation rejects bad rows", t1_existing_validation)
run("concurrent claims have no duplicate", t2_existing_concurrent_claims())
run("reload preserves completed", t3_existing_reload_preserves_completed())
run("browser overrides unaffected", t4_browser_overrides_unaffected)
run("intent matcher unaffected", t5_existing_intent_matcher)
run("perception offline unaffected", t6_existing_perception_offline)

print(f"\n=== summary: {PASS} passed, {FAIL} failed ===")
sys.exit(0 if FAIL == 0 else 1)
