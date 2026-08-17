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

export function compact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: 1 });
}

const PAD = { top: 14, right: 16, bottom: 26, left: 54 };

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
export function lineChart(series, { width = 760, height = 240, unit = '' } = {}) {
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
    ? `<div class="legend">${withData.map((s, i) =>
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
export function stackedBars(days, keys, { width = 760, height = 200 } = {}) {
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

  const legend = `<div class="legend">${keys.map((k, i) =>
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
    role="img" aria-label="proportions">${segments}</svg><div class="legend">${labels}</div>`;
}

export function statTile(label, value, hint = '') {
  return `<div class="stat">
    <div class="stat-value">${escapeHtml(value)}</div>
    <div class="stat-label">${escapeHtml(label)}</div>
    ${hint ? `<div class="stat-hint">${escapeHtml(hint)}</div>` : ''}
  </div>`;
}
