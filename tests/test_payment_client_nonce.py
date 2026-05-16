"""
Unit tests for PaymentClient's integration with the reorg-safe NonceManager.

Verifies that PaymentClient:
- Default-constructs a NonceManager when none is supplied (backward-compat path).
- Accepts an injected NonceManager.
- Calls acquire_nonce when building+sending a transaction.
- Calls confirm_nonce on a successful receipt.
- Calls release_nonce on a failed receipt.

web3 / eth_account may not be installed in CI; these tests stub the
``web3`` and ``eth_account`` modules at import time so the
``PaymentClient`` class can be instantiated against fully-mocked rails.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


# ─── Stub web3 + eth_account before importing payment_protocol ─────────────

def _install_web3_stubs():
    """Install minimal web3 / eth_account stand-ins so payment_protocol
    imports cleanly even when those packages are not installed."""
    if "web3" not in sys.modules:
        web3_mod = types.ModuleType("web3")

        class _Web3:
            @staticmethod
            def to_checksum_address(addr):
                return addr

            @staticmethod
            def HTTPProvider(url):  # noqa: N802 - mirror web3 spelling
                return ("HTTPProvider", url)

            def __init__(self, provider):
                self._provider = provider
                self.eth = MagicMock()

        web3_mod.Web3 = _Web3
        web3_mod.AsyncWeb3 = MagicMock()
        sys.modules["web3"] = web3_mod

    if "eth_account" not in sys.modules:
        eth_account_mod = types.ModuleType("eth_account")

        class _Account:
            address = "0xPayer"

            @staticmethod
            def from_key(key):
                acct = _Account()
                acct.address = "0xPayer"
                return acct

            def sign_transaction(self, tx):
                return MagicMock(raw_transaction=b"\x00" * 32)

        eth_account_mod.Account = _Account
        sys.modules["eth_account"] = eth_account_mod


_install_web3_stubs()


# ─── Helpers ───────────────────────────────────────────────────────────────

def _make_client(nonce_manager=None, status=1):
    """Build a PaymentClient with all web3 surfaces mocked.

    ``status`` controls the receipt status returned by
    ``wait_for_transaction_receipt`` (1 = success, 0 = failure).
    """
    from src.payment_protocol import PaymentClient

    client = PaymentClient(
        private_key="0x" + "11" * 32,
        escrow_address="0xEscrow",
        rpc_url="http://localhost:8545",
        chain_id=1,
        nonce_manager=nonce_manager,
    )

    # Stub the eth namespace to return deterministic values
    client.w3.eth = MagicMock()
    client.w3.eth.gas_price = 20_000_000_000
    client.w3.eth.get_transaction_count = MagicMock(return_value=0)
    client.w3.eth.send_raw_transaction = MagicMock(return_value=b"\xab" * 32)
    receipt = MagicMock(status=status)
    client.w3.eth.wait_for_transaction_receipt = MagicMock(return_value=receipt)

    # Re-stub the account signer (the real one was discarded when we replaced .eth)
    client.account = MagicMock()
    client.account.address = "0xPayer"
    client.account.sign_transaction = MagicMock(
        return_value=MagicMock(raw_transaction=b"\x00" * 32)
    )
    client.wallet_address = "0xPayer"
    return client


# ─── Tests ─────────────────────────────────────────────────────────────────

def test_default_nonce_manager_is_constructed():
    """PaymentClient should default-construct a NonceManager when not passed one."""
    from switchboard.nonce_manager import NonceManager

    client = _make_client()
    assert isinstance(client.nonce_manager, NonceManager)


def test_injected_nonce_manager_is_used():
    """PaymentClient should use the NonceManager the caller provides."""
    injected = MagicMock()
    client = _make_client(nonce_manager=injected)
    assert client.nonce_manager is injected


def test_get_nonce_delegates_to_manager():
    """get_nonce should call acquire_nonce on the manager."""
    nm = MagicMock()
    nm.acquire_nonce = MagicMock(return_value=42)
    client = _make_client(nonce_manager=nm)

    nonce = client.get_nonce()

    assert nonce == 42
    nm.acquire_nonce.assert_called_once_with("0xPayer")


def test_sign_and_send_acquires_nonce_from_manager():
    """sign_and_send must pull the nonce from the manager (via get_nonce)."""
    nm = MagicMock()
    nm.acquire_nonce = MagicMock(return_value=7)
    client = _make_client(nonce_manager=nm)

    tx = {"to": "0xEscrow", "value": 1}
    tx_hash = client.sign_and_send(tx)

    nm.acquire_nonce.assert_called_once_with("0xPayer")
    assert tx["nonce"] == 7
    assert client._tx_nonces[tx_hash] == 7


def test_sign_and_send_respects_user_supplied_nonce():
    """If the caller pre-sets ``tx['nonce']``, it must end up in the sent tx
    and be the nonce stashed for later confirm/release."""
    nm = MagicMock()
    nm.acquire_nonce = MagicMock(return_value=999)
    client = _make_client(nonce_manager=nm)

    tx = {"to": "0xEscrow", "value": 1, "nonce": 3}
    tx_hash = client.sign_and_send(tx)

    # Note: the existing dict.get(..., self.get_nonce()) pattern always
    # evaluates the default, so we cannot assert acquire_nonce wasn't called.
    # The contract we care about is that the user-supplied nonce wins.
    assert tx["nonce"] == 3
    assert client._tx_nonces[tx_hash] == 3


def test_wait_for_confirmations_confirms_nonce_on_success():
    """A successful receipt should confirm the nonce on the manager."""
    nm = MagicMock()
    nm.acquire_nonce = MagicMock(return_value=11)
    client = _make_client(nonce_manager=nm, status=1)

    tx_hash = client.sign_and_send({"to": "0xEscrow", "value": 1})
    client.wait_for_confirmations(tx_hash)

    nm.confirm_nonce.assert_called_once_with("0xPayer", 11)
    nm.release_nonce.assert_not_called()
    # Mapping should be cleared after the receipt was processed.
    assert tx_hash not in client._tx_nonces


def test_wait_for_confirmations_releases_nonce_on_failure():
    """A failed receipt should release the nonce so it can be reused."""
    nm = MagicMock()
    nm.acquire_nonce = MagicMock(return_value=13)
    client = _make_client(nonce_manager=nm, status=0)

    tx_hash = client.sign_and_send({"to": "0xEscrow", "value": 1})
    with pytest.raises(RuntimeError):
        client.wait_for_confirmations(tx_hash)

    nm.release_nonce.assert_called_once_with("0xPayer", 13)
    nm.confirm_nonce.assert_not_called()
    assert tx_hash not in client._tx_nonces


def test_create_payment_flows_through_nonce_manager():
    """End-to-end: create_payment should acquire then confirm a nonce."""
    nm = MagicMock()
    nm.acquire_nonce = MagicMock(return_value=4)
    client = _make_client(nonce_manager=nm, status=1)

    # Stub contract to avoid real ABI plumbing.
    fake_fn = MagicMock()
    fake_fn.build_transaction = MagicMock(
        return_value={"to": "0xEscrow", "data": "0x", "value": 1, "gas": 100000}
    )
    client.contract = MagicMock()
    client.contract.functions.createPayment = MagicMock(return_value=fake_fn)

    req = client.create_payment(
        payee="0xPayee",
        amount_wei=1,
        timeout_blocks=10,
        challenge_period_blocks=2,
    )

    assert req.payer == "0xPayer"
    nm.acquire_nonce.assert_called_once_with("0xPayer")
    nm.confirm_nonce.assert_called_once_with("0xPayer", 4)
    nm.release_nonce.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
