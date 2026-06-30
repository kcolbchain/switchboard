/* ============================================================
   SWITCHBOARD — LIVE AGENTIC-ESCROW DEMO (controller)
   ------------------------------------------------------------
   Calls POST /api/demo/run (examples/agentic_demo/server.py), then animates
   the REAL timeline it returns: Agent A and Agent B as two parties, a
   mock-Ethereum AgentEscrow in the middle, and the seven canonical steps
   (402 offer → validate → escrow LOCK → deliver → confirm → RELEASE →
   SafeSwap quote/execute) with live balances + escrow state.

   All on-chain numbers come from the server's per-step snapshots via the pure
   reducer in view.js (DemoView.reduce) — this file never recomputes chain math.

   SIMULATED / MOCK CHAIN — not a live network. No real ETH/RPC/funds.
   Synthetic agents/keys only. The escrow/x402/SafeSwap orchestration shown is
   switchboard's; this page only makes it watchable.
   Demo by Pattermesh (Patty / P. Sundaram) on kcolbchain/switchboard.
   ============================================================ */
(function liveDemo() {
  'use strict';
  var V = (typeof DemoView !== 'undefined') ? DemoView : (typeof globalThis !== 'undefined' ? globalThis.DemoView : null);
  if (!V) { console.error('[demo] view.js (DemoView) not loaded'); return; }

  // ── DOM refs ────────────────────────────────────────────────────────────
  var $ = function (id) { return document.getElementById(id); };
  var canvas = $('stage');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');

  var els = {
    btnRun: $('btnRun'), btnStep: $('btnStep'), btnReplay: $('btnReplay'),
    selPrice: $('selPrice'), selSwap: $('selSwap'),
    status: $('statusPill'),
    balA: $('balA'), balAUnit: $('balAUnit'), deltaA: $('deltaA'),
    balB: $('balB'), balBUnit: $('balBUnit'), deltaB: $('deltaB'),
    escrowCard: $('escrowCard'), escrowState: $('escrowState'), escrowMeta: $('escrowMeta'),
    ledgerBody: $('ledgerBody'), clock: $('ledgerClock'),
    caption: $('stageCaption'),
    summary: $('summaryFacts'), rawJson: $('rawJson'),
    errToast: $('errToast')
  };

  // ── palette pulled from CSS vars (theme-aware, matches lab) ───────────────
  var cssVar = function (v, fb) {
    return (getComputedStyle(document.documentElement).getPropertyValue(v).trim() || fb);
  };
  function COL() {
    return {
      gold: cssVar('--gold', '#d4a853'), emerald: cssVar('--emerald', '#4ecb71'),
      blue: cssVar('--blue', '#5b9cf5'), violet: cssVar('--violet', '#a78bfa'),
      cyan: cssVar('--cyan', '#67d4e0'), fg: cssVar('--fg', '#eae6df'),
      faint: cssVar('--fg-faint', '#5c5854'), line: cssVar('--line', 'rgba(255,255,255,.08)'),
      elev: cssVar('--bg-elev', '#101018')
    };
  }
  var COLOR_KEY = { gold: 'gold', emerald: 'emerald', blue: 'blue', violet: 'violet' };

  // ── canvas node layout (mirrors web/lab/swap.html, keyed by server ids) ───
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0;
  var nodes = {
    A:        { label: 'Agent A',  sub: 'payer',        kind: 'agent',    x: 0.13, y: 0.40 },
    escrow:   { label: 'AgentEscrow', sub: 'mock chain', kind: 'contract', x: 0.42, y: 0.70 },
    B:        { label: 'Agent B',  sub: 'payee',        kind: 'agent',    x: 0.60, y: 0.28 },
    safeswap: { label: 'SafeSwap', sub: 'orchestrator', kind: 'service',  x: 0.88, y: 0.56 }
  };

  function resize() {
    var r = canvas.getBoundingClientRect();
    W = r.width || canvas.clientWidth; H = r.height || 460;
    canvas.width = W * DPR; canvas.height = H * DPR;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  // ── run + animation state ─────────────────────────────────────────────────
  var run = null;          // the run envelope from /api/demo/run
  var index = 0;           // number of COMPLETED events (cursor)
  var playing = false;     // auto-advance mode
  var raf = 0;
  var t0 = 0;
  var DUR = 1250;          // ms per animated packet
  var GAP = 520;           // ms beat for steps with no packet (e.g. validate)
  var animPacket = null;   // packet currently flying (from view of next event)
  var gapTimer = 0;

  // ── networking ────────────────────────────────────────────────────────────
  function api(path, body) {
    var opts = { headers: { 'Accept': 'application/json' } };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      return res.json().then(function (j) {
        if (!res.ok || (j && j.ok === false)) {
          throw new Error((j && j.error) || ('HTTP ' + res.status));
        }
        return j;
      });
    });
  }

  function runParams() {
    return {
      price_units: parseInt(els.selPrice.value, 10),
      swap_to: els.selSwap.value,
      seed: 42,
      deterministic: true
    };
  }

  function showError(msg) {
    if (!els.errToast) return;
    els.errToast.textContent = '⚠ ' + msg + '  (still a MOCK CHAIN — no real funds were touched)';
    els.errToast.classList.add('show');
    setStatus('error', 'err');
  }
  function clearError() { if (els.errToast) els.errToast.classList.remove('show'); }

  // ── controls ────────────────────────────────────────────────────────────
  function setStatus(text, cls) {
    els.status.textContent = text;
    els.status.className = 'status-pill' + (cls ? ' ' + cls : '');
  }
  function setBusy(busy) {
    els.btnRun.disabled = busy;
    els.selPrice.disabled = busy;
    els.selSwap.disabled = busy;
  }

  function fetchAndPlay() {
    clearError();
    stopTimers();
    setBusy(true);
    setStatus('running…', 'run');
    return api('/api/demo/run', runParams()).then(function (env) {
      run = env;
      renderRaw(env);
      resetCursor();
      setBusy(false);
      play();
    }).catch(function (e) {
      setBusy(false);
      showError(String(e.message || e));
    });
  }

  function resetCursor() {
    index = 0; animPacket = null;
    renderAll();
    layoutLedger();
    paintLedger();
  }

  function play() {
    if (!run) { fetchAndPlay(); return; }
    playing = true;
    setStatus('running…', 'run');
    stepBeat(performance.now(), /*auto*/ true);
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(frame);
  }

  function pause() {
    playing = false;
    stopTimers();
  }

  function stopTimers() { clearTimeout(gapTimer); gapTimer = 0; }

  // Advance one event (manual Step button or auto beat). Sets up the packet
  // for timeline[index] (the next event) to fly, then applies it on completion.
  function stepBeat(now, auto) {
    if (!run) return;
    var n = run.timeline.length;
    if (index >= n) { onDone(); return; }
    var view = V.reduce(run, index);   // view BEFORE applying the next event
    animPacket = view.packet;          // packet for the upcoming event
    t0 = now;
    captionFor(run.timeline[index]);
    if (!animPacket) {
      // no packet to fly (e.g. setup / validate): apply after a short beat
      if (auto && playing) {
        gapTimer = setTimeout(function () { applyCurrent(); if (playing) stepBeat(performance.now(), true); }, GAP);
      } else {
        applyCurrent();
      }
    }
  }

  // Commit the event at `index` (move cursor forward) and repaint side panels.
  function applyCurrent() {
    index = Math.min(index + 1, run.timeline.length);
    animPacket = null;
    renderAll();
    paintLedger();
    if (index >= run.timeline.length) onDone();
  }

  function onDone() {
    playing = false;
    stopTimers();
    var routed = run && run.summary && run.summary.swap_routed;
    setStatus(routed ? 'settled · swap routed' : 'settled', 'ok');
    renderSummary();
  }

  function manualStep() {
    if (!run) { fetchAndPlay(); return; }
    pause();
    if (index >= run.timeline.length) { resetCursor(); return; }
    // fly the packet, then apply; if no packet, apply immediately
    var view = V.reduce(run, index);
    animPacket = view.packet;
    t0 = performance.now();
    captionFor(run.timeline[index]);
    setStatus('step ' + (index + 1) + ' / ' + run.timeline.length, 'run');
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(frame);
    if (!animPacket) applyCurrent();
  }

  // ── render: balances / escrow / ledger / caption / summary ────────────────
  function renderAll() {
    var view = V.reduce(run, index);
    renderBalances(view);
    renderEscrow(view);
    els.clock.textContent = 'block #' + view.block;
  }

  function renderBalances(view) {
    var a = view.balances && view.balances.A;
    var b = view.balances && view.balances.B;
    var fa = V.formatAmount(a), fb = V.formatAmount(b);
    els.balA.textContent = fa.value; els.balAUnit.textContent = fa.token || 'USDC';
    els.balB.textContent = fb.value; els.balBUnit.textContent = fb.token || 'USDC';
    var da = V.balanceDelta('A', view), db = V.balanceDelta('B', view);
    els.deltaA.className = 'delta' + (da.dir ? ' ' + da.dir : ''); els.deltaA.textContent = da.text;
    els.deltaB.className = 'delta' + (db.dir ? ' ' + db.dir : ''); els.deltaB.textContent = db.text;
  }

  function renderEscrow(view) {
    var lbl = V.escrowStateLabel(view.escrow);
    els.escrowState.innerHTML = '<span class="st ' + esc(lbl.state) + '">' + esc(lbl.text) + '</span>';
    var lit = view.escrow && (view.escrow.state === 'Locked');
    els.escrowCard.classList.toggle('lit', !!lit);
    if (view.escrow) {
      var amt = V.formatAmount(view.escrow.amount);
      var rid = view.escrow.request_id ? String(view.escrow.request_id).slice(0, 14) + '…' : '';
      els.escrowMeta.innerHTML =
        amt.value + ' ' + esc(amt.token) + ' · ' + esc(view.escrow.payer || 'A') + ' → ' + esc(view.escrow.payee || 'B') +
        (rid ? ' · <code>' + esc(rid) + '</code>' : '');
    } else {
      els.escrowMeta.innerHTML = 'AgentEscrow vault — lock → confirm → release';
    }
  }

  function layoutLedger() {
    els.ledgerBody.innerHTML = '';
    if (!run) return;
    run.timeline.forEach(function (ev, k) {
      var row = document.createElement('div');
      row.className = 'ledger-row' + (V.isSwapStep(ev.step) ? ' swap' : '');
      row.id = 'lr-' + k;
      var amtHtml = ev.amount ? ' <span class="lr-amt">' + esc(V.formatAmount(ev.amount).value + ' ' + (ev.amount.token || '')) + '</span>' : '';
      row.innerHTML =
        '<span class="lr-dot"></span>' +
        '<span class="lr-step">' + esc(ev.step) + '</span>' +
        '<span class="lr-detail"><strong>' + esc(ev.title || ev.step) + '</strong> ' + detailHtml(ev.detail) + amtHtml + '</span>';
      // click a row to jump the cursor there (replay control)
      row.addEventListener('click', function () { jumpTo(k + 1); });
      els.ledgerBody.appendChild(row);
    });
  }

  function paintLedger() {
    if (!run) return;
    var view = V.reduce(run, index);
    view.rows.forEach(function (r, k) {
      var row = $('lr-' + k);
      if (!row) return;
      row.classList.toggle('done', r.done);
      row.classList.toggle('active', r.active);
    });
  }

  function captionFor(ev) {
    if (!ev) return;
    var swap = V.isSwapStep(ev.step);
    els.caption.classList.toggle('swap', swap);
    var amt = ev.amount ? ' — <b>' + esc(V.formatAmount(ev.amount).value + ' ' + (ev.amount.token || '')) + '</b>' : '';
    els.caption.innerHTML =
      '<span class="cap-step">' + esc((ev.seq != null ? ev.seq + ' · ' : '') + ev.step) + '</span> ' +
      detailHtml(ev.detail || ev.title || '') + amt;
  }

  function jumpTo(targetCompleted) {
    pause();
    if (!run) return;
    index = Math.max(0, Math.min(targetCompleted, run.timeline.length));
    animPacket = null;
    renderAll(); paintLedger();
    if (index > 0) captionFor(run.timeline[index - 1]);
    if (index >= run.timeline.length) onDone();
    else setStatus('step ' + index + ' / ' + run.timeline.length, 'run');
  }

  // ── summary (genuine library output) ──────────────────────────────────────
  function renderSummary() {
    if (!run || !run.summary || !els.summary) return;
    var s = run.summary;
    var swap = s.swap || {};
    var spend = s.spend_summary || {};
    var facts = [
      { k: 'settled', v: s.settled ? 'true' : 'false', ok: !!s.settled,
        note: 'escrow ' + (s.escrow_state || '—') },
      { k: 'swap routed', v: s.swap_routed ? 'true' : 'false', ok: !!s.swap_routed,
        note: (swap.route || []).join(' → ') || '—' },
      { k: 'amount out', v: fmtBig(swap.amountOut) + ' <small>' + esc(swap.tokenOut || '') + '</small>', ok: false,
        note: 'in ' + fmtBig(swap.amountIn) + ' ' + esc(swap.tokenIn || '') },
      { k: 'total spent', v: spendDisplay(spend) + ' <small>USDC</small>', ok: false,
        note: 'X402Middleware.get_spend_summary()' }
    ];
    els.summary.innerHTML = facts.map(function (f) {
      return '<div class="fact"><div class="k">' + esc(f.k) + '</div>' +
        '<div class="v' + (f.ok ? ' ok' : '') + '">' + f.v + '</div>' +
        '<div class="note">' + esc(f.note) + '</div></div>';
    }).join('');
  }

  function spendDisplay(spend) {
    var wei = spend.total_spent_wei;
    if (wei == null) return '0.00';
    // USDC base units (6dp) per DEMO.md §6: total_spent_wei == price_units
    return V.scaleUnits(BigInt(wei), 6, 2);
  }
  function fmtBig(x) {
    if (x == null) return '0';
    var s = String(x);
    return s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function renderRaw(env) {
    if (!els.rawJson) return;
    try { els.rawJson.innerHTML = highlightJson(JSON.stringify(env.summary || env, null, 2)); }
    catch (e) { els.rawJson.textContent = JSON.stringify(env.summary || env, null, 2); }
  }

  // ── canvas drawing (adapted from web/lab/swap.html) ───────────────────────
  function px(n) { return { x: n.x * W, y: n.y * H }; }

  function drawRail(aKey, bKey) {
    var c = COL();
    if (!nodes[aKey] || !nodes[bKey]) return;
    var a = px(nodes[aKey]), b = px(nodes[bKey]);
    ctx.save();
    ctx.strokeStyle = c.line; ctx.lineWidth = 1; ctx.setLineDash([4, 6]);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.setLineDash([]); ctx.restore();
  }

  function nodeLit(key, view) {
    if (animPacket && (animPacket.from === key || animPacket.to === key)) return true;
    if (key === 'escrow' && view.escrow && view.escrow.state === 'Locked') return true;
    if (key === 'safeswap' && V.didComplete(view, 'swap.quote')) return true;
    return false;
  }

  function drawNode(key, view) {
    var c = COL();
    var n = nodes[key]; if (!n) return;
    var p = px(n); var r = 30;
    var accent = n.kind === 'contract' ? c.emerald : n.kind === 'service' ? c.violet : c.gold;
    var lit = nodeLit(key, view);
    ctx.save();
    if (lit) { ctx.shadowColor = accent; ctx.shadowBlur = 24; }
    ctx.lineWidth = lit ? 2 : 1.25;
    ctx.strokeStyle = accent; ctx.fillStyle = c.elev;
    if (n.kind === 'contract') {
      ctx.beginPath();
      for (var i = 0; i < 6; i++) {
        var a = Math.PI / 6 + i * Math.PI / 3;
        var x = p.x + r * Math.cos(a), y = p.y + r * Math.sin(a);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.closePath();
    } else if (n.kind === 'service') {
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.closePath();
    } else {
      var s = r * 1.7, sx = p.x - s / 2, sy = p.y - s / 2, rad = 10;
      ctx.beginPath();
      ctx.moveTo(sx + rad, sy);
      ctx.arcTo(sx + s, sy, sx + s, sy + s, rad);
      ctx.arcTo(sx + s, sy + s, sx, sy + s, rad);
      ctx.arcTo(sx, sy + s, sx, sy, rad);
      ctx.arcTo(sx, sy, sx + s, sy, rad);
      ctx.closePath();
    }
    ctx.fill(); ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.fillStyle = c.fg;
    ctx.font = "600 13px 'DM Sans', system-ui, sans-serif";
    ctx.textAlign = 'center';
    ctx.fillText(n.label, p.x, p.y + r + 18);
    ctx.fillStyle = c.faint;
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.fillText(n.sub, p.x, p.y + r + 32);
    ctx.restore();
  }

  function drawPacket(prog) {
    if (!animPacket) return;
    var c = COL();
    var col = c[COLOR_KEY[animPacket.color]] || c.gold;
    var a = px(nodes[animPacket.from]), b = px(nodes[animPacket.to]);
    if (!a || !b) return;
    var e = prog < 0.5 ? 2 * prog * prog : 1 - Math.pow(-2 * prog + 2, 2) / 2; // easeInOut
    var x = a.x + (b.x - a.x) * e, y = a.y + (b.y - a.y) * e;
    ctx.save();
    ctx.shadowColor = col; ctx.shadowBlur = 18; ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
    var tx = a.x + (b.x - a.x) * Math.max(0, e - 0.08), ty = a.y + (b.y - a.y) * Math.max(0, e - 0.08);
    ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.globalAlpha = 0.45;
    ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(x, y); ctx.stroke();
    ctx.restore();
  }

  function frame(now) {
    ctx.clearRect(0, 0, W, H);
    var view = run ? V.reduce(run, index) : { escrow: null, rows: [], index: 0 };
    drawRail('A', 'escrow'); drawRail('escrow', 'B'); drawRail('A', 'B'); drawRail('B', 'safeswap');
    Object.keys(nodes).forEach(function (k) { drawNode(k, view); });
    if (animPacket) {
      var prog = Math.min((now - t0) / DUR, 1);
      drawPacket(prog);
      if (prog >= 1) {
        applyCurrent();
        if (playing) stepBeat(now, true);
      }
    }
    // keep painting while animating or auto-playing; otherwise idle one more frame
    if (animPacket || playing) raf = requestAnimationFrame(frame);
    else raf = requestAnimationFrame(idleFrame);
  }

  // idle: redraw static graph occasionally so theme/resize stays crisp
  function idleFrame() {
    ctx.clearRect(0, 0, W, H);
    var view = run ? V.reduce(run, index) : { escrow: null, rows: [], index: 0 };
    drawRail('A', 'escrow'); drawRail('escrow', 'B'); drawRail('A', 'B'); drawRail('B', 'safeswap');
    Object.keys(nodes).forEach(function (k) { drawNode(k, view); });
    // stop the loop when fully idle (no animation pending) — repaint on demand
  }

  // ── small HTML helpers ─────────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  // Render a one-line detail: escape, then re-allow `code` spans for tokens in backticks.
  function detailHtml(detail) {
    var safe = esc(detail);
    return safe.replace(/`([^`]+)`/g, function (_, g) { return '<code>' + g + '</code>'; });
  }
  function highlightJson(s) {
    return esc(s)
      .replace(/&quot;([^&]+?)&quot;(\s*:)/g, '<span class="jk">&quot;$1&quot;</span>$2')
      .replace(/:\s*&quot;([^&]*?)&quot;/g, ': <span class="js">&quot;$1&quot;</span>')
      .replace(/:\s*(-?\d+(?:\.\d+)?|true|false|null)/g, ': <span class="jn">$1</span>');
  }

  // ── wire up ────────────────────────────────────────────────────────────────
  els.btnRun.addEventListener('click', function () { run = null; fetchAndPlay(); });
  els.btnStep.addEventListener('click', function () { manualStep(); });
  els.btnReplay.addEventListener('click', function () {
    if (!run) { fetchAndPlay(); return; }
    resetCursor(); play();
  });
  window.addEventListener('resize', function () { resize(); });

  // ── boot: pull last/seeded run from /api/demo/state so the page is alive
  //         immediately (no click needed), then auto-play once. ─────────────────
  resize();
  setStatus('loading…', 'run');
  api('/api/demo/state')
    .then(function (env) {
      run = env;
      renderRaw(env);
      layoutLedger();
      resetCursor();
      setStatus('ready · press Run', '');
      // gentle autostart so a partner audience sees motion on load
      setTimeout(function () { if (!playing && index === 0) play(); }, 900);
    })
    .catch(function (e) {
      // state endpoint optional/unavailable — fall back to a live Run on click
      layoutLedger();
      resetCursor();
      setStatus('ready · press Run', '');
      raf = requestAnimationFrame(idleFrame);
    });
})();
