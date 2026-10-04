"""Shared helpers for building prompt-context strings used by multiple agents.

Centralises two patterns that previously appeared in writer, factcheck and
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

    Combines the posting (00_stellenanzeige.md) and analysis (01_analyse.md)
    into one string used by `clarifications.format_clarifications_for_prompt`
    to decide which past Q/As are topic-relevant.

    Deterministic: same RunContext → same byte sequence. Empty if both
    inputs are missing (returns "" which the clarifications loader treats
    as "no filter"). Order is fixed: posting first, then analysis,
    separated by a single newline.
    """
    posting = _read_or_empty(ctx, "00_stellenanzeige.md")
    analyse = _read_or_empty(ctx, "01_analyse.md")
    if not posting and not analyse:
        return ""
    return f"{posting}\n{analyse}" if posting and analyse else (posting or analyse)
