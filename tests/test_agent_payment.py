"""
Tests for the AgentPaymentClient.

Uses unittest.mock to avoid a real blockchain dependency.
Run with: pytest tests/test_agent_payment.py -v
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from python.agent_payment import (
    ESCROW_ABI,
    ETH_ADDRESS,
    AgentPaymentClient,
    EscrowInfo,
    EscrowStatus,
    PaymentRequest,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FAKE_PRIVATE_KEY = "0x" + "ab" * 32
FAKE_ESCROW_ADDR = "0x" + "11" * 20
FAKE_SELLER = "0x" + "22" * 20


def _make_client(w3_mock: MagicMock) -> AgentPaymentClient:
    """Instantiate a client with a mocked Web3 provider."""
    w3_mock.eth.chain_id = 1
    w3_mock.eth.get_transaction_count.return_value = 0
    w3_mock.eth.estimate_gas.return_value = 21_000
    w3_mock.eth.gas_price = 1_000_000_000
    w3_mock.eth.contract.return_value = MagicMock()

    with patch("python.agent_payment.Web3") as Web3Cls:
        Web3Cls.to_checksum_address = lambda a: a
        client = AgentPaymentClient(
            web3=w3_mock,
            escrow_address=FAKE_ESCROW_ADDR,
            private_key=FAKE_PRIVATE_KEY,
        )
    return client


# ---------------------------------------------------------------------------
# PaymentRequest unit tests
# ---------------------------------------------------------------------------


class TestPaymentRequest:
    def test_to_dict(self):
        req = PaymentRequest(
            sender="0xAAA",
            receiver="0xBBB",
            amount=100,
            token=ETH_ADDRESS,
            deadline=9999,
            nonce=1,
        )
        d = req.to_dict()
        assert d["version"] == "1.0"
        assert d["type"] == "payment_request"
        assert d["from"] == "0xAAA"
        assert d["to"] == "0xBBB"
        assert d["amount"] == "100"
        assert d["deadline"] == 9999
        assert d["nonce"] == 1

    def test_to_json_roundtrip(self):
        import json

        req = PaymentRequest(
            sender="0xAAA", receiver="0xBBB", amount=42, nonce=7
        )
        parsed = json.loads(req.to_json())
        assert parsed["amount"] == "42"
        assert parsed["nonce"] == 7

    def test_default_token_is_eth(self):
        req = PaymentRequest(sender="0xA", receiver="0xB", amount=1)
        assert req.token == ETH_ADDRESS

    def test_metadata_default_empty(self):
        req = PaymentRequest(sender="0xA", receiver="0xB", amount=1)
        assert req.metadata == {}


# ---------------------------------------------------------------------------
# EscrowInfo unit tests
# ---------------------------------------------------------------------------


class TestEscrowInfo:
    def test_status_enum(self):
        info = EscrowInfo(
            escrow_id=0,
            buyer="0xA",
            seller="0xB",
            token=ETH_ADDRESS,
            amount=100,
            deadline=0,
            nonce=0,
            status=EscrowStatus.RELEASED,
            approval_count=0,
            threshold=0,
        )
        assert info.status == EscrowStatus.RELEASED
        assert info.status == 1


# ---------------------------------------------------------------------------
# AgentPaymentClient tests
# ---------------------------------------------------------------------------


class TestClientRequestCreation:
    def test_create_payment_request_defaults(self):
        w3 = MagicMock()
        client = _make_client(w3)
        req = client.create_payment_request(
            receiver=FAKE_SELLER, amount=1_000
        )
        assert req.sender == client.address
        assert req.receiver == FAKE_SELLER
        assert req.amount == 1_000
        assert req.token == ETH_ADDRESS
        assert req.deadline > int(time.time())
        assert req.nonce == 0

    def test_create_payment_request_custom_params(self):
        w3 = MagicMock()
        client = _make_client(w3)
        req = client.create_payment_request(
            receiver=FAKE_SELLER,
            amount=500,
            token="0xTOKEN",
            deadline=1234567890,
            nonce=42,
            metadata={"job": "indexing"},
        )
        assert req.token == "0xTOKEN"
        assert req.deadline == 1234567890
        assert req.nonce == 42
        assert req.metadata["job"] == "indexing"


class TestClientGetEscrow:
    def test_get_escrow_parses_correctly(self):
        w3 = MagicMock()
        client = _make_client(w3)
        client.contract.functions.getEscrow.return_value.call.return_value = (
            "0xBuyer",
            "0xSeller",
            ETH_ADDRESS,
            1000,
            9999,
            7,
            0,  # Created
            0,
            0,
        )
        info = client.get_escrow(0)
        assert isinstance(info, EscrowInfo)
        assert info.buyer == "0xBuyer"
        assert info.seller == "0xSeller"
        assert info.amount == 1000
        assert info.status == EscrowStatus.CREATED


class TestClientMonitor:
    def test_monitor_returns_when_released(self):
        w3 = MagicMock()
        client = _make_client(w3)

        # First call: Created, second call: Released
        client.contract.functions.getEscrow.return_value.call.side_effect = [
            ("0xB", "0xS", ETH_ADDRESS, 100, 9999, 1, 0, 0, 0),
            ("0xB", "0xS", ETH_ADDRESS, 100, 9999, 1, 1, 0, 0),
        ]

        status = client.monitor(0, poll_interval=0, timeout=2)
        assert status == EscrowStatus.RELEASED

    def test_monitor_timeout(self):
        w3 = MagicMock()
        client = _make_client(w3)

        # Always return Created
        client.contract.functions.getEscrow.return_value.call.return_value = (
            "0xB", "0xS", ETH_ADDRESS, 100, 9999, 1, 0, 0, 0
        )

        with pytest.raises(TimeoutError):
            client.monitor(0, poll_interval=0, timeout=0.1)


class TestClientTransactions:
    def _setup_tx_client(self):
        w3 = MagicMock()
        client = _make_client(w3)
        # Mock the transaction flow
        w3.eth.account.sign_transaction.return_value = SimpleNamespace(
            raw_transaction=b"\x00"
        )
        w3.eth.send_raw_transaction.return_value = b"\x01" * 32
        w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}
        return w3, client

    def test_release_builds_correct_tx(self):
        w3, client = self._setup_tx_client()
        client.contract.functions.releaseEscrow.return_value.build_transaction.return_value = {
            "to": FAKE_ESCROW_ADDR,
            "value": 0,
        }
        receipt = client.release(42)
        client.contract.functions.releaseEscrow.assert_called_once_with(42)
        assert receipt["status"] == 1

    def test_refund_builds_correct_tx(self):
        w3, client = self._setup_tx_client()
        client.contract.functions.refundEscrow.return_value.build_transaction.return_value = {
            "to": FAKE_ESCROW_ADDR,
            "value": 0,
        }
        receipt = client.refund(7)
        client.contract.functions.refundEscrow.assert_called_once_with(7)
        assert receipt["status"] == 1

    def test_approve_builds_correct_tx(self):
        w3, client = self._setup_tx_client()
        client.contract.functions.approveRelease.return_value.build_transaction.return_value = {
            "to": FAKE_ESCROW_ADDR,
            "value": 0,
        }
        receipt = client.approve(3)
        client.contract.functions.approveRelease.assert_called_once_with(3)
        assert receipt["status"] == 1
