// 등록 대상 정책 (Gate 1 G1-A; ADR-0015 §2). The one Settings surface with a save contract: the
// registration target policy of one marketplace × canonical account.
//
// The page renders what the server holds and sends what the operator typed. It computes no
// revision, fingerprint, validity or readiness: the server validates the whole revision, creates
// its identity and fingerprint, and refuses a save that is invalid, unsafe, unchanged or made
// against a policy that moved since it was read. After every save the page re-reads the policy.
// The one sum it forms is the fee rate of the fee preset (판매수수료 + 주문관리수수료), exactly,
// and the server checks that sum again.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { errorText as plainError } from '../../core/errors.js';
import { closeModal, openModal } from '../../core/modal.js';
import { toast } from '../../core/toast.js';

const BASE = '/api/v1/settings/target-policies';
// The current leaf catalog's taxonomy revision: one entry is enough to read it.
const CATALOG = '/api/v1/settings/category-catalog';
// The read-only fee reference table (참고값 · 기준일 · 출처) the fee preset fills from.
const FEE_REFERENCE = '/api/v1/settings/fee-reference';
// The preset's vocabulary in Korean; the rates themselves come only from the server's table.
const STORE_TYPE_LABEL = { SMART_STORE: '스마트스토어', BRAND_STORE: '브랜드스토어' };
const SELLER_GRADE_LABEL = { GENERAL: '일반', SMB3: '중소3', SMB2: '중소2', SMB1: '중소1', SMALL: '영세' };
// The seller's SmartStore address book, read from the connected account (never stored).
const ADDRESS_BOOKS = '/api/v1/connect/marketplaces/smartstore/addressbooks';
const ADDRESS_TYPE_LABEL = { RELEASE: '출고지', REFUND_OR_EXCHANGE: '반품·교환지', GENERAL: '일반' };
const TITLE = '등록 대상 정책';
const ACTOR = 'operator';

// The vocabulary the contract declares, shown as choices in Korean. Choosing one decides nothing.
const ROUNDING = ['CEIL_KRW_1'];
const LOOKUP_KEYS = ['SELLER_CODE', 'GTIN', 'OFFICIAL_KEY', 'NORMALIZED_NAME'];
const LOOKUP_LABEL = { SELLER_CODE: '판매자 상품코드', GTIN: '바코드(GTIN)', OFFICIAL_KEY: '공식 상품번호', NORMALIZED_NAME: '상품명' };
const OPTION_LABEL = {
  CEIL_KRW_1: '1원 단위 올림',
  DELIVERY: '택배·소포·등기',
  NORMAL: '일반 배송',
  PAID: '유료',
  PREPAID: '선결제',
  PRIMARY: '기본 반품 택배사',
  CJGLS: 'CJ대한통운',
};
const ERROR_COPY = {
  TARGET_POLICY_INVALID: '입력값을 확인하세요. 잘못된 값이 있어 저장하지 않았습니다.',
  TARGET_POLICY_PRICING_CONTEXT_INVALID: '수수료·가격 설정을 확인하세요. 잘못된 값이 있어 저장하지 않았습니다.',
  TARGET_POLICY_UNSAFE_CONTENT: '입력값에 쓸 수 없는 내용(주소·키 등)이 있어 저장하지 않았습니다.',
  TARGET_POLICY_UNCHANGED: '바뀐 값이 없습니다.',
  TARGET_POLICY_CURRENT_MOVED: '다른 곳에서 정책이 바뀌었습니다. 새로 불러온 값을 확인한 뒤 다시 저장하세요.',
  TARGET_POLICY_ACCOUNT_UNKNOWN: '연결된 판매 계정을 찾을 수 없습니다.',
  TARGET_POLICY_AUTHORING_REVISION_UNOWNED: '자동으로 기록되는 값은 직접 바꿀 수 없습니다.',
};
const DELIVERY_TYPES = ['DELIVERY'];
const DELIVERY_ATTRIBUTES = ['NORMAL'];
const DELIVERY_FEE_TYPES = ['PAID'];
const DELIVERY_PAY_TYPES = ['PREPAID'];
const RETURN_CARRIER_PRIORITIES = ['PRIMARY'];
// The outbound courier codes a real SmartStore CREATE has accepted (CJGLS = CJ대한통운, canary
// 2026-10-05). Another code is added only once it is proven the same way.
const DELIVERY_COMPANIES = ['CJGLS'];

let sequence = 0;

function errorText(error) {
  return plainError(error, ERROR_COPY);
}

function field(label, value, { editable, name }) {
  sequence += 1;
  const id = `target-policy-${sequence}`;
  const input = h('input', {
    id,
    type: 'text',
    autocomplete: 'off',
    spellcheck: 'false',
    value: value ?? '',
    'data-policy-field': name,
  });
  input.disabled = !editable;
  return { input, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), input) };
}

function check(label, checked, { editable, name }) {
  sequence += 1;
  const id = `target-policy-${sequence}`;
  const box = h('input', { id, type: 'checkbox', 'data-policy-field': name });
  box.checked = Boolean(checked);
  box.disabled = !editable;
  return { box, row: h('label', { class: 'kv', for: id }, h('span', {}, label), box) };
}

function choice(label, options, value, { editable, name }) {
  sequence += 1;
  const id = `target-policy-${sequence}`;
  const select = h(
    'select',
    { id, 'data-policy-field': name },
    options.map((option) => h('option', { value: option }, OPTION_LABEL[option] ?? option)),
  );
  if (value) select.value = value;
  select.disabled = !editable;
  return { select, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), select) };
}

// A whole number is sent as a number; anything else is sent as typed, so the server refuses it.
function whole(text) {
  return /^\d+$/.test(text.trim()) ? Number(text.trim()) : text;
}

// Exact fee arithmetic, never floats: a rate is held as whole units of 1e-8 (a fraction with up
// to 8 decimals, a percent with up to 6). The server re-checks the sum exactly.
function unitsOfPercent(text) {
  const match = /^(\d{1,2})(?:\.(\d{1,6}))?$/.exec((text ?? '').trim());
  return match ? BigInt(match[1] + (match[2] ?? '').padEnd(6, '0')) : null;
}

function unitsOfRate(text) {
  const match = /^0(?:\.(\d{1,8}))?$/.exec(text ?? '');
  return match ? BigInt((match[1] ?? '').padEnd(8, '0')) : null;
}

function decimalText(units, digits) {
  const padded = units.toString().padStart(digits + 1, '0');
  const fraction = padded.slice(-digits).replace(/0+$/, '');
  const whole = String(BigInt(padded.slice(0, -digits)));
  return fraction ? `${whole}.${fraction}` : whole;
}

const rateText = (units) => decimalText(units, 8);
const percentText = (units) => decimalText(units, 6);

// A server-owned revision reference: shown, never authorable here. The server stamps the
// owner's current revision into each revision it saves; an earlier revision holds none.
function serverOwned(label, value, name) {
  return h(
    'div',
    { class: 'kv', 'data-policy-reference': name },
    h('span', {}, label),
    h('span', { class: 'chip' }, value ? '기록됨' : '자동으로 기록됩니다 (정책을 저장하면 채워집니다)'),
  );
}

function templatesText(templates) {
  return Object.entries(templates ?? {})
    .map(([kind, identity]) => `${kind}=${identity}`)
    .join('\n');
}

function templatesOf(text) {
  const map = {};
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const at = trimmed.indexOf('=');
    map[at < 0 ? trimmed : trimmed.slice(0, at).trim()] = at < 0 ? '' : trimmed.slice(at + 1).trim();
  }
  return map;
}

function revisionLine(current) {
  if (!current) return '아직 저장한 정책이 없습니다. 정책을 저장해야 등록할 수 있습니다';
  return `저장됨 · ${new Date(current.authored_at).toLocaleString('ko-KR')}`;
}

// 수수료 프리셋 (2026-10-16 owner direction): the seller picks 스토어 유형 and 판매자 등급 once and
// the two fee rates fill from the server's reference table; any number can still be changed, and a
// changed one is marked 「직접 입력」. The saved pricing_context.fee_rate is their exact sum (the
// pricing rule charges fee_rate × 판매가 + 건당 고정 수수료, so the two percentages add up to one
// rate); the preset itself is saved beside it only so this form shows it again.
function feePreset(marketplaceKey, pricing, saved, editable) {
  sequence += 1;
  const base = `target-policy-${sequence}`;
  const select = (name, labels, value) => {
    const element = h(
      'select',
      { id: `${base}-${name}`, 'data-policy-field': name },
      h('option', { value: '' }, '고르세요'),
      Object.entries(labels).map(([key, label]) => h('option', { value: key }, label)),
    );
    element.value = value ?? '';
    element.disabled = !editable;
    return element;
  };
  const storeType = select('fee_store_type', STORE_TYPE_LABEL, saved?.store_type);
  const sellerGrade = select('fee_seller_grade', SELLER_GRADE_LABEL, saved?.seller_grade);
  const percentField = (name, rate) => {
    const units = unitsOfRate(rate);
    const input = h('input', {
      id: `${base}-${name}`,
      type: 'text',
      inputmode: 'decimal',
      autocomplete: 'off',
      spellcheck: 'false',
      value: units === null ? '' : percentText(units),
      'data-policy-field': name,
    });
    input.disabled = !editable;
    const mark = h('span', { class: 'chip', 'data-fee-source': name });
    return { input, mark };
  };
  const sales = percentField('sales_fee_percent', saved?.sales_fee_rate);
  const order = percentField('order_fee_percent', saved?.order_fee_rate);
  const total = h('div', { class: 'kv', 'data-fee-total': '' });
  const legacy = h('div', { class: 'mini', 'data-fee-legacy': '' });
  const referenceLine = h('div', { class: 'mini', 'data-fee-reference': 'NOT_LOADED' }, '수수료 참고표를 불러오는 중입니다.');
  const marketingLine = h('div', { class: 'mini', 'data-fee-marketing': '' });
  const shippingHint = h('div', { class: 'mini', 'data-fee-hint': 'shipping' });
  let reference = null;

  const storeRow = () => reference?.store_types.find((entry) => entry.store_type === storeType.value) ?? null;
  const gradeRow = () => reference?.seller_grades.find((entry) => entry.seller_grade === sellerGrade.value) ?? null;
  const untouched = () => !storeType.value && !sellerGrade.value && !sales.input.value.trim() && !order.input.value.trim();

  // 참고값 when the number is the chosen preset's reference value, otherwise 직접 입력.
  const markOf = ({ input, mark }, referenceRate, missingCopy) => {
    const typed = unitsOfPercent(input.value);
    const expected = unitsOfRate(referenceRate);
    let state = 'MANUAL';
    if (!input.value.trim()) state = 'EMPTY';
    else if (typed === null) state = 'INVALID';
    else if (expected !== null && typed === expected) state = 'REFERENCE';
    mark.setAttribute('data-fee-state', state);
    mark.className = { REFERENCE: 'chip good', MANUAL: 'chip info', EMPTY: 'chip warn', INVALID: 'chip warn' }[state];
    mark.textContent = {
      REFERENCE: '참고값',
      MANUAL: '직접 입력',
      EMPTY: missingCopy,
      INVALID: '숫자로 적어 주세요 (예: 3.63)',
    }[state];
  };

  const refresh = () => {
    const store = storeRow();
    const grade = gradeRow();
    markOf(sales, store?.sales_fee_rate, '입력 필요');
    markOf(order, grade?.order_fee_rate, grade && !grade.order_fee_rate ? '참고값 없음 · 판매자센터에서 확인해 입력해 주세요' : '입력 필요');
    const salesUnits = unitsOfPercent(sales.input.value);
    const orderUnits = unitsOfPercent(order.input.value);
    if (salesUnits !== null && orderUnits !== null) {
      total.setAttribute('data-fee-total', rateText(salesUnits + orderUnits));
      total.replaceChildren(
        h('span', {}, '합계 수수료율'),
        h('b', {}, `판매수수료 ${percentText(salesUnits)}% + 주문관리수수료 ${percentText(orderUnits)}% = ${percentText(salesUnits + orderUnits)}%`),
      );
    } else {
      total.setAttribute('data-fee-total', '');
      total.replaceChildren(h('span', {}, '합계 수수료율'), h('span', { class: 'chip warn' }, '스토어 유형과 판매자 등급을 고르거나 두 수수료를 입력해 주세요'));
    }
    // A policy saved before the preset existed holds one typed total; it stays until the seller
    // fills the two parts.
    const savedUnits = unitsOfRate(pricing?.fee_rate);
    legacy.textContent =
      !saved && pricing?.fee_rate && untouched()
        ? `지금 저장된 수수료율은 ${savedUnits === null ? pricing.fee_rate : `${percentText(savedUnits)}%`}입니다 (예전 방식으로 한 번에 입력한 값). 스토어 유형과 판매자 등급을 고르면 나눠서 채워집니다.`
        : '';
    marketingLine.textContent = store?.marketing_link_sales_fee_rate
      ? `판매수수료는 일반 유입 기준입니다. 판매자센터 마케팅 링크로 들어온 주문은 ${percentText(unitsOfRate(store.marketing_link_sales_fee_rate))}%로 더 낮지만, 등록할 때는 유입 경로를 알 수 없어 높은 쪽으로 계산합니다.`
      : '';
    // The pricing rule charges fees on the sale price only; the order fee on the buyer's shipping
    // is the seller's to add as a fixed other cost. The example uses the typed order fee.
    const example =
      orderUnits === null
        ? ''
        : ` (예: 배송비 3,000원 × ${percentText(orderUnits)}% ≈ ${((3000n * orderUnits + 99_999_999n) / 100_000_000n).toLocaleString('ko-KR')}원)`;
    shippingHint.textContent = `가격 계산은 판매가에만 수수료를 붙입니다. 주문관리수수료는 고객이 내는 배송비에도 붙으므로, 그 몫까지 넣으려면 「건당 기타 비용」에 원 단위로 더해 주세요${example}.`;
  };

  storeType.addEventListener('change', () => {
    const store = storeRow();
    if (store) sales.input.value = store.sales_fee_rate ? percentText(unitsOfRate(store.sales_fee_rate)) : '';
    refresh();
  });
  sellerGrade.addEventListener('change', () => {
    const grade = gradeRow();
    if (grade) order.input.value = grade.order_fee_rate ? percentText(unitsOfRate(grade.order_fee_rate)) : '';
    refresh();
  });
  sales.input.addEventListener('input', refresh);
  order.input.addEventListener('input', refresh);
  refresh();

  getJson(`${FEE_REFERENCE}/${encodeURIComponent(marketplaceKey)}`)
    .then((table) => {
      reference = table;
      for (const option of sellerGrade.options) {
        const grade = table.seller_grades.find((entry) => entry.seller_grade === option.value);
        if (grade) option.textContent = `${grade.label} (${grade.revenue_band})`;
      }
      referenceLine.setAttribute('data-fee-reference', table.version);
      referenceLine.replaceChildren(
        `참고값 · 기준일 ${table.reference_date} · 출처: `,
        ...table.sources.flatMap((source, index) => [
          index ? ', ' : '',
          h('a', { href: source.url, target: '_blank', rel: 'noopener noreferrer' }, source.label),
        ]),
        table.official ? '' : ' (판매자 블로그·계산기 정리이며 네이버 공식 공지가 아닙니다)',
      );
      refresh();
    })
    .catch(() => {
      referenceLine.setAttribute('data-fee-reference', 'UNAVAILABLE');
      referenceLine.textContent = '수수료 참고표를 불러오지 못했습니다. 두 수수료를 직접 입력해 주세요.';
    });

  const labelled = (label, control, mark = null) =>
    h(
      'div',
      { class: 'form-row' },
      h('label', { for: control.id }, label),
      mark ? h('div', { class: 'fee-input' }, control, mark) : control,
    );
  const rows = [
    h('h4', { class: 'policy-advanced-title' }, '마켓 수수료'),
    labelled('스토어 유형', storeType),
    labelled('판매자 등급 (네이버페이 매출 구간)', sellerGrade),
    labelled('판매수수료 (%)', sales.input, sales.mark),
    marketingLine,
    labelled('네이버페이 주문관리수수료 (%)', order.input, order.mark),
    total,
    legacy,
    referenceLine,
    h('div', { class: 'mini', 'data-fee-hint': 'notice' }, '판매자센터 공지와 다르면 직접 고쳐 주세요. 고친 값은 「직접 입력」으로 표시됩니다.'),
    shippingHint,
  ];

  // What a save sends: the exact sum as fee_rate and the preset beside it. Untouched, a policy
  // saved before the preset keeps its typed total. Anything incomplete is sent as typed (an empty
  // rate), so the server refuses the whole save.
  const compose = () => {
    if (untouched()) return { fee_rate: pricing?.fee_rate ?? '', fee_preset: null };
    const salesUnits = unitsOfPercent(sales.input.value);
    const orderUnits = unitsOfPercent(order.input.value);
    if (salesUnits === null || orderUnits === null) return { fee_rate: '', fee_preset: null };
    return {
      fee_rate: rateText(salesUnits + orderUnits),
      fee_preset: {
        store_type: storeType.value || null,
        seller_grade: sellerGrade.value || null,
        reference_version:
          storeType.value || sellerGrade.value ? (reference?.version ?? saved?.reference_version ?? null) : null,
        sales_fee_rate: rateText(salesUnits),
        order_fee_rate: rateText(orderUnits),
      },
    };
  };
  return { rows, compose };
}

// One policy revision, shown in two tabs: 'policy' (등록 정책) and 'delivery' (배송 관리). Every
// field is built from the server's current inputs; a tab shows only its own rows, and a save
// sends the other tab's fields unchanged, so the revision is always whole.
function editor(view, onSaved, section, accountLabel = '스마트스토어 판매 계정') {
  const editable = view.editable === true;
  const inputs = view.inputs;
  const pricing = inputs?.pricing_context;
  const asset = inputs?.asset_policy;
  const delivery = inputs?.delivery_policy;
  const on = { editable };

  const taxonomy = field('카테고리 체계 버전', inputs?.taxonomy_revision, { ...on, name: 'taxonomy_revision' });
  const sanitizer = field('상품정보 정리 규칙 버전', inputs?.sanitizer_profile_version, { ...on, name: 'sanitizer_profile_version' });
  // 2026-10-16: the two pricing labels are the server's, stamped from the saved fees, and the fee
  // rate comes from the fee preset (판매수수료 + 주문관리수수료). The delivery tab sends the saved
  // fee rate and preset unchanged.
  const fees =
    section === 'delivery'
      ? { rows: [], compose: () => ({ fee_rate: pricing?.fee_rate ?? '', fee_preset: inputs?.fee_preset ?? null }) }
      : feePreset(view.marketplace_key, pricing, inputs?.fee_preset ?? null, editable);
  const feeFixed = field('건당 고정 수수료 (원, 스마트스토어는 0)', pricing ? String(pricing.fee_fixed_krw) : '', { ...on, name: 'fee_fixed_krw' });
  const otherRate = field('기타 비용률 (카드·광고 등, 소수로: 2%면 0.02, 없으면 0)', pricing?.other_cost_rate, { ...on, name: 'other_cost_rate' });
  const otherFixed = field('건당 기타 비용 (원, 없으면 0)', pricing ? String(pricing.other_cost_fixed_krw) : '', {
    ...on,
    name: 'other_cost_fixed_krw',
  });
  const pricingAccount = check('이 판매 계정에만 적용', pricing?.account_id, {
    ...on,
    name: 'pricing_account_scoped',
  });
  const costRounding = choice('원가 원 단위 처리', ROUNDING, pricing?.cost_rounding, { ...on, name: 'cost_rounding' });
  const priceRounding = choice('판매가 원 단위 처리', ROUNDING, pricing?.price_rounding, { ...on, name: 'price_rounding' });
  // 최신 카테고리 체계로 맞추기: the current Naver leaf catalog's taxonomy revision, offered with one
  // click and applied only after the seller confirms it in the dialog; it is saved with the policy.
  const taxonomyState = h('div', { class: 'mini', 'data-taxonomy-state': 'NOT_LOADED' });
  const alignTaxonomy = h(
    'button',
    { type: 'button', class: 'btn', 'data-action': 'align-taxonomy' },
    '최신 카테고리 체계로 맞추기',
  );
  alignTaxonomy.disabled = true;
  let latestTaxonomy = null;
  const showTaxonomyState = () => {
    const typed = taxonomy.input.value.trim();
    const state = !latestTaxonomy ? 'NO_CATALOG' : typed === latestTaxonomy ? 'CURRENT' : 'BEHIND';
    taxonomyState.setAttribute('data-taxonomy-state', state);
    taxonomyState.textContent = {
      NO_CATALOG: '받아 둔 네이버 카테고리 목록이 없습니다. 설정 › 스마트스토어 › 상품 / 카테고리에서 동기화하세요.',
      CURRENT: '지금 네이버 카테고리 목록과 같습니다.',
      BEHIND: '받아 둔 네이버 카테고리 목록과 다릅니다. 「최신 카테고리 체계로 맞추기」를 누르고 저장하세요.',
    }[state];
    alignTaxonomy.disabled = !editable || state !== 'BEHIND';
  };
  taxonomy.input.addEventListener('input', showTaxonomyState);
  if (section !== 'delivery') {
    getJson(`${CATALOG}/${encodeURIComponent(view.marketplace_key)}?limit=1`)
      .then((catalog) => {
        latestTaxonomy = catalog.taxonomy_revision ?? null;
        showTaxonomyState();
      })
      .catch(() => {
        taxonomyState.textContent = '네이버 카테고리 목록을 확인하지 못했습니다.';
      });
  }
  alignTaxonomy.addEventListener('click', () => {
    const apply = h('button', { type: 'button', class: 'btn blue', 'data-action': 'confirm-align-taxonomy' }, '맞추기');
    apply.addEventListener('click', () => {
      taxonomy.input.value = latestTaxonomy;
      showTaxonomyState();
      closeModal();
      toast(TITLE, '카테고리 체계를 맞췄습니다. 「등록 정책 저장」을 눌러야 적용됩니다.');
    });
    openModal({
      eyebrow: TITLE,
      title: '최신 카테고리 체계로 맞출까요?',
      narrow: true,
      body: h(
        'div',
        { 'data-role': 'align-taxonomy-confirm' },
        h('p', {}, '등록 정책의 카테고리 체계를 받아 둔 네이버 카테고리 목록에 맞춥니다.'),
        h(
          'p',
          { class: 'mini' },
          '맞춘 뒤에는 새 체계로 기록한 카테고리 등록 정보만 쓰입니다. 이전 체계로 기록한 카테고리는 다시 준비해야 합니다. 「등록 정책 저장」을 눌러야 적용됩니다.',
        ),
      ),
      footer: [apply],
    });
  });
  const profile = field('이미지 프로필', asset?.profile, { ...on, name: 'asset_profile' });
  const minImages = field('최소 이미지 수', asset ? String(asset.min_images) : '', { ...on, name: 'min_images' });
  const maxImages = field('최대 이미지 수', asset ? String(asset.max_images) : '', { ...on, name: 'max_images' });
  const representative = check('대표 이미지 필수', asset?.requires_representative, { ...on, name: 'requires_representative' });
  const providerIdentity = check('마켓에 올린 이미지만 사용', asset?.provider_asset_identity_required, {
    ...on,
    name: 'provider_asset_identity_required',
  });
  const physicalDelivery = check('배송 정보 사용', Boolean(delivery), {
    ...on,
    name: 'physical_delivery',
  });
  const deliveryType = choice('배송 방법', DELIVERY_TYPES, delivery?.delivery_type, { ...on, name: 'delivery_type' });
  const deliveryAttribute = choice('배송 속성', DELIVERY_ATTRIBUTES, delivery?.delivery_attribute_type, {
    ...on,
    name: 'delivery_attribute_type',
  });
  const deliveryFeeType = choice('배송비 유형', DELIVERY_FEE_TYPES, delivery?.delivery_fee_type, {
    ...on,
    name: 'delivery_fee_type',
  });
  const baseFee = field('기본 배송비 (원)', delivery ? String(delivery.base_fee_krw) : '', {
    ...on,
    name: 'base_fee_krw',
  });
  const deliveryPayType = choice('배송비 결제', DELIVERY_PAY_TYPES, delivery?.delivery_fee_pay_type, {
    ...on,
    name: 'delivery_fee_pay_type',
  });
  const afterServicePhone = field('A/S 전화번호 (예: 02-000-0000)', inputs?.after_service_telephone ?? '', {
    ...on,
    name: 'after_service_telephone',
  });
  const deliveryCompany = choice('택배사', DELIVERY_COMPANIES, delivery?.delivery_company ?? 'CJGLS', {
    ...on,
    name: 'delivery_company',
  });
  const returnPriority = choice(
    '반품 택배사 우선순위',
    RETURN_CARRIER_PRIORITIES,
    delivery?.return_delivery_company_priority_type,
    { ...on, name: 'return_delivery_company_priority_type' },
  );
  const returnFee = field('반품 배송비 (원)', delivery ? String(delivery.return_delivery_fee_krw) : '', {
    ...on,
    name: 'return_delivery_fee_krw',
  });
  const exchangeFee = field('교환 배송비 (원)', delivery ? String(delivery.exchange_delivery_fee_krw) : '', {
    ...on,
    name: 'exchange_delivery_fee_krw',
  });
  const shippingAddress = field('출고지', delivery ? String(delivery.shipping_address_id) : '', {
    ...on,
    name: 'shipping_address_id',
  });
  const returnAddress = field('반품·교환지', delivery ? String(delivery.return_address_id) : '', {
    ...on,
    name: 'return_address_id',
  });
  // 네이버 주소록: the connected account's address book turns the two number fields into
  // dropdowns of named entries. It loads by itself when the editor opens and again on the button;
  // without a CONNECT session the number fields stay as they are. A field holding a number keeps
  // it (shown even when the book no longer lists it); an empty one takes the first entry of its
  // type. The server still validates whatever is saved.
  const addressList = h('div', { class: 'mini', 'data-address-books': 'NOT_LOADED' });
  const loadAddresses = h(
    'button',
    { type: 'button', class: 'btn', 'data-action': 'load-address-books' },
    '네이버 주소록 불러오기',
  );
  loadAddresses.disabled = !editable;
  const label = (book) =>
    `${book.name || '(이름 없음)'} · ${ADDRESS_TYPE_LABEL[book.address_type] ?? book.address_type} · ${book.address_book_no}`;
  const applyAddressBooks = (books) => {
    addressList.setAttribute('data-address-books', String(books.length));
    if (!books.length) {
      // Nothing to choose from: the number fields stay for manual entry.
      addressList.textContent = '네이버 주소록에 등록된 주소가 없습니다.';
      return;
    }
    for (const [input, type] of [
      [shippingAddress.input, 'RELEASE'],
      [returnAddress.input, 'REFUND_OR_EXCHANGE'],
    ]) {
      const name = input.getAttribute('data-policy-field');
      // A second load replaces the earlier dropdown instead of stacking another one.
      input.parentElement?.querySelector(`select[data-address-select='${name}']`)?.remove();
      if (!input.value.trim()) {
        const first = books.find((book) => book.address_type === type);
        if (first) input.value = String(first.address_book_no);
      }
      // Entries of the field's own type first, then the rest, each group in the book's order.
      const ordered = [
        ...books.filter((book) => book.address_type === type),
        ...books.filter((book) => book.address_type !== type),
      ];
      const current = input.value.trim();
      const options = ordered.map((book) => h('option', { value: String(book.address_book_no) }, label(book)));
      if (current && !ordered.some((book) => String(book.address_book_no) === current)) {
        options.unshift(h('option', { value: current }, `${current} · 주소록에 없는 번호`));
      }
      if (!current) options.unshift(h('option', { value: '' }, '주소를 고르세요'));
      const select = h('select', { 'data-address-select': name }, options);
      select.value = current;
      select.disabled = !editable;
      select.addEventListener('change', () => {
        input.value = select.value;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      });
      input.hidden = true;
      input.after(select);
    }
    addressList.textContent = `네이버 주소록 ${books.length}개를 불러왔습니다.`;
  };
  const fetchAddressBooks = async ({ quiet }) => {
    loadAddresses.disabled = true;
    try {
      const found = await getJson(ADDRESS_BOOKS);
      applyAddressBooks(found.address_books ?? []);
    } catch (error) {
      if (quiet) {
        addressList.textContent = '스마트스토어 연결 후 주소록을 불러올 수 있습니다 (API 관리 › 계정 확인).';
      } else {
        toast('주소록을 불러오지 못했습니다', errorText(error));
      }
    } finally {
      loadAddresses.disabled = !editable;
    }
  };
  loadAddresses.addEventListener('click', () => fetchAddressBooks({ quiet: false }));
  if (editable && section === 'delivery') fetchAddressBooks({ quiet: true });
  sequence += 1;
  const templatesId = `target-policy-${sequence}`;
  const templates = h('textarea', { id: templatesId, rows: '3', spellcheck: 'false', 'data-policy-field': 'templates' });
  templates.value = templatesText(inputs?.templates);
  templates.disabled = !editable;
  const proofRequired = check('등록 전 중복 확인 필수', inputs?.duplicate_proof_required, {
    ...on,
    name: 'duplicate_proof_required',
  });
  const keys = LOOKUP_KEYS.map((key) => ({
    key,
    ...check(`중복 확인 기준: ${LOOKUP_LABEL[key] ?? key}`, (inputs?.duplicate_lookup_keys ?? []).includes(key), { ...on, name: `lookup_${key}` }),
  }));

  const save = h(
    'button',
    { type: 'button', class: 'btn blue', 'data-action': 'save-target-policy' },
    section === 'delivery' ? '배송 정책 저장' : '등록 정책 저장',
  );
  save.disabled = !editable;
  save.addEventListener('click', async () => {
    save.disabled = true;
    const composed = fees.compose();
    try {
      await sendJson('POST', `${BASE}/${view.marketplace_key}/${view.marketplace_account_id}/revisions`, {
        actor: ACTOR,
        expected_current_revision: view.current?.policy_revision ?? null,
        inputs: {
          taxonomy_revision: taxonomy.input.value.trim(),
          pricing_context: {
            marketplace_key: view.marketplace_key,
            account_id: pricingAccount.box.checked ? view.marketplace_account_id : null,
            // Server-owned labels: sent as null, stamped from the saved fees.
            fee_table_version: null,
            pricing_policy_version: null,
            fee_rate: composed.fee_rate,
            fee_fixed_krw: whole(feeFixed.input.value),
            other_cost_rate: otherRate.input.value.trim(),
            other_cost_fixed_krw: whole(otherFixed.input.value),
            cost_rounding: costRounding.select.value,
            price_rounding: priceRounding.select.value,
          },
          sanitizer_profile_version: sanitizer.input.value.trim(),
          asset_policy: {
            profile: profile.input.value.trim(),
            min_images: whole(minImages.input.value),
            max_images: whole(maxImages.input.value),
            requires_representative: representative.box.checked,
            provider_asset_identity_required: providerIdentity.box.checked,
          },
          delivery_policy: physicalDelivery.box.checked
            ? {
                delivery_type: deliveryType.select.value,
                delivery_attribute_type: deliveryAttribute.select.value,
                delivery_company: deliveryCompany.select.value,
                delivery_fee_type: deliveryFeeType.select.value,
                base_fee_krw: whole(baseFee.input.value),
                delivery_fee_pay_type: deliveryPayType.select.value,
                return_delivery_company_priority_type: returnPriority.select.value,
                return_delivery_fee_krw: whole(returnFee.input.value),
                exchange_delivery_fee_krw: whole(exchangeFee.input.value),
                shipping_address_id: whole(shippingAddress.input.value),
                return_address_id: whole(returnAddress.input.value),
              }
            : null,
          // The seller's A/S contact the marketplace requires on every listing (null when empty).
          after_service_telephone: afterServicePhone.input.value.trim() || null,
          // The fee preset the fee rate was composed from (null: the saved total is kept as is).
          fee_preset: composed.fee_preset,
          templates: templatesOf(templates.value),
          duplicate_proof_required: proofRequired.box.checked,
          duplicate_lookup_keys: keys.filter((entry) => entry.box.checked).map((entry) => entry.key),
          // Server-owned references: never authored here; the server stamps them (ADR-0015 §2).
          category_mapping_revision: null,
          detail_composition_revision: null,
        },
      });
      toast(TITLE, '등록 정책을 저장했습니다. 이후 등록부터 적용됩니다.');
      await onSaved();
    } catch (error) {
      // Nothing was written: the server refuses a save whole. The typed values stay for a fix.
      toast(TITLE, errorText(error));
      save.disabled = !editable;
    }
  });

  // What a seller sets first; the version and identity fields the marketplace setup needs follow
  // under their own heading.
  const policyRows = [
    ...fees.rows,
    feeFixed.row,
    otherRate.row,
    otherFixed.row,
    costRounding.row,
    priceRounding.row,
    minImages.row,
    maxImages.row,
    representative.row,
    afterServicePhone.row,
    h('h4', { class: 'policy-advanced-title' }, '고급 설정 · 처음 한 번 입력하면 보통은 바꿀 필요가 없습니다'),
    pricingAccount.row,
    taxonomy.row,
    h('div', { class: 'form-row' }, taxonomyState, alignTaxonomy),
    sanitizer.row,
    profile.row,
    providerIdentity.row,
    h('div', { class: 'form-row' }, h('label', { for: templatesId }, '스마트스토어 템플릿 번호 (한 줄에 종류=번호)'), templates),
    proofRequired.row,
    ...keys.map((key) => key.row),
    serverOwned('수수료 변경 기록', pricing?.fee_table_version, 'fee_table_version'),
    serverOwned('가격 규칙 변경 기록', pricing?.pricing_policy_version, 'pricing_policy_version'),
    serverOwned('카테고리 연결 버전', inputs?.category_mapping_revision, 'category_mapping_revision'),
    serverOwned('상세페이지 구성 버전', inputs?.detail_composition_revision, 'detail_composition_revision'),
  ];
  const deliveryRows = [
    physicalDelivery.row,
    deliveryType.row,
    deliveryAttribute.row,
    deliveryCompany.row,
    deliveryFeeType.row,
    baseFee.row,
    deliveryPayType.row,
    returnPriority.row,
    returnFee.row,
    exchangeFee.row,
    h('div', { class: 'form-row' }, loadAddresses, addressList),
    shippingAddress.row,
    returnAddress.row,
  ];

  return h(
    'div',
    { class: 'target-policy', 'data-account': view.marketplace_account_id, 'data-policy-section': section },
    h('div', { class: 'kv' }, h('span', {}, '판매 계정'), h('b', {}, accountLabel)),
    h(
      'div',
      { class: 'kv' },
      h('span', {}, '저장 상태'),
      h(
        'span',
        {
          class: view.current ? 'chip good' : 'chip',
          'data-policy-state': view.current ? 'saved' : 'none',
          'data-policy-revision': view.current ? String(view.current.revision_no) : '',
          'data-policy-fingerprint': view.current?.content_fingerprint ?? '',
        },
        revisionLine(view.current),
      ),
    ),
    h('div', { class: 'kv' }, h('span', {}, '저장한 횟수'), h('b', { 'data-policy-history': String(view.history.length) }, `${view.history.length}번`)),
    ...(section === 'delivery' ? deliveryRows : policyRows),
    h('div', { class: 'api-action-row' }, save),
  );
}

export function targetPolicyPanel(marketplaceKey, section = 'policy') {
  const host = h('div', { class: 'truth-host', 'data-target-policy': marketplaceKey });
  const load = async () => {
    try {
      const view = await getJson(`${BASE}/${marketplaceKey}`);
      host.replaceChildren(
        ...(view.accounts.length
          ? view.accounts.map((account, index) =>
              editor(account, load, section, view.accounts.length > 1 ? `스마트스토어 판매 계정 ${index + 1}` : '스마트스토어 판매 계정'),
            )
          : [h('div', { class: 'note' }, '연결된 스마트스토어 계정이 없습니다. API 관리에서 계정을 연결한 뒤 저장할 수 있습니다.')]),
      );
    } catch (error) {
      host.replaceChildren(h('span', { class: 'chip warn' }, `확인 불가 · ${errorText(error)}`));
    }
  };
  load();
  return host;
}
