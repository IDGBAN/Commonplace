from journal_analyzer import cli, export


def test_export_format_choices_match_the_available_writers():
    assert {e.value for e in cli.ExportFormat} == {"json", "markdown"}
    for name in cli.ExportFormat:
        assert hasattr(export, f"to_{name.value}")
