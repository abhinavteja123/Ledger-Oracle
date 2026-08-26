"""Rule 1 (PRD 5.2): the policy engine makes no network calls.

Monkeypatches socket.socket to raise on construction, then runs decide() across a
spread of states (pass/block/escalate paths). If policy.py ever reaches for the
network, this test fails with a socket error instead of a normal Verdict.
"""
import socket

import pytest

from tests.helpers import capture, claim, lookup_result, order_payments_result, state

from policy import decide


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def _blocked(*a, **kw):
        raise AssertionError("policy.decide() touched the network")

    monkeypatch.setattr(socket, "socket", _blocked)


def test_pass_path_is_offline():
    c = capture()
    v = decide(state(extracted=claim(), evidence=[lookup_result(matches=[c])]))
    assert v.decision == "pass"


def test_block_path_is_offline():
    v = decide(state(extracted=claim(), evidence=[lookup_result()]))
    assert v.decision == "block"


def test_escalate_path_is_offline():
    v = decide(state(extracted=claim(), evidence=[lookup_result(unconnected=True)]))
    assert v.decision == "escalate"


def test_empty_claim_is_offline():
    v = decide(state(extracted=None))
    assert v.decision == "escalate"
