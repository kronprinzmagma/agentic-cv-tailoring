"""Tests for the deterministic cross-section redundancy check.

Pins the detector that catches verbatim flagship-claim repetition between
Management Summary and the other CV sections (found in 4 of 5 runs
reviewed on 2026-06-12).
"""
from cv_tailor.redundancy_check import (
    check_cross_section_redundancy,
    format_findings,
    write_redundancy_report,
)

REPEATED = (
    "Die neue MediaCorp App lancierte ich zwei Wochen vor Termin und 20% unter Budget."
)


def _cv(summary_extra: str = "", erfahrung_extra: str = "") -> str:
    return (
        "# Finaler CV (DE)\n\n"
        "## Management Summary\n\n"
        f"Ich führe digitale Produkte seit 20 Jahren. {summary_extra}\n\n"
        "---\n\n"
        "## Schlüsselkompetenzen\n\n"
        "**Produktverantwortung** - Roadmap und Backlog.\n\n"
        "---\n\n"
        "## Berufserfahrung\n\n"
        "### 2015–2023 | MediaCorp – Product Owner\n\n"
        f"Aufbau der internen App-Entwicklung. {erfahrung_extra}\n"
    )


def test_clean_cv_has_no_findings():
    assert check_cross_section_redundancy(_cv()) == []


def test_verbatim_repetition_detected():
    cv = _cv(summary_extra=REPEATED, erfahrung_extra=REPEATED)
    findings = check_cross_section_redundancy(cv)
    assert len(findings) == 1
    assert findings[0].section == "Berufserfahrung"
    assert "zwei wochen vor termin" in findings[0].phrase


def test_repetition_survives_bold_markers_and_case():
    """Keyword-marker bolds and case changes must not hide the repetition."""
    cv = _cv(
        summary_extra=REPEATED,
        erfahrung_extra="die neue MediaCorp App lancierte ich **zwei Wochen vor Termin** und 20% unter Budget.",
    )
    assert len(check_cross_section_redundancy(cv)) == 1


def test_short_overlap_does_not_fire():
    """Common short phrases (< 8 words) are not redundancy."""
    cv = _cv(
        summary_extra="Führung eines interdisziplinären Teams bei HealthApp.",
        erfahrung_extra="Führung eines interdisziplinären Teams mit Fokus auf Kommunikation.",
    )
    assert check_cross_section_redundancy(cv) == []


def test_missing_summary_section_returns_empty():
    assert check_cross_section_redundancy("## Berufserfahrung\n\nText ohne Summary.") == []


def test_format_findings_readable():
    cv = _cv(summary_extra=REPEATED, erfahrung_extra=REPEATED)
    out = format_findings(check_cross_section_redundancy(cv))
    assert out.startswith("# Redundanz-Hinweise")
    assert "Berufserfahrung" in out


def test_write_report_only_when_findings(tmp_path):
    assert write_redundancy_report(tmp_path, _cv()) is None
    assert not (tmp_path / "_redundancy.md").exists()

    cv = _cv(summary_extra=REPEATED, erfahrung_extra=REPEATED)
    path = write_redundancy_report(tmp_path, cv)
    assert path is not None and path.exists()
    assert "Redundanz" in path.read_text(encoding="utf-8")
