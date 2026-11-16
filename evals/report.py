"""Prints a run's answers beside what a right answer says, for reading."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .runner import WORK
from .scenarios import SCENARIOS

_ORDER = {scenario.id: n for n, scenario in enumerate(SCENARIOS)}


def answers(label: str, work: Path = WORK) -> list[dict[str, Any]]:
    folder = work / "runs" / label
    if not folder.is_dir():
        raise ValueError(f"No run called {label} under {work / 'runs'}.")
    found = []
    for path in folder.glob("*.json"):
        if path.name == "run.json":
            continue
        got = json.loads(path.read_text(encoding="utf-8"))
        got["take"] = int(path.stem.rsplit(".", 1)[-1])
        got["label"] = label
        found.append(got)
    return sorted(found, key=lambda g: (_ORDER.get(g["id"], len(_ORDER)), g["take"]))


def about(label: str, work: Path = WORK) -> dict[str, Any]:
    path = work / "runs" / label / "run.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def show(
    labels: Sequence[str],
    only: Sequence[str] = (),
    thinking: bool = False,
    work: Path = WORK,
) -> str:
    """Every answer of these runs, grouped by scenario so runs can be compared."""
    lines: list[str] = []
    for label in labels:
        told = about(label, work)
        if told:
            lines.append(
                f"{label}: code {told.get('code')}, precise model {told.get('precise_model')}, "
                f"temperature {told.get('temperature')}, overrides {told.get('overrides') or 'none'}"
            )
    found = [g for label in labels for g in answers(label, work) if not only or g["id"] in only]
    found.sort(key=lambda g: (_ORDER.get(g["id"], len(_ORDER)), labels.index(g["label"]), g["take"]))
    last = None
    for got in found:
        if got["id"] != last:
            last = got["id"]
            lines += ["", "=" * 78, f"{got['id']}  ({', '.join(got['tags'])})", f"Asked: {got['question']}"]
            if got.get("after"):
                lines.append(f"After: {got['after']}")
            lines.append(f"A right answer: {got['want']}")
            if got.get("bad"):
                lines.append(f"It has gone wrong by: {got['bad']}")
        plan = got.get("plan") or {}
        routed = f"{plan.get('wants')}{', about now' if plan.get('current') else ''}"
        lines += [
            "",
            f"--- {got['label']} take {got['take']}: {got['stage']}, routed as {routed}, "
            f"read {got['read_entries']} entries, {got['seconds']}s, "
            f"tripwires: {'; '.join(got['tripped']) or 'none'}",
        ]
        if got.get("searched") and got["searched"] != got["question"]:
            lines.append(f"Searched for: {got['searched']}")
        if thinking and got.get("thinking"):
            lines += ["Thinking:", got["thinking"].strip(), "Answer:"]
        lines.append(got["answer"].strip())
    return "\n".join(lines).strip() + "\n"


def summary(labels: Sequence[str], work: Path = WORK) -> str:
    """One line per scenario and run: time and tripwires. Not a score."""
    lines = []
    for label in labels:
        found = answers(label, work)
        lines.append(f"{label}: {len(found)} answers, {sum(g['seconds'] for g in found)}s in all")
        for got in found:
            flag = "; ".join(got["tripped"]) or "-"
            lines.append(f"  {got['id']:<16} take {got['take']}  {got['stage']:<12} {got['seconds']:>4}s  {flag}")
    return "\n".join(lines) + "\n"
