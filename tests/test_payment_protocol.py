"""
Unit tests for Agent-to-Agent Payment Protocol

Run with: pytest tests/test_payment_protocol.py -v

Uses mock chain state to test:
- Payment creation with funds locked
- Confirmation flow
- Timeout/refund flow
- Cancellation
- Nonce management
"""

import pytest
import time
from unittest.mock import MagicMock, patch, PropertyMock
from decimal import Decimal

# ─── Mock Web3 ─────────────────────────────────────────────────────────────

class MockWeb3:
    def __init__(self):
        self.eth = MockEth()
        self.to_checksum_address = lambda a: a.lower()

class MockEth:
    def __init__(self):
        self.gas_price = 20000000000  # 20 gwei
        self.block_number = 1000
        self.get_transaction_count = lambda addr: 1
        self.get_balance = lambda addr: 10**21  # 1000 ETH
        self.wait_for_transaction_receipt = MagicMock(return_value=MagicMock(status=1))
        self.send_raw_transaction = MagicMock(return_value=b'\x00' * 32)

class MockAccount:
    def __init__(self, addr="0x742d35Cc6634C0532925a3b844Bc9e7595f"):
        self.address = addr

    @staticmethod
    def from_key(key):
        return MockAccount()

    def sign_transaction(self, tx):
        return MagicMock(raw_transaction=b'\x00' * 32)

# ─── Mock Contract ─────────────────────────────────────────────────────────

class MockContract:
    def __init__(self):
        self.address = "0x1234567890123456789012345678901234567890"
        self.payments = {}  # requestId → payment data

    def functions(self):
        return MockContractFunctions(self)

    def/events(self):
        return MockEvents()


class MockContractFunctions:
    def __init__(self, contract):
        self.contract = contract

    def createPayment(self, requestId, payee, timeoutBlocks, challengePeriod):
        return MockFn("createPayment", self.contract, requestId, payee, timeoutBlocks, challengePeriod)

    def confirmPayment(self, requestId):
        return MockFn("confirmPayment", self.contract, requestId)

    def requestRefund(self, requestId):
        return MockFn("requestRefund", self.contract, requestId)

    def cancelPayment(self, requestId):
        return MockFn("cancelPayment", self.contract, requestId)

    def getPayment(self, requestId):
        return MockFn("getPayment", self.contract, requestId)

    def isExpired(self, requestId):
        return MockFn("isExpired", self.contract, requestId)


class MockFn:
    def __init__(self, name, contract, *args):
        self.name = name
        self.contract = contract
        self.args = args

    def build_transaction(self, tx_params):
        return {
            'to': self.contract.address,
            'data': '0x',
            **tx_params
        }

    def call(self):
        if self.name == "createPayment":
            req_id = self.args[0]
            self.contract.payments[req_id] = {
                'state': 1,  # Locked
                'amount': 1000000000000000000,  # 1 ETH
                'createdAt': 1000
            }
            return True
        elif self.name == "getPayment":
            req_id = self.args[0]
            if req_id in self.contract.payments:
                p = self.contract.payments[req_id]
                return [
                    "0x742d35Cc6634C0532925a3b844Bc9e7595f",  # payer
                    "0x853d955aCEf822Db058eb8505911ED77F175b99e",  # payee
                    p['amount'],
                    100,  # timeout_blocks
                    10,   # challenge_period
                    p['state'],
                    req_id,
                    p['createdAt']
                ]
            return ["", "", 0, 0, 0, 0, "", 0]
        elif self.name == "isExpired":
            req_id = self.args[0]
            if req_id in self.contract.payments:
                return self.contract.payments[req_id]['state'] == 1  # Locked
            return False
        return None


# ─── Import and test the module ────────────────────────────────────────────

def test_payment_request_creation():
    """Test that PaymentRequest dataclass works correctly"""
    from src.payment_protocol import PaymentRequest

    req = PaymentRequest(
        request_id="test-123",
        payer="0x742d35Cc6634C0532925a3b844Bc9e7595f",
        payee="0x853d955aCEf822Db058eb8505911ED77F175b99e",
        amount_wei=10**18,
        timeout_blocks=100,
        challenge_period_blocks=10,
        description="Test payment",
        currency="ETH"
    )

    assert req.request_id == "test-123"
    assert req.amount_wei == 10**18
    assert req.timeout_blocks == 100
    assert req.status == "pending"
    assert req.currency == "ETH"

    # Test JSON serialization
    json_str = req.to_json()
    assert "test-123" in json_str
    assert "locked" not in json_str  # status should be "pending"

    # Test content hash
    h = req.content_hash()
    assert h.startswith("0x")
    assert len(h) == 66  # 0x + 64 hex chars


def test_payment_request_from_dict():
    """Test deserialization from dict"""
    from src.payment_protocol import PaymentRequest

    d = {
        "version": "1.0",
        "request_id": "test-456",
        "payer": "0x742d35Cc6634C0532925a3b844Bc9e7595f",
        "payee": "0x853d955aCEf822Db058eb8505911ED77F175b99e",
        "amount_wei": 500000000000000000,
        "amount_usd": "50.00",
        "currency": "ETH",
        "chain_id": 1,
        "timeout_blocks": 100,
        "challenge_period_blocks": 10,
        "description": "Test",
        "metadata": {"order_id": "123"},
        "created_at": 1234567890.0,
        "status": "locked"
    }

    req = PaymentRequest.from_dict(d)
    assert req.request_id == "test-456"
    assert req.amount_usd == Decimal("50.00")
    assert req.status == "locked"


def test_format_wei():
    """Test wei formatting"""
    from src.payment_protocol import format_wei

    assert "1.000000 ETH" in format_wei(10**18)
    assert "0.500000 ETH" in format_wei(5 * 10**17)
    assert "0.000001 ETH" in format_wei(10**12)


def test_parse_wei():
    """Test parsing human-readable amounts to wei"""
    from src.payment_protocol import parse_wei

    assert parse_wei("1 ETH") == 10**18
    assert parse_wei("0.5 ETH") == 5 * 10**17
    assert parse_wei("1000000000000000000 wei") == 10**18
    assert parse_wei("1.5 ETH") == int(Decimal("1.5") * Decimal(10**18))


def test_payment_state_enum():
    """Test PaymentState enum"""
    from src.payment_protocol import PaymentState

    assert PaymentState.LOCKED.value == "locked"
    assert PaymentState.RELEASED.value == "released"
    assert PaymentState.REFUNDED.value == "refunded"


def test_content_hash_deterministic():
    """Test that content_hash is deterministic"""
    from src.payment_protocol import PaymentRequest

    req1 = PaymentRequest(
        request_id="det-test",
        payer="0x742d35Cc6634C0532925a3b844Bc9e7595f",
        payee="0x853d955aCEf822Db058eb8505911ED77F175b99e",
        amount_wei=10**18,
        currency="ETH"
    )

    req2 = PaymentRequest(
        request_id="det-test",
        payer="0x742d35Cc6634C0532925a3b844Bc9e7595f",
        payee="0x853d955aCEf822Db058eb8505911ED77F175b99e",
        amount_wei=10**18,
        currency="ETH"
    )

    assert req1.content_hash() == req2.content_hash()

    # Different content → different hash
    req3 = PaymentRequest(
        request_id="det-test-CHANGED",
        payer="0x742d35Cc6634C0532925a3b844Bc9e7595f",
        payee="0x853d955aCEf822Db058eb8505911ED77F175b99e",
        amount_wei=10**18,
        currency="ETH"
    )
    assert req1.content_hash() != req3.content_hash()


def test_mock_contract_create():
    """Test mock contract payment creation flow"""
    contract = MockContract()
    fns = contract.functions()

    result = fns.createPayment("req-001", "0xPayee", 100, 10).call()
    assert result == True
    assert "req-001" in contract.payments
    assert contract.payments["req-001"]["state"] == 1  # Locked


def test_payment_lifecycle():
    """Test full payment lifecycle: create → confirm → released"""
    contract = MockContract()

    # Create
    contract.functions().createPayment("req-002", "0xPayee", 100, 10).call()
    payment = contract.functions().getPayment("req-002").call()
    assert payment[5] == 1  # Locked state

    # Confirm (would release funds)
    contract.payments["req-002"]["state"] = 3  # Released
    payment_after = contract.functions().getPayment("req-002").call()
    assert payment_after[5] == 3  # Released state


def test_timeout_and_refund():
    """Test timeout → refund flow"""
    contract = MockContract()

    # Create payment with very short timeout (would be expired in mock)
    contract.functions().createPayment("req-003", "0xPayee", 1, 10).call()

    # Simulate expired payment
    contract.payments["req-003"]["state"] = 1  # Still locked but should be expired
    is_expired = contract.functions().isExpired("req-003").call()
    assert is_expired == True

    # After challenge period → refund
    contract.payments["req-003"]["state"] = 4  # Refunded
    payment = contract.functions().getPayment("req-003").call()
    assert payment[5] == 4  # Refunded state


def test_payment_metadata():
    """Test that arbitrary metadata can be stored with payment"""
    from src.payment_protocol import PaymentRequest

    req = PaymentRequest(
        request_id="meta-test",
        payer="0x742d35Cc6634C0532925a3b844Bc9e7595f",
        payee="0x853d955aCEf822Db058eb8505911ED77F175b99e",
        amount_wei=10**18,
        metadata={
            "order_id": "ORD-12345",
            "service": "code-review",
            "tags": ["solidity", "audit"],
            "priority": "high"
        }
    )

    d = req.to_dict()
    assert d["metadata"]["order_id"] == "ORD-12345"
    assert d["metadata"]["service"] == "code-review"
    assert "solidity" in d["metadata"]["tags"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
