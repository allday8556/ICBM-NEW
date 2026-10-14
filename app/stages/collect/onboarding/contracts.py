"""Read shapes of 「+ 공급처 추가」 (ADR-0036 §4, slice O2).

Every shape carries names, states, statuses, hashes and marker names only. A credential never
appears in one (OB-05): the login is saved through CONNECT's own credential route and the view
reports only whether one is stored. The robots lines are the policy document the operator is
shown; they are never stored or audited.
"""

from datetime import datetime

from pydantic import BaseModel


class TemplateEvidenceView(BaseModel):
    platform: str
    home_markers: list[str]
    login_path: str
    login_status: int | None
    login_markers: list[str]
    matched: bool


class RobotsView(BaseModel):
    lines: list[str]
    disallows_all: bool
    disallows_products: bool
    crawl_delay: str | None


class PolicyDocumentView(BaseModel):
    document: str
    path: str
    # The public address the operator may open in their own browser.
    url: str
    status: int | None
    failure: str | None
    content_sha256: str | None
    truncated: bool
    robots: RobotsView | None
    warnings: list[str]


class DetectionView(BaseModel):
    """One detection pass, held by the server for ``expires_at`` so 「저장」 can name it."""

    detection_id: str
    origin: str
    host: str
    # ``cafe24``, ``godomall`` or ``unsupported``.
    detected: str
    reason: str | None
    home_status: int | None
    # Where the home page redirected, when it did and the target is an acceptable origin.
    suggested_origin: str | None
    # The template the operator chose between the matches of an AMBIGUOUS detection, if any.
    chosen: str | None
    # The template whose policy documents were read: what 「저장」 confirms.
    policy_template: str | None
    evidence: list[TemplateEvidenceView]
    policy: list[PolicyDocumentView]
    templates: list[str]
    # What 「저장」 may confirm: the detected template, or the templates an AMBIGUOUS detection
    # matched. Empty: the site stops with "a template is needed" (ADR-0036 §4 step 2).
    choices: list[str]
    reads: int
    policy_reads: int
    expires_at: datetime


class OnboardedHostView(BaseModel):
    host: str
    role: str


class StoredPolicyReadView(BaseModel):
    read_id: str
    document: str
    path: str
    http_status: int
    content_sha256: str | None
    read_at: datetime
    acknowledged_by: str
    acknowledged_at: datetime


class ConnectionView(BaseModel):
    state: str
    capability_status: str
    credentials_stored: bool
    last_verified_at: datetime | None
    last_error_code: str | None


class ReconReadiness(BaseModel):
    """ADR-0036 §4 steps 2–5: what ``DRAFT → RECON`` needs, each condition on its own."""

    platform_confirmed: bool
    policy_acknowledged: bool
    credentials_stored: bool
    connection_verified: bool

    @property
    def ready(self) -> bool:
        return (
            self.platform_confirmed
            and self.policy_acknowledged
            and self.credentials_stored
            and self.connection_verified
        )


class OnboardingView(BaseModel):
    supplier_key: str
    display_name: str
    origin: str
    template: str
    state: str
    state_seq: int
    created_at: datetime
    hosts: list[OnboardedHostView]
    policy_reads: list[StoredPolicyReadView]
    # CONNECT's view of the supplier while it is bound; ``None`` once it is not (DISABLED).
    connection: ConnectionView | None
    recon: ReconReadiness
    problems: list[str]


# ---------------------------------------------------------------- 「사이트 분석」 (ADR-0036 §5, O3)


class TrialSubmitted(BaseModel):
    """A trial read accepted as one durable job; its result is a trial row when the job ends."""

    job_id: str
    sample_url_key: str


class TrialFieldView(BaseModel):
    field: str
    core: bool
    # CONFIRMED (읽음), ABSENT (없음) or REVIEW_REQUIRED (못 읽음).
    status: str
    # Why a field that is not read was held, as the template's own bounded evidence.
    note: str | None


class UnreadRowView(BaseModel):
    kind: str
    label: str
    value: str
    # The slot a rule proposes (D4: never AI), or none.
    candidate: str | None
    rule: str | None


class ObservedHostView(BaseModel):
    host: str
    role: str
    count: int
    # The newest trial that observed it under the current words: what a confirmation names.
    trial_id: str
    confirmed: bool
    # Whether a confirmation could pass: an image or login host that the host guard accepts.
    confirmable: bool
    refusal: str | None


class TrialView(BaseModel):
    trial_id: str
    sample_url: str
    created_at: datetime
    words_seq: int
    template_revision: str
    facts_fingerprint: str
    fields: list[TrialFieldView]
    # The values the operator verifies (name, prices, options, stock, product number, images);
    # ``None`` when the stored capture no longer reproduces what the trial showed.
    summary: dict[str, object] | None
    unread_rows: list[UnreadRowView]
    structural: list[str]
    # Every core field read and no structural problem: what O5 may ask the operator to verify.
    verifiable: bool
    # The operator's verdict on exactly these facts (``CORRECT`` or ``WRONG``; a ``WRONG`` stands
    # over any ``CORRECT``), or ``None`` before one (§7, slice O5).
    verdict: str | None = None
    # The operator's reason when the sample was retired from the analysis (amendment
    # 6160586049): a retired sample neither proves nor blocks, and is shown with its warning.
    retired_reason: str | None = None


class StaleSampleView(BaseModel):
    """A sample whose newest trial ran under older words or another template revision."""

    sample_url: str
    words_seq: int
    template_revision: str


class AnalysisView(BaseModel):
    supplier_key: str
    state: str
    template: str
    template_revision: str
    words_seq: int
    sample_limit: int
    # Samples this site has used, failed reads included: each is read once (§5 step 2).
    samples_taken: int
    samples: list[TrialView]
    stale: list[StaleSampleView]
    observed_hosts: list[ObservedHostView]
    # ``ActivationProof.details()``: the ACTIVE gate's own counts (§7) and the template revision
    # they are judged under.
    activation: dict[str, int | str]
    # Samples whose newest trial under the current words is verifiable: "샘플 n/3 확인 가능".
    verifiable_samples: int
    # 「사용 시작」 is enabled: the site is RECON and the ACTIVE gate is proven (§7).
    can_activate: bool = False


class ReanalysisView(BaseModel):
    analysed: int
    current: int
    missing: int
    analysis: AnalysisView


# ---------------------------------------------------------------- 「수집 규칙」 (§6, slice O4)


class WordsSlotView(BaseModel):
    slot: str
    # ``row`` names an information row by its label, ``phrase`` a sales-channel phrase, ``mark``
    # anything else the template matches by a word (a sold-out mark, a purchase control, a title
    # suffix).
    kind: str
    # The template's own words for the slot (read-only) and the site's words appended to them.
    defaults: list[str]
    site: list[str]


class WordsView(BaseModel):
    supplier_key: str
    state: str
    template: str
    # The words revision a save must name as ``expected_seq`` (``0`` before the first).
    words_seq: int
    slots: list[WordsSlotView]
    words_per_slot: int
    word_limit: int


class FieldChangeView(BaseModel):
    field: str
    now: str
    proposed: str


class WordsPreviewSampleView(BaseModel):
    sample_url: str
    trial_id: str
    # The capture reads differently from its page (``CAPTURE_DIVERGES``): no words make it prove.
    diverged: bool
    # The fields whose status the proposed words change.
    changes: list[FieldChangeView]
    # Unread rows and phrases the proposed words read, and those they no longer read.
    newly_read: list[str]
    newly_unread: list[str]
    unread_now: int
    unread_proposed: int
    structural_now: list[str]
    structural_proposed: list[str]


class WordsPreviewView(BaseModel):
    words_seq: int
    samples: list[WordsPreviewSampleView]
    # Samples whose stored capture is missing or no longer matches its checksum.
    missing: int
