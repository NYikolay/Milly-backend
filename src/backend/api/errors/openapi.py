"""OpenAPI helpers documenting the unified API error envelope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute

from backend.api.errors.schemas import ErrorResponse
from backend.core.errors import ErrorCode, get_error_spec

ERROR_RESPONSES: dict[int, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "Bad Request"},
    404: {"model": ErrorResponse, "description": "Not Found"},
    422: {"model": ErrorResponse, "description": "Validation Error"},
    500: {"model": ErrorResponse, "description": "Internal Server Error"},
    503: {"model": ErrorResponse, "description": "Service Unavailable"},
}


def document_errors(
    errors: Mapping[ErrorCode, str],
) -> dict[int | str, dict[str, Any]]:
    """
    Build OpenAPI responses for operation-specific error codes.

    Purpose:
        Document the concrete HTTP status and machine-readable code of every
        error an operation can return, deriving the status from the error
        registry so documentation cannot drift from runtime behavior.

    Args:
        errors: Mapping from an error code to an operation-specific English
            explanation of when the operation returns it.

    Returns:
        dict[int | str, dict[str, Any]]: Response documentation keyed by HTTP
            status, ready to pass to FastAPI's ``responses`` argument.

    Raises:
        KeyError: If a code has no registered specification.

    Examples:
        >>> responses = document_errors(
        ...     {ErrorCode.FORBIDDEN: "The request origin is not allowed."}
        ... )
        >>> responses[403]["description"]
        '- forbidden: The request origin is not allowed.'

    Notes:
        - Codes sharing one status are merged into a single response
          description in mapping order.
        - Passing the result as ``responses`` to a standardized route replaces
          the generic description for the same status.
    """
    grouped: dict[int, list[str]] = {}
    for code, description in errors.items():
        spec = get_error_spec(code)
        grouped.setdefault(spec.status_code, []).append(f"- {code.value}: {description}")

    responses: dict[int | str, dict[str, Any]] = {
        status: {
            "model": ErrorResponse,
            "description": "\n".join(descriptions),
        }
        for status, descriptions in grouped.items()
    }
    return responses


class StandardizedAPIRoute(APIRoute):
    """API route that documents the unified error envelope on every operation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """
        Merge the common error responses with route-specific responses.

        Purpose:
            Ensure every operation documents the unified error contract while
            allowing a route to override a status with its own response.

        Args:
            *args: Positional arguments forwarded to ``APIRoute``.
            **kwargs: Keyword arguments forwarded to ``APIRoute``; an optional
                ``responses`` mapping is merged over ``ERROR_RESPONSES``.

        Notes:
            - Copies ``ERROR_RESPONSES``, including each nested mapping, so
              route construction never mutates the shared defaults.
            - Route-specific responses win on status-code conflicts.
        """
        merged_responses: dict[int, dict[str, object]] = {
            status: dict(response) for status, response in ERROR_RESPONSES.items()
        }
        merged_responses.update(kwargs.get("responses") or {})
        kwargs["responses"] = merged_responses
        super().__init__(*args, **kwargs)


class StandardizedAPIRouter(APIRouter):
    """API router that uses ``StandardizedAPIRoute`` by default."""

    def __init__(
        self,
        *,
        route_class: type[APIRoute] = StandardizedAPIRoute,
        **kwargs: Any,
    ) -> None:
        """
        Build a router documenting the unified error envelope.

        Purpose:
            Apply error-response documentation to every route unless the caller
            explicitly supplies a different route class.

        Args:
            route_class: Route class used to build path operations. Defaults to
                ``StandardizedAPIRoute``.
            **kwargs: Keyword arguments forwarded to ``APIRouter``.

        Notes:
            - Passing ``route_class`` explicitly disables the standardized
              default for this router.
        """
        super().__init__(route_class=route_class, **kwargs)
