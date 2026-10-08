"""The layered prompt registry of the v29 prototype, as a catalog (ADR-0026 §3).

The prototype (`design/prototypes/icbm_redesign_test_v29_final.html`) defines the registry the
owner planned: one global rule set, six roles, four platform policies and fifteen tasks. This
catalog names each entry, its layer, its display title and, for a task, the screen that calls it
and the role it composes with — exactly as the prototype pairs them. It holds no prompt text:
every prompt is read from the PromptTemplate or PlatformPolicy store, whose revision 1 is the
prototype's text (``seed_v29.json``, written by the application at startup).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Layer(StrEnum):
    GLOBAL = "GLOBAL"
    ROLE = "ROLE"
    POLICY = "POLICY"
    TASK = "TASK"


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    layer: Layer
    title: str
    description: str | None = None
    # A task only: the screen whose button calls it, and the role it composes with. A task no
    # prototype button calls has neither.
    screen: str | None = None
    role_key: str | None = None


GLOBAL_KEY: Final = "ICBM_GLOBAL_RULES_V2"
# The policy of a task whose marketplace is not decided yet (the prototype's default).
COMMON_POLICY_KEY: Final = "POLICY_COMMON_MARKET_V1"
# The platform policy of each marketplace the registry has one for (the prototype's three).
POLICY_BY_MARKETPLACE: Final = {
    "smartstore": "POLICY_NAVER_V1",
    "coupang": "POLICY_COUPANG_V1",
    "st11": "POLICY_11ST_V1",
}

CATALOG: Final[tuple[CatalogEntry, ...]] = (
    CatalogEntry(
        GLOBAL_KEY, Layer.GLOBAL, "공통 규칙", "모든 AI 기능에 공통 적용되는 사실성·검증·승인 정책"
    ),
    CatalogEntry(
        "ROLE_DATA_VALIDATOR_V1", Layer.ROLE, "수집 검증 AI", "DOM·가격·배송비·옵션 근거 검증"
    ),
    CatalogEntry("ROLE_PRODUCT_MD_V1", Layer.ROLE, "상품/MD AI", "상품명·태그·카테고리·옵션 정리"),
    CatalogEntry(
        "ROLE_COMMERCE_OPS_V1", Layer.ROLE, "커머스 운영 AI", "주문 상태·다음 처리·운영 안내"
    ),
    CatalogEntry("ROLE_CS_V1", Layer.ROLE, "CS 응대 AI", "문의 분석·고객 답변 초안"),
    CatalogEntry(
        "ROLE_STOCK_VALIDATOR_V1", Layer.ROLE, "판매상태 검증 AI", "BUY/CART·품절 근거 교차검증"
    ),
    CatalogEntry("ROLE_SHOPPING_INSIGHT_V1", Layer.ROLE, "상품기획·마케팅 AI"),
    CatalogEntry(
        "POLICY_NAVER_V1",
        Layer.POLICY,
        "스마트스토어 정책",
        "추천/제외 태그 ID · SEO · 공식 데이터 우선",
    ),
    CatalogEntry(
        "POLICY_COUPANG_V1", Layer.POLICY, "쿠팡 정책", "카테고리 추천 → AI 재검증 · 필수 옵션 확인"
    ),
    CatalogEntry(
        "POLICY_11ST_V1", Layer.POLICY, "11번가 정책", "실제 카테고리 ID 범위 · 등록 정책"
    ),
    CatalogEntry(COMMON_POLICY_KEY, Layer.POLICY, "공통 마켓 정책"),
    CatalogEntry(
        "TASK_EXTRACT_CORRECT_V1",
        Layer.TASK,
        "추출 오류/누락 보정",
        screen="수집관리",
        role_key="ROLE_DATA_VALIDATOR_V1",
    ),
    CatalogEntry(
        "TASK_PRODUCT_RECOMMEND_BUNDLE_V1",
        Layer.TASK,
        "통합DB 상품 추천",
        screen="통합DB",
        role_key="ROLE_PRODUCT_MD_V1",
    ),
    CatalogEntry(
        "TASK_CATEGORY_REMATCH_V1",
        Layer.TASK,
        "플랫폼 카테고리 재추천",
        screen="등록관리",
        role_key="ROLE_PRODUCT_MD_V1",
    ),
    CatalogEntry(
        "TASK_ORDER_RESPONSE_ASSIST_V1",
        Layer.TASK,
        "주문 운영 보조",
        screen="주문관리",
        role_key="ROLE_COMMERCE_OPS_V1",
    ),
    CatalogEntry(
        "TASK_INQUIRY_REPLY_V1",
        Layer.TASK,
        "고객 문의 답변",
        screen="문의관리",
        role_key="ROLE_CS_V1",
    ),
    CatalogEntry(
        "TASK_SOLDOUT_JUDGMENT_V1",
        Layer.TASK,
        "품절 판정 보정",
        screen="품절확인",
        role_key="ROLE_STOCK_VALIDATOR_V1",
    ),
    CatalogEntry(
        "TASK_TREND_ANALYSIS_V1",
        Layer.TASK,
        "쇼핑 트렌드 분석",
        screen="AI 인사이트",
        role_key="ROLE_SHOPPING_INSIGHT_V1",
    ),
    CatalogEntry("TASK_PRODUCT_RECOMMEND_V1", Layer.TASK, "추천 아이템 발굴"),
    CatalogEntry(
        "TASK_PRODUCT_NAME_OPTIMIZE_V1",
        Layer.TASK,
        "급상승 키워드 기반 상품명 최적화",
        screen="AI 인사이트",
        role_key="ROLE_SHOPPING_INSIGHT_V1",
    ),
    CatalogEntry("TASK_TAG_RECOMMEND_V1", Layer.TASK, "검색/트렌드 태그 추천"),
    CatalogEntry(
        "TASK_SOURCING_REVIEW_V1",
        Layer.TASK,
        "소싱 검토",
        screen="AI 인사이트",
        role_key="ROLE_SHOPPING_INSIGHT_V1",
    ),
    CatalogEntry(
        "TASK_REGISTRATION_CANDIDATE_V1",
        Layer.TASK,
        "등록 후보 선별",
        screen="AI 인사이트",
        role_key="ROLE_SHOPPING_INSIGHT_V1",
    ),
    CatalogEntry(
        "TASK_PLATFORM_COMPARE_V1",
        Layer.TASK,
        "플랫폼별 트렌드 비교",
        screen="AI 인사이트",
        role_key="ROLE_SHOPPING_INSIGHT_V1",
    ),
    CatalogEntry(
        "TASK_BROADCAST_OPPORTUNITY_V1",
        Layer.TASK,
        "홈쇼핑 방송 연계 기회",
        screen="AI 인사이트",
        role_key="ROLE_SHOPPING_INSIGHT_V1",
    ),
    CatalogEntry(
        "TASK_COMPETITION_FILTER_V1",
        Layer.TASK,
        "경쟁강도 필터",
        screen="AI 인사이트",
        role_key="ROLE_SHOPPING_INSIGHT_V1",
    ),
)

BY_KEY: Final = {entry.key: entry for entry in CATALOG}

# The fields a revision's content holds, per layer. A task carries the prototype's four: its mode,
# its prompt, its input variables and its output schema; every other layer holds one prompt.
CONTENT_FIELDS: Final = {
    Layer.GLOBAL: ("prompt",),
    Layer.ROLE: ("prompt",),
    Layer.POLICY: ("prompt",),
    Layer.TASK: ("mode", "prompt", "variables", "output"),
}
# What an operator edits: the mode names the task and is never edited (the prototype has no tab).
EDITABLE_FIELDS: Final = {
    Layer.GLOBAL: ("prompt",),
    Layer.ROLE: ("prompt",),
    Layer.POLICY: ("prompt",),
    Layer.TASK: ("prompt", "variables", "output"),
}
