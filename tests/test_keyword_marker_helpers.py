"""Tests for keyword-marker helpers (no LLM calls)."""
import json

from cv_tailor.agents.keyword_marker import (
    _apply_bolds,
    _bold_phrase_in_text,
    _parse_phrase_response,
    _split_preamble,
)


# --- _parse_phrase_response ---

def test_parse_valid_json():
    raw = json.dumps({
        "summary": ["Cloud-First-Strategie", "KI-gestützte Workflows"],
        "stations": {
            "2023–2025 | HealthApp – Senior PO": ["KI-Fachgruppe", "HealthAppConnect"],
        },
    })
    result = _parse_phrase_response(raw)
    assert result["summary"] == ["Cloud-First-Strategie", "KI-gestützte Workflows"]
    assert result["stations"]["2023–2025 | HealthApp – Senior PO"] == ["KI-Fachgruppe", "HealthAppConnect"]


def test_parse_with_code_fence():
    raw = "```json\n" + json.dumps({"summary": ["Roadmap"], "stations": {}}) + "\n```"
    result = _parse_phrase_response(raw)
    assert result["summary"] == ["Roadmap"]


def test_parse_invalid_json_returns_empty():
    result = _parse_phrase_response("not json")
    assert result == {"summary": [], "stations": {}}


# --- _bold_phrase_in_text ---

def test_bold_phrase_basic():
    text = "Ich habe Cloud-First-Strategie eingeführt."
    result, applied = _bold_phrase_in_text(text, "Cloud-First-Strategie", set())
    assert result == "Ich habe **Cloud-First-Strategie** eingeführt."
    assert applied is True


def test_bold_phrase_not_applied_twice():
    used: set[str] = {"Cloud-First-Strategie"}
    text = "Cloud-First-Strategie ist gut."
    result, applied = _bold_phrase_in_text(text, "Cloud-First-Strategie", used)
    assert applied is False
    assert "**" not in result


def test_bold_phrase_skips_headers():
    text = "### 2023–2025 | Cloud-First-Strategie\n\nKI-gestützte Workflows eingeführt."
    result, applied = _bold_phrase_in_text(text, "Cloud-First-Strategie", set())
    assert applied is False


def test_bold_phrase_not_double_bolded():
    text = "Ich habe **Cloud-First-Strategie** eingeführt."
    result, applied = _bold_phrase_in_text(text, "Cloud-First-Strategie", set())
    assert applied is False


def test_bold_phrase_absent():
    text = "Some text without the phrase."
    result, applied = _bold_phrase_in_text(text, "Cloud-First-Strategie", set())
    assert applied is False
    assert result == text


# --- _apply_bolds ---

def test_apply_bolds_summary_limit():
    body = "## Management Summary\n\nA B C D E\n\n## Berufserfahrung\n\n### 2023 | Firm\n\nwork here\n"
    phrases = {
        "summary": ["A", "B", "C", "D", "E"],  # 5 phrases, limit is 4
        "stations": {},
    }
    marked, stats = _apply_bolds(body, phrases)
    assert stats["summary_bolds"] == 4
    assert marked.count("**") == 8  # 4 bolds × 2 markers each


def test_apply_bolds_global_dedup():
    body = "## Management Summary\n\nKI-Fachgruppe eingesetzt.\n\n## Berufserfahrung\n\n### 2023 | HealthApp\n\nKI-Fachgruppe aufgebaut.\n"
    phrases = {
        "summary": ["KI-Fachgruppe"],
        "stations": {"2023 | HealthApp": ["KI-Fachgruppe"]},  # same phrase — should not apply twice
    }
    marked, stats = _apply_bolds(body, phrases)
    # Should appear exactly once (global dedup)
    assert marked.count("**KI-Fachgruppe**") == 1


# --- _split_preamble ---

def test_split_preamble_strips_metadata():
    cv = "# Finaler CV (DE)\n\n**Run:** abc\n\n## Management Summary\n\nText.\n"
    preamble, body = _split_preamble(cv)
    assert "## Management Summary" in body
    assert "**Run:**" in preamble
    assert "**Run:**" not in body
