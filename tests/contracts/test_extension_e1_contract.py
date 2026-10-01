"""Repository pins of the extension capture transport, slice E1 (ADR-0019 §10; Issue #126 rulings
5906290729 and 5906712259; owner amendment 5907095955 of the E1 specification 5907009512).

These read the repository as text and structure. They hold what E1 is allowed to be: a transport
with two routes and no CORS, a client with no storage or download reach, a job that is never
replayed, and an owner that appends nothing.
"""

import ast
import json
import re
from pathlib import Path

from app.capabilities.jobs.worker import JobWorker
from app.stages.collect.extension import service
from app.stages.collect.extension.nonces import NONCE_TTL_S
from app.stages.collect.extension.pairing import TIMESTAMP_WINDOW_S
from app.stages.collect.extension.policy import MAX_HTML_BYTES, MAX_IMAGE_REFS, MAX_NODES
from integrations.suppliers.base import SupplierTransport

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION = REPO_ROOT / "ui" / "extension"
OWNER = REPO_ROOT / "app" / "stages" / "collect" / "extension"
ROUTER = REPO_ROOT / "app" / "interface" / "api" / "routes" / "collect_extension.py"
DOCUMENTS = REPO_ROOT / "documents"
EXTENSION_TESTS = REPO_ROOT / "tests" / "integration" / "collect" / "extension"


def _read(path: Path) -> str:
    return path.read_text("utf-8")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


# ---------------------------------------------------------------- the client


def test_the_manifest_is_exactly_the_reviewed_one() -> None:
    manifest = json.loads(_read(EXTENSION / "manifest.json"))
    # Owner amendment 5909645067 §2, replacing the 109 of E1 specification §11.3: the literal.
    # PerformanceResourceTiming.responseStatus needs Chrome 109 and chrome.sidePanel Chrome 114,
    # so 114 is the lowest version that meets the whole contract. There is no older fallback.
    assert manifest["minimum_chrome_version"] == "114"
    assert '"minimum_chrome_version": "114"' in _read(EXTENSION / "manifest.json")
    assert manifest["manifest_version"] == 3
    # Ruling B-11: the reviewed supplier host and the loopback, and nothing else.
    assert manifest["host_permissions"] == ["https://kmretail.co.kr/*", "http://127.0.0.1/*"]
    # No cookie, request, download, history or tab-content reach.
    assert manifest["permissions"] == ["scripting", "sidePanel", "storage"]
    assert "optional_permissions" not in manifest and "optional_host_permissions" not in manifest
    assert "content_scripts" not in manifest and "web_accessible_resources" not in manifest
    assert "externally_connectable" not in manifest
    assert manifest["background"] == {"service_worker": "service_worker.js", "type": "module"}


def test_the_extension_is_plain_modules_with_no_toolchain() -> None:
    # Ruling B-8: no npm, no build step, no new dependency, and no new top-level directory.
    names = {
        path.relative_to(EXTENSION).as_posix() for path in EXTENSION.rglob("*") if path.is_file()
    }
    assert names == {
        "manifest.json",
        "service_worker.js",
        "sidepanel.html",
        "sidepanel.css",
        "sidepanel.js",
        "lib/capture.js",
        "lib/client.js",
        "lib/signing.js",
    }
    assert not (REPO_ROOT / "extension").exists()
    for forbidden in ("package.json", "package-lock.json", "node_modules", "tsconfig.json"):
        assert not list(REPO_ROOT.glob(forbidden)) and not list(EXTENSION.rglob(forbidden))


def test_the_extension_reads_no_cookie_storage_or_header_and_downloads_nothing() -> None:
    # ADR-0019 §3, §12.4–§12.6 (AC-07, AC-22, AC-23, AC-24).
    forbidden = (
        "document.cookie",
        "chrome.cookies",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "chrome.webRequest",
        "chrome.declarativeNetRequest",
        "chrome.downloads",
        "chrome.history",
        "chrome.debugger",
        "XMLHttpRequest",
        "createObjectURL",
        "download",
        'credentials: "include"',
        "innerHTML",
        "outerHTML",
        "eval(",
        "new Function",
    )
    for path in (
        *EXTENSION.glob("*.js"),
        *EXTENSION.glob("lib/*.js"),
        EXTENSION / "sidepanel.html",
    ):
        text = _read(path)
        for word in forbidden:
            assert word not in text, f"{path.name}: {word}"
    # The function injected into the tab talks to nothing: it returns, and the service worker sends.
    capture = "\n".join(
        line
        for line in _read(EXTENSION / "lib" / "capture.js").splitlines()
        if not line.lstrip().startswith("//")
    )
    for word in ("fetch(", "chrome.", "import ", "WebSocket", "sendBeacon", "postMessage"):
        assert word not in capture, word
    # Every request to ICBM omits credentials and refuses a redirect.
    client = _read(EXTENSION / "lib" / "client.js")
    assert client.count("fetch(") == client.count('credentials: "omit"') == 2
    assert client.count('redirect: "error"') == 2
    # The only thing the extension stores is its pairing (ruling B-2; owner amendment 5909645067
    # §2): one storage area, one key, in every file of the extension. There is no session storage
    # and no window the side panel falls back to.
    worker = _read(EXTENSION / "service_worker.js")
    everything = "\n".join(_read(path) for path in sorted(EXTENSION.rglob("*.js")))
    assert set(re.findall(r"chrome\.storage\.(\w+)", everything)) == {"local"}
    assert re.findall(r"chrome\.storage\.local\.(\w+)\(", everything) == ["get", "set", "remove"]
    assert re.findall(r"chrome\.storage\.local\.set\(\{ \[(\w+)\]", worker) == ["PAIRING_KEY"]
    assert "chrome.storage.local.get(PAIRING_KEY)" in worker
    assert "chrome.storage.local.remove(PAIRING_KEY)" in worker
    for gone in ("chrome.windows", "TARGET_TAB", "if (chrome.sidePanel)"):
        assert gone not in everything, gone


def test_the_side_panel_keeps_the_three_axes_apart() -> None:
    # ADR-0019 §12.3–§12.4 (AC-21, AC-22): a transport state, a run outcome and a code are three
    # things, and a run outcome is shown only when ICBM read it back.
    panel = _read(EXTENSION / "sidepanel.html")
    for role in ("transport-state", "run-outcome", "run-code", "run-id"):
        assert f'data-role="{role}"' in panel
    script = _read(EXTENSION / "sidepanel.js")
    assert 'result.state === "READ_BACK" ? result.outcome : "—"' in script
    # No outcome word is ever written by the extension itself, and REVIEW and AUTH are no states.
    for source in (script, _read(EXTENSION / "service_worker.js")):
        for word in ('"RECORDED"', '"NO_REVISION"', '"FAILED"', "REVIEW_REQUIRED", '"AUTH"'):
            assert word not in source, word
    assert "전송됨" in script and "처리 중" in script


# ---------------------------------------------------------------- the surface


def test_the_extension_surface_is_two_routes_and_their_preflight() -> None:
    tree = ast.parse(_read(ROUTER))
    routes = sorted(
        (decorator.func.attr, ast.unparse(decorator.args[0]))  # type: ignore[attr-defined]
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
    )
    assert routes == [
        ("get", "POLICY_PATH"),
        ("options", "CAPTURE_PATH"),
        ("options", "POLICY_PATH"),
        ("post", "CAPTURE_PATH"),
    ]
    source = _read(ROUTER)
    assert 'POLICY_PATH = "/api/v1/collect/extension/capture-policies/{supplier_key}"' in source
    assert 'CAPTURE_PATH = "/api/v1/collect/extension/captures"' in source


def test_the_application_gains_no_cors() -> None:
    # Ruling B-3: no broad CORS. Only the extension router ever names an allowed origin.
    naming = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        text = _read(path)
        assert "CORSMiddleware" not in text, path
        if "Access-Control-Allow-Origin" in text:
            naming.append(path.relative_to(REPO_ROOT).as_posix())
    assert naming == ["app/interface/api/routes/collect_extension.py"]
    source = _read(ROUTER)
    assert '"Access-Control-Allow-Origin": "*"' not in source and "Allow-Credentials" not in source
    # The DIRECT_URL submit's router is not where the ingest lives.
    collect = _read(REPO_ROOT / "app" / "interface" / "api" / "routes" / "collect.py")
    assert "extension" not in collect.lower().replace("an extension capture", "")


# ---------------------------------------------------------------- the owner


def test_the_ingest_owner_appends_nothing_and_reaches_no_network() -> None:
    modules = sorted(OWNER.glob("*.py"))
    assert {path.name for path in modules} == {
        "__init__.py",
        "buffer.py",
        "capture.py",
        "gate.py",
        "nonces.py",
        "pairing.py",
        "policy.py",
        "service.py",
    }
    forbidden = {
        "app.stages.collect.revisions",
        "app.stages.collect.sourceassets",
        "app.stages.collect.assets",
        "app.stages.collect.readback",
        "integrations.suppliers.transport.gateway",
        "httpx",
        "requests",
        "socket",
        "urllib.request",
        "playwright",
    }
    for path in modules:
        imported = _imports(path)
        assert not imported & forbidden, path.name
        assert not [name for name in imported if name.startswith("app.stages.collect.adaptive")]
        assert not [name for name in imported if name.startswith("app.stages.register")]
    source = _read(OWNER / "service.py")
    # No canonical writer is called: no revision append, no asset record, no product read reserved.
    for call in (
        ".append(collected",
        "_revisions",
        "recorder",
        "reserve_product_read",
        "read_image",
    ):
        assert call not in source, call
    assert ".recorded(" not in source and "RECORDED" not in source.replace("``RECORDED``", "")


def test_the_capture_job_is_never_replayed() -> None:
    # Ruling N-1: non-idempotent, one attempt, no retry of the capture work.
    assert service.EXTENSION_CAPTURE_POLICY.max_attempts == 1
    source = _read(OWNER / "service.py")
    assert "idempotent=False" in source and "idempotent=True" not in source
    assert service.EXTENSION_CAPTURE_JOB == "collect.extension_capture"


def test_the_handoff_is_pinned_to_the_in_process_worker() -> None:
    # Ruling N-1, test 5: the buffer is valid only while ADR-0002 Option A holds. A change of the
    # worker's placement fails here until the extension handoff contract is changed with it.
    adr = _read(DOCUMENTS / "decisions" / "adr" / "0002-job-worker-placement.md")
    assert "Choose **Option A for ICBM-NEW v1**" in adr
    assert "A — worker runs as a background task inside the FastAPI process" in adr
    assert JobWorker.IN_PROCESS is True
    container = _read(REPO_ROOT / "app" / "container.py")
    assert "worker = JobWorker(" in container
    assert "worker_in_process=worker.IN_PROCESS" in container
    assert "await services.worker.start()" in _read(REPO_ROOT / "app" / "main.py")
    assert "EXTENSION_WORKER_NOT_IN_PROCESS" in _read(OWNER / "service.py")


def test_the_ceilings_are_the_decided_ones() -> None:
    # Ruling B-4, for E1 only.
    assert (MAX_HTML_BYTES, MAX_NODES, MAX_IMAGE_REFS) == (512 * 1024, 20_000, 40)
    assert service.MINIMUM_INGEST_INTERVAL_S == 5.0
    # E1 specification §2.2: a nonce outlives the window in which its request is accepted.
    assert NONCE_TTL_S >= 2 * TIMESTAMP_WINDOW_S
    # The transport is admitted, and the server-side browser one still is not a collection one.
    assert {member.value for member in SupplierTransport} == {"HTTP", "BROWSER", "EXTENSION"}


# ---------------------------------------------------------------- the canonical documents


def test_the_glossary_names_what_e1_introduced() -> None:
    glossary = _read(DOCUMENTS / "architecture" / "GLOSSARY.md")
    for term in (
        "transport_kind",
        "capture_policy_revision",
        "capture_policy_digest",
        "NO_BUNDLE",
        "EXTENSION_COMPARE_ONLY",
        "EXTENSION_CAPTURE_BUFFER_MISSING",
    ):
        assert f"| `{term}` |" in glossary, term
    # The codes the code writes are the glossary's own.
    assert service.EXTENSION_COMPARE_ONLY == "EXTENSION_COMPARE_ONLY"
    assert service.EXTENSION_CAPTURE_BUFFER_MISSING == "EXTENSION_CAPTURE_BUFFER_MISSING"
    assert service.NO_BUNDLE == "NO_BUNDLE"
    row = next(line for line in glossary.splitlines() if line.startswith("| `NO_BUNDLE` |"))
    assert "**not** a comparison PASS" in row


def test_the_acceptance_record_is_accepted_and_hides_nothing() -> None:
    record = _read(DOCUMENTS / "acceptance" / "adaptive" / "EXTENSION-E1.md")
    assert record.splitlines()[2].startswith("- Status: **ACCEPTED")
    # The one real acceptance is named by its exact main and run, with compare-only truth.
    accepted = record.split("### 5.2 The acceptance", 1)[1]
    for fact in (
        "28b59979dc856926a65d4ff6e3512d0d8a1725fe",
        "02aa6058-9a44-4275-b45e-65e734b48c83",
        "`NO_REVISION` / `EXTENSION_COMPARE_ONLY`",
        "`NO_BUNDLE`",
        "It is not a comparison PASS and\n  not `VALIDATED`",
        "**Zero write:**",
    ):
        assert fact in accepted, fact
    for source in (
        "5906290729",
        "5906712259",
        "5907095955",
        "5907009512",
        "5909645067",
        "5909188774",
    ):
        assert source in record, source
    # Owner amendment 5909645067 §4: the incident is never retroactively authorized, is never
    # acceptance evidence, and no document claims a zero-request history.
    for phrase in (
        "are not retroactively authorized",
        "are not acceptance evidence",
        "do not consume, replace or widen the future acceptance grant",
        "No document may say that the whole implementation history made zero supplier requests",
        "the implementation **history** is\n  not",
    ):
        assert phrase in record, phrase
    assert "bb9906ccd61cac270908ad64de50be98a645bbec74313be5c3e5d577bed2fcf4" in record
    # The real acceptance is not authorized here, and the operator's preconditions are stated.
    assert "not authorized" in record and "Claude never browses the supplier" in record
    assert "`location.href == navigation entry.name`" in record and "`redirectCount == 0`" in record
    assert "the authorized click is not consumed" in record
    # The two unintended requests of the implementation are on the record.
    incident = record.split("## 6. Incident during implementation", 1)[1]
    assert "two HTTP requests reached" in incident and "Neither was authorized" in incident
    roadmap = _read(DOCUMENTS / "roadmap" / "ROADMAP.md")
    assert "**E1 — one click, compare only — is implemented provider-zero**" in roadmap
    e1_line = roadmap.split("**E1 —", 1)[1].split("\n", 1)[0]
    assert "**E1 is accepted**" in e1_line and "`PENDING`" not in e1_line
    assert "E2, E3 and the extension-transport Phase C are not authorized" in roadmap


def test_no_extension_browser_test_can_leave_the_loopback() -> None:
    # Every browser a repository test launches comes from ``tests/support/browser.py`` and
    # resolves no host name (``test_repository_rules``: the repository-wide rule). A launch
    # without the block is how a routed redirect once reached the supplier's host
    # (EXTENSION-E1.md §6). The extension tests launch nothing themselves.
    for path in sorted(EXTENSION_TESTS.glob("test_*.py")):
        text = _read(path)
        assert ".launch" not in text and "host-resolver-rules" not in text, path.name
    assert "launch_browser(playwright)" in _read(
        EXTENSION_TESTS / "test_extension_capture_browser.py"
    )
    assert "launch_extension_context(" in _read(EXTENSION_TESTS / "test_extension_e2e.py")
    # A redirect is never answered from a route, because its follow-up request is not routed.
    # The one routed 302 is the test that proves the block: it points at a name that cannot
    # resolve.
    routed = {
        path.name: _read(path).count("status=302") for path in EXTENSION_TESTS.glob("test_*.py")
    }
    assert {name: count for name, count in routed.items() if count} == {
        "test_extension_capture_browser.py": 1
    }
