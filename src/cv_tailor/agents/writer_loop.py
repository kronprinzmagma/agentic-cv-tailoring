"""Writer loop: orchestrates writer + parallel reviewers + factcheck veto for all CV sections. Produces 04_final_de.md."""
from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from cv_tailor.agents.coach_reviewer import run_coach_reviewer
from cv_tailor.agents.factcheck import run_factcheck_iteration
from cv_tailor.agents.hiring_reviewer import run_hiring_reviewer
from cv_tailor.agents.writer import MAX_ROUNDS, SECTIONS, run_writer
from cv_tailor.consistency_check import (
    autofix_headers,
    format_issues_for_writer,
    validate_berufserfahrung,
)
from cv_tailor.length_check import (
    check_section_length,
    format_issues_for_writer as format_length_issues_for_writer,
)
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

BELEG_INDEX_PATH = Path("data/beleg_index.json")
STANDARD_CV_PATH = Path("data/standard_cv.md")


# A veto occurrence directly prefixed by a negation ("Kein Veto", "kein
# hartes Veto", "ohne Veto-Status") is an all-clear. The negation must be
# adjacent (at most one word gap) — "kein Summary-Text — Veto" or "ohne
# Beleg (→ Veto)" negate something else and stay vetos.
_VETO_NEGATION_PREFIX_RE = re.compile(
    r"\b(kein\w*|nicht|ohne)\s+(\w+\s+)?$", re.IGNORECASE
)


def _count_explicit_veto_markers(text: str) -> int:
    """Count concrete `**...Veto**` patterns in the body (not just verdicts).

    The Coach prompt instructs reviewers to flag concrete violations with
    `**Veto: <Grund>**`, `**Übertreibungs-Veto**`, etc.
    These are hard signals (the reviewer found a specific, named problem) and
    distinct from the catch-all verdict "Überarbeitung nötig" which Coach
    tends to use by default for almost any section.

    Case-insensitive (`**VETO: …**` counts). A span counts unless every
    "veto" in it is negation-prefixed — `**Kein Veto**` is an all-clear,
    `**Kein Veto auf Inhalt, aber Veto auf Platzierung**` still counts.
    """
    count = 0
    for span in re.findall(r"\*\*[^*]*veto[^*]*\*\*", text, flags=re.IGNORECASE):
        for m in re.finditer(r"veto", span, flags=re.IGNORECASE):
            if not _VETO_NEGATION_PREFIX_RE.search(span[: m.start()]):
                count += 1
                break
    return count


_COACH_QUESTION_HEADING_RE = re.compile(
    r"^#{2,4}\s+(Offene Fragen|Open Questions)",
    re.IGNORECASE,
)


def _extract_coach_questions(review_text: str) -> str:
    """Pull the open-questions block out of a coach review.

    Matches `## Offene Fragen ...` (DE) and `## Open Questions ...` (EN),
    including h3/h4 variants. Stops at the next heading of equal or
    higher level. Returns empty if no questions were raised.
    """
    if not review_text:
        return ""
    lines = review_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _COACH_QUESTION_HEADING_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^#{2,4}\s+", lines[j].strip()):
            end = j
            break
    body = "\n".join(lines[start:end]).strip()
    if not body or body.lower() in ("(keine)", "(none)", "—", "-"):
        return ""
    return body


def _append_coach_questions(
    ctx: "RunContext", section: str, round_num: int, coach_review: str
) -> None:
    """Append extracted coach questions to the per-run questions aggregator."""
    body = _extract_coach_questions(coach_review)
    if not body:
        return
    out_path = ctx.run_dir / "_coach_questions.md"
    header = f"\n## {section} (Runde {round_num})\n\n"
    if not out_path.exists():
        out_path.write_text(
            "# Offene Fragen vom Coach\n\n"
            "Der Coach hat während der Pipeline Punkte gefunden, die zwischen Beleg-Index/"
            "Standard-CV und Entwurf nicht eindeutig zuordenbar waren. Bitte prüfe vor "
            "dem Versenden:\n"
            + header
            + body
            + "\n",
            encoding="utf-8",
        )
    else:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(header + body + "\n")
    log.info(
        "writer_loop.coach_questions_appended",
        run_id=ctx.run_id,
        section=section,
        round_num=round_num,
    )


# Literal option scales from the reviewer prompts. A model that echoes the
# template line before picking has not verdicted "Ablegen"/"Grundsätzliches
# Problem" — strip echoes before matching rejection terms.
_VERDICT_OPTION_ECHOES = (
    "weiterleiten (überzeugend) / weiterleiten mit fragezeichen / ablegen",
    "bereit / überarbeitung nötig / grundsätzliches problem",
)

# Rejection vocabulary from the two reviewer prompts. "ablegen" is the
# verdict word the HM prompt actually defines ("Weiterleiten / … /
# Ablegen") — 14 historical reviews used it without being parsed.
_VERDICT_REJECTION_TERMS = (
    "grundsätzliches problem",
    "grundsaetzliches problem",
    "ablehnung",
    "ablegen",
    "rejected",
    "reject:",
)

# Negation directly before a rejection term ("kein grundsätzliches Problem",
# "würde es nicht ablegen", "statt Ablegen") means the reviewer is giving the
# all-clear, not a veto. Adjacent only (max one word gap) — "nicht
# überzeugend — Ablegen" stays a reject.
_VERDICT_NEGATION_RE = re.compile(r"\b(kein\w*|nicht|ohne|statt)\s+(\w+\s+)?$")


def _reviewer_signals_veto(review_text: str) -> bool:
    """Return True if a reviewer's verdict signals required revision.

    Round 2 is expensive — only trigger it when the reviewer found something
    concrete. Coach defaults to "Überarbeitung nötig" for almost any draft;
    treating that alone as veto means we always pay for round 2.

    Veto triggers (any of):
    1. A rejection verdict in the Gesamturteil block: "Grundsätzliches
       Problem" (Coach) or "Ablehnung" / "Ablegen" / "Reject" (HM). The
       match is anchored to the last "Gesamturteil" occurrence (fallback:
       1500-char tail), negation-aware ("kein grundsätzliches Problem"
       does not trigger) and ignores echoed option-scale template lines.
    2. At least one concrete `**...Veto**` marker in the body (specific
       violation named by the reviewer; see _count_explicit_veto_markers).

    Mere "Überarbeitung nötig" / "minor revision" / "Bereit" → no veto.
    Empty review or unparseable → no veto (conservative).
    """
    if not review_text:
        return False
    text_lower = review_text.lower()

    # 1. Rejection verdict, anchored to the Gesamturteil block. "ablegen" is
    # also an ordinary German verb ("Rechenschaft ablegen") — body prose must
    # never veto, only the verdict block.
    idx = text_lower.rfind("gesamturteil")
    segment = text_lower[idx : idx + 400] if idx != -1 else text_lower[-1500:]
    for echo in _VERDICT_OPTION_ECHOES:
        segment = segment.replace(echo, " ")
    for term in _VERDICT_REJECTION_TERMS:
        start = 0
        while (pos := segment.find(term, start)) != -1:
            if not _VERDICT_NEGATION_RE.search(segment[max(0, pos - 40) : pos]):
                return True
            start = pos + len(term)

    # 2. Explicit, concrete veto markers anywhere in body (≥1)
    if _count_explicit_veto_markers(review_text) >= 1:
        return True

    return False
SECTION_TITLES = {
    "management_summary": "Management Summary",
    "schluesselkompetenzen": "Schlüsselkompetenzen",
    "berufserfahrung": "Berufserfahrung",
}


def _strip_duplicate_section_heading(text: str, section: str) -> str:
    """Remove writer-supplied wrappers before final consolidation."""
    title = SECTION_TITLES[section].lower()
    aliases = {title, section.replace("_", " ").lower(), section.lower()}
    lines = text.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip().lstrip("#").strip().lower() in aliases:
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and lines[0].strip() == "---":
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _process_section(
    ctx: RunContext,
    section: str,
    beleg_index_path: Path,
    analyse_path: Path,
    progress_cb: Callable[[str], None] | None,
) -> tuple[str, str]:
    """Process one section through the writer/reviewer/factcheck loop.

    Runs up to MAX_ROUNDS:
      1. Writer produces a draft
      2. Hiring + Coach reviewer run in parallel
      3. Factcheck veto check
      4. Accept on first clean round; revise or raise on exhausted budget

    Returns (section, accepted_text). Raises RuntimeError on factcheck blocker.
    Called from a ThreadPoolExecutor — all file writes use section-scoped paths
    so concurrent section processing never conflicts.
    """
    if progress_cb:
        progress_cb(f"  {SECTION_TITLES[section]}: Writer startet …")
    log.info("writer_loop.section_start", run_id=ctx.run_id, section=section)
    write_run_log_entry(ctx.run_dir, "writer_loop", f"Abschnitt '{section}' gestartet")

    accepted_text: str | None = None
    draft_text: str = ""

    for round_num in range(1, MAX_ROUNDS + 1):
        log.info("writer_loop.round_start", run_id=ctx.run_id, section=section, round_num=round_num)

        # Step 1: Writer
        draft_path = run_writer(
            ctx,
            section=section,
            round_num=round_num,
            beleg_index_path=beleg_index_path,
            analyse_path=analyse_path,
        )
        draft_text = draft_path.read_text(encoding="utf-8")

        # Step 2: Parallel reviews (hiring + coach)
        with ThreadPoolExecutor(max_workers=2) as pool:
            hiring_fut = pool.submit(run_hiring_reviewer, ctx, section, draft_text, round_num)
            coach_fut  = pool.submit(run_coach_reviewer,  ctx, section, draft_text, round_num)
            hiring_review = hiring_fut.result()
            coach_review  = coach_fut.result()

        # Extract any "## Offene Fragen an Alex" block from the coach review
        # and append it to the per-run aggregator. Coach asks rather than
        # guesses when claims are ambiguous against the Standard-CV / Beleg-Index.
        _append_coach_questions(ctx, section, round_num, coach_review)

        # Step 3: Factcheck veto (substance — hard blocker if persistent)
        factcheck_veto = run_factcheck_iteration(
            ctx, section, draft_text, round_num, beleg_index_path=beleg_index_path
        )

        # Step 3b: Deterministic consistency check (Berufserfahrung only).
        # Catches hallucinated station headers (invented titles, drifted date
        # ranges, split merged entries). Runs *before* veto combining so we
        # can attempt an autofix at MAX_ROUNDS.
        consistency_veto = False
        if section == "berufserfahrung":
            consistent, issues = validate_berufserfahrung(draft_text, STANDARD_CV_PATH)
            iter_dir = ctx.run_dir / "03_iterationen"
            iter_dir.mkdir(exist_ok=True)
            consistency_path = iter_dir / f"{section}_v{round_num}_consistency.md"
            consistency_path.write_text(
                format_issues_for_writer(issues) + "\n", encoding="utf-8"
            )
            if not consistent:
                consistency_veto = True
                log.warning(
                    "writer_loop.consistency_veto",
                    run_id=ctx.run_id,
                    section=section,
                    round_num=round_num,
                    issue_count=len(issues),
                )
                write_run_log_entry(
                    ctx.run_dir,
                    "consistency_check",
                    f"{section} Runde {round_num}: Konsistenz-Veto ({len(issues)} Drift-Punkte)",
                )

        # Step 3c: Deterministic length-budget check (Summary words / Bullet
        # words). Soft veto — like style: requests revision, drops after
        # MAX_ROUNDS so we always produce output. Reason: length is a quality
        # concern, not a factual issue; a too-long draft is still shippable
        # if the writer can't shorten in budget. Findings are written to
        # `{section}_v{N}_length.md` and fed to the writer in round 2.
        length_veto = False
        length_issues = check_section_length(section, draft_text)
        if length_issues:
            iter_dir = ctx.run_dir / "03_iterationen"
            iter_dir.mkdir(exist_ok=True)
            length_path = iter_dir / f"{section}_v{round_num}_length.md"
            length_path.write_text(
                format_length_issues_for_writer(length_issues), encoding="utf-8"
            )
            length_veto = True
            log.info(
                "writer_loop.length_veto",
                run_id=ctx.run_id,
                section=section,
                round_num=round_num,
                issues=len(length_issues),
            )
            write_run_log_entry(
                ctx.run_dir,
                "length_check",
                f"{section} Runde {round_num}: Längen-Veto ({len(length_issues)} Überlängen)",
            )

        substance_veto = factcheck_veto or consistency_veto

        # Step 3a: Reviewer veto (style/register — soft, drops after MAX_ROUNDS)
        # If the coach or hiring reviewer signals "Überarbeitung nötig", we
        # request a revision — but unlike factcheck, we don't escalate to a
        # blocker if the writer can't satisfy them in MAX_ROUNDS. After the
        # budget is exhausted, we accept the latest draft. Rationale: factcheck
        # protects substance (faktisch falsch → block). Reviewer veto protects
        # quality (besser → request revision, accept best effort).
        style_veto = False
        coach_veto = _reviewer_signals_veto(coach_review)
        hiring_veto = _reviewer_signals_veto(hiring_review)
        # Length veto folds into style veto: deterministic quality signal,
        # treated identically (request revision now, accept best effort at MAX_ROUNDS).
        if length_veto:
            style_veto = True
        if coach_veto or hiring_veto:
            style_veto = True
            log.info(
                "writer_loop.reviewer_veto",
                run_id=ctx.run_id,
                section=section,
                round_num=round_num,
                coach=coach_veto,
                hiring=hiring_veto,
            )
            write_run_log_entry(
                ctx.run_dir,
                "reviewer_veto",
                f"{section} Runde {round_num}: "
                f"{'Coach' if coach_veto else ''}"
                f"{'+Hiring' if coach_veto and hiring_veto else ('Hiring' if hiring_veto else '')} "
                f"verlangt Überarbeitung",
            )

        # Last-resort header autofix at MAX_ROUNDS — only for consistency drift.
        # Factcheck veto (LLM-decided substance issues) stays a hard blocker.
        if (
            section == "berufserfahrung"
            and round_num == MAX_ROUNDS
            and consistency_veto
        ):
            fixed_text, fixes_applied = autofix_headers(draft_text, STANDARD_CV_PATH)
            if fixes_applied:
                draft_text = fixed_text
                iter_dir = ctx.run_dir / "03_iterationen"
                (iter_dir / f"{section}_v{round_num}.md").write_text(
                    draft_text, encoding="utf-8"
                )
                log.info(
                    "writer_loop.consistency_autofix",
                    run_id=ctx.run_id,
                    section=section,
                    fixes=fixes_applied,
                )
                write_run_log_entry(
                    ctx.run_dir,
                    "consistency_autofix",
                    f"{section}: {len(fixes_applied)} Header automatisch korrigiert",
                )
                consistent_after, _ = validate_berufserfahrung(
                    draft_text, STANDARD_CV_PATH
                )
                if consistent_after:
                    consistency_veto = False
                    substance_veto = factcheck_veto  # only factcheck remains

        # Combined veto: substance is hard, style is soft.
        # On the LAST round, only substance vetoes block — style vetoes are
        # downgraded to "accept best effort" so we always produce output.
        if round_num < MAX_ROUNDS:
            veto = substance_veto or style_veto
        else:
            veto = substance_veto  # last round: only substance blocks
            if style_veto and not substance_veto:
                log.info(
                    "writer_loop.style_veto_accepted_at_max_rounds",
                    run_id=ctx.run_id,
                    section=section,
                )
                write_run_log_entry(
                    ctx.run_dir,
                    "reviewer_veto",
                    f"{section} Runde {round_num}: Style-Veto akzeptiert (MAX_ROUNDS erreicht)",
                )

        if not veto:
            accepted_text = draft_text
            if progress_cb:
                progress_cb(f"  ✓ {SECTION_TITLES[section]} akzeptiert (Runde {round_num})")
            log.info("writer_loop.section_accepted", run_id=ctx.run_id, section=section, round_num=round_num)
            write_run_log_entry(ctx.run_dir, "writer_loop", f"'{section}' Runde {round_num} akzeptiert (kein Veto)")
            break
        elif round_num < MAX_ROUNDS:
            if progress_cb:
                progress_cb(f"  ↻ {SECTION_TITLES[section]}: Veto — Runde {round_num + 1} …")
            log.info("writer_loop.revision_round", run_id=ctx.run_id, section=section, round_num=round_num, reason="Veto")
            write_run_log_entry(ctx.run_dir, "writer_loop", f"'{section}' Runde {round_num}: Veto — weiter zu Runde {round_num + 1}")
        else:
            blocker_path = ctx.run_dir / f"_factcheck_blocker_{section}.md"
            blocker_content = (
                f"# Faktencheck-Blocker: {section}\n\n"
                f"**Run:** {ctx.run_id}\n"
                f"**Runde:** {round_num}/{MAX_ROUNDS}\n\n"
                "Der Faktencheck hat nach ausgeschöpftem Iterationsbudget weiterhin "
                "ein Veto gemeldet. Der Abschnitt wird nicht automatisch akzeptiert.\n\n"
                f"Bitte prüfe `{ctx.run_dir / '03_iterationen' / f'{section}_v{round_num}.md'}` "
                "und die zugehörigen Review-Dateien.\n"
            )
            blocker_path.write_text(blocker_content, encoding="utf-8")
            log.error("writer_loop.factcheck_blocked", run_id=ctx.run_id, section=section)
            write_run_log_entry(
                ctx.run_dir,
                "writer_loop",
                f"'{section}' blockiert — Faktencheck-Veto nach {MAX_ROUNDS} Runden",
            )
            raise RuntimeError(
                f"Faktencheck blockiert Abschnitt '{section}' nach {MAX_ROUNDS} Runden. "
                f"Siehe {blocker_path}."
            )

    if accepted_text is None:
        accepted_text = draft_text
    return section, accepted_text


def run_writer_loop(
    ctx: RunContext,
    beleg_index_path: Path = BELEG_INDEX_PATH,
    analyse_path: Path | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> Path:
    """Run the writer/reviewer/factcheck loop for all sections in parallel.

    All three sections (Management Summary, Schlüsselkompetenzen, Berufserfahrung)
    are processed concurrently — they are independent of each other. Each section
    internally runs its own reviewer ThreadPoolExecutor (hiring + coach in parallel).

    Concurrency model:
      - 3 outer threads (one per section) × 2 inner threads (reviewers) = up to 9
        concurrent threads, all blocked on network I/O.
      - All file writes are section-scoped (different filenames) — no write conflicts.
      - _run_log_lock (orchestrator) and _log_lock (llm) serialise shared writes.

    After all sections complete (or the first raises RuntimeError), consolidates
    accepted drafts into 04_final_de.md and returns its path.

    Args:
        progress_cb: Optional callable for section-level progress messages.
                     Pass click.echo in CLI context; omit for web/silent contexts.
    """
    if analyse_path is None:
        analyse_path = ctx.run_dir / "01_analyse.md"

    section_outputs: dict[str, str] = {}

    config_path = Path("config.yaml")
    concurrency = 1
    if config_path.exists():
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        concurrency = int(cfg.get("writer_section_concurrency", 1))
    concurrency = max(1, min(concurrency, len(SECTIONS)))

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _process_section, ctx, section, beleg_index_path, analyse_path, progress_cb
            ): section
            for section in SECTIONS
        }
        for future in as_completed(futures):
            section, accepted_text = future.result()  # re-raises RuntimeError on blocker
            section_outputs[section] = _strip_duplicate_section_heading(accepted_text, section)

    # Consolidate in canonical section order
    final_parts = []
    for section in SECTIONS:
        section_title = SECTION_TITLES[section]
        final_parts.append(f"## {section_title}\n\n{section_outputs[section]}")

    final_content = (
        f"# Finaler CV (DE)\n\n"
        f"**Run:** {ctx.run_id}\n"
        f"**Erstellt:** {datetime.now(timezone.utc).isoformat()}\n\n"
        + "\n\n---\n\n".join(final_parts)
        + "\n"
    )

    final_path = ctx.run_dir / "04_final_de.md"
    final_path.write_text(final_content, encoding="utf-8")
    write_run_log_entry(ctx.run_dir, "writer_loop", f"04_final_de.md geschrieben ({len(final_content)} Zeichen)")

    # Deterministic cross-section redundancy check: no reviewer ever sees
    # the assembled CV, so verbatim flagship-claim repetition between
    # Summary and Berufserfahrung goes unnoticed otherwise. Report-only —
    # repetition is a judgement call, the user decides.
    from cv_tailor.redundancy_check import write_redundancy_report
    redundancy_path = write_redundancy_report(ctx.run_dir, final_content)
    if redundancy_path is not None:
        write_run_log_entry(
            ctx.run_dir, "redundancy_check",
            "Wortgleiche Wiederholungen zwischen Abschnitten gefunden — _redundancy.md geschrieben",
        )
        log.warning("writer_loop.redundancy_found", run_id=ctx.run_id)

    log.info("writer_loop.done", run_id=ctx.run_id, chars=len(final_content))
    return final_path
