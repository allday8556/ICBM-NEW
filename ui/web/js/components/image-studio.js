// Image Studio (ADR-0041 §6, §7; I5): the registration editor's 「이미지 자동번역」 buttons and the
// Studio panel that translates the Chinese or Japanese text inside product images into Korean.
//
// The buttons keep the place the AI placeholder decision gave them (Issue #219 comment
// 6054956408). While the server's image_translate capability is NOT_CONFIGURED — no OCR engine,
// no translation task or no approved AI profile — they stay inert, marked 준비 중, and their
// tooltip names what is missing. Only when it reads READY do they open the panel.
//
// Everything the panel shows is the server's: the images an Item uses, the OCR regions with their
// flags and defaults, the candidate on offer (a stale one is never offered), the current edit, a
// stateless preview and the selection. It never judges a region itself. A run starts only on the
// operator's 「번역 시작」 for the images ticked (D4, at most 30). 「모두 적용」 includes every
// unflagged region and never a flagged one; a flagged region needs its own 「확인함」 before
// 「적용」. 「적용」 saves the edit, renders it, reads the rendering again for source text left over
// (I6: a refusal names such a region `원문 글자 남음`, which then needs its own 「확인함」) and
// records the translated image (the M4 image owner);
// 「확인 완료」 records the operator's PASS on exactly that image and then uses it in place of the
// source (USE_DERIVED); 「되돌리기」 uses the source again. Nothing is deleted.

import { ApiError, getJson, sendForBlob, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';

const BASE = '/api/v1/image-studio';
const OPERATOR = 'operator';
const LABEL = '이미지 자동번역';
const POLL_MS = 500;
const POLL_LIMIT = 240; // two minutes
const LANGUAGE_COPY = { zh: '중국어', ja: '일본어' };
const ROLE_COPY = { REPRESENTATIVE: '원본 대표', DETAIL: '원본 상세' };
const OUTPUT_COPY = { REPRESENTATIVE: '대표', ADDITIONAL: '추가', DETAIL: '상세' };
const FLAG_COPY = {
  LOW_CONFIDENCE: ['인식 신뢰도 낮음', 'warn'],
  NUMBER_UNIT_DROPPED: ['숫자 · 단위 빠짐', 'bad'],
  CONTACT_DETAIL: ['연락처', 'warn'],
  SCRIPT_MISMATCH: ['다른 언어', 'warn'],
  COMPLEX_BACKGROUND: ['복잡한 배경', 'warn'],
  LEFTOVER_SOURCE_TEXT: ['원문 글자 남음', 'warn'],
  LAYOUT_OVERFLOW: ['칸에 넘침', 'bad'],
  MISSING_GLYPHS: ['그릴 수 없는 글자', 'bad'],
};
// The flags a region is confirmed for one by one (IT-05); the other two block 「적용」 outright.
// LEFTOVER_SOURCE_TEXT is found only by 「적용」 reading the rendered image again (I6).
const CONFIRM_FLAGS = new Set(['LOW_CONFIDENCE', 'NUMBER_UNIT_DROPPED', 'CONTACT_DETAIL', 'SCRIPT_MISMATCH', 'COMPLEX_BACKGROUND', 'LEFTOVER_SOURCE_TEXT']);
const STALE_COPY = { input: '입력', prompt: '프롬프트', policy: '정책', provider: 'AI 공급자', schema: '결과 형식' };
const ERROR_COPY = {
  IMAGE_TRANSLATE_NOT_CONFIGURED: '이미지 번역이 준비되지 않았습니다.',
  IMAGE_EDIT_MOVED: '다른 곳에서 먼저 저장했습니다. 다시 불러온 뒤 적용하세요.',
  IMAGE_STUDIO_FLAGGED_UNCONFIRMED: '확인이 필요한 영역을 하나씩 확인해야 적용할 수 있습니다.',
  IMAGE_OCR_NOT_CONFIGURED: 'OCR 엔진이 없어 번역본에 남은 원문 글자를 확인할 수 없습니다.',
  IMAGE_STUDIO_RENDER_INCOMPLETE: '번역문이 칸에 다 들어가지 않았습니다. 문구를 줄이세요.',
  IMAGE_STUDIO_NOTHING_INCLUDED: '번역을 적용할 영역이 없습니다.',
  IMAGE_STUDIO_QA_REQUIRED: '번역본을 확인한 뒤에 사용할 수 있습니다.',
  IMAGE_STUDIO_SELECTION_MISSING: '먼저 대표이미지 단계에서 이미지 쓰임을 저장하세요.',
  IMAGE_STUDIO_IMAGE_NOT_PLACED: '이 이미지는 지금 사용 안 함입니다. 먼저 쓰임을 고르세요.',
  IMAGE_STUDIO_REVISION_NOT_CURRENT: '편집이 바뀌었습니다. 다시 불러온 뒤 적용하세요.',
  IMAGE_STUDIO_RENDERER_CHANGED: '렌더러가 바뀌었습니다. 다시 적용하세요.',
};

let capabilityRead = null;

// The capability, read once per page.
function capability() {
  capabilityRead ??= getJson(`${BASE}/capability`).catch(() => ({ status: 'UNAVAILABLE', missing: [] }));
  return capabilityRead;
}

function errorText(error) {
  if (error instanceof ApiError) return ERROR_COPY[error.error?.code] ?? `${error.message} (${error.error?.code ?? error.status})`;
  return String(error?.message ?? error);
}

// 「이미지 자동번역」 in ② (scope 'selected': the images the operator ticks) or ⑤ (scope 'detail':
// the product's detail images ticked in advance). The slot stays empty until the capability is
// READY and the unit editable: a control that cannot work is omitted, never shown inert.
export function translateButton(unit, { scope, editable }) {
  const slot = h('span', { class: 'studio-slot', 'data-role': 'studio-slot', 'data-studio-slot': scope, 'data-ready': 'pending' });
  capability().then((read) => {
    if (read.status !== 'READY' || !editable || !unit.items.length) {
      slot.dataset.ready = 'false';
      return;
    }
    slot.dataset.ready = 'true';
    const title = scope === 'detail' ? '상세 이미지 속 글자를 한국어로 번역합니다' : '고른 이미지 속 글자를 한국어로 번역합니다';
    slot.append(
      h(
        'button',
        {
          type: 'button',
          class: 'btn ai',
          'data-studio-scope': scope,
          'data-ready': 'true',
          'data-action': 'image-translate',
          title,
          onclick: () => openStudio(unit, { scope, languages: read.languages ?? ['zh', 'ja'], max: read.max_images_per_run ?? 30 }),
        },
        `✨ ${LABEL}`,
      ),
    );
  });
  return slot;
}

function imageUrl(itemId, sha) {
  return `/api/v1/products/items/${encodeURIComponent(itemId)}/images/${sha}`;
}

function stateUrl(itemId, sha, tail = '') {
  return `${BASE}/items/${encodeURIComponent(itemId)}/images/${sha}${tail}`;
}

function selectionChip(selection) {
  if (selection.decision === 'USE_DERIVED') return h('span', { class: 'chip good', 'data-role': 'studio-selection', 'data-decision': 'USE_DERIVED' }, `번역본 사용 중 · ${OUTPUT_COPY[selection.output_role] ?? ''}`);
  if (selection.decision === 'EXCLUDE') return h('span', { class: 'chip', 'data-role': 'studio-selection', 'data-decision': 'EXCLUDE' }, '사용 안 함');
  if (selection.decision === 'USE_SOURCE') return h('span', { class: 'chip info', 'data-role': 'studio-selection', 'data-decision': 'USE_SOURCE' }, `원본 사용 · ${OUTPUT_COPY[selection.output_role] ?? ''}`);
  return h('span', { class: 'chip warn', 'data-role': 'studio-selection', 'data-decision': 'NONE' }, '쓰임 미정');
}

function flagChips(flags) {
  return flags.map((flag) => {
    const [label, tone] = FLAG_COPY[flag] ?? [flag, 'warn'];
    return h('span', { class: `chip ${tone}`, 'data-flag': flag }, label);
  });
}

function blobToDataUrl(blob) {
  // The page's policy admits data: images only, never blob: URLs.
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function waitForJob(jobId, line) {
  for (let i = 0; i < POLL_LIMIT; i += 1) {
    const job = await getJson(`/api/v1/system/jobs/${encodeURIComponent(jobId)}`);
    if (job.state === 'SUCCEEDED' || job.state === 'DEAD') return job;
    line.textContent = '이미지 속 글자를 읽고 번역하는 중…';
    await new Promise((resolve) => window.setTimeout(resolve, POLL_MS));
  }
  throw new Error('번역이 오래 걸리고 있습니다. 잠시 뒤 다시 여세요.');
}

// The panel for one registration unit: its Items' images, the run and one image's review.
export function openStudio(unit, { scope, languages, max }) {
  const items = unit.items;
  let item = items[0];
  let language = languages[0] ?? 'zh';
  let listing = null;
  const ticked = new Set();
  let firstListing = true;

  const itemSelect = h('select', { 'data-role': 'studio-item', 'aria-label': '품목' }, ...items.map((entry) => h('option', { value: entry.item_id }, `품목 ${entry.ordinal}`)));
  itemSelect.hidden = items.length < 2;
  const languageSelect = h('select', { 'data-role': 'studio-language', 'aria-label': '원문 언어' }, ...languages.map((key) => h('option', { value: key }, `${LANGUAGE_COPY[key] ?? key} → 한국어`)));
  const grid = h('div', { class: 'studio-images', 'data-role': 'studio-images' }, h('span', { class: 'mini' }, '이미지를 불러오는 중…'));
  const runLine = h('span', { class: 'mini', 'data-role': 'studio-run-line' });
  const run = h('button', { type: 'button', class: 'btn blue', 'data-action': 'studio-run', disabled: true }, '번역 시작');
  const review = h('div', { class: 'studio-review', 'data-role': 'studio-review' });

  function refreshRun() {
    run.disabled = !ticked.size || ticked.size > max;
    runLine.textContent = ticked.size > max ? `한 번에 ${max}장까지 번역합니다.` : ticked.size ? `${ticked.size}장을 번역합니다. 글자만 보내고 이미지는 보내지 않습니다.` : '번역할 이미지를 고르세요.';
  }

  async function loadImages() {
    listing = await getJson(`${BASE}/items/${encodeURIComponent(item.item_id)}/images`);
    if (firstListing && scope === 'detail') {
      for (const image of listing.images) {
        if ((image.selection.output_role ?? image.role) === 'DETAIL' && image.selection.decision !== 'EXCLUDE') ticked.add(image.sha256);
      }
    }
    firstListing = false;
    grid.replaceChildren(
      ...(listing.images.length
        ? listing.images.map((image) => {
            const pick = h('input', { type: 'checkbox', 'data-role': 'studio-pick', 'aria-label': `${ROLE_COPY[image.role] ?? image.role} ${image.ordinal} 번역` });
            pick.checked = ticked.has(image.sha256);
            pick.addEventListener('change', () => {
              if (pick.checked) ticked.add(image.sha256);
              else ticked.delete(image.sha256);
              refreshRun();
            });
            const open = h('button', { type: 'button', class: 'btn', 'data-action': 'studio-open', disabled: !image.observed }, '검토');
            open.addEventListener('click', () => showReview(image.sha256));
            return h(
              'div',
              { class: 'studio-image', 'data-image': image.sha256, 'data-observed': String(image.observed) },
              h('img', { src: imageUrl(item.item_id, image.sha256), alt: `${ROLE_COPY[image.role] ?? image.role} ${image.ordinal}`, loading: 'lazy' }),
              h('label', { class: 'studio-pick' }, pick, `${ROLE_COPY[image.role] ?? image.role} ${image.ordinal}`),
              h('div', { class: 'row' }, selectionChip(image.selection), image.observed ? h('span', { class: 'chip', 'data-role': 'studio-observed' }, '글자 읽음') : null),
              open,
            );
          })
        : [h('span', { class: 'mini' }, '확정된 원본 이미지가 없습니다.')]),
    );
    refreshRun();
  }

  run.addEventListener('click', async () => {
    run.disabled = true;
    const images = [...ticked];
    try {
      const { job_id: jobId } = await sendJson('POST', `${BASE}/products/${encodeURIComponent(item.product_group_id)}/translations`, { images, source_language: language, actor: OPERATOR });
      const job = await waitForJob(jobId, runLine);
      await loadImages();
      if (job.state === 'DEAD') {
        runLine.textContent = '일부 이미지를 번역하지 못했습니다. 번역된 이미지만 검토할 수 있습니다.';
        toast('일부 이미지를 번역하지 못했습니다', job.last_error_code ?? '');
      } else {
        runLine.textContent = '번역을 마쳤습니다. 이미지마다 검토하세요.';
      }
      const first = listing.images.find((image) => ticked.has(image.sha256) && image.observed);
      if (first) await showReview(first.sha256);
    } catch (error) {
      refreshRun();
      runLine.textContent = `번역하지 못했습니다 · ${errorText(error)}`;
    }
  });

  async function showReview(sha) {
    review.replaceChildren(h('span', { class: 'mini' }, '검토 화면을 불러오는 중…'));
    try {
      const state = await getJson(`${stateUrl(item.item_id, sha)}?source_language=${language}`);
      review.replaceChildren(reviewBody(state));
    } catch (error) {
      review.replaceChildren(h('span', { class: 'chip warn' }, `불러오지 못했습니다 · ${errorText(error)}`));
    }
  }

  function reviewBody(state) {
    const sha = state.source_sha256;
    const saved = new Map((state.edit?.items ?? []).map((entry) => [entry.region_id, entry]));
    const rows = state.regions.map((region) => {
      const edit = saved.get(region.region_id);
      return {
        region,
        text: edit?.text ?? region.suggestion ?? '',
        include: edit ? edit.include : false,
        erase: edit?.erase && edit.erase !== 'NONE' ? edit.erase : region.default_erase,
        confirmed: false,
        flags: region.flags,
        // Flags 「적용」 found on the rendered image (I6): kept until the review reloads.
        found: [],
      };
    });
    let edit = state.edit;
    let expectedSeq = state.expected_seq;
    let previewOk = false;
    let previewing = false;
    let timer = null;
    let sequence = 0;

    const original = h('img', { src: imageUrl(item.item_id, sha), alt: '원본 이미지' });
    const previewImage = h('img', { alt: '번역 미리보기', 'data-role': 'studio-preview-image', hidden: true });
    const previewState = h('span', { class: 'chip', 'data-role': 'studio-preview-state' }, '미리보기 전');
    const candidate = state.candidate;
    const candidateChip = !candidate
      ? h('span', { class: 'chip warn', 'data-role': 'studio-candidate', 'data-status': 'NONE' }, '번역 없음 · 번역 시작을 누르세요')
      : candidate.status === 'FRESH'
        ? h('span', { class: 'chip good', 'data-role': 'studio-candidate', 'data-status': 'FRESH' }, 'AI 번역 제안 · 최신')
        : h('span', { class: 'chip warn', 'data-role': 'studio-candidate', 'data-status': 'STALE' }, `제안이 최신이 아님 · ${candidate.stale_reasons.map((key) => STALE_COPY[key] ?? key).join(', ')} 바뀜 · 다시 번역하세요`);
    const allButton = h('button', { type: 'button', class: 'btn', 'data-action': 'studio-apply-all' }, '모두 적용');
    const applyButton = h('button', { type: 'button', class: 'btn blue', 'data-action': 'studio-apply', disabled: true }, '적용');
    const revertButton = h('button', { type: 'button', class: 'btn', 'data-action': 'studio-revert', disabled: state.selection.decision !== 'USE_DERIVED' }, '되돌리기');
    const why = h('span', { class: 'mini', 'data-role': 'studio-apply-why' });
    const qaHost = h('div', { class: 'studio-qa', 'data-role': 'studio-qa' });
    const list = h('div', { class: 'studio-regions', 'data-role': 'studio-regions' });

    const flagged = (row) => row.flags.some((flag) => CONFIRM_FLAGS.has(flag));
    const blocked = (row) => row.flags.some((flag) => !CONFIRM_FLAGS.has(flag));

    function withFound(flags, more) {
      return [...flags, ...more.filter((flag) => !flags.includes(flag))];
    }

    // A refusal of 「적용」 names each unconfirmed region with its codes; the panel shows the codes
    // only the rendering revealed (LEFTOVER_SOURCE_TEXT) and asks for that region's 「확인함」.
    function showRefusedFlags(error) {
      const named = error instanceof ApiError && error.error?.code === 'IMAGE_STUDIO_FLAGGED_UNCONFIRMED' ? error.error.details?.flags : null;
      if (!named) return;
      for (const row of rows) {
        const more = (named[row.region.region_id] ?? []).filter((flag) => !row.flags.includes(flag));
        row.found = withFound(row.found, more);
        row.flags = withFound(row.flags, more);
      }
      repaint();
    }

    function draft() {
      return rows.map((row) => ({ region_id: row.region.region_id, include: row.include, text: row.text || null, erase: row.erase }));
    }

    const READY_COPY = '적용하면 번역본을 만들고, 확인 완료 후에 원본 대신 사용합니다.';

    // Whether 「적용」 may be pressed, and why not. The server checks the same again.
    function verdict() {
      const included = rows.filter((row) => row.include);
      const unconfirmed = included.filter((row) => flagged(row) && !row.confirmed);
      if (!state.observation_id) return [false, '이 이미지는 아직 글자를 읽지 않았습니다.'];
      if (!included.length) return [false, '번역을 적용할 영역을 고르세요. 「모두 적용」은 확인이 필요 없는 영역만 고릅니다.'];
      if (included.some((row) => !row.text.trim())) return [false, '적용할 영역에 번역문을 입력하세요.'];
      if (unconfirmed.length) return [false, `확인이 필요한 영역(${unconfirmed.map((row) => row.region.region_id).join(', ')})을 하나씩 「확인함」으로 확인하세요.`];
      if (previewing) return [false, '미리보기를 그리는 중입니다.'];
      if (included.some(blocked) || !previewOk) return [false, '미리보기가 완전하지 않습니다. 넘치는 문구를 줄이세요.'];
      return [true, READY_COPY];
    }

    function refresh() {
      const [ok, message] = verdict();
      why.textContent = message;
      applyButton.disabled = !ok || Boolean(qaHost.childElementCount);
    }

    // Each row is built once and repainted in place: a preview never takes the focus from a text.
    function buildRow(row) {
      const { region } = row;
      const input = h('input', { type: 'text', 'data-role': 'studio-korean', 'aria-label': `${region.region_id} 번역문`, maxlength: '500' });
      input.value = row.text;
      input.addEventListener('input', () => {
        row.text = input.value;
        schedule();
      });
      row.includeBox = h('input', { type: 'checkbox', 'data-role': 'studio-include' });
      row.includeBox.checked = row.include;
      row.includeBox.addEventListener('change', () => {
        row.include = row.includeBox.checked;
        paint(row);
        schedule();
      });
      const confirm = h('input', { type: 'checkbox', 'data-role': 'studio-confirm' });
      confirm.addEventListener('change', () => {
        row.confirmed = confirm.checked;
        refresh();
      });
      row.flagsHost = h('div', { class: 'studio-flags' });
      row.confirmLabel = h('label', { class: 'studio-confirm' }, confirm, '확인함');
      row.node = h(
        'div',
        { class: 'studio-region', 'data-region': region.region_id },
        h('b', { class: 'studio-rid' }, region.region_id),
        h('span', { class: 'studio-source', lang: region.script === 'ja' ? 'ja' : 'zh' }, region.text),
        input,
        row.flagsHost,
        h('div', { class: 'studio-toggles' }, h('label', {}, row.includeBox, '번역 적용'), row.confirmLabel),
      );
      paint(row);
      return row.node;
    }

    function paint(row) {
      const isFlagged = flagged(row);
      row.node.classList.toggle('flagged', isFlagged);
      row.node.dataset.flagged = String(isFlagged);
      row.node.dataset.include = String(row.include);
      row.node.dataset.flags = row.flags.join(' ');
      row.includeBox.checked = row.include;
      row.confirmLabel.hidden = !isFlagged;
      row.flagsHost.replaceChildren(h('span', { class: 'chip', 'data-role': 'studio-confidence' }, `신뢰도 ${Math.round(row.region.confidence * 100)}%`), ...flagChips(row.flags));
    }

    function drawRows() {
      list.replaceChildren(...rows.map(buildRow));
      refresh();
    }

    function repaint() {
      rows.forEach(paint);
      refresh();
    }

    async function renderPreview() {
      const mine = (sequence += 1);
      previewOk = false;
      previewState.dataset.complete = 'pending';
      if (!state.observation_id || rows.some((row) => row.include && !row.text.trim())) {
        previewing = false;
        previewState.className = 'chip';
        previewState.textContent = '번역문을 입력하면 미리보기를 그립니다';
        refresh();
        return;
      }
      previewState.className = 'chip';
      previewState.textContent = '미리보기를 그리는 중';
      try {
        const { blob, headers } = await sendForBlob('POST', stateUrl(item.item_id, sha, '/preview'), { observation_id: state.observation_id, source_language: language, items: draft() });
        if (mine !== sequence) return;
        previewing = false;
        const found = JSON.parse(headers.get('X-Image-Studio-Review') ?? '{}');
        previewImage.src = await blobToDataUrl(blob);
        previewImage.hidden = false;
        for (const row of rows) row.flags = withFound(found.flags?.[row.region.region_id] ?? row.flags, row.found);
        previewOk = Boolean(found.complete);
        previewState.className = previewOk ? 'chip good' : 'chip bad';
        previewState.textContent = previewOk ? '미리보기 · 완전함' : '미리보기 · 넘치거나 그릴 수 없는 글자가 있습니다';
        previewState.dataset.complete = String(previewOk);
        repaint();
      } catch (error) {
        if (mine !== sequence) return;
        previewing = false;
        previewState.className = 'chip bad';
        previewState.textContent = `미리보기 실패 · ${errorText(error)}`;
        refresh();
      }
    }

    function schedule() {
      previewing = true;
      previewState.dataset.complete = 'pending';
      refresh();
      window.clearTimeout(timer);
      timer = window.setTimeout(renderPreview, 250);
    }

    allButton.addEventListener('click', () => {
      // Every unflagged region with a text; never a flagged one (§6).
      for (const row of rows) {
        if (!flagged(row) && !blocked(row) && row.text.trim()) row.include = true;
      }
      repaint();
      schedule();
    });

    async function saveEdit() {
      try {
        const revision = await sendJson('POST', stateUrl(item.item_id, sha, '/revisions'), { observation_id: state.observation_id, source_language: language, items: draft(), expected_seq: expectedSeq, actor: OPERATOR });
        edit = revision;
        expectedSeq = revision.seq;
        return revision.revision_id;
      } catch (error) {
        // An edit identical to the current revision is that revision.
        if (error instanceof ApiError && error.error?.code === 'IMAGE_EDIT_UNCHANGED' && edit) return edit.revision_id;
        throw error;
      }
    }

    applyButton.addEventListener('click', async () => {
      applyButton.disabled = true;
      why.textContent = '번역본을 만드는 중…';
      try {
        const revisionId = await saveEdit();
        const confirmed = rows.filter((row) => row.include && row.confirmed).map((row) => row.region.region_id);
        const applied = await sendJson('POST', stateUrl(item.item_id, sha, '/apply'), { revision_id: revisionId, source_language: language, confirmed, actor: OPERATOR });
        showQa(applied);
        why.textContent = '번역본을 만들었습니다. 확인한 뒤 「확인 완료」를 누르세요.';
      } catch (error) {
        showRefusedFlags(error);
        why.textContent = errorText(error);
        toast('적용하지 못했습니다', errorText(error));
      } finally {
        refresh();
      }
    });

    function showQa(applied) {
      const done = h('button', { type: 'button', class: 'btn blue', 'data-action': 'studio-qa-pass' }, '확인 완료');
      done.addEventListener('click', async () => {
        done.disabled = true;
        try {
          await sendJson('POST', stateUrl(item.item_id, sha, '/qa'), { derivation_id: applied.derivation_id, verdict: 'PASS', actor: OPERATOR });
          await sendJson('POST', stateUrl(item.item_id, sha, '/select'), { derivation_id: applied.derivation_id, actor: OPERATOR });
          toast('번역본을 사용합니다', '원본은 그대로 남아 있고 「되돌리기」로 다시 쓸 수 있습니다.');
          await loadImages();
          await showReview(sha);
        } catch (error) {
          toast('확인하지 못했습니다', errorText(error));
          done.disabled = false;
        }
      });
      qaHost.replaceChildren(
        h('h4', {}, '번역본 확인'),
        h('img', { src: stateUrl(item.item_id, sha, `/derivations/${encodeURIComponent(applied.derivation_id)}/image`), alt: '만든 번역본', 'data-role': 'studio-artifact', 'data-sha256': applied.artifact_sha256 }),
        h('span', { class: 'mini' }, `이 번역본 그대로를 확인합니다 · ${applied.artifact_sha256.slice(0, 12)}`),
        h('div', { class: 'row' }, done),
      );
    }

    revertButton.addEventListener('click', async () => {
      revertButton.disabled = true;
      try {
        await sendJson('POST', stateUrl(item.item_id, sha, '/revert'), { actor: OPERATOR });
        toast('원본 이미지를 다시 사용합니다', '번역본은 지우지 않았습니다.');
        await loadImages();
        await showReview(sha);
      } catch (error) {
        toast('되돌리지 못했습니다', errorText(error));
        revertButton.disabled = false;
      }
    });

    drawRows();
    if (state.observation_id) schedule();
    return h(
      'div',
      { class: 'studio-review-body', 'data-image': sha, 'data-observed': String(Boolean(state.observation_id)) },
      h('div', { class: 'row studio-head' }, h('b', {}, `${ROLE_COPY[state.role] ?? state.role} ${state.ordinal}`), selectionChip(state.selection), candidateChip),
      h('div', { class: 'studio-compare' }, h('figure', {}, h('figcaption', { class: 'mini' }, '원본'), original), h('figure', {}, h('figcaption', { class: 'row' }, h('span', { class: 'mini' }, '번역 미리보기'), previewState), previewImage)),
      state.observation_id
        ? h('div', { class: 'row studio-tools' }, allButton, h('span', { class: 'mini' }, '확인이 필요한 영역은 하나씩 「확인함」을 눌러야 적용됩니다. 연락처와 복잡한 배경은 기본으로 원문 유지입니다.'))
        : h('div', { class: 'note' }, '이 이미지는 아직 글자를 읽지 않았습니다. 이미지를 고르고 「번역 시작」을 누르세요.'),
      list,
      h('div', { class: 'row studio-actions' }, applyButton, revertButton, why),
      qaHost,
      inUse(state),
    );
  }

  // The translated image the Item uses now in place of this one, exactly as recorded.
  function inUse(state) {
    const { selection } = state;
    if (selection.decision !== 'USE_DERIVED' || !selection.derivation_id) return null;
    return h(
      'div',
      { class: 'studio-qa', 'data-role': 'studio-in-use' },
      h('h4', {}, '사용 중인 번역본'),
      h('img', { src: stateUrl(item.item_id, state.source_sha256, `/derivations/${encodeURIComponent(selection.derivation_id)}/image`), alt: '사용 중인 번역본', 'data-sha256': selection.derived_sha256 }),
      h('span', { class: 'mini' }, '원본은 그대로 남아 있습니다. 「되돌리기」로 원본을 다시 씁니다.'),
    );
  }

  itemSelect.addEventListener('change', () => {
    item = items.find((entry) => entry.item_id === itemSelect.value) ?? items[0];
    ticked.clear();
    review.replaceChildren();
    loadImages().catch((error) => grid.replaceChildren(h('span', { class: 'chip warn' }, `이미지를 불러오지 못했습니다 · ${errorText(error)}`)));
  });
  languageSelect.addEventListener('change', () => {
    language = languageSelect.value;
    const shown = review.querySelector('[data-image]')?.dataset.image;
    if (shown) showReview(shown);
  });

  openModal({
    eyebrow: scope === 'detail' ? '상세페이지' : '대표이미지',
    title: '이미지 자동번역',
    help: '이미지 속 중국어 · 일본어 글자를 읽어 한국어로 바꾼 새 이미지를 만듭니다. 원본은 바뀌지 않고, 확인 완료한 번역본만 원본 대신 사용합니다.',
    body: h(
      'div',
      { class: 'studio', 'data-role': 'image-studio', 'data-scope': scope },
      h('div', { class: 'row studio-bar' }, itemSelect, languageSelect, run, runLine),
      grid,
      review,
    ),
  });
  loadImages().catch((error) => grid.replaceChildren(h('span', { class: 'chip warn' }, `이미지를 불러오지 못했습니다 · ${errorText(error)}`)));
}
