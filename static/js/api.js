/**
 * Thin fetch wrapper for the REST API.
 *
 * Every unsafe request carries the CSRF token from the cookie, so the dashboard
 * can talk to the API with the same session and the same protection as the HTML
 * forms. Error responses use the envelope produced by core.exceptions:
 *   { detail, code, errors }
 */
(function (window) {
  'use strict';

  function getCookie(name) {
    const match = document.cookie.match(new RegExp('(^|;\\s*)' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[2]) : null;
  }

  class ApiError extends Error {
    constructor(status, payload) {
      super((payload && payload.detail) || 'Request failed.');
      this.name = 'ApiError';
      this.status = status;
      this.code = (payload && payload.code) || 'error';
      this.errors = (payload && payload.errors) || {};
    }
  }

  async function request(url, options) {
    const opts = Object.assign({ method: 'GET', headers: {} }, options || {});
    const method = opts.method.toUpperCase();

    opts.headers = Object.assign({ Accept: 'application/json' }, opts.headers);
    opts.credentials = 'same-origin';

    if (!['GET', 'HEAD', 'OPTIONS', 'TRACE'].includes(method)) {
      opts.headers['X-CSRFToken'] = getCookie('csrftoken') || '';
    }

    // Plain objects are sent as JSON; FormData is left alone so the browser can
    // set its own multipart boundary (used by the CSV upload in Phase 3).
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body === 'object') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }

    const response = await fetch(url, opts);

    if (response.status === 204) {
      return null;
    }

    let payload = null;
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      payload = await response.json().catch(() => null);
    }

    if (!response.ok) {
      throw new ApiError(response.status, payload);
    }
    return payload;
  }

  const api = {
    ApiError: ApiError,
    request: request,
    get: (url) => request(url, { method: 'GET' }),
    post: (url, body) => request(url, { method: 'POST', body: body }),
    patch: (url, body) => request(url, { method: 'PATCH', body: body }),
    put: (url, body) => request(url, { method: 'PUT', body: body }),
    delete: (url) => request(url, { method: 'DELETE' }),
    getCookie: getCookie,
  };

  window.api = api;
})(window);
