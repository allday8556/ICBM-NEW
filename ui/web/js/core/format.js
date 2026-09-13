const pad = (value) => String(value).padStart(2, '0');

// 'YYYY-MM-DD' (contract date) or an ISO timestamp -> 'YYYY.MM.DD' in the operator's local time.
export function dotDate(value) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value.replaceAll('-', '.');
  const date = new Date(value);
  return `${date.getFullYear()}.${pad(date.getMonth() + 1)}.${pad(date.getDate())}`;
}

// ISO timestamp -> 'YYYY.MM.DD HH:MM' in the operator's local time.
export function dotDateTime(value) {
  const date = new Date(value);
  return `${dotDate(value)} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
