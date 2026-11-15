/* brain UI - shared: data, search, note panel, filters, activity feed, keyboard.
   No innerHTML with note content anywhere: every string from a note goes in via textContent. */
(function () {
  'use strict';
  const $ = (s, r) => (r || document).querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const PALETTE = ['#5fb3f5', '#7fd68a', '#e07be0', '#f0e06a', '#5fe0d0', '#ff8f8f', '#b89cff', '#9fdc5f', '#f49ac1', '#8fa8ff', '#d4a373', '#7cc7ff'];
  const HEAT_MS = 9000;
  const HL_A = '\u0001', HL_B = '\u0002';

  const B = window.Brain = {
    nodes: [], links: [], byId: new Map(), projects: [], types: ['user', 'feedback', 'project', 'reference'],
    typeOff: new Set(), projOff: new Set(), query: '', hits: new Map(), selected: null, hovered: null,
    heat: new Map(), view: null, reduced: REDUCED, palette: PALETTE,
    color(p) { const i = B.projects.indexOf(p); return PALETTE[(i < 0 ? 0 : i) % PALETTE.length]; },
    visible(n) { return !B.typeOff.has(n.type || 'untyped') && !B.projOff.has(n.project); },
    hot(id) { const t = B.heat.get(id); if (!t) return 0; const a = (performance.now() - t) / HEAT_MS; return a >= 1 ? 0 : a < 0 ? 1 : 1 - a; },
    anyHot() { const now = performance.now(); for (const t of B.heat.values()) if (now - t < HEAT_MS) return true; return false; },
    dim(n) { return !B.visible(n) || (B.query && !B.hits.has(n.id)); },
    neighbors(id) { const s = new Set(); for (const l of B.links) { const a = l.source.id || l.source, b = l.target.id || l.target; if (a === id) s.add(b); if (b === id) s.add(a); } return s; },
    esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]); },
    ago(ts) {
      if (!ts) return 'never'; const s = Math.max(0, Date.now() / 1000 - ts);
      if (s < 60) return 'just now'; if (s < 3600) return Math.round(s / 60) + 'm ago'; if (s < 86400) return Math.round(s / 3600) + 'h ago';
      if (s < 86400 * 30) return Math.round(s / 86400) + 'd ago'; return Math.round(s / 86400 / 30) + 'mo ago';
    },
    refresh() { if (B.view && B.view.refresh) B.view.refresh(); },
  };

  // ------------------------------------------------------------ load
  B.load = async function () {
    const r = await fetch('/graph', { cache: 'no-store' });
    if (!r.ok) throw new Error('graph ' + r.status);
    const data = await r.json();
    B.nodes = data.nodes; B.links = data.links; B.projects = data.projects; B.dangling = data.dangling || [];
    B.byId = new Map(B.nodes.map(n => [n.id, n]));
    const deg = new Map();
    B.links.forEach(l => { deg.set(l.source, (deg.get(l.source) || 0) + 1); deg.set(l.target, (deg.get(l.target) || 0) + 1); });
    B.nodes.forEach(n => { n.degree = deg.get(n.id) || 0; });
    const now = Date.now() / 1000;
    // what was recalled in the last few minutes glows at load, so a fresh page shows the trail
    B.nodes.forEach(n => { if (n.last_recall && now - n.last_recall < 300) B.heat.set(n.id, performance.now() - (now - n.last_recall) / 300 * HEAT_MS * .7); });
    renderStats(data); renderChips(); renderLegend();
    return data;
  };

  function renderStats(data) {
    const s = $('.stats'); if (!s) return; s.textContent = '';
    s.append(`${B.nodes.length} notes · ${B.links.length} links · ${B.projects.length} projects`);
    if (data.indexed_at) s.append(` · indexed ${B.ago(data.indexed_at)}`);
    if (data.warnings) { s.append(' · '); s.append(el('span', 'warn', `${data.warnings} with warnings`)); }
  }

  const SHAPES = { project: 'circle', reference: 'square', feedback: 'diamond', user: 'triangle', untyped: 'ring' };
  B.shapeOf = t => SHAPES[t || 'untyped'] || 'ring';
  function shapeSvg(kind, color) {
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '-6 -6 12 12');
    let d;
    if (kind === 'circle') d = 'M0,-5A5,5,0,1,1,0,5A5,5,0,1,1,0,-5Z';
    else if (kind === 'square') d = 'M-4.5,-4.5h9v9h-9z';
    else if (kind === 'diamond') d = 'M0,-5.5L5.5,0L0,5.5L-5.5,0Z';
    else if (kind === 'triangle') d = 'M0,-5.5L5.5,4.5H-5.5Z';
    else d = 'M0,-5A5,5,0,1,1,0,5A5,5,0,1,1,0,-5ZM0,-2.5A2.5,2.5,0,1,0,0,2.5A2.5,2.5,0,1,0,0,-2.5Z';
    const p = document.createElementNS(ns, 'path'); p.setAttribute('d', d); p.setAttribute('fill', color || 'currentColor'); p.setAttribute('fill-rule', 'evenodd'); svg.appendChild(p); return svg;
  }
  B.shapeSvg = shapeSvg;

  function renderChips() {
    const box = $('#chips'); if (!box) return; box.textContent = '';
    const counts = {}; B.nodes.forEach(n => { const t = n.type || 'untyped'; counts[t] = (counts[t] || 0) + 1; });
    const kinds = [...B.types.filter(t => counts[t]), ...(counts.untyped ? ['untyped'] : [])];
    const all = el('button', 'chip'); all.append('All notes'); all.setAttribute('aria-pressed', String(B.typeOff.size === 0));
    all.onclick = () => { B.typeOff.clear(); renderChips(); B.refresh(); };
    box.append(all);
    kinds.forEach(t => {
      const c = el('button', 'chip'); c.append(shapeSvg(B.shapeOf(t)), t, el('span', 'n', String(counts[t])));
      c.setAttribute('aria-pressed', String(!B.typeOff.has(t))); c.title = `${t}: ${counts[t]} notes (click to toggle, shift-click = only)`;
      c.onclick = e => {
        if (e.shiftKey) { B.typeOff = new Set(kinds.filter(k => k !== t)); }
        else if (B.typeOff.has(t)) B.typeOff.delete(t); else B.typeOff.add(t);
        if (B.typeOff.size === kinds.length) B.typeOff.clear();
        renderChips(); B.refresh();
      };
      box.append(c);
    });
  }

  function renderLegend() {
    const box = $('#legend'); if (!box) return; box.textContent = '';
    const counts = {}; B.nodes.forEach(n => counts[n.project] = (counts[n.project] || 0) + 1);
    B.projects.forEach(p => {
      const b = el('button', 'lg'); b.style.setProperty('--c', B.color(p)); b.append(el('i'), p, el('span', 'n', String(counts[p])));
      b.setAttribute('aria-pressed', String(!B.projOff.has(p))); b.title = `${p}: ${counts[p]} notes (click to toggle, shift-click = only)`;
      b.onclick = e => {
        if (e.shiftKey) B.projOff = new Set(B.projects.filter(q => q !== p));
        else if (B.projOff.has(p)) B.projOff.delete(p); else B.projOff.add(p);
        if (B.projOff.size === B.projects.length) B.projOff.clear();
        renderLegend(); B.refresh();
      };
      box.append(b);
    });
  }

  // ------------------------------------------------------------ search
  let searchTimer = null, lastQ = '', cur = -1, results = [];
  function marked(s) {
    // server marks matches with \x01 … \x02; turn them into <mark> safely
    const frag = document.createDocumentFragment();
    String(s || '').split(HL_A).forEach((part, i) => {
      if (i === 0) { frag.append(part); return; }
      const j = part.indexOf(HL_B);
      if (j < 0) { frag.append(part); return; }
      frag.append(el('mark', null, part.slice(0, j)), part.slice(j + 1));
    });
    return frag;
  }
  B.marked = marked;

  B.search = function (q) {
    B.query = q.trim(); $('.searchbox').classList.toggle('has', !!B.query);
    clearTimeout(searchTimer);
    if (!B.query) { B.hits = new Map(); results = []; cur = -1; $('#results').hidden = true; B.refresh(); return; }
    searchTimer = setTimeout(async () => {
      const q2 = B.query; if (q2 === lastQ) return; lastQ = q2;
      try {
        const r = await fetch('/search?q=' + encodeURIComponent(q2) + '&n=30&log=0');
        const data = await r.json();
        if (B.query !== q2) return;
        results = data.hits; B.hits = new Map(results.map((h, i) => [h.id, i]));
        renderResults(); B.refresh();
      } catch (e) { console.error(e); }
    }, 140);
  };

  function renderResults() {
    const sec = $('#results'), ol = $('ol', sec); sec.hidden = false; ol.textContent = ''; cur = -1;
    $('.cnt', sec).textContent = results.length ? `${results.length} hit${results.length === 1 ? '' : 's'}` : 'no match';
    results.forEach((h, i) => {
      const li = el('li'), b = el('button', 'res'); b.setAttribute('role', 'option'); b.dataset.i = i;
      const t = el('div', 't'); const dot = el('i'); dot.style.setProperty('--c', B.color(h.project));
      const name = el('b'); name.append(marked(h.name_hl)); name.title = h.id;
      t.append(dot, name, el('span', 'ty ' + (h.type || ''), h.type || '—')); b.append(t);
      const d = el('div', 'd'); d.append(marked(h.description_hl)); b.append(d);
      const w = el('div', 'w'); ['name', 'description', 'body'].forEach(f => w.append(el('span', h.why.includes(f) ? 'on' : '', f)));
      w.append(el('span', '', h.project)); if (h.mode === 'any') w.append(el('span', '', 'some words'));
      b.append(w);
      if (h.why.includes('body')) { const s = el('div', 's'); s.append(marked(h.snippet)); b.append(s); }
      b.onclick = () => { B.open(h.id); if (B.view && B.view.focus) B.view.focus(h.id); };
      b.onmouseenter = () => { B.hovered = h.id; B.refresh(); }; b.onmouseleave = () => { B.hovered = null; B.refresh(); };
      li.append(b); ol.append(li);
    });
  }
  function moveCur(d) {
    if (!results.length) return; cur = (cur + d + results.length) % results.length;
    const items = $('#results ol').children; [...items].forEach((li, i) => li.firstChild.classList.toggle('cur', i === cur));
    items[cur].scrollIntoView({ block: 'nearest' }); B.hovered = results[cur].id; B.refresh();
  }

  // ------------------------------------------------------------ note panel
  const INLINE = /\[\[([^\]\n]+)\]\]|\[([^\]\n]+)\]\(\.?\/?([A-Za-z0-9][A-Za-z0-9._-]*)\.md\)|`([^`\n]+)`/g;
  function resolveName(project, target) {
    const t = target.trim().toLowerCase();
    let best = null;
    for (const n of B.nodes) {
      const stem = n.id.split('/').slice(1).join('/').toLowerCase();
      if (n.name.toLowerCase() === t || stem === t) { if (n.project === project) return n; best = best || n; }
    }
    return best;
  }
  function renderBody(pre, note) {
    pre.textContent = '';
    note.body.split('\n').forEach((line, li) => {
      if (li) pre.append('\n');
      const isHead = /^#{1,6}\s/.test(line);
      const dest = isHead ? el('span', 'h') : pre;
      let last = 0, m; INLINE.lastIndex = 0;
      while ((m = INLINE.exec(line))) {
        dest.append(line.slice(last, m.index)); last = INLINE.lastIndex;
        if (m[4] != null) { dest.append(el('code', null, m[4])); continue; }
        const target = m[1] != null ? m[1] : m[3], label = m[1] != null ? `[[${m[1]}]]` : m[2];
        const n = resolveName(note.project, target);
        const b = el('button', 'wl' + (n ? '' : ' missing'), label);
        if (n) { b.title = n.description || n.id; b.onclick = () => { B.open(n.id); if (B.view && B.view.focus) B.view.focus(n.id); }; }
        else { b.title = 'no note with this name'; b.disabled = true; }
        dest.append(b);
      }
      dest.append(line.slice(last));
      if (isHead) pre.append(dest);
    });
  }
  function linkChips(box, ids, missing) {
    box.textContent = '';
    ids.forEach(id => {
      const n = B.byId.get(id); const b = el('button', 'lk' + (missing ? ' missing' : ''));
      if (!missing && n) { const i = el('i'); i.style.setProperty('--c', B.color(n.project)); b.append(i); }
      b.append(el('span', null, missing ? `[[${id}]]` : (n ? (n.project === B.byId.get(B.selected).project ? n.name : id) : id)));
      if (n) { b.title = n.description || id; b.onclick = () => { B.open(id); if (B.view && B.view.focus) B.view.focus(id); }; } else b.disabled = true;
      box.append(b);
    });
  }

  B.open = async function (id) {
    const n = B.byId.get(id); if (!n) return;
    B.selected = id; B.refresh();
    let note;
    try { const r = await fetch('/api/note/' + encodeURIComponent(id)); if (!r.ok) throw 0; note = await r.json(); }
    catch (e) { note = Object.assign({ body: '(could not load note)', links_out: [], links_in: [], missing: [], warn: [] }, n); }
    if (B.selected !== id) return;
    const sec = $('#note'); sec.hidden = false; $('#welcome').hidden = true;
    $('#note-title').textContent = n.name;
    const meta = $('#note-meta'); meta.textContent = '';
    const pj = el('span', 'pj'); const dot = el('i'); dot.style.setProperty('--c', B.color(n.project)); pj.append(dot, n.project); meta.append(pj);
    meta.append(el('span', 'ty ' + (n.type || ''), n.type || 'untyped'));
    meta.append(el('span', null, 'edited ' + B.ago(note.mtime)));
    meta.append(el('span', null, `${n.recalls || 0} recall${n.recalls === 1 ? '' : 's'}`));
    if (!note.listed) meta.append(el('span', null, 'not in MEMORY.md'));
    (note.warn || []).forEach(w => meta.append(el('span', 'warn', '⚠ ' + w)));
    $('#note-desc').textContent = n.description || ''; $('#note-desc').hidden = !n.description;
    renderBody($('#note-body'), note);
    const out = $('#links-out'), inn = $('#links-in'), mis = $('#links-missing');
    linkChips(out, note.links_out); $('#h-out').hidden = out.hidden = !note.links_out.length;
    linkChips(inn, note.links_in); $('#h-in').hidden = inn.hidden = !note.links_in.length;
    linkChips(mis, note.missing, true); $('#h-missing').hidden = mis.hidden = !note.missing.length;
    $('#cli code').textContent = `brain g ${n.name}`;
    $('#note .body').scrollTop = 0;
    // the read is an access too: it joins the recall trail
    B.touch([id, ...note.links_out, ...note.links_in], 'view', id, true);
  };
  B.close = function () { B.selected = null; $('#note').hidden = true; $('#welcome').hidden = false; B.refresh(); };

  // ------------------------------------------------------------ activity feed
  const KIND_LABEL = { search: 'search', 'ui-search': 'search', get: 'read', view: 'read', links: 'links', list: 'list' };
  B.touch = function (ids, kind, query, local) {
    const now = performance.now(); ids.forEach((id, i) => B.heat.set(id, now + i * 90));
    if (!local) B.refresh();
  };
  function addEvent(ev, fresh) {
    const ol = $('#feed ol'); $('#feed-empty').hidden = true;
    const li = el('li', 'ev' + (fresh && !REDUCED ? ' new' : ''));
    li.append(el('span', 'k ' + ev.kind, KIND_LABEL[ev.kind] || ev.kind));
    const q = el('span', 'q'); if (ev.kind === 'search' || ev.kind === 'ui-search') q.append(el('span', 'mono', ev.query)); else q.textContent = (B.byId.get(ev.query) || {}).name || ev.query || '—';
    q.title = ev.query || ''; li.append(q);
    const t = el('time', null, new Date(ev.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })); t.dateTime = new Date(ev.ts * 1000).toISOString(); li.append(t);
    if (ev.nodes && ev.nodes.length) {
      const ns = el('div', 'nodes');
      ev.nodes.slice(0, 5).forEach(id => { const n = B.byId.get(id); const b = el('button', null, n ? n.name : id); b.title = id; if (n) b.onclick = () => { B.open(id); if (B.view && B.view.focus) B.view.focus(id); }; else b.disabled = true; ns.append(b); });
      if (ev.nodes.length > 5) ns.append(el('span', 'more', `+${ev.nodes.length - 5}`));
      li.append(ns);
    }
    ol.prepend(li); while (ol.children.length > 60) ol.lastChild.remove();
  }
  B.listen = function () {
    const es = new EventSource('/events'); const live = $('#live'); let first = true, seen = 0;
    es.onopen = () => live.classList.remove('off');
    es.onerror = () => live.classList.add('off');
    es.onmessage = m => {
      const ev = JSON.parse(m.data); const fresh = !first || (Date.now() / 1000 - ev.ts < 20);
      if (fresh) B.touch(ev.nodes || [], ev.kind, ev.query);
      addEvent(ev, !first); seen++;
    };
    // the burst of history arrives in the first ~100 ms; after that every event is live
    setTimeout(() => first = false, 800);
  };

  // ------------------------------------------------------------ keyboard & wiring
  B.wire = function () {
    const input = $('#search');
    input.addEventListener('input', e => B.search(e.target.value));
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); moveCur(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); moveCur(-1); }
      else if (e.key === 'Enter') { const h = results[cur < 0 ? 0 : cur]; if (h) { B.open(h.id); if (B.view && B.view.focus) B.view.focus(h.id); } }
      else if (e.key === 'Escape') { if (input.value) { input.value = ''; B.search(''); } else { input.blur(); } }
    });
    $('.searchbox .clear').onclick = () => { input.value = ''; B.search(''); input.focus(); };
    $('#note-close').onclick = B.close;
    $('#cli button').onclick = async () => { try { await navigator.clipboard.writeText($('#cli code').textContent); $('#cli button').textContent = 'copied'; setTimeout(() => $('#cli button').textContent = 'copy', 1200); } catch (e) { } };
    document.addEventListener('keydown', e => {
      const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
      if (e.key === '/' && !typing) { e.preventDefault(); input.focus(); input.select(); }
      else if (e.key === 'Escape' && !typing) { if (B.query) { input.value = ''; B.search(''); } else if (B.selected) B.close(); else { B.hovered = null; B.refresh(); } }
      else if (e.key === 'f' && !typing && B.view && B.view.fit) B.view.fit();
    });
    const hint = $('#hint'); if (hint) setTimeout(() => hint.classList.add('gone'), 9000);
  };

  B.boot = async function (view) {
    B.view = view;
    try { await B.load(); } catch (e) { $('.stats').textContent = 'could not load the index: ' + e.message; return; }
    $('#empty').classList.toggle('on', !B.nodes.length);
    view.init(); B.wire(); B.listen();
  };
})();
