// M6-A (ADR-0023 §3, §8): each SmartStore registration's operated state, read back from the
// marketplace. Read-only towards the marketplace: the panel shows what the server observed and the
// drift it sees, and its one action — 지금 동기화 — runs the same read-only pass the periodic job
// runs. Nothing here repairs a listing.
// M6-E (ADR-0024): listings ICBM did not create but adopted by the owner-declared seller-code
// convention (KM: KM + KM상품번호) are listed beside ICBM's own, marked 가져온 상품. KM 상품
// 가져오기 runs one read-only adoption pass; nothing is attached by name.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { dotDateTime } from '../core/format.js';
import { toast } from '../core/toast.js';

const LISTINGS = '/api/v1/operate/listings';
const SYNC = '/api/v1/operate/listings/sync';
const ADOPT = '/api/v1/operate/adoptions/run';
const ADOPTION_LABEL = {
  ADOPTED: '가져옴',
  ALREADY_ADOPTED: '이미 가져옴',
  REGISTERED_BY_ICBM: 'ICBM 등록 상품',
  NOT_SINGLE_ITEM: '옵션 상품(제외)',
  NOT_FOUND: '네이버에 없음',
  AMBIGUOUS: '같은 코드 여러 개',
  MISMATCH: '코드 불일치',
  DELETED: '삭제된 상품',
  LISTING_TAKEN: '다른 상품에 연결됨',
  RATE_LIMITED: '호출 한도로 중단',
  NOT_REACHED: '중단으로 미확인',
  UNAVAILABLE: '확인 불가',
  FAILED: '확인 실패',
};

const STATUS_LABEL = {
  SALE: '판매중',
  OUTOFSTOCK: '품절',
  SUSPENSION: '판매중지',
  CLOSE: '판매종료',
  PROHIBITION: '판매금지',
  WAIT: '판매대기',
  DELETE: '삭제됨',
};
const DRIFT_LABEL = {
  PRICE_DIFFERS_FROM_SNAPSHOT: '가격이 등록 때와 다름',
  NOT_ON_SALE: '판매중 아님',
  STOCK_ZERO: '재고 0',
  SELLER_CODE_MISMATCH: '판매자코드 불일치',
};
const OUTCOME_LABEL = {
  COMPLETED: '완료',
  COMPLETED_WITH_FAILURES: '일부 실패',
  SESSION_UNAVAILABLE: '스마트스토어 연결 필요 (설정 › API 관리 › 계정 확인)',
  RATE_LIMITED: '요청 한도로 중단',
  INTERRUPTED: '중단됨',
};

function won(value) {
  return typeof value === 'number' ? `${value.toLocaleString('ko-KR')}원` : '—';
}

function stateCell(listing) {
  if (listing.deleted_by_icbm) return h('span', { class: 'chip', 'data-listing-state': 'DELETED_BY_ICBM' }, 'ICBM에서 삭제');
  if (listing.lifecycle_state === 'EXTERNALLY_REMOVED') {
    return h('span', { class: 'chip bad', 'data-listing-state': 'EXTERNALLY_REMOVED' }, '마켓에서 사라짐');
  }
  if (!listing.last_result) return h('span', { class: 'chip', 'data-listing-state': 'NOT_SYNCED' }, '아직 확인 전');
  if (listing.last_result === 'READ_FAILED') {
    return h('span', { class: 'chip warn', 'data-listing-state': 'READ_FAILED', title: listing.error_code ?? '' }, '확인 실패');
  }
  const label = STATUS_LABEL[listing.sale_status] ?? listing.sale_status ?? '—';
  const tone = listing.sale_status === 'SALE' && listing.display_status === 'ON' ? 'good' : 'warn';
  return h('span', { class: `chip ${tone}`, 'data-listing-state': listing.sale_status ?? 'UNKNOWN' }, label);
}

function row(listing) {
  const adopted = listing.kind === 'ADOPTED';
  return h(
    'tr',
    adopted ? { 'data-adoption': listing.adoption_id, 'data-listing-kind': 'ADOPTED' } : { 'data-registration': listing.registration_id },
    h(
      'td',
      {},
      listing.product_name ?? (adopted ? listing.seller_product_code : '—'),
      adopted ? h('span', { class: 'chip', 'data-role': 'adopted-mark' }, '가져온 상품') : null,
    ),
    h('td', {}, listing.marketplace_product_id),
    h('td', {}, stateCell(listing)),
    h('td', {}, won(listing.sale_price), adopted ? null : h('span', { class: 'mini' }, `등록 ${won(listing.snapshot_sale_price)}`)),
    h('td', {}, listing.stock_quantity ?? '—'),
    h('td', {}, listing.observed_at ? dotDateTime(listing.observed_at) : '—'),
    h(
      'td',
      {},
      ...(listing.drift ?? []).map((code) => h('span', { class: 'chip warn', 'data-drift': code }, DRIFT_LABEL[code] ?? code)),
    ),
  );
}

function body(found, reload) {
  const last = found.last_run;
  const button = h('button', { type: 'button', class: 'btn blue', 'data-action': 'sync-listings' }, '지금 동기화');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const run = await sendJson('POST', SYNC, {});
      toast('판매 상태', `${OUTCOME_LABEL[run.outcome] ?? run.outcome} · 확인 ${run.observed}/${run.targets}`);
      await reload();
    } catch (error) {
      const code = error instanceof ApiError ? error.error?.code : null;
      toast('판매 상태', code === 'OPERATE_LISTING_SYNC_RUNNING' ? '이미 동기화가 진행 중입니다.' : String(error?.message ?? error));
      button.disabled = false;
    }
  });
  // ADR-0030 §11 S2: one button per supplier with an owner-declared seller-code convention,
  // as the server lists them; the page never names a supplier itself.
  const adopters = (found.adoptable_suppliers ?? []).map(({ supplier_key: key, display_name: name }) => {
    const title = `${name} 상품 가져오기`;
    const adopt = h('button', { type: 'button', class: 'btn', 'data-action': 'adopt-listings', 'data-supplier': key }, title);
    adopt.addEventListener('click', async () => {
      adopt.disabled = true;
      try {
        const run = await sendJson('POST', ADOPT, { supplier_key: key });
        const tally = {};
        for (const outcome of run.outcomes) tally[outcome.outcome] = (tally[outcome.outcome] ?? 0) + 1;
        const summary = Object.entries(tally)
          .map(([code, count]) => `${ADOPTION_LABEL[code] ?? code} ${count}`)
          .join(' · ');
        toast(title, summary || '대상 상품 없음');
        await reload();
      } catch (error) {
        const code = error instanceof ApiError ? error.error?.code : null;
        toast(title, code === 'OPERATE_ADOPTION_RUNNING' ? '이미 가져오는 중입니다.' : String(error?.message ?? error));
      } finally {
        adopt.disabled = false;
      }
    });
    return adopt;
  });
  const minutes = Math.round((found.interval_s ?? 0) / 60);
  return [
    h(
      'div',
      { class: 'supplier-head-row' },
      h('span', { class: 'mini', 'data-sync-interval': String(found.interval_s) }, minutes > 0 ? `자동 확인 ${minutes}분마다` : '자동 확인 꺼짐'),
      last
        ? h(
            'span',
            { class: 'mini', 'data-sync-outcome': last.outcome ?? last.state },
            `마지막 확인 ${dotDateTime(last.started_at)} · ${OUTCOME_LABEL[last.outcome] ?? last.outcome ?? '진행 중'}`,
          )
        : h('span', { class: 'mini', 'data-sync-outcome': 'NONE' }, '아직 확인한 적 없음'),
      ...adopters,
      button,
    ),
    found.listings.length
      ? h(
          'table',
          { class: 'table' },
          h(
            'thead',
            {},
            h('tr', {}, ...['상품명', '상품번호', '판매 상태', '현재가', '재고', '마지막 확인', '차이'].map((label) => h('th', {}, label))),
          ),
          h('tbody', {}, ...found.listings.map(row)),
        )
      : h('div', { class: 'note' }, '네이버에 등록된 상품이 아직 없습니다.'),
  ];
}

export function listingSyncPanel() {
  const panel = h(
    'section',
    { class: 'panel listing-sync', 'data-role': 'listing-sync' },
    h('h2', { class: 'panel-title' }, '판매 상태 (네이버)'),
  );
  const content = h('div', { 'data-listing-sync': 'LOADING' }, h('span', { class: 'mini' }, '불러오는 중…'));
  panel.append(content);
  const reload = async () => {
    try {
      const found = await getJson(LISTINGS);
      content.setAttribute('data-listing-sync', 'READY');
      content.replaceChildren(...body(found, reload));
    } catch (error) {
      content.setAttribute('data-listing-sync', 'UNAVAILABLE');
      content.replaceChildren(h('span', { class: 'chip warn' }, `판매 상태를 불러오지 못했습니다 · ${String(error?.message ?? error)}`));
    }
  };
  reload();
  return panel;
}
