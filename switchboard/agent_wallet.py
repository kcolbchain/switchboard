"""AgentWallet — the Python agent-facing wallet (Unit ⑧).

Wraps ``MPCWallet`` (keeps threshold signing / no single point of failure)
and adds:

* ``Treasury`` — per-(chain_id, token) balance tracking.
* ``EscrowClient`` — a thin **Protocol** seam for the on-chain escrow.
  The real escrow client is NOT available in this worktree yet; tests run
  against a mock.  Wire the real client in by passing an implementation of
  ``EscrowClient`` at construction time.

The ``pay(request) -> receipt`` entrypoint is the primary agent interface.
It validates the request, debits the treasury, and drives the escrow.

Seam for future wiring
----------------------
``EscrowClient`` is a ``typing.Protocol`` with two required methods::

    create_payment(chain_id, token, amount, payee) -> str   # escrow_id
    release_payment(escrow_id) -> bool

Downstream units (the Router, FleetBalancer, etc.) plug in between
``AgentWallet.pay`` and the escrow call — see spec §4.3 and §4.4.

Usage::

    from switchboard.mpc_wallet import MPCWallet
    from switchboard.treasury import Treasury
    from switchboard.agent_wallet import AgentWallet, PaymentRequest

    mpc = MPCWallet()
    treasury = Treasury()
    treasury.credit(chain_id=1, token=USDC, amount=1_000_000_000)

    wallet = AgentWallet(mpc=mpc, treasury=treasury, escrow=my_escrow_client)
    receipt = wallet.pay(PaymentRequest(chain_id=1, token=USDC, amount_wei=100_000_000, payee="0x..."))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from switchboard.mpc_wallet import MPCWallet
from switchboard.treasury import Treasury, InsufficientBalance

# Canonical payment-request type (protocol v1.2, src/payment_protocol.py).
# AgentWallet speaks the *same* PaymentRequest as the settlement protocol so
# there is a single request shape across the wallet, delegation, MCP, and the
# on-chain negotiation.  It carries a multi-token ``token`` field and an
# ``amount`` alias for ``amount_wei`` (see src/payment_protocol.py).
from src.payment_protocol import PaymentRequest  # noqa: F401 — re-exported


class WalletError(RuntimeError):
    """Base error for AgentWallet operations."""


# ---------------------------------------------------------------------------
# EscrowClient — the thin seam for the (not-yet-available) on-chain client.
# ---------------------------------------------------------------------------

@runtime_checkable
class EscrowClient(Protocol):
    """Protocol that any on-chain escrow client must satisfy.

    Seam note
    ---------
    The real ``MultiTokenAgentEscrow`` client (Unit ① / ③) is not present in
    this worktree.  Tests pass a ``MagicMock(spec=EscrowClient)`` instead.
    When the escrow client lands, wire it in by passing an instance that
    implements these two methods.

    ``create_payment`` creates an escrow entry and returns an opaque
    escrow_id string.  ``release_payment`` triggers release of held funds.
    """

    def create_payment(
        self,
        chain_id: int,
        token: str,
        amount: int,
        payee: str,
    ) -> str:
        """Create an escrow entry; return an opaque escrow_id."""
        ...

    def release_payment(self, escrow_id: str) -> bool:
        """Release the escrowed funds to the payee; return True on success."""
        ...


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
#
# ``PaymentRequest`` is imported from ``src.payment_protocol`` (the canonical
# protocol type) at the top of this module — AgentWallet no longer defines its
# own.  A request must carry ``chain_id``, ``token``, ``amount`` (alias of
# ``amount_wei``), and ``payee``.


@dataclass(frozen=True)
class PaymentReceipt:
    """Returned by ``AgentWallet.pay()`` on success."""

    tx_id: str          # The MPC-signed tx hash or escrow_id
    chain_id: int
    token: str
    amount: int
    payee: str
    escrow_id: Optional[str] = None


# ---------------------------------------------------------------------------
# AgentWallet
# ---------------------------------------------------------------------------

class AgentWallet:
    """Agent-facing wallet: wraps MPCWallet, manages Treasury, drives escrow.

    Parameters
    ----------
    mpc:
        The underlying MPC threshold-signing wallet (must not be None).
    treasury:
        Per-(chain_id, token) balance store.  If None, a fresh empty
        Treasury is created (useful for tests that inject balances later).
    escrow:
        An object satisfying the ``EscrowClient`` Protocol.  If None, a
        no-op stub is used (payments will not hit any chain — only for
        testing treasury logic in isolation).
    """

    def __init__(
        self,
        mpc: Optional[MPCWallet] = None,
        treasury: Optional[Treasury] = None,
        escrow: Optional[EscrowClient] = None,
    ) -> None:
        self._mpc = mpc if mpc is not None else MPCWallet()
        self.treasury: Treasury = treasury if treasury is not None else Treasury()
        self._escrow: EscrowClient = escrow if escrow is not None else _NoOpEscrow()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def address(self) -> str:
        """Return the wallet's EVM address (from the underlying MPCWallet)."""
        return self._mpc.address()

    # ------------------------------------------------------------------
    # Treasury delegation
    # ------------------------------------------------------------------

    def balance(self, chain_id: int, token: str) -> int:
        """Total balance for (chain_id, token)."""
        return self.treasury.balance(chain_id, token)

    def spendable(self, chain_id: int, token: str) -> int:
        """Spendable balance (balance minus reserve) for (chain_id, token)."""
        return self.treasury.spendable(chain_id, token)

    # ------------------------------------------------------------------
    # pay()
    # ------------------------------------------------------------------

    def pay(self, request: PaymentRequest) -> PaymentReceipt:
        """Execute a payment on behalf of the wallet.

        Flow
        ----
        1. Validate the request (non-zero amount, sufficient balance).
        2. Debit the treasury atomically (InsufficientBalance bubbles up).
        3. Sign via MPCWallet.
        4. Create and release an escrow entry via the EscrowClient seam.
        5. Return a PaymentReceipt.

        The Router (Unit ⑩-⑫) plugs in between steps 1 and 2 in a later
        unit — the plan intentionally leaves a narrow seam here.
        """
        if request.amount <= 0:
            raise WalletError(f"Payment amount must be positive, got {request.amount}")

        # Check spendable balance before debiting.
        spendable = self.treasury.spendable(request.chain_id, request.token)
        if spendable < request.amount:
            raise InsufficientBalance(
                f"Insufficient spendable balance: have {spendable}, need {request.amount}"
            )

        # Debit treasury (atomic; raises InsufficientBalance on race).
        self.treasury.debit(request.chain_id, request.token, request.amount)

        # Sign the transaction via MPC.
        tx = {
            "chain_id": request.chain_id,
            "token": request.token,
            "amount": request.amount,
            "payee": request.payee,
        }
        tx_hash = self._mpc.sign_and_send(tx)

        # Create and immediately release the escrow.
        escrow_id = self._escrow.create_payment(
            chain_id=request.chain_id,
            token=request.token,
            amount=request.amount,
            payee=request.payee,
        )
        self._escrow.release_payment(escrow_id)

        return PaymentReceipt(
            tx_id=tx_hash,
            chain_id=request.chain_id,
            token=request.token,
            amount=request.amount,
            payee=request.payee,
            escrow_id=escrow_id,
        )


# ---------------------------------------------------------------------------
# No-op stub used when no EscrowClient is provided.
# ---------------------------------------------------------------------------

class _NoOpEscrow:
    """Stub escrow that does nothing — useful for treasury-only tests."""

    def create_payment(self, chain_id: int, token: str, amount: int, payee: str) -> str:
        return "0xnoop"

    def release_payment(self, escrow_id: str) -> bool:
        return True
