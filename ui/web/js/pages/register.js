// 등록관리: the Registration Management screen (M5 PR-F §B).
//
// Everything here is rendered from server-owned state. The page calculates no price, no
// readiness, no duplicate verdict, no option compatibility, no budget, no retry eligibility and
// no capability: each action arrives with the server's own `enabled` verdict and reason code, and
// pressing it calls the contract that decided it. An UNKNOWN outcome is never shown as FAILED, a
// 2xx is never shown as CONFIRMED, and an AUTH brake is never offered an operator resume — the
// server does not offer it, and this page renders only what it was given.
//
// It reuses the v29 panel/chip/table/kv system; the only new class is the requirement list of the
// canary panel. Explanations live behind the existing help icon, never as permanent subtitles.

import { ApiError, getJson, sendJson } from '../core/api.js';
import { fragment, h } from '../core/dom.js';
import { dotDateTime } from '../core/format.js';
import { withHelp } from '../core/help.js';
import { toast } from '../core/toast.js';
import { openModal } from '../core/modal.js';
import { platformTag } from '../core/platform.js';
import { pageHead } from '../components/page-head.js';
import { emptyState, errorState } from '../components/states.js';
import { reviewItemsBlock } from '../components/review-items.js';
import { KIND_LABEL } from '../components/review-counts.js';
import { READ_STATE_TONE } from '../components/registration-status.js';
import { itemImagesEditor } from '../components/item-images.js';
import { listingSyncPanel } from '../components/listing-sync.js';
import { categoryPicker } from '../components/category-picker.js';
import { freshProductFacts, unitFacts, unitProductName } from '../components/product-facts.js';
import { duplicateRow } from '../components/duplicate-row.js';

const SCREEN = '/api/v1/screens/register';
const OVERVIEW = '/api/v1/register/overview';
const CANARY = '/api/v1/register/canary';
const LIVE = '/api/v1/register/live';
// B-UX1: the readiness of every pre-send unit, evaluated by the server now.
const READINESS = '/api/v1/register/readiness';
const READINESS_STATUSES = ['READY', 'REVIEW_REQUIRED', 'BLOCKED', 'DUPLICATE', 'STALE', 'NOT_EVALUATED'];
const READINESS_LABEL = {
  READY: '등록 가능',
  REVIEW_REQUIRED: '확인 필요',
  BLOCKED: '등록 불가',
  DUPLICATE: '중복 확인',
  STALE: '다시 확인',
  NOT_EVALUATED: '점검 전',
};
// B-UX2: what the operator can fix or re-check now, and where (server-owned).
const FIXES = '/api/v1/screens/register/fixes';
const TITLE = '등록관리';
const HELP =
  '수집한 상품을 마켓에 등록하고, 등록 상태를 서버가 판단한 그대로 보여줍니다. 실행 가능 여부는 서버가 결정합니다.';
const CANARY_HELP =
  '실제 마켓에 상품을 보내기 전에 계정 연결, 권한, 등록 결과 확인 경로를 한 번에 점검합니다.';

const STATUS_HELP =
  '등록 상태는 서버가 한 가지 기준으로 판정한 등록중 · 등록성공 · 재확인필요 · 등록실패입니다. 결과를 모르는 등록은 실패로 표시하지 않고, 다시 보내지 않은 채 등록 여부를 확인합니다.';

const LIVE_HELP =
  '실등록 잠금과 실행 권한 상태를 보여줍니다. 상세 기록은 문제를 점검할 때만 펼쳐 보세요.';

const BRAKE_LABEL = { ENGAGED: '잠김', RELEASED: '해제됨' };
const GRANT_LABEL = { ACTIVE: '유효', EXPIRED: '만료됨', REVOKED: '회수됨', EXHAUSTED: '소진됨' };
const STAGE_LABEL = { ASSET: '이미지 업로드', CREATE: '상품 등록', DELETE: '상품 삭제', DISPATCH: '발송처리' };
const PROOF_LABEL = {
  evidence_retention_ready: '작업 기록 보존',
  visual_acceptance_recorded: '화면 검수 완료',
};
const EXECUTION_MODE_LABEL = { DRY_RUN: '안전 모드', LIVE: '실등록 모드' };
const WRITE_STATUS_LABEL = { UNVERIFIED: '확인 필요', VERIFIED: '확인됨', BLOCKED: '사용 불가' };
const CANARY_REQUIREMENT_LABEL = {
  CLEAN_RUNTIME: '실행 환경 확인',
  ACCOUNT_BOUND: '판매 계정 연결',
  AUTH_READY: '마켓 인증',
  WRITE_SCOPE_PROVEN: '상품 쓰기 권한',
  UNIT_PREPARED: '등록할 상품 준비',
  CREATE_ADOPTED: '상품 등록 연결',
  IMAGE_UPLOAD_ADOPTED: '이미지 등록 연결',
  RECONCILE_PATH_ADOPTED: '등록 결과 확인 연결',
  READBACK_ADOPTED: '등록 상품 조회 연결',
  READBACK_EXECUTABLE: '등록 상품 조회 준비',
  PUBLISHED_STATE_PROVABLE: '판매 상태 확인',
  NO_UNRESOLVED_CONFLICT: '미해결 충돌 없음',
  SCOPE_SENDS_ALLOWED: '전송 가능 상태',
  SINGLE_UNIT: '검증 상품 1건 선택',
  ASSET_MUTATION_READY: '이미지 전송 준비',
  CREATE_MUTATION_READY: '상품 등록 준비',
};

export const INTENT_LABEL = {
  PREPARED: '전송 준비됨',
  SENT: '전송됨',
  CONFIRMED: '확인 완료',
  UNKNOWN: '결과 미확인',
  FAILED: '전송 실패',
};

const VERIFICATION_LABEL = {
  NOT_VERIFIED: '읽기 확인 전',
  PASS: '읽기 확인 일치',
  MISMATCH: '읽기 확인 불일치',
};

const OUTCOME_LABEL = {
  APPLIED_PROVEN: '반영 확인됨',
  NOT_APPLIED_PROVEN: '미반영 확인됨',
  UNKNOWN: '결과 미확인',
};

export const PREPARATION_LABEL = {
  DRAFTED: '초안',
  SNAPSHOT_FROZEN: '스냅샷 고정됨',
  INTENT_OPEN: '등록 요청 생성됨',
};

const PAUSE_LABEL = {
  AUTH: '인증 중단',
  POLICY: '정책 중단',
  FAILURE_BUDGET: '연속 실패 중단',
};

const ACTION_LABEL = {
  EVALUATE: '등록 준비 확인',
  FREEZE: '등록 내용 확정',
  CREATE_ENQUEUE: '등록 전송',
  RECONCILE: '등록확인 재시도',
  VERIFY: '읽기 확인',
  RESUME_SCOPE: '전송 재개',
  SETTLE_REJECTION: '거절 확인 · 미등록 처리',
};

const OPERATOR = 'operator';

// Server reason codes rendered as copy. The page never derives a verdict, only its wording.
export const REASON_COPY = {
  // ADR-0031 §4.2, ADR-0030 §7: what the supplier's own source forbids.
  SOURCE_CHANNEL_FORBIDDEN: '공급사가 이 마켓 판매를 금지한 상품입니다',
  SOURCE_CHANNEL_UNRESOLVED: '공급사 판매채널 문구를 확인할 수 없습니다',
  SUPPLIER_NOT_ACTIVE: '공급사 사이트가 아직 시험 단계(RECON)라 등록할 수 없습니다',
  // ADR-0039 §2, §3: the supplier's own policy, not applied by the user's per-supplier choice.
  // A warning that blocks nothing; the restriction is still shown.
  SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED:
    '공급사가 이 마켓 판매를 금지했지만, 판매채널 제한을 적용하지 않도록 설정한 공급처입니다 (사용자 책임)',
  SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED:
    '공급사 판매채널 문구를 확인할 수 없지만, 판매채널 제한을 적용하지 않도록 설정한 공급처입니다 (사용자 책임)',
  'M4_PRICING.PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED':
    '공급사 최저판매가가 있지만, 최저판매가를 적용하지 않도록 설정한 공급처입니다 (사용자 책임)',
  // The same pricing warning as an Item's own pricing reason (no layer prefix).
  PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED:
    '공급사 최저판매가가 있지만, 최저판매가를 적용하지 않도록 설정한 공급처입니다 (사용자 책임)',
  // ADR-0038 §1 (O1a): a quantity offer of a CNY or JPY source is never priced.
  'M4_PRICING.PRICING_SOURCE_CURRENCY_UNSUPPORTED':
    '해외 통화(CNY·JPY) 수량별 가격은 가격을 정할 수 없습니다',
  // ADR-0038 §3, §4 (O2): the landed cost of a 구매대행 product, failing closed.
  'M4_PRICING.PRICING_FX_UNAVAILABLE':
    '환율(송금 보낼때 TTS)이 아직 없어 가격을 정할 수 없습니다. 설정 › 해외구매대행에서 환율을 갱신하세요.',
  'M4_PRICING.PRICING_FX_STALE':
    '환율이 영업일 3일보다 오래되어 가격을 정할 수 없습니다. 설정 › 해외구매대행에서 환율을 갱신하세요.',
  'M4_PRICING.PRICING_OVERSEAS_SHIPPING_UNSET':
    '해외 배송비가 없어 가격을 정할 수 없습니다. 상품의 해외 배송비를 입력하거나 설정 › 해외구매대행에서 기본값을 정하세요.',
  // B-PRICE1: the repin command's refusals.
  REGISTER_DRAFT_REVISION_MOVED: '초안이 바뀌었습니다. 화면을 새로 고친 뒤 다시 시도하세요.',
  REGISTER_REPIN_INTENT_OPEN: '이 초안의 등록 요청이 아직 진행 중이라 가격을 다시 고정할 수 없습니다.',
  REGISTER_DRAFT_ITEM_NOT_PRICED: 'M4가 일부 품목의 가격을 정하지 못해 아무것도 다시 고정하지 않았습니다.',
  REGISTER_READ_QUEUED: '전송 작업이 대기 중입니다.',
  REGISTER_READ_SENDABLE: '전송할 수 있는 상태입니다.',
  REGISTER_READ_SEND_IN_FLIGHT: '전송이 진행 중입니다.',
  REGISTER_READ_AWAITING_VERIFICATION: '마켓 반영이 확인되어 읽기 확인을 기다리고 있습니다.',
  REGISTER_READ_RETRY_SCHEDULED: '자동 재시도가 예약되어 있습니다.',
  REGISTER_READ_VERIFIED: '읽기 확인이 스냅샷과 일치했습니다.',
  REGISTER_READ_OUTCOME_UNKNOWN: '전송 결과를 확인하지 못했습니다. 다시 보내지 않고 등록 여부를 확인합니다.',
  REGISTER_READ_READBACK_MISMATCH: '마켓에 반영된 내용이 스냅샷과 다릅니다.',
  REGISTER_READ_VERIFICATION_OVERDUE: '반영 후 읽기 확인이 기한 안에 끝나지 않았습니다.',
  REGISTER_READ_NOT_APPLIED: '마켓에 반영되지 않은 것이 확인되었습니다.',
  REGISTER_READ_PRE_SEND_FAILED: '전송 전에 거부되어 아무 것도 보내지 않았습니다.',
  REGISTER_READ_STATE_UNCLASSIFIED: '서버가 이 등록 상태를 분류하지 못했습니다. 결함으로 기록되었습니다.',
  // B-STATUS: the latest Attempt's cause and the reconcile result, shown apart.
  SMARTSTORE_HTTP_400: '네이버가 400으로 거절했습니다.',
  SMARTSTORE_CREATE_REJECTED: '네이버가 등록 요청을 거절했습니다 (등록되지 않음).',
  ZERO: '등록 확인: 네이버에서 찾지 못함',
  MULTIPLE: '등록 확인: 후보가 여러 개',
  LOOKUP_UNAVAILABLE: '등록 확인: 조회 불가',
  ERROR: '등록 확인: 조회 오류',
  REGISTER_REJECTION_NOT_PROVEN: '마지막 시도가 네이버의 400 거절이 아니라 미등록으로 처리할 수 없습니다.',
  REGISTER_REJECTION_LOOKUP_MISSING: '거절 뒤 등록확인 재시도에서 "찾지 못함"이 한 번 있어야 미등록으로 처리할 수 있습니다.',
  REGISTER_INTENT_ABSENT: '아직 등록 요청이 만들어지지 않았습니다.',
  REGISTER_INTENT_NOT_SENDABLE: '지금 상태에서는 서버가 전송을 허용하지 않습니다.',
  REGISTER_JOB_ALREADY_QUEUED: '이미 대기 중인 전송 작업이 있습니다.',
  REGISTER_SCOPE_PAUSED: '이 계정 범위의 전송이 중단되어 있습니다.',
  REGISTER_FAILURE_BUDGET_EXHAUSTED: '연속 실패 한도를 넘어 전송이 중단되었습니다.',
  REGISTER_SEND_REQUEST_ABSENT: '고정된 전송 요청이 없습니다.',
  REGISTER_NOT_UNKNOWN: '결과가 확정되어 대조할 대상이 없습니다.',
  REGISTER_NOT_APPLIED: '마켓 반영이 확인되지 않아 읽기 확인을 할 수 없습니다.',
  REGISTER_ALREADY_VERIFIED: '이미 읽기 확인을 마쳤습니다.',
  REGISTER_SCOPE_NOT_PAUSED: '중단된 범위가 없습니다.',
  REGISTER_SCOPE_RESUME_NOT_PERMITTED: '인증 중단은 재인증으로만 풀립니다.',
  REGISTER_ACCOUNT_NOT_BOUND: '판매자 계정 연결이 확인되지 않았습니다.',
  REGISTER_PREFLIGHT_INPUTS_NOT_DURABLE:
    '저장된 등록 준비 정보가 없어 지금 상태를 확인할 수 없습니다.',
  REGISTER_TARGET_POLICY_MISSING: '이 계정의 등록 정책이 아직 설정되지 않았습니다.',
  REGISTER_PREPARATION_ABSENT: '아직 등록 준비 내용이 저장되지 않았습니다.',
  REGISTER_PREFLIGHT_NOT_READY: '등록 준비 확인을 먼저 완료해 주세요.',
  REGISTER_UNIT_ALREADY_FROZEN: '이미 스냅샷이 고정된 단위입니다.',
  REGISTER_FROZEN_METADATA_UNRESOLVED:
    '스냅샷이 고정한 카테고리 메타데이터 리비전을 찾을 수 없어 규칙을 표시하지 않습니다. 현재 리비전으로 대신 표시하지 않습니다.',
  DUPLICATE_EVIDENCE_MISSING: '중복 확인 근거가 없습니다.',
  // ADR-0042 S3b: the store's own catalog decides duplicates.
  DUPLICATE_EVIDENCE_STALE: '스토어 다시 확인 중입니다. 확인이 끝나면 중복 여부를 다시 판단합니다.',
  DUPLICATE_EVIDENCE_UNAVAILABLE: '스토어 상품을 아직 확인하지 못해 중복 여부를 알 수 없습니다.',
  PROVIDER_DUPLICATE_FOUND: '이미 스토어에 같은 판매자코드의 상품이 있습니다.',
  PROVIDER_DUPLICATE_WEAK_SIGNAL: '스토어에 상품명이 같은 상품이 있습니다. 같은 상품인지 확인해 주세요.',
  // ADR-0042 S3b at send time: the store is checked again before a CREATE is sent.
  REGISTER_DUPLICATE_RESYNC_WAIT: '앞 상품 등록 후 스토어를 다시 확인하는 중입니다. 확인이 끝나면 자동으로 이어서 등록합니다.',
  REGISTER_SEND_DUPLICATE_EVIDENCE_STALE: '스토어 확인이 오래되어 등록을 보내지 않았습니다. 스토어를 다시 확인한 뒤 다시 등록해 주세요.',
  REGISTER_SEND_DUPLICATE_EVIDENCE_UNAVAILABLE: '스토어 상품을 확인하지 못해 등록을 보내지 않았습니다. 스토어 확인이 끝난 뒤 다시 등록해 주세요.',
  // ADR-0033 §8: a resolved top or bottom notice the unit's composition does not place. Since G5
  // the SmartStore composition (v3) places both; an older composition revision does not.
  PUBLICATION_GUIDANCE_UNPLACED: '이 상품의 상세페이지 구성은 상·하단 공지를 넣지 못합니다. 등록 정책을 다시 저장한 뒤 준비 내용을 저장하면 공지가 들어갑니다. 이 상품에서 그 공지를 끄면 이 사유는 사라집니다.',
  // ADR-0037 §2: an image over SmartStore's upload limit with no upload-fit variant.
  IMAGE_UPLOAD_FIT_FAILED: '9.5MB 넘는 이미지의 업로드용 압축본이 없습니다. 아직 없다면 준비 내용을 저장하면 다시 만듭니다. 저장 후에도 남으면 줄일 수 없는 이미지이니 다른 이미지를 고르세요.',
  GUIDANCE_TEXT_INVALID: '쓸 수 없는 공지 문구입니다. 상세페이지 단계의 직접 작성 문구를 확인하세요.',
  GUIDANCE_TEXT_TOO_WIDE: '공지 문구 중 템플릿 글상자보다 넓은 줄이 있습니다.',
  GUIDANCE_TEMPLATE_UNKNOWN: '알 수 없는 공지 템플릿입니다.',
  REGISTER_GUIDANCE_CHOICE_INVALID: '직접 작성한 공지는 템플릿과 문구가 모두 있어야 합니다.',
  REGISTER_GUIDANCE_UNAVAILABLE: '상세페이지 공지를 그릴 수 있는 서버 구성 요소가 연결되어 있지 않습니다.',
  ENDPOINT_NOT_ADOPTED: '해당 마켓 연동 계약이 아직 채택되지 않았습니다.',
  PROOF_NOT_AVAILABLE_IN_PROCESS: '실행 중인 앱에서는 증명할 수 없는 항목입니다.',
  ACCOUNT_NOT_BOUND: '판매자 계정 연결이 확인되지 않았습니다.',
  AUTH_NOT_READY: '마켓 인증이 준비되지 않았습니다.',
  WRITE_SCOPE_NOT_PROVEN: '쓰기 권한 범위가 증명되지 않았습니다.',
  NO_PREPARED_INTENT: '전송 준비된 등록 요청이 없습니다.',
  READBACK_SESSION_NOT_WIRED: '등록 결과를 되읽을 수 있는 마켓 세션이 연결되어 있지 않습니다.',
  PUBLISHED_STATE_UNPROVEN: '되읽기 계약이 판매 상태를 증명하지 못해 등록 성공을 확정할 수 없습니다.',
  UNRESOLVED_CONFLICT: '미해결 충돌이 남아 있습니다.',
  EXECUTION_SCOPE_STOPPED: '전송이 중단된 범위입니다.',
  MORE_THAN_ONE_UNIT_SELECTED: '카나리는 한 건만 대상으로 합니다.',
  ASSET_MUTATION_NOT_READY: '이미지 업로드 단계 준비도가 READY가 아닙니다.',
  CREATE_MUTATION_NOT_READY: '등록 전송 단계 준비도가 READY가 아닙니다.',
  RUNTIME_NOT_CLEAN: '실행 중인 코드가 정확한 커밋이 아닙니다.',
  LIVE_EXECUTION_MODE_NOT_LIVE: '실행 모드가 LIVE가 아닙니다.',
  LIVE_PROTECTED_WRITE_BRAKE_ENGAGED: '보호 쓰기 브레이크가 잠겨 있습니다.',
  LIVE_PROTECTED_WRITE_BRAKE_UNREADABLE: '보호 쓰기 브레이크 상태를 읽을 수 없습니다.',
  LIVE_GRANT_NO_MATCHING_ACTIVE_GRANT: '일치하는 유효 실행 권한이 없습니다.',
  LIVE_ENDPOINT_NOT_ADOPTED: '해당 마켓 연동 계약이 아직 채택되지 않았습니다.',
  LIVE_SENDER_NOT_WIRED: '전송 경로가 연결되어 있지 않습니다.',
  LIVE_CANARY_ELIGIBILITY_UNPROVEN: '카나리 대상 적격성이 증명되지 않았습니다.',
  LIVE_RESTORE_PROOF_ABSENT: '이 대상의 복원 증명이 없습니다.',
  LIVE_EVIDENCE_RETENTION_UNPROVEN: '증거 보존이 증명되지 않았습니다.',
  LIVE_VISUAL_ACCEPTANCE_UNRECORDED: '현재 코드의 화면 검수 기록이 없습니다.',
  LIVE_ASSET_ATTEMPT_OWNER_UNREADABLE: '업로드 시도 기록을 읽을 수 없습니다.',
  LIVE_ASSET_REPLAY_UNRESOLVED: '같은 이미지의 미확인 업로드가 남아 있습니다.',
  LIVE_ASSET_REPLAY_APPLIED_REUSE_NOT_ADOPTED: '이미 반영된 이미지의 재사용 경로가 채택되지 않았습니다.',
  LIVE_ASSET_REPLAY_KEY_UNDETERMINABLE: '업로드 대상을 식별할 수 없습니다.',
  LIVE_ASSET_CANDIDATE_NOT_READY: '이미지 등록 준비가 완료되지 않았습니다.',
  LIVE_ASSET_CANDIDATE_DRIFT: '실행 권한 발급 이후 후보가 바뀌었습니다.',
  // 2026-10-15 usability check: a category without reviewed requirements (ADR-0042 will prepare
  // them automatically), and the refusals of the authoring reads.
  REGISTER_CATEGORY_METADATA_MISSING:
    '이 카테고리는 아직 등록 정보(필수 항목·고시)가 준비되지 않았습니다. 설정 › 상품/카테고리에서 준비하거나 다른 카테고리를 선택하세요.',
  CATEGORY_METADATA_MISSING:
    '이 카테고리는 아직 등록 정보(필수 항목·고시)가 준비되지 않았습니다. 설정 › 상품/카테고리에서 준비하거나 다른 카테고리를 선택하세요.',
  CATEGORY_METADATA_UNREVIEWED: '이 카테고리의 등록 정보가 아직 검토되지 않았습니다. 설정 › 상품/카테고리에서 검토하세요.',
  REGISTER_DRAFT_NOT_FOUND: '이 등록 초안을 찾을 수 없습니다. 등록관리에서 다시 열어 주세요.',
  CATEGORY_CATALOG_MISSING: '카테고리 목록이 아직 없습니다. 설정 › 상품/카테고리에서 카테고리 목록을 동기화하세요.',
  CATEGORY_TAXONOMY_NOT_CURRENT: '카테고리 목록이 바뀌었습니다. 카테고리를 다시 선택하세요.',
  CATEGORY_NOT_CURRENT_LEAF: '지금 카테고리 목록에 없는 카테고리입니다. 카테고리를 다시 선택하세요.',
  // What a CREATE job that stopped says, in the operator's words (the read state stays the server's).
  REGISTER_SEND_PREFLIGHT_NOT_READY:
    '보내기 직전에 다시 확인했더니 등록 준비가 더 이상 「등록 가능」이 아닙니다. 등록 준비 확인을 다시 해 주세요.',
  REGISTER_SEND_FINGERPRINT_DRIFT: '등록 내용을 확정한 뒤 상품 정보가 바뀌었습니다. 준비 내용을 다시 확인하고 확정해 주세요.',
  REGISTER_SEND_ASSET_DRIFT: '확정한 뒤 이미지가 바뀌었습니다. 준비 내용을 다시 확인하고 확정해 주세요.',
  REGISTER_WIRE_NOT_SENDABLE: '마켓에 보낼 내용에 빈 칸이 있어 보내지 않았습니다. 미리보기에서 빈 칸을 확인하세요.',
  REGISTER_SEND_SCOPE_MISMATCH: '이 상품의 판매 계정이 전송 범위와 맞지 않아 보내지 않았습니다.',
  REGISTER_SEND_REQUEST_VERSION: '전송 요청 형식이 지금 서버와 맞지 않아 보내지 않았습니다. 등록 내용을 다시 확정해 주세요.',
  REGISTER_SNAPSHOT_MISSING: '확정한 등록 내용을 찾을 수 없어 보내지 않았습니다.',
  REGISTER_NOT_SENDABLE: '지금 상태에서는 보낼 수 없습니다.',
  REGISTER_INTENT_RESERVED_BY_BULK: '일괄 등록이 이 상품을 처리하고 있습니다.',
  REGISTER_UNKNOWN_REQUIRES_RECONCILE: '이전 전송 결과를 모르는 상태라 다시 보내지 않고 등록 여부부터 확인합니다.',
  REGISTER_UNRESOLVED_CONFLICT: '같은 상품의 다른 등록 요청이 남아 있어 보내지 않았습니다.',
  REGISTER_CREATE_NOT_ADOPTED: '상품 등록 연동이 아직 준비되지 않았습니다.',
  REGISTER_CREATE_FAILED: '마켓이 상품 등록을 받지 않았습니다.',
  REGISTER_OUTCOME_UNKNOWN: '전송 결과를 확인하지 못했습니다. 다시 보내지 않고 등록 여부를 확인합니다.',
  SMARTSTORE_SESSION_UNAVAILABLE: '스마트스토어 연결(인증)이 준비되지 않았습니다. 설정에서 계정 연결을 확인하세요.',
  SMARTSTORE_CREATE_WIRE_NOT_SENDABLE: '마켓에 보낼 내용에 빈 칸이 있어 보내지 않았습니다.',
  LIVE_CREATE_STAGE_GATE_NOT_READY: '상품 등록 단계가 아직 실등록 준비가 되지 않았습니다.',
  LIVE_CREATE_EXECUTION_SCOPE_NOT_ACTIVE: '실등록 실행 범위가 열려 있지 않습니다.',
  LIVE_SEND_TIME_TRUTH_MOVED: '보내기 직전에 확인한 내용이 바뀌어 보내지 않았습니다.',
  LIVE_GRANT_NOT_LIVE: '실행 권한이 실등록 모드가 아닙니다.',
};

// Anything the server said that has no Korean wording above is never shown as its English
// message or its code: the operator reads this sentence instead (the code stays an attribute).
export const GENERIC_ERROR = '요청을 처리하지 못했습니다. 잠시 뒤 다시 시도하고, 계속되면 화면을 새로 고쳐 주세요.';
const HANGUL = /[가-힣]/;

export function errorCode(error) {
  return error instanceof ApiError ? error.error?.code ?? null : null;
}

// The Korean sentence for a failed request: the code's own wording, the page's own Korean
// message (a check this page made before sending), or the generic sentence.
export function errorCopy(error, copy = REASON_COPY) {
  const code = errorCode(error);
  if (code && copy[code]) return copy[code];
  if (code && REASON_COPY[code]) return REASON_COPY[code];
  if (!(error instanceof ApiError) && error instanceof Error && HANGUL.test(error.message)) return error.message;
  return GENERIC_ERROR;
}

export function kv(label, value) {
  return h('div', { class: 'kv' }, h('span', {}, label), h('b', {}, value ?? '—'));
}

export function chip(text, tone) {
  return h('span', { class: tone ? `chip ${tone}` : 'chip' }, text);
}

export function reason(code) {
  return code ? h('div', { class: 'mini', 'data-reason': code }, REASON_COPY[code] ?? '확인이 필요한 항목입니다.') : null;
}

export function table(headers, rows) {
  return h(
    'table',
    { class: 'table' },
    h('thead', {}, h('tr', {}, ...headers.map((label) => h('th', {}, label)))),
    h('tbody', {}, ...rows),
  );
}

function won(amount) {
  return amount === null || amount === undefined ? '—' : `${amount.toLocaleString('ko-KR')}원`;
}

// ADR-0039 §2, §3: a warning that a supplier's sales policy is not applied names the supplier and
// quotes the supplier's own words. The warning is rendered at once with its static copy; the
// server's note for the Item (the supplier, its policy now, its words) then words it, and only
// when the note says the same thing — otherwise the static copy stays. Nothing is ever hidden
// (SP-04), and the page judges nothing: whether a warning exists is the server's reason.
const POLICY_NOTE = (itemId) => `/api/v1/products/items/${encodeURIComponent(itemId)}/sales-policy-note`;
const CHANNEL_WARNINGS = new Set(['SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED', 'SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED']);
const MINIMUM_WARNINGS = new Set([
  'PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED',
  'M4_PRICING.PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED',
]);
const CHANNELS_NOT_APPLIED = '이 공급처의 판매정책을 적용하지 않도록 설정했습니다 (사용자 책임)';
const MINIMUM_NOT_APPLIED = '이 공급처의 최저판매가를 적용하지 않도록 설정했습니다 (사용자 책임)';
// One read per Item per render: every warning of the same Item shares it.
const policyNotes = new Map();

export function freshPolicyNotes() {
  policyNotes.clear();
}

function policyNote(itemId) {
  if (!policyNotes.has(itemId)) policyNotes.set(itemId, getJson(POLICY_NOTE(itemId)).catch(() => null));
  return policyNotes.get(itemId);
}

function policyAmount(minimum) {
  return minimum.currency === 'KRW'
    ? `${Number(minimum.amount).toLocaleString('ko-KR')}원`
    : sourcePrice(minimum.currency, minimum.amount);
}

// The quoted wording of one policy warning, or null when the note does not say the same thing.
export function policyWarningCopy(code, note) {
  if (!note?.supplier_key) return null;
  const name = note.supplier_name || note.supplier_key;
  const channels = note.sales_channels;
  if (CHANNEL_WARNINGS.has(code)) {
    if (note.apply_channel_restrictions !== false || !channels) return null;
    if (code === 'SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED') {
      if (channels.status !== 'CONFIRMED' || !channels.quote) return null;
      return `${name}의 판매채널 제한: “${channels.quote}” — ${CHANNELS_NOT_APPLIED}`;
    }
    if (channels.status !== 'REVIEW_REQUIRED') return null;
    return channels.quote
      ? `${name}의 판매채널 문구를 확인할 수 없습니다: “${channels.quote}” — ${CHANNELS_NOT_APPLIED}`
      : `${name}의 판매채널 문구를 확인할 수 없습니다 — ${CHANNELS_NOT_APPLIED}`;
  }
  if (MINIMUM_WARNINGS.has(code)) {
    const minimum = note.minimum_sale_price;
    if (note.apply_minimum_price !== false || !minimum) return null;
    if (minimum.status === 'CONFIRMED' && minimum.amount && minimum.currency) {
      return `${name} 최저판매가 ${policyAmount(minimum)} (공급사 표기: “${minimum.label}”) — ${MINIMUM_NOT_APPLIED}`;
    }
    if (minimum.status !== 'REVIEW_REQUIRED') return null;
    return minimum.quote
      ? `${name} 최저판매가 문구를 확인할 수 없습니다: “${minimum.quote}” — ${MINIMUM_NOT_APPLIED}`
      : `${name} 최저판매가 문구를 확인할 수 없습니다 — ${MINIMUM_NOT_APPLIED}`;
  }
  return null;
}

// The Item a policy warning is about: given by the caller (an Item row), or read from the
// preflight reason's subject — the Item itself for a source gate, `item:{id}:…` for a price.
function policyItem(reason, itemId) {
  if (itemId) return itemId;
  const subject = reason.subject ?? '';
  if (CHANNEL_WARNINGS.has(reason.code)) return subject || null;
  return subject.startsWith('item:') ? subject.slice(5).split(':')[0] || null : null;
}

// Words one rendered policy warning with the supplier's own words, once the note arrives.
export function quotePolicyWarning(node, reason, itemId) {
  if (!CHANNEL_WARNINGS.has(reason.code) && !MINIMUM_WARNINGS.has(reason.code)) return node;
  const item = policyItem(reason, itemId);
  if (!item) return node;
  node.setAttribute('data-policy-item', item);
  node.setAttribute('data-policy-note', 'pending');
  policyNote(item).then((note) => {
    const text = policyWarningCopy(reason.code, note);
    if (!text) {
      node.setAttribute('data-policy-note', 'static');
      return;
    }
    node.textContent = text;
    node.setAttribute('title', text);
    node.setAttribute('data-supplier', note.supplier_key);
    node.setAttribute('data-policy-note', 'quoted');
  });
  return node;
}

// One reason exactly as its owner returned it (B-UX1): the code is shown, and its status, subject
// and areas travel as attributes. Nothing here judges a reason.
function reasonNode(tag, reason, itemId) {
  return quotePolicyWarning(
    h(
      tag,
      {
        class: 'mini',
        'data-reason': reason.code,
        'data-reason-status': reason.status,
        'data-areas': (reason.areas ?? []).join(' '),
        title: REASON_COPY[reason.code] ?? '확인이 필요한 항목입니다.',
      },
      REASON_COPY[reason.code] ?? '확인이 필요한 항목입니다.',
    ),
    reason,
    itemId,
  );
}

export function statusChip(status, codes, reasons, itemId) {
  if (!status) return '—';
  const structured = reasons ?? [];
  return fragment(
    chip(READINESS_LABEL[status] ?? '확인 필요', status === 'READY' ? 'good' : status === 'BLOCKED' ? 'bad' : 'warn'),
    ...(structured.length
      ? structured.map((reason) => reasonNode('span', reason, itemId))
      : (codes ?? []).map((code) => h('span', { class: 'mini', 'data-reason': code }, REASON_COPY[code] ?? '확인이 필요한 항목입니다.'))),
  );
}

// B-UX1: a unit's reasons grouped under the first area the server gave each; a reason is listed
// once, and an area the server could not classify keeps its real code under UNCLASSIFIED.
export function reasonGroups(reasons, labels) {
  const groups = new Map();
  for (const reason of reasons) {
    const area = (reason.areas ?? [])[0] ?? 'UNCLASSIFIED';
    if (!groups.has(area)) groups.set(area, []);
    groups.get(area).push(reason);
  }
  return [...groups.entries()].map(([area, members]) =>
    h(
      'div',
      { class: 'register-reason-area', 'data-area': area },
      h('span', { class: 'mini' }, labels?.[area] ?? '기타 확인사항'),
      ...members.map((reason) => reasonNode('div', reason)),
    ),
  );
}

// B-UX2: the fix-only projection. A row only says where to act; the move is to that surface, and
// nothing is fixed from here. Rows without an action are counted, never offered.
function fixesPanel(found, ctx) {
  if (!found) return null;
  if (found.unavailable) {
    return h('div', { class: 'register-fixes', 'data-fixes': 'UNAVAILABLE' }, reason(found.unavailable));
  }
  const go = (row) => {
    if (row.surface?.startsWith('SETTINGS_')) {
      ctx.navigate('settings');
      return;
    }
    const unit = row.draft_id
      ? document.querySelector(`.register-unit[data-draft='${CSS.escape(row.draft_id)}']`)
      : null;
    const section = row.surface === 'REGISTER_IMAGES' ? unit?.querySelector("[data-editor-section='images']") : null;
    (section ?? unit)?.scrollIntoView({ block: 'start' });
  };
  const rows = found.fixes.map((row) =>
      h(
        'div',
        {
          class: 'register-fix',
          'data-fix': row.code,
          'data-actionability': row.actionability,
          'data-surface': row.surface,
          'data-source': row.source,
        },
        chip(row.actionability_label, row.actionability === 'FIX_AVAILABLE' ? 'warn' : ''),
        h('span', { class: 'mini', 'data-reason': row.code }, REASON_COPY[row.code] ?? '확인이 필요한 항목입니다.'),
        h(
          'button',
          { type: 'button', class: 'btn', 'data-action': 'GO_TO_FIX', onclick: () => go(row) },
          row.surface_label,
        ),
      ),
    );
  return h(
    'div',
    { class: 'register-fixes', 'data-fixes': found.projection_version },
    h(
      'details',
      { open: found.fixes.length <= 5 ? true : null },
      h('summary', { class: 'supplier-head-row' }, h('b', {}, '확인이 필요한 항목'), chip(`${found.fixes.length}건`)),
      ...rows,
    ),
  );
}

// B-UX1: the readiness of the whole pre-send population, as the server counted it now.
function readinessPanel(readiness) {
  if (!readiness) return null;
  const areas = (readiness.areas ?? []).filter((area) => area.units > 0);
  const notEvaluated = Object.entries(readiness.not_evaluated ?? {});
  return h(
    'div',
    {
      class: 'register-readiness',
      'data-readiness': readiness.summary_version,
      'data-population': String(readiness.population),
    },
    h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 준비 현황'), chip(`전체 ${readiness.population}`)),
    h(
      'div',
      { class: 'supplier-head-row' },
      ...READINESS_STATUSES.filter((status) => (readiness.statuses?.[status] ?? 0) > 0).map((status) =>
        h(
          'span',
          { class: 'mini', 'data-status-count': status },
          `${READINESS_LABEL[status]} ${readiness.statuses?.[status] ?? 0}`,
        ),
      ),
    ),
    notEvaluated.length
      ? h(
          'div',
          { class: 'supplier-head-row' },
          ...notEvaluated.map(([code, count]) =>
            h('span', { class: 'mini', 'data-not-evaluated': code }, `${REASON_COPY[code] ?? '점검할 수 없는 항목'} ${count}`),
          ),
        )
      : null,
    areas.length
      ? h(
          'div',
          { class: 'supplier-head-row' },
          ...areas.map((area) =>
            h(
              'span',
              { class: 'mini', 'data-area-count': area.area, title: area.codes.join(', ') },
              `${area.label} ${area.units}`,
            ),
          ),
        )
      : null,
  );
}

// A source-currency amount is the page's own decimal text, shown in its currency, never converted.
const SOURCE_CURRENCY_FORMAT = { CNY: (amount) => `¥${amount}`, JPY: (amount) => `${amount}円` };

function sourcePrice(currency, amount) {
  return SOURCE_CURRENCY_FORMAT[currency]?.(amount) ?? `${amount} ${currency}`;
}

export function itemRow(item) {
  const assets = item.publication_assets ?? [];
  return h(
    'tr',
    { 'data-item': item.item_id, 'data-price-current': String(item.price_pin_current) },
    h(
      'td',
      {},
      `품목 ${item.ordinal}`,
      ...(item.option_keys ?? []).map((key) => h('span', { class: 'mini', 'data-option': key }, key)),
    ),
    h(
      'td',
      {},
      won(item.sale_price_krw),
      // ADR-0039 §3: a supplier minimum the user chose not to apply is shown beside the price.
      item.minimum_applied === false
        ? h(
            'span',
            { class: 'mini', 'data-minimum-applied': 'false' },
            item.stated_minimum_sale_price_krw === null || item.stated_minimum_sale_price_krw === undefined
              ? ' · 최저판매가 미적용 (사용자 설정)'
              : ` · 최저판매가 ${won(item.stated_minimum_sale_price_krw)} 미적용 (사용자 설정)`,
          )
        : null,
      // ADR-0038 §4: a source-currency price shows what it was made from, as the server recorded it.
      item.source_currency
        ? h(
            'span',
            { class: 'mini', 'data-price-basis': item.source_currency },
            ` · 원가 ${sourcePrice(item.source_currency, item.source_price)} · 환율(TTS) ${item.fx_publication_date ?? '—'} 기준`,
          )
        : null,
    ),
    h(
      'td',
      {},
      won(item.current_sale_price_krw),
      item.price_pin_current === false ? chip('고정가 아님', 'warn') : null,
    ),
    h('td', {}, statusChip(item.base_status, item.base_reason_codes, item.base_reasons, item.item_id)),
    h('td', {}, statusChip(item.pricing_status, item.pricing_reason_codes, item.pricing_reasons, item.item_id)),
    h(
      'td',
      { 'data-assets': String(assets.length) },
      ...assets.map((asset, index) =>
        h(
          'span',
          { class: 'mini', 'data-asset': asset.sha256, 'data-qa': asset.qa_verdict ?? 'NONE' },
          `${asset.role === 'REPRESENTATIVE' ? '대표 이미지' : '이미지'} ${index + 1}`,
          asset.upload_fit ? uploadFitLabel(asset.upload_fit) : null,
        ),
      ),
    ),
  );
}

// ADR-0037 §3: the image is uploaded as its upload-fit variant; the server says which one.
function uploadFitLabel(fit) {
  const mb = (fit.byte_size / 1_000_000).toFixed(1);
  return h(
    'span',
    { class: 'chip', 'data-upload-fit': fit.source_sha256 },
    `업로드용 압축본 · ${fit.width}×${fit.height} · ${mb}MB`,
  );
}

function fieldRow(field) {
  return h(
    'li',
    { 'data-field': field.key, 'data-provided': field.provided ? 'true' : 'false' },
    // The marketplace's own Korean name; the wire key stays only as an attribute and a tooltip.
    h('span', { title: field.key }, fieldName(field)),
    chip(field.provided ? '입력됨' : '없음', field.provided ? 'good' : field.required ? 'bad' : 'warn'),
    field.required ? chip('필수') : null,
  );
}

export function categoryBlock(category) {
  const fields = [...category.attributes, ...category.notice_fields];
  return h(
    'div',
    { class: 'register-category', 'data-category': category.category_id },
    kv('카테고리', category.category_id),
    category.metadata_unavailable_reason
      ? h(
          'div',
          { class: 'note', 'data-reason': category.metadata_unavailable_reason },
          REASON_COPY[category.metadata_unavailable_reason] ?? '확인이 필요한 항목입니다.',
        )
      : null,
    category.notice_type ? kv('정보고시', category.notice_type_label ?? category.notice_type) : null,
    category.options_supported === null
      ? null
      : kv('옵션', category.options_supported ? `최대 ${category.max_options}개` : '미지원'),
    fields.length ? h('ul', { class: 'requirement-list' }, ...fields.map(fieldRow)) : null,
  );
}

function field(label, name, value, type = 'input') {
  const control = h(type, { name, value: value ?? '', rows: type === 'textarea' ? '3' : null });
  if (type === 'textarea') control.value = value ?? '';
  return h('label', { class: 'kv' }, h('span', {}, label), control);
}

// A notice field the collection states exactly (2026-10-15 usability check): the field, the
// marketplace's own label it must carry, and the collected fact that fills it. Only these exact
// pairs are offered, and only as an editable value marked 수집값; nothing else is filled in.
const COLLECTED_NOTICE = [
  ['manufacturer', null, 'manufacturer'],
  ['producer', ['제조업소', '생산자'], 'manufacturer'],
  ['producer', ['제조국', '제조국(원산지, 가공지 등이 다를 경우)'], 'origin'],
];
const COLLECTED_ATTRIBUTE = [['brand', null, 'brand']];
const COLLECTED_KEYS = ['manufacturer', 'origin', 'brand'];

function collectedFact(table, rule) {
  const row = table.find(([key, labels]) => key === rule.key && (!labels || labels.includes(rule.label)));
  return row ? row[2] : null;
}

// A category attribute is recorded by the operator under its own key; no captured provider data
// names it. The common ones are written out in Korean here; any other keeps its key.
const ATTRIBUTE_LABEL = {
  brand: '브랜드',
  color: '색상',
  size: '사이즈',
  material: '소재',
  modelName: '모델명',
  manufacturer: '제조사',
  origin: '원산지',
};

// The name a field is shown with: the marketplace's own label, else a common attribute's Korean
// name, else its key.
function fieldName(rule) {
  return rule.label ?? ATTRIBUTE_LABEL[rule.key] ?? rule.key;
}

// The authoring form of one provider-listing unit (§27). It collects the operator's own inputs
// and sends them; it computes no readiness and stores nothing of its own — the server keeps them.
export function authoringForm(unit, onDone) {
  const authored = unit.authored;
  const inputs = authored?.inputs ?? {};
  // 2026-10-15: the category is searched by name; the leaf id is what the form keeps.
  const picker = categoryPicker({
    marketplaceKey: unit.marketplace_key,
    value: inputs.category?.category_id,
    onChange: () => {
      loadMetadata().catch(metadataFailed);
    },
  });
  const categoryControl = picker.input;
  const dynamicFields = h('div', { class: 'register-authoring-fields' });
  // A note only while something is missing (reportMissing); otherwise an empty, hidden holder.
  const missingNote = h('div', { 'data-role': 'missing-required', role: 'alert', hidden: true });
  const fieldsPart = h('div', { class: 'register-authoring-fields-part' }, missingNote, dynamicFields);
  const optionFields = h('div', { class: 'register-option-fields' });
  const fieldControls = new Map();
  let metadata = null;

  function metadataFailed(error) {
    metadata = null;
    fieldControls.clear();
    dynamicFields.replaceChildren();
    const text = errorCopy(error);
    picker.setProblem(text);
    toast('카테고리 등록 정보를 불러오지 못했습니다', text);
  }

  // A field the marketplace fills itself when it is omitted ("상품상세 참조", a stated default) is
  // never a field the operator must type, whatever its required badge. A boolean is chosen, not
  // typed, and an integer is sent as a number: the server never coerces a value's type.
  function authoredField(rule, kind, current) {
    const mustType = rule.required && !rule.omitted_default;
    const valueType = rule.value_type ?? 'TEXT';
    const name = fieldName(rule);
    // The marketplace's own Korean label; the wire key is only an attribute and a tooltip.
    const caption = h('span', { title: rule.key, 'data-field-key': rule.key }, mustType ? `${name} (필수)` : name);
    let control;
    if (valueType === 'BOOLEAN') {
      const select = h('select', { name: `${kind}.${rule.key}` },
        h('option', { value: '' }, '—'),
        h('option', { value: 'true' }, '예'),
        h('option', { value: 'false' }, '아니오'));
      select.value = current?.value === true ? 'true' : current?.value === false ? 'false' : '';
      control = h('label', { class: 'kv' }, caption, select);
    } else {
      const input = h('input', {
        name: `${kind}.${rule.key}`,
        value: current?.value === undefined ? '' : String(current.value),
      });
      control = h('label', { class: 'kv' }, caption, input);
      const placeholder = {
        YEAR_MONTH: 'yyyy-MM', DATE: 'yyyy-MM-dd', INTEGER: '정수', LONG: '정수',
      }[valueType];
      if (placeholder) input.placeholder = placeholder;
    }
    control.setAttribute('data-field-key', rule.key);
    const input = control.querySelector('input, select');
    let reference = null;
    if (rule.detail_page_reference_allowed) {
      reference = h('input', {
        type: 'checkbox',
        'data-detail-reference': kind,
        'data-field-key': rule.key,
        'aria-label': `${name} 상세페이지 참조`,
      });
      reference.checked = current?.detail_page_reference === true;
      const sync = () => {
        input.disabled = reference.checked;
        input.required = mustType && !reference.checked;
        if (reference.checked) input.value = '';
      };
      reference.addEventListener('change', sync);
      control.append(h('span', { class: 'mini' }, reference, ' 상세페이지 참조'));
      sync();
    } else {
      input.required = mustType;
    }
    input.addEventListener('input', () => input.removeAttribute('aria-invalid'));
    fieldControls.set(`${kind}:${rule.key}`, { input, reference, current, valueType, rule, label: control, prefilled: null });
    return control;
  }

  // 수집값: an empty field with an exact collected fact is filled with it, visibly marked, and
  // stays the operator's to change. A changed value is the operator's own again.
  async function prefill(found) {
    let facts;
    try {
      facts = await unitFacts(unit, COLLECTED_KEYS);
    } catch {
      return;
    }
    if (metadata !== found) return;
    const offer = (kind, table) => {
      for (const rule of kind === 'notice' ? found.notice_fields : found.attributes) {
        const control = fieldControls.get(`${kind}:${rule.key}`);
        if (!control || control.current || control.valueType !== 'TEXT') continue;
        if (control.reference?.checked || control.input.value.trim()) continue;
        const fact = collectedFact(table, rule);
        const value = fact ? facts[fact] : null;
        if (!value) continue;
        control.input.value = value;
        control.prefilled = value;
        const mark = h(
          'span',
          { class: 'chip info', 'data-prefill': 'collected', title: '수집한 상품 정보에서 가져온 값입니다. 고칠 수 있습니다.' },
          '수집값',
        );
        control.label.append(mark);
        control.input.addEventListener('input', () => {
          if (control.input.value.trim() === control.prefilled) return;
          control.prefilled = null;
          mark.remove();
        });
      }
    };
    offer('notice', COLLECTED_NOTICE);
    offer('attribute', COLLECTED_ATTRIBUTE);
  }

  function optionRow(dimension, values) {
    const dimensionInput = h('input', {
      value: dimension,
      placeholder: '예: 색상, 사이즈',
      'data-option-dimension': 'true',
    });
    return h(
      'div',
      { class: 'register-option-row' },
      h('label', { class: 'kv' }, h('span', {}, '옵션명'), dimensionInput),
      ...unit.items.map((item) => h(
        'label',
        { class: 'kv' },
        h('span', {}, `품목 ${item.ordinal}`),
        h('input', {
          value: values[item.item_id] ?? '',
          'data-option-item': item.item_id,
          placeholder: '옵션값',
        }),
      )),
    );
  }

  function renderOptions(found) {
    const existing = inputs.options ?? {};
    const dimensions = [...new Set(
      Object.values(existing).flatMap((values) => Object.keys(values)),
    )].sort();
    optionFields.replaceChildren();
    if (unit.items.length <= 1 || (!found.options_supported && dimensions.length === 0)) return;
    const rows = dimensions.length ? dimensions : [''];
    const valuesFor = (dimension) => Object.fromEntries(
      unit.items.map((item) => [item.item_id, existing[item.item_id]?.[dimension] ?? '']),
    );
    const rowHost = h('div', { class: 'register-option-rows' },
      ...rows.map((dimension) => optionRow(dimension, valuesFor(dimension))));
    optionFields.append(
      h('div', { class: 'supplier-head-row' }, h('b', {}, '옵션 입력'),
        chip(`${unit.items.length}/${found.max_options}개 품목`)),
      rowHost,
    );
    if (found.options_supported) {
      const add = h('button', {
        type: 'button', class: 'btn', 'data-action': 'ADD_OPTION_DIMENSION',
      }, '옵션 항목 추가');
      add.disabled = rowHost.children.length >= found.max_option_dimensions;
      add.addEventListener('click', () => {
        rowHost.append(optionRow('', {}));
        add.disabled = rowHost.children.length >= found.max_option_dimensions;
      });
      optionFields.append(add);
    }
  }

  function renderMetadata(found) {
    metadata = found;
    fieldControls.clear();
    hideMissing();
    const controls = [];
    if (found.notice_type) {
      controls.push(kv('정보고시 유형', found.notice_type_label ?? found.notice_type));
    }
    for (const rule of found.attributes) {
      controls.push(authoredField(rule, 'attribute', inputs.attributes?.[rule.key]));
    }
    for (const rule of found.notice_fields) {
      controls.push(authoredField(rule, 'notice', inputs.notices?.[rule.key]));
    }
    dynamicFields.replaceChildren(...controls);
    renderOptions(found);
    prefill(found);
  }

  // One read per category: a save pressed while the first read is still in flight waits for that
  // same read, so the fields are built once and what the operator typed is never rebuilt away.
  let pending = null;
  async function loadMetadata() {
    const categoryId = categoryControl.value.trim();
    if (!categoryId) {
      metadata = null;
      dynamicFields.replaceChildren();
      return null;
    }
    if (metadata?.category_id === categoryId) return metadata;
    if (pending?.categoryId === categoryId) return pending.read;
    const read = getJson(
      `/api/v1/register/drafts/${unit.draft_id}/authoring-metadata/${encodeURIComponent(categoryId)}`,
    ).then((found) => {
      // A category chosen meanwhile has its own read; this one no longer owns the fields.
      if (categoryControl.value.trim() === categoryId) {
        picker.setProblem(null);
        renderMetadata(found);
      }
      return found;
    }).finally(() => {
      if (pending?.read === read) pending = null;
    });
    pending = { categoryId, read };
    return read;
  }

  function hideMissing() {
    missingNote.hidden = true;
    missingNote.className = '';
    missingNote.replaceChildren();
  }

  // The required fields still empty, in the order they are shown.
  function missingRequired() {
    return [...fieldControls.values()].filter(
      (control) => control.input.required && !control.input.disabled && !control.input.value.trim(),
    );
  }

  // 준비 내용 만들기 never does nothing silently: the first empty required field is brought into
  // view (the editor moves to its step), focused, and every missing one is named in Korean.
  function reportMissing(missing) {
    for (const control of fieldControls.values()) control.input.removeAttribute('aria-invalid');
    for (const control of missing) control.input.setAttribute('aria-invalid', 'true');
    const names = missing.map((control) => fieldName(control.rule));
    missingNote.replaceChildren(
      h('b', {}, `필수 항목 ${names.length}개가 비어 있습니다`),
      h('ul', {}, ...names.map((name) => h('li', {}, name))),
    );
    missingNote.hidden = false;
    missingNote.className = 'note';
    const first = missing[0].input;
    if (form.reveal) form.reveal(first);
    else first.scrollIntoView({ block: 'center' });
    first.focus();
    toast('필수 항목을 채워 주세요', names.join(', '));
  }

  const head = h('div', { class: 'supplier-head-row' }, h('b', {}, '등록 준비 입력'),
    authored ? chip('저장됨', 'good') : chip('저장 필요', 'warn'));
  const nameField = field('상품명', 'name', inputs.name?.value);
  // B-DETAIL: the selected detail images come first; the text after them is optional when the
  // unit has detail images (the server decides, never this page).
  const detailField = field(
    '상세 본문 (상세 이미지 뒤, 상세 이미지가 있으면 선택)',
    'detail_body',
    inputs.detail_body,
    'textarea',
  );
  const submit = h(
    'button',
    {
      type: 'submit',
      class: 'btn blue',
      'data-action': 'SAVE_PREPARATION',
      'data-unit': unit.unit_ref,
    },
    authored ? '준비 내용 저장' : '준비 내용 만들기',
  );
  const form = h(
    'form',
    // The browser's own validation cannot reach a field on a hidden step; this form names what
    // is missing itself (reportMissing).
    { class: 'register-authoring', 'data-preparation': authored?.preparation_id ?? '', novalidate: true },
    head,
    picker.el,
    nameField,
    fieldsPart,
    optionFields,
    detailField,
    submit,
  );
  // The editor page places these by step and binds them back to this form (the `form` attribute);
  // the save reads them through the same references either way.
  form.parts = { head, category: picker.el, name: nameField, fields: fieldsPart, options: optionFields, detail: detailField, submit };
  // What a save sends. `omit` ('category' or 'name') sends that field as not chosen: the AI apply
  // fills it next (saveForApply).
  async function saveBody(omit = null) {
    let metadata = null;
    try {
      metadata = await loadMetadata();
    } catch (error) {
      if (omit !== 'category') throw error;
    }
    const approved = omit === 'category' ? null : metadata;
    const values = Object.fromEntries(new FormData(form).entries());
    const collectFields = (kind, rules) => Object.fromEntries(rules.flatMap((rule) => {
      const control = fieldControls.get(`${kind}:${rule.key}`);
      if (control?.reference?.checked) {
        return [[rule.key, {
          detail_page_reference: true,
          provenance: control.current?.provenance ?? 'OPERATOR_CONFIRMED',
        }]];
      }
      const raw = control?.input.value.trim() ?? '';
      let value = raw;
      if (control?.valueType === 'BOOLEAN') value = raw === 'true';
      else if (['INTEGER', 'LONG'].includes(control?.valueType) && /^-?[0-9]+$/.test(raw)) {
        // Only an integer a JavaScript number holds exactly is sent as a number. A larger one
        // stays the operator's own text, which the server refuses as the wrong type — it is
        // never rounded into another value.
        const number = Number(raw);
        if (Number.isSafeInteger(number) && BigInt(raw) === BigInt(number)) value = number;
      }
      // A collected value saved as it was filled is the source's fact; any other is the
      // operator's own.
      const provenance = control?.prefilled && raw === control.prefilled
        ? 'SOURCE_FACT'
        : control?.current?.provenance ?? 'OPERATOR_CONFIRMED';
      return raw ? [[rule.key, { value, provenance }]] : [];
    }));
    const attributes = collectFields('attribute', approved?.attributes ?? []);
    const notices = collectFields('notice', approved?.notice_fields ?? []);
    const options = {};
    for (const row of optionFields.querySelectorAll('.register-option-row')) {
      const dimension = row.querySelector('[data-option-dimension]').value.trim();
      if (!dimension) continue;
      for (const input of row.querySelectorAll('[data-option-item]')) {
        const value = input.value.trim();
        if (!value) continue;
        const itemId = input.getAttribute('data-option-item');
        options[itemId] ??= {};
        options[itemId][dimension] = value;
      }
    }
    return {
      actor: OPERATOR,
      item_ids: unit.items.map((item) => item.item_id),
      inputs: {
        category: approved
          ? {
              category_id: approved.category_id,
              mapping_revision: approved.mapping_revision,
              taxonomy_revision: approved.taxonomy_revision,
              confirmation: 'OPERATOR_CONFIRMED',
            }
          : null,
        name: values.name && omit !== 'name' ? { value: values.name } : null,
        tags: inputs.tags ?? [],
        attributes,
        notices,
        options,
        // The composition is the target policy's, whichever category is chosen.
        detail_composition_revision: metadata?.detail_composition_revision ?? null,
        // Under a profile that places detail images an empty body is still a composition.
        detail_body: (metadata?.detail_sections ?? []).includes('DETAIL_IMAGES')
          ? (values.detail_body ?? '')
          : (values.detail_body || null),
        detail_sections: metadata?.detail_sections ?? inputs.detail_sections ?? ['BODY'],
        // ADR-0033 §5 (G4): the product editor's 상·하단 공지 choice; any other surface sends the
        // stored choice back unchanged, so a save never resets it.
        guidance: form.guidanceValue ? form.guidanceValue() : (inputs.guidance ?? null),
      },
    };
  }

  // ADR-0026 §7, ADR-0029 §5 (AIC-04): an AI value is applied to a saved preparation only, as
  // AI_SUGGESTION naming the result shown. Before anything is saved (2026-10-15 usability check),
  // the AI apply first saves what the operator has entered — without the field the AI fills — and
  // then applies to that preparation; the operator's own later save is still what confirms it.
  if (!authored) {
    form.saveForApply = async (omit) =>
      sendJson('POST', '/api/v1/register/preparations', { ...(await saveBody(omit)), draft_id: unit.draft_id });
  }
  if (inputs.category?.category_id) loadMetadata().catch(metadataFailed);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await loadMetadata();
      const missing = missingRequired();
      if (missing.length) {
        reportMissing(missing);
        return;
      }
      hideMissing();
      const body = await saveBody();
      if (authored) {
        await sendJson('POST', `/api/v1/register/preparations/${authored.preparation_id}`, body);
      } else {
        await sendJson('POST', '/api/v1/register/preparations', { ...body, draft_id: unit.draft_id });
      }
      toast('등록 준비 내용을 저장했습니다');
      onDone();
    } catch (error) {
      if (errorCode(error) === 'REGISTER_CATEGORY_METADATA_MISSING') picker.setProblem(errorCopy(error));
      toast('저장하지 못했습니다', errorCopy(error));
    }
  });
  return form;
}

export function preflightBlock(unit, labels) {
  if (!unit.preflight) {
    return h(
      'div',
      { class: 'register-preflight', 'data-preflight': 'UNAVAILABLE' },
      kv('등록 준비 확인', '확인 전'),
      reason(unit.preflight_unavailable_reason),
    );
  }
  const preflight = unit.preflight;
  return h(
    'div',
    { class: 'register-preflight', 'data-preflight': preflight.status },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('b', {}, '등록 준비 확인'),
      chip(READINESS_LABEL[preflight.status] ?? '확인 필요', preflight.status === 'READY' ? 'good' : 'warn'),
      preflight.fingerprint_matches_snapshot === false ? chip('저장 후 내용 변경됨', 'warn') : null,
    ),
    ...((preflight.reasons ?? []).length
      ? reasonGroups(preflight.reasons, labels)
      : preflight.reason_codes.map((code) => h('div', { class: 'mini', 'data-reason': code }, REASON_COPY[code] ?? '확인이 필요한 항목입니다.'))),
  );
}

export function attemptRow(attempt) {
  return h(
    'tr',
    {},
    h('td', {}, String(attempt.attempt_no)),
    h('td', {}, OUTCOME_LABEL[attempt.outcome] ?? '진행 중'),
    h('td', { 'data-error-class': attempt.error_class ?? '', 'data-error-code': attempt.error_code ?? '' }, attempt.error_code ? REASON_COPY[attempt.error_code] ?? '마켓 응답을 확인해 주세요.' : '—'),
    h('td', {}, attempt.started_at ? dotDateTime(attempt.started_at) : '—'),
  );
}

export function scopeBlock(scope) {
  const paused = scope.state === 'PAUSED';
  return h(
    'div',
    { class: 'register-scope', 'data-scope-state': scope.state },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('b', {}, '전송 브레이크'),
      chip(paused ? (PAUSE_LABEL[scope.pause_reason] ?? '중단') : '전송 가능', paused ? 'bad' : 'good'),
    ),
    kv('연속 실패', String(scope.consecutive_failures)),
    scope.paused_at ? kv('중단 시각', dotDateTime(scope.paused_at)) : null,
    scope.resumed_at ? kv('마지막 재개', dotDateTime(scope.resumed_at)) : null,
  );
}

async function run(unit, action, button, onDone) {
  button.disabled = true;
  try {
    const result = await call(unit, action.action, unit.intent?.intent_id);
    if (action.action === 'CREATE_ENQUEUE') {
      followSend(result, unit.draft_id, button, onDone);
      return;
    }
    toast(`${ACTION_LABEL[action.action] ?? '요청'} 완료`);
    onDone();
  } catch (error) {
    toast(`${ACTION_LABEL[action.action] ?? '요청'}: 처리하지 못했습니다`, errorCopy(error));
    button.disabled = false;
  }
}

// 2026-10-15 usability check: 등록 전송 only queues a CREATE job, so its answer is never shown as
// done. The page says the request was accepted, follows the job the server named until it stops,
// and then shows the unit's read state as the server derives it now, with its reason in Korean.
// It decides nothing: the job state and the read state are the owners' own.
const JOB = (jobId) => `/api/v1/system/jobs/${encodeURIComponent(jobId)}`;
const SEND_POLL_MS = 1500;
const SEND_POLL_LIMIT = 40;
const JOB_STOPPED = new Set(['SUCCEEDED', 'DEAD', 'RETRY_SCHEDULED']);

function wait(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function sendOutcome(result, draftId) {
  let job = null;
  if (result?.job_id) {
    for (let attempt = 0; attempt < SEND_POLL_LIMIT; attempt += 1) {
      try {
        job = await getJson(JOB(result.job_id));
      } catch {
        job = null;
        break;
      }
      if (JOB_STOPPED.has(job.state)) break;
      await wait(SEND_POLL_MS);
    }
  }
  let read = null;
  if (draftId && result?.intent_id) {
    try {
      const units = await getJson(`/api/v1/register/units/${encodeURIComponent(draftId)}`);
      read = units.find((unit) => unit.intent?.intent_id === result.intent_id)?.intent?.read_state ?? null;
    } catch {
      read = null;
    }
  }
  return { job, read };
}

function outcomeCopy({ job, read }) {
  const cause = job?.last_error_code ?? read?.cause_code ?? null;
  const why = cause ? REASON_COPY[cause] ?? '마켓 응답을 확인해 주세요.' : read ? REASON_COPY[read.reason_code] ?? '' : '';
  if (!job) {
    return { tone: 'warn', title: '전송 결과를 아직 확인하지 못했습니다', why: why || '잠시 뒤 등록 상태에서 확인하세요.' };
  }
  if (job.state === 'DEAD') return { tone: 'bad', title: '등록 전송이 중단되었습니다', why: why || '전송하지 못했습니다.' };
  if (job.state === 'RETRY_SCHEDULED') {
    return { tone: 'warn', title: '자동 재시도가 예약되었습니다', why: why || REASON_COPY.REGISTER_READ_RETRY_SCHEDULED };
  }
  if (job.state === 'SUCCEEDED') return { tone: 'info', title: '전송 작업을 마쳤습니다', why };
  return { tone: 'warn', title: '전송이 아직 진행 중입니다', why: '잠시 뒤 화면을 새로 고쳐 등록 상태를 확인하세요.' };
}

async function followSend(result, draftId, button, onDone) {
  toast('전송 요청을 접수했습니다', '전송 결과를 확인하는 중입니다.');
  const holder = button.closest('.register-action, td') ?? button.parentElement;
  const note = h(
    'div',
    { class: 'note', role: 'status', 'data-role': 'send-outcome', 'data-state': 'PENDING', 'data-job': result?.job_id ?? '' },
    '전송 요청을 접수했습니다. 결과를 확인하는 중입니다…',
  );
  holder?.append(note);
  const found = await sendOutcome(result, draftId);
  const copy = outcomeCopy(found);
  const reasonCode = found.job?.last_error_code ?? found.read?.cause_code ?? found.read?.reason_code ?? '';
  const body = () =>
    h(
      'div',
      { class: 'send-outcome', 'data-role': 'send-outcome-body' },
      h('b', {}, copy.title),
      found.read ? h('div', { class: 'kv' }, h('span', {}, '등록 상태'), readStateChip(found.read)) : null,
      copy.why ? h('div', { class: 'mini', 'data-reason': reasonCode }, copy.why) : null,
    );
  const refresh = () =>
    h('button', { type: 'button', class: 'btn', 'data-action': 'refresh-after-send', onclick: () => onDone() }, '화면 새로 고침');
  note.dataset.state = found.job?.state ?? 'UNREAD';
  note.replaceChildren(body(), refresh());
  toast(copy.title, copy.why);
  openModal({
    eyebrow: '등록 전송',
    title: copy.title,
    body: h('div', { 'data-role': 'send-outcome-modal', 'data-state': found.job?.state ?? 'UNREAD' }, body()),
    footer: [refresh()],
  });
}

function call(unit, action, intentId) {
  if (action === 'RESUME_SCOPE') {
    return sendJson('POST', '/api/v1/register/scopes/resume', {
      marketplace_key: unit.scope.marketplace_key,
      marketplace_account_id: unit.scope.marketplace_account_id,
      actor: OPERATOR,
      reason: 'OPERATOR-REVIEWED',
    });
  }
  if (action === 'EVALUATE' || action === 'FREEZE') {
    const step = action === 'EVALUATE' ? 'evaluate' : 'freeze';
    return sendJson('POST', `/api/v1/register/preparations/${unit.authored.preparation_id}/${step}`, {
      actor: OPERATOR,
    });
  }
  const path = {
    CREATE_ENQUEUE: 'create',
    RECONCILE: 'reconcile',
    VERIFY: 'verify',
    SETTLE_REJECTION: 'settle-rejection',
  }[action];
  return sendJson('POST', `/api/v1/register/intents/${intentId}/${path}`, {});
}

// ADR-0014 §28.5: the server's read state, as it labelled it. Only 등록실패 is red.
export function readStateChip(read, problem) {
  if (!read) {
    return h('span', { class: 'chip bad', 'data-reason': problem ?? 'REGISTER_READ_STATE_UNCLASSIFIED' }, '분류 오류');
  }
  return h('span', { class: `chip ${READ_STATE_TONE[read.state] ?? ''}`.trim(), 'data-read-state': read.state }, read.label);
}

async function runStatus(entry, action, button, onDone) {
  button.disabled = true;
  try {
    const result = await call({}, action, entry.intent_id);
    if (action === 'CREATE_ENQUEUE') {
      followSend(result, entry.draft_id, button, onDone);
      return;
    }
    toast(`${ACTION_LABEL[action] ?? '요청'} 완료`);
    onDone();
  } catch (error) {
    toast(`${ACTION_LABEL[action] ?? '요청'}: 처리하지 못했습니다`, errorCopy(error));
    button.disabled = false;
  }
}

function statusRow(entry, onDone) {
  const read = entry.read_state;
  const action = read?.action ?? null;
  const button = action
    ? h(
        'button',
        {
          type: 'button',
          class: read.action_enabled ? 'btn blue' : 'btn',
          disabled: !read.action_enabled,
          'data-action': action,
          'data-intent': entry.intent_id,
          onclick: () => runStatus(entry, action, button, onDone),
        },
        ACTION_LABEL[action] ?? action,
      )
    : null;
  // B-STATUS: only when the server says the UNKNOWN is a provider 400 rejection it can settle.
  const settle = read?.rejection_settleable
    ? h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'SETTLE_REJECTION',
          'data-intent': entry.intent_id,
          onclick: () => runStatus(entry, 'SETTLE_REJECTION', settle, onDone),
        },
        ACTION_LABEL.SETTLE_REJECTION,
      )
    : null;
  const causes = [read?.attempt_cause_code, read?.cause_code]
    .filter((code, index, all) => code && all.indexOf(code) === index)
    .map((code) => h('span', { class: 'mini', 'data-cause': code }, REASON_COPY[code] ?? '마켓 응답을 확인해 주세요.'));
  return h(
    'tr',
    { 'data-intent': entry.intent_id, 'data-read-row': read?.state ?? 'UNCLASSIFIED' },
    h('td', {}, entry.product_name ?? '—'),
    h('td', {}, readStateChip(read, entry.read_state_problem)),
    h(
      'td',
      {},
      read
        ? h('span', { class: 'mini', 'data-reason': read.reason_code }, REASON_COPY[read.reason_code] ?? '마켓 응답을 확인해 주세요.')
        : reason(entry.read_state_problem),
      ...causes,
    ),
    h('td', {}, read?.requested_at ? dotDateTime(read.requested_at) : '—'),
    h('td', {}, read?.last_confirmed_at ? dotDateTime(read.last_confirmed_at) : '—'),
    h('td', {}, String(read?.confirmation_attempts ?? 0)),
    h(
      'td',
      {},
      button ?? (settle ? null : '—'),
      button && !read.action_enabled ? reason(read.action_reason_code) : null,
      settle,
    ),
  );
}

// The detail panel of the registration status card (ADR-0014 §28.5): every recent Intent with the
// server's read state, its reason, request and confirmation times and the one action it offers.
function registrationStatusPanel(status, onDone) {
  if (!status || !status.entries.length) return null;
  const counts = status.counts;
  return h(
    'section',
    { class: 'panel register-status', 'data-role': 'register-status' },
    h(
      'div',
      { class: 'supplier-head-row' },
      withHelp(h('h2', { class: 'panel-title' }, '등록 진행 상태'), STATUS_HELP),
    ),
    h(
      'div',
      { class: 'register-summary' },
      kv(status.labels.REGISTERING, String(counts.registering)),
      kv(status.labels.REGISTERED, String(counts.registered)),
      kv(status.labels.RECHECK_REQUIRED, String(counts.recheck_required)),
      kv(status.labels.FAILED, String(counts.failed)),
    ),
    table(
      ['상품명', '상태', '사유', '요청 시각', '마지막 확인', '확인 시도', '동작'],
      status.entries.map((entry) => statusRow(entry, onDone)),
    ),
  );
}

export function actionCell(unit, action, onDone) {
  const button = h(
    'button',
    {
      type: 'button',
      class: action.enabled ? 'btn blue' : 'btn',
      disabled: !action.enabled,
      'data-action': action.action,
      'data-draft': unit.draft_id,
      'data-unit': unit.unit_ref,
      onclick: () => run(unit, action, button, onDone),
    },
    ACTION_LABEL[action.action] ?? action.action,
  );
  return h('div', { class: 'register-action' }, button, action.enabled ? null : reason(action.reason_code));
}

// B-PREVIEW: what this frozen Snapshot would send, as the server projected it. Every value is
// inserted as text; provider URLs never arrive (the server redacts them) and the detail body is
// its structure, never HTML.
export function previewBlock(unit) {
  if (!unit.snapshot) return null;
  const holder = h('div', { class: 'register-preview', 'data-preview': unit.snapshot.registration_snapshot_id });
  const button = h('button', { type: 'button', class: 'btn', 'data-action': 'PREVIEW_SNAPSHOT' }, '스마트스토어 미리보기');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const view = await getJson(
        `/api/v1/register/snapshots/${encodeURIComponent(unit.snapshot.registration_snapshot_id)}/preview`,
      );
      holder.replaceChildren(previewView(view));
    } catch (error) {
      toast('미리보기를 불러오지 못했습니다', errorCopy(error));
      button.disabled = false;
    }
  });
  holder.append(button);
  return holder;
}

// ADR-0033 G6: the frozen notices of a guidance plan (composition v3), each as the image the
// renderer draws, in its order: the top notices before the detail images, the bottom ones after
// the body. The image is the Detail Guidance owner's local route the server named, never a
// provider URL. An empty placement (and every v1/v2 preview) renders nothing.
const GUIDANCE_PLACEMENT_LABEL = { top: '상단 공지', bottom: '하단 공지' };

function guidanceSlots(slots, placement) {
  if (!slots.length) return null;
  const label = GUIDANCE_PLACEMENT_LABEL[placement];
  return h(
    'div',
    { class: 'dg-resolved', 'data-role': `preview-guidance-${placement}` },
    ...slots.map((slot) => {
      const what =
        slot.source === 'PRODUCT'
          ? '이 상품 공지'
          : slot.kind === 'PERIOD'
            ? '기간 공지'
            : slot.kind === 'STANDING'
              ? '상시 공지'
              : '공지';
      const period =
        slot.kind === 'PERIOD' && slot.starts_at && slot.ends_at
          ? `${dotDateTime(slot.starts_at)} ~ ${dotDateTime(slot.ends_at)}`
          : null;
      return h(
        'figure',
        { 'data-role': 'preview-guidance-slot', 'data-placement': slot.placement, 'data-sha256': slot.sha256, 'data-kind': slot.kind ?? '' },
        h('img', { src: slot.image_url, alt: `${label} · ${what}` }),
        h(
          'figcaption',
          {},
          h('span', { class: 'chip' }, label),
          h('span', { class: slot.source === 'PRODUCT' ? 'chip info' : 'chip' }, what),
          period ? h('span', { class: 'mini', 'data-role': 'preview-guidance-period' }, period) : null,
          h('span', { class: 'mini' }, slot.provider_asset_prepared ? '마켓 업로드 준비됨' : '마켓 업로드 없음'),
        ),
      );
    }),
  );
}

function previewView(view) {
  const detail = view.detail;
  return h(
    'div',
    { class: 'register-preview-view', 'data-projected': String(view.projected) },
    h('div', { class: 'supplier-head-row' }, h('b', {}, '스마트스토어 미리보기'),
      view.projected
        ? chip(view.sendable ? '보낼 수 있음' : '빈 칸 있음', view.sendable ? 'good' : 'warn')
        : chip('투영 거절', 'bad')),
    view.refusal_code ? reason(view.refusal_code) : null,
    kv('상품명', view.name ?? '—'),
    kv('판매가', view.sale_prices_krw.map((price) => won(price)).join(', ') || '—'),
    kv('카테고리', view.category_id ? '선택됨' : '—'),
    ...view.gaps.map((gap) => h('div', { class: 'mini', 'data-gap': 'true' }, gap)),
    table(
      ['이미지 역할', '순서', '이미지', '마켓 업로드'],
      view.images.map((image, index) =>
        h('tr', { 'data-preview-image': image.sha256 },
          h('td', {}, image.role === 'REPRESENTATIVE' ? '대표 이미지' : image.role === 'DETAIL' ? '상세 이미지' : '추가 이미지'),
          h('td', {}, image.position === null ? '—' : String(image.position)),
          h('td', {}, `이미지 ${index + 1}`),
          h('td', {}, image.provider_asset_prepared ? '준비됨' : '없음')),
      ),
    ),
    detail
      ? h('div', { class: 'register-preview-detail', 'data-detail-builder': detail.builder },
          kv('상세 구획', detail.sections.join(' → ') || '—'),
          guidanceSlots(detail.guidance?.top ?? [], 'top'),
          kv('상세 이미지', `${detail.image_slots.length}장`),
          ...detail.paragraphs.map((paragraph) => h('p', { class: 'mini' }, paragraph)),
          guidanceSlots(detail.guidance?.bottom ?? [], 'bottom'))
      : null,
    view.document.length
      ? table(
          ['보낼 항목', '값'],
          view.document.map((field, index) =>
            h('tr', { 'data-field': field.path },
              h('td', {}, `등록 정보 ${index + 1}`),
              h('td', {}, field.redacted ? '(마켓 참조 · 표시 안 함)' : field.value ?? '—')),
          ),
        )
      : null,
    view.not_sent.length
      ? table(
          ['보내지 않는 항목', '값'],
          view.not_sent.map((row, index) =>
            h('tr', { 'data-not-sent': row.path },
              h('td', {}, `제외 정보 ${index + 1}`),
              h('td', {}, row.value === 'DETAIL_PAGE_REFERENCE' ? '상세페이지 참조' : row.value)),
          ),
        )
      : null,
  );
}

// B-PRICE1: when the server says a pinned price is no longer M4's current one, the operator may
// ask M4 to price again and re-pin the Draft. The page names no price; the server decides all.
const REPIN_REASONS = new Set(['PRICING_SNAPSHOT_MISSING', 'PRICING_SNAPSHOT_SUPERSEDED']);

export function repinBlock(unit, onDone) {
  // Offered when the server says the pin is not M4's current price, or M4's own pricing readiness
  // names a missing or superseded snapshot; the server decides everything else.
  const moved = unit.items.some(
    (item) =>
      item.price_pin_current === false ||
      (item.pricing_reason_codes ?? []).some((code) => REPIN_REASONS.has(code)),
  );
  if (!moved) return null;
  const button = h('button', { type: 'button', class: 'btn', 'data-action': 'REPIN_DRAFT' }, '가격 다시 고정');
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const moved = await sendJson('POST', `/api/v1/register/drafts/${unit.draft_id}/repin`, {
        actor: OPERATOR,
        expected_draft_revision: unit.draft_revision,
      });
      const count = moved.items.filter((item) => item.repinned).length;
      toast(count ? `${count}개 품목의 가격을 다시 고정했습니다.` : 'M4 가격이 그대로라 바뀐 것이 없습니다.');
      onDone();
    } catch (error) {
      toast('가격을 다시 고정하지 못했습니다', errorCopy(error));
      button.disabled = false;
    }
  });
  return h('div', { class: 'register-repin', 'data-repin': unit.draft_id }, button);
}

// B-EDITOR (Issue #127): one unit is a workspace over its owners, never a second product record.
// Four sections — 상품 정보, 이미지, 가격, 등록 준비 — each act through their own owner's command, and
// every section stays rendered: the bar only moves to one, it hides nothing (Gate-3 selectors).
const EDITOR_SECTIONS = [
  ['info', '상품 정보'],
  ['images', '이미지'],
  ['price', '가격'],
  ['ready', '등록 준비'],
];

function editorSection(unit, key, label, ...children) {
  return h(
    'div',
    { class: 'register-editor-section', 'data-editor-section': key, 'data-draft': unit.draft_id },
    h('h3', { class: 'panel-subtitle' }, label),
    ...children,
  );
}

function editorNav(panel) {
  return h(
    'div',
    { class: 'register-editor-nav', role: 'navigation', 'aria-label': '편집 구역' },
    ...EDITOR_SECTIONS.map(([key, label]) =>
      h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-editor-go': key,
          onclick: () => panel.querySelector(`[data-editor-section='${key}']`)?.scrollIntoView({ block: 'start' }),
        },
        label,
      ),
    ),
  );
}

function imagesSection(unit, onDone) {
  // Images are chosen before a Snapshot freezes them; afterwards they are only shown.
  const editable = !unit.snapshot && !unit.intent;
  return editorSection(
    unit,
    'images',
    '이미지',
    editable ? null : h('span', { class: 'mini' }, '스냅샷이 고정되어 이미지는 보기만 합니다.'),
    ...unit.items.map((item) =>
      h(
        'div',
        { class: 'register-item-images', 'data-item': item.item_id },
        h('b', {}, `품목 ${item.ordinal}`),
        itemImagesEditor(item.item_id, { editable, onChanged: onDone }),
      ),
    ),
  );
}

function unitPanel(unit, onDone, labels) {
  const intent = unit.intent;
  const panel = h(
    'section',
    {
      class: 'panel register-unit',
      'data-draft': unit.draft_id,
      'data-unit': unit.unit_ref,
      'data-account': unit.marketplace_account_id,
      'data-marketplace': unit.marketplace_key,
      'data-preparation': unit.preparation,
    },
    h(
      'div',
      { class: 'supplier-head-row' },
      h('h2', { class: 'panel-title' }, '등록 상세'),
      chip(PREPARATION_LABEL[unit.preparation] ?? unit.preparation),
      intent ? readStateChip(intent.read_state, intent.read_state_problem) : null,
    ),
    intent ? kv('요청 상태', INTENT_LABEL[intent.state] ?? intent.state) : null,
    h('div', { class: 'kv' }, h('span', {}, '판매 채널'), platformTag(unit.marketplace_key)),
    intent ? kv('검증 상태', VERIFICATION_LABEL[intent.verification_state] ?? intent.verification_state) : null,
    intent?.marketplace_product_id ? kv('마켓 상품번호', intent.marketplace_product_id) : null,
    unit.published_state ? kv('마켓 노출 상태', unit.published_state) : null,
    unit.conflicting_intents.length ? kv('충돌 중인 요청', String(unit.conflicting_intents.length)) : null,
  );
  panel.append(
    editorNav(panel),
    editorSection(
      unit,
      'info',
      '상품 정보',
      unit.category ? categoryBlock(unit.category) : null,
      unit.snapshot ? null : authoringForm(unit, onDone),
    ),
    imagesSection(unit, onDone),
    editorSection(
      unit,
      'price',
      '가격',
      repinBlock(unit, onDone),
      unit.item_facts_unavailable_reason ? reason(unit.item_facts_unavailable_reason) : null,
      table(
        ['품목', '판매가', '현재 판매가', '기본정보', '가격', '이미지'],
        unit.items.map(itemRow),
      ),
    ),
    editorSection(
      unit,
      'ready',
      '등록 준비',
      // ADR-0042 S3b (§0 a4): the duplicate row, decided by the store's last complete sync.
      duplicateRow(unit, onDone),
      preflightBlock(unit, labels),
      previewBlock(unit),
      intent && intent.attempts.length
        ? table(['시도', '결과', '확인 내용', '시작'], intent.attempts.map(attemptRow))
        : null,
      scopeBlock(unit.scope),
      h('div', { class: 'supplier-actions' }, ...unit.actions.map((action) => actionCell(unit, action, onDone))),
    ),
  );
  return panel;
}

// Gate 2 G2-C (ADR-0016 §7): one account's REGISTER ReviewItems — execution states and each
// current preparation's candidate preflight — with both producers' coverage. The server derives
// every item; a resolution is re-derived by its owner and never changes a REGISTER fact.
function reviewScopeLabel(item) {
  const scope = item.scope;
  if (scope.intent_id) return '등록 요청';
  if (scope.preparation_id) return '등록 준비';
  return '계정 범위';
}

function reviewPanel(marketplaceKey, accountId) {
  const block = reviewItemsBlock({
    kindLabel: KIND_LABEL,
    emptyCopy: '이 판매 계정에 기록된 등록 검토 항목이 없습니다.',
    errorCopy: (code) => REASON_COPY[code] ?? GENERIC_ERROR,
    scopeLabel: reviewScopeLabel,
  });
  const panel = h(
    'section',
    { class: 'panel register-review', 'data-role': 'register-review', 'data-account': accountId },
    h('div', { class: 'supplier-head-row' }, h('h2', { class: 'panel-title' }, '검토 항목'), platformTag(marketplaceKey)),
  );
  block({ marketplace_key: marketplaceKey, marketplace_account_id: accountId }).then((found) => {
    if (found) panel.append(found);
  });
  return panel;
}

function canaryPanel(canary) {
  const blocked = canary.verdict === 'BLOCKED';
  const satisfied = canary.requirements.filter((item) => item.satisfied).length;
  return h(
    'section',
    { class: 'panel register-canary', 'data-canary': canary.verdict },
    h(
      'div',
      { class: 'supplier-head-row' },
      withHelp(h('h2', { class: 'panel-title' }, '실등록 사전 점검'), CANARY_HELP),
      chip(blocked ? '차단됨' : '준비됨', blocked ? 'bad' : 'good'),
    ),
    kv('실행 상태', EXECUTION_MODE_LABEL[canary.execution_mode] ?? '확인 필요'),
    kv('마켓 권한', WRITE_STATUS_LABEL[canary.write_status] ?? '확인 필요'),
    h(
      'details',
      { class: 'register-safety-details' },
      h('summary', {}, `세부 점검 ${satisfied}/${canary.requirements.length} 충족`),
      h(
        'ul',
        { class: 'requirement-list' },
        ...canary.requirements.map((item) =>
          h(
            'li',
            { 'data-requirement': item.requirement, 'data-satisfied': item.satisfied ? 'true' : 'false' },
            h('span', {}, CANARY_REQUIREMENT_LABEL[item.requirement] ?? '준비 상태 확인'),
            chip(item.satisfied ? '충족' : '미충족', item.satisfied ? 'good' : 'warn'),
            item.satisfied ? null : reason(item.reason_code),
          ),
        ),
      ),
    ),
  );
}

function grantRow(grant) {
  const readiness = grant.readiness
    ? fragment(
        chip(READINESS_LABEL[grant.readiness.verdict] ?? '확인 필요', grant.readiness.verdict === 'READY' ? 'good' : 'bad'),
        h(
          'ul',
          { class: 'requirement-list' },
          ...grant.readiness.missing.map((code) => h('li', { 'data-missing': code }, REASON_COPY[code] ?? '확인이 필요한 항목입니다.')),
        ),
      )
    : h('span', { class: 'mini' }, '등록 단계 준비도는 카나리 준비도에서 확인합니다.');
  return h(
    'tr',
    { 'data-grant': grant.grant_id, 'data-grant-state': grant.state, 'data-stage': grant.stage },
    h('td', {}, STAGE_LABEL[grant.stage] ?? grant.stage),
    h('td', {}, chip(GRANT_LABEL[grant.state] ?? grant.state, grant.state === 'ACTIVE' ? 'good' : 'warn')),
    h('td', {}, '연결됨'),
    h('td', {}, `${grant.budget_used} / ${grant.budget_max}`),
    h('td', {}, dotDateTime(grant.expires_at)),
    h('td', {}, readiness),
  );
}

// The pre-LIVE safety state (ADR-0018 §9, §10): the protected-write brake, the unit-independent
// proofs and every grant with the ASSET readiness the server derives for it. Read-only.
function livePanel(live) {
  const brake = live.brake;
  const active = live.grants.filter((grant) => grant.state === 'ACTIVE').length;
  return h(
    'section',
    { class: 'panel register-live', 'data-role': 'live-panel' },
    withHelp(h('h2', { class: 'panel-title' }, '보호 쓰기 · 실행 권한'), LIVE_HELP),
    h(
      'div',
      { 'data-role': 'live-brake', 'data-brake-state': brake.state },
      kv('보호 쓰기 브레이크', BRAKE_LABEL[brake.state] ?? brake.state),
      chip(BRAKE_LABEL[brake.state] ?? '확인 필요', brake.state === 'RELEASED' ? 'good' : 'bad'),
      brake.recorded
        ? null
        : h('div', { class: 'mini', 'data-reason': 'BRAKE_NOT_RECORDED' }, '기록이 없어 기본값인 잠김으로 판단합니다.'),
      brake.reason_code ? h('div', { class: 'mini', 'data-reason': brake.reason_code }, REASON_COPY[brake.reason_code] ?? '안전 잠금 사유가 기록되어 있습니다.') : null,
    ),
    h(
      'details',
      { class: 'register-safety-details' },
      h('summary', {}, `실행 권한 상세 · 사용 가능 ${active}건`),
      h(
        'ul',
        { class: 'requirement-list', 'data-role': 'live-proofs' },
        ...Object.entries(live.proofs).map(([name, proven]) =>
          h(
            'li',
            { 'data-proof': name, 'data-satisfied': proven ? 'true' : 'false' },
            h('span', {}, PROOF_LABEL[name] ?? '안전 상태 확인'),
            chip(proven ? '확인됨' : '확인 필요', proven ? 'good' : 'warn'),
          ),
        ),
      ),
      h(
        'div',
        { 'data-role': 'live-grants' },
        live.grants.length
          ? table(['단계', '상태', '계정 연결', '사용량', '만료', '준비도'], live.grants.map(grantRow))
          : h('div', { class: 'mini', 'data-grant-state': 'NONE' }, '발급된 실행 권한이 없습니다.'),
      ),
    ),
  );
}

// ---------------------------------------------------------------- v29 layout (2026-10-07)
//
// v29's 등록관리 composition: five cards, the 등록 이력 row, the 등록 대기 목록 beside 플랫폼별 등록
// 설정, then 등록상품 관리, 실패 / 재시도 and the safety panels. Small queues keep their workspaces
// inline; large queues stay list-first and open the dedicated editor instead of expanding a long
// diagnostic page. A count the system has no source for reads 데이터 없음; controls without a real
// contract are omitted from the operator surface.
const NO_DATA = '데이터 없음';

function noDataCell(label) {
  return h('td', { class: 'no-data', 'data-no-data': label }, NO_DATA);
}

function goTo(section) {
  document.querySelector(`[data-section='${section}']`)?.scrollIntoView({ block: 'start' });
}

// Each card is a count the server holds, or 데이터 없음 when no read states it. A count whose read
// has not answered yet says so instead (`undefined`), and is replaced when it does.
function registerCards(screen, status, readiness) {
  const category = (readiness?.areas ?? []).find((area) => area.area === 'CATEGORY');
  const cards = [
    ['waiting', '◷', '등록 대기', screen.registration_candidates_total, 'pending'],
    ['registered', '✓', '등록 완료', screen.registrations_total, 'products'],
    // The status's own labels; without a status read the cards keep v29's titles.
    ['failed', '×', status?.labels?.FAILED ?? '실패', status?.counts?.failed, 'failed'],
    ['recheck', '↻', status?.labels?.RECHECK_REQUIRED ?? '재시도 필요', status?.counts?.recheck_required, 'failed'],
    ['category', '◇', '카테고리 확인', readiness === undefined ? undefined : category?.units ?? null, 'pending'],
  ];
  return h(
    'div',
    { class: 'grid5 page-kpis', 'data-role': 'register-cards' },
    ...cards.map(([key, icon, label, count, section]) =>
      h(
        'button',
        {
          type: 'button',
          class: key === 'waiting' ? 'kpi state-card active' : 'kpi state-card',
          'data-card': key,
          onclick: () => goTo(section),
        },
        h('span', { class: 'kpi-top' }, h('span', { class: 'kicon', 'aria-hidden': 'true' }, icon), label),
        typeof count === 'number'
          ? h('strong', { 'data-total': String(count) }, count.toLocaleString('ko-KR'))
          : count === undefined
            ? h('strong', { class: 'no-data', 'data-loading': label }, '…')
            : h('strong', { class: 'no-data', 'data-no-data': label }, NO_DATA),
      ),
    ),
  );
}

function utilityRow(overview) {
  return h(
    'div',
    { class: 'register-utility-row' },
    h('span', { class: 'mini', 'data-role': 'paused-scopes' }, `중단된 범위 ${overview.paused_scopes.length}`),
    h('button', { type: 'button', class: 'btn', 'data-action': 'open-history', onclick: () => goTo('failed') }, '등록 이력'),
  );
}

// The registration editor (2026-10-08 owner UX): a row opens this choice, and the chosen work opens
// in a new tab. 상품수정 needs a registration the server has confirmed; the choice itself sends
// nothing. A unit's ref moves as it is authored and frozen (the server's UnitView), so the link also
// carries its Items: the editor follows the same unit through those moves.
export function unitItemsKey(unit) {
  return unit.items.map((item) => item.item_id).sort().join(',');
}

export function editorHref(unit, mode) {
  const params = new URLSearchParams({ draft: unit.draft_id, unit: unit.unit_ref, items: unitItemsKey(unit), mode });
  return `${window.location.pathname}#/register-editor?${params}`;
}

function unitCountName(unit) {
  return unit.items.length === 1 ? '상품 1개' : `상품 ${unit.items.length}개`;
}

// A unit's name: the authored 상품명, else (2026-10-15) the collected product name the Product DB
// holds, filled in when its read answers; until then, and without one, how many products it holds.
export function unitNameNode(unit, tag = 'b') {
  const authored = unit.authored?.inputs?.name?.value?.trim();
  const node = h(tag, { 'data-role': 'unit-name', 'data-name-source': authored ? 'authored' : 'count' }, authored || unitCountName(unit));
  if (!authored) {
    unitProductName(unit).then((name) => {
      if (!name) return;
      node.textContent = name;
      node.dataset.nameSource = 'collected';
    });
  }
  return node;
}

function openUnitChoice(unit) {
  const registered = unit.intent?.read_state?.state === 'REGISTERED';
  const open = (mode) => {
    window.open(editorHref(unit, mode), '_blank');
  };
  const card = (mode, tag, tone, title, copy, enabled) =>
    h(
      'button',
      {
        type: 'button',
        class: 'choice-card',
        'data-choice': mode,
        disabled: !enabled,
        onclick: () => enabled && open(mode),
      },
      h('span', { class: `chip ${tone}` }, tag),
      h('b', {}, title),
      h('span', { class: 'mini' }, copy),
    );
  openModal({
    eyebrow: '등록관리 · 상품 편집기',
    title: unitNameNode(unit, 'span'),
    help: '편집기는 새 탭으로 열립니다. 등록관리 화면은 그대로 두고 여러 상품을 나란히 열어 작업할 수 있습니다.',
    body: h(
      'div',
      { class: 'unit-choice', 'data-role': 'unit-choice', 'data-unit': unit.unit_ref },
      h(
        'div',
        { class: 'detail-group' },
        h('div', { class: 'kv' }, h('span', {}, '대상 플랫폼'), platformTag(unit.marketplace_key)),
        kv('준비 상태', PREPARATION_LABEL[unit.preparation] ?? unit.preparation),
        unit.intent ? h('div', { class: 'kv' }, h('span', {}, '등록 상태'), readStateChip(unit.intent.read_state, unit.intent.read_state_problem)) : null,
      ),
      h(
        'div',
        { class: 'choice-grid' },
        card('register', '상품등록', 'info', '마켓에 등록하기', '편집기에서 상품 정보·이미지·가격·고시·상세를 채우고 등록합니다.', !registered),
        card('edit', '상품수정', 'good', '등록된 상품 수정하기', registered ? '등록된 상품의 내용을 확인합니다. 마켓에 수정 반영은 준비 중입니다.' : '마켓 등록이 확인된 뒤에 수정할 수 있습니다.', registered),
      ),
    ),
  });
}

// One row per unit, as the server read it; 열기 opens the real editor choice.
function queueRow(unit) {
  const read = unit.intent?.read_state;
  return h(
    'tr',
    {
      class: 'register-queue-row',
      'data-queue-unit': unit.unit_ref,
      tabindex: '0',
      onclick: (event) => {
        if (!event.target.closest('button')) openUnitChoice(unit);
      },
      onkeydown: (event) => {
        if (event.key === 'Enter') openUnitChoice(unit);
      },
    },
    h('td', {}, unitNameNode(unit)),
    h('td', {}, platformTag(unit.marketplace_key)),
    unit.category ? h('td', {}, h('span', { class: 'mono' }, unit.category.category_id)) : noDataCell('카테고리'),
    h('td', {}, chip(PREPARATION_LABEL[unit.preparation] ?? unit.preparation)),
    h('td', {}, read ? h('span', { class: `chip ${READ_STATE_TONE[read.state] ?? ''}`.trim() }, read.label) : '-'),
    h(
      'td',
      {},
      h(
        'button',
        {
          type: 'button',
          class: 'btn',
          'data-action': 'open-unit',
          onclick: () => openUnitChoice(unit),
        },
        '상품 열기',
      ),
    ),
  );
}

function queuePanel(units, readinessBlock, empty) {
  return h(
    'section',
    { class: 'panel register-queue', 'data-role': 'register-queue', 'data-section': 'pending' },
    h('div', { class: 'supplier-head-row' }, h('h3', { class: 'panel-title' }, '등록 대기 목록'), h('span', { class: 'mini' }, `총 ${units.length}건`)),
    units.length
      ? table(['상품', '판매 채널', '카테고리', '준비 상태', '등록 결과', ''], units.map(queueRow))
      : empty,
    readinessBlock,
  );
}

function settingsPanel(ctx) {
  return h(
    'aside',
    { class: 'side-detail register-settings', 'data-role': 'register-settings' },
    h('h3', {}, '판매 채널 설정'),
    h('p', { class: 'mini' }, '배송·가격·카테고리 기준과 계정 연결 상태를 설정에서 관리합니다.'),
    h(
      'div',
      { class: 'inner-tabs register-platform-tabs' },
      ...['smartstore', 'coupang', 'st11'].map((key) =>
        h('button', { type: 'button', 'aria-label': `${key} 설정 열기`, onclick: () => ctx.navigate('settings') }, platformTag(key)),
      ),
    ),
    h('button', { type: 'button', class: 'btn', onclick: () => ctx.navigate('settings') }, '설정 열기'),
  );
}

// A bare block placed as its own card in a section; the block itself is unchanged.
function panelOf(block) {
  return block ? h('section', { class: 'panel register-fixes-panel' }, block) : null;
}

function sectionWrap(section, title, ...children) {
  const shown = children.filter(Boolean);
  if (!shown.length) return null;
  return h(
    'div',
    { class: 'register-section', 'data-section': section },
    h('h2', { class: 'register-section-title' }, title),
    ...shown,
  );
}

// 2026-10-15 usability check: the page is drawn as soon as the screen counts and the units
// arrive. Every slower read (readiness, fixes, canary, live) is its own place that says it is
// loading and is replaced by what the server answers — or by its error — when that read does.
function loadingSlot(role, label) {
  return h('div', { class: 'register-loading', 'data-loading': role, role: 'status' }, chip(`${label} 불러오는 중`));
}

function fill(slot, read, render) {
  read
    .then((value) => {
      const node = render(value);
      if (node) slot.replaceWith(node);
      else slot.remove();
    })
    .catch((error) => slot.replaceWith(h('div', { class: 'register-loading', 'data-load-failed': slot.dataset.loading }, errorState(error))));
}

function summaryRead(path) {
  // A summary is its own read: when it is refused the page still shows every unit, and says the
  // summary is unavailable with the server's code.
  return getJson(path).catch((error) => ({
    unavailable: error instanceof ApiError ? error.error?.code ?? 'ERROR' : 'ERROR',
  }));
}

export default {
  key: 'register',
  title: TITLE,
  navLabel: TITLE,
  icon: '▱',
  async render(ctx) {
    const head = pageHead({ title: TITLE, help: HELP });
    freshPolicyNotes();
    freshProductFacts();
    let screen;
    let overview;
    try {
      [screen, overview] = await Promise.all([getJson(SCREEN), getJson(OVERVIEW)]);
    } catch (error) {
      return fragment(head, errorState(error));
    }
    // Asked once the units are on screen, all four at once; each fills its own place.
    const readinessRead = summaryRead(READINESS);
    const canaryRead = getJson(CANARY);
    const liveRead = getJson(LIVE);
    const reload = () => ctx.navigate('register');
    const readinessSlot = loadingSlot('readiness', '등록 준비 현황');
    fill(readinessSlot, readinessRead, (readiness) =>
      readiness?.unavailable
        ? h('div', { class: 'register-readiness', 'data-readiness': 'UNAVAILABLE' }, reason(readiness.unavailable))
        : readinessPanel(readiness),
    );
    const canarySlot = loadingSlot('canary', '실등록 사전 점검');
    fill(canarySlot, canaryRead, canaryPanel);
    const liveSlot = loadingSlot('live', '보호 쓰기 · 실행 권한');
    fill(liveSlot, liveRead, livePanel);
    const statusPanel = registrationStatusPanel(overview.registration_status, reload);
    // Opened from the registration status card: its detail panel is brought into view.
    if (statusPanel && ctx.params.get('status') === 'open') {
      window.setTimeout(() => statusPanel.scrollIntoView({ block: 'start' }), 0);
    }
    const cards = registerCards(screen, overview.registration_status, undefined);
    readinessRead.then((readiness) =>
      cards.replaceWith(registerCards(screen, overview.registration_status, readiness?.unavailable ? null : readiness)),
    );
    if (!overview.units.length) {
      return fragment(
        head,
        cards,
        utilityRow(overview),
        h(
          'div',
          { class: 'section-grid register-layout' },
          queuePanel(
            [],
            readinessSlot,
            emptyState({
              title: '등록 후보가 없습니다',
              copy: '통합DB에서 등록할 상품을 선택하면 필요한 항목을 확인한 뒤 등록할 수 있습니다.',
              action: { label: '통합DB 보기', onSelect: () => ctx.navigate('db') },
            }),
          ),
          settingsPanel(ctx),
        ),
        sectionWrap('products', '등록상품 관리', listingSyncPanel()),
        sectionWrap('failed', '실패 / 재시도', statusPanel),
        sectionWrap('safety', '실등록 안전장치', canarySlot, liveSlot),
      );
    }
    // Opened from the Product DB with the Draft it just created (Gate 1 G1-D): that Draft's
    // units are marked and brought into view. The route names the Draft only; its units are
    // read from the server like every other unit.
    const focusDraft = ctx.params.get('draft');
    // A short queue remains useful inline. Once the queue grows, the old design expanded every
    // technical workspace into one very long page. Keep the normal operator view list-first and
    // show an inline workspace only for small queues or a Draft explicitly opened from the DB.
    const showInlineWorkspaces = Boolean(focusDraft) || overview.units.length <= 3;
    const panelUnits = focusDraft
      ? overview.units.filter((unit) => unit.draft_id === focusDraft)
      : showInlineWorkspaces
        ? overview.units
        : [];
    // The workspaces group their reasons under the readiness's area labels, so they are drawn
    // once that read answers (without it, under the server's codes' generic heading).
    const panelsSlot = panelUnits.length ? loadingSlot('units', '상품 등록 작업') : null;
    if (panelsSlot) {
      readinessRead.then((readiness) => {
        const areaLabels = Object.fromEntries((readiness?.areas ?? []).map((area) => [area.area, area.label]));
        const panels = panelUnits.map((unit) => {
          const panel = unitPanel(unit, reload, areaLabels);
          if (focusDraft && unit.draft_id === focusDraft) panel.setAttribute('aria-current', 'true');
          return panel;
        });
        panelsSlot.replaceWith(...panels);
        const focused = panels.find((panel) => panel.getAttribute('aria-current') === 'true');
        if (focused && ctx.params.get('status') !== 'open') {
          window.setTimeout(() => focused.scrollIntoView({ block: 'start' }), 0);
        }
      });
    }
    const accounts = new Map(
      overview.units.map((unit) => [`${unit.marketplace_key}|${unit.marketplace_account_id}`, unit]),
    );
    const reviews = [...accounts.values()].map((unit) => reviewPanel(unit.marketplace_key, unit.marketplace_account_id));
    const fixesRead = summaryRead(FIXES);
    const fixesSlot = loadingSlot('fixes', '확인이 필요한 항목');
    fill(fixesSlot, fixesRead, (fixes) => panelOf(fixesPanel(fixes, ctx)));
    return fragment(
      head,
      cards,
      utilityRow(overview),
      h('div', { class: 'section-grid register-layout' }, queuePanel(overview.units, readinessSlot, null), settingsPanel(ctx)),
      sectionWrap('products', '등록상품 관리', listingSyncPanel()),
      sectionWrap('failed', '실패 / 재시도', fixesSlot, statusPanel),
      sectionWrap('safety', '실등록 안전장치', canarySlot, liveSlot),
      sectionWrap('reviews', '검토 항목', ...reviews),
      sectionWrap('units', '상품 등록 작업', panelsSlot),
    );
  },
};
