/* The application shell: who you are, where you are, and what you may see.
 *
 * Three states, and only three. Signed out shows the login form and nothing else.
 * Signed in with a password that must change shows the change-password form and
 * nothing else - the server refuses every other call in that state, so offering the
 * navigation would be offering a set of buttons that all fail. Signed in normally
 * shows the tabs the role can use.
 *
 * Routing is the URL fragment, so a reload lands where you were and a tab can be
 * linked to. There is no history rewriting and no client-side router: six views do
 * not need one.
 */

'use strict';

import { api, setPasswordChangeHandler, setUnauthenticatedHandler } from './api.js';
import { escapeHtml } from './charts.js';
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

/* ------------------------------------------------------------------- login */

function showLogin(message = '') {
  document.body.className = 'signed-out';
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

        ${message ? `<div class="error-box">${escapeHtml(message)}</div>` : ''}

        <label>Username<input id="login-user" autocomplete="username" required autofocus></label>
        <label>Password<input id="login-pass" type="password" autocomplete="current-password" required></label>
        <button type="submit" id="login-submit">Sign in</button>

        <p class="caption">Accounts are created by an administrator. There is no
        self-service registration and no default account.</p>
      </form>
    </div>`;

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
        <label>Current password<input id="cp-current" type="password" required autofocus></label>
        <label>New password<input id="cp-new" type="password" minlength="12" required></label>
        <button type="submit">Set password</button>
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
      $('change-error').innerHTML = `<div class="error-box">${escapeHtml(error.detail || error.message)}</div>`;
    }
  });
}

/* --------------------------------------------------------------------- app */

async function showApp() {
  document.body.className = 'signed-in';

  const tabs = ORDER
    .filter((key) => !VIEWS[key].requiredRole || can(state.user, VIEWS[key].requiredRole))
    .map((key) => `<button class="tab" data-view="${key}">${escapeHtml(VIEWS[key].title)}</button>`)
    .join('');

  $('root').innerHTML = `
    <header class="bar">
      <div class="brand">
        <span class="mark"></span>
        <div>
          <h1>Green Energy Monitoring Platform</h1>
          <p class="sub">Team Ecoforge</p>
        </div>
      </div>
      <nav class="tabs">${tabs}</nav>
      <div class="status" id="status">
        <span class="dot" id="health-dot"></span><span id="health-text">connecting</span>
      </div>
      <div class="who">
        <button class="ghost small" data-view="account">
          ${escapeHtml(state.user.display_name || state.user.username)}
          <span class="badge role">${escapeHtml(state.user.role)}</span>
        </button>
        <button class="ghost small" id="sign-out">Sign out</button>
      </div>
    </header>
    <main id="view"></main>`;

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

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === key);
  });

  const root = $('view');
  root.innerHTML = '<div class="loading">Loading…</div>';
  try {
    await view.render(root, {
      user: state.user,
      onPasswordChanged: () => {},
    });
  } catch (error) {
    root.innerHTML = `<div class="error-box">${escapeHtml(error.detail || error.message)}</div>`;
  }
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
