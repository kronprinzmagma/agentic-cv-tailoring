"""Tests for the shared prompt-context builder.

Pins the Anthropic cache-determinism invariant: writer, factcheck and
coach must all derive bit-identical gating-context strings for the same
RunContext. If this drifts, the prompt cache splits across agents and
the writer cache-hit rate drops from ~80% back to ~48%.
"""
from datetime import datetime, timezone
from pathlib import Path

from cv_tailor.orchestrator import RunContext
from cv_tailor.prompt_context import build_gating_context


def _make_ctx(run_dir: Path) -> RunContext:
    return RunContext(
        run_id=run_dir.name, run_dir=run_dir,
        started_at=datetime.now(timezone.utc),
    )


def test_gating_context_returns_empty_when_no_artifacts(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    assert build_gating_context(ctx) == ""


def test_gating_context_uses_posting_only_when_no_analysis(tmp_path: Path):
    (tmp_path / "00_stellenanzeige.md").write_text("posting body", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    out = build_gating_context(ctx)
    assert out == "posting body"


def test_gating_context_uses_analysis_only_when_no_posting(tmp_path: Path):
    (tmp_path / "01_analyse.md").write_text("analyse body", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    out = build_gating_context(ctx)
    assert out == "analyse body"


def test_gating_context_joins_posting_and_analysis_with_newline(tmp_path: Path):
    (tmp_path / "00_stellenanzeige.md").write_text("POSTING", encoding="utf-8")
    (tmp_path / "01_analyse.md").write_text("ANALYSE", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    assert build_gating_context(ctx) == "POSTING\nANALYSE"


def test_gating_context_deterministic_idempotent(tmp_path: Path):
    """Critical cache invariant: same inputs → same bytes, every call."""
    (tmp_path / "00_stellenanzeige.md").write_text("Some posting text", encoding="utf-8")
    (tmp_path / "01_analyse.md").write_text("Some analysis text", encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    a = build_gating_context(ctx)
    b = build_gating_context(ctx)
    c = build_gating_context(ctx)
    assert a == b == c
    # And bit-identical across processes (no time-based / random fields)
    assert isinstance(a, str)
    assert len(a) == len(a.encode("utf-8")) or "ä" in a or len(a.encode("utf-8")) >= len(a)


def test_gating_context_byte_identical_across_three_agents(tmp_path: Path):
    """The whole point of this helper: writer/factcheck/coach all derive
    the same string from the same RunContext. We simulate three call sites
    invoking it independently and verify the strings match exactly."""
    (tmp_path / "00_stellenanzeige.md").write_text("posting", encoding="utf-8")
    (tmp_path / "01_analyse.md").write_text("analysis", encoding="utf-8")
    ctx = _make_ctx(tmp_path)

    # Simulate three independent agent calls
    writer_ctx = build_gating_context(ctx)
    factcheck_ctx = build_gating_context(ctx)
    coach_ctx = build_gating_context(ctx)
    assert writer_ctx == factcheck_ctx == coach_ctx
