"""What the M3 findings are scanned against (Issue #52 comment 5689874555).

The generic scanner stays strict; only the choice of input values changes, so that a storefront's
one-character flag cookies stop colliding with ordinary structural text.
"""

import json

import pytest

from app.system.secret_scan import variants
from scripts.m3harness.inventory import (
    EXCLUSION_REASON,
    assert_sanitized,
    findings_secrets,
)

LOGIN = ("operator@example.invalid", "pa$$word-1234")
FINDINGS = {"path_form": "/product/{n}/category/{n}/", "hosts": ["img.cafe24.com"]}


def cookie(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value, "domain": "shop.invalid", "path": "/"}


def test_the_login_is_always_scanned() -> None:
    secrets = findings_secrets(LOGIN, [])
    assert secrets.values == LOGIN
    assert secrets.excluded_cookies == ()


@pytest.mark.parametrize("value", ["F", "1", "ko", "T12", "abcde", "00000"])
def test_a_short_alphanumeric_flag_on_an_ordinary_cookie_is_left_out(value: str) -> None:
    secrets = findings_secrets([], [cookie("ec_ipad_device", value)])
    assert secrets.values == ()
    assert secrets.excluded_cookies == ("ec_ipad_device",)
    assert secrets.audit() == {
        "scanned_values": 0,
        "excluded_cookies": ["ec_ipad_device"],
        "excluded_count": 1,
        "reason": EXCLUSION_REASON,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ECSESSID", "a1b2c"),  # session-like name, short value
        ("PHPSESSVERIFY", "1"),  # security-like name, one character
        ("login_provider_1", "kakao"),
        ("csrf", "abc"),
        ("auth_state", "zz"),
        ("return_url", "/"),  # not alphanumeric, so it stays in the scan
        ("wish_id", "abcdef"),  # six characters, over the bound
        ("fb_event_id", "12345678"),
    ],
)
def test_everything_else_stays_in_the_scan(name: str, value: str) -> None:
    secrets = findings_secrets([], [cookie(name, value)])
    assert secrets.values == (value,)
    assert secrets.excluded_cookies == ()


def test_the_audit_records_names_and_counts_but_never_values() -> None:
    cookies = [cookie("iscache", "1"), cookie("ec_mem_level", "2"), cookie("ECSESSID", "s3cr3tt")]
    secrets = findings_secrets(LOGIN, cookies)
    audit = secrets.audit()
    assert audit["excluded_cookies"] == ["ec_mem_level", "iscache"]
    assert audit["excluded_count"] == 2
    assert audit["scanned_values"] == 3  # the login and the session cookie
    rendered = repr(audit)
    for value in ("1", "2", "s3cr3tt", *LOGIN):
        assert value not in rendered or value in ("1", "2")  # counts may contain those digits
    assert "s3cr3tt" not in rendered


def test_excluded_values_no_longer_refuse_ordinary_findings() -> None:
    cookies = [cookie("iscache", "1"), cookie("ec_mem_level", "2")]
    assert_sanitized(FINDINGS, findings_secrets(LOGIN, cookies).values)


def test_a_session_value_still_refuses_findings_in_every_encoding() -> None:
    session = "S3ss10n-Value-9f2"
    secrets = findings_secrets(LOGIN, [cookie("ECSESSID", session)])
    refused = set()
    for encoding, forms in variants(session).items():
        for form in forms:
            try:
                carrier = {"leak": form.decode("utf-8")}
            except UnicodeDecodeError:
                continue  # a form of another text encoding cannot sit in a JSON string
            if form not in json.dumps(carrier, ensure_ascii=False).encode("utf-8"):
                continue  # JSON escaping reshapes this form; its raw text is covered above
            with pytest.raises(ValueError, match="disclose a secret value"):
                assert_sanitized(carrier, secrets.values)
            refused.add(encoding)
    assert refused >= {"raw", "url", "base64", "html"}
    with pytest.raises(ValueError, match="disclose a secret value"):
        assert_sanitized({"leak": LOGIN[1]}, secrets.values)


def test_the_generic_scanner_is_unchanged() -> None:
    # The exception lives in the input set only: the scanner still makes every family of needles
    # for any value, including a single character, and still refuses one it is given.
    for secret in ("1", "/", "S3ss10n-Value-9f2"):
        families = variants(secret)
        assert set(families) >= {"raw", "url", "json", "html"}, secret
        assert secret.encode("utf-8") in families["raw"], secret
        assert all(families[name] for name in ("raw", "url", "json", "html")), secret
    with pytest.raises(ValueError, match="disclose a secret value"):
        assert_sanitized(FINDINGS, ["/"])
