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

import { ApiError, getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { dotDateTime } from '../core/format.js';
import { withHelp } from '../core/help.js';
import { markInert } from '../core/inert.js';
import { marketplaceLabel } from '../core/platform.js';
import { toast } from '../core/toast.js';
import { pageHead } from '../components/page-head.js';
import { emptyState, errorState, unsupportedState } from '../components/states.js';

const SCREEN = '/api/v1/screens/db';
const PRODUCTS = '/api/v1/products';
const DRAFTS = '/api/v1/register/drafts';
const DRAFT_TARGETS = '/api/v1/register/draft-targets';
const DRAFT_HELP =
  '선택한 품목으로 등록 초안을 만듭니다. 서버가 선택을 다시 검증하고, 계정의 현재 등록 정책으로 M4가 매긴 가격 스냅샷을 품목마다 고정합니다. 하나라도 가격이 정해지지 않으면 초안을 만들지 않습니다.';
const SHAPE_LABEL = {
  SINGLE_LISTING_WITH_OPTIONS: '옵션 상품 하나로 등록',
  SEPARATE_LISTINGS: '품목마다 따로 등록',
  SELECTED_OFFERS: '선택한 구성만 등록',
};
const BASIS_LABEL = { MINIMUM_SALE_PRICE: '최저판매가', TARGET_MARGIN: '목표 마진' };
const OUTCOME_LABEL = { RECORDED: '새로 기록', UNCHANGED: '기존 스냅샷' };
const TITLE = '통합DB';
const HELP = '모든 상품을 하나의 DB에서 관리하고 등록부터 판매까지 빠르게 운영합니다.';
const LIST_HELP =
  '운영 중인 정규 상품만 최신순으로 보여줍니다. 검색은 상품 ID, 공급처, 공급처 상품번호, 각 원천 상품의 현재 확정 상품명을 서버에서 찾습니다.';
const DETAIL_HELP =
  '원천 정보는 각 구성원의 현재 원천 리비전을 그대로 읽어 보여주며, 다른 구성원이나 상품의 값으로 채우지 않습니다.';
const TARGET_HELP =
  '선택한 상품의 품목만 등록 대상으로 고를 수 있습니다. 확인은 서버가 현재 상품 구성으로 다시 검증할 뿐, 등록 초안이나 가격은 아직 만들지 않습니다.';
const PAGE_SIZE = 20;

const GROUP_STATUS = { ACTIVE: '운영', RETIRED: '보관됨' };
const FACT_LABEL = {
  original_name: '상품명',
  brand: '브랜드',
  manufacturer: '제조사',
  origin: '원산지',
  stock: '재고',
};
const STOCK_LABEL = { ON_SALE: '판매 중', SOLD_OUT: '품절', REVIEW_REQUIRED: '확인 필요' };
const BINDING_LABEL = { BASE_PRODUCT: '기본 상품', SOURCE_OFFER: '수량 구성' };
const STATUS_LABEL = { ABSENT: '없음', REVIEW_REQUIRED: '확인 필요' };

// Server reason codes rendered as copy. The page never derives a verdict, only its wording.
const REASON_COPY = {
  PRODUCTS_PRODUCT_RETIRED: '보관된 상품이라 등록 대상으로 고를 수 없습니다.',
  PRODUCTS_MEMBERSHIP_REVISION_MISSING: '확정된 구성원이 없어 고를 수 없습니다.',
  PRODUCTS_MEMBERSHIP_REVISION_NOT_CURRENT: '구성원 기록이 현재 구성과 맞지 않아 고를 수 없습니다.',
  PRODUCTS_ITEM_BINDING_MISSING: '현재 연결된 원천 상품이 없는 품목입니다.',
  PRODUCTS_ITEM_BINDING_MEMBER_NOT_CONFIRMED: '연결된 원천 상품이 이 상품의 확정 구성원이 아닙니다.',
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
  MARKETPLACE_ACCOUNT_NOT_BOUND: '판매 계정 연결이 확인되지 않음',
  REGISTER_TARGET_POLICY_MISSING: '등록 정책 없음',
  REGISTER_PRICING_CONTEXT_MISMATCH: '등록 정책의 가격 기준이 이 계정과 맞지 않아 초안을 만들지 않았습니다.',
  REGISTER_DRAFT_ITEM_NOT_PRICED: 'M4가 가격을 정하지 못한 품목이 있어 초안을 만들지 않았습니다.',
  REGISTER_DRAFT_SELECTION_MOVED: '가격을 정하는 동안 상품 구성이 바뀌어 초안을 만들지 않았습니다.',
  REGISTER_DRAFT_PRICE_CONTEXT: '가격 스냅샷이 초안의 대상과 맞지 않아 초안을 만들지 않았습니다.',
  REQUEST_INVALID: '요청이 올바르지 않습니다.',
};

function copy(code) {
  return REASON_COPY[code] ?? code;
}

function errorCode(error) {
  return error instanceof ApiError ? error.error?.code ?? null : null;
}

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value ?? '—'));
}

function chip(text, tone) {
  return h('span', { class: tone ? `chip ${tone}` : 'chip' }, text);
}

function short(id) {
  return id ? id.slice(0, 8) : '—';
}

// One source fact as its member's current revision states it: a value only when CONFIRMED.
function factValue(fact) {
  if (!fact || fact.status === null) return chip('기록 없음');
  if (fact.status !== 'CONFIRMED') {
    return chip(STATUS_LABEL[fact.status] ?? fact.status, fact.status === 'REVIEW_REQUIRED' ? 'warn' : null);
  }
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

function nameLines(row) {
  if (!row.member_names.length) return h('div', { class: 'mini' }, '확정 구성원 없음');
  return row.member_names.map((member) =>
    h(
      'div',
      { class: 'db-member-name', 'data-member': member.member_id, 'data-revision': member.source_revision_id ?? '' },
      h('span', { class: 'db-name' }, factValue(member.facts[0])),
      h('span', { class: 'mini' }, `${member.supplier_key} · ${member.source_product_id}`),
    ),
  );
}

function listRow(row, state, onSelect) {
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
    h('td', {}, nameLines(row)),
    h('td', {}, h('span', { class: 'mono' }, short(id))),
    h('td', {}, String(product.members.length)),
    h('td', {}, String(product.items.length)),
    h('td', {}, product.membership_revision_no === null ? '—' : `r${product.membership_revision_no}`),
    h('td', {}, dotDateTime(product.created_at)),
  );
  return tr;
}

function memberGroup(source) {
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
      h('h4', {}, `${source.supplier_key} · ${source.source_product_id}`),
      source.facts_status
        ? chip(source.facts_status === 'CONFIRMED' ? '원천 확정' : '원천 확인 필요', source.facts_status === 'CONFIRMED' ? 'good' : 'warn')
        : chip('현재 리비전 없음', 'warn'),
    ),
    kv('원천 리비전', short(source.source_revision_id)),
    ...facts,
    images
      ? h(
          'div',
          { class: 'kv', 'data-fact': 'images', 'data-status': images.status ?? 'NONE' },
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
  if (images.representative_sha256) {
    shown.push(`대표 ${short(images.representative_sha256)} · ${images.included}/${images.references}`);
  } else if (images.status === 'CONFIRMED') {
    shown.push(`${images.included}/${images.references}`);
  }
  if (images.status !== 'CONFIRMED') shown.push(factValue({ key: 'images', status: images.status }));
  return shown;
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
    return fragment(head, workspace(ctx.params.get('product'), ctx.navigate));
  },
};

function workspace(initialProduct, navigateTo) {
  // Page state only: nothing here is a second copy of product truth, and nothing outlives a reload.
  const state = {
    query: null,
    cursors: [null], // the cursor of every page visited; the last one names the current page
    listSeq: 0,
    productId: null,
    detailSeq: 0,
    detail: null,
    chosen: new Set(),
    targetSeq: 0,
    draftSeq: 0,
  };

  const search = h('input', {
    type: 'search',
    name: 'q',
    maxlength: '100',
    placeholder: '상품명, 상품 ID, 공급처 상품번호로 검색하세요...',
    'aria-label': '통합DB 검색',
    autocomplete: 'off',
  });
  const toolbar = h(
    'form',
    { class: 'db-toolbar panel', role: 'search', 'data-role': 'db-search' },
    search,
    h('button', { type: 'submit', class: 'btn blue' }, '검색'),
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
        h('tr', {}, ...['상품명 · 원천', '상품 ID', '구성원', '품목', '멤버십', '생성'].map((label) => h('th', {}, label))),
      ),
      listBody,
    ),
    h('div', { class: 'db-pager' }, pageLabel, first, previous, next),
  );
  const detailPanel = h('aside', { class: 'panel db-detail', 'data-role': 'product-detail' });
  let lastPage = null;

  function renderIdleDetail() {
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
        ? page.products.map((row) => listRow(row, state, selectProduct))
        : [h('tr', {}, h('td', { class: 'table-empty', colspan: '6' }, state.query ? '검색 결과가 없습니다.' : '운영 중인 상품이 없습니다.'))]),
    );
  }

  async function loadList() {
    const seq = ++state.listSeq;
    const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
    if (state.query) params.set('q', state.query);
    const cursor = state.cursors.at(-1);
    if (cursor) params.set('cursor', cursor);
    listPanel.setAttribute('aria-busy', 'true');
    try {
      const page = await getJson(`${PRODUCTS}?${params}`);
      if (seq !== state.listSeq) return; // a newer list request superseded this one
      renderList(page);
    } catch (error) {
      if (seq !== state.listSeq) return;
      listBody.replaceChildren(
        h('tr', {}, h('td', { class: 'table-empty', colspan: '6', 'data-reason': errorCode(error) ?? '' }, copy(errorCode(error) ?? String(error.message ?? error)))),
      );
    } finally {
      if (seq === state.listSeq) listPanel.removeAttribute('aria-busy');
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
    detailPanel.dataset.product = id;
    detailPanel.dataset.state = 'loading';
    detailPanel.replaceChildren(
      withHelp(h('h3', {}, '상품 상세 정보'), DETAIL_HELP),
      h('div', { class: 'mini' }, `상품 ${short(id)} 상세를 불러오는 중입니다.`),
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
      'aria-label': `품목 ${short(item.item_id)} 선택`,
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
      h('td', {}, composition(item), h('span', { class: 'mini' }, short(item.item_id))),
      h(
        'td',
        {},
        binding ? BINDING_LABEL[binding.binding_kind] ?? binding.binding_kind : '연결 없음',
        binding ? h('span', { class: 'mini' }, `구성원 ${short(binding.group_member_id)}`) : null,
      ),
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
    withHelp(h('h4', {}, '등록 초안'), DRAFT_HELP),
    h('div', { class: 'form-row' }, h('label', { for: 'draft-target' }, '판매 계정'), targetSelect),
    h('div', { class: 'form-row' }, h('label', { for: 'draft-shape' }, '리스팅 형태'), shapeSelect),
    h('div', { class: 'detail-actions' }, draftButton),
    draftResult,
  );
  let targets = [];
  let drafting = false;

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
          `${marketplaceLabel(target.marketplace_key)} · ${short(target.marketplace_account_id)} · ` +
            (target.unavailable_reason ? copy(target.unavailable_reason) : `정책 ${short(target.policy_revision)}`),
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
      { 'data-pin-item': pin.item_id, 'data-pricing-snapshot': pin.pricing_snapshot_id },
      h('td', {}, item ? composition(item) : short(pin.item_id)),
      h('td', { class: 'mono' }, short(pin.pricing_snapshot_id)),
      h('td', {}, `${pin.final_sale_price_krw.toLocaleString('ko-KR')}원`),
      h('td', {}, BASIS_LABEL[pin.price_basis] ?? pin.price_basis),
      h('td', {}, OUTCOME_LABEL[pin.pricing_outcome] ?? pin.pricing_outcome),
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
      toast('등록 초안 생성', `초안 ${short(created.draft_id)} · 품목 ${created.items.length}개`);
      // The Draft exists either way; only the Product it was made from may show it.
      if (seq !== state.draftSeq || state.productId !== id || created.product_group_id !== id) return;
      const items = new Map(product.items.map((item) => [item.item_id, item]));
      draftResult.dataset.state = 'created';
      draftResult.dataset.draft = created.draft_id;
      draftResult.replaceChildren(
        h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 초안 생성됨'), chip(`초안 ${short(created.draft_id)}`, 'good')),
        kv('초안', created.draft_id),
        kv('대상', `${marketplaceLabel(created.marketplace_key)} · ${short(created.marketplace_account_id)}`),
        kv('리스팅 형태', SHAPE_LABEL[created.listing_shape] ?? created.listing_shape),
        kv('정책 리비전', short(created.policy_revision)),
        kv('멤버십 리비전', short(created.membership_revision_id)),
        h(
          'table',
          { class: 'table' },
          h('thead', {}, h('tr', {}, ...['품목', '가격 스냅샷', '판매가', '가격 근거', 'M4'].map((label) => h('th', {}, label)))),
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
      draftResult.replaceChildren(
        h('div', { class: 'note', 'data-reason': code ?? '' }, copy(code ?? String(error.message ?? error))),
        ...Object.entries(unpriced).map(([itemId, reasons]) =>
          h(
            'div',
            { class: 'mini', 'data-unpriced-item': itemId },
            `${short(itemId)}: ${reasons.map((reason) => reason.code).join(', ')}`,
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
    selectionBar.replaceChildren(
      h('b', {}, `선택 상품 ${short(id)}`),
      h('span', { class: 'mini' }, `품목 ${state.chosen.size}개 선택`),
    );
    checkButton.disabled = state.chosen.size === 0 || detail.selection_unavailable_reason !== null;
    targetResult.replaceChildren();
    delete targetResult.dataset.state;
    // A changed choice is a new command: an earlier answer no longer describes it.
    draftResult.replaceChildren();
    delete draftResult.dataset.state;
    delete draftResult.dataset.draft;
    renderDraftButton();
  }

  function renderDetail() {
    const detail = state.detail;
    const product = detail.product;
    const selection = new Map(detail.item_selection.map((entry) => [entry.item_id, entry]));
    detailPanel.dataset.product = product.product_group_id;
    detailPanel.dataset.state = 'ready';
    detailPanel.dataset.membership = product.membership_revision_id ?? '';
    detailPanel.replaceChildren(
      withHelp(h('h3', {}, '상품 상세 정보'), DETAIL_HELP),
      h(
        'div',
        { class: 'detail-title' },
        h('div', {}, h('b', { class: 'mono' }, product.product_group_id), h('div', { class: 'mini' }, `생성 ${dotDateTime(product.created_at)}`)),
        chip(GROUP_STATUS[product.status] ?? product.status, product.status === 'ACTIVE' ? 'good' : 'warn'),
      ),
      h(
        'div',
        { class: 'detail-group' },
        kv('멤버십 리비전', product.membership_revision_no === null ? '없음' : `r${product.membership_revision_no} · ${short(product.membership_revision_id)}`),
        kv('확정 구성원', `${product.members.length}개`),
        product.retired_at ? kv('보관 시각', dotDateTime(product.retired_at)) : null,
      ),
      ...detail.member_sources.map(memberGroup),
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
      selectionBar,
      h('div', { class: 'detail-actions' }, checkButton),
      targetResult,
      draftPanel,
    );
    renderSelection();
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
        h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 대상 확인됨'), chip(`품목 ${target.items.length}개`, 'good')),
        kv('상품', short(target.product_group_id)),
        kv('멤버십 리비전', `r${target.membership_revision_no}`),
        ...target.items.map((item) => h('div', { class: 'mini', 'data-target-item': item.item_id }, `수량 ${item.quantity} · ${short(item.item_id)}`)),
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
      targetResult.replaceChildren(
        h('div', { class: 'note', 'data-reason': code ?? '' }, copy(code ?? String(error.message ?? error))),
        reload,
      );
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
  loadList();
  // The targets are the owners' accounts, bindings and policies: read once for this page.
  getJson(DRAFT_TARGETS).then(renderTargets, (error) => {
    targetSelect.replaceChildren(h('option', { value: '' }, copy(errorCode(error) ?? String(error.message ?? error))));
    renderDraftButton();
  });
  if (initialProduct) selectProduct(initialProduct);
  return h('div', { class: 'db-workspace' }, toolbar, h('div', { class: 'db-layout' }, listPanel, detailPanel));
}
