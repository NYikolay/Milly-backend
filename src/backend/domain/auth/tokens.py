"""Pure refresh-token helpers with no framework dependencies."""

from __future__ import annotations

import hashlib
import secrets


def generate_refresh_token() -> str:
    """
    Generate an opaque refresh token.

    Purpose:
        Produce a high-entropy bearer value that is stored only as a hash.

    Returns:
        str: URL-safe random refresh token.

    Notes:
        - Uses 32 random bytes, yielding at least 43 URL-safe characters.
    """
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    """
    Hash a refresh token for storage and lookup.

    Purpose:
        Derive a deterministic SHA-256 digest so the raw refresh token never
        needs to be persisted.

    Args:
        token: Raw refresh token.

    Returns:
        str: Lowercase hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(token.encode()).hexdigest()
