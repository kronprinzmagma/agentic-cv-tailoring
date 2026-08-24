"""Tests for the deterministic final-artifact validation (Trim + Translation).

These guards close the chain AFTER the factcheck: trim and translation are
the last content transformations and previously ran unvalidated.
"""
from pathlib import Path

import pytest

import cv_tailor.consistency_check as cc
from cv_tailor.final_check import (
    count_bold_runs,
    extract_number_runs,
    extract_station_companies,
    format_translation_report,
    validate_translation,
    validate_trimmed_cv,
)

STANDARD_CV = """# Alex Müller

## Berufserfahrung

### 2023–2025 | HealthApp – Senior Product Owner

- Plattform-Ownership HealthAppConnect.

### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote

- ML-Empfehlungssystem konzipiert.
"""

CV_DE = """# Finaler CV (DE)

**Run:** 2026-08-12_20260812_143900_stellenanzeige_test
**Erstellt:** 2026-08-12T14:56:15.106949+00:00

## Management Summary

Ich habe über 10 Personen fachlich geführt und 20% unter Budget geliefert.

## Schlüsselkompetenzen

**Produktführung** — Roadmaps und Discovery.

## Berufserfahrung

### 2023–2025 | HealthApp – Senior Product Owner

- Plattform mit 3 Nutzergruppen ausgebaut.

### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote

- Neue MediaCorp App zwei Wochen vor Termin geliefert.
"""

CV_EN = """# Final CV (EN)

**Run:** 2026-08-12_20260812_143900_job_ad_test
**Created:** 2026-08-12T16:20:34.446428+00:00

## Management Summary

I led a team of more than 10 people and delivered 20% under budget.

## Key Competencies

**Product leadership** — roadmaps and discovery.

## Professional Experience

### 2023–2025 | HealthApp – Senior Product Owner

- Grew the platform to 3 user groups.

### 2015–2023 | MediaCorp – Product Owner Data-Driven Offerings

- Delivered the new MediaCorp App two weeks ahead of schedule.
"""


@pytest.fixture(autouse=True)
def _company_tokens(monkeypatch, tmp_path: Path):
    """Point the consistency-check token cache at a test Standard-CV.

    Siehe die ausfuehrliche Begruendung in test_consistency_check.py: der
    synthetische CV muss unter dem relativen Default-Pfad liegen, sonst
    faellt die interne Helferkette auf den echten data/standard_cv.md zurueck.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    p = data_dir / "standard_cv.md"
    p.write_text(STANDARD_CV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc, "COMPANY_TOKENS", {})
    monkeypatch.setattr(cc, "_TOKENS_LOADED_FROM", None)
    cc._company_tokens(p)
    yield
    monkeypatch.setattr(cc, "COMPANY_TOKENS", {})
    monkeypatch.setattr(cc, "_TOKENS_LOADED_FROM", None)


# ---------------------------------------------------------------------------
# extract helpers
# ---------------------------------------------------------------------------

def test_extract_station_companies():
    # Substitution-safe (public sync anonymises company names): compare
    # count + DE↔EN equality instead of literal slug spellings.
    stations = extract_station_companies(CV_DE)
    assert len(stations) == 2
    assert "healthapp" in stations
    assert stations == extract_station_companies(CV_EN)


def test_extract_number_runs_ignores_metadata_lines():
    runs = extract_number_runs(CV_DE)
    assert "106949" not in runs  # timestamp from **Erstellt:** line
    assert "20260812" not in runs  # run id from **Run:** line
    assert runs["10"] == 1 and runs["20"] == 1


def test_number_runs_language_invariant():
    assert extract_number_runs("10'000 CHF, 3,5 Mio.") == extract_number_runs(
        "10,000 CHF, 3.5 million"
    )


# ---------------------------------------------------------------------------
# validate_trimmed_cv
# ---------------------------------------------------------------------------

def test_trim_accepts_moderate_cut():
    trimmed = CV_DE.replace("- Plattform mit 3 Nutzergruppen ausgebaut.\n", "")
    ok, issues = validate_trimmed_cv(CV_DE, trimmed)
    assert ok, issues


def test_trim_rejects_empty_result():
    ok, issues = validate_trimmed_cv(CV_DE, "   \n")
    assert not ok
    assert any("leer" in i for i in issues)


def test_trim_rejects_half_destroyed_cv():
    ok, issues = validate_trimmed_cv(CV_DE, CV_DE[: len(CV_DE) // 4])
    assert not ok


def test_trim_rejects_dropped_section():
    trimmed = CV_DE.replace("## Schlüsselkompetenzen", "## Sonstiges")
    ok, issues = validate_trimmed_cv(CV_DE, trimmed)
    assert not ok
    assert any("Schlüsselkompetenzen" in i for i in issues)


def test_trim_rejects_dropped_station():
    lines = CV_DE.splitlines()
    cut = "\n".join(line for line in lines if "HealthApp" not in line)
    ok, issues = validate_trimmed_cv(CV_DE, cut)
    assert not ok
    assert any("healthapp" in i and "fehlt nach dem Trim" in i for i in issues)


# ---------------------------------------------------------------------------
# validate_translation
# ---------------------------------------------------------------------------

def test_translation_clean_pair_passes():
    check = validate_translation(CV_DE, CV_EN)
    assert check.ok
    assert check.warnings == []


def test_translation_empty_is_fatal():
    check = validate_translation(CV_DE, "")
    assert not check.ok
    assert any("leer" in f for f in check.fatal)


def test_translation_truncated_is_fatal():
    check = validate_translation(CV_DE, CV_EN[: len(CV_EN) // 4])
    assert not check.ok


def test_translation_dropped_station_is_fatal():
    en = "\n".join(line for line in CV_EN.splitlines() if "HealthApp" not in line)
    check = validate_translation(CV_DE, en)
    assert not check.ok
    assert any("healthapp" in f for f in check.fatal)


def test_translation_number_drift_is_warning_not_fatal():
    en = CV_EN.replace("more than 10 people", "more than 12 people")
    check = validate_translation(CV_DE, en)
    assert check.ok  # numbers drift → warning, not fatal
    assert any("'12'" in w for w in check.warnings)
    assert any("'10'" in w for w in check.warnings)


def test_translation_spelled_out_numbers_cancel():
    # "zwei Wochen" ↔ "two weeks": no digit runs on either side → no warning
    check = validate_translation(CV_DE, CV_EN)
    assert not any("'2'" in w for w in check.warnings)


# ---------------------------------------------------------------------------
# report formatting
# ---------------------------------------------------------------------------

def test_report_marks_fatal_as_verworfen():
    check = validate_translation(CV_DE, "")
    report = format_translation_report(check)
    assert "VERWORFEN" in report
    assert "04_final_en.md wurde nicht geschrieben" in report


def test_report_warnings_only_has_no_verworfen():
    en = CV_EN.replace("more than 10 people", "more than 12 people")
    check = validate_translation(CV_DE, en)
    report = format_translation_report(check)
    assert "VERWORFEN" not in report
    assert "Abgleich DE ↔ EN" in report


# ---------------------------------------------------------------------------
# bold preservation (keyword marker survives the translator)
# ---------------------------------------------------------------------------

def test_count_bold_runs_ignores_metadata_preamble():
    text = "**Run:** x\n**Erstellt:** y\n\n## Management Summary\n\n**Alpha** und **Beta**.\n"
    assert count_bold_runs(text) == 2


def test_count_bold_runs_ignores_markers_spanning_a_newline():
    assert count_bold_runs("**offen\nzu**") == 0


def test_total_bold_loss_is_warned_not_fatal():
    """2026-08-24 archlet: 18 Marker im DE-CV, 0 im EN-CV."""
    de = CV_DE + "\n" + " ".join(f"**t{i}**" for i in range(10))
    check = validate_translation(de, CV_EN)
    assert check.ok, "Bold-Verlust darf das EN-Artefakt nie verwerfen"
    assert any("Fettmarkierungen" in w for w in check.warnings)


def test_preserved_bolds_produce_no_warning():
    bolds = "\n" + " ".join(f"**t{i}**" for i in range(10))
    check = validate_translation(CV_DE + bolds, CV_EN + bolds)
    assert not any("Fettmarkierungen" in w for w in check.warnings)


def test_small_bold_loss_stays_under_threshold():
    # 10 markers DE, 8 EN → 20% loss, below MAX_BOLD_LOSS_RATIO
    de = CV_DE + "\n" + " ".join(f"**t{i}**" for i in range(10))
    en = CV_EN + "\n" + " ".join(f"**t{i}**" for i in range(8))
    check = validate_translation(de, en)
    assert not any("Fettmarkierungen" in w for w in check.warnings)


def test_no_bolds_in_de_produces_no_division_error():
    check = validate_translation(CV_DE, CV_EN)
    assert not any("Fettmarkierungen" in w for w in check.warnings)
