from unittest.mock import MagicMock, patch

import pytest
from conftest import copy_fixtures, make_config, plan_for

from journal_analyzer import db as db_mod
from journal_analyzer import ingester, query
from journal_analyzer.config import Config

QUESTION = "When did I go hiking?"


@pytest.fixture
def indexed(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    yield cfg, conn
    conn.close()


def _reversing_ranker() -> MagicMock:
    ranker = MagicMock()
    ranker.rerank.side_effect = lambda request: [
        {"id": p["id"], "score": 1.0 - i * 0.1}
        for i, p in enumerate(reversed(request.passages))
    ]
    return ranker


def test_config_defaults(tmp_path):
    cfg = make_config(tmp_path)
    assert cfg.reranking.enabled is True
    assert cfg.reranking.model == "ms-marco-MiniLM-L-12-v2"


def test_config_parsing(tmp_path):
    cfg = Config.model_validate(
        {
            "vault": {"path": str(tmp_path / "vault")},
            "reranking": {"enabled": False, "model": "custom-model"},
        }
    )
    assert cfg.reranking.enabled is False
    assert cfg.reranking.model == "custom-model"


def test_reranked_order_replaces_the_fusion_order(indexed, fake_client):
    cfg, conn = indexed
    cfg.reranking.model = "test-model"
    plan = plan_for("factual", keywords=["hiking"])

    with patch("journal_analyzer.query._get_ranker") as get_ranker:
        get_ranker.return_value = _reversing_ranker()
        reranked = query._retrieve(QUESTION, plan, cfg, conn, fake_client)
        get_ranker.assert_called_once_with("test-model")

    cfg.reranking.enabled = False
    baseline = query._retrieve(QUESTION, plan, cfg, conn, fake_client)

    assert [r["id"] for r in reranked] == [r["id"] for r in reversed(baseline)]


def test_disabled_reranking_never_loads_the_model(indexed, fake_client):
    cfg, conn = indexed
    cfg.reranking.enabled = False
    with patch("journal_analyzer.query._get_ranker") as get_ranker:
        results = query._retrieve(
            QUESTION, plan_for("factual", keywords=["hiking"]), cfg, conn, fake_client
        )
    get_ranker.assert_not_called()
    assert len(results) == 2


def test_a_single_candidate_short_circuits(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path, names=["2024-01-15.md"])
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)

    with patch("journal_analyzer.query._get_ranker") as get_ranker:
        results = query._retrieve(
            QUESTION, plan_for("factual", keywords=["hiking"]), cfg, conn, fake_client
        )
    get_ranker.assert_not_called()
    assert len(results) == 1


def test_an_unusable_reranker_degrades_to_the_fusion_order(indexed, fake_client, monkeypatch):
    monkeypatch.setattr(query, "_RERANK_WARNED", False)
    cfg, conn = indexed
    plan = plan_for("factual", keywords=["hiking"])

    with patch("journal_analyzer.query._get_ranker", side_effect=OSError("no network")):
        degraded = query._retrieve(QUESTION, plan, cfg, conn, fake_client)

    cfg.reranking.enabled = False
    baseline = query._retrieve(QUESTION, plan, cfg, conn, fake_client)
    assert [r["id"] for r in degraded] == [r["id"] for r in baseline]
