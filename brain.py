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
  brain ctx TASK [--tokens N] [-p P]  context bundle for a task: best notes plus their links,
                                      trimmed to a token budget (default 1500), one call
  brain dupes [THRESHOLD]             near-duplicate notes (0-1, default 0.5): merge them, or
                                      read both, where two notes disagree is where they overlap
  brain embed                         compute embeddings for semantic search (needs BRAIN_EMBED)
  brain mcp                           MCP server on stdio: search, get, context and list as tools
  brain index                         rebuild now (also automatic on change; incremental)
  brain serve [PORT] [--lan]          live web UI (default 127.0.0.1:7777; --lan = all interfaces,
                                      HTTP Basic auth if ~/.config/brain/pw exists)

Search tries every word, then any word, then close spellings ("did you mean").
Within a match tier, notes many others link to and notes recalled often rank higher.
"""
import difflib, glob, hashlib, json, math, os, re, sqlite3, stat, sys, time

HOME = os.path.expanduser("~")
ROOT = os.environ.get("BRAIN_ROOT") or os.path.dirname(os.path.realpath(__file__))
DB = os.environ.get("BRAIN_DB") or os.path.join(ROOT, "brain.db")
SRC = os.environ.get("BRAIN_SRC") or os.path.join(HOME, ".claude/projects/*/memory/*.md")
PW_FILE = os.path.join(HOME, ".config/brain/pw")
STATIC = os.path.join(ROOT, "static")
SCHEMA = "4"
VERSION = "0.2.0"
# Semantic search is off unless set: "ollama:<model>", e.g. ollama:nomic-embed-text
EMBED = os.environ.get("BRAIN_EMBED", "")
OLLAMA = os.environ.get("BRAIN_OLLAMA", "http://127.0.0.1:11434").rstrip("/")
# the access log keeps this many rows; counts per note survive in `recalls`
EVENTS_KEEP = int(os.environ.get("BRAIN_EVENTS_KEEP", "5000"))
TYPES = ("user", "feedback", "project", "reference")
LINK = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9._ -]*?)\]\]")
MDLINK = re.compile(r"\]\(\.?/?([A-Za-z0-9][A-Za-z0-9._-]*)\.md\)")
# markers the search uses to say where a hit matched; never typed in a note
HL_A, HL_B = "\x01", "\x02"


def slash(path):
    """Forward slashes, so every path check below also holds on Windows."""
    return path.replace("\\", "/")


def project_of(path):
    # Claude Code layout: ~/.claude/projects/<project>/memory/x.md. Any other
    # folder of notes is its own project, named by the folder it sits in.
    path = slash(path)
    if "/.claude/projects/" in path:
        p = path.split("/.claude/projects/")[1].split("/memory/")[0]
        p = re.sub(r"^-home-[^-]+-", "", p).strip("-")
        p = re.sub(r"^[A-Za-z]--Users-[^-]+-", "", p).strip("-")  # Windows: C--Users-name-proj
        return p or path.rsplit("/", 2)[-2]
    parts = path.rsplit("/", 2)
    return (parts[-2] if len(parts) > 1 else "") or "notes"


def stem_of(path):
    return os.path.splitext(slash(path).rsplit("/", 1)[-1])[0]


def unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


BLOCK = re.compile(r"^[|>][+-]?\d*$")


def parse_frontmatter(text):
    """The subset of YAML notes use: key: value, one level of nesting
    (metadata: / type: x), block scalars (description: >- then indented lines),
    plain values continued on indented lines, and `- item` lists (joined
    with ", "). Anything else is skipped with a warning, never raised on."""
    meta, warns = {}, []
    lines = text.splitlines()
    section, last = None, None  # section: open parent key; last: key a continuation joins
    i = 0
    while i < len(lines):
        line, n = lines[i], i + 1
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        item = re.match(r"\s*-\s+(.*)$", line)
        if item and last is not None and (indent > 0 or meta.get(last) == ""):
            v = unquote(item.group(1))
            meta[last] = f"{meta[last]}, {v}" if meta.get(last) else v
            continue
        m = re.match(r"\s*([A-Za-z_][\w.-]*)\s*:(?:\s+(.*)|\s*)$", line)
        if not m:
            if last and indent > 0 and meta.get(last):
                meta[last] += " " + line.strip()  # plain scalar continued on the next line
                continue
            warns.append(f"frontmatter line {n} is not key: value")
            continue
        k, raw = m.group(1), (m.group(2) or "").strip()
        if indent == 0:
            key = k
            section = None
        elif section:
            key = f"{section}.{k}"
        else:
            key = k
        if BLOCK.match(raw):
            chunk = []
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip(" \t")) > indent):
                chunk.append(lines[i].strip())
                i += 1
            joined = ("\n" if raw[0] == "|" else " ").join(c for c in chunk if c or raw[0] == "|").strip()
            meta[key] = re.sub(r"\s+", " ", joined) if raw[0] == ">" else joined
            last = None
            continue
        v = unquote(raw)
        if indent == 0 and v == "":
            section = k
            meta.setdefault(k, "")
            last = k  # a list may follow (tags:\n  - a)
            continue
        meta[key] = v
        last = key
    meta = {k: v for k, v in meta.items() if v != "" or "." in k}
    return meta, warns


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
            meta, fwarns = parse_frontmatter(stripped[3:end])
            warns += fwarns
            body = stripped[end + 4:].lstrip("\n")
            if not body.strip():
                warns.append("empty body")
    elif not text.strip():
        warns.append("empty file")
    else:
        warns.append("no frontmatter")
    name = meta.get("name") or stem_of(path)
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
    if v and v[0] == SCHEMA:
        return db  # the common case writes nothing, so readers never queue behind a lock
    # Derived tables are rebuilt from the notes; the access log, its counts and
    # embeddings (keyed by content hash) survive a schema change.
    for t in ("notes", "fts", "vocab", "links", "dangling", "listed", "rank", "state"):
        db.execute(f"drop table if exists {t}")
    db.executescript("""
      create table if not exists notes(id text primary key, project text, name text, type text,
        description text, body text, path text, mtime real, size integer, warn text);
      create virtual table if not exists fts using fts5(id unindexed, name, description, body,
        tokenize='porter unicode61', prefix='2 3 4');
      create virtual table if not exists vocab using fts5vocab(fts, 'row');
      create table if not exists links(src text, dst text, primary key(src, dst));
      create table if not exists dangling(src text, target text, primary key(src, target));
      create table if not exists listed(id text primary key);
      create table if not exists rank(id text primary key, pr real);
      create table if not exists events(id integer primary key, ts real, kind text, query text, nodes text);
      create table if not exists recalls(id text primary key, n integer, last real);
      create table if not exists emb(id text primary key, hash text, model text, vec text);
      create table if not exists state(k text primary key, v text);
    """)
    if not db.execute("select 1 from recalls limit 1").fetchone():
        for ts, kind, nodes in db.execute("select ts, kind, nodes from events").fetchall():
            for nid in json.loads(nodes or "[]"):
                _count(db, nid, ts, kind)
    db.execute("insert or replace into state values('schema', ?)", (SCHEMA,))
    db.commit()
    return db


RECALL_KINDS = ("get", "search", "ctx", "mcp")


def _count(db, nid, ts, kind):
    if kind in RECALL_KINDS:
        db.execute("insert into recalls values(?,1,?) on conflict(id) do update set n=n+1, last=max(last, excluded.last)",
                   (nid, ts))
    else:
        db.execute("insert into recalls values(?,0,?) on conflict(id) do update set last=max(last, excluded.last)",
                   (nid, ts))


def _has(db, table):
    return db.execute("select 1 from sqlite_master where name=?", (table,)).fetchone() is not None


def all_files():
    """Every file BRAIN_SRC names. Several globs may be given, split by the OS
    path separator (: on Linux and macOS, ; on Windows); ** recurses."""
    out = set()
    for pattern in (SRC.split(os.pathsep) if os.pathsep in SRC else [SRC]):
        if pattern.strip():
            out.update(glob.glob(os.path.expanduser(pattern.strip()), recursive=True))
    # no stat here: fingerprint() and index() stat each file once and skip what is not a file
    return sorted(p for p in out if p.lower().endswith(".md"))


def is_index_file(p):
    return slash(p).rsplit("/", 1)[-1] == "MEMORY.md"


def sources(files=None):
    return [p for p in (all_files() if files is None else files) if not is_index_file(p)]


def index_files(files=None):
    return [p for p in (all_files() if files is None else files) if is_index_file(p)]


def fingerprint(paths):
    st = []
    for p in paths:
        try:
            s = os.stat(p)
            if stat.S_ISREG(s.st_mode):
                st.append(f"{p}:{s.st_mtime_ns}:{s.st_size}")
        except OSError:
            pass
    return "%d:%s" % (len(st), hashlib.sha1("|".join(sorted(st)).encode()).hexdigest()[:16])


def index(db, force=False):
    """Bring the index up to date. Incremental: only changed, new or removed
    files are touched; the link graph is rebuilt when anything changed.
    One bad file never stops the others or the server."""
    files = all_files()
    paths, idx = sources(files), index_files(files)
    fp = fingerprint(paths + idx)
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
        if not stat.S_ISREG(st.st_mode):
            continue
        seen.add(p)
        old = known.get(p)
        if old and old[0] == st.st_mtime and old[1] == st.st_size:
            continue
        try:
            name, desc, typ, body, warns = parse(p)
        except Exception as e:  # pragma: no cover - defensive; parse is tolerant already
            name, desc, typ, body, warns = stem_of(p), "", "", "", [f"could not read: {e}"]
        proj = project_of(p)
        nid = f"{proj}/{name}"
        if old:
            db.execute("delete from fts where id=?", (old[2],))
            db.execute("delete from notes where path=?", (p,))
        clash = db.execute("select path from notes where id=?", (nid,)).fetchone()
        if clash and clash[0] != p:
            warns.append(f"name clashes with {os.path.basename(clash[0])}; indexed as {name}~{stem_of(p)}")
            nid = f"{proj}/{name}~{stem_of(p)}"
        db.execute("insert or replace into notes values(?,?,?,?,?,?,?,?,?,?)",
                   (nid, proj, name, typ, desc, body, p, st.st_mtime, st.st_size, json.dumps(warns)))
        db.execute("insert into fts values(?,?,?,?)", (nid, name, desc, body))
        changed += 1
    for p, (_, _, nid) in known.items():
        if p not in seen:
            db.execute("delete from fts where id=?", (nid,))
            db.execute("delete from notes where path=?", (p,))
            changed += 1
    rebuild_links(db, idx)
    rebuild_rank(db)
    db.execute("insert or replace into state values('fp',?)", (fp,))
    db.execute("insert or replace into state values('indexed_at',?)", (str(time.time()),))
    db.commit()
    return changed


CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def link_targets(text):
    text = CODE.sub(" ", text)  # `[[extra_ports]]` in code is a TOML table, not a link
    out = {t.strip().lower() for t in LINK.findall(text)}
    out |= {t.lower() for t in MDLINK.findall(text)}
    return out


def rebuild_links(db, idx=None):
    db.execute("delete from links"); db.execute("delete from dangling"); db.execute("delete from listed")
    by_name = {}
    for nid, proj, name, path in db.execute("select id, project, name, path from notes"):
        by_name.setdefault(name.lower(), []).append(nid)
        by_name.setdefault(stem_of(path).lower(), []).append(nid)

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
    for p in (index_files() if idx is None else idx):
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


def rebuild_rank(db, damping=0.85, rounds=30):
    """PageRank over the [[link]] graph, scaled so the average note is 1.0.
    A note many others cite is a hub worth reading first."""
    ids = [r[0] for r in db.execute("select id from notes")]
    db.execute("delete from rank")
    if not ids:
        return
    out = {i: [] for i in ids}
    for s_, d in db.execute("select src, dst from links"):
        if s_ in out and d in out:
            out[s_].append(d)
    n = len(ids)
    pr = dict.fromkeys(ids, 1.0 / n)
    for _ in range(rounds):
        sink = sum(pr[i] for i in ids if not out[i])
        nxt = dict.fromkeys(ids, (1 - damping) / n + damping * sink / n)
        for i in ids:
            if out[i]:
                share = damping * pr[i] / len(out[i])
                for d in out[i]:
                    nxt[d] += share
        pr = nxt
    db.executemany("insert into rank values(?,?)", [(i, pr[i] * n) for i in ids])


def warnings(db):
    out = []
    for nid, path, warn in db.execute("select id, path, warn from notes where warn != '[]' order by id"):
        for w in json.loads(warn or "[]"):
            out.append((nid, path, w))
    return out


def log(db, kind, query, nodes):
    now = time.time()
    cur = db.execute("insert into events(ts,kind,query,nodes) values(?,?,?,?)",
                     (now, kind, query, json.dumps(nodes)))
    for nid in dict.fromkeys(nodes):
        _count(db, nid, now, kind)
    if cur.lastrowid % 100 == 0:  # keep the raw log bounded; the counts live on in `recalls`
        db.execute("delete from events where id <= ?", (cur.lastrowid - EVENTS_KEEP,))
    db.commit()


def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve(db, name):
    name = name.strip()
    row = db.execute("select id from notes where id=? or name=? collate nocase "
                     "order by project=? desc", (name, name, os.environ.get("BRAIN_PREFER_PROJECT", ""))).fetchone()
    if row:
        return row[0]
    e = like_escape(name)
    row = db.execute("select id from notes where replace(path, '\\', '/') like ? escape '\\' limit 1",
                     (f"%/{e}.md",)).fetchone()
    if row:
        return row[0]
    row = db.execute("select id from notes where name like ? escape '\\' or id like ? escape '\\' "
                     "order by length(id) limit 1", (f"%{e}%", f"%{e}%")).fetchone()
    return row[0] if row else None


def project_filter(db, project):
    """An exact project name selects only it; anything else is a substring."""
    if db.execute("select 1 from notes where project=? limit 1", (project,)).fetchone():
        return "n.project = ?", project
    return "n.project like ? escape '\\'", f"%{like_escape(project)}%"


def neighbors(db, nid):
    out = db.execute("select dst from links where src=? union select src from links where dst=?", (nid, nid)).fetchall()
    return [r[0] for r in out]


STOP = frozenset("a an and are as at be by do does for from how i in is it of on or the to was what when "
                 "where which who why with you".split())


def query_words(q):
    """The words a query searches by. Common words ("the", "how") are dropped
    while anything else is left, so "how to release the client" means release client."""
    words = [w for w in (x.strip(".-").replace('"', "") for x in re.findall(r"[\w.-]+", q)) if w]
    kept = [w for w in words if w.lower() not in STOP]
    return kept or words


def fts_query(q):
    """Every word must appear (stemmed; words of 3+ letters also match as a prefix)."""
    words = query_words(q)
    if not words:
        return None
    return " AND ".join(f'("{w}" OR "{w}"*)' if len(w) >= 3 else f'"{w}"' for w in words)


def fts_query_any(q):
    return " OR ".join(f'("{w}" OR "{w}"*)' if len(w) >= 3 else f'"{w}"' for w in query_words(q)) or None


def respell(db, q):
    """Close spellings of each word from the index's own vocabulary, for a
    query that matched nothing. Returns the corrected query, or None."""
    words, fixed, changed = query_words(q), [], False
    for w in words:
        lw = w.lower()
        if len(lw) < 4 or db.execute("select 1 from vocab where term >= ? and term < ? limit 1",
                                     (lw, lw + "￿")).fetchone():
            fixed.append(w)
            continue
        # compare only terms of a similar length and the same first letter: fast at any size
        terms = [r[0] for r in db.execute("select term from vocab where length(term) between ? and ? and substr(term,1,1)=?",
                                          (len(lw) - 2, len(lw) + 2, lw[0]))]
        best = difflib.get_close_matches(lw, terms, n=1, cutoff=0.75)
        if best:
            fixed.append(best[0]); changed = True
        else:
            fixed.append(w)
    return " ".join(fixed) if changed else None


def boosts(db, ids):
    """Per note: (link rank, recall count). Both default to neutral."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    pr = dict(db.execute(f"select id, pr from rank where id in ({marks})", ids).fetchall())
    rc = dict(db.execute(f"select id, n from recalls where id in ({marks})", ids).fetchall())
    return {i: (pr.get(i, 1.0), rc.get(i, 0)) for i in ids}


def search(db, q, n=3, project=None, typ=None, days=None, semantic=True):
    """Ranked hits. Each row: dict(id, description, snippet, why, score, name, type, project, mtime, mode).
    Name counts 10x and description 4x a body word; a query where every word
    matches wins over one where only some do (AND first, OR as the fallback,
    then close spellings, mode "fuzzy" with `suggest`). Inside a tier, notes
    others link to and notes recalled often rank higher. With BRAIN_EMBED set
    and `brain embed` run, notes close in meaning are fused in (mode "semantic")."""
    where, params = [], []
    if project:
        clause, val = project_filter(db, project)
        where.append(clause); params.append(val)
    if typ:
        where.append("n.type = ?"); params.append(typ)
    if days:
        where.append("n.mtime >= ?"); params.append(time.time() - float(days) * 86400)
    extra = (" and " + " and ".join(where)) if where else ""
    sql = ("select n.id, n.description, snippet(fts, 3, ?, ?, '…', 12), "
           "highlight(fts, 1, ?, ?), highlight(fts, 2, ?, ?), bm25(fts, 0, 10, 4, 1), "
           "n.name, n.type, n.project, n.mtime from fts join notes n on n.id=fts.id "
           "where fts match ?" + extra + " order by bm25(fts, 0, 10, 4, 1) limit ?")
    rows, mode, suggest, used = [], "all", None, q

    def run(mq):
        try:
            return db.execute(sql, [HL_A, HL_B] * 3 + [mq] + params + [max(200, n * 5)]).fetchall()
        except sqlite3.OperationalError:
            return []

    for mode, mq in (("all", fts_query(q)), ("any", fts_query_any(q))):
        if not mq:
            break
        rows = run(mq)
        if rows:
            break
    if (not rows or mode == "any") and q.strip():
        # nothing matched, or not every word did: a misspelt word is the usual reason
        fix = respell(db, q)
        if fix:
            tries = (fts_query(fix),) if rows else (fts_query(fix), fts_query_any(fix))
            for mq in tries:
                got = run(mq) if mq else []
                if got:
                    rows, mode, suggest, used = got, "fuzzy", fix, fix
                    break
    nwords = len(query_words(used))
    bo = boosts(db, [r[0] for r in rows])
    out = []
    for nid, desc, snip, hname, hdesc, score, name, t, proj, mtime in rows:
        why = []
        if HL_A in hname: why.append("name")
        if HL_A in hdesc: why.append("description")
        if HL_A in snip: why.append("body")
        pr, rc = bo.get(nid, (1.0, 0))
        # bm25 is negative (lower is better), so a boost above 1 moves a note up
        boosted = score * (1 + 0.25 * math.log1p(max(pr - 1, 0)) + 0.1 * math.log1p(rc))
        # Field tier first, bm25 within it. bm25's idf collapses when most notes share a word
        # (FTS5 clamps a negative idf to ~0), and then a body that repeats the word forty times
        # would outrank the note that is named after it.
        tier = (-(hname.count(HL_A) >= nwords), -("name" in why), -("description" in why), boosted)
        out.append(dict(id=nid, description=desc, snippet=snip, name_hl=hname, description_hl=hdesc,
                        why=why, score=round(-boosted, 3), name=name, type=t, project=proj, mtime=mtime,
                        mode=mode, _tier=tier))
    out.sort(key=lambda h: h["_tier"])
    for h in out:
        del h["_tier"]
    if semantic and EMBED and q.strip():
        out = fuse_semantic(db, q, out, n, extra, params)
    if suggest and out:
        for h in out:
            h["suggest"] = suggest
    return out[:n]


# ---------------------------------------------------------------- semantic (optional)

def embed_texts(texts):
    """Vectors from the configured model; raises on any failure."""
    import urllib.request
    kind, _, model = EMBED.partition(":")
    if kind != "ollama" or not model:
        raise ValueError(f"BRAIN_EMBED must be ollama:<model>, not {EMBED!r}")
    req = urllib.request.Request(OLLAMA + "/api/embed", data=json.dumps(dict(model=model, input=texts)).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        vecs = json.load(r)["embeddings"]
    if len(vecs) != len(texts):
        raise ValueError("embedding count does not match input count")
    return vecs


def note_text(name, desc, body):
    return f"{name}\n{desc}\n{body}"[:4000]


def embed_all(db, batch=16):
    """Embed notes whose text changed since their last vector. Returns (done, kept)."""
    rows = db.execute("select id, name, description, body from notes").fetchall()
    have = {i: (h, m) for i, h, m in db.execute("select id, hash, model from emb")}
    todo = []
    for nid, name, desc, body in rows:
        h = hashlib.sha1(note_text(name, desc, body).encode()).hexdigest()
        if have.get(nid) != (h, EMBED):
            todo.append((nid, h, note_text(name, desc, body)))
    for k in range(0, len(todo), batch):
        part = todo[k:k + batch]
        vecs = embed_texts([t for _, _, t in part])
        db.executemany("insert or replace into emb values(?,?,?,?)",
                       [(nid, h, EMBED, json.dumps([round(x, 5) for x in v])) for (nid, h, _), v in zip(part, vecs)])
        db.commit()
    db.execute("delete from emb where id not in (select id from notes)")
    db.commit()
    return len(todo), len(rows) - len(todo)


def _cos(a, b):
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


def fuse_semantic(db, q, hits, n, extra, params):
    """Reciprocal-rank fusion of the word hits with the nearest notes in meaning.
    Silent on any failure: semantic search is an extra, never a dependency."""
    try:
        rows = db.execute("select e.id, e.vec from emb e join notes n on n.id=e.id where e.model=?" + extra,
                          [EMBED] + params).fetchall()
        if not rows:
            return hits
        qv = embed_texts([q])[0]
    except Exception:
        return hits
    near = sorted(((_cos(qv, json.loads(v)), i) for i, v in rows), reverse=True)[:max(n * 3, 10)]
    near = [(s, i) for s, i in near if s > 0.3]
    score = {}
    for r, h in enumerate(hits):
        score[h["id"]] = score.get(h["id"], 0) + 1 / (60 + r)
    for r, (_, i) in enumerate(near):
        score[i] = score.get(i, 0) + 1 / (60 + r)
    by_id = {h["id"]: h for h in hits}
    for s, i in near:
        if i in by_id:
            if "meaning" not in by_id[i]["why"]:
                by_id[i]["why"].append("meaning")
            continue
        nid, desc, body, name, t, proj, mtime = db.execute(
            "select id, description, body, name, type, project, mtime from notes where id=?", (i,)).fetchone()
        by_id[i] = dict(id=nid, description=desc, snippet=body[:160].replace("\n", " "), name_hl=name,
                        description_hl=desc, why=["meaning"], score=round(s, 3), name=name, type=t, project=proj,
                        mtime=mtime, mode="semantic")
    return sorted(by_id.values(), key=lambda h: -score.get(h["id"], 0))


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
    if hits and hits[0].get("suggest"):
        print(f"(no exact match; did you mean: {hits[0]['suggest']})")
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


def get_text(db, name, head=None, kind="get"):
    """A note and its links as text, or None."""
    nid = resolve(db, name)
    if not nid:
        return None
    body, path = db.execute("select body, path from notes where id=?", (nid,)).fetchone()
    nb = neighbors(db, nid)
    log(db, kind, nid, [nid] + nb)
    text = body.rstrip()
    if head:
        lines = text.splitlines()
        text = "\n".join(lines[:head]) + (f"\n… ({len(lines) - head} more lines; omit --head for all)" if len(lines) > head else "")
    return f"# {nid}  ({path})\n{text}\n\nlinked: {', '.join(nb) or 'none'}"


def cmd_get(db, args):
    head = None
    if "--head" in args:
        i = args.index("--head"); head = int(args[i + 1]); args = args[:i] + args[i + 2:]
    out = get_text(db, " ".join(args), head)
    if out is None:
        sys.exit("no such note")
    print(out)


def search_text(hits):
    if not hits:
        return "no match"
    lines = []
    if hits[0].get("suggest"):
        lines.append(f"(no exact match; did you mean: {hits[0]['suggest']})")
    for h in hits:
        lines.append(f"{h['id']} - {h['description'][:110]}")
        lines.append(f"  …{plain(h['snippet']).replace(chr(10), ' ')}")
    return "\n".join(lines)


def tokens_of(text):
    return (len(text) + 3) // 4  # ~4 characters a token: close enough to budget by


ANY_KEEP = 0.25  # in an any-word match, a note scoring under this share of the best is dropped
ANY_COVER = 0.75  # ... and so is one holding fewer than this share of the words the best one holds


def context(db, task, budget=1500, project=None, kind="ctx"):
    """What an agent should read before a task, in one call: the best notes in
    full, best first, then one line for each note they link to, never past
    `budget` tokens. Returns (text, ids)."""
    hits = search(db, task, n=8, project=project)
    if not hits:
        return f"# context: {task}\nno notes match", []
    parts, ids, used = [f"# context: {task}"], [], tokens_of(task) + 4
    if hits[0].get("suggest"):
        parts.append(f"(matched as: {hits[0]['suggest']})")
    if hits[0]["mode"] != "all":
        # no note has every word: a note that shares one common word ("release") with
        # the task is noise next to the one that shares most, so keep only close seconds
        words = [w.lower() for w in query_words(task)]

        def covered(nid):
            text = " ".join(db.execute("select name, description, body from notes where id=?", (nid,)).fetchone()).lower()
            toks = set(re.findall(r"\w+", text))
            return sum(1 for w in words if any(t.startswith(w) for t in toks))

        top, need = hits[0]["score"], math.ceil(ANY_COVER * covered(hits[0]["id"]))
        hits = [hits[0]] + [h for h in hits[1:] if h["score"] >= ANY_KEEP * top and covered(h["id"]) >= need]
    for h in hits:
        body = db.execute("select body from notes where id=?", (h["id"],)).fetchone()[0].strip()
        block = f"\n## {h['id']} - {h['description']}\n{body}"
        cost = tokens_of(block)
        if used + cost <= budget:
            parts.append(block); used += cost; ids.append(h["id"])
            continue
        room = (budget - used) * 4 - len(h["id"]) - len(h["description"]) - 40
        if room > 200:  # worth a cut copy: say so, and how to get the rest
            parts.append(f"\n## {h['id']} - {h['description']}\n{body[:room].rstrip()}\n… (cut; brain g {h['name']})")
            used = budget; ids.append(h["id"])
        break
    related = []
    for nid in ids[:3]:
        for nb in neighbors(db, nid):
            if nb not in ids and nb not in related:
                related.append(nb)
    if related and used < budget:
        parts.append("\n## linked notes (brain g NAME to read)")
        for nb in related:
            desc = db.execute("select description from notes where id=?", (nb,)).fetchone()[0]
            line = f"- {nb} - {desc[:120]}"
            if used + tokens_of(line) > budget:
                break
            parts.append(line); used += tokens_of(line)
    log(db, kind, task, ids)
    return "\n".join(parts), ids


def cmd_ctx(db, args):
    budget, proj, words = 1500, None, []
    it = iter(args)
    for a in it:
        if a in ("--tokens", "-k"): budget = int(next(it))
        elif a == "-p": proj = next(it)
        else: words.append(a)
    if not words:
        sys.exit("usage: brain ctx TASK [--tokens N] [-p PROJECT]")
    print(context(db, " ".join(words), budget, proj)[0])


def shingles(text, k=3):
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(w[i:i + k]) for i in range(max(len(w) - k + 1, 1))} if w else set()


def dupes(db, threshold=0.5):
    """Pairs of notes sharing most of their wording (Jaccard over word 3-grams).
    Counted through an inverted index, so it never compares every pair."""
    sets = {nid: shingles(f"{d}\n{b}") for nid, d, b in db.execute("select id, description, body from notes")}
    holders = {}
    for nid, sh in sets.items():
        for s_ in sh:
            holders.setdefault(s_, []).append(nid)
    shared = {}
    limit = max(20, len(sets) // 10)  # a phrase in a tenth of all notes says nothing about two of them
    for ids in holders.values():
        if 1 < len(ids) <= limit:
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    key = (ids[x], ids[y])
                    shared[key] = shared.get(key, 0) + 1
    out = []
    for (a, b), c in shared.items():
        j = c / (len(sets[a]) + len(sets[b]) - c)
        if j >= threshold:
            out.append((round(j, 2), a, b))
    return sorted(out, reverse=True)


def cmd_dupes(db, args):
    th = float(args[0]) if args else 0.5
    out = dupes(db, th)
    for j, a, b in out:
        print(f"{j:.2f}  {a}  ~  {b}")
    print(f"{len(out)} pairs at {th:.2f} or more alike: merge them, or read both (where they overlap is where they may disagree)")


def cmd_embed(db, args):
    if not EMBED:
        sys.exit("semantic search is off: set BRAIN_EMBED=ollama:<model> (e.g. ollama:nomic-embed-text)")
    try:
        done, kept = embed_all(db)
    except Exception as e:
        sys.exit(f"embedding failed: {type(e).__name__}: {e}")
    print(f"{done} notes embedded, {kept} already current ({EMBED})")


# ---------------------------------------------------------------- MCP (stdio)

MCP_TOOLS = [
    dict(name="brain_search", description="Search the notes. Returns the best notes, one line each with a snippet. "
         "Use first, with a few topic words.",
         inputSchema=dict(type="object", properties=dict(
             query=dict(type="string", description="topic words"),
             n=dict(type="integer", description="how many notes (default 5)"),
             project=dict(type="string"), type=dict(type="string", enum=list(TYPES))), required=["query"])),
    dict(name="brain_get", description="Read one note in full, with the names of the notes it links to.",
         inputSchema=dict(type="object", properties=dict(name=dict(type="string", description="note name or id")),
                          required=["name"])),
    dict(name="brain_context", description="Everything worth knowing before a task, in one call: the best notes in "
         "full plus their linked notes, within a token budget.",
         inputSchema=dict(type="object", properties=dict(
             task=dict(type="string", description="what you are about to do"),
             tokens=dict(type="integer", description="budget (default 1500)"), project=dict(type="string")),
             required=["task"])),
    dict(name="brain_list", description="List every note (id and description), optionally for one project or type.",
         inputSchema=dict(type="object", properties=dict(project=dict(type="string"),
                                                         type=dict(type="string", enum=list(TYPES))))),
]


def mcp_call(db, name, a):
    index(db)
    if name == "brain_search":
        hits = search(db, str(a["query"]), int(a.get("n") or 5), a.get("project"), a.get("type"))
        log(db, "mcp", str(a["query"]), [h["id"] for h in hits])
        return search_text(hits)
    if name == "brain_get":
        out = get_text(db, str(a["name"]), kind="mcp")
        if out is None:
            raise LookupError(f"no such note: {a['name']}")
        return out
    if name == "brain_context":
        return context(db, str(a["task"]), int(a.get("tokens") or 1500), a.get("project"), kind="mcp")[0]
    if name == "brain_list":
        where, params = [], []
        if a.get("project"):
            clause, val = project_filter(db, a["project"]); where.append(clause); params.append(val)
        if a.get("type"):
            where.append("n.type = ?"); params.append(a["type"])
        rows = db.execute("select n.id, n.description from notes n" + (" where " + " and ".join(where) if where else "")
                          + " order by n.id", params).fetchall()
        return "\n".join(f"{i} - {d[:100]}" for i, d in rows) or "no notes"
    raise LookupError(f"unknown tool: {name}")


def mcp_handle(db, msg):
    """One JSON-RPC message in, one reply out (None for a notification)."""
    mid, method, p = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    try:
        if method == "initialize":
            res = dict(protocolVersion=p.get("protocolVersion") or "2025-06-18",
                       capabilities=dict(tools=dict(listChanged=False)),
                       serverInfo=dict(name="second-brain", version=VERSION),
                       instructions="Notes from past work. Call brain_context with the task before starting, "
                                    "or brain_search then brain_get for one fact.")
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = dict(tools=MCP_TOOLS)
        elif method == "tools/call":
            try:
                text = mcp_call(db, p.get("name"), p.get("arguments") or {})
                res = dict(content=[dict(type="text", text=text)], isError=False)
            except (LookupError, KeyError, ValueError, TypeError) as e:
                res = dict(content=[dict(type="text", text=f"error: {e}")], isError=True)
        else:
            return dict(jsonrpc="2.0", id=mid, error=dict(code=-32601, message=f"method not found: {method}"))
        return dict(jsonrpc="2.0", id=mid, result=res)
    except Exception as e:  # one bad request never ends the session
        return dict(jsonrpc="2.0", id=mid, error=dict(code=-32603, message=f"{type(e).__name__}: {e}"))


def cmd_mcp(args):
    db = connect()
    try:
        index(db)
    except Exception as e:
        print(f"index warning: {e}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message="parse error"))
        else:
            reply = mcp_handle(db, msg) if isinstance(msg, dict) else \
                dict(jsonrpc="2.0", id=None, error=dict(code=-32600, message="batches are not supported"))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


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
    for nid, desc, body, mtime, typ in db.execute("select id, description, body, mtime, type from notes"):
        if typ in ("feedback", "user"):
            continue  # a rule says "unmerged" about how to work, not about work left open
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
    ranked = db.execute("select r.id, r.n from recalls r join notes n on n.id=r.id where r.n > 0 "
                        "order by r.n desc, r.id limit 15").fetchall()
    for nid, c in ranked:
        print(f"{c:4d}  {nid}")
    used = db.execute("select count(*) from recalls r join notes n on n.id=r.id where r.n > 0").fetchone()[0]
    unused = db.execute("select count(*) from notes").fetchone()[0] - used
    print(f"{used} notes ever recalled, {unused} never")


def cmd_ls(db, args):
    proj = typ = None
    it = iter(args)
    for a in it:
        if a == "-p": proj = next(it, None)
        elif a == "-t": typ = next(it, None)
    where, params = [], []
    if proj:
        clause, val = project_filter(db, proj); where.append(clause); params.append(val)
    if typ: where.append("n.type = ?"); params.append(typ)
    sql = "select n.id, n.description from notes n" + (" where " + " and ".join(where) if where else "") + " order by n.id"
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
    rc = {i: (c, last) for i, c, last in db.execute("select id, n, last from recalls")}
    pr = dict(db.execute("select id, pr from rank").fetchall())
    for n in nodes:
        c, last = rc.get(n["id"], (0, None))
        n["recalls"], n["last_recall"], n["rank"] = c, last, round(pr.get(n["id"], 1.0), 3)
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
    if a[0] == "mcp":
        return cmd_mcp(a[1:])
    if a[0] in ("-V", "--version", "version"):
        return print(VERSION)
    db = connect()
    index(db, force=a[0] == "index")
    cmds = {"s": cmd_search, "search": cmd_search, "g": cmd_get, "get": cmd_get, "n": cmd_neighbors,
            "ls": cmd_ls, "stale": cmd_stale, "top": cmd_top, "index": cmd_index,
            "orphans": cmd_orphans, "check": cmd_check, "ctx": cmd_ctx, "dupes": cmd_dupes, "embed": cmd_embed}
    if a[0] not in cmds:
        sys.exit(__doc__)
    cmds[a[0]](db, a[1:])


if __name__ == "__main__":
    main()
