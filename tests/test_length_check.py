"""Tests for the deterministic length-budget check.

Pins the bullet-length and summary-word-count vetos that prevent
Berufserfahrung Runde 2 from being driven by overlong bullets (88% → ?)
and Summary drift from 140-160 → 134-207.
"""
from cv_tailor.length_check import (
    BULLET_MAX_WORDS,
    SUMMARY_MAX_WORDS,
    check_berufserfahrung_bullet_lengths,
    check_section_length,
    check_summary_length,
    format_issues_for_writer,
)


# ── Summary checks ──────────────────────────────────────────────────────────

SHORT_SUMMARY = "Ich bin ein Product Manager. " * 10  # ~50 Wörter
OK_SUMMARY = "Wort " * 150  # exactly within corridor


def test_summary_under_budget_passes():
    assert check_summary_length(SHORT_SUMMARY) == []


def test_summary_at_max_passes():
    txt = "Wort " * SUMMARY_MAX_WORDS
    assert check_summary_length(txt) == []


def test_summary_overlong_vetos():
    txt = "Wort " * (SUMMARY_MAX_WORDS + 5)
    issues = check_summary_length(txt)
    assert len(issues) == 1
    assert issues[0].kind == "summary_overlong"
    assert issues[0].word_count == SUMMARY_MAX_WORDS + 5
    assert "Wörter" in issues[0].detail


def test_summary_strips_markdown_before_counting():
    """Bold and italic markers shouldn't inflate the word count."""
    bare = "Wort " * 100
    with_bolds = "**Wort** " * 100
    assert check_summary_length(bare) == check_summary_length(with_bolds)


# ── Berufserfahrung bullet checks ──────────────────────────────────────────

LONG_BULLET = (
    "Produktverantwortung für die HealthAppConnect Plattform, eine "
    "cloudbasierte Kommunikationsplattform im regulierten "
    "Gesundheitssektor mit besonderer Berücksichtigung der Anforderungen: "
    "Neustrukturierung des Backlogs, datenbasierte Priorisierung und "
    "messbare Verbesserung der Release-Stabilität bei HealthAppConnect."
)  # 26 words after stripping markdown — clearly above the 22-word budget

SHORT_BULLET = "Plattform-Ownership HealthAppConnect — Backlog und Release-Stabilität."


def test_bullet_under_budget_passes():
    draft = (
        "### 2023–2025 | HealthApp\n"
        f"- {SHORT_BULLET}\n"
    )
    assert check_berufserfahrung_bullet_lengths(draft) == []


def test_long_bullet_vetos_with_station_context():
    draft = (
        "### 2023–2025 | HealthApp\n"
        f"- {LONG_BULLET}\n"
    )
    issues = check_berufserfahrung_bullet_lengths(draft)
    assert len(issues) == 1
    assert issues[0].word_count > BULLET_MAX_WORDS
    assert "HealthApp" in issues[0].detail  # station context preserved


def test_paragraph_style_bullets_also_counted():
    """Writer sometimes produces paragraph-style (no `-` prefix) within a
    station body. They still count as bullets for length-check purposes."""
    draft = (
        "### 2015–2023 | MediaCorp – Product Owner\n"
        f"{LONG_BULLET}\n"
    )
    issues = check_berufserfahrung_bullet_lengths(draft)
    assert len(issues) == 1


def test_offender_cap_enforced():
    """When many bullets are overlong, the offender list is capped so
    the writer's round-2 context doesn't explode."""
    draft = "### 2023–2025 | HealthApp\n" + "\n".join(f"- {LONG_BULLET}" for _ in range(20))
    issues = check_berufserfahrung_bullet_lengths(draft, max_offenders=3)
    # 3 actual + 1 suppression notice
    assert len(issues) == 4
    assert issues[-1].word_count == 0
    assert "unterdrückt" in issues[-1].detail.lower()


def test_check_section_length_dispatches_correctly():
    """Wrong section names produce no issues (writer for schluesselkompetenzen
    has no length veto right now)."""
    overlong_summary = "Wort " * (SUMMARY_MAX_WORDS + 10)
    assert check_section_length("management_summary", overlong_summary)
    assert not check_section_length("schluesselkompetenzen", overlong_summary)


def test_format_issues_writer_readable():
    """The writer-facing markdown must include a heading and a numbered list."""
    issues = check_summary_length("Wort " * (SUMMARY_MAX_WORDS + 10))
    out = format_issues_for_writer(issues)
    assert out.startswith("# Längen-Check")
    assert "Pflichtkorrektur" in out
    assert "1. " in out


def test_format_empty_says_so():
    out = format_issues_for_writer([])
    assert "Keine Längen-Probleme" in out
