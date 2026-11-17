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

The PNG is a static documentation asset; no production code or dependency was changed. Its outer framing was authored in HTML/CSS and captured with Chromium, without generative image synthesis. The demo label is embedded in the pixels as well as the README caption. Keep both when reusing it.

This change targets only this public mirror. The private-to-public synchronisation may replace README.md and may omit or remove public-only assets. Durable preservation would require a separately authorised change to the private sync source/allowlist; none was made here. Recheck the README and asset after future syncs.
