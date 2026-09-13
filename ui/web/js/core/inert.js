// M0 controls that exist visually but are not bound to any contract yet (Issue #1, ADR-0004).
// Activating one only explains that; it never calls the server, a supplier or a marketplace.

import { toast } from './toast.js';

export const M0_NOTICE =
  'M0 표시 전용입니다. 이 기능은 이후 마일스톤에서 새 계약으로 연결되며, 지금은 외부 호출·쓰기가 없습니다.';

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
      toast(target.dataset.inert, M0_NOTICE);
    },
    true,
  );
}
