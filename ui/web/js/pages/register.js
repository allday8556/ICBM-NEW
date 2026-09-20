// 등록관리: the Registration Management screen (M5 PR-F §B).
//
// Everything here is rendered from server-owned state. The page calculates no price, no
// readiness, no duplicate verdict, no option compatibility, no budget, no retry eligibility and
// no capability: each action arrives with the server's own `enabled` verdict and reason code, and
// pressing it calls the contract that decided it. An UNKNOWN outcome is never shown as FAILED, a
// 2xx is never shown as CONFIRMED, and an AUTH brake is never offered an operator resume — the
// server does not offer it, and this page renders only what it was given.
//
// It reuses the v29 panel/chip/table/kv system; the only new class is the requirement list of the
// canary panel. Explanations live behind the existing help icon, never as permanent subtitles.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { dotDateTime } from '../core/format.js';
import { withHelp } from '../core/help.js';
import { toast } from '../core/toast.js';
import { pageHead } from '../components/page-head.js';
import { emptyState, errorState } from '../components/states.js';

const SCREEN = '/api/v1/screens/register';
const OVERVIEW = '/api/v1/register/overview';
const CANARY = '/api/v1/register/canary';
const TITLE = '등록관리';
const HELP =
  '수집한 상품을 마켓에 등록하고, 등록 상태를 서버가 판단한 그대로 보여줍니다. 실행 가능 여부는 서버가 결정합니다.';
const CANARY_HELP =
  '실제 마켓 쓰기는 별도 승인이 필요한 제한 캠페인입니다. 이 영역은 준비 상태만 보여주며 아무 것도 승인하지 않습니다.';

const INTENT_LABEL = {
  PREPARED: '전송 준비됨',
  SENT: '전송됨',
  CONFIRMED: '확인 완료',
  UNKNOWN: '결과 미확인',
  FAILED: '전송 실패',
};

const INTENT_TONE = { CONFIRMED: 'good', UNKNOWN: 'warn', FAILED: 'bad' };

const VERIFICATION_LABEL = {
  NOT_VERIFIED: '읽기 확인 전',
  PASS: '읽기 확인 일치',
  MISMATCH: '읽기 확인 불일치',
};

const OUTCOME_LABEL = {
  APPLIED_PROVEN: '반영 확인됨',
  NOT_APPLIED_PROVEN: '미반영 확인됨',
  UNKNOWN: '결과 미확인',
};

const PREPARATION_LABEL = {
  DRAFTED: '초안',
  SNAPSHOT_FROZEN: '스냅샷 고정됨',
  INTENT_OPEN: '등록 요청 생성됨',
};

const PAUSE_LABEL = {
  AUTH: '인증 중단',
  POLICY: '정책 중단',
  FAILURE_BUDGET: '연속 실패 중단',
};

const ACTION_LABEL = {
  EVALUATE: 'Preflight 평가',
  FREEZE: '스냅샷 고정',
  CREATE_ENQUEUE: '등록 전송',
  RECONCILE: '결과 대조',
  VERIFY: '읽기 확인',
  RESUME_SCOPE: '전송 재개',
};

const OPERATOR = 'operator';

// Server reason codes rendered as copy. The page never derives a verdict, only its wording.
const REASON_COPY = {
  REGISTER_INTENT_ABSENT: '아직 등록 요청이 만들어지지 않았습니다.',
  REGISTER_INTENT_NOT_SENDABLE: '지금 상태에서는 서버가 전송을 허용하지 않습니다.',
  REGISTER_JOB_ALREADY_QUEUED: '이미 대기 중인 전송 작업이 있습니다.',
  REGISTER_SCOPE_PAUSED: '이 계정 범위의 전송이 중단되어 있습니다.',
  REGISTER_FAILURE_BUDGET_EXHAUSTED: '연속 실패 한도를 넘어 전송이 중단되었습니다.',
  REGISTER_SEND_REQUEST_ABSENT: '고정된 전송 요청이 없습니다.',
  REGISTER_NOT_UNKNOWN: '결과가 확정되어 대조할 대상이 없습니다.',
  REGISTER_NOT_APPLIED: '마켓 반영이 확인되지 않아 읽기 확인을 할 수 없습니다.',
  REGISTER_ALREADY_VERIFIED: '이미 읽기 확인을 마쳤습니다.',
  REGISTER_SCOPE_NOT_PAUSED: '중단된 범위가 없습니다.',
  REGISTER_SCOPE_RESUME_NOT_PERMITTED: '인증 중단은 재인증으로만 풀립니다.',
  REGISTER_ACCOUNT_NOT_BOUND: '판매자 계정 연결이 확인되지 않았습니다.',
  REGISTER_PREFLIGHT_INPUTS_NOT_DURABLE:
    '이 단위의 Preflight 입력이 서버에 남아 있지 않아 지금 평가를 보여줄 수 없습니다.',
  REGISTER_TARGET_POLICY_MISSING: '이 계정의 등록 정책이 아직 설정되지 않았습니다.',
  REGISTER_PREPARATION_ABSENT: '아직 등록 준비 내용이 저장되지 않았습니다.',
  REGISTER_PREFLIGHT_NOT_READY: 'Preflight가 READY가 아니어서 스냅샷을 고정할 수 없습니다.',
  REGISTER_UNIT_ALREADY_FROZEN: '이미 스냅샷이 고정된 단위입니다.',
  DUPLICATE_EVIDENCE_MISSING: '중복 조회 근거가 없습니다. 조회 계약이 아직 채택되지 않았습니다.',
  ENDPOINT_NOT_ADOPTED: '해당 마켓 연동 계약이 아직 채택되지 않았습니다.',
  PROOF_NOT_AVAILABLE_IN_PROCESS: '실행 중인 앱에서는 증명할 수 없는 항목입니다.',
  ACCOUNT_NOT_BOUND: '판매자 계정 연결이 확인되지 않았습니다.',
  AUTH_NOT_READY: '마켓 인증이 준비되지 않았습니다.',
  WRITE_SCOPE_NOT_PROVEN: '쓰기 권한 범위가 증명되지 않았습니다.',
  NO_PREPARED_INTENT: '전송 준비된 등록 요청이 없습니다.',
  UNRESOLVED_CONFLICT: '미해결 충돌이 남아 있습니다.',
  EXECUTION_SCOPE_STOPPED: '전송이 중단된 범위입니다.',
  MORE_THAN_ONE_UNIT_SELECTED: '카나리는 한 건만 대상으로 합니다.',
  RUNTIME_NOT_CLEAN: '실행 중인 코드가 정확한 커밋이 아닙니다.',
};

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value ?? '—'));
}

function chip(text, tone) {
  return h('span', { class: tone ? `chip ${tone}` : 'chip' }, text);
}

function reason(code) {
  return code ? h('div', { class: 'mini' }, REASON_COPY[code] ?? code) : null;
}

function table(headers, rows) {
  return h(
    'table',
    { class: 'table' },
    h('thead', {}, h('tr', {}, ...headers.map((label) => h('th', {}, label)))),
    h('tbody', {}, ...rows),
  );
}

function won(amount) {
  return amount === null || amount === undefined ? '—' : `${amount.toLocaleString('ko-KR')}원`;
}

function statusChip(status, codes) {
  if (!status) return '—';
  return fragment(
    chip(status, status === 'READY' ? 'good' : status === 'BLOCKED' ? 'bad' : 'warn'),
    ...(codes ?? []).map((code) => h('span', { class: 'mini', 'data-reason': code }, code)),
  );
}

function itemRow(item) {
  const assets = item.publication_assets ?? [];
  return h(
    'tr',
    { 'data-item': item.item_id, 'data-price-current': String(item.price_pin_current) },
    h(
      'td',
      {},
      item.item_id,
      ...(item.option_keys ?? []).map((key) => h('span', { class: 'mini', 'data-option': key }, key)),
    ),
    h('td', {}, won(item.sale_price_krw)),
    h('td', {}, item.price_basis ?? '—'),
    // The current M4 price of the same Item: a pin that is no longer current is visible here,
    // and whether it is current is the server's own verdict.
    h(
      'td',
      {},
      won(item.current_sale_price_krw),
      item.price_pin_current === false ? chip('고정가 아님', 'warn') : null,
    ),
    h('td', {}, statusChip(item.base_status, item.base_reason_codes)),
    h('td', {}, statusChip(item.pricing_status, item.pricing_reason_codes)),
    h('td', {}, item.registration_item_key ?? '—'),
    h(
      'td',
      { 'data-assets': String(assets.length) },
      ...assets.map((asset) =>
        h(
          'span',
          { class: 'mini', 'data-asset': asset.sha256, 'data-qa': asset.qa_verdict ?? 'NONE' },
          `${asset.role} ${asset.sha256.slice(0, 8)} ${asset.qa_verdict ?? 'QA 없음'}`,
        ),
      ),
    ),
  );
}

function fieldRow(field) {
  return h(
    'li',
    { 'data-field': field.key, 'data-provided': field.provided ? 'true' : 'false' },
    h('span', {}, field.key),
    chip(field.provided ? '입력됨' : '없음', field.provided ? 'good' : field.required ? 'bad' : 'warn'),
    field.required ? chip('필수') : null,
  );
}

function categoryBlock(category) {
  const fields = [...category.attributes, ...category.notice_fields];
  return h(
    'div',
    { class: 'register-category', 'data-category': category.category_id },
    kv('카테고리', category.category_id),
    kv('분류 리비전', category.taxonomy_revision),
    category.notice_type ? kv('정보고시', category.notice_type) : null,
    category.options_supported === null
      ? null
      : kv('옵션', category.options_supported ? `최대 ${category.max_options}개` : '미지원'),
    fields.length ? h('ul', { class: 'requirement-list' }, ...fields.map(fieldRow)) : null,
  );
}

function field(label, name, value, type = 'input') {
  const control = h(type, { name, value: value ?? '', rows: type === 'textarea' ? '3' : null });
  if (type === 'textarea') control.value = value ?? '';
  return h('label', { class: 'kv' }, h('span', {}, label), control);
}

// The authoring form of one provider-listing unit (§27). It collects the operator's own inputs
// and sends them; it computes no readiness and stores nothing of its own — the server keeps them.
function authoringForm(unit, onDone) {
  const authored = unit.authored;
  const inputs = authored?.inputs ?? {};
  const form = h(
    'form',
    { class: 'register-authoring', 'data-preparation': authored?.preparation_id ?? '' },
    h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 준비 입력'),
      authored ? chip(`리비전 ${authored.revision_no}`) : chip('미저장', 'warn')),
    field('카테고리', 'category_id', inputs.category?.category_id),
    field('상품명', 'name', inputs.name?.value),
    field('브랜드', 'brand', inputs.attributes?.brand?.value),
    field('제조사', 'manufacturer', inputs.notices?.manufacturer?.value),
    field('상세 본문', 'detail_body', inputs.detail_body, 'textarea'),
    h(
      'button',
      {
        type: 'submit',
        class: 'btn blue',
        'data-action': 'SAVE_PREPARATION',
        'data-unit': unit.unit_ref,
      },
      authored ? '준비 내용 저장' : '준비 내용 만들기',
    ),
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    const body = {
      actor: OPERATOR,
      item_ids: unit.items.map((item) => item.item_id),
      inputs: {
        category: values.category_id
          ? {
              category_id: values.category_id,
              mapping_revision: inputs.category?.mapping_revision ?? 'operator',
              taxonomy_revision: inputs.category?.taxonomy_revision ?? 'operator',
              confirmation: 'OPERATOR_CONFIRMED',
            }
          : null,
        name: values.name ? { value: values.name } : null,
        tags: inputs.tags ?? [],
        attributes: values.brand ? { brand: { value: values.brand } } : {},
        notices: values.manufacturer ? { manufacturer: { value: values.manufacturer } } : {},
        options: inputs.options ?? {},
        detail_composition_revision: inputs.detail_composition_revision ?? 'operator',
        detail_body: values.detail_body || null,
        detail_sections: inputs.detail_sections ?? ['BODY'],
      },
    };
    try {
      if (authored) {
        await sendJson('POST', `/api/v1/register/preparations/${authored.preparation_id}`, body);
      } else {
        await sendJson('POST', '/api/v1/register/preparations', { ...body, draft_id: unit.draft_id });
      }
      toast('등록 준비 내용을 저장했습니다');
      onDone();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast(REASON_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
    }
  });
  return form;
}

function preflightBlock(unit) {
  if (!unit.preflight) {
    return h(
      'div',
      { class: 'register-preflight', 'data-preflight': 'UNAVAILABLE' },
      kv('Preflight', '평가 없음'),
      reason(unit.preflight_unavailable_reason),
    );
  }
  const preflight = unit.preflight;
  return h(
    'div',
    { class: 'register-preflight', 'data-preflight': preflight.status },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('b', {}, 'Preflight'),
      chip(preflight.status, preflight.status === 'READY' ? 'good' : 'warn'),
      preflight.fingerprint_matches_snapshot === false ? chip('스냅샷과 다름', 'warn') : null,
    ),
    ...preflight.reason_codes.map((code) => h('div', { class: 'mini', 'data-reason': code }, code)),
  );
}

function attemptRow(attempt) {
  return h(
    'tr',
    {},
    h('td', {}, String(attempt.attempt_no)),
    h('td', {}, OUTCOME_LABEL[attempt.outcome] ?? '진행 중'),
    h('td', {}, attempt.error_class ?? '—'),
    h('td', {}, attempt.error_code ?? '—'),
    h('td', {}, attempt.started_at ? dotDateTime(attempt.started_at) : '—'),
  );
}

function scopeBlock(scope) {
  const paused = scope.state === 'PAUSED';
  return h(
    'div',
    { class: 'register-scope', 'data-scope-state': scope.state },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('b', {}, '전송 브레이크'),
      chip(paused ? (PAUSE_LABEL[scope.pause_reason] ?? '중단') : '전송 가능', paused ? 'bad' : 'good'),
    ),
    kv('연속 실패', String(scope.consecutive_failures)),
    kv('재개 세대', String(scope.resume_generation)),
    scope.paused_at ? kv('중단 시각', dotDateTime(scope.paused_at)) : null,
    scope.resumed_at ? kv('마지막 재개', dotDateTime(scope.resumed_at)) : null,
  );
}

async function run(unit, action, button, onDone) {
  button.disabled = true;
  try {
    await call(unit, action.action, unit.intent?.intent_id);
    toast(`${ACTION_LABEL[action.action] ?? action.action} 완료`);
    onDone();
  } catch (error) {
    const code = error instanceof ApiError ? error.error?.code : null;
    toast(REASON_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
    button.disabled = false;
  }
}

function call(unit, action, intentId) {
  if (action === 'RESUME_SCOPE') {
    return sendJson('POST', '/api/v1/register/scopes/resume', {
      marketplace_key: unit.scope.marketplace_key,
      marketplace_account_id: unit.scope.marketplace_account_id,
      actor: OPERATOR,
      reason: 'OPERATOR-REVIEWED',
    });
  }
  if (action === 'EVALUATE' || action === 'FREEZE') {
    const step = action === 'EVALUATE' ? 'evaluate' : 'freeze';
    return sendJson('POST', `/api/v1/register/preparations/${unit.authored.preparation_id}/${step}`, {
      actor: OPERATOR,
    });
  }
  const path = { CREATE_ENQUEUE: 'create', RECONCILE: 'reconcile', VERIFY: 'verify' }[action];
  return sendJson('POST', `/api/v1/register/intents/${intentId}/${path}`, {});
}

function actionCell(unit, action, onDone) {
  const button = h(
    'button',
    {
      type: 'button',
      class: action.enabled ? 'btn blue' : 'btn',
      disabled: !action.enabled,
      'data-action': action.action,
      'data-draft': unit.draft_id,
      'data-unit': unit.unit_ref,
      onclick: () => run(unit, action, button, onDone),
    },
    ACTION_LABEL[action.action] ?? action.action,
  );
  return h('div', { class: 'register-action' }, button, action.enabled ? null : reason(action.reason_code));
}

function unitPanel(unit, onDone) {
  const intent = unit.intent;
  return h(
    'section',
    {
      class: 'panel register-unit',
      'data-draft': unit.draft_id,
      'data-unit': unit.unit_ref,
      'data-preparation': unit.preparation,
    },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('h2', { class: 'panel-title' }, unit.unit_ref),
      chip(PREPARATION_LABEL[unit.preparation] ?? unit.preparation),
      intent ? chip(INTENT_LABEL[intent.state] ?? intent.state, INTENT_TONE[intent.state]) : null,
    ),
    kv('초안', unit.draft_id),
    kv('리스팅 형태', unit.listing_shape),
    kv('판매 계정', unit.marketplace_account_id),
    kv('계정 연결', unit.account_binding),
    kv('초안 리비전', String(unit.draft_revision)),
    unit.snapshot ? kv('리스팅 식별자', unit.snapshot.listing_identity) : null,
    unit.snapshot ? kv('스냅샷 지문', unit.snapshot.preflight_fingerprint.slice(0, 16)) : null,
    intent ? kv('검증 상태', VERIFICATION_LABEL[intent.verification_state] ?? intent.verification_state) : null,
    intent?.marketplace_product_id ? kv('마켓 상품번호', intent.marketplace_product_id) : null,
    unit.published_state ? kv('마켓 노출 상태', unit.published_state) : null,
    unit.conflicting_intents.length ? kv('충돌 중인 요청', String(unit.conflicting_intents.length)) : null,
    unit.category ? categoryBlock(unit.category) : null,
    unit.snapshot ? null : authoringForm(unit, onDone),
    unit.authored ? kv('준비 지문', unit.authored.inputs_fingerprint.slice(0, 16)) : null,
    preflightBlock(unit),
    unit.item_facts_unavailable_reason ? reason(unit.item_facts_unavailable_reason) : null,
    table(
      ['품목', '고정 판매가', '가격 근거', '현재 M4 판매가', '기본 준비', '가격 준비', '등록 품목 키', '이미지'],
      unit.items.map(itemRow),
    ),
    intent && intent.attempts.length
      ? table(['시도', '결과', '오류 분류', '오류 코드', '시작'], intent.attempts.map(attemptRow))
      : null,
    scopeBlock(unit.scope),
    h('div', { class: 'supplier-actions' }, ...unit.actions.map((action) => actionCell(unit, action, onDone))),
  );
}

function canaryPanel(canary) {
  const blocked = canary.verdict === 'BLOCKED';
  return h(
    'section',
    { class: 'panel register-canary', 'data-canary': canary.verdict },
    h(
      'div',
      { class: 'supplier-head-row' },
      withHelp(h('h2', { class: 'panel-title' }, '실거래 카나리 준비도'), CANARY_HELP),
      chip(blocked ? '차단됨' : '준비됨', blocked ? 'bad' : 'good'),
    ),
    kv('실행 모드', canary.execution_mode),
    kv('쓰기 권한 상태', canary.write_status),
    h(
      'ul',
      { class: 'requirement-list' },
      ...canary.requirements.map((item) =>
        h(
          'li',
          { 'data-requirement': item.requirement, 'data-satisfied': item.satisfied ? 'true' : 'false' },
          h('span', {}, item.requirement),
          chip(item.satisfied ? '충족' : '미충족', item.satisfied ? 'good' : 'warn'),
          item.endpoint_id && !item.satisfied ? h('span', { class: 'mini' }, item.endpoint_id) : null,
          item.satisfied ? null : reason(item.reason_code),
        ),
      ),
    ),
  );
}

export default {
  key: 'register',
  title: TITLE,
  navLabel: TITLE,
  icon: '▱',
  async render(ctx) {
    const head = pageHead({ title: TITLE, help: HELP });
    let screen;
    let overview;
    let canary;
    try {
      [screen, overview, canary] = await Promise.all([
        getJson(SCREEN),
        getJson(OVERVIEW),
        getJson(CANARY),
      ]);
    } catch (error) {
      return fragment(head, errorState(error));
    }
    const reload = () => ctx.navigate('register');
    if (!overview.units.length) {
      return fragment(
        head,
        canaryPanel(canary),
        emptyState({
          title: '등록 후보가 없습니다',
          copy: '통합DB에서 등록 가능한 상품을 선택하면 Preflight를 거쳐 등록할 수 있습니다.',
          action: { label: '통합DB 보기', onSelect: () => ctx.navigate('db') },
        }),
      );
    }
    return fragment(
      head,
      h(
        'div',
        { class: 'register-summary' },
        kv('등록 후보', String(screen.registration_candidates_total)),
        kv('등록 완료', String(screen.registrations_total)),
        kv('중단된 범위', String(overview.paused_scopes.length)),
      ),
      canaryPanel(canary),
      ...overview.units.map((unit) => unitPanel(unit, reload)),
    );
  },
};
