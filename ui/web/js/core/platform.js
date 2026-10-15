// The single marketplace-identity renderer. Identity comes from the shell contract
// (integrations/marketplaces/identity.py), never from data embedded in the UI.

import { h } from './dom.js';

const catalog = new Map();

export function setMarketplaces(marketplaces) {
  catalog.clear();
  for (const marketplace of marketplaces) catalog.set(marketplace.key, marketplace);
}

// The marketplace's display name as the shell contract gives it, for plain-text places such as a
// select option; the key itself when the shell names no such marketplace.
export function marketplaceLabel(key) {
  return catalog.get(key)?.label ?? key;
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

// One marketplace account as the seller tells it apart: the marketplace's name and the account's
// own id. The server keeps no store name for an account (ACCOUNT_IDENTITY §6), so the id is the
// stable identity; a position in a list ("계정 1") would move when the list changes.
export function accountLabel(marketplaceKey, accountId) {
  return `${marketplaceLabel(marketplaceKey)} · ${accountId}`;
}
