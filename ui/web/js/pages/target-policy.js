// 등록 대상 정책 (Gate 1 G1-A; ADR-0015 §2). The one Settings surface with a save contract: the
// registration target policy of one marketplace × canonical account.
//
// The page renders what the server holds and sends what the operator typed. It computes no
// revision, fingerprint, validity or readiness: the server validates the whole revision, creates
// its identity and fingerprint, and refuses a save that is invalid, unsafe, unchanged or made
// against a policy that moved since it was read. After every save the page re-reads the policy.

import { getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { toast } from '../core/toast.js';

const BASE = '/api/v1/settings/target-policies';
const TITLE = '등록 대상 정책';
const ACTOR = 'operator';

// The vocabulary the contract declares, shown as choices. Choosing one decides nothing.
const ROUNDING = ['CEIL_KRW_1'];
const LOOKUP_KEYS = ['SELLER_CODE', 'GTIN', 'OFFICIAL_KEY', 'NORMALIZED_NAME'];

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

// A server-owned revision reference with no owner yet: shown, never authorable here.
function unowned(label, value, name) {
  return h(
    'div',
    { class: 'kv', 'data-policy-reference': name },
    h('span', {}, label),
    h('span', { class: 'chip' }, value ?? '없음 · 서버 소유 리비전이 아직 없어 입력할 수 없습니다'),
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
          templates: templatesOf(templates.value),
          duplicate_proof_required: proofRequired.box.checked,
          duplicate_lookup_keys: keys.filter((entry) => entry.box.checked).map((entry) => entry.key),
          // Server-owned references with no owner yet: never authored here (ADR-0015 §2).
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
    unowned('카테고리 매핑 리비전', inputs?.category_mapping_revision, 'category_mapping_revision'),
    unowned('상세 구성 리비전', inputs?.detail_composition_revision, 'detail_composition_revision'),
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
