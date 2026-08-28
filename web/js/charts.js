/* Charts as inline SVG, hand-drawn.
 *
 * No charting library, for the same reason there is no mapping library: F13 requires
 * the demonstration to survive an unplugged network cable, and the Content-Security-
 * Policy this application sends forbids loading script from anywhere else. A CDN
 * <script> would not merely be a bad idea here, it would be blocked by the browser.
 *
 * These are the shapes the dashboards actually need. Each takes plain data and
 * returns an SVG string, so a view is a template and nothing owns mutable chart state.
 *
 * A chart here is INTERACTIVE by default, and that is a deliberate reversal. The
 * first version relied on the browser's own `<title>` tooltip: it appears after a
 * second of stillness, it is unstyled, it cannot be reached from the keyboard, and it
 * shows one mark's value when the question is nearly always "what were all of them at
 * that moment". Every chart now carries a crosshair, a real readout, arrow-key
 * navigation and a table of the same numbers underneath, because a value only a mouse
 * can reach is a value half the room cannot read.
 */

'use strict';

import { locale } from './i18n.js';

export const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Icons, drawn here for the same reason the charts are: an icon font or an SVG
 * sprite from a CDN would be blocked by the Content-Security-Policy, and an
 * offline demonstration cannot depend on one anyway (F13).
 *
 * They are strokes in `currentColor`, so one drawing serves both appearances
 * instead of needing a light and a dark asset.
 */
const ICONS = {
  light: '<circle cx="12" cy="12" r="4.2"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2' +
         'M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/>',
  dark: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',

  /* One per navigation destination, and the paths are the handoff's own. Every
   * one of these is a real screen: there is no Search, no notification bell and
   * no settings gear here, because there is nothing behind them. An icon for a
   * feature that does not exist is worse than no icon.
   *
   * Alerts is a triangle rather than a bell, deliberately: the triangle is the
   * shape this product uses for critical severity everywhere else, so the rail
   * item and the row it leads to are drawn in the same language. */
  overview: '<path d="M4 20V9l8-5 8 5v11M9.5 20v-6h5v6"/>',
  map: '<path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2zM9 4v14M15 6v14"/>',
  alerts: '<path d="M12 4l9 16H3zM12 10v4M12 17.2v.1"/>',
  forecasts: '<path d="M3 17l5-6 4 3 4-7 5 5"/>',
  runs: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  /* Evidence: a clipboard with its tick. The one screen whose whole job is
   * showing that the other claims were checked rather than asserted. */
  evidence: '<rect x="5" y="4" width="14" height="17" rx="1.5"/><path d="M9 4.5V3h6v1.5"/>'
          + '<path d="M8.5 12l2.5 2.5L15.5 10M8.5 17H16"/>',
  integrity: '<path d="M12 3l7 3v6c0 4-3 6.5-7 9-4-2.5-7-5-7-9V6zM9 12l2.5 2.5L16 10"/>',
  admin: '<path d="M12 3l2 2h3v3l2 2-2 2v3h-3l-2 2-2-2H7v-3l-2-2 2-2V5h3zM12 9.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5z"/>',
  account: '<path d="M12 4a4 4 0 110 8 4 4 0 010-8zM4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
  signout: '<path d="M14.5 8V5.5a1.5 1.5 0 0 0-1.5-1.5H6a1.5 1.5 0 0 0-1.5 1.5v13A1.5 1.5 0 0 0 6 20h7a1.5 1.5 0 0 0 1.5-1.5V16"/>' +
           '<path d="M9.5 12h10m0 0-3-3m3 3-3 3"/>',
  check: '<path d="M4 12.5l5 5L20 6.5"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/>'
     + '<circle cx="12" cy="12" r="3.2"/>',
  'eye-off': '<path d="M4 4l16 16"/>'
     + '<path d="M9.5 6.2A9.6 9.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3.3 4"/>'
     + '<path d="M6.3 8.1A17 17 0 0 0 2.5 12S6 18.5 12 18.5a9.7 9.7 0 0 0 3.3-.6"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  /* The same triangle as critical severity, at 24 units. Stale data and a
   * critical alert are both "this needs looking at", and drawing them as one
   * shape is the point of having a shape language at all. */
  warning: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.2v.1"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="1.5"/>' + '<path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4v4.5h-4.5"/>',
  table: '<rect x="3.5" y="4.5" width="17" height="15" rx="1.5"/>' +
         '<path d="M3.5 9.5h17M9.5 9.5v10"/>',
};

/* The shape language. Colour is never the only carrier of meaning here, so
 * every severity, state and verdict pairs its hue with a distinct shape AND a
 * word. These are the handoff's own paths, on a 14-unit box, and they are the
 * same six marks wherever they appear - a legend, a table cell, a map pin, a
 * chart key.
 *
 * This is not decoration. `--crit` and `--high` are close enough for
 * deuteranopia that the alerts chart stacks two bars a colourblind reader
 * cannot tell apart; the shape is what actually separates them.
 */
const MARKS = {
  critical: 'M7 1.5l5.5 10h-11Z',      // triangle
  high:     'M2.5 2.5h9v9h-9Z',        // square
  medium:   'M2 5.5h10v3H2Z',          // bar
  check:    'M2 7.5l3.5 3.5L12 3',     // funded, pass, intact
  dash:     'M3 7h8',                  // not funded, neutral
  cross:    'M3 3l8 8M11 3l-8 8',      // fail, disabled, struck out
};

/* Filled for the three severities, stroked for the three verdicts: a triangle
 * has to read as a solid mass at 11px, and a check drawn as a filled shape is
 * a blob. */
const FILLED = new Set(['critical', 'high', 'medium']);

export function mark(name, { size = 14 } = {}) {
  const d = MARKS[name];
  if (!d) return '';
  const paint = FILLED.has(name)
    ? 'fill="currentColor" stroke="none"'
    : 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square"';
  return `<svg class="mark" viewBox="0 0 14 14" width="${size}" height="${size}" ${paint}` +
         ` aria-hidden="true" focusable="false"><path d="${d}"/></svg>`;
}

/* Shape, then word, then colour - in that order of load-bearing-ness. */
export function sevChip(severity) {
  const key = String(severity || '').toLowerCase();
  if (!MARKS[key]) return escapeHtml(severity || '—');
  const label = key.charAt(0).toUpperCase() + key.slice(1);
  return `<span class="sev ${key}">${mark(key, { size: 11 })}${label}</span>`;
}

export function icon(name) {
  const body = ICONS[name];
  if (!body) return '';
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${body}</svg>`;
}

export function compact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return Number(n).toLocaleString(locale(), { maximumFractionDigits: 1 });
}

/* The readout says the number, not an abbreviation of it. `compact` exists so an
 * axis can be read at a glance; a reader who has gone to the trouble of pointing at
 * one particular hour wants the figure, and "1.2k" is not it. */
const exact = (n) => (n === null || n === undefined || Number.isNaN(n))
  ? '—'
  : Number(n).toLocaleString(locale(), { maximumFractionDigits: 1 });

const PAD = { top: 16, right: 18, bottom: 30, left: 56 };

/* Marks are thin and the surface shows between them: a 2px gap around every filled
 * segment is what stops a stack reading as one striped block. */
const SEGMENT_GAP = 2;
const CORNER = 4;

const fmtDay = (ms) => new Date(ms).toISOString().slice(5, 10);
const fmtStamp = (ms) => new Date(ms).toISOString().slice(0, 16).replace('T', ' ');

/* ------------------------------------------------------- responsive charts */

/* A chart is an SVG string, drawn once, at 760px.
 *
 * It scales - the viewBox sees to that - but scaling is not redrawing. Tick
 * density, label spacing and the number of gridlines were all decided for 760
 * pixels, so on a wide monitor the axis is sparse and stretched, and on a narrow
 * one the labels crowd into each other. The map has redrawn on resize since it was
 * written; the charts never have.
 *
 * Rather than make every call site manage a ResizeObserver, each chart registers
 * how to redraw itself at a given width, and `hydrateCharts` connects the ones
 * that are now in the document. Call sites keep returning strings and know nothing
 * about any of this.
 */
const pendingCharts = new Map();
let chartSeq = 0;

/* The host owns three children and only the first is ever redrawn.
 *
 * The readout and the table do not depend on width, and rebuilding them on every
 * resize would throw away an open table and a tooltip mid-hover. Splitting them out
 * also gives the interaction layer somewhere stable to live: listeners bind to the
 * host once and read whatever the latest draw left behind, so a redraw never has to
 * re-attach anything - and a missed re-attach is a chart that silently stops
 * responding at one particular window width.
 */
function registerChart(make, table = '') {
  const id = `chart-${++chartSeq}`;
  /* The id reaches the drawing function because SVG paint servers are referenced
   * by id and ids are document-scoped: two charts on one screen defining
   * `#hatch-a` would have the second silently repaint the first. */
  const draw = (width) => make(width, id);
  pendingCharts.set(id, draw);
  const first = draw(DEFAULT_WIDTH);
  return `<div class="chart-host" data-chart-id="${id}">
    <div class="chart-plot">${typeof first === 'string' ? first : first.html}</div>
    <div class="chart-tip" role="status" aria-live="polite" data-empty="1"></div>
    ${table}
  </div>`;
}

function debounceRedraw(fn) {
  let timer = null;
  return () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(fn, 120);
  };
}

/* Below this the axis has no room for its labels whatever the container says. */
const MIN_CHART_WIDTH = 320;
/* Generous: a single view renders at most a handful. */
const MAX_PENDING = 32;
const DEFAULT_WIDTH = 760;

export function hydrateCharts(root, signal) {
  const hosts = [...root.querySelectorAll('[data-chart-id]')]
    .filter((host) => !host.__redraw);
  if (!hosts.length) return;

  /* Two triggers, because they catch different things.
   *
   * `window.resize` is the common case - someone drags the window, or turns a
   * tablet - and it fires in every browser and every automation harness.
   * ResizeObserver catches what resize cannot: the container changing width while
   * the window does not, which is exactly what happens when the sidebar collapses
   * to its icon rail. Neither alone covers both. */
  const redrawAll = () => hosts.forEach((host) => host.__redraw?.());
  window.addEventListener('resize', debounceRedraw(redrawAll), { signal });

  hosts.forEach((host) => {
    const draw = pendingCharts.get(host.dataset.chartId);
    if (!draw) return;
    /* Consumed on connect: the registry holds closures over chart data, and a
     * view that renders and is navigated away from would otherwise leave them
     * alive for the life of the page. */
    pendingCharts.delete(host.dataset.chartId);

    const plot = host.querySelector('.chart-plot');
    if (!plot) return;

    let last = 0;
    host.__redraw = () => {
      const width = Math.max(MIN_CHART_WIDTH, Math.round(host.getBoundingClientRect().width));
      /* A redraw changes nothing about the host's own width, but observing an
       * element while writing into it is how a ResizeObserver loop starts. The
       * threshold makes it converge, and it also stops a one-pixel scrollbar
       * change from rebuilding every chart on the screen. */
      if (Math.abs(width - last) < 24) return;
      last = width;
      const out = draw(width);
      plot.innerHTML = typeof out === 'string' ? out : out.html;
      host.__meta = typeof out === 'string' ? null : out.meta;
      /* The cursor indexes into geometry that has just been replaced. */
      clearReadout(host);
    };
    host.__redraw();
    bindInteraction(host, signal);

    if (typeof ResizeObserver === 'function') {
      const observer = new ResizeObserver(() => host.__redraw());
      observer.observe(host);
      signal?.addEventListener('abort', () => observer.disconnect());
    }
  });

  /* Registrations for hosts that were never inserted - a view that threw between
   * building its HTML and writing it - would otherwise accumulate for the life of
   * the page. Anything still pending after a hydrate pass has no host. */
  if (pendingCharts.size > MAX_PENDING) pendingCharts.clear();
}

/* --------------------------------------------------------- the hover layer */

function bindInteraction(host, signal) {
  const options = signal ? { signal } : undefined;
  const plot = host.querySelector('.chart-plot');
  if (!plot) return;

  plot.addEventListener('pointermove', (event) => {
    if (!host.__meta) return;
    const index = indexFromPointer(host, event.clientX);
    if (index !== null) moveCursor(host, index);
  }, options);

  plot.addEventListener('pointerleave', () => clearReadout(host), options);

  /* The same values, from the keyboard. Shift jumps ten at a time, because
   * stepping through 336 hourly readings one arrow press at a time is not
   * navigation, it is a punishment. */
  plot.addEventListener('keydown', (event) => {
    const meta = host.__meta;
    if (!meta || !meta.count) return;
    const at = host.__cursor ?? 0;
    const step = event.shiftKey ? 10 : 1;
    let next = null;
    if (event.key === 'ArrowRight') next = Math.min(meta.count - 1, at + step);
    else if (event.key === 'ArrowLeft') next = Math.max(0, at - step);
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = meta.count - 1;
    else if (event.key === 'Escape') { clearReadout(host); return; }
    else return;
    event.preventDefault();
    /* An arrow press that lands on the index already shown must still repaint:
     * the first press after focus has no cursor to move. */
    if (host.__cursor === next) return;
    moveCursor(host, next);
  }, options);

  plot.addEventListener('focusout', () => clearReadout(host), options);
}

/* Pointer x in client pixels to a data index.
 *
 * The SVG is drawn in viewBox units and displayed at whatever width the column
 * gives it, so the two coincide only by accident. Everything below works in viewBox
 * units and converts once, here.
 */
function indexFromPointer(host, clientX) {
  const meta = host.__meta;
  const svg = host.querySelector('svg.chart');
  if (!meta || !svg || !meta.count) return null;
  const rect = svg.getBoundingClientRect();
  if (!rect.width) return null;
  return nearestIndex(meta, (clientX - rect.left) * (meta.width / rect.width));
}

function nearestIndex(meta, x) {
  if (meta.kind === 'bars') {
    const span = (meta.plotRight - meta.plotLeft) / meta.count;
    return Math.min(meta.count - 1, Math.max(0, Math.floor((x - meta.plotLeft) / span)));
  }
  /* Binary search: 336 hourly points is not somewhere to run a linear scan sixty
   * times a second. */
  const xs = meta.xs;
  if (xs.length === 1) return 0;
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < x) lo = mid; else hi = mid;
  }
  return (x - xs[lo] <= xs[hi] - x) ? lo : hi;
}

function moveCursor(host, index) {
  if (host.__cursor === index) return;
  host.__cursor = index;
  paintCursor(host, host.__meta, index);
  writeReadout(host, host.__meta, index);
}

function paintCursor(host, meta, index) {
  const svg = host.querySelector('svg.chart');
  if (!svg) return;
  svg.classList.add('is-cursored');

  if (meta.kind === 'line') {
    const x = meta.xs[index];
    const line = svg.querySelector('.crosshair');
    if (line) { line.setAttribute('x1', x); line.setAttribute('x2', x); }
    meta.series.forEach((series, i) => {
      const dot = svg.querySelector(`.cursor-dot.s${i}`);
      if (!dot) return;
      const point = series.at[index];
      /* A series with a gap here has no value to point at, and a dot parked at
       * its last known position would claim one. */
      dot.classList.toggle('is-void', !point);
      if (point) { dot.setAttribute('cx', point.sx); dot.setAttribute('cy', point.sy); }
    });
  } else {
    svg.querySelectorAll('.col.is-active').forEach((c) => c.classList.remove('is-active'));
    svg.querySelector(`.col[data-i="${index}"]`)?.classList.add('is-active');
  }

  positionReadout(host, meta, index);
}

/* The readout is placed by writing one custom property and nothing else.
 *
 * A CSSOM write is not what `style-src` blocks - that is the inline `style="..."`
 * ATTRIBUTE in parsed markup, which the policy discards silently and which cost this
 * project three dead layout rules in `map.js`. Even so, only the coordinate comes
 * from here: every visual decision about the readout lives in `style.css` against
 * `--tip-x`, so there is still exactly one place to change how it looks.
 */
function positionReadout(host, meta, index) {
  const tip = host.querySelector('.chart-tip');
  const svg = host.querySelector('svg.chart');
  if (!tip || !svg) return;
  const rect = svg.getBoundingClientRect();
  if (!rect.width) return;
  const unit = rect.width / meta.width;
  const x = meta.kind === 'bars' ? meta.columns[index].cx : meta.xs[index];
  tip.style.setProperty('--tip-x', `${(x * unit).toFixed(1)}px`);
  /* Flip to the left of the crosshair once there is no room on the right, so the
   * readout never hangs off the panel. */
  tip.classList.toggle('flip', x * unit > rect.width * 0.62);
}

/* Built with textContent, never innerHTML.
 *
 * Series labels here are building codes and severity names that arrive from the
 * API. They are trusted today, and the escaping everywhere else in this file exists
 * because they might not be tomorrow; a readout assembled by string concatenation
 * would be the one place that assumption stopped holding.
 */
function writeReadout(host, meta, index) {
  const tip = host.querySelector('.chart-tip');
  if (!tip) return;
  tip.replaceChildren();
  delete tip.dataset.empty;

  const head = document.createElement('div');
  head.className = 'tip-head';
  head.textContent = meta.kind === 'bars'
    ? String(meta.columns[index].date)
    : fmtStamp(meta.times[index]);
  tip.append(head);

  const rows = meta.kind === 'bars'
    ? meta.keys.map((key, i) => ({
        cls: `b${i}`,
        /* A proportion has one row per bar rather than one row per key, so it
         * names its own: "100% of total" says something, "share" does not. */
        label: meta.rowLabels ? meta.rowLabels[index] : key,
        value: meta.columns[index].values[key] ?? 0,
      }))
    : meta.series.map((series, i) => ({
        cls: `s${i}`, label: series.label, value: series.at[index]?.v ?? null,
      }));

  for (const row of rows) {
    const line = document.createElement('div');
    line.className = 'tip-row';

    const key = document.createElement('span');
    key.className = `tip-key ${row.cls}`;
    line.append(key);

    /* Value first and heavier: the reader already knows which series they are
     * looking at, and came for the number. The legend's hierarchy, inverted. */
    /* The unit rides on the value rather than sitting on its own line. A separate
     * unit row read as a third fact when it is part of the first one, and beside a
     * series already named "Portfolio kW" it said the same word twice. */
    const value = document.createElement('strong');
    value.className = 'tip-value';
    value.textContent = row.value === null
      ? '—'
      : exact(row.value) + (meta.unit ? ` ${meta.unit}` : '');
    line.append(value);

    const label = document.createElement('span');
    label.className = 'tip-label';
    label.textContent = row.label;
    line.append(label);

    tip.append(line);
  }

}

function clearReadout(host) {
  host.__cursor = null;
  const tip = host.querySelector('.chart-tip');
  if (tip) { tip.replaceChildren(); tip.dataset.empty = '1'; }
  host.querySelector('svg.chart')?.classList.remove('is-cursored');
  host.querySelectorAll('.col.is-active').forEach((c) => c.classList.remove('is-active'));
}

/* ------------------------------------------------------------------ scales */

function scale(values, size, pad0, pad1, invert = false) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const usable = size - pad0 - pad1;
  return (value) => invert
    ? pad0 + usable - ((value - min) / span) * usable
    : pad0 + ((value - min) / span) * usable;
}

/* Round tick values, so an axis reads 0 / 500 / 1,000 rather than 0 / 437 / 874. */
function niceTicks(min, max, count = 4) {
  const span = (max - min) || 1;
  const step = Math.pow(10, Math.floor(Math.log10(span / count)));
  const chosen = [1, 2, 2.5, 5, 10].map((m) => m * step).find((c) => span / c <= count)
    || step * 10;
  const ticks = [];
  for (let v = Math.ceil(min / chosen) * chosen; v <= max; v += chosen) ticks.push(v);
  return ticks;
}

/* How many date labels the axis has room for. Two - the first and the last - is
 * what it had, and it left the middle of a fortnight unlabelled. */
const dateTickCount = (width) => Math.max(2, Math.min(7, Math.floor(width / 130)));

function emptyChart(width, height, message) {
  return `<svg class="chart empty" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="${escapeHtml(message)}">
    <text class="axis" x="${width / 2}" y="${height / 2}" text-anchor="middle">${escapeHtml(message)}</text>
  </svg>`;
}

/* ------------------------------------------------------------------ texture */

/* Hatching, because the severity palette is not separable by hue.
 *
 * Run through the colour checks, `critical` and `high` in the light appearance are
 * 0.6 apart in deutan ΔE - to roughly one man in twenty they are the SAME COLOUR,
 * and the alerts chart stacks them on top of each other. `medium` and `high` are 13
 * apart even in normal vision, which is below the 15 at which a reader can be
 * expected to tell two marks apart at a glance.
 *
 * Re-stepping the tokens is the real repair and it is not a chart's decision to
 * make: those three colours are also the alert badges, the map markers and the
 * severity chips, so they change everywhere or nowhere. What a chart CAN do is stop
 * relying on hue alone. Each band carries its own texture as well as its colour, so
 * the stack stays readable in any vision, in greyscale, and printed.
 *
 * The lines are painted in the surface colour, so a hatch reads as the surface
 * showing through the mark rather than as a second ink on top of it.
 */
const TEXTURES = ['', 'hatch-a', 'hatch-b', 'hatch-c'];

function textureDefs(id) {
  const line = '<line class="hatch-line" x1="0" y1="0" x2="0" y2="7"/>';
  const pattern = (name, angle) =>
    `<pattern id="${name}-${id}" width="7" height="7" patternUnits="userSpaceOnUse" ` +
    `patternTransform="rotate(${angle})">${line}</pattern>`;
  return `<defs>${pattern('hatch-a', 45)}${pattern('hatch-b', 135)}` +
         `${pattern('hatch-c', 90)}</defs>`;
}

/* The texture is a second shape over the first rather than a fill on it, so the
 * colour stays a CSS decision and only the pattern reference has to know the id. */
function textured(index, id, shape) {
  const name = TEXTURES[index % TEXTURES.length];
  if (!name) return '';
  return shape.replace('class="bar', `fill="url(#${name}-${id})" class="texture bar`)
              .replace('class="seg', `fill="url(#${name}-${id})" class="texture seg`);
}

/* ------------------------------------------------------------ table fallback */

/* The same numbers, without a pointer.
 *
 * A `<details>` rather than a scripted toggle: it needs no JavaScript, it is
 * keyboard operable and screen-reader announced for free, and it stays closed, so a
 * reader who does not want it pays one line of chrome for it.
 */
function tableView(caption, columns, rows, note = '') {
  if (!rows.length) return '';
  /* A cell counts as numeric if what it shows starts with a digit or a sign -
   * "1,240", "-3.2", "97%", "12.4k" all do, "2042-04-20 12:23" deliberately does
   * not, because a timestamp is read left to right like a word. */
  const numericColumn = columns.map((_, i) =>
    rows.every((r) => /^[-+]?[\d.,]+\s*[%a-zA-Z/]{0,6}$/.test(String(r[i] ?? '').trim())));
  const head = columns.map((c, i) =>
    `<th scope="col"${numericColumn[i] ? ' class="num"' : ''}>${escapeHtml(c)}</th>`).join('');
  const body = rows.map((r) =>
    `<tr>${r.map((cell, i) =>
      `<td${numericColumn[i] ? ' class="num"' : ''}>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('');
  return `<details class="chart-table">
    <summary>${icon('table')}<span>${escapeHtml(caption)}</span></summary>
    ${note ? `<p class="caption">${escapeHtml(note)}</p>` : ''}
    <div class="chart-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
  </details>`;
}

/* A fortnight of hourly readings is 336 rows, and nobody reads 336 rows - they
 * check a shape and a couple of values. Every nth row keeps it a table rather than
 * a data dump, and the note says plainly that it has been thinned. */
const TABLE_MAX_ROWS = 60;

function thin(list) {
  if (list.length <= TABLE_MAX_ROWS) return { rows: list, stride: 1 };
  const stride = Math.ceil(list.length / TABLE_MAX_ROWS);
  return { rows: list.filter((_, i) => i % stride === 0), stride };
}

/* ------------------------------------------------------------- chain figure */

/* The reading chain, drawn as the thing it is: one run of sequence numbers, a
 * point up to which every link was re-computed and matched, and - when there is
 * one - the sequence where that stopped being true.
 *
 * A verdict of "Broken at 4718 of 555,600" is a sentence a reader has to hold
 * three numbers in their head to picture. Drawn, the proportion is the argument:
 * the verified prefix IS most of the bar, and the break is a hairline near its
 * start with everything after it unproven. That is what the screen is claiming,
 * and it is easier to check by eye than to reconstruct from figures.
 *
 * Not a chart of data. It is a diagram of a claim, so it carries no axis - only
 * the three sequence numbers that bound it, and a legend that says what the two
 * regions mean.
 */
export function chainFigure({ rows, breakSeq = null, anchorSeq = null, ok = true }) {
  const total = Math.max(1, Number(rows) || 1);
  const W = 1000, H = 74, TRACK_Y = 22, TRACK_H = 26;
  const at = (seq) => Math.max(0, Math.min(W, (seq / total) * W));

  /* A break at sequence 1 of half a million is a zero-width verified region, and
   * a break at the last row is a zero-width unproven one. Both still have to be
   * visible, so each region keeps a floor of two pixels once it exists at all. */
  const cut = breakSeq === null ? W : at(breakSeq);
  const provenW = breakSeq === null ? W : Math.max(2, cut);
  const restW = breakSeq === null ? 0 : Math.max(2, W - cut);

  const anchor = anchorSeq ? at(anchorSeq) : null;

  return `<figure class="chain-figure">
    <svg viewBox="0 0 ${W} ${H}" class="chain-svg" role="img"
         aria-label="${breakSeq === null
           ? `Every one of ${exact(total)} links re-computed and matched.`
           : `Links 1 to ${exact(Math.max(0, breakSeq - 1))} matched. `
             + `From ${exact(breakSeq)} to ${exact(total)} the chain is unproven.`}">
      <defs>
        <pattern id="chain-unproven" width="7" height="7" patternUnits="userSpaceOnUse"
                 patternTransform="rotate(45)">
          <rect width="7" height="7" class="chain-unproven-bg"/>
          <line x1="0" y1="0" x2="0" y2="7" class="chain-unproven-line"/>
        </pattern>
      </defs>

      <rect x="0" y="${TRACK_Y}" width="${provenW.toFixed(1)}" height="${TRACK_H}"
            rx="3" class="chain-proven"/>
      ${restW ? `<rect x="${cut.toFixed(1)}" y="${TRACK_Y}" width="${restW.toFixed(1)}"
            height="${TRACK_H}" rx="3" class="chain-rest"/>` : ''}

      ${breakSeq !== null ? `
        <line x1="${cut.toFixed(1)}" y1="${TRACK_Y - 8}" x2="${cut.toFixed(1)}"
              y2="${TRACK_Y + TRACK_H + 8}" class="chain-break"/>
        <path d="M${(cut - 6).toFixed(1)} ${TRACK_Y - 9}h12l-6 8Z" class="chain-break-mark"/>` : ''}

      ${anchor !== null ? `
        <line x1="${anchor.toFixed(1)}" y1="${TRACK_Y - 4}" x2="${anchor.toFixed(1)}"
              y2="${TRACK_Y + TRACK_H + 4}" class="chain-anchor"/>` : ''}

      <text x="0" y="${H - 6}" class="chain-tick" text-anchor="start">1</text>
      ${breakSeq !== null ? `<text x="${cut.toFixed(1)}" y="14" class="chain-tick break"
        text-anchor="${cut > W * 0.85 ? 'end' : cut < W * 0.15 ? 'start' : 'middle'}"
        >${exact(breakSeq)}</text>` : ''}
      <text x="${W}" y="${H - 6}" class="chain-tick" text-anchor="end">${exact(total)}</text>
    </svg>
    <figcaption class="chain-legend">
      <span class="key"><span class="swatch proven"></span>${breakSeq === null
        ? 'Re-computed and matched' : 'Matched up to the break'}</span>
      ${breakSeq !== null
        ? '<span class="key"><span class="swatch rest"></span>Unproven after it</span>' : ''}
      ${anchorSeq ? '<span class="key"><span class="swatch anchor"></span>External anchor</span>' : ''}
    </figcaption>
  </figure>`;
}

/* --------------------------------------------------------------- line chart */

/* A time series, one or more lines. series = [{label, points: [[iso, y], ...]}] */
export function lineChart(series, options = {}) {
  const withData = series.filter((s) => s.points && s.points.length);
  let table = '';
  if (withData.length) {
    /* One row per timestamp across every series, so the table answers the same
     * question the crosshair does rather than a different one per line. */
    const times = [...new Set(withData.flatMap((s) => s.points.map((p) => Date.parse(p[0]))))]
      .sort((a, b) => a - b);
    const maps = withData.map((s) => new Map(s.points.map((p) => [Date.parse(p[0]), p[1]])));
    const { rows, stride } = thin(times);
    table = tableView(
      'Table',
      ['Time', ...withData.map((s) => s.label + (options.unit ? ` (${options.unit})` : ''))],
      rows.map((t) => [fmtStamp(t), ...maps.map((m) => m.has(t) ? exact(m.get(t)) : '—')]),
      stride > 1 ? `Every ${stride}th of ${times.length} readings.` : '',
    );
  }
  return registerChart((width) => drawLine(series, { ...options, width }), table);
}

function drawLine(series, { width = DEFAULT_WIDTH, height = 240, unit = '' } = {}) {
  const withData = series.filter((s) => s.points && s.points.length);
  if (!withData.length) return emptyChart(width, height, 'no data in this window');

  const times = [...new Set(withData.flatMap((s) => s.points.map((p) => Date.parse(p[0]))))]
    .sort((a, b) => a - b);
  const values = withData.flatMap((s) => s.points.map((p) => p[1]));
  const lo = Math.min(...values, 0);
  const hi = Math.max(...values);

  const x = scale([times[0], times[times.length - 1]], width, PAD.left, PAD.right);
  const y = scale([lo, hi], height, PAD.top, PAD.bottom, true);
  const xs = times.map((t) => +x(t).toFixed(2));

  const grid = niceTicks(lo, hi).map((t) =>
    `<line class="grid" x1="${PAD.left}" y1="${y(t).toFixed(1)}" x2="${width - PAD.right}" y2="${y(t).toFixed(1)}"/>` +
    `<text class="axis" x="${PAD.left - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${compact(t)}</text>`
  ).join('');

  const resolved = withData.map((s) => {
    const byTime = new Map(s.points.map((p) => [Date.parse(p[0]), p[1]]));
    return {
      label: s.label,
      at: times.map((t, i) => byTime.has(t)
        ? { sx: xs[i], sy: +y(byTime.get(t)).toFixed(2), v: byTime.get(t) }
        : null),
    };
  });

  /* A gap in one series breaks its line rather than drawing a straight segment
   * across the missing hours - a forecast that stops has stopped, and joining the
   * ends invents the part nobody computed. */
  const paths = resolved.map((s, i) => {
    let d = '';
    let pen = false;
    for (const point of s.at) {
      if (!point) { pen = false; continue; }
      d += `${pen ? 'L' : 'M'}${point.sx},${point.sy}`;
      pen = true;
    }
    return `<path class="series s${i}" d="${d}" fill="none"/>`;
  }).join('');

  /* The area belongs to a single series only. Under two overlapping lines it is a
   * wash that hides whichever is behind, and the quantity it emphasises - the
   * volume under the curve - is not one anyone reads off a comparison. */
  let area = '';
  if (resolved.length === 1) {
    const points = resolved[0].at.filter(Boolean);
    if (points.length > 1) {
      const base = (height - PAD.bottom).toFixed(1);
      const line = points.map((p, i) => `${i ? 'L' : 'M'}${p.sx},${p.sy}`).join('');
      area = `<path class="area" d="${line}L${points[points.length - 1].sx},${base}` +
             `L${points[0].sx},${base}Z"/>`;
    }
  }

  /* Selective direct labelling: the last value of each series, and nothing else. A
   * number on every point is a table pretending to be a chart. */
  const endLabels = resolved.map((s, i) => {
    const last = [...s.at].reverse().find(Boolean);
    if (!last) return '';
    const flip = last.sx > width - PAD.right - 56;
    /* Above the point, not beside it. On the same baseline the label lands on the
     * series line it is labelling, and at the right-hand edge - where the last
     * point always is - it had the line running through the digits. */
    const ty = Math.max(PAD.top + 10, last.sy - 9);
    return `<g class="end-label s${i}">` +
      `<circle cx="${last.sx}" cy="${last.sy}" r="3.5"/>` +
      `<text x="${(last.sx + (flip ? -6 : 6)).toFixed(1)}" y="${ty.toFixed(1)}"` +
      ` text-anchor="${flip ? 'end' : 'start'}">${escapeHtml(compact(last.v))}</text></g>`;
  }).join('');

  const ticks = dateTickCount(width);
  const dateTicks = Array.from({ length: ticks }, (_, i) => {
    const at = Math.round((i / (ticks - 1)) * (times.length - 1));
    const anchor = i === 0 ? 'start' : i === ticks - 1 ? 'end' : 'middle';
    return `<text class="axis" x="${xs[at].toFixed(1)}" y="${height - 10}" ` +
           `text-anchor="${anchor}">${fmtDay(times[at])}</text>`;
  }).join('');

  const cursor = `<g class="cursor" aria-hidden="true">
    <line class="crosshair" x1="0" y1="${PAD.top}" x2="0" y2="${height - PAD.bottom}"/>
    ${resolved.map((_, i) => `<circle class="cursor-dot s${i}" r="4.5" cx="0" cy="0"/>`).join('')}
  </g>`;

  const legend = resolved.length > 1
    ? `<div class="chart-legend">${resolved.map((s, i) =>
        `<span class="key s${i}">${escapeHtml(s.label)}</span>`).join('')}</div>`
    : '';

  const label = `${resolved.map((s) => s.label).join(' and ')}, ` +
    `${fmtStamp(times[0])} to ${fmtStamp(times[times.length - 1])}`;

  const html = `${legend}<svg class="chart" viewBox="0 0 ${width} ${height}" tabindex="0"
    role="img" aria-label="${escapeHtml(label)}">
    ${grid}${area}${paths}${cursor}${endLabels}${dateTicks}
    ${unit ? `<text class="axis unit" x="4" y="12">${escapeHtml(unit)}</text>` : ''}
  </svg>`;

  return {
    html,
    meta: { kind: 'line', width, height, unit, times, xs, series: resolved, count: times.length },
  };
}

/* -------------------------------------------------------------- stacked bars */

/* Stacked bars per day. days = [{date, ...counts}], keys names the stack order. */
export function stackedBars(days, keys, options = {}) {
  const table = days.length
    ? tableView('Table', ['Day', ...keys, 'Total'], days.map((d) => [
        d.date, ...keys.map((k) => exact(d[k] || 0)),
        exact(keys.reduce((sum, k) => sum + (d[k] || 0), 0)),
      ]))
    : '';
  return registerChart((width, id) => drawBars(days, keys, { ...options, width, id }), table);
}

function drawBars(days, keys, { width = DEFAULT_WIDTH, height = 200, id = 'x' } = {}) {
  if (!days.length) return emptyChart(width, height, 'no alerts in this window');

  const totals = days.map((d) => keys.reduce((sum, k) => sum + (d[k] || 0), 0));
  const max = Math.max(...totals, 1);
  const usableH = height - PAD.top - PAD.bottom;
  const base = height - PAD.bottom;
  const step = (width - PAD.left - PAD.right) / days.length;
  const barW = Math.max(1, step * 0.72);

  const grid = niceTicks(0, max, 3).map((t) => {
    const yy = base - (t / max) * usableH;
    return `<line class="grid" x1="${PAD.left}" y1="${yy.toFixed(1)}" x2="${width - PAD.right}" y2="${yy.toFixed(1)}"/>` +
           `<text class="axis" x="${PAD.left - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end">${compact(t)}</text>`;
  }).join('');

  const columns = [];
  const bars = days.map((day, i) => {
    const left = PAD.left + i * step + (step - barW) / 2;
    let cursor = base;

    /* Measured bottom-up but capped top-down: only the topmost visible segment
     * gets the rounded end, and which one that is is not known until the whole
     * stack has been walked. */
    const drawn = keys.map((key, k) => {
      const value = day[key] || 0;
      if (!value) return null;
      const raw = (value / max) * usableH;
      cursor -= raw;
      return { k, x: left, y: cursor, h: Math.max(1, raw - SEGMENT_GAP), key, value };
    }).filter(Boolean);

    const highest = drawn.length ? drawn[drawn.length - 1] : null;
    const shapes = drawn.map((seg) => {
      let shape;
      if (seg === highest) {
        const r = Math.min(CORNER, barW / 2, seg.h);
        shape = `<path class="bar b${seg.k}" d="M${seg.x.toFixed(1)},${(seg.y + seg.h).toFixed(1)}` +
          `V${(seg.y + r).toFixed(1)}a${r.toFixed(1)},${r.toFixed(1)} 0 0 1 ${r.toFixed(1)},${(-r).toFixed(1)}` +
          `h${(barW - 2 * r).toFixed(1)}a${r.toFixed(1)},${r.toFixed(1)} 0 0 1 ${r.toFixed(1)},${r.toFixed(1)}` +
          `V${(seg.y + seg.h).toFixed(1)}Z"/>`;
      } else {
        shape = `<rect class="bar b${seg.k}" x="${seg.x.toFixed(1)}" y="${seg.y.toFixed(1)}" ` +
          `width="${barW.toFixed(1)}" height="${seg.h.toFixed(1)}"/>`;
      }
      return shape + textured(seg.k, id, shape);
    }).join('');

    columns.push({
      cx: left + barW / 2,
      date: day.date,
      values: Object.fromEntries(keys.map((k) => [k, day[k] || 0])),
      total: totals[i],
    });

    /* The hit target is the whole column, floor to ceiling, not the painted
     * pixels. A day with two alerts is four pixels tall and nobody hits it. */
    return `<g class="col" data-i="${i}">
      <rect class="col-hit" x="${(PAD.left + i * step).toFixed(1)}" y="${PAD.top}"
        width="${step.toFixed(1)}" height="${(base - PAD.top).toFixed(1)}"/>
      ${shapes}
    </g>`;
  }).join('');

  const ticks = Math.min(dateTickCount(width), days.length);
  const dateTicks = Array.from({ length: ticks }, (_, i) => {
    const at = ticks === 1 ? 0 : Math.round((i / (ticks - 1)) * (days.length - 1));
    const anchor = i === 0 ? 'start' : i === ticks - 1 ? 'end' : 'middle';
    const cx = PAD.left + at * step + step / 2;
    return `<text class="axis" x="${cx.toFixed(1)}" y="${height - 10}" ` +
           `text-anchor="${anchor}">${escapeHtml(String(days[at].date).slice(5))}</text>`;
  }).join('');

  const legend = `<div class="chart-legend">${keys.map((k, i) => {
    const shape = MARKS[k] ? mark(k, { size: 11 }) : '';
    return `<span class="key b${i}${shape ? ' shaped' : ''}">${shape}${
      escapeHtml(k)}</span>`;
  }).join('')}</div>`;

  const html = `${legend}<svg class="chart" viewBox="0 0 ${width} ${height}" tabindex="0"
    role="img" aria-label="alerts per day, ${escapeHtml(String(days[0].date))} to ${escapeHtml(String(days[days.length - 1].date))}">
    ${textureDefs(id)}${grid}${bars}${dateTicks}
  </svg>`;

  return { html, meta: { kind: 'bars', width, height, keys, columns, count: days.length,
                         plotLeft: PAD.left, plotRight: width - PAD.right, unit: '' } };
}

/* ----------------------------------------------------------- proportion bar */

/* Proportions as one horizontal bar, replacing the pie chart Grafana used. Two
 * similar slices of a pie cannot be compared by eye, and comparing them is the only
 * thing anyone does with that panel.
 *
 * It paints from the CATEGORICAL colours, not the severity ones, and the difference
 * is not cosmetic. This bar answers "which forecaster is in use", and with all fifty
 * buildings on one model it drew a full-width bar in the same red as a critical
 * alert - a screen reading "100%" in alarm red, meaning that everything is fine.
 * Status colours are reserved for status. */
export function proportionBar(entries) {
  const total = entries.reduce((sum, e) => sum + e.value, 0);
  const table = total
    ? tableView('Table', ['Group', 'Count', 'Share'], entries.map((e) => [
        e.label, exact(e.value), `${((e.value / total) * 100).toFixed(1)}%`,
      ]))
    : '';
  return registerChart((width, id) => drawProportion(entries, width, id), table);
}

function drawProportion(entries, width = DEFAULT_WIDTH, id = 'x') {
  const total = entries.reduce((sum, e) => sum + e.value, 0);
  if (!total) return emptyChart(width, 46, 'nothing to show');

  const height = 22;
  let x = 0;
  const columns = [];
  const segments = entries.map((entry, i) => {
    const w = (entry.value / total) * width;
    const inner = Math.max(1, w - SEGMENT_GAP);
    const r = Math.min(CORNER, inner / 2, height / 2);
    const rect = `<rect class="seg b${i}" x="${x.toFixed(1)}" y="0" rx="${r.toFixed(1)}"
      width="${inner.toFixed(1)}" height="${height}"/>`;
    const texture = textured(i, id, rect);
    /* Labelled inside the segment only where it fits. Anything narrower keeps its
     * number in the readout and the table, which is where it stays legible. */
    const label = w > 64
      ? `<text class="seg-label" x="${(x + inner / 2).toFixed(1)}" y="${height / 2 + 4}"
          text-anchor="middle">${escapeHtml(((entry.value / total) * 100).toFixed(0))}%</text>`
      : '';
    columns.push({ cx: x + inner / 2, date: entry.label,
                   values: { share: entry.value }, total: entry.value });
    x += w;
    return rect + texture + label;
  }).join('');

  const labels = entries.map((entry, i) =>
    `<span class="key b${i}">${escapeHtml(entry.label)} ${escapeHtml(exact(entry.value))}</span>`).join('');

  const html = `<svg class="chart proportion" viewBox="0 0 ${width} ${height}" tabindex="0"
    role="img" aria-label="proportions">${textureDefs(id)}${segments}</svg>
    <div class="chart-legend proportion">${labels}</div>`;

  return { html, meta: { kind: 'bars', width, height, keys: ['share'], columns,
                         rowLabels: entries.map((e) =>
                           `${((e.value / total) * 100).toFixed(1)}% of ${exact(total)}`),
                         count: entries.length, plotLeft: 0, plotRight: width, unit: '' } };
}

/* ---------------------------------------------------------------- stat tiles */

/* A sparkline, for a tile whose number has a history.
 *
 * No axes and no labels, on purpose: it answers "which way, and how steadily", and
 * the moment it carries a scale it is a chart in a space too small to be one. The
 * figure beside it is the value; this is only its shape.
 */
export function sparkline(values, { width = 120, height = 28 } = {}) {
  const points = values.filter((v) => typeof v === 'number' && !Number.isNaN(v));
  if (points.length < 2) return '';
  const lo = Math.min(...points);
  const hi = Math.max(...points);
  const span = (hi - lo) || 1;
  const stepX = width / (points.length - 1);
  const at = (v) => height - 2 - ((v - lo) / span) * (height - 4);
  const path = points.map((v, i) => `${i ? 'L' : 'M'}${(i * stepX).toFixed(1)},${at(v).toFixed(1)}`).join('');
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" aria-hidden="true" focusable="false">
    <path class="spark-line" d="${path}" fill="none"/>
    <circle class="spark-end" cx="${width}" cy="${at(points[points.length - 1]).toFixed(1)}" r="2.5"/>
  </svg>`;
}

/* `trend` is a signed percentage, `spark` a series of numbers. Both optional, and a
 * tile given neither renders exactly as it did before.
 *
 * The arrow is paired with a sign and a number rather than being a coloured glyph on
 * its own: up is not always good - these tiles count faults as often as they count
 * savings - so the direction is stated and the reader decides what it means.
 */
/* label / figure / note, and an optional row of shape-marked chips under it for
 * a figure that splits - the open-alert count into its three severities, say.
 * `chips` is markup and `hint` is text: the note is written here and the chips
 * are built from `mark()`, so neither is ever user input. */
export function statTile(label, value, hint = '', { spark = null, trend = null, chips = '' } = {}) {
  const direction = trend === null ? '' : trend > 0 ? 'up' : trend < 0 ? 'down' : 'flat';
  const arrow = { up: '▲', down: '▼', flat: '■' }[direction] || '';
  const delta = direction
    ? `<span class="stat-trend ${direction}"><span aria-hidden="true">${arrow}</span> ` +
      `${escapeHtml(Math.abs(trend).toFixed(0))}%</span>`
    : '';
  /* Label, figure, note, then anything supplementary - in that order, always.
   *
   * The sparkline used to sit between the figure and the note, so the note started
   * 36px lower on the one card that had a sparkline than on the three that did
   * not. Four cards in a row with their descriptions on four different baselines
   * is what makes a dashboard look assembled rather than designed. The spark and
   * the severity split are the supplementary parts and they go in a footer that is
   * pushed to the bottom of the card, which lines them up too. */
  return `<div class="stat">
    <div class="stat-head">
      <div class="stat-value">${escapeHtml(value)}${delta}</div>
      <div class="stat-label">${escapeHtml(label)}</div>
      ${hint ? `<div class="stat-hint">${escapeHtml(hint)}</div>` : ''}
    </div>
    ${spark || chips ? `<div class="stat-foot">
      ${spark ? `<div class="stat-spark">${sparkline(spark)}</div>` : ''}
      ${chips ? `<div class="stat-chips">${chips}</div>` : ''}
    </div>` : ''}
  </div>`;
}
