# Competitive Survey: Native-ETH Agent Escrow

_Date: 2026-06-05_

## Scope
This survey asked a narrow question: among public agent-payment / agent-escrow projects, who actually ships a **native-ETH** escrow primitive — a payable `createPayment{value: ...}`-style entry point that settles in plain ETH on an EVM chain.

Method:
- checked the live repo/docs pages or official docs for each project
- looked for a payable escrow contract, request-id keyed flow, timeout/refund, and any public mainnet or testnet deployment notes
- if I could not find a public escrow contract, I labeled it **no public evidence** instead of guessing

## Table

| Project | Native ETH? | Agent-targeted? | Mainnet / live? | Repo / docs | Notes |
|---|---|---:|---|---|---|
| switchboard | yes | yes | testnet + public demo | https://github.com/kcolbchain/switchboard | `AgentEscrow.sol` uses a payable create path with request-id, timeout, challenge period, refund/cancel flow. |
| Coinbase x402 | no, USDC rail | yes | yes (Base) | https://github.com/coinbase/x402 | HTTP 402 payment rail; the live docs / issue trail point at USDC settlement rather than native ETH escrow. |
| Google A2A / AP2 x402 | no public escrow contract found | yes | docs/spec only | https://github.com/google-a2a/a2a-x402 | Public material describes an agent-payment envelope / protocol, not a native-ETH escrow contract. |
| Circle Nanopayments | no, USDC batched settlement | yes | yes | https://developers.circle.com/gateway/nanopayments | Gas-free USDC nanopayments with off-chain authorizations and batched onchain settlement. |
| MPP / Tempo | no, stablecoin micropayments | yes | yes | https://mpp.dev/use-cases/micropayments | One-time charges use on-chain stablecoin transfers on Tempo; not a native-ETH escrow primitive. |
| Kleros Escrow | yes, ETH escrow contract | no | yes | https://docs.kleros.io/products/escrow | ETH escrow exists, but the flow is human-dispute / arbitration-centric, not agent-payment specific. |
| Reality.eth | no | no | yes | https://realitio.github.io/docs/html/ | General-purpose on-chain oracle / dispute primitive, not an agent escrow rail. |
| UMA Optimistic Oracle / Polymarket wrappers | no | no | yes | https://docs.uma.xyz/resources/glossary | Oracle/dispute rail, with escrow-like settlement patterns in some app-specific wrappers, but not a native agent escrow product. |
| Polymarket UMA sports oracle wrapper | no | no | yes | https://github.com/Polymarket/uma-sports-oracle | App-specific oracle wrapper; useful sanity check, but not an agent escrow product. |

## Positioning summary

Switchboard appears to be the **only public project I found that combines all three** of the following in one minimal primitive:

1. native ETH escrow on an EVM chain,
2. an agent-targeted request-id / timeout / challenge / refund flow,
3. a public codebase and live demo path.

That makes the “native-ETH escrow” claim believable **within the agent-payment niche**, not in the broader escrow market. The broader escrow market absolutely has ETH escrow already, especially Kleros and older oracle/dispute systems. But those are not agent-payment rails.

So the honest positioning is:
- **first / rare in agent payments:** native-ETH escrow with an agent flow
- **not first in general escrow:** ETH escrow has existed for years
- **clearly differentiated vs USDC rails:** x402, Circle, AP2, and MPP all lean on token rails or off-chain authorization rather than plain ETH escrow

If we keep this page current, it becomes the grounding doc for the README positioning table and for future roadmap calls.
