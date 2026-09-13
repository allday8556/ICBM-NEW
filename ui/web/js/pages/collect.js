// 수집관리: 수집 / 공급처 관리 views. Both render from the collect contract; the supplier
// view's natural zero-data state is the "새 공급처 추가" card (v28 CONNECT surface).

import { getJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { dotDate } from '../core/format.js';
import { withHelp } from '../core/help.js';
import { M0_NOTICE, markInert } from '../core/inert.js';
import { openModal } from '../core/modal.js';
import { datePill, pageHead } from '../components/page-head.js';
import { emptyState, unsupportedState } from '../components/states.js';

const ENDPOINT = '/api/v1/screens/collect';
const TITLE = '수집관리';
const VIEWS = [
  ['jobs', '수집'],
  ['suppliers', '공급처 관리'],
];

let fieldSequence = 0;

function credentialField(label, attrs = {}) {
  fieldSequence += 1;
  const id = `supplier-field-${fieldSequence}`;
  return h(
    'div',
    { class: 'form-row' },
    h('label', { for: id }, label),
    h('input', { id, type: 'text', readonly: true, 'aria-readonly': 'true', autocomplete: 'off', placeholder: '미설정', ...attrs }),
  );
}

function openSupplierCredential() {
  openModal({
    narrow: true,
    eyebrow: 'CONNECT · Supplier Credential',
    title: '새 공급처 추가',
    help: '저장된 비밀번호는 화면에 다시 표시하지 않고 저장 여부만 표시합니다.',
    body: h(
      'div',
      { class: 'editor-card' },
      credentialField('공급처명'),
      credentialField('도메인', { placeholder: 'https://' }),
      credentialField('ID'),
      credentialField('Password', { type: 'password' }),
      h('div', { class: 'note' }, h('b', {}, '공급처 연결은 M1(K홀세일 CONNECT)에서 활성화됩니다.'), h('br'), M0_NOTICE),
    ),
    footer: [
      markInert(h('button', { type: 'button', class: 'btn' }, '연결 테스트')),
      markInert(h('button', { type: 'button', class: 'btn blue' }, '저장')),
    ],
  });
}

function jobsView(view, ctx) {
  if (view.meta.state !== 'EMPTY') return unsupportedState(view.meta);
  return emptyState({
    title: '아직 수집 작업이 없습니다',
    copy: '공급처를 연결한 뒤 첫 상품을 수집하세요.',
    // Collection needs a connected supplier first, so the CTA leads to supplier management.
    action: { label: '첫 상품 수집하기', onSelect: () => ctx.navigate('collect', { view: 'suppliers' }) },
  });
}

function suppliersView(view) {
  const header = h(
    'div',
    { class: 'panel supplier-head' },
    h(
      'div',
      { class: 'supplier-head-row' },
      withHelp(
        h('h3', { class: 'panel-title' }, '공급처 연결 관리'),
        '공급처 인증·세션·수집 프로필을 한 곳에서 확인합니다. 저장된 비밀번호는 다시 표시하지 않습니다.',
      ),
      h('button', { type: 'button', class: 'btn blue', onclick: openSupplierCredential }, '+ 공급처 추가'),
    ),
  );
  if (view.suppliers.length) return fragment(header, unsupportedState(view.meta));
  const addCard = h(
    'button',
    { type: 'button', class: 'supplier-card supplier-add-card', onclick: openSupplierCredential },
    h(
      'div',
      {},
      h('div', { class: 'empty-icon', 'aria-hidden': 'true' }, '＋'),
      h('b', {}, '새 공급처 추가'),
      h('div', { class: 'mini' }, '도메인과 로그인 정보를 등록한 뒤 연결 테스트와 사이트 분석을 진행합니다.'),
    ),
  );
  return fragment(header, h('div', { class: 'supplier-grid' }, addCard));
}

export default {
  key: 'collect',
  title: TITLE,
  navLabel: TITLE,
  icon: '◌',
  async render(ctx) {
    const view = await getJson(ENDPOINT);
    const active = ctx.params.get('view') === 'suppliers' ? 'suppliers' : 'jobs';
    const tabs = h(
      'div',
      { class: 'inner-tabs', role: 'tablist', 'aria-label': '수집관리 보기' },
      VIEWS.map(([key, label]) =>
        h(
          'button',
          {
            type: 'button',
            role: 'tab',
            'aria-selected': String(key === active),
            onclick: () => ctx.navigate('collect', { view: key }),
          },
          label,
        ),
      ),
    );
    return fragment(
      pageHead({
        title: TITLE,
        help: '국내외 도매몰 상품을 수집하고 자동 분석·검증합니다.',
        actions: [datePill(`${dotDate(view.meta.generated_at)}　오늘`)],
      }),
      tabs,
      active === 'suppliers' ? suppliersView(view) : jobsView(view, ctx),
    );
  },
};
