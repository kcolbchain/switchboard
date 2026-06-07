"""Smoke tests for the Create Protocol stable facade."""

import pytest

import switchboard
from switchboard.create_protocol import CreateProtocolSurfaceError


RECOVERY_SET = {
    "0x1111111111111111111111111111111111111111",
    "0x2222222222222222222222222222222222222222",
}


def test_create_protocol_primitives_are_exported():
    for name in (
        "provision_wallet",
        "sign",
        "rotate",
        "meter",
        "a2a_handshake",
        "recovery_quorum",
    ):
        assert callable(getattr(switchboard, name))


def test_wallet_provision_sign_rotate_and_recovery_quorum():
    wallet_id = switchboard.provision_wallet("2/3", RECOVERY_SET)

    assert wallet_id.startswith("0x")
    assert switchboard.recovery_quorum(wallet_id) == RECOVERY_SET

    signature = switchboard.sign(wallet_id, {"agentId": 7, "nonce": 1})
    assert signature.startswith("0x")
    assert len(signature) == 66

    rotated = switchboard.rotate(wallet_id, {"threshold": 3, "parties": 5})
    assert rotated == wallet_id
    assert switchboard.recovery_quorum(wallet_id) == RECOVERY_SET


def test_meter_returns_portable_receipt():
    receipt = switchboard.meter("session-123", "0.001-USDC/request")

    assert receipt.session_id == "session-123"
    assert receipt.rate == "0.001-USDC/request"
    assert receipt.receipt_id.startswith("0x")
    assert receipt.to_dict()["session_id"] == "session-123"


def test_a2a_handshake_returns_channel():
    channel = switchboard.a2a_handshake("agent:peer-1")

    assert channel.peer == "agent:peer-1"
    assert channel.status == "open"
    assert channel.channel_id
    assert channel.to_dict()["status"] == "open"


def test_unknown_wallet_rejected():
    with pytest.raises(CreateProtocolSurfaceError):
        switchboard.sign("0x0000000000000000000000000000000000000000", b"payload")
