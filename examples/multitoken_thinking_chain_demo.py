#!/usr/bin/env python3
"""Watchable multi-token settlement demo driven by HanzoEscrowThinkingChain.

Demonstrates:
  - 2 agents: Payer (Hanzo AI) holds USDC; Payee (Meridian) accepts DAI + LUX.
  - Partner tokens LUX and ZOO featured in payer's offer list.
  - negotiate_settlement_token() picks LUX (the common token with highest rank).
  - SwapSettlementAdapter handles USDC -> LUX swap before escrow creation.
  - HanzoEscrowThinkingChain runs all 6 steps with clear console output.
  - Second run: low-tier agent, policy denial at POLICY_CHECK step (HALT).

Run with:
    python3 examples/multitoken_thinking_chain_demo.py
"""
from __future__ import annotations

import sys
import os
import time

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from switchboard.agent_wallet import AgentWallet
from switchboard.treasury import Treasury
from switchboard.access_policy import (
    AccessPolicy, AgentTier, TierConfig, TokenBucketConfig,
)
from switchboard.escrow_adapters import InMemoryEscrowClient, SwapSettlementAdapter
from switchboard.thinking_chain import (
    HanzoEscrowThinkingChain, StepType, StepOutcome, ChainHaltedError,
)
from src.payment_protocol import SettlementToken

# ─── Token addresses ────────────────────────────────────────────────────────
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAI  = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
LUX  = "0xLUX0000000000000000000000000000000000001"   # partner token
ZOO  = "0xZOO0000000000000000000000000000000000002"   # partner token
CHAIN_ID = 1

AMOUNT = 1_000_000   # 1 USDC (6 decimals)

# ─── ANSI colours ───────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

STEP_ICONS = {
    StepType.ASSESS_TASK:       ">>",
    StepType.NEGOTIATE_TOKEN:   "<>",
    StepType.POLICY_CHECK:      "[]",
    StepType.CREATE_ESCROW:     "##",
    StepType.VERIFY_WORK:       "OK",
    StepType.RELEASE_OR_REFUND: "$$",
}

OUTCOME_COLOURS = {
    StepOutcome.PASS: GREEN,
    StepOutcome.FAIL: RED,
    StepOutcome.HALT: RED,
}


def _print_header(title: str) -> None:
    width = 72
    print(f"\n{BOLD}{'=' * width}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'=' * width}{RESET}")


def _print_step(record) -> None:
    icon = STEP_ICONS.get(record.step_type, ".")
    colour = OUTCOME_COLOURS.get(record.outcome, RESET)
    print(f"\n  [{icon}] {BOLD}{record.step_type.name}{RESET}")
    print(f"     Reasoning : {record.reasoning}")
    print(f"     Outcome   : {colour}{record.outcome.name}{RESET}")
    if record.data:
        # Print data key-value pairs, skip long values
        for k, v in record.data.items():
            val_str = str(v)
            if len(val_str) > 60:
                val_str = val_str[:57] + "..."
            print(f"     {k:12s}: {val_str}")
    if record.events:
        for ev in record.events:
            print(f"     {CYAN}[WalletOpEvent] op={ev.op_type} token={ev.token[:16]}... "
                  f"denied={ev.denied}{RESET}")


def run_happy_path() -> None:
    _print_header("DEMO 1: Happy Path -- USDC payer, DAI+LUX payee (LUX negotiated)")

    # ── Setup ────────────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Agent Setup{RESET}")
    print(f"  * Payer  : Hanzo AI Agent  (holds USDC, offers USDC / LUX / ZOO)")
    print(f"  * Payee  : Meridian Agent  (accepts DAI / LUX)")
    print(f"  * Amount : {AMOUNT:,} base units  (~1 USDC)")
    print(f"  * Chain  : Ethereum mainnet (chain_id=1)")

    treasury = Treasury()
    treasury.credit(chain_id=CHAIN_ID, token=USDC, amount=10_000_000)
    wallet = AgentWallet(treasury=treasury)

    # Use a custom TierConfig so the TRUSTED tier can handle 1_000_000
    # (USDC base units, ~1 USDC).  The default cap of 100_000 is intentionally
    # conservative for the default network; demo uses a higher ceiling.
    demo_tier_config = TierConfig(
        explorer=TokenBucketConfig(per_tx_cap=1_000,       rate=1.0,   capacity=10),
        standard=TokenBucketConfig(per_tx_cap=10_000,      rate=10.0,  capacity=50),
        trusted=TokenBucketConfig(per_tx_cap=10_000_000,   rate=100.0, capacity=200),
    )
    policy = AccessPolicy(tier_config=demo_tier_config)
    policy.register("hanzo-agent", AgentTier.TRUSTED)

    escrow_client = InMemoryEscrowClient()
    swap_adapter  = SwapSettlementAdapter(escrow_client)

    # Payer offers USDC (rank 10), LUX (rank 8), ZOO (rank 3)
    payer_offers = [
        SettlementToken(chain_id=CHAIN_ID, token=USDC, min_amount=0, rank=10),
        SettlementToken(chain_id=CHAIN_ID, token=LUX,  min_amount=0, rank=8),
        SettlementToken(chain_id=CHAIN_ID, token=ZOO,  min_amount=0, rank=3),
    ]
    # Payee accepts DAI (rank 10), LUX (rank 7)
    payee_accepts = [
        SettlementToken(chain_id=CHAIN_ID, token=DAI, min_amount=0, rank=10),
        SettlementToken(chain_id=CHAIN_ID, token=LUX, min_amount=0, rank=7),
    ]

    print(f"\n  {BOLD}Token Negotiation Preview{RESET}")
    print(f"  Payer offers  : USDC (rank 10), LUX (rank 8), ZOO (rank 3)")
    print(f"  Payee accepts : DAI  (rank 10), LUX (rank 7)")
    print(f"  Common tokens : LUX  (combined rank = 8+7 = 15)")
    print(f"  No USDC<->DAI common pair -> LUX is the negotiated settlement token")
    print(f"  Payer holds USDC -> SwapSettlementAdapter will simulate USDC->LUX swap")

    # Wire up a custom escrow client that goes through swap for USDC->LUX.
    # We subclass HanzoEscrowThinkingChain and override _step_create_escrow
    # to:
    #   1. Check spendable balance in the SOURCE token (USDC) the wallet holds.
    #   2. Debit the treasury in USDC (the wallet's actual holding).
    #   3. Route through swap_adapter when negotiated token differs from source.
    # This is consistent with the fixed base-class _step_create_escrow which
    # debits the wallet before creating the escrow — but here the source token
    # (USDC) differs from the negotiated token (LUX), so we debit USDC and
    # create the LUX escrow via SwapSettlementAdapter.
    class SwapAwareHanzoChain(HanzoEscrowThinkingChain):
        def __init__(self, swap_adapter: SwapSettlementAdapter, from_token: str, **kw):
            super().__init__(**kw)
            self._swap_adapter = swap_adapter
            self._from_token = from_token

        def _step_create_escrow(self):
            import time as _time
            from switchboard.metrics import WalletOpEvent
            from switchboard.thinking_chain import StepRecord, StepType, StepOutcome

            token = self._negotiated_token.token if self._negotiated_token else ""

            # Financial gate: check spendable balance in the SOURCE token
            # (the token the wallet actually holds — USDC in Demo 1).
            spendable = self._wallet.spendable(self._chain_id, self._from_token)
            if spendable < self._amount:
                return StepRecord(
                    step_type=StepType.CREATE_ESCROW,
                    reasoning=(
                        f"Insufficient spendable balance for source token "
                        f"{self._from_token!r} on chain {self._chain_id}: "
                        f"have {spendable}, need {self._amount}. Halting chain."
                    ),
                    outcome=StepOutcome.HALT,
                    data={
                        "from_token": self._from_token,
                        "token": token,
                        "amount": self._amount,
                        "spendable": spendable,
                    },
                    events=[],
                )

            # Debit the treasury in the SOURCE token (USDC).
            # The swap_adapter converts USDC to LUX at 1:1 demo rate before
            # creating the escrow — so the escrow is denominated in LUX while
            # the wallet accounting entry is in USDC.
            self._wallet.treasury.debit(self._chain_id, self._from_token, self._amount)

            if token != self._from_token:
                # Different token: go through swap (USDC -> LUX)
                eid = self._swap_adapter.swap_and_create(
                    chain_id=self._chain_id,
                    from_token=self._from_token,
                    to_token=token,
                    amount=self._amount,
                    payee=self._payee,
                )
                swap_path = f"{self._from_token[:6]}...->LUX (1:1 demo rate)"
            else:
                # Same token: direct escrow creation (no swap needed)
                eid = self._escrow.create_payment(
                    chain_id=self._chain_id,
                    token=token,
                    amount=self._amount,
                    payee=self._payee,
                )
                swap_path = "no swap (tokens match)"

            self._escrow_id = eid
            balance_after = self._wallet.spendable(self._chain_id, self._from_token)

            ev = WalletOpEvent(
                op_type="create_escrow_via_swap",
                token=token,
                rail="escrow",
                amount=float(self._amount),
                agent_id=self._agent_id,
                wallet_id=self._wallet.address(),
                denied=False,
                denial_reason=None,
                timestamp=_time.time(),
            )
            return StepRecord(
                step_type=StepType.CREATE_ESCROW,
                reasoning=(
                    f"Debited treasury {self._amount} {self._from_token[:10]}... (USDC); "
                    f"swapped USDC -> {token[:10]}... (LUX) via SwapSettlementAdapter "
                    f"then created escrow {eid!r}."
                ),
                outcome=StepOutcome.PASS,
                data={
                    "escrow_id": eid,
                    "token": token,
                    "from_token": self._from_token,
                    "amount": self._amount,
                    "swap_path": swap_path,
                    "balance_after": balance_after,
                },
                events=[ev],
            )

    chain = SwapAwareHanzoChain(
        swap_adapter=swap_adapter,
        from_token=USDC,
        payer_wallet=wallet,
        payee_address="0xMeridian000000000000000000000000000",
        payer_offers=payer_offers,
        payee_accepts=payee_accepts,
        amount=AMOUNT,
        access_policy=policy,
        agent_id="hanzo-agent",
        escrow_client=escrow_client,
        chain_id=CHAIN_ID,
    )

    print(f"\n  {BOLD}Thinking Chain Execution{RESET}")
    try:
        records = chain.run()
        for rec in records:
            _print_step(rec)
            time.sleep(0.05)

        print(f"\n  {GREEN}{BOLD}Settlement complete.{RESET}")
        final_eid = next(r for r in records if r.step_type == StepType.CREATE_ESCROW).data.get("escrow_id", "?")
        final_escrow = escrow_client.get_escrow(final_eid)
        print(f"  Escrow {final_eid!r}: state={final_escrow['state']!r}, "
              f"token={final_escrow['token'][:16]}..., amount={final_escrow['amount']}")
    except ChainHaltedError as e:
        print(f"\n  {RED}{BOLD}Chain halted: {e.reason}{RESET}")


def run_policy_denial() -> None:
    _print_header("DEMO 2: Policy Denial -- Explorer-tier agent blocked at POLICY_CHECK")

    print(f"\n  {BOLD}Agent Setup{RESET}")
    print(f"  * Payer  : Low-Tier Bot   (Explorer tier, per_tx_cap = 1,000)")
    print(f"  * Amount : {AMOUNT:,} base units  (far exceeds Explorer cap)")
    print(f"  * Expected: HALT at POLICY_CHECK step")

    treasury = Treasury()
    treasury.credit(chain_id=CHAIN_ID, token=USDC, amount=10_000_000)
    wallet = AgentWallet(treasury=treasury)

    policy = AccessPolicy()
    policy.register("low-tier-bot", AgentTier.EXPLORER)   # per_tx_cap = 1,000

    payer_offers = [SettlementToken(chain_id=CHAIN_ID, token=USDC, min_amount=0, rank=10)]
    payee_accepts = [SettlementToken(chain_id=CHAIN_ID, token=USDC, min_amount=0, rank=10)]

    chain = HanzoEscrowThinkingChain(
        payer_wallet=wallet,
        payee_address="0xPayee",
        payer_offers=payer_offers,
        payee_accepts=payee_accepts,
        amount=AMOUNT,   # 1_000_000 >> Explorer cap of 1_000
        access_policy=policy,
        agent_id="low-tier-bot",
        chain_id=CHAIN_ID,
    )

    print(f"\n  {BOLD}Thinking Chain Execution{RESET}")
    try:
        chain.run()
        print(f"\n  {RED}Expected a halt but got success -- something is wrong!{RESET}")
    except ChainHaltedError as e:
        # Print records up to halt
        for rec in chain.records:
            _print_step(rec)
            time.sleep(0.05)
        print(f"\n  {YELLOW}{BOLD}Chain halted as expected: {e.reason}{RESET}")
        print(f"  Halted at step: {e.step_record.step_type.name}")
        print(f"  No escrow was created -- funds were never touched.")
        print(f"  {GREEN}Policy enforcement working correctly.{RESET}")


def main() -> None:
    print(f"\n{BOLD}{'=' * 72}{RESET}")
    print(f"{BOLD}  SWITCHBOARD -- Multi-Token Thinking Chain Demo{RESET}")
    print(f"{BOLD}  Escrow primitive + LUX/ZOO partner tokens + swap path{RESET}")
    print(f"{BOLD}{'=' * 72}{RESET}")

    run_happy_path()
    print()
    run_policy_denial()

    print(f"\n{BOLD}{'=' * 72}{RESET}")
    print(f"{BOLD}  Demo complete.{RESET}")
    print(f"{BOLD}{'=' * 72}{RESET}\n")


if __name__ == "__main__":
    main()
