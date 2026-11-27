"""Open factcheck points: the run yields a CV, the PDF stays locked (2026-11-25).

All inputs are synthetic; writer, reviewers and the factcheck model are stubs.
"""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cv_tailor import pdf_renderer, web
from cv_tailor import verify as V
from cv_tailor.agents import factcheck as fc
from cv_tailor.agents import writer_loop as wl
from cv_tailor.llm import ABBRUCH_FEHLER
from cv_tailor.orchestrator import RunContext
from tests.test_factcheck_truncation import _FakeLLM

DRAFT = "- Erfundene Zeile."
SECTION = "management_summary"


def _answer(veto: bool) -> str:
    problem = {
        "behauptung": "Team von 99",
        "art": "zahl",
        "beleg_ids": [],
        "begruendung": "nirgends belegt",
        "einstufung": "kein_beleg",
    }
    return json.dumps(
        {"geprueft": [DRAFT], "veto": veto, "has_gaps": False, "probleme": [problem] * veto}
    )


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch) -> RunContext:
    run = tmp_path / "2026-11-25_demo"
    run.mkdir()
    (run / "_run.log").write_text("", encoding="utf-8")
    index = tmp_path / "beleg_index.json"
    index.write_text('{"belege": []}', encoding="utf-8")
    prompt = tmp_path / "factcheck.md"
    prompt.write_text("synthetisch", encoding="utf-8")

    def writer(ctx, section, round_num, **kwargs):
        path = ctx.run_dir / "03_iterationen" / f"{section}_v{round_num}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(DRAFT, encoding="utf-8")
        return path

    def factcheck(ctx, section, text, round_num, **kwargs):
        return fc.run_factcheck_iteration(
            ctx, section, text, round_num, beleg_index_path=index, prompt_path=prompt
        )

    monkeypatch.setattr(wl, "run_writer", writer)
    monkeypatch.setattr(wl, "run_factcheck_iteration", factcheck)
    monkeypatch.setattr(wl, "run_hiring_reviewer", lambda *a: "")
    monkeypatch.setattr(wl, "run_coach_reviewer", lambda *a: "")
    monkeypatch.setattr(wl, "validate_berufserfahrung", lambda *a, **k: (True, []))
    monkeypatch.setattr(wl, "check_section_length", lambda *a: [])
    monkeypatch.setattr(wl, "check_fact_limits", lambda *a: [])
    monkeypatch.setattr(wl, "load_fact_limits", lambda: [])
    monkeypatch.setattr(wl, "check_style", lambda *a: [])
    monkeypatch.setattr(wl, "station_tokens", lambda *a: [])
    monkeypatch.setattr(wl, "check_station_rules", lambda *a, **k: [])
    monkeypatch.setattr(wl, "load_station_rules", lambda: [])
    monkeypatch.setattr(wl, "ki_posting_for_run", lambda d: False)
    monkeypatch.setattr("cv_tailor.clarifications.write_selection_report", lambda *a: None)
    monkeypatch.setattr("cv_tailor.clarifications.format_clarifications_for_prompt", lambda **k: "")
    monkeypatch.setattr(fc, "format_clarifications_for_prompt", lambda **k: "")
    monkeypatch.setattr(fc, "get_beleg_index_compact", lambda p: "")
    monkeypatch.setattr("cv_tailor.prompt_context.build_gating_context", lambda ctx: "")
    monkeypatch.setattr("cv_tailor.factcheck_sources.static_blocks", lambda *a: [])
    monkeypatch.setattr("cv_tailor.factcheck_sources.extra_clarifications_block", lambda *a: "")
    monkeypatch.setattr("cv_tailor.fact_limits.prompt_block", lambda: "")
    monkeypatch.setattr("cv_tailor.companion_docs.load_companion_context", lambda d: None)
    monkeypatch.setattr("cv_tailor.companion_docs.format_for_prompt", lambda c, role: "")
    monkeypatch.setattr("cv_tailor.redundancy_check.write_redundancy_report", lambda *a: None)
    return RunContext(run_id=run.name, run_dir=run, started_at="")


def _model(monkeypatch, content: str, truncated: bool = False) -> None:
    monkeypatch.setattr(fc, "call_llm", _FakeLLM([(content, truncated)] * 20))


def _open_text(ctx: RunContext) -> str:
    return (ctx.run_dir / V.FACTCHECK_OPEN_FILE).read_text(encoding="utf-8")


def test_persistent_veto_yields_cv_and_open_points(ctx, monkeypatch) -> None:
    _model(monkeypatch, _answer(veto=True))
    final = wl.run_writer_loop(ctx)
    assert final.is_file()
    text = _open_text(ctx)
    for title in wl.SECTION_TITLES.values():
        assert f"## {title}" in text
    assert "Team von 99" in text and "ungeprüft" not in text
    assert f"**Runde:** {wl.MAX_ROUNDS}/{wl.MAX_ROUNDS}" in text
    log = (ctx.run_dir / "_run.log").read_text(encoding="utf-8")
    # round 1 still revises, both rounds are still counted as vetoes
    assert "Runde 1: Veto — weiter zu Runde 2" in log
    assert log.count("Veto (Drift)") == wl.MAX_ROUNDS * len(wl.SECTIONS)
    assert "mit offenen Faktencheck-Punkten" in log
    assert not list(ctx.run_dir.glob("_factcheck_blocker_*"))


@pytest.mark.parametrize(
    ("content", "truncated"),
    [("", False), ("abgeschnittene Prosa", True), (_answer(veto=True), True)],
)
def test_unreadable_or_truncated_verdict_is_unchecked(ctx, monkeypatch, content, truncated) -> None:
    _model(monkeypatch, content, truncated)
    wl.run_writer_loop(ctx)
    assert _open_text(ctx).count("**Status:** ungeprüft") == len(wl.SECTIONS)


def test_clean_loop_removes_stale_file_and_stamp(ctx, monkeypatch) -> None:
    _model(monkeypatch, _answer(veto=True))
    wl.run_writer_loop(ctx)
    V.write_stamp(ctx.run_dir, V.FACTCHECK_OPEN_STEP, "akzeptiert")
    _model(monkeypatch, _answer(veto=False))
    wl.run_writer_loop(ctx)
    assert not (ctx.run_dir / V.FACTCHECK_OPEN_FILE).exists()
    assert V.FACTCHECK_OPEN_STEP not in V.read_stamps(ctx.run_dir)


def test_consistency_veto_after_autofix_stays_hard(ctx, monkeypatch) -> None:
    _model(monkeypatch, _answer(veto=True))
    monkeypatch.setattr(wl, "validate_berufserfahrung", lambda *a, **k: (False, ["Drift"]))
    monkeypatch.setattr(wl, "format_issues_for_writer", lambda issues: "Drift")
    monkeypatch.setattr(wl, "autofix_headers", lambda text, path: (text, []))
    monkeypatch.setattr(wl, "autofix_station_layout", lambda text, path, **k: (text, []))
    with pytest.raises(RuntimeError, match="Konsistenz"):
        wl.run_writer_loop(ctx)
    assert not (ctx.run_dir / "04_final_de.md").exists()
    assert (ctx.run_dir / "_factcheck_blocker_berufserfahrung.md").is_file()


@pytest.mark.parametrize("error", ABBRUCH_FEHLER)
def test_abort_errors_are_not_turned_into_open_points(ctx, monkeypatch, error) -> None:
    exc = error.__new__(error)

    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(fc, "call_llm", boom)
    with pytest.raises(error):
        wl.run_writer_loop(ctx)
    assert not (ctx.run_dir / "04_final_de.md").exists()


def _run_with_open_points(tmp_path: Path) -> Path:
    run = tmp_path / "2026-11-25_offen"
    run.mkdir()
    (run / "04_final_de.md").write_text("## Management Summary\n\nText.\n", encoding="utf-8")
    (run / V.FACTCHECK_OPEN_FILE).write_text("# Offen\n\n- Punkt\n", encoding="utf-8")
    return run


def _verify_status(run: Path) -> list[str]:
    results = V.run_verify(run, with_pages=False, standard_cv_path=run / "fehlt.md")
    return [r.status for r in results if r.name == "Faktencheck offene Punkte"]


def test_verify_locks_until_stamp_and_again_after_changes(tmp_path: Path) -> None:
    run = _run_with_open_points(tmp_path)
    assert _verify_status(run) == ["FAIL"]
    V.write_stamp(run, V.FACTCHECK_OPEN_STEP, "von Alex akzeptiert")
    assert _verify_status(run) == ["OK"]
    assert V.factcheck_open_block(run) is None
    (run / "04_final_de.md").write_text("## Management Summary\n\nNeu.\n", encoding="utf-8")
    assert _verify_status(run) == ["FAIL"]
    V.write_stamp(run, V.FACTCHECK_OPEN_STEP, "von Alex akzeptiert")
    (run / V.FACTCHECK_OPEN_FILE).write_text("# Offen\n\n- anderer Punkt\n", encoding="utf-8")
    assert _verify_status(run) == ["FAIL"]


def test_verify_is_silent_without_open_points(tmp_path: Path) -> None:
    run = _run_with_open_points(tmp_path)
    (run / V.FACTCHECK_OPEN_FILE).unlink()
    assert _verify_status(run) == []
    with pytest.raises(ValueError):
        V.write_stamp(run, V.FACTCHECK_OPEN_STEP, "nichts da")


def test_stamp_needs_a_note(tmp_path: Path) -> None:
    run = _run_with_open_points(tmp_path)
    with pytest.raises(ValueError, match="--note"):
        V.write_stamp(run, V.FACTCHECK_OPEN_STEP, "  ")


def test_renderer_refuses_then_renders_after_stamp(tmp_path: Path, monkeypatch) -> None:
    run = _run_with_open_points(tmp_path)
    (run / "04_final_de.pdf").write_bytes(b"alt")
    ctx = RunContext(run_id=run.name, run_dir=run, started_at="")
    template = tmp_path / "template.html"
    template.write_text("x", encoding="utf-8")
    reached: list[str] = []
    monkeypatch.setattr(
        pdf_renderer, "build_template_markdown", lambda *a, **k: reached.append("x")
    )
    monkeypatch.setattr(pdf_renderer, "missing_generated_sections", lambda md: ["Stopp"])

    with pytest.raises(pdf_renderer.FaktencheckOffen, match="faktencheck-offen"):
        pdf_renderer.run_pdf_renderer(ctx, template_path=template)
    assert not reached and not (run / "04_final_de.pdf").exists()

    V.write_stamp(run, V.FACTCHECK_OPEN_STEP, "korrigiert")
    pdf_renderer.run_pdf_renderer(ctx, template_path=template)
    assert reached


def test_web_serves_the_file_and_refuses_the_render(tmp_path: Path) -> None:
    assert V.FACTCHECK_OPEN_FILE in web.ALLOWED_RUN_FILES
    run = _run_with_open_points(tmp_path)
    job_id = web.STATE.create_job(run / "00_stellenanzeige.md").job_id
    web.STATE.update(job_id, status="completed", run_id=run.name, run_dir=str(run))
    sent: list[tuple] = []

    class Handler:
        def _send_error(self, status, message):
            sent.append((status, message))

    web.CvTailorHandler._handle_render_pdf(Handler(), f"/api/runs/{job_id}/pdf")
    assert sent[0][0] == 409 and "faktencheck-offen" in sent[0][1]
    assert web.STATE.get(job_id).status == "completed"


def test_cli_closing_message_and_stamp(tmp_path: Path, monkeypatch, capsys) -> None:
    from cv_tailor import cli

    (tmp_path / "runs").mkdir()
    run = _run_with_open_points(tmp_path / "runs")
    monkeypatch.chdir(tmp_path)
    cli._echo_factcheck_open(run)
    assert V.FACTCHECK_OPEN_FILE in capsys.readouterr().out

    runner = CliRunner()
    without = runner.invoke(cli.cli, ["stamp", V.FACTCHECK_OPEN_STEP, run.name])
    assert without.exit_code == 2
    with_note = runner.invoke(cli.cli, ["stamp", V.FACTCHECK_OPEN_STEP, run.name, "--note", "ok"])
    assert with_note.exit_code == 0
    cli._echo_factcheck_open(run)
    assert capsys.readouterr().out == ""
