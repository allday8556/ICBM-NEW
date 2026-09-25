"""One operator-submitted product, collected end to end (ADR-0010 §3–§9).

This is generic COLLECT core, and it owns the whole run: the durable job, the order the steps run
in, which references are worth a request, how a refusal is classified, what is stored, what is
appended and what the operator is told. A supplier contributes four things and no more — where its
product pages live, what its document says the product is, what its fields mean, and which of its
image references are product evidence.

The shape of a run::

    operator URL → durable ``collect.product`` job → one product read through the policed gateway
    → the supplier's identity rule → the supplier's field parser → the supplier's image roles
    → bounded image fetches through the same gateway → SourceAssetRecorder → revision append
    → the run's terminal outcome, which the API reads back.

Three rules hold the run closed:

*A page is read once.* One product document: no listing, no pagination, no related product, no
second page of any kind. The profile's own path form is the only thing the gateway will fetch, and
nothing found inside a document is followed.

*An unresolved identity is an answer, not an error.* When the source states no stable identity the
run appends no revision and ends ``NO_REVISION``. Retrying would read the same page and reach the
same answer, so it is neither raised nor retried.

*Ambiguity never becomes traffic.* A field the parser leaves ``REVIEW_REQUIRED`` changes nothing
about what is requested — no re-read, no extra fetch, no provider-debug loop. Only the transient
classes the shared job policy already approves are ever retried.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.collect.facts import (
    CollectedFacts,
    FactsStatus,
    FetchTargetRefusal,
    ImageIssue,
    ImageReference,
    ImageRole,
)
from app.collect.models import CollectionOutcome
from app.collect.revisions import ProductFactsRevisionStore
from app.collect.runs import (
    CollectionRunRecord,
    CollectionRunStore,
    PacingKey,
    SameProductTooSoon,
)
from app.collect.shadow import FrozenRun, ShadowInput, ShadowStep
from app.collect.sourceassets import (
    FetchedImage,
    RevalidatedImage,
    SourceAssetRecorder,
    SourceImage,
    UnfetchedImage,
)
from app.collect.urls import UrlPolicy
from app.core.clock import Clock
from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.db.database import Database
from app.jobs.policy import RetryPolicy
from app.jobs.registry import JobContext, JobDefinition, TerminalJob
from app.jobs.service import JobService
from integrations.suppliers.collection import (
    CollectionProfile,
    DocumentView,
    ImageCandidate,
    ImageResponse,
    ReadKind,
    SourceIdentity,
    SourceIdentityResult,
    SupplierCollection,
)
from integrations.suppliers.collection import ImageRole as SourceRole
from integrations.suppliers.transport.collection import (
    CollectionBudgetRefused,
    CollectionTargetRefused,
    ImageFetchRefused,
    RequestBudget,
    check_target,
    image_fetch_target,
)

logger = logging.getLogger("icbm.collect")

COLLECT_PRODUCT_JOB = "collect.product"
# What a run says when its job ended without the run reaching an answer of its own. It names the
# lifecycle fact and the job system's own classification, and claims nothing about the source.
UNFINISHED_RUN = "JOB_ENDED_WITHOUT_RESULT"
# A collection reads one page and fetches bounded images. A transient provider failure is worth
# another attempt; nothing else is, and the shared error taxonomy decides which classes those are.
#
# Every delay here is longer than the same-product floor of ADR-0010 §4, deliberately: a retry is
# another *real* read of the same product, so the schedule has to be compatible with the interval
# rather than something the interval has to keep refusing.
COLLECT_POLICY = RetryPolicy(max_attempts=3, base_delay_s=90.0, factor=2.0, max_delay_s=600.0)
# How many recent runs the COLLECT screen may ask for at once (Gate 1 G1-E).
RECENT_RUNS_DEFAULT = 10
RECENT_RUNS_MAX = 50

# What a supplier's own page role means in the revision's canonical vocabulary. A page's primary
# image is the product's representative one; everything else the role rules recognise as product
# evidence is detail. ``UI_COMMON`` and ``UNKNOWN`` are absent on purpose: a layout asset and a
# reference no rule recognised are never collected as product evidence (comment 5696242775 §2).
CANONICAL_ROLE: Mapping[SourceRole, ImageRole] = {
    SourceRole.PRIMARY: ImageRole.REPRESENTATIVE,
    SourceRole.DETAIL: ImageRole.DETAIL,
    SourceRole.THUMBNAIL: ImageRole.DETAIL,
    SourceRole.PRODUCT_AUX: ImageRole.DETAIL,
}


class SessionProvider(Protocol):
    """What the run needs from CONNECT: one session payload, used in memory and never stored."""

    def collection_session(
        self, supplier_key: str, *, operator_initiated: bool = False
    ) -> bytes: ...


class CollectionGateway(Protocol):
    """What the run needs from the transport. The policed gateway is the only implementation."""

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView: ...

    def read_image(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        budget: RequestBudget,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
    ) -> ImageResponse: ...


@dataclass(frozen=True)
class RegisteredCollection:
    """One supplier's collection, bound to the extraction identity that produced its rules."""

    collection: SupplierCollection
    extractor_revision: str
    extractor_fingerprint: str

    def __post_init__(self) -> None:
        if self.extractor_revision != self.collection.roles.identity:
            raise ValueError("the image-role rules and the manifest must name one identity")

    @property
    def supplier_key(self) -> str:
        return self.collection.supplier_key


@dataclass(frozen=True)
class KnownAsset:
    """Bytes an earlier collection stored, and the validators the provider gave for them."""

    sha256: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class SubmittedCollection:
    """What the operator is handed the moment a collection is accepted."""

    collection_run_id: str
    job_id: str
    correlation_id: str


@dataclass(frozen=True)
class RecordedSource:
    """What a RECORDED run appended: the source identity its revision states, and that revision."""

    collection_run_id: str
    supplier_key: str
    source_product_id: str
    revision_id: str


class CollectionRunNotRecorded(AppError):
    """The run has no revision to follow: it is still PENDING, or ended without appending one."""

    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class CollectionResult:
    """What one execution produced, before it becomes the run's terminal outcome."""

    revision_id: str | None
    facts_status: FactsStatus | None
    reason: str | None


@dataclass
class RunBudget:
    """One run's own bounds, reserved before any byte is sent.

    The gateway checks whether a target is permitted; this decides whether the request may happen
    at all. It is per-run and in memory — a durable reservation ledger belongs to the acceptance
    harness, which must prove a bounded campaign, not to a single production collection.

    A policy read is not part of a collection: the profile's public documents were read during
    reconnaissance and are not re-read per product.
    """

    max_product_reads: int
    max_image_requests: int
    spent: dict[ReadKind, int] = field(default_factory=dict)

    def caps(self) -> Mapping[ReadKind, int]:
        return {
            ReadKind.PRODUCT_READ: self.max_product_reads,
            ReadKind.IMAGE_REQUEST: self.max_image_requests,
            ReadKind.POLICY_READ: 0,
        }

    def reserve(self, kind: ReadKind, subject: str) -> None:
        used = self.spent.get(kind, 0)
        if used >= self.caps()[kind]:
            raise CollectionBudgetRefused(
                "COLLECT_BUDGET_EXHAUSTED", f"this run may not make another {kind.value}"
            )
        self.spent[kind] = used + 1


class ProductCollectionService:
    """The COLLECT stage's production owner: submit one product, collect it, read it back."""

    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        jobs: JobService,
        runs: CollectionRunStore,
        revisions: ProductFactsRevisionStore,
        recorder: SourceAssetRecorder,
        sessions: SessionProvider,
        gateway: CollectionGateway,
        collections: Sequence[RegisteredCollection] = (),
        after_recorded: Callable[[str], object] | None = None,
        shadow: ShadowStep | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._jobs = jobs
        self._runs = runs
        self._revisions = revisions
        self._recorder = recorder
        self._sessions = sessions
        self._gateway = gateway
        self._collections = {registered.supplier_key: registered for registered in collections}
        # What follows a durably RECORDED run (M4 PR-C): the product owner's materialization,
        # handed in so COLLECT never depends on it. It is idempotent, so a replayed attempt of a
        # RECORDED run calls it again and changes nothing that already follows the run.
        self._after_recorded = after_recorded
        # The one-fetch shadow comparison (ADR-0017 §10), handed in so COLLECT never depends on
        # the Adaptive packages. It runs only for a run whose frozen decision is ENABLED, only
        # after the canonical write, in its own unit, and nothing it does reaches the run.
        self._shadow = shadow

    # ------------------------------------------------------------------ submission

    def job_definition(self) -> JobDefinition:
        return JobDefinition(
            job_type=COLLECT_PRODUCT_JOB,
            handler=self._run_job,
            description="Collect one supplier product into an immutable source-truth revision.",
            # A collection only reads, and one run leaves one revision: an interrupted attempt is
            # recovered from the revision it already appended, never collected a second time.
            idempotent=True,
            retry_policy=COLLECT_POLICY,
            # A run belongs to its job: when the job can no longer run, the run has its answer.
            on_terminal=self._settle_unfinished_run,
            # And where a run still waiting for one can be found again, so that answer does not
            # depend on the call above having happened, or having worked.
            unsettled_owned_jobs=self._runs.unsettled_job_ids,
        )

    def _settle_unfinished_run(self, terminal: TerminalJob) -> None:
        """Close a run whose job ended without the run reaching an outcome of its own.

        Every answer a collection can give — recorded, no revision, a classified failure — is
        settled by :meth:`_run_job` itself. This is only reached when the job ended some other
        way: an exception no one classified, an interrupted attempt that will not resume, or a
        handler that returned without settling. The run is then FAILED and says exactly that, with
        the job system's own classification of what happened; it is never given a source-truth
        meaning it has no evidence for, and no revision is invented for it. What the attempt
        already consumed stays consumed: the same-product read it reserved is untouched, so a
        failure buys no earlier next read.

        It is called when the job ends and again by the reconciliation sweep, in any order and any
        number of times: a run that already has an answer is left exactly as it is.
        """
        record = self._runs.for_job(terminal.job_id)
        if record is None or record.outcome is not CollectionOutcome.PENDING:
            return
        detail = f"{UNFINISHED_RUN}:{terminal.error_code or terminal.state}"
        self._runs.failed(record.collection_run_id, detail=detail)
        logger.error(
            "collect.run_unfinished",
            extra={
                "collection_run_id": record.collection_run_id,
                "job_id": terminal.job_id,
                "job_state": terminal.state,
                "error_class": terminal.error_class,
                "error_code": terminal.error_code,
            },
        )

    def supplier_keys(self) -> list[str]:
        return sorted(self._collections)

    def submit(self, supplier_key: str, product_url: str) -> SubmittedCollection:
        """Accept exactly one product URL and return the run's durable identity.

        The URL is checked against the supplier's own profile here, so an unacceptable target is
        refused before a job exists rather than becoming a failed run.
        """
        registered = self._registered(supplier_key)
        profile = registered.collection.profile
        try:
            # The same rule the transport applies, so a URL the profile already knows is
            # unacceptable — a foreign host, a listing path, a query key that is not explicitly
            # safe — never becomes a durable job and a failed run.
            check_target(profile, product_url, ReadKind.PRODUCT_READ)
        except CollectionTargetRefused as refused:
            raise InputValidationError("COLLECT_URL_REFUSED", refused.message) from None
        remaining = self._runs.seconds_until_readable(
            pacing_key(registered.collection, product_url),
            interval_s=profile.limits.same_product_interval_s,
        )
        if remaining > 0:
            raise SameProductTooSoon(remaining)
        with self._db.write() as session:
            job = self._jobs.enqueue(
                COLLECT_PRODUCT_JOB,
                payload={"supplier_key": supplier_key, "product_url": product_url},
                target_ref=supplier_key,
                session=session,
            )
            run_id = self._runs.open(
                session,
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                supplier_key=supplier_key,
                source_url=product_url,
            )
        self._jobs.notify_worker()
        logger.info(
            "collect.submitted",
            extra={"collection_run_id": run_id, "job_id": job.job_id, "supplier": supplier_key},
        )
        return SubmittedCollection(run_id, job.job_id, job.correlation_id)

    def run(self, collection_run_id: str) -> CollectionRunRecord:
        return self._runs.get(collection_run_id)

    def recent_runs(self, limit: int | None = None) -> tuple[CollectionRunRecord, ...]:
        """The newest runs, newest first: what the COLLECT screen follows after a reload."""
        size = RECENT_RUNS_DEFAULT if limit is None else limit
        if isinstance(size, bool) or not 1 <= size <= RECENT_RUNS_MAX:
            raise InputValidationError(
                "COLLECT_RUN_LIMIT_INVALID", f"between 1 and {RECENT_RUNS_MAX} runs are listed"
            )
        return self._runs.recent(limit=size)

    def recorded_source(self, collection_run_id: str) -> RecordedSource:
        """The source identity and revision a RECORDED run appended, read from that revision.

        Any other run has none: a PENDING run has not finished, and a NO_REVISION or FAILED run
        appended nothing, so there is nothing a product could have been materialized from.
        """
        record = self._runs.get(collection_run_id)
        revision = (
            None
            if record.outcome is not CollectionOutcome.RECORDED or record.revision_id is None
            else self._revisions.get(record.revision_id)
        )
        if revision is None:
            raise CollectionRunNotRecorded(
                "COLLECT_RUN_NOT_RECORDED",
                "only a RECORDED run names a source revision",
                details={"outcome": record.outcome.value},
            )
        return RecordedSource(
            collection_run_id=record.collection_run_id,
            supplier_key=revision.supplier_key,
            source_product_id=revision.source_product_id,
            revision_id=revision.revision_id,
        )

    # ------------------------------------------------------------------ execution

    def _run_job(self, context: JobContext) -> None:
        record = self._runs.for_job(context.job_id)
        if record is None:
            raise NotFoundError("COLLECT_RUN_UNKNOWN", "this job has no collection run")
        if record.outcome is not CollectionOutcome.PENDING:
            # The run already has its answer; another attempt never rewrites it. A RECORDED one
            # may still owe what follows it, if the attempt that recorded it died before that.
            if record.outcome is CollectionOutcome.RECORDED:
                self._recorded(record.collection_run_id)
            return
        if (appended := self._revisions.for_run(record.collection_run_id)) is not None:
            # The previous attempt appended this run's revision and died before settling the run.
            # The revision is immutable and is already the answer: finish the run from it rather
            # than reading the provider again and appending a second one.
            logger.info(
                "collect.recovered",
                extra={
                    "collection_run_id": record.collection_run_id,
                    "revision_id": appended.revision_id,
                },
            )
            self._runs.recovered(
                record.collection_run_id,
                revision_id=appended.revision_id,
                facts_status=appended.facts_status,
            )
            self._recorded(record.collection_run_id)
            return
        try:
            result = self.collect(
                record.supplier_key, record.source_url, run_id=record.collection_run_id
            )
        except AppError as error:
            if COLLECT_POLICY.allows_retry(error.error_class, context.attempt_no):
                # The shared policy says this class is worth another attempt, so the run stays
                # PENDING and the job's own attempt history carries the failure. Nothing here
                # invents a retry of its own, and no other class ever reaches one.
                raise
            self._runs.failed(record.collection_run_id, detail=error.code)
            raise
        if result.revision_id is None or result.facts_status is None:
            self._runs.no_revision(
                record.collection_run_id, reason=result.reason or "the identity is unresolved"
            )
            return
        self._runs.recorded(
            record.collection_run_id,
            revision_id=result.revision_id,
            facts_status=result.facts_status,
        )
        self._recorded(record.collection_run_id)

    def _recorded(self, collection_run_id: str) -> None:
        """Hand a run to what follows it, only once its RECORDED outcome is durable."""
        if self._after_recorded is not None:
            self._after_recorded(collection_run_id)

    def collect(self, supplier_key: str, product_url: str, *, run_id: str) -> CollectionResult:
        """Read one product and append its revision, or say why there is none."""
        registered = self._registered(supplier_key)
        collection = registered.collection
        profile = collection.profile
        budget = RunBudget(
            max_product_reads=1, max_image_requests=profile.limits.max_image_requests_per_run
        )
        # One real product read, taken durably before anything is sent. Every attempt passes
        # through here — a retry of this same run included — so the same product is never read
        # twice inside the interval ADR-0010 §4 fixes, restart or no restart.
        frozen = self._runs.reserve_product_read(
            run_id,
            key=pacing_key(collection, product_url),
            interval_s=profile.limits.same_product_interval_s,
        )
        captured_at = self._clock.now()
        document = self._gateway.read_document(
            profile,
            product_url,
            kind=ReadKind.PRODUCT_READ,
            budget=budget,
            session=self._sessions.collection_session(supplier_key),
        )
        identity = collection.identity(document, product_url)
        if not isinstance(identity, SourceIdentity):
            logger.info(
                "collect.identity_unresolved",
                extra={"collection_run_id": run_id, "supplier": supplier_key},
            )
            reason = identity.reason or "the identity is unresolved"
            # The canonical answer is durable before the shadow looks (ADR-0017 §10.1; review
            # 5312254605 B1): a crash after this point leaves a settled NO_REVISION run that is
            # never read again, and its missing shadow is the blocking SHADOW_MISSING.
            self._runs.no_revision(run_id, reason=reason)
            self._shadow_step(
                frozen,
                run_id=run_id,
                registered=registered,
                product_url=product_url,
                document=document,
                identity=identity,
                collected=None,
                candidates=(),
                images=(),
                revision_id=None,
            )
            return CollectionResult(None, None, reason)
        # The source has now said which product this is, so that is what the interval follows
        # from here: another accepted form of this product's URL buys no second read.
        self._runs.note_identity(run_id, source_product_id=identity.source_product_id)
        candidates = tuple(collection.roles.classify(document.body, product_url))
        images = self._images(
            profile,
            candidates,
            budget=budget,
            known=self._known_assets(supplier_key, identity.source_product_id),
        )
        collected = CollectedFacts(
            supplier_key=supplier_key,
            source_product_id=identity.source_product_id,
            source_url=product_url,
            captured_at=captured_at,
            extractor_revision=registered.extractor_revision,
            extractor_fingerprint=registered.extractor_fingerprint,
            collection_run_id=run_id,
            correlation_id=get_correlation_id() or new_correlation_id(),
            fields=collection.fields(document),
            images=images,
        )
        stored = self._revisions.append(collected, url_policy=url_policy_of(profile))
        logger.info(
            "collect.recorded",
            extra={
                "collection_run_id": run_id,
                "revision_id": stored.revision_id,
                "facts_status": stored.facts_status.value,
                "image_refs": len(stored.images),
            },
        )
        # The revision's unit has committed; only now may the shadow look (ADR-0017 §10.1).
        self._shadow_step(
            frozen,
            run_id=run_id,
            registered=registered,
            product_url=product_url,
            document=document,
            identity=identity,
            collected=collected,
            candidates=candidates,
            images=images,
            revision_id=stored.revision_id,
        )
        return CollectionResult(stored.revision_id, stored.facts_status, None)

    def _shadow_step(
        self,
        frozen: FrozenRun,
        *,
        run_id: str,
        registered: RegisteredCollection,
        product_url: str,
        document: DocumentView,
        identity: SourceIdentityResult,
        collected: CollectedFacts | None,
        candidates: tuple[ImageCandidate, ...],
        images: tuple[ImageReference, ...],
        revision_id: str | None,
    ) -> None:
        """Hand the shadow what this run already holds, and nothing it could spend.

        Only a run whose frozen decision is ENABLED reaches the step, so a disabled run makes no
        Adaptive call at all. The canonical write has committed before this is called — the
        revision append, or the NO_REVISION outcome itself — and no unit is open here. The step
        promises never to raise; if it ever does, the run does not notice.
        """
        if self._shadow is None or frozen.shadow.decision != "ENABLED":
            return
        try:
            self._shadow(
                ShadowInput(
                    collection_run_id=run_id,
                    supplier_key=registered.supplier_key,
                    source_url=product_url,
                    frozen=frozen,
                    document=document,
                    identity=identity,
                    collected=collected,
                    url_policy=url_policy_of(registered.collection.profile),
                    candidates=candidates,
                    images=images,
                    revision_id=revision_id,
                )
            )
        except Exception:
            logger.exception("collect.shadow_escaped", extra={"collection_run_id": run_id})

    # ------------------------------------------------------------------ images

    def _known_assets(self, supplier_key: str, source_product_id: str) -> Mapping[str, KnownAsset]:
        """What an earlier collection already stored for this product, by stable locator.

        Only a reference whose own bytes are stored and whose provider gave a validator can be
        revalidated; everything else is fetched again. The same URL is never assumed to mean the
        same content — the provider has to say so.
        """
        previous = self._revisions.latest(supplier_key, source_product_id)
        if previous is None:
            return {}
        return {
            reference.locator: KnownAsset(
                sha256=reference.sha256,
                etag=reference.etag,
                last_modified=reference.last_modified,
            )
            for reference in previous.images
            if reference.locator is not None
            and reference.sha256 is not None
            and (reference.etag or reference.last_modified)
        }

    def _images(
        self,
        profile: CollectionProfile,
        candidates: Sequence[ImageCandidate],
        *,
        budget: RunBudget,
        known: Mapping[str, KnownAsset] = MappingProxyType({}),
    ) -> tuple[ImageReference, ...]:
        """Fetch what the page's own roles call product evidence, in the page's own order.

        The references and their roles are the supplier's. Whether to spend a request on one, and
        what a refusal means, are not. Nothing here computes a checksum or writes a byte — the
        recorder does that, and it is the only thing that does.
        """
        limits = profile.limits
        wanted = [c for c in candidates if c.role in CANONICAL_ROLE][: limits.max_image_refs]
        collected: list[SourceImage] = []
        spent_bytes = 0
        for candidate in wanted:
            role = CANONICAL_ROLE[candidate.role]
            # What the reference resolves to is the transport's own judgement, asked once and
            # recorded whether or not a request follows. A refusal here is exactly the refusal the
            # gateway would make before reserving anything, so no request is made for it.
            locator: str | None = None
            refusal: FetchTargetRefusal | None = None
            try:
                locator = image_fetch_target(profile, candidate.url).locator
            except CollectionTargetRefused as refused:
                refusal = refused.reason
            unreadable = _unfetched(candidate, role, locator=locator, refusal=refusal)
            stored = known.get(locator) if locator else None
            # What this run may still spend. The bound goes to the gateway, so a body over it is
            # refused as it arrives and never reaches the store: the advertised run total is a
            # hard cap, not an average.
            allowance = limits.max_new_image_bytes_per_run - spent_bytes
            if allowance <= 0:
                collected.append(unreadable(ImageIssue.BUDGET_EXHAUSTED))
                continue
            if refusal is not None:
                # The gateway would refuse it before reserving anything: the same outcome, and the
                # reason is now kept. Nothing about the URL is repaired to make it fetchable.
                collected.append(unreadable(ImageIssue.FETCH_FAILED))
                continue
            try:
                response = self._gateway.read_image(
                    profile,
                    candidate.url,
                    budget=budget,
                    etag=stored.etag if stored else None,
                    last_modified=stored.last_modified if stored else None,
                    max_bytes=min(limits.max_image_bytes, allowance),
                )
            except CollectionBudgetRefused:
                collected.append(unreadable(ImageIssue.BUDGET_EXHAUSTED))
                continue
            except CollectionTargetRefused as refused:
                # The same judge refused it at the gateway; keep its reason as well.
                collected.append(
                    _unfetched(candidate, role, locator=None, refusal=refused.reason)(
                        ImageIssue.FETCH_FAILED
                    )
                )
                continue
            except ImageFetchRefused as refused:
                issue = ImageIssue(refused.issue.value)
                if issue is ImageIssue.OVERSIZE and allowance < limits.max_image_bytes:
                    # It fitted the profile's per-image bound; what it did not fit was what this
                    # run had left.
                    issue = ImageIssue.BUDGET_EXHAUSTED
                collected.append(unreadable(issue))
                continue
            except AppError:
                # One image a provider would not serve is not a failed collection: the reference
                # records that it could not be turned into bytes, and the revision keeps it.
                collected.append(unreadable(ImageIssue.FETCH_FAILED))
                continue
            if response.not_modified:
                if stored is None:
                    # Nothing of ours to reuse: a 304 we cannot match is no evidence at all.
                    collected.append(unreadable(ImageIssue.FETCH_FAILED))
                    continue
                # The provider confirmed the bytes already stored. The new revision points at
                # exactly that asset, in this page's own order.
                collected.append(
                    RevalidatedImage(
                        role=role,
                        ordinal=candidate.order,
                        host=candidate.host,
                        provenance=candidate.rule,
                        sha256=stored.sha256,
                        locator=locator,
                        http_etag=response.etag or stored.etag,
                        http_last_modified=response.last_modified or stored.last_modified,
                        **_written(candidate),
                    )
                )
                continue
            if not response.content:
                collected.append(unreadable(ImageIssue.FETCH_FAILED))
                continue
            spent_bytes += len(response.content)
            collected.append(
                FetchedImage(
                    role=role,
                    ordinal=candidate.order,
                    host=candidate.host,
                    provenance=candidate.rule,
                    content=response.content,
                    locator=locator,
                    http_etag=response.etag,
                    http_last_modified=response.last_modified,
                    **_written(candidate),
                )
            )
        return self._recorder.record(collected)

    # ------------------------------------------------------------------ common

    def _registered(self, supplier_key: str) -> RegisteredCollection:
        try:
            return self._collections[supplier_key]
        except KeyError:
            raise NotFoundError(
                "COLLECT_SUPPLIER_UNKNOWN", f"no collection definition for {supplier_key!r}"
            ) from None


def _written(candidate: ImageCandidate) -> dict[str, Any]:
    """What a revision keeps of how the page wrote the reference: its form, never its text."""
    written = candidate.source_form
    if written is None:
        return {"source_form": None, "source_trimmed": None}
    form, trimmed = written
    return {"source_form": form, "source_trimmed": trimmed}


def _unfetched(
    candidate: ImageCandidate,
    role: ImageRole,
    *,
    locator: str | None,
    refusal: FetchTargetRefusal | None,
) -> Callable[[ImageIssue], UnfetchedImage]:
    """One reference that produced no bytes, whatever the reason turns out to be.

    It keeps what the reference resolved to: the canonical fetch target when the target check
    allowed it, or the target check's reason when it did not.
    """

    def reference(issue: ImageIssue) -> UnfetchedImage:
        return UnfetchedImage(
            role=role,
            ordinal=candidate.order,
            host=candidate.host,
            provenance=candidate.rule,
            issue=issue,
            locator=locator,
            target_refusal=refusal,
            **_written(candidate),
        )

    return reference


def pacing_key(collection: SupplierCollection, product_url: str) -> PacingKey:
    """What the same-product interval is measured on for this URL (ADR-0010 §4).

    The URL part is the normalized in-scope URL — never the raw operator string, whose spelling is
    not evidence of anything. The product part is filled in when the supplier's own URL form makes
    plain which product the URL points at: two accepted spellings of one product, and the same
    product after a rename, are then one product to the interval rather than three.

    A hint is never an identity. It can only make a collection wait; what a revision is recorded
    under still comes from the document the supplier served.
    """
    parts = urlsplit(product_url)
    return PacingKey(
        supplier_key=collection.supplier_key,
        url=f"https://{parts.hostname}{parts.path.rstrip('/')}",
        source_product_id=collection.url_product_hint(product_url),
    )


def url_policy_of(profile: CollectionProfile) -> UrlPolicy:
    """The supplier's own safe query keys, as the URL policy the revision store enforces."""
    return UrlPolicy({host: frozenset(keys) for host, keys in profile.safe_query_keys.items()})
