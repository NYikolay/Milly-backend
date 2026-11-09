from __future__ import annotations

import asyncio
import json
from http.client import HTTPException
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.auth.errors import AuthNotConfiguredError
from backend.core.config import Settings

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_REQUEST_TIMEOUT_SECONDS = 10.0
_SAFE_ERROR = "Telegram message delivery failed"


class TelegramSender(Protocol):
    """Contract for delivering a text message to a Telegram chat."""

    async def send(self, chat_id: int, text: str) -> int | None:
        """
        Send a message to a Telegram chat.

        Purpose:
            Decouple reminder delivery from the concrete Telegram transport.

        Args:
            chat_id: Telegram chat identifier from the user's profile.
            text: Message text; implementations must never log it.

        Returns:
            int | None: Telegram message identifier when available.

        Raises:
            AuthNotConfiguredError: If the bot token is missing.
            RuntimeError: If Telegram rejects the request or omits the message
                identifier.
        """
        ...


class TelegramBotSender:
    """Telegram Bot API sender configured with an application bot token."""

    def __init__(self, settings: Settings) -> None:
        """
        Initialize the sender and validate the bot token.

        Purpose:
            Fail before delivery work when Telegram cannot be called.

        Args:
            settings: Application settings containing the bot token.

        Raises:
            AuthNotConfiguredError: If the Telegram bot token is missing or
                blank.

        Notes:
            - The token is kept in memory and is never logged or included in
              raised error messages.
        """
        token = (
            settings.telegram_bot_token.get_secret_value().strip()
            if settings.telegram_bot_token is not None
            else ""
        )
        if not token:
            raise AuthNotConfiguredError()
        self._token = token

    async def send(self, chat_id: int, text: str) -> int | None:
        """
        Send a text message through the Telegram Bot API.

        Purpose:
            Deliver reminders without blocking the taskiq event loop.

        Args:
            chat_id: Telegram chat identifier from the user's profile.
            text: Message text; never logged by this sender.

        Returns:
            int | None: Telegram message identifier.

        Raises:
            RuntimeError: If the request fails, Telegram rejects the message, or
                the response lacks a message identifier.

        Notes:
            - The blocking HTTP request runs in a worker thread.
            - Error messages are generic so the token and message text cannot
              leak through logs.
        """
        document = await asyncio.to_thread(_post_message, self._token, chat_id, text)
        if document.get("ok") is not True:
            raise RuntimeError(_SAFE_ERROR)
        result = document.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            raise RuntimeError(_SAFE_ERROR)
        return message_id


def _post_message(token: str, chat_id: int, text: str) -> dict[str, Any]:
    """
    Post one message to the Telegram Bot API.

    Purpose:
        Encapsulate the blocking HTTP exchange so it can run in a worker thread.

    Args:
        token: Telegram bot token.
        chat_id: Telegram chat identifier from the user's profile.
        text: Message text.

    Returns:
        dict[str, Any]: Decoded Telegram response document.

    Raises:
        RuntimeError: If the request fails or the response is not a JSON object.

    Notes:
        - The message is always sent with Telegram ``HTML`` parse mode.
        - The raised error intentionally omits the token, text, and HTTP details.
    """
    payload = urlencode(
        {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
        }
    ).encode("utf-8")
    request = Request(
        _API_URL.format(token=token),
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            document = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPException, ValueError):  # fmt: skip
        raise RuntimeError(_SAFE_ERROR) from None
    if not isinstance(document, dict):
        raise RuntimeError(_SAFE_ERROR)
    return document
