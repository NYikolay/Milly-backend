from __future__ import annotations

from enum import StrEnum


class Priority(StrEnum):
    """Task and series priority values persisted as strings."""

    URGENT_IMPORTANT = "urgent_important"
    IMPORTANT_NOT_URGENT = "important_not_urgent"
    URGENT_NOT_IMPORTANT = "urgent_not_important"
    NEITHER = "neither"


class SeriesFrequency(StrEnum):
    """Supported recurring-series frequencies."""

    DAILY = "daily"


class ReminderKind(StrEnum):
    """Supported task and series reminder kinds."""

    BEFORE_DUE = "before_due"
    SCHEDULED = "scheduled"


class DeliveryStatus(StrEnum):
    """Lifecycle states of a reminder delivery."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
