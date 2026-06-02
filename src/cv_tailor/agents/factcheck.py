"""Factcheck agent: validates analysis and section drafts against the Beleg-Index."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cv_tailor.beleg_index import get_beleg_index_compact
from cv_tailor.clarifications import format_clarifications_for_prompt
from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

FACTCHECK_PROMPT_PATH = Path("prompts/factcheck.md")
BELEG_INDEX_PATH = Path("data/beleg_index.json")
MAX_TOKENS = 2048        # initial factcheck (question-only, short output)
MAX_TOKENS_ITERATION = 4096  # per-section drift check — full berufserfahrung needs ~2500 tokens

# Keyword fallback patterns — only triggered when JSON parsing fails.
# Match phrases that signal real gaps; explicitly negative contexts
# ("Keine Lücke", "ohne Überzeichnung") should not count.
GAP_KEYWORDS = [
    "nicht belegt",
    "kein beleg",
    "fehlender beleg",
    "echte lücke",
    "kritische lücke",
    "klärung erforderlich",
    "klärung nötig",
]
# Negation patterns that, if they precede a gap keyword in the same line,
# should suppress the false-positive (e.g. "Keine offenen Lücken").
_NEGATED_GAP_PATTERNS = [
    "keine offenen",
    "keine kritischen",
    "keine drift",
    "keine sachliche drift",
    "keine lücke",
    "keine relevanten",
    "keine signifikante",
    "alle belegt",
]
# Top-level verdict phrases that, if present anywhere in the response,
# override line-level keyword matches. The factcheck model often issues
# a global "no veto" verdict in a heading and then discusses individual
# clarification points further down — the discussion text may contain
# gap keywords ("Titel X nicht belegt") that are NOT classified as veto
# by the model itself. We trust the global verdict.
_GLOBAL_NO_VETO_VERDICTS = [
    "keine strukturellen vetos",
    "keine sachliche drift gefunden",
    "kein veto",
    "alle bullets sind in den belegen vorhanden",
]
CLEAN_FINDING_MARKERS = [
    "keine drift",
    "keine kritischen drift",
    "keine sachliche drift",
    "keine drift gefunden",
    "keine strukturellen vetos",
    "keine strukturelle drift",
    "alle zentralen claims sind belegt",
    "alle bullets sind in den belegen",
    "alle behauptungen belegt",
    "keine überzeichnung",
    "keine signifikante",
    "keine relevante",
    "substanz ist belegt",
    "alles belegt",
    "alle claims belegt",
]


def _extract_json_object(text: str) -> dict:
    """Parse a JSON object from a model response, tolerating markdown fences."""
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`").strip()
        if content.lower().startswith("json"):
            content = content[4:].lstrip()
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Factcheck response did not contain a JSON object")
    return json.loads(content[start : end + 1])


def _parse_factcheck_result(content: str, *, default_text_key: str) -> tuple[bool, str]:
    """Return (problem_found, markdown_details) from the structured factcheck response."""
    try:
        data = _extract_json_object(content)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("factcheck.parse_fallback", error=str(exc))
        lowered = content.lower()
        # Global verdict short-circuit: if the model issued a top-level "no
        # veto" verdict, trust it — line-level keyword matches that follow
        # are discussing clarification points, not flagging vetos.
        if any(verdict in lowered for verdict in _GLOBAL_NO_VETO_VERDICTS):
            log.info("factcheck.parse_fallback_global_no_veto")
            return False, content.strip()
        # Otherwise: scan lines. If any line contains a gap keyword AND
        # no nearby negation, count it as a problem.
        problem_found = False
        for line in lowered.splitlines():
            if any(k in line for k in GAP_KEYWORDS) and not any(
                neg in line for neg in _NEGATED_GAP_PATTERNS
            ):
                problem_found = True
                break
        return problem_found, content.strip()

    problem_found = bool(data.get("has_gaps", data.get("veto", False)))
    details = (
        data.get(default_text_key)
        or data.get("questions_markdown")
        or data.get("findings_markdown")
        or ""
    ).strip()
    if not details:
        details = "Keine offenen Punkte." if not problem_found else "Offene Punkte ohne Detailtext."
    return problem_found, details


_MIN_CHECKMARKS_FOR_OVERRIDE = 3


def _details_are_clean(details: str) -> bool:
    """True if findings indicate no real substance gap.

    Two paths:
    1. Explicit clean-marker phrase ("keine Drift", "keine Überzeichnung", etc.)
    2. **Strict all-checkmarks heuristic** (WR-07): ≥3 `✓` markers, zero
       failure marks, no unhedged failure phrases. Single-✓ outputs no
       longer qualify — they were the false-positive vector that masked
       real veto signals from short replies.

    Every override fires a WARNING with the full details so the choice is
    auditable retroactively.
    """
    lowered = details.lower()
    if any(marker in lowered for marker in CLEAN_FINDING_MARKERS):
        log.info("factcheck.clean_via_marker")
        return True
    # Strict all-checkmarks heuristic
    check_count = details.count("✓")
    has_fail_mark = "✗" in details or "❌" in details
    if check_count < _MIN_CHECKMARKS_FOR_OVERRIDE or has_fail_mark:
        return False
    # Negation-aware scan of remaining failure phrases
    for line in lowered.splitlines():
        if "nicht belegt" in line and not any(n in line for n in _NEGATED_GAP_PATTERNS):
            return False
        if "fehlender beleg" in line and not any(n in line for n in _NEGATED_GAP_PATTERNS):
            return False
        if "echte lücke" in line:
            return False
        if "kritische lücke" in line:
            return False
    log.warning(
        "factcheck.veto_override",
        check_count=check_count,
        details_preview=details[:200],
    )
    return True


def run_factcheck(
    ctx: RunContext,
    beleg_index_path: Path = BELEG_INDEX_PATH,
    prompt_path: Path = FACTCHECK_PROMPT_PATH,
) -> bool:
    """Run the factcheck agent against 01_analyse.md.

    Returns True if gaps were found and 02_klaerungsfragen.md was written
    (pipeline should pause for user input). Returns False if no gaps found.
    """
    system_prompt = load_prompt(prompt_path)

    analyse_path = ctx.run_dir / "01_analyse.md"
    if not analyse_path.exists():
        raise FileNotFoundError(
            f"01_analyse.md not found in {ctx.run_dir} — run analyst first"
        )
    analyse_text = analyse_path.read_text(encoding="utf-8")
    beleg_index_compact = get_beleg_index_compact(beleg_index_path)

    # Build topic-gating context from posting + analysis so only topic-
    # relevant past clarifications are surfaced into factcheck.
    posting_path = ctx.run_dir / "00_stellenanzeige.md"
    posting_text = posting_path.read_text(encoding="utf-8") if posting_path.exists() else ""

    user_msg = (
        f"## Analyse-Output (zu prüfen)\n{analyse_text}\n\n"
        f"## Beleg-Index (vollständig, kompakt)\n{beleg_index_compact}"
    )
    clarifications_text = format_clarifications_for_prompt(
        current_context=posting_text + "\n" + analyse_text,
    )
    if clarifications_text:
        user_msg += f"\n\n## Zusatzkontext aus früheren Klärungen\n{clarifications_text}"
    antworten_path = ctx.run_dir / "02_antworten.md"
    if antworten_path.exists():
        user_msg += f"\n\n## Antworten auf Klärungsfragen\n{antworten_path.read_text(encoding='utf-8')}"

    log.info("run_factcheck.start", run_id=ctx.run_id)
    content = call_llm(
        agent="factcheck",
        phase="phase3_factcheck",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        snippet_text=analyse_text[:500],
    )

    gaps_found, details = _parse_factcheck_result(content, default_text_key="questions_markdown")

    if gaps_found:
        klaerung_path = ctx.run_dir / "02_klaerungsfragen.md"
        klaerung_content = (
            f"# Klärungsfragen\n\n"
            f"**Run:** {ctx.run_id}\n"
            f"**Erstellt:** {datetime.now(timezone.utc).isoformat()}\n\n"
            f"{details}\n"
        )
        klaerung_path.write_text(klaerung_content, encoding="utf-8")
        write_run_log_entry(
            ctx.run_dir, "factcheck", "Lücken identifiziert — 02_klaerungsfragen.md geschrieben"
        )
        log.info("run_factcheck.gaps_found", run_id=ctx.run_id)
        return True

    write_run_log_entry(ctx.run_dir, "factcheck", "Keine Lücken gefunden")
    log.info("run_factcheck.no_gaps", run_id=ctx.run_id)
    return False


def run_factcheck_iteration(
    ctx: RunContext,
    section: str,
    section_text: str,
    round_num: int,
    beleg_index_path: Path = BELEG_INDEX_PATH,
    prompt_path: Path = FACTCHECK_PROMPT_PATH,
) -> bool:
    """Veto check for a single section draft iteration.

    Returns True if Belegbarkeitsdrift is detected (veto — caller should
    request another round if budget allows). Returns False if clean.

    Unlike run_factcheck (which operates on 01_analyse.md and writes
    02_klaerungsfragen.md), this function operates on a section_text
    string and does NOT write any file — it only returns a bool.
    """
    system_prompt = load_prompt(prompt_path)
    beleg_index_compact = get_beleg_index_compact(beleg_index_path)

    # Static context (identical across all section iterations within a run):
    #   beleg_index + clarifications — cached by Anthropic after the first call.
    # Dynamic context (changes per call): section name + round + section draft.
    # Topic-gating context comes from the shared helper so writer, factcheck
    # and coach all derive identical Anthropic cache keys for the same inputs.
    from cv_tailor.prompt_context import build_gating_context
    clarifications_text = format_clarifications_for_prompt(
        current_context=build_gating_context(ctx) or None,
    )
    static_parts = [f"## Beleg-Index\n{beleg_index_compact}"]
    if clarifications_text:
        static_parts.append(f"## Zusatzkontext aus früheren Klärungen\n{clarifications_text}")

    dynamic_parts = [
        f"## Abschnitt: {section} (Runde {round_num})",
        f"## Entwurf\n{section_text}",
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

    log.info("run_factcheck_iteration.start", run_id=ctx.run_id, section=section, round_num=round_num)
    content = call_llm(
        agent="factcheck",
        phase="phase4_factcheck_iteration",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS_ITERATION,
        iteration=round_num,
        snippet_text=section_text[:500],
    )

    try:
        data = _extract_json_object(content)
        veto = bool(data.get("veto", False) or data.get("has_gaps", False))
        details = (
            data.get("findings_markdown")
            or data.get("questions_markdown")
            or ""
        ).strip()
        if not details:
            details = "Offene Punkte ohne Detailtext." if veto else "Keine Drift gefunden."
    except (ValueError, json.JSONDecodeError):
        veto, details = _parse_factcheck_result(content, default_text_key="findings_markdown")
    if veto and _details_are_clean(details):
        log.warning("factcheck_iteration.clean_details_override", run_id=ctx.run_id, section=section, round_num=round_num)
        veto = False
    iter_dir = ctx.run_dir / "03_iterationen"
    iter_dir.mkdir(exist_ok=True)
    findings_path = iter_dir / f"{section}_v{round_num}_factcheck.md"
    findings_path.write_text(details + "\n", encoding="utf-8")
    log.info(
        "run_factcheck_iteration.done",
        run_id=ctx.run_id,
        section=section,
        round_num=round_num,
        veto=veto,
    )
    write_run_log_entry(
        ctx.run_dir,
        "factcheck_iteration",
        f"{section} Runde {round_num}: {'Veto (Drift)' if veto else 'OK'} — Details: {findings_path}",
    )
    return veto
