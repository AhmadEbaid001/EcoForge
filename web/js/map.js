/* The map view.
 *
 * Moved wholesale out of the old single-page map and into a module the shell can
 * mount, keeping its internals untouched: this is the part of the system that has
 * been demonstrated most, and rewriting working projection and rendering code to fit
 * a new navigation shell would risk the one screen that must not break.
 *
 * The markup it needs travels with it, so the shell stays generic and no other view
 * has to know that this one wants an <svg> of a particular id.
 */

'use strict';

import { api } from './api.js';
import { pageHead } from './ui.js';

/* Class names here are namespaced `map-*` on purpose. `controls` and `legend`
 * used to be shared with the panel toolbars and the chart legends, and the
 * stylesheet's later block won both: this column was being laid out as a
 * wrapping flex ROW, and so was the legend inside it. One class name claimed by
 * two components is invisible until someone opens the screen and it is wrong.
 *
 * There are no `style="..."` attributes left in this file either. The
 * Content-Security-Policy is `style-src 'self'` with no 'unsafe-inline', so the
 * browser was discarding every one of them - the narrative cards had never had
 * the side-by-side layout their markup asked for. */
const MAP_HTML = String.raw`<!-- ---------------------------------------------------------------- -->
  <section class="map-controls" aria-label="Allocation controls">

    <div class="field">
      <label for="budget">Budget</label>
      <output id="budget-out">10,000,000 EGP</output>
      <input type="range" id="budget" min="1000000" max="30000000" step="500000" value="10000000">
      <div class="scale"><span>1M</span><span>15M</span><span>30M</span></div>
    </div>

    <div class="field">
      <label for="objective">Rank by</label>
      <span class="select-wrap"><select id="objective">
        <option value="lca_carbon">Life-cycle carbon (kgCO&#8322;e)</option>
        <option value="raw_kwh">First-year energy (kWh)</option>
        <option value="egp_saved">First-year money (EGP)</option>
      </select></span>
    </div>

    <div class="field">
      <label for="solver">Allocation method</label>
      <span class="select-wrap"><select id="solver">
        <option value="cpsat">Exact optimization (CP-SAT)</option>
        <option value="greedy_upgrade">Greedy + upgrade pass</option>
        <option value="greedy">Greedy heuristic (saturates)</option>
        <option value="equal_split">Equal split &mdash; status quo</option>
      </select></span>
    </div>

    <div class="field">
      <label for="cap">Max funded per district</label>
      <span class="select-wrap"><select id="cap">
        <option value="">No cap</option>
        <option value="2">2</option>
        <option value="4">4</option>
        <option value="6">6</option>
      </select></span>
    </div>

    <div class="map-actions">
      <button id="compare-btn" type="button" class="primary">Compare all four methods</button>
      <button id="narrative-btn" type="button" class="ghost">Why building-specific?</button>
    </div>

    <div class="map-legend">
      <h3>Map</h3>
      <div><i class="sw funded"></i> Funded by this allocation</div>
      <div><i class="sw unfunded"></i> Not funded</div>
      <div><i class="sw anomaly"></i> Open anomaly</div>
      <p class="note">Size reflects annual consumption. A funded building carries a
      lighter outline as well as its colour.</p>
    </div>
  </section>

  <!-- ---------------------------------------------------------------- -->
  <section class="mapwrap">
    <div class="summary" id="summary">
      <div class="metric"><span class="k" id="m-funded">&mdash;</span><span class="v">buildings funded</span></div>
      <div class="metric"><span class="k" id="m-spent">&mdash;</span><span class="v">of budget spent</span></div>
      <div class="metric"><span class="k" id="m-kwh">&mdash;</span><span class="v">kWh/yr saved</span></div>
      <div class="metric"><span class="k" id="m-carbon">&mdash;</span><span class="v">kgCO&#8322;e lifetime</span></div>
      <div class="metric"><span class="k" id="m-solve">&mdash;</span><span class="v">solve time</span></div>
    </div>

    <svg id="map" tabindex="0" role="img" aria-describedby="map-help"
         aria-label="District map of the building portfolio"></svg>

    <!-- Zoom was a mouse wheel and nothing else, which left the map unusable
         from a keyboard and awkward on a trackpad in front of a room. The
         buttons and the arrow keys reach the same state the wheel does. -->
    <div class="map-zoom" role="group" aria-label="Zoom">
      <button type="button" id="zoom-in" aria-label="Zoom in">+</button>
      <button type="button" id="zoom-out" aria-label="Zoom out">&minus;</button>
      <button type="button" id="zoom-reset" aria-label="Fit the whole district">Fit</button>
    </div>

    <p class="maphint" id="map-help">Scroll or use + and &minus; to zoom &mdash; real
    OpenStreetMap footprints appear as you zoom in &middot; drag or use the arrow keys to
    pan &middot; click a building for its options</p>
  </section>

  <!-- ---------------------------------------------------------------- -->
  <aside class="detail" id="detail">
    <div class="empty" id="detail-empty">
      <h2>Select a building</h2>
      <p>Every option the optimizer considered there is listed, with the one it chose
         and why &mdash; so a recommendation can be interrogated rather than taken on trust.</p>
    </div>
    <div id="detail-body" hidden></div>
  </aside>`;

/* `tabindex="-1"` so the dialog itself can take focus when it opens. That is
 * what lets Escape work without a document-level listener - the key event
 * bubbles from inside the modal - and it is also what stops a keyboard user
 * being left behind on the button that opened it. */
const MODAL_HTML = String.raw`<div id="narrative-modal" class="modal" hidden>
  <div class="modal-inner" tabindex="-1" role="dialog" aria-modal="true"
       aria-label="The best measure is building-specific">
    <button class="close" id="narrative-close" type="button" aria-label="Close">&times;</button>
    <h2>The best measure is building-specific</h2>
    <div id="narrative-body">Loading&hellip;</div>
  </div>
</div>

<div id="compare-modal" class="modal" hidden>
  <div class="modal-inner" tabindex="-1" role="dialog" aria-modal="true"
       aria-label="Same portfolio, same budget, four methods">
    <button class="close" id="compare-close" type="button" aria-label="Close">&times;</button>
    <h2>Same portfolio, same budget, four methods</h2>
    <div id="compare-body">Press Compare to solve.</div>
  </div>
</div>`;

/* Whether the person looking at this may run the optimizer.
 *
 * A viewer may read the portfolio but not solve - `/optimize` is analyst-only. The
 * map used to call it on mount regardless, so a viewer's first sight of the product
 * was a red health indicator reading "this action requires the analyst role", five
 * summary metrics showing em-dashes, and a budget slider that 403'd on every drag.
 * Nothing was broken; they simply were not allowed, and the screen had no way to say
 * so.
 *
 * Read-only is a real state here, not a degraded one: the stored allocations are the
 * decisions the organisation actually made, and someone who may not re-run the
 * optimizer is precisely the person who should be looking at them. */
let canSolve = false;

const ROLE_LADDER = ['viewer', 'analyst', 'admin'];
const atLeast = (role, needed) =>
  ROLE_LADDER.indexOf(role) >= ROLE_LADDER.indexOf(needed);

export const mapView = {
  title: 'Map',
  async render(root, ctx) {
    canSolve = atLeast(ctx?.user?.role || 'viewer', 'analyst');

    root.innerHTML = `
      ${pageHead({
        title: 'Allocation map',
        description: canSolve
          ? 'Set a budget and a method; the optimizer decides which buildings get '
            + 'which retrofit. Click a building to see every option it considered.'
          : 'The most recent stored allocation. Click a building to see every option '
            + 'the optimizer considered there, and which one it chose.',
      })}
      <div class="map-layout">${MAP_HTML}</div>${MODAL_HTML}`;

    if (!canSolve) makeReadOnly(root);
    await main(ctx?.signal);
  },
};

/* Remove the controls rather than disable them.
 *
 * A row of greyed-out sliders is a screen telling someone what they cannot have.
 * Replacing the panel with the run's provenance answers the question they actually
 * have - what is this allocation, and when was it decided. */
function makeReadOnly(root) {
  const controls = root.querySelector('.map-controls');
  if (!controls) return;
  controls.innerHTML = `
    <div class="field">
      <h3 class="field-title">Stored allocation</h3>
      <p class="note" id="readonly-note">Showing the most recent optimizer run.
      Running a new one needs the analyst role.</p>
      <dl class="facts" id="run-facts"></dl>
    </div>`;
}



const API = '/api/v1';

const state = {
  geo: null,          // FeatureCollection
  run: null,          // most recent allocation response
  selected: null,     // building id
  view: { x: 0, y: 0, k: 1 },
  projected: [],      // [{id, props, points:[[x,y]...], cx, cy}]
};

/* ------------------------------------------------------------------ utils */

const $ = (id) => document.getElementById(id);

const fmt = (n, digits = 0) =>
  n === null || n === undefined || Number.isNaN(n)
    ? '—'
    : n.toLocaleString('en-US', { maximumFractionDigits: digits });

function compact(n) {
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(0) + 'k';
  return fmt(n);
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* ------------------------------------------------------------- projection */

/* Equirectangular, with longitude compressed by cos(latitude) so the district
 * does not look stretched. Over a few kilometres the distortion is far below the
 * width of a stroke, and a real projection would be a dependency for no gain. */
function project(geo) {
  const lats = [], lons = [];
  for (const f of geo.features) {
    for (const ring of ringsOf(f)) {
      for (const [lon, lat] of ring) { lats.push(lat); lons.push(lon); }
    }
  }
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);
  const minLon = Math.min(...lons), maxLon = Math.max(...lons);
  const midLat = (minLat + maxLat) / 2;
  const kx = Math.cos((midLat * Math.PI) / 180);

  const spanX = (maxLon - minLon) * kx || 1e-6;
  const spanY = (maxLat - minLat) || 1e-6;

  return { minLat, maxLat, minLon, maxLon, kx, spanX, spanY };
}

function ringsOf(feature) {
  const g = feature.geometry;
  if (!g) return [];
  if (g.type === 'Polygon') return g.coordinates;
  if (g.type === 'MultiPolygon') return g.coordinates.flat();
  if (g.type === 'Point') return [[g.coordinates]];
  return [];
}

/* ------------------------------------------------------------------ render */

const PAD = 60;

/* Zoom at which real footprints replace the consumption-sized squares. */
const FOOTPRINT_ZOOM = 3;

// The old ceiling was 8, which was set before anyone had measured what a footprint
// is worth at that zoom. Measured in the browser: at k = 3.5 the median outline is
// 6.4 screen pixels across and at k = 8 it is 10.4 - correct geometry that nobody can
// actually read, which makes "real OpenStreetMap footprints" a claim again rather
// than something a reviewer can see. At 24 the median reaches about 31 pixels and an
// L-shaped building is unmistakably L-shaped.
//
// The squares do not shrink at the switch by accident: a square is a SYMBOL sized by
// annual consumption, while a footprint is drawn at true scale, so the building
// necessarily gets smaller when the real outline takes over. That is the intended
// trade and it is why the ceiling, not the switch point, is what needed raising.
const MAX_ZOOM = 24;

function buildGeometry() {
  const svg = $('map');
  const W = svg.clientWidth, H = svg.clientHeight;
  const p = project(state.geo);

  /* Buildings occupy a tiny fraction of a district's area, so drawing footprints
   * at true scale yields fifty invisible specks. They are drawn as squares of
   * area proportional to consumption, centred on the real centroid: position is
   * geographic, size encodes the quantity the decision is about. */
  const scale = Math.min((W - 2 * PAD) / p.spanX, (H - 2 * PAD) / p.spanY);
  const consumptions = state.geo.features.map((f) => f.properties.annual_kwh || 0);
  const maxKwh = Math.max(...consumptions, 1);

  const toScreen = ([lon, lat]) => [
    PAD + (lon - p.minLon) * p.kx * scale,
    PAD + (p.maxLat - lat) * scale,              // screen y grows downward
  ];

  state.projected = state.geo.features.map((f) => {
    const props = f.properties;
    const [x, y] = toScreen([props.lon, props.lat]);
    const side = 9 + 26 * Math.sqrt((props.annual_kwh || 0) / maxKwh);

    // The true OpenStreetMap outline, projected once. At overview zoom a real
    // footprint is about a pixel across, so the square is what gets drawn; zoom in
    // and the actual building shape takes over. Fetching real geometry and never
    // showing it would make "real footprints" a claim rather than something a
    // reviewer can see.
    const ring = (ringsOf(f)[0] || []).map(toScreen);

    return { id: props.id, props, cx: x, cy: y, side, ring };
  });
}

/* One hue per district, from the name, so the districts stay visually separate
 * without a palette anyone has to maintain. Saturation and lightness come from
 * tokens rather than being fixed here: at 30% lightness these were dark grey
 * blocks, which is right on a dark ground and unreadable on a light one. */
function districtColour(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) % 360;
  return `hsl(${hash}, var(--district-s), var(--district-l))`;
}

function render() {
  const svg = $('map');
  const { x, y, k } = state.view;
  /* A run fetched from /runs/{id} carries building_code but not building_id, so
   * fall back to the `funded` flag the server already put on each feature when
   * the geojson was requested with that run. */
  const funded = new Set(
    (state.run?.items || []).map((i) => i.building_id).filter(Boolean));
  const isFunded_ = (b) => funded.has(b.id) || b.props.funded === true;

  const parts = [`<g transform="translate(${x},${y}) scale(${k})">`];

  // District labels, placed at each cluster's centroid.
  const byDistrict = new Map();
  for (const b of state.projected) {
    const list = byDistrict.get(b.props.district) || [];
    list.push(b);
    byDistrict.set(b.props.district, list);
  }
  for (const [name, list] of byDistrict) {
    const cx = list.reduce((s, b) => s + b.cx, 0) / list.length;
    const cy = Math.min(...list.map((b) => b.cy)) - 16;
    parts.push(
      `<text class="district-label" x="${cx.toFixed(1)}" y="${cy.toFixed(1)}" ` +
      `text-anchor="middle">${escapeHtml(name)}</text>`
    );
  }

  // Past this zoom the real outlines are big enough to read.
  const showFootprints = k >= FOOTPRINT_ZOOM;

  for (const b of state.projected) {
    const isFunded = isFunded_(b);
    const half = b.side / 2;
    const fill = isFunded ? 'var(--funded)' : districtColour(b.props.district);
    const cls = 'bldg' + (isFunded ? ' funded' : '') +
      (state.selected === b.id ? ' selected' : '');
    const title = `<title>${escapeHtml(b.props.code)} — ` +
      `${compact(b.props.annual_kwh)} kWh/yr${isFunded ? ' — funded' : ''}</title>`;

    if (showFootprints && b.ring.length >= 4) {
      const points = b.ring.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(' ');
      parts.push(
        `<polygon class="${cls}" data-id="${b.id}" points="${points}" ` +
        `fill="${fill}">${title}</polygon>`
      );
    } else {
      parts.push(
        `<rect class="${cls}" data-id="${b.id}" rx="2" ` +
        `x="${(b.cx - half).toFixed(1)}" y="${(b.cy - half).toFixed(1)}" ` +
        `width="${b.side.toFixed(1)}" height="${b.side.toFixed(1)}" ` +
        `fill="${fill}">${title}</rect>`
      );
    }
    if (b.props.anomalies > 0) {
      parts.push(
        `<circle class="anom-ring" cx="${b.cx.toFixed(1)}" cy="${b.cy.toFixed(1)}" ` +
        `r="${(half + 4).toFixed(1)}"/>`
      );
    }
  }

  parts.push('</g>');
  svg.innerHTML = parts.join('');

  svg.querySelectorAll('.bldg').forEach((el) => {
    el.addEventListener('click', () => selectBuilding(el.dataset.id));
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------------ interaction */

function attachPanZoom(signal) {
  const svg = $('map');
  let dragging = false, startX = 0, startY = 0, originX = 0, originY = 0;

  svg.addEventListener('mousedown', (e) => {
    dragging = true; svg.classList.add('dragging');
    startX = e.clientX; startY = e.clientY;
    originX = state.view.x; originY = state.view.y;
  });
  /* `signal` ties these to the current mount. Without it they survive every
   * navigation away and back, and the map ends up with one set per visit. */
  window.addEventListener('mouseup', () => { dragging = false; svg.classList.remove('dragging'); }, { signal });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    state.view.x = originX + (e.clientX - startX);
    state.view.y = originY + (e.clientY - startY);
    render();
  }, { signal });
  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    zoomAbout(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });

  /* The keyboard reaches the same state the wheel does. Listeners hang off the
   * svg rather than the window, so they leave with it - the map is already
   * mounted more than once per session. */
  svg.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 120 : 40;
    const keys = {
      ArrowLeft: () => { state.view.x += step; },
      ArrowRight: () => { state.view.x -= step; },
      ArrowUp: () => { state.view.y += step; },
      ArrowDown: () => { state.view.y -= step; },
      '+': () => zoomAbout(1.25),
      '=': () => zoomAbout(1.25),
      '-': () => zoomAbout(1 / 1.25),
      '0': resetView,
    };
    const action = keys[e.key];
    if (!action) return;
    e.preventDefault();
    action();
    render();
  });
}

/* Zooms about a point in the map's own coordinates, defaulting to the middle of
 * the viewport - which is what a zoom BUTTON should do, since there is no cursor
 * position to anchor to. */
function zoomAbout(factor, px, py) {
  const svg = $('map');
  const next = Math.min(MAX_ZOOM, Math.max(0.4, state.view.k * factor));
  const ax = px === undefined ? svg.clientWidth / 2 : px;
  const ay = py === undefined ? svg.clientHeight / 2 : py;
  state.view.x = ax - ((ax - state.view.x) * next) / state.view.k;
  state.view.y = ay - ((ay - state.view.y) * next) / state.view.k;
  state.view.k = next;
  render();
}

function resetView() {
  state.view = { x: 0, y: 0, k: 1 };
  render();
}

/* ------------------------------------------------------------------- data */

async function loadMap() {
  state.geo = await api.geojson(state.run?.run_id);
  buildGeometry();
  render();
}

function currentRequest() {
  const cap = $('cap').value;
  return {
    budget_egp: Number($('budget').value),
    objective: $('objective').value,
    solver: $('solver').value,
    max_funded_per_district: cap ? Number(cap) : null,
  };
}

async function solve() {
  setStatus('solving', 'warn');
  try {
    state.run = await api.optimize(currentRequest());
    showSummary(state.run);
    render();
    if (state.selected) selectBuilding(state.selected);
    setStatus(`${state.run.status.toLowerCase()} · ${Math.round(state.run.solve_ms)} ms`, 'ok');
  } catch (err) {
    setStatus(err.message, 'bad');
  }
}

function showSummary(run) {
  /* A live solve returns budget_used_frac and total_kwh_saving; a run read back
   * from storage carries neither, so both are derived rather than shown as NaN. */
  const spent = run.budget_used_frac ?? (run.budget_egp ? run.total_cost_egp / run.budget_egp : 0);
  const kwh = run.total_kwh_saving
    ?? (run.items || []).reduce((sum, i) => sum + (i.annual_kwh_saving || 0), 0);

  $('m-funded').textContent = run.buildings_funded;
  $('m-spent').textContent = (spent * 100).toFixed(0) + '%';
  $('m-kwh').textContent = compact(kwh);
  $('m-carbon').textContent = compact(run.total_benefit_kgco2e);
  $('m-solve').textContent = Math.round(run.solve_ms) + ' ms';
}

async function selectBuilding(id) {
  state.selected = id;
  render();

  let data;
  try {
    data = await api.buildingCandidates(id, state.run?.run_id);
  } catch {
    return;
  }

  const b = data.building;

  /* The options arrive ranked by the objective, so the position in the list is
   * information. Numbering it makes that explicit rather than leaving the
   * reader to count - and it means the chosen option can be seen NOT to be
   * rank 1, which happens under a district cap and is exactly the case worth
   * being able to interrogate. */
  const rows = data.options.slice(0, 12).map((o, i) => {
    const negative = o.lifetime_benefit_kgco2e <= 0;
    return `
    <div class="opt ${o.chosen ? 'chosen' : ''} ${negative ? 'negative' : ''}">
      <div class="head">
        <span class="name">${escapeHtml(o.label)}</span>
        ${o.chosen ? '<span class="tag">Allocated</span>'
          : negative ? '<span class="tag">Net negative</span>' : ''}
      </div>
      <div class="foot">
        <div class="row">
          <span>Cost: <b>${compact(o.cost_egp)} EGP</b></span>
          <span>Saves: <b>${compact(o.annual_kwh_saving)} kWh/yr</b></span>
          <span>Benefit: <b>${fmt(o.score_per_kegp, 0)} /kEGP</b></span>
        </div>
        <span class="rank">Rank #${i + 1}</span>
      </div>
    </div>`;
  }).join('');

  $('detail-empty').hidden = true;
  const body = $('detail-body');
  body.hidden = false;
  body.innerHTML = `
    <h2>${escapeHtml(b.name)}</h2>
    <div class="code">${escapeHtml(b.code)} · ${escapeHtml(b.district)}</div>
    <div class="facts">
      <div><span>Use</span>${escapeHtml(b.occupancy_pattern)}</div>
      <div><span>Insulation</span>${escapeHtml(b.insulation_quality)}</div>
      <div><span>HVAC age</span>${b.hvac_age_yr} yr</div>
      <div><span>Roof</span>${fmt(b.roof_area_m2)} m²</div>
      <div><span>Floor</span>${fmt(b.floor_area_m2)} m²</div>
      <div><span>Consumption</span>${compact(b.annual_kwh)} kWh/yr</div>
    </div>
    <h3 class="section-label">Options considered
      <span class="count">${data.options.length}</span></h3>
    <div class="opt-list">${rows || '<p class="note">No applicable interventions.</p>'}</div>`;
}

/* ---------------------------------------------------------------- compare */

async function compare() {
  const modal = $('compare-modal');
  modal.hidden = false;
  $('compare-body').textContent = 'Solving all four…';

  let data;
  try {
    data = await api.compare(currentRequest());
  } catch (error) {
    $('compare-body').innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
    return;
  }

  const names = {
    equal_split: 'Equal split (status quo)',
    greedy: 'Greedy heuristic',
    greedy_upgrade: 'Greedy + upgrade pass',
    cpsat: 'Exact optimization',
  };
  const order = ['equal_split', 'greedy', 'greedy_upgrade', 'cpsat'];
  const best = Math.max(...order.map((n) => data.results[n].total_benefit_kgco2e));

  const rows = order.map((n) => {
    const r = data.results[n];
    const delta = r.improvement_vs_equal_split_pct;
    return `<tr class="${r.total_benefit_kgco2e === best ? 'best' : ''}">
      <td>${names[n]}</td>
      <td>${r.buildings_funded}</td>
      <td>${compact(r.total_cost_egp)}</td>
      <td>${compact(r.total_benefit_kgco2e)}</td>
      <td>${delta === null ? 'baseline' : (delta > 0 ? '+' : '') + delta.toFixed(1) + '%'}</td>
      <td>${Math.round(r.solve_ms)} ms</td></tr>`;
  }).join('');

  // Quoted against greedy + upgrade, never against plain greedy. Plain greedy never
  // revisits a funded building, so above roughly 16 M EGP it stops spending and the
  // gap against it measures its ceiling rather than the value of exact optimization.
  const gap = data.cpsat_vs_greedy_upgrade_pct;
  const capped = data.max_funded_per_district;
  const plainStalled = data.results.greedy.total_cost_egp < data.results.greedy_upgrade.total_cost_egp * 0.9;

  $('compare-body').innerHTML = `
    <table>
      <thead><tr><th>Method</th><th>Funded</th><th>Spent EGP</th>
        <th>Lifetime kgCO₂e</th><th>vs status quo</th><th>Time</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="caption">
      Exact optimization beats the strongest heuristic by
      <strong>${gap > 0 ? '+' : ''}${gap.toFixed(1)}%</strong> here.
      ${capped
        ? `That gap is large because a per-district cap is active: no greedy variant
           can plan around a constraint that couples buildings across the portfolio.`
        : `Unconstrained, that gap is small and is reported rather than hidden — a
           good heuristic is near-optimal on a plain knapsack. Set a per-district cap
           to see where exact optimization earns its place.`}
      ${plainStalled
        ? `The plain greedy row is spending less than the others: it never revisits a
           funded building, so once each holds an option it stops, whatever budget is
           left. The comparison above is quoted against the upgrade pass instead.`
        : ''}
    </p>`;
}

/* -------------------------------------------------------------- narrative */

const IV_NAMES = {
  led_lighting_v1: 'LED lighting',
  hvac_controls_v1: 'BMS controls',
  insulation_roof_v1: 'Roof insulation',
  glazing_upgrade_v1: 'Double glazing',
  hvac_replacement_v1: 'HVAC replacement',
  rooftop_solar_v1: 'Rooftop solar',
};
const ivName = (id) => IV_NAMES[id] || id;

async function narrative() {
  const modal = $('narrative-modal');
  modal.hidden = false;
  $('narrative-body').textContent = 'Loading…';

  let data;
  try {
    data = await api.narrative();
  } catch {
    $('narrative-body').textContent = 'Not available for this catalog.';
    return;
  }

  const cards = data.buildings.map((b) => {
    const rows = b.options.slice(0, 5).map((o) => `
      <tr class="${o.intervention === b.best ? 'best' : ''}">
        <td>${ivName(o.intervention)}</td>
        <td>${compact(o.cost_egp)}</td>
        <td>${fmt(o.score_per_kegp, 0)}</td></tr>`).join('');
    return `
      <div class="card">
        <h3>${escapeHtml(b.code)}</h3>
        <p class="caption">
          ${escapeHtml(b.occupancy_pattern)} · ${escapeHtml(b.insulation_quality)} insulation ·
          HVAC ${b.hvac_age_yr} yr · ${compact(b.annual_kwh)} kWh/yr</p>
        <table><thead><tr><th>Measure</th><th>Cost EGP</th><th>Benefit /kEGP</th></tr></thead>
        <tbody>${rows}</tbody></table>
      </div>`;
  }).join('');

  const dist = Object.entries(data.distribution)
    .map(([k, n]) => `${ivName(k)} on ${n}`).join(', ');

  $('narrative-body').innerHTML = `
    <div class="card-row">${cards}</div>
    <p class="caption">
      Across the portfolio the best single measure is ${escapeHtml(dist)} — so no single
      priority list is right for every building, which is what per-building optimization
      is for. Note what does <em>not</em> happen: rooftop generation never has the best
      benefit density anywhere. Cheap controls and lighting dominate it, which is the
      ordinary efficiency-before-generation loading order, discovered from the catalog
      rather than assumed.
    </p>`;
}

/* -------------------------------------------------------------- lifecycle */

function setStatus(text, kind) {
  $('health-text').textContent = text;
  $('health-dot').className = 'dot ' + (kind || '');
}

async function checkHealth() {
  try {
    const res = await fetch('/health');
    const body = await res.json();
    if (body.database !== 'ok') throw new Error('database ' + body.database);
  } catch (err) {
    setStatus(err.message, 'bad');
  }
}

function wire(signal) {
  /* The solve controls only exist when the caller may solve. Zoom, pan and the
   * building detail are wired either way - reading the allocation is the part a
   * viewer is here for, and it is fully interactive for them. */
  if (canSolve) {
    const budget = $('budget');
    const showBudget = () => {
      $('budget-out').textContent = fmt(Number(budget.value)) + ' EGP';
    };
    showBudget();

    // Debounced so dragging the slider does not queue a solve per pixel; 120 ms is
    // below the threshold where the map stops feeling attached to the control.
    const solveSoon = debounce(solve, 120);
    budget.addEventListener('input', () => { showBudget(); solveSoon(); });

    ['objective', 'solver', 'cap'].forEach((id) => $(id).addEventListener('change', solve));

    wireModal('compare-modal', 'compare-close', $('compare-btn'), compare);
    wireModal('narrative-modal', 'narrative-close', $('narrative-btn'), narrative);
  }

  $('zoom-in').addEventListener('click', () => zoomAbout(1.25));
  $('zoom-out').addEventListener('click', () => zoomAbout(1 / 1.25));
  $('zoom-reset').addEventListener('click', resetView);

  window.addEventListener('resize', debounce(() => { buildGeometry(); render(); }, 150),
                          { signal });
}

/* Open, dismiss and - the part that was missing - focus.
 *
 * Both modals used to open while focus stayed on the button behind the scrim,
 * so Tab walked the page underneath and Escape did nothing. Moving focus into
 * the dialog fixes the dismissal too: the key event bubbles from inside, so
 * Escape needs no document-level listener to leak. */
function wireModal(modalId, closeId, opener, onOpen) {
  const modal = $(modalId);
  const inner = modal.querySelector('.modal-inner');

  const close = () => {
    modal.hidden = true;
    if (opener && opener.isConnected) opener.focus();
  };

  opener.addEventListener('click', async () => {
    await onOpen();
    inner.focus();
  });
  $(closeId).addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  modal.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); }
  });
}

async function main(signal) {
  wire(signal);
  attachPanZoom(signal);
  await checkHealth();
  await loadMap();

  if (canSolve) {
    await solve();
  } else {
    await showStoredRun();
  }

  await loadMap();          // reload so anomaly rings and funded state agree
}

/* The read-only path: the newest stored allocation, as decided.
 *
 * `/dashboard/runs` and `/runs/{id}` are both viewer-readable, which is the whole
 * point - the decisions are public to anyone who may see the portfolio, only making
 * new ones is restricted.
 */
async function showStoredRun() {
  setStatus('loading the last allocation', 'warn');
  try {
    const runs = await api.runs();
    if (!runs.length) {
      setStatus('no allocation stored yet', 'warn');
      const note = $('readonly-note');
      if (note) {
        note.textContent = 'No optimizer run has been stored yet. An analyst has to '
                         + 'run one before there is anything to show here.';
      }
      return;
    }

    state.run = await api.get(`/runs/${encodeURIComponent(runs[0].run_id)}`);
    showSummary(state.run);
    showRunFacts(state.run);
    render();
    setStatus(`stored run · ${state.run.status.toLowerCase()}`, 'ok');
  } catch (err) {
    setStatus(err.detail || err.message, 'bad');
  }
}

function showRunFacts(run) {
  const facts = $('run-facts');
  if (!facts) return;
  const when = (run.created_at || '').replace('T', ' ').slice(0, 16);
  facts.innerHTML = `
    <div><dt>Decided</dt><dd>${escapeHtml(when)}</dd></div>
    <div><dt>Budget</dt><dd>${compact(run.budget_egp)} EGP</dd></div>
    <div><dt>Ranked by</dt><dd>${escapeHtml(run.objective)}</dd></div>
    <div><dt>Method</dt><dd>${escapeHtml(run.solver)}</dd></div>`;
}

