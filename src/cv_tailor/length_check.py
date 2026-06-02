"""Deterministic length-budget checks for writer drafts.

Catches the two most common length-driven rounds-2:
- **Management Summary >170 Wörter** (Zielkorridor laut CLAUDE.md: 140–160)
- **Berufserfahrung-Bullets >22 Wörter** (Recruiter-Scan-Test, Anti-3-Konzepte)

Both are *soft* vetos — they request a revision but don't permanently block
output. After MAX_ROUNDS the latest draft is accepted (siehe writer_loop).
The check writes findings to `03_iterationen/<section>_v<N>_length.md` so
the writer sees them as Pflichtkorrektur in round 2.

Why deterministic, not LLM-judged: word counts are exact, no probability
involved. Sparing an LLM call for what regex can decide is cheap quality
discipline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# Budgets per section. Keep aligned with prompts/writer.md.
SUMMARY_MAX_WORDS = 170
BULLET_MAX_WORDS = 22


@dataclass(frozen=True)
class LengthIssue:
    kind: str           # "summary_overlong" | "bullet_overlong"
    detail: str         # human-readable description
    word_count: int


_WORD_RE = re.compile(r"\b[\wÀ-ɏ]+\b")


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _strip_markdown(text: str) -> str:
    """Remove markup so word counts reflect prose, not asterisks/dashes."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # bolds
    text = re.sub(r"\*([^*]+)\*", r"\1", text)       # italics
    text = re.sub(r"\[(.+?)\]\([^)]+\)", r"\1", text)  # md links
    return text


def check_summary_length(draft: str, *, max_words: int = SUMMARY_MAX_WORDS) -> list[LengthIssue]:
    """Veto when the Management Summary draft exceeds the word budget."""
    plain = _strip_markdown(draft)
    wc = _word_count(plain)
    if wc <= max_words:
        return []
    return [LengthIssue(
        kind="summary_overlong",
        detail=(
            f"Management Summary hat {wc} Wörter — Budget ist {max_words}, "
            f"Zielkorridor 140–160. Kürze um {wc - max_words}+ Wörter "
            "durch Streichen ganzer Sätze, nicht Umformulieren."
        ),
        word_count=wc,
    )]


def _iter_bullets(draft: str) -> list[tuple[str, str]]:
    """Yield (station_header_or_empty, bullet_text) pairs from the Berufserfahrung draft.

    The writer produces a mix of paragraph-style and `-`-bullet style.
    Both count as one bullet for length-check purposes.
    """
    plain = _strip_markdown(draft)
    bullets: list[tuple[str, str]] = []
    current_header = ""
    for raw in plain.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("###"):
            current_header = line.lstrip("# ").strip()
            continue
        if line.startswith("##"):
            current_header = ""
            continue
        if line.startswith("---"):
            continue
        # Bullet-style ("- ...") or paragraph
        if line.startswith("- ") or line.startswith("* "):
            line = line[2:].strip()
        bullets.append((current_header, line))
    return bullets


def check_berufserfahrung_bullet_lengths(
    draft: str, *, max_words: int = BULLET_MAX_WORDS, max_offenders: int = 6,
) -> list[LengthIssue]:
    """Veto when any Berufserfahrungs-Bullet exceeds the word budget.

    Returns one LengthIssue per offending bullet so the writer sees them
    individually in the round-2 feedback (with header context, so it knows
    which station to fix).
    """
    issues: list[LengthIssue] = []
    for header, bullet in _iter_bullets(draft):
        wc = _word_count(bullet)
        if wc <= max_words:
            continue
        preview = bullet[:140] + ("…" if len(bullet) > 140 else "")
        station_label = f" ({header[:50]})" if header else ""
        issues.append(LengthIssue(
            kind="bullet_overlong",
            detail=f"Bullet hat {wc} Wörter (max {max_words}){station_label}: \"{preview}\"",
            word_count=wc,
        ))
        if len(issues) >= max_offenders:
            issues.append(LengthIssue(
                kind="bullet_overlong",
                detail=f"… (weitere zu lange Bullets unterdrückt — Limit {max_offenders} im Feedback)",
                word_count=0,
            ))
            break
    return issues


def check_section_length(section: str, draft: str) -> list[LengthIssue]:
    """Dispatch to the right per-section length checker."""
    if section == "management_summary":
        return check_summary_length(draft)
    if section == "berufserfahrung":
        return check_berufserfahrung_bullet_lengths(draft)
    return []


def format_issues_for_writer(issues: Sequence[LengthIssue]) -> str:
    """Format issues as Markdown for both the writer's round-2 context and audit logging."""
    if not issues:
        return "# Längen-Check\n\nKeine Längen-Probleme gefunden.\n"
    lines = ["# Längen-Check", ""]
    lines.append("Folgende Längen-Budgets wurden überschritten. **Pflichtkorrektur**: durch Streichen — nicht Umformulieren — auf das Budget bringen.\n")
    for i, issue in enumerate(issues, 1):
        lines.append(f"{i}. {issue.detail}")
    return "\n".join(lines) + "\n"
