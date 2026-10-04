"""Pins the hard cap on initial-factcheck question rounds.

Observed 2026-06-12: 8 consecutive question rounds in one run — the
factcheck found new doubts in every answer context and retroactively
escalated requirements ("AI project on-site at a 2011 station"). Once
answers exist, the initial factcheck must never pause the pipeline again;
remaining doubts become advisory (_factcheck_open_questions.md).
"""
import json

import pytest

from cv_tailor.agents import factcheck as fc
from cv_tailor.orchestrator import RunContext


@pytest.fixture
def run_ctx(tmp_path, monkeypatch):
    run_dir = tmp_path / "2026-06-12_testrun"
    run_dir.mkdir()
    (run_dir / "01_analyse.md").write_text("# Analyse\n\nKein Abgleich.", encoding="utf-8")
    (run_dir / "00_stellenanzeige.md").write_text("# Posting", encoding="utf-8")
    monkeypatch.setattr(fc, "get_beleg_index_compact", lambda *a, **k: "BELG-001: Demo")
    monkeypatch.setattr(fc, "format_clarifications_for_prompt", lambda *a, **k: "")
    monkeypatch.setattr(fc, "load_prompt", lambda *a, **k: "system prompt")
    return RunContext(run_id=run_dir.name, run_dir=run_dir, started_at="2026-06-12T00:00:00Z")


def _llm_with_gaps(*args, **kwargs):
    return json.dumps({
        "has_gaps": True,
        "veto": False,
        "questions_markdown": "Bitte Detail X klären.",
        "findings_markdown": "Detail X unklar.",
    })


def test_first_round_pauses_with_questions(run_ctx, monkeypatch):
    monkeypatch.setattr(fc, "call_llm", _llm_with_gaps)
    assert fc.run_factcheck(run_ctx) is True
    assert (run_ctx.run_dir / "02_klaerungsfragen.md").exists()


def test_answered_round_never_pauses_again(run_ctx, monkeypatch):
    """With answers on disk, has_gaps=True becomes advisory, not a pause."""
    (run_ctx.run_dir / "02_klaerungsfragen.md").write_text("# Fragen\n\n1. X?", encoding="utf-8")
    (run_ctx.run_dir / "02_antworten.md").write_text("# Antworten\n\nHabe ich nicht.", encoding="utf-8")
    monkeypatch.setattr(fc, "call_llm", _llm_with_gaps)

    assert fc.run_factcheck(run_ctx) is False

    advisory = run_ctx.run_dir / "_factcheck_open_questions.md"
    assert advisory.exists()
    assert "beratend" in advisory.read_text(encoding="utf-8")


def test_clean_followup_round_writes_no_advisory(run_ctx, monkeypatch):
    (run_ctx.run_dir / "02_klaerungsfragen.md").write_text("# Fragen", encoding="utf-8")
    (run_ctx.run_dir / "02_antworten.md").write_text("# Antworten", encoding="utf-8")
    monkeypatch.setattr(fc, "call_llm", lambda *a, **k: json.dumps({
        "has_gaps": False, "veto": False,
        "questions_markdown": "Keine offenen Belegbarkeitslücken.",
        "findings_markdown": "Keine Drift gefunden.",
    }))
    assert fc.run_factcheck(run_ctx) is False
    assert not (run_ctx.run_dir / "_factcheck_open_questions.md").exists()
