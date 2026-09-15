// The single marketplace-identity renderer. Identity comes from the shell contract
// (integrations/marketplaces/identity.py), never from data embedded in the UI.

import { h } from './dom.js';

const catalog = new Map();

export function setMarketplaces(marketplaces) {
  catalog.clear();
  for (const marketplace of marketplaces) catalog.set(marketplace.key, marketplace);
}

export function platformTag(key, { name = false } = {}) {
  const marketplace = catalog.get(key);
  if (!marketplace) return h('span', { class: 'pf' }, key);
  let mark;
  if (marketplace.logo_url) {
    mark = h('img', { class: 'pf-logo', src: marketplace.logo_url, alt: marketplace.label });
  } else {
    // No mark available yet: brand-coloured monogram, as in v29.
    mark = h('span', { class: 'pf-mono', 'aria-hidden': 'true' }, marketplace.label.slice(0, 2));
    mark.style.background = marketplace.brand_color;
  }
  return h('span', { class: 'pf' }, mark, name ? h('span', { class: 'pf-name' }, marketplace.label) : null);
}

// The platform named in text instead of a mark (the settings screen).
export function platformWordmark(key) {
  const marketplace = catalog.get(key);
  return h('span', { class: 'pf-name' }, marketplace ? marketplace.wordmark : key);
}
