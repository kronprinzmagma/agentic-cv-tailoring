"""Career-Coach-Reviewer agent: reviews a drafted CV section from a career
coaching perspective AND from the perspective of someone who knows Alex'ss
actual profile (Standard-CV + Beleg-Index + prior clarifications).

Uses Anthropic claude-sonnet-4-6. Profile context is sent as a cached
prefix so the per-section calls reuse it cheaply.
"""
from __future__ import annotations

from pathlib import Path

from cv_tailor.beleg_index import get_beleg_index_compact
from cv_tailor.clarifications import format_clarifications_for_prompt
from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

COACH_REVIEWER_PROMPT_PATH = Path("prompts/coach_reviewer.md")
STANDARD_CV_PATH = Path("data/standard_cv.md")
BELEG_INDEX_PATH = Path("data/beleg_index.json")
MAX_TOKENS = 4096
PLACEHOLDER_MARKER = "USER FÜLLT"


def run_coach_reviewer(
    ctx: RunContext,
    section: str,
    section_text: str,
    round_num: int,
    prompt_path: Path = COACH_REVIEWER_PROMPT_PATH,
    standard_cv_path: Path = STANDARD_CV_PATH,
    beleg_index_path: Path = BELEG_INDEX_PATH,
) -> str:
    """Review a CV section draft from a career coach perspective.

    Coach sees:
      - The Standard-CV (Alex'ss canonical seniority and role definitions)
      - The Beleg-Index (what is provable and at what scope)
      - Prior clarifications (Q&A memory, may be empty)
      - The section text to evaluate

    These four blocks form the user message; the first three are wrapped in
    `cache_control: ephemeral` so the per-section calls within one run reuse
    the prefix and only pay for the small section-specific tail.

    Writes review to 03_iterationen/<section>_v<round_num>_review_coach.md.
    Returns the review text.
    """
    system_prompt = load_prompt(prompt_path)
    if PLACEHOLDER_MARKER in system_prompt:
        log.warning("run_coach_reviewer.placeholder_prompt", section=section)

    standard_cv = (
        standard_cv_path.read_text(encoding="utf-8")
        if standard_cv_path.exists()
        else "(Standard-CV nicht gefunden)"
    )
    beleg_index = get_beleg_index_compact(beleg_index_path)
    posting_path = ctx.run_dir / "00_stellenanzeige.md"
    posting_text = (
        posting_path.read_text(encoding="utf-8")
        if posting_path.exists()
        else "(Stellenanzeige nicht gefunden)"
    )
    # Topic-gate clarifications via the shared helper so writer, factcheck
    # and coach derive identical Anthropic cache keys for the same inputs.
    from cv_tailor.prompt_context import build_gating_context
    clarifications = format_clarifications_for_prompt(
        current_context=build_gating_context(ctx) or None,
    )

    static_parts = [
        f"## Stellenanzeige (was die Rolle fordert)\n{posting_text}",
        f"## Standard-CV (Alex' Profil — authoritative für Rolle, Seniorität, Tonfall)\n{standard_cv}",
        f"## Beleg-Index (was ist mit welcher Scope belegt)\n{beleg_index}",
    ]
    if clarifications:
        static_parts.append(
            f"## Frühere Klärungsantworten\n{clarifications}"
        )

    dynamic_parts = [
        f"## Abschnitt: {section}",
        f"## Entwurf (Runde {round_num})\n{section_text}",
    ]

    user_msg = [
        {
            "type": "text",
            "text": "\n\n".join(static_parts),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "\n\n".join(dynamic_parts),
        },
    ]

    content = call_llm(
        agent="coach_reviewer",
        phase="phase4_review",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        iteration=round_num,
        snippet_text=section_text[:500],
    )
    iter_dir = ctx.run_dir / "03_iterationen"
    iter_dir.mkdir(exist_ok=True)
    out_path = iter_dir / f"{section}_v{round_num}_review_coach.md"
    out_path.write_text(content, encoding="utf-8")
    write_run_log_entry(ctx.run_dir, "coach_reviewer", f"{section}_v{round_num}_review_coach.md geschrieben")
    log.info("run_coach_reviewer.done", run_id=ctx.run_id, section=section, round_num=round_num)
    return content
