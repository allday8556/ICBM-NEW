"""The shipping presets (ADR-0033 §11): editable example contents, never used until saved.

Each preset is a top notice and a bottom notice, valid under §1, and suggests its matching
template. A preset only fills the operator's inputs: it is validated, rendered and saved exactly as
typed text is, and what is saved is the operator's text (DG-08). Seller-specific values are the
visible placeholder ``○○``. The overseas preset states no legal threshold or amount.
"""

from dataclasses import dataclass
from typing import Any, Final

from app.capabilities.detail_guidance.renderer import Template

PRESETS_VERSION: Final = "guidance-presets/v1"
PLACEHOLDER: Final = "○○"


@dataclass(frozen=True)
class GuidancePreset:
    key: str
    label: str
    template: Template
    top: dict[str, Any]
    bottom: dict[str, Any]


PRESETS: Final[tuple[GuidancePreset, ...]] = (
    GuidancePreset(
        key="DOMESTIC",
        label="국내배송",
        template=Template.DOMESTIC,
        top={
            "blocks": [
                {
                    "heading": "당일발송 안내",
                    "lines": ["오후 ○○시 이전 결제 시 당일 발송", "(주말·공휴일 제외)"],
                }
            ]
        },
        bottom={
            "blocks": [
                {
                    "heading": "배송 안내",
                    "lines": [
                        "○○택배로 발송됩니다",
                        "결제 후 ○○일 이내 출고됩니다",
                        "도서산간 지역은 추가 배송비가 있습니다",
                    ],
                },
                {
                    "heading": "교환·반품 안내",
                    "lines": [
                        "상품 수령 후 7일 이내 신청해 주세요",
                        "단순변심 반품은 왕복 배송비가 듭니다",
                    ],
                },
                {
                    "heading": "C/S 안내",
                    "lines": ["평일 ○○:00 ~ ○○:00 (주말 휴무)", "문의는 톡톡으로 남겨 주세요"],
                },
            ]
        },
    ),
    GuidancePreset(
        key="OVERSEAS",
        label="해외배송",
        template=Template.OVERSEAS,
        top={
            "blocks": [
                {
                    "heading": "해외배송 상품 안내",
                    "lines": [
                        "해외에서 직접 배송되는 상품입니다",
                        "받으시기까지 영업일 기준 ○○~○○일",
                    ],
                }
            ]
        },
        bottom={
            "blocks": [
                {
                    "heading": "해외배송 안내",
                    "lines": [
                        "주문 후 해외 현지에서 발송됩니다",
                        "현지 사정으로 배송이 늦어질 수 있습니다",
                    ],
                },
                {
                    "heading": "통관·관부가세 안내",
                    "lines": [
                        "통관을 위해 개인통관고유부호가 필요합니다",
                        "면세 한도를 넘으면 관부가세가 생깁니다",
                        "관부가세 부담: ○○",
                    ],
                },
                {
                    "heading": "교환·반품 안내",
                    "lines": [
                        "해외 발송 후에는 취소가 어려울 수 있습니다",
                        "단순변심 반품은 국제 왕복 배송비가 듭니다",
                    ],
                },
            ]
        },
    ),
)
