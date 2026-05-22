"""CLI entry point for cv-tailor."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from dotenv import load_dotenv


def _run_eval_suite_after_run() -> None:
    """Run the deterministic eval suite and print a one-line summary.

    Called automatically after every successful `run` and `continue` command.
    Failures are printed as warnings but never abort the CLI — the CV is already
    written and the eval result is informational.
    """
    import importlib.util

    evals_dir = (Path(__file__).parents[2] / "evals").resolve()
    eval_run_path = evals_dir / "run.py"
    if not eval_run_path.exists():
        return  # eval suite not present — skip silently
    # WR-03: same path guard as the public `eval` command — defence-in-depth
    if not eval_run_path.resolve().is_relative_to(evals_dir):
        return

    try:
        spec = importlib.util.spec_from_file_location("cv_tailor_eval_run", eval_run_path)
        if spec is None:  # IN-03: explicit None check for opaque AttributeError prevention
            return
        mod = importlib.util.module_from_spec(spec)
        sys.modules["cv_tailor_eval_run"] = mod
        spec.loader.exec_module(mod)

        cases = mod._load_cases()
        case_reports = []
        for case in cases:
            run_dir, results, notes = mod._evaluate_case(case, llm_judge=False)
            case_reports.append((case, run_dir, results, notes))

        report_path = mod._write_report(case_reports)
        total = sum(len(r) for _, _, r, _ in case_reports)
        passed = sum(1 for _, _, r, _ in case_reports for res in r if res.passed)
        failed = total - passed

        click.echo("")
        click.echo("─" * 60)
        if failed == 0:
            click.echo(f"Eval: {passed}/{total} Checks bestanden ✓")
        else:
            click.echo(f"Eval: {passed}/{total} Checks bestanden — {failed} Failure(s)")
        rel = report_path.relative_to(Path(__file__).resolve().parents[2])
        click.echo(f"Report: {rel}")
        click.echo("─" * 60)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"\n[Eval übersprungen: {type(exc).__name__}: {exc}]", err=True)


@click.group()
@click.version_option(package_name="cv-tailor")
def cli() -> None:
    """cv-tailor — tailor a CV to a job posting via a multi-agent pipeline."""


@cli.command()
@click.option(
    "--cv",
    "cv_path",
    type=click.Path(path_type=Path),
    default=Path("data/standard_cv.md"),
    show_default=True,
    help="Path to the Standard-CV (Markdown).",
)
@click.option(
    "--zeugnisse",
    "zeugnis_dir",
    type=click.Path(path_type=Path),
    default=Path("data/zeugnisse"),
    show_default=True,
    help="Directory containing Zeugnis PDFs.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=Path("data/beleg_index.json"),
    show_default=True,
    help="Output path for beleg_index.json.",
)
@click.option(
    "--no-samples",
    is_flag=True,
    default=False,
    help="Skip the 10-sample console output.",
)
def bootstrap(cv_path: Path, zeugnis_dir: Path, out_path: Path, no_samples: bool) -> None:
    """Build the Beleg-Index from Standard-CV (Markdown) and Zeugnisse (PDFs).

    Reads data/standard_cv.md and data/zeugnisse/*.pdf, runs deterministic
    rule-based extraction plus LLM classification, and writes
    data/beleg_index.json. Prints 10 random samples for manual spot-check.
    """
    load_dotenv()
    from cv_tailor.llm import require_llm_environment

    try:
        require_llm_environment()
    except RuntimeError as exc:
        click.echo(f"Fehler: {exc}", err=True)
        sys.exit(1)

    # Local import to avoid loading litellm/yaml on every CLI invocation.
    from cv_tailor.beleg_index import (
        build_beleg_index,
        format_samples_for_display,
        write_beleg_index,
    )

    try:
        index = build_beleg_index(cv_path=cv_path, zeugnis_dir=zeugnis_dir)
        write_beleg_index(index, out_path)
    except FileNotFoundError as exc:
        click.echo(f"Fehler: {exc}", err=True)
        click.echo(
            "Hinweis: Lege deine Quelldateien an den Standardpfaden ab "
            "(data/standard_cv.md, data/zeugnisse/*.pdf) oder nutze --cv / --zeugnisse.",
            err=True,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Unerwarteter Fehler: {type(exc).__name__}: {exc}", err=True)
        sys.exit(2)

    n_entries = len(index.get("entries", []))
    click.echo(f"Beleg-Index geschrieben: {out_path} ({n_entries} Einträge)")

    if not no_samples and n_entries > 0:
        click.echo("")
        click.echo(format_samples_for_display(index, n=10))


@cli.command()
@click.argument("stellenanzeige", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def run(stellenanzeige: Path) -> None:
    """Start a tailoring run for the given job posting file."""
    load_dotenv()
    from cv_tailor.llm import require_llm_environment
    from cv_tailor.orchestrator import init_run, write_run_log_entry
    from cv_tailor.agents.analyst import run_analyst
    from cv_tailor.agents.factcheck import run_factcheck

    try:
        require_llm_environment()
    except RuntimeError as exc:
        click.echo(f"Fehler: {exc}", err=True)
        sys.exit(1)

    try:
        ctx = init_run(stellenanzeige)
        click.echo(f"Run gestartet: {ctx.run_id}")
        click.echo(f"  Verzeichnis: {ctx.run_dir}")

        write_run_log_entry(ctx.run_dir, "run", f"Stellenanzeige: {stellenanzeige}")

        click.echo("Analyst läuft …")
        analyse_path = run_analyst(ctx)
        click.echo(f"  01_analyse.md geschrieben ({analyse_path.stat().st_size} Bytes)")

        # Profile-Fit-Gate — deterministic LÜCKE-check on the analyst table.
        # Stops the run before the expensive writer-loop if the role has
        # critical gaps Alex can't fill without overstating.
        from cv_tailor.profile_fit import check_profile_fit, format_gaps_for_cli, has_critical_gaps
        fit_gaps = check_profile_fit(analyse_path)
        if fit_gaps:
            click.echo("")
            click.echo(format_gaps_for_cli(fit_gaps))
            default_continue = not has_critical_gaps(fit_gaps)
            if not click.confirm("Trotzdem fortfahren?", default=default_continue):
                click.echo("Abgebrochen — keine Token verbraucht für Writer-Loop.")
                write_run_log_entry(ctx.run_dir, "profile_fit", "Run vom User wegen Profile-Fit-Gaps abgebrochen")
                sys.exit(0)
            write_run_log_entry(
                ctx.run_dir,
                "profile_fit",
                f"User bestätigt trotz {len(fit_gaps)} Profile-Fit-Hinweis(en)",
            )

        click.echo("Faktencheck läuft …")
        has_gaps = run_factcheck(ctx)

        if has_gaps:
            click.echo("")
            click.echo("=" * 60)
            click.echo("PAUSE: Faktencheck hat Belegbarkeitslücken identifiziert.")
            click.echo(f"  Klärungsfragen: {ctx.run_dir}/02_klaerungsfragen.md")
            click.echo("")
            click.echo("Bitte lies die Klärungsfragen und führe dann aus:")
            click.echo("  uv run cv-tailor continue")
            click.echo("=" * 60)
            sys.exit(0)

        click.echo("Analyse abgeschlossen — keine Klärungsfragen.")
        click.echo("Writer-Schleife läuft (3 Abschnitte parallel, max 2 Runden je) …")
        from cv_tailor.agents.writer_loop import run_writer_loop
        final_path = run_writer_loop(ctx, progress_cb=click.echo)
        click.echo(f"  04_final_de.md geschrieben ({final_path.stat().st_size} Bytes)")
        write_run_log_entry(ctx.run_dir, "run", "Pipeline Phase 4 abgeschlossen")

        # Post-writer stages (Diff → Keyword-Marker → Translator →
        # Friendly-Copy → Quality-Snapshot) are bundled in
        # `pipeline_stages.run_postprocess_stages` and shared with the Web UI.
        from cv_tailor.pipeline_stages import run_postprocess_stages
        results = run_postprocess_stages(ctx, progress_cb=click.echo)
        translated_path = results["translated_path"]
        write_run_log_entry(ctx.run_dir, "run", "Postprocess-Stages abgeschlossen")

        # PDF rendering via the manual CV template + headless Chromium.
        # Skip with CV_TAILOR_SKIP_PDF=1 if Playwright isn't installed or
        # the template moved. Overflow (>3 pages) writes a marker file
        # instead of producing a PDF.
        import os as _os
        if _os.environ.get("CV_TAILOR_SKIP_PDF") != "1":
            click.echo("PDF wird gerendert …")
            try:
                from cv_tailor.pdf_renderer import run_pdf_renderer
                pdfs = run_pdf_renderer(ctx)
                for pdf in pdfs:
                    click.echo(f"  {pdf.name} ({pdf.stat().st_size // 1024} KB)")
                if not pdfs:
                    click.echo("  Keine PDFs erzeugt (siehe _pdf_overflow_*.md oder Log)")
            except Exception as exc:  # noqa: BLE001
                click.echo(f"  PDF-Render übersprungen: {type(exc).__name__}: {exc}", err=True)

        coach_q = ctx.run_dir / "_coach_questions.md"
        if coach_q.exists():
            click.echo("")
            click.echo("─" * 60)
            click.echo("Der Coach hat offene Fragen — bitte vor dem Versenden prüfen:")
            click.echo(f"  {coach_q}")
            click.echo("─" * 60)

        _run_eval_suite_after_run()

    except FileNotFoundError as exc:
        click.echo(f"Fehler: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Unerwarteter Fehler: {type(exc).__name__}: {exc}", err=True)
        sys.exit(2)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Host for the local web UI.")
@click.option("--port", default=8765, show_default=True, type=int, help="Port for the local web UI.")
def web(host: str, port: int) -> None:
    """Start the local web interface for uploading job postings."""
    load_dotenv()
    from cv_tailor.web import serve

    serve(host=host, port=port)


def _select_paused_run(paused: list[Path]) -> Path | None:
    """Show all paused runs, let user pick by number (or press Enter for #1)."""
    if len(paused) == 1:
        click.echo(f"Pausierter Lauf gefunden: {paused[0].name}")
        if not click.confirm("Diesen Lauf fortsetzen?", default=True):
            return None
        return paused[0]

    click.echo("Pausierte Läufe (neueste zuerst):")
    import os as _os
    from datetime import datetime as _dt, timezone as _tz
    for i, d in enumerate(paused, 1):
        mtime = _dt.fromtimestamp(_os.path.getmtime(d), tz=_tz.utc)
        state = "Klärungsfragen offen" if (d / "02_klaerungsfragen.md").exists() and not (d / "02_antworten.md").exists() else "Writer unterbrochen"
        click.echo(f"  [{i}] {d.name}  ({state}, {mtime:%Y-%m-%d %H:%M})")
    choice = click.prompt(
        "Welcher Lauf? Nummer eingeben (oder Enter für 1, q zum Abbrechen)",
        default="1",
        show_default=False,
    ).strip().lower()
    if choice in ("q", "quit", "abort"):
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(paused):
            return paused[idx]
    except ValueError:
        pass
    click.echo("Ungültige Auswahl.", err=True)
    return None


@cli.command(name="continue")
def continue_() -> None:
    """Resume a paused run (interactive picker if multiple exist)."""
    load_dotenv()
    from cv_tailor.llm import require_llm_environment
    from cv_tailor.orchestrator import find_paused_runs, resume_paused_run, RunContext
    from cv_tailor.agents.factcheck import run_factcheck

    try:
        require_llm_environment()
    except RuntimeError as exc:
        click.echo(f"Fehler: {exc}", err=True)
        sys.exit(1)

    paused_list = find_paused_runs()
    if not paused_list:
        click.echo("Kein pausierter Lauf gefunden.")
        click.echo("Tipp: Führe zuerst 'uv run cv-tailor run <stellenanzeige>' aus.")
        sys.exit(1)

    paused_dir = _select_paused_run(paused_list)
    if paused_dir is None:
        click.echo("Abgebrochen.")
        sys.exit(0)
    try:
        # Two paused states (see find_paused_run docstring):
        # - Awaiting clarifications: prompt user for answers via stdin
        # - Writer interrupted: answers already on disk, skip the prompt
        answers_path = paused_dir / "02_antworten.md"
        klaerung_path = paused_dir / "02_klaerungsfragen.md"
        if klaerung_path.exists() and not answers_path.exists():
            resume_paused_run(paused_dir)
            click.echo("Antworten gespeichert. Analyse wird fortgesetzt …")
        else:
            click.echo("Writer-Phase wurde unterbrochen — Antworten bereits vorhanden, kein Prompt.")

        run_id = paused_dir.name
        # WR-07: read the original started_at from _run.log to preserve run metadata.
        # Fall back to the current time (resume time) only if the log is missing or unparseable.
        started_at = datetime.now(timezone.utc).isoformat()
        run_log_path = paused_dir / "_run.log"
        if run_log_path.exists():
            import re as _re
            match = _re.search(r"\*\*Started:\*\*\s*(\S+)", run_log_path.read_text(encoding="utf-8"))
            if match:
                started_at = match.group(1)
        ctx = RunContext(run_id=run_id, run_dir=paused_dir, started_at=started_at)
        has_gaps = run_factcheck(ctx)

        if has_gaps:
            click.echo("Faktencheck findet noch offene Fragen — 02_klaerungsfragen.md aktualisiert.")
            click.echo("Bitte ergänze 02_antworten.md und starte danach erneut: uv run cv-tailor continue")
            sys.exit(0)
        else:
            click.echo("Analyse fortgesetzt — keine weiteren Klärungsfragen.")

        click.echo("Writer-Schleife läuft (3 Abschnitte parallel, max 2 Runden je) …")
        from cv_tailor.agents.writer_loop import run_writer_loop
        final_path = run_writer_loop(ctx, progress_cb=click.echo)
        click.echo(f"  04_final_de.md geschrieben ({final_path.stat().st_size} Bytes)")

        # Shared post-writer stages (siehe pipeline_stages.run_postprocess_stages).
        from cv_tailor.pipeline_stages import run_postprocess_stages
        results = run_postprocess_stages(ctx, progress_cb=click.echo)
        translated_path = results["translated_path"]

        # PDF rendering via the manual CV template + headless Chromium.
        # Skip with CV_TAILOR_SKIP_PDF=1 if Playwright isn't installed or
        # the template moved. Overflow (>3 pages) writes a marker file
        # instead of producing a PDF.
        import os as _os
        if _os.environ.get("CV_TAILOR_SKIP_PDF") != "1":
            click.echo("PDF wird gerendert …")
            try:
                from cv_tailor.pdf_renderer import run_pdf_renderer
                pdfs = run_pdf_renderer(ctx)
                for pdf in pdfs:
                    click.echo(f"  {pdf.name} ({pdf.stat().st_size // 1024} KB)")
                if not pdfs:
                    click.echo("  Keine PDFs erzeugt (siehe _pdf_overflow_*.md oder Log)")
            except Exception as exc:  # noqa: BLE001
                click.echo(f"  PDF-Render übersprungen: {type(exc).__name__}: {exc}", err=True)

        coach_q = ctx.run_dir / "_coach_questions.md"
        if coach_q.exists():
            click.echo("")
            click.echo("─" * 60)
            click.echo("Der Coach hat offene Fragen — bitte vor dem Versenden prüfen:")
            click.echo(f"  {coach_q}")
            click.echo("─" * 60)

        _run_eval_suite_after_run()

    except Exception as exc:  # noqa: BLE001
        click.echo(f"Fehler beim Fortsetzen: {type(exc).__name__}: {exc}", err=True)
        sys.exit(2)


@cli.command(name="post-process")
@click.argument("run_id")
def post_process(run_id: str) -> None:
    """Run diff, keyword marker, translator, friendly copy and quality snapshot on an existing 04_final_de.md.

    Use this to complete a run that was blocked mid-pipeline (e.g. after a factcheck
    blocker that was manually resolved).  The run must already have 04_final_de.md.
    """
    load_dotenv()
    from cv_tailor.llm import require_llm_environment
    from cv_tailor.orchestrator import RunContext

    try:
        require_llm_environment()
    except RuntimeError as exc:
        click.echo(f"Fehler: {exc}", err=True)
        sys.exit(1)

    run_dir = Path("runs") / run_id
    if not run_dir.exists():
        click.echo(f"Run-Verzeichnis nicht gefunden: {run_dir}", err=True)
        sys.exit(1)
    final_de = run_dir / "04_final_de.md"
    if not final_de.exists():
        click.echo(f"04_final_de.md fehlt in {run_dir} — bitte zuerst assemblen.", err=True)
        sys.exit(1)

    import re as _re
    started_at = datetime.now(timezone.utc).isoformat()
    run_log_path = run_dir / "_run.log"
    if run_log_path.exists():
        match = _re.search(r"\*\*Started:\*\*\s*(\S+)", run_log_path.read_text(encoding="utf-8"))
        if match:
            started_at = match.group(1)

    ctx = RunContext(run_id=run_id, run_dir=run_dir, started_at=started_at)

    click.echo(f"Post-Processing für {run_id} …")
    from cv_tailor.pipeline_stages import run_postprocess_stages
    results = run_postprocess_stages(ctx, progress_cb=click.echo)
    translated_path = results.get("translated_path")
    if translated_path:
        click.echo(f"  Übersetzung: {translated_path.name}")

    import os as _os
    if _os.environ.get("CV_TAILOR_SKIP_PDF") != "1":
        click.echo("PDF wird gerendert …")
        try:
            from cv_tailor.pdf_renderer import run_pdf_renderer
            pdfs = run_pdf_renderer(ctx)
            for pdf in pdfs:
                click.echo(f"  {pdf.name} ({pdf.stat().st_size // 1024} KB)")
            if not pdfs:
                click.echo("  Keine PDFs erzeugt (siehe _pdf_overflow_*.md oder Log)")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  PDF-Render übersprungen: {type(exc).__name__}: {exc}", err=True)

    click.echo("Post-Processing abgeschlossen.")
    _run_eval_suite_after_run()


@cli.group()
def clarifications() -> None:
    """Manage persisted clarification answers."""


@clarifications.command(name="status")
def clarifications_status() -> None:
    """Show how many clarification entries are stored."""
    from cv_tailor.clarifications import count_clarifications

    click.echo(f"Gespeicherte Klärungen: {count_clarifications()}")


@clarifications.command(name="import-run")
@click.argument("run_id")
def clarifications_import_run(run_id: str) -> None:
    """Import 02_klaerungsfragen.md + 02_antworten.md from a run."""
    from cv_tailor.clarifications import save_run_clarification

    run_dir = Path("runs") / run_id
    if not run_dir.exists():
        click.echo(f"Run nicht gefunden: {run_dir}", err=True)
        sys.exit(1)
    if save_run_clarification(run_dir):
        click.echo(f"Klärung importiert: {run_id}")
    else:
        click.echo(f"Keine neue Klärung importiert: {run_id}")


@clarifications.command(name="migrate-topics")
def clarifications_migrate_topics() -> None:
    """Backfill the `topics` field on pre-migration clarifications entries.

    Idempotent — entries that already carry a non-empty topics list are
    left untouched. Run once after upgrading to topic-gated clarifications.
    """
    from cv_tailor.clarifications import migrate_topics

    result = migrate_topics()
    click.echo(f"Topics-Migration: {result['updated']}/{result['total']} Einträge aktualisiert.")


@cli.command(name="quality-trend")
@click.option("--last", "last_n", type=int, default=10, help="Anzahl der letzten Runs in der Tabelle.")
@click.option("--backfill", is_flag=True, help="Snapshots für alle bisherigen Runs nachträglich erzeugen.")
def quality_trend(last_n: int, backfill: bool) -> None:
    """Zeigt Kosten- und Qualitäts-Metriken der letzten Runs als Trendtabelle.

    Spalten: cost (USD), calls (LLM-Aufrufe), v2 (Sections in Runde 2),
    fc-veto (Faktencheck-Vetos), cons (Konsistenz-Findings), cache% (Cache-Hit),
    summary-w (Summary-Wörter), diff-rows (Diff-Zeilen), fit-warn (Profil-Fit-
    Warnung). Regressionen gegen den trailing-Median werden separat geflagt.
    """
    from cv_tailor.quality_snapshot import (
        capture_run_snapshot, load_snapshots, format_trend_table, detect_regressions,
    )

    if backfill:
        runs_root = Path("runs")
        if not runs_root.exists():
            click.echo("Keine Runs gefunden.")
            return
        captured = 0
        for d in sorted(runs_root.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            if not (d / "04_final_de.md").exists():
                continue
            try:
                capture_run_snapshot(d)
                captured += 1
            except Exception as exc:  # noqa: BLE001
                click.echo(f"Skip {d.name}: {exc}", err=True)
        click.echo(f"Backfill abgeschlossen: {captured} Snapshots erzeugt/aktualisiert.\n")

    snaps = load_snapshots(limit=last_n)
    click.echo(format_trend_table(snaps))
    findings = detect_regressions()
    if findings:
        click.echo("\nRegression-Findings (latest vs. trailing median):")
        for f in findings:
            median = f.get("median") if f.get("median") not in (None, 0) else "—"
            delta = f.get("delta_pct")
            delta_str = f" ({delta:+.1f}%)" if delta is not None else ""
            click.echo(f"  • {f['metric']}: {f['latest']} vs. Median {median}{delta_str}")
    else:
        click.echo("\nKeine Qualitäts-Regression entdeckt.")


@cli.command()
@click.argument("run_id", required=False)
@click.option("--errors", is_flag=True, help="Show only errors from the last 30 days.")
def logs(run_id: str | None, errors: bool) -> None:
    """Show structured logs for a run, or recent errors with --errors."""
    if run_id and not errors:
        run_log_path = Path("runs") / run_id / "_run.log"
        if not run_log_path.exists():
            click.echo(f"Kein Lauf-Log gefunden: {run_log_path}", err=True)
            sys.exit(1)
        click.echo(run_log_path.read_text(encoding="utf-8"))
        sys.exit(0)

    logs_root = Path("logs")
    if not logs_root.exists():
        click.echo("Keine Logs gefunden (logs/ Verzeichnis existiert nicht).")
        sys.exit(1)

    filename = "errors.jsonl" if errors else "llm_calls.jsonl"
    cutoff = datetime.now(timezone.utc) - timedelta(days=30) if errors else None

    records = []
    for month_dir in sorted(logs_root.iterdir()):
        if not month_dir.is_dir():
            continue
        jsonl_path = month_dir / filename
        if not jsonl_path.exists():
            continue
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id and record.get("run_id") != run_id:
                continue
            if cutoff:
                ts = record.get("timestamp", "")
                try:
                    record_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if record_dt < cutoff:
                        continue
                except (ValueError, AttributeError):
                    pass
            records.append(record)

    if not records:
        if errors:
            click.echo("Keine Fehler in den letzten 30 Tagen gefunden.")
        elif run_id:
            click.echo(f"Keine Log-Einträge für run_id={run_id!r} gefunden.")
        else:
            click.echo("Keine Log-Einträge gefunden.")
        sys.exit(0)

    click.echo(f"\n{'Fehler-Log' if errors else 'LLM-Calls'}: {len(records)} Einträge\n")
    click.echo("-" * 80)
    for r in records:
        if errors:
            click.echo(
                f"[{r.get('timestamp', '?')[:19]}] {r.get('agent', '?')} | "
                f"{r.get('phase', '?')} | run={r.get('run_id', '?')}\n"
                f"  Fehler: {r.get('error', '?')}"
            )
        else:
            status_marker = "OK" if r.get("status") == "success" else "ERR"
            click.echo(
                f"[{r.get('timestamp', '?')[:19]}] {status_marker} | "
                f"{r.get('agent', '?')} | {r.get('model', '?')} | "
                f"in={r.get('input_tokens', 0)} out={r.get('output_tokens', 0)} | "
                f"${r.get('cost_usd', 0.0):.4f} | {r.get('duration_ms', 0)}ms"
            )
        click.echo("-" * 80)


@cli.command()
@click.option("--judge", is_flag=True, help="Run optional LLM-as-Judge rubric checks.")
def eval(judge: bool) -> None:
    """Run the eval suite against evals/cases/."""
    import importlib.util

    evals_dir = (Path(__file__).parents[2] / "evals").resolve()
    eval_run_path = evals_dir / "run.py"
    # CR-07: use importlib to load by absolute path — avoids sys.path manipulation
    # and ensures we only ever load the known run.py from the verified evals/ directory.
    if not eval_run_path.exists():
        click.echo(f"Eval-Suite nicht gefunden: {eval_run_path}", err=True)
        sys.exit(1)
    if not eval_run_path.resolve().is_relative_to(evals_dir):
        click.echo("Eval-Pfad ist ungültig.", err=True)
        sys.exit(1)
    try:
        spec = importlib.util.spec_from_file_location("cv_tailor_eval_run", eval_run_path)
        if spec is None:  # IN-03: guard against opaque AttributeError on module_from_spec(None)
            click.echo("Eval-Suite konnte nicht geladen werden (spec ist None).", err=True)
            sys.exit(1)
        module = importlib.util.module_from_spec(spec)
        # Register in sys.modules before exec_module so @dataclass __module__ lookup works
        # on Python 3.14+ (sys.modules.get(cls.__module__) must not return None).
        sys.modules["cv_tailor_eval_run"] = module
        spec.loader.exec_module(module)
        exit_code = module.main(llm_judge=judge)
        sys.exit(exit_code or 0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Eval-Suite Fehler: {type(exc).__name__}: {exc}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--days", type=int, default=7, show_default=True, help="Läufe älter als N Tage gelten als verwaist.")
@click.option("--archive/--delete", "archive", default=True, show_default=True, help="In runs/_archived/ verschieben statt löschen.")
@click.option("--yes", is_flag=True, default=False, help="Bestätigung überspringen (für Skripte).")
def cleanup(days: int, archive: bool, yes: bool) -> None:
    """Verwaiste, unvollständige Läufe finden und archivieren oder löschen.

    Ein Lauf gilt als verwaist, wenn er KEIN 04_final_de.md hat UND seit
    mehr als N Tagen nicht modifiziert wurde. Verbleibt 04_final_de.md
    NICHT, ist die Pipeline nicht durchgelaufen — entweder Klärungsfragen
    nie beantwortet oder Server mittendrin abgebrochen.
    """
    import os as _os
    import shutil as _shutil
    import time as _time

    runs_root = Path("runs")
    if not runs_root.exists():
        click.echo("Kein runs/-Verzeichnis vorhanden.")
        return

    cutoff = _time.time() - days * 86400
    candidates: list[tuple[Path, float, str]] = []
    for d in runs_root.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if (d / "04_final_de.md").exists():
            continue  # complete
        # WR-09: directory mtime updates only on add/remove, not on file edits.
        # Use the most recent file mtime inside the run so a paused run where
        # the user just dropped 02_antworten.md is not falsely archived.
        try:
            file_mtimes = [p.stat().st_mtime for p in d.rglob("*") if p.is_file()]
        except OSError:
            file_mtimes = []
        mtime = max(file_mtimes) if file_mtimes else _os.path.getmtime(d)
        if mtime > cutoff:
            continue  # too recent
        # Classify why it's orphaned
        if not (d / "01_analyse.md").exists():
            reason = "Analyse fehlt"
        elif (d / "02_klaerungsfragen.md").exists() and not (d / "02_antworten.md").exists():
            reason = "Klärungsfragen nicht beantwortet"
        else:
            reason = "Writer unterbrochen"
        candidates.append((d, mtime, reason))

    if not candidates:
        click.echo(f"Keine verwaisten Läufe älter als {days} Tage gefunden.")
        return

    candidates.sort(key=lambda t: t[1])
    from datetime import datetime as _dt, timezone as _tz
    click.echo(f"Verwaiste Läufe (älter als {days} Tage, kein 04_final_de.md):")
    for d, mtime, reason in candidates:
        ts = _dt.fromtimestamp(mtime, tz=_tz.utc)
        click.echo(f"  - {d.name}  ({reason}, {ts:%Y-%m-%d})")

    action = "archivieren" if archive else "löschen"
    if not yes and not click.confirm(f"\n{len(candidates)} Lauf/Läufe {action}?", default=False):
        click.echo("Abgebrochen.")
        return

    archive_root = runs_root / "_archived"
    if archive:
        archive_root.mkdir(exist_ok=True)

    for d, _mtime, _reason in candidates:
        try:
            if archive:
                target = archive_root / d.name
                if target.exists():
                    target = archive_root / f"{d.name}.{int(_time.time())}"
                _shutil.move(str(d), str(target))
                click.echo(f"  → archiviert: {d.name}")
            else:
                _shutil.rmtree(d)
                click.echo(f"  → gelöscht: {d.name}")
        except OSError as exc:
            click.echo(f"  ✗ Fehler bei {d.name}: {exc}", err=True)

    click.echo(f"\nFertig. {len(candidates)} Lauf/Läufe verarbeitet.")


if __name__ == "__main__":
    cli()
