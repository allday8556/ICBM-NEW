// 수집 사실 (Collect Truth Inspector): one stored ProductFactsRevision, read back as the server holds
// it (GET /api/v1/collect/revisions/{id}). Every status, value and evidence entry is the revision's
// own; the page counts what it was handed and decides nothing — no confidence, no verdict, and no
// way to change a fact. A field the source did not state reads 없음, and one it stated but could not
// be read from the page alone reads 확인 필요, exactly as recorded.

import { getJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { withHelp } from '../core/help.js';

const REVISIONS = '/api/v1/collect/revisions';

const FIELD_LABEL = {
  original_name: '상품명',
  prices: '가격',
  options: '옵션',
  images: '이미지',
  stock: '재고',
  shipping: '배송',
  minimum_sale_price: '최저판매가',
  quantity_tiers: '수량별 가격',
  brand: '브랜드',
  manufacturer: '제조사',
  origin: '원산지',
  notice: '상품정보고시',
  detail_description: '상세설명',
};
const LEVEL_LABEL = { CORE: '필수', COVERAGE: '보조' };
const STATUS = {
  CONFIRMED: ['확정', 'good'],
  ABSENT: ['없음', null],
  REVIEW_REQUIRED: ['확인 필요', 'warn'],
};
const STATUS_ORDER = ['CONFIRMED', 'ABSENT', 'REVIEW_REQUIRED'];
const ROLE_LABEL = { REPRESENTATIVE: '대표', DETAIL: '상세' };
const DISPOSITION_LABEL = { INCLUDED: '포함', EXCLUDED: '제외', UNRESOLVED: '미해결' };
const AVAILABILITY_LABEL = { ON_SALE: '판매 중', SOLD_OUT: '품절' };
const TRANSPORT_LABEL = { DIRECT_URL: '직접 URL', EXTENSION: '확장 수집' };
const VALUE_LIMIT = 160;
const HELP =
  '저장된 원천 리비전의 필드별 상태·값·근거를 그대로 보여줍니다. 화면은 판정하거나 값을 바꾸지 않습니다. ' +
  '확인 필요는 페이지에 근거가 있지만 페이지만으로 읽을 수 없었던 필드, 없음은 페이지가 밝히지 않은 필드입니다.';

function statusChip(status) {
  const [label, tone] = STATUS[status] ?? [status, null];
  return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-field-status': status }, label);
}

function won(amount) {
  return `${Number(amount).toLocaleString('ko-KR')}원`;
}

function bytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function clip(text) {
  return text.length > VALUE_LIMIT ? `${text.slice(0, VALUE_LIMIT)}…` : text;
}

// The stored canonical value, worded. Unknown shapes are shown as the stored JSON itself.
function valueText(valueJson) {
  if (valueJson === null || valueJson === undefined) return '—';
  let value;
  try {
    value = JSON.parse(valueJson);
  } catch {
    return clip(valueJson);
  }
  if (value === null) return '—';
  if (typeof value !== 'object') return clip(String(value));
  if (typeof value.text === 'string') return clip(value.text);
  if (Array.isArray(value.prices)) return clip(value.prices.map((p) => `${p.label} ${won(p.amount_krw)}`).join(' · '));
  if (typeof value.amount_krw === 'number') return `${value.label ?? ''} ${won(value.amount_krw)}`.trim();
  // A tier is the source's own (quantity, total price) pair; it is never divided into a unit price.
  if (Array.isArray(value.tiers)) return clip(value.tiers.map((t) => `${t.quantity}개 ${won(t.total_price_krw)}`).join(' · '));
  if (typeof value.policy_text === 'string') return clip(value.policy_text);
  if (typeof value.availability === 'string') return AVAILABILITY_LABEL[value.availability] ?? value.availability;
  if (Array.isArray(value.axes)) return `옵션 축 ${value.axes.length}개 · 구성 ${(value.configurations ?? []).length}개`;
  if (Array.isArray(value.items)) return `${value.items.length}개 항목`;
  if (Array.isArray(value.references)) return `참조 ${value.references.length}개`;
  return clip(JSON.stringify(value));
}

function evidenceTable(evidence) {
  if (!evidence.length) return h('div', { class: 'note' }, '기록된 근거가 없습니다.');
  return h(
    'table',
    { class: 'table evidence-table', 'data-role': 'evidence' },
    h('thead', {}, h('tr', {}, ...['종류', '위치', '관찰값', '정규화', '상태', '근거 지문'].map((label) => h('th', {}, label)))),
    h(
      'tbody',
      {},
      ...evidence.map((entry) =>
        h(
          'tr',
          { 'data-evidence-kind': entry.kind },
          h('td', { class: 'mono' }, entry.kind),
          h('td', { class: 'mono' }, entry.locator),
          h('td', {}, entry.observed ?? '—'),
          h('td', {}, entry.normalized ?? '—'),
          h('td', {}, statusChip(entry.status)),
          h('td', { class: 'mono', title: entry.digest }, entry.digest.slice(0, 12)),
        ),
      ),
    ),
  );
}

function fieldRows(field) {
  const label = FIELD_LABEL[field.key] ?? field.key;
  const detail = h(
    'tr',
    { class: 'evidence-row', 'data-evidence-for': field.key, hidden: true },
    h('td', { colspan: '4' }, evidenceTable(field.evidence)),
  );
  const toggle = h(
    'button',
    { type: 'button', class: 'btn', 'data-action': 'toggle-evidence', 'aria-expanded': 'false' },
    `근거 ${field.evidence.length}`,
  );
  toggle.addEventListener('click', () => {
    detail.hidden = !detail.hidden;
    toggle.setAttribute('aria-expanded', String(!detail.hidden));
  });
  const row = h(
    'tr',
    { 'data-field': field.key, 'data-status': field.status, 'data-level': field.level },
    h('td', {}, h('b', {}, label), ' ', h('span', { class: 'mini' }, LEVEL_LABEL[field.level] ?? field.level)),
    h('td', {}, statusChip(field.status)),
    h('td', { 'data-role': 'field-value' }, field.status === 'CONFIRMED' ? valueText(field.value_json) : '—'),
    h('td', {}, toggle),
  );
  return [row, detail];
}

function imageReason(image) {
  return [image.issue, image.target_refusal, image.exclusion].filter(Boolean).join(' · ') || '—';
}

function imagesTable(images) {
  if (!images.length) return null;
  return h(
    'table',
    { class: 'table', 'data-role': 'image-refs' },
    h('thead', {}, h('tr', {}, ...['역할', '순서', '호스트', '상태', '판정', '사유', '크기'].map((label) => h('th', {}, label)))),
    h(
      'tbody',
      {},
      ...images.map((image) =>
        h(
          'tr',
          { 'data-image-role': image.role, 'data-status': image.status, 'data-disposition': image.disposition ?? '' },
          h('td', {}, ROLE_LABEL[image.role] ?? image.role),
          h('td', {}, String(image.ordinal)),
          h('td', { class: 'mono', title: image.locator ?? '' }, image.host),
          h('td', {}, statusChip(image.status)),
          h('td', {}, image.disposition ? DISPOSITION_LABEL[image.disposition] ?? image.disposition : '—'),
          h('td', { class: 'mono' }, imageReason(image)),
          h('td', {}, image.asset ? `${image.asset.width}×${image.asset.height} · ${bytes(image.asset.byte_size)}` : '—'),
        ),
      ),
    ),
  );
}

function counts(fields) {
  const tally = Object.fromEntries(STATUS_ORDER.map((status) => [status, 0]));
  fields.forEach((field) => {
    tally[field.status] = (tally[field.status] ?? 0) + 1;
  });
  return h(
    'div',
    { class: 'supplier-actions', 'data-role': 'field-counts' },
    ...STATUS_ORDER.map((status) => {
      const [label, tone] = STATUS[status];
      return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-count-status': status, 'data-count': String(tally[status]) }, `${label} ${tally[status]}`);
    }),
  );
}

function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value));
}

// The revision of a RECORDED run, as one block. A failed read shows the server's reason and nothing
// in its place.
export async function collectFactsBlock(revisionId, run, errorCopy) {
  const holder = h('section', { class: 'collect-facts', 'data-role': 'collect-facts', 'data-revision': revisionId });
  let revision;
  try {
    revision = await getJson(`${REVISIONS}/${encodeURIComponent(revisionId)}`);
  } catch (error) {
    const code = error?.error?.code ?? null;
    holder.dataset.state = 'error';
    holder.append(h('div', { class: 'note', 'data-reason': code ?? '' }, errorCopy(code, error?.error?.message)));
    return holder;
  }
  holder.dataset.state = 'ready';
  const included = revision.images.filter((image) => image.disposition === 'INCLUDED').length;
  holder.append(fragment(
    h('div', { class: 'supplier-head-row' }, withHelp(h('h4', { class: 'panel-title' }, '수집 사실'), HELP)),
    counts(revision.fields),
    kv('추출 규칙', revision.extractor_revision),
    run.transport_kind ? kv('수집 경로', TRANSPORT_LABEL[run.transport_kind] ?? run.transport_kind) : null,
    h(
      'div',
      { class: 'kv', 'data-role': 'image-count', 'data-included': String(included), 'data-total': String(revision.images.length) },
      h('span', {}, '이미지'),
      h('b', {}, `포함 ${included} / 전체 ${revision.images.length}`),
    ),
    revision.fingerprints_intact ? null : h('div', { class: 'note', 'data-reason': 'FINGERPRINTS_NOT_INTACT' }, '저장된 지문이 다시 계산한 값과 다릅니다.'),
    h(
      'table',
      { class: 'table', 'data-role': 'field-summary' },
      h('thead', {}, h('tr', {}, ...['필드', '상태', '값', '근거'].map((label) => h('th', {}, label)))),
      h('tbody', {}, ...revision.fields.flatMap(fieldRows)),
    ),
    imagesTable(revision.images),
  ));
  return holder;
}
