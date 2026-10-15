// Empty, unsupported and error surfaces share the v28 zero-data card shape.

import { h } from '../core/dom.js';
import { errorCode, errorText } from '../core/errors.js';

export function emptyState({ title, copy, action }) {
  return h(
    'div',
    { class: 'empty-state' },
    h(
      'div',
      {},
      h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '◇'),
      h('h3', {}, title),
      h('div', { class: 'mini' }, copy),
      action ? h('button', { type: 'button', class: 'btn blue', onclick: action.onSelect }, action.label) : null,
    ),
  );
}

// A screen state the page has nothing to draw for. The seller sees one plain line; the server's
// state name stays in a data attribute for support.
export function unsupportedState(meta) {
  return h(
    'div',
    { class: 'empty-state state-unsupported', role: 'status', 'data-screen-state': meta?.state ?? '' },
    h(
      'div',
      {},
      h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '!'),
      h('h3', {}, '이 화면을 표시할 수 없습니다'),
      h('div', { class: 'mini' }, '화면을 새로 고치거나 잠시 후 다시 열어 주세요.'),
    ),
  );
}

// Support-only details (an error code, a request number), folded away under one summary line.
export function supportDetails(lines) {
  const shown = lines.filter(Boolean);
  if (!shown.length) return null;
  return h(
    'details',
    { class: 'support-details' },
    h('summary', {}, '문제 해결용 정보'),
    ...shown.map((line) => h('div', { class: 'mini mono' }, line)),
  );
}

export function errorState(error) {
  const code = errorCode(error);
  const correlationId = error?.correlationId ?? error?.error?.correlation_id;
  return h(
    'div',
    { class: 'empty-state state-error', role: 'alert', 'data-error-code': code ?? '' },
    h(
      'div',
      {},
      h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '×'),
      h('h3', {}, '화면을 불러오지 못했습니다'),
      h('div', { class: 'mini' }, errorText(error)),
      supportDetails([code ? `오류 코드 ${code}` : null, correlationId ? `요청 번호 ${correlationId}` : null]),
    ),
  );
}
