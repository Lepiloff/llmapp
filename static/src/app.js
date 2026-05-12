/* ============================================================
 * LLM App Market — htmx + UX hooks
 * Keep tiny. No framework. Progressive enhancement only.
 * ============================================================ */
(function () {
  'use strict';

  function getCookie(name) {
    var match = document.cookie.match(new RegExp('(^|; )' + name + '=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : '';
  }

  // ---------- htmx configuration ----------
  document.addEventListener('htmx:configRequest', function (evt) {
    var token = getCookie('csrftoken');
    if (token) evt.detail.headers['X-CSRFToken'] = token;
  });

  // ---------- Top-of-page progress bar during htmx swaps ----------
  var bar = null;
  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement('div');
    bar.className = 'progress-grid';
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-hidden', 'true');
    return bar;
  }
  document.addEventListener('htmx:beforeRequest', function () {
    ensureBar();
    if (!bar.isConnected) document.body.appendChild(bar);
  });
  document.addEventListener('htmx:afterRequest', function () {
    if (bar && bar.isConnected) bar.remove();
  });
  document.addEventListener('htmx:responseError', function () {
    if (bar && bar.isConnected) bar.remove();
  });

  // ---------- Focus management after htmx swaps ----------
  document.addEventListener('htmx:afterSwap', function (evt) {
    // If the new content has an [autofocus] element, focus it.
    var target = evt.target;
    var auto = target.querySelector('[autofocus]');
    if (auto) auto.focus();
  });

  // ---------- Auto-dismiss flash messages after 6s (DOM only; assistive tech still sees them) ----------
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-flash]').forEach(function (el) {
      setTimeout(function () { el.style.opacity = '0'; setTimeout(function(){ el.remove(); }, 400); }, 6000);
    });
  });
})();
