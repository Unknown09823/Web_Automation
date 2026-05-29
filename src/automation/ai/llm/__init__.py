"""LLM clients for the autonomous browser agent.

The framework speaks to Large Language Models through the small interface
defined in :mod:`automation.ai.llm.base`. The default backend is
:class:`automation.ai.llm.groq.GroqClient` (Qwen models on Groq), but any
class implementing :class:`LLMBackend` can be plugged in via the env
variable ``AI__PROVIDER`` (groq | openrouter | openai | anthropic).

The LLM is responsible for:

* Parsing free-form Telegram instructions into structured execution plans
* Decomposing fuzzy goals (``"complete the onboarding"``) into the agent's
  goal taxonomy when the built-in templates don't cover them
* Suggesting recovery strategies when a goal repeatedly fails

The LLM never drives the browser directly — Playwright actions remain
deterministic. The LLM produces *structured JSON* the agent then executes.
"""
from automation.ai.llm.base import LLMBackend, LLMRequest, LLMResponse, LLMError
from automation.ai.llm.factory import build_llm_from_settings

__all__ = [
    "LLMBackend", "LLMRequest", "LLMResponse", "LLMError",
    "build_llm_from_settings",
]
