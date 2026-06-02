"""Keyword-Marker agent: post-hoc bolds key matching terms in the final CV.

Runs after writer_loop produces 04_final_de.md. The writer no longer marks
keywords itself — it focuses on substance and natural voice. This agent
adds **bold** markings on a finished CV, so the keyword-thinking does not
contaminate the drafting phase.

Architecture: the LLM returns a JSON list of exact phrases to bold — it does
NOT return the full marked CV text. The Python layer applies the bolds
deterministically, guaranteeing zero text drift.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

KEYWORD_MARKER_PROMPT_PATH = Path("prompts/keyword_marker.md")
MAX_TOKENS = 2048


def _split_preamble(cv_text: str) -> tuple[str, str]:
    """Split a final CV into a metadata preamble and the markable body.

    The preamble is everything before the first `## ` section heading —
    i.e. the `# Finaler CV (DE)` title, `**Run:**`, `**Erstellt:**` lines.
    These are pipeline metadata that should not be sent to the model.

    Returns (preamble, body). Both may be empty strings.
    """
    lines = cv_text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("## "):
            preamble = "".join(lines[:i])
            body = "".join(lines[i:])
            return preamble, body
    return "", cv_text


def _parse_phrase_response(raw: str) -> dict[str, object]:
    """Parse the JSON phrase list from the model response.

    Returns a dict with 'summary' (list[str]) and 'stations' (dict[str, list[str]]).
    Falls back to empty structure on parse error.
    """
    text = raw.strip()
    # Strip optional code fence
    if text.startswith("```"):
        lines = text.splitlines()
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip().startswith("```")), None)
        text = "\n".join(lines[1:end]) if end else "\n".join(lines[1:])
    try:
        data = json.loads(text)
        summary = [str(p) for p in data.get("summary", []) if p]
        stations: dict[str, list[str]] = {}
        for key, phrases in (data.get("stations") or {}).items():
            if isinstance(phrases, list):
                stations[str(key)] = [str(p) for p in phrases if p]
        return {"summary": summary, "stations": stations}
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        log.warning("keyword_marker.parse_error", error=str(exc))
        return {"summary": [], "stations": {}}


def _bold_phrase_in_text(text: str, phrase: str, used: set[str]) -> tuple[str, bool]:
    """Bold the first occurrence of `phrase` in `text` outside headers.

    Returns (modified_text, applied). Does not modify already-bolded spans or
    header lines (lines starting with #). Case-sensitive match only.
    """
    if phrase in used:
        return text, False
    idx = text.find(phrase)
    if idx == -1:
        return text, False
    # Check the phrase is not inside a header line
    line_start = text.rfind("\n", 0, idx) + 1
    line_end_nl = text.find("\n", idx)
    line_end = line_end_nl if line_end_nl != -1 else len(text)
    if text[line_start:line_end].lstrip().startswith("#"):
        return text, False
    # Check not already inside bold markers
    before_2 = text[max(0, idx - 2):idx]
    after_2 = text[idx + len(phrase):idx + len(phrase) + 2]
    if before_2.endswith("**") or after_2.startswith("**"):
        return text, False
    result = text[:idx] + f"**{phrase}**" + text[idx + len(phrase):]
    used.add(phrase)
    return result, True


def _apply_bolds(cv_body: str, phrases: dict[str, object]) -> tuple[str, dict]:
    """Apply bold markers from the phrase dict to cv_body.

    Enforces:
    - Summary: max 4 bolds
    - Each station: max 4 bolds
    - Never the same phrase twice globally

    Returns (marked_body, stats).
    """
    used_globally: set[str] = set()
    stats: dict[str, object] = {"summary_bolds": 0, "stations": {}}

    # --- Summary ---
    summary_phrases = (phrases.get("summary") or [])[:4]
    summary_applied = 0
    for phrase in summary_phrases:
        if not phrase:
            continue
        cv_body, applied = _bold_phrase_in_text(cv_body, phrase, used_globally)
        if applied:
            summary_applied += 1
    stats["summary_bolds"] = summary_applied

    # --- Berufserfahrung stations ---
    station_map: dict[str, list[str]] = phrases.get("stations") or {}
    for station_heading, station_phrases in station_map.items():
        applied_count = 0
        for phrase in (station_phrases or [])[:4]:
            if not phrase:
                continue
            cv_body, applied = _bold_phrase_in_text(cv_body, phrase, used_globally)
            if applied:
                applied_count += 1
        stats["stations"][station_heading[:60]] = applied_count

    return cv_body, stats


def run_keyword_marker(
    ctx: RunContext,
    final_cv_path: Path | None = None,
    analyse_path: Path | None = None,
    prompt_path: Path = KEYWORD_MARKER_PROMPT_PATH,
) -> Path:
    """Mark keywords in the final CV. Returns the path of the updated file.

    The LLM returns a JSON list of exact phrases to bold. Python applies
    them deterministically — zero text drift is possible.
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

    preamble, cv_body = _split_preamble(cv_text)

    system_prompt = load_prompt(prompt_path)
    user_msg = (
        f"## CV\n{cv_body}\n\n"
        f"## Stellenanzeige (Vokabular-Quelle)\n{posting_text}\n\n"
        f"## Analyse (Hebel und Vokabular-Hinweise)\n{analyse_text}"
    )

    log.info("keyword_marker.start", run_id=ctx.run_id)
    raw = call_llm(
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

    phrases = _parse_phrase_response(raw)
    total_phrases = len(phrases.get("summary") or []) + sum(
        len(v) for v in (phrases.get("stations") or {}).values()
    )

    if total_phrases == 0:
        log.warning("keyword_marker.no_phrases", run_id=ctx.run_id)
        write_run_log_entry(ctx.run_dir, "keyword_marker", "Keine Phrasen vom Modell — Original behalten")
        return final_cv_path

    marked_body, stats = _apply_bolds(cv_body, phrases)

    full_content = preamble + marked_body
    final_cv_path.write_text(
        full_content + ("" if full_content.endswith("\n") else "\n"),
        encoding="utf-8",
    )
    write_run_log_entry(ctx.run_dir, "keyword_marker", f"Keywords markiert in {final_cv_path.name}")
    log.info("keyword_marker.done", run_id=ctx.run_id, chars=len(marked_body), stats=stats)

    _audit_bold_distribution(ctx, marked_body)
    return final_cv_path


def _audit_bold_distribution(ctx: RunContext, content: str) -> None:
    """Count bolds per Berufserfahrung station and log if uneven."""
    section_match = re.search(
        r"##\s+(?:Berufserfahrung|Professional Experience|Work Experience)(.*?)(?=^##\s|\Z)",
        content, flags=re.S | re.M | re.I,
    )
    if not section_match:
        return
    section = section_match.group(1)
    stations = re.split(r"^###\s+", section, flags=re.M)
    if len(stations) <= 1:
        return
    distribution: list[tuple[str, int]] = []
    for chunk in stations[1:]:
        header_line = chunk.splitlines()[0].strip() if chunk.strip() else ""
        body = "\n".join(chunk.splitlines()[1:])
        body_bolds = len(re.findall(r"\*\*[^*]+\*\*", body))
        distribution.append((header_line[:60], body_bolds))
    counts = [c for _, c in distribution]
    if not counts:
        return
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
