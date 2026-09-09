from __future__ import annotations

from aegis.domain import (
    clean_text,
    credibility_label,
    importance_label,
    keyword_hits,
    short_text,
    source_credibility_score,
    source_type_from_name,
    stable_hash,
)


def test_text_helpers_are_deterministic():
    assert clean_text("  a\n b  ") == "a b"
    assert short_text("abcdef", 5) == "ab..."
    assert stable_hash(" a ", "b") == stable_hash("a", "b")


def test_source_classification_and_scores():
    assert source_type_from_name("Reuters") == "wire"
    assert source_type_from_name("Federal Reserve / Fed") == "official"
    assert source_type_from_name("Finance KOL Digest") == "social"
    assert source_credibility_score("official") > source_credibility_score("wire") > source_credibility_score("social")


def test_labels_and_keyword_hits():
    assert importance_label(90) == "S"
    assert importance_label(70) == "A"
    assert credibility_label(0.9) == "HIGH"
    assert credibility_label(0.7) == "MEDIUM"
    assert keyword_hits("Fed signals a RATE HIKE", ["fed", "rate hike", "oil"]) == ["fed", "rate hike"]
