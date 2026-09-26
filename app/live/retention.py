"""The evidence-retention proof (ADR-0018 §8): end to end, fail closed.

``EVIDENCE_RETENTION_READY`` is never asserted. It holds only while a recorded PASSED proof matches
the retention checks **as they are now**, read from the live database and the running process:

- every canary-scope durable table — the REGISTER rows, the pre-LIVE owners, the audit trail, the
  review owner, and the M4 product, pricing and image truth a Snapshot and a drill rest on — keeps
  its ``BEFORE DELETE`` refusal trigger, so no row of that evidence can be deleted by any path;
- the forward-only triggers of the grant, the brake and the ASSET attempt owner are in place, so
  unresolved evidence (an ``UPLOAD_UNKNOWN``, a consumed grant) cannot be rewritten away;
- **no automatic deletion is authorized**: no registered job type names a delete, purge, prune,
  cleanup or retention action (a later deletion policy needs its own decision);
- the sanitizer and the adapter's safe-retention profile versions evidence is recorded under.

Any failed check makes the proof FAILED and the readiness false. A change of schema head or of any
check makes an earlier proof stale. Sanitation before hash or persist stays the owners' rule
(ADR-0014 §15); this proof only shows that the path which keeps the evidence is intact.
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text

from app.db.database import Database
from app.live.model import RETENTION_CHECK_FAILED, ProofVerdict
from app.live.store import LiveAuthorityStore
from app.register.sanitize import SANITIZER_RULES_VERSION

RETENTION_CHECKS_VERSION: Final = "evidence-retention-checks/v1"

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
        try:
            with self._db.read() as session:
                rows = session.execute(
                    text("SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'")
                ).all()
        except Exception:
            rows = None
        deleting = sorted(
            job for job in self._job_types() if any(word in job.lower() for word in _DELETING_WORDS)
        )
        if rows is None:
            checks: dict[str, Any] = {"version": RETENTION_CHECKS_VERSION, "readable": False}
            return RetentionChecks(checks, passed=False)
        guarded = {table for _, table, sql in rows if "BEFORE DELETE" in (sql or "").upper()}
        names = {name for name, _, _ in rows}
        checks = {
            "version": RETENTION_CHECKS_VERSION,
            "readable": True,
            "schema_head": self._head(),
            "no_delete_triggers": {table: table in guarded for table in PROTECTED_TABLES},
            "forward_only_triggers": {name: name in names for name in FORWARD_ONLY_TRIGGERS},
            "deleting_job_types": deleting,
            "automatic_deletion_authorized": False,
            "sanitizer_rules_version": SANITIZER_RULES_VERSION,
            "safe_retention_profile_version": self._profile,
        }
        passed = (
            bool(checks["schema_head"])
            and all(checks["no_delete_triggers"].values())
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
