import { h } from '../core/dom.js';
import { withHelp } from '../core/help.js';

export function pageHead({ title, help, actions = [] }) {
  return h(
    'div',
    { class: 'page-head' },
    h('div', {}, withHelp(h('h1', { class: 'page-title' }, title), help)),
    actions.length ? h('div', { class: 'page-actions' }, actions) : null,
  );
}

export function datePill(text) {
  return h('span', { class: 'date-pill' }, text);
}
