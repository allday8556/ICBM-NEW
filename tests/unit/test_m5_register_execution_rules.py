"""M5 PR-E pure rules: the send-request codec, the execution policy and the retry matrix.

Nothing here touches a database, a job or a provider: these are the decisions the execution
owner makes before and after it talks to anything (kickoff §4, §5, §9, §10).
"""

from dataclasses import replace

import pytest

from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import AUTO_RETRYABLE, ErrorClass
from app.products.image_model import ImageAssetKind
from app.register.execution import (
    CREATE_ENDPOINT_GROUP,
    CREATE_JOB_TYPE,
    CREATE_POLICY,
    EXECUTION_POLICY_VERSION,
    SEND_REQUEST_VERSION,
    ExecutionPolicy,
    ExecutionRefused,
    decode_send_request,
    encode_send_request,
    frozen_unit_identity,
    target_ref,
)
from app.register.model import ResolutionEvidence, ResolvedBy
from app.register.policy import DuplicateKeyKind, Provenance
from app.register.preparation import (
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
    DuplicateEvidence,
    DuplicateMatch,
    DuplicateVerdict,
    FieldValue,
    ListingValues,
    PreflightRequest,
    PreparedAsset,
    UnitRequest,
)
from app.register.sanitize import PayloadSanitationError
from app.register.store import AttemptRecord

IDENTITY = "icbm-0123456789abcdef0123456789abcdef"
FINGERPRINT = "c" * 64


def _request(**overrides: object) -> PreflightRequest:
    values: dict[str, object] = {
        "unit": UnitRequest("draft-1", 3, ("item-1",)),
        "category": CategorySelection(
            "cat-1", "map-1", "tax-1", CategoryConfirmation.OPERATOR_CONFIRMED
        ),
        "listing": ListingValues(
            name=FieldValue("테스트 상품"),
            tags=frozenset({"tag-b", "tag-a"}),
            attributes={"brand": FieldValue("KM", Provenance.SOURCE_FACT)},
            notices={"origin": FieldValue(detail_page_reference=True)},
            options={"item-1": {"색상": "빨강"}},
        ),
        "detail": DetailComposition("detail-1", "본문", ("BODY",)),
        "duplicate_evidence": DuplicateEvidence(
            marketplace_key="smartstore",
            marketplace_account_id="mpa-1",
            listing_identity=IDENTITY,
            lookup_contract_version="lookup-1",
            evidence_digest="e" * 64,
            verdict=DuplicateVerdict.NO_MATCH,
            keys_checked=frozenset({DuplicateKeyKind.SELLER_CODE}),
            matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, "provider-listing-1"),),
        ),
    }
    values.update(overrides)
    return PreflightRequest(**values)  # type: ignore[arg-type]


def _asset(reference: str = "provider-asset-1") -> PreparedAsset:
    return PreparedAsset(
        asset_kind=ImageAssetKind.SOURCE_ASSET,
        sha256="1" * 64,
        derivation_id=None,
        asset_profile="asset-profile-1",
        candidate_fingerprint=FINGERPRINT,
        provider_asset_ref=reference,
    )


def _encoded(**overrides: object) -> dict[str, object]:
    return encode_send_request(
        "intent-1",
        _request(**overrides),
        (_asset(),),
        listing_identity=IDENTITY,
        identity_generation=0,
    )


# ---------------------------------------------------------------- the send request codec


def test_the_codec_round_trips_every_preflight_input() -> None:
    request, prepared = _request(), (_asset(),)
    payload = encode_send_request(
        "intent-1", request, prepared, listing_identity=IDENTITY, identity_generation=2
    )
    assert payload["version"] == SEND_REQUEST_VERSION
    assert frozen_unit_identity(payload) == (IDENTITY, 2)
    decoded, assets = decode_send_request(payload)
    # The job carries the operator's inputs exactly: a retry re-evaluates the same request.
    assert decoded == request
    assert assets == prepared


def test_a_payload_of_another_version_is_refused() -> None:
    payload = dict(_encoded())
    payload["version"] = "registration-send-request/v2"
    with pytest.raises(ExecutionRefused) as refused:
        decode_send_request(payload)
    assert refused.value.code == "REGISTER_SEND_REQUEST_VERSION"
    assert refused.value.error_class is ErrorClass.VALIDATION


@pytest.mark.parametrize(
    "listing",
    [
        ListingValues(name=FieldValue("Bearer abcdefghijklmnop")),
        ListingValues(
            name=FieldValue("x"),
            attributes={"api_key": FieldValue("secret-value")},
        ),
        ListingValues(
            name=FieldValue("x"),
            attributes={"brand": FieldValue("see https://supplier.example/a.jpg")},
        ),
    ],
)
def test_forbidden_material_never_reaches_a_durable_job_payload(listing: ListingValues) -> None:
    # §15: the job row is durable, so the codec refuses what the payload may never carry.
    with pytest.raises(PayloadSanitationError):
        _encoded(listing=listing)


@pytest.mark.parametrize(
    "reference",
    ["provider-asset-1", "https://provider.example/a.jpg", "https://shop-phinf.example/a/b.jpg"],
)
def test_a_safe_provider_reference_passes_the_send_codec(reference: str) -> None:
    # PR-C/PR-D allow an opaque reference or a plain https one; the Snapshot builder already
    # judges them by that rule, and the send codec applies exactly the same typed boundary.
    payload = encode_send_request(
        "intent-1",
        _request(),
        (_asset(reference),),
        listing_identity=IDENTITY,
        identity_generation=0,
    )
    assert payload["prepared_assets"][0]["provider_asset_ref"] == reference  # type: ignore[index]


@pytest.mark.parametrize(
    "reference",
    [
        "https://cdn.example/a.jpg?sig=abc",
        "https://cdn.example/a.jpg#access_token=abc",
        "https://user:secret@cdn.example/a.jpg",
        "http://cdn.example/a.jpg",
        "http:cdn.example/a.jpg",
        "ftp:cdn.example/a.jpg",
        "//cdn.example/a.jpg",
        "Bearer abcdefghijklmnop",
    ],
)
def test_an_unsafe_provider_reference_never_passes_the_send_codec(reference: str) -> None:
    with pytest.raises(PayloadSanitationError):
        encode_send_request(
            "intent-1",
            _request(),
            (_asset(reference),),
            listing_identity=IDENTITY,
            identity_generation=0,
        )


def test_duplicate_evidence_identities_are_judged_by_their_own_rules() -> None:
    # The evidence carries a provider listing reference and a digest, so it is judged the way
    # PR-C judges it — never by the business-value rule that refuses every URL.
    safe = _request(
        duplicate_evidence=replace(
            _request().duplicate_evidence,  # type: ignore[arg-type]
            matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, "https://listing.example/p/1"),),
        )
    )
    assert encode_send_request(
        "intent-1", safe, (_asset(),), listing_identity=IDENTITY, identity_generation=0
    )["duplicate_evidence"]["matches"][0]["provider_listing_ref"] == (  # type: ignore[index]
        "https://listing.example/p/1"
    )
    for bad in ("https://listing.example/p/1?sig=abc", "http:listing.example/p/1"):
        unsafe = _request(
            duplicate_evidence=replace(
                _request().duplicate_evidence,  # type: ignore[arg-type]
                matches=(DuplicateMatch(DuplicateKeyKind.SELLER_CODE, bad),),
            )
        )
        with pytest.raises(PayloadSanitationError):
            encode_send_request(
                "intent-1", unsafe, (_asset(),), listing_identity=IDENTITY, identity_generation=0
            )


def test_the_job_names_its_intent_in_the_target_ref() -> None:
    # The owner and the job find each other through this, with no second link.
    assert target_ref("intent-1") == "intent:intent-1"
    assert CREATE_JOB_TYPE == "register.create"


# ---------------------------------------------------------------- the retry matrix (§9, §10)


def _attempt(outcome: RemoteOutcome | None, error_class: ErrorClass | None) -> AttemptRecord:
    return AttemptRecord(
        attempt_id="a-1",
        intent_id="intent-1",
        attempt_no=1,
        request_payload_hash="f" * 64,
        finished=outcome is not None,
        remote_outcome=outcome,
        resolved_outcome=None,
        resolved_by=None,
        resolution_evidence_kind=None,
        error_class=error_class,
    )


@pytest.mark.parametrize("error_class", sorted(AUTO_RETRYABLE, key=lambda c: c.value))
def test_only_a_proven_not_applied_outcome_may_reach_an_automatic_retry(
    error_class: ErrorClass,
) -> None:
    # The job policy retries these classes, so the handler may only expose them once the
    # attempt is durably NOT_APPLIED_PROVEN; UNKNOWN is reported as REVIEW_REQUIRED instead.
    assert CREATE_POLICY.allows_retry(error_class, 1)
    assert not CREATE_POLICY.allows_retry(ErrorClass.REVIEW_REQUIRED, 1)
    assert not CREATE_POLICY.allows_retry(error_class, CREATE_POLICY.max_attempts)


def test_the_retry_backoff_is_deterministic_and_bounded() -> None:
    assert CREATE_POLICY.schedule_s() == [60.0, 120.0]
    assert CREATE_POLICY.max_delay_s == 900.0
    assert CREATE_POLICY.max_attempts == 3


@pytest.mark.parametrize(
    ("outcome", "error_class", "retryable"),
    [
        (RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.TRANSIENT, True),
        (RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.RATE_LIMITED, True),
        (RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.AUTH, False),
        (RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.VALIDATION, False),
        (RemoteOutcome.UNKNOWN, ErrorClass.TRANSIENT, False),
        (RemoteOutcome.UNKNOWN, ErrorClass.RATE_LIMITED, False),
        (RemoteOutcome.APPLIED_PROVEN, ErrorClass.TRANSIENT, False),
    ],
)
def test_the_matrix_of_error_class_and_remote_outcome(
    outcome: RemoteOutcome, error_class: ErrorClass, retryable: bool
) -> None:
    # The two axes are independent: a CREATE is replayed only where both allow it.
    replayable = outcome is RemoteOutcome.NOT_APPLIED_PROVEN and error_class in AUTO_RETRYABLE
    assert replayable is retryable


# ---------------------------------------------------------------- the failure budget (§10)


def test_the_policy_is_versioned_application_policy_scoped_to_one_endpoint_group() -> None:
    policy = ExecutionPolicy()
    assert (policy.version, policy.endpoint_group) == (
        EXECUTION_POLICY_VERSION,
        CREATE_ENDPOINT_GROUP,
    )
    assert policy.pause_classes == frozenset({ErrorClass.AUTH, ErrorClass.POLICY_BLOCKED})


def test_the_budget_counts_consecutive_proven_failures_only() -> None:
    policy = ExecutionPolicy(max_proven_failures=2)
    failure = _attempt(RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.TRANSIENT)
    assert policy.budget([failure]).sends_allowed
    exhausted = policy.budget([failure, failure])
    assert (exhausted.exhausted, exhausted.consecutive_failures) == (True, 2)
    # A success in the scope clears what came before it.
    applied = _attempt(RemoteOutcome.APPLIED_PROVEN, None)
    assert policy.budget([applied, failure, failure]).sends_allowed
    # An attempt still open proves nothing either way and is not counted.
    assert policy.budget([_attempt(None, None), failure]).consecutive_failures == 1


def test_an_auth_or_policy_cause_pauses_the_scope_rather_than_counting() -> None:
    policy = ExecutionPolicy()
    for cause in (ErrorClass.AUTH, ErrorClass.POLICY_BLOCKED):
        state = policy.budget([_attempt(RemoteOutcome.NOT_APPLIED_PROVEN, cause)])
        assert state.paused_by is cause and not state.sends_allowed
        assert state.canonical()["paused_by"] == cause.value


def test_an_unknown_outcome_is_not_budgeted_and_neither_pauses_nor_frees_the_scope() -> None:
    # An UNKNOWN is reconciled (§10), not budgeted: it proves no failure, so it spends nothing,
    # and it clears nothing either — its own Intent already blocks its conflict scope.
    policy = ExecutionPolicy(max_proven_failures=2)
    unknown = _attempt(RemoteOutcome.UNKNOWN, ErrorClass.TRANSIENT)
    assert policy.budget([unknown]).consecutive_failures == 0
    assert policy.budget([unknown]).paused_by is None
    failure = _attempt(RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.TRANSIENT)
    # Sitting between two proven failures, it neither adds to them nor resets them.
    assert policy.budget([failure, unknown, failure]).exhausted


# ---------------------------------------------------------------- resolution vocabulary (§10)


def test_an_operator_assertion_is_not_a_resolution_evidence_kind() -> None:
    # B3: resolved_by names who recorded it; the evidence itself is always machine or provider.
    assert {e.value for e in ResolutionEvidence} == {
        "PROVIDER_READ_BACK",
        "PROVIDER_LOOKUP",
        "TRANSMISSION_PRECLUDED",
        "REVIEWED_MACHINE_PROOF",
    }
    assert ResolvedBy.USER in set(ResolvedBy)


def test_a_prepared_asset_reference_travels_only_when_it_is_safe() -> None:
    # The codec inherits PR-C's sanitizer, so an unsafe reference cannot ride in the job payload.
    with pytest.raises(PayloadSanitationError):
        encode_send_request(
            "intent-1",
            _request(),
            (_asset("https://cdn.example/a.jpg?sig=abc"),),
            listing_identity=IDENTITY,
            identity_generation=0,
        )
    safe = _encoded()
    assert safe["prepared_assets"][0]["provider_asset_ref"] == "provider-asset-1"  # type: ignore[index]


def test_the_codec_is_deterministic_for_the_same_inputs() -> None:
    first, second = _encoded(), _encoded()
    assert first == second
    # Option and attribute order never changes the payload.
    shuffled = _request(
        listing=replace(
            _request().listing,
            tags=frozenset({"tag-a", "tag-b"}),
        )
    )
    assert (
        encode_send_request(
            "intent-1", shuffled, (_asset(),), listing_identity=IDENTITY, identity_generation=0
        )
        == first
    )
