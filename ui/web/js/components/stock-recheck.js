// M6-B (ADR-0023 §4, §8): the listed source products' supplier stock, as the server judged it from
// each source's current revision, and the last recheck. The operator decides what to do with a
// live listing whose source is sold out (M6-07); nothing here changes a listing. Its one action —
// 지금 재확인 — asks COLLECT to re-collect the listed sources now, within COLLECT's own pacing.

import { getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { errorText } from '../core/errors.js';
import { dotDateTime } from '../core/format.js';
import { supplierName, supplierNames } from '../core/suppliers.js';
import { toast } from '../core/toast.js';

const STOCK = '/api/v1/operate/stock';
const RECHECK = '/api/v1/operate/stock/recheck';

const AVAILABILITY = {
  ON_SALE: ['판매중', 'good'],
  SOLD_OUT: ['품절', 'bad'],
  REVIEW_REQUIRED: ['확인 필요', 'warn'],
};
const OUTCOME = {
  RECORDED: '재수집 완료',
  NO_REVISION: '재수집했지만 상품 정보를 읽지 못함',
  FAILED: '재수집 실패',
  REFUSED: '재수집 거절',
};

function availabilityChip(value) {
  const [label, tone] = AVAILABILITY[value] ?? ['알 수 없음', ''];
  return h('span', { class: `chip ${tone}`.trim(), 'data-availability': value ?? 'UNKNOWN' }, label);
}

function row(source, names) {
  return h(
    'tr',
    { 'data-source': `${source.supplier_key}:${source.source_product_id}` },
    h(
      'td',
      {},
      source.product_name ?? '상품명 없음',
      h('span', { class: 'mini' }, `${supplierName(names, source.supplier_key)} · 상품번호 ${source.source_product_id}`),
    ),
    h('td', {}, availabilityChip(source.availability)),
    h(
      'td',
      {},
      source.pending
        ? h('span', { class: 'chip', 'data-recheck': 'PENDING' }, '재수집 중')
        : h(
            'span',
            { class: 'mini', 'data-recheck': source.last_recheck_outcome ?? 'NONE', 'data-error-code': source.last_recheck_error ?? '' },
            source.last_recheck_outcome ? OUTCOME[source.last_recheck_outcome] ?? '재수집 결과 확인 필요' : '아직 재확인 전',
          ),
    ),
    h('td', {}, source.last_finished_at ? dotDateTime(source.last_finished_at) : '—'),
    h(
      'td',
      {},
      source.needs_decision
        ? h('span', { class: 'chip bad', 'data-decision': 'NEEDED' }, '네이버에서 판매중지 결정 필요')
        : '—',
    ),
  );
}

export function stockRecheckPanel() {
  const panel = h(
    'section',
    { class: 'panel stock-recheck', 'data-role': 'stock-recheck' },
    h('h2', { class: 'panel-title' }, '등록 상품의 공급처 재고'),
  );
  const content = h('div', { 'data-stock-recheck': 'LOADING' }, h('span', { class: 'mini' }, '불러오는 중…'));
  panel.append(content);
  const reload = async () => {
    try {
      const [found, names] = await Promise.all([getJson(STOCK), supplierNames()]);
      const button = h('button', { type: 'button', class: 'btn blue', 'data-action': 'recheck-stock' }, '지금 재확인');
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const result = await sendJson('POST', RECHECK, {});
          toast('공급처 재고', `${result.requested}건 재확인을 요청했습니다${result.settled ? ` · ${result.settled}건 완료` : ''}.`);
          await reload();
        } catch (error) {
          toast('공급처 재고', errorText(error));
          button.disabled = false;
        }
      });
      const hours = Math.round((found.interval_s ?? 0) / 3600);
      content.setAttribute('data-stock-recheck', 'READY');
      content.replaceChildren(
        h(
          'div',
          { class: 'supplier-head-row' },
          h('span', { class: 'mini', 'data-recheck-interval': String(found.interval_s) }, hours > 0 ? `자동 재확인 ${hours}시간마다 · 한 번에 최대 ${found.cap}개` : '자동 재확인 꺼짐'),
          button,
        ),
        found.sources.length
          ? h(
              'table',
              { class: 'table' },
              h('thead', {}, h('tr', {}, ...['상품', '공급처 재고', '마지막 재확인', '재확인 시각', '결정'].map((label) => h('th', {}, label)))),
              h('tbody', {}, ...found.sources.map((source) => row(source, names))),
            )
          : h('div', { class: 'note' }, '네이버에 판매 중인 등록 상품이 없습니다.'),
      );
    } catch (error) {
      content.setAttribute('data-stock-recheck', 'UNAVAILABLE');
      content.replaceChildren(h('span', { class: 'chip warn' }, `공급처 재고를 불러오지 못했습니다. ${errorText(error)}`));
    }
  };
  reload();
  return panel;
}
