"""Every command needs a page in `journal browse`, or a reason not to have one.

Adding a command fails this until it's put in one of the lists below, so a
feature can't quietly end up command-line only.
"""

import re
from importlib import resources

from journal_analyzer import cli

# command -> an address in the browser where the same thing can be done
PAGES = {
    "ask": "/ask",
    "compare": "/compare/2024",
    "digest": "/digest/month/2024-03",
    "entities": "/notes",
    "history": "/history",
    "index": "/upkeep",
    "lapsed": "/notes",
    "links": "/notes",
    "metric": "/metrics",
    "mood": "/",
    "note": "/note/1",
    "on-this-day": "/",
    "profile": "/you",
    "review": "/review/2024",
    "rollup": "/upkeep",
    "search": "/search",
    "show": "/entry/1",
    "similar": "/entry/1",
    "stats": "/",
    "streaks": "/",
    "tags": "/tags",
    "themes": "/themes",
    "unlinked": "/note/1",
}

# what a command with a page can still only do from the command line
PAGE_CANT_YET = {
    "index": "naming files to read, the lists --dry-run prints, and --no-memo",
    "profile": "--rescan",
    "rollup": "--since, --until and --only",
    "tags": "--similar",
}

# commands waiting for a page
NO_PAGE_YET = {
    "doctor": "a setup and health page",
    "export": "a download",
    "insights": "a page of its own",
    "prompt": "a page of its own",
    "watch": "the upkeep page",
}

# commands a page wouldn't make sense for
COMMAND_LINE_ONLY = {
    "browse": "it starts the pages",
    "init": "indexing creates the cache, so there's nothing to press",
    "mcp": "an MCP client starts it and talks to it on stdin and stdout",
}


def _commands() -> set[str]:
    return {c.name or c.callback.__name__.replace("_", "-") for c in cli.app.registered_commands}


def _routes() -> list[re.Pattern[str]]:
    script = resources.files("journal_analyzer").joinpath("browse_static", "app.js").read_text("utf-8")
    table = script.split("const routes = [", 1)[1].split("];", 1)[0]
    found = re.findall(r"\[/(\^.*?\$)/, \w+", table)
    assert found, "couldn't read the routes out of app.js"
    return [re.compile(pattern.replace("\\/", "/")) for pattern in found]


def test_every_command_has_a_page_or_a_reason():
    groups = (PAGES, NO_PAGE_YET, COMMAND_LINE_ONLY)
    listed = [name for group in groups for name in group]
    assert sorted(listed) == sorted(set(listed)), "a command is in two lists"
    assert _commands() == set(listed)
    assert set(PAGE_CANT_YET) <= set(PAGES)


def test_the_pages_named_are_real():
    routes = _routes()
    for command, address in PAGES.items():
        assert any(route.search(address) for route in routes), f"{command}: no page at #{address}"
