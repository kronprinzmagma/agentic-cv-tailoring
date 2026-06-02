"""Writer agent: produces a per-section CV proposal and writes it to 03_iterationen/<section>_v<round_num>.md."""
from __future__ import annotations

import re
from pathlib import Path

from cv_tailor.beleg_index import get_beleg_index_compact
from cv_tailor.clarifications import format_clarifications_for_prompt
from cv_tailor.cv_filename import CV_AUTHOR_TOKEN
from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

WRITER_PROMPT_PATH = Path("prompts/writer.md")
BELEG_INDEX_PATH = Path("data/beleg_index.json")
EXEMPLARS_DIR = Path("data/examples/optimized_cvs")
SECTIONS = ["management_summary", "schluesselkompetenzen", "berufserfahrung"]
MAX_ROUNDS = 2
MAX_TOKENS = 4096
PLACEHOLDER_MARKER = "USER FÜLLT"
META_MARKERS = (
    "Änderungen ggü.",
    "Änderungen gegenüber",
    "Begründung:",
    "Review-Zusammenfassung",
)


_LANG_LINE_RE = re.compile(
    r"^\s*\*{0,2}(Sprachen|Sprachkenntnisse|Languages?)\*{0,2}\s*[:：].*$",
    re.IGNORECASE,
)


def _strip_language_lines(text: str) -> str:
    """Remove stray Sprachen/Languages lines from writer output.

    Languages must appear only in the Skills & Tools section (rendered from
    the Standard-CV), never inside Management Summary, Schlüsselkompetenzen,
    or Berufserfahrung. The writer prompt forbids this but the model
    occasionally appends a `**Sprachen:** ...` line anyway.
    """
    kept: list[str] = []
    for line in text.splitlines():
        if _LANG_LINE_RE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def sanitize_writer_output(content: str) -> str:
    """Remove model-side review metadata that must not land in CV sections.

    Also normalises Em-dash (—) → En-dash (–): the goldstandard CVs use
    En-dash consistently; Em-dash is a strong LLM fingerprint that AI-
    detectors and experienced readers latch onto. Replacement is unambiguous
    in CV-prose context — there's no semantic difference Alex relies on.

    Section heading lines (## ... / ### ...) are left untouched so the
    Standard-CV's `###` station headers remain verbatim-matchable by the
    consistency check.
    """
    lines: list[str] = []
    for line in content.splitlines():
        if any(marker in line for marker in META_MARKERS):
            break
        lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "---":
        lines.pop()
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\s*\((?:BELG|BELEG)-\d+(?:,\s*(?:BELG|BELEG)-\d+)*\)", "", cleaned)
    cleaned = re.sub(r"\b(?:BELG|BELEG)-\d+\b", "", cleaned)
    cleaned = _strip_language_lines(cleaned)
    # Em-dash → En-dash, line-by-line, but leave heading lines (## / ###)
    # untouched so verbatim station headers from Standard-CV stay intact.
    out_lines: list[str] = []
    for line in cleaned.splitlines():
        if re.match(r"^\s*#{1,3}\s", line):
            out_lines.append(line)
        else:
            out_lines.append(line.replace("—", "–"))
    cleaned = "\n".join(out_lines)
    # Collapse any double-blank lines introduced by line removal
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() + "\n"


def _slugify(text: str) -> str:
    """Lowercase + alphanumeric only, for self-match exclusion."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def load_style_exemplars(
    section: str,
    exclude_posting_text: str = "",
    exemplars_dir: Path = EXEMPLARS_DIR,
    max_count: int = 2,
) -> str:
    """Load curated CV examples as style references for the writer.

    Returns a markdown block containing only the matching section from each
    exemplar — to keep token cost low and the focus on register/rhythm rather
    than wholesale content imitation.

    `max_count=2` (was 4 in early iterations): demonstration-based learning
    saturates fast. Two well-chosen exemplars transport the target register
    just as well as four, and the prompt prefix gets ~30% smaller. Bump back
    to 4 only if quality-trend metrics show the writer drifting toward
    wordiness across runs.

    `exclude_posting_text` is used to filter out self-match: if a posting
    string strongly references a company that has its own exemplar file
    (e.g. SMG, AutoRetailCo), that exemplar is dropped so the writer isn't fed
    its own gold standard during a self-test.

    Returns empty string if the exemplars directory is missing.
    """
    if not exemplars_dir.exists():
        return ""

    # Markdown heading patterns per section in the exemplar files
    section_headings = {
        "management_summary": ("Management Summary",),
        "schluesselkompetenzen": ("Schlüsselkompetenzen", "Core Competencies", "Key Competencies"),
        "berufserfahrung": ("Berufserfahrung", "Professional Experience", "Work Experience"),
    }
    wanted = section_headings.get(section, ())
    if not wanted:
        return ""

    exclude_slug = _slugify(exclude_posting_text[:200]) if exclude_posting_text else ""
    next_section_marker = re.compile(
        r"^\s*#+\s*(Schlüsselkompetenzen|Schluesselkompetenzen|Core Competencies|"
        r"Key Competencies|Berufserfahrung|Professional Experience|Work Experience|"
        r"Ausbildung|Education|Sprachkenntnisse|Languages|Software|Skills|Zertifikate)",
        re.I,
    )
    section_heading_re = re.compile(
        r"^\s*#+\s*(" + "|".join(re.escape(w) for w in wanted) + r")\s*$",
        re.I | re.M,
    )

    snippets: list[str] = []
    files = sorted(exemplars_dir.glob("*.md"))
    for fp in files:
        # Skip self-matches: drop exemplar files whose stem appears in the
        # current posting (loose substring match on slugified company names).
        # Strip both underscore- and space-separated forms of the author token
        # so the remaining stem holds only company + role tokens.
        _token_space = CV_AUTHOR_TOKEN.replace("_", " ")
        stem_slug = _slugify(
            fp.stem.replace(CV_AUTHOR_TOKEN, "").replace(_token_space, "")
        )
        # Stem typically contains "000000-companyname-role". Compare each
        # alphabetic run in the stem against the posting slug.
        # Derive author-name tokens dynamically so a different configured author
        # doesn't accidentally leak into company token comparisons.
        _author_words = {
            w.lower()
            for w in re.findall(r"[A-Za-z]{2,}", CV_AUTHOR_TOKEN.replace("CV_", "").replace("_", " "))
        }
        _role_filler_words = {"manager", "owner", "product"}
        company_tokens = [
            t for t in re.findall(r"[A-Za-z]{4,}", fp.stem)
            if t.lower() not in (_author_words | _role_filler_words)
        ]
        if exclude_slug and any(_slugify(tok) in exclude_slug for tok in company_tokens):
            continue

        text = fp.read_text(encoding="utf-8")
        match = section_heading_re.search(text)
        if not match:
            continue
        start = match.end()
        # Find the next section heading after this one
        tail = text[start:]
        next_match = next_section_marker.search(tail)
        section_body = tail[: next_match.start()] if next_match else tail
        section_body = section_body.strip()
        if not section_body:
            continue
        # Extract a clean label: drop the author token prefix and the
        # version anchor stem, keep what's left (role/company token).
        label = re.sub(r"^(CV[_ ]Alex[_ ]Müller[- _]?)?(\d{6}[- ]?)?", "", fp.stem).strip()
        if not label:
            label = fp.stem
        snippets.append(f"### Beispiel: {label}\n{section_body}")

        if len(snippets) >= max_count:
            break

    if not snippets:
        return ""
    header = (
        "## Stil-Beispiele für diesen Abschnitt (erfolgreich eingereicht / im Chat ausgearbeitet)\n\n"
        "Diese Beispiele zeigen das **Stilregister**, in dem Alex' CVs am Ende bestehen — "
        "Knappheit, Rhythmus, Selbstbewusstseinsgrad, kurze deklarative Sätze, gelegentliche "
        "rhetorische Kontraste wie *\"nicht nur X, sondern Y\"*. **Wortlaut nicht kopieren** — "
        "Register und Satz-Ökonomie übernehmen. Wenn dein Entwurf deutlich länger oder vorsichtiger "
        "klingt als diese Beispiele, schreib um.\n\n"
    )
    return header + "\n\n---\n\n".join(snippets)


def run_writer(
    ctx: RunContext,
    section: str,
    round_num: int,
    beleg_index_path: Path = BELEG_INDEX_PATH,
    analyse_path: Path | None = None,
    prompt_path: Path = WRITER_PROMPT_PATH,
) -> Path:
    """Run the writer agent for a given section and round, writing a versioned iteration file.

    Returns the path to the written iteration file.
    """
    # Round guard (first thing)
    if round_num > MAX_ROUNDS:
        raise ValueError(
            f"run_writer: round_num={round_num} exceeds MAX_ROUNDS={MAX_ROUNDS} "
            f"for section '{section}'"
        )
    if section not in SECTIONS:
        raise ValueError(f"Unknown section '{section}'. Valid: {SECTIONS}")

    # Load prompt (cached after first call)
    system_prompt = load_prompt(prompt_path)
    if PLACEHOLDER_MARKER in system_prompt:
        log.warning("run_writer.placeholder_prompt", section=section)

    # Load analyse.md
    if analyse_path is None:
        analyse_path = ctx.run_dir / "01_analyse.md"
    if not analyse_path.exists():
        raise FileNotFoundError(f"01_analyse.md not found in {ctx.run_dir}")
    analyse_text = analyse_path.read_text(encoding="utf-8")

    # Load beleg_index (cached after first call across all sections/rounds)
    beleg_index_compact = get_beleg_index_compact(beleg_index_path)

    # Load 02_antworten.md (optional)
    antworten_path = ctx.run_dir / "02_antworten.md"
    antworten_text = antworten_path.read_text(encoding="utf-8") if antworten_path.exists() else ""

    activation_path = ctx.run_dir / "_experience_activation.md"
    activation_text = activation_path.read_text(encoding="utf-8") if activation_path.exists() else ""

    # Load previous round output (if round_num > 1)
    prev_path = ctx.run_dir / "03_iterationen" / f"{section}_v{round_num - 1}.md"
    prev_text = prev_path.read_text(encoding="utf-8") if prev_path.exists() else ""
    prev_hiring_path = ctx.run_dir / "03_iterationen" / f"{section}_v{round_num - 1}_review_hiring.md"
    prev_coach_path = ctx.run_dir / "03_iterationen" / f"{section}_v{round_num - 1}_review_coach.md"
    prev_factcheck_path = ctx.run_dir / "03_iterationen" / f"{section}_v{round_num - 1}_factcheck.md"
    prev_consistency_path = ctx.run_dir / "03_iterationen" / f"{section}_v{round_num - 1}_consistency.md"
    prev_length_path = ctx.run_dir / "03_iterationen" / f"{section}_v{round_num - 1}_length.md"
    prev_hiring_text = prev_hiring_path.read_text(encoding="utf-8") if prev_hiring_path.exists() else ""
    prev_coach_text = prev_coach_path.read_text(encoding="utf-8") if prev_coach_path.exists() else ""
    prev_factcheck_text = prev_factcheck_path.read_text(encoding="utf-8") if prev_factcheck_path.exists() else ""
    prev_consistency_text = prev_consistency_path.read_text(encoding="utf-8") if prev_consistency_path.exists() else ""
    prev_length_text = prev_length_path.read_text(encoding="utf-8") if prev_length_path.exists() else ""

    # Build user message split into static (cached) and dynamic parts.
    #
    # Static context is identical for all sections/rounds within a run:
    #   analyse + activation_map + beleg_index + antworten + clarifications
    # Anthropic caches this prefix after the first writer call, so the 2nd–6th
    # calls (3 sections × up to 2 rounds) pay only ~10% of those input tokens.
    # call_llm._prepare_messages_for_provider flattens the content array back
    # to a plain string for non-Anthropic providers transparently.
    # Topic-gate clarifications against posting + analysis so cross-context
    # past answers don't leak names/groups into bullets whose belegs don't
    # mention them (CLAUDE.md "Cross-Beleg-Fusion-Guard"). The gating
    # context comes from a shared helper so writer, factcheck and coach
    # all derive identical Anthropic cache keys for the same inputs.
    from cv_tailor.prompt_context import build_gating_context
    clarifications_text = format_clarifications_for_prompt(
        current_context=build_gating_context(ctx) or None,
    )
    static_parts = [f"## Analyse\n{analyse_text}"]
    if activation_text:
        static_parts.append(f"## Experience Activation Map\n{activation_text}")
    static_parts.append(f"## Beleg-Index\n{beleg_index_compact}")
    if antworten_text:
        static_parts.append(f"## Antworten auf Klärungsfragen\n{antworten_text}")
    if clarifications_text:
        static_parts.append(f"## Zusatzkontext aus früheren Klärungen\n{clarifications_text}")

    # Style exemplars: 2 curated CV sections from data/examples/optimized_cvs/.
    # The exemplars communicate target register / rhythm / brevity through
    # demonstration rather than prose rules. Self-matches (e.g. SMG exemplar
    # during an SMG run) are filtered out via posting-text overlap.
    #
    # IMPORTANT: exemplars live in dynamic_parts (not static), because they
    # are *section-specific* (Summary exemplars differ from Berufserfahrung
    # exemplars). Putting them in static_parts created three separate
    # Anthropic-Cache lines per run (one per section) and dropped the writer
    # cache-hit rate to 48%. Moving them post-cache-cut keeps the long static
    # prefix (analysis + activation + beleg-index + clarifications) identical
    # across all section iterations → cache hits go to 80%+.
    posting_path = ctx.run_dir / "00_stellenanzeige.md"
    posting_text = posting_path.read_text(encoding="utf-8") if posting_path.exists() else ""
    exemplars_text = load_style_exemplars(section, exclude_posting_text=posting_text)

    # Dynamic context changes per section and round:
    #   section name + style exemplars (per section) + optional previous draft + reviewer feedback
    dynamic_parts = [f"## Abschnitt\n{section}"]
    if exemplars_text:
        dynamic_parts.append(exemplars_text)
    if prev_text:
        dynamic_parts.append(f"## Vorheriger Entwurf (Runde {round_num - 1})\n{prev_text}")
    if prev_hiring_text:
        dynamic_parts.append(f"## Hiring-Manager-Review zur vorherigen Runde\n{prev_hiring_text}")
    if prev_coach_text:
        dynamic_parts.append(f"## Coach-Review zur vorherigen Runde\n{prev_coach_text}")
    if prev_factcheck_text:
        dynamic_parts.append(f"## Faktencheck zur vorherigen Runde (Pflichtkorrektur)\n{prev_factcheck_text}")
    if prev_length_text:
        dynamic_parts.append(f"## Längen-Check zur vorherigen Runde (Pflichtkorrektur)\n{prev_length_text}")
    if prev_consistency_text:
        dynamic_parts.append(f"## Konsistenz-Check zur vorherigen Runde (Pflichtkorrektur)\n{prev_consistency_text}")

    user_msg: list[dict] | str
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

    log.info("run_writer.start", run_id=ctx.run_id, section=section, round_num=round_num)
    content = call_llm(
        agent="writer",
        phase="phase4_writer",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        iteration=round_num,
        snippet_text=analyse_text[:500],
    )
    content = sanitize_writer_output(content)

    # Write output
    iter_dir = ctx.run_dir / "03_iterationen"
    iter_dir.mkdir(exist_ok=True)
    out_path = iter_dir / f"{section}_v{round_num}.md"
    out_path.write_text(content, encoding="utf-8")
    write_run_log_entry(ctx.run_dir, "writer", f"{section}_v{round_num}.md geschrieben ({len(content)} Zeichen)")
    log.info("run_writer.done", run_id=ctx.run_id, section=section, round_num=round_num, chars=len(content))
    return out_path
