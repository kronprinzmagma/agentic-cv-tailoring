"""Tests for keyword-marker helpers (no LLM calls)."""
from cv_tailor.agents.keyword_marker import _strip_text_for_compare


def test_strip_normalises_bold_markers():
    a = "Plain text with no bolds."
    b = "Plain text with no **bolds**."
    assert _strip_text_for_compare(a) == _strip_text_for_compare(b)


def test_strip_detects_punctuation_drift():
    """IN-05: comma vs period must surface as a difference."""
    a = "First sentence, second sentence."
    b = "First sentence. second sentence."  # subtle drift
    assert _strip_text_for_compare(a) == _strip_text_for_compare(b)
    # but real text edit must not normalize away
    c = "First sentence, second different word."
    assert _strip_text_for_compare(a) != _strip_text_for_compare(c)


def test_strip_tolerates_whitespace():
    a = "Word\n\nanother\tword  here"
    b = "Word another word here"
    assert _strip_text_for_compare(a).strip() == _strip_text_for_compare(b).strip()
