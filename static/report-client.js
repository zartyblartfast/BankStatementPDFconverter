'use strict';
// Same-origin, session-bound report editing. No file: or remote API fallback.
async function ledgerFetch(path, options = {}) {
  if (window.location.protocol === 'file:') {
    throw new Error('Standalone HTML editing is unsupported. Open this report through the local dashboard at http://127.0.0.1:5000/report.');
  }
  const target = new URL(path, window.location.href);
  if (target.origin !== window.location.origin) {
    throw new Error('Only same-origin local dashboard requests are supported.');
  }
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const response = await fetch('/api/csrf-token', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('Reload this report in the local dashboard to authorize editing.');
    const token = await response.json();
    headers.set('X-CSRF-Token', token.csrf_token);
  }
  return fetch(target.href, {...options, method, headers, credentials: 'same-origin'});
}
