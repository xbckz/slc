/* ===========================
   SLICE LEAGUE · MAIN JS
   =========================== */

document.addEventListener('DOMContentLoaded', () => {

  // ─── SECURITY: rel="noopener noreferrer" on all external links ──
  document.querySelectorAll('a[target="_blank"]').forEach(a => {
    const rel = a.getAttribute('rel') || '';
    if (!rel.includes('noopener')) {
      a.setAttribute('rel', 'noopener noreferrer');
    }
  });

  // ─── SCROLL PROGRESS BAR ────────────────────────────────────────
  const progressBar = document.createElement('div');
  progressBar.id = 'scrollProgress';
  document.body.prepend(progressBar);

  // ─── BACK TO TOP ────────────────────────────────────────────────
  const bttBtn = document.createElement('button');
  bttBtn.id = 'backToTop';
  bttBtn.setAttribute('aria-label', 'Back to top');
  bttBtn.innerHTML = '<i class="fa fa-chevron-up"></i>';
  document.body.appendChild(bttBtn);
  bttBtn.addEventListener('click', () => {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  // ─── NOTIFICATION BAR DISMISS ───────────────────────────────────
  const notifBar = document.querySelector('.notif-bar');
  const nav = document.querySelector('.nav');
  const hero = document.querySelector('.hero');

  function applyNotifHeight(h) {
    document.documentElement.style.setProperty('--notif-h', h + 'px');
  }

  if (notifBar) {
    // Check if already dismissed this session
    if (sessionStorage.getItem('notifDismissed') === '1') {
      notifBar.classList.add('dismissed');
      document.body.classList.remove('has-notif');
      applyNotifHeight(0);
    } else {
      // Add dismiss button if not already there
      if (!notifBar.querySelector('.notif-dismiss')) {
        const btn = document.createElement('button');
        btn.className = 'notif-dismiss';
        btn.setAttribute('aria-label', 'Dismiss notification');
        btn.textContent = '×';
        notifBar.appendChild(btn);
      }
      const h = notifBar.offsetHeight;
      applyNotifHeight(h);

      notifBar.querySelector('.notif-dismiss').addEventListener('click', () => {
        notifBar.classList.add('dismissed');
        document.body.classList.remove('has-notif');
        applyNotifHeight(0);
        sessionStorage.setItem('notifDismissed', '1');
        if (nav) nav.style.top = '0';
      });
    }
  }

  // ─── NAV SCROLL EFFECT + NOTIF OVERLAP FIX ──────────────────────
  if (nav) {
    const onScroll = () => {
      const scrolled = window.scrollY;
      const notifH = notifBar && !notifBar.classList.contains('dismissed')
        ? notifBar.offsetHeight : 0;

      // Snap nav to top once notif bar scrolled away
      if (notifH > 0) {
        const remaining = Math.max(0, notifH - scrolled);
        nav.style.top = remaining + 'px';
      } else {
        nav.style.top = '0';
      }

      // Border opacity
      nav.style.borderBottomColor = scrolled > 40
        ? 'rgba(255,255,255,0.1)'
        : 'rgba(255,255,255,0.07)';

      // Scroll progress bar
      const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
      progressBar.style.width = maxScroll > 0
        ? (scrolled / maxScroll * 100) + '%'
        : '0%';

      // Back-to-top visibility
      bttBtn.classList.toggle('visible', scrolled > 400);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll(); // run once on load to set correct state
  }

  // ─── MOBILE NAV ─────────────────────────────────────────────────
  const hamburger = document.getElementById('hamburger');
  const mobileNav = document.getElementById('mobileNav');
  if (hamburger && mobileNav) {
    hamburger.addEventListener('click', () => {
      mobileNav.classList.toggle('open');
      const spans = hamburger.querySelectorAll('span');
      if (mobileNav.classList.contains('open')) {
        spans[0].style.transform = 'translateY(7px) rotate(45deg)';
        spans[1].style.opacity = '0';
        spans[2].style.transform = 'translateY(-7px) rotate(-45deg)';
      } else {
        spans[0].style.transform = '';
        spans[1].style.opacity = '';
        spans[2].style.transform = '';
      }
    });
    document.addEventListener('click', (e) => {
      if (!hamburger.contains(e.target) && !mobileNav.contains(e.target)) {
        mobileNav.classList.remove('open');
        hamburger.querySelectorAll('span').forEach(s => { s.style.transform = ''; s.style.opacity = ''; });
      }
    });
  }

  // ─── TAB SYSTEM ─────────────────────────────────────────────────
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tabName = btn.dataset.tab;
      if (!tabName) return;

      document.querySelectorAll('.tab-btn').forEach(b => {
        if (b.closest('.tab-nav') === btn.closest('.tab-nav')) {
          b.classList.remove('active');
        }
      });
      btn.classList.add('active');

      document.querySelectorAll('.tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `tab-${tabName}`);
      });
    });
  });

  // ─── SCROLL REVEAL ──────────────────────────────────────────────
  const revealObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        revealObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.08, rootMargin: '0px 0px -40px 0px' });

  document.querySelectorAll('.reveal').forEach(el => revealObserver.observe(el));

  // Expose globally so dynamic content (players.html cards) can register
  window.revealObserver = revealObserver;

  // ─── STATS COUNTER ──────────────────────────────────────────────
  function animateCount(el, target, duration = 1600) {
    const isFloat = target % 1 !== 0;
    let start = 0;
    const step = (timestamp) => {
      if (!start) start = timestamp;
      const progress = Math.min((timestamp - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = eased * target;
      el.textContent = isFloat ? current.toFixed(1) : Math.floor(current).toLocaleString();
      if (progress < 1) requestAnimationFrame(step);
      else el.textContent = isFloat ? target.toFixed(1) : target.toLocaleString();
    };
    requestAnimationFrame(step);
  }

  const counterObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting && !entry.target.dataset.counted) {
        entry.target.dataset.counted = 'true';
        const target = parseFloat(entry.target.dataset.count);
        animateCount(entry.target, target);
      }
    });
  }, { threshold: 0.5 });

  document.querySelectorAll('[data-count]').forEach(el => counterObserver.observe(el));

  // Hero countdown is started by the inline script in index.html so the
  // target date can be edited directly on the [data-target] attribute.

  // ─── SMOOTH HOVER GLOW ON CARDS ─────────────────────────────────
  document.querySelectorAll('.player-card, .team-card, .match-card').forEach(card => {
    card.addEventListener('mousemove', (e) => {
      const rect = card.getBoundingClientRect();
      card.style.setProperty('--mx', ((e.clientX - rect.left) / rect.width * 100) + '%');
      card.style.setProperty('--my', ((e.clientY - rect.top) / rect.height * 100) + '%');
    });
  });

  // ─── LB BAR ANIMATE ON SCROLL ───────────────────────────────────
  const lbObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.querySelectorAll('.lb-bar-fill').forEach(el => {
          setTimeout(() => { el.style.width = el.dataset.width + '%'; }, 100);
        });
        lbObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.2 });
  const lbTable = document.getElementById('lbTable');
  if (lbTable) lbObserver.observe(lbTable);

  // ─── BRACKET MATCH HOVER ────────────────────────────────────────
  document.querySelectorAll('.b-match, .bracket-match').forEach(match => {
    match.addEventListener('mouseenter', () => {
      match.style.boxShadow = '0 0 20px rgba(139,92,246,0.18)';
    });
    match.addEventListener('mouseleave', () => {
      match.style.boxShadow = '';
    });
  });

  // ─── ESC KEY TO CLOSE MODALS ────────────────────────────────────
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      // Bracket match detail modal
      const modal = document.querySelector('.match-detail-overlay');
      if (modal && typeof closeModal === 'function') closeModal();
    }
  });

  // ─── TOOLTIP SYSTEM ─────────────────────────────────────────────
  let _tip = null;
  document.querySelectorAll('[title]').forEach(el => {
    const title = el.getAttribute('title');
    if (!title) return;
    el.removeAttribute('title');
    el.setAttribute('data-tooltip', title);
    el.addEventListener('mouseenter', (e) => {
      if (!_tip) {
        _tip = document.createElement('div');
        _tip.id = '_tooltip';
        _tip.style.cssText = 'position:fixed;background:#1B173B;border:1px solid rgba(139,92,246,0.35);border-radius:8px;padding:6px 12px;font-size:.75rem;color:#F5F2FF;pointer-events:none;z-index:9999;transition:opacity .15s;white-space:nowrap;box-shadow:0 8px 20px rgba(0,0,0,0.4);';
        document.body.appendChild(_tip);
      }
      _tip.textContent = title;
      _tip.style.opacity = '1';
      _tip.style.top  = (e.clientY - 36) + 'px';
      _tip.style.left = (e.clientX + 8) + 'px';
    });
    el.addEventListener('mousemove', (e) => {
      if (_tip) {
        _tip.style.top  = (e.clientY - 36) + 'px';
        _tip.style.left = (e.clientX + 8) + 'px';
      }
    });
    el.addEventListener('mouseleave', () => {
      if (_tip) _tip.style.opacity = '0';
    });
  });

  console.log('%c// Slice League', 'color:#8B5CF6;font-size:1.2rem;font-weight:700;font-family:monospace');
  console.log('%cBelgian Brawl Stars Tournament · Season 2 · 2026', 'color:#A3E635;font-size:.9rem;');
  console.log('%cOrganized by Nikounaki · Sponsored by Keytrade Bank', 'color:#5b9fef;font-size:.8rem;');
});
