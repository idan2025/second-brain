/* brain UI - 2D force graph (d3) */
(function () {
  'use strict';
  const B = window.Brain;
  const SYM = { circle: d3.symbolCircle, square: d3.symbolSquare, diamond: d3.symbolDiamond, triangle: d3.symbolTriangle, ring: d3.symbolCircle };
  let svg, g, linkSel, nodeSel, labelSel, ringSel, sim, zoom, k = 1, labelsAll = false, timer = null, pending = false;
  const stage = document.getElementById('stage'), tip = document.getElementById('tip');

  function size() { const r = stage.getBoundingClientRect(); return { w: r.width, h: r.height }; }
  function radius(n) { return 4.5 + Math.sqrt(n.degree) * 2.4; }
  function symbolPath(n) {
    const kind = B.shapeOf(n.type), r = radius(n), area = Math.PI * r * r * (kind === 'circle' ? 1 : 1.25);
    let d = d3.symbol().type(SYM[kind]).size(area)();
    if (kind === 'ring') { const ri = r * .45; d += `M0,${-ri}A${ri},${ri},0,1,0,0,${ri}A${ri},${ri},0,1,0,0,${-ri}Z`; }
    return d;
  }

  const view = {
    init() {
      const { w, h } = size();
      svg = d3.select('#stage').insert('svg', ':first-child').attr('role', 'img').attr('aria-label', 'graph of memory notes');
      g = svg.append('g');
      zoom = d3.zoom().scaleExtent([.15, 8]).on('zoom', e => { g.attr('transform', e.transform); if (Math.abs(e.transform.k - k) > .05) { k = e.transform.k; refreshLabels(); } });
      svg.call(zoom).on('dblclick.zoom', null);
      svg.on('click', e => { if (e.target === svg.node()) { B.hovered = null; hideTip(); if (B.selected) B.close(); } });

      const nodes = B.nodes, links = B.links;
      sim = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(d => d.id).distance(l => 50 + 10 * Math.min(4, Math.max(l.source.degree, l.target.degree))).strength(.5))
        .force('charge', d3.forceManyBody().strength(d => -170 - d.degree * 18).distanceMax(700))
        .force('x', d3.forceX(w / 2).strength(.045)).force('y', d3.forceY(h / 2).strength(.055))
        .force('collide', d3.forceCollide(d => radius(d) + 6).iterations(2));
      // projects pull together a little, so the clusters read as projects
      const centers = new Map(B.projects.map((p, i) => { const a = i / Math.max(1, B.projects.length) * Math.PI * 2; return [p, [w / 2 + Math.cos(a) * w * .22, h / 2 + Math.sin(a) * h * .22]]; }));
      sim.force('px', d3.forceX(d => centers.get(d.project)[0]).strength(.06)).force('py', d3.forceY(d => centers.get(d.project)[1]).strength(.06));

      linkSel = g.append('g').attr('class', 'links').selectAll('line').data(links).join('line');
      ringSel = g.append('g').selectAll('circle').data(nodes).join('circle').attr('class', 'ring').attr('r', 0).attr('opacity', 0);
      nodeSel = g.append('g').selectAll('g').data(nodes).join('g').attr('class', 'node').attr('tabindex', -1);
      nodeSel.append('path').attr('d', symbolPath).attr('fill', d => B.color(d.project)).attr('fill-rule', 'evenodd');
      nodeSel.on('click', (e, d) => { e.stopPropagation(); B.open(d.id); })
        .on('pointerenter', (e, d) => { B.hovered = d.id; showTip(e, d); view.refresh(); })
        .on('pointermove', (e, d) => moveTip(e))
        .on('pointerleave', () => { B.hovered = null; hideTip(); view.refresh(); })
        .call(d3.drag().on('start', (e, d) => { if (!e.active) sim.alphaTarget(.25).restart(); d.fx = d.x; d.fy = d.y; })
          .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; hideTip(); }).on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = d.fy = null; }));
      labelSel = g.append('g').selectAll('text').data(nodes).join('text').text(d => d.name).attr('dy', d => radius(d) + 11).attr('text-anchor', 'middle');

      sim.on('tick', () => {
        linkSel.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
        nodeSel.attr('transform', d => `translate(${d.x},${d.y})`); ringSel.attr('cx', d => d.x).attr('cy', d => d.y);
        labelSel.attr('x', d => d.x).attr('y', d => d.y);
      });
      // settle a bit before the first paint, then fit
      for (let i = 0; i < 120; i++) sim.tick();
      sim.alpha(.35).restart();
      setTimeout(() => view.fit(0), 50); setTimeout(() => view.fit(600), 1600);
      addEventListener('resize', () => { const { w, h } = size(); sim.force('x').x(w / 2); sim.force('y').y(h / 2); });
      tools(); view.refresh(); ensureTimer();
    },
    refresh() { if (pending) return; pending = true; requestAnimationFrame(() => { pending = false; paintState(); }); },
    focus(id, ms) {
      const n = B.byId.get(id); if (!n || n.x == null) return; const { w, h } = size(); const s = Math.max(1.4, Math.min(2.2, k));
      svg.transition().duration(B.reduced ? 0 : (ms == null ? 650 : ms)).ease(d3.easeCubicOut)
        .call(zoom.transform, d3.zoomIdentity.translate(w / 2 - n.x * s, h / 2 - n.y * s).scale(s));
    },
    fit(ms) {
      const vis = B.nodes.filter(n => !B.dim(n) && n.x != null); if (!vis.length) return;
      const x0 = d3.min(vis, n => n.x), x1 = d3.max(vis, n => n.x), y0 = d3.min(vis, n => n.y), y1 = d3.max(vis, n => n.y);
      const { w, h } = size(); const s = Math.max(.2, Math.min(2.5, .82 / Math.max((x1 - x0 + 80) / w, (y1 - y0 + 80) / h)));
      svg.transition().duration(B.reduced ? 0 : (ms == null ? 600 : ms)).ease(d3.easeCubicOut)
        .call(zoom.transform, d3.zoomIdentity.translate(w / 2 - (x0 + x1) / 2 * s, h / 2 - (y0 + y1) / 2 * s + 10).scale(s));
    },
  };

  function tools() {
    const box = document.getElementById('tools');
    const mk = (title, path, on) => { const b = document.createElement('button'); b.className = 'tool'; b.title = title; b.setAttribute('aria-label', title); b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`; b.onclick = on; box.append(b); return b; };
    mk('Fit to view (f)', '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', () => view.fit());
    const lb = mk('Show every label', '<path d="M4 7h16M4 12h10M4 17h16"/>', () => { labelsAll = !labelsAll; lb.setAttribute('aria-pressed', String(labelsAll)); refreshLabels(); });
    lb.setAttribute('aria-pressed', 'false');
  }

  function showTip(e, d) {
    tip.textContent = '';
    const b = document.createElement('b'); b.textContent = d.name; tip.append(b);
    const m = document.createElement('div'); m.className = 'meta'; const i = document.createElement('i'); i.style.setProperty('--c', B.color(d.project));
    m.append(i, `${d.project} · ${d.type || 'untyped'} · ${d.degree} link${d.degree === 1 ? '' : 's'} · edited ${B.ago(d.mtime)}`); tip.append(m);
    if (d.description) { const p = document.createElement('p'); p.textContent = d.description; tip.append(p); }
    tip.classList.add('on'); moveTip(e);
  }
  function moveTip(e) {
    const r = stage.getBoundingClientRect(); let x = e.clientX - r.left + 14, y = e.clientY - r.top + 14;
    if (x + 310 > r.width) x = Math.max(4, e.clientX - r.left - 314); if (y + tip.offsetHeight + 8 > r.height) y = Math.max(4, e.clientY - r.top - tip.offsetHeight - 10);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function hideTip() { tip.classList.remove('on'); }

  function paintState() {
    const hov = B.hovered || (B.selected && !B.query ? B.selected : null);
    const nb = hov ? B.neighbors(hov) : null;
    nodeSel.classed('hit', d => B.query && B.hits.has(d.id)).classed('sel', d => d.id === B.selected)
      .style('display', d => B.visible(d) ? null : 'none')
      .style('opacity', d => B.dim(d) ? .12 : (nb && d.id !== hov && !nb.has(d.id) ? .22 : 1));
    linkSel.style('display', l => B.visible(l.source) && B.visible(l.target) ? null : 'none')
      .style('opacity', l => (B.query && !(B.hits.has(l.source.id) && B.hits.has(l.target.id))) ? .06 : (nb && l.source.id !== hov && l.target.id !== hov ? .18 : 1));
    paintLinks(hov);
    refreshLabels();
  }
  function paintLinks(hov) {
    // one place decides a link's stroke: the recall glow wins, then the hovered node's edges
    linkSel.attr('stroke', l => { const h = Math.min(B.hot(l.source.id), B.hot(l.target.id)); if (h) return `rgba(255,179,71,${.25 + h * .7})`; return hov && (l.source.id === hov || l.target.id === hov) ? 'var(--accent-line)' : null; })
      .attr('stroke-width', l => { const h = Math.min(B.hot(l.source.id), B.hot(l.target.id)); if (h) return 1 + h * 2.2; return hov && (l.source.id === hov || l.target.id === hov) ? 1.6 : null; });
  }
  function refreshLabels() {
    const hov = B.hovered || (B.selected && !B.query ? B.selected : null); const nb = hov ? B.neighbors(hov) : null;
    labelSel.style('display', d => {
      if (!B.visible(d) || B.dim(d)) return 'none';
      if (labelsAll || d.id === hov || (nb && nb.has(d.id)) || (B.query && B.hits.has(d.id)) || B.hot(d.id) > 0 || d.id === B.selected) return null;
      if (nb) return 'none';
      return (k >= 1.15 || (k >= .7 && d.degree >= 3) || d.degree >= 6) ? null : 'none';
    }).classed('hl', d => d.id === hov || d.id === B.selected || (B.query && B.hits.has(d.id)) || B.hot(d.id) > 0)
      .style('font-size', Math.min(13, 10.5 / Math.sqrt(Math.max(.35, k))) + 'px')
      .style('opacity', d => nb && d.id !== hov && !nb.has(d.id) ? .25 : null);
  }
  function ensureTimer() {
    if (timer) return;
    timer = d3.timer(() => {
      const any = B.anyHot();
      ringSel.attr('r', d => { const h = B.hot(d.id); if (!h) return 0; const pulse = B.reduced ? 0 : Math.sin(performance.now() / 260) * 1.5; return radius(d) + 4 + (1 - h) * 10 + pulse; })
        .attr('opacity', d => B.hot(d.id) * .9).attr('stroke-width', d => 1 + B.hot(d.id) * 2);
      nodeSel.select('path').attr('stroke', d => B.hot(d.id) ? 'var(--accent)' : null).attr('stroke-width', d => B.hot(d.id) ? 1.5 + B.hot(d.id) * 1.5 : null);
      paintLinks(B.hovered || (B.selected && !B.query ? B.selected : null));
      if (!any) { timer.stop(); timer = null; paintState(); }
    });
  }
  const origTouch = B.touch; B.touch = function (ids, kind, q, local) { origTouch(ids, kind, q, local); ensureTimer(); refreshLabels(); };

  document.addEventListener('DOMContentLoaded', () => B.boot(view));
})();
