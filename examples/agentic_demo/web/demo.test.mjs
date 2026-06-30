/* ============================================================
   node --test  for the LIVE DEMO frontend pure helpers (view.js)
   + a drift guard that the served demo.html embeds the web/ sources.

   Scoped to the frontend builder's files only. Does not touch Python or
   the backend's tests/test_agentic_demo_live.py. Deterministic.

   Run:  node --test examples/agentic_demo/web/demo.test.mjs
   ============================================================ */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { buildHtml } from './build.mjs';

const require = createRequire(import.meta.url);
const V = require('./view.js'); // view.js CJS-exports DemoView under Node
const HERE = dirname(fileURLToPath(import.meta.url));

/* ── A fixture timeline shaped EXACTLY like POST /api/demo/run (DEMO.md §2),
      using the REAL numbers the unchanged run_scenario produces for
      price=5 USDC, swap=ETH (verified against examples/agentic_demo):
        A starts 100 USDC; locks 5 → A=95; release → B=5 USDC;
        SafeSwap receipt amountOut = int(5_000_000 * 0.0004 * 0.997) = 1994
        (the MockSafeSwapOrchestrator returns a raw integer, NOT 18dp wei),
        so B flips to 1994 ETH units. The server supplies a `display` string. */
function amt(token, units, decimals, display) {
  return { token, units, decimals, display };
}
const PRICE = 5000000;       // 5 USDC base units
const SWAP_OUT = 1994;       // real SafeSwap receipt amountOut for 5 USDC → ETH
function fixtureRun() {
  const A100 = amt('USDC', 100000000, 6, '100.00');
  const A95 = amt('USDC', 95000000, 6, '95.00');
  const B0 = amt('USDC', 0, 6, '0.00');
  const B5 = amt('USDC', 5000000, 6, '5.00');
  const BETH = amt('ETH', SWAP_OUT, 18, '1994');
  const escAmt = amt('USDC', PRICE, 6, '5.00');
  const rid = 'req-deterministic-0001';
  const E = (state) => ({ request_id: rid, state, amount: escAmt, payer: 'A', payee: 'B' });
  const ev = (seq, step, actor, peer, title, opts) => Object.assign({
    seq, step, phase: step.indexOf('swap') === 0 ? 'swap' : (step === 'setup' ? 'setup' : 'pay'),
    actor, peer, title, detail: title, amount: null, escrow: null,
    balances: { A: A100, B: B0 }, block: 0, tx: null, data: {}
  }, opts || {});
  return {
    ok: true,
    sandbox: 'SIMULATED / MOCK CHAIN — not a live network.',
    deterministic: true,
    params: { price_units: PRICE, swap_to: 'ETH', seed: 42 },
    agents: V.DEFAULT_AGENTS,
    timeline: [
      ev(0, 'setup', 'system', null, 'Agent A funded', { balances: { A: A100, B: B0 }, block: 0 }),
      ev(1, '402', 'B', 'A', '402 Payment Required', { balances: { A: A100, B: B0 }, block: 0, amount: escAmt }),
      ev(2, 'validate', 'A', null, 'offer passes policy', { balances: { A: A100, B: B0 }, block: 0 }),
      ev(3, 'pay', 'A', 'escrow', 'Locked in AgentEscrow',
        { balances: { A: A95, B: B0 }, escrow: E('Locked'), block: 2, amount: escAmt, tx: rid }),
      ev(4, 'deliver', 'B', 'A', '200 OK', { balances: { A: A95, B: B0 }, escrow: E('Locked'), block: 2 }),
      ev(5, 'settle', 'A', 'escrow', 'escrow Released',
        { balances: { A: A95, B: B5 }, escrow: E('Released'), block: 3, amount: escAmt }),
      ev(6, 'swap.quote', 'B', 'safeswap', 'SafeSwap route quoted', { balances: { A: A95, B: B5 }, escrow: E('Released'), block: 3 }),
      ev(7, 'swap.execute', 'safeswap', 'B', 'routed swap settled',
        { balances: { A: A95, B: BETH }, escrow: E('Released'), block: 3, amount: amt('ETH', SWAP_OUT, 18, '1994') })
    ],
    summary: {
      settled: true, swap_routed: true, escrow_request_id: 'req-deterministic-0001',
      escrow_state: 'Released',
      swap: { tokenIn: 'USDC', tokenOut: 'ETH', amountIn: String(PRICE), amountOut: SWAP_OUT, route: ['SafeSwap.Router', 'UniswapV3'] },
      spend_summary: { total_spent_wei: PRICE }
    }
  };
}

/* ── formatAmount / scaleUnits ─────────────────────────────────────────────── */

test('scaleUnits: exact BigInt scaling, no float drift', () => {
  assert.equal(V.scaleUnits(95000000n, 6, 2), '95.00');
  assert.equal(V.scaleUnits(5000000n, 6, 2), '5.00');
  assert.equal(V.scaleUnits(100000000n, 6, 2), '100.00');
  assert.equal(V.scaleUnits(0n, 6, 2), '0.00');
  // 0.001994 ETH in wei → 4dp display truncates to 0.0019
  assert.equal(V.scaleUnits(1994000000000000n, 18, 4), '0.0019');
  // exact 0.0004 ETH
  assert.equal(V.scaleUnits(400000000000000n, 18, 4), '0.0004');
  // accepts string/number too
  assert.equal(V.scaleUnits('5000000', 6, 2), '5.00');
});

test('formatAmount: prefers server display, else derives by token', () => {
  assert.deepEqual(V.formatAmount(amt('USDC', 95000000, 6, '95.00')), { value: '95.00', token: 'USDC' });
  // no display → derive: USDC=2dp, ETH=4dp
  assert.deepEqual(V.formatAmount(amt('USDC', 5000000, 6)), { value: '5.00', token: 'USDC' });
  assert.deepEqual(V.formatAmount(amt('ETH', '1994000000000000', 18)), { value: '0.0019', token: 'ETH' });
  assert.deepEqual(V.formatAmount(null), { value: '0', token: '' });
});

test('formatAmount: never shows a non-zero balance as "0.0000" (raw-unit guard)', () => {
  // SafeSwap returns amount_out=1994 as a raw integer; as 18dp ETH that derives
  // to 0.0000 — the guard falls back to the raw unit count so it is not "zero".
  const r = V.formatAmount(amt('ETH', 1994, 18 /* no display */));
  assert.equal(r.value, '1994');
  assert.equal(r.token, 'ETH');
  assert.equal(r.raw, true);
  // a genuinely-zero balance still shows 0.00 (no false positive)
  assert.deepEqual(V.formatAmount(amt('USDC', 0, 6)), { value: '0.00', token: 'USDC' });
  // when the server DOES provide a non-zero display, we trust it verbatim
  assert.deepEqual(V.formatAmount(amt('ETH', 1994, 18, '1994')), { value: '1994', token: 'ETH' });
  // but a server display that rounds a non-zero balance to all-zeros is overridden
  // (resilient to a backend that naively derives wei-display for the swap output)
  const g = V.formatAmount(amt('ETH', 1994, 18, '0.0000'));
  assert.equal(g.value, '1994'); assert.equal(g.raw, true);
  // a real zero with display "0.00" is left alone
  assert.deepEqual(V.formatAmount(amt('USDC', 0, 6, '0.00')), { value: '0.00', token: 'USDC' });
});

/* ── phase / swap classification ───────────────────────────────────────────── */

test('phaseOf + isSwapStep group steps the way the UI styles them', () => {
  assert.equal(V.phaseOf('setup'), 'setup');
  assert.equal(V.phaseOf('402'), 'pay');
  assert.equal(V.phaseOf('pay'), 'pay');
  assert.equal(V.phaseOf('settle'), 'pay');
  assert.equal(V.phaseOf('swap.quote'), 'swap');
  assert.equal(V.phaseOf('swap.execute'), 'swap');
  assert.ok(V.isSwapStep('swap.quote') && V.isSwapStep('swap.execute'));
  assert.ok(!V.isSwapStep('pay') && !V.isSwapStep('settle'));
});

/* ── packetFor: actor→peer routing maps to canvas nodes ─────────────────────── */

test('packetFor: routes packets between canvas nodes, skips no-motion steps', () => {
  const run = fixtureRun();
  const byStep = {};
  run.timeline.forEach((e) => { byStep[e.step] = V.packetFor(e); });
  assert.equal(byStep['setup'], null);
  assert.equal(byStep['validate'], null);
  assert.deepEqual(byStep['402'], { from: 'B', to: 'A', kind: '402', color: 'gold' });
  assert.deepEqual(byStep['pay'], { from: 'A', to: 'escrow', kind: 'lock', color: 'blue' });
  assert.deepEqual(byStep['settle'], { from: 'escrow', to: 'B', kind: 'release', color: 'emerald' });
  assert.deepEqual(byStep['swap.quote'], { from: 'B', to: 'safeswap', kind: 'quote', color: 'violet' });
  assert.deepEqual(byStep['swap.execute'], { from: 'safeswap', to: 'B', kind: 'exec', color: 'violet' });
});

test('packetFor: canonical step ignores actor/peer, uses fixed value-flow', () => {
  // even with no actor/peer, a canonical step routes by its fixed endpoints
  assert.deepEqual(V.packetFor({ seq: 3, step: 'pay', actor: null, peer: null }),
    { from: 'A', to: 'escrow', kind: 'lock', color: 'blue' });
  // settle: A initiates but funds fly escrow → B (value flow, not initiator)
  assert.deepEqual(V.packetFor({ seq: 5, step: 'settle', actor: 'A', peer: 'escrow' }),
    { from: 'escrow', to: 'B', kind: 'release', color: 'emerald' });
});

test('packetFor: unknown step falls back to actor/peer routing', () => {
  assert.deepEqual(V.packetFor({ seq: 9, step: 'custom', actor: 'A', peer: 'B' }),
    { from: 'A', to: 'B', kind: 'flow', color: 'gold' });
  // unknown step with an un-mappable endpoint → no packet
  assert.equal(V.packetFor({ seq: 9, step: 'custom', actor: 'system', peer: null }), null);
});

test('normNode maps server vocabulary, drops "system"/unknown', () => {
  assert.equal(V.normNode('A'), 'A');
  assert.equal(V.normNode('escrow'), 'escrow');
  assert.equal(V.normNode('safeswap'), 'safeswap');
  assert.equal(V.normNode('system'), null);
  assert.equal(V.normNode(null), null);
});

/* ── reduce: the timeline→view reducer (the load-bearing core) ──────────────── */

test('reduce(index=0): pre-roll — nothing applied, no escrow, block 0', () => {
  const run = fixtureRun();
  const v = V.reduce(run, 0);
  assert.equal(v.index, 0);
  assert.equal(v.total, 8);
  assert.equal(v.started, false);
  assert.equal(v.done, false);
  assert.equal(v.escrow, null);
  assert.equal(v.block, 0);
  // seed balances: A funded, B empty
  assert.equal(V.formatAmount(v.balances.A).value, '100.00');
  assert.equal(V.formatAmount(v.balances.B).value, '0.00');
  assert.equal(v.rows.length, 8);
  assert.ok(v.rows.every((r) => r.done === false));
});

test('reduce after pay (index=4): escrow Locked, A debited to 95, B still 0', () => {
  const run = fixtureRun();
  const v = V.reduce(run, 4); // setup,402,validate,pay applied
  assert.equal(v.escrow.state, 'Locked');
  assert.equal(V.formatAmount(v.balances.A).value, '95.00');
  assert.equal(V.formatAmount(v.balances.B).value, '0.00');
  assert.equal(v.block, 2);
  // the just-applied row (pay, k=3) is active
  assert.equal(v.rows[3].active, true);
  assert.ok(v.rows.slice(0, 4).every((r) => r.done));
});

test('reduce after settle (index=6): escrow Released, B credited 5 USDC', () => {
  const run = fixtureRun();
  const v = V.reduce(run, 6); // through settle + swap.quote? index=6 → 6 applied (…settle is k=5)
  assert.equal(v.escrow.state, 'Released');
  assert.equal(V.formatAmount(v.balances.A).value, '95.00');
  assert.equal(V.formatAmount(v.balances.B).value, '5.00');
});

test('reduce at end (index=total): B flips to the swap asset (ETH)', () => {
  const run = fixtureRun();
  const v = V.reduce(run, run.timeline.length);
  assert.equal(v.done, true);
  const fb = V.formatAmount(v.balances.B);
  assert.equal(fb.token, 'ETH');
  assert.equal(fb.value, '1994'); // real SafeSwap receipt amountOut, shown via server display
  assert.equal(v.escrow.state, 'Released');
});

test('reduce clamps out-of-range indices', () => {
  const run = fixtureRun();
  assert.equal(V.reduce(run, -5).index, 0);
  assert.equal(V.reduce(run, 999).index, run.timeline.length);
  assert.equal(V.reduce(run, NaN).index, 0);
});

test('reduce: packet is for the NEXT (in-flight) event, not the applied one', () => {
  const run = fixtureRun();
  // index=3 means setup,402,validate applied; next event is pay → packet A→escrow
  const v = V.reduce(run, 3);
  assert.deepEqual(v.packet, { from: 'A', to: 'escrow', kind: 'lock', color: 'blue' });
});

/* ── balanceDelta + escrow label ───────────────────────────────────────────── */

test('balanceDelta: A shows escrowed debit after pay; B shows release then swap', () => {
  const run = fixtureRun();
  assert.deepEqual(V.balanceDelta('A', V.reduce(run, 4)), { dir: 'down', text: '-5.00 escrowed' });
  const afterSettle = V.balanceDelta('B', V.reduce(run, 6));
  assert.equal(afterSettle.dir, 'up');
  assert.match(afterSettle.text, /released/);
  assert.equal(V.balanceDelta('B', V.reduce(run, run.timeline.length)).dir, 'swap');
});

test('escrowStateLabel maps escrow snapshot to a styled state', () => {
  assert.deepEqual(V.escrowStateLabel(null), { state: 'empty', text: 'no escrow yet' });
  assert.deepEqual(V.escrowStateLabel({ state: 'Locked' }), { state: 'Locked', text: 'Locked' });
  assert.deepEqual(V.escrowStateLabel({ state: 'Released' }), { state: 'Released', text: 'Released' });
});

test('didComplete tracks applied steps by cursor', () => {
  const run = fixtureRun();
  const v = V.reduce(run, 6);
  assert.ok(V.didComplete(v, 'settle'));     // k=5 < 6
  assert.ok(V.didComplete(v, 'pay'));
  assert.ok(!V.didComplete(v, 'swap.execute')); // k=7, not yet
});

/* ── reduce handles an empty/missing run gracefully (page boot safety) ──────── */

test('reduce tolerates empty/missing run without throwing', () => {
  const v = V.reduce({ timeline: [] }, 0);
  assert.equal(v.total, 0);
  assert.equal(v.done, true);
  assert.deepEqual(v.rows, []);
  const v2 = V.reduce(null, 0);
  assert.equal(v2.total, 0);
});

/* ── drift guard: the served demo.html embeds the canonical web/ sources ────── */

test('build: demo.html inlines view.js + demo.js + demo.css, no leftover external refs', () => {
  const out = buildHtml();
  const view = readFileSync(join(HERE, 'view.js'), 'utf8');
  const demo = readFileSync(join(HERE, 'demo.js'), 'utf8');
  const css = readFileSync(join(HERE, 'demo.css'), 'utf8');
  // canonical sources present verbatim
  assert.ok(out.includes(view), 'view.js not inlined');
  assert.ok(out.includes(demo), 'demo.js not inlined');
  assert.ok(out.includes(css), 'demo.css not inlined');
  // external references to OUR assets are gone (so server-served page is self-contained)
  assert.ok(!out.includes('href="demo.css"'), 'leftover demo.css link');
  assert.ok(!out.includes('src="view.js"'), 'leftover view.js script src');
  assert.ok(!out.includes('src="demo.js"'), 'leftover demo.js script src');
  // sandbox banner + key API hooks survive into the served page
  assert.ok(/Simulated · Mock Chain/.test(out));
  assert.ok(out.includes('/api/demo/run'));
  assert.ok(out.includes('id="stage"'));
});

test('build output is deterministic (same bytes each call)', () => {
  assert.equal(buildHtml(), buildHtml());
});
