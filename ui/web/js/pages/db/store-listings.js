// 통합DB 「스토어 상품」 (ADR-0042 S1b, §0 a2 and b2): every listing of the connected store as the
// StoreCatalog owner last read it. Read-only towards the marketplace: the tab shows what the
// server reports and its one action, 지금 동기화, asks the server for one read-only sync.
//
// - 「마지막 동기화 <time>」 is always on screen, beside the store's listing count, also while a sync
//   runs; a running or failed sync is shown as such, never as an old success (SCP-07, §5.4).
// - The status filters and their counts are the server's: every listing is in exactly one filter,
//   so the counts add up to 전체. A listing the last sync did not list reads 「스토어에서 확인 안
//   됨」 until provider evidence ends it (종료) (SCP-08).
// - The filter, the search and the page live in the route, never in browser storage. The server
//   pages the list; only the shown page's details are read.
// - Every value is what the store's last successful detail read recorded. A value the catalog
//   does not keep (the discount, and anything not read) keeps its place and reads 데이터 없음.
//   Provider tokens appear only in data-* attributes, never as text.
// - 「판매자센터 상품 수와 같습니다」 (ADR-0042 §5.2, seller decides): the last complete sync's
//   per-status counts beside the seller-center hint. Until the seller confirms them once, and
//   again whenever a status class vanishes or the total jumps, a registration's duplicate check
//   never reads 「중복 없음」.

import { ApiError, getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { dotDateTime } from '../../core/format.js';
import { withHelp } from '../../core/help.js';
import { toast } from '../../core/toast.js';

const LISTINGS = '/api/v1/operate/store-catalog/listings';
const STATUS = '/api/v1/operate/store-catalog/status';
const SYNC = '/api/v1/operate/store-catalog/sync';
const COVERAGE = '/api/v1/operate/store-catalog/coverage';
const COVERAGE_CONFIRM = '/api/v1/operate/store-catalog/coverage/confirm';
const OPERATOR = 'operator';
const COVERAGE_KEYS = ['ON_SALE', 'OUT_OF_STOCK', 'SUSPENDED', 'SALE_CLOSED', 'OTHER_STATUS'];
const COVERAGE_REASON = {
  STATUS_CLASS_MISSING: '확인 때 있던 상태의 상품이 이번 동기화에 하나도 없습니다.',
  TOTAL_JUMPED: '확인 때보다 상품 수가 크게 바뀌었습니다.',
};
const PAGE_SIZE = 50;
// While a sync runs the status line re-reads its progress at this pace.
const POLL_MS = 3000;
const NO_DATA = '데이터 없음';

// The server's filters, in the order the seller center lists the sale statuses.
export const STORE_FILTERS = [
  ['ALL', '전체'],
  ['ON_SALE', '판매중'],
  ['OUT_OF_STOCK', '품절'],
  ['SUSPENDED', '판매중지'],
  ['SALE_CLOSED', '판매종료'],
  ['OTHER_STATUS', '그 밖의 상태'],
  ['ENDED', '종료(삭제됨)'],
  ['NOT_SEEN', '스토어에서 확인 안 됨'],
  ['NOT_READ', '확인 전'],
];
const FILTER_LABEL = Object.fromEntries(STORE_FILTERS);
const FILTER_TONE = { ON_SALE: 'good', OUT_OF_STOCK: 'warn', SUSPENDED: 'warn', ENDED: 'bad', NOT_SEEN: 'warn' };
const SALE_LABEL = {
  SALE: '판매중',
  OUTOFSTOCK: '품절',
  SUSPENSION: '판매중지',
  CLOSE: '판매종료',
  WAIT: '판매대기',
  UNADMISSION: '승인대기',
  REJECTION: '승인거부',
  PROHIBITION: '판매금지',
  DELETE: '삭제',
};
const DISPLAY_LABEL = { SUSPENSION: '전시중지', WAIT: '전시대기' };
const ENDED_LABEL = { STATUS_DELETE: '판매 상태가 삭제로 확인됨', HTTP_404: '스토어에 상품이 없다고 확인됨' };
const FEE_LABEL = {
  FREE: '무료배송',
  PAID: '유료',
  CONDITIONAL_FREE: '조건부 무료',
  CHARGE_BY_QUANTITY: '수량별 부과',
  UNIT_QUANTITY_PAID: '수량 단위별 부과',
  RANGE_QUANTITY_PAID: '구간별 부과',
};
const PAY_LABEL = { PREPAID: '선결제', COLLECT: '착불', COLLECT_OR_PREPAID: '착불 또는 선결제' };
const OUTCOME_LABEL = {
  INCOMPLETE: '일부만 확인됨 (결론 없음)',
  UNAVAILABLE: '지금은 확인할 수 없음',
  ERROR: '확인 실패',
  INTERRUPTED: '중단됨',
};
const RUN_ERROR_LABEL = {
  SMARTSTORE_SESSION_UNAVAILABLE: '스마트스토어 연결 확인 필요',
  'GW.RATE_LIMIT': '호출 한도로 잠시 멈춤',
  'GW.QUOTA_LIMIT': '호출 한도로 잠시 멈춤',
  SMARTSTORE_CATALOG_TOO_LARGE: '상품이 100,000개를 넘어 가져올 수 없음',
  STORE_CATALOG_DETAIL_READ_FAILED: '일부 상품을 읽지 못함',
  STORE_CATALOG_DETAILS_STALE: '하루 넘게 다시 읽지 못한 상품이 있음',
  STORE_CATALOG_LISTING_OUTSIDE_ENUMERATION: '목록에 없는 판매 상품이 있음',
  STORE_CATALOG_SYNC_INTERRUPTED: '앱이 중간에 종료됨',
};
const REQUEST_COPY = {
  STORE_CATALOG_FILTER_INVALID: '알 수 없는 상태 필터입니다.',
  STORE_CATALOG_QUERY_INVALID: '검색어는 100자까지 입력할 수 있습니다.',
  STORE_CATALOG_PAGE_INVALID: '목록 위치가 올바르지 않습니다.',
};
const COLUMNS = ['상품명', '상태', '판매가', '할인', '재고', '옵션', '배송', '카테고리', '판매자코드', '상세 확인'];
const HELP =
  '연결된 스마트스토어의 모든 상품을 마지막 동기화 기준으로 보여줍니다. 값은 상품마다 마지막으로 상세를 읽은 시각(상세 확인)의 값입니다. 판매중·품절·판매중지·판매종료는 판매자센터의 판매 상태와 같고, 그 밖의 상태는 판매대기·승인대기·판매금지 등입니다. 마지막 동기화 목록에 없던 상품은 「스토어에서 확인 안 됨」으로 남고, 스토어가 삭제를 확인해 준 상품만 「종료(삭제됨)」가 됩니다. 아직 상세를 읽지 못한 상품은 「확인 전」입니다. 할인은 아직 가져오지 않는 값이라 데이터 없음으로 표시됩니다.';

function count(value) {
  return typeof value === 'number' ? value.toLocaleString('ko-KR') : '—';
}

function won(value) {
  return `${value.toLocaleString('ko-KR')}원`;
}

function noDataCell(label) {
  return h('td', { class: 'no-data', 'data-no-data': label }, NO_DATA);
}

function errorCode(error) {
  return error instanceof ApiError ? error.error?.code ?? null : null;
}

// ---------------------------------------------------------------- the status line (SCP-07)

function progressText(running) {
  if (!running) return '';
  if (running.details_target > 0) {
    return `스토어 상품 가져오는 중 ${count(running.details_done + running.details_failed)} / ${count(running.details_target)}`;
  }
  return '스토어 상품 확인 중…';
}

function failedRun(status) {
  const last = status.last_run;
  if (!last || last.state === 'RUNNING' || last.outcome === 'COMPLETE') return '';
  const reason = RUN_ERROR_LABEL[last.error_code] ?? '';
  return [`최근 동기화 ${dotDateTime(last.as_of)}`, OUTCOME_LABEL[last.outcome] ?? '완료되지 않음', reason].filter(Boolean).join(' · ');
}

function statusBar(onFinished) {
  const total = h('b', { 'data-role': 'store-listing-total' });
  const lastSync = h('span', { 'data-role': 'store-last-sync' });
  const progress = h('div', { class: 'mini', 'data-role': 'store-sync-progress', hidden: true });
  const failure = h('div', { class: 'mini', 'data-role': 'store-sync-failure', hidden: true });
  const button = h('button', { type: 'button', class: 'btn blue', 'data-action': 'sync-store-catalog' }, '지금 동기화');
  const bar = h(
    'section',
    { class: 'panel store-sync-bar', 'data-role': 'store-sync-bar' },
    h('div', { class: 'store-sync-line' }, h('div', { class: 'store-sync-text' }, total, h('span', { class: 'store-sync-dot', 'aria-hidden': 'true' }, ' · '), lastSync), button),
    progress,
    failure,
  );
  let timer = null;
  let wasRunning = false;

  const render = (status) => {
    const complete = status.last_complete;
    total.textContent = complete ? `스토어 상품 ${count(status.listing_count)}개` : '스토어 상품 아직 확인 안 됨';
    // SCP-07: the last complete sync stays on screen whatever runs or failed since.
    lastSync.textContent = complete ? `마지막 동기화 ${dotDateTime(complete.as_of)}` : '마지막 동기화 없음';
    bar.dataset.state = status.running ? 'running' : complete ? 'complete' : 'none';
    progress.textContent = progressText(status.running);
    progress.hidden = !status.running;
    const failed = failedRun(status);
    failure.textContent = failed;
    failure.hidden = !failed;
    if (status.last_run?.error_code) failure.dataset.reason = status.last_run.error_code;
    button.disabled = Boolean(status.running);
  };

  const poll = async () => {
    clearTimeout(timer);
    if (!bar.isConnected) return;
    try {
      const found = await getJson(STATUS);
      if (!bar.isConnected) return;
      render(found.status);
      if (found.status.running) {
        wasRunning = true;
        timer = setTimeout(poll, POLL_MS);
      } else if (wasRunning) {
        onFinished(); // the sync ended: read the list and the counts again
      }
    } catch {
      progress.textContent = '동기화 상태를 읽지 못했습니다.';
      progress.hidden = false;
    }
  };

  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const answer = await sendJson('POST', SYNC, {});
      render(answer.status);
      toast('스토어 상품', '동기화를 시작했습니다.');
    } catch (error) {
      toast('스토어 상품', errorCode(error) === 'STORE_CATALOG_STORE_NOT_CONNECTED' ? '스마트스토어를 먼저 연결하세요.' : '동기화를 시작하지 못했습니다.');
    }
    wasRunning = true;
    timer = setTimeout(poll, 500);
  });

  return {
    element: bar,
    show(status) {
      render(status);
      if (status.running) {
        wasRunning = true;
        timer = setTimeout(poll, POLL_MS);
      }
    },
  };
}

function notConnected(ctx) {
  return h(
    'section',
    { class: 'panel store-sync-bar', 'data-role': 'store-not-connected' },
    h('div', { class: 'store-sync-line' }, h('div', { class: 'store-sync-text' }, h('b', {}, '스마트스토어가 연결되어 있지 않습니다'), h('span', { 'data-role': 'store-last-sync' }, ' · 마지막 동기화 없음'))),
    h('div', { class: 'mini' }, '설정 › 스마트스토어 › API 관리에서 스토어를 연결하면 스토어 상품을 자동으로 가져옵니다.'),
    h(
      'div',
      { class: 'store-actions' },
      h('button', { type: 'button', class: 'btn blue', 'data-action': 'open-store-settings', onclick: () => ctx.navigate('settings', { tab: 'smartstore', sub: 'api' }) }, 'API 관리로 이동'),
      h('button', { type: 'button', class: 'btn', 'data-action': 'sync-store-catalog', disabled: true }, '지금 동기화'),
    ),
  );
}

// ---------------------------------------------------------------- coverage (ADR-0042 §5.2)

function coveragePanel(onConfirmed) {
  const panel = h('section', { class: 'panel store-coverage', 'data-role': 'store-coverage', hidden: true });
  const render = (view) => {
    const current = view.current;
    if (!current) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    panel.dataset.state = view.state;
    if (view.reason) panel.dataset.reason = view.reason;
    const counts = COVERAGE_KEYS.map((key) =>
      h('span', { class: 'chip', 'data-filter': key }, `${FILTER_LABEL[key]} ${count(current.counts[key] ?? 0)}`),
    );
    const confirmed = view.state === 'CONFIRMED';
    const title = confirmed
      ? `판매자센터 상품 수 확인됨 · ${dotDateTime(view.confirmed.confirmed_at)}`
      : view.state === 'INVALIDATED'
        ? '판매자센터 상품 수를 다시 확인해 주세요'
        : '판매자센터 상품 수와 같은지 확인해 주세요';
    const button = h('button', { type: 'button', class: confirmed ? 'btn' : 'btn blue', 'data-action': 'confirm-store-coverage' }, '판매자센터 상품 수와 같습니다');
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        render(await sendJson('POST', COVERAGE_CONFIRM, { run_id: current.run_id, actor: OPERATOR }));
        toast('스토어 상품', '판매자센터 상품 수와 같다고 기록했습니다.');
        onConfirmed();
      } catch (error) {
        toast('스토어 상품', errorCode(error) === 'STORE_CATALOG_COVERAGE_RUN_MOVED' ? '새 동기화가 끝났습니다. 바뀐 상품 수를 보고 다시 확인해 주세요.' : '기록하지 못했습니다.');
        button.disabled = false;
      }
    });
    panel.replaceChildren(
      h('div', { class: 'store-sync-line' }, h('b', {}, title), button),
      h('div', { class: 'store-filters' }, h('span', { class: 'mini' }, `스토어 상품 ${count(current.total)}개 · 마지막 동기화 ${dotDateTime(current.as_of)}`), ...counts),
      h('div', { class: 'mini' }, '판매자센터 › 상품관리 › 상품 조회/수정의 상태별 상품 수와 비교해 주세요. 같다고 확인한 뒤에야 등록할 때 「중복 없음」으로 판단합니다.'),
      view.reason ? h('div', { class: 'mini', 'data-reason': view.reason }, COVERAGE_REASON[view.reason] ?? '') : null,
    );
  };
  const load = async () => {
    try {
      render(await getJson(COVERAGE));
    } catch {
      panel.hidden = true;
    }
  };
  load();
  return panel;
}

// ---------------------------------------------------------------- one listing row

function optionsCell(fields) {
  const options = fields?.options;
  if (!options || !Array.isArray(options.combinations)) return noDataCell('옵션');
  const groups = (options.group_names ?? []).filter((name) => typeof name === 'string' && name);
  const combinations = options.combinations;
  if (!groups.length && !combinations.length) return h('td', { 'data-options': '0' }, '옵션 없음');
  const usable = combinations.filter((c) => c.usable === true).length;
  const known = combinations.some((c) => typeof c.usable === 'boolean');
  return h(
    'td',
    { 'data-options': String(combinations.length) },
    groups.length ? groups.join(' · ') : '옵션',
    h('span', { class: 'mini store-cell-line' }, `조합 ${count(combinations.length)}개${known ? ` · 판매 가능 ${count(usable)}개` : ''}`),
  );
}

function shippingCell(fields) {
  const shipping = fields?.shipping;
  if (!shipping) return noDataCell('배송');
  const fee = shipping.delivery_fee_type;
  const base = typeof shipping.base_fee === 'number' ? shipping.base_fee : null;
  const free = typeof shipping.free_conditional_amount === 'number' ? shipping.free_conditional_amount : null;
  let text = null;
  if (fee === 'FREE') text = '무료배송';
  else if (fee === 'CONDITIONAL_FREE') text = `${base !== null ? won(base) : '배송비'}${free !== null ? ` (${won(free)} 이상 무료)` : ' (조건부 무료)'}`;
  else if (fee && FEE_LABEL[fee]) text = base !== null ? `${FEE_LABEL[fee]} ${won(base)}` : FEE_LABEL[fee];
  else if (fee) text = base !== null ? `배송비 ${won(base)}` : '배송비 확인 필요';
  else if (base !== null) text = `배송비 ${won(base)}`;
  if (text === null) return noDataCell('배송');
  const pay = PAY_LABEL[shipping.delivery_fee_pay_type];
  return h(
    'td',
    { 'data-fee-type': fee ?? '', 'data-pay-type': shipping.delivery_fee_pay_type ?? '' },
    text,
    pay ? h('span', { class: 'mini store-cell-line' }, pay) : null,
  );
}

function statusCell(listing) {
  const key = listing.filter;
  const label = key === 'OTHER_STATUS' ? SALE_LABEL[listing.sale_status] ?? '그 밖의 상태' : FILTER_LABEL[key] ?? '확인 전';
  const tone = FILTER_TONE[key];
  const notes = [];
  if (key === 'ENDED' && ENDED_LABEL[listing.ended_evidence]) notes.push(ENDED_LABEL[listing.ended_evidence]);
  if (key === 'NOT_SEEN' && SALE_LABEL[listing.sale_status]) notes.push(`마지막으로 읽은 상태: ${SALE_LABEL[listing.sale_status]}`);
  if (DISPLAY_LABEL[listing.display_status] && key !== 'ENDED') notes.push(DISPLAY_LABEL[listing.display_status]);
  if (listing.last_result === 'READ_FAILED') notes.push(listing.details_read_at ? '최근 읽기 실패 · 이전 값 표시' : '상세를 읽지 못함');
  return h(
    'td',
    { 'data-sale-status': listing.sale_status ?? '', 'data-display-status': listing.display_status ?? '', 'data-ended-evidence': listing.ended_evidence ?? '' },
    h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-role': 'store-listing-status' }, label),
    ...notes.map((note) => h('span', { class: 'mini store-cell-line' }, note)),
  );
}

function listingRow(listing) {
  const fields = listing.fields;
  const name = typeof fields?.name === 'string' && fields.name ? fields.name : null;
  const code = listing.seller_management_code ?? listing.listed_seller_code;
  const number = listing.channel_product_no ? `상품번호 ${listing.channel_product_no}` : `원상품번호 ${listing.origin_product_no}`;
  return h(
    'tr',
    {
      'data-listing': listing.listing_id,
      'data-origin-product': listing.origin_product_no,
      'data-channel-product': listing.channel_product_no ?? '',
      'data-filter': listing.filter,
      'data-state': listing.state,
    },
    h(
      'td',
      { class: 'store-name-cell' },
      name ? h('span', { class: 'db-name' }, name) : h('span', { class: 'no-data', 'data-no-data': '상품명' }, NO_DATA),
      h('span', { class: 'mini store-cell-line' }, number),
    ),
    // The status sits beside the name, so a narrow screen shows both before the table scrolls.
    statusCell(listing),
    typeof listing.sale_price === 'number' ? h('td', {}, won(listing.sale_price)) : noDataCell('판매가'),
    // The immediate discount is not among the fields the catalog keeps yet (§5.3, S1a).
    noDataCell('할인'),
    typeof listing.stock_quantity === 'number' ? h('td', {}, `${count(listing.stock_quantity)}개`) : noDataCell('재고'),
    optionsCell(fields),
    shippingCell(fields),
    listing.category_name
      ? h('td', { 'data-category-id': fields?.category?.leaf_category_id ?? '' }, listing.category_name)
      : h('td', { class: 'no-data', 'data-no-data': '카테고리', 'data-category-id': fields?.category?.leaf_category_id ?? '' }, NO_DATA),
    code ? h('td', { class: 'store-code-cell' }, code) : noDataCell('판매자코드'),
    listing.details_read_at ? h('td', { 'data-role': 'details-read-at' }, dotDateTime(listing.details_read_at)) : noDataCell('상세 확인'),
  );
}

function emptyRow(page) {
  let text = '스토어에 상품이 없습니다.';
  if (page.query) text = '검색 결과가 없습니다.';
  else if (page.filter !== 'ALL') text = `「${FILTER_LABEL[page.filter]}」 상품이 없습니다.`;
  else if (!page.status.last_complete) text = '아직 가져온 스토어 상품이 없습니다. 첫 동기화가 끝나면 이곳에 표시됩니다.';
  return h('tr', {}, h('td', { class: 'table-empty', colspan: String(COLUMNS.length) }, text));
}

// ---------------------------------------------------------------- the tab

function routeState(params) {
  const filter = STORE_FILTERS.some(([key]) => key === params.get('filter')) ? params.get('filter') : 'ALL';
  const query = (params.get('q') ?? '').trim();
  const pageNo = Math.max(1, Number.parseInt(params.get('page') ?? '1', 10) || 1);
  return { filter, query, pageNo };
}

export async function storeListingsView(ctx) {
  const route = routeState(ctx.params);
  const go = (changes) => {
    const next = { ...route, ...changes };
    ctx.navigate('db', {
      view: 'store',
      filter: next.filter === 'ALL' ? null : next.filter,
      q: next.query || null,
      page: next.pageNo > 1 ? String(next.pageNo) : null,
    });
  };
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String((route.pageNo - 1) * PAGE_SIZE) });
  if (route.filter !== 'ALL') params.set('filter', route.filter);
  if (route.query) params.set('q', route.query);
  let page;
  try {
    page = await getJson(`${LISTINGS}?${params}`);
  } catch (error) {
    const code = errorCode(error);
    return h(
      'section',
      { class: 'panel store-listings', 'data-role': 'store-listings' },
      h('div', { class: 'note', 'data-reason': code ?? '' }, REQUEST_COPY[code] ?? '스토어 상품을 불러오지 못했습니다.'),
      h('button', { type: 'button', class: 'btn', onclick: () => go({ filter: 'ALL', query: '', pageNo: 1 }) }, '처음부터 다시 보기'),
    );
  }

  const status = page.status;
  const head = status.marketplace_account_id ? statusBar(() => go({})) : null;
  if (head) head.show(status);

  const search = h('input', {
    type: 'search',
    class: 'field search',
    name: 'q',
    maxlength: '100',
    value: route.query,
    placeholder: '상품명이나 판매자코드로 검색하세요...',
    'aria-label': '스토어 상품 검색',
    autocomplete: 'off',
  });
  const toolbar = h(
    'form',
    { class: 'db-toolbar panel store-toolbar', role: 'search', 'data-role': 'store-search' },
    search,
    h('button', { type: 'submit', class: 'btn blue' }, '검색'),
    route.query ? h('button', { type: 'button', class: 'btn', 'data-action': 'clear-store-search', onclick: () => go({ query: '', pageNo: 1 }) }, '검색 지우기') : null,
  );
  toolbar.addEventListener('submit', (event) => {
    event.preventDefault();
    go({ query: search.value.trim(), pageNo: 1 });
  });

  const filters = h(
    'div',
    { class: 'store-filters', role: 'tablist', 'aria-label': '스토어 상품 상태', 'data-role': 'store-filters' },
    STORE_FILTERS.map(([key, label]) =>
      h(
        'button',
        {
          type: 'button',
          role: 'tab',
          class: key === page.filter ? 'store-filter active' : 'store-filter',
          'data-filter': key,
          'aria-selected': String(key === page.filter),
          onclick: () => go({ filter: key, pageNo: 1 }),
        },
        label,
        h('strong', { 'data-role': 'filter-count' }, count(page.counts[key] ?? 0)),
      ),
    ),
  );

  const first = page.matching ? page.offset + 1 : 0;
  const last = page.offset + page.listings.length;
  const pager = h(
    'div',
    { class: 'db-pager' },
    h('span', { class: 'mini', 'data-role': 'store-page-range' }, `${count(first)}–${count(last)} / ${count(page.matching)}개`),
    h('button', { type: 'button', class: 'btn', 'data-action': 'first-page', disabled: route.pageNo === 1, onclick: () => go({ pageNo: 1 }) }, '처음'),
    h('button', { type: 'button', class: 'btn', 'data-action': 'previous-page', disabled: route.pageNo === 1, onclick: () => go({ pageNo: route.pageNo - 1 }) }, '이전'),
    h('button', { type: 'button', class: 'btn', 'data-action': 'next-page', disabled: last >= page.matching, onclick: () => go({ pageNo: route.pageNo + 1 }) }, '다음'),
  );

  const list = h(
    'section',
    { class: 'panel store-listings', 'data-role': 'store-listings', 'data-filter': page.filter },
    h(
      'div',
      { class: 'db-list-head' },
      withHelp(h('h2', { class: 'panel-title' }, '스토어 상품'), HELP),
      h('span', { class: 'mini', 'data-role': 'matching-total' }, `${count(page.matching)}개`),
    ),
    filters,
    h(
      'div',
      { class: 'store-table-scroll' },
      h(
        'table',
        { class: 'table store-table' },
        h('thead', {}, h('tr', {}, ...COLUMNS.map((label) => h('th', {}, label)))),
        h('tbody', {}, ...(page.listings.length ? page.listings.map(listingRow) : [emptyRow(page)])),
      ),
    ),
    pager,
  );

  return h(
    'div',
    { class: 'store-workspace', 'data-role': 'store-workspace' },
    head ? head.element : notConnected(ctx),
    head ? coveragePanel(() => {}) : null,
    toolbar,
    list,
  );
}
