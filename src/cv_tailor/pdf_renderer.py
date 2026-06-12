"""PDF renderer: print the final CV through the manual HTML template via headless Chromium.

Uses Playwright with the bundled chromium-headless-shell so the output is
byte-for-byte equivalent to the user's manual print-to-PDF step. No font
shrinking, no text mutation, no auto-fit — if content overflows 3 pages,
we write a marker file and return None instead of producing a PDF.

Pipeline position: after writer_loop + diff + keyword_marker + translator.
Each language gets its own PDF: 04_final_de.pdf and (if translator ran)
04_final_en.pdf. Friendly recruiter-named copies are written alongside
the canonical files via cv_filename.write_friendly_copy.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from cv_tailor.cv_filename import CV_AUTHOR_TOKEN
from cv_tailor.logging_config import get_logger
from cv_tailor.orchestrator import RunContext, write_run_log_entry

log = get_logger(__name__)

# IN-02: anchor to repo root so the module works regardless of CWD
# (CLI/web invoke from repo root, but eval harnesses or scripts can run elsewhere).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Template-Variante per Env wählbar (z.B. A/B-Test mit anderem Foto):
#   CV_TAILOR_TEMPLATE="CV_Template variante b.html" uv run cv-tailor …
# Relativ-Pfade werden am Repo-Root aufgelöst; Default bleibt das bisherige Template.
_template_override = os.environ.get("CV_TAILOR_TEMPLATE", "").strip()
TEMPLATE_PATH = (
    (_PROJECT_ROOT / _template_override if not Path(_template_override).is_absolute() else Path(_template_override))
    if _template_override
    else _PROJECT_ROOT / "CV_Template neu mit bild.html"
)
STANDARD_CV_PATH = _PROJECT_ROOT / "data" / "standard_cv.md"
STANDARD_CV_PATH_EN = _PROJECT_ROOT / "data" / "standard_cv_en.md"
MAX_PAGES = 3
DEFAULT_ROLE_LINE = "Digital Product · Platform · AI"

# Section heading sets per language — used to extract the tail (Education,
# Certificates, Languages, Skills & Tools) from the right Standard-CV file.
_TAIL_SECTIONS_DE = ["Ausbildung", "Zertifikate & Qualifikationen", "Sprachkenntnisse", "Software & Tools"]
_TAIL_SECTIONS_EN = ["Education", "Certificates & Qualifications", "Languages", "Skills & Tools"]


# ---------------------------------------------------------------------------
# Markdown stitching: Standard-CV header + generated sections + tail sections
# ---------------------------------------------------------------------------

def _extract_section(text: str, heading: str) -> str:
    """Return the `## heading` block (heading included) up to next `## ` or EOF.

    Case-insensitive match (WR-06) so minor capitalization variations in the
    Standard-CV don't silently drop tail sections. Missing sections are
    logged at WARN level so the user notices breakage even when the PDF
    still renders.
    """
    pattern = rf"(##\s+{re.escape(heading)}.*?)(?=^##\s+|\Z)"
    m = re.search(pattern, text, re.S | re.M | re.I)
    if not m:
        log.warning("pdf_renderer.section_missing", heading=heading)
        return ""
    return m.group(1).rstrip()


def _extract_header_block(standard_cv_text: str) -> str:
    """Return name (H1) + contact lines until the first `---` separator."""
    parts = standard_cv_text.split("---", 1)
    return parts[0].strip()


def _extract_tail_sections(standard_cv_text: str, lang: str = "de") -> str:
    """Return Education / Certificates / Languages / Tools blocks joined by `---`."""
    sections = _TAIL_SECTIONS_EN if lang == "en" else _TAIL_SECTIONS_DE
    blocks = [b for b in (_extract_section(standard_cv_text, s) for s in sections) if b]
    return "\n\n---\n\n".join(blocks)


# Headings the generated CV (writer/translator) must not emit — they live in
# the standard-CV tail and are stitched in by the renderer. If a generated
# section uses one of these headings it duplicates with the tail in the PDF.
# Stripping is case-insensitive and tolerates minor spelling variants.
_GENERATED_TAIL_FORBIDDEN = (
    # English
    "education", "certificates & qualifications", "certificates and qualifications",
    "certifications", "languages", "language skills",
    "skills & tools", "skills and tools", "skills", "tools",
    # German (defensive — writer shouldn't produce these in DE either)
    "ausbildung", "zertifikate & qualifikationen", "zertifikate und qualifikationen",
    "zertifikate", "sprachkenntnisse", "sprachen",
    "software & tools", "software und tools", "software", "kenntnisse & tools",
)


def _strip_generated_tail(text: str) -> str:
    """Remove rogue tail-style sections from the generated CV before stitching.

    The writer and translator are instructed to stop after the last
    Berufserfahrungs-Station because Education / Certificates / Languages /
    Skills & Tools are appended by `_extract_tail_sections`. Defensive
    fallback if a model still emits one: strip any matching `## Heading`
    block (heading + body) up to the next `## ` or EOF. Operates on the
    full text so subsequent `---` separators don't fool the scanner.
    """
    pattern = re.compile(
        r"(^##\s+(?P<heading>[^\n]+?)\s*$)"           # heading line
        r".*?"                                         # body
        r"(?=^##\s+|\Z)",                              # until next H2 / EOF
        re.S | re.M,
    )

    def _drop_if_forbidden(match: re.Match[str]) -> str:
        heading_norm = re.sub(r"[^a-z& ]+", "", match.group("heading").lower()).strip()
        # Also normalise " and " → " & " for matching
        heading_alt = heading_norm.replace(" and ", " & ").replace("  ", " ")
        if heading_norm in _GENERATED_TAIL_FORBIDDEN or heading_alt in _GENERATED_TAIL_FORBIDDEN:
            log.warning("pdf_renderer.stripped_generated_tail", heading=match.group("heading"))
            return ""
        return match.group(0)

    cleaned = pattern.sub(_drop_if_forbidden, text)
    # Collapse leftover trailing separator runs (`---\n\n---\n`) created by stripping
    cleaned = re.sub(r"(\n---\s*){2,}", "\n---\n", cleaned)
    # Trim trailing separator + whitespace at the very end
    cleaned = re.sub(r"(\n---\s*)+\Z", "", cleaned).rstrip()
    return cleaned


def _bulletize_experience(text: str) -> str:
    """Convert paragraph-style Berufserfahrung entries to bullet-style.

    The template's experience renderer expects `- bullet` items beneath each
    `### YYYY | Firma – Titel`. The writer often produces paragraphs in this
    section (matching the gold-standard exemplars). Without conversion those
    paragraphs render as empty stations.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_berufserfahrung = False
    pending: list[str] = []

    def flush():
        if pending:
            joined = " ".join(p.strip() for p in pending).strip()
            if joined:
                out.append(f"- {joined}")
            pending.clear()

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("## "):
            flush()
            in_berufserfahrung = stripped.lower().startswith(("## berufserfahrung", "## professional experience"))
            out.append(line)
            continue
        if not in_berufserfahrung:
            out.append(line)
            continue
        if (
            stripped.startswith("### ")
            or stripped.startswith("- ")
            or stripped.startswith("* ")
            or stripped.startswith("---")
            or stripped == ""
        ):
            flush()
            out.append(line)
            continue
        pending.append(stripped)
    flush()
    return "\n".join(out)


def build_template_markdown(
    final_cv_md: Path,
    standard_cv_path: Path | None = None,
    role_line: str = DEFAULT_ROLE_LINE,
    lang: str = "de",
    generated_override: str | None = None,
) -> str:
    """Stitch a template-compatible markdown: header + generated sections + tail.

    Picks the German or English Standard-CV based on `lang`. The header
    block (name + contact) and the tail sections (Education / Certificates
    / Languages / Skills & Tools) are drawn from that file; section
    headings adapt to the language naturally.

    `generated_override` lets the caller pass an in-memory string instead
    of re-reading `final_cv_md` — avoids a TOCTOU race with concurrent
    editor saves on the freshest-MD code path in `run_pdf_renderer`.
    """
    if standard_cv_path is None:
        standard_cv_path = STANDARD_CV_PATH_EN if lang == "en" else STANDARD_CV_PATH
    if not standard_cv_path.exists() and lang == "en":
        # Fall back to German Standard-CV if English version is missing
        log.warning("pdf_renderer.no_en_standard_cv", fallback=str(STANDARD_CV_PATH))
        standard_cv_path = STANDARD_CV_PATH
    standard = standard_cv_path.read_text(encoding="utf-8")
    generated = generated_override if generated_override is not None else final_cv_md.read_text(encoding="utf-8")

    header = _extract_header_block(standard)
    # Insert `**Rolle:**` / `**Role:**` line right after the name (H1)
    role_key = "Role" if lang == "en" else "Rolle"
    lines = header.splitlines()
    insert_at = 1
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = i + 1
            break
    while insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1
    lines.insert(insert_at, f"**{role_key}:** {role_line}")
    lines.insert(insert_at, "")
    header_block = "\n".join(lines)

    # Strip the writer's meta header (`# Finaler CV ...` / `# Final CV ...`)
    gen_clean = re.sub(
        r"^#\s+(Final|Finaler).*?(?=^##\s+)", "", generated, count=1, flags=re.S | re.M
    ).strip()
    # Strip rogue tail-style sections that the writer/translator may have
    # produced despite the prompt — the real tail comes from the standard CV.
    gen_clean = _strip_generated_tail(gen_clean)
    gen_clean = _bulletize_experience(gen_clean)

    tail = _extract_tail_sections(standard, lang=lang)
    return f"{header_block}\n\n---\n\n{gen_clean}\n\n---\n\n{tail}\n"


# ---------------------------------------------------------------------------
# Playwright rendering
# ---------------------------------------------------------------------------

async def _render_pdf_async(template_path: Path, markdown_text: str, output_pdf: Path) -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        # --disable-font-subpixel-positioning + --font-render-hinting=none
        # force Chromium to embed fonts as Type0/CIDFont instead of Type3.
        # Type3 fonts are drawing procedures with no character encoding;
        # macOS Preview cannot extract text from them (copy-paste produces
        # garbled output). Type0/CIDFont fonts carry proper ToUnicode CMap
        # tables that every PDF reader — including macOS Preview — can use.
        browser = await p.chromium.launch(args=[
            "--disable-font-subpixel-positioning",
            "--font-render-hinting=none",
        ])
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"file://{template_path.resolve()}")
        await page.wait_for_load_state("networkidle")
        # Inject markdown via the template's localStorage key
        await page.evaluate(
            f"localStorage.setItem('cv-template:md', {json.dumps(markdown_text)});"
        )
        await page.reload()
        await page.wait_for_load_state("networkidle")
        # Small delay for any final font-rendering settling
        await page.wait_for_timeout(500)
        await page.pdf(
            path=str(output_pdf),
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            prefer_css_page_size=True,
        )
        await browser.close()

    try:
        from pypdf import PdfReader
        return len(PdfReader(str(output_pdf)).pages)
    except Exception as exc:  # noqa: BLE001
        # IN-01: surface the failure so silent overflow is visible
        log.warning("pdf_renderer.page_count_unavailable", error=str(exc))
        return -1


def render_pdf(template_path: Path, markdown_text: str, output_pdf: Path) -> int:
    """Render a CV markdown through the template to PDF. Returns page count.

    Synchronous wrapper around the async Playwright API.
    """
    return asyncio.run(_render_pdf_async(template_path, markdown_text, output_pdf))


def count_pages_de(
    ctx: RunContext,
    template_path: Path = TEMPLATE_PATH,
    standard_cv_path: Path | None = None,
    role_line: str = DEFAULT_ROLE_LINE,
) -> int:
    """Return the page count the current 04_final_de.md would produce.

    Renders to a temporary file (deleted afterwards). Returns -1 on error
    (Playwright unavailable, template missing, etc.).
    """
    import tempfile

    final_path = ctx.run_dir / "04_final_de.md"
    if not final_path.exists() or not template_path.exists():
        return -1
    if standard_cv_path is None:
        standard_cv_path = STANDARD_CV_PATH
    try:
        md = build_template_markdown(
            final_path,
            standard_cv_path=standard_cv_path,
            lang="de",
            role_line=role_line,
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            pages = render_pdf(template_path, md, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return pages
    except Exception as exc:  # noqa: BLE001
        log.warning("pdf_renderer.count_pages_error", error=str(exc))
        return -1


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run_pdf_renderer(
    ctx: RunContext,
    template_path: Path = TEMPLATE_PATH,
    standard_cv_path: Path | None = None,
    role_line: str = DEFAULT_ROLE_LINE,
) -> list[Path]:
    """Render PDF(s) for the final CV(s) in the run directory.

    Renders 04_final_de.pdf (always) and 04_final_en.pdf (if 04_final_en.md
    exists from the translator). If a render produces > MAX_PAGES, writes a
    `_pdf_overflow_<lang>.md` marker and skips the PDF. Returns the list of
    successfully written PDF paths.
    """
    if not template_path.exists():
        log.warning("pdf_renderer.template_missing", path=str(template_path))
        write_run_log_entry(
            ctx.run_dir, "pdf_renderer", f"Template nicht gefunden: {template_path}"
        )
        return []

    import os as _os
    written: list[Path] = []
    for lang in ("de", "en"):
        canonical_md = ctx.run_dir / f"04_final_{lang}.md"
        if not canonical_md.exists():
            continue
        # If a recruiter-friendly copy exists AND was edited more recently
        # than the canonical, use it as input. Matches the user's natural
        # workflow of editing the file they want to send.
        source_md = canonical_md
        generated_text: str | None = None
        friendly_candidates = [
            p for p in ctx.run_dir.glob(f"{CV_AUTHOR_TOKEN}-*.md")
            if p.is_file()
            and (lang == "en") == p.stem.endswith("_EN")
        ]
        for fc in friendly_candidates:
            if fc.stat().st_mtime > canonical_md.stat().st_mtime:
                # Read once into memory — same buffer is used for sync-back
                # and for the render — avoids TOCTOU when the editor saves
                # mid-render. Atomic write via tmp + os.replace.
                generated_text = fc.read_text(encoding="utf-8")
                tmp = canonical_md.with_suffix(canonical_md.suffix + ".tmp")
                tmp.write_text(generated_text, encoding="utf-8")
                _os.replace(tmp, canonical_md)
                source_md = fc
                log.info("pdf_renderer.using_friendly_source", lang=lang, path=str(fc))
                break
        try:
            md = build_template_markdown(
                source_md, standard_cv_path, role_line,
                lang=lang, generated_override=generated_text,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("pdf_renderer.build_failed", lang=lang, error=str(exc))
            write_run_log_entry(
                ctx.run_dir, "pdf_renderer", f"{lang}: Markdown-Build fehlgeschlagen: {exc}"
            )
            continue

        target_pdf = ctx.run_dir / f"04_final_{lang}.pdf"
        try:
            pages = render_pdf(template_path, md, target_pdf)
        except Exception as exc:  # noqa: BLE001
            log.warning("pdf_renderer.render_failed", lang=lang, error=str(exc))
            write_run_log_entry(
                ctx.run_dir, "pdf_renderer", f"{lang}: Render fehlgeschlagen: {exc}"
            )
            if target_pdf.exists():
                target_pdf.unlink()
            continue

        if pages < 0:
            log.warning(
                "pdf_renderer.page_count_skipped",
                lang=lang,
                path=str(target_pdf),
            )
            write_run_log_entry(
                ctx.run_dir,
                "pdf_renderer",
                f"{lang}: Seitenzahl konnte nicht ermittelt werden, Overflow-Check übersprungen",
            )
        if pages > MAX_PAGES:
            overflow_marker = ctx.run_dir / f"_pdf_overflow_{lang}.md"
            overflow_marker.write_text(
                f"# PDF-Überlauf\n\nCV {lang.upper()} hat {pages} Seiten — Limit ist {MAX_PAGES}.\n"
                "Kein PDF ausgeliefert. Bitte Inhalt manuell kürzen oder Layout anpassen.\n",
                encoding="utf-8",
            )
            if target_pdf.exists():
                target_pdf.unlink()
            log.warning("pdf_renderer.overflow", lang=lang, pages=pages, max=MAX_PAGES)
            write_run_log_entry(
                ctx.run_dir,
                "pdf_renderer",
                f"{lang}: PDF-Überlauf ({pages} Seiten > {MAX_PAGES}), kein PDF ausgeliefert",
            )
            continue

        log.info("pdf_renderer.done", lang=lang, pages=pages, path=str(target_pdf))
        write_run_log_entry(
            ctx.run_dir, "pdf_renderer", f"04_final_{lang}.pdf geschrieben ({pages} Seiten)"
        )
        written.append(target_pdf)

        # Also write a recruiter-friendly named copy alongside (CV_<Author>-<anchor> ... .pdf)
        try:
            from cv_tailor.cv_filename import write_friendly_copy
            posting_path = ctx.run_dir / "00_stellenanzeige.md"
            friendly = write_friendly_copy(target_pdf, posting_path, language=lang)
            if friendly is not None:
                log.info("pdf_renderer.friendly_copy", lang=lang, path=str(friendly))
        except Exception as exc:  # noqa: BLE001
            log.warning("pdf_renderer.friendly_copy_failed", lang=lang, error=str(exc))
    return written
