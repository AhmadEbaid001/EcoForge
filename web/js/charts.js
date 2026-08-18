/* Charts as inline SVG, hand-drawn.
 *
 * No charting library, for the same reason there is no mapping library: F13 requires
 * the demonstration to survive an unplugged network cable, and the Content-Security-
 * Policy this application sends forbids loading script from anywhere else. A CDN
 * <script> would not merely be a bad idea here, it would be blocked by the browser.
 *
 * These are the four shapes the dashboards actually need. Each takes plain data and
 * returns an SVG string, so a view is a template and nothing owns mutable chart state.
 */

'use strict';

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
  auto: '<circle cx="12" cy="12" r="8"/><path d="M12 4v16" />' +
        '<path class="filled" d="M12 4a8 8 0 0 1 0 16z"/>',
  light: '<circle cx="12" cy="12" r="4.2"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2' +
         'M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/>',
  dark: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',

  /* One per navigation destination. Every one of these is a real screen: there
   * is no Search, no notification bell and no settings gear here, because there
   * is nothing behind them. An icon for a feature that does not exist is worse
   * than no icon. */
  overview: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/>' +
            '<rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/>' +
            '<rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/>' +
            '<rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  map: '<path d="M9 4.5 3.5 6.8v12.7L9 17.2l6 2.3 5.5-2.3V4.5L15 6.8z"/>' +
       '<path d="M9 4.5v12.7M15 6.8v12.7"/>',
  forecasts: '<path d="M3.5 20V4"/><path d="M3.5 20h17"/>' +
             '<path d="M6.5 15.5 10 11l3 3 4.5-6.5"/>',
  alerts: '<path d="M18 8.5a6 6 0 1 0-12 0c0 5-2 6.5-2 6.5h16s-2-1.5-2-6.5z"/>' +
          '<path d="M13.7 19a2 2 0 0 1-3.4 0"/>',
  runs: '<path d="M3.5 6.5h17M3.5 12h17M3.5 17.5h17"/>' +
        '<path d="M7 4.5v4M14 9.5v5M18 15.5v4"/>',
  integrity: '<path d="M12 3.5 5 6.2v5.3c0 4.3 2.9 7.6 7 8.9 4.1-1.3 7-4.6 7-8.9V6.2z"/>' +
             '<path d="m9 12 2.2 2.2L15.5 10"/>',
  admin: '<circle cx="9" cy="8.5" r="3.2"/>' +
         '<path d="M3.5 19.5c0-3 2.5-4.8 5.5-4.8s5.5 1.8 5.5 4.8"/>' +
         '<path d="M16.5 6.5h4M16.5 10h4M16.5 13.5h2.5"/>',
  account: '<circle cx="12" cy="8.5" r="3.5"/>' +
           '<path d="M5 20c0-3.5 3-5.5 7-5.5s7 2 7 5.5"/>',
  signout: '<path d="M14.5 8V5.5a1.5 1.5 0 0 0-1.5-1.5H6a1.5 1.5 0 0 0-1.5 1.5v13A1.5 1.5 0 0 0 6 20h7a1.5 1.5 0 0 0 1.5-1.5V16"/>' +
           '<path d="M9.5 12h10m0 0-3-3m3 3-3 3"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  /* The reference's brand glyph was Material's `electric_bolt`. */
  bolt: '<path class="filled" d="M13 2 4.5 13.2H11l-1.2 8.8L19.5 10.8H13z"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4v4.5h-4.5"/>',
};

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
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: 1 });
}

const PAD = { top: 14, right: 16, bottom: 26, left: 54 };

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

function registerChart(draw) {
  const id = `chart-${++chartSeq}`;
  pendingCharts.set(id, draw);
  return `<div class="chart-host" data-chart-id="${id}">${draw(DEFAULT_WIDTH)}</div>`;
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

    let last = 0;
    host.__redraw = () => {
      const width = Math.max(MIN_CHART_WIDTH, Math.round(host.getBoundingClientRect().width));
      /* A redraw changes nothing about the host's own width, but observing an
       * element while writing into it is how a ResizeObserver loop starts. The
       * threshold makes it converge, and it also stops a one-pixel scrollbar
       * change from rebuilding every chart on the screen. */
      if (Math.abs(width - last) < 24) return;
      last = width;
      host.innerHTML = draw(width);
    };
    host.__redraw();

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

function emptyChart(width, height, message) {
  return `<svg class="chart empty" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="${escapeHtml(message)}">
    <text class="axis" x="${width / 2}" y="${height / 2}" text-anchor="middle">${escapeHtml(message)}</text>
  </svg>`;
}

/* A time series, one or more lines. series = [{label, points: [[iso, y], ...]}] */
export function lineChart(series, options = {}) {
  return registerChart((width) => drawLine(series, { ...options, width }));
}

function drawLine(series, { width = DEFAULT_WIDTH, height = 240, unit = '' } = {}) {
  const withData = series.filter((s) => s.points && s.points.length);
  if (!withData.length) return emptyChart(width, height, 'no data in this window');

  const times = withData.flatMap((s) => s.points.map((p) => Date.parse(p[0])));
  const values = withData.flatMap((s) => s.points.map((p) => p[1]));
  const lo = Math.min(...values, 0);
  const hi = Math.max(...values);

  const x = scale(times, width, PAD.left, PAD.right);
  const y = scale([lo, hi], height, PAD.top, PAD.bottom, true);

  const grid = niceTicks(lo, hi).map((t) =>
    `<line class="grid" x1="${PAD.left}" y1="${y(t).toFixed(1)}" x2="${width - PAD.right}" y2="${y(t).toFixed(1)}"/>` +
    `<text class="axis" x="${PAD.left - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${compact(t)}</text>`
  ).join('');

  const paths = withData.map((s, i) => {
    const d = s.points
      .map((p, idx) => `${idx ? 'L' : 'M'}${x(Date.parse(p[0])).toFixed(1)},${y(p[1]).toFixed(1)}`)
      .join('');
    return `<path class="series s${i}" d="${d}" fill="none"/>`;
  }).join('');

  const first = new Date(Math.min(...times)).toISOString().slice(0, 10);
  const last = new Date(Math.max(...times)).toISOString().slice(0, 10);

  const legend = withData.length > 1
    ? `<div class="chart-legend">${withData.map((s, i) =>
        `<span class="key s${i}">${escapeHtml(s.label)}</span>`).join('')}</div>`
    : '';

  return `${legend}<svg class="chart" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="${escapeHtml(withData.map((s) => s.label).join(' and '))}">
    ${grid}${paths}
    <text class="axis" x="${PAD.left}" y="${height - 8}">${first}</text>
    <text class="axis" x="${width - PAD.right}" y="${height - 8}" text-anchor="end">${last}</text>
    ${unit ? `<text class="axis unit" x="4" y="12">${escapeHtml(unit)}</text>` : ''}
  </svg>`;
}

/* Stacked bars per day. days = [{date, ...counts}], keys names the stack order. */
export function stackedBars(days, keys, options = {}) {
  return registerChart((width) => drawBars(days, keys, { ...options, width }));
}

function drawBars(days, keys, { width = DEFAULT_WIDTH, height = 200 } = {}) {
  if (!days.length) return emptyChart(width, height, 'no alerts in this window');

  const totals = days.map((d) => keys.reduce((sum, k) => sum + (d[k] || 0), 0));
  const max = Math.max(...totals, 1);
  const usableH = height - PAD.top - PAD.bottom;
  const step = (width - PAD.left - PAD.right) / days.length;
  const barW = Math.max(1, step * 0.72);

  const bars = days.map((day, i) => {
    let cursor = height - PAD.bottom;
    return keys.map((key, k) => {
      const value = day[key] || 0;
      if (!value) return '';
      const h = (value / max) * usableH;
      cursor -= h;
      return `<rect class="bar b${k}" x="${(PAD.left + i * step).toFixed(1)}" y="${cursor.toFixed(1)}"
        width="${barW.toFixed(1)}" height="${h.toFixed(1)}"><title>${escapeHtml(day.date)}: ${value} ${escapeHtml(key)}</title></rect>`;
    }).join('');
  }).join('');

  const grid = niceTicks(0, max, 3).map((t) => {
    const yy = height - PAD.bottom - (t / max) * usableH;
    return `<line class="grid" x1="${PAD.left}" y1="${yy.toFixed(1)}" x2="${width - PAD.right}" y2="${yy.toFixed(1)}"/>` +
           `<text class="axis" x="${PAD.left - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end">${compact(t)}</text>`;
  }).join('');

  const legend = `<div class="chart-legend">${keys.map((k, i) =>
    `<span class="key b${i}">${escapeHtml(k)}</span>`).join('')}</div>`;

  return `${legend}<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="alerts per day">
    ${grid}${bars}
    <text class="axis" x="${PAD.left}" y="${height - 8}">${escapeHtml(days[0].date)}</text>
    <text class="axis" x="${width - PAD.right}" y="${height - 8}" text-anchor="end">${escapeHtml(days[days.length - 1].date)}</text>
  </svg>`;
}

/* Proportions as one horizontal bar, replacing the pie chart Grafana used. Two
 * similar slices of a pie cannot be compared by eye, and comparing them is the only
 * thing anyone does with that panel. */
export function proportionBar(entries) {
  const total = entries.reduce((sum, e) => sum + e.value, 0);
  if (!total) return emptyChart(760, 46, 'nothing to show');

  let x = 0;
  const segments = entries.map((entry, i) => {
    const w = (entry.value / total) * 760;
    const rect = `<rect class="seg b${i}" x="${x.toFixed(1)}" y="0" width="${w.toFixed(1)}" height="18">
      <title>${escapeHtml(entry.label)}: ${entry.value} (${((entry.value / total) * 100).toFixed(0)}%)</title></rect>`;
    x += w;
    return rect;
  }).join('');

  const labels = entries.map((entry, i) =>
    `<span class="key b${i}">${escapeHtml(entry.label)} ${entry.value}</span>`).join('');

  return `<svg class="chart proportion" viewBox="0 0 760 18" preserveAspectRatio="none"
    role="img" aria-label="proportions">${segments}</svg><div class="chart-legend">${labels}</div>`;
}

export function statTile(label, value, hint = '') {
  return `<div class="stat">
    <div class="stat-value">${escapeHtml(value)}</div>
    <div class="stat-label">${escapeHtml(label)}</div>
    ${hint ? `<div class="stat-hint">${escapeHtml(hint)}</div>` : ''}
  </div>`;
}
