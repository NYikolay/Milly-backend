"""Tests for the single composition root used by API and Taskiq entrypoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import (
    get_current_access,
    get_label_service,
    get_palette_service,
    get_project_service,
    get_series_service,
    get_task_service,
)
from backend.application.auth.service import AuthService
from backend.application.ports.collaborators import ReminderSender
from backend.application.task_management.labels import LabelService
from backend.application.task_management.palette import PaletteService
from backend.application.task_management.projects import ProjectService
from backend.application.task_management.series import SeriesService
from backend.application.task_management.series_generation import (
    SeriesGenerationService,
)
from backend.application.task_management.tasks import TaskService
from backend.composition.auth import build_auth_service, build_token_verifier
from backend.composition.task_management import (
    build_palette_service,
    build_reminder_dispatcher,
    build_series_generation_service,
    build_series_service,
    build_task_management_services,
)
from backend.core.config import Settings
from backend.infrastructure.auth.tokens import JwtTokenVerifier, create_access_token
from backend.infrastructure.persistence.sqlalchemy.account_repository import (
    SqlAlchemyAccountRepository,
)
from backend.infrastructure.persistence.sqlalchemy.label_repository import (
    SqlAlchemyLabelRepository,
)
from backend.infrastructure.persistence.sqlalchemy.palette_repository import (
    SqlAlchemyPaletteRepository,
)
from backend.infrastructure.persistence.sqlalchemy.project_repository import (
    SqlAlchemyProjectRepository,
)
from backend.infrastructure.persistence.sqlalchemy.reminder_repository import (
    SqlAlchemyReminderRepository,
)
from backend.infrastructure.persistence.sqlalchemy.series_repository import (
    SqlAlchemySeriesRepository,
)
from backend.infrastructure.persistence.sqlalchemy.task_repository import (
    SqlAlchemyTaskRepository,
)
from backend.infrastructure.persistence.sqlalchemy.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from backend.infrastructure.telegram.sender import TelegramBotSender
from tests.task_management_fakes import FakeSender, fixed_now


class RecordingSession:
    """Minimal session double used to prove adapter and UoW sharing."""


class FakeState:
    """Application-state double exposing settings and the token verifier."""

    def __init__(self, settings: Settings) -> None:
        """Store the settings and verifier read by the auth dependencies."""
        self.settings = settings
        self.token_verifier = build_token_verifier(settings)


class FakeApp:
    """Application double exposing the state attribute used by dependencies."""

    def __init__(self, settings: Settings) -> None:
        """Build the state container with the supplied settings."""
        self.state = FakeState(settings)
        self.app = self

    def __getattr__(self, name: str) -> object:
        """Expose only the state attribute required by dependencies."""
        raise AttributeError(name)


def make_settings() -> Settings:
    """Build deterministic settings with a bot token and JWT secret."""
    return Settings(
        _env_file=None,
        telegram_bot_token="123:secret",
        auth_jwt_secret="a" * 32,
    )


def make_session() -> AsyncSession:
    """Build a typed recording session double."""
    return cast(AsyncSession, RecordingSession())


def make_request(settings: Settings) -> Request:
    """Build a request double carrying application settings."""
    return cast(Request, FakeApp(settings))


def test_task_composition_uses_one_session_and_shared_uow() -> None:
    """API and worker builders choose the same aggregate adapters."""
    session = make_session()

    services = build_task_management_services(session, clock=fixed_now)

    assert isinstance(services.palette, PaletteService)
    assert isinstance(services.projects, ProjectService)
    assert isinstance(services.labels, LabelService)
    assert isinstance(services.series, SeriesService)
    assert isinstance(services.tasks, TaskService)
    tasks = cast(Any, services.tasks)
    projects = cast(Any, services.projects)
    series = cast(Any, services.series)
    labels = cast(Any, services.labels)
    palette = cast(Any, services.palette)
    assert tasks._repository is not projects._repository
    assert tasks._unit_of_work is series._unit_of_work
    assert tasks._unit_of_work._session is session
    assert tasks._repository._session is session
    assert series._repository._session is session
    assert projects._repository._session is session
    assert labels._repository._session is session
    assert palette._repository._session is session


def test_task_composition_passes_the_project_repository_as_today_progress_reader() -> None:
    """TaskService receives the aggregate project repository for today progress."""
    services = build_task_management_services(make_session(), clock=fixed_now)

    assert isinstance(services.projects._repository, SqlAlchemyProjectRepository)
    assert services.tasks._project_reader is services.projects._repository


def test_series_builder_uses_the_account_and_aggregate_adapters() -> None:
    """The series builder wires series, task, reminder, reference, and project ports."""
    session = make_session()

    service = build_series_service(session, clock=fixed_now)

    references = cast(Any, service._references)
    materializer = cast(Any, service._materializer)
    assert isinstance(service._repository, SqlAlchemySeriesRepository)
    assert isinstance(service._task_repository, SqlAlchemyTaskRepository)
    assert isinstance(service._reminder_repository, SqlAlchemyReminderRepository)
    assert isinstance(references._account_repository, SqlAlchemyAccountRepository)
    assert isinstance(materializer._series_repository, SqlAlchemySeriesRepository)
    assert isinstance(materializer._task_repository, SqlAlchemyTaskRepository)
    assert isinstance(materializer._reminder_repository, SqlAlchemyReminderRepository)
    assert isinstance(materializer._project_reader, SqlAlchemyProjectRepository)
    assert materializer._references is references
    assert isinstance(references._label_reader, SqlAlchemyLabelRepository)
    assert isinstance(service._unit_of_work, SqlAlchemyUnitOfWork)
    assert service._unit_of_work._session is session


def test_generation_builder_uses_the_aggregate_adapters() -> None:
    """The generation builder wires series, materializer, and unit-of-work ports."""
    session = make_session()

    service = build_series_generation_service(session, clock=fixed_now)

    materializer = cast(Any, service._materializer)
    assert isinstance(service, SeriesGenerationService)
    assert isinstance(service._series_repository, SqlAlchemySeriesRepository)
    assert isinstance(materializer._series_repository, SqlAlchemySeriesRepository)
    assert isinstance(materializer._task_repository, SqlAlchemyTaskRepository)
    assert isinstance(materializer._reminder_repository, SqlAlchemyReminderRepository)
    assert isinstance(materializer._project_reader, SqlAlchemyProjectRepository)
    assert isinstance(materializer._references._account_repository, SqlAlchemyAccountRepository)
    assert isinstance(materializer._references._label_reader, SqlAlchemyLabelRepository)
    assert isinstance(service._unit_of_work, SqlAlchemyUnitOfWork)
    assert service._unit_of_work is materializer._unit_of_work
    assert service._unit_of_work._session is session


def test_dispatcher_builder_uses_aggregate_readers_and_sender() -> None:
    """The delivery builder wires reminder, reader, account, UoW, and sender ports."""
    session = make_session()
    sender = FakeSender()

    dispatcher = build_reminder_dispatcher(
        session,
        sender,
        clock=fixed_now,
        monotonic=lambda: 0.0,
    )

    assert isinstance(dispatcher._reminder_repository, SqlAlchemyReminderRepository)
    assert isinstance(dispatcher._task_reader, SqlAlchemyTaskRepository)
    assert isinstance(dispatcher._series_reader, SqlAlchemySeriesRepository)
    assert isinstance(dispatcher._project_reader, SqlAlchemyProjectRepository)
    assert isinstance(dispatcher._account_repository, SqlAlchemyAccountRepository)
    assert isinstance(dispatcher._unit_of_work, SqlAlchemyUnitOfWork)
    assert dispatcher._unit_of_work._session is session
    assert dispatcher._sender is sender


def test_palette_builder_binds_the_supplied_session() -> None:
    """The admin factory binds a palette service to the caller's session."""
    session = make_session()

    service = build_palette_service(session)

    assert isinstance(service, PaletteService)
    assert isinstance(service._repository, SqlAlchemyPaletteRepository)
    assert service._repository._session is session


def test_auth_composition_builds_auth_service() -> None:
    """Auth composition maps settings and selects the auth adapters."""
    session = make_session()
    settings = make_settings()

    service = build_auth_service(session, settings, clock=fixed_now)

    assert isinstance(service, AuthService)
    assert service._repository.__class__.__name__ == "SqlAlchemyAuthRepository"
    assert isinstance(service._unit_of_work, SqlAlchemyUnitOfWork)
    assert service._unit_of_work._session is session
    assert service._settings.telegram_bot_token == "123:secret"
    assert service._settings.auth_jwt_secret == "a" * 32
    assert service._settings.auth_jwt_issuer == settings.auth_jwt_issuer
    assert (
        service._settings.telegram_init_data_max_age_seconds
        == settings.telegram_init_data_max_age_seconds
    )
    assert service._token_issuer.__class__.__name__ == "JwtTokenIssuer"
    assert service._validator.__class__.__name__ == "PydanticInitDataValidator"


async def test_api_service_dependencies_use_the_composition_root() -> None:
    """API dependencies build application services from composition factories."""
    session = make_session()

    palette = await get_palette_service(session)
    projects = await get_project_service(session)
    labels = await get_label_service(session)
    series = await get_series_service(session)
    tasks = await get_task_service(session)

    assert isinstance(palette, PaletteService)
    assert isinstance(projects, ProjectService)
    assert isinstance(labels, LabelService)
    assert isinstance(series, SeriesService)
    assert isinstance(tasks, TaskService)
    assert cast(Any, palette)._repository._session is session
    assert cast(Any, projects)._repository._session is session
    assert cast(Any, labels)._repository._session is session
    assert cast(Any, series)._unit_of_work._session is session
    assert cast(Any, tasks)._unit_of_work._session is session
    assert cast(Any, tasks)._project_reader.__class__ is SqlAlchemyProjectRepository


async def test_auth_dependency_uses_auth_composition() -> None:
    """The auth dependency builds AuthService with mapped application settings."""
    settings = make_settings()
    session = make_session()

    from backend.api.deps import get_auth_service

    service = await get_auth_service(make_request(settings), session)

    assert isinstance(service, AuthService)
    assert cast(Any, service)._unit_of_work._session is session
    secret = settings.auth_jwt_secret
    assert secret is not None
    assert service._settings.auth_jwt_secret == secret.get_secret_value()


def test_auth_composition_builds_a_token_verifier() -> None:
    """Auth composition selects the JWT verifier adapter."""
    verifier = build_token_verifier(make_settings())

    assert isinstance(verifier, JwtTokenVerifier)


async def test_current_access_uses_the_app_state_token_verifier() -> None:
    """The bearer dependency verifies tokens through the composed port."""
    settings = make_settings()
    token, _ = create_access_token(
        7,
        11,
        7007,
        settings=settings,
        now=datetime.now(UTC),
    )

    claims = await get_current_access(
        make_request(settings),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
    )

    assert claims.user_id == 7
    assert claims.session_id == 11
    assert claims.telegram_id == 7007


def test_taskiq_entrypoints_use_the_composition_root() -> None:
    """Taskiq entrypoints build services through composition, not adapters."""
    generation = Path("src/backend/tasks/generation.py").read_text()
    delivery = Path("src/backend/tasks/delivery.py").read_text()

    assert "build_series_generation_service" in generation
    assert "SqlAlchemySeriesRepository" not in generation
    assert "build_reminder_dispatcher" in delivery


def test_compatibility_modules_reexport_new_layers() -> None:
    """Legacy module paths expose the application, domain, and infrastructure symbols."""
    from backend.application.ports.auth import AuthRepository
    from backend.auth.repository import AuthRepository as LegacyAuthRepository
    from backend.auth.repository import (
        SqlAlchemyAuthRepository as LegacyAuthRepositoryAdapter,
    )
    from backend.auth.service import AuthService as LegacyAuthService
    from backend.infrastructure.persistence.sqlalchemy.auth_repository import (
        SqlAlchemyAuthRepository,
    )
    from backend.task_management.labels import LabelService as LegacyLabelService
    from backend.task_management.palette import PaletteService as LegacyPaletteService
    from backend.task_management.projects import ProjectService as LegacyProjectService
    from backend.task_management.series import SeriesService as LegacySeriesService
    from backend.task_management.tasks import TaskService as LegacyTaskService

    assert LegacyAuthService is AuthService
    assert LegacyAuthRepository is AuthRepository
    assert LegacyAuthRepositoryAdapter is SqlAlchemyAuthRepository
    assert LegacyPaletteService is PaletteService
    assert LegacyProjectService is ProjectService
    assert LegacyLabelService is LabelService
    assert LegacySeriesService is SeriesService
    assert LegacyTaskService is TaskService


def test_telegram_modules_reexport_the_reminder_sender_contract() -> None:
    """The old Taskiq telegram path exposes the infrastructure sender and port alias."""
    from backend.tasks import telegram as legacy_telegram

    assert legacy_telegram.TelegramBotSender is TelegramBotSender
    assert legacy_telegram.TelegramSender is ReminderSender


def test_fixed_now_is_timezone_aware() -> None:
    """The shared fake clock is deterministic and aware."""
    assert fixed_now().tzinfo is UTC
    assert isinstance(fixed_now(), datetime)
