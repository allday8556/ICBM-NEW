// 카테고리 메타데이터 검토 (Gate 1 G1-B; ADR-0015 §3). The operator-reviewed category metadata of one
// marketplace × taxonomy revision × category, recorded from reviewed evidence. SmartStore's
// official leaf catalog is synchronized explicitly and supplies the selectable category identity.
//
// The page renders what the server holds and sends what the operator typed. It computes no
// revision, fingerprint, review time or validity: the server validates the whole revision, creates
// its identity, fingerprint and review provenance, refuses an AI suggestion recorded as reviewed,
// and refuses a save that is invalid, unchanged or made against a revision that moved since it was
// read. After every save the page re-reads the server.
//
// 2026-10-16 seller fixes: the notice type is chosen from the marketplace's documented types by
// their Korean names (the stored value is the same enum), a new category starts from the server's
// form defaults (no options: 1 item, 1 dimension), and the rules a seller rarely writes are folded
// under 「세부 규칙」. The 확인 완료 tick and the 근거 are still the seller's own: nothing is ever
// marked reviewed for them. The register category picker opens the same form for one category
// (openCategoryMetadataEditor).

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { errorText as plainError } from '../../core/errors.js';
import { closeModal, openModal } from '../../core/modal.js';
import { toast } from '../../core/toast.js';

const BASE = '/api/v1/settings/category-metadata';
const FORM = '/api/v1/settings/category-metadata-form';
const CATALOG = '/api/v1/settings/category-catalog';
const SMARTSTORE_SYNC = '/api/v1/connect/marketplaces/smartstore/categories/sync';
const TITLE = '카테고리 등록 정보';
const ACTOR = 'operator';
const PROVENANCE = [
  ['OPERATOR_CONFIRMED', '직접 확인함 (근거 있음)'],
  ['AI_SUGGESTION', 'AI 제안 (확인 전)'],
];
const ERROR_COPY = {
  CATEGORY_METADATA_AI_NOT_REVIEWABLE: 'AI 제안은 확인 완료로 기록할 수 없습니다. 직접 확인한 뒤 「직접 확인함」으로 저장하세요.',
  CATEGORY_METADATA_INVALID: '입력값을 확인하세요. 잘못된 값이 있어 저장하지 않았습니다.',
  CATEGORY_METADATA_UNSAFE_CONTENT: '입력값에 쓸 수 없는 내용(주소·키 등)이 있어 저장하지 않았습니다.',
  CATEGORY_METADATA_UNCHANGED: '바뀐 값이 없습니다.',
  CATEGORY_METADATA_CURRENT_MOVED: '다른 곳에서 바뀌었습니다. 다시 불러온 뒤 저장하세요.',
  CATEGORY_CATALOG_MISSING: '저장된 네이버 카테고리가 없습니다. 먼저 동기화하세요.',
  CATEGORY_CATALOG_EMPTY: '네이버에서 받은 카테고리가 없습니다. 잠시 후 다시 시도하세요.',
  CATEGORY_TAXONOMY_NOT_CURRENT:
    '등록 정책의 카테고리 체계가 지금 네이버 카테고리 목록과 다릅니다. 설정 › 스마트스토어 › 등록 정책에서 「최신 카테고리 체계로 맞추기」를 누르고 저장하세요.',
  CATEGORY_NOT_CURRENT_LEAF: '지금 네이버 카테고리 목록의 마지막 단계 카테고리가 아닙니다. 카테고리를 다시 고르세요.',
};
// Without the server's form (an older server, a failed read) nothing is offered: the notice type
// stays a typed field and no default is assumed beyond the one valid without options.
const NO_FORM = { notice_types: [], name_max_length: null, options_supported: false, max_options: 1, max_dimensions: 1 };
const NAME_HINT = '네이버 판매자센터 상품 등록 화면에서 확인한 상품명 최대 글자 수를 넣으세요.';
const NOTICE_FIELDS_HINT =
  '스마트스토어는 고른 고시 유형의 네이버 기준 항목이 자동으로 적용됩니다. 보통은 비워 둡니다.';

let sequence = 0;
const forms = new Map();

function errorText(error) {
  return plainError(error, ERROR_COPY);
}

// The form's choices and starting values, read once per marketplace per page.
function metadataForm(marketplaceKey) {
  if (!forms.has(marketplaceKey)) {
    forms.set(
      marketplaceKey,
      getJson(`${FORM}/${encodeURIComponent(marketplaceKey)}`).catch(() => {
        forms.delete(marketplaceKey);
        return NO_FORM;
      }),
    );
  }
  return forms.get(marketplaceKey);
}

function input(label, name, value, { type = 'text', readonly = false, hint = null, placeholder = null } = {}) {
  sequence += 1;
  const id = `category-metadata-${sequence}`;
  const control = h('input', { id, type, autocomplete: 'off', spellcheck: 'false', 'data-meta-field': name, placeholder });
  if (type === 'checkbox') control.checked = Boolean(value);
  else control.value = value ?? '';
  control.readOnly = readonly;
  const note = hint ? h('div', { class: 'mini', 'data-meta-hint': name }, hint) : null;
  const row =
    type === 'checkbox'
      ? h('label', { class: 'kv', for: id }, h('span', {}, label), control)
      : h('div', { class: 'form-row' }, h('label', { for: id }, label), control, note);
  return { control, row };
}

function area(label, name, value, hint = null) {
  sequence += 1;
  const id = `category-metadata-${sequence}`;
  const control = h('textarea', { id, rows: '4', spellcheck: 'false', 'data-meta-field': name });
  control.value = value;
  const note = hint ? h('div', { class: 'mini', 'data-meta-hint': name }, hint) : null;
  return { control, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), control, note) };
}

function select(label, name, options, value) {
  sequence += 1;
  const id = `category-metadata-${sequence}`;
  const control = h(
    'select',
    { id, 'data-meta-field': name },
    options.map(([key, text]) => h('option', { value: key }, text)),
  );
  if (value) control.value = value;
  return { control, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), control) };
}

// 상품정보고시 유형: a choice among the documented types by their Korean names; the value sent is
// the enum the server stores. A stored value the list does not hold is kept and named as such.
// Without choices (another marketplace) it stays a typed field.
function noticeTypeControl(form, value) {
  const choices = form.notice_types ?? [];
  if (!choices.length) return input('상품정보고시 유형 (비우면 없음)', 'notice_type', value);
  const options = [['', '고르지 않음 (고시 없이 저장)'], ...choices.map((choice) => [choice.notice_type, choice.label])];
  if (value && !choices.some((choice) => choice.notice_type === value)) options.push([value, `${value} (목록에 없는 유형)`]);
  return select('상품정보고시 유형', 'notice_type', options, value ?? '');
}

// One rule per line: key | 필수 y/n | 상세페이지 참조 y/n | REVIEW_REQUIRED or BLOCKED | 최대 길이 or -.
// The page only splits what was typed; a token it cannot read is sent as typed, so the server —
// which owns validity — refuses it.
function flag(text) {
  if (text === 'y') return true;
  if (text === 'n') return false;
  return text;
}

function length(text) {
  if (text === '' || text === '-') return null;
  return /^\d+$/.test(text) ? Number(text) : text;
}

function rulesOf(text) {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [key = '', required = '', reference = '', missing = '', max = ''] = line.split('|').map((part) => part.trim());
      return {
        key,
        required: flag(required),
        detail_page_reference_allowed: flag(reference),
        missing_status: missing,
        max_length: length(max),
      };
    });
}

function rulesText(rules) {
  return (rules ?? [])
    .map((rule) =>
      [
        rule.key,
        rule.required ? 'y' : 'n',
        rule.detail_page_reference_allowed ? 'y' : 'n',
        rule.missing_status,
        rule.max_length ?? '-',
      ].join(' | '),
    )
    .join('\n');
}

function reviewLine(current) {
  if (!current) return '기록 없음 · 이 카테고리로는 아직 등록할 수 없습니다';
  if (!current.reviewed) return '확인 전 · 확인 완료로 저장해야 등록할 수 있습니다';
  return `확인 완료 · ${new Date(current.reviewed_at).toLocaleString('ko-KR')}`;
}

function entryRow(entry, onEdit, names = new Map()) {
  const current = entry.current;
  const edit = h('button', { type: 'button', class: 'btn', 'data-action': 'edit-category-metadata' }, '불러와 편집');
  edit.addEventListener('click', () => onEdit(entry));
  return h(
    'div',
    {
      class: 'kv category-metadata-entry',
      'data-metadata-key': `${entry.taxonomy_revision}/${entry.category_id}`,
      'data-meta-reviewed': current ? String(current.reviewed) : 'none',
      'data-meta-history': String(entry.history.length),
    },
    h('span', {}, names.get(entry.category_id) ?? `카테고리 ${entry.category_id}`),
    h(
      'span',
      {
        class: current?.reviewed ? 'chip good' : 'chip warn',
        'data-meta-revision': current ? String(current.revision_no) : '',
        'data-meta-reviewed-by': current?.reviewed_by ?? '',
        'data-meta-fingerprint': current?.content_fingerprint ?? '',
      },
      reviewLine(current),
    ),
    edit,
  );
}

function categorySelect(catalog, value) {
  sequence += 1;
  const id = `category-metadata-${sequence}`;
  const control = h(
    'select',
    { id, 'data-meta-field': 'category_id' },
    catalog.entries.map((item) =>
      h('option', { value: item.category_id }, `${item.whole_category_name} (${item.category_id})`),
    ),
  );
  if (value) control.value = value;
  return { control, row: h('div', { class: 'form-row' }, h('label', { for: id }, '카테고리'), control) };
}

// The editor of one key. `entry` is the server's view of a recorded key (its content may be null:
// a key opened from the register picker before anything was recorded); without one the operator
// picks the key. `options.fromCatalog` says the key is a leaf of the current catalog (the picker's
// choice); `options.compact` leaves out the key rows a seller does not need to see.
function editor(marketplaceKey, entry, catalog, onSaved, form = NO_FORM, options = {}) {
  const content = entry?.content;
  const current = entry?.current;
  const fixed = Boolean(entry);
  const catalogReady = !entry && Boolean(catalog?.taxonomy_revision && catalog.entries?.length);
  const leafKnown = catalogReady || Boolean(options.fromCatalog);
  const taxonomy = input(
    '카테고리 체계 버전',
    'taxonomy_revision',
    entry?.taxonomy_revision ?? catalog?.taxonomy_revision,
    { readonly: fixed || catalogReady },
  );
  const category = catalogReady
    ? categorySelect(catalog, entry?.category_id)
    : input('카테고리 ID', 'category_id', entry?.category_id, { readonly: fixed });
  // A leaf of the current catalog is a leaf, and a leaf is where a product is registered; both
  // stay the seller's to change before the save, and nothing is reviewed until they tick it.
  const leaf = input('리프 카테고리', 'leaf', content ? content.leaf : leafKnown, { type: 'checkbox' });
  if (catalogReady) leaf.control.disabled = true;
  const registrable = input('등록 가능 카테고리', 'registrable', content ? content.registrable : leafKnown, { type: 'checkbox' });
  const nameMax = input(
    '상품명 최대 길이',
    'name_max_length',
    content ? String(content.name_max_length) : form.name_max_length == null ? '' : String(form.name_max_length),
    { hint: content ? null : NAME_HINT, placeholder: '예: 100' },
  );
  const attributes = area('속성 규칙 (한 줄에 하나: 항목 | 필수 y/n | 상세페이지 참조 y/n | 없을 때 REVIEW_REQUIRED(확인 필요) 또는 BLOCKED(등록 불가) | 최대 글자 수 또는 -)', 'attributes', rulesText(content?.attributes));
  const noticeType = noticeTypeControl(form, content?.notice?.notice_type);
  const noticeFields = area(
    '상품정보고시 항목 규칙 (같은 형식)',
    'notice_fields',
    rulesText(content?.notice?.fields),
    (form.notice_types ?? []).length ? NOTICE_FIELDS_HINT : null,
  );
  const optionsSupported = input('옵션 지원', 'options_supported', content ? content.options.options_supported : form.options_supported, { type: 'checkbox' });
  const maxOptions = input('최대 옵션 수', 'max_options', String(content ? content.options.max_options : form.max_options));
  const maxDimensions = input('최대 옵션 단계 수', 'max_dimensions', String(content ? content.options.max_dimensions : form.max_dimensions));
  // A category without options allows exactly one item and one dimension: unticking says so.
  optionsSupported.control.addEventListener('change', () => {
    if (optionsSupported.control.checked) return;
    maxOptions.control.value = '1';
    maxDimensions.control.value = '1';
  });
  const templates = input('필수 템플릿 (쉼표로 구분)', 'required_templates', (content?.required_templates ?? []).join(', '));
  const evidence = input('확인한 근거 (예: 판매자센터 화면, 날짜 · 주소는 넣지 마세요)', 'evidence_reference', current?.evidence_reference ?? '');
  const provenance = select('내용 출처', 'content_provenance', PROVENANCE, current?.content_provenance ?? 'OPERATOR_CONFIRMED');
  const reviewed = input('확인 완료로 저장 (근거를 확인했습니다)', 'reviewed', false, { type: 'checkbox' });

  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-category-metadata' }, '카테고리 정보 저장');
  save.addEventListener('click', async () => {
    save.disabled = true;
    const tax = taxonomy.control.value.trim();
    const cat = category.control.value.trim();
    const notice = noticeType.control.value.trim();
    try {
      const saved = await sendJson('POST', `${BASE}/${marketplaceKey}/${encodeURIComponent(tax)}/${encodeURIComponent(cat)}/revisions`, {
        actor: ACTOR,
        expected_current_revision: current?.metadata_revision ?? null,
        content_provenance: provenance.control.value,
        evidence_reference: evidence.control.value.trim(),
        reviewed: reviewed.control.checked,
        content: {
          leaf: leaf.control.checked,
          registrable: registrable.control.checked,
          name_max_length: length(nameMax.control.value.trim()),
          attributes: rulesOf(attributes.control.value),
          notice: notice ? { notice_type: notice, fields: rulesOf(noticeFields.control.value) } : null,
          options: {
            options_supported: optionsSupported.control.checked,
            max_options: length(maxOptions.control.value.trim()),
            max_dimensions: length(maxDimensions.control.value.trim()),
          },
          required_templates: templates.control.value
            .split(',')
            .map((value) => value.trim())
            .filter(Boolean),
        },
      });
      toast(TITLE, '저장했습니다. 이후 등록 확인에 바로 적용됩니다.');
      await onSaved(saved);
    } catch (error) {
      // Nothing was written: the server refuses a save whole. The typed values stay for a fix.
      toast(TITLE, errorText(error));
      save.disabled = false;
    }
  });

  // The rules a seller rarely writes: open when the recorded revision holds any.
  const advanced = h(
    'details',
    {
      class: 'category-metadata-advanced',
      'data-meta-advanced': 'true',
      open: Boolean(content?.attributes?.length || content?.notice?.fields?.length || content?.required_templates?.length),
    },
    h('summary', {}, '세부 규칙 (속성·고시 항목·템플릿 · 보통은 비워 둡니다)'),
    attributes.row,
    noticeFields.row,
    templates.row,
  );
  return h(
    'div',
    { class: 'category-metadata-editor', 'data-editing': entry ? `${entry.taxonomy_revision}/${entry.category_id}` : 'new' },
    options.compact ? null : taxonomy.row,
    options.compact ? null : category.row,
    leaf.row,
    registrable.row,
    noticeType.row,
    nameMax.row,
    optionsSupported.row,
    maxOptions.row,
    maxDimensions.row,
    advanced,
    evidence.row,
    provenance.row,
    reviewed.row,
    h('div', { class: 'api-action-row' }, save),
  );
}

// 「이 카테고리 등록 정보 준비」 (register category picker): the same form for exactly one
// category of the policy's taxonomy, in a modal. `onSaved` runs after the server recorded it, so
// the caller reads the category again; the seller still ticks 확인 완료 themselves.
export async function openCategoryMetadataEditor({ marketplaceKey, taxonomyRevision, categoryId, categoryName, onSaved }) {
  const [entry, form] = await Promise.all([
    getJson(`${BASE}/${encodeURIComponent(marketplaceKey)}/${encodeURIComponent(taxonomyRevision)}/${encodeURIComponent(categoryId)}`),
    metadataForm(marketplaceKey),
  ]);
  const body = h(
    'div',
    { class: 'category-metadata', 'data-marketplace': marketplaceKey, 'data-metadata-modal': `${taxonomyRevision}/${categoryId}` },
    h('div', { class: 'kv' }, h('span', {}, '카테고리'), h('b', {}, categoryName || `카테고리 번호 ${categoryId}`)),
    editor(
      marketplaceKey,
      entry,
      null,
      async (saved) => {
        closeModal();
        await onSaved?.(saved);
      },
      form,
      { fromCatalog: true, compact: true },
    ),
  );
  openModal({
    eyebrow: '카테고리 등록 정보',
    title: '이 카테고리 등록 정보 준비',
    help: '고시 유형과 상품명 최대 길이를 판매자센터에서 확인해 넣고, 근거를 적은 뒤 「확인 완료로 저장」을 체크하세요. 체크하지 않으면 확인 전으로 저장되어 아직 등록할 수 없습니다.',
    body,
  });
}

export function categoryMetadataPanel(marketplaceKey) {
  const list = h('div', { class: 'category-metadata-list' });
  const form = h('div', { class: 'truth-host' });
  const host = h('div', { class: 'category-metadata', 'data-marketplace': marketplaceKey }, list, form);
  let load = null;
  let catalog = null;
  let formView = NO_FORM;
  const edit = (entry) => form.replaceChildren(editor(marketplaceKey, entry, catalog, load, formView));
  const fresh = h('button', { type: 'button', class: 'btn', 'data-action': 'new-category-metadata' }, '새 카테고리 기록');
  fresh.addEventListener('click', () => edit(null));
  const catalogQuery = h('input', {
    type: 'search',
    autocomplete: 'off',
    placeholder: '카테고리명·경로·ID 검색',
    'data-category-catalog-query': 'true',
  });
  const search = h('button', { type: 'button', class: 'btn', 'data-action': 'search-category-catalog' }, '카테고리 검색');
  search.addEventListener('click', async () => {
    search.disabled = true;
    try {
      const query = encodeURIComponent(catalogQuery.value.trim());
      catalog = await getJson(`${CATALOG}/${marketplaceKey}?query=${query}&limit=500`);
      edit(null);
      toast(TITLE, `검색 결과 ${catalog.entries.length}개를 불러왔습니다.`);
    } catch (error) {
      toast(TITLE, errorText(error));
    } finally {
      search.disabled = false;
    }
  });
  const sync = h('button', { type: 'button', class: 'btn', 'data-action': 'sync-category-catalog' }, '네이버 카테고리 동기화');
  sync.addEventListener('click', async () => {
    sync.disabled = true;
    try {
      catalog = await sendJson('POST', SMARTSTORE_SYNC);
      toast(TITLE, `네이버 카테고리 ${catalog.total}개를 저장했습니다.`);
      await load();
    } catch (error) {
      toast(TITLE, errorText(error));
    } finally {
      sync.disabled = false;
    }
  });
  load = async () => {
    try {
      const [view, catalogView, offered] = await Promise.all([
        getJson(`${BASE}/${marketplaceKey}`),
        getJson(`${CATALOG}/${marketplaceKey}?limit=500`),
        metadataForm(marketplaceKey),
      ]);
      catalog = catalogView;
      formView = offered;
      const catalogLine = catalog.taxonomy_revision
        ? `네이버 카테고리 ${catalog.total}개를 받아 두었습니다.`
        : '저장된 네이버 카테고리가 없습니다. 「네이버 카테고리 동기화」를 누르세요.';
      list.replaceChildren(
        h(
          'div',
          { class: 'note', 'data-category-catalog-state': catalog.taxonomy_revision ? 'ready' : 'missing', 'data-taxonomy': catalog.taxonomy_revision ?? '' },
          catalogLine,
        ),
        ...(view.entries.length
          ? view.entries.map((entry) => entryRow(entry, edit, new Map((catalog.entries ?? []).map((item) => [item.category_id, item.whole_category_name]))))
          : [h('div', { class: 'note', 'data-reason': 'CATEGORY_METADATA_MISSING' }, '기록된 카테고리 정보가 없습니다. 카테고리 정보를 기록해야 그 카테고리로 등록할 수 있습니다.')]),
        h('div', { class: 'api-action-row' }, catalogQuery, search, sync, fresh),
      );
      form.replaceChildren();
    } catch (error) {
      list.replaceChildren(h('span', { class: 'chip warn' }, `확인 불가 · ${errorText(error)}`));
    }
  };
  load();
  return host;
}
