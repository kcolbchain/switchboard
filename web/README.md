# switchboard web dashboard

Zero-build interactive map of the 2026 agent-payment rails — x402, MPP,
AP2, Circle Nanopayments — and how switchboard's on-chain escrow fits
alongside them.

## What it shows

- **Protocol flow** — a sequence diagram per protocol with 4 numbered
  steps. Click a step to see the wire-level snippet (HTTP, JSON body,
  Solidity call).
- **Compatibility matrix** — side-by-side: transport, settlement asset,
  agent↔agent vs agent↔server, streaming / sessions, disputes, fiat
  rails, license.
- **How switchboard fits** — the gap these rails leave (agent-side keys,
  nonces, budgets, escrow) and what this repo provides.

## Run locally

```bash
python3 -m http.server -d web 8080
```

## Hosted

- kcolbchain.com/switchboard/

## Source references

Protocol summaries reflect April 2026 state:

- x402 joined the Linux Foundation 2026-04-02 (Coinbase, Google, AWS,
  Microsoft, Stripe, Visa, Mastercard as founding members).
- MPP (Stripe × Paradigm × Tempo) went live on Tempo L1 mainnet 2026-03-18.
- Circle Nanopayments testnet opened 2026-03-03.
- Google Cloud announced AP2 the same week.
- Mastercard's Verifiable Intent specification (open-sourced on GitHub)
  complements AP2.
