// The one way an error reaches the operator: plain Korean. The server's own messages are written
// for developers (English, with codes), so they are never shown; a screen maps the codes it knows
// to its own copy and everything else falls back to the plain lines below. The code itself stays
// available to support in a data-reason attribute, never in the visible text.

import { ApiError } from './api.js';

export const GENERIC_ERROR = '처리하지 못했습니다. 잠시 후 다시 시도하세요.';

const BY_STATUS = {
  400: '요청 내용을 확인한 뒤 다시 시도하세요.',
  401: '권한이 확인되지 않았습니다. 설정에서 연결 상태를 확인하세요.',
  403: '지금은 이 작업을 할 수 없습니다.',
  404: '대상을 찾을 수 없습니다. 화면을 새로 고친 뒤 다시 시도하세요.',
  409: '그사이 내용이 바뀌었습니다. 화면을 새로 고친 뒤 다시 시도하세요.',
  422: '입력 내용을 확인한 뒤 다시 시도하세요.',
  429: '요청이 많아 잠시 멈췄습니다. 잠시 후 다시 시도하세요.',
  503: '연결된 서비스가 응답하지 않습니다. 잠시 후 다시 시도하세요.',
};

// The server's error code, or null (a browser error has none).
export function errorCode(error) {
  if (error instanceof ApiError) return error.error?.code ?? null;
  return error?.error?.code ?? null;
}

// Plain Korean for an error: the screen's own copy for its code, else a line by HTTP status, else
// the generic line. Never the server's message and never the code.
export function errorText(error, copy = {}) {
  const code = errorCode(error);
  if (code && copy[code]) return copy[code];
  if (error instanceof TypeError) return '앱 서버에 연결하지 못했습니다. ICBM이 실행 중인지 확인하세요.';
  const status = error instanceof ApiError ? error.status : error?.status;
  return BY_STATUS[status] ?? GENERIC_ERROR;
}
