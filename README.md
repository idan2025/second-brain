# second-brain

On-demand recall over Markdown memory notes. Stdlib Python only, no install.

Notes (one fact per `.md` file, YAML-style frontmatter with `name`, `description`,
`metadata.type`) stay the source of truth. `brain` indexes them into SQLite FTS5,
builds a link graph from `[[name]]` links, answers compact queries and logs every
access so a live web UI can show recall as it happens. Notes are only read, never written.

By default it reads Claude Code's per-project memory (`~/.claude/projects/*/memory/*.md`).
Point it at any folder of notes with `BRAIN_SRC`.

## Use

```
brain s QUERY [-p PROJECT] [-t TYPE] [--days N] [-n N] [-v] [-w] [--json]   search
brain g NAME [--head N]        a note and its linked notes
brain n NAME                   linked notes only
brain ls [-p PROJECT] [-t TYPE]
brain stale [DAYS]             old notes that talk about open/pending work
brain orphans | check | top | index
brain serve [PORT] [--lan]     live graph UI (127.0.0.1:7777; /3d for three.js)
```

Install (people and AI agents): see [INSTALL.md](INSTALL.md).

## Config (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRAIN_SRC` | `~/.claude/projects/*/memory/*.md` | glob of note files |
| `BRAIN_ROOT` | the script's folder | where `ui*.html` and `static/` live |
| `BRAIN_DB` | `$BRAIN_ROOT/brain.db` | index and access log |
| `BRAIN_PREFER_PROJECT` | none | project whose note wins when two share a name |

## Web UI and auth

`brain serve` binds loopback. `--lan` binds all interfaces; if `~/.config/brain/pw`
exists, HTTP Basic auth (any username, that file's contents as password) is required.
**With no password file the UI is open to your network.** The UI shows your notes,
so set a password before using `--lan`. d3 and 3d-force-graph are bundled in `static/`
(licences alongside), so it works offline.

## Test

```
python3 -I tests/test_brain.py
```

Tests run against a throwaway note layout, never your real notes or index.

## Licence

MIT. Bundled d3 (ISC) and 3d-force-graph (MIT) keep their own licences in `static/`.
