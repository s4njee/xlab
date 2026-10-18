/* Views shared by the dashboard and the HTML reports: an experiment's analysis, a tree, an AI insight. */
(function () {
  'use strict';
  const C = window.XLabCharts, esc = C.esc, fmt = C.fmt;

  function improvement(e) {
    if (e.ratio == null) return null;
    return e.better === 'lower' ? 1 / e.ratio : e.ratio;
  }

  function metricName(result, m) {
    const d = (result.metrics || {})[m] || {};
    return (d.label || m) + (d.unit ? ` (${d.unit})` : '');
  }

  function experiment(el, result, opts) {
    opts = opts || {};
    if (!result || result.error) { el.innerHTML = `<p class="muted">${esc(result ? result.error : 'No analysis yet.')}</p>`; return; }
    const metric = opts.metric || result.primary;
    const positive = Object.entries(result.metrics).filter(([, m]) => m.positive).map(([k]) => k);
    const counts = result.counts || {};
    let html = `<div class="stack">`;
    html += `<div><h2>${esc(result.experiment)} <span class="pill">${esc(result.kind)}</span></h2>`;
    if (result.question) html += `<p class="prose"><b>Question.</b> ${esc(result.question)}</p>`;
    if (result.hypothesis) html += `<p class="prose secondary"><b>Hypothesis.</b> ${esc(result.hypothesis)}</p>`;
    html += `<p class="secondary">${result.blocks} complete blocks from ${result.runs.length} run(s) · ${counts.valid || 0} valid, ${counts.void || 0} void` +
      `${counts.awaiting ? `, ${counts.awaiting} awaiting` : ''} · control <b>${esc(result.control || '—')}</b> · δ ${Math.round(result.delta * 100)}% · verdicts need ${result.min_blocks} blocks` +
      `${result.kind === 'confirmatory' ? ' · Bonferroni-widened intervals' : result.kind === 'screening' ? ' · screening: verdicts are leads' : ''}</p></div>`;
    if (opts.selector !== false) {
      html += `<div class="row"><label class="secondary">Metric</label><select data-metric>${positive.map(m =>
        `<option value="${esc(m)}"${m === metric ? ' selected' : ''}>${esc(metricName(result, m))}${m === result.primary ? ' — primary' : ''}</option>`).join('')}</select></div>`;
    }
    const effects = result.effects.filter(e => e.metric === metric);
    if (effects.length) html += `<div class="card"><div data-forest></div></div>`;
    (result.pareto || []).forEach((f, i) => { html += `<div class="card"><div data-pareto="${i}"></div></div>`; });
    const abs = result.absolutes.filter(a => a.metric === metric);
    if (abs.length) {
      html += `<div class="card table-wrap"><h3>${esc(metricName(result, metric))} by arm</h3><table><thead><tr><th>Cell</th><th>Arm</th><th class="num">Geo. mean</th><th class="num">95% interval</th><th class="num">Median</th><th class="num">CV</th><th class="num">n</th></tr></thead><tbody>` +
        abs.map(a => `<tr><td>${esc(a.cell)}</td><td>${esc(a.arm)}${a.arm === result.control ? ' <span class="muted">control</span>' : ''}</td><td class="num">${fmt(a.gmean ?? a.mean)}</td>` +
          `<td class="num">[${fmt(a.gmean_low ?? a.mean_low, 3)}, ${fmt(a.gmean_high ?? a.mean_high, 3)}]</td><td class="num">${fmt(a.median)}</td><td class="num">${a.cv != null ? (a.cv * 100).toFixed(1) + '%' : '—'}</td><td class="num">${a.n}</td></tr>`).join('') +
        `</tbody></table></div>`;
    }
    if (Object.keys(result.void_reasons || {}).length) {
      html += `<div class="card"><h3>Void samples</h3><ul>${Object.entries(result.void_reasons).map(([r, n]) => `<li>${n}× ${esc(r)}</li>`).join('')}</ul></div>`;
    }
    html += `</div>`;
    el.innerHTML = html;
    const sel = el.querySelector('[data-metric]');
    if (sel) sel.addEventListener('change', () => experiment(el, result, { ...opts, metric: sel.value }));
    if (effects.length) {
      const rows = effects.map(e => ({ label: e.arm, sub: result.cells.length > 1 ? e.cell : '', group: e.cell, ratio: e.ratio, low: e.low, high: e.high,
        verdict: e.verdict, blocks: e.blocks, p: e.p, improvement: improvement(e) }));
      C.forest(el.querySelector('[data-forest]'), rows, { delta: result.delta, better: effects[0].better,
        title: `${metricName(result, metric)}: ratio to ${result.control}` });
    }
    (result.pareto || []).forEach((f, i) => {
      const mx = result.metrics[f.x] || {}, my = result.metrics[f.y] || {};
      C.pareto(el.querySelector(`[data-pareto="${i}"]`), f, { title: `Trade-off${result.cells.length > 1 ? ' · ' + f.cell : ''}`,
        xLabel: mx.label || f.x, yLabel: my.label || f.y, xUnit: mx.unit, yUnit: my.unit });
    });
  }

  function tree(el, view, opts) {
    opts = opts || {};
    if (!view || view.error) { el.innerHTML = `<p class="muted">${esc(view ? view.error : 'No tree.')}</p>`; return; }
    let html = `<div class="stack"><div><h2>${esc(view.id)} <span class="pill ${view.done ? 'good' : 'run'}">${view.done ? 'done' : 'in progress'}</span></h2>` +
      (view.question ? `<p class="prose">${esc(view.question)}</p>` : '') + `</div><div class="card"><div data-dag></div></div>` +
      `<div class="card table-wrap"><table><thead><tr><th>Node</th><th>State</th><th>Question</th><th>Runs</th><th>Decision</th></tr></thead><tbody>` +
      view.nodes.map(n => `<tr><td><b>${esc(n.id)}</b>${n.start ? ' <span class="muted">start</span>' : ''}</td><td>${statePill(n.state)}</td><td>${esc(n.question || '')}</td>` +
        `<td>${(n.runs || []).map(r => `<a href="#run/${esc(r.id)}">${esc(r.id.slice(-12))}</a> ${r.valid}/${r.planned}`).join('<br>')}</td>` +
        `<td>${n.decision ? esc(n.decision.label) + (n.decision.arms && n.decision.arms.length ? ` <span class="muted">(${esc(n.decision.arms.join(', '))})</span>` : '') : ''}</td></tr>`).join('') +
      `</tbody></table></div>`;
    if (view.decisions && view.decisions.length) {
      html += `<div class="card"><h3>Log</h3><ul class="list events">${view.decisions.slice().reverse().map(d =>
        `<li><time>${esc(d.utc.slice(5, 16).replace('T', ' '))}</time><span>${esc(d.node || '')} ${esc(d.text)}</span></li>`).join('')}</ul></div>`;
    }
    el.innerHTML = html + '</div>';
    C.dag(el.querySelector('[data-dag]'), view, { onSelect: opts.onSelect });
  }

  function statePill(s) {
    const cls = { running: 'run', queued: 'run', awaiting: 'warn', complete: 'good', decided: 'good', failed: 'bad', cancelled: 'bad',
      held: 'warn', halted: 'bad', stale: 'bad', idle: '', paused: 'warn', valid: 'good', void: 'bad' }[s] || '';
    return `<span class="pill ${cls}">${esc(s)}</span>`;
  }

  function insight(el, doc) {
    if (!doc || !doc.data) { el.innerHTML = '<p class="muted">No AI analysis yet.</p>'; return; }
    const d = doc.data, model = d.model && d.model.model;
    let html = `<div class="row"><h2>Analysis</h2><span class="badge-ai">${model ? 'written by ' + esc(model) : 'no model call'}</span>` +
      `<span class="muted">${esc((doc.start || '').slice(0, 16).replace('T', ' '))} – ${esc((doc.end || '').slice(0, 16).replace('T', ' '))} UTC</span></div>` +
      `<p class="prose">${esc(d.overview)}</p>`;
    (d.findings || []).forEach(f => {
      html += `<div class="finding"><b>${esc(f.title)}.</b> ${esc(f.finding)}<div class="secondary"><b>Evidence:</b> ${esc(f.evidence)}</div>` +
        `<div class="muted"><b>Caveat:</b> ${esc(f.caveat)} · ${f.experiments.map(x => `<a href="#experiment/${esc(x)}">${esc(x)}</a>`).join(', ')}</div></div>`;
    });
    if ((d.anomalies || []).length) html += `<h3>Anomalies</h3><ul>${d.anomalies.map(a => `<li><b>${esc(a.title)}.</b> ${esc(a.detail)}</li>`).join('')}</ul>`;
    if ((d.next_experiments || []).length) html += `<h3>Suggested next experiments</h3><ul>${d.next_experiments.map(n => `<li><b>${esc(n.question)}</b> ${esc(n.rationale)} <span class="secondary">${esc(n.design)}</span></li>`).join('')}</ul>`;
    if ((d.limitations || []).length) html += `<h3>Limitations</h3><ul>${d.limitations.map(l => `<li>${esc(l)}</li>`).join('')}</ul>`;
    el.innerHTML = html;
  }

  /* Sentinel runs: per cell, the control's value run by run, and the latest twin/other ratios. */
  function sentinel(el, s) {
    const runs = s.runs || [];
    const drift = Object.fromEntries((s.drift || []).map(d => [d.cell, d]));
    const cells = runs.length ? runs[runs.length - 1].cells.map(c => c.cell) : [];
    let html = `<div class="row"><h2>Sentinel ${esc(s.id)}</h2><span class="muted">every ${s.every_hours} h · ${runs.length} run(s)` +
      `${s.next_utc ? ' · next ' + esc(s.next_utc.slice(5, 16).replace('T', ' ')) : ''}</span></div>`;
    if (!runs.length) { el.innerHTML = html + '<p class="muted">The first run goes to the front of the queue as soon as the runner is free.</p>'; return; }
    cells.forEach((cell, i) => {
      const d = drift[cell];
      html += `<div data-sentinel="${i}"></div>`;
      if (d) html += `<p class="${d.flagged ? '' : 'muted'}">${d.flagged ? V_flag() : ''}Latest control at ${fmt(d.ratio_to_median, 4)}× its median over ${d.previous_runs} earlier runs (robust z ${fmt(d.z, 2)}).</p>`;
    });
    const last = runs[runs.length - 1];
    html += `<div class="table-wrap"><table><thead><tr><th>Cell</th><th class="num">Control CV</th><th>Ratios to ${esc(last.control)} (latest run)</th></tr></thead><tbody>` +
      last.cells.map(c => `<tr><td>${esc(c.cell)}</td><td class="num">${c.cv != null ? (c.cv * 100).toFixed(2) + '%' : '—'}</td><td>${c.effects.map(e =>
        `${esc(e.arm)} ${fmt(e.ratio, 4)}× <span class="muted">[${fmt(e.low, 3)}, ${fmt(e.high, 3)}]</span>`).join(' · ')}</td></tr>`).join('') + `</tbody></table></div>`;
    el.innerHTML = html;
    cells.forEach((cell, i) => {
      const d = drift[cell];
      const pts = runs.map((r, j) => {
        const c = r.cells.find(x => x.cell === cell) || {};
        return { t: r.ended, v: c.control_gmean, label: r.run, flagged: d && d.flagged && j === runs.length - 1,
          extra: c.cv != null ? `CV ${(c.cv * 100).toFixed(2)}%` : '' };
      });
      const earlier = pts.slice(0, -1).map(p => p.v).filter(v => v > 0).sort((a, b) => a - b);
      const median = earlier.length >= 3 ? earlier[Math.floor((earlier.length - 1) / 2)] : null;
      C.timeline(el.querySelector(`[data-sentinel="${i}"]`), pts, { title: `${last.control}: ${last.primary}${last.unit ? ' (' + last.unit + ')' : ''} · ${cell}`,
        metric: last.primary, reference: median });
    });
  }
  function V_flag() { return '<span class="pill bad">drift</span> '; }

  window.XLabViews = { experiment, tree, insight, sentinel, statePill, improvement };
})();
