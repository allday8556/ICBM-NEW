// Structure of the settings screen, reproduced from the approved v29 prototype. Generic fields
// come from the settings contract (`policy_values`); explicitly owned operator panels such as
// SmartStore and Coupang credentials use their own narrow API contracts.

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

// The v29 AI Prompt Registry as Settings shows it (ADR-0026 §3, §8). Each card opens the layered
// prompt editor on the role, platform policy and task the prototype pairs it with; the text and
// revision of every entry come from the server's prompt stores.
const MD = 'ROLE_PRODUCT_MD_V1';
const BUNDLE = 'TASK_PRODUCT_RECOMMEND_BUNDLE_V1';
const REMATCH = 'TASK_CATEGORY_REMATCH_V1';
const REGISTRY = {
  roles: [
    { label: '수집 검증 AI', help: '수집한 가격·배송비·옵션 검증', role: 'ROLE_DATA_VALIDATOR_V1', task: 'TASK_EXTRACT_CORRECT_V1' },
    { label: '상품/MD AI', help: '상품명·태그·카테고리·옵션 정리', role: MD, task: BUNDLE },
    { label: '커머스 운영 AI', help: '주문 상태·다음 처리·운영 안내', role: 'ROLE_COMMERCE_OPS_V1', task: 'TASK_ORDER_RESPONSE_ASSIST_V1' },
    { label: 'CS 응대 AI', help: '문의 분석·고객 답변 초안', role: 'ROLE_CS_V1', task: 'TASK_INQUIRY_REPLY_V1' },
    { label: '판매상태 검증 AI', help: '구매 버튼·품절 표시로 판매 상태 확인', role: 'ROLE_STOCK_VALIDATOR_V1', task: 'TASK_SOLDOUT_JUDGMENT_V1' },
  ],
  policies: [
    { label: '스마트스토어 정책', help: '추천/제외 태그 · 검색 최적화 · 공식 데이터 우선', policy: 'POLICY_NAVER_V1', role: MD, task: BUNDLE },
    { label: '쿠팡 정책', help: '카테고리 추천 → AI 재검증 · 필수 옵션 확인', policy: 'POLICY_COUPANG_V1', role: MD, task: REMATCH },
    { label: '11번가 정책', help: '11번가 카테고리 범위 · 등록 정책', policy: 'POLICY_11ST_V1', role: MD, task: REMATCH },
  ],
  tasks: [
    { label: '추출 보정', help: '수집관리', role: 'ROLE_DATA_VALIDATOR_V1', task: 'TASK_EXTRACT_CORRECT_V1' },
    { label: '통합 상품 추천', help: '통합DB', role: MD, task: BUNDLE },
    { label: '카테고리 재추천', help: '등록관리', role: MD, task: REMATCH },
    { label: '주문 운영 보조', help: '주문관리', role: 'ROLE_COMMERCE_OPS_V1', task: 'TASK_ORDER_RESPONSE_ASSIST_V1' },
    { label: '문의 답변', help: '문의관리', role: 'ROLE_CS_V1', task: 'TASK_INQUIRY_REPLY_V1' },
    { label: '품절 판정', help: '품절확인', role: 'ROLE_STOCK_VALIDATOR_V1', task: 'TASK_SOLDOUT_JUDGMENT_V1' },
  ],
};

function apiStatusCard(marketplace, capabilities) {
  return {
    title: '연동 상태 / 권한',
    items: [
      { status: '현재 상태', marketplace },
      ...capabilities.map((label) => ({ status: label, chip: '준비 중' })),
    ],
  };
}

function shippingOverrideCard(prefix) {
  return {
    title: '배송 관리',
    items: [
      toggle('공통 배송값 사용', `${prefix}.use_common_shipping`),
      field('배송비 (따로 정하기)', `${prefix}.shipping_fee`),
      field('출고일 (따로 정하기)', `${prefix}.dispatch_days`),
      field('출고지 (따로 정하기)', `${prefix}.origin`),
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
        { inheritNote: { bold: '적용 방법', text: '각 마켓의 배송 관리에서 「공통 배송값 사용」이 켜져 있으면 이 값을 씁니다.' } },
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
                { flow: ['상품 실제 정보', '수집한 상품 정보', '마켓 필수값', '공통 기본값'] },
                note('상품 실제 사실과 충돌하는 공통값은 자동 적용하지 않고 확인필요로 보냅니다.'),
              ],
            },
          ],
        },
      ],
    },
    {
      // ADR-0033 G3: the store-wide top and bottom notices of the detail page, saved through the
      // Detail Guidance owner's own contract (detail-guidance.js).
      key: 'guidance',
      label: '상세페이지 공지',
      blocks: [
        {
          card: {
            title: '상세페이지 공지',
            full: true,
            help:
              '상세페이지 맨 위(상단 공지)와 맨 아래(하단 공지)에 들어가는 공지입니다. 입력한 문구로 공지 이미지를 만들며, 저장한 공지는 모든 상품의 기본값이 됩니다. · 기간 공지는 시작부터 끝까지만 들어갑니다. · 문구에는 웹 주소나 태그를 쓸 수 없습니다.',
            items: [{ detailGuidance: true }],
          },
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
                // 2026-10-16: the target margin is the pricing owner's fixed 35% (TARGET_NET_MARGIN);
                // no setting changes it yet, so the note never claims one does.
                note(
                  '목표 마진은 지금 35%로 고정되어 있으며 아직 바꿀 수 없습니다. 판매가는 매입원가·배송비와 스마트스토어 › 등록 정책의 수수료 설정으로 자동 계산됩니다.',
                ),
              ],
            },
          ],
        },
      ],
    },
    {
      // ADR-0038 §3, §5: the 해외 구매대행 settings, saved through their own versioned contract,
      // and the Korea Eximbank TTS with its write-only API key (overseas.js). Nothing is priced here.
      key: 'overseas',
      label: '해외구매대행',
      blocks: [
        {
          cards: [
            {
              title: '해외 구매대행',
              help: '해외 구매대행 상품의 원가 계산과 등록에 쓰는 기본값입니다. 저장할 때마다 변경 기록이 남고, 가격은 저장된 값으로 계산합니다.',
              items: [{ overseasSettings: true }],
            },
            {
              title: '환율',
              help: '한국수출입은행의 송금 보낼 때 환율을 영업일 11시 이후 하루 한 번 받아 둡니다. 3영업일보다 오래되었거나 없으면 가격을 계산하지 않습니다.',
              items: [{ fxRates: true }],
            },
          ],
        },
      ],
    },
    {
      key: 'ai',
      label: 'AI',
      blocks: [
        {
          cards: [
            {
              title: 'AI 기본 설정',
              items: [
                { aiCapability: true },
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
            title: 'AI 공급자',
            full: true,
            help: '내 PC에서 실행하는 AI 연결 프로그램(CLIProxyAPI)으로 AI를 씁니다. 실행 파일·연결 설정·상품 정보 전송을 승인해야 연결됩니다.',
            items: [{ aiProvider: true }],
          },
        },
        {
          card: {
            title: 'AI 프롬프트',
            full: true,
            help: 'AI에게 보내는 지시문입니다. 공통 규칙 → 역할 → 마켓 정책 → 작업 → 상품 정보 순서로 합쳐집니다.',
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
                '커머스API센터에서 발급한 애플리케이션 ID와 시크릿을 입력합니다. · 저장하면 아래에서 계정 확인을 다시 해야 합니다. · 시크릿은 이 PC에 안전하게 저장되며 다시 표시되지 않습니다. · 판매자 계정은 입력값이 아니라 로그인한 계정으로 정해집니다.',
              items: [{ smartstoreCredentials: true }],
            },
            {
              // CAPABILITY_MAPPING §14.11 surface 1 (M2 PR-D): live capability truth, not demo rows.
              title: '연동 상태 / 권한',
              help:
                '로그인(인증)·등록 권한·실제 등록을 따로 보여줍니다. · ● 자동으로 확인됨 · ◐ 직접 확인함 · ○ 아직 확인 안 됨 · 직접 확인한 권한은 이후에 바뀌어도 자동으로 알 수 없습니다. · 실제 등록은 첫 등록이 확인된 뒤 표시됩니다.',
              items: [{ capabilityProjection: 'smartstore' }, { workflowActions: 'smartstore' }],
            },
            {
              // M2 PR-E, instructions §8A: observation and first binding are separate actions.
              title: '계정 확인 / 연결 대상 확정',
              help:
                '「계정 확인」은 로그인한 판매자 계정을 보여줄 뿐 연결하지 않습니다. · 처음 연결할 때는 보여준 계정이 맞는지 확인한 뒤 「이 계정 연결」을 누릅니다. · 이미 연결된 계정은 자동으로 바뀌지 않습니다.',
              items: [{ smartstoreAccount: true }],
            },
            {
              title: 'API 연동 확인',
              help:
                'ICBM이 쓰는 스마트스토어 연동 방식을 직접 확인했다고 기록합니다. 처음 연결할 때 한 번 「직접 확인함」으로 저장해야 계정을 연결할 수 있습니다. · 네이버가 확인한 것이 아니라 내가 확인한 기록입니다.',
              items: [{ contractReview: 'smartstore' }],
            },
            {
              title: '등록 권한 확인',
              help:
                '커머스API센터에서 켜 둔 API 그룹을 체크해 기록합니다. · 내가 직접 확인한 기록이며 네이버가 자동으로 확인한 것이 아닙니다. · 30일이 지나면 다시 확인해야 합니다.',
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
              '판매 계정별 등록 정책입니다. · 저장하면 이후 등록부터 적용되고, 이미 등록 내용을 확정한 상품은 그때의 정책을 그대로 씁니다. · 잘못된 값이 하나라도 있으면 아무것도 저장되지 않습니다.',
            items: [{ targetPolicy: 'smartstore' }],
          },
        },
      ],
    },
    {
      key: 'shipping',
      label: '배송 관리',
      // The delivery half of the same registration target policy (one revision, two tabs).
      blocks: [
        {
          card: {
            title: '배송 정책',
            full: true,
            help:
              '등록할 때 스마트스토어에 보내는 배송·반품·교환 정보입니다. · 등록 정책과 함께 저장됩니다. · 출고지와 반품·교환지는 연결된 계정의 네이버 주소록에서 고릅니다.',
            items: [{ targetPolicy: 'smartstore', section: 'delivery' }],
          },
        },
      ],
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
                toggle('네이버 카테고리 범위 안에서만 선택', 'smartstore.category_limit'),
                toggle('AI 카테고리 검증', 'smartstore.category_ai'),
                { promptEditor: { label: '네이버 정책 프롬프트 편집', policy: 'POLICY_NAVER_V1', role: MD, task: REMATCH } },
              ],
            },
          ],
        },
        {
          // Gate 1 G1-B (ADR-0015 §3): the operator-reviewed category metadata, saved through its
          // own contract. No provider category endpoint is adopted.
          card: {
            title: '카테고리 정보',
            full: true,
            help:
              '카테고리별 필수 속성·상품정보고시·옵션·템플릿 규칙을 판매자센터에서 확인한 대로 기록합니다. · 확인 완료로 저장한 카테고리만 등록할 수 있습니다. · AI 제안은 확인 완료로 저장할 수 없습니다.',
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
                toggle('태그 ID로 저장', 'smartstore.tag_id'),
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
      blocks: [{ cards: [{ title: '스마트스토어 고유 설정', items: [note('아직 설정할 수 있는 항목이 없습니다.')] }] }],
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
                '쿠팡 Wing에서 발급받은 API 정보를 한 번 입력합니다. · 키는 이 PC에 안전하게 저장되며 다시 표시되지 않습니다.',
              items: [{ coupangCredentials: true }],
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
                toggle('AI로 상품 정보 다시 확인', 'coupang.ai_validate'),
                toggle('확신이 낮은 추천은 자동 적용 안 함', 'coupang.low_confidence_block'),
                { promptEditor: { label: '쿠팡 정책 프롬프트 편집', policy: 'POLICY_COUPANG_V1', role: MD, task: REMATCH } },
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
                toggle('공급처 옵션 코드 유지', 'coupang.preserve_sku'),
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
              items: [note('쿠팡 그로스 연동은 준비 중입니다.')],
            },
          ],
        },
      ],
    },
    {
      key: 'extra',
      label: '고유 설정',
      blocks: [{ cards: [{ title: '쿠팡 고유 설정', items: [note('아직 설정할 수 있는 항목이 없습니다.')] }] }],
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
              help: '11번가 연동은 준비 중입니다.',
              items: [
                field('API 키', 'st11.api_key', { secret: true }),
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
              items: [
                toggle('11번가 카테고리 후보 안에서만 선택', 'st11.category_limit'),
                { promptEditor: { label: '11번가 정책 프롬프트 편집', policy: 'POLICY_11ST_V1', role: MD, task: REMATCH } },
              ],
            },
          ],
        },
      ],
    },
    {
      key: 'extra',
      label: '고유 설정',
      blocks: [{ cards: [{ title: '11번가 고유 설정', items: [note('아직 설정할 수 있는 항목이 없습니다.')] }] }],
    },
  ],
};
