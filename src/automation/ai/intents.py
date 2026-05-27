"""Semantic intent matching.

Maps user/workflow intents (``register``, ``login``, ``submit``, ...) to a
weighted set of synonyms, ARIA roles, and regex patterns. Used by the
perception layer to label detected elements with intent-level meaning so the
planner can reason about pages without depending on CSS selectors.

The intent dictionary is data, not logic — it can be extended via config or
plugins without touching this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(slots=True)
class Intent:
    """A semantic intent: a name plus the words/patterns that imply it."""

    name: str
    keywords: tuple[str, ...]
    roles: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()
    description: str = ""


#: Built-in intents. Order is irrelevant; matching is by score.
INTENTS: tuple[Intent, ...] = (
    Intent(
        name="register",
        keywords=(
            "register", "sign up", "signup", "create account", "create an account",
            "join", "get started", "join now", "create new", "new account",
        ),
        roles=("button", "link"),
        description="Create a new account.",
    ),
    Intent(
        name="login",
        keywords=(
            "login", "log in", "sign in", "signin", "access account",
            "log into", "enter", "access",
        ),
        roles=("button", "link"),
        description="Authenticate to existing account.",
    ),
    Intent(
        name="logout",
        keywords=("logout", "log out", "sign out", "signout", "exit"),
        roles=("button", "link"),
        description="End the current session.",
    ),
    Intent(
        name="submit",
        keywords=("submit", "send", "save", "confirm", "ok", "done", "apply"),
        roles=("button",),
        description="Submit a form or confirm an action.",
    ),
    Intent(
        name="continue",
        keywords=("continue", "next", "proceed", "go on", "forward"),
        roles=("button", "link"),
        description="Advance to next step.",
    ),
    Intent(
        name="cancel",
        keywords=("cancel", "back", "previous", "go back", "abort", "close"),
        roles=("button", "link"),
        description="Abort or step back.",
    ),
    Intent(
        name="dashboard",
        keywords=("dashboard", "home", "overview", "main"),
        roles=("link", "button"),
        description="Go to dashboard / home.",
    ),
    Intent(
        name="profile",
        keywords=("profile", "account", "my account", "settings", "preferences"),
        roles=("link", "button"),
        description="Open profile or settings.",
    ),
    Intent(
        name="username_field",
        keywords=("username", "user name", "login id", "email or username"),
        roles=("textbox",),
        placeholders=("username", "user name", "login"),
        description="Username input.",
    ),
    Intent(
        name="email_field",
        keywords=("email", "e-mail", "email address"),
        roles=("textbox",),
        placeholders=("email", "e-mail"),
        description="Email input.",
    ),
    Intent(
        name="password_field",
        keywords=("password", "passcode", "secret"),
        roles=("textbox",),
        placeholders=("password",),
        description="Password input.",
    ),
    Intent(
        name="confirm_password_field",
        keywords=(
            "confirm password", "repeat password", "re-enter password",
            "verify password",
        ),
        roles=("textbox",),
        placeholders=("confirm password", "repeat password"),
        description="Password confirmation input.",
    ),
    Intent(
        name="search",
        keywords=("search", "find", "lookup"),
        roles=("textbox", "searchbox", "button"),
        placeholders=("search",),
        description="Search input or trigger.",
    ),
    Intent(
        name="accept_terms",
        keywords=(
            "accept terms", "agree", "i agree", "terms and conditions",
            "privacy policy", "consent",
        ),
        roles=("checkbox",),
        description="Terms / consent checkbox.",
    ),
    Intent(
        name="dialog_close",
        keywords=("close", "dismiss", "no thanks", "not now"),
        roles=("button",),
        description="Close a dialog or popup.",
    ),
)


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


@dataclass(slots=True)
class IntentMatch:
    intent: str
    score: float
    matched_on: str  # "text", "aria", "placeholder", "role+text"


class IntentMatcher:
    """Score-based matcher: free of CSS selectors and language-specific rules.

    Confidence is in [0, 1]. Higher means more likely.
    """

    def __init__(self, intents: Iterable[Intent] = INTENTS) -> None:
        self._intents = tuple(intents)

    @property
    def intents(self) -> tuple[Intent, ...]:
        return self._intents

    def match(
        self,
        *,
        text: str | None = None,
        aria_label: str | None = None,
        placeholder: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> list[IntentMatch]:
        candidates: list[IntentMatch] = []
        haystacks = {
            "text": _normalize(text),
            "aria": _normalize(aria_label),
            "placeholder": _normalize(placeholder),
            "name": _normalize(name),
        }
        role_norm = _normalize(role)
        for intent in self._intents:
            score = 0.0
            matched_on = ""
            for source, hay in haystacks.items():
                if not hay:
                    continue
                for kw in intent.keywords:
                    if kw == hay:
                        if score < 0.95:
                            score = 0.95
                            matched_on = source
                    elif kw in hay:
                        # weight by ratio of match length to text length
                        ratio = len(kw) / max(len(hay), 1)
                        s = 0.55 + 0.4 * ratio
                        if s > score:
                            score = s
                            matched_on = source
                for ph in intent.placeholders:
                    if ph in hay:
                        s = 0.7
                        if s > score:
                            score = s
                            matched_on = source
            if intent.roles and role_norm in intent.roles:
                # role agreement gives a small boost; alone is not enough
                score = min(1.0, score + 0.1) if score > 0 else 0.25
                if not matched_on:
                    matched_on = "role"
            if score > 0:
                candidates.append(IntentMatch(intent.name, round(score, 3), matched_on))
        candidates.sort(key=lambda m: m.score, reverse=True)
        return candidates

    def best(self, **kwargs: object) -> IntentMatch | None:
        matches = self.match(**kwargs)  # type: ignore[arg-type]
        return matches[0] if matches else None
