"""Beleg-Index: deterministic extraction of claims from Standard-CV and Zeugnisse.

Hybrid architecture (per CLAUDE.md):
  - Rule-based parsers (this module) extract claims with exact source positions.
  - LLM enrichment (Plan 02-03) adds classification metadata only.
  - The verbatim `snippet` field is NEVER rewritten by an LLM.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from cv_tailor.llm import call_llm
from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

SNIPPET_MAX = 500
KONTEXT_MAX = 200  # per side


@dataclass(frozen=True)
class RawClaim:
    """Deterministically extracted raw claim — no LLM involved.

    Fields are populated purely by rule-based extraction. LLM enrichment
    happens later (Plan 02-03) and adds classification metadata only, never
    rewrites snippet.
    """

    snippet: str           # Verbatim text from source, max 500 chars
    quelle_datei: str      # Relative path, e.g. "data/standard_cv.md"
    quelle_position: str   # "line:42" for Markdown, "page:3" for PDF
    quelle_typ: str        # "standard_cv" | "zeugnis"
    kontext: str           # Surrounding text, max 200 chars before + 200 after
    section: str | None    # Nearest heading, e.g. "Berufserfahrung" or None


# --- Markdown parser ---

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*```")
# Sentence split: period/!/? followed by space + capital letter (incl. German Umlauts).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _build_kontext(lines: list[str], idx: int) -> str:
    before = " ".join(lines[max(0, idx - 3) : idx]).strip()
    after = " ".join(lines[idx + 1 : idx + 4]).strip()
    before_t = _truncate(before, KONTEXT_MAX) if before else ""
    after_t = _truncate(after, KONTEXT_MAX) if after else ""
    if before_t and after_t:
        return f"{before_t} … {after_t}"
    return before_t or after_t


def parse_standard_cv(path: Path) -> list[RawClaim]:
    """Parse a Markdown CV into deterministic RawClaim records.

    Args:
        path: Path to Standard-CV Markdown file.

    Returns:
        List of RawClaim, one per bullet/numbered item or per sentence in
        paragraph text under a heading. Order preserves source order.

    Raises:
        FileNotFoundError: If `path` does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Standard-CV not found at {path}. Expected location: data/standard_cv.md"
        )

    # Normalize: store as posix relative if possible
    try:
        rel_path = str(path.relative_to(Path.cwd())) if path.is_absolute() else str(path)
    except ValueError:
        rel_path = str(path)

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    claims: list[RawClaim] = []
    current_section: str | None = None
    in_code_fence = False

    for idx, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        if _CODE_FENCE_RE.match(line):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not line.strip():
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            current_section = heading.group(2).strip()
            continue

        # Bullet or numbered list item
        for regex in (_BULLET_RE, _NUMBERED_RE):
            m = regex.match(line)
            if m:
                snippet = _truncate(m.group(1).strip(), SNIPPET_MAX)
                claims.append(
                    RawClaim(
                        snippet=snippet,
                        quelle_datei=rel_path,
                        quelle_position=f"line:{idx + 1}",
                        quelle_typ="standard_cv",
                        kontext=_build_kontext(lines, idx),
                        section=current_section,
                    )
                )
                break
        else:
            # Paragraph: split into sentences. Skip pure decorative lines.
            stripped = line.strip()
            if len(stripped) < 4:  # Drop "---", "***", etc.
                continue
            sentences = _SENTENCE_SPLIT_RE.split(stripped)
            for sent in sentences:
                sent = sent.strip()
                if len(sent) < 4:
                    continue
                claims.append(
                    RawClaim(
                        snippet=_truncate(sent, SNIPPET_MAX),
                        quelle_datei=rel_path,
                        quelle_position=f"line:{idx + 1}",
                        quelle_typ="standard_cv",
                        kontext=_build_kontext(lines, idx),
                        section=current_section,
                    )
                )

    log.info("parse_standard_cv.done", path=rel_path, claim_count=len(claims))
    return claims


# ---------------------------------------------------------------------------
# LLM enrichment (Plan 02-03)
# ---------------------------------------------------------------------------

BEHAUPTUNG_MAX = 300
VALID_TYPS = {"skill", "experience", "education", "achievement", "responsibility", "other"}

_CLASSIFY_SYSTEM_PROMPT = """You classify CV claims for a fact-check index.
You receive a verbatim snippet from a CV or work certificate (Zeugnis).
Respond with ONLY a JSON object, no prose, no markdown fences:

{
  "typ": "skill" | "experience" | "education" | "achievement" | "responsibility" | "other",
  "normalized_behauptung": "<concise normalized assertion, max 300 chars, same language as snippet>",
  "normalized_differs_meaningfully": true | false
}

Rules:
- DO NOT paraphrase, embellish, or invent.
- normalized_behauptung must convey the same factual claim as the snippet.
- If the snippet is already a clear assertion, set normalized_differs_meaningfully=false and copy the snippet verbatim into normalized_behauptung.
- If you cannot classify confidently, set typ="other".
"""


@dataclass(frozen=True)
class BelegEntry:
    id: str
    behauptung: str
    quelle_datei: str
    quelle_position: str
    snippet: str
    kontext: str
    typ: str
    quelle_typ: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "behauptung": self.behauptung,
            "quelle_datei": self.quelle_datei,
            "quelle_position": self.quelle_position,
            "snippet": self.snippet,
            "kontext": self.kontext,
            "typ": self.typ,
            "quelle_typ": self.quelle_typ,
        }


def _load_factcheck_config(config_path: Path = Path("config.yaml")) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {config_path}")
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    agents = (cfg or {}).get("agents", {})
    fc = agents.get("factcheck")
    if not fc or "provider" not in fc or "model" not in fc:
        raise ValueError("config.yaml missing agents.factcheck.{provider,model}")
    return fc


def _classify_one(claim: RawClaim) -> tuple[str, str]:
    """Call LLM to classify claim. Returns (typ, behauptung). On any failure: ("other", claim.snippet)."""
    user_msg = (
        f"Snippet: {claim.snippet}\n"
        f"Source: {claim.quelle_datei} ({claim.quelle_position})\n"
        f"Section: {claim.section or '(none)'}\n"
    )
    try:
        content = call_llm(
            agent="factcheck",
            phase="phase2_bootstrap",
            run_id="bootstrap",
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=400,
            iteration=0,
            snippet_text=claim.snippet,
        )
        # WR-01: robust fence stripping (handles ```json\n...\n``` and language tag)
        content = content.strip()
        content = re.sub(r"^```(?:json)?\s*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
        data = json.loads(content)
        typ = data.get("typ", "other")
        if typ not in VALID_TYPS:
            typ = "other"
        normalized = (data.get("normalized_behauptung") or "").strip()
        differs = bool(data.get("normalized_differs_meaningfully", False))
        if not differs or not normalized:
            behauptung = claim.snippet
        else:
            behauptung = normalized[:BEHAUPTUNG_MAX]
        return typ, behauptung
    except Exception as exc:
        err_str = f"{type(exc).__name__}: {exc}"
        log.warning("enrich_claims.fallback", error=err_str)
        return "other", claim.snippet


def enrich_claims(claims: Iterable[RawClaim], fc_config: dict) -> list[BelegEntry]:
    entries: list[BelegEntry] = []
    for idx, claim in enumerate(claims, start=1):
        typ, behauptung = _classify_one(claim)
        entries.append(BelegEntry(
            id=f"BELG-{idx:03d}",
            behauptung=behauptung,
            quelle_datei=claim.quelle_datei,
            quelle_position=claim.quelle_position,
            snippet=claim.snippet,  # VERBATIM — never overwritten
            kontext=claim.kontext,
            typ=typ,
            quelle_typ=claim.quelle_typ,
        ))
    return entries


def build_beleg_index(
    cv_path: Path,
    zeugnis_dir: Path,
    config_path: Path = Path("config.yaml"),
) -> dict:
    from cv_tailor.zeugnis_parser import parse_zeugnisse  # local import avoids cycle
    fc_config = _load_factcheck_config(config_path)
    cv_claims = parse_standard_cv(cv_path)
    z_claims = parse_zeugnisse(zeugnis_dir) if zeugnis_dir.exists() else []
    all_claims = cv_claims + z_claims
    log.info(
        "build_beleg_index.claims_extracted",
        cv_claims=len(cv_claims),
        zeugnis_claims=len(z_claims),
        total=len(all_claims),
    )
    entries = enrich_claims(all_claims, fc_config)
    zeugnis_files = (
        sorted(str(p) for p in zeugnis_dir.glob("*.pdf")) +
        sorted(str(p) for p in zeugnis_dir.glob("*.md"))
    ) if zeugnis_dir.exists() else []
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_files": {
            "standard_cv": str(cv_path),
            "zeugnisse": zeugnis_files,
        },
        "entries": [e.to_dict() for e in entries],
    }


def write_beleg_index(index: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("write_beleg_index.done", path=str(out_path), entry_count=len(index.get("entries", [])))


def format_beleg_index_compact(index: dict) -> str:
    """Return a compact one-line-per-entry summary for LLM context.

    Keeps all entries visible while staying well under typical context limits.
    Format: BELG-NNN [typ] snippet (quelle_typ, position)

    Snippet limit raised to 250 chars (was 120). 120 truncated important
    entries mid-sentence — e.g. BELG-023 was cut at "Führung v..." losing
    the "über 10 Personen / 20% unter Budget" numbers, which then caused
    factcheck false-positives on otherwise verbatim claims from the
    Standard-CV. Only ~84 of 366 entries exceed 120 chars, so the cost
    increase is modest (~17%).
    """
    lines = []
    for e in index.get("entries", []):
        snippet = e["snippet"][:250]
        lines.append(f"{e['id']} [{e['typ']}] {snippet} ({e['quelle_typ']}, {e['quelle_position']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cached loaders — beleg_index.json is read up to 12× per run (writer ×6,
# factcheck ×6). Parse once and reuse.
# ---------------------------------------------------------------------------

_index_data_cache: dict[str, dict] = {}   # resolved path → parsed JSON dict
_compact_cache: dict[str, str] = {}       # resolved path → compact string


def load_beleg_index(path: Path) -> dict:
    """Load and cache the beleg_index JSON data by resolved path."""
    key = str(path.resolve())
    if key not in _index_data_cache:
        if not path.exists():
            _index_data_cache[key] = {"entries": []}
        else:
            _index_data_cache[key] = json.loads(path.read_text(encoding="utf-8"))
    return _index_data_cache[key]


def get_beleg_index_compact(path: Path) -> str:
    """Return the compact LLM-context string for a beleg_index file, cached."""
    key = str(path.resolve())
    if key not in _compact_cache:
        data = load_beleg_index(path)
        if not data.get("entries"):
            _compact_cache[key] = (
                "(Beleg-Index nicht gefunden — bitte zuerst cv-tailor bootstrap ausführen)"
            )
        else:
            _compact_cache[key] = format_beleg_index_compact(data)
    return _compact_cache[key]


def format_samples_for_display(index: dict, n: int = 10, seed: int = 42) -> str:
    entries = index.get("entries", [])
    if not entries:
        return "(keine Einträge im Beleg-Index)"
    sample_size = min(n, len(entries))
    rng = random.Random(seed)
    samples = rng.sample(entries, sample_size)
    lines = [f"=== {sample_size} Stichproben aus Beleg-Index ({len(entries)} Einträge total) ==="]
    for i, e in enumerate(samples, start=1):
        lines.append(
            f"\n[{i}] {e['id']}  ({e['typ']}, {e['quelle_typ']})\n"
            f"    Quelle:     {e['quelle_datei']} ({e['quelle_position']})\n"
            f"    Behauptung: {e['behauptung']}\n"
            f"    Snippet:    {e['snippet']}\n"
            f"    Kontext:    {e['kontext'] or '(kein Kontext)'}"
        )
    return "\n".join(lines)
