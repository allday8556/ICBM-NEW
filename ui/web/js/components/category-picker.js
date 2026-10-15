// 카테고리 검색 선택 (2026-10-15 usability check): the marketplace leaf category is chosen by its
// Korean name, never typed as a bare number.
//
// The candidates are the server's own durable leaf catalog
// (GET /api/v1/settings/category-catalog/{marketplace}?query=…), searched as the operator types.
// Each is shown with its whole Korean path (대분류 > … > 리프) and the choice stores the leaf id
// in the form's `category_id` field. The page judges nothing: whether a category can be authored
// is the server's answer when its requirements are read.

import { getJson } from '../core/api.js';
import { h } from '../core/dom.js';

const CATALOG = '/api/v1/settings/category-catalog';
const LIMIT = 30;
const DEBOUNCE_MS = 220;
// A leaf id typed by hand (a number, or a test fixture's id): accepted only with Enter, and only
// when the catalog has no entry with exactly that id.
const RAW_ID = /^[A-Za-z0-9_.-]+$/;
const NOTE = {
  idle: '카테고리 이름의 일부를 입력하면 후보가 나옵니다.',
  searching: '카테고리를 찾는 중…',
  none: '맞는 카테고리가 없습니다. 다른 낱말로 검색해 보세요.',
  empty: '카테고리 목록이 아직 없습니다. 설정 › 상품/카테고리에서 카테고리 목록을 동기화하세요.',
  unavailable: '카테고리 검색을 쓸 수 없습니다. 카테고리 번호를 알면 입력한 뒤 Enter를 누르세요.',
  pick: '목록에서 카테고리를 골라 주세요.',
};

// The whole category name the catalog records ("식품>건강식품>…"), spaced for reading.
export function categoryPath(whole) {
  return String(whole ?? '')
    .split('>')
    .map((part) => part.trim())
    .filter(Boolean)
    .join(' > ');
}

function search(marketplaceKey, query) {
  return getJson(
    `${CATALOG}/${encodeURIComponent(marketplaceKey)}?query=${encodeURIComponent(query)}&limit=${LIMIT}`,
  );
}

// The catalog entry with exactly this leaf id, or null.
export async function findCategory(marketplaceKey, categoryId) {
  if (!categoryId) return null;
  try {
    const found = await search(marketplaceKey, categoryId);
    return (found.entries ?? []).find((entry) => entry.category_id === categoryId) ?? null;
  } catch {
    return null;
  }
}

// One picker. `onChange(entry)` fires after a choice; `entry.whole_category_name` is null for an
// id typed by hand that the catalog does not name.
export function categoryPicker({ marketplaceKey, value, onChange }) {
  const hidden = h('input', { type: 'hidden', name: 'category_id', value: value ?? '' });
  const current = h('div', { class: 'category-current', 'data-role': 'category-current' });
  const query = h('input', {
    type: 'search',
    'data-role': 'category-search',
    placeholder: '카테고리 검색 (예: 루테인)',
    'aria-label': '카테고리 검색',
    autocomplete: 'off',
    role: 'combobox',
    'aria-expanded': 'false',
    'aria-autocomplete': 'list',
  });
  const list = h('ul', { class: 'category-results', 'data-role': 'category-results', role: 'listbox', hidden: true });
  const note = h('div', { class: 'mini', 'data-role': 'category-note' }, NOTE.idle);
  // Classed as a note only while it says something: an empty, hidden note is no state to show.
  const problem = h('div', { 'data-role': 'category-problem', hidden: true });
  let entries = [];
  let shownFor = null;
  let active = -1;
  let timer = null;
  let sequence = 0;
  const cache = new Map();

  function setNote(key) {
    note.textContent = NOTE[key] ?? '';
    note.dataset.note = key;
  }

  function showCurrent(entry) {
    const id = hidden.value;
    if (!id) {
      current.replaceChildren(h('span', { class: 'no-data' }, '선택한 카테고리가 없습니다'));
      current.dataset.category = '';
      return;
    }
    current.dataset.category = id;
    const path = entry?.whole_category_name ? categoryPath(entry.whole_category_name) : null;
    current.replaceChildren(
      h('b', { 'data-role': 'category-path' }, path ?? `카테고리 번호 ${id}`),
      path ? h('span', { class: 'mini', 'data-role': 'category-id' }, ` · 번호 ${id}`) : null,
    );
  }

  function close() {
    list.hidden = true;
    query.setAttribute('aria-expanded', 'false');
    active = -1;
  }

  function highlight(index) {
    active = index;
    [...list.children].forEach((row, at) => row.setAttribute('aria-selected', String(at === index)));
    list.children[index]?.scrollIntoView({ block: 'nearest' });
  }

  function draw() {
    list.replaceChildren(
      ...entries.map((entry, index) =>
        h(
          'li',
          {
            role: 'option',
            'data-category': entry.category_id,
            'aria-selected': 'false',
            // A press, not a click: the search box keeps focus until the choice is made.
            onmousedown: (event) => {
              event.preventDefault();
              choose(entry);
            },
            onmouseenter: () => highlight(index),
          },
          h('b', {}, entry.name),
          h('span', { class: 'mini' }, categoryPath(entry.whole_category_name)),
        ),
      ),
    );
    list.hidden = entries.length === 0;
    query.setAttribute('aria-expanded', String(entries.length > 0));
    active = -1;
  }

  async function lookup(text) {
    if (cache.has(text)) return cache.get(text);
    const found = await search(marketplaceKey, text);
    const result = { entries: found.entries ?? [], total: found.total ?? 0 };
    cache.set(text, result);
    return result;
  }

  async function run(text) {
    const mine = ++sequence;
    if (!text) {
      entries = [];
      shownFor = null;
      draw();
      setNote('idle');
      return { entries: [], total: 0 };
    }
    setNote('searching');
    try {
      const found = await lookup(text);
      if (mine !== sequence) return found;
      entries = found.entries;
      shownFor = text;
      draw();
      setNote(found.total === 0 ? 'empty' : entries.length ? 'idle' : 'none');
      if (entries.length) note.textContent = `${entries.length}개 후보 · 위아래 화살표와 Enter로 고를 수 있습니다.`;
      return found;
    } catch {
      if (mine === sequence) {
        entries = [];
        draw();
        setNote('unavailable');
      }
      return null;
    }
  }

  function choose(entry) {
    hidden.value = entry.category_id;
    query.value = '';
    entries = [];
    shownFor = null;
    draw();
    close();
    setNote('idle');
    showCurrent(entry);
    setProblem(null);
    onChange?.(entry);
  }

  // Enter: the highlighted candidate, else the exact id, else the only candidate, else an id
  // typed by hand the catalog does not hold. A name that matches several is never guessed.
  async function resolveTyped() {
    const text = query.value.trim();
    if (!text) return;
    if (active >= 0 && entries[active] && shownFor === text) {
      choose(entries[active]);
      return;
    }
    window.clearTimeout(timer);
    const found = shownFor === text ? { entries } : await run(text);
    const candidates = found?.entries ?? [];
    const exact = candidates.find((entry) => entry.category_id === text);
    if (exact) choose(exact);
    else if (candidates.length === 1) choose(candidates[0]);
    else if (!candidates.length && RAW_ID.test(text)) choose({ category_id: text, whole_category_name: null });
    else setNote(candidates.length ? 'pick' : 'none');
  }

  query.addEventListener('input', () => {
    window.clearTimeout(timer);
    const text = query.value.trim();
    timer = window.setTimeout(() => run(text), DEBOUNCE_MS);
  });
  query.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      // Never the form's own submit: Enter chooses a category here.
      event.preventDefault();
      resolveTyped();
    } else if (event.key === 'ArrowDown' && entries.length) {
      event.preventDefault();
      list.hidden = false;
      highlight(Math.min(active + 1, entries.length - 1));
    } else if (event.key === 'ArrowUp' && entries.length) {
      event.preventDefault();
      highlight(Math.max(active - 1, 0));
    } else if (event.key === 'Escape') {
      close();
    }
  });
  query.addEventListener('blur', () => window.setTimeout(close, 120));
  query.addEventListener('focus', () => {
    if (entries.length) {
      list.hidden = false;
      query.setAttribute('aria-expanded', 'true');
    }
  });

  function setProblem(text) {
    problem.hidden = !text;
    problem.className = text ? 'note' : '';
    problem.textContent = text ?? '';
  }

  showCurrent(null);
  if (value) findCategory(marketplaceKey, value).then((entry) => entry && hidden.value === value && showCurrent(entry));

  const el = h(
    'div',
    { class: 'kv category-picker', 'data-role': 'category-picker' },
    h('span', {}, '카테고리'),
    h('div', { class: 'category-picker-body' }, current, h('div', { class: 'category-search' }, query, list), note, problem),
    hidden,
  );
  return {
    el,
    input: hidden,
    search: query,
    // A choice made elsewhere (the AI recommendation): shown and announced like the operator's.
    select: (entry) => choose(entry),
    setProblem,
  };
}
