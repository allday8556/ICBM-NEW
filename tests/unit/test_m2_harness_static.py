"""Static guards for the M2 acceptance harness (Issue #46 §2.1; comment 5669037896 point 1).

The harness owns no SmartStore client. It builds the live transport in one guarded place, spells
no SmartStore host or path, opens no egress grant, follows no redirect, patches no product module,
and talks HTTP only to its own application processes on loopback. Production code never imports
it, and only the crash child replaces the commit boundary.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_FILES = [
    *sorted((REPO_ROOT / "scripts" / "m2harness").glob("*.py")),
    REPO_ROOT / "scripts" / "m2_acceptance.py",
]
PRODUCTION_ROOTS = [REPO_ROOT / "app", REPO_ROOT / "integrations"]
# The wire pattern the repository rules apply to production code (test_repository_rules.py).
_WIRE = re.compile(r"commerce\.naver\.com|/external\b|^/v\d+/|/oauth2/|/seller/", re.I)
_OTHER_CLIENTS = {
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
    "websockets",
    "playwright",
    "selenium",
}


def _trees() -> dict[str, ast.Module]:
    return {
        path.relative_to(REPO_ROOT).as_posix(): ast.parse(path.read_text("utf-8"))
        for path in HARNESS_FILES
    }


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _callee(node) == name]


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _code_strings(tree: ast.Module) -> list[str]:
    """String literals that are code, not documentation (docstrings are skipped)."""
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_live_transport_is_built_in_one_guarded_place_only() -> None:
    trees = _trees()
    builders = [
        (path, call) for path, tree in trees.items() for call in _calls(tree, "HTTPTransport")
    ]
    assert [path for path, _ in builders] == ["scripts/m2harness/transport.py"]
    [(_, call)] = builders
    assert call.args == []
    assert [(k.arg, getattr(k.value, "value", None)) for k in call.keywords] == [
        ("trust_env", False)
    ]
    # The factory checks for CI and pytest when it is created and again when it builds.
    live_inner = next(
        node
        for node in ast.walk(trees["scripts/m2harness/transport.py"])
        if isinstance(node, ast.FunctionDef) and node.name == "live_inner"
    )
    assert len(_calls(live_inner, "ci_or_test")) == 2
    # Only the application-process entry uses it, and only for a REAL ledger.
    users = [(path, call) for path, tree in trees.items() for call in _calls(tree, "live_inner")]
    assert [path for path, _ in users] == ["scripts/m2harness/cli.py"]
    branch = next(
        node
        for node in ast.walk(trees["scripts/m2harness/cli.py"])
        if isinstance(node, ast.If)
        and any(_calls(statement, "live_inner") for statement in node.body)
    )
    assert ast.unparse(branch.test) == "campaign.mode is Mode.REAL"


def test_the_harness_talks_http_only_to_its_own_loopback_processes() -> None:
    for path, tree in _trees().items():
        assert not _imports(tree) & _OTHER_CLIENTS, path
        for call in _calls(tree, "Client") + _calls(tree, "AsyncClient"):
            assert path == "scripts/m2harness/campaign.py", path
            [base] = [keyword.value for keyword in call.keywords if keyword.arg == "base_url"]
            assert isinstance(base, ast.JoinedStr)
            first = base.values[0]
            assert isinstance(first, ast.Constant) and first.value == "http://127.0.0.1:"


def test_the_harness_spells_no_smartstore_host_or_path() -> None:
    offenders = [
        f"{path}: {value}"
        for path, tree in _trees().items()
        for value in _code_strings(tree)
        if _WIRE.search(value)
    ]
    assert offenders == []


def test_the_harness_opens_no_grant_follows_no_redirect_and_patches_nothing() -> None:
    for path, tree in _trees().items():
        assert _calls(tree, "grant") == [], path
        assert _calls(tree, "setattr") == [], path
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            for keyword in call.keywords:
                if keyword.arg == "follow_redirects":
                    value = keyword.value
                    assert isinstance(value, ast.Constant) and value.value is False, path


def test_production_code_never_imports_the_harness() -> None:
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for root in PRODUCTION_ROOTS
        for path in root.rglob("*.py")
        if any(
            name == "scripts" or name.startswith("scripts.")
            for name in _imports(ast.parse(path.read_text("utf-8")))
        )
    ]
    assert offenders == []


def test_only_the_crash_child_replaces_the_commit_boundary() -> None:
    writers = []
    for path, tree in _trees().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr.startswith("_")
                    and not (isinstance(target.value, ast.Name) and target.value.id == "self")
                ):
                    writers.append((path, target.attr))
    assert writers == [("scripts/m2harness/crash.py", "_commit")]
