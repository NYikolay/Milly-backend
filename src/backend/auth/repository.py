"""Compatibility re-exports of the auth persistence port and adapter."""

from backend.application.ports.auth import AuthRepository as AuthRepository
from backend.infrastructure.persistence.sqlalchemy.auth_repository import (
    SqlAlchemyAuthRepository as SqlAlchemyAuthRepository,
)

__all__ = ["AuthRepository", "SqlAlchemyAuthRepository"]
