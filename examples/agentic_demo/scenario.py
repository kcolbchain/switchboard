"""Agentic payments scenario: Agent A pays Agent B, then B swaps via SafeSwap.

Flow exercised end-to-end (offline, node-free):

  1. Agent A asks Agent B's paid endpoint for work (an inference job).
  2. Agent B replies ``402 Payment Required`` with an x402 ``PaymentOffer``
     (escrow scheme — funds locked, released on delivery).
  3. Agent A's :class:`X402Middleware` validates the offer against policy
     (cap, allowlist, gas budget), pays into escrow, and retries with proof.
  4. Agent B delivers the work; Agent A *settles* the escrow (confirm -> release).
  5. **Agentic swap**: now-funded Agent B routes the received USDC through the
     **SafeSwap** orchestrator into a target asset (ETH for gas), getting a
     best-execution route + receipt.

The scenario depends only on the real ``switchboard`` package surface plus the
mock substrate in this package, so the same code runs against a live RPC +
SafeSwap by swapping in a real ``PaymentClient`` and ``SafeSwapClient(base_url=...)``.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from switchboard.gas_tracker import GasTracker
from switchboard.x402_middleware import (
    PaymentOffer,
    PaymentProof,
    PaymentScheme,
    X402Middleware,
)

from .onchain import EscrowState, MockChain, MockPaymentClient
from .safeswap import (
    MockSafeSwapOrchestrator,
    SafeSwapClient,
    SwapReceipt,
    SwapRequest,
)


# token decimals (USDC 6dp on the wire; we keep ETH in wei = 18dp)
USDC = 10**6
ETH = 10**18


class BudgetGuard:
    """Adapts the real :class:`switchboard.gas_tracker.GasTracker` to the
    duck-typed contract :class:`X402Middleware` expects from a ``gas_tracker``:
    ``can_send_transaction(wallet, amount)`` and ``record_gas_usage(wallet, amount)``.

    The stdlib ``GasTracker`` is a process singleton with single-arg methods; this
    wrapper drops the per-wallet arg the middleware passes (the tracker enforces a
    global budget) and resets state on construction so repeated demo/test runs are
    independent.
    """

    def __init__(self, hourly_limit: int, daily_limit: int):
        self._tracker = GasTracker(hourly_limit=hourly_limit, daily_limit=daily_limit)
        # GasTracker is a singleton; clear any leaked state then apply our limits.
        self._tracker.reset_all()
        self._tracker.set_limits(hourly_limit=hourly_limit, daily_limit=daily_limit)

    def can_send_transaction(self, wallet: str, amount: int) -> bool:
        return self._tracker.can_send_transaction(amount)

    def record_gas_usage(self, wallet: str, amount: int) -> None:
        self._tracker.record_gas_usage(amount)


@dataclass
class StepLog:
    """One ledger-visible event in the flow, for printing + assertions."""

    step: str
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class ScenarioResult:
    offer: PaymentOffer
    proof: PaymentProof
    escrow_request_id: str
    escrow_state_after_settle: str
    swap_receipt: SwapReceipt
    agent_b_balance_token_out: int
    steps: list[StepLog]
    spend_summary: dict

    @property
    def settled(self) -> bool:
        return self.escrow_state_after_settle == EscrowState.RELEASED.value

    @property
    def swap_routed(self) -> bool:
        return bool(self.swap_receipt and self.swap_receipt.route and self.swap_receipt.amount_out > 0)


class AgentBEndpoint:
    """Agent B's paid 'inference' endpoint, x402-gated with the ESCROW scheme.

    Returns a 402 ``PaymentOffer`` on a cold call, then serves the work once a
    valid ``X-Payment-Proof`` for the escrow request is presented.
    """

    def __init__(self, recipient: str, price_units: int = 5 * USDC, chain_id: int = 8453):
        self.recipient = recipient
        self.price_units = price_units
        self.chain_id = chain_id
        self.served: list[str] = []

    def offer(self, endpoint: str) -> PaymentOffer:
        return PaymentOffer(
            amount_wei=self.price_units,
            currency="USDC",
            recipient=self.recipient,
            chain_id=self.chain_id,
            scheme=PaymentScheme.ESCROW,
            description="agent-B inference job",
            endpoint=endpoint,
            nonce=uuid.uuid4().hex[:16],
            expires_at=int(time.time()) + 300,
        )

    def deliver(self, proof: PaymentProof) -> dict:
        """Verify the proof carries an escrow ref + serve the deliverable."""
        if not proof.tx_hash:
            raise ValueError("missing payment proof")
        self.served.append(proof.tx_hash)
        return {
            "status": 200,
            "result": {"embedding_dim": 1536, "tokens": 4096, "job": proof.tx_hash},
        }


def run_scenario(
    *,
    chain: MockChain | None = None,
    safeswap: SafeSwapClient | None = None,
    agent_a_addr: str = "0xA0A0a0a0a0A0a0A0a0A0a0a0a0A0a0a0A0A0A0a0",
    agent_b_addr: str = "0xB0b0B0b0b0b0b0B0b0b0b0b0B0b0b0B0b0B0B0b0",
    price_units: int = 5 * USDC,
    swap_to: str = "ETH",
    verbose: bool = False,
) -> ScenarioResult:
    """Run the full A2A pay -> settle -> swap flow and return a structured result."""

    steps: list[StepLog] = []

    def record(step: str, detail: str, **data) -> None:
        steps.append(StepLog(step, detail, data))
        if verbose:
            print(f"[{step}] {detail}")

    chain = chain or MockChain()
    safeswap = safeswap or SafeSwapClient(transport=MockSafeSwapOrchestrator())

    # Fund Agent A with USDC to pay for work.
    chain.fund(agent_a_addr, 100 * USDC)
    record("setup", f"Agent A funded with {100} USDC; Agent B starts empty",
           agent_a_balance=chain.balance_of(agent_a_addr))

    # — Agent A's payment stack —
    payment_client = MockPaymentClient(chain, wallet_address=agent_a_addr)
    gas_tracker = BudgetGuard(hourly_limit=50 * USDC, daily_limit=200 * USDC)
    middleware = X402Middleware(
        payment_client=payment_client,
        gas_tracker=gas_tracker,
        max_payment_wei=20 * USDC,
        allowed_recipients={agent_b_addr},
    )

    # — Agent B's paid endpoint —
    agent_b = AgentBEndpoint(recipient=agent_b_addr, price_units=price_units, chain_id=8453)
    endpoint = "https://agent-b.example/v1/inference"

    # 1. Cold call -> 402 offer
    offer = agent_b.offer(endpoint)
    record("402", f"Agent B -> 402 Payment Required: {price_units / USDC} USDC (escrow)",
           recipient=offer.recipient, amount=offer.amount_wei, scheme=offer.scheme.value)

    # 2. Agent A validates + pays into escrow (reuses real middleware logic)
    middleware._validate_offer(offer)
    record("validate", "Agent A: offer passes policy (cap / allowlist / gas budget)")

    proof = middleware._pay_onchain(offer)  # ESCROW path -> create_payment -> lock
    escrow_request_id = proof.tx_hash
    gas_tracker.record_gas_usage(agent_a_addr, offer.amount_wei)
    middleware.total_spent_wei += offer.amount_wei
    record("pay", f"Agent A locked {price_units / USDC} USDC in escrow",
           request_id=escrow_request_id,
           escrow_state=payment_client.get_payment_state(escrow_request_id))

    # 3. Agent B delivers the work against the proof
    delivery = agent_b.deliver(proof)
    record("deliver", f"Agent B delivered work (HTTP {delivery['status']})", result=delivery["result"])

    # 4. Agent A settles: confirm -> release escrow to Agent B
    payment_client.confirm_payment(escrow_request_id)
    state_after = payment_client.get_payment_state(escrow_request_id)
    record("settle", f"Agent A confirmed -> escrow {state_after}; Agent B paid",
           escrow_state=state_after, agent_b_balance=chain.balance_of(agent_b_addr))

    # record the completed payment for the spend summary
    from switchboard.x402_middleware import PaymentRecord
    middleware.payment_history.append(
        PaymentRecord(endpoint=endpoint, offer=offer, proof=proof, response_status=delivery["status"])
    )

    # 5. AGENTIC SWAP — Agent B routes received USDC through SafeSwap into swap_to
    received = chain.balance_of(agent_b_addr)
    swap_req = SwapRequest(
        token_in="USDC", token_out=swap_to, amount_in=received,
        chain_id=offer.chain_id, recipient=agent_b_addr,
    )
    quote = safeswap.quote(swap_req)
    record("swap.quote", f"SafeSwap quote: {received / USDC} USDC -> {quote.amount_out} {swap_to} units "
                         f"via {' -> '.join(quote.route)}", route=quote.route, amount_out=quote.amount_out)
    receipt = safeswap.execute(quote)
    record("swap.execute", f"SafeSwap routed swap settled (tx {receipt.tx_hash[:12]}...)",
           tx=receipt.tx_hash, amount_out=receipt.amount_out, route=receipt.route)

    return ScenarioResult(
        offer=offer,
        proof=proof,
        escrow_request_id=escrow_request_id,
        escrow_state_after_settle=state_after,
        swap_receipt=receipt,
        agent_b_balance_token_out=receipt.amount_out,
        steps=steps,
        spend_summary=middleware.get_spend_summary(),
    )
