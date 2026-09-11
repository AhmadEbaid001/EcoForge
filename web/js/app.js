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
import { builtFigure, signInArtwork, stackStrip, stageFigure, wordmark }
  from './brand.js';
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

const APPEARANCE_LABELS = () => ({
  light: t('shell.lightAppearance'),
  dark: t('shell.darkAppearance'),
});

function appearanceSwitch() {
  const current = storedAppearance();
  const labels = APPEARANCE_LABELS();
  const buttons = APPEARANCE_CHOICES.map((key) => `
    <button type="button" data-appearance="${key}" title="${labels[key]}"
            aria-label="${labels[key]}"
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
            aria-label="${t('si.textSizeN', { n: value })}">${value}%</button>`).join('');
  return `<div class="textscale" role="group" aria-label="${t('si.textSize')}">${buttons}</div>`;
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

/* The landing page.
 *
 * A separate page, not a preamble to the form: somebody arriving at this URL for
 * the first time has never heard of GEMP, and somebody arriving for the hundredth
 * time wants the password field. The landing answers the first and carries a
 * Sign in button for the second, which swaps to the form outright.
 *
 * Everything here is fixed copy and drawn geometry. Nothing is fetched. This page
 * renders for a visitor who has not signed in, so a figure describing the
 * deployment's own state - how many rates are cited, how many alerts are open -
 * would be answering the questions the sign-in screen exists to gate. The numbers
 * that ARE here are fixed by the design: fifty buildings, four methods, 720x.
 */
function showLanding() {
  document.body.className = 'signed-out';
  if (window.location.hash) {
    window.history.replaceState(null, '', window.location.pathname);
  }

  const stage = (n, kind, title, body) => `
    <li class="lp-rise">
      <span class="about-step">${n}</span>
      <div><h4 class="plain">${title}</h4><p>${body}</p></div>
      <div class="stage-fig-wrap">${stageFigure(kind)}</div>
    </li>`;

  $('root').innerHTML = `
    <div class="landing-page">
      <header class="lp-nav">
        <a class="lp-brand" href="#top" aria-label="GEMP">${wordmark('GEMP')}</a>
        <nav class="lp-links" aria-label="Sections">
          <a href="#what">What it does</a>
          <a href="#built">How it is built</a>
          <a href="#limits">What it does not claim</a>
        </nav>
        <div class="lp-nav-actions">
          ${appearanceSwitch()}
          <button type="button" class="primary" data-signin>Sign in</button>
        </div>
      </header>

      <section class="lp-hero" id="top">
        <div class="lp-hero-copy lp-enter">
          <p class="lp-badge">Team Ecoforge &middot; RoboDam 2026</p>
          <h1>Fifty public buildings.<br>Measured, not assumed.<br>
            <span class="lp-accent">Evidence for every choice.</span></h1>
          <p class="lp-standfirst">GEMP decides which public buildings across Greater Cairo get
          retrofitted and with what &mdash; measuring each one rather than assuming it,
          proving the measurements were never altered, and solving the allocation
          exactly rather than approximately.</p>
          <div class="lp-cta">
            <button type="button" class="primary" data-signin>Sign in</button>
            <a class="btn secondary" href="#what">See what it does</a>
          </div>
        </div>
        <div class="lp-hero-art lp-enter lp-enter-art">${signInArtwork()}</div>
      </section>

      <!-- Where a commercial site puts customer logos. The numbers that used to
           be here have a section of their own further down, which is where they
           can carry the sentence each of them needs. -->
      <section class="lp-stack" aria-label="Built with">
        <h2 class="sr-only">Built with</h2>
        <div class="stack-viewport">${stackStrip()}</div>
      </section>

      <section class="lp-band" id="what">
        <h2 class="lp-h lp-rise">What it does</h2>
        <ol class="about-stages">
          ${stage('01', 'measures', 'Measures',
            'Half-hourly meter readings arrive over MQTT and are signed as they land. '
            + "Each building's readings form a hash chain, with a copy of the chain head "
            + 'written outside the database volume &mdash; so deleting rows cannot '
            + 'quietly delete the evidence that they existed. Any chain can be re-walked '
            + 'on demand, and the platform will say where it first breaks.')}
          ${stage('02', 'forecasts', 'Forecasts',
            'Every building gets its own model, selected against a seasonal-naive '
            + 'baseline. The baseline is kept and reported whenever it wins, because a '
            + 'building whose load is stable enough that last week predicts this week is '
            + 'telling you something worth knowing rather than failing. The annual figure '
            + 'each retrofit is costed against comes from that model, not from a '
            + 'floor-area rule of thumb.')}
          ${stage('03', 'decides', 'Decides',
            'Every building &times; measure combination is expanded into a costed '
            + 'candidate, and the allocation is solved exactly with CP-SAT under the '
            + 'budget and an optional per-district cap. Three baselines are solved beside '
            + 'it &mdash; plain greedy, greedy with an upgrade pass, and an equal split of '
            + 'the budget &mdash; so the gain is quoted against the status quo rather than '
            + 'asserted.')}
          ${stage('04', 'accounts', 'Accounts for it',
            'Each stored allocation keeps a 64-character hash of the inputs that produced '
            + 'it: same hash, same allocation, years later. It exports as a bill of '
            + 'quantities with a rate and a citation on every line. And every claim the '
            + 'paper makes is re-measured against the running deployment by a harness '
            + 'whose output is a screen rather than a terminal &mdash; including the '
            + 'claims that are currently failing.')}
        </ol>
      </section>

      <section class="lp-band" id="numbers">
        <div class="band-head lp-rise">
          <p class="lp-kicker">Reference figures</p>
          <h2 class="lp-h">Four numbers, and where each one comes from</h2>
          <p class="lp-sub">None of these is a target or an average. Each is a
          property of how the platform is built, which is why it can be quoted
          without a footnote.</p>
        </div>
        <dl class="num-strip lp-rise-group">
          <div class="num-cell">
            <dt class="num-key">Public buildings</dt>
            <dd class="num-body">
              <span class="num-val"><span class="num-digits d2">50</span></span>
              <span class="num-note">Each costed from its own metered consumption
              rather than from a floor-area rule of thumb.</span>
              <span class="num-src">data/buildings.geojson</span>
            </dd>
          </div>
          <div class="num-cell">
            <dt class="num-key">Allocation methods</dt>
            <dd class="num-body">
              <span class="num-val"><span class="num-digits d1">4</span></span>
              <span class="num-note">Solved side by side on identical inputs, the
              status quo among them, so the recommendation has something to
              beat.</span>
              <span class="num-src">optimize.SOLVERS</span>
            </dd>
          </div>
          <div class="num-cell">
            <dt class="num-key">Input hash</dt>
            <dd class="num-body">
              <span class="num-val"><span class="num-digits d2">64</span><span
                class="num-unit">chars</span></span>
              <span class="num-note">Stored with every allocation, so a run can be
              tied to the exact data it was solved against.</span>
              <span class="num-src">sha256 &middot; services.inputs_hash</span>
            </dd>
          </div>
          <div class="num-cell">
            <dt class="num-key">Default replay speed</dt>
            <dd class="num-body">
              <span class="num-val"><span class="num-digits d3">720</span><span
                class="num-mult">&times;</span></span>
              <span class="num-note">The shipped rate at which the simulator replays
              stored history to reach the present; each deployment sets its own. It
              is clamped there: once level, the clock advances at real time and can
              never run into the future.</span>
              <span class="num-src">GEMP_SIM_SPEED</span>
            </dd>
          </div>
        </dl>
      </section>

      <section class="lp-band lp-band-alt" id="built">
        <div class="band-head lp-rise">
          <h2 class="lp-h">How it is built</h2>
          <p class="lp-sub">Four decisions taken early, each of which closed off an
          easier option. They are the reason the platform behaves the way it does in
          a room with no network and a reviewer asking where a number came from.</p>
        </div>
        <div class="built-grid lp-rise-group">
          <article class="built-card">
            <div class="built-fig">${builtFigure('offline')}</div>
            <div class="built-body">
              <h3>It works with the network unplugged</h3>
              <p>No CDN, no web fonts, no tile server, no charting library. The
              allocation map is SVG over local geometry - satellite imagery included,
              committed as a file rather than fetched - and every chart is drawn by
              hand, so a demonstration does not depend on conference wifi.</p>
            </div>
          </article>
          <article class="built-card">
            <div class="built-fig">${builtFigure('roles')}</div>
            <div class="built-body">
              <h3>Three roles, and refusals are recorded</h3>
              <p>Viewers read, analysts solve and acknowledge, administrators manage
              accounts. Every authenticated action is written to an audit log
              <em>including the ones that were denied</em>, which is the half most
              audit logs leave out.</p>
            </div>
          </article>
          <article class="built-card">
            <div class="built-fig">${builtFigure('reproducible')}</div>
            <div class="built-body">
              <h3>Reproducible by construction</h3>
              <p>Candidate expansion is deterministic and the optimizer is exact, so
              the same inputs give the same allocation on any machine. That is a
              measured claim rather than an aspiration &mdash; the harness checks
              it.</p>
            </div>
          </article>
          <article class="built-card">
            <div class="built-fig">${builtFigure('arguable')}</div>
            <div class="built-body">
              <h3>Built to be argued with</h3>
              <p>Every recommendation shows the options that lost, not only the one
              that won. An answer a reviewer cannot interrogate is an answer they are
              being asked to take on trust.</p>
            </div>
          </article>
        </div>
      </section>

      <section class="lp-band lp-band-dark" id="limits">
        <div class="limits-layout">
          <div class="limits-head lp-rise">
            <p class="lp-kicker">Stated in place &middot; collected here</p>
            <h2 class="lp-h">What it does not claim</h2>
            <p class="lp-sub">Every screen says these where somebody could be misled
            by not knowing them. They are gathered here so none of them is a
            surprise.</p>
            <p class="limits-count"><span>05</span> limitations</p>
          </div>
          <ol class="limit-list lp-rise-group">
            <li>
              <span class="limit-n">01</span>
              <div class="limit-body">
                <h3>Lifetime carbon is an estimate</h3>
                <p class="limit-where">Stored allocations</p>
                <p>A projection over the horizon in the parameters, not a measurement
                of anything that has happened.</p>
              </div>
            </li>
            <li>
              <span class="limit-n">02</span>
              <div class="limit-body">
                <h3>Integrity is not accuracy</h3>
                <p class="limit-where">Reading integrity</p>
                <p>A verified chain says nobody altered what the meter sent. Whether
                the meter itself behaved is a separate question, and one the alert
                inbox answers.</p>
              </div>
            </li>
            <li>
              <span class="limit-n">03</span>
              <div class="limit-body">
                <h3>No per-building error figure</h3>
                <p class="limit-where">Load forecasting</p>
                <p>The platform reports which model was selected and draws both
                lines. It does not publish a per-building error metric, so it does
                not invent one.</p>
              </div>
            </li>
            <li>
              <span class="limit-n">04</span>
              <div class="limit-body">
                <h3>One input is still a placeholder</h3>
                <p class="limit-where">TOU objective &middot; BOQ</p>
                <p>Every costing rate now names a source. The 24-hour marginal emission
                profile behind the time-of-use objective does not, and is marked as a
                placeholder wherever it appears. Rates that lose a source are labelled
                on the printed bill of quantities.</p>
              </div>
            </li>
            <li>
              <span class="limit-n">05</span>
              <div class="limit-body">
                <h3>The data is simulated</h3>
                <p class="limit-where">Portfolio overview</p>
                <p>Fifty real buildings, a real street network and a real catalog
                structure, driven by a simulator rather than by fifty real meters.</p>
              </div>
            </li>
          </ol>
        </div>
      </section>

      <section class="lp-close lp-rise">
        <h2>Sign in to open the platform.</h2>
        <p>Accounts are issued by an administrator. There is no self-service
        registration and no default account.</p>
        <button type="button" class="primary" data-signin>Sign in</button>
      </section>

      <footer class="lp-foot">
        <span>&copy; ${new Date().getFullYear()} Team Ecoforge. All rights reserved.</span>
      </footer>
    </div>`;

  wireAppearance($('root'));
  $('root').querySelectorAll('[data-signin]').forEach((button) => {
    button.addEventListener('click', () => showLogin());
  });

  /* Motion is an enhancement, never a gate.
   *
   * Everything below starts visible in the stylesheet and is only hidden once
   * this script has confirmed it can bring it back - so a reader with no
   * JavaScript, or one who has asked their system for less motion, gets the
   * whole page at once rather than a column of blank sections that never
   * arrive. `prefers-reduced-motion` is honoured by not arming any of it. */
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const root = $('root');
  const nav = root.querySelector('.lp-nav');

  if (!still) {
    root.classList.add('lp-animate');

    /* A group staggers its own children, so the strip of four figures arrives as
     * four things rather than as one block. */
    root.querySelectorAll('.lp-rise-group').forEach((group) => {
      [...group.children].forEach((child, i) => {
        child.classList.add('lp-rise', `d${Math.min(i, 5)}`);
      });
    });
  }

  /* A figure that counts up to itself when it arrives.
   *
   * The constraint this is built around: it must never leave a number on screen
   * that is not the real one. Two things follow from that, and the first draft
   * of this had neither.
   *
   * It does not start from zero. A count that begins at nothing spends its first
   * frames displaying a figure that is flatly wrong, and it changes digit count
   * on the way up. Starting a little over half way keeps every intermediate the
   * same width as the answer and within sight of it, so a frame caught mid-count
   * reads as a value settling rather than as a different claim.
   *
   * And it gives up the moment the frames stop arriving on time. Under a
   * throttled renderer - a background tab, a window behind another - the gap
   * between frames stretched to a third of a second, and whatever was painted
   * stayed on screen for all of it. So a late frame is treated as the animation
   * being over: write the real figure and stop. The reader loses an effect,
   * which is the cheaper of the two things that could be lost.
   *
   * The digits also keep a fixed width in `ch`, declared in the markup, so the
   * multiplication sign beside `720` has nothing to walk left into.
   */
  const COUNT_MS = 900;
  const STALL_MS = 90;   /* about five dropped frames */
  const FROM = 0.55;

  const countUp = (el) => {
    const final = el.textContent;
    const target = Number(final.replace(/[^0-9]/g, ''));
    if (!Number.isFinite(target) || target < 4) return;

    const first = Math.round(target * FROM);
    let started = null;
    let previous = null;

    const frame = (now) => {
      if (started === null) { started = now; previous = now; }
      const late = now - previous > STALL_MS;
      previous = now;

      const t = Math.min(1, (now - started) / COUNT_MS);
      if (t >= 1 || late) { el.textContent = final; return; }

      /* Decelerating, because a figure that arrives at speed and stops dead
         reads as a glitch rather than as a measurement settling. */
      const eased = 1 - ((1 - t) ** 3);
      el.textContent = String(Math.round(first + (target - first) * eased));
      requestAnimationFrame(frame);
    };

    requestAnimationFrame(frame);
  };

  /* Called for each element the pass below reveals, so the counters are driven
     by the same rectangle check as everything else rather than by a second
     mechanism that could disagree with it. */
  const onRevealed = (el) => {
    if (still) return;
    el.querySelectorAll('.num-digits').forEach(countUp);
  };

  /* Position-checked on scroll rather than observed.
   *
   * An IntersectionObserver is the tidier tool and it was the first thing here,
   * but it has one failure mode this page cannot afford: every animated element
   * starts at `opacity: 0`, so if the callback never arrives - a tab that was
   * never composited, a renderer that suppressed it - the visitor gets a blank
   * page rather than a page without an effect. A rectangle check cannot fail
   * that way, it costs one pass over twenty-one elements, and it is throttled to
   * a frame. */
  const rising = [...root.querySelectorAll('.lp-rise')];
  let last = 0;

  const paint = () => {
    last = Date.now();
    if (nav) nav.classList.toggle('is-stuck', window.scrollY > 8);
    if (still) return;
    for (let i = rising.length - 1; i >= 0; i -= 1) {
      const box = rising[i].getBoundingClientRect();
      /* Anything at or above the fold line, including what has already been
       * scrolled past: a reader who follows a nav anchor jumps over whole
       * sections, and those must not be left invisible behind them. */
      if (box.top < window.innerHeight * 0.92) {
        const el = rising[i];
        el.classList.add('is-in');
        rising.splice(i, 1);
        onRevealed(el);
      }
    }
  };

  /* Throttled on a clock rather than on a frame. `requestAnimationFrame` does not
   * run while a page is not being painted - a background tab, a window behind
   * another - so a reader returning to a tab they had parked would find the
   * sections still hidden until they happened to scroll again. Twenty-one
   * rectangle reads every sixtieth of a second is not worth that risk. */
  const onScroll = () => {
    if (Date.now() - last < 60) return;
    paint();
  };

  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll, { passive: true });
  paint();
}

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
    <div class="login-wrap is-signin">
      <header class="login-bar">
        ${textScaleSwitch()}
        ${appearanceSwitch()}
        <!-- The shell's toggle lives behind the sign-in form, which is one
             screen too late: somebody who reads Arabic meets this page first. -->
        <button type="button" class="secondary" id="signin-lang"
                lang="${currentLang() === 'ar' ? 'en' : 'ar'}"
                >${currentLang() === 'ar' ? 'EN' : '\u0639'}</button>
      </header>

      <!-- One card on the brand field, split: the way in, and what this is.

           Everything on the scene side is fixed copy and drawn geometry. Nothing
           is fetched and nothing describes the portfolio - an unauthenticated
           visitor is told what the platform is FOR, never what it currently
           holds. An earlier version of this screen listed the session model and
           the password hash; naming a hash algorithm in front of an attacker
           helps only the attacker. -->
      <div class="login-card">
        <div class="login-col">
          <div class="login-lockup">
            <p class="wordmark brand-wordmark">${wordmark('GEMP')}</p>
          </div>

          <form class="login" id="login-form">
            <h2>${t('si.signIn')}</h2>
            ${message ? `<div class="error-box" role="alert">
              <p>${escapeHtml(message)}</p>
              <p class="small">${t('si.sameAnswer')}</p>
              <p class="small">${attempts === 1
                ? t('si.firstAttempt')
                : t('si.nAttempts', { n: attempts })}${t('si.attemptsTail')}</p>
            </div>` : ''}

            <label for="login-user">${t('si.username')}
              <input id="login-user" name="username" type="text" class="mono"
                     autocomplete="username" autocapitalize="none" spellcheck="false"
                     required autofocus></label>

            <label for="login-pass">${t('si.password')}
              <span class="password-field">
                <input id="login-pass" name="password" type="password" class="mono"
                       autocomplete="current-password" required>
                <button type="button" class="reveal" id="login-reveal"
                        aria-pressed="false" aria-label="${t('si.showPassword')}">
                  ${icon('eye')}
                </button>
              </span>
              <span class="hint" id="reveal-state">${t('si.passwordHidden')}</span>
              <span class="caps-hint" id="caps-hint" role="status">${t('si.capsLock')}</span>
            </label>

            <!-- Disabled until there is something to send, with a label that says
                 what is missing rather than going silent. -->
            <button type="submit" class="primary" id="login-submit" disabled>
              ${t('si.needBoth')}</button>
          </form>

          <p class="login-note">${t('si.noRegistration')}</p>

          <footer class="login-foot">
            <button type="button" class="linkish" data-to-landing>${t('si.whatThisIs')}</button>
            <span>${t('si.team')}</span>
          </footer>
        </div>

        <aside class="login-scene" aria-labelledby="signin-lede">
          <div class="login-scene-art">${signInArtwork()}</div>
          <div class="login-scene-body">
            <p class="lede" id="signin-lede">${t('si.lede')}</p>
            <ul class="login-points">
              <li>${icon('integrity')}<span>${t('si.pointIntegrity')}</span></li>
              <li>${icon('table')}<span>${t('si.pointInputs')}</span></li>
              <li>${icon('check')}<span>${t('si.pointLost')}</span></li>
            </ul>
          </div>
        </aside>
      </div>

    </div>`;

  wireAppearance($('root'));
  wireTextScale($('root'));
  applyScale(storedScale());

  $('root').querySelector('[data-to-landing]')?.addEventListener('click', () => showLanding());

  /* Re-rendering the screen is the whole repaint: the form is empty at this
     point, so nothing a reader typed can be lost by it. */
  $('signin-lang')?.addEventListener('click', () => {
    setLangCode(currentLang() === 'ar' ? 'en' : 'ar');
    showLogin(message);
  });

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
    submit.textContent = filled ? t('si.signIn') : t('si.needBoth');
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
    reveal.setAttribute('aria-label', shown ? t('si.showPassword') : t('si.hidePassword'));
    $('reveal-state').textContent = shown ? t('si.passwordHidden') : t('si.passwordShown');
    field.focus();
  });

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    submit.disabled = true;
    submit.textContent = t('si.signingIn');
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
        <button type="button" class="secondary" id="firstlogin-lang"
                lang="${currentLang() === 'ar' ? 'en' : 'ar'}"
                >${currentLang() === 'ar' ? 'EN' : '\u0639'}</button>
      </header>

      <div class="login-body">
        <div class="login-col">
          <div class="login-lockup">
            <p class="wordmark brand-wordmark">${wordmark('GEMP')}</p>
          </div>

          <form class="login panel first-login" id="change-form">
            <h2>${icon('lock')}${t('fl.title')}</h2>
            <p class="lede">${t('fl.lede')}</p>

            <div id="change-error"></div>

            <label for="cp-current">${t('fl.temporary')}
              <input id="cp-current" type="password" class="mono"
                     autocomplete="current-password" required autofocus></label>

            <label for="cp-new">${t('fl.new')}
              <input id="cp-new" type="password" class="mono" autocomplete="new-password"
                     minlength="12" required>
              <span class="meter" id="cp-meter" aria-hidden="true"><span></span></span>
              <span class="hint" id="cp-hint">${t('fl.minChars', { n: 12 })}</span></label>

            <label for="cp-confirm">${t('fl.repeat')}
              <input id="cp-confirm" type="password" class="mono"
                     autocomplete="new-password" required>
              <span class="hint" id="cp-match"></span></label>

            <button type="submit" class="primary" id="cp-submit" disabled>
              ${t('fl.fillAll')}</button>
            <p class="caption">${t('fl.noEmailReset')}</p>
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
    $('cp-hint').textContent = !value ? t('fl.minChars', { n: 12 })
      : long ? t('fl.longEnough', { n: value.length })
             : t('fl.ofChars', { n: value.length, min: 12 });
    $('cp-hint').className = `hint ${long ? 'ok' : 'warn'}`;
    $('cp-match').textContent = !again.value ? ''
      : matches ? t('fl.match') : t('fl.noMatch');
    $('cp-match').className = `hint ${matches ? 'ok' : 'warn'}`;

    const ready = current.value && long && matches;
    submit.disabled = !ready;
    submit.textContent = ready ? t('fl.submit')
      : !current.value ? t('fl.needTemporary')
      : !long ? t('fl.tooShort')
      : t('fl.mustMatch');
  };

  [current, fresh, again].forEach((field) => field.addEventListener('input', check));
  check();

  /* Nothing typed here survives a language switch either, and for the same
     reason: the fields are empty when the screen opens. */
  $('firstlogin-lang')?.addEventListener('click', () => {
    setLangCode(currentLang() === 'ar' ? 'en' : 'ar');
    showPasswordChange();
  });

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

/* On a phone the rail is a bar across the foot of the screen and eight labelled
 * destinations are wider than 375px, so it scrolls. A navigation bar that opens
 * showing stops four through seven, with the one you are on somewhere off to the
 * left, is a navigation bar that has to be searched before it can be used.
 *
 * `scrollLeft` rather than `scrollIntoView`: the bar is `position: fixed`, and
 * scrollIntoView walks every scrollable ancestor, so it also scrolls the page
 * behind it to a place nobody asked to be. This moves one element and nothing
 * else. On a desktop the rail is a column that never overflows, so the guard is
 * false and this does nothing at all.
 */
function centreCurrentNavItem() {
  const rail = document.querySelector('.rail');
  const current = rail?.querySelector('.nav-item[aria-current="page"]');
  if (!rail || !current || rail.scrollWidth <= rail.clientWidth) return;
  rail.scrollLeft = current.offsetLeft - (rail.clientWidth - current.offsetWidth) / 2;
}

/* Seven destinations in a row of top tabs was already crowding the header at
 * 1180px, and it left nowhere to put a page title. A fixed sidebar gives each
 * destination a stable position, room for a label AND an icon, and hands the
 * whole top of the workspace back to the screen you are actually on.
 *
 * Below 768px it is not a sidebar at all: the stylesheet turns it into a fixed
 * bar across the foot of the screen, which is where a thumb is. It keeps all
 * nine labels there too - an icon-only bar is a rebus, and this rail exists
 * because a judge with ten minutes should not have to learn one. On a phone
 * nine labels plus sign out are wider than the screen, so the bar scrolls
 * sideways and `centreCurrentNavItem` below keeps the current stop in it. */
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
  const langButton = '<button type="button" class="secondary" id="lang-toggle"></button>';

  const who = escapeHtml(state.user.display_name || state.user.username);

  $('root').innerHTML = `
    <!-- Nine navigation items stand between the top of the document and the
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
            <span class="rail-role">${escapeHtml(t(`role.${state.user.role}`))}</span>
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
              <span class="clock-text" id="clock-text">${t('ui.readJustNow')}</span>
              <span class="clock-tag" id="clock-tag">${t('ui.fresh')}</span>
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
  /* Everything in the shell that is a WORD rather than a structure, re-read from
   * the catalogue.
   *
   * The shell was rendered once and never again, while `gemp:lang` only
   * re-navigated the view. So the rail, the sign-out button and the language
   * toggle itself kept whatever language they were built in - the toggle sat
   * there reading "ع" after the screen had already switched to Arabic, which
   * is the clearest possible way to tell a reader the feature does not work.
   *
   * Repainting in place rather than re-rendering the shell keeps focus, scroll
   * position and the data clock's own age, none of which should reset because
   * somebody changed language. */
  const paintShellStrings = () => {
    const ar = currentLang() === 'ar';
    const root = $('root');

    const skip = root.querySelector('.skip-link');
    if (skip) skip.textContent = t('shell.skip');

    const rail = root.querySelector('.rail');
    if (rail) rail.setAttribute('aria-label', t('shell.sections'));

    root.querySelectorAll('[data-view]').forEach((button) => {
      const label = button.querySelector('.nav-label');
      if (label) label.textContent = t(`nav.${button.dataset.view}`);
    });

    const out = root.querySelector('#sign-out span');
    if (out) out.textContent = t('shell.signout');

    const role = root.querySelector('.rail-role');
    if (role) role.textContent = t(`role.${state.user.role}`);

    const reread = root.querySelector('[data-refresh]');
    if (reread) {
      /* The icon is the first child and has to survive; only the text node after
         it carries the word. */
      const text = [...reread.childNodes]
        .find((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
      if (text) text.textContent = t('shell.reread');
      else reread.append(t('shell.reread'));
    }

    const toggle = $('lang-toggle');
    if (toggle) {
      /* The button names the language it will switch TO, written in that
         language, so it is legible to somebody who cannot read the language
         currently on the screen. */
      toggle.textContent = ar ? 'EN' : '\u0639';
      toggle.setAttribute('lang', ar ? 'en' : 'ar');
      const label = ar ? 'Switch to English' : '\u0627\u0644\u062a\u0628\u062f\u064a\u0644 \u0625\u0644\u0649 \u0627\u0644\u0639\u0631\u0628\u064a\u0629';
      toggle.setAttribute('aria-label', label);
      toggle.title = label;
    }
  };

  paintShellStrings();
  startClock();
  wireReread();

  document.querySelectorAll('[data-view]').forEach((button) => {
    button.addEventListener('click', () => navigate(button.dataset.view));
  });
  $('sign-out').addEventListener('click', async () => {
    await api.logout().catch(() => {});
    state.user = null;
    /* Out to the landing page, not to the form. Signing out is a decision to
     * leave, and the front door is where leaving puts you - the same place the
     * link puts a visitor who has never signed in. A form with nothing above it
     * is where the session ENDED handler sends people, because that is the case
     * where somebody was in the middle of something and needs to get back. */
    showLanding();
  });

  /* Language: flip direction and strings immediately, then re-render the screen
   * the reader is on so nothing waits for the next navigation. */
  $('lang-toggle').addEventListener('click', () => {
    setLangCode(currentLang() === 'ar' ? 'en' : 'ar');
  });
  window.addEventListener('gemp:lang', () => {
    paintShellStrings();
    navigate(state.view);
  });

  const opening = splitHash();
  await navigate(opening.key || 'overview', opening.key ? opening.search : '');
}

/* `#/alerts?building=b012` - a screen key, and the state the screen should open
 * in. Deep links matter here because the product's screens ask about each other:
 * integrity finds a broken chain and the next question is "what did the alert
 * inbox say about this building", which is not a useful link if it lands on an
 * unfiltered inbox of thirty thousand rows. */
function splitHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const cut = raw.indexOf('?');
  const key = cut >= 0 ? raw.slice(0, cut) : raw;
  return { key: VIEWS[key] ? key : null, search: cut >= 0 ? raw.slice(cut + 1) : '' };
}

function fromHash() {
  return splitHash().key;
}

async function navigate(key, search = '') {
  const view = VIEWS[key];
  if (!view) return;
  /* `requiredRole` dims the rail item; it does not block the navigation. The
   * screen behind it explains what it holds and who can open it, which is the
   * whole reason the item is still in the rail. Refusing to navigate would
   * leave the previous screen on display with a rail item that looks broken. */

  state.view = key;
  state.search = search;
  window.location.hash = `#/${key}${search ? `?${search}` : ''}`;

  /* aria-current rather than a class, so the styling and the announcement to a
   * screen reader cannot drift apart: there is one source for both. */
  document.querySelectorAll('.nav-item').forEach((item) => {
    if (item.dataset.view === key) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  centreCurrentNavItem();
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
   * A view that listens on `window` - the map does, for resize, pointermove,
   * pointerup and pointercancel - leaves those behind on every mount, so five
   * visits to the Map
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
      /* Read-only, and empty for a screen opened from the rail. */
      params: new URLSearchParams(search),
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
  const { key, search } = splitHash();
  /* The search is part of the address: arriving at the inbox filtered to one
   * building from an inbox that is already open is a different screen, and
   * comparing the key alone would have ignored it. */
  if (state.user && key && (key !== state.view || search !== (state.search || ''))) {
    navigate(key, search);
  }
});

async function boot() {
  applyAppearance(storedAppearance());
  try {
    const session = await api.session();
    if (!session.authenticated) {
      /* First contact is the landing page. A session that EXPIRED goes straight
       * to the form instead - see the unauthenticated handler - because somebody
       * who was working does not need to be told what the product is. */
      showLanding();
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
