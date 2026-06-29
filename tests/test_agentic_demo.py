"""Tests for the agentic-payments demo (examples/agentic_demo).

Asserts the full flow end-to-end:
    402 offer -> pay -> settle  (escrow Released)  and  the SafeSwap swap routes.

The demo is node/RPC-free: it drives the real ``switchboard`` x402 middleware +
gas budget against an in-memory chain, and the real ``SafeSwapClient`` against an
in-process mock orchestrator.
"""

from __future__ import annotations

import pytest

from examples.agentic_demo import (
    EscrowState,
    MockChain,
    MockSafeSwapOrchestrator,
    SafeSwapClient,
    SafeSwapError,
    SwapRequest,
    run_scenario,
)
from examples.agentic_demo.safeswap import SwapQuote
from examples.agentic_demo.scenario import USDC, AgentBEndpoint


# ─── full flow ───────────────────────────────────────────────────────────────


def test_full_flow_offer_pay_settle_and_swap_routes():
    chain = MockChain()
    transport = MockSafeSwapOrchestrator()
    result = run_scenario(chain=chain, safeswap=SafeSwapClient(transport=transport))

    # 402 offer was made with the escrow scheme + correct price
    assert result.offer.scheme.value == "escrow"
    assert result.offer.amount_wei == 5 * USDC
    assert result.offer.currency == "USDC"

    # pay -> settle: escrow ended Released, not just Locked
    assert result.settled is True
    assert result.escrow_state_after_settle == EscrowState.RELEASED.value
    assert chain.escrow_state(result.escrow_request_id) is EscrowState.RELEASED

    # the swap actually routed through SafeSwap
    assert result.swap_routed is True
    assert result.swap_receipt.token_in == "USDC"
    assert result.swap_receipt.token_out == "ETH"
    assert result.swap_receipt.amount_out > 0
    assert result.swap_receipt.route  # non-empty venue path
    assert result.swap_receipt.tx_hash.startswith("0x")

    # SafeSwap orchestrator was genuinely called: quote then execute
    paths = [p for p, _ in transport.calls]
    assert paths == ["/v1/quote", "/v1/execute"]


def test_step_order_is_offer_pay_deliver_settle_swap():
    result = run_scenario()
    steps = [s.step for s in result.steps]
    # the load-bearing ordering: 402 before pay, settle before swap
    assert steps.index("402") < steps.index("pay") < steps.index("settle")
    assert steps.index("settle") < steps.index("swap.quote") < steps.index("swap.execute")


def test_funds_move_payer_to_payee_then_swap_out():
    chain = MockChain()
    a = "0xAAaA"
    b = "0xBBbB"
    result = run_scenario(chain=chain, agent_a_addr=a, agent_b_addr=b, price_units=5 * USDC)

    # Agent A spent 5 USDC (100 funded - 5), escrow released the 5 to Agent B
    assert chain.balance_of(a) == 95 * USDC
    assert chain.balance_of(b) == 5 * USDC
    # spend summary reflects exactly one settled payment of 5 USDC
    assert result.spend_summary["total_payments"] == 1
    assert result.spend_summary["total_spent_wei"] == 5 * USDC


def test_swap_to_lux_uses_lux_route():
    result = run_scenario(swap_to="LUX")
    assert result.swap_receipt.token_out == "LUX"
    assert "LuxDEX" in result.swap_receipt.route


# ─── escrow state machine ────────────────────────────────────────────────────


def test_escrow_starts_locked_before_settle():
    chain = MockChain()
    # run only up to the lock by inspecting an isolated escrow via the client
    from examples.agentic_demo.onchain import MockPaymentClient

    chain.fund("0xA", 10 * USDC)
    client = MockPaymentClient(chain, "0xA")
    req = client.create_payment("0xB", 5 * USDC)
    assert client.get_payment_state(req.request_id) == EscrowState.LOCKED.value
    assert chain.balance_of("0xB") == 0  # not yet released
    client.confirm_payment(req.request_id)
    assert client.get_payment_state(req.request_id) == EscrowState.RELEASED.value
    assert chain.balance_of("0xB") == 5 * USDC


def test_escrow_refund_blocked_until_challenge_window():
    from examples.agentic_demo.onchain import MockPaymentClient

    chain = MockChain()
    chain.fund("0xA", 10 * USDC)
    client = MockPaymentClient(chain, "0xA")
    req = client.create_payment("0xB", 5 * USDC, timeout_blocks=5, challenge_period_blocks=3)
    with pytest.raises(RuntimeError, match="challenge period not over"):
        client.request_refund(req.request_id)
    chain.mine(20)
    assert client.request_refund(req.request_id) is True
    assert client.get_payment_state(req.request_id) == EscrowState.REFUNDED.value
    assert chain.balance_of("0xA") == 10 * USDC  # fully refunded


# ─── SafeSwap orchestrator ───────────────────────────────────────────────────


def test_safeswap_quote_then_execute_roundtrip():
    client = SafeSwapClient(transport=MockSafeSwapOrchestrator())
    quote = client.quote(SwapRequest(token_in="USDC", token_out="ETH", amount_in=5 * USDC))
    assert isinstance(quote, SwapQuote)
    assert quote.amount_out > 0
    receipt = client.execute(quote)
    assert receipt.amount_out == quote.amount_out
    assert receipt.tx_hash.startswith("0x")


def test_safeswap_fee_is_applied():
    # 5 USDC -> ETH at 0.0004 with 0.30% fee: 5e6 * 0.0004 = 2000, minus 0.3% = 1994
    client = SafeSwapClient(transport=MockSafeSwapOrchestrator())
    quote = client.quote(SwapRequest(token_in="USDC", token_out="ETH", amount_in=5 * USDC))
    assert quote.amount_out == 1994
    assert quote.fee_bps == 30


def test_safeswap_unknown_pair_raises():
    client = SafeSwapClient(transport=MockSafeSwapOrchestrator())
    with pytest.raises(SafeSwapError, match="no SafeSwap route"):
        client.quote(SwapRequest(token_in="DOGE", token_out="ETH", amount_in=100))


def test_safeswap_route_convenience_quotes_and_executes():
    client = SafeSwapClient(transport=MockSafeSwapOrchestrator())
    receipt = client.route(SwapRequest(token_in="USDC", token_out="LUX", amount_in=3 * USDC))
    assert receipt.token_out == "LUX"
    assert receipt.amount_out > 0


# ─── x402 endpoint shape ─────────────────────────────────────────────────────


def test_agent_b_endpoint_offer_and_delivery():
    ep = AgentBEndpoint(recipient="0xB", price_units=2 * USDC)
    offer = ep.offer("https://x/y")
    assert offer.recipient == "0xB"
    assert offer.amount_wei == 2 * USDC
    assert offer.endpoint == "https://x/y"

    from switchboard.x402_middleware import PaymentProof

    proof = PaymentProof(tx_hash="req-1", chain_id=8453, payer="0xA", amount_wei=2 * USDC)
    out = ep.deliver(proof)
    assert out["status"] == 200
    assert "req-1" in ep.served
