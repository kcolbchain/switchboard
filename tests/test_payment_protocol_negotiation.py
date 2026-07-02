"""
Unit ⑥ — Token negotiation tests for payment_protocol.py (v1.2)

Tests:
- deterministic pick: same inputs → same negotiated token
- highest combined-rank wins
- no-common-token → None
- v1.1 back-compat: PaymentRequest without settlement_token still parses fine
- SettlementToken dataclass fields
"""

import pytest
from src.payment_protocol import (
    SettlementToken,
    negotiate_settlement_token,
    PaymentRequest,
)


# ─── SettlementToken structure ───────────────────────────────────────────────

class TestSettlementToken:
    def test_fields(self):
        tok = SettlementToken(chain_id=8453, token="0xUSDS", min_amount=0, rank=1)
        assert tok.chain_id == 8453
        assert tok.token == "0xUSDS"
        assert tok.rank == 1

    def test_equality_by_chain_and_token(self):
        a = SettlementToken(chain_id=1, token="0xA", min_amount=0, rank=2)
        b = SettlementToken(chain_id=1, token="0xA", min_amount=5, rank=10)
        # Two tokens on the same chain+address are the same settlement instrument
        # regardless of min_amount or rank — equality on (chain_id, token).
        assert a.chain_id == b.chain_id
        assert a.token == b.token


# ─── negotiate_settlement_token ───────────────────────────────────────────────

class TestNegotiateSettlementToken:
    # Canonical token fixtures
    ETH_BASE = SettlementToken(chain_id=8453, token="0x0000000000000000000000000000000000000000", min_amount=0, rank=1)
    USDC_BASE = SettlementToken(chain_id=8453, token="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", min_amount=0, rank=2)
    DAI_BASE  = SettlementToken(chain_id=8453, token="0x6B175474E89094C44Da98b954EedeAC495271d0F", min_amount=0, rank=3)
    LUX_BASE  = SettlementToken(chain_id=8453, token="0xLUXaddress", min_amount=0, rank=4)

    def test_single_common_token(self):
        payer_offer  = [self.USDC_BASE]
        payee_accepts = [self.USDC_BASE]
        result = negotiate_settlement_token(payer_offer, payee_accepts)
        assert result is not None
        assert result.token == self.USDC_BASE.token

    def test_deterministic_pick(self):
        """Same inputs always pick the same token."""
        payer_offer  = [self.USDC_BASE, self.DAI_BASE]
        payee_accepts = [self.DAI_BASE, self.USDC_BASE]
        r1 = negotiate_settlement_token(payer_offer, payee_accepts)
        r2 = negotiate_settlement_token(payer_offer, payee_accepts)
        assert r1 is not None
        assert r1.token == r2.token

    def test_highest_combined_rank_wins(self):
        """
        Combined rank = payer_rank + payee_rank; higher rank = more preferred.
        Payer ranks DAI=3 higher than USDC=2.
        Payee ranks DAI=5 higher than USDC=1.
        Combined: DAI=8, USDC=3 → DAI wins.
        """
        payer_usdc = SettlementToken(chain_id=8453, token="0xUSDS", min_amount=0, rank=2)
        payer_dai  = SettlementToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=3)
        payee_usdc = SettlementToken(chain_id=8453, token="0xUSDS", min_amount=0, rank=1)
        payee_dai  = SettlementToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=5)

        result = negotiate_settlement_token([payer_usdc, payer_dai], [payee_usdc, payee_dai])
        assert result is not None
        assert result.token == "0xDAI"

    def test_no_common_token_returns_none(self):
        payer_offer  = [self.ETH_BASE]
        payee_accepts = [self.USDC_BASE]
        result = negotiate_settlement_token(payer_offer, payee_accepts)
        assert result is None

    def test_empty_payer_returns_none(self):
        result = negotiate_settlement_token([], [self.USDC_BASE])
        assert result is None

    def test_empty_payee_returns_none(self):
        result = negotiate_settlement_token([self.USDC_BASE], [])
        assert result is None

    def test_both_empty_returns_none(self):
        result = negotiate_settlement_token([], [])
        assert result is None

    def test_multiple_common_picks_highest_combined_rank(self):
        """
        Three common tokens — pick the one with highest combined rank.
        Payer:  ETH=1, USDC=2, DAI=3
        Payee:  DAI=1, USDC=3, ETH=2
        Combined: ETH=3, USDC=5, DAI=4 → USDC wins (5).
        """
        payer = [
            SettlementToken(chain_id=8453, token="0xETH",  min_amount=0, rank=1),
            SettlementToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2),
            SettlementToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=3),
        ]
        payee = [
            SettlementToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=1),
            SettlementToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=3),
            SettlementToken(chain_id=8453, token="0xETH",  min_amount=0, rank=2),
        ]
        result = negotiate_settlement_token(payer, payee)
        assert result is not None
        assert result.token == "0xUSDC"

    def test_cross_chain_tokens_dont_match(self):
        """Tokens on different chain_ids should not intersect."""
        eth_mainnet = SettlementToken(chain_id=1,    token="0xUSDC", min_amount=0, rank=5)
        eth_base    = SettlementToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=5)
        result = negotiate_settlement_token([eth_mainnet], [eth_base])
        assert result is None

    def test_tiebreaker_is_deterministic(self):
        """
        When combined ranks are tied, result must still be deterministic (not random).
        Pick the lexicographically smallest token address as a stable tiebreaker.
        """
        tok_a = SettlementToken(chain_id=1, token="0xAAAA", min_amount=0, rank=2)
        tok_b = SettlementToken(chain_id=1, token="0xBBBB", min_amount=0, rank=2)
        payer  = [tok_a, tok_b]
        payee  = [
            SettlementToken(chain_id=1, token="0xAAAA", min_amount=0, rank=2),
            SettlementToken(chain_id=1, token="0xBBBB", min_amount=0, rank=2),
        ]
        r1 = negotiate_settlement_token(payer, payee)
        r2 = negotiate_settlement_token(list(reversed(payer)), list(reversed(payee)))
        assert r1 is not None
        assert r1.token == r2.token  # deterministic


# ─── PaymentRequest v1.2 — settlement_token field ─────────────────────────────

class TestPaymentRequestV12:
    def test_settlement_token_field_present(self):
        """PaymentRequest now has a settlement_token field (defaults None)."""
        req = PaymentRequest(
            request_id="r1",
            payer="0xPAYER",
            payee="0xPAYEE",
            amount_wei=10**18,
        )
        assert hasattr(req, "settlement_token")
        assert req.settlement_token is None

    def test_settlement_token_can_be_set(self):
        tok = SettlementToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2)
        req = PaymentRequest(
            request_id="r2",
            payer="0xPAYER",
            payee="0xPAYEE",
            amount_wei=10**18,
            settlement_token=tok,
        )
        assert req.settlement_token is not None
        assert req.settlement_token.token == "0xUSDC"

    def test_v11_back_compat_from_dict_no_settlement_token(self):
        """A v1.1 dict without settlement_token must still parse cleanly."""
        d = {
            "version": "1.1",
            "request_id": "v11-test",
            "payer": "0xPAYER",
            "payee": "0xPAYEE",
            "amount_wei": 10**18,
            "currency": "ETH",
            "chain_id": 1,
            "timeout_blocks": 100,
            "challenge_period_blocks": 10,
            "description": "",
            "metadata": {},
            "created_at": 1234567890.0,
            "status": "pending",
        }
        req = PaymentRequest.from_dict(d)
        assert req.settlement_token is None
        assert req.currency == "ETH"

    def test_currency_alias_still_works(self):
        """currency field is retained as v1.1-compatible alias for ETH profile."""
        req = PaymentRequest(
            request_id="alias-test",
            payer="0xPAYER",
            payee="0xPAYEE",
            amount_wei=10**18,
            currency="ETH",
        )
        assert req.currency == "ETH"

    def test_settlement_token_serializes_to_dict(self):
        """settlement_token should appear in to_dict() output."""
        tok = SettlementToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2)
        req = PaymentRequest(
            request_id="ser-test",
            payer="0xPAYER",
            payee="0xPAYEE",
            amount_wei=10**18,
            settlement_token=tok,
        )
        d = req.to_dict()
        assert "settlement_token" in d

    def test_content_hash_excludes_settlement_token(self):
        """
        settlement_token is a negotiated result (like status); it MUST NOT
        change the content_hash so both sides can agree on the hash before
        negotiation is finalised.
        """
        req_bare = PaymentRequest(
            request_id="hash-test",
            payer="0xPAYER",
            payee="0xPAYEE",
            amount_wei=10**18,
        )
        tok = SettlementToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2)
        req_with_tok = PaymentRequest(
            request_id="hash-test",
            payer="0xPAYER",
            payee="0xPAYEE",
            amount_wei=10**18,
            settlement_token=tok,
        )
        assert req_bare.content_hash() == req_with_tok.content_hash()
