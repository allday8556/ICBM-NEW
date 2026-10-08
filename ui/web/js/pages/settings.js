// 설정: renders the v29 settings structure from the settings contract. The general common and
// platform settings have no save contract (`editable: false`), so every such field shows as
// unset, every toggle as off, and their save/test actions are inert. The live parts are 등록 권한
// 확인 (M2 PR-C, its own permission-attestation contract), the SmartStore capability projection
// (M2 PR-D, CAPABILITY_MAPPING §14.11), read from the capability read API, the SmartStore
// operator actions (M2 PR-E, instructions §8A), after each of which that truth is re-read, and
// the registration target policy (Gate 1 G1-A, ADR-0015 §2), the operator-reviewed category
// metadata (Gate 1 G1-B, ADR-0015 §3) and the AI Prompt Registry (ADR-0026 AIF-1): the surfaces
// the server lists in `editable_surfaces`, each saved through its own contract. What the save bar says comes from those server fields, never
// from this page.

import { getJson } from '../core/api.js';
import { authLine, statusChip } from '../platforms/smartstore/capability.js';
import { fragment, h } from '../core/dom.js';
import { withHelp } from '../core/help.js';
import { markInert } from '../core/inert.js';
import { platformTag } from '../core/platform.js';
import { pageHead } from '../components/page-head.js';
import { openPromptEditor, promptRegistry } from '../components/prompt-registry.js';
import { capabilityProjection } from './settings/capability-projection.js';
import { permissionAttestationPanel } from './settings/permission-attestation.js';
import { API_STATUS_LABEL, CONNECTION_LABEL, PLATFORM_TABS, SUBTABS } from './settings/settings-schema.js';
import { accountPanel, contractReviewPanel, credentialsPanel, workflowActions } from './settings/smartstore-operator.js';
import { categoryMetadataPanel } from './settings/category-metadata.js';
import { targetPolicyPanel } from './settings/target-policy.js';

const ENDPOINT = '/api/v1/screens/settings';
const READINESS = '/api/ready';
const CAPABILITIES = '/api/v1/connect/marketplaces/capabilities';
const CAPABILITY = (key) => `/api/v1/connect/marketplaces/${key}/capability`;

// One live truth per capability marketplace on the page (M2 PR-E). The PR-D projection, its
// action rows and the A0 card are re-read from the server after every operator action; nothing is
// promoted locally.
function liveTruth(key, initial) {
  const projection = h('div', { class: 'truth-host' });
  const actions = h('div', { class: 'truth-host' });
  const attestation = h('div', { class: 'truth-host' });
  let view = initial;
  let refresh = null;
  const draw = () => {
    projection.replaceChildren(capabilityProjection(key, view));
    actions.replaceChildren(...workflowActions(key, view, refresh));
  };
  refresh = async () => {
    view = await getJson(CAPABILITY(key)).catch(() => null);
    draw();
    attestation.replaceChildren(permissionAttestationPanel(key));
  };
  draw();
  attestation.replaceChildren(permissionAttestationPanel(key));
  return { projection, actions, attestation, refresh };
}
const TITLE = '설정';

// The display name of each surface the server says it accepts a save for (settings contract).
const SURFACE_LABEL = {
  REGISTRATION_TARGET_POLICY: '등록 대상 정책 (마켓 탭 › 등록 정책)',
  REGISTRATION_CATEGORY_METADATA: '카테고리 메타데이터 (마켓 탭 › 상품 / 카테고리)',
  AI_PROMPT_REGISTRY: 'AI Prompt Registry (공통 › AI / Prompt)',
};

function saveScope(view) {
  if (view.editable) return '변경 사항을 저장할 수 있습니다';
  const surfaces = (view.editable_surfaces ?? []).map((surface) => SURFACE_LABEL[surface] ?? surface);
  if (!surfaces.length) return '읽기 전용 — 저장 계약이 연결된 설정이 없습니다';
  return `일반 설정은 저장 계약이 없어 읽기 전용입니다 · 저장 가능: ${surfaces.join(', ')}`;
}

let fieldSequence = 0;

function button(label, variant) {
  return markInert(h('button', { type: 'button', class: variant ? `btn ${variant}` : 'btn' }, label));
}

function fieldRow(item, state) {
  fieldSequence += 1;
  const id = `setting-field-${fieldSequence}`;
  const value = state.values[item.id];
  return h(
    'div',
    { class: 'form-row' },
    h('label', { for: id }, item.field),
    h('input', {
      id,
      type: item.secret ? 'password' : 'text',
      readonly: true,
      'aria-readonly': 'true',
      autocomplete: 'off',
      value: value ?? '',
      placeholder: item.placeholder ?? '미설정',
      'data-setting': item.id,
    }),
  );
}

function toggleRow(item, state) {
  const on = state.values[item.id] === true;
  const control = h('button', {
    type: 'button',
    class: on ? 'toggle' : 'toggle off',
    role: 'switch',
    'aria-checked': String(on),
    'aria-label': item.toggle,
    'data-setting': item.id,
  });
  return h('div', { class: 'kv' }, h('span', {}, item.toggle), markInert(control, item.toggle));
}

function statusRow(item, state) {
  const chip = item.marketplace
    ? API_STATUS_LABEL[state.connections.get(item.marketplace)] ?? '미연결'
    : item.chip;
  return h('div', { class: 'kv' }, h('span', {}, item.status), h('span', { class: 'chip' }, chip));
}

// A marketplace with an adopted capability contract shows its authentication line (§14.3,
// §14.11 surface 2); the others keep the settings contract's connection state. A capability
// marketplace whose truth could not be read says so rather than falling back to 미연동.
function connectionChip(key, state) {
  const capability = state.capabilities.get(key);
  if (capability) return statusChip('auth', authLine(capability));
  if (state.connections.has(key)) return h('span', { class: 'chip' }, CONNECTION_LABEL[state.connections.get(key)] ?? '미연동');
  return h('span', { class: 'chip warn' }, '확인 불가');
}

// ADR-0026 AIF-2: the ai capability as readiness reports it. No AI provider is configured, so it
// reads 미설정; this row only shows the server's state.
function aiCapabilityRow() {
  const chip = h('span', { class: 'chip', 'data-role': 'ai-capability' }, '확인 중');
  getJson(READINESS)
    .then((view) => view.capabilities.find((item) => item.key === 'ai'))
    .catch(() => null)
    .then((ai) => {
      if (!ai) {
        chip.textContent = '확인 불가';
        chip.className = 'chip warn';
        return;
      }
      chip.dataset.status = ai.status;
      chip.textContent = ai.status === 'READY' ? '연결됨' : '미설정 · AI 공급자 없음';
      chip.className = ai.status === 'READY' ? 'chip good' : 'chip';
    });
  return h('div', { class: 'kv' }, h('span', {}, 'AI 공급자'), chip);
}

function usersTable() {
  return h(
    'table',
    { class: 'table' },
    h('thead', {}, h('tr', {}, h('th', {}, '이름'), h('th', {}, '권한'), h('th', {}, '상태'))),
    h('tbody', {}, h('tr', {}, h('td', { class: 'table-empty', colspan: '3' }, '등록된 사용자가 없습니다 · v1은 로컬 단일 운영자'))),
  );
}

function renderItem(item, state) {
  if (item.field) return fieldRow(item, state);
  if (item.toggle) return toggleRow(item, state);
  if (item.status) return statusRow(item, state);
  if (item.kv) {
    return h('div', { class: 'kv' }, h('span', {}, item.kv), h('b', {}, '미설정', h('em', { class: 'inherit-tag' }, item.tag)));
  }
  if (item.note) return h('div', { class: 'note' }, item.note);
  if (item.flow) {
    return h(
      'div',
      { class: 'policy-flow' },
      item.flow.map((step, index) => [index ? h('span', { 'aria-hidden': 'true' }, '›') : null, h('b', {}, step)]),
    );
  }
  if (item.actions) {
    return h(
      'div',
      { class: item.layout === 'stretch' ? 'detail-actions' : 'api-action-row' },
      item.actions.map((action) => button(action.label, action.variant)),
    );
  }
  if (item.button) return button(item.button, item.variant);
  if (item.marketplaceStatus) {
    return item.marketplaceStatus.map((key) =>
      h('div', { class: 'alert-row', 'data-marketplace': key }, h('span', {}, platformTag(key)), connectionChip(key, state)),
    );
  }
  if (item.capabilityProjection) return state.truth(item.capabilityProjection).projection;
  if (item.workflowActions) return state.truth(item.workflowActions).actions;
  if (item.permissionAttestation) return state.truth(item.permissionAttestation).attestation;
  if (item.smartstoreCredentials) return credentialsPanel(() => state.truth('smartstore').refresh());
  if (item.smartstoreAccount) return accountPanel(() => state.truth('smartstore').refresh());
  if (item.contractReview) {
    return contractReviewPanel(item.contractReview, () => state.truth(item.contractReview).refresh());
  }
  if (item.targetPolicy) return targetPolicyPanel(item.targetPolicy, item.section ?? 'policy');
  if (item.categoryMetadata) return categoryMetadataPanel(item.categoryMetadata);
  if (item.usersTable) return usersTable();
  if (item.aiCapability) return aiCapabilityRow();
  if (item.registry) return promptRegistry(item.registry);
  if (item.promptEditor) {
    const { label, ...context } = item.promptEditor;
    return h(
      'button',
      { type: 'button', class: 'btn ai', 'data-prompt-key': context.policy, onclick: () => openPromptEditor({ ...context, layer: 'policy' }) },
      label,
    );
  }
  return null;
}

function renderCard(card, state) {
  return h(
    'div',
    { class: card.full ? 'setting-card full' : 'setting-card' },
    withHelp(h('h3', {}, card.title), card.help),
    card.items.map((item) => renderItem(item, state)),
  );
}

function renderBlock(block, state) {
  if (block.cards) return h('div', { class: 'cards2' }, block.cards.map((card) => renderCard(card, state)));
  if (block.card) return renderCard(block.card, state);
  if (block.inheritNote) {
    return h('div', { class: 'inherit-note' }, h('b', {}, block.inheritNote.bold), ` · ${block.inheritNote.text}`);
  }
  return null;
}

function tabList(label, className, tabs, activeKey, onSelect, renderLabel) {
  return h(
    'div',
    { class: className, role: 'tablist', 'aria-label': label },
    tabs.map((tab) =>
      h(
        'button',
        { type: 'button', role: 'tab', 'aria-selected': String(tab.key === activeKey), onclick: () => onSelect(tab.key) },
        renderLabel(tab),
      ),
    ),
  );
}

export default {
  key: 'settings',
  title: TITLE,
  navLabel: TITLE,
  icon: '⚙',
  async render(ctx) {
    const [view, capabilities] = await Promise.all([getJson(ENDPOINT), getJson(CAPABILITIES).catch(() => [])]);
    const requestedTab = ctx.params.get('tab');
    const tabKey = PLATFORM_TABS.some((tab) => tab.key === requestedTab) ? requestedTab : 'common';
    const subtabs = SUBTABS[tabKey];
    const sub = subtabs.find((item) => item.key === ctx.params.get('sub')) ?? subtabs[0];
    const state = {
      values: view.policy_values,
      connections: new Map(view.marketplace_connections.map((c) => [c.marketplace_key, c.connection_state])),
      capabilities: new Map(capabilities.map((c) => [c.marketplace_key, c])),
    };
    const truths = new Map();
    state.truth = (key) => {
      if (!truths.has(key)) truths.set(key, liveTruth(key, state.capabilities.get(key) ?? null));
      return truths.get(key);
    };

    return fragment(
      pageHead({ title: TITLE, help: '공통 기본값과 플랫폼별 연동·정책을 한 곳에서 관리합니다.' }),
      tabList(
        '설정 범위',
        'tabs',
        PLATFORM_TABS,
        tabKey,
        (key) => ctx.navigate('settings', { tab: key }),
        (tab) => (tab.marketplace ? platformTag(tab.marketplace) : tab.label),
      ),
      h(
        'div',
        { class: 'settings-pane' },
        tabList(
          '세부 설정',
          'subtabs',
          subtabs,
          sub.key,
          (key) => ctx.navigate('settings', { tab: tabKey, sub: key }),
          (tab) => tab.label,
        ),
        sub.blocks.map((block) => renderBlock(block, state)),
      ),
      h(
        'div',
        { class: 'savebar' },
        h('span', { class: 'chip', 'data-save-scope': (view.editable_surfaces ?? []).join(' ') }, saveScope(view)),
        h('div', { class: 'savebar-actions' }, button('취소'), button('설정 저장', 'blue')),
      ),
    );
  },
};
