"""M5 PR-C: the deterministic outbound payload of one provider-listing unit (ADR-0014 §6, §7, §15;
kickoff 5742880432 §4, §12).

Pure: it consumes a **final READY** :class:`~app.register.preparation.PreflightResult` and returns
the sanitized canonical representation the PR-B Snapshot freezes, with the ``payload_hash`` the
store will compute from it. It reads nothing else, so nothing it sends can differ from what was
evaluated:
- each Item's price is the Draft's exact pinned PricingSnapshot, never another current price;
- the scope is the canonical ``marketplace_account_id``;
- ``registration_item_key`` derives from the unit's listing identity and the Item key only;
- publication assets are exact local artifact identities, with the provider reference PR-D
  prepared under the candidate fingerprint; never a supplier URL;
- no credential, session or tokenized material can be in it (``app.register.sanitize``).

The SmartStore wire representation is PR-D's: this is the business representation it encodes.
"""

from dataclasses import dataclass
from typing import Any, Final

from app.products.model import ReadinessStatus
from app.register import sanitize
from app.register.model import registration_item_key, sanitized_digest
from app.register.preparation import PreflightResult, PreflightStage

PAYLOAD_BUILDER_VERSION: Final = "registration-payload/v1"


class PayloadNotReadyError(ValueError):
    """Only a final READY preflight result has a payload."""


@dataclass(frozen=True)
class OutboundItem:
    item_id: str
    registration_item_key: str
    source_snapshot: dict[str, Any]
    publication_assets: tuple[dict[str, Any], ...]
    outbound_values: dict[str, Any]


@dataclass(frozen=True)
class OutboundPayload:
    payload: dict[str, Any]
    payload_digest: str
    items: tuple[OutboundItem, ...]
    policy_revisions: dict[str, str]


def build_payload(result: PreflightResult) -> OutboundPayload:
    if result.stage is not PreflightStage.FINAL or result.status is not ReadinessStatus.READY:
        raise PayloadNotReadyError("a payload is built only from a final READY preflight")
    request, unit = result.request, result.resolved
    target, category, detail, listing = (
        unit.target,
        request.category,
        request.detail,
        request.listing,
    )
    metadata = unit.metadata
    # A final READY result has every one of these; anything else is a broken caller.
    assert category is not None and detail is not None and metadata is not None
    assert listing.name is not None
    profile = target.asset_policy.profile
    prepared = {asset.key: asset for asset in result.prepared_assets}
    items: list[OutboundItem] = []
    payload_items: list[dict[str, Any]] = []
    for item in unit.items:
        pin, binding = item.pin, item.binding
        assert pin is not None and binding is not None
        key = registration_item_key(
            unit.listing_identity, item.product_group_id, item.composition_signature
        )
        assets = tuple(
            {
                **image.canonical(),
                "asset_profile": profile,
                "provider_asset_ref": (
                    prepared[image.key].provider_asset_ref if image.key in prepared else None
                ),
            }
            for image in item.images
        )
        options = dict(sorted(listing.options.get(item.item_id, {}).items()))
        outbound = {
            "pricing_snapshot_id": pin.pricing_snapshot_id,
            "sale_price_krw": pin.final_sale_price_krw,
            "price_basis": pin.price_basis.value,
            "options": options,
        }
        source_snapshot = {
            **binding.canonical(),
            "pricing_snapshot_id": pin.pricing_snapshot_id,
            "membership_revision_id": pin.membership_revision_id,
            "source_product_facts_revision_id": pin.source_product_facts_revision_id,
        }
        items.append(OutboundItem(item.item_id, key, source_snapshot, assets, outbound))
        payload_items.append(
            {
                "registration_item_key": key,
                "item_id": item.item_id,
                "product_group_id": item.product_group_id,
                "composition_signature": item.composition_signature,
                **outbound,
                "publication_assets": list(assets),
            }
        )
    selected_category = {
        "category_id": category.category_id,
        "mapping_revision": category.mapping_revision,
        "taxonomy_revision": category.taxonomy_revision,
    }
    business = {
        "category": selected_category,
        "name": listing.name.canonical(),
        "tags": sorted(set(listing.tags)),
        "attributes": {k: v.canonical() for k, v in sorted(listing.attributes.items())},
        "notice": None
        if metadata.notice is None
        else {
            "notice_type": metadata.notice.notice_type,
            "fields": {k: v.canonical() for k, v in sorted(listing.notices.items())},
        },
        "detail": {
            "composition_revision": detail.composition_revision,
            "sections": list(detail.sections),
            "body": detail.body,
        },
        "options": {i.item_id: i.outbound_values["options"] for i in items},
        "templates": dict(sorted(target.templates.items())),
    }
    # Fail closed even if a caller bypassed the preflight's own sanitation reasons.
    sanitize.require_clean(business)
    for asset in (a for i in items for a in i.publication_assets):
        reference = asset["provider_asset_ref"]
        if reference is not None and not sanitize.safe_provider_reference(reference):
            raise sanitize.PayloadSanitationError(
                ((sanitize.SECRET_MATERIAL, "publication_assets.provider_asset_ref"),)
            )
    payload = {
        "builder_version": PAYLOAD_BUILDER_VERSION,
        "marketplace_key": unit.marketplace_key,
        "marketplace_account_id": unit.marketplace_account_id,
        "listing_shape": unit.listing_shape.value,
        "listing_identity": unit.listing_identity,
        "category": {**selected_category, "metadata_revision": metadata.metadata_revision},
        "name": business["name"],
        "tags": business["tags"],
        "attributes": business["attributes"],
        "notice": business["notice"],
        "policy": {
            "policy_revision": target.policy_revision,
            "templates": business["templates"],
        },
        "detail": business["detail"],
        "items": payload_items,
    }
    policy_revisions = {
        "policy_revision": target.policy_revision,
        "pricing_context_fingerprint": target.pricing_context.fingerprint,
        "metadata_revision": metadata.metadata_revision,
        **{f"template:{kind}": identity for kind, identity in sorted(target.templates.items())},
    }
    return OutboundPayload(payload, sanitized_digest(payload), tuple(items), policy_revisions)
