"""Analyst agent: reads Stellenanzeige + Standard-CV + Zeugnisse + prompts/analyst.md, calls LLM, writes 01_analyse.md."""
from __future__ import annotations

import json
from pathlib import Path

from cv_tailor.beleg_index import get_beleg_index_compact, load_beleg_index
from cv_tailor.clarifications import format_clarifications_for_prompt
from cv_tailor.experience_activation import format_activation_markdown
from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

ANALYST_PROMPT_PATH = Path("prompts/analyst.md")
BELEG_INDEX_PATH = Path("data/beleg_index.json")
CV_PATH = Path("data/standard_cv.md")
MAX_TOKENS = 8192


def run_analyst(
    ctx: RunContext,
    beleg_index_path: Path = BELEG_INDEX_PATH,
    cv_path: Path = CV_PATH,
    prompt_path: Path = ANALYST_PROMPT_PATH,
) -> Path:
    """Run the analyst agent and write 01_analyse.md to the run directory.

    Returns the path to the written 01_analyse.md file.
    """
    system_prompt = load_prompt(prompt_path)

    stellenanzeige_text = (ctx.run_dir / "00_stellenanzeige.md").read_text(encoding="utf-8")

    cv_text = (
        cv_path.read_text(encoding="utf-8")
        if cv_path.exists()
        else "(Standard-CV nicht gefunden)"
    )
    beleg_index_compact = get_beleg_index_compact(beleg_index_path)
    if beleg_index_path.exists():
        beleg_index_data = load_beleg_index(beleg_index_path)
        activation_map = format_activation_markdown(stellenanzeige_text, beleg_index_data)
    else:
        activation_map = "(Experience Activation nicht verfügbar — Beleg-Index fehlt)"

    activation_path = ctx.run_dir / "_experience_activation.md"
    activation_path.write_text(activation_map, encoding="utf-8")

    # Topic-gate clarifications against the current posting + activation
    # so cross-context past answers (e.g. "Praxisassistent:innen as analytics
    # tool users") don't surface during runs that don't activate that topic.
    clarifications_text = format_clarifications_for_prompt(
        current_context=stellenanzeige_text + "\n" + activation_map,
    )

    user_msg = (
        f"## Stellenanzeige\n{stellenanzeige_text}\n\n"
        f"## Experience Activation Map\n{activation_map}\n\n"
        f"## Zusatzkontext aus früheren Klärungen\n{clarifications_text or '(keine früheren Klärungen)'}\n\n"
        f"## Standard-CV\n{cv_text}\n\n"
        f"## Beleg-Index (vollständig, kompakt)\n{beleg_index_compact}"
    )

    log.info("run_analyst.start", run_id=ctx.run_id)
    content = call_llm(
        agent="analyst",
        phase="phase3_analyse",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        snippet_text=stellenanzeige_text[:500],
    )

    analyse_path = ctx.run_dir / "01_analyse.md"
    analyse_path.write_text(content, encoding="utf-8")
    write_run_log_entry(ctx.run_dir, "analyst", f"01_analyse.md geschrieben ({len(content)} Zeichen)")
    log.info("run_analyst.done", run_id=ctx.run_id, chars=len(content))
    return analyse_path
