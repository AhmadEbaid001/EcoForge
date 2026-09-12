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
import { wordmark } from './brand.js';
import { locale, t } from './i18n.js';
import {
  chainFigure, compact, escapeHtml, icon, lineChart, mark, proportionBar, sevChip,
  stackedBars, statTile,
} from './charts.js';
import {
  clearStatus, confirmAction, dataTable, emptyState, markRead, onReread,
  openDialog, pageHead, picker, pickerValue, runJob, selectWrap, setStatus,
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
/* The label is read at render time, not fixed at module load, so the control
   follows the language like everything else. In Arabic the count and its noun
   agree differently at 7 and at 30, so each one is its own string rather than a
   number glued to a word. */
const RANGES = [7, 14, 30, 90];

function rangeControl(selected) {
  const buttons = RANGES.map((days) =>
    `<button type="button" class="seg-btn" data-days="${days}"
       aria-pressed="${days === selected}">${escapeHtml(t(`range.d${days}`))}</button>`).join('');
  /* No visible "Window" label. Four day ranges sitting beside the data clock in
   * the page header are read as a window without being told, and the word cost
   * 71px of a header that has to hold the clock, the language, the text size and
   * the appearance controls on one line down to 1280. The group keeps its
   * accessible name - dropping the caption must not drop the label. */
  return `<div class="chart-filters">
    <div class="segmented" role="group" aria-label="${t('range.window')}">${buttons}</div>
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
/* Looked up through a function, not a frozen object: `t()` has to run at render
   time or the labels keep whatever language the module was first evaluated in.
   Both tables also gained tou_carbon, which the objective list has offered for a
   while and these never named. */
const SOLVER_LABEL = () => ({
  cpsat: t('slv.cpsat'),
  greedy_upgrade: t('slv.greedy_upgrade'),
  greedy: t('slv.greedy'),
  equal_split: t('slv.equal_split'),
});

const OBJECTIVE_LABEL = () => ({
  lca_carbon: t('obj.lca_carbon'),
  tou_carbon: t('obj.tou_carbon'),
  raw_kwh: t('obj.raw_kwh'),
  egp_saved: t('obj.egp_saved'),
});

const solverLabel = (id) => SOLVER_LABEL()[id] || String(id || '—');
const objectiveLabel = (id) => OBJECTIVE_LABEL()[id] || String(id || '—');

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
                     t('overview.buildingsNote'))}
          ${statTile(t('overview.readings'),
                     summary.readings_exact === false
                       ? t('overview.aboutN', { n: compact(summary.readings) })
                       : compact(summary.readings),
                     t('overview.readingsNote'))}
          ${statTile(t('overview.openAlerts'), compact(summary.open_anomalies),
                     t('overview.alertsNote'),
                     { spark: dailyTotals(dailyData.days),
                       trend: halfOverHalf(dailyTotals(dailyData.days)),
                       chips: SEVERITY_ORDER
                         .filter((k) => (summary.open_by_severity || {})[k])
                         .map((k) => `<span class="stat-chip ${k}">${
                           sevChip(k)} ${compact(summary.open_by_severity[k])}</span>`)
                         .join('') })}
          ${statTile(t('overview.measured'),
                     t('overview.ofTotal', { n: fromForecast, total: summary.buildings }),
                     t('overview.measuredNote'))}
        </div>

        <div class="two-col">
          <section class="panel">
            <header>
              <h3>${t('overview.demandPanel')}</h3>
              <span class="scope">${t('overview.demandScope', {
                days: state.days, n: summary.buildings })}</span>
            </header>
            ${lineChart([{ label: t('overview.demandSeries'), points: loadData.points }], { unit: 'kW' })}
            <div class="panel-foot">
              <p class="caption">${t('overview.demandCaption')}</p>
              <div class="panel-actions">
                <a class="btn secondary" href="#/forecasts">${t('overview.openForecasting')}</a>
              </div>
            </div>
          </section>

          <section class="panel">
            <header>
              <h3>${t('overview.alertsPanel')}</h3>
              <span class="scope">${t('overview.alertsScope', { days: state.days })}</span>
            </header>
            ${stackedBars(dailyData.days, ['critical', 'high', 'medium'], { height: 240 })}
            <div class="panel-foot">
              <p class="caption">${t('overview.alertsCaption')}</p>
              <div class="panel-actions">
                <a class="btn secondary" href="#/alerts">${t('overview.openInbox')}</a>
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
              ${statTile(t('overview.budget'), `${compact(run.budget_egp)} ${t('unit.egp')}`)}
              ${statTile(t('overview.funded'),
                  t('overview.buildingsUnit', { n: run.buildings_funded }))}
              ${statTile(t('overview.spent'), `${compact(run.total_cost_egp)} ${t('unit.egp')}`)}
              ${statTile(t('overview.shareSpent'), run.budget_egp
                  ? `${((run.total_cost_egp / run.budget_egp) * 100).toFixed(0)}%` : '—')}
              ${statTile(t('overview.lifetimeBenefit'),
                  `${compact(run.total_benefit_kgco2e)} ${t('unit.kgco2e')}`)}
              ${statTile(t('overview.districtCap'), run.max_funded_per_district
                  ? t('rn.perDistrict', { n: run.max_funded_per_district }) : t('rn.none'))}
            </div>
            <div class="panel-foot">
              <p class="caption">${t('overview.solvedBy', {
                solver: escapeHtml(solverLabel(run.solver)),
                objective: escapeHtml(objectiveLabel(run.objective)),
                when: fmtDateTime(run.created_at),
              })}</p>
              <div class="panel-actions">
                <a class="btn primary" href="#/map">${t('overview.openOnMap')}</a>
              </div>
            </div>`
            : emptyState({
                title: t('ov.emptyRunTitle'),
                body: t('ov.emptyRunBody'),
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

/* Wires a button that starts a maintenance job and reports it to the end.
 *
 * These run for minutes. The button disables itself, the strip narrates, and the
 * screen reloads itself when the job finishes - because the whole point of both
 * jobs is that the numbers on the screen change.
 */
function wireJobButton(root, ctx, { selector, kind, label, confirm, onDone }) {
  /* The action slot is in the shell header, which is a sibling of the view root
   * rather than a child of it, so this cannot be a root-scoped query. One screen
   * is mounted at a time, so the id is unambiguous. */
  const button = root.querySelector(selector) || document.querySelector(selector);
  if (!button) return;
  const original = button.textContent;

  button.addEventListener('click', async () => {
    if (confirm) {
      const ok = await confirmAction(confirm);
      if (!ok) return;
    }
    button.disabled = true;
    button.textContent = t('jb.running', { label });
    setStatus(root, { kind: 'hint', message: t('jb.started', { label }) });

    try {
      const state = await runJob(kind, {
        signal: ctx?.signal,
        onState: (s) => {
          if (s.status === 'busy') {
            setStatus(root, { kind: 'warn', message: s.error || t('jb.busy') });
          }
        },
      });

      if (state.status === 'done') {
        setStatus(root, { kind: 'ok',
          message: t('jb.finished', { label,
            summary: (state.summary || '').split('\n').pop() || '' }).trim() });
        if (onDone) await onDone();
      } else if (state.status === 'failed') {
        setStatus(root, { kind: 'error',
          message: t('jb.failed', { label, error: state.error || t('jb.seeLog') }) });
      } else if (state.status === 'busy') {
        /* Already reported by onState; nothing to add. */
      }
    } catch (error) {
      setStatus(root, { kind: 'error',
        message: error?.detail || String(error?.message || error) });
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });
}

/* ----------------------------------------------------------------- forecasts */

/* How far apart two data-time stamps are, in the largest unit that still reads as
 * a quantity. Forecast staleness is the number this screen exists to expose and
 * "13,058 hours" is not a number anyone converts in their head. */
function ageBetween(fromIso, toIso) {
  if (!fromIso || !toIso) return null;
  const hours = (Date.parse(toIso) - Date.parse(fromIso)) / 3.6e6;
  if (!Number.isFinite(hours)) return null;
  if (hours < 48) return t('age.hours', { n: Math.max(0, Math.round(hours)) });
  const days = hours / 24;
  if (days < 60) return t('age.days', { n: Math.round(days) });
  return t('age.months', { n: (days / 30.44).toFixed(1) });
}

export const forecasts = {
  title: 'Forecasts',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({ root, title: t('fx.title') })}
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

      const isAdmin = can(ctx.user, 'admin');

      root.innerHTML = `
        ${pageHead({ root, title: t('fx.title'),
          actions: isAdmin
            ? `<button type="button" class="secondary" id="refit-btn">${t('fx.refit')}</button>`
            : '' })}

        <section class="panel">
          <header>
            <h3>${t('fx.coverage')}</h3>
            <span class="scope">${t('fx.scope', { n: buildings.length })}</span>
          </header>

          <div class="stat-row compact">
            ${statTile(t('fx.costedFrom'), unanimous ? unanimous.model_version
                       : t('fx.modelCount', { n: models.length }),
                       unanimous ? t('fx.allBuildings', { n: unanimous.buildings })
                                 : t('fx.split'))}
            ${statTile(t('fx.reaching'),
                       t('fx.ofTotal', { covered, total: buildings.length }),
                       coverState === 'full' ? t('fx.coverFull')
                       : coverState === 'none' ? t('fx.coverNone')
                       : t('fx.coverPartial'))}
            ${statTile(t('fx.age'), staleBy || '—', t('fx.ageNote'))}
          </div>

          ${coverState === 'full' ? '' : `
          <div class="note-panel ${coverState === 'none' ? 'bad' : 'warn'}" role="note">
            ${icon('warning')}
            <p>${t('fx.staleBody', {
              age: escapeHtml(staleBy || t('fx.someTime')),
              detail: coverState === 'none' ? t('fx.staleNone')
                : t('fx.stalePartial', { covered, total: buildings.length }),
            })}</p>
          </div>`}

          ${unanimous ? '' : proportionBar(models.map((m) => ({
            label: m.model_version, value: m.buildings,
          })))}

          <p class="caption">${unanimous
            ? t('fx.unanimous', { model: escapeHtml(unanimous.model_version) })
            : t('fx.mixed')}</p>
        </section>

        <section class="panel">
          <header>
            <h3>${t('fx.chartPanel')}</h3>
            <span class="scope" id="fc-scope">${t('fx.noBuilding')}</span>
            <div class="toolbar">
              ${picker({ id: 'fc-building', label: t('fx.building'), options, value: state.building })}
            </div>
          </header>
          <div id="fc-chart">${emptyState({
            title: t('fx.pickTitle'),
            body: t('fx.pickBody'),
          })}</div>
          <div class="stroke-legend" id="fc-legend" hidden>
            <span class="key"><svg viewBox="0 0 34 8" width="34" height="8" aria-hidden="true"><path d="M0 4h34" stroke="var(--ink)" stroke-width="1.8" fill="none"/></svg>${t('fx.legendActual')}</span>
            <span class="key" data-key="forecast"><svg viewBox="0 0 34 8" width="34" height="8" aria-hidden="true"><path d="M0 4h34" stroke="var(--accent)" stroke-width="1.8" stroke-dasharray="5 3" fill="none"/></svg>${t('fx.legendForecast')}</span>
          </div>
        </section>`;

      /* The refit is what makes this screen's own numbers true, and it was
       * shell-only: the coverage panel could report that the forecasts were five
       * months stale and offer no way to do anything about it. */
      wireJobButton(root, ctx, {
        selector: '#refit-btn',
        kind: 'forecast-refit',
        label: t('fx.refitShort'),
        confirm: {
          title: t('fx.refitTitle'),
          description: t('fx.refitBody'),
          confirmLabel: t('fx.refit'),
        },
        onDone: () => forecasts.render(root, ctx),
      });

      const input = root.querySelector('#fc-building');
      wirePicker(input, options.filter((o) => o.label));
      const draw = async () => {
        const target = root.querySelector('#fc-chart');
        target.innerHTML = skeletonChart();
        const data = await api.forecast(state.building, 336);
        const b = byId.get(state.building);
        const hasForecast = (data.forecast || []).length > 0;

        target.innerHTML = lineChart([
          { label: t('fx.seriesActual'), points: data.actual },
          { label: t('fx.seriesForecast'), points: data.forecast },
        ], { unit: 'kW' }) + (hasForecast ? '' : `
          <p class="note warn">${t('fx.noneStored')}</p>`);

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
              data.model_version || t('fx.noModel')} · ${
              t('fx.costedAt', { kwh: compact(b.annual_kwh) })}`
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
      /* Opened from the rail this is empty; opened from another screen that had
       * a building in hand - integrity, say - the inbox arrives already narrowed
       * to it, which is the whole reason that link exists. */
      severity: '', building: ctx.params?.get('building') || '', onlyOpen: true,
      rows: [], selected: new Set(), total: null, bySeverity: {},
      sortKey: 'robust_z', sortDir: 'desc',
    };
    const analyst = can(ctx.user, 'analyst');

    root.innerHTML = `
      ${pageHead({ root, title: t('al.title') })}
      ${skeletonRows(8)}`;

    await guard(root, async () => {
      const [buildings, firstSummary] = await Promise.all([
        api.get('/buildings'), api.summary(),
      ]);
      const options = [{ value: '', label: t('al.allBuildings') }, ...buildingOptions(buildings)];
      const summaryTotal = firstSummary.open_anomalies;
      /* An id that names no building is a filter that would silently return
       * nothing, so it is dropped rather than honoured. */
      if (state.building && !buildings.some((b) => b.id === state.building)) state.building = '';
      const arrivedFiltered = Boolean(state.building);

      /* One sentence, stated rather than implied. The list is a capped slice of
       * about thirty thousand rows; acknowledging one used to refill it from the
       * pool with nothing on screen saying where the others were. */
      const countLine = (shown) => {
        if (!state.onlyOpen) return t('al.countAllClosed', { shown });
        /* Fewer rows than the cap means the filter returned everything it had, so
         * these ARE all of them. Only a full page is a slice of something larger.
         * Without this the screen told a reader looking at the two alerts on one
         * building that they were "the 2 largest of 114.8k", and invited them to
         * narrow a filter that was already as narrow as it goes. */
        if (shown < ROW_CAP) {
          return state.building || state.severity
            ? t('al.countAllFiltered', { shown })
            : t('al.countAll', { shown });
        }
        const total = state.severity && !state.building
          ? state.bySeverity[state.severity]
          : (state.building ? null : state.total);
        if (total === null || total === undefined) {
          return t('al.countFirst', { shown });
        }
        return t('al.countSlice', { shown, total: compact(total) });
      };

      /* The reference's column set, with this system's fields in place of the
       * ones it invented. It had an "Anomaly Type" column (Spike, Phantom
       * Load) and a "Deviation %"; the detector produces neither. What it does
       * produce is a robust z against the forecaster's expectation, and
       * expected-versus-observed is the same comparison its column made. */
      const columns = [
        { key: 'building_code', label: t('col.building'), sortable: true,
          render: (r) => `<b>${escapeHtml(r.building_code)}</b>` },
        { key: 'ts', label: t('col.detected'), sortable: true,
          render: (r) => `<span class="tabular">${fmtDateTime(r.ts)}</span>` },
        /* Observed goes bold only past a 50% deviation. Bolding it on every row
         * makes the weight mean "this is the observed column" instead of "look
         * at this one". */
        { key: 'expected_kw', label: t('al.colExpected'), num: true, sortable: true,
          render: (r) => {
            const far = r.expected_kw
              ? Math.abs(r.observed_kw - r.expected_kw) / Math.abs(r.expected_kw) > 0.5
              : false;
            return `${compact(r.expected_kw)} / ${far
              ? `<b>${compact(r.observed_kw)}</b>` : compact(r.observed_kw)}`;
          } },
        { key: 'robust_z', label: t('al.colZ'), num: true, sortable: true,
          value: (r) => (r.robust_z === null ? null : Math.abs(r.robust_z)),
          render: (r) => (r.robust_z === null ? '—'
            : `<span class="sev-figure ${escapeHtml(r.severity)}">${
                r.robust_z > 0 ? '+' : ''}${r.robust_z.toFixed(1)}</span>`) },
        { key: 'severity', label: t('al.colSeverity'), sortable: true,
          render: (r) => sevChip(r.severity) },
        { key: 'acknowledged', label: t('al.colStatus'), sortable: true,
          render: (r) => (r.acknowledged
            ? `<span class="chip closed">${t('al.closed')}</span>`
            : `<span class="chip open">${t('al.open')}</span>`) },
      ];
      if (analyst) {
        columns.push({
          key: '_actions', label: '', cls: 'row-actions',
          render: (r) => (r.acknowledged ? '' : `<button class="ghost small" data-ack="${r.id}"
            aria-label="${escapeHtml(t('al.ackAria', {
              severity: r.severity, code: r.building_code }))}">${t('al.acknowledge')}</button>`),
        });
      }

      const paint = () => {
        const body = root.querySelector('#alert-body');
        if (!state.rows.length) {
          body.innerHTML = emptyState({
            title: state.onlyOpen ? t('al.emptyOpenTitle') : t('al.emptyFilterTitle'),
            body: state.severity || state.building
              ? t('al.emptyFiltered')
              : t('al.emptyAll'),
          });
          root.querySelector('#alert-foot').innerHTML = '';
          return;
        }

        body.innerHTML = dataTable({
          columns, rows: state.rows.map((r) => ({ ...r, _id: r.id })),
          sortKey: state.sortKey, sortDir: state.sortDir, selectable: analyst,
          /* Every checkbox used to announce "Select this row" - a hundred times,
           * identically. What it selects is the alert, so it says which one. */
          rowLabel: (r) => t('al.selectAria', {
            severity: r.severity, code: r.building_code, when: fmtDateTime(r.ts) }),
        });
        root.querySelector('#alert-foot').innerHTML =
          `<span>${escapeHtml(countLine(state.rows.length))}</span>
           <span class="muted">${t('al.footNote')}</span>`;

        wireSort(body, state, paint);

        body.querySelectorAll('[data-ack]').forEach((button) => {
          button.addEventListener('click', async () => {
            button.disabled = true;
            const id = Number(button.dataset.ack);
            await guard(root, async () => {
              await api.acknowledgeOne(id);
              await load();
              offerUndo([id], t('al.closed1'));
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
          return total ? t('al.closeAllN', { n: compact(total) }) : t('al.closeAll');
        }
        return total ? t('al.closeMatchingN', { n: compact(total) }) : t('al.closeMatching');
      };

      /* The bar stays. A control that appears only once a condition is met
       * teaches nobody that the condition exists - the disabled button says what
       * is missing instead, which is the whole point of a disabled state. */
      const paintActionBar = () => {
        const bar = root.querySelector('#alert-actions');
        const n = state.selected.size;
        bar.hidden = false;
        bar.innerHTML = `
          <span>${n ? t('al.nSelected', { n: `<strong>${n}</strong>` }) : t('al.nothingSelected')}</span>
          <span class="bulk-actions">
            <button type="button" class="primary" data-ack-selected${n ? '' : ' disabled'}>${
              n ? t('al.ackN', { n }) : t('al.tickARow')}</button>
            ${n ? `<button type="button" class="secondary" data-clear-selection>${t('al.clearSelection')}</button>` : ''}
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
            offerUndo(ids, ids.length === 1 ? t('al.closed1')
              : t('al.closedN', { n: ids.length }));
          });
        });
      };

      /* Acknowledging takes an `acknowledged: false`, so this is a real undo
       * rather than a message claiming one exists. */
      const offerUndo = (ids, message) => {
        setStatus(root, {
          kind: 'ok',
          message: t('al.byAt', { message, user: ctx.user.username,
            time: new Date().toLocaleTimeString(locale()) }),
          actionLabel: t('al.undo', { n: ids.length }),
          actionAttr: 'data-undo',
        });
        root.querySelector('[data-undo]')?.addEventListener('click', async () => {
          await guard(root, async () => {
            await api.acknowledge({ ids, acknowledged: false });
            await load();
            setStatus(root, { kind: 'hint', message: t('al.reopened', { n: ids.length }) });
          });
        });
      };

      const load = async () => {
        const body = root.querySelector('#alert-body');
        body.innerHTML = skeletonRows(6);
        const params = `?limit=${ROW_CAP}&only_open=${state.onlyOpen}` +
          (state.severity ? `&severity=${encodeURIComponent(state.severity)}` : '') +
          (state.building ? `&building_id=${encodeURIComponent(state.building)}` : '');
        const [rows, summary] = await Promise.all([api.anomalyFeed(params), api.alertSummary()]);
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
        ${pageHead({ root, title: t('al.title') })}

        <div class="panel toolbar-panel">
          <div class="toolbar">
            <div class="group">
              <span class="inline-label" id="sev-label">${t('al.colSeverity')}</span>
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
              ${picker({ id: 'alert-building', label: t('al.filterBuilding'), options,
                         value: state.building })}
            </div>
            <span class="rule"></span>
            <label class="check"><input type="checkbox" id="alert-open" checked> ${t('al.openOnly')}</label>
            <button type="button" class="secondary" id="alert-clear">${t('al.clearFilters')}</button>
          </div>
        </div>

        ${analyst ? '<div id="alert-actions" class="action-bar"></div>' : `
        <div class="note-panel" role="note">
          ${icon('lock')}
          <p>${t('al.readOnly')}</p>
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
        root.querySelector('#alert-building').value = t('al.allBuildings');
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
                state.severity ? t('al.scopeSeverity', { severity: state.severity }) : null,
                state.building ? t('al.scopeBuilding', { building: buildingInput.value }) : null,
              ].filter(Boolean).join(t('al.scopeAnd'))
            : t('al.scopeWhole');
          const count = !filtered ? state.total
            : (state.severity && !state.building ? state.bySeverity[state.severity] : null);

          /* The count has to be TYPED. A second click is protection against a
           * slip of the hand and nothing else; typing the number is the one
           * thing that cannot be done without having read it. */
          const ok = await confirmAction({
            title: filtered ? t('al.closeFilteredTitle') : t('al.closeAllTitle'),
            description: (filtered
                ? t('al.closeFilteredBody', { scope })
                : t('al.closeAllBody'))
              + (count ? t('al.closeCount', { n: count.toLocaleString(locale()) }) : '')
              + t('al.closeTail', { user: ctx.user.username }),
            confirmLabel: count
              ? t('al.closeConfirmN', { n: count.toLocaleString(locale()) })
              : t('al.closeConfirmAll'),
            confirmText: count ? String(count) : '',
            confirmHint: count ? t('al.closeHint') : '',
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
              message: filtered
                ? t('al.closedScoped', { n: compact(result?.changed ?? 0), scope,
                                         user: ctx.user.username })
                : t('al.closedPortfolio', { n: compact(result?.changed ?? 0),
                                            user: ctx.user.username }),
            });
          });
        });
      }

      await load();

      if (arrivedFiltered) {
        const b = buildings.find((x) => x.id === state.building);
        setStatus(root, {
          kind: 'hint',
          message: t('al.showingOnly', { building: b ? b.code : t('al.oneBuilding') }),
        });
      }
    });
  },
};

/* --------------------------------------------------------------------- runs */

/* This screen is provenance, not history-as-a-feature. It exists so a run can be
 * re-found and reproduced from its input hash, which is also why the hash gets a
 * control of its own rather than being quietly truncated. */
export const runs = {
  title: 'Allocations',
  async render(root, ctx) {
    /* Truncated by default: at 64 characters the hash is wider than every other
     * column put together and pushes the figures off a 1024px window. The
     * control says which you are looking at, and the footer says what each
     * choice costs you. */
    const state = { rows: [], sortKey: 'created_at', sortDir: 'desc', hashFull: false };

    root.innerHTML = `
      ${pageHead({ root, title: t('rn.title') })}
      ${skeletonRows(6)}`;

    await guard(root, async () => {
      state.rows = await api.runs();

      const columns = [
        { key: 'created_at', label: t('ad.colWhen'), sortable: true,
          render: (r) => `<span class="mono">${fmtDateTime(r.created_at)}</span>` },
        { key: 'budget_egp', label: t('rn.colBudget'), num: true, sortable: true,
          render: (r) => compact(r.budget_egp) },
        { key: 'objective', label: t('rn.colObjective'), sortable: true,
          render: (r) => escapeHtml(objectiveLabel(r.objective)) },
        /* Exact optimization is check-marked and bold wherever it appears: it is
         * the method that is provably right, and a reader scanning twenty-five
         * rows should not have to read the word to find it. */
        { key: 'solver', label: t('rn.colMethod'), sortable: true,
          render: (r) => (r.solver === 'cpsat'
            ? `<span class="method-exact">${mark('check', { size: 11 })}${t('rn.exact')}</span>`
            : escapeHtml(solverLabel(r.solver))) },
        { key: 'buildings_funded', label: t('rn.colFunded'), num: true, sortable: true,
          render: (r) => r.buildings_funded },
        { key: 'total_cost_egp', label: t('rn.colSpent'), num: true, sortable: true,
          render: (r) => compact(r.total_cost_egp) },
        { key: 'total_benefit_kgco2e', label: t('rn.colLifetime'), num: true, sortable: true,
          render: (r) => compact(r.total_benefit_kgco2e) },
        { key: 'inputs_hash', label: t('rn.colHash'), cls: 'mono',
          render: (r) => (state.hashFull
            ? `<span class="hash-all">${escapeHtml(r.inputs_hash)}</span>`
            : `<span class="hash" title="${escapeHtml(r.inputs_hash)}">${
                escapeHtml(String(r.inputs_hash).slice(0, 10))}&hellip;</span>`) },
        { key: '_provenance', label: '', cls: 'row-actions',
          render: (r) => `<button type="button" class="secondary small"
            data-prov="${escapeHtml(r.run_id)}">${t('rn.provenance')}</button>
            <button type="button" class="secondary small"
            data-boq="${escapeHtml(r.run_id)}">${t('rn.boq')}</button>` },
      ];

      const paint = () => {
        const body = root.querySelector('#runs-body');
        body.innerHTML = state.rows.length
          ? dataTable({ columns, rows: state.rows, sortKey: state.sortKey, sortDir: state.sortDir })
          : emptyState({
              title: t('rn.emptyTitle'),
              body: t('rn.emptyBody'),
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
        ${pageHead({ root, title: t('rn.title') })}
        <section class="panel">
          <header>
            <h3>${t('rn.runs')}</h3>
            <span class="scope">${t('rn.scope', { n: state.rows.length })}</span>
            <div class="segmented" role="group" aria-label="${t('rn.hashGroup')}" id="hash-mode">
              <button type="button" class="seg-btn" data-hash="short" aria-pressed="true">${t('rn.first10')}</button>
              <button type="button" class="seg-btn" data-hash="full" aria-pressed="false">${t('rn.all64')}</button>
            </div>
          </header>
          <div id="runs-body"></div>
          <p class="caption" id="hash-note"></p>
        </section>`;

      const hashNote = () => {
        root.querySelector('#hash-note').innerHTML = state.hashFull
          ? t('rn.noteFull') : t('rn.noteShort');
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
         aria-label="${t('rn.provAria')}">
      <button class="close" type="button" data-close aria-label="${t('ui.close')}">&times;</button>
      <h2>${t('rn.provenance')}</h2>
      <div class="prov-body">
        <dl class="facts">
          <div><dt>${t('rn.storedAt')}</dt><dd>${escapeHtml(fmtDateTime(run.created_at))}</dd></div>
          <div><dt>${t('rn.budget')}</dt><dd>${compact(run.budget_egp)} ${t('unit.egp')}</dd></div>
          <div><dt>${t('rn.objective')}</dt><dd>${escapeHtml(objectiveLabel(run.objective))}</dd></div>
          <div><dt>${t('rn.method')}</dt><dd>${escapeHtml(solverLabel(run.solver))}</dd></div>
          <div><dt>${t('rn.districtCap')}</dt><dd>${run.max_funded_per_district
            ? t('rn.perDistrict', { n: run.max_funded_per_district }) : t('rn.none')}</dd></div>
          <div><dt>${t('rn.funded')}</dt><dd>${run.buildings_funded}</dd></div>
          <div><dt>${t('rn.spent')}</dt><dd>${compact(run.total_cost_egp)} ${t('unit.egp')}</dd></div>
          <div><dt>${t('rn.benefit')}</dt><dd>${compact(run.total_benefit_kgco2e)} ${t('unit.kgco2e')}</dd></div>
          <div><dt>${t('rn.solveTime')}</dt><dd>${Math.round(run.solve_ms)} ${t('unit.ms')}</dd></div>
          <div><dt>${t('rn.status')}</dt><dd>${escapeHtml(run.status)}</dd></div>
        </dl>

        <h3 class="section-label">${t('rn.inputHash')}</h3>
        <p class="hash-block" id="prov-hash">${escapeHtml(run.inputs_hash)}</p>
        <div class="form-actions">
          <button type="button" class="secondary" data-copy>${t('rn.copyHash')}</button>
          <span class="hint" id="prov-copied" role="status"></span>
        </div>

        <p class="caption">${t('rn.hashCaption')}</p>
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
      said.textContent = t('rn.copied');
    } catch {
      /* Clipboard access can be refused, and a button that silently does
       * nothing is worse than one that says so. The hash is selectable above. */
      said.textContent = t('rn.copyRefused');
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
  title: t('ev.title'),
  async render(root, ctx) {
    const state = { rows: [], sortKey: 'id', sortDir: 'asc', failingOnly: false };

    root.innerHTML = `
      ${pageHead({ root, title: t('ev.title') })}
      ${skeletonRows(8)}`;

    await guard(root, async () => {
      const body0 = await api.evidence();
      state.rows = body0.claims || [];

      /* A known-open row is a FAIL somebody wrote down. It is counted apart from
       * an unacknowledged failure because the two mean very different things: one
       * is a debt on the record, the other is a sentence that quietly stopped
       * being true. */
      const tally = state.rows.reduce((acc, r) => {
        if (r.verdict === 'PASS') acc.pass += 1;
        else if (r.verdict === 'SKIP') acc.skip += 1;
        else if (r.known_open) acc.known += 1;
        else acc.fail += 1;
        return acc;
      }, { pass: 0, skip: 0, known: 0, fail: 0 });

      const measurable = state.rows.length - tally.skip;
      const clean = tally.fail === 0;

      /* Staleness is the one thing that can make every green row on this screen
       * meaningless, so it is reported at the top rather than left to be worked
       * out from a filename. */
      const age = body0.measured_at
        ? (Date.now() - Date.parse(body0.measured_at)) / 3.6e6 : null;
      const ageLabel = age === null ? null
        : age < 1 ? t('ev.ageHour')
        : age < 48 ? t('ev.ageHours', { n: Math.round(age) })
        : t('ev.ageDays', { n: Math.round(age / 24) });
      const stale = age !== null && age > 48;

      const verdictChip = (r) => {
        if (r.verdict === 'PASS') return `<span class="sev pass">${mark('check', { size: 11 })}${t('ev.pass')}</span>`;
        if (r.verdict === 'SKIP') return `<span class="muted">${mark('dash', { size: 11 })}${t('ev.notMeasured')}</span>`;
        return r.known_open
          ? `<span class="sev high">${mark('dash', { size: 11 })}${t('ev.knownOpen')}</span>`
          : `<span class="sev critical">${mark('cross', { size: 11 })}${t('ev.fail')}</span>`;
      };

      /* The statement leads. `F8-b` is the harness's handle on a claim and means
       * nothing to somebody reading this for the first time, so it goes last and
       * quiet - the sentence is what a reader is here to check. */
      const columns = [
        { key: 'statement', label: t('ev.colStatement'), sortable: true, cls: 'wrap',
          render: (r) => escapeHtml(r.statement) },
        { key: '_verdict', label: t('ev.colVerdict'), sortable: true, render: verdictChip },
        { key: 'measured', label: t('ev.colMeasured'), cls: 'wrap',
          render: (r) => (r.measured ? escapeHtml(r.measured) : '<span class="muted">&mdash;</span>') },
        { key: 'id', label: t('ev.colRef'), sortable: true, cls: 'mono muted',
          render: (r) => escapeHtml(r.id) },
      ];

      const isAdmin = can(ctx.user, 'admin');

      root.innerHTML = `
        ${pageHead({ root, title: t('ev.title'),
          actions: isAdmin
            ? `<button type="button" class="secondary" id="claims-btn">${t('ev.remeasure')}</button>`
            : '' })}

        <section class="panel">
          <header>
            <h3>${t('ev.harness')}</h3>
            <span class="scope">${ageLabel
              ? t('ev.lastMeasured', { age: escapeHtml(ageLabel) })
              : t('ev.neverMeasured')}</span>
          </header>

          <div class="posture-verdict ${clean ? 'good' : 'bad'}">
            <span class="verdict-shield">${icon(clean ? 'integrity' : 'warning')}</span>
            <span class="verdict-text">
              <span class="verdict-word">${state.rows.length
                ? t('ev.hold', { pass: tally.pass, total: measurable })
                : t('ev.nothingYet')}</span>
              <span class="verdict-gloss">${state.rows.length
                ? t('ev.glossLead')
                  + (tally.fail ? t('ev.glossFail', { n: tally.fail })
                    : tally.known ? t('ev.glossKnown', { n: tally.known })
                    : t('ev.glossClean'))
                : t('ev.glossNever')}</span>
            </span>
            <span class="tally">
              <span class="sev pass">${mark('check', { size: 11 })}${tally.pass}</span>
              ${tally.known ? `<span class="sev high">${mark('dash', { size: 11 })}${tally.known}</span>` : ''}
              ${tally.fail ? `<span class="sev critical">${mark('cross', { size: 11 })}${tally.fail}</span>` : ''}
              ${tally.skip ? `<span class="muted small">${t('ev.notMeasuredN', { n: tally.skip })}</span>` : ''}
            </span>
          </div>

          ${stale ? `
          <div class="note-panel warn" role="note">
            ${icon('warning')}
            <p>${t('ev.staleNote', { age: escapeHtml(ageLabel) })}</p>
          </div>` : ''}

          <div class="toolbar evidence-tools">
            <button type="button" class="seg-btn deny-toggle" id="evidence-failing"
                    aria-pressed="false">${t('ev.unresolvedOnly')}</button>
            <button type="button" class="secondary small" id="evidence-export">${t('ev.exportCsv')}</button>
          </div>

          <div id="evidence-body"></div>
          ${body0.note ? `<p class="caption">${escapeHtml(body0.note)}</p>` : ''}
        </section>`;

      const paint = () => {
        const host = root.querySelector('#evidence-body');
        const rows = state.failingOnly
          ? state.rows.filter((r) => r.verdict !== 'PASS' && r.verdict !== 'SKIP')
          : state.rows;
        host.innerHTML = rows.length
          ? dataTable({ columns,
              rows: rows.map((r) => ({ ...r,
                _cls: (r.verdict !== 'PASS' && r.verdict !== 'SKIP' && !r.known_open)
                  ? 'denied-row' : '' })),
              sortKey: state.sortKey, sortDir: state.sortDir })
          : emptyState({
              title: state.failingOnly ? t('ev.emptyUnresolvedTitle') : t('ev.emptyNoneTitle'),
              body: state.failingOnly ? t('ev.emptyHolding') : t('ev.emptyNever'),
            });
        wireSort(host, state, paint);
      };
      paint();

      /* The verdicts are only worth what their date is worth, so the screen that
       * reports them is the screen that can re-run them. */
      wireJobButton(root, ctx, {
        selector: '#claims-btn',
        kind: 'evidence',
        label: t('ev.jobLabel'),
        confirm: {
          title: t('ev.jobTitle'),
          description: t('ev.jobBody'),
          confirmLabel: t('ev.jobConfirm'),
        },
        onDone: () => evidence.render(root, ctx),
      });

      root.querySelector('#evidence-failing').addEventListener('click', (event) => {
        state.failingOnly = !state.failingOnly;
        event.currentTarget.setAttribute('aria-pressed', String(state.failingOnly));
        paint();
      });

      root.querySelector('#evidence-export').addEventListener('click', () => {
        if (!state.rows.length) return;
        downloadCsv(`gemp-claims-${new Date().toISOString().slice(0, 10)}.csv`,
          state.rows.map((r) => ({
            id: r.id, verdict: r.verdict, known_open: r.known_open ? 'yes' : '',
            statement: r.statement, measured: r.measured, detail: r.detail || '',
          })));
      });

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
    { key: 'building_code', label: t('col.building'), sortable: true,
      render: (r) => `${escapeHtml(r.building_code)} <span class="muted">${
        escapeHtml(r.building_name)}</span>` },
    { key: 'measure', label: t('boq.colMeasure'), sortable: true, render: (r) => escapeHtml(r.measure) },
    { key: 'quantity', label: t('boq.colQuantity'), num: true, sortable: true,
      render: (r) => `${compact(r.quantity)} ${escapeHtml(r.basis)}` },
    { key: 'unit_rate', label: t('boq.colRate'), render: (r) => escapeHtml(r.unit_rate) },
    { key: 'line_total_egp', label: t('boq.colTotal'), num: true, sortable: true,
      render: (r) => compact(r.line_total_egp) },
    { key: '_cite', label: '',
      render: (r) => (r.citation
        ? `<span class="sev high" title="${escapeHtml(r.citation)}">${t('boq.uncited')}</span>`
        : '') },
  ];
  const state = { rows: boq.lines, sortKey: 'building_code', sortDir: 'asc' };

  host.innerHTML = `
    <div class="modal-inner wide" role="dialog" aria-modal="true" tabindex="-1"
         aria-label="${t('boq.title')}">
      <button class="close" type="button" data-close aria-label="${t('ui.close')}">&times;</button>
      <h2>${t('boq.title')}</h2>

      <!-- The printed sheet's masthead. Hidden on screen, because on screen the
           dialog already has a heading and the app already has a wordmark; on
           paper neither is there, and a costed document that does not say who
           issued it, for which run, against which inputs, is not a document
           anybody can file. -->
      <div class="print-sheet-head" aria-hidden="true">
        <div class="print-brand">${wordmark('GEMP')}</div>
        <div class="print-title">
          <h1>${t('boq.title')}</h1>
          <p>${t('boq.subtitle')}</p>
        </div>
        <dl class="print-meta">
          <div><dt>${t('boq.run')}</dt><dd class="mono">${escapeHtml(String(runId))}</dd></div>
          <div><dt>${t('boq.issued')}</dt><dd>${escapeHtml(new Date().toLocaleString(locale()))}</dd></div>
          <div><dt>${t('boq.lines')}</dt><dd>${boq.lines.length}</dd></div>
          <div><dt>${t('boq.total')}</dt><dd><strong>${compact(boq.total_egp)} ${t('unit.egp')}</strong></dd></div>
          <div class="wide"><dt>${t('boq.inputHash')}</dt>
            <dd class="mono">${escapeHtml(String(boq.inputs_hash))}</dd></div>
        </dl>
      </div>

      <p class="caption">${t('boq.caption', {
        run: escapeHtml(String(runId).slice(0, 10)),
        lines: boq.lines.length,
        total: compact(boq.total_egp),
        egp: t('unit.egp'),
        hash: escapeHtml(String(boq.inputs_hash).slice(0, 10)),
      })}</p>
      <div id="boq-body"></div>
      <p class="print-foot" aria-hidden="true">${t('boq.printFoot')}</p>
      <div class="form-actions">
        <button type="button" class="primary" data-download>${t('boq.download')}</button>
        <button type="button" class="secondary" data-print>${t('boq.print')}</button>
        <span class="hint">${t('boq.hint')}</span>
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
  host.querySelector('[data-print]').addEventListener('click', () => {
    const previous = document.title;
    document.title = t('rn.printTitle', { run: String(runId).slice(0, 8) });
    /* Restored after the dialog closes, whether the sheet was printed or the
     * print dialog was dismissed. `afterprint` fires for both. */
    const restore = () => {
      document.title = previous;
      window.removeEventListener('afterprint', restore);
    };
    window.addEventListener('afterprint', restore);
    window.print();
  });

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
export const integrity = {
  title: 'Integrity',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({ root, title: t('in.title') })}
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
        ${pageHead({ root, title: t('in.title') })}

        <div class="two-col aside-first">
          <section class="panel">
            <header><h3>${t('in.verifyPanel')}</h3></header>
            <div class="verify-panel">
              ${picker({ id: 'int-building', label: t('col.building'), options, value: '' })}
              <button type="button" class="primary" data-verify disabled>
                ${t('in.pickFirst')}</button>
              <p class="note">${t('in.readsOnly')}</p>

              <!-- Three words this screen cannot avoid using, defined before it
                   uses them. A verdict in vocabulary the reader does not share
                   is not a verdict - but a reader who already has the vocabulary
                   should not have to scroll past it on every visit, so it opens
                   rather than occupying the column. -->
              <details class="glossary-wrap">
                <summary>${t('in.glossarySummary')}</summary>
              <dl class="glossary">
                <div><dt>${t('in.gChainWalk')}</dt><dd>${t('in.gChainWalkBody')}</dd></div>
                <div><dt>${t('in.gAnchor')}</dt><dd>${t('in.gAnchorBody')}</dd></div>
                <div><dt>${t('in.gAgree')}</dt><dd>${t('in.gAgreeBody')}</dd></div>
              </dl>
              </details>
            </div>
          </section>

          <section class="panel">
            <header>
              <h3>${t('in.result')}</h3>
              <span class="scope" id="int-scope">${t('in.nothingVerified')}</span>
            </header>
            <div id="int-body" class="result-panel"></div>
          </section>
        </div>`;

      const input = root.querySelector('#int-building');
      const button = root.querySelector('[data-verify]');
      wirePicker(input, options.filter((o) => o.label));

      const idle = () => {
        root.querySelector('#int-body').innerHTML = emptyState({
          title: t('in.idleTitle'),
          body: t('in.idleBody'),
        });
      };

      const draw = async () => {
        const body = root.querySelector('#int-body');
        const b = byId.get(state.building);
        /* The panel already holds the height it will have when the result
         * arrives, so nothing below it jumps when the walk finishes. */
        body.innerHTML = `
          <div class="running" aria-busy="true" aria-live="polite">
            <p>${t('in.walking', { code: escapeHtml(b ? b.code : '') })}</p>
            <div class="progress"><span class="progress-bar"></span></div>
            <p class="note">${t('in.walkingNote')}</p>
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
            <span class="check-verdict">${pass === null ? t('in.notAnchored')
              : pass ? t('in.pass') : t('in.fail')}</span>
          </div>`;

        body.innerHTML = `
          <div class="verdict ${ok ? 'good' : 'bad'}">
            <span class="verdict-shield">${icon(ok ? 'integrity' : 'warning')}</span>
            <span class="verdict-text">
              <span class="verdict-word">${ok ? t('in.intact') : t('in.broken')}</span>
              <span class="verdict-gloss">${ok ? t('in.intactGloss') : t('in.brokenGloss')}</span>
            </span>
            <span class="verdict-rows">${t('in.rowsChecked', { n: compact(report.rows) })}</span>
          </div>

          <section class="chain-panel" aria-labelledby="chain-fig-h">
            <div class="chain-head">
              <h4 id="chain-fig-h" class="plain">${t('in.chainTitle')}</h4>
              <span class="scope">${t('in.chainScope', { n: compact(report.rows) })}</span>
            </div>
            ${chainFigure({
              rows: report.rows,
              breakSeq: report.break ? report.break.seq : null,
              anchorSeq: report.checkpoint_seq || null,
              ok,
            })}
            <p class="caption">${report.break
              ? t('in.chainBroken', {
                  upto: compact(Math.max(0, report.break.seq - 1)),
                  seq: compact(report.break.seq) })
              : t('in.chainWhole', { n: compact(report.rows) })}</p>
          </section>

          <div class="checks">
            ${check(report.chain_ok, t('in.checkChain'), t('in.checkChainNote'))}
            ${check(report.checkpoint_ok, t('in.checkAnchor'),
                    report.checkpoint_ok === null
                      ? t('in.checkAnchorNone')
                      : t('in.checkAnchorOk', {
                          at: report.checkpoint_seq
                            ? t('in.atSequence', { n: compact(report.checkpoint_seq) }) : '' }))}
            ${check(report.anchor_matches_database, t('in.checkAgree'), t('in.checkAgreeNote'))}
          </div>

          ${report.break ? `
            <div class="break-panel" role="alert">
              <h4 class="plain">${t('in.breakTitle')}</h4>
              <dl class="facts">
                <div><dt>${t('in.breakSeq')}</dt><dd>${report.break.seq}</dd></div>
                <div><dt>${t('in.breakUpto')}</dt><dd>${compact(Math.max(0, report.break.seq - 1))}</dd></div>
                <div><dt>${t('in.breakAfter')}</dt><dd>${compact(Math.max(0, report.rows - report.break.seq))}</dd></div>
                <div><dt>${t('col.building')}</dt><dd>${escapeHtml(b ? b.code : '')}</dd></div>
              </dl>
              <p>${t('in.breakBody1')}</p>
              <p>${t('in.breakBody2')}</p>
            </div>` : ''}

          ${report.hint ? `<p class="note">${escapeHtml(report.hint)}</p>` : ''}

          <div class="form-actions">
            <button type="button" class="secondary" data-verify-again>${t('in.verifyAgain')}</button>
            <a class="btn secondary" href="#/alerts?building=${
              encodeURIComponent(state.building)}">${t('in.seeAlerts', {
                code: escapeHtml(b ? b.code : t('in.thisBuilding')) })}</a>
          </div>

          <p class="caption">${ok ? t('in.captionOk') : t('in.captionBad')}</p>`;

        body.querySelector('[data-verify-again]').addEventListener('click',
          () => guard(root, draw));
      };

      const sync = () => {
        const b = byId.get(state.building);
        button.disabled = !state.building;
        button.textContent = b ? t('in.verifyCode', { code: b.code }) : t('in.pickFirst');
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

/* ------------------------------------------------------------- judge pass */

/* The booth's side of the Judge Pass: whether a card works right now, how many
 * have been used, what became of each one's email, and the one switch that
 * matters on the day. One request, and its absence is not an error - the panel
 * simply is not drawn. */
const judgePassStates = () => ({
  open: `<span class="sev medium">${mark('check', { size: 11 })}${t('ad.jpOpen')}</span>`,
  paused: `<span class="sev high">${mark('dash', { size: 11 })}${t('ad.jpPaused')}</span>`,
  full: `<span class="sev high">${mark('dash', { size: 11 })}${t('ad.jpFull')}</span>`,
  closed: `<span class="sev critical">${mark('cross', { size: 11 })}${t('ad.jpClosed')}</span>`,
  unconfigured: `<span class="sev critical">${mark('cross', { size: 11 })}${
    t('ad.jpUnconfigured')}</span>`,
});

/* Read from a map rather than built from the status word, so every label is a
   literal key the translation check can see. */
const judgePassMail = () => ({
  sent: `<span class="state-cell ok">${mark('check', { size: 11 })}${t('ad.jpSent')}</span>`,
  failed: `<span class="state-cell bad">${mark('cross', { size: 11 })}${t('ad.jpFailed')}</span>`,
  queued: `<span class="state-cell warn">${mark('dash', { size: 11 })}${t('ad.jpQueued')}</span>`,
  off: `<span class="state-cell warn">${mark('dash', { size: 11 })}${t('ad.jpOff')}</span>`,
});

function judgePassPanel(jp) {
  if (!jp) return '';
  const states = judgePassStates();
  const mailCells = judgePassMail();
  const closes = jp.until
    ? new Date(jp.until).toLocaleString(locale(), { dateStyle: 'medium', timeStyle: 'short' })
    : '—';
  /* A closed event cannot be reopened from here - the closing time is the
     environment's to change - so offering the switch would offer nothing. */
  const switchable = jp.state !== 'unconfigured' && jp.state !== 'closed';

  return `
    <section class="panel" id="jp-panel">
      <header>
        <h3>${t('ad.jpTitle')}</h3>
        <span class="scope">${t('ad.jpScope')}</span>
        ${switchable ? `<button type="button" class="secondary small" id="jp-switch"
            data-enabled="${jp.enabled}">${jp.enabled ? t('ad.jpPause') : t('ad.jpResume')}</button>` : ''}
        ${jp.passes.length ? `<button type="button" class="secondary small" id="jp-export">${
          t('ad.exportCsv')}</button>` : ''}
      </header>

      ${jp.problem ? `<div class="note-panel warn" role="note">${icon('warning')}<p>${
        escapeHtml(jp.problem)}</p></div>` : ''}

      <dl class="facts">
        <div><dt>${t('ad.jpState')}</dt><dd>${states[jp.state] || escapeHtml(jp.state)}</dd></div>
        <div><dt>${t('ad.jpCloses')}</dt><dd>${escapeHtml(closes)}</dd></div>
        <div><dt>${t('ad.jpIssued')}</dt><dd>${t('ad.jpIssuedN', {
          n: jp.issued_24h, max: jp.daily_max })}</dd></div>
        <div><dt>${t('ad.jpMail')}</dt><dd>${jp.mail_configured
          ? `<span class="sev medium">${mark('check', { size: 11 })}${t('ad.jpMailOn')}</span>`
          : `<span class="sev high">${mark('dash', { size: 11 })}${t('ad.jpMailOff')}</span>`}</dd></div>
        ${jp.link ? `<div><dt>${t('ad.jpLink')}</dt><dd class="mono jp-link">${
          escapeHtml(jp.link)}</dd></div>` : ''}
      </dl>

      ${jp.passes.length ? `<div class="table-wrap"><table>
        <thead><tr><th>${t('ad.jpColEmail')}</th><th>${t('ad.jpColIssued')}</th>
          <th>${t('ad.jpColMail')}</th><th>${t('ad.jpColLast')}</th></tr></thead>
        <tbody>${jp.passes.map((p) => `<tr>
          <td class="mono">${escapeHtml(p.email)}</td>
          <td class="mono">${fmtDateTime(p.created_at)}</td>
          <td title="${escapeHtml(p.mail_detail || '')}">${
            mailCells[p.mail_status] || escapeHtml(p.mail_status)}</td>
          <td class="mono">${fmtDateTime(p.last_login_at)}</td>
        </tr>`).join('')}</tbody>
      </table></div>` : emptyState({ title: t('ad.jpEmptyTitle'), body: t('ad.jpEmptyBody') })}

      <p class="caption">${t('ad.jpCaption')}</p>
    </section>`;
}

function wireJudgePass(root, jp, reload) {
  if (!jp) return;
  root.querySelector('#jp-switch')?.addEventListener('click', async (event) => {
    const enabling = event.currentTarget.dataset.enabled !== 'true';
    await guard(root, async () => {
      await api.setJudgePass(enabling);
      await reload(enabling ? t('ad.jpResumedDone') : t('ad.jpPausedDone'));
    });
  });
  /* The list is the contact sheet the booth was for. Named columns, for the
     same reason the audit export names them. */
  root.querySelector('#jp-export')?.addEventListener('click', () => {
    downloadCsv(`gemp-judge-passes-${new Date().toISOString().slice(0, 10)}.csv`,
      jp.passes.map((p) => ({
        email: p.email, issued: p.created_at || '', email_status: p.mail_status,
        last_sign_in: p.last_login_at || '',
      })));
  });
}

export const admin = {
  title: t('ad.title'),
  /* `requiredRole` is what dims this item in the rail, and the rail keeps it for
   * everyone: an item that vanishes teaches nobody what they cannot see. The
   * view itself decides what to render, and for a non-admin that is an
   * explanation rather than a locked door. */
  requiredRole: 'admin',
  async render(root, ctx) {
    if (!can(ctx.user, 'admin')) {
      root.innerHTML = `
        ${pageHead({ root, title: t('ad.title') })}
        <div class="note-panel" role="note">
          ${icon('lock')}
          <p><strong>${t('ad.lockedRole', {
            role: t(`role.${ctx.user.role}`),
          })}</strong> ${t('ad.lockedBody')}</p>
        </div>
        <section class="panel">
          <header><h3>${t('ad.holdsTitle')}</h3></header>
          <ul class="prose-list">
            <li>${t('ad.holdsPosture')}</li>
            <li>${t('ad.holdsAccounts')}</li>
            <li>${t('ad.holdsAudit')}</li>
          </ul>
          <p class="caption">${t('ad.lockedCaption')}</p>
        </section>`;
      return;
    }

    root.innerHTML = `
      ${pageHead({ root, title: t('ad.title') })}
      ${skeletonRows(6)}`;

    await guard(root, async () => {
      const [users, posture, auditRows, passes] = await Promise.all([
        api.users(), api.securityPosture(), api.audit(50),
        /* The Judge Pass panel is an addition, not a dependency: if it cannot be
           read, the rest of Administration renders exactly as it did before. */
        api.judgePass().catch(() => null),
      ]);

      const auditColumns = [
        { key: 'ts', label: t('ad.colWhen'), sortable: true,
          render: (r) => `<span class="mono">${fmtDateTime(r.ts)}</span>` },
        { key: 'username', label: t('ad.colUser'), sortable: true,
          render: (r) => escapeHtml(r.username || '—') },
        { key: 'action', label: t('ad.colAction'), sortable: true, cls: 'mono',
          render: (r) => escapeHtml(r.action) },
        { key: 'target', label: t('ad.colTarget'), render: (r) => escapeHtml(r.target || '—') },
        { key: 'outcome', label: t('ad.colOutcome'), sortable: true,
          /* Denied stands out: the row is critical-soft, the word is bold and it
           * carries the cross. A refused action is the one line in an audit log
           * anybody scans for. */
          render: (r) => (r.outcome === 'denied'
            ? `<span class="sev critical">${mark('cross', { size: 11 })}${t('ad.denied')}</span>`
            /* The outcome is an enum from the audit table, so it is looked up
               rather than printed; an unrecognised one prints itself. */
            : `<span class="muted">${escapeHtml(
                r.outcome === 'allowed' ? t('ad.allowed') : String(r.outcome ?? '—'))}</span>`) },
        { key: 'ip', label: t('ad.colAddress'), cls: 'mono', render: (r) => escapeHtml(r.ip || '—') },
      ];
      const auditState = { sortKey: 'ts', sortDir: 'desc', deniedOnly: false };
      const warnings = posture.warnings || [];

      root.innerHTML = `
        ${pageHead({ root,
          title: t('ad.title'),
          actions: `<button id="recompute-btn" type="button" class="secondary">${
                     t('ad.recompute')}</button>`
                   + `<button id="user-add" type="button" class="primary">${
                     t('ad.addAccount')}</button>`,
        })}

        <section class="panel">
          <header><h3>${t('ad.posture')}</h3>
            <span class="scope">${t('ad.postureScope')}</span></header>

          <!-- The endpoint computes a list of warnings precisely so that somebody
               can see them - "a security control nobody can see the state of is a
               control nobody maintains" is the reason given in its own source -
               and this screen was discarding every one of them except the cookie
               flag. The verdict states how many stand, and each is named. -->
          <div class="posture-verdict ${warnings.length ? 'bad' : 'good'}">
            <span class="verdict-shield">${icon(warnings.length ? 'warning' : 'integrity')}</span>
            <span class="verdict-text">
              <span class="verdict-word">${warnings.length === 1
                ? t('ad.toFixOne')
                : warnings.length
                  ? t('ad.toFix', { n: warnings.length })
                  : t('ad.nothingOutstanding')}</span>
              <span class="verdict-gloss">${warnings.length
                ? t('ad.postureBad') : t('ad.postureGood')}</span>
            </span>
          </div>

          ${warnings.length ? `<ul class="posture-warnings">
            ${warnings.map((w) => `<li>${mark('cross', { size: 12 })}<span>${
              escapeHtml(w)}</span></li>`).join('')}
          </ul>` : ''}

          <!-- The Secure flag keeps a callout of its own even though it is also in
               the list above: it is the one item on this screen that changes what
               an attacker on the same network can do without a password. -->
          ${posture.cookie_secure ? '' : `
          <div class="posture-alert" role="alert">
            <h4 class="plain">${t('ad.cookieTitle')}</h4>
            <p>${t('ad.cookieBody')}</p>
          </div>`}

          <dl class="facts">
            <div><dt>${t('ad.cookieFlag')}</dt>
            <dd>${posture.cookie_secure
              ? `<span class="sev medium">${mark('check', { size: 11 })}${t('ad.on')}</span>`
              : `<span class="sev critical">${mark('cross', { size: 11 })}${t('ad.off')}</span>`}</dd></div>
            <div><dt>${t('ad.idleTimeout')}</dt><dd>${t('ad.hours', { n: posture.session_idle_timeout_hours })}</dd></div>
            <div><dt>${t('ad.absoluteLifetime')}</dt><dd>${t('ad.days', { n: posture.session_absolute_lifetime_days })}</dd></div>
            <div><dt>${t('ad.minPassword')}</dt><dd>${t('ad.characters', { n: posture.password_min_length })}</dd></div>
            <div><dt>${t('ad.hashCost')}</dt><dd>${posture.password_hash_cost === undefined
              ? '—' : `2^${Math.round(Math.log2(posture.password_hash_cost))} (n=${
                  compact(posture.password_hash_cost)})`}</dd></div>
            <div><dt>${t('ad.hashesBelow')}</dt><dd>${posture.password_hashes_below_current_cost
              ? `<span class="sev high">${mark('dash', { size: 11 })}${
                  t('ad.accountsN', { n: posture.password_hashes_below_current_cost })}</span>`
              : `<span class="sev medium">${mark('check', { size: 11 })}${t('rn.none')}</span>`}</dd></div>
            <div><dt>${t('ad.auditFailures')}</dt><dd>${posture.audit_write_failures
              ? `<span class="sev critical">${mark('cross', { size: 11 })}${
                  posture.audit_write_failures}</span>`
              : `<span class="sev medium">${mark('check', { size: 11 })}${t('rn.none')}</span>`}</dd></div>
            <div><dt>${t('ad.accounts')}</dt><dd>${t('ad.usersSummary', {
              total: (posture.users && posture.users.total) ?? users.length,
              admins: (posture.users && posture.users.admins) ?? '—' })}${
              users.filter((u) => !u.is_active).length
                ? t('ad.someDisabled', { n: users.filter((u) => !u.is_active).length })
                : t('ad.noneDisabled')}</dd></div>
          </dl>

          ${posture.recent_failed_logins.length ? `<h4 class="section-label">${t('ad.recentFailed')}</h4>
            <div class="table-wrap"><table>
              <thead><tr><th>${t('ad.failedWhen')}</th><th>${t('ad.failedUsername')}</th><th>${t('ad.colAddress')}</th></tr></thead>
              <tbody>${posture.recent_failed_logins.map((f) => `<tr>
                <td class="mono">${fmtDateTime(f.ts)}</td><td>${escapeHtml(f.username)}</td>
                <td class="mono">${escapeHtml(f.ip)}</td></tr>`).join('')}</tbody>
            </table></div>
            <p class="caption">${t('ad.failedCaption')}</p>`
            : `<p class="caption">${t('ad.noFailed')}</p>`}
        </section>

        ${judgePassPanel(passes)}

        <section class="panel">
          <header><h3>${t('ad.accounts')}</h3><span class="muted small">${
            t('ad.accountsTotal', { n: users.length })}</span></header>
          <div class="table-wrap"><table>
            <thead><tr><th>${t('ad.colUsername')}</th><th>${t('ad.colName')}</th><th>${
              t('ad.colRole')}</th><th>${t('ad.colStatus')}</th>
              <th>${t('ad.colLastLogin')}</th><th class="row-actions"><span class="sr-only">${
              t('ui.actions')}</span></th></tr></thead>
            <!-- The two cells an admin cannot use on their own row show a lock
                 and the reason, not a greyed-out control. A disabled select
                 invites a click and explains nothing; the words do both. -->
            <tbody>${users.map((u) => {
              const self = u.id === ctx.user.id || u.username === ctx.user.username;
              return `<tr>
              <td class="mono">${escapeHtml(u.username)}${
                self ? `<span class="badge">${t('ad.you')}</span>` : ''}</td>
              <td>${escapeHtml(u.display_name)}</td>
              <td>${self && u.role === 'admin'
                ? `<span class="locked-cell">${mark('cross', { size: 11 })}${t('ad.ownAdmin')}</span>`
                : selectWrap(`<select class="inline" data-role="${escapeHtml(u.id)}"
                        data-current="${escapeHtml(u.role)}"
                        data-username="${escapeHtml(u.username)}"
                        aria-label="${escapeHtml(t('ad.roleAria', { name: u.username }))}">
                  ${['viewer', 'analyst', 'admin'].map((r) =>
                    `<option value="${r}"${r === u.role ? ' selected' : ''}>${t(`role.${r}`)}</option>`).join('')}
                </select>`)}
              </td>
              <td>${u.must_change_password
                ? `<span class="state-cell warn">${mark('dash', { size: 11 })}${t('ad.mustSetPassword')}</span>`
                : u.is_active
                  ? `<span class="state-cell ok">${mark('check', { size: 11 })}${t('ad.active')}</span>`
                  : `<span class="state-cell bad">${mark('cross', { size: 11 })}${t('ad.disabled')}</span>`}</td>
              <td class="mono">${fmtDateTime(u.last_login_at)}</td>
              <td class="row-actions">
                ${self
                  ? `<span class="locked-cell">${mark('cross', { size: 11 })}${t('ad.cannotDisableSelf')}</span>`
                  : `<button class="secondary small" type="button" data-toggle="${escapeHtml(u.id)}"
                  data-active="${u.is_active}" data-username="${escapeHtml(u.username)}"
                  aria-label="${u.is_active ? t('ad.disable') : t('ad.enable')} ${escapeHtml(u.username)}"
                  >${u.is_active ? t('ad.disable') : t('ad.enable')}</button>`}
                <button class="secondary small" type="button" data-reset="${escapeHtml(u.id)}"
                  data-username="${escapeHtml(u.username)}"
                  aria-label="${escapeHtml(t('ad.resetAria', { name: u.username }))}">${t('ad.resetPassword')}</button>
              </td>
            </tr>`; }).join('')}</tbody>
          </table></div>
          <p class="caption">${t('ad.usersCaption')}</p>
        </section>

        <section class="panel">
          <header>
            <h3>${t('ad.auditLog')}</h3>
            <span class="scope">${t('ad.auditScope')}</span>
            <button type="button" class="seg-btn deny-toggle" id="denied-only"
                    aria-pressed="false">${t('ad.deniedOnly')}</button>
            <button type="button" class="secondary small" id="audit-export">${t('ad.exportCsv')}</button>
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
              title: auditState.deniedOnly ? t('ad.auditEmptyDeniedTitle') : t('ad.auditEmptyTitle'),
              body: auditState.deniedOnly ? t('ad.auditEmptyDenied') : t('ad.auditEmpty') });
        wireSort(body, auditState, paintAudit);
      };
      paintAudit();
      wireJudgePass(root, passes, reload);

      root.querySelector('#denied-only').addEventListener('click', (event) => {
        auditState.deniedOnly = !auditState.deniedOnly;
        event.currentTarget.setAttribute('aria-pressed', String(auditState.deniedOnly));
        paintAudit();
      });

      root.querySelector('#audit-export').addEventListener('click', () => {
        const rows = auditState.deniedOnly
          ? auditRows.filter((r) => r.outcome === 'denied')
          : auditRows;
        if (!rows.length) return;
        /* The columns are named rather than spread from the row, so a field added
         * to the API later cannot silently start appearing in an exported audit
         * file that somebody is treating as a fixed format. */
        downloadCsv(
          `gemp-audit-${auditState.deniedOnly ? 'denied-' : ''}${
            new Date().toISOString().slice(0, 10)}.csv`,
          rows.map((r) => ({
            ts: r.ts, username: r.username || '', action: r.action,
            target: r.target || '', outcome: r.outcome, ip: r.ip || '',
          })));
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
            title: t('ad.roleTitle', { name }),
            description: t('ad.roleBody', {
                name, next: t(`role.${next}`), previous: t(`role.${previous}`),
              })
              + (next === 'admin' ? t('ad.roleAdmin')
                : next === 'analyst' ? t('ad.roleAnalyst') : t('ad.roleViewer')),
            confirmLabel: t('ad.roleConfirm', { name, next: t(`role.${next}`) }),
          });
          if (!ok) { select.value = previous; return; }
          await guard(root, async () => {
            await api.updateUser(select.dataset.role, { role: next });
            await reload(t('ad.roleDone', { name, role: t(`role.${next}`) }));
          });
        });
      });

      root.querySelectorAll('[data-toggle]').forEach((button) => {
        button.addEventListener('click', async () => {
          const disabling = button.dataset.active === 'true';
          const name = button.dataset.username;
          if (disabling) {
            const ok = await confirmAction({
              title: t('ad.disableTitle', { name }),
              description: t('ad.disableBody'),
              confirmLabel: t('ad.disableConfirm', { name }),
            });
            if (!ok) return;
          }
          await guard(root, async () => {
            await api.updateUser(button.dataset.toggle, { is_active: !disabling });
            await reload(t('ad.stateDone', {
              name, state: disabling ? t('ad.disabled') : t('ad.enabled'),
            }));
          });
        });
      });

      root.querySelectorAll('[data-reset]').forEach((button) => {
        button.addEventListener('click', async () => {
          const name = button.dataset.username;
          const values = await openDialog({
            title: t('ad.resetTitle', { name }),
            description: t('ad.resetBody'),
            submitLabel: t('ad.resetConfirm'),
            validate: passwordProblem,
            fields: [
              { name: 'password', label: t('ad.newPassword'), type: 'password',
                autocomplete: 'new-password', minlength: MIN_PASSWORD,
                hint: t('ad.minChars', { n: MIN_PASSWORD }) },
              { name: 'confirm', label: t('ad.repeatNew'), type: 'password',
                autocomplete: 'new-password' },
            ],
          });
          if (!values) return;
          await guard(root, async () => {
            await api.resetPassword(button.dataset.reset, values.password);
            await reload(t('ad.resetDone', { name }));
          });
        });
      });

      /* Also in the workspace header, for the same reason. */
      document.querySelector('#user-add')?.addEventListener('click', async () => {
        const values = await openDialog({
          title: t('ad.addTitle'),
          description: t('ad.addBody'),
          submitLabel: t('ad.addConfirm'),
          validate: (v) => (!v.username.trim() ? t('ad.usernameRequired') : passwordProblem(v)),
          fields: [
            { name: 'username', label: t('ad.username'), autocomplete: 'off' },
            { name: 'password', label: t('ad.tempPassword'), type: 'password',
              autocomplete: 'new-password', minlength: MIN_PASSWORD,
              hint: t('ad.minChars', { n: MIN_PASSWORD }) },
            { name: 'confirm', label: t('ad.repeatPassword'), type: 'password',
              autocomplete: 'new-password' },
            { name: 'role', label: t('ad.roleField'), type: 'select',
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
          await reload(t('ad.addDone', {
            name: values.username.trim(), role: t(`role.${values.role}`),
          }));
        });
      });

      /* Recompute was a curl command with no face. The endpoint has existed and
       * been admin-gated since Phase 1; this is it finally wired to a button, so
       * "the parameters are data, not code" is something you can demonstrate by
       * clicking rather than by opening a terminal. */
      root.querySelector('#recompute-btn')?.addEventListener('click', async () => {
        const ok = await confirmAction({
          title: t('ad.recomputeTitle'),
          description: t('ad.recomputeBody'),
          confirmLabel: t('ad.recomputeConfirm'),
          confirmText: String(await api.meta().then((m) => m.buildings).catch(() => 50)),
        });
        if (!ok) return;
        await guard(root, async () => {
          const result = await api.recompute();
          setStatus(root, {
            kind: 'ok',
            message: t('ad.recomputeDone', {
                candidates: result.candidates, buildings: result.buildings,
                interventions: result.interventions, hash: result.inputs_hash,
              })
              + (result.uncited_catalog_rows.length
                ? t('ad.recomputeUncited', { rows: result.uncited_catalog_rows.join(', ') })
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
    : t('ua.unknown');
  const platform = /Windows/.test(ua) ? 'Windows'
    : /Macintosh|Mac OS/.test(ua) ? 'macOS'
    : /Android/.test(ua) ? 'Android'
    : /iPhone|iPad/.test(ua) ? 'iOS'
    : /Linux/.test(ua) ? 'Linux'
    : '';
  return platform ? t('ua.on', { engine, platform }) : engine;
}

export const account = {
  title: 'Account',
  async render(root, ctx) {
    root.innerHTML = `
      ${pageHead({ root, title: t('ac.title') })}
      ${skeletonRows(4)}`;

    await guard(root, async () => {
      const sessions = await api.mySessions();
      const others = Math.max(0, sessions.length - 1);
      const hereIp = (sessions.find((x) => x.current) || {}).ip || null;
      const elsewhere = sessions.filter((x) => !x.current && x.ip && hereIp && x.ip !== hereIp);

      root.innerHTML = `
        ${pageHead({ root, title: t('ac.title') })}

        <div class="two-col aside-first">
          <section class="panel">
            <header><h3>${t('ac.changePassword')}</h3></header>

            <!-- Said before the fields, not after the submit. Changing the
                 password is also the only way to end another session in this
                 platform, so somebody may be here to do exactly that - and
                 somebody else may not have realised it happens at all. -->
            <div class="posture-alert" role="note">
              <h4 class="plain">${t('ac.signsOutOthers')}</h4>
              <p>${others === 0 ? t('ac.noOthers')
                : others === 1 ? t('ac.oneOther')
                : t('ac.manyOthers', { n: others })}</p>
            </div>

            <form id="pw-form" class="form">
              <label for="pw-current">${t('ac.currentPassword')}
                <span class="password-field">
                  <input type="password" id="pw-current" class="mono"
                         autocomplete="current-password" required>
                  <button type="button" class="reveal" data-reveal="pw-current"
                          aria-pressed="false" aria-label="${t('ac.showCurrent')}">
                    ${icon('eye')}</button>
                </span></label>

              <label for="pw-new">${t('ac.newPassword')}
                <input type="password" id="pw-new" class="mono" autocomplete="new-password"
                       minlength="${MIN_PASSWORD}" required>
                <span class="meter" id="pw-meter" aria-hidden="true"><span></span></span>
                <span class="hint" id="pw-hint">${t('ac.atLeast', { n: MIN_PASSWORD })}</span></label>

              <label for="pw-confirm">${t('ac.repeatNew')}
                <input type="password" id="pw-confirm" class="mono"
                       autocomplete="new-password" required>
                <span class="hint" id="pw-match"></span></label>

              <div id="pw-result"></div>
              <div class="form-actions">
                <button type="submit" class="primary" id="pw-submit" disabled>
                  ${t('ac.fillEveryField')}</button>
              </div>
              <p class="caption">${t('ac.managerNote')}</p>
            </form>
          </section>

          <section class="panel">
            <header>
              <h3>${t('ac.signedInWhere')}</h3>
              <span class="scope">${t('ac.activeN', { n: sessions.length })}</span>
            </header>
            ${sessions.length ? `<div class="table-wrap"><table>
              <thead><tr><th>${t('ac.colStarted')}</th><th>${t('ac.colLastSeen')}</th><th>${
                t('ad.colAddress')}</th><th>${t('ac.colBrowser')}</th></tr></thead>
              <tbody>${sessions.map((s) => `<tr${s.current ? ' class="chosen"' : ''}>
                <td class="mono">${fmtDateTime(s.created_at)}${s.current
                  ? `<span class="state-cell ok">${mark('check', { size: 11 })}${t('ac.thisOne')}</span>` : ''}</td>
                <td class="mono">${fmtDateTime(s.last_seen_at)}</td>
                <td class="mono">${escapeHtml(s.ip || '—')}${
                  !s.current && s.ip && hereIp && s.ip !== hereIp
                    ? `<span class="state-cell warn">${mark('dash', { size: 11 })
                       }${t('ac.differentAddress')}</span>` : ''}</td>
                <td class="muted small wrap" title="${escapeHtml(s.user_agent || '')}"
                  >${escapeHtml(browserName(s.user_agent))}</td>
              </tr>`).join('')}</tbody>
            </table></div>` : emptyState({
              title: t('ac.onlyThisTitle'),
              body: t('ac.onlyThisBody') })}
            ${elsewhere.length ? `
            <div class="posture-alert" role="note">
              <h4 class="plain">${elsewhere.length === 1
                ? t('ac.oneElsewhere')
                : t('ac.manyElsewhere', { n: elsewhere.length })}</h4>
              <p>${t('ac.elsewhereBody', {
                them: elsewhere.length === 1 ? t('ac.itOne') : t('ac.itMany'),
                them2: elsewhere.length === 1 ? t('ac.itOne') : t('ac.itMany'),
              })}</p>
            </div>` : ''}
            <p class="caption">${t('ac.closingCaption')}</p>
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
          ? t('ac.atLeast', { n: MIN_PASSWORD })
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
        submit.textContent = ready ? t('ac.submitReady')
          : !current.value ? t('ac.needCurrent')
          : !long ? t('ac.tooShort')
          : same ? t('ac.mustDiffer')
          : t('ac.mustMatch');
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
            message: t('ac.passwordChanged', {
                user: ctx.user.username,
                time: new Date().toLocaleTimeString(locale()),
              })
              + (others
                ? (others === 1 ? t('ac.oneOtherOut') : t('ac.othersOut', { n: others }))
                : t('ac.noOthers')),
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
