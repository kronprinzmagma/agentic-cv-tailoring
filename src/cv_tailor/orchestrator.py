"""Run directory lifecycle management and pause/continue state machine."""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cv_tailor.logging_config import get_logger
from cv_tailor.clarifications import save_run_clarification

log = get_logger(__name__)

_run_log_lock = threading.Lock()


@dataclass
class RunContext:
    run_id: str      # "YYYY-MM-DD_<slug>"
    run_dir: Path    # absolute path
    started_at: str  # ISO-8601 UTC


def _make_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:40]


def init_run(stellenanzeige_path: Path, runs_root: Path = Path("runs")) -> RunContext:
    """Create a new run directory under runs/YYYY-MM-DD_<slug>/, copy the job posting, and write the initial _run.log."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _make_slug(stellenanzeige_path.stem)
    run_id = f"{date_str}_{slug}"
    run_dir = runs_root / run_id
    suffix = 2
    while run_dir.exists():
        run_id = f"{date_str}_{slug}_{suffix}"
        run_dir = runs_root / run_id
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(stellenanzeige_path, run_dir / "00_stellenanzeige.md")

    started_at = datetime.now(timezone.utc).isoformat()
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log_content = (
        f"# Run: {run_id}\n\n"
        f"**Started:** {started_at}\n"
        f"**Status:** running\n\n"
        f"## Events\n\n"
        f"- [{ts}] orchestrator: run initialized\n"
    )
    (run_dir / "_run.log").write_text(log_content, encoding="utf-8")

    log.info("init_run.done", run_id=run_id, run_dir=str(run_dir))
    return RunContext(run_id=run_id, run_dir=run_dir, started_at=started_at)


def write_run_log_entry(run_dir: Path, phase: str, message: str) -> None:
    """Append a timestamped event line to the run's _run.log."""
    log_path = run_dir / "_run.log"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"- [{ts}] {phase}: {message}\n"
    with _run_log_lock:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)


def find_paused_runs(runs_root: Path = Path("runs")) -> list[Path]:
    """Return ALL paused run directories, newest first.

    See find_paused_run for the qualification logic.
    """
    if not runs_root.exists():
        return []
    candidates: list[Path] = []
    for d in runs_root.iterdir():
        if not d.is_dir():
            continue
        if (d / "04_final_de.md").exists():
            continue
        if not (d / "01_analyse.md").exists():
            continue
        if (d / "02_klaerungsfragen.md").exists() and not (d / "02_antworten.md").exists():
            candidates.append(d)
            continue
        if (d / "02_antworten.md").exists() or not (d / "02_klaerungsfragen.md").exists():
            candidates.append(d)
    return sorted(candidates, key=lambda d: os.path.getmtime(d), reverse=True)


def find_paused_run(runs_root: Path = Path("runs")) -> Path | None:
    """Return the most recently modified run directory that needs continuation.

    Two paused states qualify:
    1. **Awaiting clarifications**: 02_klaerungsfragen.md exists, 02_antworten.md
       does not, run has not completed (no 04_final_de.md).
    2. **Writer interrupted**: 02_antworten.md exists (or no clarifications
       were needed) AND 01_analyse.md exists AND 04_final_de.md does not exist.
       Covers crashes between writer-loop start and pipeline completion.

    The most recently touched candidate wins so a fresh interruption is
    preferred over an old orphan.
    """
    if not runs_root.exists():
        return None
    candidates: list[Path] = []
    for d in runs_root.iterdir():
        if not d.is_dir():
            continue
        if (d / "04_final_de.md").exists():
            continue  # completed
        if not (d / "01_analyse.md").exists():
            continue  # not even analysed yet — nothing to resume
        # State 1: clarifications open
        if (d / "02_klaerungsfragen.md").exists() and not (d / "02_antworten.md").exists():
            candidates.append(d)
            continue
        # State 2: writer interrupted (answers in place or no clarifications, but no final)
        if (d / "02_antworten.md").exists() or not (d / "02_klaerungsfragen.md").exists():
            candidates.append(d)
    if not candidates:
        return None
    return max(candidates, key=lambda d: os.path.getmtime(d))


def resume_paused_run(run_dir: Path) -> Path:
    """Prompt user to answer Klärungsfragen via stdin and write 02_antworten.md."""
    klaerung_path = run_dir / "02_klaerungsfragen.md"
    antworten_path = run_dir / "02_antworten.md"

    print(f"Paused run found: {run_dir.name}\n")
    print("Klärungsfragen:")
    print("---------------")
    print(klaerung_path.read_text(encoding="utf-8"))
    print("\nBitte beantworte die obigen Fragen (leere Zeile = Eingabe abschliessen):")

    collected_lines: list[str] = []
    consecutive_empty = 0
    for line in sys.stdin:
        line = line.rstrip("\n")
        if line == "":
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
        else:
            consecutive_empty = 0
        collected_lines.append(line)

    antworten_text = "\n".join(collected_lines).strip()
    answered_at = datetime.now(timezone.utc).isoformat()
    content = (
        f"# Antworten auf Klärungsfragen\n\n"
        f"**Run:** {run_dir.name}\n"
        f"**Beantwortet:** {answered_at}\n\n"
        f"{antworten_text}\n"
    )
    antworten_path.write_text(content, encoding="utf-8")
    write_run_log_entry(run_dir, "continue", "Antworten auf Klärungsfragen erhalten")
    if save_run_clarification(run_dir):
        write_run_log_entry(run_dir, "continue", "Klärungsantworten in data/clarifications.json gespeichert")
    log.info("resume_paused_run.done", run_dir=str(run_dir))
    return antworten_path
