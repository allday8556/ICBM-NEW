import base64
import html
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from app.system.secret_scan import VARIANTS, scan
from tests.suppliers import PASSWORD


def _only(report: dict[str, object], label: str) -> dict[str, int]:
    return report["hits"][label]  # type: ignore[index,no-any-return]


@pytest.mark.parametrize(
    ("variant", "encoded"),
    [
        ("raw", PASSWORD.encode("utf-8")),
        ("raw", PASSWORD.encode("utf-16-le")),
        ("url", quote(PASSWORD, safe="").encode()),
        ("json", json.dumps({"p": PASSWORD})[6:-1].encode()),
        ("html", html.escape(PASSWORD).encode()),
        ("base64", base64.b64encode(b"x" + PASSWORD.encode())),
        ("base64", base64.b64encode(b"xy" + PASSWORD.encode() + b"tail")),
        ("base64", base64.urlsafe_b64encode(PASSWORD.encode())),
    ],
)
def test_each_encoding_of_a_secret_is_found(tmp_path: Path, variant: str, encoded: bytes) -> None:
    (tmp_path / "artifact.bin").write_bytes(b"prefix " + encoded + b" suffix")
    counts = _only(scan([tmp_path], {"password": PASSWORD}), "password")
    assert counts[variant] >= 1


def test_clean_artifacts_report_zero_and_never_the_value(tmp_path: Path) -> None:
    (tmp_path / "db").write_bytes(b"nothing to see")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.jsonl").write_text('{"msg": "supplier.request"}', "utf-8")
    report = scan([tmp_path], {"password": PASSWORD})
    assert report["files_scanned"] == 2
    assert report["total_hits"] == 0
    assert _only(report, "password") == dict.fromkeys(VARIANTS, 0)
    assert PASSWORD not in json.dumps(report, ensure_ascii=False)


def test_empty_secrets_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        scan([tmp_path], {"password": ""})
