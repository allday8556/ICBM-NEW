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

export async function getJson(path) {
  const response = await fetch(path, {
    headers: { Accept: 'application/json', [CLIENT_HEADER]: 'icbm-web' },
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
