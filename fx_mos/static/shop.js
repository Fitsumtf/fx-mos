/* Service shop board.
   Same API as the line board; different question. On a line the gate asks
   "may this move downstream". Here it asks "may this car go back to its owner". */

const SHOP = 'SVC-1';
const POLL_MS = 4000;

let selected = null;
let polling = null;

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* keep status */ }
    throw new Error(detail);
  }
  return res.json();
}

function toast(msg, bad = false) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.toggle('is-bad', bad);
  el.classList.add('is-on');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('is-on'), 4600);
}

const pct = (v) => `${Math.round((v || 0) * 100)}%`;

/* ---------------------------------------------------------------- render */

function renderHeader(d) {
  $('shop-code').textContent = d.line.code;
  $('shop-name').textContent = d.line.name;

  const t = d.totals;
  const busy = d.stations.filter((s) => s.units.length).length;
  $('bar-stats').innerHTML = [
    ['In the shop', t.in_process, false],
    ['Not releasable', t.held, t.held > 0],
    ['Open faults', t.open_ncs, t.open_ncs > 0],
    ['Released', t.completed, false],
    ['Bays busy', `${busy}/${d.stations.length}`, false],
  ].map(([k, v, alert]) => `
    <div class="stat ${alert ? 'is-alert' : ''}">
      <span class="stat-v">${v}</span><span class="stat-k">${k}</span>
    </div>`).join('');

  $('bays-note').textContent =
    `${busy} of ${d.stations.length} occupied · ${d.line.layout.toLowerCase()} layout`;
}

function renderBays(d) {
  $('bays').innerHTML = d.stations.map((s) => {
    const veh = s.units[0];
    const held = s.units.some((u) => u.status === 'HELD');
    const caps = (s.capabilities || [])
      .map((c) => `<span class="cap">${c.replace(/_/g, ' ')}</span>`).join('');

    const body = veh
      ? `<button class="veh ${veh.serial === selected ? 'is-selected' : ''}"
                 data-serial="${veh.serial}" data-status="${veh.status}" title="${veh.serial}">
           <span class="veh-serial">${veh.serial}</span>
           <span class="veh-job">${veh.model || ''}</span>
         </button>`
      : '<div class="bay-free">free</div>';

    return `
      <div class="bay-card ${held ? 'is-held' : veh ? 'is-busy' : ''}">
        <div class="bay-top">
          <span class="bay-id">${s.code}</span>
          <span class="bay-state">${held ? 'held' : veh ? 'working' : 'open'}</span>
        </div>
        <div class="caps">${caps}</div>
        ${body}
      </div>`;
  }).join('');

  document.querySelectorAll('.veh').forEach((el) =>
    el.addEventListener('click', () => select(el.dataset.serial)));
}

function renderHolds(ncs) {
  const c = $('hold-count');
  c.textContent = ncs.length;
  c.classList.toggle('is-alert', ncs.length > 0);

  if (!ncs.length) {
    $('holds').innerHTML =
      '<p class="empty">Nothing is held. Every vehicle in the shop can be released.</p>';
    return;
  }

  $('holds').innerHTML = ncs.map((n) => {
    const reasons = (n.detail && n.detail.reasons) || [];
    return `
      <div class="hold" data-severity="${n.severity}">
        <div class="hold-top">
          <span class="hold-code">${n.code}</span>
          <span class="hold-where">${n.station || '—'}</span>
        </div>
        <p class="hold-title">${n.title}</p>
        <span class="hold-unit">${n.unit}</span>
        ${reasons.length ? `<p class="hold-reason">${reasons.join('<br>')}</p>` : ''}
        <div class="hold-actions">
          <button class="btn btn-sm" data-nc="${n.code}" data-decision="REWORK">Redo the job</button>
          <button class="btn btn-sm" data-nc="${n.code}" data-decision="USE_AS_IS">Accept as is</button>
        </div>
      </div>`;
  }).join('');

  document.querySelectorAll('[data-nc]').forEach((el) =>
    el.addEventListener('click', () => disposition(el.dataset.nc, el.dataset.decision)));
}

function renderUtil(summary) {
  const measured = summary.stations.filter((s) => s.planned_seconds > 0);
  if (!measured.length) {
    $('util').innerHTML =
      '<p class="empty">No bay time recorded yet. Run a service day.</p>';
    $('busiest').textContent = 'no data yet';
    return;
  }

  const busiest = measured.reduce((a, b) => (b.run_seconds > a.run_seconds ? b : a));
  $('busiest').textContent = `${busiest.station} busiest`;
  const peak = busiest.run_seconds || 1;

  $('util').innerHTML = measured.map((s) => `
    <div class="oee-row ${s.station === busiest.station ? 'is-bottleneck' : ''}">
      <span class="oee-code">${s.station}</span>
      <div class="oee-bar" title="${(s.run_seconds / 3600).toFixed(1)} hours of work">
        <div class="oee-seg q" style="width:${(s.run_seconds / peak) * 100}%"></div>
      </div>
      <span class="oee-val">${(s.run_seconds / 3600).toFixed(1)}h</span>
    </div>`).join('')
    + `<p class="util-note">
         Hours of recorded work per bay over the window. A bay well below the
         others is either short of capability or short of jobs routed to it.
       </p>`;
}

function renderRecord(cert) {
  $('veh-serial').textContent = cert.serial;

  const torque = cert.measurements.filter(
    (m) => (m.uom || '').toLowerCase() === 'nm');
  const other = cert.measurements.filter(
    (m) => (m.uom || '').toLowerCase() !== 'nm');

  const stepFor = (station) =>
    (cert.steps.find((s) => s.station === station) || {}).operator || '';

  const line = (m) => `
    <div class="torque-line">
      <b>${m.name}</b>
      <span>
        <span class="${m.in_spec ? 'ok' : 'bad'}">${m.value}${m.uom}</span>
        ${m.lsl != null || m.usl != null
          ? `<span class="who">spec ${m.lsl ?? ''}${m.lsl != null && m.usl != null ? '–' : ''}${m.usl ?? ''}</span>`
          : ''}
      </span>
    </div>`;

  const parts = cert.components.length
    ? cert.components.map((c) => `
        <div class="torque-line">
          <b>${c.part_number}</b><span>${c.serial_or_lot || '—'}</span>
        </div>`).join('')
    : '<p class="empty">No parts recorded.</p>';

  const jobs = cert.steps.map((s) => `
    <div class="torque-line">
      <b>${s.name || s.step}</b>
      <span>
        <span class="${s.status === 'COMPLETE' ? 'ok' : 'bad'}">${s.status.toLowerCase()}</span>
        <span class="who">${s.operator || ''}</span>
      </span>
    </div>`).join('');

  $('record').innerHTML = `
    <p class="proof-lead">
      Customer says the work was not done. This is the answer, and it took
      no searching.
    </p>
    <dl class="kv">
      <dt>Vehicle</dt><dd>${cert.serial}</dd>
      <dt>Service</dt><dd>${cert.model}</dd>
      <dt>Status</dt><dd>${cert.status}</dd>
      <dt>Plan</dt><dd>${cert.flow.code} v${cert.flow.version}</dd>
      <dt>In</dt><dd>${(cert.started_at || '—').replace('T', ' ').slice(0, 16)}</dd>
      <dt>Out</dt><dd>${(cert.completed_at || '—').replace('T', ' ').slice(0, 16)}</dd>
    </dl>
    ${torque.length ? `<div class="rec-section">Torque records (${torque.length})</div>
      ${torque.map(line).join('')}` : ''}
    <div class="rec-section">Parts fitted (${cert.components.length})</div>
    ${parts}
    ${other.length ? `<div class="rec-section">Other measurements (${other.length})</div>
      ${other.map(line).join('')}` : ''}
    <div class="rec-section">Jobs and technician (${cert.steps.length})</div>
    ${jobs}`;
}

/* -------------------------------------------------------------- actions */

async function select(serial) {
  selected = serial;
  document.querySelectorAll('.veh').forEach((el) =>
    el.classList.toggle('is-selected', el.dataset.serial === serial));
  try {
    renderRecord(await api(`/api/units/${serial}/birth-certificate`));
  } catch (err) {
    toast(`Could not open ${serial}: ${err.message}`, true);
  }
}

async function disposition(code, decision) {
  const resolution = prompt(
    `Closing ${code} as ${decision.replace('_', ' ').toLowerCase()}.\n\n` +
    'What was found and what was done? This goes on the permanent record.');
  if (resolution === null) return;
  if (!resolution.trim()) { toast('A disposition needs a written resolution.', true); return; }
  try {
    const r = await api(`/api/ncs/${code}/disposition`, {
      method: 'POST',
      body: JSON.stringify({ decision, closed_by: 'service.manager', resolution }),
    });
    toast(`${code} closed. Vehicle is now ${r.unit_status.toLowerCase()}.`);
    refresh();
  } catch (err) {
    toast(`Could not close ${code}: ${err.message}`, true);
  }
}

async function runDay() {
  const b = $('btn-day');
  b.disabled = true;
  b.textContent = 'Running…';
  try {
    const r = await api('/api/simulate-shop', {
      method: 'POST',
      body: JSON.stringify({ vehicles: 40, fault_rate: 0.07 }),
    });
    toast(
      `${r.vehicles_in} in · ${r.released} released · ` +
      `${r.faults_caught} faults caught before the car left the bay.`);
    refresh();
  } catch (err) {
    toast(`Could not run the day: ${err.message}`, true);
  } finally {
    b.disabled = false;
    b.textContent = 'Run a service day';
  }
}

async function refresh() {
  try {
    const [d, ncs] = await Promise.all([
      api(`/api/dashboard?line_code=${SHOP}`),
      api('/api/ncs?open_only=true'),
    ]);
    renderHeader(d);
    renderBays(d);
    renderHolds(ncs);
    renderUtil(d.oee);
    if (selected) {
      api(`/api/units/${selected}/birth-certificate`).then(renderRecord).catch(() => {});
    }
  } catch (err) {
    toast(`Lost the shop: ${err.message}`, true);
  }
}

$('btn-day').addEventListener('click', runDay);
refresh();
polling = setInterval(refresh, POLL_MS);
document.addEventListener('visibilitychange', () => {
  clearInterval(polling);
  if (!document.hidden) { refresh(); polling = setInterval(refresh, POLL_MS); }
});
