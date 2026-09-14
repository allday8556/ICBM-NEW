// The one owner of the SmartStore capability projection (CAPABILITY_MAPPING §14; M2 PR-D).
// It maps server enums to the frozen §14 labels and evidence-strength markers and decides
// nothing: every line reads its own server field (§14.8), routing is on enum values only (§2.6),
// and a value this build does not know renders as itself with review treatment, never as a guess.

import { h } from './dom.js';
import { dotDate } from './format.js';

// §14.1: ● strong (machine/runtime-proven), ◐ limited (operator-attested), ○ unverified/unknown.
const MARK = { strong: '●', limited: '◐', unverified: '○' };
const MARK_LABEL = { strong: '강한 증거', limited: '운영자 확인 증거', unverified: '미확인' };

function line(text, { strength = null, tone = null } = {}) {
  return { text, strength, glyph: strength ? MARK[strength] : null, tone };
}

function unexpected(value) {
  return line(String(value), { tone: 'warn' });
}

// §14.3 authentication labels for a typed PAUSED/AUTHENTICATION reason.
const AUTH_PAUSED = {
  APPLICATION_REAUTH_REQUIRED: '재인증 필요',
  AUTH_RETRY_LIMIT: '인증 확인 필요',
  ACCOUNT_RESTRICTED: '계정 제한 확인 필요',
};

// §14.7
const REASON = {
  AUTH_RETRY_LIMIT: '인증 재시도 한도 초과',
  APPLICATION_REAUTH_REQUIRED: '네이버 애플리케이션 재인증 필요',
  SCOPE_INSUFFICIENT: '필수 API 권한 부족',
  ACCOUNT_RESTRICTED: '계정 제한 확인 필요',
};

// §14.9
const SCOPE = { AUTHENTICATION: '인증', PRODUCT_REGISTRATION: '상품 등록' };

export function scopeLabel(scope) {
  return SCOPE[scope] ?? scope;
}

// §14.10: the enum value is shown; the gloss is presentation only.
const ERROR_GLOSS = {
  TRANSIENT: '일시적 오류',
  RATE_LIMITED: '호출 한도',
  AUTH: '인증 오류',
  VALIDATION: '입력 오류',
  POLICY_BLOCKED: '정책 차단',
  NOT_FOUND: '대상 없음',
  CONFLICT: '충돌',
  DUPLICATE: '중복',
  REVIEW_REQUIRED: '검토 필요',
  FATAL: '처리 불가',
  UNKNOWN: '원인 미확정',
};
const OUTCOME_GLOSS = { APPLIED_PROVEN: '적용 확인', NOT_APPLIED_PROVEN: '미적용 확인', UNKNOWN: '미확정' };

function overlay(view, scope) {
  return view.workflow.find((item) => item.workflow_scope === scope) ?? null;
}

// §14.3, first match wins: AUTH_MISMATCH, a typed PAUSED/AUTHENTICATION reason,
// REVIEW_REQUIRED/AUTHENTICATION, then the raw auth axis.
export function authLine(view) {
  if (view.auth === 'AUTH_MISMATCH') return line('계정 확인 필요', { tone: 'bad' });
  const auth = overlay(view, 'AUTHENTICATION');
  if (auth?.workflow_state === 'PAUSED') {
    return AUTH_PAUSED[auth.reason_code] ? line(AUTH_PAUSED[auth.reason_code], { tone: 'warn' }) : unexpected(auth.reason_code);
  }
  if (auth?.workflow_state === 'REVIEW_REQUIRED') return line('확인 필요', { tone: 'warn' });
  if (view.auth === 'READY') return line('연결됨', { strength: 'strong', tone: 'good' });
  if (view.auth === 'NOT_READY') return line('연결 확인 전', { strength: 'unverified' });
  if (view.auth === 'NOT_BOUND') return line('연결 필요', { strength: 'unverified' });
  return unexpected(view.auth);
}

// §14.4, from the write_scope view (status + evidence grade). A positive permission is never shown
// without its strength (S1); a READY whose strength is absent is not presented as a grant.
export function permissionLine(scope) {
  if (scope.status === 'READY') {
    if (scope.evidence_grade === 'STRONG') return line('권한 확인됨 (자동 확인)', { strength: 'strong', tone: 'good' });
    if (scope.evidence_grade === 'LIMITED') return line('권한 확인됨 (관리자 화면 확인)', { strength: 'limited', tone: 'info' });
    return line('권한 상태 확인 필요', { tone: 'warn' });
  }
  if (scope.status === 'MISSING') return line('권한 부족', { tone: 'bad' });
  if (scope.status === 'UNKNOWN') return line('권한 미확인', { strength: 'unverified' });
  return unexpected(scope.status);
}

// §14.5: always the write axis itself; overlays never overwrite it (§14.9).
export function writeLine(write) {
  if (write.status === 'UNVERIFIED') return line('미확인', { strength: 'unverified' });
  if (write.status === 'READY') return line('검증됨', { strength: 'strong', tone: 'good' });
  if (write.status === 'BLOCKED') return line('차단됨', { tone: 'bad' });
  return unexpected(write.status);
}

// §14.6: its own row, never an evidence-strength marker (F1). CURRENT is an operator recording.
export function freshnessLine(view) {
  const value = view.contract_freshness;
  if (value === 'UNRECORDED') return line('API 계약 상태 미확인');
  if (value === 'STALE') return line('API 계약 재검토 필요', { tone: 'warn' });
  if (value === 'REVIEW_REQUIRED') return line('API 계약 검토 필요', { tone: 'bad' });
  if (value === 'CURRENT') {
    const recorded = view.contract_freshness_recorded_at;
    return line(recorded ? `API 계약 확인됨 (운영자 기록 · ${dotDate(recorded)})` : 'API 계약 확인됨 (운영자 기록)');
  }
  return unexpected(value);
}

// §14.9: one 조치 필요 row per overlay, scoped by workflow_scope.
export function actionLines(view) {
  return view.workflow.map((item) => {
    const scope = SCOPE[item.workflow_scope] ?? item.workflow_scope;
    if (item.workflow_state === 'PAUSED') return line(`${scope} · ${REASON[item.reason_code] ?? item.reason_code}`, { tone: 'bad' });
    if (item.workflow_state === 'REVIEW_REQUIRED') return line(`${scope} · 확인 필요`, { tone: 'warn' });
    return unexpected(`${scope} · ${item.workflow_state}`);
  });
}

// §14.10: shown only while non-null; diagnostic context, never another line's input.
export function diagnosticLines(view) {
  const rows = [];
  if (view.error_class) {
    const gloss = ERROR_GLOSS[view.error_class];
    rows.push({ label: '최근 오류 분류', axis: 'error_class', line: line(gloss ? `${view.error_class} (${gloss})` : view.error_class) });
  }
  if (view.remote_outcome) {
    const gloss = OUTCOME_GLOSS[view.remote_outcome];
    rows.push({ label: '원격 결과', axis: 'remote_outcome', line: line(gloss ? `${view.remote_outcome} (${gloss})` : view.remote_outcome) });
  }
  return rows;
}

// A status chip whose strength marker stays visible when the text truncates (§14.1), and whose
// axis and strength stay machine-checkable (data-axis, data-strength).
export function statusChip(axis, status) {
  return h(
    'span',
    { class: status.tone ? `chip cap-status ${status.tone}` : 'chip cap-status', 'data-axis': axis },
    status.glyph
      ? h('span', { class: 'glyph', 'data-strength': status.strength, role: 'img', 'aria-label': MARK_LABEL[status.strength] }, status.glyph)
      : null,
    h('span', { class: 'cap-text' }, status.text),
  );
}
