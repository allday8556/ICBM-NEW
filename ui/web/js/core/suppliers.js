// Supplier display names for screens that only hold a supplier key (a run, a Product member, an
// order). The names are CONNECT's own (GET /api/v1/connect/suppliers); a key the list does not name
// is shown as it is, and the reserved test-product namespace reads as 테스트 상품.

import { getJson } from './api.js';

const SUPPLIERS = '/api/v1/connect/suppliers';
export const SYNTHETIC_SUPPLIER = 'icbm-synthetic';

// A Map of supplier key -> display name. A failed read is an empty map (keys are shown as they are).
export async function supplierNames() {
  try {
    const listed = await getJson(SUPPLIERS);
    return new Map(listed.map((supplier) => [supplier.supplier_key, supplier.display_name]));
  } catch {
    return new Map();
  }
}

export function supplierName(names, key) {
  if (!key) return '—';
  if (key === SYNTHETIC_SUPPLIER) return '테스트 상품';
  return names?.get(key) ?? key;
}
