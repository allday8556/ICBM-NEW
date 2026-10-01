"""The zero-write Adaptive dry run of an extension capture (ADR-0019 E1; ruling ``5906290729``
B-7; implements ``app.stages.collect.shadow.DryRunStep``).

An E1 extension run is compare only. It is handed the one in-memory ``DocumentView`` and what the
canonical extractor read from it, and it answers one of three things:

- ``NO_BUNDLE`` — the supplier has no reviewed bundle enabled. The supplier's shadow switch is the
  one reviewed way a bundle is designated to run beside the canonical extractor (ADR-0017 §7.1,
  S7), so a supplier whose switch is off, or whose enabled bundle this engine can no longer run,
  has none. ``NO_BUNDLE`` is never a comparison PASS, ``VALIDATED``, ``SHADOW`` or ``ACTIVE``.
- ``COMPARED`` — the enabled bundle was evaluated by the pure engine and compared in memory.
- ``COMPARE_FAILED`` — the evaluation raised; the failure is named by its type, and the
  extension owner settles the run ``FAILED`` on it. It is never a successful run.

**It writes nothing.** It reads the switch and the bundle, and it never calls the shadow evidence
owner: no shadow record, no evidence window, no ledger event and no profile transition. A
compare-only run fetched no image, so the canonical side carries no image checksum; the summary
says so, and an image outcome of such a run proves nothing about images.
"""

import logging
from collections import Counter
from collections.abc import Mapping

from app.platform.db.database import Database
from app.stages.collect.adaptive.engine.document import read_html
from app.stages.collect.adaptive.engine.engine import extract
from app.stages.collect.adaptive.engine.hooks import HookManifest
from app.stages.collect.adaptive.shadow.compare import compare
from app.stages.collect.adaptive.shadow.switch import ShadowSwitch, running_bundle
from app.stages.collect.adaptive.store.store import AdaptiveProfileStore
from app.stages.collect.shadow import COMPARE_FAILED, NO_BUNDLE, DryRunInput, DryRunResult

logger = logging.getLogger("icbm.collect.shadow")


class DryRunComparer:
    def __init__(
        self,
        db: Database,
        switch: ShadowSwitch,
        profiles: AdaptiveProfileStore,
        manifests: Mapping[str, HookManifest] | None = None,
    ) -> None:
        self._db = db
        self._switch = switch
        self._profiles = profiles
        self._manifests = dict(manifests or {})

    def __call__(self, dry_run: DryRunInput) -> DryRunResult:
        with self._db.read() as session:
            frozen = self._switch.freeze(session, dry_run.supplier_key)
        if frozen.decision != "ENABLED" or frozen.bundle_key is None:
            return DryRunResult(NO_BUNDLE)
        epr_digest = running_bundle(frozen.bundle_key)
        if epr_digest is None:
            return DryRunResult(NO_BUNDLE)
        try:
            bundle = self._profiles.load_bundle(epr_digest)
            extraction = extract(
                bundle,
                read_html(dry_run.document.body),
                self._manifests.get(dry_run.supplier_key),
            )
            comparison = compare(
                source_url=dry_run.source_url,
                identity=dry_run.identity,
                collected=dry_run.collected,
                url_policy=dry_run.url_policy,
                candidates=dry_run.candidates,
                # A compare-only run fetched no image: the canonical side has no checksum.
                images=(),
                extraction=extraction,
            )
        except Exception as failure:
            logger.warning(
                "collect.extension_dry_run_failed",
                extra={
                    "collection_run_id": dry_run.collection_run_id,
                    "failure": type(failure).__name__,
                },
            )
            return DryRunResult(
                COMPARE_FAILED, frozen.bundle_key, {"failure": type(failure).__name__}
            )
        return DryRunResult(
            "COMPARED",
            frozen.bundle_key,
            {
                "verdict": comparison.verdict.value,
                "severity": None if comparison.severity is None else comparison.severity.value,
                "template": dict(comparison.template),
                "facts_status": dict(comparison.facts_status),
                "field_verdicts": dict(
                    sorted(Counter(f.verdict.value for f in comparison.fields).items())
                ),
                "image_outcomes": dict(
                    sorted(Counter(i.outcome.value for i in comparison.images).items())
                ),
                "canonical_images_fetched": False,
            },
        )
