"""Telegram controller — the framework's primary control surface.

Polls the Telegram Bot API (no third-party SDK; uses ``urllib``) and
forwards authorized commands to the local FastAPI backend. The bot is a
full execution controller, not a monitoring dashboard: it can plan and
execute tasks, stream live progress, cancel/resume runs, replay past
runs, batch-execute against many accounts, and surface learned templates
— all of it without touching JSON/YAML files or SSH.

Configuration (any of the below):
  - env ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_ALLOWED_CHAT_IDS`` (csv)
  - config keys ``telegram.token`` and ``telegram.allowed_chat_ids``

Authorization is allow-list only. Unknown chat IDs are dropped silently
and logged.

Three command groups
====================

Execution (the new control center)
  /run [text]        plan + ask confirmation + execute
  /plan [text]       preview a plan (no execution)
  /newtask           same as /run, prompts for the instruction
  /batch [text]      multi-account NL with explicit batching
  /watch [run_id]    stream live progress to this chat
  /unwatch [run_id]  stop streaming
  /cancel [run_id]   cancel a running execution
  /resume <run_id>   resume from the last checkpoint
  /replay <run_id> [account_id]
                     fetch a replay summary
  /templates         list learned page templates (replay-first cache)
  /runs [N]          recent runs
  /runinfo <run_id>  detail for one run
  /wf <name> [account_id]
                     run a workflow (optionally bound to one account)
  /wfbatch <name> <account_ids…>
                     run a workflow against many accounts
  /abort             discard pending un-confirmed plan

Monitoring (kept from the old bot)
  /status /health /plugins /logs /workers /queue /workflows /ai
  /accounts /completed /failed /rejected /reload_accounts

Help
  /start /help

Free text (no leading slash) is treated as a natural-language
instruction and goes through the same /run flow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

# Soft cap so a single Telegram message never exceeds the 4096-char limit.
_MAX_TG_CHARS = 3500

# Watcher loop tuning — kept friendly to Telegram's ~30 msg/s/user budget.
_WATCH_POLL_S = 1.5
_WATCH_BATCH_S = 2.0
# Allow a few transient API failures inside the watcher before giving up.
_WATCH_MAX_API_FAILURES = 5
# Pending plans (un-confirmed) become stale and refuse to execute after this.
_PENDING_PLAN_TTL_S = 600.0


# ---------------------------------------------------------------- session


@dataclass
class PendingPlan:
    """A plan awaiting user confirmation before execution."""

    token: str
    instruction: str
    plan: dict[str, Any]
    preview: dict[str, Any]
    parallel: bool
    max_parallel: int | None
    created_at: float = field(default_factory=time.time)


@dataclass
class WatchSession:
    """One live-stream of a run to a chat."""

    run_id: str
    cursor: int = 0
    task: asyncio.Task[None] | None = None


@dataclass
class ChatSession:
    """Per-chat state held in memory only — survives restarts via Telegram itself."""

    chat_id: int
    pending_plan: PendingPlan | None = None
    awaiting: str | None = None  # e.g. "instruction" after /newtask
    watchers: dict[str, WatchSession] = field(default_factory=dict)
    last_run_id: str | None = None


# ---------------------------------------------------------------- controller


class TelegramController:
    """Long-polling Telegram bot that drives the framework's HTTP API."""

    def __init__(
        self,
        token: str,
        allowed_chat_ids: list[int],
        api_base_url: str = "http://127.0.0.1:8080",
        api_token: str | None = None,
    ) -> None:
        self.token = token
        self.allowed = {int(x) for x in allowed_chat_ids if str(x).strip()}
        self.api_base_url = api_base_url.rstrip("/")
        self.api_token = api_token

        self._offset = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._sessions: dict[int, ChatSession] = {}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "TelegramController | None":
        tg = (config or {}).get("telegram", {}) or {}
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("token")
        if not token:
            return None
        env_chats = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        allowed = [int(x.strip()) for x in env_chats.split(",") if x.strip()]
        if not allowed:
            allowed = [int(x) for x in (tg.get("allowed_chat_ids") or [])]
        return cls(
            token=token,
            allowed_chat_ids=allowed,
            api_base_url=os.environ.get("AUTOMATION_API_URL", "http://127.0.0.1:8080"),
            api_token=os.environ.get("AUTOMATION_API_TOKEN"),
        )

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poll")
        log.info(
            "Telegram controller started (allowed chats: %s)", sorted(self.allowed),
        )

    async def stop(self) -> None:
        self._stop.set()
        # Cancel watcher tasks AND wait for them to drain so in-flight sends
        # complete (and asyncio doesn't warn about destroyed pending tasks).
        watcher_tasks: list[asyncio.Task[None]] = []
        for sess in self._sessions.values():
            for w in list(sess.watchers.values()):
                if w.task and not w.task.done():
                    w.task.cancel()
                    watcher_tasks.append(w.task)
        if watcher_tasks:
            await asyncio.gather(*watcher_tasks, return_exceptions=True)
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------- poll
    async def _poll_loop(self) -> None:
        if not self.allowed:
            # Fail-closed: refuse to accept any updates when no operator is on
            # the allow-list. The pre-rewrite bot fell open here because it was
            # monitoring-only; the rewrite can execute plans, so a missing
            # allow-list is a footgun, not a convenience.
            log.error(
                "Telegram allow-list is empty — refusing to process any updates."
                " Set TELEGRAM_ALLOWED_CHAT_IDS or telegram.allowed_chat_ids."
            )
            await self._stop.wait()
            return

        while not self._stop.is_set():
            try:
                # ``allowed_updates`` keeps the stream tight and lets Telegram
                # know we want callback queries (inline keyboard taps).
                updates = await self._call(
                    "getUpdates",
                    {
                        "timeout": 25,
                        "offset": self._offset,
                        "allowed_updates": json.dumps(
                            ["message", "edited_message", "callback_query"],
                        ),
                    },
                )
                for u in updates.get("result", []) or []:
                    self._offset = u["update_id"] + 1
                    try:
                        await self._handle_update(u)
                    except Exception:  # noqa: BLE001
                        log.exception("telegram update handler failed")
            except Exception:  # noqa: BLE001
                log.exception("telegram poll error")
                await asyncio.sleep(5)

    # ------------------------------------------------------------- dispatcher
    async def _handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return

        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not self._authorized(chat_id):
            log.warning("telegram: rejecting unauthorized chat_id=%s", chat_id)
            return

        sess = self._session(chat_id)

        # If the chat is in a "fill in the blank" state, take the text as
        # input for whatever was asked.
        if sess.awaiting == "instruction":
            sess.awaiting = None
            await self._begin_run_flow(sess, text, batch_hint=False)
            return
        if sess.awaiting == "batch_instruction":
            sess.awaiting = None
            await self._begin_run_flow(sess, text, batch_hint=True)
            return

        # Slash command vs. natural language.
        if text.startswith("/"):
            await self._dispatch_command(sess, text)
        else:
            # Treat any free-form text as an NL instruction.
            await self._begin_run_flow(sess, text, batch_hint=False)

    async def _dispatch_command(self, sess: ChatSession, text: str) -> None:
        parts = text.split(maxsplit=1)
        head = parts[0].lower().lstrip("/").split("@", 1)[0]
        body = parts[1].strip() if len(parts) > 1 else ""
        args = body.split() if body else []

        try:
            reply = await self._run_command(sess, head, body, args)
        except _Reply as r:  # explicit pre-formatted reply (skip default send)
            if r.text:
                await self._send(sess.chat_id, r.text, reply_markup=r.markup)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("command failed: /%s", head)
            reply = f"error: {exc}"

        if reply:
            await self._send(sess.chat_id, reply)

    # ------------------------------------------------------------- callback
    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        cq_id = cq.get("id")
        chat = (cq.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id")
        data = cq.get("data") or ""
        if not chat_id:
            await self._answer_callback(cq_id, "no chat", alert=False)
            return
        if not self._authorized(chat_id):
            await self._answer_callback(cq_id, "unauthorized", alert=True)
            return

        sess = self._session(chat_id)
        action, _, token = data.partition(":")

        if action == "confirm":
            pending = sess.pending_plan
            if not pending or pending.token != token:
                await self._answer_callback(cq_id, "plan expired", alert=True)
                return
            if (time.time() - pending.created_at) > _PENDING_PLAN_TTL_S:
                sess.pending_plan = None
                await self._answer_callback(cq_id, "plan expired (TTL)", alert=True)
                await self._send(chat_id, "plan expired — please /run again")
                return
            await self._answer_callback(cq_id, "starting…")
            await self._execute_pending(sess, pending)
            return

        if action == "cancel":
            pending = sess.pending_plan
            if pending and pending.token == token:
                sess.pending_plan = None
                await self._answer_callback(cq_id, "discarded")
                await self._send(chat_id, "plan discarded")
            else:
                await self._answer_callback(cq_id, "nothing to cancel")
            return

        if action == "watch":
            await self._answer_callback(cq_id, "watching")
            await self._start_watch(sess, token)
            return

        if action == "stop":
            await self._answer_callback(cq_id, "cancelling")
            await self._cancel_run(sess, token)
            return

        await self._answer_callback(cq_id, f"unknown action: {action}", alert=True)

    # ---------------------------------------------------------- session util
    def _session(self, chat_id: int) -> ChatSession:
        sess = self._sessions.get(chat_id)
        if sess is None:
            sess = ChatSession(chat_id=chat_id)
            self._sessions[chat_id] = sess
        return sess

    def _authorized(self, chat_id: int) -> bool:
        """Allow-list check. Fails closed when the list is empty.

        The bot can now execute plans, so an empty allow-list is treated as
        "block everyone" rather than "allow everyone" (the old monitoring
        bot's default). Operators must add at least one chat id explicitly.
        """
        return bool(self.allowed) and chat_id in self.allowed

    # -------------------------------------------------------- command router
    async def _run_command(
        self,
        sess: ChatSession,
        cmd: str,
        body: str,
        args: list[str],
    ) -> str:
        # ----- discovery / monitoring (kept from the old bot) ------------
        if cmd in {"start", "help", "?"}:
            # /start is Telegram's conventional welcome (clients tap it on
            # first open) so we surface help here rather than starting the
            # engine. The engine-start path moved to /engine_start so it is
            # still reachable from chat.
            return _HELP

        if cmd == "status":
            return _summary(
                await self._api("GET", "/status"),
                keys=("running", "status"),
            )
        if cmd == "health":
            return _summary(await self._api("GET", "/health"))
        if cmd == "plugins":
            data = await self._api("GET", "/plugins")
            lines = [
                f"{p['name']} v{p.get('metadata', {}).get('version','?')} "
                f"enabled={p['enabled']} started={p['started']}"
                for p in data.get("plugins", []) or []
            ]
            return "\n".join(lines) or "no plugins"
        if cmd == "logs":
            name = args[0] if args else "activity"
            data = await self._api("GET", f"/logs/{name}?lines=20")
            return "\n".join(data.get("lines", []))[-_MAX_TG_CHARS:] or "(empty)"
        if cmd in {"workers", "queue"}:
            data = await self._api("GET", "/status")
            return json.dumps(
                {"scheduler": data.get("scheduler"), "queues": data.get("queues")},
                indent=2,
            )

        # engine lifecycle
        if cmd in {"engine_start", "engine"}:
            return _summary(await self._api("POST", "/control/start"))
        if cmd == "stop":
            return _summary(await self._api("POST", "/control/stop"))
        if cmd == "restart":
            return _summary(await self._api("POST", "/control/restart"))
        if cmd == "reload":
            return _summary(await self._api("POST", "/control/reload"))

        # accounts
        if cmd == "accounts":
            return _format_accounts_status(
                await self._api("GET", "/accounts/status"),
            )
        if cmd == "completed":
            limit = int(args[0]) if args and args[0].isdigit() else 20
            data = await self._api("GET", f"/accounts/completed?limit={limit}")
            return _format_account_list(data, "completed")
        if cmd == "failed":
            limit = int(args[0]) if args and args[0].isdigit() else 20
            data = await self._api("GET", f"/accounts/failed?limit={limit}")
            return _format_account_list(data, "failed")
        if cmd == "rejected":
            data = await self._api("GET", "/accounts/rejected?limit=20")
            entries = data.get("rejected", []) or []
            if not entries:
                return "no rejected accounts"
            lines = [f"#{e.get('source_index')}: {e.get('reason')}" for e in entries]
            return "rejected:\n" + "\n".join(lines)
        if cmd in {"reload_accounts", "accounts_reload"}:
            data = await self._api("POST", "/accounts/reload")
            return (
                f"reloaded: loaded={data.get('loaded', 0)} "
                f"rejected={data.get('rejected', 0)}"
            )
        if cmd == "ai":
            data = await self._api("GET", "/ai/status")
            return json.dumps(data, indent=2, default=str)

        # ------------------------- execution control center ---------------
        if cmd == "run":
            if not body:
                sess.awaiting = "instruction"
                return "send the instruction in your next message"
            await self._begin_run_flow(sess, body, batch_hint=False)
            raise _Reply("")  # response already sent

        if cmd == "plan":
            if not body:
                return "usage: /plan <instruction>"
            await self._send_plan_only(sess, body)
            raise _Reply("")

        if cmd == "newtask":
            sess.awaiting = "instruction"
            return (
                "send the instruction in your next message — for example:\n"
                "  Create 5 accounts on https://example.com/signup with "
                "password Test@123. Then login and complete onboarding."
            )

        if cmd == "batch":
            if not body:
                sess.awaiting = "batch_instruction"
                return (
                    "send the batch instruction. Mention parallelism if you want it,"
                    " e.g.\n"
                    "  Accounts: 50 Parallel: 5\n"
                    "  Register, login, complete onboarding."
                )
            await self._begin_run_flow(sess, body, batch_hint=True)
            raise _Reply("")

        if cmd == "abort":
            if sess.pending_plan:
                sess.pending_plan = None
                return "pending plan discarded"
            sess.awaiting = None
            return "nothing pending"

        if cmd == "watch":
            run_id = args[0] if args else (sess.last_run_id or "")
            if not run_id:
                return "usage: /watch <run_id>"
            await self._start_watch(sess, run_id)
            raise _Reply("")

        if cmd == "unwatch":
            run_id = args[0] if args else ""
            stopped = self._stop_watch(sess, run_id or None)
            return f"stopped {stopped} watcher(s)" if stopped else "nothing to stop"

        if cmd == "cancel":
            run_id = args[0] if args else (sess.last_run_id or "")
            if not run_id:
                return "usage: /cancel <run_id>"
            await self._cancel_run(sess, run_id)
            raise _Reply("")

        if cmd == "resume":
            if not args:
                return "usage: /resume <run_id>"
            run_id = args[0]
            data = await self._api("POST", f"/agent/runs/{run_id}/resume")
            sess.last_run_id = run_id
            return f"resuming {run_id}: {data.get('status', 'unknown')}"

        if cmd == "replay":
            if not args:
                return "usage: /replay <run_id> [account_id]"
            run_id = args[0]
            account_id = args[1] if len(args) > 1 else None
            return await self._format_replay(run_id, account_id)

        if cmd == "templates":
            return await self._format_templates()

        if cmd == "runs":
            limit = int(args[0]) if args and args[0].isdigit() else 10
            return await self._format_runs(limit)

        if cmd == "runinfo":
            if not args:
                return "usage: /runinfo <run_id>"
            return await self._format_runinfo(args[0])

        if cmd == "workflows":
            data = await self._api("GET", "/workflows")
            wfs = data.get("workflows", []) or []
            return ("workflows:\n" + "\n".join(f"  - {w}" for w in wfs)) if wfs \
                else "no workflows"

        if cmd == "wf":
            if not args:
                return "usage: /wf <name> [account_id]"
            name = args[0]
            account_id = args[1] if len(args) > 1 else None
            payload: dict[str, Any] = {}
            if account_id:
                payload["account_id"] = account_id
            data = await self._api(
                "POST", f"/workflows/{name}/run", body=payload,
            )
            return (
                f"workflow {name} started"
                + (f" for account {account_id}" if account_id else "")
                + f"\nstatus: {data.get('status', 'unknown')}"
            )

        if cmd == "wfbatch":
            if len(args) < 2:
                return "usage: /wfbatch <name> <account_id1> <account_id2> …"
            name = args[0]
            account_ids = args[1:]
            data = await self._api(
                "POST",
                f"/workflows/{name}/run_for_accounts",
                body={
                    "account_ids": account_ids,
                    "parallel": len(account_ids) > 1,
                    "max_parallel": min(4, len(account_ids)),
                },
            )
            return (
                f"workflow {name} batch started for {len(account_ids)} account(s)"
                f"\nstatus: {data.get('status', 'unknown')}"
            )

        return f"unknown command: /{cmd}\n\n{_HELP}"

    # ------------------------------------------------------------ run flow
    async def _begin_run_flow(
        self, sess: ChatSession, instruction: str, *, batch_hint: bool,
    ) -> None:
        """Build a plan from NL and ask the user to confirm execution."""
        try:
            plan_resp = await self._api(
                "POST", "/agent/plan", body={"instruction": instruction},
            )
        except _ApiError as exc:
            await self._send(sess.chat_id, f"plan failed: {exc}")
            return

        plan = plan_resp.get("plan") or {}
        preview = plan_resp.get("preview") or {}

        # If the user explicitly said "batch", let plan.parallel be true and
        # bump max_parallel accordingly when not already set.
        parallel = bool(plan.get("parallel", False))
        max_parallel = plan.get("max_parallel")
        if batch_hint and not parallel:
            parallel = True
            max_parallel = max_parallel or min(
                int(plan.get("account_config", {}).get("count", 4)) or 4, 4,
            )

        token = secrets.token_hex(4)
        sess.pending_plan = PendingPlan(
            token=token,
            instruction=instruction,
            plan=plan,
            preview=preview,
            parallel=parallel,
            max_parallel=int(max_parallel) if max_parallel else None,
        )

        text = _format_plan_preview(preview, parallel, max_parallel)
        markup = json.dumps({
            "inline_keyboard": [[
                {"text": "Confirm & Run", "callback_data": f"confirm:{token}"},
                {"text": "Cancel", "callback_data": f"cancel:{token}"},
            ]],
        })
        await self._send(sess.chat_id, text, reply_markup=markup)

    async def _send_plan_only(self, sess: ChatSession, instruction: str) -> None:
        try:
            resp = await self._api(
                "POST", "/agent/plan", body={"instruction": instruction},
            )
        except _ApiError as exc:
            await self._send(sess.chat_id, f"plan failed: {exc}")
            return
        preview = resp.get("preview") or {}
        plan = resp.get("plan") or {}
        text = _format_plan_preview(
            preview,
            bool(plan.get("parallel", False)),
            plan.get("max_parallel"),
            footer="Use /run to execute.",
        )
        await self._send(sess.chat_id, text)

    async def _execute_pending(self, sess: ChatSession, pending: PendingPlan) -> None:
        """User confirmed — submit the plan and start a watcher."""
        body = {
            "instruction": pending.instruction,
            "plan": pending.plan,
            "parallel": pending.parallel,
        }
        if pending.max_parallel is not None:
            body["max_parallel"] = pending.max_parallel

        try:
            resp = await self._api("POST", "/agent/run", body=body)
        except _ApiError as exc:
            # Keep the pending plan so the user can /run again or tap Confirm
            # on the same message after fixing the cause (network blip, etc.)
            await self._send(
                sess.chat_id,
                f"failed to start run: {exc}\nplan kept — tap Confirm again to retry",
            )
            return

        # Submission succeeded — burn the token so a stale tap can't double-run.
        if sess.pending_plan is pending:
            sess.pending_plan = None

        run_id = resp.get("run_id") or ""
        accounts = resp.get("accounts") or []
        sess.last_run_id = run_id

        # Inline buttons: tap-to-watch / tap-to-cancel.
        markup = json.dumps({
            "inline_keyboard": [[
                {"text": "Watch", "callback_data": f"watch:{run_id}"},
                {"text": "Cancel run", "callback_data": f"stop:{run_id}"},
            ]],
        })
        msg = (
            f"started {run_id}\n"
            f"accounts: {len(accounts)}\n"
            f"goals:    {resp.get('goals', '?')}\n"
            f"target:   {resp.get('target_url') or '(none)'}\n"
            "tap Watch to stream live progress here."
        )
        await self._send(sess.chat_id, msg, reply_markup=markup)

        # Auto-start the watcher so users get progress without an extra tap.
        await self._start_watch(sess, run_id, announce=False)

    # ----------------------------------------------------------- live watch
    async def _start_watch(
        self, sess: ChatSession, run_id: str, *, announce: bool = True,
    ) -> None:
        if not run_id:
            return
        if run_id in sess.watchers:
            if announce:
                await self._send(sess.chat_id, f"already watching {run_id}")
            return
        ws = WatchSession(run_id=run_id)
        ws.task = asyncio.create_task(
            self._watch_loop(sess, ws),
            name=f"tg-watch-{sess.chat_id}-{run_id}",
        )
        sess.watchers[run_id] = ws
        if announce:
            await self._send(sess.chat_id, f"watching {run_id}")

    def _stop_watch(self, sess: ChatSession, run_id: str | None) -> int:
        """Cancel watcher tasks. Identity-checks the dict pop so a freshly
        spawned watcher is never disowned by an in-flight cancel."""
        targets = list(sess.watchers.keys()) if not run_id else [run_id]
        stopped = 0
        for rid in targets:
            ws = sess.watchers.get(rid)
            if ws is None:
                continue
            # Pop only when the dict still points at the same WatchSession
            # we found — otherwise we'd evict a successor watcher started
            # in the gap between cancel and finally.
            if sess.watchers.get(rid) is ws:
                sess.watchers.pop(rid, None)
            if ws.task and not ws.task.done():
                ws.task.cancel()
                stopped += 1
        return stopped

    async def _cancel_run(self, sess: ChatSession, run_id: str) -> None:
        try:
            resp = await self._api("POST", f"/agent/runs/{run_id}/cancel")
        except _ApiError as exc:
            await self._send(sess.chat_id, f"cancel failed: {exc}")
            return
        await self._send(
            sess.chat_id,
            f"cancel requested for {run_id}: {resp.get('status', 'ok')}",
        )

    async def _watch_loop(self, sess: ChatSession, ws: WatchSession) -> None:
        """Tail the run's events and post compact updates to the chat.

        Buffers events for a short window so chats don't get flooded one
        message per event. Tolerates short bursts of transient API errors
        before giving up. Loop exits when the run reaches a terminal state
        or the task is cancelled.
        """
        run_id = ws.run_id
        terminal = {"completed", "failed", "cancelled"}
        last_post = 0.0
        buffer: list[dict[str, Any]] = []
        consecutive_errors = 0

        try:
            await self._send(sess.chat_id, f"[{run_id}] streaming…")
            while True:
                try:
                    resp = await self._api(
                        "GET",
                        f"/agent/runs/{run_id}/events_tail?cursor={ws.cursor}",
                    )
                except _ApiError as exc:
                    consecutive_errors += 1
                    if consecutive_errors >= _WATCH_MAX_API_FAILURES:
                        await self._send(
                            sess.chat_id,
                            f"[{run_id}] watch giving up after "
                            f"{consecutive_errors} errors: {exc}",
                        )
                        break
                    # Exponential-ish backoff so we don't hammer a flaky API.
                    await asyncio.sleep(_WATCH_POLL_S * consecutive_errors)
                    continue

                consecutive_errors = 0
                ws.cursor = int(resp.get("cursor") or ws.cursor)
                buffer.extend(resp.get("events") or [])

                now = time.time()
                if buffer and (now - last_post) >= _WATCH_BATCH_S:
                    text = _format_event_batch(run_id, buffer)
                    if text:
                        await self._send(sess.chat_id, text)
                    buffer.clear()
                    last_post = now

                status = resp.get("status")
                if status in terminal and not buffer:
                    await self._send(sess.chat_id, f"[{run_id}] done — {status}")
                    break

                await asyncio.sleep(_WATCH_POLL_S)

        except asyncio.CancelledError:
            try:
                await self._send(sess.chat_id, f"[{run_id}] watch stopped")
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            # Identity-checked removal: only evict when this very task is
            # still the registered watcher, not a successor spawned during
            # cancellation.
            if sess.watchers.get(run_id) is ws:
                sess.watchers.pop(run_id, None)

    # --------------------------------------------------------- formatters
    async def _format_runs(self, limit: int) -> str:
        try:
            data = await self._api("GET", f"/agent/runs?limit={limit}")
        except _ApiError as exc:
            return f"runs failed: {exc}"
        active = data.get("active") or {}
        stored = data.get("runs") or []
        lines: list[str] = []
        if active:
            lines.append("active:")
            for rid, info in active.items():
                lines.append(f"  - {rid}  {info.get('status', '?')}")
        if stored:
            lines.append("recent:")
            for r in stored:
                rid = r.get("run_id") or "?"
                st = r.get("status", "?")
                instr = (r.get("instruction") or "").replace("\n", " ")[:60]
                lines.append(f"  - {rid}  {st}  {instr}")
        return "\n".join(lines) or "no runs"

    async def _format_runinfo(self, run_id: str) -> str:
        try:
            data = await self._api("GET", f"/agent/runs/{run_id}")
        except _ApiError as exc:
            return f"runinfo failed: {exc}"
        run = data.get("run") or {}
        active = data.get("active")
        accs = run.get("accounts") or []
        lines = [
            f"run:      {run.get('run_id', run_id)}",
            f"status:   {run.get('status', '?')}",
            f"active:   {bool(active)}",
            f"accounts: {len(accs)}",
            f"goals:    {len(run.get('goals') or [])}",
        ]
        if run.get("instruction"):
            lines.append(f"goal:     {run['instruction'][:200]}")
        if run.get("error"):
            lines.append(f"error:    {run['error'][:200]}")
        return "\n".join(lines)

    async def _format_replay(
        self, run_id: str, account_id: str | None,
    ) -> str:
        if not account_id:
            try:
                run = (await self._api("GET", f"/agent/runs/{run_id}")).get("run") or {}
            except _ApiError as exc:
                return f"replay failed: {exc}"
            accs = run.get("accounts") or []
            if not accs:
                return "no accounts in run"
            account_id = accs[0]
        try:
            data = await self._api(
                "GET", f"/agent/runs/{run_id}/replay/{account_id}",
            )
        except _ApiError as exc:
            return f"replay failed: {exc}"
        replay = data.get("replay") or []
        if not replay:
            return f"no replay records for {account_id}"
        return _format_replay_records(run_id, account_id, replay)

    async def _format_templates(self) -> str:
        try:
            data = await self._api("GET", "/ai/templates")
        except _ApiError as exc:
            return f"templates failed: {exc}"
        if not data.get("enabled", True):
            return "AI memory disabled — no templates"
        templates = data.get("templates") or []
        if not templates:
            return "no learned templates yet"
        stats = data.get("stats") or {}
        lines = [
            "learned templates (replay-first):",
            f"  workflow runs: total={stats.get('total', 0)} "
            f"ok={stats.get('ok', 0)} "
            f"sr={(stats.get('success_rate') or 0):.2f}",
            "",
        ]
        for t in templates[:20]:
            lines.append(
                f"- sig={t.get('page_signature', '?')[:24]} "
                f"title={(t.get('title') or '?')[:40]} "
                f"seen={t.get('seen_count', 0)} "
                f"intents={t.get('intent_count', 0)}"
            )
            for intent, info in (t.get("intents") or {}).items():
                lines.append(
                    f"    {intent:<18} conf={info.get('confidence', 0):.2f} "
                    f"hits={info.get('success_count', 0)}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------- HTTP I/O
    async def _api(
        self, method: str, path: str, body: dict[str, Any] | None = None,
    ) -> dict:
        url = f"{self.api_base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, _http_call, method, url, headers, data,
            )
        except urllib.request.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:500]
            except Exception:  # noqa: BLE001
                detail = ""
            raise _ApiError(f"{e.code} {e.reason} {detail}".strip()) from e
        except Exception as exc:  # noqa: BLE001
            raise _ApiError(str(exc)) from exc

    async def _call(self, method: str, params: dict[str, Any]) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        body = urllib.parse.urlencode(params).encode("utf-8")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _http_call, "POST", url,
            {"Content-Type": "application/x-www-form-urlencoded"}, body,
        )

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: str | None = None,
    ) -> None:
        if not text:
            return
        # Telegram caps at 4096 chars per message; chunk politely.
        chunks = [
            text[i: i + _MAX_TG_CHARS]
            for i in range(0, len(text), _MAX_TG_CHARS)
        ]
        for i, chunk in enumerate(chunks):
            params: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            # Only attach the keyboard to the LAST chunk so it stays usable.
            if reply_markup and i == len(chunks) - 1:
                params["reply_markup"] = reply_markup
            try:
                await self._call("sendMessage", params)
            except Exception:  # noqa: BLE001
                log.exception("telegram send failed")

    async def _answer_callback(
        self, cq_id: str | None, text: str = "", *, alert: bool = False,
    ) -> None:
        if not cq_id:
            return
        try:
            await self._call(
                "answerCallbackQuery",
                {"callback_query_id": cq_id, "text": text, "show_alert": alert},
            )
        except Exception:  # noqa: BLE001
            log.debug("answerCallbackQuery failed")


# ---------------------------------------------------------------- helpers


class _Reply(Exception):
    """Raised by command handlers to short-circuit the default reply.

    Used when the handler has already pushed messages to the chat (e.g. an
    inline keyboard) and the dispatcher should NOT send a follow-up text.
    """

    def __init__(self, text: str = "", markup: str | None = None) -> None:
        super().__init__(text)
        self.text = text
        self.markup = markup


class _ApiError(RuntimeError):
    """Wraps an HTTP error from the local FastAPI."""


def _http_call(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}


# ------------------------------------------------------------- formatters

_HELP = (
    "EXECUTION\n"
    "  /run [text]            plan + confirm + execute\n"
    "  /plan [text]           preview a plan only\n"
    "  /newtask               prompt for an instruction\n"
    "  /batch [text]          multi-account NL with batching\n"
    "  /watch [run_id]        stream live progress\n"
    "  /unwatch [run_id]      stop streaming\n"
    "  /cancel [run_id]       cancel a running execution\n"
    "  /resume <run_id>       resume from checkpoint\n"
    "  /replay <run_id> [acc] show replay summary\n"
    "  /templates             list learned templates\n"
    "  /runs [N]              recent runs\n"
    "  /runinfo <run_id>      details for one run\n"
    "  /wf <name> [acc]       run a workflow file\n"
    "  /wfbatch <name> <ids>  workflow batch run\n"
    "  /abort                 discard a pending plan\n"
    "\n"
    "ENGINE LIFECYCLE\n"
    "  /engine_start          start the framework engine\n"
    "  /stop                  stop the framework engine\n"
    "  /restart               restart the framework engine\n"
    "  /reload                hot-reload config + plugins\n"
    "\n"
    "MONITORING\n"
    "  /status /health /plugins /logs /workers /queue /workflows /ai\n"
    "  /accounts /completed [N] /failed [N] /rejected /reload_accounts\n"
    "\n"
    "TIP\n"
    "  Plain text without a leading slash is treated as a natural-language\n"
    "  instruction and goes through the same plan-and-confirm flow."
)


def _format_plan_preview(
    preview: dict[str, Any],
    parallel: bool,
    max_parallel: int | None,
    *,
    footer: str = "Tap Confirm to execute, or Cancel to discard.",
) -> str:
    instruction = (preview.get("instruction") or "").strip()
    goals = preview.get("goals") or []
    target = preview.get("target_url") or "(none)"
    accounts = preview.get("account_count", 0)
    eta = preview.get("estimated_time_seconds", 0) or 0
    notes = preview.get("notes") or []

    lines = ["execution plan:"]
    if instruction:
        lines.append(f"  goal:        {instruction[:200]}")
    lines.extend([
        f"  target url:  {target}",
        f"  accounts:    {accounts}",
        f"  parallel:    {parallel}"
        + (f" (max {max_parallel})" if parallel and max_parallel else ""),
        f"  estimate:    {int(eta)}s",
        "  steps:",
    ])
    for i, g in enumerate(goals, 1):
        desc = g.get("description") or g.get("type") or f"step {i}"
        lines.append(f"    {i}. {desc}")
    if notes:
        lines.append("  notes:")
        for n in notes:
            lines.append(f"    - {n}")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


def _format_event_batch(run_id: str, events: list[dict[str, Any]]) -> str:
    """Compact, human-readable rollup of a batch of agent events.

    Skips noisy intermediate events (WAITING / WAIT_RESOLVED) unless the
    batch contains nothing else, so chat messages stay scannable.
    """
    high_value = {
        "agent.run.started", "agent.run.completed", "agent.run.failed",
        "agent.run.cancelled", "agent.run.resumed",
        "agent.account.started", "agent.account.completed",
        "agent.account.failed",
        "agent.goal.started", "agent.goal.completed", "agent.goal.failed",
        "agent.recovery.started", "agent.recovery.succeeded",
        "agent.recovery.failed",
        "agent.verification.passed", "agent.verification.failed",
        "agent.ai.decision",
    }
    keep = [e for e in events if e.get("type") in high_value]
    if not keep:
        # Fall back to the raw events so the user always sees *something*.
        keep = events[-3:]

    out = [f"[{run_id}]"]
    for e in keep[-12:]:  # cap a single message at ~12 events
        out.append("  " + _format_event_line(e))
    return "\n".join(out)


def _format_event_line(e: dict[str, Any]) -> str:
    etype = (e.get("type") or "").replace("agent.", "")
    acc = e.get("account_id") or ""
    goal = e.get("goal") or ""
    msg = (e.get("message") or "").replace("\n", " ")
    conf = e.get("confidence") or 0.0
    parts = [etype]
    if acc:
        parts.append(f"acc={acc}")
    if goal:
        parts.append(f"goal={goal}")
    if conf:
        parts.append(f"conf={conf:.2f}")
    if msg and msg.lower() != etype.lower():
        parts.append(f"- {msg[:120]}")
    return " ".join(parts)


def _format_replay_records(
    run_id: str, account_id: str, replay: list[dict[str, Any]],
) -> str:
    lines = [
        f"replay {run_id} / {account_id}",
        f"  records: {len(replay)}",
    ]
    # Surface goal start/end and errors only — full replay can be massive.
    for r in replay[:200]:
        rtype = r.get("type", "?")
        if rtype not in {"goal_start", "goal_end", "error", "navigate", "ai_decision"}:
            continue
        url = (r.get("url") or "")[:80]
        goal = r.get("goal") or ""
        ok = r.get("success", True)
        dur = r.get("duration_ms", 0) or 0
        marker = "+" if ok else "x"
        bits = [f"  {marker} {rtype}"]
        if goal:
            bits.append(f"goal={goal}")
        if url:
            bits.append(f"url={url}")
        if dur:
            bits.append(f"dur={dur}ms")
        if rtype == "error":
            bits.append("err=" + str((r.get("data") or {}).get("error", ""))[:120])
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _format_accounts_status(data: Any) -> str:
    """Compact human-readable status block for /accounts."""
    if not isinstance(data, dict) or not data.get("configured"):
        return "no account manager configured"
    p = data.get("progress", {}) or {}
    lines = [
        f"total:     {p.get('total', 0)}",
        f"pending:   {p.get('pending', 0)}",
        f"running:   {p.get('running', 0)}",
        f"completed: {p.get('completed', 0)}",
        f"failed:    {p.get('failed', 0)}",
        f"skipped:   {p.get('skipped', 0)}",
        f"rejected:  {p.get('rejected', 0)}",
        f"speed:     {p.get('speed_per_minute', 0):.2f} acct/min",
        f"source:    {data.get('source_file', '?')}",
    ]
    return "\n".join(lines)


def _format_account_list(data: Any, label: str) -> str:
    if not isinstance(data, dict):
        return f"no {label} accounts"
    accounts = data.get("accounts", []) or []
    if not accounts:
        return f"no {label} accounts"
    lines = [f"{label}: {data.get('count', len(accounts))}"]
    for a in accounts[:30]:
        ident = a.get("number") or a.get("username") or a.get("email") or a.get("id")
        suffix = ""
        if a.get("attempts"):
            suffix = f" attempts={a['attempts']}"
        if a.get("last_error"):
            suffix += f" err={str(a['last_error'])[:60]}"
        lines.append(f"  - {ident}{suffix}")
    return "\n".join(lines)


def _summary(data: Any, keys: tuple[str, ...] | None = None) -> str:
    if isinstance(data, dict):
        if keys:
            return "\n".join(f"{k}: {data.get(k)}" for k in keys)
        return json.dumps(data, indent=2, default=str)[:_MAX_TG_CHARS]
    return str(data)
