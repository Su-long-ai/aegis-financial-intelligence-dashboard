from __future__ import annotations

import sqlite3

import aegis.pipeline as pipeline_module
from aegis.pipeline import AegisPipeline

EXPECTED_TABLES = {
    "events",
    "historical_cases",
    "market_assets",
    "news_raw",
    "push_cards",
    "reasoning_chains",
    "reviews",
    "source_profiles",
    "workflow_logs",
}


def make_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "OpenAI", None)
    monkeypatch.setattr(pipeline_module, "DEEPSEEK_API_KEY", None)
    return AegisPipeline(tmp_path / "aegis.db")


def test_database_initialization_is_local_and_repeatable(tmp_path, monkeypatch):
    db_path = tmp_path / "aegis.db"
    make_pipeline(tmp_path, monkeypatch)
    AegisPipeline(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        source_count = conn.execute("SELECT COUNT(*) FROM source_profiles").fetchone()[0]
        asset_count = conn.execute("SELECT COUNT(*) FROM market_assets").fetchone()[0]
        history_count = conn.execute("SELECT COUNT(*) FROM historical_cases").fetchone()[0]

    assert EXPECTED_TABLES <= tables
    assert source_count > 0
    assert asset_count > 0
    assert history_count > 0


def test_rule_classifier_is_deterministic_without_llm(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path, monkeypatch)
    result = pipeline.classify_news_locally(
        "Fed signals rate hike after inflation surprise",
        "Powell said rates may stay higher for longer.",
        "Reuters",
    )
    assert result.event_type == "FED_POLICY"
    assert result.importance_level == "S"
    assert result.credibility_level == "HIGH"
    assert result.urgency_level == "URGENT"
    assert "fed" in [item.lower() for item in result.matched_keywords]


def test_rule_classifier_handles_general_news(tmp_path, monkeypatch):
    pipeline = make_pipeline(tmp_path, monkeypatch)
    result = pipeline.classify_news_locally(
        "Company publishes annual report",
        "Routine corporate update.",
        "Market Feed",
    )
    assert result.event_type == "GENERAL"
    assert result.validated_sources == ["Market Feed"]
