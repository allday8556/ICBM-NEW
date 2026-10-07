// 등록 대상 정책 (Gate 1 G1-A; ADR-0015 §2). The one Settings surface with a save contract: the
// registration target policy of one marketplace × canonical account.
//
// The page renders what the server holds and sends what the operator typed. It computes no
// revision, fingerprint, validity or readiness: the server validates the whole revision, creates
// its identity and fingerprint, and refuses a save that is invalid, unsafe, unchanged or made
// against a policy that moved since it was read. After every save the page re-reads the policy.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { toast } from '../../core/toast.js';

const BASE = '/api/v1/settings/target-policies';
// The seller's SmartStore address book, read from the connected account (never stored).
const ADDRESS_BOOKS = '/api/v1/connect/marketplaces/smartstore/addressbooks';
const ADDRESS_TYPE_LABEL = { RELEASE: '출고지', REFUND_OR_EXCHANGE: '반품·교환지', GENERAL: '일반' };
const TITLE = '등록 대상 정책';
const ACTOR = 'operator';

// The vocabulary the contract declares, shown as choices. Choosing one decides nothing.
const ROUNDING = ['CEIL_KRW_1'];
const LOOKUP_KEYS = ['SELLER_CODE', 'GTIN', 'OFFICIAL_KEY', 'NORMALIZED_NAME'];
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
  const body = error?.error;
  return body?.message ? `${body.message} (${body.code})` : String(error?.message ?? error);
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
    options.map((option) => h('option', { value: option }, option)),
  );
  if (value) select.value = value;
  select.disabled = !editable;
  return { select, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), select) };
}

// A whole number is sent as a number; anything else is sent as typed, so the server refuses it.
function whole(text) {
  return /^\d+$/.test(text.trim()) ? Number(text.trim()) : text;
}

// A server-owned revision reference: shown, never authorable here. The server stamps the
// owner's current revision into each revision it saves; an earlier revision holds none.
function serverOwned(label, value, name) {
  return h(
    'div',
    { class: 'kv', 'data-policy-reference': name },
    h('span', {}, label),
    h('span', { class: 'chip' }, value ?? '없음 · 서버 소유 리비전입니다. 정책을 새로 저장하면 서버가 기록합니다'),
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
  if (!current) return '저장된 정책 없음 · 등록 사전검사는 정책이 저장될 때까지 진행되지 않습니다';
  const at = new Date(current.authored_at).toLocaleString('ko-KR');
  return `현재 리비전 #${current.revision_no} · 지문 ${current.content_fingerprint.slice(0, 12)} · ${current.authored_by} · ${at}`;
}

function editor(view, onSaved) {
  const editable = view.editable === true;
  const inputs = view.inputs;
  const pricing = inputs?.pricing_context;
  const asset = inputs?.asset_policy;
  const delivery = inputs?.delivery_policy;
  const on = { editable };

  const taxonomy = field('카테고리 체계 리비전', inputs?.taxonomy_revision, { ...on, name: 'taxonomy_revision' });
  const sanitizer = field('정제 프로필 버전', inputs?.sanitizer_profile_version, { ...on, name: 'sanitizer_profile_version' });
  const feeTable = field('수수료표 버전', pricing?.fee_table_version, { ...on, name: 'fee_table_version' });
  const pricingPolicy = field('가격정책 버전', pricing?.pricing_policy_version, { ...on, name: 'pricing_policy_version' });
  const feeRate = field('수수료율 (예: 0.055)', pricing?.fee_rate, { ...on, name: 'fee_rate' });
  const feeFixed = field('고정 수수료 (원)', pricing ? String(pricing.fee_fixed_krw) : '', { ...on, name: 'fee_fixed_krw' });
  const otherRate = field('기타 비용률', pricing?.other_cost_rate, { ...on, name: 'other_cost_rate' });
  const otherFixed = field('기타 고정비 (원)', pricing ? String(pricing.other_cost_fixed_krw) : '', {
    ...on,
    name: 'other_cost_fixed_krw',
  });
  const pricingAccount = check('계정별 수수료·정책 (해제하면 계정 무관)', pricing?.account_id, {
    ...on,
    name: 'pricing_account_scoped',
  });
  const costRounding = choice('원가 반올림', ROUNDING, pricing?.cost_rounding, { ...on, name: 'cost_rounding' });
  const priceRounding = choice('판매가 반올림', ROUNDING, pricing?.price_rounding, { ...on, name: 'price_rounding' });
  const profile = field('이미지 프로필', asset?.profile, { ...on, name: 'asset_profile' });
  const minImages = field('최소 이미지 수', asset ? String(asset.min_images) : '', { ...on, name: 'min_images' });
  const maxImages = field('최대 이미지 수', asset ? String(asset.max_images) : '', { ...on, name: 'max_images' });
  const representative = check('대표 이미지 필수', asset?.requires_representative, { ...on, name: 'requires_representative' });
  const providerIdentity = check('마켓 이미지 식별자 필요', asset?.provider_asset_identity_required, {
    ...on,
    name: 'provider_asset_identity_required',
  });
  const physicalDelivery = check('실물 배송 정보 전송', Boolean(delivery), {
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
  const deliveryCompany = choice('택배사 (CJGLS = CJ대한통운)', DELIVERY_COMPANIES, delivery?.delivery_company ?? 'CJGLS', {
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
  const shippingAddress = field('출고지 주소록 번호', delivery ? String(delivery.shipping_address_id) : '', {
    ...on,
    name: 'shipping_address_id',
  });
  const returnAddress = field('반품·교환지 주소록 번호', delivery ? String(delivery.return_address_id) : '', {
    ...on,
    name: 'return_address_id',
  });
  // 네이버 주소록 불러오기: the connected account's address book becomes the choices of the two
  // number fields. A field already holding a number keeps it; an empty one takes the first entry
  // of its type. The server still validates whatever is saved.
  const addressList = h('div', { class: 'mini', 'data-address-books': 'NOT_LOADED' });
  const loadAddresses = h(
    'button',
    { type: 'button', class: 'btn', 'data-action': 'load-address-books' },
    '네이버 주소록 불러오기',
  );
  loadAddresses.disabled = !editable;
  loadAddresses.addEventListener('click', async () => {
    loadAddresses.disabled = true;
    try {
      const found = await getJson(ADDRESS_BOOKS);
      const books = found.address_books ?? [];
      const label = (book) =>
        `${book.address_book_no} · ${book.name || '(이름 없음)'} · ${ADDRESS_TYPE_LABEL[book.address_type] ?? book.address_type}`;
      for (const [input, type] of [
        [shippingAddress.input, 'RELEASE'],
        [returnAddress.input, 'REFUND_OR_EXCHANGE'],
      ]) {
        sequence += 1;
        const listId = `target-policy-${sequence}`;
        input.setAttribute('list', listId);
        input.after(
          h(
            'datalist',
            { id: listId },
            books.map((book) => h('option', { value: String(book.address_book_no) }, label(book))),
          ),
        );
        if (!input.value.trim()) {
          const first = books.find((book) => book.address_type === type);
          if (first) input.value = String(first.address_book_no);
        }
      }
      addressList.setAttribute('data-address-books', String(books.length));
      addressList.replaceChildren(
        ...(books.length
          ? books.map((book) => h('div', { 'data-address-book': String(book.address_book_no) }, label(book)))
          : [h('div', {}, '네이버 주소록에 등록된 주소가 없습니다.')]),
      );
    } catch (error) {
      toast(`주소록을 불러오지 못했습니다: ${errorText(error)}`);
    } finally {
      loadAddresses.disabled = !editable;
    }
  });
  sequence += 1;
  const templatesId = `target-policy-${sequence}`;
  const templates = h('textarea', { id: templatesId, rows: '3', spellcheck: 'false', 'data-policy-field': 'templates' });
  templates.value = templatesText(inputs?.templates);
  templates.disabled = !editable;
  const proofRequired = check('중복 확인 증거 필수', inputs?.duplicate_proof_required, {
    ...on,
    name: 'duplicate_proof_required',
  });
  const keys = LOOKUP_KEYS.map((key) => ({
    key,
    ...check(`조회 키 ${key}`, (inputs?.duplicate_lookup_keys ?? []).includes(key), { ...on, name: `lookup_${key}` }),
  }));

  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-target-policy' }, '등록 정책 저장');
  save.disabled = !editable;
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await sendJson('POST', `${BASE}/${view.marketplace_key}/${view.marketplace_account_id}/revisions`, {
        actor: ACTOR,
        expected_current_revision: view.current?.policy_revision ?? null,
        inputs: {
          taxonomy_revision: taxonomy.input.value.trim(),
          pricing_context: {
            marketplace_key: view.marketplace_key,
            account_id: pricingAccount.box.checked ? view.marketplace_account_id : null,
            fee_table_version: feeTable.input.value.trim(),
            pricing_policy_version: pricingPolicy.input.value.trim(),
            fee_rate: feeRate.input.value.trim(),
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
          templates: templatesOf(templates.value),
          duplicate_proof_required: proofRequired.box.checked,
          duplicate_lookup_keys: keys.filter((entry) => entry.box.checked).map((entry) => entry.key),
          // Server-owned references: never authored here; the server stamps them (ADR-0015 §2).
          category_mapping_revision: null,
          detail_composition_revision: null,
        },
      });
      toast(TITLE, '새 정책 리비전을 저장했습니다. 이후 등록 사전검사는 이 리비전으로 다시 평가됩니다.');
      await onSaved();
    } catch (error) {
      // Nothing was written: the server refuses a save whole. The typed values stay for a fix.
      toast(TITLE, errorText(error));
      save.disabled = !editable;
    }
  });

  return h(
    'div',
    { class: 'target-policy', 'data-account': view.marketplace_account_id },
    h('div', { class: 'kv' }, h('span', {}, '계정'), h('b', {}, view.marketplace_account_id)),
    h(
      'div',
      { class: 'kv' },
      h('span', {}, '저장 상태'),
      h('span', { class: view.current ? 'chip good' : 'chip', 'data-policy-state': view.current ? 'saved' : 'none' }, revisionLine(view.current)),
    ),
    h('div', { class: 'kv' }, h('span', {}, '리비전 이력'), h('b', { 'data-policy-history': String(view.history.length) }, `${view.history.length}개`)),
    taxonomy.row,
    serverOwned('카테고리 매핑 리비전', inputs?.category_mapping_revision, 'category_mapping_revision'),
    serverOwned('상세 구성 리비전', inputs?.detail_composition_revision, 'detail_composition_revision'),
    sanitizer.row,
    feeTable.row,
    pricingPolicy.row,
    feeRate.row,
    feeFixed.row,
    otherRate.row,
    otherFixed.row,
    pricingAccount.row,
    costRounding.row,
    priceRounding.row,
    profile.row,
    minImages.row,
    maxImages.row,
    representative.row,
    providerIdentity.row,
    physicalDelivery.row,
    deliveryType.row,
    deliveryAttribute.row,
    deliveryFeeType.row,
    baseFee.row,
    deliveryPayType.row,
    returnPriority.row,
    returnFee.row,
    exchangeFee.row,
    afterServicePhone.row,
    deliveryCompany.row,
    h('div', { class: 'form-row' }, loadAddresses, addressList),
    shippingAddress.row,
    returnAddress.row,
    h('div', { class: 'form-row' }, h('label', { for: templatesId }, '템플릿 (한 줄에 종류=식별자)'), templates),
    proofRequired.row,
    ...keys.map((key) => key.row),
    h('div', { class: 'api-action-row' }, save),
  );
}

export function targetPolicyPanel(marketplaceKey) {
  const host = h('div', { class: 'truth-host', 'data-target-policy': marketplaceKey });
  const load = async () => {
    try {
      const view = await getJson(`${BASE}/${marketplaceKey}`);
      host.replaceChildren(
        ...(view.accounts.length
          ? view.accounts.map((account) => editor(account, load))
          : [h('div', { class: 'note' }, '연결 대상으로 확정된 계정이 없습니다 · 계정을 확정한 뒤 정책을 저장할 수 있습니다')]),
      );
    } catch (error) {
      host.replaceChildren(h('span', { class: 'chip warn' }, `확인 불가 · ${errorText(error)}`));
    }
  };
  load();
  return host;
}
