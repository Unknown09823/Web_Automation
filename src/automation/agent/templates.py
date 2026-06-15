"""Reusable workflow templates.

Templates are saved goal-sequences a user can re-run by name from Telegram::

    /template_save RegisterAndLogin
    Open the website
    Register
    Login

    /template_run RegisterAndLogin

Each template is a JSON file under ``data/templates/<name>.json`` with the
shape::

    {
        "name": "RegisterAndLogin",
        "instruction": "Open the website. Register. Login.",
        "goals": [<AgentGoal.to_dict()>, ...],
        "target_url": "...",
        "account_config": {...},
        "created_at": 1716981234.5,
        "updated_at": 1716981234.5,
        "uses": 7
    }

The store has no DB dependency; one file per template keeps the format
hand-editable and easy to inspect.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@dataclass(slots=True)
class Template:
    """A saved workflow template."""

    name: str
    instruction: str = ""
    goals: list[dict[str, Any]] = field(default_factory=list)
    target_url: str = ""
    account_config: dict[str, Any] = field(default_factory=dict)
    parallel: bool = False
    max_parallel: int = 1
    notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    uses: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Template":
        return cls(
            name=data["name"],
            instruction=data.get("instruction", ""),
            goals=data.get("goals", []) or [],
            target_url=data.get("target_url", ""),
            account_config=data.get("account_config", {}) or {},
            parallel=bool(data.get("parallel", False)),
            max_parallel=int(data.get("max_parallel", 1)),
            notes=list(data.get("notes", []) or []),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            uses=int(data.get("uses", 0)),
        )

    @classmethod
    def from_plan(cls, name: str, plan: Any) -> "Template":
        """Build a template from an :class:`ExecutionPlan` (NL planner output)."""
        ac = getattr(plan, "account_config", None)
        ac_dict: dict[str, Any] = {}
        if ac:
            ac_dict = {
                "count": getattr(ac, "count", 1),
                "password": "" if getattr(ac, "password", "") else "",
                "generate_numbers": bool(getattr(ac, "generate_numbers", False)),
                "number_length": int(getattr(ac, "number_length", 10)),
                "generate_emails": bool(getattr(ac, "generate_emails", False)),
                "generate_usernames": bool(getattr(ac, "generate_usernames", False)),
            }
        return cls(
            name=name,
            instruction=getattr(plan, "instruction", "") or "",
            goals=[g.to_dict() for g in getattr(plan, "goals", [])],
            target_url=getattr(plan, "target_url", "") or "",
            account_config=ac_dict,
            parallel=bool(getattr(plan, "parallel", False)),
            max_parallel=int(getattr(plan, "max_parallel", 1)),
            notes=list(getattr(plan, "notes", []) or []),
        )


class TemplateStore:
    """File-backed CRUD for templates.

    ``one file per template`` keeps the layout transparent — operators can
    `cat data/templates/RegisterAndLogin.json` to inspect or hand-edit one.
    """

    def __init__(self, root: str | Path = "data/templates") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_name(name: str) -> str:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError(
                f"invalid template name {name!r}: use letters, digits, _ and -",
            )
        return name

    def _path(self, name: str) -> Path:
        return self.root / f"{self.validate_name(name)}.json"

    # ----------------------------------------------------------- CRUD
    def list(self) -> list[Template]:
        out: list[Template] = []
        if not self.root.exists():
            return out
        for f in sorted(self.root.iterdir()):
            if f.suffix.lower() != ".json":
                continue
            try:
                out.append(Template.from_dict(json.loads(f.read_text())))
            except (json.JSONDecodeError, KeyError):
                log.warning("template %s is corrupt; skipping", f.name)
        return out

    def get(self, name: str) -> Template | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return Template.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError):
            log.warning("template %s is corrupt", name)
            return None

    def save(self, template: Template) -> Template:
        template.name = self.validate_name(template.name)
        existing = self.get(template.name)
        now = time.time()
        if existing:
            template.created_at = existing.created_at
            # Inherit usage count, but never let `save` clobber a bump that
            # ``record_use`` already applied to ``template``.
            if template.uses < existing.uses:
                template.uses = existing.uses
        template.updated_at = now
        path = self._path(template.name)
        path.write_text(json.dumps(template.to_dict(), indent=2, default=str))
        return template

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def record_use(self, name: str) -> None:
        """Increment usage counter; safe to call after every run."""
        t = self.get(name)
        if not t:
            return
        t.uses += 1
        t.updated_at = time.time()
        self.save(t)


__all__ = ["Template", "TemplateStore"]
