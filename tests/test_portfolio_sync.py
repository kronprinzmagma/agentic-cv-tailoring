"""Tests for the portfolio source: section gating, additive IDs, token budget.

The portfolio is a living document outside this repo. Three properties matter
and are easy to break silently:
  1. only evidence sections are indexed (a changelog line is not a Beleg),
  2. existing BELG-IDs survive a sync (clarifications reference them),
  3. portfolio entries stay out of the compact index (token budget).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cv_tailor.beleg_index import (
    format_beleg_index_compact,
    parse_markdown_source,
    parse_portfolio,
)
from cv_tailor.experience_activation import activate_entries
from cv_tailor.portfolio_sync import _max_id

PORTFOLIO_SAMPLE = """# Alex Müller — KI-Erfahrungslevel

**Letzte Aktualisierung:** 2026-09-07 (ein sehr langer Meta-Absatz über die
Pflege dieses Dokuments, der niemals ein Beleg werden darf.)

## Formale Weiterbildung

- Claude Code in Action — Anthropic / Coursera, abgeschlossen 2026.

## Eigene Projekte

### steuer-extraktor — Production-grade

- Deterministische Layout-Extraktoren mit arithmetischer Gegenprobe.

## Wachstumskanten

- RAG-Implementation — keine eigene Umsetzung.

## Update-Konvention

- Datum oben anpassen, Changelog-Eintrag am Ende.

## Changelog

- **2026-09-07** — steuer-extraktor: Quick Task aufgenommen.
"""


@pytest.fixture
def portfolio_file(tmp_path: Path) -> Path:
    path = tmp_path / "PORTFOLIO.md"
    path.write_text(PORTFOLIO_SAMPLE, encoding="utf-8")
    return path


def test_only_evidence_sections_are_indexed(portfolio_file: Path) -> None:
    snippets = [c.snippet for c in parse_portfolio(portfolio_file)]
    joined = " ".join(snippets)
    assert "Claude Code in Action" in joined
    assert "Layout-Extraktoren" in joined
    assert "keine eigene Umsetzung" in joined
    # Meta sections must not leak in.
    assert "Changelog-Eintrag" not in joined
    assert "Quick Task aufgenommen" not in joined
    assert "Meta-Absatz" not in joined


def test_all_claims_tagged_portfolio(portfolio_file: Path) -> None:
    claims = parse_portfolio(portfolio_file)
    assert claims
    assert {c.quelle_typ for c in claims} == {"portfolio"}


def test_growth_edges_are_indexed_as_evidence(portfolio_file: Path) -> None:
    """Wachstumskanten are honesty limits — they must reach the pipeline."""
    snippets = [c.snippet for c in parse_portfolio(portfolio_file)]
    assert any("RAG" in s for s in snippets)


def test_missing_portfolio_returns_empty(tmp_path: Path) -> None:
    assert parse_portfolio(tmp_path / "nope.md") == []


def test_parse_markdown_source_without_filter_takes_everything(
    portfolio_file: Path,
) -> None:
    claims = parse_markdown_source(portfolio_file, source_typ="portfolio")
    assert any("Changelog-Eintrag" in c.snippet for c in claims)


def test_max_id_continues_after_highest(tmp_path: Path) -> None:
    entries = [{"id": "BELG-001"}, {"id": "BELG-530"}, {"id": "kaputt"}]
    assert _max_id(entries) == 530
    assert _max_id([]) == 0


def test_compact_excludes_portfolio_by_default() -> None:
    index = {
        "entries": [
            {
                "id": "BELG-001",
                "typ": "skill",
                "snippet": "Aus dem Standard-CV",
                "quelle_typ": "standard_cv",
                "quelle_position": "line:1",
            },
            {
                "id": "BELG-999",
                "typ": "skill",
                "snippet": "Aus dem Portfolio",
                "quelle_typ": "portfolio",
                "quelle_position": "line:9",
            },
        ]
    }
    default = format_beleg_index_compact(index)
    assert "Aus dem Standard-CV" in default
    assert "Aus dem Portfolio" not in default

    opted_in = format_beleg_index_compact(index, include_portfolio=True)
    assert "Aus dem Portfolio" in opted_in


def test_activation_can_split_by_provenance() -> None:
    index = {
        "entries": [
            {
                "id": "BELG-001",
                "typ": "skill",
                "snippet": "Backlog priorisiert und Roadmap verantwortet",
                "quelle_typ": "standard_cv",
                "quelle_position": "line:1",
                "quelle_datei": "data/standard_cv.md",
            },
            {
                "id": "BELG-999",
                "typ": "skill",
                "snippet": "Backlog priorisiert im Eigenprojekt",
                "quelle_typ": "portfolio",
                "quelle_position": "line:9",
                "quelle_datei": "PORTFOLIO.md",
            },
        ]
    }
    job = "Wir suchen jemanden für Backlog, Roadmap und Priorisierung im Produktmanagement."

    without = activate_entries(job, index, exclude_quelle_typ="portfolio")
    only = activate_entries(job, index, quelle_typ="portfolio")

    ids_without = {e.id for hits in without.values() for e in hits}
    ids_only = {e.id for hits in only.values() for e in hits}
    assert "BELG-999" not in ids_without
    assert ids_only <= {"BELG-999"}


def test_sync_is_idempotent_on_ids(tmp_path: Path, monkeypatch) -> None:
    """A second sync replaces portfolio entries instead of appending duplicates."""
    from cv_tailor import portfolio_sync as ps

    index_path = tmp_path / "beleg_index.json"
    index_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "BELG-001",
                        "behauptung": "bestehend",
                        "snippet": "bestehend",
                        "quelle_typ": "standard_cv",
                        "quelle_datei": "data/standard_cv.md",
                        "quelle_position": "line:1",
                        "kontext": "",
                        "typ": "skill",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    portfolio = tmp_path / "PORTFOLIO.md"
    portfolio.write_text(PORTFOLIO_SAMPLE, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "agents:\n  factcheck: { provider: anthropic, model: claude-haiku-4-5 }\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ps, "_classify_all", lambda claims: [("skill", c.snippet) for c in claims])

    first = ps.sync_portfolio(index_path, portfolio, config)
    second = ps.sync_portfolio(index_path, portfolio, config)

    assert first["added"] == second["added"]
    assert first["first_id"] == second["first_id"] == "BELG-002"
    assert second["removed"] == first["added"]

    entries = json.loads(index_path.read_text(encoding="utf-8"))["entries"]
    assert entries[0]["id"] == "BELG-001"
    assert entries[0]["behauptung"] == "bestehend"
    assert len({e["id"] for e in entries}) == len(entries)


def test_sync_without_configured_portfolio_raises(tmp_path: Path) -> None:
    from cv_tailor import portfolio_sync as ps

    index_path = tmp_path / "beleg_index.json"
    index_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("agents: {}\n", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        ps.sync_portfolio(index_path, None, config)


# --- Open-ended stations ("2025–heute") ------------------------------------
# A current role has no end year. Before this, the header failed to parse,
# the company never entered COMPANY_TOKENS, and the generated header was
# flagged as an invented company — the 2026-08-14 guard firing on a real job.

OPEN_END_CV = """## Berufserfahrung

### 2025–heute | Eigenständige Vertiefung: KI- und Datenprodukte

- Eigenprojekte von Prototyp bis produktiv.

### 2023–2025 | HealthApp – Senior Product Owner

- Backlog-Führung für HealthAppConnect.
"""


@pytest.mark.parametrize("word", ["heute", "today", "present", "Heute"])
def test_open_ended_station_is_recognised(tmp_path: Path, word: str) -> None:
    import cv_tailor.consistency_check as cc

    cv = tmp_path / "standard_cv.md"
    cv.write_text(OPEN_END_CV.replace("heute", word), encoding="utf-8")
    cc.COMPANY_TOKENS = {}
    cc._TOKENS_LOADED_FROM = None

    draft = (
        f"### 2025–{word} | Eigenständige Vertiefung: KI- und Datenprodukte\n"
        "\nEtwas Belegtes.\n"
    )
    ok, issues = cc.validate_berufserfahrung(draft, cv)
    assert ok, issues


def test_open_end_maps_to_far_future_year(tmp_path: Path) -> None:
    import cv_tailor.consistency_check as cc

    cv = tmp_path / "standard_cv.md"
    cv.write_text(OPEN_END_CV, encoding="utf-8")
    cc.COMPANY_TOKENS = {}
    cc._TOKENS_LOADED_FROM = None
    cc._company_tokens(cv)  # tokens from the fixture, not from data/standard_cv.md

    ranges = cc.parse_standard_cv_ranges(OPEN_END_CV)
    open_ranges = [r for r in ranges.values() if r.end_year == cc.OPEN_END_YEAR]
    assert open_ranges, ranges


def test_invented_company_still_flagged_with_open_end_support(tmp_path: Path) -> None:
    """The relaxed year pattern must not weaken the invented-company guard."""
    import cv_tailor.consistency_check as cc

    cv = tmp_path / "standard_cv.md"
    cv.write_text(OPEN_END_CV, encoding="utf-8")
    cc.COMPANY_TOKENS = {}
    cc._TOKENS_LOADED_FROM = None

    draft = "### 2024–heute | Phantom Systems AG – Chief Everything\n\nEtwas.\n"
    ok, issues = cc.validate_berufserfahrung(draft, cv)
    assert not ok
    assert any("Erfundene Station" in i for i in issues)


# --- Wachstumskanten must never render as evidence --------------------------

def test_growth_edges_render_in_their_own_block() -> None:
    """A self-declared gap ranked among evidence reads as competence.

    First portfolio run put "Voice-Agents — fehlt komplett" under
    "Team Leadership" with score 9. The honesty limits should travel with the
    map, but never inside "Aktivierte Belege".
    """
    from cv_tailor.experience_activation import format_activation_markdown

    index = {
        "entries": [
            {
                "id": "BELG-900",
                "typ": "achievement",
                "snippet": "Multi-Agent-Pipeline mit Eval-Gating gesteuert",
                "quelle_typ": "portfolio",
                "quelle_position": "line:40",
                "quelle_datei": "PORTFOLIO.md",
                "section": "Eigene Projekte",
            },
            {
                "id": "BELG-901",
                "typ": "other",
                "snippet": "Voice-Agents — fehlt komplett, keine eigene Umsetzung",
                "quelle_typ": "portfolio",
                "quelle_position": "line:392",
                "quelle_datei": "PORTFOLIO.md",
                "section": "Wachstumskanten",
            },
        ]
    }
    job = (
        "Gesucht: Product Owner für KI-Agenten und Automatisierung. "
        "Erfahrung mit Multi-Agent-Systemen, Eval und Voice-Agents von Vorteil."
    )
    md = format_activation_markdown(job, index)

    assert "## Bekannte Lücken" in md
    before_gaps = md.split("## Bekannte Lücken", 1)[0]
    assert "Voice-Agents" not in before_gaps
    assert "BELG-901" in md.split("## Bekannte Lücken", 1)[1]


def test_evidence_sections_still_activate_normally() -> None:
    from cv_tailor.experience_activation import format_activation_markdown

    index = {
        "entries": [
            {
                "id": "BELG-900",
                "typ": "achievement",
                "snippet": "Backlog priorisiert und Roadmap verantwortet im Eigenprojekt",
                "quelle_typ": "portfolio",
                "quelle_position": "line:40",
                "quelle_datei": "PORTFOLIO.md",
                "section": "Eigene Projekte",
            }
        ]
    }
    job = "Product Owner für Backlog, Roadmap und Priorisierung gesucht."
    md = format_activation_markdown(job, index)
    assert "BELG-900" in md
    assert "Skalierungsgrenze" in md


def test_entries_without_section_are_treated_as_evidence() -> None:
    """Pre-existing index entries carry no `section` — they must not vanish."""
    from cv_tailor.experience_activation import format_activation_markdown

    index = {
        "entries": [
            {
                "id": "BELG-001",
                "typ": "achievement",
                "snippet": "Backlog priorisiert und Roadmap verantwortet",
                "quelle_typ": "standard_cv",
                "quelle_position": "line:1",
                "quelle_datei": "data/standard_cv.md",
            }
        ]
    }
    job = "Product Owner für Backlog, Roadmap und Priorisierung gesucht."
    md = format_activation_markdown(job, index)
    assert "BELG-001" in md
    assert "## Bekannte Lücken" not in md


# --- Completeness: a dropped station is invisible to every other rule -------

COMPLETE_CV = """## Berufserfahrung

### 2025–heute | Eigenständige Vertiefung: KI- und Datenprodukte

- Eigenprojekte.

### 2023–2025 | HealthApp – Senior Product Owner

- Backlog-Führung.
"""


def test_missing_station_is_flagged(tmp_path: Path) -> None:
    """Writer dropped '2025–heute' on both rounds and every check stayed green."""
    import cv_tailor.consistency_check as cc

    cv = tmp_path / "standard_cv.md"
    cv.write_text(COMPLETE_CV, encoding="utf-8")
    cc.COMPANY_TOKENS = {}
    cc._TOKENS_LOADED_FROM = None

    draft = "### 2023–2025 | HealthApp – Senior Product Owner\n\nBacklog-Führung.\n"
    ok, issues = cc.validate_berufserfahrung(draft, cv, check_completeness=True)
    assert not ok
    assert any("Fehlende Station" in i for i in issues)
    assert any("Eigenständige Vertiefung" in i for i in issues)


def test_complete_draft_passes(tmp_path: Path) -> None:
    import cv_tailor.consistency_check as cc

    cv = tmp_path / "standard_cv.md"
    cv.write_text(COMPLETE_CV, encoding="utf-8")
    cc.COMPANY_TOKENS = {}
    cc._TOKENS_LOADED_FROM = None

    ok, issues = cc.validate_berufserfahrung(COMPLETE_CV, cv, check_completeness=True)
    assert ok, issues


def test_scaling_limit_forbids_the_negation_pattern() -> None:
    """The limit steers word choice — it must not read as a sentence template.

    v1 ended with "… gemessen — nicht implementiert"; the writer copied that
    shape three times into the CV ("Steuerung, nicht Modellbau" / "entschieden,
    nicht gebaut" / "bei mir, nicht beim Engineering"). Defensive negations were
    removed from the writer prompt on 2026-05-11 — this channel must not
    reintroduce them.
    """
    from cv_tailor.experience_activation import format_activation_markdown

    index = {
        "entries": [
            {
                "id": "BELG-900",
                "typ": "achievement",
                "snippet": "Backlog priorisiert und Roadmap verantwortet im Eigenprojekt",
                "quelle_typ": "portfolio",
                "quelle_position": "line:40",
                "quelle_datei": "PORTFOLIO.md",
                "section": "Eigene Projekte",
            }
        ]
    }
    job = "Product Owner für Backlog, Roadmap und Priorisierung gesucht."
    md = format_activation_markdown(job, index)

    assert "Skalierungsgrenze" in md
    assert "Die Abgrenzung selbst gehört nicht in den CV-Text" in md
    assert "zulässige Verben" in md
    # Scope: the list applies to portfolio claims, not to evidenced stations.
    assert "Nur für Aussagen über diese Eigenprojekte" in md
    assert "keine allgemeine Stilvorgabe" in md
    # The block must not hand the writer a ready-made "X — nicht Y" clause.
    assert "gemessen — nicht implementiert" not in md
