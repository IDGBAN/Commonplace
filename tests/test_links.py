import json
from datetime import date

from conftest import add_entry, add_note, invoke_cli, make_config
from typer.testing import CliRunner

from journal_analyzer import cli, db, ingester, links, store


def _by_target(found):
    return {b.target: b for b in found}


def test_each_broken_link_gets_a_reason(conn, config):
    add_note(conn, "John Doe", aliases=["JD"])
    add_note(conn, "John Lee", aliases=["JD"])
    add_note(conn, "Bear Mountain", kind="locations", links=["Garden Project"])
    add_entry(conn, date(2024, 1, 15), path="/vault/15.md",
              links=["JD", "Jon Doe", "2024-01-20", "2024-02-30", "Nobody Yet", "Al"])
    add_entry(conn, date(2024, 1, 16), path="/vault/16.md", links=["Nobody Yet"])
    found = links.diagnose(config, conn)
    by = _by_target(found)
    assert by["jd"].reason == "shared_alias"
    assert "John Doe and John Lee" in by["jd"].why
    assert (by["jon doe"].reason, by["jon doe"].like) == ("typo", "John Doe")
    assert by["jon doe"].fix == "Did you mean [[John Doe]]?"
    assert by["2024-01-20"].reason == "no_entry"
    assert by["2024-02-30"].reason == "not_a_date"
    assert by["al"].reason == "missing"  # too short to guess at
    assert (by["garden project"].entries, by["garden project"].notes) == (0, 1)
    assert found[0].target == "nobody yet" and found[0].entries == 2
    assert found[0].last == "2024-01-16"


def test_files_the_cache_knows_about(conn, config):
    store.upsert_file(conn, "/vault/Links/People/Secret.md", "h", "private")
    store.upsert_file(conn, "/vault/Links/People/Broken.md", "h", "error", "unreadable: bad bytes")
    add_entry(conn, date(2024, 1, 15), path="/vault/15.md", links=["Secret", "Broken"])
    by = _by_target(links.diagnose(config, conn))
    assert by["secret"].reason == "private"
    assert by["broken"].reason == "errored"
    assert "bad bytes" in by["broken"].why


def _vault(tmp_path):
    cfg = make_config(tmp_path, notes_paths=["Links"], daily_note_glob="[0-9][0-9][0-9][0-9]/**/*.md",
                      ignore_patterns=["Archive"])
    files = {
        "2024/2024-01-15.md": "Climbed with [[Josh]], [[Old Friend]] and [[Ex]]. Saw [[John Doe]].",
        "Josh.md": "From the gym.",
        ".trash/Old Friend.md": "Deleted.",
        "Archive/Ex.md": "Archived.",
        "Links/People/John Doe.md": "Friend.",
    }
    for rel, text in files.items():
        (cfg.vault.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (cfg.vault.path / rel).write_text(text, encoding="utf-8")
    return cfg


def test_vault_files_explain_links_outside_the_note_folders(tmp_path, fake_client):
    cfg = _vault(tmp_path)
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    without = _by_target(links.diagnose(cfg, conn))
    assert {b.reason for b in without.values()} == {"missing"}

    by = _by_target(links.diagnose(cfg, conn, links.vault_markdown(cfg)))
    assert (by["josh"].reason, by["josh"].path) == ("outside", "Josh.md")
    assert by["josh"].fix == 'Add "Josh.md" to [notes] paths, or set other_files = true.'
    assert by["old friend"].reason == "hidden" and ".trash" in by["old friend"].why
    assert by["ex"].reason == "ignored"
    assert "john doe" not in by
    conn.close()


def test_links_command_lists_them_and_one_in_detail(tmp_path, fake_client):
    cfg = _vault(tmp_path)
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    conn.close()
    # invoke_cli writes its own config, so these come along on the command line
    path = tmp_path / "links.toml"
    path.write_text(
        f"[vault]\npath = '{cfg.vault.path.as_posix()}'\n"
        "daily_note_glob = '[0-9][0-9][0-9][0-9]/**/*.md'\nignore_patterns = ['Archive']\n"
        f"[db]\npath = '{cfg.db.path.as_posix()}'\n[notes]\npaths = ['Links']\n",
        encoding="utf-8",
    )
    listed = CliRunner().invoke(cli.app, ["links", "--json", "--config", str(path)])
    assert listed.exit_code == 0, listed.output
    data = json.loads(listed.output)
    assert data["total"] == 3
    assert {b["reason"] for b in data["links"]} == {"outside", "hidden", "ignored"}

    one = CliRunner().invoke(cli.app, ["links", "Josh", "--config", str(path)])
    assert one.exit_code == 0, one.output
    assert "isn't in your note folders" in one.output
    assert "2024-01-15" in one.output

    fine = CliRunner().invoke(cli.app, ["links", "John Doe", "--config", str(path)])
    assert "finds a note or a day, so it counts" in fine.output
    unknown = CliRunner().invoke(cli.app, ["links", "Nobody", "--config", str(path)])
    assert unknown.exit_code == 1


def test_no_broken_links(tmp_path, config, conn):
    add_note(conn, "John Doe")
    add_entry(conn, date(2024, 1, 15), links=["John Doe"])
    conn.close()
    result = invoke_cli(tmp_path, config, "links")
    assert result.exit_code == 0, result.output
    assert "Every link finds a note or a day." in result.output
