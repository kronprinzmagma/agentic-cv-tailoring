#!/usr/bin/env python3
"""Render data/persona.yaml into runnable demo CV inputs.

Outputs (relative to --target-dir, default: project root):
  data/.demo/standard_cv.md        — German standard CV
  data/.demo/standard_cv_en.md     — English standard CV
  data/.demo/zeugnisse/<filename>  — Markdown testimonials (one per employer)

Idempotent: re-running produces identical files (no timestamps).
By default writes into the .demo/ subdirectory of data/ so the private
repo's real standard_cv.md is not overwritten. Pass --overwrite-real
to write directly into data/ (the sync script uses this).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

LABELS = {
    "de": {
        "address": "Adresse",
        "mobile": "Mobile",
        "email": "E-Mail",
        "nationality": "Nationalität",
        "dob": "Geburtsdatum",
        "summary": "Management Summary",
        "competencies": "Schlüsselkompetenzen",
        "experience": "Berufserfahrung",
        "education": "Ausbildung",
        "certificates": "Zertifikate & Qualifikationen",
        "languages": "Sprachen",
        "skills": "Tools & Methoden",
    },
    "en": {
        "address": "Address",
        "mobile": "Mobile",
        "email": "E-Mail",
        "nationality": "Nationality",
        "dob": "Date of Birth",
        "summary": "Management Summary",
        "competencies": "Key Competencies",
        "experience": "Professional Experience",
        "education": "Education",
        "certificates": "Certificates & Qualifications",
        "languages": "Languages",
        "skills": "Skills & Tools",
    },
}


def _suffix(lang: str) -> str:
    return "_de" if lang == "de" else "_en"


def render_standard_cv(persona: dict, lang: str) -> str:
    labels = LABELS[lang]
    sfx = _suffix(lang)
    out: list[str] = []
    out.append(f"# {persona['name']}")
    out.append("")
    out.append(f"**{labels['address']}:** {persona['address']}")
    out.append(f"**{labels['mobile']}:** {persona['mobile']}")
    out.append(f"**{labels['email']}:** {persona['email']}")
    out.append(f"**{labels['nationality']}:** {persona['nationality']}")
    out.append(f"**{labels['dob']}:** {persona['date_of_birth']}")
    out.append("")
    out.append("---")
    out.append("")
    out.append(f"## {labels['summary']}")
    out.append("")
    summary = persona[f"summary{sfx}"].strip()
    out.append(summary)
    out.append("")
    out.append("---")
    out.append("")
    out.append(f"## {labels['competencies']}")
    out.append("")
    for kc in persona["key_competencies"]:
        out.append(f"- **{kc['title' + sfx]}** – {kc['body' + sfx]}")
    out.append("")
    out.append("---")
    out.append("")
    out.append(f"## {labels['experience']}")
    out.append("")
    for exp in persona["experience"]:
        title = exp[f"title{sfx}"]
        out.append(f"### {exp['period']} | {exp['company']} – {title}")
        out.append("")
        for b in exp[f"bullets{sfx}"]:
            out.append(f"- {b}")
        out.append("")
    out.append("---")
    out.append("")
    out.append(f"## {labels['education']}")
    out.append("")
    for ed in persona["education"]:
        degree = ed[f"degree{sfx}"]
        out.append(f"- **{ed['period']}** – {ed['institution']}, {degree}")
    out.append("")
    out.append(f"## {labels['certificates']}")
    out.append("")
    for c in persona["certificates"]:
        out.append(f"- {c['name']} ({c['year']})")
    out.append("")
    out.append(f"## {labels['languages']}")
    out.append("")
    for lg in persona["languages"]:
        name = lg[f"name{sfx}"]
        out.append(f"- {name}: {lg['level']}")
    out.append("")
    out.append(f"## {labels['skills']}")
    out.append("")
    for sk in persona["skills"]:
        cat = sk[f"category{sfx}"]
        items = ", ".join(sk["items"])
        out.append(f"- **{cat}:** {items}")
    out.append("")
    return "\n".join(out)


def render_testimonial(persona: dict, testimonial: dict, lang: str) -> str:
    sfx = _suffix(lang)
    body = testimonial[f"body{sfx}"].strip()
    issuer = testimonial[f"issuer{sfx}"]
    return (
        f"# {testimonial['employer']} — {testimonial['period']}\n\n"
        f"{issuer}\n\n"
        f"---\n\n"
        f"{body}\n"
    )


def write_outputs(
    persona_data: dict,
    target_dir: Path,
    overwrite_real: bool = False,
) -> list[Path]:
    if overwrite_real:
        data_dir = target_dir / "data"
    else:
        data_dir = target_dir / "data" / ".demo"
    zeugnisse_dir = data_dir / "zeugnisse"
    data_dir.mkdir(parents=True, exist_ok=True)
    zeugnisse_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    persona = persona_data["persona"]
    de = render_standard_cv(persona, "de")
    en = render_standard_cv(persona, "en")
    cv_de = data_dir / "standard_cv.md"
    cv_en = data_dir / "standard_cv_en.md"
    cv_de.write_text(de, encoding="utf-8")
    cv_en.write_text(en, encoding="utf-8")
    written.extend([cv_de, cv_en])

    for t in persona_data.get("testimonials", []):
        path = zeugnisse_dir / t["filename"]
        path.write_text(render_testimonial(persona, t, "de"), encoding="utf-8")
        written.append(path)

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render demo persona into CV inputs."
    )
    parser.add_argument(
        "--persona",
        type=Path,
        default=Path("data/persona.yaml"),
        help="Path to persona.yaml (default: data/persona.yaml relative to target-dir)",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=Path.cwd(),
        help="Target project root (default: cwd)",
    )
    parser.add_argument(
        "--overwrite-real",
        action="store_true",
        help="Write to data/ instead of data/.demo/ — destructive in private repo.",
    )
    args = parser.parse_args(argv)

    if args.persona.is_absolute():
        persona_path = args.persona
    else:
        # Try target-dir first, then cwd (allows calling from project root with a
        # non-project target-dir, e.g. uv run python scripts/init_persona.py --target-dir /tmp/test)
        persona_path = args.target_dir / args.persona
        if not persona_path.exists():
            persona_path = Path.cwd() / args.persona
    if not persona_path.exists():
        print(f"ERROR: persona file not found: {persona_path}", file=sys.stderr)
        return 1
    data = yaml.safe_load(persona_path.read_text(encoding="utf-8"))
    paths = write_outputs(data, args.target_dir, overwrite_real=args.overwrite_real)
    for p in paths:
        print(f"wrote: {p}")
    print(f"\n{len(paths)} files written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
