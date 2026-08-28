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
import { mark } from './charts.js';
import { locale, t } from './i18n.js';
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
/* Three regions: the controls that decide the allocation, the map that shows
 * it, and the evidence for the one building you asked about. The evidence lives
 * INSIDE this screen rather than on a recommendations screen of its own,
 * because "why that building?" is a question about the solve currently on
 * screen and splitting the two would split the argument in half. */
const MAP_HTML = () => String.raw`<!-- ---------------------------------------------------------------- -->
  <section class="panel map-controls" aria-labelledby="ctl-h">
    <header><h3 id="ctl-h">${t('map.controls')}</h3></header>
    <div class="control-body">

      <div class="field">
        <label for="budget">${t('map.budget')}</label>
        <output id="budget-out" for="budget">10,000,000</output>
        <div class="field-unit" id="budget-unit">EGP</div>
        <input type="range" id="budget" min="1000000" max="30000000" step="250000"
               value="10000000" aria-valuetext="10,000,000 Egyptian pounds">
        <div class="scale"><span>1,000,000</span><span>30,000,000</span></div>
        <p class="note" id="solve-note">Every change re-solves against all fifty
        buildings. Nothing is precomputed and the slider does not snap.</p>
      </div>

      <fieldset class="field">
        <legend>${t('map.rankBy')}</legend>
        <div class="radiogroup" role="radiogroup" aria-label="${t('map.rankBy')}" id="objective">
          <button type="button" role="radio" data-value="lca_carbon" aria-checked="true">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text"><span class="radio-label">Life-cycle carbon (kgCO&#8322;e)</span></span>
          </button>
          <button type="button" role="radio" data-value="tou_carbon" aria-checked="false">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text"><span class="radio-label">TOU carbon — measured shape (kgCO&#8322;e)</span></span>
          </button>
          <button type="button" role="radio" data-value="raw_kwh" aria-checked="false">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text"><span class="radio-label">First-year energy (kWh)</span></span>
          </button>
          <button type="button" role="radio" data-value="egp_saved" aria-checked="false">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text"><span class="radio-label">First-year money (EGP)</span></span>
          </button>
        </div>
      </fieldset>

      <!-- FOUR of them, and the copy must never say otherwise: equal split is
           one of the four, and it is the status quo the rest are measured
           against. A test greps this directory for the wrong number, which is
           also why this comment does not spell it out. -->
      <fieldset class="field">
        <legend>${t('map.method')}</legend>
        <div class="radiogroup" role="radiogroup" aria-label="${t('map.method')}" id="solver">
          <button type="button" role="radio" data-value="cpsat" aria-checked="true">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text">
              <span class="radio-label">Exact optimization (CP-SAT)</span>
              <span class="radio-note">Solves the knapsack exactly. The one that earns
                its time under a district cap.</span>
            </span>
          </button>
          <button type="button" role="radio" data-value="greedy_upgrade" aria-checked="false">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text">
              <span class="radio-label">Greedy plus an upgrade pass</span>
              <span class="radio-note">Best benefit per pound first, then spends what is
                left upgrading what it already picked.</span>
            </span>
          </button>
          <button type="button" role="radio" data-value="greedy" aria-checked="false">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text">
              <span class="radio-label">Plain greedy</span>
              <span class="radio-note">Takes the best ratio until the money runs out.
                Saturates, and stops.</span>
            </span>
          </button>
          <button type="button" role="radio" data-value="equal_split" aria-checked="false">
            <span class="radio-mark" aria-hidden="true"></span>
            <span class="radio-text">
              <span class="radio-label">Equal split &mdash; the status quo</span>
              <span class="radio-note">An equal share to every building. Not a
                contender; it is what the other three are argued against.</span>
            </span>
          </button>
        </div>
      </fieldset>

      <div class="field">
        <label for="cap">${t('map.districtCap')}</label>
        <span class="select-wrap"><select id="cap">
          <option value="">No cap</option>
          <option value="2">2 per district</option>
          <option value="4">4 per district</option>
          <option value="6">6 per district</option>
        </select></span>
      </div>

      <div class="map-actions">
        <button id="compare-btn" type="button" class="primary">${t('map.compare')}</button>
        <button id="narrative-btn" type="button" class="secondary">${t('map.whyBuildingSpecific')}</button>
      </div>
      <p class="note" id="map-status" role="status"></p>
    </div>
  </section>

  <!-- ---------------------------------------------------------------- -->
  <section class="panel mapwrap" aria-labelledby="map-h">
    <header>
      <h3 id="map-h">The map &middot; New Cairo</h3>
      <div class="map-legend">
        <span class="key"><svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true"><rect x="1.5" y="1.5" width="11" height="11" fill="var(--accent)" stroke="var(--accentLine)" stroke-width="1.4"/><path d="M4 7.2l2 2 4-4.4" fill="none" stroke="var(--accentInk)" stroke-width="1.4"/></svg>Funded</span>
        <span class="key"><svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true"><rect x="1.5" y="1.5" width="11" height="11" fill="var(--bldg)" stroke="var(--lineStrong)" stroke-width="1.4"/></svg>Not funded</span>
        <span class="key"><svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true"><rect x="3.5" y="3.5" width="7" height="7" fill="var(--bldg)" stroke="var(--lineStrong)" stroke-width="1.4"/><path d="M7 0l3 5H4Z" fill="var(--crit)"/></svg>Open alert</span>
        <span class="key"><svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true"><rect x="2.5" y="2.5" width="9" height="9" fill="var(--bldg)" stroke="var(--ink)" stroke-width="1.6"/></svg>Selected</span>
      </div>
    </header>

    <div class="mapstage">
      <!-- The result of the solve, over the thing it decided. A bar above the
           map put the answer and the picture in two different places; here the
           eye lands on both at once. -->
      <div class="results" id="summary">
        <div class="result"><span class="v">buildings funded</span><span class="k" id="m-funded">&mdash;</span></div>
        <div class="result"><span class="v">of budget spent</span><span class="k" id="m-spent">&mdash;</span></div>
        <div class="result"><span class="v">kWh saved / yr</span><span class="k" id="m-kwh">&mdash;</span></div>
        <div class="result"><span class="v">kgCO&#8322;e lifetime</span><span class="k" id="m-carbon">&mdash;</span></div>
        <div class="result"><span class="v">solve time</span><span class="k" id="m-solve">&mdash;</span></div>
      </div>

      <svg id="map" tabindex="0" role="group" aria-describedby="map-help"
           aria-label="Schematic map of 50 costed buildings over the New Cairo street network"></svg>

      <!-- Zoom was a mouse wheel and nothing else, which left the map unusable
           from a keyboard and awkward on a trackpad in front of a room. The
           buttons and the arrow keys reach the same state the wheel does. -->
      <div class="map-zoom" role="group" aria-label="Zoom">
        <button type="button" id="zoom-in" aria-label="Zoom in">
          <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg>
        </button>
        <button type="button" id="zoom-out" aria-label="Zoom out">
          <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><path d="M4 10h12"/></svg>
        </button>
        <button type="button" id="zoom-reset" aria-label="Fit the whole area">
          <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><path d="M4 8V4h4M16 12v4h-4M16 8V4h-4M4 12v4h4"/></svg>
        </button>
        <button type="button" id="zoom-centre" aria-label="Centre on the selected building" disabled>
          <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><path d="M10 5v10M5 10h10"/><circle cx="10" cy="10" r="4"/></svg>
        </button>
      </div>

      <div class="map-scale">
        <span class="map-scale-read">
          <span id="scale-label">&mdash;</span>
          <span class="map-scale-bar"></span>
        </span>
        <span class="map-zoom-factor">zoom <span id="zoom-label">1.0&times;</span></span>
      </div>
    </div>

    <!-- Stated, not discovered. Nothing on this map is reachable only by
         hovering, and a keyboard binding nobody is told about is a keyboard
         binding nobody uses. -->
    <footer class="map-foot" id="map-help">
      <span>Keyboard: arrows pan, <kbd>+</kbd> / <kbd>&minus;</kbd> zoom,
        <kbd>0</kbd> fits, Tab steps building to building.</span>
      <span class="map-foot-note">Squares are sized by annual kWh below 3&times; zoom,
        then become true OpenStreetMap footprints.</span>
    </footer>
  </section>

  <!-- ---------------------------------------------------------------- -->
  <section class="panel detail" aria-labelledby="sel-h">
    <header>
      <h3 id="sel-h">Selected building</h3>
      <button type="button" class="secondary" id="sel-clear" hidden>Clear selection</button>
    </header>

    <!-- Fifty entries is too many to scroll and too few to page, so it filters
         across code, name and district and says so. -->
    <div class="picker-wrap">
      <label for="pick">Find a building &mdash; 50 of them, so type to filter</label>
      <div class="combobox">
        <input id="pick" type="text" role="combobox" aria-expanded="false"
               aria-controls="picklist" aria-autocomplete="list" autocomplete="off"
               placeholder="Code, name or district">
        <ul id="picklist" role="listbox" aria-label="Buildings" hidden></ul>
      </div>
    </div>

    <div class="empty-state" id="detail-empty">
      <h4>Nothing selected yet</h4>
      <p>Pick a building on the map, or use the filter above. This panel then shows its
         six costed facts and every retrofit option the optimizer weighed for it &mdash;
         ranked, with the rejected ones still visible.</p>
    </div>
    <div id="detail-body" hidden></div>
  </section>`;

/* `tabindex="-1"` so the dialog itself can take focus when it opens. That is
 * what lets Escape work without a document-level listener - the key event
 * bubbles from inside the modal - and it is also what stops a keyboard user
 * being left behind on the button that opened it. */
const MODAL_HTML = String.raw`<div id="narrative-modal" class="modal" hidden>
  <div class="modal-inner wide" tabindex="-1" role="dialog" aria-modal="true"
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
      ${pageHead({ root, title: t('map.title') })}
      <div class="map-layout">${MAP_HTML()}</div>${MODAL_HTML}`;

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
    <header><h3>Most recent stored allocation</h3></header>
    <div class="control-body">
      <dl class="facts" id="run-facts"></dl>
      <p class="note" id="readonly-note">The map below is that allocation exactly as it
      was solved &mdash; not a fresh one. Running the optimizer needs the analyst role.</p>
      <p class="note" id="map-status" role="status"></p>
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
    : n.toLocaleString(locale(), { maximumFractionDigits: digits });

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
  /* A hidden or zero-width panel would make `scale` negative - (0 - 120) / spanX -
   * and every building would project off the canvas, mirrored. Nothing reaches
   * that today, but the debounced resize handler recomputes on any window change,
   * including a minimise, and the failure is silent: the map simply goes blank and
   * stays blank until something resizes it again. A floor costs one comparison. */
  const W = Math.max(svg.clientWidth, 2 * PAD + 1);
  const H = Math.max(svg.clientHeight, 2 * PAD + 1);
  const p = project(state.geo);

  /* Buildings occupy a tiny fraction of a district's area, so drawing footprints
   * at true scale yields fifty invisible specks. They are drawn as squares of
   * area proportional to consumption, centred on the real centroid: position is
   * geographic, size encodes the quantity the decision is about. */
  const scale = Math.min((W - 2 * PAD) / p.spanX, (H - 2 * PAD) / p.spanY);
  const consumptions = state.geo.features.map((f) => f.properties.annual_kwh || 0);
  const maxKwh = Math.max(...consumptions, 1);

  /* Centre what is left over.
   *
   * The scale is the smaller of the two fits, so one axis is filled and the
   * other has room to spare - and anchoring both at PAD pushes the whole
   * district into a corner. New Cairo is wider than it is tall against a panel
   * that is taller than it is wide, which left the right-hand third of the map
   * as empty ground. */
  const offsetX = (W - p.spanX * scale) / 2;
  const offsetY = (H - p.spanY * scale) / 2;

  const toScreen = ([lon, lat]) => [
    offsetX + (lon - p.minLon) * p.kx * scale,
    offsetY + (p.maxLat - lat) * scale,          // screen y grows downward
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

  /* Metres per screen pixel at k = 1, for the scale bar. One degree of latitude
   * is 111,320 m closely enough over a district. */
  state.metresPerPx = 111320 / scale;

  buildContext(toScreen);
}

/* ------------------------------------------------------------ context layer */

/* The streets and neighbouring footprints the portfolio sits among, projected
 * with the SAME transform as the portfolio so the two layers agree.
 *
 * Built once per geometry pass and kept as a string. There are about 2,600
 * features in it: rebuilding that markup on every pointer move would make the
 * map unusable, which is why panning now moves the group's transform instead of
 * redrawing (see attachPanZoom).
 */
function buildContext(toScreen) {
  if (!state.context) { state.contextMarkup = ''; return; }

  const parts = [];
  const water = [], roads = [], buildings = [];

  for (const feature of state.context.features) {
    const kind = feature.properties?.kind;
    const coords = feature.geometry?.coordinates;
    if (!coords) continue;

    if (kind === 'road') {
      const d = coords.map((point, i) => {
        const [x, y] = toScreen(point);
        return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
      }).join('');
      const weight = feature.properties.weight || 1;
      // Classed by weight so the stylesheet can drop the minor roads at low
      // zoom, where they are a grey wash rather than information.
      const rank = weight >= 2.4 ? 'major' : weight >= 1.4 ? 'mid' : 'minor';
      roads.push(`<path class="ctx-road ${rank}" d="${d}"/>`);
    } else {
      const points = (coords[0] || []).map((point) => {
        const [x, y] = toScreen(point);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(' ');
      if (!points) continue;
      const markup = `<polygon class="ctx-${kind}" points="${points}"/>`;
      if (kind === 'building') buildings.push(markup);
      else water.push(markup);
    }
  }

  // Painted in this order: ground, then blocks, then streets over both.
  parts.push('<g class="ctx" aria-hidden="true">', water.join(''),
             buildings.join(''), roads.join(''), '</g>');
  state.contextMarkup = parts.join('');
}

async function loadContext() {
  /* Served from this origin as a static file. A tile from a map provider would
   * be an off-origin request - blocked by the Content-Security-Policy, and dead
   * on a demonstration machine with the cable out (F13). The surroundings ship
   * with the application instead.
   *
   * Missing or unreadable is not an error worth stopping for: the map then
   * draws exactly what it drew before this layer existed. */
  if (state.context) return;
  try {
    const response = await fetch('data/context.geojson', { credentials: 'same-origin' });
    if (!response.ok) return;
    state.context = await response.json();
  } catch {
    state.context = null;
  }
}

/* One hue per district, from the name, so the districts stay visually separate
 * without a palette anyone has to maintain. Saturation and lightness come from
 * tokens rather than being fixed here: at 30% lightness these were dark grey
 * blocks, which is right on a dark ground and unreadable on a light one. */
/* Which retrofit the current allocation bought at each building, so the map can
 * say it out loud in the mark's accessible name. A live solve carries it on the
 * run's items; a run read back through /geojson carries it on the feature. */
function fundedLabels() {
  return new Map((state.run?.items || [])
    .filter((i) => i.building_id)
    .map((i) => [i.building_id, i.label]));
}

/* Two states and no third. Funded is the accent with a check cut into it; not
 * funded is --bldg behind the strong line. The map used to tint each building
 * by its district, which put five hues on screen competing with the one
 * distinction the screen exists to draw - and the district is already written
 * across the cluster in a label. */
function render() {
  const svg = $('map');
  const { x, y, k } = state.view;
  /* A run fetched from /runs/{id} carries building_code but not building_id, so
   * fall back to the `funded` flag the server already put on each feature when
   * the geojson was requested with that run. */
  const labels = fundedLabels();
  const isFunded_ = (b) => labels.has(b.id) || b.props.funded === true;

  /* Everything inside the group is multiplied by k, so a glyph that should stay
   * the same size on screen has to be divided by it first. This is the whole
   * trick behind "stroke widths and label sizes are recomputed per zoom so they
   * stay optically constant" - strokes get it from vector-effect, geometry gets
   * it from here. */
  const s = 1 / k;

  /* The zoom band drives which context detail is drawn, through CSS rather
   * than by rebuilding the markup: at the opening zoom the minor roads are a
   * grey wash that hides the streets someone would actually navigate by. */
  svg.dataset.zoom = k >= 6 ? 'close' : k >= 2.5 ? 'mid' : 'far';
  updateScaleBar();

  const parts = [`<g id="map-root" transform="translate(${x},${y}) scale(${k})">`,
                 state.contextMarkup || ''];

  /* District labels, one per cluster, uppercase.
   *
   * The five districts interleave geographically, so their centroids collide -
   * two labels land on top of each other and neither can be read. Colliding
   * labels are pushed UPWARD until they clear, which is cheap, stable between
   * frames, and keeps the label over its own cluster. */
  const byDistrict = new Map();
  for (const b of state.projected) {
    const list = byDistrict.get(b.props.district) || [];
    list.push(b);
    byDistrict.set(b.props.district, list);
  }
  const placed = [];
  for (const [name, list] of byDistrict) {
    const cx = list.reduce((sum, b) => sum + b.cx, 0) / list.length;
    let cy = Math.min(...list.map((b) => b.cy)) - 22 * s;
    let guard = 0;
    while (guard++ < 8 && placed.some((q) =>
      Math.abs(q.y - cy) < 20 * s && Math.abs(q.x - cx) < 105 * s)) cy -= 26 * s;
    placed.push({ x: cx, y: cy });
    parts.push(
      `<text class="district-label" x="${cx.toFixed(1)}" y="${cy.toFixed(1)}" ` +
      `font-size="${(11 * s).toFixed(2)}" letter-spacing="${(1.3 * s).toFixed(2)}" ` +
      `text-anchor="middle">${escapeHtml(name).toUpperCase()}</text>`
    );
  }

  // Past this zoom the real outlines are big enough to read.
  const showFootprints = k >= FOOTPRINT_ZOOM;

  for (const b of state.projected) {
    const isFunded = isFunded_(b);
    const selected = state.selected === b.id;
    const half = b.side / 2;
    const cls = 'bldg' + (isFunded ? ' funded' : '');

    /* One focusable group per building, not a focusable shape: the mark is up
     * to four paths - the outline, the check, the alert triangle and the
     * selection ring - and they have to be one stop, with one name, that
     * announces the whole state.
     *
     * An outline does not render on an SVG <g>, which is why .bldg-hit draws
     * its own focus stroke in the stylesheet rather than relying on the ring
     * every other control in this application uses. */
    const what = isFunded
      ? `Funded: ${labels.get(b.id) || b.props.label || 'a retrofit'}`
      : 'Not funded';
    const alert = b.props.anomalies > 0
      ? `. ${b.props.anomalies} open alert${b.props.anomalies === 1 ? '' : 's'}`
      : '';
    const aria = `${b.props.name}, ${b.props.code}, ${b.props.district}. ` +
      `${compact(b.props.annual_kwh)} kWh a year. ${what}${alert}`;

    parts.push(`<g class="bldg-hit${selected ? ' selected' : ''}" tabindex="0" ` +
      `role="button" aria-pressed="${selected}" data-id="${escapeHtml(b.id)}" ` +
      `aria-label="${escapeHtml(aria)}">`);

    if (showFootprints && b.ring.length >= 4) {
      const points = b.ring.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(' ');
      parts.push(`<polygon class="${cls}" points="${points}"/>`);
    } else {
      parts.push(
        `<rect class="${cls}" rx="${(1.5 * s).toFixed(2)}" ` +
        `x="${(b.cx - half).toFixed(1)}" y="${(b.cy - half).toFixed(1)}" ` +
        `width="${b.side.toFixed(1)}" height="${b.side.toFixed(1)}"/>`
      );
    }

    /* The check is cut into the funded mark in --accentInk, so funded reads as
     * funded in greyscale and to a colourblind reader. */
    if (isFunded) {
      const g = Math.min(half * 0.42, 9 * s);
      parts.push(
        `<path class="bldg-check" d="M${(b.cx - g).toFixed(1)} ${b.cy.toFixed(1)}` +
        `l${(g * 0.8).toFixed(1)} ${(g * 0.9).toFixed(1)}` +
        `L${(b.cx + g).toFixed(1)} ${(b.cy - g * 0.9).toFixed(1)}"/>`
      );
    }

    /* Pinned ABOVE the mark rather than ringed around it. A ring around a
     * building that is itself a square reads as a second building at low zoom,
     * and it grew and shrank with the footprint; the triangle is the same
     * critical shape the alert inbox uses and it stays the same size. */
    if (b.props.anomalies > 0) {
      const t = 4.5 * s;
      parts.push(
        `<path class="bldg-alert" d="M${b.cx.toFixed(1)} ${(b.cy - half - 3.5 * s).toFixed(1)}` +
        `l${t.toFixed(1)} ${(t * 1.55).toFixed(1)}h${(-t * 2).toFixed(1)}Z"/>`
      );
    }

    if (selected) {
      const pad = 5 * s;
      parts.push(
        `<rect class="bldg-ring" x="${(b.cx - half - pad).toFixed(1)}" ` +
        `y="${(b.cy - half - pad).toFixed(1)}" ` +
        `width="${(b.side + pad * 2).toFixed(1)}" height="${(b.side + pad * 2).toFixed(1)}"/>`
      );
    }

    parts.push('</g>');
  }

  parts.push('</g>');

  /* Every render replaces the fifty marks, and one of them may be the element
   * that currently has focus - selecting a building IS a render, and selection
   * follows focus. Without this, Tab-stepping the map moves focus to a building,
   * redraws, and drops focus to the document body: the next Tab starts again
   * from the top of the page. */
  const focused = document.activeElement?.closest?.('.bldg-hit')?.dataset.id;
  svg.innerHTML = parts.join('');

  /* Selection follows focus as well as click, so Tab-stepping the map reveals
   * each building's evidence rather than requiring a second key to ask for it.
   * That is the whole reason the marks are focusable. */
  svg.querySelectorAll('.bldg-hit').forEach((el) => {
    el.addEventListener('click', () => selectBuilding(el.dataset.id));
    el.addEventListener('focus', () => {
      if (state.selected !== el.dataset.id) selectBuilding(el.dataset.id);
    });
  });

  if (focused) {
    svg.querySelector(`.bldg-hit[data-id="${CSS.escape(focused)}"]`)?.focus();
  }
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
    /* Move the group, do not rebuild it. The context layer is about 2,600
     * paths; regenerating that markup on every pointer move made the map
     * unusable to drag. A pan changes nothing about WHAT is drawn, only where,
     * so the transform is the whole update. */
    applyTransform();
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
    /* 8% of the viewport per press, which is a pan that means the same thing at
     * every zoom - 40 fixed pixels is a third of the screen when zoomed out and
     * an imperceptible nudge when zoomed in. */
    const stepX = svg.clientWidth * (e.shiftKey ? 0.24 : 0.08);
    const stepY = svg.clientHeight * (e.shiftKey ? 0.24 : 0.08);
    const keys = {
      ArrowLeft: () => { state.view.x += stepX; applyTransform(); },
      ArrowRight: () => { state.view.x -= stepX; applyTransform(); },
      ArrowUp: () => { state.view.y += stepY; applyTransform(); },
      ArrowDown: () => { state.view.y -= stepY; applyTransform(); },
      '+': () => zoomAbout(ZOOM_STEP),
      '=': () => zoomAbout(ZOOM_STEP),
      '-': () => zoomAbout(1 / ZOOM_STEP),
      '0': resetView,
    };
    const action = keys[e.key];
    if (!action) return;
    e.preventDefault();
    action();
  });
}

/* A pan is a transform change and nothing else. Zoom still goes through
 * render(), because the zoom band decides which context detail is drawn and
 * whether footprints replace the consumption squares. */
function applyTransform() {
  const group = document.getElementById('map-root');
  const { x, y, k } = state.view;
  if (group) group.setAttribute('transform', `translate(${x},${y}) scale(${k})`);
  updateScaleBar();
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

/* The bar is a fixed 80 screen pixels wide and the LABEL says what that is
 * worth at the current zoom.
 *
 * The usual trick is the other way round - a round number like "500 m" and a
 * bar stretched to match - but that would mean writing a width from JavaScript,
 * and an inline style is discarded under `style-src 'self'`. A fixed bar with a
 * measured label is the same information and survives the policy. Two
 * significant figures, because the third would be claiming a precision an
 * equirectangular projection does not have. */
function updateScaleBar() {
  const label = $('scale-label');
  if (!label || !state.metresPerPx) return;
  const metres = (80 / state.view.k) * state.metresPerPx;
  const round2 = (v) => {
    const power = Math.pow(10, Math.floor(Math.log10(v)) - 1);
    return Math.round(v / power) * power;
  };
  const value = round2(metres);
  label.textContent = value >= 1000
    ? `${(value / 1000).toFixed(value % 1000 ? 1 : 0)} km`
    : `${Math.round(value)} m`;

  /* The distance the bar is worth answers "how big is that building"; the
   * factor answers "how far in am I, and is that why the footprints appeared".
   * They are different questions and the footprint switch at 3x makes the
   * second one worth answering. */
  const zoom = $('zoom-label');
  if (zoom) zoom.textContent = `${state.view.k.toFixed(1)}×`;
}

/* 1.6x a press. The buttons, the keyboard and the wheel all land on the same
 * ladder of zoom levels, so pressing + twice and scrolling twice arrive at the
 * same place. */
const ZOOM_STEP = 1.6;

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
    objective: groupValue('objective'),
    solver: groupValue('solver'),
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

/* Nothing selected: the panel goes back to saying what it will show, and the
 * centre button goes back to being unusable rather than silently doing nothing. */
function clearSelection() {
  state.selected = null;
  $('detail-body').hidden = true;
  $('detail-empty').hidden = false;
  $('sel-clear').hidden = true;
  $('zoom-centre').disabled = true;
  render();
}

async function selectBuilding(id) {
  state.selected = id;
  $('sel-clear').hidden = false;
  $('zoom-centre').disabled = false;
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
  /* Every option, not a top-N slice.
   *
   * The claim this product makes is that the best retrofit is building-specific, and
   * that claim is only arguable if a sceptic can see what LOST. Truncating the list
   * would leave the panel showing a recommendation and asking to be believed. Rank is
   * still numbered, so a chosen option that is not rank 1 - which happens under a
   * district cap, and is exactly the case worth interrogating - is visible as such. */
  const rows = data.options.map((o, i) => {
    const negative = o.lifetime_benefit_kgco2e <= 0;
    const life = o.service_life_yr
      ? `${o.service_life_yr} yr life`
      : 'life not stated';
    return `
    <tr class="${o.chosen ? 'is-chosen' : ''} ${negative ? 'is-negative' : ''}">
      <td class="opt-name">
        <span class="opt-mark" aria-hidden="true">${o.chosen ? mark('check', { size: 12 }) : negative ? mark('cross', { size: 12 }) : mark('dash', { size: 12 })}</span>
        <span class="opt-label">${escapeHtml(o.label)}</span>
        <span class="opt-sub">#${i + 1} &middot; ${escapeHtml(life)} &middot;
          ${compact(o.lifetime_benefit_kgco2e)} kgCO&#8322;e over its lifetime</span>
        ${negative ? `<span class="opt-why">Embodied carbon exceeds the saving, so this
          can never be chosen.</span>` : ''}
      </td>
      <td class="num">${compact(o.cost_egp)}</td>
      <td class="num">${compact(o.annual_kwh_saving)}</td>
      <td class="num strong">${fmt(o.score_per_kegp, 0)}</td>
    </tr>`;
  }).join('');

  const anomalies = anomalyCount(b.id);
  const chosen = data.options.find((o) => o.chosen);

  /* Why THIS option won here, in the building's own terms.
   *
   * A ranked table says which option scored highest; it does not say what about this
   * building made it score highest. Naming the two attributes that actually drive the
   * condition multipliers - HVAC age and insulation quality - is what turns the table
   * from an assertion into an argument, and it is the sentence the whole
   * building-specific claim rests on. */
  const why = chosen
    ? `<p class="detail-why">${escapeHtml(chosen.label)} wins here because this
       building has a ${b.hvac_age_yr}-year-old ${escapeHtml(pretty(b.hvac_type) || 'HVAC')}
       system and ${escapeHtml(b.insulation_quality)} insulation. Change the budget or the
       district cap and this row can change &mdash; the ranking is per building, not a
       portfolio-wide priority list.</p>`
    : `<p class="detail-why">Nothing was funded here under the current budget, method
       and cap. The ranking below is still what the optimizer weighed.</p>`;

  $('detail-empty').hidden = true;
  const body = $('detail-body');
  body.hidden = false;
  body.innerHTML = `
    <div class="detail-head">
      <h2>${escapeHtml(b.name)}</h2>
      <span class="chip ${chosen ? 'funded' : 'unfunded'}">
        <span aria-hidden="true">${chosen ? mark('check', { size: 12 }) : mark('dash', { size: 12 })}</span>
        ${chosen ? 'Funded' : 'Not funded'}</span>
    </div>
    <div class="code">${escapeHtml(b.code)} &middot; ${escapeHtml(b.district)}</div>

    ${anomalies ? `<p class="detail-alert" role="note">
      <span aria-hidden="true">${mark('critical', { size: 12 })}</span>
      ${anomalies} open ${anomalies === 1 ? 'alert' : 'alerts'} on this meter. Its
      metered consumption is what every figure below is costed against, so treat them
      as provisional until the alerts are resolved.</p>` : ''}

    <div class="facts">
      <div><span>Use</span>${escapeHtml(pretty(b.occupancy_pattern))}</div>
      <div><span>Insulation</span>${escapeHtml(pretty(b.insulation_quality))}</div>
      <div><span>HVAC age</span>${b.hvac_age_yr} yr${b.hvac_type ? ` &middot; ${escapeHtml(pretty(b.hvac_type))}` : ''}</div>
      <div><span>Roof</span>${fmt(b.roof_area_m2)} m&sup2;</div>
      <div><span>Floor</span>${fmt(b.floor_area_m2)} m&sup2;</div>
      <div><span>Annual energy</span>${compact(b.annual_kwh)} kWh/yr</div>
    </div>

    <h3 class="section-label">Every option the optimizer weighed
      <span class="count">${data.options.length}</span></h3>
    <p class="detail-method">Benefit is kWh saved &times; the option&rsquo;s lifetime
      &times; ${GRID_FACTOR} kgCO&#8322;e/kWh, less the embodied carbon of the works.
      These are estimates, not measured savings.</p>

    <div class="opt-table-wrap">
      <table class="opt-table">
        <thead><tr>
          <th scope="col">Option</th>
          <th scope="col" class="num">Cost EGP</th>
          <th scope="col" class="num">kWh/yr</th>
          <th scope="col" class="num">Benefit /k EGP</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="4">No applicable interventions.</td></tr>'}</tbody>
      </table>
    </div>
    ${why}`;
}

/* The grid emission factor the benefit figures are built on. Stated on screen rather
 * than left implicit, because it is one of the numbers still awaiting a citation and
 * a reader is entitled to see which constant a recommendation rests on. */
const GRID_FACTOR = '0.45';

/* Open alerts for one building, read from the geometry the map already holds rather
 * than re-fetched: the map's own anomaly rings are drawn from this, so the panel and
 * the marks can never disagree about whether a meter is under question. */
function anomalyCount(buildingId) {
  const feature = (state.geo?.features || [])
    .find((f) => f.properties.id === buildingId);
  return feature ? (feature.properties.anomalies || 0) : 0;
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

  const chosen = groupValue('solver');
  const request_budget = Number($('budget').value);

  const rows = order.map((n) => {
    const r = data.results[n];
    const delta = r.improvement_vs_equal_split_pct;
    /* Exact optimization carries the check because it is the one that is
     * provably right, not because it happens to win this solve; the row that is
     * currently selected in the controls is accent-soft, so a reader can see
     * both "what is best" and "what you are looking at" at once. */
    const cls = [n === 'cpsat' ? 'best' : '', n === chosen ? 'chosen' : ''].join(' ').trim();
    /* Three different things, and they used to print as one word.
     *
     * `equal_split` IS the baseline and has nothing to compare against. Every
     * other row has a number - unless the baseline scored zero, in which case the
     * improvement is infinite and there is no percentage to write. Rendering all
     * three as "baseline" claimed that exact optimization was the status quo. */
    const versus = n === 'equal_split' ? 'baseline'
      : delta === null ? '&infin; &mdash; the status quo funded nothing'
      : `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`;
    return `<tr class="${cls}">
      <td>${n === 'cpsat' ? `${mark('check', { size: 12 })} ` : ''}${names[n]}</td>
      <td>${r.buildings_funded}</td>
      <td>${compact(r.total_cost_egp)}</td>
      <td>${compact(r.total_benefit_kgco2e)}</td>
      <td>${versus}</td>
      <td>${Math.round(r.solve_ms)} ms</td></tr>`;
  }).join('');

  // Quoted against greedy + upgrade, never against plain greedy. Plain greedy never
  // revisits a funded building, so above roughly 16 M EGP it stops spending and the
  // gap against it measures its ceiling rather than the value of exact optimization.
  const gap = data.cpsat_vs_greedy_upgrade_pct;
  const capped = data.max_funded_per_district;
  const plainStalled = data.results.greedy.total_cost_egp < data.results.greedy_upgrade.total_cost_egp * 0.9;

  /* `gap` is null when the heuristic it is measured against scored nothing, for
   * the same reason the column above can be. A number is not always available and
   * the sentence has to survive that. */
  const gapText = gap === null || gap === undefined
    ? 'by a margin with no percentage, because the strongest heuristic funded nothing'
    : `by <strong>${gap > 0 ? '+' : ''}${gap.toFixed(1)}%</strong>`;

  $('compare-body').innerHTML = `
    <table>
      <thead><tr><th>Method</th><th>Funded</th><th>Spent EGP</th>
        <th>Lifetime kgCO₂e</th><th>vs status quo</th><th>Time</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="caption">
      Exact optimization beats the strongest heuristic ${gapText} here.
      ${capped
        ? `That gap is large because a per-district cap is active: no greedy variant
           can plan around a constraint that couples buildings across the portfolio.`
        : `No cap is active, so this is a plain knapsack and a good heuristic gets
           close to it — whatever the figure above turns out to be, it is reported
           rather than framed. Set a per-district cap to see where exact optimization
           earns its solve time: a greedy pass spends its cap slots on the wrong
           buildings, and the gap opens.`}
      ${plainStalled
        ? `The plain greedy row is spending less than the others: it never revisits a
           funded building, so once each holds an option it stops, whatever budget is
           left. The comparison above is quoted against the upgrade pass instead.`
        : ''}
      ${data.equal_split_scored === false
        ? `Equal split funded nothing at all at this budget: a share of about
           ${compact(request_budget / 50)} EGP per building does not reach the price of
           the cheapest retrofit anywhere in the portfolio, so the entire budget goes
           unspent. That is why the column above has no percentage in it.`
        : `Equal split is the status quo rather than a contender: a share of about
           ${compact(data.results.equal_split.total_cost_egp / 50)} EGP per building buys
           nothing in most of them, so millions go unspent.`}
      All four ran on the same fifty buildings, the same budget and the same objective,
      so the columns are comparable &mdash; and the carbon figures are estimates, not
      measured savings.
    </p>`;
}

/* -------------------------------------------------------------- narrative */

/* The same words the controls above use, for the panel a viewer sees in their
 * place. A stored run that says `cpsat` and a control that says "Exact
 * optimization (CP-SAT)" are the same thing, and only one of them is English. */
const SOLVER_LABEL = {
  cpsat: 'exact optimization (CP-SAT)',
  greedy_upgrade: 'greedy plus an upgrade pass',
  greedy: 'plain greedy',
  equal_split: 'equal split — the status quo',
};

const OBJECTIVE_LABEL = {
  lca_carbon: 'life-cycle carbon',
  raw_kwh: 'first-year energy',
  egp_saved: 'first-year money',
};

const IV_NAMES = {
  led_lighting_v1: 'LED lighting',
  hvac_controls_v1: 'BMS controls',
  insulation_roof_v1: 'Roof insulation',
  glazing_upgrade_v1: 'Double glazing',
  hvac_replacement_v1: 'HVAC replacement',
  rooftop_solar_v1: 'Rooftop solar',
};
const ivName = (id) => IV_NAMES[id] || id;

/* `admin_24x7` is what the database calls it. Nobody reads a screen in snake
 * case, and a raw enum in a fact grid beside "2,361 m²" reads as a leak rather
 * than a value. */
const pretty = (value) => String(value || '')
  .replace(/_/g, ' ')
  .replace(/24x7/gi, '24×7')
  .replace(/^./, (c) => c.toUpperCase());

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
    const rows = b.options.slice(0, 3).map((o) => `
      <tr class="${o.intervention === b.best ? 'best' : ''}">
        <td>${o.intervention === b.best ? `${mark('check', { size: 12 })} ` : ''}${
          escapeHtml(ivName(o.intervention))}</td>
        <td>${compact(o.cost_egp)}</td>
        <td>${fmt(o.score_per_kegp, 0)}</td></tr>`).join('');
    return `
      <div class="card">
        <h3>${escapeHtml(b.code)}</h3>
        <p class="caption">
          ${escapeHtml(b.occupancy_pattern)} · ${escapeHtml(b.insulation_quality)} insulation ·
          HVAC ${b.hvac_age_yr} yr · ${compact(b.annual_kwh)} kWh/yr</p>
        <div class="card-table">
          <table><thead><tr><th>Measure</th><th>Cost EGP</th><th>Benefit /kEGP</th></tr></thead>
          <tbody>${rows}</tbody></table>
        </div>
        <p class="winner">Winner: <strong>${escapeHtml(ivName(b.best))}</strong>.</p>
      </div>`;
  }).join('');

  const dist = Object.entries(data.distribution)
    .map(([k, n]) => `${ivName(k)} on ${n}`).join(', ');

  /* Spelled out, and counted from what came back. The server returns one
   * building per distinct winning measure, so how many cards there are IS the
   * finding: four groups means four different right answers. Hard-coding "four"
   * would have the dialog claiming a contrast the data may not contain. */
  const COUNT = ['no', 'one', 'two', 'three', 'four'][data.buildings.length] || 'several';

  $('narrative-body').innerHTML = `
    <p class="lede">A portfolio-wide priority list would give every building the same
    retrofit. These ${COUNT} disagree about which retrofit is best, and the reason is in
    their facts &mdash; the HVAC age and the insulation, not the size of the building.
    Each one is the largest building in its group, and every group has a different
    winner.</p>
    <div class="card-row">${cards}</div>
    <p class="caption">
      Across the portfolio the best single measure is ${escapeHtml(dist)} — so no single
      priority list is right for every building, which is what per-building optimization
      is for. Note what does <em>not</em> happen: rooftop generation never has the best
      benefit density anywhere. Cheap controls and lighting dominate it, which is the
      ordinary efficiency-before-generation loading order, discovered from the catalog
      rather than assumed.
    </p>
    <p class="caption">This is also why the option list on the right of the map shows the
    options that were rejected. The argument only holds if you can see what lost.</p>`;
}

/* -------------------------------------------------------------- lifecycle */

/* What the solver is doing, under the controls that asked it. This used to
 * write to a status light in the rail, where it was the only thing on nine
 * screens reporting the health of one of them; the rail has no light now and
 * "is the platform healthy" is the overview's question. */
function setStatus(text, kind) {
  const slot = $('map-status');
  if (!slot) return;
  slot.textContent = text || '';
  slot.className = 'note' + (kind ? ` ${kind}` : '');
}

/* An ARIA radiogroup of buttons rather than a <select>.
 *
 * Four allocation methods, each of which needs a line saying what it does - a
 * <select> can hold the four labels and none of the four explanations, and the
 * explanations are the reason anybody would choose between them. The chosen one
 * is readable at rest, with no list to open. */
function wireRadiogroup(id, onChange) {
  const group = $(id);
  if (!group) return;
  const buttons = [...group.querySelectorAll('[role="radio"]')];

  const choose = (button) => {
    buttons.forEach((b) => b.setAttribute('aria-checked', String(b === button)));
    onChange(button.dataset.value);
  };

  group.addEventListener('click', (event) => {
    const button = event.target.closest('[role="radio"]');
    if (button && button.getAttribute('aria-checked') !== 'true') choose(button);
  });

  /* Arrow keys move between radios and select as they go, which is what the
   * radiogroup pattern promises and what a row of plain buttons does not. */
  group.addEventListener('keydown', (event) => {
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const here = buttons.indexOf(document.activeElement);
    const next = buttons[(here + step + buttons.length) % buttons.length];
    next.focus();
    choose(next);
  });
}

const groupValue = (id) =>
  $(id)?.querySelector('[role="radio"][aria-checked="true"]')?.dataset.value;

/* The building picker: a real combobox over all fifty, filtering across code,
 * name and district at once - because an analyst knows a building by whichever
 * of the three they happened to read last. */
function wirePicker(signal) {
  const input = $('pick');
  const list = $('picklist');
  if (!input || !list) return;

  const close = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
  };

  const paint = () => {
    const query = input.value.trim().toLowerCase();
    const all = state.projected.map((b) => b.props);
    const matches = (query
      ? all.filter((p) => `${p.code} ${p.name} ${p.district}`.toLowerCase().includes(query))
      : all).slice(0, 40);

    list.innerHTML = matches.length
      ? matches.map((p) => `
        <li role="option" aria-selected="${p.id === state.selected}">
          <button type="button" data-pick="${escapeHtml(p.id)}">
            <span>${escapeHtml(p.name)}</span>
            <span class="mono">${escapeHtml(p.code)}</span>
          </button>
        </li>`).join('')
      : `<li class="picker-empty">No building matches &ldquo;${escapeHtml(input.value)}&rdquo;.
         Clear the box to see all ${all.length}.</li>`;

    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  };

  input.addEventListener('focus', paint);
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
    input.value = '';
    close();
    selectBuilding(button.dataset.pick);
  });
  /* Clicking anywhere else closes it. Tied to the mount, like every other
   * listener this view puts on something it does not own. */
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.picker-wrap')) close();
  }, { signal });
}

/* Puts the selected building in the middle of the viewport at the current zoom.
 * Without it, finding a building through the filter left it selected, described
 * in the panel, and somewhere off the edge of the map. */
function centreOnSelection() {
  const svg = $('map');
  const b = state.projected.find((p) => p.id === state.selected);
  if (!b || !svg) return;
  const { k } = state.view;
  state.view.x = svg.clientWidth / 2 - b.cx * k;
  state.view.y = svg.clientHeight / 2 - b.cy * k;
  render();
}

function wire(signal) {
  /* The solve controls only exist when the caller may solve. Zoom, pan and the
   * building detail are wired either way - reading the allocation is the part a
   * viewer is here for, and it is fully interactive for them. */
  if (canSolve) {
    const budget = $('budget');
    const showBudget = () => {
      const value = fmt(Number(budget.value));
      $('budget-out').textContent = value;
      budget.setAttribute('aria-valuetext', `${value} Egyptian pounds`);
    };
    showBudget();

    // Debounced so dragging the slider does not queue a solve per pixel; 120 ms is
    // below the threshold where the map stops feeling attached to the control.
    const solveSoon = debounce(solve, 120);
    budget.addEventListener('input', () => { showBudget(); solveSoon(); });

    wireRadiogroup('objective', solve);
    wireRadiogroup('solver', solve);
    $('cap').addEventListener('change', solve);

    wireModal('compare-modal', 'compare-close', $('compare-btn'), compare);
    wireModal('narrative-modal', 'narrative-close', $('narrative-btn'), narrative);
  }

  $('zoom-in').addEventListener('click', () => zoomAbout(ZOOM_STEP));
  $('zoom-out').addEventListener('click', () => zoomAbout(1 / ZOOM_STEP));
  $('zoom-reset').addEventListener('click', resetView);
  $('zoom-centre').addEventListener('click', centreOnSelection);
  $('sel-clear').addEventListener('click', clearSelection);

  wirePicker(signal);

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
  await loadContext();      // before the first geometry pass, so it is there on paint 1
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

/* The stored run's own facts, and its input hash in full.
 *
 * The hash is the whole point of showing this to a viewer: it covers the fifty
 * building records, their consumption figures, the budget, the objective, the
 * district cap and the method, so the same hash means the same allocation and a
 * different one means an input moved and the two runs are not comparable. It is
 * shown at 64 characters, wrapped, rather than truncated - a truncated hash
 * cannot be checked against anything. */
function showRunFacts(run) {
  const facts = $('run-facts');
  if (!facts) return;
  const when = (run.created_at || '').replace('T', ' ').slice(0, 16);
  const cap = run.max_funded_per_district;
  facts.innerHTML = `
    <div><dt>Decided</dt><dd>${escapeHtml(when)}</dd></div>
    <div><dt>Budget</dt><dd>${compact(run.budget_egp)} EGP</dd></div>
    <div><dt>Ranked by</dt><dd>${escapeHtml(OBJECTIVE_LABEL[run.objective] || run.objective)}</dd></div>
    <div><dt>Method</dt><dd>${escapeHtml(SOLVER_LABEL[run.solver] || run.solver)}</dd></div>
    <div><dt>District cap</dt><dd>${cap ? `${cap} per district` : 'none'}</dd></div>
    ${run.inputs_hash ? `<div class="wide"><dt>Input hash</dt>
      <dd class="hash-block">${escapeHtml(run.inputs_hash)}</dd></div>` : ''}`;
}

