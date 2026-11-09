"""Tests for clarifications topic-gating.

Pins the Cross-Beleg-Fusion-Guard (2026-05-18): past Q/A answers must
not leak into runs whose posting does not activate the same topic.
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


def _store(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


def test_old_matching_entry_beats_recent_unrelated_ones(tmp_store):
    """2026-10-23: only entries[-12:] were ever considered, so settled facts
    from early runs aged out of every prompt. Ranking covers the whole store."""
    old = {"run_id": "old_lang", "questions_markdown": "?",
           "answers_markdown": ANSWER_LANGUAGES, "topics": ["languages"]}
    recent = [{"run_id": f"recent_{i}", "questions_markdown": "?",
               "answers_markdown": "Gastro.", "topics": ["domain_gastro"]} for i in range(12)]
    _store(tmp_store, [old] + recent)
    out = format_clarifications_for_prompt(
        path=tmp_store, current_context="Wir suchen verhandlungssichere Sprachkenntnisse."
    )
    assert "old_lang" in out
    assert "recent_0" not in out


def test_ranking_prefers_more_overlap_then_recency_and_keeps_order(tmp_store):
    entries = [
        {"run_id": "a_two_topics", "questions_markdown": "?", "answers_markdown": "x",
         "topics": ["analytics", "ml_ai"]},
        {"run_id": "b_one_topic", "questions_markdown": "?", "answers_markdown": "x",
         "topics": ["analytics"]},
        {"run_id": "c_one_topic_newer", "questions_markdown": "?", "answers_markdown": "x",
         "topics": ["analytics"]},
    ]
    _store(tmp_store, entries)
    context = "KPI-Dashboards und ML-basierte Empfehlungssysteme"
    out = format_clarifications_for_prompt(path=tmp_store, current_context=context, limit=2)
    assert "a_two_topics" in out and "c_one_topic_newer" in out
    assert "b_one_topic" not in out
    assert out.index("a_two_topics") < out.index("c_one_topic_newer")  # chronological


def test_char_budget_skips_entries_that_do_not_fit(tmp_store):
    entries = [
        {"run_id": "huge", "questions_markdown": "?", "answers_markdown": "x" * 900,
         "topics": ["analytics", "ml_ai"]},
        {"run_id": "small", "questions_markdown": "?", "answers_markdown": "x" * 100,
         "topics": ["analytics"]},
    ]
    _store(tmp_store, entries)
    out = format_clarifications_for_prompt(
        path=tmp_store, current_context="KPI-Dashboards und ML", max_chars=500
    )
    assert "small" in out and "huge" not in out


# 2026-10-26 (Daten-Rolle bei einem Versicherer): Die Anzeige schrieb "Daten Analyse",
# "Datenprodukte" und "Power BI"; die Analytics-Klärungen (A/B-Tests, Rohdaten-Aufbereitung) wurden
# nicht aktiviert, der CV blieb in der Datenkompetenz abstrakt.
POSTING_DATENPRODUKTE = (
    "Product Owner Datenprodukte. Du stellst sicher, dass die Daten zusammengefasst "
    "und für die Auswertung optimiert werden können (Hadron-Stack). Berichte werden "
    "mit Power BI erstellt. Erfahrung im Bereich Daten Analyse und Daten Engineering "
    "von Vorteil."
)


@pytest.mark.parametrize(
    "text",
    [
        "Erfahrung im Bereich Daten Analyse von Vorteil, Berichte in Power BI.",
        "Verantwortung für den Aufbau unserer Datenprodukte im Nicht-Leben-Bereich.",
        "Own the data product roadmap together with the data engineering team.",
        "Berichte in Tableau oder Looker bauen und Business Intelligence verstehen.",
    ],
)
def test_data_role_phrasings_activate_analytics(text):
    assert "analytics" in _match_topics(text)


@pytest.mark.parametrize(
    "text",
    [
        "Berichterstattung an die Geschäftsleitung und Datenschutz-Reporting an den VR.",
        "Produktverantwortung für die Kundenplattform, enge Arbeit mit Design und Entwicklung.",
        "Wir bauen Software für Restaurants und Gastronomiebetriebe in der Schweiz.",
    ],
)
def test_unrelated_postings_do_not_activate_analytics(text):
    assert "analytics" not in _match_topics(text)


def test_data_posting_includes_analytics_clarification(tmp_store):
    """End-to-end: die Analytics-Klärung erreicht die Pipeline, die
    Sprachen-Klärung bleibt draussen."""
    tmp_store.write_text(json.dumps({
        "version": 1,
        "entries": [
            {"run_id": "old_ab_tests",
             "questions_markdown": "Analytics?",
             "answers_markdown": "Bei MediaCorp eigene Hypothesen, Varianten, Auswertung der A/B-Tests.",
             "topics": ["analytics"]},
            {"run_id": "old_lang",
             "questions_markdown": "Sprachen?",
             "answers_markdown": ANSWER_LANGUAGES,
             "topics": ["languages"]},
        ],
    }), encoding="utf-8")
    out = format_clarifications_for_prompt(
        path=tmp_store, current_context=POSTING_DATENPRODUKTE
    )
    assert "old_ab_tests" in out
    assert "old_lang" not in out
