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
MAX_TOKENS = 4096        # initial factcheck + iteration findings (2449 Tokens beobachtet)
MAX_TOKENS_ITERATION = 4096  # per-section drift check — full berufserfahrung needs ~2500 tokens
# Truncation-Guard (2026-09-18, Rittmeyer-Lauf): der initiale Factcheck lief
# bei 4096 Tokens in finish_reason=length, enthielt kein JSON, und der
# Keyword-Fallback las die abgeschnittene Prosa als «keine Lücken» — die
# Klärungsrunde wurde still übersprungen, die Fragen kamen erst vom Coach.
# Eine abgeschnittene Antwort ist kein Urteil: einmal mit mehr Budget
# wiederholen; bleibt sie abgeschnitten, gilt sie als Lücke bzw. Veto.
MAX_TOKENS_TRUNCATION_RETRY = 8192
TRUNCATION_NOTE = (
    "**Hinweis:** Die Faktencheck-Antwort wurde vom Modell abgeschnitten "
    "(max_tokens erreicht, auch nach Wiederholung). Eine unvollständige "
    "Prüfung gilt nicht als Freispruch — bitte den Text unten als "
    "Teilbefund lesen und den Lauf nach Prüfung fortsetzen."
)

# Festes Antwortformat (Structured Outputs, ab 2026-11-19). Bis dahin suchte der
# Code die erste `{` und letzte `}` und las bei Misserfolg Prosa per
# Stichwortsuche; eine Regel hob zudem `veto: true` auf, sobald im Befund
# irgendwo «keine Drift» stand. Das Faktencheck-Eval (evals/agent_eval.py
# factcheck) zeigte: Das Modell nannte 35 von 36 eingebauten Fehlern, die
# Auswertung im Code verlor davon 10. Jetzt gilt nur noch das Schema.
PROBLEM_KINDS = ("zahl", "titel", "datum", "faehigkeit", "ueberzeichnung", "sonstiges")
FACTCHECK_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "has_gaps": {"type": "boolean"},
        "veto": {"type": "boolean"},
        "probleme": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "behauptung": {"type": "string"},
                    "art": {"type": "string", "enum": list(PROBLEM_KINDS)},
                    "beleg_ids": {"type": "array", "items": {"type": "string"}},
                    "begruendung": {"type": "string"},
                },
                "required": ["behauptung", "art", "beleg_ids", "begruendung"],
            },
        },
        "questions_markdown": {"type": "string"},
        "findings_markdown": {"type": "string"},
    },
    "required": ["has_gaps", "veto", "probleme", "questions_markdown", "findings_markdown"],
}
FACTCHECK_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {"name": "faktencheck", "strict": True, "schema": FACTCHECK_SCHEMA},
}


def _extract_json_object(text: str) -> dict:
    """Parse the JSON object of a structured response (markdown fences tolerated)."""
    content = text.strip()
    if content.startswith("```"):
        content = content.strip("`").strip()
        if content.lower().startswith("json"):
            content = content[4:].lstrip()
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Factcheck response is not a JSON object")
    return data


def _parse_verdict(content: str) -> dict | None:
    """Strukturiertes Urteil oder None, wenn die Antwort kein gültiges Urteil trägt.

    Rückgabe: ``{"veto", "has_gaps", "probleme", "questions", "findings"}``.
    ``veto`` ist True, wenn das Modell ein Veto setzt **oder** mindestens ein
    Problem listet: ein gelistetes Problem kann kein Freispruch sein.
    Keine Stichwortsuche, keine Prosa-Auswertung.
    """
    try:
        data = _extract_json_object(content)
    except (ValueError, json.JSONDecodeError):
        return None
    veto, has_gaps = data.get("veto"), data.get("has_gaps")
    if not isinstance(veto, bool) and not isinstance(has_gaps, bool):
        return None
    probleme = [p for p in data.get("probleme") or [] if isinstance(p, dict)]
    return {
        "veto": bool(veto) or bool(probleme),
        "has_gaps": bool(has_gaps),
        "probleme": probleme,
        "questions": str(data.get("questions_markdown") or "").strip(),
        "findings": str(data.get("findings_markdown") or "").strip(),
    }


def _format_problems(probleme: list[dict]) -> str:
    """Probleme als Markdown-Liste (Zusatz zum Befund, Grundlage für den Writer)."""
    lines = []
    for p in probleme:
        belege = ", ".join(str(b) for b in p.get("beleg_ids") or []) or "kein Beleg"
        lines.append(
            f"- ✗ [{p.get('art', 'sonstiges')}] «{p.get('behauptung', '').strip()}» — "
            f"{p.get('begruendung', '').strip()} (Belege: {belege})"
        )
    return "\n".join(lines)


def _verdict_known(content: str) -> bool:
    """True if the response carries a usable structured verdict.

    Empty text, `{}`, JSON without a boolean `veto`/`has_gaps`, or prose are
    unknown — and unknown is never clean (review 2026-10-23, R02).
    """
    return _parse_verdict(content) is not None


UNKNOWN_VERDICT_NOTE = (
    "**Hinweis:** Der Faktencheck lieferte auch nach einer Wiederholung kein "
    "auswertbares Urteil (kein gültiges JSON mit veto/has_gaps). "
    "Ungeprüft gilt nicht als sauber."
)


def _call_until_verdict(call, *, run_id: str) -> tuple[str, bool, bool]:
    """Run `call` and repeat once if the answer carries no usable verdict.

    Returns ``(content, truncated, unknown)``. Truncation has its own guard;
    `unknown` is only set for an untruncated answer without a verdict.
    """
    content, truncated = call()
    if truncated or _verdict_known(content):
        return content, truncated, False
    log.warning("factcheck.unknown_verdict_retry", run_id=run_id, preview=content[:120])
    content, truncated = call()
    unknown = not truncated and not _verdict_known(content)
    if unknown:
        log.warning("factcheck.unknown_verdict", run_id=run_id, preview=content[:120])
    return content, truncated, unknown


def _call_factcheck_llm(
    *,
    phase: str,
    run_id: str,
    messages: list[dict],
    max_tokens: int,
    iteration: int = 0,
    snippet_text: str = "",
) -> tuple[str, bool]:
    """call_llm with a single retry on truncation.

    Returns ``(content, truncated)``. ``truncated`` is True only when the
    retry with MAX_TOKENS_TRUNCATION_RETRY was cut off as well — callers
    must then treat the result as unverified, never as clean.
    """
    meta: dict = {}
    content = call_llm(
        agent="factcheck", phase=phase, run_id=run_id, messages=messages,
        max_tokens=max_tokens, iteration=iteration, snippet_text=snippet_text,
        meta_out=meta, response_format=FACTCHECK_RESPONSE_FORMAT,
    )
    if not meta.get("truncated"):
        return content, False
    log.warning(
        "factcheck.truncated_retry", run_id=run_id, phase=phase,
        max_tokens=max_tokens, retry_max_tokens=MAX_TOKENS_TRUNCATION_RETRY,
    )
    meta = {}
    content = call_llm(
        agent="factcheck", phase=phase, run_id=run_id, messages=messages,
        max_tokens=MAX_TOKENS_TRUNCATION_RETRY, iteration=iteration,
        snippet_text=snippet_text, meta_out=meta, response_format=FACTCHECK_RESPONSE_FORMAT,
    )
    truncated = bool(meta.get("truncated"))
    if truncated:
        log.error("factcheck.truncated_after_retry", run_id=run_id, phase=phase)
    return content, truncated


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
        current_context=posting_text,
    )
    if clarifications_text:
        user_msg += f"\n\n## Zusatzkontext aus früheren Klärungen\n{clarifications_text}"
    from cv_tailor.fact_limits import prompt_block as fact_limits_block
    fact_limits = fact_limits_block()
    if fact_limits:
        user_msg += f"\n\n{fact_limits}"
    antworten_path = ctx.run_dir / "02_antworten.md"
    if antworten_path.exists():
        user_msg += f"\n\n## Antworten auf Klärungsfragen\n{antworten_path.read_text(encoding='utf-8')}"

    log.info("run_factcheck.start", run_id=ctx.run_id)

    def _call() -> tuple[str, bool]:
        return _call_factcheck_llm(
            phase="phase3_factcheck",
            run_id=ctx.run_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=MAX_TOKENS,
            snippet_text=analyse_text[:500],
        )

    content, truncated, unknown = _call_until_verdict(_call, run_id=ctx.run_id)

    verdict = _parse_verdict(content)
    gaps_found = bool(verdict and verdict["has_gaps"])
    details = ""
    if verdict:
        details = verdict["questions"] or verdict["findings"]
        if verdict["probleme"] and gaps_found:
            details = f"{details}\n\n{_format_problems(verdict['probleme'])}".strip()
        elif verdict["probleme"]:
            # Probleme ohne has_gaps: keine Frage und keine Pause (Frage-Disziplin),
            # aber auch nicht still verwerfen (Review 2026-11-19). Sie stehen im
            # Lauf-Log; die Drift selbst faengt der Sektions-Faktencheck ab.
            write_run_log_entry(
                ctx.run_dir, "factcheck",
                f"{len(verdict['probleme'])} Problem(e) in der Analyse gelistet, ohne "
                f"Klärungsbedarf (beratend):\n{_format_problems(verdict['probleme'])}",
            )
    if verdict is None:
        details = content.strip()  # Teilbefund (abgeschnitten/ungültig) bleibt sichtbar
    if not details:
        details = "Offene Punkte ohne Detailtext." if gaps_found else "Keine offenen Punkte."
    if unknown:
        gaps_found = True
        details = f"{UNKNOWN_VERDICT_NOTE}\n\n{details}"
        write_run_log_entry(
            ctx.run_dir, "factcheck",
            "Kein auswertbares Urteil (auch nach Wiederholung) — als Klärungsbedarf gewertet",
        )
    if truncated and not gaps_found:
        # Abgeschnitten heisst ungeprüft — nie «keine Lücken».
        gaps_found = True
        details = f"{TRUNCATION_NOTE}\n\n{details or content}"
        write_run_log_entry(
            ctx.run_dir, "factcheck",
            "Antwort abgeschnitten (auch nach Retry) — als Klärungsbedarf gewertet, "
            "kein Freispruch",
        )

    # Gap-to-Question-Routing: LÜCKE-/SCHWACH-Zeilen aus dem Profil-Fit-
    # Abgleich werden als Klärungsfragen eingemischt, statt nur als
    # fortfahren/abbrechen-Warnung zu enden (Alex' Feedback 2026-06-12:
    # erst fragen, dann Erfahrung transferieren). Nur in der ersten
    # Factcheck-Runde — sobald 02_antworten.md existiert, wurden die
    # Fragen gestellt; erneutes Einmischen würde eine Endlos-Pause erzeugen.
    gap_questions_md = ""
    if not antworten_path.exists():
        from cv_tailor.clarifications import clarification_chunks
        from cv_tailor.profile_fit import collect_question_gaps, format_gaps_as_questions

        standard_cv = Path("data/standard_cv.md")
        fit_gaps = collect_question_gaps(
            analyse_path,
            standard_cv.read_text(encoding="utf-8") if standard_cv.exists() else "",
        )
        if fit_gaps:
            gap_questions_md = format_gaps_as_questions(
                fit_gaps, answered_context=clarification_chunks(),
            )

    # Harte Runden-Kappung (2026-06-12): sobald Alex einmal geantwortet hat,
    # pausiert der initiale Factcheck NIE wieder. Beobachtet: 8 Frage-Runden
    # in Folge — der Factcheck fand mit jedem Antwort-Kontext neue Zweifel
    # und verschärfte Anforderungen rückwirkend ("AI-Projekt vor Ort bei
    # Namics"). Restzweifel werden beratend nach _factcheck_open_questions.md
    # geschrieben (analog _coach_questions.md); sachliche Drift fängt
    # weiterhin der Sektions-Factcheck im Writer-Loop hart ab.
    if gaps_found and antworten_path.exists():
        advisory_path = ctx.run_dir / "_factcheck_open_questions.md"
        advisory_path.write_text(
            "# Offene Factcheck-Hinweise (beratend)\n\n"
            f"**Run:** {ctx.run_id}\n"
            f"**Erstellt:** {datetime.now(timezone.utc).isoformat()}\n\n"
            "Der Faktencheck hatte nach der beantworteten Frage-Runde weitere "
            "Anmerkungen. Die Pipeline pausiert dafür nicht erneut — bitte vor "
            "dem Versenden kurz prüfen. Unbelegte Claims blockiert weiterhin "
            "der Sektions-Factcheck während des Schreibens.\n\n"
            f"{details}\n",
            encoding="utf-8",
        )
        write_run_log_entry(
            ctx.run_dir, "factcheck",
            "Frage-Runde bereits beantwortet — weitere Hinweise beratend in _factcheck_open_questions.md, Pipeline läuft weiter",
        )
        log.info("run_factcheck.advisory_after_answers", run_id=ctx.run_id)
        return False

    if gaps_found or gap_questions_md:
        parts: list[str] = []
        if gaps_found:
            parts.append(details)
        if gap_questions_md:
            parts.append(gap_questions_md)
        klaerung_path = ctx.run_dir / "02_klaerungsfragen.md"
        klaerung_content = (
            f"# Klärungsfragen\n\n"
            f"**Run:** {ctx.run_id}\n"
            f"**Erstellt:** {datetime.now(timezone.utc).isoformat()}\n\n"
            + "\n\n".join(parts)
            + "\n"
        )
        klaerung_path.write_text(klaerung_content, encoding="utf-8")
        source = "Faktencheck" if gaps_found else "Profil-Fit-Abgleich"
        if gaps_found and gap_questions_md:
            source = "Faktencheck + Profil-Fit-Abgleich"
        write_run_log_entry(
            ctx.run_dir, "factcheck",
            f"Lücken identifiziert ({source}) — 02_klaerungsfragen.md geschrieben",
        )
        log.info(
            "run_factcheck.gaps_found", run_id=ctx.run_id,
            factcheck_gaps=gaps_found, fit_gap_questions=bool(gap_questions_md),
        )
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
    from cv_tailor.fact_limits import prompt_block as fact_limits_block
    fact_limits = fact_limits_block()
    if fact_limits:
        static_parts.append(fact_limits)
    from cv_tailor.companion_docs import format_for_prompt, load_companion_context
    companion_block = format_for_prompt(load_companion_context(ctx.run_dir), role="reviewer")
    if companion_block:
        static_parts.append(companion_block)

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
    def _call() -> tuple[str, bool]:
        return _call_factcheck_llm(
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

    content, truncated, unknown = _call_until_verdict(_call, run_id=ctx.run_id)

    verdict = _parse_verdict(content)
    veto = bool(verdict and (verdict["veto"] or verdict["has_gaps"]))
    details = ""
    if verdict:
        details = verdict["findings"] or verdict["questions"]
        if verdict["probleme"]:
            details = f"{details}\n\n{_format_problems(verdict['probleme'])}".strip()
    if verdict is None:
        details = content.strip()  # Teilbefund (abgeschnitten/ungültig) bleibt sichtbar
    if not details:
        details = "Offene Punkte ohne Detailtext." if veto else "Keine Drift gefunden."
    if unknown:
        veto = True
        details = f"{UNKNOWN_VERDICT_NOTE}\n\n{details}"
    if truncated and not veto:
        # Abgeschnitten heisst ungeprüft — der Abschnitt geht als Veto in
        # die nächste Runde; die Note landet in den Findings für den Writer.
        veto = True
        details = f"{TRUNCATION_NOTE}\n\n{details}"
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
