# .well-known/agent-payment.json

## Purpose
Standard endpoint for agent payment capability discovery. Hosted at `https://<agent-host>/.well-known/agent-payment.json`.

## Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "AgentPaymentManifest",
  "type": "object",
  "required": ["agent_id", "public_key", "protocols"],
  "properties": {
    "agent_id": { "type": "string", "description": "Unique agent identifier" },
    "public_key": { "type": "string", "description": "Agent's public key (hex)" },
    "key_algorithm": { "type": "string", "enum": ["sphincs+", "dilithium", "ed25519"], "default": "sphincs+" },
    "protocols": {
      "type": "array",
      "items": { "type": "string", "enum": ["x402", "native-eth-escrow", "switchboard-pq"] }
    },
    "payment_addresses": {
      "type": "object",
      "properties": {
        "eth": { "type": "string", "pattern": "^0x[a-fA-F0-9]{40}$" },
        "sol": { "type": "string" },
        "tron": { "type": "string" }
      }
    },
    "endpoints": {
      "type": "object",
      "properties": {
        "payment": { "type": "string", "format": "uri" },
        "webhook": { "type": "string", "format": "uri" }
      }
    }
  }
}
```

## Example

```json
{
  "agent_id": "agent-1",
  "public_key": "a1b2c3d4e5f6...",
  "key_algorithm": "sphincs+",
  "protocols": ["x402", "switchboard-pq"],
  "payment_addresses": { "eth": "0x1234..." },
  "endpoints": { "payment": "https://agent.example.com/x402" }
}
```

## Discovery
Agent fetches this before sending a payment to verify the recipient's key and supported protocols.
