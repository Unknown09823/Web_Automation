"""Goal definitions and decomposition.

High-level goals (register_account, login, complete_task, download_file, etc.)
are decomposed into ordered sub-goals. The agent works through sub-goals
sequentially, using AI perception + planning at each stage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class GoalType(str, Enum):
    REGISTER_ACCOUNT = "register_account"
    LOGIN = "login"
    COMPLETE_TASK = "complete_task"
    DOWNLOAD_FILE = "download_file"
    UPLOAD_FILE = "upload_file"
    PURCHASE_ITEM = "purchase_item"
    FILL_APPLICATION = "fill_application"
    NAVIGATE = "navigate"
    VERIFY_EMAIL = "verify_email"
    COMPLETE_ONBOARDING = "complete_onboarding"
    LOGOUT = "logout"
    CUSTOM = "custom"

    # ----- v2: deterministic-first goals --------------------------------
    # Each value here is the same string the deterministic engine's
    # GOAL_SHAPES dict uses, so a goal of these types decomposes into a
    # single CUSTOM sub-goal whose ``ai_goal`` parameter is just the
    # value string. That keeps the agent loop unchanged while letting
    # NL planners and Telegram users address richer concepts directly.

    OPEN_PAGE = "open_page"            # navigate to a named page on the host
    CLICK_INTENT = "click_intent"      # click any element matching params.intent
    CUSTOM_STEP = "custom_step"        # explicit alias of CUSTOM, easier to read
    CLAIM_REWARD = "claim_reward"
    OPEN_REWARDS_PAGE = "open_rewards_page"
    OPEN_BETTING_PAGE = "open_betting_page"
    PLACE_BET = "place_bet"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"


class GoalStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class AgentGoal:
    """A single goal in an execution plan."""

    type: GoalType
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    status: GoalStatus = GoalStatus.PENDING
    sub_goals: list["AgentGoal"] = field(default_factory=list)
    max_retries: int = 3
    timeout_seconds: float = 120.0
    verification_hints: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "description": self.description,
            "params": self.params,
            "status": self.status.value,
            "sub_goals": [g.to_dict() for g in self.sub_goals],
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "verification_hints": self.verification_hints,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentGoal":
        return cls(
            type=GoalType(data.get("type", "custom")),
            description=data.get("description", ""),
            params=data.get("params", {}),
            status=GoalStatus(data.get("status", "pending")),
            sub_goals=[cls.from_dict(g) for g in data.get("sub_goals", [])],
            max_retries=int(data.get("max_retries", 3)),
            timeout_seconds=float(data.get("timeout_seconds", 120.0)),
            verification_hints=data.get("verification_hints", []),
            error=data.get("error"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )


# --------------------------------------------------------------------------
# Goal decomposition templates
# --------------------------------------------------------------------------

_DECOMPOSITION: dict[GoalType, list[dict[str, Any]]] = {
    GoalType.REGISTER_ACCOUNT: [
        {"type": "navigate", "description": "Open registration page",
         "verification_hints": ["registration form visible", "signup page loaded"]},
        {"type": "custom", "description": "Fill registration form",
         "params": {"ai_goal": "register"},
         "verification_hints": ["form fields filled"]},
        {"type": "custom", "description": "Submit registration",
         "params": {"ai_goal": "submit"},
         "verification_hints": ["success message", "redirect to dashboard",
                                "welcome page", "account created"]},
    ],
    GoalType.LOGIN: [
        {"type": "navigate", "description": "Open login page",
         "verification_hints": ["login form visible", "signin page loaded"]},
        {"type": "custom", "description": "Fill login credentials",
         "params": {"ai_goal": "login"},
         "verification_hints": ["credentials entered"]},
        {"type": "custom", "description": "Submit login",
         "params": {"ai_goal": "submit"},
         "verification_hints": ["dashboard loaded", "logged in",
                                "welcome back", "profile visible"]},
    ],
    GoalType.DOWNLOAD_FILE: [
        {"type": "navigate", "description": "Navigate to download page",
         "verification_hints": ["download link visible", "download button present"]},
        {"type": "custom", "description": "Initiate download",
         "params": {"ai_goal": "download"},
         "verification_hints": ["download started"]},
        {"type": "custom", "description": "Wait for download completion",
         "params": {"wait_for": "download_complete"},
         "verification_hints": ["file downloaded", "download complete"]},
    ],
    GoalType.UPLOAD_FILE: [
        {"type": "navigate", "description": "Navigate to upload page",
         "verification_hints": ["upload area visible", "file input present"]},
        {"type": "custom", "description": "Select and upload file",
         "params": {"ai_goal": "upload"},
         "verification_hints": ["file uploaded", "upload complete",
                                "success message"]},
    ],
    GoalType.COMPLETE_TASK: [
        {"type": "navigate", "description": "Navigate to task section",
         "verification_hints": ["task page loaded"]},
        {"type": "custom", "description": "Identify and complete task",
         "params": {"ai_goal": "complete_task"},
         "verification_hints": ["task completed", "success"]},
    ],
    GoalType.FILL_APPLICATION: [
        {"type": "navigate", "description": "Open application form",
         "verification_hints": ["application form visible"]},
        {"type": "custom", "description": "Fill all form fields",
         "params": {"ai_goal": "fill_form"},
         "verification_hints": ["fields filled"]},
        {"type": "custom", "description": "Submit application",
         "params": {"ai_goal": "submit"},
         "verification_hints": ["application submitted", "confirmation"]},
    ],
    GoalType.PURCHASE_ITEM: [
        {"type": "navigate", "description": "Navigate to product page",
         "verification_hints": ["product visible", "add to cart button"]},
        {"type": "custom", "description": "Add to cart",
         "params": {"ai_goal": "add_to_cart"},
         "verification_hints": ["added to cart", "cart updated"]},
        {"type": "custom", "description": "Complete checkout",
         "params": {"ai_goal": "checkout"},
         "verification_hints": ["order placed", "purchase complete"]},
    ],
    GoalType.COMPLETE_ONBOARDING: [
        {"type": "custom", "description": "Complete onboarding steps",
         "params": {"ai_goal": "continue"},
         "verification_hints": ["onboarding complete", "dashboard loaded",
                                "skip", "done"]},
    ],
    GoalType.VERIFY_EMAIL: [
        {"type": "custom", "description": "Check for verification email",
         "params": {"ai_goal": "verify_email"},
         "verification_hints": ["email verified", "verification complete"]},
    ],
    GoalType.LOGOUT: [
        {"type": "custom", "description": "Logout from current session",
         "params": {"ai_goal": "logout"},
         "verification_hints": ["logged out", "login page", "signed out"]},
    ],

    # ------------------------------------------------------- v2 atomic goals
    # These are *one-step* goals: the deterministic engine's GOAL_SHAPES
    # already knows how to find and click the right element. Decomposing
    # them as a single CUSTOM sub-goal keeps the agent loop signature
    # unchanged while routing the work through the new stack.
    GoalType.CLAIM_REWARD: [
        {"type": "custom", "description": "Claim reward / gift / bonus",
         "params": {"ai_goal": "claim_reward"},
         "verification_hints": [
             "claimed", "reward credited", "bonus added", "thank you",
             "successfully claimed", "added to your account",
         ]},
    ],
    GoalType.OPEN_REWARDS_PAGE: [
        {"type": "custom", "description": "Open the rewards / promotions page",
         "params": {"ai_goal": "open_rewards_page"},
         "verification_hints": [
             "rewards", "promotions", "bonuses", "offers", "loyalty",
         ]},
    ],
    GoalType.OPEN_BETTING_PAGE: [
        {"type": "custom", "description": "Open the betting / casino lobby",
         "params": {"ai_goal": "open_betting_page"},
         "verification_hints": [
             "sports", "casino", "lobby", "in-play", "live betting",
         ]},
    ],
    GoalType.PLACE_BET: [
        {"type": "custom", "description": "Place a bet",
         "params": {"ai_goal": "place_bet"},
         "verification_hints": [
             "bet placed", "bet confirmed", "wager accepted",
             "thank you for your bet",
         ]},
    ],
    GoalType.DEPOSIT: [
        {"type": "custom", "description": "Open the deposit flow",
         "params": {"ai_goal": "deposit"},
         "verification_hints": [
             "deposit", "add funds", "amount", "payment method",
         ]},
    ],
    GoalType.WITHDRAW: [
        {"type": "custom", "description": "Open the withdrawal flow",
         "params": {"ai_goal": "withdraw"},
         "verification_hints": [
             "withdraw", "withdrawal", "cashout", "amount to withdraw",
         ]},
    ],
    # OPEN_PAGE and CLICK_INTENT are configurable atomic goals: the
    # ``ai_goal`` is taken from goal.params at execution time so the NL
    # planner can synthesize ad-hoc clicks for unknown buttons.
    GoalType.OPEN_PAGE: [
        {"type": "custom",
         "description": "Open the named page",
         "params": {},  # ai_goal supplied by the parent goal's params
         "verification_hints": []},
    ],
    GoalType.CLICK_INTENT: [
        {"type": "custom",
         "description": "Click element matching the given intent",
         "params": {},
         "verification_hints": []},
    ],
    GoalType.CUSTOM_STEP: [
        {"type": "custom",
         "description": "Custom step",
         "params": {},
         "verification_hints": []},
    ],
}


class GoalDecomposer:
    """Decompose high-level goals into ordered sub-goals.

    Uses built-in templates for known goal types and passes custom goals
    through as-is. The AI brain handles the actual execution of each sub-goal.
    """

    def decompose(self, goal: AgentGoal) -> list[AgentGoal]:
        """Return ordered sub-goals for the given goal.

        If the goal already has sub_goals defined, returns those.
        Otherwise uses the built-in template for known types.
        """
        if goal.sub_goals:
            return goal.sub_goals

        template = _DECOMPOSITION.get(goal.type)
        if template is None:
            # Custom or navigate: treat as a single atomic goal
            return [goal]

        sub_goals: list[AgentGoal] = []
        for entry in template:
            sg = AgentGoal(
                type=GoalType(entry["type"]),
                description=entry.get("description", ""),
                params={**goal.params, **entry.get("params", {})},
                verification_hints=entry.get("verification_hints", []),
                max_retries=goal.max_retries,
                timeout_seconds=goal.timeout_seconds,
            )
            sub_goals.append(sg)
        return sub_goals

    def build_plan(self, goals: list[AgentGoal]) -> list[AgentGoal]:
        """Flatten a list of high-level goals into an ordered execution plan.

        Each goal is decomposed and its sub-goals are appended in order.
        """
        plan: list[AgentGoal] = []
        for goal in goals:
            sub = self.decompose(goal)
            plan.extend(sub)
        return plan
