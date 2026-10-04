"""Tests for the naturalisation response parser.

2026-08-25 (SBB-Lauf): the model returned a valid suggestions object and
then kept writing. The old `find("{") … rfind("}")` slice spanned both,
`json.loads` raised "Extra data", and the caller turned that into an empty
suggestion list — the UI showed "keine Vorschläge" for a response that
carried a dozen.
"""
import pytest

from cv_tailor.agents.naturalisation import _extract_json


def test_plain_object():
    assert _extract_json('{"suggestions": []}') == {"suggestions": []}


def test_fenced_object():
    assert _extract_json('```json\n{"suggestions": [1]}\n```') == {"suggestions": [1]}


def test_trailing_prose_after_object():
    text = '{"suggestions": []}\n\nIch hoffe, das hilft weiter!'
    assert _extract_json(text) == {"suggestions": []}


def test_trailing_prose_containing_a_brace():
    """The exact shape that broke: a later `}` extended the slice."""
    text = '{"suggestions": [{"id": "s1"}]}\n\nHinweis: {siehe oben}\n'
    assert _extract_json(text) == {"suggestions": [{"id": "s1"}]}


def test_second_json_block_is_ignored():
    text = '{"suggestions": [], "lang": "de"}\n{"debug": true}'
    assert _extract_json(text) == {"suggestions": [], "lang": "de"}


def test_leading_prose_before_object():
    text = 'Hier die Vorschläge:\n{"suggestions": [], "lang": "en"}'
    assert _extract_json(text) == {"suggestions": [], "lang": "en"}


def test_nested_objects_survive():
    text = '{"suggestions": [{"id": "s1", "meta": {"a": 1}}]}'
    assert _extract_json(text)["suggestions"][0]["meta"] == {"a": 1}


def test_no_object_raises_value_error():
    with pytest.raises(ValueError):
        _extract_json("Keine Vorschläge diesmal.")


def test_unparsable_braces_raise_value_error():
    with pytest.raises(ValueError):
        _extract_json("{nicht wirklich json")
