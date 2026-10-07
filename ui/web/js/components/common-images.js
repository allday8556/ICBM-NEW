// 공급처 공통 이미지 (A-NEXT2a): one supplier's common images as Issue #219's owner holds them.
//
// The owner (app/stages/products/common_images.py) detects the candidates and remembers every
// decision; this component only reads its list, previews each file through the owner's own
// read-only bytes route (Issue #231) and hands the operator's BLOCK or KEEP to the owner's decide
// route. The page decides no verdict, counts no product and never shows a file the owner did not
// list. A candidate no operator has decided is REVIEW, and the owner treats it as blocked.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';

const COMMON = '/api/v1/products/supplier-common-images';
const OPERATOR = 'operator';
const VERDICT = {
  BLOCK: ['차단', 'bad'],
  KEEP: ['유지', 'good'],
  REVIEW: ['확인 필요', 'warn'],
};
const VERDICT_ORDER = ['REVIEW', 'BLOCK', 'KEEP'];
const ERROR_COPY = {
  PRODUCTS_COMMON_IMAGE_SUPPLIER_UNKNOWN: '공통 이미지를 관리하지 않는 공급처입니다.',
  PRODUCTS_COMMON_IMAGE_SUPPLIER_SYNTHETIC: '합성 공급처의 공통 이미지는 원래 공급처에서 정합니다.',
  PRODUCTS_COMMON_IMAGE_UNSEEN: '이 공급처가 보여 준 적 없는 파일입니다.',
  PRODUCTS_COMMON_IMAGE_SHA_INVALID: '파일 지문 형식이 올바르지 않습니다.',
  PRODUCTS_COMMON_IMAGE_VERDICT_INVALID: '차단 또는 유지만 정할 수 있습니다.',
  COLLECT_ASSET_UNKNOWN: '저장된 이미지 파일이 없습니다.',
};
const HELP =
  '같은 공급처의 여러 상품 상세에 반복해서 나온 이미지(배송 안내, 브랜드 배너 등)입니다. 확인 필요는 아직 아무도 정하지 않은 후보로, 정하기 전까지 상품 이미지에서 빠집니다. 차단은 계속 빼고, 유지는 상품 이미지로 씁니다.';

function errorText(error) {
  const code = error?.error?.code ?? (error instanceof ApiError ? error.status : null);
  const copy = ERROR_COPY[code] ?? error?.error?.message ?? error?.message ?? String(error);
  return code ? `${copy} (${code})` : copy;
}

function verdictChip(verdict) {
  const [label, tone] = VERDICT[verdict] ?? [verdict, null];
  return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-verdict': verdict }, label);
}

function imageUrl(supplierKey, sha256) {
  return `${COMMON}/${encodeURIComponent(supplierKey)}/${encodeURIComponent(sha256)}/image`;
}

// The verdict counts of the owner's list, as the supplier card's one line.
function tally(images) {
  const counts = Object.fromEntries(VERDICT_ORDER.map((verdict) => [verdict, 0]));
  images.forEach((image) => {
    counts[image.verdict] = (counts[image.verdict] ?? 0) + 1;
  });
  return counts;
}

function tallyChips(counts) {
  return VERDICT_ORDER.map((verdict) => {
    const [label, tone] = VERDICT[verdict];
    return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-count-verdict': verdict, 'data-count': String(counts[verdict]) }, `${label} ${counts[verdict]}`);
  });
}

// The supplier card's line: the owner's counts, read when the card is drawn.
export function commonImagesSummary(supplier) {
  const value = h('b', { class: 'common-image-counts' }, '—');
  const holder = h('div', { class: 'kv', 'data-role': 'common-image-summary', 'data-supplier': supplier.supplier_key }, h('span', {}, '공통 이미지'), value);
  getJson(`${COMMON}/${encodeURIComponent(supplier.supplier_key)}`)
    .then((listed) => {
      const counts = tally(listed.images);
      holder.dataset.state = 'ready';
      value.replaceChildren(...tallyChips(counts));
    })
    .catch((error) => {
      holder.dataset.state = 'error';
      holder.dataset.reason = error?.error?.code ?? '';
      value.replaceChildren(h('span', { class: 'mini' }, errorText(error)));
    });
  return holder;
}

// The owner previews only a file the supplier has shown in a product's CONFIRMED detail images; a
// file no product shows yet (an owner-seeded one, say) is not asked for.
function preview(supplierKey, sha256, productCount) {
  const frame = h('div', { class: 'common-image-thumb', 'data-role': 'common-image-preview' });
  if (!productCount) {
    frame.dataset.state = 'unseen';
    frame.append(h('span', { class: 'mini no-data' }, '아직 이 공급처 상품에서 본 적 없는 파일 · 미리보기 데이터 없음'));
    return frame;
  }
  const image = h('img', { alt: `공통 이미지 ${sha256.slice(0, 8)}`, loading: 'lazy', src: imageUrl(supplierKey, sha256) });
  image.addEventListener('error', () => {
    frame.dataset.state = 'error';
    frame.replaceChildren(h('span', { class: 'mini' }, '미리보기를 불러오지 못했습니다'));
  });
  frame.append(image);
  return frame;
}

// The owner's current decision for one file, as recorded. Earlier decisions are kept by the owner
// but no read returns them, so the line names the decision's own number and who made it.
function decisionLine(image) {
  if (!image.decision) {
    return h('div', { class: 'mini', 'data-role': 'common-image-decision', 'data-decided': 'false' }, '아직 정한 사람이 없습니다 · 정하기 전까지 차단으로 취급');
  }
  const { revision_no: revision, decided_by: by, reason } = image.decision;
  // Revision 0 is the owner's own seed (Issue #219), in force until an operator decides.
  if (revision === 0) {
    return h('div', { class: 'mini', 'data-role': 'common-image-decision', 'data-decided': 'true', 'data-revision': '0' }, `초기 지정 · ${by}${reason ? ` · ${reason}` : ''}`);
  }
  return h(
    'div',
    { class: 'mini', 'data-role': 'common-image-decision', 'data-decided': 'true', 'data-revision': String(revision) },
    `${revision}번째 결정 · ${by}${reason ? ` · ${reason}` : ''}`,
    revision > 1 ? h('span', { class: 'no-data' }, ' · 이전 결정 기록 데이터 없음') : null,
  );
}

function tile(supplierKey, image, onDecide) {
  const decide = (verdict) =>
    h(
      'button',
      {
        type: 'button',
        class: image.verdict === verdict ? `btn ${verdict === 'BLOCK' ? 'dark' : 'blue'}` : 'btn',
        'data-action': `decide-${verdict.toLowerCase()}`,
        'aria-pressed': String(image.verdict === verdict),
        onclick: (event) => onDecide(image, verdict, event.currentTarget),
      },
      VERDICT[verdict][0],
    );
  return h(
    'div',
    { class: 'common-image-tile', 'data-sha': image.sha256, 'data-verdict': image.verdict, 'data-decided': String(image.decided) },
    preview(supplierKey, image.sha256, image.product_count),
    h('div', { class: 'common-image-head' }, verdictChip(image.verdict), h('span', { class: 'mono mini', title: image.sha256 }, image.sha256.slice(0, 12))),
    h('div', { class: 'kv' }, h('span', {}, '발견 상품'), h('b', { 'data-role': 'product-count' }, `${image.product_count}개`)),
    decisionLine(image),
    h('div', { class: 'supplier-actions' }, decide('BLOCK'), decide('KEEP')),
  );
}

// The supplier's list in a modal. Every decision is the owner's POST; the list is then read again,
// so what the operator sees is always what the owner now holds.
export function openCommonImages(supplier, onChanged = () => {}) {
  const key = supplier.supplier_key;
  const summary = h('div', { class: 'supplier-actions', 'data-role': 'common-image-counts' });
  const rule = h('div', { class: 'mini', 'data-role': 'common-image-rule' });
  const grid = h('div', { class: 'common-image-grid', 'data-role': 'common-image-grid', 'data-supplier': key }, h('div', { class: 'mini' }, '불러오는 중입니다.'));
  let busy = false;

  async function load() {
    try {
      const listed = await getJson(`${COMMON}/${encodeURIComponent(key)}`);
      grid.dataset.state = 'ready';
      rule.textContent = `서로 다른 상품 ${listed.detection_min_products}개 이상의 상세에 나온 파일을 후보로 찾습니다 · 규칙 ${listed.detection_rule_version}`;
      summary.replaceChildren(...tallyChips(tally(listed.images)));
      grid.replaceChildren(
        ...(listed.images.length
          ? listed.images.map((image) => tile(key, image, decide))
          : [h('div', { class: 'note', 'data-role': 'common-image-empty' }, '이 공급처에서 찾은 공통 이미지가 없습니다.')]),
      );
    } catch (error) {
      grid.dataset.state = 'error';
      grid.replaceChildren(h('div', { class: 'note', 'data-reason': error?.error?.code ?? '' }, errorText(error)));
    }
  }

  async function decide(image, verdict, button) {
    if (busy || image.verdict === verdict) return;
    busy = true;
    button.disabled = true;
    try {
      await sendJson('POST', `${COMMON}/${encodeURIComponent(key)}/${encodeURIComponent(image.sha256)}`, { verdict, actor: OPERATOR });
      toast('공통 이미지', `${image.sha256.slice(0, 8)} · ${VERDICT[verdict][0]}(으)로 정했습니다.`);
      onChanged();
    } catch (error) {
      toast('공통 이미지', errorText(error));
    } finally {
      busy = false;
      await load();
    }
  }

  openModal({
    eyebrow: 'Issue #219 · 공급처 공통 이미지',
    title: `${supplier.display_name} 공통 이미지`,
    help: HELP,
    body: fragment(h('div', { class: 'common-image-meta' }, summary, rule), grid),
  });
  load();
}
