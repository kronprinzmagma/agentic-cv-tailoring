"""Regression tests for the project review of 2026-10-23 (ChatGPT).

The review shipped reproductions that confirmed the buggy behaviour
(evals/reports/project_review_2026-10-23/reproduce.py). These tests run the
same synthetic scenarios with the expectation reversed. Findings with a
natural home have their tests there (factcheck verdict, final check,
consistency, template, sync, friendly copy); this file covers the
cross-component ones: which version is current, web visibility, caches and
measurement.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cv_tailor import consistency_check as cc
from cv_tailor.orchestrator import RunContext

STANDARD = """# Demo Person

## Berufserfahrung

### 2023–2025 | ExampleCo – Product Owner

- Plattform mit 3 Nutzergruppen ausgebaut.
"""
DE = """## Management Summary

Ich habe 10 Personen geführt und ein Produkt erfolgreich auf den Markt gebracht.

## Schlüsselkompetenzen

Produktführung und sorgfältige Priorisierung anhand dokumentierter Kundenbedürfnisse.

## Berufserfahrung

### 2023–2025 | ExampleCo – Product Owner

- Plattform mit 3 Nutzergruppen ausgebaut.
"""
EN = """## Management Summary

I led 10 people and successfully launched a product with a documented customer focus.

## Key Competencies

Product leadership and careful prioritisation based on documented customer needs.

## Professional Experience

### 2023–2025 | ExampleCo – Product Owner

- Expanded the platform to 3 user groups.
"""


@pytest.fixture
def base(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/standard_cv.md").write_text(STANDARD, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc, "COMPANY_TOKENS", {})
    monkeypatch.setattr(cc, "_TOKENS_LOADED_FROM", None)
    yield tmp_path
    monkeypatch.setattr(cc, "COMPANY_TOKENS", {})
    monkeypatch.setattr(cc, "_TOKENS_LOADED_FROM", None)


def make_run(base: Path, name: str) -> tuple[Path, RunContext]:
    run = base / name
    run.mkdir()
    (run / "04_final_de.md").write_text(DE, encoding="utf-8")
    (run / "00_stellenanzeige.md").write_text(
        "# Product Owner\n**Arbeitgeber:** DemoCorp\n", encoding="utf-8"
    )
    return run, RunContext(name, run, "2026-10-23T00:00:00+00:00")


def test_r05_rejected_retranslation_retires_old_en(base):
    from cv_tailor import web
    from cv_tailor.agents import translator

    run, ctx = make_run(base, "stale_translation")
    (run / "04_final_en.md").write_text(EN, encoding="utf-8")
    (run / "04_final_en.pdf").write_bytes(b"old")
    (run / "00_stellenanzeige.md").write_text(
        "English requirements: experience and qualifications. " * 15, encoding="utf-8"
    )
    with patch.object(translator, "is_primarily_english", return_value=True), \
         patch.object(translator, "call_llm", return_value=""):
        out = translator.run_translator(
            ctx, prompt_path=Path(__file__).parents[1] / "prompts/translator.md",
            beleg_index_path=base / "absent.json",
        )
    assert out is None
    assert not (run / "04_final_en.md").exists() and not (run / "04_final_en.pdf").exists()
    assert (run / "04_final_en.md.stale").exists()
    files = web._job_to_dict(
        web.WebJob("test", status="completed", run_id=run.name, run_dir=str(run))
    )["available_files"]
    assert "04_final_de.md" in files and "04_final_en.md" not in files


def test_r07_failed_pdf_rebuild_retires_old_pdf(base):
    from cv_tailor import pdf_renderer

    run, ctx = make_run(base, "stale_pdf")
    (run / "04_final_de.md").write_text(
        DE.replace("## Schlüsselkompetenzen", "## Other"), encoding="utf-8"
    )
    (run / "04_final_de.pdf").write_bytes(b"old PDF")
    template = base / "template.html"
    template.write_text("placeholder", encoding="utf-8")
    written = pdf_renderer.run_pdf_renderer(
        ctx, template_path=template, standard_cv_path=base / "data/standard_cv.md"
    )
    assert written == []
    assert not (run / "04_final_de.pdf").exists()
    assert (run / "04_final_de.pdf.stale").read_bytes() == b"old PDF"


def test_r10_naturalisation_uses_newer_canonical(base):
    from cv_tailor.agents import naturalisation
    from cv_tailor.cv_filename import CV_AUTHOR_TOKEN

    run, ctx = make_run(base, "naturalisation_old_source")
    canonical = run / "04_final_de.md"
    friendly = run / f"{CV_AUTHOR_TOKEN}-demo.md"
    friendly.write_text("OLDER TEXT WITH KNOWN ERRORS", encoding="utf-8")
    canonical.write_text(DE + "\nCURRENT CORRECTION\n", encoding="utf-8")
    os.utime(friendly, (1000, 1000))
    os.utime(canonical, (2000, 2000))
    with patch.object(naturalisation, "call_llm", return_value='{"suggestions": []}'), \
         patch.object(naturalisation, "load_prompt", return_value="test"):
        result = naturalisation.run_naturalisation(ctx)
    assert result["source"] == canonical.name


def test_r14_run_claim_check_reads_edited_friendly_copy(base):
    import re

    from cv_tailor.cv_filename import write_friendly_copy
    from cv_tailor.fact_limits import Check, FactLimit, write_run_report

    run, _ = make_run(base, "friendly_fact_check")
    friendly = write_friendly_copy(run / "04_final_de.md", run / "00_stellenanzeige.md")
    friendly.write_text(DE + "\nFORBIDDEN CLAIM\n", encoding="utf-8")
    newer = (run / "04_final_de.md").stat().st_mtime + 10
    os.utime(friendly, (newer, newer))
    limits = [FactLimit(
        "synthetic", "Forbid this claim", (Check(re.compile("FORBIDDEN CLAIM"), "not allowed"),)
    )]
    report, count = write_run_report(run, limits)
    assert count == 1
    assert friendly.name in report.read_text(encoding="utf-8")


def test_r08_fact_limits_report_is_served():
    from cv_tailor import web

    for name in ("_fact_limits.md", "_pdf_missing_sections_de.md", "_pdf_missing_sections_en.md"):
        assert name in web.ALLOWED_RUN_FILES
    html = (Path(web.__file__).parent / "web_assets/index.html").read_text(encoding="utf-8")
    assert '"_fact_limits.md": "Faktengrenzen"' in html


def test_r09_historical_cost_found_by_run_month(tmp_path):
    import json

    from cv_tailor import cost_tracking

    logs = tmp_path / "2026-06"
    logs.mkdir()
    (logs / "llm_calls.jsonl").write_text(
        json.dumps({"run_id": "2026-06-01_demo", "status": "success", "cost_usd": 1.23}) + "\n",
        encoding="utf-8",
    )
    cost = cost_tracking.compute_run_cost("2026-06-01_demo", log_root=tmp_path)
    assert cost["calls"] == 1 and cost["total_cost_usd"] == pytest.approx(1.23)


def test_r11_index_cache_sees_updates(tmp_path):
    import json

    from cv_tailor import beleg_index as bi

    idx = tmp_path / "index.json"
    entry = {"id": "BELG-1", "typ": "skill", "snippet": "OLD CLAIM", "behauptung": "OLD CLAIM",
             "quelle_typ": "standard_cv", "quelle_datei": "x", "quelle_position": "line:1"}
    idx.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    assert "OLD CLAIM" in bi.get_beleg_index_compact(idx)
    entry["snippet"] = entry["behauptung"] = "CORRECTED CLAIM"
    bi.write_beleg_index({"entries": [entry]}, idx)
    assert "CORRECTED CLAIM" in bi.get_beleg_index_compact(idx)
    entry["snippet"] = entry["behauptung"] = "EXTERNAL EDIT"
    idx.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")  # e.g. refresh-index
    os.utime(idx, ns=(idx.stat().st_mtime_ns + 10**9, idx.stat().st_mtime_ns + 10**9))
    assert "EXTERNAL EDIT" in bi.get_beleg_index_compact(idx)


def test_r13_concurrent_snapshot_updates_keep_both(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from cv_tailor import quality_snapshot as qs

    snapshots = tmp_path / "snapshots.jsonl"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda name: qs.update_snapshot_fields(name, {"rework_kept_pct": 50}, snapshots),
            [f"run-{i}" for i in range(16)],
        ))
    assert len(qs.load_snapshots(snapshots)) == 16
