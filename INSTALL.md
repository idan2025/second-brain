# Install second-brain

For people and AI agents. Steps are exact; run them in order.

## Requirements

- Python 3.8+ with SQLite FTS5 (`python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')"` must print nothing and exit 0).
- git (for the clone). No pip packages.

## 1. Get the code

```sh
git clone https://github.com/idan2025/second-brain.git ~/second-brain
```

## 2. Put `brain` on PATH

```sh
mkdir -p ~/.local/bin
ln -sf ~/second-brain/brain.py ~/.local/bin/brain
chmod +x ~/second-brain/brain.py
```

If `~/.local/bin` is not on PATH, use `~/bin` or `/usr/local/bin` (may need sudo) instead.

## 3. Point it at notes

Either:

- **Claude Code memory** (default, nothing to set): notes live in `~/.claude/projects/*/memory/*.md`.
- **Any other folder of notes**: set `BRAIN_SRC` to a glob of `.md` files, for example:

  ```sh
  export BRAIN_SRC="$HOME/notes/*.md"
  ```

  Add it to the shell profile (`~/.bashrc`, `~/.zshrc`) to keep it.

A note is a Markdown file. Frontmatter is optional but recommended:

```markdown
---
name: short-kebab-slug
description: one line used for search results
metadata:
  type: user | feedback | project | reference
---

Body text. Link other notes with [[other-slug]].
```

## 4. Verify

```sh
brain check
brain s "any word from your notes"
```

`brain check` prints index warnings, orphans and dangling links. Expected on a new install: no errors. `brain s` prints matching notes, one per line.

## 5. Optional: web UI

```sh
brain serve 7777            # 127.0.0.1 only, open at http://127.0.0.1:7777/
```

Do not use `--lan` unless `~/.config/brain/pw` exists (see README). Without it the UI is open to the network.

## Commands for agents

| Need | Command |
| --- | --- |
| find notes about a topic | `brain s "topic words" -n 5` |
| read one note and its links | `brain g note-name` |
| list all notes | `brain ls` |
| rebuild index | `brain index` |

Notes are read-only to `brain`. Write new notes as plain `.md` files in the source folder; the index updates on the next query.

## Troubleshooting

- `brain: command not found`: step 2's directory is not on PATH. Run `echo $PATH`.
- No results: check `BRAIN_SRC` matches files (`ls $BRAIN_SRC` with the glob expanded by the shell).
- `sqlite3.OperationalError: no such module: fts5`: Python's SQLite lacks FTS5; use a Python build linked to a SQLite with FTS5.

## Uninstall

```sh
rm ~/.local/bin/brain
rm -rf ~/second-brain
rm -f ~/second-brain/brain.db*   # index only; notes are never touched
```
