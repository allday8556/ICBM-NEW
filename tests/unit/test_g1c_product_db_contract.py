"""Static rules of the product DB read path (Gate 1 G1-C, ADR-0015 §5, Issue #89 5792525426).

The operator's list, search, detail and registration-target check read canonical state and write
nothing: every product route is a GET, and the product DB read owner never opens a write unit of
work. Each rule runs on the repository and on a synthetic violation, so a rule that could never
fire is caught as surely as one that fails.
"""

import ast
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ROUTES = "app/api/routes/products.py"
READ_OWNERS = ("app/products/service.py", "app/products/catalog.py")
WRITE_OPENERS = frozenset({"transaction", "write"})
PRODUCT_DB_VIEWS = "app/products/contracts.py"
# What G1-C's read models never carry: a price, a readiness or compliance verdict, a marketplace.
FORBIDDEN_FIELDS = ("price", "readiness", "ready", "registrable", "compliance", "marketplace")


def _source(where: str) -> str:
    return (REPO_ROOT / where).read_text("utf-8")


def route_method_problems(where: str, source: str) -> list[str]:
    """A product route registered with anything other than GET."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            call = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(call, ast.Attribute) and call.attr != "get":
                offenders.append(f"{where}:{node.lineno} {node.name}")
    return offenders


def write_opener_problems(sources: Iterable[tuple[str, str]]) -> list[str]:
    """A read owner that opens a write unit of work."""
    offenders = []
    for where, source in sources:
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_OPENERS
            ):
                offenders.append(f"{where}:{node.lineno}")
    return offenders


def view_field_problems(source: str, views: Iterable[str]) -> list[str]:
    """A field of a product DB read model that would carry a price or a verdict."""
    wanted = set(views)
    offenders = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef) or node.name not in wanted:
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                name = statement.target.id
                if any(word in name for word in FORBIDDEN_FIELDS):
                    offenders.append(f"{node.name}.{name}")
    return offenders


G1C_VIEWS = (
    "SourceFactView",
    "SourceImagesView",
    "MemberSourceView",
    "ItemSelectionView",
    "ProductRowView",
    "ProductPageView",
    "ProductDetailView",
    "TargetItemView",
    "RegistrationTargetView",
)


def test_every_product_route_is_a_read() -> None:
    assert route_method_problems(PRODUCT_ROUTES, _source(PRODUCT_ROUTES)) == []


def test_the_route_method_detector_fires() -> None:
    synthetic = (
        "@router.get('/a')\ndef a(): ...\n"
        "@router.post('/b')\ndef b(): ...\n"
        "@router.put('/c')\ndef c(): ...\n"
    )
    assert route_method_problems("x.py", synthetic) == ["x.py:4 b", "x.py:6 c"]


def test_the_product_db_read_owner_opens_no_write() -> None:
    assert write_opener_problems((where, _source(where)) for where in READ_OWNERS) == []


def test_the_write_opener_detector_fires() -> None:
    sources = [
        ("app/products/service.py", "with self._store.transaction() as unit:\n    pass\n"),
        ("app/products/catalog.py", "with db.write() as session:\n    pass\n"),
        ("app/products/fine.py", "with self._store.reading() as unit:\n    pass\n"),
    ]
    assert write_opener_problems(sources) == [
        "app/products/service.py:1",
        "app/products/catalog.py:1",
    ]


def test_the_product_db_read_models_carry_no_price_or_verdict() -> None:
    views = _source(PRODUCT_DB_VIEWS)
    assert all(f"class {view}(" in views for view in G1C_VIEWS)
    assert view_field_problems(views, G1C_VIEWS) == []


def test_the_view_field_detector_fires() -> None:
    synthetic = (
        "class ProductRowView(BaseModel):\n"
        "    sale_price_krw: int\n"
        "    readiness: str\n"
        "    item_id: str\n"
        "class Other(BaseModel):\n"
        "    price: int\n"
    )
    assert view_field_problems(synthetic, ["ProductRowView"]) == [
        "ProductRowView.sale_price_krw",
        "ProductRowView.readiness",
    ]
