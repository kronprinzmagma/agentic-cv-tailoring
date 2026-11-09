"""Hiring-Manager-Reviewer agent: reviews a drafted CV section from a hiring manager perspective. Uses OpenAI gpt-4.1 (anti-echo — different provider from writer)."""
from __future__ import annotations

from pathlib import Path

from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

HIRING_REVIEWER_PROMPT_PATH = Path("prompts/hiring_manager_reviewer.md")
MAX_TOKENS = 2048
PLACEHOLDER_MARKER = "USER FÜLLT"


def run_hiring_reviewer(
    ctx: RunContext,
    section: str,
    section_text: str,
    round_num: int,
    prompt_path: Path = HIRING_REVIEWER_PROMPT_PATH,
) -> str:
    """Review a CV section draft from a hiring manager perspective.

    Writes review to 03_iterationen/<section>_v<round_num>_review_hiring.md.
    Returns the review text.
    """
    system_prompt = load_prompt(prompt_path)
    if PLACEHOLDER_MARKER in system_prompt:
        log.warning("run_hiring_reviewer.placeholder_prompt", section=section)
    analyse_path = ctx.run_dir / "01_analyse.md"
    analyse_text = analyse_path.read_text(encoding="utf-8") if analyse_path.exists() else ""
    user_msg = (
        f"## Stellenanalyse\n{analyse_text}\n\n"
        f"## Abschnitt: {section}\n"
        f"## Entwurf (Runde {round_num})\n{section_text}"
    )
    content = call_llm(
        agent="hiring_reviewer",
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
    out_path = iter_dir / f"{section}_v{round_num}_review_hiring.md"
    out_path.write_text(content, encoding="utf-8")
    write_run_log_entry(ctx.run_dir, "hiring_reviewer", f"{section}_v{round_num}_review_hiring.md geschrieben")
    log.info("run_hiring_reviewer.done", run_id=ctx.run_id, section=section, round_num=round_num)
    return content
