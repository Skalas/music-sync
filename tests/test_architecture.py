"""Architecture guard: musicsync/application must not import musicsync/infrastructure."""

from __future__ import annotations

import ast
from pathlib import Path


def _application_sources() -> list[Path]:
    app_dir = Path(__file__).parent.parent / "musicsync" / "application"
    return list(app_dir.glob("*.py"))


def _imported_modules(path: Path) -> list[str]:
    """Return all top-level module names imported in *path* (via import / from...import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


def test_application_has_no_infrastructure_imports() -> None:
    """musicsync/application must never import from musicsync/infrastructure."""
    violations: list[str] = []
    for src in _application_sources():
        for mod in _imported_modules(src):
            if mod.startswith("musicsync.infrastructure"):
                violations.append(f"{src.name}: imports {mod}")

    assert not violations, (
        "Layer violation — application imports infrastructure:\n"
        + "\n".join(violations)
    )
