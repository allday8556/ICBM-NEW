// 설정: renders the v29 settings structure from the settings contract. In M0 the contract is
// read-only (`editable: false`) with no saved values and no connections, so every field shows
// as unset, every toggle as off, and save/test actions are inert. The one live card is
// 등록 권한 확인 (M2 PR-C), which talks to its own permission-attestation contract.

import { getJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { withHelp } from '../core/help.js';
import { markInert } from '../core/inert.js';
import { platformTag } from '../core/platform.js';
import { pageHead } from '../components/page-head.js';
import { permissionAttestationPanel } from './permission-attestation.js';
import { API_STATUS_LABEL, CONNECTION_LABEL, PLATFORM_TABS, SUBTABS } from './settings-schema.js';

const ENDPOINT = '/api/v1/screens/settings';
const TITLE = '설정';

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

function usersTable() {
  return h(
    'table',
    { class: 'table' },
    h('thead', {}, h('tr', {}, h('th', {}, '이름'), h('th', {}, '권한'), h('th', {}, '상태'))),
    h('tbody', {}, h('tr', {}, h('td', { class: 'table-empty', colspan: '3' }, '등록된 사용자가 없습니다 · v1은 로컬 단일 운영자'))),
  );
}

function registryItem(name, help, withEm) {
  return markInert(
    h('div', { class: 'registry-item' }, withHelp(h('b', {}, name), help), withEm ? h('em', {}, 'M0 · 미연결') : null),
    name,
  );
}

function registrySection(title, help, action, grid) {
  return h(
    'div',
    { class: 'registry-section' },
    h('div', { class: 'registry-head' }, h('div', {}, withHelp(h('b', {}, title), help)), action),
    grid,
  );
}

function registry(data) {
  return [
    registrySection(
      '1. 공통 규칙 · Global Rules',
      '모든 AI 기능에 공통 적용되는 사실성·검증·승인 정책',
      button('공통 규칙 편집', 'ai'),
      null,
    ),
    registrySection(
      '2. 역할 · Role Profiles',
      '업무별 AI의 전문 역할과 판단 우선순위',
      null,
      h('div', { class: 'registry-grid roles' }, data.roles.map(([name, help]) => registryItem(name, help, true))),
    ),
    registrySection(
      '3. 플랫폼 정책 · Platform Policy',
      '플랫폼 공식 데이터·SEO·추천 API 사용 우선순위',
      null,
      h('div', { class: 'registry-grid policies' }, data.policies.map(([name, help]) => registryItem(name, help, true))),
    ),
    registrySection(
      '4. 작업 · Task Prompts',
      '실행 버튼에 따라 TASK_MODE를 선택',
      null,
      h('div', { class: 'registry-grid tasks' }, data.tasks.map(([name, help]) => registryItem(name, help, false))),
    ),
  ];
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
      h(
        'div',
        { class: 'alert-row' },
        h('span', {}, platformTag(key)),
        h('span', { class: 'chip' }, CONNECTION_LABEL[state.connections.get(key)] ?? '미연동'),
      ),
    );
  }
  if (item.permissionAttestation) return permissionAttestationPanel(item.permissionAttestation);
  if (item.usersTable) return usersTable();
  if (item.registry) return registry(item.registry);
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
    const view = await getJson(ENDPOINT);
    const requestedTab = ctx.params.get('tab');
    const tabKey = PLATFORM_TABS.some((tab) => tab.key === requestedTab) ? requestedTab : 'common';
    const subtabs = SUBTABS[tabKey];
    const sub = subtabs.find((item) => item.key === ctx.params.get('sub')) ?? subtabs[0];
    const state = {
      values: view.policy_values,
      connections: new Map(view.marketplace_connections.map((c) => [c.marketplace_key, c.connection_state])),
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
        h('span', { class: 'chip' }, view.editable ? '변경 사항을 저장할 수 있습니다' : 'M0 · 읽기 전용 — 설정 저장 계약이 아직 연결되지 않았습니다'),
        h('div', { class: 'savebar-actions' }, button('취소'), button('설정 저장', 'blue')),
      ),
    );
  },
};
