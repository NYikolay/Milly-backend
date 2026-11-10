"""Compatibility re-exports of access-token and refresh-token helpers."""

from backend.domain.auth.models import AccessTokenClaims as AccessTokenClaims
from backend.domain.auth.tokens import generate_refresh_token as generate_refresh_token
from backend.domain.auth.tokens import hash_refresh_token as hash_refresh_token
from backend.infrastructure.auth.tokens import (
    create_access_token as create_access_token,
)
from backend.infrastructure.auth.tokens import (
    decode_access_token as decode_access_token,
)

__all__ = [
    "AccessTokenClaims",
    "create_access_token",
    "decode_access_token",
    "generate_refresh_token",
    "hash_refresh_token",
]
