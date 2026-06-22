"""Tests for Lucidly syUSD auto-park adapter."""

from switchboard.adapters.lucidly import LucidlyAutoPark, LucidlyConfig, MockLucidlyVault
from switchboard.x402_middleware import (
    PaymentOffer,
    PaymentProof,
    PaymentRecord,
    X402Middleware,
)


class SlippingVault(MockLucidlyVault):
    def __init__(self, slippage_bps):
        super().__init__()
        self.slippage_bps = slippage_bps

    def preview_deposit(self, chain: str, amount_usd: float) -> dict:
        return {"shares": amount_usd, "slippage_bps": self.slippage_bps}


def test_rebalance_parks_excess():
    park = LucidlyAutoPark()
    result = park.rebalance("base", liquid_balance_usd=10_000.0)
    assert result["action"] == "parked"
    assert result["amount_usd"] > 0
    assert result["total_parked"] > 0


def test_rebalance_disabled():
    park = LucidlyAutoPark(config=LucidlyConfig(enabled=False))
    result = park.rebalance("base", liquid_balance_usd=10_000.0)
    assert result["action"] == "disabled"


def test_unpark_returns_liquidity():
    park = LucidlyAutoPark()
    park.rebalance("base", liquid_balance_usd=10_000.0)
    returned = park.unpark("base", amount_usd=100.0)
    assert returned > 0


def test_unpark_zero_when_empty():
    park = LucidlyAutoPark()
    returned = park.unpark("base", amount_usd=100.0)
    assert returned == 0.0


def test_ensure_liquid_no_action_needed():
    park = LucidlyAutoPark()
    returned = park.ensure_liquid("base", required_usd=100, liquid_balance_usd=500)
    assert returned == 0.0


def test_ensure_liquid_unparks_when_short():
    park = LucidlyAutoPark()
    park.rebalance("base", liquid_balance_usd=10_000.0)
    returned = park.ensure_liquid("base", required_usd=5000, liquid_balance_usd=100)
    assert returned > 0


def test_yield_report():
    park = LucidlyAutoPark()
    park.rebalance("base", liquid_balance_usd=10_000.0)
    report = park.yield_report()
    assert report["total_parked_usd"] > 0
    assert report["positions"] > 0


def test_yield_report_by_chain():
    park = LucidlyAutoPark()
    park.rebalance("base", liquid_balance_usd=10_000.0)
    report = park.yield_report(chain="base")
    assert "total_yield_usd" in report


def test_status():
    park = LucidlyAutoPark()
    park.rebalance("base", liquid_balance_usd=10_000.0)
    status = park.status("base")
    assert status["chain"] == "base"
    assert status["enabled"] is True
    assert status["idle_target_pct"] > 0


def test_status_idle_target_pct_is_a_fraction_matching_config_convention():
    # `idle_target_pct` must use the same bps->fraction convention everywhere:
    # LucidlyConfig.idle_target_pct (= bps / 10_000) and the rebalance math
    # (target_pct = bps / 10_000) both yield a 0..1 fraction. status() used
    # `/ 100` instead, returning a value 100x too large (e.g. 80.0 vs 0.8).
    config = LucidlyConfig(per_chain_targets={"base": 8000}, idle_target_bps=8000)
    park = LucidlyAutoPark(config=config)
    park.rebalance("base", liquid_balance_usd=10_000.0)

    status = park.status("base")
    assert status["idle_target_pct"] == 0.8
    assert status["idle_target_pct"] == config.idle_target_pct
    # A fraction in [0, 1], not a percentage.
    assert 0.0 <= status["idle_target_pct"] <= 1.0


def test_status_idle_target_pct_falls_back_to_default_bps_as_fraction():
    # Chain absent from per_chain_targets falls back to idle_target_bps,
    # still expressed as a 0..1 fraction (not 50.0).
    config = LucidlyConfig(per_chain_targets={}, idle_target_bps=5000)
    park = LucidlyAutoPark(config=config)
    status = park.status("optimism")
    assert status["idle_target_pct"] == 0.5


def test_different_chain_targets():
    config = LucidlyConfig(per_chain_targets={"ethereum": 5000, "base": 8000})
    park = LucidlyAutoPark(config=config)
    base_result = park.rebalance("base", liquid_balance_usd=10_000.0)
    assert base_result["action"] == "parked"


def test_cap_reached():
    config = LucidlyConfig(max_parked_usd=10)
    park = LucidlyAutoPark(config=config)
    r1 = park.rebalance("base", liquid_balance_usd=10_000.0)
    assert r1["action"] == "parked"
    r2 = park.rebalance("base", liquid_balance_usd=10_000.0)
    assert r2["action"] == "cap_reached"


def test_rebalance_skips_when_vault_entry_slippage_exceeds_cap():
    config = LucidlyConfig(max_entry_slippage_bps=25)
    park = LucidlyAutoPark(vault=SlippingVault(slippage_bps=30), config=config)

    result = park.rebalance("base", liquid_balance_usd=10_000.0)

    assert result == {
        "action": "skip_slippage",
        "chain": "base",
        "slippage_bps": 30,
        "max_entry_slippage_bps": 25,
    }
    assert park.status("base")["total_parked_usd"] == 0


def test_ensure_liquid_unparks_to_threshold_buffer():
    config = LucidlyConfig(idle_target_bps=8000, unpark_threshold_bps=1500)
    park = LucidlyAutoPark(config=config)
    park.rebalance("base", liquid_balance_usd=10_000.0)

    returned = park.ensure_liquid("base", required_usd=1_000.0, liquid_balance_usd=1_000.0)

    assert returned == 500.0
    assert park.status("base")["liquid_buffer_usd"] == 1_500.0


# ─── weekly realized-APY card (issue #80 AC #4) ───────────────────────────────

def test_weekly_yield_report_exposes_realized_30d_apy_per_wallet():
    # AC #4: a weekly cron emits a per-wallet `realized_30d_apy` JSON blob to a
    # public surface. The adapter must expose that surface.
    park = LucidlyAutoPark()
    park.rebalance("base", liquid_balance_usd=10_000.0)
    park.vault.simulate_yield(days=30)

    report = park.weekly_yield_report()

    assert report["window_days"] == 30
    assert "realized_30d_apy" in report
    assert report["realized_30d_apy"] >= 0.0
    assert "by_chain" in report
    assert "generated_at" in report


def test_weekly_yield_report_is_json_serializable():
    import json

    park = LucidlyAutoPark()
    park.rebalance("base", liquid_balance_usd=10_000.0)
    # Must serialize cleanly for the public surface the cron writes.
    blob = json.dumps(park.weekly_yield_report())
    assert "realized_30d_apy" in blob


# ─── guarded post-settlement rebalance hook in x402 middleware (issue #80) ─────

def _make_record(amount_wei=1_000_000, chain_id=8453, recipient="0xPAYEE"):
    offer = PaymentOffer(amount_wei=amount_wei, currency="USDC", recipient=recipient, chain_id=chain_id)
    proof = PaymentProof(tx_hash="0xabc", chain_id=chain_id, payer="0xPAYER", amount_wei=amount_wei)
    return PaymentRecord(endpoint="https://agent.example/infer", offer=offer, proof=proof, response_status=200)


class _RecordingPark:
    """Stand-in adapter that records rebalance() calls."""

    def __init__(self):
        self.calls = []

    def rebalance(self, chain, liquid_balance_usd):
        self.calls.append((chain, liquid_balance_usd))
        return {"action": "parked", "chain": chain}


def test_settlement_invokes_lucidly_rebalance_hook():
    # After a payment settles, the middleware must invoke the Lucidly adapter's
    # rebalance so idle float gets parked.
    client = object()  # not used by the synchronous hook path
    park = _RecordingPark()
    mw = X402Middleware(payment_client=client, lucidly_park=park)

    record = _make_record(amount_wei=2_000_000, chain_id=8453)
    mw._post_settlement_rebalance(record)

    assert len(park.calls) == 1
    chain, _liquid = park.calls[0]
    # Base mainnet chain id -> "base" chain key for the adapter.
    assert chain == "base"


def test_settlement_hook_is_a_noop_without_adapter():
    # No adapter configured -> the hook must be a harmless no-op (no crash).
    mw = X402Middleware(payment_client=object())
    mw._post_settlement_rebalance(_make_record())  # must not raise


def test_settlement_hook_is_guarded_against_adapter_errors():
    # A misbehaving adapter must never break payment settlement.
    class _Boom:
        def rebalance(self, *a, **k):
            raise RuntimeError("vault down")

    mw = X402Middleware(payment_client=object(), lucidly_park=_Boom())
    # Guarded: swallows the error rather than propagating into the payment path.
    mw._post_settlement_rebalance(_make_record())


def test_two_parks_same_chain_same_clock_are_distinct_positions():
    # The position key was `chain:clock()`. The adapter takes an injectable
    # deterministic clock ("Pluggable clock for deterministic tests"), so two
    # parks on the same chain at the same clock value mapped to the SAME key —
    # the second ParkedPosition silently overwrote the first, dropping its
    # per-position yield accrual even though `_total_parked` still counted both.
    fixed_t = 1_000_000.0
    park = LucidlyAutoPark(clock=lambda: fixed_t)

    r1 = park.rebalance("base", liquid_balance_usd=10_000.0)
    r2 = park.rebalance("base", liquid_balance_usd=10_000.0)
    assert r1["action"] == "parked"
    assert r2["action"] == "parked"

    report = park.yield_report()
    # Both park events must be retained as distinct positions, and the position
    # count must stay consistent with the parked total (which already sums both).
    assert report["positions"] == 2
    assert report["total_parked_usd"] == r1["amount_usd"] + r2["amount_usd"]
