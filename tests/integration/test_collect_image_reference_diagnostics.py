"""Image reference diagnostics through the production COLLECT path (Issue #52 ruling 5716978033,
PR-1).

A synthetic supplier writes its image references in every form a page can: absolute, protocol-
relative and relative, trimmed and not, and with each defect the transport's target check refuses.
It resolves them the way a supplier parser does and reports what it wrote. The real job,
orchestrator, recorder, revision store and read-back then show — for every reference,
deterministically — how it was written, what it resolved to and, where the target check refused
it, why; and that nothing was reserved or sent for a refused target. No provider can be reached.
"""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from urllib.parse import urljoin

import pytest

from app.api.routes.collect import revision as revision_route
from app.collect.collection import RegisteredCollection
from app.collect.facts import FetchTargetRefusal, FieldStatus, ImageIssue, ImageRole, LocatorForm
from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import acquire_data_dir
from integrations.suppliers.collection import ImageCandidate, ImageRoleRules
from integrations.suppliers.collection import ImageRole as SourceRole
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    HOST,
    IMAGE_HOST,
    PRIMARY_BYTES,
    PRODUCT_URL,
    SUPPLIER_KEY,
    FakeGateway,
    StubSessions,
    collection,
    page,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

SECRET = "pw-that-must-never-be-stored"
# (what the page wrote, source role, expected written form, trimmed, refusal, canonical locator)
WRITTEN = (
    (f"https://{IMAGE_HOST}/p/primary.png", SourceRole.PRIMARY, "ABSOLUTE", False, None, True),
    (f"//{IMAGE_HOST}/p/detail.png", SourceRole.DETAIL, "PROTOCOL_RELATIVE", False, None, True),
    ("/p/relative.png", SourceRole.DETAIL, "RELATIVE", False, "HOST_NOT_ALLOWLISTED", False),
    (f"http://{IMAGE_HOST}/p/http.png", SourceRole.DETAIL, "ABSOLUTE", False, "NON_HTTPS", False),
    (f"  https://{IMAGE_HOST}/p/trimmed.png\n", SourceRole.DETAIL, "ABSOLUTE", True, None, True),
    (
        f"https://{IMAGE_HOST}/p/in side.png",
        SourceRole.DETAIL,
        "ABSOLUTE",
        False,
        "WHITESPACE",
        False,
    ),
    (
        f"https://{IMAGE_HOST}/p/frag.png#top",
        SourceRole.DETAIL,
        "ABSOLUTE",
        False,
        "FRAGMENT",
        False,
    ),
    (
        f"https://user:{SECRET}@{IMAGE_HOST}/p/cred.png",
        SourceRole.DETAIL,
        "ABSOLUTE",
        False,
        "CREDENTIALS_PRESENT",
        False,
    ),
    (
        f"https://{IMAGE_HOST}:8443/p/port.png",
        SourceRole.DETAIL,
        "ABSOLUTE",
        False,
        "NON_STANDARD_PORT",
        False,
    ),
    (
        f"ftp://{IMAGE_HOST}/p/ftp.png",
        SourceRole.DETAIL,
        "ABSOLUTE",
        False,
        "UNSUPPORTED_SCHEME",
        False,
    ),
    (
        f"https://{IMAGE_HOST}:99999/p/bad.png",
        SourceRole.DETAIL,
        "ABSOLUTE",
        False,
        "UNPARSEABLE",
        False,
    ),
    (
        f"https://{IMAGE_HOST}/p/query.png?sig={SECRET}",
        SourceRole.DETAIL,
        "ABSOLUTE",
        False,
        None,
        False,
    ),
)


def _classify(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    """Resolve each written reference the way a supplier parser does: trim, then join."""
    return tuple(
        ImageCandidate(
            url=urljoin(product_url, written.strip()),
            role=role,
            order=order,
            rule=f"diag.{order}",
            source=written,
        )
        for order, (written, role, *_) in enumerate(WRITTEN)
    )


def _registered() -> RegisteredCollection:
    shop = replace(
        collection(max_image_requests=20),
        roles=ImageRoleRules(identity=EXTRACTOR_REVISION, classify=_classify),
    )
    return RegisteredCollection(
        collection=shop,
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
    )


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway(
        documents=[page()],
        images={
            f"https://{IMAGE_HOST}/p/primary.png": PRIMARY_BYTES,
            f"https://{IMAGE_HOST}/p/detail.png": DETAIL_BYTES,
        },
    )


@pytest.fixture
def collecting(config: AppConfig, clock: FakeClock, gateway: FakeGateway) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
            collections=(_registered(),),
        )
        try:
            yield built
        finally:
            built.db.dispose()


def _collect(container: Container) -> str:
    submitted = container.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    assert container.runner.run_next() is not None
    revision_id = container.collection.run(submitted.collection_run_id).revision_id
    assert revision_id is not None
    return revision_id


def _diagnostics(container: Container, revision_id: str) -> list[tuple[object, ...]]:
    view = container.source_truth.revision(revision_id)
    return sorted(
        (
            ref.ordinal,
            ref.role,
            ref.provenance,
            ref.host,
            ref.status,
            ref.issue,
            ref.locator,
            ref.source_form,
            ref.source_trimmed,
            ref.target_refusal,
        )
        for ref in view.images
    )


def test_every_reference_reads_back_how_it_was_written_and_what_it_resolved_to(
    collecting: Container, gateway: FakeGateway, clock: FakeClock
) -> None:
    revision_id = _collect(collecting)
    view = collecting.source_truth.revision(revision_id)
    by_order = {ref.ordinal: ref for ref in view.images}
    assert sorted(by_order) == list(range(len(WRITTEN))), "no reference was dropped"

    for order, (written, role, form, trimmed, refusal, has_locator) in enumerate(WRITTEN):
        ref = by_order[order]
        resolved = urljoin(PRODUCT_URL, written.strip())
        assert ref.provenance == f"diag.{order}"
        expected_role = ImageRole.REPRESENTATIVE if role is SourceRole.PRIMARY else ImageRole.DETAIL
        assert ref.role is expected_role
        assert (ref.source_form, ref.source_trimmed) == (LocatorForm(form), trimmed), written
        if refusal is None:
            assert ref.target_refusal is None, written
        else:
            assert ref.target_refusal is FetchTargetRefusal(refusal), written
            assert (
                ref.status is FieldStatus.REVIEW_REQUIRED and ref.issue is ImageIssue.FETCH_FAILED
            )
            assert ref.locator is None and ref.asset is None
        if has_locator:
            assert ref.locator == resolved
        else:
            assert ref.locator is None, written
    assert by_order[2].host == HOST, "a relative reference resolves against the page"
    assert by_order[0].status is FieldStatus.CONFIRMED and by_order[0].asset is not None
    assert by_order[1].status is FieldStatus.CONFIRMED, "protocol-relative resolves to https"

    # Only the targets the judge allowed were ever handed to the transport.
    assert gateway.image_reads == [
        f"https://{IMAGE_HOST}/p/primary.png",
        f"https://{IMAGE_HOST}/p/detail.png",
        f"https://{IMAGE_HOST}/p/trimmed.png",
        f"https://{IMAGE_HOST}/p/query.png?sig={SECRET}",
    ]

    # The API reads back exactly what the store holds.
    api = revision_route(revision_id, collecting)
    assert [
        (ref.ordinal, ref.source_form, ref.source_trimmed, ref.target_refusal, ref.locator)
        for ref in api.images
    ] == [
        (ref.ordinal, ref.source_form, ref.source_trimmed, ref.target_refusal, ref.locator)
        for ref in view.images
    ]


def test_the_same_page_gives_the_same_diagnostics_every_time(
    collecting: Container, clock: FakeClock
) -> None:
    first = _collect(collecting)
    clock.advance(collection().profile.limits.same_product_interval_s + 1)
    second = _collect(collecting)
    assert first != second
    assert _diagnostics(collecting, first) == _diagnostics(collecting, second)


def test_no_written_reference_text_or_credential_reaches_the_database(
    config: AppConfig, collecting: Container
) -> None:
    _collect(collecting)
    with closing(sqlite3.connect(config.database_path)) as raw:
        rows = raw.execute("SELECT * FROM product_facts_image_refs").fetchall()
        evidence = raw.execute("SELECT * FROM product_facts_evidence").fetchall()
    stored = repr(rows) + repr(evidence)
    assert SECRET not in stored
    # Every URL that was stored is a canonical https locator; no written form survives as text.
    urls = re.findall(r"[a-z]*:?//[^\s'\"]+", stored)
    assert urls and all(url.startswith(f"https://{IMAGE_HOST}/p/") for url in urls), urls
    assert "in side" not in stored and "#top" not in stored and ":8443" not in stored
