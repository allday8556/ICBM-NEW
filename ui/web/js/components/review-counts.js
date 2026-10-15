// Review counts per kind, as the server decided them (Gate 2 G2-C, ADR-0016 §7).
//
// The server owns every state here. A count is shown as a number only when the server says it
// is authoritative (CURRENT); NOT_WIRED and NOT_CURRENT never render as 0. The rows the server
// knows are shown as what they are — a lower bound — never as the count. How the server counts
// (its producers and their coverage) is not the seller's concern: it stays in data attributes.

import { h } from '../core/dom.js';

export const KIND_LABEL = {
  COLLECT_EVIDENCE: '상품 정보 확인',
  STOCK: '품절 확인',
  SOURCE_CHANGE: '공급처 정보 변경',
  COMPLIANCE: '판매 금지 확인',
  REGISTRATION_ERROR: '등록 오류',
  FULFILLMENT: '주문 처리',
};

// The count cell: a number only for CURRENT; otherwise what is known, never a zero.
export function countText(view) {
  if (view.state === 'CURRENT') return `${view.open}건`;
  if (view.open_known > 0) return `확인 필요 ${view.open_known}건 이상`;
  return view.state === 'NOT_WIRED' ? '준비 중' : '확인 중';
}

function tone(view) {
  if (view.state === 'CURRENT') return view.open > 0 ? 'warn' : 'good';
  return view.open_known > 0 ? 'warn' : 'info';
}

export function reviewCountRow(view) {
  return h(
    'tr',
    { 'data-kind': view.kind, 'data-state': view.state },
    h('td', {}, KIND_LABEL[view.kind] ?? '기타 확인'),
    h('td', { 'data-role': 'review-count' }, h('span', { class: `chip ${tone(view)}` }, countText(view))),
  );
}

export function reviewCountTable(views) {
  return h(
    'table',
    { class: 'table', 'data-role': 'review-counts' },
    h('thead', {}, h('tr', {}, h('th', {}, '확인할 일'), h('th', {}, '건수'))),
    h('tbody', {}, ...views.map(reviewCountRow)),
  );
}
