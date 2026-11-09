"""Pins the truncation guard on the factcheck agent.

Observed 2026-09-18 (Rittmeyer run): the initial factcheck hit max_tokens
(finish_reason=length), returned prose without a JSON object, and the
keyword fallback read the cut-off text as "no gaps". The clarification
round was silently skipped; the questions only surfaced later from the
coach. A truncated answer is not a verdict — it must be retried once with
a larger budget and, if still truncated, count as gaps / veto.
"""
import json

import pytest

from cv_tailor.agents import factcheck as fc
from cv_tailor.orchestrator import RunContext


@pytest.fixture
def run_ctx(tmp_path, monkeypatch):
    run_dir = tmp_path / "2026-09-18_testrun"
    run_dir.mkdir()
    (run_dir / "01_analyse.md").write_text("# Analyse\n\nKein Abgleich.", encoding="utf-8")
    (run_dir / "00_stellenanzeige.md").write_text("# Posting", encoding="utf-8")
    monkeypatch.setattr(fc, "get_beleg_index_compact", lambda *a, **k: "BELG-001: Demo")
    monkeypatch.setattr(fc, "format_clarifications_for_prompt", lambda *a, **k: "")
    monkeypatch.setattr(fc, "load_prompt", lambda *a, **k: "system prompt")
    monkeypatch.setattr(fc, "check_profile_fit", lambda *a, **k: [], raising=False)
    return RunContext(run_id=run_dir.name, run_dir=run_dir, started_at="2026-09-18T00:00:00Z")


class _FakeLLM:
    """Scripted call_llm: one (content, truncated) pair per call, records max_tokens."""

    def __init__(self, script):
        self.script = list(script)
        self.max_tokens_seen: list[int] = []

    def __call__(self, *args, **kwargs):
        self.max_tokens_seen.append(kwargs["max_tokens"])
        content, truncated = self.script.pop(0)
        meta_out = kwargs.get("meta_out")
        if meta_out is not None:
            meta_out.update(finish_reason="length" if truncated else "stop", truncated=truncated)
        return content


# Prose the keyword fallback would read as clean — exactly the Rittmeyer failure mode.
_TRUNCATED_CLEAN_PROSE = (
    "## Prüfung\n\nKeine strukturellen Vetos gefunden. Die Analyse ist weitgehend belegt, "
    "allerdings sollte zur GastroSaaS-Station noch geklärt werden, ob"
)
_CLEAN_JSON = json.dumps({"has_gaps": False, "veto": False, "questions_markdown": ""})


def test_truncated_then_complete_uses_retry_verdict(run_ctx, monkeypatch):
    llm = _FakeLLM([(_TRUNCATED_CLEAN_PROSE, True), (_CLEAN_JSON, False)])
    monkeypatch.setattr(fc, "call_llm", llm)
    assert fc.run_factcheck(run_ctx) is False
    assert llm.max_tokens_seen == [fc.MAX_TOKENS, fc.MAX_TOKENS_TRUNCATION_RETRY]


def test_truncated_twice_counts_as_gaps(run_ctx, monkeypatch):
    llm = _FakeLLM([(_TRUNCATED_CLEAN_PROSE, True), (_TRUNCATED_CLEAN_PROSE, True)])
    monkeypatch.setattr(fc, "call_llm", llm)
    assert fc.run_factcheck(run_ctx) is True
    questions = (run_ctx.run_dir / "02_klaerungsfragen.md").read_text(encoding="utf-8")
    assert "abgeschnitten" in questions
    assert "GastroSaaS" in questions  # Teilbefund bleibt sichtbar


def test_not_truncated_is_single_call(run_ctx, monkeypatch):
    llm = _FakeLLM([(_CLEAN_JSON, False)])
    monkeypatch.setattr(fc, "call_llm", llm)
    assert fc.run_factcheck(run_ctx) is False
    assert llm.max_tokens_seen == [fc.MAX_TOKENS]


def test_plain_string_mock_without_meta_out_still_works(run_ctx, monkeypatch):
    """Legacy test style: call_llm mocked as a bare lambda returning a string."""
    monkeypatch.setattr(fc, "call_llm", lambda *a, **k: _CLEAN_JSON)
    assert fc.run_factcheck(run_ctx) is False


def test_iteration_truncated_twice_is_veto(run_ctx, monkeypatch):
    monkeypatch.setattr("cv_tailor.prompt_context.build_gating_context", lambda ctx: "")
    monkeypatch.setattr("cv_tailor.companion_docs.load_companion_context", lambda d: None)
    monkeypatch.setattr("cv_tailor.companion_docs.format_for_prompt", lambda c, role: "")
    llm = _FakeLLM([(_TRUNCATED_CLEAN_PROSE, True), (_TRUNCATED_CLEAN_PROSE, True)])
    monkeypatch.setattr(fc, "call_llm", llm)
    veto = fc.run_factcheck_iteration(run_ctx, "berufserfahrung", "### Entwurf", round_num=1)
    assert veto is True
    findings = (run_ctx.run_dir / "03_iterationen" / "berufserfahrung_v1_factcheck.md").read_text(
        encoding="utf-8"
    )
    assert "abgeschnitten" in findings
    assert llm.max_tokens_seen == [fc.MAX_TOKENS_ITERATION, fc.MAX_TOKENS_TRUNCATION_RETRY]


def test_iteration_clean_after_retry_is_no_veto(run_ctx, monkeypatch):
    monkeypatch.setattr("cv_tailor.prompt_context.build_gating_context", lambda ctx: "")
    monkeypatch.setattr("cv_tailor.companion_docs.load_companion_context", lambda d: None)
    monkeypatch.setattr("cv_tailor.companion_docs.format_for_prompt", lambda c, role: "")
    clean_iter = json.dumps({"veto": False, "findings_markdown": "Keine Drift gefunden."})
    llm = _FakeLLM([(_TRUNCATED_CLEAN_PROSE, True), (clean_iter, False)])
    monkeypatch.setattr(fc, "call_llm", llm)
    assert fc.run_factcheck_iteration(run_ctx, "berufserfahrung", "### Entwurf", round_num=1) is False
