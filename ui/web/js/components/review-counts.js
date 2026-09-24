// Review counts per kind, as the server decided them (Gate 2 G2-C, ADR-0016 §7).
//
// The server owns every state here. A count is shown as a number only when the server says it
// is authoritative (CURRENT); NOT_WIRED and NOT_CURRENT never render as 0. The rows the server
// knows are shown as what they are — known rows, a lower bound — never as the count.

import { h } from '../core/dom.js';

export const KIND_LABEL = {
  COLLECT_EVIDENCE: '수집 근거',
  STOCK: '재고(품절)',
  SOURCE_CHANGE: '원천 변경',
  COMPLIANCE: '컴플라이언스',
  REGISTRATION_ERROR: '등록 오류',
  FULFILLMENT: '주문 이행',
};

const STATE = {
  CURRENT: { label: '최신', tone: 'good' },
  NOT_CURRENT: { label: '최신 아님', tone: 'warn' },
  NOT_WIRED: { label: '집계 전', tone: 'info' },
};

const REASON_COPY = {
  REVIEW_PRODUCER_NOT_IMPLEMENTED: '이 항목을 만드는 기능이 아직 없습니다',
  REVIEW_PRODUCER_NO_FULL_PASS: '전체 점검이 아직 한 번도 끝나지 않았습니다',
  REVIEW_COVERAGE_NO_PASS_THIS_RUN: '이번 실행에서 전체 점검이 아직 끝나지 않았습니다',
  REVIEW_COVERAGE_STALE: '마지막 전체 점검이 오래되었습니다',
  REVIEW_INDEX_FAILURE_UNRECOVERED: '색인 실패가 아직 복구되지 않았습니다',
  REVIEW_OWNER_MOVED_DURING_PASS: '점검 중 원본이 바뀌었습니다',
  REVIEW_OWNER_MOVED_SINCE_PASS: '점검 이후 원본이 바뀌어 다시 점검 중입니다',
  REVIEW_OWNER_TRUTH_UNREADABLE: '원본 상태를 읽지 못했습니다',
};

// The count cell: a number only for CURRENT; otherwise what is known, never a zero.
export function countText(view) {
  if (view.state === 'CURRENT') return `${view.open}건`;
  return view.open_known > 0 ? `확인된 ${view.open_known}건 이상` : '집계할 수 없음';
}

function why(view) {
  const blocked = view.emitters.filter((e) => !e.wired || !e.current);
  return blocked.map((e) => `${e.producer}: ${REASON_COPY[e.reason] ?? e.reason ?? ''}`).join(' · ');
}

export function reviewCountRow(view) {
  const state = STATE[view.state] ?? { label: view.state, tone: '' };
  return h(
    'tr',
    { 'data-kind': view.kind, 'data-state': view.state },
    h('td', {}, KIND_LABEL[view.kind] ?? view.kind),
    h('td', {}, h('span', { class: `chip ${state.tone}` }, state.label)),
    h('td', { 'data-role': 'review-count' }, countText(view)),
    h('td', { class: 'mini' }, why(view)),
  );
}

export function reviewCountTable(views) {
  return h(
    'table',
    { class: 'table', 'data-role': 'review-counts' },
    h('thead', {}, h('tr', {}, h('th', {}, '검토 종류'), h('th', {}, '집계 상태'), h('th', {}, '열린 항목'), h('th', {}, '사유'))),
    h('tbody', {}, ...views.map(reviewCountRow)),
  );
}
