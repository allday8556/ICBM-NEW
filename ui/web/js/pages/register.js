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
import { openModal } from '../core/modal.js';
import { markInert } from '../core/inert.js';
import { platformTag } from '../core/platform.js';
import { pageHead } from '../components/page-head.js';
import { emptyState, errorState } from '../components/states.js';
import { reviewItemsBlock } from '../components/review-items.js';
import { KIND_LABEL } from '../components/review-counts.js';
import { READ_STATE_TONE } from '../components/registration-status.js';
import { itemImagesEditor } from '../components/item-images.js';
import { listingSyncPanel } from '../components/listing-sync.js';

const SCREEN = '/api/v1/screens/register';
const OVERVIEW = '/api/v1/register/overview';
const CANARY = '/api/v1/register/canary';
const LIVE = '/api/v1/register/live';
// B-UX1: the readiness of every pre-send unit, evaluated by the server now.
const READINESS = '/api/v1/register/readiness';
const READINESS_STATUSES = ['READY', 'REVIEW_REQUIRED', 'BLOCKED', 'DUPLICATE', 'STALE', 'NOT_EVALUATED'];
// B-UX2: what the operator can fix or re-check now, and where (server-owned).
const FIXES = '/api/v1/screens/register/fixes';
const FIX_ACTIONABILITY = ['FIX_AVAILABLE', 'RECHECK', 'WAITING', 'NO_OPERATOR_ACTION', 'NOT_IMPLEMENTED'];
const TITLE = '등록관리';
const HELP =
  '수집한 상품을 마켓에 등록하고, 등록 상태를 서버가 판단한 그대로 보여줍니다. 실행 가능 여부는 서버가 결정합니다.';
const CANARY_HELP =
  '실제 마켓 쓰기는 별도 승인이 필요한 제한 캠페인입니다. 이 영역은 준비 상태만 보여주며 아무 것도 승인하지 않습니다.';

const STATUS_HELP =
  '등록 상태는 서버가 한 가지 기준으로 판정한 등록중 · 등록성공 · 재확인필요 · 등록실패입니다. 결과를 모르는 등록은 실패로 표시하지 않고, 다시 보내지 않은 채 등록 여부를 확인합니다.';

const LIVE_HELP =
  '보호 쓰기 브레이크와 실행 권한(grant)은 서버가 가진 상태 그대로입니다. 준비도는 서버가 계산한 결과이며, 이 화면에서 아무 것도 승인하거나 기록하지 않습니다.';

const BRAKE_LABEL = { ENGAGED: '잠김', RELEASED: '해제됨' };
const GRANT_LABEL = { ACTIVE: '유효', EXPIRED: '만료됨', REVOKED: '회수됨', EXHAUSTED: '소진됨' };
const STAGE_LABEL = { ASSET: '이미지 업로드', CREATE: '상품 등록', DELETE: '상품 삭제', DISPATCH: '발송처리' };
const PROOF_LABEL = {
  evidence_retention_ready: '증거 보존 증명',
  visual_acceptance_recorded: '화면 검수 기록',
};

export const INTENT_LABEL = {
  PREPARED: '전송 준비됨',
  SENT: '전송됨',
  CONFIRMED: '확인 완료',
  UNKNOWN: '결과 미확인',
  FAILED: '전송 실패',
};

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

export const PREPARATION_LABEL = {
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
  RECONCILE: '등록확인 재시도',
  VERIFY: '읽기 확인',
  RESUME_SCOPE: '전송 재개',
  SETTLE_REJECTION: '거절 확인 · 미등록 처리',
};

const OPERATOR = 'operator';

// Server reason codes rendered as copy. The page never derives a verdict, only its wording.
export const REASON_COPY = {
  // ADR-0031 §4.2, ADR-0030 §7: what the supplier's own source forbids.
  SOURCE_CHANNEL_FORBIDDEN: '공급사가 이 마켓 판매를 금지한 상품입니다',
  SOURCE_CHANNEL_UNRESOLVED: '공급사 판매채널 문구를 확인할 수 없습니다',
  SUPPLIER_NOT_ACTIVE: '공급사 사이트가 아직 시험 단계(RECON)라 등록할 수 없습니다',
  // B-PRICE1: the repin command's refusals.
  REGISTER_DRAFT_REVISION_MOVED: '초안이 바뀌었습니다. 화면을 새로 고친 뒤 다시 시도하세요.',
  REGISTER_REPIN_INTENT_OPEN: '이 초안의 등록 요청이 아직 진행 중이라 가격을 다시 고정할 수 없습니다.',
  REGISTER_DRAFT_ITEM_NOT_PRICED: 'M4가 일부 품목의 가격을 정하지 못해 아무것도 다시 고정하지 않았습니다.',
  REGISTER_READ_QUEUED: '전송 작업이 대기 중입니다.',
  REGISTER_READ_SENDABLE: '전송할 수 있는 상태입니다.',
  REGISTER_READ_SEND_IN_FLIGHT: '전송이 진행 중입니다.',
  REGISTER_READ_AWAITING_VERIFICATION: '마켓 반영이 확인되어 읽기 확인을 기다리고 있습니다.',
  REGISTER_READ_RETRY_SCHEDULED: '자동 재시도가 예약되어 있습니다.',
  REGISTER_READ_VERIFIED: '읽기 확인이 스냅샷과 일치했습니다.',
  REGISTER_READ_OUTCOME_UNKNOWN: '전송 결과를 확인하지 못했습니다. 다시 보내지 않고 등록 여부를 확인합니다.',
  REGISTER_READ_READBACK_MISMATCH: '마켓에 반영된 내용이 스냅샷과 다릅니다.',
  REGISTER_READ_VERIFICATION_OVERDUE: '반영 후 읽기 확인이 기한 안에 끝나지 않았습니다.',
  REGISTER_READ_NOT_APPLIED: '마켓에 반영되지 않은 것이 확인되었습니다.',
  REGISTER_READ_PRE_SEND_FAILED: '전송 전에 거부되어 아무 것도 보내지 않았습니다.',
  REGISTER_READ_STATE_UNCLASSIFIED: '서버가 이 등록 상태를 분류하지 못했습니다. 결함으로 기록되었습니다.',
  // B-STATUS: the latest Attempt's cause and the reconcile result, shown apart.
  SMARTSTORE_HTTP_400: '네이버가 400으로 거절했습니다.',
  SMARTSTORE_CREATE_REJECTED: '네이버가 등록 요청을 거절했습니다 (등록되지 않음).',
  ZERO: '등록 확인: 네이버에서 찾지 못함',
  MULTIPLE: '등록 확인: 후보가 여러 개',
  LOOKUP_UNAVAILABLE: '등록 확인: 조회 불가',
  ERROR: '등록 확인: 조회 오류',
  REGISTER_REJECTION_NOT_PROVEN: '마지막 시도가 네이버의 400 거절이 아니라 미등록으로 처리할 수 없습니다.',
  REGISTER_REJECTION_LOOKUP_MISSING: '거절 뒤 등록확인 재시도에서 "찾지 못함"이 한 번 있어야 미등록으로 처리할 수 있습니다.',
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
  REGISTER_FROZEN_METADATA_UNRESOLVED:
    '스냅샷이 고정한 카테고리 메타데이터 리비전을 찾을 수 없어 규칙을 표시하지 않습니다. 현재 리비전으로 대신 표시하지 않습니다.',
  DUPLICATE_EVIDENCE_MISSING: '중복 조회 근거가 없습니다. 조회 계약이 아직 채택되지 않았습니다.',
  // ADR-0033 G4: a resolved top or bottom notice that no composition can place until G5.
  PUBLICATION_GUIDANCE_UNPLACED: '상·하단 공지는 아직 등록에 넣을 수 없습니다 (G5 이후). 이 상품에서 그 공지를 끄면 이 사유는 사라집니다.',
  GUIDANCE_TEXT_INVALID: '쓸 수 없는 공지 문구입니다. 상세페이지 단계의 직접 작성 문구를 확인하세요.',
  GUIDANCE_TEXT_TOO_WIDE: '공지 문구 중 템플릿 글상자보다 넓은 줄이 있습니다.',
  GUIDANCE_TEMPLATE_UNKNOWN: '알 수 없는 공지 템플릿입니다.',
  REGISTER_GUIDANCE_CHOICE_INVALID: '직접 작성한 공지는 템플릿과 문구가 모두 있어야 합니다.',
  REGISTER_GUIDANCE_UNAVAILABLE: '상세페이지 공지를 그릴 수 있는 서버 구성 요소가 연결되어 있지 않습니다.',
  ENDPOINT_NOT_ADOPTED: '해당 마켓 연동 계약이 아직 채택되지 않았습니다.',
  PROOF_NOT_AVAILABLE_IN_PROCESS: '실행 중인 앱에서는 증명할 수 없는 항목입니다.',
  ACCOUNT_NOT_BOUND: '판매자 계정 연결이 확인되지 않았습니다.',
  AUTH_NOT_READY: '마켓 인증이 준비되지 않았습니다.',
  WRITE_SCOPE_NOT_PROVEN: '쓰기 권한 범위가 증명되지 않았습니다.',
  NO_PREPARED_INTENT: '전송 준비된 등록 요청이 없습니다.',
  READBACK_SESSION_NOT_WIRED: '등록 결과를 되읽을 수 있는 마켓 세션이 연결되어 있지 않습니다.',
  PUBLISHED_STATE_UNPROVEN: '되읽기 계약이 판매 상태를 증명하지 못해 등록 성공을 확정할 수 없습니다.',
  UNRESOLVED_CONFLICT: '미해결 충돌이 남아 있습니다.',
  EXECUTION_SCOPE_STOPPED: '전송이 중단된 범위입니다.',
  MORE_THAN_ONE_UNIT_SELECTED: '카나리는 한 건만 대상으로 합니다.',
  ASSET_MUTATION_NOT_READY: '이미지 업로드 단계 준비도가 READY가 아닙니다.',
  CREATE_MUTATION_NOT_READY: '등록 전송 단계 준비도가 READY가 아닙니다.',
  RUNTIME_NOT_CLEAN: '실행 중인 코드가 정확한 커밋이 아닙니다.',
  LIVE_EXECUTION_MODE_NOT_LIVE: '실행 모드가 LIVE가 아닙니다.',
  LIVE_PROTECTED_WRITE_BRAKE_ENGAGED: '보호 쓰기 브레이크가 잠겨 있습니다.',
  LIVE_PROTECTED_WRITE_BRAKE_UNREADABLE: '보호 쓰기 브레이크 상태를 읽을 수 없습니다.',
  LIVE_GRANT_NO_MATCHING_ACTIVE_GRANT: '일치하는 유효 실행 권한이 없습니다.',
  LIVE_ENDPOINT_NOT_ADOPTED: '해당 마켓 연동 계약이 아직 채택되지 않았습니다.',
  LIVE_SENDER_NOT_WIRED: '전송 경로가 연결되어 있지 않습니다.',
  LIVE_CANARY_ELIGIBILITY_UNPROVEN: '카나리 대상 적격성이 증명되지 않았습니다.',
  LIVE_RESTORE_PROOF_ABSENT: '이 대상의 복원 증명이 없습니다.',
  LIVE_EVIDENCE_RETENTION_UNPROVEN: '증거 보존이 증명되지 않았습니다.',
  LIVE_VISUAL_ACCEPTANCE_UNRECORDED: '현재 코드의 화면 검수 기록이 없습니다.',
  LIVE_ASSET_ATTEMPT_OWNER_UNREADABLE: '업로드 시도 기록을 읽을 수 없습니다.',
  LIVE_ASSET_REPLAY_UNRESOLVED: '같은 이미지의 미확인 업로드가 남아 있습니다.',
  LIVE_ASSET_REPLAY_APPLIED_REUSE_NOT_ADOPTED: '이미 반영된 이미지의 재사용 경로가 채택되지 않았습니다.',
  LIVE_ASSET_REPLAY_KEY_UNDETERMINABLE: '업로드 대상을 식별할 수 없습니다.',
  LIVE_ASSET_CANDIDATE_NOT_READY: '후보 Preflight가 READY가 아닙니다.',
  LIVE_ASSET_CANDIDATE_DRIFT: '실행 권한 발급 이후 후보가 바뀌었습니다.',
};

export function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value ?? '—'));
}

export function chip(text, tone) {
  return h('span', { class: tone ? `chip ${tone}` : 'chip' }, text);
}

export function reason(code) {
  return code ? h('div', { class: 'mini' }, REASON_COPY[code] ?? code) : null;
}

export function table(headers, rows) {
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

// One reason exactly as its owner returned it (B-UX1): the code is shown, and its status, subject
// and areas travel as attributes. Nothing here judges a reason.
function reasonNode(tag, reason) {
  return h(
    tag,
    {
      class: 'mini',
      'data-reason': reason.code,
      'data-reason-status': reason.status,
      'data-areas': (reason.areas ?? []).join(' '),
      title: [reason.status, reason.subject].filter(Boolean).join(' · '),
    },
    reason.code,
  );
}

export function statusChip(status, codes, reasons) {
  if (!status) return '—';
  const structured = reasons ?? [];
  return fragment(
    chip(status, status === 'READY' ? 'good' : status === 'BLOCKED' ? 'bad' : 'warn'),
    ...(structured.length
      ? structured.map((reason) => reasonNode('span', reason))
      : (codes ?? []).map((code) => h('span', { class: 'mini', 'data-reason': code }, code))),
  );
}

// B-UX1: a unit's reasons grouped under the first area the server gave each; a reason is listed
// once, and an area the server could not classify keeps its real code under UNCLASSIFIED.
export function reasonGroups(reasons, labels) {
  const groups = new Map();
  for (const reason of reasons) {
    const area = (reason.areas ?? [])[0] ?? 'UNCLASSIFIED';
    if (!groups.has(area)) groups.set(area, []);
    groups.get(area).push(reason);
  }
  return [...groups.entries()].map(([area, members]) =>
    h(
      'div',
      { class: 'register-reason-area', 'data-area': area },
      h('span', { class: 'mini' }, labels?.[area] ?? area),
      ...members.map((reason) => reasonNode('div', reason)),
    ),
  );
}

// B-UX2: the fix-only projection. A row only says where to act; the move is to that surface, and
// nothing is fixed from here. Rows without an action are counted, never offered.
function fixesPanel(found, ctx) {
  if (!found) return null;
  if (found.unavailable) {
    return h('div', { class: 'register-fixes', 'data-fixes': 'UNAVAILABLE' }, reason(found.unavailable));
  }
  const go = (row) => {
    if (row.surface?.startsWith('SETTINGS_')) {
      ctx.navigate('settings');
      return;
    }
    const unit = row.draft_id
      ? document.querySelector(`.register-unit[data-draft='${CSS.escape(row.draft_id)}']`)
      : null;
    const section = row.surface === 'REGISTER_IMAGES' ? unit?.querySelector("[data-editor-section='images']") : null;
    (section ?? unit)?.scrollIntoView({ block: 'start' });
  };
  return h(
    'div',
    { class: 'register-fixes', 'data-fixes': found.projection_version },
    h('div', { class: 'supplier-head-row' }, h('b', {}, '고칠 것'), chip(`${found.fixes.length}`)),
    h(
      'div',
      { class: 'supplier-head-row' },
      ...FIX_ACTIONABILITY.map((key) =>
        h('span', { class: 'mini', 'data-actionability-count': key }, `${key} ${found.counts?.[key] ?? 0}`),
      ),
    ),
    ...found.fixes.map((row) =>
      h(
        'div',
        {
          class: 'register-fix',
          'data-fix': row.code,
          'data-actionability': row.actionability,
          'data-surface': row.surface,
          'data-source': row.source,
        },
        chip(row.actionability_label, row.actionability === 'FIX_AVAILABLE' ? 'warn' : ''),
        h('span', { class: 'mini', 'data-reason': row.code, title: row.subject ?? '' }, row.code),
        h(
          'button',
          { type: 'button', class: 'btn', 'data-action': 'GO_TO_FIX', onclick: () => go(row) },
          row.surface_label,
        ),
      ),
    ),
  );
}

// B-UX1: the readiness of the whole pre-send population, as the server counted it now.
function readinessPanel(readiness) {
  if (!readiness) return null;
  const areas = (readiness.areas ?? []).filter((area) => area.units > 0);
  const notEvaluated = Object.entries(readiness.not_evaluated ?? {});
  return h(
    'div',
    {
      class: 'register-readiness',
      'data-readiness': readiness.summary_version,
      'data-population': String(readiness.population),
    },
    h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 준비 현황'), chip(`전체 ${readiness.population}`)),
    h(
      'div',
      { class: 'supplier-head-row' },
      ...READINESS_STATUSES.map((status) =>
        h(
          'span',
          { class: 'mini', 'data-status-count': status },
          `${status} ${readiness.statuses?.[status] ?? 0}`,
        ),
      ),
    ),
    notEvaluated.length
      ? h(
          'div',
          { class: 'supplier-head-row' },
          ...notEvaluated.map(([code, count]) =>
            h('span', { class: 'mini', 'data-not-evaluated': code }, `${code} ${count}`),
          ),
        )
      : null,
    areas.length
      ? h(
          'div',
          { class: 'supplier-head-row' },
          ...areas.map((area) =>
            h(
              'span',
              { class: 'mini', 'data-area-count': area.area, title: area.codes.join(', ') },
              `${area.label} ${area.units}`,
            ),
          ),
        )
      : null,
  );
}

export function itemRow(item) {
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
    h('td', {}, statusChip(item.base_status, item.base_reason_codes, item.base_reasons)),
    h('td', {}, statusChip(item.pricing_status, item.pricing_reason_codes, item.pricing_reasons)),
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

export function categoryBlock(category) {
  const fields = [...category.attributes, ...category.notice_fields];
  return h(
    'div',
    { class: 'register-category', 'data-category': category.category_id },
    kv('카테고리', category.category_id),
    kv('분류 리비전', category.taxonomy_revision),
    category.metadata_revision ? kv('메타데이터 리비전 (고정)', category.metadata_revision) : null,
    category.metadata_unavailable_reason
      ? h(
          'div',
          { class: 'note', 'data-reason': category.metadata_unavailable_reason },
          REASON_COPY[category.metadata_unavailable_reason] ?? category.metadata_unavailable_reason,
        )
      : null,
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
export function authoringForm(unit, onDone) {
  const authored = unit.authored;
  const inputs = authored?.inputs ?? {};
  const categoryInput = field('카테고리', 'category_id', inputs.category?.category_id);
  const categoryControl = categoryInput.querySelector('input');
  const dynamicFields = h('div', { class: 'register-authoring-fields' });
  const optionFields = h('div', { class: 'register-option-fields' });
  const fieldControls = new Map();
  let metadata = null;

  // A field the marketplace fills itself when it is omitted ("상품상세 참조", a stated default) is
  // never a field the operator must type, whatever its required badge. A boolean is chosen, not
  // typed, and an integer is sent as a number: the server never coerces a value's type.
  function authoredField(rule, kind, current) {
    const mustType = rule.required && !rule.omitted_default;
    const valueType = rule.value_type ?? 'TEXT';
    const label = mustType ? `${rule.key} (필수)` : rule.key;
    let control;
    if (valueType === 'BOOLEAN') {
      const select = h('select', { name: `${kind}.${rule.key}` },
        h('option', { value: '' }, '—'),
        h('option', { value: 'true' }, '예'),
        h('option', { value: 'false' }, '아니오'));
      select.value = current?.value === true ? 'true' : current?.value === false ? 'false' : '';
      control = h('label', { class: 'kv' }, h('span', {}, label), select);
    } else {
      control = field(label, `${kind}.${rule.key}`,
        current?.value === undefined ? undefined : String(current.value));
      const placeholder = {
        YEAR_MONTH: 'yyyy-MM', DATE: 'yyyy-MM-dd', INTEGER: '정수', LONG: '정수',
      }[valueType];
      if (placeholder) control.querySelector('input').placeholder = placeholder;
    }
    const input = control.querySelector('input, select');
    let reference = null;
    if (rule.detail_page_reference_allowed) {
      reference = h('input', {
        type: 'checkbox',
        'data-detail-reference': kind,
        'data-field-key': rule.key,
        'aria-label': `${rule.key} 상세페이지 참조`,
      });
      reference.checked = current?.detail_page_reference === true;
      const sync = () => {
        input.disabled = reference.checked;
        input.required = mustType && !reference.checked;
        if (reference.checked) input.value = '';
      };
      reference.addEventListener('change', sync);
      control.append(h('span', { class: 'mini' }, reference, ' 상세페이지 참조'));
      sync();
    } else {
      input.required = mustType;
    }
    fieldControls.set(`${kind}:${rule.key}`, { input, reference, current, valueType });
    return control;
  }

  function optionRow(dimension, values) {
    const dimensionInput = h('input', {
      value: dimension,
      placeholder: '예: 색상, 사이즈',
      'data-option-dimension': 'true',
    });
    return h(
      'div',
      { class: 'register-option-row' },
      h('label', { class: 'kv' }, h('span', {}, '옵션명'), dimensionInput),
      ...unit.items.map((item) => h(
        'label',
        { class: 'kv' },
        h('span', {}, `Item ${item.item_id}`),
        h('input', {
          value: values[item.item_id] ?? '',
          'data-option-item': item.item_id,
          placeholder: '옵션값',
        }),
      )),
    );
  }

  function renderOptions(found) {
    const existing = inputs.options ?? {};
    const dimensions = [...new Set(
      Object.values(existing).flatMap((values) => Object.keys(values)),
    )].sort();
    optionFields.replaceChildren();
    if (unit.items.length <= 1 || (!found.options_supported && dimensions.length === 0)) return;
    const rows = dimensions.length ? dimensions : [''];
    const valuesFor = (dimension) => Object.fromEntries(
      unit.items.map((item) => [item.item_id, existing[item.item_id]?.[dimension] ?? '']),
    );
    const rowHost = h('div', { class: 'register-option-rows' },
      ...rows.map((dimension) => optionRow(dimension, valuesFor(dimension))));
    optionFields.append(
      h('div', { class: 'supplier-head-row' }, h('b', {}, '옵션 입력'),
        chip(`${unit.items.length}/${found.max_options} Items`)),
      rowHost,
    );
    if (found.options_supported) {
      const add = h('button', {
        type: 'button', class: 'btn', 'data-action': 'ADD_OPTION_DIMENSION',
      }, '옵션 항목 추가');
      add.disabled = rowHost.children.length >= found.max_option_dimensions;
      add.addEventListener('click', () => {
        rowHost.append(optionRow('', {}));
        add.disabled = rowHost.children.length >= found.max_option_dimensions;
      });
      optionFields.append(add);
    }
  }

  function renderMetadata(found) {
    metadata = found;
    fieldControls.clear();
    const controls = [];
    for (const rule of found.attributes) {
      controls.push(authoredField(rule, 'attribute', inputs.attributes?.[rule.key]));
    }
    for (const rule of found.notice_fields) {
      controls.push(authoredField(rule, 'notice', inputs.notices?.[rule.key]));
    }
    dynamicFields.replaceChildren(...controls);
    renderOptions(found);
  }

  async function loadMetadata() {
    const categoryId = categoryControl.value.trim();
    if (!categoryId) {
      metadata = null;
      dynamicFields.replaceChildren();
      return null;
    }
    if (metadata?.category_id === categoryId) return metadata;
    const found = await getJson(
      `/api/v1/register/drafts/${unit.draft_id}/authoring-metadata/${encodeURIComponent(categoryId)}`,
    );
    renderMetadata(found);
    return found;
  }
  const head = h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 준비 입력'),
    authored ? chip(`리비전 ${authored.revision_no}`) : chip('미저장', 'warn'));
  const nameField = field('상품명', 'name', inputs.name?.value);
  // B-DETAIL: the selected detail images come first; the text after them is optional when the
  // unit has detail images (the server decides, never this page).
  const detailField = field(
    '상세 본문 (상세 이미지 뒤, 상세 이미지가 있으면 선택)',
    'detail_body',
    inputs.detail_body,
    'textarea',
  );
  const submit = h(
    'button',
    {
      type: 'submit',
      class: 'btn blue',
      'data-action': 'SAVE_PREPARATION',
      'data-unit': unit.unit_ref,
    },
    authored ? '준비 내용 저장' : '준비 내용 만들기',
  );
  const form = h(
    'form',
    { class: 'register-authoring', 'data-preparation': authored?.preparation_id ?? '' },
    head,
    categoryInput,
    nameField,
    dynamicFields,
    optionFields,
    detailField,
    submit,
  );
  // The editor page places these by step and binds them back to this form (the `form` attribute);
  // the save reads them through the same references either way.
  form.parts = { head, category: categoryInput, name: nameField, fields: dynamicFields, options: optionFields, detail: detailField, submit };
  categoryControl.addEventListener('change', () => {
    loadMetadata().catch((error) => {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast(REASON_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
    });
  });
  if (inputs.category?.category_id) loadMetadata().catch(() => {});
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const approved = await loadMetadata();
      const values = Object.fromEntries(new FormData(form).entries());
      const collectFields = (kind, rules) => Object.fromEntries(rules.flatMap((rule) => {
        const control = fieldControls.get(`${kind}:${rule.key}`);
        if (control?.reference?.checked) {
          return [[rule.key, {
            detail_page_reference: true,
            provenance: control.current?.provenance ?? 'OPERATOR_CONFIRMED',
          }]];
        }
        const raw = control?.input.value.trim() ?? '';
        let value = raw;
        if (control?.valueType === 'BOOLEAN') value = raw === 'true';
        else if (['INTEGER', 'LONG'].includes(control?.valueType) && /^-?[0-9]+$/.test(raw)) {
          // Only an integer a JavaScript number holds exactly is sent as a number. A larger one
          // stays the operator's own text, which the server refuses as the wrong type — it is
          // never rounded into another value.
          const number = Number(raw);
          if (Number.isSafeInteger(number) && BigInt(raw) === BigInt(number)) value = number;
        }
        return raw ? [[rule.key, {
          value,
          provenance: control.current?.provenance ?? 'OPERATOR_CONFIRMED',
        }]] : [];
      }));
      const attributes = collectFields('attribute', approved?.attributes ?? []);
      const notices = collectFields('notice', approved?.notice_fields ?? []);
      const options = {};
      for (const row of optionFields.querySelectorAll('.register-option-row')) {
        const dimension = row.querySelector('[data-option-dimension]').value.trim();
        if (!dimension) continue;
        for (const input of row.querySelectorAll('[data-option-item]')) {
          const value = input.value.trim();
          if (!value) continue;
          const itemId = input.getAttribute('data-option-item');
          options[itemId] ??= {};
          options[itemId][dimension] = value;
        }
      }
      const body = {
        actor: OPERATOR,
        item_ids: unit.items.map((item) => item.item_id),
        inputs: {
          category: approved
            ? {
                category_id: approved.category_id,
                mapping_revision: approved.mapping_revision,
                taxonomy_revision: approved.taxonomy_revision,
                confirmation: 'OPERATOR_CONFIRMED',
              }
            : null,
          name: values.name ? { value: values.name } : null,
          tags: inputs.tags ?? [],
          attributes,
          notices,
          options,
          detail_composition_revision: approved?.detail_composition_revision ?? null,
          // Under a profile that places detail images an empty body is still a composition.
          detail_body: (approved?.detail_sections ?? []).includes('DETAIL_IMAGES')
            ? (values.detail_body ?? '')
            : (values.detail_body || null),
          detail_sections: approved?.detail_sections ?? inputs.detail_sections ?? ['BODY'],
          // ADR-0033 §5 (G4): the product editor's 상·하단 공지 choice; any other surface sends the
          // stored choice back unchanged, so a save never resets it.
          guidance: form.guidanceValue ? form.guidanceValue() : (inputs.guidance ?? null),
        },
      };
      if (authored) {
        await sendJson('POST', `/api/v1/register/preparations/${authored.preparation_id}`, body);
      } else {
        await sendJson('POST', '/api/v1/register/preparations', { ...body, draft_id: unit.draft_id });
      }
      toast('등록 준비 내용을 저장했습니다');
      onDone();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast(REASON_COPY[code] ?? (error instanceof Error ? error.message : String(error)));
    }
  });
  return form;
}

export function preflightBlock(unit, labels) {
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
    ...((preflight.reasons ?? []).length
      ? reasonGroups(preflight.reasons, labels)
      : preflight.reason_codes.map((code) => h('div', { class: 'mini', 'data-reason': code }, code))),
  );
}

export function attemptRow(attempt) {
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

export function scopeBlock(scope) {
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
  const path = {
    CREATE_ENQUEUE: 'create',
    RECONCILE: 'reconcile',
    VERIFY: 'verify',
    SETTLE_REJECTION: 'settle-rejection',
  }[action];
  return sendJson('POST', `/api/v1/register/intents/${intentId}/${path}`, {});
}

// ADR-0014 §28.5: the server's read state, as it labelled it. Only 등록실패 is red.
export function readStateChip(read, problem) {
  if (!read) {
    return h('span', { class: 'chip bad', 'data-reason': problem ?? 'REGISTER_READ_STATE_UNCLASSIFIED' }, '분류 오류');
  }
  return h('span', { class: `chip ${READ_STATE_TONE[read.state] ?? ''}`.trim(), 'data-read-state': read.state }, read.label);
}

async function runStatus(entry, action, button, onDone) {
  button.disabled = true;
  try {
    await call({}, action, entry.intent_id);
    toast(`${ACTION_LABEL[action] ?? action} 완료`);
    onDone();
  } catch (error) {
    const code = error instanceof ApiError ? error.error?.code : null;
    toast(REASON_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
    button.disabled = false;
  }
}

function statusRow(entry, onDone) {
  const read = entry.read_state;
  const action = read?.action ?? null;
  const button = action
    ? h(
        'button',
        {
          type: 'button',
          class: read.action_enabled ? 'btn blue' : 'btn',
          disabled: !read.action_enabled,
          'data-action': action,
          'data-intent': entry.intent_id,
          onclick: () => runStatus(entry, action, button, onDone),
        },
        ACTION_LABEL[action] ?? action,
      )
    : null;
  // B-STATUS: only when the server says the UNKNOWN is a provider 400 rejection it can settle.
  const settle = read?.rejection_settleable
    ? h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'SETTLE_REJECTION',
          'data-intent': entry.intent_id,
          onclick: () => runStatus(entry, 'SETTLE_REJECTION', settle, onDone),
        },
        ACTION_LABEL.SETTLE_REJECTION,
      )
    : null;
  const causes = [read?.attempt_cause_code, read?.cause_code]
    .filter((code, index, all) => code && all.indexOf(code) === index)
    .map((code) => h('span', { class: 'mini', 'data-cause': code }, REASON_COPY[code] ?? code));
  return h(
    'tr',
    { 'data-intent': entry.intent_id, 'data-read-row': read?.state ?? 'UNCLASSIFIED' },
    h('td', {}, entry.product_name ?? '—'),
    h('td', {}, entry.seller_code ?? '—'),
    h('td', {}, readStateChip(read, entry.read_state_problem)),
    h(
      'td',
      {},
      read
        ? h('span', { class: 'mini', 'data-reason': read.reason_code }, REASON_COPY[read.reason_code] ?? read.reason_code)
        : reason(entry.read_state_problem),
      ...causes,
    ),
    h('td', {}, read?.requested_at ? dotDateTime(read.requested_at) : '—'),
    h('td', {}, read?.last_confirmed_at ? dotDateTime(read.last_confirmed_at) : '—'),
    h('td', {}, String(read?.confirmation_attempts ?? 0)),
    h(
      'td',
      {},
      button ?? (settle ? null : '—'),
      button && !read.action_enabled ? reason(read.action_reason_code) : null,
      settle,
    ),
  );
}

// The detail panel of the registration status card (ADR-0014 §28.5): every recent Intent with the
// server's read state, its reason, request and confirmation times and the one action it offers.
function registrationStatusPanel(status, onDone) {
  if (!status || !status.entries.length) return null;
  const counts = status.counts;
  return h(
    'section',
    { class: 'panel register-status', 'data-role': 'register-status' },
    h(
      'div',
      { class: 'supplier-head-row' },
      withHelp(h('h2', { class: 'panel-title' }, '등록 진행 상태'), STATUS_HELP),
    ),
    h(
      'div',
      { class: 'register-summary' },
      kv(status.labels.REGISTERING, String(counts.registering)),
      kv(status.labels.REGISTERED, String(counts.registered)),
      kv(status.labels.RECHECK_REQUIRED, String(counts.recheck_required)),
      kv(status.labels.FAILED, String(counts.failed)),
    ),
    table(
      ['상품명', 'ICBM 판매자코드', '상태', '사유', '요청 시각', '마지막 확인', '확인 시도', '동작'],
      status.entries.map((entry) => statusRow(entry, onDone)),
    ),
  );
}

export function actionCell(unit, action, onDone) {
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

// B-PREVIEW: what this frozen Snapshot would send, as the server projected it. Every value is
// inserted as text; provider URLs never arrive (the server redacts them) and the detail body is
// its structure, never HTML.
export function previewBlock(unit) {
  if (!unit.snapshot) return null;
  const holder = h('div', { class: 'register-preview', 'data-preview': unit.snapshot.registration_snapshot_id });
  const button = h('button', { type: 'button', class: 'btn', 'data-action': 'PREVIEW_SNAPSHOT' }, '스마트스토어 미리보기');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const view = await getJson(
        `/api/v1/register/snapshots/${encodeURIComponent(unit.snapshot.registration_snapshot_id)}/preview`,
      );
      holder.replaceChildren(previewView(view));
    } catch (error) {
      toast(error instanceof ApiError ? error.message : String(error));
      button.disabled = false;
    }
  });
  holder.append(button);
  return holder;
}

function previewView(view) {
  const detail = view.detail;
  return h(
    'div',
    { class: 'register-preview-view', 'data-projected': String(view.projected) },
    h('div', { class: 'supplier-head-row' }, h('b', {}, '스마트스토어 미리보기'),
      view.projected
        ? chip(view.sendable ? '보낼 수 있음' : '빈 칸 있음', view.sendable ? 'good' : 'warn')
        : chip('투영 거절', 'bad')),
    view.refusal_code ? reason(view.refusal_code) : null,
    kv('상품명', view.name ?? '—'),
    kv('판매가', view.sale_prices_krw.map((price) => won(price)).join(', ') || '—'),
    kv('카테고리 ID', view.category_id ?? '—'),
    ...view.gaps.map((gap) => h('div', { class: 'mini', 'data-gap': 'true' }, gap)),
    table(
      ['이미지 역할', '순서', '이미지', '마켓 업로드'],
      view.images.map((image) =>
        h('tr', { 'data-preview-image': image.sha256 },
          h('td', {}, image.role),
          h('td', {}, image.position === null ? '—' : String(image.position)),
          h('td', {}, image.sha256.slice(0, 8)),
          h('td', {}, image.provider_asset_prepared ? '준비됨' : '없음')),
      ),
    ),
    detail
      ? h('div', { class: 'register-preview-detail', 'data-detail-builder': detail.builder },
          kv('상세 구획', detail.sections.join(' → ') || '—'),
          kv('상세 이미지', `${detail.image_slots.length}장`),
          ...detail.paragraphs.map((paragraph) => h('p', { class: 'mini' }, paragraph)))
      : null,
    view.document.length
      ? table(
          ['보낼 항목', '값'],
          view.document.map((field) =>
            h('tr', { 'data-field': field.path },
              h('td', {}, field.path),
              h('td', {}, field.redacted ? '(마켓 참조 · 표시 안 함)' : field.value ?? '—')),
          ),
        )
      : null,
    view.not_sent.length
      ? table(
          ['보내지 않음', '값'],
          view.not_sent.map((row) =>
            h('tr', { 'data-not-sent': row.path },
              h('td', {}, row.path),
              h('td', {}, row.value === 'DETAIL_PAGE_REFERENCE' ? '상세페이지 참조' : row.value)),
          ),
        )
      : null,
  );
}

// B-PRICE1: when the server says a pinned price is no longer M4's current one, the operator may
// ask M4 to price again and re-pin the Draft. The page names no price; the server decides all.
const REPIN_REASONS = new Set(['PRICING_SNAPSHOT_MISSING', 'PRICING_SNAPSHOT_SUPERSEDED']);

export function repinBlock(unit, onDone) {
  // Offered when the server says the pin is not M4's current price, or M4's own pricing readiness
  // names a missing or superseded snapshot; the server decides everything else.
  const moved = unit.items.some(
    (item) =>
      item.price_pin_current === false ||
      (item.pricing_reason_codes ?? []).some((code) => REPIN_REASONS.has(code)),
  );
  if (!moved) return null;
  const button = h('button', { type: 'button', class: 'btn', 'data-action': 'REPIN_DRAFT' }, '가격 다시 고정');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const moved = await sendJson('POST', `/api/v1/register/drafts/${unit.draft_id}/repin`, {
        actor: OPERATOR,
        expected_draft_revision: unit.draft_revision,
      });
      const count = moved.items.filter((item) => item.repinned).length;
      toast(count ? `${count}개 품목의 가격을 다시 고정했습니다.` : 'M4 가격이 그대로라 바뀐 것이 없습니다.');
      onDone();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast(REASON_COPY[code] ?? (error instanceof ApiError ? error.message : String(error)));
      button.disabled = false;
    }
  });
  return h('div', { class: 'register-repin', 'data-repin': unit.draft_id }, button);
}

// B-EDITOR (Issue #127): one unit is a workspace over its owners, never a second product record.
// Four sections — 상품 정보, 이미지, 가격, 등록 준비 — each act through their own owner's command, and
// every section stays rendered: the bar only moves to one, it hides nothing (Gate-3 selectors).
const EDITOR_SECTIONS = [
  ['info', '상품 정보'],
  ['images', '이미지'],
  ['price', '가격'],
  ['ready', '등록 준비'],
];

function editorSection(unit, key, label, ...children) {
  return h(
    'div',
    { class: 'register-editor-section', 'data-editor-section': key, 'data-draft': unit.draft_id },
    h('h3', { class: 'panel-subtitle' }, label),
    ...children,
  );
}

function editorNav(panel) {
  return h(
    'div',
    { class: 'register-editor-nav', role: 'navigation', 'aria-label': '편집 구역' },
    ...EDITOR_SECTIONS.map(([key, label]) =>
      h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-editor-go': key,
          onclick: () => panel.querySelector(`[data-editor-section='${key}']`)?.scrollIntoView({ block: 'start' }),
        },
        label,
      ),
    ),
  );
}

function imagesSection(unit, onDone) {
  // Images are chosen before a Snapshot freezes them; afterwards they are only shown.
  const editable = !unit.snapshot && !unit.intent;
  return editorSection(
    unit,
    'images',
    '이미지',
    editable ? null : h('span', { class: 'mini' }, '스냅샷이 고정되어 이미지는 보기만 합니다.'),
    ...unit.items.map((item) =>
      h(
        'div',
        { class: 'register-item-images', 'data-item': item.item_id },
        h('b', {}, `품목 ${item.ordinal} · ${item.item_id}`),
        itemImagesEditor(item.item_id, { editable, onChanged: onDone }),
      ),
    ),
  );
}

function unitPanel(unit, onDone, labels) {
  const intent = unit.intent;
  const panel = h(
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
      intent ? readStateChip(intent.read_state, intent.read_state_problem) : null,
    ),
    intent ? kv('요청 상태', INTENT_LABEL[intent.state] ?? intent.state) : null,
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
  );
  panel.append(
    editorNav(panel),
    editorSection(
      unit,
      'info',
      '상품 정보',
      unit.category ? categoryBlock(unit.category) : null,
      unit.snapshot ? null : authoringForm(unit, onDone),
      unit.authored ? kv('준비 지문', unit.authored.inputs_fingerprint.slice(0, 16)) : null,
    ),
    imagesSection(unit, onDone),
    editorSection(
      unit,
      'price',
      '가격',
      repinBlock(unit, onDone),
      unit.item_facts_unavailable_reason ? reason(unit.item_facts_unavailable_reason) : null,
      table(
        ['품목', '고정 판매가', '가격 근거', '현재 M4 판매가', '기본 준비', '가격 준비', '등록 품목 키', '이미지'],
        unit.items.map(itemRow),
      ),
    ),
    editorSection(
      unit,
      'ready',
      '등록 준비',
      preflightBlock(unit, labels),
      previewBlock(unit),
      intent && intent.attempts.length
        ? table(['시도', '결과', '오류 분류', '오류 코드', '시작'], intent.attempts.map(attemptRow))
        : null,
      scopeBlock(unit.scope),
      h('div', { class: 'supplier-actions' }, ...unit.actions.map((action) => actionCell(unit, action, onDone))),
    ),
  );
  return panel;
}

// Gate 2 G2-C (ADR-0016 §7): one account's REGISTER ReviewItems — execution states and each
// current preparation's candidate preflight — with both producers' coverage. The server derives
// every item; a resolution is re-derived by its owner and never changes a REGISTER fact.
function reviewScopeLabel(item) {
  const scope = item.scope;
  if (scope.intent_id) return `요청 ${scope.intent_id.slice(0, 8)} · 초안 ${scope.draft_id.slice(0, 8)}`;
  if (scope.preparation_id) return `준비 ${scope.preparation_id.slice(0, 8)} · 초안 ${scope.draft_id.slice(0, 8)}`;
  return '계정 범위';
}

function reviewPanel(marketplaceKey, accountId) {
  const block = reviewItemsBlock({
    kindLabel: KIND_LABEL,
    emptyCopy: '이 판매 계정에 기록된 등록 검토 항목이 없습니다.',
    errorCopy: (code, message) => REASON_COPY[code] ?? message ?? code,
    scopeLabel: reviewScopeLabel,
  });
  const panel = h(
    'section',
    { class: 'panel register-review', 'data-role': 'register-review', 'data-account': accountId },
    h('h2', { class: 'panel-title' }, `검토 항목 · ${accountId}`),
  );
  block({ marketplace_key: marketplaceKey, marketplace_account_id: accountId }).then((found) => {
    if (found) panel.append(found);
  });
  return panel;
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

function grantRow(grant) {
  const readiness = grant.readiness
    ? fragment(
        chip(grant.readiness.verdict, grant.readiness.verdict === 'READY' ? 'good' : 'bad'),
        h(
          'ul',
          { class: 'requirement-list' },
          ...grant.readiness.missing.map((code) => h('li', { 'data-missing': code }, REASON_COPY[code] ?? code)),
        ),
      )
    : h('span', { class: 'mini' }, '등록 단계 준비도는 카나리 준비도에서 확인합니다.');
  return h(
    'tr',
    { 'data-grant': grant.grant_id, 'data-grant-state': grant.state, 'data-stage': grant.stage },
    h('td', {}, STAGE_LABEL[grant.stage] ?? grant.stage),
    h('td', {}, chip(GRANT_LABEL[grant.state] ?? grant.state, grant.state === 'ACTIVE' ? 'good' : 'warn')),
    h('td', { class: 'mono' }, grant.marketplace_account_id),
    h('td', {}, `${grant.budget_used} / ${grant.budget_max}`),
    h('td', {}, dotDateTime(grant.expires_at)),
    h('td', {}, readiness),
  );
}

// The pre-LIVE safety state (ADR-0018 §9, §10): the protected-write brake, the unit-independent
// proofs and every grant with the ASSET readiness the server derives for it. Read-only.
function livePanel(live) {
  const brake = live.brake;
  return h(
    'section',
    { class: 'panel register-live', 'data-role': 'live-panel' },
    withHelp(h('h2', { class: 'panel-title' }, '보호 쓰기 · 실행 권한'), LIVE_HELP),
    h(
      'div',
      { 'data-role': 'live-brake', 'data-brake-state': brake.state },
      kv('보호 쓰기 브레이크', BRAKE_LABEL[brake.state] ?? brake.state),
      chip(brake.state, brake.state === 'RELEASED' ? 'good' : 'bad'),
      brake.recorded
        ? null
        : h('div', { class: 'mini', 'data-reason': 'BRAKE_NOT_RECORDED' }, '기록이 없어 기본값인 잠김으로 판단합니다.'),
      brake.reason_code ? h('div', { class: 'mini', 'data-reason': brake.reason_code }, brake.reason_code) : null,
    ),
    h(
      'ul',
      { class: 'requirement-list', 'data-role': 'live-proofs' },
      ...Object.entries(live.proofs).map(([name, proven]) =>
        h(
          'li',
          { 'data-proof': name, 'data-satisfied': proven ? 'true' : 'false' },
          h('span', {}, PROOF_LABEL[name] ?? name),
          chip(proven ? '증명됨' : '미증명', proven ? 'good' : 'warn'),
        ),
      ),
    ),
    h(
      'div',
      { 'data-role': 'live-grants' },
      live.grants.length
        ? table(['단계', '상태', '계정', '예산', '만료', '준비도'], live.grants.map(grantRow))
        : h('div', { class: 'mini', 'data-grant-state': 'NONE' }, '발급된 실행 권한이 없습니다.'),
    ),
  );
}

// ---------------------------------------------------------------- v29 layout (2026-10-07)
//
// v29's 등록관리 composition: five cards, the 등록 이력 row, the 등록 대기 목록 beside 플랫폼별 등록
// 설정, then 등록상품 관리, 실패 / 재시도 and the safety panels, each unit's own workspace last.
// v29 switches these as tabs; here every section stays on screen (the Gate 3 surface reads them on
// one route) and a card brings its section into view. A v29 slot the system has no source for
// keeps its place and reads 데이터 없음 (owner decision, option 가); a v29 control with no contract is
// markInert and sends nothing.
const NO_DATA = '데이터 없음';

function noDataCell(label) {
  return h('td', { class: 'no-data', 'data-no-data': label }, NO_DATA);
}

function noDataKv(label) {
  return h('div', { class: 'kv', 'data-no-data': label }, h('span', {}, label), h('b', { class: 'no-data' }, NO_DATA));
}

function goTo(section) {
  document.querySelector(`[data-section='${section}']`)?.scrollIntoView({ block: 'start' });
}

// Each card is a count the server holds, or 데이터 없음 when no read states it.
function registerCards(screen, status, readiness) {
  const category = (readiness?.areas ?? []).find((area) => area.area === 'CATEGORY');
  const cards = [
    ['waiting', '◷', '등록 대기', screen.registration_candidates_total, 'pending'],
    ['registered', '✓', '등록 완료', screen.registrations_total, 'products'],
    // The status's own labels; without a status read the cards keep v29's titles.
    ['failed', '×', status?.labels?.FAILED ?? '실패', status?.counts?.failed, 'failed'],
    ['recheck', '↻', status?.labels?.RECHECK_REQUIRED ?? '재시도 필요', status?.counts?.recheck_required, 'failed'],
    ['category', '◇', '카테고리 확인', category?.units, 'pending'],
  ];
  return h(
    'div',
    { class: 'grid5 page-kpis', 'data-role': 'register-cards' },
    ...cards.map(([key, icon, label, count, section]) =>
      h(
        'button',
        {
          type: 'button',
          class: key === 'waiting' ? 'kpi state-card active' : 'kpi state-card',
          'data-card': key,
          onclick: () => goTo(section),
        },
        h('span', { class: 'kpi-top' }, h('span', { class: 'kicon', 'aria-hidden': 'true' }, icon), label),
        typeof count === 'number'
          ? h('strong', { 'data-total': String(count) }, count.toLocaleString('ko-KR'))
          : h('strong', { class: 'no-data', 'data-no-data': label }, NO_DATA),
      ),
    ),
  );
}

function utilityRow(overview) {
  return h(
    'div',
    { class: 'register-utility-row' },
    h('span', { class: 'mini', 'data-role': 'paused-scopes' }, `중단된 범위 ${overview.paused_scopes.length}`),
    h('button', { type: 'button', class: 'btn', 'data-action': 'open-history', onclick: () => goTo('failed') }, '등록 이력'),
  );
}

// The registration editor (2026-10-08 owner UX): a row opens this choice, and the chosen work opens
// in a new tab. 상품수정 needs a registration the server has confirmed; the choice itself sends
// nothing. A unit's ref moves as it is authored and frozen (the server's UnitView), so the link also
// carries its Items: the editor follows the same unit through those moves.
export function unitItemsKey(unit) {
  return unit.items.map((item) => item.item_id).sort().join(',');
}

export function editorHref(unit, mode) {
  const params = new URLSearchParams({ draft: unit.draft_id, unit: unit.unit_ref, items: unitItemsKey(unit), mode });
  return `${window.location.pathname}#/register-editor?${params}`;
}

function openUnitChoice(unit) {
  const registered = unit.intent?.read_state?.state === 'REGISTERED';
  const open = (mode) => {
    window.open(editorHref(unit, mode), '_blank');
  };
  const card = (mode, tag, tone, title, copy, enabled) =>
    h(
      'button',
      {
        type: 'button',
        class: 'choice-card',
        'data-choice': mode,
        disabled: !enabled,
        onclick: () => enabled && open(mode),
      },
      h('span', { class: `chip ${tone}` }, tag),
      h('b', {}, title),
      h('span', { class: 'mini' }, copy),
    );
  openModal({
    eyebrow: '등록관리 · 상품 편집기',
    title: unit.unit_ref,
    help: '편집기는 새 탭으로 열립니다. 등록관리 화면은 그대로 두고 여러 상품을 나란히 열어 작업할 수 있습니다.',
    body: h(
      'div',
      { class: 'unit-choice', 'data-role': 'unit-choice', 'data-unit': unit.unit_ref },
      h(
        'div',
        { class: 'detail-group' },
        h('div', { class: 'kv' }, h('span', {}, '대상 플랫폼'), platformTag(unit.marketplace_key)),
        kv('준비 상태', PREPARATION_LABEL[unit.preparation] ?? unit.preparation),
        unit.intent ? h('div', { class: 'kv' }, h('span', {}, '등록 상태'), readStateChip(unit.intent.read_state, unit.intent.read_state_problem)) : null,
      ),
      h(
        'div',
        { class: 'choice-grid' },
        card('register', '상품등록', 'info', '마켓에 등록하기', '편집기에서 상품 정보·이미지·가격·고시·상세를 채우고 등록합니다.', !registered),
        card('edit', '상품수정', 'good', '등록된 상품 수정하기', registered ? '등록된 상품의 내용을 확인합니다. 마켓에 수정 반영은 준비 중입니다.' : '마켓 등록이 확인된 뒤에 수정할 수 있습니다.', registered),
      ),
    ),
  });
}

// One row per unit, as the server read it; 열기 brings that unit's own workspace into view.
function queueRow(unit) {
  const read = unit.intent?.read_state;
  return h(
    'tr',
    {
      class: 'register-queue-row',
      'data-queue-unit': unit.unit_ref,
      tabindex: '0',
      onclick: (event) => {
        if (!event.target.closest('button')) openUnitChoice(unit);
      },
      onkeydown: (event) => {
        if (event.key === 'Enter') openUnitChoice(unit);
      },
    },
    h('td', {}, h('b', {}, unit.unit_ref), h('div', { class: 'mini mono' }, `초안 ${unit.draft_id.slice(0, 8)}`)),
    h('td', {}, platformTag(unit.marketplace_key)),
    unit.category ? h('td', {}, h('span', { class: 'mono' }, unit.category.category_id)) : noDataCell('카테고리'),
    noDataCell('옵션 상태'),
    noDataCell('이미지 상태'),
    h('td', {}, chip(PREPARATION_LABEL[unit.preparation] ?? unit.preparation)),
    noDataCell('진행률'),
    h('td', {}, read ? h('span', { class: `chip ${READ_STATE_TONE[read.state] ?? ''}`.trim() }, read.label) : '-'),
    h(
      'td',
      {},
      h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'open-unit',
          onclick: () => document.querySelector(`.register-unit[data-unit='${CSS.escape(unit.unit_ref)}']`)?.scrollIntoView({ block: 'start' }),
        },
        '열기',
      ),
    ),
  );
}

function queuePanel(units, readinessBlock, empty) {
  return h(
    'section',
    { class: 'panel register-queue', 'data-role': 'register-queue', 'data-section': 'pending' },
    h('div', { class: 'supplier-head-row' }, h('h3', { class: 'panel-title' }, '등록 대기 목록'), h('span', { class: 'mini' }, `총 ${units.length}건`)),
    h(
      'div',
      { class: 'toolbar register-queue-tools' },
      markInert(h('button', { type: 'button', class: 'btn dark' }, '▶ 대량 등록'), '대량 등록'),
      markInert(h('button', { type: 'button', class: 'btn' }, '↻ 선택 재시도'), '선택 재시도'),
      markInert(h('button', { type: 'button', class: 'btn ai-btn' }, '✨ AI 카테고리 재추천'), 'AI 카테고리 재추천'),
      markInert(h('button', { type: 'button', class: 'btn' }, '⇩ 엑셀 내보내기'), '엑셀 내보내기'),
    ),
    units.length
      ? table(['상품', '대상 플랫폼', '카테고리', '옵션 상태', '이미지 상태', '등록 상태', '진행률', '최종 결과', ''], units.map(queueRow))
      : empty,
    readinessBlock,
  );
}

// v29's 플랫폼별 등록 설정: no read here states these defaults, and its toggles stay a visual shell
// (UI_SOURCE_OF_TRUTH architect ruling on the registration automation controls: zero writes).
function settingsPanel() {
  return h(
    'aside',
    { class: 'side-detail register-settings', 'data-role': 'register-settings' },
    h('h3', {}, '플랫폼별 등록 설정'),
    h(
      'div',
      { class: 'inner-tabs register-platform-tabs' },
      h('button', { type: 'button', 'aria-selected': 'true', 'aria-label': '스마트스토어' }, platformTag('smartstore')),
      markInert(h('button', { type: 'button', 'aria-selected': 'false', 'aria-label': '쿠팡' }, platformTag('coupang')), '쿠팡'),
      markInert(h('button', { type: 'button', 'aria-selected': 'false', 'aria-label': '11번가' }, platformTag('st11')), '11번가'),
    ),
    h('div', { class: 'detail-group' }, noDataKv('기본 배송비'), noDataKv('기본 출고일'), noDataKv('상품 상태')),
    h(
      'div',
      { class: 'detail-group' },
      ...['자동 가격조정', '카테고리 자동매칭', '등록 실패 자동 재시도'].map((label) =>
        h(
          'div',
          { class: 'kv' },
          h('span', {}, label),
          markInert(h('span', { class: 'toggle off', role: 'switch', 'aria-checked': 'false', 'aria-label': label }), label),
        ),
      ),
    ),
    h('div', { class: 'note' }, '수집 → 검증 → 가격확정 → 등록준비 → 등록완료'),
  );
}

// A bare block placed as its own card in a section; the block itself is unchanged.
function panelOf(block) {
  return block ? h('section', { class: 'panel register-fixes-panel' }, block) : null;
}

function sectionWrap(section, title, ...children) {
  const shown = children.filter(Boolean);
  if (!shown.length) return null;
  return h(
    'div',
    { class: 'register-section', 'data-section': section },
    h('h2', { class: 'register-section-title' }, title),
    ...shown,
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
    let live;
    let readiness;
    let fixes;
    try {
      [screen, overview, canary, live, readiness, fixes] = await Promise.all([
        getJson(SCREEN),
        getJson(OVERVIEW),
        getJson(CANARY),
        getJson(LIVE),
        // The summary is its own read: when it is refused the page still shows every unit, and
        // says the summary is unavailable with the server's code.
        getJson(READINESS).catch((error) => ({
          unavailable: error instanceof ApiError ? error.error?.code ?? 'ERROR' : 'ERROR',
        })),
        getJson(FIXES).catch((error) => ({
          unavailable: error instanceof ApiError ? error.error?.code ?? 'ERROR' : 'ERROR',
        })),
      ]);
    } catch (error) {
      return fragment(head, errorState(error));
    }
    const reload = () => ctx.navigate('register');
    const areaLabels = Object.fromEntries(
      (readiness?.areas ?? []).map((area) => [area.area, area.label]),
    );
    const readinessBlock = readiness?.unavailable
      ? h('div', { class: 'register-readiness', 'data-readiness': 'UNAVAILABLE' }, reason(readiness.unavailable))
      : readinessPanel(readiness);
    const statusPanel = registrationStatusPanel(overview.registration_status, reload);
    // Opened from the registration status card: its detail panel is brought into view.
    if (statusPanel && ctx.params.get('status') === 'open') {
      window.setTimeout(() => statusPanel.scrollIntoView({ block: 'start' }), 0);
    }
    const cards = registerCards(screen, overview.registration_status, readiness?.unavailable ? null : readiness);
    if (!overview.units.length) {
      return fragment(
        head,
        cards,
        utilityRow(overview),
        h(
          'div',
          { class: 'section-grid register-layout' },
          queuePanel(
            [],
            readinessBlock,
            emptyState({
              title: '등록 후보가 없습니다',
              copy: '통합DB에서 등록 가능한 상품을 선택하면 Preflight를 거쳐 등록할 수 있습니다.',
              action: { label: '통합DB 보기', onSelect: () => ctx.navigate('db') },
            }),
          ),
          settingsPanel(),
        ),
        sectionWrap('products', '등록상품 관리', listingSyncPanel()),
        sectionWrap('failed', '실패 / 재시도', statusPanel),
        sectionWrap('safety', '실등록 안전장치', canaryPanel(canary), livePanel(live)),
      );
    }
    // Opened from the Product DB with the Draft it just created (Gate 1 G1-D): that Draft's
    // units are marked and brought into view. The route names the Draft only; its units are
    // read from the server like every other unit.
    const focusDraft = ctx.params.get('draft');
    const panels = overview.units.map((unit) => {
      const panel = unitPanel(unit, reload, areaLabels);
      if (focusDraft && unit.draft_id === focusDraft) panel.setAttribute('aria-current', 'true');
      return panel;
    });
    const accounts = new Map(
      overview.units.map((unit) => [`${unit.marketplace_key}|${unit.marketplace_account_id}`, unit]),
    );
    const reviews = [...accounts.values()].map((unit) => reviewPanel(unit.marketplace_key, unit.marketplace_account_id));
    const focused = panels.find((panel) => panel.getAttribute('aria-current') === 'true');
    if (focused && ctx.params.get('status') !== 'open') {
      window.setTimeout(() => focused.scrollIntoView({ block: 'start' }), 0);
    }
    return fragment(
      head,
      cards,
      utilityRow(overview),
      h('div', { class: 'section-grid register-layout' }, queuePanel(overview.units, readinessBlock, null), settingsPanel()),
      sectionWrap('products', '등록상품 관리', listingSyncPanel()),
      sectionWrap('failed', '실패 / 재시도', panelOf(fixesPanel(fixes, ctx)), statusPanel),
      sectionWrap('safety', '실등록 안전장치', canaryPanel(canary), livePanel(live)),
      sectionWrap('reviews', '검토 항목', ...reviews),
      sectionWrap('units', '등록 단위 작업 공간', ...panels),
    );
  },
};
