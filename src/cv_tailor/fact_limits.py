"""Faktengrenzen: hard limits on CV claims that Alex has settled once.

Source: `data/fact_limits.yaml` (private, gitignored). One file, two uses:

1. **Prompt block** (`format_for_prompt`) — the settled facts go into the
   cached prefix of analyst, writer, coach and factcheck, regardless of
   clarification ranking. Review of the October runs (2026-10-23): the same
   six corrections were made by hand in every CV (KI rollout, leadership
   type, GastroSaaS role …) because the deciding answers had aged out of the
   clarification window or never were clarifications at all.
2. **Deterministic check** (`check_text`) — regex findings per line, no LLM.
   The writer loop feeds them back as Pflichtkorrektur (soft veto, like
   `length_check`); after the translator a report covers the final DE/EN CV
   (`_fact_limits.md`); `cv-tailor check-claims` re-runs it after hand edits.
   Two of the October CVs went out with «disziplinarisch» in the
   Schlüsselkompetenzen — introduced by hand, after every pipeline gate.

Same contract as `idiom_check`: only high-precision patterns. A finding that
is usually wrong trains the reader to ignore the report.

YAML schema:

    limits:
      - id: fuehrung_disziplinarisch
        fact: "Disziplinarisch geführt nur bei MediaHoldingCo (5 PM + 2 Werkstudent:innen) …"
        checks:
          - pattern: "disziplinar|disciplinar"   # regex, case-insensitive, per line
            also: "…"                             # optional: must match the same line too
            unless: "…"                           # optional: line is fine if this matches
            only_in_station: "MediaHoldingCo"            # optional: allowed inside this `### ` block
                                                  # (a list matches any — EN headers differ)
            message: "«disziplinarisch» nur in der MediaHoldingCo-Station."
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml

log = structlog.get_logger()

FACT_LIMITS_PATH = Path("data/fact_limits.yaml")
REPORT_FILENAME = "_fact_limits.md"
PROMPT_TITLE = (
    "## Faktengrenzen (von Alex bestätigt — verbindlich, gehen dem Standard-CV-Wortlaut vor)"
)

_STATION_RE = re.compile(r"^###\s+")
# Lines that are metadata or headings, never claims.
_SKIP_LINE_RE = re.compile(r"^\s*(#|\*\*(Run|Erstellt|Created|Rolle|Role):\*\*)")


@dataclass(frozen=True)
class Check:
    pattern: re.Pattern[str]
    message: str
    also: re.Pattern[str] | None = None
    unless: re.Pattern[str] | None = None
    only_in_station: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactLimit:
    id: str
    fact: str
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class Finding:
    limit_id: str
    line_no: int
    excerpt: str
    message: str


def _compile(expr: str | None) -> re.Pattern[str] | None:
    return re.compile(expr, re.IGNORECASE) if expr else None


def _as_tuple(value: str | list[str] | None) -> tuple[str, ...]:
    if not value:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


_cache: dict[str, tuple[float, list[FactLimit]]] = {}


def load_fact_limits(path: Path = FACT_LIMITS_PATH) -> list[FactLimit]:
    """Parse the limits file; [] when it does not exist (public clone, tests)."""
    if not path.is_file():
        return []
    key = str(path.resolve())
    mtime = path.stat().st_mtime
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    limits: list[FactLimit] = []
    for raw in data.get("limits", []):
        checks = tuple(
            Check(
                pattern=re.compile(c["pattern"], re.IGNORECASE),
                message=c["message"],
                also=_compile(c.get("also")),
                unless=_compile(c.get("unless")),
                only_in_station=_as_tuple(c.get("only_in_station")),
            )
            for c in raw.get("checks", [])
        )
        limits.append(FactLimit(id=raw["id"], fact=" ".join(raw["fact"].split()), checks=checks))
    _cache[key] = (mtime, limits)
    return limits


def format_for_prompt(limits: list[FactLimit]) -> str:
    """Prompt block with every settled fact; '' when there are none."""
    if not limits:
        return ""
    lines = [
        PROMPT_TITLE,
        "Abschliessend geklärt. Nicht erneut nachfragen, nicht abschwächen, nicht "
        "überschreiten. Widerspricht ein Standard-CV- oder Beleg-Wortlaut einer "
        "Grenze, gilt die Grenze.",
        "",
    ]
    lines.extend(f"- {limit.fact}" for limit in limits)
    return "\n".join(lines)


def prompt_block(path: Path = FACT_LIMITS_PATH) -> str:
    """The settled-facts block for agent prompts; '' without a limits file.

    Includes the per-station Pflichtsubstanz: the writer is asked to use it, so
    factcheck and coach must see it as settled, not as unbelegt.
    """
    block = format_for_prompt(load_fact_limits(path))
    substance = format_station_substance(load_station_rules(path))
    return "\n\n".join(part for part in (block, substance) if part)


# --- Pflichtsubstanz und Wortbudget pro Station ----------------------------
#
# Ideen-Scouting 2026-10-23, Kanal A: Nacharbeit ergänzt vor allem belegte
# Substanz, die der Writer weglässt (34 Läufe: «KI-Gilde ins Leben gerufen» 8×,
# «wenig genutzte Features eingestellt» 7×, «Releasezeit halbiert» 6× von Hand
# eingefügt, nie gestrichen). Gegenstück zu den Verboten: was pro Station
# drinstehen soll, plus ein Wortbudget, damit der CV bei drei Seiten bleibt.


@dataclass(frozen=True)
class PflichtFakt:
    id: str
    text: str
    muster: re.Pattern[str]


@dataclass(frozen=True)
class StationRule:
    station: str
    max_woerter: int | None = None
    min_pflicht: int = 0
    min_zeilen: int = 0
    pflicht: tuple[PflichtFakt, ...] = ()


@dataclass(frozen=True)
class StationRules:
    stations: tuple[StationRule, ...] = ()
    max_total_words: int | None = None

    def for_header(self, header: str) -> StationRule | None:
        lowered = header.lower()
        return next((r for r in self.stations if r.station.lower() in lowered), None)


def load_station_rules(path: Path = FACT_LIMITS_PATH) -> StationRules:
    """Per-station rules from the limits file; empty without file or section."""
    if not path.is_file():
        return StationRules()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    stations = tuple(
        StationRule(
            station=raw["station"],
            max_woerter=raw.get("max_woerter"),
            min_pflicht=int(raw.get("min_pflicht", 0)),
            min_zeilen=int(raw.get("min_zeilen", 0)),
            pflicht=tuple(
                PflichtFakt(
                    f["id"], " ".join(f["text"].split()), re.compile(f["muster"], re.IGNORECASE)
                )
                for f in raw.get("pflicht", [])
            ),
        )
        for raw in data.get("stationen", [])
    )
    return StationRules(stations, data.get("berufserfahrung_max_woerter"))


def format_station_substance(rules: StationRules) -> str:
    """Prompt section listing the Pflichtsubstanz as settled facts."""
    with_facts = [r for r in rules.stations if r.pflicht]
    if not with_facts:
        return ""
    lines = [
        "### Pflichtsubstanz pro Station (von Alex bestätigt, gilt als belegt)",
        "",
    ]
    for rule in with_facts:
        lines.append(f"- {rule.station}: " + " · ".join(f.text for f in rule.pflicht))
    return "\n".join(lines)


def skeleton_notes(rules: StationRules, header: str) -> list[str]:
    """Lines the station scaffold adds under a header: budget and Pflichtsubstanz."""
    rule = rules.for_header(header)
    if rule is None:
        return []
    notes: list[str] = []
    if rule.max_woerter:
        notes.append(f"- Wortbudget: höchstens {rule.max_woerter} Wörter in dieser Station")
    if rule.min_zeilen:
        notes.append(f"- mindestens {rule.min_zeilen} Bullets")
    if rule.pflicht:
        notes.append(
            f"- Pflichtsubstanz, mindestens {rule.min_pflicht} davon aufnehmen "
            "(nah am Wortlaut, Auswahl nach Anzeige):"
        )
        notes.extend(f"  - {fact.text}" for fact in rule.pflicht)
    return notes


def _station_blocks(section_text: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    for line in section_text.splitlines():
        if _STATION_RE.match(line):
            blocks.append((line, []))
        elif blocks and line.strip() and line.strip() != "---":
            blocks[-1][1].append(line.replace("**", "").strip())
    return blocks


def _word_count(lines: list[str]) -> int:
    return sum(len(re.findall(r"\w+", line)) for line in lines)


def check_station_rules(section_text: str, rules: StationRules) -> list[str]:
    """Findings for Berufserfahrung: missing Pflichtsubstanz, budgets, bullets."""
    findings: list[str] = []
    total = 0
    for header, body in _station_blocks(section_text):
        words = _word_count(body)
        total += words
        rule = rules.for_header(header)
        if rule is None:
            continue
        name = header.lstrip("# ").strip()
        if rule.max_woerter and words > rule.max_woerter:
            findings.append(
                f"{name}: {words} Wörter, Budget {rule.max_woerter}. "
                "Streichen, nicht umformulieren."
            )
        if rule.min_zeilen and len(body) < rule.min_zeilen:
            findings.append(f"{name}: {len(body)} Bullets, mindestens {rule.min_zeilen} verlangt.")
        if rule.pflicht:
            text = " ".join(body)
            present = [f for f in rule.pflicht if f.muster.search(text)]
            if len(present) < rule.min_pflicht:
                missing = [f.text for f in rule.pflicht if f not in present]
                findings.append(
                    f"{name}: {len(present)} von mindestens {rule.min_pflicht} Pflichtfakten. "
                    "Kandidaten: " + " · ".join(missing)
                )
    if rules.max_total_words and total > rules.max_total_words:
        findings.append(
            f"Berufserfahrung gesamt {total} Wörter, Budget {rules.max_total_words} "
            "(sonst mehr als drei PDF-Seiten). In den Stationen ausserhalb des Hauptsignals kürzen."
        )
    return findings


def format_station_findings(findings: list[str]) -> str:
    if not findings:
        return "Pflichtsubstanz und Wortbudgets eingehalten.\n"
    lines = [
        "# Pflichtsubstanz und Wortbudget",
        "",
        "Fehlende Pflichtfakten aus den Kandidaten ergänzen (belegt, nah am Wortlaut); "
        "zu lange Stationen kürzen. Beides zusammen: ergänzen, was fehlt, und dafür "
        "Schwächeres streichen.",
        "",
    ]
    lines.extend(f"{i}. {f}" for i, f in enumerate(findings, start=1))
    return "\n".join(lines) + "\n"


def check_text(text: str, limits: list[FactLimit]) -> list[Finding]:
    """Line-wise findings for every check that fires; station-aware."""
    findings: list[Finding] = []
    station = ""
    for line_no, line in enumerate(text.splitlines(), start=1):
        if _STATION_RE.match(line):
            station = line
            continue
        if not line.strip() or _SKIP_LINE_RE.match(line):
            continue
        plain = line.replace("**", "")
        for limit in limits:
            for check in limit.checks:
                if not check.pattern.search(plain):
                    continue
                if check.also and not check.also.search(plain):
                    continue
                if check.unless and check.unless.search(plain):
                    continue
                if any(token.lower() in station.lower() for token in check.only_in_station):
                    continue
                findings.append(Finding(limit.id, line_no, plain.strip()[:200], check.message))
                break  # one finding per limit and line
    return findings


def format_findings_for_writer(findings: list[Finding]) -> str:
    """Pflichtkorrektur block for the next writer round."""
    if not findings:
        return "Keine Faktengrenze verletzt.\n"
    lines = [
        "# Faktengrenzen verletzt",
        "",
        "Jede Zeile unten widerspricht einer von Alex abschliessend geklärten Grenze. "
        "Umformulieren oder streichen — nicht durch eine andere Überzeichnung ersetzen.",
        "",
    ]
    for i, f in enumerate(findings, start=1):
        lines.append(f"{i}. {f.message}")
        lines.append(f"   Zeile {f.line_no}: «{f.excerpt}»")
    return "\n".join(lines) + "\n"


def format_report(findings_by_file: dict[str, list[Finding]]) -> str:
    lines = [
        "# Faktengrenzen-Check",
        "",
        "Deterministischer Abgleich gegen `data/fact_limits.yaml`. Report-only: "
        "vor der Übergabe korrigieren oder bewusst stehen lassen.",
    ]
    for name, findings in findings_by_file.items():
        lines += ["", f"## {name}", ""]
        if not findings:
            lines.append("Keine Faktengrenze verletzt.")
            continue
        for f in findings:
            lines.append(f"- **{f.limit_id}** (Zeile {f.line_no}): {f.message}")
            lines.append(f"  «{f.excerpt}»")
    return "\n".join(lines) + "\n"


def check_files(
    paths: list[Path], limits: list[FactLimit] | None = None
) -> dict[str, list[Finding]]:
    limits = load_fact_limits() if limits is None else limits
    return {
        p.name: check_text(p.read_text(encoding="utf-8"), limits)
        for p in paths
        if p.is_file()
    }


def write_run_report(
    run_dir: Path, limits: list[FactLimit] | None = None
) -> tuple[Path | None, int]:
    """Check the final CV(s) of a run; write `_fact_limits.md` when anything fires.

    Returns (report path or None, number of findings). A stale report from an
    earlier check is removed once the CV is clean.
    """
    limits = load_fact_limits() if limits is None else limits
    report_path = run_dir / REPORT_FILENAME
    if not limits:
        return None, 0
    # The file that will actually ship: hand edits usually land in the
    # friendly copy (review 2026-10-23, R14). Same rule as the renderer.
    from cv_tailor.rework import resolve_final

    paths = [p for lang in ("de", "en") if (p := resolve_final(run_dir, lang)) is not None]
    results = check_files(paths, limits)
    total = sum(len(f) for f in results.values())
    if total == 0:
        report_path.unlink(missing_ok=True)
        return None, 0
    report_path.write_text(format_report(results), encoding="utf-8")
    log.info("fact_limits.report_written", run_dir=str(run_dir), findings=total)
    return report_path, total
