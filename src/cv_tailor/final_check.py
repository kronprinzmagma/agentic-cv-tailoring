"""Deterministic final-artifact validation for post-factcheck transformations.

Trim and translation run AFTER the factcheck/consistency gates. Both can
degrade or destroy an already-validated CV — an empty LLM response, a dropped
station, a number that drifted in translation. This module closes that chain
with cheap, deterministic guards (no LLM calls):

- `validate_trimmed_cv`: the trim pass must never empty the CV, halve it,
  drop a required section, or lose a Berufserfahrung station.
- `validate_translation`: the EN CV must carry the same stations as the DE
  CV and not be empty/truncated (fatal); number drift and lost keyword-marker
  bolds between DE and EN are reported as warnings.

Fatal findings mean the transformed artifact is rejected and the caller
falls back to the validated predecessor. Warnings are report-only.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from cv_tailor.consistency_check import (
    _structurally_station_like,
    parse_generated_header,
)

# Section headings the writer always produces in the DE CV. The trim pass
# must preserve every one that the original contained.
REQUIRED_SECTIONS_DE = ("Management Summary", "Schlüsselkompetenzen", "Berufserfahrung")

# A trim pass cuts from >3 pages down to 3 — realistic cuts are 10–40%.
# Anything below half the original signals a broken response, not a trim.
MIN_TRIM_RATIO = 0.5
# EN translations of DE business prose track the original length closely;
# below half the DE length the output is truncated or partial.
MIN_TRANSLATION_RATIO = 0.5
# The keyword marker runs on the DE CV *before* the translator, so its
# `**Begriff**` markers have to survive the translation. Observed reality:
# most runs carry them over 1:1, but the translator occasionally strips them
# wholesale (2026-05-11 fintechco 7→0, 2026-08-24 archlet 18→0) — silently,
# because nothing else inspects formatting. Losing more than this share of
# the markers is a warning, never fatal: the text itself is intact.
MAX_BOLD_LOSS_RATIO = 0.3

_NUMBER_RUN_RE = re.compile(r"\d+")
_BOLD_RUN_RE = re.compile(r"\*\*[^*\n]+\*\*")

# Metadata preamble lines in 04_final_de.md ("**Run:** …", "**Erstellt:** …")
# carry run-id timestamps the translator correctly drops — they are not CV
# content and must not pollute the DE↔EN number comparison.
_METADATA_LINE_RE = re.compile(r"^\s*\*\*(Run|Erstellt|Created)\s*:\*\*", re.IGNORECASE)


def _strip_metadata_lines(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not _METADATA_LINE_RE.match(line)
    )


def extract_station_companies(text: str) -> set[str]:
    """Return the set of canonical company keys of all station headers.

    Uses the consistency-check parser: only `###`-level, pipe-separated
    headers with a year qualify. Companies that carry no known token
    (invented or renamed beyond recognition) parse to None and are simply
    absent from the set — a DE↔EN comparison then surfaces the loss.
    """
    found: set[str] = set()
    for line in text.splitlines():
        if not _structurally_station_like(line):
            continue
        parsed = parse_generated_header(line)
        if parsed is not None:
            found.add(parsed[0])
    return found


def station_headers(text: str) -> tuple[Counter, dict[str, str], list[str]]:
    """Station headers of a CV: (count per company key, first header line per
    key, station-shaped headers no known company matches).

    `extract_station_companies` is a set and drops unknown headers; an added
    invented station or a duplicated one passed unnoticed (review 2026-10-23,
    R04).
    """
    counts: Counter = Counter()
    first_line: dict[str, str] = {}
    unknown: list[str] = []
    for line in text.splitlines():
        if not _structurally_station_like(line):
            continue
        parsed = parse_generated_header(line)
        if parsed is None:
            unknown.append(line.strip())
            continue
        counts[parsed[0]] += 1
        first_line.setdefault(parsed[0], line.strip())
    return counts, first_line, unknown


def extract_number_runs(text: str) -> Counter:
    """Multiset of digit runs in `text`.

    Digit runs are language-invariant: "10'000" (DE) and "10,000" (EN) both
    yield {10, 000}; "3,5 Mio." and "3.5 million" both yield {3, 5}. Spelled
    numbers ("zwei" ↔ "two") produce no runs on either side and cancel out.
    Metadata preamble lines are stripped first.
    """
    return Counter(_NUMBER_RUN_RE.findall(_strip_metadata_lines(text)))


def count_bold_runs(text: str) -> int:
    """Number of `**…**` markers in `text`, metadata preamble excluded.

    Counts markers, not distinct terms — a term bolded twice counts twice,
    which is what the DE↔EN comparison needs. Runs spanning a newline are
    not counted: those are unbalanced markers, not intentional emphasis.
    """
    return len(_BOLD_RUN_RE.findall(_strip_metadata_lines(text)))


def validate_trimmed_cv(original: str, trimmed: str) -> tuple[bool, list[str]]:
    """Deterministic acceptance check for the writer-trim output.

    Returns (ok, issues). Any issue → caller keeps the original.
    """
    issues: list[str] = []

    if not trimmed.strip():
        return False, ["Trim-Ergebnis ist leer."]

    ratio = len(trimmed) / max(len(original), 1)
    if ratio < MIN_TRIM_RATIO:
        issues.append(
            f"Trim-Ergebnis ist zu kurz ({len(trimmed)} von {len(original)} "
            f"Zeichen, {ratio:.0%}) — unter der {MIN_TRIM_RATIO:.0%}-Grenze."
        )

    for section in REQUIRED_SECTIONS_DE:
        if section.lower() in original.lower() and section.lower() not in trimmed.lower():
            issues.append(f"Pflichtabschnitt '{section}' fehlt nach dem Trim.")

    original_stations = extract_station_companies(original)
    trimmed_stations = extract_station_companies(trimmed)
    for company in sorted(original_stations - trimmed_stations):
        issues.append(f"Berufserfahrungs-Station '{company}' fehlt nach dem Trim.")
    for company in sorted(trimmed_stations - original_stations):
        issues.append(f"Berufserfahrungs-Station '{company}' ist nach dem Trim neu — nicht erlaubt.")

    # Trimming may drop claims, never change them (review 2026-10-23, R03):
    # headers stay verbatim, no digit run may appear that the original lacks,
    # no new fact-limit finding, no unknown or duplicated station.
    orig_counts, orig_lines, orig_unknown = station_headers(original)
    trim_counts, trim_lines, trim_unknown = station_headers(trimmed)
    for key, line in trim_lines.items():
        if key in orig_lines and line != orig_lines[key]:
            issues.append(f"Stations-Header verändert: '{orig_lines[key]}' → '{line}'.")
    for key, n in trim_counts.items():
        if n > orig_counts.get(key, 0):
            issues.append(f"Station '{key}' kommt nach dem Trim öfter vor als vorher.")
    for line in trim_unknown:
        if line not in orig_unknown:
            issues.append(f"Unbekannte Station nach dem Trim: '{line}'.")
    new_numbers = extract_number_runs(trimmed) - extract_number_runs(original)
    for run, count in sorted(new_numbers.items()):
        issues.append(f"Zahl '{run}' ist nach dem Trim neu oder verändert ({count}×).")
    from cv_tailor.fact_limits import check_text, load_fact_limits

    limits = load_fact_limits()
    before = Counter(f.limit_id for f in check_text(original, limits))
    for limit_id, n in (Counter(f.limit_id for f in check_text(trimmed, limits)) - before).items():
        issues.append(f"Faktengrenze '{limit_id}' nach dem Trim neu verletzt ({n}×).")

    return len(issues) == 0, issues


@dataclass
class TranslationCheck:
    """Result of the DE↔EN final validation."""
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fatal


def validate_translation(de_text: str, en_text: str) -> TranslationCheck:
    """Deterministic acceptance check for the translator output.

    Fatal (EN artifact is rejected, DE remains the shipped CV):
    - empty or truncated output (< MIN_TRANSLATION_RATIO of the DE length)
    - Berufserfahrung station set differs from the DE CV (dropped, invented,
      or renamed-beyond-recognition employer)

    Warnings (EN is written, findings go to _translation_check.md):
    - digit runs present in DE but missing in EN, or vice versa. Spelled-out
      conversions ("zwei Wochen" → "two weeks") cancel out; "zwei" → "2"
      produces a warning by design — a human glance decides.
    """
    check = TranslationCheck()

    if not en_text.strip():
        check.fatal.append("Übersetzung ist leer.")
        return check

    ratio = len(en_text) / max(len(de_text), 1)
    if ratio < MIN_TRANSLATION_RATIO:
        check.fatal.append(
            f"Übersetzung ist zu kurz ({len(en_text)} von {len(de_text)} "
            f"Zeichen, {ratio:.0%}) — vermutlich abgeschnitten."
        )

    de_stations = extract_station_companies(de_text)
    en_stations = extract_station_companies(en_text)
    for company in sorted(de_stations - en_stations):
        check.fatal.append(
            f"Station '{company}' aus dem DE-CV fehlt im EN-CV "
            f"(entfernt oder Firmenname nicht mehr erkennbar)."
        )
    for company in sorted(en_stations - de_stations):
        check.fatal.append(f"Station '{company}' im EN-CV hat keine DE-Entsprechung.")
    de_counts, _, de_unknown = station_headers(de_text)
    en_counts, _, en_unknown = station_headers(en_text)
    if len(en_unknown) > len(de_unknown):
        for line in en_unknown:
            check.fatal.append(f"Station im EN-CV keiner bekannten Firma zuordenbar: '{line}'.")
    for key, n in en_counts.items():
        if n > de_counts.get(key, 0):
            check.fatal.append(f"Station '{key}' kommt im EN-CV öfter vor als im DE-CV.")

    de_bolds = count_bold_runs(de_text)
    en_bolds = count_bold_runs(en_text)
    if de_bolds:
        lost = (de_bolds - en_bolds) / de_bolds
        if lost > MAX_BOLD_LOSS_RATIO:
            check.warnings.append(
                f"Fettmarkierungen: DE {de_bolds}, EN {en_bolds} "
                f"({lost:.0%} verloren) — der Translator hat die Keyword-Marker "
                f"nicht übernommen. Keyword-Marker auf 04_final_en.md nachziehen."
            )

    de_numbers = extract_number_runs(de_text)
    en_numbers = extract_number_runs(en_text)
    for run, count in sorted((de_numbers - en_numbers).items()):
        check.warnings.append(
            f"Zahl '{run}' kommt im DE-CV {count}× öfter vor als im EN-CV."
        )
    for run, count in sorted((en_numbers - de_numbers).items()):
        check.warnings.append(
            f"Zahl '{run}' kommt im EN-CV {count}× öfter vor als im DE-CV."
        )

    return check


def format_translation_report(check: TranslationCheck) -> str:
    """Render the check result as _translation_check.md content."""
    lines = ["# Übersetzungs-Endprüfung (deterministisch)", ""]
    if check.fatal:
        lines.append("**Status: VERWORFEN** — 04_final_en.md wurde nicht geschrieben.")
        lines.append("")
        lines.append("## Fatale Befunde")
        lines.append("")
        for issue in check.fatal:
            lines.append(f"- {issue}")
        lines.append("")
    if check.warnings:
        lines.append("## Hinweise (Abgleich DE ↔ EN)")
        lines.append("")
        for warning in check.warnings:
            lines.append(f"- {warning}")
        lines.append("")
        lines.append(
            "_Ziffernfolgen sind sprachinvariant — ausgeschriebene Zahlen "
            "('zwei' ↔ 'two') erscheinen hier nicht. Jeder Eintrag verdient "
            "einen kurzen Blick, ist aber nicht zwingend ein Fehler._"
        )
        lines.append("")
    return "\n".join(lines)
