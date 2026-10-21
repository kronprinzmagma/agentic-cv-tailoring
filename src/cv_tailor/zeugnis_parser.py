"""Zeugnis parser for the Beleg-Index.

Supports both PDF (via pdfplumber) and Markdown files.
Deterministic extraction: no OCR tweaks, no LLM — pure rule-based per BOOT-03.
"""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from cv_tailor.beleg_index import KONTEXT_MAX, SNIPPET_MAX, RawClaim
from cv_tailor.logging_config import get_logger

log = get_logger(__name__)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*```")
_MIN_SENTENCE_LEN = 4


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _normalize_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd())) if path.is_absolute() else str(path)
    except ValueError:
        return str(path)


def _build_kontext_from_sentences(sentences: list[str], idx: int) -> str:
    before = " ".join(sentences[max(0, idx - 2) : idx]).strip()
    after = " ".join(sentences[idx + 1 : idx + 3]).strip()
    before_t = _truncate(before, KONTEXT_MAX) if before else ""
    after_t = _truncate(after, KONTEXT_MAX) if after else ""
    if before_t and after_t:
        return f"{before_t} … {after_t}"
    return before_t or after_t


def _build_kontext_from_lines(lines: list[str], idx: int) -> str:
    before = " ".join(lines[max(0, idx - 3) : idx]).strip()
    after = " ".join(lines[idx + 1 : idx + 4]).strip()
    before_t = _truncate(before, KONTEXT_MAX) if before else ""
    after_t = _truncate(after, KONTEXT_MAX) if after else ""
    if before_t and after_t:
        return f"{before_t} … {after_t}"
    return before_t or after_t


def _parse_single_pdf(pdf_path: Path) -> list[RawClaim]:
    rel_path = _normalize_path(pdf_path)
    claims: list[RawClaim] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception as exc:
                    log.warning(
                        "parse_zeugnisse.page_extract_failed",
                        file=rel_path,
                        page=page_idx,
                        error=str(exc),
                    )
                    continue
                text = text.strip()
                if len(text) < _MIN_SENTENCE_LEN:
                    continue
                normalized = re.sub(r"\s+", " ", text)
                sentences = _SENTENCE_SPLIT_RE.split(normalized)
                sentences = [s.strip() for s in sentences if len(s.strip()) >= _MIN_SENTENCE_LEN]
                for s_idx, sent in enumerate(sentences):
                    claims.append(
                        RawClaim(
                            snippet=_truncate(sent, SNIPPET_MAX),
                            quelle_datei=rel_path,
                            quelle_position=f"page:{page_idx}",
                            quelle_typ="zeugnis",
                            kontext=_build_kontext_from_sentences(sentences, s_idx),
                            section=None,
                        )
                    )
    except Exception as exc:
        log.warning("parse_zeugnisse.file_skipped", file=rel_path, error=str(exc))
        return []
    return claims


def _parse_single_md(md_path: Path) -> list[RawClaim]:
    """Parse a Markdown Zeugnis file — same logic as parse_standard_cv."""
    rel_path = _normalize_path(md_path)
    text = md_path.read_text(encoding="utf-8")
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

        # Skip blockquotes (intro/meta lines starting with >)
        if line.lstrip().startswith(">"):
            continue

        for regex in (_BULLET_RE, _NUMBERED_RE):
            m = regex.match(line)
            if m:
                snippet = m.group(1).strip()
                # Skip TOC links like "[HealthApp AG](#healthapp-ag...)"
                if snippet.startswith("[") and "](#" in snippet:
                    break
                snippet = _truncate(snippet, SNIPPET_MAX)
                claims.append(
                    RawClaim(
                        snippet=snippet,
                        quelle_datei=rel_path,
                        quelle_position=f"line:{idx + 1}",
                        quelle_typ="zeugnis",
                        kontext=_build_kontext_from_lines(lines, idx),
                        section=current_section,
                    )
                )
                break
        else:
            stripped = line.strip()
            if len(stripped) < _MIN_SENTENCE_LEN:
                continue
            # Skip pure decorative lines and TOC-style links
            if stripped.startswith("[") and "](#" in stripped:
                continue
            sentences = _SENTENCE_SPLIT_RE.split(stripped)
            for sent in sentences:
                sent = sent.strip()
                if len(sent) < _MIN_SENTENCE_LEN:
                    continue
                claims.append(
                    RawClaim(
                        snippet=_truncate(sent, SNIPPET_MAX),
                        quelle_datei=rel_path,
                        quelle_position=f"line:{idx + 1}",
                        quelle_typ="zeugnis",
                        kontext=_build_kontext_from_lines(lines, idx),
                        section=current_section,
                    )
                )

    return claims


def parse_zeugnisse(directory: Path) -> list[RawClaim]:
    """Parse all PDFs and Markdown files in `directory` into RawClaim records.

    Args:
        directory: Path to the Zeugnisse directory (typically data/zeugnisse/).

    Returns:
        Flat list of RawClaim across all files, sorted by filename then position.

    Raises:
        FileNotFoundError: If `directory` does not exist.
    """
    if not directory.exists():
        raise FileNotFoundError(
            f"Zeugnisse directory not found at {directory}. "
            f"Expected location: data/zeugnisse/"
        )

    pdfs = sorted(directory.glob("*.pdf"))
    mds = sorted(directory.glob("*.md"))

    if not pdfs and not mds:
        log.info("parse_zeugnisse.no_files_found", directory=_normalize_path(directory))
        return []

    all_claims: list[RawClaim] = []

    for pdf_path in pdfs:
        file_claims = _parse_single_pdf(pdf_path)
        all_claims.extend(file_claims)
        log.info("parse_zeugnisse.file_done", file=_normalize_path(pdf_path), claim_count=len(file_claims))

    for md_path in mds:
        file_claims = _parse_single_md(md_path)
        all_claims.extend(file_claims)
        log.info("parse_zeugnisse.file_done", file=_normalize_path(md_path), claim_count=len(file_claims))

    log.info(
        "parse_zeugnisse.done",
        directory=_normalize_path(directory),
        pdf_count=len(pdfs),
        md_count=len(mds),
        total_claims=len(all_claims),
    )
    return all_claims
