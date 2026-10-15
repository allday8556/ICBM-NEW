// What the Product DB holds about the products of one registration unit, read through its own
// detail read (GET /api/v1/products/{product_group_id}/detail): the collected name, manufacturer,
// origin and brand, as the collection recorded them.
//
// Only a CONFIRMED value is used, and only when every source of every product in the unit states
// the same one: anything else is no value. Nothing here guesses, and nothing here writes.

import { getJson } from '../core/api.js';

const DETAIL = (groupId) => `/api/v1/products/${encodeURIComponent(groupId)}/detail`;
// One read per product per render.
const details = new Map();

export function freshProductFacts() {
  details.clear();
}

function detail(groupId) {
  if (!details.has(groupId)) details.set(groupId, getJson(DETAIL(groupId)).catch(() => null));
  return details.get(groupId);
}

function confirmed(found, key) {
  const sources = found?.member_sources ?? [];
  if (!sources.length) return null;
  const values = sources.map((source) => {
    const fact = (source.facts ?? []).find((item) => item.key === key);
    return fact?.status === 'CONFIRMED' && typeof fact.value === 'string' && fact.value.trim() ? fact.value.trim() : null;
  });
  if (values.some((value) => value === null)) return null;
  return new Set(values).size === 1 ? values[0] : null;
}

function groups(unit) {
  return [...new Set((unit.items ?? []).map((item) => item.product_group_id).filter(Boolean))];
}

// The collected values every product of the unit agrees on, by fact key.
export async function unitFacts(unit, keys) {
  const ids = groups(unit);
  if (!ids.length) return {};
  const found = await Promise.all(ids.map(detail));
  const agreed = {};
  for (const key of keys) {
    const values = found.map((one) => confirmed(one, key));
    if (values.every((value) => value !== null) && new Set(values).size === 1) agreed[key] = values[0];
  }
  return agreed;
}

// The collected product name of the unit: its one product's, or the first one's with how many more.
export async function unitProductName(unit) {
  const ids = groups(unit);
  if (!ids.length) return null;
  const names = (await Promise.all(ids.map(detail))).map((one) => confirmed(one, 'original_name'));
  const known = [...new Set(names.filter(Boolean))];
  if (!known.length) return null;
  return known.length === 1 ? known[0] : `${known[0]} 외 ${known.length - 1}개`;
}
