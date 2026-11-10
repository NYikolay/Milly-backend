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


def _imports(path: Path) -> set[str]:
    """Collect absolute module names imported by one Python source file."""
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_domain_and_application_do_not_import_outer_layers() -> None:
    """Inner layers never depend on frameworks, adapters, or the composition root."""
    roots = [Path("src/backend/domain"), Path("src/backend/application")]
    for root in roots:
        for path in root.rglob("*.py"):
            for imported in _imports(path):
                assert not imported.startswith(FORBIDDEN), f"{path}: {imported}"
