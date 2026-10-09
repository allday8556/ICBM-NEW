// M6.5 (ADR-0025 §3, §4, §5, §8): one order's fulfillment. The operator orders from the supplier
// by hand — 배송지 복사 opens the shipping record through the audited route and copies it — then
// records the supplier's order number and the purchase amount, and types the carrier and tracking
// number in. Every save sends the revision it read; a stale one is refused and the panel reloads.
// 발송처리 asks the server for the one marketplace write: the send-time safety stack decides, and a
// refusal names every layer that still blocks it. Nothing here writes to a supplier.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { dotDateTime } from '../core/format.js';
import { toast } from '../core/toast.js';

const ORDERS = '/api/v1/operate/orders';
const CARRIERS = '/api/v1/operate/carriers';
// The carriers most first-vertical suppliers ship with, offered first (the rest follow).
const COMMON = ['CJGLS', 'HANJIN', 'HYUNDAI', 'KGB', 'EPOST'];

export const FULFILLMENT_STATE = {
  NOT_FULFILLABLE: ['처리 불가', 'bad'],
  NOT_PAYED: ['처리 대상 아님', ''],
  AWAITING_SUPPLIER_ORDER: ['공급사 주문 전', 'warn'],
  SUPPLIER_ORDERED: ['공급사 주문됨', 'info'],
  TRACKING_CAPTURED: ['송장 입력됨', 'good'],
  DISPATCHING: ['발송처리 중', 'info'],
  DISPATCH_SENT: ['발송처리 완료', 'good'],
  DISPATCH_REJECTED: ['발송처리 거절', 'bad'],
  DISPATCH_UNKNOWN: ['발송처리 확인 필요', 'warn'],
  DISPATCH_CONFLICT: ['송장 불일치', 'bad'],
  DISPATCHED: ['발송됨', 'info'],
  DELIVERED: ['배송완료', 'good'],
};
const ATTEMPT = {
  STARTED: '전송 중',
  APPLIED_PROVEN: '네이버 처리 완료',
  REJECTED: '네이버가 거절',
  NOT_APPLIED_PROVEN: '전송되지 않음',
  UNKNOWN: '결과 불명 — 다시 보내지 않음',
};
const VERIFICATION = {
  DISPATCH_CONFIRMED: '다시 읽어 확인됨',
  UNDISPATCHED: '미발송 확인 — 다시 승인 가능',
  CONFLICT: '다른 송장이 등록됨',
};
// The send-time layers (ADR-0018 §4.3; ADR-0025 §5) a refusal names.
const LAYER = {
  EXECUTION_MODE: 'LIVE 모드 아님',
  PROTECTED_WRITE_BRAKE: '쓰기 브레이크 잠김',
  GRANT: '이 주문의 발송 승인 없음',
  ENDPOINT_ADOPTED: '발송 API 미채택',
  EVIDENCE_RETENTION: '증거 보존 미증명',
  PERMISSION_ATTESTED: '주문 판매자 권한 미확인',
};
const DISPATCH_REASON = {
  OPERATE_DISPATCH_TRACKING_MISSING: '송장을 먼저 입력하세요.',
  OPERATE_DISPATCH_BLOCKED: '이 주문은 이미 발송처리를 보냈습니다. 결과가 불명이면 판매자센터에서 확인하세요.',
  OPERATE_DISPATCH_METHOD_UNSUPPORTED: '택배 배송 주문만 발송처리할 수 있습니다.',
  OPERATE_ORDER_ALREADY_DISPATCHED: '네이버에 이미 송장이 등록된 주문입니다.',
  OPERATE_DISPATCH_ACCOUNT_UNBOUND: '스마트스토어 계정 연결이 필요합니다.',
};
// ADR-0025 §6: the documented deliveryStatus enumeration (packet D).
const DELIVERY = {
  COLLECT_REQUEST: '수거 요청',
  COLLECT_WAIT: '수거 대기',
  COLLECT_CARGO: '집화',
  DELIVERING: '배송중',
  DELIVERY_COMPLETION: '배송 완료',
  DELIVERY_FAIL: '배송 실패',
  WRONG_INVOICE: '오류 송장',
  COLLECT_CARGO_FAIL: '집화 실패',
  COLLECT_CARGO_CANCEL: '집화 취소',
  NOT_TRACKING: '배송 추적 없음',
};
const REASON = {
  OPERATE_ORDER_NOT_FULFILLABLE: 'ICBM 상품에 연결되지 않은 주문입니다 (연결됨 또는 가져온 상품만 처리할 수 있습니다).',
  OPERATE_ORDER_NOT_PAYED: '결제 완료 상태의 주문만 공급사 주문과 송장을 기록할 수 있습니다.',
};
const ACTION = {
  RECORDED: '공급사 주문 기록',
  AMENDED: '공급사 주문 수정',
  TRACKING_CAPTURED: '송장 입력',
  TRACKING_AMENDED: '송장 수정',
};

let carrierList = null;

async function carriers() {
  if (carrierList === null) carrierList = (await getJson(CARRIERS)).carriers;
  return carrierList;
}

function message(error) {
  return error instanceof ApiError ? error.message : String(error?.message ?? error);
}

function won(value) {
  return typeof value === 'number' ? `${value.toLocaleString('ko-KR')}원` : '—';
}

function field(label, input) {
  return h('label', { class: 'field' }, h('span', { class: 'mini' }, label), input);
}

async function copyShipping(productOrderId, button) {
  button.disabled = true;
  try {
    const record = await getJson(`${ORDERS}/${encodeURIComponent(productOrderId)}/shipping`);
    const text = [
      record.recipient_name ?? '',
      [record.phone1, record.phone2].filter(Boolean).join(' / '),
      `(${record.zip_code ?? ''}) ${record.base_address ?? ''} ${record.detail_address ?? ''}`.trim(),
      record.memo ? `메모: ${record.memo}` : '',
    ]
      .filter(Boolean)
      .join('\n');
    await navigator.clipboard.writeText(text);
    toast('배송지 복사', '수령인·연락처·주소를 복사했습니다. 공급사 주문서에 붙여넣으세요.');
  } catch (error) {
    toast('배송지 복사', message(error));
  } finally {
    button.disabled = false;
  }
}

function supplierOrderForm(view, onSaved) {
  const reference = h('input', { type: 'text', maxlength: '100', value: view.supplier_order_ref ?? '', 'data-field': 'supplier-order-ref' });
  const amount = h('input', { type: 'number', min: '0', step: '1', value: view.purchase_amount ?? '', 'data-field': 'purchase-amount' });
  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-supplier-order' }, view.revision ? '공급사 주문 수정' : '공급사 주문 기록');
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const saved = await sendJson('PUT', `${ORDERS}/${encodeURIComponent(view.product_order_id)}/supplier-order`, {
        supplier_order_ref: reference.value.trim(),
        purchase_amount: Number.parseInt(amount.value, 10),
        expected_revision: view.revision ?? null,
      });
      toast('공급사 주문', '기록했습니다.');
      onSaved(saved);
    } catch (error) {
      toast('공급사 주문', message(error));
      save.disabled = false;
      if (error instanceof ApiError && error.status === 409) onSaved(null);
    }
  });
  return h('div', { class: 'fulfillment-step', 'data-step': 'supplier-order' }, h('b', {}, '1. 공급사 주문'), field('공급사 주문번호', reference), field('구매가 (원)', amount), save);
}

function trackingForm(view, list, onSaved) {
  const ordered = [...COMMON.map((code) => list.find((c) => c.code === code)).filter(Boolean), ...list.filter((c) => !COMMON.includes(c.code))];
  const carrier = h(
    'select',
    { 'data-field': 'carrier-code' },
    h('option', { value: '' }, '택배사 선택'),
    ...ordered.map((c) => h('option', { value: c.code }, c.name)),
  );
  if (view.carrier_code) carrier.value = view.carrier_code;
  const number = h('input', { type: 'text', maxlength: '50', value: view.tracking_number ?? '', 'data-field': 'tracking-number' });
  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-tracking' }, view.tracking_number ? '송장 수정' : '송장 입력');
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const saved = await sendJson('PUT', `${ORDERS}/${encodeURIComponent(view.product_order_id)}/tracking`, {
        carrier_code: carrier.value,
        tracking_number: number.value.trim(),
        expected_revision: view.revision,
      });
      toast('송장', '입력했습니다.');
      onSaved(saved);
    } catch (error) {
      toast('송장', message(error));
      save.disabled = false;
      if (error instanceof ApiError && error.status === 409) onSaved(null);
    }
  });
  return h('div', { class: 'fulfillment-step', 'data-step': 'tracking' }, h('b', {}, '2. 송장'), field('택배사', carrier), field('송장번호', number), save);
}

// What the SmartStore order read shows of the shipment (ADR-0025 §6), and whether it is the
// tracking captured here.
function deliveryReadback(view) {
  if (!view.delivery_tracking_number && !view.delivery_status) return null;
  const parts = [
    view.delivery_company_name ?? view.delivery_company ?? '택배사 —',
    view.delivery_tracking_number ? `송장 ${view.delivery_tracking_number}` : null,
    view.delivery_status ? DELIVERY[view.delivery_status] ?? view.delivery_status : null,
    view.sent_at ? `발송 ${dotDateTime(view.sent_at)}` : null,
    view.delivered_at ? `배송완료 ${dotDateTime(view.delivered_at)}` : null,
  ].filter(Boolean);
  return h(
    'div',
    { class: 'supplier-head-row', 'data-role': 'delivery-readback' },
    h('b', {}, '네이버 배송 정보'),
    h('span', { class: 'mini' }, parts.join(' · ')),
    view.tracking_matches === false ? h('span', { class: 'chip bad', 'data-role': 'tracking-mismatch' }, '입력한 송장과 다름') : null,
    view.wrong_tracking_number ? h('span', { class: 'chip warn', 'data-role': 'wrong-tracking' }, '오류 송장 — 확인 필요') : null,
  );
}

function dispatchStep(view, attempts, onDone) {
  const send = h('button', { type: 'button', class: 'btn blue', 'data-action': 'dispatch-order' }, '발송처리');
  const note = h('div', { class: 'mini', 'data-role': 'dispatch-blockers' });
  send.addEventListener('click', async () => {
    send.disabled = true;
    try {
      const attempt = await sendJson('POST', `${ORDERS}/${encodeURIComponent(view.product_order_id)}/dispatch`, {});
      toast('발송처리', `${ATTEMPT[attempt.state] ?? attempt.state}${attempt.verification ? ` · ${VERIFICATION[attempt.verification] ?? attempt.verification}` : ''}`);
      onDone();
    } catch (error) {
      const layers = error instanceof ApiError ? error.error?.details?.layers ?? [] : [];
      const reason = error instanceof ApiError ? DISPATCH_REASON[error.error?.code] : null;
      note.replaceChildren(
        layers.length
          ? `보내지 않았습니다 · ${layers.map((layer) => LAYER[layer.layer] ?? layer.layer).join(', ')}`
          : reason ?? `보내지 않았습니다 · ${message(error)}`,
      );
      send.disabled = false;
    }
  });
  const latest = attempts.at(-1);
  const verify = latest && !latest.verification && ['APPLIED_PROVEN', 'REJECTED', 'UNKNOWN'].includes(latest.state)
    ? h('button', { type: 'button', class: 'btn', 'data-action': 'verify-dispatch' }, '결과 다시 확인')
    : null;
  verify?.addEventListener('click', async () => {
    verify.disabled = true;
    try {
      await sendJson('POST', `${ORDERS}/${encodeURIComponent(view.product_order_id)}/dispatch/verify`, {});
      onDone();
    } catch (error) {
      toast('발송처리', message(error));
      verify.disabled = false;
    }
  });
  return h(
    'div',
    { class: 'fulfillment-step', 'data-step': 'dispatch' },
    h('b', {}, '3. 발송처리'),
    send,
    verify,
    h('span', { class: 'mini' }, '네이버에 택배사·송장을 등록합니다. LIVE 승인이 있을 때만 전송됩니다.'),
    note,
    attempts.length
      ? h(
          'ul',
          { class: 'mini', 'data-role': 'dispatch-attempts' },
          ...attempts.map((attempt) =>
            h(
              'li',
              {},
              `${attempt.attempt_no}회차 · ${dotDateTime(attempt.started_at)} · 송장 ${attempt.tracking_number} · ${ATTEMPT[attempt.state] ?? attempt.state}${attempt.fail_code ? ` (${attempt.fail_code})` : ''}${attempt.verification ? ` · ${VERIFICATION[attempt.verification] ?? attempt.verification}` : ''}`,
            ),
          ),
        )
      : null,
  );
}

function history(view) {
  if (!view.history.length) return null;
  return h(
    'ul',
    { class: 'mini', 'data-role': 'fulfillment-history' },
    ...view.history.map((entry) =>
      h(
        'li',
        {},
        `${dotDateTime(entry.recorded_at)} · ${ACTION[entry.action] ?? entry.action} · 주문번호 ${entry.supplier_order_ref} · ${won(entry.purchase_amount)}${entry.tracking_number ? ` · 송장 ${entry.tracking_number}` : ''}`,
      ),
    ),
  );
}

// The panel for one order, opened from its row. ``onChanged`` reloads the list after a save.
export function fulfillmentPanel(order, onChanged) {
  const box = h('div', { class: 'fulfillment-panel', 'data-role': 'fulfillment', 'data-order': order.product_order_id }, h('span', { class: 'mini' }, '불러오는 중…'));
  const render = async (view) => {
    try {
      const current = view ?? (await getJson(`${ORDERS}/${encodeURIComponent(order.product_order_id)}/fulfillment`));
      const [label, tone] = FULFILLMENT_STATE[current.state] ?? [current.state, ''];
      box.setAttribute('data-fulfillment', current.state);
      const head = h(
        'div',
        { class: 'supplier-head-row' },
        h('span', { class: `chip ${tone}`.trim() }, label),
        current.supplier_key ? h('span', { class: 'mini' }, `공급사 ${current.supplier_key} · 상품 ${current.source_product_id}`) : null,
      );
      if (current.state === 'NOT_FULFILLABLE' || (current.state === 'NOT_PAYED' && !current.revision)) {
        box.replaceChildren(head, h('div', { class: 'note' }, REASON[current.reason] ?? current.reason ?? ''), deliveryReadback(current));
        return;
      }
      const copy = h('button', { type: 'button', class: 'btn', 'data-action': 'copy-shipping' }, '배송지 복사');
      copy.disabled = order.shipping_state !== 'STORED';
      copy.addEventListener('click', () => copyShipping(order.product_order_id, copy));
      const saved = (next) => {
        render(next ?? undefined);
        onChanged?.();
      };
      const steps = [head, deliveryReadback(current), h('div', { class: 'supplier-head-row' }, copy, h('span', { class: 'mini' }, '공급사 주문서에 붙여넣을 배송지를 복사합니다 (열람 기록이 남습니다).')), supplierOrderForm(current, saved)];
      if (current.revision) steps.push(trackingForm(current, await carriers(), saved));
      if (current.tracking_number) steps.push(dispatchStep(current, current.dispatch_attempts ?? [], () => saved(null)));
      steps.push(history(current));
      box.replaceChildren(...steps.filter(Boolean));
    } catch (error) {
      box.replaceChildren(h('span', { class: 'chip warn' }, `처리 정보를 불러오지 못했습니다 · ${message(error)}`));
    }
  };
  render();
  return box;
}
