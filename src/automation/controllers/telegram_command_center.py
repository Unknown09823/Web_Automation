"""Telegram-first Command Center for the autonomous browser agent.

This module is the **primary user surface** of the framework. Everything
that used to require editing JSON, YAML, ``.env``, or running CLI commands
is now driven from chat messages. The bot runs in-process inside the same
event loop as :class:`BrowserAgent`, ``AccountManager`` and the FastAPI
control plane, so it can call them directly — no HTTP hop, no token-roundtrip.

Architecture::

    Telegram (long-poll urllib)
        ↓
    CommandCenter._dispatch_*()
        ↓
    BrowserAgent / AccountManager / Settings / TemplateStore / SiteMemory
        ↓
    Browser workers (Playwright) and per-run RunContext folders

Supported commands (see ``HELP_TEXT``):

  * Task lifecycle:  /newtask /run /batch /status /runs /stop /resume
                      /cancel /replay /watch
  * Templates:       /templates /template_save /template_get /template_delete
  * Accounts:        /accounts /accounts_reload
  * Config:          /config /config_set
  * Memory:          /memory
  * Workers:         /workers
  * Help:            /start /help

Free-form text (no leading ``/``) is treated as a natural-language
instruction. The LLM parses it into a plan, the bot replies with a preview
and inline ``Run`` / ``Cancel`` / ``Save as template`` buttons.

Authorization is by chat-id allow-list, taken from
``TELEGRAM_ALLOWED_CHAT_IDS``. Unknown senders are ignored silently.

Secrets are *never* echoed: every outgoing message goes through
:meth:`Settings.public_dict`-style masking before being sent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


TELEGRAM_API = "https://api.telegram.org"
MAX_TELEGRAM_MSG = 3500  # safe under the 4096 hard cap


# =============================================================================
#  Help text
# =============================================================================
HELP_TEXT = """\
*Autonomous Browser Agent — Telegram Control Center*

*Send any free-form instruction* and I will plan + run it, e.g.
`Create 5 accounts on https://example.com with password Test@123, login, then download the latest report.`

*Task lifecycle*
/newtask <text>    Plan a new task (= just send free text)
/run <name>        Run a saved template
/batch             Open the batch wizard
/status            Show currently running tasks
/runs [N]          Show recent runs (default 10)
/stop <run_id>     Cancel a running task
/resume <run_id>   Resume a failed run from its checkpoint
/cancel <run_id>   Alias for /stop
/replay <run_id> <account_id>   Show recorded actions
/watch <run_id>    Stream live progress to this chat

*Templates*
/templates                     List saved templates
/template_save <name> <text>   Save a new template (text is the instruction)
/template_get <name>           Show a template
/template_delete <name>        Delete a template

*Accounts*
/accounts             Show account totals + last 10
/accounts_reload      Reload accounts file from disk

*Config & runtime*
/config                       Show current configuration (secrets masked)
/config_set <key> <value>     Update a non-secret setting
/memory [domain]              Show self-learning memory (per-site)
/templates_exec [domain]      Execution templates (replay layer that
                              skips the LLM after account #1)
/relearn <domain> [workflow]  Force AI on the next run instead of replay
/workers                      Show active browser workers
/help                         This help
"""


# =============================================================================
#  Primitive: thin wrapper over the Telegram Bot API (urllib only)
# =============================================================================
class TelegramAPI:
    """Tiny non-blocking Telegram client. urllib + executor — no SDK."""

    def __init__(self, token: str, request_timeout: float = 30.0) -> None:
        if not token:
            raise ValueError("TelegramAPI requires a token")
        self.token = token
        self.request_timeout = request_timeout

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        body: bytes
        headers = {}
        # Use JSON for nested values (reply_markup, etc.); urlencoded for flat
        if any(isinstance(v, (dict, list)) for v in params.values()):
            body = json.dumps(params, default=str).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = urllib.parse.urlencode(
                {k: ("" if v is None else v) for k, v in params.items()},
            ).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, _http_post_telegram, url, headers, body, self.request_timeout,
            )
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:300]
            log.warning("telegram %s -> HTTP %d: %s", method, exc.code, body_text)
            return {"ok": False, "error_code": exc.code, "description": body_text}
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram %s call failed: %r", method, exc)
            return {"ok": False, "description": repr(exc)}

    async def get_updates(
        self, offset: int, timeout: int = 25,
    ) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(
                ["message", "edited_message", "callback_query"],
            )},
        )
        return list(result.get("result", []) or [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "Markdown",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        chunks = _chunk_text(text, MAX_TELEGRAM_MSG)
        last: dict[str, Any] = {}
        for i, chunk in enumerate(chunks):
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": disable_web_page_preview,
            }
            if parse_mode:
                params["parse_mode"] = parse_mode
            # Only attach the reply markup to the LAST chunk so the buttons
            # appear at the bottom of the visible reply.
            if reply_markup is not None and i == len(chunks) - 1:
                params["reply_markup"] = reply_markup
            last = await self.call("sendMessage", params)
        return last

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str | None = "Markdown",
    ) -> dict[str, Any]:
        text = text[:MAX_TELEGRAM_MSG]
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return await self.call("editMessageText", params)

    async def answer_callback(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> dict[str, Any]:
        return await self.call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text[:200],
                "show_alert": show_alert,
            },
        )


def _http_post_telegram(
    url: str, headers: dict[str, str], body: bytes, timeout: float,
) -> dict[str, Any]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "raw": text}


def _chunk_text(text: str, size: int) -> list[str]:
    """Split a long message at line boundaries (best-effort) to fit Telegram."""
    text = text or ""
    if len(text) <= size:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut < int(size * 0.5):
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


# =============================================================================
#  Per-chat conversation state
# =============================================================================
@dataclass(slots=True)
class PendingPlan:
    """A natural-language plan awaiting user confirmation."""
    plan: Any  # ExecutionPlan
    instruction: str
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ChatState:
    """All per-chat-id session state held by the command center."""
    chat_id: int
    pending_plan: PendingPlan | None = None
    pending_template_name: str | None = None  # awaiting instruction text
    watch_run_ids: set[str] = field(default_factory=set)
    live_monitors: dict[str, asyncio.Task] = field(default_factory=dict)
    last_run_id: str | None = None


# =============================================================================
#  Live monitor — tails events.jsonl and edits a single status message
# =============================================================================
class LiveMonitor:
    """Continuously edit one Telegram message with run progress.

    The framework already writes a JSONL stream of agent events into
    ``data/runs/<run_id>/events.jsonl``. The monitor tails that file and
    rewrites a fixed Telegram message every ``update_interval`` seconds
    with a compact progress board (current account / goal / URL / retries
    / confidence / worker).

    One :class:`asyncio.Task` per (chat_id, run_id). The task ends when:
      * the run reaches a terminal status, or
      * ``cancel()`` is called.
    """

    def __init__(
        self,
        api: TelegramAPI,
        chat_id: int,
        run_id: str,
        runs_root: Path,
        *,
        update_interval: float = 4.0,
    ) -> None:
        self.api = api
        self.chat_id = chat_id
        self.run_id = run_id
        self.runs_root = runs_root
        self.update_interval = update_interval
        self.message_id: int | None = None
        self._stop = asyncio.Event()
        self._last_render = ""

    async def cancel(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        run_dir = self.runs_root / self.run_id
        events_file = run_dir / "events.jsonl"
        status_file = run_dir / "status.json"

        # Send the initial placeholder
        first = await self.api.send_message(
            self.chat_id,
            f"*Watching* `{self.run_id}` ...\n_waiting for first event_",
        )
        msg = (first.get("result") or {})
        self.message_id = msg.get("message_id")

        terminal = {"completed", "failed", "cancelled"}
        while not self._stop.is_set():
            try:
                board = self._render_board(events_file, status_file)
                if board != self._last_render and self.message_id:
                    await self.api.edit_message(
                        self.chat_id, self.message_id, board,
                    )
                    self._last_render = board
                # check termination
                status = _read_json(status_file).get("status") if status_file.exists() else None
                if status in terminal:
                    break
            except Exception:  # noqa: BLE001
                log.exception("live monitor tick failed for %s", self.run_id)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.update_interval,
                )
            except asyncio.TimeoutError:
                pass

    def _render_board(self, events_file: Path, status_file: Path) -> str:
        events = _tail_jsonl(events_file, limit=500)
        status_obj = _read_json(status_file)
        status = status_obj.get("status", "?")

        # Aggregate totals
        accounts_total = len(status_obj.get("accounts") or [])
        accounts_done = sum(
            1 for e in events
            if e.get("type") == "agent.account.completed"
        )
        accounts_failed = sum(
            1 for e in events
            if e.get("type") == "agent.account.failed"
        )
        retries = sum(
            1 for e in events
            if e.get("type") == "agent.recovery.started"
        )
        ai_calls = sum(
            1 for e in events
            if e.get("type") == "agent.ai.decision"
        )

        # Current state from the most recent informative event
        current = _last_progress_event(events)
        current_account = current.get("account_id", "—")
        current_goal = current.get("goal") or current.get("message", "—")
        current_url = current.get("data", {}).get("url") or current.get("url", "—")
        confidence = current.get("confidence")

        # Progress percentage
        pct = 0
        if accounts_total > 0:
            pct = int((accounts_done + accounts_failed) / accounts_total * 100)

        bar = _progress_bar(pct)

        lines = [
            f"*Run* `{self.run_id}` — *{status.upper()}*",
            f"`{bar}` {pct}%",
            "",
            f"*Account*  `{current_account}`",
            f"*Goal*     {_clip(current_goal, 90)}",
            f"*URL*      `{_clip(current_url, 90)}`",
            f"*Done*     {accounts_done}/{accounts_total}    "
            f"*Failed* {accounts_failed}    *Retries* {retries}",
            f"*AI calls* {ai_calls}    *Confidence* "
            f"{('%.2f' % confidence) if isinstance(confidence, (int, float)) else '—'}",
            "",
            "_/stop " + self.run_id + "_  to abort  · _/replay " + self.run_id +
            " <account>_  to inspect",
        ]
        return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _tail_jsonl(path: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _last_progress_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the most recent event that conveys current state."""
    interesting = {
        "agent.account.started", "agent.goal.started", "agent.step.started",
        "agent.waiting", "agent.ai.decision",
        "agent.verification.passed", "agent.verification.failed",
        "agent.goal.completed",
    }
    for e in reversed(events):
        if e.get("type") in interesting:
            return e
    if events:
        return events[-1]
    return {}


def _progress_bar(pct: int, width: int = 18) -> str:
    pct = max(0, min(100, int(pct)))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _clip(text: Any, n: int) -> str:
    s = str(text or "—")
    return s if len(s) <= n else s[: n - 1] + "…"


# =============================================================================
#  Main bot class
# =============================================================================
class CommandCenter:
    """The Telegram-first Command Center.

    Constructed with the in-process objects it needs to talk to. Use
    :py:meth:`from_settings` for the standard wiring.

    Lifecycle::

        cc = CommandCenter.from_settings(settings, agent=agent, accounts=acc, ...)
        await cc.start()      # spawns the long-poll task
        ...
        await cc.stop()       # cancels poll + all live monitors
    """

    def __init__(
        self,
        *,
        api: TelegramAPI,
        settings: Any,
        agent: Any = None,
        accounts: Any = None,
        browser: Any = None,
        templates: Any = None,
        site_memory: Any = None,
        nl_planner: Any = None,
        execution_templates: Any = None,
        runs_root: str | Path = "data/runs",
        allowed_chat_ids: list[int] | None = None,
        live_update_interval: float = 4.0,
    ) -> None:
        self.api = api
        self.settings = settings
        self.agent = agent
        self.accounts = accounts
        self.browser = browser
        self.templates = templates
        self.site_memory = site_memory
        self.nl_planner = nl_planner
        # ExecutionTemplateStore — surfaced via /templates_exec for operators
        # to inspect / promote / delete the deterministic replay templates
        # that the AdaptiveExecutor records and replays.
        self.execution_templates = execution_templates
        self.runs_root = Path(runs_root)
        self.allowed = set(int(x) for x in (allowed_chat_ids or []) if str(x).strip())
        self.live_update_interval = live_update_interval

        self._chats: dict[int, ChatState] = {}
        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._offset = 0

    # ---------------------------------------------------------------- factory
    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        agent: Any = None,
        accounts: Any = None,
        browser: Any = None,
        templates: Any = None,
        site_memory: Any = None,
        nl_planner: Any = None,
        execution_templates: Any = None,
    ) -> "CommandCenter | None":
        token = settings.get_secret("TELEGRAM_BOT_TOKEN")
        if not token:
            return None
        api = TelegramAPI(token=token)
        return cls(
            api=api,
            settings=settings,
            agent=agent,
            accounts=accounts,
            browser=browser,
            templates=templates,
            site_memory=site_memory,
            nl_planner=nl_planner,
            execution_templates=execution_templates,
            runs_root=settings.get("runs.path", "data/runs"),
            allowed_chat_ids=settings.telegram_allowed_chat_ids(),
            live_update_interval=float(
                settings.get("telegram.live_update_interval_seconds", 4.0),
            ),
        )

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._poll_task:
            return
        self._stop.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="tg-poll")
        log.info(
            "Telegram command center started (allowed chats=%s)",
            sorted(self.allowed) or "OPEN",
        )

    async def stop(self) -> None:
        self._stop.set()
        # cancel all live monitors
        for state in self._chats.values():
            for task in list(state.live_monitors.values()):
                task.cancel()
        if self._poll_task:
            try:
                await asyncio.wait_for(self._poll_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._poll_task.cancel()
        self._poll_task = None
        log.info("Telegram command center stopped")

    # ----------------------------------------------------------------- polling
    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self.api.get_updates(self._offset, timeout=25)
                for u in updates:
                    self._offset = max(self._offset, u["update_id"] + 1)
                    asyncio.create_task(self._handle_update(u))
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                log.exception("telegram poll error")
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        try:
            if "callback_query" in update:
                await self._handle_callback(update["callback_query"])
                return
            msg = update.get("message") or update.get("edited_message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            text = (msg.get("text") or "").strip()
            if not chat_id or not text:
                return
            if self.allowed and int(chat_id) not in self.allowed:
                log.warning(
                    "telegram: rejecting unauthorized chat_id=%s", chat_id,
                )
                return
            await self._handle_message(int(chat_id), text)
        except Exception:  # noqa: BLE001
            log.exception("telegram handle_update failed")

    # ----------------------------------------------------------------- routing
    async def _handle_message(self, chat_id: int, text: str) -> None:
        state = self._chats.setdefault(chat_id, ChatState(chat_id=chat_id))
        # Slash command
        if text.startswith("/"):
            cmd, _, args = text.partition(" ")
            cmd = cmd.lstrip("/").lower().split("@", 1)[0]
            args = args.strip()
            await self._dispatch_command(state, cmd, args)
            return

        # Conversational replies
        low = text.strip().lower()
        if state.pending_plan and low in {"yes", "y", "proceed", "go", "run", "execute"}:
            await self._execute_pending_plan(state)
            return
        if state.pending_plan and low in {"no", "n", "cancel", "abort"}:
            state.pending_plan = None
            await self._reply(chat_id, "Plan discarded.")
            return
        if state.pending_template_name:
            name = state.pending_template_name
            state.pending_template_name = None
            await self._save_template_from_text(chat_id, name, text)
            return

        # Anything else: parse as a new natural-language instruction
        await self._handle_nl_instruction(state, text)

    async def _dispatch_command(
        self, state: ChatState, cmd: str, args: str,
    ) -> None:
        chat_id = state.chat_id
        handler = _COMMAND_TABLE.get(cmd)
        if handler is None:
            await self._reply(
                chat_id,
                f"Unknown command: /{cmd}\n\nSend /help to see what I can do.",
            )
            return
        try:
            await handler(self, state, args)
        except Exception as exc:  # noqa: BLE001
            log.exception("/%s failed", cmd)
            await self._reply(chat_id, f"`/{cmd}` failed: `{exc}`")

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        data = cq.get("data") or ""
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not chat_id:
            return
        if self.allowed and int(chat_id) not in self.allowed:
            return
        state = self._chats.setdefault(int(chat_id), ChatState(chat_id=int(chat_id)))

        action, _, payload = data.partition(":")
        if action == "run":
            await self.api.answer_callback(cq_id, text="Starting…")
            await self._execute_pending_plan(state)
        elif action == "cancel":
            await self.api.answer_callback(cq_id, text="Cancelled")
            state.pending_plan = None
            await self._reply(int(chat_id), "Plan cancelled.")
        elif action == "savetpl":
            await self.api.answer_callback(
                cq_id, text="Send template name as your next message",
            )
            state.pending_template_name = "_AWAITING_NAME"
            await self._reply(
                int(chat_id),
                "Reply with: `/template_save <name>` to save this plan.",
            )
        elif action == "stop":
            await self.api.answer_callback(cq_id, text="Stopping…")
            await self._cmd_stop(state, payload)
        elif action == "watch":
            await self.api.answer_callback(cq_id, text="Watching…")
            await self._cmd_watch(state, payload)
        else:
            await self.api.answer_callback(cq_id)

    # ============================================================== commands
    async def _cmd_help(self, state: ChatState, args: str) -> None:
        await self._reply(state.chat_id, HELP_TEXT)

    async def _cmd_start(self, state: ChatState, args: str) -> None:
        banner = (
            "*Welcome to the Autonomous Browser Agent.*\n\n"
            "Send a free-form instruction or use a command.\n\n"
        )
        await self._reply(state.chat_id, banner + HELP_TEXT)

    # ----------------- new task + conversational ------------------------------
    async def _cmd_newtask(self, state: ChatState, args: str) -> None:
        if not args.strip():
            await self._reply(
                state.chat_id,
                "Send `/newtask <instruction>` or just type the instruction.",
            )
            return
        await self._handle_nl_instruction(state, args)

    async def _handle_nl_instruction(self, state: ChatState, text: str) -> None:
        if not self.nl_planner:
            await self._reply(
                state.chat_id, "NL planner not configured (no planner available).",
            )
            return
        plan = await self.nl_planner.parse(text)
        state.pending_plan = PendingPlan(plan=plan, instruction=text)
        preview = _format_plan_preview(plan)
        keyboard = {
            "inline_keyboard": [[
                {"text": "✓ Run",            "callback_data": "run:_"},
                {"text": "✗ Cancel",         "callback_data": "cancel:_"},
                {"text": "💾 Save template",  "callback_data": "savetpl:_"},
            ]],
        }
        await self._reply(state.chat_id, preview, reply_markup=keyboard)

    async def _execute_pending_plan(self, state: ChatState) -> None:
        if not state.pending_plan:
            await self._reply(state.chat_id, "No pending plan to run.")
            return
        plan = state.pending_plan.plan
        instruction = state.pending_plan.instruction
        state.pending_plan = None
        run_id = await self._launch_plan(state.chat_id, plan, instruction=instruction)
        if run_id:
            state.last_run_id = run_id

    async def _launch_plan(
        self, chat_id: int, plan: Any, *, instruction: str = "",
    ) -> str | None:
        if not self.agent:
            await self._reply(chat_id, "Agent not configured. Cannot execute.")
            return None

        # Generate accounts if requested by the plan
        from automation.agent.data_factory import DataFactory
        ac = plan.account_config
        account_ids: list[str] = []
        generated_preview: list[dict[str, Any]] = []
        if ac and ac.count >= 1:
            factory = DataFactory()
            generated = factory.generate(
                ac.count,
                password=ac.password,
                generate_numbers=bool(ac.generate_numbers),
                number_length=int(ac.number_length or 10),
                generate_emails=bool(ac.generate_emails),
                generate_usernames=bool(ac.generate_usernames),
            )
            if self.accounts:
                try:
                    await self.accounts.add_accounts(
                        [g.to_dict() for g in generated],
                    )
                except Exception:  # noqa: BLE001
                    log.exception("could not register generated accounts")
            account_ids = [g.id for g in generated]
            generated_preview = [
                {k: v for k, v in g.to_dict().items() if k != "password"}
                for g in generated[:5]
            ]

        # Fall back to "default" if planner returned no accounts
        if not account_ids:
            account_ids = ["default"]

        run = self.agent.prepare_run(
            goals=plan.goals,
            account_ids=account_ids,
            target_url=plan.target_url,
            instruction=instruction or plan.instruction,
            parallel=bool(plan.parallel),
            max_parallel=int(plan.max_parallel or 1),
        )
        asyncio.create_task(
            self.agent.execute_prepared(run), name=f"agent-run-{run.run_id}",
        )

        # Send run confirmation with action buttons
        msg = (
            f"*Run started* — `{run.run_id}`\n"
            f"goals: {len(plan.goals)}    accounts: {len(account_ids)}\n"
            f"url: `{_clip(plan.target_url, 80)}`"
        )
        if generated_preview:
            msg += "\n\n_first generated accounts (preview):_\n"
            for a in generated_preview:
                ident = a.get("number") or a.get("username") or a.get("email") or a.get("id")
                msg += f"  · `{ident}`\n"
        keyboard = {
            "inline_keyboard": [[
                {"text": "📡 Watch",  "callback_data": f"watch:{run.run_id}"},
                {"text": "⏹ Stop",    "callback_data": f"stop:{run.run_id}"},
            ]],
        }
        await self._reply(chat_id, msg, reply_markup=keyboard)
        return run.run_id

    # ---------------- run lifecycle commands ---------------------------------
    async def _cmd_status(self, state: ChatState, args: str) -> None:
        if not self.agent:
            await self._reply(state.chat_id, "Agent not configured.")
            return
        active = self.agent.active_runs or {}
        if not active:
            await self._reply(state.chat_id, "_no active runs_")
            return
        lines = ["*Active runs:*"]
        for rid, r in active.items():
            instr = _clip(r.get("instruction", ""), 60)
            n_acc = len(r.get("accounts") or [])
            lines.append(
                f"`{rid}` — {r.get('status', '?')} · accounts={n_acc}\n  {instr}",
            )
        await self._reply(state.chat_id, "\n".join(lines))

    async def _cmd_runs(self, state: ChatState, args: str) -> None:
        from automation.agent.run import list_runs
        n = 10
        if args.strip().isdigit():
            n = max(1, min(100, int(args.strip())))
        runs = list_runs(str(self.runs_root), limit=n)
        if not runs:
            await self._reply(state.chat_id, "_no runs yet_")
            return
        lines = ["*Recent runs:*"]
        for r in runs:
            ts = r.get("created_at", 0)
            ts_str = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"
            lines.append(
                f"`{r.get('run_id')}` · {r.get('status', '?')} · {ts_str}\n"
                f"  {_clip(r.get('instruction', ''), 70)}",
            )
        await self._reply(state.chat_id, "\n".join(lines))

    async def _cmd_stop(self, state: ChatState, args: str) -> None:
        run_id = args.strip() or state.last_run_id
        if not run_id:
            await self._reply(
                state.chat_id, "Usage: `/stop <run_id>`",
            )
            return
        if not self.agent:
            await self._reply(state.chat_id, "Agent not configured.")
            return
        ok = await self.agent.cancel(run_id)
        if ok:
            await self._reply(state.chat_id, f"⏹  Stopping `{run_id}` …")
        else:
            await self._reply(state.chat_id, f"`{run_id}` is not active.")

    async def _cmd_resume(self, state: ChatState, args: str) -> None:
        run_id = args.strip() or state.last_run_id
        if not run_id:
            await self._reply(state.chat_id, "Usage: `/resume <run_id>`")
            return
        if not self.agent:
            await self._reply(state.chat_id, "Agent not configured.")
            return
        run_dir = Path(self.runs_root) / run_id
        if not run_dir.exists():
            await self._reply(state.chat_id, f"Run `{run_id}` not found.")
            return
        asyncio.create_task(
            self.agent.resume_run(str(run_dir)), name=f"agent-resume-{run_id}",
        )
        state.last_run_id = run_id
        await self._reply(state.chat_id, f"⏵  Resuming `{run_id}` …")

    async def _cmd_replay(self, state: ChatState, args: str) -> None:
        parts = args.split()
        if len(parts) < 2:
            await self._reply(
                state.chat_id, "Usage: `/replay <run_id> <account_id>`",
            )
            return
        run_id, account_id = parts[0], parts[1]
        path = Path(self.runs_root) / run_id / account_id / "replay.json"
        if not path.exists():
            await self._reply(
                state.chat_id, f"No replay for `{run_id}` / `{account_id}`.",
            )
            return
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            await self._reply(state.chat_id, "Replay file is corrupt.")
            return
        actions = data if isinstance(data, list) else data.get("actions") or []
        lines = [f"*Replay* `{run_id}` / `{account_id}` — {len(actions)} actions"]
        for a in actions[:30]:
            kind = a.get("kind") or a.get("type") or "?"
            target = a.get("selector") or a.get("url") or a.get("goal") or ""
            lines.append(f"  · {kind}  `{_clip(target, 60)}`")
        if len(actions) > 30:
            lines.append(f"  … ({len(actions) - 30} more)")
        await self._reply(state.chat_id, "\n".join(lines))

    async def _cmd_watch(self, state: ChatState, args: str) -> None:
        run_id = args.strip() or state.last_run_id
        if not run_id:
            await self._reply(state.chat_id, "Usage: `/watch <run_id>`")
            return
        if run_id in state.live_monitors:
            await self._reply(state.chat_id, f"Already watching `{run_id}`.")
            return
        monitor = LiveMonitor(
            api=self.api,
            chat_id=state.chat_id,
            run_id=run_id,
            runs_root=self.runs_root,
            update_interval=self.live_update_interval,
        )
        task = asyncio.create_task(
            monitor.run(), name=f"watch-{run_id}-{state.chat_id}",
        )
        state.live_monitors[run_id] = task
        state.watch_run_ids.add(run_id)

        def _done(_t: asyncio.Task) -> None:
            state.live_monitors.pop(run_id, None)
            state.watch_run_ids.discard(run_id)
        task.add_done_callback(_done)

    # ------------------- templates -------------------------------------------
    async def _cmd_templates(self, state: ChatState, args: str) -> None:
        if not self.templates:
            await self._reply(state.chat_id, "Templates not configured.")
            return
        items = self.templates.list()
        if not items:
            await self._reply(state.chat_id, "_no templates yet_")
            return
        lines = ["*Templates:*"]
        for t in items:
            lines.append(
                f"`{t.name}` · uses={t.uses} · goals={len(t.goals)}\n"
                f"  {_clip(t.instruction, 70)}",
            )
        await self._reply(state.chat_id, "\n".join(lines))

    async def _cmd_template_save(self, state: ChatState, args: str) -> None:
        if not self.templates:
            await self._reply(state.chat_id, "Templates not configured.")
            return
        parts = args.split(None, 1)
        if not parts:
            await self._reply(
                state.chat_id,
                "Usage: `/template_save <name> <instruction>` or "
                "`/template_save <name>` then send instruction next.",
            )
            return
        name = parts[0]
        try:
            self.templates.validate_name(name)
        except ValueError as exc:
            await self._reply(state.chat_id, f"Invalid name: {exc}")
            return
        if len(parts) == 1:
            state.pending_template_name = name
            await self._reply(
                state.chat_id,
                f"Send the instruction text for template *{name}* as your next message.",
            )
            return
        await self._save_template_from_text(state.chat_id, name, parts[1])

    async def _save_template_from_text(
        self, chat_id: int, name: str, instruction: str,
    ) -> None:
        if not self.nl_planner or not self.templates:
            await self._reply(chat_id, "Planner or template store missing.")
            return
        plan = await self.nl_planner.parse(instruction)
        from automation.agent.templates import Template
        tpl = Template.from_plan(name, plan)
        saved = self.templates.save(tpl)
        await self._reply(
            chat_id,
            f"Saved template `{saved.name}` "
            f"(goals={len(saved.goals)}, target=`{_clip(saved.target_url, 60)}`).",
        )

    async def _cmd_template_get(self, state: ChatState, args: str) -> None:
        if not self.templates:
            await self._reply(state.chat_id, "Templates not configured.")
            return
        name = args.strip()
        if not name:
            await self._reply(state.chat_id, "Usage: `/template_get <name>`")
            return
        t = self.templates.get(name)
        if not t:
            await self._reply(state.chat_id, f"No template `{name}`.")
            return
        body = (
            f"*Template* `{t.name}`\n"
            f"uses: {t.uses}\n"
            f"target: `{_clip(t.target_url, 80)}`\n"
            f"instruction: {_clip(t.instruction, 200)}\n"
            f"goals:\n"
            + "\n".join(
                f"  {i + 1}. {g.get('description') or g.get('type', '?')}"
                for i, g in enumerate(t.goals)
            )
        )
        await self._reply(state.chat_id, body)

    async def _cmd_template_delete(self, state: ChatState, args: str) -> None:
        if not self.templates:
            await self._reply(state.chat_id, "Templates not configured.")
            return
        name = args.strip()
        if not name:
            await self._reply(state.chat_id, "Usage: `/template_delete <name>`")
            return
        ok = self.templates.delete(name)
        await self._reply(
            state.chat_id,
            f"Deleted `{name}`." if ok else f"No template `{name}`.",
        )

    async def _cmd_run(self, state: ChatState, args: str) -> None:
        """Run a saved template by name."""
        if not self.templates:
            await self._reply(state.chat_id, "Templates not configured.")
            return
        name = args.strip()
        if not name:
            await self._reply(state.chat_id, "Usage: `/run <template_name>`")
            return
        t = self.templates.get(name)
        if not t:
            await self._reply(state.chat_id, f"No template `{name}`.")
            return
        # Build an ExecutionPlan from the stored template
        from automation.agent.goals import AgentGoal
        from automation.agent.nl_planner import AccountGenConfig, ExecutionPlan
        plan = ExecutionPlan(
            instruction=t.instruction,
            goals=[AgentGoal.from_dict(g) for g in t.goals],
            account_config=AccountGenConfig.from_dict(t.account_config or {}),
            target_url=t.target_url,
            parallel=t.parallel,
            max_parallel=t.max_parallel,
        )
        run_id = await self._launch_plan(
            state.chat_id, plan, instruction=t.instruction,
        )
        if run_id:
            self.templates.record_use(name)
            state.last_run_id = run_id

    # ------------------- batch ----------------------------------------------
    async def _cmd_batch(self, state: ChatState, args: str) -> None:
        """One-shot batch wizard. Supports header-style invocation::

            /batch
            accounts: 100
            parallel: 5
            password: Test@123
            target: https://example.com
            task:
            Register
            Login
            Complete profile
        """
        if not args.strip():
            await self._reply(
                state.chat_id,
                "*Batch wizard*\n"
                "Send the command in this form:\n"
                "```\n/batch\naccounts: 100\nparallel: 5\n"
                "password: Test@123\ntarget: https://example.com\ntask:\n"
                "Register\nLogin\nComplete profile\n```",
            )
            return
        try:
            spec = _parse_batch_spec(args)
        except ValueError as exc:
            await self._reply(state.chat_id, f"Could not parse batch: {exc}")
            return

        if not self.nl_planner:
            await self._reply(state.chat_id, "Planner not configured.")
            return
        plan = await self.nl_planner.parse(spec["task"])
        # Override account config and execution params from headers
        plan.account_config.count = spec.get("accounts", plan.account_config.count) or 1
        if spec.get("password"):
            plan.account_config.password = spec["password"]
        if spec.get("target"):
            plan.target_url = spec["target"]
        plan.parallel = bool(spec.get("parallel", 1) and int(spec["parallel"]) > 1)
        plan.max_parallel = int(spec.get("parallel", plan.max_parallel) or 1)

        state.pending_plan = PendingPlan(plan=plan, instruction=spec["task"])
        preview = _format_plan_preview(plan)
        keyboard = {
            "inline_keyboard": [[
                {"text": "✓ Run", "callback_data": "run:_"},
                {"text": "✗ Cancel", "callback_data": "cancel:_"},
            ]],
        }
        await self._reply(state.chat_id, preview, reply_markup=keyboard)

    # ------------------- accounts -------------------------------------------
    async def _cmd_accounts(self, state: ChatState, args: str) -> None:
        if not self.accounts:
            await self._reply(state.chat_id, "Account manager not configured.")
            return
        try:
            status = self.accounts.status()
        except AttributeError:
            status = {}
        progress = status.get("progress") or {}
        lines = ["*Accounts:*"]
        for k in ("total", "pending", "running", "completed", "failed", "skipped",
                  "rejected"):
            lines.append(f"  {k:<10} {progress.get(k, 0)}")
        lines.append("")
        accs = []
        try:
            accs = self.accounts.list_accounts(limit=10)
        except Exception:  # noqa: BLE001
            pass
        if accs:
            lines.append("*Last 10:*")
            for a in accs:
                ident = (
                    getattr(a, "number", "")
                    or getattr(a, "username", "")
                    or getattr(a, "email", "")
                    or getattr(a, "id", "")
                )
                lines.append(
                    f"  `{ident}` · {getattr(a, 'status', '?')}",
                )
        await self._reply(state.chat_id, "\n".join(lines))

    async def _cmd_accounts_reload(self, state: ChatState, args: str) -> None:
        if not self.accounts:
            await self._reply(state.chat_id, "Account manager not configured.")
            return
        try:
            res = self.accounts.load(replace_static=True)
            await self._reply(
                state.chat_id, f"Reloaded — loaded={res}",
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(state.chat_id, f"Reload failed: `{exc}`")

    # ------------------- config ---------------------------------------------
    async def _cmd_config(self, state: ChatState, args: str) -> None:
        public = self.settings.public_dict()
        text = "*Configuration* (secrets masked):\n```\n" + json.dumps(
            public, indent=2, default=str,
        )[:3000] + "\n```"
        await self._reply(state.chat_id, text)

    async def _cmd_config_set(self, state: ChatState, args: str) -> None:
        parts = args.split(None, 1)
        if len(parts) != 2:
            await self._reply(
                state.chat_id,
                "Usage: `/config_set <key> <value>`\n"
                "Use dotted (`execution.max_parallel_workers`) or "
                "flat (`MAX_PARALLEL_WORKERS`) keys.\n"
                "Secret keys must be set via .env, not via Telegram.",
            )
            return
        key, value = parts
        coerced = _coerce_value(value)
        try:
            self.settings.set(key, coerced, persist=True)
        except PermissionError as exc:
            await self._reply(state.chat_id, f"Refused: `{exc}`")
            return
        await self._reply(
            state.chat_id, f"Set `{key}` = `{self.settings.get(key)}` (persisted).",
        )

    # ------------------- memory ---------------------------------------------
    async def _cmd_memory(self, state: ChatState, args: str) -> None:
        if not self.site_memory:
            await self._reply(state.chat_id, "Site memory not configured.")
            return
        domain = args.strip()
        if domain:
            rec = self.site_memory.get(domain)
            if not rec.successes and not rec.failures:
                await self._reply(state.chat_id, f"No memory for `{domain}`.")
                return
            body = (
                f"*Site* `{rec.domain}`\n"
                f"successes: {rec.successes}    failures: {rec.failures}    "
                f"confidence: {rec.confidence}\n"
                f"workflows:\n"
                + "\n".join(
                    f"  · {wf}: runs={s.get('runs', 0)} ok={s.get('ok', 0)} "
                    f"avg={s.get('avg_seconds', 0):.1f}s"
                    for wf, s in rec.workflows.items()
                )
                + f"\nlogins: {len(rec.logins)}    submits: {len(rec.submits)}    "
                f"dashboards: {len(rec.dashboard_urls)}"
            )
            await self._reply(state.chat_id, body)
            return
        summary = self.site_memory.summary()
        lines = [
            f"*Site memory* — {summary['sites']} sites · "
            f"{summary['total_successes']} successes · "
            f"{summary['total_failures']} failures",
        ]
        for entry in summary.get("by_domain", [])[:25]:
            lines.append(
                f"  `{entry['domain']}` · ok={entry['successes']} · "
                f"fail={entry['failures']} · conf={entry['confidence']}",
            )
        await self._reply(state.chat_id, "\n".join(lines))

    # ------------------- workers --------------------------------------------
    async def _cmd_workers(self, state: ChatState, args: str) -> None:
        if not self.browser:
            await self._reply(state.chat_id, "Browser manager not configured.")
            return
        try:
            sessions = self.browser.list_sessions()
        except Exception:  # noqa: BLE001 — older managers
            sessions = []
        if not sessions:
            await self._reply(state.chat_id, "_no active browser workers_")
            return
        lines = ["*Workers:*"]
        for s in sessions:
            acc = getattr(s, "account_id", "?")
            healthy = getattr(s, "healthy", True)
            profile = getattr(s, "profile_id", "")
            mark = "✓" if healthy else "✗"
            lines.append(f"  {mark} `{acc}` · profile=`{profile}`")
        await self._reply(state.chat_id, "\n".join(lines))

    # --------------------- execution templates (adaptive) ----------------
    async def _cmd_templates_exec(self, state: ChatState, args: str) -> None:
        """Surface the deterministic-replay templates the AdaptiveExecutor
        records and uses to skip the LLM after account #1.

        Forms:
          /templates_exec                 — global summary
          /templates_exec <domain>        — per-domain detail
        """
        store = self.execution_templates
        if store is None:
            await self._reply(
                state.chat_id, "Execution template store not configured.",
            )
            return
        domain = args.strip().lower()
        if not domain:
            summary = store.summary()
            domains = summary.get("domains", []) or []
            if not domains:
                await self._reply(
                    state.chat_id,
                    "_no execution templates yet — run a goal once and one "
                    "will be recorded automatically_",
                )
                return
            lines = [
                f"*Execution templates* — {len(domains)} domain(s)",
                "",
            ]
            for d in domains[:20]:
                lines.append(f"*{d['domain']}*")
                for w in d.get("workflows", []):
                    lines.append(
                        f"  · `{w['workflow']}` v{w['latest_version']}  "
                        f"conf={w['confidence']:.2f}  "
                        f"replays={w['replays_succeeded']}/"
                        f"{w['replays_attempted']}  "
                        f"avg={w['avg_duration_ms']}ms",
                    )
            await self._reply(state.chat_id, "\n".join(lines))
            return

        templates = store.list_for_domain(domain)
        if not templates:
            await self._reply(
                state.chat_id, f"No execution templates for `{domain}`.",
            )
            return
        # Group by workflow, show latest version of each + per-version stats
        by_wf: dict[str, list[Any]] = {}
        for t in templates:
            by_wf.setdefault(t.workflow, []).append(t)
        lines = [f"*Execution templates for {domain}*", ""]
        for wf, versions in by_wf.items():
            versions.sort(key=lambda x: x.version, reverse=True)
            latest = versions[0]
            lines.append(
                f"*{wf}* — latest v{latest.version} · "
                f"confidence={latest.confidence:.2f}  "
                f"({len(versions)} version(s))",
            )
            lines.append(
                f"  replays: {latest.stats.replays_succeeded}/"
                f"{latest.stats.replays_attempted} succeeded · "
                f"avg {int(latest.stats.avg_duration_ms)}ms · "
                f"actions={len(latest.actions)}",
            )
            if latest.input_keys_required:
                lines.append(
                    "  inputs needed: "
                    + ", ".join(f"`{k}`" for k in sorted(latest.input_keys_required)),
                )
            if latest.stats.last_failure_reason:
                lines.append(
                    f"  last failure: _{_clip(latest.stats.last_failure_reason, 80)}_",
                )
            lines.append("")
        await self._reply(state.chat_id, "\n".join(lines))

    async def _cmd_relearn(self, state: ChatState, args: str) -> None:
        """Delete the highest-confidence template for (domain[, workflow])
        so the next run on that domain re-engages the AI brain and records
        a fresh template version.

          /relearn example.com                 — wipe ALL workflows
          /relearn example.com register_account — wipe just one workflow
        """
        store = self.execution_templates
        if store is None:
            await self._reply(
                state.chat_id, "Execution template store not configured.",
            )
            return
        parts = args.strip().split()
        if not parts:
            await self._reply(
                state.chat_id,
                "Usage: `/relearn <domain> [workflow]`",
            )
            return
        domain = parts[0]
        workflow = parts[1] if len(parts) > 1 else None
        templates = store.list_for_domain(domain)
        if workflow:
            templates = [t for t in templates if t.workflow == workflow]
        if not templates:
            await self._reply(
                state.chat_id,
                f"No matching execution templates to relearn for `{domain}`"
                + (f" / `{workflow}`" if workflow else ""),
            )
            return
        deleted = 0
        for t in templates:
            if store.delete(t.domain, t.workflow, t.version):
                deleted += 1
        await self._reply(
            state.chat_id,
            f"🧹 Cleared {deleted} template(s) for `{domain}`"
            + (f" / `{workflow}`" if workflow else "")
            + ". Next run will re-engage the AI and record a fresh template.",
        )

    # ------------------------------------------------------------------ I/O
    async def _reply(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> dict[str, Any]:
        return await self.api.send_message(
            chat_id, text, reply_markup=reply_markup,
        )


# =============================================================================
#  Helpers
# =============================================================================
def _format_plan_preview(plan: Any) -> str:
    """Render an :class:`ExecutionPlan` as a Markdown preview.

    Any password the planner extracted from the instruction is masked in the
    echoed instruction line so the user's secret never round-trips back to
    the chat.
    """
    ac = plan.account_config
    instruction = _redact_secret(plan.instruction or "", ac.password)
    lines = [
        "*Plan ready.*",
        "",
        f"*Instruction:* {_clip(instruction, 200)}",
    ]
    if plan.target_url:
        lines.append(f"*URL:*  `{_clip(plan.target_url, 90)}`")
    lines.append("")
    lines.append("*Goals:*")
    for i, g in enumerate(plan.goals, 1):
        desc = g.description or g.type.value
        lines.append(f"  {i}. {desc}")
    lines.append("")
    lines.append(f"*Accounts:* {ac.count}")
    if ac.password:
        lines.append("*Password:* `****`  _(set)_")
    lines.append(
        f"*Parallel:* {plan.parallel} (max={plan.max_parallel})",
    )
    if plan.estimated_time_seconds:
        lines.append(f"*Estimated:* ~{int(plan.estimated_time_seconds)}s")
    if plan.notes:
        lines.append("")
        lines.append("*Notes:*")
        for n in plan.notes:
            lines.append(f"  · {n}")
    lines.append("")
    lines.append("Tap *✓ Run* to execute, or send `yes` / `no`.")
    return "\n".join(lines)


def _redact_secret(text: str, secret: str) -> str:
    """Replace ``secret`` with ``****`` in ``text``. No-op if ``secret`` empty."""
    if not secret or not text:
        return text
    return text.replace(secret, "****")


def _parse_batch_spec(text: str) -> dict[str, Any]:
    """Parse the ``/batch`` body into a header dict + ``task`` body."""
    spec: dict[str, Any] = {}
    body_lines: list[str] = []
    in_task = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_task:
            if line:
                body_lines.append(line)
            continue
        if not line:
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "task":
                in_task = True
                if val:
                    body_lines.append(val)
                continue
            if key in ("accounts", "parallel", "max_parallel"):
                try:
                    spec[key] = int(val)
                except ValueError:
                    raise ValueError(f"{key} must be an integer")
            else:
                spec[key] = val
        else:
            # No colon: treat as part of the task implicitly
            in_task = True
            body_lines.append(line)
    spec["task"] = "\n".join(body_lines).strip()
    if not spec.get("task"):
        raise ValueError("missing task body")
    return spec


def _coerce_value(text: str) -> Any:
    """Coerce a string into bool/int/float/json/list when reasonable."""
    s = text.strip()
    low = s.lower()
    if low in ("true", "yes", "on"): return True
    if low in ("false", "no", "off"): return False
    if low in ("null", "none"): return None
    try:
        if "." in s and not s.startswith("0."):
            return float(s)
        return int(s)
    except ValueError:
        pass
    if s.startswith(("[", "{")):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    return s


# =============================================================================
#  Command dispatch table — each entry: name -> async (CommandCenter, ChatState, args) -> None
# =============================================================================
_COMMAND_TABLE: dict[str, Callable[[CommandCenter, ChatState, str], Awaitable[None]]] = {
    "start":            CommandCenter._cmd_start,
    "help":             CommandCenter._cmd_help,
    "?":                CommandCenter._cmd_help,
    "newtask":          CommandCenter._cmd_newtask,
    "status":           CommandCenter._cmd_status,
    "runs":             CommandCenter._cmd_runs,
    "stop":             CommandCenter._cmd_stop,
    "cancel":           CommandCenter._cmd_stop,
    "resume":           CommandCenter._cmd_resume,
    "replay":           CommandCenter._cmd_replay,
    "watch":            CommandCenter._cmd_watch,
    "templates":        CommandCenter._cmd_templates,
    "template_save":    CommandCenter._cmd_template_save,
    "template_get":     CommandCenter._cmd_template_get,
    "template_delete":  CommandCenter._cmd_template_delete,
    "run":              CommandCenter._cmd_run,
    "batch":            CommandCenter._cmd_batch,
    "accounts":         CommandCenter._cmd_accounts,
    "accounts_reload":  CommandCenter._cmd_accounts_reload,
    "config":           CommandCenter._cmd_config,
    "config_set":       CommandCenter._cmd_config_set,
    "memory":           CommandCenter._cmd_memory,
    "workers":          CommandCenter._cmd_workers,
    "templates_exec":   CommandCenter._cmd_templates_exec,
    "exec_templates":   CommandCenter._cmd_templates_exec,  # alias
    "relearn":          CommandCenter._cmd_relearn,
}


__all__ = ["CommandCenter", "TelegramAPI", "LiveMonitor", "HELP_TEXT"]
