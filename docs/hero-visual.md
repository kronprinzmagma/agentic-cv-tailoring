# README visual provenance

Asset: [screenshot-web-ui.png](screenshot-web-ui.png)

Captured from public source commit `ad83a874889d0d09bc7cdff26c062a259ce167a9` on 2026-11-17. This is a staged demonstration with exclusively synthetic data, not evidence of a live AI run or measured model performance. The visible application/CLI content is based on existing code, not an invented interface.

## Method

The original HTML/CSS/JavaScript web UI was rendered in Chromium. Its existing renderJob function received a fixed run fixture; the job-posting and output fields contain hand-written synthetic text. The surrounding title and frame are editorial, outside the application. No application UI or product logic was changed.

## Synthetic inputs and limits

Fictional persona Alex Beispiel, employer Beispiel Digital AG, role Product Lead. The $0.74 / 26 calls / 87% cache values, completion status, stage details and all draft text are fixtures. No LLM pipeline, evidence validation or PDF generation was run for this image. The existing cost tile supports an agent breakdown in its hover tooltip; the image shows only the compact summary.

## Source references

- [src/cv_tailor/web_assets/index.html](../src/cv_tailor/web_assets/index.html)
- [src/cv_tailor/cost_tracking.py](../src/cv_tailor/cost_tracking.py)
- [src/cv_tailor/web.py](../src/cv_tailor/web.py)
- [src/cv_tailor/agents/factcheck.py](../src/cv_tailor/agents/factcheck.py)

## Maintenance

The visual and this public README are maintained together in the working repository. The public mirror receives `docs/screenshot-web-ui.png` and this provenance file through the existing allowlist. The sync checks the active README image reference, image signature, provenance link and staged file selection before replacing any public content. A missing or excluded asset stops publication instead of removing the hero.

Update the source README and asset together. The image uses exclusively synthetic data and staged model responses; keep the embedded demo label and caption. Only this reviewed image path is added to the binary allowlist. Existing privacy checks remain in place.
