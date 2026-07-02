import pytest
from switchboard.escrow_adapters import InMemoryEscrowClient, SwapSettlementAdapter
from switchboard.thinking_chain import (
    ThinkingChain, StepRecord, StepType, StepOutcome,
    HanzoEscrowThinkingChain, ChainHaltedError,
)
from switchboard.agent_wallet import AgentWallet
from switchboard.treasury import Treasury
from switchboard.mpc_wallet import MPCWallet
from switchboard.access_policy import AccessPolicy, AgentTier, TierConfig, TokenBucketConfig
from src.payment_protocol import SettlementToken

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAI  = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
LUX  = "0xLUX0000000000000000000000000000000000001"
ZOO  = "0xZOO0000000000000000000000000000000000002"

def test_inmemory_escrow_create_and_release():
    client = InMemoryEscrowClient()
    eid = client.create_payment(chain_id=1, token=USDC, amount=1_000_000, payee="0xPayee")
    assert eid.startswith("escrow-")
    assert client.get_escrow(eid)["state"] == "open"
    result = client.release_payment(eid)
    assert result is True
    assert client.get_escrow(eid)["state"] == "released"

def test_inmemory_escrow_refund():
    client = InMemoryEscrowClient()
    eid = client.create_payment(chain_id=1, token=USDC, amount=500_000, payee="0xPayee")
    result = client.refund_payment(eid)
    assert result is True
    assert client.get_escrow(eid)["state"] == "refunded"

def test_inmemory_escrow_double_release_raises():
    client = InMemoryEscrowClient()
    eid = client.create_payment(chain_id=1, token=USDC, amount=100, payee="0xP")
    client.release_payment(eid)
    with pytest.raises(ValueError, match="not open"):
        client.release_payment(eid)

def test_swap_settlement_adapter_cross_token():
    client = InMemoryEscrowClient()
    adapter = SwapSettlementAdapter(client)
    eid = adapter.swap_and_create(chain_id=1, from_token=USDC, to_token=DAI, amount=1_000_000, payee="0xPayee")
    escrow = client.get_escrow(eid)
    assert escrow["token"] == DAI
    assert escrow["amount"] == 1_000_000
    assert escrow["state"] == "open"


# ---------------------------------------------------------------------------
# Task 2: ThinkingChain tests
# ---------------------------------------------------------------------------

def _make_payer_wallet(token=USDC, amount=5_000_000):
    treasury = Treasury()
    treasury.credit(chain_id=1, token=token, amount=amount)
    return AgentWallet(treasury=treasury)

def _payer_offers():
    return [
        SettlementToken(chain_id=1, token=USDC, min_amount=0, rank=10),
        SettlementToken(chain_id=1, token=LUX,  min_amount=0, rank=5),
        SettlementToken(chain_id=1, token=ZOO,  min_amount=0, rank=3),
    ]

def _payee_accepts_usdc():
    return [SettlementToken(chain_id=1, token=USDC, min_amount=0, rank=10)]

def _payee_accepts_dai():
    return [SettlementToken(chain_id=1, token=DAI, min_amount=0, rank=10)]

def _trusted_policy():
    """AccessPolicy whose TRUSTED tier admits high-value test amounts.

    Uses a custom TierConfig so we exercise the real per-tx-cap plumbing
    without mutating the production _DEFAULT_TIER_CONFIG.
    """
    cfg = TierConfig(
        explorer=TokenBucketConfig(per_tx_cap=1_000,      rate=1.0,   capacity=10),
        standard=TokenBucketConfig(per_tx_cap=10_000,     rate=10.0,  capacity=50),
        trusted=TokenBucketConfig(per_tx_cap=100_000_000, rate=100.0, capacity=200),
    )
    return AccessPolicy(tier_config=cfg)

def test_step_types_are_ordered():
    """Six canonical step types exist and maintain declaration order."""
    types = list(StepType)
    assert types[0] == StepType.ASSESS_TASK
    assert types[-1] == StepType.RELEASE_OR_REFUND
    assert len(types) == 6

def test_step_record_is_frozen():
    from switchboard.metrics import WalletOpEvent
    import time
    ev = WalletOpEvent(op_type="test", token=USDC, rail="", amount=0.0,
                       agent_id="", wallet_id="", denied=False,
                       denial_reason=None, timestamp=time.time())
    rec = StepRecord(
        step_type=StepType.ASSESS_TASK,
        reasoning="test",
        outcome=StepOutcome.PASS,
        data={"k": "v"},
        events=[ev],
    )
    import dataclasses
    assert dataclasses.is_dataclass(rec)
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        rec.outcome = StepOutcome.FAIL  # type: ignore

def test_hanzo_happy_path_settles():
    """Happy path: same token (USDC), policy allows, chain releases escrow."""
    policy = _trusted_policy()
    policy.register("hanzo-agent", AgentTier.TRUSTED)
    wallet = _make_payer_wallet(USDC, 5_000_000)
    chain = HanzoEscrowThinkingChain(
        payer_wallet=wallet,
        payee_address="0xPayee",
        payer_offers=_payer_offers(),
        payee_accepts=_payee_accepts_usdc(),
        amount=1_000_000,
        access_policy=policy,
        agent_id="hanzo-agent",
    )
    records = chain.run()
    assert len(records) == 6
    assert all(r.outcome == StepOutcome.PASS for r in records)
    assert records[2].step_type == StepType.POLICY_CHECK
    assert records[3].step_type == StepType.CREATE_ESCROW
    assert "escrow_id" in records[3].data
    assert records[5].step_type == StepType.RELEASE_OR_REFUND
    assert records[5].data.get("action") == "release"

def test_hanzo_chain_is_inspectable():
    """Records are stored on chain.records after run()."""
    policy = _trusted_policy()
    policy.register("hanzo-agent", AgentTier.TRUSTED)
    wallet = _make_payer_wallet(USDC, 5_000_000)
    chain = HanzoEscrowThinkingChain(
        payer_wallet=wallet,
        payee_address="0xPayee",
        payer_offers=_payer_offers(),
        payee_accepts=_payee_accepts_usdc(),
        amount=1_000_000,
        access_policy=policy,
        agent_id="hanzo-agent",
    )
    chain.run()
    assert len(chain.records) == 6
    # each record carries the step type in declaration order
    for i, st in enumerate(StepType):
        assert chain.records[i].step_type == st

def test_hanzo_denied_by_policy_halts():
    """A policy denial on POLICY_CHECK step raises ChainHaltedError."""
    # Explorer tier with per_tx_cap=1_000 will deny amount=1_000_000
    policy = AccessPolicy()
    policy.register("low-tier-agent", AgentTier.EXPLORER)
    wallet = _make_payer_wallet(USDC, 5_000_000)
    chain = HanzoEscrowThinkingChain(
        payer_wallet=wallet,
        payee_address="0xPayee",
        payer_offers=_payer_offers(),
        payee_accepts=_payee_accepts_usdc(),
        amount=1_000_000,   # exceeds Explorer per_tx_cap=1_000
        access_policy=policy,
        agent_id="low-tier-agent",
    )
    with pytest.raises(ChainHaltedError) as exc_info:
        chain.run()
    err = exc_info.value
    assert err.step_record.step_type == StepType.POLICY_CHECK
    assert err.step_record.outcome == StepOutcome.HALT
    # chain.records contains steps up to and including the halted one
    assert chain.records[-1].step_type == StepType.POLICY_CHECK
    assert chain.records[-1].outcome == StepOutcome.HALT

def test_hanzo_negotiate_no_common_token_halts():
    """If payer and payee share no token, NEGOTIATE_TOKEN step halts."""
    policy = _trusted_policy()
    policy.register("agent-x", AgentTier.TRUSTED)
    wallet = _make_payer_wallet(USDC, 5_000_000)
    # payee only accepts ZOO; payer only offers USDC
    chain = HanzoEscrowThinkingChain(
        payer_wallet=wallet,
        payee_address="0xPayee",
        payer_offers=[SettlementToken(chain_id=1, token=USDC, min_amount=0, rank=10)],
        payee_accepts=[SettlementToken(chain_id=1, token=ZOO, min_amount=0, rank=10)],
        amount=1_000_000,
        access_policy=policy,
        agent_id="agent-x",
    )
    with pytest.raises(ChainHaltedError) as exc_info:
        chain.run()
    assert exc_info.value.step_record.step_type == StepType.NEGOTIATE_TOKEN

def test_thinking_chain_events_collected():
    """Each step that calls real modules emits WalletOpEvent(s) in its record."""
    policy = _trusted_policy()
    policy.register("event-agent", AgentTier.TRUSTED)
    wallet = _make_payer_wallet(USDC, 5_000_000)
    chain = HanzoEscrowThinkingChain(
        payer_wallet=wallet,
        payee_address="0xPayee",
        payer_offers=_payer_offers(),
        payee_accepts=_payee_accepts_usdc(),
        amount=1_000_000,
        access_policy=policy,
        agent_id="event-agent",
    )
    chain.run()
    # The POLICY_CHECK step must have at least one WalletOpEvent
    policy_step = next(r for r in chain.records if r.step_type == StepType.POLICY_CHECK)
    assert len(policy_step.events) >= 1
    # CREATE_ESCROW and RELEASE_OR_REFUND must each emit at least one event
    create_step = next(r for r in chain.records if r.step_type == StepType.CREATE_ESCROW)
    assert len(create_step.events) >= 1
    release_step = next(r for r in chain.records if r.step_type == StepType.RELEASE_OR_REFUND)
    assert len(release_step.events) >= 1


def test_hanzo_create_escrow_debits_wallet():
    """CREATE_ESCROW step debits the wallet treasury — wallet is no longer hollow."""
    policy = _trusted_policy()
    policy.register("debit-agent", AgentTier.TRUSTED)
    wallet = _make_payer_wallet(USDC, 1_500_000)
    chain = HanzoEscrowThinkingChain(
        payer_wallet=wallet,
        payee_address="0xPayee",
        payer_offers=_payer_offers(),
        payee_accepts=_payee_accepts_usdc(),
        amount=1_000_000,
        access_policy=policy,
        agent_id="debit-agent",
    )
    chain.run()
    # Treasury must have been debited by the escrow amount
    assert wallet.balance(1, USDC) == 500_000
