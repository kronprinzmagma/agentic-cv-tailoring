"""Deterministic length-budget checks for writer drafts.

Catches the two most common length-driven rounds-2:
- **Management Summary >140 Wörter** (Schreibziel laut prompts/writer.md: 110–130)
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
#
# Summary bounds are DELIBERATELY wider than the prompt's target corridor
# (110–130): the prompt states the goal, this check is the hard-fail band.
# The gap is hysteresis — a 105-word summary should not cost a writer round.
# Not a sync error; if you tighten one side, decide about the other.
# Tightened 2026-10-23 after the interview-case benchmark: raw summaries ran
# 108–146 words against 109–139 in the CVs Alex sent.
# 2026-11-16 (Monatsreview Oktober): with target == cap (140) every v1 since
# the rebuild overshot (128–192 words, median 163) and cost a round. The
# writing target is now 110–130 with the cap unchanged, and the paragraph caps
# sum to 135 (before: 60/50/40 = 150, more than the budget they served).
SUMMARY_TARGET = "110–130"
SUMMARY_MAX_WORDS = 140
SUMMARY_MIN_WORDS = 100
SUMMARY_PARAGRAPH_MAX_WORDS = (55, 45, 35)
SUMMARY_SENTENCE_MAX_WORDS = 22
BULLET_MAX_WORDS = 22

# The Eigenregie anchor bullet carries a link and must stay verbatim (consistency
# rule 7), so its length is not the writer's to fix. Only bullets with a link are
# exempt: that is how the anchor is defined.
_LINK = re.compile(r"https?://|github\.com/|www\.")
KOMPETENZEN_MAX_COUNT = 6
# Check at 130 words. Interview-case benchmark 2026-10-23: raw drafts ran
# 132–147 words against 117–130 in the CVs Alex sent.
# 2026-11-16: a total alone is not countable while writing — v1 ran 104–195
# words, v2 stayed above the cap in 3 of 3 October runs. The prompt now states
# a per-point target (countable, like the bullet rule), and an over-budget
# finding names the points above it. A long point alone is no veto: Alex'
# sent CVs carry points of up to 25 words inside a 112–130 total.
KOMPETENZEN_MAX_WORDS = 130
KOMPETENZ_POINT_TARGET_WORDS = 20


@dataclass(frozen=True)
class LengthIssue:
    # "summary_overlong" | "summary_too_short" | "bullet_overlong"
    # | "kompetenzen_too_many" | "kompetenzen_overlong"
    kind: str
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


def _paragraphs(draft: str) -> list[str]:
    """Prose paragraphs of a section draft, headings dropped."""
    return [
        p.strip() for p in re.split(r"\n\s*\n", _strip_markdown(draft).strip())
        if p.strip() and not p.strip().startswith("#")
    ]


def _summary_paragraph_hint(draft: str) -> str:
    """Name the paragraphs above their cap, so the writer knows where to cut."""
    over = [
        f"Absatz {i} hat {wc} Wörter (max. {cap})"
        for i, (wc, cap) in enumerate(
            zip(
                (_word_count(p) for p in _paragraphs(draft)),
                SUMMARY_PARAGRAPH_MAX_WORDS,
                strict=False,  # a draft may have fewer or more than three paragraphs
            ),
            1,
        )
        if wc > cap
    ]
    return (" " + "; ".join(over) + ".") if over else ""


def check_summary_length(
    draft: str,
    *,
    max_words: int = SUMMARY_MAX_WORDS,
    min_words: int = SUMMARY_MIN_WORDS,
) -> list[LengthIssue]:
    """Veto when the Management Summary draft leaves the word corridor."""
    plain = _strip_markdown(draft)
    wc = _word_count(plain)
    if wc > max_words:
        return [LengthIssue(
            kind="summary_overlong",
            detail=(
                f"Management Summary hat {wc} Wörter — Budget ist {max_words}, "
                f"Zielkorridor {SUMMARY_TARGET}. Kürze um {wc - max_words}+ Wörter "
                "durch Streichen ganzer Sätze, nicht Umformulieren."
                + _summary_paragraph_hint(draft)
            ),
            word_count=wc,
        )]
    if wc < min_words:
        return [LengthIssue(
            kind="summary_too_short",
            detail=(
                f"Management Summary hat nur {wc} Wörter — Zielkorridor ist {SUMMARY_TARGET}. "
                "Meist fehlt Absatz 3 (Differenzierung) oder ein konkreter Beleg in "
                "Absatz 2. Ergänze Substanz, keinen Füllstoff."
            ),
            word_count=wc,
        )]
    return []


def check_kompetenzen_count(
    draft: str, *, max_count: int = KOMPETENZEN_MAX_COUNT,
) -> list[LengthIssue]:
    """Veto when Schlüsselkompetenzen has more than `max_count` entries.

    Seven entries plus a long summary produced a 4-page PDF overflow
    (kantonluz 2026-06-08). Entries are recognised by the prompt-mandated
    format: a line starting with a bold headline (`**…**`).
    """
    count = sum(
        1 for line in draft.splitlines()
        if line.strip().startswith("**") and "**" in line.strip()[2:]
    )
    if count <= max_count:
        return []
    return [LengthIssue(
        kind="kompetenzen_too_many",
        detail=(
            f"Schlüsselkompetenzen hat {count} Punkte — Maximum ist {max_count}. "
            f"Streiche die {count - max_count} schwächsten Punkte ersatzlos "
            "(nicht zusammenlegen — das erzeugt überlange Sammel-Punkte)."
        ),
        word_count=count,
    )]


def kompetenz_points(draft: str) -> list[tuple[str, int]]:
    """(headline, word count incl. headline) per Schlüsselkompetenz point."""
    points: list[tuple[str, int]] = []
    for raw in draft.splitlines():
        line = raw.strip().lstrip("-* ").strip() if raw.strip()[:2] in ("- ", "* ") else raw.strip()
        m = re.match(r"\*\*(.+?)\*\*", line)
        if not m:
            continue
        points.append((m.group(1).strip(), _word_count(_strip_markdown(line))))
    return points


def check_kompetenzen_words(
    draft: str,
    *,
    max_words: int = KOMPETENZEN_MAX_WORDS,
    point_target: int = KOMPETENZ_POINT_TARGET_WORDS,
) -> list[LengthIssue]:
    """Veto when Schlüsselkompetenzen exceed the word budget.

    The veto condition is the total only. The finding names the points above
    the per-point target, longest first, so round 2 cuts those instead of
    shaving a word everywhere.
    """
    wc = _word_count(_strip_markdown(draft))
    if wc <= max_words:
        return []
    long_points = sorted(
        (p for p in kompetenz_points(draft) if p[1] > point_target),
        key=lambda p: -p[1],
    )
    where = ""
    if long_points:
        where = " Zu lang: " + "; ".join(
            f"«{headline}» {count} Wörter" for headline, count in long_points
        ) + f" (Ziel pro Punkt: höchstens {point_target}, Headline mitgezählt)."
    return [LengthIssue(
        kind="kompetenzen_overlong",
        detail=(
            f"Schlüsselkompetenzen haben {wc} Wörter — Budget ist {max_words}. "
            f"Pro Punkt eine belegte Handlung statt zwei; kürzen um "
            f"{wc - max_words}+ Wörter durch Streichen.{where}"
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
        if wc <= max_words or _LINK.search(bullet):
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
    if section == "schluesselkompetenzen":
        return check_kompetenzen_count(draft) + check_kompetenzen_words(draft)
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
