"""Agent event types published to the framework EventBus.

Every significant state transition emits an event so the dashboard,
API SSE stream, and plugins can observe the agent in real time.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentEventType(str, Enum):
    # Lifecycle
    RUN_STARTED = "agent.run.started"
    RUN_COMPLETED = "agent.run.completed"
    RUN_FAILED = "agent.run.failed"
    RUN_RESUMED = "agent.run.resumed"
    RUN_CANCELLED = "agent.run.cancelled"

    # Goal-level
    GOAL_STARTED = "agent.goal.started"
    GOAL_COMPLETED = "agent.goal.completed"
    GOAL_FAILED = "agent.goal.failed"

    # Step-level
    STEP_STARTED = "agent.step.started"
    STEP_COMPLETED = "agent.step.completed"
    STEP_FAILED = "agent.step.failed"

    # Waiting
    WAITING = "agent.waiting"
    WAIT_RESOLVED = "agent.wait.resolved"

    # Recovery
    RECOVERY_STARTED = "agent.recovery.started"
    RECOVERY_SUCCEEDED = "agent.recovery.succeeded"
    RECOVERY_FAILED = "agent.recovery.failed"

    # Verification
    VERIFICATION_STARTED = "agent.verification.started"
    VERIFICATION_PASSED = "agent.verification.passed"
    VERIFICATION_FAILED = "agent.verification.failed"

    # AI Decision
    AI_DECISION = "agent.ai.decision"
    AI_REASONING = "agent.ai.reasoning"

    # Checkpoint
    CHECKPOINT_SAVED = "agent.checkpoint.saved"
    CHECKPOINT_RESTORED = "agent.checkpoint.restored"

    # Account-level
    ACCOUNT_STARTED = "agent.account.started"
    ACCOUNT_COMPLETED = "agent.account.completed"
    ACCOUNT_FAILED = "agent.account.failed"


@dataclass(slots=True)
class AgentEvent:
    """A structured agent event with full context for replay and dashboard."""

    type: AgentEventType
    run_id: str
    account_id: str | None = None
    goal: str | None = None
    step: str | None = None
    message: str = ""
    confidence: float = 0.0
    retry_count: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "run_id": self.run_id,
            "account_id": self.account_id,
            "goal": self.goal,
            "step": self.step,
            "message": self.message,
            "confidence": self.confidence,
            "retry_count": self.retry_count,
            "data": self.data,
            "timestamp": self.timestamp,
        }

    def to_sse(self) -> str:
        """Format as a Server-Sent Event data line."""
        import json
        return f"data: {json.dumps(self.to_dict())}\n\n"
