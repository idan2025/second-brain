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
brain ctx TASK [--tokens N]    context bundle for a task, within a token budget
brain n NAME                   linked notes only
brain ls [-p PROJECT] [-t TYPE]
brain stale [DAYS]             old notes that talk about open/pending work
brain dupes [THRESHOLD]        near-duplicate notes
brain orphans | check | top | index
brain embed                    embeddings for semantic search (optional)
brain mcp                      MCP server on stdio, for AI tools
brain serve [PORT] [--lan]     live graph UI (127.0.0.1:7777; /3d for three.js)
```

Install (people and AI agents): see [INSTALL.md](INSTALL.md).

## How search ranks

1. Every word must match (stemmed, prefixes count). Common words like "the" and "how" are dropped.
2. Failing that, any word.
3. If a word is misspelt, close spellings from the notes' own vocabulary (`did you mean`).
4. Within that: a match in the name beats the description, which beats the body. Then bm25, boosted
   for notes many others link to (PageRank over `[[links]]`) and notes recalled often.
5. `ctx` on an any-word match keeps only notes close to the best one (a quarter of its score and
   three quarters of the words it holds), so one shared common word does not pull in an unrelated note.
   Links inside `code` are not links. `stale` skips feedback and user notes (rules, not status).
6. With semantic search on, notes close in meaning are fused in (reciprocal rank fusion), so
   "puppy" can find a note that only says "dog".

## Use from AI tools (MCP)

`brain mcp` is a Model Context Protocol server on stdio with four tools: `brain_search`,
`brain_get`, `brain_context` and `brain_list`. Claude Code:

```
claude mcp add brain -- brain mcp
```

Any other MCP client (Claude Desktop, Cursor, and others) takes the same command in its JSON config:

```json
{ "mcpServers": { "brain": { "command": "brain", "args": ["mcp"] } } }
```

Tools that cannot speak MCP can run the CLI: `brain ctx "the task"` is the one call to make.

## Config (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRAIN_SRC` | `~/.claude/projects/*/memory/*.md` | glob of note files |
| `BRAIN_ROOT` | the script's folder | where `ui*.html` and `static/` live |
| `BRAIN_DB` | `$BRAIN_ROOT/brain.db` | index and access log |
| `BRAIN_PREFER_PROJECT` | none | project whose note wins when two share a name |
| `BRAIN_EMBED` | off | `ollama:<model>` turns on semantic search, e.g. `ollama:nomic-embed-text` |
| `BRAIN_OLLAMA` | `http://127.0.0.1:11434` | Ollama address |
| `BRAIN_EVENTS_KEEP` | `5000` | raw access-log rows kept; per-note counts are kept forever |

`BRAIN_SRC` takes several globs separated by `:` (`;` on Windows), and `**` recurses:
`BRAIN_SRC="$HOME/notes/**/*.md:$HOME/work/*.md"`.

## Semantic search (optional)

Off by default; plain search never needs it. With [Ollama](https://ollama.com):

```
ollama pull nomic-embed-text
export BRAIN_EMBED=ollama:nomic-embed-text
brain embed            # again after notes change; only changed notes are re-embedded
```

If Ollama is down, search quietly falls back to words only.

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
