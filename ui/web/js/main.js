// Boot: load the shell contract, render chrome, then render the routed screen from its contract.
// Flow per screen: UI → application contract (/api/v1/screens/*) → service → state → UI.

import { getJson } from './core/api.js';
import { h } from './core/dom.js';
import { hideHelp, initHelp } from './core/help.js';
import { M0_NOTICE, initInert } from './core/inert.js';
import { closeModal } from './core/modal.js';
import { setMarketplaces } from './core/platform.js';
import { DEFAULT_PAGE, navigate, parseRoute } from './core/router.js';
import { toast } from './core/toast.js';
import { errorState } from './components/states.js';
import { PAGE_BY_KEY, PAGES } from './pages/index.js';

const content = document.getElementById('content');
let renderSequence = 0;

function renderNav() {
  document.getElementById('nav').replaceChildren(
    ...PAGES.map((page) =>
      h(
        'button',
        { type: 'button', class: 'nav-item', 'data-page': page.key, 'aria-label': page.navLabel, onclick: () => navigate(page.key) },
        h('span', { class: 'nav-icon', 'aria-hidden': 'true' }, page.icon),
        h('span', { class: 'nav-label' }, page.navLabel),
      ),
    ),
  );
}

function setActiveNav(key) {
  for (const item of document.querySelectorAll('#nav .nav-item')) {
    if (item.dataset.page === key) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
}

function renderChrome(shell) {
  document.getElementById('operatorInitial').textContent = shell.operator_display_name.slice(0, 1);
  document
    .getElementById('operatorName')
    .replaceChildren(`${shell.operator_display_name}님`, h('small', {}, 'ICBM 로컬 운영'));
  document
    .getElementById('sideFoot')
    .replaceChildren(`v${shell.version} · ${shell.milestone} · ${shell.execution_mode}`, h('br'), '© 2026 ICBM');
  document.getElementById('commandInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      toast('통합 검색', M0_NOTICE);
    }
  });
}

async function show(route) {
  const page = PAGE_BY_KEY.get(route.page) ?? PAGE_BY_KEY.get(DEFAULT_PAGE);
  const sequence = ++renderSequence;
  hideHelp();
  closeModal();
  setActiveNav(page.key);
  document.title = `${page.title} · ICBM`;
  content.setAttribute('aria-busy', 'true');
  let node;
  try {
    node = await page.render({ params: route.params, navigate });
  } catch (error) {
    node = errorState(error);
  }
  if (sequence !== renderSequence) return; // a newer navigation superseded this one
  content.replaceChildren(node);
  content.removeAttribute('aria-busy');
  content.dataset.page = page.key;
  window.scrollTo(0, 0);
}

async function boot() {
  initHelp();
  initInert();
  renderNav();
  try {
    const shell = await getJson('/api/v1/shell');
    setMarketplaces(shell.marketplaces);
    renderChrome(shell);
  } catch (error) {
    content.replaceChildren(errorState(error));
    return;
  }
  window.addEventListener('hashchange', () => show(parseRoute()));
  await show(parseRoute());
}

boot();
