"""Faktengrenzen: settled facts reach the prompts and are checked line by line.

Rules here are fictional on purpose — the real ones live in the private
data/fact_limits.yaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cv_tailor import fact_limits as fl

LIMITS_YAML = """
limits:
  - id: fuehrung_disziplinarisch
    fact: >
      Disziplinarisch geführt nur bei Acme.
    checks:
      - pattern: "\\\\bdisziplinar|\\\\bdisciplinar"
        only_in_station: "Acme"
        message: "«disziplinarisch» nur in der Acme-Station."
  - id: jahre
    fact: "Seit 2010 tätig: über 15 Jahre, nie 20."
    checks:
      - pattern: "\\\\b20\\\\s*(Jahre|years)"
        message: "Nie 20 Jahre."
  - id: po_jahre
    fact: "Sieben Jahre PO nur zusammen mit Beta."
    checks:
      - pattern: "sieben Jahre[^.]{0,40}Product Owner"
        unless: "Beta"
        message: "Sieben Jahre PO nur zusammen mit Beta."
  - id: nur_fakt
    fact: "Ein Fakt ohne Check."
    checks: []
"""


@pytest.fixture
def limits(tmp_path: Path) -> list[fl.FactLimit]:
    path = tmp_path / "fact_limits.yaml"
    path.write_text(LIMITS_YAML, encoding="utf-8")
    return fl.load_fact_limits(path)


def test_missing_file_is_a_no_op(tmp_path: Path) -> None:
    assert fl.load_fact_limits(tmp_path / "nope.yaml") == []
    assert fl.prompt_block(tmp_path / "nope.yaml") == ""


def test_prompt_block_lists_every_fact(limits) -> None:
    block = fl.format_for_prompt(limits)
    assert block.startswith(fl.PROMPT_TITLE)
    assert "- Ein Fakt ohne Check." in block
    assert "gehen dem Standard-CV-Wortlaut vor" in block


def test_station_scope_allows_the_word_only_in_its_station(limits) -> None:
    text = (
        "## Schlüsselkompetenzen\n"
        "**Führung** - 5 Personen bei Acme disziplinarisch geführt.\n"
        "## Berufserfahrung\n"
        "### 2015 | Acme AG – Head of Products\n"
        "- Disziplinarische Führung von 5 Produktmanager:innen.\n"
        "### 2016–2023 | Beta – Product Owner\n"
        "- Disziplinarisch verantwortlich für das Team.\n"
    )
    findings = fl.check_text(text, limits)
    assert [f.line_no for f in findings] == [2, 7]
    assert all(f.limit_id == "fuehrung_disziplinarisch" for f in findings)


def test_interdisziplinaer_is_not_disziplinarisch(limits) -> None:
    text = "- Interdisziplinäre Teams geführt.\n- Led an interdisciplinary Scrum team.\n"
    assert fl.check_text(text, limits) == []


def test_unless_and_bold_markers(limits) -> None:
    text = (
        "Über **20 Jahre** Erfahrung.\n"
        "Über sieben Jahre als Product Owner bei Alpha und Beta.\n"
        "Über sieben Jahre als Product Owner bei Alpha.\n"
    )
    ids = [(f.limit_id, f.line_no) for f in fl.check_text(text, limits)]
    assert ids == [("jahre", 1), ("po_jahre", 3)]


def test_headings_and_metadata_are_not_claims(limits) -> None:
    text = "# 20 Jahre Rückblick\n**Run:** 20 Jahre\n### 2005–2025 | Acme – 20 Jahre\n"
    assert fl.check_text(text, limits) == []


def test_writer_block_numbers_findings(limits) -> None:
    findings = fl.check_text("Über 20 Jahre Erfahrung.\n", limits)
    block = fl.format_findings_for_writer(findings)
    assert "1. Nie 20 Jahre." in block
    assert "«Über 20 Jahre Erfahrung.»" in block
    assert fl.format_findings_for_writer([]).startswith("Keine Faktengrenze")


def test_run_report_written_and_removed_when_clean(tmp_path: Path, limits) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "04_final_de.md").write_text("Über 20 Jahre Erfahrung.\n", encoding="utf-8")
    (run / "04_final_en.md").write_text("More than 15 years.\n", encoding="utf-8")
    report, count = fl.write_run_report(run, limits)
    assert count == 1 and report is not None
    text = report.read_text(encoding="utf-8")
    assert "## 04_final_de.md" in text and "## 04_final_en.md" in text

    (run / "04_final_de.md").write_text("Über 15 Jahre Erfahrung.\n", encoding="utf-8")
    assert fl.write_run_report(run, limits) == (None, 0)
    assert not (run / fl.REPORT_FILENAME).exists()


def test_writer_prompt_declares_precedence_over_standard_cv() -> None:
    for prompt in ("writer", "coach_reviewer", "factcheck", "analyst"):
        text = Path(f"prompts/{prompt}.md").read_text(encoding="utf-8")
        assert "Faktengrenzen" in text, prompt


def test_only_in_station_accepts_a_list(tmp_path: Path) -> None:
    path = tmp_path / "limits.yaml"
    path.write_text(
        'limits:\n  - id: gremien\n    fact: "Steering nur bei Gamma."\n    checks:\n'
        '      - pattern: "steering committee"\n'
        '        only_in_station: ["Gamma Rundfunk", "Gamma Broadcasting"]\n'
        '        message: "Nur bei Gamma."\n',
        encoding="utf-8",
    )
    limits = fl.load_fact_limits(path)
    text = (
        "### 2015–2023 | Gamma Broadcasting – PO\n- Reported to the steering committee.\n"
        "### 2023–2025 | Delta – PO\n- Reported to the steering committee.\n"
    )
    assert [f.line_no for f in fl.check_text(text, limits)] == [4]


STATIONS_YAML = """
limits: []
berufserfahrung_max_woerter: 40
stationen:
  - station: "Acme"
    max_woerter: 20
    min_pflicht: 2
    min_zeilen: 2
    pflicht:
      - id: launch
        text: "App zwei Wochen vor Termin lanciert"
        muster: "zwei Wochen"
      - id: team
        text: "Team von 8 Personen aufgebaut"
        muster: "Team"
      - id: cloud
        text: "Cloud-Strategie eingeführt"
        muster: "Cloud"
"""


def _station_rules(tmp_path: Path) -> fl.StationRules:
    path = tmp_path / "limits.yaml"
    path.write_text(STATIONS_YAML, encoding="utf-8")
    return fl.load_station_rules(path)


def test_station_rules_missing_substance_and_budget(tmp_path: Path) -> None:
    rules = _station_rules(tmp_path)
    ok = "### 2020–2023 | Acme – PO\n- App zwei Wochen vor Termin lanciert.\n- Team aufgebaut.\n"
    assert fl.check_station_rules(ok, rules) == []

    thin = "### 2020–2023 | Acme – PO\n- " + " ".join(["Wort"] * 25) + " Cloud.\n"
    findings = fl.check_station_rules(thin, rules)
    assert any("Budget 20" in f for f in findings)
    assert any("1 Bullets" in f for f in findings)
    assert any("1 von mindestens 2 Pflichtfakten" in f and "Team" in f for f in findings)


def test_station_rules_total_budget_and_prompt(tmp_path: Path) -> None:
    rules = _station_rules(tmp_path)
    text = "### 2020 | Other – X\n- " + " ".join(["Wort"] * 45) + "\n"
    assert any("gesamt 45 Wörter, Budget 40" in f for f in fl.check_station_rules(text, rules))
    prompt = fl.format_station_substance(rules)
    assert "Pflichtsubstanz pro Station" in prompt and "Acme: App zwei Wochen" in prompt
    notes = fl.skeleton_notes(rules, "### 2020–2023 | Acme – PO")
    assert notes[0].startswith("- Wortbudget: höchstens 20") and any("Cloud" in n for n in notes)
    assert fl.skeleton_notes(rules, "### 2018 | Unknown – X") == []


def test_station_rules_empty_without_file(tmp_path: Path) -> None:
    rules = fl.load_station_rules(tmp_path / "missing.yaml")
    assert rules.stations == () and fl.check_station_rules("### X\n- a\n", rules) == []
