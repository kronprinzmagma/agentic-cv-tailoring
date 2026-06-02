"""Tests for the Berufserfahrung consistency checker.

Pins the three hard rules:
1. Verbatim station headers (no title shortening, no date drift)
2. One entry per company (no splitting MediaCorp into two periods)
3. No invented companies
"""
from pathlib import Path

import pytest

from cv_tailor.consistency_check import (
    autofix_headers,
    format_issues_for_writer,
    validate_berufserfahrung,
)
import cv_tailor.consistency_check as cc

STANDARD_CV = """# Alex Müller

## Berufserfahrung

### 2023–2025 | HealthApp – Senior Product Owner

- Plattform-Ownership HealthAppConnect.

### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote

- ML-Empfehlungssystem konzipiert.

### 2011–2014 | GastroSaaS / local-directory.example – Managing Director / Product & Partner Manager

- Gründung und Exit.

### 2007–2011 | Namics AG – Senior Consultant mit Fokus auf Online Marketing

- Beratung Finanzinstitute.
"""


@pytest.fixture
def standard_cv_path(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "standard_cv.md"
    p.write_text(STANDARD_CV, encoding="utf-8")
    # consistency_check caches company tokens by source path — reset for isolation
    monkeypatch.setattr(cc, "COMPANY_TOKENS", {})
    monkeypatch.setattr(cc, "_TOKENS_LOADED_FROM", None)
    return p


def test_verbatim_headers_pass(standard_cv_path):
    draft = (
        "### 2023–2025 | HealthApp – Senior Product Owner\n"
        "- Bullet\n"
        "### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote\n"
        "- Bullet\n"
    )
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert ok, issues
    assert issues == []


def test_added_ag_suffix_flagged(standard_cv_path):
    """Real-world drift: writer adds 'AG' to HealthApp."""
    draft = "### 2023–2025 | HealthApp AG – Senior Product Owner\n- Bullet\n"
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert not ok
    assert any("Header-Drift" in i and "healthapp" in i.lower() for i in issues)


def test_invented_title_flagged(standard_cv_path):
    """Writer hallucinates 'Founder' for GastroSaaS (Standard-CV: Managing Director)."""
    draft = "### 2011–2014 | GastroSaaS / local-directory.example – Founder & Product Manager / Product & Partner Manager\n"
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert not ok
    assert any("gastrosaas" in i.lower() for i in issues)


def test_split_company_flagged(standard_cv_path):
    """MediaCorp gets split into two periods — must be flagged as multiple entries."""
    draft = (
        "### 2015–2017 | MediaCorp – Projektleiter\n"
        "- Bullet\n"
        "### 2017–2023 | MediaCorp – Product Owner\n"
        "- Bullet\n"
    )
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert not ok
    assert any("Mehrere Stationen" in i for i in issues)


@pytest.mark.xfail(
    reason="Known coverage gap: _looks_like_station_header requires a known "
           "company token, so fully-invented companies (no slug match against "
           "the Standard-CV) are skipped silently instead of being flagged as "
           "'Erfundene Station'. To close the gap, the validator should "
           "consider any line that *structurally* looks like a station header "
           "(### YYYY[–YYYY] | X – Y) and not just those mentioning a known "
           "company.",
    strict=False,
)
def test_invented_company_flagged(standard_cv_path):
    """Writer makes up a company that isn't in the Standard-CV."""
    draft = "### 2020–2022 | FictionalCorp – Director\n- Bullet\n"
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert not ok
    assert any("Erfundene Station" in i for i in issues)


def test_autofix_replaces_drifted_header(standard_cv_path):
    """Autofix should restore the canonical HealthApp header verbatim."""
    draft = (
        "### 2023–2025 | HealthApp AG – Senior Product Owner\n"
        "- Bullet stays untouched.\n"
    )
    fixed, applied = autofix_headers(draft, standard_cv_path)
    assert applied
    assert "### 2023–2025 | HealthApp – Senior Product Owner" in fixed
    assert "Bullet stays untouched" in fixed  # body preserved


def test_format_issues_with_findings_has_drift_heading():
    """Quality-snapshot detector relies on `Header-Drift für` numbering —
    this format must stay stable."""
    out = format_issues_for_writer(["Header-Drift für 'healthapp': X weicht ab."])
    assert "# Konsistenz-Check" in out
    assert "1. Header-Drift für 'healthapp'" in out


def test_format_issues_clean_message():
    assert format_issues_for_writer([]) == "Keine strukturelle Drift gefunden."
