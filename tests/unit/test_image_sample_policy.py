"""How a bounded image sample is spent (Issue #52 comment 5696242775 §2, §3).

The policy is generic: it knows roles, an order and a cap. What a role *means* is the supplier's
site knowledge, tested separately.
"""

import pytest

from integrations.suppliers.collection import (
    RECONNAISSANCE_ONLY,
    ImageCandidate,
    ImageRole,
    plan_image_sample,
)

RULES = "supplier-images-1"


def candidate(order: int, role: ImageRole, path: str = "", host: str = "img.example.invalid"):
    return ImageCandidate(
        url=f"https://{host}{path or f'/{order}.jpg'}", role=role, order=order, rule=f"r.{role}"
    )


def test_the_sample_is_one_primary_then_the_detail_sequence() -> None:
    pool = [
        candidate(0, ImageRole.UI_COMMON),
        candidate(1, ImageRole.PRIMARY),
        *[candidate(2 + n, ImageRole.DETAIL) for n in range(9)],
        candidate(20, ImageRole.THUMBNAIL),
        candidate(21, ImageRole.PRODUCT_AUX),
    ]
    plan = plan_image_sample(pool, 6, rules=RULES)
    assert [c.role for c in plan.selected] == [ImageRole.PRIMARY, *[ImageRole.DETAIL] * 5]
    # The detail images are taken in the page's own order, not in the order they were handed over.
    assert [c.order for c in plan.selected] == [1, 2, 3, 4, 5, 6]


def test_a_common_or_unrecognised_reference_never_consumes_a_slot() -> None:
    # The whole page is furniture and one reference no rule recognised: nothing is sampled.
    pool = [
        *[candidate(n, ImageRole.UI_COMMON) for n in range(8)],
        candidate(8, ImageRole.UNKNOWN),
    ]
    plan = plan_image_sample(pool, 6, rules=RULES)
    assert plan.selected == ()
    assert {reason for _, reason in plan.excluded} == {"UI_COMMON", "UNKNOWN"}


def test_an_unrecognised_reference_does_not_fail_open_into_product_sampling() -> None:
    # Even with the cap wide open and one real product image, UNKNOWN stays out.
    pool = [
        candidate(0, ImageRole.PRIMARY),
        *[candidate(n, ImageRole.UNKNOWN) for n in range(1, 9)],
    ]
    plan = plan_image_sample(pool, 6, rules=RULES)
    assert [c.role for c in plan.selected] == [ImageRole.PRIMARY]


def test_thumbnail_and_auxiliary_images_only_fill_what_is_left() -> None:
    pool = [
        candidate(0, ImageRole.THUMBNAIL),
        candidate(1, ImageRole.PRODUCT_AUX),
        candidate(2, ImageRole.PRIMARY),
        candidate(3, ImageRole.DETAIL),
        candidate(4, ImageRole.UI_COMMON),
    ]
    plan = plan_image_sample(pool, 6, rules=RULES)
    assert [c.role for c in plan.selected] == [
        ImageRole.PRIMARY,
        ImageRole.DETAIL,
        ImageRole.THUMBNAIL,
        ImageRole.PRODUCT_AUX,
    ], "product evidence first, then whatever the cap leaves"
    assert ImageRole.UI_COMMON not in {c.role for c in plan.selected}


def test_only_one_primary_is_sampled_however_many_the_page_declares() -> None:
    pool = [candidate(n, ImageRole.PRIMARY, path=f"/big/{n}.jpg") for n in range(4)]
    plan = plan_image_sample(pool, 6, rules=RULES)
    assert [c.order for c in plan.selected] == [0]
    assert [reason for _, reason in plan.excluded] == ["lower sampling priority"] * 3


def test_the_same_asset_written_twice_consumes_one_slot() -> None:
    # The page declares its representative image twice, once with a cache-busting query.
    pool = [
        ImageCandidate("https://h.invalid/big/1.jpg", ImageRole.PRIMARY, 0, "r.og"),
        ImageCandidate("https://h.invalid/big/1.jpg?v=2#top", ImageRole.PRIMARY, 1, "r.key"),
        *[candidate(2 + n, ImageRole.DETAIL) for n in range(3)],
    ]
    plan = plan_image_sample(pool, 6, rules=RULES)
    assert [c.order for c in plan.selected] == [0, 2, 3, 4]
    assert ("duplicate normalized URL") in {reason for _, reason in plan.excluded}


def test_the_cap_is_never_widened() -> None:
    pool = [candidate(n, ImageRole.DETAIL) for n in range(30)]
    assert len(plan_image_sample(pool, 6, rules=RULES).selected) == 5, "detail is capped at five"
    assert plan_image_sample(pool, 0, rules=RULES).selected == ()
    with pytest.raises(ValueError, match="not negative"):
        plan_image_sample(pool, -1, rules=RULES)


def test_the_audit_explains_every_choice_and_carries_no_token() -> None:
    pool = [
        ImageCandidate("https://h.invalid/big/12/34.jpg?sig=SECRET", ImageRole.PRIMARY, 0, "r.key"),
        candidate(1, ImageRole.DETAIL),
        *[candidate(2 + n, ImageRole.UI_COMMON) for n in range(3)],
    ]
    audit = plan_image_sample(pool, 6, rules=RULES).audit()
    assert audit["rules"] == RULES and audit["limit"] == 6
    chosen = audit["selected"]
    assert [entry["role"] for entry in chosen] == ["PRIMARY", "DETAIL"]
    first = chosen[0]
    assert set(first) == {"role", "host", "order", "identity", "path_form", "rule", "reason"}
    assert first["path_form"] == "/big/{n}/{n}.jpg", "digit runs are masked"
    assert first["rule"] == "r.key" and "slot 1 of 6" in first["reason"]
    assert "SECRET" not in str(audit), "a query token never reaches the findings"
    assert audit["not_selected"] == [
        {"role": "UI_COMMON", "host": "img.example.invalid", "reason": "UI_COMMON", "count": 3}
    ]
    assert audit["reconnaissance_only"] == RECONNAISSANCE_ONLY
    assert "not a publication" in RECONNAISSANCE_ONLY
