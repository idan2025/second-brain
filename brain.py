#!/usr/bin/env python3
"""brain: on-demand recall over a folder of Markdown notes (default: Claude Code memory).

The notes (one fact per file; default ~/.claude/projects/*/memory/*.md, or BRAIN_SRC) stay
the source of truth; this indexes them into SQLite FTS5 with a link graph
from their [[name]] links, answers compact queries, and logs every access so
the web UI can show recall live. Notes are never written, only read.

  brain s QUERY [-p PROJECT] [-t TYPE] [--days N] [-n N] [-v] [-w] [--json]
                                      search; prints id, description
                                      -v adds a snippet, -w says why each hit matched
                                      -t user|feedback|project|reference, --days N = changed in N days
  brain g NAME [--head N]             print a note and its linked notes
  brain n NAME                        linked notes only (the "synapses")
  brain ls [-p PROJECT] [-t TYPE]     every note, one line each
  brain stale [DAYS]                  old notes that talk about open/pending work
  brain orphans                       notes nothing links to, and links to notes that do not exist
  brain check                         health: index warnings, orphans, dangling links
  brain top                           most recalled notes, and how many were never used
  brain index                         rebuild now (also automatic on change; incremental)
  brain serve [PORT] [--lan]          live web UI (default 127.0.0.1:7777; --lan = all interfaces,
                                      HTTP Basic auth if ~/.config/brain/pw exists)
"""
import glob, hashlib, json, os, re, sqlite3, sys, time

HOME = os.path.expanduser("~")
ROOT = os.environ.get("BRAIN_ROOT") or os.path.dirname(os.path.realpath(__file__))
DB = os.environ.get("BRAIN_DB") or os.path.join(ROOT, "brain.db")
SRC = os.environ.get("BRAIN_SRC") or os.path.join(HOME, ".claude/projects/*/memory/*.md")
PW_FILE = os.path.join(HOME, ".config/brain/pw")
STATIC = os.path.join(ROOT, "static")
SCHEMA = "3"
TYPES = ("user", "feedback", "project", "reference")
LINK = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9._ -]*?)\]\]")
MDLINK = re.compile(r"\]\(\.?/?([A-Za-z0-9][A-Za-z0-9._-]*)\.md\)")
# markers the search uses to say where a hit matched; never typed in a note
HL_A, HL_B = "\x01", "\x02"


def project_of(path):
    # Claude Code layout: ~/.claude/projects/<project>/memory/x.md. Any other
    # folder of notes is its own project, named by the folder it sits in.
    if "/.claude/projects/" in path:
        p = path.split("/.claude/projects/")[1].split("/memory/")[0]
        p = re.sub(r"^-home-[^-]+-", "", p).strip("-")
        return p or os.path.basename(os.path.dirname(path))
    return os.path.basename(os.path.dirname(path)) or "notes"


def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse(path):
    """Read one note. Returns (name, description, type, body, warnings).

    Tolerant on purpose: a note that is half-written or has odd frontmatter is
    indexed with what can be read and a warning, never dropped or raised on.
    """
    warns = []
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if "�" in text:
        warns.append("not valid UTF-8 (replaced bytes)")
    meta, body = {}, text
    stripped = text.lstrip("﻿")
    if stripped.startswith("---"):
        end = stripped.find("\n---", 3)
        if end < 0:
            warns.append("frontmatter never closed (indexing the whole file as body)")
        else:
            section = None
            for n, line in enumerate(stripped[3:end].splitlines(), 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indented = line[:1] in " \t"
                m = re.match(r"\s*([A-Za-z_][\w.-]*)\s*:\s*(.*)$", line)
                if not m:
                    warns.append(f"frontmatter line {n} is not key: value")
                    continue
                k, v = m.group(1), unquote(m.group(2))
                if not indented:
                    section = k if v == "" else None
                    if v != "":
                        meta[k] = v
                elif section:
                    meta[f"{section}.{k}"] = v
            body = stripped[end + 4:].lstrip("\n")
            if not body.strip():
                warns.append("empty body")
    elif not text.strip():
        warns.append("empty file")
    else:
        warns.append("no frontmatter")
    name = meta.get("name") or os.path.basename(path)[:-3]
    typ = meta.get("type") or meta.get("metadata.type") or ""
    if typ and typ not in TYPES:
        warns.append(f"unknown type {typ!r}")
    desc = meta.get("description", "")
    if not desc:
        warns.append("no description")
    return name, desc, typ, body, warns


def connect():
    os.makedirs(ROOT, exist_ok=True)
    db = sqlite3.connect(DB, timeout=10)
    db.execute("pragma journal_mode=wal")
    v = db.execute("select v from state where k='schema'").fetchone() if _has(db, "state") else None
    if not v or v[0] != SCHEMA:
        for t in ("notes", "fts", "links", "dangling", "listed", "state"):
            db.execute(f"drop table if exists {t}")
    db.executescript("""
      create table if not exists notes(id text primary key, project text, name text, type text,
        description text, body text, path text, mtime real, size integer, warn text);
      create virtual table if not exists fts using fts5(id unindexed, name, description, body,
        tokenize='porter unicode61', prefix='2 3 4');
      create table if not exists links(src text, dst text, primary key(src, dst));
      create table if not exists dangling(src text, target text, primary key(src, target));
      create table if not exists listed(id text primary key);
      create table if not exists events(id integer primary key, ts real, kind text, query text, nodes text);
      create table if not exists state(k text primary key, v text);
      insert or replace into state values('schema', '%s');
    """ % SCHEMA)
    return db


def _has(db, table):
    return db.execute("select 1 from sqlite_master where name=?", (table,)).fetchone() is not None


def sources():
    return [p for p in glob.glob(SRC) if not p.endswith("/MEMORY.md")]


def index_files():
    return [p for p in glob.glob(SRC) if p.endswith("/MEMORY.md")]


def fingerprint(paths):
    st = []
    for p in paths:
        try:
            s = os.stat(p)
            st.append(f"{p}:{s.st_mtime_ns}:{s.st_size}")
        except OSError:
            pass
    return "%d:%s" % (len(st), hashlib.sha1("|".join(sorted(st)).encode()).hexdigest()[:16])


def index(db, force=False):
    """Bring the index up to date. Incremental: only changed, new or removed
    files are touched; the link graph is rebuilt when anything changed.
    One bad file never stops the others or the server."""
    paths = sources()
    fp = fingerprint(paths + index_files())
    row = db.execute("select v from state where k='fp'").fetchone()
    if row and row[0] == fp and not force:
        return False
    known = {p: (m, s, i) for i, p, m, s in db.execute("select id, path, mtime, size from notes")}
    if force:
        db.execute("delete from notes"); db.execute("delete from fts"); known = {}
    seen, changed = set(), 0
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        seen.add(p)
        old = known.get(p)
        if old and old[0] == st.st_mtime and old[1] == st.st_size:
            continue
        try:
            name, desc, typ, body, warns = parse(p)
        except Exception as e:  # pragma: no cover - defensive; parse is tolerant already
            name, desc, typ, body, warns = os.path.basename(p)[:-3], "", "", "", [f"could not read: {e}"]
        proj = project_of(p)
        nid = f"{proj}/{name}"
        if old:
            db.execute("delete from fts where id=?", (old[2],))
            db.execute("delete from notes where path=?", (p,))
        clash = db.execute("select path from notes where id=?", (nid,)).fetchone()
        if clash and clash[0] != p:
            warns.append(f"name clashes with {os.path.basename(clash[0])}; indexed as {name}~{os.path.basename(p)[:-3]}")
            nid = f"{proj}/{name}~{os.path.basename(p)[:-3]}"
        db.execute("insert or replace into notes values(?,?,?,?,?,?,?,?,?,?)",
                   (nid, proj, name, typ, desc, body, p, st.st_mtime, st.st_size, json.dumps(warns)))
        db.execute("insert into fts values(?,?,?,?)", (nid, name, desc, body))
        changed += 1
    for p, (_, _, nid) in known.items():
        if p not in seen:
            db.execute("delete from fts where id=?", (nid,))
            db.execute("delete from notes where path=?", (p,))
            changed += 1
    rebuild_links(db)
    db.execute("insert or replace into state values('fp',?)", (fp,))
    db.execute("insert or replace into state values('indexed_at',?)", (str(time.time()),))
    db.commit()
    return changed


def link_targets(text):
    out = {t.strip().lower() for t in LINK.findall(text)}
    out |= {t.lower() for t in MDLINK.findall(text)}
    return out


def rebuild_links(db):
    db.execute("delete from links"); db.execute("delete from dangling"); db.execute("delete from listed")
    by_name = {}
    for nid, proj, name, path in db.execute("select id, project, name, path from notes"):
        by_name.setdefault(name.lower(), []).append(nid)
        by_name.setdefault(os.path.basename(path)[:-3].lower(), []).append(nid)

    def resolve_in(proj, target):
        cands = by_name.get(target, [])
        return next((c for c in cands if c.startswith(proj + "/")), cands[0] if cands else None)

    for nid, proj, body in db.execute("select id, project, body from notes").fetchall():
        for target in link_targets(body):
            dst = resolve_in(proj, target)
            if dst and dst != nid:
                db.execute("insert or ignore into links values(?,?)", (nid, dst))
            elif not dst:
                db.execute("insert or ignore into dangling values(?,?)", (nid, target))
    for p in index_files():
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        proj = project_of(p)
        for target in link_targets(text):
            dst = resolve_in(proj, target)
            if dst:
                db.execute("insert or ignore into listed values(?)", (dst,))
            else:
                db.execute("insert or ignore into dangling values(?,?)", (f"{proj}/MEMORY.md", target))


def warnings(db):
    out = []
    for nid, path, warn in db.execute("select id, path, warn from notes where warn != '[]' order by id"):
        for w in json.loads(warn or "[]"):
            out.append((nid, path, w))
    return out


def log(db, kind, query, nodes):
    db.execute("insert into events(ts,kind,query,nodes) values(?,?,?,?)",
               (time.time(), kind, query, json.dumps(nodes)))
    db.commit()


def resolve(db, name):
    name = name.strip()
    row = db.execute("select id from notes where id=? or name=? collate nocase "
                     "order by project=? desc", (name, name, os.environ.get("BRAIN_PREFER_PROJECT", ""))).fetchone()
    if row:
        return row[0]
    row = db.execute("select id from notes where path like ? limit 1", (f"%/{name}.md",)).fetchone()
    if row:
        return row[0]
    row = db.execute("select id from notes where name like ? or id like ? order by length(id) limit 1",
                     (f"%{name}%", f"%{name}%")).fetchone()
    return row[0] if row else None


def neighbors(db, nid):
    out = db.execute("select dst from links where src=? union select src from links where dst=?", (nid, nid)).fetchall()
    return [r[0] for r in out]


def fts_query(q):
    """Every word must appear (stemmed; words of 3+ letters also match as a prefix)."""
    words = [w for w in re.findall(r"[\w.-]+", q) if w.strip(".-")]
    if not words:
        return None
    parts = []
    for w in words:
        w = w.strip(".-").replace('"', "")
        term = f'"{w}"'
        if len(w) >= 3 and not w.endswith("*"):
            term = f'({term} OR "{w}"*)'
        parts.append(term)
    return " AND ".join(parts)


def fts_query_any(q):
    words = [w.strip(".-").replace('"', "") for w in re.findall(r"[\w.-]+", q)]
    words = [w for w in words if w]
    return " OR ".join(f'("{w}" OR "{w}"*)' if len(w) >= 3 else f'"{w}"' for w in words) or None


def search(db, q, n=3, project=None, typ=None, days=None):
    """Ranked hits. Each row: dict(id, description, snippet, why, score, name, type, project, mtime).
    Name counts 10x and description 4x a body word; a query where every word
    matches wins over one where only some do (AND first, OR as the fallback)."""
    where, params = [], []
    if project:
        where.append("n.project like ?"); params.append(f"%{project}%")
    if typ:
        where.append("n.type = ?"); params.append(typ)
    if days:
        where.append("n.mtime >= ?"); params.append(time.time() - float(days) * 86400)
    extra = (" and " + " and ".join(where)) if where else ""
    sql = ("select n.id, n.description, snippet(fts, 3, ?, ?, '…', 12), "
           "highlight(fts, 1, ?, ?), highlight(fts, 2, ?, ?), bm25(fts, 0, 10, 4, 1), "
           "n.name, n.type, n.project, n.mtime from fts join notes n on n.id=fts.id "
           "where fts match ?" + extra + " order by bm25(fts, 0, 10, 4, 1) limit ?")
    rows = []
    for mode, mq in (("all", fts_query(q)), ("any", fts_query_any(q))):
        if not mq:
            break
        try:
            rows = db.execute(sql, [HL_A, HL_B] * 3 + [mq] + params + [max(200, n * 5)]).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            break
    nwords = len(re.findall(r"[\w.-]+", q))
    out = []
    for nid, desc, snip, hname, hdesc, score, name, t, proj, mtime in rows:
        why = []
        if HL_A in hname: why.append("name")
        if HL_A in hdesc: why.append("description")
        if HL_A in snip: why.append("body")
        # Field tier first, bm25 within it. bm25's idf collapses when most notes share a word
        # (FTS5 clamps a negative idf to ~0), and then a body that repeats the word forty times
        # would outrank the note that is named after it.
        tier = (-(hname.count(HL_A) >= nwords), -("name" in why), -("description" in why), score)
        out.append(dict(id=nid, description=desc, snippet=snip, name_hl=hname, description_hl=hdesc,
                        why=why, score=round(-score, 3), name=name, type=t, project=proj, mtime=mtime,
                        mode=mode, _tier=tier))
    out.sort(key=lambda h: h["_tier"])
    for h in out:
        del h["_tier"]
    return out[:n]


def plain(s):
    return s.replace(HL_A, "").replace(HL_B, "")


def brackets(s):
    return s.replace(HL_A, "[").replace(HL_B, "]")


def cmd_search(db, args):
    n, proj, typ, days, words, verbose, why, as_json = 3, None, None, None, [], False, False, False
    it = iter(args)
    for a in it:
        if a == "-v": verbose = True
        elif a in ("-w", "--why"): why = True
        elif a == "--json": as_json = True
        elif a == "-n": n = int(next(it))
        elif a == "-p": proj = next(it)
        elif a == "-t": typ = next(it)
        elif a in ("--days", "--since"): days = float(next(it))
        else: words.append(a)
    q = " ".join(words)
    hits = search(db, q, n, proj, typ, days)
    log(db, "search", q, [h["id"] for h in hits])
    if as_json:
        for h in hits:
            h["snippet"] = brackets(h["snippet"]); h["name_hl"] = brackets(h["name_hl"]); h["description_hl"] = brackets(h["description_hl"])
        print(json.dumps(hits, indent=1))
        return
    for h in hits:
        print(f"{h['id']} - {h['description'][:110]}")
        if verbose:
            print(f"  …{plain(h['snippet']).replace(chr(10), ' ')}")
        if why:
            where = ", ".join(h["why"]) or "stem/prefix"
            tag = "" if h["mode"] == "all" else " (not every word)"
            print(f"  ↳ matched in {where}{tag}: {brackets(h['snippet']).replace(chr(10), ' ')[:140]}")
    if not hits:
        print("no match")


def cmd_get(db, args):
    head = None
    if "--head" in args:
        i = args.index("--head"); head = int(args[i + 1]); args = args[:i] + args[i + 2:]
    nid = resolve(db, " ".join(args))
    if not nid:
        sys.exit("no such note")
    body, path = db.execute("select body, path from notes where id=?", (nid,)).fetchone()
    nb = neighbors(db, nid)
    log(db, "get", nid, [nid] + nb)
    text = body.rstrip()
    if head:
        lines = text.splitlines()
        text = "\n".join(lines[:head]) + (f"\n… ({len(lines) - head} more lines; omit --head for all)" if len(lines) > head else "")
    print(f"# {nid}  ({path})\n{text}\n\nlinked: {', '.join(nb) or 'none'}")


def cmd_neighbors(db, args):
    nid = resolve(db, " ".join(args))
    if not nid:
        sys.exit("no such note")
    nb = neighbors(db, nid)
    log(db, "links", nid, [nid] + nb)
    for x in nb:
        print(x, "-", db.execute("select description from notes where id=?", (x,)).fetchone()[0][:120])


STALE_WORDS = re.compile(r"\b(unmerged|not merged|still open|pending|open as of|next:|todo|not yet|undecided)\b", re.I)


def stale(db, days):
    now = time.time()
    out = []
    for nid, desc, body, mtime in db.execute("select id, description, body, mtime from notes"):
        age = (now - mtime) / 86400
        flags = sorted({m.group(0).lower() for m in STALE_WORDS.finditer(desc + " " + body)})
        if age > days and flags:
            out.append((age, nid, flags))
    return sorted(out, reverse=True)


def cmd_stale(db, args):
    days = int(args[0]) if args else 14
    out = stale(db, days)
    for age, nid, flags in out:
        print(f"{age:4.0f}d  {nid}  [{', '.join(flags[:4])}]")
    print(f"{len(out)} notes older than {days}d that talk about open/pending work: verify, then fix or delete")


def orphans(db):
    """Notes nothing links to. 'listed' = only the project's MEMORY.md names it."""
    linked = {r[0] for r in db.execute("select dst from links")}
    listed = {r[0] for r in db.execute("select id from listed")}
    lone, index_only = [], []
    for nid, in db.execute("select id from notes order by id"):
        if nid in linked:
            continue
        (index_only if nid in listed else lone).append(nid)
    dang = db.execute("select src, target from dangling order by src, target").fetchall()
    return lone, index_only, dang


def cmd_orphans(db, args):
    lone, index_only, dang = orphans(db)
    if lone:
        print(f"{len(lone)} notes no note and no MEMORY.md links to:")
        for nid in lone:
            print(f"  {nid}")
    if index_only:
        print(f"{len(index_only)} notes only MEMORY.md links to (no note cites them):")
        for nid in index_only:
            print(f"  {nid}")
    if dang:
        print(f"{len(dang)} links to notes that do not exist:")
        for src, target in dang:
            print(f"  {src} -> [[{target}]]")
    if not (lone or index_only or dang):
        print("every note is linked and every link resolves")


def cmd_check(db, args):
    ws = warnings(db)
    total = db.execute("select count(*) from notes").fetchone()[0]
    print(f"{total} notes, {db.execute('select count(*) from links').fetchone()[0]} links, "
          f"{len({w[0] for w in ws})} notes with warnings")
    for nid, path, w in ws:
        print(f"  warn  {nid}: {w}")
    cmd_orphans(db, [])
    st = stale(db, 14)
    print(f"{len(st)} notes older than 14d talk about open work (brain stale)")


def cmd_top(db, args):
    hits = {}
    for (nodes,) in db.execute("select nodes from events where kind in ('get','search')"):
        for nid in json.loads(nodes):
            hits[nid] = hits.get(nid, 0) + 1
    ranked = sorted(hits.items(), key=lambda x: -x[1])
    for nid, c in ranked[:15]:
        print(f"{c:4d}  {nid}")
    unused = db.execute("select count(*) from notes").fetchone()[0] - len(hits)
    print(f"{len(hits)} notes ever recalled, {unused} never")


def cmd_ls(db, args):
    proj = typ = None
    it = iter(args)
    for a in it:
        if a == "-p": proj = next(it, None)
        elif a == "-t": typ = next(it, None)
    where, params = [], []
    if proj: where.append("project like ?"); params.append(f"%{proj}%")
    if typ: where.append("type = ?"); params.append(typ)
    sql = "select id, description from notes" + (" where " + " and ".join(where) if where else "") + " order by id"
    rows = db.execute(sql, params).fetchall()
    log(db, "list", proj or "*", [])
    for nid, desc in rows:
        print(f"{nid} - {desc[:100]}")


def cmd_index(db, args):
    print(db.execute("select count(*) from notes").fetchone()[0], "notes,",
          db.execute("select count(*) from links").fetchone()[0], "links")
    for nid, path, w in warnings(db):
        print(f"warn  {nid}: {w}", file=sys.stderr)


# ---------------------------------------------------------------- web UI

STATIC_TYPES = {".js": "application/javascript", ".css": "text/css; charset=utf-8", ".txt": "text/plain; charset=utf-8",
                ".svg": "image/svg+xml", ".woff2": "font/woff2", ".map": "application/json", ".md": "text/plain; charset=utf-8",
                ".html": "text/html; charset=utf-8", ".json": "application/json", ".png": "image/png"}


def graph_json(db):
    deg_in, deg_out = {}, {}
    links = db.execute("select src, dst from links").fetchall()
    for s, d in links:
        deg_out[s] = deg_out.get(s, 0) + 1
        deg_in[d] = deg_in.get(d, 0) + 1
    listed = {r[0] for r in db.execute("select id from listed")}
    nodes = [dict(id=r[0], project=r[1], name=r[2], type=r[3], description=r[4], mtime=r[5], size=r[6],
                  warn=json.loads(r[7] or "[]"), inbound=deg_in.get(r[0], 0), outbound=deg_out.get(r[0], 0),
                  listed=r[0] in listed)
             for r in db.execute("select id, project, name, type, description, mtime, size, warn from notes")]
    recent = {}
    for ts, nodes_js in db.execute("select ts, nodes from events where kind in ('get','search','links','view') "
                                   "order by id desc limit 200"):
        for nid in json.loads(nodes_js):
            recent.setdefault(nid, ts)
    for n in nodes:
        n["last_recall"] = recent.get(n["id"])
    hits = {}
    for (nodes_js,) in db.execute("select nodes from events where kind in ('get','search')"):
        for nid in json.loads(nodes_js):
            hits[nid] = hits.get(nid, 0) + 1
    for n in nodes:
        n["recalls"] = hits.get(n["id"], 0)
    dang = db.execute("select src, target from dangling").fetchall()
    projects = sorted({n["project"] for n in nodes}, key=lambda p: (-sum(1 for n in nodes if n["project"] == p), p))
    indexed_at = db.execute("select v from state where k='indexed_at'").fetchone()
    return dict(nodes=nodes, links=[dict(source=s, target=d) for s, d in links],
                dangling=[dict(source=s, target=t) for s, t in dang], projects=projects, types=list(TYPES),
                warnings=len({w[0] for w in warnings(db)}),
                indexed_at=float(indexed_at[0]) if indexed_at else None)


def note_json(db, nid):
    row = db.execute("select id, project, name, type, description, body, path, mtime, warn from notes where id=?",
                     (nid,)).fetchone()
    if not row:
        return None
    out = [r[0] for r in db.execute("select dst from links where src=? order by dst", (nid,))]
    inn = [r[0] for r in db.execute("select src from links where dst=? order by src", (nid,))]
    dang = [r[0] for r in db.execute("select target from dangling where src=? order by target", (nid,))]
    return dict(id=row[0], project=row[1], name=row[2], type=row[3], description=row[4], body=row[5],
                path=row[6], mtime=row[7], warn=json.loads(row[8] or "[]"), links_out=out, links_in=inn,
                missing=dang, listed=db.execute("select 1 from listed where id=?", (nid,)).fetchone() is not None)


def cmd_serve(args):
    import http.server, socketserver, urllib.parse
    host = "0.0.0.0" if "--lan" in args else "127.0.0.1"
    args = [a for a in args if a != "--lan"]
    port = int(args[0]) if args else 7777

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "brain"
        sys_version = ""

        def log_message(self, *a):
            pass

        def send(self, code, ctype, data, cache=False, inline_style=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if ctype.startswith("text/html"):
                # the 3D library positions its tooltip through a style attribute, which a strict
                # style-src blocks; inline *style* is allowed on that page only, inline script never
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'self'; style-src 'self'%s; img-src 'self' data: blob:; "
                                 % (" 'unsafe-inline'" if inline_style else "") +
                                 "connect-src 'self'; font-src 'self'; worker-src 'self' blob:; base-uri 'none'; "
                                 "form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def json(self, obj, code=200):
            self.send(code, "application/json; charset=utf-8", json.dumps(obj).encode())

        def authed(self):
            import base64, hmac
            try:
                want = open(PW_FILE).read().strip()
            except OSError:
                return True  # no password file: open (loopback use)
            got = self.headers.get("Authorization", "")
            if got.startswith("Basic "):
                try:
                    pw = base64.b64decode(got[6:]).decode().split(":", 1)[1]
                    if hmac.compare_digest(pw.encode(), want.encode()):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="brain"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self):
            if not self.authed():
                return
            try:
                self.route()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # one bad request or file never takes the server down
                try:
                    self.send(500, "text/plain; charset=utf-8", f"error: {type(e).__name__}: {e}".encode())
                except Exception:
                    pass

        def route(self):
            url = urllib.parse.urlsplit(self.path)
            path, qs = url.path, urllib.parse.parse_qs(url.query)
            if path.startswith("/static/"):
                m = re.fullmatch(r"/static/([\w.-]+)", path)
                ext = os.path.splitext(m.group(1))[1] if m else ""
                full = os.path.join(STATIC, m.group(1)) if m else ""
                if m and ext in STATIC_TYPES and os.path.isfile(full):
                    with open(full, "rb") as f:
                        return self.send(200, STATIC_TYPES[ext], f.read(), cache=ext in (".js", ".woff2"))
                return self.send(404, "text/plain", b"not found")
            if path in ("/", "/3d", "/index.html"):
                name = "ui3d.html" if path == "/3d" else "ui.html"
                with open(os.path.join(ROOT, name), "rb") as f:
                    return self.send(200, "text/html; charset=utf-8", f.read(), inline_style=(path == "/3d"))
            db = connect()
            if path == "/graph":
                index(db)
                return self.json(graph_json(db))
            if path == "/search":
                index(db)
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    return self.json(dict(query=q, hits=[]))
                n = min(int((qs.get("n") or ["12"])[0]), 50)
                hits = search(db, q, n, (qs.get("project") or [None])[0], (qs.get("type") or [None])[0],
                              (qs.get("days") or [None])[0])
                if (qs.get("log") or ["1"])[0] != "0":
                    log(db, "ui-search", q, [h["id"] for h in hits])
                return self.json(dict(query=q, hits=hits))
            if path.startswith("/note/"):
                nid = urllib.parse.unquote(path[6:])
                row = db.execute("select body from notes where id=?", (nid,)).fetchone()
                if row and (qs.get("log") or ["1"])[0] != "0":
                    log(db, "view", nid, [nid] + neighbors(db, nid))
                return self.send(200 if row else 404, "text/plain; charset=utf-8", (row[0] if row else "").encode())
            if path.startswith("/api/note/"):
                nid = urllib.parse.unquote(path[10:])
                n = note_json(db, nid)
                if not n:
                    return self.json(dict(error="no such note"), 404)
                if (qs.get("log") or ["1"])[0] != "0":
                    log(db, "view", nid, [nid] + n["links_out"] + n["links_in"])
                return self.json(n)
            if path == "/check":
                index(db)
                lone, index_only, dang = orphans(db)
                return self.json(dict(warnings=[dict(id=i, path=p, warning=w) for i, p, w in warnings(db)],
                                      orphans=lone, index_only=index_only,
                                      dangling=[dict(source=s, target=t) for s, t in dang],
                                      stale=[dict(age_days=round(a, 1), id=i, flags=f) for a, i, f in stale(db, 14)]))
            if path == "/health":
                return self.json(dict(ok=True, notes=db.execute("select count(*) from notes").fetchone()[0]))
            if path == "/events":
                return self.events(db)
            self.send(404, "text/plain", b"not found")

        def events(self, db):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            last = (db.execute("select max(id) from events").fetchone()[0] or 0) - 30
            try:
                while True:
                    rows = db.execute("select id, ts, kind, query, nodes from events where id>? order by id",
                                      (last,)).fetchall()
                    for i, ts, kind, q, nodes in rows:
                        last = i
                        msg = json.dumps(dict(id=i, ts=ts, kind=kind, query=q, nodes=json.loads(nodes)))
                        self.wfile.write(f"id: {i}\ndata: {msg}\n\n".encode())
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    db = connect()
    try:
        index(db)
    except Exception as e:
        print(f"index warning: {e}", file=sys.stderr)
    locked = os.path.isfile(PW_FILE)
    print(f"brain UI on http://{host}:{port}/", flush=True)
    print("login: password from ~/.config/brain/pw" if locked else "login: none (no ~/.config/brain/pw)", flush=True)
    S((host, port), H).serve_forever()


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "help"):
        sys.exit(__doc__)
    if a[0] == "serve":
        return cmd_serve(a[1:])
    db = connect()
    index(db, force=a[0] == "index")
    cmds = {"s": cmd_search, "search": cmd_search, "g": cmd_get, "get": cmd_get, "n": cmd_neighbors,
            "ls": cmd_ls, "stale": cmd_stale, "top": cmd_top, "index": cmd_index,
            "orphans": cmd_orphans, "check": cmd_check}
    if a[0] not in cmds:
        sys.exit(__doc__)
    cmds[a[0]](db, a[1:])


if __name__ == "__main__":
    main()
