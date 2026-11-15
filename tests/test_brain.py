#!/usr/bin/env python3
"""Tests for brain.py: ranking, tolerant frontmatter, incremental re-index, orphans, serve auth.

Runs against a throwaway copy of the note layout in a temp dir; never touches
~/.claude or ~/brain/brain.db.   python3 -I tests/test_brain.py
"""
import base64, importlib.util, json, os, shutil, socket, subprocess, sys, tempfile, time, unittest, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN = os.path.join(HERE, "..", "brain.py")


def load_brain(root, src):
    os.environ["BRAIN_ROOT"] = root
    os.environ["BRAIN_DB"] = os.path.join(root, "brain.db")
    os.environ["BRAIN_SRC"] = src
    spec = importlib.util.spec_from_file_location("brain_under_test", BRAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def note(dirp, stem, name=None, desc="", typ="project", body="", raw=None):
    os.makedirs(dirp, exist_ok=True)
    p = os.path.join(dirp, stem + ".md")
    if raw is None:
        raw = f"---\nname: {name or stem}\ndescription: \"{desc}\"\nmetadata:\n  type: {typ}\n---\n{body}\n"
    with open(p, "w") as f:
        f.write(raw)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="brain-test-")
        self.home = os.path.join(self.tmp, "home")
        self.mem = os.path.join(self.home, ".claude/projects/-home-pi-alpha/memory")
        self.mem2 = os.path.join(self.home, ".claude/projects/-home-pi-beta/memory")
        self.root = os.path.join(self.tmp, "root")
        self.src = os.path.join(self.home, ".claude/projects/*/memory/*.md")
        os.makedirs(self.mem); os.makedirs(self.mem2); os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def brain(self):
        b = load_brain(self.root, self.src)
        db = b.connect(); b.index(db)
        return b, db


class Ranking(Base):
    def test_name_beats_description_beats_body_and_why_says_where(self):
        note(self.mem, "udp-hub", desc="the datagram path", body="nothing relevant here")
        note(self.mem, "gate-notes", desc="how udp reaches the gate", body="plain text")
        note(self.mem, "long-log", desc="a log", body="udp " * 40 + "mentioned many times in the body")
        note(self.mem, "unrelated", desc="tcp only", body="tcp tcp tcp")
        b, db = self.brain()
        hits = b.search(db, "udp", n=10)
        ids = [h["id"] for h in hits]
        self.assertEqual(ids[0], "alpha/udp-hub", ids)
        self.assertEqual(ids[1], "alpha/gate-notes", ids)
        self.assertEqual(ids[2], "alpha/long-log", ids)
        self.assertNotIn("alpha/unrelated", ids)
        self.assertEqual(hits[0]["why"], ["name"])
        self.assertEqual(hits[1]["why"], ["description"])
        self.assertEqual(hits[2]["why"], ["body"])

    def test_prefix_and_stemming_match(self):
        note(self.mem, "lan-rooms-dummy-proof", desc="rooms must work for gamers", body="the firewall is opened")
        b, db = self.brain()
        self.assertEqual([h["id"] for h in b.search(db, "dum")], ["alpha/lan-rooms-dummy-proof"])
        self.assertEqual([h["id"] for h in b.search(db, "room")], ["alpha/lan-rooms-dummy-proof"])
        self.assertEqual([h["id"] for h in b.search(db, "gamer firewalls")], ["alpha/lan-rooms-dummy-proof"])

    def test_every_word_first_then_any_word(self):
        note(self.mem, "both", desc="hub and udp together", body="")
        note(self.mem, "one", desc="only the hub", body="")
        b, db = self.brain()
        hits = b.search(db, "hub udp", n=5)
        self.assertEqual([h["id"] for h in hits], ["alpha/both"])
        self.assertEqual(hits[0]["mode"], "all")
        hits = b.search(db, "hub zzzznotaword", n=5)
        self.assertEqual({h["id"] for h in hits}, {"alpha/both", "alpha/one"})
        self.assertEqual(hits[0]["mode"], "any")

    def test_filters_by_type_project_and_recency(self):
        note(self.mem, "a", desc="udp rule", typ="feedback")
        note(self.mem2, "b", desc="udp fact", typ="reference")
        old = note(self.mem, "c", desc="udp old", typ="project")
        os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
        b, db = self.brain()
        self.assertEqual([h["id"] for h in b.search(db, "udp", typ="feedback")], ["alpha/a"])
        self.assertEqual([h["id"] for h in b.search(db, "udp", project="beta")], ["beta/b"])
        self.assertEqual({h["id"] for h in b.search(db, "udp", days=7)}, {"alpha/a", "beta/b"})


class Frontmatter(Base):
    def test_bad_frontmatter_warns_and_never_crashes(self):
        note(self.mem, "fine", desc="a good note", body="ok")
        note(self.mem, "unclosed", raw="---\nname: unclosed\ndescription: half written\n")
        note(self.mem, "garbage", raw="---\nthis line has no colon\nname: garbage\n---\nbody text\n")
        note(self.mem, "empty", raw="")
        note(self.mem, "bare", raw="just prose, no frontmatter at all\n")
        note(self.mem, "badtype", raw="---\nname: badtype\ndescription: x\ntype: banana\n---\nbody\n")
        b, db = self.brain()
        ids = {r[0] for r in db.execute("select id from notes")}
        self.assertEqual(ids, {"alpha/fine", "alpha/unclosed", "alpha/garbage", "alpha/empty", "alpha/bare", "alpha/badtype"})
        warns = {}
        for nid, _, w in b.warnings(db):
            warns[nid] = warns.get(nid, "") + " | " + w
        self.assertIn("never closed", warns["alpha/unclosed"])
        self.assertIn("line 2", warns["alpha/garbage"])
        self.assertIn("empty file", warns["alpha/empty"])
        self.assertIn("no frontmatter", warns["alpha/bare"])
        self.assertIn("banana", warns["alpha/badtype"])
        self.assertNotIn("alpha/fine", warns)
        # the half-written note is still searchable by what it has
        self.assertEqual([h["id"] for h in b.search(db, "half written")], ["alpha/unclosed"])
        # garbage line did not stop the name from being read
        self.assertEqual(db.execute("select name from notes where id='alpha/garbage'").fetchone()[0], "garbage")

    def test_metadata_type_and_top_level_type_both_read(self):
        note(self.mem, "nested", typ="feedback")
        note(self.mem, "flat", raw="---\nname: flat\ndescription: d\ntype: reference\n---\nb\n")
        b, db = self.brain()
        self.assertEqual(db.execute("select type from notes where id='alpha/nested'").fetchone()[0], "feedback")
        self.assertEqual(db.execute("select type from notes where id='alpha/flat'").fetchone()[0], "reference")


class Incremental(Base):
    def test_only_changed_files_are_reparsed_and_removed_ones_go(self):
        a = note(self.mem, "a", desc="first", body="see [[b]]")
        bp = note(self.mem, "b", desc="second", body="")
        b, db = self.brain()
        self.assertEqual(db.execute("select count(*) from links").fetchone()[0], 1)
        self.assertFalse(b.index(db), "nothing changed: index must take the fast path")
        time.sleep(0.02)
        with open(bp, "a") as f:
            f.write("now with an extra line about tokens\n")
        os.utime(bp, None)
        self.assertEqual(b.index(db), 1, "exactly one file changed")
        self.assertEqual([h["id"] for h in b.search(db, "tokens")], ["alpha/b"])
        os.remove(bp)
        self.assertEqual(b.index(db), 1)
        self.assertEqual({r[0] for r in db.execute("select id from notes")}, {"alpha/a"})
        self.assertEqual(db.execute("select count(*) from links").fetchone()[0], 0)
        self.assertEqual([(s, t) for s, t in db.execute("select src, target from dangling")], [("alpha/a", "b")])

    def test_schema_change_rebuilds_but_keeps_events(self):
        note(self.mem, "a", desc="x")
        b, db = self.brain()
        b.log(db, "search", "x", ["alpha/a"])
        db.execute("update state set v='0' where k='schema'"); db.commit(); db.close()
        b2 = load_brain(self.root, self.src)
        db = b2.connect(); b2.index(db)
        self.assertEqual(db.execute("select count(*) from notes").fetchone()[0], 1)
        self.assertEqual(db.execute("select count(*) from events").fetchone()[0], 1)


class Graph(Base):
    def test_orphans_listed_and_dangling(self):
        note(self.mem, "hub", desc="h", body="links to [[spoke]] and [[missing-one]]")
        note(self.mem, "spoke", desc="s", body="")
        note(self.mem, "lonely", desc="l", body="")
        note(self.mem, "indexed-only", desc="i", body="")
        with open(os.path.join(self.mem, "MEMORY.md"), "w") as f:
            f.write("# index\n- [hub](hub.md)\n- [indexed-only](indexed-only.md)\n")
        b, db = self.brain()
        lone, index_only, dang = b.orphans(db)
        self.assertEqual(lone, ["alpha/lonely"])
        self.assertEqual(index_only, ["alpha/hub", "alpha/indexed-only"])
        self.assertEqual(dang, [("alpha/hub", "missing-one")])
        self.assertEqual(db.execute("select count(*) from notes where id like '%MEMORY%'").fetchone()[0], 0)

    def test_markdown_links_count_as_links(self):
        note(self.mem, "a", desc="x", body="see [the other](b.md)")
        note(self.mem, "b", desc="y", body="")
        b, db = self.brain()
        self.assertEqual(b.neighbors(db, "alpha/a"), ["alpha/b"])


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Serve(Base):
    def start(self, with_pw):
        env = dict(os.environ, HOME=self.home, BRAIN_ROOT=self.root, BRAIN_DB=os.path.join(self.root, "brain.db"), BRAIN_SRC=self.src)
        for k in ("BRAIN_ROOT", "BRAIN_DB", "BRAIN_SRC"):
            env[k] = env[k]
        # the server reads ui.html and static/ from BRAIN_ROOT: point it at the real ones
        for f in ("ui.html", "ui3d.html"):
            shutil.copy(os.path.join(HERE, "..", f), self.root)
        shutil.copytree(os.path.join(HERE, "..", "static"), os.path.join(self.root, "static"))
        pw = None
        if with_pw:
            pw = base64.b64encode(os.urandom(12)).decode()
            os.makedirs(os.path.join(self.home, ".config/brain"))
            with open(os.path.join(self.home, ".config/brain/pw"), "w") as f:
                f.write(pw + "\n")
            os.chmod(os.path.join(self.home, ".config/brain/pw"), 0o600)
        port = free_port()
        self.proc = subprocess.Popen([sys.executable, "-I", BRAIN, "serve", str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=.2).close(); break
            except OSError:
                time.sleep(.1)
        return port, pw

    def get(self, port, path, pw=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if pw is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"anyone:{pw}".encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), b""

    def tearDown(self):
        if getattr(self, "proc", None):
            self.proc.kill(); self.proc.wait(); self.proc.stdout.close()
        super().tearDown()

    def test_password_file_locks_every_route_and_any_username_works(self):
        note(self.mem, "a", desc="udp", body="hello")
        port, pw = self.start(with_pw=True)
        for path in ("/", "/3d", "/graph", "/search?q=udp", "/note/alpha%2Fa", "/api/note/alpha%2Fa", "/static/brain.css", "/events", "/check", "/health"):
            code, hdr, _ = self.get(port, path)
            self.assertEqual(code, 401, path)
            self.assertIn("Basic", hdr.get("WWW-Authenticate", ""), path)
        self.assertEqual(self.get(port, "/graph", pw="wrong")[0], 401)
        code, hdr, body = self.get(port, "/graph", pw=pw)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["nodes"][0]["id"], "alpha/a")
        code, hdr, body = self.get(port, "/", pw=pw)
        self.assertEqual(code, 200)
        self.assertIn("script-src 'self'", hdr.get("Content-Security-Policy", ""))
        self.assertNotIn("unsafe-inline", hdr["Content-Security-Policy"].split("style-src")[0])
        self.assertNotIn("Access-Control-Allow-Origin", hdr)
        self.assertEqual(hdr.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(self.get(port, "/static/../brain.py", pw=pw)[0], 404)
        self.assertEqual(self.get(port, "/static/d3.LICENSE.txt", pw=pw)[0], 200)
        code, _, body = self.get(port, "/search?q=udp&log=0", pw=pw)
        self.assertEqual(json.loads(body)["hits"][0]["why"], ["description"])

    def test_no_password_file_means_open(self):
        note(self.mem, "a", desc="udp", body="hello")
        port, _ = self.start(with_pw=False)
        self.assertEqual(self.get(port, "/health")[0], 200)
        self.assertEqual(self.get(port, "/graph")[0], 200)

    def test_a_bad_note_arriving_while_serving_does_not_take_the_server_down(self):
        note(self.mem, "a", desc="udp", body="hello")
        port, _ = self.start(with_pw=False)
        self.assertEqual(self.get(port, "/graph")[0], 200)
        note(self.mem, "broken", raw="---\nname: broken\n: : :\n\x00\xff garbage")
        os.utime(os.path.join(self.mem, "broken.md"), (time.time() + 1, time.time() + 1))
        code, _, body = self.get(port, "/graph")
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)["nodes"]), 2)
        code, _, body = self.get(port, "/check")
        self.assertTrue(any(w["id"] == "alpha/broken" for w in json.loads(body)["warnings"]))


class Sources(Base):
    def test_globstar_recurses_and_several_globs_combine(self):
        notes = os.path.join(self.tmp, "notes")
        note(os.path.join(notes, "deep", "er"), "zebra-fact", desc="striped animals")
        other = os.path.join(self.tmp, "other")
        note(other, "okapi-fact", desc="a forest giraffe")
        b = load_brain(self.root, os.path.join(notes, "**", "*.md") + os.pathsep + os.path.join(other, "*.md"))
        db = b.connect(); b.index(db)
        self.assertEqual([h["id"] for h in b.search(db, "striped")], ["er/zebra-fact"])
        self.assertEqual([h["id"] for h in b.search(db, "giraffe")], ["other/okapi-fact"])

    def test_windows_paths_group_and_name_like_posix_ones(self):
        b = load_brain(self.root, self.src)
        self.assertEqual(b.project_of(r"C:\Users\ana\.claude\projects\C--Users-ana-shop\memory\x.md"), "shop")
        self.assertEqual(b.project_of(r"D:\notes\work\x.md"), "work")
        self.assertEqual(b.stem_of(r"D:\notes\work\my-note.md"), "my-note")
        self.assertTrue(b.is_index_file(r"C:\a\memory\MEMORY.md"))

    def test_a_like_wildcard_in_a_name_is_literal(self):
        note(self.mem, "a_b", desc="underscore")
        note(self.mem, "axb-longer", desc="would match a_b as a wildcard")
        b, db = self.brain()
        self.assertEqual(b.resolve(db, "a_"), "alpha/a_b")
        self.assertIsNone(b.resolve(db, "a%z"))


class Yaml(Base):
    def test_block_scalars_continuations_and_lists(self):
        note(self.mem, "folded", raw="---\nname: folded\ndescription: >-\n  first line\n  second line\nmetadata:\n  type: user\n---\nbody\n")
        note(self.mem, "literal", raw="---\nname: literal\ndescription: |\n  one\n  two\n---\nbody\n")
        note(self.mem, "plain", raw="---\nname: plain\ndescription: starts here\n  and goes on\n---\nbody\n")
        note(self.mem, "listy", raw="---\nname: listy\ndescription: d\ntags:\n  - red\n  - blue\n---\nbody\n")
        b, db = self.brain()
        d = dict(db.execute("select name, description from notes"))
        self.assertEqual(d["folded"], "first line second line")
        self.assertEqual(d["literal"], "one\ntwo")
        self.assertEqual(d["plain"], "starts here and goes on")
        self.assertEqual(db.execute("select type from notes where name='folded'").fetchone()[0], "user")
        self.assertEqual(b.parse_frontmatter("tags:\n  - red\n  - blue\n")[0]["tags"], "red, blue")
        self.assertEqual(b.warnings(db), [])


class Efficiency(Base):
    def test_an_up_to_date_index_is_never_written(self):
        note(self.mem, "a", desc="x")
        b, db = self.brain(); db.close()
        db = b.connect()
        before = db.total_changes
        b.index(db)
        self.assertEqual(db.total_changes, before, "connect + unchanged index must write nothing")

    def test_the_log_is_bounded_and_counts_survive_pruning(self):
        note(self.mem, "a", desc="x")
        b, db = self.brain()
        b.EVENTS_KEEP = 50
        for _ in range(300):
            b.log(db, "search", "x", ["alpha/a"])
        self.assertLessEqual(db.execute("select count(*) from events").fetchone()[0], 150)
        self.assertEqual(db.execute("select n from recalls where id='alpha/a'").fetchone()[0], 300)

    def test_counts_are_seeded_from_an_old_log(self):
        note(self.mem, "a", desc="x")
        b, db = self.brain()
        b.log(db, "get", "a", ["alpha/a"]); b.log(db, "view", "a", ["alpha/a"])
        db.execute("delete from recalls"); db.execute("update state set v='0' where k='schema'"); db.commit(); db.close()
        b2 = load_brain(self.root, self.src)
        db = b2.connect()
        self.assertEqual(db.execute("select n from recalls where id='alpha/a'").fetchone()[0], 1)


class Smarter(Base):
    def test_a_typo_finds_the_note_and_says_what_it_searched(self):
        note(self.mem, "gate", desc="the firewall rules", body="datagrams pass the gateway")
        b, db = self.brain()
        hits = b.search(db, "firewal rulez")
        self.assertEqual([h["id"] for h in hits], ["alpha/gate"])
        self.assertEqual(hits[0]["mode"], "fuzzy")
        self.assertIn("rule", hits[0]["suggest"])

    def test_common_words_do_not_decide_a_query(self):
        note(self.mem, "release-flow", desc="how a client release is cut", body="tag and publish")
        note(self.mem, "the-the", desc="the the the how to", body="the to how")
        b, db = self.brain()
        self.assertEqual([h["id"] for h in b.search(db, "how to release the client")], ["alpha/release-flow"])
        self.assertEqual(b.query_words("the"), ["the"], "a query of only common words still searches")

    def test_an_exact_project_name_is_not_a_substring(self):
        note(self.mem, "a", desc="udp")
        alphabet = os.path.join(self.home, ".claude/projects/-home-pi-alphabet/memory")
        note(alphabet, "b", desc="udp")
        b, db = self.brain()
        self.assertEqual([h["id"] for h in b.search(db, "udp", project="alpha")], ["alpha/a"])
        self.assertEqual({h["id"] for h in b.search(db, "udp", project="alph")}, {"alpha/a", "alphabet/b"})

    def test_a_hub_others_cite_outranks_an_equal_note(self):
        note(self.mem, "hub-udp", desc="udp notes", body="")
        note(self.mem, "side-udp", desc="udp notes", body="")
        for i in range(4):
            note(self.mem, f"cite{i}", desc="c", body="see [[hub-udp]]")
        b, db = self.brain()
        self.assertEqual(b.search(db, "udp", n=2)[0]["id"], "alpha/hub-udp")

    def test_recalls_break_a_tie(self):
        note(self.mem, "one-udp", desc="udp notes", body="")
        note(self.mem, "two-udp", desc="udp notes", body="")
        b, db = self.brain()
        for _ in range(20):
            b.log(db, "get", "x", ["alpha/two-udp"])
        self.assertEqual(b.search(db, "udp", n=2)[0]["id"], "alpha/two-udp")

    def test_context_fits_its_budget_and_names_linked_notes(self):
        note(self.mem, "deploy", desc="how to deploy", body="step " * 200 + "see [[rollback]]")
        note(self.mem, "rollback", desc="undo a deploy", body="restore the backup")
        note(self.mem, "unrelated", desc="cats", body="meow")
        b, db = self.brain()
        text, ids = b.context(db, "deploy", budget=400)
        self.assertLessEqual(b.tokens_of(text), 420)
        self.assertEqual(ids[0], "alpha/deploy")
        self.assertIn("rollback", text)
        self.assertNotIn("meow", text)
        full, _ = b.context(db, "deploy", budget=5000)
        self.assertIn("restore the backup", full)

    def test_near_duplicates_are_found_and_distinct_notes_are_not(self):
        text = "the hub gate listens on udp port 4790 and falls back to tcp when udp does not answer"
        note(self.mem, "a", desc="x", body=text)
        note(self.mem, "b", desc="x", body=text + " within three seconds")
        note(self.mem, "c", desc="y", body="minecraft worlds are backed up before every import of a new world")
        b, db = self.brain()
        pairs = b.dupes(db, 0.5)
        self.assertEqual([(a_, b_) for _, a_, b_ in pairs], [("alpha/a", "alpha/b")])


class Mcp(Base):
    def test_the_stdio_server_speaks_mcp(self):
        note(self.mem, "udp-hub", desc="the datagram path", body="port 4790, see [[gate]]")
        note(self.mem, "gate", desc="the gate", body="rules")
        env = dict(os.environ, BRAIN_ROOT=self.root, BRAIN_DB=os.path.join(self.root, "brain.db"), BRAIN_SRC=self.src)
        msgs = [dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2025-06-18", capabilities={},
                                                                          clientInfo=dict(name="t", version="1"))),
                dict(jsonrpc="2.0", method="notifications/initialized"),
                dict(jsonrpc="2.0", id=2, method="tools/list"),
                dict(jsonrpc="2.0", id=3, method="tools/call", params=dict(name="brain_search", arguments=dict(query="datagram"))),
                dict(jsonrpc="2.0", id=4, method="tools/call", params=dict(name="brain_get", arguments=dict(name="udp-hub"))),
                dict(jsonrpc="2.0", id=5, method="tools/call", params=dict(name="brain_context", arguments=dict(task="udp hub"))),
                dict(jsonrpc="2.0", id=6, method="tools/call", params=dict(name="brain_get", arguments=dict(name="nope-none"))),
                dict(jsonrpc="2.0", id=7, method="bogus")]
        r = subprocess.run([sys.executable, "-I", BRAIN, "mcp"], env=env, capture_output=True, text=True,
                           input="\n".join(json.dumps(m) for m in msgs) + "\nnot json\n", timeout=30)
        out = [json.loads(l) for l in r.stdout.splitlines()]
        by = {m.get("id"): m for m in out}
        self.assertEqual(len(out), 8, r.stdout + r.stderr)  # 7 requests + the parse error; no reply to the notification
        self.assertEqual(by[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual({t["name"] for t in by[2]["result"]["tools"]}, {"brain_search", "brain_get", "brain_context", "brain_list"})
        self.assertIn("alpha/udp-hub", by[3]["result"]["content"][0]["text"])
        self.assertIn("linked: alpha/gate", by[4]["result"]["content"][0]["text"])
        self.assertIn("port 4790", by[5]["result"]["content"][0]["text"])
        self.assertTrue(by[6]["result"]["isError"])
        self.assertEqual(by[7]["error"]["code"], -32601)
        self.assertEqual(by[None]["error"]["code"], -32700)


class Semantic(Base):
    def test_a_note_close_in_meaning_is_found_without_a_shared_word(self):
        import http.server, threading
        # a stand-in embedding server: vectors by topic words, so "puppy" lands near "dog"
        topics = [("dog", "puppy", "canine"), ("ship", "boat", "vessel")]

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                vecs = [[float(sum(t.lower().count(w) for w in ws)) + 0.01 for ws in topics] for t in req["input"]]
                data = json.dumps(dict(embeddings=vecs)).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            note(self.mem, "pets", desc="our dog", body="the dog sleeps")
            note(self.mem, "harbour", desc="boats", body="a boat in port")
            os.environ["BRAIN_EMBED"] = "ollama:fake"
            os.environ["BRAIN_OLLAMA"] = f"http://127.0.0.1:{srv.server_port}"
            b, db = self.brain()
            self.assertEqual(b.embed_all(db), (2, 0))
            self.assertEqual(b.embed_all(db), (0, 2), "unchanged notes are not embedded again")
            hits = b.search(db, "puppy")
            self.assertEqual(hits[0]["id"], "alpha/pets")
            self.assertEqual(hits[0]["mode"], "semantic")
            os.environ["BRAIN_OLLAMA"] = "http://127.0.0.1:9"  # server gone: plain search still works
            b2 = load_brain(self.root, self.src)
            self.assertEqual([h["id"] for h in b2.search(b2.connect(), "dog")], ["alpha/pets"])
        finally:
            os.environ.pop("BRAIN_EMBED", None); os.environ.pop("BRAIN_OLLAMA", None)
            srv.shutdown(); srv.server_close()


class Cli(Base):
    def run_cli(self, *args):
        env = dict(os.environ, BRAIN_ROOT=self.root, BRAIN_DB=os.path.join(self.root, "brain.db"), BRAIN_SRC=self.src)
        return subprocess.run([sys.executable, "-I", BRAIN, *args], env=env, capture_output=True, text=True)

    def test_existing_commands_keep_their_shape_and_exit_codes(self):
        note(self.mem, "udp-hub", desc="the datagram path", body="see [[spoke]]")
        note(self.mem, "spoke", desc="a spoke", body="")
        r = self.run_cli("s", "udp"); self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout.splitlines()[0], "alpha/udp-hub - the datagram path")
        r = self.run_cli("s", "zzzznothing"); self.assertEqual(r.returncode, 0); self.assertEqual(r.stdout.strip(), "no match")
        r = self.run_cli("s", "udp", "-w"); self.assertIn("matched in name", r.stdout)
        r = self.run_cli("g", "udp-hub"); self.assertEqual(r.returncode, 0); self.assertTrue(r.stdout.startswith("# alpha/udp-hub  (")); self.assertIn("linked: alpha/spoke", r.stdout)
        r = self.run_cli("g", "nope-nothing"); self.assertEqual(r.returncode, 1); self.assertIn("no such note", r.stderr)
        r = self.run_cli("n", "udp-hub"); self.assertEqual(r.stdout.strip(), "alpha/spoke - a spoke")
        r = self.run_cli("ls"); self.assertEqual(r.stdout.splitlines(), ["alpha/spoke - a spoke", "alpha/udp-hub - the datagram path"])
        r = self.run_cli("stale"); self.assertEqual(r.returncode, 0); self.assertTrue(r.stdout.strip().endswith("verify, then fix or delete"))
        r = self.run_cli("top"); self.assertEqual(r.returncode, 0); self.assertIn("notes ever recalled", r.stdout)
        r = self.run_cli("index"); self.assertEqual(r.stdout.strip(), "2 notes, 1 links")
        r = self.run_cli("orphans"); self.assertEqual(r.returncode, 0); self.assertIn("alpha/udp-hub", r.stdout)
        r = self.run_cli("check"); self.assertEqual(r.returncode, 0); self.assertTrue(r.stdout.startswith("2 notes, 1 links"))
        r = self.run_cli("bogus"); self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
