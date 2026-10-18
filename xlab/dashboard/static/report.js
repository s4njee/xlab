/* Renders a saved report (the JSON embedded in the page) with the dashboard's charts. */
(function () {
  'use strict';
  const C = window.XLabCharts, V = window.XLabViews, esc = C.esc;
  const data = JSON.parse(document.getElementById('data').textContent);
  const root = document.getElementById('report');
  const w = data.window, s = data.summary, n = s.samples || {};
  const t = x => (x || '').slice(0, 16).replace('T', ' ');
  let html = `<header class="top"><div class="brand"><div class="mark">x</div><h1>${esc(data.lab)} report</h1></div>` +
    `<span class="muted">${t(w.start)} – ${t(w.end)} UTC</span><span class="spacer"></span><span class="muted">generated ${t(data.generated_utc)} on ${esc(data.host.hostname)}</span></header>`;
  html += `<section><div class="tiles">` +
    [['Valid samples', n.valid || 0, `${n.void || 0} void`], ['Runs sampled', s.runs_sampled, Object.entries(s.runs_ended || {}).map(([k, v]) => `${v} ${k}`).join(', ') || 'none ended'],
      ['Queue', s.queued, 'waiting'], ['Manual tasks', s.tasks_outstanding, 'outstanding'],
      ['Runner', esc((s.runner || {}).state || 'unknown'), esc(t((s.runner || {}).heartbeat))]]
      .map(([l, v, sub]) => `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`).join('') + `</div>`;
  if (Object.keys(data.void_reasons || {}).length) html += `<p class="secondary">Void: ${Object.entries(data.void_reasons).map(([r, c]) => `${c}× ${esc(r)}`).join('; ')}</p>`;
  html += `</section>`;
  if (data.insight) html += `<section class="card" data-insight></section>`;
  if (data.runs.length) {
    html += `<section class="card table-wrap"><h2>Runs</h2><table><thead><tr><th>Run</th><th>Experiment</th><th>State</th><th class="num">Valid / planned</th><th class="num">Void</th><th>Tree</th></tr></thead><tbody>` +
      data.runs.map(r => `<tr><td class="mono">${esc(r.id)}</td><td>${esc(r.experiment)}</td><td>${V.statePill(r.state)}</td><td class="num">${r.counts.valid || 0} / ${r.planned}</td>` +
        `<td class="num">${r.counts.void || 0}</td><td>${esc(r.tree ? r.tree + '/' + r.node : '')}</td></tr>`).join('') + `</tbody></table></section>`;
  }
  Object.keys(data.experiments).forEach((k, i) => { html += `<section data-exp="${i}"></section>`; });
  (data.trees || []).forEach((tr, i) => { html += `<section data-tree="${i}"></section>`; });
  (data.standing || []).forEach((st, i) => { html += `<section class="card" data-standing="${i}"></section>`; });
  const problems = (data.events || []).filter(e => ['failed', 'halt', 'warning', 'error', 'cancelled', 'block'].includes(e.kind));
  if (problems.length) html += `<section class="card"><h2>Problems</h2><ul class="list events">${problems.map(e => `<li><time>${t(e.utc)}</time><span>${esc(e.kind)}: ${esc(e.text)}</span></li>`).join('')}</ul></section>`;
  const h = data.host;
  html += `<section class="muted"><h3>Provenance</h3><p>xlab ${esc(h.xlab)}, Python ${esc(h.python)}, ${esc(h.system)} ${esc(h.release)} ${esc(h.machine)}${h.cpu ? ', ' + esc(h.cpu) : ''}. ` +
    `Every run directory keeps the exact spec, the plan with its seed, host and tool fingerprints, and each sample's record.</p></section>`;
  root.innerHTML = html;
  if (data.insight) V.insight(root.querySelector('[data-insight]'), { data: data.insight, start: data.insight_window.start, end: data.insight_window.end });
  Object.values(data.experiments).forEach((r, i) => V.experiment(root.querySelector(`[data-exp="${i}"]`), r));
  (data.trees || []).forEach((tr, i) => V.tree(root.querySelector(`[data-tree="${i}"]`), tr));
  (data.standing || []).forEach((st, i) => V.sentinel(root.querySelector(`[data-standing="${i}"]`), st));
})();
