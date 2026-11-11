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
)

REMOVED_LEGACY_MODULES = (
    Path("src/backend/task_management"),
    Path("src/backend/auth"),
    Path("src/backend/tasks/telegram.py"),
    Path("src/backend/application/ports/legacy.py"),
    Path("src/backend/infrastructure/persistence/sqlalchemy/legacy.py"),
    Path("src/backend/task_management/ports.py"),
    Path("src/backend/task_management/repository.py"),
)


ORM_MODELS_MODULE = "backend.models"


def _module_parts(path: Path) -> list[str]:
    """Return the dotted module parts of a file located under ``src``."""
    parts = list(path.with_suffix("").parts)
    if "src" not in parts:
        return []
    return parts[parts.index("src") + 1 :]


def _imports(path: Path) -> set[str]:
    """
    Collect absolute module names imported by one Python source file.

    Purpose:
        Feed architecture guards with every imported module, including names
        reached through ``from`` imports and relative imports.

    Notes:
        - Every ``ImportFrom`` alias also contributes ``module.alias`` so
          ``from backend.core import config`` is checked like
          ``import backend.core.config``.
        - Relative imports are resolved against the source file package when it
          lives under ``src``; imports beyond that package root are invalid and
          therefore skipped.
    """
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    module_parts = _module_parts(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = module_parts[:-1]
                if not package or node.level > len(package):
                    continue
                base = package[: len(package) - node.level + 1]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if not module:
                continue
            result.add(module)
            result.update(f"{module}.{alias.name}" for alias in node.names)
    return result


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


def test_api_does_not_import_concrete_infrastructure() -> None:
    """API dependencies choose concrete adapters only through composition."""
    for path in Path("src/backend/api").rglob("*.py"):
        for imported in _imports(path):
            assert not imported.startswith("backend.infrastructure"), f"{path}: {imported}"


def test_import_collector_checks_from_import_names(tmp_path: Path) -> None:
    """From-import aliases are checked as full module paths."""
    path = tmp_path / "src" / "backend" / "domain" / "sample.py"
    path.parent.mkdir(parents=True)
    path.write_text("from backend.core import config\n")

    assert "backend.core.config" in _imports(path)


def test_import_collector_resolves_relative_imports(tmp_path: Path) -> None:
    """Relative imports resolve against the package under ``src``."""
    path = tmp_path / "src" / "backend" / "application" / "sample.py"
    path.parent.mkdir(parents=True)
    path.write_text("from ..core.config import Settings\nfrom . import ports\n")

    imports = _imports(path)

    assert "backend.core.config.Settings" in imports
    assert "backend.application.ports" in imports


def test_removed_legacy_modules_are_absent() -> None:
    """Dead composite facades and compatibility packages are not kept around."""
    for path in REMOVED_LEGACY_MODULES:
        assert not path.exists(), f"{path} must be removed"


def test_domain_does_not_import_core_errors() -> None:
    """Domain policies are independent of the transport error registry."""
    for path in Path("src/backend/domain").rglob("*.py"):
        for imported in _imports(path):
            assert imported != "backend.core.errors", f"{path}: {imported}"
            assert not imported.startswith("backend.core.errors."), f"{path}: {imported}"


def test_application_does_not_import_concrete_adapters() -> None:
    """Application depends only on ports, domain, and shared kernel errors."""
    for path in Path("src/backend/application").rglob("*.py"):
        for imported in _imports(path):
            assert not imported.startswith("backend.infrastructure"), f"{path}: {imported}"
            assert not imported.startswith("backend.composition"), f"{path}: {imported}"


def test_infrastructure_does_not_import_the_composition_root() -> None:
    """Adapters never reach outward to the module that chooses them."""
    for path in Path("src/backend/infrastructure").rglob("*.py"):
        for imported in _imports(path):
            assert not imported.startswith("backend.composition"), f"{path}: {imported}"
