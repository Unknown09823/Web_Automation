"""Smoke tests for the Telegram-first refactor.

Runnable without pytest:

    PYTHONPATH=src python3.12 scripts/test_telegram_first.py

Covers:
  * Settings: env+master merging, secret masking, startup_report,
    legacy_config_dict, set/persist on master_config.json
  * TemplateStore: validate_name, save/get/delete, record_use
  * SiteMemory: domain_from_url, remember_success/failure, summary
  * LLM factory: returns None when no key; LLMResponse.parsed_json on
    fenced JSON output
  * Telegram command-center helpers: _format_plan_preview, _parse_batch_spec,
    _coerce_value, _progress_bar, _COMMAND_TABLE wiring
  * NLPlanner: regex fallback path is unchanged
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
from automation.agent.nl_planner import NLPlanner
from automation.agent.site_memory import SiteMemory, domain_from_url
from automation.agent.templates import Template, TemplateStore
from automation.ai.llm import build_llm_from_settings
from automation.ai.llm.base import LLMResponse
from automation.ai.llm.groq import GroqClient
from automation.config.settings import Settings
from automation.controllers.telegram_command_center import (
    _COMMAND_TABLE,
    _coerce_value,
    _format_plan_preview,
    _parse_batch_spec,
    _progress_bar,
    CommandCenter,
)


_passes: list[str] = []
_fails: list[tuple[str, str]] = []


def expect(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
        _passes.append(label)
    else:
        print(f"  FAIL  {label}{(': ' + detail) if detail else ''}")
        _fails.append((label, detail))


async def aexpect(label: str, coro) -> None:
    try:
        await coro
        print(f"  PASS  {label}")
        _passes.append(label)
    except AssertionError as exc:
        print(f"  FAIL  {label}: {exc}")
        _fails.append((label, str(exc)))


# =================================================================== Settings
def test_settings() -> None:
    print("\n=== Settings ===")
    with tempfile.TemporaryDirectory() as d:
        master = Path(d) / "master.json"
        master.write_text(json.dumps({
            "execution": {"max_parallel_workers": 1},
            "browser": {"headless": False},
            "ai": {"provider": "groq", "default_planner_model": "fallback"},
        }))
        env = Path(d) / "env"
        env.write_text(
            "GROQ_API_KEY=gsk_test123\n"
            "TELEGRAM_BOT_TOKEN=12345:ABC\n"
            "TELEGRAM_ALLOWED_CHAT_IDS=1,2, 3\n"
            "MAX_PARALLEL_WORKERS=7\n"
            "DEFAULT_PLANNER_MODEL=qwen/qwen3-32b\n"
            "HEADLESS=true\n"
        )
        # ensure no existing AUTOMATION_API_TOKEN bleed-through
        for k in (
            "GROQ_API_KEY", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_CHAT_IDS", "MAX_PARALLEL_WORKERS",
            "DEFAULT_PLANNER_MODEL", "HEADLESS",
        ):
            os.environ.pop(k, None)
        s = Settings(master, env)

        expect("env-alias resolves GROQ_API_KEY",
               s.get_secret("GROQ_API_KEY") == "gsk_test123")
        expect("env-alias resolves dotted equivalent",
               s.get_secret("ai.groq_api_key") == "gsk_test123")
        expect("env overrides master for max_parallel_workers",
               s.get("MAX_PARALLEL_WORKERS") == 7,
               detail=str(s.get("MAX_PARALLEL_WORKERS")))
        expect("dotted access matches env alias",
               s.get("execution.max_parallel_workers") == 7)
        expect("HEADLESS bool coerced",
               s.get("browser.headless") is True)
        expect("env overrides master for planner model",
               s.get("DEFAULT_PLANNER_MODEL") == "qwen/qwen3-32b")
        expect("allowed chat ids parsed",
               s.telegram_allowed_chat_ids() == [1, 2, 3])
        expect("default chat id picks first allowed",
               s.default_chat_id() == 1)

        # Masking
        public = s.public_dict()
        masked_token = public.get("telegram", {}).get("token", "?")
        expect("public_dict masks telegram token",
               masked_token == "****")

        # legacy adapter
        legacy = s.legacy_config_dict()
        expect("legacy adapter exposes playwright.headless",
               legacy.get("playwright", {}).get("headless") is True)
        expect("legacy adapter exposes accounts_max_attempts",
               isinstance(legacy.get("accounts_max_attempts"), int))

        # set + persist refuses secrets
        try:
            s.set("GROQ_API_KEY", "leaked", persist=False)
            expect("refuses to set secret keys", False, "no error raised")
        except PermissionError:
            expect("refuses to set secret keys", True)

        # set + persist non-secret writes back
        s.set("execution.max_parallel_workers", 9, persist=True)
        expect("set+persist writes master_config.json",
               json.loads(master.read_text())["execution"]["max_parallel_workers"] == 9)

        # startup report — Telegram and Groq present from .env
        rows = {name: (ok, detail) for name, ok, detail in s.startup_report()}
        expect("startup report: Telegram OK", rows["Telegram"][0] is True)
        expect("startup report: Groq OK",     rows["Groq"][0] is True)


# =============================================================== TemplateStore
def test_templates() -> None:
    print("\n=== TemplateStore ===")
    with tempfile.TemporaryDirectory() as d:
        store = TemplateStore(d)
        try:
            store.validate_name("bad name")
            expect("rejects invalid template name", False)
        except ValueError:
            expect("rejects invalid template name", True)

        t = Template(
            name="DemoFlow", instruction="Go to site, login, download report",
            goals=[
                {"type": "navigate", "description": "Open site",
                 "params": {"url": "https://example.com"}},
                {"type": "login", "description": "Login", "params": {}},
                {"type": "download_file", "description": "Download report",
                 "params": {}},
            ],
            target_url="https://example.com",
        )
        store.save(t)

        items = store.list()
        expect("list contains saved template",
               len(items) == 1 and items[0].name == "DemoFlow")
        got = store.get("DemoFlow")
        expect("get returns saved template",
               got is not None and len(got.goals) == 3)

        store.record_use("DemoFlow")
        expect("record_use increments uses",
               store.get("DemoFlow").uses == 1)
        expect("delete removes template", store.delete("DemoFlow") is True)
        expect("delete returns False for missing",
               store.delete("DemoFlow") is False)


# ================================================================== SiteMemory
def test_site_memory() -> None:
    print("\n=== SiteMemory ===")
    with tempfile.TemporaryDirectory() as d:
        m = SiteMemory(d)

        expect("domain_from_url normalizes www.+upper",
               domain_from_url("https://www.Example.COM/login") == "example.com")
        expect("domain_from_url handles raw hostname",
               domain_from_url("foo.bar.com:443") == "foo.bar.com")
        expect("domain_from_url empty -> empty",
               domain_from_url("") == "")

        rec = m.remember_success(
            "https://example.com/signup",
            workflow="register_account",
            duration_seconds=15.5,
            login_selector="#email",
            submit_selector="button[type=submit]",
            dashboard_url="https://example.com/dashboard",
        )
        expect("remember_success increments successes",
               rec.successes == 1)
        expect("remember_success records workflow stats",
               rec.workflows["register_account"]["runs"] == 1
               and rec.workflows["register_account"]["ok"] == 1)
        expect("remember_success captures login selector",
               any(s["selector"] == "#email" for s in rec.logins))
        expect("remember_success captures dashboard URL",
               "https://example.com/dashboard" in rec.dashboard_urls)

        # second success: same selector should dedup
        m.remember_success(
            "https://example.com/signup",
            workflow="register_account",
            duration_seconds=12.0,
            login_selector="#email",
        )
        rec2 = m.get("https://example.com/x")
        expect("login selectors dedup across calls",
               sum(1 for s in rec2.logins if s["selector"] == "#email") == 1)

        # failure path
        m.remember_failure(
            "https://example.com/signup",
            workflow="register_account",
            duration_seconds=8.0,
            note="captcha not solved",
        )
        rec3 = m.get("https://example.com")
        expect("remember_failure increments failures",
               rec3.failures == 1)
        expect("confidence reflects ratio",
               0.0 < rec3.confidence < 1.0)

        # summary
        s = m.summary()
        expect("summary aggregates totals",
               s["sites"] == 1 and s["total_successes"] == 2
               and s["total_failures"] == 1)


# ==================================================================== LLM
def test_llm_factory() -> None:
    print("\n=== LLM factory ===")
    with tempfile.TemporaryDirectory() as d:
        master = Path(d) / "m.json"
        master.write_text(json.dumps({"ai": {"provider": "groq"}}))
        env = Path(d) / "env"
        env.write_text("")  # nothing
        for k in (
            "GROQ_API_KEY", "OPENROUTER_API_KEY",
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        ):
            os.environ.pop(k, None)
        s = Settings(master, env)
        expect("factory returns None when no keys configured",
               build_llm_from_settings(s) is None)

        # With a key, factory builds a GroqClient instance
        env.write_text("GROQ_API_KEY=gsk_test123\n")
        s2 = Settings(master, env)
        client = build_llm_from_settings(s2)
        expect("factory builds GroqClient when GROQ_API_KEY is set",
               isinstance(client, GroqClient))
        expect("client default_model is qwen3-32b family",
               client and "qwen" in client.default_model.lower())


def test_llm_response_parsing() -> None:
    print("\n=== LLMResponse JSON parsing ===")
    # Pure JSON
    r = LLMResponse(text='{"a": 1}')
    expect("plain JSON parses", r.parsed_json() == {"a": 1})

    # Fenced
    r = LLMResponse(text='```json\n{"a": 2}\n```')
    expect("```json fence parses", r.parsed_json() == {"a": 2})

    # JSON with prose around it
    r = LLMResponse(text='Sure, here is the plan:\n{"a": 3}\nLet me know.')
    expect("inline JSON within prose parses",
           r.parsed_json() == {"a": 3})

    # Unparseable
    r = LLMResponse(text="totally not JSON")
    expect("non-JSON returns None", r.parsed_json() is None)


# ========================================================== command center
def test_command_center_helpers() -> None:
    print("\n=== Command center helpers ===")
    plan = asyncio.run(
        NLPlanner().parse(
            "Create 5 accounts on https://example.com password Test@123",
        ),
    )
    preview = _format_plan_preview(plan)
    expect("preview includes URL", "https://example.com" in preview)
    expect("preview masks password",
           "Test@123" not in preview and "****" in preview)
    expect("preview includes accounts count", "*Accounts:* 5" in preview)

    # Batch spec parser
    spec = _parse_batch_spec(
        "accounts: 100\nparallel: 5\npassword: Test@123\n"
        "target: https://example.com\ntask:\nRegister\nLogin\nComplete profile",
    )
    expect("batch parses accounts as int",
           spec["accounts"] == 100 and spec["parallel"] == 5)
    expect("batch joins task lines",
           "Register" in spec["task"] and "Complete profile" in spec["task"])

    # Bad batch — empty body should raise
    try:
        _parse_batch_spec("accounts: 5\nparallel: 2")
        expect("batch missing task raises", False)
    except ValueError:
        expect("batch missing task raises", True)

    # Coercion
    expect("coerce true",  _coerce_value("true")  is True)
    expect("coerce 5",     _coerce_value("5") == 5)
    expect("coerce 5.5",   _coerce_value("5.5") == 5.5)
    expect("coerce json",  _coerce_value('{"x":1}') == {"x": 1})
    expect("coerce keep",  _coerce_value("plain") == "plain")

    # Progress bar
    expect("bar 0%",   set(_progress_bar(0)) == {"░"})
    expect("bar 100%", set(_progress_bar(100)) == {"█"})
    expect("bar 50% mixed", "█" in _progress_bar(50) and "░" in _progress_bar(50))

    # Command table coverage — every promised command must be wired
    promised = {
        "newtask", "run", "batch", "status", "runs", "stop", "resume",
        "cancel", "replay", "watch", "templates", "template_save",
        "template_get", "template_delete", "accounts", "accounts_reload",
        "config", "config_set", "memory", "workers", "help", "start",
    }
    missing = promised - set(_COMMAND_TABLE)
    expect("all spec commands wired", not missing,
           detail=f"missing={missing}")

    # from_settings returns None without a bot token
    with tempfile.TemporaryDirectory() as d:
        master = Path(d) / "m.json"
        master.write_text("{}")
        env = Path(d) / "env"
        env.write_text("")  # no token
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        s = Settings(master, env)
        expect("CommandCenter.from_settings None without token",
               CommandCenter.from_settings(s) is None)


# ================================================================ NLPlanner
def test_nl_planner_regex_fallback() -> None:
    print("\n=== NLPlanner regex fallback ===")
    plan = asyncio.run(NLPlanner().parse(
        "Create 20 accounts on https://example.com with random "
        "10-digit numbers and password Test@123",
    ))
    expect("regex extracts count",
           plan.account_config.count == 20)
    expect("regex extracts password",
           plan.account_config.password == "Test@123")
    expect("regex extracts URL",
           plan.target_url == "https://example.com")
    expect("regex picks parallel for count > 3", plan.parallel is True)


def main() -> int:
    test_settings()
    test_templates()
    test_site_memory()
    test_llm_factory()
    test_llm_response_parsing()
    test_command_center_helpers()
    test_nl_planner_regex_fallback()
    print(
        f"\n=== summary: {len(_passes)} passed, {len(_fails)} failed ===",
    )
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
