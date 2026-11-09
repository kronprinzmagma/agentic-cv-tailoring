"""Shared helpers for building prompt-context strings used by multiple agents.

Centralises a pattern that previously appeared in writer, factcheck and
coach with subtle drift (different string concatenations of posting +
analysis text). Drift between callers broke the Anthropic prompt cache —
each agent created a different cache key for the same logical inputs.

Now every caller goes through `build_gating_context(ctx)`, which returns a
deterministic string. Cache-key stability becomes a property of the
helper, not of caller discipline. The accompanying unit test asserts
idempotence.
"""
from __future__ import annotations

from cv_tailor.orchestrator import RunContext


def _read_or_empty(ctx: RunContext, name: str) -> str:
    """Read `ctx.run_dir / name` or return empty string if absent."""
    p = ctx.run_dir / name
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def build_gating_context(ctx: RunContext) -> str:
    """Return the canonical topic-gating context for the current run.

    The posting (00_stellenanzeige.md) only. Until 2026-10-23 the analysis
    was appended as well; but the analysis maps the posting onto Alex'
    stations and therefore names MediaCorp, HealthApp and GastroSaaS in every run,
    which switched on the media/health/gastro topics regardless of the role
    and let every clarification through. CLAUDE.md states the intent: a past
    answer may only be used when the *posting* activates the same topic.

    Deterministic: same RunContext → same byte sequence, so writer,
    factcheck and coach keep identical Anthropic cache keys. Empty when the
    posting is missing (the clarifications loader treats "" as "no filter").
    """
    return _read_or_empty(ctx, "00_stellenanzeige.md")
