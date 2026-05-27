"""Small security helpers used by API auth and audit logging."""
from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_token(length: int = 48) -> str:
    """Generate a URL-safe random token."""
    return secrets.token_urlsafe(length)


def hash_token(token: str, salt: str = "automation") -> str:
    """One-way hash a token. Used for audit-friendly identifiers."""
    return hashlib.sha256(f"{salt}:{token}".encode()).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    """Compare two strings in constant time."""
    return hmac.compare_digest(a.encode(), b.encode())
