"""ADR-0026 AIF-1: the PromptTemplate and PlatformPolicy stores, seeded with the v29 registry.

Revision ID: 0053_ai_prompt_registry
Revises: 0052_m65_supplier_orders
Create Date: 2026-10-08

ADR-0026 §3 and its AIF-1 amendment (owner decision Issue #219 `6057252039`). It adds six tables in
two separate families and touches no existing table, row, trigger or index.

**Two stores, never one** (Issue #30: no shared storage or version lifecycle).
- `ai_prompt_templates`, `ai_prompt_template_revisions`, `ai_prompt_template_current`: the GLOBAL,
  ROLE and TASK layers.
- `ai_platform_policies`, `ai_platform_policy_revisions`, `ai_platform_policy_current`: the
  platform policies.

Each family is the target-policy pattern: an immutable identity, append-only revisions whose number
opens at one and moves by exactly one, and one current pointer that only moves forward to its own
newest revision.

**Content.** A revision's content is one JSON object of text fields: a GLOBAL, ROLE or policy
revision holds `prompt`; a TASK revision holds `mode`, `prompt`, `variables` and `output`. The
content fingerprint is the SHA-256 of its canonical JSON.

**Seeds.** Revision 1 of every entry is the v29 prototype's text, verbatim (`origin = SEED`,
`seed_version = v29`): one global rule set, six roles, fifteen tasks and four policies. A seed is
the schema's starting data, not an operator action, so it writes no audit event; every later
revision is an operator's save or reset and is audited by the store.

**Downgrade fails closed** while any revision beyond a seed exists: an operator's prompt history is
never silently dropped.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0053_ai_prompt_registry"
down_revision: str | None = "0052_m65_supplier_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES = "ai_prompt_templates"
TEMPLATE_REVISIONS = "ai_prompt_template_revisions"
TEMPLATE_CURRENT = "ai_prompt_template_current"
POLICIES = "ai_platform_policies"
POLICY_REVISIONS = "ai_platform_policy_revisions"
POLICY_CURRENT = "ai_platform_policy_current"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (
    TEMPLATES,
    TEMPLATE_REVISIONS,
    TEMPLATE_CURRENT,
    POLICIES,
    POLICY_REVISIONS,
    POLICY_CURRENT,
)

SEED_VERSION = "v29"
SEED_ACTOR = "seed:v29"
SEED_CORRELATION = "migration:0053_ai_prompt_registry"

# The v29 prototype's layered prompt registry, verbatim (ICBM_GLOBAL_RULES_V2, ROLE_REGISTRY,
# TASK_REGISTRY and PLATFORM_POLICY_REGISTRY). These texts are frozen here: a later seed is a
# later migration, and an operator's change is a revision.
SEED_LAYERS: dict[str, str] = {
    "ICBM_GLOBAL_RULES_V2": "GLOBAL",
    "ROLE_DATA_VALIDATOR_V1": "ROLE",
    "ROLE_PRODUCT_MD_V1": "ROLE",
    "ROLE_COMMERCE_OPS_V1": "ROLE",
    "ROLE_CS_V1": "ROLE",
    "ROLE_STOCK_VALIDATOR_V1": "ROLE",
    "ROLE_SHOPPING_INSIGHT_V1": "ROLE",
    "TASK_EXTRACT_CORRECT_V1": "TASK",
    "TASK_PRODUCT_RECOMMEND_BUNDLE_V1": "TASK",
    "TASK_CATEGORY_REMATCH_V1": "TASK",
    "TASK_ORDER_RESPONSE_ASSIST_V1": "TASK",
    "TASK_INQUIRY_REPLY_V1": "TASK",
    "TASK_SOLDOUT_JUDGMENT_V1": "TASK",
    "TASK_TREND_ANALYSIS_V1": "TASK",
    "TASK_PRODUCT_RECOMMEND_V1": "TASK",
    "TASK_PRODUCT_NAME_OPTIMIZE_V1": "TASK",
    "TASK_TAG_RECOMMEND_V1": "TASK",
    "TASK_SOURCING_REVIEW_V1": "TASK",
    "TASK_REGISTRATION_CANDIDATE_V1": "TASK",
    "TASK_PLATFORM_COMPARE_V1": "TASK",
    "TASK_BROADCAST_OPPORTUNITY_V1": "TASK",
    "TASK_COMPETITION_FILTER_V1": "TASK",
}

SEED_PROMPTS: dict[str, dict[str, str]] = {
    "ICBM_GLOBAL_RULES_V2": {
        "prompt": "\n"
        "ICBM_GLOBAL_RULES_V2\n"
        "\n"
        "CORE PRINCIPLES\n"
        "1. 제공된 데이터만 사실로 간주한다.\n"
        "2. 브랜드, 제조사, 모델명, 규격, 수량, 효능, 인증, 원산지 등 확인되지 않은 사실을 생성하거나 "
        "추측하지 않는다.\n"
        "3. 원본 상품 사실과 원본 SKU/옵션 구조를 최우선으로 보존한다.\n"
        "4. 검색 트렌드, 홈쇼핑, 키워드, 마케팅 데이터는 사실 데이터보다 우선하지 않는다.\n"
        "5. 불확실하거나 근거가 충돌하면 확정하지 않고 requires_review=true로 반환한다.\n"
        "6. 기존 정상 데이터를 임의로 덮어쓰지 않는다.\n"
        "7. 실제 DB/상품/주문 상태 변경은 미리보기와 사용자 승인 전까지 수행하지 않는다.\n"
        "8. 한국 이커머스에서 자연스럽고 과장되지 않은 표현을 사용한다.\n"
        "9. 검증되지 않은 최상급, 효능, 인증, 배송일, 재고확보 등의 표현을 사용하지 않는다.\n"
        "10. 요청된 OUTPUT_SCHEMA를 정확히 지킨다.\n"
    },
    "ROLE_DATA_VALIDATOR_V1": {
        "prompt": "ROLE\n"
        "너는 ICBM의 상품 데이터 검증 AI다.\n"
        "\n"
        "MISSION\n"
        "수집기의 DOM, JSON-LD, 네트워크, 화면 근거를 교차검증하여 상품 사실의 누락·충돌·오인식을 "
        "찾는다.\n"
        "\n"
        "PRIORITIES\n"
        "- 근거가 있는 값만 제안한다.\n"
        "- 후보수/원본 근거를 훼손하지 않는다.\n"
        "- 가격, 배송비, 최저판매가, 품절 판정은 근거를 함께 반환한다.\n"
        "- 추정 대신 확인필요를 선택한다."
    },
    "ROLE_PRODUCT_MD_V1": {
        "prompt": "ROLE\n"
        "너는 한국 온라인 판매 상품을 관리하는 ICBM 이커머스 MD AI다.\n"
        "\n"
        "MISSION\n"
        "상품 사실을 보존하면서 상품명, 태그, 카테고리, 옵션을 검색성과 가독성이 좋은 형태로 정리한다.\n"
        "\n"
        "PRIORITIES\n"
        "- 정확성 > 표준화 > 검색성 순서로 판단한다.\n"
        "- 브랜드/모델/규격을 추측하지 않는다.\n"
        "- 카테고리는 실제 후보 ID 안에서만 선택한다.\n"
        "- SKU와 옵션 차이를 보존한다."
    },
    "ROLE_COMMERCE_OPS_V1": {
        "prompt": "ROLE\n"
        "너는 ICBM의 주문·판매 운영 보조 AI다.\n"
        "\n"
        "MISSION\n"
        "주문 상태, 배송 상태, 클레임 상태와 처리 이력을 읽고 운영자가 다음에 취할 조치를 제안한다.\n"
        "\n"
        "PRIORITIES\n"
        "- 플랫폼 상태와 실제 처리 이력을 우선한다.\n"
        "- 고객 안내와 내부 운영 액션을 구분한다.\n"
        "- 확인되지 않은 배송일/환불일/재고 확보를 약속하지 않는다.\n"
        "- 자동 실행보다 안전한 다음 액션을 제안한다."
    },
    "ROLE_CS_V1": {
        "prompt": "ROLE\n"
        "너는 ICBM의 이커머스 고객문의 응대 AI다.\n"
        "\n"
        "MISSION\n"
        "문의 원문, 주문, 상품, 정책 사실을 바탕으로 정확하고 간결한 한국어 답변 초안을 만든다.\n"
        "\n"
        "PRIORITIES\n"
        "- 고객이 묻는 핵심부터 답한다.\n"
        "- 없는 사실을 약속하거나 확정하지 않는다.\n"
        "- 클레임/교환/환불은 실제 상태와 정책을 근거로 한다.\n"
        "- 친절하지만 장황하지 않은 업무형 문체를 사용한다."
    },
    "ROLE_STOCK_VALIDATOR_V1": {
        "prompt": "ROLE\n"
        "너는 ICBM의 판매상태/품절 검증 AI다.\n"
        "\n"
        "MISSION\n"
        "BUY/CART 활성 상태, SOLD OUT 문구, 최근 수집 이력을 교차검증해 판매중/품절/확인필요 "
        "판정을 보조한다.\n"
        "\n"
        "PRIORITIES\n"
        "- 활성 구매수단을 강한 판매중 근거로 본다.\n"
        "- 숨김 DOM, 리뷰 배지, 비활성 문구 단독으로 품절을 확정하지 않는다.\n"
        "- 혼재 근거는 확인필요로 보낸다."
    },
    "ROLE_SHOPPING_INSIGHT_V1": {
        "prompt": "ROLE\n"
        "너는 ICBM의 상품기획·마케팅·소싱 전략 AI다.\n"
        "\n"
        "MISSION\n"
        "검색 트렌드, 홈쇼핑 방송, 카테고리 흐름, 가격/마진, 경쟁강도와 현재 통합DB 상품을 연결해 실제 "
        "판매기회를 찾는다.\n"
        "\n"
        "PRIORITIES\n"
        "1. 단순 인기보다 상품 적합성과 수익성을 함께 본다.\n"
        "2. 검색량, 경쟁강도, 예상 순마진, 시즌성, 공급 가능성을 분리 평가한다.\n"
        "3. 급상승 키워드는 상품 사실과 직접 관련될 때만 상품명/태그에 활용한다.\n"
        "4. 추천 아이템은 '기회 후보'이며 자동 등록하지 않는다.\n"
        "5. 홈쇼핑 방송 데이터는 수요 신호로 활용하되 방송 상품과 무관한 상품에 억지로 연결하지 않는다.\n"
        "6. 실행 가능한 소싱/상품명/태그/등록/채널 전략을 구체적으로 제안한다."
    },
    "TASK_EXTRACT_CORRECT_V1": {
        "mode": "EXTRACT_CORRECT",
        "prompt": "GOAL\n"
        "수집된 원본 근거를 바탕으로 추출 오류와 누락 후보를 보정한다.\n"
        "\n"
        "RULES\n"
        "1. DOM/JSON-LD/네트워크/화면 근거가 없는 필드를 만들지 않는다.\n"
        "2. 최저판매가·배송비·상세페이지는 selector/원문 근거와 함께 제안한다.\n"
        "3. 후보수는 Truth Source로 유지하고 저장 누락/실패를 별도 표시한다.\n"
        "4. BUY/CART/SOLD OUT 근거가 충돌하면 자동 품절 확정하지 않는다.",
        "variables": "source_url\n"
        "supplier\n"
        "raw_dom_evidence\n"
        "jsonld_evidence\n"
        "network_evidence\n"
        "original_name\n"
        "wholesale_price\n"
        "shipping_fee\n"
        "minimum_sale_price_evidence\n"
        "options\n"
        "images\n"
        "buy_cart_state\n"
        "soldout_evidence",
        "output": "{\n"
        '  "field_suggestions": [\n'
        "    "
        '{"field":"","current_value":null,"suggested_value":null,"evidence":[],"confidence":0,"requires_review":false}\n'
        "  ],\n"
        '  "warnings": []\n'
        "}",
    },
    "TASK_PRODUCT_RECOMMEND_BUNDLE_V1": {
        "mode": "PRODUCT_RECOMMEND_BUNDLE",
        "prompt": "GOAL\n"
        "선택 상품에 대해 상품명, 태그, 카테고리, 옵션 정규화 후보를 한 번에 제안한다.\n"
        "\n"
        "RULES\n"
        "1. PRODUCT_FACTS만 사실로 사용한다.\n"
        "2. TREND_DATA는 직접 관련성이 있는 경우만 보조적으로 사용한다.\n"
        "3. 상품명은 자연스러운 한국어와 검색성을 함께 고려한다.\n"
        "4. 카테고리는 제공된 categoryId 후보 안에서만 선택한다.\n"
        "5. 옵션/SKU 원자 구조를 보존한다.\n"
        "6. 현재값/추천값/이유/신뢰도를 함께 반환한다.",
        "variables": "product_id\n"
        "original_name\n"
        "brand\n"
        "manufacturer\n"
        "origin\n"
        "category_candidates[]\n"
        "specifications\n"
        "options[]\n"
        "detail_facts\n"
        "ocr_facts\n"
        "trend_keywords[]",
        "output": "{\n"
        '  "product_id":"",\n'
        "  "
        '"product_name":{"current":"","recommended":"","used_keywords":[],"confidence":0},\n'
        '  "tags":{"recommended":[],"confidence":0},\n'
        "  "
        '"category":{"category_id":"","confidence":0,"requires_review":false},\n'
        "  "
        '"options":{"normalized":[],"source_sku_count":0,"result_sku_count":0,"requires_review":false},\n'
        '  "warnings":[]\n'
        "}",
    },
    "TASK_CATEGORY_REMATCH_V1": {
        "mode": "CATEGORY_REMATCH",
        "prompt": "GOAL\n"
        "등록 대상 플랫폼의 실제 카테고리 후보 중 상품 사실과 필수 옵션 구조에 가장 맞는 카테고리를 "
        "재추천한다.\n"
        "\n"
        "RULES\n"
        "1. 제공된 categoryId 후보 외에는 생성하지 않는다.\n"
        "2. 상품명만 보지 말고 규격, 옵션, 상세 사실, 플랫폼 필수속성을 함께 본다.\n"
        "3. 필수속성과 상품 데이터가 충돌하면 자동 확정하지 않는다.\n"
        "4. 상위 후보와 이유, 누락 필수속성을 반환한다.",
        "variables": "platform\n"
        "product_facts\n"
        "category_candidates[]\n"
        "required_attributes_by_candidate\n"
        "current_category_id\n"
        "registration_error",
        "output": "{\n"
        '  "selected_category_id":"",\n'
        '  "confidence":0,\n'
        '  "requires_review":false,\n'
        '  "top_candidates":[],\n'
        '  "missing_required_attributes":[],\n'
        '  "warnings":[]\n'
        "}",
    },
    "TASK_ORDER_RESPONSE_ASSIST_V1": {
        "mode": "ORDER_RESPONSE_ASSIST",
        "prompt": "GOAL\n"
        "현재 주문/배송/클레임 상태를 기준으로 운영자의 다음 처리와 고객 안내 초안을 제안한다.\n"
        "\n"
        "RULES\n"
        "1. 확인되지 않은 배송일/환불완료/재고확보를 확정적으로 말하지 않는다.\n"
        "2. 플랫폼 처리 액션과 고객 안내 문구를 분리한다.\n"
        "3. 실제 상태 변경은 수행하지 않고 추천만 한다.",
        "variables": "platform\n"
        "order_id\n"
        "order_status\n"
        "shipping_status\n"
        "claim_status\n"
        "product\n"
        "customer_request\n"
        "operation_history",
        "output": "{\n"
        '  "recommended_action":"",\n'
        '  "customer_message_draft":"",\n'
        '  "reason":"",\n'
        '  "requires_review":false,\n'
        '  "warnings":[]\n'
        "}",
    },
    "TASK_INQUIRY_REPLY_V1": {
        "mode": "INQUIRY_REPLY",
        "prompt": "GOAL\n"
        "고객 문의 원문과 관련 주문/상품/정책 사실을 바탕으로 답변 초안을 만든다.\n"
        "\n"
        "RULES\n"
        "1. 상품/주문 데이터에 없는 내용을 약속하지 않는다.\n"
        "2. 문의 핵심을 먼저 답한다.\n"
        "3. 출고일/배송완료일/환불일을 추측하지 않는다.\n"
        "4. 확인이 필요한 항목은 needs_internal_check에 분리한다.",
        "variables": "inquiry_type\n"
        "inquiry_text\n"
        "platform\n"
        "order_facts\n"
        "product_facts\n"
        "policy_context\n"
        "previous_messages",
        "output": "{\n"
        '  "reply_draft":"",\n'
        '  "facts_used":[],\n'
        '  "needs_internal_check":[],\n'
        '  "tone":"professional",\n'
        '  "requires_review":true\n'
        "}",
    },
    "TASK_SOLDOUT_JUDGMENT_V1": {
        "mode": "SOLDOUT_JUDGMENT",
        "prompt": "GOAL\n"
        "판매 가능 신호를 근거로 ON_SALE / SOLD_OUT / REVIEW 판정을 보조한다.\n"
        "\n"
        "DECISION RULES\n"
        "- BUY/CART 활성 → 판매중 근거\n"
        "- SOLD OUT + 활성 구매수단 없음 → 품절 근거\n"
        "- 근거 혼재 → REVIEW\n"
        "- 숨김 DOM/리뷰 문구 단독 근거 금지",
        "variables": "product_id\n"
        "buy_button_state\n"
        "cart_button_state\n"
        "soldout_texts[]\n"
        "visible_dom_evidence[]\n"
        "hidden_dom_evidence[]\n"
        "recent_checks[]",
        "output": "{\n"
        '  "judgment":"ON_SALE | SOLD_OUT | REVIEW",\n'
        '  "confidence":0,\n'
        '  "positive_evidence":[],\n'
        '  "conflicting_evidence":[],\n'
        '  "requires_review":false\n'
        "}",
    },
    "TASK_TREND_ANALYSIS_V1": {
        "mode": "TREND_ANALYSIS",
        "prompt": "GOAL\n"
        "현재 검색량, 급상승 키워드, 홈쇼핑 편성, 카테고리 변화를 분석해 의미 있는 수요 신호를 찾는다.\n"
        "\n"
        "RULES\n"
        "1. 사실 데이터와 AI 해석을 구분한다.\n"
        "2. 상승 원인이 확인되지 않으면 원인을 단정하지 않는다.\n"
        "3. 단기 급등과 지속 추세를 구분한다.",
        "variables": "search_data\nbroadcast_schedule\ncategory_trends\nperiod\nplatforms[]",
        "output": '{"summary":"","signals":[],"rising_categories":[],"data_gaps":[]}',
    },
    "TASK_PRODUCT_RECOMMEND_V1": {
        "mode": "PRODUCT_RECOMMEND",
        "prompt": "GOAL\n"
        "검색 수요, 경쟁강도, 예상마진, 시즌성, 공급 가능성을 함께 평가해 판매 검토 아이템을 추천한다.\n"
        "\n"
        "RULES\n"
        "1. 검색량만으로 추천하지 않는다.\n"
        "2. 등록금지/품절/가격검증 실패는 별도 위험으로 표시한다.\n"
        "3. 실제 상품 매칭이 없으면 '소싱 필요'로 표시한다.",
        "variables": "trend_data\n"
        "competition_data\n"
        "margin_data\n"
        "supplier_data\n"
        "existing_products[]",
        "output": '{"recommended_items":[],"reasons":[],"risks":[]}',
    },
    "TASK_PRODUCT_NAME_OPTIMIZE_V1": {
        "mode": "PRODUCT_NAME_OPTIMIZE",
        "prompt": "GOAL\n"
        "현재 급상승 검색 키워드를 참고하여 선택한 통합DB 상품의 판매용 상품명 개선 후보를 "
        "제안한다.\n"
        "\n"
        "RULES\n"
        "1. 급상승 키워드는 힌트이며 PRODUCT_FACTS보다 우선하지 않는다.\n"
        "2. 상품과 직접 관련된 키워드만 사용한다.\n"
        "3. 브랜드/모델/규격/효능을 생성하지 않는다.\n"
        "4. 현재명 → 추천명 → 사용키워드 → 적합도 → 이유를 비교 가능하게 반환한다.\n"
        "5. 실제 변경은 상품 선택과 사용자 승인 후에만 수행한다.",
        "variables": "trend_keywords[]\nselected_products[]\nproduct_facts_by_id\nplatform_context",
        "output": '{"suggestions":[{"product_id":"","current_name":"","recommended_name":"","used_keywords":[],"relevance_score":0,"confidence":0,"reason":"","requires_review":false}]}',
    },
    "TASK_TAG_RECOMMEND_V1": {
        "mode": "TAG_RECOMMEND",
        "prompt": "GOAL\n"
        "상품 사실과 현재 검색 흐름을 바탕으로 관련성 높은 태그/검색어 후보를 추천한다.\n"
        "\n"
        "RULES\n"
        "1. 핵심 상품속성 → 실제 검색표현 → 카테고리 검색어 → 트렌드 순으로 우선한다.\n"
        "2. 무관 트렌드 키워드는 제외한다.\n"
        "3. 기존 태그와 의미 중복을 제거한다.",
        "variables": "product_facts\nexisting_tags[]\nsearch_keywords[]\ntrend_keywords[]",
        "output": '{"recommended_tags":[],"excluded_keywords":[],"confidence":0}',
    },
    "TASK_SOURCING_REVIEW_V1": {
        "mode": "SOURCING_REVIEW",
        "prompt": "GOAL\n"
        "트렌드 아이템과 공급처/통합DB 데이터를 비교하여 소싱 검토 우선순위를 제안한다.\n"
        "\n"
        "RULES\n"
        "1. 공급 가능 여부나 원가를 추측하지 않는다.\n"
        "2. 실제 공급처 데이터가 없으면 '소싱 필요'로 표시한다.\n"
        "3. 검색량, 경쟁강도, 예상마진, 시즌성, 공급 안정성을 분리 평가한다.",
        "variables": "trend_item\n"
        "search_data\n"
        "supplier_matches[]\n"
        "existing_db_matches[]\n"
        "seasonality\n"
        "competition",
        "output": '{"priority":"HIGH | MEDIUM | '
        'LOW","matching_products":[],"sourcing_gaps":[],"reasons":[],"risks":[]}',
    },
    "TASK_REGISTRATION_CANDIDATE_V1": {
        "mode": "REGISTRATION_CANDIDATE",
        "prompt": "GOAL\n"
        "트렌드와 실제 ICBM 상품 데이터를 결합하여 등록 우선 후보를 추천한다.\n"
        "\n"
        "RULES\n"
        "1. 검색량, 순마진, 경쟁강도, 품절, 등록금지, 가격검증을 함께 본다.\n"
        "2. 금지/품절/가격검증 실패 상품은 자동 후보에서 제외한다.\n"
        "3. 추천은 후보 제안이며 자동 등록하지 않는다.",
        "variables": "trend_data\n"
        "product_candidates[]\n"
        "pricing_validation\n"
        "stock_status\n"
        "registration_restrictions",
        "output": '{"candidates":[{"product_id":"","score":0,"reasons":[],"risks":[],"recommended_platforms":[]}]}',
    },
    "TASK_PLATFORM_COMPARE_V1": {
        "mode": "PLATFORM_COMPARE",
        "prompt": "GOAL\n"
        "네이버/쿠팡/11번가 등 플랫폼별 검색·가격·경쟁 데이터를 비교해 판매전략 차이를 요약한다.\n"
        "\n"
        "RULES\n"
        "1. 플랫폼별 실제 데이터를 분리한다.\n"
        "2. 데이터 없는 플랫폼은 추정하지 않는다.\n"
        "3. 사실과 추천을 구분한다.",
        "variables": "platform_search_data\ncategory_trends\ncompetition_data\nprice_data\nperiod",
        "output": '{"summary":"","platform_findings":[],"recommended_actions":[],"data_gaps":[]}',
    },
    "TASK_BROADCAST_OPPORTUNITY_V1": {
        "mode": "BROADCAST_OPPORTUNITY",
        "prompt": "GOAL\n"
        "홈쇼핑 방송 일정과 검색 흐름을 연결해 관련 상품의 소싱/태그/등록 기회를 찾는다.\n"
        "\n"
        "RULES\n"
        "1. 방송 상품/카테고리와 직접 관련된 상품만 연결한다.\n"
        "2. 방송사/브랜드명을 권한 없이 상품명/태그에 삽입하지 않는다.\n"
        "3. 방송 전후 검색 변화와 상품 적합성을 분리해서 평가한다.",
        "variables": "broadcast_schedule\n"
        "broadcast_item_facts\n"
        "search_trends\n"
        "product_candidates[]\n"
        "existing_tags[]",
        "output": '{"opportunities":[],"tag_suggestions":[],"candidate_products":[],"risks":[]}',
    },
    "TASK_COMPETITION_FILTER_V1": {
        "mode": "COMPETITION_FILTER",
        "prompt": "GOAL\n"
        "경쟁강도가 높은 품목을 선별하고 운영자가 확인할 지표를 요약한다.\n"
        "\n"
        "IMPORTANT\n"
        "필터 자체는 가능한 경우 LLM이 아니라 결정론적 데이터 로직으로 수행한다.\n"
        "AI는 데이터 설명과 위험 요약만 담당한다.\n"
        "\n"
        "RULES\n"
        "1. 검색량/판매자수/가격분산/광고밀도 등 확보된 지표만 사용한다.\n"
        "2. 데이터가 없으면 추정하지 않는다.",
        "variables": "competition_metrics[]\nthresholds\nplatform",
        "output": '{"filtered_product_ids":[],"metric_summary":{},"data_gaps":[]}',
    },
}

SEED_POLICIES: dict[str, dict[str, str]] = {
    "POLICY_NAVER_V1": {
        "prompt": "PLATFORM_POLICY\n"
        "스마트스토어 공식 데이터와 등록/SEO 정책을 최우선으로 사용한다.\n"
        "\n"
        "CATEGORY\n"
        "1. ICBM이 보유한 실제 네이버 카테고리 ID 범위 안에서만 선택한다.\n"
        "2. AI가 존재하지 않는 카테고리 ID를 생성하지 않는다.\n"
        "\n"
        "TAGS\n"
        "1. 네이버 추천 태그와 제외 태그를 우선 적용한다.\n"
        "2. 가능하면 태그명보다 공식 tag_id를 기준으로 처리한다.\n"
        "3. 제외 태그는 AI가 다시 추천하지 않는다.\n"
        "4. 검색 데이터/급상승 키워드는 상품과 직접 관련될 때만 보강한다.\n"
        "\n"
        "SEO\n"
        "1. 네이버 공식 상품명/검색/태그 가이드를 우선한다.\n"
        "2. 중복 키워드와 무관 인기검색어 삽입을 피한다.\n"
        "3. 브랜드·모델·규격은 ProductFacts에 있을 때만 사용한다."
    },
    "POLICY_COUPANG_V1": {
        "prompt": "PLATFORM_POLICY\n"
        "쿠팡 공식 카테고리/상품등록 데이터와 정책을 최우선으로 사용한다.\n"
        "\n"
        "CATEGORY\n"
        "1. 쿠팡 카테고리 추천 기능 결과를 우선 후보로 받는다.\n"
        "2. 추천 결과를 ProductFacts와 AI가 반드시 교차검증한다.\n"
        "3. 예: 일반 영양제를 '강아지 영양제'로 추천하는 등 상품 대상/용도가 일치하지 않으면 REJECT 또는 "
        "requires_review=true.\n"
        "4. AI가 임의 categoryId를 생성하지 않는다.\n"
        "5. 실제 제공된 후보 categoryId 범위 안에서만 선택한다.\n"
        "\n"
        "OPTIONS / ATTRIBUTES\n"
        "1. 해당 쿠팡 카테고리의 필수 옵션/속성 구조를 함께 검증한다.\n"
        "2. 상품 사실과 필수속성이 충돌하면 자동확정하지 않는다.\n"
        "\n"
        "SEO\n"
        "쿠팡 공식 등록/검색 정책이 제공되면 해당 정책을 우선한다."
    },
    "POLICY_11ST_V1": {
        "prompt": "PLATFORM_POLICY\n"
        "11번가의 실제 카테고리/등록 정책 데이터 범위 안에서만 추천한다.\n"
        "\n"
        "RULES\n"
        "1. 제공된 실제 categoryId 후보 중에서만 선택한다.\n"
        "2. 플랫폼 공식 필수속성과 금칙/등록 정책을 우선한다.\n"
        "3. SEO/검색 데이터는 공식 정책과 ProductFacts를 침범하지 않는 범위에서만 보강한다.\n"
        "4. 근거가 부족하면 requires_review=true."
    },
    "POLICY_COMMON_MARKET_V1": {
        "prompt": "PLATFORM_POLICY\n"
        "특정 플랫폼이 아직 결정되지 않은 분석/쇼핑인사이트 작업에 사용한다.\n"
        "\n"
        "PRIORITY\n"
        "플랫폼 공식 데이터/정책 > ICBM ProductFacts > 플랫폼 자체 추천 기능 > 검색 데이터 "
        "> AI 판단.\n"
        "\n"
        "RULES\n"
        "1. 플랫폼이 결정되면 반드시 해당 플랫폼 전용 Policy로 교체한다.\n"
        "2. 검색/트렌드 데이터는 상품 사실을 변경하는 근거가 아니다.\n"
        "3. 추천은 후보이며 플랫폼 등록 전에 실제 플랫폼 정책 검증을 수행한다."
    },
}


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _text_field(field: str) -> str:
    return f"json_type(NEW.content_json, '$.{field}') = 'text'"


def fingerprint(content: dict[str, str]) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upgrade() -> None:
    _create_family(
        TEMPLATES, TEMPLATE_REVISIONS, TEMPLATE_CURRENT, key="template_key", layered=True
    )
    _create_family(POLICIES, POLICY_REVISIONS, POLICY_CURRENT, key="policy_key", layered=False)
    _seed()


def _create_family(identity: str, revisions: str, current: str, *, key: str, layered: bool) -> None:
    columns = [sa.Column(key, sa.String(length=64), nullable=False)]
    checks = [_check(identity, f"{key} <> ''", "key_present")]
    if layered:
        columns.append(sa.Column("layer", sa.String(length=8), nullable=False))
        checks.append(_check(identity, "layer IN ('GLOBAL', 'ROLE', 'TASK')", "layer_known"))
    op.create_table(
        identity,
        *columns,
        sa.Column("created_at", sa.DateTime(), nullable=False),
        *checks,
        sa.PrimaryKeyConstraint(key, name=op.f(f"pk_{identity}")),
    )
    op.create_table(
        revisions,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column(key, sa.String(length=64), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("seed_version", sa.String(length=16), nullable=True),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(revisions, "revision_no >= 1", "revision_no_positive"),
        _check(
            revisions,
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            "content_is_object",
        ),
        _check(revisions, _hex64("content_fingerprint"), "content_fingerprint_hex"),
        _check(revisions, "origin IN ('SEED', 'OPERATOR', 'RESET')", "origin_known"),
        # A seed is revision 1 and names its seed version; nothing else is a seed.
        _check(
            revisions,
            "(origin = 'SEED') = (revision_no = 1)"
            " AND (origin = 'SEED') = (seed_version IS NOT NULL)",
            "seed_is_first",
        ),
        _check(revisions, "authored_by <> ''", "authored_by_present"),
        _check(revisions, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            [key], [f"{identity}.{key}"], name=op.f(f"fk_{revisions}_{key}_{identity}")
        ),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{revisions}")),
        _unique(revisions, key, "revision_no"),
    )
    op.create_table(
        current,
        sa.Column(key, sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("moved_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(current, "moved_by <> ''", "moved_by_present"),
        _check(current, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            [key], [f"{identity}.{key}"], name=op.f(f"fk_{current}_{key}_{identity}")
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{revisions}.revision_id"],
            name=op.f(f"fk_{current}_revision_id_{revisions}"),
        ),
        sa.PrimaryKeyConstraint(key, name=op.f(f"pk_{current}")),
    )
    _install_triggers(identity, revisions, current, key=key, layered=layered)


def _install_triggers(
    identity: str, revisions: str, current: str, *, key: str, layered: bool
) -> None:
    _trigger(identity, "no_update", "UPDATE", _raise(f"{identity} is never updated", "1"))
    _trigger(identity, "no_delete", "DELETE", _raise(f"{identity} is never deleted", "1"))
    _trigger(
        revisions,
        "revision_follows",
        "INSERT",
        _raise(
            f"{revisions}: a revision follows the one before it",
            f"NEW.revision_no <> 1 + (SELECT COALESCE(MAX(revision_no), 0)"
            f" FROM {revisions} r WHERE r.{key} = NEW.{key})",
        ),
    )
    # The content holds exactly the text fields of its layer.
    prompt_only = (
        f"{_text_field('prompt')} AND (SELECT COUNT(*) FROM json_each(NEW.content_json)) = 1"
    )
    task = (
        f"{_text_field('mode')} AND {_text_field('prompt')} AND {_text_field('variables')}"
        f" AND {_text_field('output')} AND (SELECT COUNT(*) FROM json_each(NEW.content_json)) = 4"
    )
    if layered:
        shape = (
            f"CASE (SELECT layer FROM {identity} t WHERE t.{key} = NEW.{key})"
            f" WHEN 'TASK' THEN ({task}) ELSE ({prompt_only}) END"
        )
    else:
        shape = prompt_only
    _trigger(
        revisions,
        "content_shape",
        "INSERT",
        _raise(f"{revisions}: the content holds exactly the text fields of its layer", f"NOT ({shape})"),
    )
    _trigger(revisions, "no_update", "UPDATE", _raise(f"a {revisions} row is never updated", "1"))
    _trigger(revisions, "no_delete", "DELETE", _raise(f"a {revisions} row is never deleted", "1"))
    newest = (
        f"NOT EXISTS (SELECT 1 FROM {revisions} r"
        f" WHERE r.revision_id = NEW.revision_id AND r.{key} = NEW.{key}"
        f" AND r.revision_no = (SELECT MAX(m.revision_no) FROM {revisions} m"
        f" WHERE m.{key} = NEW.{key}))"
    )
    _trigger(
        current,
        "names_newest_on_insert",
        "INSERT",
        _raise(f"{current}: the current revision is the newest of its own entry", newest),
    )
    _trigger(
        current,
        "same_entry",
        "UPDATE",
        _raise(f"{current}: a pointer never changes entry", f"NEW.{key} <> OLD.{key}"),
    )
    _trigger(
        current,
        "names_newest_on_update",
        "UPDATE",
        _raise(f"{current}: the current revision is the newest of its own entry", newest),
    )
    _trigger(current, "no_delete", "DELETE", _raise(f"a {current} row is never deleted", "1"))


def _seed() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    for key, content in SEED_PROMPTS.items():
        _seed_one(
            bind,
            TEMPLATES,
            TEMPLATE_REVISIONS,
            TEMPLATE_CURRENT,
            "template_key",
            key,
            content,
            now,
            layer=SEED_LAYERS[key],
        )
    for key, content in SEED_POLICIES.items():
        _seed_one(
            bind,
            POLICIES,
            POLICY_REVISIONS,
            POLICY_CURRENT,
            "policy_key",
            key,
            content,
            now,
            layer=None,
        )


def _seed_one(
    bind: sa.engine.Connection,
    identity: str,
    revisions: str,
    current: str,
    key_column: str,
    key: str,
    content: dict[str, str],
    now: str,
    *,
    layer: str | None,
) -> None:
    revision_id = hashlib.sha256(f"{SEED_VERSION}:{identity}:{key}".encode()).hexdigest()[:32]
    revision_id = (
        f"{revision_id[:8]}-{revision_id[8:12]}-{revision_id[12:16]}"
        f"-{revision_id[16:20]}-{revision_id[20:32]}"
    )
    if layer is None:
        bind.execute(
            sa.text(f"INSERT INTO {identity} ({key_column}, created_at) VALUES (:key, :now)"),
            {"key": key, "now": now},
        )
    else:
        bind.execute(
            sa.text(
                f"INSERT INTO {identity} ({key_column}, layer, created_at) VALUES (:key, :layer, :now)"
            ),
            {"key": key, "layer": layer, "now": now},
        )
    bind.execute(
        sa.text(
            f"INSERT INTO {revisions} (revision_id, {key_column}, revision_no, content_json,"
            " content_fingerprint, origin, seed_version, authored_by, correlation_id, authored_at)"
            " VALUES (:id, :key, 1, :content, :fingerprint, 'SEED', :seed, :actor, :cid, :now)"
        ),
        {
            "id": revision_id,
            "key": key,
            "content": json.dumps(
                content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
            "fingerprint": fingerprint(content),
            "seed": SEED_VERSION,
            "actor": SEED_ACTOR,
            "cid": SEED_CORRELATION,
            "now": now,
        },
    )
    bind.execute(
        sa.text(
            f"INSERT INTO {current} ({key_column}, revision_id, moved_by, correlation_id, moved_at)"
            " VALUES (:key, :id, :actor, :cid, :now)"
        ),
        {"key": key, "id": revision_id, "actor": SEED_ACTOR, "cid": SEED_CORRELATION, "now": now},
    )


def downgrade() -> None:
    bind = op.get_bind()
    beyond_seed = {
        table: bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE origin <> 'SEED'")
        ).scalar_one()
        for table in (TEMPLATE_REVISIONS, POLICY_REVISIONS)
    }
    if any(beyond_seed.values()):
        raise RuntimeError(
            "cannot drop the AI prompt registry: "
            + ", ".join(f"{n} operator revision(s) in {t}" for t, n in beyond_seed.items() if n)
            + " are held; an operator's prompt history is never silently destroyed"
        )
    for table in reversed(CREATED):
        op.drop_table(table)
