"""Tests for clarifications topic-gating.

Pins the Cross-Beleg-Fusion-Guard (2026-05-18): past Q/A answers must
not leak into runs whose posting+analysis don't activate the same topic.
"""
import json
from pathlib import Path

import pytest

from cv_tailor import clarifications as cl
from cv_tailor.clarifications import (
    _match_topics,
    _resolve_entry_topics,
    format_clarifications_for_prompt,
    migrate_topics,
)


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch) -> Path:
    """A clean clarifications.json that doesn't touch the real store."""
    p = tmp_path / "clarifications.json"
    monkeypatch.setattr(cl, "CLARIFICATIONS_PATH", p)
    return p


# Sample answers — each labelled with the topic they should classify to.
ANSWER_ANALYTICS = (
    "Bei HealthApp habe ich Produktanalysen für die interne Nutzung aufgebaut; "
    "Skripte, welche Daten aufbereitet haben, damit ich sie mit ChatGPT auswerten konnte. "
    "KPI-Frameworks und Dashboard-Aufbau gehören zu meinem Repertoire."
)

ANSWER_LANGUAGES = (
    "Im CV nur die tatsächlich vorhandenen Sprachkenntnisse ausweisen. "
    "Französisch B1 ist vorhanden. Italienisch und Portugiesisch nicht erwähnen."
)

ANSWER_ML_AI = (
    "Bei MediaCorp war ich an der Konzeption und Pilotierung eines ML-basierten "
    "Empfehlungssystems beteiligt. Feature Engineering und Model Evaluation "
    "wurden vom Data-Engineering-Team verantwortet, ich war auf Product-Owner-Seite."
)


def test_match_topics_analytics():
    topics = _match_topics(ANSWER_ANALYTICS)
    assert "analytics" in topics
    assert "*" not in topics  # not universal — confidently classified


def test_match_topics_languages():
    topics = _match_topics(ANSWER_LANGUAGES)
    assert "languages" in topics


def test_match_topics_ml_ai():
    topics = _match_topics(ANSWER_ML_AI)
    assert "ml_ai" in topics


def test_match_topics_empty_returns_universal():
    """Empty / very short text falls back to universal so the entry is
    never silently excluded from future prompts."""
    assert _match_topics("") == ["*"]
    assert _match_topics("kurz") == ["*"]


def test_match_topics_deterministic():
    """Classifier must produce identical output for identical input —
    relied upon by clarifications.gated cache-key stability."""
    a = _match_topics(ANSWER_ANALYTICS)
    b = _match_topics(ANSWER_ANALYTICS)
    assert a == b


def test_resolve_topics_uses_stored_field(tmp_store):
    """Pre-migration entries (no topics field) classify on-read; entries
    with stored topics return them verbatim."""
    entry_unmigrated = {
        "run_id": "x", "questions_markdown": "Q?",
        "answers_markdown": ANSWER_LANGUAGES,
    }
    assert "languages" in _resolve_entry_topics(entry_unmigrated)

    entry_migrated = {
        "run_id": "y", "topics": ["custom_topic"],
        "questions_markdown": "Q?", "answers_markdown": ANSWER_LANGUAGES,
    }
    assert _resolve_entry_topics(entry_migrated) == ["custom_topic"]


def test_format_filters_to_topic_match(tmp_store):
    """A past languages-only answer must NOT appear when the current
    context is purely about gastronomy + team management."""
    tmp_store.write_text(json.dumps({
        "version": 1,
        "entries": [
            {"run_id": "old_lang",
             "questions_markdown": "Sprachen?",
             "answers_markdown": ANSWER_LANGUAGES,
             "topics": ["languages"]},
            {"run_id": "old_gastro",
             "questions_markdown": "Restaurant?",
             "answers_markdown": "Bei GastroSaaS haben wir mit Gastronomiebetreibern gearbeitet.",
             "topics": ["domain_gastro"]},
        ],
    }), encoding="utf-8")
    current = "Restaurant-Plattform Engineering Manager. Gastronomiebetreiber und Servicepersonal."
    out = format_clarifications_for_prompt(path=tmp_store, current_context=current)
    assert "old_gastro" in out
    assert "old_lang" not in out
    assert "Französisch B1" not in out


def test_format_includes_universal_entries_always(tmp_store):
    """Universal-topic entries are always included regardless of current context."""
    tmp_store.write_text(json.dumps({
        "version": 1,
        "entries": [
            {"run_id": "universal_fact",
             "questions_markdown": "?",
             "answers_markdown": "Alex heisst tatsächlich Alex.",
             "topics": ["*"]},
        ],
    }), encoding="utf-8")
    out = format_clarifications_for_prompt(
        path=tmp_store, current_context="Anything specific here"
    )
    assert "universal_fact" in out


def test_format_no_filter_when_context_empty(tmp_store):
    """Backwards-compat: caller without context gets everything."""
    tmp_store.write_text(json.dumps({
        "version": 1,
        "entries": [
            {"run_id": "lang_entry",
             "questions_markdown": "?",
             "answers_markdown": ANSWER_LANGUAGES,
             "topics": ["languages"]},
        ],
    }), encoding="utf-8")
    out = format_clarifications_for_prompt(path=tmp_store, current_context=None)
    assert "lang_entry" in out


def test_migrate_topics_idempotent(tmp_store):
    """Running migration twice is safe; second run reports zero updates."""
    tmp_store.write_text(json.dumps({
        "version": 1,
        "entries": [
            {"run_id": "a",
             "questions_markdown": "Sprachen?",
             "answers_markdown": ANSWER_LANGUAGES},
            {"run_id": "b",
             "questions_markdown": "Analytics?",
             "answers_markdown": ANSWER_ANALYTICS,
             "topics": ["analytics"]},  # already migrated
        ],
    }), encoding="utf-8")
    r1 = migrate_topics(tmp_store)
    assert r1["updated"] == 1  # only 'a' migrated
    r2 = migrate_topics(tmp_store)
    assert r2["updated"] == 0
