"""ART / PI Planning in a posting must lead to the SAFe evidence (ZKB 2026-11-26)."""
import pytest

from cv_tailor.experience_activation import activate_entries, detect_job_themes

INDEX = {"entries": [
    {"id": "BELG-1", "snippet": "Release Plannings im SAFe-Umfeld koordiniert",
     "quelle_typ": "standard_cv"},
    {"id": "BELG-2", "snippet": "Backlog für 3 Teams priorisiert", "quelle_typ": "standard_cv"},
    {"id": "BELG-3", "snippet": "psychological safety im Team gefördert",
     "quelle_typ": "standard_cv"},
]}


def _themes(text: str) -> set[str]:
    return {theme for theme, _score in detect_job_themes(text)}


@pytest.mark.parametrize("text", [
    "Sparringspartner:in für dezentrale ART-Teams",
    "working closely with the AI ART and other units",
    "Zusammenarbeit im ART stärken",
    "in sechs ARTs organisiert",
    "Unser Agile Release Train liefert die Basis",
    "in sechs Agile Release Trains",
    "Du bereitest das PI Planning vor",
    "Teilnahme am PI-Planning",
])
def test_scaled_agile_terms_activate_the_safe_theme(text: str) -> None:
    assert "scaled_agile" in _themes(text)
    hits = activate_entries(text, INDEX)["scaled_agile"]
    assert [e.id for e in hits] == ["BELG-1"]  # word overlap alone does not qualify


@pytest.mark.parametrize("text", [
    "Wir verändern die Art und Weise, wie wir arbeiten",
    "Art der Anstellung: unbefristet",
    "ART DER ANSTELLUNG",
    "STATE-OF-THE-ART Technologie, state-of-the-art",
    "SMART Ziele, START im März, Artikel",
    "a safe place to work",
    "API planning für die Schnittstellen",
])
def test_ordinary_words_do_not_activate_the_safe_theme(text: str) -> None:
    assert "scaled_agile" not in _themes(text)
