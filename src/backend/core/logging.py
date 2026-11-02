from __future__ import annotations

import logging
import sys

import structlog

from backend.core.config import Settings


def configure_logging(settings: Settings) -> None:
    """
    Configure structlog and route stdlib logs through one stdout handler.

    Purpose:
        Install a single process-wide logging pipeline shared by the
        application, uvicorn, and third-party libraries.

    Args:
        settings: Application settings providing the log level, output format,
            and SQL echo flag.

    Returns:
        None: This function configures global logging and returns nothing.

    Notes:
        - Repeated calls replace the previous configuration safely.
        - Uvicorn access logs are disabled because access logging is emitted by
          ``RequestContextMiddleware``.
        - When ``sql_echo`` is false, the SQLAlchemy engine logger is raised to
          ``WARNING`` so SQL statements are not logged.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level.upper())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

    if not settings.sql_echo:
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
