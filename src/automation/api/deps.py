"""FastAPI dependencies and shared context."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Header, HTTPException, Request, status

from automation.utils.security import constant_time_eq

if TYPE_CHECKING:  # pragma: no cover
    from automation.accounts.manager import AccountManager
    from automation.ai.brain import AIBrain
    from automation.browser.manager import BrowserManager
    from automation.controllers.workflow_engine import WorkflowEngine
    from automation.core.engine import Engine

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    """Container for objects shared across API routes.

    Held on ``app.state.ctx`` so handlers can pull them via ``request.app.state.ctx``.
    """

    engine: "Engine"
    accounts: "AccountManager | None" = None
    browser: "BrowserManager | None" = None
    brain: "AIBrain | None" = None
    workflow_engine: "WorkflowEngine | None" = None
    audit: list[dict[str, Any]] = field(default_factory=list)
    workflows_dir: str = "config/workflows"
    workflow_results: list[dict[str, Any]] = field(default_factory=list)

    def record_audit(self, who: str, action: str, detail: str = "") -> None:
        entry = {
            "ts": time.time(),
            "who": who,
            "action": action,
            "detail": detail,
        }
        self.audit.append(entry)
        # cap audit log to last 1000 entries to bound memory
        if len(self.audit) > 1000:
            del self.audit[: len(self.audit) - 1000]


def get_context(request: Request) -> AppContext:
    ctx: AppContext | None = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise HTTPException(status_code=500, detail="API context not configured")
    return ctx


def auth_required(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
) -> str:
    """API token auth.

    Token is read from env ``AUTOMATION_API_TOKEN``. If unset, the API runs in
    *open* mode (development) and warns on startup. Send token as
    ``Authorization: Bearer <token>`` or ``X-API-Token: <token>``.
    """
    expected = os.environ.get("AUTOMATION_API_TOKEN", "")
    if not expected:
        return "anonymous"
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_api_token:
        presented = x_api_token.strip()
    if not presented or not constant_time_eq(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # cheap "actor" identifier for audit logs (last 6 chars of token hash)
    from automation.utils.security import hash_token
    return f"token:{hash_token(presented)[:8]}"
