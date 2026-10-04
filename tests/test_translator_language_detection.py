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


# --- Low-signal fallback (Lonio, 2026-10-04) --------------------------------

JOBCOACH_HEADER = """# Founding Product Builder

**Arbeitgeber:** Beispiel AG (Rechtsform in Anzeige nicht genannt)
**Standort der Stelle:** Zürich / Remote-Anteil in Anzeige nicht genannt
**Pensum:** in Anzeige nicht genannt
**Gesichert:** 2026-09-29

---

"""

STARTUP_EN_BODY = """If you think product management means writing tickets for engineers to execute, this role isn't for you.
We believe AI doesn't create talent. It amplifies it. The quality of the outcome depends on how well you organize them.
We're building an AI-native backoffice outsourcing service for finance and HR.
You should be excited by turning complex compliance logic into specs engineers can build from without gaps.
Working daily with engineering to scope work and catch edge cases before they become technical debt.
What matters to us is that you understand what AI agents can and can't reliably do today,
write specs precise enough that an engineer builds from them, and take ownership of the parts nobody has defined yet.
"""

SWISS_DE_WITH_BUZZWORDS = """Wir sind ein junges Team und bauen eine Plattform für das Backoffice von KMU.
Du bist der Product Owner und arbeitest eng mit dem Engineering zusammen, das ist dein Ownership.
Du schreibst Specs, die das Team ohne Rückfragen umsetzen kann, und bist bei Customer Calls dabei.
Wir arbeiten mit AI Agents und einem modernen Stack, und du hilfst uns, die Roadmap zu schärfen.
Bei uns zählt nicht der Titel, sondern was du für die Kunden und das Team erreichst.
"""


def test_startup_english_posting_without_classic_markers_is_english():
    """Lonio regression: almost no classic markers, German Jobcoach header on top."""
    assert is_primarily_english(JOBCOACH_HEADER + STARTUP_EN_BODY) is True


def test_swiss_german_posting_with_english_buzzwords_stays_german():
    assert is_primarily_english(JOBCOACH_HEADER + SWISS_DE_WITH_BUZZWORDS) is False


def test_jobcoach_header_is_stripped_before_function_word_count():
    from cv_tailor.agents.translator import _posting_body
    body = _posting_body(JOBCOACH_HEADER + STARTUP_EN_BODY)
    assert body.startswith("If you think product management")
    assert "Arbeitgeber" not in body


def test_function_word_fallback_needs_enough_text():
    from cv_tailor.agents.translator import _function_word_verdict
    assert _function_word_verdict("the role is for you") is None
