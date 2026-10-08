// AI Prompt Registry (ADR-0026 AIF-1): the v29 Settings registry and its layered prompt editor.
//
// Every text and revision comes from the server's PromptTemplate and PlatformPolicy stores
// (GET /api/v1/ai/registry); revision 1 of each entry is the v29 prototype's text. The editor is the
// prototype's: seven tabs (공통 규칙, 역할, 플랫폼 Policy, 작업, 출력 형식, 입력 변수, 조립 미리보기),
// and only the current tab is saved or reset, each as a new revision against the revision it was
// read from. The preview is the server's composition of the saved layers. Nothing here calls an
// AI provider: none exists.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { h } from '../core/dom.js';
import { withHelp } from '../core/help.js';
import { openModal } from '../core/modal.js';
import { toast } from '../core/toast.js';

const REGISTRY = '/api/v1/ai/registry';
const GLOBAL_KEY = 'ICBM_GLOBAL_RULES_V2';
const COMMON_POLICY = 'POLICY_COMMON_MARKET_V1';

// tab → which entry of the editor's context it edits, and which field of that entry.
const TABS = [
  ['global', '공통 규칙', 'global', 'prompt'],
  ['role', '역할 Role', 'role', 'prompt'],
  ['policy', '플랫폼 Policy', 'policy', 'prompt'],
  ['task', '작업 Task', 'task', 'prompt'],
  ['output', '출력 형식', 'task', 'output'],
  ['variables', '입력 변수', 'task', 'variables'],
  ['preview', '조립 미리보기', null, null],
];
const SCOPE = { global: 'Global', role: 'Role', policy: 'Policy', task: 'Task', output: 'Output', variables: 'Variables', preview: 'Preview' };

const ERROR_COPY = {
  AI_PROMPT_CURRENT_MOVED: '다른 곳에서 먼저 저장되었습니다. 편집기를 다시 열어 최신 내용으로 작업하세요.',
  AI_PROMPT_UNCHANGED: '현재 저장된 내용과 같아 저장할 것이 없습니다.',
  AI_PROMPT_TEXT_INVALID: '내용을 입력하세요. 최대 20,000자까지 저장할 수 있습니다.',
  AI_PROMPT_FIELD_NOT_EDITABLE: '이 계층에서는 편집할 수 없는 항목입니다.',
};

function errorCopy(error) {
  if (error instanceof ApiError) return ERROR_COPY[error.error?.code] ?? error.message;
  return String(error);
}

function familyOf(entry) {
  return entry.layer === 'POLICY' ? 'policies' : 'prompts';
}

function versionLine(entry) {
  return `v${entry.current.revision_no} · ${entry.modified_fields.length ? '사용자 수정본' : '기본값'}`;
}

// ------------------------------------------------------------------ the Settings registry

function card(entry, label, help, context, onSaved, className) {
  return h(
    'button',
    {
      type: 'button',
      class: `registry-item live${entry?.modified_fields.length ? ' modified' : ''}${className ? ` ${className}` : ''}`,
      'data-prompt-key': entry?.key ?? '',
      onclick: () => openPromptEditor(context, onSaved),
    },
    withHelp(h('b', {}, label), help),
    entry ? h('em', {}, versionLine(entry)) : null,
  );
}

function section(title, help, action, grid) {
  return h(
    'div',
    { class: 'registry-section' },
    h('div', { class: 'registry-head' }, h('div', {}, withHelp(h('b', {}, title), help)), action),
    grid,
  );
}

// `layout` is the v29 registry as Settings shows it (settings-schema REGISTRY): which roles,
// policies and tasks the card lists, each with the role, policy and task its editor opens on.
export function promptRegistry(layout) {
  const host = h('div', { class: 'prompt-registry', 'data-role': 'prompt-registry' }, h('div', { class: 'mini' }, '프롬프트를 불러오는 중…'));
  const draw = async () => {
    let entries;
    try {
      entries = new Map((await getJson(REGISTRY)).entries.map((entry) => [entry.key, entry]));
    } catch (error) {
      host.replaceChildren(h('div', { class: 'note' }, `프롬프트 저장소를 읽지 못했습니다 · ${errorCopy(error)}`));
      return;
    }
    const global = entries.get(GLOBAL_KEY);
    host.replaceChildren(
      section(
        '1. 공통 규칙 · Global Rules',
        '모든 AI 기능에 공통 적용되는 사실성·검증·승인 정책',
        h(
          'button',
          {
            type: 'button',
            class: 'btn ai',
            'data-prompt-key': GLOBAL_KEY,
            onclick: () => openPromptEditor({ role: 'ROLE_PRODUCT_MD_V1', task: 'TASK_PRODUCT_RECOMMEND_BUNDLE_V1', layer: 'global' }, draw),
          },
          `공통 규칙 편집 · ${versionLine(global)}`,
        ),
        null,
      ),
      section(
        '2. 역할 · Role Profiles',
        '업무별 AI의 전문 역할과 판단 우선순위',
        null,
        h('div', { class: 'registry-grid roles' }, layout.roles.map((item) => card(entries.get(item.role), item.label, item.help, { role: item.role, task: item.task, layer: 'role' }, draw))),
      ),
      section(
        '3. 플랫폼 정책 · Platform Policy',
        '플랫폼 공식 데이터·SEO·추천 API 사용 우선순위',
        null,
        h('div', { class: 'registry-grid policies' }, layout.policies.map((item) => card(entries.get(item.policy), item.label, item.help, { role: item.role, task: item.task, policy: item.policy, layer: 'policy' }, draw))),
      ),
      section(
        '4. 작업 · Task Prompts',
        '실행 버튼에 따라 TASK_MODE를 선택',
        null,
        h('div', { class: 'registry-grid tasks' }, layout.tasks.map((item) => card(entries.get(item.task), item.label, item.help, { role: item.role, task: item.task, layer: 'task' }, draw))),
      ),
    );
  };
  draw();
  return host;
}

// ------------------------------------------------------------------ the layered editor

// Opens the editor on one role × policy × task, at one tab. `onSaved` runs after each save or reset.
export async function openPromptEditor({ role, task, policy = COMMON_POLICY, layer = 'global' }, onSaved = () => {}) {
  let entries;
  try {
    entries = new Map((await getJson(REGISTRY)).entries.map((entry) => [entry.key, entry]));
  } catch (error) {
    toast('프롬프트 저장소를 읽지 못했습니다', errorCopy(error));
    return;
  }
  const keys = { global: GLOBAL_KEY, role, policy, task };
  let active = TABS.some(([key]) => key === layer) ? layer : 'global';
  const drafts = new Map(); // tab → unsaved text

  const scope = h('span', { class: 'chip info', 'data-role': 'prompt-scope' });
  const state = h('span', { class: 'chip', 'data-role': 'prompt-state' });
  const help = h('span', { class: 'mini' });
  const area = h('textarea', { class: 'prompt-text', spellcheck: 'false', 'data-role': 'prompt-text' });
  const tabs = h('div', { class: 'prompt-tabs', role: 'tablist', 'aria-label': '프롬프트 계층' });
  const meta = h('div', { class: 'mini prompt-meta' });
  const reset = h('button', { type: 'button', class: 'btn', 'data-action': 'prompt-reset' }, '↺ 현재 계층 초기화');
  const save = h('button', { type: 'button', class: 'btn blue', 'data-action': 'prompt-save' }, '현재 계층 저장');

  const tabOf = (key) => TABS.find(([tab]) => tab === key);
  const entryOf = (key) => {
    const [, , slot] = tabOf(key);
    return slot ? entries.get(keys[slot]) : null;
  };
  const savedText = (key) => {
    const [, , , field] = tabOf(key);
    return entryOf(key)?.content[field] ?? '';
  };
  const helpText = (key) => {
    const entry = entryOf(key);
    const taskTitle = entries.get(task)?.title ?? '';
    return {
      global: '모든 ICBM AI 기능에 공통 적용됩니다.',
      role: `${entry?.title ?? ''} 역할을 사용하는 모든 Task에 적용됩니다.`,
      policy: `${entry?.title ?? ''} 플랫폼 정책이 해당 마켓 AI 작업에 적용됩니다.`,
      task: `${taskTitle} 작업을 호출하는 모든 화면에 적용됩니다.`,
      output: `${taskTitle}의 출력 형식에만 적용됩니다.`,
      variables: `${taskTitle}의 입력 변수 정의에만 적용됩니다.`,
      preview: '저장된 각 계층을 조립한 최종 프롬프트 미리보기입니다. 실행 데이터 자리는 실제 실행 때 채워집니다.',
    }[key];
  };

  function drawMeta() {
    const name = (key) => `${entries.get(key)?.title ?? key} v${entries.get(key)?.current.revision_no ?? '?'}`;
    meta.textContent = `${name(role)} · ${name(policy)} · ${name(task)}`;
  }

  function drawTabs() {
    tabs.replaceChildren(
      ...TABS.map(([key, label]) =>
        h(
          'button',
          {
            type: 'button',
            role: 'tab',
            class: key === active ? 'active' : '',
            'aria-selected': String(key === active),
            'data-layer-tab': key,
            onclick: () => select(key),
          },
          label,
          drafts.has(key) ? h('span', { class: 'dirty-dot', 'aria-label': '저장되지 않은 변경' }) : null,
        ),
      ),
    );
  }

  function drawState() {
    const [, , , field] = tabOf(active);
    const entry = entryOf(active);
    const dirty = drafts.has(active);
    const modified = Boolean(entry && entry.modified_fields.includes(field));
    scope.textContent = SCOPE[active];
    state.className = `chip ${dirty ? 'warn' : modified ? 'info' : ''}`;
    state.textContent = active === 'preview' ? (drafts.size ? '저장된 내용 기준' : '조립 결과') : dirty ? '저장되지 않은 변경' : modified ? '사용자 수정본' : '기본값';
    help.textContent = helpText(active);
    const editable = active !== 'preview';
    save.hidden = !editable;
    reset.hidden = !editable;
    save.disabled = !dirty;
    reset.disabled = !modified;
  }

  async function select(key) {
    active = key;
    drawTabs();
    if (key === 'preview') {
      area.readOnly = true;
      area.value = '조립 중…';
      drawState();
      try {
        const view = await getJson(`/api/v1/ai/preview?${new URLSearchParams({ role, policy, task })}`);
        if (active === 'preview') area.value = view.text;
      } catch (error) {
        if (active === 'preview') area.value = `조립하지 못했습니다 · ${errorCopy(error)}`;
      }
      return;
    }
    area.readOnly = false;
    area.value = drafts.get(key) ?? savedText(key);
    drawState();
  }

  area.addEventListener('input', () => {
    if (active === 'preview') return;
    if (area.value === savedText(active)) drafts.delete(active);
    else drafts.set(active, area.value);
    drawTabs();
    drawState();
  });

  async function write(kind) {
    const [, , , field] = tabOf(active);
    const entry = entryOf(active);
    const path = `/api/v1/ai/${familyOf(entry)}/${encodeURIComponent(entry.key)}/${kind === 'save' ? 'revisions' : 'reset'}`;
    const payload = { actor: 'operator', expected_current_revision: entry.current.revision_id, field };
    if (kind === 'save') payload.text = drafts.get(active);
    save.disabled = true;
    reset.disabled = true;
    try {
      const updated = await sendJson('POST', path, payload);
      entries.set(updated.key, updated);
      drafts.delete(active);
      toast(kind === 'save' ? '저장했습니다' : '기본값으로 되돌렸습니다', `${updated.title} v${updated.current.revision_no}`);
      onSaved();
    } catch (error) {
      toast(kind === 'save' ? '저장하지 못했습니다' : '되돌리지 못했습니다', errorCopy(error));
    }
    drawMeta();
    select(active);
  }
  save.addEventListener('click', () => write('save'));
  reset.addEventListener('click', () => write('reset'));

  openModal({
    eyebrow: 'ICBM Prompt Registry · Layered Prompt System',
    title: entries.get(task)?.title ?? 'AI Prompt',
    body: h(
      'div',
      { class: 'prompt-editor', 'data-role': 'prompt-editor', 'data-task': task, 'data-policy': policy, 'data-prompt-role': role },
      meta,
      tabs,
      h('div', { class: 'prompt-edit-toolbar' }, h('div', { class: 'prompt-chips' }, scope, state), help),
      h('div', { class: 'prompt-content' }, area),
    ),
    footer: [
      h('div', { class: 'prompt-foot-left' }, h('span', { class: 'chip good' }, 'Global + Role + Policy + Task'), h('span', { class: 'mini' }, '현재 탭만 독립적으로 저장/초기화됩니다.')),
      h('div', { class: 'prompt-foot-actions' }, reset, save),
    ],
  });
  drawMeta();
  select(active);
}

