from datetime import date

from conftest import add_entry, add_note
from typer.testing import CliRunner

from journal_analyzer import cli, store


def _complete(tmp_path, config, *words: str) -> list[str]:
    path = tmp_path / "config.toml"
    path.write_text(
        f"[vault]\npath = '{config.vault.path.as_posix()}'\n[db]\npath = '{config.db.path.as_posix()}'\n",
        encoding="utf-8",
    )
    # the shell line is split like a POSIX shell, where backslashes escape
    line = ["journal", words[0], "--config", path.as_posix(), *words[1:]]
    result = CliRunner().invoke(
        cli.app, [], prog_name="journal",
        env={"_JOURNAL_COMPLETE": "complete_bash", "COMP_WORDS": " ".join(line),
             "COMP_CWORD": str(len(line) - 1)},
    )
    assert result.exit_code == 0, result.output
    return [line for line in result.output.splitlines() if line]


def _journal(conn):
    add_note(conn, "John Doe", aliases=["Johnny"], properties={"Relationship": "Friend, School"})
    add_note(conn, "Jane Doe", kind="family")
    add_entry(conn, date(2024, 1, 15), tags=["hiking"], links=["Jon Doe"], path="/vault/a.md")
    entry = add_entry(conn, date(2024, 3, 2), tags=["home"], path="/vault/b.md")
    store.replace_entry_metrics(conn, entry, {"sleep": 7.0})


def test_names_tags_and_metrics_complete_from_the_cache(tmp_path, config, conn):
    _journal(conn)
    conn.close()
    assert _complete(tmp_path, config, "note", "j") == ["jane Doe", "john Doe", "johnny"]
    assert _complete(tmp_path, config, "mood", "--about", "Jo") == ["John Doe", "Johnny"]
    assert _complete(tmp_path, config, "search", "x", "--tag", "h") == ["hiking", "home"]
    assert _complete(tmp_path, config, "metric", "s") == ["sleep"]
    assert _complete(tmp_path, config, "entities", "--type", "f") == ["family"]
    assert _complete(tmp_path, config, "links", "jon") == ["jon doe"]


def test_periods_days_and_groups_complete(tmp_path, config, conn):
    _journal(conn)
    conn.close()
    assert _complete(tmp_path, config, "digest", "2024") == ["2024", "2024-03", "2024-01"]
    assert _complete(tmp_path, config, "digest", "2024-W") == ["2024-W09", "2024-W03"]
    assert _complete(tmp_path, config, "compare", "2024", "2024-0") == ["2024-03", "2024-01"]
    assert _complete(tmp_path, config, "show", "2024-0") == ["2024-03-02", "2024-01-15"]
    assert _complete(tmp_path, config, "entities", "--by", "rel") == ["relationship"]
    assert _complete(tmp_path, config, "mood", "--group", "rel") == ["relationship="]
    assert _complete(tmp_path, config, "mood", "--group", "relationship=") == [
        "relationship=Friend", "relationship=School",
    ]


def test_no_cache_completes_nothing_quietly(tmp_path, config):
    assert _complete(tmp_path, config, "note", "j") == []
