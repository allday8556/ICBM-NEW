"""What the server re-checks of a capture (ADR-0019 §5, §6): its size and what the policy allows."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.stages.collect.extension.buffer import BufferedCapture, CaptureBuffer, CaptureBufferFull
from app.stages.collect.extension.capture import (
    CaptureEnvelope,
    TransportEvidence,
    measure,
    policy_violations,
)
from app.stages.collect.extension.policy import BrowserCapturePolicy, CapturePolicySource
from tests.support.extension_support import (
    BODY,
    HEAD,
    REPO_ROOT,
    SUPPLIER,
    envelope,
    frame,
    transport,
)


@pytest.fixture(scope="module")
def policy() -> BrowserCapturePolicy:
    return CapturePolicySource(REPO_ROOT / "integrations" / "suppliers").load(SUPPLIER)


def test_a_conforming_capture_has_no_violation(policy: BrowserCapturePolicy) -> None:
    assert policy_violations(frame(), policy) == ()


def test_measure_counts_exactly_what_arrived() -> None:
    measured = measure(frame())
    assert measured.html_bytes == len(frame().encode("utf-8"))
    # Every element of what arrived: the frame, the three head elements and the scope.
    assert measured.nodes == len(re.findall(r"<[a-z]", frame()))
    # The key image's src and the description image's ec-data-src.
    assert measured.image_refs == 2


def test_each_image_reference_counts_once_and_srcset_by_entry() -> None:
    body = (
        '<div class="xans-product-detail">'
        '<img id="a" class="b" style="c" src="/1.jpg" data-src="/2.jpg"'
        ' srcset="/3.jpg 1x, /4.jpg 2x">'
        '<img src="  "><img class="only">'
        "</div>"
    )
    assert measure(frame(body=body)).image_refs == 4


@pytest.mark.parametrize(
    ("head", "body", "violation"),
    [
        # Anything in <head> beyond the three identity declarations.
        (HEAD + '<meta property="og:title" content="x">', BODY, "HEAD_NOT_ALLOWED:meta"),
        (HEAD + "<title>x</title>", BODY, "HEAD_NOT_ALLOWED:title"),
        (HEAD + '<link rel="stylesheet" href="/a.css">', BODY, "HEAD_NOT_ALLOWED:link"),
        (HEAD + "<script>x()</script>", BODY, "HEAD_NOT_ALLOWED:script"),
        # An excluded tag in the scope.
        (HEAD, BODY + "<script>x()</script>", "TAG_EXCLUDED:script"),
        (HEAD, BODY + '<iframe src="/x"></iframe>', "TAG_EXCLUDED:iframe"),
        (HEAD, BODY + "<textarea>typed</textarea>", "TAG_EXCLUDED:textarea"),
        (HEAD, BODY + '<meta property="og:title" content="x">', "TAG_EXCLUDED:meta"),
        # An attribute outside the allowlist.
        (
            HEAD,
            '<div class="xans-product-detail" onclick="x()"></div>',
            "ATTRIBUTE_NOT_ALLOWED:div[onclick]",
        ),
        (
            HEAD,
            '<div class="xans-product-detail"><a href="/member">x</a></div>',
            "ATTRIBUTE_NOT_ALLOWED:a[href]",
        ),
        (
            HEAD,
            '<div class="xans-product-detail"><input value="3"></div>',
            "ATTRIBUTE_NOT_ALLOWED:input[value]",
        ),
        (
            HEAD,
            '<div class="xans-product-detail"><img alt="x" src="/a.jpg"></div>',
            "ATTRIBUTE_NOT_ALLOWED:img[alt]",
        ),
        (
            HEAD,
            '<div class="xans-product-detail" data-member-token="t"></div>',
            "ATTRIBUTE_NOT_ALLOWED:div[data-member-token]",
        ),
        # A comment is not something the extension ever writes.
        (HEAD, BODY + "<!-- note -->", "COMMENT"),
    ],
)
def test_what_the_policy_does_not_allow_is_named(
    policy: BrowserCapturePolicy, head: str, body: str, violation: str
) -> None:
    assert violation in policy_violations(frame(head, body), policy)


@pytest.mark.parametrize(
    "html",
    [
        BODY,
        f"<html><body>{BODY}</body></html>",
        f"<html><head>{HEAD}</head></html>",
        f"<html><body>{BODY}</body><head>{HEAD}</head></html>",
        f'<html lang="ko"><head>{HEAD}</head><body>{BODY}</body></html>',
        f"<html><head>{HEAD}</head><body>{BODY}</body><body></body></html>",
    ],
)
def test_a_capture_outside_the_exact_frame_is_a_violation(
    policy: BrowserCapturePolicy, html: str
) -> None:
    assert policy_violations(html, policy)


def test_a_violation_names_a_kind_never_a_value(policy: BrowserCapturePolicy) -> None:
    body = (
        '<div class="xans-product-detail" data-member-token="not-a-real-token">010-0000-0000</div>'
    )
    found = policy_violations(frame(body=body), policy)
    assert found == ("ATTRIBUTE_NOT_ALLOWED:div[data-member-token]",)
    assert "not-a-real-token" not in "".join(found) and "010" not in "".join(found)


# ---------------------------------------------------------------- the envelope


def test_the_envelope_is_exact_and_strict() -> None:
    accepted = CaptureEnvelope.model_validate(envelope())
    assert accepted.supplier_key == SUPPLIER and accepted.transport.response_status == 200
    # The capture is never part of a representation that could reach a log.
    assert "xans-product-detail" not in repr(accepted)
    for broken in (
        envelope(cookies="a=b"),
        envelope(headers={"Authorization": "x"}),
        envelope(transport={**transport(), "cookie": "a=b"}),
        envelope(transport={k: v for k, v in transport().items() if k != "response_status"}),
        envelope(transport=transport(response_status="200")),
        envelope(transport=transport(response_status=None)),
        envelope(transport=transport(redirect_count=False)),
        envelope(policy={"revision": "x"}),
        envelope(html=None) | {"html": 1},
        {k: v for k, v in envelope().items() if k != "html"},
    ):
        with pytest.raises(ValidationError):
            CaptureEnvelope.model_validate(broken)


# ---------------------------------------------------------------- the in-process buffer


def _capture(html: str = "<html></html>") -> BufferedCapture:
    return BufferedCapture(html, TransportEvidence.model_validate(transport()))


def test_the_buffer_hands_a_capture_over_exactly_once() -> None:
    buffer = CaptureBuffer()
    buffer.put("run-1", _capture("one"))
    assert len(buffer) == 1
    taken = buffer.take("run-1")
    assert taken is not None and taken.html == "one"
    assert buffer.take("run-1") is None and len(buffer) == 0
    # A capture is never part of a representation.
    assert "one" not in repr(taken)


def test_the_buffer_is_bounded_and_refuses_when_full() -> None:
    buffer = CaptureBuffer(capacity=2)
    buffer.put("a", _capture())
    buffer.put("b", _capture())
    with pytest.raises(CaptureBufferFull) as full:
        buffer.put("c", _capture())
    assert full.value.code == "EXTENSION_CAPTURE_BUFFER_FULL"
    buffer.discard("a")
    buffer.put("c", _capture())
    with pytest.raises(ValueError):
        CaptureBuffer(capacity=0)


def test_the_buffer_module_touches_no_durable_store() -> None:
    # Ruling 5906712259 N-1: memory only. The module opens no file and no database.
    source = Path(REPO_ROOT / "app" / "stages" / "collect" / "extension" / "buffer.py").read_text(
        "utf-8"
    )
    for forbidden in ("open(", "sqlite", "Database", "write_text", "write_bytes", "pickle", "json"):
        assert forbidden not in source, forbidden
