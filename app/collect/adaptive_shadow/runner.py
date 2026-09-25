"""The one-fetch shadow step (ADR-0017 §10.1, §10.2; implements ``app.collect.shadow.ShadowStep``).

It is handed what the canonical run already holds in memory and nothing it could spend: the one
``DocumentView``, the canonical facts, the supplier's classified image candidates, the observed
checksums and the frozen decision. It has no gateway, session, budget or egress handle, so it
cannot make a supplier request (S1), and it opens no canonical unit (S4).

- It runs only the **frozen** bundle. If this running engine can no longer execute that exact
  bundle — its revision or schema moved on since the reservation — the run is ``SHADOW_FAILED``;
  a different bundle is never substituted.
- The engine evaluation is pure and holds no write unit. The record is then written in the
  shadow's own unit (§10.1).
- Every failure is contained (S3): an engine or comparison exception becomes a ``SHADOW_FAILED``
  record, and a failure of the shadow's own write is a log line. Nothing reaches the run.
"""

import logging
from collections.abc import Mapping

from app.collect.adaptive.document import read_html
from app.collect.adaptive.engine import extract
from app.collect.adaptive.hooks import HookManifest
from app.collect.adaptive_shadow.compare import Comparison, compare
from app.collect.adaptive_shadow.evidence import RunVerdict
from app.collect.adaptive_shadow.store import ShadowEvidenceStore
from app.collect.adaptive_shadow.switch import running_bundle
from app.collect.adaptive_store.store import AdaptiveProfileStore
from app.collect.shadow import ShadowInput

logger = logging.getLogger("icbm.collect.shadow")


class ShadowRunner:
    def __init__(
        self,
        profiles: AdaptiveProfileStore,
        evidence: ShadowEvidenceStore,
        manifests: Mapping[str, HookManifest] | None = None,
    ) -> None:
        self._profiles = profiles
        self._evidence = evidence
        self._manifests = dict(manifests or {})

    def __call__(self, shadow: ShadowInput) -> None:
        frozen = shadow.frozen.shadow
        if frozen.decision != "ENABLED" or frozen.bundle_key is None:
            return  # a disabled run makes no Adaptive call at all (S7)
        bundle_key = frozen.bundle_key
        try:
            result = self._compare(shadow, bundle_key)
            verdict, severity, detail = (
                result.verdict,
                None if result.severity is None else result.severity.value,
                result.as_json(),
            )
        except Exception as failure:
            verdict, severity = RunVerdict.SHADOW_FAILED, None
            detail = {"verdict": RunVerdict.SHADOW_FAILED.value, "failure": type(failure).__name__}
            logger.warning(
                "collect.shadow_failed",
                extra={
                    "collection_run_id": shadow.collection_run_id,
                    "failure": type(failure).__name__,
                },
            )
        try:
            self._evidence.record_outcome(
                collection_run_id=shadow.collection_run_id,
                supplier_key=shadow.supplier_key,
                revision_id=shadow.revision_id,
                bundle_key=bundle_key,
                first_product_read_at=shadow.frozen.first_product_read_at,
                verdict=verdict,
                severity=severity,
                comparison=detail,
                correlation_id=shadow.collected.correlation_id
                if shadow.collected is not None
                else f"shadow:{shadow.collection_run_id}",
            )
        except Exception:
            logger.exception(
                "collect.shadow_write_failed",
                extra={"collection_run_id": shadow.collection_run_id},
            )

    def _compare(self, shadow: ShadowInput, bundle_key: str) -> Comparison:
        epr_digest = running_bundle(bundle_key)
        if epr_digest is None:
            raise RuntimeError("ENGINE_CHANGED_SINCE_RESERVATION")
        bundle = self._profiles.load_bundle(epr_digest)
        extraction = extract(
            bundle, read_html(shadow.document.body), self._manifests.get(shadow.supplier_key)
        )
        return compare(
            source_url=shadow.source_url,
            identity=shadow.identity,
            collected=shadow.collected,
            url_policy=shadow.url_policy,
            candidates=shadow.candidates,
            images=shadow.images,
            extraction=extraction,
        )
