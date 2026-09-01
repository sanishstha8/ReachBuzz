/**
 * Toast notifications.
 *
 * Server-rendered Django messages are shown on load; window.toast(text, level)
 * raises the same notification from AJAX flows.
 */
(function (window, document) {
  'use strict';

  const LEVEL_COLOURS = {
    debug: 'secondary',
    info: 'info',
    success: 'success',
    warning: 'warning',
    error: 'danger',
    danger: 'danger',
  };

  function container() {
    let el = document.getElementById('toastContainer');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toastContainer';
      el.className = 'toast-container position-fixed bottom-0 end-0 p-3';
      el.setAttribute('aria-live', 'polite');
      el.setAttribute('aria-atomic', 'true');
      document.body.appendChild(el);
    }
    return el;
  }

  function toast(text, level) {
    const colour = LEVEL_COLOURS[level] || 'secondary';
    const isError = colour === 'danger';

    const el = document.createElement('div');
    el.className = 'toast align-items-center border-0 text-bg-' + colour;
    el.setAttribute('role', isError ? 'alert' : 'status');

    const body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = text; // textContent, never innerHTML: no XSS via messages.

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-white me-2 m-auto';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Close');

    const row = document.createElement('div');
    row.className = 'd-flex';
    row.appendChild(body);
    row.appendChild(close);
    el.appendChild(row);

    container().appendChild(el);

    const instance = new window.bootstrap.Toast(el, { autohide: !isError, delay: 6000 });
    el.addEventListener('hidden.bs.toast', () => el.remove());
    instance.show();
    return instance;
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('#toastContainer .toast').forEach(function (el) {
      window.bootstrap.Toast.getOrCreateInstance(el).show();
    });
  });

  window.toast = toast;
})(window, document);
