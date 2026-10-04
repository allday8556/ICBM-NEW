"""B-PRICE1: re-price a Draft through the M4 pricing owner and re-pin it, through the real
application on a migrated database. No provider is contacted.

- the operator names only the Draft revision they saw: M4 prices every open Item under the
  account's current policy context, and the Draft is re-pinned to what M4 holds current, one
  revision per moved pin (``change_draft_item_price``), all or nothing;
- ``UNCHANGED`` re-pins nothing and moves no revision; ``NOT_PRICED`` re-pins nothing and returns
  M4's reasons; a moved Draft is refused;
- while an Intent of the Draft may still send its frozen price, nothing is re-pinned;
- REGISTER writes no price: every snapshot is M4's, and its minimum-sale-price basis is M4's.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from tests.integration.register.test_g1d_draft_command import (  # noqa: F401 - fixtures
    account,
    api,
    body,
    container,
    counts,
    create,
    materialize,
    refused,
    selection,
    sources,
)
from tests.integration.review.test_g2c_review_counts import _freeze
from tests.support.gate1_support import CLIENT, MARKET, OPERATOR, save_policy
from tests.support.product_support import Collections, product

pytestmark = pytest.mark.integration

CID = "cid-b-price1"


def drafted(api: TestClient, container: Container, sources: Collections, account: str) -> Any:  # noqa: F811
    save_policy(api, account)
    group = materialize(container, sources, "S-REPIN", product())
    membership, items = selection(api, group)
    created = create(api, body(group, membership, [items[1]], account)).json()
    return created


def repin(api: TestClient, draft_id: str, revision: int) -> Any:  # noqa: F811
    return api.post(
        f"/api/v1/register/drafts/{draft_id}/repin",
        json={"actor": OPERATOR, "expected_draft_revision": revision},
        headers=CLIENT,
    )


def test_a_moved_pricing_context_is_repriced_by_m4_and_repinned(
    api: TestClient,  # noqa: F811
    config: AppConfig,
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
) -> None:
    created = drafted(api, container, sources, account)
    draft_id, revision = created["draft_id"], created["draft_revision"]
    old_pin = created["items"][0]["pricing_snapshot_id"]
    # The account's policy now prices for this account only: the pin's context is stale.
    policy_revision = save_policy(api, account, account_scoped=True)
    before = counts(config)
    response = repin(api, draft_id, revision)
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved["policy_revision"] == policy_revision
    (item,) = moved["items"]
    assert (item["previous_pricing_snapshot_id"], item["repinned"]) == (old_pin, True)
    assert item["pricing_outcome"] == "RECORDED" and item["pricing_snapshot_id"] != old_pin
    assert moved["draft_revision"] == revision + 1
    draft = container.registrations.draft(draft_id)
    assert draft is not None and draft.draft_revision == revision + 1
    assert [i.pricing_snapshot_id for i in draft.items] == [item["pricing_snapshot_id"]]
    # The price is M4's own snapshot under the policy's context; REGISTER wrote no price.
    snapshot = container.registrations.pricing_pin(item["pricing_snapshot_id"])
    policy = container.registration_preflight.target_policy(MARKET, account)
    assert snapshot is not None and policy is not None
    assert snapshot.pricing_context_fingerprint == policy.pricing_context.fingerprint
    assert (snapshot.final_sale_price_krw, snapshot.price_basis.value) == (
        item["final_sale_price_krw"],
        item["price_basis"],
    )
    after = counts(config)
    assert after["pricing_snapshots"] == before["pricing_snapshots"] + 1
    assert after["registration_drafts"] == before["registration_drafts"]
    # Again: M4 holds the same price, so nothing moves.
    again = repin(api, draft_id, revision + 1)
    assert again.status_code == 200, again.text
    (same,) = again.json()["items"]
    assert (same["pricing_outcome"], same["repinned"]) == ("UNCHANGED", False)
    assert again.json()["draft_revision"] == revision + 1
    assert counts(config)["pricing_snapshots"] == after["pricing_snapshots"]


def test_a_moved_draft_is_refused_and_nothing_is_repinned(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
) -> None:
    created = drafted(api, container, sources, account)
    response = repin(api, created["draft_id"], created["draft_revision"] + 5)
    refused(response, 409, "REGISTER_DRAFT_REVISION_MOVED")
    draft = container.registrations.draft(created["draft_id"])
    assert draft is not None and draft.draft_revision == created["draft_revision"]
    refused(repin(api, "no-such-draft", 1), 404, "REGISTER_DRAFT_NOT_FOUND")


def test_an_open_intent_refuses_the_repin(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
) -> None:
    created = drafted(api, container, sources, account)
    draft_id, revision = created["draft_id"], created["draft_revision"]
    item_id = created["items"][0]["item_id"]
    snapshot = _freeze(container, draft_id, item_id)
    with container.registrations.transaction() as unit:
        batch = unit.create_batch(MARKET, account, created_by=OPERATOR, correlation_id=CID)
        intent = unit.create_intent(batch, snapshot, created_by=OPERATOR, correlation_id=CID)
    save_policy(api, account, account_scoped=True)
    error = refused(repin(api, draft_id, revision), 409, "REGISTER_REPIN_INTENT_OPEN")
    assert error["details"]["intent_ids"] == [intent.intent_id]
    draft = container.registrations.draft(draft_id)
    assert draft is not None and draft.draft_revision == revision
    assert [i.pricing_snapshot_id for i in draft.items] == [
        created["items"][0]["pricing_snapshot_id"]
    ]


def test_an_unpriced_item_repins_nothing_and_returns_m4s_reasons(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = drafted(api, container, sources, account)
    draft_id, revision = created["draft_id"], created["draft_revision"]
    from app.stages.products.model import ReadinessStatus, Reason
    from app.stages.products.pricing_service import PricingOutcome, PricingResult

    def not_priced(item_id: str, context: Any, *, correlation_id: str | None = None) -> Any:
        return PricingResult(
            outcome=PricingOutcome.NOT_PRICED,
            item_id=item_id,
            context_fingerprint=context.fingerprint,
            snapshot=None,
            move=None,
            reasons=(Reason("BINDING_PROVENANCE_STALE", ReadinessStatus.STALE),),
        )

    monkeypatch.setattr(container.drafting._pricing, "price", not_priced)
    error = refused(repin(api, draft_id, revision), 409, "REGISTER_DRAFT_ITEM_NOT_PRICED")
    assert error["details"]["items"] == {
        created["items"][0]["item_id"]: [{"code": "BINDING_PROVENANCE_STALE", "subject": None}]
    }
    draft = container.registrations.draft(draft_id)
    assert draft is not None and draft.draft_revision == revision
