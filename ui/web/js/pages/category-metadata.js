// 카테고리 메타데이터 검토 (Gate 1 G1-B; ADR-0015 §3). The operator-reviewed category metadata of one
// marketplace × taxonomy revision × category, recorded from reviewed evidence. No provider category
// endpoint is adopted; nothing here calls a marketplace.
//
// The page renders what the server holds and sends what the operator typed. It computes no
// revision, fingerprint, review time or validity: the server validates the whole revision, creates
// its identity, fingerprint and review provenance, refuses an AI suggestion recorded as reviewed,
// and refuses a save that is invalid, unchanged or made against a revision that moved since it was
// read. After every save the page re-reads the server.

import { getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { toast } from '../core/toast.js';

const BASE = '/api/v1/settings/category-metadata';
const TITLE = '카테고리 메타데이터';
const ACTOR = 'operator';
const MISSING = ['REVIEW_REQUIRED', 'BLOCKED'];
const PROVENANCE = [
  ['OPERATOR_CONFIRMED', '운영자 확인 (검토 근거 있음)'],
  ['AI_SUGGESTION', 'AI 제안 (검토 전)'],
];

let sequence = 0;

function errorText(error) {
  const body = error?.error;
  return body?.message ? `${body.message} (${body.code})` : String(error?.message ?? error);
}

function input(label, name, value, { type = 'text', readonly = false } = {}) {
  sequence += 1;
  const id = `category-metadata-${sequence}`;
  const control = h('input', { id, type, autocomplete: 'off', spellcheck: 'false', 'data-meta-field': name });
  if (type === 'checkbox') control.checked = Boolean(value);
  else control.value = value ?? '';
  control.readOnly = readonly;
  const row =
    type === 'checkbox'
      ? h('label', { class: 'kv', for: id }, h('span', {}, label), control)
      : h('div', { class: 'form-row' }, h('label', { for: id }, label), control);
  return { control, row };
}

function area(label, name, value) {
  sequence += 1;
  const id = `category-metadata-${sequence}`;
  const control = h('textarea', { id, rows: '4', spellcheck: 'false', 'data-meta-field': name });
  control.value = value;
  return { control, row: h('div', { class: 'form-row' }, h('label', { for: id }, label), control) };
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
  if (!current) return '기록 없음 · 사전검사는 CATEGORY_METADATA_MISSING';
  const at = (value) => new Date(value).toLocaleString('ko-KR');
  const review = current.reviewed
    ? `검토 완료 · ${current.reviewed_by} · ${at(current.reviewed_at)}`
    : '미검토 · 사전검사는 CATEGORY_METADATA_UNREVIEWED';
  return `현재 리비전 #${current.revision_no} · ${review} · 근거 ${current.evidence_reference} · 지문 ${current.content_fingerprint.slice(0, 12)}`;
}

function entryRow(entry, onEdit) {
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
    h('span', {}, `${entry.taxonomy_revision} · ${entry.category_id}`),
    h('span', { class: current?.reviewed ? 'chip good' : 'chip warn' }, reviewLine(current)),
    edit,
  );
}

function editor(marketplaceKey, entry, onSaved) {
  const content = entry?.content;
  const current = entry?.current;
  const fixed = Boolean(entry);
  const taxonomy = input('카테고리 체계 리비전', 'taxonomy_revision', entry?.taxonomy_revision, { readonly: fixed });
  const category = input('카테고리 ID', 'category_id', entry?.category_id, { readonly: fixed });
  const leaf = input('리프 카테고리', 'leaf', content?.leaf, { type: 'checkbox' });
  const registrable = input('등록 가능 카테고리', 'registrable', content?.registrable, { type: 'checkbox' });
  const nameMax = input('상품명 최대 길이', 'name_max_length', content ? String(content.name_max_length) : '');
  const attributes = area('속성 규칙 (한 줄에: 키 | 필수 y/n | 상세참조 y/n | REVIEW_REQUIRED·BLOCKED | 최대길이 또는 -)', 'attributes', rulesText(content?.attributes));
  const noticeType = input('상품정보고시 유형 (비우면 없음)', 'notice_type', content?.notice?.notice_type);
  const noticeFields = area('고시 필드 규칙 (같은 형식)', 'notice_fields', rulesText(content?.notice?.fields));
  const optionsSupported = input('옵션 지원', 'options_supported', content?.options?.options_supported, { type: 'checkbox' });
  const maxOptions = input('최대 옵션 수', 'max_options', content ? String(content.options.max_options) : '');
  const maxDimensions = input('최대 옵션 차원', 'max_dimensions', content ? String(content.options.max_dimensions) : '');
  const templates = input('필수 템플릿 (쉼표로 구분)', 'required_templates', (content?.required_templates ?? []).join(', '));
  const evidence = input('검토 근거 참조 (URL·자격증명 불가)', 'evidence_reference', current?.evidence_reference ?? '');
  const provenance = select('내용 출처', 'content_provenance', PROVENANCE, current?.content_provenance ?? 'OPERATOR_CONFIRMED');
  const reviewed = input('검토 완료로 기록 (근거를 확인했습니다)', 'reviewed', false, { type: 'checkbox' });

  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'save-category-metadata' }, '메타데이터 리비전 저장');
  save.addEventListener('click', async () => {
    save.disabled = true;
    const tax = taxonomy.control.value.trim();
    const cat = category.control.value.trim();
    try {
      await sendJson('POST', `${BASE}/${marketplaceKey}/${encodeURIComponent(tax)}/${encodeURIComponent(cat)}/revisions`, {
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
          notice: noticeType.control.value.trim()
            ? { notice_type: noticeType.control.value.trim(), fields: rulesOf(noticeFields.control.value) }
            : null,
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
      toast(TITLE, '새 메타데이터 리비전을 저장했습니다. 이후 등록 사전검사는 이 리비전으로 다시 평가됩니다.');
      await onSaved();
    } catch (error) {
      // Nothing was written: the server refuses a save whole. The typed values stay for a fix.
      toast(TITLE, errorText(error));
      save.disabled = false;
    }
  });

  return h(
    'div',
    { class: 'category-metadata-editor', 'data-editing': entry ? `${entry.taxonomy_revision}/${entry.category_id}` : 'new' },
    taxonomy.row,
    category.row,
    leaf.row,
    registrable.row,
    nameMax.row,
    attributes.row,
    noticeType.row,
    noticeFields.row,
    optionsSupported.row,
    maxOptions.row,
    maxDimensions.row,
    templates.row,
    evidence.row,
    provenance.row,
    reviewed.row,
    h('div', { class: 'api-action-row' }, save),
  );
}

export function categoryMetadataPanel(marketplaceKey) {
  const list = h('div', { class: 'category-metadata-list' });
  const form = h('div', { class: 'truth-host' });
  const host = h('div', { class: 'category-metadata', 'data-marketplace': marketplaceKey }, list, form);
  let load = null;
  const edit = (entry) => form.replaceChildren(editor(marketplaceKey, entry, load));
  const fresh = h('button', { type: 'button', class: 'btn', 'data-action': 'new-category-metadata' }, '새 카테고리 기록');
  fresh.addEventListener('click', () => edit(null));
  load = async () => {
    try {
      const view = await getJson(`${BASE}/${marketplaceKey}`);
      list.replaceChildren(
        ...(view.entries.length
          ? view.entries.map((entry) => entryRow(entry, edit))
          : [h('div', { class: 'note' }, '기록된 카테고리 메타데이터가 없습니다 · 모든 카테고리의 사전검사는 CATEGORY_METADATA_MISSING입니다')]),
        h('div', { class: 'api-action-row' }, fresh),
      );
      form.replaceChildren();
    } catch (error) {
      list.replaceChildren(h('span', { class: 'chip warn' }, `확인 불가 · ${errorText(error)}`));
    }
  };
  load();
  return host;
}
