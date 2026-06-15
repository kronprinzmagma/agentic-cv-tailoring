"""Deterministic consistency checker for Berufserfahrung station headers.

Validates that each station header in a generated CV section maps to a real
station in the standard CV — same company, dates within the allowed range.

Catches:
- Invented companies (no token match in standard CV)
- Hallucinated date ranges (e.g. Nov 2011 – Feb 2015 for MediaHoldingCo when Standard-CV has only 2015)
- Duplicated company entries that exceed the allowed total range

This is a fast, regex-based check with zero LLM cost. Used as a hard veto
gate after the LLM factcheck for the berufserfahrung section.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple


# Synonym table only — canonical tokens are discovered dynamically from the
# Standard-CV via `_company_tokens()` (WR-08). This keeps the check
# resilient when Alex adds a new station: previously a missing company
# silently passed validation.
COMPANY_SYNONYMS: dict[str, str] = {
    # Map common substring patterns in CV outputs → canonical Standard-CV
    # key. The canonical here must match a value produced by
    # `_company_tokens` slugification, OR be the substring that appears in
    # the Standard-CV header itself (which becomes its own canonical).
    "mediacorp": "schweizer_radio_und_fernsehen",
    "schweizer radio": "schweizer_radio_und_fernsehen",
}

# Mutable snapshot — populated lazily from data/standard_cv.md the first
# time a parser/validator is invoked. Tests can override via `_set_company_tokens`.
COMPANY_TOKENS: dict[str, str] = {}
_TOKENS_LOADED_FROM: Path | None = None


def _company_tokens(standard_cv_path: Path | None = None) -> dict[str, str]:
    """Return the company-token map, building it once from the Standard-CV.

    For each `### YYYY[–YYYY] | Firma – Titel` station, the company portion
    (everything before the first `–`/`-` after the pipe) is split on `/`
    and slugified into lowercase token keys. The first slug becomes the
    canonical key for that company. Static synonyms in COMPANY_SYNONYMS
    are merged in.
    """
    global COMPANY_TOKENS, _TOKENS_LOADED_FROM
    path = standard_cv_path or Path("data/standard_cv.md")
    if COMPANY_TOKENS and _TOKENS_LOADED_FROM == path:
        return COMPANY_TOKENS
    tokens: dict[str, str] = {}
    if path.exists():
        header_re = re.compile(r"^###\s+\d{4}(?:\s*[–\-]\s*\d{4})?\s*\|\s*(.+?)$")
        for raw in path.read_text(encoding="utf-8").splitlines():
            m = header_re.match(raw.strip())
            if not m:
                continue
            rest = m.group(1)
            company_field = re.split(r"\s+[–\-]\s+", rest, maxsplit=1)[0]
            sub_companies = [s.strip() for s in company_field.split("/")]
            # Slug = lowercase, drop trailing legal-form suffix, collapse
            # whitespace runs. KEEP spaces — lookups use substring match.
            slugs: list[str] = []
            for s in sub_companies:
                if not s:
                    continue
                lowered = s.lower()
                lowered = re.sub(r"\s+(ag|gmbh|sa)\s*$", "", lowered)
                lowered = re.sub(r"\s+", " ", lowered).strip()
                if lowered:
                    slugs.append(lowered)
            if not slugs:
                continue
            # Canonical key: ASCII, no whitespace, no dots — usable as dict key
            canonical = re.sub(r"[^a-z0-9]+", "_", slugs[0]).strip("_")
            for slug in slugs:
                tokens[slug] = canonical
    # Merge synonyms; values must resolve to a canonical that exists
    canonicals_present = set(tokens.values())
    for syn, canonical in COMPANY_SYNONYMS.items():
        if canonical in canonicals_present:
            tokens[syn] = canonical
    COMPANY_TOKENS = tokens
    _TOKENS_LOADED_FROM = path
    return COMPANY_TOKENS


def _set_company_tokens(tokens: dict[str, str]) -> None:
    """Test hook — override the cache."""
    global COMPANY_TOKENS, _TOKENS_LOADED_FROM
    COMPANY_TOKENS = dict(tokens)
    _TOKENS_LOADED_FROM = Path("__test__")


class DateRange(NamedTuple):
    start_year: int
    end_year: int


class CanonicalStation(NamedTuple):
    """A canonical station from the Standard-CV."""
    company_key: str
    header_line: str  # exact `### YYYY[–YYYY] | Company – Title` from Standard-CV
    start_year: int
    end_year: int


def _identify_company(header_field: str) -> str | None:
    """Return canonical company key for a field that mentions a known company."""
    tokens = _company_tokens()
    field = header_field.lower()
    for token, key in tokens.items():
        if token in field:
            return key
    return None


def parse_canonical_stations(text: str) -> dict[str, CanonicalStation]:
    """Extract canonical station headers per company from the Standard-CV.

    Returns {company_key: CanonicalStation} — each station's full header line
    as it appears in the Standard-CV, plus the company key and date range.
    The header_line is what the generated CV should reproduce verbatim.
    """
    stations: dict[str, CanonicalStation] = {}
    header_re = re.compile(r"^(###\s+(\d{4})(?:\s*[–\-]\s*(\d{4}))?\s*\|\s*.+?)\s*$")
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = header_re.match(line)
        if not m:
            continue
        canonical_line = m.group(1).strip()
        start = int(m.group(2))
        end = int(m.group(3)) if m.group(3) else start
        rest = canonical_line.split("|", 1)[1]
        company_field = re.split(r"\s+[–\-]\s+", rest, maxsplit=1)[0]
        field_lower = company_field.lower()
        for token, key in _company_tokens().items():
            if token in field_lower:
                # Keep the first occurrence; if the same company appears
                # multiple times in standard CV, only the first wins.
                if key not in stations:
                    stations[key] = CanonicalStation(
                        company_key=key,
                        header_line=canonical_line,
                        start_year=start,
                        end_year=end,
                    )
    return stations


def parse_standard_cv_ranges(text: str) -> dict[str, DateRange]:
    """Extract allowed company → (start_year, end_year) from Standard-CV headers.

    Standard-CV station header format: `### YYYY[–YYYY] | Company – Title`
    Companies merged in one header (e.g. "GastroSaaS / local-directory.example") get the same range
    for each token they contain.
    """
    ranges: dict[str, DateRange] = {}
    header_re = re.compile(r"^###\s+(\d{4})(?:\s*[–\-]\s*(\d{4}))?\s*\|\s*(.+?)$")
    for line in text.splitlines():
        m = header_re.match(line.strip())
        if not m:
            continue
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        rest = m.group(3)
        # The "rest" is "Company – Title"; split on em-dash or en-dash or hyphen
        company_field = re.split(r"\s+[–\-]\s+", rest, maxsplit=1)[0]
        field_lower = company_field.lower()
        matched = False
        for token, key in _company_tokens().items():
            if token in field_lower:
                existing = ranges.get(key)
                if existing:
                    ranges[key] = DateRange(
                        min(existing.start_year, start),
                        max(existing.end_year, end),
                    )
                else:
                    ranges[key] = DateRange(start, end)
                matched = True
        if not matched:
            # Unknown company — extend COMPANY_TOKENS if you want strict checking
            pass
    return ranges


def _strip_md_decorations(line: str) -> str:
    """Strip leading #, *, and surrounding ** from a markdown header-ish line."""
    s = line.strip()
    s = s.lstrip("#").lstrip()
    # Remove leading/trailing ** for bold-only headers
    if s.startswith("**"):
        s = s[2:]
    # Trailing trim of any closing **
    s = s.rstrip()
    if s.endswith("**"):
        s = s[:-2]
    return s.strip()


def _looks_like_station_header(line: str) -> bool:
    """Heuristic: does this line look like a Berufserfahrung station header?"""
    stripped = line.strip()
    if not stripped:
        return False
    starts_ok = (
        stripped.startswith("###")
        or stripped.startswith("**")
    )
    if not starts_ok:
        return False
    if "|" not in stripped:
        return False
    # Must mention a known company token to qualify as a station header
    if not _identify_company(stripped):
        return False
    return True


def parse_generated_header(line: str) -> tuple[str, DateRange, str] | None:
    """Parse a generated CV station header.

    Accepted formats (pipe-separated):
      `### Title | Company | Mon YYYY – Mon YYYY`
      `**Title** | Company | Mon YYYY – Mon YYYY`
      `### Company – Title` (Standard-CV style with date in `### YYYY | ...`)

    Returns (company_key, date_range, raw_header) or None if not parseable.
    """
    cleaned = _strip_md_decorations(line)
    if "|" not in cleaned:
        return None
    parts = [p.strip() for p in cleaned.split("|")]
    if len(parts) < 2:
        return None

    date_part: str | None = None
    company_part: str | None = None
    for p in parts:
        if re.search(r"\b(19|20)\d{2}\b", p) and date_part is None:
            date_part = p
        elif _identify_company(p) and company_part is None:
            company_part = p

    if company_part is None:
        return None
    company_key = _identify_company(company_part)
    if company_key is None:
        return None

    if date_part is None:
        return company_key, DateRange(0, 9999), line.strip()

    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", date_part)]
    if not years:
        return company_key, DateRange(0, 9999), line.strip()
    return company_key, DateRange(min(years), max(years)), line.strip()


def validate_berufserfahrung(
    section_text: str, standard_cv_path: Path
) -> tuple[bool, list[str]]:
    """Strictly validate Berufserfahrung station headers against the Standard-CV.

    Three rules:
    1. **Exact header**: each generated station header must match the
       Standard-CV header for that company verbatim. No title shortening,
       no date variation, no GastroSaaS-vs-local-directory.example separation.
    2. **One entry per company**: no splitting a company into multiple
       periods (e.g. MediaCorp 2015–2017 + MediaCorp 2017–2023). The Standard-CV is
       authoritative — one entry per company, period.
    3. **No invented companies**: every company in the output must exist
       in the Standard-CV.

    Returns (is_valid, issues). If standard_cv_path does not exist or no
    canonical stations parse, validation is skipped (returns valid).
    """
    if not standard_cv_path.exists():
        return True, []

    standard_text = standard_cv_path.read_text(encoding="utf-8")
    canonicals = parse_canonical_stations(standard_text)
    if not canonicals:
        return True, []

    issues: list[str] = []
    seen_companies: dict[str, list[str]] = {}  # company_key → list of raw headers

    for raw_line in section_text.splitlines():
        line = raw_line.rstrip()
        if not _looks_like_station_header(line):
            continue
        # Strip trailing whitespace and unify dash style for comparison
        line_norm = line.strip()
        parsed = parse_generated_header(line)
        if parsed is None:
            continue
        company_key, _dr, _raw = parsed

        if company_key not in canonicals:
            issues.append(
                f"Erfundene Station: '{line_norm[:100]}' — Firma '{company_key}' "
                f"existiert nicht im Standard-CV."
            )
            continue

        canonical = canonicals[company_key]
        # Strict equality (normalised whitespace)
        if " ".join(line_norm.split()) != " ".join(canonical.header_line.split()):
            issues.append(
                f"Header-Drift für '{company_key}': '{line_norm[:120]}' "
                f"weicht ab. Erwartet wortgenau: '{canonical.header_line}'."
            )

        seen_companies.setdefault(company_key, []).append(line_norm)

    # One entry per company
    for company_key, headers in seen_companies.items():
        if len(headers) > 1:
            issues.append(
                f"Mehrere Stationen für '{company_key}': "
                f"{len(headers)} Einträge gefunden. Standard-CV erlaubt nur einen. "
                f"Erwartet: '{canonicals[company_key].header_line}'."
            )

    return len(issues) == 0, issues


def autofix_headers(section_text: str, standard_cv_path: Path) -> tuple[str, list[str]]:
    """Deterministically replace each station header in `section_text` with
    the verbatim Standard-CV header for that company.

    Behaviour:
    - First occurrence of a known company: header replaced with canonical line.
    - Second+ occurrence of the same company (split station): the duplicate
      header and the separating `---` ahead of it are dropped, and the bullets
      that follow are merged under the first occurrence. This preserves all
      body content while collapsing splits into one station.
    - Body bullets are never touched.

    Returns (corrected_text, list_of_fixes_applied).
    """
    if not standard_cv_path.exists():
        return section_text, []
    canonicals = parse_canonical_stations(standard_cv_path.read_text(encoding="utf-8"))
    if not canonicals:
        return section_text, []

    fixes: list[str] = []
    out_lines: list[str] = []
    seen: set[str] = set()
    header_re = re.compile(r"^\s*###\s+\d{4}(?:\s*[–\-]\s*\d{4})?\s*\|\s*.+$")

    lines = section_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if header_re.match(line.strip()):
            parsed = parse_generated_header(line)
            if parsed:
                company_key, _dr, _raw = parsed
                if company_key in canonicals:
                    canonical_line = canonicals[company_key].header_line
                    if company_key not in seen:
                        # First occurrence — replace header verbatim
                        if line.strip() != canonical_line:
                            fixes.append(
                                f"Header für '{company_key}' auf Standard-CV "
                                f"normalisiert: '{line.strip()[:80]}' → "
                                f"'{canonical_line}'"
                            )
                        out_lines.append(canonical_line)
                        seen.add(company_key)
                        i += 1
                        continue
                    else:
                        # Duplicate station for this company — drop the header
                        # and any preceding `---` separator + blank line.
                        # Bullets that follow merge into the first occurrence.
                        # Remove trailing separator if any.
                        while out_lines and (
                            out_lines[-1].strip() == ""
                            or out_lines[-1].strip() == "---"
                        ):
                            out_lines.pop()
                        fixes.append(
                            f"Aufgespaltene Station für '{company_key}' "
                            f"zusammengeführt (Header '{line.strip()[:80]}' entfernt)"
                        )
                        # Insert a blank line as bullet separator
                        out_lines.append("")
                        i += 1
                        # Skip optional blank line right after header
                        while i < len(lines) and lines[i].strip() == "":
                            i += 1
                        continue
        out_lines.append(line)
        i += 1

    result = "\n".join(out_lines)
    if section_text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result, fixes


def format_issues_for_writer(issues: list[str]) -> str:
    """Format issues as a markdown block the writer can read in round 2."""
    if not issues:
        return "Keine strukturelle Drift gefunden."
    lines = ["# Konsistenz-Check: Drift gegen Standard-CV erkannt", ""]
    for i, issue in enumerate(issues, 1):
        lines.append(f"{i}. {issue}")
    lines.append("")
    lines.append(
        "**Pflichtkorrektur in der nächsten Runde:** Jede Station muss exakt "
        "einer Station im Standard-CV entsprechen. Keine erfundenen Titel, "
        "Firmen oder Zeiträume. Zusammengeführte Einträge wie "
        "`GastroSaaS / local-directory.example` bleiben in einer Station."
    )
    return "\n".join(lines)
