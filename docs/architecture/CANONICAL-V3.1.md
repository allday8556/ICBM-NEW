# ICBM Canonical Architecture v3.1 — 수집 · 중복관리 · 대량등록

> 상태: **FREEZE APPROVED** — 아키텍트 승인 (2026-09-13)
> 작성: Claude (v3 감사 지적 5건 + 문서 정리 2건 반영)
> 대체: `CANONICAL-V1/V2/V3-BY-CLAUDE.md` (모두 폐기. 참조하지 않는다)
> 배치 위치: `docs/architecture/CANONICAL-V3.1.md`

v1 대비 가장 큰 변경은 **Registration / Draft / Snapshot에 Item 계층이 생긴 것**이다(v2).
v3는 그 Item 계층이 아직 도달하지 못했던 지점을 마저 정리한다 — Snapshot의 facts revision,
현재 조달처 저장소, Batch 경계, `primary_source_id`의 의미.

---

## 1. 최상위 원칙

```
공급처 원본        = 사실
이미지/OCR 결과    = 검증 전 Evidence
플랫폼 공식 API    = 공식 후보 / 정책
검색데이터         = 수요 신호
CLIPROXYAPI        = 검증 / 해석 / 추천
사용자 확정값      = 최우선
```

```
AI는 사실을 만들지 않는다.
AI 장애는 COLLECT를 막지 않는다.
변경되지 않은 영역은 다시 처리하지 않는다.
물리적 AI 호출 수보다 품질과 부분복구 가능성을 우선한다.
사용자 수정값은 stale AI job이 덮어쓸 수 없다.
ProductFacts와 Marketplace 등록정책을 섞지 않는다.
결과 불명 CREATE는 재시도하지 않는다. 먼저 조회한다.
자동으로 지우지 않는다. 제안하고 사용자가 고른다.
```

목표는 **빠르고 정확한 대량등록**이다. 순서가 중요하다 — 빠르게 틀린 것을 대량생산하지 않는 것이 먼저다.

---

## 2. identity 모델

### 2.1 다섯 층

```
① SourceDiscovery                 무엇이 존재하는가
② SourceProduct / SourceSKU
   / QuantityOffer                공급처가 실제로 무엇을 파는가
③ ProductGroup                    여러 공급처 중 무엇이 동일한 판매 대상인가
④ ListingDraft / ListingItem      그것을 마켓에서 어떤 구성으로 팔 것인가
⑤ RegistrationSnapshot            실제로 무엇을 보냈는가
```

### 2.2 ProductGroup — 제품 자체의 intrinsic identity

**ProductGroup은 상품군이 아니라 "판매 가능한 동일 identity"의 집합이다.**
여러 공급처에서 발견된 같은 제품을 하나로 묶은 것이다.

동일성 판정에 들어가는 것 — **제조사가 결정한 속성만** 들어간다.

```
브랜드 / 제조사
모델 / 품번 (MPN)
GTIN
색상 / 규격
용량 (unit_amount + unit_code)
제조사 고유 pack        예: 제조사 SKU 자체가 "30정 × 3박스 세트"
```

들어가지 **않는** 것 — 판매자가 결정하는 것은 전부 ListingComposition(§2.3)으로 간다.

```
ICBM이 몇 개씩 묶어 파는가
공급처의 수량별 가격 구간
```

따라서 다음은 서로 다른 ProductGroup이다.

```
비타민C 500mg 30정
비타민C 500mg 60정
비타민C 1000mg 30정
```

반면 다음은 **같은 ProductGroup**이다.

```
30정 1박스를 1개 판매
30정 1박스를 3개 묶어 판매
```

후자의 차이는 ListingComposition이 담는다.

상위 개념 `ProductFamily`를 나중에 둘 수 있으나 **검색·UI 편의용이며 등록 identity가 될 수 없다.**

### 2.3 ListingComposition — 판매자가 만든 구성

**ICBM이 마켓에서 판매하는 구성**이다. 제조사 포장단위가 아니라 판매 묶음(multiplicity)을 표현한다.

```
ListingComposition            (immutable entity)
  composition_id
  quantity                    판매 묶음 수
  unit_amount / unit_code     기준 단위 (제품 자체 속성에서 파생)
  pack_count
  units_per_pack
  total_amount
  composition_signature       = hash(canonical normalized structure)
```

`composition_signature`는 사람이 읽는 `"2 x 500ml"` 문자열이 아니라 **정규화된 구조의 canonical
signature**다. 표기 차이(`2x500ml`, `500ml 2개`)가 서로 다른 identity가 되는 것을 막는다.

**ListingComposition은 immutable이다.** 구성이 바뀌면 기존 행을 UPDATE하지 않고 새 composition을
만든다. 그래야 과거 RegistrationSnapshot의 의미가 소급해서 바뀌지 않는다.

### 2.4 Item의 논리적 판매 identity

```
Item sellable identity = current_group_id + listing_composition_signature
```

이 키가 §6.7(같은 Registration 내부 옵션 중복)과 §6.6·§9.4(마켓 중복)에서 동일하게 쓰인다.

### 2.5 중심 모델

```
MarketplaceListingDraft
 └─ DraftListingItem[]
      ├─ group_id
      ├─ listing_composition_id
      └─ source_offer_bindings

RegistrationSnapshot
 └─ RegistrationItemSnapshot[]
      ├─ registration_item_key          등록 전에 생성되는 안정 키
      ├─ group_id_at_registration
      ├─ group_membership_revision_id
      ├─ listing_composition_id
      ├─ source_product_facts_revision_id_at_registration
      ├─ pricing_snapshot_id_at_registration
      └─ source_snapshot

RegistrationIntent
 └─ registration_snapshot_id
 └─ RegistrationAttempt[]

MarketplaceRegistration
 └─ MarketplaceRegistrationItem[]
      ├─ registration_item_key
      ├─ current_group_id
      ├─ listing_composition_id
      ├─ current_source_binding_id      ← 지금 어디서 조달하는가
      └─ marketplace_option_id / value  CREATE 이후 부여됨
```

```
SINGLE_LISTING_WITH_OPTIONS   Registration 1 : Item N
SEPARATE_LISTINGS             Registration N : Item 1 (각각)
SELECTED_OFFERS               선택된 Item 만 포함
```

마켓 상품 하나에 옵션이 10개면 `MarketplaceRegistrationItem` 10개로 표현된다.

### 2.6 네 질문의 분리

```
ProductGroup                                       무엇을 파는가
ListingComposition                                 어떤 구성으로 파는가
MarketplaceRegistrationItem.current_source_binding 지금 어디서 조달하는가
RegistrationItemSnapshot                           그때 실제로 무엇을 보냈는가
```

이 넷을 절대 섞지 않는다.

### 2.7 현재 상태와 역사적 사실

```
MarketplaceRegistrationItem.current_group_id        가변. 현재 소속
RegistrationItemSnapshot.group_id_at_registration   불변. 등록 당시 값
```

이름 자체가 의미를 드러내도록 고정한다. SPLIT 이후 `current_group_id`만 successor로 갱신되고
Snapshot 값은 영구 불변이다.

---

## 3. 파이프라인

```
SOURCE DISCOVERY
  ├─ cheap discovery (목록 수준)
  └─ NEW / KNOWN / CHANGED / STALE 판정
        ↓
COLLECT                          (NEW / CHANGED / STALE 만)
        ↓
ProductFactsRevision
        ↓
PRE-GATE                         명백한 품절 / 판매금지 / 금지표현 / 기본 사실 누락
        ↓
STREAMING DEDUP                  기존 ProductGroup 후보와 즉시 비교
        ↓
PLATFORM DISCOVERY / ENRICH      (일부 선행 가능)
        ↓
COLLECTION BATCH FINALIZE
        ↓
DEDUP RECHECK                    ← barrier
        ↓
GroupMembershipRevision 확정
        ↓
LISTING DRAFT / ITEM 구성
        ↓
FINAL-GATE / READINESS
        ↓
BATCH PREFLIGHT
        ↓
READY / REVIEW_REQUIRED / BLOCKED / DUPLICATE / STALE
        ↓
RegistrationSnapshot             불변 전송본 고정
        ↓
RegistrationIntent → CANARY
        ↓
READ-BACK CONTRACT VERIFY
        ↓
BULK REGISTER
        ↓
READ-BACK CONTRACT VERIFY
        ↓
POST-REGISTER AUDIT
```

**속도를 위해 스트리밍, 정확도를 위해 등록 직전 barrier.**

### 3.1 불변조건

```
CollectionBatch 의 final dedup pass 가 끝나기 전에는
해당 배치에서 RegistrationBatch 를 생성하지 않는다.
```

```
COLLECT MUST NOT invoke AI inline.
```

```
하나의 Draft 안의 모든 Item 은
(group_id + composition_signature) 가 서로 달라야 한다.
```

---

## 4. SOURCE DISCOVERY

```
SourceDiscoveryRun
  discovery_run_id
  supplier_key
  source_scope              카테고리 URL / 검색어 / 목록
  checkpoint                opaque. adapter 만 내용을 이해한다
  state
  seen_count / new_count / known_count / changed_count
  started_at / completed_at / last_run_at
```

`checkpoint`는 opaque 값이다. 사이트마다 `page=28`, `offset=2800`, `cursor_token=…`,
`last_product_id=…` 형태가 다르다. orchestrator는 해석하지 않는다.

### 4.1 cheap discovery

```
source_product_id / URL / 목록 가격 / 목록 재고 상태 / thumbnail / listing_fingerprint
```

### 4.2 상세수집 대상 판정

```
NEW                                   → 수집
KNOWN + 최근 확인 + fingerprint 동일  → 상세수집 skip
KNOWN + fingerprint 변경              → CHANGED → 재수집
KNOWN + refresh TTL 초과              → STALE   → 재수집
```

`known_count` 상품을 영원히 skip하지 않는다. 대량수집 비용의 대부분이 이 판정에서 결정된다.

---

## 5. COLLECT

### 5.1 상세페이지 정제

포함: 상품 설명 / 상품정보제공고시 / 규격·성분·옵션 / 상품 이미지 / 구성·사용정보
제외: 공급처 공통 공지 / 배송 공통 안내 / 회원·사업자 안내 / 이벤트 배너 / 타 상품 광고

```
supplier_profile.exclude_selectors
반복 블록 감지 (같은 공급처 다른 상품에 동일 블록 → 공통 영역)
판정 애매 → 해당 블록만 REVIEW_REQUIRED, 수집 자체는 실패시키지 않는다
```

AI로 블록을 판정하지 않는다. 상세페이지당 호출이 발생해 비용이 통제되지 않는다.

### 5.2 SourceSKU / QuantityOffer

```
SourceSKU
  source_sku_id
  속성 (예: 빨강 / 500ml)

QuantityOffer                  ← Source 쪽 사실. 판매 구성이 아니다
  offer_id
  source_sku_id
  quantity
  total_price / currency
  supplier_shipping
  minimum_sale_price
```

```
1개 18,000 / 2개 34,000 / 3개 48,000
```

를 `17,000 × 2` 같은 형태로 평탄화하지 않는다.

`minimum_sale_price`가 존재하면 목표마진가보다 우선하며 **`max()` 규칙을 사용하지 않는다.**

**`QuantityOffer`는 공급처가 제시한 가격 구간이고, `ListingComposition`은 ICBM이 정한 판매 구성이다.**
둘은 대응할 수도, 대응하지 않을 수도 있다(§12.1).

### 5.3 품절 판단

```
BUY/CART 활성            → 판매중
SOLD OUT + 구매경로 없음 → 품절
근거 혼재                → 확인필요
```

### 5.4 ImageFactsAdapter — 조건부

국내 공급처는 상품정보제공고시가 이미지에만 존재하는 경우가 있다. 완전 제외하면
REVIEW_REQUIRED가 과도하게 발생한다.

```
required fact missing (N개 이상, 임계값은 Policy)
+ relevant detail images exist
→ ImageFactsAdapter
```

결과는 사실로 바로 승격하지 않는다. Evidence / Candidate Fact로 저장한다.

```
source = IMAGE_OCR
image_hash / image_index
extracted_value / confidence
evidence_region (가능한 경우)
```

상품당 최대 이미지 수 / 최대 호출 수 / 최대 비용을 제한한다.

---

## 6. DEDUP / GROUPING

### 6.1 중복의 세 유형

| 유형 | 내용 | 해결 |
| ---- | ---- | ---- |
| 자기 중복 | 같은 공급처 상품 재수집 | `(supplier_key, source_product_id)` 유일성 |
| 교차 공급처 중복 | 도매몰 A와 B가 같은 상품 | ProductGroup (이 절) |
| 마켓 중복 등록 | 내 계정에 이미 존재 | Preflight `DUPLICATE` (§9.4) |

마켓 중복 검사는 ICBM DB만 보지 않는다. **ICBM 도입 전 직접 등록한 상품**을 잡기 위해
마켓 조회(판매자상품코드 · 바코드 · 상품명)까지 수행한다.

### 6.2 매칭 신호

```
AUTO CONFIRM 가능

A. valid GTIN exact (checksum / 형식 검증 통과)
   + 용량 / 규격 / 제조사 pack conflict 없음

B. manufacturer(brand) + MPN exact
   + 핵심 규격 conflict 없음
```

```
후보 생성용 (자동 병합 불가)

pHash / 상품명 정규화 유사도 / 브랜드 / 모델명 / 옵션 구조
```

**pHash는 강한 후보 신호이나 자동 병합 신호가 아니다.** 같은 제조사가 500ml와 1L 제품에
동일 대표 이미지를 쓰는 사례가 있다.

`Asset`에 `asset_hash`와 함께 `phash`를 저장한다. Asset Pipeline(§11.5)에서 이미 해시를
계산하므로 추가 비용이 거의 없다.

**용량 / 규격 / 제조사 pack 불일치는 신호가 아니라 거부 조건이다.**

### 6.3 실행 방식

```
수집 중:   STREAMING DEDUP    기존 ProductGroup 후보와 즉시 비교 → 조기 비용 절감
배치 종료: DEDUP RECHECK      배치 내 신규 상품 간 중복 재평가 → GroupMembershipRevision 확정
```

RECHECK 시점에는 아직 등록 전이므로 병합 비용이 없다.

### 6.4 오탐이 미탐보다 위험하다

놓친 중복은 상품 2개가 올라가는 것이고, 잘못 묶은 것은 고객이 다른 물건을 받는 것이다.

```
자동 병합   결정론적 신호(§6.2 A/B)에서만
그 외       CANDIDATE + 리뷰 큐
병합은 되돌릴 수 있어야 한다 (REJECTED → 독립 상품 복귀)
의도적 중복 등록  DuplicateOverride (§9.4) + 사유 + 승인자 + 감사로그
                  ProductGroup 에는 두지 않는다
```

### 6.5 등록 후 SPLIT

Item 단위로 처리한다. 같은 Registration의 다른 Item은 영향받지 않는다.

```
Registration
 ├─ Item A → Group X
 └─ Item B → Group Y

Group X split
↓
영향받는 Item A 만 처리
↓
등록 당시 source / group membership 근거로 successor 판정
↓
Item A.current_group_id 갱신
↓
GROUP_MEMBERSHIP_STALE
↓
RegistrationItemSnapshot 과 재검증
   이상 없음 → ACTIVE 유지
   충돌     → REVIEW_REQUIRED

Item B 영향 없음
```

successor가 명확하지 않으면 **자동 선택하지 않고 `REVIEW_REQUIRED`**로 둔다.

### 6.6 등록 후 MERGE — 서로 다른 Registration

```
Registration #1 Item → Group A + Composition C
Registration #2 Item → Group B + Composition C

A + B merge
↓
동일 duplicate_scope 안에서
동일 current_group_id + 동일 composition_signature
↓
DUPLICATE_CONFLICT
```

```
자동 삭제 · 자동 비활성화 금지
→ 사용자가 survivor listing 선택
→ 나머지 비활성 / 정리 제안
→ 감사로그
```

### 6.7 등록 후 MERGE — 같은 Registration 내부

**`group_id`가 같아졌다는 이유만으로 옵션 중복이 아니다.**

```
Item A → Group 생수500ml + Composition 1 × 500ml
Item B → Group 생수500ml + Composition 2 × 500ml
→ 정상. 1개 / 2개 옵션은 공존한다
```

판정은 Item sellable identity(§2.4)로 한다.

```
Item A → Group A + Composition C1
Item B → Group B + Composition C2

A + B merge 이후

C1 != C2   → 정상 옵션으로 유지
C1 == C2   → OPTION_DUPLICATE_CONFLICT
```

```
자동 옵션 삭제 금지
→ 사용자 survivor option 선택
→ 나머지 제거 / 비활성 제안
→ 감사로그
```

마켓에서 판매 중인 옵션을 자동으로 지우면 진행 중인 주문에 영향이 간다. §1의
"자동으로 지우지 않는다" 원칙을 옵션 수준에도 동일하게 적용한다.

### 6.8 계보 보존

```
GroupChangeEvent
  event_type              MERGE | SPLIT
  predecessor_group_ids
  successor_group_ids
  membership_revision
  decided_by / created_at
```

---

## 7. ENRICH (AI 단계)

### 7.1 Task는 논리적으로 독립, 호출은 가능하면 묶는다

```
EnrichmentPlan
├─ category_validation
├─ recommended_name
├─ recommended_tags
├─ required_option_mapping
└─ missing_fact_review
```

fingerprint(§7.2)에 따라 이번에 필요한 Task만 선택한다.
호환되는 Task는 한 번의 CLIPROXYAPI 요청으로 batch할 수 있으나 **응답은 Task별 독립 상태**다.

```
category_validation     OK
recommended_name        OK
recommended_tags        FAILED(content_filter)
required_option_mapping OK
```

실패한 Task만 재실행한다.

**"정상 상품 = 반드시 AI 1회"는 계약이 아니다.** 목표는 `최소 호출 수 + 품질 유지 + 부분 실패 복구`이며,
1회/2회 batch 품질은 실제 데이터로 비교해 결정한다(§13).

### 7.2 fingerprint

```
enrich_input_fingerprint = hash(
    relevant_facts
  + policy_version
  + prompt_version
  + model_id
  + enrichment_schema_version
)
```

영역별로 둔다.

```
price_fingerprint / detail_fingerprint / option_fingerprint
notice_fingerprint / image_fingerprint
```

```
가격만 변경      → 가격 계산만 갱신
상세/성분 변경   → 상품명 · 태그 · 고시 검증
옵션 변경        → 필수옵션 검증
변화 없음        → 기존 AI 결과 재사용
```

### 7.3 그룹 변경 시 처리 — 캐시 무효화와 확정값 무효화는 다른 문제

그룹 MERGE / SPLIT 시 그룹의 구성원이 바뀌므로 해당 그룹의 facts 집합이 바뀌고,
fingerprint가 무효가 된다. (조달처 교체는 Item 운영 상태이지 facts 변경이 아니다 — §12.1)

```
fingerprint 재계산
→ 기존 EnrichmentResult 의 fingerprint 와 비교
→ mismatch → 해당 결과를 STALE 로 표시
→ 필요한 task 만 재실행
```

**사용자가 lock한 `final_name` / `final_tags` 등은 유지한다.**

```
lock 된 확정값 유지 + SOURCE_DRIFT 표시
```

캐시가 무효화된 것이지 사용자의 결정이 무효화된 것이 아니다.

### 7.4 CategoryMapping 재사용

```
accepted_by = USER                 → 최고 신뢰도
accepted_by = AI                   → confidence threshold 이상에서만 자동 재사용
marketplace_taxonomy_revision 변경 → USER / AI 모두 STALE, 재검토 대상
```

사용자 확정도 해당 마켓 카테고리가 삭제·이동된 뒤까지 영원히 재사용하지 않는다.

```
Mapping
  supplier_key / source_category_path
  mapping_scope / discriminator / discriminator_strategy_version
  marketplace_key / marketplace_category_id
  marketplace_taxonomy_revision
  mapping_revision
  accepted_by / confidence
```

`discriminator` 최적값은 판단이 아니라 실측으로 정한다. 거칠면 오류 증폭기가 되고,
세밀하면 재사용률이 0에 수렴한다.

### 7.5 AI 결과의 근거 보존

```
brand.value / brand.source / brand.evidence / brand.confidence
```

카테고리 · 브랜드 · 고시 등 AI가 판단한 중요 값은 `value + evidence + source + confidence`로 보존한다.

### 7.6 상품명 / 태그

등록 대상 플랫폼(`RegistrationTargetSet`)에 대해서만 생성한다.

```
smartstore.recommended_name / final_name / recommended_tags / final_tags
coupang.recommended_name / final_name / recommended_keywords / final_keywords
```

```
original_name / recommended_name / final_name
final_name_source    AI_INITIAL | AI_APPLIED | USER_MANUAL
final_name_locked
```

`final_name`이 비어 있으면 `original_name`으로 간주한다.

**최초 수집 시 1회 자동 적용한다.**

```
final_name = recommended_name
final_name_source = AI_INITIAL
→ AI_UNREVIEWED 상태 부여
```

`AI_UNREVIEWED`는 그 자체로 등록을 BLOCK하지 않는다. 다만 **필터 · 카운터 · 상세 표시**에서
사람이 아직 확인하지 않았음을 알 수 있어야 한다.

### 7.7 Lock과 동시성

Lock 단위는 `field × marketplace × account`다.

```
expected_field_revision
AI result apply → revision mismatch → 자동 적용 금지
```

**Lock + optimistic concurrency 둘 다 사용한다.** 적용 직전 재확인만으로는 TOCTOU 창이 남는다.

원본 변경 시:

```
사용자 확정값 유지 + SOURCE_DRIFT 표시 + 변경내역 표시
```

조용히 무시하지도, 조용히 덮어쓰지도 않는다.

### 7.8 태그 방향

```
ProductFacts → 플랫폼 태그추천 API → SearchSignalAdapter → CLIPROXYAPI 검증/확장
→ 중복 / 무관 / 금지 표현 제거 → 상품명 · 카테고리명 중복 최소화 → 최종 태그
```

```
피할 것:  루테인 / 건강식품 / 영양제
지향:     눈건강 / 루테인지아잔틴 / 중장년눈건강 / 황반건강
```

효능 · 기능성 표현은 `ProductFacts 근거 + 고시 근거 + 플랫폼 정책 허용`을 모두 만족할 때만 사용한다.
개수를 채우려고 무관 태그를 넣지 않는다.

---

## 8. Draft / ListingItem

```
MarketplaceListingDraft
  draft_id
  marketplace_key
  account_id
  listing_shape        SINGLE_LISTING_WITH_OPTIONS | SEPARATE_LISTINGS | SELECTED_OFFERS
  category_mapping_id
  final_name / final_tags
  required_attributes
  image_selection
  policy_version
  readiness_state
  draft_revision

DraftListingItem
  draft_item_id
  draft_id
  group_id
  listing_composition_id
  pricing_snapshot_id          ← 가격은 Item 속성이다
  source_offer_bindings[]      (source_sku_id, offer_id, 근거)
  item_readiness_state
```

**가격은 리스팅이 아니라 Item에 속한다.**

```
Item A = 1개 구성   원가 10,000  minimum_sale_price 18,000  판매가 18,000
Item B = 2개 구성   원가 19,000  minimum_sale_price 32,000  판매가 32,000
```

가격을 상위 레벨에 두면 SKU / 옵션 가격 평탄화 문제가 아키텍처 차원에서 다시 들어온다.

```
PricingSnapshot                (immutable)
  pricing_snapshot_id
  purchase_cost
  supplier_shipping
  minimum_sale_price
  target_margin_price
  final_sale_price
  price_basis                  TARGET_MARGIN | MINIMUM_SALE_PRICE
  pricing_policy_version
  fx_rate / fx_source / fx_captured_at    (비KRW 공급처)
```

확정 규칙도 Item별 Snapshot에 남는다.

```
minimum_sale_price 존재
→ 목표마진가보다 우선
→ price_basis = MINIMUM_SALE_PRICE
```

`max()` 규칙을 사용하지 않는다(§5.2).

**Draft의 식별 범위는 `(marketplace × account × draft_id)`다.**
v1의 `product × marketplace × account`는 Item 계층과 맞지 않으므로 폐기한다.
"무엇을 하나의 리스팅으로 묶을 것인가"는 사실이 아니라 판매 결정이며, `listing_shape`와
Item 집합으로 표현된다.

### 8.1 Draft 불변조건

```
한 Draft 안의 Item 들은 (group_id + composition_signature) 가 서로 달라야 한다.
```

```
SINGLE_LISTING_WITH_OPTIONS 인 Draft 의 모든 Item 은
같은 마켓 카테고리에 속하고 옵션으로 묶을 수 있어야 한다.   ← FINAL-GATE 검사
```

두 번째는 마켓이 거부할 조합(서로 다른 카테고리의 상품을 한 옵션으로 묶는 것)을 등록 전에 거른다.
"옵션으로 묶을 수 있는가"는 **플랫폼 adapter가 제공하는 결정론적 compatibility 검사**로 정의한다.
AI 판단에 두지 않는다.

### 8.2 source binding

Item이 참조하는 판매 구성은 `ListingComposition`이고, 그것을 **지금 무엇으로 조달하는가**가
`source_offer_bindings`다. 공급처가 바뀌면 binding만 바뀌고 Composition은 그대로다.

---

## 9. 게이트와 Preflight

### 9.1 PRE-GATE / FINAL-GATE

```
PRE-GATE     명백한 품절 / 확실한 판매금지 / 명백한 금지표현 / 기본 사실 누락
FINAL-GATE   최종 카테고리 / 카테고리 필수고시 / 필수옵션 / 브랜드 / 플랫폼 정책
             + Draft 불변조건(§8.1)
```

카테고리가 확정되어야 판단 가능한 항목이 있으므로 게이트는 ENRICH 앞뒤로 나뉜다.
BLOCKED가 명백한 상품만 AI 처리를 건너뛴다.

```
blocked_stage        PRE_GATE | FINAL_GATE
blocked_reason_code
gate_version / policy_version
blocked_at
```

`blocked_stage`로 **AI 비용을 쓴 이후 BLOCKED된 비율**을 측정한다. 높으면 PRE-GATE를 강화한다.

### 9.2 금지 표현

```
1. 결정론적 정책 / 금지어 검사 (카테고리별, 버전 보유)
2. 사전 통과분에 한해 AI 검토
```

AI만으로 금지 여부를 판정하지 않는다.

### 9.3 BATCH PREFLIGHT

외부 API 쓰기 **전에** 대부분의 오류를 제거한다. 마켓 API 호출이 마지막 단계여야 한다.

```
총 3,000건
READY             2,731
REVIEW_REQUIRED     114
BLOCKED             139
DUPLICATE            12
STALE                 4
```

검사 항목:

```
category / required options / price / minimum_sale_price
images / product notice / tags / stock
prohibited product / marketplace policy
revision freshness / duplicate listing / draft 불변조건
```

### 9.4 identity와 duplicate_scope

Registration identity에는 `account_id`가 **반드시** 들어간다.

```
Registration identity = (marketplace × account_id × listing_shape × Item 집합)
Item identity         = (current_group_id + composition_signature)
```

중복 판정 범위는 별도 정책이다.

```
duplicate_scope   ACCOUNT | SELLER_ENTITY | MARKETPLACE
```

첫 버전은 `ACCOUNT` 기준으로 시작한다. 다계정 운영 시 플랫폼 정책에 따라 ComplianceGate가
더 넓은 scope를 적용한다. **DB identity와 마켓 컴플라이언스 정책을 섞지 않는다.**

의도적 중복 등록은 **ProductGroup이 아니라 마켓/계정 범위의 override**로 허용한다.

```
DuplicateOverride
  override_id
  marketplace_key
  account_id
  group_id
  listing_composition_id      nullable
  reason
  approved_by
  created_at / revoked_at
```

`allow_duplicate`를 ProductGroup에 두면 "무엇을 파는가"에 판매정책이 섞이고, 스마트스토어
한 계정에서 허용한 것이 쿠팡·11번가·다른 계정까지 전파된다. **ProductGroup에는 두지 않는다.**

```
SellerEntity
  seller_entity_id

MarketplaceAccount
  account_id
  seller_entity_id   NOT NULL, FK → SellerEntity
```

빈 자리만 만들어두면 채워지지 않는다. **첫날부터 실제 관계가 채워지는 스키마로 만든다.**

```
부트스트랩 순서
1. SellerEntity 테이블 생성
2. default SellerEntity 생성
3. 기존 MarketplaceAccount backfill
4. FK 확인
5. seller_entity_id NOT NULL 적용
```

---

## 10. 등록 실행

### 10.1 RegistrationSnapshot — 불변 전송본

등록 후 Draft나 ProductFacts가 바뀌면 "그때 무엇을 보냈는지"가 사라진다.

```
RegistrationSnapshot
  snapshot_id
  marketplace_key / account_id
  listing_shape
  draft_revision
  policy_version / gate_version
  category_mapping_revision / tag_mapping_revision
  required_option_revision
  payload_hash / asset_hashes
  payload

RegistrationItemSnapshot
  item_snapshot_id
  snapshot_id
  registration_item_key                              immutable. 등록 전에 생성
  group_id_at_registration
  group_membership_revision_id
  source_product_facts_revision_id_at_registration   ← Item 마다 다르다
  listing_composition_id
  pricing_snapshot_id_at_registration                ← 가격도 Item 마다 다르다
  source_snapshot                                    등록 시점의 조달 근거
  marketplace_option_value
```

`product_facts_revision`은 Snapshot 공통값이 될 수 없다. Registration 하나에 ProductGroup이
여러 개 들어가므로 Item마다 facts revision이 다르다.

```
Registration
 ├─ Item A → Group A → facts revision 17
 ├─ Item B → Group B → facts revision 42
 └─ Item C → Group C → facts revision 91
```

이 값은 **등록 당시 조달 Source의 facts revision**이다. 향후 ProductGroup 자체에 canonical
facts revision을 두면 `group_facts_revision_id_at_registration`을 별도로 추가한다 — 이름이
겹치지 않도록 `source_` 접두사를 유지한다.

`pricing_snapshot_id_at_registration`도 같은 이유로 Item에 있다. read-back의 판매가 `EXACT`
비교(§10.4)와 Preflight의 `price / minimum_sale_price` 검사(§9.3)는 모두 Item 단위다.

`registration_item_key`는 **등록 전에 ICBM이 생성하는 안정 키**다. 가능하면 플랫폼의
판매자 옵션코드 / SKU 코드에도 심는다. `marketplace_option_id`는 CREATE 이후에 생기므로
대응 검증의 키가 될 수 없다.

```
Snapshot → CREATE → Read-back → Snapshot ↔ 마켓 실제값 비교
```

**현재 편집 중인 Draft와 비교하지 않는다. 실제 보낸 Snapshot과 비교한다.**

### 10.2 RegistrationIntent — 중복등록 방지

대량등록에서 실패보다 위험한 것은 **재시도로 같은 상품이 두 번 등록되는 것**이다.

Intent가 등록하려는 대상은 SourceProduct도 Group도 아니라 **확정된 불변 전송본**이다.

```
RegistrationIntent
  intent_id
  registration_batch_id
  registration_snapshot_id
  marketplace_key
  account_id
  operation                CREATE | UPDATE | DELETE
  idempotency_key
  state
  marketplace_product_id
```

group / composition / source 근거는 Snapshot 안의 Item에 들어 있으므로 Intent에 중복해 두지 않는다.

```
요청 timeout
→ CREATE 재시도 금지
→ 기존 intent 확인
→ marketplace read-back / lookup
→ 이미 생성됐으면 연결
→ 없다는 것이 확인된 경우에만 재시도
```

마켓 API가 idempotency key를 지원하지 않아도 **ICBM 내부에서 보장한다.**
판매자상품코드는 ICBM이 생성하는 결정론적 값이어야 조회로 화해할 수 있다.

### 10.3 RegistrationAttempt — 실행 이력

Intent에 마지막 상태만 남기면 감사와 장애 분석이 불가능하다.

```
RegistrationAttempt
  attempt_id
  intent_id
  attempt_no
  request_payload_hash
  response_code / response_body_digest
  error_class / error_code
  ambiguous_result            bool. 결과 불명 여부
  resolved_by                 READ_BACK | LOOKUP | USER
  started_at / finished_at
```

`ambiguous_result`가 §10.2의 재시도 금지 규칙과 연결된다.

### 10.4 Read-back은 계약 검증이다

```
CREATE 200 OK  ≠  등록 성공
```

실제 성공은 `요청 성공 + marketplace_product_id 확인 + read-back 성공 + 핵심 필드 일치`다.

```
판매가        EXACT
옵션 구조     EXACT        ← Item 집합과 1:1 대응 확인
카테고리      EXACT
판매 상태     EXACT
상품명        NORMALIZED_COMPARE
태그          SET_COMPARE
이미지        ORDER / TOLERANCE 정책
```

비교 대상은 **실제로 보낸 불변 Snapshot**이다. 편집 가능한 현재 상태와 비교하지 않는다.

```
RegistrationItemSnapshot[]
        ↕ EXACT MATCH   (registration_item_key 로 대응)
Marketplace read-back options[]
        ↓ 검증 성공 후
MarketplaceRegistrationItem[] 생성 / 갱신
```

`MarketplaceRegistrationItem`은 검증의 기준이 아니라 **검증 통과 후 만들어지는 결과**다.
옵션 하나가 누락되어도 CREATE는 성공으로 보고될 수 있으므로 개수와 대응을 모두 확인한다.
마켓이 옵션 순서를 바꾸거나 옵션명을 정규화해도 `registration_item_key`로 맞춘다.

### 10.5 CANARY

```
canary_size (정책값, 예: 20)
→ CREATE → READ-BACK VERIFY → 사용자 확인 → BULK REGISTER
```

### 10.6 대량 되돌리기

`RegistrationIntent`에 `registration_batch_id`를 남기고, `RegistrationAttempt`는 Intent를 통해
해당 배치에 귀속된다. 중복 저장하지 않는다. 사후에는 어느 것이 그 배치였는지 복원할 수 없다.

```
RegistrationBatch
→ 배치 단위 일괄 비활성화 / 수정
→ 대상: 해당 batch_id 로 등록된 것만
```

---

## 11. 배치 · 실패 · 동시성 · 자산

### 11.1 CollectionBatch / CollectionBatchItem

**CollectionBatch는 multi-source다.** 교차 공급처 DEDUP(§6.3)을 하려면 한 배치에 여러
공급처가 들어가야 한다. 단일 supplier에 묶으면 A 공급처와 B 공급처 사이 중복을 배치 안에서
검토할 수 없다.

```
CollectionBatch
  batch_id
  requested_count / completed / failed / blocked
  state / created_by / created_at

CollectionBatchSource
  batch_id
  supplier_key
  source_scope
  discovery_run_id

CollectionBatchItem
  batch_id → product
  stage     COLLECT | PRE_GATE | DEDUP | PLATFORM_DISCOVERY | ENRICH
            | DRAFT | FINAL_GATE | PREFLIGHT
  error_class / last_error_code
```

**등록은 CollectionBatch의 stage가 아니다.**

```
RegistrationBatch
 └─ RegistrationIntent[]
      └─ RegistrationAttempt[]
```

Collection batch와 Registration batch를 섞지 않는다. 수집 배치는 PREFLIGHT에서 끝나고,
그 결과로 별도의 등록 배치가 만들어진다(§3.1 barrier).

없으면 부분 재개가 불가능하고 진행률을 표시할 수 없다. **대량 작업은 반드시 중간에 죽는다.**

### 11.2 Failure Budget

**FailureBudget은 CollectionBatch와 RegistrationBatch에 각각 독립 적용한다.**

```
BatchPolicy
  batch_kind                    COLLECTION | REGISTRATION
  scope                         COLLECTION  → supplier / account
                                REGISTRATION → marketplace / account / endpoint_group
  max_consecutive_failures      예: 20
  max_failure_rate              예: 30% (최소 50건 이후 평가)
  on_breach                     PAUSE (중단 아님 — 재개 가능)
```

수집 쪽은 차단된 공급처 계정에 계속 요청을 보내는 것을 막고, 등록 쪽은 **마켓 인증이 깨진 채로
3,000건을 계속 보내는 것**을 막는다. 개별 잡의 백오프로는 둘 다 막을 수 없다(§11.3).

### 11.3 error_class

```
TRANSIENT | RATE_LIMIT | AUTH | VALIDATION | POLICY
CONFLICT | DUPLICATE | REVIEW_REQUIRED | FATAL | UNKNOWN
```

각 class마다 `retry 가능 여부 / 횟수 / backoff / batch pause 여부 / 사용자 개입 필요`를 정책으로 갖는다.

> 인증 만료가 20건 연속 발생한 것은 상품 20개의 실패가 아니라 **플랫폼 인증 1건의 실패**다.

### 11.4 Rate Limiter / Adaptive Concurrency

```
MarketplaceQuotaLimiter
적용 범위: PLATFORM DISCOVERY read / REGISTER write / OPERATE sync·read-back
관리 단위: marketplace × account × endpoint_group
READ / WRITE quota 분리 가능
```

```
정상       → 소폭 증가
429 증가   → 감소
5xx 증가   → backoff
AUTH       → 해당 계정 queue PAUSE
```

**공급처 수집 동시성과 마켓 등록 API 동시성은 별개로 관리한다.**

### 11.5 Asset Pipeline

```
원본 이미지 → 불필요 이미지 제거 → hash / pHash
→ resize / format normalize → marketplace asset → upload
```

```
Asset
  asset_hash / normalized_asset_hash / phash
  marketplace / marketplace_asset_id / uploaded_at
```

**변환도 한 번, 업로드도 재사용 가능한 범위에서는 한 번.**

---

## 12. 운영 루프 — 공급처 자동 대체

```
Stock / Price Monitor
        ↓
Item 의 current_source_binding 조달 불가
        ↓
ProductGroup 의 GroupMember 중 fallback candidates
        ↓
SourceCompatibilityGate
        ↓
PricingEngine 재계산
        ↓
same MarketplaceRegistration / Item 유지
        ↓
MarketplaceRegistrationItem.current_source_binding_id 교체
```

**마켓 리스팅과 옵션을 유지한 채 조달처만 교체한다.** 새 리스팅을 만들면 리뷰·순위·판매이력이 사라진다.

교체는 **Item 단위**다. 옵션 하나의 조달이 막혀도 나머지 옵션은 영향받지 않는다.

### 12.1 SourceBinding — 현재 조달처

등록 당시의 조달 근거(`RegistrationItemSnapshot.source_snapshot`)와 **지금 실제 조달처**는
다른 것이다. 전자는 불변 사실, 후자는 운영 상태다.

```
SourceBinding
  source_binding_id
  group_member_id
  source_sku_id
  quantity_offer_id
  fulfillment_quantity
  valid_from / valid_to
```

```
RegistrationItemSnapshot.source_snapshot            등록 당시 과거 사실 (불변)
MarketplaceRegistrationItem.current_source_binding_id  지금 실제 조달처 (가변)
```

**ProductGroup에는 `primary_source_id`를 두지 않는다.** 현재 조달처는 Item 단위이므로,
Group에 운영 source를 남기면 "무엇을 파는가"와 "어디서 조달하는가"가 다시 섞인다.
fallback 후보 탐색은 `GroupMember` 목록으로 충분하다.

### 12.2 SourceCompatibilityGate

```
1. ListingComposition 충족 가능 여부       ← 가장 먼저 본다
2. 원산지 / 구성품 / 수량 / 유통기한 정책 / 브랜드 표기
3. 매입원가 / 배송비 / minimum_sale_price / 목표마진
4. 판매 가능 여부 / 재고
```

1번이 먼저인 이유는, 가격과 규격이 모두 맞아도 **등록된 구성을 조달할 수 없으면 나머지 검사가
무의미하기 때문**이다.

```
등록 Composition = 2 × 500ml
새 공급처는 1개 / 5개 offer 만 보유
→ 기본적으로 교체 불가
```

```
allow_composed_fulfillment = false   (기본값)
```

`true`일 때만 "1개 상품 × 2 주문" 같은 조합 조달을 별도 계산한다.

**새 공급처로 마진이 깨지면 자동 교체 금지 → REVIEW_REQUIRED.**

### 12.3 주문 하강 경로

```
Marketplace Listing
→ MarketplaceRegistrationItem      (마켓 옵션 → Item 매핑)
→ current_group_id + ListingComposition
→ current_source_binding
→ SupplierOrder Snapshot
```

`SupplierOrder`에는 **주문 시점에 실제로 사용한 공급처**를 스냅샷으로 남긴다.
`current_source_binding`은 바뀌므로 스냅샷 없이는 사후 역추적이 불가능해진다.

---

## 13. 계측

```
성능
collect_ms / platform_api_ms / image_analysis_ms / ai_ms / persistence_ms / total_ms
collect_transport        BROWSER | HTTP
session_reuse_hit

비용
ai_call_count / ai_tokens_in / ai_tokens_out / ai_cost
enrich_cache_hit

정확도
registration_success_rate
readiness_first_pass_rate
review_required_rate
post_register_defect_rate          ← 진짜 정확도 지표
manual_override / recommendation_accepted / enrichment_task_success

중복관리
group_size_distribution
dedup_auto_merge_rate
dedup_unmerge_rate                 ← 오탐 지표
option_duplicate_conflict_rate
source_switch_count
enrich_saved_by_group
mapping_reuse_rate / mapping_override_rate / mapping_error_rate

게이트
blocked_stage 분포
```

**등록 성공률은 정확도가 아니다.** 등록에 성공했어도 카테고리가 틀렸으면 실패다.
`post_register_defect_rate`가 높으면 파이프라인이 빠르게 틀린 것을 대량생산하고 있다는 뜻이다.

`collect_transport`는 대량수집 속도 개선의 첫 단추다. 공급처 상당수는 브라우저 없이 수집 가능하며,
수집 프로파일에 `transport`를 두고 HTTP 우선 · 브라우저 폴백으로 가면 처리량이 크게 달라진다.

`persistence_ms`의 총 시간 대비 비율은 SQLite 단일 쓰기 소유자 구조의 천장을 판단하는 근거다.

---

## 14. 동결 스키마

```
SellerEntity
MarketplaceAccount (seller_entity_id NOT NULL)

SourceDiscoveryRun / checkpoint

CollectionBatch
CollectionBatchSource
CollectionBatchItem
BatchPolicy / FailureBudget  (batch_kind, scope)

ProductFactsRevision
SourceProduct / SourceSKU / QuantityOffer

ProductGroup
GroupMember
GroupMembershipRevision
GroupChangeEvent

Asset  (asset_hash / normalized_asset_hash / phash)

ListingComposition  (immutable, composition_signature)
PricingSnapshot     (immutable, price_basis, pricing_policy_version)

MarketplaceListingDraft
DraftListingItem    (pricing_snapshot_id)

DuplicateOverride   (marketplace_key, account_id, group_id, approved_by)

RegistrationBatch
RegistrationSnapshot
RegistrationItemSnapshot
  (registration_item_key,
   source_product_facts_revision_id_at_registration,
   pricing_snapshot_id_at_registration)
RegistrationIntent  (registration_batch_id, registration_snapshot_id, idempotency_key)
RegistrationAttempt (intent_id, ambiguous_result)

SourceBinding

MarketplaceRegistration
MarketplaceRegistrationItem
  (registration_item_key, current_group_id, listing_composition_id,
   current_source_binding_id, marketplace_option_id)

SupplierOrder  (actual source snapshot)

버전 계보
  gate_version / policy_version / taxonomy_revision
  mapping_revision / match_strategy_version
  discriminator_strategy_version / enrichment_schema_version
  draft_revision / composition_signature
  registration_item_key

duplicate_scope
```

ProductGroup 부속:

```
match_method / match_confidence / match_strategy_version
decided_by / status
created_at / confirmed_at
(primary_source_id 는 두지 않는다 — §12.1)
(allow_duplicate 는 두지 않는다 — DuplicateOverride, §9.4)
```

### 스키마와 계측만 먼저, 실측 후 조정할 것

```
adaptive concurrency 알고리즘
HTTP / BROWSER 자동선택 최적화
이미지 upload cache 세부 구현
discriminator 최적값
매칭 임계값과 자동 병합 허용 범위
fallback source 선정 정책
AI batch 1회 / 2회 분할
```

---

## 15. v3 대비 변경 (v3.1)

| # | 지적 | 수정 |
| - | ---- | ---- |
| 1 | **가격이 Item 계층으로 안 내려감** | `PricingSnapshot`(immutable) 도입. `DraftListingItem.pricing_snapshot_id`, `RegistrationItemSnapshot.pricing_snapshot_id_at_registration`. Draft/Snapshot 상위의 `pricing_snapshot` / `pricing_revision` 제거 |
| 2 | **`allow_duplicate`가 ProductGroup에 있음** | 제거. `DuplicateOverride(marketplace × account × group)`로 이동(§9.4). 한 계정의 허용이 다른 마켓·계정으로 전파되는 것을 차단 |
| 3 | FailureBudget 범위 | `BatchPolicy.batch_kind` + `scope` 추가. Collection은 supplier/account, Registration은 marketplace/account/endpoint_group |
| 4 | stage 명칭 혼동 | `DISCOVERY` → **`PLATFORM_DISCOVERY`**. SOURCE DISCOVERY(§4)와 구분 |
| 5 | facts revision 명칭 | `product_facts_revision_id_at_registration` → **`source_product_facts_revision_id_at_registration`**. 향후 group facts revision과 구분 |

## 16. v2 대비 변경 (v3)

| # | 지적 | 수정 |
| - | ---- | ---- |
| 1 | §8.1 Draft 불변조건 | 승인. "옵션으로 묶을 수 있는가"를 **플랫폼 adapter의 결정론적 compatibility 검사**로 한정 (AI 판단 아님) |
| 2 | §10.4 read-back 비교 대상 | `MarketplaceRegistrationItem` → **`RegistrationItemSnapshot`**. Item은 검증 기준이 아니라 검증 통과 후 생성되는 결과 |
| 3 | 옵션 대응 키 | `registration_item_key` 도입. 등록 **전에** ICBM이 생성하는 불변 키이며 판매자 옵션코드에 심는다. `marketplace_option_id`는 CREATE 이후 값이라 키가 될 수 없음 |
| 4 | 현재 조달처 저장소 부재 | `SourceBinding` 엔티티 + `MarketplaceRegistrationItem.current_source_binding_id` 추가 (§12.1) |
| 5 | `product_facts_revision` | Snapshot 공통 → **`RegistrationItemSnapshot` 별 revision**. Registration 하나에 Group 여러 개가 들어가므로 공통값이 성립하지 않음 |
| 6 | Batch 경계 | `CollectionBatch`를 **multi-source**로 (`CollectionBatchSource[]`). stage에서 `REGISTER` 제거 — 등록은 `RegistrationBatch` 소관 |
| 7 | `primary_source_id` | **제거**. 현재 조달처는 Item binding만 truth. Group에 운영 source를 두면 identity 층이 다시 섞임 |
| 8 | Attempt batch 귀속 | Intent에만 `registration_batch_id`. Attempt는 Intent를 통해 귀속. 중복 저장 안 함 |

## 17. v1 → v2 변경 요약 (이력)

| # | 변경 | 근거 |
| - | ---- | ---- |
| 1 | Registration / Draft / Snapshot에 **Item 계층** 도입 | `SINGLE_LISTING_WITH_OPTIONS`와 `ProductGroup` 1:1 참조가 충돌 |
| 2 | ProductGroup = intrinsic identity, ListingComposition = seller multiplicity로 **경계 재정의** | v1 §2.1이 수량·pack을 Group에 넣어 Composition과 의미 중복 |
| 3 | `composition_signature` 도입, Composition을 **immutable entity**로 | 표기 문자열을 identity로 쓰면 안 되고, UPDATE하면 과거 Snapshot 의미가 소급 변경됨 |
| 4 | `current_group_id` / `group_id_at_registration` **명명 분리** | 현재 상태와 역사적 사실의 혼동 방지 |
| 5 | `RegistrationIntent`에서 product_id / group_id **제거**, snapshot 참조만 | Intent가 등록하는 것은 Group이 아니라 확정 전송본 |
| 6 | `RegistrationAttempt` **정식 스키마 추가** | Intent에 마지막 상태만 남으면 ambiguous result 추적 불가 |
| 7 | `SellerEntity` **실제 엔티티로 생성** + NOT NULL + 부트스트랩 순서 | 존재하지 않는 대상을 참조하는 enum 방지, 빈 자리는 채워지지 않음 |
| 8 | §6.5~6.7 merge/split을 **Item 단위**로 재작성 | Item 계층 도입의 파급 |
| 9 | `OPTION_DUPLICATE_CONFLICT` 조건을 `group + composition_signature` **동일 시로 한정** | group만 같은 1개/2개 옵션은 정상 공존 |
| 10 | §7.3 fingerprint 재계산 시 **EnrichmentResult STALE 처리 + lock 유지** 명시 | 캐시 무효화와 확정값 무효화는 다른 문제 |
| 11 | Draft 식별 범위를 `(marketplace × account × draft_id)`로 **수정** | v1의 `product × …`는 Item 계층과 불일치 |
| 12 | Draft 불변조건 2건 추가 (§8.1) | Item 중복 방지, 옵션 묶기 가능성 사전 검증 |
| 13 | read-back에 **옵션 대응 검증** 추가 (§10.4) | 옵션 누락도 CREATE 성공으로 보고될 수 있음 |

---

## 18. 계약 미정 (마일스톤별)

| 항목 | 확정 시점 |
| ---- | --------- |
| `CliproxyAdapter` 전체 계약 | M3 이전 |
| `SearchSignalAdapter` 공급자 선정 | M3 이전. 공식 API 또는 이용이 허용된 데이터만 |
| `ImageFactsAdapter` 임계값 | M3 |
| `discriminator_strategy` 최적값 | M3 실측 후 |
| AI batch 1회 / 2회 | M3 실측 후 |

### M0 경계선

M0에서 AI 기능을 구현하지 않는다. 다만 다음 경계는 M0에 존재해야 한다.

```
docs 에 명시:  COLLECT MUST NOT invoke AI inline.
Job          → ENRICH 계열로 확장 가능한 구조
ErrorClass   → AI Adapter 오류를 매핑할 자리
```

### 문서 분할 (동결 후)

동결 이후 다음으로 나누되 **같은 규칙을 복제하지 않고 서로 참조**한다.

```
ARCHITECTURE.md   identity 모델과 불변조건 (§1, §2)
pipeline.md       실행 흐름 (§3~10)
operations.md     계측 · 실패예산 · 운영정책 (§11~13)
```
