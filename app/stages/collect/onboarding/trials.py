"""「사이트 분석」: trial reads, offline re-analysis and observed-host confirmation (ADR-0036 §5).

**A trial read** (``submit`` → one ``onboarding.trial`` job):
- only for a ``RECON`` onboarded site (``DRAFT``, ``ACTIVE`` and ``DISABLED`` are refused), and
  only a URL the site's bound collection profile accepts as a product page on its confirmed
  storefront host, in the template's path form, with only the template's safe query keys: the
  same target check COLLECT applies (``check_target``);
- at most 5 distinct samples per site (§5 step 1), each named by its pacing key
  (``pacing_key``: the normalized in-scope URL), which is also the trial's ``sample_url_key``;
- paced on the template's defaults (OB-06): 60 s since the last read of the same sample and 10 s
  since the last trial read of the site, counted on the trial jobs themselves (a job is one read),
  and on COLLECT's own last read of that product. The submission refuses a read that would come too
  soon, and the job checks again before it reads, against the jobs that actually started;
- read **once**, through the policed collection gateway with the site's CONNECT session, exactly
  as COLLECT reads a product page, under a budget of one product read and nothing else: no image,
  no policy document, no retry (the job's policy allows one attempt);
- the page is sanitized (``capture.sanitize``) and kept as a local sample capture, and the report
  (``analysis.analyse``) is recorded through the onboarding store as one ``onboarding_trials``
  row. Nothing here calls COLLECT's ``_record_document``, appends a ``ProductFactsRevision`` or
  stores an image (OB-02).

**Re-analysis** (``reanalyse``) runs the current words over the stored captures of every sample
whose newest trial is not under the current words and template revision, and records a new trial
for each. It reads nothing from the supplier.

**Observed hosts** (D1, OB-01): a host the newest trials observed as an image or login host is
confirmed through the store's own ``confirm_host``, which requires that trial and applies the host
guard. A confirmed image host joins the collection's egress at once (the registry rebinds).
"""

import hashlib
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.jobs.models import TERMINAL_STATE_NAMES, Job
from app.capabilities.jobs.policy import RetryPolicy
from app.capabilities.jobs.records import JobRecord
from app.capabilities.jobs.registry import JobContext, JobDefinition
from app.capabilities.jobs.service import JobService
from app.platform.core.clock import Clock
from app.platform.core.errors import (
    AppError,
    ErrorClass,
    InputValidationError,
    PolicyBlockedError,
    RateLimitedError,
)
from app.stages.collect.collection import CollectionGateway, SessionProvider, pacing_key
from app.stages.collect.facts import FIELD_REGISTRY, FieldLevel, FieldStatus
from app.stages.collect.onboarding.analysis import TrialReport, analyse
from app.stages.collect.onboarding.capture import (
    CaptureIntegrityError,
    TrialCaptureStore,
    sanitize,
)
from app.stages.collect.onboarding.catalog import SupplierCatalog
from app.stages.collect.onboarding.contracts import (
    AnalysisView,
    ObservedHostView,
    ReanalysisView,
    StaleSampleView,
    TrialFieldView,
    TrialSubmitted,
    TrialView,
    UnreadRowView,
)
from app.stages.collect.onboarding.hosts import onboarding_host
from app.stages.collect.onboarding.site import HostRole, OnboardingRecord, OnboardingState
from app.stages.collect.onboarding.store import (
    CORE_FIELDS,
    ONBOARDING_NOT_RECON,
    READ_STATUSES,
    OnboardingConflictError,
    OnboardingStore,
    StoredTrial,
    TrialInput,
)
from app.stages.collect.runs import PacingKey
from integrations.suppliers.collection import ReadKind, SupplierCollection
from integrations.suppliers.platforms import PlatformAnalysis, PlatformTemplate
from integrations.suppliers.sites import TEMPLATES, BoundSite
from integrations.suppliers.transport.collection import (
    CollectionBudgetRefused,
    CollectionTargetRefused,
    check_target,
)
from integrations.suppliers.transport.session_payload import decode_session

logger = logging.getLogger("icbm.collect.onboarding")

ONBOARDING_TRIAL_JOB: Final = "onboarding.trial"
# A trial read is never retried: another attempt would be another real read of the sample.
TRIAL_POLICY: Final = RetryPolicy(max_attempts=1, base_delay_s=60.0, max_delay_s=60.0)
# ADR-0036 §5 steps 1–2: up to 5 sample product URLs per site, each read once. Every trial job
# ever queued counts, whatever it ended in: a read that failed was still a read of the supplier.
SAMPLE_LIMIT: Final = 5
# Job rows are never deleted, so the ledger of a site's trial reads is all its trial jobs.
_EVER: Final = datetime(1970, 1, 1, tzinfo=UTC)
# The template defaults the trial reads are paced on (OB-06), when a template names none.
SAME_SAMPLE_INTERVAL_S: Final = 60.0
TRIAL_INTERVAL_S: Final = 10.0

ONBOARDING_SITE_UNBOUND: Final = "ONBOARDING_SITE_UNBOUND"
ONBOARDING_SAMPLE_REFUSED: Final = "ONBOARDING_SAMPLE_REFUSED"
ONBOARDING_SAMPLES_FULL: Final = "ONBOARDING_SAMPLES_FULL"
ONBOARDING_SAMPLE_ALREADY_READ: Final = "ONBOARDING_SAMPLE_ALREADY_READ"
ONBOARDING_TRIAL_PENDING: Final = "ONBOARDING_TRIAL_PENDING"
ONBOARDING_TRIAL_TOO_SOON: Final = "ONBOARDING_TRIAL_TOO_SOON"
ONBOARDING_TRIAL_BUDGET: Final = "ONBOARDING_TRIAL_BUDGET"
ONBOARDING_SAMPLE_UNREAD: Final = "ONBOARDING_SAMPLE_UNREAD"
ONBOARDING_ANALYSIS_UNAVAILABLE: Final = "ONBOARDING_ANALYSIS_UNAVAILABLE"
ONBOARDING_HOST_ROLE_UNOBSERVED: Final = "ONBOARDING_HOST_ROLE_UNOBSERVED"

_CONFIRMABLE_ROLES: Final = frozenset({HostRole.IMAGE.value, HostRole.LOGIN.value})
_ROLE_ORDER: Final = {"image": 0, "login": 1, "asset": 2}


class TrialTooSoon(RateLimitedError):
    """A trial read would come sooner than the template's pacing allows; nothing was read."""

    def __init__(self, seconds_remaining: float, scope: str) -> None:
        super().__init__(
            ONBOARDING_TRIAL_TOO_SOON,
            f"the sample was read too recently; {seconds_remaining:.0f} s remain",
            details={"seconds_remaining": round(seconds_remaining, 1), "scope": scope},
        )
        self.seconds_remaining = seconds_remaining


class SampleUnread(AppError):
    """The read answered with something other than the product page (a redirect, a denial)."""

    error_class = ErrorClass.REVIEW_REQUIRED


class TrialBudget:
    """A trial's own request budget: exactly one product read, and nothing else (OB-02, OB-06).

    It reserves before any byte is sent, as COLLECT's run budget does, so an image request, a
    policy read or a second product read is refused without being sent."""

    def __init__(self) -> None:
        self.product_reads = 0

    def reserve(self, kind: ReadKind, subject: str) -> None:
        if kind is not ReadKind.PRODUCT_READ or self.product_reads >= 1:
            raise CollectionBudgetRefused(
                ONBOARDING_TRIAL_BUDGET, "a trial reads exactly one product page and nothing else"
            )
        self.product_reads += 1


@dataclass(frozen=True)
class _Site:
    record: OnboardingRecord
    bound: BoundSite
    template: PlatformTemplate
    analysis: PlatformAnalysis

    @property
    def collection(self) -> SupplierCollection:
        return self.bound.collection

    @property
    def words(self) -> Mapping[str, tuple[str, ...]]:
        """Each slot's words as the bound site reads them: the template's, then the site's."""
        overrides = self.bound.config.label_overrides
        return {
            slot: defaults + tuple(w for w in overrides.get(slot, ()) if w not in defaults)
            for slot, defaults in self.template.label_slots.items()
        }


def _digest(sample_key: str) -> str:
    return hashlib.sha256(sample_key.encode("utf-8")).hexdigest()[:16]


def target_ref(supplier_key: str, sample_key: str) -> str:
    """A trial job's target: the supplier and a digest of the sample's pacing key."""
    return f"{supplier_key}:{_digest(sample_key)}"


def last_trial_read(session: Session, key: PacingKey, now: datetime) -> datetime | None:
    """The latest moment a trial may have read this product (OB-06), never earlier than its read.

    A finished trial job counts from when it finished, which is after its request was sent and
    answered; a started job that has not finished counts as reading ``now``, so a delay between the
    job's start and its request (the session, the gateway) can never shorten the interval. COLLECT's
    same-product interval counts it (``CollectionRunStore`` ``other_reads``): an ordinary collection
    does not read a product a trial has just read. A job that never reached its read counts as
    well; the interval errs on the side of waiting."""
    moments: list[datetime] = []
    for state, started, finished in session.execute(
        select(Job.state, Job.started_at, Job.finished_at).where(
            Job.job_type == ONBOARDING_TRIAL_JOB,
            Job.target_ref == target_ref(key.supplier_key, key.url),
            Job.started_at.is_not(None),
        )
    ):
        if state not in TERMINAL_STATE_NAMES:
            moments.append(now)
        else:
            moments.append(finished or started)
    return max(
        (m if m.tzinfo is not None else m.replace(tzinfo=UTC) for m in moments), default=None
    )


class SiteAnalysisService:
    def __init__(
        self,
        *,
        store: OnboardingStore,
        catalog: SupplierCatalog,
        jobs: JobService,
        gateway: CollectionGateway,
        sessions: SessionProvider,
        captures: TrialCaptureStore,
        clock: Clock,
        collect_remaining: Callable[[PacingKey, float], float] | None = None,
        templates: Mapping[str, PlatformTemplate] = TEMPLATES,
    ) -> None:
        """``collect_remaining`` is COLLECT's own same-product interval check (a read-only call of
        the run store)."""
        self._store = store
        self._catalog = catalog
        self._jobs = jobs
        self._gateway = gateway
        self._sessions = sessions
        self._captures = captures
        self._clock = clock
        self._collect_remaining = collect_remaining
        self._templates = templates
        # A check and the job it admits are one step: two submits of a sample cannot both pass.
        self._admit = threading.Lock()

    def job_definition(self) -> JobDefinition:
        return JobDefinition(
            job_type=ONBOARDING_TRIAL_JOB,
            handler=self._run_job,
            description="Read one sample product page of an onboarded site for site analysis.",
            # Another attempt would be another real read of the sample: never replayed.
            idempotent=False,
            retry_policy=TRIAL_POLICY,
        )

    # ------------------------------------------------------------------ trial reads

    def submit(self, key: str, sample_url: str, *, actor: str) -> TrialSubmitted:
        """Accept one sample URL as one trial job (202), or refuse it before anything is read.

        Each sample is read once, ever: a sample a trial job was ever queued for is refused, and a
        site takes at most ``SAMPLE_LIMIT`` samples, failed reads included. A words change is
        re-analysed from the stored captures, never by reading a sample again."""
        site = self._site(key)
        url = self._sample(site, sample_url)
        pacing = pacing_key(site.collection, url)
        target = target_ref(key, pacing.url)
        with self._admit:
            jobs = self._ledger(key)
            if any(
                job.target_ref == target and job.state not in TERMINAL_STATE_NAMES for job in jobs
            ):
                raise OnboardingConflictError(
                    ONBOARDING_TRIAL_PENDING, "that sample is already waiting to be read"
                )
            self._refuse_reread(key, target, jobs)
            now = self._clock.now()
            self._paced(site, pacing, now, self._recent(site, now), started_only=False)
            job = self._jobs.enqueue(
                ONBOARDING_TRIAL_JOB,
                payload={"supplier_key": key, "sample_url": url, "actor": actor},
                target_ref=target,
            )
        logger.info(
            "collect.onboarding_trial_submitted",
            extra={"supplier": key, "job_id": job.job_id},
        )
        return TrialSubmitted(job_id=job.job_id, sample_url_key=pacing.url)

    def _run_job(self, context: JobContext) -> None:
        key = str(context.payload.get("supplier_key", ""))
        url = str(context.payload.get("sample_url", ""))
        site = self._site(key)
        url = self._sample(site, url)
        pacing = pacing_key(site.collection, url)
        # The same ledger again, without this job: a job queued around the submit check is
        # refused here, before its read.
        earlier = [j for j in self._ledger(key) if j.job_id != context.job_id]
        self._refuse_reread(key, target_ref(key, pacing.url), earlier)
        now = self._clock.now()
        recent = [j for j in self._recent(site, now) if j.job_id != context.job_id]
        self._paced(site, pacing, now, recent, started_only=True)
        budget = TrialBudget()
        session = self._sessions.collection_session(key)
        document = self._gateway.read_document(
            site.collection.profile,
            url,
            kind=ReadKind.PRODUCT_READ,
            budget=budget,
            session=session,
        )
        if document.status != 200:
            # A redirect (an expired session sends the reader to the login page) or another
            # answer is not the product page: nothing is kept and no trial is recorded.
            raise SampleUnread(
                ONBOARDING_SAMPLE_UNREAD,
                f"the sample answered HTTP {document.status}, not the product page",
                details={"http_status": document.status},
            )
        body = sanitize(
            document.body,
            regions=site.analysis.capture_regions(site.bound.config),
            base_url=url,
            safe_query_keys=site.collection.profile.safe_query_keys,
            secrets=_cookie_values(session),
        )
        sha256 = self._captures.put(body)
        actor = str(context.payload.get("actor") or f"job:{context.job_id}")[:64]
        trial = self._record(site, pacing.url, sha256, body, actor=actor)
        logger.info(
            "collect.onboarding_trial_recorded",
            extra={"supplier": key, "trial_id": trial.trial_id, "job_id": context.job_id},
        )

    # ------------------------------------------------------------------ re-analysis

    def reanalyse(self, key: str, *, actor: str) -> ReanalysisView:
        """Re-run the current words over the stored captures; no supplier read (§5 step 6)."""
        site = self._site(key)
        newest = _newest(self._store.trials(key))
        analysed = current = missing = 0
        for trial in newest.values():
            if self._current(site, trial):
                current += 1
                continue
            try:
                body = self._captures.read(trial.capture_sha256)
            except CaptureIntegrityError:
                missing += 1
                continue
            self._record(site, trial.sample_url_key, trial.capture_sha256, body, actor=actor)
            analysed += 1
        return ReanalysisView(
            analysed=analysed, current=current, missing=missing, analysis=self.view(key)
        )

    # ------------------------------------------------------------------ hosts

    def confirm_host(
        self, key: str, host: str, role: str, *, trial_id: str, actor: str
    ) -> OnboardingRecord:
        """Confirm an image or login host a trial of this supplier observed in that role."""
        trial = next((t for t in self._store.trials(key) if t.trial_id == trial_id), None)
        name = onboarding_host(host)
        observed = trial is not None and any(
            entry.get("host") == name and entry.get("role") == role
            for entry in trial.observed_hosts
        )
        if role not in _CONFIRMABLE_ROLES or not observed:
            raise InputValidationError(
                ONBOARDING_HOST_ROLE_UNOBSERVED,
                "an image or login host is confirmed in the role a trial observed it in",
            )
        return self._store.confirm_host(key, name, role, observed_trial_id=trial_id, actor=actor)

    def disable_host(self, key: str, host: str, role: str, *, actor: str) -> OnboardingRecord:
        return self._store.disable_host(key, host, role, actor=actor)

    # ------------------------------------------------------------------ the report

    def view(self, key: str) -> AnalysisView:
        record = self._store.current(key)
        template = self._store.template(record.template)
        trials = self._store.trials(key)
        newest = _newest(trials)
        site = self._bound(record)
        samples: list[TrialView] = []
        stale: list[StaleSampleView] = []
        for trial in newest.values():
            if trial.words_seq == record.words_seq and trial.template_revision == template.revision:
                samples.append(self._trial_view(site, trial))
            else:
                stale.append(
                    StaleSampleView(
                        sample_url=trial.sample_url_key,
                        words_seq=trial.words_seq,
                        template_revision=trial.template_revision,
                    )
                )
        proof = self._store.activation(key)
        return AnalysisView(
            supplier_key=key,
            state=record.state.value,
            template=record.template,
            template_revision=template.revision,
            words_seq=record.words_seq,
            sample_limit=SAMPLE_LIMIT,
            samples_taken=len(self._taken(key, self._ledger(key))),
            samples=samples,
            stale=stale,
            observed_hosts=self._hosts(
                record, [t for t in newest.values() if t.words_seq == record.words_seq]
            ),
            activation=proof.details(),
            verifiable_samples=sum(1 for sample in samples if sample.verifiable),
        )

    # ------------------------------------------------------------------ internals

    def _site(self, key: str) -> _Site:
        record = self._store.current(key)
        if record.state is not OnboardingState.RECON:
            raise OnboardingConflictError(
                ONBOARDING_NOT_RECON,
                "site analysis reads a supplier that is in analysis (RECON) only",
                details={"state": record.state.value},
            )
        site = self._bound(record)
        if site is None:
            raise OnboardingConflictError(
                ONBOARDING_SITE_UNBOUND, "the supplier is not bound; its problems are listed"
            )
        return site

    def _bound(self, record: OnboardingRecord) -> _Site | None:
        entry = self._catalog.onboarded(record.supplier_key)
        template = self._templates.get(record.template)
        if entry is None or entry.record is None or entry.bound is None or template is None:
            return None
        if template.analysis is None:
            raise PolicyBlockedError(
                ONBOARDING_ANALYSIS_UNAVAILABLE, "the template has no site analysis rules"
            )
        # The bound definition is the one the registry holds now: its words are the record's.
        return _Site(entry.record, entry.bound, template, template.analysis)

    @staticmethod
    def _sample(site: _Site, sample_url: str) -> str:
        """The URL as the operator gave it, when the site's own profile accepts it as a product
        page on its confirmed storefront host; refused otherwise, before anything is read."""
        url = sample_url.strip() if isinstance(sample_url, str) else ""
        try:
            check_target(site.collection.profile, url, ReadKind.PRODUCT_READ)
        except CollectionTargetRefused as refused:
            raise InputValidationError(
                ONBOARDING_SAMPLE_REFUSED,
                "a sample is a product page of this supplier's storefront, in its template's form",
                details={"reason": refused.reason.value},
            ) from None
        if site.record.storefront_host not in site.record.hosts_of(HostRole.STOREFRONT):
            raise InputValidationError(
                ONBOARDING_SAMPLE_REFUSED,
                "the storefront host is not confirmed",
                details={"reason": "STOREFRONT_DISABLED"},
            )
        return url

    def _ledger(self, key: str) -> list[JobRecord]:
        """Every trial job this site ever had: each one is (or was) one read of a sample."""
        return self._jobs.recent_for_targets(
            ONBOARDING_TRIAL_JOB, target_prefix=f"{key}:", since=_EVER
        )

    def _taken(self, key: str, jobs: list[JobRecord]) -> set[str | None]:
        """The samples this site has used: every trial job's target and every trial's sample."""
        return {job.target_ref for job in jobs} | {
            target_ref(key, trial.sample_url_key) for trial in self._store.trials(key)
        }

    def _refuse_reread(self, key: str, target: str, jobs: list[JobRecord]) -> None:
        taken = self._taken(key, jobs)
        if target in taken:
            raise OnboardingConflictError(
                ONBOARDING_SAMPLE_ALREADY_READ,
                "each sample is read once; 다시 분석 reads its stored capture again",
            )
        if len(taken) >= SAMPLE_LIMIT:
            raise InputValidationError(
                ONBOARDING_SAMPLES_FULL,
                f"site analysis reads at most {SAMPLE_LIMIT} samples, failed reads included",
                details={"sample_limit": SAMPLE_LIMIT},
            )

    def _recent(self, site: _Site, now: datetime) -> list[JobRecord]:
        since = now - timedelta(seconds=max(self._intervals(site)))
        return self._jobs.recent_for_targets(
            ONBOARDING_TRIAL_JOB, target_prefix=f"{site.record.supplier_key}:", since=since
        )

    @staticmethod
    def _intervals(site: _Site) -> tuple[float, float]:
        limits = site.template.default_limits
        return (
            float(limits.get("same_product_interval_s", SAME_SAMPLE_INTERVAL_S)),
            float(limits.get("min_queue_interval_s", TRIAL_INTERVAL_S)),
        )

    def _paced(
        self,
        site: _Site,
        pacing: PacingKey,
        now: datetime,
        recent: list[JobRecord],
        *,
        started_only: bool,
    ) -> None:
        same, between = self._intervals(site)
        target = target_ref(site.record.supplier_key, pacing.url)
        for job in recent:
            moment = job.started_at if started_only else (job.started_at or job.created_at)
            if moment is None:
                continue
            elapsed = (now - moment).total_seconds()
            if job.target_ref == target and elapsed < same:
                raise TrialTooSoon(same - elapsed, "sample")
            if elapsed < between:
                raise TrialTooSoon(between - elapsed, "site")
        if self._collect_remaining is not None:
            remaining = self._collect_remaining(pacing, same)
            if remaining > 0:
                raise TrialTooSoon(remaining, "sample")

    def _record(
        self, site: _Site, sample_key: str, sha256: str, body: str, *, actor: str
    ) -> StoredTrial:
        report = self._report(site, sample_key, body)
        return self._store.record_trial(
            site.record.supplier_key,
            TrialInput(
                sample_url_key=sample_key,
                template_revision=site.template.revision,
                words_seq=site.record.words_seq,
                capture_sha256=sha256,
                facts_fingerprint=report.fingerprint,
                field_statuses=report.statuses,
                unread_rows=tuple(row.as_json() for row in report.unread),
                observed_hosts=tuple(host.as_json() for host in report.hosts),
                structural=report.structural,
            ),
            actor=actor,
        )

    @staticmethod
    def _report(site: _Site, sample_key: str, body: str) -> TrialReport:
        return analyse(
            body=body,
            sample_url=sample_key,
            storefront_host=site.record.storefront_host,
            collection=site.collection,
            analysis=site.analysis,
            words=site.words,
        )

    def _current(self, site: _Site, trial: StoredTrial) -> bool:
        return (
            trial.words_seq == site.record.words_seq
            and trial.template_revision == site.template.revision
        )

    def _trial_view(self, site: _Site | None, trial: StoredTrial) -> TrialView:
        summary: dict[str, Any] | None = None
        notes: Mapping[str, str] = {}
        if site is not None:
            try:
                report = self._report(
                    site, trial.sample_url_key, self._captures.read(trial.capture_sha256)
                )
            except CaptureIntegrityError:
                report = None
            # The values are shown only when the capture still reproduces exactly what the trial
            # recorded: what the operator verifies is what the fingerprint names.
            if report is not None and report.fingerprint == trial.facts_fingerprint:
                summary = dict(report.summary)
                notes = report.notes
        statuses = trial.field_statuses
        fields = [
            TrialFieldView(
                field=name,
                core=spec.level is FieldLevel.CORE,
                status=statuses.get(name, FieldStatus.REVIEW_REQUIRED.value),
                note=notes.get(name),
            )
            for name, spec in FIELD_REGISTRY.items()
        ]
        core_read = all(statuses.get(f) in READ_STATUSES[f] for f in CORE_FIELDS)
        return TrialView(
            trial_id=trial.trial_id,
            sample_url=trial.sample_url_key,
            created_at=trial.created_at,
            words_seq=trial.words_seq,
            template_revision=trial.template_revision,
            facts_fingerprint=trial.facts_fingerprint,
            fields=fields,
            summary=summary,
            unread_rows=[
                UnreadRowView(
                    kind=str(row.get("kind", "row")),
                    label=str(row.get("label", "")),
                    value=str(row.get("value", "")),
                    candidate=_text_or_none(row.get("candidate")),
                    rule=_text_or_none(row.get("rule")),
                )
                for row in trial.unread_rows
            ],
            structural=list(trial.structural),
            verifiable=core_read and not trial.structural and summary is not None,
        )

    @staticmethod
    def _hosts(record: OnboardingRecord, trials: list[StoredTrial]) -> list[ObservedHostView]:
        seen: dict[tuple[str, str], tuple[int, str]] = {}
        for trial in sorted(trials, key=lambda t: (t.created_at, t.trial_id)):
            for entry in trial.observed_hosts:
                host, role = str(entry.get("host", "")), str(entry.get("role", ""))
                count = entry.get("count", 0)
                total = seen.get((host, role), (0, ""))[0] + (
                    count if isinstance(count, int) else 0
                )
                seen[(host, role)] = (total, trial.trial_id)
        views: list[ObservedHostView] = []
        for (host, role), (count, trial_id) in sorted(
            seen.items(), key=lambda i: (_ROLE_ORDER.get(i[0][1], 9), i[0][0])
        ):
            refusal: str | None = None
            try:
                onboarding_host(host)
            except InputValidationError as refused:
                refusal = str((refused.details or {}).get("reason", "refused"))
            confirmed = role in _CONFIRMABLE_ROLES and host in record.hosts_of(HostRole(role))
            views.append(
                ObservedHostView(
                    host=host,
                    role=role,
                    count=count,
                    trial_id=trial_id,
                    confirmed=confirmed,
                    confirmable=role in _CONFIRMABLE_ROLES and refusal is None and not confirmed,
                    refusal=refusal,
                )
            )
        return views


def _cookie_values(session: bytes) -> list[str]:
    """The session's own cookie values, in memory only, so a capture never keeps one the page
    echoed. The payload is the one this read already holds; nothing of it is kept or returned
    beyond the sanitizer's call, and an unreadable payload redacts nothing more (the gateway
    already refused to read with it)."""
    try:
        cookies, _ = decode_session(session)
    except Exception:
        return []
    return [cookie.get("value", "") for cookie in cookies if cookie.get("value")]


def _newest(trials: Iterable[StoredTrial]) -> dict[str, StoredTrial]:
    """The newest trial of each sample, in the order the samples were first read."""
    newest: dict[str, StoredTrial] = {}
    for trial in sorted(trials, key=lambda t: (t.created_at, t.words_seq, t.trial_id)):
        newest[trial.sample_url_key] = trial
    return newest


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["ONBOARDING_TRIAL_JOB", "SiteAnalysisService", "TrialBudget"]
