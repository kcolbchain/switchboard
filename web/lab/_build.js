/**
 * Lab page generator — stamps out the shared head, sidebar, and footer
 * for each case study page. Run: node _build.js
 *
 * This is a dev convenience script, not a production dependency.
 * The output HTML files are self-contained and can be served statically.
 */

const fs = require('fs');
const path = require('path');

const HEAD = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<TITLE_SLOT>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=DM+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet" />
<link rel="stylesheet" href="shared.css" />
</head>
<body>
<button class="sidebar-toggle" id="sidebarToggle" aria-label="toggle sidebar">&#9776;</button>
<div class="app">`;

const SIDEBAR = `
<nav class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <div class="sidebar-logo"><em>kcolb</em>chain / switchboard</div>
    <div class="sidebar-sub">the lab</div>
  </div>
  <div class="sidebar-search">
    <span class="icon">&#8981;</span>
    <input type="text" id="navSearch" placeholder="Search case studies..." />
  </div>
  <div class="sidebar-nav" id="sidebarNav">
    <div class="nav-group">
      <div class="nav-group-label">Overview</div>
      <a class="nav-item" href="index.html"><span class="nav-label">Dashboard</span></a>
    </div>
    <div class="nav-group">
      <div class="nav-group-label">Protocol Patterns</div>
      <a class="nav-item" href="x402.html"><span class="nav-num">01</span><span class="nav-label">x402 Paywall</span><span class="nav-chip live">live</span></a>
      <a class="nav-item" href="escrow.html"><span class="nav-num">02</span><span class="nav-label">Escrow &amp; Refund</span><span class="nav-chip live">live</span></a>
      <a class="nav-item" href="streaming.html"><span class="nav-num">03</span><span class="nav-label">Streaming MPP</span><span class="nav-chip live">live</span></a>
      <a class="nav-item" href="auction.html"><span class="nav-num">04</span><span class="nav-label">Compute Auction</span><span class="nav-chip live">live</span></a>
      <a class="nav-item" href="swap.html"><span class="nav-num">05</span><span class="nav-label">Agentic Pay + Swap</span><span class="nav-chip new">new</span></a>
    </div>
    <div class="nav-group">
      <div class="nav-group-label">Real-World Scenes</div>
      <a class="nav-item" href="taxi.html"><span class="nav-num">09</span><span class="nav-label">Taxi Handover</span><span class="nav-chip new">new</span></a>
      <a class="nav-item" href="cafe.html"><span class="nav-num">10</span><span class="nav-label">Caf&eacute; Walk-by</span><span class="nav-chip new">new</span></a>
      <a class="nav-item" href="delivery.html"><span class="nav-num">11</span><span class="nav-label">Food Delivery</span></a>
      <a class="nav-item" href="trip.html"><span class="nav-num">15</span><span class="nav-label">Multi-City Trip</span></a>
    </div>
    <div class="nav-group">
      <div class="nav-group-label">Infrastructure</div>
      <a class="nav-item" href="multichain.html"><span class="nav-label">Multi-Chain Map</span></a>
      <a class="nav-item" href="pq.html"><span class="nav-label">Post-Quantum</span><span class="nav-chip pq">pq</span></a>
      <a class="nav-item" href="rails.html"><span class="nav-label">Rails Comparison</span></a>
    </div>
    <div class="nav-group">
      <div class="nav-group-label">Utilities</div>
      <a class="nav-item" href="tools.html"><span class="nav-label">Dev Tools</span></a>
    </div>
  </div>
  <div class="sidebar-footer">
    <a href="../index.html">explorer</a>
    <a href="../agents-demo.html">canvas</a>
    <a href="../simulator.html">sim</a>
    <a href="../docs.html">docs</a>
    <a href="https://github.com/kcolbchain/switchboard" target="_blank">src</a>
  </div>
</nav>`;

const FOOTER = `
<footer class="lab-footer">
  <div class="brand"><em>kcolb</em>chain / switchboard &middot; the lab</div>
  <div class="links">
    <a href="../index.html">explorer</a>
    <a href="../agents-demo.html">canvas lab</a>
    <a href="../simulator.html">simulator</a>
    <a href="../docs.html">docs</a>
    <a href="https://github.com/kcolbchain/switchboard" target="_blank">source</a>
    <a href="https://kcolbchain.com" target="_blank">kcolbchain</a>
  </div>
</footer>`;

const TAIL = `
</div><!-- .main -->
</div><!-- .app -->
<script src="shared.js"></script>
<SCRIPT_SLOT>
</body>
</html>`;

function buildPage({ file, title, content, script }) {
  let html = HEAD.replace('<TITLE_SLOT>', `<title>${title}</title>`);
  html += SIDEBAR;
  html += '\n<div class="main">\n';
  html += content;
  html += FOOTER;
  html += TAIL.replace('<SCRIPT_SLOT>', script ? `<script>\n${script}\n</script>` : '');

  fs.writeFileSync(path.join(__dirname, file), html, 'utf8');
  console.log(`  wrote ${file} (${html.length} bytes)`);
}

module.exports = { buildPage, HEAD, SIDEBAR, FOOTER, TAIL };

// If run directly, just report
if (require.main === module) {
  console.log('Lab build helpers loaded. Import buildPage() from page scripts, or run individual page generators.');
}
