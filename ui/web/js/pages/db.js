// 통합DB: the Product DB screen (Gate 1 G1-C, ADR-0015 §5).
//
// Everything here is server read state. The page lists ACTIVE canonical Products page by page,
// searches on the server, shows one Product's detail, and lets the operator choose Items of that
// one Product as a future registration target, which the server revalidates. It decides no price,
// readiness, compliance or marketplace verdict and creates nothing: no Draft, no PricingSnapshot
// and no stored selection. A reload starts again from server state with nothing chosen.
//
// Product context isolation. Every detail and every selection is keyed by product_group_id.
// Choosing another Product clears the previous detail and Item selection at once, and a response
// that arrives for any Product other than the one selected now — or for an older request — is
// discarded, never rendered. Nothing is cached across Products: each detail is read fresh.
//
// Gate 1 G1-D adds one command: 등록 초안 만들기 sends the chosen Product, its membership
// revision, the chosen Items, a target the server lists and a listing shape. The server
// revalidates, prices through M4, pins and creates the Draft — or nothing. The page computes no
// price and sends no price, policy revision or snapshot.
//
// It reuses the v29 panel/table/chip/kv system and the prototype's DB layout (list beside a
// detail panel). Explanations live behind the help icon, never as permanent subtitles.

import { getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { dotDate, dotDateTime } from '../core/format.js';
import { errorCode, errorText } from '../core/errors.js';
import { withHelp } from '../core/help.js';
import { markInert } from '../core/inert.js';
import { marketplaceLabel } from '../core/platform.js';
import { supplierName, supplierNames } from '../core/suppliers.js';
import { toast } from '../core/toast.js';
import { aiNamePanel } from '../components/ai-name.js';
import { aiCategoryPanel } from '../components/ai-category.js';
import { aiTagsPanel } from '../components/ai-tags.js';
import { pageHead } from '../components/page-head.js';
import { emptyState, errorState, unsupportedState } from '../components/states.js';
import { reviewItemsBlock } from '../components/review-items.js';
import { KIND_LABEL } from '../components/review-counts.js';

const SCREEN = '/api/v1/screens/db';
const PRODUCTS = '/api/v1/products';
const DRAFTS = '/api/v1/register/drafts';
const DRAFT_TARGETS = '/api/v1/register/draft-targets';
const DRAFT_HELP =
  '선택한 품목으로 등록할 상품을 만듭니다. 가격은 판매 계정의 등록 정책으로 자동 계산되며, 한 품목이라도 가격을 계산하지 못하면 만들지 않습니다.';
const PRICES = '/api/v1/pricing/latest';
const SHAPE_LABEL = {
  SINGLE_LISTING_WITH_OPTIONS: '옵션 상품 하나로 등록',
  SEPARATE_LISTINGS: '품목마다 따로 등록',
  SELECTED_OFFERS: '선택한 구성만 등록',
};
const BASIS_LABEL = { MINIMUM_SALE_PRICE: '최저판매가', TARGET_MARGIN: '목표 마진' };
const TITLE = '통합DB';
const HELP = '모든 상품을 하나의 DB에서 관리하고 등록부터 판매까지 빠르게 운영합니다.';
const LIST_HELP =
  '운영 중인 상품을 최신순으로 보여줍니다. 상품명, 공급처, 공급처 상품번호로 찾을 수 있습니다. 가격은 마지막으로 계산된 값입니다.';
const DETAIL_HELP = '공급처에서 수집한 정보를 그대로 보여줍니다.';
const TARGET_HELP =
  '등록할 품목을 고릅니다. 「등록 대상 확인」은 고른 품목을 지금 등록할 수 있는지만 확인하고, 아무것도 만들지 않습니다.';
const PAGE_SIZE = 20;
// v29 (owner decision 2026-10-07, option 가): a v29 slot the system has no source for keeps its
// place and reads 데이터 없음. Nothing is estimated and nothing comes from demo content.
const NO_DATA = '데이터 없음';
const LIST_COLUMNS = ['상품명', '공급처', '원가', '배송비', '최저판매가', '가격기준', '네이버', '쿠팡', '11번가', '상태', '등록유형'];
// The price columns: filled from the Product's latest recorded price (a display read), else 데이터 없음.
const PRICE_COLUMNS = ['원가', '배송비', '최저판매가', '가격기준', '네이버', '쿠팡', '11번가'];
const CHANNEL_COLUMNS = { 네이버: 'smartstore', 쿠팡: 'coupang', '11번가': 'st11' };
const KPI_CARDS = [
  ['all', '▤', '전체상품'],
  ['registrable', '🛒', '등록가능'],
  ['review', '!', '확인필요'],
  ['soldout', '◫', '품절'],
  ['prohibited', '⊘', '금지상품'],
];

const GROUP_STATUS = { ACTIVE: '운영', RETIRED: '보관됨' };
const FACT_LABEL = {
  original_name: '상품명',
  brand: '브랜드',
  manufacturer: '제조사',
  origin: '원산지',
  stock: '재고',
  sales_channels: '판매채널',
};
// ADR-0031 §5: the supplier's sales-channel restriction, worded from the server's reading.
const MARKET_LABEL = { smartstore: '스마트스토어', coupang: '쿠팡' };
// The server's value is ``SCOPE[:market...]|the supplier's words``; one reading line per market.
function channelWords(value) {
  if (value === null || value === undefined) return chip('확인 필요', 'warn');
  const [reading, ...words] = String(value).split('|');
  const [scope, ...forbidden] = reading.split(':');
  const lines =
    scope === 'ALL_ALLOWED'
      ? ['모든 마켓 판매 가능']
      : scope === 'CLOSED_MALL_ONLY'
        ? ['폐쇄몰 전용 (오픈마켓 판매 불가)']
        : scope === 'LISTED'
          ? forbidden.map((key) => `${MARKET_LABEL[key] ?? key} 판매 불가`)
          : [reading];
  return h(
    'span',
    { 'data-channel-scope': scope },
    ...lines.flatMap((line, index) => (index ? [h('br'), line] : [line])),
    words.length ? h('span', { class: 'mini' }, h('br'), `공급사 문구: ${words.join('|')}`) : null,
  );
}
const STOCK_LABEL = { ON_SALE: '판매 중', SOLD_OUT: '품절', REVIEW_REQUIRED: '확인 필요' };
const BINDING_LABEL = { BASE_PRODUCT: '기본 상품', SOURCE_OFFER: '수량 구성' };
const STATUS_LABEL = { ABSENT: '없음', REVIEW_REQUIRED: '확인 필요' };

// Server reason codes rendered as copy. The page never derives a verdict, only its wording.
const REASON_COPY = {
  PRODUCTS_PRODUCT_RETIRED: '보관된 상품이라 등록 대상으로 고를 수 없습니다.',
  PRODUCTS_MEMBERSHIP_REVISION_MISSING: '연결된 공급처 상품이 없어 고를 수 없습니다.',
  PRODUCTS_MEMBERSHIP_REVISION_NOT_CURRENT: '상품 구성이 바뀌어 고를 수 없습니다. 상세를 다시 불러오세요.',
  PRODUCTS_ITEM_BINDING_MISSING: '연결된 공급처 상품이 없는 품목입니다.',
  PRODUCTS_ITEM_BINDING_MEMBER_NOT_CONFIRMED: '연결된 공급처 상품이 이 상품에 속하지 않습니다.',
  PRODUCTS_SELECTION_NOT_SELECTABLE: '이 상품은 지금 등록 대상으로 고를 수 없습니다.',
  PRODUCTS_SELECTION_MEMBERSHIP_STALE: '선택한 뒤 상품 구성이 바뀌었습니다. 상세를 다시 불러와 다시 고르세요.',
  PRODUCTS_SELECTION_ITEM_OUTSIDE_PRODUCT: '다른 상품의 품목은 함께 고를 수 없습니다.',
  PRODUCTS_SELECTION_ITEM_NOT_SELECTABLE: '고른 품목 중 지금 등록 대상이 될 수 없는 품목이 있습니다.',
  PRODUCTS_SELECTION_INVALID: '선택 요청이 올바르지 않습니다.',
  PRODUCTS_PRODUCT_UNKNOWN: '해당 상품을 찾을 수 없습니다.',
  PRODUCTS_CURSOR_INVALID: '목록 위치가 올바르지 않아 처음부터 다시 불러와야 합니다.',
  PRODUCTS_QUERY_INVALID: '검색어는 100자까지 입력할 수 있습니다.',
  PRODUCTS_PAGE_LIMIT_INVALID: '한 페이지의 상품 수가 올바르지 않습니다.',
  REGISTER_TARGET_ACCOUNT_UNKNOWN: '선택한 판매 계정을 찾을 수 없습니다.',
  MARKETPLACE_ACCOUNT_NOT_BOUND: '판매 계정 연결 필요',
  REGISTER_TARGET_POLICY_MISSING: '등록 정책 저장 필요',
  REGISTER_PRICING_CONTEXT_MISMATCH: '등록 정책의 가격 기준이 이 계정과 맞지 않아 만들지 않았습니다.',
  REGISTER_DRAFT_ITEM_NOT_PRICED: '가격을 계산하지 못한 품목이 있어 만들지 않았습니다.',
  REGISTER_DRAFT_SELECTION_MOVED: '가격을 계산하는 동안 상품 구성이 바뀌어 만들지 않았습니다. 상세를 다시 불러오세요.',
  REGISTER_DRAFT_PRICE_CONTEXT: '가격 정보가 선택한 판매 계정과 맞지 않아 만들지 않았습니다.',
  REQUEST_INVALID: '요청이 올바르지 않습니다.',
};

// Why an Item could not be priced (the server's pricing reason codes), in plain words.
const PRICING_COPY = {
  PRICING_PURCHASE_PRICE_UNRESOLVED: '공급가를 확인하지 못했습니다',
  PRICING_PURCHASE_PRICE_AMBIGUOUS: '공급가가 여러 개라 정하지 못했습니다',
  PRICING_SHIPPING_UNRESOLVED: '배송비를 확인하지 못했습니다',
  PRICING_SHIPPING_CONDITIONAL: '조건부 배송비라 정하지 못했습니다',
  PRICING_SHIPPING_UNKNOWN: '배송비를 확인하지 못했습니다',
  PRICING_MINIMUM_SALE_PRICE_UNRESOLVED: '최저판매가를 확인하지 못했습니다',
  PRICING_MINIMUM_SALE_PRICE_NOT_POSITIVE: '최저판매가가 올바르지 않습니다',
  PRICING_MINIMUM_SALE_PRICE_NOT_OFFER_BOUND: '최저판매가가 이 수량 구성에 맞지 않습니다',
  PRICING_TARGET_MARGIN_UNREACHABLE: '목표 마진으로 가격을 정할 수 없습니다',
  PRICING_QUANTITY_OFFER_UNRESOLVED: '수량 구성의 가격을 확인하지 못했습니다',
  PRICING_FX_STALE: '환율 정보가 오래되었습니다',
  PRICING_FX_UNAVAILABLE: '환율 정보를 가져오지 못했습니다',
  PRICING_OVERSEAS_SHIPPING_UNSET: '해외 배송비가 설정되지 않았습니다 (설정 › 해외구매대행)',
  PRICING_SOURCE_CURRENCY_UNSUPPORTED: '지원하지 않는 통화입니다',
  PRICE_LOSS: '이 가격이면 손해입니다',
  PRICE_BELOW_MIN_MARGIN: '최소 마진보다 낮습니다',
};

// A reason the server gave for an Item or a selection, as copy; an unknown code reads generically.
function copy(code) {
  return REASON_COPY[code] ?? '지금은 고를 수 없습니다.';
}

function pricingCopy(code) {
  return PRICING_COPY[String(code).replace(/^M4_PRICING\./, '')] ?? '가격을 계산하지 못했습니다';
}

function won(value) {
  return typeof value === 'number' ? `${value.toLocaleString('ko-KR')}원` : '—';
}

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value ?? '—'));
}

function chip(text, tone) {
  return h('span', { class: tone ? `chip ${tone}` : 'chip' }, text);
}

// One source fact as its member's current revision states it: a value only when CONFIRMED.
function factValue(fact) {
  if (!fact || fact.status === null) return chip('기록 없음');
  if (fact.key === 'sales_channels' && fact.status === 'ABSENT') return '판매채널 제한 없음';
  if (fact.status !== 'CONFIRMED') {
    return chip(STATUS_LABEL[fact.status] ?? fact.status, fact.status === 'REVIEW_REQUIRED' ? 'warn' : null);
  }
  if (fact.key === 'sales_channels') return channelWords(fact.value);
  return fact.key === 'stock' ? STOCK_LABEL[fact.value] ?? fact.value : fact.value;
}

function composition(item) {
  const c = item.composition;
  const parts = [`수량 ${c.quantity}`];
  if (c.unit_amount) parts.push(`${c.unit_amount}${c.unit_code ?? ''}`);
  if (c.pack_count) parts.push(`${c.pack_count}팩`);
  if (c.units_per_pack) parts.push(`팩당 ${c.units_per_pack}`);
  return parts.join(' · ');
}

function noData(label) {
  return h('td', { class: 'no-data', 'data-no-data': label }, NO_DATA);
}

function noDataKv(label) {
  return h('div', { class: 'kv', 'data-no-data': label }, h('span', {}, label), h('b', { class: 'no-data' }, NO_DATA));
}

// v29's five cards. 전체상품 is the server's own total; the other four have no owner yet.
function kpiCards(total) {
  return h(
    'div',
    { class: 'grid5 page-kpis', 'data-role': 'db-cards' },
    ...KPI_CARDS.map(([key, icon, label]) => {
      const known = key === 'all';
      const card = h(
        'button',
        { type: 'button', class: known ? 'kpi state-card active' : 'kpi state-card', 'data-card': key },
        h('span', { class: 'kpi-top' }, h('span', { class: 'kicon', 'aria-hidden': 'true' }, icon), label),
        known
          ? h('strong', { 'data-total': String(total) }, total.toLocaleString('ko-KR'))
          : h('strong', { class: 'no-data', 'data-no-data': label }, NO_DATA),
      );
      return known ? card : markInert(card, label);
    }),
  );
}

function sourceLabel(names, supplierKey, sourceProductId) {
  return `${supplierName(names, supplierKey)} · 상품번호 ${sourceProductId}`;
}

function nameLines(row, names) {
  if (!row.member_names.length) return h('div', { class: 'mini' }, '공급처 상품 없음');
  return row.member_names.map((member) =>
    h(
      'div',
      { class: 'db-member-name', 'data-member': member.member_id, 'data-revision': member.source_revision_id ?? '' },
      h('span', { class: 'db-name' }, factValue(member.facts[0])),
      h('span', { class: 'mini' }, sourceLabel(names, member.supplier_key, member.source_product_id)),
    ),
  );
}

// The latest recorded prices of one Product: the newest overall (its costs) and one per channel.
function priceSet(prices) {
  const byChannel = new Map();
  for (const price of prices) if (!byChannel.has(price.marketplace_key)) byChannel.set(price.marketplace_key, price);
  const newest = [...prices].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] ?? null;
  return { newest, byChannel };
}

function priceText(column, set) {
  const latest = set.newest;
  if (!latest) return null;
  if (column === '원가') return won(latest.purchase_cost_krw + latest.supplier_shipping_krw);
  if (column === '배송비') return won(latest.supplier_shipping_krw);
  if (column === '최저판매가') return latest.minimum_sale_price_krw === null ? '없음' : won(latest.minimum_sale_price_krw);
  if (column === '가격기준') return BASIS_LABEL[latest.price_basis] ?? '—';
  const channel = set.byChannel.get(CHANNEL_COLUMNS[column]);
  return channel ? won(channel.final_sale_price_krw) : null;
}

// Fills a row's price cells once the latest prices arrive; a cell with nothing recorded keeps 데이터 없음.
function fillPrices(tr, prices) {
  const set = priceSet(prices);
  for (const column of PRICE_COLUMNS) {
    const cell = tr.querySelector(`td[data-no-data='${column}']`);
    const text = priceText(column, set);
    if (!cell || text === null) continue;
    cell.classList.remove('no-data');
    cell.removeAttribute('data-no-data');
    cell.dataset.price = column;
    cell.title = `${dotDate(set.newest.created_at)} 계산 기준`;
    cell.textContent = text;
  }
}

function listRow(row, state, onSelect, names) {
  const product = row.product;
  const id = product.product_group_id;
  const tr = h(
    'tr',
    {
      'data-product': id,
      'aria-selected': state.productId === id ? 'true' : 'false',
      tabindex: '0',
      onclick: () => onSelect(id),
      onkeydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onSelect(id);
        }
      },
    },
    h(
      'td',
      {},
      nameLines(row, names),
      h('div', { class: 'mini db-row-meta' }, `품목 ${product.items.length}개 · ${dotDateTime(product.created_at)} 등록`),
    ),
    h('td', {}, [...new Set(row.member_names.map((member) => supplierName(names, member.supplier_key)))].join(', ') || '—'),
    ...PRICE_COLUMNS.map(noData),
    h('td', {}, chip(GROUP_STATUS[product.status] ?? product.status, product.status === 'ACTIVE' ? 'good' : 'warn')),
    noData('등록유형'),
  );
  return tr;
}

function memberGroup(source, names) {
  const facts = source.facts.map((fact) => h(
    'div',
    { class: 'kv', 'data-fact': fact.key, 'data-status': fact.status ?? 'NONE' },
    h('span', {}, FACT_LABEL[fact.key] ?? fact.key),
    h('b', {}, factValue(fact)),
  ));
  const images = source.images;
  return h(
    'div',
    {
      class: 'detail-group db-member',
      'data-member': source.member_id,
      'data-revision': source.source_revision_id ?? '',
    },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('h4', {}, sourceLabel(names, source.supplier_key, source.source_product_id)),
      source.facts_status
        ? chip(source.facts_status === 'CONFIRMED' ? '정보 확인됨' : '정보 확인 필요', source.facts_status === 'CONFIRMED' ? 'good' : 'warn')
        : chip('수집 정보 없음', 'warn'),
    ),
    ...facts,
    images
      ? h(
          'div',
          { class: 'kv', 'data-fact': 'images', 'data-status': images.status ?? 'NONE', 'data-representative': images.representative_sha256 ?? '' },
          h('span', {}, '이미지'),
          h('b', { class: 'db-images' }, ...imagesValue(images)),
        )
      : null,
  );
}

// The representative reference and counts are shown when they exist, but they never stand in for
// the field's own status: an images field that is not CONFIRMED always shows that it is not.
function imagesValue(images) {
  const shown = [];
  if (images.representative_sha256 || images.status === 'CONFIRMED') {
    shown.push(`${images.included}/${images.references}장`);
  }
  if (images.status !== 'CONFIRMED') shown.push(factValue({ key: 'images', status: images.status }));
  return shown;
}

// The representative image, through the bound Item's own image read (the image owner's route);
// without one the slot keeps its place empty.
function detailThumb(detail) {
  const empty = () => h('div', { class: 'hero-thumb', 'data-role': 'detail-thumb', 'data-thumb': 'none', title: '대표 이미지 데이터 없음' }, h('span', { 'aria-hidden': 'true' }, '🖼'));
  for (const source of detail.member_sources) {
    const sha = source.images?.representative_sha256;
    const item = detail.product.items.find((candidate) => candidate.current_binding?.group_member_id === source.member_id);
    if (!sha || !item) continue;
    const holder = h('div', { class: 'hero-thumb', 'data-role': 'detail-thumb', 'data-thumb': 'item-image' });
    const image = h('img', { alt: '대표 이미지', src: `${PRODUCTS}/items/${encodeURIComponent(item.item_id)}/images/${encodeURIComponent(sha)}` });
    image.addEventListener('error', () => holder.replaceWith(empty()));
    holder.append(image);
    return holder;
  }
  return empty();
}

// The first member's confirmed name, or nothing when no member states one.
function detailName(detail) {
  const fact = detail.member_sources.flatMap((source) => source.facts).find((candidate) => candidate.key === 'original_name');
  return fact?.status === 'CONFIRMED' ? fact.value : null;
}

// v29's detail groups, as their own card under the detail. Only what a member's source states is
// shown; the rest keeps its place.
function priceKv(label, text) {
  return text === null ? noDataKv(label) : h('div', { class: 'kv', 'data-price': label }, h('span', {}, label), h('b', {}, text));
}

function channelText(price) {
  if (!price) return null;
  return `${won(price.final_sale_price_krw)} · 마진 ${(price.expected_net_margin_bp / 100).toFixed(1)}%`;
}

// v29's 가격 정보 and 채널별 판매가 / 마진율 groups, from the latest recorded prices.
function priceGroups(prices = []) {
  const set = priceSet(prices);
  const latest = set.newest;
  // 도매가: the supplier's own price (in its currency for an overseas source), as the snapshot recorded it.
  const wholesale = latest ? (latest.source_currency ? `${latest.source_price} ${latest.source_currency}` : won(latest.purchase_cost_krw)) : null;
  return [
    h(
      'div',
      { class: 'detail-group', 'data-role': 'detail-prices' },
      h('h4', {}, '가격 정보'),
      priceKv('도매가', wholesale),
      priceKv('배송비', priceText('배송비', set)),
      priceKv('원가', priceText('원가', set)),
      priceKv('가격기준', priceText('가격기준', set)),
      latest ? h('div', { class: 'mini' }, `${dotDateTime(latest.created_at)}에 계산된 가격입니다.`) : null,
    ),
    h(
      'div',
      { class: 'detail-group', 'data-role': 'detail-channels' },
      h('h4', {}, '채널별 판매가 / 마진율'),
      ...Object.entries(CHANNEL_COLUMNS).map(([label, key]) => priceKv(label, channelText(set.byChannel.get(key)))),
    ),
  ];
}

function v29Groups(detail) {
  const brand = detail ? detail.member_sources.flatMap((source) => source.facts).find((fact) => fact.key === 'brand') : null;
  return [
    h('h3', {}, '가격 · 채널 · 태그'),
    h(
      'div',
      { class: 'detail-group', 'data-role': 'detail-basics' },
      detail
        ? h('div', { class: 'kv', 'data-fact': 'brand-summary' }, h('span', {}, '브랜드'), h('b', {}, factValue(brand)))
        : noDataKv('브랜드'),
      // ADR-0029 C3: the bound SmartStore account's category recommendation (both v29 slots).
      detail
        ? h('div', { 'data-role': 'detail-ai-category' }, aiCategoryPanel(detail.product.product_group_id))
        : noDataKv('카테고리'),
      detail ? null : noDataKv('카테고리 추천 신뢰도'),
    ),
    ...priceGroups(),
    // ADR-0028 T4: the SmartStore tag recommendation of the account bound to the connection.
    h(
      'div',
      { class: 'detail-group', 'data-role': 'detail-tags' },
      h('h4', {}, '태그'),
      detail ? aiTagsPanel(detail.product.product_group_id) : h('div', { class: 'mini no-data' }, NO_DATA),
    ),
    // ADR-0027 AIS-2: the product-name recommendation, live while the ai capability is READY.
    h(
      'div',
      { class: 'detail-group', 'data-role': 'detail-ai-name' },
      h('h4', {}, 'AI 상품명 추천'),
      detail ? aiNamePanel(detail.product.product_group_id) : h('div', { class: 'mini no-data' }, NO_DATA),
    ),
    h(
      'div',
      { class: 'detail-actions', 'data-role': 'detail-v29-actions' },
      markInert(h('button', { type: 'button', class: 'btn' }, '상세 편집'), '상세 편집'),
    ),
  ];
}

export default {
  key: 'db',
  title: TITLE,
  navLabel: TITLE,
  icon: '◫',
  async render(ctx) {
    const head = pageHead({
      title: TITLE,
      help: HELP,
      actions: [markInert(h('button', { type: 'button', class: 'btn' }, '⇩ 엑셀 다운로드'))],
    });
    const screen = await getJson(SCREEN);
    const cards = kpiCards(screen.products_total ?? 0);
    if (screen.meta.state === 'EMPTY') {
      return fragment(
        head,
        emptyState({
          title: '통합DB에 상품이 없습니다',
          copy: '수집이 완료되면 정규화된 상품이 이곳에 쌓입니다.',
          action: { label: '상품 수집하기', onSelect: () => ctx.navigate('collect', { view: 'jobs' }) },
        }),
      );
    }
    if (screen.meta.state !== 'READY') return fragment(head, unsupportedState(screen.meta));
    return fragment(head, cards, workspace(ctx.params.get('product'), ctx.navigate));
  },
};

function workspace(initialProduct, navigateTo) {
  // Page state only: nothing here is a second copy of product truth, and nothing outlives a reload.
  const state = {
    query: null,
    includeTest: false,
    cursors: [null], // the cursor of every page visited; the last one names the current page
    listSeq: 0,
    productId: null,
    detailSeq: 0,
    detail: null,
    chosen: new Set(),
    targetSeq: 0,
    draftSeq: 0,
  };

  const testToggle = h('input', { type: 'checkbox', name: 'include_test', 'data-role': 'db-include-test' });
  testToggle.addEventListener('change', () => {
    state.includeTest = testToggle.checked;
    state.cursors = [null];
    loadList();
  });
  let names = new Map();
  const search = h('input', {
    type: 'search',
    class: 'field search',
    name: 'q',
    maxlength: '100',
    placeholder: '상품명, 공급처, 공급처 상품번호로 검색하세요...',
    'aria-label': '통합DB 검색',
    autocomplete: 'off',
  });
  const toolbar = h(
    'form',
    { class: 'db-toolbar panel', role: 'search', 'data-role': 'db-search' },
    search,
    // v29's 카테고리 / 플랫폼 / 공급처 / 가격기준 filters have no server filter yet, so they are not
    // shown. Synthetic test products are left out unless the operator asks for them.
    h('label', { class: 'mini db-test-toggle' }, testToggle, ' 테스트 상품 보기'),
    h('button', { type: 'submit', class: 'btn blue' }, '검색'),
    markInert(h('button', { type: 'button', class: 'btn ai-btn', 'data-role': 'db-ai' }, '✨ AI 추천'), 'AI 추천'),
  );
  const listBody = h('tbody', {});
  const total = h('span', { class: 'mini', 'data-role': 'matching-total' });
  const pageLabel = h('span', { class: 'mini', 'data-role': 'page-number' });
  const first = h('button', { type: 'button', class: 'btn', 'data-action': 'first-page' }, '처음');
  const previous = h('button', { type: 'button', class: 'btn', 'data-action': 'previous-page' }, '이전');
  const next = h('button', { type: 'button', class: 'btn', 'data-action': 'next-page' }, '다음');
  const listPanel = h(
    'section',
    { class: 'panel db-list', 'data-role': 'product-list' },
    h('div', { class: 'db-list-head' }, withHelp(h('h2', { class: 'panel-title' }, '상품 목록'), LIST_HELP), total),
    h(
      'table',
      { class: 'table' },
      h(
        'thead',
        {},
        h('tr', {}, ...LIST_COLUMNS.map((label) => h('th', {}, label))),
      ),
      listBody,
    ),
    h('div', { class: 'db-pager' }, pageLabel, first, previous, next),
  );
  const detailPanel = h('aside', { class: 'panel db-detail', 'data-role': 'product-detail' });
  const v29Panel = h('section', { class: 'panel db-detail db-v29-groups', 'data-role': 'product-v29-groups' });
  let lastPage = null;

  function renderIdleDetail() {
    v29Panel.replaceChildren(...v29Groups(null));
    detailPanel.removeAttribute('data-product');
    detailPanel.dataset.state = 'idle';
    detailPanel.replaceChildren(
      withHelp(h('h3', {}, '상품 상세 정보'), DETAIL_HELP),
      h('div', { class: 'mini' }, '목록에서 상품을 고르면 상세와 품목이 표시됩니다.'),
    );
  }

  function markSelectedRow() {
    for (const row of listBody.querySelectorAll('tr[data-product]')) {
      row.setAttribute('aria-selected', row.dataset.product === state.productId ? 'true' : 'false');
    }
  }

  function renderList(page) {
    lastPage = page;
    total.textContent = `${page.matching_total.toLocaleString('ko-KR')}개`;
    pageLabel.textContent = `${state.cursors.length}페이지`;
    first.disabled = state.cursors.length === 1;
    previous.disabled = state.cursors.length === 1;
    next.disabled = page.next_cursor === null;
    listBody.replaceChildren(
      ...(page.products.length
        ? page.products.map((row) => listRow(row, state, selectProduct, names))
        : [h('tr', {}, h('td', { class: 'table-empty', colspan: '11' }, state.query ? '검색 결과가 없습니다.' : '운영 중인 상품이 없습니다.'))]),
    );
  }

  async function loadList() {
    const seq = ++state.listSeq;
    const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
    if (state.query) params.set('q', state.query);
    if (!state.includeTest) params.set('include_test', 'false');
    const cursor = state.cursors.at(-1);
    if (cursor) params.set('cursor', cursor);
    listPanel.setAttribute('aria-busy', 'true');
    try {
      const page = await getJson(`${PRODUCTS}?${params}`);
      if (seq !== state.listSeq) return; // a newer list request superseded this one
      renderList(page);
      loadListPrices(page, seq);
    } catch (error) {
      if (seq !== state.listSeq) return;
      listBody.replaceChildren(
        h('tr', {}, h('td', { class: 'table-empty', colspan: '11', 'data-reason': errorCode(error) ?? '' }, errorText(error, REASON_COPY))),
      );
    } finally {
      if (seq === state.listSeq) listPanel.removeAttribute('aria-busy');
    }
  }

  // The page's latest recorded prices, a display read; without one a price cell keeps 데이터 없음.
  async function loadListPrices(page, seq) {
    const ids = page.products.map((row) => row.product.product_group_id);
    if (!ids.length) return;
    let prices;
    try {
      prices = await getJson(`${PRICES}?${new URLSearchParams(ids.map((id) => ['product_group_id', id]))}`);
    } catch {
      return;
    }
    if (seq !== state.listSeq) return;
    for (const id of ids) {
      const tr = listBody.querySelector(`tr[data-product='${CSS.escape(id)}']`);
      if (tr) fillPrices(tr, prices.filter((price) => price.product_group_id === id));
    }
  }

  // Choosing a Product drops everything of the previous one at once: its detail, its chosen
  // Items and any selection check still in flight.
  function selectProduct(id) {
    if (state.productId === id) return;
    state.productId = id;
    state.detail = null;
    state.chosen = new Set();
    state.targetSeq += 1;
    state.draftSeq += 1; // a Draft command still in flight answers for the previous Product only
    const seq = ++state.detailSeq;
    markSelectedRow();
    // Product context isolation: nothing of the previous Product stays in the v29 card either.
    v29Panel.replaceChildren(...v29Groups(null));
    detailPanel.dataset.product = id;
    detailPanel.dataset.state = 'loading';
    detailPanel.replaceChildren(
      withHelp(h('h3', {}, '상품 상세 정보'), DETAIL_HELP),
      h('div', { class: 'mini' }, '상세를 불러오는 중입니다.'),
    );
    getJson(`${PRODUCTS}/${encodeURIComponent(id)}/detail`).then(
      (detail) => {
        // Only the latest request for the Product selected now may render.
        if (seq !== state.detailSeq || state.productId !== id || detail.product.product_group_id !== id) return;
        state.detail = detail;
        renderDetail();
      },
      (error) => {
        if (seq !== state.detailSeq || state.productId !== id) return;
        detailPanel.dataset.state = 'error';
        detailPanel.replaceChildren(errorState(error));
      },
    );
  }

  function itemRow(item, selection) {
    const box = h('input', {
      type: 'checkbox',
      'data-item': item.item_id,
      'aria-label': `${composition(item)} 선택`,
      disabled: !selection.selectable,
    });
    box.checked = state.chosen.has(item.item_id);
    box.addEventListener('change', () => {
      if (box.checked) state.chosen.add(item.item_id);
      else state.chosen.delete(item.item_id);
      // A changed choice is a new selection: the previous check no longer describes it.
      state.targetSeq += 1;
      state.draftSeq += 1;
      renderSelection();
    });
    const binding = item.current_binding;
    return h(
      'tr',
      { 'data-item': item.item_id, 'data-selectable': selection.selectable ? 'true' : 'false' },
      h('td', {}, box),
      h('td', {}, composition(item)),
      h('td', { 'data-binding': binding?.binding_kind ?? 'NONE' }, binding ? BINDING_LABEL[binding.binding_kind] ?? '연결됨' : '연결 없음'),
      h(
        'td',
        {},
        selection.selectable
          ? chip('선택 가능', 'good')
          : h('span', { class: 'mini', 'data-reason': selection.reason }, copy(selection.reason)),
      ),
    );
  }

  // ---------------------------------------------------------------- 등록 초안 (Gate 1 G1-D)
  //
  // The operator picks a target the server lists and a listing shape; the server revalidates
  // the selection, prices every Item through M4 and pins it, or creates nothing. The page sends
  // only those choices — never a price, a policy revision or a snapshot — and shows what the
  // server answered. One click is one POST; a reload never sends it again.
  const targetSelect = h('select', { id: 'draft-target', name: 'draft_target', 'data-role': 'draft-target' });
  const shapeSelect = h('select', { id: 'draft-shape', name: 'listing_shape', 'data-role': 'draft-shape' });
  const draftButton = h(
    'button',
    { type: 'button', class: 'btn blue', 'data-action': 'create-draft', onclick: () => createDraft() },
    '등록 초안 만들기',
  );
  const draftResult = h('div', { class: 'db-draft', 'data-role': 'draft-result' });
  const draftPanel = h(
    'div',
    { class: 'detail-group db-draft-form', 'data-role': 'draft-form' },
    withHelp(h('h4', {}, '등록할 상품 만들기'), DRAFT_HELP),
    h('div', { class: 'form-row' }, h('label', { for: 'draft-target' }, '판매 계정'), targetSelect),
    h('div', { class: 'form-row' }, h('label', { for: 'draft-shape' }, '등록 방식'), shapeSelect),
    h('div', { class: 'detail-actions' }, draftButton),
    draftResult,
  );
  let targets = [];
  let drafting = false;

  // Two accounts of one marketplace are told apart by their order, never by an internal id.
  function accountSuffix(target, index) {
    const same = targets.filter((other) => other.marketplace_key === target.marketplace_key);
    return same.length > 1 ? ` 계정 ${same.indexOf(targets[index]) + 1}` : '';
  }

  function renderTargets(listed) {
    targets = listed.targets;
    targetSelect.replaceChildren(
      h('option', { value: '' }, targets.length ? '판매 계정 선택' : '등록할 수 있는 판매 계정이 없습니다'),
      ...targets.map((target, index) =>
        h(
          'option',
          {
            value: String(index),
            disabled: target.unavailable_reason !== null,
            'data-account': target.marketplace_account_id,
            'data-reason': target.unavailable_reason ?? '',
          },
          `${marketplaceLabel(target.marketplace_key)}${accountSuffix(target, index)}` +
            (target.unavailable_reason ? ` · ${copy(target.unavailable_reason)}` : ''),
        ),
      ),
    );
    shapeSelect.replaceChildren(
      ...listed.listing_shapes.map((shape) => h('option', { value: shape }, SHAPE_LABEL[shape] ?? shape)),
    );
    renderDraftButton();
  }

  function renderDraftButton() {
    const detail = state.detail;
    draftButton.disabled =
      drafting ||
      !detail ||
      state.chosen.size === 0 ||
      detail.selection_unavailable_reason !== null ||
      targetSelect.value === '';
  }
  targetSelect.addEventListener('change', () => {
    state.draftSeq += 1;
    draftResult.replaceChildren();
    renderDraftButton();
  });

  function pinRow(pin, items) {
    const item = items.get(pin.item_id);
    return h(
      'tr',
      { 'data-pin-item': pin.item_id, 'data-pricing-snapshot': pin.pricing_snapshot_id, 'data-pricing-outcome': pin.pricing_outcome },
      h('td', {}, item ? composition(item) : '품목'),
      h('td', {}, won(pin.final_sale_price_krw)),
      h('td', {}, BASIS_LABEL[pin.price_basis] ?? '—'),
    );
  }

  async function createDraft() {
    const detail = state.detail;
    const target = targets[Number(targetSelect.value)];
    if (drafting || !detail || !target || state.chosen.size === 0) return;
    const product = detail.product;
    const id = product.product_group_id;
    const seq = ++state.draftSeq;
    drafting = true;
    renderDraftButton();
    draftResult.replaceChildren();
    const body = {
      product_group_id: id,
      membership_revision_id: product.membership_revision_id,
      item_ids: product.items.map((item) => item.item_id).filter((itemId) => state.chosen.has(itemId)),
      marketplace_key: target.marketplace_key,
      marketplace_account_id: target.marketplace_account_id,
      listing_shape: shapeSelect.value,
      actor: 'operator',
    };
    try {
      const created = await sendJson('POST', DRAFTS, body);
      toast('등록 대기 목록에 추가', `품목 ${created.items.length}개를 등록관리에 추가했습니다.`);
      // The Draft exists either way; only the Product it was made from may show it.
      if (seq !== state.draftSeq || state.productId !== id || created.product_group_id !== id) return;
      const items = new Map(product.items.map((item) => [item.item_id, item]));
      draftResult.dataset.state = 'created';
      draftResult.dataset.draft = created.draft_id;
      draftResult.dataset.policyRevision = created.policy_revision;
      draftResult.dataset.membershipRevision = created.membership_revision_id;
      draftResult.replaceChildren(
        h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 대기 목록에 추가됨'), chip(`품목 ${created.items.length}개`, 'good')),
        kv('판매 채널', marketplaceLabel(created.marketplace_key)),
        kv('등록 방식', SHAPE_LABEL[created.listing_shape] ?? '—'),
        h(
          'table',
          { class: 'table' },
          h('thead', {}, h('tr', {}, ...['품목', '판매가', '가격기준'].map((label) => h('th', {}, label)))),
          h('tbody', {}, ...created.items.map((pin) => pinRow(pin, items))),
        ),
        h(
          'div',
          { class: 'detail-actions' },
          h(
            'button',
            {
              type: 'button',
              class: 'btn blue',
              'data-action': 'open-draft',
              onclick: () => navigateTo('register', { draft: created.draft_id }),
            },
            '등록관리에서 열기',
          ),
        ),
      );
    } catch (error) {
      if (seq !== state.draftSeq || state.productId !== id) return;
      const code = errorCode(error);
      const unpriced = code === 'REGISTER_DRAFT_ITEM_NOT_PRICED' ? error.error?.details?.items ?? {} : {};
      draftResult.dataset.state = 'refused';
      const items = new Map(product.items.map((item) => [item.item_id, item]));
      draftResult.replaceChildren(
        h('div', { class: 'note', 'data-reason': code ?? '' }, errorText(error, REASON_COPY)),
        ...Object.entries(unpriced).map(([itemId, reasons]) =>
          h(
            'div',
            { class: 'mini', 'data-unpriced-item': itemId, 'data-reasons': reasons.map((reason) => reason.code).join(' ') },
            `${items.has(itemId) ? composition(items.get(itemId)) : '품목'}: ${[...new Set(reasons.map((reason) => pricingCopy(reason.code)))].join(', ')}`,
          ),
        ),
        h(
          'button',
          {
            type: 'button',
            class: 'btn',
            'data-action': 'reload-detail',
            onclick: () => {
              state.productId = null; // read the same Product again, fresh, with nothing chosen
              selectProduct(id);
            },
          },
          '상세 다시 불러오기',
        ),
      );
    } finally {
      // One command at a time: the next one may start once this one has its answer.
      drafting = false;
      renderDraftButton();
    }
  }

  const selectionBar = h('div', { class: 'db-selection', 'data-role': 'selection' });
  const targetResult = h('div', { class: 'db-target', 'data-role': 'target-result' });
  const checkButton = h(
    'button',
    { type: 'button', class: 'btn blue', 'data-action': 'check-registration-target', onclick: () => checkTarget() },
    '등록 대상 확인',
  );

  function renderSelection() {
    const detail = state.detail;
    if (!detail) return;
    const id = detail.product.product_group_id;
    selectionBar.dataset.product = id;
    selectionBar.dataset.count = String(state.chosen.size);
    selectionBar.replaceChildren(h('b', {}, `품목 ${state.chosen.size}개 선택`));
    checkButton.disabled = state.chosen.size === 0 || detail.selection_unavailable_reason !== null;
    targetResult.replaceChildren();
    delete targetResult.dataset.state;
    // A changed choice is a new command: an earlier answer no longer describes it.
    draftResult.replaceChildren();
    delete draftResult.dataset.state;
    delete draftResult.dataset.draft;
    renderDraftButton();
  }

  // Gate 2 G2-C (ADR-0016 §7): this Product's ReviewItems from M4 base readiness, with that
  // producer's coverage. The block reads the server's rows; the owner re-derives any resolution.
  const productReview = reviewItemsBlock({
    kindLabel: KIND_LABEL,
    emptyCopy: '이 상품에 기록된 검토 항목이 없습니다.',
    errorCopy: (code) => REASON_COPY[code] ?? errorText({ error: { code } }),
    scopeLabel: (item) => {
      const found = state.detail?.product.items.find((candidate) => candidate.item_id === item.scope.item_id);
      return found ? composition(found) : null;
    },
  });

  async function loadProductReview(id, slot) {
    const block = await productReview({ product_group_id: id });
    // Product context isolation: an answer for another Product, or a detail since redrawn, is dropped.
    if (!block || state.productId !== id || !slot.isConnected) return;
    slot.replaceChildren(block);
  }

  function renderDetail() {
    const detail = state.detail;
    const product = detail.product;
    const reviewSlot = h('div', { class: 'detail-group', 'data-role': 'product-review', 'data-product': product.product_group_id });
    const selection = new Map(detail.item_selection.map((entry) => [entry.item_id, entry]));
    detailPanel.dataset.product = product.product_group_id;
    detailPanel.dataset.state = 'ready';
    detailPanel.dataset.membership = product.membership_revision_id ?? '';
    detailPanel.replaceChildren(
      withHelp(h('h3', {}, '상품 상세 정보'), DETAIL_HELP),
      h(
        'div',
        { class: 'detail-title db-detail-title' },
        detailThumb(detail),
        h(
          'div',
          { class: 'db-detail-name' },
          h('b', { 'data-role': 'detail-name' }, detailName(detail) ?? '이름 없음'),
          h('div', { class: 'mini' }, `등록 ${dotDateTime(product.created_at)}`),
        ),
        chip(GROUP_STATUS[product.status] ?? product.status, product.status === 'ACTIVE' ? 'good' : 'warn'),
      ),

      h(
        'div',
        { class: 'detail-group' },
        kv('연결된 공급처 상품', `${product.members.length}개`),
        product.retired_at ? kv('보관 시각', dotDateTime(product.retired_at)) : null,
      ),
      ...detail.member_sources.map((source) => memberGroup(source, names)),
      h(
        'div',
        { class: 'detail-group db-items' },
        withHelp(h('h4', {}, '품목'), TARGET_HELP),
        detail.selection_unavailable_reason
          ? h('div', { class: 'note', 'data-reason': detail.selection_unavailable_reason }, copy(detail.selection_unavailable_reason))
          : null,
        h(
          'table',
          { class: 'table' },
          h('thead', {}, h('tr', {}, ...['선택', '구성', '연결', '상태'].map((label) => h('th', {}, label)))),
          h('tbody', {}, ...product.items.map((item) => itemRow(item, selection.get(item.item_id)))),
        ),
      ),
      reviewSlot,
      selectionBar,
      h('div', { class: 'detail-actions' }, checkButton),
      targetResult,
      draftPanel,
    );
    v29Panel.replaceChildren(...v29Groups(detail));
    renderSelection();
    loadProductReview(product.product_group_id, reviewSlot);
    loadDetailPrices(product.product_group_id);
  }

  // The Product's latest recorded prices for the v29 price groups (a display read).
  async function loadDetailPrices(id) {
    let prices;
    try {
      prices = await getJson(`${PRICES}?${new URLSearchParams([['product_group_id', id]])}`);
    } catch {
      return;
    }
    if (state.productId !== id || state.detail?.product.product_group_id !== id || !prices.length) return;
    // Only the two price groups are redrawn: the AI panels beside them keep their own state.
    const [pricesGroup, channelsGroup] = priceGroups(prices);
    v29Panel.querySelector("[data-role='detail-prices']")?.replaceWith(pricesGroup);
    v29Panel.querySelector("[data-role='detail-channels']")?.replaceWith(channelsGroup);
  }

  async function checkTarget() {
    const detail = state.detail;
    if (!detail || state.chosen.size === 0) return;
    const id = detail.product.product_group_id;
    const seq = ++state.targetSeq;
    const params = new URLSearchParams([['membership_revision_id', detail.product.membership_revision_id]]);
    for (const itemId of state.chosen) params.append('item_id', itemId);
    checkButton.disabled = true;
    try {
      const target = await getJson(`${PRODUCTS}/${encodeURIComponent(id)}/registration-target?${params}`);
      if (seq !== state.targetSeq || state.productId !== id || target.product_group_id !== id) return;
      targetResult.dataset.state = 'confirmed';
      targetResult.dataset.product = id;
      targetResult.replaceChildren(
        h('div', { class: 'supplier-head-row' }, h('b', {}, '등록할 수 있습니다'), chip(`품목 ${target.items.length}개`, 'good')),
        ...target.items.map((item) => h('div', { class: 'mini', 'data-target-item': item.item_id }, `수량 ${item.quantity}`)),
      );
    } catch (error) {
      if (seq !== state.targetSeq || state.productId !== id) return;
      const code = errorCode(error);
      targetResult.dataset.state = 'refused';
      targetResult.dataset.product = id;
      const reload = h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'reload-detail',
          onclick: () => {
            state.productId = null; // read the same Product again, fresh, with nothing chosen
            selectProduct(id);
          },
        },
        '상세 다시 불러오기',
      );
      targetResult.replaceChildren(h('div', { class: 'note', 'data-reason': code ?? '' }, errorText(error, REASON_COPY)), reload);
    } finally {
      if (seq === state.targetSeq) checkButton.disabled = state.chosen.size === 0;
    }
  }

  toolbar.addEventListener('submit', (event) => {
    event.preventDefault();
    state.query = search.value.trim() || null;
    state.cursors = [null];
    loadList();
  });
  first.addEventListener('click', () => {
    state.cursors = [null];
    loadList();
  });
  previous.addEventListener('click', () => {
    if (state.cursors.length > 1) state.cursors.pop();
    loadList();
  });
  next.addEventListener('click', () => {
    if (!lastPage?.next_cursor) return;
    state.cursors.push(lastPage.next_cursor);
    loadList();
  });

  renderIdleDetail();
  // Supplier names for the list and detail; the list is drawn again once they arrive.
  supplierNames().then((found) => {
    names = found;
    if (lastPage) {
      renderList(lastPage);
      loadListPrices(lastPage, state.listSeq);
    }
  });
  loadList();
  // The targets are the owners' accounts, bindings and policies: read once for this page.
  getJson(DRAFT_TARGETS).then(renderTargets, (error) => {
    targetSelect.replaceChildren(h('option', { value: '' }, errorText(error, REASON_COPY)));
    renderDraftButton();
  });
  if (initialProduct) selectProduct(initialProduct);
  return h('div', { class: 'db-workspace' }, toolbar, h('div', { class: 'db-layout' }, listPanel, h('div', { class: 'db-side' }, detailPanel, v29Panel)));
}
