"""Append PORTFOLIO.md claims to the Beleg-Index without renumbering it.

Why a separate module instead of extending `bootstrap`: a full rebuild
renumbers every BELG-ID, and those IDs are referenced from
`data/clarifications.json`, from prompt examples and from review notes. A
rebuild would leave all of them pointing at the wrong evidence — silently.

So the sync is additive and idempotent:
  * existing entries are never touched,
  * portfolio entries already in the index are dropped and re-added, so an
    updated portfolio replaces its own claims rather than duplicating them,
  * new IDs continue after the current maximum.

The portfolio is a living document maintained outside this repo (path in
`config.yaml` under `sources.portfolio`). It is the only source that tracks
current work; the Standard-CV carries a hand-copied excerpt that drifts.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cv_tailor.beleg_index import (
    BelegEntry,
    RawClaim,
    _classify_one,
    _load_factcheck_config,
    load_beleg_index,
    load_portfolio_path,
    parse_portfolio,
    write_beleg_index,
)
from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

PORTFOLIO_TYP = "portfolio"
_ID_RE = re.compile(r"^BELG-(\d+)$")
# Classification is one small Haiku call per claim and the portfolio carries
# a few hundred. Sequential runs take minutes; four threads keep it under one.
_CLASSIFY_WORKERS = 4


def _max_id(entries: list[dict]) -> int:
    best = 0
    for entry in entries:
        match = _ID_RE.match(str(entry.get("id", "")))
        if match:
            best = max(best, int(match.group(1)))
    return best


def _classify_all(claims: list[RawClaim]) -> list[tuple[str, str]]:
    """Classify claims in parallel, preserving input order."""
    if not claims:
        return []
    with ThreadPoolExecutor(max_workers=_CLASSIFY_WORKERS) as pool:
        return list(pool.map(_classify_one, claims))


def sync_portfolio(
    index_path: Path = Path("data/beleg_index.json"),
    portfolio_path: Path | None = None,
    config_path: Path = Path("config.yaml"),
    dry_run: bool = False,
) -> dict:
    """Replace all portfolio entries in the index with the current portfolio.

    Returns a summary dict: `{"parsed", "removed", "added", "total",
    "first_id", "portfolio_path", "written"}`.

    Raises FileNotFoundError when no portfolio is configured or the configured
    file is missing — a silent no-op would look like a working sync.
    """
    portfolio_path = portfolio_path or load_portfolio_path(config_path)
    if portfolio_path is None:
        raise FileNotFoundError(
            "Kein Portfolio konfiguriert — erwarte `sources.portfolio` in config.yaml."
        )
    if not portfolio_path.exists():
        raise FileNotFoundError(f"Portfolio nicht gefunden: {portfolio_path}")

    index = load_beleg_index(index_path)
    entries: list[dict] = index.get("entries", [])

    kept = [e for e in entries if e.get("quelle_typ") != PORTFOLIO_TYP]
    removed = len(entries) - len(kept)

    claims = parse_portfolio(portfolio_path)
    log.info(
        "portfolio_sync.parsed",
        claims=len(claims),
        removed=removed,
        path=str(portfolio_path),
    )
    if dry_run:
        return {
            "parsed": len(claims),
            "removed": removed,
            "added": 0,
            "total": len(kept) + len(claims),
            "first_id": _max_id(kept) + 1,
            "portfolio_path": str(portfolio_path),
            "written": False,
        }

    # Validates that agents.factcheck is configured — _classify_one relies on it.
    _load_factcheck_config(config_path)
    start_id = _max_id(kept) + 1
    classified = _classify_all(claims)
    new_entries = [
        BelegEntry(
            id=f"BELG-{start_id + i:03d}",
            behauptung=behauptung,
            quelle_datei=claim.quelle_datei,
            quelle_position=claim.quelle_position,
            snippet=claim.snippet,  # VERBATIM — never overwritten
            kontext=claim.kontext,
            typ=typ,
            quelle_typ=claim.quelle_typ,
            section=claim.section,
        ).to_dict()
        for i, (claim, (typ, behauptung)) in enumerate(zip(claims, classified, strict=True))
    ]

    index["entries"] = kept + new_entries
    index.setdefault("source_files", {})["portfolio"] = str(portfolio_path)
    write_beleg_index(index, index_path)

    log.info(
        "portfolio_sync.done",
        added=len(new_entries),
        removed=removed,
        total=len(index["entries"]),
        first_id=f"BELG-{start_id:03d}",
    )
    return {
        "parsed": len(claims),
        "removed": removed,
        "added": len(new_entries),
        "total": len(index["entries"]),
        "first_id": f"BELG-{start_id:03d}",
        "portfolio_path": str(portfolio_path),
        "written": True,
    }
