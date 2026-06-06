# Adding a scene to the agent-payments canvas lab

The canvas lab (`web/agents-demo.html`) is a single static HTML file, no build
step. Each "scene" is a self-contained object you append to the
`SCENES` registry plus an entry in `SCENE_GROUPS`. Hot-swap any time.

You don't need permission to add one — open a PR.

## What a scene looks like

```js
SCENES.myScene = {
  // Required ----------------------------------------------------------
  id: "myScene",           // matches SCENES key, used in URL slugs / tests
  name: "My scene",        // shown on the pill button
  short: "16",             // two-digit number shown in the pill prefix
  desc: "Short HTML description shown in the left sidebar.",

  // enter() is called every time the scene activates. Reset agent
  // balances, mark agents visible, set initial caption + log.
  enter() {
    setVisible(["bob", "eli"]);
    A.bob.balance = 10; A.eli.balance = 0;
    this.layout();
    this.phase = 0; this.cool = 50;
    setCaption("step 1 / 3", "<strong>Patty</strong> is about to call Abhi.");
    log(`<span class="info">myScene</span> scene loaded`);
  },

  // layout() runs on enter() and whenever the window resizes. Positions
  // are stage-relative; W and H are the canvas width and height.
  layout() {
    A.bob.pos = { x: W * 0.25, y: H * 0.5 };
    A.eli.pos = { x: W * 0.75, y: H * 0.5 };
  },

  // tick(t) runs every frame the scene is active and not paused. Drive
  // your scene state machine here. `this.cool` is a built-in cooldown
  // pattern — set it to a tick count to gate the next phase.
  tick(t) {
    if (this.cool > 0) { this.cool--; return; }
    if (this.phase === 0) {
      emit("bob", "eli", { kind: "offer", label: "1.00 USDC",
        amount: 1.0, speed: 0.02,
        onArrive: () => {
          A.bob.balance -= 1; A.eli.balance += 1;
          jobs++; jobsEl.textContent = jobs;
        }});
      log(`Patty → Abhi: 1.00 USDC`);
      setCaption("step 2 / 3", "Payment in flight.");
      this.phase = 1; this.cool = 80;
    } else if (this.phase === 1) {
      setCaption("step 3 / 3", "Settled. Loop resets.");
      this.phase = 2; this.cool = 160;
    } else {
      A.bob.balance = 10; A.eli.balance = 0;
      this.phase = 0; this.cool = 60;
    }
  },

  // Optional ----------------------------------------------------------
  drawUnderlay(ctx, W, H) { /* behind agents */ },
  drawChannels(ctx)        { /* lines between agents */ },
  drawOverlay(ctx, W, H)   { /* labels, badges, progress bars */ },
  onAgentClick(agent)      { /* respond to a click on an agent */ },
};
```

Then add the id to a group in `SCENE_GROUPS`:

```js
{ label: "use cases", ids: ["taxi","cafe","delivery","split","ethEscrow","subscribe","myScene"] },
```

That's it. Reload `agents-demo.html` and your scene appears in the pill bar.

## Agent registry

All agents live in `const A = {...}` at the top of the script. Each
has a `kind` (`human` / `bot` / `contract`) that drives its shape
(squircle / octagon / hex) and a `wallet` (`agentic` / `hitl` /
`treasury` / `provider` / `oracle` / `contract`) that drives its
outer-ring color.

Other agent fields worth knowing about:

| field         | purpose                                                     |
| ------------- | ----------------------------------------------------------- |
| `hue`         | base color for the signature gradient and halo (0–360)      |
| `protocols`   | string array — shown in the tooltip as pills (x402, MPP, …) |
| `sigAlg`      | signature algorithm label shown in the tooltip              |
| `priority`    | `"high"` / `"medium"` / `"low"` — auction scoring           |
| `radius`      | drawn size (default 32)                                     |
| `balance`     | live USDC (or ETH in the eth-escrow scene) balance          |
| `dailyCap`    | spend cap; tooltip + gas_budget-style enforcement           |

Add new agents inline in `A`, or attach to `A` from inside your
scene's `enter()` if it's scene-local. Either works.

## Packets

Use `emit(fromId, toId, opts)` to send a packet between two agents.
Common kinds and what they render as:

| kind      | head color | typical use                                  |
| --------- | ---------- | -------------------------------------------- |
| `offer`   | white      | PaymentOffer / call                          |
| `proof`   | green      | PaymentProof / settled receipt               |
| `request` | blue       | RPC / HTTP-style request                     |
| `bid`     | yellow     | marketplace bid                              |
| `data`    | magenta    | signed data, deliverable, oracle response    |
| `refund`  | red        | refund or rejection                          |
| `stream`  | agent hue  | per-chunk micropayment                       |

`opts.onArrive` runs when the packet head hits the recipient — use it
to mutate balances, log, or emit a follow-up packet.

## Cast — when to use whom

The three protagonists you'll probably reach for:

- **Patty** (`A.bob`) — the human user. Buyer side. Has an agentic
  wallet with caps.
- **Abhi** (`A.eli`) — the human service operator. Owns the café and
  the inference service; also Patty's counterparty in the ETH escrow
  and subscription scenes.
- **Tridib** (`A.gus`) — the human merchant / restaurateur. Owns Spice
  Hub; appears as a payer in split-bill.

Bots/AI agents you can pull from: `alice`, `iris`, `juno`, `chen`
(oracle), `fina` (treasury), `hana` (embed service). For service
surfaces: `cafe`, `rest`, `merchant`, `dispatch`. For settlement:
`escrow`.

Shape conventions:
- **Human** (squircle): Patty, Abhi, Tridib, Sara, Deva, Sai, Kiran
- **Bot** (octagon): all AI / autonomous agents
- **Contract** (hex): on-chain entities

## Tests

`tests/test_web_lab.py` runs over the canvas lab on every PR. If you add a
scene:

1. Append your id to `EXPECTED_SCENES`.
2. Run `pytest tests/test_web_lab.py -v` locally.
3. The suite also runs `node --check` on the embedded script if
   `node` is on PATH. Keep it clean.

## Style guidance

- **Caption every phase.** The caption text is the story; the
  animation just illustrates it. If a viewer pauses, the caption
  should still make sense.
- **Don't over-animate.** The eye should always know which agent is
  acting. A pulse, a packet, a state change — one beat at a time.
- **Hue per agent, not per scene.** Don't recolor existing agents;
  pick a new `hue` if you introduce a new one.
- **Keep scenes ~80–250 lines.** Beyond that, factor helpers out into
  the shared block above `SCENES.x402`.

## Bigger ideas wanted

- **Subscription with auto-pause** when usage drops (mirror of #15).
- **IoT meter** — bot-to-bot recurring micropayments, no humans.
- **Insurance claim** with parameterized payout via oracle.
- **Royalty stream** to ML model owners on each inference.
- **Refund dispute** — the unhappy path past challenge_period.
- **KYC handshake** — verify-once, cite-many for repeat payments.
- **Cross-chain settlement** — bridge → escrow → release.

PRs welcome. If you're not sure where a scene belongs, file an issue
or DM @abhicris.
