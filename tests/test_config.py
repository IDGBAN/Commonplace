import pytest

from journal_analyzer.config import load_config
from journal_analyzer.models import ConfigError

MINIMAL = """
[vault]
path = '{vault}'
"""


def _write(tmp_path, text: str, name: str = "config.toml", encoding: str = "utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


def test_defaults_fill_in_around_the_vault_path(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL.format(vault=tmp_path)))
    assert cfg.vault.path == tmp_path.resolve()
    assert cfg.ollama.host == "http://localhost:11434"
    assert cfg.indexing.mood_scale_max == 10
    assert cfg.reranking.enabled is True


def test_paths_are_expanded_and_absolute(tmp_path):
    cfg = load_config(
        _write(tmp_path, MINIMAL.format(vault=tmp_path) + "\n[db]\npath = '~/x.db'\n")
    )
    assert cfg.db.path.is_absolute()
    assert "~" not in str(cfg.db.path)


def test_a_utf8_bom_does_not_break_parsing(tmp_path):
    cfg = load_config(
        _write(tmp_path, MINIMAL.format(vault=tmp_path), encoding="utf-8-sig")
    )
    assert cfg.vault.path == tmp_path.resolve()


def test_missing_file_names_the_path_and_the_fix(tmp_path):
    with pytest.raises(ConfigError, match=r"config\.example\.toml"):
        load_config(tmp_path / "absent.toml")


def test_unparsable_toml_explains_windows_backslashes(tmp_path):
    path = _write(tmp_path, '[vault]\npath = "C:\\Users\\me"\n')
    with pytest.raises(ConfigError, match="forward slashes"):
        load_config(path)


def test_invalid_values_are_reported_per_field(tmp_path):
    path = _write(tmp_path, "[vault]\ndate_source = 'weekday'\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    message = str(exc.value)
    assert "vault.path" in message
    assert "vault.date_source" in message


def test_out_of_range_numbers_are_rejected(tmp_path):
    path = _write(
        tmp_path,
        MINIMAL.format(vault=tmp_path) + "\n[indexing]\nmood_scale_max = 1\n",
    )
    with pytest.raises(ConfigError, match=r"indexing\.mood_scale_max"):
        load_config(path)


def test_embed_defaults_target_qwen3_embedding(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL.format(vault=tmp_path)))
    assert cfg.ollama.embed_model == "qwen3-embedding:0.6b"
    assert cfg.ollama.embed_num_ctx == 8192


def test_a_tiny_embed_window_is_rejected(tmp_path):
    text = MINIMAL.format(vault=tmp_path) + """
[ollama]
embed_num_ctx = 64
"""
    with pytest.raises(ConfigError, match=r"ollama[.]embed_num_ctx"):
        load_config(_write(tmp_path, text))


def test_reasoning_defaults_off_for_fast_and_to_the_model_for_precise(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL.format(vault=tmp_path)))
    assert cfg.ollama.fast_model_think is False
    assert cfg.ollama.precise_model_think is None


def test_reasoning_can_be_switched_per_role(tmp_path):
    text = MINIMAL.format(vault=tmp_path) + """
[ollama]
fast_model_think = true
precise_model_think = false
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.ollama.fast_model_think is True
    assert cfg.ollama.precise_model_think is False
