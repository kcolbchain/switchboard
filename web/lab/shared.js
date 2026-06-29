/* ============================================================
   SWITCHBOARD LAB — SHARED BEHAVIORS
   Scroll reveals, count-up, sidebar search, mobile toggle
   ============================================================ */

(function labShared() {

  /* ── Scroll reveal ─────────────────────────────────────────── */
  const revealEls = document.querySelectorAll('.reveal');
  const revealObs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) { e.target.classList.add('visible'); revealObs.unobserve(e.target); }
    });
  }, { threshold: 0.06, rootMargin: '0px 0px -30px 0px' });
  revealEls.forEach(el => revealObs.observe(el));

  /* ── Count-up animation ────────────────────────────────────── */
  const counters = document.querySelectorAll('[data-count]');
  const countObs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (!e.isIntersecting) return;
      const el = e.target;
      const target = parseInt(el.dataset.count);
      const suffix = el.dataset.suffix || '';
      const dur = 1400;
      const start = performance.now();
      function tick(now) {
        const p = Math.min((now - start) / dur, 1);
        const ease = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(target * ease) + suffix;
        if (p < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
      countObs.unobserve(el);
    });
  }, { threshold: 0.5 });
  counters.forEach(c => countObs.observe(c));

  /* ── Sidebar search ────────────────────────────────────────── */
  const searchInput = document.getElementById('navSearch');
  const navItems = document.querySelectorAll('.nav-item');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      const q = searchInput.value.toLowerCase().trim();
      navItems.forEach(item => {
        const text = item.textContent.toLowerCase();
        item.style.display = (!q || text.includes(q)) ? '' : 'none';
      });
    });
  }

  /* ── Highlight current page in sidebar ─────────────────────── */
  const currentPath = location.pathname.split('/').pop() || 'index.html';
  navItems.forEach(item => {
    const href = (item.getAttribute('href') || '').split('/').pop();
    if (href === currentPath) {
      item.classList.add('active');
    }
  });

  /* ── Mobile sidebar toggle ─────────────────────────────────── */
  const sidebar = document.getElementById('sidebar');
  const toggle = document.getElementById('sidebarToggle');
  if (toggle && sidebar) {
    toggle.addEventListener('click', () => sidebar.classList.toggle('open'));
    // Close on nav click (mobile)
    navItems.forEach(item => {
      item.addEventListener('click', () => {
        if (window.innerWidth <= 900) sidebar.classList.remove('open');
      });
    });
  }

  /* ── Timeline auto-cycle (if present) ──────────────────────── */
  document.querySelectorAll('[data-timeline]').forEach(tl => {
    const steps = tl.querySelectorAll('.tl-step');
    if (steps.length === 0) return;
    let idx = 0;
    setInterval(() => {
      steps.forEach(s => s.classList.remove('active'));
      steps[idx].classList.add('active');
      idx = (idx + 1) % steps.length;
    }, 2200);
  });

  /* ── PWA: manifest link + service worker registration ──────── */
  /* The lab is installable + offline-capable. Pages under web/lab/ reach the
     manifest + service worker at the web/ root (one level up). Both are injected
     here so every lab page participates without per-page edits. */
  (function pwa() {
    // Ensure a manifest <link> exists (some pages declare it statically).
    if (!document.querySelector('link[rel="manifest"]')) {
      const link = document.createElement('link');
      link.rel = 'manifest';
      link.href = '../manifest.json';
      document.head.appendChild(link);
    }
    if (!document.querySelector('meta[name="theme-color"]')) {
      const meta = document.createElement('meta');
      meta.name = 'theme-color';
      meta.content = '#06060b';
      document.head.appendChild(meta);
    }
    // Register the service worker with scope at the web/ root.
    if ('serviceWorker' in navigator && location.protocol !== 'file:') {
      window.addEventListener('load', () => {
        navigator.serviceWorker
          .register('../sw.js', { scope: '../' })
          .catch(() => { /* offline support is progressive; ignore failures */ });
      });
    }
  })();

})();
