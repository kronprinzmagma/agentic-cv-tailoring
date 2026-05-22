"""Profile-Fit-Check: parse the analyst's Anforderungsabgleich table and
surface unmet requirements before the writer-loop starts.

Why this exists
---------------
A poorly-fitting role can only be "tailored" by stretching claims. The
writer-loop has no choice but to fill in what isn't there — which is
exactly the overreach pattern we keep fighting. This module gives the
user an early stop sign: if the analyst already classified critical
requirements as LÜCKE, ask before paying for a full optimisation run.

Deterministic, no LLM call. Reads the table from `01_analyse.md` and
returns a list of unmet requirements with their analyst comment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FitGap:
    requirement: str
    status: str  # "LÜCKE" | "SCHWACH"
    comment: str
    severity: str  # "critical" | "soft"


# Words in the analyst's Kommentar that strongly suggest the requirement is
# optional / nice-to-have. When present, we downgrade severity rather than
# treating the row as a hard mismatch.
_NICE_TO_HAVE_MARKERS = (
    "nice to have",
    "nice-to-have",
    "wünschenswert",
    "von vorteil",
    "kann-anforderung",
    "kann anforderung",
    "plus",
    "bonus",
    "optional",
)

# Words in the requirement text that emphasise strength — Alex explicitly
# called this out as a key signal ("Strong understanding of ..." being a
# LÜCKE is a strong fit-warning).
_STRENGTH_QUALIFIERS = (
    "strong ",
    "deep ",
    "expert",
    "proven",
    "extensive",
    "stark",
    "tief",
    "ausgeprägt",
    "umfassend",
    "fundiert",
    "mehrjährig",
    "established",
)


def _row_is_nice_to_have(comment: str) -> bool:
    low = comment.lower()
    return any(m in low for m in _NICE_TO_HAVE_MARKERS)


def _row_has_strength_qualifier(requirement: str) -> bool:
    low = requirement.lower()
    return any(q in low for q in _STRENGTH_QUALIFIERS)


def parse_anforderungsabgleich(analyse_text: str) -> list[tuple[str, str, str, str]]:
    """Parse the Anforderungsabgleich table from an analyst output.

    Returns a list of (requirement, beleg_ids, status, comment) tuples.
    Returns empty list if the section / table cannot be found.
    """
    # Locate the section
    lines = analyse_text.splitlines()
    section_start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("### 4.") and "anforderungsabgleich" in line.lower():
            section_start = i + 1
            break
        if "anforderungsabgleich" in line.strip().lower() and line.strip().startswith("###"):
            section_start = i + 1
            break
    if section_start is None:
        return []

    # Find the table start (first line beginning with `|`) and end (first
    # blank line or next `###`)
    table_lines: list[str] = []
    for j in range(section_start, len(lines)):
        s = lines[j].rstrip()
        if s.startswith("###"):
            break
        if not s.strip() and table_lines:
            break
        if s.startswith("|"):
            table_lines.append(s)

    # IN-03: locate the separator row (`|----|----|`) — everything before it
    # is header (skip), everything after is data.
    rows: list[tuple[str, str, str, str]] = []
    sep_pattern = re.compile(r"^\|[-:\s|]+\|$")
    sep_idx = next(
        (i for i, line in enumerate(table_lines) if sep_pattern.match(line)),
        -1,
    )
    data_lines = table_lines[sep_idx + 1:] if sep_idx >= 0 else table_lines
    for line in data_lines:
        if sep_pattern.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        req, beleg, status, comment = cells[0], cells[1], cells[2], "|".join(cells[3:])
        rows.append((req, beleg, status.upper(), comment))
    return rows


def check_profile_fit(analyse_path: Path) -> list[FitGap]:
    """Return critical/soft fit gaps found in the analyst output.

    A row is **critical** if status is LÜCKE and the comment does not flag
    nice-to-have. A row is **soft** if status is LÜCKE-nice-to-have or
    SCHWACH with a strength qualifier in the requirement text.
    """
    if not analyse_path.exists():
        return []
    rows = parse_anforderungsabgleich(analyse_path.read_text(encoding="utf-8"))
    gaps: list[FitGap] = []
    for req, _beleg, status, comment in rows:
        if status == "LÜCKE":
            severity = "soft" if _row_is_nice_to_have(comment) else "critical"
            gaps.append(FitGap(req, status, comment, severity))
        elif status == "SCHWACH" and _row_has_strength_qualifier(req):
            gaps.append(FitGap(req, status, comment, "soft"))
    return gaps


def format_gaps_for_cli(gaps: list[FitGap]) -> str:
    """Pretty-print gaps for terminal display."""
    if not gaps:
        return ""
    critical = [g for g in gaps if g.severity == "critical"]
    soft = [g for g in gaps if g.severity == "soft"]
    parts: list[str] = []
    parts.append("─" * 60)
    parts.append("Profil-Fit-Hinweise")
    parts.append("─" * 60)
    if critical:
        parts.append("")
        parts.append(f"  Kritisch ({len(critical)}): nicht belegte Muss-Anforderungen")
        for g in critical:
            parts.append(f"    • {g.requirement}")
            short_comment = g.comment.replace("\n", " ").strip()
            if len(short_comment) > 120:
                short_comment = short_comment[:117] + "..."
            if short_comment:
                parts.append(f"        ↳ {short_comment}")
    if soft:
        parts.append("")
        parts.append(f"  Beachten ({len(soft)}): schwach belegt oder Kann-Anforderung")
        for g in soft:
            parts.append(f"    • {g.requirement}  [{g.status}]")
    parts.append("")
    parts.append("─" * 60)
    return "\n".join(parts)


def has_critical_gaps(gaps: list[FitGap]) -> bool:
    return any(g.severity == "critical" for g in gaps)
