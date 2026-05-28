"""Account data generation.

Generates random accounts based on instructions:
  - Random mobile numbers (10-digit, configurable prefix)
  - Random usernames
  - Random emails
  - Custom patterns
  - CSV imports
  - Manual data pass-through

Used by the NL planner to auto-generate account data from instructions
like "Create 20 accounts with random 10-digit numbers and password Test@123".
"""
from __future__ import annotations

import csv
import io
import logging
import random
import string
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GeneratedAccount:
    """A single generated account ready for the AccountManager."""

    id: str
    number: str = ""
    username: str = ""
    email: str = ""
    password: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "password": self.password}
        if self.number:
            d["number"] = self.number
        if self.username:
            d["username"] = self.username
        if self.email:
            d["email"] = self.email
        if self.metadata:
            d["metadata"] = self.metadata
        return d



class DataFactory:
    """Generate account data from high-level instructions.

    Deterministic when seeded; fully random otherwise. Supports multiple
    generation strategies that can be combined.
    """

    def __init__(self, *, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def generate(
        self,
        count: int,
        *,
        password: str = "",
        number_prefix: str = "",
        number_length: int = 10,
        username_prefix: str = "user_",
        email_domain: str = "example.com",
        generate_numbers: bool = False,
        generate_usernames: bool = False,
        generate_emails: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> list[GeneratedAccount]:
        """Generate ``count`` accounts with the specified parameters."""
        accounts: list[GeneratedAccount] = []
        for i in range(count):
            acc_id = f"acc_{i + 1:03d}"
            number = ""
            username = ""
            email = ""

            if generate_numbers:
                number = self._random_number(number_prefix, number_length)
            if generate_usernames:
                username = self._random_username(username_prefix)
            if generate_emails:
                email = self._random_email(email_domain)

            accounts.append(GeneratedAccount(
                id=acc_id,
                number=number,
                username=username,
                email=email,
                password=password or self._random_password(),
                metadata=dict(metadata or {}),
            ))
        return accounts

    def from_csv(self, csv_text: str, *, password: str = "") -> list[GeneratedAccount]:
        """Parse CSV text into accounts.

        Expected columns: number|username|email|password (any subset).
        """
        reader = csv.DictReader(io.StringIO(csv_text))
        accounts: list[GeneratedAccount] = []
        for i, row in enumerate(reader):
            number = row.get("number") or row.get("phone") or ""
            username = row.get("username") or ""
            email = row.get("email") or ""
            pwd = row.get("password") or row.get("pass") or password
            acc_id = (
                row.get("id") or number or username or email
                or f"csv_{i + 1:03d}"
            )
            accounts.append(GeneratedAccount(
                id=acc_id,
                number=number,
                username=username,
                email=email,
                password=pwd,
            ))
        return accounts

    def from_list(self, items: list[dict[str, Any]]) -> list[GeneratedAccount]:
        """Convert a list of dicts to GeneratedAccount objects."""
        accounts: list[GeneratedAccount] = []
        for i, item in enumerate(items):
            number = str(item.get("number") or item.get("phone") or "")
            username = str(item.get("username") or "")
            email = str(item.get("email") or "")
            pwd = str(item.get("password") or item.get("pass") or "")
            acc_id = str(
                item.get("id") or number or username or email
                or f"item_{i + 1:03d}"
            )
            accounts.append(GeneratedAccount(
                id=acc_id,
                number=number,
                username=username,
                email=email,
                password=pwd,
                metadata=item.get("metadata", {}),
            ))
        return accounts

    # ---------------------------------------------------------------- generators
    def _random_number(self, prefix: str, length: int) -> str:
        """Generate a random phone number."""
        prefix = prefix or ""
        remaining = length - len(prefix)
        if remaining <= 0:
            remaining = length
            prefix = ""
        # First digit after prefix should not be 0
        first = str(self._rng.randint(1, 9))
        rest = "".join(str(self._rng.randint(0, 9)) for _ in range(remaining - 1))
        return f"{prefix}{first}{rest}"

    def _random_username(self, prefix: str) -> str:
        """Generate a random username."""
        suffix = "".join(
            self._rng.choices(string.ascii_lowercase + string.digits, k=8)
        )
        return f"{prefix}{suffix}"

    def _random_email(self, domain: str) -> str:
        """Generate a random email."""
        local = "".join(
            self._rng.choices(string.ascii_lowercase + string.digits, k=10)
        )
        return f"{local}@{domain}"

    def _random_password(self) -> str:
        """Generate a reasonably strong random password."""
        chars = string.ascii_letters + string.digits + "!@#$%"
        pwd = [
            self._rng.choice(string.ascii_uppercase),
            self._rng.choice(string.ascii_lowercase),
            self._rng.choice(string.digits),
            self._rng.choice("!@#$%"),
        ]
        pwd.extend(self._rng.choices(chars, k=8))
        self._rng.shuffle(pwd)
        return "".join(pwd)
