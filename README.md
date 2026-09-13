# Journal Analyzer

A command-line tool for asking questions about an Obsidian-style Markdown journal. It runs everything locally through [Ollama](https://ollama.com), so your journal stays on your machine.

The tool never writes to your `.md` files. What it learns goes into a SQLite cache you can delete and rebuild whenever you like. For each dated entry it stores a mood score, some tags and a one-line summary written the way you'd write it yourself ("Hiked with John and felt calmer after", not "The author expresses..."). It also reads your undated reference notes about people, places and topics, and keeps track of how they link to your entries. Answers cite Obsidian wikilinks like `[[2024-01-15]]` or `[[John Doe]]`, and if the stored summaries aren't enough, it goes back and reads the full text of whatever it's citing.

## How it works

Each file is tracked by its sha256 hash, so re-running the indexer is cheap and a killed run just picks up where it stopped. Backfill and `journal watch` use the same code for each file.

The whole journal never goes into a model's context at once. Aggregate questions like "how was my mood in March?" are answered straight from SQL. Factual questions use hybrid search over a small set of candidates: sqlite-vec vectors plus FTS5 keyword search, merged with reciprocal-rank fusion. Narrative questions, and questions covering more than a couple of months, also get the month digests (or the year digests, when there would be too many months).

Links decide who and what an entry is about. If an entry links to `[[John Doe]]`, it mentions John Doe, and no model has to guess. When you ask something, the model sees the matching entries along with the notes your question names, the notes closest to it in meaning, and the notes those entries link to.

Folder and file names count too. Any model that reads a file is told where it sits in the vault, like `Links/People/John Doe.md` or `Trips/Lisbon/2024-03-05.md`, so a note filed as a person is read as one even if its text never says so.

There are three models. A fast one extracts entries, summarizes notes and routes questions. A precise one handles synthesis and digests. An embedding model produces the vectors.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally (`ollama serve`)
- Three pulled models (you can change the names in `config.toml`):

```sh
ollama pull llama3.2:3b            # fast model: extraction and routing
ollama pull qwen3:14b              # precise model: synthesis and digests
ollama pull qwen3-embedding:0.6b   # embedding model
```

## Install

```sh
git clone https://github.com/IDGBAN/Commonplace.git && cd Commonplace
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Configure

```sh
cp config.example.toml config.toml
```

Then open `config.toml` and set these:

- `[vault] path` is your journal folder. It's only ever read.
- `date_source` is where each entry's date comes from: `"filename"` for a file named like `2024-01-15.md`, `"frontmatter"` for a `date:` key, or `"heading"` for the first heading that parses as a date.
- `split_heading_regex` splits a file that holds several dated entries. Set it to something like `'^## \d{4}-\d{2}-\d{2}'`.
- `ignore_patterns` lists folders and files to skip, like `[".stversions", ".stfolder", ".smart-env", ".obsidian"]`. A name without a `/` matches anywhere in the tree. A pattern with a `/` is matched against the path from the vault root. `*` and `?` work in both.
- `[notes] paths` points at the folders with your undated reference notes, e.g. `["Links"]`. See [Reference notes](#reference-notes).
- `[ollama]` has the host, the three model names, whether the fast and precise models think before answering (`fast_model_think`, `precise_model_think`), the embedding context window (`embed_num_ctx`) and the request timeout. The chat models run at Ollama's default context window. When a prompt won't fit (a very long entry, a big pile of digests), the request asks for a bigger window instead of letting the prompt get cut, and Ollama reloads the model at that size. Thinking is off for the fast model by default, because on a reasoning model it makes extraction a lot slower without helping much. If you change `embed_model` later you'll have to rebuild the cache, since vectors from different models can't be compared.
- `[db] path` is where the SQLite cache goes.
- `[reranking]` is a cross-encoder that reorders search results before they're used for an answer. It makes results more relevant, but it downloads its model from the internet the first time it runs. If that fails, searches fall back to the fusion ranking and print a warning once. Set `enabled = false` to turn it off.

Every command takes `--config PATH` and defaults to `./config.toml`.

## Usage

Check the setup, index everything once, then start asking.

```sh
# setup
journal doctor            # check the vault folder, Ollama, and that the models are pulled
journal init              # create the DB schema and detect the embedding size

# indexing (safe to stop and re-run)
journal index             # index the vault, skipping unchanged files
journal index --force     # re-process everything
journal stats             # entries, notes, links, date range, DB size, errored and future-dated files

# questions
journal ask "When did I go hiking with John?"
journal ask "Who is John, and when did I last see her?"
journal ask "How did my mood change over spring?"     # answered from SQL

# digests
journal rollup                # write missing or stale month, year, then week digests
journal rollup --rebuild-all  # regenerate all of them

# live updates
journal watch             # keeps running and indexes notes as you save them

# reflection
journal chat              # Q&A that remembers the last few turns
journal insights          # mood, tag and timing patterns you didn't ask about
journal prompt            # ideas for what to write next, from threads left open
```

Each digest is one call to the precise model, so on a CPU a full `journal rollup` can take hours. Month and year digests are written first because those are what answers use, so if you stop a run partway they're already there.

`journal ask` shows which step it's on while it works. At the end it says which stage produced the answer: `aggregate` (plain SQL), `fast_summary` (the fast model reading stored summaries and notes) or `precise_raw` (the precise model re-reading the full text of the cited entries and notes).

### Browsing commands

These read the cache and print tables. No model writes anything for them, so they come back right away. `search` is the only one that uses a model, and only to embed your query.

```sh
journal search "hiking with John"        # keyword and semantic search over entries and notes
journal search "garden" --since 2024-03-01 --until 2024-06-30
journal show 2024-01-15                  # one day's mood, tags, linked notes and summary
journal show 2024-01-15 --raw            # same, plus the stored entry text
journal note "John Doe"               # a reference note and the entries that link to it
journal tags --limit 30                  # most used tags
journal entities --type people           # most-linked notes, optionally of one kind
journal mood --since 2023-01-01          # average mood per month, with a bar chart
journal streaks                          # days written, words, longest run
journal on-this-day                      # what you wrote on this date in earlier years
journal similar 2024-01-15               # closest entries by stored embedding
journal export --format markdown --out ~/journal-export.md
```

`search`, `tags`, `entities`, `mood`, `insights` and `export` take `--since` and `--until` as ISO dates. `journal export` writes JSON by default, includes the full entry text with `--include-raw`, and won't write to an `--out` path inside the vault.

### Reflection commands

`journal chat` is `ask` with memory. It keeps the last few turns so follow-ups work: ask "How was March?", then "what about the month after that?", and the second question gets rewritten into a standalone one before the usual retrieval runs. Each answer still shows which stage produced it. Ctrl-C while it's answering drops that answer and keeps the conversation going. Type `exit` or press Ctrl-D to leave.

`journal insights` runs statistics over the cache in plain SQL, then has the model describe only the findings that pass a threshold: tags and linked notes whose average mood differs from your baseline, weekday and seasonal patterns, and themes that show up together. These are correlations, not causes, and raw entries are never sent to a model. Use `--since` and `--until` (ISO dates) to limit the range, and set the thresholds in `[insights]` (`min_sample_size`, `mood_deviation_threshold`).

`journal prompt` looks back over recent entries (14 days by default, or `--days N`) and suggests open-ended writing prompts that pick up loose threads, like a project you named, a person who keeps coming up, or a feeling you left unresolved. `--count N` sets how many. It only reads the cache and never touches your notes.

## Reference notes

Point `[notes] paths` at the folders that hold your undated notes, for example `paths = ["Links"]`. A note's kind is the folder directly under that path, so `Links/People/John Doe.md` is a `people` note and `Links/Topics-Things/Chess.md` is a `topics-things` note. Single files work too: `paths = ["Links", "Lyrics.md"]`.

For each note, the cache keeps:

- its aliases from the frontmatter, so a question about "John" finds `John Doe`
- its other properties, like `Relationship: Friend`, which are searchable and show up in answers
- a summary. Short notes are used as they are, and only notes longer than `summarize_over_chars` (280 by default) cost a model call
- the links it and your entries make, resolved the way Obsidian does it: by file name, ignoring case, folders, headings and display text

Entities come only from links. `journal entities`, the name filter in answers and the per-person mood patterns in `journal insights` all reflect exactly what you linked. A link to a note that doesn't exist yet starts counting as soon as you create the note. Links to attachments (images, video, PDFs) are ignored, and embedded attachments are left out of the indexed text.

If you add a folder to `paths` and run `journal index` again, the files in it switch from entries to notes even if they haven't changed.

## Starting over

The DB is only a cache, so a full rebuild is:

```sh
rm ~/.journal_analyzer/journal.db
journal init && journal index && journal rollup --rebuild-all
```

If an upgrade changes the cache format, commands will say so and tell you which file to delete, instead of misreading the old one.

## Tests and linting

```sh
python -m pytest
python -m ruff check .
python -m mypy
```

Every Ollama call in the tests is mocked and nothing touches the network, so they run offline. pytest, ruff and mypy all come with the `dev` extra (`pip install -e ".[dev]"`).

## Good to know

- The vault is only ever opened for reading. Nothing gets written back to your notes.
- A bad file doesn't stop a run. It's marked `error` in the `files` table (you'll see it in `journal stats`) and retried next time.
- If `journal index` gets killed, the next run carries on from where it stopped. Reference notes are indexed first so links resolve early.
- Files you delete, rename or start ignoring while `journal watch` isn't running are dropped from the cache by the next `journal index`. If the vault folder turns up empty (an unplugged drive, a sync that hasn't finished), nothing is dropped.
- If an edit makes a file's extraction or embedding fail, its old entries stay in the cache until a later run succeeds.
- `journal watch` keeps going when Ollama restarts. Changed files stay queued and get retried.
- `journal watch` handles whole folders as well. A folder that's deleted or moved out of the vault (which is what the Recycle Bin does) takes its files out of the cache, even though Windows only reports the folder.
- On Linux and macOS, only you can read the cache file and the folder it creates.
- If you switch embedding models, the cache notices (it records the model name and vector width) and tells you how to rebuild, rather than mixing vectors that can't be compared.
- Everything except reranking works with no network at all. If reranking can't run, searches use the hybrid-search order instead of failing.
