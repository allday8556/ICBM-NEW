// ADR-0042 S3b (§0 a4): the duplicate row of one registration unit, as the seller sees it.
//
// Everything here is the server's answer (unit.duplicate): the store's last COMPLETE catalog sync
// decides it, and this row only says it in plain Korean. Codes stay in data-* attributes.
//
// - 「중복 없음 · 14:02 동기화 기준」 when the snapshot shows no matching store listing;
// - 「스토어 다시 확인 중…」 while the automatic re-sync runs, with 「다시 보기」 to read it again;
// - 「이미 스토어에 있는 상품: …」 with the seller's choices when a store listing matches:
//   「같은 상품으로 연결」 (ADR-0042 S2a: the store listing becomes this product's listing, and
//   nothing is registered again), 「다른 상품」 for a listing only its name resembles (an S2a
//   decision; that listing no longer matches this product), or 「의도적으로 한 번 더 등록」, which
//   records the seller's reason and can be undone. A listing already linked to this product reads
//   so, with no link button;
// - 「스토어 상품 수 확인이 필요합니다」 until the seller confirms, in 통합DB › 「스토어 상품」, that
//   the store's per-status counts equal the seller center's (ADR-0042 §5.2): before that the
//   server never says 「중복 없음」.
// The last complete sync time is always shown beside any result (SCP-07).

import { ApiError, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { closeModal, openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';

const PREPARATIONS = '/api/v1/register/preparations';
const LINK = '/api/v1/operate/store-links/confirm';
const REJECT = '/api/v1/operate/store-links/reject';
const OPERATOR = 'operator';

const KEY_LABEL = {
  SELLER_CODE: '판매자코드가 같습니다',
  NORMALIZED_NAME: '상품명이 같거나 비슷합니다',
  GTIN: '바코드가 같습니다',
  LISTING_LINK: '이 상품과 연결된 스토어 상품입니다',
};
const LINK_ERROR = {
  STORE_LINK_GROUP_ALREADY_IN_STORE: '이 상품은 이미 이 스토어의 다른 상품과 연결되어 있습니다.',
  STORE_LINK_LISTING_LINKED_ELSEWHERE: '이 스토어 상품은 다른 상품에 연결되어 있습니다. 통합DB에서 먼저 연결을 해제해 주세요.',
  STORE_LINK_LISTING_OWNED: 'ICBM 등록이나 기존 상품 연결로 관리되는 스토어 상품이라 연결할 수 없습니다.',
  STORE_LINK_LISTING_UNKNOWN: '스토어 상품을 아직 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.',
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

// The one common product of this unit, when its Items name exactly one (ADR-0042 S2a).
function unitProduct(unit) {
  const groups = [...new Set((unit.items ?? []).map((item) => item.product_group_id).filter(Boolean))];
  return groups.length === 1 ? groups[0] : null;
}

function linkModal(unit, match, product, reload) {
  const submit = h('button', { type: 'button', class: 'btn blue', 'data-action': 'duplicate-link-confirm' }, '같은 상품으로 연결');
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      await sendJson('POST', LINK, {
        actor: OPERATOR,
        marketplace_key: unit.marketplace_key,
        marketplace_account_id: unit.marketplace_account_id,
        origin_product_no: match.marketplace_product_id,
        product_group_id: product,
      });
      closeModal();
      toast('중복 확인', '스토어 상품을 이 상품과 같은 상품으로 연결했습니다.');
      await reload();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast('중복 확인', LINK_ERROR[code] ?? '연결하지 못했습니다. 잠시 후 다시 시도해 주세요.');
      submit.disabled = false;
    }
  });
  openModal({
    eyebrow: '중복 확인',
    title: '같은 상품으로 연결',
    help: '스토어에 이미 있는 상품을 이 상품으로 관리합니다. 연결은 통합DB에서 언제든 해제할 수 있습니다.',
    narrow: true,
    body: h(
      'div',
      { class: 'stack' },
      h('div', { class: 'note' }, `스토어 상품 ‘${match.name || '이름 없는 상품'}’(상품번호 ${match.marketplace_product_id})을 이 상품과 같은 상품으로 연결합니다. 스토어에 새로 등록하지 않습니다.`),
    ),
    footer: [submit],
  });
}

function matchLine(match, link) {
  const status = SALE_LABEL[match.sale_status] ? ` · ${SALE_LABEL[match.sale_status]}` : '';
  return h(
    'li',
    { 'data-key-kind': match.key_kind, 'data-product-no': match.marketplace_product_id },
    h('b', {}, match.name || '이름 없는 상품'),
    h('span', { class: 'mini' }, ` 상품번호 ${match.marketplace_product_id}${status} · ${KEY_LABEL[match.key_kind] ?? '같은 상품일 수 있습니다'}`),
    link,
  );
}

async function rejectMatch(unit, match, product, reload, button) {
  button.disabled = true;
  try {
    await sendJson('POST', REJECT, {
      actor: OPERATOR,
      marketplace_key: unit.marketplace_key,
      marketplace_account_id: unit.marketplace_account_id,
      origin_product_no: match.marketplace_product_id,
      product_group_id: product,
    });
    toast('중복 확인', '다른 상품으로 기록했습니다. 통합DB에서 되돌릴 수 있습니다.');
    await reload();
  } catch (error) {
    const code = error instanceof ApiError ? error.error?.code : null;
    toast('중복 확인', LINK_ERROR[code] ?? '기록하지 못했습니다. 잠시 후 다시 시도해 주세요.');
    button.disabled = false;
  }
}

// 「다른 상품」: only for a listing its name alone resembles (a weak signal the seller decides).
function rejectButton(unit, match, editable, reload) {
  const product = unitProduct(unit);
  if (match.key_kind !== 'NORMALIZED_NAME' || !product) return null;
  const button = h('button', { type: 'button', class: 'btn', 'data-action': 'duplicate-reject', 'data-product-no': match.marketplace_product_id }, '다른 상품');
  button.disabled = !editable;
  button.addEventListener('click', () => rejectMatch(unit, match, product, reload, button));
  return button;
}

function linkButton(unit, match, editable, reload) {
  const product = unitProduct(unit);
  if (match.key_kind === 'LISTING_LINK' || !product) return null;
  const button = h('button', { type: 'button', class: 'btn', 'data-action': 'duplicate-link', 'data-product-no': match.marketplace_product_id }, '같은 상품으로 연결');
  button.disabled = !editable;
  button.addEventListener('click', () => linkModal(unit, match, product, reload));
  return button;
}

function matchBlock(unit, duplicate, reload) {
  const editable = Boolean(unit.authored?.preparation_id) && !unit.snapshot;
  const chosen = duplicate.override_ids.length > 0;
  const linkedOnly = duplicate.matches.length > 0 && duplicate.matches.every((match) => match.key_kind === 'LISTING_LINK');
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
  const why = linkedOnly
    ? '이 상품은 이미 이 스토어에서 판매 중이라 새로 등록하지 않습니다. 따로 한 번 더 판매하려면 이유를 적고 한 번 더 등록할 수 있습니다.'
    : '같은 상품을 두 번 등록하지 않도록 등록을 멈췄습니다. 같은 상품이면 스토어 상품과 연결하고, 다른 상품이라면 이유를 적고 한 번 더 등록할 수 있습니다.';
  return [
    h('div', {}, h('b', {}, '이미 스토어에 있는 상품:')),
    h('ul', { class: 'duplicate-matches' }, ...duplicate.matches.map((match) => matchLine(match, chosen ? null : [linkButton(unit, match, editable, reload), rejectButton(unit, match, editable, reload)]))),
    chosen ? null : h('div', { class: 'mini' }, why),
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
  } else if (state === 'COVERAGE_UNCONFIRMED') {
    holder.append(
      head('스토어 상품 수 확인이 필요합니다', 'warn'),
      h('div', { class: 'mini' }, '통합DB › 스토어 상품에서 상태별 상품 수가 판매자센터와 같은지 한 번 확인해 주세요. 확인 전에는 중복 없음으로 판단하지 않습니다.'),
      h(
        'div',
        { class: 'row' },
        lastSync(duplicate),
        h('button', { type: 'button', class: 'btn', 'data-action': 'open-store-listings', onclick: () => { window.location.hash = '#/db?view=store'; } }, '스토어 상품 열기'),
        recheck(reload),
      ),
    );
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
