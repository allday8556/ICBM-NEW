"""ADR-0017 §13: the prototype is isolated. Nothing imports it; it imports nothing that acts."""

import ast
import socket
from pathlib import Path

import pytest

from prototypes.adaptive_collector.tests.conftest import NetworkRefused

REPO = Path(__file__).resolve().parents[3]
PROTOTYPE = REPO / "prototypes" / "adaptive_collector"
ALLOWED_STDLIB = {
    "ast",
    "collections",
    "copy",
    "dataclasses",
    "enum",
    "hashlib",
    "html",
    "inspect",
    "json",
    "pathlib",
    "re",
    "socket",  # tests only: to refuse it
    "struct",
    "typing",
    "urllib",
}
ALLOWED_THIRD_PARTY = {"pydantic", "pytest"}
# The only production module the prototype may import: the pure COLLECT value models.
ALLOWED_PRODUCTION = {"app.collect.facts"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_prototype_imports_only_value_models_stdlib_and_itself() -> None:
    for path in PROTOTYPE.rglob("*.py"):
        for name in _imports(path):
            top = name.split(".")[0]
            if name.startswith("prototypes.adaptive_collector") or name in ALLOWED_PRODUCTION:
                continue
            assert top in ALLOWED_STDLIB | ALLOWED_THIRD_PARTY, f"{path.name}: {name}"
            assert name not in {"urllib.request", "http.client"}, f"{path.name}: {name}"
            if top == "socket":
                assert path.parent.name == "tests", f"{path.name}: socket outside the tests"


def test_nothing_in_production_scripts_or_tests_imports_the_prototype() -> None:
    for root in ("app", "integrations", "scripts", "tests"):
        for path in (REPO / root).rglob("*.py"):
            for name in _imports(path):
                assert not name.startswith("prototypes"), f"{path}: {name}"


def test_the_network_is_refused_inside_every_prototype_test() -> None:
    with pytest.raises(NetworkRefused):
        socket.create_connection(("127.0.0.1", 9))
