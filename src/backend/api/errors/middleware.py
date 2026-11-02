from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.exc import SQLAlchemyError
from starlette._utils import parse_host_header
from starlette.datastructures import URL, Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from taskiq.exceptions import TaskiqError

from backend.api.errors.handlers import render_error_response
from backend.api.errors.mapping import (
    ErrorPayload,
    payload_from_app_error,
    payload_from_http_exception,
    payload_from_sqlalchemy_error,
    payload_from_taskiq_error,
    payload_from_unexpected_error,
)
from backend.core.config import Settings
from backend.core.errors import AppError, BadRequestError


class ErrorEnvelopeMiddleware:
    """Render middleware and unhandled failures as the flat error envelope."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        """
        Initialize the error envelope middleware.

        Purpose:
            Store the wrapped application and the settings used by the error
            renderer.

        Args:
            app: Next ASGI application in the middleware stack.
            settings: Application settings used to render error responses.
        """
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Render failures raised by the wrapped application as JSON errors.

        Purpose:
            Catch expected application failures, HTTP exceptions, database
            failures, task broker failures, and unexpected exceptions raised
            by inner middleware and render them with the shared error contract.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.

        Returns:
            None: This coroutine returns after the wrapped application or the
                rendered error response completes.

        Raises:
            Exception: The original exception when the response has already
                started and can no longer be replaced.

        Notes:
            - Non-HTTP scopes pass through unchanged.
            - ``BaseException`` subclasses such as cancellation are not caught.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_with_tracking(message: Message) -> None:
            """
            Track whether the response has started before forwarding a message.

            Purpose:
                Record the start of the inner response so an error cannot
                replace a response that is already in flight.

            Args:
                message: ASGI response message being forwarded.
            """
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def render(payload: ErrorPayload, exc: Exception) -> None:
            """
            Render an error payload or propagate the original failure.

            Purpose:
                Send the envelope response unless the inner response already
                started, in which case the original exception is re-raised.

            Args:
                payload: Error data produced by a payload mapper.
                exc: Original exception that escaped the inner application.

            Raises:
                Exception: If the response has already started.
            """
            if response_started:
                raise exc
            response = render_error_response(
                payload,
                scope=scope,
                settings=self.settings,
                exc=exc,
            )
            await response(scope, receive, send)

        try:
            await self.app(scope, receive, send_with_tracking)
        except AppError as exc:
            await render(payload_from_app_error(exc), exc)
        except StarletteHTTPException as exc:
            await render(payload_from_http_exception(exc), exc)
        except SQLAlchemyError as exc:
            await render(payload_from_sqlalchemy_error(exc), exc)
        except TaskiqError as exc:
            await render(payload_from_taskiq_error(exc), exc)
        except Exception as exc:
            await render(payload_from_unexpected_error(exc, debug=self.settings.debug), exc)


class AllowedHostsMiddleware:
    """Reject HTTP requests whose Host header is not in the allowlist."""

    def __init__(
        self,
        app: ASGIApp,
        allowed_hosts: Sequence[str],
        www_redirect: bool = True,
    ) -> None:
        """
        Initialize the allowed hosts middleware.

        Purpose:
            Normalize the allowlist and store the redirect policy once per
            application instance.

        Args:
            app: Next ASGI application in the middleware stack.
            allowed_hosts: Exact hostnames or ``*.example.com`` wildcard
                patterns; ``"*"`` disables validation.
            www_redirect: Whether a bare host is redirected to its allowed
                ``www.`` variant. Defaults to True.

        Notes:
            - Matching is case-insensitive, so patterns are lowercased here.
        """
        self.app = app
        self.allowed_hosts = [pattern.lower() for pattern in allowed_hosts]
        self.allow_any = "*" in allowed_hosts
        self.www_redirect = www_redirect

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Validate the Host header before forwarding an HTTP request.

        Purpose:
            Enforce the allowlist, redirect a bare host to its allowed ``www``
            variant, and reject invalid or disallowed hosts with a bad-request
            application error.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.

        Returns:
            None: This coroutine returns after the request is forwarded or the
                redirect response completes.

        Raises:
            BadRequestError: If the Host header is missing or invalid, or the
                host is not allowed and no redirect applies.

        Notes:
            - Non-HTTP scopes and ``"*"`` allowlists pass through unchanged.
            - Ports are ignored during matching but preserved in redirects.
        """
        if scope["type"] != "http" or self.allow_any:
            await self.app(scope, receive, send)
            return

        parsed_host = parse_host_header(Headers(scope=scope).get("host"))
        if parsed_host is None:
            raise BadRequestError(message="Invalid host header")
        host = parsed_host.host.lower()

        found_www_redirect = False
        for pattern in self.allowed_hosts:
            if host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:])):
                await self.app(scope, receive, send)
                return
            if "www." + host == pattern:
                found_www_redirect = True

        if found_www_redirect and self.www_redirect:
            url = URL(scope=scope)
            redirect_url = url.replace(netloc="www." + url.netloc)
            response = RedirectResponse(url=str(redirect_url))
            await response(scope, receive, send)
            return

        raise BadRequestError()
