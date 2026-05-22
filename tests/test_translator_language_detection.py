"""Tests for the Translator's language detector.

These tests freeze the bug fixes around the MLOpsCo/AI-Platform-Co runs in
2026-05: stopwords must not bias long English postings toward DE just
because the user pastes German UI chrome around the body, and the
ratio threshold must be inclusive at 0.7 when ASCII purity is high.
"""
from cv_tailor.agents.translator import (
    GERMAN_CONTENT_MARKERS,
    GERMAN_STOPWORDS,
    is_primarily_english,
)


# Long English posting wrapped in German UI chrome ("Standort:", "Über X",
# "Remote-Tage möglich"). Stopwords (`und`, `der`, `das`) leak through but
# the body is unambiguously English. Reproduces the MLOpsCo bug where
# the old detector returned False.
LONG_EN_WITH_DE_CHROME = """# Product Manager – MLOpsCo AI

**Standort:** Zürich (Hybrid, Remote-Tage möglich)
**Unternehmen:** MLOpsCo AI

## Über MLOpsCo AI

MLOpsCo AI is a Swiss-engineered AI risk control platform built to
help organizations discover, evaluate, and govern AI risk. Founded by
leading AI security researchers from ETH Zurich.

## Requirements

- 5+ years of experience in product management
- Strong execution skills and end-to-end ownership
- Excellent cross-functional collaboration
- Fluent English required, based in Zurich

## What you'll bring

- Proven track record of leading product launches
- Strong analytical skills, the ability to derive insights
- Technical background preferred
""" * 2  # ensure > 200 words so stopword fallback is OFF


SHORT_EN_BUZZWORD = (
    "Product Manager — you will own end-to-end responsibilities. "
    "We are looking for a candidate with experience in B2B SaaS. "
    "Fluent english required. Based in Zurich. Requirements: 3+ years."
)


SHORT_DE_POSTING = (
    "Produktmanager — wir suchen einen Kandidaten mit Erfahrung in B2B SaaS. "
    "Anforderungen: 3+ Jahre. Die Rolle umfasst Verantwortung für das Produkt. "
    "Bewerbung mit Deutsch fließend erforderlich. Was du mitbringst..."
)


LONG_DE_POSTING = """# Senior Product Owner — gesucht

## Über uns
Wir sind eine Schweizer Plattform für Gastronomie. Wir bieten allen
unseren Kunden modernste Software und unterstützen sie bei der
Optimierung ihrer Restaurantbetriebe.

## Aufgaben und Anforderungen
- Verantwortung für die Roadmap unserer Plattform
- Enge Zusammenarbeit mit Engineering und Sales
- Kandidat oder Kandidatin mit 5+ Jahren Erfahrung
- Kenntnisse in agilen Methoden zwingend erforderlich
- Deutsch fließend, Englisch von Vorteil

## Was wir bieten
Wettbewerbsfähiges Gehalt und flexible Arbeitszeiten.
""" * 2


def test_long_english_with_german_chrome_detected_as_english():
    """Regression test for the MLOpsCo false-negative: stopwords from the
    German UI label block ("Standort:", "Über X") leaked into an otherwise
    English posting and tipped the ratio. With the stopword gating on
    short-only and >=0.7 threshold, the verdict must now be True."""
    assert is_primarily_english(LONG_EN_WITH_DE_CHROME) is True


def test_short_english_posting_detected_as_english():
    assert is_primarily_english(SHORT_EN_BUZZWORD) is True


def test_short_german_posting_detected_as_german():
    assert is_primarily_english(SHORT_DE_POSTING) is False


def test_long_german_posting_detected_as_german():
    assert is_primarily_english(LONG_DE_POSTING) is False


def test_empty_text_short_circuits_to_false():
    assert is_primarily_english("") is False
    assert is_primarily_english("   ") is False
    # Below the 50-char minimum
    assert is_primarily_english("Too short to classify") is False


def test_stopwords_distinct_from_content_markers():
    """Hard separation: stopwords must NOT appear in content markers and
    vice versa. Mixing them would re-introduce the MLOpsCo regression."""
    assert GERMAN_STOPWORDS.isdisjoint(GERMAN_CONTENT_MARKERS)
