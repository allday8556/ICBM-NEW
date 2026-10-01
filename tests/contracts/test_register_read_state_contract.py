"""ADR-0014 §28.5 (M5-35, M5-36): exactly one partition function exists, on the server.

Every screen, card, panel and counter reads it; no JavaScript recalculates a state, names one of
the four labels itself or paints anything but 등록실패 red.
"""

import re
from pathlib import Path

from app.capabilities.live_safety import visual
from app.stages.register.read_state import READ_STATE_LABELS, ReadState

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "web" / "js"
CARD = UI / "components" / "registration-status.js"
REGISTER = UI / "pages" / "register.js"
ADR = ROOT / "documents" / "decisions" / "adr" / "0014-smartstore-register-idempotency-readback.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_labels_are_the_adrs_own_and_only_the_server_holds_them() -> None:
    adr = _read(ADR)
    for label in READ_STATE_LABELS.values():
        assert label in adr, label
    for source in UI.rglob("*.js"):
        text = _read(source)
        for label in READ_STATE_LABELS.values():
            # Rendered from the server's `label` / `labels`; no page names one as a value.
            assert not re.search(rf"['\"`]{label}['\"`]", text), (source.name, label)


def test_no_page_derives_a_read_state_from_an_intents_own_fields() -> None:
    register = _read(REGISTER)
    card = _read(CARD)
    # The raw Intent state is shown as text only; its tone was the client-side verdict this removed.
    assert "INTENT_TONE" not in register
    for text in (register, card):
        assert "intent.state ===" not in text and "intent.state ==" not in text
        assert "remote_outcome ===" not in text and "verification_state ===" not in text
    # The card reads the server's partition, and only it.
    assert "'/api/v1/register/status'" in card
    assert "status.labels[state]" in card


def test_only_failure_is_red() -> None:
    card = _read(CARD)
    tones = dict(re.findall(r"^\s+([A-Z_]+): '([a-z]+)',$", card, re.M))
    assert set(tones) == {state.value for state in ReadState}
    assert [state for state, tone in tones.items() if tone == "bad"] == [ReadState.FAILED.value]


def test_the_card_and_panel_are_gate_3_visual_surfaces() -> None:
    """ADR-0014 §28.5: both join ADR-0018 §9's visual acceptance contract."""
    register = next(t for t in visual.REQUIRED_TARGETS if t.name == "register")
    assert '#registrationStatus [data-read-state="RECHECK_REQUIRED"]' in register.populated
    for state in ReadState:
        assert (
            f'[data-role="register-status"] tr[data-intent] [data-read-state="{state.value}"]'
            in register.populated
        )
    assert "[data-read-state]" in visual.STATE_SELECTORS
