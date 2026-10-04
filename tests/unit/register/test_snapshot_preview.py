"""B-PREVIEW (owner decisions 5975647306): what one frozen Snapshot would send.

- the frozen outbound values and every field of the provider document are shown;
- no provider URL ever appears: image leaves are redacted, images are shown by local identity and
  whether a provider asset was prepared, and the detail body is structure, never HTML;
- frozen values the projection does not send (tags, attributes) are marked not sent;
- a projection the adapter refuses is shown with its own code.
"""

import copy
import json
from typing import Any

from app.stages.register.detail import DETAIL_RENDERER_VERSION
from app.stages.register.preview import (
    DETAIL_PAGE_REFERENCE,
    PROJECTION_REFUSED,
    preview,
)
from integrations.marketplaces.smartstore import product
from tests.unit.integrations.marketplaces.smartstore.test_m5_register_adapter import (
    REF_MAIN,
    _asset,
    payload,
)

HASH = "h" * 64


def no_url(view: Any) -> bool:
    return "://" not in json.dumps(view.model_dump(mode="json"), ensure_ascii=False)


def test_the_frozen_values_and_the_document_are_shown_without_any_provider_url() -> None:
    frozen = payload()
    view = preview("snap-1", HASH, frozen, product.project)
    assert view.projected and view.encoding_version == product.WIRE_ENCODING_VERSION
    assert (view.name, view.sale_prices_krw, view.category_id) == ("테스트 상품", (19900,), "cat-1")
    fields = {f.path: f for f in view.document}
    assert fields["originProduct.name"].value == "테스트 상품"
    assert fields["originProduct.salePrice"].value == "19900"
    assert fields["originProduct.leafCategoryId"].value == "cat-1"
    # Provider references and the rendered detail body are redacted, never shown.
    image = fields["originProduct.images.representativeImage.url"]
    assert (image.value, image.redacted) == (None, True)
    assert fields["originProduct.detailContent"].redacted
    assert no_url(view)
    # Images by local identity, with whether a provider asset was prepared.
    assert [(i.role, i.provider_asset_prepared) for i in view.images] == [
        ("REPRESENTATIVE", True),
        ("ADDITIONAL", True),
    ]
    # The v1 body as structure.
    assert view.detail is not None
    assert (view.detail.builder, view.detail.paragraphs) == ("BODY_ONLY", ("본문",))
    # Frozen but not sent.
    assert {(n.path, n.value) for n in view.not_sent} == {
        ("tags[0]", "tag-a"),
        ("attributes.brand", "KM"),
    }


def test_a_detail_plan_is_shown_as_structure_never_html() -> None:
    frozen = payload()
    detail_asset = _asset("DETAIL", "3" * 64, "https://shop-phinf.example/a/detail-3.jpg")
    frozen["items"][0]["publication_assets"].append(detail_asset)
    frozen["builder_version"] = "registration-payload/v2"
    frozen["detail"] = {
        "composition_revision": "detail-2",
        "sections": ["DETAIL_IMAGES", "BODY"],
        "body_format": "PLAIN_TEXT",
        "renderer": DETAIL_RENDERER_VERSION,
        "body": "first <b>paragraph</b>\n\nsecond",
        "images": [
            {
                "item_id": frozen["items"][0]["item_id"],
                "position": 0,
                "asset_kind": "SOURCE_ASSET",
                "sha256": "3" * 64,
                "derivation_id": None,
            }
        ],
    }
    view = preview("snap-2", HASH, frozen, product.project)
    assert view.projected and view.detail is not None
    assert view.detail.builder == "PLAN"
    assert view.detail.sections == ("DETAIL_IMAGES", "BODY")
    assert view.detail.image_slots == ("3" * 64,)
    # The operator's text is plain text: shown as written, never parsed or rendered.
    assert view.detail.paragraphs == ("first <b>paragraph</b>", "second")
    dumped = json.dumps(view.model_dump(mode="json"), ensure_ascii=False)
    assert "<img" not in dumped and no_url(view)


def test_a_refused_projection_is_shown_with_its_own_code() -> None:
    broken = copy.deepcopy(payload())
    broken["items"][0]["publication_assets"][0]["provider_asset_ref"] = None
    view = preview("snap-3", HASH, broken, product.project)
    assert (view.projected, view.refusal_code) == (False, "WIRE_IMAGE_NOT_PREPARED")
    assert view.document == () and view.name == "테스트 상품"
    unwired = preview("snap-3", HASH, payload(), None)
    assert (unwired.projected, unwired.refusal_code) == (False, PROJECTION_REFUSED)


def test_a_detail_page_reference_attribute_is_named_as_such() -> None:
    frozen = payload(attributes={"origin": {"detail_page_reference": True, "provenance": "X"}})
    view = preview("snap-4", HASH, frozen, product.project)
    assert [(n.path, n.value) for n in view.not_sent if n.path.startswith("attributes")] == [
        ("attributes.origin", DETAIL_PAGE_REFERENCE)
    ]
    assert REF_MAIN not in json.dumps(view.model_dump(mode="json"))
