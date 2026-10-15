// 등록 권한 확인 (SMARTSTORE-A0-PERMISSION, M2 PR-C). The operator records which API groups
// Commerce API Center shows. The server supplies the required groups, the application binding,
// the mapping revision, the time and the age bound; this form sends only the observed groups. It
// reports whether the stored evidence counted, and says so when it expired rather than letting a
// lifted block look like a grant (PERMISSIONS_SCOPES §8.2). Its current-truth chip carries the
// evidence-strength marker of the capability's write_scope (M2 PR-D, CAPABILITY_MAPPING §14.11
// surface 3); the input semantics are PR-C's and unchanged.

import { getJson, sendJson } from '../../core/api.js';
import { permissionLine, statusChip } from '../../platforms/smartstore/capability.js';
import { h } from '../../core/dom.js';
import { errorText as plainError } from '../../core/errors.js';
import { dotDateTime } from '../../core/format.js';
import { toast } from '../../core/toast.js';

const BASE = '/api/v1/connect/marketplaces';
const TITLE = '등록 권한 확인';

const REFUSAL = {
  APPLICATION_NOT_CONFIGURED: '스마트스토어 애플리케이션 정보가 아직 연결되지 않아 기록할 수 없습니다.',
  MAPPING_REVISION_UNAVAILABLE: '권한 확인 기준이 아직 준비되지 않아 기록할 수 없습니다.',
};

const INVALIDATION = {
  APPLICATION_NOT_CONFIGURED: '애플리케이션 정보가 없음',
  APPLICATION_FINGERPRINT_MISMATCH: '애플리케이션 정보가 바뀜',
  REQUIRED_GROUPS_CHANGED: '필요한 권한이 바뀜',
  MAPPING_REVISION_UNAVAILABLE: '권한 확인 기준이 없음',
  MAPPING_REVISION_CHANGED: '권한 확인 기준이 바뀜',
  EXPIRED: '확인 유효기간 경과',
  MALFORMED: '저장된 기록을 읽을 수 없음',
};

const CONTRACT = {
  UNRECORDED: 'API 계약 상태 미확인',
  STALE: 'API 계약 재검토 필요',
  REVIEW_REQUIRED: 'API 계약 검토 필요',
};

function promotionChip(view) {
  if (view.promotion === 'APPLIED') {
    return view.evaluation?.write_scope?.status === 'MISSING'
      ? ['필수 권한 없음 · 반영됨', 'bad']
      : ['권한 확인됨 (관리자 화면 확인) · 반영됨', 'good'];
  }
  if (view.promotion === 'BLOCKED_BY_CONTRACT_FRESHNESS') {
    return [`저장됨 · 반영 대기 (${CONTRACT[view.contract_freshness] ?? view.contract_freshness})`, 'warn'];
  }
  if (view.promotion === 'NOT_CURRENT') {
    // An expired confirmation lifts a block without granting anything (PERMISSIONS_SCOPES §8.2).
    return view.evaluation?.invalidations?.includes('EXPIRED')
      ? ['저장된 권한 확인이 만료되어 현재 권한 상태를 확인할 수 없음', 'warn']
      : ['저장된 확인이 현재 기준과 달라 권한 미확인', 'warn'];
  }
  if (view.promotion === 'PENDING') return ['반영 확인 중', 'info'];
  return ['기록 없음', null];
}

// The primary value is the bound the current decision uses — min(recorded, configured) — never a
// looser input (Issue #34). The configured and recorded bounds are context only. Before any
// evidence exists, the configured bound is the policy a new recording will get.
function validity(view) {
  const effective = view.evaluation?.applicable_max_age_days;
  if (effective == null) return `${view.max_age_days}일`;
  const recorded = view.attestation?.freshness_policy_max_age_days;
  return recorded != null && recorded !== view.max_age_days
    ? `${effective}일 (현재 설정 ${view.max_age_days}일 · 기록 당시 ${recorded}일)`
    : `${effective}일`;
}

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value));
}

function errorText(error) {
  const reason = error?.error?.details?.reason;
  return REFUSAL[reason] ?? plainError(error);
}

function render(root, key, view) {
  const label = (group) => view.group_labels[group] ?? group;
  const observed = new Set(view.attestation?.observed_groups ?? []);
  const checks = view.selectable_groups.map((group) => {
    const input = h('input', { type: 'checkbox', value: group, 'aria-label': label(group) });
    input.checked = observed.has(group);
    input.disabled = !view.recording_available;
    return { group, input, row: h('label', { class: 'kv' }, h('span', {}, label(group)), input) };
  });
  const save = h('button', { type: 'button', class: 'btn blue' }, '권한 확인 저장');
  save.disabled = !view.recording_available;
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const updated = await sendJson('POST', `${BASE}/${key}/permission-attestation`, {
        observed_groups: checks.filter((check) => check.input.checked).map((check) => check.group),
      });
      toast(TITLE, '체크한 권한을 저장했습니다.');
      render(root, key, updated);
    } catch (error) {
      toast(TITLE, errorText(error));
      save.disabled = !view.recording_available;
    }
  });
  const [chipLabel, tone] = promotionChip(view);
  // The marker and, once the evidence counts, the tone come from current capability truth.
  const current = permissionLine(view.capability_write_scope);
  const chip = statusChip('permission', { ...current, text: chipLabel, tone: view.promotion === 'APPLIED' ? current.tone : tone });
  const invalid = view.evaluation?.invalidations ?? [];
  const record = view.attestation;
  // Unlike h(), replaceChildren() would render a null child as the text "null".
  const rows = [
    kv('필수 API 그룹', view.required_groups.map(label).join(', ')),
    h('div', { class: 'kv' }, h('span', {}, '커머스API센터에서 켜 둔 API 그룹'), h('span', {}, record ? '' : '켜 둔 그룹을 체크하세요')),
    ...checks.map((check) => check.row),
    kv('확인 일시', record ? dotDateTime(record.observed_at) : '저장 시 자동 기록'),
    kv('확인 유효기간', validity(view)),
    h('div', { class: 'kv' }, h('span', {}, '현재 상태'), chip),
    invalid.length ? kv('다시 확인이 필요한 이유', invalid.map((reason) => INVALIDATION[reason] ?? '기준이 바뀜').join(', ')) : null,
    view.recording_available
      ? null
      : h('div', { class: 'note', 'data-reason': view.recording_refusal ?? '' }, REFUSAL[view.recording_refusal] ?? '지금은 기록할 수 없습니다.'),
    h('div', { class: 'api-action-row' }, save),
  ];
  root.replaceChildren(...rows.filter(Boolean));
}

export function permissionAttestationPanel(key) {
  const root = h('div', { class: 'permission-attestation', 'data-marketplace': key }, h('div', { class: 'note' }, '불러오는 중'));
  getJson(`${BASE}/${key}/permission-attestation`)
    .then((view) => render(root, key, view))
    .catch((error) => root.replaceChildren(h('div', { class: 'note' }, errorText(error))));
  return root;
}
