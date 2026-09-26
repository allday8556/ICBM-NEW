"""The evidence-retention proof (ADR-0018 §8): end to end, fail closed.

``EVIDENCE_RETENTION_READY`` is never asserted. It holds only while a recorded PASSED proof matches
the retention checks **as they are now**, read from the live database and the running process:

- every canary-scope durable table — the REGISTER rows, the pre-LIVE owners, the audit trail, the
  review owner, and the M4 product, pricing and image truth a Snapshot and a drill rest on — keeps
  a ``BEFORE DELETE`` trigger whose body is an **unconditional** ``RAISE(ABORT, …)``, so no row of
  that evidence can be deleted by any path;
- **the semantics, not the names**: every trigger on those tables — the delete guards, the
  append-only and transition guards, and the forward-only triggers of the grant, the brake and the
  ASSET attempt owner, so unresolved evidence (an ``UPLOAD_UNKNOWN``, a consumed grant) cannot be
  rewritten away — is exactly the trigger the shipped migrations build at head
  (``app.db.schema_contract``): a same-name no-op replacement, a dropped or an extra trigger fails;
- **no automatic deletion is authorized**: no registered job type names a delete, purge, prune,
  cleanup or retention action (a later deletion policy needs its own decision);
- the sanitizer and the adapter's safe-retention profile versions evidence is recorded under.

Any failed check makes the proof FAILED and the readiness false. The checks carry the digest of the
contract's guard SQL and of the live guard SQL, so a change of schema head, of any guard's
semantics or of any other check makes an earlier proof stale. Sanitation before hash or persist
stays the owners' rule (ADR-0014 §15); this proof only shows that the path which keeps the
evidence is intact.
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text

from app.db.database import Database
from app.db.schema_contract import (
    MANIFEST_QUERY,
    SchemaManifest,
    digest_of,
    expected_manifest,
    manifest_of,
    refuses_every_delete,
)
from app.live.model import RETENTION_CHECK_FAILED, ProofVerdict
from app.live.store import LiveAuthorityStore
from app.register.sanitize import SANITIZER_RULES_VERSION

RETENTION_CHECKS_VERSION: Final = "evidence-retention-checks/v2"

# Every table whose rows are canary evidence or the chain a restore proof compares (§7, §8).
PROTECTED_TABLES: Final = (
    "audit_events",
    "marketplace_accounts",
    "seller_entities",
    "registration_drafts",
    "registration_draft_items",
    "registration_preparations",
    "registration_preparation_revisions",
    "registration_preparation_items",
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_snapshot_preparations",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
    "registration_execution_scopes",
    "marketplace_registrations",
    "marketplace_registration_items",
    "duplicate_overrides",
    "registration_target_policies",
    "registration_target_policy_revisions",
    "registration_target_policy_current",
    "registration_category_metadata",
    "registration_category_metadata_revisions",
    "registration_category_metadata_current",
    "review_items",
    "review_item_events",
    "product_facts_revisions",
    "source_products",
    "product_groups",
    "product_items",
    "pricing_snapshots",
    "image_selection_revisions",
    "image_selection_outputs",
    "image_qa_results",
    "source_assets",
    "derived_image_artifacts",
    "live_grants",
    "protected_write_brakes",
    "asset_upload_attempts",
    "restore_drills",
    "retention_proofs",
)
FORWARD_ONLY_TRIGGERS: Final = (
    "trg_live_grants_forward_only",
    "trg_protected_write_brakes_one_generation_per_change",
    "trg_asset_upload_attempts_terminal_exactly_once",
)
_DELETING_WORDS: Final = ("delete", "purge", "prune", "cleanup", "retention", "expire_evidence")


@dataclass(frozen=True)
class RetentionChecks:
    checks: dict[str, Any]
    passed: bool

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.checks, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RetentionProofService:
    def __init__(
        self,
        *,
        db: Database,
        store: LiveAuthorityStore,
        job_types: Callable[[], Iterable[str]],
        safe_retention_profile_version: str,
        schema_head: Callable[[], str | None],
    ) -> None:
        self._db = db
        self._store = store
        self._job_types = job_types
        self._profile = safe_retention_profile_version
        self._head = schema_head

    def checks(self) -> RetentionChecks:
        """The retention checks as they are now. Unreadable truth fails them, never passes them."""
        head = self._head()
        live: SchemaManifest | None
        contract: SchemaManifest | None
        try:
            with self._db.read() as session:
                live = manifest_of(session.execute(text(MANIFEST_QUERY)).tuples().all())
        except Exception:
            live = None
        try:
            contract = expected_manifest(head) if head else None
        except Exception:
            contract = None
        deleting = sorted(
            job for job in self._job_types() if any(word in job.lower() for word in _DELETING_WORDS)
        )
        if live is None or contract is None:
            checks: dict[str, Any] = {
                "version": RETENTION_CHECKS_VERSION,
                "readable": live is not None,
                "schema_contract": contract is not None,
            }
            return RetentionChecks(checks, passed=False)
        expected = {table: contract.on_table(table) for table in PROTECTED_TABLES}
        present = {table: live.on_table(table) for table in PROTECTED_TABLES}
        forward = {o.name: o for guards in expected.values() for o in guards.values()}
        checks = {
            "version": RETENTION_CHECKS_VERSION,
            "readable": True,
            "schema_contract": True,
            "schema_head": head,
            # Every delete of the table is refused by a live trigger, whatever else it has.
            "no_delete_triggers": {
                table: any(refuses_every_delete(o, table) for o in present[table].values())
                for table in PROTECTED_TABLES
            },
            # Every trigger of the table is exactly the contract's: none dropped, altered or added.
            "guards_match_contract": {
                table: present[table] == expected[table] for table in PROTECTED_TABLES
            },
            "forward_only_triggers": {
                name: name in forward and live.objects.get(f"trigger:{name}") == forward[name]
                for name in FORWARD_ONLY_TRIGGERS
            },
            "contract_guard_digest": digest_of(
                o for guards in expected.values() for o in guards.values()
            ),
            "live_guard_digest": digest_of(
                o for guards in present.values() for o in guards.values()
            ),
            "deleting_job_types": deleting,
            "automatic_deletion_authorized": False,
            "sanitizer_rules_version": SANITIZER_RULES_VERSION,
            "safe_retention_profile_version": self._profile,
        }
        passed = (
            all(checks["no_delete_triggers"].values())
            and all(checks["guards_match_contract"].values())
            and all(checks["forward_only_triggers"].values())
            and not deleting
        )
        return RetentionChecks(checks, passed)

    def prove(self, *, actor: str, correlation_id: str) -> tuple[str, ProofVerdict]:
        """Run the checks now and record the proof. A failed check records a FAILED proof."""
        current = self.checks()
        verdict = ProofVerdict.PASSED if current.passed else ProofVerdict.FAILED
        with self._store.transaction() as unit:
            proof_id = unit.record_retention_proof(
                verdict=verdict,
                failure_code=None if current.passed else RETENTION_CHECK_FAILED,
                schema_head=str(current.checks.get("schema_head") or "unreadable"),
                check_digest=current.digest,
                checks=current.checks,
                actor=actor,
                correlation_id=correlation_id,
            )
        return proof_id, verdict

    def ready(self) -> bool:
        """``EVIDENCE_RETENTION_READY``: a PASSED proof of exactly the checks as they are now."""
        current = self.checks()
        if not current.passed:
            return False
        with self._store.reading() as unit:
            return unit.retention_proven(current.digest, str(current.checks["schema_head"]))
