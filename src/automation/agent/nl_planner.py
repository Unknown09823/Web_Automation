"""Natural Language → Execution Plan.

Phase 1: Rule-based parser that extracts goals, account generation
parameters, and execution settings from plain English instructions.

Phase 2 (stub): LLM backend interface for richer NL understanding.

Example inputs:
  "Create 20 accounts on https://example.com/signup with random 10-digit
   numbers and password Test@123. After registration, login and complete
   onboarding."

Output: An ExecutionPlan with goals, account generation config, and settings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from automation.agent.goals import AgentGoal, GoalType

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AccountGenConfig:
    """Configuration for account generation extracted from NL."""

    count: int = 1
    password: str = ""
    generate_numbers: bool = False
    number_length: int = 10
    number_prefix: str = ""
    generate_emails: bool = False
    email_domain: str = "example.com"
    generate_usernames: bool = False
    username_prefix: str = "user_"
    manual_data: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "password": self.password,
            "generate_numbers": self.generate_numbers,
            "number_length": self.number_length,
            "number_prefix": self.number_prefix,
            "generate_emails": self.generate_emails,
            "email_domain": self.email_domain,
            "generate_usernames": self.generate_usernames,
            "username_prefix": self.username_prefix,
            "manual_data": self.manual_data,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountGenConfig":
        return cls(
            count=int(data.get("count", 1)),
            password=str(data.get("password", "")),
            generate_numbers=bool(data.get("generate_numbers", False)),
            number_length=int(data.get("number_length", 10)),
            number_prefix=str(data.get("number_prefix", "")),
            generate_emails=bool(data.get("generate_emails", False)),
            email_domain=str(data.get("email_domain", "example.com")),
            generate_usernames=bool(data.get("generate_usernames", False)),
            username_prefix=str(data.get("username_prefix", "user_")),
            manual_data=list(data.get("manual_data", []) or []),
        )


@dataclass(slots=True)
class ExecutionPlan:
    """A complete plan generated from natural language."""

    instruction: str
    goals: list[AgentGoal]
    account_config: AccountGenConfig
    target_url: str = ""
    parallel: bool = False
    max_parallel: int = 4
    estimated_time_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "goals": [g.to_dict() for g in self.goals],
            "account_config": self.account_config.to_dict(),
            "target_url": self.target_url,
            "parallel": self.parallel,
            "max_parallel": self.max_parallel,
            "estimated_time_seconds": self.estimated_time_seconds,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionPlan":
        return cls(
            instruction=str(data.get("instruction", "")),
            goals=[AgentGoal.from_dict(g) for g in data.get("goals", [])],
            account_config=AccountGenConfig.from_dict(
                data.get("account_config", {}) or {},
            ),
            target_url=str(data.get("target_url", "")),
            parallel=bool(data.get("parallel", False)),
            max_parallel=int(data.get("max_parallel", 4)),
            estimated_time_seconds=float(data.get("estimated_time_seconds", 0.0)),
            notes=list(data.get("notes", []) or []),
        )


def _pretty_keyword(keyword: str) -> str:
    """Format a goal keyword as a human-readable description."""
    return keyword.replace("_", " ").title()



# --------------------------------------------------------------------------
# LLM Backend interface (stub for Phase 2)
# --------------------------------------------------------------------------

class LLMBackend(Protocol):
    """Interface for pluggable LLM backends (OpenAI, Anthropic, local, etc.)"""

    async def parse_instruction(self, instruction: str) -> dict[str, Any]:
        """Parse a natural language instruction into structured data."""
        ...


# --------------------------------------------------------------------------
# Rule-based NL Parser (Phase 1 — no external dependencies)
# --------------------------------------------------------------------------

# Patterns for extracting numbers
_COUNT_PATTERNS = [
    re.compile(r"create\s+(\d+)\s+accounts?", re.I),
    re.compile(r"register\s+(\d+)\s+(?:users?|accounts?)", re.I),
    re.compile(r"(\d+)\s+accounts?", re.I),
    re.compile(r"(\d+)\s+users?", re.I),
    re.compile(r"sign\s*up\s+(\d+)", re.I),
]

_URL_PATTERN = re.compile(r"https?://[^\s,\"'<>]+", re.I)

_PASSWORD_PATTERNS = [
    re.compile(r"password[:\s]+([^\s,\"']+)", re.I),
    re.compile(r"pass(?:word)?[:\s]+([^\s,\"']+)", re.I),
    re.compile(r"pwd[:\s]+([^\s,\"']+)", re.I),
]

# Trailing punctuation to strip from regex captures
_TRAIL_PUNCT = ".,;:!?)"

_NUMBER_LENGTH_PATTERNS = [
    re.compile(r"(\d+)[\s-]*digit\s+(?:mobile|phone|number)", re.I),
    re.compile(r"(?:mobile|phone|number).*?(\d+)[\s-]*digit", re.I),
]

_GOAL_KEYWORDS: dict[str, GoalType] = {
    "register": GoalType.REGISTER_ACCOUNT,
    "sign up": GoalType.REGISTER_ACCOUNT,
    "signup": GoalType.REGISTER_ACCOUNT,
    "create account": GoalType.REGISTER_ACCOUNT,
    "login": GoalType.LOGIN,
    "log in": GoalType.LOGIN,
    "sign in": GoalType.LOGIN,
    "download": GoalType.DOWNLOAD_FILE,
    "upload": GoalType.UPLOAD_FILE,
    "purchase": GoalType.PURCHASE_ITEM,
    "buy": GoalType.PURCHASE_ITEM,
    "fill application": GoalType.FILL_APPLICATION,
    "apply": GoalType.FILL_APPLICATION,
    "onboarding": GoalType.COMPLETE_ONBOARDING,
    "complete onboarding": GoalType.COMPLETE_ONBOARDING,
    "complete task": GoalType.COMPLETE_TASK,
    "verify email": GoalType.VERIFY_EMAIL,
    "logout": GoalType.LOGOUT,
    "log out": GoalType.LOGOUT,
}


class NLPlanner:
    """Parse natural language instructions into executable plans.

    Phase 1: Rule-based extraction using regex patterns.
    Phase 2: Pluggable LLM backend for richer understanding.
    """

    def __init__(self, llm: LLMBackend | None = None) -> None:
        self.llm = llm

    async def parse(self, instruction: str) -> ExecutionPlan:
        """Parse a natural language instruction into an ExecutionPlan."""
        if self.llm:
            try:
                result = await self.llm.parse_instruction(instruction)
                return self._from_llm_result(instruction, result)
            except Exception:  # noqa: BLE001
                log.warning("LLM parse failed, falling back to rule-based")

        return self._rule_based_parse(instruction)

    def _rule_based_parse(self, instruction: str) -> ExecutionPlan:
        """Extract structured data using regex patterns."""
        text = instruction.strip()

        # Extract count
        count = 1
        for pattern in _COUNT_PATTERNS:
            m = pattern.search(text)
            if m:
                count = int(m.group(1))
                break

        # Extract URL
        url_match = _URL_PATTERN.search(text)
        target_url = url_match.group(0).rstrip(_TRAIL_PUNCT) if url_match else ""

        # Extract password (strip trailing sentence punctuation)
        password = ""
        for pattern in _PASSWORD_PATTERNS:
            m = pattern.search(text)
            if m:
                password = m.group(1).rstrip(_TRAIL_PUNCT)
                break

        # Extract number length
        number_length = 10
        for pattern in _NUMBER_LENGTH_PATTERNS:
            m = pattern.search(text)
            if m:
                number_length = int(m.group(1))
                break

        # Detect what to generate
        text_lower = text.lower()
        generate_numbers = any(
            kw in text_lower for kw in [
                "mobile", "phone", "number", "digit",
            ]
        )
        generate_emails = any(
            kw in text_lower for kw in ["email", "e-mail"]
        )
        generate_usernames = any(
            kw in text_lower for kw in ["username", "user name"]
        )

        # If nothing specific mentioned but creating accounts, default to numbers
        if not generate_numbers and not generate_emails and not generate_usernames:
            if count > 1:
                generate_numbers = True

        # Extract goals from text
        goals = self._extract_goals(text)

        # If no goals extracted but we have a count, default to register
        if not goals and count >= 1:
            goals.append(AgentGoal(
                type=GoalType.REGISTER_ACCOUNT,
                description="Register account",
                params={"url": target_url} if target_url else {},
            ))

        # Build account config
        account_config = AccountGenConfig(
            count=count,
            password=password,
            generate_numbers=generate_numbers,
            number_length=number_length,
            generate_emails=generate_emails,
            generate_usernames=generate_usernames,
        )

        # Estimate time (rough: 15s per account per goal)
        estimated = count * len(goals) * 15.0

        notes: list[str] = []
        if not target_url:
            notes.append("No URL detected — will need to be provided")
        if not password and count > 0:
            notes.append("No password specified — will generate random passwords")

        return ExecutionPlan(
            instruction=instruction,
            goals=goals,
            account_config=account_config,
            target_url=target_url,
            parallel=count > 3,
            max_parallel=min(count, 4),
            estimated_time_seconds=estimated,
            notes=notes,
        )

    def _extract_goals(self, text: str) -> list[AgentGoal]:
        """Extract ordered goals from text, preserving text-position order."""
        text_lower = text.lower()
        goals: list[AgentGoal] = []
        seen: set[GoalType] = set()

        # Pass 1: split by common delimiters and pick goals per part in order
        parts = re.split(r"[.,;]\s*|\bthen\b|\bafter\b|\band\b", text_lower)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            for keyword, goal_type in _GOAL_KEYWORDS.items():
                if keyword in part and goal_type not in seen:
                    seen.add(goal_type)
                    goals.append(AgentGoal(
                        type=goal_type,
                        description=_pretty_keyword(keyword),
                    ))
                    break

        # Pass 2: catch goals missed by part-splitting, ordered by their first
        # appearance in the original text (NOT by dict insertion order).
        missed: list[tuple[int, GoalType, str]] = []
        for keyword, goal_type in _GOAL_KEYWORDS.items():
            if goal_type in seen:
                continue
            idx = text_lower.find(keyword)
            if idx >= 0:
                missed.append((idx, goal_type, keyword))
        missed.sort(key=lambda t: t[0])
        for _, goal_type, keyword in missed:
            if goal_type in seen:
                continue
            seen.add(goal_type)
            goals.append(AgentGoal(
                type=goal_type,
                description=_pretty_keyword(keyword),
            ))

        return goals

    def _from_llm_result(
        self, instruction: str, result: dict[str, Any]
    ) -> ExecutionPlan:
        """Convert LLM structured output into an ExecutionPlan."""
        goals = [
            AgentGoal(
                type=GoalType(g.get("type", "custom")),
                description=g.get("description", ""),
                params=g.get("params", {}),
            )
            for g in result.get("goals", [])
        ]
        ac = result.get("account_config", {})
        account_config = AccountGenConfig(
            count=ac.get("count", 1),
            password=ac.get("password", ""),
            generate_numbers=ac.get("generate_numbers", False),
            number_length=ac.get("number_length", 10),
            generate_emails=ac.get("generate_emails", False),
            generate_usernames=ac.get("generate_usernames", False),
        )
        return ExecutionPlan(
            instruction=instruction,
            goals=goals,
            account_config=account_config,
            target_url=result.get("target_url", ""),
            parallel=result.get("parallel", False),
            max_parallel=result.get("max_parallel", 4),
            estimated_time_seconds=result.get("estimated_time_seconds", 0),
            notes=result.get("notes", []),
        )
