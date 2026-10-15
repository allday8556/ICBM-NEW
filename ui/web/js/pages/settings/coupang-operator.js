// Coupang credential setup in Settings. Credential values travel only in this explicit PUT and
// are committed as one OS-secret-store bundle. Reads return configured/generation status only.

import { getJson, sendJson } from '../../core/api.js';
import { h } from '../../core/dom.js';
import { errorText as plainError } from '../../core/errors.js';
import { toast } from '../../core/toast.js';

const ENDPOINT = '/api/v1/connect/marketplaces/coupang/credentials';

function errorText(error) {
  return plainError(error);
}

export function coupangCredentialsPanel() {
  const title = '쿠팡 Open API 인증';
  const status = h('span', { class: 'chip' }, '확인 중');
  const vendorId = h('input', {
    id: 'coupang-vendor-id',
    type: 'text',
    autocomplete: 'off',
    spellcheck: 'false',
    placeholder: 'Vendor ID',
  });
  const accessKey = h('input', {
    id: 'coupang-access-key',
    type: 'password',
    autocomplete: 'new-password',
    placeholder: 'Access Key',
  });
  const secretKey = h('input', {
    id: 'coupang-secret-key',
    type: 'password',
    autocomplete: 'new-password',
    placeholder: 'Secret Key',
  });
  const save = h(
    'button',
    { type: 'button', class: 'btn blue', 'data-action': 'save-coupang-credentials' },
    'API 정보 저장',
  );

  const show = (view) => {
    status.className = view.configured ? 'chip good' : 'chip';
    status.textContent = view.configured ? '저장됨' : '미설정';
    status.dataset.generation = view.configured ? String(view.credential_generation) : '';
  };
  getJson(ENDPOINT)
    .then(show)
    .catch(() => {
      status.className = 'chip warn';
      status.textContent = '확인 불가';
    });

  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      const view = await sendJson('PUT', ENDPOINT, {
        vendor_id: vendorId.value,
        access_key: accessKey.value,
        secret_key: secretKey.value,
      });
      show(view);
      toast(title, '저장했습니다. 키는 이 PC에 안전하게 저장되며 다시 표시되지 않습니다.');
    } catch (error) {
      toast(title, errorText(error));
    } finally {
      vendorId.value = '';
      accessKey.value = '';
      secretKey.value = '';
      save.disabled = false;
    }
  });

  return h(
    'div',
    { class: 'coupang-credentials' },
    h('div', { class: 'form-row' }, h('label', { for: vendorId.id }, '업체 코드 (Vendor ID)'), vendorId),
    h('div', { class: 'form-row' }, h('label', { for: accessKey.id }, '액세스 키 (Access Key)'), accessKey),
    h('div', { class: 'form-row' }, h('label', { for: secretKey.id }, '시크릿 키 (Secret Key)'), secretKey),
    h('div', { class: 'kv' }, h('span', {}, '저장 상태'), status),
    h('div', { class: 'api-action-row' }, save),
  );
}
