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
STEPS = ("naturalisation", "ki-stil-check", "quellenabgleich", "chatgpt")
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


def write_stamp(run_dir: Path, step: str, note: str = "") -> dict:
    if step not in STEPS:
        raise ValueError(f"Unbekannter Schritt: {step} (erlaubt: {', '.join(STEPS)})")
    stamps = read_stamps(run_dir)
    stamps[step] = {
        "hash": text_hash(run_dir),
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": note,
    }
    tmp = run_dir / (STAMP_FILE + ".tmp")
    tmp.write_text(json.dumps(stamps, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(run_dir / STAMP_FILE)
    return stamps[step]


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


def _clip(items: list[str], limit: int = 3) -> str:
    shown = [i.replace("\n", " ")[:120] for i in items[:limit]]
    more = f" (+{len(items) - limit} weitere)" if len(items) > limit else ""
    return "; ".join(shown) + more


def run_verify(
    run_dir: Path,
    *,
    with_pages: bool = True,
    standard_cv_path: Path | None = None,
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

    guard("Pflichtsubstanz", substance)
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
