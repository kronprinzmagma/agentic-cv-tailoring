"""Writer-Trim: single-pass shortening of 04_final_de.md when page count > MAX_PAGES.

Called by pipeline_stages.run_page_trim_if_needed after the writer_loop produces
the first draft. The LLM returns the full CV with content cut — no new facts,
no structural changes. Safety: content is never lengthened (char-count guard).
"""
from __future__ import annotations

from pathlib import Path

from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

TRIM_PROMPT_PATH = Path("prompts/writer_trim.md")
MAX_TOKENS = 6144

# Allowed model for trim pass — same as writer (from config), but we default to
# claude-sonnet since trim requires judgement about what to cut.
_TRIM_AGENT = "writer"


def run_writer_trim(ctx: RunContext, pages: int) -> Path:
    """Shorten 04_final_de.md to fit 3 pages.

    Args:
        ctx: run context
        pages: current page count (for logging / prompt context)

    Returns:
        Path to the (potentially updated) 04_final_de.md.
    """
    final_path = ctx.run_dir / "04_final_de.md"
    if not final_path.exists():
        raise FileNotFoundError(f"04_final_de.md not found: {final_path}")

    cv_text = final_path.read_text(encoding="utf-8")
    original_len = len(cv_text)

    system_prompt = load_prompt(TRIM_PROMPT_PATH)
    user_msg = (
        f"Der CV hat aktuell **{pages} Seiten** — Ziel ist **3 Seiten**.\n\n"
        f"Bitte kürzen:\n\n{cv_text}"
    )

    log.info("writer_trim.start", run_id=ctx.run_id, pages=pages)
    content = call_llm(
        agent=_TRIM_AGENT,
        phase="phase3b_trim",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        snippet_text=cv_text[:300],
    ).strip()

    # Safety: trim must not lengthen the CV
    if len(content) >= original_len:
        log.warning(
            "writer_trim.not_shorter",
            run_id=ctx.run_id,
            original=original_len,
            result=len(content),
        )
        write_run_log_entry(ctx.run_dir, "writer_trim", "Trim-Pass hat nicht gekürzt — Original behalten")
        return final_path

    # Safety: the trim runs AFTER factcheck/consistency — a broken response
    # (empty, truncated, dropped section or station) must never replace the
    # validated CV. Deterministic check, fail-closed: keep the original.
    from cv_tailor.final_check import validate_trimmed_cv

    trim_ok, trim_issues = validate_trimmed_cv(cv_text, content)
    if not trim_ok:
        log.warning(
            "writer_trim.validation_failed",
            run_id=ctx.run_id,
            issues=trim_issues,
        )
        write_run_log_entry(
            ctx.run_dir,
            "writer_trim",
            "Trim-Ergebnis verworfen (Endprüfung): " + " | ".join(trim_issues),
        )
        return final_path

    final_path.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
    write_run_log_entry(
        ctx.run_dir,
        "writer_trim",
        f"CV gekürzt: {original_len} → {len(content)} Zeichen ({pages} → max 3 Seiten)",
    )
    log.info("writer_trim.done", run_id=ctx.run_id, original=original_len, trimmed=len(content))
    return final_path
