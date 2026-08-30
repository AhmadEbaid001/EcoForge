/* One way in and out of the API.
 *
 * Every request in the application goes through here so that three things are true in
 * exactly one place: the CSRF token is attached, an expired session logs the user out
 * instead of leaving them clicking a dead page, and an error carries a message a
 * person can act on rather than "[object Object]".
 */

'use strict';

const BASE = '/api/v1';

/* Read by the page, echoed in a header. The cookie is deliberately not HttpOnly -
 * that is what makes the double-submit check work, because an attacker on another
 * origin can cause the cookie to be SENT but cannot READ it to build the header. */
function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)gemp_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : '';
}

export class ApiError extends Error {
  constructor(status, detail, body) {
    super(detail || `request failed (${status})`);
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

/* Set by the shell. Called when the server says the session is gone, so a user who
 * has been idle for eight hours sees the login page rather than silent failures. */
let onUnauthenticated = () => {};
export function setUnauthenticatedHandler(fn) { onUnauthenticated = fn; }

let onPasswordChangeRequired = () => {};
export function setPasswordChangeHandler(fn) { onPasswordChangeRequired = fn; }

async function request(method, path, body) {
  const options = {
    method,
    headers: { 'Accept': 'application/json' },
    /* Same-origin only. The API is served from the same host as the page, so there is
     * never a reason for a credentialed cross-origin request - and saying so means a
     * misconfiguration cannot turn into one. */
    credentials: 'same-origin',
  };

  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  if (method !== 'GET' && method !== 'HEAD') {
    options.headers['X-GEMP-CSRF'] = csrfToken();
  }

  const response = await fetch(BASE + path, options);

  if (response.status === 204) return null;

  let payload = null;
  const type = response.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    payload = await response.json().catch(() => null);
  }

  if (!response.ok) {
    const detail = payload?.detail ?? payload?.error ?? response.statusText;
    if (response.status === 401) onUnauthenticated();
    if (response.status === 409 && response.headers.get('X-GEMP-Password-Change')) {
      onPasswordChangeRequired();
    }
    throw new ApiError(response.status, typeof detail === 'string' ? detail : 'request failed', payload);
  }

  return payload;
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
  patch: (path, body) => request('PATCH', path, body),

  /* --- auth --- */
  session: () => request('GET', '/auth/session'),
  login: (username, password) => request('POST', '/auth/login', { username, password }),
  logout: () => request('POST', '/auth/logout'),
  changePassword: (current_password, new_password) =>
    request('POST', '/auth/password', { current_password, new_password }),
  mySessions: () => request('GET', '/auth/sessions'),

  /* --- dashboards --- */
  summary: () => request('GET', '/dashboard/summary'),
  /* The three fields the alert inbox needs. Calling summary() for them meant
     waiting on the reading count, which is six seconds of work for a number
     the inbox never shows. */
  alertSummary: () => request('GET', '/dashboard/alerts/summary'),
  load: (days) => request('GET', `/dashboard/load?days=${days}`),
  anomaliesDaily: (days) => request('GET', `/dashboard/anomalies/daily?days=${days}`),
  anomalyFeed: (params = '') => request('GET', `/dashboard/anomalies${params}`),
  runs: () => request('GET', '/dashboard/runs'),
  forecast: (buildingId, hours) =>
    request('GET', `/dashboard/forecast/${encodeURIComponent(buildingId)}?hours=${hours}`),

  /* --- portfolio and optimizer --- */
  meta: () => request('GET', '/meta'),
  geojson: (runId) => request('GET', `/map/geojson${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`),
  optimize: (payload) => request('POST', '/optimize', payload),
  compare: (payload) => request('POST', '/compare', payload),
  buildingCandidates: (id, runId) =>
    request('GET', `/buildings/${encodeURIComponent(id)}/candidates${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`),
  narrative: () => request('GET', '/narrative/building-specific'),
  acknowledge: (body) => request('POST', '/anomalies/acknowledge', body),
  acknowledgeOne: (id) => request('POST', `/anomalies/${id}/acknowledge`),
  verifyChain: (buildingId) =>
    request('GET', `/integrity/verify/${encodeURIComponent(buildingId)}`),

  /* --- evidence and procurement --- */
  evidence: () => request('GET', '/evidence/claims'),
  boq: (runId) => request('GET', `/runs/${encodeURIComponent(runId)}/boq`),

  /* --- administration --- */
  users: () => request('GET', '/admin/users'),
  createUser: (body) => request('POST', '/admin/users', body),
  updateUser: (id, body) => request('PATCH', `/admin/users/${encodeURIComponent(id)}`, body),
  resetPassword: (id, new_password) =>
    request('POST', `/admin/users/${encodeURIComponent(id)}/password`, { new_password }),
  audit: (limit = 200) => request('GET', `/admin/audit?limit=${limit}`),
  securityPosture: () => request('GET', '/admin/security'),
  recompute: () => request('POST', '/candidates/recompute'),

  /* --- maintenance jobs --- */
  jobStart: (kind) => request('POST', `/jobs/${encodeURIComponent(kind)}`),
  jobStatus: (kind) => request('GET', `/jobs/${encodeURIComponent(kind)}`),
};
