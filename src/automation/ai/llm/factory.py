"""LLM factory.

Picks an :class:`LLMBackend` from the unified :class:`Settings`. Order of
preference:

  1. The explicit ``ai.provider`` value from ``master_config.json``
     (default: ``groq``).
  2. If that provider has no key, fall through to the next one with a key:
     groq → openrouter → openai → anthropic.
  3. If nothing is configured, return ``None`` (the agent's regex fallback
     in :class:`NLPlanner` keeps working without any LLM).
"""
from __future__ import annotations

import logging
from typing import Any

from automation.ai.llm.base import LLMBackend, LLMError
from automation.ai.llm.groq import GROQ_BASE_URL, GroqClient

log = logging.getLogger(__name__)


_PROVIDER_ENDPOINTS: dict[str, tuple[str, str]] = {
    # provider_name -> (base_url, api_key_env)
    "groq":       (GROQ_BASE_URL,                                "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
    "openai":     ("https://api.openai.com/v1/chat/completions",   "OPENAI_API_KEY"),
}

_PROVIDER_DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    # provider -> (planner_model, reasoning_model)
    "groq":       ("qwen/qwen3-32b",   "qwen-qwq-32b"),
    "openrouter": ("qwen/qwen3-32b",   "qwen/qwen3-32b"),
    "openai":     ("gpt-4o-mini",      "gpt-4o-mini"),
}


def build_llm_from_settings(settings: Any) -> LLMBackend | None:
    """Build the active LLM client. Returns ``None`` if nothing configured."""
    preferred = (settings.get("ai.provider") or "groq").lower().strip()
    candidates = [preferred] + [
        p for p in ("groq", "openrouter", "openai") if p != preferred
    ]

    timeout = float(settings.get("ai.request_timeout_seconds", 60))
    planner_model = settings.get("DEFAULT_PLANNER_MODEL")
    reasoning_model = settings.get("DEFAULT_REASONING_MODEL")

    for provider in candidates:
        if provider not in _PROVIDER_ENDPOINTS:
            continue
        base_url, env_var = _PROVIDER_ENDPOINTS[provider]
        api_key = settings.get_secret(env_var)
        if not api_key:
            continue
        default_planner, default_reasoning = _PROVIDER_DEFAULT_MODELS[provider]
        try:
            client = GroqClient(  # OpenAI-compatible class re-used here
                api_key=api_key,
                default_model=planner_model or default_planner,
                reasoning_model=reasoning_model or default_reasoning,
                base_url=base_url,
                request_timeout_seconds=timeout,
                provider_name=provider,
            )
        except LLMError:
            log.exception("could not build %s client", provider)
            continue
        log.info(
            "LLM backend = %s (planner=%s, reasoning=%s)",
            provider, client.default_model, client.reasoning_model,
        )
        return client

    log.info("No LLM provider configured — using rule-based fallback only")
    return None


__all__ = ["build_llm_from_settings"]
