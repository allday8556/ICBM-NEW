"""The extension ingest owner: one capture, accepted or refused, then compared (ADR-0019 E1).

The order of one ingest is fixed (E1 specification ``5907009512`` §3). The caller has already
proven the sender — loopback, ``X-ICBM-Client``, the pinned origin and the pairing — and this
owner does the rest::

    5  ceilings: bytes, elements, image references, one capture at a time, the minimum interval
    6  transport evidence: every ``DocumentView`` field is browser-observed and valid
    7  target: ``check_target(profile, url, ReadKind.PRODUCT_READ)`` under the extension envelope
    8  policy: the capture names the revision and digest the server recomputes now
    -- accepted: one write unit enqueues the job and opens the canonical run (``PENDING``,
       ``EXTENSION``, the policy revision and digest); the HTML waits in the in-process buffer --
    9  the server's own structural check and final scan of exactly what arrived
    10 the same ``DocumentView``
    11 the supplier's canonical extractor, in memory
    12 the Adaptive dry run: a comparison, or ``NO_BUNDLE``
    13 the run settles ``NO_REVISION``; any failure from step 9 on — a dry run that could
       not compare included — settles it ``FAILED``

Steps 5–8 never open a run. Steps 9–13 always settle the run that was opened. Nothing is appended:
no ``ProductFactsRevision``, no source asset, no Adaptive row. ``RECORDED`` is unreachable here.

The job is not replayable, because its capture is deliberately not durable (ruling ``5906712259``
N-1): it is registered non-idempotent with one attempt, a missing buffer ends it with a fixed code,
and an interrupted attempt is never run again.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.capabilities.jobs.policy import RetryPolicy
from app.capabilities.jobs.registry import JobContext, JobDefinition, TerminalJob
from app.capabilities.jobs.service import JobService
from app.platform.core.clock import Clock
from app.platform.core.correlation import get_correlation_id, new_correlation_id
from app.platform.core.errors import (
    AppError,
    InputValidationError,
    NotFoundError,
    PolicyBlockedError,
    RateLimitedError,
)
from app.platform.db.database import Database
from app.stages.collect.collection import RegisteredCollection, url_policy_of
from app.stages.collect.extension.buffer import BufferedCapture, CaptureBuffer
from app.stages.collect.extension.capture import (
    CAPTURED_CONTENT_TYPE,
    CAPTURED_STATUS,
    CaptureEnvelope,
    TransportEvidence,
    measure,
    policy_violations,
)
from app.stages.collect.extension.policy import (
    MAX_HTML_BYTES,
    MAX_IMAGE_REFS,
    MAX_NODES,
    BrowserCapturePolicy,
    CapturePolicyRefused,
    CapturePolicySource,
)
from app.stages.collect.facts import CollectedFacts
from app.stages.collect.models import CollectionOutcome, TransportKind
from app.stages.collect.runs import CollectionRunRecord, CollectionRunStore, RunProvenance
from app.stages.collect.shadow import (
    COMPARE_FAILED,
    NO_BUNDLE,
    DryRunInput,
    DryRunResult,
    DryRunStep,
)
from integrations.suppliers.base import SupplierTransport
from integrations.suppliers.collection import (
    CollectionProfile,
    DocumentView,
    ReadKind,
    SourceIdentity,
)
from integrations.suppliers.transport.collection import CollectionTargetRefused, check_target

logger = logging.getLogger("icbm.collect.extension")

EXTENSION_CAPTURE_JOB = "collect.extension_capture"
# One attempt, never retried: the capture body is not durable, so there is nothing to retry with.
EXTENSION_CAPTURE_POLICY = RetryPolicy(max_attempts=1, base_delay_s=1.0)
# Ruling 5906290729 B-4: one capture per pairing at a time, and at least this long between two
# accepted captures. These are E1 single-click caps, not list-queue values.
MINIMUM_INGEST_INTERVAL_S = 5.0

# The ``detail`` of an E1 run settled NO_REVISION: captured and compared in memory, nothing
# written. A code on the existing outcome axis, never a new outcome.
EXTENSION_COMPARE_ONLY = "EXTENSION_COMPARE_ONLY"
# The ``detail`` of a run whose capture was gone when its job ran (a restart). Never retried.
EXTENSION_CAPTURE_BUFFER_MISSING = "EXTENSION_CAPTURE_BUFFER_MISSING"
EXTENSION_CAPTURE_POLICY_CHANGED = "EXTENSION_CAPTURE_POLICY_CHANGED"
EXTENSION_CAPTURE_POLICY_VIOLATION = "EXTENSION_CAPTURE_POLICY_VIOLATION"
EXTENSION_FINAL_SCAN_REFUSED = "EXTENSION_FINAL_SCAN_REFUSED"
# The ``detail`` of a run whose processing raised something no one classified. The exception's
# own text never leaves the process: it may quote the captured page.
EXTENSION_PROCESSING_FAILED = "EXTENSION_PROCESSING_FAILED"
# The ``detail`` of a run whose Adaptive dry run could not evaluate or compare its bundle. A
# failure of step 12 is a failure of the run; only ``NO_BUNDLE`` and a comparison are answers.
EXTENSION_ADAPTIVE_COMPARE_FAILED = "EXTENSION_ADAPTIVE_COMPARE_FAILED"
UNFINISHED_RUN = "JOB_ENDED_WITHOUT_RESULT"

# The server's own final gate of a capture: the findings, as kinds and boundaries only. Empty
# means the capture owner's sanitizer had nothing private or secret to take out of it and its
# final scan found no residual — only then may the capture go on as it arrived. The container
# hands in the capture owner's sanitizer and final scan, so this package never depends on the
# Adaptive packages.
FinalScan = Callable[[str], Sequence[str]]


class ExtensionCeilingExceeded(PolicyBlockedError):
    """A capture crosses a ceiling. The whole ingest is refused; nothing is truncated."""


class ExtensionIngestBusy(RateLimitedError):
    """Another capture of this pairing is still being processed, or one was accepted too
    recently."""


class ExtensionCaptureFailed(AppError):
    """An accepted capture could not be processed. The run is settled ``FAILED`` with the code."""

    error_class = PolicyBlockedError.error_class


@dataclass(frozen=True)
class AcceptedCapture:
    """What the extension is handed the moment a capture is accepted: an identity to read back,
    never a result."""

    collection_run_id: str
    job_id: str
    correlation_id: str


@dataclass(frozen=True)
class CaptureReport:
    """What one processed capture produced, in memory. It is handed to the report sink and logged
    as counts; it is never persisted by this owner.

    ``fields`` holds each canonical field's status, and ``image_roles`` how many references the
    supplier's role rules put under each role. No captured value is part of it.
    """

    collection_run_id: str
    supplier_key: str
    outcome: CollectionOutcome
    detail: str
    transport_kind: TransportKind
    capture_policy_revision: str
    capture_policy_digest: str
    evidence: Mapping[str, Any]
    source_product_id: str | None
    identity_reason: str | None
    fields: Mapping[str, str] = field(default_factory=dict)
    image_roles: Mapping[str, int] = field(default_factory=dict)
    adaptive: DryRunResult = field(default_factory=lambda: DryRunResult(NO_BUNDLE))


ReportSink = Callable[[CaptureReport], None]


class ExtensionCaptureService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        jobs: JobService,
        runs: CollectionRunStore,
        policies: CapturePolicySource,
        buffer: CaptureBuffer,
        final_scan: FinalScan,
        worker_in_process: bool,
        collections: Sequence[RegisteredCollection] = (),
        dry_run: DryRunStep | None = None,
        report_sink: ReportSink | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._jobs = jobs
        self._runs = runs
        self._policies = policies
        self._buffer = buffer
        self._final_scan = final_scan
        # The in-process buffer is a handoff between the request and the worker of one process
        # (ADR-0002 Option A). Where the worker is not in this process, nothing is accepted.
        self._worker_in_process = worker_in_process
        self._collections = {registered.supplier_key: registered for registered in collections}
        self._dry_run = dry_run
        self._report_sink = report_sink

    # ------------------------------------------------------------------ the policy read

    def policy(self, supplier_key: str) -> BrowserCapturePolicy:
        """The reviewed capture policy of one registered supplier, read from the repository now.
        The extension cuts a page only under exactly these bytes."""
        self._registered(supplier_key)
        return self._policies.load(supplier_key)

    # ------------------------------------------------------------------ the ingest (steps 5–8)

    def ingest(self, envelope: CaptureEnvelope) -> AcceptedCapture:
        """Accept one authenticated capture and open its canonical run, or refuse it whole."""
        if not self._worker_in_process:
            raise PolicyBlockedError(
                "EXTENSION_WORKER_NOT_IN_PROCESS",
                "the extension capture handoff needs the in-process job worker (ADR-0002)",
            )
        registered = self._registered(envelope.supplier_key)
        supplier_key = registered.supplier_key
        # 5. Ceilings, before anything else is looked at and before any run exists.
        measured = measure(envelope.html)
        _ceiling("EXTENSION_CEILING_HTML_BYTES", measured.html_bytes, MAX_HTML_BYTES)
        _ceiling("EXTENSION_CEILING_NODES", measured.nodes, MAX_NODES)
        _ceiling("EXTENSION_CEILING_IMAGE_REFS", measured.image_refs, MAX_IMAGE_REFS)
        self._require_free()
        # 6. Transport evidence: observed, valid, and never filled in.
        evidence = envelope.transport
        _require_evidence(evidence)
        # 7. The target, judged by the one fetch judge under the extension envelope.
        envelope_profile = extension_profile(registered.collection.profile)
        try:
            check_target(envelope_profile, evidence.url, ReadKind.PRODUCT_READ)
        except CollectionTargetRefused as refused:
            raise InputValidationError("EXTENSION_TARGET_REFUSED", refused.message) from None
        # 8. The policy: recomputed from the repository now, and the one the capture names.
        policy = self._policies.load(supplier_key)
        if policy.host != envelope_profile.storefront_host:
            raise CapturePolicyRefused(
                "CAPTURE_POLICY_HOST_MISMATCH",
                "the capture policy is not for the supplier's reviewed storefront host",
            )
        if (envelope.policy.revision, envelope.policy.digest) != (policy.revision, policy.digest):
            raise CapturePolicyRefused(
                "CAPTURE_POLICY_MISMATCH",
                "the capture was not cut with the current reviewed policy; fetch it again",
            )
        _ceiling("EXTENSION_CEILING_HTML_BYTES", measured.html_bytes, policy.bounds.max_html_bytes)
        _ceiling("EXTENSION_CEILING_NODES", measured.nodes, policy.bounds.max_nodes)
        _ceiling("EXTENSION_CEILING_IMAGE_REFS", measured.image_refs, policy.bounds.max_image_refs)
        # Accepted. The job, the run and the buffered capture appear together or not at all.
        run_id: str | None = None
        try:
            with self._db.write() as session:
                self._require_free(session=session)
                job = self._jobs.enqueue(
                    EXTENSION_CAPTURE_JOB,
                    # Identifiers and provenance only. The capture itself is never durable.
                    payload={
                        "supplier_key": supplier_key,
                        "source_url": evidence.url,
                        "capture_policy_revision": policy.revision,
                        "capture_policy_digest": policy.digest,
                    },
                    target_ref=supplier_key,
                    session=session,
                )
                run_id = self._runs.open(
                    session,
                    job_id=job.job_id,
                    correlation_id=job.correlation_id,
                    supplier_key=supplier_key,
                    source_url=evidence.url,
                    provenance=RunProvenance(
                        TransportKind.EXTENSION, policy.revision, policy.digest
                    ),
                )
                self._buffer.put(run_id, BufferedCapture(envelope.html, evidence))
        except BaseException:
            if run_id is not None:
                self._buffer.discard(run_id)
            raise
        self._jobs.notify_worker()
        logger.info(
            "collect.extension_accepted",
            extra={
                "collection_run_id": run_id,
                "job_id": job.job_id,
                "supplier": supplier_key,
                "capture_policy_revision": policy.revision,
                "html_bytes": measured.html_bytes,
                "nodes": measured.nodes,
                "image_refs": measured.image_refs,
            },
        )
        return AcceptedCapture(run_id, job.job_id, job.correlation_id)

    def _require_free(self, *, session: Session | None = None) -> None:
        """One capture at a time, and the minimum interval between two accepted ones. Both are
        read from the canonical runs, so they hold across a restart. There is one pairing, so
        every extension run counts."""
        pending, last = self._runs.transport_activity(TransportKind.EXTENSION, session=session)
        if pending:
            raise ExtensionIngestBusy(
                "EXTENSION_CEILING_CONCURRENCY", "a capture is still being processed"
            )
        if last is not None:
            now = self._clock.now()
            if last.tzinfo is None:
                last = last.replace(tzinfo=now.tzinfo)
            if now - last < timedelta(seconds=MINIMUM_INGEST_INTERVAL_S):
                raise ExtensionIngestBusy(
                    "EXTENSION_CEILING_INTERVAL", "a capture was accepted too recently"
                )

    # ------------------------------------------------------------------ the job (steps 9–13)

    def job_definition(self) -> JobDefinition:
        return JobDefinition(
            job_type=EXTENSION_CAPTURE_JOB,
            handler=self._run_job,
            description="Compare one extension capture in memory; append nothing (E1).",
            # The capture body lives in this process's memory only: an interrupted attempt cannot
            # be run again, and the job system must not try (ruling 5906712259 N-1).
            idempotent=False,
            retry_policy=EXTENSION_CAPTURE_POLICY,
            on_terminal=self._settle_unfinished_run,
            unsettled_owned_jobs=self._runs.unsettled_job_ids,
        )

    def _settle_unfinished_run(self, terminal: TerminalJob) -> None:
        """Close a run whose job ended without the run reaching an outcome: an interrupted
        attempt, or an exception no one classified. It is FAILED, says exactly that, and is never
        run again. A run that already has an answer is left as it is."""
        record = self._runs.for_job(terminal.job_id)
        if record is None or record.outcome is not CollectionOutcome.PENDING:
            return
        self._buffer.discard(record.collection_run_id)
        self._runs.failed(
            record.collection_run_id,
            detail=f"{UNFINISHED_RUN}:{terminal.error_code or terminal.state}",
        )
        logger.error(
            "collect.extension_run_unfinished",
            extra={
                "collection_run_id": record.collection_run_id,
                "job_id": terminal.job_id,
                "job_state": terminal.state,
                "error_code": terminal.error_code,
            },
        )

    def _run_job(self, context: JobContext) -> None:
        record = self._runs.for_job(context.job_id)
        if record is None:
            raise NotFoundError("COLLECT_RUN_UNKNOWN", "this job has no collection run")
        if record.outcome is not CollectionOutcome.PENDING:
            return  # the run already has its answer; nothing rewrites it
        run_id = record.collection_run_id
        capture = self._buffer.take(run_id)
        try:
            if capture is None:
                # The process that accepted the capture is gone, and the capture with it.
                raise ExtensionCaptureFailed(
                    EXTENSION_CAPTURE_BUFFER_MISSING,
                    "the capture was not in this process's memory when its job ran",
                )
            try:
                report = self._compare(record, capture)
            except AppError:
                raise
            except Exception as unclassified:
                # The extractor holds captured page content. Whatever it raised is reduced to its
                # type before it can reach the job's stored error text or a log.
                raise ExtensionCaptureFailed(
                    EXTENSION_PROCESSING_FAILED,
                    "the capture could not be processed",
                    details={"failure": type(unclassified).__name__},
                ) from None
        except AppError as error:
            self._runs.failed(run_id, detail=error.code)
            logger.warning(
                "collect.extension_failed",
                extra={"collection_run_id": run_id, "error_code": error.code},
            )
            raise
        self._runs.no_revision(run_id, reason=report.detail)
        logger.info(
            "collect.extension_compared",
            extra={
                "collection_run_id": run_id,
                "supplier": report.supplier_key,
                "detail": report.detail,
                "identity_resolved": report.source_product_id is not None,
                "fields": dict(report.fields),
                "image_roles": dict(report.image_roles),
                "adaptive": report.adaptive.state,
            },
        )
        if self._report_sink is not None:
            try:
                self._report_sink(report)
            except Exception:
                logger.exception("collect.extension_report_failed", extra={"run": run_id})

    def _compare(self, record: CollectionRunRecord, capture: BufferedCapture) -> CaptureReport:
        run_id, supplier_key = record.collection_run_id, record.supplier_key
        registered = self._registered(supplier_key)
        collection = registered.collection
        provenance = record.provenance
        assert provenance is not None and provenance.capture_policy_digest is not None
        assert provenance.capture_policy_revision is not None
        # 9. The server's own check of exactly what arrived, under the policy the run names.
        policy = self._policies.load(supplier_key)
        if (policy.revision, policy.digest) != (
            provenance.capture_policy_revision,
            provenance.capture_policy_digest,
        ):
            raise ExtensionCaptureFailed(
                EXTENSION_CAPTURE_POLICY_CHANGED,
                "the capture policy changed after the capture was accepted",
            )
        violations = policy_violations(capture.html, policy)
        if violations:
            raise ExtensionCaptureFailed(
                EXTENSION_CAPTURE_POLICY_VIOLATION,
                "the capture holds what its policy does not allow",
                details={"violations": list(violations)[:20]},
            )
        findings = self._final_scan(capture.html)
        if findings:
            raise ExtensionCaptureFailed(
                EXTENSION_FINAL_SCAN_REFUSED,
                "the server's sanitizer and final scan found secret or private material",
                details={"findings": len(findings)},
            )
        # 10. The same DocumentView, from what the browser observed and nothing else.
        evidence = capture.evidence
        url = evidence.url
        document = DocumentView(
            kind=ReadKind.PRODUCT_READ,
            status=evidence.response_status,
            path=urlsplit(url).path or "/",
            # The contract's value for a 200: the direct transport also reports no redirect
            # target unless the response is a 3xx. A redirected navigation was refused at ingest.
            location=None,
            content_type=evidence.content_type,
            body=capture.html,
        )
        # 11. The supplier's canonical extractor, in memory. Nothing is appended.
        identity = collection.identity(document, url)
        candidates = tuple(collection.roles.classify(document.body, url))
        roles: dict[str, int] = {}
        for candidate in candidates:
            roles[candidate.role.value] = roles.get(candidate.role.value, 0) + 1
        collected: CollectedFacts | None = None
        if isinstance(identity, SourceIdentity):
            self._runs.note_identity(run_id, source_product_id=identity.source_product_id)
            collected = CollectedFacts(
                supplier_key=supplier_key,
                source_product_id=identity.source_product_id,
                source_url=url,
                captured_at=self._clock.now(),
                extractor_revision=registered.extractor_revision,
                extractor_fingerprint=registered.extractor_fingerprint,
                collection_run_id=run_id,
                correlation_id=get_correlation_id() or new_correlation_id(),
                fields=collection.fields(document),
                # A compare-only run fetches no image and records no source asset.
                images=(),
            )
        # 12. The Adaptive dry run: a comparison, or NO_BUNDLE. It writes nothing.
        adaptive = self._adaptive(
            DryRunInput(
                collection_run_id=run_id,
                supplier_key=supplier_key,
                source_url=url,
                document=document,
                identity=identity,
                collected=collected,
                url_policy=url_policy_of(collection.profile),
                candidates=candidates,
            )
        )
        if adaptive.state == COMPARE_FAILED:
            # E1 specification §3: any failure from step 9 on settles the run FAILED.
            raise ExtensionCaptureFailed(
                EXTENSION_ADAPTIVE_COMPARE_FAILED,
                "the Adaptive dry run could not evaluate or compare its bundle",
                details=dict(adaptive.summary or {}),
            )
        return CaptureReport(
            collection_run_id=run_id,
            supplier_key=supplier_key,
            outcome=CollectionOutcome.NO_REVISION,
            # An unresolved identity is the same answer it is on the direct path: the parser's
            # own reason. A resolved one says what E1 did with it.
            detail=EXTENSION_COMPARE_ONLY
            if isinstance(identity, SourceIdentity)
            else (identity.reason or "the identity is unresolved"),
            transport_kind=TransportKind.EXTENSION,
            capture_policy_revision=policy.revision,
            capture_policy_digest=policy.digest,
            evidence={
                "response_status": evidence.response_status,
                "redirect_count": evidence.redirect_count,
                "content_type": evidence.content_type,
                "character_set": evidence.character_set,
            },
            source_product_id=identity.source_product_id
            if isinstance(identity, SourceIdentity)
            else None,
            identity_reason=None if isinstance(identity, SourceIdentity) else identity.reason,
            fields={}
            if collected is None
            else {key: fact.status.value for key, fact in collected.fields.items()},
            image_roles=dict(sorted(roles.items())),
            adaptive=adaptive,
        )

    def _adaptive(self, dry_run: DryRunInput) -> DryRunResult:
        if self._dry_run is None:
            return DryRunResult(NO_BUNDLE)
        try:
            return self._dry_run(dry_run)
        except Exception as failure:
            logger.exception(
                "collect.extension_dry_run_escaped",
                extra={"collection_run_id": dry_run.collection_run_id},
            )
            return DryRunResult(COMPARE_FAILED, None, {"failure": type(failure).__name__})

    # ------------------------------------------------------------------ common

    def _registered(self, supplier_key: str) -> RegisteredCollection:
        try:
            return self._collections[supplier_key]
        except KeyError:
            raise NotFoundError(
                "COLLECT_SUPPLIER_UNKNOWN", f"no collection definition for {supplier_key!r}"
            ) from None


def extension_profile(profile: CollectionProfile) -> CollectionProfile:
    """The supplier's own access envelope under the extension transport (ADR-0019 §10): the same
    host, path and query rules, and a transport no gateway will ever send a request for."""
    return replace(profile, transport=SupplierTransport.EXTENSION)


def _ceiling(code: str, value: int, bound: int) -> None:
    if value > bound:
        raise ExtensionCeilingExceeded(
            code, "the capture crosses a ceiling", details={"bound": bound}
        )


def _require_evidence(evidence: TransportEvidence) -> None:
    """Every ``DocumentView`` transport field is observed and valid, or the capture is refused.

    Nothing is defaulted (ADR-0019 §2): an absent status arrives as 0 or not at all, a redirect
    chain is refused rather than hidden, and the content type is the one the browser reported.
    """
    if evidence.response_status != CAPTURED_STATUS:
        raise InputValidationError(
            "EXTENSION_EVIDENCE_STATUS", "the browser did not observe an HTTP 200 for this page"
        )
    if evidence.redirect_count != 0:
        raise InputValidationError(
            "EXTENSION_EVIDENCE_REDIRECT",
            "the page was reached through a redirect; reload its final URL and capture again",
        )
    if evidence.url != evidence.navigation_name:
        raise InputValidationError(
            "EXTENSION_EVIDENCE_URL",
            "the page's location is not the URL its navigation loaded",
        )
    if evidence.content_type != CAPTURED_CONTENT_TYPE:
        raise InputValidationError(
            "EXTENSION_EVIDENCE_CONTENT_TYPE", "the captured document is not text/html"
        )
    if not evidence.character_set.strip():
        raise InputValidationError(
            "EXTENSION_EVIDENCE_CHARSET", "the browser reported no character set"
        )
