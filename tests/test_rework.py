"""Rework metrics: raw pipeline output vs. the CV at handover."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from cv_tailor import quality_snapshot as qs
from cv_tailor import rework
from cv_tailor.cv_filename import CV_AUTHOR_TOKEN

RAW = """# Finaler CV (DE)

**Run:** test

## Management Summary

Ich habe Produkte gebaut und Teams geführt.

---

## Schlüsselkompetenzen

**Führung** - Teams aufgebaut.

---

## Berufserfahrung

### 2020–2023 | Acme – PO

- Plattform verantwortet.
"""


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "2026-10-23_acme"
    run.mkdir()
    (run / "04_final_de.md").write_text(RAW, encoding="utf-8")
    return run


def test_save_pipeline_raw_first_call_wins(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert rework.save_pipeline_raw(run) == [run / "_pipeline_raw_de.md"]
    (run / "04_final_de.md").write_text(RAW.replace("gebaut", "entwickelt"), encoding="utf-8")
    assert rework.save_pipeline_raw(run) == []
    assert "gebaut" in (run / "_pipeline_raw_de.md").read_text(encoding="utf-8")


def test_measure_uses_freshest_friendly_copy(tmp_path: Path) -> None:
    run = _run(tmp_path)
    rework.save_pipeline_raw(run)
    friendly = run / f"{CV_AUTHOR_TOKEN}-000000 Acme PO.md"
    edited = RAW.replace(
        "Plattform verantwortet.", "Plattform verantwortet und zwei Releases geliefert."
    )
    friendly.write_text(edited, encoding="utf-8")
    future = time.time() + 5
    os.utime(friendly, (future, future))

    m = rework.measure_rework(run)
    assert m["rework_raw_source"] == "raw"
    assert m["rework_word_delta"] == 4
    assert 70 < m["rework_kept_pct"] < 100


def test_measure_ignores_metadata_and_bold(tmp_path: Path) -> None:
    run = _run(tmp_path)
    rework.save_pipeline_raw(run)
    marked = RAW.replace("**Run:** test", "**Run:** other")
    marked = marked.replace("Teams aufgebaut", "**Teams** aufgebaut")
    (run / "04_final_de.md").write_text(marked, encoding="utf-8")
    assert rework.measure_rework(run)["rework_kept_pct"] == 100.0


def test_fallback_to_writer_backup_then_none(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert rework.resolve_raw(run)[1] == "none"
    (run / "04_final_de.md.bak.writer").write_text(RAW, encoding="utf-8")
    assert rework.resolve_raw(run)[1] == "bak.writer"
    assert rework.measure_rework(run)["rework_kept_pct"] == 100.0


def test_record_updates_snapshot_in_place(tmp_path: Path) -> None:
    run = _run(tmp_path)
    rework.save_pipeline_raw(run)
    path = tmp_path / "snapshots.jsonl"
    path.write_text(
        "\n".join(json.dumps({"run_id": r, "calls": 1}) for r in (run.name, "later_run")) + "\n",
        encoding="utf-8",
    )
    rework.record_rework(run, snapshots_path=path)
    records = qs.load_snapshots(path)
    assert [r["run_id"] for r in records] == [run.name, "later_run"]  # order kept
    assert records[0]["rework_kept_pct"] == 100.0
    assert records[0]["calls"] == 1


def test_trend_columns_and_regression_need_measured_runs(tmp_path: Path) -> None:
    assert qs._fmt_kept({"rework_kept_pct": 41.6, "rework_raw_source": "raw"}) == "42"
    approx = {"rework_kept_pct": 41.6, "rework_raw_source": "iterationen (approx.)"}
    assert qs._fmt_kept(approx) == "~42"
    assert qs._fmt_kept({}) == ""
    assert qs._fmt_fact_limits({"fact_limits_raw": 3, "fact_limits_final": 0}) == "3→0"

    path = tmp_path / "snapshots.jsonl"
    snaps = [{"run_id": f"r{i}", "cache_hit_rate": 0.6, "rework_kept_pct": 80} for i in range(4)]
    snaps.append({"run_id": "r4", "cache_hit_rate": 0.6, "rework_kept_pct": 30})
    path.write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    metrics = [f["metric"] for f in qs.detect_regressions(path)]
    assert "rework_kept_pct" in metrics

    snaps[-1].pop("rework_kept_pct")  # not handed over yet → no signal
    path.write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    assert "rework_kept_pct" not in [f["metric"] for f in qs.detect_regressions(path)]


def test_eval_section_extraction_spans_station_separators() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import run as eval_run

    text = RAW + "\n---\n\n### 2018–2020 | Beta – PM\n\n- Zweite Station.\n"
    section = eval_run._extract_section(text, "Berufserfahrung")
    assert "Acme" in section and "Zweite Station" in section
    assert eval_run._extract_section(text, "Management Summary").endswith("Teams geführt.")
