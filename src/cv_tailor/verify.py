"""One ordered verification of a run, plus freshness stamps for the manual steps.

Why this exists
---------------
By 2026-10-30 a CV passed through more than a dozen checks: fact limits,
consistency, station rules, length, style, redundancy, ATS, page count, and
four steps nobody can automate (naturalisation, the ki-stil-check skill, the
source comparison, the external ChatGPT review). Two problems showed up:

1. **Order and precedence were folklore.** Checks that add text (ATS terms,
   review feedback) fight checks that limit it (length, style); running them
   in the wrong order meant trimming twice. `verify` runs the deterministic
   ones in one fixed order, strongest first.
2. **Nothing knew a check was stale.** The style skill is applied by hand and
   was forgotten three times: no gate saw that the text had changed after it
   ran. A stamp stores a hash of the final text when a manual step is done;
   `verify` compares it with the text as it is now.

Precedence (a lower line never overrides a higher one):
facts (fact limits, consistency, anchor) > length and pages > style >
ATS terms > redundancy.

`verify` is report-only and exits 1 only on FAIL. Runs without a stamp file are
unaffected everywhere else (the renderer only warns when a stamp file exists).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STAMP_FILE = "_stamps.json"
# Open factcheck points after the last writer round lock the PDF. The stamp for
# them means: Alex accepted the points, or all of them are corrected.
FACTCHECK_OPEN_FILE = "_factcheck_offen.md"
FACTCHECK_OPEN_STEP = "faktencheck-offen"
STEPS = ("naturalisation", "ki-stil-check", "quellenabgleich", "chatgpt", FACTCHECK_OPEN_STEP)
# Gates that must describe the text that goes out. Naturalisation naturally comes
# earlier (the style check changes text after it) and the ChatGPT review is the
# user's own step: both are shown, neither blocks.
REQUIRED_FRESH = ("ki-stil-check", "quellenabgleich")
MAX_PAGES = 3


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # OK | INFO | WARN | FAIL
    detail: str = ""


# --------------------------------------------------------------------------- #
# Stamps
# --------------------------------------------------------------------------- #


def _final_paths(run_dir: Path) -> list[Path]:
    from cv_tailor.rework import resolve_final

    return [p for p in (resolve_final(run_dir, lang) for lang in ("de", "en")) if p is not None]


def text_hash(run_dir: Path) -> str:
    """Hash of the CV text as it would go out (freshest DE and EN final)."""
    digest = hashlib.sha256()
    for path in _final_paths(run_dir):
        digest.update(path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def read_stamps(run_dir: Path) -> dict:
    path = run_dir / STAMP_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _open_points_hash(run_dir: Path) -> str:
    return hashlib.sha256((run_dir / FACTCHECK_OPEN_FILE).read_bytes()).hexdigest()


def _save_stamps(run_dir: Path, stamps: dict) -> None:
    tmp = run_dir / (STAMP_FILE + ".tmp")
    tmp.write_text(json.dumps(stamps, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(run_dir / STAMP_FILE)


def write_stamp(run_dir: Path, step: str, note: str = "") -> dict:
    if step not in STEPS:
        raise ValueError(f"Unbekannter Schritt: {step} (erlaubt: {', '.join(STEPS)})")
    entry = {
        "hash": text_hash(run_dir),
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": note,
    }
    if step == FACTCHECK_OPEN_STEP:
        if not (run_dir / FACTCHECK_OPEN_FILE).is_file():
            raise ValueError(f"Keine {FACTCHECK_OPEN_FILE} im Lauf: nichts zu stempeln.")
        if not note.strip():
            raise ValueError(f"{FACTCHECK_OPEN_STEP} braucht --note (akzeptiert oder korrigiert).")
        entry["open_hash"] = _open_points_hash(run_dir)
    stamps = read_stamps(run_dir)
    stamps[step] = entry
    _save_stamps(run_dir, stamps)
    return entry


def drop_stamp(run_dir: Path, step: str) -> None:
    stamps = read_stamps(run_dir)
    if stamps.pop(step, None) is not None:
        _save_stamps(run_dir, stamps)


def factcheck_open_block(run_dir: Path) -> str | None:
    """Why no PDF may be rendered, or None. The file's existence counts as open."""
    if not (run_dir / FACTCHECK_OPEN_FILE).is_file():
        return None
    state = stamp_state(run_dir, FACTCHECK_OPEN_STEP)
    if state == "fresh":
        entry = read_stamps(run_dir)[FACTCHECK_OPEN_STEP]
        if entry.get("open_hash") == _open_points_hash(run_dir):
            return None
    changed = " Der Stempel gilt nicht mehr: Text oder Punkte geändert."
    reason = "" if state == "missing" else changed
    return (
        f"Offene Faktencheck-Punkte ({FACTCHECK_OPEN_FILE}).{reason} Punkte korrigieren oder "
        f"von Alex akzeptieren lassen, dann `uv run cv-tailor stamp {FACTCHECK_OPEN_STEP} "
        f"{run_dir.name} --note \"…\"`."
    )


def stamp_state(run_dir: Path, step: str) -> str:
    """`fresh` (text unchanged since the step), `stale` or `missing`."""
    entry = read_stamps(run_dir).get(step)
    if not entry:
        return "missing"
    return "fresh" if entry.get("hash") == text_hash(run_dir) else "stale"


def stamp_warnings(run_dir: Path) -> list[str]:
    """Warnings for the renderer; empty when the run has no stamp file at all."""
    if not (run_dir / STAMP_FILE).is_file():
        return []
    warnings = []
    for step in REQUIRED_FRESH:
        state = stamp_state(run_dir, step)
        if state != "fresh":
            warnings.append(
                f"{step}: "
                + ("Text seit dem Schritt geändert" if state == "stale" else "nicht gestempelt")
            )
    return warnings


def _stamp_results(run_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    stamps = read_stamps(run_dir)
    for step in STEPS:
        if step == FACTCHECK_OPEN_STEP:
            continue  # own check: only relevant while open points exist
        state = stamp_state(run_dir, step)
        when = stamps.get(step, {}).get("time", "")[:16].replace("T", " ")
        if step in REQUIRED_FRESH:
            if state == "fresh":
                results.append(CheckResult(f"Stempel {step}", "OK", f"aktuell ({when})"))
            elif state == "stale":
                results.append(
                    CheckResult(
                        f"Stempel {step}",
                        "FAIL",
                        f"Text seit {when} geändert: Schritt wiederholen, "
                        f"dann `cv-tailor stamp {step} <run-id>`",
                    )
                )
            else:
                results.append(CheckResult(f"Stempel {step}", "FAIL", "Schritt nicht gestempelt"))
        elif state == "missing":
            results.append(CheckResult(f"Stempel {step}", "INFO", "nicht gesetzt"))
        else:
            tail = (
                "" if state == "fresh" else ", Text seither geändert (normal, Schritt liegt früher)"
            )
            results.append(CheckResult(f"Stempel {step}", "INFO", f"gesetzt {when}{tail}"))
    return results


# --------------------------------------------------------------------------- #
# The ordered check bundle
# --------------------------------------------------------------------------- #


def _section(text: str, name: str) -> str:
    match = re.search(rf"## {re.escape(name)}\n(.*?)(?=\n## |\Z)", text, re.S)
    return match.group(1) if match else ""


MIN_EXPERIENCE_BOLDS = 4  # finished CVs carry 9 to 19; hand edits that lost them 0 to 2


def missing_bold_terms(text: str) -> list[str]:
    """Sections whose scan terms are gone (Concordia, Straumann 2026-11-03: none left)."""
    def bolds(pattern: str) -> int | None:
        match = re.search(rf"(?ms)^##\s+(?:{pattern})\b(.*?)(?=^##\s|\Z)", text)
        return len(re.findall(r"\*\*[^*\n]+\*\*", match.group(1))) if match else None

    summary = bolds("Management Summary|Summary")
    experience = bolds("Berufserfahrung|Professional Experience|Work Experience")
    missing = []
    if summary == 0:
        missing.append("Summary ohne fette Begriffe")
    if experience is not None and experience < MIN_EXPERIENCE_BOLDS:
        missing.append(f"Berufserfahrung nur {experience} fette Begriffe")
    return missing


# Team sizes in an evidence claim: "11-köpfigen Projektteams", "über 10 Personen",
# "Team von 5". Other numbers were too noisy to check (years of experience,
# workload percentages, counts in project descriptions): calibrated 2026-11-26
# on 109 runs, all numbers flagged 62 runs, team sizes alone 25.
_TEAM_SIZE_RE = re.compile(
    r"(?<![\d.'’])(\d+)\s*-?\s*(?:köpfig|personen|mitarbeit|entwickler|leute)"
    r"|\bteams? (?:von|mit) (?:über |bis zu |rund )?(\d+)",
    re.IGNORECASE,
)
# Only claims about a team someone built, led or managed; a company size
# ("300 Mitarbeitende") or a staffing change ("von 4 auf 2 Personen") is no
# team that belongs in the CV.
_LEAD_RE = re.compile(r"\bteams?\b|projektteam|führung\b|geführt|leitung\b|geleitet|aufbau", re.I)
_STAFFING_RE = re.compile(r"\bvon (\d+) auf (\d+)\b", re.I)

_NUMBER_WORDS = {
    "2": "zwei", "3": "drei", "4": "vier", "5": "fünf", "6": "sechs", "7": "sieben",
    "8": "acht", "9": "neun", "10": "zehn", "11": "elf", "12": "zwölf",
}
# A size counts as present only next to a person or team noun, so "10 Agenten"
# or "seit 10 Jahren" never stands in for "über 10 Personen".
_PERSON_NOUN = (
    r"(?:köpfig|person|mitarbeit|entwickler|leute|produktmanager|product manager"
    r"|werkstudent|kolleg|fte|spezialist|fachleute|designer|engineer)"
)
_META_LINE_RE = re.compile(r"^\s*\*\*[^*\n]{1,40}:\*\*")
_RANGE_RE = re.compile(r"BELG-(\d+)\s*(?:[-–—]|bis)\s*(?:BELG-)?(\d+)")
_MAX_RANGE = 200


def _team_sizes(text: str) -> set[str]:
    if not _LEAD_RE.search(text):
        return set()
    staffing = {n for pair in _STAFFING_RE.findall(text) for n in pair}
    return {a or b for a, b in _TEAM_SIZE_RE.findall(text)} - staffing


def _size_in(size: str, text: str) -> bool:
    """The size as digits or German number word, right before a person or team noun."""
    tokens = [rf"(?<![\d.'’]){size}(?!\d)(?![.'’]\d)"]
    if size in _NUMBER_WORDS:
        tokens.append(rf"\b{_NUMBER_WORDS[size]}")
    tok = "(?:" + "|".join(tokens) + ")"
    near_noun = rf"{tok}[\s-]*(?:[^\s|]+\s+)?[^\s|]*?{_PERSON_NOUN}"
    team_of = rf"\bteams? (?:von|mit) (?:über |bis zu |rund |mehr als )?{tok}"
    return bool(re.search(near_noun, text, re.I) or re.search(team_of, text, re.I))


def _belg_ids(cell: str) -> list[str]:
    """All IDs in a cell, ranges "BELG-167–192" expanded (capped, never re-numbered)."""
    ids: list[str] = []
    for start, end in _RANGE_RE.findall(cell):
        lo, hi = int(start), int(end)
        if lo < hi <= lo + _MAX_RANGE:
            width = len(start)
            ids += [f"BELG-{n:0{width}d}" for n in range(lo, hi + 1)]
    ids += re.findall(r"BELG-\d+", _RANGE_RE.sub(" ", cell))
    return list(dict.fromkeys(ids))


def _cv_body(final_text: str) -> str:
    """CV text without the run header block and metadata lines."""
    body = final_text[final_text.find("## "):] if "## " in final_text else final_text
    return "\n".join(line for line in body.splitlines() if not _META_LINE_RE.match(line))


def _columns(header: list[str]) -> tuple[int, int]:
    """Indices of the evidence and status columns, from the table header if present."""
    lowered = [c.lower() for c in header]
    beleg = next((i for i, c in enumerate(lowered) if "beleg" in c), 1)
    status = next((i for i, c in enumerate(lowered) if c == "status"), 2)
    return beleg, status


def station_of_entries(entries: list[dict], standard_cv: Path) -> dict[str, str]:
    """`### ` station header of each standard-CV entry, from its line position."""
    if not standard_cv.is_file():
        return {}
    lines = standard_cv.read_text(encoding="utf-8").splitlines()
    stations: dict[str, str] = {}
    for entry in entries:
        pos = str(entry.get("quelle_position", ""))
        if entry.get("quelle_typ") != "standard_cv" or not pos.startswith("line:"):
            continue
        try:
            line_no = int(pos.split(":", 1)[1])
        except ValueError:
            continue
        header = next(
            (ln for ln in reversed(lines[: max(line_no - 1, 0)]) if ln.startswith("### ")), ""
        )
        if header:
            stations[entry.get("id", "")] = header
    return stations


def missing_strong_team_sizes(
    analysis: str,
    final_text: str,
    entries: list[dict],
    limits: list | None = None,
    stations: dict[str, str] | None = None,
) -> list[str]:
    """Strong evidence with a team size that the final text drops.

    The analyst rated a requirement STARK with a claim like "Leitung eines
    8-köpfigen Teams"; the writer left it out and nobody noticed until the
    external review. Entries whose wording hits a fact limit at their station
    are skipped, so the check never pushes towards a claim Alex has ruled out.
    """
    from cv_tailor import fact_limits

    by_id = {e.get("id"): e for e in entries}
    stations = stations or {}
    body = _cv_body(final_text)
    found: dict[str, str] = {}
    beleg_col, status_col = 1, 2
    for line in analysis.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if any(c.lower() == "status" for c in cells):
            beleg_col, status_col = _columns(cells)
            continue
        if len(cells) <= max(beleg_col, status_col):
            continue
        if not re.search(r"\bSTARK\b", cells[status_col].replace("*", "")):
            continue
        for bid in _belg_ids(cells[beleg_col]):
            entry = by_id.get(bid)
            if entry is None or bid in found:
                continue
            claim = entry.get("behauptung", "")
            missing = sorted(s for s in _team_sizes(claim) if not _size_in(s, body))
            if not missing:
                continue
            station = stations.get(bid, "")
            probe = f"{station}\n- {claim}" if station else claim
            if limits and fact_limits.check_text(probe, limits):
                continue
            found[bid] = f"{bid} ({'/'.join(missing)} Personen) für «{cells[0][:40]}»"
    return list(found.values())


def _clip(items: list[str], limit: int = 3) -> str:
    shown = [i.replace("\n", " ")[:120] for i in items[:limit]]
    more = f" (+{len(items) - limit} weitere)" if len(items) > limit else ""
    return "; ".join(shown) + more


def run_verify(
    run_dir: Path,
    *,
    with_pages: bool = True,
    standard_cv_path: Path | None = None,
    beleg_index_path: Path = Path("data/beleg_index.json"),
) -> list[CheckResult]:
    """Run the deterministic bundle in precedence order; never raises per check."""
    from cv_tailor import (
        consistency_check,
        fact_limits,
        length_check,
        redundancy_check,
        style_check,
    )
    from cv_tailor.pdf_renderer import STANDARD_CV_PATH
    from cv_tailor.rework import resolve_final

    std = standard_cv_path or STANDARD_CV_PATH
    final_de = resolve_final(run_dir, "de")
    if final_de is None:
        return [CheckResult("Endtext", "FAIL", "04_final_de.md fehlt")]
    text = final_de.read_text(encoding="utf-8")
    sections = {
        "management_summary": _section(text, "Management Summary"),
        "schluesselkompetenzen": _section(text, "Schlüsselkompetenzen"),
        "berufserfahrung": _section(text, "Berufserfahrung"),
    }
    ki = fact_limits.ki_posting_for_run(run_dir)
    results: list[CheckResult] = []

    def guard(name: str, fn) -> None:
        try:
            results.extend(fn())
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the rest
            results.append(
                CheckResult(name, "WARN", f"Check abgebrochen: {type(exc).__name__}: {exc}")
            )

    # 1. Facts
    def facts() -> list[CheckResult]:
        limits = fact_limits.load_fact_limits()
        if not limits:
            return [CheckResult("Faktengrenzen", "INFO", "keine data/fact_limits.yaml (No-op)")]
        hits = [f"{f.limit_id}: {f.excerpt[:70]}" for f in fact_limits.check_text(text, limits)]
        en = resolve_final(run_dir, "en")
        if en is not None:
            hits += [
                f"EN {f.limit_id}"
                for f in fact_limits.check_text(en.read_text(encoding="utf-8"), limits)
            ]
        return [
            CheckResult(
                "Faktengrenzen", "FAIL" if hits else "OK", _clip(hits) if hits else "keine Treffer"
            )
        ]

    def consistency() -> list[CheckResult]:
        ok, issues = consistency_check.validate_berufserfahrung(
            sections["berufserfahrung"], std, check_completeness=True
        )
        return [
            CheckResult(
                "Konsistenz (Header, Anker, Vollständigkeit)",
                "OK" if ok else "FAIL",
                "" if ok else _clip(issues),
            )
        ]

    def open_factcheck() -> list[CheckResult]:
        if not (run_dir / FACTCHECK_OPEN_FILE).is_file():
            return []
        block = factcheck_open_block(run_dir)
        return [
            CheckResult(
                "Faktencheck offene Punkte",
                "FAIL" if block else "OK",
                block or "per Stempel akzeptiert oder korrigiert",
            )
        ]

    # No guard: a broken check here must fail loudly, never degrade to WARN.
    results.extend(open_factcheck())
    guard("Faktengrenzen", facts)
    guard("Konsistenz", consistency)

    # 2. Length, pages and station rules
    def substance() -> list[CheckResult]:
        found = fact_limits.check_station_rules(
            sections["berufserfahrung"], fact_limits.load_station_rules(), ki=ki
        )
        label = "Pflichtsubstanz und Stationsbudget" + (" (KI-nahe Anzeige)" if ki else "")
        return [
            CheckResult(label, "WARN" if found else "OK", _clip(found) if found else "eingehalten")
        ]

    def length() -> list[CheckResult]:
        found = []
        for key, body in sections.items():
            found += [f"{key}: {i.kind}" for i in length_check.check_section_length(key, body)]
        return [
            CheckResult(
                "Länge (Summary, Kompetenzen, Bullets)",
                "WARN" if found else "OK",
                _clip(found) if found else "eingehalten",
            )
        ]

    def pages() -> list[CheckResult]:
        if not with_pages:
            return [CheckResult("Seiten", "INFO", "übersprungen (--no-pages)")]
        from cv_tailor.orchestrator import RunContext
        from cv_tailor.pdf_renderer import count_pages_de

        count = count_pages_de(
            RunContext(run_id=run_dir.name, run_dir=run_dir.resolve(), started_at="")
        )
        if count < 0:
            return [CheckResult("Seiten", "WARN", "Seitenzahl nicht ermittelbar")]
        status = "FAIL" if count > MAX_PAGES else "OK"
        return [CheckResult("Seiten", status, f"{count} von maximal {MAX_PAGES}")]

    def strong_evidence() -> list[CheckResult]:
        from cv_tailor.beleg_index import load_beleg_index

        analysis = run_dir / "01_analyse.md"
        entries = (
            load_beleg_index(beleg_index_path).get("entries", [])
            if analysis.is_file() and beleg_index_path.is_file()
            else []
        )
        if not entries:
            return [
                CheckResult("Starke Belege (Teamgrösse)", "INFO", "keine Analyse oder kein Index")
            ]
        found = missing_strong_team_sizes(
            analysis.read_text(encoding="utf-8"),
            text,
            entries,
            fact_limits.load_fact_limits(),
            station_of_entries(entries, std),
        )
        return [
            CheckResult(
                "Starke Belege (Teamgrösse)",
                "WARN" if found else "OK",
                _clip(found) if found else "im Endtext",
            )
        ]

    guard("Pflichtsubstanz", substance)
    guard("Starke Belege", strong_evidence)
    guard("Länge", length)
    guard("Seiten", pages)

    # 3. Style
    def style() -> list[CheckResult]:
        tokens = style_check.station_tokens(std)
        found = []
        for key, body in sections.items():
            found += [f"{key}: {f.kind}" for f in style_check.check_style(key, body, tokens)]
        return [
            CheckResult(
                "Stil (deterministisch)",
                "WARN" if found else "OK",
                _clip(found) if found else "keine Treffer",
            )
        ]

    guard("Stil", style)

    # 4. ATS (writes _ats.md; the PDF part is included when the PDF exists)
    def ats() -> list[CheckResult]:
        from cv_tailor.ats_check import write_ats_report

        _, befunde, hinweise = write_ats_report(run_dir)
        if befunde:
            return [
                CheckResult(
                    "ATS", "FAIL", f"{befunde} Befunde, {hinweise} Hinweise (siehe _ats.md)"
                )
            ]
        return [
            CheckResult(
                "ATS",
                "INFO" if hinweise else "OK",
                f"{hinweise} Hinweise (siehe _ats.md)" if hinweise else "keine",
            )
        ]

    guard("ATS", ats)

    # 5. Redundancy
    def redundancy() -> list[CheckResult]:
        found = [f.phrase for f in redundancy_check.check_cross_section_redundancy(text)]
        return [
            CheckResult(
                "Redundanz",
                "WARN" if found else "OK",
                _clip(found) if found else "keine wortgleichen Wiederholungen",
            )
        ]

    guard("Redundanz", redundancy)

    # Bold terms: the keyword marker runs once, hand edits can drop its marks
    def bold_terms() -> list[CheckResult]:
        missing = missing_bold_terms(text)
        return [
            CheckResult(
                "Fettmarkierung",
                "WARN" if missing else "OK",
                f"{'; '.join(missing)} (Marker erneut laufen lassen oder von Hand setzen)"
                if missing
                else "vorhanden",
            )
        ]

    guard("Fettmarkierung", bold_terms)

    # 6. Copy used for delivery
    def delivery() -> list[CheckResult]:
        canonical = run_dir / "04_final_de.md"
        newest = resolve_final(run_dir, "de")
        if newest is not None and canonical.is_file() and newest != canonical:
            same = newest.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")
            if not same:
                return [
                    CheckResult(
                        "Versandkopie",
                        "WARN",
                        f"{newest.name} weicht von 04_final_de.md ab (die jüngere gewinnt)",
                    )
                ]
        return [CheckResult("Versandkopie", "OK", "")]

    guard("Versandkopie", delivery)

    results.extend(_stamp_results(run_dir))
    return results


def format_table(results: list[CheckResult]) -> str:
    width = max((len(r.name) for r in results), default=10)
    lines = []
    for r in results:
        lines.append(f"[{r.status:4s}] {r.name:<{width}}  {r.detail}".rstrip())
    fails = sum(1 for r in results if r.status == "FAIL")
    warns = sum(1 for r in results if r.status == "WARN")
    lines.append("")
    lines.append(
        f"{fails} FAIL, {warns} WARN"
        + ("  → nicht übergeben, FAIL zuerst beheben." if fails else "")
    )
    return "\n".join(lines)
