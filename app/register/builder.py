"""M5 PR-C: the RegistrationSnapshot builder (ADR-0014 §3, §6; kickoff 5742880432 §4).

It turns one **final READY** preflight into the immutable Snapshot the PR-B store freezes, and
nothing else. Freshness is fail-closed:
- the preflight is evaluated again from current truth, and the Snapshot is frozen only if that
  evaluation is still READY **and** its fingerprint equals the one the caller holds. Any drift in
  between (a price pin, a Draft revision, a category, taxonomy or policy revision, an image or QA,
  duplicate evidence, an UNKNOWN conflict, the account binding, the detail composition or a
  prepared asset) refuses the freeze as ``REGISTER_PREFLIGHT_STALE``;
- the payload comes from that evaluation only (:func:`app.register.payload.build_payload`), so no
  caller substitutes another price, category, Item or account after the preflight;
- the store's own invariants (current Draft revision, pins, open Items, single-listing coverage,
  bound canonical account) and migration 0016's triggers still apply to the write;
- freezing the same preparation again returns the Snapshot already frozen for it.

It opens no Intent, sends nothing and uploads nothing: execution is PR-E's.
"""

from app.core.errors import InputValidationError
from app.products.model import ReadinessStatus
from app.register.model import RegistrationConflictError
from app.register.payload import build_payload
from app.register.preflight import RegistrationPreflightService
from app.register.preparation import PreflightResult, PreflightStage
from app.register.store import (
    ItemSnapshotSpec,
    RegistrationStore,
    RegistrationUnit,
    SnapshotRecord,
    SnapshotSpec,
)


class RegistrationSnapshotBuilder:
    def __init__(
        self, *, preflight: RegistrationPreflightService, registrations: RegistrationStore
    ) -> None:
        self._preflight = preflight
        self._registrations = registrations

    def freeze(
        self,
        ready: PreflightResult,
        *,
        created_by: str,
        correlation_id: str,
        preparation_revision_id: str | None = None,
        registrations: RegistrationUnit | None = None,
    ) -> SnapshotRecord:
        """``preparation_revision_id`` names the authored revision these inputs came from (§27),
        so the immutable Snapshot can prove it. A caller holding its own inputs passes none."""
        if ready.stage is not PreflightStage.FINAL or ready.status is not ReadinessStatus.READY:
            raise InputValidationError(
                "REGISTER_PREFLIGHT_NOT_READY",
                "a Snapshot is frozen only from a final READY preflight",
            )
        fresh = self._preflight.final(ready.request, ready.prepared_assets)
        if (
            fresh.status is not ReadinessStatus.READY
            or fresh.dependency_fingerprint != ready.dependency_fingerprint
        ):
            raise RegistrationConflictError(
                "REGISTER_PREFLIGHT_STALE",
                "a dependency changed since the preflight: evaluate it again",
                details={"status": fresh.status.value, "reasons": list(fresh.codes)},
            )
        if registrations is not None:
            return self._freeze_fresh(
                registrations,
                fresh,
                created_by=created_by,
                correlation_id=correlation_id,
                preparation_revision_id=preparation_revision_id,
            )
        with self._registrations.transaction() as unit:
            return self._freeze_fresh(
                unit,
                fresh,
                created_by=created_by,
                correlation_id=correlation_id,
                preparation_revision_id=preparation_revision_id,
            )

    def _freeze_fresh(
        self,
        registrations: RegistrationUnit,
        fresh: PreflightResult,
        *,
        created_by: str,
        correlation_id: str,
        preparation_revision_id: str | None,
    ) -> SnapshotRecord:
        """Write a validated result through the caller's unit of work when one is supplied."""
        outbound = build_payload(fresh)
        request, unit = fresh.request, fresh.resolved
        category, detail = request.category, request.detail
        assert category is not None and detail is not None
        existing = registrations.matching_snapshot(
            unit.draft_id,
            unit.draft_revision,
            unit.listing_identity,
            fresh.dependency_fingerprint,
            outbound.payload_digest,
        )
        if existing is not None:
            provenance = registrations.snapshot_preparation(existing.registration_snapshot_id)
            if preparation_revision_id is None or (
                provenance is not None
                and provenance.preparation_revision_id == preparation_revision_id
            ):
                return existing
            # An identical legacy Snapshot, or one produced by another authored revision, cannot
            # be relabelled after the fact. Freeze a new Snapshot whose provenance is truthful.
        return registrations.freeze_snapshot(
            SnapshotSpec(
                draft_id=unit.draft_id,
                draft_revision=unit.draft_revision,
                listing_identity=unit.listing_identity,
                preflight_rule_version=fresh.rule_version,
                preflight_fingerprint=fresh.dependency_fingerprint,
                category_mapping_revision=category.mapping_revision,
                taxonomy_revision=category.taxonomy_revision,
                policy_revisions=outbound.policy_revisions,
                detail_composition_revision=detail.composition_revision,
                sanitizer_profile_version=unit.target.sanitizer_profile_version,
                payload=outbound.payload,
                items=[
                    ItemSnapshotSpec(
                        item_id=item.item_id,
                        source_snapshot=item.source_snapshot,
                        publication_assets=item.publication_assets,
                        outbound_values=item.outbound_values,
                    )
                    for item in outbound.items
                ],
                preparation_revision_id=preparation_revision_id,
                identity_generation=unit.identity_generation,
            ),
            created_by=created_by,
            correlation_id=correlation_id,
        )
