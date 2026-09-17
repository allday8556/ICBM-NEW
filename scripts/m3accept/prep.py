"""PREP for ``m3-accept-01``: arming, the gates before any request, and the REAL environment.

Arming binds the campaign to one exact commit, one target and one frozen budget. Before anything is
persisted — no manifest, no ceiling, and so no approval can exist — it refuses when:

* the budget and the production profile disagree (the campaign never changes the profile);
* the data directory is not the campaign's own ``<root>/data``: an ordinary ICBM data directory,
  or a path that resolves anywhere else, is not a substitute for it;
* the M1 connection owner in that directory does not hold a locally usable connection and session.
  This is read from the owner's own local state with every transport refusing — arming never calls
  ``collection_session`` or ``verify`` and never sends a request. Establishing the connection there
  is an ordinary CONNECT operation outside the campaign; the harness never automates a login.

The PREP gates are evaluated before a pass starts and before any transport exists. None of them
sends anything: the M1 session check reads the connection owner's own local state, and the hard-
zero check reads source files.
"""

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.collect.collection import RegisteredCollection
from app.config import AppConfig, database_path
from app.container import build_container
from app.core.ownership import acquire_data_dir
from integrations.suppliers.collection import ReadKind, SupplierCollection
from integrations.suppliers.transport.collection import (
    CollectionTargetRefused,
    DeferredCollectionGateway,
    check_target,
    ci_or_test,
)
from scripts.m2harness.gates import REPO_ROOT, Checkout, dedicated_problems
from scripts.m3accept.campaign import Environment
from scripts.m3accept.ledger import CampaignLedger
from scripts.m3accept.m1 import NoTraffic, m1_session_problems
from scripts.m3accept.manifest import (
    CAMPAIGN_ID,
    EXCLUDED_IMAGE_HOSTS,
    M3_ACCEPT_01_BUDGET,
    CampaignBudget,
    Manifest,
    approval_phrase,
    byte_facts,
    target_digest,
)

# The modules no part of a campaign may import: AI, OCR and marketplace code. The repository
# rules pin this list to the one that guards the COLLECT source-truth path, so the two never drift.
HARD_ZERO_MODULES = (
    "anthropic",
    "openai",
    "google.generativeai",
    "google.genai",
    "vertexai",
    "cohere",
    "mistralai",
    "groq",
    "ollama",
    "litellm",
    "langchain",
    "llama_index",
    "transformers",
    "torch",
    "tensorflow",
    "onnxruntime",
    "pytesseract",
    "tesserocr",
    "easyocr",
    "paddleocr",
    "cv2",
    "azure.ai",
    "azure.cognitiveservices",
    "app.ai",
    "integrations.ai",
    "integrations.marketplaces",
    "app.connect.marketplace",
    "app.connect.smartstore",
)
HARD_ZERO_ROOTS = ("app/collect", "integrations/suppliers", "scripts/m3accept")
DATA_DIR_NAME = "data"


class ArmingRefused(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str = ""


def canonical_target(collection: SupplierCollection, product_url: str) -> str:
    """The operator's URL as the production transport would read it, or a refusal."""
    try:
        return check_target(collection.profile, product_url, ReadKind.PRODUCT_READ)
    except CollectionTargetRefused as refused:
        raise ArmingRefused(
            [f"the target is not an accepted product URL: {refused.message}"]
        ) from None


def profile_problems(collection: SupplierCollection, budget: CampaignBudget) -> list[str]:
    """Where the frozen budget and the production profile disagree. Empty when they agree."""
    limits = collection.profile.limits
    problems = []
    if limits.max_image_bytes != budget.per_image_bytes:
        problems.append("the per-image byte bound differs from the production profile")
    if limits.max_new_image_bytes_per_run != budget.new_image_bytes_per_pass:
        problems.append("the per-pass byte total differs from the production profile")
    if limits.same_product_interval_s != budget.same_product_interval_s:
        problems.append("the same-product interval differs from the production profile")
    if EXCLUDED_IMAGE_HOSTS & collection.profile.image_hosts:
        problems.append("the production profile allows an image host this campaign excludes")
    return problems


def campaign_data_dir(ledger: CampaignLedger) -> Path:
    """The one data directory a campaign runs on: ``data`` beside its ledger."""
    return ledger.path.parent / DATA_DIR_NAME


def data_dir_problems(ledger: CampaignLedger, config: AppConfig) -> list[str]:
    """Why ``config`` is not the campaign's own data directory. Empty when it is."""
    expected = campaign_data_dir(ledger)
    given = Path(config.data_dir)
    if given.absolute() != expected.absolute():
        return ["the data directory is not this campaign's own <root>/data"]
    if given.is_symlink() or given.resolve() != expected.parent.resolve() / DATA_DIR_NAME:
        return ["the campaign data directory resolves somewhere else; a link is not a substitute"]
    return []


def local_m1_problems(env: Environment) -> list[str]:
    """The M1 owner's local answer, composed with transports that refuse every request."""
    kwargs: dict[str, Any] = {
        "collection_gateway": NoTraffic(),
        "supplier_gateway": NoTraffic(),
    }
    if env.secret_store is not None:
        kwargs["secret_store"] = env.secret_store
    if env.clock is not None:
        kwargs["clock"] = env.clock
    if env.registered is not None:
        kwargs["collections"] = env.registered
    if env.suppliers is not None:
        kwargs["suppliers"] = env.suppliers
    if not database_path(env.config.data_dir).is_file():
        return ["the campaign data directory holds no ICBM database, so no M1 connection"]
    with acquire_data_dir(env.config.data_dir, app_version="m3-accept-arm-check") as lease:
        container = build_container(env.config, ownership=lease, **kwargs)
        try:
            return m1_session_problems(container, env.supplier_key)
        finally:
            container.db.dispose()


def arm(
    ledger: CampaignLedger,
    *,
    env: Environment,
    product_url: str,
    phase_b_findings: Mapping[str, Any],
    mode: str,
    code_sha: str,
    budget: CampaignBudget = M3_ACCEPT_01_BUDGET,
) -> Manifest:
    """Bind the campaign once: exact SHA, target digest, frozen budget, proven byte facts.

    Every gate runs before the ledger is written, so a refused campaign has no manifest, no
    ceilings, and cannot be approved.
    """
    collection = env.collection
    problems = profile_problems(collection, budget)
    if len(code_sha) != 40 or any(c not in "0123456789abcdef" for c in code_sha):
        problems.append("the code SHA is a full 40-character commit")
    if mode not in ("REAL", "DRY"):
        problems.append("a campaign is REAL or DRY")
    problems += data_dir_problems(ledger, env.config)
    if not problems:
        problems += local_m1_problems(env)
    if problems:
        raise ArmingRefused(problems)
    manifest = Manifest(
        campaign_id=CAMPAIGN_ID,
        code_sha=code_sha,
        target_digest=target_digest(canonical_target(collection, product_url)),
        budget=budget,
        byte_facts=byte_facts(phase_b_findings, baseline=budget.product_evidence_baseline),
        mode=mode,
    )
    ledger.arm(manifest)
    return manifest


def typed_approval_matches(typed: str, code_sha: str) -> bool:
    """Whether the operator's own words are the exact phrase for this SHA. Nothing is generated."""
    return typed.strip() == approval_phrase(code_sha)


# ---------------------------------------------------------------- the gates


def hard_zero_problems(root: Path = REPO_ROOT) -> list[str]:
    """Every import of AI, OCR or marketplace code anywhere a campaign can reach."""
    problems = []
    for base in HARD_ZERO_ROOTS:
        for path in sorted((root / base).rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if any(name == f or name.startswith(f"{f}.") for f in HARD_ZERO_MODULES):
                        problems.append(f"{path.relative_to(root).as_posix()} imports {name}")
    return problems


def prep_gates(
    *,
    root: Path,
    manifest: Mapping[str, Any],
    checkout: Checkout,
    environ: Mapping[str, str],
) -> list[Gate]:
    """The gates a REAL pass needs before anything else happens."""
    head = checkout.head()
    blocker = ci_or_test(environ)
    dedicated = dedicated_problems(root, environ)
    hard_zero = hard_zero_problems()
    return [
        Gate("not a CI or test run", blocker is None, blocker or ""),
        Gate(
            "exact armed SHA checked out",
            head == manifest.get("code_sha"),
            f"HEAD {head[:12]} vs armed {str(manifest.get('code_sha'))[:12]}",
        ),
        Gate("clean working tree", checkout.dirty() == 0, f"{checkout.dirty()} changed paths"),
        Gate("dedicated campaign directory", not dedicated, "; ".join(dedicated)),
        Gate("no AI, OCR or marketplace import", not hard_zero, "; ".join(hard_zero)),
        Gate("REAL mode armed", manifest.get("mode") == "REAL", str(manifest.get("mode"))),
    ]


# ---------------------------------------------------------------- the REAL environment


def local_environment(
    config: AppConfig, *, collection: SupplierCollection, registered: RegisteredCollection
) -> Environment:
    """What arming composes: the campaign data directory, with every transport refusing."""
    return Environment(
        config=config,
        supplier_key=collection.supplier_key,
        collection=collection,
        collection_transport=NoTraffic,
        connect_transport=NoTraffic,
        registered=(registered,),
    )


def real_environment(
    config: AppConfig, *, collection: SupplierCollection, registered: RegisteredCollection
) -> Environment:
    """The production transports behind the campaign. It cannot be built under CI or pytest."""
    if (blocker := ci_or_test()) is not None:
        raise RuntimeError(f"a REAL campaign environment never exists under {blocker}")
    from integrations.suppliers.transport.gateway import PolicedSupplierGateway

    return Environment(
        config=config,
        supplier_key=collection.supplier_key,
        collection=collection,
        collection_transport=DeferredCollectionGateway,
        connect_transport=lambda: PolicedSupplierGateway(browser_channel=config.browser_channel),
        registered=(registered,),
    )
