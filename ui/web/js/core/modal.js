// One modal owner: backdrop, Escape, focus trap and focus restore.

import { h } from './dom.js';
import { withHelp } from './help.js';

let active = null;
let sequence = 0;

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function trapFocus(event, container) {
  const focusables = [...container.querySelectorAll(FOCUSABLE)].filter((el) => !el.hasAttribute('disabled'));
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export function closeModal() {
  if (!active) return;
  const { root, onKey, previousFocus } = active;
  active = null;
  document.removeEventListener('keydown', onKey);
  root.remove();
  if (previousFocus instanceof HTMLElement) previousFocus.focus();
}

export function openModal({ eyebrow, title, help, body, footer = [], narrow = false }) {
  closeModal();
  sequence += 1;
  const titleId = `modal-title-${sequence}`;
  const previousFocus = document.activeElement;
  const closeButton = h('button', { type: 'button', class: 'btn', onclick: closeModal }, '닫기');
  const card = h(
    'div',
    { class: `modal-card${narrow ? ' narrow' : ''}` },
    h(
      'div',
      { class: 'modal-head' },
      h('div', {}, eyebrow ? h('div', { class: 'mini' }, eyebrow) : null, withHelp(h('h2', { id: titleId }, title), help)),
      closeButton,
    ),
    h('div', { class: 'modal-body' }, body),
    footer.length ? h('div', { class: 'modal-foot' }, footer) : null,
  );
  const root = h(
    'div',
    { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId },
    h('div', { class: 'modal-backdrop', onclick: closeModal }),
    card,
  );
  const onKey = (event) => {
    if (event.key === 'Escape') closeModal();
    else if (event.key === 'Tab') trapFocus(event, card);
  };
  document.addEventListener('keydown', onKey);
  document.body.append(root);
  active = { root, onKey, previousFocus };
  closeButton.focus();
}
