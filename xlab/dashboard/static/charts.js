/* xlab charts: plain SVG, no dependencies. Shared by the dashboard and the HTML reports.
   Colours come from CSS tokens (style.css), so light and dark modes are handled there. */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fmt(v, digits) {
    if (v == null || Number.isNaN(v)) return '—';
    if (typeof v !== 'number') return String(v);
    const a = Math.abs(v);
    if (a !== 0 && (a >= 1e6 || a < 1e-3)) return v.toPrecision(3);
    if (a >= 1e4) return (v / 1e3).toFixed(1) + 'K';
    return Number(v.toPrecision(digits || 4)).toString();
  }

  // -- tooltip -----------------------------------------------------------------
  let tip = null;
  function tooltip() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'tooltip';
      document.body.appendChild(tip);
    }
    return tip;
  }
  function showTip(event, html) {
    const t = tooltip();
    t.innerHTML = html;
    t.style.display = 'block';
    const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
    let x = event.clientX + pad, y = event.clientY + pad;
    if (x + w > window.innerWidth - 8) x = event.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = event.clientY - h - pad;
    t.style.left = Math.max(8, x) + 'px';
    t.style.top = Math.max(8, y) + 'px';
  }
  function hideTip() { if (tip) tip.style.display = 'none'; }

  function bindTips(root, items) {
    root.querySelectorAll('[data-tip]').forEach(el => {
      const item = items[+el.getAttribute('data-tip')];
      el.addEventListener('pointermove', e => showTip(e, item));
      el.addEventListener('pointerleave', hideTip);
    });
  }

  // -- scales --------------------------------------------------------------------
  function linear(d0, d1, r0, r1) {
    const k = (r1 - r0) / ((d1 - d0) || 1);
    const f = v => r0 + (v - d0) * k;
    f.ticks = n => niceTicks(d0, d1, n);
    return f;
  }
  function logScale(d0, d1, r0, r1) {
    const l0 = Math.log(d0), l1 = Math.log(d1), k = (r1 - r0) / ((l1 - l0) || 1);
    const f = v => r0 + (Math.log(v) - l0) * k;
    f.ticks = () => logTicks(d0, d1);
    return f;
  }
  function niceTicks(a, b, n) {
    const span = b - a || 1, raw = span / (n || 5), mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= (n || 5)) || mag * 10;
    const out = [];
    for (let v = Math.ceil(a / step) * step; v <= b + step * 1e-9; v += step) out.push(+v.toPrecision(12));
    return out;
  }
  function logTicks(a, b) {
    const out = [];
    for (let e = Math.floor(Math.log10(a)); e <= Math.ceil(Math.log10(b)); e++) {
      for (const m of [1, 2, 5]) {
        const v = m * Math.pow(10, e);
        if (v >= a * 0.999 && v <= b * 1.001) out.push(v);
      }
    }
    return out.length >= 2 ? out : [a, b];
  }
  function ratioTicks(a, b) {
    const candidates = [0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.33, 0.5, 0.67, 0.8, 0.9, 0.95, 1, 1.05, 1.1, 1.25, 1.5, 2, 3, 4, 5, 10, 20, 50, 100];
    const inside = candidates.filter(v => v >= a && v <= b);
    return inside.length >= 2 ? inside : [a, 1, b].filter((v, i, s) => s.indexOf(v) === i);
  }

  function svg(width, height, body, cls) {
    return `<svg class="${cls || ''}" viewBox="0 0 ${width} ${height}" role="img" xmlns="${NS}">${body}</svg>`;
  }

  const VERDICT = {
    better: { color: 'var(--good)', pill: 'good' }, worse: { color: 'var(--critical)', pill: 'bad' },
    equivalent: { color: 'var(--text-secondary)', pill: '' }, inconclusive: { color: 'var(--text-muted)', pill: '' },
    insufficient: { color: 'var(--text-muted)', pill: '' }, lower: { color: 'var(--series-1)', pill: '' },
    higher: { color: 'var(--series-1)', pill: '' },
  };

  /* Forest plot: one row per comparison, ratio to control on a log axis with its interval.
     The neutral band is the region the experiment treats as "no meaningful difference". */
  function forest(el, rows, opts) {
    opts = opts || {};
    if (!rows.length) { el.innerHTML = '<p class="muted">No paired comparisons yet.</p>'; return; }
    const delta = opts.delta || 0.05, W = 760, rowH = 26, top = 46, left = 190, right = 250;
    const H = top + rows.length * rowH + 30;
    let lo = 1 / (1 + delta), hi = 1 + delta;
    rows.forEach(r => {
      [r.low, r.high, r.ratio].forEach(v => { if (v > 0) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    });
    lo /= 1.04; hi *= 1.04;
    const x = logScale(lo, hi, left, W - right);
    const items = [];
    let body = '';
    // band and gridlines
    body += `<rect class="band" x="${x(1 / (1 + delta))}" y="${top - 6}" width="${x(1 + delta) - x(1 / (1 + delta))}" height="${rows.length * rowH + 6}"/>`;
    let lastX = -1e9;
    const ticks = ratioTicks(lo, hi).filter(t => {
      if (t === 1 || x(t) - lastX >= 42) { lastX = x(t); return true; }
      return false;
    }).filter((t, i, all) => t === 1 || Math.abs(x(t) - x(1)) >= 42);
    ticks.forEach(t => {
      body += `<line class="gridline" x1="${x(t)}" x2="${x(t)}" y1="${top - 6}" y2="${top + rows.length * rowH}"/>`;
      body += `<text x="${x(t)}" y="${top + rows.length * rowH + 16}" text-anchor="middle" class="num">${fmt(t, 3)}×</text>`;
    });
    body += `<line class="ref" x1="${x(1)}" x2="${x(1)}" y1="${top - 10}" y2="${top + rows.length * rowH}"/>`;
    const dir = opts.better === 'lower' ? '← lower is better' : opts.better === 'higher' ? 'higher is better →' : '';
    body += `<text x="0" y="14" class="title">${esc(opts.title || 'Ratio to control')}</text>`;
    body += `<text x="0" y="30">${esc(dir)}${dir ? '  ·  ' : ''}shaded band: within ±${Math.round(delta * 100)}%, treated as no real difference</text>`;
    body += `<text x="${W - right + 12}" y="${top - 12}" class="muted">ratio [95% interval]</text><text x="${W - 4}" y="${top - 12}" text-anchor="end" class="muted">verdict</text>`;
    let lastGroup = null;
    rows.forEach((r, i) => {
      const y = top + i * rowH + rowH / 2;
      const v = VERDICT[r.verdict] || VERDICT.inconclusive;
      if (r.group && r.group !== lastGroup && i > 0) body += `<line class="gridline" x1="0" x2="${W}" y1="${y - rowH / 2}" y2="${y - rowH / 2}"/>`;
      lastGroup = r.group;
      body += `<text x="${left - 10}" y="${y + 4}" text-anchor="end" fill="var(--text-primary)">${esc(r.label)}</text>`;
      if (r.sub) body += `<text x="4" y="${y + 4}" class="muted">${esc(r.sub)}</text>`;
      if (r.ratio > 0) {
        if (r.low > 0 && r.high > 0) body += `<line x1="${x(r.low)}" x2="${x(r.high)}" y1="${y}" y2="${y}" stroke="${v.color}" stroke-width="2" stroke-linecap="round"/>`;
        body += `<circle class="mark" cx="${x(r.ratio)}" cy="${y}" r="5" fill="${v.color}" stroke="var(--surface-1)" stroke-width="2"/>`;
      }
      body += `<text x="${W - right + 12}" y="${y + 4}" class="num" fill="var(--text-primary)">${fmt(r.ratio, 3)}× <tspan fill="var(--text-muted)">[${fmt(r.low, 3)}, ${fmt(r.high, 3)}]</tspan></text>`;
      body += `<text x="${W - 4}" y="${y + 4}" text-anchor="end">${esc(r.verdict || '')}</text>`;
      items.push(`<b>${esc(r.label)}</b>${r.sub ? ' · ' + esc(r.sub) : ''}<br>ratio ${fmt(r.ratio)}× [${fmt(r.low)}, ${fmt(r.high)}]` +
        (r.improvement ? `<br><span class="k">improvement</span> ${fmt(r.improvement, 3)}×` : '') +
        `<br><span class="k">blocks</span> ${r.blocks ?? '—'} · <span class="k">p</span> ${fmt(r.p, 2)}<br><span class="k">verdict</span> ${esc(r.verdict)}`);
      body += `<rect class="hit" data-tip="${i}" x="0" y="${y - rowH / 2}" width="${W}" height="${rowH}"/>`;
    });
    el.innerHTML = `<div class="chart">${svg(W, H, body)}</div>`;
    bindTips(el, items);
  }

  /* Pareto scatter: every arm at its geometric means; the non-dominated arms are joined
     and labelled. Two colours only: on the front, and dominated. */
  function pareto(el, front, opts) {
    opts = opts || {};
    const pts = (front.points || []).filter(p => p.x > 0 && p.y > 0);
    if (pts.length < 2) { el.innerHTML = '<p class="muted">Not enough arms measured yet.</p>'; return; }
    const W = 760, H = 380, L = 64, R = 24, T = 30, B = 44;
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys), ymax = Math.max(...ys);
    const xlog = xmax / xmin > 20, ylog = ymax / ymin > 20;
    const padx = xlog ? 1.25 : (xmax - xmin) * 0.08 || xmax * 0.1, pady = ylog ? 1.25 : (ymax - ymin) * 0.1 || ymax * 0.1;
    const x = xlog ? logScale(xmin / padx, xmax * padx, L, W - R) : linear(Math.max(0, xmin - padx), xmax + padx, L, W - R);
    const y = ylog ? logScale(ymin / pady, ymax * pady, H - B, T) : linear(Math.max(0, ymin - pady), ymax + pady, H - B, T);
    let body = `<text x="${L}" y="16" class="title">${esc(opts.title || '')}</text>`;
    const xt = x.ticks(6), yt = y.ticks(5);
    yt.forEach(t => {
      body += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/>`;
      body += `<text x="${L - 8}" y="${y(t) + 4}" text-anchor="end" class="num">${fmt(t, 3)}</text>`;
    });
    xt.forEach(t => {
      body += `<text x="${x(t)}" y="${H - B + 16}" text-anchor="middle" class="num">${fmt(t, 3)}</text>`;
    });
    body += `<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>`;
    const arrow = b => b === 'higher' ? ' (higher is better)' : b === 'lower' ? ' (lower is better)' : '';
    body += `<text x="${(L + W - R) / 2}" y="${H - 8}" text-anchor="middle">${esc(opts.xLabel || front.x)}${esc(opts.xUnit ? ', ' + opts.xUnit : '')}${arrow(front.better_x)}${xlog ? ' · log scale' : ''}</text>`;
    body += `<text transform="translate(14 ${(T + H - B) / 2}) rotate(-90)" text-anchor="middle">${esc(opts.yLabel || front.y)}${esc(opts.yUnit ? ', ' + opts.yUnit : '')}${arrow(front.better_y)}${ylog ? ' · log' : ''}</text>`;
    const on = pts.filter(p => p.front).sort((a, b) => a.x - b.x);
    if (on.length > 1) {
      body += `<polyline points="${on.map(p => `${x(p.x)},${y(p.y)}`).join(' ')}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" opacity="0.6"/>`;
    }
    const items = [];
    const ordered = pts.slice().sort((a, b) => (a.front ? 1 : 0) - (b.front ? 1 : 0));
    ordered.forEach((p, i) => {
      const cx = x(p.x), cy = y(p.y);
      const fill = p.front ? 'var(--series-1)' : 'var(--text-muted)';
      body += `<circle class="mark" cx="${cx}" cy="${cy}" r="${p.front ? 5 : 4}" fill="${fill}" stroke="var(--surface-1)" stroke-width="2"/>`;
      body += `<circle class="hit" data-tip="${i}" cx="${cx}" cy="${cy}" r="11"/>`;
      items.push(`<b>${esc(p.arm)}</b>${p.front ? ' · on the front' : ' · dominated'}<br><span class="k">${esc(front.x)}</span> ${fmt(p.x)}` +
        (p.x_low ? ` [${fmt(p.x_low, 3)}, ${fmt(p.x_high, 3)}]` : '') + `<br><span class="k">${esc(front.y)}</span> ${fmt(p.y)}` +
        (p.y_low ? ` [${fmt(p.y_low, 3)}, ${fmt(p.y_high, 3)}]` : '') + `<br><span class="k">samples</span> ${p.n ?? '—'}`);
    });
    // direct labels on the front only, alternating above/below to limit collisions
    on.forEach((p, i) => {
      const cx = x(p.x), cy = y(p.y), dy = i % 2 ? 16 : -10;
      const anchor = cx > W - R - 80 ? 'end' : cx < L + 60 ? 'start' : 'middle';
      body += `<text x="${cx}" y="${cy + dy}" text-anchor="${anchor}" fill="var(--text-primary)" pointer-events="none">${esc(p.arm)}</text>`;
    });
    el.innerHTML = `<div class="legend"><span><i style="background:var(--series-1)"></i>On the Pareto front</span><span><i style="background:var(--text-muted)"></i>Dominated</span></div><div class="chart">${svg(W, H, body)}</div>`;
    bindTips(el, items);
  }

  /* Strip plot: one row per arm, each sample a dot; the newest sample of each arm is ringed. */
  function strip(el, rows, opts) {
    opts = opts || {};
    const values = rows.flatMap(r => r.values.map(v => v.v)).filter(v => v != null && v > 0);
    if (!values.length) { el.innerHTML = '<p class="muted">No samples yet.</p>'; return; }
    const W = 760, rowH = 24, T = 24, L = 150, R = 90, H = T + rows.length * rowH + 30;
    let lo = Math.min(...values), hi = Math.max(...values);
    const pad = (hi - lo) * 0.08 || hi * 0.05;
    const x = linear(Math.max(0, lo - pad), hi + pad, L, W - R);
    let body = `<text x="${L}" y="14" class="title">${esc(opts.title || '')}</text>`;
    x.ticks(6).forEach(t => {
      body += `<line class="gridline" x1="${x(t)}" x2="${x(t)}" y1="${T - 4}" y2="${T + rows.length * rowH}"/>`;
      body += `<text x="${x(t)}" y="${T + rows.length * rowH + 16}" text-anchor="middle" class="num">${fmt(t, 3)}</text>`;
    });
    const items = [];
    rows.forEach((r, i) => {
      const y = T + i * rowH + rowH / 2;
      body += `<text x="${L - 10}" y="${y + 4}" text-anchor="end" fill="var(--text-primary)">${esc(r.arm)}${r.control ? ' (control)' : ''}</text>`;
      const vals = r.values.filter(v => v.v > 0);
      vals.forEach((v, j) => {
        const latest = j === vals.length - 1;
        body += `<circle cx="${x(v.v)}" cy="${y}" r="${latest ? 5 : 3.5}" fill="${latest ? 'var(--series-1)' : 'var(--text-muted)'}" fill-opacity="${latest ? 1 : 0.55}" stroke="var(--surface-1)" stroke-width="${latest ? 2 : 1}"/>`;
      });
      const med = vals.length ? vals.map(v => v.v).sort((a, b) => a - b)[Math.floor((vals.length - 1) / 2)] : null;
      body += `<text x="${W - R + 10}" y="${y + 4}" class="num">${med != null ? 'median ' + fmt(med) : ''}</text>`;
      items.push(`<b>${esc(r.arm)}</b><br><span class="k">samples</span> ${vals.length}<br><span class="k">median</span> ${fmt(med)}` +
        (vals.length ? `<br><span class="k">latest</span> ${fmt(vals[vals.length - 1].v)}` : '') + (r.void ? `<br><span class="k">void</span> ${r.void}` : ''));
      body += `<rect class="hit" data-tip="${i}" x="0" y="${y - rowH / 2}" width="${W}" height="${rowH}"/>`;
    });
    if (opts.unit) body += `<text x="${(L + W - R) / 2}" y="${H - 2}" text-anchor="middle">${esc(opts.label || '')} (${esc(opts.unit)})</text>`;
    el.innerHTML = `<div class="chart">${svg(W, H, body)}</div>`;
    bindTips(el, items);
  }

  /* Decision tree as a layered graph: nodes by depth from the start nodes, edges with their rule labels. */
  function dag(el, view, opts) {
    opts = opts || {};
    const nodes = view.nodes || [];
    if (!nodes.length) { el.innerHTML = '<p class="muted">Empty tree.</p>'; return; }
    const depth = {};
    const out = {};
    (view.edges || []).forEach(e => { if (e.from !== e.to) (out[e.from] = out[e.from] || []).push(e.to); });
    const queue = nodes.filter(n => n.start).map(n => n.id);
    queue.forEach(id => { depth[id] = 0; });
    for (let guard = 0; queue.length && guard < 10000; guard++) {
      const id = queue.shift();
      (out[id] || []).forEach(t => {
        if (depth[t] == null || depth[t] < depth[id] + 1) {
          if (depth[id] + 1 < nodes.length) { depth[t] = depth[id] + 1; queue.push(t); }
        }
      });
    }
    nodes.forEach(n => { if (depth[n.id] == null) depth[n.id] = 0; });
    const layers = [];
    nodes.forEach(n => { (layers[depth[n.id]] = layers[depth[n.id]] || []).push(n); });
    const NW = 200, NH = 62, GX = 150, GY = 34;
    const W = Math.max(1, layers.length) * (NW + GX) - GX + 20;
    const H = Math.max(...layers.map(l => l.length)) * (NH + GY) - GY + 20;
    const pos = {};
    layers.forEach((layer, i) => {
      const total = layer.length * (NH + GY) - GY, off = (H - total) / 2;
      layer.forEach((n, j) => { pos[n.id] = { x: 10 + i * (NW + GX), y: off + j * (NH + GY) }; });
    });
    let body = '';
    const items = [];
    (view.edges || []).forEach(e => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      const cls = e.taken === true ? 'taken' : e.taken === false ? 'skipped' : '';
      let d, lx, ly;
      if (e.from === e.to) {
        d = `M${a.x + NW - 30},${a.y} C${a.x + NW - 10},${a.y - 30} ${a.x + NW + 30},${a.y - 10} ${a.x + NW},${a.y + 14}`;
        lx = a.x + NW + 4; ly = a.y - 14;
      } else {
        const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
        d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
        lx = mx; ly = (y1 + y2) / 2 - 4;
      }
      body += `<path class="edge ${cls}" d="${d}"${e.promote ? ' stroke-dasharray="4 3"' : ''}/>`;
      const label = (e.label || '').length > 24 ? e.label.slice(0, 23) + '…' : e.label || '';
      body += `<text class="edge-label ${cls}" x="${lx}" y="${ly}" text-anchor="middle">${esc(label)}</text>`;
    });
    nodes.forEach((n, i) => {
      const p = pos[n.id];
      const runs = n.runs || [];
      const last = runs[runs.length - 1];
      const progress = last && last.planned ? Math.min(1, last.valid / last.planned) : 0;
      const q = (n.question || n.spec || n.promote && ('confirms ' + n.promote) || '');
      const qt = q.length > 30 ? q.slice(0, 29) + '…' : q;
      body += `<g class="node ${esc(n.state)}" data-node="${esc(n.id)}" data-tip="${i}" transform="translate(${p.x} ${p.y})" style="cursor:pointer">`;
      body += `<rect width="${NW}" height="${NH}" rx="8"/>`;
      body += `<text class="id" x="10" y="18">${esc(n.id)}</text>`;
      body += `<text x="${NW - 10}" y="18" text-anchor="end">${esc(n.state)}</text>`;
      body += `<text x="10" y="35">${esc(qt)}</text>`;
      if (last) {
        body += `<rect x="10" y="${NH - 14}" width="${NW - 20}" height="5" rx="2.5" fill="var(--surface-2)"/>`;
        body += `<rect x="10" y="${NH - 14}" width="${(NW - 20) * progress}" height="5" rx="2.5" fill="var(--series-1)"/>`;
      }
      body += `</g>`;
      items.push(`<b>${esc(n.id)}</b> · ${esc(n.state)}${n.question ? '<br>' + esc(n.question) : ''}` +
        (last ? `<br><span class="k">run</span> ${esc(last.id)} · ${last.valid}/${last.planned ?? '?'} valid` : '') +
        (n.decision ? `<br><span class="k">decision</span> ${esc(n.decision.label)}` : ''));
    });
    el.innerHTML = `<div class="chart dag" style="overflow-x:auto">${svg(W, H, body, 'dag')}</div>`;
    const s = el.querySelector('svg');
    s.style.minWidth = Math.min(W, 1400) + 'px';
    bindTips(el, items);
    if (opts.onSelect) el.querySelectorAll('[data-node]').forEach(g => g.addEventListener('click', () => opts.onSelect(g.getAttribute('data-node'))));
  }

  /* Timeline: one value per run over time (a sentinel's control), with its interval and an
     optional reference value (the median of earlier runs). Flagged points are ringed in red. */
  function timeline(el, points, opts) {
    opts = opts || {};
    const pts = points.filter(p => p.v > 0 && p.t);
    if (!pts.length) { el.innerHTML = '<p class="muted">No finished runs yet.</p>'; return; }
    const W = 760, H = 220, L = 64, R = 24, T = 30, B = 34;
    const ts = pts.map(p => Date.parse(p.t));
    let t0 = Math.min(...ts), t1 = Math.max(...ts);
    const padT = Math.max(30e3, (t1 - t0) * 0.05);
    t0 -= padT; t1 += padT;
    const vals = pts.flatMap(p => [p.v, p.low, p.high]).filter(v => v > 0).concat(opts.reference ? [opts.reference] : []);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.15 || hi * 0.05;
    const x = linear(t0, t1, L, W - R), y = linear(Math.max(0, lo - pad), hi + pad, H - B, T);
    let body = `<text x="0" y="14" class="title">${esc(opts.title || '')}</text>`;
    y.ticks(4).forEach(t => {
      body += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/>`;
      body += `<text x="${L - 8}" y="${y(t) + 4}" text-anchor="end" class="num">${fmt(t, 3)}</text>`;
    });
    const span = t1 - t0, n = Math.min(6, Math.max(2, pts.length));
    for (let i = 0; i < n; i++) {
      const t = t0 + span * i / (n - 1), d = new Date(t);
      const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      const label = span > 2 * 86400e3 ? `${d.getMonth() + 1}/${d.getDate()}` : span < 1200e3 ? `${hm}:${String(d.getSeconds()).padStart(2, '0')}` : hm;
      body += `<text x="${x(t)}" y="${H - B + 16}" text-anchor="middle" class="num">${label}</text>`;
    }
    body += `<line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>`;
    if (opts.reference) {
      body += `<line class="ref" x1="${L}" x2="${W - R}" y1="${y(opts.reference)}" y2="${y(opts.reference)}"/>`;
      body += `<text x="${W - R}" y="${y(opts.reference) - 4}" text-anchor="end">median of earlier runs</text>`;
    }
    const sorted = pts.map((p, i) => ({ ...p, ms: ts[i] })).sort((a, b) => a.ms - b.ms);
    if (sorted.length > 1) body += `<polyline points="${sorted.map(p => `${x(p.ms)},${y(p.v)}`).join(' ')}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const items = [];
    sorted.forEach((p, i) => {
      if (p.low > 0 && p.high > 0) body += `<line x1="${x(p.ms)}" x2="${x(p.ms)}" y1="${y(p.low)}" y2="${y(p.high)}" stroke="var(--series-1)" stroke-width="2" stroke-opacity="0.35" stroke-linecap="round"/>`;
      body += `<circle cx="${x(p.ms)}" cy="${y(p.v)}" r="${p.flagged ? 6 : 4.5}" fill="var(--series-1)" stroke="${p.flagged ? 'var(--critical)' : 'var(--surface-1)'}" stroke-width="2"/>`;
      body += `<circle class="hit" data-tip="${i}" cx="${x(p.ms)}" cy="${y(p.v)}" r="11"/>`;
      items.push(`<b>${esc(p.label || '')}</b><br>${esc(p.t.slice(0, 16).replace('T', ' '))} UTC<br><span class="k">${esc(opts.metric || 'value')}</span> ${fmt(p.v)}` +
        (p.low ? ` [${fmt(p.low, 3)}, ${fmt(p.high, 3)}]` : '') + (p.extra ? `<br>${p.extra}` : '') + (p.flagged ? '<br><b>drift flagged</b>' : ''));
    });
    el.innerHTML = `<div class="chart">${svg(W, H, body)}</div>`;
    bindTips(el, items);
  }

  function verdictPill(v) {
    const m = VERDICT[v] || VERDICT.inconclusive;
    return `<span class="pill ${m.pill}">${esc(v)}</span>`;
  }

  window.XLabCharts = { forest, pareto, strip, dag, timeline, fmt, esc, verdictPill, showTip, hideTip };
})();
