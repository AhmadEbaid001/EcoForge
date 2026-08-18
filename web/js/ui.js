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

import { escapeHtml, icon } from './charts.js';

/* --------------------------------------------------------------- page header */

/* Writes the workspace header, which lives OUTSIDE the scrolling pane, and
 * returns the status slot for the view to place inside it.
 *
 * h1 is the product name in the sidebar, so a view title is an h2 and a panel
 * heading is an h3. Screen readers navigate by heading level; skipping one
 * makes the outline lie about the structure.
 */
export function pageHead({ title, description, actions = '', meta = '' }) {
  const host = document.getElementById('page-head');
  if (host) {
    host.innerHTML = `
      <div class="page-head">
        <div class="page-head-text">
          <h2>${escapeHtml(title)}</h2>
          ${description ? `<p>${escapeHtml(description)}</p>` : ''}
        </div>
        <div class="page-head-side">${meta}${actions}</div>
      </div>`;
  }
  return '<div class="view-status" data-status role="status" aria-atomic="true"></div>';
}

/* The data clock, stated rather than implied.
 *
 * The simulator runs at 720x, so a screen left open for a minute is showing
 * twelve hours of stale data. Nothing polls - that is a known open item - so the
 * honest thing is to say when the numbers were read and offer to read them
 * again, instead of presenting a stopped clock as a live one. */
export function freshness(dataClock) {
  const stamp = dataClock ? dataClock.replace('T', ' ').slice(0, 16) : 'unknown';
  return `
    <span class="freshness" title="Data time, not wall-clock time: the simulator runs at 720x">
      ${icon('clock')}
      <span class="freshness-label">Data clock</span>
      <span>${escapeHtml(stamp)}</span>
    </span>
    <button type="button" class="ghost" data-refresh>${icon('refresh')}Refresh</button>`;
}

/* ------------------------------------------------------------------- status */

/* One atomic sentence, in the view's own status slot. "Closed 34 alerts" rather
 * than a bare number, because a screen reader announcing "34" tells nobody
 * what happened. */
export function setStatus(root, { kind = 'ok', message, actionLabel = '', actionAttr = '' } = {}) {
  const slot = root.querySelector('[data-status]');
  if (!slot) return null;
  if (!message) { slot.innerHTML = ''; return null; }

  const box = { ok: 'ok-box', warn: 'warn-box', error: 'error-box', hint: 'hint-box' }[kind];
  slot.innerHTML = `
    <div class="${box} status-line">
      <span>${escapeHtml(message)}</span>
      <span class="status-actions">
        ${actionLabel ? `<button type="button" class="ghost small" ${actionAttr}>${escapeHtml(actionLabel)}</button>` : ''}
        <button type="button" class="close-inline" data-dismiss aria-label="Dismiss this message">&times;</button>
      </span>
    </div>`;
  slot.querySelector('[data-dismiss]').addEventListener('click', () => { slot.innerHTML = ''; });
  return slot;
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
    return `<th class="${c.num ? 'num' : ''}" aria-sort="${aria}">${label}</th>`;
  }).join('');

  const body = sorted.map((row) => `
    <tr${row._id !== undefined ? ` data-row="${escapeHtml(row._id)}"` : ''}>
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

/* Fifty buildings in a <select> is fifty items to scroll past to reach b047. A
 * native datalist keeps the keyboard behaviour the browser already provides
 * instead of reimplementing a combobox badly. */
export function picker({ id, label, options, value }) {
  const current = options.find((o) => o.value === value) || options[0];
  return `
    <label class="inline-label" for="${id}">${escapeHtml(label)}</label>
    ${selectWrap(`
      <input class="picker" id="${id}" type="text" list="${id}-list" autocomplete="off"
             value="${escapeHtml(current ? current.label : '')}"
             aria-describedby="${id}-help">`)}
    <datalist id="${id}-list">
      ${options.map((o) => `<option value="${escapeHtml(o.label)}"></option>`).join('')}
    </datalist>
    <span class="sr-only" id="${id}-help">Type to filter, or pick from the list.</span>`;
}

/* Selecting the existing text on focus means typing replaces the current
 * building instead of appending to it, which is what turns a text field with a
 * list behind it into something that behaves like a picker. */
export function wirePicker(input) {
  input.addEventListener('focus', () => input.select());
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
                             submitKind = 'primary', validate }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const host = document.createElement('div');
    host.className = 'modal';
    host.innerHTML = `
      <div class="modal-inner narrow" role="dialog" aria-modal="true"
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
          <div data-error></div>
          <div class="form-actions">
            <button type="submit" class="${submitKind}">${escapeHtml(submitLabel)}</button>
            <button type="button" class="ghost" data-close>Cancel</button>
            ${fields.some((f) => f.type === 'password')
              ? '<button type="button" class="ghost small" data-generate>Suggest a password</button>'
              : ''}
          </div>
        </form>
      </div>`;

    const form = host.querySelector('[data-form]');
    const errors = host.querySelector('[data-error]');
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
export const confirmAction = ({ title, description, confirmLabel }) =>
  openDialog({ title, description, fields: [], submitLabel: confirmLabel, submitKind: 'danger' });
