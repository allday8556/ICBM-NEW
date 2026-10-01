"""The extension pairing and its replay protection (ADR-0019 §3; ruling 5906290729 B-2;
E1 specification 5907009512 §2.2)."""

import base64
import json
import threading
import time
from datetime import datetime
from typing import Any

import pytest

from app.platform.core.errors import AppError
from app.platform.core.secrets import MemorySecretStore
from app.stages.collect.extension.nonces import (
    NONCE_TTL_S,
    NonceCache,
    NonceCacheFull,
    NonceReplayed,
)
from app.stages.collect.extension.pairing import (
    EMPTY_BODY_SHA256,
    EXTENSION_ID_HEADER,
    GENERATION_HEADER,
    NONCE_HEADER,
    PAIRING_ID_HEADER,
    PAIRING_SECRET_KEY,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    TIMESTAMP_WINDOW_S,
    ExtensionPairing,
    PairingConflict,
    PairingRecord,
    PairingRefused,
)
from tests.support.extension_support import CAPTURES, EXTENSION_ID, ICBM_ORIGIN, ORIGIN, signed
from tests.support.jobs_support import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def pairing(secrets: MemorySecretStore, clock: FakeClock) -> ExtensionPairing:
    return ExtensionPairing(secrets, clock, NonceCache(clock))


def _verify(
    pairing: ExtensionPairing, record: PairingRecord, clock: FakeClock, **signing: Any
) -> Any:
    origin = signing.pop("request_origin", ORIGIN)
    headers = signed(record, method="POST", path=CAPTURES, now=clock.now(), **signing)
    return pairing.verify(method="POST", path=CAPTURES, origin=origin, headers=headers)


def _refusal(
    pairing: ExtensionPairing, record: PairingRecord, clock: FakeClock, **signing: Any
) -> str:
    with pytest.raises(AppError) as refused:
        _verify(pairing, record, clock, **signing)
    return refused.value.code


# ---------------------------------------------------------------- the operator commands


def test_a_pairing_lives_in_the_keyring_and_nowhere_else(
    pairing: ExtensionPairing, secrets: MemorySecretStore
) -> None:
    assert pairing.current() is None
    issued = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN)
    stored = secrets.get(PAIRING_SECRET_KEY)
    assert stored is not None and json.loads(stored)["extension_id"] == EXTENSION_ID
    assert pairing.current() == issued.record
    assert (issued.record.generation, issued.record.extension_id) == (1, EXTENSION_ID)
    # What the operator sees of a pairing never includes its secret.
    assert "secret" not in issued.record.describe()
    assert issued.record.secret not in json.dumps(issued.record.describe())


def test_the_pairing_code_carries_what_the_extension_needs(pairing: ExtensionPairing) -> None:
    issued = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN)
    padded = issued.code + "=" * (-len(issued.code) % 4)
    document = json.loads(base64.urlsafe_b64decode(padded))
    assert document == {
        "v": 1,
        "origin": ICBM_ORIGIN,
        "pairing_id": issued.record.pairing_id,
        "generation": 1,
        "secret": issued.record.secret,
    }
    assert len(base64.urlsafe_b64decode(issued.record.secret + "=")) == 32


def test_pairing_is_explicit_and_one_at_a_time(pairing: ExtensionPairing) -> None:
    for identity in ("", "abc", "A" * 32, "abcdefghijklmnopabcdefghijklmnoq", "a" * 33):
        with pytest.raises(AppError) as refused:
            pairing.pair(identity, origin=ICBM_ORIGIN)
        assert refused.value.code == "EXTENSION_ID_INVALID", identity
    pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN)
    with pytest.raises(PairingConflict) as conflict:
        pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN)
    assert conflict.value.code == "EXTENSION_ALREADY_PAIRED"


def test_rotation_invalidates_the_prior_generation(
    pairing: ExtensionPairing, clock: FakeClock
) -> None:
    with pytest.raises(PairingConflict):
        pairing.rotate(origin=ICBM_ORIGIN)
    first = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    assert _verify(pairing, first, clock).generation == 1
    second = pairing.rotate(origin=ICBM_ORIGIN).record
    assert (second.pairing_id, second.generation) == (first.pairing_id, 2)
    assert second.secret != first.secret
    assert _refusal(pairing, first, clock) == "EXTENSION_PAIRING_MISMATCH"
    assert _verify(pairing, second, clock).generation == 2


def test_revocation_removes_the_pairing(pairing: ExtensionPairing, clock: FakeClock) -> None:
    assert pairing.revoke() is False
    record = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    assert pairing.revoke() is True
    assert pairing.current() is None
    assert _refusal(pairing, record, clock) == "EXTENSION_NOT_PAIRED"


# ---------------------------------------------------------------- request verification


def test_a_signed_request_is_verified(pairing: ExtensionPairing, clock: FakeClock) -> None:
    record = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    sender = _verify(pairing, record, clock, body=b'{"a":1}')
    assert (sender.extension_id, sender.pairing_id, sender.origin) == (
        EXTENSION_ID,
        record.pairing_id,
        ORIGIN,
    )
    assert sender.body_sha256 != EMPTY_BODY_SHA256
    # A request with no Origin header is still bound to the pinned identity by its signature.
    assert _verify(pairing, record, clock, request_origin=None).extension_id == EXTENSION_ID


@pytest.mark.parametrize(
    ("signing", "code"),
    [
        ({"overrides": {EXTENSION_ID_HEADER: "p" * 32}}, "EXTENSION_IDENTITY_MISMATCH"),
        ({"request_origin": "chrome-extension://" + "p" * 32}, "EXTENSION_ORIGIN_MISMATCH"),
        ({"request_origin": "https://kmretail.co.kr"}, "EXTENSION_ORIGIN_MISMATCH"),
        ({"request_origin": "null"}, "EXTENSION_ORIGIN_MISMATCH"),
        ({"overrides": {PAIRING_ID_HEADER: "someone-else"}}, "EXTENSION_PAIRING_MISMATCH"),
        ({"overrides": {GENERATION_HEADER: "2"}}, "EXTENSION_PAIRING_MISMATCH"),
        ({"overrides": {TIMESTAMP_HEADER: "soon"}}, "EXTENSION_SIGNATURE_MALFORMED"),
        ({"overrides": {NONCE_HEADER: "short"}}, "EXTENSION_SIGNATURE_MALFORMED"),
        ({"overrides": {SIGNATURE_HEADER: "zz"}}, "EXTENSION_SIGNATURE_MALFORMED"),
        ({"overrides": {SIGNATURE_HEADER: "0" * 64}}, "EXTENSION_SIGNATURE_INVALID"),
        # Signed for another moment, or another nonce, than the one presented.
        ({"overrides": {TIMESTAMP_HEADER: "1"}}, "EXTENSION_SIGNATURE_INVALID"),
        ({"overrides": {NONCE_HEADER: "n" * 32}}, "EXTENSION_SIGNATURE_INVALID"),
    ],
)
def test_each_failed_check_refuses_with_its_own_code(
    pairing: ExtensionPairing, clock: FakeClock, signing: dict[str, Any], code: str
) -> None:
    record = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    assert _refusal(pairing, record, clock, **signing) == code


def test_the_signature_binds_the_method_the_path_and_the_body(
    pairing: ExtensionPairing, clock: FakeClock
) -> None:
    record = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    headers = signed(record, method="POST", path=CAPTURES, body=b"one", now=clock.now())
    for method, path in (("GET", CAPTURES), ("POST", CAPTURES + "/x"), ("POST", "/api/v1/other")):
        with pytest.raises(PairingRefused) as refused:
            pairing.verify(method=method, path=path, origin=ORIGIN, headers=headers)
        assert refused.value.code == "EXTENSION_SIGNATURE_INVALID"
    # The body is bound by its digest: the verified sender names the digest that was signed.
    sender = pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=headers)
    assert (
        sender.body_sha256
        == signed(record, method="POST", path=CAPTURES, body=b"one")["X-ICBM-Body-SHA256"]
    )


def test_a_stale_timestamp_is_refused_either_way(
    pairing: ExtensionPairing, clock: FakeClock
) -> None:
    record = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    now = clock.now().timestamp()
    for offset in (-(TIMESTAMP_WINDOW_S + 1), TIMESTAMP_WINDOW_S + 1):
        headers = signed(record, method="POST", path=CAPTURES, now=now + offset)
        with pytest.raises(PairingRefused) as refused:
            pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=headers)
        assert refused.value.code == "EXTENSION_TIMESTAMP_STALE"
    edge = signed(record, method="POST", path=CAPTURES, now=now - TIMESTAMP_WINDOW_S)
    assert pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=edge)


def test_a_replayed_request_is_refused(pairing: ExtensionPairing, clock: FakeClock) -> None:
    record = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    headers = signed(record, method="POST", path=CAPTURES, now=clock.now(), nonce="n" * 32)
    assert pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=headers)
    with pytest.raises(NonceReplayed) as replayed:
        pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=headers)
    assert replayed.value.code == "EXTENSION_NONCE_REPLAYED"


def test_a_refused_request_consumes_no_nonce(pairing: ExtensionPairing, clock: FakeClock) -> None:
    record = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record
    forged = signed(
        record,
        method="POST",
        path=CAPTURES,
        now=clock.now(),
        nonce="n" * 32,
        overrides={SIGNATURE_HEADER: "0" * 64},
    )
    with pytest.raises(PairingRefused):
        pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=forged)
    # The genuine request with the same nonce is still accepted: a forgery cannot burn it.
    genuine = signed(record, method="POST", path=CAPTURES, now=clock.now(), nonce="n" * 32)
    assert pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=genuine)


# ---------------------------------------------------------------- the nonce cache


def test_the_nonce_cache_outlives_the_timestamp_window() -> None:
    # A nonce may be forgotten only after a request carrying it could no longer be accepted.
    assert NONCE_TTL_S >= 2 * TIMESTAMP_WINDOW_S


def test_a_nonce_is_forgotten_only_after_its_time_to_live(clock: FakeClock) -> None:
    cache = NonceCache(clock, ttl_s=10, capacity=4)
    cache.consume("p", "a")
    clock.advance(9)
    with pytest.raises(NonceReplayed):
        cache.consume("p", "a")
    clock.advance(2)
    cache.consume("p", "a")
    assert cache.size("p") == 1


def test_a_full_cache_refuses_and_never_evicts_a_live_nonce(clock: FakeClock) -> None:
    cache = NonceCache(clock, ttl_s=10, capacity=2)
    cache.consume("p", "a")
    cache.consume("p", "b")
    with pytest.raises(NonceCacheFull) as full:
        cache.consume("p", "c")
    assert full.value.code == "EXTENSION_NONCE_CACHE_FULL"
    # Both live nonces are still refused as replays: nothing was evicted to make room.
    for known in ("a", "b"):
        with pytest.raises(NonceReplayed):
            cache.consume("p", known)
    # Another pairing has its own bound.
    cache.consume("q", "a")


def test_the_cache_is_memory_only_and_a_restart_empties_it(clock: FakeClock) -> None:
    first = NonceCache(clock)
    first.consume("p", "a")
    # A new process holds a new cache: nothing was written anywhere it could be read back from.
    assert vars(first).keys() == {"_clock", "_ttl", "_capacity", "_seen", "_lock"}
    restarted = NonceCache(clock)
    restarted.consume("p", "a")
    with pytest.raises(ValueError):
        NonceCache(clock, ttl_s=0)
    with pytest.raises(ValueError):
        NonceCache(clock, capacity=0)


class _SlowSeen(dict[str, datetime]):
    """The nonces one pairing has used, slowed down exactly inside the race window: every caller
    that finds the nonce absent waits before it goes on to insert it. Without the lock, every
    thread that arrives in that wait is accepted."""

    def __contains__(self, key: object) -> bool:
        found = super().__contains__(key)
        if not found:
            time.sleep(0.02)
        return found


def _slowed(cache: NonceCache, pairing_id: str) -> NonceCache:
    cache._seen[pairing_id] = _SlowSeen()
    return cache


def test_one_nonce_presented_by_many_threads_is_accepted_exactly_once(clock: FakeClock) -> None:
    # GPT audit 5365019650 B-3: the policy route runs in a thread pool, so the same signed
    # request can arrive twice at once. Exactly one is accepted; every other is a replay.
    threads = 16
    for round_number in range(3):
        cache = _slowed(NonceCache(clock), "p")
        gate = threading.Barrier(threads)
        outcomes: list[str] = []

        def present(
            cache: NonceCache = cache,
            gate: threading.Barrier = gate,
            outcomes: list[str] = outcomes,
        ) -> None:
            gate.wait()
            try:
                cache.consume("p", "same-nonce")
            except NonceReplayed:
                outcomes.append("replayed")
            else:
                outcomes.append("accepted")

        workers = [threading.Thread(target=present) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert sorted(outcomes) == ["accepted"] + ["replayed"] * (threads - 1), round_number
        assert cache.size("p") == 1


def test_the_slowed_check_really_loses_without_the_lock(clock: FakeClock) -> None:
    # The test above can fail: the same cache with its lock replaced by one that excludes nobody
    # accepts the nonce more than once. So the lock is what makes it pass.
    class _NoLock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_: object) -> None:
            return None

    cache = _slowed(NonceCache(clock), "p")
    cache._lock = _NoLock()  # type: ignore[assignment]
    threads = 8
    gate = threading.Barrier(threads)
    accepted: list[int] = []

    def present(index: int) -> None:
        gate.wait()
        try:
            cache.consume("p", "same-nonce")
        except NonceReplayed:
            return
        accepted.append(index)

    workers = [threading.Thread(target=present, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert len(accepted) > 1


def test_many_threads_never_take_the_cache_over_its_bound(clock: FakeClock) -> None:
    capacity, threads = 4, 16
    cache = _slowed(NonceCache(clock, capacity=capacity), "p")
    gate = threading.Barrier(threads)
    accepted: list[int] = []

    def present(index: int) -> None:
        gate.wait()
        try:
            cache.consume("p", f"nonce-{index}")
        except NonceCacheFull:
            return
        accepted.append(index)

    workers = [threading.Thread(target=present, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert len(accepted) == capacity == cache.size("p")
