"""verify: ordered deterministic bundle and freshness stamps for manual steps."""

from pathlib import Path

import pytest

from cv_tailor import verify as V

STANDARD_CV = """# Jane Doe

**E-Mail:** jane@example.org

## Berufserfahrung

### 2020–2023 | Acme AG – Product Owner

- Erster Bullet.
"""

FINAL = """# Finaler CV (DE)

## Management Summary

Ich leite Produkte seit 10 Jahren und habe 3 Teams aufgebaut.

## Schlüsselkompetenzen

**Produktführung** - Bei Acme Backlog geführt.

## Berufserfahrung

### 2020–2023 | Acme AG – Product Owner

- Erster Bullet.
"""


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "2026-10-30_demo"
    run.mkdir()
    (run / "04_final_de.md").write_text(FINAL, encoding="utf-8")
    return run


def test_stamp_is_fresh_until_the_text_changes(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert V.stamp_state(run, "ki-stil-check") == "missing"
    V.write_stamp(run, "ki-stil-check", "nach ATS-Ergänzung")
    assert V.stamp_state(run, "ki-stil-check") == "fresh"
    (run / "04_final_de.md").write_text(FINAL + "\nNeue Zeile.\n", encoding="utf-8")
    assert V.stamp_state(run, "ki-stil-check") == "stale"


def test_unknown_step_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        V.write_stamp(_run(tmp_path), "irgendwas")


def test_hash_covers_both_languages(tmp_path: Path) -> None:
    run = _run(tmp_path)
    before = V.text_hash(run)
    (run / "04_final_en.md").write_text("# Final CV (EN)\n", encoding="utf-8")
    assert V.text_hash(run) != before


def test_renderer_warnings_only_exist_for_stamped_runs(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert V.stamp_warnings(run) == []  # no stamp file: nothing to say
    V.write_stamp(run, "naturalisation")
    warnings = V.stamp_warnings(run)
    assert {w.split(":")[0] for w in warnings} == {"ki-stil-check", "quellenabgleich"}
    V.write_stamp(run, "ki-stil-check")
    V.write_stamp(run, "quellenabgleich")
    assert V.stamp_warnings(run) == []


def _by_name(results: list[V.CheckResult], prefix: str) -> V.CheckResult:
    return next(r for r in results if r.name.startswith(prefix))


def test_verify_fails_on_stale_required_stamps_and_ignores_early_steps(tmp_path: Path) -> None:
    run = _run(tmp_path)
    std = tmp_path / "standard_cv.md"
    std.write_text(STANDARD_CV, encoding="utf-8")
    V.write_stamp(run, "naturalisation")
    V.write_stamp(run, "ki-stil-check")
    V.write_stamp(run, "quellenabgleich")
    results = V.run_verify(run, with_pages=False, standard_cv_path=std)
    assert _by_name(results, "Stempel ki-stil-check").status == "OK"
    assert _by_name(results, "Seiten").status == "INFO"

    (run / "04_final_de.md").write_text(FINAL.replace("10 Jahren", "12 Jahren"), encoding="utf-8")
    results = V.run_verify(run, with_pages=False, standard_cv_path=std)
    assert _by_name(results, "Stempel ki-stil-check").status == "FAIL"
    assert _by_name(results, "Stempel quellenabgleich").status == "FAIL"
    # an earlier step is shown, never blocking
    assert _by_name(results, "Stempel naturalisation").status == "INFO"
    assert _by_name(results, "Stempel chatgpt").status == "INFO"


def test_verify_runs_facts_before_length_before_style_before_ats(tmp_path: Path) -> None:
    run = _run(tmp_path)
    std = tmp_path / "standard_cv.md"
    std.write_text(STANDARD_CV, encoding="utf-8")
    names = [r.name for r in V.run_verify(run, with_pages=False, standard_cv_path=std)]
    order = [
        "Faktengrenzen",
        "Konsistenz",
        "Pflichtsubstanz",
        "Länge",
        "Seiten",
        "Stil",
        "ATS",
        "Redundanz",
    ]
    positions = [next(i for i, n in enumerate(names) if n.startswith(o)) for o in order]
    assert positions == sorted(positions)


def test_missing_final_text_is_a_single_fail(tmp_path: Path) -> None:
    run = tmp_path / "leer"
    run.mkdir()
    assert [r.status for r in V.run_verify(run, with_pages=False)] == ["FAIL"]


def test_table_counts_fail_and_warn() -> None:
    table = V.format_table(
        [
            V.CheckResult("A", "OK"),
            V.CheckResult("B", "WARN", "x"),
            V.CheckResult("C", "FAIL", "y"),
        ]
    )
    assert "1 FAIL, 1 WARN" in table and "nicht übergeben" in table


def test_missing_bold_terms_are_named_per_section() -> None:
    assert V.missing_bold_terms(FINAL) == [
        "Summary ohne fette Begriffe",
        "Berufserfahrung nur 0 fette Begriffe",
    ]


def test_bold_terms_in_competencies_do_not_count_for_the_experience() -> None:
    marked = FINAL.replace("seit 10 Jahren", "seit **10 Jahren**").replace(
        "- Erster Bullet.", "- **Eins**, **zwei**, **drei** und **vier**."
    )
    assert V.missing_bold_terms(marked) == []


def test_english_sections_are_checked_too() -> None:
    english = "## Summary\n\nI lead.\n\n## Professional Experience\n\n- **One** term.\n"
    assert V.missing_bold_terms(english) == [
        "Summary ohne fette Begriffe",
        "Berufserfahrung nur 1 fette Begriffe",
    ]



ANALYSE_STARK = """### 4. Anforderungsabgleich
| Anforderung | Beleg-ID(s) | Status | Kommentar |
|---|---|---|---|
| Leitung komplexer Projekte | BELG-901, BELG-902 | STARK | Team |
| Budget | BELG-903 | MITTEL | Team |
"""
ENTRIES = [
    {"id": "BELG-901", "behauptung": "Demo-Portal: Aufbau und Leitung eines 8-köpfigen Teams"},
    {"id": "BELG-902", "behauptung": "Projektcontrolling und Reporting"},
    {"id": "BELG-903", "behauptung": "Führung eines 4-köpfigen Teams"},
]


def test_strong_team_size_missing_from_final_is_named() -> None:
    final = "## Berufserfahrung\n- Projektcontrolling verantwortet.\n"
    found = V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES)
    assert len(found) == 1 and found[0].startswith("BELG-901 (8 Personen)")


def test_strong_team_size_present_or_only_medium_is_quiet() -> None:
    final = "## Berufserfahrung\n- Ein **8-köpfiges Team** aufgebaut.\n"
    assert V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES) == []


def test_unrelated_number_does_not_count_as_team_size() -> None:
    final = "## Berufserfahrung\n- 8 Agenten orchestriert, seit 8 Jahren Teams geführt.\n"
    assert V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES)


def test_header_numbers_do_not_count_as_present() -> None:
    final = "**Run:** 2026-08-08\n\n## Berufserfahrung\n- Projektcontrolling.\n"
    assert V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES)
    no_heading = "**Run:** 8 Personen im Lauf\n\n- Projektcontrolling.\n"
    assert V.missing_strong_team_sizes(ANALYSE_STARK, no_heading, ENTRIES)


def test_team_size_as_number_word_counts_as_present() -> None:
    final = "## Berufserfahrung\n- Ein achtköpfiges Team aufgebaut.\n"
    assert V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES) == []


def test_id_ranges_are_expanded() -> None:
    analysis = ANALYSE_STARK.replace("BELG-901, BELG-902", "BELG-900–902")
    final = "## Berufserfahrung\n- Projektcontrolling.\n"
    assert V.missing_strong_team_sizes(analysis, final, ENTRIES)[0].startswith("BELG-901")
    assert V._belg_ids("BELG-005-BELG-007, BELG-010") == [
        "BELG-005", "BELG-006", "BELG-007", "BELG-010"
    ]
    assert V._belg_ids("BELG-001–999") == []  # implausible range, never guessed


def test_mixed_or_bold_strong_status_counts() -> None:
    final = "## Berufserfahrung\n- Projektcontrolling.\n"
    for status in ("**STARK**", "MITTEL–STARK"):
        analysis = ANALYSE_STARK.replace("| STARK |", f"| {status} |")
        assert V.missing_strong_team_sizes(analysis, final, ENTRIES), status


def test_status_column_is_taken_from_the_header() -> None:
    analysis = (
        "| Anforderung | Status | Beleg-ID(s) |\n|---|---|---|\n"
        "| Leitung | STARK | BELG-901 |\n"
    )
    final = "## Berufserfahrung\n- Projektcontrolling.\n"
    assert V.missing_strong_team_sizes(analysis, final, ENTRIES)


def test_company_size_and_staffing_change_are_no_team() -> None:
    entries = [
        {"id": "BELG-901", "behauptung": "Acme ist ein führender Anbieter mit 300 Mitarbeitenden"},
        {"id": "BELG-902", "behauptung": "Releasezeit halbiert, Team von 4 auf 2 Personen"},
    ]
    final = "## Berufserfahrung\n- Projektcontrolling.\n"
    assert V.missing_strong_team_sizes(ANALYSE_STARK, final, entries) == []


def test_strong_team_size_behind_a_fact_limit_is_skipped(monkeypatch) -> None:
    from cv_tailor import fact_limits

    monkeypatch.setattr(fact_limits, "check_text", lambda text, limits: ["hit"])
    final = "## Berufserfahrung\n- Projektcontrolling.\n"
    assert V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES, limits=["x"]) == []


def test_fact_limit_is_checked_at_the_entry_station(tmp_path: Path) -> None:
    from cv_tailor import fact_limits

    yaml_text = """limits:
  - id: fuehrung
    fact: "Teamleitung nur bei Acme"
    checks:
      - pattern: "Leitung eines"
        message: "nur bei Acme"
        only_in_station: "Acme"
"""
    path = tmp_path / "fact_limits.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    limits = fact_limits.load_fact_limits(path)
    final = "## Berufserfahrung\n- Projektcontrolling.\n"
    # without a station the limit fires and the entry is skipped
    assert V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES, limits) == []
    # at the station where it is allowed, the missing team is named
    stations = {"BELG-901": "### 2020–2023 | Acme AG – Product Owner"}
    found = V.missing_strong_team_sizes(ANALYSE_STARK, final, ENTRIES, limits, stations)
    assert found and found[0].startswith("BELG-901")


def test_station_of_standard_cv_entries(tmp_path: Path) -> None:
    std = tmp_path / "standard_cv.md"
    std.write_text(STANDARD_CV, encoding="utf-8")
    entries = [
        {"id": "BELG-001", "quelle_typ": "standard_cv", "quelle_position": "line:9"},
        {"id": "BELG-002", "quelle_typ": "zeugnis", "quelle_position": "line:9"},
    ]
    assert V.station_of_entries(entries, std) == {
        "BELG-001": "### 2020–2023 | Acme AG – Product Owner"
    }


def test_run_verify_wires_the_strong_evidence_check(tmp_path: Path, monkeypatch) -> None:
    import json

    from cv_tailor import fact_limits

    monkeypatch.setattr(fact_limits, "load_fact_limits", lambda *a, **k: [])
    run = _run(tmp_path)
    std = tmp_path / "standard_cv.md"
    std.write_text(STANDARD_CV, encoding="utf-8")
    index = tmp_path / "beleg_index.json"
    name = "Starke Belege"

    # no analysis: the index is never read, the check is INFO
    missing_index = tmp_path / "fehlt.json"
    results = V.run_verify(
        run, with_pages=False, standard_cv_path=std, beleg_index_path=missing_index
    )
    assert _by_name(results, name).status == "INFO"

    (run / "01_analyse.md").write_text(ANALYSE_STARK, encoding="utf-8")
    index.write_text(json.dumps({"entries": ENTRIES}), encoding="utf-8")
    results = V.run_verify(run, with_pages=False, standard_cv_path=std, beleg_index_path=index)
    check = _by_name(results, name)
    assert check.status == "WARN" and "BELG-901" in check.detail
