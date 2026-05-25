"""Tests for MPC wallet module."""

import pytest
from switchboard.mpc_wallet import MPCWallet, NotEnoughParties, MPCError


def test_wallet_creation():
    w = MPCWallet(parties=3, threshold=2)
    assert w.address().startswith("0x")
    assert len(w.shard_ids()) == 3


def test_threshold_cannot_exceed_parties():
    with pytest.raises(MPCError):
        MPCWallet(parties=2, threshold=5)


def test_signing_with_enough_shards():
    w = MPCWallet(parties=3, threshold=2)
    tx = {"to": "0x1234", "value": 1000}
    sig = w.sign_and_send(tx)
    assert sig.startswith("0x")
    assert len(sig) > 10


def test_signing_fails_without_enough_shards():
    w = MPCWallet(parties=3, threshold=2)
    session_id = w.initiate_signing("0xabcd")
    w.submit_shard_signature(session_id, "shard-0", b"sig1")
    with pytest.raises(NotEnoughParties):
        w.finalize_signature(session_id)


def test_evm_address():
    w = MPCWallet(parties=3, threshold=2, chain_id=8453)
    addr = w.get_evm_address()
    assert addr.startswith("0x")


def test_unknown_session():
    w = MPCWallet()
    with pytest.raises(MPCError):
        w.submit_shard_signature("bad-session", "shard-0", b"sig")
