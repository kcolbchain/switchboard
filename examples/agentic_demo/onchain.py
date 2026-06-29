"""In-memory on-chain substrate for the agentic demo.

The real :class:`src.payment_protocol.PaymentClient` and
:class:`switchboard.x402_middleware.X402Middleware` talk to a node via web3.
For a runnable, node-free demo we provide a ``MockChain`` ledger plus a
``MockPaymentClient`` that implements exactly the surface those components call:

    - ``wallet_address``
    - ``sign_and_send(tx)``           (direct value transfer — x402 EXACT scheme)
    - ``wait_for_confirmations(tx)``
    - ``create_payment(payee, amount_wei, ...)``   (escrow lock — ESCROW scheme)
    - ``confirm_payment(request_id)``              (escrow release)
    - ``get_payment_state(request_id)``

It models a minimal **AgentEscrow** (lock -> confirm -> release / refund) on top
of a balance ledger, so the demo exercises the genuine
``402 offer -> pay -> settle`` state machine, just against memory instead of a
testnet. Swapping in a real ``PaymentClient`` against an RPC needs no code change
upstream — the scenario only depends on this surface.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class EscrowState(Enum):
    LOCKED = "Locked"
    CONFIRMED = "Confirmed"
    RELEASED = "Released"
    REFUNDED = "Refunded"
    CANCELLED = "Cancelled"


@dataclass
class _Escrow:
    request_id: str
    payer: str
    payee: str
    amount_wei: int
    created_block: int
    timeout_blocks: int
    challenge_period: int
    state: EscrowState = EscrowState.LOCKED


@dataclass
class _PaymentRequest:
    """Mirror of src.payment_protocol.PaymentRequest's load-bearing fields."""

    request_id: str
    payer: str
    payee: str
    amount_wei: int
    status: str = "locked"


class MockChain:
    """A toy ledger: ETH/native balances per address + an escrow vault."""

    def __init__(self) -> None:
        self.balances: dict[str, int] = {}
        self.escrows: dict[str, _Escrow] = {}
        self.block_number: int = 0
        self.tx_log: list[dict] = []

    def fund(self, address: str, amount_wei: int) -> None:
        self.balances[address] = self.balances.get(address, 0) + amount_wei

    def balance_of(self, address: str) -> int:
        return self.balances.get(address, 0)

    def mine(self, blocks: int = 1) -> None:
        self.block_number += blocks

    # — direct transfer (x402 EXACT) —
    def transfer(self, frm: str, to: str, amount_wei: int) -> str:
        if self.balances.get(frm, 0) < amount_wei:
            raise RuntimeError(f"insufficient balance: {frm} has {self.balances.get(frm,0)} < {amount_wei}")
        self.balances[frm] -= amount_wei
        self.balances[to] = self.balances.get(to, 0) + amount_wei
        self.mine()
        tx_hash = "0x" + hashlib.sha256(f"{frm}{to}{amount_wei}{uuid.uuid4()}".encode()).hexdigest()
        self.tx_log.append({"type": "transfer", "from": frm, "to": to, "amount": amount_wei, "tx": tx_hash})
        return tx_hash

    # — escrow lifecycle (AgentEscrow) —
    def escrow_lock(self, payer: str, payee: str, amount_wei: int, request_id: str,
                    timeout_blocks: int, challenge_period: int) -> str:
        if self.balances.get(payer, 0) < amount_wei:
            raise RuntimeError(f"insufficient balance to lock: {payer}")
        self.balances[payer] -= amount_wei
        esc = _Escrow(
            request_id=request_id, payer=payer, payee=payee, amount_wei=amount_wei,
            created_block=self.block_number, timeout_blocks=timeout_blocks,
            challenge_period=challenge_period,
        )
        self.escrows[request_id] = esc
        self.mine()
        tx_hash = "0x" + hashlib.sha256(f"lock{request_id}".encode()).hexdigest()
        self.tx_log.append({"type": "escrow_lock", "request_id": request_id, "amount": amount_wei, "tx": tx_hash})
        return tx_hash

    def escrow_confirm(self, request_id: str) -> str:
        esc = self.escrows[request_id]
        if esc.state is not EscrowState.LOCKED:
            raise RuntimeError(f"escrow {request_id} not in Locked state: {esc.state}")
        esc.state = EscrowState.RELEASED
        self.balances[esc.payee] = self.balances.get(esc.payee, 0) + esc.amount_wei
        self.mine()
        tx_hash = "0x" + hashlib.sha256(f"release{request_id}".encode()).hexdigest()
        self.tx_log.append({"type": "escrow_release", "request_id": request_id,
                            "payee": esc.payee, "amount": esc.amount_wei, "tx": tx_hash})
        return tx_hash

    def escrow_refund(self, request_id: str) -> str:
        esc = self.escrows[request_id]
        unlock_block = esc.created_block + esc.timeout_blocks + esc.challenge_period
        if self.block_number < unlock_block:
            raise RuntimeError(
                f"challenge period not over: available at block {unlock_block}, current {self.block_number}"
            )
        esc.state = EscrowState.REFUNDED
        self.balances[esc.payer] = self.balances.get(esc.payer, 0) + esc.amount_wei
        self.mine()
        return "0x" + hashlib.sha256(f"refund{request_id}".encode()).hexdigest()

    def escrow_state(self, request_id: str) -> EscrowState:
        return self.escrows[request_id].state


class MockPaymentClient:
    """Implements the PaymentClient surface used by X402Middleware + the scenario,
    backed by a :class:`MockChain`. No node required."""

    def __init__(self, chain: MockChain, wallet_address: str):
        self.chain = chain
        self.wallet_address = wallet_address
        self.pending_payments: dict[str, _PaymentRequest] = {}

    # — used by X402Middleware EXACT path —
    def sign_and_send(self, tx: dict) -> str:
        return self.chain.transfer(self.wallet_address, tx["to"], int(tx["value"]))

    def wait_for_confirmations(self, tx_hash: str, confirmations: int | None = None) -> dict:
        return {"status": 1, "transactionHash": tx_hash}

    # — used by X402Middleware ESCROW path + scenario —
    def create_payment(self, payee: str, amount_wei: int, timeout_blocks: int = 50,
                       challenge_period_blocks: int = 10, request_id: str | None = None,
                       description: str = "", metadata: dict | None = None) -> _PaymentRequest:
        request_id = request_id or str(uuid.uuid4())
        self.chain.escrow_lock(
            payer=self.wallet_address, payee=payee, amount_wei=amount_wei,
            request_id=request_id, timeout_blocks=timeout_blocks,
            challenge_period=challenge_period_blocks,
        )
        req = _PaymentRequest(request_id=request_id, payer=self.wallet_address,
                              payee=payee, amount_wei=amount_wei, status="locked")
        self.pending_payments[request_id] = req
        return req

    def confirm_payment(self, request_id: str) -> bool:
        self.chain.escrow_confirm(request_id)
        if request_id in self.pending_payments:
            self.pending_payments[request_id].status = "confirmed"
        return True

    def request_refund(self, request_id: str) -> bool:
        self.chain.escrow_refund(request_id)
        if request_id in self.pending_payments:
            self.pending_payments[request_id].status = "refunded"
        return True

    def get_payment_state(self, request_id: str) -> str:
        return self.chain.escrow_state(request_id).value

    def get_balance(self) -> int:
        return self.chain.balance_of(self.wallet_address)
