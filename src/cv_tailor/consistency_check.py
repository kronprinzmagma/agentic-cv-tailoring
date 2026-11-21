"""Deterministic consistency checker for Berufserfahrung station headers.

Validates that each station header in a generated CV section maps to a real
station in the standard CV — same company, dates within the allowed range.

Catches:
- Invented companies (no token match in standard CV)
- Hallucinated date ranges (e.g. Nov 2011 – Feb 2015 for MediaHoldingCo when Standard-CV has only 2015)
- Duplicated company entries that exceed the allowed total range
- Stations out of Standard-CV order or left as a bare header (opt-in, whole section)
- A station that drops the verifiable link its first Standard-CV bullet carries (opt-in, whole section)

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
    # English renderings of the two non-company stations (2026-09-16). The
    # Translator carries the fixed headers from prompts/translator.md; the
    # extra synonyms tolerate drift so `validate_translation` does not reject
    # a whole EN CV over a paraphrased station label (observed 2026-09-19).
    "independent": "eigenregie",
    "self-employed": "eigenregie",
    "self-directed": "eigenregie",
    "freelance": "eigenregie",
    "own initiative": "eigenregie",
    "career break": "famili_r_bedingte_auszeit",
    "family-related": "famili_r_bedingte_auszeit",
    "family related": "famili_r_bedingte_auszeit",
    "family leave": "famili_r_bedingte_auszeit",
    "parental leave": "famili_r_bedingte_auszeit",
    "sabbatical": "famili_r_bedingte_auszeit",
}

# Mutable snapshot — populated lazily from data/standard_cv.md the first
# time a parser/validator is invoked. Tests can override via `_set_company_tokens`.
COMPANY_TOKENS: dict[str, str] = {}
_TOKENS_LOADED_FROM: Path | None = None


# A station's end can be a year or an open end ("2025–heute"). Without this
# the header parses as "no station" — the company never enters COMPANY_TOKENS
# and the generated header is then flagged as an invented company (the guard
# from 2026-08-14 firing on a legitimate current role).
_OPEN_END_WORDS = ("heute", "today", "present", "jetzt", "now")
_END_YEAR_RE = r"(?:\d{4}|" + "|".join(_OPEN_END_WORDS) + r")"
# Open-ended stations get a far-future end year so range checks stay simple.
OPEN_END_YEAR = 9999


def _end_year(token: str | None, start: int) -> int:
    """Resolve a captured end-of-range token to a year."""
    if not token:
        return start
    if token.isdigit():
        return int(token)
    return OPEN_END_YEAR


def _company_tokens(standard_cv_path: Path | None = None) -> dict[str, str]:
    """Return the company-token map, building it once from the Standard-CV.

    For each `### YYYY[–YYYY] | Firma – Titel` station, the company portion
    (everything before the first `–`/`-` after the pipe) is split on `/`
    and slugified into lowercase token keys. The first slug becomes the
    canonical key for that company. Static synonyms in COMPANY_SYNONYMS
    are merged in.
    """
    global COMPANY_TOKENS, _TOKENS_LOADED_FROM
    # No explicit path → reuse whatever was loaded last. The validators prime
    # the cache with their `standard_cv_path`; the helpers below call this
    # without arguments and must see the same tokens — otherwise a test with
    # a temporary Standard-CV silently validates against the real one.
    path = standard_cv_path or _TOKENS_LOADED_FROM or Path("data/standard_cv.md")
    if COMPANY_TOKENS and _TOKENS_LOADED_FROM == path:
        return COMPANY_TOKENS
    tokens: dict[str, str] = {}
    if path.exists():
        header_re = re.compile(
            r"^###\s+\d{4}(?:\s*[–\-]\s*" + _END_YEAR_RE + r")?\s*\|\s*(.+?)$",
            re.IGNORECASE,
        )
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
    # Non-empty body lines (bullets/paragraphs) verbatim from the Standard-CV,
    # up to the next heading. Standard-CV text is evidence by definition, so
    # the layout autofix may quote the first line into an empty station.
    body_lines: tuple[str, ...] = ()


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
    header_re = re.compile(
        r"^(###\s+(\d{4})(?:\s*[–\-]\s*(" + _END_YEAR_RE + r"))?\s*\|\s*.+?)\s*$",
        re.IGNORECASE,
    )
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = header_re.match(line)
        if not m:
            if line.strip().startswith("#"):
                current_key = None  # any other heading ends the station body
            elif current_key is not None and line.strip() and line.strip() != "---":
                st = stations[current_key]
                stations[current_key] = st._replace(
                    body_lines=st.body_lines + (line.strip(),)
                )
            continue
        current_key = None
        canonical_line = m.group(1).strip()
        start = int(m.group(2))
        end = _end_year(m.group(3), start)
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
                    current_key = key
    return stations


def parse_standard_cv_ranges(text: str) -> dict[str, DateRange]:
    """Extract allowed company → (start_year, end_year) from Standard-CV headers.

    Standard-CV station header format: `### YYYY[–YYYY] | Company – Title`
    Companies merged in one header (e.g. "GastroSaaS / local-directory.example") get the same range
    for each token they contain.
    """
    ranges: dict[str, DateRange] = {}
    header_re = re.compile(
        r"^###\s+(\d{4})(?:\s*[–\-]\s*(" + _END_YEAR_RE + r"))?\s*\|\s*(.+?)$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        m = header_re.match(line.strip())
        if not m:
            continue
        start = int(m.group(1))
        end = _end_year(m.group(2), start)
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


def _structurally_station_like(line: str) -> bool:
    """Does this line have the *shape* of a station header, company aside?

    Deliberately narrower than `_looks_like_station_header`: only `###`-level
    headings with a pipe and a 4-digit year qualify. Body bullets and bold
    inline text never start with `###`, so precision stays high — a false
    positive here becomes a hard veto. This closes the gap where a fully
    invented company carried no known token and was silently skipped.
    """
    stripped = line.strip()
    if not stripped.startswith("###"):
        return False
    if "|" not in stripped:
        return False
    return bool(re.search(r"\b(19|20)\d{2}\b", stripped))


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
    section_text: str,
    standard_cv_path: Path,
    check_completeness: bool = False,
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
    4. **Completeness** (opt-in via `check_completeness`): every station in
       the Standard-CV must appear in the output. Rules 1–3 inspect what is
       there; a dropped station is invisible to all of them and leaves an
       unexplained gap in the CV. Off by default because callers that check a
       *fragment* (precision tests, partial drafts) would otherwise always
       fail; the writer loop passes the whole section and opts in.

    Returns (is_valid, issues). If standard_cv_path does not exist or no
    canonical stations parse, validation is skipped (returns valid).
    """
    if not standard_cv_path.exists():
        return True, []

    _company_tokens(standard_cv_path)  # prime the token cache for this CV
    standard_text = standard_cv_path.read_text(encoding="utf-8")
    canonicals = parse_canonical_stations(standard_text)
    if not canonicals:
        return True, []

    issues: list[str] = []
    seen_companies: dict[str, list[str]] = {}  # company_key → list of raw headers

    for raw_line in section_text.splitlines():
        line = raw_line.rstrip()
        if not _looks_like_station_header(line):
            # Station-shaped heading without any known company token →
            # fully invented employer (e.g. "### 2020–2022 | FictionalCorp – …").
            if _structurally_station_like(line):
                issues.append(
                    f"Erfundene Station: '{line.strip()[:100]}' — Firma ist im "
                    f"Standard-CV nicht vorhanden."
                )
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

    # Rule 4: completeness. Every rule above inspects what the draft *contains*;
    # none of them notices a station that is simply absent. The writer dropped
    # the current "2025–heute" station on both rounds and every check stayed
    # green (2026-09-10). Omission is the harder error to spot by eye, because
    # what is left reads perfectly well.
    for company_key, canonical in canonicals.items():
        if check_completeness and company_key not in seen_companies:
            issues.append(
                f"Fehlende Station: '{canonical.header_line}' steht im Standard-CV, "
                f"fehlt aber im Entwurf. Jede Station des Standard-CV muss "
                f"erscheinen — Weglassen erzeugt eine unerklärte Lücke im Lebenslauf."
            )

    # Rules 5 + 6: order and empty bodies. Both surfaced in the iWay run
    # (2026-09-18): round 1 dropped the two newest stations, rule 4 caught
    # it, and round 2 *appended* them after the oldest station — one of them
    # as a bare header. Rules 1–4 were all green on that draft.
    if check_completeness:
        blocks = _split_station_blocks(section_text)
        present = [key for key, _h, _b in blocks if key in canonicals]
        expected = [key for key in canonicals if key in present]
        if present != expected:
            issues.append(
                "Reihenfolge der Stationen weicht vom Standard-CV ab: "
                + " → ".join(present)
                + ". Erwartet (jüngste zuerst): "
                + " → ".join(expected)
                + ". Neue oder nachgetragene Stationen gehören an ihre "
                "chronologische Position, nicht ans Ende."
            )
        for key, header, body in blocks:
            canonical = canonicals.get(key)
            anchor = _anchor_url(canonical)
            if anchor and not _has_anchor(body, anchor):
                issues.append(
                    f"Station ohne Anker: '{header[:100]}' — der erste Standard-CV-Bullet "
                    f"dieser Station trägt den prüfbaren Link '{anchor}', der Entwurf nicht. "
                    f"Diesen Bullet wortgleich (mit Anzahl und Link) übernehmen; "
                    f"Umschreibungen wie 'eigene KI-gestützte Tools' ersetzen ihn nicht."
                )
            elif anchor and not _has_anchor_line(body, canonical):
                issues.append(
                    f"Anker-Bullet verändert: '{header[:100]}' — der Link steht, aber der "
                    f"erste Standard-CV-Bullet nicht wortgleich: '{canonical.body_lines[0][:120]}'. "
                    f"Zahlen und Aussagen dieses Bullets nicht umschreiben."
                )
            if canonical is None or len(canonical.body_lines) < 2:
                continue  # stations the Standard-CV itself keeps bare stay bare
            if not any(l.strip() and l.strip() != "---" for l in body):
                issues.append(
                    f"Station ohne Inhalt: '{header[:100]}' — der Header steht, "
                    f"aber kein Bullet folgt. Mindestens ein Satz aus den "
                    f"Standard-CV-Bullets dieser Station gehört darunter."
                )

    return len(issues) == 0, issues


# Rule 7 (anchor link). The first Standard-CV bullet of a station may carry a
# checkable link (today: the self-directed AI-product station links to the
# public code repositories). A thin paraphrase such as "eigene KI-gestützte
# Tools" drops the one part a recruiter can verify. Measured 2026-10-01 on the
# last 17 CVs that carry that station: 9 shipped without the link.
_ANCHOR_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}/[^\s)\]>*]+",
    re.IGNORECASE,
)


def _norm_url(url: str) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", url.strip().lower()).rstrip("/.,;:")


def _anchor_url(canonical: CanonicalStation | None) -> str | None:
    """Normalised link in the station's first Standard-CV body line, if any."""
    if canonical is None or not canonical.body_lines:
        return None
    m = _ANCHOR_URL_RE.search(canonical.body_lines[0])
    return _norm_url(m.group(0)) if m else None


def _has_anchor(body_lines: list[str], anchor: str) -> bool:
    return anchor in "\n".join(body_lines).lower().replace("**", "")


def _norm_line(line: str) -> str:
    """Comparable form of a bullet: no list marker, bold, URL scheme or end punctuation."""
    s = line.strip().replace("**", "")
    s = re.sub(r"^[-*]\s+", "", s)
    s = re.sub(r"(?:https?://)?(?:www\.)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"https?://", "", s, flags=re.IGNORECASE)
    s = " ".join(s.split()).lower()
    return s.rstrip(" .;:,")


def _has_anchor_line(body_lines: list[str], canonical: CanonicalStation) -> bool:
    """True when the station's first Standard-CV bullet appears verbatim (normalised).

    The link alone is not enough: the Swiss Olympic run (2026-10-04) kept the
    link but rewrote the bullet to «3 Projekte, davon 2 öffentlich» — invented
    numbers next to a correct URL.
    """
    target = _norm_line(canonical.body_lines[0])
    return any(_norm_line(line) == target for line in body_lines if line.strip())


def _split_station_blocks(section_text: str) -> list[tuple[str | None, str, list[str]]]:
    """Split a Berufserfahrung section into (company_key, header, body_lines).

    Only `###` headings with a pipe count as station headers (same shape rule
    as `_structurally_station_like`). Lines before the first header are not
    returned. `company_key` is None for headers no known company matches.
    """
    blocks: list[tuple[str | None, str, list[str]]] = []
    for raw in section_text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if _structurally_station_like(stripped):
            parsed = parse_generated_header(stripped)
            key = parsed[0] if parsed else None
            blocks.append((key, stripped, []))
        elif blocks:
            blocks[-1][2].append(line)
    return blocks


def autofix_station_layout(
    section_text: str, standard_cv_path: Path, *, fill_missing: bool = False
) -> tuple[str, list[str]]:
    """Deterministically restore Standard-CV station order and fill bare stations.

    Companion to `autofix_headers` for the MAX_ROUNDS fallback:
    - With `fill_missing` (whole section, not a fragment), a Standard-CV
      station missing from the draft is added with its header and first body
      line verbatim. Benchmark 2026-10-23 (Kanton ZH): the writer dropped the
      career-break station in round 2 and the run shipped without it.
    - Station blocks are re-sorted into Standard-CV order; blocks whose
      company is unknown keep their relative order and go last.
    - A station whose Standard-CV entry has ≥2 body lines but whose draft
      body is empty gets the *first* Standard-CV body line verbatim — quoted
      evidence, never generated text.
    - A station whose first Standard-CV bullet carries a link (rule 7) but
      whose draft lacks it: a single-line body is replaced by that bullet,
      a longer body gets it prepended.
    Bodies are otherwise untouched; the preamble before the first header is
    preserved.
    """
    if not standard_cv_path.exists():
        return section_text, []
    _company_tokens(standard_cv_path)
    canonicals = parse_canonical_stations(standard_cv_path.read_text(encoding="utf-8"))
    if not canonicals:
        return section_text, []

    lines = section_text.splitlines()
    first_header = next(
        (i for i, l in enumerate(lines) if _structurally_station_like(l.strip())),
        None,
    )
    if first_header is None:
        return section_text, []
    preamble = lines[:first_header]
    blocks = _split_station_blocks("\n".join(lines[first_header:]))
    fixes: list[str] = []

    if fill_missing:
        have = {key for key, _h, _b in blocks}
        for key, canonical in canonicals.items():
            if key not in have:
                blocks.append((key, canonical.header_line, list(canonical.body_lines[:1])))
                fixes.append(
                    f"Fehlende Station '{canonical.header_line[:60]}' "
                    f"mit Standard-CV-Wortlaut ergänzt"
                )

    order = {key: i for i, key in enumerate(canonicals)}
    present = [key for key, _h, _b in blocks]
    sorted_blocks = sorted(
        enumerate(blocks),
        key=lambda ib: (order.get(ib[1][0], len(order)), ib[0]),
    )
    if [b[0] for _i, b in sorted_blocks] != present:
        fixes.append(
            "Stationen in Standard-CV-Reihenfolge gebracht: "
            + " → ".join(str(k) for k in present)
            + " ⇒ "
            + " → ".join(str(b[0]) for _i, b in sorted_blocks)
        )

    out: list[str] = list(preamble)
    for idx, (key, header, body) in enumerate(b for _i, b in sorted_blocks):
        # Normalise the body: strip trailing separators, re-add one between blocks.
        content = list(body)
        while content and (content[-1].strip() == "" or content[-1].strip() == "---"):
            content.pop()
        while content and content[0].strip() == "":
            content.pop(0)
        canonical = canonicals.get(key) if key else None
        if canonical and len(canonical.body_lines) >= 2 and not content:
            content = [canonical.body_lines[0]]
            fixes.append(
                f"Leere Station '{header[:60]}' mit erstem Standard-CV-Bullet gefüllt"
            )
        anchor = _anchor_url(canonical)
        if anchor and _has_anchor(content, anchor) and not _has_anchor_line(content, canonical):
            # Link present, bullet rewritten: swap the line carrying the link
            # for the verbatim Standard-CV bullet (keeps the draft's list style).
            first = canonical.body_lines[0]
            for i, line in enumerate(content):
                if anchor in line.lower().replace("**", ""):
                    dashed = line.lstrip().startswith("- ")
                    content[i] = first if dashed else re.sub(r"^[-*]\s+", "", first)
                    fixes.append(
                        f"Station '{header[:60]}': umgeschriebenen Anker-Bullet durch "
                        f"Standard-CV-Wortlaut ersetzt"
                    )
                    break
        elif anchor and not _has_anchor(content, anchor):
            first = canonical.body_lines[0]
            dashed = bool(content) and content[0].lstrip().startswith("- ")
            line = first if (dashed or not content) else re.sub(r"^[-*]\s+", "", first)
            if len([l for l in content if l.strip()]) <= 1:
                # A single thin line is the paraphrase itself — replace it.
                content = [line]
                fixes.append(
                    f"Station '{header[:60]}': dünne Fassung durch ersten Standard-CV-Bullet "
                    f"(mit Link) ersetzt"
                )
            else:
                content = [line, ""] + content
                fixes.append(
                    f"Station '{header[:60]}': ersten Standard-CV-Bullet (mit Link) vorangestellt"
                )
        out.append(header)
        out.append("")
        out.extend(content)
        if content:
            out.append("")
        if idx < len(sorted_blocks) - 1:
            out.append("---")
            out.append("")

    result = "\n".join(out)
    if section_text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result, fixes


def autofix_headers(section_text: str, standard_cv_path: Path) -> tuple[str, list[str]]:
    """Deterministically replace each station header in `section_text` with
    the verbatim Standard-CV header for that company.

    Behaviour:
    - First occurrence of a known company: header replaced with canonical line.
    - Second+ occurrence of the same company (split station): the duplicate
      header is dropped and its whole block body moves to the first
      occurrence, also when another station stands in between.
    - Body bullets are never touched.

    Returns (corrected_text, list_of_fixes_applied).
    """
    if not standard_cv_path.exists():
        return section_text, []
    _company_tokens(standard_cv_path)  # prime the token cache for this CV
    canonicals = parse_canonical_stations(standard_cv_path.read_text(encoding="utf-8"))
    if not canonicals:
        return section_text, []

    fixes: list[str] = []
    header_re = re.compile(
        r"^\s*###\s+\d{4}(?:\s*[–\-]\s*" + _END_YEAR_RE + r")?\s*\|\s*.+$",
        re.IGNORECASE,
    )

    # Parse whole blocks first, then merge: removing only the duplicate header
    # left its bullets under whatever station preceded it — with another
    # employer in between, claims moved to the wrong company
    # (review 2026-10-23, R15).
    preamble: list[str] = []
    blocks: list[list] = []  # [header_line, company_key_or_None, body_lines]
    for line in section_text.splitlines():
        if header_re.match(line.strip()):
            parsed = parse_generated_header(line)
            key = parsed[0] if parsed and parsed[0] in canonicals else None
            blocks.append([line, key, []])
        elif blocks:
            blocks[-1][2].append(line)
        else:
            preamble.append(line)

    def _trim(body: list[str]) -> list[str]:
        body = list(body)
        while body and body[-1].strip() in ("", "---"):
            body.pop()
        while body and body[0].strip() == "":
            body.pop(0)
        return body

    kept: list[list] = []
    first_by_key: dict[str, list] = {}
    for header, key, body in blocks:
        if key is None:
            kept.append([header, _trim(body)])
            continue
        canonical_line = canonicals[key].header_line
        if key not in first_by_key:
            if header.strip() != canonical_line:
                fixes.append(
                    f"Header für '{key}' auf Standard-CV normalisiert: "
                    f"'{header.strip()[:80]}' → '{canonical_line}'"
                )
            entry = [canonical_line, _trim(body)]
            first_by_key[key] = entry
            kept.append(entry)
        else:
            target = first_by_key[key]
            extra = _trim(body)
            if extra:
                target[1] = target[1] + [""] + extra
            fixes.append(
                f"Aufgespaltene Station für '{key}' zusammengeführt "
                f"(Header '{header.strip()[:80]}' entfernt, Inhalt zur ersten Station verschoben)"
            )

    if not fixes:
        return section_text, []
    has_rule = any(line.strip() == "---" for line in section_text.splitlines())
    separator = ["", "---", ""] if has_rule else [""]
    out_lines: list[str] = list(preamble)
    for idx, (header, body) in enumerate(kept):
        out_lines.append(header)
        if body:
            out_lines.append("")
            out_lines.extend(body)
        if idx < len(kept) - 1:
            out_lines.extend(separator)

    result = "\n".join(out_lines)
    if section_text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result, fixes


def format_station_skeleton(standard_cv_path: Path, ki: bool = False) -> str:
    """Writer scaffold for Berufserfahrung: every station verbatim, in order.

    Review 2026-10-23: in 26 of 28 runs round 1 of Berufserfahrung failed the
    consistency check with the same five findings («HealthApp AG», «(MediaCorp)»,
    old titles, Eigenregie and Auszeit missing). The writer prompt demands
    headers «wortgenau aus dem Standard-CV», but the writer never sees the
    Standard-CV — only Beleg-Index and exemplars, which carry older headers.
    Round 2 was spent on headers instead of substance. This scaffold hands
    over exactly what rules 1–7 enforce: header, order, the anchor bullet and
    the one-line body of stations the Standard-CV keeps that short.
    Empty string when the Standard-CV is missing.
    """
    if not standard_cv_path.exists():
        return ""
    _company_tokens(standard_cv_path)  # prime the token cache for this CV
    canonicals = parse_canonical_stations(standard_cv_path.read_text(encoding="utf-8"))
    if not canonicals:
        return ""
    lines = [
        "## Stations-Gerüst (Pflicht, aus dem Standard-CV)",
        "",
        "Alle Stationen in genau dieser Reihenfolge, Header zeichengenau übernehmen "
        "(kein «AG», keine Klammer-Kürzel, keine Titelvariante). Mit «wortgleich» "
        "markierte Zeilen stehen unverändert in der Station. Alles Übrige wählst du "
        "nach Anzeige und Belegen.",
        "",
    ]
    # Budgets and Pflichtsubstanz per station (data/fact_limits.yaml; empty
    # without the file, e.g. in the public clone).
    from cv_tailor.fact_limits import load_station_rules, skeleton_notes

    rules = load_station_rules()
    if rules.max_total_words:
        lines.insert(
            len(lines) - 1,
            f"Gesamtbudget Berufserfahrung: höchstens {rules.max_total_words} Wörter "
            "(sonst mehr als drei PDF-Seiten).",
        )
    if ki:
        lines.insert(
            len(lines) - 1,
            "KI-nahe Anzeige: Die Eigenregie-Station trägt hier das zweite Hauptsignal "
            "und bekommt das höhere Budget. Stationen ohne Bezug zur Stelle "
            "(MediaHoldingCo, GastroSaaS, Namics) knapper halten, damit das Gesamtbudget reicht.",
        )
    for station in canonicals.values():
        lines.append(station.header_line)
        first = re.sub(r"^[-*]\s+", "", station.body_lines[0]) if station.body_lines else ""
        if _anchor_url(station):
            lines.append(f"- wortgleich (Anker-Bullet): {first}")
        elif len(station.body_lines) == 1:
            lines.append(f"- wortgleich (einzige Zeile): {first}")
        lines.extend(skeleton_notes(rules, station.header_line, ki=ki))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
