# Trying answers against the real models

The tests in `tests/` mock every model call, so they can say a prompt was sent but not whether the answer it gets is any good. This folder is for that: a made-up journal with facts planted in it, questions whose right answers are known, and a runner that puts them to the models in your `config.toml`.

Use it before and after changing a prompt, the router, how many entries are read, or the model itself.

```sh
python -m evals list                      # the scenarios and what each tests
python -m evals run before                # ask all of them, keep the answers as "before"
python -m evals show before               # read them beside what a right answer says
```

It needs Ollama running with your models pulled. On a CPU an answer that thinks takes four or five minutes, so all 21 scenarios take about an hour and a half; `--only` and `--tag` cut that down.

Your own journal is never opened. The runner takes the `[ollama]` and other settings from your config and swaps in a throwaway vault and cache under `evals/.work/`, which git ignores. The profile and the question history are turned off for the run.

## What's in the journal

`journal.py` has 35 entries from 28 September to 15 November 2026 and six notes, and the questions are asked on Monday 16 November. Each fact a scenario turns on is written in on purpose:

- A colleague found and fixed a bug, and the writer took the thanks for it.
- Five bad nights, each after caffeine at 4pm or later, each blamed on work. Two stressful days with no late caffeine and a good night's sleep.
- Ten runs in October adding up to 64 km, some mentioned in passing in entries about something else, then a week that doubles the distance and a shin that gets worse.
- A loan of £300 with £100 repaid late, and a request for £500 more.
- A goal of three gym sessions a week, two sessions, and an entry that says it's going well.
- A demo moved forward at short notice, an import the writer knew was untested, and an argument about whose fault it was.
- A report due Wednesday with only an outline, a call promised for Tuesday evening, and a "dentist tomorrow" in an entry ten days old.

Entries go into the cache with the summaries written in `journal.py`, not ones a model wrote, so a run tests answering and not indexing.

## What the scenarios test

| Tag | What it's for |
|---|---|
| `verdict` | Saying the writer is right, wrong or partly right, whichever way the question leans |
| `fix` | Ending with what would put it right, built on what the days show |
| `pushback` | Follow-ups: holding an answer against a bad objection, changing it for a good one |
| `pattern` | A cause spread across the whole journal |
| `now` | What to do next, with the weekdays right |
| `count` | Sums and counts over many entries |
| `lookup` | The record, and saying so when it isn't there |

`scenarios.py` gives each one a `want` (what a right answer says), often a `bad` (how it has gone wrong before), and tripwires in `must` and `must_not`.

## Reading a run

Read the answers. `show` prints each one under its `want`, with how the question was routed, how many entries were read and how long it took. The tripwires are only patterns: an answer can set none off and be wrong, or set one off and be right, so they tell you where to look first and nothing more. There is no score.

What to look for, beyond the `want`:

- Does it open with the verdict?
- Is every name, number and date as the journal has it? Rates and weekdays are where a model slips.
- Does it keep to what was asked? A sore leg and a bad night in the same week are not why a report is late unless an entry says so.
- When the writer is right, does it say so and stop?
- In a follow-up, does it take what it's told as the writer's account, and move only as far as that warrants?

Answers vary from one take to the next, so one good answer doesn't settle it. `--repeat 3` asks each scenario three times; a change is better when it's better across takes.

## Comparing

Give each run a label and show them together:

```sh
python -m evals run before --tag verdict --repeat 2
# change a prompt in journal_analyzer/query.py
python -m evals run after --tag verdict --repeat 2
python -m evals show before after --only demo_fault,credit
python -m evals summary before after
```

A constant of `journal_analyzer.query` can be set for one run without editing the file:

```sh
python -m evals run warm --set SYNTHESIS_TEMPERATURE=0.6 --tag verdict
python -m evals run wide --set JUDGMENT_TOP_K=24 --only sleep,shin
```

`--effort` and `--style` ask every scenario the way the pickers on the Ask page do, to see what a quick answer loses or what happens when a question is forced to be a verdict:

```sh
python -m evals run quick --effort quick --tag verdict
python -m evals show after quick --only credit,demo_fault
```

`show --thinking` prints the model's thinking above each answer, which is where to look when a verdict is wrong for no reason the answer gives. Each answer is also kept as JSON under `evals/.work/runs/<label>/`, with the router's plan and the prompt's size.

## Adding a scenario

Plant the facts first: write or change entries in `journal.py` so the right answer is there to be found and a tempting wrong one is too (the writer's own mistaken view, a similar fact about someone else, a stale "tomorrow"). Then add a `Scenario` to `scenarios.py` with a `want` a reader can hold an answer against. Keep tripwires few and loose. Changing the journal rebuilds its cache on the next run, and can change what other scenarios find, so run them all once afterwards.

`python -m pytest tests/test_evals.py` checks the journal and scenarios hang together and that the runner works, with the models mocked.
