"""Tests for per-epoch gas budget enforcement — issue #78.

Issue #78 promotes the rolling-window gas budget to a per-agent *epoch* cap
surfaced as a non-blocking runtime hint to the block builder. The epoch source
is an injectable provider (block number or finalized timestamp), NEVER raw
wall-clock, because a wall-clock reset is reorg-unsafe (see
docs/security/gas-budget-threat-model.md, "Premature epoch reset by time or
reorg manipulation").

These tests exercise:
- opt-in default (per_epoch=None / no provider => no enforcement),
- epoch-window enforcement in can_spend()/record() alongside hour/day,
- auto-reset of cumulative epoch spend on epoch transition,
- auto-unpause on epoch transition,
- multi-epoch spend accounting,
- the read-only get_hint() runtime-hint API (no mutation).
"""

from __future__ import annotations

import pytest

from switchboard.gas_manager import (
    BudgetExhausted,
    GasManager,
    GasLimits,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
)


class FakeClock:
    """Deterministic monotonically-controllable wall-clock (seconds)."""

    def __init__(self, start: float = 1_700_000_000.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeEpoch:
    """Deterministic injectable epoch source (e.g. a block number).

    Stands in for the chain-side epoch provider. NOT wall-clock derived: the
    caller advances it explicitly, modelling a finalized block/epoch boundary.
    """

    def __init__(self, start: int = 100):
        self._e = start

    def __call__(self) -> int:
        return self._e

    def advance(self, n: int = 1) -> None:
        self._e += n


WALLET = "0xAgent"


# ======================================================================
# Opt-in default: no provider / per_epoch=None => zero behaviour change
# ======================================================================


class TestEpochOptIn:

    def test_no_epoch_provider_means_no_epoch_enforcement(self):
        # per_epoch is set but NO provider injected => epoch window is inert.
        m = GasManager(default_limits=GasLimits(per_epoch=1_000))
        m.record(WALLET, 5_000)  # far over per_epoch, but no provider => no cap
        assert m.is_paused(WALLET) is False
        assert m.can_spend(WALLET, 5_000) is True

    def test_per_epoch_none_means_no_epoch_enforcement(self):
        # provider present but per_epoch=None => epoch window is inert.
        epoch = FakeEpoch()
        m = GasManager(default_limits=GasLimits(per_hour=1_000), epoch_provider=epoch)
        # per_epoch defaults to None, so only the hourly cap binds.
        assert GasLimits().per_epoch is None
        m.record(WALLET, 999)
        assert m.is_paused(WALLET) is False
        # Hourly cap still works exactly as before.
        m.record(WALLET, 1)
        assert m.is_paused(WALLET) is True

    def test_existing_hour_day_behaviour_unchanged_with_provider(self):
        clock = FakeClock()
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_hour=100, per_day=200),
            clock=clock,
            epoch_provider=epoch,
        )
        m.record(WALLET, 100)
        assert m.is_paused(WALLET) is True  # hour cap hit
        clock.advance(SECONDS_PER_HOUR + 1)
        assert m.can_spend(WALLET, 10) is True  # hour window rolled


# ======================================================================
# Epoch enforcement in can_spend()/record()
# ======================================================================


class TestEpochEnforcement:

    def test_epoch_cap_blocks_overspend(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=100_000), epoch_provider=epoch
        )
        assert m.can_spend(WALLET, 60_000) is True
        m.record(WALLET, 60_000)
        assert m.can_spend(WALLET, 40_000) is True
        assert m.can_spend(WALLET, 40_001) is False

    def test_record_pauses_when_epoch_cap_hit(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 1_000)
        assert m.is_paused(WALLET) is True
        assert m.can_spend(WALLET, 1) is False

    def test_check_raises_when_epoch_exhausted(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=500), epoch_provider=epoch
        )
        m.record(WALLET, 500)
        with pytest.raises(BudgetExhausted) as exc:
            m.check(WALLET, 1)
        assert exc.value.args[0].wallet == WALLET

    def test_epoch_independent_of_hour_and_day(self):
        # Epoch cap is the tightest binding constraint here.
        clock = FakeClock()
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_hour=10_000, per_day=100_000, per_epoch=1_000),
            clock=clock,
            epoch_provider=epoch,
        )
        m.record(WALLET, 1_000)
        assert m.is_paused(WALLET) is True
        # Rolling the hour window does NOT clear the epoch spend.
        clock.advance(SECONDS_PER_HOUR + 1)
        assert m.can_spend(WALLET, 1) is False  # epoch still exhausted
        assert m.status(WALLET).spent_this_epoch == 1_000


# ======================================================================
# Epoch transition: auto-reset of spend + auto-unpause
# ======================================================================


class TestEpochTransition:

    def test_epoch_transition_resets_spend(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 1_000)
        assert m.status(WALLET).spent_this_epoch == 1_000

        epoch.advance()  # new epoch
        assert m.status(WALLET).spent_this_epoch == 0
        assert m.can_spend(WALLET, 1_000) is True

    def test_epoch_transition_auto_unpauses(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 1_000)
        assert m.is_paused(WALLET) is True

        epoch.advance()
        assert m.is_paused(WALLET) is False
        assert m.can_spend(WALLET, 1_000) is True

    def test_epoch_transition_does_not_unpause_if_hour_still_exhausted(self):
        # Auto-unpause on epoch reset must NOT override a still-binding
        # hour/day cap. Epoch reset clears epoch spend only.
        clock = FakeClock()
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_hour=1_000, per_epoch=1_000),
            clock=clock,
            epoch_provider=epoch,
        )
        m.record(WALLET, 1_000)  # hits both hour and epoch
        assert m.is_paused(WALLET) is True

        epoch.advance()  # epoch spend resets, but hour window still full
        assert m.is_paused(WALLET) is True
        assert m.can_spend(WALLET, 1) is False
        assert m.status(WALLET).spent_this_epoch == 0  # epoch did reset

    def test_skipping_epochs_still_resets(self):
        # A reorg-free jump of several epochs (e.g. builder offline) resets once.
        epoch = FakeEpoch(start=100)
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 800)
        epoch.advance(5)  # jump 100 -> 105
        assert m.status(WALLET).spent_this_epoch == 0
        assert m.status(WALLET).epoch_id == 105


# ======================================================================
# Multi-epoch spend accounting
# ======================================================================


class TestMultiEpochSpend:

    def test_spend_accumulates_within_epoch_resets_across(self):
        epoch = FakeEpoch(start=1)
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 300)
        m.record(WALLET, 400)
        assert m.status(WALLET).spent_this_epoch == 700

        epoch.advance()
        m.record(WALLET, 250)
        assert m.status(WALLET).spent_this_epoch == 250

        epoch.advance()
        m.record(WALLET, 999)
        assert m.status(WALLET).spent_this_epoch == 999
        assert m.is_paused(WALLET) is False  # 999 < 1000

    def test_wallets_have_independent_epoch_spend(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record("a", 1_000)
        assert m.is_paused("a") is True
        assert m.is_paused("b") is False
        assert m.status("b").spent_this_epoch == 0


# ======================================================================
# Runtime-hint API: get_hint() — read-only, no mutation
# ======================================================================


class TestRuntimeHint:

    def test_hint_reports_epoch_spend_and_remaining(self):
        epoch = FakeEpoch(start=42)
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 600)

        hint = m.get_hint(WALLET)
        assert hint.epoch_id == 42
        assert hint.spent_this_epoch == 600
        assert hint.remaining == 400

    def test_hint_will_fit(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 600)

        hint = m.get_hint(WALLET)
        assert hint.will_fit(400) is True
        assert hint.will_fit(401) is False
        assert hint.will_fit(0) is True

    def test_hint_is_read_only_no_mutation(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 600)

        before = m.status(WALLET)
        for _ in range(5):
            h = m.get_hint(WALLET)
            h.will_fit(10_000)  # querying must not mutate state
        after = m.status(WALLET)

        assert before.spent_this_epoch == after.spent_this_epoch == 600
        assert after.paused is False

    def test_hint_reflects_epoch_transition(self):
        epoch = FakeEpoch(start=7)
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.record(WALLET, 900)
        assert m.get_hint(WALLET).remaining == 100

        epoch.advance()
        hint = m.get_hint(WALLET)
        assert hint.epoch_id == 8
        assert hint.spent_this_epoch == 0
        assert hint.remaining == 1_000

    def test_hint_when_epoch_disabled(self):
        # No provider => no epoch enforcement. The hint must be safe to call
        # and must signal "unbounded": epoch_id is None and everything fits.
        m = GasManager(default_limits=GasLimits(per_hour=1_000))
        m.record(WALLET, 500)
        hint = m.get_hint(WALLET)
        assert hint.epoch_id is None
        assert hint.remaining is None
        assert hint.will_fit(10**12) is True

    def test_hint_when_no_per_epoch_cap_but_provider_present(self):
        # Provider present, per_epoch=None => epoch tracked for visibility but
        # remaining is unbounded (None) and everything fits.
        epoch = FakeEpoch(start=5)
        m = GasManager(default_limits=GasLimits(per_hour=1_000), epoch_provider=epoch)
        m.record(WALLET, 500)
        hint = m.get_hint(WALLET)
        assert hint.epoch_id == 5
        assert hint.spent_this_epoch == 500
        assert hint.remaining is None
        assert hint.will_fit(10**12) is True


# ======================================================================
# Per-wallet limit overrides still work with epoch caps
# ======================================================================


class TestEpochPerWalletLimits:

    def test_per_wallet_epoch_override(self):
        epoch = FakeEpoch()
        m = GasManager(
            default_limits=GasLimits(per_epoch=1_000), epoch_provider=epoch
        )
        m.set_limits("0xVIP", GasLimits(per_epoch=10_000))
        m.record("0xVIP", 5_000)
        m.record(WALLET, 900)
        assert m.is_paused("0xVIP") is False
        assert m.can_spend(WALLET, 200) is False  # default 1_000 epoch cap binds
