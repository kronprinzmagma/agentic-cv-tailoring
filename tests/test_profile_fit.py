"""Tests for profile-fit gap parsing + severity classification."""
from pathlib import Path

from cv_tailor.profile_fit import (
    FitGap,
    check_profile_fit,
    format_gaps_as_questions,
    format_gaps_for_cli,
    has_critical_gaps,
    parse_anforderungsabgleich,
)


ANALYSE_TEMPLATE = """# Analyse

### 4. Anforderungsabgleich

| Anforderung | Beleg-ID(s) | Status | Kommentar |
|---|---|---|---|
| 5+ Jahre PM-Erfahrung | BELG-007 | STARK | 20 Jahre belegt |
| Strong execution skills | BELG-021 | SCHWACH | implizit |
| AI-Governance / Audit-Readiness | — | LÜCKE | Keine direkte Erfahrung |
| B2B SaaS | BELG-030 | MITTEL | GastroSaaS war SaaS |
| Italienisch | — | LÜCKE | Nicht vorhanden — Nice to have |
| Netzwerk im Gesundheitswesen (von Vorteil) | — | LÜCKE | Kein Hinweis auf aktives Netzwerk |

### 5. Gap vs. Framing
Some other section.
"""


def test_parse_extracts_all_data_rows(tmp_path):
    rows = parse_anforderungsabgleich(ANALYSE_TEMPLATE)
    statuses = [r[2] for r in rows]
    assert "STARK" in statuses
    assert "LÜCKE" in statuses
    assert len(rows) == 6
    # Separator row must NOT appear as data
    assert not any(r[0].startswith("--") for r in rows)


def test_parse_no_anforderungsabgleich_returns_empty():
    text = "# Analyse\n\n### 1. Etwas anderes\nKein Anforderungsabgleich hier."
    assert parse_anforderungsabgleich(text) == []


def test_check_classifies_critical_lücke(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    severities = {g.severity for g in gaps}
    requirements = {g.requirement for g in gaps}
    assert "critical" in severities  # AI-Governance has no nice-to-have marker
    assert "AI-Governance / Audit-Readiness" in requirements


def test_check_downgrades_nice_to_have_to_soft(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    nice = next(g for g in gaps if g.requirement == "Italienisch")
    assert nice.severity == "soft"


def test_von_vorteil_in_requirement_text_downgrades(tmp_path: Path):
    """Pharmasuisse 2026-06-11: '(von Vorteil)' stand im Anforderungstext,
    nicht im Kommentar — wurde fälschlich als kritisch geflaggt."""
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    netzwerk = next(g for g in gaps if g.requirement.startswith("Netzwerk"))
    assert netzwerk.severity == "soft"


def test_check_picks_up_schwach_with_strength_qualifier(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    gaps = check_profile_fit(p)
    # "Strong execution skills" — Strength qualifier + SCHWACH = soft gap
    strong_gaps = [g for g in gaps if "execution" in g.requirement.lower()]
    assert strong_gaps
    assert strong_gaps[0].severity == "soft"


def test_has_critical_gaps_short_circuit(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    assert has_critical_gaps(check_profile_fit(p))


def test_format_gaps_cli_human_readable(tmp_path: Path):
    p = tmp_path / "01_analyse.md"
    p.write_text(ANALYSE_TEMPLATE, encoding="utf-8")
    out = format_gaps_for_cli(check_profile_fit(p))
    assert "Kritisch" in out or "kritisch" in out.lower()
    assert "AI-Governance" in out


# ── Gap-to-Question-Routing ─────────────────────────────────────────────────

GOVERNANCE_GAP = FitGap(
    requirement="Tiefes Verständnis von IT-Governance, Security und Compliance",
    status="LÜCKE",
    comment="Nicht belegt. HealthApp hat regulatorisches Umfeld, aber keine explizite Governance-Verantwortung.",
    severity="critical",
)
NETZWERK_GAP = FitGap(
    requirement="Netzwerk im Gesundheitswesen (von Vorteil)",
    status="LÜCKE",
    comment="Kein Hinweis auf aktives Netzwerk.",
    severity="soft",
)


def test_questions_block_contains_requirement_and_instruction():
    out = format_gaps_as_questions([GOVERNANCE_GAP])
    assert "## Fragen aus dem Profil-Fit-Abgleich" in out
    assert "IT-Governance" in out
    assert "habe ich nicht" in out  # explicit opt-out instruction
    assert "Muss-Anforderung" in out


def test_questions_critical_sorted_first():
    out = format_gaps_as_questions([NETZWERK_GAP, GOVERNANCE_GAP])
    assert out.index("IT-Governance") < out.index("Netzwerk im Gesundheitswesen")


def test_questions_capped():
    gaps = [
        FitGap(f"Anforderung Nummer {i} mit speziellen Details", "LÜCKE", "", "critical")
        for i in range(10)
    ]
    out = format_gaps_as_questions(gaps, max_questions=4)
    assert out.count("Frage: Hast du belegbare Erfahrung") == 4


def test_already_answered_gap_suppressed():
    """Two distinctive requirement words in the stored Q/A corpus → skip."""
    answered = (
        "Frage: Erfahrung mit IT-Governance?\n"
        "Antwort: Bei HealthApp habe ich Compliance-Anforderungen im eHealth-Umfeld umgesetzt."
    )
    out = format_gaps_as_questions([GOVERNANCE_GAP], answered_context=answered)
    assert out == ""


def test_unrelated_answers_do_not_suppress():
    answered = "Frage: Italienisch? Antwort: Nein, nur Grundkenntnisse aus den Ferien."
    out = format_gaps_as_questions([GOVERNANCE_GAP], answered_context=answered)
    assert "IT-Governance" in out
