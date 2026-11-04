from typing import Any

import pytest

from backend.core.config import Settings
from backend.db.session import Database


def test_database_engine_receives_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the configured statement and pool timeouts to the async engine."""
    captured: dict[str, Any] = {}

    def fake_create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("backend.db.session.create_async_engine", fake_create_async_engine)
    monkeypatch.setattr("backend.db.session.async_sessionmaker", lambda *args, **kwargs: object())
    Database(Settings(_env_file=None))
    assert captured["pool_timeout"] == 10
    assert captured["connect_args"]["command_timeout"] == 30.0
    assert captured["connect_args"]["server_settings"]["statement_timeout"] == "30000"
    assert captured["connect_args"]["server_settings"]["application_name"] == "Milly API"
