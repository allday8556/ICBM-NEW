"""The Coupang endpoint inventory stays complete, classified, and provider-zero."""

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "documents/contracts/platforms/coupang/ENDPOINT_INVENTORY.md"

ROW = re.compile(
    r"^\| (?P<number>\d+) \| `(?P<method>[^`]+)` \| "
    r"`(?P<path>[^`]+)` \| \[(?P<title>[^]]+)\]\((?P<source>[^)]+)\) \| "
    r"`(?P<market>[^`]+)` \| `(?P<effect>[^`]+)` \| "
    r"`(?P<classification>[^`]+)` \|$"
)

ALLOWED_CLASSIFICATIONS = {
    "IMPLEMENT",
    "CONTRACT_ONLY",
    "ACCOUNT_GATED",
    "OUT_OF_PRODUCT_SCOPE",
}

ALLOWED_MARKETS = {"KR", "KR/TW", "TW", "INDEX KR/TW"}
ALLOWED_EFFECTS = {
    "READ",
    "RECOMMENDATION_READ",
    "DOCUMENT_READ",
    "MUTATION",
    "REFERENCE",
}


def _endpoint_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match is not None:
            rows.append(match.groupdict())
    return rows


def test_inventory_contains_every_official_index_row_once() -> None:
    rows = _endpoint_rows()

    assert [int(row["number"]) for row in rows] == list(range(1, 103))
    assert len(rows) == 102


def test_every_endpoint_has_one_closed_classification() -> None:
    rows = _endpoint_rows()
    counts = Counter(row["classification"] for row in rows)

    assert set(counts) == ALLOWED_CLASSIFICATIONS
    assert counts == {
        "IMPLEMENT": 72,
        "CONTRACT_ONLY": 2,
        "ACCOUNT_GATED": 26,
        "OUT_OF_PRODUCT_SCOPE": 2,
    }


def test_every_endpoint_links_its_official_detail_and_closes_semantics() -> None:
    rows = _endpoint_rows()

    assert {row["market"] for row in rows} <= ALLOWED_MARKETS
    assert {row["effect"] for row in rows} <= ALLOWED_EFFECTS
    assert all(row["source"].startswith("https://developers.coupang.com/en/api/") for row in rows)
    assert len({row["source"] for row in rows}) == 102
    assert all(row["effect"] == "READ" for row in rows if row["method"] == "GET")
    assert next(row for row in rows if row["number"] == "33")["effect"] == "DOCUMENT_READ"


def test_inventory_is_not_an_adoption_or_live_authority() -> None:
    text = INVENTORY.read_text(encoding="utf-8")

    assert "Runtime adoption | `NONE`" in text
    assert "`IMPLEMENT != callable`" in text
    assert "`inventory classification != LIVE authority`" in text
    assert "https://developers.coupang.com/en/api" in text
    assert "TBD" not in text
