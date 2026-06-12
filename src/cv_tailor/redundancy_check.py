"""Deterministic cross-section redundancy check on the assembled final CV.

Why this exists
---------------
Coach and Hiring-Reviewer review one section at a time — no reviewer ever
sees the assembled CV. Flagship claims ("zwei Wochen vor Termin und 20%
unter Budget", "KI-Fachgruppe aufgebaut") therefore recur verbatim in
Summary AND Berufserfahrung; 4 of the 5 runs reviewed on 2026-06-12 had
this. Verbatim repetition wastes the scarcest resource in a CV: space and
recruiter attention.

This check is the deterministic safety net behind the writer-prompt rule
("Keine wortgleiche Wiederholung über Abschnittsgrenzen"): it scans the
final CV for shared word n-grams between the Management Summary and the
other sections and writes a human-readable report to `_redundancy.md`.
It does not modify the CV — repetition is a judgement call (sometimes a
fact legitimately belongs in both places), so the user decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# A shared run of >= NGRAM_SIZE words counts as verbatim repetition. Eight
# words is long enough that incidental phrase overlap ("Führung eines
# interdisziplinären Teams") doesn't fire, short enough to catch the real
# flagship-claim duplications observed in runs.
NGRAM_SIZE = 8

_WORD_RE = re.compile(r"\b[\wÀ-ɏ%+]+\b")


@dataclass(frozen=True)
class RedundancyFinding:
    section: str   # section title where the repetition recurs
    phrase: str    # the repeated word sequence (normalised)


def _normalise_words(text: str) -> list[str]:
    """Markdown-stripped, lowercased word list."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return [w.lower() for w in _WORD_RE.findall(text)]


def _split_sections(final_cv_text: str) -> dict[str, str]:
    """Split the assembled CV into `## <title>` sections."""
    sections: dict[str, str] = {}
    current_title: str | None = None
    current_lines: list[str] = []
    for line in final_cv_text.splitlines():
        m = re.match(r"^## (.+)$", line.strip())
        if m:
            if current_title is not None:
                sections[current_title] = "\n".join(current_lines)
            current_title = m.group(1).strip()
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        sections[current_title] = "\n".join(current_lines)
    return sections


def _shared_phrases(base_words: list[str], other_words: list[str], n: int) -> list[str]:
    """Maximal word runs of length >= n appearing in both texts."""
    base_ngrams = {
        tuple(base_words[i:i + n]) for i in range(len(base_words) - n + 1)
    }
    phrases: list[str] = []
    i = 0
    while i <= len(other_words) - n:
        if tuple(other_words[i:i + n]) in base_ngrams:
            # extend the match greedily to its maximal length
            j = i + n
            while j < len(other_words) and tuple(other_words[j - n + 1:j + 1]) in base_ngrams:
                j += 1
            phrases.append(" ".join(other_words[i:j]))
            i = j
        else:
            i += 1
    return phrases


def check_cross_section_redundancy(
    final_cv_text: str, *, ngram_size: int = NGRAM_SIZE,
) -> list[RedundancyFinding]:
    """Find verbatim word runs shared between Management Summary and the rest."""
    sections = _split_sections(final_cv_text)
    summary_title = next(
        (t for t in sections if "summary" in t.lower()), None,
    )
    if summary_title is None:
        return []
    summary_words = _normalise_words(sections[summary_title])
    findings: list[RedundancyFinding] = []
    for title, body in sections.items():
        if title == summary_title:
            continue
        for phrase in _shared_phrases(summary_words, _normalise_words(body), ngram_size):
            findings.append(RedundancyFinding(section=title, phrase=phrase))
    return findings


def format_findings(findings: list[RedundancyFinding]) -> str:
    """Human-readable `_redundancy.md` report."""
    lines = [
        "# Redundanz-Hinweise",
        "",
        "Folgende Formulierungen aus der Management Summary kehren wortgleich "
        "in anderen Abschnitten wieder. Wiederholung ist nicht automatisch falsch — "
        "aber wortgleiche Doppelung verschenkt Platz. Empfehlung: an einer der "
        "beiden Stellen anders zuschneiden oder streichen.",
        "",
    ]
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. **{f.section}**: „{f.phrase}“")
    return "\n".join(lines) + "\n"


def write_redundancy_report(run_dir: Path, final_cv_text: str) -> Path | None:
    """Run the check and persist `_redundancy.md` if anything was found.

    Returns the report path, or None when the CV is clean (no file written —
    absence of the file means absence of findings).
    """
    findings = check_cross_section_redundancy(final_cv_text)
    if not findings:
        return None
    report_path = run_dir / "_redundancy.md"
    report_path.write_text(format_findings(findings), encoding="utf-8")
    return report_path
