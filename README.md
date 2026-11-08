# Journal Analyzer

A command-line tool for asking questions about an Obsidian-style Markdown journal. It runs everything locally through [Ollama](https://ollama.com), so your journal stays on your machine.

The tool never writes to your `.md` files. What it learns goes into a SQLite cache you can delete and rebuild whenever you like. For each dated entry it stores a mood score, some tags and a one-line summary written the way you'd write it yourself ("Hiked with John and felt calmer after", not "The author expresses..."). Long entries also get a longer summary of a few sentences, so less of what happened is lost. It also reads your undated reference notes about people, places and topics, and keeps track of how they link to your entries. Answers cite Obsidian wikilinks like `[[2024-01-15]]` or `[[John Doe]]`, and if the stored summaries aren't enough, it goes back and reads the full text of whatever it's citing.

## How it works

Each file is tracked by its sha256 hash, so re-running the indexer is cheap and a killed run just picks up where it stopped. Backfill and `journal watch` use the same code for each file.

The whole journal never goes into a model's context at once. Aggregate questions like "how was my mood in March?" are answered straight from SQL. Factual questions use hybrid search over a small set of candidates: sqlite-vec vectors plus FTS5 keyword search, merged with reciprocal-rank fusion. Narrative questions, and questions covering more than a couple of months, also get the month digests (or the year digests, when there would be too many months). Questions about a span of a few weeks get that span's week digests.

Links decide who and what an entry is about. If an entry links to `[[John Doe]]`, it mentions John Doe, and no model has to guess. When you ask something, the model sees the matching entries along with the notes your question names, the notes closest to it in meaning, and the notes those entries link to.

The notes your entries link to go along as background wherever a model reads your journal. When an entry is read, the fast model also gets what each of its links points at: the note's kind, aliases, properties and summary, how the link was written (`[[John Doe|John]]` reads as "John", so it's told that's John Doe), and the summary of any other day it links to, as much as fits in its window. A link to a heading or a block, like `[[2024-03-04#Work]]` or `[[John Doe#Kids]]`, brings the text of that part instead of the day's summary or the start of the note. If the heading has been renamed since, the summary goes in as before. Digests get the notes their entries link to most, relationship digests get the notes the note itself links to, and answers get the notes the entries they read link to. A link also finds a note by an alias when no note has that file name and only one note has that alias, so `[[John]]` counts as John Doe.

Folder and file names count too. Any model that reads a file is told where it sits in the vault, like `Links/People/John Doe.md` or `Trips/Lisbon/2024-03-05.md`, so a note filed as a person is read as one even if its text never says so.

There are three models. A fast one extracts entries, summarizes notes, picks out facts about you for your profile and routes questions. A precise one handles synthesis, digests and the profile itself. An embedding model produces the vectors.

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
- `private_tag` (default `private`) keeps entries out of the cache: a file tagged `#private`, inline or in its frontmatter tags, or with `private: true` in its frontmatter, is never sent to a model and nothing from it is stored. In a file split into several entries, a section with the tag is left out on its own. It works for notes too. `strip_comments` (on by default) leaves out `%% comments %%`, which Obsidian hides in reading view. See [Private entries](#private-entries).
- `frontmatter_mood_key` is for when you already score your own mood in each entry's frontmatter, like `mood: 7`. Set it to `"mood"` and your number is used instead of the model's guess. `frontmatter_mood_min` and `frontmatter_mood_max` give the scale you write it on (1 to 10 by default), and it's converted to the app's scale. A value like `3/5` brings its own scale. Words like `mood: tired` are ignored.
- `[tags]` controls tags. With `from_frontmatter = true` (the default), tags in an entry's frontmatter count the same as inline `#tags`. `ignore` drops tags from every entry, which helps when a template puts `daily` on every note. `merge` makes one tag count as another, like `{ friendship = "friends" }`; `journal tags --similar` lists tags that look like duplicates and prints a `merge` line you can paste in. When the model tags an entry it's shown the `known_in_prompt` most used tags (40 by default) and asked to reuse them, so near-duplicates stop piling up.
- `[metrics]` is for numbers you keep in entry frontmatter, like `sleep: 7.5`, `steps: 8k`, `bedtime: 23:30` or `exercise: true` (true and false count as 1 and 0). By default every numeric property counts except the date and mood keys; `keys` limits it to a list and `ignore` leaves some out. See [Metrics](#metrics).
- `[notes] paths` points at the folders with your undated reference notes, e.g. `["Links"]`. `other_files = true` makes every Markdown file outside `daily_note_glob` a note too. `learned_alias_min` (2 by default) is how many entries have to write a link one way, like `[[John Doe|Johnny]]`, before that text counts as a name for the note, and `learned_alias_ignore` lists link text that never should. See [Reference notes](#reference-notes).
- `[ollama]` has the host, the three model names, whether the fast and precise models think before answering (`fast_model_think`, `precise_model_think`), the embedding context window (`embed_num_ctx`) and the request timeout. The precise model runs at a 4,096-token context window unless you set `precise_num_ctx`. Every request says which window it wants, so the context length set in the Ollama app doesn't apply here; set as high as 256k, it can take more memory than the model itself and slow a CPU to a crawl. When a prompt won't fit (a very long entry, a big pile of digests), the request asks for a bigger window instead of letting the prompt get cut, and Ollama reloads the model at that size. The bigger window is kept for the rest of the command, because going back down would mean another reload. Thinking is off for the fast model by default, because on a reasoning model it makes extraction a lot slower without helping much. A model that thinks gets room in its window for that as well. A model can think for up to 2,048 tokens (`think_tokens`) before an answer or a JSON reply; a model still thinking at that point is stopped and asked again with thinking off, reusing the prompt it already read, so it can't use up the window and come back empty, and for the rest of that command replies of the same kind skip thinking. If you change `embed_model` later you'll have to rebuild the cache, since vectors from different models can't be compared.
  `fast_num_ctx` (8192 by default) gives the fast model one fixed window, so Ollama loads it once and there's room for the notes an entry links to; `precise_num_ctx` does the same for the precise model and is off (0) by default. A prompt bigger than the window still gets a bigger one. `[indexing] linked_note_chars` caps how much of each linked note goes along with an entry, and `[rollups] linked_notes_chars` how much room the linked notes get in each digest. Entries indexed before this only get their linked notes when you run `journal index --force`.
- `[db] path` is where the SQLite cache goes. `memo` keeps model results in a second file next to it so rebuilds don't redo them; see [Starting over](#starting-over).
- `[reranking]` is a cross-encoder that reorders search results before they're used for an answer. It makes results more relevant, but it downloads its model from the internet the first time it runs. If that fails, searches fall back to the fusion ranking and print a warning once. Set `enabled = false` to turn it off.

Every command takes `--config PATH` and defaults to `./config.toml`.

`journal --install-completion` sets up Tab completion for your shell (bash, zsh, fish or PowerShell). Tab then completes from the cache: note titles, aliases and learned names for `note`, `unlinked`, `--about` and `--with`, tags for `--tag`, kinds for `--type`, metric keys for `metric`, days for `show` and `similar`, periods for `digest` and `compare`, properties for `--by`, `property=value` for `--group`, and broken links for `links`. Typing in lower case is fine, since names are matched ignoring case. With no cache yet, Tab just completes nothing.

## Usage

Check the setup, index everything once, then start asking.

```sh
# setup
journal doctor            # check the vault, Ollama, the models, and config.toml against Obsidian's settings
journal init              # create the DB schema and detect the embedding size

# indexing (safe to stop and re-run)
journal index             # index the vault, skipping unchanged files
journal index --force     # re-process everything (model results it's seen before are reused)
journal index --dry-run   # what would be indexed, which files won't parse, how many model calls
journal index 2024-03-04.md --force   # read one file (or folder) again, leaving the rest alone
journal stats             # entries, notes, links, date range, DB size, errored and future-dated files

# questions
journal ask "When did I go hiking with John?"
journal ask "When did I go hiking with John?" --sources   # and what it read
journal ask "Who is John, and when did I last see him?"
journal ask "How did my mood change over spring?"     # answered from SQL
journal ask "What did we talk about?" --about "John Doe" --since 2024-03-01

# digests
journal rollup                # update your profile, then write missing or stale month, year, relationship and week digests
journal rollup --rebuild-all  # regenerate all of them
journal rollup --since 2025-01-01 --only month,year   # just this year's months and year
journal rollup --dry-run      # list what would be written, without Ollama
journal digest 2024-03        # read one digest: a year (2024), month (2024-03) or week (2024-W10)
journal digest 2024-03 --refresh   # write that one first if it's missing or out of date
journal digest 2024-03-02..2024-03-11 --refresh   # a span of days, like a trip

# live updates
journal watch             # keeps running and indexes notes as you save them (and digests, if you turn that on)

# reflection
journal ask               # no question: ask one after another, with follow-ups
journal ask --after 12 "and April?"   # follow up question 12 from your history
journal history           # earlier questions and answers, with [history] enabled = true
journal insights          # mood, tag and timing patterns you didn't ask about
journal themes --refresh  # group your entries into the threads that run through them
journal themes            # those themes, biggest first; 'journal themes 3' shows one
journal prompt            # ideas for what to write next, from threads left open
journal review 2025       # the year in review, as Markdown (or a month, 2025-03, or a week, 2025-W10)
journal compare 2024 2025 # two periods side by side; one period compares it with the one before

# other tools
journal mcp               # read-only tools for an MCP client, on stdin and stdout
journal profile           # what your journal says about you
journal profile --update  # read entries it hasn't yet and fold in what's new
```

`journal doctor` also reads Obsidian's own settings in `.obsidian/` and warns when `config.toml` disagrees with them, each time with the line to put in `config.toml`:

- the daily notes core plugin's folder and date format, written as a daily note for today would be, against `daily_note_glob` and, with `date_source = "filename"`, against `date_format`
- the templates folder (the core plugin's, or Templater's), whose files would fail to parse as entries
- the files you excluded in Obsidian's settings, when they'd still be indexed
- files in hidden folders like `.trash`, which Obsidian leaves out of the vault and which a `**/*.md` glob still picks up

It never writes to `.obsidian` or anywhere in the vault, and with no `.obsidian` folder it skips these checks.

Before a long first run, or after changing `date_source`, `date_format` or `split_heading_regex`, run `journal index --dry-run`. It walks the vault the same way `journal index` does and parses every file it would index, without Ollama and without touching the cache. It lists files that would fail and why, files that split into several entries, entries dated in the future, files that would be dropped, and roughly how many model calls the real run would make. Add `--force` to check every file, not just new and changed ones.

To index just some files, name them: `journal index 2024-03-04.md Trips/Lisbon` takes files and folders from the vault root (or from the folder you're in), and nothing else in the vault is read or dropped. It's handy after editing an entry when `journal watch` isn't running, or with `--force` to have the model read one entry again. A named file that's been deleted is taken out of the cache, and one a full run wouldn't index (outside the vault, left out by `ignore_patterns`, or matched by neither `daily_note_glob` nor `notes.paths`) is refused with the reason. `--dry-run` works the same way.

`journal rollup` shows a progress bar with the time left. `--since` and `--until` limit it to periods that overlap those dates and `--only` to some kinds (`profile`, `month`, `year`, `note`, `week`, `range`); your profile and relationship digests cover the whole journal, so the dates don't apply to them. `--dry-run` lists what it would write and why (not written yet, out of date, or rewritten for `--rebuild-all`). To read one digest, or to write just the one you want instead of waiting for a full run, use `journal digest`. A year is written from its month digests, so refreshing a year writes any of its months that are missing first.

A digest can cover any span of days, too, for the weeks and months that don't line up with what happened: a trip, a move, a hard fortnight. `journal digest 2024-03-02..2024-03-11 --refresh` writes one (up to 92 days; past that, read the month and year digests), and `journal digest 2024-03-02..2024-03-11` reads it back. They're only written when you ask, but once there they go out of date like the others when an entry in them changes, and `journal rollup` rewrites the stale ones (`--only range` for just those), as `journal watch` does with `watch_refresh_minutes` set. A question whose dates overlap one reads it next to the week or month digests, the three that share the most days with the question first. The browser lists them under Spans on the Digests page, and each entry's page shows the spans it's in. `journal compare` and `journal review` still take years, months and weeks only.

Notes you link often (from 8 or more entries by default) also get a relationship digest: a few sentences on how that person, place or topic figures in your journal over the years. It's written from the note itself and the summaries of the entries that link to it, a sample of each year's when there are many. `journal note` shows it, so do the note's page and the Digests page in the browser, and answers that name the note read it. It goes out of date when an entry that links the note, or the note itself, changes. `[rollups] note_min_mentions` sets the threshold (0 turns them off) and `note_entries_per_year` caps how many entries from each year go in.

`journal watch` can keep the week and month digests current as you write. Set `[rollups] watch_refresh_minutes` (0, off, by default) and, once nothing is waiting to be indexed and that many minutes have passed since it last did this, watch writes the digests of the weeks and months it indexed or dropped entries in: the ones that are out of date, and the ones that don't exist yet. Months go first and newest first, since answers read months, and it stops as soon as a file changes and picks up again later. It only looks at days that changed while it was running, so a backlog from before (after `journal index --force`, say) is still `journal rollup`'s job, and so are year and relationship digests. If Ollama is down, the days wait for the next try.

Each digest is one call to the precise model, so on a CPU a full `journal rollup` can take hours. Your profile is updated first, since every digest is written with it, then month and year digests, because those are what answers use, so if you stop a run partway they're already there.

`journal ask` shows which step it's on while it works, then prints the answer as the model writes it, so on a CPU you can start reading long before it finishes. Under the answer it says how long each step took ("Took 23m 10s: working out the question 4.1s, searching 1.2s, loading the precise model 29s, precise model reading 19m 02s, ..."); follow-ups, the browser and `journal history` show the same, and `--json` has it as `timings`. Once a model is called it says whether Ollama is still loading it into memory, how much it's reading ("Re-reading 8 entries and 3 notes in full, about 6,400 words..."), and when it has started thinking, since on a CPU those can take a lot longer than the answer itself. While it reads there's a bar with the time left. Ollama doesn't say how far through a prompt it is, so this is an estimate: after every call Ollama reports how many tokens it read and how long that took, and the app keeps that per model (in the memo file) to time the next one. The first question to a model has no bar yet, and a follow-up asked in a row that Ollama partly remembers finishes early. With `[ollama] show_thinking = true` the thinking is printed as it comes, dimmed, before the answer; it's off by default because it can run to pages. The fast model's first try is held back for a moment until it's clear it isn't giving up (it answers INSUFFICIENT when the summaries aren't enough and the precise model takes over). Answers are asked for in light Markdown, a short list when one names several days or things and bold for the fact you asked for, which the browser formats and which still reads as plain text in a terminal. At the end it says which stage produced the answer: `aggregate` (plain SQL), `fast_summary` (the fast model reading stored summaries and notes) or `precise_raw` (the precise model re-reading the full text of the cited entries and notes).

When the precise model is about to re-read entries and notes for an answer and a thinking model wouldn't have room to think in the window, the least relevant notes are left out first, then the least relevant entries, a whole one at a time, instead of the window growing for the thinking alone. The most relevant note and entry always stay; if they alone leave no room, nothing is left out and the window grows. The answer says when something was left out.

To check an answer, add `--sources`: it lists the entries (date, mood, tags, summary) and notes the model was given, and for each cited day the sentence of it with the most of the question's words, so you can read what you wrote next to what the answer says. When asking in a row, type `sources` after an answer. The browser shows the same sentences under an answer's Sources, and `ask --json` gives each as `sources.dates[].quote` (null when no sentence has the question's words). Words that most sentences have count for little, so the sentence that names John or the garden wins over one that only says "today". No model picks them, and the part of a day that matched the search counts for a little more. The `Sources:` line links each day and note to its file, so in a terminal that supports links (Windows Terminal, iTerm2 and most Linux terminals) clicking one opens it in Obsidian.

### Browsing commands

These read the cache and print tables. No model writes anything for them, so they come back right away. `search` is the only one that uses a model, and only to embed your query.

```sh
journal search "hiking with John"        # keyword and semantic search over entries and notes
journal search "garden" --since 2024-03-01 --until 2024-06-30
journal show 2024-01-15                  # one day's mood, tags, linked notes and summary
journal show 2024-01-15 --raw            # same, plus the stored entry text
journal note "John Doe"               # a reference note and the entries that link to it
journal tags --limit 30                  # most used tags
journal tags --similar                   # tags that look like the same thing, as a merge table
journal entities --type people           # most-linked notes, optionally of one kind
journal entities --with "John Doe"       # notes linked from the same entries as John
journal entities --by relationship       # notes grouped by a property, with their mood
journal mood --group relationship=family # mood on the days that link family
journal unlinked                         # notes your entries name without linking
journal unlinked "John Doe"              # the entries that name John without a link
journal links                            # links that find no note or day, and why
journal links "Jon Doe"                  # what's wrong with one, and the entries that write it
journal lapsed --type people             # people you linked often but not in the last 6 months
journal mood --since 2023-01-01          # average mood per month, with a bar chart
journal mood --about "John Doe"          # only the days you linked John
journal metric                           # numbers you keep in frontmatter, like sleep: 7
journal metric sleep --since 2024-01-01  # one of them by month
journal streaks                          # days written, words, longest run
journal on-this-day                      # what you wrote on this date in earlier years
journal similar 2024-01-15               # closest entries by stored embedding
journal export --format markdown --out ~/journal-export.md
journal export --format csv --out ~/journal.csv   # one row per day, for a spreadsheet
```

`search`, `tags`, `entities`, `mood`, `metric`, `insights` and `export` take `--since` and `--until` as ISO dates. `search`, `mood`, `metric` and `export`, as well as `ask` and `chat`, also take `--about NOTE` (only entries that link that note, by title or alias), `--tag TAG` and `--group PROPERTY=VALUE` (only entries that link a note with that property value). On `ask` and `chat`, `--since` and `--until` replace whatever dates the question's wording suggests, which helps when the small routing model reads a date wrong; with `chat` the filters apply to every question in the conversation. `journal export` writes JSON by default, includes the full entry text with `--include-raw`, and won't write to an `--out` path inside the vault.

`--format csv` is for spreadsheets: one row per entry with its date, mood, word count, tags and linked notes, a column for each number you keep in frontmatter (see [Metrics](#metrics)), then the summaries and the file. Tags and notes are separated by `; `. A text cell that starts with `=`, `+`, `-` or `@` gets a `'` in front, since a spreadsheet would otherwise run it as a formula (an entry that starts with `- [x]` shows up as `#NAME?` in Excel). Written to a file with `--out`, it starts with a byte-order mark so Excel reads it as UTF-8; printed to the terminal it doesn't.

### JSON for scripts

`ask`, `search`, `show`, `note`, `stats`, `tags`, `entities`, `mood`, `metric`, `streaks`, `on-this-day`, `similar`, `lapsed`, `unlinked`, `links`, `compare`, `review` and `digest` take `--json` and print one JSON object instead of tables, for a launcher, an editor plugin or `jq`. Entries, notes and digests have the same shape as in the `journal browse` API. Errors come out as `{"error": "..."}` with exit code 1, and anything that isn't the result (like the progress of `digest --refresh`) goes to stderr. `ask --json` waits for the whole answer instead of streaming it.

```sh
journal mood --about "John Doe" --json | jq '.months[] | select(.avg_mood < 5)'
```

### Tools for MCP clients

`journal mcp` serves the journal as read-only tools over MCP, the protocol desktop chat apps, editors and agents use to call local tools. Point a client at it as a stdio server:

```json
{
  "mcpServers": {
    "journal": { "command": "journal", "args": ["mcp", "--config", "C:/path/to/config.toml"] }
  }
}
```

The tools are `search`, `ask`, `day`, `note`, `notes`, `mood`, `digest`, `on_this_day`, `broken_links`, `compare`, `review`, `metric`, `similar`, `tags`, `lapsed`, `unlinked` and `profile`. They return the same JSON as the `--json` commands, and `ask`, `mood` and `metric` take the same `about`, `tag` and `group` filters. `search` falls back to keywords when Ollama isn't running, and `ask` needs Ollama like `journal ask` does. Every call opens the cache read-only, so it can run next to `journal watch`, and nothing is ever written. It needs no extra packages.

Whatever app you connect gets your journal's text, summaries and notes. If that app sends them to a hosted model, they leave your machine, which nothing else in this tool does. Connect it only to clients you'd trust with the journal itself.

`[mcp]` in `config.toml` narrows what a client can see:

- `raw_text = false` leaves out the text of entries and notes, and the sentences `unlinked` quotes, so a client gets summaries, digests, tags and numbers. Answers from `ask` are still written from the full text by your local model and can repeat parts of it; leave `ask` out of `tools` if that matters.
- `exclude_tags = ["therapy"]` takes every entry with one of those tags out of every tool: tagged inline, in the file's frontmatter, or by the model, even if `[tags] ignore` drops the tag. A note with one in its file goes too. So does what was written from them: the digests of any period they fall in, the relationship digests of the notes they link and of notes that link their days, the facts about you they gave, and your profile if those facts went into it. Links to a day that has nothing left on it are dropped as well, so they don't show up as broken links that give the date away.
- `exclude_kinds = ["health"]` does the same for notes of those kinds, along with the links to them and the digests that read them. The entries that link such a note stay. Their summaries were written with the note as background, so they can repeat what it says; tag those entries too if they should go.
- `tools = ["search", "day", "mood"]` offers only those tools.

With `exclude_tags` or `exclude_kinds` set, the server keeps a copy of the cache in memory with all of that taken out, and makes it again whenever the cache file changes, so it keeps up with `journal watch`. The cache itself isn't touched. When it starts, it says on stderr what it's leaving out, which most clients show in their logs.

### Browsing in a web page

```sh
journal browse              # opens http://127.0.0.1:8765 in your browser
journal browse --port 9000 --no-open
```

`journal browse` shows everything in the cache as a local web page, so you can see exactly what the app knows. Home opens on today: a box to ask from, what you wrote on this day in other years, your latest entries, every entry as a mark coloured by its mood, and anything that needs a look, like a file that couldn't be indexed. The pages are grouped down the side (on a phone-sized window they slide in from the menu button), and Auto, Light and Dark at the bottom pick the colours.

Each entry page puts your text next to what the models took from it. The text is shown with its Markdown formatted, headings, lists and links to days and notes included, and "Show it as stored" switches to the plain text the models read. Beside it: mood, summary, tags, the notes and days it links to (and the part of each that a `#heading` link points at, which opens with that part marked), links that don't resolve to a note yet, which model read it and when, whether it has a vector, the entries closest to it in meaning, and the digests that cover it. There are also pages for notes (aliases, properties, backlinks, mentions by year), tags (with average mood against your usual), the numbers you keep in frontmatter (see [Metrics](#metrics)), digests, a calendar coloured by mood, words or one of those numbers, and every tracked file with its index status and errors. Type `/` to jump to a date, note or tag, `a` for Ask, `h` for Home, `j`/`k` to step through entries, and `?` for the list of keys.

The Ask page answers questions the same way `journal ask` does. You see each step while it works and the answer as it's written (and with `show_thinking` on, the model's thinking, folded away once the answer starts), then its sources linked to their entry and note pages, and a list of everything the model read. Answers are shown with their Markdown formatted: lists, bold, headings and tables, and each day or `[[note]]` an answer names is highlighted and opens that page. Digests, your profile and a model's thinking are shown the same way. The question box stays at the bottom of the window under a long conversation. Follow-ups build on earlier answers like they do in `journal ask`, and the conversation stays put while you open the pages it cites. Asking needs Ollama; Cancel stops waiting for an answer. A question asked while another is being answered waits in line under it (the button says Queue) and starts once the one before it is done, so it can follow up on that answer; Remove takes it out of the line. Each answer says which model wrote it and how long it took. Tick "Tell me when an answer is ready" under the question box to get a notification from your browser when an answer arrives while you're looking at something else; the browser asks your permission the first time, and the setting is kept in that browser. From the jump box, type a question and pick "Ask".

Note, tag and digest pages, and the Entries page once it's filtered, have an Ask box of their own. A question asked there only reads the entries that page shows: the ones that link the note, have the tag, fall in the digest's dates, or pass the filters (all but the words filter). It works like `--about`, `--tag`, `--group`, `--since` and `--until` on `journal ask`. The Ask page shows what it's kept to above the question box, and follow-ups stay kept to it until you take it off with ×.

It opens the cache read-only and never touches your notes, so it's safe to leave open while `journal watch` runs. Reload a page to see new changes. It only listens on 127.0.0.1 and turns away requests that use any other host name. Requests sent by other websites are refused, and anything that does something, like asking a question or forgetting one, only works from the browser's own pages: each of those requests carries a token that's made new every time `journal browse` starts. Keyword search works offline; matching by meaning needs Ollama and falls back to keywords when it isn't running. Nothing loads from the internet. Ctrl-C stops it.

### Reflection commands

`journal ask` with no question asks one after another and remembers the last few turns, so follow-ups work: ask "How was March?", then "what about the month after that?", and the second question gets rewritten into a standalone one before the usual retrieval runs. Each answer still shows which stage produced it. Ctrl-C while it's answering drops that answer and keeps the conversation going. Type `exit` or press Ctrl-D to leave. The filters (`--since`, `--about` and so on) apply to every question.

With history on, you can also follow up a question later: `journal ask --after 12 "and April?"` reads question 12's conversation as context and adds to it, and `--after last` follows the newest one. Leaving out the question with `--after` carries on in a row from there. With history off there's nothing earlier to follow, so `--after` says so; asking in a row still works, it just isn't kept.

`journal insights` runs statistics over the cache in plain SQL, then has the model describe only the findings that pass a threshold: tags and linked notes whose average mood differs from your baseline, weekday and seasonal patterns, numbers you keep in frontmatter (see [Metrics](#metrics)), and themes that show up together. These are correlations, not causes, and raw entries are never sent to a model. Use `--since` and `--until` (ISO dates) to limit the range, and set the thresholds in `[insights]` (`min_sample_size`, `mood_deviation_threshold`).

`journal review 2025` puts a year on one page in Markdown: the year digest, writing numbers, mood by month, your best and hardest days by mood, the notes you linked most and the ones that first appear that year, your top tags against the year before, and tags that fell away. Days and notes are `[[links]]`, so it reads well pasted into Obsidian. It makes no model calls; if the year digest isn't written yet it says how to write it. Leave out the year for the latest one you wrote in, and add `--out FILE` to save it (outside the vault).

A month or a week works the same way. `journal review 2025-03` has the month digest, mood by week (each week cut to the days inside the month), your three best and hardest days, and the tags and notes against February. `journal review 2025-W10` lists each day of the week with its mood and summary instead of picking best and hardest. `--json` prints the same review as JSON.

The browser has the same review as a page, `#/review/2025-03` and so on, linked from each digest's page and from the calendar, with each month of a year linking to its own review. `journal compare` has a page too: the Compare link on a digest or review page sets the period beside the one before, and the form at the top takes any two.

`journal compare 2024 2025` puts two periods side by side: entries, days written, words, average mood, the longest streak and the average of each number you keep in frontmatter, then the tags you wrote more and less of, the notes you linked more and less, and the notes that are new or no longer linked. Periods are years, months (`2025-03`) or ISO weeks (`2025-W10`), and with one period it compares it with the one just before, so `journal compare 2025-03` is February against March. Tags and notes are compared by the share of each period's entries they're on, since a busy month would otherwise seem to have more of everything, and one needs at least 2 entries in a period to count as rising, falling, new or gone. It makes no model calls, and takes `--json`.

`journal prompt` looks back over recent entries (14 days by default, or `--days N`) and suggests open-ended writing prompts that pick up loose threads, like a project you named, a person who keeps coming up, or a feeling you left unresolved. `--count N` sets how many. It only reads the cache and never touches your notes.

## Themes

Tags depend on what the model picked for each entry, and notes on what you linked, so a thread you never named, like a year of job hunting or a run of bad sleep, may show up in neither. `journal themes --refresh` finds those threads from what the entries are about: it groups their stored vectors with k-means, seeded so the same entries always give the same themes, then has the fast model name each group from the 8 entries nearest its middle (and the tags and notes they share most). Grouping needs no model and takes a second or two for a few thousand entries; naming is one call per theme, kept in the memo, so a theme that comes out the same isn't named twice. If Ollama is down, the themes are kept without names and labelled by what their entries share until the next refresh.

`journal themes` lists them, biggest first, with how many entries each has, when it ran and its average mood, and `journal themes 3` shows one: what it's about, its entries by year, its tags and notes and the entries closest to its middle. Both take `--json`. The browser's Themes page puts every theme on one time axis, each month shaded by the share of that month's entries in it, and each theme has a page with its entries month by month. A narrative question, or one about four weeks or more, also gets the themes with the most entries in its dates.

An entry written after the last refresh goes with the nearest theme when the themes are read, without anything being written, so they keep up between refreshes. Once a fifth of the entries came that way (`[themes] stale_share`), the list says it's time to refresh. `[themes] count` sets how many to look for (0, the default, picks about the square root of a quarter of your entries, between 3 and 15), and a theme with fewer than `min_entries` entries (5) is folded into the others. Themes are numbered again at each refresh, so `journal themes 3` can be a different one afterwards.

With `exclude_tags` or `exclude_kinds` set under `[mcp]`, a theme named from an entry that's left out keeps its entries but loses its name and description, which were written from that entry.

## Question history

Answers aren't kept anywhere unless you ask for that, since they're made of your journal. Set `[history] enabled = true` and every question you ask with `journal ask` or the browser's Ask page is kept with its answer, so you can read it again:

```sh
journal history               # the questions you asked, newest first
journal history 12            # question 12 and its answer again
journal history --delete 12   # forget that one for good
journal history --clear       # delete the whole history file (asks first; --yes doesn't)
journal ask --after 12 "..."  # follow up question 12 (or --after last)
```

They go in a file of their own, `journal-history.db` next to the cache (`[history] path` moves it, but never into the vault), so rebuilding the cache leaves them alone and deleting that file deletes all of them. It's made the first time a question is kept, with the same private permissions as the cache, and listing it never creates it. Only what you need to read an answer again goes in: the question, how a follow-up was read, what it was kept to (`--about`, dates and so on), the answer with its Sources line, which days and notes it cited, and how long each part of the answer took. Not the entries it read, and not the sentences quoted from them.

Deleting really deletes: rows are overwritten rather than left in the file's free space, and the file keeps no `-wal` journal holding older copies. `keep` (500) is how many questions it holds before the oldest go, and `forget_after_days` (0, off) also drops the ones older than that. If you turn history off again, nothing new is kept, but the file stays until you run `journal history --clear`; `journal history` and `journal stats` remind you it's there. Questions asked through `journal mcp` are never kept, since the app on the other end keeps its own.

With it on, the browser's Ask page lists your latest questions, and Earlier questions lists all of them, with a box to filter them by their words. Each opens on a page of its own with its answer, its sources and the questions that came before it in the same conversation. Follow up carries that conversation on from the Ask page, like `journal ask --after`. Forget deletes one question and Delete all deletes the history file, both for good and both after asking first, the same as `journal history --delete` and `--clear`.

## Longer summaries

A one-sentence summary loses most of a long entry, and when the summaries aren't enough an answer falls back to the slower precise model re-reading the full text. So entries of 400 words or more (`[indexing] detail_min_words`) also get a longer summary of 2 or 3 sentences, and from 1,000 words (`detail_long_words`) one of 3 to 5. It's written in the same call as everything else about the entry, in the same first-person voice. Summaries are always in your voice ("Felt drained after work", not "The author felt drained"); one that comes back written from outside is asked for again, and `journal index` and `journal watch` rewrite any older ones that still are, one short fast-model call each.

The one-liner stays where a list wants one line: the tables, the browser's entry lists and the calendar. The longer one goes where depth helps: answers from summaries, `ask --sources`, week and month digests, relationship digests and writing prompts. `journal show`, `journal export` and the entry page in the browser show both. Prompts have room for only so much, so each one fills a budget (`[rollups] entry_details_chars` for digests), longest entries first; the rest go in as their one-liner.

Shorter entries are read with exactly the same prompt as before, so changing these settings or upgrading only costs model time for long entries: run `journal index --force` and everything else comes from the memo. Set `detail_min_words = 0` to turn it off.

## Searching by part

A day with several top-level headings, like `# Morning`, `# Work` and `# Evening`, is also stored part by part. Each part gets its own keyword index and its own vector, so a search for "the quarterly review" finds the day by its Work part even when the rest of the day is about something else and the whole entry is too long to embed in full. A part shorter than `[indexing] section_min_chars` (200 characters by default) joins the one before it, and text above the first heading joins the first part. Entries with only one part aren't split.

When a part carries the match:

- the answer reads that part's text next to the day's summary
- the Sources line cites it, like `[[2024-03-04#Work]]`, and so does `sources.dates[].section` in `ask --json`
- `journal search` shows the part under the date, and `search --json` gives it as `section`
- in the browser, search results and Sources open the day at that part, and each entry page lists the parts it was split into

Splitting costs one embedding call per part when an entry is indexed, and nothing else. After an upgrade, or when you change `sections` or `section_min_chars`, the next `journal index` splits the entries already in the cache from their stored text and embeds the parts, without reading them again with the fast model. `sections = false` turns it off.

## Photos

A day that's a couple of lines and a photo says little to a model that only reads text. Set `[ollama] vision_model` to a model that reads images, like `gemma3`, `llava` or `qwen2.5vl` (a Gemma model you already use for `fast_model` may do), and every image an entry links or embeds, like `![[beach.jpg]]`, `[[photo.png]]` or `![](trip/photo.webp)`, is described in a sentence or two. The description goes along with the entry's text when the fast model reads it and when it's embedded, so the summary, mood and tags can draw on it, and a search for "the beach" finds that day. Photos are also searched by keyword, the precise model sees the descriptions when it re-reads entries, and `journal show` and the browser's entry page list them.

Images are found the way Obsidian finds attachments: by the path in the link, or else by file name anywhere in the vault outside hidden folders. Up to `[indexing] photos_per_entry` (4) are described for each entry, videos and SVGs are skipped, and a model that can't read images gets one warning and no more tries. Each description is one call to the vision model, kept in the memo by the image's bytes, so a photo is described once however many entries show it. Changing `vision_model` makes the next `journal index` read again the files that have photos, and only those. `journal index --dry-run` says how many photos it would describe, and `journal doctor` checks that the vision model is pulled. The model runs in Ollama like the others, so the photos never leave your machine.

## Your profile

The app keeps a short profile of you, written in your voice: names and nicknames (yours, and the ones you use for people), personality, likes and dislikes, hobbies, routines, work, values, health. Answers, follow-ups, every digest (including relationship digests), `journal insights` and `journal prompt` get it as background, so "my sister" or a nickname reads right and a digest knows what's usual for you. The step that asks your question's dates and names, and the step that rewrites a follow-up, only get the names part. Reading a single entry never uses it, so a wrong guess can't work its way into your summaries and then back into the profile.

It's built in two steps. The fast model reads your entries a few at a time and pulls out lasting facts about you, each with the date of the entry it came from. The precise model then folds new facts into the profile, and when two facts disagree the later one wins ("I used to run, now I swim"). If you keep a note about yourself, point `[profile] note` at it; it's read as you wrote it and wins over anything inferred. A note marked private is left out.

The profile goes into a lot of prompts, so it has to stay short. Models don't keep to a word count, so `[profile] max_words` is turned into a number of sentences per section (4 for 500 words), which they do keep to. If a profile still comes out more than a quarter over the limit, the precise model gets one more call that only shortens it, keeping every name. That's one try for each version of the profile and limit, so a model that won't shorten it isn't asked again on every run.

New entries are read for it as `journal index` and `journal watch` index them; watch waits until it's idle and rewrites the profile at most every 30 minutes (`watch_update_minutes`), since each rewrite is a precise-model call. Entries indexed before the profile existed are read by `journal profile --update` or by `journal rollup`, which updates the profile before writing any digest. That first read is one fast-model call per 8 entries (`scan_batch_entries`), so on a big journal and a CPU it takes a while; `journal rollup --dry-run` says how many calls.

Each rewrite reads the whole profile and writes all of it out again, so on a CPU it's often the slowest part of `journal index`. It never thinks first, whatever `precise_model_think` says: tried on a CPU, a 27B model spent half an hour thinking about one rewrite and never got to the profile. Even without thinking, a dense model of 20 billion parameters or more writes only a few words a second there, about 6 minutes a rewrite. A mixture-of-experts model such as `qwen3:30b-a3b` as the precise model is several times quicker.

`journal profile` shows it, with when it was written and how many facts it's built from. `--facts` lists the facts with their dates, `--rebuild` writes it again from every fact in date order, and `--rescan` forgets what was read and reads every entry again. The browser's You page shows the profile next to the facts, each linked to its day, and `journal review` lists what was new about you that year. A profile change doesn't mark digests out of date; `journal rollup --rebuild-all` rewrites them with the current one.

## Metrics

Numbers in your entries' frontmatter are stored with each entry without any model reading them. `journal metric` lists them with their range and average, and `journal metric sleep` shows one by month (it takes `--since`, `--until`, `--about` and `--tag`). They also turn up where they help:

- `journal insights` compares your mood on either side of each metric's median, or on the yes and no days of a true/false one, and reports the differences that pass the `[insights]` thresholds
- a question that names one, like "how did I sleep in March?", gets its monthly averages when it's answered from statistics, and the entries an answer reads list their numbers
- week and month digests see each day's numbers next to its mood
- `journal show`, `journal export` and the entry pages in `journal browse` show them
- the browser's Numbers page lists them, and each has a page of its own: its average by month above your mood by month on the same days (two charts lined up month by month, with a table under them), your mood on either side of its median, or with and without a yes/no one, as `journal insights` works it out, and the days with the highest and lowest values
- the Calendar's Colour by picker shades each day by one of them, or by words written, in one blue from light (less) to dark (more), and a dashed square is a day you wrote without recording it

Changing `[metrics]` takes effect on the next `journal index` with no model calls.

## Private entries

Marking a file private keeps it out of everything: no model reads it, the cache stores nothing from it, and it can't come up in answers, digests, search or `journal browse`. The file is listed with the status `private` (`journal stats` counts them), so the indexer knows to skip it until it changes. Adding the tag to an entry that's already indexed takes it out of the cache on the next `journal index` or as soon as `journal watch` sees the edit; removing the tag puts it back.

If you change `private_tag`, the next index checks every file against the new tag without calling a model, takes out the ones that are now private and indexes the ones that no longer are. The first index after upgrading does the same check, so entries you'd already tagged `#private` leave the cache.

Comments are taken out of a file's text before anything reads it. Files you've already indexed keep their comments until they change, or until you run `journal index --force`. Switching `strip_comments` re-indexes the files that contain `%%`.

## Reference notes

Point `[notes] paths` at the folders that hold your undated notes, for example `paths = ["Links"]`. A note's kind is the folder directly under that path, so `Links/People/John Doe.md` is a `people` note and `Links/Topics-Things/Chess.md` is a `topics-things` note. Single files work too: `paths = ["Links", "Lyrics.md"]`.

For each note, the cache keeps:

- its aliases from the frontmatter, so a question about "John" finds `John Doe`
- its other properties, like `Relationship: Friend`, which are searchable and show up in answers
- a summary. Short notes are used as they are, and only notes longer than `summarize_over_chars` (280 by default) cost a model call
- the links it and your entries make, resolved the way Obsidian does it: by file name, ignoring case, folders, headings and display text. When no note has that file name, a link also finds the one note with that alias. A link to a day finds it by its date or by its file's name, so `[[2026-03-04 needs links]]` is the entry in `2026-03-04 needs links.md`

A link to a heading or block counts as a link to the whole note or day. The part it points at is kept too: it's what the model reads for that link, `journal show` lists it after the day or note (`other days: 2024-03-04 (Work)`), and in the browser it opens the day or note with that part marked. Entries indexed before this get the parts' text in their reading after `journal index --force`, which only calls the model for entries with such links. The next `journal index` or `journal watch` reads which parts your links point at from the files without calling a model, so `journal show` and the browser list them straight away.

Entities come only from links. `journal entities`, the name filter in answers and the per-person mood patterns in `journal insights` all reflect exactly what you linked. A link to a note that doesn't exist yet starts counting as soon as you create the note. Links to attachments (images, video, PDFs) don't count as mentions, and embedded attachments are left out of the indexed text; with a vision model set, images are described instead (see [Photos](#photos)).

Properties group notes. A note with `Relationship: [Friend, School]` is in the Friend group and the School group of `relationship`, and a plain `Relationship: Friend` puts it in one. `journal entities --by relationship` lists each value with how many notes have it, how many entries link one of them, and their average mood. `--group relationship=family` on `ask`, `chat`, `search`, `mood`, `metric` and `export` keeps only the entries that link a note in that group, so `journal mood --group relationship=family` is your mood on the days you wrote about family. `journal insights` reports groups of two or more notes whose days run above or below your usual mood, using the properties in `[notes] group_keys`, or all of them when that's empty. In the browser, a note's property values link to those entries, and the Notes page lists the groups. Property names and values ignore case. Notes indexed before groups existed get their values from their files on the next `journal index`, without a model call.

`journal note "John Doe"` also lists the notes most often linked from the same entries as John, with how many entries they share, and so does his page in the browser under Often linked with. `journal entities --with "John Doe"` gives the whole list, and takes `--since`, `--until` and `--type` like the rest of `entities`.

The browser draws the same thing for all your notes at once on the Note map (linked from the Notes page): the notes you link most as dots, sized by how many entries link them and coloured by kind, joined by a line when two or more entries link both. Pointing at a note, or tabbing to it, picks out the ones it turns up with. You can keep it to one kind or one year, show 25 to 150 notes, and read the pairs as a table underneath. Only the three most common kinds get a colour of their own, since more can't be told apart in every pair, and the rest share a gray.

`journal lapsed` lists the notes you linked from at least 5 entries (`--min`) but not in the 6 months (`--quiet`) before your latest entry, with the year you linked each one most and how long it's been. The browser's Notes page shows the same list under Gone quiet. The months are counted back from your latest entry rather than today, so a few weeks off from writing doesn't make everyone look absent.

The way you write links counts as well. If you write `[[John Doe|Johnny]]` in two or more entries (`[notes] learned_alias_min`), "Johnny" becomes another name for John Doe wherever names are matched: a question that says Johnny finds him, `journal note Johnny` and `--about Johnny` work, and `journal unlinked` catches entries that say Johnny without a link. Link text that you use for two different notes, words like "her" or "him", and anything in `learned_alias_ignore` never count. `"Mom's"` counts as Mom. `journal note` lists the names it learned, and so does the note's page in the browser. Links themselves still resolve only by file name and aliases, the way Obsidian does it, so a learned name never adds a mention on its own. Names are learned again at the end of every `journal index`, and by `journal watch` as files change.

Because only links count, an entry that says "lunch with John" without `[[John Doe]]` is missing from John's numbers. `journal unlinked` lists the notes this happens to and how often, and `journal unlinked "John Doe"` shows each of those entries with the sentence that names him, so you can add the links in Obsidian. It matches titles and aliases as whole words, ignoring case, and skips a name inside another note's longer name. The browser lists them on each note's page too.

Links go the other way too: an event or trip note can link the days it covers, like `[[2024-03-05]]` in `Links/Events/Lisbon.md`. `journal note Lisbon` lists those days, the note's page in the browser shows them with their summaries, each of those entries shows the note under Notes that link this day (and `journal show` as `linked from notes`), and the note's relationship digest reads them. They don't count as mentions unless you set `[notes] day_links_count = true`. With it on, each day a note links counts as mentioning it, the same as if the entry had linked the note, so it shows up in `journal entities`, `mood --about`, `insights` and the rest. Changing the setting takes effect on the next `journal index` with no model calls, and marks the digests it affects as out of date.

A link that finds no note or day counts for nothing, and `journal links` lists them with the likeliest reason and what to do:

- a Markdown file by that name exists but isn't read as a note: it's outside your note folders (add it to `paths`, or set `other_files`), in a hidden folder like `.trash`, or left out by `ignore_patterns`
- the note couldn't be indexed, or it's marked private
- it's an alias two notes share, so it can't tell which one you mean
- it's a date with no entry, or not a real date
- it's close to the name of a note that exists, like `[[Jon Doe]]` for John Doe
- or there's simply no note by that name yet

`journal links "Jon Doe"` shows one of them with the entries that write it, linked to their files. The browser's Notes page lists the same links with their reasons, except that it never looks at the files in your vault, so a file outside your note folders shows up there as having no note.

If you add a folder to `paths` and run `journal index` again, the files in it switch from entries to notes even if they haven't changed.

Notes don't have to live in one folder. With `[notes] other_files = true`, every Markdown file that `daily_note_glob` doesn't pick is a note as well, so a `Josh.md` at the vault root or a `Projects/Garden.md` counts without being listed in `paths`. Its kind is the folder it sits in under the vault root (`projects`), or `note` at the root. Hidden folders such as `.trash` and `.obsidian` are left out, the way Obsidian leaves them out, and `ignore_patterns` still apply. This only changes something when `daily_note_glob` is narrower than every file, for example `"[0-9][0-9][0-9][0-9]/**/*.md"` for daily notes filed in year folders. Turning it off again drops those notes on the next `journal index`. `journal index --dry-run` shows how many files would become notes before you run it.

## Starting over

The DB is only a cache, so a full rebuild is:

```sh
rm ~/.journal_analyzer/journal.db
journal init && journal index && journal rollup --rebuild-all
```

If an upgrade changes the cache format, commands will say so and tell you which file to delete, instead of misreading the old one.

A rebuild doesn't have to redo the model work. Next to the cache there's a second file, `journal-memo.db`, that keeps what the models returned for entry readings, note summaries, digests, the profile and embeddings, keyed by everything that went into each one: the model and its exact weights, the prompt, and the options. When the same input comes round again, after a rebuild or with `journal index --force`, the kept result is used instead of a model call, so a rebuild of an unchanged journal takes minutes. Anything that would change the result (an edited entry, another model, a re-pulled model, a new prompt in an upgrade) misses and goes to the model. Answers to questions are left out, since they're written at a higher temperature to read naturally. The list of tags shown to the model doesn't count toward an entry's key, because it grows during a first index and would otherwise make every rebuild miss.

To have the models read everything again, add `--no-memo` to `index`, `watch`, `rollup`, `digest` or `profile`, or delete the memo file. `[db] memo = false` turns it off and `memo_path` moves it. `journal stats` shows its size. It holds summaries of your journal, so it gets the same private permissions as the cache.

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
- The cache keeps the model's own mood and tags for each entry next to the frontmatter it read. Changing `frontmatter_mood_key` or `[tags]` takes effect on the next `journal index` or `journal watch` without calling a model, and switching them back restores what the model said.
- `journal watch` keeps going when Ollama restarts. Changed files stay queued and get retried.
- `journal watch` handles whole folders as well. A folder that's deleted or moved out of the vault (which is what the Recycle Bin does) takes its files out of the cache, even though Windows only reports the folder.
- On Linux and macOS, only you can read the cache file and the folder it creates.
- If you switch embedding models, the cache notices (it records the model name and vector width) and tells you how to rebuild, rather than mixing vectors that can't be compared.
- Everything except reranking works with no network at all. If reranking can't run, searches use the hybrid-search order instead of failing.
