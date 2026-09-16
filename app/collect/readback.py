"""Reading a stored revision back out (ADR-0010 §6–§9).

The database is the authority. This turns what the revision store read back into the API's view
without recomputing a value, filling in an absent one, or consulting anything else: an image
reference's asset is looked up by the checksum the reference already holds, and a reference that
holds none simply has no asset.
"""

from app.collect.assets import SourceAssetStore
from app.collect.contracts import (
    EvidenceView,
    FieldView,
    ImageReferenceView,
    RevisionHistoryView,
    RevisionView,
    SourceAssetView,
)
from app.collect.facts import ImageReference
from app.collect.revisions import ProductFactsRevisionStore, StoredRevision
from app.core.errors import NotFoundError


class SourceTruthReadback:
    def __init__(self, revisions: ProductFactsRevisionStore, assets: SourceAssetStore) -> None:
        self._revisions = revisions
        self._assets = assets

    def revision(self, revision_id: str) -> RevisionView:
        stored = self._revisions.get(revision_id)
        if stored is None:
            raise NotFoundError("COLLECT_REVISION_UNKNOWN", "no revision has that identifier")
        return self._view(stored)

    def history(self, supplier_key: str, source_product_id: str) -> RevisionHistoryView:
        return RevisionHistoryView(
            supplier_key=supplier_key,
            source_product_id=source_product_id,
            revisions=tuple(
                self._view(stored)
                for stored in self._revisions.history(supplier_key, source_product_id)
            ),
        )

    def _view(self, stored: StoredRevision) -> RevisionView:
        return RevisionView(
            revision_id=stored.revision_id,
            supplier_key=stored.supplier_key,
            source_product_id=stored.source_product_id,
            sequence=stored.sequence,
            source_url=stored.source_url,
            captured_at=stored.captured_at,
            recorded_at=stored.recorded_at,
            currency=stored.currency,
            extractor_revision=stored.extractor_revision,
            extractor_fingerprint=stored.extractor_fingerprint,
            source_fingerprint=stored.source_fingerprint,
            collection_run_id=stored.collection_run_id,
            correlation_id=stored.correlation_id,
            facts_status=stored.facts_status,
            fingerprints_intact=stored.fingerprints_intact(),
            fields=tuple(
                FieldView(
                    key=field.key,
                    level=field.level,
                    status=field.status,
                    value_json=field.value_json,
                    fingerprint=field.fingerprint,
                    evidence=tuple(
                        EvidenceView(
                            kind=item.evidence.kind,
                            locator=item.evidence.locator,
                            observed=item.evidence.observed,
                            normalized=item.evidence.normalized,
                            status=item.evidence.status,
                            digest=item.digest,
                        )
                        for item in field.evidence
                    ),
                )
                for field in stored.fields.values()
            ),
            images=tuple(self._image(reference) for reference in stored.images),
        )

    def _image(self, reference: ImageReference) -> ImageReferenceView:
        stored = None if reference.sha256 is None else self._assets.get(reference.sha256)
        return ImageReferenceView(
            role=reference.role,
            ordinal=reference.ordinal,
            host=reference.host,
            provenance=reference.provenance,
            locator=reference.locator,
            status=reference.status,
            issue=reference.issue,
            asset=None
            if stored is None
            else SourceAssetView(
                sha256=stored.sha256,
                mime_type=stored.mime_type,
                byte_size=stored.byte_size,
                width=stored.width,
                height=stored.height,
            ),
        )
