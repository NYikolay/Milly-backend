"""Architecture guards for the layered backend."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN = (
    "fastapi",
    "starlette",
    "sqlalchemy",
    "taskiq",
    "pydantic",
    "pydantic_settings",
    "jwt",
    "backend.models",
    "backend.db",
    "backend.infrastructure",
    "backend.api",
    "backend.tasks",
    "backend.admin",
    "backend.composition",
    "backend.core.config",
    "backend.core.middleware",
    "backend.task_management",
    "backend.auth",
)

COMPATIBILITY_SHIMS = (
    Path("src/backend/task_management/enums.py"),
    Path("src/backend/task_management/date_bounds.py"),
    Path("src/backend/task_management/schedule.py"),
    Path("src/backend/task_management/tasks.py"),
    Path("src/backend/task_management/palette.py"),
    Path("src/backend/task_management/labels.py"),
    Path("src/backend/task_management/projects.py"),
    Path("src/backend/task_management/series.py"),
    Path("src/backend/task_management/reminders.py"),
    Path("src/backend/task_management/ports.py"),
    Path("src/backend/task_management/repository.py"),
    Path("src/backend/auth/service.py"),
    Path("src/backend/auth/repository.py"),
    Path("src/backend/auth/cookies.py"),
    Path("src/backend/auth/tokens.py"),
    Path("src/backend/auth/init_data.py"),
    Path("src/backend/auth/errors.py"),
    Path("src/backend/auth/schemas.py"),
    Path("src/backend/tasks/telegram.py"),
)

ORM_MODELS_MODULE = "backend.models"


def _imports(path: Path) -> set[str]:
    """Collect absolute module names imported by one Python source file."""
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
            if node.module == "backend":
                result.update(f"backend.{alias.name}" for alias in node.names)
    return result


def _defines_implementation(path: Path) -> bool:
    """Return whether a module defines its own class or function at module level."""
    tree = ast.parse(path.read_text())
    return any(
        isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
        for node in tree.body
    )


def _declares_all(path: Path) -> bool:
    """Return whether a module assigns ``__all__`` at module level."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            return True
    return False


def test_domain_and_application_do_not_import_outer_layers() -> None:
    """Inner layers never depend on frameworks, adapters, or the composition root."""
    roots = [Path("src/backend/domain"), Path("src/backend/application")]
    for root in roots:
        for path in root.rglob("*.py"):
            for imported in _imports(path):
                assert not imported.startswith(FORBIDDEN), f"{path}: {imported}"


def test_api_does_not_import_orm_models() -> None:
    """API schemas, mappers, and routes do not depend on persistence models."""
    for path in Path("src/backend/api").rglob("*.py"):
        for imported in _imports(path):
            assert imported != ORM_MODELS_MODULE, f"{path}: {imported}"
            assert not imported.startswith(f"{ORM_MODELS_MODULE}."), f"{path}: {imported}"


def test_compatibility_modules_are_shims_only() -> None:
    """Old module paths re-export new layers instead of owning implementations."""
    for path in COMPATIBILITY_SHIMS:
        assert not _defines_implementation(path), f"{path} defines its own implementation"
        assert _declares_all(path), f"{path} must declare __all__"
