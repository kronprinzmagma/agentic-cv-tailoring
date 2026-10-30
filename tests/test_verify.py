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
