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
import {
  compact, escapeHtml, lineChart, proportionBar, stackedBars, statTile,
} from './charts.js';
import {
  clearStatus, confirmAction, dataTable, emptyState, freshness, openDialog,
  pageHead, picker, pickerValue, selectWrap, setStatus, skeletonChart,
  skeletonRows, skeletonTiles, wirePicker, wireSort,
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
    const slot = root.querySelector('[data-status]');
    if (slot) {
      const detail = error instanceof ApiError ? error.detail : String(error);
      setStatus(root, { kind: 'error', message: detail });
    } else {
      root.innerHTML = errorBox(error);
    }
  }
}

/* Wires the Refresh button that `freshness()` renders. It lives in the
 * workspace header, which is outside the view's own container. */
function wireRefresh(root, reload) {
  document.querySelector('[data-refresh]')?.addEventListener('click', () => reload());
}

const buildingOptions = (buildings) =>
  buildings.map((b) => ({ value: b.id, label: b.code }));

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
  return `<div class="chart-filters">
    <span class="filter-label" id="range-label">Window</span>
    <div class="segmented" role="group" aria-labelledby="range-label">${buttons}</div>
  </div>`;
}

/* Delegated, so the buttons can be replaced by a redraw without the listener
 * going with them. */
function wireRange(root, onChange) {
  const group = root.querySelector('.chart-filters .segmented');
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

/* ------------------------------------------------------------------ overview */

export const overview = {
  title: 'Overview',
  /* Kept on the view rather than inside render(), so leaving the screen and
   * coming back does not silently reset the window the reader chose. */
  state: { days: 30 },
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({
        title: 'Portfolio overview',
        description: 'What the platform is measuring, and the allocation it last recommended.',
      })}
      ${skeletonTiles(4)}${skeletonChart()}`;

    const state = overview.state;

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
          ${statTile('Buildings', compact(summary.buildings))}
          ${statTile('Readings stored', compact(summary.readings))}
          ${statTile('Open alerts', compact(summary.open_anomalies),
                     Object.entries(summary.open_by_severity || {})
                       .map(([k, v]) => `${k} ${v}`).join(' · '),
                     { spark: dailyTotals(dailyData.days),
                       trend: halfOverHalf(dailyTotals(dailyData.days)) })}
          ${statTile('Costed on measurement', `${fromForecast}/${summary.buildings}`,
                     'F3: buildings whose annual kWh comes from the forecast')}
        </div>

        <section class="panel">
          <header>
            <h3>Portfolio demand</h3>
            <span class="muted small">point at the chart, or focus it and use the arrow keys</span>
          </header>
          ${lineChart([{ label: 'Portfolio demand', points: loadData.points }], { unit: 'kW' })}
          <p class="caption">The simulator runs at 720&times;, so data time runs ahead of
          the wall clock — every window in the platform is measured in data time for
          that reason, and the clock above says which moment this was read at.</p>
        </section>

        <section class="panel">
          <header>
            <h3>Alerts per day</h3>
            <span class="muted small">stacked critical, high, medium</span>
          </header>
          ${stackedBars(dailyData.days, ['critical', 'high', 'medium'])}
        </section>`;

      root.innerHTML = `
        ${pageHead({
          title: 'Portfolio overview',
          description: 'What the platform is measuring, and the allocation it last recommended.',
          meta: freshness(summary.data_clock),
        })}

        ${rangeControl(state.days)}
        <div id="ov-panels">${panels(load, daily)}</div>

        <section class="panel">
          <header><h3>Most recent allocation</h3></header>
          ${run ? `
            <div class="stat-row compact">
              ${statTile('Budget', compact(run.budget_egp) + ' EGP')}
              ${statTile('Funded', run.buildings_funded + ' buildings')}
              ${statTile('Spent', compact(run.total_cost_egp) + ' EGP')}
              ${statTile('Lifetime benefit', compact(run.total_benefit_kgco2e) + ' kgCO₂e')}
            </div>
            <p class="caption">${escapeHtml(run.solver)} on ${escapeHtml(run.objective)},
            ${fmtDateTime(run.created_at)}.</p>`
            : emptyState({
                title: 'No allocation has been run yet',
                body: 'Open the Map tab, set a budget and a method, and the optimizer will '
                    + 'store its recommendation here.',
              })}
        </section>`;

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

export const forecasts = {
  title: 'Forecasts',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({
        title: 'Load forecasting',
        description: 'Which model is costing each building, and where it is wrong.',
      })}
      ${skeletonChart()}`;

    await guard(root, async () => {
      const [metrics, buildings] = await Promise.all([
        api.get('/metrics/forecast'), api.get('/buildings'),
      ]);
      const options = buildingOptions(buildings);
      const state = { building: options[0]?.value };

      /* No per-building error is shown, because /metrics/forecast does not carry
       * one: it aggregates by model_version. A MAPE chip here would be a number
       * this screen invented. */

      root.innerHTML = `
        ${pageHead({
          title: 'Load forecasting',
          description: 'Which model is costing each building, and where it is wrong.',
        })}

        <section class="panel">
          <header><h3>Which forecaster is in use</h3></header>
          ${proportionBar((metrics.by_model || []).map((m) => ({
            label: `${m.model_version} (${m.buildings})`, value: m.buildings,
          })))}
          <p class="caption">A portfolio where the seasonal-naive baseline wins on many
          buildings is telling you the load is close to perfectly weekly. That is
          information, not a failure — and it is why the baseline is kept rather than
          hidden.</p>
        </section>

        <section class="panel">
          <header>
            <h3>Actual against forecast</h3>
            <div class="toolbar">
              ${picker({ id: 'fc-building', label: 'Building', options, value: state.building })}
            </div>
          </header>
          <div id="fc-chart">${skeletonChart()}</div>
          <p class="caption">Two weeks of hourly readings against what the model expected.
          A single error figure says the model is good without letting anyone see where
          it is wrong.</p>
        </section>`;

      const input = root.querySelector('#fc-building');
      wirePicker(input);
      const draw = async () => {
        const target = root.querySelector('#fc-chart');
        target.innerHTML = skeletonChart();
        const data = await api.forecast(state.building, 336);
        target.innerHTML = lineChart([
          { label: 'Actual', points: data.actual },
          { label: 'Forecast', points: data.forecast },
        ], { unit: 'kW' });
      };

      /* A datalist reports every keystroke as a change, so resolve the text and
       * only fetch when it actually names a different building. */
      input.addEventListener('change', () => {
        const value = pickerValue(input, options);
        if (!value || value === state.building) return;
        state.building = value;
        guard(root, draw);
      });

      await draw();
    });
  },
};

/* ------------------------------------------------------------------- alerts */

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
      ${pageHead({
        title: 'Alert inbox',
        description: 'Readings that deviate from what the forecaster expected, worst first.',
      })}
      ${skeletonRows(8)}`;

    await guard(root, async () => {
      const buildings = await api.get('/buildings');
      const options = [{ value: '', label: 'All buildings' }, ...buildingOptions(buildings)];

      /* One sentence, stated rather than implied. The list is a capped slice of
       * about thirty thousand rows; acknowledging one used to refill it from the
       * pool with nothing on screen saying where the others were. */
      const countLine = (shown) => {
        if (!state.onlyOpen) return `Showing the ${shown} largest deviations, open and closed.`;
        const total = state.severity ? state.bySeverity[state.severity] : state.total;
        const scope = `${state.severity ? `${state.severity} ` : ''}alert`;
        if (total === null || total === undefined) return `Showing ${shown}.`;
        return total > shown
          ? `Showing the ${shown} largest of ${compact(total)} open ${scope}s.`
          : `Showing all ${shown} open ${scope}s.`;
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
        { key: 'expected_kw', label: 'Expected vs observed (kW)', num: true, sortable: true,
          render: (r) => `${compact(r.expected_kw)} / <b>${compact(r.observed_kw)}</b>` },
        { key: 'robust_z', label: 'Deviation z', num: true, sortable: true,
          value: (r) => (r.robust_z === null ? null : Math.abs(r.robust_z)),
          render: (r) => (r.robust_z === null ? '—'
            : `<span class="sev-figure ${escapeHtml(r.severity)}">${
                r.robust_z > 0 ? '+' : ''}${r.robust_z.toFixed(1)}</span>`) },
        { key: 'severity', label: 'Severity', sortable: true,
          render: (r) => `<span class="sev ${escapeHtml(r.severity)}">${escapeHtml(r.severity)}</span>` },
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
           <span class="muted">Sorting applies to the rows shown, not to the whole inbox.</span>`;

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

      const paintActionBar = () => {
        const bar = root.querySelector('#alert-actions');
        const n = state.selected.size;
        if (!n) { bar.hidden = true; bar.innerHTML = ''; return; }
        bar.hidden = false;
        bar.innerHTML = `
          <span><strong>${n}</strong> selected</span>
          <button type="button" class="primary" data-ack-selected>Acknowledge ${n} selected</button>
          <button type="button" class="ghost small" data-clear-selection>Clear selection</button>`;

        bar.querySelector('[data-clear-selection]').addEventListener('click', () => {
          state.selected = new Set();
          paint();
        });
        bar.querySelector('[data-ack-selected]').addEventListener('click', async () => {
          const ids = [...state.selected];
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
          kind: 'ok', message, actionLabel: 'Undo', actionAttr: 'data-undo',
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
        const params = `?limit=100&only_open=${state.onlyOpen}` +
          (state.severity ? `&severity=${encodeURIComponent(state.severity)}` : '') +
          (state.building ? `&building_id=${encodeURIComponent(state.building)}` : '');
        const [rows, summary] = await Promise.all([api.anomalyFeed(params), api.summary()]);
        state.rows = rows;
        state.total = summary.open_anomalies;
        state.bySeverity = summary.open_by_severity || {};
        /* Rendered by pageHead() into the workspace header, which is a
         * SIBLING of this view's container - so it is looked up on the
         * document, not on `root`. */
        const clock = document.querySelector('#alert-clock');
        if (clock) clock.innerHTML = freshness(summary.data_clock);
        wireRefresh(root, load);
        paint();
      };

      /* The reference's layout: a filter strip of its own above the table,
       * groups separated by a 1px rule, the destructive action pushed to the
       * far right as quiet text — then the table filling the rest of the pane
       * and scrolling inside its own container. */
      root.innerHTML = `
        ${pageHead({
          title: 'Alert inbox',
          description: 'Readings that deviate from what the forecaster expected, worst first.',
          meta: '<span id="alert-clock"></span>',
        })}

        <div class="panel toolbar-panel">
          <div class="toolbar">
            <div class="group">
              <label class="inline-label" for="alert-sev">Severity</label>
              ${selectWrap(`<select id="alert-sev" class="inline">
                <option value="">All severities</option>
                <option value="critical">Critical</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
              </select>`)}
            </div>
            <span class="rule"></span>
            <div class="group">
              ${picker({ id: 'alert-building', label: 'Building', options, value: '' })}
            </div>
            <span class="rule"></span>
            <label class="check"><input type="checkbox" id="alert-open" checked> Open only</label>
          </div>
          ${analyst ? `<button id="alert-ack-bulk" type="button" class="link">
            Close a whole group…</button>` : ''}
        </div>

        <div id="alert-actions" class="action-bar" hidden></div>

        <section class="panel flex">
          <div id="alert-body" class="table-host"></div>
          <div id="alert-foot" class="table-foot"></div>
        </section>

        <p class="caption standalone">Acknowledging is reversible — every close offers an
        undo, and closing a whole group at once needs a filter, because an unfiltered
        acknowledge would close every alert in the portfolio and is refused by the API.</p>`;

      root.querySelector('#alert-sev').addEventListener('change', (e) => {
        state.severity = e.target.value;
        state.selected = new Set();
        clearStatus(root);
        guard(root, load);
      });

      const buildingInput = root.querySelector('#alert-building');
      wirePicker(buildingInput);
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

      const bulk = root.querySelector('#alert-ack-bulk');
      if (bulk) {
        bulk.addEventListener('click', async () => {
          /* This closes every alert matching the filter, not the hundred on
           * screen - which is what the old "Acknowledge all shown" claimed. It
           * says the real scope and the real count, and it cannot be undone by
           * ids because the ids of thirty thousand rows were never loaded. */
          if (!state.severity && !state.building) {
            setStatus(root, {
              kind: 'warn',
              message: 'Filter by severity or building first. Closing everything at once is '
                     + 'deliberately not one click.',
            });
            root.querySelector('#alert-sev').focus();
            return;
          }
          const scope = [
            state.severity ? `severity ${state.severity}` : null,
            state.building ? `building ${buildingInput.value}` : null,
          ].filter(Boolean).join(' and ');
          const count = state.severity && !state.building
            ? state.bySeverity[state.severity] : null;

          const ok = await confirmAction({
            title: 'Close a whole group of alerts',
            description: `This closes every open alert matching ${scope}`
              + `${count ? ` — ${compact(count)} of them` : ''}, not only the rows on screen. `
              + 'It cannot be undone from here, because the ids of the rows off screen were '
              + 'never loaded. Re-open them with a filtered un-acknowledge.',
            confirmLabel: 'Close them all',
          });
          if (!ok) return;

          await guard(root, async () => {
            const selectors = { acknowledged: true };
            if (state.severity) selectors.severity = state.severity;
            if (state.building) selectors.building_id = state.building;
            /* `changed` is the row count; `acknowledged` in the response is the
             * boolean that was asked for, not a total. */
            const result = await api.acknowledge(selectors);
            await load();
            setStatus(root, {
              kind: 'ok',
              message: `Closed ${compact(result?.changed ?? 0)} alerts matching ${scope}.`,
            });
          });
        });
      }

      await load();
    });
  },
};

/* --------------------------------------------------------------------- runs */

export const runs = {
  title: 'Allocations',
  async render(root, ctx) {
    const state = { rows: [], sortKey: 'created_at', sortDir: 'desc' };

    root.innerHTML = `
      ${pageHead({
        title: 'Stored allocations',
        description: 'Every recommendation the optimizer has produced, and the inputs it used.',
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
          render: (r) => escapeHtml(r.objective) },
        { key: 'solver', label: 'Method', sortable: true,
          render: (r) => escapeHtml(r.solver) },
        { key: 'buildings_funded', label: 'Funded', num: true, sortable: true,
          render: (r) => r.buildings_funded },
        { key: 'total_cost_egp', label: 'Spent EGP', num: true, sortable: true,
          render: (r) => compact(r.total_cost_egp) },
        { key: 'total_benefit_kgco2e', label: 'Lifetime kgCO₂e', num: true, sortable: true,
          render: (r) => compact(r.total_benefit_kgco2e) },
        /* The full hash was a column of forty characters that pushed everything
         * else off a narrow window. Enough to compare by eye, all of it on hover
         * and in the accessible name. */
        { key: 'inputs_hash', label: 'Inputs', cls: 'mono',
          render: (r) => `<span class="hash" title="${escapeHtml(r.inputs_hash)}">${
            escapeHtml(String(r.inputs_hash).slice(0, 10))}…</span>` },
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
      };

      root.innerHTML = `
        ${pageHead({
          title: 'Stored allocations',
          description: 'Every recommendation the optimizer has produced, and the inputs it used.',
          actions: '<button type="button" class="ghost" data-refresh>Refresh</button>',
        })}
        <section class="panel">
          <header><h3>Runs</h3><span class="muted small">${state.rows.length} stored</span></header>
          <div id="runs-body"></div>
          <p class="caption">Every run records the hash of the catalog, parameters and
          consumption that produced it, so a stored recommendation can be reproduced
          against the inputs as they were rather than as they are now.</p>
        </section>`;

      paint();
      wireRefresh(root, () => runs.render(root, ctx));
    });
  },
};

/* ---------------------------------------------------------------- integrity */

export const integrity = {
  title: 'Integrity',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({
        title: 'Reading integrity',
        description: 'Whether the stored measurements are the ones that were signed on arrival.',
      })}
      ${skeletonRows(4)}`;

    await guard(root, async () => {
      const buildings = await api.get('/buildings');
      const options = buildingOptions(buildings);
      const state = { building: options[0]?.value };

      root.innerHTML = `
        ${pageHead({
          title: 'Reading integrity',
          description: 'Whether the stored measurements are the ones that were signed on arrival.',
        })}

        <section class="panel">
          <header>
            <h3>Chain verification</h3>
            <div class="toolbar">
              ${picker({ id: 'int-building', label: 'Building', options, value: state.building })}
              <button type="button" class="ghost small" data-verify>Verify again</button>
            </div>
          </header>
          <div id="int-body">${skeletonRows(4)}</div>
          <p class="caption">Each reading is signed and chained to the one before it, so
          a modified row breaks the walk at exactly that row. A deleted tail breaks
          nothing — there is nothing after it left to check — which is why chain heads
          are also written to a file outside the database volume and compared here.</p>
        </section>`;

      const input = root.querySelector('#int-building');
      wirePicker(input);
      const draw = async () => {
        const body = root.querySelector('#int-body');
        body.innerHTML = skeletonRows(4);
        const report = await api.verifyChain(state.building);
        const ok = report.chain_ok && report.checkpoint_ok !== false;
        body.innerHTML = `
          <div class="verdict ${ok ? 'good' : 'bad'}">${ok ? 'Intact' : 'Broken'}</div>
          <dl class="facts">
            <dt>Rows checked</dt><dd>${compact(report.rows)}</dd>
            <dt>Chain walk</dt><dd>${report.chain_ok ? 'verifies' : 'broken'}</dd>
            <dt>External anchor</dt><dd>${
              report.checkpoint_ok === null ? 'not anchored yet'
              : report.checkpoint_ok ? `matches (sequence ${compact(report.checkpoint_seq)})`
              : 'does NOT match — the tail has been deleted'}</dd>
            <dt>Anchor vs database copy</dt><dd>${
              report.anchor_matches_database === null ? '—'
              : report.anchor_matches_database ? 'agree' : 'DISAGREE'}</dd>
          </dl>
          ${report.break ? `<div class="error-box" role="alert">First break:
            ${escapeHtml(report.break.reason)} at sequence ${report.break.seq}</div>` : ''}
          ${report.hint ? `<div class="hint-box">${escapeHtml(report.hint)}</div>` : ''}
          ${ok ? '' : `<div class="hint-box">A break is evidence, not an error to dismiss.
            The sequence number above is where the stored chain stops agreeing with itself;
            compare it against the anchor file outside the database volume before anything
            is rewritten.</div>`}`;
      };

      input.addEventListener('change', () => {
        const value = pickerValue(input, options);
        if (!value || value === state.building) return;
        state.building = value;
        guard(root, draw);
      });
      root.querySelector('[data-verify]').addEventListener('click', () => guard(root, draw));

      await draw();
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

export const admin = {
  title: 'Administration',
  requiredRole: 'admin',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({
        title: 'Administration',
        description: 'Accounts, what the deployment is currently exposed to, and who did what.',
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
          render: (r) => (r.outcome === 'denied'
            ? '<span class="sev critical">denied</span>'
            : `<span class="muted">${escapeHtml(r.outcome)}</span>`) },
        { key: 'ip', label: 'Address', cls: 'mono', render: (r) => escapeHtml(r.ip || '—') },
      ];
      const auditState = { sortKey: 'ts', sortDir: 'desc' };

      root.innerHTML = `
        ${pageHead({
          title: 'Administration',
          description: 'Accounts, what the deployment is currently exposed to, and who did what.',
          actions: '<button id="user-add" type="button" class="primary">Add account</button>',
        })}

        ${posture.warnings.length ? `<div class="warn-box" role="alert">
          ${posture.warnings.map((w) => escapeHtml(w)).join('<br>')}</div>` : ''}

        <section class="panel">
          <header><h3>Security posture</h3>
            <span class="muted small">what this deployment is exposed to right now</span></header>
          <dl class="facts">
            <dt>Session cookie Secure flag</dt>
            <dd>${posture.cookie_secure
              ? '<span class="sev medium">on</span>'
              : '<span class="sev high">OFF — required on any networked deployment</span>'}</dd>
            <dt>Idle timeout</dt><dd>${posture.session_idle_timeout_hours} hours</dd>
            <dt>Absolute session lifetime</dt><dd>${posture.session_absolute_lifetime_days} days</dd>
            <dt>Minimum password length</dt><dd>${posture.password_min_length}</dd>
            <dt>Accounts</dt><dd>${posture.users}</dd>
          </dl>
          ${posture.recent_failed_logins.length ? `<h4>Recent failed logins</h4>
            <div class="table-wrap"><table>
              <thead><tr><th>When</th><th>Username tried</th><th>Address</th></tr></thead>
              <tbody>${posture.recent_failed_logins.map((f) => `<tr>
                <td class="mono">${fmtDateTime(f.ts)}</td><td>${escapeHtml(f.username)}</td>
                <td class="mono">${escapeHtml(f.ip)}</td></tr>`).join('')}</tbody>
            </table></div>`
            : '<p class="muted small">No failed logins recorded.</p>'}
        </section>

        <section class="panel">
          <header><h3>Accounts</h3><span class="muted small">${users.length} total</span></header>
          <div class="table-wrap"><table>
            <thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Status</th>
              <th>Last login</th><th><span class="sr-only">Actions</span></th></tr></thead>
            <tbody>${users.map((u) => `<tr>
              <td>${escapeHtml(u.username)}</td>
              <td>${escapeHtml(u.display_name)}</td>
              <td>
                ${selectWrap(`<select class="inline" data-role="${escapeHtml(u.id)}"
                        data-current="${escapeHtml(u.role)}"
                        data-username="${escapeHtml(u.username)}"
                        aria-label="Role for ${escapeHtml(u.username)}">
                  ${['viewer', 'analyst', 'admin'].map((r) =>
                    `<option value="${r}"${r === u.role ? ' selected' : ''}>${r}</option>`).join('')}
                </select>`)}
              </td>
              <td>${u.is_active
                ? '<span class="dot-label"><span class="dot ok"></span>active</span>'
                : '<span class="dot-label"><span class="dot bad"></span>disabled</span>'}
                ${u.must_change_password
                  ? '<span class="badge">must change password</span>' : ''}</td>
              <td class="mono">${fmtDateTime(u.last_login_at)}</td>
              <td class="row-actions">
                <button class="ghost small" type="button" data-toggle="${escapeHtml(u.id)}"
                  data-active="${u.is_active}" data-username="${escapeHtml(u.username)}"
                  aria-label="${u.is_active ? 'Disable' : 'Enable'} ${escapeHtml(u.username)}"
                  >${u.is_active ? 'Disable' : 'Enable'}</button>
                <button class="ghost small" type="button" data-reset="${escapeHtml(u.id)}"
                  data-username="${escapeHtml(u.username)}"
                  aria-label="Reset the password for ${escapeHtml(u.username)}">Reset password</button>
              </td>
            </tr>`).join('')}</tbody>
          </table></div>
          <p class="caption">An admin cannot remove their own admin role or disable
          their own account: leaving a deployment with no administrator is recoverable
          only with shell access to the database.</p>
        </section>

        <section class="panel">
          <header><h3>Audit log</h3><span class="muted small">most recent 50 events</span></header>
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
        body.innerHTML = auditRows.length
          ? dataTable({ columns: auditColumns, rows: auditRows,
                        sortKey: auditState.sortKey, sortDir: auditState.sortDir })
          : emptyState({ title: 'Nothing recorded yet',
                         body: 'Every authenticated action appears here as it happens.' });
        wireSort(body, auditState, paintAudit);
      };
      paintAudit();

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
    });
  },
};

/* ------------------------------------------------------------------ account */

export const account = {
  title: 'Account',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({
        title: 'Your account',
        description: 'Change your password, and see everywhere this account is signed in.',
      })}
      ${skeletonRows(4)}`;

    await guard(root, async () => {
      const sessions = await api.mySessions();

      root.innerHTML = `
        ${pageHead({
          title: 'Your account',
          description: 'Change your password, and see everywhere this account is signed in.',
        })}

        <div class="two-col">
          <section class="panel">
            <header><h3>Change password</h3></header>
            <form id="pw-form" class="form">
              <label for="pw-current">Current password
                <input type="password" id="pw-current" autocomplete="current-password" required></label>
              <label for="pw-new">New password
                <input type="password" id="pw-new" autocomplete="new-password"
                       minlength="${MIN_PASSWORD}" required>
                <span class="hint">At least ${MIN_PASSWORD} characters.</span></label>
              <label for="pw-confirm">Repeat the new password
                <input type="password" id="pw-confirm" autocomplete="new-password" required></label>
              <div id="pw-result"></div>
              <div class="form-actions">
                <button type="submit" class="primary">Change password</button>
              </div>
              <p class="caption">This signs out every other session for this account — a
              password change is what you do when you think a credential has been taken.</p>
            </form>
          </section>

          <section class="panel">
            <header><h3>Where you are signed in</h3>
              <span class="muted small">${sessions.length} active</span></header>
            ${sessions.length ? `<div class="table-wrap"><table>
              <thead><tr><th>Started</th><th>Last seen</th><th>Address</th><th>Browser</th></tr></thead>
              <tbody>${sessions.map((s) => `<tr>
                <td class="mono">${fmtDateTime(s.created_at)}</td>
                <td class="mono">${fmtDateTime(s.last_seen_at)}</td>
                <td class="mono">${escapeHtml(s.ip || '—')}</td>
                <td class="muted small">${escapeHtml((s.user_agent || '').slice(0, 60))}</td>
              </tr>`).join('')}</tbody>
            </table></div>` : emptyState({
              title: 'No other sessions',
              body: 'This is the only place this account is currently signed in.' })}
            <p class="caption">A session you do not recognise is a reason to change the
            password: doing so ends every other one.</p>
          </section>
        </div>`;

      root.querySelector('#pw-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const result = root.querySelector('#pw-result');
        /* Checked here as well as by the server, because a mismatch that only
         * surfaces as a rejected request has already sent the password. */
        const problem = passwordProblem({
          password: root.querySelector('#pw-new').value,
          confirm: root.querySelector('#pw-confirm').value,
        });
        if (problem) {
          result.innerHTML = `<div class="error-box" role="alert">${escapeHtml(problem)}</div>`;
          root.querySelector('#pw-new').focus();
          return;
        }
        try {
          await api.changePassword(
            root.querySelector('#pw-current').value,
            root.querySelector('#pw-new').value);
          result.innerHTML = '';
          setStatus(root, { kind: 'ok', message: 'Password changed. Other sessions are signed out.' });
          root.querySelector('#pw-form').reset();
          ctx.onPasswordChanged?.();
        } catch (error) {
          result.innerHTML = errorBox(error);
        }
      });
    });
  },
};
