"""Diff agent: compares 04_final_de.md with data/standard_cv.md and writes a compact diff table to 05_diff.md."""
from __future__ import annotations

import re
from pathlib import Path

from cv_tailor.llm import call_llm
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

DIFF_PROMPT_PATH = Path("prompts/diff.md")
STANDARD_CV_PATH = Path("data/standard_cv.md")
MAX_TABLE_ROWS = 55
MAX_DIFF_CHARS = 6500
MAX_TOKENS = 8192
PLACEHOLDER_MARKER = "USER FÜLLT"


def _count_table_rows(text: str) -> int:
    rows = 0
    separator_re = re.compile(r"^\|[-| :]+\|$")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if "Abschnitt" in stripped:
            continue
        if separator_re.match(stripped):
            continue
        rows += 1
    return rows


def run_diff_agent(
    ctx: RunContext,
    final_cv_path: Path | None = None,
    standard_cv_path: Path = STANDARD_CV_PATH,
    prompt_path: Path = DIFF_PROMPT_PATH,
) -> Path:
    """Run the diff agent and write 05_diff.md to the run directory."""
    if not prompt_path.exists():
        raise FileNotFoundError(f"Diff prompt not found: {prompt_path}")
    system_prompt = prompt_path.read_text(encoding="utf-8")
    if PLACEHOLDER_MARKER in system_prompt:
        log.warning("run_diff_agent.placeholder_prompt")

    if final_cv_path is None:
        final_cv_path = ctx.run_dir / "04_final_de.md"
    if not final_cv_path.exists():
        raise FileNotFoundError(f"Final CV not found: {final_cv_path}")
    if not standard_cv_path.exists():
        raise FileNotFoundError(f"Standard-CV not found: {standard_cv_path}")

    final_cv_text = final_cv_path.read_text(encoding="utf-8")
    standard_cv_text = standard_cv_path.read_text(encoding="utf-8")
    user_msg = f"## Original-CV\n{standard_cv_text}\n\n## Final-CV\n{final_cv_text}"

    log.info("run_diff_agent.start", run_id=ctx.run_id)
    content = call_llm(
        agent="diff",
        phase="phase5_diff",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        snippet_text=final_cv_text[:500],
    ).strip()

    row_count = _count_table_rows(content)
    if row_count > MAX_TABLE_ROWS or len(content) > MAX_DIFF_CHARS:
        log.warning("diff_agent.oversized", rows=row_count, threshold=MAX_TABLE_ROWS)
        write_run_log_entry(
            ctx.run_dir,
            "diff",
            f"Tabelle hat {row_count} Zeilen / {len(content)} Zeichen — Konsolidierung erzwungen",
        )
        consolidation_msg = (
            f"Die folgende Diff-Tabelle hat {row_count} Zeilen und {len(content)} Zeichen. "
            f"Konsolidiere sie auf maximal {MAX_TABLE_ROWS} Tabellenzeilen und maximal "
            f"{MAX_DIFF_CHARS} Zeichen. Behalte strukturelle Änderungen, "
            "Hinzufügungen und Entfernungen. Gib nur die Tabelle aus.\n\n"
            f"## Ursprüngliche Tabelle\n{content}"
        )
        content = call_llm(
            agent="diff",
            phase="phase5_diff",
            run_id=ctx.run_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": consolidation_msg},
            ],
            max_tokens=MAX_TOKENS,
            iteration=1,
            snippet_text=content[:500],
        ).strip()

    out_path = ctx.run_dir / "05_diff.md"
    out_path.write_text(content + "\n", encoding="utf-8")
    write_run_log_entry(ctx.run_dir, "diff", f"05_diff.md geschrieben ({len(content)} Zeichen)")
    log.info("diff_agent.done", run_id=ctx.run_id, chars=len(content))
    return out_path
