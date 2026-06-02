"""Tests for profile-fit gap parsing + severity classification."""
from pathlib import Path

from cv_tailor.profile_fit import (
    check_profile_fit,
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

### 5. Gap vs. Framing
Some other section.
"""


def test_parse_extracts_all_data_rows(tmp_path):
    rows = parse_anforderungsabgleich(ANALYSE_TEMPLATE)
    statuses = [r[2] for r in rows]
    assert "STARK" in statuses
    assert "LÜCKE" in statuses
    assert len(rows) == 5
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
