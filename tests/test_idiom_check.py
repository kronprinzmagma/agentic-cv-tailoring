"""Tests for the deterministic English idiom (calque) check.

Pins the detector that catches German constructions surviving translation
into the English CV — the class of phrasing an external review flagged
~8× per CV in the Rivia and grape runs (2026-08-12/13).
"""
from cv_tailor.idiom_check import (
    check_idioms,
    check_missing_articles,
    format_findings,
    write_idiom_report,
)


def _matched(text: str) -> list[str]:
    return [f.matched.lower() for f in check_idioms(text)]


def test_clean_english_produces_no_findings():
    clean = (
        "I took GastroSaaS from idea to exit to local-directory.example as the sole PM.\n"
        "Brought app development in-house and line-managed 5 product managers.\n"
        "Used the analyses to inform roadmap prioritization.\n"
    )
    assert check_idioms(clean) == []


def test_detects_leadership_calque():
    assert "disciplinary leadership" in _matched(
        "Disciplinary leadership of a team of 5 product managers."
    )


def test_interdisciplinary_does_not_trip_the_leadership_pattern():
    """`interdisciplinary` contains `disciplinary` — must not match that rule.

    Since 2026-08-14 the word has its own finding (`cross-functional` is the
    idiomatic term for a team), but the substring safety still has to hold:
    it must never be reported as the leadership calque.
    """
    findings = check_idioms("Worked in an interdisciplinary Scrum team.")
    assert [f.matched.lower() for f in findings] == ["interdisciplinary"]
    assert all("leadership" not in f.suggestion for f in findings)


def test_detects_nominalisation_calques():
    text = (
        "Built internal app development from external vendors.\n"
        "Established them as decision basis for roadmap prioritization.\n"
        "Strategic further development of the platform.\n"
    )
    found = _matched(text)
    assert "built internal app development" in found
    assert "decision basis" in found
    assert "further development of" in found


def test_detects_wrong_preposition_and_redundant_qualifier():
    found = _matched("From product idea to successful exit at local-directory.example.")
    assert "exit at" in found
    assert "successful exit" in found


def test_detects_em_dash():
    assert "—" in _matched("Product owner — data-driven offerings.")
    assert check_idioms("Product owner – data-driven offerings.") == []


def test_repeated_phrase_collapses_to_one_finding():
    text = "decision basis here. And decision basis there."
    assert len([f for f in check_idioms(text) if f.matched.lower() == "decision basis"]) == 1


def test_finding_carries_suggestion_source_and_context():
    (finding,) = [f for f in check_idioms("We had decision basis for it.")
                  if f.matched.lower() == "decision basis"]
    assert finding.suggestion
    assert finding.source == "Entscheidungsgrundlage"
    assert "decision basis" in finding.context


def test_report_written_only_when_findings_exist(tmp_path):
    assert write_idiom_report(tmp_path, "I took the product from idea to exit to local-directory.example.") is None
    assert not (tmp_path / "_idiom.md").exists()

    path = write_idiom_report(tmp_path, "Disciplinary leadership of 5 product managers.")
    assert path is not None and path.name == "_idiom.md"
    assert "line-managed" in path.read_text(encoding="utf-8")


def test_format_findings_lists_every_finding():
    findings = check_idioms(
        "Disciplinary leadership of 5 PMs. Established as decision basis for planning."
    )
    report = format_findings(findings)
    for f in findings:
        assert f.matched in report


# ---------------------------------------------------------------------------
# Structural check: missing article before a singular countable noun.
#
# Added 2026-08-14 after measuring recall: none of the 17 curated phrase
# patterns fired on the 15 calques found by hand in the Rivia EN CV. Phrase
# lists only catch what someone already saw; this rule catches a class.
# ---------------------------------------------------------------------------


def test_missing_article_before_singular_noun_is_flagged():
    found = _matched(
        "Led billing feature from idea to rollout.\n"
        "Introduced cloud-first strategy, achieving a five-figure saving.\n"
        "Initiated AI working group at HealthApp.\n"
    )
    assert "led billing feature" in found
    assert "introduced cloud-first strategy" in found
    assert "initiated ai working group" in found


def test_article_present_is_not_flagged():
    assert check_missing_articles(
        "Took the billing feature from idea to rollout.\n"
        "Introduced a cloud-first strategy that cut costs.\n"
        "Set up HealthApp's internal AI working group.\n"
    ) == []


def test_plural_head_needs_no_article():
    assert check_missing_articles(
        "Gathered requirements from the sports and business desks.\n"
        "Developed and launched several digital products.\n"
    ) == []


def test_uncountable_head_needs_no_article():
    """Abstract nouns correctly take no article — the biggest FP class."""
    assert check_missing_articles(
        "Increased release stability across the platform.\n"
        "Enabled product measurement for the roadmap.\n"
        "Built specific expertise in regulated markets.\n"
        "Owned SEO for the whole portfolio.\n"
    ) == []


def test_proper_noun_head_needs_no_article():
    assert check_missing_articles(
        "Founded GastroSaaS, a mobile reservation system for restaurants.\n"
        "Restructured HealthAppConnect and improved release stability.\n"
    ) == []


def test_coordinated_list_needs_no_article():
    """`set up backlog, refinement, and release routines` — the list governs."""
    assert check_missing_articles(
        "Set up backlog, refinement, and release routines.\n"
        "Gathered market and customer needs.\n"
    ) == []


def test_clause_continuing_after_comma_still_counts():
    """A verb after the comma means a new clause, not a coordinated list."""
    found = [f.matched.lower() for f in check_missing_articles(
        "Restructured backlog, prioritized by technical and business criteria."
    )]
    assert "restructured backlog" in found


def test_adverb_ends_the_noun_phrase():
    assert check_missing_articles(
        "Delivered releases ahead of schedule.\n"
        "Initiated AI workflows independently.\n"
    ) == []


def test_headings_and_tables_are_skipped():
    assert check_missing_articles(
        "### 2015 | MediaHoldingCo - Head of Products\n"
        "| Led billing feature | x |\n"
        "**Created:** 2026-08-12T16:20:34\n"
    ) == []


def test_structural_findings_reach_check_idioms():
    """The structural rule is part of the single public entry point."""
    assert "led billing feature" in _matched("Led billing feature from idea.")
