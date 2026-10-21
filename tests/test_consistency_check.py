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
    autofix_station_layout,
    format_issues_for_writer,
    validate_berufserfahrung,
)
import cv_tailor.consistency_check as cc

STANDARD_CV = """# Alex Müller

## Berufserfahrung

### 2023–2025 | HealthApp – Senior Product Owner

- Plattform-Ownership HealthAppConnect.
- Release-Prozess stabilisiert.

### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote

- ML-Empfehlungssystem konzipiert.

### 2011–2014 | GastroSaaS / local-directory.example – Managing Director / Product & Partner Manager

- Gründung und Exit.

### 2007–2011 | Namics AG – Senior Consultant mit Fokus auf Online Marketing

- Beratung Finanzinstitute.
"""


@pytest.fixture
def standard_cv_path(tmp_path: Path, monkeypatch) -> Path:
    """Isolierter Standard-CV — hermetisch, ohne den echten data/standard_cv.md.

    Wichtig (2026-08-18): Die interne Helferkette in consistency_check ruft
    _company_tokens() teils OHNE Pfad auf und faellt dann auf den relativen
    Default Path("data/standard_cv.md") zurueck. Eine Datei in tmp_path allein
    reicht darum nicht — die Tests lasen bis hierher Alex' echten CV mit
    Personendaten und bestanden nur, weil dessen Firmen zufaellig zu STANDARD_CV
    passen. In CI (ohne die gitignorierte Datei) schlugen sie fehl.
    Loesung: ins tmp-Verzeichnis wechseln und den synthetischen CV genau unter
    dem Default-Pfad ablegen, damit beide Zugriffswege dieselbe Datei sehen.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    p = data_dir / "standard_cv.md"
    p.write_text(STANDARD_CV, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # consistency_check caches company tokens by source path — reset for isolation
    monkeypatch.setattr(cc, "COMPANY_TOKENS", {})
    monkeypatch.setattr(cc, "_TOKENS_LOADED_FROM", None)
    return Path("data/standard_cv.md")


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


def test_invented_company_flagged(standard_cv_path):
    """Writer makes up a company that isn't in the Standard-CV.

    Closed coverage gap (vormals xfail): station-shaped `###`-headings
    without any known company token are now flagged as 'Erfundene Station'
    instead of being silently skipped.
    """
    draft = "### 2020–2022 | FictionalCorp – Director\n- Bullet\n"
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert not ok
    assert any("Erfundene Station" in i for i in issues)


def test_invented_company_among_valid_stations(standard_cv_path):
    """An invented station between two verbatim stations is still caught."""
    draft = (
        "### 2023–2025 | HealthApp – Senior Product Owner\n"
        "- Bullet\n"
        "### 2018–2020 | GhostStartup GmbH – Head of Product\n"
        "- Bullet\n"
        "### 2011–2014 | GastroSaaS / local-directory.example – Managing Director / Product & Partner Manager\n"
        "- Bullet\n"
    )
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert not ok
    assert any("Erfundene Station" in i and "GhostStartup" in i for i in issues)


def test_body_lines_with_year_and_pipe_not_flagged(standard_cv_path):
    """Precision guard: bullets and bold text with year+pipe are not stations."""
    draft = (
        "### 2023–2025 | HealthApp – Senior Product Owner\n"
        "- Projekt Alpha | Rollout 2024 abgeschlossen\n"
        "**Highlight | 2024:** Plattform-Launch\n"
        "## Weiterbildung | 2020\n"
    )
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert ok, issues


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


# --- Rules 5 + 6: station order and bare headers (iWay run, 2026-09-18) ---

def test_station_order_drift_flagged(standard_cv_path):
    """Appending a caught-up station after the oldest one is a finding."""
    draft = (
        "### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote\n"
        "- MediaCorp bullet.\n\n---\n\n"
        "### 2007–2011 | Namics AG – Senior Consultant mit Fokus auf Online Marketing\n"
        "- Namics bullet.\n\n---\n\n"
        "### 2023–2025 | HealthApp – Senior Product Owner\n"
        "- HealthApp bullet.\n"
    )
    ok, issues = validate_berufserfahrung(draft, standard_cv_path, check_completeness=True)
    assert not ok
    assert any("Reihenfolge der Stationen" in i for i in issues)


def test_station_order_not_checked_on_fragments(standard_cv_path):
    """Without check_completeness a partial draft in any order stays valid."""
    draft = (
        "### 2007–2011 | Namics AG – Senior Consultant mit Fokus auf Online Marketing\n"
        "- x\n\n"
        "### 2023–2025 | HealthApp – Senior Product Owner\n"
        "- y\n"
    )
    ok, issues = validate_berufserfahrung(draft, standard_cv_path)
    assert ok, issues


def test_bare_station_header_flagged(standard_cv_path):
    """A station the Standard-CV fills with bullets may not stay a bare header."""
    draft = (
        "### 2023–2025 | HealthApp – Senior Product Owner\n\n---\n\n"
        "### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote\n"
        "- MediaCorp bullet.\n"
    )
    ok, issues = validate_berufserfahrung(draft, standard_cv_path, check_completeness=True)
    assert any("Station ohne Inhalt" in i and "HealthApp" in i for i in issues)


def test_autofix_layout_reorders_and_fills(standard_cv_path):
    draft = (
        "### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote\n\n"
        "- MediaCorp bullet.\n\n---\n\n"
        "### 2007–2011 | Namics AG – Senior Consultant mit Fokus auf Online Marketing\n\n"
        "- Namics bullet.\n\n---\n\n"
        "### 2023–2025 | HealthApp – Senior Product Owner\n"
    )
    fixed, fixes = autofix_station_layout(draft, standard_cv_path)
    assert len(fixes) == 2
    headers = [l for l in fixed.splitlines() if l.startswith("### ")]
    assert [h.split("|")[0].strip() for h in headers] == ["### 2023–2025", "### 2015–2023", "### 2007–2011"]
    # Bare HealthApp station got the first Standard-CV bullet verbatim, nothing invented
    assert "- Plattform-Ownership HealthAppConnect." in fixed
    assert "- MediaCorp bullet." in fixed and "- Namics bullet." in fixed
    ok, issues = validate_berufserfahrung(fixed, standard_cv_path, check_completeness=False)
    assert ok, issues


def test_autofix_layout_noop_when_clean(standard_cv_path):
    draft = (
        "### 2023–2025 | HealthApp – Senior Product Owner\n\n- a\n\n---\n\n"
        "### 2015–2023 | MediaCorp – Product Owner Datenbasierte Angebote\n\n- b\n"
    )
    fixed, fixes = autofix_station_layout(draft, standard_cv_path)
    assert fixes == []
    assert fixed == draft


# --- Rule 7: anchor link of the first Standard-CV bullet -------------------

ANCHOR_CV = """# Alex Müller

## Berufserfahrung

### 2026–heute | Eigenregie – KI-Produktentwicklung

- Sechs KI-Anwendungen verantwortet, alle öffentlich auf github.com/example-user.
- Zweiter Bullet.

### 2023–2025 | HealthApp – Senior Product Owner

- Plattform-Ownership HealthAppConnect.
- Release-Prozess stabilisiert.
"""


@pytest.fixture
def anchor_cv_path(tmp_path):
    p = tmp_path / "standard_cv.md"
    p.write_text(ANCHOR_CV, encoding="utf-8")
    cc._set_company_tokens({})
    return p


EIGEN = "### 2026–heute | Eigenregie – KI-Produktentwicklung\n\n"
BLUE = "### 2023–2025 | HealthApp – Senior Product Owner\n\n- Plattform-Ownership.\n"


def test_thin_station_without_link_is_flagged(anchor_cv_path):
    draft = EIGEN + "Konzeption und Entwicklung eigener KI-gestützter Tools.\n\n---\n\n" + BLUE
    ok, issues = validate_berufserfahrung(draft, anchor_cv_path, check_completeness=True)
    assert any("Station ohne Anker" in i and "github.com/example-user" in i for i in issues)


def test_station_with_link_passes_even_with_bold_and_scheme(anchor_cv_path):
    draft = (
        EIGEN
        + "Sechs KI-Anwendungen verantwortet, alle öffentlich auf **https://www.github.com/example-user**.\n\n---\n\n"
        + BLUE
    )
    ok, issues = validate_berufserfahrung(draft, anchor_cv_path, check_completeness=True)
    assert not any("Anker" in i for i in issues), issues


def test_anchor_rule_is_opt_in(anchor_cv_path):
    draft = EIGEN + "Dünne Fassung.\n"
    ok, issues = validate_berufserfahrung(draft, anchor_cv_path, check_completeness=False)
    assert not any("Anker" in i for i in issues)


def test_stations_without_link_in_standard_cv_are_not_anchored(anchor_cv_path):
    draft = EIGEN + "Sechs KI-Anwendungen verantwortet, alle öffentlich auf github.com/example-user.\n\n---\n\n" + "### 2023–2025 | HealthApp – Senior Product Owner\n\n- Etwas anderes.\n"
    ok, issues = validate_berufserfahrung(draft, anchor_cv_path, check_completeness=True)
    assert not any("Anker" in i for i in issues), issues


def test_autofix_replaces_single_thin_line_with_anchor_bullet(anchor_cv_path):
    draft = EIGEN + "Konzeption und Entwicklung eigener KI-gestützter Tools.\n\n---\n\n" + BLUE
    fixed, fixes = autofix_station_layout(draft, anchor_cv_path)
    assert any("ersetzt" in f for f in fixes)
    assert "eigener KI-gestützter Tools" not in fixed
    # same paragraph style as the draft: no leading dash invented
    assert "\nSechs KI-Anwendungen verantwortet, alle öffentlich auf github.com/example-user." in fixed
    ok, issues = validate_berufserfahrung(fixed, anchor_cv_path, check_completeness=True)
    assert ok, issues


def test_autofix_prepends_anchor_bullet_to_longer_body_and_keeps_dash_style(anchor_cv_path):
    draft = EIGEN + "- Erster freier Bullet.\n- Zweiter freier Bullet.\n\n---\n\n" + BLUE
    fixed, fixes = autofix_station_layout(draft, anchor_cv_path)
    assert any("vorangestellt" in f for f in fixes)
    lines = [l for l in fixed.splitlines() if l.strip()]
    i = lines.index("### 2026–heute | Eigenregie – KI-Produktentwicklung")
    assert lines[i + 1].startswith("- Sechs KI-Anwendungen")
    assert "- Erster freier Bullet." in fixed and "- Zweiter freier Bullet." in fixed


def test_autofix_noop_when_anchor_present(anchor_cv_path):
    draft = EIGEN + "- Sechs KI-Anwendungen verantwortet, alle öffentlich auf github.com/example-user.\n\n---\n\n" + BLUE
    fixed, fixes = autofix_station_layout(draft, anchor_cv_path)
    assert fixes == [] and fixed == draft


def test_link_kept_but_bullet_rewritten_is_flagged(anchor_cv_path):
    """Swiss Olympic regression: correct link, invented numbers in the same bullet."""
    draft = EIGEN + "Eigene KI-Tools produktiv betrieben – 3 Projekte, davon 2 öffentlich: github.com/example-user\n\n---\n\n" + BLUE
    ok, issues = validate_berufserfahrung(draft, anchor_cv_path, check_completeness=True)
    assert any("Anker-Bullet verändert" in i for i in issues)


def test_autofix_swaps_rewritten_anchor_line_and_keeps_others(anchor_cv_path):
    draft = (EIGEN + "- Eigene KI-Tools – 3 Projekte, davon 2 öffentlich: github.com/example-user\n"
             "- Zweiter freier Bullet.\n\n---\n\n" + BLUE)
    fixed, fixes = autofix_station_layout(draft, anchor_cv_path)
    assert any("umgeschriebenen Anker-Bullet" in f for f in fixes)
    assert "3 Projekte" not in fixed
    assert "- Sechs KI-Anwendungen verantwortet, alle öffentlich auf github.com/example-user." in fixed
    assert "- Zweiter freier Bullet." in fixed
    ok, issues = validate_berufserfahrung(fixed, anchor_cv_path, check_completeness=True)
    assert ok, issues
