/* brain UI - 3D force graph (3d-force-graph / three.js) */
(function () {
  'use strict';
  const B = window.Brain;
  let G, auto = !B.reduced, timer = null, pending = false, lastHot = false;
  const stage = document.getElementById('stage');
  const BG = '#0b0d11', DIM = '#262b35', ACCENT = '#ffb347', EDGE = 'rgba(239,232,220,0.16)';

  function hex(c, a) { return c + Math.round(Math.max(0, Math.min(1, a)) * 255).toString(16).padStart(2, '0'); }
  function mix(c, t) { // c hex -> toward accent by t
    const p = x => parseInt(x, 16), r = p(c.slice(1, 3)), g = p(c.slice(3, 5)), b = p(c.slice(5, 7));
    const R = 255, Gg = 179, Bb = 71; const q = v => Math.round(v).toString(16).padStart(2, '0');
    return '#' + q(r + (R - r) * t) + q(g + (Gg - g) * t) + q(b + (Bb - b) * t);
  }
  function nodeColor(n) {
    if (B.dim(n)) return hex(DIM, .35);
    const h = B.hot(n.id); if (h > 0) return mix(B.color(n.project), .4 + h * .6);
    const hov = B.hovered || B.selected;
    if (hov) { if (n.id === hov) return '#ffffff'; const nb = B.neighbors(hov); return nb.has(n.id) ? B.color(n.project) : hex(B.color(n.project), .3); }
    if (B.query && B.hits.has(n.id)) return ACCENT;
    return B.color(n.project);
  }
  function hotLink(l) { return Math.min(B.hot(l.source.id || l.source), B.hot(l.target.id || l.target)); }
  function touches(l, id) { return (l.source.id || l.source) === id || (l.target.id || l.target) === id; }
  function linkColor(l) {
    const h = hotLink(l); if (h > 0) return hex(ACCENT, .35 + h * .65);
    const hov = B.hovered || B.selected; if (hov && touches(l, hov)) return hex(ACCENT, .55);
    if (B.query) return B.hits.has(l.source.id || l.source) && B.hits.has(l.target.id || l.target) ? hex(ACCENT, .5) : 'rgba(239,232,220,0.04)';
    return EDGE;
  }
  function linkWidth(l) { const h = hotLink(l); if (h > 0) return .6 + h * 2.2; const hov = B.hovered || B.selected; return hov && touches(l, hov) ? 1 : 0; }
  function label(n) {
    const e = B.esc; // 3d-force-graph puts this string in the tooltip as HTML, so every field is escaped first
    return `<div class="tip3"><b>${e(n.name)}</b><div class="meta">${e(n.project)} · ${e(n.type || 'untyped')} · ${n.degree} link${n.degree === 1 ? '' : 's'} · edited ${e(B.ago(n.mtime))}</div>${n.description ? `<p>${e(n.description)}</p>` : ''}</div>`;
  }

  const view = {
    init() {
      const host = document.createElement('div'); host.className = 'view'; stage.prepend(host);
      G = ForceGraph3D({ controlType: 'orbit', rendererConfig: { antialias: true, alpha: false } })(host)
        .graphData({ nodes: B.nodes, links: B.links }).backgroundColor(BG)
        .width(stage.clientWidth).height(stage.clientHeight)
        .showNavInfo(false)
        .nodeLabel(label).nodeVal(n => (1 + n.degree * .9) * (B.query && B.hits.has(n.id) ? 2.2 : 1)).nodeRelSize(6).nodeResolution(16).nodeOpacity(.95)
        .nodeColor(nodeColor).nodeVisibility(n => B.visible(n))
        .linkColor(linkColor).linkWidth(linkWidth).linkOpacity(.7).linkResolution(5)
        .linkVisibility(l => B.visible(l.source) && B.visible(l.target))
        .linkDirectionalParticles(l => (B.reduced ? 0 : (hotLink(l) > 0 ? 3 : 0))).linkDirectionalParticleWidth(1.6).linkDirectionalParticleColor(() => ACCENT).linkDirectionalParticleSpeed(.008)
        .onNodeHover(n => { B.hovered = n ? n.id : null; host.style.cursor = n ? 'pointer' : null; view.refresh(); })
        .onNodeClick(n => { B.open(n.id); view.focus(n.id); })
        .onBackgroundClick(() => { if (B.selected) B.close(); })
        .onEngineStop(() => { if (!view._fitted) { view._fitted = true; G.zoomToFit(B.reduced ? 0 : 800, 40); } });
      G.d3Force('charge').strength(-70); G.d3Force('link').distance(l => 40 + 8 * Math.min(4, Math.max(l.source.degree || 0, l.target.degree || 0)));
      const c = G.controls(); c.autoRotate = auto; c.autoRotateSpeed = .5; c.enableDamping = true; c.dampingFactor = .08;
      const stop = () => { auto = false; c.autoRotate = false; rot.setAttribute('aria-pressed', 'false'); };
      ['pointerdown', 'wheel', 'touchstart'].forEach(e => host.addEventListener(e, stop, { passive: true }));
      G.scene().fog = null;
      setTimeout(() => { if (!view._fitted) { view._fitted = true; G.zoomToFit(B.reduced ? 0 : 800, 20); } }, 1800);
      addEventListener('resize', () => G.width(stage.clientWidth).height(stage.clientHeight));
      const box = document.getElementById('tools');
      const mk = (title, path, on) => { const b = document.createElement('button'); b.className = 'tool'; b.title = title; b.setAttribute('aria-label', title); b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`; b.onclick = on; box.append(b); return b; };
      mk('Fit to view (f)', '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', () => view.fit());
      const rot = mk('Auto-rotate', '<path d="M21 12a9 9 0 1 1-3-6.7M21 3v6h-6"/>', () => { auto = !auto; c.autoRotate = auto; rot.setAttribute('aria-pressed', String(auto)); });
      rot.setAttribute('aria-pressed', String(auto));
      ensureTimer();
    },
    refresh() { if (pending) return; pending = true; requestAnimationFrame(() => { pending = false; repaint(); }); },
    focus(id) {
      const n = B.byId.get(id); if (!n || n.x == null) return;
      const d = 90, r = 1 + d / (Math.hypot(n.x, n.y, n.z) || 1);
      G.cameraPosition({ x: n.x * r, y: n.y * r, z: n.z * r }, n, B.reduced ? 0 : 900);
    },
    fit() { G.zoomToFit(B.reduced ? 0 : 700, 40, n => !B.dim(n)); },
  };
  function repaint() {
    G.nodeColor(G.nodeColor()); G.nodeVal(G.nodeVal()); G.linkColor(G.linkColor()); G.linkWidth(G.linkWidth());
    G.nodeVisibility(G.nodeVisibility()); G.linkVisibility(G.linkVisibility()); G.linkDirectionalParticles(G.linkDirectionalParticles());
  }
  function ensureTimer() {
    if (timer) return;
    timer = setInterval(() => { const any = B.anyHot(); if (any || lastHot) repaint(); lastHot = any; }, 200);
  }
  document.addEventListener('DOMContentLoaded', () => B.boot(view));
})();
