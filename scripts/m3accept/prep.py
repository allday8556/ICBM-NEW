"""PREP for ``m3-accept-01``: arming, the gates before any request, and the REAL environment.

Arming binds the campaign to one exact commit, one target and one frozen budget, and refuses when
the budget and the production profile disagree — the campaign never changes the profile, so a
mismatch means the contract under review is not the code that would run.

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
from app.config import AppConfig
from app.container import Container
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


def arm(
    ledger: CampaignLedger,
    *,
    collection: SupplierCollection,
    product_url: str,
    phase_b_findings: Mapping[str, Any],
    mode: str,
    code_sha: str,
    budget: CampaignBudget = M3_ACCEPT_01_BUDGET,
) -> Manifest:
    """Bind the campaign once: exact SHA, target digest, frozen budget, proven byte facts."""
    problems = profile_problems(collection, budget)
    if len(code_sha) != 40 or any(c not in "0123456789abcdef" for c in code_sha):
        problems.append("the code SHA is a full 40-character commit")
    if mode not in ("REAL", "DRY"):
        problems.append("a campaign is REAL or DRY")
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


def m1_session_problems(container: Container, supplier_key: str) -> list[str]:
    """Whether the accepted M1 session is loadable, read locally. No request is made.

    The connection owner's own summary answers it: a READY connection reports a VERIFIED session
    only when the stored session exists and decrypts, and it demotes itself when it does not. The
    cookie material is never read here — that stays with the local leak scanner alone.
    """
    summary = container.connect.supplier_connection(supplier_key)
    if summary.auth_state != "AUTHENTICATED" or summary.session_state != "VERIFIED":
        return [f"the M1 connection is {summary.state}; a campaign never logs in or recovers"]
    return []


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
