"""The canonical SmartStore token request (AUTH.md §3, §4, §21; ENDPOINT_MATRIX §6, §13 #4)."""

import base64

import bcrypt
import pytest

from integrations.marketplaces.smartstore.signing import (
    TOKEN_FORM_FIELDS,
    ApplicationCredentials,
    SignatureError,
    client_secret_sign,
    token_form,
)

# A fixture bcrypt salt standing in for a client secret; never a real credential.
SECRET = "$2a$04$abcdefghijklmnopqrstuu"
CLIENT_ID = "fixture-client-id"
TIMESTAMP_MS = 1757894400000
# bcrypt("fixture-client-id_1757894400000", salt=SECRET), Base64-encoded. bcrypt is
# deterministic for a given salt, so this pins the §4 construction.
EXPECTED_SIGN = "JDJhJDA0JGFiY2RlZmdoaWprbG1ub3BxcnN0dXVrQ2JKdFFEL0pEMWdidUtKalVmMEg2ajNWeEJyWThp"


def test_the_signature_is_base64_of_bcrypt_over_client_id_and_timestamp() -> None:
    assert client_secret_sign(CLIENT_ID, SECRET, TIMESTAMP_MS) == EXPECTED_SIGN
    assert base64.b64decode(EXPECTED_SIGN) == bcrypt.hashpw(
        f"{CLIENT_ID}_{TIMESTAMP_MS}".encode(), SECRET.encode()
    )


def test_the_signature_belongs_to_its_timestamp() -> None:
    assert client_secret_sign(CLIENT_ID, SECRET, TIMESTAMP_MS + 1) != EXPECTED_SIGN


def test_em13_4_the_token_form_is_exactly_the_self_contract() -> None:
    form = token_form(ApplicationCredentials(CLIENT_ID, SECRET, 1), TIMESTAMP_MS)
    assert form == {
        "client_id": CLIENT_ID,
        "timestamp": str(TIMESTAMP_MS),
        "client_secret_sign": EXPECTED_SIGN,
        "grant_type": "client_credentials",
        "type": "SELF",
    }
    assert set(form) == TOKEN_FORM_FIELDS
    assert "account_id" not in form


@pytest.mark.parametrize(
    "secret", ["not-a-bcrypt-salt", "$2a$04$tooshort", "$9z$04$abcdefghijklmnopqrstuu"]
)
def test_an_unusable_secret_fails_locally_without_naming_it(secret: str) -> None:
    with pytest.raises(SignatureError) as caught:
        client_secret_sign(CLIENT_ID, secret, TIMESTAMP_MS)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.parametrize("timestamp", [0, -1, True])
def test_the_timestamp_is_a_positive_millisecond_time(timestamp: int) -> None:
    with pytest.raises(ValueError):
        token_form(ApplicationCredentials(CLIENT_ID, SECRET, 1), timestamp)


@pytest.mark.parametrize(
    ("client_id", "secret", "generation"),
    [
        ("", SECRET, 1),
        ("  ", SECRET, 1),
        (CLIENT_ID, "", 1),
        (CLIENT_ID, SECRET, 0),
        (CLIENT_ID, SECRET, True),
    ],
)
def test_a_credential_bundle_is_complete_and_committed(
    client_id: str, secret: str, generation: int
) -> None:
    with pytest.raises(ValueError):
        ApplicationCredentials(client_id, secret, generation)


def test_a_credential_bundle_never_shows_its_secret() -> None:
    credentials = ApplicationCredentials(CLIENT_ID, SECRET, 1)
    assert SECRET not in repr(credentials)
    assert SECRET not in str(credentials)
