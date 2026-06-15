"""Groq backend.

Groq exposes an OpenAI-compatible Chat Completions endpoint at
``https://api.groq.com/openai/v1/chat/completions``. We use it for the
default planner and reasoning paths with Qwen models (``qwen/qwen3-32b``
and ``qwen-qwq-32b``). No SDK dependency — plain ``urllib`` requests run
inside an executor so the rest of the asyncio loop is not blocked.

Other OpenAI-compatible providers (OpenRouter, OpenAI itself) reuse this
class with just a different base URL + auth header. See ``factory.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from automation.ai.llm.base import (
    GOAL_DECOMPOSITION_SCHEMA,
    INSTRUCTION_PARSE_SCHEMA,
    LLMBackend,
    LLMError,
    LLMRequest,
    LLMResponse,
    RECOVERY_SCHEMA,
)

log = logging.getLogger(__name__)


GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqClient(LLMBackend):
    """Groq Chat Completions client (OpenAI-compatible).

    Designed to also serve OpenRouter / OpenAI / any other OpenAI-compatible
    endpoint by passing a different ``base_url`` + ``api_key``.
    """

    name = "groq"

    def __init__(
        self,
        api_key: str,
        *,
        default_model: str = "qwen/qwen3-32b",
        reasoning_model: str = "qwen-qwq-32b",
        base_url: str = GROQ_BASE_URL,
        request_timeout_seconds: float = 60.0,
        provider_name: str = "groq",
    ) -> None:
        if not api_key:
            raise LLMError(f"{provider_name} client requires an API key")
        self.api_key = api_key
        self.default_model = default_model
        self.reasoning_model = reasoning_model
        self.base_url = base_url
        self.request_timeout_seconds = request_timeout_seconds
        self.name = provider_name

    # ------------------------------------------------------------- chat
    async def chat(self, request: LLMRequest) -> LLMResponse:
        """Run one chat completion."""
        model = request.model or self.default_model
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": float(request.temperature),
            "max_tokens": int(request.max_tokens),
        }
        if request.json_only:
            # OpenAI-compatible providers (Groq, OpenAI, OpenRouter) accept
            # this hint; if the model doesn't honor it we still parse the
            # response defensively.
            body["response_format"] = {"type": "json_object"}
        body.update(request.extra or {})

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = json.dumps(body).encode("utf-8")
        timeout = float(request.timeout_seconds or self.request_timeout_seconds)

        loop = asyncio.get_running_loop()
        started = time.time()
        try:
            data = await loop.run_in_executor(
                None, _http_post, self.base_url, headers, payload, timeout,
            )
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(
                f"{self.name} HTTP {exc.code}: {body_text}",
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"{self.name} request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError(f"{self.name} request timed out") from exc

        elapsed_ms = int((time.time() - started) * 1000)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content", "") or "",
            model=data.get("model", model),
            finish_reason=choice.get("finish_reason", ""),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            raw=data,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------- structured calls
    async def parse_instruction(self, instruction: str) -> dict[str, Any]:
        """Telegram NL → :class:`ExecutionPlan`-compatible dict."""
        prompt = (
            "User instruction:\n"
            + (instruction or "").strip()
            + "\n\nReply with the JSON plan."
        )
        resp = await self.chat(LLMRequest(
            system=INSTRUCTION_PARSE_SCHEMA,
            user=prompt,
            json_only=True,
            temperature=0.1,
        ))
        result = resp.parsed_json()
        if not isinstance(result, dict):
            raise LLMError(
                f"{self.name} returned non-JSON parse output: "
                f"{(resp.text or '')[:200]}",
            )
        return result

    async def decompose_goal(
        self,
        goal_type: str,
        description: str,
        site_url: str = "",
        recent_failures: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Used when built-in templates don't cover a fuzzy goal."""
        recent_failures = recent_failures or []
        user = (
            f"Goal type: {goal_type}\n"
            f"Description: {description}\n"
            f"Site URL: {site_url or 'unknown'}\n"
        )
        if recent_failures:
            user += "Recent failures: " + "; ".join(recent_failures[:5]) + "\n"
        user += "\nReply with the JSON sub-goal list."

        resp = await self.chat(LLMRequest(
            system=GOAL_DECOMPOSITION_SCHEMA,
            user=user,
            json_only=True,
            temperature=0.2,
            model=self.reasoning_model,
        ))
        result = resp.parsed_json()
        if not isinstance(result, dict) or "sub_goals" not in result:
            raise LLMError(
                f"{self.name} returned malformed decomposition: "
                f"{(resp.text or '')[:200]}",
            )
        return list(result.get("sub_goals", []) or [])

    async def suggest_recovery(
        self,
        *,
        goal: str,
        last_error: str,
        url: str = "",
        page_signature: str = "",
        recent_attempts: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Used by the supervisor when retries are exhausted."""
        recent_attempts = recent_attempts or []
        user = (
            f"Goal: {goal}\n"
            f"URL: {url}\n"
            f"Page signature: {page_signature}\n"
            f"Last error: {last_error}\n"
        )
        if recent_attempts:
            user += "Strategies already tried: " + ", ".join(recent_attempts) + "\n"
        user += "\nReply with the JSON strategy list."

        resp = await self.chat(LLMRequest(
            system=RECOVERY_SCHEMA,
            user=user,
            json_only=True,
            temperature=0.3,
            model=self.reasoning_model,
        ))
        result = resp.parsed_json()
        if not isinstance(result, dict):
            raise LLMError(
                f"{self.name} returned malformed recovery: "
                f"{(resp.text or '')[:200]}",
            )
        return list(result.get("strategies", []) or [])


# ---------------------------------------------------------------------- HTTP
def _http_post(
    url: str,
    headers: dict[str, str],
    payload: bytes,
    timeout: float,
) -> dict[str, Any]:
    """Synchronous POST run inside an executor."""
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"non-JSON response: {text[:300]}") from exc


__all__ = ["GroqClient"]
