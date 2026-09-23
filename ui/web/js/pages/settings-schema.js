// Structure of the settings screen, reproduced from the approved v29 prototype.
// This is presentation structure only: every value comes from the settings contract
// (`policy_values`), which is empty in M0, so every field renders as unset.

export const CONNECTION_LABEL = { NOT_CONNECTED: '미연동' };
export const API_STATUS_LABEL = { NOT_CONNECTED: '미연결' };

export const PLATFORM_TABS = [
  { key: 'common', label: '공통' },
  { key: 'smartstore', marketplace: 'smartstore' },
  { key: 'coupang', marketplace: 'coupang' },
  { key: 'st11', marketplace: 'st11' },
];

const field = (label, id, extra = {}) => ({ field: label, id, ...extra });
const toggle = (label, id) => ({ toggle: label, id });
const note = (text) => ({ note: text });

const REGISTRY = {
  roles: [
    ['수집 검증 AI', 'DOM·가격·배송비·옵션 근거 검증'],
    ['상품/MD AI', '상품명·태그·카테고리·옵션 정리'],
    ['커머스 운영 AI', '주문 상태·다음 처리·운영 안내'],
    ['CS 응대 AI', '문의 분석·고객 답변 초안'],
    ['판매상태 검증 AI', 'BUY/CART·품절 근거 교차검증'],
  ],
  policies: [
    ['스마트스토어 정책', '추천/제외 태그 ID · SEO · 공식 데이터 우선'],
    ['쿠팡 정책', '카테고리 추천 → AI 재검증 · 필수 옵션 확인'],
    ['11번가 정책', '실제 카테고리 ID 범위 · 등록 정책'],
  ],
  tasks: [
    ['추출 보정', '수집관리'],
    ['통합 상품 추천', '통합DB'],
    ['카테고리 재추천', '등록관리'],
    ['주문 운영 보조', '주문관리'],
    ['문의 답변', '문의관리'],
    ['품절 판정', '품절확인'],
  ],
};

function apiStatusCard(marketplace, capabilities) {
  return {
    title: '연동 상태 / 권한',
    items: [
      { status: '현재 상태', marketplace },
      ...capabilities.map((label) => ({ status: label, chip: '연동 전' })),
    ],
  };
}

function shippingOverrideCard(prefix) {
  return {
    title: '배송 관리',
    items: [
      toggle('공통 배송값 사용', `${prefix}.use_common_shipping`),
      field('배송비 override', `${prefix}.shipping_fee`, { placeholder: '공통값 사용' }),
      field('출고일 override', `${prefix}.dispatch_days`, { placeholder: '공통값 사용' }),
      field('출고지 override', `${prefix}.origin`, { placeholder: '공통 출고지 사용' }),
    ],
  };
}

function appliedValuesCard(labels) {
  return { title: '현재 적용값', items: labels.map((label) => ({ kv: label, tag: '공통' })) };
}

const API_ACTIONS = { actions: [{ label: '연결 테스트' }, { label: 'API 정보 저장', variant: 'blue' }] };

export const SUBTABS = {
  common: [
    {
      key: 'basic',
      label: '기본정보',
      blocks: [
        {
          cards: [
            {
              title: '사업자 / 기본정보',
              help: '플랫폼 공통으로 사용하는 사업자 기본정보입니다.',
              items: [
                field('사업자등록번호', 'business.registration_no'),
                field('상호(법인명)', 'business.company_name'),
                field('대표자명', 'business.representative'),
                field('담당자명', 'business.contact_name'),
              ],
            },
            {
              title: '통신판매 / 약관 정보',
              help: '통신판매업 신고번호와 공통 고지 URL을 관리합니다.',
              items: [
                field('통신판매업 신고번호', 'business.mail_order_no'),
                field('개인정보 URL', 'business.privacy_url'),
                field('이용약관 URL', 'business.terms_url'),
              ],
            },
            { title: '플랫폼 연동 현황', items: [{ marketplaceStatus: ['smartstore', 'coupang', 'st11'] }] },
            { title: '사용자 권한', items: [{ usersTable: true }] },
          ],
        },
      ],
    },
    {
      key: 'shipping',
      label: '배송 공통사항',
      blocks: [
        {
          cards: [
            {
              title: '기본 배송 정책',
              help: '플랫폼별 별도 설정이 없으면 이 값을 기본값으로 사용합니다.',
              items: [
                field('기본 배송비', 'shipping.base_fee'),
                field('기본 출고일', 'shipping.dispatch_days'),
                field('무료배송 기준', 'shipping.free_threshold'),
                field('제주 추가배송비', 'shipping.jeju_fee'),
                field('도서산간 추가', 'shipping.remote_fee'),
              ],
            },
            {
              title: '기본 출고지',
              items: [
                field('우편번호', 'shipping.origin_zip'),
                field('주소', 'shipping.origin_address'),
                field('담당자', 'shipping.origin_contact'),
                field('연락처', 'shipping.origin_phone'),
              ],
            },
          ],
        },
        { inheritNote: { bold: '상속 규칙', text: '각 플랫폼 배송관리에서 `공통값 사용`이 켜져 있으면 이 값을 자동 사용합니다.' } },
      ],
    },
    {
      key: 'return',
      label: '반품/교환 공통사항',
      blocks: [
        {
          cards: [
            {
              title: '반품 / 교환 기본 정책',
              items: [
                field('반품 배송비', 'returns.return_fee'),
                field('교환 왕복배송비', 'returns.exchange_fee'),
                field('반품 가능 기간', 'returns.window_days'),
              ],
            },
            {
              title: '기본 반품지',
              items: [field('우편번호', 'returns.zip'), field('주소', 'returns.address'), field('담당자', 'returns.contact')],
            },
          ],
        },
      ],
    },
    {
      key: 'notice',
      label: '상품정보고시 공통사항',
      blocks: [
        {
          cards: [
            {
              title: '상품정보제공고시 공통값',
              help: '상품별 사실 데이터가 없을 때만 사용할 수 있는 공통 기본값입니다.',
              items: [
                field('제조자/수입자', 'notice.manufacturer', { placeholder: '공통 기본값' }),
                field('제조국', 'notice.origin', { placeholder: '공통 기본값' }),
                field('A/S 책임자', 'notice.as_owner'),
                field('A/S 연락처', 'notice.as_phone'),
              ],
            },
            {
              title: '적용 우선순위',
              items: [
                { flow: ['상품 실제 정보', '수집/OCR 사실', '플랫폼 필수값', '공통 기본값'] },
                note('상품 실제 사실과 충돌하는 공통값은 자동 적용하지 않고 확인필요로 보냅니다.'),
              ],
            },
          ],
        },
      ],
    },
    {
      key: 'pricing',
      label: '가격정책',
      blocks: [
        {
          cards: [
            {
              title: '가격정책',
              items: [
                field('기본 목표마진', 'pricing.target_margin'),
                field('가격 올림 기준', 'pricing.rounding'),
                field('기본 배송비', 'pricing.base_shipping'),
              ],
            },
            {
              title: '가격 우선순위',
              items: [
                { flow: ['판매금지/품절', '매입원가', '최저판매가', '목표마진 계산'] },
                note('실제 PricingEngine 결과를 화면의 가격기준 라벨과 동일하게 사용합니다.'),
              ],
            },
          ],
        },
      ],
    },
    {
      key: 'overseas',
      label: '해외구매대행',
      blocks: [
        {
          cards: [
            {
              title: '해외 구매대행 / 전자상거래부호',
              help: '한 번 입력하면 해외 구매대행 상품 등록 시 지원 플랫폼 필드에 자동 적용합니다.',
              items: [
                field('전자상거래부호', 'overseas.ecommerce_code', { placeholder: '전자상거래부호 입력' }),
                { actions: [{ label: '저장', variant: 'blue' }], layout: 'stretch' },
              ],
            },
            {
              title: '자동 적용',
              items: [
                toggle('해외 구매대행 상품 자동 감지', 'overseas.auto_detect'),
                toggle('지원 플랫폼 등록 payload 자동 포함', 'overseas.auto_send'),
                toggle('등록 결과 read-back 검증', 'overseas.readback'),
                note('플랫폼별 API 필드가 확인된 경우 자동 처리하고 예외만 확인필요로 보냅니다.'),
              ],
            },
          ],
        },
      ],
    },
    {
      key: 'ai',
      label: 'AI / Prompt',
      blocks: [
        {
          cards: [
            {
              title: 'AI 기본 설정',
              items: [
                toggle('AI 상품명 추천', 'ai.product_name'),
                toggle('AI 태그 추천', 'ai.tags'),
                toggle('AI 카테고리 검증', 'ai.category'),
                toggle('결과 미리보기 후 적용', 'ai.preview'),
              ],
            },
          ],
        },
        {
          card: {
            title: 'AI Prompt Registry',
            full: true,
            help:
              'ICBM AI는 공통 규칙(Global) → 역할(Role) → 플랫폼 정책(Platform Policy) → 작업(Task) → 실행 데이터 순서로 조립됩니다.',
            items: [{ registry: REGISTRY }],
          },
        },
      ],
    },
    {
      key: 'alert',
      label: '알림',
      blocks: [
        {
          cards: [
            {
              title: '알림 설정',
              items: [
                toggle('주문 알림', 'alert.order'),
                toggle('문의 알림', 'alert.inquiry'),
                toggle('취소/교환/반품 알림', 'alert.claim'),
                toggle('품절 확인 알림', 'alert.soldout'),
              ],
            },
          ],
        },
      ],
    },
  ],

  smartstore: [
    {
      key: 'api',
      label: 'API 관리',
      blocks: [
        {
          cards: [
            {
              // M2 PR-E: a live credential replacement. The prototype's 스토어 식별값 is gone: the
              // seller account comes only from the authenticated account read.
              title: '스마트스토어 API 인증',
              help:
                '커머스API 애플리케이션 인증정보를 입력합니다. · 저장하면 새 자격증명 세대로 교체되고 이전 세션과 계정 확인은 무효가 됩니다. · Secret은 OS 보안 저장소에만 저장되며 다시 표시되지 않습니다. · 판매자 계정은 입력값이 아니라 인증된 계정 조회 결과로만 정해집니다.',
              items: [{ smartstoreCredentials: true }],
            },
            {
              // CAPABILITY_MAPPING §14.11 surface 1 (M2 PR-D): live capability truth, not demo rows.
              title: '연동 상태 / 권한',
              help:
                '인증·등록 권한·실제 등록은 각각의 증거로 따로 판단합니다. · ● 자동으로 확인된 강한 증거 · ◐ 운영자가 관리자 화면에서 확인한 증거 · ○ 미확인 · 운영자 확인은 이후의 권한 변경을 자동으로 감지하지 않습니다. · 실제 상품 등록은 등록 검증 전까지 확인되지 않습니다.',
              items: [{ capabilityProjection: 'smartstore' }, { workflowActions: 'smartstore' }],
            },
            {
              // M2 PR-E, instructions §8A: observation and first binding are separate actions.
              title: '계정 확인 / 연결 대상 확정',
              help:
                '계정 확인은 현재 인증으로 판매자 계정을 조회해 보여줄 뿐 연결하지 않습니다. · 처음 연결할 때는 표시된 계정을 운영자가 확인한 뒤 따로 확정해야 합니다. · 확정할 때 계정을 다시 조회해 같은 계정인지 비교하며, 실패하면 아무것도 연결되지 않습니다. · 이미 연결된 계정은 자동으로 바뀌지 않습니다.',
              items: [{ smartstoreAccount: true }],
            },
            {
              title: 'API 계약 검토 기록',
              help:
                'ICBM이 채택한 스마트스토어 API 계약을 운영자가 검토한 결과를 기록합니다. · NAVER가 확인한 것이 아닙니다. · 같은 값을 다시 기록해도 새 검토 기록으로 남습니다. · 허용되지 않는 전환은 서버가 거부합니다.',
              items: [{ contractReview: 'smartstore' }],
            },
            {
              title: '등록 권한 확인',
              help:
                '커머스API센터 관리자 화면에서 본 API 그룹을 기록합니다. · 운영자 확인(A0) 증거이며 NAVER 자동 확인이 아닙니다. · 필수 그룹·애플리케이션·매핑 기준·확인 일시는 ICBM이 채우고 실제 상품 등록은 확인하지 않습니다. · 확인은 30일(설정으로 줄일 수만 있음)이 지나면 만료되어 다시 확인해야 합니다.',
              items: [{ permissionAttestation: 'smartstore' }],
            },
          ],
        },
      ],
    },
    {
      // Gate 1 G1-A (ADR-0015 §2): the one Settings surface with a save contract.
      key: 'policy',
      label: '등록 정책',
      blocks: [
        {
          card: {
            title: '등록 대상 정책',
            full: true,
            help:
              '계정별 등록 대상 정책입니다. · 저장할 때마다 서버가 새 리비전을 만들고 이전 리비전은 그대로 남습니다. · 모든 값은 서버가 검증하며, 잘못된 값이 하나라도 있으면 아무것도 저장되지 않습니다. · 이미 만들어진 등록 스냅샷은 저장 당시의 정책 리비전을 그대로 유지합니다.',
            items: [{ targetPolicy: 'smartstore' }],
          },
        },
      ],
    },
    {
      key: 'shipping',
      label: '배송 관리',
      blocks: [{ cards: [shippingOverrideCard('smartstore'), appliedValuesCard(['기본 배송비', '기본 출고일', '출고지'])] }],
    },
    {
      key: 'product',
      label: '상품 / 카테고리',
      blocks: [
        {
          cards: [
            {
              title: '상품 / 카테고리',
              items: [
                toggle('실제 네이버 categoryId 범위 제한', 'smartstore.category_limit'),
                toggle('AI 카테고리 검증', 'smartstore.category_ai'),
                { button: '네이버 Policy 편집', variant: 'ai' },
              ],
            },
          ],
        },
        {
          // Gate 1 G1-B (ADR-0015 §3): the operator-reviewed category metadata, saved through its
          // own contract. No provider category endpoint is adopted.
          card: {
            title: '카테고리 메타데이터 검토',
            full: true,
            help:
              '카테고리별 필수 속성·상품정보고시·옵션·템플릿 규칙을 검토한 근거로부터 기록합니다. · 저장할 때마다 서버가 새 리비전을 만들고 현재 리비전으로 지정하며, 이전 리비전은 그대로 남습니다. · 근거가 증명하지 않는 규칙은 기록하지 않습니다. · AI 제안은 검토 완료로 기록할 수 없습니다. · 현재 리비전이 미검토이면 등록 사전검사는 이전 검토본으로 대체하지 않고 CATEGORY_METADATA_UNREVIEWED로 멈춥니다.',
            items: [{ categoryMetadata: 'smartstore' }],
          },
        },
      ],
    },
    {
      key: 'seo',
      label: '태그 / SEO',
      blocks: [
        {
          cards: [
            {
              title: '태그 / SEO',
              items: [
                toggle('네이버 추천 태그 우선', 'smartstore.recommended_tags'),
                toggle('네이버 제외 태그 차단', 'smartstore.excluded_tags'),
                toggle('tag_id 우선 저장', 'smartstore.tag_id'),
                toggle('검색데이터 AI 보강', 'smartstore.search_boost'),
                note('추천/제외 태그와 SEO 가이드가 우선이며 검색데이터는 상품 사실과 직접 관련된 경우에만 보강합니다.'),
              ],
            },
          ],
        },
      ],
    },
    {
      key: 'extra',
      label: '고유 설정',
      blocks: [{ cards: [{ title: '스마트스토어 고유 설정', items: [note('플랫폼 고유 설정이 추가되면 이 탭에 모읍니다.')] }] }],
    },
  ],

  coupang: [
    {
      key: 'api',
      label: 'API 관리',
      blocks: [
        {
          cards: [
            {
              title: '쿠팡 Open API 인증',
              help:
                'Wing에서 발급받은 API 인증정보를 입력합니다. · Secret Key는 실제 빌드에서 암호화 저장하며 로그/AI 요청에 포함하지 않습니다.',
              items: [
                field('Vendor ID', 'coupang.vendor_id', { placeholder: 'A00******' }),
                field('Access Key', 'coupang.access_key', { placeholder: 'Access Key' }),
                field('Secret Key', 'coupang.secret_key', { placeholder: 'Secret Key', secret: true }),
                API_ACTIONS,
              ],
            },
            apiStatusCard('coupang', ['상품 조회', '상품 등록/수정', '주문 조회']),
          ],
        },
      ],
    },
    {
      key: 'shipping',
      label: '배송 관리',
      blocks: [{ cards: [shippingOverrideCard('coupang'), appliedValuesCard(['기본 배송비', '기본 출고일'])] }],
    },
    {
      key: 'product',
      label: '상품 / 카테고리',
      blocks: [
        {
          cards: [
            {
              title: '상품 / 카테고리',
              help: '쿠팡 카테고리 추천은 별도 기능 카드가 아니라 AI 추천 내부에서 자동 사용합니다.',
              items: [
                toggle('쿠팡 카테고리 추천 결과 사용', 'coupang.category_recommend'),
                toggle('ProductFacts AI 재검증', 'coupang.ai_validate'),
                toggle('저신뢰 자동확정 금지', 'coupang.low_confidence_block'),
                { button: '쿠팡 Policy 편집', variant: 'ai' },
              ],
            },
          ],
        },
      ],
    },
    {
      key: 'option',
      label: '옵션 / 필수속성',
      blocks: [
        {
          cards: [
            {
              title: '옵션 / 필수속성',
              items: [
                toggle('카테고리 필수 옵션 구조 우선', 'coupang.required_options'),
                toggle('원본 SKU 보존', 'coupang.preserve_sku'),
                toggle('필수속성 충돌 시 확인필요', 'coupang.option_review'),
              ],
            },
          ],
        },
      ],
    },
    {
      key: 'growth',
      label: '쿠팡 그로스',
      blocks: [
        {
          cards: [
            {
              title: '쿠팡 그로스',
              help:
                '공식 API와 권한 범위를 확인한 뒤 상품/입고/재고/물류 중 지원 가능한 부분을 연결합니다. · 지원범위 확인 → 권한/인증 → 데이터 매핑 → 운영 UI → read-back',
              items: [{ button: '기능 설계 보기' }],
            },
          ],
        },
      ],
    },
    {
      key: 'extra',
      label: '고유 설정',
      blocks: [{ cards: [{ title: '쿠팡 고유 설정', items: [note('추가 플랫폼 고유 옵션은 이 탭에 모읍니다.')] }] }],
    },
  ],

  st11: [
    {
      key: 'api',
      label: 'API 관리',
      blocks: [
        {
          cards: [
            {
              title: '11번가 Open API 인증',
              help:
                '11번가에서 발급받은 API 인증정보를 입력합니다. · 실제 API 스펙 연결 시 공식 문서의 인증 필드 기준으로 최종 매핑합니다.',
              items: [
                field('API Key', 'st11.api_key', { placeholder: 'API Key', secret: true }),
                field('판매자 식별값', 'st11.seller_id', { placeholder: '필요한 경우 입력' }),
                API_ACTIONS,
              ],
            },
            apiStatusCard('st11', ['상품 조회', '상품 등록/수정', '주문 조회']),
          ],
        },
      ],
    },
    {
      key: 'shipping',
      label: '배송 관리',
      blocks: [{ cards: [{ title: '배송 관리', items: [toggle('공통 배송값 사용', 'st11.use_common_shipping')] }] }],
    },
    {
      key: 'product',
      label: '상품 / 카테고리',
      blocks: [
        {
          cards: [
            {
              title: '상품 / 카테고리',
              items: [toggle('실제 categoryId 후보 내 선택', 'st11.category_limit'), { button: '11번가 Policy 편집', variant: 'ai' }],
            },
          ],
        },
      ],
    },
    {
      key: 'extra',
      label: '고유 설정',
      blocks: [{ cards: [{ title: '11번가 고유 설정', items: [note('추가 설정은 이 탭에 모읍니다.')] }] }],
    },
  ],
};
