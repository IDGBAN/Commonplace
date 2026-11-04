import json
from datetime import date

from conftest import invoke_cli, make_config

from journal_analyzer import obsidian

DAY = date(2026, 3, 4)


def test_moment_formats_turn_into_globs_and_dates():
    assert obsidian.format_date("YYYY/MM/YYYY-MM-DD", DAY) == "2026/03/2026-03-04"
    assert obsidian.format_date("Do MMMM YYYY, dddd", DAY) == "4th March 2026, Wednesday"
    assert obsidian.format_date("[Week] D.M.YY", DAY) == "Week 4.3.26"
    assert obsidian.to_glob("YYYY/MM/YYYY-MM-DD") == (
        "[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    )
    assert obsidian.to_glob("DD MMM YYYY") == "[0-9][0-9] * [0-9][0-9][0-9][0-9]"
    assert obsidian.to_strptime("DD.MM.YYYY dddd") == "%d.%m.%Y %A"
    assert obsidian.to_strptime("Do MMMM YYYY") is None
    assert not obsidian.supported("YYYY-[W]ww")
    assert obsidian.supported("[Journal] YYYY-MM-DD")


def _vault(tmp_path, **vault):
    cfg = make_config(tmp_path, **vault)
    settings = cfg.vault.path / ".obsidian"
    settings.mkdir()
    return cfg, settings


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_no_obsidian_folder_means_nothing_to_check(tmp_path):
    assert obsidian.check(make_config(tmp_path)) is None


def test_daily_notes_that_the_config_would_miss(tmp_path):
    cfg, settings = _vault(tmp_path, daily_note_glob="Journal/*.md", date_format="%d-%m-%Y")
    _write(settings / "daily-notes.json", {"format": "YYYY/MM/YYYY-MM-DD", "folder": "Daily"})
    glob, dates = obsidian.check(cfg, DAY)
    assert not glob.ok and "Daily/2026/03/2026-03-04.md" in glob.message
    assert glob.toml == (
        "daily_note_glob = 'Daily/[0-9][0-9][0-9][0-9]/[0-9][0-9]/"
        "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md'"
    )
    # the ISO date in the name still reads, whatever date_format says
    assert dates.ok

    cfg.vault.daily_note_glob = "Daily/**/*.md"
    cfg.vault.date_format = "%Y"
    _write(settings / "daily-notes.json", {"format": "DD.MM.YYYY", "folder": "Daily"})
    glob, dates = obsidian.check(cfg, DAY)
    assert glob.ok
    assert not dates.ok and dates.toml == "date_format = '%d.%m.%Y'"


def test_default_daily_notes_match_the_default_config(tmp_path):
    cfg, settings = _vault(tmp_path)
    _write(settings / "daily-notes.json", {})
    assert all(a.ok for a in obsidian.check(cfg, DAY))


def test_templates_excluded_files_and_hidden_folders(tmp_path):
    cfg, settings = _vault(tmp_path, ignore_patterns=[".obsidian"])
    _write(settings / "templates.json", {"folder": "Templates"})
    _write(settings / "plugins" / "templater-obsidian" / "data.json", {"templates_folder": "Meta/Templater"})
    _write(settings / "app.json", {"userIgnoreFilters": ["Archive/", "/^Old.*/"]})
    root = cfg.vault.path
    for rel in ["Templates/Daily.md", "Meta/Templater/Weekly.md", "Archive/2020-01-01.md", ".trash/2021-01-01.md",
                "2026-03-04.md"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    advice = obsidian.check(cfg, DAY)
    templates = [a for a in advice if "template files" in a.message]
    assert [a.message.split(" in ")[1].split(" ")[0] for a in templates] == ["Templates", "Meta/Templater"]
    assert templates[-1].toml == 'ignore_patterns = [".obsidian", "Meta/Templater"]'
    excluded = next(a for a in advice if "Obsidian excludes" in a.message)
    assert "Archive" in excluded.message and excluded.toml == 'ignore_patterns = [".obsidian", "Archive"]'
    hidden = next(a for a in advice if "hidden folders" in a.message)
    assert "1 in .trash" in hidden.message
    assert hidden.toml == 'ignore_patterns = [".obsidian", ".trash"]'

    cfg.vault.ignore_patterns = [".obsidian", ".trash", "Templates", "Meta", "Archive"]
    assert all(a.ok for a in obsidian.check(cfg, DAY))


def test_doctor_reports_them(tmp_path, monkeypatch, fake_client):
    cfg, settings = _vault(tmp_path)
    _write(settings / "daily-notes.json", {"format": "YYYY-MM-DD", "folder": "Daily"})
    (cfg.vault.path / ".trash").mkdir()
    (cfg.vault.path / ".trash" / "2021-01-01.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr("journal_analyzer.cli.OllamaClient", lambda config: fake_client)
    result = invoke_cli(tmp_path, cfg, "doctor")
    text = " ".join(result.output.split())
    assert "OK daily_note_glob picks Obsidian's daily notes" in text
    assert "WARN Files in hidden folders are indexed (1 in .trash)" in text
    assert "in config.toml: ignore_patterns = [\".trash\"]" in text
