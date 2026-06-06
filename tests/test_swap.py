import pytest
from switchboard.swap import StableSwapRouterClient, SwapQuote, StableSwapError

def test_quote_swap():
    client = StableSwapRouterClient(
        router_address="0x111",
        cr8usd_address="0x222",
        musd_address="0x333",
        fee_bps=5
    )
    
    quote = client.quote_swap(token_in="0x222", amount=100000.0)
    assert quote.token_out == "0x333"
    assert quote.fee == 50.0
    assert quote.amount_out == 99950.0
    
def test_quote_swap_zero_amount():
    client = StableSwapRouterClient("0x111", "0x222", "0x333")
    with pytest.raises(StableSwapError):
        client.quote_swap(token_in="0x222", amount=0)

def test_build_swap_tx():
    client = StableSwapRouterClient(
        router_address="0x111",
        cr8usd_address="0x222",
        musd_address="0x333"
    )
    tx = client.build_swap_cr8usd_to_musd_tx(amount_wei=10**18, recipient="0x444")
    assert tx["to"] == "0x111"
    assert "0x82f254e0" in tx["data"]
    assert "0x444".replace("0x", "").zfill(64) in tx["data"]
    assert hex(10**18).replace("0x", "").zfill(64) in tx["data"]

