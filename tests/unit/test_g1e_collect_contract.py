"""Static rules of the COLLECT submit path (Gate 1 G1-E, ADR-0015 §5, Issue #89 5794763664).

The one write is the existing one-product submit; everything the screen adds is a read. The UI
keeps no second truth in browser storage, and its collection code issues exactly one POST, only
from the operator's own form submit. Each rule runs on the repository and on a synthetic
violation, so a rule that could never fire is caught as surely as one that fails.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECT_ROUTES = "app/api/routes/collect.py"
UI_JS = REPO_ROOT / "ui" / "web" / "js"
COLLECT_PAGE = UI_JS / "pages" / "collect.js"
SUBMIT = ("post", "/api/v1/collect/collections")
BROWSER_STORAGE = re.compile(r"\b(localStorage|sessionStorage|indexedDB)\b|document\.cookie")
COLLECT_POST = re.compile(r"sendJson\(\s*'POST'\s*,\s*RUNS\b")


def route_writes(source: str) -> list[tuple[str, str]]:
    """Every (method, path) registered with anything other than GET."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr != "get"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                found.append((decorator.func.attr, str(decorator.args[0].value)))
    return found


def storage_problems(sources: dict[str, str]) -> list[str]:
    return sorted(where for where, text in sources.items() if BROWSER_STORAGE.search(text))


def test_the_only_collect_write_is_the_one_product_submit() -> None:
    source = (REPO_ROOT / COLLECT_ROUTES).read_text("utf-8")
    assert route_writes(source) == [SUBMIT]


def test_the_route_write_detector_fires() -> None:
    synthetic = (
        "@router.post('/api/v1/collect/collections')\ndef a(): ...\n"
        "@router.get('/api/v1/collect/collections')\ndef b(): ...\n"
        "@router.post('/api/v1/collect/collections/{x}/retry')\ndef c(): ...\n"
    )
    assert route_writes(synthetic) == [SUBMIT, ("post", "/api/v1/collect/collections/{x}/retry")]


def test_the_ui_keeps_nothing_in_browser_storage() -> None:
    sources = {str(p.relative_to(REPO_ROOT)): p.read_text("utf-8") for p in UI_JS.rglob("*.js")}
    assert storage_problems(sources) == []


def test_the_browser_storage_detector_fires() -> None:
    assert storage_problems(
        {
            "a.js": "localStorage.setItem('run', id);",
            "b.js": "const x = sessionStorage.getItem('url');",
            "c.js": "getJson('/api/v1/collect/collections');",
        }
    ) == ["a.js", "b.js"]


def test_the_collect_page_posts_a_collection_from_one_place_only() -> None:
    assert len(COLLECT_POST.findall(COLLECT_PAGE.read_text("utf-8"))) == 1
