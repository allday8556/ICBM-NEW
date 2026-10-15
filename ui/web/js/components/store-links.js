// ADR-0042 S2a (§0 b3): which common product each store listing is, as the seller sees it in 통합DB.
//
// Everything here is the server's answer (/api/v1/operate/store-links): the ListingLinks owner
// links, proposes and records; this component only says it in plain Korean and sends the
// seller's choice. Codes stay in data-* attributes.
//
// - 「N개 상품이 판매자코드·바코드로 자동 연결됨」 with 「확인」 and 「되돌리기」 per link;
// - 「같은 상품인가요?」 with 「같은 상품」 and 「다른 상품」 per proposal — when both products
//   could be kept, the seller picks which one stays;
// - 「다른 상품으로 표시한 상품」, each with 「되돌리기」;
// - in a product's detail, its store listings with 「연결 해제」 where the seller may end one.
// The last complete sync time is shown with the store data (SCP-07).

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { closeModal, openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';
import { syncTime } from './duplicate-row.js';

const LINKS = '/api/v1/operate/store-links';
const OPERATOR = 'operator';
const TITLE = '스토어 상품 연결';
// How many 「같은 상품인가요?」 rows are shown at once; the rest follow as these are answered.
const SHOWN = 20;

export const UNCONNECTED = '도매처 미연결';

export const KIND_LABEL = {
  IMPORTED: '스토어에서 가져옴',
  STRONG_KEY: '자동 연결',
  USER_CONFIRMED: '직접 연결',
  REGISTERED_BY_ICBM: 'ICBM 등록',
  ADOPTED: '기존 상품 연결',
};
const KEY_LABEL = { SELLER_CODE: '판매자코드가 같습니다', GTIN: '바코드가 같습니다' };
const SIGNAL_LABEL = { ...KEY_LABEL, NAME_SIMILAR: '상품명이 비슷합니다' };
const CONFLICT_LABEL = {
  volume: '용량',
  weight: '중량',
  count: '개수',
  pack: '팩 수',
  box: '박스 수',
  set: '세트 수',
  bottle: '병 수',
  can: '캔 수',
  tablet: '정 수',
  capsule: '캡슐 수',
  sachet: '포 수',
  sheet: '매 수',
  roll: '롤 수',
};
const SALE_LABEL = {
  SALE: '판매중',
  OUTOFSTOCK: '품절',
  SUSPENSION: '판매중지',
  CLOSE: '판매종료',
  PROHIBITION: '판매금지',
  WAIT: '판매대기',
};
const STATE_LABEL = { NOT_SEEN: '스토어에서 확인 안 됨', ENDED: '스토어에서 삭제됨' };
const ERROR_COPY = {
  STORE_LINK_GROUP_ALREADY_IN_STORE: '이 상품은 이미 이 스토어에 연결된 상품이 있어 연결하지 않았습니다.',
  STORE_LINK_LISTING_LINKED_ELSEWHERE: '이 스토어 상품은 다른 상품에 연결되어 있습니다. 먼저 연결을 해제해 주세요.',
  STORE_LINK_LISTING_OWNED: 'ICBM 등록이나 기존 상품 연결로 관리되는 스토어 상품이라 여기서 바꿀 수 없습니다.',
  STORE_LINK_SURVIVOR_INVALID: '그 상품은 남길 수 없습니다. 다른 쪽을 남겨 주세요.',
  STORE_LINK_OWN_PRODUCT: '이 스토어 상품의 기본 상품이라 연결을 해제할 수 없습니다.',
  STORE_LINK_NOT_ACTIVE: '이미 해제된 연결입니다.',
  STORE_LINK_GROUP_NOT_ACTIVE: '보관된 상품에는 연결할 수 없습니다.',
};

function errorText(error) {
  const code = error instanceof ApiError ? error.error?.code : null;
  return ERROR_COPY[code] ?? '처리하지 못했습니다. 잠시 후 다시 시도해 주세요.';
}

const won = (value) => (typeof value === 'number' ? `${value.toLocaleString('ko-KR')}원` : null);

function listingLine(listing) {
  const parts = [`상품번호 ${listing.origin_product_no}`];
  const state = STATE_LABEL[listing.listing_state] ?? SALE_LABEL[listing.sale_status];
  if (state) parts.push(state);
  const price = won(listing.sale_price);
  if (price) parts.push(price);
  return parts.join(' · ');
}

function listingName(listing) {
  return listing.name || '이름 없는 스토어 상품';
}

async function act(method, path, body, done, message, button) {
  if (button) button.disabled = true;
  try {
    const answer = await sendJson(method, path, { actor: OPERATOR, ...body });
    toast(TITLE, message);
    await done();
    return answer;
  } catch (error) {
    toast(TITLE, errorText(error));
    if (button) button.disabled = false;
    return null;
  }
}

// The store listings of the given products, keyed by product: { listings, supplier_connected }.
export async function fetchStoreLinks(groupIds) {
  const ids = [...new Set(groupIds)].slice(0, 50);
  if (!ids.length) return new Map();
  const params = new URLSearchParams(ids.map((id) => ['group_id', id]));
  try {
    const answer = await getJson(`${LINKS}/products?${params}`);
    return new Map(answer.products.map((entry) => [entry.product_group_id, entry]));
  } catch {
    // The list still shows every product; only the store side is missing.
    return new Map();
  }
}

// A product's store listings, for the 통합DB detail. 「연결 해제」 where the seller may end one.
export function storeListingsGroup(entry, onChanged) {
  const listings = entry?.listings ?? [];
  const holder = h('div', { class: 'detail-group store-links-detail', 'data-role': 'store-listings' }, h('h4', {}, '스토어 상품'));
  if (!listings.length) {
    holder.append(h('div', { class: 'mini' }, '연결된 스토어 상품이 없습니다.'));
    return holder;
  }
  for (const listing of listings) {
    const row = h(
      'div',
      { class: 'store-links-row', 'data-listing': listing.listing_id, 'data-link-kind': listing.kind, 'data-product-no': listing.origin_product_no },
      h('div', {}, h('b', {}, listingName(listing)), h('span', { class: 'mini' }, ` ${listingLine(listing)}`)),
      h('span', { class: 'chip' }, KIND_LABEL[listing.kind] ?? '연결됨'),
    );
    if (listing.unlinkable && listing.link_id) {
      const unlink = h('button', { type: 'button', class: 'btn', 'data-action': 'store-link-unlink' }, '연결 해제');
      unlink.addEventListener('click', () =>
        act('POST', `${LINKS}/${encodeURIComponent(listing.link_id)}/unlink`, {}, onChanged, '연결을 해제했습니다. 이 스토어 상품은 따로 관리됩니다.', unlink),
      );
      row.append(unlink);
    }
    holder.append(row);
  }
  if (!entry.supplier_connected) {
    holder.append(h('div', { class: 'mini', 'data-role': 'store-links-unconnected' }, `${UNCONNECTED} · 도매처를 연결하면 원가와 재고를 확인할 수 있습니다.`));
  }
  return holder;
}

function noticeBlock(notices, refresh) {
  const count = notices.length;
  const list = h('ul', { class: 'store-links-list', hidden: true, 'data-role': 'store-link-notices' });
  for (const notice of notices) {
    const undo = h('button', { type: 'button', class: 'btn', 'data-action': 'store-link-undo', 'data-link': notice.link_id }, '되돌리기');
    undo.addEventListener('click', () =>
      act('POST', `${LINKS}/${encodeURIComponent(notice.link_id)}/unlink`, { undo: true }, refresh, '자동 연결을 되돌렸습니다.', undo),
    );
    list.append(
      h(
        'li',
        { class: 'store-links-row', 'data-notice': notice.notice_id, 'data-key-kind': notice.key_kind },
        h(
          'div',
          {},
          h('b', {}, listingName(notice.listing)),
          h('span', { class: 'mini' }, ` ${listingLine(notice.listing)} → ${notice.product_name || '이름 없는 상품'} · ${KEY_LABEL[notice.key_kind] ?? '같은 상품입니다'}`),
        ),
        undo,
      ),
    );
  }
  const show = h('button', { type: 'button', class: 'btn', 'data-action': 'store-link-notices-show' }, '되돌리기');
  show.addEventListener('click', () => {
    list.hidden = !list.hidden;
  });
  const seen = h('button', { type: 'button', class: 'btn blue', 'data-action': 'store-link-acknowledge' }, '확인');
  seen.addEventListener('click', () =>
    act('POST', `${LINKS}/notices/acknowledge`, { notice_ids: notices.map((notice) => notice.notice_id) }, refresh, '자동 연결을 확인했습니다.', seen),
  );
  return h(
    'div',
    { class: 'note store-links-notice', 'data-role': 'store-link-notice', 'data-count': String(count) },
    h('div', { class: 'store-links-row' }, h('b', {}, `${count}개 상품이 판매자코드·바코드로 자동 연결됨`), h('span', { class: 'store-links-actions' }, seen, show)),
    list,
  );
}

function reasonText(proposal) {
  const parts = [SIGNAL_LABEL[proposal.signal] ?? '같은 상품일 수 있습니다'];
  if (proposal.ambiguous) parts.push('같은 판매자코드·바코드의 스토어 상품이 여러 개입니다');
  if (proposal.conflicts.length) parts.push(`${proposal.conflicts.map((c) => CONFLICT_LABEL[c] ?? '구성').join('·')}이 다릅니다`);
  return parts.join(' · ');
}

function productSide(proposal) {
  if (proposal.supplier_keys.length) return `도매처 ${proposal.supplier_keys.join(', ')}`;
  if (proposal.product_listings.length) return '다른 스토어의 상품';
  return UNCONNECTED;
}

function confirmProposal(proposal, refresh, button) {
  const body = { listing_id: proposal.listing.listing_id, product_group_id: proposal.product_group_id };
  if (proposal.survivors.length < 2) {
    act('POST', `${LINKS}/confirm`, body, refresh, '같은 상품으로 연결했습니다.', button);
    return;
  }
  // Both products could be kept: the seller picks the one that stays (ADR-0013 §4 MERGE).
  const choices = [
    [proposal.product_group_id, proposal.product_name || '선택한 상품'],
    [proposal.listing.product_group_id, listingName(proposal.listing)],
  ];
  const radios = choices.map(([groupId, label], index) => {
    const input = h('input', { type: 'radio', name: 'store-link-survivor', value: groupId, 'data-survivor': groupId });
    input.checked = index === 0;
    return h('label', { class: 'store-links-row' }, input, h('span', {}, `‘${label}’ 상품으로 합치기`));
  });
  const submit = h('button', { type: 'button', class: 'btn blue', 'data-action': 'store-link-merge-confirm' }, '같은 상품으로 합치기');
  submit.addEventListener('click', async () => {
    const chosen = radios.map((label) => label.querySelector('input')).find((input) => input.checked);
    const answer = await act('POST', `${LINKS}/confirm`, { ...body, survivor_group_id: chosen?.value }, refresh, '같은 상품으로 합쳤습니다.', submit);
    if (answer) closeModal();
  });
  openModal({
    eyebrow: TITLE,
    title: '같은 상품으로 합치기',
    help: '두 상품을 하나로 합칩니다. 남길 상품을 고르면 다른 쪽은 보관되고, 언제든 연결을 해제할 수 있습니다.',
    narrow: true,
    body: h('div', { class: 'store-links-choices' }, ...radios),
    footer: [submit],
  });
}

function proposalRow(proposal, refresh) {
  const same = h('button', { type: 'button', class: 'btn blue', 'data-action': 'store-link-same' }, '같은 상품');
  same.addEventListener('click', () => confirmProposal(proposal, refresh, same));
  const different = h('button', { type: 'button', class: 'btn', 'data-action': 'store-link-different' }, '다른 상품');
  different.addEventListener('click', () =>
    act(
      'POST',
      `${LINKS}/reject`,
      { listing_id: proposal.listing.listing_id, product_group_id: proposal.product_group_id },
      refresh,
      '다른 상품으로 표시했습니다.',
      different,
    ),
  );
  return h(
    'li',
    {
      class: 'store-links-proposal',
      'data-listing': proposal.listing.listing_id,
      'data-product': proposal.product_group_id,
      'data-signal': proposal.signal,
      'data-conflicts': proposal.conflicts.join(' ') || null,
    },
    h('div', {}, h('b', {}, listingName(proposal.listing)), h('span', { class: 'mini' }, ` 스토어 · ${listingLine(proposal.listing)}`)),
    h('div', {}, h('b', {}, proposal.product_name || '이름 없는 상품'), h('span', { class: 'mini' }, ` ${productSide(proposal)}`)),
    h('div', { class: 'mini', 'data-role': 'store-link-reason' }, reasonText(proposal)),
    h('div', { class: 'store-links-actions' }, same, different),
  );
}

function decisionsBlock(decisions, refresh) {
  const list = h('ul', { class: 'store-links-list' });
  for (const decision of decisions) {
    const undo = h('button', { type: 'button', class: 'btn', 'data-action': 'store-link-decision-undo', 'data-decision': decision.decision_id }, '되돌리기');
    undo.addEventListener('click', () =>
      act('POST', `${LINKS}/decisions/${encodeURIComponent(decision.decision_id)}/revoke`, {}, refresh, '다른 상품 표시를 되돌렸습니다.', undo),
    );
    list.append(
      h(
        'li',
        { class: 'store-links-row', 'data-decision': decision.decision_id },
        h('span', {}, `${decision.listing_name || '스토어 상품'} ≠ ${decision.product_name || '이름 없는 상품'}`),
        undo,
      ),
    );
  }
  return h('details', { class: 'store-links-decisions', 'data-role': 'store-link-decisions' }, h('summary', {}, `다른 상품으로 표시한 상품 ${decisions.length}개`), list);
}

// The panel above the 통합DB list. ``onChanged`` re-reads the list after a seller action.
export function storeLinksPanel({ onChanged } = {}) {
  const holder = h('section', { class: 'panel store-links', 'data-role': 'store-links', hidden: true });

  async function refresh() {
    await load();
    if (onChanged) await onChanged();
  }

  async function load() {
    let overview;
    try {
      overview = await getJson(LINKS);
    } catch {
      holder.hidden = true;
      return;
    }
    if (!overview.marketplace_account_id || (!overview.linked && !overview.notices.length && !overview.proposals.length)) {
      holder.hidden = true;
      holder.replaceChildren();
      return;
    }
    const time = syncTime(overview.as_of);
    holder.hidden = false;
    holder.dataset.linked = String(overview.linked);
    holder.dataset.unconnected = String(overview.unconnected);
    holder.replaceChildren(
      h(
        'div',
        { class: 'store-links-row' },
        h('h2', { class: 'panel-title' }, TITLE),
        h('span', { class: 'mini', 'data-role': 'store-links-summary' }, `스토어 상품 ${overview.linked.toLocaleString('ko-KR')}개 연결 · ${UNCONNECTED} ${overview.unconnected.toLocaleString('ko-KR')}개`),
        h('span', { class: 'mini', 'data-role': 'store-links-last-sync' }, time ? `마지막 동기화 ${time}` : '아직 동기화 기록이 없습니다'),
      ),
      overview.notices.length ? noticeBlock(overview.notices, refresh) : null,
      overview.proposals.length
        ? h(
            'div',
            { class: 'store-links-proposals', 'data-role': 'store-link-proposals' },
            h('b', {}, `확인 필요 · 같은 상품인가요? ${overview.proposals.length}건`),
            h('ul', { class: 'store-links-list' }, ...overview.proposals.slice(0, SHOWN).map((proposal) => proposalRow(proposal, refresh))),
            overview.proposals.length > SHOWN ? h('div', { class: 'mini' }, `나머지 ${overview.proposals.length - SHOWN}건은 위 항목을 정리하면 이어서 보입니다.`) : null,
          )
        : null,
      overview.decisions.length ? decisionsBlock(overview.decisions, refresh) : null,
    );
  }

  holder.reload = load;
  load();
  return holder;
}
