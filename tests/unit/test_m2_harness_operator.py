"""The operator's confirmations in REAL runs (Issue #46 comment 5671016749).

Only the exact phrase confirms and only an explicit `DECLINE` declines. Any other input, such as
the bare four-character suffix that ended `m2-campaign-01`, is a typing slip: the prompt repeats
and nothing else happens. Keystrokes are scripted; nothing here touches a ledger or a network.
"""

import pytest

from scripts.m2harness.campaign import DECLINE, TerminalOperator

UID = "uid-fixture-abcd"
BIND = "BIND abcd"


class Keyboard:
    """Scripted operator keystrokes. Records every prompt shown and every message written."""

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []
        self.messages: list[str] = []

    def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise AssertionError("the operator was asked again after the last scripted answer")
        return self.lines.pop(0)

    def operator(self) -> TerminalOperator:
        return TerminalOperator(read=self.read, write=self.messages.append)


@pytest.mark.parametrize(
    "slips",
    [
        (),
        ("abcd",),  # the bare suffix: what ended m2-campaign-01
        ("abcd", "bind abcd", "BIND  abcd", "BINDabcd", "BIND abce", "yes", "", "decline"),
    ],
)
def test_only_the_exact_phrase_binds_and_every_slip_is_asked_again(slips: tuple[str, ...]) -> None:
    keyboard = Keyboard(*slips, BIND)
    assert keyboard.operator().confirm_binding(UID, None) is True
    assert len(keyboard.prompts) == len(slips) + 1
    assert all(BIND in prompt and DECLINE in prompt for prompt in keyboard.prompts)
    retries = [message for message in keyboard.messages if message.startswith("Not recognized")]
    assert len(retries) == len(slips)
    assert all(BIND in message for message in retries)


@pytest.mark.parametrize("slips", [(), ("abcd",), ("abcd", "decline", "no")])
def test_only_an_explicit_decline_declines(slips: tuple[str, ...]) -> None:
    keyboard = Keyboard(*slips, DECLINE)
    assert keyboard.operator().confirm_binding(UID, None) is False
    assert len(keyboard.prompts) == len(slips) + 1


def test_the_crash_retry_confirmation_follows_the_same_rule() -> None:
    confirm = Keyboard("T4B", "spend t4b", "SPEND  T4B", "SPEND T4B")
    assert confirm.operator().confirm_crash_retry(["NO_MARKER"]) is True
    assert len(confirm.prompts) == 4
    decline = Keyboard("T4B", DECLINE)
    assert decline.operator().confirm_crash_retry(["NO_MARKER"]) is False
    assert len(decline.prompts) == 2


def test_ctrl_c_still_interrupts_a_confirmation() -> None:
    def interrupted(prompt: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        TerminalOperator(read=interrupted, write=lambda message: None).confirm_binding(UID, None)
