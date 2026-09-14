// 연동 상태 / 권한 (M2 PR-D): the read-only three-layer projection of SmartStore capability truth
// (CAPABILITY_MAPPING §14.11, surface 1). It renders what the capability read API returned and
// decides nothing. Mutating operator actions belong to PR-E.

import { h } from '../core/dom.js';
import { actionLines, authLine, diagnosticLines, freshnessLine, permissionLine, statusChip, writeLine } from '../core/capability.js';

function row(label, axis, status) {
  return h('div', { class: 'kv' }, h('span', {}, label), statusChip(axis, status));
}

export function capabilityProjection(key, view) {
  if (!view) return h('div', { class: 'note' }, '연동 상태를 불러오지 못했습니다');
  return h(
    'div',
    { class: 'capability-projection', 'data-marketplace': key },
    row('인증', 'auth', authLine(view)),
    row('등록 권한', 'permission', permissionLine(view.write_scope)),
    row('실제 등록', 'write', writeLine(view.write)),
    row('API 계약', 'freshness', freshnessLine(view)),
    actionLines(view).map((status) => row('조치 필요', 'action', status)),
    diagnosticLines(view).map((item) => row(item.label, item.axis, item.line)),
  );
}
