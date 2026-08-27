/* FX MOS floor display.
   Polls one aggregate endpoint, renders the line, and lets a supervisor
   disposition a hold without leaving the board. */

const POLL_MS = 4000;
const ZONE_LABEL = {
  SUBFRAME: 'Sub-frame',
  PRE_MARRIAGE: 'Pre-marriage',
  MARRIAGE: 'Marriage',
  POST_MARRIAGE: 'Post-marriage',
  END_OF_LINE: 'End of line',
};

let selectedSerial = null;
let polling = null;

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* keep the status code */ }
    throw new Error(detail);
  }
  return res.json();
}

function toast(message, bad = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('is-bad', bad);
  el.classList.add('is-on');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('is-on'), 4200);
}

function pct(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

/* ------------------------------------------------------------- rendering */

function renderHeader(data) {
  $('line-code').textContent = data.line.code;
  $('line-name').textContent = data.line.name;

  const t = data.totals;
  $('bar-stats').innerHTML = [
    ['In process', t.in_process, false],
    ['Held', t.held, t.held > 0],
    ['Open NCs', t.open_ncs, t.open_ncs > 0],
    ['Signed off', t.completed, false],
    ['Line OEE', pct(data.oee.line_oee), false],
  ].map(([k, v, alert]) => `
    <div class="stat ${alert ? 'is-alert' : ''}">
      <span class="stat-v">${v}</span>
      <span class="stat-k">${k}</span>
    </div>`).join('');
}

function renderSpine(data) {
  // Zone headers span however many bays sit in that zone.
  const zones = [];
  for (const s of data.stations) {
    const last = zones[zones.length - 1];
    if (last && last.zone === s.zone) last.count += 1;
    else zones.push({ zone: s.zone, count: 1 });
  }
  $('zones').innerHTML = zones.map((z) => `
    <div class="zone" data-zone="${z.zone}" style="flex:${z.count} 1 0">
      ${ZONE_LABEL[z.zone] || z.zone}
    </div>`).join('');

  $('spine').innerHTML = data.stations.map((s) => {
    const held = s.units.some((u) => u.status === 'HELD');
    const oee = data.oee.stations.find((o) => o.station === s.code);
    const carriers = s.units.length
      ? s.units.map(carrier).join('')
      : '<div class="bay-empty">empty</div>';
    return `
      <div class="bay ${held ? 'is-held' : ''}">
        <div class="bay-code">${s.code}</div>
        <div class="bay-name">${s.name}</div>
        ${carriers}
        <div class="bay-meta">
          <span>${s.ideal_cycle_seconds}s</span>
          <span>${oee ? pct(oee.oee) : '—'}</span>
        </div>
      </div>`;
  }).join('');

  document.querySelectorAll('.carrier').forEach((el) => {
    el.addEventListener('click', () => selectUnit(el.dataset.serial));
  });
}

function carrier(unit) {
  return `
    <button class="carrier ${unit.serial === selectedSerial ? 'is-selected' : ''}"
            data-serial="${unit.serial}" data-status="${unit.status}"
            title="${unit.serial}">
      <span class="carrier-serial">${unit.serial}</span>
      <span class="carrier-state">${unit.status.replace('_', ' ')}</span>
    </button>`;
}

function renderHolds(ncs) {
  const count = $('hold-count');
  count.textContent = ncs.length;
  count.classList.toggle('is-alert', ncs.length > 0);

  if (!ncs.length) {
    $('holds').innerHTML = '<p class="empty">Nothing is held. The line is free to run.</p>';
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
          <button class="btn btn-sm" data-nc="${n.code}" data-decision="REWORK">Send to rework</button>
          <button class="btn btn-sm" data-nc="${n.code}" data-decision="USE_AS_IS">Use as is</button>
          <button class="btn btn-sm" data-nc="${n.code}" data-decision="SCRAP">Scrap</button>
        </div>
      </div>`;
  }).join('');

  document.querySelectorAll('[data-nc]').forEach((el) => {
    el.addEventListener('click', () => disposition(el.dataset.nc, el.dataset.decision));
  });
}

function renderOEE(summary) {
  const tag = $('bottleneck');
  if (summary.bottleneck) {
    tag.textContent = `${summary.bottleneck} · ${summary.bottleneck_reason || 'pacing'}`;
    tag.classList.add('is-alert');
  } else {
    tag.textContent = 'no data yet';
    tag.classList.remove('is-alert');
  }

  const measured = summary.stations.filter((s) => s.planned_seconds > 0);
  if (!measured.length) {
    $('oee').innerHTML = '<p class="empty">No station time recorded yet. Run some units.</p>';
    return;
  }

  // OEE is a product, so the bar is a cascade: start with all the planned time,
  // lose some to stoppages, lose more to slow cycles, lose the rest to scrap.
  // The darkest band is what is left, and its width equals the printed number.
  const rows = measured.map((s) => {
    const a = s.availability;
    const ap = a * s.performance;
    const apq = ap * s.quality;
    return `
      <div class="oee-row ${s.station === summary.bottleneck ? 'is-bottleneck' : ''}">
        <span class="oee-code">${s.station}</span>
        <div class="oee-bar"
             title="Availability ${pct(a)} → performance ${pct(s.performance)} → quality ${pct(s.quality)}">
          <div class="oee-seg a" style="width:${a * 100}%"></div>
          <div class="oee-seg p" style="width:${ap * 100}%"></div>
          <div class="oee-seg q" style="width:${apq * 100}%"></div>
        </div>
        <span class="oee-val">${pct(s.oee)}</span>
      </div>`;
  }).join('');

  const worst = measured.find((s) => s.station === summary.bottleneck);
  const note = worst && worst.top_loss
    ? `${worst.station} is the constraint. Biggest single loss is ${worst.top_loss.toLowerCase()}
       at ${Math.round(worst.losses[worst.top_loss] / 60)} min over the window.`
    : 'Not enough throughput to name a constraint yet.';

  $('oee').innerHTML = `
    <div class="legend">
      <span class="a">Ran</span><span class="p">At pace</span><span class="q">Good first time</span>
    </div>
    ${rows}
    <p class="oee-note">${note}</p>`;
}

function renderRecord(cert) {
  $('unit-serial').textContent = cert.serial;

  const measurements = cert.measurements.length
    ? cert.measurements.map((m) => `
        <div class="rec-line">
          <b>${m.name}</b>
          <span>
            <span class="${m.in_spec ? 'ok' : 'bad'}">${m.value}${m.uom}</span>
            ${m.interlock ? '<span class="rec-flag">LOCK</span>' : ''}
          </span>
        </div>`).join('')
    : '<p class="empty">No measurements recorded yet.</p>';

  const components = cert.components.length
    ? cert.components.map((c) => `
        <div class="rec-line">
          <b>${c.part_number}</b>
          <span>${c.serial_or_lot}</span>
        </div>`).join('')
    : '<p class="empty">No parts consumed yet.</p>';

  const steps = cert.steps.map((s) => `
    <div class="rec-line">
      <b>${s.station} ${s.step}${s.attempt > 1 ? ` (try ${s.attempt})` : ''}</b>
      <span class="${s.status === 'COMPLETE' ? 'ok' : 'bad'}">${s.status.toLowerCase()}</span>
    </div>`).join('');

  $('unit').innerHTML = `
    <div class="rec-section">Identity</div>
    <dl class="kv">
      <dt>Serial</dt><dd>${cert.serial}</dd>
      <dt>Model</dt><dd>${cert.model}</dd>
      <dt>Status</dt><dd>${cert.status}</dd>
      <dt>Routing</dt><dd>${cert.flow.code} v${cert.flow.version}</dd>
      <dt>Station</dt><dd>${cert.current_station || '—'}</dd>
    </dl>
    <div class="rec-section">Components (${cert.components.length})</div>
    ${components}
    <div class="rec-section">Measurements (${cert.measurements.length})</div>
    ${measurements}
    <div class="rec-section">Steps (${cert.steps.length})</div>
    ${steps}`;
}

/* ------------------------------------------------------------- behaviour */

async function selectUnit(serial) {
  selectedSerial = serial;
  document.querySelectorAll('.carrier').forEach((el) => {
    el.classList.toggle('is-selected', el.dataset.serial === serial);
  });
  try {
    renderRecord(await api(`/api/units/${serial}/birth-certificate`));
  } catch (err) {
    toast(`Could not open ${serial}: ${err.message}`, true);
  }
}

async function disposition(code, decision) {
  const resolution = prompt(
    `Closing ${code} as ${decision.replace('_', ' ').toLowerCase()}.\n\n` +
    'Write what was found and what was done. This goes on the permanent record.'
  );
  if (resolution === null) return;
  if (!resolution.trim()) {
    toast('A disposition needs a written resolution.', true);
    return;
  }
  try {
    const result = await api(`/api/ncs/${code}/disposition`, {
      method: 'POST',
      body: JSON.stringify({ decision, closed_by: 'supervisor', resolution }),
    });
    toast(`${code} closed as ${decision.toLowerCase()}. Unit is now ${result.unit_status.toLowerCase()}.`);
    refresh();
  } catch (err) {
    toast(`Could not close ${code}: ${err.message}`, true);
  }
}

async function releaseOrder() {
  const button = $('btn-order');
  button.disabled = true;
  try {
    const id = `WEB-${Date.now()}`;
    const order = await api('/api/orders', {
      method: 'POST',
      body: JSON.stringify({ erp_order_id: id, model_code: 'FXE1', quantity: 3, line_code: 'GA-1' }),
    });
    for (const serial of order.serials) {
      await api(`/api/units/${serial}/start`, { method: 'POST' }).catch(() => {});
    }
    toast(`Released ${order.serials.length} units. Serials allocated and queued at SF-10.`);
    refresh();
  } catch (err) {
    toast(`Order refused: ${err.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function runSimulation() {
  const button = $('btn-sim');
  button.disabled = true;
  button.textContent = 'Running…';
  try {
    const result = await api('/api/simulate', {
      method: 'POST',
      body: JSON.stringify({ units: 8, defect_rate: 0.18 }),
    });
    toast(
      `${result.released} released · ${result.signed_off} signed off · ` +
      `${result.scrapped} scrapped · ${result.open_ncs} still held.`
    );
    refresh();
  } catch (err) {
    toast(`Simulation failed: ${err.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = 'Run 8 units';
  }
}

async function refresh() {
  try {
    const [data, ncs] = await Promise.all([
      api('/api/dashboard'),
      api('/api/ncs?open_only=true'),
    ]);
    renderHeader(data);
    renderSpine(data);
    renderHolds(ncs);
    renderOEE(data.oee);
    if (selectedSerial) {
      api(`/api/units/${selectedSerial}/birth-certificate`).then(renderRecord).catch(() => {});
    }
  } catch (err) {
    toast(`Lost the line: ${err.message}`, true);
  }
}

$('btn-order').addEventListener('click', releaseOrder);
$('btn-sim').addEventListener('click', runSimulation);

refresh();
polling = setInterval(refresh, POLL_MS);
document.addEventListener('visibilitychange', () => {
  clearInterval(polling);
  if (!document.hidden) {
    refresh();
    polling = setInterval(refresh, POLL_MS);
  }
});
