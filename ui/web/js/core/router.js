// Hash routing: #/<page>?<params>. Every entry point (sidebar, CTA, direct URL) uses navigate().

export const DEFAULT_PAGE = 'dashboard';

export function parseRoute(hash = window.location.hash) {
  const raw = hash.replace(/^#\/?/, '');
  const [path = '', query = ''] = raw.split('?');
  return { page: path || DEFAULT_PAGE, params: new URLSearchParams(query) };
}

export function routeHash(page, params = {}) {
  const entries = Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '');
  const query = new URLSearchParams(entries).toString();
  return `#/${page}${query ? `?${query}` : ''}`;
}

export function navigate(page, params = {}) {
  const next = routeHash(page, params);
  if (window.location.hash === next) window.dispatchEvent(new HashChangeEvent('hashchange'));
  else window.location.hash = next;
}
