/* xlab dashboard: polls nothing; refreshes when the server's event stream says the lab's data changed. */
(function () {
  'use strict';
  const C = window.XLabCharts, V = window.XLabViews, esc = C.esc, fmt = C.fmt;
  const main = document.getElementById('main');
  let state = null, route = [], busyTyping = false;
  const cache = {};

  // -- data -----------------------------------------------------------------------
  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  }
  async function post(url, body) {
    const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', 'x-xlab': '1' }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }
  function toast(text) {
    const t = document.getElementById('toast');
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove('show'), 2600);
  }

  async function refresh() {
    try {
      state = await getJSON('/api/state');
      document.getElementById('lab-name').textContent = state.lab.name;
      document.title = `${state.lab.name} · xlab`;
      header();
      if (!busyTyping) render();
    } catch (e) {
      document.getElementById('runner').innerHTML = `<span class="pill bad">dashboard offline</span>`;
    }
  }

  let pending = null;
  function connect() {
    const es = new EventSource('/api/stream');
    let lastRefresh = 0;
    es.addEventListener('version', () => {
      if (pending) return;
      const wait = Math.max(300, 2000 - (Date.now() - lastRefresh));
      pending = setTimeout(() => { pending = null; lastRefresh = Date.now(); refresh(); }, wait);
    });
    es.onerror = () => { document.getElementById('runner').innerHTML = `<span class="pill bad">reconnecting</span>`; };
  }

  // -- chrome ------------------------------------------------------------------------
  function header() {
    const r = state.runner || {};
    const label = state.halted_until ? 'halted' : state.paused && r.state !== 'running' ? 'paused' : r.state || 'not running';
    document.getElementById('runner').innerHTML = `${V.statePill(label)} <span class="muted">${esc(r.detail || '')}</span>`;
    const btn = document.getElementById('pause');
    btn.textContent = state.paused || state.halted_until ? 'Resume' : 'Pause';
    btn.onclick = async () => {
      await post('/api/control', { action: state.paused || state.halted_until ? 'resume' : 'pause' });
      toast(state.paused ? 'Runner resumed' : 'Runner will pause after the current run');
    };
    document.querySelectorAll('nav.tabs a').forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + (route[0] || 'overview')));
  }

  function ago(utc) {
    if (!utc) return '—';
    const s = (Date.now() - Date.parse(utc)) / 1000;
    if (s < 60) return `${Math.round(s)} s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${(s / 3600).toFixed(1)} h ago`;
    return `${(s / 86400).toFixed(1)} d ago`;
  }
  function when(utc) { return utc ? utc.slice(5, 16).replace('T', ' ') : '—'; }

  function runsTable(rows, empty) {
    if (!rows.length) return `<p class="muted">${esc(empty)}</p>`;
    return `<div class="table-wrap"><table><thead><tr><th>Run</th><th>Experiment</th><th>State</th><th class="num">Valid / planned</th><th class="num">Void</th><th>Tree</th><th>Created</th><th></th></tr></thead><tbody>` +
      rows.map(r => `<tr><td><a href="#run/${esc(r.id)}" class="mono">${esc(r.id.slice(0, 16))}…</a></td><td><a href="#experiment/${esc(r.experiment)}">${esc(r.experiment)}</a></td>` +
        `<td>${V.statePill(r.state)}${r.detail ? `<div class="muted">${esc(r.detail)}</div>` : ''}</td><td class="num">${r.valid} / ${r.planned ?? '?'}${r.awaiting ? ` <span class="muted">(${r.awaiting} to record)</span>` : ''}</td>` +
        `<td class="num">${r.void}</td><td>${esc(r.tree ? r.tree + '/' + r.node : r.lane === 'standing' ? 'sentinel' : '')}</td><td class="muted">${when(r.created)}</td>` +
        `<td>${['queued', 'running', 'awaiting'].includes(r.state) ? `<button data-cancel="${esc(r.id)}">Cancel</button>` : ''}</td></tr>`).join('') +
      `</tbody></table></div>`;
  }
  function bindCancel(root) {
    root.querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => {
      if (!confirm('Cancel this run? Samples already taken are kept.')) return;
      try { await post('/api/control', { action: 'cancel', run: b.dataset.cancel }); toast('Cancelled'); } catch (e) { toast(e.message); }
    });
  }

  // -- views ------------------------------------------------------------------------------
  function overview() {
    const cur = state.current, r = state.runner || {};
    const tasks = state.tasks.length, queued = state.queue.length;
    let html = `<div class="stack"><div class="tiles">` +
      tile('Runner', esc(r.state || 'not running'), r.heartbeat ? 'heartbeat ' + ago(r.heartbeat) : 'start it with xlab up') +
      tile('Current run', cur ? `${cur.run.valid} / ${cur.run.planned}` : '—', cur ? esc(cur.run.experiment) : 'nothing running') +
      tile('Queue', queued, queued ? esc(state.queue[0].experiment) + ' is next' : 'empty') +
      tile('Manual tasks', tasks, tasks ? 'waiting to be recorded' : 'none outstanding') +
      tile('Reports', state.reports.length, state.reports.length ? 'latest ' + ago(state.reports[0].end) : `every ${state.report_every_hours} h`) +
      `</div>`;
    if (cur) {
      const pct = cur.run.planned ? Math.min(100, 100 * cur.run.valid / cur.run.planned) : 0;
      html += `<div class="card"><div class="row"><h2>${esc(cur.run.experiment)}</h2>${V.statePill('running')}<span class="spacer"></span><a href="#run/${esc(cur.run.id)}">run details</a></div>` +
        (cur.run.question ? `<p class="prose secondary">${esc(cur.run.question)}</p>` : '') +
        `<div class="progress" title="${pct.toFixed(0)}%"><span style="width:${pct}%"></span></div><p class="muted">${esc(r.detail || '')} · ${cur.run.void} void</p>` +
        `<div data-strip></div></div>`;
      html += `<div class="card"><div data-current-forest></div></div>`;
    }
    html += `<div class="grid two"><div class="card"><h2>Queue</h2>${runsTable(state.queue, 'The queue is empty.')}` +
      `<h3>Add an experiment</h3><div class="row"><select data-spec>${state.specs.map(s => `<option>${esc(s)}</option>`).join('')}</select>` +
      `<label class="secondary"><input type="checkbox" data-front> next</label><button class="primary" data-enqueue ${state.specs.length ? '' : 'disabled'}>Queue</button></div></div>` +
      `<div class="card" data-insight></div></div>`;
    (state.standing || []).forEach((s, i) => { html += `<div class="card" data-standing="${i}"></div>`; });
    html += `<div class="card"><h2>Recent runs</h2>${runsTable(state.runs.slice(0, 15), 'No runs yet.')}</div></div>`;
    main.innerHTML = html;
    bindCancel(main);
    const enq = main.querySelector('[data-enqueue]');
    if (enq) enq.onclick = async () => {
      try {
        const out = await post('/api/enqueue', { spec: main.querySelector('[data-spec]').value, front: main.querySelector('[data-front]').checked });
        toast('Queued ' + out.run);
      } catch (e) { toast(e.message); }
    };
    V.insight(main.querySelector('[data-insight]'), state.insight);
    (state.standing || []).forEach((s, i) => V.sentinel(main.querySelector(`[data-standing="${i}"]`), s));
    if (cur) {
      const byArm = {};
      cur.arms.forEach(a => { byArm[a] = { arm: a, control: a === cur.control, values: [], void: 0 }; });
      cur.samples.forEach(s => {
        if (!byArm[s.arm]) return;
        if (s.state === 'valid') byArm[s.arm].values.push({ v: s.value });
        if (s.state === 'void') byArm[s.arm].void++;
      });
      const m = cur.metrics[cur.primary] || {};
      C.strip(main.querySelector('[data-strip]'), Object.values(byArm), { title: `Recent samples: ${m.label || cur.primary}`, unit: m.unit, label: m.label || cur.primary });
      const a = cur.analysis;
      const el = main.querySelector('[data-current-forest]');
      if (a && a.effects) {
        const rows = a.effects.filter(e => e.primary).map(e => ({ label: e.arm, sub: a.cells.length > 1 ? e.cell : '', group: e.cell, ratio: e.ratio, low: e.low, high: e.high, verdict: e.verdict, blocks: e.blocks, p: e.p, improvement: V.improvement(e) }));
        C.forest(el, rows, { delta: a.delta, better: rows.length ? a.effects[0].better : null, title: `This run so far: ${a.primary} vs ${a.control}` });
      } else el.innerHTML = '<p class="muted">No paired comparisons yet in this run.</p>';
    }
  }
  function tile(label, value, sub) {
    return `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  }

  async function experiments(id) {
    const names = state.experiments.map(e => e.experiment);
    id = id || names[0];
    main.innerHTML = `<div class="grid side"><div class="card"><h2>Experiments</h2><ul class="list">${state.experiments.map(e =>
      `<li class="${e.experiment === id ? 'active' : ''}" data-go="#experiment/${esc(e.experiment)}">${esc(e.experiment)}<div class="muted">${e.runs} run(s) · ${when(e.last)}</div></li>`).join('') ||
      '<li class="muted">No experiments yet.</li>'}</ul></div><div data-detail><p class="muted">${id ? 'Loading…' : 'Queue a spec to begin.'}</p></div></div>`;
    main.querySelectorAll('[data-go]').forEach(li => li.onclick = () => { location.hash = li.dataset.go; });
    if (!id) return;
    const show = result => {
      const el = main.querySelector('[data-detail]');
      if (!el) return;
      V.experiment(el, result, { metric: cache['metric:' + id] });
      const sel = el.querySelector('[data-metric]');
      if (sel) sel.addEventListener('change', () => { cache['metric:' + id] = sel.value; });
    };
    const last = cache['exp:' + id];
    if (last) show(last.result);           // keep the page steady while the new analysis loads
    if (last && last.version === state.version) return;
    const result = await getJSON('/api/experiment/' + encodeURIComponent(id)).catch(e => ({ error: e.message }));
    cache['exp:' + id] = { version: state.version, result };
    if ((location.hash.slice(1).split('/')[1] || state.experiments[0]?.experiment) === id) show(result);
  }

  function trees(id) {
    const list = state.trees;
    id = id || (list[0] && list[0].id);
    main.innerHTML = `<div class="grid side"><div class="card"><h2>Trees</h2><ul class="list">${list.map(t =>
      `<li class="${t.id === id ? 'active' : ''}" data-go="#tree/${esc(t.id)}">${esc(t.id)}<div class="muted">${t.done ? 'done' : 'in progress'}</div></li>`).join('') || '<li class="muted">No trees started.</li>'}</ul>` +
      `<h3>Start a tree</h3><div class="row"><select data-tree>${state.tree_files.map(t => `<option>${esc(t)}</option>`).join('')}</select>` +
      `<button class="primary" data-start ${state.tree_files.length ? '' : 'disabled'}>Start</button></div></div><div data-detail></div></div>`;
    main.querySelectorAll('[data-go]').forEach(li => li.onclick = () => { location.hash = li.dataset.go; });
    const start = main.querySelector('[data-start]');
    if (start) start.onclick = async () => {
      try { await post('/api/tree/start', { path: main.querySelector('[data-tree]').value }); toast('Tree started'); } catch (e) { toast(e.message); }
    };
    const view = list.find(t => t.id === id);
    const el = main.querySelector('[data-detail]');
    if (view) V.tree(el, view, { onSelect: node => {
      const n = view.nodes.find(x => x.id === node);
      if (n && n.runs.length) location.hash = '#run/' + n.runs[n.runs.length - 1].id;
    } });
    else el.innerHTML = '<div class="card"><p class="muted">Start a tree from trees/*.toml, or draft one with <code>xlab tree draft "question"</code>.</p></div>';
  }

  function tasks() {
    const list = state.tasks;
    if (!list.length) { main.innerHTML = `<div class="card"><h2>Manual tasks</h2><p class="muted">Nothing is waiting to be recorded. Experiments with <code>adapter = "manual"</code> appear here once queued.</p></div>`; return; }
    const byRun = {};
    list.forEach(t => (byRun[t.run_id] = byRun[t.run_id] || []).push(t));
    let html = `<div class="stack">`;
    for (const [run, items] of Object.entries(byRun)) {
      const metrics = (state.task_metrics || {})[run] || {};
      html += `<div class="card"><div class="row"><h2>${esc(items[0].experiment)}</h2><span class="muted mono">${esc(run)}</span></div>` +
        `<p class="secondary">Do these in the order shown: the order is randomized within each block so that time-of-day and drift do not favour any arm.</p>`;
      items.slice(0, 25).forEach(t => {
        html += `<form class="task" data-run="${esc(run)}" data-unit="${esc(t.unit)}"><div class="row"><b>#${t.position + 1}</b><span>replicate ${t.block + 1}${t.attempt ? ` (retry ${t.attempt})` : ''}</span>` +
          `<span class="pill">${esc(t.arm)}</span><span class="muted">${esc(t.cell)}</span></div>` +
          (t.instructions ? `<p class="prose">${esc(t.instructions)}</p>` : '') +
          `<div class="row">${Object.entries(metrics).map(([k, m]) => `<label>${esc(m.label || k)}${m.unit ? ` (${esc(m.unit)})` : ''} <input type="number" step="any" name="${esc(k)}"></label>`).join('')}` +
          `<input name="note" placeholder="note (optional)"><button class="primary" type="submit">Record</button>` +
          `<button type="button" data-void>Void…</button></div></form>`;
      });
      if (items.length > 25) html += `<p class="muted">${items.length - 25} more after these.</p>`;
      html += `</div>`;
    }
    main.innerHTML = html + '</div>';
    main.querySelectorAll('form.task').forEach(f => {
      f.addEventListener('focusin', () => { busyTyping = true; });
      f.addEventListener('focusout', () => { setTimeout(() => { busyTyping = !!main.querySelector('form.task :focus'); }, 50); });
      f.onsubmit = async ev => {
        ev.preventDefault();
        const metrics = {};
        f.querySelectorAll('input[type=number]').forEach(i => { if (i.value !== '') metrics[i.name] = Number(i.value); });
        try {
          await post('/api/record', { run: f.dataset.run, unit: f.dataset.unit, metrics, note: f.querySelector('[name=note]').value });
          busyTyping = false; toast('Recorded'); refresh();
        } catch (e) { toast(e.message); }
      };
      f.querySelector('[data-void]').onclick = async () => {
        const reason = prompt('Why can this sample not be used? (Its block will be re-planned.)');
        if (!reason) return;
        try { await post('/api/record', { run: f.dataset.run, unit: f.dataset.unit, void_reason: reason }); busyTyping = false; toast('Voided'); refresh(); } catch (e) { toast(e.message); }
      };
    });
  }

  function reports(id) {
    const list = state.reports;
    id = id || (list[0] && list[0].id);
    main.innerHTML = `<div class="grid side"><div class="card"><h2>Reports</h2><p class="muted">Every ${state.report_every_hours} h; AI analysis ${state.ai.provider && state.ai.provider !== 'none' ? `by ${esc(state.ai.provider)} every ${state.ai.every_hours} h` : 'is off'}.</p><ul class="list">${list.map(r =>
      `<li class="${r.id === id ? 'active' : ''}" data-go="#reports/${esc(r.id)}">${when(r.end)}<div class="muted">${(r.data && r.data.samples && r.data.samples.valid) || 0} valid samples${r.data && r.data.insight ? ' · AI' : ''}</div></li>`).join('') ||
      '<li class="muted">No reports yet.</li>'}</ul></div><div class="card" style="padding:0;overflow:hidden">${id ? `<iframe src="/api/report/${esc(id)}" style="width:100%;height:80vh;border:0;background:var(--page)"></iframe>` : '<p class="muted" style="padding:14px">The first report is written when xlab up has run for a moment; use <code>xlab report --now</code> to make one immediately.</p>'}</div></div>`;
    main.querySelectorAll('[data-go]').forEach(li => li.onclick = () => { location.hash = li.dataset.go; });
  }

  function events() {
    main.innerHTML = `<div class="card"><h2>Events</h2><ul class="list events">${state.events.map(e =>
      `<li><time>${when(e.utc)}</time><span class="pill ${['failed', 'error', 'halt', 'cancelled'].includes(e.kind) ? 'bad' : e.kind === 'warning' ? 'warn' : e.kind === 'complete' ? 'good' : ''}">${esc(e.kind)}</span><span>${esc(e.text)}${e.run_id ? ` <a href="#run/${esc(e.run_id)}" class="muted mono">${esc(e.run_id.slice(-10))}</a>` : ''}</span></li>`).join('')}</ul></div>`;
  }

  async function run(id) {
    main.innerHTML = '<p class="muted">Loading…</p>';
    const d = await getJSON('/api/run/' + encodeURIComponent(id)).catch(e => ({ error: e.message }));
    if (d.error) { main.innerHTML = `<div class="card"><p>${esc(d.error)}</p></div>`; return; }
    const r = d.run, p = d.plan;
    const env = d.env && d.env[d.env.length - 1];
    let html = `<div class="stack"><div class="card"><div class="row"><h2>${esc(r.experiment)}</h2>${V.statePill(r.state)}<span class="mono muted">${esc(r.id)}</span><span class="spacer"></span>` +
      `${['queued', 'running', 'awaiting'].includes(r.state) ? `<button data-cancel="${esc(r.id)}">Cancel</button>` : ''}</div>` +
      `<p class="secondary">${r.valid} valid / ${r.planned} planned · ${r.void} void · seed ${p.seed} · ${p.blocks} blocks × ${p.cells.length} cells × ${p.arms.length} arms · spec ${esc(p.spec_sha256.slice(0, 12))}` +
      `${r.tree ? ` · tree ${esc(r.tree)}/${esc(r.node)}` : ''}</p>${r.detail ? `<p class="muted">${esc(r.detail)}</p>` : ''}` +
      (env ? `<p class="muted">Host ${esc(env.fingerprint.host.hostname)} · ${esc(env.fingerprint.host.cpu || env.fingerprint.host.machine)} · fingerprint ${esc(env.sha)}` +
        `${Object.entries(env.fingerprint.adapter || {}).map(([k, v]) => ` · ${esc(k)}: ${esc(String(v).slice(0, 60))}`).join('')}</p>` : '') + `</div><div data-analysis></div>`;
    const metrics = Object.keys(p.metrics || {});
    html += `<div class="card table-wrap"><h3>Samples (${d.samples.length})</h3><table><thead><tr><th>Unit</th><th>State</th><th>Arm</th><th>Cell</th>${metrics.slice(0, 6).map(m => `<th class="num">${esc(m)}</th>`).join('')}<th>Ended</th><th>Note</th></tr></thead><tbody>` +
      d.samples.slice().reverse().slice(0, 400).map(s => `<tr><td class="mono">${esc(s.unit)}</td><td>${V.statePill(s.state)}</td><td>${esc(s.arm)}</td><td>${esc(s.cell)}</td>` +
        `${metrics.slice(0, 6).map(m => `<td class="num">${fmt(s.metrics[m])}</td>`).join('')}<td class="muted">${when(s.ended)}</td><td class="muted">${esc(s.void_reason || '')}</td></tr>`).join('') + `</tbody></table></div></div>`;
    main.innerHTML = html;
    bindCancel(main);
    V.experiment(main.querySelector('[data-analysis]'), d.analysis);
  }

  // -- routing --------------------------------------------------------------------------
  function render() {
    if (!state) return;
    route = (location.hash.slice(1) || 'overview').split('/');
    header();
    const [view, arg] = [route[0], route.slice(1).join('/')];
    if (view === 'experiment' || view === 'experiments') return experiments(arg);
    if (view === 'tree' || view === 'trees') return trees(arg);
    if (view === 'tasks') return tasks();
    if (view === 'reports') return reports(arg);
    if (view === 'events') return events();
    if (view === 'run') return run(arg);
    return overview();
  }
  window.addEventListener('hashchange', () => { busyTyping = false; render(); });
  document.getElementById('theme').onclick = () => {
    const root = document.documentElement, cur = root.getAttribute('data-theme');
    const dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
    try { localStorage.setItem('xlab-theme', root.getAttribute('data-theme')); } catch (e) { /* private mode */ }
  };
  try { const t = localStorage.getItem('xlab-theme'); if (t) document.documentElement.setAttribute('data-theme', t); } catch (e) { /* ignore */ }
  refresh().then(connect);
})();
