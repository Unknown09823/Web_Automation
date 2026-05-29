"""Autonomous Browser Agent — goal-driven, adaptive, resumable.

This package transforms the framework from a step-by-step workflow runner
into a persistent AI-driven browser agent that thinks in goals, waits
intelligently, recovers from failures, resumes interrupted work, and
completes long multi-stage tasks without rigid scripting.

Public surface:
  * ``BrowserAgent`` — the outer loop (agent.py)
  * ``AgentGoal`` / ``GoalDecomposer`` — goal definitions (goals.py)
  * ``AdaptiveWaiter`` — intelligent waiting (waiter.py)
  * ``RunContext`` — execution memory / run folder (run.py)
  * ``CheckpointManager`` — resume from last success (checkpoints.py)
  * ``ActionRecorder`` — replay ledger (recorder.py)
  * ``SuccessVerifier`` — multi-signal verification (verifier.py)
  * ``RecoveryStack`` — stacked error recovery (recovery.py)
  * ``AgentEvent`` — event types for EventBus (events.py)
  * ``DataFactory`` — account generation (data_factory.py)
  * ``NLPlanner`` — natural language → plan (nl_planner.py)
"""
from __future__ import annotations

from automation.agent.events import AgentEvent, AgentEventType
from automation.agent.goals import AgentGoal, GoalDecomposer, GoalType
from automation.agent.run import RunContext

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "AgentGoal",
    "GoalDecomposer",
    "GoalType",
    "RunContext",
]
