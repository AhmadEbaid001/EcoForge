/* The views behind the navigation tabs.
 *
 * Each exports `render(root, ctx)` and owns nothing else. A view fetches what it
 * needs, writes HTML into its container, and wires its own handlers - so switching
 * tabs cannot leave a timer or a listener running against a screen nobody is looking
 * at, which is the usual way a single-page application starts leaking.
 *
 * `ctx.user` carries the role, and every control that a role cannot use is not
 * rendered at all. That is presentation only: the server refuses the call regardless,
 * and hiding a button has never stopped anyone who can open a terminal.
 *
 * Every view opens with the same three things, from ui.js: a title that says what
 * the screen is for, the data clock the numbers were read at, and a status slot
 * where the result of an action appears. Before that, a view was a stack of
 * panels and the only clue to where you were was which tab was highlighted.
 */

'use strict';

import { api, ApiError } from './api.js';
import { t } from './i18n.js';
import {
  compact, escapeHtml, icon, lineChart, mark, proportionBar, sevChip, stackedBars,
  statTile,
} from './charts.js';
import {
  clearStatus, confirmAction, dataTable, emptyState, markRead, onReread,
  openDialog, pageHead, picker, pickerValue, selectWrap, setStatus,
  skeletonChart, skeletonRows, skeletonTiles, wirePicker, wireSort,
} from './ui.js';

const can = (user, role) => {
  const ladder = ['viewer', 'analyst', 'admin'];
  return ladder.indexOf(user.role) >= ladder.indexOf(role);
};

const fmtDateTime = (iso) => (iso ? iso.replace('T', ' ').slice(0, 16) : '—');

function errorBox(error) {
  const detail = error instanceof ApiError ? error.detail : String(error);
  return `<div class="error-box" role="alert">${escapeHtml(detail)}</div>`;
}

async function guard(root, work) {
  try {
    await work();
  } catch (error) {
    /* An error inside an already-rendered view goes to the status slot, so the
     * screen the person was reading does not vanish underneath the message. */
    const detail = error instanceof ApiError ? error.detail : String(error);
    if (root.childElementCount) setStatus(root, { kind: 'error', message: detail });
    else root.innerHTML = errorBox(error);
  }
}

/* Re-read lives in the shell - in the header beside the clock, and again in the
 * stale strip when there is one - so a view says what re-reading MEANS for it
 * rather than binding a button it does not own. */
function wireRefresh(root, reload) {
  onReread(reload);
}

const buildingOptions = (buildings) =>
  buildings.map((b) => ({ value: b.id, label: `${b.code} — ${b.name}` }));

/* One filter row, above everything it scopes.
 *
 * Not a control per panel: two windows on one screen is how a demonstration ends
 * up comparing a fortnight of demand against a month of alerts and drawing a
 * conclusion from the mismatch. One window, stated once, and every number under it
 * moves together.
 *
 * Presets rather than a date picker, because the data clock runs at 720x and no
 * one in the room knows today's date in data time. "Last 30 days" is a question a
 * reader can ask; "2027-08-11 to 2027-09-10" is one they would have to work out.
 */
const RANGES = [
  { days: 7, label: '7 days' },
  { days: 14, label: '14 days' },
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
];

function rangeControl(selected) {
  const buttons = RANGES.map((r) =>
    `<button type="button" class="seg-btn" data-days="${r.days}"
       aria-pressed="${r.days === selected}">${escapeHtml(r.label)}</button>`).join('');
  /* No visible "Window" label. Four day ranges sitting beside the data clock in
   * the page header are read as a window without being told, and the word cost
   * 71px of a header that has to hold the clock, the language, the text size and
   * the appearance controls on one line down to 1280. The group keeps its
   * accessible name - dropping the caption must not drop the label. */
  return `<div class="chart-filters">
    <div class="segmented" role="group" aria-label="Window">${buttons}</div>
  </div>`;
}

/* Delegated, so the buttons can be replaced by a redraw without the listener
 * going with them. */
function wireRange(root, onChange) {
  /* The control renders into the shell's page header, which is a sibling of the
   * view root rather than a child of it, so this cannot be a root-scoped query.
   * One screen is mounted at a time and only this one uses the control. */
  const group = document.querySelector('.page-head .segmented');
  group?.addEventListener('click', (event) => {
    const button = event.target.closest('.seg-btn');
    if (!button || button.getAttribute('aria-pressed') === 'true') return;
    group.querySelectorAll('.seg-btn').forEach((b) =>
      b.setAttribute('aria-pressed', String(b === button)));
    onChange(Number(button.dataset.days));
  });
}

/* Movement over the window, as a percentage: the second half against the first.
 * Null rather than zero when there is nothing to compare against - a tile that
 * says "0%" is claiming it measured something. */
function halfOverHalf(values) {
  if (values.length < 4) return null;
  const half = Math.floor(values.length / 2);
  const before = values.slice(0, half).reduce((a, b) => a + b, 0);
  const after = values.slice(half).reduce((a, b) => a + b, 0);
  if (!before) return null;
  return ((after - before) / before) * 100;
}

/* What the database calls a solver and an objective, and what a person calls
 * them. `cpsat` and `lca_carbon` are correct values and are not English; they
 * were reaching the screen inside sentences, which reads as a leak rather than
 * as a fact. The map's controls already name all seven of these, and these are
 * the same names - one vocabulary, whichever screen you are on. */
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

const solverLabel = (id) => SOLVER_LABEL[id] || String(id || '—');
const objectiveLabel = (id) => OBJECTIVE_LABEL[id] || String(id || '—');

/* Worst first, everywhere. The API hands back a plain object whose key order is
 * whatever the GROUP BY produced, and an ordinal scale printed in arbitrary order
 * stops being a scale. */
const SEVERITY_ORDER = ['critical', 'high', 'medium'];

/* ------------------------------------------------------------------ overview */

/* No purpose sentence on this screen. The four figures, the two charts and the
 * stored-run panel each name themselves, and a paragraph telling the reader what
 * they are about to look at costs a band of header height on every visit. The
 * one thing the sentence carried that the layout did not - that the money is
 * allocated elsewhere - is said by the button at the foot of the stored run,
 * which is now the only route to the map on this screen. */

export const overview = {
  title: 'Overview',
  /* Kept on the view rather than inside render(), so leaving the screen and
   * coming back does not silently reset the window the reader chose. */
  state: { days: 30 },
  async render(root, ctx) {
    /* Hoisted above the first paint: the skeleton now renders the window control
     * too, so the binding has to exist before that template literal runs. */
    const state = overview.state;

    root.innerHTML = `
      ${pageHead({ root,
        title: t('overview.title'),
        actions: rangeControl(state.days),
      })}
      ${skeletonTiles(4)}${skeletonChart()}`;

    await guard(root, async () => {
      const [summary, load, daily] = await Promise.all([
        api.summary(), api.load(state.days), api.anomaliesDaily(state.days),
      ]);

      const measured = summary.annual_kwh_source || {};
      const fromForecast = measured.forecast || 0;
      const run = summary.latest_run;

      const dailyTotals = (days) => days.map((d) =>
        (d.critical || 0) + (d.high || 0) + (d.medium || 0));

      const panels = (loadData, dailyData) => `
        <div class="stat-row">
          ${statTile(t('overview.buildings'), compact(summary.buildings),
                     'Public buildings in New Cairo, all costed.')}
          ${statTile(t('overview.readings'), compact(summary.readings),
                     'Half-hourly, and every one signed on arrival.')}
          ${statTile(t('overview.openAlerts'), compact(summary.open_anomalies),
                     'Rounded — the count moves while you read it.',
                     { spark: dailyTotals(dailyData.days),
                       trend: halfOverHalf(dailyTotals(dailyData.days)),
                       chips: SEVERITY_ORDER
                         .filter((k) => (summary.open_by_severity || {})[k])
                         .map((k) => `<span class="stat-chip ${k}">${
                           sevChip(k)} ${compact(summary.open_by_severity[k])}</span>`)
                         .join('') })}
          ${statTile(t('overview.measured'), `${fromForecast} of ${summary.buildings}`,
                     'Costed on metered consumption, not a floor-area rule.')}
        </div>

        <div class="two-col">
          <section class="panel">
            <header>
              <h3>${t('overview.demandPanel')}</h3>
              <span class="scope">${state.days} days, hourly, kW summed across
                ${summary.buildings} buildings</span>
            </header>
            ${lineChart([{ label: 'Portfolio demand', points: loadData.points }], { unit: 'kW' })}
            <div class="panel-foot">
              <p class="caption">Point at the chart, or focus it and use the arrow keys.
              Every window here is measured in data time, which the simulator advances at
              720&times; wall clock.</p>
              <div class="panel-actions">
                <a class="btn secondary" href="#/forecasts">Open forecasting</a>
              </div>
            </div>
          </section>

          <section class="panel">
            <header>
              <h3>${t('overview.alertsPanel')}</h3>
              <span class="scope">${state.days} days, stacked by severity</span>
            </header>
            ${stackedBars(dailyData.days, ['critical', 'high', 'medium'], { height: 240 })}
            <div class="panel-foot">
              <p class="caption">Readings that deviated from what the forecaster expected.
              The inbox lists them worst first.</p>
              <div class="panel-actions">
                <a class="btn secondary" href="#/alerts">Open the alert inbox</a>
              </div>
            </div>
          </section>
        </div>`;

      root.innerHTML = `
        ${pageHead({ root,
          title: t('overview.title'),
          actions: rangeControl(state.days),
        })}

        <div id="ov-panels">${panels(load, daily)}</div>

        <section class="panel">
          <header>
            <h3>${t('overview.latestRun')}</h3>
            <!-- Said out loud, because the figures underneath look exactly like
                 live ones and are not: this is what was decided and stored, not
                 what the optimizer would answer now. -->
            <span class="scope">${t('overview.storedNotLive')}</span>
          </header>
          ${run ? `
            <div class="stat-row compact">
              ${statTile(t('overview.budget'), compact(run.budget_egp) + ' EGP')}
              ${statTile(t('overview.funded'), run.buildings_funded + ' buildings')}
              ${statTile(t('overview.spent'), compact(run.total_cost_egp) + ' EGP')}
              ${statTile(t('overview.shareSpent'), run.budget_egp
                  ? `${((run.total_cost_egp / run.budget_egp) * 100).toFixed(0)}%` : '—')}
              ${statTile(t('overview.lifetimeBenefit'), compact(run.total_benefit_kgco2e) + ' kgCO₂e')}
              ${statTile(t('overview.districtCap'), run.max_funded_per_district
                  ? `${run.max_funded_per_district} per district` : 'none')}
            </div>
            <div class="panel-foot">
              <p class="caption">Solved by <strong>${escapeHtml(solverLabel(run.solver))}</strong>,
              ranked by <strong>${escapeHtml(objectiveLabel(run.objective))}</strong>, at data time
              ${fmtDateTime(run.created_at)}. The lifetime carbon figure is an estimate,
              not a measurement.</p>
              <div class="panel-actions">
                <a class="btn primary" href="#/map">Open it on the map</a>
              </div>
            </div>`
            : emptyState({
                title: 'No allocation has been run yet',
                body: 'Open the allocation map, set a budget and a method, and the '
                    + 'optimizer will store its recommendation here.',
              })}
        </section>`;

      markRead(summary.data_clock);
      wireRefresh(root, () => overview.render(root, ctx));

      wireRange(root, async (days) => {
        state.days = days;
        const target = root.querySelector('#ov-panels');
        /* The frame is kept and dimmed rather than replaced with a skeleton: a
         * reader who has just changed the window is looking at the chart, and
         * swapping it for grey boxes makes the screen jump under them. */
        target.classList.add('is-loading');
        await guard(root, async () => {
          const [nextLoad, nextDaily] = await Promise.all([
            api.load(days), api.anomaliesDaily(days),
          ]);
          target.innerHTML = panels(nextLoad, nextDaily);
        });
        target.classList.remove('is-loading');
      });
    });
  },
};

/* ----------------------------------------------------------------- forecasts */

/* How far apart two data-time stamps are, in the largest unit that still reads as
 * a quantity. Forecast staleness is the number this screen exists to expose and
 * "13,058 hours" is not a number anyone converts in their head. */
function ageBetween(fromIso, toIso) {
  if (!fromIso || !toIso) return null;
  const hours = (Date.parse(toIso) - Date.parse(fromIso)) / 3.6e6;
  if (!Number.isFinite(hours)) return null;
  if (hours < 48) return `${Math.max(0, Math.round(hours))} hours`;
  const days = hours / 24;
  if (days < 60) return `${Math.round(days)} days`;
  return `${(days / 30.44).toFixed(1)} months`;
}

export const forecasts = {
  title: 'Forecasts',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({ root, title: 'Load forecasting' })}
      ${skeletonChart()}`;

    await guard(root, async () => {
      const [metrics, buildings] = await Promise.all([
        api.get('/metrics/forecast'), api.get('/buildings'),
      ]);
      /* A blank first entry, so the box starts empty rather than showing the
       * name of a building nobody picked and no chart is drawn for. */
      const options = [{ value: '', label: '' }, ...buildingOptions(buildings)];
      const byId = new Map(buildings.map((b) => [b.id, b]));
      /* Nothing is picked on arrival. Drawing an arbitrary building would put a
       * chart on screen that nobody asked about, and the empty state below says
       * what the panel will hold - which is the more useful first frame. */
      const state = { building: '' };

      /* No per-building error is shown, because /metrics/forecast does not carry
       * one: it aggregates by model_version. A MAPE chip here would be a number
       * this screen invented, and the purpose sentence says so out loud. */

      /* A bar divided into one segment is a bar that says nothing: it draws a
       * full-width block labelled 100% and asks the reader to work out that the
       * portfolio is unanimous. When one model has won everywhere, say so; keep
       * the bar for when there is a split to see. */
      const models = (metrics.by_model || []).slice()
        .sort((a, b) => b.buildings - a.buildings);
      const unanimous = models.length === 1 ? models[0] : null;
      const staleBy = ageBetween(metrics.newest_forecast_ts, metrics.newest_reading_ts);
      const covered = metrics.buildings_covered_recently;
      /* Coverage is what the chart below actually depends on, so it is reported
       * as a state rather than left for the reader to infer from an empty panel. */
      const coverState = covered === 0 ? 'none'
        : covered < buildings.length ? 'partial' : 'full';

      root.innerHTML = `
        ${pageHead({ root, title: 'Load forecasting' })}

        <section class="panel">
          <header>
            <h3>Forecast coverage</h3>
            <span class="scope">${buildings.length} buildings, in data time</span>
          </header>

          <div class="stat-row compact">
            ${statTile('Costed from', unanimous ? unanimous.model_version
                       : `${models.length} models`,
                       unanimous ? `All ${unanimous.buildings} buildings.`
                                 : 'Split across the portfolio.')}
            ${statTile('Reaching the last 14 days', `${covered} of ${buildings.length}`,
                       coverState === 'full' ? 'Every building has a comparable line.'
                       : coverState === 'none' ? 'No building has one.'
                       : 'The rest have nothing to compare against.')}
            ${statTile('Forecast age', staleBy || '—',
                       'Behind the newest meter reading.')}
          </div>

          ${coverState === 'full' ? '' : `
          <div class="note-panel ${coverState === 'none' ? 'bad' : 'warn'}" role="note">
            ${icon('warning')}
            <p>The stored forecasts end <strong>${escapeHtml(staleBy || 'some time')}</strong>
            before the newest reading, so ${coverState === 'none'
              ? 'no building has a forecast inside the two weeks the panel below draws'
              : `only ${covered} of ${buildings.length} buildings have one inside the two `
                + 'weeks the panel below draws'}. The chart is not broken &mdash; there is
            nothing recent to draw. Re-running the nightly refit regenerates them.</p>
          </div>`}

          ${unanimous ? '' : proportionBar(models.map((m) => ({
            label: m.model_version, value: m.buildings,
          })))}

          <p class="caption">${unanimous
            ? `Every building is costed from <strong>${escapeHtml(unanimous.model_version)}</strong>.
               A single winner is a finding, not a default: the selector keeps a
               seasonal-naive baseline and reports it whenever it wins, so a portfolio
               reading like this one is saying the learned model beat that baseline
               everywhere it was measured.`
            : `Where the seasonal-naive baseline wins, that is
               <strong>information, not a shortfall</strong>. Those buildings have a load
               shape stable enough that last week predicts this week, and a heavier model
               would only add variance to the figure the optimizer costs against. It is
               why the baseline is kept and reported rather than quietly replaced.`}</p>
        </section>

        <section class="panel">
          <header>
            <h3>Actual against forecast</h3>
            <span class="scope" id="fc-scope">no building selected</span>
            <div class="toolbar">
              ${picker({ id: 'fc-building', label: 'Building', options, value: state.building })}
            </div>
          </header>
          <div id="fc-chart">${emptyState({
            title: 'Pick a building',
            body: 'This panel then draws two weeks of hourly metered readings against '
                + 'what the model expected for the same hours — the metered line solid, '
                + 'the forecast dashed. Comparing them by eye is the point: a single '
                + 'error figure would say the model is good without letting anyone see '
                + 'where it is wrong.',
          })}</div>
          <div class="stroke-legend" id="fc-legend" hidden>
            <span class="key"><svg viewBox="0 0 34 8" width="34" height="8" aria-hidden="true"><path d="M0 4h34" stroke="var(--ink)" stroke-width="1.8" fill="none"/></svg>Metered actual</span>
            <span class="key" data-key="forecast"><svg viewBox="0 0 34 8" width="34" height="8" aria-hidden="true"><path d="M0 4h34" stroke="var(--accent)" stroke-width="1.8" stroke-dasharray="5 3" fill="none"/></svg>Forecast</span>
          </div>
        </section>`;

      const input = root.querySelector('#fc-building');
      wirePicker(input, options.filter((o) => o.label));
      const draw = async () => {
        const target = root.querySelector('#fc-chart');
        target.innerHTML = skeletonChart();
        const data = await api.forecast(state.building, 336);
        const b = byId.get(state.building);
        const hasForecast = (data.forecast || []).length > 0;

        target.innerHTML = lineChart([
          { label: 'Actual', points: data.actual },
          { label: 'Forecast', points: data.forecast },
        ], { unit: 'kW' }) + (hasForecast ? '' : `
          <p class="note warn">No forecast is stored for this building inside this
          window, so only the metered line is drawn. The panel above says how far
          behind the stored forecasts are.</p>`);

        /* The legend named a dashed forecast line whether or not one had been
         * drawn, which on a building with no recent forecast is the screen
         * describing something that is not there. */
        const legend = root.querySelector('#fc-legend');
        legend.hidden = false;
        legend.querySelector('[data-key="forecast"]').hidden = !hasForecast;
        /* Name, code, district, the model that won here, and the figure this
         * building is costed at - so the chart is anchored to a building rather
         * than floating as "a forecast". */
        root.querySelector('#fc-scope').textContent = b
          ? `${b.name} · ${b.code} · ${b.district} · ${
              data.model_version || 'no model recorded'} · costed at ${
              compact(b.annual_kwh)} kWh/yr`
          : '';
      };

      /* A datalist reports every keystroke as a change, so resolve the text and
       * only fetch when it actually names a different building. */
      input.addEventListener('change', () => {
        const value = pickerValue(input, options);
        if (!value || value === state.building) return;
        state.building = value;
        guard(root, draw);
      });
    });
  },
};

/* ------------------------------------------------------------------- alerts */

/* The server's hard ceiling per request. The count line reasons about it, so it
 * cannot be a literal typed into the query string alone. */
const ROW_CAP = 100;

export const alerts = {
  title: 'Alerts',
  async render(root, ctx) {
    const state = {
      severity: '', building: '', onlyOpen: true,
      rows: [], selected: new Set(), total: null, bySeverity: {},
      sortKey: 'robust_z', sortDir: 'desc',
    };
    const analyst = can(ctx.user, 'analyst');

    root.innerHTML = `
      ${pageHead({ root, title: 'Alert inbox' })}
      ${skeletonRows(8)}`;

    await guard(root, async () => {
      const [buildings, firstSummary] = await Promise.all([
        api.get('/buildings'), api.summary(),
      ]);
      const options = [{ value: '', label: 'All buildings' }, ...buildingOptions(buildings)];
      const summaryTotal = firstSummary.open_anomalies;

      /* One sentence, stated rather than implied. The list is a capped slice of
       * about thirty thousand rows; acknowledging one used to refill it from the
       * pool with nothing on screen saying where the others were. */
      const countLine = (shown) => {
        if (!state.onlyOpen) return `The ${shown} largest deviations, open and closed.`;
        const scope = `${state.severity ? `${state.severity} ` : ''}alert`;
        /* Fewer rows than the cap means the filter returned everything it had, so
         * these ARE all of them. Only a full page is a slice of something larger.
         * Without this the screen told a reader looking at the two alerts on one
         * building that they were "the 2 largest of 114.8k", and invited them to
         * narrow a filter that was already as narrow as it goes. */
        if (shown < ROW_CAP) {
          return state.building || state.severity
            ? `All ${shown} open ${scope}s matching this filter.`
            : `All ${shown} open ${scope}s.`;
        }
        const total = state.severity && !state.building
          ? state.bySeverity[state.severity]
          : (state.building ? null : state.total);
        if (total === null || total === undefined) {
          return `The first ${shown} — narrow the filter to reach the rest.`;
        }
        return `The ${shown} largest of ${compact(total)} open ${scope}s — narrow a filter `
          + 'to reach the rest.';
      };

      /* The reference's column set, with this system's fields in place of the
       * ones it invented. It had an "Anomaly Type" column (Spike, Phantom
       * Load) and a "Deviation %"; the detector produces neither. What it does
       * produce is a robust z against the forecaster's expectation, and
       * expected-versus-observed is the same comparison its column made. */
      const columns = [
        { key: 'building_code', label: 'Building', sortable: true,
          render: (r) => `<b>${escapeHtml(r.building_code)}</b>` },
        { key: 'ts', label: 'Detected (data time)', sortable: true,
          render: (r) => `<span class="tabular">${fmtDateTime(r.ts)}</span>` },
        /* Observed goes bold only past a 50% deviation. Bolding it on every row
         * makes the weight mean "this is the observed column" instead of "look
         * at this one". */
        { key: 'expected_kw', label: 'Expected vs observed (kW)', num: true, sortable: true,
          render: (r) => {
            const far = r.expected_kw
              ? Math.abs(r.observed_kw - r.expected_kw) / Math.abs(r.expected_kw) > 0.5
              : false;
            return `${compact(r.expected_kw)} / ${far
              ? `<b>${compact(r.observed_kw)}</b>` : compact(r.observed_kw)}`;
          } },
        { key: 'robust_z', label: 'Deviation z', num: true, sortable: true,
          value: (r) => (r.robust_z === null ? null : Math.abs(r.robust_z)),
          render: (r) => (r.robust_z === null ? '—'
            : `<span class="sev-figure ${escapeHtml(r.severity)}">${
                r.robust_z > 0 ? '+' : ''}${r.robust_z.toFixed(1)}</span>`) },
        { key: 'severity', label: 'Severity', sortable: true,
          render: (r) => sevChip(r.severity) },
        { key: 'acknowledged', label: 'Status', sortable: true,
          render: (r) => (r.acknowledged
            ? '<span class="chip closed">Closed</span>'
            : '<span class="chip open">Open</span>') },
      ];
      if (analyst) {
        columns.push({
          key: '_actions', label: '', cls: 'row-actions',
          render: (r) => (r.acknowledged ? '' : `<button class="ghost small" data-ack="${r.id}"
            aria-label="Acknowledge the ${escapeHtml(r.severity)} alert on
            ${escapeHtml(r.building_code)}">Acknowledge</button>`),
        });
      }

      const paint = () => {
        const body = root.querySelector('#alert-body');
        if (!state.rows.length) {
          body.innerHTML = emptyState({
            title: state.onlyOpen ? 'Nothing open here' : 'Nothing matches this filter',
            body: state.severity || state.building
              ? 'No alerts match the current filter. Widen it to see the rest of the inbox.'
              : 'The inbox is empty. Every deviation the detector raised has been acknowledged.',
          });
          root.querySelector('#alert-foot').innerHTML = '';
          return;
        }

        body.innerHTML = dataTable({
          columns, rows: state.rows.map((r) => ({ ...r, _id: r.id })),
          sortKey: state.sortKey, sortDir: state.sortDir, selectable: analyst,
          /* Every checkbox used to announce "Select this row" - a hundred times,
           * identically. What it selects is the alert, so it says which one. */
          rowLabel: (r) => `Select the ${r.severity} alert at ${r.building_code}, `
                         + `${fmtDateTime(r.ts)}`,
        });
        root.querySelector('#alert-foot').innerHTML =
          `<span>${escapeHtml(countLine(state.rows.length))}</span>
           <span class="muted">Sorting reorders these rows. Deviation z has no units.</span>`;

        wireSort(body, state, paint);

        body.querySelectorAll('[data-ack]').forEach((button) => {
          button.addEventListener('click', async () => {
            button.disabled = true;
            const id = Number(button.dataset.ack);
            await guard(root, async () => {
              await api.acknowledgeOne(id);
              await load();
              offerUndo([id], 'Closed 1 alert.');
            });
          });
        });

        /* Bulk select, because acknowledging thirty rows one at a time is thirty
         * round trips and thirty chances to lose your place. The API takes a list
         * of ids in one call. */
        if (analyst) {
          const boxes = [...body.querySelectorAll('[data-select]')];
          const all = body.querySelector('[data-select-all]');
          const sync = () => {
            state.selected = new Set(boxes.filter((b) => b.checked).map((b) => Number(b.dataset.select)));
            all.checked = boxes.length > 0 && state.selected.size === boxes.length;
            all.indeterminate = state.selected.size > 0 && state.selected.size < boxes.length;
            paintActionBar();
          };
          boxes.forEach((box) => {
            box.checked = state.selected.has(Number(box.dataset.select));
            box.addEventListener('change', sync);
          });
          all.addEventListener('change', () => {
            boxes.forEach((box) => { box.checked = all.checked; });
            sync();
          });
          sync();
        }
      };

      /* What the destructive button will actually do, counted, in its label. It
       * used to read "Close every alert matching this filter", which named a
       * filter rather than a consequence and left the reader to work out whether
       * that meant the hundred rows or the thirty thousand behind them. */
      const bulkLabel = () => {
        const filtered = state.severity || state.building;
        const total = state.severity && !state.building
          ? state.bySeverity[state.severity] : (filtered ? null : state.total);
        if (!filtered) {
          return total ? `Close all ${compact(total)} open alerts…` : 'Close all open alerts…';
        }
        return total ? `Close all ${compact(total)} matching alerts…` : 'Close all matching alerts…';
      };

      /* The bar stays. A control that appears only once a condition is met
       * teaches nobody that the condition exists - the disabled button says what
       * is missing instead, which is the whole point of a disabled state. */
      const paintActionBar = () => {
        const bar = root.querySelector('#alert-actions');
        const n = state.selected.size;
        bar.hidden = false;
        bar.innerHTML = `
          <span>${n ? `<strong>${n}</strong> selected` : 'Nothing selected'}</span>
          <span class="bulk-actions">
            <button type="button" class="primary" data-ack-selected${n ? '' : ' disabled'}>${
              n ? `Acknowledge ${n} selected` : 'Tick a row to acknowledge it'}</button>
            ${n ? '<button type="button" class="secondary" data-clear-selection>Clear selection</button>' : ''}
            <button type="button" class="destructive" id="alert-ack-bulk">${
              escapeHtml(bulkLabel())}</button>
          </span>`;

        bar.querySelector('[data-clear-selection]')?.addEventListener('click', () => {
          state.selected = new Set();
          paint();
        });
        wireBulkClose(bar.querySelector('#alert-ack-bulk'));
        bar.querySelector('[data-ack-selected]').addEventListener('click', async () => {
          const ids = [...state.selected];
          if (!ids.length) return;
          await guard(root, async () => {
            await api.acknowledge({ ids });
            state.selected = new Set();
            await load();
            offerUndo(ids, `Closed ${ids.length} alert${ids.length === 1 ? '' : 's'}.`);
          });
        });
      };

      /* Acknowledging takes an `acknowledged: false`, so this is a real undo
       * rather than a message claiming one exists. */
      const offerUndo = (ids, message) => {
        setStatus(root, {
          kind: 'ok',
          message: `${message} by ${ctx.user.username} at ${new Date().toLocaleTimeString('en-GB')}.`,
          actionLabel: `Undo — put ${ids.length} back to open`,
          actionAttr: 'data-undo',
        });
        root.querySelector('[data-undo]')?.addEventListener('click', async () => {
          await guard(root, async () => {
            await api.acknowledge({ ids, acknowledged: false });
            await load();
            setStatus(root, { kind: 'hint', message: `Reopened ${ids.length} alert${ids.length === 1 ? '' : 's'}.` });
          });
        });
      };

      const load = async () => {
        const body = root.querySelector('#alert-body');
        body.innerHTML = skeletonRows(6);
        const params = `?limit=${ROW_CAP}&only_open=${state.onlyOpen}` +
          (state.severity ? `&severity=${encodeURIComponent(state.severity)}` : '') +
          (state.building ? `&building_id=${encodeURIComponent(state.building)}` : '');
        const [rows, summary] = await Promise.all([api.anomalyFeed(params), api.summary()]);
        state.rows = rows;
        state.total = summary.open_anomalies;
        state.bySeverity = summary.open_by_severity || {};
        markRead(summary.data_clock);
        wireRefresh(root, load);
        paint();
      };

      /* A filter strip of its own above the table, then the table. The cap is
       * stated permanently rather than discovered: a hundred rows out of thirty
       * thousand looks like the whole inbox unless the screen says otherwise. */
      root.innerHTML = `
        ${pageHead({ root, title: 'Alert inbox' })}

        <div class="panel toolbar-panel">
          <div class="toolbar">
            <div class="group">
              <span class="inline-label" id="sev-label">Severity</span>
              <!-- Three, and there is no "low". The detector does not produce
                   one, so offering the filter would be offering a filter that
                   always returns nothing. -->
              <div class="sev-toggles" role="group" aria-labelledby="sev-label">
                ${['critical', 'high', 'medium'].map((s) => `
                  <button type="button" class="sev-toggle ${s}" data-sev="${s}"
                          aria-pressed="false">${sevChip(s)}</button>`).join('')}
              </div>
            </div>
            <span class="rule"></span>
            <div class="group">
              ${picker({ id: 'alert-building', label: 'Building', options, value: '' })}
            </div>
            <span class="rule"></span>
            <label class="check"><input type="checkbox" id="alert-open" checked> Open only</label>
            <button type="button" class="secondary" id="alert-clear">Clear filters</button>
          </div>
        </div>

        ${analyst ? '<div id="alert-actions" class="action-bar"></div>' : `
        <div class="note-panel" role="note">
          ${icon('lock')}
          <p>Alerts are read-only for your role. Acknowledging one is an analyst action
          and is recorded against the username that did it, so it cannot be done on
          someone else's behalf. Ask an analyst, or an administrator to change your
          role — either change is written to the audit log.</p>
        </div>`}

        <section class="panel flex">
          <div id="alert-body" class="table-host"></div>
          <div id="alert-foot" class="table-foot"></div>
        </section>`;

      /* One severity at a time: the API takes a single `severity`, and a
       * multi-select that silently ORs client-side would be filtering the
       * hundred rows it was given rather than the thirty thousand it was not. */
      root.querySelectorAll('[data-sev]').forEach((button) => {
        button.addEventListener('click', () => {
          const wanted = button.dataset.sev === state.severity ? '' : button.dataset.sev;
          state.severity = wanted;
          root.querySelectorAll('[data-sev]').forEach((b) =>
            b.setAttribute('aria-pressed', String(b.dataset.sev === wanted)));
          state.selected = new Set();
          clearStatus(root);
          guard(root, load);
        });
      });

      root.querySelector('#alert-clear').addEventListener('click', () => {
        state.severity = '';
        state.building = '';
        state.onlyOpen = true;
        state.selected = new Set();
        root.querySelectorAll('[data-sev]').forEach((b) => b.setAttribute('aria-pressed', 'false'));
        root.querySelector('#alert-building').value = 'All buildings';
        root.querySelector('#alert-open').checked = true;
        clearStatus(root);
        guard(root, load);
      });

      const buildingInput = root.querySelector('#alert-building');
      wirePicker(buildingInput, options);
      buildingInput.addEventListener('change', () => {
        const value = pickerValue(buildingInput, options);
        if (value === null || value === state.building) return;
        state.building = value;
        state.selected = new Set();
        clearStatus(root);
        guard(root, load);
      });

      root.querySelector('#alert-open').addEventListener('change', (e) => {
        state.onlyOpen = e.target.checked;
        state.selected = new Set();
        clearStatus(root);
        guard(root, load);
      });

      function wireBulkClose(bulk) {
        if (!bulk) return;
        bulk.addEventListener('click', async () => {
          /* This closes every alert matching the filter, not the hundred on
           * screen - which is what the old "Acknowledge all shown" claimed. It
           * says the real scope and the real count, and it cannot be undone by
           * ids because the ids of thirty thousand rows were never loaded. */
          /* Closing the whole open inbox used to be refused outright, on the
           * grounds that it should not be one click. It still is not one click -
           * the count below has to be typed - but refusing it entirely meant the
           * only way to empty the inbox was to work through it a filter at a
           * time, which is not safer, only longer. The protection that matters
           * is the typed number, and that is unchanged. */
          const filtered = state.severity || state.building;
          const scope = filtered
            ? [
                state.severity ? `severity ${state.severity}` : null,
                state.building ? `building ${buildingInput.value}` : null,
              ].filter(Boolean).join(' and ')
            : 'the whole open inbox';
          const count = !filtered ? state.total
            : (state.severity && !state.building ? state.bySeverity[state.severity] : null);

          /* The count has to be TYPED. A second click is protection against a
           * slip of the hand and nothing else; typing the number is the one
           * thing that cannot be done without having read it. */
          const ok = await confirmAction({
            title: filtered ? 'Close every alert matching this filter'
                            : 'Close every open alert',
            description: (filtered
                ? `This closes every open alert matching ${scope}`
                : 'This closes every open alert in the portfolio')
              + (count ? ` — ${count.toLocaleString('en-US')} of them` : '')
              + ', not only the rows on screen. It will be recorded as '
              + `${ctx.user.username}. It cannot be undone from this screen, because the `
              + 'ids of the rows off screen were never loaded. Acknowledging instead is '
              + 'reversible, and reaches the rows you can see.',
            confirmLabel: count ? `Close ${count.toLocaleString('en-US')} alerts` : 'Close them all',
            confirmText: count ? String(count) : '',
            confirmHint: count ? 'The exact number, digits only.' : '',
          });
          if (!ok) return;

          await guard(root, async () => {
            const selectors = { acknowledged: true };
            if (state.severity) selectors.severity = state.severity;
            if (state.building) selectors.building_id = state.building;
            /* No selector means the whole inbox, which the API refuses unless the
             * caller says so in a field it could not have set by accident. */
            if (!filtered) selectors.all_open = true;
            /* `changed` is the row count; `acknowledged` in the response is the
             * boolean that was asked for, not a total. */
            const result = await api.acknowledge(selectors);
            await load();
            setStatus(root, {
              kind: 'ok',
              message: `Closed ${compact(result?.changed ?? 0)} alerts `
                     + `${filtered ? `matching ${scope}` : 'across the portfolio'}, `
                     + `by ${ctx.user.username}. This one cannot be undone here.`,
            });
          });
        });
      }

      await load();
    });
  },
};

/* --------------------------------------------------------------------- runs */

/* This screen is provenance, not history-as-a-feature. It exists so a run can be
 * re-found and reproduced from its input hash, which is also why the hash gets a
 * control of its own rather than being quietly truncated. */
const RUNS_PURPOSE = 'Every allocation the optimizer has stored, and the 64-character '
  + 'hash of the inputs that produced it. Same hash, same allocation.';

export const runs = {
  title: 'Allocations',
  async render(root, ctx) {
    /* Truncated by default: at 64 characters the hash is wider than every other
     * column put together and pushes the figures off a 1024px window. The
     * control says which you are looking at, and the footer says what each
     * choice costs you. */
    const state = { rows: [], sortKey: 'created_at', sortDir: 'desc', hashFull: false };

    root.innerHTML = `
      ${pageHead({ root,
        title: 'Stored allocations',
        description: RUNS_PURPOSE,
      })}
      ${skeletonRows(6)}`;

    await guard(root, async () => {
      state.rows = await api.runs();

      const columns = [
        { key: 'created_at', label: 'When', sortable: true,
          render: (r) => `<span class="mono">${fmtDateTime(r.created_at)}</span>` },
        { key: 'budget_egp', label: 'Budget EGP', num: true, sortable: true,
          render: (r) => compact(r.budget_egp) },
        { key: 'objective', label: 'Objective', sortable: true,
          render: (r) => escapeHtml(objectiveLabel(r.objective)) },
        /* Exact optimization is check-marked and bold wherever it appears: it is
         * the method that is provably right, and a reader scanning twenty-five
         * rows should not have to read the word to find it. */
        { key: 'solver', label: 'Method', sortable: true,
          render: (r) => (r.solver === 'cpsat'
            ? `<span class="method-exact">${mark('check', { size: 11 })}exact optimization</span>`
            : escapeHtml(solverLabel(r.solver))) },
        { key: 'buildings_funded', label: 'Funded', num: true, sortable: true,
          render: (r) => r.buildings_funded },
        { key: 'total_cost_egp', label: 'Spent EGP', num: true, sortable: true,
          render: (r) => compact(r.total_cost_egp) },
        { key: 'total_benefit_kgco2e', label: 'Lifetime kgCO₂e', num: true, sortable: true,
          render: (r) => compact(r.total_benefit_kgco2e) },
        { key: 'inputs_hash', label: 'Input hash', cls: 'mono',
          render: (r) => (state.hashFull
            ? `<span class="hash-all">${escapeHtml(r.inputs_hash)}</span>`
            : `<span class="hash" title="${escapeHtml(r.inputs_hash)}">${
                escapeHtml(String(r.inputs_hash).slice(0, 10))}&hellip;</span>`) },
        { key: '_provenance', label: '', cls: 'row-actions',
          render: (r) => `<button type="button" class="secondary small"
            data-prov="${escapeHtml(r.run_id)}">Provenance</button>
            <button type="button" class="secondary small"
            data-boq="${escapeHtml(r.run_id)}">BOQ</button>` },
      ];

      const paint = () => {
        const body = root.querySelector('#runs-body');
        body.innerHTML = state.rows.length
          ? dataTable({ columns, rows: state.rows, sortKey: state.sortKey, sortDir: state.sortDir })
          : emptyState({
              title: 'No allocations stored yet',
              body: 'Run the optimizer from the Map tab. Every solve is stored here with the '
                  + 'hash of the inputs that produced it.',
            });
        wireSort(body, state, paint);
        body.querySelectorAll('[data-prov]').forEach((button) => {
          button.addEventListener('click', () =>
            showProvenance(state.rows.find((r) => r.run_id === button.dataset.prov)));
        });
        body.querySelectorAll('[data-boq]').forEach((button) => {
          button.addEventListener('click', () => {
            guard(body, () => showBoq(button.dataset.boq));
          });
        });
      };

      root.innerHTML = `
        ${pageHead({ root,
          title: 'Stored allocations',
          description: RUNS_PURPOSE,
        })}
        <section class="panel">
          <header>
            <h3>Runs</h3>
            <span class="scope">${state.rows.length} stored, newest first</span>
            <div class="segmented" role="group" aria-label="Input hash length" id="hash-mode">
              <button type="button" class="seg-btn" data-hash="short" aria-pressed="true">First 10</button>
              <button type="button" class="seg-btn" data-hash="full" aria-pressed="false">All 64</button>
            </div>
          </header>
          <div id="runs-body"></div>
          <p class="caption" id="hash-note"></p>
        </section>`;

      const hashNote = () => {
        root.querySelector('#hash-note').innerHTML = state.hashFull
          ? 'Showing all 64 characters, which is the only form you can actually check '
            + 'one run against another with — at the cost of a table that scrolls '
            + 'sideways. Lifetime kgCO&#8322;e is an estimate, not a measurement.'
          : 'Showing the first 10 characters, which fits and is enough to tell two runs '
            + 'apart by eye — but not enough to <em>prove</em> two runs are the same. '
            + 'Switch to all 64, or open Provenance, for that. Lifetime kgCO&#8322;e is '
            + 'an estimate, not a measurement.';
      };

      root.querySelectorAll('[data-hash]').forEach((button) => {
        button.addEventListener('click', () => {
          state.hashFull = button.dataset.hash === 'full';
          root.querySelectorAll('[data-hash]').forEach((b) =>
            b.setAttribute('aria-pressed', String(b === button)));
          hashNote();
          paint();
        });
      });

      hashNote();
      paint();
      wireRefresh(root, () => runs.render(root, ctx));
    });
  },
};

/* One run, in full, with the hash it can be re-found by.
 *
 * The explanation matters as much as the value: a 64-character string with no
 * statement of what it covers is a decoration. It covers the fifty building
 * records, their consumption figures, the budget, the objective, the district
 * cap and the method — so the same hash means the same allocation, and a
 * different one means an input moved and the two runs are not comparable. */
function showProvenance(run) {
  if (!run) return;
  const host = document.createElement('div');
  host.className = 'modal';
  const opener = document.activeElement;

  const close = () => {
    host.remove();
    if (opener && opener.isConnected) opener.focus();
  };

  host.innerHTML = `
    <div class="modal-inner narrow" role="dialog" aria-modal="true" tabindex="-1"
         aria-label="Provenance of this allocation">
      <button class="close" type="button" data-close aria-label="Close">&times;</button>
      <h2>Provenance</h2>
      <div class="prov-body">
        <dl class="facts">
          <div><dt>Stored at (data time)</dt><dd>${escapeHtml(fmtDateTime(run.created_at))}</dd></div>
          <div><dt>Budget</dt><dd>${compact(run.budget_egp)} EGP</dd></div>
          <div><dt>Objective</dt><dd>${escapeHtml(objectiveLabel(run.objective))}</dd></div>
          <div><dt>Method</dt><dd>${escapeHtml(solverLabel(run.solver))}</dd></div>
          <div><dt>District cap</dt><dd>${run.max_funded_per_district
            ? `${run.max_funded_per_district} per district` : 'none'}</dd></div>
          <div><dt>Buildings funded</dt><dd>${run.buildings_funded}</dd></div>
          <div><dt>Spent</dt><dd>${compact(run.total_cost_egp)} EGP</dd></div>
          <div><dt>Lifetime benefit</dt><dd>${compact(run.total_benefit_kgco2e)} kgCO&#8322;e</dd></div>
          <div><dt>Solve time</dt><dd>${Math.round(run.solve_ms)} ms</dd></div>
          <div><dt>Status</dt><dd>${escapeHtml(run.status)}</dd></div>
        </dl>

        <h3 class="section-label">Input hash</h3>
        <p class="hash-block" id="prov-hash">${escapeHtml(run.inputs_hash)}</p>
        <div class="form-actions">
          <button type="button" class="secondary" data-copy>Copy the hash</button>
          <span class="hint" id="prov-copied" role="status"></span>
        </div>

        <p class="caption">This hash covers the fifty building records, their
        consumption figures, the budget, the objective, the district cap and the
        method. The same hash means the same allocation. A different hash means an
        input moved, and the two runs are not comparable &mdash; whatever their
        totals happen to look like.</p>
      </div>
    </div>`;

  host.querySelector('[data-close]').addEventListener('click', close);
  host.addEventListener('click', (event) => { if (event.target === host) close(); });
  host.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.stopPropagation(); close(); }
  });

  host.querySelector('[data-copy]').addEventListener('click', async () => {
    const said = host.querySelector('#prov-copied');
    try {
      await navigator.clipboard.writeText(run.inputs_hash);
      said.textContent = 'Copied all 64 characters.';
    } catch {
      /* Clipboard access can be refused, and a button that silently does
       * nothing is worse than one that says so. The hash is selectable above. */
      said.textContent = 'The browser refused clipboard access — select the hash above.';
    }
  });

  document.body.appendChild(host);
  host.querySelector('.modal-inner').focus();
}

/* ---------------------------------------------------------------- evidence */

/* The claims harness, on screen.
 *
 * `python -m gemp.evaluate` re-measures every sentence the paper makes against
 * the running system and exits non-zero when one stops holding - the output used
 * to live in a terminal, which is where differentiators go to be missed. This
 * view reads the same CSV the harness writes and adds nothing to it: no
 * summarising, no hiding of FAIL rows. A harness that only reports what already
 * works is a marketing document; this screen shows whatever it said, including
 * the known-open row that is supposed to be red.
 */
export const evidence = {
  title: 'Evidence',
  async render(root, ctx) {
    const EVIDENCE_PURPOSE = 'Every claim in the paper, re-measured by the claims '
        + 'harness against this deployment. A FAIL here means a sentence stopped '
        + 'being true and has not been corrected yet — which is exactly what the '
        + 'harness exists to catch.';
    const state = { rows: [], sortKey: 'id', sortDir: 'asc' };

    root.innerHTML = `
      ${pageHead({ root, title: 'Evidence', description: EVIDENCE_PURPOSE })}
      ${skeletonRows(8)}`;

    await guard(root, async () => {
      const body0 = await api.evidence();
      state.rows = body0.claims || [];

      const verdictChip = (r) => {
        if (r.verdict === 'PASS') return `<span class="sev pass">${mark('check', { size: 11 })}Pass</span>`;
        if (r.verdict === 'SKIP') return '<span class="muted">Skipped</span>';
        return r.known_open
          ? `<span class="sev high">${mark('dash', { size: 11 })}Known-open</span>`
          : `<span class="sev critical">${mark('cross', { size: 11 })}Fail</span>`;
      };

      const columns = [
        { key: 'id', label: 'Claim', sortable: true, cls: 'mono',
          render: (r) => `<strong>${escapeHtml(r.id)}</strong>` },
        { key: 'statement', label: 'What the paper says',
          render: (r) => escapeHtml(r.statement) },
        { key: '_verdict', label: 'Verdict', sortable: true,
          render: verdictChip },
        { key: 'measured', label: 'Measured now',
          render: (r) => escapeHtml(r.measured) },
        { key: 'detail', label: '', cls: 'mono muted',
          render: (r) => (r.detail ? escapeHtml(r.detail) : '') },
      ];

      root.innerHTML = `
        ${pageHead({ root, title: 'Evidence', description: EVIDENCE_PURPOSE })}
        <section class="panel">
          <header>
            <h3>Claims</h3>
            <span class="scope">${state.rows.length} measured claims</span>
          </header>
          <div id="evidence-body"></div>
          <p class="caption" id="evidence-note">${body0.note
            ? escapeHtml(body0.note) : ''}</p>
        </section>`;

      const paint = () => {
        const host = root.querySelector('#evidence-body');
        host.innerHTML = state.rows.length
          ? dataTable({ columns, rows: state.rows,
              sortKey: state.sortKey, sortDir: state.sortDir })
          : emptyState({
              title: 'No measurements yet',
              body: 'The claims harness has not been run on this deployment.',
              hint: 'Run: python -m gemp.evaluate',
            });
        wireSort(host, state, paint);
      };
      paint();
      wireRefresh(root, () => evidence.render(root, ctx));
    });
  },
};

/* ------------------------------------------------------------------- BOQ */

/* A stored allocation as a bill of quantities, with a CSV beside it.
 *
 * The CSP allows creating a Blob URL for a download but not displaying one, so
 * the table renders in-page and the download is a plain object-URL navigation -
 * which is all a procurement office needs: numbers on paper they can price
 * against, plus the file their spreadsheet expects. */
function downloadCsv(filename, rows) {
  const quote = (value) => `"${String(value ?? '').replaceAll('"', '""')}"`;
  const header = Object.keys(rows[0] || {});
  const csv = [header, ...rows.map((r) => header.map((k) => quote(r[k])))]
    .map((line) => line.join(','))
    .join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

async function showBoq(runId) {
  const opener = document.activeElement;
  const boq = await api.boq(runId);
  if (!boq.lines.length) return;

  const host = document.createElement('div');
  host.className = 'modal';
  const close = () => {
    host.remove();
    if (opener && opener.isConnected) opener.focus();
  };

  const columns = [
    { key: 'building_code', label: 'Building', sortable: true,
      render: (r) => `${escapeHtml(r.building_code)} <span class="muted">${
        escapeHtml(r.building_name)}</span>` },
    { key: 'measure', label: 'Measure', sortable: true, render: (r) => escapeHtml(r.measure) },
    { key: 'quantity', label: 'Quantity', num: true, sortable: true,
      render: (r) => `${compact(r.quantity)} ${escapeHtml(r.basis)}` },
    { key: 'unit_rate', label: 'Unit rate', render: (r) => escapeHtml(r.unit_rate) },
    { key: 'line_total_egp', label: 'Line total EGP', num: true, sortable: true,
      render: (r) => compact(r.line_total_egp) },
    { key: '_cite', label: '',
      render: (r) => (r.citation
        ? `<span class="sev high" title="${escapeHtml(r.citation)}">rate uncited</span>`
        : '') },
  ];
  const state = { rows: boq.lines, sortKey: 'building_code', sortDir: 'asc' };

  host.innerHTML = `
    <div class="modal-inner wide" role="dialog" aria-modal="true" tabindex="-1"
         aria-label="Bill of quantities">
      <button class="close" type="button" data-close aria-label="Close">&times;</button>
      <h2>Bill of quantities</h2>
      <p class="caption">Run <span class="mono">${escapeHtml(String(runId).slice(0, 10))}
        &hellip;</span> &middot; ${boq.lines.length} lines &middot; total
        ${compact(boq.total_egp)} EGP &middot; input hash
        <span class="mono">${escapeHtml(String(boq.inputs_hash).slice(0, 10))}&hellip;</span></p>
      <div id="boq-body"></div>
      <div class="form-actions">
        <button type="button" class="primary" data-download>Download CSV</button>
        <button type="button" class="secondary" data-print>Print brief</button>
        <span class="hint">Rates marked "uncited" still carry a TODO in the catalog
          and must be sourced before tender.</span>
      </div>
    </div>`;

  const paint = () => {
    const body = host.querySelector('#boq-body');
    body.innerHTML = dataTable({ columns, rows: state.rows,
      sortKey: state.sortKey, sortDir: state.sortDir });
    wireSort(body, state, paint);
  };
  paint();

  host.querySelector('[data-close]').addEventListener('click', close);
  host.addEventListener('click', (event) => { if (event.target === host) close(); });
  host.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.stopPropagation(); close(); }
  });
  host.querySelector('[data-download]').addEventListener('click', () =>
    downloadCsv(`gemp-boq-${String(runId).slice(0, 8)}.csv`, boq.lines));
  host.querySelector('[data-print]').addEventListener('click', () => window.print());

  document.body.appendChild(host);
  host.querySelector('.modal-inner').focus();
}

/* ---------------------------------------------------------------- integrity */

/* Two columns: the thing you ask, and the answer.
 *
 * The BROKEN state is designed as carefully as the intact one, deliberately. An
 * intact verdict is a green tick nobody reads twice; a break is the moment this
 * whole subsystem exists for, and it has to say what broke, where, what is still
 * true, and why there is no button here that fixes it.
 */
const INTEGRITY_PURPOSE = 'Whether the stored readings are the ones that were signed '
  + 'on arrival. The walk reads and never writes.';

export const integrity = {
  title: 'Integrity',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({ root,
        title: 'Reading integrity',
        description: INTEGRITY_PURPOSE,
      })}
      ${skeletonRows(4)}`;

    await guard(root, async () => {
      const buildings = await api.get('/buildings');
      /* Nothing selected on arrival: a chain walk is ~51,000 rows of work, and
       * starting one for a building nobody asked about is both a lie about what
       * the screen is for and a second of the server's time. */
      const options = [{ value: '', label: '' }, ...buildingOptions(buildings)];
      const byId = new Map(buildings.map((b) => [b.id, b]));
      const state = { building: '', phase: 'idle' };

      root.innerHTML = `
        ${pageHead({ root,
          title: 'Reading integrity',
          description: INTEGRITY_PURPOSE,
        })}

        <div class="two-col aside-first">
          <section class="panel">
            <header><h3>Verify a building</h3></header>
            <div class="verify-panel">
              ${picker({ id: 'int-building', label: 'Building', options, value: '' })}
              <button type="button" class="primary" data-verify disabled>
                Pick a building first</button>
              <p class="note">The walk reads and never writes. Nothing on this screen can
              alter a reading, a signature or the anchor file.</p>

              <!-- Three words this screen cannot avoid using, defined before it
                   uses them. A verdict in vocabulary the reader does not share
                   is not a verdict. -->
              <dl class="glossary">
                <div><dt>Chain walk</dt><dd>Re-reading every stored reading for the
                  building in order and checking that each one still hashes to the value
                  the next one recorded for it.</dd></div>
                <div><dt>Anchor file</dt><dd>A copy of the chain head written outside the
                  database volume, so deleting rows from the database cannot quietly
                  delete the evidence that they existed.</dd></div>
                <div><dt>Anchor agrees with database</dt><dd>The head in that file and the
                  head the database currently reports are the same value &mdash; nobody
                  rebuilt the chain and updated only one of the two.</dd></div>
              </dl>
            </div>
          </section>

          <section class="panel">
            <header>
              <h3>Result</h3>
              <span class="scope" id="int-scope">nothing verified yet</span>
            </header>
            <div id="int-body" class="result-panel"></div>
          </section>
        </div>`;

      const input = root.querySelector('#int-building');
      const button = root.querySelector('[data-verify]');
      wirePicker(input, options.filter((o) => o.label));

      const idle = () => {
        root.querySelector('#int-body').innerHTML = emptyState({
          title: 'Nothing verified yet',
          body: 'Pick a building and press verify. The walk re-reads every stored reading '
              + 'for it in order, checks the signature chain, and compares the result '
              + 'against the anchor file held outside the database volume.',
        });
      };

      const draw = async () => {
        const body = root.querySelector('#int-body');
        const b = byId.get(state.building);
        /* The panel already holds the height it will have when the result
         * arrives, so nothing below it jumps when the walk finishes. */
        body.innerHTML = `
          <div class="running" aria-busy="true" aria-live="polite">
            <p>Walking the reading chain for <strong>${escapeHtml(b ? b.code : '')}</strong>&hellip;</p>
            <div class="progress"><span class="progress-bar"></span></div>
            <p class="note">Reading only. This can take a moment: it is every stored
            reading for the building, in order.</p>
          </div>`;

        const report = await api.verifyChain(state.building);
        const ok = report.chain_ok && report.checkpoint_ok !== false;
        root.querySelector('#int-scope').textContent = b
          ? `${b.code} · ${b.district}` : '';

        const check = (pass, name, note) => `
          <div class="check ${pass === null ? 'unknown' : pass ? 'pass' : 'fail'}">
            <span class="check-mark">${mark(pass === null ? 'dash' : pass ? 'check' : 'cross', { size: 15 })}</span>
            <span class="check-text">
              <span class="check-name">${name}</span>
              <span class="check-note">${note}</span>
            </span>
            <span class="check-verdict">${pass === null ? 'Not anchored' : pass ? 'Pass' : 'Fail'}</span>
          </div>`;

        body.innerHTML = `
          <div class="verdict ${ok ? 'good' : 'bad'}">
            <span class="verdict-shield">${icon(ok ? 'integrity' : 'warning')}</span>
            <span class="verdict-text">
              <span class="verdict-word">${ok ? 'Intact' : 'Broken'}</span>
              <span class="verdict-gloss">${ok
                ? 'Every stored reading for this building is the one that was signed when '
                  + 'it arrived. Nobody has changed them.'
                : 'The stored readings for this building can no longer be proved to be the '
                  + 'ones that were signed when they arrived.'}</span>
            </span>
            <span class="verdict-rows"><strong>${compact(report.rows)}</strong> rows checked</span>
          </div>

          <div class="checks">
            ${check(report.chain_ok, 'Chain walk verifies',
                    'Every reading still hashes to the value the next one recorded for it.')}
            ${check(report.checkpoint_ok, 'External anchor file matches',
                    report.checkpoint_ok === null
                      ? 'No anchor has been written for this building yet, so a deleted '
                        + 'tail could not be detected.'
                      : `The head written outside the database volume matches the chain${
                          report.checkpoint_seq ? ` at sequence ${compact(report.checkpoint_seq)}` : ''}.`)}
            ${check(report.anchor_matches_database, 'Anchor and database agree',
                    'The two copies of the chain head are the same value, so the chain was '
                    + 'not rebuilt with only one of them updated.')}
          </div>

          ${report.break ? `
            <div class="break-panel" role="alert">
              <h4 class="plain">Where it first failed</h4>
              <dl class="facts">
                <div><dt>First failing sequence</dt><dd>${report.break.seq}</dd></div>
                <div><dt>Verified up to</dt><dd>${compact(Math.max(0, report.break.seq - 1))}</dd></div>
                <div><dt>Rows after the break</dt><dd>${compact(Math.max(0, report.rows - report.break.seq))}</dd></div>
                <div><dt>Building</dt><dd>${escapeHtml(b ? b.code : '')}</dd></div>
              </dl>
              <p>There are two things that produce this, and the platform cannot tell you
              which: a reading was altered after it was signed, or the chain was rebuilt
              from this point onward. Either way the break is at the sequence above.</p>
              <p>The readings are still on disk and are still shown everywhere else in
              this platform. What they have lost is not their value but their proof —
              they can no longer be shown to be unaltered. <strong>There is deliberately
              no button here that fixes this.</strong> A platform that can silently repair
              a chain can also silently erase the thing the break is telling you.</p>
            </div>` : ''}

          ${report.hint ? `<p class="note">${escapeHtml(report.hint)}</p>` : ''}

          <div class="form-actions">
            <button type="button" class="secondary" data-verify-again>Verify again</button>
            <a class="btn secondary" href="#/alerts">See this building&rsquo;s alerts</a>
          </div>

          <p class="caption">${ok
            ? 'This says nothing about whether the meter was accurate. It says only that '
              + 'nobody changed what the meter sent. Whether the meter itself behaved is '
              + 'the alert inbox&rsquo;s question.'
            : 'Whether the meter itself behaved is a separate question, and one the alert '
              + 'inbox answers.'}</p>`;

        body.querySelector('[data-verify-again]').addEventListener('click',
          () => guard(root, draw));
      };

      const sync = () => {
        const b = byId.get(state.building);
        button.disabled = !state.building;
        button.textContent = b ? `Verify ${b.code}` : 'Pick a building first';
      };

      input.addEventListener('change', () => {
        const value = pickerValue(input, options);
        if (value === null || value === state.building) return;
        state.building = value;
        sync();
      });
      button.addEventListener('click', () => guard(root, draw));

      sync();
      idle();
    });
  },
};

/* -------------------------------------------------------------------- admin */

const MIN_PASSWORD = 12;

function passwordProblem({ password, confirm }) {
  if (!password) return 'A password is required.';
  if (password.length < MIN_PASSWORD) {
    return `At least ${MIN_PASSWORD} characters. That one has ${password.length}.`;
  }
  if (confirm !== undefined && password !== confirm) return 'The two passwords do not match.';
  return null;
}

const ADMIN_PURPOSE = 'Accounts, what this deployment is currently exposed to, and '
  + 'who did what. Admin role only.';

export const admin = {
  title: 'Administration',
  /* `requiredRole` is what dims this item in the rail, and the rail keeps it for
   * everyone: an item that vanishes teaches nobody what they cannot see. The
   * view itself decides what to render, and for a non-admin that is an
   * explanation rather than a locked door. */
  requiredRole: 'admin',
  async render(root, ctx) {
    if (!can(ctx.user, 'admin')) {
      root.innerHTML = `
        ${pageHead({ root, title: 'Administration', description: ADMIN_PURPOSE })}
        <div class="note-panel" role="note">
          ${icon('lock')}
          <p><strong>Administration needs the admin role, and yours is
          ${escapeHtml(ctx.user.role)}.</strong> Nothing here is hidden from you as a
          matter of secrecy; it is simply a set of controls your role cannot use.</p>
        </div>
        <section class="panel">
          <header><h3>What this screen holds</h3></header>
          <ul class="prose-list">
            <li><strong>Security posture</strong> — what this deployment is currently
            exposed to: the session cookie flags, the timeouts, the minimum password
            length, and the recent failed sign-in attempts.</li>
            <li><strong>Accounts</strong> — the list of people who can sign in, their
            roles, and the actions that create, disable and reset them.</li>
            <li><strong>Audit log</strong> — the most recent authenticated actions,
            including the ones that were refused.</li>
          </ul>
          <p class="caption">Your own password and the list of places this account is
          signed in are on the <a href="#/account">account screen</a>, which needs no
          admin role. If you need one of the things above, ask an administrator to do
          it or to change your role — a role change is written to the audit log with
          both usernames, yours and theirs.</p>
        </section>`;
      return;
    }

    root.innerHTML = `
      ${pageHead({ root,
        title: 'Administration',
        description: ADMIN_PURPOSE,
      })}
      ${skeletonRows(6)}`;

    await guard(root, async () => {
      const [users, posture, auditRows] = await Promise.all([
        api.users(), api.securityPosture(), api.audit(50),
      ]);

      const auditColumns = [
        { key: 'ts', label: 'When', sortable: true,
          render: (r) => `<span class="mono">${fmtDateTime(r.ts)}</span>` },
        { key: 'username', label: 'User', sortable: true,
          render: (r) => escapeHtml(r.username || '—') },
        { key: 'action', label: 'Action', sortable: true, cls: 'mono',
          render: (r) => escapeHtml(r.action) },
        { key: 'target', label: 'Target', render: (r) => escapeHtml(r.target || '—') },
        { key: 'outcome', label: 'Outcome', sortable: true,
          /* Denied stands out: the row is critical-soft, the word is bold and it
           * carries the cross. A refused action is the one line in an audit log
           * anybody scans for. */
          render: (r) => (r.outcome === 'denied'
            ? `<span class="sev critical">${mark('cross', { size: 11 })}Denied</span>`
            : `<span class="muted">${escapeHtml(r.outcome)}</span>`) },
        { key: 'ip', label: 'Address', cls: 'mono', render: (r) => escapeHtml(r.ip || '—') },
      ];
      const auditState = { sortKey: 'ts', sortDir: 'desc', deniedOnly: false };

      root.innerHTML = `
        ${pageHead({ root,
          title: 'Administration',
          description: ADMIN_PURPOSE,
          actions: '<button id="recompute-btn" type="button" class="secondary">'
                   + 'Recompute candidates</button>'
                   + '<button id="user-add" type="button" class="primary">Add account</button>',
        })}

        <section class="panel">
          <header><h3>Security posture</h3>
            <span class="scope">what this deployment is exposed to right now</span></header>

          <!-- The Secure flag gets a callout of its own because it is the one
               item on this screen that changes what an attacker can do, and a
               row in a fact grid reads exactly like the four rows that do not
               matter. -->
          ${posture.cookie_secure ? '' : `
          <div class="posture-alert" role="alert">
            <h4 class="plain">The session cookie is being sent without the Secure flag.</h4>
            <p>Without it the browser will send the session cookie over plain HTTP as well
            as HTTPS. Anyone able to watch the network between a signed-in browser and
            this server — the same wifi, the same switch, anything in between — can read
            that cookie and use it to act as that person, without ever needing their
            password. Turn it on before this is reachable over a network you do not
            control.</p>
          </div>`}

          <dl class="facts">
            <div><dt>Session cookie Secure flag</dt>
            <dd>${posture.cookie_secure
              ? `<span class="sev medium">${mark('check', { size: 11 })}On</span>`
              : `<span class="sev critical">${mark('cross', { size: 11 })}Off</span>`}</dd></div>
            <div><dt>Idle timeout</dt><dd>${posture.session_idle_timeout_hours} hours</dd></div>
            <div><dt>Absolute session lifetime</dt><dd>${posture.session_absolute_lifetime_days} days</dd></div>
            <div><dt>Minimum password length</dt><dd>${posture.password_min_length} characters</dd></div>
            <div><dt>Accounts</dt><dd>${posture.users}${
              users.filter((u) => !u.is_active).length
                ? `, ${users.filter((u) => !u.is_active).length} disabled` : ', none disabled'}</dd></div>
          </dl>

          ${posture.recent_failed_logins.length ? `<h4 class="section-label">Recent failed sign-ins</h4>
            <div class="table-wrap"><table>
              <thead><tr><th>When</th><th>Username tried</th><th>Address</th></tr></thead>
              <tbody>${posture.recent_failed_logins.map((f) => `<tr>
                <td class="mono">${fmtDateTime(f.ts)}</td><td>${escapeHtml(f.username)}</td>
                <td class="mono">${escapeHtml(f.ip)}</td></tr>`).join('')}</tbody>
            </table></div>
            <p class="caption">A username appearing here does <strong>not</strong> mean the
            account exists. Sign-in answers the same way for a wrong password, an unknown
            account and a disabled one — that is deliberate, and it means this list cannot
            tell you which of the three each row was either.</p>`
            : '<p class="caption">No failed sign-ins recorded.</p>'}
        </section>

        <section class="panel">
          <header><h3>Accounts</h3><span class="muted small">${users.length} total</span></header>
          <div class="table-wrap"><table>
            <thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Status</th>
              <th>Last login</th><th><span class="sr-only">Actions</span></th></tr></thead>
            <!-- The two cells an admin cannot use on their own row show a lock
                 and the reason, not a greyed-out control. A disabled select
                 invites a click and explains nothing; the words do both. -->
            <tbody>${users.map((u) => {
              const self = u.id === ctx.user.id || u.username === ctx.user.username;
              return `<tr>
              <td class="mono">${escapeHtml(u.username)}${
                self ? '<span class="badge">you</span>' : ''}</td>
              <td>${escapeHtml(u.display_name)}</td>
              <td>${self && u.role === 'admin'
                ? `<span class="locked-cell">${mark('cross', { size: 11 })}admin — your own</span>`
                : selectWrap(`<select class="inline" data-role="${escapeHtml(u.id)}"
                        data-current="${escapeHtml(u.role)}"
                        data-username="${escapeHtml(u.username)}"
                        aria-label="Role for ${escapeHtml(u.username)}">
                  ${['viewer', 'analyst', 'admin'].map((r) =>
                    `<option value="${r}"${r === u.role ? ' selected' : ''}>${r}</option>`).join('')}
                </select>`)}
              </td>
              <td>${u.must_change_password
                ? `<span class="state-cell warn">${mark('dash', { size: 11 })}Must set password</span>`
                : u.is_active
                  ? `<span class="state-cell ok">${mark('check', { size: 11 })}Active</span>`
                  : `<span class="state-cell bad">${mark('cross', { size: 11 })}Disabled</span>`}</td>
              <td class="mono">${fmtDateTime(u.last_login_at)}</td>
              <td class="row-actions">
                ${self
                  ? `<span class="locked-cell">${mark('cross', { size: 11 })}cannot disable your own account</span>`
                  : `<button class="secondary small" type="button" data-toggle="${escapeHtml(u.id)}"
                  data-active="${u.is_active}" data-username="${escapeHtml(u.username)}"
                  aria-label="${u.is_active ? 'Disable' : 'Enable'} ${escapeHtml(u.username)}"
                  >${u.is_active ? 'Disable' : 'Enable'}</button>`}
                <button class="secondary small" type="button" data-reset="${escapeHtml(u.id)}"
                  data-username="${escapeHtml(u.username)}"
                  aria-label="Reset the password for ${escapeHtml(u.username)}">Reset password</button>
              </td>
            </tr>`; }).join('')}</tbody>
          </table></div>
          <p class="caption">An admin cannot remove their own admin role or disable
          their own account: leaving a deployment with no administrator is recoverable
          only with shell access to the database.</p>
        </section>

        <section class="panel">
          <header>
            <h3>Audit log</h3>
            <span class="scope">the 50 most recent events — this is the whole log the API
              returns, not a page of it</span>
            <button type="button" class="seg-btn deny-toggle" id="denied-only"
                    aria-pressed="false">Denied only</button>
          </header>
          <div id="audit-body"></div>
        </section>`;

      /* Rejections have to land somewhere. `guard` covers the work inside render,
       * but a throw between building the markup and writing it would escape into an
       * unhandled rejection - a screen that silently stops responding to its own
       * buttons, with the reason only in the console. */
      const reload = (message) => admin.render(root, ctx).then(() => {
        if (message) setStatus(root, { kind: 'ok', message });
      }).catch((error) => {
        setStatus(root, { kind: 'error', message: error?.detail || String(error) });
      });

      const paintAudit = () => {
        const body = root.querySelector('#audit-body');
        const rows = auditState.deniedOnly
          ? auditRows.filter((r) => r.outcome === 'denied')
          : auditRows;
        body.innerHTML = rows.length
          ? dataTable({ columns: auditColumns,
                        rows: rows.map((r) => ({ ...r,
                          _cls: r.outcome === 'denied' ? 'denied-row' : '' })),
                        sortKey: auditState.sortKey, sortDir: auditState.sortDir })
          : emptyState({
              title: auditState.deniedOnly ? 'Nothing was refused' : 'Nothing recorded yet',
              body: auditState.deniedOnly
                ? 'No action in the 50 most recent events was denied.'
                : 'Every authenticated action appears here as it happens.' });
        wireSort(body, auditState, paintAudit);
      };
      paintAudit();

      root.querySelector('#denied-only').addEventListener('click', (event) => {
        auditState.deniedOnly = !auditState.deniedOnly;
        event.currentTarget.setAttribute('aria-pressed', String(auditState.deniedOnly));
        paintAudit();
      });

      /* A role change used to fire on `change` with no confirmation and no
       * visible result: the select moved, a request went out, and the only way
       * to know it worked was to reload. Both halves of that are fixed. */
      root.querySelectorAll('[data-role]').forEach((select) => {
        select.addEventListener('change', async () => {
          const next = select.value;
          const previous = select.dataset.current;
          const name = select.dataset.username;
          const ok = await confirmAction({
            title: `Change the role of ${name}?`,
            description: `${name} becomes ${next}, from ${previous}. `
              + (next === 'admin'
                ? 'An admin can create accounts, reset passwords and read the audit log.'
                : next === 'analyst'
                  ? 'An analyst can run the optimizer and acknowledge alerts.'
                  : 'A viewer can read the portfolio but change nothing.'),
            confirmLabel: `Make ${name} ${next}`,
          });
          if (!ok) { select.value = previous; return; }
          await guard(root, async () => {
            await api.updateUser(select.dataset.role, { role: next });
            await reload(`${name} is now ${next}.`);
          });
        });
      });

      root.querySelectorAll('[data-toggle]').forEach((button) => {
        button.addEventListener('click', async () => {
          const disabling = button.dataset.active === 'true';
          const name = button.dataset.username;
          if (disabling) {
            const ok = await confirmAction({
              title: `Disable ${name}?`,
              description: 'Their sessions stop working immediately and they cannot sign in '
                         + 'again until the account is enabled. Nothing is deleted.',
              confirmLabel: `Disable ${name}`,
            });
            if (!ok) return;
          }
          await guard(root, async () => {
            await api.updateUser(button.dataset.toggle, { is_active: !disabling });
            await reload(`${name} is now ${disabling ? 'disabled' : 'active'}.`);
          });
        });
      });

      root.querySelectorAll('[data-reset]').forEach((button) => {
        button.addEventListener('click', async () => {
          const name = button.dataset.username;
          const values = await openDialog({
            title: `Reset the password for ${name}`,
            description: 'They will be required to choose a new one at next login, and every '
                       + 'session they currently hold stops working.',
            submitLabel: 'Reset password',
            validate: passwordProblem,
            fields: [
              { name: 'password', label: 'New password', type: 'password',
                autocomplete: 'new-password', minlength: MIN_PASSWORD,
                hint: `At least ${MIN_PASSWORD} characters.` },
              { name: 'confirm', label: 'Repeat the new password', type: 'password',
                autocomplete: 'new-password' },
            ],
          });
          if (!values) return;
          await guard(root, async () => {
            await api.resetPassword(button.dataset.reset, values.password);
            await reload(`Password reset for ${name}. Give it to them once, in person.`);
          });
        });
      });

      /* Also in the workspace header, for the same reason. */
      document.querySelector('#user-add')?.addEventListener('click', async () => {
        const values = await openDialog({
          title: 'Add an account',
          description: 'There is no self-service registration. The temporary password has to '
                     + 'be changed at first login.',
          submitLabel: 'Create account',
          validate: (v) => (!v.username.trim() ? 'A username is required.' : passwordProblem(v)),
          fields: [
            { name: 'username', label: 'Username', autocomplete: 'off' },
            { name: 'password', label: 'Temporary password', type: 'password',
              autocomplete: 'new-password', minlength: MIN_PASSWORD,
              hint: `At least ${MIN_PASSWORD} characters.` },
            { name: 'confirm', label: 'Repeat the password', type: 'password',
              autocomplete: 'new-password' },
            { name: 'role', label: 'Role', type: 'select',
              options: ['viewer', 'analyst', 'admin'], value: 'viewer' },
          ],
        });
        if (!values) return;
        await guard(root, async () => {
          await api.createUser({
            username: values.username.trim(),
            password: values.password,
            role: values.role,
          });
          await reload(`Account ${values.username.trim()} created as ${values.role}.`);
        });
      });

      /* Recompute was a curl command with no face. The endpoint has existed and
       * been admin-gated since Phase 1; this is it finally wired to a button, so
       * "the parameters are data, not code" is something you can demonstrate by
       * clicking rather than by opening a terminal. */
      root.querySelector('#recompute-btn')?.addEventListener('click', async () => {
        const ok = await confirmAction({
          title: 'Recompute the candidate set?',
          description: 'Re-expands every building × measure combination from the '
                     + 'current catalog.csv and params.yaml, rewrites the stored '
                     + 'candidates, and invalidates the optimizer cache. Any change '
                     + 'to data/ takes effect at the next solve.',
          confirmLabel: 'Recompute',
          confirmText: String(await api.meta().then((m) => m.buildings).catch(() => 50)),
        });
        if (!ok) return;
        await guard(root, async () => {
          const result = await api.recompute();
          setStatus(root, {
            kind: 'ok',
            message: `Candidates rebuilt: ${result.candidates} options over `
                   + `${result.buildings} buildings from ${result.interventions} `
                   + `catalog rows (inputs ${result.inputs_hash}…).`
                   + (result.uncited_catalog_rows.length
                     ? ` Uncited rows remain: ${result.uncited_catalog_rows.join(', ')}.`
                     : ''),
          });
        });
      });
    });
  },
};

/* ------------------------------------------------------------------ account */

/* Enough of a user-agent string to recognise a session by, and no more. This is
 * not device fingerprinting: the question the column answers is "was that me, in
 * that browser, on that machine" and a version number does not help answer it.
 * The full string stays in the title attribute. */
function browserName(agent) {
  const ua = String(agent || '');
  if (!ua) return '—';
  const engine = /Edg\//.test(ua) ? 'Edge'
    : /OPR\//.test(ua) ? 'Opera'
    : /Chrome\//.test(ua) ? 'Chrome'
    : /Firefox\//.test(ua) ? 'Firefox'
    : /Safari\//.test(ua) ? 'Safari'
    : 'Unknown browser';
  const platform = /Windows/.test(ua) ? 'Windows'
    : /Macintosh|Mac OS/.test(ua) ? 'macOS'
    : /Android/.test(ua) ? 'Android'
    : /iPhone|iPad/.test(ua) ? 'iOS'
    : /Linux/.test(ua) ? 'Linux'
    : '';
  return platform ? `${engine} on ${platform}` : engine;
}

const ACCOUNT_PURPOSE = 'Your password, and every place this account is currently '
  + 'signed in. Changing the password is what ends the other sessions.';

export const account = {
  title: 'Account',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({ root,
        title: 'Your account',
        description: ACCOUNT_PURPOSE,
      })}
      ${skeletonRows(4)}`;

    await guard(root, async () => {
      const sessions = await api.mySessions();
      const others = Math.max(0, sessions.length - 1);

      root.innerHTML = `
        ${pageHead({ root,
          title: 'Your account',
          description: ACCOUNT_PURPOSE,
        })}

        <div class="two-col aside-first">
          <section class="panel">
            <header><h3>Change password</h3></header>

            <!-- Said before the fields, not after the submit. Changing the
                 password is also the only way to end another session in this
                 platform, so somebody may be here to do exactly that - and
                 somebody else may not have realised it happens at all. -->
            <div class="posture-alert" role="note">
              <h4 class="plain">Changing your password signs out every other session.</h4>
              <p>${others === 0
                ? 'There are no others right now, so this will only affect the browser '
                  + 'you are using.'
                : `There ${others === 1 ? 'is 1 other' : `are ${others} others`} right now. `
                  + `${others === 1 ? 'It' : 'They'} will stop working immediately.`}</p>
            </div>

            <form id="pw-form" class="form">
              <label for="pw-current">Current password
                <span class="password-field">
                  <input type="password" id="pw-current" class="mono"
                         autocomplete="current-password" required>
                  <button type="button" class="reveal" data-reveal="pw-current"
                          aria-pressed="false" aria-label="Show the current password">
                    ${icon('eye')}</button>
                </span></label>

              <label for="pw-new">New password
                <input type="password" id="pw-new" class="mono" autocomplete="new-password"
                       minlength="${MIN_PASSWORD}" required>
                <span class="meter" id="pw-meter" aria-hidden="true"><span></span></span>
                <span class="hint" id="pw-hint">At least ${MIN_PASSWORD} characters.</span></label>

              <label for="pw-confirm">Repeat the new password
                <input type="password" id="pw-confirm" class="mono"
                       autocomplete="new-password" required>
                <span class="hint" id="pw-match"></span></label>

              <div id="pw-result"></div>
              <div class="form-actions">
                <button type="submit" class="primary" id="pw-submit" disabled>
                  Fill in every field</button>
              </div>
              <p class="caption">The three fields are named so a password manager can fill
              them and save the new one.</p>
            </form>
          </section>

          <section class="panel">
            <header>
              <h3>Where this account is signed in</h3>
              <span class="scope">${sessions.length} active</span>
            </header>
            ${sessions.length ? `<div class="table-wrap"><table>
              <thead><tr><th>Started</th><th>Last seen</th><th>Address</th><th>Browser</th></tr></thead>
              <tbody>${sessions.map((s) => `<tr${s.current ? ' class="chosen"' : ''}>
                <td class="mono">${fmtDateTime(s.created_at)}${s.current
                  ? `<span class="state-cell ok">${mark('check', { size: 11 })}this one</span>` : ''}</td>
                <td class="mono">${fmtDateTime(s.last_seen_at)}</td>
                <td class="mono">${escapeHtml(s.ip || '—')}</td>
                <td class="muted small wrap" title="${escapeHtml(s.user_agent || '')}"
                  >${escapeHtml(browserName(s.user_agent))}</td>
              </tr>`).join('')}</tbody>
            </table></div>` : emptyState({
              title: 'Only this session',
              body: 'This account is signed in here and nowhere else. Others appear when '
                  + 'you sign in from another browser or machine, and drop off by '
                  + 'themselves after the 8-hour idle limit or the 7-day absolute one.' })}
            <p class="caption">A session you do not recognise is a reason to change your
            password <strong>now</strong>, because that is what ends the others. There is
            no per-session sign-out in this platform, and this screen will not pretend
            there is.</p>
          </section>
        </div>`;

      /* Reveal, on the one field where it earns its place: the current password
       * is the one people mistype and then cannot tell why they were refused. */
      root.querySelectorAll('[data-reveal]').forEach((button) => {
        button.addEventListener('click', () => {
          const field = root.querySelector(`#${button.dataset.reveal}`);
          const shown = field.type === 'text';
          field.type = shown ? 'password' : 'text';
          button.innerHTML = icon(shown ? 'eye' : 'eye-off');
          button.setAttribute('aria-pressed', String(!shown));
          button.setAttribute('aria-label',
            shown ? 'Show the current password' : 'Hide the current password');
          field.focus();
        });
      });

      /* Live, because a rule that only fires on submit is a rule the person
       * finds out about after they have already committed to a password. */
      const fresh = root.querySelector('#pw-new');
      const again = root.querySelector('#pw-confirm');
      const current = root.querySelector('#pw-current');
      const meter = root.querySelector('#pw-meter');
      const hint = root.querySelector('#pw-hint');
      const match = root.querySelector('#pw-match');

      const check = () => {
        const value = fresh.value;
        const long = value.length >= MIN_PASSWORD;
        meter.dataset.state = !value ? 'empty' : long ? 'ok' : 'short';
        meter.style.setProperty('--fill',
          `${Math.min(100, (value.length / MIN_PASSWORD) * 100)}%`);
        hint.textContent = !value
          ? `At least ${MIN_PASSWORD} characters.`
          : long
            ? `${value.length} characters — long enough.`
            : `${value.length} of ${MIN_PASSWORD} characters.`;
        hint.className = `hint ${long ? 'ok' : 'warn'}`;

        match.textContent = !again.value ? ''
          : again.value === value ? 'The two match.' : 'The two do not match yet.';
        match.className = `hint ${again.value && again.value === value ? 'ok' : 'warn'}`;

        /* The label says what is missing rather than the button going quiet.
         * "Must differ from the current one" is checked here as well as on
         * submit, because finding out afterwards means choosing again. */
        const submit = root.querySelector('#pw-submit');
        const same = value !== '' && value === current.value;
        const ready = current.value && long && again.value === value && !same;
        submit.disabled = !ready;
        submit.textContent = ready ? 'Change password and sign out other sessions'
          : !current.value ? 'Enter your current password'
          : !long ? 'The new password is too short'
          : same ? 'The new password must differ from the current one'
          : 'The two new passwords must match';
      };
      [current, fresh, again].forEach((field) => field.addEventListener('input', check));
      check();

      root.querySelector('#pw-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const result = root.querySelector('#pw-result');
        /* Checked here as well as by the server, because a mismatch that only
         * surfaces as a rejected request has already sent the password. */
        const problem = fresh.value === current.value
          ? 'The new password must differ from the current one.'
          : passwordProblem({ password: fresh.value, confirm: again.value });
        if (problem) {
          result.innerHTML = `<div class="error-box" role="alert">${escapeHtml(problem)}</div>`;
          fresh.focus();
          return;
        }
        try {
          await api.changePassword(current.value, fresh.value);
          result.innerHTML = '';
          setStatus(root, {
            kind: 'ok',
            message: `Password changed by ${ctx.user.username} at `
                   + `${new Date().toLocaleTimeString('en-GB')}. `
                   + `${others ? `${others} other session${others === 1 ? '' : 's'} signed out.`
                              : 'No other sessions were open.'}`,
          });
          root.querySelector('#pw-form').reset();
          check();
          ctx.onPasswordChanged?.();
        } catch (error) {
          result.innerHTML = errorBox(error);
        }
      });
    });
  },
};
