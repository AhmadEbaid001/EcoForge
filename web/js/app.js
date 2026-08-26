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
import { wordmark } from './brand.js';
import { escapeHtml, hydrateCharts, icon } from './charts.js';
import { currentLang, locale, setLangCode, t } from './i18n.js';
import { markRead, startClock, wireReread } from './ui.js';

import { mapView } from './map.js';
import { account, admin, alerts, evidence, forecasts, integrity, overview, runs } from './views.js';

const VIEWS = {
  overview,
  map: mapView,
  forecasts,
  alerts,
  runs,
  evidence,
  integrity,
  admin,
  account,
};

/* The rail, in the handoff's order. Eight items and always all eight: the
 * admin row stays for a non-admin, dimmed, and the screen behind it explains
 * what Administration holds and who can open it. A navigation item that
 * vanishes for some people teaches them nothing about what they cannot see.
 *
 * The rail label is not the screen title. "Allocations" fits under a 20px icon
 * at .5625rem; "Stored allocations" does not, and the header says it in full
 * one line to the right. */
const ORDER = ['overview', 'map', 'alerts', 'forecasts', 'runs', 'evidence', 'integrity', 'admin', 'account'];

/* Rail labels come from i18n.t(`nav.${key}`) so the Arabic toggle re-reads them
 * from one dictionary; the English text lives there too, as the fallback. */

const ROLE_LADDER = ['viewer', 'analyst', 'admin'];
const can = (user, role) => ROLE_LADDER.indexOf(user.role) >= ROLE_LADDER.indexOf(role);

const $ = (id) => document.getElementById(id);

const state = { user: null, view: 'overview', viewLifetime: null };

/* -------------------------------------------------------------- appearance */

/* Two buttons, Light and Dark, over three states.
 *
 * Auto has no button any more, but it is still the state a browser starts in, and
 * removing it as the DEFAULT would be a different and much worse change. Auto means
 * "no data-theme attribute", which leaves the stylesheet's prefers-color-scheme
 * query in charge, and that ordering is load-bearing: the CSP forbids inline script,
 * so nothing can run before the stylesheet paints. If the correct appearance
 * depended on JavaScript, every load would flash the wrong one first. Following the
 * system until someone chooses otherwise means the common case is already right by
 * the time this file executes.
 *
 * So the switch is an override rather than a three-way choice. Until it is touched
 * neither button reads as pressed, which is honest - the system is deciding, not the
 * page. Once touched the choice sticks, and it cannot be handed back to the system
 * from the interface; clearing gemp.appearance from local storage is the way back.
 *
 * The two buttons exist because this screen gets demonstrated on other people's
 * projectors, where the right answer is whatever the room can read.
 */
const APPEARANCE_KEY = 'gemp.appearance';
const APPEARANCES = ['auto', 'light', 'dark'];
/* What the switch offers. Auto stays valid, and stays the default; it is simply not
 * something the interface asks anyone to pick. */
const APPEARANCE_CHOICES = ['light', 'dark'];

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
  light: 'Light appearance',
  dark: 'Dark appearance',
};

function appearanceSwitch() {
  const current = storedAppearance();
  const buttons = APPEARANCE_CHOICES.map((key) => `
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

/* ------------------------------------------------------------- text scale */

/* The projector affordance. Three steps, and each one sets the ROOT font size,
 * which every length in the stylesheet is expressed against - so the whole
 * interface grows, map chrome and all. It is the same design at a different
 * size, not a second design for a big room.
 *
 * Writing `documentElement.style.fontSize` is a CSSOM property write, not an
 * inline style attribute in the markup, so `style-src 'self'` does not stop it.
 */
const SCALE_KEY = 'gemp.textScale';
const SCALES = [100, 125, 150];

function storedScale() {
  try {
    const value = Number(window.localStorage.getItem(SCALE_KEY));
    return SCALES.includes(value) ? value : 100;
  } catch {
    return 100;
  }
}

function applyScale(percent) {
  document.documentElement.style.fontSize = `${(16 * percent) / 100}px`;
  try { window.localStorage.setItem(SCALE_KEY, String(percent)); } catch { /* ignore */ }
  document.querySelectorAll('[data-scale]').forEach((button) => {
    button.setAttribute('aria-pressed', String(Number(button.dataset.scale) === percent));
  });
}

function textScaleSwitch() {
  const current = storedScale();
  const buttons = SCALES.map((value) => `
    <button type="button" data-scale="${value}" aria-pressed="${value === current}"
            aria-label="Text size ${value} percent">${value}%</button>`).join('');
  return `<div class="textscale" role="group" aria-label="Text size">${buttons}</div>`;
}

function wireTextScale(container) {
  container.querySelectorAll('[data-scale]').forEach((button) => {
    button.addEventListener('click', () => applyScale(Number(button.dataset.scale)));
  });
}

/* ------------------------------------------------------------------- login */

/* Failed attempts since this page was loaded. The server counts them too and
 * throttles on them; this is only so the screen can say "that is the third
 * attempt" instead of showing the same sentence three times and looking as
 * though nothing happened. */
let attempts = 0;

function showLogin(message = '') {
  document.body.className = 'signed-out';
  /* Signing out from #/admin and back in as a viewer used to land on #/admin,
   * which navigate() silently refuses - leaving the loading state on screen
   * with no way to tell what had gone wrong. */
  if (window.location.hash) {
    window.history.replaceState(null, '', window.location.pathname);
  }

  /* One centred column, and nothing else on the page.
   *
   * There is deliberately NO panel of security properties here. An earlier
   * version listed the session model, the idle timeout, the password hash and
   * the audit log; every one of those was true and pinned by a test, and they
   * still do not belong on a sign-in screen. A hash algorithm named in front of
   * an unauthenticated visitor reassures nobody who could check it and tells
   * anybody who is attacking it which shape to attack. The Administration
   * screen reports all four, to people who have signed in.
   */
  $('root').innerHTML = `
    <div class="login-wrap">
      <header class="login-bar">
        ${textScaleSwitch()}
        ${appearanceSwitch()}
      </header>

      <div class="login-body">
        <div class="login-col">
          <!-- The mark and the sentence are one lockup, centred together. Left
               aligning a 15rem mark inside a 26rem column reads as a mark that
               missed its mark. -->
          <div class="login-lockup">
            <p class="wordmark brand-wordmark">${wordmark('GEMP')}</p>
            <p class="lede">Allocates a fixed budget across fifty public buildings in New
            Cairo, and shows the evidence for every building it chose.</p>
          </div>

          <form class="login panel" id="login-form">
            <h2>Sign in</h2>

            ${message ? `<div class="error-box" role="alert">
              <p>${escapeHtml(message)}</p>
              <p class="small">The server answers the same way whether the password is
              wrong, the account does not exist, or it has been disabled &mdash; so this
              message cannot tell you which, and nor can we.</p>
              <p class="small">${attempts === 1 ? 'That was the first attempt'
                : `That is ${attempts} attempts`} from this browser. Each one is recorded
              with the username tried and the address it came from.</p>
            </div>` : ''}

            <label for="login-user">Username
              <input id="login-user" name="username" type="text" class="mono"
                     autocomplete="username" autocapitalize="none" spellcheck="false"
                     required autofocus></label>

            <label for="login-pass">Password
              <span class="password-field">
                <input id="login-pass" name="password" type="password" class="mono"
                       autocomplete="current-password" required>
                <button type="button" class="reveal" id="login-reveal"
                        aria-pressed="false" aria-label="Show password">
                  ${icon('eye')}
                </button>
              </span>
              <span class="hint" id="reveal-state">The password is hidden.</span>
              <span class="caps-hint" id="caps-hint" role="status">Caps Lock is on.</span>
            </label>

            <!-- Disabled until there is something to send, with a label that says
                 what is missing rather than going silent. -->
            <button type="submit" class="primary" id="login-submit" disabled>
              Enter a username and password</button>
          </form>
        </div>
      </div>

      <footer class="login-foot">
        <span>Accounts are created by an administrator. There is no self-service
        registration, no default account, and no email reset &mdash; a forgotten
        password has to be reset by an administrator in person.</span>
        <span>Team Ecoforge &middot; RoboDam2026</span>
      </footer>
    </div>`;

  wireAppearance($('root'));
  wireTextScale($('root'));
  applyScale(storedScale());

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

  const submit = $('login-submit');
  const ready = () => {
    const filled = $('login-user').value.trim() && $('login-pass').value;
    submit.disabled = !filled;
    submit.textContent = filled ? 'Sign in' : 'Enter a username and password';
  };
  $('login-user').addEventListener('input', ready);
  $('login-pass').addEventListener('input', ready);

  /* A revealed password is readable by the room, and on this project the room
   * is a demonstration hall with a projector. The state is stated, not left to
   * be inferred from an icon. */
  const reveal = $('login-reveal');
  reveal.addEventListener('click', () => {
    const field = $('login-pass');
    const shown = field.type === 'text';
    field.type = shown ? 'password' : 'text';
    reveal.innerHTML = icon(shown ? 'eye' : 'eye-off');
    reveal.setAttribute('aria-pressed', String(!shown));
    reveal.setAttribute('aria-label', shown ? 'Show password' : 'Hide password');
    $('reveal-state').textContent = shown
      ? 'The password is hidden.'
      : 'The password is visible on screen — mind the room.';
    field.focus();
  });

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    submit.disabled = true;
    submit.textContent = 'Signing in…';
    try {
      const result = await api.login($('login-user').value, $('login-pass').value);
      attempts = 0;
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
      attempts += 1;
      showLogin(error.detail || 'Those details were not accepted.');
    }
  });
}

/* The first-login state. Nothing else in GEMP is reachable from here, and the
 * panel says so rather than leaving somebody to discover it by clicking a rail
 * that is not there. */
function showPasswordChange() {
  document.body.className = 'signed-out';
  $('root').innerHTML = `
    <div class="login-wrap">
      <header class="login-bar">
        ${textScaleSwitch()}
        ${appearanceSwitch()}
      </header>

      <div class="login-body">
        <div class="login-col">
          <div class="login-lockup">
            <p class="wordmark brand-wordmark">${wordmark('GEMP')}</p>
          </div>

          <form class="login panel first-login" id="change-form">
            <h2>${icon('lock')}Set a new password before you continue</h2>
            <p class="lede">This account was created with a temporary password, which
            works once. Nothing else in GEMP is reachable until it is replaced — the
            server refuses every other request while the account is in this state.</p>

            <div id="change-error"></div>

            <label for="cp-current">Temporary password
              <input id="cp-current" type="password" class="mono"
                     autocomplete="current-password" required autofocus></label>

            <label for="cp-new">New password
              <input id="cp-new" type="password" class="mono" autocomplete="new-password"
                     minlength="12" required>
              <span class="meter" id="cp-meter" aria-hidden="true"><span></span></span>
              <span class="hint" id="cp-hint">At least 12 characters.</span></label>

            <label for="cp-confirm">Repeat the new password
              <input id="cp-confirm" type="password" class="mono"
                     autocomplete="new-password" required>
              <span class="hint" id="cp-match"></span></label>

            <button type="submit" class="primary" id="cp-submit" disabled>
              Fill in every field</button>
            <p class="caption">There is no email reset. If you lose this password an
            administrator has to set another one and hand it over directly.</p>
          </form>
        </div>
      </div>
    </div>`;

  wireAppearance($('root'));
  wireTextScale($('root'));
  applyScale(storedScale());

  const fresh = $('cp-new');
  const again = $('cp-confirm');
  const current = $('cp-current');
  const submit = $('cp-submit');
  const meter = $('cp-meter');

  /* Validated as it is typed, and before submit. A temporary password is
   * usually being copied from a piece of paper, and finding out on the round
   * trip that the two fields differ means typing both again. */
  const check = () => {
    const value = fresh.value;
    const long = value.length >= 12;
    const matches = again.value !== '' && again.value === value;

    meter.dataset.state = !value ? 'empty' : long ? 'ok' : 'short';
    meter.style.setProperty('--fill', `${Math.min(100, (value.length / 12) * 100)}%`);
    $('cp-hint').textContent = !value ? 'At least 12 characters.'
      : long ? `${value.length} characters — long enough.`
             : `${value.length} of 12 characters.`;
    $('cp-hint').className = `hint ${long ? 'ok' : 'warn'}`;
    $('cp-match').textContent = !again.value ? ''
      : matches ? 'The two match.' : 'The two do not match yet.';
    $('cp-match').className = `hint ${matches ? 'ok' : 'warn'}`;

    const ready = current.value && long && matches;
    submit.disabled = !ready;
    submit.textContent = ready ? 'Set password and sign in'
      : !current.value ? 'Enter the temporary password'
      : !long ? 'The new password is too short'
      : 'The two new passwords must match';
  };

  [current, fresh, again].forEach((field) => field.addEventListener('input', check));
  check();

  $('change-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api.changePassword(current.value, fresh.value);
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

  /* Every item, for everyone. Administration is dimmed rather than dropped when
   * the role cannot open it, and its screen says what it holds and who to ask. */
  const navItems = ORDER.map((key) => {
    const locked = VIEWS[key].requiredRole && !can(state.user, VIEWS[key].requiredRole);
    return `
    <button class="nav-item${locked ? ' locked' : ''}" type="button" data-view="${key}">
      ${icon(key)}<span class="nav-label">${escapeHtml(t(`nav.${key}`))}</span>
    </button>`;
  }).join('');

  /* The language toggle sits with the other room controls rather than buried in
   * Account, because direction is a whole-shell decision: flipping it re-renders
   * the rail immediately and every view re-reads its strings on the gemp:lang
   * event. One button, two states, labelled in BOTH languages so it reads the
   * same to whoever needs it whichever language the screen is currently in. */
  const langButton = `<button type="button" class="secondary" id="lang-toggle"
      aria-label="${currentLang() === 'ar' ? 'Switch to English' : 'التبديل إلى العربية'}">
      ${currentLang() === 'ar' ? 'EN' : 'ع'}</button>`;

  const who = escapeHtml(state.user.display_name || state.user.username);

  $('root').innerHTML = `
    <!-- Eight navigation items stand between the top of the document and the
         content, on every single view change. Without a way past them a keyboard
         user tabs through the whole rail to reach the table they came for, and
         does it again the next time. WCAG 2.4.1 calls this bypassing blocks; it is
         one link and it is only visible when it has focus. -->
    <a class="skip-link" href="#view">${t('shell.skip')}</a>
    <div class="shell">
      <nav class="rail" aria-label="${t('shell.sections')}">
        <div class="rail-logo">${wordmark('GEMP')}</div>
        ${navItems}
        <div class="rail-spacer"></div>
        <!-- What you are, above a hairline. Not a decoration: which of these
              screens will accept an action from you is decided by this word, and
              two of them say so in their own copy. -->
        <div class="rail-foot">
          <div class="rail-identity">
            <span class="rail-who" title="${who}">${who}</span>
            <span class="rail-role">${escapeHtml(state.user.role)}</span>
          </div>
          <button class="rail-signout" type="button" id="sign-out">
            ${icon('signout')}<span>${t('shell.signout')}</span>
          </button>
        </div>
      </nav>

      <div class="workspace">
        <!-- Four zones, identical on all nine screens: what this screen is, the
             question it answers, when its numbers were read, and the two
             controls that decide whether the room can read them.

             The right-hand zone is written ONCE, here, and survives every
             navigation. Views write only into the two slots on the left through
             ui.pageHead(); a clock that were re-rendered per view would reset
             its own age every time somebody changed screen, which is precisely
             the lie it exists to prevent. -->
        <div class="page-head">
          <div class="page-head-text" id="page-head-text"></div>
          <div class="page-head-side">
            <span id="page-head-actions"></span>
            <span class="clock" id="data-clock" role="status">
              ${icon('clock')}
              <span class="clock-text" id="clock-text">Read just now</span>
              <span class="clock-tag" id="clock-tag">fresh</span>
            </span>
            <button type="button" class="secondary" data-refresh>
              ${icon('refresh')}${t('shell.reread')}
            </button>
            ${langButton}
            ${textScaleSwitch()}
            ${appearanceSwitch()}
          </div>
        </div>
        <!-- The two strips live between the header and the content so that
             neither can cover a control that has focus. -->
        <div id="strips"></div>
        <!-- tabindex="-1" so the skip link can actually move focus here. A
             fragment link alone scrolls the page without moving the focus ring,
             so the next Tab press carries on from the rail regardless. -->
        <main id="view" tabindex="-1"></main>
      </div>
    </div>`;

  wireAppearance($('root'));
  wireTextScale($('root'));
  applyScale(storedScale());
  startClock();
  wireReread();

  document.querySelectorAll('[data-view]').forEach((button) => {
    button.addEventListener('click', () => navigate(button.dataset.view));
  });
  $('sign-out').addEventListener('click', async () => {
    await api.logout().catch(() => {});
    state.user = null;
    showLogin();
  });

  /* Language: flip direction and strings immediately, then re-render the screen
   * the reader is on so nothing waits for the next navigation. */
  $('lang-toggle').addEventListener('click', () => {
    setLangCode(currentLang() === 'ar' ? 'en' : 'ar');
  });
  window.addEventListener('gemp:lang', () => {
    navigate(state.view);
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
  /* `requiredRole` dims the rail item; it does not block the navigation. The
   * screen behind it explains what it holds and who can open it, which is the
   * whole reason the item is still in the rail. Refusing to navigate would
   * leave the previous screen on display with a rail item that looks broken. */

  state.view = key;
  window.location.hash = `#/${key}`;

  /* aria-current rather than a class, so the styling and the announcement to a
   * screen reader cannot drift apart: there is one source for both. */
  document.querySelectorAll('.nav-item').forEach((item) => {
    if (item.dataset.view === key) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  /* Cleared here rather than by each view, so a view that throws before it
   * renders cannot leave the previous screen's title above the error - and so
   * that a result strip about the screen you just left ("42 alerts
   * acknowledged") does not follow you to the next one. The clock is not
   * touched: it belongs to the read, not to the screen. */
  $('page-head-text').innerHTML = '';
  $('page-head-actions').innerHTML = '';
  $('result-strip')?.remove();

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

  /* A FRESH element per navigation, not the same one cleared.
   *
   * Aborting the lifetime stops the listeners, but it does not stop a request
   * that is already in flight: the previous screen's fetch resolves a second
   * later and writes its markup into `#view`, which by then belongs to the
   * screen you asked for. The overview did exactly that over the top of the
   * map. Handing each view its own node means a late write lands in a detached
   * element and is simply never seen. */
  const previous = $('view');
  const root = document.createElement('main');
  root.id = 'view';
  root.tabIndex = -1;
  previous.replaceWith(root);
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
    /* The read happened when the view's data arrived, which is here - not when
     * somebody clicked the rail. A view that knows its own data-time stamp says
     * so again with it; this is the floor. */
    markRead();

    const signal = state.viewLifetime.signal;
    hydrateCharts(root, signal);

    /* A view can rebuild itself without the shell knowing - the Refresh button in
     * the workspace header calls the view's own render(). Everything hydrated
     * above is then thrown away and replaced by fresh markup that nothing has
     * connected, so the charts would go back to drawing at a fixed width and the
     * age chip would simply vanish. Watching for that is cheaper than asking
     * every view to remember to announce it. */
    const rehydrate = new MutationObserver(() => hydrateCharts(root, signal));
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
