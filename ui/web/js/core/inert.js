// M0 controls that exist visually but are not bound to any contract yet (Issue #1, ADR-0004).
// Activating one only explains that; it never calls the server, a supplier or a marketplace.

import { toast } from './toast.js';

export const INERT_NOTICE = '준비 중인 기능입니다. 지금은 눌러도 아무것도 바뀌지 않습니다.';

export function markInert(el, label) {
  el.dataset.inert = label ?? el.textContent.trim();
  el.setAttribute('aria-disabled', 'true');
  return el;
}

export function initInert() {
  document.addEventListener(
    'click',
    (event) => {
      const target = event.target instanceof Element ? event.target.closest('[data-inert]') : null;
      if (!target || target.closest('.help-icon')) return;
      event.preventDefault();
      event.stopPropagation();
      toast(target.dataset.inert, INERT_NOTICE);
    },
    true,
  );
}
