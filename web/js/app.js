/* The application shell: who you are, where you are, and what you may see.
 *
 * Three states, and only three. Signed out shows the login form and nothing else.
 * Signed in with a password that must change shows the change-password form and
 * nothing else - the server refuses every other call in that state, so offering the
 * navigation would be offering a set of buttons that all fail. Signed in normally
 * shows the tabs the role can use.
 *
 * Routing is the URL fragment, so a reload lands where you were and a tab can be
 * linked to. There is no history rewriting and no client-side router: seven views do
 * not need one.
 */

'use strict';

import { api, setPasswordChangeHandler, setUnauthenticatedHandler } from './api.js';
import { escapeHtml, icon } from './charts.js';
import { mapView } from './map.js';
import { account, admin, alerts, forecasts, integrity, overview, runs } from './views.js';

const VIEWS = {
  overview,
  map: mapView,
  forecasts,
  alerts,
  runs,
  integrity,
  admin,
  account,
};

const ORDER = ['overview', 'map', 'forecasts', 'alerts', 'runs', 'integrity', 'admin'];

const ROLE_LADDER = ['viewer', 'analyst', 'admin'];
const can = (user, role) => ROLE_LADDER.indexOf(user.role) >= ROLE_LADDER.indexOf(role);

const $ = (id) => document.getElementById(id);

const state = { user: null, view: 'overview' };

/* -------------------------------------------------------------- appearance */

/* Light and dark, with Auto as the default.
 *
 * Auto means "no data-theme attribute", which leaves the stylesheet's
 * prefers-color-scheme query in charge. That ordering matters for a reason
 * beyond taste: the CSP forbids inline script, so nothing can run before the
 * stylesheet paints. A design where the correct appearance depended on
 * JavaScript would therefore flash the wrong one on every load. Following the
 * system by default means the common case is right before this file executes.
 *
 * Light and Dark exist anyway because this screen gets demonstrated on other
 * people's projectors, where the right answer is whatever the room can read.
 */
const APPEARANCE_KEY = 'gemp.appearance';
const APPEARANCES = ['auto', 'light', 'dark'];

function storedAppearance() {
  try {
    const value = window.localStorage.getItem(APPEARANCE_KEY);
    return APPEARANCES.includes(value) ? value : 'auto';
  } catch {
    /* Private-mode browsers throw rather than return null. Auto is a fine answer. */
    return 'auto';
  }
}

function applyAppearance(choice) {
  const root = document.documentElement;
  if (choice === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);

  try {
    window.localStorage.setItem(APPEARANCE_KEY, choice);
  } catch { /* not worth failing a page load over */ }

  document.querySelectorAll('[data-appearance]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.appearance === choice));
  });
}

const APPEARANCE_LABELS = {
  auto: 'Match the system appearance',
  light: 'Light appearance',
  dark: 'Dark appearance',
};

function appearanceSwitch() {
  const current = storedAppearance();
  const buttons = APPEARANCES.map((key) => `
    <button type="button" data-appearance="${key}" title="${APPEARANCE_LABELS[key]}"
            aria-label="${APPEARANCE_LABELS[key]}"
            aria-pressed="${key === current}">${icon(key)}</button>`).join('');

  return `<div class="appearance" role="group" aria-label="Appearance">${buttons}</div>`;
}

function wireAppearance(container) {
  container.querySelectorAll('[data-appearance]').forEach((button) => {
    button.addEventListener('click', () => applyAppearance(button.dataset.appearance));
  });
}

/* ------------------------------------------------------------------- login */

function showLogin(message = '') {
  document.body.className = 'signed-out';
  /* Signing out from #/admin and back in as a viewer used to land on #/admin,
   * which navigate() silently refuses - leaving the loading state on screen
   * with no way to tell what had gone wrong. */
  if (window.location.hash) {
    window.history.replaceState(null, '', window.location.pathname);
  }

  $('root').innerHTML = `
    <div class="login-wrap">
      <form class="login" id="login-form">
        <div class="brand">
          <span class="mark"></span>
          <div>
            <h1>Green Energy Monitoring Platform</h1>
            <p class="sub">Budget-constrained retrofit prioritization</p>
          </div>
        </div>

        ${message ? `<div class="error-box" role="alert">${escapeHtml(message)}</div>` : ''}

        <label for="login-user">Username
          <input id="login-user" name="username" autocomplete="username" required autofocus></label>
        <label for="login-pass">Password
          <input id="login-pass" name="password" type="password" autocomplete="current-password" required></label>
        <button type="submit" class="primary" id="login-submit">Sign in</button>

        <p class="caption">Accounts are created by an administrator. There is no
        self-service registration and no default account.</p>

        <div class="form-actions">${appearanceSwitch()}</div>
      </form>
    </div>`;

  wireAppearance($('root'));

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('login-submit');
    button.disabled = true;
    button.textContent = 'Signing in…';
    try {
      const result = await api.login($('login-user').value, $('login-pass').value);
      state.user = result.user;
      if (result.must_change_password) {
        showPasswordChange();
      } else {
        await showApp();
      }
    } catch (error) {
      /* The server deliberately returns the same message for a wrong password, an
       * unknown account and a disabled one. Passing it through unchanged keeps that
       * property instead of helpfully guessing which it was. */
      showLogin(error.detail || 'sign in failed');
    }
  });
}

function showPasswordChange() {
  document.body.className = 'signed-out';
  $('root').innerHTML = `
    <div class="login-wrap">
      <form class="login" id="change-form">
        <h1>Choose a new password</h1>
        <p class="caption">This account was created with a temporary password. It has to
        be replaced before anything else can be used.</p>
        <div id="change-error"></div>
        <label for="cp-current">Current password
          <input id="cp-current" type="password" autocomplete="current-password" required autofocus></label>
        <label for="cp-new">New password
          <input id="cp-new" type="password" autocomplete="new-password" minlength="12" required>
          <span class="hint">At least 12 characters.</span></label>
        <button type="submit" class="primary">Set password</button>
      </form>
    </div>`;

  $('change-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api.changePassword($('cp-current').value, $('cp-new').value);
      const session = await api.session();
      state.user = session.user;
      await showApp();
    } catch (error) {
      $('change-error').innerHTML =
        `<div class="error-box" role="alert">${escapeHtml(error.detail || error.message)}</div>`;
    }
  });
}

/* --------------------------------------------------------------------- app */

async function showApp() {
  document.body.className = 'signed-in';

  const tabs = ORDER
    .filter((key) => !VIEWS[key].requiredRole || can(state.user, VIEWS[key].requiredRole))
    .map((key) => `<button class="tab" type="button" data-view="${key}">${escapeHtml(VIEWS[key].title)}</button>`)
    .join('');

  const who = escapeHtml(state.user.display_name || state.user.username);

  $('root').innerHTML = `
    <header class="bar">
      <div class="brand">
        <span class="mark"></span>
        <div>
          <h1>Green Energy Monitoring Platform</h1>
          <p class="sub">Team Ecoforge</p>
        </div>
      </div>
      <nav class="tabs" aria-label="Views">${tabs}</nav>
      <div class="status" id="status" role="status">
        <span class="dot" id="health-dot"></span><span id="health-text">connecting</span>
      </div>
      ${appearanceSwitch()}
      <div class="who">
        <button class="ghost small" type="button" data-view="account"
                aria-label="Account settings for ${who}">
          ${who}
          <span class="badge role">${escapeHtml(state.user.role)}</span>
        </button>
        <button class="ghost small" type="button" id="sign-out">Sign out</button>
      </div>
    </header>
    <main id="view"></main>`;

  wireAppearance($('root'));

  document.querySelectorAll('[data-view]').forEach((button) => {
    button.addEventListener('click', () => navigate(button.dataset.view));
  });
  $('sign-out').addEventListener('click', async () => {
    await api.logout().catch(() => {});
    state.user = null;
    showLogin();
  });

  await navigate(fromHash() || 'overview');
}

function fromHash() {
  const key = window.location.hash.replace(/^#\/?/, '');
  return VIEWS[key] ? key : null;
}

async function navigate(key) {
  const view = VIEWS[key];
  if (!view) return;
  if (view.requiredRole && !can(state.user, view.requiredRole)) return;

  state.view = key;
  window.location.hash = `#/${key}`;

  /* aria-current rather than a class, so the styling and the announcement to a
   * screen reader cannot drift apart: there is one source for both. */
  document.querySelectorAll('.tab').forEach((tab) => {
    if (tab.dataset.view === key) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  });

  const root = $('view');
  root.innerHTML = '<div class="loading">Loading…</div>';
  try {
    await view.render(root, {
      user: state.user,
      onPasswordChanged: refreshSession,
    });
  } catch (error) {
    root.innerHTML = `<div class="error-box" role="alert">${escapeHtml(error.detail || error.message)}</div>`;
  }
}

/* A password change revokes every other session for the account, and the one
 * doing the changing keeps a session whose flags have moved. Re-reading it
 * costs one request and keeps the header from claiming a role the server has
 * stopped agreeing with. */
async function refreshSession() {
  try {
    const session = await api.session();
    if (session.authenticated) state.user = session.user;
  } catch { /* the next request will discover it too */ }
}

/* -------------------------------------------------------------------- boot */

setUnauthenticatedHandler(() => {
  /* Any request can discover the session has gone - an idle timeout, an admin
   * disabling the account, a revoked session. Whichever request finds out first sends
   * the user somewhere they can do something about it. */
  if (state.user) {
    state.user = null;
    showLogin('Your session has ended. Please sign in again.');
  }
});

setPasswordChangeHandler(() => {
  if (state.user) showPasswordChange();
});

window.addEventListener('hashchange', () => {
  const key = fromHash();
  if (state.user && key && key !== state.view) navigate(key);
});

async function boot() {
  applyAppearance(storedAppearance());
  try {
    const session = await api.session();
    if (!session.authenticated) {
      showLogin();
      return;
    }
    state.user = session.user;
    if (session.must_change_password) {
      showPasswordChange();
      return;
    }
    await showApp();
  } catch (error) {
    showLogin(`Cannot reach the server: ${error.message}`);
  }
}

boot();
