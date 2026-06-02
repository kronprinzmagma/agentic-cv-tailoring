"""Build a recruiter-friendly filename for the final CV.

Pattern: `CV_<Author>-<anchor> <Company> <Position>.md`
- Author token is built from `output.author_name` in config.yaml
  (spaces → underscores, prefix "CV_"). Override via `CV_AUTHOR_NAME` env.
- `<anchor>` is `output.cv_version_anchor` in config.yaml (a stable version
  tag for the underlying standard CV).
- Company and position are extracted from the job posting `00_stellenanzeige.md`.

The friendly file is written alongside `04_final_de.md` / `04_final_en.md`,
which remain the canonical internal artifacts (referenced by diff, keyword
marker, web UI, eval cases).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"

_DEFAULT_AUTHOR_NAME = "Author"
_DEFAULT_VERSION_ANCHOR = "000000"

# Characters that are problematic on macOS/Linux filesystems or in CLI usage
_BAD_FS_CHARS = re.compile(r'[\\/:"*?<>|\n\r\t]+')

# Legal entity suffixes to strip from company names
_LEGAL_SUFFIXES = re.compile(
    r"\s*\b(AG|GmbH|SA|SE|Ltd\.?|Inc\.?|Corp\.?|Holding|Group|Gruppe)\b\.?\s*$",
    flags=re.IGNORECASE,
)

# Gender designation variants: (w/m/d), (m/w/d), (m/f/d), (f/m/d), (w m d), (gn), (all genders), etc.
_GENDER_DESIGNATIONS = re.compile(
    r"\s*\([wmfd]\s*[/\s][wmfd]\s*(?:[/\s][wmfd])?\)"
    r"|\s*\(gn\)"
    r"|\s*\(all\s+genders\)"
    r"|\s*\(d\s*/\s*[mf]\s*/\s*[wmf]\)",
    flags=re.IGNORECASE,
)

# Percentage patterns: "100%", "80%-100%", "80% – 100%"
_PERCENTAGES = re.compile(r"\s*\d+\s*%(?:\s*[-–]\s*\d+\s*%)?")

# Pipe-separated secondary info: " | something extra"
_PIPE_SUFFIX = re.compile(r"\s*\|.*$")


def _load_output_config(config_path: Path = _CONFIG_PATH) -> tuple[str, str]:
    """Return (author_name, version_anchor) from config.yaml or fallbacks.

    Priority:
      1. CV_AUTHOR_NAME env var (overrides author_name only)
      2. config.yaml output.author_name + output.cv_version_anchor
      3. Hardcoded fallbacks (_DEFAULT_AUTHOR_NAME, _DEFAULT_VERSION_ANCHOR)

    Never raises — missing config falls back to defaults so module import
    never blocks on a misconfigured repo.
    """
    author_name = _DEFAULT_AUTHOR_NAME
    version_anchor = _DEFAULT_VERSION_ANCHOR
    try:
        if config_path.exists():
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            output = cfg.get("output") or {}
            author_name = str(output.get("author_name") or author_name)
            version_anchor = str(output.get("cv_version_anchor") or version_anchor)
    except Exception:
        # Defensive: any YAML error falls back to defaults silently.
        pass
    env_override = os.environ.get("CV_AUTHOR_NAME")
    if env_override:
        author_name = env_override
    return author_name, version_anchor


def _author_token(author_name: str) -> str:
    """Build the filename-safe author token.

    "Alex Müller" → "CV_Alex_Müller"
    "Alex Müller" → "CV_Alex_Müller"
    Spaces collapse to underscores; other filesystem-illegal chars dropped.
    """
    cleaned = _BAD_FS_CHARS.sub(" ", author_name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return f"CV_{cleaned}" if cleaned else "CV_Author"


_author_name, CV_VERSION_ANCHOR = _load_output_config()
CV_AUTHOR_TOKEN = _author_token(_author_name)


def _sanitize(value: str) -> str:
    """Make `value` safe for use in a filename component.

    - Replaces path-illegal characters with a space
    - Collapses whitespace runs
    - Strips leading/trailing whitespace
    """
    cleaned = _BAD_FS_CHARS.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _extract_company(posting_text: str) -> str | None:
    """Pull the company name from a `**Arbeitgeber:** ...` or `**Unternehmen:** ...` line.

    Strips legal entity suffixes (AG, GmbH, Ltd, …) for a cleaner filename.
    """
    m = re.search(
        r"\*\*(?:Arbeitgeber|Unternehmen):\*\*\s*(.+?)(?:\n|$)", posting_text
    )
    if not m:
        return None
    name = m.group(1).strip()
    name = _LEGAL_SUFFIXES.sub("", name).strip()
    return _sanitize(name) or None


def _extract_position(posting_text: str) -> str | None:
    """Pull the position from the first `#`-heading.

    Strips:
    - "Stellenanzeige:"-style prefixes
    - trailing "– Company" suffixes
    - pipe-separated secondary info (" | Claude Expert 100%")
    - gender designations "(w/m/d)", "(m/f/d)", "(w m d)", …
    - percentage patterns "100%", "80%-100%"
    """
    for line in posting_text.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            heading = line[2:].strip()
            # Drop "Stellenanzeige:" / "Job Posting:" / "Stelle:" prefixes
            heading = re.sub(
                r"^(stellenanzeige|stellen|job\s*posting|stelle)\s*[:\-–]\s*",
                "",
                heading,
                flags=re.IGNORECASE,
            )
            # Drop trailing " – Company" if it duplicates the company line
            heading = re.split(r"\s+[–\-]\s+", heading, maxsplit=1)[0]
            # Drop pipe-separated secondary info (e.g. "| Claude Expert 100%")
            heading = _PIPE_SUFFIX.sub("", heading)
            # Drop gender designations
            heading = _GENDER_DESIGNATIONS.sub("", heading)
            # Drop percentage patterns
            heading = _PERCENTAGES.sub("", heading)
            return _sanitize(heading) or None
    return None


def friendly_cv_filename(
    posting_text: str,
    language: str = "de",
    fallback_slug: str | None = None,
    extension: str = ".md",
) -> str:
    """Return the recruiter-friendly CV filename (without directory).

    Args:
        posting_text: content of `00_stellenanzeige.md`
        language: "de" or "en" — added as suffix `_EN` for the English version
        fallback_slug: used if company/position can't be extracted
        extension: file extension including the leading dot (".md" or ".pdf")

    Returns:
        e.g. `CV_<Author>-<anchor> Company Position.md`
        or   `CV_<Author>-<anchor> Company Position_EN.pdf`
    """
    company = _extract_company(posting_text)
    position = _extract_position(posting_text)

    parts: list[str] = [f"{CV_AUTHOR_TOKEN}-{CV_VERSION_ANCHOR}"]
    if company:
        parts.append(company)
    if position:
        parts.append(position)
    if not company and not position and fallback_slug:
        parts.append(_sanitize(fallback_slug))

    base = " - ".join(parts).strip()
    suffix = "_EN" if language.lower() == "en" else ""
    if not extension.startswith("."):
        extension = "." + extension
    return f"{base}{suffix}{extension}"


def write_friendly_copy(
    canonical_path: Path,
    posting_path: Path,
    language: str = "de",
) -> Path | None:
    """Write a friendly-named copy of `canonical_path` next to the original.

    Handles both text (.md) and binary (.pdf) sources transparently via the
    canonical file's extension.

    Returns the new path, or None if the posting couldn't be read.
    """
    if not posting_path.exists() or not canonical_path.exists():
        return None
    posting_text = posting_path.read_text(encoding="utf-8")
    fallback = canonical_path.parent.name
    ext = canonical_path.suffix or ".md"
    name = friendly_cv_filename(
        posting_text, language=language, fallback_slug=fallback, extension=ext
    )
    friendly_path = canonical_path.parent / name
    if ext.lower() == ".pdf":
        friendly_path.write_bytes(canonical_path.read_bytes())
    else:
        friendly_path.write_text(canonical_path.read_text(encoding="utf-8"), encoding="utf-8")
    return friendly_path
