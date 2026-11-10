"""Compatibility re-exports for the Telegram reminder sender."""

from backend.application.ports.collaborators import ReminderSender as TelegramSender
from backend.infrastructure.telegram.sender import TelegramBotSender

__all__ = ["TelegramBotSender", "TelegramSender"]
