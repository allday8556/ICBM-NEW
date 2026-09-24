"""The engine's extraction identity: manifest pin and goldens (ADR-0010 §12, ADR-0017 §5.6)."""

import json
from pathlib import Path

from app.collect.adaptive import extraction_identity
from app.collect.adaptive.document import read_html
from app.collect.adaptive.engine import extract
from integrations.suppliers.extraction import extractor_fingerprint, read_manifest
from tests.adaptive_support import FIXTURES, hook_manifest, hooked_bundle, page, synmart_bundle

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / "app" / "collect" / "adaptive"
MANIFEST = PACKAGE / "extraction_identity.py"


def test_the_manifest_holds_exactly_the_three_literals_and_is_acyclic() -> None:
    manifest = read_manifest(MANIFEST)  # refuses anything but the three literal constants
    assert manifest.revision == extraction_identity.EXTRACTOR_REVISION
    required = {p.relative_to(REPO).as_posix() for p in PACKAGE.rglob("*.py")} - {
        MANIFEST.relative_to(REPO).as_posix()
    }
    assert set(manifest.inputs) == required, "every engine module is hashed, and only those"
    assert len(manifest.inputs) == len(set(manifest.inputs))


def test_the_implementation_fingerprint_is_current() -> None:
    manifest = read_manifest(MANIFEST)
    assert extractor_fingerprint(REPO, manifest.inputs) == manifest.fingerprint, (
        "EXTRACTOR_FINGERPRINT is stale: re-pin it, and advance EXTRACTOR_REVISION in the same "
        "change if extraction behaviour changed (the goldens below say whether it did)"
    )


def test_the_engine_goldens_hold_for_this_revision() -> None:
    goldens = json.loads((FIXTURES / "goldens.json").read_text("utf-8"))
    assert goldens["extractor_revision"] == extraction_identity.EXTRACTOR_REVISION, (
        "a new EXTRACTOR_REVISION needs goldens recorded under it"
    )
    bundle = synmart_bundle()
    got = {
        name: extract(bundle, read_html(page(name))).digest()
        for name in ("on_sale", "sold_out", "optioned", "login", "listing")
    }
    got["hooked"] = extract(hooked_bundle(), read_html(page("hooked")), hook_manifest()).digest()
    assert got == goldens["extractions"], (
        "extraction behaviour changed under an unchanged EXTRACTOR_REVISION: advance it and "
        "record new goldens"
    )
