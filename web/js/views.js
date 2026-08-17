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
 */

'use strict';

import { api, ApiError } from './api.js';
import { compact, escapeHtml, lineChart, proportionBar, stackedBars, statTile } from './charts.js';

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
    root.innerHTML = errorBox(error);
  }
}

/* A wide table inside a panel scrolls itself rather than pushing the page
 * sideways. The audit log is six columns of timestamps and addresses and does
 * not fit a narrow window at any font size. */
const table = (inner) => `<div class="table-wrap">${inner}</div>`;

/* ------------------------------------------------------------------- dialog */

/* A real dialog, because three chained `window.prompt` calls were how this
 * application used to mint credentials: clear text, no confirmation field, no
 * validation before the request went out, and nothing a password manager can
 * fill. It is also the only modal built by hand here rather than by the
 * browser, so it owns its own focus and Escape handling.
 *
 * Resolves to the form's values, or to null if it was dismissed.
 */
function openDialog({ title, fields, submitLabel, validate }) {
  return new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'modal';
    host.innerHTML = `
      <div class="modal-inner narrow" role="dialog" aria-modal="true"
           aria-label="${escapeHtml(title)}">
        <button class="close" type="button" data-close aria-label="Cancel">&times;</button>
        <h2>${escapeHtml(title)}</h2>
        <form class="form" data-form novalidate>
          ${fields.map((f) => f.type === 'select'
            ? `<label for="dlg-${f.name}">${escapeHtml(f.label)}
                 <select id="dlg-${f.name}" name="${f.name}">
                   ${f.options.map((o) => `<option value="${escapeHtml(o)}"${
                     o === f.value ? ' selected' : ''}>${escapeHtml(o)}</option>`).join('')}
                 </select></label>`
            : `<label for="dlg-${f.name}">${escapeHtml(f.label)}
                 <input id="dlg-${f.name}" name="${f.name}" type="${f.type || 'text'}"
                        autocomplete="${f.autocomplete || 'off'}"${f.minlength
                          ? ` minlength="${f.minlength}"` : ''}>
                 ${f.hint ? `<span class="hint">${escapeHtml(f.hint)}</span>` : ''}</label>`
          ).join('')}
          <div data-error></div>
          <div class="form-actions">
            <button type="submit" class="primary">${escapeHtml(submitLabel)}</button>
            <button type="button" class="ghost" data-close>Cancel</button>
            ${fields.some((f) => f.type === 'password')
              ? '<button type="button" class="ghost small" data-generate>Suggest a password</button>'
              : ''}
          </div>
        </form>
      </div>`;

    const form = host.querySelector('[data-form]');
    const errors = host.querySelector('[data-error]');

    const close = (value) => {
      document.removeEventListener('keydown', onKey);
      host.remove();
      resolve(value);
    };
    const onKey = (event) => { if (event.key === 'Escape') close(null); };

    host.querySelectorAll('[data-close]').forEach((b) =>
      b.addEventListener('click', () => close(null)));
    /* Clicking the scrim dismisses; clicking inside must not. */
    host.addEventListener('click', (event) => { if (event.target === host) close(null); });
    document.addEventListener('keydown', onKey);

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
        return;
      }
      close(values);
    });

    document.body.appendChild(host);
    host.querySelector('input, select')?.focus();
  });
}

const MIN_PASSWORD = 12;

function passwordProblem({ password, confirm }) {
  if (!password) return 'A password is required.';
  if (password.length < MIN_PASSWORD) {
    return `At least ${MIN_PASSWORD} characters. That one has ${password.length}.`;
  }
  if (confirm !== undefined && password !== confirm) return 'The two passwords do not match.';
  return null;
}

/* ------------------------------------------------------------------ overview */

export const overview = {
  title: 'Overview',
  async render(root, ctx) {
    root.innerHTML = '<div class="loading">Loading…</div>';
    await guard(root, async () => {
      const [summary, load, daily] = await Promise.all([
        api.summary(), api.load(14), api.anomaliesDaily(30),
      ]);

      const measured = summary.annual_kwh_source || {};
      const fromForecast = measured.forecast || 0;
      const run = summary.latest_run;

      root.innerHTML = `
        <div class="stat-row">
          ${statTile('Buildings', compact(summary.buildings))}
          ${statTile('Readings stored', compact(summary.readings))}
          ${statTile('Open alerts', compact(summary.open_anomalies),
                     Object.entries(summary.open_by_severity || {})
                       .map(([k, v]) => `${k} ${v}`).join(' · '))}
          ${statTile('Costed on measurement', `${fromForecast}/${summary.buildings}`,
                     'F3: buildings whose annual kWh comes from the forecast')}
        </div>

        <section class="panel">
          <header><h3>Portfolio demand</h3><span class="muted small">last 14 days of data time</span></header>
          ${lineChart([{ label: 'Portfolio kW', points: load.points }], { unit: 'kW' })}
          <p class="caption">Data clock: <strong>${fmtDateTime(summary.data_clock)}</strong>.
          The simulator runs at 720&times;, so this runs ahead of the wall clock — every
          window in the platform is measured in data time for that reason.</p>
        </section>

        <section class="panel">
          <header><h3>Alerts per day</h3><span class="muted small">last 30 days of data time</span></header>
          ${stackedBars(daily.days, ['critical', 'high', 'medium'])}
        </section>

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
            : '<p class="muted">No allocation has been run yet.</p>'}
        </section>`;
    });
  },
};

/* ----------------------------------------------------------------- forecasts */

export const forecasts = {
  title: 'Forecasts',
  async render(root, ctx) {
    root.innerHTML = '<div class="loading">Loading…</div>';
    await guard(root, async () => {
      const [metrics, buildings] = await Promise.all([
        api.get('/metrics/forecast'), api.get('/buildings'),
      ]);

      const options = buildings
        .map((b) => `<option value="${escapeHtml(b.id)}">${escapeHtml(b.code)}</option>`)
        .join('');

      root.innerHTML = `
        <section class="panel">
          <header><h3>Which forecaster is in use</h3></header>
          ${proportionBar((metrics.by_model || []).map((m) => ({
            label: m.model_version, value: m.buildings,
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
              <label class="inline-label" for="fc-building">Building</label>
              <select id="fc-building" class="inline">${options}</select>
            </div>
          </header>
          <div id="fc-chart"><div class="loading">Loading…</div></div>
          <p class="caption">A MAPE figure alone says the model is good without letting
          anyone see where it is wrong.</p>
        </section>`;

      const select = root.querySelector('#fc-building');
      const draw = async () => {
        const target = root.querySelector('#fc-chart');
        target.innerHTML = '<div class="loading">Loading…</div>';
        const data = await api.forecast(select.value, 336);
        target.innerHTML = lineChart([
          { label: 'Actual', points: data.actual },
          { label: 'Forecast', points: data.forecast },
        ], { unit: 'kW' });
      };
      select.addEventListener('change', () => guard(root, draw));
      await draw();
    });
  },
};

/* ------------------------------------------------------------------- alerts */

export const alerts = {
  title: 'Alerts',
  async render(root, ctx) {
    const state = { severity: '', onlyOpen: true, total: null, bySeverity: {} };

    /* The list is capped at 100 rows by the API. Acknowledging a row used to
     * refill it from a pool of about thirty thousand with nothing on screen
     * saying so, which reads as an inbox that cannot be emptied. The count comes
     * from the summary endpoint, which counts rather than lists. */
    const countLine = (shown) => {
      if (!state.onlyOpen) return `Showing the ${shown} largest deviations.`;
      const total = state.severity
        ? state.bySeverity[state.severity]
        : state.total;
      if (total === null || total === undefined) return `Showing ${shown}.`;
      return total > shown
        ? `Showing the ${shown} largest of ${compact(total)} open` +
          `${state.severity ? ` ${state.severity}` : ''} alerts.`
        : `Showing all ${shown} open${state.severity ? ` ${state.severity}` : ''} alerts.`;
    };

    const draw = async () => {
      const body = root.querySelector('#alert-body');
      body.innerHTML = '<div class="loading">Loading…</div>';
      const params = `?limit=100&only_open=${state.onlyOpen}` +
        (state.severity ? `&severity=${encodeURIComponent(state.severity)}` : '');
      const [rows, summary] = await Promise.all([api.anomalyFeed(params), api.summary()]);
      state.total = summary.open_anomalies;
      state.bySeverity = summary.open_by_severity || {};

      if (!rows.length) {
        body.innerHTML = '<p class="muted">Nothing open. The inbox is empty.</p>';
        return;
      }

      body.innerHTML = table(`<table>
        <thead><tr>
          <th>Building</th><th>When (data time)</th><th class="num">Observed</th>
          <th class="num">Expected</th><th class="num">z</th><th>Severity</th>
          ${can(ctx.user, 'analyst') ? '<th><span class="sr-only">Actions</span></th>' : ''}
        </tr></thead>
        <tbody>${rows.map((row) => `
          <tr>
            <td>${escapeHtml(row.building_code)}</td>
            <td>${fmtDateTime(row.ts)}</td>
            <td class="num">${compact(row.observed_kw)} kW</td>
            <td class="num">${compact(row.expected_kw)} kW</td>
            <td class="num">${row.robust_z === null ? '—' : row.robust_z.toFixed(1)}</td>
            <td><span class="sev ${escapeHtml(row.severity)}">${escapeHtml(row.severity)}</span></td>
            ${can(ctx.user, 'analyst')
              ? `<td class="row-actions"><button class="ghost small" data-ack="${row.id}"
                   aria-label="Acknowledge the ${escapeHtml(row.severity)} alert on
                   ${escapeHtml(row.building_code)}">Acknowledge</button></td>`
              : ''}
          </tr>`).join('')}
        </tbody></table>`) +
        `<div class="table-foot"><span>${escapeHtml(countLine(rows.length))}</span></div>`;

      body.querySelectorAll('[data-ack]').forEach((button) => {
        button.addEventListener('click', async () => {
          button.disabled = true;
          await guard(root, async () => {
            await api.acknowledgeOne(Number(button.dataset.ack));
            await draw();
          });
        });
      });
    };

    root.innerHTML = `
      <section class="panel">
        <header>
          <h3>Alert inbox</h3>
          <div class="toolbar">
            <select id="alert-sev" class="inline" aria-label="Filter by severity">
              <option value="">All severities</option>
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
            </select>
            <label class="check"><input type="checkbox" id="alert-open" checked> Open only</label>
            ${can(ctx.user, 'analyst')
              ? '<button id="alert-ack-all" type="button" class="ghost small">Acknowledge by severity…</button>'
              : ''}
          </div>
        </header>
        <div id="alert-notice"></div>
        <div id="alert-body"></div>
        <p class="caption">Acknowledging is reversible, and closing everything at once
        needs a filter — an unfiltered acknowledge would close every alert in the
        portfolio and is refused by the API.</p>
      </section>`;

    const notice = root.querySelector('#alert-notice');

    root.querySelector('#alert-sev').addEventListener('change', (e) => {
      state.severity = e.target.value;
      notice.innerHTML = '';
      guard(root, draw);
    });
    root.querySelector('#alert-open').addEventListener('change', (e) => {
      state.onlyOpen = e.target.checked;
      notice.innerHTML = '';
      guard(root, draw);
    });

    const ackAll = root.querySelector('#alert-ack-all');
    if (ackAll) {
      ackAll.addEventListener('click', async () => {
        /* This closes every alert of the chosen severity, not the hundred on
         * screen - which is what the button used to claim. It says so, in place,
         * rather than through a blocking window.alert that stops the page. */
        if (!state.severity) {
          notice.innerHTML = `<div class="warn-box" role="alert">Choose a severity first.
            Acknowledging everything at once is deliberately not one click.</div>`;
          root.querySelector('#alert-sev').focus();
          return;
        }
        const total = state.bySeverity[state.severity];
        notice.innerHTML = `<div class="warn-box">
          This closes <strong>every open ${escapeHtml(state.severity)} alert</strong>${
            total ? ` — ${compact(total)} of them` : ''}, not only the rows shown.
          Acknowledging is reversible.
          <div class="form-actions">
            <button type="button" class="danger" data-confirm-ack>Acknowledge all
              ${escapeHtml(state.severity)}</button>
            <button type="button" class="ghost small" data-cancel-ack>Cancel</button>
          </div></div>`;

        notice.querySelector('[data-cancel-ack]')
          .addEventListener('click', () => { notice.innerHTML = ''; });
        notice.querySelector('[data-confirm-ack]').addEventListener('click', async () => {
          notice.innerHTML = '';
          await guard(root, async () => {
            await api.acknowledge({ severity: state.severity });
            await draw();
          });
        });
        notice.querySelector('[data-confirm-ack]').focus();
      });
    }

    await guard(root, draw);
  },
};

/* --------------------------------------------------------------------- runs */

export const runs = {
  title: 'Allocations',
  async render(root, ctx) {
    root.innerHTML = '<div class="loading">Loading…</div>';
    await guard(root, async () => {
      const rows = await api.runs();
      root.innerHTML = `
        <section class="panel">
          <header><h3>Stored allocations</h3><span class="muted small">newest first</span></header>
          ${rows.length ? table(`<table>
            <thead><tr>
              <th>When</th><th class="num">Budget</th><th>Objective</th><th>Solver</th>
              <th class="num">Funded</th><th class="num">Spent</th>
              <th class="num">Benefit</th><th>Inputs</th>
            </tr></thead>
            <tbody>${rows.map((r) => `<tr>
              <td>${fmtDateTime(r.created_at)}</td>
              <td class="num">${compact(r.budget_egp)}</td>
              <td>${escapeHtml(r.objective)}</td>
              <td>${escapeHtml(r.solver)}</td>
              <td class="num">${r.buildings_funded}</td>
              <td class="num">${compact(r.total_cost_egp)}</td>
              <td class="num">${compact(r.total_benefit_kgco2e)}</td>
              <td class="mono">${escapeHtml(r.inputs_hash)}</td>
            </tr>`).join('')}</tbody></table>`)
            : '<p class="muted">No allocations stored yet.</p>'}
          <p class="caption">Every run records the hash of the catalog, parameters and
          consumption that produced it, so a stored recommendation can be reproduced
          against the inputs as they were rather than as they are now.</p>
        </section>`;
    });
  },
};

/* ---------------------------------------------------------------- integrity */

export const integrity = {
  title: 'Integrity',
  async render(root, ctx) {
    root.innerHTML = '<div class="loading">Loading…</div>';
    await guard(root, async () => {
      const buildings = await api.get('/buildings');
      const options = buildings
        .map((b) => `<option value="${escapeHtml(b.id)}">${escapeHtml(b.code)}</option>`)
        .join('');

      root.innerHTML = `
        <section class="panel">
          <header>
            <h3>Chain verification</h3>
            <div class="toolbar">
              <label class="inline-label" for="int-building">Building</label>
              <select id="int-building" class="inline">${options}</select>
            </div>
          </header>
          <div id="int-body"><div class="loading">Loading…</div></div>
          <p class="caption">Each reading is signed and chained to the one before it, so
          a modified row breaks the walk at exactly that row. A deleted tail breaks
          nothing — there is nothing after it left to check — which is why chain heads
          are also written to a file outside the database volume and compared here.</p>
        </section>`;

      const select = root.querySelector('#int-building');
      const draw = async () => {
        const body = root.querySelector('#int-body');
        body.innerHTML = '<div class="loading">Verifying…</div>';
        const report = await api.verifyChain(select.value);
        const ok = report.chain_ok && report.checkpoint_ok !== false;
        body.innerHTML = `
          <div class="verdict ${ok ? 'good' : 'bad'}">
            ${ok ? 'Intact' : 'Broken'}
          </div>
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
          ${report.hint ? `<div class="hint-box">${escapeHtml(report.hint)}</div>` : ''}`;
      };
      select.addEventListener('change', () => guard(root, draw));
      await draw();
    });
  },
};

/* -------------------------------------------------------------------- admin */

export const admin = {
  title: 'Administration',
  requiredRole: 'admin',
  async render(root, ctx) {
    root.innerHTML = '<div class="loading">Loading…</div>';
    await guard(root, async () => {
      const [users, posture, auditRows] = await Promise.all([
        api.users(), api.securityPosture(), api.audit(50),
      ]);

      root.innerHTML = `
        ${posture.warnings.length ? `<div class="warn-box" role="alert">
          ${posture.warnings.map((w) => escapeHtml(w)).join('<br>')}</div>` : ''}

        <section class="panel">
          <header><h3>Security posture</h3></header>
          <dl class="facts">
            <dt>Session cookie Secure flag</dt><dd>${posture.cookie_secure ? 'on' : 'OFF'}</dd>
            <dt>Idle timeout</dt><dd>${posture.session_idle_timeout_hours} hours</dd>
            <dt>Absolute session lifetime</dt><dd>${posture.session_absolute_lifetime_days} days</dd>
            <dt>Minimum password length</dt><dd>${posture.password_min_length}</dd>
            <dt>Accounts</dt><dd>${posture.users}</dd>
          </dl>
          ${posture.recent_failed_logins.length ? `<h4>Recent failed logins</h4>
            ${table(`<table><tbody>${posture.recent_failed_logins.map((f) => `<tr>
              <td>${fmtDateTime(f.ts)}</td><td>${escapeHtml(f.username)}</td>
              <td class="mono">${escapeHtml(f.ip)}</td></tr>`).join('')}</tbody></table>`)}`
            : '<p class="muted">No failed logins recorded.</p>'}
        </section>

        <section class="panel">
          <header><h3>Accounts</h3>
            <button id="user-add" type="button" class="ghost accent small">Add user</button></header>
          ${table(`<table>
            <thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Status</th>
              <th>Last login</th><th><span class="sr-only">Actions</span></th></tr></thead>
            <tbody>${users.map((u) => `<tr>
              <td>${escapeHtml(u.username)}</td>
              <td>${escapeHtml(u.display_name)}</td>
              <td>
                <select class="inline" data-role="${escapeHtml(u.id)}"
                        aria-label="Role for ${escapeHtml(u.username)}">
                  ${['viewer', 'analyst', 'admin'].map((r) =>
                    `<option value="${r}"${r === u.role ? ' selected' : ''}>${r}</option>`).join('')}
                </select>
              </td>
              <td>${u.is_active ? 'active' : '<span class="muted">disabled</span>'}
                ${u.must_change_password ? '<span class="badge">must change password</span>' : ''}</td>
              <td>${fmtDateTime(u.last_login_at)}</td>
              <td class="row-actions">
                <button class="ghost small" type="button" data-toggle="${escapeHtml(u.id)}"
                  data-active="${u.is_active}"
                  aria-label="${u.is_active ? 'Disable' : 'Enable'} ${escapeHtml(u.username)}"
                  >${u.is_active ? 'Disable' : 'Enable'}</button>
                <button class="ghost small" type="button" data-reset="${escapeHtml(u.id)}"
                  data-username="${escapeHtml(u.username)}"
                  aria-label="Reset the password for ${escapeHtml(u.username)}">Reset password</button>
              </td>
            </tr>`).join('')}</tbody>
          </table>`)}
          <p class="caption">An admin cannot remove their own admin role or disable
          their own account: leaving a deployment with no administrator is recoverable
          only with shell access to the database.</p>
        </section>

        <section class="panel">
          <header><h3>Audit log</h3><span class="muted small">most recent 50 events</span></header>
          ${table(`<table>
            <thead><tr><th>When</th><th>User</th><th>Action</th><th>Target</th>
              <th>Outcome</th><th>Address</th></tr></thead>
            <tbody>${auditRows.map((row) => `<tr class="${row.outcome === 'denied' ? 'denied' : ''}">
              <td>${fmtDateTime(row.ts)}</td>
              <td>${escapeHtml(row.username || '—')}</td>
              <td class="mono">${escapeHtml(row.action)}</td>
              <td>${escapeHtml(row.target || '—')}</td>
              <td>${escapeHtml(row.outcome)}</td>
              <td class="mono">${escapeHtml(row.ip || '—')}</td>
            </tr>`).join('')}</tbody>
          </table>`)}
        </section>`;

      const reload = () => admin.render(root, ctx);

      root.querySelectorAll('[data-role]').forEach((select) => {
        select.addEventListener('change', async () => {
          await guard(root, async () => {
            await api.updateUser(select.dataset.role, { role: select.value });
            await reload();
          });
        });
      });

      root.querySelectorAll('[data-toggle]').forEach((button) => {
        button.addEventListener('click', async () => {
          await guard(root, async () => {
            await api.updateUser(button.dataset.toggle,
                                 { is_active: button.dataset.active !== 'true' });
            await reload();
          });
        });
      });

      root.querySelectorAll('[data-reset]').forEach((button) => {
        button.addEventListener('click', async () => {
          const values = await openDialog({
            title: `Reset the password for ${button.dataset.username}`,
            submitLabel: 'Reset password',
            validate: passwordProblem,
            fields: [
              { name: 'password', label: 'New password', type: 'password',
                autocomplete: 'new-password', minlength: MIN_PASSWORD,
                hint: `At least ${MIN_PASSWORD} characters. They will have to change it at next login.` },
              { name: 'confirm', label: 'Repeat the new password', type: 'password',
                autocomplete: 'new-password' },
            ],
          });
          if (!values) return;
          await guard(root, async () => {
            await api.resetPassword(button.dataset.reset, values.password);
            await reload();
          });
        });
      });

      root.querySelector('#user-add').addEventListener('click', async () => {
        const values = await openDialog({
          title: 'Add an account',
          submitLabel: 'Create account',
          validate: (v) => (!v.username.trim() ? 'A username is required.' : passwordProblem(v)),
          fields: [
            { name: 'username', label: 'Username', autocomplete: 'off' },
            { name: 'password', label: 'Temporary password', type: 'password',
              autocomplete: 'new-password', minlength: MIN_PASSWORD,
              hint: `At least ${MIN_PASSWORD} characters. They will have to change it at first login.` },
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
          await reload();
        });
      });
    });
  },
};

/* ------------------------------------------------------------------ account */

export const account = {
  title: 'Account',
  async render(root, ctx) {
    root.innerHTML = '<div class="loading">Loading…</div>';
    await guard(root, async () => {
      const sessions = await api.mySessions();
      root.innerHTML = `
        <section class="panel narrow">
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
            <div class="form-actions">
              <button type="submit" class="primary">Change password</button>
            </div>
            <p class="caption">Changing your password signs out every other session for
            this account — a password change is what you do when you think a credential
            has been taken.</p>
          </form>
          <div id="pw-result"></div>
        </section>

        <section class="panel">
          <header><h3>Where you are signed in</h3></header>
          ${table(`<table>
            <thead><tr><th>Started</th><th>Last seen</th><th>Address</th><th>Browser</th></tr></thead>
            <tbody>${sessions.map((s) => `<tr>
              <td>${fmtDateTime(s.created_at)}</td>
              <td>${fmtDateTime(s.last_seen_at)}</td>
              <td class="mono">${escapeHtml(s.ip || '—')}</td>
              <td class="muted small">${escapeHtml((s.user_agent || '').slice(0, 60))}</td>
            </tr>`).join('')}</tbody></table>`)}
        </section>`;

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
          return;
        }
        try {
          await api.changePassword(
            root.querySelector('#pw-current').value,
            root.querySelector('#pw-new').value);
          result.innerHTML = '<div class="ok-box">Password changed.</div>';
          ctx.onPasswordChanged?.();
        } catch (error) {
          result.innerHTML = errorBox(error);
        }
      });
    });
  },
};
