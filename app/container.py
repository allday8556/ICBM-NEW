"""Explicit composition root: every service is built here and nowhere else."""

import logging
import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import integrations.suppliers as supplier_packages
from app.capabilities.ai import platform_category, platform_tags
from app.capabilities.ai.composer import PromptComposer
from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.profiles import (
    ProfiledProvider,
    ProfileStore,
    ProviderProfileService,
)
from app.capabilities.ai.prompts import PromptRegistryService, PromptRegistryStore
from app.capabilities.ai.search_signal import NoSearchSignal
from app.capabilities.audit.service import AuditLog
from app.capabilities.detail_guidance.image_store import GuidanceImageStore
from app.capabilities.detail_guidance.service import DetailGuidanceService
from app.capabilities.detail_guidance.store import DetailGuidanceStore
from app.capabilities.image_studio.contracts import NoOcrProvider
from app.capabilities.image_studio.ocr import ocr_capability
from app.capabilities.image_studio.store import ImageStudioStore
from app.capabilities.image_studio.task import RegistryTranslationComposer
from app.capabilities.image_studio.translation import ImageTranslationService
from app.capabilities.jobs.diagnostic import FAILING_JOB
from app.capabilities.jobs.policy import RetryPolicy
from app.capabilities.jobs.registry import JobDefinition, JobRegistry
from app.capabilities.jobs.runner import JobRunner
from app.capabilities.jobs.service import JobService
from app.capabilities.jobs.worker import JobWorker
from app.capabilities.live_safety.assets import (
    AssetUploadService,
    PreparationCandidateGate,
    PreparedUploadAssets,
)
from app.capabilities.live_safety.authority import DispatchUnit, LiveAuthorityService
from app.capabilities.live_safety.drill import DrillPaths, RestoreDrillService
from app.capabilities.live_safety.eligibility import CanaryEligibilityService
from app.capabilities.live_safety.gates import CanaryStageReadiness
from app.capabilities.live_safety.model import WireHostPolicy
from app.capabilities.live_safety.proofs import DurableStageProofs
from app.capabilities.live_safety.residual_risk import ResidualRiskAcceptanceService
from app.capabilities.live_safety.retention import RetentionProofService
from app.capabilities.live_safety.stack import SafetyStack
from app.capabilities.live_safety.status import LiveStatusService
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.capabilities.live_safety.upload_run import AssetUploadRun
from app.capabilities.live_safety.visual import VisualAcceptanceService
from app.capabilities.overseas.fx import (
    FxApiKey,
    FxRefreshScheduler,
    FxService,
    FxSnapshotStore,
    FxSource,
)
from app.capabilities.overseas.settings import OverseasSettingsService, OverseasSettingsStore
from app.capabilities.review.collect_producer import COLLECT_PRODUCER, CollectReviewProducer
from app.capabilities.review.counts import ReviewCounts
from app.capabilities.review.coverage import ReviewCoverageStore
from app.capabilities.review.owner import ReviewItemStore
from app.capabilities.review.preflight_producer import PreflightReviewProducer
from app.capabilities.review.products_producer import ProductsReviewProducer
from app.capabilities.review.reconciler import ReviewReconciler
from app.capabilities.review.register_producer import RegisterReviewProducer
from app.capabilities.review.service import ReviewService
from app.capabilities.review.stock_producer import StockReviewProducer
from app.config import AppConfig
from app.interface.screens.register_fixes import RegisterFixService
from app.interface.screens.service import ScreenService
from app.platform.core.clock import Clock, SystemClock
from app.platform.core.code_identity import running_checkout_sha, running_code_digest
from app.platform.core.egress import EGRESS
from app.platform.core.ownership import DataDirLease, require_ownership
from app.platform.core.secrets import SecretStore, build_secret_store
from app.platform.db.database import Database, sqlite_database_dir
from app.platform.db.migrate import head_revision
from app.platform.system.diagnostics import DiagnosticsService
from app.platform.system.execution_mode import ExecutionModeService
from app.platform.system.readiness import ReadinessService
from app.stages.collect.adaptive.engine.capture import boundary_of
from app.stages.collect.adaptive.engine.hooks import HookManifest
from app.stages.collect.adaptive.phase_c_capture.accounting import PhaseCReadAccounting
from app.stages.collect.adaptive.phase_c_capture.commands import PhaseCCommandStore
from app.stages.collect.adaptive.phase_c_capture.runner import CaptureRunner
from app.stages.collect.adaptive.phase_c_capture.store import CaptureStore
from app.stages.collect.adaptive.shadow.runner import ShadowRunner
from app.stages.collect.adaptive.shadow.store import ADR_RETENTION, ShadowEvidenceStore
from app.stages.collect.adaptive.shadow.switch import ShadowSwitch
from app.stages.collect.adaptive.store.gate import (
    SupplierGate,
    build_supplier_gate,
    registered_suppliers,
)
from app.stages.collect.adaptive.store.store import AdaptiveProfileStore, AdaptiveValidationStore
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.collection import (
    CollectionGateway,
    ProductCollectionService,
    RegisteredCollection,
    SessionProvider,
)
from app.stages.collect.extension.buffer import CaptureBuffer
from app.stages.collect.extension.gate import GateResult, security_gate
from app.stages.collect.extension.nonces import NonceCache
from app.stages.collect.extension.pairing import ExtensionPairing
from app.stages.collect.extension.policy import CapturePolicySource
from app.stages.collect.extension.queue import ExtensionQueues
from app.stages.collect.extension.service import ExtensionCaptureService, ReportSink
from app.stages.collect.imagedecode import HeaderImageDecoder
from app.stages.collect.onboarding.capture import TrialCaptureStore
from app.stages.collect.onboarding.catalog import SupplierCatalog
from app.stages.collect.onboarding.service import OnboardingService
from app.stages.collect.onboarding.store import OnboardingStore
from app.stages.collect.onboarding.trials import SiteAnalysisService, last_trial_read
from app.stages.collect.onboarding.words import CollectionWordsService
from app.stages.collect.readback import SourceTruthReadback
from app.stages.collect.revisions import ProductFactsRevisionStore
from app.stages.collect.runs import CollectionRunStore
from app.stages.collect.service import CollectService
from app.stages.collect.sourceassets import SourceAssetRecorder
from app.stages.collect.synthetic import SyntheticTestProductService
from app.stages.connect.accounts import AccountBinding, MarketplaceAccountStore
from app.stages.connect.coupang.service import CoupangCredentialService
from app.stages.connect.credentials import SupplierCredentialStore
from app.stages.connect.marketplace.attestation import ApiGroup
from app.stages.connect.marketplace.attestation_service import PermissionAttestationService
from app.stages.connect.marketplace.revision import EndpointMappingRevisionProvider
from app.stages.connect.marketplace.service import MarketplaceCapabilityService
from app.stages.connect.marketplace.sources import ApplicationIdentitySource
from app.stages.connect.service import ConnectService
from app.stages.connect.sessions import (
    MARKETPLACE_SESSIONS_DIR_NAME,
    SESSIONS_DIR_NAME,
    SupplierSessionStore,
)
from app.stages.connect.smartstore.keeper import SmartStoreSessionKeeper
from app.stages.connect.smartstore.service import SmartStoreConnectService
from app.stages.operate.adoption import CONVENTIONS as ADOPTION_CONVENTIONS
from app.stages.operate.adoption import AdoptionService, seller_code_of
from app.stages.operate.catalog.links import ListingLinkService
from app.stages.operate.catalog.service import StoreCatalogScheduler, StoreCatalogService
from app.stages.operate.dispatch import DispatchService, dispatch_unit
from app.stages.operate.fulfillment import FulfillmentService
from app.stages.operate.listing import ListingSyncScheduler, ListingSyncService
from app.stages.operate.orders import OrderSyncScheduler, OrderSyncService, ShippingCipher
from app.stages.operate.service import OperateService
from app.stages.operate.stock import StockRecheckScheduler, StockRecheckService
from app.stages.products.atomic_sku_economics_store import AtomicSKUEconomicsStore
from app.stages.products.atomic_sku_item_store import AtomicSKUItemStore
from app.stages.products.atomic_sku_store import AtomicSKUStore
from app.stages.products.auto_images import (
    ImageAutoSelector,
    ImageSlot,
    StaticSupplierImageRoles,
    slot_table,
)
from app.stages.products.common_images import SupplierCommonImageService
from app.stages.products.common_option_mapping_store import CommonOptionFactMappingStore
from app.stages.products.common_option_store import CommonSalesOptionStore
from app.stages.products.enrichment import EnrichmentService
from app.stages.products.image_store import DerivedImageStore
from app.stages.products.images import ProductImageService
from app.stages.products.materialization import Materialization, ProductMaterializer
from app.stages.products.overseas_costs import OverseasCostOverrideStore, OverseasCostsService
from app.stages.products.pricing_service import ProductPricingService
from app.stages.products.readiness import ProductReadinessService
from app.stages.products.sales_policy import SalesPolicyService, SalesPolicyStore
from app.stages.products.service import ProductsService
from app.stages.products.source_gates import SourceGates
from app.stages.products.store import ProductFoundationStore
from app.stages.products.tasks import PRODUCTION_TASKS, platform_tag_task
from app.stages.register.authoring import RegistrationPreparationService
from app.stages.register.authoring_revisions import AuthoringRevisionStore
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.bulk import BulkRegistrationService
from app.stages.register.category_catalog import CategoryCatalogService, CategoryCatalogStore
from app.stages.register.category_metadata import (
    CategoryMetadataService,
    CategoryMetadataStore,
    DurableRegistrationMetadata,
)
from app.stages.register.deletion import RegistrationDeletionService
from app.stages.register.drafting import DraftCommandService
from app.stages.register.execution import (
    CREATE_ENDPOINT_GROUP,
    CREATE_POLICY,
    RegistrationExecutionService,
    create_job_definition,
)
from app.stages.register.model import RegistrationLifecycle
from app.stages.register.preflight import RegistrationPreflightService
from app.stages.register.service import RegisterService
from app.stages.register.store import RegistrationStore
from app.stages.register.target_policy import (
    DurableRegistrationPolicy,
    TargetPolicyService,
    TargetPolicyStore,
    editable_surfaces,
)
from app.stages.register.upload_variants import UploadFitVariants
from integrations.ai.sidecar import WindowsProcessProbe
from integrations.fx.koreaexim import KoreaEximAp01
from integrations.marketplaces.identity import MARKETPLACE_IDENTITIES
from integrations.marketplaces.smartstore import product as smartstore_product
from integrations.marketplaces.smartstore import readback as smartstore_readback
from integrations.marketplaces.smartstore import registry as smartstore_registry
from integrations.marketplaces.smartstore.addressbook import SmartStoreAddressBookSource
from integrations.marketplaces.smartstore.adoption import SmartStoreAdoption
from integrations.marketplaces.smartstore.assets import SmartStoreAssetSender
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from integrations.marketplaces.smartstore.catalog import SmartStoreCatalogSource
from integrations.marketplaces.smartstore.category import SmartStoreCategoryCatalogSource
from integrations.marketplaces.smartstore.deletion import SmartStoreDeleteSender
from integrations.marketplaces.smartstore.delivery_companies import DELIVERY_COMPANIES
from integrations.marketplaces.smartstore.dispatch import SmartStoreDispatchSender
from integrations.marketplaces.smartstore.execution import MARKETPLACE_KEY as SMARTSTORE_KEY
from integrations.marketplaces.smartstore.execution import (
    SmartStoreCreateSender,
    SmartStoreReadback,
    SmartStoreReconcileLookup,
)
from integrations.marketplaces.smartstore.listing_finder import SmartStoreListingFinder
from integrations.marketplaces.smartstore.lookup import smartstore_duplicate_source
from integrations.marketplaces.smartstore.notice_catalog import SmartStoreNoticeCatalog
from integrations.marketplaces.smartstore.notice_schema import (
    SmartStoreNoticeRules,
    notice_labels,
)
from integrations.marketplaces.smartstore.orders import SmartStoreOrderSource
from integrations.marketplaces.smartstore.registry import RegistryMappingRevision
from integrations.marketplaces.smartstore.tags import SmartStoreTagSource
from integrations.suppliers.ali1688 import PROFILE as ALI1688_PROFILE
from integrations.suppliers.ali1688.collect.images import ROLE_TABLE as ALI1688_ROLE_TABLE
from integrations.suppliers.base import PublicReader, SupplierDefinition, SupplierGateway
from integrations.suppliers.collection import ImageRole as SupplierImageRole
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.extraction import supplier_manifest
from integrations.suppliers.kmretail import PROFILE as KM_PROFILE
from integrations.suppliers.kmretail.collect.images import OG_IMAGE_RULE as KM_OG_IMAGE_RULE
from integrations.suppliers.kmretail.collect.images import ROLE_RULES as KM_ROLE_RULES
from integrations.suppliers.registry import (
    COLLECTIONS,
    SITE_PROBLEMS,
    SITES,
    SUPPLIERS,
    repository_active,
)
from integrations.suppliers.sites import PACKAGED_HOSTS, PACKAGED_SUPPLIERS, capture_policy_bytes
from integrations.suppliers.superdelivery import PROFILE as SD_PROFILE
from integrations.suppliers.superdelivery.collect.images import ROLE_RULES as SD_ROLE_RULES
from integrations.suppliers.transport.collection import DeferredCollectionGateway
from integrations.suppliers.transport.gateway import (
    PolicedPublicReader,
    PolicedSupplierGateway,
)

logger = logging.getLogger("icbm.container")

# Where the supplier packages, and with them each reviewed capture policy, live.
SUPPLIER_PACKAGES = Path(supplier_packages.__file__).resolve().parent


def _server_final_scan(html: str) -> GateResult:
    """The server's security gate over an extension capture (ADR-0019 §6.1, the user's decision of
    2026-10-01): ``app.stages.collect.extension.gate``, naming each boundary as the capture owner
    names it. Nothing blocking means the capture goes on as it arrived."""
    return security_gate(html, boundary_of=boundary_of)


@dataclass
class Container:
    config: AppConfig
    clock: Clock
    db: Database
    audit: AuditLog
    job_registry: JobRegistry
    jobs: JobService
    runner: JobRunner
    worker: JobWorker
    secrets: SecretStore
    execution_mode: ExecutionModeService
    diagnostics: DiagnosticsService
    readiness: ReadinessService
    screens: ScreenService
    # B-UX2: the fix-only projection of Registration Management (screens layer).
    register_fixes: RegisterFixService
    connect: ConnectService
    source_assets: SourceAssetStore
    source_asset_recorder: SourceAssetRecorder
    revisions: ProductFactsRevisionStore
    source_truth: SourceTruthReadback
    collection: ProductCollectionService
    synthetic_products: SyntheticTestProductService
    extension_pairing: ExtensionPairing
    extension_capture: ExtensionCaptureService
    extension_queues: ExtensionQueues
    product_store: ProductFoundationStore
    common_sales_options: CommonSalesOptionStore
    common_option_fact_mappings: CommonOptionFactMappingStore
    atomic_skus: AtomicSKUStore
    atomic_sku_items: AtomicSKUItemStore
    atomic_sku_economics: AtomicSKUEconomicsStore
    products: ProductsService
    # ADR-0026 AIF-3: PRODUCT DB's enrichment results and the enrich.tasks job. No task is
    # defined yet and no provider is bound, so every request is refused before any write.
    enrichment: EnrichmentService
    materializer: ProductMaterializer
    pricing: ProductPricingService
    images: ProductImageService
    common_images: SupplierCommonImageService
    auto_images: ImageAutoSelector
    product_readiness: ProductReadinessService
    accounts: MarketplaceAccountStore
    registrations: RegistrationStore
    authoring_revisions: AuthoringRevisionStore
    target_policies: TargetPolicyService
    # ADR-0026 AIF-1: the PromptTemplate and PlatformPolicy stores (Settings' AI Prompt Registry).
    prompt_registry: PromptRegistryService
    # ADR-0026 AIF-2: the prompt composer and the provider port's execution. No provider is bound
    # (NoAIProvider): the ai capability reads NOT_CONFIGURED and every execution is refused.
    ai_composer: PromptComposer
    ai_execution: AIExecution
    # ADR-0027 AIS-1: the operator's CLIProxyAPI profile and its approvals.
    ai_provider: ProviderProfileService
    # ADR-0033 G2: Settings' 상세페이지 공지 — the Detail Guidance owner and its image store.
    detail_guidance: DetailGuidanceService
    # ADR-0041 I1: the Image Studio translation owner and the image.translate job (provider-zero).
    image_studio: ImageStudioStore
    image_translation: ImageTranslationService
    # ADR-0039 §1, §4: each supplier's sales policy, saved from the supplier card.
    sales_policies: SalesPolicyService
    # ADR-0038 §3, §5: Settings' 해외 구매대행 — the settings card, the FX snapshots, the Korea
    # Eximbank key and the periodic refresh.
    overseas_settings: OverseasSettingsService
    # ADR-0038 §4, §8: each overseas source product's own overseas shipping and duty rate.
    overseas_costs: OverseasCostsService
    fx: FxService
    fx_refresh_scheduler: FxRefreshScheduler
    category_metadata: CategoryMetadataService
    category_catalog: CategoryCatalogService
    # Settings delivery policy: the seller's address book, read on request and never stored.
    smartstore_addressbook: SmartStoreAddressBookSource
    # ADR-0028 T2: the two read-only tag reads, for the tag task (T3).
    smartstore_tags: SmartStoreTagSource
    registration_preflight: RegistrationPreflightService
    registration_preparations: RegistrationPreparationService
    registration_builder: RegistrationSnapshotBuilder
    registration_execution: RegistrationExecutionService
    bulk_registration: BulkRegistrationService
    live_authority: LiveAuthorityService
    canary_eligibility: CanaryEligibilityService
    residual_risk: ResidualRiskAcceptanceService
    safety_stack: SafetyStack
    asset_uploads: AssetUploadService
    # The operator's upload run (`icbm live upload-assets`, owner decision 5975217061).
    asset_upload_run: AssetUploadRun
    registration_deletions: RegistrationDeletionService
    # M6-A (ADR-0023 §3): the read-only listing-state sync and its periodic pass.
    listing_sync: ListingSyncService
    listing_sync_scheduler: ListingSyncScheduler
    store_catalog: StoreCatalogService
    store_catalog_scheduler: StoreCatalogScheduler
    # ADR-0042 S2a: store listing ↔ common product links.
    store_links: ListingLinkService
    # M6-B (ADR-0023 §4): the supplier stock recheck of listed source products.
    stock_recheck: StockRecheckService
    stock_recheck_scheduler: StockRecheckScheduler
    # M6-E (ADR-0024): SmartStore listings ICBM did not create, adopted by seller-code convention.
    adoptions: AdoptionService
    # M6-C/D (ADR-0023 §5-§7): the read-only order ingest and its periodic pass.
    order_sync: OrderSyncService
    order_sync_scheduler: OrderSyncScheduler
    # M6.5 (ADR-0025 §3, §4): the supplier order placed by hand and its tracking.
    fulfillment: FulfillmentService
    # M6.5-C (ADR-0025 §5): the DISPATCH stage's attempt owner.
    dispatches: DispatchService
    notice_catalog: SmartStoreNoticeCatalog
    restore_drills: RestoreDrillService
    retention: RetentionProofService
    visual_acceptance: VisualAcceptanceService
    live_status: LiveStatusService
    register: RegisterService
    drafting: DraftCommandService
    marketplace_capability: MarketplaceCapabilityService
    permission_attestation: PermissionAttestationService
    smartstore: SmartStoreConnectService
    coupang_credentials: CoupangCredentialService
    # Owner decision 2026-10-07: automatic token renewal through CONNECT's own pass.
    smartstore_keeper: SmartStoreSessionKeeper
    review_items: ReviewItemStore
    review_reconciler: ReviewReconciler
    adaptive_profiles: AdaptiveProfileStore
    adaptive_validation: AdaptiveValidationStore
    shadow_switch: ShadowSwitch
    shadow_evidence: ShadowEvidenceStore
    capture_store: CaptureStore
    phase_c_commands: PhaseCCommandStore
    phase_c_reads: PhaseCReadAccounting
    ownership: DataDirLease
    # ADR-0036 O1: the supplier onboarding owner and the running supplier registry that merges
    # the repository suppliers with the onboarded sites and rebinds one at run time.
    onboarding: OnboardingStore
    supplier_catalog: SupplierCatalog
    # ADR-0036 O2: 「+ 공급처 추가」 — detection, the policy reads, the DRAFT and RECON.
    onboarding_service: OnboardingService
    # ADR-0036 O3: 「사이트 분석」 — trial reads, local captures, re-analysis and observed hosts.
    onboarding_analysis: SiteAnalysisService
    # ADR-0036 O4: 「수집 규칙」 — the words editor, its revisions and its offline preview.
    onboarding_words: CollectionWordsService
    # Each configured supplier's display name, by key (ADR-0030 §11 S2: the screens' lists).
    supplier_names: Mapping[str, str] = field(default_factory=dict)


def adoption_conventions() -> dict[str, str]:
    """KM통상's convention and each site's owner-declared one (ADR-0024 §2, ADR-0030 §3). A site's
    convention is used only in the ADR-0024 code form (letters, then the source product id) and
    with a prefix of its own, so it can never match another supplier's listings."""
    conventions = dict(ADOPTION_CONVENTIONS)
    for key, site in sorted(SITES.items()):
        convention = site.config.seller_code_convention
        if convention is None:
            continue
        prefix = convention.removesuffix("{source_product_id}")
        taken = {c.removesuffix("{source_product_id}").upper() for c in conventions.values()}
        if not re.fullmatch(r"[A-Za-z]{2,8}", prefix) or prefix.upper() in taken:
            logger.warning("seller-code convention of %s not used: %s", key, convention)
            continue
        conventions[key] = convention
    return conventions


def _image_slot(role: str) -> ImageSlot | None:
    return {
        SupplierImageRole.PRIMARY.value: ImageSlot.REPRESENTATIVE,
        SupplierImageRole.THUMBNAIL.value: ImageSlot.ADDITIONAL,
        SupplierImageRole.DETAIL.value: ImageSlot.DETAIL,
        SupplierImageRole.PRODUCT_AUX.value: ImageSlot.AUXILIARY,
    }.get(role)


def supplier_image_roles() -> dict[str, dict[str, ImageSlot]]:
    """Each supplier's published image role rules, as the image auto-selection reads them
    (Issue #219): rule name → slot. A layout or unrecognised role is no product image."""
    slot_of = _image_slot
    return {
        KM_PROFILE.supplier_key: slot_table(
            ((rule.rule_id, rule.role.value) for rule in (*KM_ROLE_RULES, KM_OG_IMAGE_RULE)),
            slot_of,
        ),
        # ADR-0040 §1: 슈퍼딜리버리's own rules.
        SD_PROFILE.supplier_key: slot_table(
            ((rule.rule_id, rule.role.value) for rule in SD_ROLE_RULES), slot_of
        ),
        # ADR-0040 §1: 1688's own rules.
        ALI1688_PROFILE.supplier_key: slot_table(
            ((rule, role.value) for rule, role in ALI1688_ROLE_TABLE), slot_of
        ),
        # ADR-0030 §6: a site's rule names are its template's.
        **{key: slot_table(site.template.role_table, slot_of) for key, site in SITES.items()},
    }


class _CatalogImageRoles:
    """The repository suppliers' role tables, and each bound onboarded site's template table
    (ADR-0036 §1: an onboarded site's rule names are its template's, as a repository site's are)."""

    def __init__(self, fixed: StaticSupplierImageRoles, catalog: SupplierCatalog) -> None:
        self._fixed = fixed
        self._catalog = catalog

    def slots(self, supplier_key: str) -> Mapping[str, ImageSlot] | None:
        fixed = self._fixed.slots(supplier_key)
        if fixed is not None:
            return fixed
        entry = self._catalog.onboarded(supplier_key)
        if entry is None or not entry.offered or entry.bound is None:
            return None
        return slot_table(entry.bound.template.role_table, _image_slot)


def _repository_hosts(suppliers: Sequence[SupplierDefinition]) -> set[str]:
    hosts = set(PACKAGED_HOSTS) | {site.config.storefront_host for site in SITES.values()}
    for definition in suppliers:
        host = urlsplit(definition.profile.base_url).hostname
        if host is not None:
            hosts.add(host)
    return hosts


def build_container(
    config: AppConfig,
    *,
    ownership: DataDirLease,
    clock: Clock | None = None,
    secret_store: SecretStore | None = None,
    extra_jobs: Sequence[JobDefinition] = (),
    supplier_gateway: SupplierGateway | None = None,
    onboarding_reader: PublicReader | None = None,
    collection_gateway: CollectionGateway | None = None,
    collection_sessions: SessionProvider | None = None,
    collections: Sequence[RegisteredCollection] | None = None,
    suppliers: Sequence[SupplierDefinition] = SUPPLIERS,
    application_identity: ApplicationIdentitySource | None = None,
    mapping_revision: EndpointMappingRevisionProvider | None = None,
    smartstore_caller: SmartStoreEndpointCaller | None = None,
    adaptive_supplier_gate: SupplierGate | None = None,
    adaptive_hook_manifests: Mapping[str, HookManifest] | None = None,
    capture_policy_root: Path | None = None,
    extension_report_sink: ReportSink | None = None,
    fx_source: FxSource | None = None,
) -> Container:
    """Compose the application for one data directory.

    This opens the application database for writing, so the lease must cover that database's own
    directory before anything is opened (ADR-0006). It never acquires the lock itself and never
    creates the directory; acquisition belongs to the process entry point.
    """
    require_ownership(ownership, sqlite_database_dir(config.database_url))
    clock = clock or SystemClock()
    db = Database(config.database_url)
    audit = AuditLog(db, clock)
    registered_collections = (
        tuple(_registered(COLLECTIONS)) if collections is None else tuple(collections)
    )
    # ADR-0036 O1: the onboarding owner, and the running supplier registry. The repository
    # suppliers are bound exactly as before (OB-07); each onboarded site is bound from its records
    # now and rebound whenever the store changes it, so every service below reads the supplier set
    # through the catalog's live views and needs no restart.
    onboarding = OnboardingStore(
        db,
        clock,
        audit,
        # ADR-0040: a packaged supplier without a CONNECT definition (슈퍼딜리버리, 1688) is
        # reserved too.
        reserved_keys={d.profile.supplier_key for d in suppliers} | set(SITES) | PACKAGED_SUPPLIERS,
        reserved_hosts=_repository_hosts(suppliers),
    )
    supplier_catalog = SupplierCatalog(
        definitions=suppliers,
        collections=registered_collections,
        capture_policies={key: capture_policy_bytes(site) for key, site in SITES.items()},
        repository_active=repository_active,
        store=onboarding,
    )

    registry = JobRegistry()
    registry.register(FAILING_JOB)
    for definition in extra_jobs:
        registry.register(definition)

    policy = RetryPolicy(
        max_attempts=config.job_max_attempts,
        base_delay_s=config.job_backoff_base_s,
        factor=config.job_backoff_factor,
        max_delay_s=config.job_backoff_max_s,
    )
    jobs = JobService(db, registry, clock, policy)
    runner = JobRunner(
        db,
        registry,
        clock,
        audit,
        default_policy=policy,
        worker_id=f"worker-{os.getpid()}",
        lease_s=config.job_lease_s,
    )
    worker = JobWorker(runner, poll_interval_s=config.job_poll_interval_s, clock=clock)
    jobs.set_worker_notifier(worker.notify)

    secrets = secret_store or build_secret_store(config.secret_backend)
    supplier_credentials = SupplierCredentialStore(secrets)
    connect = ConnectService(
        db=db,
        clock=clock,
        audit=audit,
        jobs=jobs,
        credentials=supplier_credentials,
        sessions=SupplierSessionStore(config.runtime_dir / SESSIONS_DIR_NAME, secrets),
        gateway=supplier_gateway or PolicedSupplierGateway(browser_channel=config.browser_channel),
        suppliers=supplier_catalog.definitions,
        marketplaces=MARKETPLACE_IDENTITIES,
    )
    registry.register(connect.job_definition())
    # ADR-0036 O2: the add flow reads the entered storefront only through the common
    # transport's bootstrap reader; the login goes through CONNECT's credential route.
    onboarding_service = OnboardingService(
        store=onboarding,
        catalog=supplier_catalog,
        connect=connect,
        reader=onboarding_reader or PolicedPublicReader(),
        clock=clock,
    )
    # Marketplace capability truth: typed evidence in, no provider access (M2 PR-B).
    marketplace_capability = MarketplaceCapabilityService(db=db, clock=clock, audit=audit)
    # SmartStore CONNECT (M2 PR-A): every provider call goes through the registry-gated caller.
    smartstore = SmartStoreConnectService(
        db=db,
        clock=clock,
        audit=audit,
        secrets=secrets,
        sessions=SupplierSessionStore(
            config.runtime_dir / MARKETPLACE_SESSIONS_DIR_NAME, secrets, namespace="marketplace"
        ),
        capability=marketplace_capability,
        caller=smartstore_caller or SmartStoreEndpointCaller(),
        # AUTH §15: from validated configuration only; unset, CONNECT refuses.
        renewal_margin=(
            timedelta(seconds=config.smartstore_renewal_margin_s)
            if config.smartstore_renewal_margin_s is not None
            else None
        ),
    )
    coupang_credentials = CoupangCredentialService(secrets, audit)
    # SMARTSTORE-A0-PERMISSION (M2 PR-C). The application identity comes from the committed
    # SmartStore credential bundle and the endpoint-mapping revision from the endpoint registry
    # (§5.1, §5.2); test fixtures may stand in for either.
    permission_attestation = PermissionAttestationService(
        db=db,
        clock=clock,
        audit=audit,
        secrets=secrets,
        capability=marketplace_capability,
        identity=application_identity or smartstore,
        revision=mapping_revision or RegistryMappingRevision(),
        max_age_days=config.smartstore_a0_max_age_days,
    )
    marketplace_capability.set_permission_evidence(permission_attestation)
    # ROADMAP §14 item 5: a LIVE window opens, and stays open, only inside an approved bounded
    # LIVE mutation scope — a live grant, the grant owner's durable record (ADR-0018 §2, §3).
    live_store = LiveAuthorityStore(db, clock, audit)
    execution_mode = ExecutionModeService(
        config.execution_mode, audit, clock, scope=live_store.live_scope_until
    )
    diagnostics = DiagnosticsService(
        enabled=config.diagnostics_enabled, db=db, jobs=jobs, audit=audit
    )
    # ADR-0026 AIF-1, AIF-2: the prompt stores, the composer over them and the provider port. ICBM
    # runs with no AI provider (ADR-0012 §1): the ai capability never fails core readiness.
    prompt_store = PromptRegistryStore(db, clock, audit)
    prompt_registry = PromptRegistryService(prompt_store)
    ai_composer = PromptComposer(prompt_store)
    # ADR-0027: the profiled CLIProxyAPI sidecar. With no approved profile it names no
    # identity, so the ai capability reads NOT_CONFIGURED and nothing is ever sent.
    profile_store = ProfileStore(db, clock, audit)
    profiled_provider = ProfiledProvider(profile_store, WindowsProcessProbe(), clock, secrets)
    ai_execution = AIExecution(profiled_provider)
    ai_provider = ProviderProfileService(profile_store, profiled_provider, clock)
    # ADR-0033 G2: the Detail Guidance owner, its revisions and its content-addressed images.
    detail_guidance_store = DetailGuidanceStore(
        db, clock, audit, GuidanceImageStore(config.guidance_images_dir)
    )
    detail_guidance = DetailGuidanceService(detail_guidance_store)
    # ADR-0028 §3: the SearchSignal port, provider-zero until a source has its own owner decision.
    search_signal = NoSearchSignal()
    # ADR-0041 D2, I2: the local OCR port. No engine is installed until I7 (NoOcrProvider), so
    # the image_ocr capability reads NOT_CONFIGURED; it never fails core readiness.
    ocr_provider = NoOcrProvider()
    readiness = ReadinessService(
        db=db,
        worker=worker,
        secrets=secrets,
        egress=EGRESS,
        execution_mode=execution_mode,
        clock=clock,
        head_revision=head_revision(),
        ownership=ownership,
        capabilities=lambda: [
            *connect.capabilities(),
            ai_execution.capability(),
            search_signal.capability(),
            ocr_capability(ocr_provider),
        ],
    )

    # COLLECT source truth: the content-addressed asset path and the immutable revisions over
    # it. The decoder reads MIME and the original size from the stored bytes themselves.
    source_assets = SourceAssetStore(config.source_assets_dir, db, HeaderImageDecoder(), clock)
    revisions = ProductFactsRevisionStore(db, clock)
    source_asset_recorder = SourceAssetRecorder(source_assets)
    # PRODUCT DB (M4 PR-C): the canonical Product follows durably RECORDED source truth. It is
    # materialized after a run is RECORDED, or by an explicit call; never by a startup sweep.
    product_store = ProductFoundationStore(db, clock)
    common_sales_options = CommonSalesOptionStore(db, clock)
    common_option_fact_mappings = CommonOptionFactMappingStore(db, clock)
    atomic_skus = AtomicSKUStore(db, clock)
    atomic_sku_items = AtomicSKUItemStore(db, clock)
    atomic_sku_economics = AtomicSKUEconomicsStore(db, clock)
    materializer = ProductMaterializer(db=db, store=product_store, revisions=revisions, audit=audit)
    # Adaptive Collector (ADR-0017; P2 persistence, P3 shadow foundation). A supplier is admitted
    # only with a registered CONNECT definition and a registered COLLECT access envelope. No
    # supplier has a switch entry, so every run freezes DISABLED and the shadow never runs until
    # a separately authorized Phase C opening enables one.
    process_run_id = str(uuid.uuid4())
    adaptive_gate = adaptive_supplier_gate or build_supplier_gate(
        registered_suppliers(suppliers, (r.collection for r in registered_collections))
    )
    adaptive_profiles = AdaptiveProfileStore(db, clock, adaptive_gate)
    adaptive_validation = AdaptiveValidationStore(db, clock, adaptive_gate, adaptive_profiles)
    hook_manifests = dict(adaptive_hook_manifests or {})
    shadow_switch = ShadowSwitch(
        db, clock, adaptive_gate, adaptive_profiles, adaptive_validation, hook_manifests
    )
    shadow_evidence = ShadowEvidenceStore(
        db,
        clock,
        supplier_gate=adaptive_gate,
        profiles=adaptive_profiles,
        retention=ADR_RETENTION,
        process_run_id=process_run_id,
    )
    # Phase C C0: the in-memory sample capture. Off for every run unless the Phase C harness left a
    # capture request for its target, consumed at the run's first reservation.
    # C1 PREP-0: the durable accounting of every send of a Phase-C-accounted collection.
    phase_c_reads = PhaseCReadAccounting(db, clock)
    capture_store = CaptureStore(
        db, clock, supplier_gate=adaptive_gate, validation=adaptive_validation
    )
    runs = CollectionRunStore(
        db,
        clock,
        shadow_freezer=shadow_switch.freeze,
        capture_freezer=capture_store.freeze,
        # ADR-0036 §5: an onboarding trial's read paces COLLECT's next read of that product.
        other_reads=(last_trial_read,),
    )

    def _auto_select(materialized: Materialization) -> None:
        """Issue #219: the image auto-selection of every Item the run materialized. It never
        raises into the run; a refusal is logged, and the Item stays unselected until retried."""
        item_ids = dict.fromkeys(
            i for i in (materialized.item_id, *materialized.quantity_item_ids) if i is not None
        )
        for item_id in item_ids:
            try:
                auto_images.auto_select(item_id)
            except Exception:
                logger.exception("image auto-selection failed for %s", item_id)

    def after_recorded(collection_run_id: str) -> None:
        """What follows a durably RECORDED run: the Product, then the review fast path. The review
        step never raises into the run; a failure there is a recorded known failure (§4). The
        fast path also asks for a full pass, which covers what the materialization moved in M4
        (G2-C); until it completes, M4's coverage is not current."""
        try:
            materialized = materializer.materialize_run(collection_run_id)
            _auto_select(materialized)
        finally:
            run = runs.get(collection_run_id)
            if run is not None and run.source_product_id is not None:
                review_reconciler.index_scope(
                    COLLECT_PRODUCER,
                    {"supplier_key": run.supplier_key, "source_product_id": run.source_product_id},
                )

    # Owner decision 2026-10-03: an operator's labelled synthetic test product, a copy of one
    # collected revision's facts under the reserved icbm-synthetic namespace; it materializes like
    # any RECORDED run.
    synthetic_products = SyntheticTestProductService(
        db=db,
        runs=runs,
        revisions=revisions,
        audit=audit,
        clock=clock,
        after_recorded=after_recorded,
    )
    # One operator-submitted product at a time, as a durable collect.* job. The transport is
    # deferred: composing the application opens no connection, and under CI it cannot.
    collect_gateway = collection_gateway or DeferredCollectionGateway()
    collection = ProductCollectionService(
        db=db,
        clock=clock,
        jobs=jobs,
        runs=runs,
        revisions=revisions,
        recorder=source_asset_recorder,
        sessions=collection_sessions or connect,
        gateway=collect_gateway,
        collections=supplier_catalog.collections,
        collectable=supplier_catalog.collectable,
        after_recorded=after_recorded,
        shadow=ShadowRunner(adaptive_profiles, shadow_evidence, hook_manifests),
        capture=CaptureRunner(capture_store),
        accounting=phase_c_reads,
    )
    registry.register(collection.job_definition())
    # ADR-0036 O3: a trial read of a RECON onboarded site goes through the same policed gateway
    # and CONNECT session as a collection, paced on the template's defaults and on COLLECT's own
    # last read, and is recorded only as an onboarding trial: never a ProductFactsRevision.
    trial_captures = TrialCaptureStore(config.onboarding_captures_dir)
    onboarding_analysis = SiteAnalysisService(
        store=onboarding,
        catalog=supplier_catalog,
        jobs=jobs,
        gateway=collect_gateway,
        sessions=collection_sessions or connect,
        captures=trial_captures,
        clock=clock,
        collect_remaining=lambda key, interval: runs.seconds_until_readable(
            key, interval_s=interval, collection_runs_only=True
        ),
        # ADR-0036 §5 step 3: the member's login ID is redacted from a capture, in memory only.
        login_id=supplier_credentials.username,
    )
    registry.register(onboarding_analysis.job_definition())
    # ADR-0036 O4: the words of an onboarded site, saved as revisions through the store and
    # previewed offline on the stored captures; it reads nothing from a supplier.
    onboarding_words = CollectionWordsService(
        store=onboarding, catalog=supplier_catalog, captures=trial_captures
    )

    # ADR-0019 E1/E2: the extension capture transport. The pairing lives in the keyring only, the
    # replay cache and the capture buffer in this process only, and the capture policy is read
    # from the repository on every use. An accepted capture opens a canonical run, and the
    # collection owner records its document through the one pipeline a direct run uses: the
    # server reads no product page for it, and fetches its images through the policed gateway.
    extension_pairing = ExtensionPairing(secrets, clock, NonceCache(clock))
    # ADR-0019 §8.1 (E3): the list queue. Every queue read is issued here, durably, before it
    # happens; a ticketed capture claims its item in the ingest's own write unit.
    extension_queues = ExtensionQueues(
        db=db,
        clock=clock,
        runs=runs,
        collections=supplier_catalog.collections,
        collectable=supplier_catalog.collectable,
    )
    extension_capture = ExtensionCaptureService(
        db=db,
        clock=clock,
        jobs=jobs,
        runs=runs,
        policies=CapturePolicySource(
            capture_policy_root or SUPPLIER_PACKAGES,
            sites=supplier_catalog.capture_policies,
        ),
        buffer=CaptureBuffer(),
        final_scan=_server_final_scan,
        # The buffer is a handoff inside one process (ADR-0002 Option A; ruling 5906712259 N-1).
        worker_in_process=worker.IN_PROCESS,
        recorder=collection,
        collections=supplier_catalog.collections,
        report_sink=extension_report_sink,
        queue_tickets=extension_queues,
        collectable=supplier_catalog.collectable,
    )
    registry.register(extension_capture.job_definition())

    # ADR-0042 S2a: the 통합DB search also finds a common product by its linked store listing's
    # name (the ListingLinks owner below, read at search time).
    products = ProductsService(product_store, listing_search=lambda text: store_links.search(text))
    # M4 PR-D: pricing per Item and explicit context, and derived product readiness. Neither
    # makes a registration candidate: that is M5's preflight.
    pricing = ProductPricingService(
        store=product_store,
        atomic_economics=atomic_sku_economics,
        revisions=revisions,
        audit=audit,
        clock=clock,
    )
    # M4 PR-E: derived image lineage, operator image selection and exact-binary QA. Derived
    # bytes live in their own namespace, never among the source assets.
    derived_images = DerivedImageStore(config.derived_images_dir, db, HeaderImageDecoder())
    images = ProductImageService(
        store=product_store,
        artifacts=derived_images,
        audit=audit,
        clock=clock,
    )
    # ADR-0037 §2: the SmartStore upload-fit variant of each oversize selected image, made in the
    # M4 lineage when a SmartStore unit is authored; the source asset is never touched.
    upload_fit = UploadFitVariants(
        images=images,
        sources=source_assets,
        derived=derived_images,
        clock=clock,
        audit=audit,
    )
    # Issue #219: the operator's BLOCK / KEEP decisions on supplier common images and their
    # detection over each supplier's collection history.
    common_images = SupplierCommonImageService(
        db, audit, clock, configured_suppliers=collection.supplier_keys
    )
    # ADR-0039: whether ICBM applies each supplier's own sales policy (channel restrictions, minimum
    # resale price). The pricing owner and the source gates read it in their own units of work; a
    # supplier is known when CONNECT or COLLECT is configured for it (the supplier cards' keys).
    supplier_keys = frozenset(definition.profile.supplier_key for definition in suppliers)
    sales_policies = SalesPolicyService(
        SalesPolicyStore(db, clock, audit),
        lambda: supplier_keys | set(collection.supplier_keys()),
    )
    # ADR-0038 §3, §5: the 해외 구매대행 settings and the FX snapshot owner. The Korea Eximbank key
    # lives only in the OS secret store; the adapter is the only caller of its one host.
    overseas_settings = OverseasSettingsService(OverseasSettingsStore(db, clock, audit))
    # ADR-0038 §4, §8 phase 1: a source product's own landed-cost inputs, over the settings'
    # defaults. The pricing owner reads them in its own unit of work.
    overseas_costs = OverseasCostsService(OverseasCostOverrideStore(db, clock, audit))
    fx = FxService(
        FxSnapshotStore(db, clock, audit),
        FxApiKey(secrets, audit),
        fx_source or KoreaEximAp01(),
        clock,
        interval_s=config.fx_refresh_interval_s,
    )
    # Issue #219: the image auto-selection rule over each supplier's own published role rules, and
    # its automatic QA. An operator's selection always supersedes it.
    auto_images = ImageAutoSelector(
        store=product_store,
        images=images,
        roles=_CatalogImageRoles(
            StaticSupplierImageRoles(supplier_image_roles()), supplier_catalog
        ),
        audit=audit,
        clock=clock,
    )
    product_readiness = ProductReadinessService(
        store=product_store, revisions=revisions, pricing=pricing, images=images
    )
    # M5 PR-B (ADR-0014): registration persistence only. Nothing sends, reads back or compares a
    # listing yet, and no marketplace endpoint behind it is adopted.
    # The canonical marketplace-account identity that scopes registration state (M5 PR-B,
    # ACCOUNT_IDENTITY §2): established only from a committed M2 binding, with no provider call.
    accounts = MarketplaceAccountStore(db, clock, audit)
    registrations = RegistrationStore(db, clock, audit)
    # The only bearer source for provider metadata reads as well as REGISTER execution. Reading it
    # never renews or commits a token and authorizes no mutation.
    committed_bearer = smartstore.committed_bearer
    smartstore_tags = SmartStoreTagSource(
        smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
    )

    # M6-E (ADR-0024): SmartStore listings ICBM did not create, adopted by the owner-declared
    # seller-code convention and proven by a read-back. REGISTER refuses a second listing of an
    # adopted Item; OPERATE reads them back, rechecks their source and links their orders.
    def _item_source(item_id: str) -> tuple[str, str] | None:
        with product_store.reading() as unit:
            return unit.source_identity_of_item(item_id)

    def _bound_items(supplier_key: str) -> Mapping[str, tuple[str, ...]]:
        with product_store.reading() as unit:
            return unit.bound_items_of_supplier(supplier_key)

    def _registered_sources() -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for record in registrations.marketplace_registrations("smartstore"):
            if record.lifecycle_state is not RegistrationLifecycle.ACTIVE:
                continue
            if any(d.deleted for d in registrations.deletions(record.registration_id)):
                continue
            snapshot = registrations.snapshot(record.registration_snapshot_id)
            for item in () if snapshot is None else snapshot.items:
                identity = _item_source(item.item_id)
                if identity is not None:
                    found.add(identity)
        return found

    adoptions = AdoptionService(
        db=db,
        clock=clock,
        audit=audit,
        finder_factory=lambda pause: SmartStoreListingFinder(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer, pause=pause
        ),
        bound_items=_bound_items,
        registered_sources=_registered_sources,
        # ADR-0030 §3: a site's seller-code convention is the one its owner declared, if any.
        conventions=adoption_conventions(),
    )
    # Gate 1 G1-A (ADR-0015 §2): the durable, append-only target policy of each canonical account,
    # saved from Settings. It is the production policy source: an account without a current
    # revision still fails closed with REGISTER_TARGET_POLICY_MISSING.
    # ADR-0014 §27.1 (Issue #89 5907626428): the server-owned category-mapping and
    # detail-composition revisions a newly appended target-policy revision is stamped with.
    authoring_revisions = AuthoringRevisionStore(db, clock, audit)
    target_policy_store = TargetPolicyStore(db, clock, audit, authoring_revisions)
    target_policies = TargetPolicyService(target_policy_store, accounts)
    # Official SmartStore leaf-category catalog (owner decision 2026-10-04): one read-only provider
    # capture becomes an immutable local snapshot. Its digest is the taxonomy revision used by the
    # target policy and category metadata.
    category_catalog_store = CategoryCatalogStore(db, clock, audit)
    category_catalog = CategoryCatalogService(
        category_catalog_store,
        SmartStoreCategoryCatalogSource(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
        ),
        endpoint_mapping_revision=smartstore_registry.SMARTSTORE_ENDPOINT_MAPPING_REVISION,
    )
    # Gate 1 G1-B (ADR-0015 §3): the durable operator-reviewed category metadata of each
    # marketplace × taxonomy × category. New revisions are admitted only for a current provider
    # leaf category; a category with no current metadata still fails closed.
    category_metadata_store = CategoryMetadataStore(db, clock, audit)
    category_metadata = CategoryMetadataService(
        category_metadata_store,
        frozenset(m.key for m in MARKETPLACE_IDENTITIES),
        category_catalog,
    )
    # The source-side gates of Items (ADR-0031 §4, ADR-0030 §7) under each supplier's sales policy
    # (ADR-0039 §2): the one seam every marketplace's REGISTER reads.
    # ADR-0036 OB-10: an onboarded site that is not ACTIVE is gated as well.
    source_gates = SourceGates(product_store, supplier_catalog.site_active)
    # M5 PR-C (ADR-0014 §3): the derived preflight and the Snapshot builder. No provider is behind
    # either; both sources it reads are the durable owners above.
    registration_preflight = RegistrationPreflightService(
        registrations=registrations,
        readiness=product_readiness,
        pricing=pricing,
        images=images,
        capability=marketplace_capability,
        # Notice coverage S3: a SmartStore category's notice fields are the provider notice
        # schema's own for the reviewed notice type, so metadata, preflight and wire read one
        # contract.
        metadata=DurableRegistrationMetadata(category_metadata_store, SmartStoreNoticeRules()),
        policies=DurableRegistrationPolicy(target_policy_store),
        # B-DETAIL: the detail-composition profile each target names.
        detail_profiles=authoring_revisions,
        # M6-E (ADR-0024 §5): no second listing of an adopted Item.
        adopted_items=adoptions.adopted_items,
        # ADR-0031 §4, ADR-0030 §7: a forbidden or unread sales channel, or a RECON supplier;
        # ADR-0036 OB-10: an onboarded site that is not ACTIVE as well;
        # ADR-0039 §2: a warning instead where the supplier's channel policy is not applied.
        source_gates=source_gates.read,
        # ADR-0033 G4: the store-wide Detail Guidance notices a product's choice resolves to.
        guidance=detail_guidance_store,
    )
    # Gate 1 G1-D (ADR-0015 §5): a Draft from the operator's Product DB selection. It composes the
    # owners above — the revalidated selection, the bound account, the current target policy, M4
    # pricing and the registration store — and replaces none of them.
    drafting = DraftCommandService(
        products=products,
        accounts=accounts,
        # The same durable policy source the preflight reads: the current revision, every call.
        policies=DurableRegistrationPolicy(target_policy_store),
        pricing=pricing,
        registrations=registrations,
        marketplaces=[m.key for m in MARKETPLACE_IDENTITIES],
    )
    registration_builder = RegistrationSnapshotBuilder(
        preflight=registration_preflight, registrations=registrations
    )
    # M5 PR-F (ADR-0014 §27, decision 5751540323): the durable operator-authored preparation. It
    # owns inputs only; the preflight still derives every verdict, and the builder still freezes.
    # The durable ASSET upload-attempt owner (ADR-0018 §3.4) is read by the application freeze:
    # the provider assets prepared for the exact candidate being frozen (5919917893 §3).
    enrichment = EnrichmentService(
        db=db,
        clock=clock,
        audit=audit,
        jobs=jobs,
        products=product_store,
        accounts=accounts,
        composer=ai_composer,
        execution=ai_execution,
        # ADR-0027 AIS-2: the product-name stage's task, the first runnable one.
        # ADR-0028 T3: the SmartStore tag task, over the two adopted tag reads and the port.
        tasks=(
            *PRODUCTION_TASKS,
            platform_tag_task(
                task_key=platform_tags.TAG_TASK_KEY,
                prompt_key=platform_tags.BUNDLE_PROMPT_KEY,
                marketplace="smartstore",
                result_key=platform_tags.TAG_RESULT_KEY,
                fact_fields=platform_tags.TAG_FACT_FIELDS,
                schema_version=platform_tags.TAG_SCHEMA_VERSION,
                context=platform_tags.SmartStoreTagContext(
                    smartstore_tags, search_signal, clock.now
                ),
            ),
            # ADR-0029 C2: the category task, candidates from the durable official catalog only.
            platform_tag_task(
                task_key=platform_category.CATEGORY_TASK_KEY,
                prompt_key=platform_tags.BUNDLE_PROMPT_KEY,
                marketplace="smartstore",
                result_key=platform_category.CATEGORY_RESULT_KEY,
                fact_fields=platform_category.CATEGORY_FACT_FIELDS,
                schema_version=platform_category.CATEGORY_SCHEMA_VERSION,
                context=platform_category.SmartStoreCategoryContext(
                    category_catalog_store, "smartstore"
                ),
                required=("category_id",),
            ),
        ),
    )
    registry.register(enrichment.job_definition())
    # ADR-0041 I1, I4: the Image Studio translation owner and its job. The registered translation
    # task is composed from the prompt registry and sent only through the profiled provider: with
    # no approved profile (data transfer included) it names no identity, and a call past the daily
    # cap is refused (AIS-04). No OCR engine is bound (NoOcrProvider, until I7), so the capability
    # reads NOT_CONFIGURED, every request is refused before anything is written, and nothing is
    # ever read or sent.
    image_studio = ImageStudioStore(db, clock, audit)
    image_translation = ImageTranslationService(
        clock=clock,
        store=image_studio,
        audit=audit,
        jobs=jobs,
        products=product_store,
        sources=source_assets,
        ocr=ocr_provider,
        composer=RegistryTranslationComposer(ai_composer),
        execution=ai_execution,
    )
    registry.register(image_translation.job_definition())

    def _bound_account(marketplace_key: str) -> str | None:
        """The one canonical account of the marketplace bound to its committed identity."""
        for record in accounts.accounts(marketplace_key):
            if (
                accounts.binding(marketplace_key, record.marketplace_account_id)
                is AccountBinding.BOUND
            ):
                return record.marketplace_account_id
        return None

    # ADR-0042 S1a (§5): the store's whole catalog, read only through the adopted search and
    # origin read with the CONNECT owner's committed bearer; without a proven current
    # committed session nothing is read.
    store_catalog = StoreCatalogService(
        db=db,
        clock=clock,
        source=SmartStoreCatalogSource(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
        ),
        account_of=_bound_account,
        interval_s=config.operate_store_catalog_interval_s,
    )
    store_catalog_scheduler = StoreCatalogScheduler(store_catalog)
    adoption_codes = adoption_conventions()

    def _registered_groups(marketplace_key: str, account: str) -> dict[str, str]:
        """ADR-0042 §7 REGISTERED_BY_ICBM: origin product → the common product of each ACTIVE,
        undeleted ICBM registration of the store."""
        found: dict[str, str] = {}
        for record in registrations.marketplace_registrations(marketplace_key):
            if (
                record.marketplace_account_id != account
                or record.lifecycle_state is not RegistrationLifecycle.ACTIVE
                or any(d.deleted for d in registrations.deletions(record.registration_id))
            ):
                continue
            groups = sorted({item.current_group_id for item in record.items})
            if groups:
                found[record.marketplace_product_id] = groups[0]
        return found

    def _adopted_groups(marketplace_key: str) -> dict[str, str]:
        """ADR-0042 §7 ADOPTED: origin product → its Item's common product, per ACTIVE
        adoption (ADR-0024), read as a link and never rewritten."""
        if marketplace_key != "smartstore":
            return {}
        found: dict[str, str] = {}
        with product_store.reading() as unit:
            for record in adoptions.active():
                detail = unit.item_detail(record.item_id)
                if detail is not None:
                    found[record.marketplace_product_id] = detail.product_group_id
        return found

    def _overridden(marketplace_key: str, account: str, group: str) -> bool:
        """Whether an active DuplicateOverride lets the product list twice in the store."""
        with registrations.reading() as unit:
            return bool(unit.active_overrides(marketplace_key, account, group))

    # ADR-0042 S2a (§7): which common product each listing of the store is. After each COMPLETE
    # sync every unlinked listing is linked — strong keys first (with a notice and undo), then a
    # new 「도매처 미연결」 common product for the rest. Read only towards every marketplace.
    store_links = ListingLinkService(
        db=db,
        clock=clock,
        audit=audit,
        catalog=store_catalog,
        products=product_store,
        registered=_registered_groups,
        adopted=_adopted_groups,
        overridden=_overridden,
        conventions=adoption_codes,
    )
    store_catalog.add_listener(store_links.after_sync)

    def _convention_codes(item_ids: Sequence[str]) -> set[str]:
        """ADR-0024 §2: the owner-declared seller code of each Item's source, where one exists."""
        found: set[str] = set()
        for item_id in item_ids:
            identity = _item_source(item_id)
            code = None if identity is None else seller_code_of(*identity, adoption_codes)
            if code is not None:
                found.add(code)
        return found

    # ADR-0042 S3b (§6): REGISTER's duplicate evidence is the bound store's COMPLETE catalog
    # snapshot, fresh under the target policy's catalog_max_age; a stale or missing one is no
    # evidence and starts the automatic pre-registration sync (a routine read). The first CREATE
    # copy adds the positive-only seller-code spot check through the adopted search.
    duplicate_source = smartstore_duplicate_source(
        catalog=store_catalog,
        refresh=store_catalog_scheduler.refresh_for_registration,
        convention_codes=_convention_codes,
        mutation_started_at=registrations.last_mutation_started,
        caller=smartstore_caller or SmartStoreEndpointCaller(),
        bearer=committed_bearer,
        # ADR-0042 S2a (§6): a store listing linked to the unit's product is a duplicate.
        linked=store_links.linked_groups,
    )
    registration_preparations = RegistrationPreparationService(
        registrations=registrations,
        preflight=registration_preflight,
        builder=registration_builder,
        duplicate_lookup=duplicate_source,
        prepared_assets=PreparedUploadAssets(live_store),
        # ADR-0026 AIF-4: the current enrichment results an apply reads.
        enrichment=enrichment,
        # ADR-0033 G4: the owner that renders and records a product's own notice on save.
        guidance=detail_guidance_store,
        # ADR-0037 §2: the M4 owner that makes each oversize image's upload-fit variant on save.
        upload_fit=upload_fit,
    )
    # M5 PR-E (ADR-0014 §9-§11): the execution owner over the M0 job system. Its CREATE seam is
    # the production SmartStore one, which is unavailable while the endpoint is NOT_ADOPTED, so
    # no code path here can mutate the marketplace; the read-back seam is PR-D's adopted one.
    # Gate 3 area 1 (ADR-0018 §3, §3.4, §4, §10): the pre-LIVE safety owners. The stack reads the
    # execution-mode owner, whose M0 policy refuses every LIVE write, and no eligibility, restore,
    # retention or visual proof exists yet, so every mutation it judges is refused at this main.
    # Gate 3 area 2 (ADR-0018 §7, §8): the restore-drill and evidence-retention proofs are durable
    # owners. Gate 3 area 3 (§9): the reviewed visual acceptance, current only for exactly the
    # commit this process runs at and its running code digest (both taken once at composition), at
    # the current schema head.
    # Eligibility (§5.1) is its own durable owner, wired below; no record exists for any lineage.
    retention = RetentionProofService(
        db=db,
        store=live_store,
        job_types=registry.job_types,
        safe_retention_profile_version=smartstore_registry.SAFE_RETENTION_PROFILE_VERSION,
        schema_head=head_revision,
    )
    code_sha = running_checkout_sha()
    code_identity = running_code_digest(config.ui_dir)
    visual_acceptance = VisualAcceptanceService(
        store=live_store,
        code_sha=lambda: code_sha,
        code_identity=lambda: code_identity,
        schema_head=head_revision,
    )
    # ADR-0018 §5.1 (Issue #89 5910018106): the canary-eligibility owner. A record proves
    # CANARY_NON_REGULATED only for the exact lineage the stack re-derives at each stage.
    canary_eligibility = CanaryEligibilityService(
        store=live_store, registrations=registrations, preparations=registration_preparations
    )
    # ADR-0018 §6.1, G3-30 (migration 0036): the durable proof of the user and architect
    # residual-risk acceptance recorded in GitHub, for one account and the exact risk contract.
    residual_risk = ResidualRiskAcceptanceService(live_store)
    stage_proofs = DurableStageProofs(
        store=live_store,
        retention=retention,
        visual=visual_acceptance,
        eligibility=canary_eligibility,
        residual_risk=residual_risk,
        schema_head=head_revision,
    )
    safety_stack = SafetyStack(
        store=live_store, mode=execution_mode, proofs=stage_proofs, clock=clock
    )

    def _dispatch_unit(product_order_id: str) -> DispatchUnit:
        with db.read() as session:
            return dispatch_unit(session, product_order_id, account_of=_bound_account)

    live_authority = LiveAuthorityService(
        store=live_store,
        registrations=registrations,
        preparations=registration_preparations,
        # ADR-0025 §5: the Fulfillment owner states which product order a DISPATCH grant binds.
        dispatch_units=_dispatch_unit,
    )
    # ROADMAP §14 item 4: the one canonical bearer source of every SmartStore REGISTER seam.
    registration_execution = RegistrationExecutionService(
        registrations=registrations,
        preflight=registration_preflight,
        # ROADMAP §14 item 4: the four SmartStore seams read one canonical bearer source, the
        # CONNECT owner's read-only committed bearer. It answers only for the current committed
        # session of a proven, bound account with more than the renewal margin left, and None
        # otherwise. A bearer permits no mutation: M0_DRY_RUN_ONLY, the brake, the grant and every
        # other send-time layer still refuse CREATE and ASSET, and the wire projection is not
        # sendable while the official evidence leaves a required value uncaptured.
        sender=SmartStoreCreateSender(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        readback=SmartStoreReadback(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        # SEARCH is adopted for positive-only reconcile with the same bearer source.
        lookup=SmartStoreReconcileLookup(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        capability=marketplace_capability,
        compare=smartstore_readback,
        projection=smartstore_product.project,
        clock=clock,
        authority=safety_stack,
        # ADR-0042 §6: the send-time freshness check reads the same duplicate owner.
        duplicate_check=registration_preparations.duplicate_check,
    )
    bulk_registration = BulkRegistrationService(db, jobs, clock)
    registry.register(
        create_job_definition(
            registration_execution,
            retry_policy=CREATE_POLICY,
            after_terminal=bulk_registration.settle_terminal,
            additional_unsettled=bulk_registration.unsettled_jobs,
        )
    )
    # The ASSET upload path (§3.4) with its durable attempt owner. The sender is the adopted
    # SmartStore image upload with the same canonical bearer source as the CREATE seams; the
    # send-time stack still refuses every upload under M0_DRY_RUN_ONLY.
    asset_uploads = AssetUploadService(
        store=live_store,
        stack=safety_stack,
        sender=SmartStoreAssetSender(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        hosts=WireHostPolicy(
            {SMARTSTORE_KEY: smartstore_registry.canonical_host()},
            {SMARTSTORE_KEY: smartstore_registry.HOST_ALIASES},
        ),
        candidates=PreparationCandidateGate(registration_preparations, registrations),
        clock=clock,
    )
    # The only production entry point of the upload owner: an `icbm live upload-assets` run in the
    # process that owns the data directory. It composes the grant, CONNECT, the execution-mode
    # window and the M4 lineage stores, and decides nothing itself.
    asset_upload_run = AssetUploadRun(
        store=live_store,
        uploads=asset_uploads,
        mode=execution_mode,
        connect=smartstore.connect,
        sources=source_assets,
        derived=derived_images,
        # ADR-0033 §8: the recorded Detail Guidance images, the third byte source.
        guidance=detail_guidance_store,
    )
    # ADR-0018 §3.5: the deletion of one ICBM-confirmed registration, through the same send-time
    # stack and the same canonical bearer source. Its exact DELETE grant is the only authority for
    # it, an unknown deletion is never resent, and only a read-back confirms or resolves one.
    # Notice coverage S0 (owner directive 2026-10-03): a read-only capture of the official
    # 상품정보제공고시 schema with the same canonical bearer source. It reads and stores nothing.
    notice_catalog = SmartStoreNoticeCatalog(
        caller=smartstore_caller or SmartStoreEndpointCaller(),
        bearer=committed_bearer,
    )
    registration_deletions = RegistrationDeletionService(
        registrations=registrations,
        sender=SmartStoreDeleteSender(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        readback=SmartStoreReadback(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        sale_status=lambda retained: smartstore_readback.normalize(retained).sale_status,
        authority=safety_stack,
    )
    # Gate 3 area 3 (§9): the read-only projection of the brake, the grants, their ASSET readiness
    # and the unit-independent proofs, which 등록관리 shows. It writes and authorizes nothing.
    live_status = LiveStatusService(
        store=live_store,
        registrations=registrations,
        assets=asset_uploads,
        retention_ready=retention.ready,
        visual_recorded=visual_acceptance.recorded,
    )
    restore_drills = RestoreDrillService(
        db=db,
        audit=audit,
        paths=DrillPaths(data_dir=config.data_dir, database_path=config.database_path),
        store=live_store,
        stack=safety_stack,
        assets=asset_uploads,
        preparations=registration_preparations,
        registrations=registrations,
        clock=clock,
        schema_head=head_revision,
    )

    # Gate 2 (ADR-0016): the durable ReviewItem owner (G2-A) with its producers: COLLECT / M3
    # (G2-B), M4 base readiness, REGISTER execution and REGISTER preparations (G2-C). Each
    # process run has its own identity:
    # coverage is current only after a complete full pass in this run (§7). The review owner
    # reads these owners; none of them reads it.
    def _source_identity(item_id: str) -> tuple[str, str] | None:
        with product_store.reading() as unit:
            return unit.source_identity_of_item(item_id)

    stock_recheck = StockRecheckService(
        db=db,
        clock=clock,
        registrations=registrations,
        source_identity=_source_identity,
        collector=collection,
        revisions=revisions,
        interval_s=config.operate_stock_recheck_interval_s,
        cap=config.operate_stock_recheck_cap,
        adoptions=adoptions,
    )
    review_items = ReviewItemStore(
        db,
        clock,
        audit,
        producers=[
            CollectReviewProducer(revisions),
            StockReviewProducer(stock_recheck, revisions),
            ProductsReviewProducer(product_readiness),
            RegisterReviewProducer(registrations, clock),
            PreflightReviewProducer(
                registrations=registrations,
                preparations=registration_preparations,
                preflight=registration_preflight,
                readiness=product_readiness,
                audit=audit,
            ),
        ],
    )
    review_reconciler = ReviewReconciler(
        review_items,
        ReviewCoverageStore(db, clock, audit),
        clock,
        process_run_id=process_run_id,
        interval_s=config.review_reconcile_interval_s,
        max_age_s=config.review_coverage_max_age_s,
    )
    # M5 PR-F (ADR-0014 §22, §24): the Registration Management read model and its operator
    # actions. It owns no truth of its own — it reads the owners above and hands each action to
    # the owner of that action — and its canary readiness is derived and read-only.
    # ADR-0018 §10: the canary readiness only summarizes the two mutation stages, so it reads each
    # stage's verdict from the owners the send-time stack uses — never from its own weaker copy.
    canary_stages = CanaryStageReadiness(
        stack=safety_stack,
        assets=asset_uploads,
        live=live_store,
        registrations=registrations,
        preparations=registration_preparations,
        create_sender_available=registration_execution.create_sender_available,
        reconcile_path_adopted=registration_execution.reconcile_path_adopted,
        endpoint_group=CREATE_ENDPOINT_GROUP,
    )
    register_service = RegisterService(
        registrations=registrations,
        execution=registration_execution,
        preflight=registration_preflight,
        authoring=registration_preparations,
        accounts=accounts,
        jobs=jobs,
        bulk=bulk_registration,
        capability=marketplace_capability,
        adoption=SmartStoreAdoption(),
        stages=canary_stages,
        # Read at every evaluation: a bounded LIVE window opens and lapses at runtime (§14 item 5).
        execution_mode=lambda: execution_mode.state().mode.value,
        # ADR-0014 §28.5: the status panel shows the ICBM seller code the provider is sent.
        clock=clock,
        seller_code=smartstore_product.seller_management_code,
        # B-PREVIEW: the frozen-Snapshot preview reads the same wire projection the sender uses.
        preview_projection=smartstore_product.project,
        # The notice schema's own Korean names of a notice type and its fields, for display only.
        notice_labels=notice_labels,
    )
    listing_sync = ListingSyncService(
        db=db,
        clock=clock,
        registrations=registrations,
        reader=SmartStoreReadback(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        normalize=smartstore_readback.normalize,
        interval_s=config.operate_listing_sync_interval_s,
        # The provider code an ICBM listing identity is sent as, compared on read-back.
        seller_code=smartstore_product.seller_management_code,
        adoptions=adoptions,
    )
    order_sync = OrderSyncService(
        db=db,
        clock=clock,
        registrations=registrations,
        source_identity=_source_identity,
        source=SmartStoreOrderSource(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
        ),
        cipher=ShippingCipher(secrets),
        audit=audit,
        interval_s=config.operate_order_sync_interval_s,
        initial_lookback_s=config.operate_order_initial_lookback_s,
        retention_days=config.operate_order_shipping_retention_days,
        adoptions=adoptions,
        # ADR-0023 §5: the operator-attested 주문 판매자 group gates order ingest.
        order_read_attested=lambda: (
            ApiGroup.ORDER_SELLER in permission_attestation.attested_groups("smartstore")
        ),
    )
    fulfillment = FulfillmentService(
        db=db,
        clock=clock,
        audit=audit,
        # ADR-0025 §4: the documented SmartStore delivery-company codes.
        carriers=DELIVERY_COMPANIES,
    )
    # ADR-0025 §5 (M6.5-C): the dispatch of one product order through the send-time safety stack,
    # verified by the adopted order read. A bearer permits nothing: M0_DRY_RUN_ONLY, the brake,
    # the exact DISPATCH grant and every other layer still refuse until the owner approves one.
    dispatches = DispatchService(
        db=db,
        clock=clock,
        audit=audit,
        sender=SmartStoreDispatchSender(
            caller=smartstore_caller or SmartStoreEndpointCaller(),
            bearer=committed_bearer,
        ),
        authority=safety_stack,
        reader=SmartStoreOrderSource(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
        ),
        order_group_attested=lambda: (
            ApiGroup.ORDER_SELLER in permission_attestation.attested_groups("smartstore")
        ),
        account_of=_bound_account,
    )
    screens = ScreenService(
        clock=clock,
        operator_name=config.operator_name,
        marketplaces=MARKETPLACE_IDENTITIES,
        connect=connect,
        collect=CollectService(jobs),
        products=products,
        register=register_service,
        operate=OperateService(
            order_count=order_sync.order_count,
            order_capability=order_sync.capability,
            awaiting_dispatch=fulfillment.awaiting_dispatch_count,
        ),
        review=ReviewService(ReviewCounts(review_items, review_reconciler)),
        execution_mode=execution_mode,
        editable_surfaces=editable_surfaces(),
        # ADR-0040 §1: a supplier read only from captured pages takes no submitted URL.
        collection_suppliers=collection.submittable_supplier_keys,
    )
    return Container(
        listing_sync=listing_sync,
        stock_recheck=stock_recheck,
        stock_recheck_scheduler=StockRecheckScheduler(stock_recheck),
        order_sync=order_sync,
        adoptions=adoptions,
        order_sync_scheduler=OrderSyncScheduler(order_sync),
        fulfillment=fulfillment,
        dispatches=dispatches,
        listing_sync_scheduler=ListingSyncScheduler(listing_sync),
        store_catalog=store_catalog,
        store_catalog_scheduler=store_catalog_scheduler,
        store_links=store_links,
        auto_images=auto_images,
        common_images=common_images,
        config=config,
        clock=clock,
        db=db,
        audit=audit,
        job_registry=registry,
        jobs=jobs,
        runner=runner,
        worker=worker,
        secrets=secrets,
        execution_mode=execution_mode,
        diagnostics=diagnostics,
        readiness=readiness,
        screens=screens,
        register_fixes=RegisterFixService(register_service, review_items, clock),
        connect=connect,
        source_assets=source_assets,
        source_asset_recorder=source_asset_recorder,
        revisions=revisions,
        source_truth=SourceTruthReadback(revisions, source_assets),
        collection=collection,
        synthetic_products=synthetic_products,
        extension_pairing=extension_pairing,
        extension_capture=extension_capture,
        extension_queues=extension_queues,
        product_store=product_store,
        common_sales_options=common_sales_options,
        common_option_fact_mappings=common_option_fact_mappings,
        atomic_skus=atomic_skus,
        atomic_sku_items=atomic_sku_items,
        atomic_sku_economics=atomic_sku_economics,
        products=products,
        enrichment=enrichment,
        materializer=materializer,
        pricing=pricing,
        images=images,
        product_readiness=product_readiness,
        accounts=accounts,
        registrations=registrations,
        authoring_revisions=authoring_revisions,
        target_policies=target_policies,
        prompt_registry=prompt_registry,
        ai_composer=ai_composer,
        ai_execution=ai_execution,
        ai_provider=ai_provider,
        detail_guidance=detail_guidance,
        image_studio=image_studio,
        image_translation=image_translation,
        sales_policies=sales_policies,
        overseas_settings=overseas_settings,
        overseas_costs=overseas_costs,
        fx=fx,
        fx_refresh_scheduler=FxRefreshScheduler(fx),
        category_metadata=category_metadata,
        category_catalog=category_catalog,
        smartstore_addressbook=SmartStoreAddressBookSource(
            smartstore_caller or SmartStoreEndpointCaller(), committed_bearer
        ),
        smartstore_tags=smartstore_tags,
        registration_preflight=registration_preflight,
        registration_preparations=registration_preparations,
        registration_builder=registration_builder,
        registration_execution=registration_execution,
        bulk_registration=bulk_registration,
        live_authority=live_authority,
        canary_eligibility=canary_eligibility,
        residual_risk=residual_risk,
        safety_stack=safety_stack,
        asset_uploads=asset_uploads,
        asset_upload_run=asset_upload_run,
        registration_deletions=registration_deletions,
        notice_catalog=notice_catalog,
        restore_drills=restore_drills,
        retention=retention,
        visual_acceptance=visual_acceptance,
        live_status=live_status,
        register=register_service,
        drafting=drafting,
        marketplace_capability=marketplace_capability,
        permission_attestation=permission_attestation,
        smartstore=smartstore,
        coupang_credentials=coupang_credentials,
        smartstore_keeper=SmartStoreSessionKeeper(smartstore, enabled=config.smartstore_auto_renew),
        review_items=review_items,
        review_reconciler=review_reconciler,
        adaptive_profiles=adaptive_profiles,
        adaptive_validation=adaptive_validation,
        shadow_switch=shadow_switch,
        shadow_evidence=shadow_evidence,
        capture_store=capture_store,
        phase_c_commands=PhaseCCommandStore(db, clock),
        phase_c_reads=phase_c_reads,
        ownership=ownership,
        onboarding=onboarding,
        supplier_catalog=supplier_catalog,
        onboarding_service=onboarding_service,
        onboarding_analysis=onboarding_analysis,
        onboarding_words=onboarding_words,
        supplier_names=supplier_catalog.names,
    )


def _registered(collections: Sequence[SupplierCollection]) -> Iterator[RegisteredCollection]:
    """Bind each supplier's collection to the extraction identity its own manifest names.

    A revision records the rules that produced it, so a supplier whose image-role rules and
    manifest disagree is refused here rather than storing a revision under a stale identity.
    """
    for problem in SITE_PROBLEMS:
        # ADR-0030 §3: an invalid site file leaves that site out and stops nothing else.
        logger.warning("supplier site configuration not offered: %s", problem)
    for collection in collections:
        site = SITES.get(collection.supplier_key)
        if site is not None and site.collection is collection:
            # ADR-0030 §4: a site's identity is (template revision, site revision).
            revision, fingerprint = site.extractor_revision, site.extractor_fingerprint
        else:
            manifest = supplier_manifest(collection.supplier_key)
            revision, fingerprint = manifest.revision, manifest.fingerprint
        yield RegisteredCollection(
            collection=collection,
            extractor_revision=revision,
            extractor_fingerprint=fingerprint,
        )
