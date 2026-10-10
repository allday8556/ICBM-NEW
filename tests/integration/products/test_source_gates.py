"""The source gates against the real Product owner (ADR-0031 §4, ADR-0030 §7).

Each Item is materialized from a revision the real COLLECT store recorded; no supplier is
contacted.
"""

from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.stages.collect.facts import (
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    SalesChannelScope,
    SalesChannelsValue,
)
from app.stages.products.source_gates import (
    SOURCE_CHANNEL_FORBIDDEN,
    SOURCE_CHANNEL_UNRESOLVED,
    SUPPLIER_NOT_ACTIVE,
    SourceGates,
)
from tests.support.product_support import Collections, product


def _services(client: TestClient) -> Container:
    return client.app.state.container  # type: ignore[attr-defined,no-any-return]


def _item(
    client: TestClient, config: AppConfig, channels: FieldFact | None, **overrides: object
) -> str:
    services = _services(client)
    fields = product()
    if channels is not None:
        fields["sales_channels"] = channels
    run_id, _revision = Collections.of(services, config).collect(fields, **overrides)
    result = services.materializer.materialize_run(run_id)
    assert result.item_id is not None
    return result.item_id


def _channels(scope: SalesChannelScope, *forbidden: str) -> FieldFact:
    return FieldFact(
        FieldStatus.CONFIRMED,
        SalesChannelsValue(scope=scope, forbidden=forbidden, policy_text="공급사 문구"),
        (Evidence(EvidenceKind.DOM_TEXT, "th:판매가능플랫폼 + td", FieldStatus.CONFIRMED),),
    )


def test_a_closed_mall_product_is_forbidden_on_every_marketplace(
    client: TestClient, config: AppConfig
) -> None:
    item = _item(client, config, _channels(SalesChannelScope.CLOSED_MALL_ONLY))
    gates = SourceGates(_services(client).product_store, lambda key: None)
    assert gates("smartstore", [item]) == ((item, SOURCE_CHANNEL_FORBIDDEN),)
    assert gates("coupang", [item]) == ((item, SOURCE_CHANNEL_FORBIDDEN),)


def test_a_coupang_restriction_does_not_gate_smartstore(
    client: TestClient, config: AppConfig
) -> None:
    item = _item(client, config, _channels(SalesChannelScope.LISTED, "coupang"))
    gates = SourceGates(_services(client).product_store, lambda key: None)
    assert gates("smartstore", [item]) == ()


def test_an_unread_restriction_is_unresolved(client: TestClient, config: AppConfig) -> None:
    unread = FieldFact(
        FieldStatus.REVIEW_REQUIRED,
        None,
        (Evidence(EvidenceKind.DOM_TEXT, "th:판매가능플랫폼 + td", FieldStatus.REVIEW_REQUIRED),),
    )
    item = _item(client, config, unread)
    gates = SourceGates(_services(client).product_store, lambda key: None)
    assert gates("smartstore", [item]) == ((item, SOURCE_CHANNEL_UNRESOLVED),)


def test_a_template_revision_needs_an_active_site(client: TestClient, config: AppConfig) -> None:
    item = _item(client, config, None, extractor_revision="cafe24-2+example-1")
    store = _services(client).product_store
    # In RECON, and gone from the configuration: never active.
    assert SourceGates(store, lambda key: False)("smartstore", [item]) == (
        (item, SUPPLIER_NOT_ACTIVE),
    )
    assert SourceGates(store, lambda key: None)("smartstore", [item]) == (
        (item, SUPPLIER_NOT_ACTIVE),
    )
    assert SourceGates(store, lambda key: True)("smartstore", [item]) == ()


def test_a_package_revision_is_not_gated_by_site_status(
    client: TestClient, config: AppConfig
) -> None:
    item = _item(client, config, None)
    gates = SourceGates(_services(client).product_store, lambda key: None)
    assert gates("smartstore", [item]) == ()
