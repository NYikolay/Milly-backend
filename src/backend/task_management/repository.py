"""Compatibility re-export of the composite SQLAlchemy task-management adapter."""

from backend.infrastructure.persistence.sqlalchemy.legacy import (
    SqlAlchemyTaskManagementRepository as SqlAlchemyTaskManagementRepository,
)

__all__ = ["SqlAlchemyTaskManagementRepository"]
