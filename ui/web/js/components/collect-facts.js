// 수집 사실 (Collect Truth Inspector): one stored ProductFactsRevision, read back as the server holds
// it (GET /api/v1/collect/revisions/{id}). Every status, value and evidence entry is the revision's
// own; the page counts what it was handed and decides nothing — no confidence, no verdict, and no
// way to change a fact. A field the source did not state reads 없음, and one it stated but could not
// be read from the page alone reads 확인 필요, exactly as recorded.

import { getJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { withHelp } from '../core/help.js';

const REVISIONS = '/api/v1/collect/revisions';
const PRODUCTS = '/api/v1/products';
// What the image auto-selection rule (the Product DB's owner, Issue #219) says about each source
// image of an Item bound to this very revision, worded. Read from the Item's own image-candidates
// preview; the page decides nothing and shows an unknown note as itself.
const NOTE_COPY = {
  REPRESENTATIVE: '대표 이미지',
  ADDITIONAL: '추가 이미지',
  DETAIL: '상세 본문',
  DUPLICATE_BYTES: '중복 파일이라 제외',
  COMMON_IMAGE_BLOCKED: '공통 이미지(차단)라 제외',
  COMMON_IMAGE_UNDECIDED: '공통 이미지(확인 필요)라 제외',
  OVER_LIMIT: '마켓 장수 한도를 넘어 제외',
  OTHER_SLOT: '다른 칸',
};
const COMMON_NOTES = ['COMMON_IMAGE_BLOCKED', 'COMMON_IMAGE_UNDECIDED'];

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
  sales_channels: '판매채널',
};
// ADR-0031 §5: a supplier's sales-channel restriction, in Korean.
const MARKET_LABEL = { smartstore: '스마트스토어', coupang: '쿠팡' };
const CHANNEL_SCOPE_LABEL = {
  ALL_ALLOWED: '모든 마켓 판매 가능',
  CLOSED_MALL_ONLY: '폐쇄몰 전용 (오픈마켓 판매 불가)',
};
const LEVEL_LABEL = { CORE: '필수', COVERAGE: '보조' };
const STATUS = {
  CONFIRMED: ['확정', 'good'],
  ABSENT: ['없음', null],
  REVIEW_REQUIRED: ['확인 필요', 'warn'],
};
const STATUS_ORDER = ['CONFIRMED', 'ABSENT', 'REVIEW_REQUIRED'];
const ROLE_LABEL = { REPRESENTATIVE: '대표', DETAIL: '상세' };
// The image acceptance vocabulary the server records for each reference (ADR-0010 §9; Issue #52
// ruling 5723016554), worded. Each word names a code the revision already holds; the page judges
// nothing, and a code it has no word for is shown as the code itself.
const DISPOSITION_LABEL = { INCLUDED: '포함', EXCLUDED: '제외', UNRESOLVED: '확인 필요' };
const EXCLUSION_COPY = {
  SOURCE_AUTHORED_NON_HTTPS: '원천이 http 주소로 적은 이미지 (보안 연결 아님)',
};
const ISSUE_COPY = {
  BAD_HOST: '허용되지 않은 호스트',
  BAD_CONTENT_TYPE: '이미지가 아닌 응답',
  OVERSIZE: '파일 크기 제한 초과',
  BUDGET_EXHAUSTED: '이번 수집의 요청·용량 한도를 다 써서 가져오지 않음',
  UNSUPPORTED_FORMAT: '지원하지 않는 이미지 형식',
  FETCH_FAILED: '가져오기 실패',
};
// Why the transport's own target check refused the reference before anything was sent.
const REFUSAL_COPY = {
  UNPARSEABLE: '주소를 읽을 수 없음',
  WHITESPACE: '주소에 공백이 있음',
  FRAGMENT: '주소에 # 조각이 있음',
  NOT_ABSOLUTE: '완전한 주소가 아님',
  NON_HTTPS: 'http 주소 (보안 연결 아님)',
  UNSUPPORTED_SCHEME: '지원하지 않는 주소 형식',
  CREDENTIALS_PRESENT: '주소에 계정 정보가 있음',
  NON_STANDARD_PORT: '표준이 아닌 포트',
  HOST_NOT_ALLOWLISTED: '허용되지 않은 이미지 호스트',
  PATH_NOT_ALLOWED: '허용되지 않은 경로',
  QUERY_NOT_ALLOWED: '허용되지 않은 주소 매개변수(?)',
};
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

// The stored canonical value, worded for the summary row. The summary may be shortened; the full
// stored value is always one click away in the field's detail row (fullValue below).
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
  if (typeof value.scope === 'string') {
    if (value.scope === 'LISTED') {
      return clip(value.forbidden.map((key) => `${MARKET_LABEL[key] ?? key} 판매 불가`).join(' · '));
    }
    return CHANNEL_SCOPE_LABEL[value.scope] ?? value.scope;
  }
  if (typeof value.policy_text === 'string') return clip(value.policy_text);
  if (typeof value.availability === 'string') return AVAILABILITY_LABEL[value.availability] ?? value.availability;
  if (Array.isArray(value.axes)) {
    if (!value.axes.length) return '옵션 없음';
    const axes = value.axes.map((axis) => `${axis.name}: ${axis.values.join(', ')}`).join(' / ');
    return clip(`${axes}${value.configurations?.length ? ` · 구성 ${value.configurations.length}개` : ''}`);
  }
  if (Array.isArray(value.items)) return clip(value.items.map((item) => `${item.label}: ${item.text}`).join(' · '));
  if (Array.isArray(value.references)) {
    return clip(value.references.map((ref) => `${ROLE_LABEL[ref.role] ?? ref.role} ${ref.ordinal} ${DISPOSITION_LABEL[ref.disposition] ?? ref.status}`).join(' · '));
  }
  return clip(JSON.stringify(value));
}

// The whole stored value, exactly as the revision holds it: never shortened.
function fullValue(valueJson) {
  let text = valueJson;
  try {
    text = JSON.stringify(JSON.parse(valueJson), null, 2);
  } catch {
    text = valueJson;
  }
  return h('div', { class: 'field-value-full' }, h('div', { class: 'mini' }, '저장된 값 (전체)'), h('pre', { class: 'mono', 'data-role': 'field-value-full' }, text));
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

// A field's stored value and evidence open on demand. The detail row exists only while it is open:
// a collapsed row leaves no hidden state in the page (Gate 3 visual contract, ADR-0018 §9).
function fieldRows(field) {
  const label = FIELD_LABEL[field.key] ?? field.key;
  const toggle = h(
    'button',
    { type: 'button', class: 'btn', 'data-action': 'toggle-evidence', 'aria-expanded': 'false' },
    field.status === 'CONFIRMED' ? `값·근거 ${field.evidence.length}` : `근거 ${field.evidence.length}`,
  );
  let detail = null;
  toggle.addEventListener('click', () => {
    if (detail) {
      detail.remove();
      detail = null;
    } else {
      detail = h(
        'tr',
        { class: 'evidence-row', 'data-evidence-for': field.key },
        h(
          'td',
          { colspan: '4' },
          field.status === 'CONFIRMED' && field.value_json !== null ? fullValue(field.value_json) : null,
          evidenceTable(field.evidence),
        ),
      );
      row.after(detail);
    }
    toggle.setAttribute('aria-expanded', String(Boolean(detail)));
  });
  const row = h(
    'tr',
    { 'data-field': field.key, 'data-status': field.status, 'data-level': field.level },
    h('td', {}, h('b', {}, label), ' ', h('span', { class: 'mini' }, LEVEL_LABEL[field.level] ?? field.level)),
    h('td', {}, statusChip(field.status)),
    h('td', { 'data-role': 'field-value' }, field.status === 'CONFIRMED' ? valueText(field.value_json) : '—'),
    h('td', {}, toggle),
  );
  return [row];
}

// The recorded reason, worded, with the codes kept beside it. An excluded reference reads its
// closed exclusion row; otherwise the target refusal, which is more exact than FETCH_FAILED, and
// then the issue. Nothing is inferred from the URL.
function imageReason(image) {
  const codes = [image.exclusion, image.target_refusal, image.issue].filter(Boolean);
  if (!codes.length) return '—';
  const words = image.exclusion
    ? EXCLUSION_COPY[image.exclusion] ?? image.exclusion
    : image.target_refusal
      ? `가져오기 전 거절: ${REFUSAL_COPY[image.target_refusal] ?? image.target_refusal}`
      : ISSUE_COPY[image.issue] ?? image.issue;
  return fragment(
    h('span', { 'data-role': 'image-reason' }, words),
    ' ',
    h('span', { class: 'mini mono', 'data-role': 'image-reason-codes' }, codes.join(' · ')),
  );
}

// The auto-selection notes of the Product DB Item bound to exactly this revision, through the
// owner's own reads: the run's product → its Items → the first Item whose current binding is this
// revision → that Item's image-candidates preview. No Item bound to this revision means nothing is
// shown: the supplier's common-image list is never shown as this product's exclusions.
async function selectionNotes(revisionId, product) {
  if (!product || product.state !== 'MATERIALIZED' || !product.productGroupId) return { state: 'NOT_MATERIALIZED' };
  try {
    const group = await getJson(`${PRODUCTS}/${encodeURIComponent(product.productGroupId)}`);
    const item = (group.items ?? []).find((candidate) => candidate.current_binding?.provenance_revision_id === revisionId);
    if (!item) return { state: 'NOT_BOUND' };
    const found = await getJson(`${PRODUCTS}/items/${encodeURIComponent(item.item_id)}/image-candidates`);
    if (found.source_revision_id !== revisionId) return { state: 'NOT_BOUND' };
    const notes = new Map((found.auto_selection?.notes ?? []).map((n) => [`${n.source_role}:${n.source_ordinal}`, n.note]));
    return { state: 'READY', itemId: item.item_id, notes, blocked: found.auto_selection?.blocked ?? null };
  } catch (error) {
    return { state: 'ERROR', code: error?.error?.code ?? null, message: error?.error?.message ?? null };
  }
}

function selectionSummary(selection, errorCopy) {
  const holder = h('div', { class: 'kv', 'data-role': 'common-image-summary', 'data-selection': selection.state });
  if (selection.state === 'READY') {
    const values = [...selection.notes.values()];
    const blocked = values.filter((note) => note === 'COMMON_IMAGE_BLOCKED').length;
    const undecided = values.filter((note) => note === 'COMMON_IMAGE_UNDECIDED').length;
    holder.dataset.blocked = String(blocked);
    holder.dataset.undecided = String(undecided);
    holder.append(
      h('span', {}, '공통 이미지 제외'),
      h(
        'b',
        {},
        selection.blocked
          ? `자동 선택 안 함 · ${selection.blocked}`
          : `${blocked + undecided}개 (차단 ${blocked} · 확인 필요 ${undecided}) · 통합DB 품목 ${selection.itemId.slice(0, 8)} 기준`,
      ),
    );
    return holder;
  }
  const words = {
    NOT_MATERIALIZED: '통합DB 상품에 반영된 뒤 표시됩니다',
    NOT_BOUND: '이 리비전에 묶인 통합DB 품목이 없어 표시하지 않습니다',
  };
  holder.append(
    h('span', {}, '공통 이미지 제외'),
    h('b', {}, selection.state === 'ERROR' ? errorCopy(selection.code, selection.message) : words[selection.state]),
  );
  return holder;
}

// The image references open on demand, like a field's evidence, and exist only while open.
function imagesBlock(images, selection) {
  if (!images.length) return null;
  const holder = h('div', { 'data-role': 'image-refs-holder' });
  const toggle = h(
    'button',
    { type: 'button', class: 'btn', 'data-action': 'toggle-images', 'aria-expanded': 'false' },
    `이미지 참조 ${images.length}개`,
  );
  let table = null;
  toggle.addEventListener('click', () => {
    if (table) {
      table.remove();
      table = null;
    } else {
      table = imagesTable(images, selection);
      holder.append(table);
    }
    toggle.setAttribute('aria-expanded', String(Boolean(table)));
  });
  holder.append(h('div', { class: 'supplier-actions' }, toggle));
  return holder;
}

function imagesTable(images, selection) {
  const ready = selection?.state === 'READY';
  const headers = ['역할', '순서', '호스트', '상태', '판정', '사유', '크기', ...(ready ? ['자동 선택'] : [])];
  return h(
    'table',
    { class: 'table', 'data-role': 'image-refs' },
    h('thead', {}, h('tr', {}, ...headers.map((label) => h('th', {}, label)))),
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
          h(
            'td',
            {
              'data-exclusion': image.exclusion ?? '',
              'data-refusal': image.target_refusal ?? '',
              'data-issue': image.issue ?? '',
            },
            imageReason(image),
          ),
          h('td', {}, image.asset ? `${image.asset.width}×${image.asset.height} · ${bytes(image.asset.byte_size)}` : '—'),
          ready ? noteCell(selection.notes.get(`${image.role}:${image.ordinal}`)) : null,
        ),
      ),
    ),
  );
}

function noteCell(note) {
  return h(
    'td',
    { 'data-note': note ?? '', 'data-common': String(COMMON_NOTES.includes(note)) },
    note ? NOTE_COPY[note] ?? note : '—',
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

// One read of a RECORDED run's revision and of its bound Item's auto-selection notes, shared by the
// 수집 미리보기 and the 수집 사실 blocks. A failed read keeps the server's reason.
export async function readRevisionFacts(revisionId, product = null) {
  let revision;
  try {
    revision = await getJson(`${REVISIONS}/${encodeURIComponent(revisionId)}`);
  } catch (error) {
    return { revisionId, error: { code: error?.error?.code ?? null, message: error?.error?.message ?? null } };
  }
  return { revisionId, revision, selection: await selectionNotes(revisionId, product) };
}

// The revision of a RECORDED run, as one block. A failed read shows the server's reason and nothing
// in its place.
export function collectFactsBlock(read, run, errorCopy) {
  const holder = h('section', { class: 'panel collect-facts', 'data-role': 'collect-facts', 'data-revision': read.revisionId });
  if (read.error) {
    holder.dataset.state = 'error';
    holder.append(h('div', { class: 'note', 'data-reason': read.error.code ?? '' }, errorCopy(read.error.code, read.error.message)));
    return holder;
  }
  const { revision, selection } = read;
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
    selectionSummary(selection, errorCopy),
    revision.fingerprints_intact ? null : h('div', { class: 'note', 'data-reason': 'FINGERPRINTS_NOT_INTACT' }, '저장된 지문이 다시 계산한 값과 다릅니다.'),
    h(
      'table',
      { class: 'table', 'data-role': 'field-summary' },
      h('thead', {}, h('tr', {}, ...['필드', '상태', '값', '근거'].map((label) => h('th', {}, label)))),
      h('tbody', {}, ...revision.fields.flatMap(fieldRows)),
    ),
    imagesBlock(revision.images, selection),
  ));
  return holder;
}

// ---------------------------------------------------------------- 수집 미리보기 (v29 side-detail)
//
// v29's 수집 미리보기 beside the work list, filled only from what the run and its revision hold. A
// slot the system has no source for keeps its v29 place and reads 데이터 없음 (owner decision
// 2026-10-07, option 가); nothing is estimated or filled from demo content.
const NO_DATA = '데이터 없음';

function previewField(read, key) {
  return read?.revision?.fields.find((field) => field.key === key) ?? null;
}

function optionWords(valueJson) {
  let value;
  try {
    value = JSON.parse(valueJson);
  } catch {
    return valueText(valueJson);
  }
  if (Array.isArray(value?.configurations) && value.configurations.length) return `${value.configurations.length}개`;
  if (Array.isArray(value?.axes) && !value.axes.length) return '옵션 없음';
  return valueText(valueJson);
}

// A field's stored value, its recorded status when it has none, or 데이터 없음 when the revision
// holds no such field (or there is no revision yet).
function previewRow(label, field, words = valueText) {
  const text = !field ? NO_DATA : field.status === 'CONFIRMED' ? words(field.value_json) : STATUS[field.status]?.[0] ?? field.status;
  return h(
    'div',
    { class: 'kv', 'data-preview': label, 'data-status': field?.status ?? 'NO_DATA' },
    h('span', {}, label),
    h('b', { class: field ? null : 'no-data' }, text),
  );
}

function noDataRow(label) {
  return h('div', { class: 'kv', 'data-preview': label, 'data-status': 'NO_DATA' }, h('span', {}, label), h('b', { class: 'no-data' }, NO_DATA));
}

// The representative image is shown through the bound Item's own image read once the Product DB
// holds it; before that the slot keeps its place empty.
function previewThumb(read) {
  const empty = () => h('div', { class: 'hero-thumb', 'data-role': 'preview-thumb', 'data-thumb': 'none', title: '대표 이미지 데이터 없음' }, h('span', { 'aria-hidden': 'true' }, '🖼'));
  const representative = read?.revision?.images.find((image) => image.role === 'REPRESENTATIVE' && image.disposition === 'INCLUDED' && image.asset);
  if (!representative || read.selection?.state !== 'READY') return empty();
  const holder = h('div', { class: 'hero-thumb', 'data-role': 'preview-thumb', 'data-thumb': 'item-image' });
  const image = h('img', {
    alt: '대표 이미지',
    src: `${PRODUCTS}/items/${encodeURIComponent(read.selection.itemId)}/images/${encodeURIComponent(representative.asset.sha256)}`,
  });
  image.addEventListener('error', () => holder.replaceWith(empty()));
  holder.append(image);
  return holder;
}

export function collectPreviewBlock(run, read, { supplier, chips = [], errorCopy }) {
  const holder = h('div', { 'data-role': 'preview-body', 'data-run': run.collection_run_id, 'data-outcome': run.outcome });
  const name = previewField(read, 'original_name');
  const images = read?.revision?.images ?? null;
  const detailImages = images ? images.filter((image) => image.role === 'DETAIL' && image.disposition === 'INCLUDED').length : null;
  const statusChips = read?.revision
    ? ['prices', 'options', 'images'].map((key) => {
        const field = previewField(read, key);
        if (!field) return null;
        const [label, tone] = STATUS[field.status] ?? [field.status, null];
        return h('span', { class: tone ? `chip ${tone}` : 'chip', 'data-preview-status': key }, `${FIELD_LABEL[key]} ${label}`);
      })
    : [];
  holder.append(fragment(
    h(
      'div',
      { class: 'detail-title' },
      previewThumb(read),
      h(
        'div',
        { class: 'preview-name' },
        h('b', { 'data-role': 'preview-name', class: name?.status === 'CONFIRMED' ? null : 'no-data' }, name?.status === 'CONFIRMED' ? valueText(name.value_json) : name ? STATUS[name.status]?.[0] ?? name.status : NO_DATA),
        h('div', { class: 'mini' }, read?.revision ? `${read.revision.supplier_key} · ${read.revision.source_product_id}` : run.source_url),
      ),
    ),
    read?.error ? h('div', { class: 'note', 'data-reason': read.error.code ?? '' }, errorCopy(read.error.code, read.error.message)) : null,
    h(
      'div',
      { class: 'detail-group' },
      h('div', { class: 'kv', 'data-preview': '공급처' }, h('span', {}, '공급처'), h('b', {}, supplier)),
      previewRow('도매가', previewField(read, 'prices')),
      previewRow('배송비', previewField(read, 'shipping')),
      previewRow('최저판매가', previewField(read, 'minimum_sale_price')),
      previewRow('옵션 수', previewField(read, 'options'), optionWords),
      detailImages === null ? noDataRow('상세이미지') : h('div', { class: 'kv', 'data-preview': '상세이미지' }, h('span', {}, '상세이미지'), h('b', {}, `${detailImages}장`)),
    ),
    h('div', { class: 'detail-group preview-chips' }, ...chips, ...statusChips),
    h('div', { class: 'note', 'data-role': 'ai-note' }, h('b', {}, 'AI 학습 노트'), h('br', {}), NO_DATA),
  ));
  return holder;
}
