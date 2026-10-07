// 공급처 공통 이미지 (A-NEXT2a). The server owns detection, verdicts and decision history.
// This surface only reads that truth, renders bytes through the read-only preview route, and
// sends the operator's explicit BLOCK/KEEP command. It never contacts a supplier or provider.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { toast } from '../../core/toast.js';

const BASE = '/api/v1/products/supplier-common-images';
const TITLE = '공급처 공통 이미지';
const DEFAULT_SUPPLIER = 'kmretail';
const ACTOR = 'operator';

const VERDICTS = {
  BLOCK: { label: '차단', className: 'chip bad' },
  KEEP: { label: '유지', className: 'chip good' },
  REVIEW: { label: '확인 필요', className: 'chip warn' },
};

function errorText(error) {
  const body = error?.error;
  return body?.message ? `${body.message} (${body.code})` : String(error?.message ?? error);
}

function path(supplier, sha = null) {
  const root = `${BASE}/${encodeURIComponent(supplier)}`;
  return sha ? `${root}/${encodeURIComponent(sha)}` : root;
}

function historyLine(decision) {
  if (!decision) return '판정 이력 없음 · 운영자 결정 전';
  if (decision.revision_no === 0) return '기본 판정 · 공급처 공통 이미지 owner';
  return `판정 이력 ${decision.revision_no}회 · 최신 기록`;
}

function preview(supplier, image) {
  const picture = h('img', {
    src: `${path(supplier, image.sha256)}/image`,
    alt: `${supplier} 공통 이미지 ${image.sha256.slice(0, 12)}`,
    loading: 'lazy',
  });
  const fallback = h('div', { class: 'common-image-preview-fallback', hidden: true }, '미리보기 없음');
  picture.addEventListener('error', () => {
    picture.hidden = true;
    fallback.hidden = false;
  });
  return h('div', { class: 'common-image-preview' }, picture, fallback);
}

function decisionDetails(decision) {
  if (!decision) return h('p', { class: 'mini common-image-history' }, historyLine(decision));
  return h(
    'div',
    { class: 'common-image-history' },
    h('b', {}, historyLine(decision)),
    h('span', { class: 'mini' }, `판정자 ${decision.decided_by}`),
    decision.reason ? h('span', { class: 'mini' }, `사유 ${decision.reason}`) : null,
  );
}

function imageCard(supplier, image, reload) {
  const verdict = VERDICTS[image.verdict] ?? { label: image.verdict, className: 'chip' };
  const reason = h('input', {
    type: 'text',
    maxlength: '200',
    autocomplete: 'off',
    placeholder: '판정 사유 (선택)',
    'aria-label': `${image.sha256.slice(0, 12)} 판정 사유`,
    'data-common-image-reason': image.sha256,
  });
  const actions = h('div', { class: 'common-image-actions' });

  for (const [next, label, variant] of [
    ['BLOCK', '차단', 'bad'],
    ['KEEP', '유지', 'good'],
  ]) {
    const action = h(
      'button',
      {
        type: 'button',
        class: `btn common-image-decision ${variant}`,
        'data-action': `decide-common-image-${next.toLowerCase()}`,
      },
      label,
    );
    action.addEventListener('click', async () => {
      for (const button of actions.querySelectorAll('button')) button.disabled = true;
      try {
        const value = reason.value.trim();
        await sendJson('POST', path(supplier, image.sha256), {
          verdict: next,
          actor: ACTOR,
          reason: value || null,
        });
        toast(TITLE, `${label} 판정을 기록했습니다.`);
        await reload();
      } catch (error) {
        toast(TITLE, errorText(error));
        for (const button of actions.querySelectorAll('button')) button.disabled = false;
      }
    });
    actions.append(action);
  }

  return h(
    'article',
    {
      class: 'common-image-card',
      'data-common-image': image.sha256,
      'data-common-image-verdict': image.verdict,
    },
    preview(supplier, image),
    h(
      'div',
      { class: 'common-image-body' },
      h(
        'div',
        { class: 'common-image-title' },
        h('span', { class: verdict.className }, verdict.label),
        h('b', {}, `발견 상품 ${image.product_count}개`),
      ),
      h('code', { class: 'common-image-sha', title: image.sha256 }, image.sha256),
      decisionDetails(image.decision),
      reason,
      actions,
    ),
  );
}

function summary(view) {
  const counts = { BLOCK: 0, KEEP: 0, REVIEW: 0 };
  for (const image of view.images) counts[image.verdict] = (counts[image.verdict] ?? 0) + 1;
  return h(
    'div',
    { class: 'common-image-summary', 'data-common-image-rule': view.detection_rule_version },
    h('span', { class: 'chip' }, `전체 ${view.images.length}`),
    h('span', { class: 'chip bad' }, `차단 ${counts.BLOCK}`),
    h('span', { class: 'chip good' }, `유지 ${counts.KEEP}`),
    h('span', { class: 'chip warn' }, `확인 필요 ${counts.REVIEW}`),
    h('span', { class: 'mini' }, `탐지 기준 ${view.detection_min_products}개 상품`),
  );
}

export function supplierCommonImagesPanel(initialSupplier) {
  const supplier = h('input', {
    type: 'text',
    value: initialSupplier || DEFAULT_SUPPLIER,
    autocomplete: 'off',
    spellcheck: 'false',
    'aria-label': '공급처 키',
    'data-common-image-supplier': 'true',
  });
  const loadButton = h('button', { type: 'button', class: 'btn', 'data-action': 'load-common-images' }, '목록 불러오기');
  const status = h('div', { class: 'common-image-status' });
  const list = h('div', { class: 'common-image-grid' });
  const host = h(
    'div',
    { class: 'supplier-common-images', 'data-supplier': supplier.value },
    h(
      'div',
      { class: 'common-image-intro note' },
      '여러 상품에서 반복된 공급처 이미지를 확인합니다. 확인 필요 이미지는 차단 상태로 유지되며, 운영자가 차단 또는 유지를 결정합니다.',
    ),
    h(
      'div',
      { class: 'common-image-toolbar' },
      h('label', {}, h('span', {}, '공급처 키'), supplier),
      loadButton,
    ),
    status,
    list,
  );

  let load = null;
  load = async () => {
    const key = supplier.value.trim();
    if (!key) {
      status.replaceChildren(h('span', { class: 'chip warn' }, '공급처 키를 입력하세요'));
      list.replaceChildren();
      return;
    }
    loadButton.disabled = true;
    host.dataset.supplier = key;
    status.replaceChildren(h('span', { class: 'chip' }, '불러오는 중'));
    try {
      const view = await getJson(path(key));
      status.replaceChildren(summary(view));
      list.replaceChildren(
        ...(view.images.length
          ? view.images.map((image) => imageCard(view.supplier_key, image, load))
          : [h('div', { class: 'note common-image-empty' }, '탐지되거나 판정된 공통 이미지가 없습니다.')]),
      );
    } catch (error) {
      status.replaceChildren(h('span', { class: 'chip warn' }, `확인 불가 · ${errorText(error)}`));
      list.replaceChildren();
    } finally {
      loadButton.disabled = false;
    }
  };
  loadButton.addEventListener('click', load);
  supplier.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') load();
  });
  load();
  return host;
}
