"""Local web interface for cv-tailor.

The UI is intentionally small and dependency-free: a local-only HTTP server
wraps the existing orchestrator and agent pipeline, then exposes run artifacts
from the ignored runs/ directory.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import traceback
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any
from urllib.parse import unquote, urlparse

from cv_tailor.cost_tracking import compute_run_cost, format_compact as format_cost_compact
from cv_tailor.cv_filename import CV_AUTHOR_TOKEN
from cv_tailor.orchestrator import RunContext, init_run, write_run_log_entry
from cv_tailor.quality_snapshot import detect_regressions

UPLOAD_ROOT = Path("stellenanzeigen/uploads")
RUNS_ROOT = Path("runs").resolve()
ALLOWED_RUN_FILES = {
    "00_stellenanzeige.md",
    "_experience_activation.md",
    "01_analyse.md",
    "02_klaerungsfragen.md",
    "02_antworten.md",
    "04_final_de.md",
    "04_final_en.md",
    "05_diff.md",
    "_factcheck_blocker_management_summary.md",
    "_factcheck_blocker_schluesselkompetenzen.md",
    "_factcheck_blocker_berufserfahrung.md",
    "_coach_questions.md",
    "_profile_fit.md",
    "_pdf_overflow_de.md",
    "_pdf_overflow_en.md",
    "04_final_de.pdf",
    "04_final_en.pdf",
    "_run.log",
}


def _write_profile_fit_md(run_dir: Path, gaps: list) -> None:
    """Persist gap list so the user can read it in the UI tab."""
    lines = ["# Profil-Fit-Hinweise", ""]
    crit = [g for g in gaps if g.severity == "critical"]
    soft = [g for g in gaps if g.severity == "soft"]
    if crit:
        lines.append(f"## Kritisch ({len(crit)}) — nicht belegte Muss-Anforderungen")
        lines.append("")
        for g in crit:
            lines.append(f"- **{g.requirement}**")
            if g.comment:
                lines.append(f"  {g.comment}")
        lines.append("")
    if soft:
        lines.append(f"## Beachten ({len(soft)})")
        lines.append("")
        for g in soft:
            lines.append(f"- **{g.requirement}** *[{g.status}]*")
            if g.comment:
                lines.append(f"  {g.comment}")
        lines.append("")
    (run_dir / "_profile_fit.md").write_text("\n".join(lines), encoding="utf-8")


def _profile_fit_needs_decision(run_dir: Path) -> bool:
    """Return True when the on-disk fit report still carries critical gaps."""
    fit_path = run_dir / "_profile_fit.md"
    if not fit_path.exists():
        return False
    try:
        return "## Kritisch" in fit_path.read_text(encoding="utf-8")
    except OSError:
        # A failed read must not turn a known fit report into an implicit pass.
        return True


@dataclass
class WebJob:
    """In-memory status for one local UI run."""

    job_id: str
    status: str = "queued"
    stage: str = "Wartet"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    upload_path: str | None = None
    run_id: str | None = None
    run_dir: str | None = None
    error: str | None = None
    phases: list = field(default_factory=lambda: [
        {"id": "analyse",            "label": "Analyse",              "status": "pending", "detail": ""},
        {"id": "faktencheck",        "label": "Faktencheck",          "status": "pending", "detail": ""},
        {"id": "klaerung",           "label": "Klärungsfragen",       "status": "skipped", "detail": ""},
        {"id": "writer_summary",     "label": "Management Summary",   "status": "pending", "detail": ""},
        {"id": "writer_kompetenzen", "label": "Schlüsselkompetenzen", "status": "pending", "detail": ""},
        {"id": "writer_erfahrung",   "label": "Berufserfahrung",      "status": "pending", "detail": ""},
        {"id": "diff",               "label": "Diff",                 "status": "pending", "detail": ""},
        {"id": "uebersetzung",       "label": "Übersetzung",          "status": "skipped", "detail": ""},
    ])


class WebState:
    """Thread-safe job registry for the local server process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, WebJob] = {}
        self._recover_existing_runs()

    def _recover_existing_runs(self) -> None:
        """Scan `runs/` and re-register paused or interrupted runs.

        Server restarts previously lost all in-memory job state — paused
        runs vanished from the UI even though their files sat on disk.
        Now: each run with `01_analyse.md` but no `04_final_de.md` gets
        a WebJob entry with one of two states:

        - **paused**: 02_klaerungsfragen.md exists, 02_antworten.md does
          not. User submits answers via the existing UI form.
        - **fit_warning**: a critical `_profile_fit.md` report exists and
          the user must explicitly confirm the fit decision again.
        - **resumable**: 02_antworten.md exists (or no clarifications
          were ever requested). User clicks Resume to restart the writer.
        """
        runs_root = Path("runs")
        if not runs_root.exists():
            return
        for d in sorted(runs_root.iterdir(), key=lambda p: p.name):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            if (d / "04_final_de.md").exists():
                continue  # completed — nothing to recover
            if not (d / "01_analyse.md").exists():
                continue  # never made it past analyst — leave alone

            awaiting_clarification = (
                (d / "02_klaerungsfragen.md").exists()
                and not (d / "02_antworten.md").exists()
            )
            fit_decision_open = (
                not awaiting_clarification
                and _profile_fit_needs_decision(d)
            )
            if awaiting_clarification:
                status = "paused"
                stage = "Klärungsfragen beantworten"
            elif fit_decision_open:
                # A critical Profile-Fit decision is in-memory while the
                # server is alive. After a restart, recover it from the
                # persisted report rather than letting Resume skip the gate.
                status = "fit_warning"
                stage = "Profil-Fit prüfen - kritische Lücken erkannt"
            else:
                status = "resumable"
                stage = "Writer-Phase wiederaufnehmen"

            # Use the run directory name as a stable job_id so the user can
            # bookmark the URL and survives further server restarts.
            job_id = f"recovered_{d.name}"
            posting_path = d / "00_stellenanzeige.md"
            job = WebJob(
                job_id=job_id,
                status=status,
                stage=stage,
                run_id=d.name,
                run_dir=str(d),
                upload_path=str(posting_path) if posting_path.exists() else None,
            )
            # Reflect the on-disk progress in phase status.
            if (d / "01_analyse.md").exists():
                job.phases[0]["status"] = "done"  # analyse
            if (d / "02_klaerungsfragen.md").exists():
                job.phases[1]["status"] = "done"  # faktencheck
                job.phases[2]["status"] = (
                    "running" if awaiting_clarification else "done"
                )
            elif fit_decision_open:
                job.phases[1]["status"] = "pending"  # factcheck waits for decision
            self._jobs[job_id] = job

    def create_job(self, upload_path: Path) -> WebJob:
        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        job = WebJob(job_id=job_id, upload_path=str(upload_path))
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> WebJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            # Return a deep copy so readers see a consistent snapshot even while
            # the background thread mutates phases or other mutable fields.
            return copy.deepcopy(job)

    def update(self, job_id: str, **changes: Any) -> WebJob:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc).isoformat()
            return job

    def set_phase(self, job_id: str, phase_id: str, status: str, detail: str = "") -> None:
        """Update a single phase's status and optional detail text."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for phase in job.phases:
                if phase["id"] == phase_id:
                    phase["status"] = status
                    phase["detail"] = detail
                    break
            job.updated_at = datetime.now(timezone.utc).isoformat()

    def try_start_continue(self, job_id: str) -> bool:
        """Atomically transition a job from 'paused' to 'continuing'.

        Returns True if the transition succeeded (caller may spawn the thread).
        Returns False if the job was not in 'paused' state — prevents TOCTOU
        double-submit where two concurrent POSTs both see 'paused' and both
        spawn a pipeline continuation thread.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "paused":
                return False
            job.status = "continuing"
            job.updated_at = datetime.now(timezone.utc).isoformat()
            return True

    def try_start_render(self, job_id: str) -> bool:
        """Atomically claim the job for a PDF render.

        Allowed source states: `completed`, `error`, `cancelled` (re-renders
        from a settled state). Returns False if a render is already in
        progress or the job is in a non-renderable state. The transition
        target `rendering` is reset to `completed` by `_handle_render_pdf`
        when the background thread finishes, regardless of outcome.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status not in ("completed", "error", "cancelled"):
                return False
            job.status = "rendering"
            job.updated_at = datetime.now(timezone.utc).isoformat()
            return True

    def try_start_fit_continue(self, job_id: str) -> bool:
        """Atomically transition from 'fit_warning' to 'continuing'."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "fit_warning":
                return False
            job.status = "continuing"
            job.updated_at = datetime.now(timezone.utc).isoformat()
            return True

    def try_start_resume(self, job_id: str) -> bool:
        """Atomically transition a recovered/resumable job to 'continuing'."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "resumable":
                return False
            job.status = "continuing"
            job.updated_at = datetime.now(timezone.utc).isoformat()
            return True


STATE = WebState()


def _safe_filename(filename: str) -> str:
    stem = Path(filename or "stellenanzeige.md").stem
    suffix = Path(filename or "stellenanzeige.md").suffix or ".md"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-").strip("_") or "stellenanzeige"
    safe_suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]+", suffix) else ".md"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{safe_stem[:80]}{safe_suffix}"


def _job_to_dict(job: WebJob) -> dict[str, Any]:
    run_dir = Path(job.run_dir) if job.run_dir else None
    available_files = []
    if run_dir and run_dir.exists():
        available_files = sorted(
            file_name for file_name in ALLOWED_RUN_FILES if (run_dir / file_name).exists()
        )
        # When an EN version exists the job posting was English — surface only the EN CV.
        # The DE file remains on disk as an intermediate but is not shown in the UI.
        if "04_final_en.md" in available_files:
            available_files = [f for f in available_files if f != "04_final_de.md"]
        if "04_final_en.pdf" in available_files:
            available_files = [f for f in available_files if f != "04_final_de.pdf"]
    questions = ""
    if run_dir and (run_dir / "02_klaerungsfragen.md").exists():
        questions = (run_dir / "02_klaerungsfragen.md").read_text(encoding="utf-8")
    run_log = ""
    if run_dir and (run_dir / "_run.log").exists():
        run_log = (run_dir / "_run.log").read_text(encoding="utf-8")[-8000:]
    # Cost summary aggregated from logs/YYYY-MM/llm_calls.jsonl. Cheap O(N)
    # scan that finishes in <1ms for typical run sizes. Computed on every
    # poll so the user sees the cost grow as the run progresses.
    cost_summary: dict[str, Any] = {
        "total_cost_usd": 0.0, "calls": 0, "compact": "—",
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cache_read_input_tokens": 0, "by_agent": [],
    }
    if job.run_id:
        try:
            cs = compute_run_cost(job.run_id)
            cs["compact"] = format_cost_compact(cs)
            cost_summary = cs
        except Exception:  # noqa: BLE001
            # Cost tracking must never break the status endpoint
            pass
    return {
        "job_id": job.job_id,
        "status": job.status,
        "stage": job.stage,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "run_id": job.run_id,
        "run_dir": job.run_dir,
        "error": job.error,
        "available_files": available_files,
        "questions": questions,
        "run_log": run_log,
        "phases": job.phases,
        "cost_summary": cost_summary,
        # Quality regression hints (only populated when the run has finished
        # and a quality snapshot is on file). Empty list = no regression.
        "quality_regressions": _quality_regressions_for(job),
    }


def _quality_regressions_for(job: WebJob) -> list[dict[str, Any]]:
    """Run regression detection only for completed runs to avoid noise mid-run."""
    if job.status != "completed":
        return []
    try:
        return detect_regressions()
    except Exception:  # noqa: BLE001
        return []


def _build_context(job: WebJob) -> RunContext:
    if not job.run_id or not job.run_dir:
        raise ValueError("Run wurde noch nicht initialisiert.")
    return RunContext(
        run_id=job.run_id,
        run_dir=Path(job.run_dir),
        started_at=job.created_at,
    )


_WRITER_PHASE_MAP = {
    "Management Summary":   "writer_summary",
    "Schlüsselkompetenzen": "writer_kompetenzen",
    "Berufserfahrung":      "writer_erfahrung",
}


def _mark_running_phase_failed(job_id: str) -> None:
    """Set the first phase with status 'running' to 'error'."""
    job = STATE.get(job_id)
    if job:
        for phase in job.phases:
            if phase["status"] == "running":
                STATE.set_phase(job_id, phase["id"], "error")
                break


def _finish_pipeline(job_id: str, ctx: RunContext) -> None:
    """Run Writer + post-processing for the Web UI flow.

    Delegates the post-writer stages (Diff → Keyword-Marker → Translator
    → Friendly-Copy → Quality-Snapshot) to `pipeline_stages.run_postprocess_stages`,
    which is shared with the CLI. Only the UI-specific phase-tracker
    bookkeeping lives here.
    """
    import re as _re

    from cv_tailor.agents.writer_loop import run_writer_loop
    from cv_tailor.pipeline_stages import (
        capture_snapshot_safely,
        run_diff_stage,
        run_keyword_marker_stage,
        run_translator_stage,
        write_friendly_copies,
    )

    def _web_progress(msg: str) -> None:
        """Update stage, run log and individual writer-phase status."""
        clean = msg.strip()
        label = clean.lstrip("✓↻ ").strip()
        STATE.update(job_id, stage=label)
        write_run_log_entry(ctx.run_dir, "writer_loop", label)
        for section_label, phase_id in _WRITER_PHASE_MAP.items():
            if section_label in clean:
                if "akzeptiert" in clean or clean.startswith("✓") or "  ✓" in clean:
                    STATE.set_phase(job_id, phase_id, "done")
                elif "Veto" in clean or "↻" in clean:
                    m = _re.search(r"Runde (\d+)", clean)
                    detail = f"Runde {m.group(1)}" if m else ""
                    STATE.set_phase(job_id, phase_id, "running", detail)
                else:
                    STATE.set_phase(job_id, phase_id, "running")
                break

    STATE.update(job_id, stage="Writer-Schleife läuft")
    run_writer_loop(ctx, progress_cb=_web_progress)
    write_run_log_entry(ctx.run_dir, "web", "Writer-Schleife abgeschlossen")

    # Each post-stage gets called explicitly so the Web UI can advance its
    # phase-tracker (diff / uebersetzung) between them. CLI uses the
    # bundled `run_postprocess_stages` instead.
    STATE.update(job_id, stage="Diff wird erstellt")
    STATE.set_phase(job_id, "diff", "running")
    run_diff_stage(ctx)
    STATE.set_phase(job_id, "diff", "done")

    STATE.update(job_id, stage="Keywords werden markiert")
    run_keyword_marker_stage(ctx)

    STATE.update(job_id, stage="Übersetzung wird geprüft")
    STATE.set_phase(job_id, "uebersetzung", "running")
    translated_path = run_translator_stage(ctx)
    STATE.set_phase(job_id, "uebersetzung", "done")

    write_friendly_copies(ctx, translated_path)

    # PDF is now an explicit user action via the "PDF erstellen" button,
    # not part of the auto-pipeline. User edits the MD locally first, then
    # triggers POST /api/runs/<id>/pdf when ready.

    STATE.update(job_id, status="completed", stage="MD bereit — bei Bedarf bearbeiten, dann PDF erstellen")
    write_run_log_entry(ctx.run_dir, "web", "Pipeline über Web-UI abgeschlossen (MD)")

    capture_snapshot_safely(ctx)


def _run_initial_pipeline(job_id: str) -> None:
    from cv_tailor.agents.analyst import run_analyst
    from cv_tailor.agents.factcheck import run_factcheck

    job = STATE.get(job_id)
    if job is None or job.upload_path is None:
        return

    try:
        STATE.update(job_id, status="running", stage="Run wird initialisiert")
        ctx = init_run(Path(job.upload_path))
        STATE.update(job_id, run_id=ctx.run_id, run_dir=str(ctx.run_dir))
        write_run_log_entry(ctx.run_dir, "web", f"Upload erhalten: {job.upload_path}")

        STATE.update(job_id, stage="Analyse läuft")
        STATE.set_phase(job_id, "analyse", "running")
        run_analyst(ctx)
        STATE.set_phase(job_id, "analyse", "done")
        write_run_log_entry(ctx.run_dir, "web", "Analyse abgeschlossen")

        # Profile-Fit-Gate: if the analyst found critical LÜCKE rows, pause
        # so the user can decide before paying for the writer-loop.
        from cv_tailor.profile_fit import check_profile_fit, has_critical_gaps
        fit_gaps = check_profile_fit(ctx.run_dir / "01_analyse.md")
        if fit_gaps:
            _write_profile_fit_md(ctx.run_dir, fit_gaps)
            if has_critical_gaps(fit_gaps):
                STATE.update(
                    job_id,
                    status="fit_warning",
                    stage="Profil-Fit prüfen — kritische Lücken erkannt",
                )
                write_run_log_entry(ctx.run_dir, "web", f"Profile-Fit-Gate: {len(fit_gaps)} Hinweis(e), pausiert")
                return

        STATE.update(job_id, stage="Faktencheck läuft")
        STATE.set_phase(job_id, "faktencheck", "running")
        has_gaps = run_factcheck(ctx)
        STATE.set_phase(job_id, "faktencheck", "done")
        write_run_log_entry(ctx.run_dir, "web", "Faktencheck abgeschlossen")
        if has_gaps:
            STATE.set_phase(job_id, "klaerung", "running")
            STATE.update(job_id, status="paused", stage="Klärungsfragen beantworten")
            write_run_log_entry(ctx.run_dir, "web", "Wartet auf Antworten in der Web-UI")
            return

        _finish_pipeline(job_id, ctx)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        _mark_running_phase_failed(job_id)
        STATE.update(job_id, status="error", stage="Fehler", error=error)
        job_after_error = STATE.get(job_id)
        if job_after_error and job_after_error.run_dir:
            run_dir = Path(job_after_error.run_dir)
            write_run_log_entry(run_dir, "web_error", error)
            (run_dir / "_web_error.txt").write_text(traceback.format_exc(), encoding="utf-8")


def _continue_after_fit(job_id: str) -> None:
    """User confirmed fit-warning — pick up from Faktencheck onward."""
    from cv_tailor.agents.factcheck import run_factcheck

    job = STATE.get(job_id)
    if job is None or not job.run_dir:
        return
    try:
        ctx = _build_context(job)
        STATE.update(job_id, status="running", stage="Faktencheck läuft", error=None)
        STATE.set_phase(job_id, "faktencheck", "running")
        has_gaps = run_factcheck(ctx)
        STATE.set_phase(job_id, "faktencheck", "done")
        if has_gaps:
            STATE.set_phase(job_id, "klaerung", "running")
            STATE.update(job_id, status="paused", stage="Klärungsfragen beantworten")
            return
        _finish_pipeline(job_id, ctx)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        _mark_running_phase_failed(job_id)
        STATE.update(job_id, status="error", stage="Fehler", error=error)


def _resume_pipeline(job_id: str) -> None:
    """Resume a writer-interrupted run. Answers (if any) are already on disk."""
    from cv_tailor.agents.factcheck import run_factcheck

    job = STATE.get(job_id)
    if job is None or not job.run_dir:
        return
    try:
        ctx = _build_context(job)
        STATE.update(job_id, status="running", stage="Wiederaufnahme — Faktencheck", error=None)
        # Always re-run the initial factcheck before writer-loop recovery.
        # A recovered run without 02_antworten.md may have crashed after the
        # Analyst and before Factcheck completed; skipping here would bypass
        # the gate solely because the Web server restarted.
        STATE.set_phase(job_id, "faktencheck", "running")
        has_gaps = run_factcheck(ctx)
        STATE.set_phase(job_id, "faktencheck", "done")
        if has_gaps:
            STATE.set_phase(job_id, "klaerung", "running")
            STATE.update(job_id, status="paused", stage="Weitere Klärungsfragen beantworten")
            write_run_log_entry(ctx.run_dir, "web", "Resume: Faktencheck fordert weitere Klärung")
            return
        if (ctx.run_dir / "02_antworten.md").exists():
            STATE.set_phase(job_id, "klaerung", "done")
        _finish_pipeline(job_id, ctx)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        _mark_running_phase_failed(job_id)
        STATE.update(job_id, status="error", stage="Fehler", error=error)


def _continue_pipeline(job_id: str, answers: str) -> None:
    from cv_tailor.agents.factcheck import run_factcheck

    job = STATE.get(job_id)
    if job is None:
        return

    try:
        ctx = _build_context(job)
        STATE.update(job_id, status="running", stage="Antworten werden geprüft", error=None)
        answered_at = datetime.now(timezone.utc).isoformat()
        content = (
            "# Antworten auf Klärungsfragen\n\n"
            f"**Run:** {ctx.run_id}\n"
            f"**Beantwortet:** {answered_at}\n\n"
            f"{answers.strip()}\n"
        )
        (ctx.run_dir / "02_antworten.md").write_text(content, encoding="utf-8")
        write_run_log_entry(ctx.run_dir, "web", "Antworten auf Klärungsfragen erhalten")
        from cv_tailor.clarifications import save_run_clarification

        if save_run_clarification(ctx.run_dir):
            write_run_log_entry(ctx.run_dir, "web", "Klärungsantworten in data/clarifications.json gespeichert")

        STATE.set_phase(job_id, "faktencheck", "running")
        has_gaps = run_factcheck(ctx)
        STATE.set_phase(job_id, "faktencheck", "done")
        if has_gaps:
            STATE.set_phase(job_id, "klaerung", "running")
            STATE.update(job_id, status="paused", stage="Weitere Klärungsfragen beantworten")
            write_run_log_entry(ctx.run_dir, "web", "Faktencheck fordert weitere Klärung")
            return

        STATE.set_phase(job_id, "klaerung", "done")
        _finish_pipeline(job_id, ctx)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        _mark_running_phase_failed(job_id)
        STATE.update(job_id, status="error", stage="Fehler", error=error)
        job_after_error = STATE.get(job_id)
        if job_after_error and job_after_error.run_dir:
            run_dir = Path(job_after_error.run_dir)
            write_run_log_entry(run_dir, "web_error", error)
            (run_dir / "_web_error.txt").write_text(traceback.format_exc(), encoding="utf-8")


class CvTailorHandler(BaseHTTPRequestHandler):
    """HTTP handler for the dependency-free local UI."""

    server_version = "cv-tailor-web/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(_render_index())
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        if path.startswith("/api/runs/"):
            self._handle_run_get(path)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Nicht gefunden")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/runs":
            self._handle_create_run()
            return
        if path.startswith("/api/runs/") and path.endswith("/answers"):
            self._handle_answers(path)
            return
        if path.startswith("/api/runs/") and path.endswith("/resume"):
            self._handle_resume(path)
            return
        if path.startswith("/api/runs/") and path.endswith("/fit-continue"):
            self._handle_fit_continue(path)
            return
        if path.startswith("/api/runs/") and path.endswith("/fit-cancel"):
            self._handle_fit_cancel(path)
            return
        if path.startswith("/api/runs/") and path.endswith("/pdf"):
            self._handle_render_pdf(path)
            return
        if path.startswith("/api/runs/") and path.endswith("/open-md"):
            self._handle_open_md(path)
            return
        if path.startswith("/api/runs/") and path.endswith("/naturalise"):
            self._handle_naturalise(path)
            return
        if path.startswith("/api/runs/") and path.endswith("/naturalise/apply"):
            self._handle_naturalise_apply(path)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Nicht gefunden")

    def _handle_create_run(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        filename = str(payload.get("filename") or "stellenanzeige.md")
        content = str(payload.get("content") or "").strip()
        if not content:
            self._send_error(HTTPStatus.BAD_REQUEST, "Die Stellenanzeige ist leer.")
            return

        from cv_tailor.llm import validate_llm_environment

        problems = validate_llm_environment()
        if problems:
            self._send_json(
                {
                    "error": (
                        "LLM-Konfiguration unvollständig. Bitte .env aus .env.example "
                        "erstellen und echte API-Keys setzen."
                    ),
                    "problems": problems,
                },
                status=HTTPStatus.PRECONDITION_FAILED,
            )
            return

        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        upload_path = UPLOAD_ROOT / _safe_filename(filename)
        upload_path.write_text(content + "\n", encoding="utf-8")

        job = STATE.create_job(upload_path)
        threading.Thread(target=_run_initial_pipeline, args=(job.job_id,), daemon=True).start()
        self._send_json(_job_to_dict(job), status=HTTPStatus.ACCEPTED)

    def _handle_answers(self, path: str) -> None:
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if len(parts) != 4:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        if job.status != "paused":
            self._send_error(HTTPStatus.CONFLICT, "Dieser Run wartet aktuell nicht auf Antworten.")
            return
        payload = self._read_json()
        if payload is None:
            return
        answers = str(payload.get("answers") or "").strip()
        if not answers:
            self._send_error(HTTPStatus.BAD_REQUEST, "Antworten sind leer.")
            return
        # WR-01: atomic compare-and-swap from "paused" → "continuing" to prevent
        # TOCTOU double-submit: two concurrent POSTs both passed the status check
        # above on their copy; only the first one wins the CAS and spawns a thread.
        if not STATE.try_start_continue(job_id):
            self._send_error(HTTPStatus.CONFLICT, "Dieser Run wartet aktuell nicht auf Antworten.")
            return
        threading.Thread(target=_continue_pipeline, args=(job_id, answers), daemon=True).start()
        self._send_json(_job_to_dict(job), status=HTTPStatus.ACCEPTED)

    def _handle_fit_continue(self, path: str) -> None:
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if len(parts) != 4:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None or job.status != "fit_warning":
            self._send_error(HTTPStatus.CONFLICT, "Dieser Run wartet nicht auf eine Fit-Entscheidung.")
            return
        if not STATE.try_start_fit_continue(job_id):
            self._send_error(HTTPStatus.CONFLICT, "Dieser Run wartet nicht auf eine Fit-Entscheidung.")
            return
        if job.run_dir:
            write_run_log_entry(Path(job.run_dir), "web", "User bestätigt trotz Profile-Fit-Hinweisen")
        threading.Thread(target=_continue_after_fit, args=(job_id,), daemon=True).start()
        self._send_json(_job_to_dict(job), status=HTTPStatus.ACCEPTED)

    def _handle_render_pdf(self, path: str) -> None:
        """Trigger PDF generation from the (possibly user-edited) MD source.

        Concurrency: claims the job via `try_start_render` CAS before
        spawning the daemon thread. A second POST while a render is active
        gets 409 instead of starting a parallel render that would race on
        the canonical-MD sync-back.
        """
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) != 4:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None or not job.run_dir:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        run_dir = Path(job.run_dir)
        # Need at least one final MD on disk
        if not (run_dir / "04_final_de.md").exists() and not (run_dir / "04_final_en.md").exists():
            self._send_error(HTTPStatus.CONFLICT, "Noch kein finales CV-Markdown vorhanden.")
            return
        # Atomic claim — rejects double-clicks and concurrent tabs
        if not STATE.try_start_render(job_id):
            self._send_error(HTTPStatus.CONFLICT, "Ein PDF-Render läuft bereits für diesen Run.")
            return
        STATE.update(job_id, stage="PDF wird gerendert")

        def _do_render() -> None:
            try:
                ctx = _build_context(job)
            except ValueError as exc:
                STATE.update(
                    job_id, status="error", stage=f"PDF-Render: {exc}"
                )
                return
            try:
                from cv_tailor.pdf_renderer import run_pdf_renderer
                pdfs = run_pdf_renderer(ctx)
                write_run_log_entry(
                    run_dir, "pdf", f"PDF-Render manuell ausgelöst, {len(pdfs)} PDF(s) erzeugt"
                )
                STATE.update(
                    job_id,
                    status="completed",
                    stage=f"PDF bereit ({len(pdfs)})" if pdfs else "PDF-Render fehlgeschlagen",
                )
            except Exception as exc:  # noqa: BLE001
                write_run_log_entry(
                    run_dir, "web_error", f"PDF-Render fehlgeschlagen: {type(exc).__name__}: {exc}"
                )
                STATE.update(
                    job_id, status="completed", stage=f"PDF-Render fehlgeschlagen: {exc}"
                )

        threading.Thread(target=_do_render, daemon=True).start()
        self._send_json(_job_to_dict(job), status=HTTPStatus.ACCEPTED)

    def _handle_open_md(self, path: str) -> None:
        """Open the final-CV MD locally in the user's default editor (macOS `open`).

        Selection: optional `?lang=de|en` query param picks the language;
        otherwise the freshest mtime wins. Resolves the target path and
        verifies it stays under RUNS_ROOT — defence-in-depth against
        symlinks in `runs/`.
        """
        parsed = urlparse(self.path)
        lang_param = urllib.parse.parse_qs(parsed.query).get("lang", [""])[0].lower()
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) != 4:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None or not job.run_dir:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        run_dir = Path(job.run_dir)

        # Collect candidates with language tag
        de_friendly = [p for p in sorted(run_dir.glob(f"{CV_AUTHOR_TOKEN}-*.md"))
                       if p.is_file() and not p.stem.endswith("_EN")]
        en_friendly = [p for p in sorted(run_dir.glob(f"{CV_AUTHOR_TOKEN}-*_EN.md")) if p.is_file()]
        canonical_de = run_dir / "04_final_de.md"
        canonical_en = run_dir / "04_final_en.md"

        def pick_for(lang: str) -> Path | None:
            if lang == "en":
                pool = en_friendly + ([canonical_en] if canonical_en.exists() else [])
            else:
                pool = de_friendly + ([canonical_de] if canonical_de.exists() else [])
            pool = [p for p in pool if p.exists()]
            if not pool:
                return None
            # Friendly takes precedence if any, else newest mtime
            friendly = [p for p in pool if p.name.startswith(f"{CV_AUTHOR_TOKEN}-")]
            return (max(friendly, key=lambda p: p.stat().st_mtime)
                    if friendly else max(pool, key=lambda p: p.stat().st_mtime))

        if lang_param in ("de", "en"):
            target = pick_for(lang_param)
        else:
            # Default: prefer whatever was edited most recently
            cands = de_friendly + en_friendly + [p for p in (canonical_de, canonical_en) if p.exists()]
            target = max(cands, key=lambda p: p.stat().st_mtime) if cands else None

        if target is None:
            self._send_error(HTTPStatus.NOT_FOUND, "Kein CV-Markdown gefunden")
            return

        # Path-traversal guard: resolved target must stay under RUNS_ROOT
        try:
            resolved = target.resolve()
        except OSError:
            self._send_error(HTTPStatus.FORBIDDEN, "Pfad konnte nicht aufgelöst werden")
            return
        if not str(resolved).startswith(str(RUNS_ROOT) + os.sep):
            self._send_error(HTTPStatus.FORBIDDEN, "Datei nicht freigegeben")
            return

        try:
            import subprocess
            subprocess.run(["open", str(resolved)], check=False)
            write_run_log_entry(run_dir, "web", f"MD lokal geöffnet: {target.name}")
            self._send_json({"opened": target.name})
        except Exception as exc:  # noqa: BLE001
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Öffnen fehlgeschlagen: {exc}")

    def _handle_naturalise(self, path: str) -> None:
        """Run the naturalisation agent and return suggestions JSON (read-only)."""
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) != 4:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None or not job.run_dir:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        try:
            ctx = _build_context(job)
        except ValueError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return
        try:
            from cv_tailor.agents.naturalisation import run_naturalisation
            result = run_naturalisation(ctx)
        except FileNotFoundError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("naturalise_handler.failed")
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Agent fehlgeschlagen: {exc}")
            return
        self._send_json(result)

    def _handle_naturalise_apply(self, path: str) -> None:
        """Apply user-approved naturalisation suggestions to the source MD."""
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) != 5:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None or not job.run_dir:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        payload = self._read_json()
        if payload is None:
            return
        accepted = payload.get("accepted") or []
        source = str(payload.get("source") or "").strip()
        if not isinstance(accepted, list) or not source:
            self._send_error(HTTPStatus.BAD_REQUEST, "accepted (Liste) und source (Dateiname) erforderlich")
            return
        # Security: source must be a plain filename inside run_dir, not a path
        if "/" in source or "\\" in source or source.startswith("."):
            self._send_error(HTTPStatus.FORBIDDEN, "Ungültiger Quelldateiname")
            return
        try:
            ctx = _build_context(job)
        except ValueError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
            return
        try:
            from cv_tailor.agents.naturalisation import apply_suggestions
            result = apply_suggestions(ctx, accepted, source)
        except (FileNotFoundError, ValueError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("naturalise_apply_handler.failed")
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Apply fehlgeschlagen: {exc}")
            return
        self._send_json(result)

    def _handle_fit_cancel(self, path: str) -> None:
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if len(parts) != 4:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None or job.status != "fit_warning":
            self._send_error(HTTPStatus.CONFLICT, "Dieser Run wartet nicht auf eine Fit-Entscheidung.")
            return
        STATE.update(job_id, status="cancelled", stage="Abgebrochen (Profile-Fit)")
        if job.run_dir:
            write_run_log_entry(Path(job.run_dir), "web", "User hat Run wegen Profile-Fit-Hinweisen abgebrochen")
        self._send_json(_job_to_dict(job))

    def _handle_resume(self, path: str) -> None:
        """Restart the writer pipeline for a recovered, writer-interrupted run."""
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if len(parts) != 4:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        job_id = parts[2]
        job = STATE.get(job_id)
        if job is None:
            self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
            return
        if job.status != "resumable":
            self._send_error(HTTPStatus.CONFLICT, "Dieser Run ist nicht wiederaufnehmbar.")
            return
        if not STATE.try_start_resume(job_id):
            self._send_error(HTTPStatus.CONFLICT, "Dieser Run ist nicht wiederaufnehmbar.")
            return
        threading.Thread(target=_resume_pipeline, args=(job_id,), daemon=True).start()
        self._send_json(_job_to_dict(job), status=HTTPStatus.ACCEPTED)

    def _handle_run_get(self, path: str) -> None:
        parts = [unquote(part) for part in path.strip("/").split("/")]
        # GET /api/runs → list all known jobs (recovered + active), newest first
        if len(parts) == 2:
            with STATE._lock:
                snapshot = list(STATE._jobs.values())
            snapshot.sort(key=lambda j: j.updated_at, reverse=True)
            self._send_json({"runs": [_job_to_dict(copy.deepcopy(j)) for j in snapshot]})
            return
        if len(parts) == 3:
            job = STATE.get(parts[2])
            if job is None:
                self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
                return
            self._send_json(_job_to_dict(job))
            return

        if len(parts) == 5 and parts[3] == "files":
            parsed_url = urlparse(self.path)
            force_download = urllib.parse.parse_qs(parsed_url.query).get("download", ["0"])[0] == "1"
            job = STATE.get(parts[2])
            file_name = parts[4]
            if job is None or not job.run_dir:
                self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")
                return
            if file_name not in ALLOWED_RUN_FILES:
                self._send_error(HTTPStatus.FORBIDDEN, "Datei nicht freigegeben")
                return
            file_path = (Path(job.run_dir) / file_name).resolve()
            # CR-02: verify the resolved path stays within the runs directory
            if not str(file_path).startswith(str(RUNS_ROOT) + os.sep):
                self._send_error(HTTPStatus.FORBIDDEN, "Datei nicht freigegeben")
                return
            if not file_path.exists():
                self._send_error(HTTPStatus.NOT_FOUND, "Datei nicht vorhanden")
                return
            if file_name.endswith(".pdf"):
                # Recruiter-friendly attachment name for the final-CV PDFs.
                attachment_name = file_name
                if force_download and file_name in ("04_final_de.pdf", "04_final_en.pdf"):
                    posting_path = Path(job.run_dir) / "00_stellenanzeige.md"
                    if posting_path.exists():
                        from cv_tailor.cv_filename import friendly_cv_filename
                        lang = "en" if file_name == "04_final_en.pdf" else "de"
                        md_name = friendly_cv_filename(
                            posting_path.read_text(encoding="utf-8"),
                            language=lang,
                            fallback_slug=Path(job.run_dir).name,
                        )
                        if md_name.endswith(".md"):
                            attachment_name = md_name[:-3] + ".pdf"
                self._send_binary(
                    file_path.read_bytes(), file_name=attachment_name, content_type="application/pdf"
                )
                return
            # WR-02: no .html branch — agent-generated HTML must never be served as
            # text/html without a Content-Security-Policy; all current ALLOWED_RUN_FILES
            # are Markdown or plain-text. If PDF/HTML outputs are re-introduced, add
            # explicit CSP headers before serving them.

            # Recruiter-friendly attachment filename for the final CV downloads.
            # Internal storage stays `04_final_de.md` / `04_final_en.md`; only the
            # download attachment name is rewritten using cv_filename module.
            attachment_name = file_name
            if force_download and file_name in ("04_final_de.md", "04_final_en.md"):
                posting_path = Path(job.run_dir) / "00_stellenanzeige.md"
                if posting_path.exists():
                    from cv_tailor.cv_filename import friendly_cv_filename
                    lang = "en" if file_name == "04_final_en.md" else "de"
                    attachment_name = friendly_cv_filename(
                        posting_path.read_text(encoding="utf-8"),
                        language=lang,
                        fallback_slug=Path(job.run_dir).name,
                    )
            self._send_text(
                file_path.read_text(encoding="utf-8"),
                file_name=attachment_name,
                download=force_download,
            )
            return

        self._send_error(HTTPStatus.NOT_FOUND, "Run nicht gefunden")

    MAX_BODY = 2 * 1024 * 1024  # 2 MB — CR-06

    def _read_json(self) -> dict[str, Any] | None:
        # CR-01/CR-02: reject non-numeric or negative Content-Length before min() call.
        # int("-1") would make min(-1, MAX_BODY) == -1, causing rfile.read(-1) to block
        # indefinitely. "abc" would raise ValueError without this guard.
        try:
            cl = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Ungültiger Content-Length-Header")
            return None
        if cl < 0:
            self._send_error(HTTPStatus.BAD_REQUEST, "Ungültiger Content-Length-Header")
            return None
        length = min(cl, self.MAX_BODY)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # WR-05: return 400 instead of silently returning {}
            self._send_error(HTTPStatus.BAD_REQUEST, "Ungültiges JSON im Request-Body")
            return None
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, content: str, *, file_name: str, download: bool = False) -> None:
        body = content.encode("utf-8")
        safe_name = urllib.parse.quote(file_name, safe="")
        disposition = "attachment" if download else "inline"
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{safe_name}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_binary(self, content: bytes, *, file_name: str, content_type: str) -> None:
        safe_name = urllib.parse.quote(file_name, safe="")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{safe_name}")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """HTTP server variant that avoids reverse DNS lookup during bind."""

    allow_reuse_address = True  # avoids "Address already in use" on quick restart

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def _render_index() -> str:
    """Return the index HTML. Template lives in `web_assets/index.html`,
    served as-is. Kept as a function (not a constant) so future template
    engines (Jinja2) can be slotted in without changing call sites."""
    template_path = Path(__file__).parent / "web_assets" / "index.html"
    return template_path.read_text(encoding="utf-8")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the local web interface and block until interrupted."""
    server = LocalThreadingHTTPServer((host, port), CvTailorHandler)
    url = f"http://{host}:{server.server_address[1]}"
    print(f"cv-tailor Web-UI läuft lokal: {url}", flush=True)
    print("Abbrechen mit Ctrl+C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
