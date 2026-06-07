"""Tests for Lucidly syUSD auto-park adapter."""

from switchboard.adapters.lucidly import LucidlyAutoPark, LucidlyConfig, MockLucidlyVault


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
