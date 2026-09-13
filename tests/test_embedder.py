import pytest
from conftest import EMBED_DIM, copy_fixtures, make_config

from journal_analyzer import db, embedder, ingester, query, store
from journal_analyzer.models import EmbeddingModelMismatchError, ParsedNote

QWEN = "qwen3-embedding:0.6b"


def _with_ollama(cfg, **changes):
    return cfg.model_copy(update={"ollama": cfg.ollama.model_copy(update=changes)})


def test_qwen3_queries_carry_the_task_instruction(config, fake_client):
    embedder.embed_query("How was March?", _with_ollama(config, embed_model=QWEN), fake_client)
    sent = fake_client.embed_calls[-1]
    assert sent.startswith("Instruct: ")
    assert sent.endswith("\nQuery:How was March?")


def test_hf_builds_of_qwen3_embedding_get_the_instruction_too(config, fake_client):
    cfg = _with_ollama(config, embed_model="hf.co/Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0")
    embedder.embed_query("How was March?", cfg, fake_client)
    assert fake_client.embed_calls[-1].startswith("Instruct: ")


def test_other_models_get_the_query_unchanged(config, fake_client):
    embedder.embed_query("How was March?", config, fake_client)
    assert fake_client.embed_calls[-1] == "How was March?"


def test_qwen3_documents_are_embedded_without_an_instruction(config, fake_client):
    cfg = _with_ollama(config, embed_model=QWEN)
    embedder.entry_vector("A quiet day by the river.", cfg, fake_client)
    assert fake_client.embed_calls[-1] == "A quiet day by the river."


def test_every_embed_call_pins_the_context_window(config, fake_client):
    cfg = _with_ollama(config, embed_num_ctx=4096)
    embedder.probe_dimension(cfg, fake_client)
    embedder.entry_vector("text", cfg, fake_client)
    embedder.embed_query("question", cfg, fake_client)
    assert fake_client.embed_options == [{"num_ctx": 4096}] * 3


def test_entries_that_fit_the_window_are_embedded_whole(config, fake_client):
    cfg = _with_ollama(config, embed_num_ctx=512)
    text = "x" * (512 * embedder.CHARS_PER_TOKEN_ESTIMATE)
    embedder.entry_vector(text, cfg, fake_client, summary="The gist.")
    assert fake_client.embed_calls[-1] == text


def test_long_entries_lead_with_their_summary_and_fit_the_window(config, fake_client):
    cfg = _with_ollama(config, embed_num_ctx=512)
    budget = 512 * embedder.CHARS_PER_TOKEN_ESTIMATE
    embedder.entry_vector("y" * (budget * 3), cfg, fake_client, summary="The gist.")
    sent = fake_client.embed_calls[-1]
    assert sent.startswith("The gist.\n\ny")
    assert len(sent) <= budget


def test_empty_entries_are_never_sent(config, fake_client):
    assert embedder.entry_vector("   \n", config, fake_client) is None
    assert fake_client.embed_calls == []


def test_notes_are_embedded_with_their_header(config, fake_client):
    note = ParsedNote(
        title="Sara Miller", kind="people", raw_text="Climbs.", source_path="x",
        aliases=["Sara"], properties={"Relationship": "Friend"},
        rel_path="Links/People/Sara Miller.md",
    )
    vector = embedder.note_vector(note, None, config, fake_client)
    assert fake_client.embed_calls[-1] == (
        "Sara Miller (people)\nFile: Links/People/Sara Miller.md\n"
        "Also called: Sara\nRelationship: Friend\n\nClimbs."
    )
    assert len(vector) == EMBED_DIM


def test_an_empty_note_is_still_embedded_by_its_header(config, fake_client):
    note = ParsedNote(title="Chess", kind="games", raw_text="", source_path="x")
    embedder.note_vector(note, None, config, fake_client)
    assert fake_client.embed_calls[-1] == "Chess (games)"


def test_saving_a_vector_again_replaces_the_old_one(conn):
    store.save_note_vector(conn, 1, [0.1] * EMBED_DIM)
    store.save_note_vector(conn, 1, [0.2] * EMBED_DIM)
    assert conn.execute("SELECT COUNT(*) c FROM notes_vec").fetchone()["c"] == 1
    assert store.notes_vec_search(conn, [0.2] * EMBED_DIM, 5) == [1]


def test_a_different_model_with_the_same_width_is_caught(conn):
    with pytest.raises(EmbeddingModelMismatchError, match="fake-embed"):
        db.ensure_vec_table(conn, EMBED_DIM, "some-other-embed")


def test_a_different_width_is_caught(conn):
    with pytest.raises(EmbeddingModelMismatchError, match="1024-dimensional"):
        db.ensure_vec_table(conn, 1024, QWEN)


def test_the_latest_tag_and_letter_case_name_the_same_model(conn):
    db.ensure_vec_table(conn, EMBED_DIM, "fake-embed:latest")
    db.ensure_vec_table(conn, EMBED_DIM, "Fake-Embed")


def test_a_cache_from_before_names_were_recorded_adopts_the_current_model(config):
    conn = db.connect(config)
    db.init_schema(conn)
    db.set_meta(conn, "embed_dim", str(EMBED_DIM))
    db.ensure_vec_table(conn, EMBED_DIM, QWEN)
    assert db.get_meta(conn, "embed_model") == QWEN
    conn.close()


def test_searching_a_cache_built_by_another_model_explains_the_fix(tmp_path, fake_client):
    # embed_model changed but nothing re-indexed yet
    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path)
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)

    fake_client.embed = lambda model, text, options=None: [0.1] * (EMBED_DIM * 2)
    switched = _with_ollama(cfg, embed_model=QWEN)
    with pytest.raises(EmbeddingModelMismatchError, match="journal init && journal index"):
        query.search("hiking", switched, conn, fake_client)
    conn.close()


def test_the_window_is_capped_at_what_the_model_was_trained_on(config, fake_client):
    # Ollama clamps num_ctx to the model's limit, so the text budget has to as well
    fake_client.context_lengths["fake-embed"] = 1024
    embedder.entry_vector("z" * 10_000, config, fake_client, summary="Gist.")
    assert fake_client.embed_options[-1] == {"num_ctx": 1024}
    assert len(fake_client.embed_calls[-1]) <= 1024 * embedder.CHARS_PER_TOKEN_ESTIMATE


def test_a_smaller_configured_window_wins_over_the_models(config, fake_client):
    fake_client.context_lengths["fake-embed"] = 32_768
    embedder.embed_query("q", _with_ollama(config, embed_num_ctx=2048), fake_client)
    assert fake_client.embed_options[-1] == {"num_ctx": 2048}
