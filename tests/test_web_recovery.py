"""Tests for conservative Web-UI restart recovery."""
from __future__ import annotations

from pathlib import Path

from cv_tailor import web


def _make_run(tmp_path: Path, name: str) -> Path:
    run_dir = tmp_path / "runs" / name
    run_dir.mkdir(parents=True)
    (run_dir / "01_analyse.md").write_text("# Analyse\n", encoding="utf-8")
    (run_dir / "00_stellenanzeige.md").write_text("# Posting\n", encoding="utf-8")
    (run_dir / "_run.log").write_text(
        "# Run\n\n**Started:** 2026-05-22T00:00:00+00:00\n\n## Events\n",
        encoding="utf-8",
    )
    return run_dir


def test_recovery_restores_critical_profile_fit_gate(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, "2026-05-22_fit_gate")
    (run_dir / "_profile_fit.md").write_text(
        "# Profil-Fit-Hinweise\n\n"
        "## Kritisch (1) - nicht belegte Muss-Anforderungen\n\n"
        "- **Audit background**\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    state = web.WebState()

    recovered = state.get(f"recovered_{run_dir.name}")
    assert recovered is not None
    assert recovered.status == "fit_warning"
    assert "Profil-Fit" in recovered.stage


def test_resume_without_answers_rechecks_factcheck_before_writer(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, "2026-05-22_resume")
    job = web.WebJob(
        job_id="resume_job",
        status="continuing",
        run_id=run_dir.name,
        run_dir=str(run_dir),
    )
    monkeypatch.chdir(tmp_path)
    state = web.WebState()
    state._jobs[job.job_id] = job
    monkeypatch.setattr(web, "STATE", state)

    calls: list[str] = []

    def fake_factcheck(ctx):
        calls.append(f"factcheck:{ctx.run_id}")
        return False

    def fake_finish(job_id, ctx):
        calls.append(f"finish:{job_id}:{ctx.run_id}")

    from cv_tailor.agents import factcheck

    monkeypatch.setattr(factcheck, "run_factcheck", fake_factcheck)
    monkeypatch.setattr(web, "_finish_pipeline", fake_finish)

    web._resume_pipeline(job.job_id)

    assert calls == [
        f"factcheck:{run_dir.name}",
        f"finish:{job.job_id}:{run_dir.name}",
    ]
