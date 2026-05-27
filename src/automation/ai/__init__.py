"""AI subsystem: single central Brain that perceives, plans, acts, heals, and learns.

The AI layer is fully optional — if disabled in config, the rest of the
framework continues to operate normally.
"""
from __future__ import annotations

from automation.ai.brain import AIBrain
from automation.ai.intents import IntentMatcher, INTENTS
from automation.ai.memory import AIMemory
from automation.ai.perception import PagePerception, PageSnapshot, DetectedElement
from automation.ai.planner import ActionPlanner, ActionPlan, ActionStep
from automation.ai.executor import ActionExecutor
from automation.ai.healer import SelfHealer

__all__ = [
    "AIBrain",
    "IntentMatcher",
    "INTENTS",
    "AIMemory",
    "PagePerception",
    "PageSnapshot",
    "DetectedElement",
    "ActionPlanner",
    "ActionPlan",
    "ActionStep",
    "ActionExecutor",
    "SelfHealer",
]
