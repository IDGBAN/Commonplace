from __future__ import annotations

from .config import Config
from .models import ParsedNote
from .ollama_client import OllamaClient

# prose is nearer 4, 3 leaves some slack
CHARS_PER_TOKEN_ESTIMATE = 3

# Qwen3-Embedding expects an instruction on queries only. The model card says
# to write it in English even for other languages.
_QWEN3_QUERY_TASK = (
    "Given a question about someone's personal journal, retrieve the journal "
    "entries and notes that answer it"
)

_DIM_PROBE_TEXT = "dimension probe"


def _window(config: Config, client: OllamaClient) -> int:
    # Ollama quietly caps num_ctx at the model's trained length
    configured = config.ollama.embed_num_ctx
    trained = client.context_length(config.ollama.embed_model)
    return min(configured, trained) if trained else configured


def _embed(config: Config, client: OllamaClient, text: str) -> list[float]:
    # without num_ctx Ollama uses its default window and truncates silently
    return client.embed(
        config.ollama.embed_model,
        text,
        options={"num_ctx": _window(config, client)},
    )


def probe_dimension(config: Config, client: OllamaClient) -> int:
    return len(_embed(config, client, _DIM_PROBE_TEXT))


def _document_text(raw_text: str, summary: str | None, max_chars: int) -> str:
    if len(raw_text) <= max_chars:
        return raw_text
    # summary first so the gist survives the cut
    lead = f"{summary}\n\n" if summary else ""
    return (lead + raw_text)[:max_chars].strip()


def _query_text(model: str, question: str) -> str:
    if "qwen3-embedding" in model.lower():
        return f"Instruct: {_QWEN3_QUERY_TASK}\nQuery:{question}"
    return question


def entry_vector(
    raw_text: str,
    config: Config,
    client: OllamaClient,
    summary: str | None = None,
) -> list[float] | None:
    max_chars = _window(config, client) * CHARS_PER_TOKEN_ESTIMATE
    text = _document_text(raw_text.strip(), summary, max_chars)
    if not text:
        return None
    return _embed(config, client, text)


def note_vector(
    note: ParsedNote, summary: str | None, config: Config, client: OllamaClient
) -> list[float]:
    # Lots of notes are nearly empty, so the header goes in too. "my brother"
    # has to find a note that only says Relationship: Brother.
    max_chars = _window(config, client) * CHARS_PER_TOKEN_ESTIMATE
    document = f"{note.header()}\n\n{note.raw_text}".strip()
    return _embed(config, client, _document_text(document, summary, max_chars))


def embed_query(text: str, config: Config, client: OllamaClient) -> list[float]:
    return _embed(config, client, _query_text(config.ollama.embed_model, text))
