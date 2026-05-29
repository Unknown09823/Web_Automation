"""Pluggable LLM interface used by the agent and Telegram bot.

The interface is intentionally small. Backends only have to implement
``chat()``; structured helpers (instruction parsing, goal decomposition,
recovery brainstorming) live in this module and call ``chat()`` with a
canned system prompt + JSON schema.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------- types
@dataclass(slots=True)
class LLMRequest:
    """One chat-completion request."""

    system: str
    user: str
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2048
    json_only: bool = False
    timeout_seconds: float = 60.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResponse:
    """One chat-completion response."""

    text: str
    model: str = ""
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    def parsed_json(self) -> dict[str, Any] | list[Any] | None:
        """Best-effort JSON extraction from the response text.

        Tolerates: pure JSON, JSON wrapped in ```json fences, and JSON with
        prefix/suffix prose. Returns ``None`` if nothing parses.
        """
        text = (self.text or "").strip()
        if not text:
            return None
        # Strip markdown fences
        text = _strip_fences(text)
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Find the first/last brace and try the slice
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start >= 0 and end > start:
                slice_ = text[start:end + 1]
                try:
                    return json.loads(slice_)
                except json.JSONDecodeError:
                    continue
        return None


class LLMError(RuntimeError):
    """Raised by backends when a call fails. Callers can fall back to rules."""


# ----------------------------------------------------------------- protocol
class LLMBackend(Protocol):
    """Backend protocol. Implementations live alongside in this package."""

    name: str
    default_model: str

    async def chat(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        ...

    async def parse_instruction(
        self, instruction: str,
    ) -> dict[str, Any]:  # pragma: no cover
        ...


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` fences if present."""
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


# ---------------------------------------------------------- shared prompts
INSTRUCTION_PARSE_SCHEMA = """
You convert a user's free-form automation instruction into a strict JSON
plan that an autonomous browser agent can execute.

Reply with ONLY a JSON object (no prose, no markdown) matching this schema:

{
  "instruction": <verbatim original text>,
  "target_url": <string, "" if none>,
  "goals": [
    {
      "type":  <one of: "register_account" | "login" | "logout"
                | "complete_task" | "complete_onboarding" | "fill_application"
                | "verify_email" | "download_file" | "upload_file"
                | "purchase_item" | "navigate" | "custom">,
      "description": <short human description>,
      "params": <object — may be empty>
    }
  ],
  "account_config": {
    "count": <int, 1 if not specified>,
    "password": <string or "">,
    "generate_numbers":   <bool>,
    "number_length":      <int, default 10>,
    "generate_emails":    <bool>,
    "generate_usernames": <bool>
  },
  "parallel": <bool>,
  "max_parallel": <int, 1..16>,
  "estimated_time_seconds": <int>,
  "notes": [<string>, ...]
}

Rules:
* If the user mentions a count like "20 accounts", set account_config.count=20
  and parallel=true with max_parallel = min(count, 5).
* If the instruction lists multiple actions ("Register, then login, then
  download report"), produce one goal per action, in the user's order.
* If the URL appears in the text, copy it into target_url AND into
  goals[0].params.url when the first goal is "navigate" or "register_account".
* Never invent credentials. If no password is in the text, leave it empty
  (the runtime data factory generates one).
* Use "custom" for goals you cannot map confidently — the agent will fall
  back to AI perception at runtime.
"""

GOAL_DECOMPOSITION_SCHEMA = """
You break ONE high-level goal into 2..6 ordered sub-steps an autonomous
browser agent can verify independently.

Reply with ONLY a JSON object (no prose, no markdown):

{
  "sub_goals": [
    {
      "type": <one of: "navigate" | "custom" | other GoalType values>,
      "description": <short imperative — "Click submit button">,
      "params": <object — may include {"ai_goal": "..."} hint>,
      "verification_hints": [<phrase to look for to confirm success>]
    },
    ...
  ]
}

Each sub-goal must be a unit of progress whose success can be verified by
URL change, success message, or specific element appearance.
"""

RECOVERY_SCHEMA = """
You are the supervisor of an autonomous browser agent. The agent failed to
complete a goal after several retries. Suggest 2..4 ordered recovery
strategies the agent should try next.

Reply with ONLY a JSON object (no prose, no markdown):

{
  "strategies": [
    {
      "name": <short id like "dismiss_modal" | "scroll_and_retry">,
      "description": <imperative — "Close any popup overlay then retry">,
      "kind": <one of: "click" | "scroll" | "key" | "navigate" | "wait"
                | "replan" | "skip">,
      "params": <object>
    },
    ...
  ]
}

Prefer cheap strategies first (dismiss popups, scroll into view). Use
"replan" only when the page state is clearly unexpected.
"""


__all__ = [
    "LLMRequest", "LLMResponse", "LLMError", "LLMBackend",
    "INSTRUCTION_PARSE_SCHEMA", "GOAL_DECOMPOSITION_SCHEMA", "RECOVERY_SCHEMA",
]
