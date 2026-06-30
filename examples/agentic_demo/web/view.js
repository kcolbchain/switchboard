/* ============================================================
   SWITCHBOARD LIVE DEMO — pure view helpers (no DOM, no network)
   ------------------------------------------------------------
   These are the load-bearing, deterministic functions that turn the
   timeline returned by POST /api/demo/run (see examples/agentic_demo/DEMO.md
   §2) into render-ready view state. They are intentionally side-effect
   free so they can be unit-tested with `node --test` (see demo.test.mjs)
   and reused unchanged by the browser controller in demo.js.

   SIMULATED / MOCK CHAIN — not a live network. No real ETH/RPC/funds.
   ============================================================ */
(function (root) {
  'use strict';

  /** Format an Amount object (DEMO.md §2 "Amount") for display.
   *  Prefers the server-provided `display`; otherwise derives it from
   *  integer base `units` / 10**decimals WITHOUT floating drift on the
   *  integer part. USDC shows 2dp, 18dp assets (ETH/LUX) show up to 4dp. */
  function formatAmount(amount) {
    if (!amount || typeof amount !== 'object') return { value: '0', token: '' };
    var token = amount.token || '';
    var units = typeof amount.units === 'bigint' ? amount.units : BigInt(amount.units || 0);
    var isAllZero = function (s) { return /^-?0(\.0+)?$/.test(String(s)); };

    if (typeof amount.display === 'string' && amount.display.length) {
      // Trust the server's display string — UNLESS it rounds a *non-zero* balance
      // to all-zeros (e.g. the SafeSwap mock's raw integer 1994 derived as 18dp
      // ETH → "0.0000"). A real balance must never read as zero on screen, so in
      // that one case we fall back to the raw base-unit count.
      if (units > 0n && isAllZero(amount.display)) {
        return { value: units.toString(), token: token, raw: true };
      }
      return { value: amount.display, token: token };
    }
    var decimals = Number(amount.decimals || 0);
    var dispDp = token === 'USDC' ? 2 : (decimals >= 18 ? 4 : Math.min(decimals, 4));
    var value = scaleUnits(units, decimals, dispDp);
    if (units > 0n && isAllZero(value)) {
      return { value: units.toString(), token: token, raw: true };
    }
    return { value: value, token: token };
  }

  /** Exact base-units → fixed-dp decimal string using BigInt (no float).
   *  e.g. scaleUnits(95000000n, 6, 2) === "95.00"; (4n*10n**14n,18,4) === "0.0004". */
  function scaleUnits(units, decimals, dp) {
    var u = typeof units === 'bigint' ? units : BigInt(units || 0);
    var neg = u < 0n;
    if (neg) u = -u;
    var d = Number(decimals || 0);
    var scale = 10n ** BigInt(d);
    var intPart = u / scale;
    var frac = u % scale;
    var fracStr = d === 0 ? '' : frac.toString().padStart(d, '0');
    // round/truncate fractional part to `dp` digits (truncate — deterministic, matches scenario)
    var shown = dp <= 0 ? '' : fracStr.slice(0, dp).padEnd(dp, '0');
    var out = intPart.toString() + (dp > 0 ? '.' + shown : '');
    return (neg ? '-' : '') + out;
  }

  /** Coarse phase for styling. swap.* groups under "swap". */
  function phaseOf(step) {
    if (!step) return 'system';
    if (step === 'setup') return 'setup';
    if (step.indexOf('swap') === 0) return 'swap';
    return 'pay';
  }

  /** Whether a step belongs to the SafeSwap (violet) act. */
  function isSwapStep(step) {
    return typeof step === 'string' && step.indexOf('swap') === 0;
  }

  /** Default node descriptors when the run envelope omits `agents`. */
  var DEFAULT_AGENTS = {
    A:        { id: 'A',        role: 'payer',        label: 'Agent A' },
    B:        { id: 'B',        role: 'payee',        label: 'Agent B' },
    escrow:   { id: 'escrow',   role: 'contract',     label: 'AgentEscrow' },
    safeswap: { id: 'safeswap', role: 'orchestrator', label: 'SafeSwap' }
  };

  /** The animated packet for a step: which node → which node, and a class.
   *  Mirrors the rails in web/lab/swap.html but keyed off the REAL actor/peer
   *  fields when present, falling back to the canonical step ids. */
  function packetFor(ev) {
    if (!ev) return null;
    var actor = ev.actor, peer = ev.peer;
    var kindByStep = {
      'setup': null,
      '402': { kind: '402', color: 'gold' },
      'validate': null,
      'pay': { kind: 'lock', color: 'blue' },
      'deliver': { kind: 'work', color: 'emerald' },
      'settle': { kind: 'release', color: 'emerald' },
      'swap.quote': { kind: 'quote', color: 'violet' },
      'swap.execute': { kind: 'exec', color: 'violet' }
    };
    // The packet depicts VALUE FLOW (what moves), which for the seven canonical
    // steps is fixed + load-bearing (matches web/lab/swap.html's rails) — note
    // this differs from `actor` (who *initiates*): e.g. at `settle` Agent A
    // calls confirmPayment(), but the funds fly escrow → B. So for a known step
    // we use its fixed endpoints; actor/peer is only a fallback for unknown steps.
    var canonical = ev.step !== undefined ? Object.prototype.hasOwnProperty.call(kindByStep, ev.step) : false;
    var spec = kindByStep[ev.step];
    if (canonical && (spec === null || spec === undefined)) return null; // setup/validate: no motion
    var implied = {
      '402': ['B', 'A'], 'pay': ['A', 'escrow'], 'deliver': ['B', 'A'],
      'settle': ['escrow', 'B'], 'swap.quote': ['B', 'safeswap'], 'swap.execute': ['safeswap', 'B']
    }[ev.step];
    var from, to;
    if (implied) {
      from = implied[0]; to = implied[1];      // canonical value-flow wins
    } else {
      from = normNode(actor); to = normNode(peer); // unknown step: trust actor/peer
    }
    if (!from || !to || from === to) return null;
    return { from: from, to: to, kind: (spec && spec.kind) || 'flow', color: (spec && spec.color) || 'gold' };
  }

  /** Map server actor vocabulary {A,B,escrow,safeswap,system} → canvas node keys. */
  function normNode(id) {
    if (!id) return null;
    if (id === 'escrow') return 'escrow';
    if (id === 'safeswap') return 'safeswap';
    if (id === 'A' || id === 'B') return id;
    return null; // 'system' / unknown have no canvas node
  }

  /** Reduce a timeline + cursor index → the full view model the UI paints.
   *  `index` is the number of COMPLETED events (0 = nothing applied yet, before
   *  even `setup`). The reducer reads balances/escrow/block from the LAST
   *  completed event (the server already snapshots post-step state per DEMO.md
   *  §2), so the view is always truthful to the real run — JS never recomputes
   *  on-chain math. Returns a plain object (deterministic, testable). */
  function reduce(run, index) {
    var timeline = (run && run.timeline) || [];
    var agents = (run && run.agents) || DEFAULT_AGENTS;
    var n = timeline.length;
    var i = clampIndex(index, n);              // completed count, 0..n
    var last = i > 0 ? timeline[i - 1] : null; // last applied frame
    var current = i < n ? timeline[i] : null;  // the in-flight / next frame

    // Balances + escrow + block come straight from the last applied event.
    var balances = last && last.balances ? last.balances : seedBalances(run);
    var escrow = last ? last.escrow : null;
    var block = last && typeof last.block === 'number' ? last.block : 0;

    // Per-row status for the ledger.
    var rows = timeline.map(function (ev, k) {
      return {
        seq: ev.seq,
        step: ev.step,
        title: ev.title || ev.step,
        detail: ev.detail || '',
        amount: ev.amount || null,
        swap: isSwapStep(ev.step),
        done: k < i,
        active: k === (i - 1) // the most-recently-applied row reads as "active"
      };
    });

    return {
      index: i,
      total: n,
      done: i >= n,
      started: i > 0,
      current: current,        // frame whose packet is animating next
      last: last,              // frame already applied (drives balances)
      balances: balances,
      escrow: escrow,
      block: block,
      rows: rows,
      packet: packetFor(current),
      agents: agents
    };
  }

  function clampIndex(index, n) {
    var i = Number.isFinite(index) ? Math.floor(index) : 0;
    if (i < 0) i = 0;
    if (i > n) i = n;
    return i;
  }

  /** Pre-roll balances before any event applies: A funded, B empty.
   *  Derived from params.price_units only for display fallback; the real
   *  numbers always come from the timeline once it starts. */
  function seedBalances(run) {
    var fundUnits = '100000000'; // 100 USDC, mirrors scenario.run_scenario fund()
    return {
      A: { token: 'USDC', units: fundUnits, decimals: 6, display: '100.00' },
      B: { token: 'USDC', units: '0', decimals: 6, display: '0.00' }
    };
  }

  /** Signed delta string for a balance vs. its seed, for the bal card.
   *  Returns { dir: 'up'|'down'|'swap'|'', text: '...' } based on the
   *  applied frame's amount/escrow rather than recomputing chain math. */
  function balanceDelta(node, view) {
    var last = view && view.last;
    if (!last) return { dir: '', text: '' };
    var step = last.step;
    if (node === 'A') {
      // A is debited at `pay` (escrow lock).
      if (view.index >= stepIndex(view, 'pay') + 1 && hasStep(view, 'pay')) {
        var locked = lockedAmount(view);
        if (locked) return { dir: 'down', text: '-' + formatAmount(locked).value + ' escrowed' };
      }
      return { dir: '', text: '' };
    }
    if (node === 'B') {
      if (didComplete(view, 'swap.execute')) {
        return { dir: 'swap', text: 'routed via SafeSwap →' };
      }
      if (didComplete(view, 'settle')) {
        var bbal = view.balances && view.balances.B;
        return { dir: 'up', text: '+' + (bbal ? formatAmount(bbal).value : '') + ' released' };
      }
      return { dir: '', text: '' };
    }
    return { dir: '', text: '' };
  }

  function stepIndex(view, step) {
    for (var k = 0; k < view.rows.length; k++) if (view.rows[k].step === step) return k;
    return -1;
  }
  function hasStep(view, step) { return stepIndex(view, step) >= 0; }
  /** True once the event with `step` has been APPLIED (is among completed). */
  function didComplete(view, step) {
    var k = stepIndex(view, step);
    return k >= 0 && k < view.index;
  }
  /** The amount locked into escrow (from the `pay` frame), if applied. */
  function lockedAmount(view) {
    var k = stepIndex(view, 'pay');
    if (k < 0 || k >= view.index) return null;
    var ev = view.rows[k];
    return ev && ev.amount ? ev.amount : null;
  }

  /** Human escrow-state label for the escrow card. */
  function escrowStateLabel(escrow) {
    if (!escrow || !escrow.state) return { state: 'empty', text: 'no escrow yet' };
    return { state: escrow.state, text: escrow.state };
  }

  var DemoView = {
    formatAmount: formatAmount,
    scaleUnits: scaleUnits,
    phaseOf: phaseOf,
    isSwapStep: isSwapStep,
    packetFor: packetFor,
    normNode: normNode,
    reduce: reduce,
    seedBalances: seedBalances,
    balanceDelta: balanceDelta,
    escrowStateLabel: escrowStateLabel,
    didComplete: didComplete,
    stepIndex: stepIndex,
    DEFAULT_AGENTS: DEFAULT_AGENTS
  };

  // Browser: attach to window/globalThis. Node (tests): module.exports.
  root.DemoView = DemoView;
  if (typeof module !== 'undefined' && module.exports) module.exports = DemoView;
})(typeof globalThis !== 'undefined' ? globalThis : this);
