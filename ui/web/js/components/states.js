// Empty, unsupported and error surfaces share the v28 zero-data card shape.

import { h } from '../core/dom.js';

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

// M0 renders EMPTY contracts only. Any other state is surfaced honestly instead of guessed at.
export function unsupportedState(meta) {
  return h(
    'div',
    { class: 'empty-state state-unsupported', role: 'status' },
    h(
      'div',
      {},
      h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '!'),
      h('h3', {}, '이 상태의 화면은 아직 구현되지 않았습니다'),
      h('div', { class: 'mini' }, `M0 UI는 빈 상태만 표시합니다. 계약 상태: ${meta?.state ?? '알 수 없음'}`),
    ),
  );
}

export function errorState(error) {
  const detail = error?.error ? `${error.error.class} · ${error.error.code}` : String(error?.message ?? error);
  const correlationId = error?.correlationId ?? error?.error?.correlation_id;
  return h(
    'div',
    { class: 'empty-state state-error', role: 'alert' },
    h(
      'div',
      {},
      h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '×'),
      h('h3', {}, '화면 계약을 불러오지 못했습니다'),
      h('div', { class: 'mini' }, detail),
      correlationId ? h('div', { class: 'mini' }, `correlation_id: ${correlationId}`) : null,
    ),
  );
}
