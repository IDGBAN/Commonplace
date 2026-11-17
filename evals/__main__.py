"""python -m evals: put the scenarios to your models and read what comes back."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from journal_analyzer import query
from journal_analyzer.models import JournalError

from . import report, runner
from .scenarios import SCENARIOS


def _names(text: str) -> list[str]:
    return [name.strip() for name in text.split(",") if name.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Ask a made-up journal questions with known right answers, using your models.",
    )
    parser.add_argument("--work", type=Path, default=runner.WORK, help="where the cache and runs are kept")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="the scenarios and what each tests")

    run = commands.add_parser("run", help="ask the scenarios and keep the answers")
    run.add_argument("label", help="a name for this run, like 'before' or 'top-k-24'")
    run.add_argument("--only", default="", help="scenario ids, comma separated")
    run.add_argument("--tag", default="", help="only scenarios with one of these tags")
    run.add_argument("--repeat", type=int, default=1, help="takes of each scenario (answers vary)")
    run.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                     help="a constant of journal_analyzer.query for this run, e.g. JUDGMENT_TOP_K=24")
    run.add_argument("--config", default=None, help="config file to take the models from")
    run.add_argument("--effort", choices=query.EFFORTS, default=None, help="ask every scenario at this effort")
    run.add_argument("--style", choices=query.STYLES, default=None,
                     help="ask every scenario for this kind of answer, whatever its wording")

    show = commands.add_parser("show", help="print runs' answers beside what a right answer says")
    show.add_argument("labels", nargs="+")
    show.add_argument("--only", default="")
    show.add_argument("--thinking", action="store_true", help="include the model's thinking")

    brief = commands.add_parser("summary", help="time and tripwires for each answer of a run")
    brief.add_argument("labels", nargs="+")

    args = parser.parse_args(argv)
    out = sys.stdout
    if hasattr(out, "reconfigure"):
        out.reconfigure(encoding="utf-8")
    try:
        if args.command == "list":
            for scenario in SCENARIOS:
                after = f" (follows {scenario.after})" if scenario.after else ""
                print(f"{scenario.id:<16} {', '.join(scenario.tags):<24} {scenario.question}{after}")
        elif args.command == "run":
            folder = runner.run_with_ollama(
                args.label, args.config, _names(args.only), _names(args.tag),
                args.repeat, args.set, args.work, args.effort, args.style,
            )
            print(f"\nKept in {folder}. Read them with: python -m evals show {args.label}")
        elif args.command == "show":
            print(report.show(args.labels, _names(args.only), args.thinking, args.work), end="")
        else:
            print(report.summary(args.labels, args.work), end="")
    except (ValueError, JournalError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
