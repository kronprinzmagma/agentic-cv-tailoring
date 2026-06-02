"""Deterministic eval suite for cv-tailor run artifacts."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "evals" / "cases"
REPORTS_DIR = ROOT / "evals" / "reports"


@dataclass
class CheckResult:
    metric: str
    name: str
    passed: bool
    detail: str


def _load_cases() -> list[dict]:
    cases = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["_path"] = path
        cases.append(data)
    return cases


def _resolve_run_dir(case: dict) -> Path | None:
    explicit = case.get("run_dir")
    if explicit:
        path = ROOT / explicit
        return path if path.exists() else None

    pattern = case.get("run_glob")
    if not pattern:
        return None
    matches = sorted((ROOT / "runs").glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _extract_section(cv_text: str, section_name: str) -> str:
    """Extract the body of a named ## section from a CV markdown string."""
    pattern = re.compile(
        rf"^##\s+{re.escape(section_name)}\s*$\n(?P<body>.*?)(?=^---\s*$|^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(cv_text)
    if not match:
        return ""
    return match.group("body").strip()


def _extract_summary(final_cv: str) -> str:
    return _extract_section(final_cv, "Management Summary")


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\wäöüÄÖÜéèàÉÈÀß-]+\b", text))


def _diff_rows(diff_text: str) -> int:
    return sum(
        1
        for line in diff_text.splitlines()
        if line.startswith("|")
        and not line.startswith("|---")
        and "Original-Snippet" not in line
        and "Neu-Snippet" not in line
    )


def _count_long_bullets(cv_text: str, max_chars: int = 180) -> list[str]:
    """Return bullet lines that exceed max_chars (proxy for >2 visual lines in print)."""
    long_bullets = []
    for line in cv_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and len(stripped) > max_chars:
            long_bullets.append(stripped[:80] + "…")
    return long_bullets


def _contains_checks(
    metric: str, label: str, haystack: str, values: list[str], expected: bool
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for value in values:
        found = value in haystack
        passed = found is expected
        verb = "gefunden" if found else "nicht gefunden"
        results.append(CheckResult(metric, f"{label}: {value}", passed, verb))
    return results


def _parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    return json.loads(text)


def _run_llm_judge(
    case: dict, run_dir: Path, final_cv: str, diff_text: str
) -> tuple[list[CheckResult], list[str]]:
    """Run the LLM judge and return (check_results, improvement_notes)."""
    from dotenv import load_dotenv
    from cv_tailor.llm import call_llm

    load_dotenv(dotenv_path=ROOT / ".env")

    artifacts = case.get("artifacts", {})
    job_path = run_dir / artifacts.get("job_ad", "00_stellenanzeige.md")
    answers_path = run_dir / artifacts.get("answers", "02_antworten.md")
    analysis_path = run_dir / artifacts.get("analysis", "01_analyse.md")

    judge_cfg = case.get("judge", {})
    if not judge_cfg:
        return [
            CheckResult(
                "llm_as_judge",
                "optional_judge",
                True,
                "Übersprungen: kein `judge:`-Block im Case",
            )
        ], []

    rubric = judge_cfg.get(
        "rubric",
        "Bewerte Rollenfit, Faktentreue, Spezifität, Knappheit und Tonalität für einen Schweizer Senior-Product-CV.",
    )
    min_score = int(judge_cfg.get("min_score", 4))
    min_factfulness = int(judge_cfg.get("min_factfulness", 4))
    max_critical_issues = int(judge_cfg.get("max_critical_issues", 0))

    context = {
        "case_id": case.get("id"),
        "rubric": rubric,
        "job_ad": job_path.read_text(encoding="utf-8") if job_path.exists() else "",
        "clarification_answers": answers_path.read_text(encoding="utf-8") if answers_path.exists() else "",
        "analysis": analysis_path.read_text(encoding="utf-8") if analysis_path.exists() else "",
        "final_cv": final_cv,
        "diff": diff_text,
    }
    system = (
        "Du bist ein skeptischer CV-Eval-Judge. Deine Aufgabe ist nicht, den Text nett zu finden, "
        "sondern unbelegte Übertreibungen, Rollen-Mismatch, generische Aussagen und fehlende Zielrollenpassung "
        "zu identifizieren. Sei streng: Wenn ein Claim nur plausibel klingt, aber nicht aus Anzeige, Analyse "
        "oder Klärungsantworten belegbar ist, markiere ihn als Risiko. Bewerte nicht die Reihenfolge der Inputs "
        "und bevorzuge keine Position; bewerte ausschliesslich anhand der Rubrik. "
        "`critical_issues` darf nur harte Blocker enthalten, die eine Einreichung verhindern sollten: "
        "unbelegte Behauptungen, klare Überzeichnung, gravierender Rollen-Mismatch oder ausgelassene Muss-Anforderung. "
        "Normale Verbesserungen, Schärfungen oder optionale Feinschliffpunkte gehören ausschliesslich in "
        "`improvement_notes`. Ein konservativer CV darf nicht als kritisch markiert werden, nur weil er "
        "riskante Übertreibungen vermeidet. Nicht erfüllte Nice-to-have- oder Ideal-Kriterien sind keine "
        "`critical_issues`, solange der CV sie nicht fälschlich behauptet. Gib nur JSON aus."
    )
    user = (
        "Bewerte diesen CV-Output anhand der Rubrik.\n\n"
        f"{json.dumps(context, ensure_ascii=False)}\n\n"
        "JSON-Schema:\n"
        "{\n"
        '  "overall_score": 1-5,\n'
        '  "factfulness_score": 1-5,\n'
        '  "role_fit_score": 1-5,\n'
        '  "specificity_score": 1-5,\n'
        '  "concision_score": 1-5,\n'
        '  "critical_issues": ["kurze Liste harter Blocker"],\n'
        '  "improvement_notes": ["max. 5 konkrete Hinweise"]\n'
        "}"
    )
    raw = call_llm(
        agent="eval_judge",
        phase="eval_llm_judge",
        run_id=str(case.get("id", "eval")),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1200,
        snippet_text=final_cv[:1000],
    )
    try:
        parsed = _parse_json_object(raw)
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("llm_as_judge", "parse_json", False, f"{type(exc).__name__}: {exc}")], []

    critical = parsed.get("critical_issues") or []
    notes = parsed.get("improvement_notes") or []
    overall = int(parsed.get("overall_score", 0) or 0)
    factfulness = int(parsed.get("factfulness_score", 0) or 0)
    detail = (
        f"overall={overall}/{min_score}, factfulness={factfulness}/{min_factfulness}, "
        f"critical={len(critical)}/{max_critical_issues}; "
        f"critical_issues={'; '.join(critical[:3])}; "
        f"notes={'; '.join(notes[:3])}"
    )
    check = CheckResult(
        "llm_as_judge",
        "rubric_threshold",
        overall >= min_score and factfulness >= min_factfulness and len(critical) <= max_critical_issues,
        detail,
    )
    return [check], list(notes)


def _evaluate_case(
    case: dict, *, llm_judge: bool = False
) -> tuple[Path | None, list[CheckResult], list[str]]:
    """Evaluate a case. Returns (run_dir, check_results, improvement_notes)."""
    run_dir = _resolve_run_dir(case)
    if run_dir is None:
        return None, [CheckResult("setup", "run_dir", False, "Kein passender Run gefunden")], []

    artifacts = case.get("artifacts", {})
    final_path = run_dir / artifacts.get("final_cv", "04_final_de.md")
    diff_path = run_dir / artifacts.get("diff", "05_diff.md")

    if not final_path.exists():
        return run_dir, [CheckResult("setup", "final_cv", False, f"Fehlt: {final_path}")], []
    if not diff_path.exists():
        return run_dir, [CheckResult("setup", "diff", False, f"Fehlt: {diff_path}")], []

    final_cv = final_path.read_text(encoding="utf-8")
    diff_text = diff_path.read_text(encoding="utf-8")
    summary = _extract_summary(final_cv)
    checks = case.get("checks", {})

    results: list[CheckResult] = []

    # --- Global contains checks ---
    results.extend(
        _contains_checks(
            "vocabulary_coverage",
            "required_final_contains",
            final_cv,
            checks.get("required_final_contains", []),
            True,
        )
    )
    results.extend(
        _contains_checks(
            "factfulness",
            "forbidden_final_contains",
            final_cv,
            checks.get("forbidden_final_contains", []),
            False,
        )
    )
    results.extend(
        _contains_checks(
            "diff_granularity",
            "required_diff_contains",
            diff_text,
            checks.get("required_diff_contains", []),
            True,
        )
    )

    # --- Section-scoped contains checks ---
    for section_name, strings in checks.get("required_in_section", {}).items():
        section_text = _extract_section(final_cv, section_name)
        if not section_text:
            results.append(
                CheckResult(
                    "section_placement",
                    f"section_found: {section_name}",
                    False,
                    f"Abschnitt '{section_name}' nicht gefunden",
                )
            )
        else:
            results.extend(
                _contains_checks(
                    "section_placement",
                    f"required_in_section[{section_name}]",
                    section_text,
                    strings,
                    True,
                )
            )

    for section_name, strings in checks.get("forbidden_in_section", {}).items():
        section_text = _extract_section(final_cv, section_name)
        if section_text:
            results.extend(
                _contains_checks(
                    "section_placement",
                    f"forbidden_in_section[{section_name}]",
                    section_text,
                    strings,
                    False,
                )
            )

    # --- Length drift ---
    max_summary_words = checks.get("max_summary_words")
    if max_summary_words is not None:
        count = _word_count(summary)
        results.append(
            CheckResult(
                "length_drift",
                "max_summary_words",
                count <= int(max_summary_words),
                f"{count}/{max_summary_words} Wörter",
            )
        )

    # --- Bullet length (scan-test proxy) ---
    max_bullet_chars = checks.get("max_bullet_chars", 180)
    long = _count_long_bullets(final_cv, max_chars=int(max_bullet_chars))
    results.append(
        CheckResult(
            "scan_test",
            "max_bullet_length",
            len(long) == 0,
            f"OK — alle Bullets ≤{max_bullet_chars} Zeichen"
            if not long
            else f"{len(long)} Bullet(s) zu lang: {long[0]}",
        )
    )

    # --- Diff checks ---
    max_diff_rows = checks.get("max_diff_rows")
    if max_diff_rows is not None:
        rows = _diff_rows(diff_text)
        results.append(
            CheckResult(
                "diff_granularity",
                "max_diff_rows",
                rows <= int(max_diff_rows),
                f"{rows}/{max_diff_rows} Tabellenzeilen",
            )
        )

    expected_header = "| Abschnitt | Original-Snippet | Neu-Snippet | Grund (max. 10 Wörter) |"
    results.append(
        CheckResult(
            "diff_granularity",
            "diff_header",
            expected_header in diff_text,
            "Header korrekt" if expected_header in diff_text else "Header fehlt/abweichend",
        )
    )

    # --- LLM Judge ---
    improvement_notes: list[str] = []
    if llm_judge:
        judge_results, improvement_notes = _run_llm_judge(case, run_dir, final_cv, diff_text)
        results.extend(judge_results)
    else:
        results.append(
            CheckResult(
                "llm_as_judge",
                "optional_judge",
                True,
                "Übersprungen: mit `uv run cv-tailor eval --judge` aktivieren",
            )
        )

    return run_dir, results, improvement_notes


def _write_notes_summary(
    notes_by_case: dict[str, list[str]], lines: list[str]
) -> None:
    """Append an aggregated improvement-notes section to the report lines."""
    all_notes = [
        (case_id, note)
        for case_id, notes in notes_by_case.items()
        for note in notes
    ]
    if not all_notes:
        return

    lines.extend([
        "---",
        "",
        "## Judge Improvement Notes (aggregiert)",
        "",
        "Wiederkehrende Hinweise über alle Cases. Nicht als Blocker gewertet — "
        "dienen als Signal für Prompt- oder Config-Anpassungen nach ~5 Läufen.",
        "",
    ])
    for case_id, note in all_notes:
        lines.append(f"- **[{case_id}]** {note}")
    lines.append("")


def _write_report(
    case_reports: list[tuple[dict, Path | None, list[CheckResult], list[str]]]
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"eval_{now}.md"

    total = sum(len(results) for _, _, results, _ in case_reports)
    passed = sum(1 for _, _, results, _ in case_reports for result in results if result.passed)

    lines = [
        "# cv-tailor Eval Report",
        "",
        f"**Erstellt:** {datetime.now(timezone.utc).isoformat()}",
        f"**Gesamt:** {passed}/{total} Checks bestanden",
        "",
    ]
    notes_by_case: dict[str, list[str]] = {}
    for case, run_dir, results, improvement_notes in case_reports:
        case_id = case.get("id", case.get("_path", "unknown"))
        lines.extend(
            [
                f"## {case_id}",
                "",
                f"**Beschreibung:** {case.get('description', '-')}",
                f"**Run:** {run_dir.relative_to(ROOT) if run_dir else 'nicht gefunden'}",
                "",
                "| Metric | Check | Status | Detail |",
                "|---|---|---|---|",
            ]
        )
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            lines.append(f"| {result.metric} | {result.name} | {status} | {result.detail} |")
        lines.append("")
        if improvement_notes:
            notes_by_case[str(case_id)] = improvement_notes

    _write_notes_summary(notes_by_case, lines)

    report_path.write_text("\n".join(lines), encoding="utf-8")
    (REPORTS_DIR / "latest.md").write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main(*, llm_judge: bool = False) -> int:
    cases = _load_cases()
    if not cases:
        print("Keine Eval-Cases gefunden in evals/cases/.")
        return 1

    case_reports = []
    for case in cases:
        run_dir, results, notes = _evaluate_case(case, llm_judge=llm_judge)
        case_reports.append((case, run_dir, results, notes))

    report_path = _write_report(case_reports)
    total = sum(len(results) for _, _, results, _ in case_reports)
    passed = sum(1 for _, _, results, _ in case_reports for result in results if result.passed)
    failed = total - passed

    print(f"Eval abgeschlossen: {passed}/{total} Checks bestanden")
    print(f"Report: {report_path.relative_to(ROOT)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
