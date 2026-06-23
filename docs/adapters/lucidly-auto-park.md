# Lucidly syUSD auto-park adapter

`switchboard.adapters.lucidly` auto-parks idle agent USDC balances into
Lucidly's syUSD vault to earn yield between agent actions.

The adapter runs a rebalance hook after every agent settlement, moving
liquid buffer excess into the vault. It unparks on-demand when the agent
needs liquidity for the next transaction.

## Configuration

```python
from switchboard.adapters.lucidly import LucidlyConfig, LucidlyAutoPark

config = LucidlyConfig(
    idle_target_bps=8000,          # 80% of idle balance to keep liquid
    max_parked_usd=100_000.0,      # cap total parked across chains
    per_chain_targets={
        "ethereum": 5000,          # 50% liquid on eth
        "base": 8000,              # 80% liquid on base
        "arbitrum": 8000,          # 80% liquid on arb
    },
    enabled=True,
)

park = LucidlyAutoPark(config=config)
```

## Rebalance after settlement

```python
# After an agent settles a payment, move excess liquid into vault
result = park.rebalance(chain="base", liquid_balance_usd=500.0)
# {"action": "parked", "chain": "base", "amount_usd": 100.0, ...}
```

## Unpark on demand

```python
# Before broadcasting a tx that needs more liquid than available
withdrawn = park.unpark(chain="base", amount_usd=150.0)
```

## Status

```python
status = park.status(chain="base")
# {
#   "chain": "base",
#   "liquid_buffer_usd": 400.0,
#   "vault_balance_usd": 100.0,
#   "total_parked_usd": 100.0,
#   "enabled": True,
#   "idle_target_pct": 80.0,
# }
```
