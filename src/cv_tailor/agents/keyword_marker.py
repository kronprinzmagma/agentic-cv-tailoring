"""Keyword-Marker agent: post-hoc bolds key matching terms in the final CV.

Runs after writer_loop produces 04_final_de.md. The writer no longer marks
keywords itself — it focuses on substance and natural voice. This agent
adds **bold** markings on a finished CV, so the keyword-thinking does not
contaminate the drafting phase.

The agent reads the final CV plus the analyst output (which has the
job-posting vocabulary section) and returns the CV with up to 4 bold
markings in the Summary and 3–4 per Berufserfahrung station, ideally
one per bullet. Schlüsselkompetenzen headlines are already bold and are
not touched.
"""
from __future__ import annotations

from pathlib import Path

from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

KEYWORD_MARKER_PROMPT_PATH = Path("prompts/keyword_marker.md")
MAX_TOKENS = 4096


def _split_preamble(cv_text: str) -> tuple[str, str]:
    """Split a final CV into a metadata preamble and the markable body.

    The preamble is everything before the first `## ` section heading —
    i.e. the `# Finaler CV (DE)` title, `**Run:**`, `**Erstellt:**` lines.
    These are pipeline metadata that should not be sent to the model and
    should never be used in the content-drift comparison.

    Returns (preamble, body).  Both may be empty strings.
    """
    lines = cv_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("## "):
            preamble = "".join(lines[:i])
            body = "".join(lines[i:])
            return preamble, body
    return "", cv_text


def _strip_text_for_compare(text: str) -> str:
    """Normalise text so we can verify the agent didn't change content.

    IN-05: strict comparison — drop all `**`, then keep only word
    characters (letters/digits) so punctuation drift (extra comma, missing
    period) shows up as a difference. Trivial whitespace tweaks are
    still tolerated. Aggressive: catches more drift, may also flag
    benign Unicode-quote-style edits (rare in CV text).
    """
    import re as _re

    stripped = text.replace("**", "")
    # Keep only word chars (Unicode letters/digits) and spaces, normalise spaces
    word_only = _re.sub(r"[^\w\s]", " ", stripped, flags=_re.UNICODE)
    return _re.sub(r"\s+", " ", word_only).strip().lower()


def run_keyword_marker(
    ctx: RunContext,
    final_cv_path: Path | None = None,
    analyse_path: Path | None = None,
    prompt_path: Path = KEYWORD_MARKER_PROMPT_PATH,
) -> Path:
    """Mark keywords in the final CV. Returns the path of the updated file.

    If the agent's output deviates from the input in any way other than
    added `**...**` markings, falls back to the original CV (logged warning).
    This guards against the model inadvertently rewriting prose.
    """
    if final_cv_path is None:
        final_cv_path = ctx.run_dir / "04_final_de.md"
    if not final_cv_path.exists():
        raise FileNotFoundError(f"Final CV not found: {final_cv_path}")

    cv_text = final_cv_path.read_text(encoding="utf-8")

    if analyse_path is None:
        analyse_path = ctx.run_dir / "01_analyse.md"
    analyse_text = analyse_path.read_text(encoding="utf-8") if analyse_path.exists() else ""

    posting_path = ctx.run_dir / "00_stellenanzeige.md"
    posting_text = posting_path.read_text(encoding="utf-8") if posting_path.exists() else ""

    # Split off the pipeline-metadata preamble (# title, **Run:**, **Erstellt:**)
    # so the model only sees and returns the markable CV sections. The preamble
    # is prepended back after the drift check.
    preamble, cv_body = _split_preamble(cv_text)

    system_prompt = load_prompt(prompt_path)

    user_msg = (
        f"## CV (zu markieren)\n{cv_body}\n\n"
        f"## Stellenanzeige (Vokabular-Quelle)\n{posting_text}\n\n"
        f"## Analyse (Hebel und Vokabular-Hinweise)\n{analyse_text}"
    )

    log.info("keyword_marker.start", run_id=ctx.run_id)
    content = call_llm(
        agent="keyword_marker",
        phase="phase5_keyword_marker",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        snippet_text=cv_body[:500],
    ).strip()

    # Safety check: stripping `**` from both versions must yield the same text.
    # Compare only the body, not the metadata preamble.
    original_stripped = _strip_text_for_compare(cv_body).strip()
    marked_stripped = _strip_text_for_compare(content).strip()
    if original_stripped != marked_stripped:
        log.warning(
            "keyword_marker.content_drift",
            run_id=ctx.run_id,
            orig_len=len(original_stripped),
            marked_len=len(marked_stripped),
        )
        write_run_log_entry(
            ctx.run_dir,
            "keyword_marker",
            "Inhalts-Drift im Marker-Output erkannt — Original behalten",
        )
        return final_cv_path

    full_content = preamble + content
    final_cv_path.write_text(full_content + ("\n" if not full_content.endswith("\n") else ""), encoding="utf-8")
    write_run_log_entry(
        ctx.run_dir,
        "keyword_marker",
        f"Keywords markiert in {final_cv_path.name}",
    )
    log.info("keyword_marker.done", run_id=ctx.run_id, chars=len(content))

    # Observability: how is the bold distribution across Berufserfahrung
    # stations? Prompt asks for "exactly 1 per station" — we measure but
    # don't auto-correct (would override the LLM's judgement). Visible in
    # log so the user can spot pattern violations across runs.
    _audit_bold_distribution(ctx, content)
    return final_cv_path


def _audit_bold_distribution(ctx: RunContext, content: str) -> None:
    """Count bolds per Berufserfahrung station and log if uneven."""
    import re as _re
    # Find the Berufserfahrung / Professional Experience block
    section_match = _re.search(
        r"##\s+(?:Berufserfahrung|Professional Experience|Work Experience)(.*?)(?=^##\s|\Z)",
        content, flags=_re.S | _re.M | _re.I,
    )
    if not section_match:
        return
    section = section_match.group(1)
    # Split on `### ` station headers
    stations = _re.split(r"^###\s+", section, flags=_re.M)
    if len(stations) <= 1:
        return
    # First chunk before the first ### is intro; skip it
    distribution: list[tuple[str, int]] = []
    for chunk in stations[1:]:
        header_line = chunk.splitlines()[0].strip() if chunk.strip() else ""
        body = "\n".join(chunk.splitlines()[1:])
        # Subtract any bolds the writer kept inside the station header
        # (rare — bold inside `###` is unusual but possible)
        body_bolds = len(_re.findall(r"\*\*[^*]+\*\*", body))
        distribution.append((header_line[:60], body_bolds))
    counts = [c for _, c in distribution]
    if not counts:
        return
    # Target: 3–4 bolds per Berufserfahrungs-Station. Tolerate the boundary
    # values inclusive — only warn when stations fall outside [3, 4].
    if any(c < 3 or c > 4 for c in counts):
        log.warning(
            "keyword_marker.bold_distribution_uneven",
            run_id=ctx.run_id,
            stations=distribution,
            target_range=(3, 4),
        )
        write_run_log_entry(
            ctx.run_dir,
            "keyword_marker",
            f"Bold-Verteilung ungleichmässig: {counts} (Ziel: 3–4 pro Station)",
        )
    else:
        log.info("keyword_marker.bold_distribution_ok", run_id=ctx.run_id, stations=len(counts))
