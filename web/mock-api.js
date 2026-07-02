/**
 * switchboard — Agent Onboarding Mock API
 * =========================================
 * Defines the contract shape and provides in-browser stub implementations.
 * Wire this against a real backend by replacing each handler with a fetch()
 * to the documented endpoint. All responses are JSON.
 *
 * Contract version: v0.1  (matches unit ⑱ / spec §10)
 *
 * ─────────────────────────────────────────────────────────────────────────
 * ENDPOINT CONTRACT
 * ─────────────────────────────────────────────────────────────────────────
 *
 * POST /api/auth/session
 *   Body:   { email: string, password?: string }   // API-key auth: no password
 *   200:    { session_token: string, user_id: string, display_name: string }
 *   401:    { error: "invalid_credentials" }
 *
 * DELETE /api/auth/session
 *   Headers: Authorization: Bearer <session_token>
 *   204:    (no body)
 *
 * POST /api/keys
 *   Headers: Authorization: Bearer <session_token>
 *   Body:   { provider: string, key: string, label?: string }
 *     provider ∈ { "openai", "anthropic", "google", "cohere", "custom" }
 *     key      — raw provider API key; NEVER stored in plaintext; backend
 *                immediately encrypts and stores cipher only; never returned.
 *   200:    { key_id: string, provider: string, label: string,
 *              masked: string,         // e.g. "sk-…3a9c"
 *              created_at: string }    // ISO 8601
 *   422:    { error: "invalid_key", detail: string }
 *
 * GET /api/keys
 *   Headers: Authorization: Bearer <session_token>
 *   200:    [ { key_id, provider, label, masked, created_at } ]
 *
 * DELETE /api/keys/:key_id
 *   Headers: Authorization: Bearer <session_token>
 *   204:    (no body)
 *
 * GET /api/agent/mcp-endpoint
 *   Headers: Authorization: Bearer <session_token>
 *   200:    { endpoint: string,        // wss://switchboard.kcolbchain.io/mcp
 *              session_key: string,    // scoped, revocable session key
 *              expires_at: string,     // ISO 8601
 *              policy: {
 *                token_allowlist: string[],   // e.g. ["LUX","USDC","ZOO","ETH"]
 *                per_tx_cap_usd: number,
 *                daily_cap_usd: number,
 *                allowed_counterparties: string[] | null
 *              } }
 *
 * GET /api/wallet/balances
 *   Headers: Authorization: Bearer <session_token>
 *   200:    [ { chain_id: number, chain_name: string,
 *               token: string, token_address: string,
 *               balance: string,       // decimal string, full precision
 *               balance_usd: number } ]
 *
 * POST /api/escrow/create
 *   Headers: Authorization: Bearer <session_token>
 *   Body:   { payee: string, token: string, amount: string,
 *              chain_id: number, challenge_period_s: number }
 *   200:    { request_id: string, tx_hash: string, status: "pending_confirmation" }
 *   422:    { error: "no_common_settlement_token" | "policy_violation" | "insufficient_balance",
 *              detail: string }
 *
 * GET /api/policy/status
 *   Headers: Authorization: Bearer <session_token>
 *   200:    { session_key: string, expires_at: string,
 *              daily_cap_usd: number, daily_spent_usd: number,
 *              per_tx_cap_usd: number, token_allowlist: string[],
 *              allowed_counterparties: string[] | null,
 *              policy_denials_24h: number }
 *
 * GET /api/metrics
 *   Headers: Authorization: Bearer <session_token>
 *   200:    { escrow: { fill_rate: number,          // 0-1
 *                        avg_release_ms: number,
 *                        timeout_rate: number,
 *                        refund_rate: number,
 *                        challenge_rate: number,
 *                        open_count: number },
 *              wallet: { spend_by_token: { [token]: number },  // USD
 *                         spend_by_rail: { x402: number, escrow: number, mpp: number },
 *                         policy_denials_24h: number,
 *                         fleet_health: number } }   // 0-1
 * ─────────────────────────────────────────────────────────────────────────
 */

const MockAPI = (() => {
  const DELAY = () => new Promise(r => setTimeout(r, 320 + Math.random() * 180));

  let _session = null;
  let _keys = [];

  async function auth({ email }) {
    await DELAY();
    if (!email || !email.includes('@')) return { ok: false, error: 'invalid_credentials' };
    _session = {
      session_token: 'sb_sess_' + Math.random().toString(36).slice(2),
      user_id: 'usr_' + Math.random().toString(36).slice(2, 10),
      display_name: email.split('@')[0],
    };
    return { ok: true, data: _session };
  }

  async function logout() {
    await DELAY();
    _session = null;
    return { ok: true };
  }

  async function addKey({ provider, key, label }) {
    await DELAY();
    if (!key || key.length < 8) return { ok: false, error: 'invalid_key', detail: 'Key too short' };
    const entry = {
      key_id: 'key_' + Math.random().toString(36).slice(2, 10),
      provider,
      label: label || provider,
      masked: key.slice(0, 4) + '…' + key.slice(-4),
      created_at: new Date().toISOString(),
    };
    _keys.push(entry);
    return { ok: true, data: entry };
  }

  async function listKeys() {
    await DELAY();
    return { ok: true, data: [..._keys] };
  }

  async function deleteKey(key_id) {
    await DELAY();
    _keys = _keys.filter(k => k.key_id !== key_id);
    return { ok: true };
  }

  async function getMcpEndpoint() {
    await DELAY();
    return {
      ok: true, data: {
        endpoint: 'wss://switchboard.kcolbchain.io/mcp',
        session_key: 'sb_sk_' + Math.random().toString(36).slice(2, 18),
        expires_at: new Date(Date.now() + 86400000).toISOString(),
        policy: {
          token_allowlist: ['LUX', 'USDC', 'ZOO', 'ETH', 'DAI'],
          per_tx_cap_usd: 50,
          daily_cap_usd: 500,
          allowed_counterparties: null,
        },
      }
    };
  }

  async function getBalances() {
    await DELAY();
    return {
      ok: true, data: [
        { chain_id: 7777777, chain_name: 'LUX C-chain', token: 'LUX',  token_address: '0x0000…0000', balance: '14820.50', balance_usd: 2442.38 },
        { chain_id: 8453,    chain_name: 'Base',         token: 'USDC', token_address: '0x036C…f7e',  balance: '1024.00',  balance_usd: 1024.00 },
        { chain_id: 8453,    chain_name: 'Base',         token: 'ZOO',  token_address: '0xd34d…cafe', balance: '50000.00', balance_usd: 410.00  },
        { chain_id: 1,       chain_name: 'Ethereum',     token: 'ETH',  token_address: '0x0000…0000', balance: '0.31',     balance_usd: 992.00  },
      ]
    };
  }

  async function createEscrow({ payee, token, amount, chain_id }) {
    await DELAY();
    return {
      ok: true, data: {
        request_id: 'req_' + Math.random().toString(36).slice(2, 12),
        tx_hash: '0x' + [...Array(64)].map(() => Math.floor(Math.random()*16).toString(16)).join(''),
        status: 'pending_confirmation',
      }
    };
  }

  async function getPolicyStatus() {
    await DELAY();
    return {
      ok: true, data: {
        session_key: 'sb_sk_demo…',
        expires_at: new Date(Date.now() + 82800000).toISOString(),
        daily_cap_usd: 500,
        daily_spent_usd: 127.40,
        per_tx_cap_usd: 50,
        token_allowlist: ['LUX', 'USDC', 'ZOO', 'ETH', 'DAI'],
        allowed_counterparties: null,
        policy_denials_24h: 2,
      }
    };
  }

  async function getMetrics() {
    await DELAY();
    return {
      ok: true, data: {
        escrow: {
          fill_rate: 0.94,
          avg_release_ms: 1840,
          timeout_rate: 0.03,
          refund_rate: 0.03,
          challenge_rate: 0.01,
          open_count: 7,
        },
        wallet: {
          spend_by_token: { USDC: 88.20, LUX: 24.10, ETH: 15.10 },
          spend_by_rail: { x402: 62.40, escrow: 52.00, mpp: 13.00 },
          policy_denials_24h: 2,
          fleet_health: 1.0,
        }
      }
    };
  }

  return { auth, logout, addKey, listKeys, deleteKey, getMcpEndpoint, getBalances, createEscrow, getPolicyStatus, getMetrics };
})();
