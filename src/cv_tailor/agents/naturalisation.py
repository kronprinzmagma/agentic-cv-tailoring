"""Naturalisation agent: scans the final CV markdown for AI-tells / wordiness
and proposes minimal sentence-level edits.

Read-only step: the agent produces JSON suggestions, never modifies the CV.
A separate `apply_suggestions()` function takes user-approved suggestions
(filtered via Web-UI checkboxes) and rewrites the canonical MD with a
timestamped backup.

Triggered by the Web-UI button "🪶 Naturalisation prüfen". Optional, opt-in.
Cost: ~$0.02 per call (Claude Haiku).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from cv_tailor.cv_filename import CV_AUTHOR_TOKEN
from cv_tailor.llm import call_llm, load_prompt
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

NATURALISATION_PROMPT_PATH = Path("prompts/naturalisation.md")
# 16 Vorschläge × ~250 Tokens (original + vorschlag + begründung) ≈ 4000 Tokens.
# Vorher 2048 — hat die Antwort regelmässig mitten im JSON abgeschnitten,
# Parser fiel auf `[]` zurück, UI zeigte fälschlich "alles bestens".
# Headroom auf 6144, damit auch lange Original-Phrasen + 16 Vorschläge nicht
# truncate-gefährdet sind. Cost-Impact marginal (~$0.003 zusätzlich pro Call).
MAX_TOKENS = 6144


def _extract_json(text: str) -> dict:
    """Parse a JSON object from the model response, tolerating fences."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Naturalisation response had no JSON object")
    return json.loads(s[start : end + 1])


def run_naturalisation(
    ctx: RunContext,
    md_path: Path | None = None,
    prompt_path: Path = NATURALISATION_PROMPT_PATH,
) -> dict:
    """Run the naturalisation agent against the current final CV markdown.

    Returns a dict `{"suggestions": [...], "lang": "de"|"en", "source": "..."}`.
    Suggestions are deterministically validated server-side: any whose
    `original` does not occur verbatim in the source MD is dropped before
    returning to the UI.
    """
    # Decide which markdown to scan: prefer EN if it exists (English posting),
    # else DE. Within each language, prefer the freshest friendly copy.
    if md_path is None:
        en_friendly = sorted(
            (p for p in ctx.run_dir.glob(f"{CV_AUTHOR_TOKEN}-*_EN.md") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        de_friendly = sorted(
            (p for p in ctx.run_dir.glob(f"{CV_AUTHOR_TOKEN}-*.md")
             if p.is_file() and not p.stem.endswith("_EN")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        canonical_en = ctx.run_dir / "04_final_en.md"
        canonical_de = ctx.run_dir / "04_final_de.md"
        candidates: list[tuple[Path, str]] = []
        if en_friendly:
            candidates.append((en_friendly[0], "en"))
        elif canonical_en.exists():
            candidates.append((canonical_en, "en"))
        if de_friendly:
            candidates.append((de_friendly[0], "de"))
        elif canonical_de.exists():
            candidates.append((canonical_de, "de"))
        if not candidates:
            raise FileNotFoundError("Kein finales CV-Markdown im Run-Verzeichnis gefunden")
        # Pick the most recently modified across both languages
        md_path, lang = max(candidates, key=lambda c: c[0].stat().st_mtime)
    else:
        lang = "en" if "_EN" in md_path.stem or "04_final_en" in md_path.name else "de"

    md_text = md_path.read_text(encoding="utf-8")
    system_prompt = load_prompt(prompt_path)
    user_msg = f"## Sprache\n{lang}\n\n## CV-Markdown\n{md_text}"

    log.info("naturalisation.start", run_id=ctx.run_id, lang=lang, source=md_path.name)
    content = call_llm(
        agent="naturalisation",
        phase="phase7_naturalisation",
        run_id=ctx.run_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=MAX_TOKENS,
        snippet_text=md_text[:500],
    )

    try:
        data = _extract_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("naturalisation.parse_failed", error=str(exc))
        write_run_log_entry(
            ctx.run_dir, "naturalisation",
            f"Vorschlags-Parser fehlgeschlagen: {exc}",
        )
        return {"suggestions": [], "lang": lang, "source": md_path.name, "error": str(exc)}

    raw_suggestions = data.get("suggestions", []) or []
    # Validate: each suggestion's original must occur verbatim in md_text
    validated: list[dict] = []
    dropped: list[dict] = []
    seen_ids: set[str] = set()
    for s in raw_suggestions:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        original = str(s.get("original") or "")
        vorschlag = str(s.get("vorschlag") or "")
        kategorie = str(s.get("kategorie") or "")
        location = str(s.get("location") or "")
        begruendung = str(s.get("begründung") or s.get("begruendung") or "")
        if not sid or sid in seen_ids:
            dropped.append({"id": sid, "reason": "duplicate or missing id"})
            continue
        if len(original) < 15:
            dropped.append({"id": sid, "reason": "original too short"})
            continue
        if len(original) > 300:
            dropped.append({"id": sid, "reason": "original too long"})
            continue
        if not vorschlag or vorschlag.strip() == original.strip():
            dropped.append({"id": sid, "reason": "empty or no-op suggestion"})
            continue
        if original not in md_text:
            dropped.append({"id": sid, "reason": "original not found verbatim"})
            continue
        # Suggestion may not exceed original by more than 20%
        if len(vorschlag) > int(len(original) * 1.2):
            dropped.append({"id": sid, "reason": "vorschlag exceeds 120% of original"})
            continue
        seen_ids.add(sid)
        validated.append({
            "id": sid,
            "kategorie": kategorie,
            "location": location,
            "original": original,
            "vorschlag": vorschlag,
            "begründung": begruendung,
        })

    write_run_log_entry(
        ctx.run_dir, "naturalisation",
        f"{len(validated)} valide Vorschläge, {len(dropped)} verworfen ({md_path.name})",
    )
    log.info(
        "naturalisation.done", run_id=ctx.run_id,
        valid=len(validated), dropped=len(dropped),
    )
    return {
        "suggestions": validated,
        "lang": lang,
        "source": md_path.name,
        "dropped": dropped,
    }


def apply_suggestions(
    ctx: RunContext,
    accepted: list[dict],
    source_filename: str,
) -> dict:
    """Apply user-approved suggestions to the source MD.

    Each accepted suggestion must include `id`, `original`, `vorschlag`.
    Exact substring replacement only — if `original` is not found verbatim,
    the suggestion is skipped (logged, not applied). Before any write,
    the source MD is backed up to `<filename>.bak.<unix_ts>`.

    Returns `{"applied": [ids], "skipped": [{"id": ..., "reason": ...}],
    "backup": "<filename>", "target": "<filename>"}`.
    """
    if not source_filename:
        raise ValueError("source_filename required")
    md_path = ctx.run_dir / source_filename
    if not md_path.exists():
        raise FileNotFoundError(f"Quelle nicht gefunden: {source_filename}")

    md_text = md_path.read_text(encoding="utf-8")
    ts = int(time.time())
    backup_path = md_path.with_suffix(md_path.suffix + f".bak.{ts}")
    backup_path.write_text(md_text, encoding="utf-8")

    applied: list[str] = []
    skipped: list[dict] = []
    new_text = md_text
    for s in accepted:
        sid = str(s.get("id") or "").strip()
        original = str(s.get("original") or "")
        vorschlag = str(s.get("vorschlag") or "")
        if not original or not vorschlag:
            skipped.append({"id": sid, "reason": "missing original or vorschlag"})
            continue
        if original not in new_text:
            skipped.append({"id": sid, "reason": "original no longer found (overlapping edit?)"})
            continue
        new_text = new_text.replace(original, vorschlag, 1)
        applied.append(sid)

    # Atomic write
    import os as _os
    tmp = md_path.with_suffix(md_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    _os.replace(tmp, md_path)

    # If the source was a friendly copy, also sync back into canonical
    # so downstream tools (diff, PDF render, eval) stay consistent.
    if source_filename.startswith(f"{CV_AUTHOR_TOKEN}-"):
        lang = "en" if md_path.stem.endswith("_EN") else "de"
        canonical = ctx.run_dir / f"04_final_{lang}.md"
        if canonical.exists():
            tmp2 = canonical.with_suffix(canonical.suffix + ".tmp")
            tmp2.write_text(new_text, encoding="utf-8")
            _os.replace(tmp2, canonical)

    write_run_log_entry(
        ctx.run_dir, "naturalisation",
        f"Vorschläge angewendet: {len(applied)} OK, {len(skipped)} verworfen. Backup: {backup_path.name}",
    )
    log.info(
        "naturalisation.applied", run_id=ctx.run_id,
        applied=len(applied), skipped=len(skipped), backup=backup_path.name,
    )
    return {
        "applied": applied,
        "skipped": skipped,
        "backup": backup_path.name,
        "target": source_filename,
    }
