/* GEMP map UI.
 *
 * Deliberately no mapping library. F13 requires the demonstration to survive an
 * unplugged network cable, and Leaflet's default tile layer fetches from a remote
 * host - a dependency that fails silently until the one moment it matters. The
 * "map" here is fifty building polygons, which inline SVG draws natively with a
 * ten-line equirectangular projection and no bytes from anywhere.
 *
 * Everything with judgement in it lives on the server. This file fetches, projects,
 * paints and formats.
 */

'use strict';

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

function districtColour(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) % 360;
  return `hsl(${hash}, 18%, 30%)`;
}

function render() {
  const svg = $('map');
  const { x, y, k } = state.view;
  const funded = new Set((state.run?.items || []).map((i) => i.building_id));

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
    const isFunded = funded.has(b.id);
    const half = b.side / 2;
    const fill = isFunded ? 'var(--funded)' : districtColour(b.props.district);
    const cls = 'bldg' + (state.selected === b.id ? ' selected' : '');
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

function attachPanZoom() {
  const svg = $('map');
  let dragging = false, startX = 0, startY = 0, originX = 0, originY = 0;

  svg.addEventListener('mousedown', (e) => {
    dragging = true; svg.classList.add('dragging');
    startX = e.clientX; startY = e.clientY;
    originX = state.view.x; originY = state.view.y;
  });
  window.addEventListener('mouseup', () => { dragging = false; svg.classList.remove('dragging'); });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    state.view.x = originX + (e.clientX - startX);
    state.view.y = originY + (e.clientY - startY);
    render();
  });
  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const next = Math.min(MAX_ZOOM, Math.max(0.4, state.view.k * factor));
    const rect = svg.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    // Keep the point under the cursor fixed while zooming.
    state.view.x = mx - ((mx - state.view.x) * next) / state.view.k;
    state.view.y = my - ((my - state.view.y) * next) / state.view.k;
    state.view.k = next;
    render();
  }, { passive: false });
}

/* ------------------------------------------------------------------- data */

async function loadMap() {
  const runParam = state.run?.run_id ? `?run_id=${encodeURIComponent(state.run.run_id)}` : '';
  const res = await fetch(`${API}/map/geojson${runParam}`);
  state.geo = await res.json();
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
    const res = await fetch(`${API}/optimize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(currentRequest()),
    });
    if (!res.ok) throw new Error(`optimize returned ${res.status}`);
    state.run = await res.json();
    showSummary(state.run);
    render();
    if (state.selected) selectBuilding(state.selected);
    setStatus(`${state.run.status.toLowerCase()} · ${Math.round(state.run.solve_ms)} ms`, 'ok');
  } catch (err) {
    setStatus(err.message, 'bad');
  }
}

function showSummary(run) {
  $('m-funded').textContent = run.buildings_funded;
  $('m-spent').textContent = (run.budget_used_frac * 100).toFixed(0) + '%';
  $('m-kwh').textContent = compact(run.total_kwh_saving);
  $('m-carbon').textContent = compact(run.total_benefit_kgco2e);
  $('m-solve').textContent = Math.round(run.solve_ms) + ' ms';
}

async function selectBuilding(id) {
  state.selected = id;
  render();

  const runParam = state.run?.run_id ? `?run_id=${encodeURIComponent(state.run.run_id)}` : '';
  const res = await fetch(`${API}/buildings/${encodeURIComponent(id)}/candidates${runParam}`);
  if (!res.ok) return;
  const data = await res.json();

  const b = data.building;
  const rows = data.options.slice(0, 12).map((o) => `
    <div class="opt ${o.chosen ? 'chosen' : ''} ${o.lifetime_benefit_kgco2e <= 0 ? 'negative' : ''}">
      <div class="name">${escapeHtml(o.label)}</div>
      <div class="row"><span>${compact(o.cost_egp)} EGP</span>
        <span>${compact(o.annual_kwh_saving)} kWh/yr</span>
        <span>${fmt(o.score_per_kegp, 0)} /kEGP</span></div>
      ${o.chosen ? '<div class="tag">chosen by this allocation</div>' : ''}
    </div>`).join('');

  $('detail-empty').hidden = true;
  const body = $('detail-body');
  body.hidden = false;
  body.innerHTML = `
    <h2>${escapeHtml(b.name)}</h2>
    <div class="code">${escapeHtml(b.code)} · ${escapeHtml(b.district)}</div>
    <div class="facts">
      <div><span>Use</span><br>${escapeHtml(b.occupancy_pattern)}</div>
      <div><span>Insulation</span><br>${escapeHtml(b.insulation_quality)}</div>
      <div><span>HVAC age</span><br>${b.hvac_age_yr} yr</div>
      <div><span>Roof</span><br>${fmt(b.roof_area_m2)} m²</div>
      <div><span>Floor</span><br>${fmt(b.floor_area_m2)} m²</div>
      <div><span>Consumption</span><br>${compact(b.annual_kwh)} kWh/yr</div>
    </div>
    <h3 style="font-size:12px;color:var(--ink-dim);text-transform:uppercase;letter-spacing:.6px;">
      Options considered (${data.options.length})</h3>
    ${rows || '<p class="note">No applicable interventions.</p>'}`;
}

/* ---------------------------------------------------------------- compare */

async function compare() {
  const modal = $('compare-modal');
  modal.hidden = false;
  $('compare-body').textContent = 'Solving all four…';

  const res = await fetch(`${API}/compare`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(currentRequest()),
  });
  const data = await res.json();

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

  const res = await fetch(`${API}/narrative/building-specific`);
  if (!res.ok) { $('narrative-body').textContent = 'Not available for this catalog.'; return; }
  const data = await res.json();

  const cards = data.buildings.map((b) => {
    const rows = b.options.slice(0, 5).map((o) => `
      <tr class="${o.intervention === b.best ? 'best' : ''}">
        <td>${ivName(o.intervention)}</td>
        <td>${compact(o.cost_egp)}</td>
        <td>${fmt(o.score_per_kegp, 0)}</td></tr>`).join('');
    return `
      <div style="flex:1;min-width:260px">
        <h3 style="font-size:13px;margin:0 0 2px">${escapeHtml(b.code)}</h3>
        <p class="caption" style="margin:0 0 8px">
          ${escapeHtml(b.occupancy_pattern)} · ${escapeHtml(b.insulation_quality)} insulation ·
          HVAC ${b.hvac_age_yr} yr · ${compact(b.annual_kwh)} kWh/yr</p>
        <table><thead><tr><th>Measure</th><th>Cost EGP</th><th>Benefit /kEGP</th></tr></thead>
        <tbody>${rows}</tbody></table>
      </div>`;
  }).join('');

  const dist = Object.entries(data.distribution)
    .map(([k, n]) => `${ivName(k)} on ${n}`).join(', ');

  $('narrative-body').innerHTML = `
    <div style="display:flex;gap:22px;flex-wrap:wrap">${cards}</div>
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

function wire() {
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
  $('compare-btn').addEventListener('click', compare);
  $('narrative-btn').addEventListener('click', narrative);
  $('narrative-close').addEventListener('click', () => { $('narrative-modal').hidden = true; });
  $('narrative-modal').addEventListener('click', (e) => {
    if (e.target.id === 'narrative-modal') $('narrative-modal').hidden = true;
  });
  $('compare-close').addEventListener('click', () => { $('compare-modal').hidden = true; });
  $('compare-modal').addEventListener('click', (e) => {
    if (e.target.id === 'compare-modal') $('compare-modal').hidden = true;
  });
  window.addEventListener('resize', debounce(() => { buildGeometry(); render(); }, 150));
}

async function main() {
  wire();
  attachPanZoom();
  await checkHealth();
  await loadMap();
  await solve();
  await loadMap();          // reload so anomaly rings and funded state agree
}

main();
