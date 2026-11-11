"""Domain errors for task-management rules."""

from __future__ import annotations


class ReminderConfigurationError(ValueError):
    """Invalid reminder rule or schedule outside the supported range."""
