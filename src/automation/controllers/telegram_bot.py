"""Telegram controller.

Polls the Telegram Bot API (no third-party SDK required — uses ``urllib``)
and forwards authorized commands to the local FastAPI backend.

Configuration (any of):
  - env ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_ALLOWED_CHAT_IDS`` (comma-separated)
  - config keys ``telegram.token`` and ``telegram.allowed_chat_ids`` (list[int])

Authorization: only chat IDs in the allow-list may issue commands. Unknown
senders are ignored silently and logged.

Supported commands:
  /start /stop /restart /status /reload /health /plugins /logs /workers
  /accounts /queue /workflows /ai
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


class TelegramController:
    """Long-polling Telegram bot that forwards commands to the local API."""

    def __init__(
        self,
        token: str,
        allowed_chat_ids: list[int],
        api_base_url: str = "http://127.0.0.1:8080",
        api_token: str | None = None,
    ) -> None:
        self.token = token
        self.allowed = set(int(x) for x in allowed_chat_ids if str(x).strip())
        self.api_base_url = api_base_url.rstrip("/")
        self.api_token = api_token
        self._offset = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

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
        log.info("Telegram controller started (allowed chats: %s)", sorted(self.allowed))

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    # ------------------------------------------------------------------- I/O
    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self._call(
                    "getUpdates", {"timeout": 25, "offset": self._offset}
                )
                for u in updates.get("result", []):
                    self._offset = u["update_id"] + 1
                    await self._handle_update(u)
            except Exception:  # noqa: BLE001
                log.exception("telegram poll error")
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if self.allowed and chat_id not in self.allowed:
            log.warning("telegram: rejecting unauthorized chat_id=%s", chat_id)
            return
        try:
            reply = await self._dispatch(text)
        except Exception as exc:  # noqa: BLE001
            reply = f"error: {exc}"
        await self._send(chat_id, reply)

    async def _dispatch(self, text: str) -> str:
        # split on whitespace; first token is /command
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@", 1)[0]
        args = parts[1:]
        if cmd in {"start"}:
            return _summary(await self._api("POST", "/control/start"))
        if cmd in {"stop"}:
            return _summary(await self._api("POST", "/control/stop"))
        if cmd in {"restart"}:
            return _summary(await self._api("POST", "/control/restart"))
        if cmd in {"reload"}:
            return _summary(await self._api("POST", "/control/reload"))
        if cmd in {"status"}:
            return _summary(await self._api("GET", "/status"), keys=("running", "status"))
        if cmd in {"health"}:
            return _summary(await self._api("GET", "/health"))
        if cmd in {"plugins"}:
            data = await self._api("GET", "/plugins")
            lines = [
                f"{p['name']} v{p.get('metadata', {}).get('version','?')} "
                f"enabled={p['enabled']} started={p['started']}"
                for p in data.get("plugins", [])
            ]
            return "\n".join(lines) or "no plugins"
        if cmd in {"logs"}:
            name = args[0] if args else "activity"
            data = await self._api("GET", f"/logs/{name}?lines=20")
            return "\n".join(data.get("lines", []))[-3500:] or "(empty)"
        if cmd in {"workers", "queue"}:
            data = await self._api("GET", "/status")
            return json.dumps({
                "scheduler": data.get("scheduler"),
                "queues": data.get("queues"),
            }, indent=2)
        if cmd in {"accounts"}:
            data = await self._api("GET", "/accounts")
            return json.dumps(data.get("stats", {}), indent=2)
        if cmd in {"workflows"}:
            data = await self._api("GET", "/workflows")
            return "\n".join(data.get("workflows", [])) or "no workflows"
        if cmd in {"ai"}:
            data = await self._api("GET", "/ai/status")
            return json.dumps(data, indent=2, default=str)
        if cmd in {"help", "?"}:
            return _HELP
        return f"unknown command: /{cmd}\n\n{_HELP}"

    # ------------------------------------------------------------- HTTP utils
    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.api_base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _http_call, method, url, headers, data)

    async def _call(self, method: str, params: dict[str, Any]) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        body = urllib.parse.urlencode(params).encode("utf-8")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _http_call, "POST", url,
            {"Content-Type": "application/x-www-form-urlencoded"}, body,
        )

    async def _send(self, chat_id: int, text: str) -> None:
        # Telegram has a 4096 char limit; chunk politely.
        for chunk in [text[i:i + 3500] for i in range(0, len(text) or 1, 3500)]:
            try:
                await self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
            except Exception:  # noqa: BLE001
                log.exception("telegram send failed")


_HELP = (
    "Commands:\n"
    "/start /stop /restart /reload /status /health\n"
    "/plugins /logs [activity|error|debug]\n"
    "/accounts /workers /queue /workflows /ai"
)


def _summary(data: Any, keys: tuple[str, ...] | None = None) -> str:
    if isinstance(data, dict):
        if keys:
            return "\n".join(f"{k}: {data.get(k)}" for k in keys)
        return json.dumps(data, indent=2, default=str)[:3500]
    return str(data)


def _http_call(method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
