// M6-D (ADR-0023 §5, §7, §8): the ingested SmartStore product orders, each with how it resolved to
// ICBM's product (never by name), its status and the masked recipient. 지금 동기화 runs the same
// read-only pass the schedule runs; 배송정보 opens one order's encrypted shipping record for this
// view only (the server audits it by id and never lets it be cached). Nothing here writes the
// marketplace.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { dotDateTime } from '../core/format.js';
import { toast } from '../core/toast.js';

const ORDERS = '/api/v1/operate/orders';
const SYNC = '/api/v1/operate/orders/sync';

const STATUS = {
  PAYMENT_WAITING: '결제 대기',
  PAYED: '결제 완료',
  DELIVERING: '배송 중',
  DELIVERED: '배송 완료',
  PURCHASE_DECIDED: '구매 확정',
  EXCHANGED: '교환',
  CANCELED: '취소',
  RETURNED: '반품',
  CANCELED_BY_NOPAYMENT: '미결제 취소',
};
const RESOLUTION = {
  MATCHED: ['연결됨', 'good'],
  UNMATCHED: ['ICBM 등록 아님', 'warn'],
  ITEM_UNMATCHED: ['옵션 연결 안 됨', 'warn'],
  CONFLICT: ['판매자 코드 불일치', 'bad'],
};
const OUTCOME = {
  COMPLETED: '완료',
  SESSION_UNAVAILABLE: '스마트스토어 세션 없음',
  RATE_LIMITED: '호출 한도로 중단',
  FAILED: '실패',
  INTERRUPTED: '중단됨',
};

function won(value) {
  return typeof value === 'number' ? `${value.toLocaleString('ko-KR')}원` : '—';
}

function shippingButton(order) {
  if (order.shipping_state !== 'STORED') {
    return h('span', { class: 'mini', 'data-shipping': order.shipping_state }, order.shipping_state === 'DELETED' ? '보관 기간 만료로 삭제됨' : '—');
  }
  const box = h('div', { 'data-shipping': 'STORED' });
  const button = h('button', { type: 'button', class: 'btn', 'data-action': 'open-shipping' }, '배송정보');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const record = await getJson(`${ORDERS}/${encodeURIComponent(order.product_order_id)}/shipping`);
      box.replaceChildren(
        h(
          'div',
          { class: 'mini', 'data-role': 'shipping-record' },
          h('div', {}, `${record.recipient_name ?? '—'} · ${record.phone1 ?? '—'}${record.phone2 ? ` / ${record.phone2}` : ''}`),
          h('div', {}, `(${record.zip_code ?? '—'}) ${record.base_address ?? ''} ${record.detail_address ?? ''}`.trim()),
          record.memo ? h('div', {}, `메모: ${record.memo}`) : null,
        ),
      );
    } catch (error) {
      toast('배송정보', error instanceof ApiError ? error.message : String(error?.message ?? error));
      button.disabled = false;
    }
  });
  box.append(button);
  return box;
}

function row(order) {
  const [label, tone] = RESOLUTION[order.resolution] ?? [order.resolution, ''];
  return h(
    'tr',
    { 'data-order': order.product_order_id },
    h('td', {}, order.paid_at ? dotDateTime(order.paid_at) : order.ordered_at ? dotDateTime(order.ordered_at) : '—', h('span', { class: 'mini' }, order.product_order_id)),
    // The name is ICBM's own registration's; the provider's product name is not kept (ADR-0023 §7).
    h('td', {}, order.product_label ?? `원상품 ${order.original_product_id ?? '—'}`, order.option_manage_code ? h('span', { class: 'mini' }, `옵션 ${order.option_manage_code}`) : null),
    h(
      'td',
      {},
      h('span', { class: `chip ${tone}`.trim(), 'data-resolution': order.resolution }, label),
      order.supplier_key ? h('span', { class: 'mini' }, `${order.supplier_key} · ${order.source_product_id}`) : null,
    ),
    h('td', {}, order.quantity ?? '—'),
    h('td', {}, won(order.total_payment_amount)),
    h('td', { 'data-status': order.status ?? 'UNKNOWN' }, STATUS[order.status] ?? order.status ?? '—', order.claim_status ? h('span', { class: 'mini' }, order.claim_status) : null),
    h('td', {}, order.recipient_masked ?? '—', order.phone_masked ? h('span', { class: 'mini' }, order.phone_masked) : null),
    h('td', {}, shippingButton(order)),
  );
}

export function orderListPanel() {
  const panel = h('section', { class: 'panel order-list', 'data-role': 'order-list' }, h('h2', { class: 'panel-title' }, '스마트스토어 주문'));
  const content = h('div', { 'data-order-list': 'LOADING' }, h('span', { class: 'mini' }, '불러오는 중…'));
  panel.append(content);
  const reload = async () => {
    try {
      const found = await getJson(ORDERS);
      const button = h('button', { type: 'button', class: 'btn blue', 'data-action': 'sync-orders' }, '지금 동기화');
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const run = await sendJson('POST', SYNC, {});
          toast('주문 동기화', `${OUTCOME[run.outcome] ?? run.outcome} · 변경 ${run.changes}건 · 주문 ${run.orders_read}건${run.error_code ? ` · ${run.error_code}` : ''}`);
          await reload();
        } catch (error) {
          toast('주문 동기화', error instanceof ApiError ? error.message : String(error?.message ?? error));
          button.disabled = false;
        }
      });
      const minutes = Math.round((found.interval_s ?? 0) / 60);
      const last = found.last_run;
      content.setAttribute('data-order-list', found.capability);
      content.replaceChildren(
        h(
          'div',
          { class: 'supplier-head-row' },
          h(
            'span',
            { class: 'mini', 'data-order-capability': found.capability },
            found.capability === 'CONNECTED'
              ? `주문 ${found.total}건 · ${found.synced_until ? `${dotDateTime(found.synced_until)}까지 동기화` : ''}`
              : '아직 주문을 읽은 적이 없습니다 (주문 조회 권한 미확인)',
          ),
          h('span', { class: 'mini' }, minutes > 0 ? `자동 동기화 ${minutes}분마다` : '자동 동기화 꺼짐'),
          last ? h('span', { class: 'mini', 'data-order-last-run': last.outcome ?? last.state }, `마지막: ${OUTCOME[last.outcome] ?? last.state}${last.error_code ? ` · ${last.error_code}` : ''}`) : null,
          button,
        ),
        found.orders.length
          ? h(
              'table',
              { class: 'table' },
              h('thead', {}, h('tr', {}, ...['결제일', '상품', 'ICBM 연결', '수량', '결제금액', '상태', '수령인', '배송'].map((label) => h('th', {}, label)))),
              h('tbody', {}, ...found.orders.map(row)),
            )
          : h('div', { class: 'note' }, found.capability === 'CONNECTED' ? '동기화된 주문이 없습니다.' : '주문 동기화를 실행하면 주문이 표시됩니다.'),
      );
    } catch (error) {
      content.setAttribute('data-order-list', 'UNAVAILABLE');
      content.replaceChildren(h('span', { class: 'chip warn' }, `주문을 불러오지 못했습니다 · ${String(error?.message ?? error)}`));
    }
  };
  reload();
  return panel;
}
