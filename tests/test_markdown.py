"""The browser page's Markdown, run with node. Skipped where node isn't installed."""

import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="needs node to run the page's script")

CATALOG = {
    "entries": [{"date": "2024-04-14", "id": 7}],
    "notes": [
        {"id": 3, "title": "Maya Okafor", "aliases": ["Maya"], "learned": ["Mai"]},
        {"id": 4, "title": "Maya", "aliases": [], "learned": []},
    ],
}


def render(*texts: str) -> list[dict]:
    script = resources.files("journal_analyzer").joinpath("browse_static", "app.js")
    done = subprocess.run(
        [NODE, str(Path(__file__).with_name("render_markdown.js")), str(script)],
        input=json.dumps({"catalog": CATALOG, "texts": list(texts)}),
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def html(text: str) -> str:
    return render(text)[0]["html"]


def test_an_answer_keeps_its_shape():
    out = html(
        "You ran **twice**, *slowly*.\nSecond line.\n\n## Runs\n\n- River loop\n  - with Maya\n- 10k\n\n"
        "1. one\n2. two\n\n> quoted\n\n---\n\n`code` and ~~gone~~"
    )
    assert "<p>You ran <strong>twice</strong>, <em>slowly</em>.\nSecond line.</p>" in out
    assert "<h3>Runs</h3>" in out
    assert "<ul><li>River loop<ul><li>with Maya</li></ul></li><li>10k</li></ul>" in out
    assert "<ol><li>one</li><li>two</li></ol>" in out
    assert "<blockquote><p>quoted</p></blockquote>" in out
    assert "<hr></hr>" in out
    assert "<code>code</code> and <s>gone</s>" in out


def test_a_table_and_what_follows_it():
    out = html("| Month | Mood |\n|---|---:|\n| March | 7.1 |\n| April |\n\nafter")
    assert "<th>Month</th><th>Mood</th>" in out
    assert "<tr><td>March</td><td>7.1</td></tr><tr><td>April</td><td></td></tr>" in out
    assert out.endswith("<p>after</p>")
    # a line with a bar in it and no rule under it is just a line
    assert html("this | that") == "<p>this | that</p>"


def test_days_and_notes_link_to_their_pages():
    out = html("On 2024-04-14 and [[2024-04-14#Evening]] with [[Maya Okafor|her]], [[mai]] and [[Nobody]]. Not 2024-04-15.")
    assert '<a class="cite day" href="#/entry/7">2024-04-14</a>' in out
    assert '<a class="cite" href="#/entry/7?at=Evening">day 2024-04-14 \u203a Evening</a>' in out
    assert '<a class="cite" href="#/note/3">her</a>' in out
    assert '<a class="cite" href="#/note/3">mai</a>' in out
    assert '<span class="cite none" title="No note by that name">Nobody</span>' in out
    assert "Not 2024-04-15." in out
    # a note's own title wins over another note's alias
    assert '<a class="cite" href="#/note/4">Maya</a>' in html("[[Maya]]")


def test_nothing_written_is_read_as_html():
    out = html('<script>alert(1)</script> <img src=x onerror=alert(1)> **<b>bold</b>** [[<i>x</i>]]')
    assert "<script" not in out and "<img" not in out and "<b>" not in out and "<i>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "<strong>&lt;b&gt;bold&lt;/b&gt;</strong>" in out


def test_only_web_links_become_links():
    out = html("[docs](https://example.com/a_b) [bad](javascript:alert(1)) [file](file:///c:/x) [data](data:text/html,x)")
    assert '<a href="https://example.com/a_b" target="_blank" rel="noopener noreferrer">docs</a>' in out
    assert out.count("<a ") == 1
    assert "[bad](javascript:alert(1))" in out


def test_ordinary_text_is_left_alone():
    plain = "snake_case_name, 2 * 3 * 4, a_b_c and 5*6 stay, and so does **bold left open"
    assert html(plain) == f"<p>{plain}</p>"
    assert html("") == ""
    assert html("#hashtag at the start") == "<p>#hashtag at the start</p>"


def test_a_reply_still_being_written():
    # a fence that isn't closed yet takes the rest as code
    assert html("before\n```\nx = 1\n<b>") == "<p>before</p>\n<pre><code>x = 1\n&lt;b&gt;</code></pre>"
    assert html("- one\n- tw") == "<ul><li>one</li><li>tw</li></ul>"
    # a table shows its heading as soon as the rule under it starts
    assert "<th>a</th><th>b</th></tr></thead><tbody></tbody>" in html("| a | b |\n|--")


def test_one_line_for_a_list():
    text = "## A steady month\n- I ran with [[Maya Okafor|Maya]] and **slept** well.\n- Work was `loud`."
    assert render(text)[0]["plain"] == "A steady month I ran with Maya and slept well. Work was loud."


def test_a_long_answer_is_quick_to_draw():
    # it's drawn again for every word that arrives, so each pass has to be cheap
    para = "On 2024-04-14 I ran with [[Maya Okafor]] and it was **good**, *really* good, `fine`.\n"
    awkward = "*" * 3000 + " " + "_" * 3000 + " " + "[" * 3000 + " " + "|" * 3000 + "\n" + "- " * 3000
    long, hard = render(para * 400 + "\n\n- item\n" * 200, awkward)
    assert long["ms"] < 500, long["ms"]
    assert hard["ms"] < 500, hard["ms"]
