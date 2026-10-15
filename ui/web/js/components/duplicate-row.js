// ADR-0042 S3b (§0 a4): the duplicate row of one registration unit, as the seller sees it.
//
// Everything here is the server's answer (unit.duplicate): the store's last COMPLETE catalog sync
// decides it, and this row only says it in plain Korean. Codes stay in data-* attributes.
//
// - 「중복 없음 · 14:02 동기화 기준」 when the snapshot shows no matching store listing;
// - 「스토어 다시 확인 중…」 while the automatic re-sync runs, with 「다시 보기」 to read it again;
// - 「이미 스토어에 있는 상품: …」 with the seller's choice when a store listing matches:
//   「의도적으로 한 번 더 등록」, which records the seller's reason and can be undone. Linking
//   the listing as the same product (「같은 상품으로 연결」) is S2a's and is not shown until then.
// The last complete sync time is always shown beside any result (SCP-07).

import { ApiError, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { closeModal, openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';

const PREPARATIONS = '/api/v1/register/preparations';
const OPERATOR = 'operator';

const KEY_LABEL = {
  SELLER_CODE: '판매자코드가 같습니다',
  NORMALIZED_NAME: '상품명이 같습니다',
};
const SALE_LABEL = {
  SALE: '판매중',
  OUTOFSTOCK: '품절',
  SUSPENSION: '판매중지',
  CLOSE: '판매종료',
  PROHIBITION: '판매금지',
  WAIT: '판매대기',
};

const pad = (value) => String(value).padStart(2, '0');

// 14:02 today; 10/15 14:02 on another day.
export function syncTime(value) {
  if (!value) return null;
  const date = new Date(value);
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return date.toDateString() === new Date().toDateString() ? time : `${date.getMonth() + 1}/${date.getDate()} ${time}`;
}

function lastSync(duplicate) {
  const time = syncTime(duplicate.as_of);
  return h('span', { class: 'mini', 'data-role': 'duplicate-last-sync' }, time ? `마지막 동기화 ${time}` : '아직 동기화 기록이 없습니다');
}

function recheck(reload) {
  return h('button', { type: 'button', class: 'btn', 'data-action': 'duplicate-recheck', onclick: () => reload() }, '다시 보기');
}

function errorText(error) {
  if (error instanceof ApiError && error.error?.code === 'REGISTER_DUPLICATE_OVERRIDE_NOT_NEEDED') return '지금은 중복으로 확인된 상품이 없습니다.';
  return '처리하지 못했습니다. 잠시 후 다시 시도해 주세요.';
}

function overrideModal(unit, reload) {
  const reasonBox = h('textarea', { rows: '3', maxlength: '200', 'data-role': 'duplicate-reason', placeholder: '예: 구성이 다른 묶음 상품이라 따로 판매합니다' });
  const submit = h('button', { type: 'button', class: 'btn blue', 'data-action': 'duplicate-override-confirm' }, '한 번 더 등록하기');
  submit.addEventListener('click', async () => {
    const reasonText = reasonBox.value.trim();
    if (reasonText.length < 2) {
      toast('중복 확인', '이유를 두 글자 이상 적어 주세요.');
      return;
    }
    submit.disabled = true;
    try {
      await sendJson('POST', `${PREPARATIONS}/${encodeURIComponent(unit.authored.preparation_id)}/duplicate-override`, { actor: OPERATOR, reason: reasonText });
      closeModal();
      toast('중복 확인', '이 상품은 스토어에 한 번 더 등록하기로 기록했습니다.');
      await reload();
    } catch (error) {
      toast('중복 확인', errorText(error));
      submit.disabled = false;
    }
  });
  openModal({
    eyebrow: '중복 확인',
    title: '의도적으로 한 번 더 등록',
    help: '이미 스토어에 있는 상품과 같은 상품을 한 번 더 등록합니다. 선택은 기록되고 언제든 되돌릴 수 있습니다.',
    narrow: true,
    body: h('div', { class: 'stack' }, h('div', { class: 'note' }, '스토어에 같은 상품이 두 개 보이게 됩니다. 왜 한 번 더 등록하는지 적어 주세요.'), reasonBox),
    footer: [submit],
  });
}

async function undoOverride(unit, overrideId, reload, button) {
  button.disabled = true;
  try {
    await sendJson(
      'POST',
      `${PREPARATIONS}/${encodeURIComponent(unit.authored.preparation_id)}/duplicate-override/${encodeURIComponent(overrideId)}/revoke`,
      { actor: OPERATOR },
    );
    toast('중복 확인', '한 번 더 등록하기로 한 선택을 되돌렸습니다.');
    await reload();
  } catch (error) {
    toast('중복 확인', errorText(error));
    button.disabled = false;
  }
}

function matchLine(match) {
  const status = SALE_LABEL[match.sale_status] ? ` · ${SALE_LABEL[match.sale_status]}` : '';
  return h(
    'li',
    { 'data-key-kind': match.key_kind, 'data-product-no': match.marketplace_product_id },
    h('b', {}, match.name || '이름 없는 상품'),
    h('span', { class: 'mini' }, ` 상품번호 ${match.marketplace_product_id}${status} · ${KEY_LABEL[match.key_kind] ?? '같은 상품일 수 있습니다'}`),
  );
}

function matchBlock(unit, duplicate, reload) {
  const editable = Boolean(unit.authored?.preparation_id) && !unit.snapshot;
  const chosen = duplicate.override_ids.length > 0;
  const choices = chosen
    ? [
        h('span', { class: 'chip good', 'data-role': 'duplicate-override-active' }, '한 번 더 등록하기로 함'),
        ...duplicate.override_ids.map((overrideId) => {
          const undo = h('button', { type: 'button', class: 'btn', 'data-action': 'duplicate-override-undo', 'data-override': overrideId }, '되돌리기');
          undo.addEventListener('click', () => undoOverride(unit, overrideId, reload, undo));
          return undo;
        }),
      ]
    : [
        (() => {
          const again = h('button', { type: 'button', class: 'btn', 'data-action': 'duplicate-override' }, '의도적으로 한 번 더 등록');
          again.disabled = !editable;
          again.addEventListener('click', () => overrideModal(unit, reload));
          return again;
        })(),
      ];
  return [
    h('div', {}, h('b', {}, '이미 스토어에 있는 상품:')),
    h('ul', { class: 'duplicate-matches' }, ...duplicate.matches.map(matchLine)),
    chosen ? null : h('div', { class: 'mini' }, '같은 상품을 두 번 등록하지 않도록 등록을 멈췄습니다. 다른 상품이라면 이유를 적고 한 번 더 등록할 수 있습니다.'),
    h('div', { class: 'row' }, ...choices),
  ];
}

export function duplicateRow(unit, reload) {
  const duplicate = unit.duplicate;
  if (!duplicate) return null;
  const state = duplicate.state;
  const holder = h('div', { class: 'register-duplicate stack', 'data-role': 'duplicate-row', 'data-duplicate-state': state, 'data-reason': (duplicate.codes ?? []).join(' ') || null });
  const head = (text, tone) => h('div', { class: 'supplier-head-row' }, h('b', {}, '중복 확인'), h('span', { class: tone ? `chip ${tone}` : 'chip' }, text));
  if (state === 'NO_MATCH') {
    const time = syncTime(duplicate.as_of);
    holder.append(head(`중복 없음 · ${time} 동기화 기준`, 'good'));
  } else if (state === 'MATCH') {
    holder.append(head('중복 의심', 'warn'), ...matchBlock(unit, duplicate, reload).filter(Boolean), lastSync(duplicate));
  } else if (state === 'RESYNCING') {
    // Never re-read by itself: an automatic reload would drop what the seller is typing elsewhere.
    holder.append(head('스토어 다시 확인 중…', 'info'), h('div', { class: 'row' }, lastSync(duplicate), recheck(reload)));
  } else if (state === 'STALE') {
    holder.append(head('스토어 확인이 오래되었습니다', 'warn'), h('div', { class: 'mini' }, '잠시 후 스토어를 다시 확인합니다.'), h('div', { class: 'row' }, lastSync(duplicate), recheck(reload)));
  } else {
    holder.append(
      head('스토어 상품을 아직 확인하지 못했습니다', 'warn'),
      h('div', { class: 'mini' }, '스토어 상품 확인이 끝나야 중복 여부를 알 수 있습니다. 설정에서 스마트스토어 연결 상태를 확인해 주세요.'),
      lastSync(duplicate),
    );
  }
  return holder;
}
