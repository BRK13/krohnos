(function () {
  var d = document.documentElement;
  var live = document.getElementById('live');

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.className = 'sr';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () {
        return fallbackCopy(text);
      });
    }
    return Promise.resolve(fallbackCopy(text));
  }

  function textFor(btn) {
    var id = btn.getAttribute('data-copy');
    if (id) return document.getElementById(id).textContent;
    id = btn.getAttribute('data-md');
    return document.getElementById(id).textContent;
  }

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-copy], button[data-md]');
    if (!btn) return;
    var label = btn.getAttribute('data-label');
    var done = btn.getAttribute('data-done');
    copyText(textFor(btn)).then(function (ok) {
      btn.classList.toggle('is-done', ok);
      btn.textContent = ok ? done : (d.lang === 'en' ? 'Select and copy manually' : 'Selecione e copie à mão');
      if (live) live.textContent = ok ? done : btn.textContent;
      clearTimeout(btn._t);
      btn._t = setTimeout(function () {
        btn.textContent = label;
        btn.classList.remove('is-done');
        if (live) live.textContent = '';
      }, 2000);
    });
  });

  var theme = document.getElementById('theme');
  function current() {
    var t = d.getAttribute('data-theme');
    if (t) return t;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  if (theme) theme.addEventListener('click', function () {
    var n = current() === 'dark' ? 'light' : 'dark';
    d.setAttribute('data-theme', n);
    try { localStorage.setItem('kronos-theme', n); } catch (e) {}
  });

  var menu = document.getElementById('menu');
  var nav = document.getElementById('nav');
  function setMenu(open) {
    d.classList.toggle('menu-open', open);
    if (menu) menu.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  if (menu) {
    menu.addEventListener('click', function () { setMenu(!d.classList.contains('menu-open')); });
    nav.addEventListener('click', function (e) { if (e.target.closest('a')) setMenu(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setMenu(false); });
  }

  /* Section entrance: content is visible at rest; this only animates when the script runs. */
  var items = document.querySelectorAll('.appear');
  if ('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    d.classList.add('anim');
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { rootMargin: '0px 0px -8% 0px' });
    items.forEach(function (el) { io.observe(el); });
    setTimeout(function () { items.forEach(function (el) { el.classList.add('in'); }); }, 4000);
  }
})();
