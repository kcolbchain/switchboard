# Switchboard Lab PWA + the "Switchboard Plugin" Proposal

**Status:** Draft v1
**App shell:** [`web/manifest.json`](../web/manifest.json) · [`web/sw.js`](../web/sw.js)
**Registration:** [`web/lab/shared.js`](../web/lab/shared.js) (lab pages) · [`web/index.html`](../web/index.html) (root)
**Verified by:** [`tests/test_pwa.py`](../tests/test_pwa.py)

---

## 1. Why a PWA

The Switchboard Lab is a static, no-build set of pages. Making it a **Progressive
Web App** gets us three things at zero infra cost:

1. **Installable** — agents-payments demos sit one tap away on a phone home screen
   or a desktop dock, in a standalone window with no browser chrome.
2. **Offline-capable** — the whole lab (every scene, the canvas, the docs) loads
   with no network. Good for conference Wi-Fi, planes, and demo booths.
3. **A distribution surface** — the same app shell is the reference for embedding
   Switchboard payments into a *third-party* PWA (see §4, the "switchboard plugin").

This is intentionally additive: the lab still works as plain static HTML if the
service worker never registers (e.g. served from `file://`).

## 2. What shipped

| File | Role |
|------|------|
| `web/manifest.json` | Web App Manifest — name, icons, `start_url`, `display: standalone`, shortcuts |
| `web/icon.svg`, `web/icon-maskable.svg` | Vector icons (`any` + `maskable` purposes), obsidian + gold brand mark |
| `web/sw.js` | Service worker — precache app shell, offline navigation fallback, runtime caching |
| `web/index.html` | Root registers the SW at scope `./` + links the manifest |
| `web/lab/shared.js` | Every lab page registers the SW (`../sw.js`) and injects the manifest link |

### Manifest highlights

- `scope: "./"` and `start_url: "./lab/index.html"` — the installed app opens on
  the lab dashboard but controls the entire `web/` tree.
- `display: "standalone"` with `display_override` falling back to `minimal-ui`.
- `shortcuts` — long-press / right-click the installed icon jumps straight to
  **Agentic Pay + Swap**, the **Canvas Lab**, or the **x402 Paywall** scene.
- `theme_color` / `background_color` `#06060b` match the lab's obsidian splash so
  the launch transition is seamless.

## 3. Install flow

```
┌──────────────┐   browser detects     ┌──────────────────┐   user installs   ┌────────────────┐
│  open lab in  │   manifest + SW +     │  install prompt   │ ───────────────▶ │ standalone app  │
│  Chrome/Edge  │ ─ HTTPS + icons ────▶ │  (omnibox / menu) │                   │ on home / dock  │
└──────────────┘                        └──────────────────┘                   └────────────────┘
        │                                                                               │
        │ first load: SW `install` event precaches the app shell ──────────────────────┘
        │ subsequent loads: served from cache, revalidated in the background
        ▼
   works fully offline (every scene + docs)
```

**Manual install:**
- **Desktop Chrome/Edge:** open `web/lab/index.html` over HTTPS (or `localhost`),
  click the install icon in the address bar (or *⋮ → Install Switchboard*).
- **Android Chrome:** *⋮ → Add to Home screen / Install app*.
- **iOS Safari:** *Share → Add to Home Screen* (Safari reads the manifest name +
  `apple-touch-icon`; service-worker offline support applies in standalone mode).

**Local dev / verifying offline:**

```bash
cd web && python3 -m http.server 8731
# open http://localhost:8731/lab/index.html, let it load once,
# then DevTools → Network → Offline, and reload — the lab still renders.
```

> Service workers require a secure context: `https://` **or** `http://localhost`.
> Served from `file://`, the lab degrades gracefully to plain static pages
> (registration is guarded with `location.protocol !== 'file:'`).

## 4. Offline architecture

The service worker (`web/sw.js`) uses three strategies keyed off the request:

1. **App-shell precache** (`install`): the lab pages, `shared.css`/`shared.js`, the
   icons, and the root pages are fetched with `cache: "reload"` and stored under a
   versioned cache (`switchboard-lab-v1`). Adds are resilient — a single 404 does
   not abort the whole install.
2. **Navigations → network-first** with a cache fallback, then the offline shell
   (`./lab/index.html`). So a brand-new route works online and a previously-visited
   route works offline.
3. **Same-origin assets → stale-while-revalidate**; **cross-origin (Google Fonts)
   → cache-first** so type renders offline.

`activate` deletes stale caches; bump `CACHE_VERSION` on deploy to invalidate. A
page can post `SKIP_WAITING` to adopt a new worker immediately.

## 5. The "switchboard plugin" PWA proposal

The lab PWA doubles as the **reference embedding** for shipping Switchboard
payments inside *someone else's* PWA — a wallet, a marketplace, an agent console.
The idea: a drop-in **"switchboard plugin"** an app installs once and then calls to
gate features behind agent payments.

### 5.1 Shape

```
host PWA (installed)
   │
   ├─ <script type="module" src="switchboard-plugin.js"></script>
   │      registers a SECOND service worker scoped to /pay/*
   │      (or a module imported by the host SW) that:
   │        • intercepts fetches that come back 402
   │        • parses the x402 PaymentRequirements (switchboard/x402)
   │        • drives the on-chain pay/escrow flow
   │        • retries with the X-Payment proof header
   │
   └─ UI: an install-time permission ("allow agentic payments up to N USDC/day")
          backed by the gas-budget primitive (switchboard.gas_tracker)
```

The plugin reuses the exact wire types the Python library defines so host and
agent speak the same protocol:

- **402 challenge / proof** — `switchboard/x402/server.py`
  (`PaymentRequirements`, `X-Payment` / `X-Payment-Proof`, `WWW-Authenticate: x402`).
- **Escrow settlement** — `src/payment_protocol.py` + `contracts/AgentEscrow.sol`.
- **Spend caps** — `switchboard.gas_tracker.GasTracker` enforces the per-hour /
  per-day budget the user grants at install.
- **Agentic swap** — after receiving funds, route through SafeSwap exactly as in
  [`examples/agentic_demo`](../examples/agentic_demo/) (`SafeSwapClient`).

### 5.2 Install + consent flow

```
1. user installs the host PWA (manifest + SW)
2. host PWA imports the switchboard plugin
3. plugin shows a one-time consent sheet:
      "Switchboard may pay agents on your behalf, up to 20 USDC/day,
       only to recipients you approve. Funds settle on-chain via escrow."
4. consent persists the budget + allowlist (IndexedDB)
5. from then on, any fetch the host makes that returns 402 is auto-paid
   within budget — fully offline-first for the UI, on-chain for settlement
```

This mirrors the policy gate already implemented server-agnostically in
`X402Middleware._validate_offer()` (cap, recipient allowlist, gas budget) — the
plugin is that check, moved into the browser.

### 5.3 How the plugin embeds switchboard payments

A host page gates a paid feature with a single call:

```js
import { switchboardPay } from './switchboard-plugin.js';

// fetch a paid agent endpoint; the plugin handles the 402 → pay → retry loop
const res = await switchboardPay('https://agent-b.example/v1/inference', {
  method: 'POST',
  body: JSON.stringify(job),
  // policy comes from install-time consent; can be tightened per call
  maxUsd: 5,
  allow: ['0xB0b0…'],
});
```

Under the hood that is the browser twin of the Python demo: parse the offer,
validate against the budget, settle into escrow, retry with proof — and
optionally route the proceeds through SafeSwap. The lab's
[Agentic Pay + Swap scene](../web/lab/swap.html) is the visual spec for exactly
this loop.

### 5.4 Roadmap

| Step | Deliverable |
|------|-------------|
| 1 | `switchboard-plugin.js` — `switchboardPay()` + 402 interception (ships the §5.3 API) |
| 2 | Consent sheet + IndexedDB-backed budget/allowlist (the in-browser `GasTracker`) |
| 3 | Wallet binding (EIP-1193 / EIP-7702 smart-account) for real on-chain settlement |
| 4 | SafeSwap routing of received funds, surfaced as an optional auto-rebalance |
| 5 | Publish alongside [`@kcolbchain/eliza-switchboard`](../packages/plugin-switchboard) as a browser counterpart |

## 6. Verification

```bash
PYTHONPATH=. python -m pytest tests/test_pwa.py -q
```

Asserts the manifest is valid + complete, its icons and `start_url`/shortcut
targets exist, the service worker is valid JS that precaches a real on-disk app
shell with an offline fallback, and that registration is wired into both the root
page and every lab page.
