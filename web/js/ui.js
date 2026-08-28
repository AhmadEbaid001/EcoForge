/* The pieces every view needed and none of them had.
 *
 * Before this module each view opened with a bare panel: no title beyond the
 * highlighted tab, no statement of what the screen was for, a one-line spinner
 * that resized the page when it was replaced, tables that could not be sorted,
 * and successful actions that produced no visible result at all. Seven views
 * each solving that differently is seven chances to solve it badly, so the
 * answers live here.
 *
 * Two deliberate choices worth not relitigating:
 *
 * - Confirmations are INLINE, not floating toasts. A fixed toast can cover the
 *   control that has keyboard focus, and one that dismisses itself on a timer
 *   is unreadable for anyone who needs longer than six seconds. A status slot at
 *   the top of the view stays until it is replaced or dismissed.
 *
 * - Loading states are skeletons shaped like the content they stand in for, so
 *   the page does not jump when data arrives. The spinner they replaced was one
 *   line tall and everything below it moved.
 */

'use strict';

import { api } from './api.js';

import { escapeHtml, icon } from './charts.js';

/* --------------------------------------------------------------- page header */

const $ = (id) => document.getElementById(id);

/* Writes the two left-hand zones of the workspace header. The two on the right -
 * the data clock with its Re-read button, and the text-size and theme controls -
 * are written once by the shell and deliberately NOT re-rendered here: a clock
 * that started again on every navigation would reset the very age it exists to
 * report.
 *
 * h1 is the wordmark in the rail, so a screen title is an h2 and a panel heading
 * is an h3. Screen readers navigate by heading level; skipping one makes the
 * outline lie about the structure.
 */
export function pageHead({ root, title, description, descriptionHtml, actions = '' }) {
  /* A view that has been navigated away from can still be waiting on a request.
   * When it lands it rebuilds its own markup - and its own page header with it -
   * over the top of the screen you actually asked for. Its root node has been
   * detached by then, which is the one reliable signal that this call belongs to
   * a screen nobody is looking at. */
  if (root && !root.isConnected) return '';

  const text = $('page-head-text');
  if (text) {
    /* `description` is escaped. It reads like prose and it is the parameter anyone
     * adding a screen will reach for, so it must be the safe one - a purpose
     * sentence that one day interpolates a building name or a username would
     * otherwise be an injection point that looks like a caption.
     *
     * `descriptionHtml` is the deliberate exception, for the one sentence that
     * carries a link into another screen. Naming it differently is the whole
     * protection: you cannot pass markup by accident. */
    text.innerHTML = `
      <h2>${escapeHtml(title)}</h2>
      ${descriptionHtml
        ? `<p>${descriptionHtml}</p>`
        : description ? `<p>${escapeHtml(description)}</p>` : ''}`;
  }
  const slot = $('page-head-actions');
  if (slot) slot.innerHTML = actions;
  /* Returned for the views that still place their own status line inline; the
   * result strip proper lives under the header, in #strips. */
  return '';
}

/* ------------------------------------------------------------- data clock */

/* One clock for the whole read, in the header, ticking every second.
 *
 * The simulator runs at 720x real time, so a screen left open for forty-five
 * real seconds is showing nine hours of data time that has already gone past.
 * That is not a property of any one panel, which is why there are no per-panel
 * freshness badges: it is a property of the read.
 *
 * Nothing polls. A dashboard that re-fetches on a timer during a demonstration
 * competes for the same solver the person is dragging a slider against, and it
 * moves numbers under someone who is talking about them. What this does instead
 * is age the stamp honestly - the reading is from then, this is how long ago
 * that was in the unit that matters, and Re-read is right there as a real button
 * rather than an item in a menu.
 *
 * Numbers are never blanked when they go stale. They were true when they were
 * read, and the strip says exactly how long ago that was.
 */
const STALE_AFTER_S = 45;
const SIM_SPEED = 720;

const clock = { readAt: Date.now(), dataClock: null, timer: null };

/* Data time, in the unit a person would use: minutes under an hour, hours under
 * two days, then days. */
function dataAge(realSeconds) {
  const minutes = Math.round((realSeconds * SIM_SPEED) / 60);
  if (minutes < 60) return `${minutes} minutes`;
  if (minutes < 2880) return `${(minutes / 60).toFixed(1)} hours`;
  return `${Math.round(minutes / 1440)} days`;
}

function tickClock() {
  const box = $('data-clock');
  if (!box) return;

  const seconds = Math.round((Date.now() - clock.readAt) / 1000);
  const stale = seconds > STALE_AFTER_S;

  $('clock-text').textContent = `Read at ${new Date(clock.readAt).toLocaleTimeString('en-GB')}`
    + ` · ${seconds < 60 ? `${seconds}s ago` : `${Math.round(seconds / 60)}m ago`}`;
  $('clock-tag').textContent = stale ? 'stale' : 'fresh';
  box.classList.toggle('stale', stale);
  /* The data clock proper - the simulated timestamp the readings carry - is a
   * different quantity from when the browser read them, and only one of the two
   * earns a place in the header at 0.75rem. The other is here. */
  box.title = clock.dataClock
    ? `Data time of this read: ${String(clock.dataClock).replace('T', ' ').slice(0, 19)}`
    : 'Data time, not wall-clock time: the simulator runs at 720x real time';

  const host = $('strips');
  if (!host) return;
  const existing = $('stale-strip');
  if (!stale) { existing?.remove(); return; }

  const message = `<strong>Numbers are stale.</strong> Read ${dataAge(seconds)} of data time
    ago — the simulator runs at 720× real time. Nothing here has moved since, so every
    figure below describes the portfolio as it was at that moment.`;

  if (existing) {
    existing.querySelector('.strip-text').innerHTML = message;
    return;
  }
  const strip = document.createElement('div');
  strip.className = 'strip stale';
  strip.id = 'stale-strip';
  strip.setAttribute('role', 'alert');
  strip.innerHTML = `
    ${icon('warning')}
    <span class="strip-text">${message}</span>
    <span class="strip-act">
      <button type="button" class="amber-outline" data-refresh>Re-read now</button>
    </span>`;
  host.prepend(strip);
}

/* Called by a view once its data is in, so that the clock ages from the moment
 * the screen was actually read rather than the moment somebody asked for it. */
export function markRead(dataClockIso) {
  clock.readAt = Date.now();
  if (dataClockIso) clock.dataClock = dataClockIso;
  tickClock();
}

export function startClock() {
  window.clearInterval(clock.timer);
  clock.timer = window.setInterval(tickClock, 1000);
  tickClock();
}

/* Re-read exists twice - in the header and in the stale strip - and the strip
 * is built after the fact, so binding the buttons directly would miss it. One
 * delegated listener on the document, and each view registers what re-reading
 * means for it. */
let rereadHandler = null;
/* The listener is on `document`, so it outlives every screen - and `showApp()`
 * runs again on each sign-in and after a forced password change. Binding
 * unconditionally added a second listener on the second sign-in of a page load,
 * and Re-read then fired the view's reload twice: two sets of requests racing
 * to write into the same screen. Bound once, like the clock's interval. */
let rereadBound = false;

export const onReread = (fn) => { rereadHandler = fn; };

export function wireReread() {
  if (rereadBound) return;
  rereadBound = true;
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-refresh]')) rereadHandler?.();
  });
}

/* ------------------------------------------------------------------- status */

/* One atomic sentence, in the view's own status slot. "Closed 34 alerts" rather
 * than a bare number, because a screen reader announcing "34" tells nobody
 * what happened. */
export function setStatus(root, { kind = 'ok', message, actionLabel = '', actionAttr = '' } = {}) {
  const host = $('strips') || root.querySelector('[data-status]');
  if (!host) return null;

  document.getElementById('result-strip')?.remove();
  if (!message) return null;

  const strip = document.createElement('div');
  strip.className = `strip ${kind === 'error' ? 'failed' : 'ok'}`;
  strip.id = 'result-strip';
  strip.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  strip.innerHTML = `
    ${icon(kind === 'error' ? 'warning' : 'check')}
    <span class="strip-text">${escapeHtml(message)}</span>
    <span class="strip-act">
      ${actionLabel
        ? `<button type="button" class="undo" ${actionAttr}>${escapeHtml(actionLabel)}</button>`
        : ''}
      <button type="button" class="secondary" data-dismiss>Dismiss</button>
    </span>`;
  strip.querySelector('[data-dismiss]').addEventListener('click', () => strip.remove());
  host.append(strip);
  return strip;
}

export const clearStatus = (root) => setStatus(root, {});

/* ---------------------------------------------------------------- skeletons */

/* Sized like the thing that replaces it. A skeleton that is the wrong height is
 * a layout shift with extra steps. */
export const skeletonTiles = (n = 4) =>
  `<div class="stat-row" aria-busy="true">${
    '<div class="stat skeleton-tile"><span class="skeleton skeleton-figure"></span>' +
    '<span class="skeleton skeleton-line"></span></div>'.repeat(n)}</div>`;

export const skeletonChart = () =>
  `<div class="skeleton skeleton-chart" aria-busy="true" aria-label="Loading chart"></div>`;

export const skeletonRows = (n = 8) =>
  `<div class="skeleton-table" aria-busy="true" aria-label="Loading table">${
    '<span class="skeleton skeleton-row"></span>'.repeat(n)}</div>`;

/* --------------------------------------------------------------- empty state */

/* "No allocations stored yet." on its own tells someone the screen is not
 * broken and nothing else. An empty state should say what would put something
 * here. */
export function emptyState({ title, body, hint = '' }) {
  return `
    <div class="empty-state">
      <h4>${escapeHtml(title)}</h4>
      <p>${escapeHtml(body)}</p>
      ${hint ? `<p class="mono small">${escapeHtml(hint)}</p>` : ''}
    </div>`;
}

/* -------------------------------------------------------------------- tables */

/* One table renderer for all six tables.
 *
 * `columns` is [{ key, label, num, render, sortable, width }]. Sorting is
 * client-side over the rows already loaded and says so, because the API returns
 * a capped slice: sorting 100 of 31,000 rows and calling it "the worst" would be
 * a lie the interface tells on its own.
 */
export function dataTable({ columns, rows, sortKey, sortDir = 'desc', selectable = false,
                            rowLabel }) {
  const sorted = sortKey ? [...rows].sort((a, b) => {
    const column = columns.find((c) => c.key === sortKey);
    const pick = column?.value || ((row) => row[sortKey]);
    const av = pick(a), bv = pick(b);
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    const order = typeof av === 'number' && typeof bv === 'number'
      ? av - bv : String(av).localeCompare(String(bv));
    return sortDir === 'asc' ? order : -order;
  }) : rows;

  const head = columns.map((c) => {
    const aria = c.key === sortKey ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none';
    const label = c.label
      ? (c.sortable
        ? `<button type="button" class="th-sort" data-sort="${c.key}">${escapeHtml(c.label)}
             <span class="th-arrow" aria-hidden="true">${
               c.key === sortKey ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}</span></button>`
        : escapeHtml(c.label))
      : '<span class="sr-only">Actions</span>';
    return `<th class="${c.num ? 'num' : ''}${c.cls ? ` ${c.cls}` : ''}" aria-sort="${aria}">${label}</th>`;
  }).join('');

  const body = sorted.map((row) => `
    <tr${row._id !== undefined ? ` data-row="${escapeHtml(row._id)}"` : ''}${
      row._cls ? ` class="${escapeHtml(row._cls)}"` : ''}>
      ${selectable ? `<td class="col-select"><input type="checkbox" data-select="${escapeHtml(row._id)}"
        aria-label="${escapeHtml(rowLabel ? rowLabel(row) : 'Select this row')}"></td>` : ''}
      ${columns.filter((c) => c.key !== '_select').map((c) =>
        `<td class="${c.num ? 'num' : ''}${c.cls ? ` ${c.cls}` : ''}">${c.render(row)}</td>`).join('')}
    </tr>`).join('');

  return `<div class="table-wrap"><table>
    <thead><tr>${selectable
      ? '<th class="col-select"><input type="checkbox" data-select-all aria-label="Select every row shown"></th>'
      : ''}${head}</tr></thead>
    <tbody>${body}</tbody></table></div>`;
}

/* `rowLabel(row)` names each checkbox after the thing it selects.
 *
 * Without it every one of a hundred rows announces "Select this row", which tells
 * a screen-reader user that there is a checkbox and nothing whatever about what
 * ticking it would do. The table is readable by eye because the name is in the row
 * beside it; that is exactly the information a checkbox has to carry itself.
 */

/* Wires the sort buttons of a table rendered by dataTable. The caller owns the
 * state and the redraw, so sorting never needs a second fetch. */
export function wireSort(container, state, redraw) {
  container.querySelectorAll('[data-sort]').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sort;
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = key;
        state.sortDir = 'desc';
      }
      redraw();
    });
  });
}

/* --------------------------------------------------------- select controls */

/* A pseudo-element cannot hang off a <select>, and a bare one is drawn by the
 * operating system - which is why these did not match the inputs beside them
 * and why the arrow stayed dark on a dark page. Everything that opens a list
 * goes through this wrapper so there is one control, not five. */
export const selectWrap = (inner) => `<span class="select-wrap">${inner}</span>`;

/* ------------------------------------------------------- searchable picker */

/* Fifty buildings in a <select> is fifty items to scroll past to reach b047, so
 * this is a filterable listbox rather than a native control.
 *
 * The datalist version could not do the one thing a picker exists for: show you
 * what you may pick. A datalist is filtered by whatever is already in the field,
 * and the field is pre-filled with the current selection - so clicking it
 * offered exactly one option, the one already chosen, and the whole list only
 * appeared once the text had been deleted. It also cannot be styled, so it was
 * the one control in the product wearing the operating system's appearance.
 *
 * This is the combobox pattern the map's building picker already uses: the list
 * opens on focus showing everything, typing filters it, and the options are
 * ordinary buttons this stylesheet can reach.
 */
export function picker({ id, label, options, value }) {
  const current = options.find((o) => o.value === value) || options[0];
  return `
    <label class="inline-label" for="${id}">${escapeHtml(label)}</label>
    <div class="combobox">
      <input class="picker" id="${id}" type="text" role="combobox" autocomplete="off"
             aria-expanded="false" aria-controls="${id}-list" aria-autocomplete="list"
             value="${escapeHtml(current ? current.label : '')}"
             aria-describedby="${id}-help">
      <ul id="${id}-list" role="listbox" aria-label="${escapeHtml(label)}" hidden></ul>
    </div>
    <span class="sr-only" id="${id}-help">Type to filter, or pick from the list.</span>`;
}

/* Opens on focus with every option showing, which is the behaviour the datalist
 * could not give. `onPick` is optional: without it the input still fires
 * `change`, so callers written against the old picker keep working.
 */
export function wirePicker(input, options = null, onPick = null) {
  if (!options) {
    input.addEventListener('focus', () => input.select());
    return;
  }
  const list = document.getElementById(`${input.id}-list`);
  if (!list) { input.addEventListener('focus', () => input.select()); return; }

  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); };

  const paint = () => {
    const query = input.value.trim().toLowerCase();
    /* An exact match on the current selection is not a filter anybody typed - it
     * is the value the field was left showing. Treat it as "show everything",
     * which is what opening a closed picker is asking for. */
    const isCurrent = options.some((o) => o.label.toLowerCase() === query);
    const matches = (query && !isCurrent)
      ? options.filter((o) => o.label.toLowerCase().includes(query))
      : options;

    list.innerHTML = matches.length
      ? matches.map((o) => `
        <li role="option" aria-selected="${o.label === input.value}">
          <button type="button" data-pick="${escapeHtml(o.value)}"
                  data-label="${escapeHtml(o.label)}">${escapeHtml(o.label)}</button>
        </li>`).join('')
      : `<li class="picker-empty">Nothing matches &ldquo;${escapeHtml(input.value)}&rdquo;.</li>`;
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  };

  input.addEventListener('focus', () => { input.select(); paint(); });
  input.addEventListener('input', paint);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { close(); return; }
    if (event.key !== 'Enter') return;
    const first = list.querySelector('[data-pick]');
    if (first) { event.preventDefault(); first.click(); }
  });
  list.addEventListener('click', (event) => {
    const button = event.target.closest('[data-pick]');
    if (!button) return;
    input.value = button.dataset.label;
    close();
    if (onPick) onPick(button.dataset.pick);
    else input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.combobox')) close();
  });
}

/* Resolves what was typed back to an option value. Returns null when the text
 * matches nothing, so the caller can leave the previous selection in place
 * rather than fetching for a building that does not exist. */
export function pickerValue(input, options) {
  const typed = input.value.trim().toLowerCase();
  const hit = options.find((o) => o.label.toLowerCase() === typed)
    || options.find((o) => o.label.toLowerCase().startsWith(typed));
  return hit ? hit.value : null;
}

/* ---------------------------------------------------------------- jobs */

/* Starts a maintenance job and watches it to the end.

 * These take minutes, so the button cannot simply await a response: the server
 * answers 202 immediately and the work carries on behind it. This polls until
 * the job reports done or failed, and reports every state through `onState` so
 * the screen can say what is happening rather than going quiet for eight
 * minutes - which is indistinguishable from being broken.
 *
 * Polling stops when the view is torn down: `signal` is the view lifetime, so
 * navigating away does not leave a timer firing against a dead screen.
 */
export async function runJob(kind, { onState, signal } = {}) {
  const say = (state) => { if (onState) onState(state); };

  let state;
  try {
    state = await api.jobStart(kind);
  } catch (error) {
    /* 409 means somebody else's job holds the slot. That is not a failure of
     * this request and should not read like one. */
    if (error?.status === 409) {
      say({ status: 'busy', error: error.detail });
      return { status: 'busy', error: error.detail };
    }
    throw error;
  }
  say(state);

  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  /* Every two seconds. The jobs run for minutes; a tighter poll would only add
   * requests to a server that is busy doing the thing being polled for. */
  while (!signal?.aborted) {
    await wait(2000);
    if (signal?.aborted) break;
    state = await api.jobStatus(kind);
    say(state);
    if (state.status === 'done' || state.status === 'failed') return state;
  }
  return state;
}

/* -------------------------------------------------------------------- dialog */

/* A real dialog, because three chained `window.prompt` calls were how this
 * application used to mint credentials: clear text, no confirmation field, no
 * validation before the request went out, and nothing a password manager can
 * fill.
 *
 * It also does the two things a hand-built modal usually forgets. Focus is
 * trapped inside it, so Tab cannot walk into the page behind the scrim, and
 * focus returns to whatever opened it on close - otherwise a keyboard user is
 * dropped back at the top of the document with no idea where they were.
 *
 * Resolves to the form's values, or null if dismissed.
 */
export function openDialog({ title, description = '', fields = [], submitLabel,
                             submitKind = 'primary', validate, confirmText = '',
                             confirmHint = '', tone = '' }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const host = document.createElement('div');
    host.className = 'modal';
    host.innerHTML = `
      <div class="modal-inner narrow ${tone}" role="dialog" aria-modal="true"
           aria-label="${escapeHtml(title)}">
        <button class="close" type="button" data-close aria-label="Cancel">&times;</button>
        <h2>${escapeHtml(title)}</h2>
        ${description ? `<p class="dialog-lede">${escapeHtml(description)}</p>` : ''}
        <form class="form" data-form novalidate>
          ${fields.map((f) => f.type === 'select'
            ? `<label for="dlg-${f.name}">${escapeHtml(f.label)}
                 ${selectWrap(`<select id="dlg-${f.name}" name="${f.name}">
                   ${f.options.map((o) => `<option value="${escapeHtml(o)}"${
                     o === f.value ? ' selected' : ''}>${escapeHtml(o)}</option>`).join('')}
                 </select>`)}</label>`
            : `<label for="dlg-${f.name}">${escapeHtml(f.label)}
                 <input id="dlg-${f.name}" name="${f.name}" type="${f.type || 'text'}"
                        autocomplete="${f.autocomplete || 'off'}"${f.minlength
                          ? ` minlength="${f.minlength}"` : ''}>
                 ${f.hint ? `<span class="hint">${escapeHtml(f.hint)}</span>` : ''}</label>`
          ).join('')}
          ${confirmText ? `
            <!-- Typing the count is the protection, not a second click. The
                 number is the thing the reader has to have actually looked at,
                 and it is the one detail a mis-click cannot supply. -->
            <label for="dlg-confirm">Type <strong>${escapeHtml(confirmText)}</strong> to confirm
              <input id="dlg-confirm" name="__confirm" type="text" autocomplete="off"
                     inputmode="numeric" spellcheck="false">
              ${confirmHint ? `<span class="hint">${escapeHtml(confirmHint)}</span>` : ''}
            </label>` : ''}
          <div data-error></div>
          <div class="form-actions">
            <button type="submit" class="${submitKind}"${confirmText ? ' disabled' : ''}
              >${escapeHtml(submitLabel)}</button>
            <button type="button" class="ghost" data-close>Cancel</button>
            ${fields.some((f) => f.type === 'password')
              ? '<button type="button" class="ghost small" data-generate>Suggest a password</button>'
              : ''}
          </div>
        </form>
      </div>`;

    const form = host.querySelector('[data-form]');
    const errors = host.querySelector('[data-error]');
    const confirmField = host.querySelector('#dlg-confirm');
    if (confirmField) {
      const submitButton = form.querySelector('button[type="submit"]');
      confirmField.addEventListener('input', () => {
        submitButton.disabled = confirmField.value.trim() !== confirmText;
      });
    }
    const FOCUSABLE = 'button, input, select, textarea, [href], [tabindex]:not([tabindex="-1"])';

    const close = (value) => {
      document.removeEventListener('keydown', onKey, true);
      host.remove();
      if (opener && opener.isConnected) opener.focus();
      resolve(value);
    };

    const onKey = (event) => {
      if (event.key === 'Escape') { close(null); return; }
      if (event.key !== 'Tab') return;
      const items = [...host.querySelectorAll(FOCUSABLE)].filter((el) => !el.disabled);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    };

    host.querySelectorAll('[data-close]').forEach((b) =>
      b.addEventListener('click', () => close(null)));
    /* Clicking the scrim dismisses; clicking inside must not. */
    host.addEventListener('click', (event) => { if (event.target === host) close(null); });
    document.addEventListener('keydown', onKey, true);

    const generate = host.querySelector('[data-generate]');
    if (generate) {
      generate.addEventListener('click', () => {
        /* From the platform's own CSPRNG. A temporary password typed by whoever
         * is creating the account tends to be the same one every time. */
        const bytes = new Uint8Array(18);
        window.crypto.getRandomValues(bytes);
        const alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-.';
        const suggestion = Array.from(bytes, (b) => alphabet[b % alphabet.length]).join('');
        host.querySelectorAll('input[type=password]').forEach((input) => {
          input.type = 'text';           // it has to be readable to be written down
          input.value = suggestion;
        });
      });
    }

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const values = {};
      for (const field of fields) {
        values[field.name] = host.querySelector(`[name="${field.name}"]`).value;
      }
      const problem = validate ? validate(values) : null;
      if (problem) {
        errors.innerHTML = `<div class="error-box" role="alert">${escapeHtml(problem)}</div>`;
        host.querySelector('input, select')?.focus();
        return;
      }
      close(values);
    });

    document.body.appendChild(host);
    (host.querySelector('input, select') || host.querySelector('[data-close]')).focus();
  });
}

/* A confirmation that names what is about to happen and what it costs. Used for
 * the changes that are not obviously reversible - a role change, disabling an
 * account, closing thirty thousand alerts. */
export const confirmAction = ({ title, description, confirmLabel, confirmText = '',
                               confirmHint = '' }) =>
  openDialog({ title, description, fields: [], submitLabel: confirmLabel,
               submitKind: 'danger', tone: 'critical', confirmText, confirmHint });
