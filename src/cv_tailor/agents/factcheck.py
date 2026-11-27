"""Factcheck agent: validates analysis and section drafts against the Beleg-Index."""
from __future__ import annotations

import json
from datetime import UTC, datetime
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
# Einstufung (ab 2026-11-25): Bis dahin war jeder Eintrag ein Veto, auch einer,
# den das Modell selbst als zulässig beschrieb. Ein Hinweis blockiert nicht;
# alles Unbekannte blockiert.
BLOCKING_GRADES = ("widerspruch", "ueberzeichnung", "kein_beleg")
GRADES = (*BLOCKING_GRADES, "hinweis")
PROBLEM_KINDS = ("zahl", "titel", "datum", "faehigkeit", "ueberzeichnung", "sonstiges")
FACTCHECK_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        # Reihenfolge: erst prüfen, dann die Befunde, dann das Urteil daraus.
        "geprueft": {"type": "array", "items": {"type": "string"}},
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
                    "einstufung": {"type": "string", "enum": list(GRADES)},
                },
                "required": ["behauptung", "art", "beleg_ids", "begruendung", "einstufung"],
            },
        },
        "has_gaps": {"type": "boolean"},
        "veto": {"type": "boolean"},
        "questions_markdown": {"type": "string"},
        "findings_markdown": {"type": "string"},
    },
    "required": ["geprueft", "probleme", "has_gaps", "veto", "questions_markdown", "findings_markdown"],
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


def _is_blocking(problem: dict) -> bool:
    """Nur ein klar als Hinweis eingestufter Eintrag blockiert nicht."""
    return problem.get("einstufung") != "hinweis"


def _parse_verdict(content: str) -> dict | None:
    """Strukturiertes Urteil oder None, wenn die Antwort kein gültiges Urteil trägt.

    Rückgabe: ``{"veto", "has_gaps", "probleme", "hinweise", "konsistent",
    "vollstaendig", "questions", "findings"}``; ``probleme`` enthält nur
    blockierende Einträge. ``veto`` ist True, wenn das Modell ein Veto setzt
    **oder** mindestens ein blockierender Eintrag da ist. ``konsistent`` ist
    False, wenn das Modell ablehnt, ohne eine Stelle zu nennen.
    Keine Stichwortsuche, keine Prosa-Auswertung.
    """
    try:
        data = _extract_json_object(content)
    except (ValueError, json.JSONDecodeError):
        return None
    veto, has_gaps, raw = data.get("veto"), data.get("has_gaps"), data.get("probleme")
    if not isinstance(veto, bool) and not isinstance(has_gaps, bool):
        return None
    entries = [
        p if isinstance(p, dict) else {"behauptung": str(p), "begruendung": "Eintrag unlesbar."}
        for p in (raw if isinstance(raw, list) else [])
    ]
    probleme = [p for p in entries if _is_blocking(p)]
    return {
        "veto": bool(veto) or bool(probleme),
        "has_gaps": bool(has_gaps),
        "probleme": probleme,
        "hinweise": [p for p in entries if not _is_blocking(p)],
        "konsistent": bool(probleme) or not (veto or has_gaps),
        "vollstaendig": isinstance(veto, bool) and isinstance(raw, list),
        "geprueft": len(data["geprueft"]) if isinstance(data.get("geprueft"), list) else 0,
        "questions": str(data.get("questions_markdown") or "").strip(),
        "findings": str(data.get("findings_markdown") or "").strip(),
    }


def _format_problems(probleme: list[dict], mark: str = "✗") -> str:
    """Probleme als Markdown-Liste (Zusatz zum Befund, Grundlage für den Writer)."""
    lines = []
    for p in probleme:
        belege = ", ".join(str(b) for b in p.get("beleg_ids") or []) or "kein Beleg"
        lines.append(
            f"- {mark} [{p.get('art', 'sonstiges')}] «{str(p.get('behauptung', '')).strip()}» — "
            f"{str(p.get('begruendung', '')).strip()} (Belege: {belege})"
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


# Anteil der Entwurfszeilen, die ein Freispruch als geprüft ausweisen muss.
MIN_COVERAGE = 0.8


def _claim_lines(section_text: str) -> int:
    """Zeilen mit Aussagen: ohne Überschriften, Trennlinien und Leerzeilen."""
    lines = (line.strip() for line in section_text.splitlines())
    return sum(1 for line in lines if line and not line.startswith(("#", "---")))


def _section_verdict_usable(content: str, section_text: str = "") -> bool:
    """Wie `_verdict_known`, aber strenger: ein Abschnitt braucht ein volles Urteil.

    Ohne `veto` oder ohne Liste `probleme` ist nichts geprüft; eine Ablehnung
    ohne genannte Stelle ist nicht auswertbar. Ein Freispruch muss zudem die
    Zeilen ausweisen (Messung 2026-11-25: Das Modell liess einen Fehler durch
    und schrieb, es prüfe «bewusst nicht vollständig»).
    """
    verdict = _parse_verdict(content)
    if not (verdict and verdict["vollstaendig"] and verdict["konsistent"]):
        return False
    return verdict["veto"] or verdict["geprueft"] >= MIN_COVERAGE * _claim_lines(section_text)


def _call_until_verdict(call, *, run_id: str, usable=_verdict_known) -> tuple[str, bool, bool]:
    """Run `call` and repeat once if the answer carries no usable verdict.

    Returns ``(content, truncated, unknown)``. Truncation has its own guard;
    `unknown` is only set for an untruncated answer without a verdict.
    Logs the length only: the answer quotes CV text.
    """
    content, truncated = call()
    if truncated or usable(content):
        return content, truncated, False
    log.warning("factcheck.unknown_verdict_retry", run_id=run_id, chars=len(content))
    content, truncated = call()
    unknown = not truncated and not usable(content)
    if unknown:
        log.warning("factcheck.unknown_verdict", run_id=run_id, chars=len(content))
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


# Erste Zeile von `<abschnitt>_v<n>_factcheck.md`; quality_snapshot zählt sie.
VERDICT_VETO = "**Urteil:** Veto"
VERDICT_OK = "**Urteil:** kein Veto"
HINTS_FILENAME = "_factcheck_hinweise.md"


def _write_hints(ctx: RunContext, section: str, round_num: int, hinweise: list[dict]) -> None:
    """Hinweise je Abschnitt (letzte Runde) ablegen und im Lauf zusammenfassen."""
    iter_dir = ctx.run_dir / "03_iterationen"
    hint_path = iter_dir / f"{section}_factcheck_hinweise.md"
    if hinweise:
        hint_path.write_text(
            f"## {section} (Runde {round_num})\n\n{_format_problems(hinweise, '•')}\n",
            encoding="utf-8",
        )
    else:
        hint_path.unlink(missing_ok=True)
    blocks = [
        p.read_text(encoding="utf-8") for p in sorted(iter_dir.glob("*_factcheck_hinweise.md"))
    ]
    summary = ctx.run_dir / HINTS_FILENAME
    if blocks:
        summary.write_text(
            "# Faktencheck-Hinweise (blockieren nicht)\n\n"
            "Vor der Übergabe durchsehen.\n\n" + "\n".join(blocks),
            encoding="utf-8",
        )
    else:
        summary.unlink(missing_ok=True)


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
        if verdict["hinweise"]:
            write_run_log_entry(
                ctx.run_dir, "factcheck",
                f"{len(verdict['hinweise'])} Hinweis(e) zur Analyse:\n"
                f"{_format_problems(verdict['hinweise'], '•')}",
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
            f"**Erstellt:** {datetime.now(UTC).isoformat()}\n\n"
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
            f"**Erstellt:** {datetime.now(UTC).isoformat()}\n\n"
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
    from cv_tailor.factcheck_sources import extra_clarifications_block, static_blocks
    from cv_tailor.prompt_context import build_gating_context
    gating_context = build_gating_context(ctx) or None
    clarifications_text = format_clarifications_for_prompt(current_context=gating_context)
    static_parts = [
        f"## Beleg-Index\n{beleg_index_compact}",
        *static_blocks(ctx.run_dir, beleg_index_path),
    ]
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

    extra_clarifications = extra_clarifications_block(section_text, gating_context)
    dynamic_parts = [extra_clarifications] if extra_clarifications else []
    dynamic_parts += [
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

    content, truncated, unknown = _call_until_verdict(
        _call, run_id=ctx.run_id, usable=lambda c: _section_verdict_usable(c, section_text),
    )

    verdict = _parse_verdict(content)
    veto = bool(verdict and (verdict["veto"] or verdict["has_gaps"]))
    hinweise = verdict["hinweise"] if verdict else []
    if verdict is None:
        details = content.strip()  # Teilbefund (abgeschnitten/ungültig) bleibt sichtbar
    elif verdict["probleme"]:
        # Nur blockierende Punkte: Der Writer liest diese Datei als Pflichtkorrektur.
        details = _format_problems(verdict["probleme"])
    elif veto:
        details = verdict["findings"] or verdict["questions"] or "Offene Punkte ohne Detailtext."
    else:
        details = "Keine Drift gefunden."
    if unknown:
        veto = True
        details = f"{UNKNOWN_VERDICT_NOTE}\n\n{details}"
    if truncated:
        # Abgeschnitten heisst ungeprüft — der Abschnitt geht als Veto in
        # die nächste Runde; die Note landet in den Findings für den Writer.
        # Sie steht auch bei einem schon gesetzten Veto: `_factcheck_offen.md`
        # erkennt daran, dass die Punkte unvollständig sind.
        veto = True
        details = f"{TRUNCATION_NOTE}\n\n{details}"
    iter_dir = ctx.run_dir / "03_iterationen"
    iter_dir.mkdir(exist_ok=True)
    findings_path = iter_dir / f"{section}_v{round_num}_factcheck.md"
    findings_path.write_text(
        f"{VERDICT_VETO if veto else VERDICT_OK}\n\n{details}\n", encoding="utf-8",
    )
    _write_hints(ctx, section, round_num, hinweise)
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
        f"{section} Runde {round_num}: {'Veto (Drift)' if veto else 'OK'}, "
        f"{len(hinweise)} Hinweis(e) — Details: {findings_path}",
    )
    return veto
