// M6-D (ADR-0023 §5, §7, §8): the ingested SmartStore product orders, each with how it resolved to
// ICBM's product (never by name), its status and the masked recipient. 지금 동기화 runs the same
// read-only pass the schedule runs; 배송정보 opens one order's encrypted shipping record for this
// view only (the server audits it by id and never lets it be cached). 처리 opens the order's M6.5
// fulfillment (ADR-0025): the supplier order placed by hand and its tracking. Nothing here writes the
// marketplace.

import { getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { errorText } from '../core/errors.js';
import { dotDateTime } from '../core/format.js';
import { supplierName, supplierNames } from '../core/suppliers.js';
import { toast } from '../core/toast.js';
import { FULFILLMENT_STATE, fulfillmentPanel } from './fulfillment-panel.js';

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
  UNMATCHED: ['ICBM 밖에서 등록한 상품', 'warn'],
  ITEM_UNMATCHED: ['옵션 연결 필요', 'warn'],
  CONFLICT: ['판매자 상품코드가 다름', 'bad'],
};
const OUTCOME = {
  COMPLETED: '완료',
  NOT_ATTESTED: '주문 조회 권한 확인 필요',
  SESSION_UNAVAILABLE: '스마트스토어 연결 확인 필요',
  RATE_LIMITED: '요청이 많아 잠시 멈춤',
  FAILED: '실패',
  INTERRUPTED: '중단됨',
};
// SmartStore's claim states (cancel / return / exchange), in Korean.
const CLAIM = {
  CANCEL_REQUEST: '취소 요청',
  CANCELING: '취소 처리 중',
  CANCEL_DONE: '취소 완료',
  CANCEL_REJECT: '취소 철회',
  RETURN_REQUEST: '반품 요청',
  COLLECTING: '수거 중',
  COLLECT_DONE: '수거 완료',
  RETURN_DONE: '반품 완료',
  RETURN_REJECT: '반품 철회',
  EXCHANGE_REQUEST: '교환 요청',
  EXCHANGE_REDELIVERING: '교환 재배송 중',
  EXCHANGE_DONE: '교환 완료',
  EXCHANGE_REJECT: '교환 철회',
  PURCHASE_DECISION_HOLDBACK: '구매확정 보류',
  PURCHASE_DECISION_REQUEST: '구매확정 요청',
  ADMIN_CANCELING: '직권 취소 중',
  ADMIN_CANCEL_DONE: '직권 취소 완료',
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
      toast('배송정보', errorText(error));
      button.disabled = false;
    }
  });
  box.append(button);
  return box;
}

const COLUMNS = ['결제일', '상품', '상품 연결', '수량', '결제금액', '상태', '수령인', '배송', '처리'];

function panelRow(order, reload, names) {
  return h('tr', { 'data-role': 'fulfillment-row', 'data-order': order.product_order_id }, h('td', { colspan: String(COLUMNS.length) }, fulfillmentPanel(order, reload, names)));
}

// ``opened`` keeps which panels are open, so a save that reloads the list leaves them open.
function fulfillmentCell(order, reload, opened, names) {
  const [label, tone] = FULFILLMENT_STATE[order.fulfillment_state] ?? ['—', ''];
  const open = h('button', { type: 'button', class: 'btn', 'data-action': 'open-fulfillment' }, '처리');
  open.addEventListener('click', () => {
    const tr = open.closest('tr');
    const next = tr.nextElementSibling;
    if (next?.getAttribute('data-role') === 'fulfillment-row') {
      next.remove();
      opened.delete(order.product_order_id);
      return;
    }
    opened.add(order.product_order_id);
    tr.after(panelRow(order, reload, names));
  });
  return h('td', { 'data-fulfillment-state': order.fulfillment_state ?? 'UNKNOWN' }, h('span', { class: `chip ${tone}`.trim() }, label), open);
}

function row(order, reload, opened, names) {
  // No resolution yet: the product is one ICBM created and has not confirmed.
  const [label, tone] = order.resolution == null ? ['등록 확인 중', 'warn'] : RESOLUTION[order.resolution] ?? ['확인 필요', 'warn'];
  return h(
    'tr',
    { 'data-order': order.product_order_id },
    h('td', {}, order.paid_at ? dotDateTime(order.paid_at) : order.ordered_at ? dotDateTime(order.ordered_at) : '—', h('span', { class: 'mini' }, order.product_order_id)),
    // The name is ICBM's own registration's; the provider's product name is not kept (ADR-0023 §7).
    h(
      'td',
      { 'data-original-product': order.original_product_id ?? '' },
      order.product_label ?? `상품명 없음 (네이버 상품번호 ${order.original_product_id ?? '—'})`,
      order.option_manage_code ? h('span', { class: 'mini' }, `옵션 ${order.option_manage_code}`) : null,
    ),
    h(
      'td',
      {},
      h('span', { class: `chip ${tone}`.trim(), 'data-resolution': order.resolution ?? 'PENDING' }, label),
      order.supplier_key ? h('span', { class: 'mini' }, `${supplierName(names, order.supplier_key)} · 상품번호 ${order.source_product_id}`) : null,
      // ADR-0024 §4: an order of an adopted listing, linked to it beside its UNMATCHED resolution.
      order.adoption_id
        ? h(
            'span',
            { class: 'chip good', 'data-role': 'adoption-link', 'data-adoption': order.adoption_id },
            `가져온 상품 · ${supplierName(names, order.adopted_supplier_key)} · 상품번호 ${order.adopted_source_product_id}`,
          )
        : null,
    ),
    h('td', {}, order.quantity ?? '—'),
    h('td', {}, won(order.total_payment_amount)),
    h(
      'td',
      { 'data-status': order.status ?? 'UNKNOWN' },
      STATUS[order.status] ?? order.status ?? '—',
      order.claim_status ? h('span', { class: 'mini', 'data-claim': order.claim_status }, CLAIM[order.claim_status] ?? '취소·반품·교환 있음') : null,
      // ADR-0025 §6: the tracking the order read shows.
      order.tracking_number ? h('span', { class: 'mini', 'data-role': 'order-tracking' }, `송장 ${order.tracking_number}`) : null,
      order.wrong_tracking_number ? h('span', { class: 'chip warn' }, '오류 송장') : null,
    ),
    h('td', {}, order.recipient_masked ?? '—', order.phone_masked ? h('span', { class: 'mini' }, order.phone_masked) : null),
    h('td', {}, shippingButton(order)),
    fulfillmentCell(order, reload, opened, names),
  );
}

export function orderListPanel() {
  const panel = h('section', { class: 'panel order-list', 'data-role': 'order-list' }, h('h2', { class: 'panel-title' }, '스마트스토어 주문'));
  const content = h('div', { 'data-order-list': 'LOADING' }, h('span', { class: 'mini' }, '불러오는 중…'));
  panel.append(content);
  const opened = new Set();
  const reload = async () => {
    try {
      const [found, names] = await Promise.all([getJson(ORDERS), supplierNames()]);
      const button = h('button', { type: 'button', class: 'btn blue', 'data-action': 'sync-orders' }, '지금 동기화');
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const run = await sendJson('POST', SYNC, {});
          toast('주문 동기화', `${OUTCOME[run.outcome] ?? '확인 필요'} · 변경 ${run.changes}건 · 주문 ${run.orders_read}건`);
          await reload();
        } catch (error) {
          toast('주문 동기화', errorText(error));
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
              : found.attested
                ? '아직 주문을 읽은 적이 없습니다'
                : '주문을 가져오려면 설정 › 스마트스토어 › 등록 권한 확인에서 주문 권한을 체크하세요.',
          ),
          h('span', { class: 'mini' }, minutes > 0 ? `자동 동기화 ${minutes}분마다` : '자동 동기화 꺼짐'),
          last
            ? h(
                'span',
                { class: 'mini', 'data-order-last-run': last.outcome ?? last.state, 'data-error-code': last.error_code ?? '' },
                `마지막 동기화: ${OUTCOME[last.outcome] ?? (last.state === 'RUNNING' ? '진행 중' : '확인 필요')}`,
              )
            : null,
          button,
        ),
        found.orders.length
          ? h(
              'table',
              { class: 'table' },
              h('thead', {}, h('tr', {}, ...COLUMNS.map((label) => h('th', {}, label)))),
              h(
                'tbody',
                {},
                ...found.orders.flatMap((order) =>
                  opened.has(order.product_order_id)
                    ? [row(order, reload, opened, names), panelRow(order, reload, names)]
                    : [row(order, reload, opened, names)],
                ),
              ),
            )
          : h('div', { class: 'note' }, found.capability === 'CONNECTED' ? '동기화된 주문이 없습니다.' : '주문 동기화를 실행하면 주문이 표시됩니다.'),
      );
    } catch (error) {
      content.setAttribute('data-order-list', 'UNAVAILABLE');
      content.replaceChildren(h('span', { class: 'chip warn' }, `주문을 불러오지 못했습니다. ${errorText(error)}`));
    }
  };
  reload();
  return panel;
}
