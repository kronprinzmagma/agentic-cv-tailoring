"""Factcheck verdict integrity (review 2026-10-23, R01 and R02).

R01: an explicit `veto: true` was erased when the findings also contained a
clean marker for another claim. R02: an empty answer, `{}` or prose without
verdict language counted as «no problem».
"""
import json

import pytest

from cv_tailor.agents import factcheck as fc
from tests.test_factcheck_truncation import _FakeLLM, run_ctx  # noqa: F401


@pytest.fixture
def iteration_env(monkeypatch):
    monkeypatch.setattr("cv_tailor.prompt_context.build_gating_context", lambda ctx: "")
    monkeypatch.setattr("cv_tailor.companion_docs.load_companion_context", lambda d: None)
    monkeypatch.setattr("cv_tailor.companion_docs.format_for_prompt", lambda c, role: "")


def test_mixed_findings_keep_explicit_veto(run_ctx, iteration_env, monkeypatch):  # noqa: F811
    mixed = json.dumps({
        "veto": True,
        "findings_markdown": "Summary: Keine Drift gefunden.\n✗ Teamgrösse 99 ist nicht belegt. Veto.",
    })
    monkeypatch.setattr(fc, "call_llm", _FakeLLM([(mixed, False)]))
    assert fc.run_factcheck_iteration(run_ctx, "berufserfahrung", "### Entwurf", round_num=1) is True


def test_clean_override_still_works_without_failure_signal() -> None:
    assert fc._details_are_clean("Keine Drift gefunden. Alle Claims belegt.") is True
    assert fc._details_are_clean("Keine Drift gefunden.\n✗ Zahl 99 nicht belegt.") is False


@pytest.mark.parametrize("answer", ["", "{}", "Prüfung konnte nicht durchgeführt werden."])
def test_missing_verdict_is_veto_after_one_retry(run_ctx, iteration_env, monkeypatch, answer):  # noqa: F811
    llm = _FakeLLM([(answer, False), (answer, False)])
    monkeypatch.setattr(fc, "call_llm", llm)
    assert fc.run_factcheck_iteration(run_ctx, "berufserfahrung", "### Entwurf", round_num=1) is True
    assert len(llm.max_tokens_seen) == 2
    findings = (run_ctx.run_dir / "03_iterationen" / "berufserfahrung_v1_factcheck.md").read_text(
        encoding="utf-8"
    )
    assert "kein auswertbares Urteil" in findings


def test_missing_verdict_then_valid_uses_retry(run_ctx, iteration_env, monkeypatch):  # noqa: F811
    clean = json.dumps({"veto": False, "findings_markdown": "Keine Drift gefunden."})
    monkeypatch.setattr(fc, "call_llm", _FakeLLM([("{}", False), (clean, False)]))
    assert fc.run_factcheck_iteration(run_ctx, "berufserfahrung", "### Entwurf", round_num=1) is False


def test_initial_factcheck_without_verdict_pauses(run_ctx, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fc, "call_llm", _FakeLLM([("", False), ("", False)]))
    assert fc.run_factcheck(run_ctx) is True
    questions = (run_ctx.run_dir / "02_klaerungsfragen.md").read_text(encoding="utf-8")
    assert "kein auswertbares Urteil" in questions
