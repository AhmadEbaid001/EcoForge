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
import { escapeHtml, hydrateCharts, icon } from './charts.js';

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

const state = { user: null, view: 'overview', viewLifetime: null };

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

  /* The reference's sign-in screen: a slim product bar, one centred card, a
   * second card stating what the deployment is, and a footer rule.
   *
   * Its second card listed an encryption algorithm, a FIPS level, a node
   * identifier and a version number. None of those are things this system can
   * support, and a compliance claim in front of judges has to be one the code
   * can back. The four below are: the Administration screen reports every one
   * of them, and `test_auth.py` pins them. */
  $('root').innerHTML = `
    <div class="login-wrap">
      <header class="login-bar">
        <span class="mark">${icon('bolt')}</span>
        <p class="wordmark">GEMP</p>
        <span class="chip">Restricted access</span>
        ${appearanceSwitch()}
      </header>

      <div class="login-body">
        <div class="login-col">
          <form class="login" id="login-form">
            <h2>Sign in</h2>
            <p class="lede">Green Energy Monitoring Platform — budget-constrained
            retrofit prioritization.</p>

            ${message ? `<div class="error-box" role="alert">${escapeHtml(message)}</div>` : ''}

            <label for="login-user">Username
              <input id="login-user" name="username" type="text"
                     autocomplete="username" required autofocus></label>

            <label for="login-pass">Password
              <span class="password-field">
                <input id="login-pass" name="password" type="password"
                       autocomplete="current-password" required>
                <button type="button" class="reveal" id="login-reveal"
                        aria-pressed="false">Show</button>
              </span>
              <span class="caps-hint" id="caps-hint" role="status">Caps Lock is on.</span>
            </label>

            <button type="submit" class="primary" id="login-submit">Sign in</button>
          </form>

          <section class="login-facts">
            <h3>Authorized personnel only</h3>
            <dl>
              <div><dt>Sessions</dt><dd>Server-side, revocable</dd></div>
              <div><dt>Idle timeout</dt><dd>8 hours</dd></div>
              <div><dt>Password hashing</dt><dd>scrypt</dd></div>
              <div><dt>Audit log</dt><dd>Every action</dd></div>
            </dl>
          </section>
        </div>
      </div>

      <footer class="login-foot">
        <span>Accounts are created by an administrator. No self-service registration,
        no default account.</span>
        <span>Team Ecoforge &middot; RoboDam2026</span>
      </footer>
    </div>`;

  wireAppearance($('root'));

  /* Caps Lock is the commonest reason a correct password is refused, and the
   * server deliberately will not say which reason it was - so the page has to
   * catch this one itself, before the request goes out. */
  const caps = $('caps-hint');
  const watchCaps = (event) => {
    if (typeof event.getModifierState !== 'function') return;
    caps.classList.toggle('on', event.getModifierState('CapsLock'));
  };
  $('login-pass').addEventListener('keydown', watchCaps);
  $('login-pass').addEventListener('keyup', watchCaps);
  $('login-user').addEventListener('keyup', watchCaps);

  const reveal = $('login-reveal');
  reveal.addEventListener('click', () => {
    const field = $('login-pass');
    const shown = field.type === 'text';
    field.type = shown ? 'password' : 'text';
    reveal.textContent = shown ? 'Show' : 'Hide';
    reveal.setAttribute('aria-pressed', String(!shown));
    field.focus();
  });

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

/* Seven destinations in a row of top tabs was already crowding the header at
 * 1180px, and it left nowhere to put a page title. A fixed sidebar gives each
 * destination a stable position, room for a label AND an icon, and hands the
 * whole top of the workspace back to the screen you are actually on. It
 * collapses to icons on a narrow window rather than wrapping. */
async function showApp() {
  document.body.className = 'signed-in';

  const visible = ORDER
    .filter((key) => !VIEWS[key].requiredRole || can(state.user, VIEWS[key].requiredRole));

  const navItems = visible.map((key) => `
    <button class="nav-item" type="button" data-view="${key}">
      ${icon(key)}<span class="nav-label">${escapeHtml(VIEWS[key].title)}</span>
    </button>`).join('');

  const who = escapeHtml(state.user.display_name || state.user.username);

  $('root').innerHTML = `
    <div class="shell">
      <aside class="sidebar" id="sidebar">
        <div class="sidebar-brand">
          <span class="mark">${icon('bolt')}</span>
          <div class="sidebar-brand-text">
            <h1>GEMP</h1>
            <p class="sub">Team Ecoforge</p>
          </div>
        </div>

        <nav class="nav" aria-label="Views">${navItems}</nav>

        <div class="sidebar-foot">
          <div class="status" id="status" role="status">
            <span class="nav-slot"><span class="dot" id="health-dot"></span></span>
            <span id="health-text" class="nav-label">connecting</span>
          </div>
          <button class="nav-item" type="button" data-view="account"
                  aria-label="Account settings for ${who}">
            ${icon('account')}
            <span class="nav-label nav-user">
              <span class="nav-user-name">${who}</span>
              <span class="badge role">${escapeHtml(state.user.role)}</span>
            </span>
          </button>
          <button class="nav-item" type="button" id="sign-out">
            ${icon('signout')}<span class="nav-label">Sign out</span>
          </button>
          <button class="nav-item" type="button" id="nav-toggle"
                  aria-controls="sidebar" aria-expanded="true">
            ${icon('menu')}<span class="nav-label">Collapse</span>
          </button>
          ${appearanceSwitch()}
        </div>
      </aside>

      <div class="workspace">
        <!-- The page header sits OUTSIDE the scrolling pane, which is what
             keeps it and the sidebar still while fourteen rows of alerts move
             underneath them. Views write into it through ui.pageHead(). -->
        <div id="page-head"></div>
        <main id="view"></main>
      </div>
    </div>`;

  wireAppearance($('root'));

  document.querySelectorAll('[data-view]').forEach((button) => {
    button.addEventListener('click', () => navigate(button.dataset.view));
  });
  $('sign-out').addEventListener('click', async () => {
    await api.logout().catch(() => {});
    state.user = null;
    showLogin();
  });

  /* Collapsing is a choice the person made, so it outlives a navigation. */
  const toggle = $('nav-toggle');
  const applyCollapse = (collapsed) => {
    document.body.classList.toggle('nav-collapsed', collapsed);
    toggle.setAttribute('aria-expanded', String(!collapsed));
    try { window.localStorage.setItem(NAV_KEY, collapsed ? 'collapsed' : 'open'); } catch { /* ignore */ }
  };
  toggle.addEventListener('click', () =>
    applyCollapse(!document.body.classList.contains('nav-collapsed')));
  try {
    applyCollapse(window.localStorage.getItem(NAV_KEY) === 'collapsed');
  } catch { applyCollapse(false); }

  await navigate(fromHash() || 'overview');
}

const NAV_KEY = 'gemp.nav';

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
  document.querySelectorAll('.nav-item').forEach((item) => {
    if (item.dataset.view === key) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  /* Cleared here rather than by each view, so a view that throws before it
   * renders cannot leave the previous screen's title above the error. */
  const head = $('page-head');
  if (head) head.innerHTML = '';

  /* Every listener a view attaches outside its own subtree is tied to this.
   *
   * Replacing `#view`'s innerHTML drops the listeners INSIDE it, and nothing else.
   * A view that listens on `window` - the map does, for resize, mouseup and
   * mousemove - leaves those behind on every mount, so five visits to the Map
   * meant five resize handlers, each rebuilding the projection for a screen that
   * was no longer on it.
   *
   * Aborting the previous signal before rendering the next view removes them in
   * one step, and a view opts in simply by passing `{ signal }` to
   * addEventListener. No teardown function to remember to return, and nothing to
   * forget when a new view is written. */
  if (state.viewLifetime) state.viewLifetime.abort();
  state.viewLifetime = new AbortController();

  const root = $('view');
  root.innerHTML = '<div class="loading">Loading…</div>';
  try {
    await view.render(root, {
      user: state.user,
      signal: state.viewLifetime.signal,
      onPasswordChanged: refreshSession,
    });

    /* Both of these are done here rather than by each view, for the same reason
     * the abort signal is: a view should not have to remember cross-cutting
     * furniture, and seven views each remembering it differently is how they
     * drift apart. */
    const signal = state.viewLifetime.signal;
    hydrateCharts(root, signal);
    showAge(signal);

    /* A view can rebuild itself without the shell knowing - the Refresh button in
     * the workspace header calls the view's own render(). Everything hydrated
     * above is then thrown away and replaced by fresh markup that nothing has
     * connected, so the charts would go back to drawing at a fixed width and the
     * age chip would simply vanish. Watching for that is cheaper than asking
     * every view to remember to announce it. */
    const rehydrate = new MutationObserver(() => {
      hydrateCharts(root, signal);
      if (!document.querySelector('.freshness-age')) showAge(signal);
    });
    rehydrate.observe(root, { childList: true, subtree: true });
    signal.addEventListener('abort', () => rehydrate.disconnect());
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

/* --------------------------------------------------------------- staleness */

/* Data time advances at 720x, so a screen that was accurate when it loaded is
 * wrong a minute later - and the data clock in the header keeps stating the
 * moment it was read, with nothing to say that moment has passed.
 *
 * Nothing is polled. A dashboard that re-fetches on a timer during a
 * demonstration competes for the same solver the person is dragging a slider
 * against, and it moves numbers under someone who is talking about them. What
 * this does instead is age the stamp honestly: the reading is from then, this is
 * how long ago that was, the Refresh button is right there.
 */
const AGE_TICK_MS = 15000;
const STALE_AFTER_MS = 120000;

function showAge(signal) {
  const stamp = document.querySelector('.freshness');
  if (!stamp) return;

  const readAt = Date.now();
  const age = document.createElement('span');
  age.className = 'freshness-age';
  stamp.appendChild(age);

  const tick = () => {
    const seconds = Math.round((Date.now() - readAt) / 1000);
    age.textContent = seconds < 45 ? 'read just now'
      : `read ${Math.round(seconds / 60)} min ago`;
    age.classList.toggle('stale', Date.now() - readAt > STALE_AFTER_MS);
  };
  tick();

  const timer = window.setInterval(tick, AGE_TICK_MS);
  signal?.addEventListener('abort', () => window.clearInterval(timer));
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
