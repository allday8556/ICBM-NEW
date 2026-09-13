import { h } from './dom.js';

export function toast(title, message = '') {
  const stack = document.getElementById('toastStack');
  if (!stack) return;
  const el = h('div', { class: 'toast', role: 'status' }, h('b', {}, title), message);
  stack.append(el);
  window.setTimeout(() => el.remove(), 2600);
}
