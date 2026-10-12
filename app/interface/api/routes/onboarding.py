"""Supplier onboarding API: 「+ 공급처 추가」 (ADR-0036 §4, slice O2) and 「사이트 분석」 (§5, O3).

Thin typed routes over ``OnboardingService``, behind the loopback binding and the client-header
CSRF guard, with the configured operator as the audited actor. No route takes a credential: the
login of a new supplier is saved through CONNECT's own
``PUT /api/v1/connect/suppliers/{key}/credentials`` and tested through its connection test.
"""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.interface.api.deps import ContainerDep
from app.stages.collect.onboarding.contracts import (
    AnalysisView,
    DetectionView,
    OnboardingView,
    ReanalysisView,
    TrialSubmitted,
)

router = APIRouter(tags=["onboarding"])

BASE = "/api/v1/onboarding"


class DetectRequest(BaseModel):
    # A refusal never echoes what the operator typed.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    origin: str = Field(max_length=300)
    # The operator's choice among the supported templates, instead of the detected one.
    template: str | None = Field(default=None, max_length=32)


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(max_length=100)
    origin: str = Field(max_length=300)
    # The platform the operator confirmed.
    template: str = Field(max_length=32)
    detection_id: str = Field(max_length=64)
    # The operator acknowledged the robots.txt and terms reads shown with the detection.
    acknowledged: bool


@router.post(f"{BASE}/detect")
def detect(body: DetectRequest, container: ContainerDep) -> DetectionView:
    """Guard the origin, detect the platform and read the policy documents. Writes nothing."""
    return container.onboarding_service.detect(body.origin, template=body.template)


@router.get(f"{BASE}/suppliers")
def list_onboarded(container: ContainerDep) -> list[OnboardingView]:
    return container.onboarding_service.views()


@router.post(f"{BASE}/suppliers", status_code=201)
def create(body: CreateRequest, container: ContainerDep) -> OnboardingView:
    """A ``DRAFT`` supplier with the confirmed platform and the acknowledged policy reads."""
    return container.onboarding_service.create(
        name=body.name,
        origin=body.origin,
        template=body.template,
        detection_id=body.detection_id,
        acknowledged=body.acknowledged,
        actor=container.config.operator_actor,
    )


@router.get(f"{BASE}/suppliers/{{supplier_key}}")
def onboarded(supplier_key: str, container: ContainerDep) -> OnboardingView:
    return container.onboarding_service.view(supplier_key)


@router.post(f"{BASE}/suppliers/{{supplier_key}}/recon")
def start_recon(supplier_key: str, container: ContainerDep) -> OnboardingView:
    """``DRAFT → RECON`` after the platform, the policy reads, the login and a passed test."""
    return container.onboarding_service.start_recon(
        supplier_key, actor=container.config.operator_actor
    )


@router.post(f"{BASE}/suppliers/{{supplier_key}}/disable")
def disable(supplier_key: str, container: ContainerDep) -> OnboardingView:
    return container.onboarding_service.disable(supplier_key, actor=container.config.operator_actor)


# ---------------------------------------------------------------- 「사이트 분석」 (ADR-0036 §5, O3)


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    sample_url: str = Field(max_length=2000)


class HostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str = Field(max_length=253)
    # ``image`` or ``login``: the role a trial observed the host in.
    role: str = Field(max_length=16)
    trial_id: str = Field(max_length=36)


class HostDisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str = Field(max_length=253)
    role: str = Field(max_length=16)


@router.post(f"{BASE}/suppliers/{{supplier_key}}/trials", status_code=202)
def submit_trial(supplier_key: str, body: TrialRequest, container: ContainerDep) -> TrialSubmitted:
    """Queue one trial read of a sample product page (paced; one read; never a revision)."""
    return container.onboarding_analysis.submit(
        supplier_key, body.sample_url, actor=container.config.operator_actor
    )


@router.get(f"{BASE}/suppliers/{{supplier_key}}/trials")
def trials(supplier_key: str, container: ContainerDep) -> AnalysisView:
    """The newest trial of each sample under the current words, and the ACTIVE gate's counts."""
    return container.onboarding_analysis.view(supplier_key)


@router.post(f"{BASE}/suppliers/{{supplier_key}}/reanalyse")
def reanalyse(supplier_key: str, container: ContainerDep) -> ReanalysisView:
    """Re-run the current words over the stored captures. Reads nothing from the supplier."""
    return container.onboarding_analysis.reanalyse(
        supplier_key, actor=container.config.operator_actor
    )


@router.post(f"{BASE}/suppliers/{{supplier_key}}/hosts")
def confirm_host(supplier_key: str, body: HostRequest, container: ContainerDep) -> OnboardingView:
    """Confirm a host a trial observed (D1, OB-01); an image host joins the collection egress."""
    container.onboarding_analysis.confirm_host(
        supplier_key,
        body.host,
        body.role,
        trial_id=body.trial_id,
        actor=container.config.operator_actor,
    )
    return container.onboarding_service.view(supplier_key)


@router.post(f"{BASE}/suppliers/{{supplier_key}}/hosts/disable")
def disable_host(
    supplier_key: str, body: HostDisableRequest, container: ContainerDep
) -> OnboardingView:
    container.onboarding_analysis.disable_host(
        supplier_key, body.host, body.role, actor=container.config.operator_actor
    )
    return container.onboarding_service.view(supplier_key)
