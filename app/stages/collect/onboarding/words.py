"""「수집 규칙」: the words editor of an onboarded site (ADR-0036 §6, slice O4).

The operator confirms or edits the words of each template slot. A save appends a words revision
through the onboarding store (OB-03): only words pass — plain words per slot under the site
configuration's word rules, never code, a pattern, a credential, a host or a fact value — and a
change to an ``ACTIVE`` site returns it to ``RECON`` in the same unit (OB-04).

**The screen** shows the template's own words beside the site's words for every slot, and a
**preview**: the stored captures of the site's samples re-analysed in memory under the current
words and under the proposed ones, so the operator sees which unread rows the new words read and
which field statuses they change before anything is saved. A preview reads nothing from the
supplier, records nothing and binds nothing; it runs the very binding and analysis a trial runs.
"""

from collections.abc import Mapping
from typing import Any

from app.platform.core.errors import InputValidationError, PolicyBlockedError
from app.stages.collect.onboarding.analysis import CAPTURE_DIVERGES, TrialReport, analyse
from app.stages.collect.onboarding.capture import CaptureIntegrityError, TrialCaptureStore
from app.stages.collect.onboarding.catalog import SupplierCatalog
from app.stages.collect.onboarding.contracts import (
    FieldChangeView,
    WordsPreviewSampleView,
    WordsPreviewView,
    WordsSlotView,
    WordsView,
)
from app.stages.collect.onboarding.site import OnboardingRecord
from app.stages.collect.onboarding.store import (
    ONBOARDING_WORDS_INVALID,
    OnboardingStore,
    StoredTrial,
)
from app.stages.collect.onboarding.trials import ONBOARDING_ANALYSIS_UNAVAILABLE, newest_by_sample
from integrations.suppliers.platforms import PlatformAnalysis, PlatformTemplate
from integrations.suppliers.site_config import WORD_LIMIT, WORDS_PER_SLOT
from integrations.suppliers.sites import BoundSite


class CollectionWordsService:
    def __init__(
        self,
        *,
        store: OnboardingStore,
        catalog: SupplierCatalog,
        captures: TrialCaptureStore,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._captures = captures

    def view(self, key: str) -> WordsView:
        record = self._store.current(key)
        template = self._store.template(record.template)
        analysis = template.analysis
        site = dict(record.words.words) if record.words is not None else {}
        return WordsView(
            supplier_key=key,
            state=record.state.value,
            template=record.template,
            words_seq=record.words_seq,
            slots=[
                WordsSlotView(
                    slot=slot,
                    kind=_kind(analysis, slot),
                    defaults=list(defaults),
                    site=list(site.get(slot, ())),
                )
                for slot, defaults in template.label_slots.items()
            ],
            words_per_slot=WORDS_PER_SLOT,
            word_limit=WORD_LIMIT,
        )

    def save(
        self, key: str, words: Mapping[str, Any], *, expected_seq: int, actor: str
    ) -> WordsView:
        self._store.save_words(key, words, expected_seq=expected_seq, actor=actor)
        return self.view(key)

    def preview(self, key: str, words: Mapping[str, Any]) -> WordsPreviewView:
        """Each sample's stored capture under the current words and under ``words``; no read, no
        record. The words are refused exactly as a save would refuse them."""
        proposed_words = self._store.check_words(key, words)
        record = self._store.current(key)
        template = self._store.template(record.template)
        analysis = template.analysis
        if analysis is None:
            raise PolicyBlockedError(
                ONBOARDING_ANALYSIS_UNAVAILABLE, "the template has no site analysis rules"
            )
        current_words = dict(record.words.words) if record.words is not None else {}
        now = self._bound(record, current_words)
        proposed = self._bound(record, proposed_words)
        samples: list[WordsPreviewSampleView] = []
        missing = 0
        for trial in newest_by_sample(self._store.trials(key)).values():
            try:
                body = self._captures.read(trial.capture_sha256)
            except CaptureIntegrityError:
                missing += 1
                continue
            before = _report(record, template, analysis, now, current_words, trial, body)
            after = _report(record, template, analysis, proposed, proposed_words, trial, body)
            samples.append(_compare(trial, before, after))
        return WordsPreviewView(words_seq=record.words_seq, samples=samples, missing=missing)

    def _bound(self, record: OnboardingRecord, words: Mapping[str, tuple[str, ...]]) -> BoundSite:
        bound, problems = self._catalog.bind_words(record, words)
        if bound is None:
            raise InputValidationError(
                ONBOARDING_WORDS_INVALID,
                "the words break a template rule",
                details={"problems": list(problems)},
            )
        return bound


def _kind(analysis: PlatformAnalysis | None, slot: str) -> str:
    if analysis is not None and slot in analysis.row_slots:
        return "row"
    if analysis is not None and slot in analysis.channel_slots:
        return "phrase"
    return "mark"


def _slot_words(
    template: PlatformTemplate, site: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """Each slot's words as a site bound on them reads them: the template's, then the site's."""
    return {
        slot: defaults + tuple(w for w in site.get(slot, ()) if w not in defaults)
        for slot, defaults in template.label_slots.items()
    }


def _report(
    record: OnboardingRecord,
    template: PlatformTemplate,
    analysis: PlatformAnalysis,
    bound: BoundSite,
    site_words: Mapping[str, tuple[str, ...]],
    trial: StoredTrial,
    body: str,
) -> TrialReport:
    return analyse(
        body=body,
        sample_url=trial.sample_url_key,
        storefront_host=record.storefront_host,
        collection=bound.collection,
        analysis=analysis,
        words=_slot_words(template, site_words),
    )


def _unread(report: TrialReport) -> list[str]:
    return [f"{u.label} (문구)" if u.kind == "phrase" else u.label for u in report.unread]


def _compare(trial: StoredTrial, before: TrialReport, after: TrialReport) -> WordsPreviewSampleView:
    was, will = _unread(before), _unread(after)
    return WordsPreviewSampleView(
        sample_url=trial.sample_url_key,
        trial_id=trial.trial_id,
        diverged=CAPTURE_DIVERGES in trial.structural,
        changes=[
            FieldChangeView(field=field, now=status, proposed=after.statuses.get(field, status))
            for field, status in before.statuses.items()
            if after.statuses.get(field, status) != status
        ],
        newly_read=[label for label in was if label not in will],
        newly_unread=[label for label in will if label not in was],
        unread_now=len(was),
        unread_proposed=len(will),
        structural_now=list(before.structural),
        structural_proposed=list(after.structural),
    )
