"""The image acceptance model through the production COLLECT path (Issue #52 ruling 5723016554).

A synthetic supplier writes its image references the way a page can — an absolute ``https`` one,
an absolute ``http`` one, a relative one its parser failed to resolve — and the real job,
orchestrator, recorder, revision store, read-back, API and M3 campaign judge then show:

* the decision is made once, when the revision is recorded, and read back — never recomputed;
* a source-authored ``http`` reference is excluded, costs no request and keeps its PR #74
  diagnostics, and no raw unsafe URL is stored anywhere;
* a reference left unresolved at the transport stays unresolved and holds the pass;
* more than a third excluded holds the images field, and with it the pass;
* a revision recorded before the model reads back exactly as it was judged then.

Nothing here reaches a provider: the gateway is the offline fake, and it counts every request.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin

import pytest

from app.api.routes.collect import revision as revision_route
from app.collect.collection import RegisteredCollection, url_policy_of
from app.collect.contracts import RevisionView
from app.collect.facts import (
    IMAGES_FIELD,
    MISSING_REPRESENTATIVE_LOCATOR,
    EvaluatedEvidence,
    EvaluatedField,
    Evidence,
    EvidenceKind,
    FactsStatus,
    FetchTargetRefusal,
    FieldStatus,
    ImageCertainty,
    ImageDisposition,
    ImageExclusion,
    ImageIssue,
    ImageReference,
    ImageRole,
    LocatorForm,
    canonical_json,
    evaluate,
    evidence_digest,
    facts_status,
    field_fingerprint,
    source_fingerprint,
)
from app.collect.models import (
    CollectionOutcome,
    ProductFactsEvidence,
    ProductFactsField,
    ProductFactsImageRef,
    ProductFactsRevision,
)
from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import acquire_data_dir
from integrations.suppliers.collection import ImageCandidate, ImageRoleRules
from integrations.suppliers.collection import ImageRole as SourceRole
from scripts.m3accept.campaign import classify_pass
from scripts.m3accept.manifest import M3_ACCEPT_01_BUDGET
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    IMAGE_HOST,
    PRIMARY_BYTES,
    PRIMARY_URL,
    PRODUCT_URL,
    SUPPLIER_KEY,
    FakeGateway,
    StubSessions,
    collected_for_run,
    collection,
    page,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

WRITTEN_HTTP = f"http://{IMAGE_HOST}/p/written-http.png"
SECOND_HTTP = f"http://{IMAGE_HOST}/p/written-http-2.png"
# A protocol-relative reference keeps its host when a parser forgets to join it against the page,
# so it reaches the transport — which refuses it as NOT_ABSOLUTE before sending anything.
UNJOINED_PROTOCOL_RELATIVE = f"//{IMAGE_HOST}/p/never-joined.png"


def references(*written: tuple[str, SourceRole, bool]) -> RegisteredCollection:
    """A supplier whose page writes exactly these references, resolved (or not) as stated."""

    def classify(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
        return tuple(
            ImageCandidate(
                url=urljoin(product_url, text) if resolve else text,
                role=role,
                order=order,
                rule=f"accept.{order}",
                source=text,
            )
            for order, (text, role, resolve) in enumerate(written)
        )

    shop = replace(
        collection(max_image_requests=20),
        roles=ImageRoleRules(identity=EXTRACTOR_REVISION, classify=classify),
    )
    return RegisteredCollection(
        collection=shop,
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
    )


PRIMARY = (PRIMARY_URL, SourceRole.PRIMARY, True)
DETAIL = (DETAIL_URL, SourceRole.DETAIL, True)
HTTP_DETAIL = (WRITTEN_HTTP, SourceRole.DETAIL, True)
SECOND_HTTP_DETAIL = (SECOND_HTTP, SourceRole.DETAIL, True)
UNJOINED_DETAIL = (UNJOINED_PROTOCOL_RELATIVE, SourceRole.DETAIL, False)  # a parser regression


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway(
        documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES}
    )


def container_for(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, shop: RegisteredCollection
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
            collections=(shop,),
        )
        try:
            yield built
        finally:
            built.db.dispose()


def collect(container: Container) -> tuple[str, RevisionView]:
    submitted = container.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    assert container.runner.run_next() is not None
    run = container.collection.run(submitted.collection_run_id)
    assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None
    return submitted.collection_run_id, container.source_truth.revision(run.revision_id)


def judge(container: Container, run_id: str, revision: RevisionView) -> Any:
    # The judge reads the run's own revision; a revision written beside it is judged as its run's.
    run = replace(container.collection.run(run_id), revision_id=revision.revision_id)
    return classify_pass(
        run=run,
        revision=revision,
        counts={},
        refusals=[],
        budget=M3_ACCEPT_01_BUDGET,
    )


def images_field(revision: RevisionView) -> Any:
    return next(field for field in revision.fields if field.key == IMAGES_FIELD)


def raw(container: Container, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(container.config.database_path)) as db:
        return db.execute(sql, args).fetchall()


def stored_text(container: Container) -> str:
    """Every text a revision stored, in one string, to look for what must never be stored."""
    tables = (
        "product_facts_revisions",
        "product_facts_fields",
        "product_facts_evidence",
        "product_facts_image_refs",
        "collection_runs",
    )
    return "\n".join(
        json.dumps(row, default=str)
        for table in tables
        for row in raw(container, f"SELECT * FROM {table}")
    )


# ---------------------------------------------------------------- one source-authored exclusion


def test_14_a_source_authored_http_reference_is_excluded_without_a_request(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    shop = references(PRIMARY, DETAIL, HTTP_DETAIL)
    for container in container_for(config, clock, gateway, shop):
        run_id, revision = collect(container)

        # Provider traffic: one document, and the two references the transport allowed — nothing
        # for the one it refused. (16: the gateway is the offline fake; it counts every request.)
        assert gateway.document_reads == 1
        assert gateway.image_reads == [PRIMARY_URL, DETAIL_URL]

        by_order = {ref.provenance: ref for ref in revision.images}
        written_http = by_order["accept.2"]
        # PR #74 diagnostics, unchanged: how it was written and why its target was refused.
        assert (written_http.source_form, written_http.source_trimmed) == (
            LocatorForm.ABSOLUTE,
            False,
        )
        assert written_http.target_refusal is FetchTargetRefusal.NON_HTTPS
        assert (written_http.status, written_http.issue) == (
            FieldStatus.REVIEW_REQUIRED,
            ImageIssue.FETCH_FAILED,
        )
        assert written_http.locator is None and written_http.asset is None
        # The acceptance decision, recorded with the revision and read back.
        assert (
            written_http.certainty,
            written_http.disposition,
            written_http.exclusion,
        ) == (
            ImageCertainty.DETERMINATE,
            ImageDisposition.EXCLUDED,
            ImageExclusion.SOURCE_AUTHORED_NON_HTTPS,
        )
        for included in (by_order["accept.0"], by_order["accept.1"]):
            assert (included.certainty, included.disposition, included.exclusion) == (
                ImageCertainty.DETERMINATE,
                ImageDisposition.INCLUDED,
                None,
            )

        # 1 of 3 excluded, nothing unresolved, both roles included: the images field confirms.
        assert images_field(revision).status is FieldStatus.CONFIRMED
        assert revision.facts_status is FactsStatus.CONFIRMED
        assert revision.fingerprints_intact

        # R14: nothing stores the raw unsafe reference.
        assert "http://" not in stored_text(container)
        assert raw(
            container,
            "SELECT status, issue, locator, sha256, source_form, source_trimmed, target_refusal"
            " FROM product_facts_image_refs WHERE provenance = 'accept.2'",
        ) == [("REVIEW_REQUIRED", "FETCH_FAILED", None, None, "ABSOLUTE", 0, "NON_HTTPS")]

        # The API returns what the service read back.
        api = revision_route(revision.revision_id, container)
        assert [(r.provenance, r.disposition, r.exclusion) for r in api.images] == [
            (r.provenance, r.disposition, r.exclusion) for r in revision.images
        ]

        # The campaign judge consumes the recorded decision: an observation, not a HOLD (R8).
        verdict = judge(container, run_id, revision)
        assert verdict.verdict == "ACCEPTED", verdict
        assert not [r for r in verdict.reasons if r.startswith("IMAGE_REFERENCE_UNRESOLVED")]
        assert "IMAGE_REFERENCE_EXCLUDED:1:SOURCE_AUTHORED_NON_HTTPS" in verdict.observations


def test_6_more_than_a_third_excluded_holds_the_pass(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    shop = references(PRIMARY, DETAIL, HTTP_DETAIL, SECOND_HTTP_DETAIL)
    for container in container_for(config, clock, gateway, shop):
        run_id, revision = collect(container)
        assert gateway.image_reads == [PRIMARY_URL, DETAIL_URL]
        field = images_field(revision)
        assert field.status is FieldStatus.REVIEW_REQUIRED
        assert ("images:excluded", "EXCLUDED:2/4") in [
            (e.locator, e.normalized) for e in field.evidence
        ]
        verdict = judge(container, run_id, revision)
        assert verdict.verdict == "HOLD"
        assert "CORE_FACT_AMBIGUOUS:images" in verdict.reasons
        # Both exclusions are counted, and neither is reported as an unresolved reference.
        assert "IMAGE_REFERENCE_EXCLUDED:2:SOURCE_AUTHORED_NON_HTTPS" in verdict.observations
        assert not [r for r in verdict.reasons if r.startswith("IMAGE_REFERENCE_UNRESOLVED")]


def test_2_a_reference_left_unresolved_at_the_transport_holds_the_pass(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    # R3 end to end: the parser handed the transport a relative reference it should have joined.
    shop = references(PRIMARY, DETAIL, UNJOINED_DETAIL)
    for container in container_for(config, clock, gateway, shop):
        run_id, revision = collect(container)
        assert gateway.image_reads == [PRIMARY_URL, DETAIL_URL]
        unjoined = next(r for r in revision.images if r.provenance == "accept.2")
        assert (unjoined.source_form, unjoined.target_refusal) == (
            LocatorForm.PROTOCOL_RELATIVE,
            FetchTargetRefusal.NOT_ABSOLUTE,
        )
        assert (unjoined.certainty, unjoined.disposition, unjoined.exclusion) == (
            ImageCertainty.INDETERMINATE,
            ImageDisposition.UNRESOLVED,
            None,
        )
        assert images_field(revision).status is FieldStatus.REVIEW_REQUIRED
        verdict = judge(container, run_id, revision)
        assert verdict.verdict == "HOLD"
        assert "IMAGE_REFERENCE_UNRESOLVED:FETCH_FAILED" in verdict.reasons
        assert not [o for o in verdict.observations if o.startswith("IMAGE_REFERENCE_EXCLUDED")]


# ---------------------------------------------------------------- history is immutable


def legacy_images(refs: tuple[ImageReference, ...]) -> tuple[FieldStatus, str, list[Evidence]]:
    """The images field exactly as it was derived at 40ca4adc, before the acceptance model.

    Kept here as the historical fixture: every reference not observed was simply REVIEW_REQUIRED,
    and the field was CONFIRMED only with every reference observed.
    """
    value = {
        "references": [
            {"role": r.role.value, "ordinal": r.ordinal, "sha256": r.sha256, "status": r.status}
            for r in refs
        ]
    }
    evidence = [
        Evidence(
            kind=EvidenceKind.IMAGE,
            locator=r.provenance,
            status=r.status,
            observed=r.sha256,
            normalized=f"{r.role.value}:{r.ordinal}",
        )
        for r in refs
    ]
    representative = any(
        r.role is ImageRole.REPRESENTATIVE and r.status is FieldStatus.CONFIRMED for r in refs
    )
    if representative and all(r.status is FieldStatus.CONFIRMED for r in refs):
        return FieldStatus.CONFIRMED, canonical_json(value), evidence
    if not representative:
        evidence.append(
            Evidence(
                kind=EvidenceKind.IMAGE,
                locator=MISSING_REPRESENTATIVE_LOCATOR,
                status=FieldStatus.REVIEW_REQUIRED,
                normalized="REPRESENTATIVE:missing",
            )
        )
    return FieldStatus.REVIEW_REQUIRED, canonical_json(value), evidence


def record_legacy_revision(container: Container, refs: tuple[ImageReference, ...]) -> str:
    """Write one revision the way the store wrote it before the acceptance model existed."""
    collected = replace(collected_for_run("legacy-run"), images=refs)
    current = evaluate(collected, url_policy_of(collection().profile))
    status, value_json, entries = legacy_images(current.images)
    evaluated = tuple(EvaluatedEvidence(e, evidence_digest(e)) for e in entries)
    legacy_field = EvaluatedField(
        key=IMAGES_FIELD,
        level=next(f.level for f in current.fields if f.key == IMAGES_FIELD),
        status=status,
        value_json=value_json,
        evidence=evaluated,
        fingerprint=field_fingerprint(
            IMAGES_FIELD, status, value_json, (e.digest for e in evaluated)
        ),
    )
    fields = tuple(legacy_field if f.key == IMAGES_FIELD else f for f in current.fields)
    revision_id = "legacy-revision-0000-0000-000000000001"
    with container.db.write() as session:
        session.add(
            ProductFactsRevision(
                revision_id=revision_id,
                supplier_key=collected.supplier_key,
                source_product_id=collected.source_product_id,
                sequence=99,
                source_url=collected.source_url,
                captured_at=collected.captured_at,
                recorded_at=collected.captured_at,
                currency="KRW",
                extractor_revision=collected.extractor_revision,
                extractor_fingerprint=collected.extractor_fingerprint,
                source_fingerprint=source_fingerprint(
                    ((f.key, f.fingerprint) for f in fields),
                    ((r.role, r.ordinal, r.sha256) for r in current.images),
                ),
                collection_run_id=collected.collection_run_id,
                correlation_id=collected.correlation_id,
                facts_status=facts_status({f.key: f.status for f in fields}).value,
            )
        )
        session.flush()
        for f in fields:
            session.add(
                ProductFactsField(
                    revision_id=revision_id,
                    field_key=f.key,
                    level=f.level.value,
                    status=f.status.value,
                    value_json=f.value_json,
                    field_fingerprint=f.fingerprint,
                )
            )
        session.flush()
        for f in fields:
            for ordinal, entry in enumerate(f.evidence):
                session.add(
                    ProductFactsEvidence(
                        revision_id=revision_id,
                        field_key=f.key,
                        ordinal=ordinal,
                        kind=entry.evidence.kind.value,
                        locator=entry.evidence.locator,
                        observed=entry.evidence.observed,
                        normalized=entry.evidence.normalized,
                        status=entry.evidence.status.value,
                        digest=entry.digest,
                    )
                )
        for r in current.images:
            session.add(
                ProductFactsImageRef(
                    revision_id=revision_id,
                    role=r.role.value,
                    ordinal=r.ordinal,
                    host=r.host,
                    provenance=r.provenance,
                    locator=r.locator,
                    sha256=r.sha256,
                    status=r.status.value,
                    issue=None if r.issue is None else r.issue.value,
                    http_etag=r.etag,
                    http_last_modified=r.last_modified,
                    source_form=None if r.source_form is None else r.source_form.value,
                    source_trimmed=r.source_trimmed,
                    target_refusal=None if r.target_refusal is None else r.target_refusal.value,
                )
            )
    return revision_id


def test_15_a_revision_recorded_before_the_model_is_never_reclassified(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    shop = references(PRIMARY, DETAIL, HTTP_DETAIL)
    for container in container_for(config, clock, gateway, shop):
        run_id, current = collect(container)
        assert current.facts_status is FactsStatus.CONFIRMED  # the model confirms these refs...
        refs = container.revisions.get(current.revision_id).images  # type: ignore[union-attr]
        legacy_id = record_legacy_revision(container, refs)
        before = raw(
            container, "SELECT * FROM product_facts_fields WHERE revision_id = ?", legacy_id
        )

        legacy = container.source_truth.revision(legacy_id)
        # ...and the same references, judged before it existed, stay exactly as judged then.
        assert legacy.facts_status is FactsStatus.REVIEW_REQUIRED
        assert images_field(legacy).status is FieldStatus.REVIEW_REQUIRED
        assert legacy.fingerprints_intact
        assert {(r.certainty, r.disposition, r.exclusion) for r in legacy.images} == {
            (None, None, None)
        }
        # The PR #74 diagnostics of the history read back as they were stored.
        http_ref = next(r for r in legacy.images if r.provenance == "accept.2")
        assert (http_ref.source_form, http_ref.target_refusal) == (
            LocatorForm.ABSOLUTE,
            FetchTargetRefusal.NON_HTTPS,
        )

        # The campaign judge reads it the way it was judged then.
        verdict = judge(container, run_id, legacy)
        assert verdict.verdict == "HOLD"
        assert "IMAGE_REFERENCE_UNRESOLVED:FETCH_FAILED" in verdict.reasons
        assert "CORE_FACT_AMBIGUOUS:images" in verdict.reasons
        assert not [o for o in verdict.observations if o.startswith("IMAGE_REFERENCE_EXCLUDED")]

        # Reading wrote nothing.
        after = raw(
            container, "SELECT * FROM product_facts_fields WHERE revision_id = ?", legacy_id
        )
        assert after == before
        api = revision_route(legacy_id, container)
        assert api.facts_status is FactsStatus.REVIEW_REQUIRED
        assert {r.disposition for r in api.images} == {None}
