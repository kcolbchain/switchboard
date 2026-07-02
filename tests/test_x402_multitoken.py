"""
Unit ⑦ — x402 multi-token accepts[] envelope tests

Tests:
- X402Server can be constructed with a multi-token accepts list
- 402 response advertises accepted tokens in X-Payment-Accepts header
- PaymentRequirements extended with accepts[] entries {chain_id, token, min_amount, rank}
- Middleware validates that an incoming settlement_token is in the accepted set
- Payment in a non-accepted token is rejected
- Payment in an accepted token is allowed
- Back-compat: server without accepts[] still works as before
"""

import json
import pytest
from unittest.mock import MagicMock

from switchboard.x402.server import (
    X402Server,
    PaymentRequirements,
    AcceptedToken,
    PaymentVerifier,
)
from switchboard.x402_middleware import (
    X402Middleware,
    PaymentOffer,
    PaymentScheme,
)


# ─── AcceptedToken structure ─────────────────────────────────────────────────

class TestAcceptedToken:
    def test_fields(self):
        tok = AcceptedToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=1)
        assert tok.chain_id == 8453
        assert tok.token == "0xUSDC"
        assert tok.min_amount == 0
        assert tok.rank == 1

    def test_to_dict(self):
        tok = AcceptedToken(chain_id=8453, token="0xUSDC", min_amount=100, rank=2)
        d = tok.to_dict()
        assert d == {"chain_id": 8453, "token": "0xUSDC", "min_amount": 100, "rank": 2}

    def test_from_dict(self):
        d = {"chain_id": 1, "token": "0xDAI", "min_amount": 0, "rank": 3}
        tok = AcceptedToken.from_dict(d)
        assert tok.chain_id == 1
        assert tok.token == "0xDAI"
        assert tok.rank == 3


# ─── PaymentRequirements multi-token extension ───────────────────────────────

class TestPaymentRequirementsMultiToken:
    def test_accepts_list_in_to_header(self):
        """When accepts is set, to_header() includes it in the JSON output."""
        toks = [
            AcceptedToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2),
            AcceptedToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=1),
        ]
        reqs = PaymentRequirements(
            scheme="exact",
            network="base",
            asset="USDC",
            amount="1.00",
            pay_to="0xPAYEE",
            accepts=toks,
        )
        header = reqs.to_header()
        data = json.loads(header)
        assert "accepts" in data
        assert len(data["accepts"]) == 2
        tokens = {entry["token"] for entry in data["accepts"]}
        assert "0xUSDC" in tokens
        assert "0xDAI" in tokens

    def test_accepts_roundtrips_via_from_dict(self):
        toks = [AcceptedToken(chain_id=1, token="0xUSDC", min_amount=0, rank=5)]
        reqs = PaymentRequirements(
            scheme="exact", network="mainnet", asset="USDC",
            amount="2.00", pay_to="0xPAYEE", accepts=toks,
        )
        header = reqs.to_header()
        reqs2 = PaymentRequirements.from_header(header)
        assert len(reqs2.accepts) == 1
        assert reqs2.accepts[0].token == "0xUSDC"
        assert reqs2.accepts[0].rank == 5

    def test_no_accepts_still_works(self):
        """Back-compat: PaymentRequirements without accepts behaves as before."""
        reqs = PaymentRequirements(
            scheme="exact", network="base", asset="USDC",
            amount="1.00", pay_to="0xPAYEE",
        )
        header = reqs.to_header()
        data = json.loads(header)
        assert "accepts" not in data  # not included when empty

    def test_from_header_without_accepts(self):
        raw = json.dumps({
            "scheme": "exact", "network": "base", "asset": "USDC",
            "amount": "1.00", "payTo": "0xPAYEE",
        })
        reqs = PaymentRequirements.from_header(raw)
        assert reqs.accepts == []


# ─── X402Server multi-token 402 response ─────────────────────────────────────

class TestX402ServerMultiToken:
    def _make_server(self, accepts=None):
        return X402Server(
            pay_to_address="0xPAYEE",
            amount_usdc="1.00",
            accepts=accepts,
        )

    def test_build_402_without_accepts(self):
        server = self._make_server()
        status, headers, body = server.build_402_response()
        assert status == 402
        data = json.loads(body)
        assert "payment_requirements" in data

    def test_build_402_with_accepts_lists_tokens(self):
        toks = [
            AcceptedToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2),
            AcceptedToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=1),
        ]
        server = self._make_server(accepts=toks)
        status, headers, body = server.build_402_response()
        assert status == 402
        data = json.loads(body)
        reqs = data.get("payment_requirements", {})
        assert "accepts" in reqs
        assert len(reqs["accepts"]) == 2

    def test_build_402_header_carries_accepts(self):
        toks = [AcceptedToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2)]
        server = self._make_server(accepts=toks)
        _, headers, _ = server.build_402_response()
        raw = headers.get("X-Payment-Required", "")
        data = json.loads(raw)
        assert "accepts" in data
        assert data["accepts"][0]["token"] == "0xUSDC"


# ─── Middleware — settlement_token validation ──────────────────────────────────

class TestX402MiddlewareMultiToken:
    USDC_BASE = AcceptedToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2)
    DAI_BASE  = AcceptedToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=1)

    def _make_middleware(self, accepted_tokens=None, **kwargs):
        client = MagicMock()
        client.wallet_address = "0xPAYER"
        return X402Middleware(
            payment_client=client,
            accepted_tokens=accepted_tokens,
            **kwargs,
        )

    def test_validate_settlement_token_accepted(self):
        """A settlement_token in the accepted list passes validation."""
        mw = self._make_middleware(accepted_tokens=[self.USDC_BASE, self.DAI_BASE])
        # Should not raise
        mw._validate_settlement_token(chain_id=8453, token="0xUSDC")

    def test_validate_settlement_token_rejected(self):
        """A settlement_token NOT in the accepted list raises ValueError."""
        mw = self._make_middleware(accepted_tokens=[self.USDC_BASE])
        with pytest.raises(ValueError, match="not an accepted settlement token"):
            mw._validate_settlement_token(chain_id=8453, token="0xDAI")

    def test_validate_settlement_token_wrong_chain_rejected(self):
        """Token on wrong chain_id is rejected even if address matches."""
        mw = self._make_middleware(accepted_tokens=[self.USDC_BASE])  # chain_id=8453
        with pytest.raises(ValueError, match="not an accepted settlement token"):
            mw._validate_settlement_token(chain_id=1, token="0xUSDC")  # mainnet

    def test_no_accepted_tokens_bypasses_check(self):
        """When accepted_tokens is not set (None), the check is a no-op (back-compat)."""
        mw = self._make_middleware(accepted_tokens=None)
        # Must not raise regardless of what token is proposed
        mw._validate_settlement_token(chain_id=8453, token="0xANYTHING")

    def test_empty_accepted_tokens_rejects_all(self):
        """An explicit empty list means no token is acceptable."""
        mw = self._make_middleware(accepted_tokens=[])
        with pytest.raises(ValueError, match="not an accepted settlement token"):
            mw._validate_settlement_token(chain_id=8453, token="0xUSDC")

    def test_validate_offer_with_accepted_token_passes(self):
        """_validate_offer passes when offer token is in accepted_tokens list."""
        mw = self._make_middleware(
            accepted_tokens=[self.USDC_BASE],
            max_payment_wei=10**18,
        )
        offer = PaymentOffer(
            amount_wei=1000,
            currency="USDC",
            recipient="0xRECIPIENT",
            chain_id=8453,
            token="0xUSDC",
        )
        mw._validate_offer(offer)  # must not raise

    def test_validate_offer_with_rejected_token_raises(self):
        """_validate_offer raises when offer token is not in accepted_tokens."""
        mw = self._make_middleware(
            accepted_tokens=[self.USDC_BASE],
            max_payment_wei=10**18,
        )
        offer = PaymentOffer(
            amount_wei=1000,
            currency="ZOO",
            recipient="0xRECIPIENT",
            chain_id=8453,
            token="0xZOO",
        )
        with pytest.raises(ValueError, match="not an accepted settlement token"):
            mw._validate_offer(offer)

    def test_validate_offer_no_token_field_bypasses_token_check(self):
        """Offers without a token field (v1.1-style) bypass the token check (back-compat)."""
        mw = self._make_middleware(
            accepted_tokens=[self.USDC_BASE],
            max_payment_wei=10**18,
        )
        offer = PaymentOffer(
            amount_wei=1000,
            currency="ETH",
            recipient="0xRECIPIENT",
            chain_id=8453,
            # token field absent / None
        )
        mw._validate_offer(offer)  # must not raise


# ─── PaymentOffer multi-token extension ──────────────────────────────────────

class TestPaymentOfferMultiToken:
    def test_from_header_with_token_field(self):
        header = json.dumps({
            "amount": "1000000",
            "recipient": "0xRECIPIENT",
            "chainId": 8453,
            "token": "0xUSDC",
        })
        offer = PaymentOffer.from_header(header)
        assert offer.token == "0xUSDC"

    def test_from_header_without_token_field(self):
        """Back-compat: no token field → offer.token is None."""
        header = json.dumps({
            "amount": "1000000",
            "recipient": "0xRECIPIENT",
            "chainId": 8453,
        })
        offer = PaymentOffer.from_header(header)
        assert offer.token is None


# ─── X402Server.validate_settlement_token (server-side) ──────────────────────

class TestX402ServerSettlementTokenValidation:
    USDC_BASE = AcceptedToken(chain_id=8453, token="0xUSDC", min_amount=0, rank=2)
    DAI_BASE  = AcceptedToken(chain_id=8453, token="0xDAI",  min_amount=0, rank=1)

    def _make_server(self, accepts):
        return X402Server(
            pay_to_address="0xPAYEE",
            amount_usdc="1.00",
            accepts=accepts,
        )

    def test_accepted_token_passes(self):
        server = self._make_server([self.USDC_BASE, self.DAI_BASE])
        ok, msg = server.validate_settlement_token(chain_id=8453, token="0xDAI")
        assert ok is True
        assert msg == ""

    def test_non_accepted_token_fails(self):
        server = self._make_server([self.USDC_BASE])
        ok, msg = server.validate_settlement_token(chain_id=8453, token="0xLUX")
        assert ok is False
        assert "not accepted" in msg

    def test_no_accepts_configured_always_passes(self):
        server = self._make_server([])
        ok, msg = server.validate_settlement_token(chain_id=8453, token="0xANY")
        assert ok is True
