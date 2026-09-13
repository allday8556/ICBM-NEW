// The only HTTP client in the UI. It talks to this origin's application contracts, nothing else.

const CLIENT_HEADER = 'X-ICBM-Client';

export class ApiError extends Error {
  constructor(status, error, correlationId) {
    super(error?.message ?? `HTTP ${status}`);
    this.status = status;
    this.error = error;
    this.correlationId = correlationId;
  }
}

async function request(method, path, payload) {
  const headers = { Accept: 'application/json', [CLIENT_HEADER]: 'icbm-web' };
  if (payload !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method,
    headers,
    body: payload === undefined ? undefined : JSON.stringify(payload),
    cache: 'no-store',
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, body?.error ?? null, response.headers.get('X-Correlation-ID'));
  }
  return body;
}

export function getJson(path) {
  return request('GET', path);
}

// State-changing calls to this origin's own contracts (the client header is the CSRF guard).
export function sendJson(method, path, payload) {
  return request(method, path, payload);
}
