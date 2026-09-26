"""The backup/restore drill (ADR-0018 §7): a proven drill, not a declaration.

A drill proves one stage's exact target, taken at one consistent point:

1. the chain the stage rests on is derived from the owners — for ASSET the preparation owner's
   current candidate and the grant; for CREATE the frozen Snapshot, its Intent and the execution
   copy of the exact revision that produced it — and turned into **elements**: owner rows named
   by their identities, each required, optional or required to be absent;
2. the restore target is the stack's own target digest for that stage (the digest admission
   computes at send time), so the proof is stale as soon as any of that state moves;
3. under the process write coordinator — no other writer can move the source — the owner-write
   fence is re-read (a write since step 1 fails the drill), the database is backed up with
   SQLite's online backup API into a **separate fresh root** (WAL-consistent; never a file copy),
   every element is read from the source, and the selected artifacts' bytes are copied;
4. the restored root proves ``PRAGMA integrity_check`` = ``ok`` and the Alembic head, and every
   element and artifact is compared **by identity and state** with the source;
5. one append-only drill record keeps the sanitized evidence — identities, states, counts,
   digests, versions, times and every element recorded as absent. No path, credential, secret or
   raw payload enters it.

Only state that cannot yet exist at the stage is recorded as absent; the drill never creates
anything in either root, and the only row it writes to the active root is its own record.
"""

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from app.audit.service import AuditLog
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database
from app.live.model import (
    DRILL_ARTIFACT_MISMATCH,
    DRILL_ELEMENT_MISMATCH,
    DRILL_INTEGRITY_FAILED,
    DRILL_REQUIRED_ELEMENT_ABSENT,
    DRILL_SCHEMA_NOT_AT_HEAD,
    DRILL_SOURCE_MOVED,
    DRILL_STAGE_MISMATCH,
    DRILL_TARGET_UNDETERMINABLE,
    MutationStage,
    ProofVerdict,
)
from app.live.stack import SafetyStack
from app.live.store import GLOBAL_BRAKE, LiveAuthorityStore
from app.products.image_model import ImageAssetKind
from app.register.model import IntentState
from app.register.store import RegistrationStore

DRILL_VERSION: Final = "restore-drill/v1"
CREATE_ENDPOINT_GROUP: Final = "product_registration"
_SENDABLE = (IntentState.PREPARED, IntentState.FAILED)


# ---------------------------------------------------------------- elements


@dataclass(frozen=True)
class Element:
    """One owner row set named by its identities. Tables and columns are code constants only."""

    name: str
    table: str
    where: tuple[tuple[str, Any], ...]
    required: bool = True
    must_be_absent: bool = False

    def query(self) -> tuple[str, tuple[Any, ...]]:
        predicate = " AND ".join(f"{column} = ?" for column, _ in self.where)
        return f"SELECT * FROM {self.table} WHERE {predicate}", tuple(v for _, v in self.where)


@dataclass(frozen=True)
class Reading:
    rows: int
    digest: str


def read_element(connection: sqlite3.Connection, element: Element) -> Reading:
    """Every column of every row the element names, canonical and digested: identity and state."""
    sql, values = element.query()
    cursor = connection.execute(sql, values)
    names = [d[0] for d in cursor.description]
    rows = sorted(
        (
            json.dumps(dict(zip(names, row, strict=True)), sort_keys=True, default=_plain)
            for row in cursor
        ),
    )
    return Reading(len(rows), _digest(rows))


@dataclass(frozen=True)
class Artifact:
    asset_kind: ImageAssetKind
    sha256: str

    @property
    def relative(self) -> Path:
        folder = (
            "source-assets" if self.asset_kind is ImageAssetKind.SOURCE_ASSET else "derived-images"
        )
        return Path(folder) / "sha256" / self.sha256[:2] / self.sha256


def unit_elements(resolved: Any, preparation: Any, revision_id: str) -> list[Element]:
    """The product and preparation sides of one unit, as the preflight resolved it (§7)."""
    elements = [
        Element(
            "canonical_account",
            "marketplace_accounts",
            (("marketplace_account_id", resolved.marketplace_account_id),),
        ),
        Element("draft", "registration_drafts", (("draft_id", resolved.draft_id),)),
        Element("draft_items", "registration_draft_items", (("draft_id", resolved.draft_id),)),
        Element(
            "preparation",
            "registration_preparations",
            (("preparation_id", preparation.preparation_id),),
        ),
        Element(
            "preparation_revision",
            "registration_preparation_revisions",
            (("preparation_revision_id", revision_id),),
        ),
        Element(
            "preparation_items",
            "registration_preparation_items",
            (("preparation_revision_id", revision_id),),
        ),
        Element(
            "target_policy_revision",
            "registration_target_policy_revisions",
            (("policy_revision_id", resolved.target.policy_revision),),
        ),
    ]
    if resolved.metadata is not None:
        elements.append(
            Element(
                "category_metadata_revision",
                "registration_category_metadata_revisions",
                (("metadata_revision_id", resolved.metadata.metadata_revision),),
            )
        )
    for item in resolved.items:
        tag = item.item_id
        elements += [
            Element(f"item:{tag}", "product_items", (("item_id", item.item_id),)),
            Element(
                f"group:{tag}", "product_groups", (("product_group_id", item.product_group_id),)
            ),
        ]
        if item.pin is not None:
            elements.append(
                Element(
                    f"price_pin:{tag}",
                    "pricing_snapshots",
                    (("pricing_snapshot_id", item.pin.pricing_snapshot_id),),
                )
            )
        if item.binding is not None:
            elements.append(
                Element(
                    f"source_revision:{tag}",
                    "product_facts_revisions",
                    (("revision_id", item.binding.provenance_revision_id),),
                )
            )
            if item.binding.source_product_uid is not None:
                elements.append(
                    Element(
                        f"source_product:{tag}",
                        "source_products",
                        (("source_product_uid", item.binding.source_product_uid),),
                    )
                )
        if item.selection_revision_id is not None:
            elements += [
                Element(
                    f"image_selection:{tag}",
                    "image_selection_revisions",
                    (("selection_revision_id", item.selection_revision_id),),
                ),
                Element(
                    f"image_outputs:{tag}",
                    "image_selection_outputs",
                    (("selection_revision_id", item.selection_revision_id),),
                ),
            ]
        for image in item.images:
            if image.qa_result_id is not None:
                elements.append(
                    Element(
                        f"image_qa:{image.sha256}",
                        "image_qa_results",
                        (("qa_result_id", image.qa_result_id),),
                    )
                )
            if image.asset_kind is ImageAssetKind.SOURCE_ASSET:
                elements.append(
                    Element(
                        f"artifact:{image.sha256}", "source_assets", (("sha256", image.sha256),)
                    )
                )
            else:
                elements.append(
                    Element(
                        f"artifact:{image.sha256}",
                        "derived_image_artifacts",
                        (("artifact_sha256", image.sha256),),
                    )
                )
    return _unique(elements)


def unit_artifacts(resolved: Any) -> list[Artifact]:
    found = {(i.asset_kind, i.sha256) for item in resolved.items for i in item.images}
    return [Artifact(kind, sha) for kind, sha in sorted(found)]


# ---------------------------------------------------------------- the drill


class CandidateEvaluator(Protocol):
    def evaluate(self, preparation_id: str) -> Any: ...

    def execution_copy(self, registration_snapshot_id: str) -> Any: ...


class AssetTargets(Protocol):
    """The ASSET upload path's own restore target and replay keys (``AssetUploadService``)."""

    def restore_target(self, grant_id: str) -> str | None: ...

    def replay_keys(self, grant_id: str) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class DrillPaths:
    data_dir: Path
    database_path: Path


@dataclass(frozen=True)
class DrillResult:
    drill_id: str
    verdict: ProofVerdict
    failure_code: str | None
    target_digest: str
    evidence: dict[str, Any] = field(default_factory=dict)


class RestoreDrillService:
    def __init__(
        self,
        *,
        db: Database,
        audit: AuditLog,
        paths: DrillPaths,
        store: LiveAuthorityStore,
        stack: SafetyStack,
        assets: AssetTargets,
        preparations: CandidateEvaluator,
        registrations: RegistrationStore,
        clock: Clock,
        schema_head: Callable[[], str | None],
    ) -> None:
        self._db = db
        self._audit = audit
        self._paths = paths
        self._store = store
        self._stack = stack
        self._assets = assets
        self._preparations = preparations
        self._registrations = registrations
        self._clock = clock
        self._head = schema_head

    # ------------------------------------------------------------------ the two stages

    def drill_asset(
        self, grant_id: str, *, restore_root: Path, actor: str, correlation_id: str
    ) -> DrillResult:
        """Prove the exact pre-upload chain of one ASSET grant (§7 'The ASSET restore proof')."""
        root = self._fresh_root(restore_root)
        grant = self._store.grant_record(grant_id)
        if grant is None or grant.stage is not MutationStage.ASSET:
            raise NotFoundError("DRILL_GRANT_NOT_FOUND", "no ASSET grant with this identity")
        fence = self._stack.truth_fence()
        preparation = self._registrations.preparation_of_revision(
            grant.preparation_revision_id or ""
        )
        if preparation is None:
            raise NotFoundError("DRILL_PREPARATION_NOT_FOUND", "the grant's preparation is gone")
        target = self._assets.restore_target(grant_id)
        if target is None:
            raise InputValidationError(DRILL_TARGET_UNDETERMINABLE, "no ASSET restore target")
        candidate = self._preparations.evaluate(preparation.preparation_id)
        revision = grant.preparation_revision_id or ""
        elements = unit_elements(candidate.resolved, preparation, revision)
        elements += [
            Element("grant", "live_grants", (("grant_id", grant_id),)),
            Element(
                "brake", "protected_write_brakes", (("brake_id", GLOBAL_BRAKE),), required=False
            ),
        ]
        elements += [
            # The whole replay-conflict scope of every selected artifact: an explicit, possibly
            # empty history read from the owner's table, never inferred from absence elsewhere.
            Element(
                f"replay_scope:{key}",
                "asset_upload_attempts",
                (("replay_key", key),),
                required=False,
            )
            for key in self._assets.replay_keys(grant_id)
        ]
        # Cannot exist yet before the upload: a Snapshot frozen from this revision (and so its
        # Intent, Attempts and registration). Present means this is not the ASSET stage.
        elements.append(
            Element(
                "snapshot_from_revision",
                "registration_snapshot_preparations",
                (("preparation_revision_id", revision),),
                required=False,
                must_be_absent=True,
            )
        )
        stage_ok = preparation.current.preparation_revision_id == revision
        return self._run(
            MutationStage.ASSET,
            grant_id,
            target,
            _unique(elements),
            unit_artifacts(candidate.resolved),
            fence,
            root,
            actor,
            correlation_id,
            stage_ok=stage_ok,
        )

    def drill_create(
        self, intent_id: str, *, restore_root: Path, actor: str, correlation_id: str
    ) -> DrillResult:
        """Prove the exact post-freeze chain of one Intent (§7 'The CREATE restore proof')."""
        root = self._fresh_root(restore_root)
        intent = self._registrations.intent(intent_id)
        if intent is None:
            raise NotFoundError("DRILL_INTENT_NOT_FOUND", "no registration Intent")
        fence = self._stack.truth_fence()
        snapshot_id = intent.registration_snapshot_id
        provenance = self._registrations.snapshot_preparation(snapshot_id)
        if provenance is None:
            raise NotFoundError("DRILL_PROVENANCE_NOT_FOUND", "the Snapshot has no provenance")
        preparation = self._registrations.preparation_of_revision(
            provenance.preparation_revision_id
        )
        if preparation is None:
            raise NotFoundError("DRILL_PREPARATION_NOT_FOUND", "the Snapshot's revision is gone")
        copy = self._preparations.execution_copy(snapshot_id)
        attempts = self._registrations.attempts(intent_id)
        scope = self._registrations.execution_scope(
            intent.marketplace_key, intent.marketplace_account_id, CREATE_ENDPOINT_GROUP
        )
        target = self._stack.create_restore_target(
            intent, attempt_no=len(attempts) + 1, scope=scope
        )
        resolved = copy.final.resolved
        elements = unit_elements(resolved, preparation, provenance.preparation_revision_id)
        elements += [
            # The REGISTER chain: never absent in a CREATE proof (§7).
            Element(
                "snapshot", "registration_snapshots", (("registration_snapshot_id", snapshot_id),)
            ),
            Element(
                "item_snapshots",
                "registration_item_snapshots",
                (("registration_snapshot_id", snapshot_id),),
            ),
            Element(
                "snapshot_provenance",
                "registration_snapshot_preparations",
                (("registration_snapshot_id", snapshot_id),),
            ),
            Element(
                "batch",
                "registration_batches",
                (("registration_batch_id", intent.registration_batch_id),),
            ),
            Element("intent", "registration_intents", (("intent_id", intent_id),)),
            # §26's brake: an absent row is an ACTIVE scope, and the owner's reading is recorded.
            Element(
                "execution_scope",
                "registration_execution_scopes",
                (
                    ("marketplace_key", intent.marketplace_key),
                    ("marketplace_account_id", intent.marketplace_account_id),
                    ("endpoint_group", CREATE_ENDPOINT_GROUP),
                ),
                required=False,
            ),
            Element(
                "brake", "protected_write_brakes", (("brake_id", GLOBAL_BRAKE),), required=False
            ),
            Element("create_grants", "live_grants", (("intent_id", intent_id),), required=False),
            # What cannot exist yet before the CREATE may be absent: its Attempts and registration.
            Element(
                "attempts", "registration_attempts", (("intent_id", intent_id),), required=False
            ),
            Element(
                "registration",
                "marketplace_registrations",
                (("intent_id", intent_id),),
                required=False,
            ),
        ]
        for group in sorted({item.product_group_id for item in resolved.items}):
            elements.append(
                Element(
                    f"duplicate_overrides:{group}",
                    "duplicate_overrides",
                    (
                        ("marketplace_account_id", intent.marketplace_account_id),
                        ("product_group_id", group),
                    ),
                    required=False,
                )
            )
        for other in self._registrations.conflicting_intents(snapshot_id):
            elements.append(
                Element(f"conflict:{other}", "registration_intents", (("intent_id", other),))
            )
        return self._run(
            MutationStage.CREATE,
            intent_id,
            target,
            _unique(elements),
            unit_artifacts(resolved),
            fence,
            root,
            actor,
            correlation_id,
            stage_ok=intent.state in _SENDABLE,
            scope_state=scope.state.value,
        )

    # ------------------------------------------------------------------ the common drill

    def _run(
        self,
        stage: MutationStage,
        unit_ref: str,
        target: str,
        elements: Sequence[Element],
        artifacts: Sequence[Artifact],
        fence: int,
        root: Path,
        actor: str,
        correlation_id: str,
        *,
        stage_ok: bool,
        scope_state: str | None = None,
    ) -> DrillResult:
        started = self._clock.now()
        head = self._head() or ""
        restored_db = root / "runtime" / self._paths.database_path.name
        source: dict[str, Reading] = {}
        moved = False
        # The consistent point: no other writer can move the source while the backup is taken.
        with self._db.write() as session:
            if self._audit.owner_writes(session) != fence:
                moved = True
            else:
                restored_db.parent.mkdir(parents=True, exist_ok=True)
                with _Readonly(self._paths.database_path) as src:
                    _backup_into_fresh_root(src, restored_db)
                    source = {e.name: read_element(src, e) for e in elements}
                for artifact in artifacts:
                    origin = self._paths.data_dir / artifact.relative
                    if origin.is_file():
                        (root / artifact.relative).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(origin, root / artifact.relative)
        evidence: dict[str, Any] = {
            "drill_version": DRILL_VERSION,
            "stage": stage.value,
            "target_digest": target,
            "schema_head": head,
            "scope_state": scope_state,
        }
        failure: str | None = None
        integrity: str | None = None
        backup_digest: str | None = None
        records: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        if moved:
            failure = DRILL_SOURCE_MOVED
        else:
            backup_digest = hashlib.sha256(restored_db.read_bytes()).hexdigest()
            with _Readonly(restored_db) as restored:
                integrity = str(restored.execute("PRAGMA integrity_check").fetchone()[0])
                version = restored.execute("SELECT version_num FROM alembic_version").fetchone()
                for element in elements:
                    before, after = source[element.name], read_element(restored, element)
                    records.append(
                        {
                            "element": element.name,
                            "table": element.table,
                            "keys": dict(element.where),
                            "state": "PRESENT" if before.rows else "ABSENT",
                            "rows": before.rows,
                            "digest": before.digest,
                            "restored_digest": after.digest,
                            "match": before == after,
                        }
                    )
            for artifact in artifacts:
                copied = root / artifact.relative
                restored_sha = (
                    hashlib.sha256(copied.read_bytes()).hexdigest() if copied.is_file() else None
                )
                files.append(
                    {
                        "asset_kind": artifact.asset_kind.value,
                        "sha256": artifact.sha256,
                        "match": restored_sha == artifact.sha256,
                    }
                )
            failure = _judge(
                elements,
                records,
                files,
                integrity=integrity,
                version=None if version is None else str(version[0]),
                head=head,
                stage_ok=stage_ok,
            )
        evidence |= {"integrity": integrity, "elements": records, "artifacts": files}
        absent = [r["element"] for r in records if r["state"] == "ABSENT"]
        evidence["absent"] = absent
        verdict = ProofVerdict.PASSED if failure is None else ProofVerdict.FAILED
        with self._store.transaction() as unit:
            drill_id = unit.record_drill(
                stage=stage,
                unit_ref=unit_ref,
                target_digest=target,
                verdict=verdict,
                failure_code=failure,
                schema_head=head,
                integrity=integrity,
                backup_digest=backup_digest,
                restore_root_digest=hashlib.sha256(str(root).casefold().encode()).hexdigest(),
                evidence=evidence,
                element_count=len(records),
                absent_count=len(absent),
                started_at=started,
                actor=actor,
                correlation_id=correlation_id,
            )
        return DrillResult(drill_id, verdict, failure, target, evidence)

    def _fresh_root(self, restore_root: Path) -> Path:
        """A separate fresh root: absolute, outside the active data root, new or empty."""
        root = restore_root.resolve()
        active = self._paths.data_dir.resolve()
        if not restore_root.is_absolute():
            raise InputValidationError("DRILL_RESTORE_ROOT_INVALID", "the restore root is absolute")
        if root == active or active in root.parents or root in active.parents:
            raise InputValidationError(
                "DRILL_RESTORE_ROOT_INVALID",
                "a drill never restores over or inside the active root",
            )
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise InputValidationError(
                "DRILL_RESTORE_ROOT_INVALID", "the restore root is not fresh"
            )
        return root


def _judge(
    elements: Sequence[Element],
    records: Sequence[dict[str, Any]],
    files: Sequence[dict[str, Any]],
    *,
    integrity: str | None,
    version: str | None,
    head: str,
    stage_ok: bool,
) -> str | None:
    if integrity != "ok":
        return DRILL_INTEGRITY_FAILED
    if not head or version != head:
        return DRILL_SCHEMA_NOT_AT_HEAD
    by_name = {record["element"]: record for record in records}
    if not stage_ok or any(e.must_be_absent and by_name[e.name]["rows"] for e in elements):
        return DRILL_STAGE_MISMATCH
    if any(e.required and not by_name[e.name]["rows"] for e in elements):
        return DRILL_REQUIRED_ELEMENT_ABSENT
    if not all(record["match"] for record in records):
        return DRILL_ELEMENT_MISMATCH
    if not all(file["match"] for file in files):
        return DRILL_ARTIFACT_MISMATCH
    return None


def _backup_into_fresh_root(source: sqlite3.Connection, restored_db: Path) -> None:
    """The one writable connection a drill opens: onto the restore root ``_fresh_root`` validated,
    which is never the active data root nor inside it (ADR-0006, ADR-0018 §7)."""
    destination = sqlite3.connect(restored_db)
    try:
        source.backup(destination)
    finally:
        destination.close()


class _Readonly:
    """A read-only SQLite connection that is always closed."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)

    def __enter__(self) -> sqlite3.Connection:
        return self._connection

    def __exit__(self, *_: object) -> None:
        self._connection.close()


def _unique(elements: Sequence[Element]) -> list[Element]:
    seen: dict[str, Element] = {}
    for element in elements:
        seen.setdefault(element.name, element)
    return list(seen.values())


def _plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _digest(rows: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


__all__ = [
    "Artifact",
    "DrillPaths",
    "DrillResult",
    "Element",
    "RestoreDrillService",
    "read_element",
    "unit_artifacts",
    "unit_elements",
]
