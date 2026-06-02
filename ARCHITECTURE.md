# Architecture

This document distills the engineering decisions behind the agentic CV tailoring
system. See [`README.md`](./README.md) for a high-level overview and getting-started
instructions.

Every decision is captured here with its rationale. Where trade-offs were made,
the reasoning is documented so future maintainers can revisit rather than
re-investigate.

---

## Architecture Overview

The pipeline is a linear sequence with two internal loops (per-section writer
loop) and two conditional branches (profile-fit gate, language detection):

```
Job Posting
  → Analyst                         (Anthropic Sonnet)
  → Profile-Fit Gate                (deterministic, no LLM)
  → Factcheck v1                    (Anthropic Haiku)
  → [Pause for clarifications?]     (human gate, optional)
  → Writer Loop — per section × 3:
      Writer → Hiring Reviewer + Coach Reviewer (parallel)
            → Consistency Check    (deterministic)
            → Length Check         (deterministic)
            → Factcheck v2         (Anthropic Haiku)
            → [if veto: loop, max 2 rounds]
  → Diff Agent                      (Anthropic Haiku)
  → Keyword Marker                  (Anthropic Haiku)
  → [Language detection: EN?]
      yes → Translator              (OpenAI gpt-4.1-mini)
      no  → PDF Renderer            (Playwright)
  → PDF Renderer
  → Final CV + PDF + Diff
```

**Sequential vs parallel:** The three CV sections (Management Summary,
Key Competencies, Work Experience) run sequentially by default
(`writer_section_concurrency: 1` in `config.yaml`). Within each section,
the Hiring Reviewer and Coach Reviewer run in parallel via
`ThreadPoolExecutor(2)`. The sequential default is a practical constraint:
Anthropic Tier 1 allows ~30k input tokens/minute; three sections each
consuming ~10k tokens simultaneously would exceed the limit reliably.
Operators on Tier 2+ can set `writer_section_concurrency: 3` to restore
full parallelism.

**Iteration budget:** Maximum 2 rounds per CV section. This is enforced
hard — the loop does not exceed it even when reviewers still signal
dissatisfaction. Best-case: one clean round (Writer + Hiring + Coach +
Factcheck), no Round 2.

---

## Agent Roles

| Agent | Provider | Model class | Responsibility |
|---|---|---|---|
| Analyst | Anthropic | Sonnet | Requirements abstraction + experience-activation mapping |
| Writer | Anthropic | Sonnet | Per-section CV draft authoring |
| Hiring Manager Reviewer | OpenAI | gpt-4.1 | Skeptical recruiter perspective (anti-echo) |
| Coach Reviewer | Anthropic | Sonnet | Insider voice + profile-fit perspective |
| Factcheck | Anthropic | Haiku | Veto against the evidence index |
| Diff | Anthropic | Haiku | Compact diff table generation |
| Keyword Marker | Anthropic | Haiku | Post-hoc bold marking of keywords |
| Translator | OpenAI | gpt-4.1-mini | DE→EN translation when posting is English |
| Naturalisation | Anthropic | Haiku | EN polish suggestions after translation |

**Anti-echo principle.** The Hiring Manager Reviewer and the Translator
deliberately run on OpenAI while all other agents run on Anthropic. Putting
every agent on the same provider creates a risk of confirmation bias — the
Writer drafts on Anthropic, the Reviewer reviews on Anthropic, and both
share the same conceptual priors. Routing the primary quality gate (the
Hiring Reviewer) and the language transformation step (Translator) to a
different provider creates independent cognitive paths. Provider switching
is a config change only (`config.yaml`); no code changes are required.

**Coach as insider.** The Coach Reviewer receives the full standard CV plus
the evidence index as a cached context prefix. This lets it detect
role-framing drift (e.g., a product manager presenting engineering
deliverables as personal output, or scale claims that exceed what the
evidence supports) — something a reviewer who sees only the draft cannot do.

**Factcheck scope.** Each factcheck call receives the evidence index as a
static cached prefix and the current section draft as the dynamic suffix.
It checks every substantive claim against the evidence index. A clean result
lets the loop advance; a veto triggers Round 2. After `MAX_ROUNDS`, a
remaining factcheck veto becomes a hard block — the section is not written.

---

## Deterministic Gates

Three deterministic checks run during the pipeline to stop expensive LLM
loops early when the inputs or outputs don't meet structural requirements.
None of these use an LLM.

### Profile-Fit Gate (`src/cv_tailor/profile_fit.py`)

Runs after the Analyst and before any Writer calls. Parses the analyst's
requirement-alignment table from `01_analyse.md` and identifies:

- **LÜCKE rows** (critical gaps): role requirements the CV cannot address
- **SCHWACH rows with strength qualifiers**: requirements phrased with
  "strong", "deep", "fundiert", "tief" where the evidence is thin

**Effect:** If critical gaps exist, the pipeline pauses (CLI: `click.confirm`;
Web UI: a warning panel with "Continue anyway" / "Cancel" buttons). The
operator can proceed or abort before any Writer + Reviewer + Factcheck tokens
flow. Aborting saves the full cost of the writer loop (~$0.60 of the ~$0.74
total run cost).

**Why gate rather than just warn?** If the structural fit is absent, the
writer loop can only bridge the gap through scale inflation or invented
claims — exactly what the factcheck step is designed to block. Stopping
before the loop is cheaper and more honest.

### Consistency Check (`src/cv_tailor/consistency_check.py`)

Runs after every Writer round for the Work Experience section. Three rules,
all deterministic:

1. **Verbatim header rule.** Every `### YYYY[–YYYY] | Employer — Title`
   header in the generated output must match the corresponding line in
   `data/standard_cv.md` character-for-character. Any deviation is a veto.

2. **One-station-per-employer rule.** No employer may appear as two separate
   dated blocks (splitting one tenure into two entries to create visual
   variety). Detected by counting distinct date ranges per employer token.

3. **No-invented-employer rule.** Every employer name in the output must
   appear in the standard CV. Unknown employer names are vetoed.

**On hard veto after MAX_ROUNDS:** `autofix_headers()` runs as a fallback.
It replaces headers with verbatim standard-CV headers and merges split
stations. The body bullets are not touched.

**Findings** are written to `03_iterationen/<section>_v<N>_consistency.md`.
The Writer reads this file in Round 2 as mandatory correction context.

### Length Check (`src/cv_tailor/length_check.py`)

Runs after every Writer round.

- **Bullet rule:** Every bullet in the Work Experience section must be
  ≤22 words. Longer bullets receive a soft veto with the offending lines
  listed.
- **Summary rule:** The Management Summary must be 140–160 words. Veto
  fires at >170 words.

**Soft veto:** After `MAX_ROUNDS`, the output is accepted with a
`style_veto_accepted_at_max_rounds` log entry — not blocked. Rationale:
a length violation does not make the CV factually wrong; it degrades
scannability. Best-effort output is better than no output.

**Findings** are written to `03_iterationen/<section>_v<N>_length.md` and
fed back to the Writer in Round 2.

---

## Topic-Gated Clarification Memory

### Problem: cross-claim fusion

When the operator answers clarification questions about posting A ("which
user groups did this product serve?"), those answers are persisted to
`data/clarifications.json`. If a later posting B is in a different domain,
replaying all stored answers creates a cross-claim fusion risk: the Writer
could combine an evidence anchor from posting A's domain with a factual
claim from posting B, producing a compound statement that is unsupported
by any single piece of evidence.

### Solution: topic taxonomy + on-read filtering

Each clarification entry carries a `topics` list. When a clarification is
stored, a deterministic (no LLM) keyword classifier assigns topics from a
small, high-precision taxonomy:

`analytics`, `user_research`, `languages`, `subscription_saas`, `ml_ai`,
`team_management`, `domain_health`, `domain_media`, `domain_finance`,
`domain_gastro`, `tech_stack`, `compliance_security`

Plus `*` (universal) as a fallback for short or unclassifiable entries.

At prompt-construction time, `format_clarifications_for_prompt(current_context=...)`
filters the stored entries to those whose topics overlap with the current
posting and analysis text. Domain-mismatched answers are excluded.

**What it does not prevent:** An answer with mixed-domain content (e.g.,
answering a media-domain question that also mentions healthcare users)
will carry both topics and will activate in either context. The Coach
Reviewer's dedicated "Addressee invented (cross-claim fusion)" veto rule
is the second defensive layer for such cases.

**Backwards compatibility:** Entries written before the `topics` field was
introduced are classified on-read in memory. `clarifications.migrate_topics()`
persists the classification back to disk idempotently.

---

## Prompt Engineering

### Prompts as versioned Markdown files

All agent prompts live in `prompts/` as Markdown files (`analyst.md`,
`writer.md`, `hiring_manager_reviewer.md`, `coach_reviewer.md`,
`factcheck.md`, `translator.md`, `diff.md`, `keyword_marker.md`,
`naturalisation.md`). Prompt engineering is part of the codebase and
is reviewable via `git diff`. A `prompts_sha` is captured in the quality
snapshot for reproducibility.

### Static-vs-dynamic split for cache efficiency

```
Cached prefix (static, sent once per session warm-up):
  system prompt
  + standard CV
  + evidence index (compact form)
  + style exemplars (2 manually-polished example CVs)
  + analyst output (for writer + coach + factcheck)

Uncached suffix (dynamic, changes per call):
  current section draft
  + reviewer feedback (Round 2 only)
  + deterministic-gate findings (Round 2 only)
```

The split is implemented in `src/cv_tailor/llm._prepare_messages_for_provider()`.
Anthropic content is wrapped in `cache_control: ephemeral`. OpenAI content
arrays are flattened to strings (OpenAI does not support Anthropic's
`cache_control` field).

Expected cache-hit rate after warm-up: ~50–75%. This is tracked in the
quality snapshot as `cache_hit_rate` and in the `llm_calls.jsonl` log via
`cache_read_input_tokens` and `cache_creation_input_tokens`.

### Demonstration-based style transfer

Two manually-polished CVs live under `data/examples/optimized_cvs/.demo/`
as style exemplars. The Writer loads them as a cached prefix via
`load_style_exemplars(max_count=2)`. Style is transferred by example —
the model observes what the target register looks like rather than following
prose rules like "write punchy sentences".

`max_count=2` balances style signal against token cost. Demonstration-based
learning saturates quickly; two well-chosen exemplars transport the target
register as effectively as four, at ~30% lower token cost.

### Writer round-2 rule: polish, do not rewrite

Round 2 is triggered by reviewer feedback, but the round-2 constraint is
strict: the Writer may only delete, reword, or correct scale claims from
the Round 1 draft. Adding new topics, new evidence categories, new bullet
headlines, or "learning-as-track-record" upgrades (treating courses as
equivalent to delivered work) is forbidden in Round 2. This prevents
reviewers from inadvertently causing the Writer to introduce new unsupported
claims in response to style feedback.

---

## Quality Observability

### Cost tracking (`src/cv_tailor/cost_tracking.py`)

Aggregates per `run_id` from the already-logged `cost_usd`/token fields
in `logs/<YYYY-MM>/llm_calls.jsonl`. O(N) scan over current month (and
prior month for long runs). Exposed in the Web UI as a cost tile with a
compact string (`$1.23 · 31 calls · 55% cache-hits`) and a hover tooltip
with per-agent breakdown.

### Quality snapshot (`src/cv_tailor/quality_snapshot.py`)

At pipeline completion, a deterministic (no LLM) snapshot is written to
`logs/quality_snapshots.jsonl` via upsert (one entry per `run_id`). Metrics:

| Metric | What it measures |
|---|---|
| `writer_round_2_count` | Sections that required a second round |
| `factcheck_iter_vetos` | Factcheck vetos across all iterations |
| `consistency_findings` | Deterministic header-drift findings |
| `summary_word_count` | Management Summary word count |
| `diff_row_count` | Number of rows in the diff table |
| `bullet_length_stats` | mean / stddev / max of bullet word counts |
| `cliche_density_per_100_words` | Frequency of curated filler phrases |
| `cache_hit_rate` | Fraction of input tokens served from cache |
| `total_cost_usd` | Total cost for the run |
| `calls` | Total LLM calls |
| `git_sha` | Repo commit at run time |
| `prompts_sha` | Hash of all prompt files |

### Regression detection

`detect_regressions()` compares the latest snapshot against the
trailing-N median (default: 5 runs). Regression fires when:

- Higher-is-worse metrics (`writer_round_2_count`, `factcheck_iter_vetos`,
  `consistency_findings`, `rate_limit_retries`, `errors`, `total_cost_usd`)
  exceed `median × (1 + threshold)`
- Lower-is-worse metrics (`cache_hit_rate`) fall below `median × (1 - threshold)`

Thresholds are configured per metric. Minimum 3 preceding runs required
before detection activates (avoids false positives on early data).

**CLI:** `uv run cv-tailor quality-trend [--last N] [--backfill]` renders
an ASCII trend table and lists current regression findings.
`--backfill` generates snapshots for all existing runs retroactively.

**Web UI:** Regression findings appear as an orange warning block under the
pipeline stepper on run completion.

---

## Eval Suite

### Schema

```yaml
id: demo_pm_de
run_glob: "2026-05-*_demo_pm_de_*"
artifacts:
  final_cv: "04_final_de.md"
  diff: "05_diff.md"
checks:
  required_final_contains: []
  forbidden_final_contains: []
  required_diff_contains: []
  max_summary_words: 160
  max_diff_rows: 26
judge:
  min_score: 4
  min_factfulness: 4
  max_critical_issues: 0
  rubric: "Skeptical evaluation rubric for role fit and factual accuracy."
```

A case checks local run artifacts in `runs/` without committing them.
`run_glob` picks the most recent matching run.

### Five metrics

1. **Factuality (deterministic):** checks `required_final_contains` /
   `forbidden_final_contains` — strings that must or must not appear
2. **Vocabulary coverage:** fraction of posting keywords present in the
   final CV
3. **Length drift:** `summary_word_count` against `max_summary_words`;
   `diff_row_count` against `max_diff_rows`
4. **Diff granularity:** checks `required_diff_contains`
5. **LLM-as-judge (optional, `--judge`):** skeptical rubric evaluating
   role fit, factual accuracy, and critical issues. This is a second
   perspective, never the sole source of truth.

### Running evals

```bash
uv run cv-tailor eval                 # deterministic checks only
uv run cv-tailor eval --judge         # add LLM-as-judge
```

Eval auto-runs after each successful `run` and `continue` command.

---

## Concurrency & Caching

### Locks

Three shared resources require serialization:

- `llm._log_lock` — serializes parallel JSONL log writes. The Hiring
  Reviewer and Coach Reviewer run in a `ThreadPoolExecutor(2)` per section;
  without a lock, their concurrent writes to `llm_calls.jsonl` would
  corrupt the log.
- `orchestrator._run_log_lock` — serializes appends to `_run.log` across
  threads for the same reason.
- `clarifications._store_lock` — guards the read-modify-write cycle on
  `clarifications.json`. The write is atomic via `tmp + os.replace`
  (avoids partial-write corruption on crash).

### In-process caches

- `llm._config_cache` — `config.yaml` is parsed once per process, not
  once per LLM call (~20× reduction in file I/O)
- `llm._prompt_cache` / `load_prompt()` — prompt Markdown files are loaded
  once per process. In dev mode (`CV_TAILOR_DEV_RELOAD=1`), mtime is checked
  and the file is reloaded on change, enabling prompt iteration without
  server restart.
- `beleg_index._index_data_cache` / `_compact_cache` — the evidence index
  JSON is parsed once, avoiding up to 12 re-reads per run.

### Anthropic prompt caching

`llm._prepare_messages_for_provider()` wraps the system prompt and static
context in `cache_control: ephemeral` before every Anthropic call. This
instructs the API to store the prefix KV cache across requests in the same
session. The Writer, Factcheck, and Coach calls benefit most because they
share a large static prefix (standard CV + evidence index + style exemplars).

### Rate-limit retries

`call_llm()` retries on `litellm.RateLimitError` up to 3 times with
exponential backoff: 10s → 20s → 40s. Rate-limit attempts are logged with
`status="rate_limit"` in `llm_calls.jsonl` but not in `errors.jsonl`.
Exhausted retries land in `errors.jsonl`.

---

## Web UI

### Design principle: thin orchestration layer

`src/cv_tailor/web.py` (1 138 lines after the index template was extracted
to `src/cv_tailor/web_assets/index.html`) uses the existing pipeline
directly. It introduces no separate CV logic — all intelligence lives in the
agents and deterministic gates described above.

### Security constraints

- **Localhost only.** The server binds to `127.0.0.1` only. No public
  network exposure.
- **Path-traversal guard.** File-serving resolves all paths with `.resolve()`
  and verifies containment under `runs/` before serving.
- **Body cap.** `_read_json` limits request bodies to 2 MB and validates
  `Content-Length` with a `try/except ValueError` and a `cl < 0` check.
- **No HTML file-serving.** Agent-generated files are never served as
  `text/html` to avoid XSS without a CSP header.
- **Atomic state transitions.** `WebState.try_start_continue()` is a CAS
  operation (paused → continuing) to prevent double-submit races.

### Pipeline stepper

`WebJob.phases` tracks the status of each pipeline phase (pending →
running → done / error / skipped). `WebState.set_phase()` updates
individual phases thread-safely. The frontend re-polls every 2.2 seconds
and renders a CSS-animated stepper.

### PDF workflow

PDF rendering is an explicit user action (a button), not auto-triggered.
This lets the operator polish the Markdown output before committing to a
PDF. The button calls `POST /api/runs/<id>/pdf`, which spawns a background
thread running `run_pdf_renderer(ctx)`. The frontend polls until the phase
transitions from `running` to `done`.

---

## What Was Deliberately Not Built

A set of reasonable engineering improvements were evaluated and consciously
deferred:

- **Full Pipeline class with Protocol-based ProgressReporter/PauseHandler.**
  Would unify the CLI and Web paths into a single orchestration abstraction.
  Deferred because the refactor is invasive and risks introducing subtle
  behavior drift between the two paths, which have different pause semantics.

- **Pydantic config validation.** Would catch malformed `config.yaml` at
  startup rather than at the first LLM call. Deferred because no concrete
  bug has been attributed to its absence — adding validation without a
  motivating failure would be premature.

- **Directory reorganisation** (`agents/`, `checks/`, `io/`, `web/`,
  `observability/`). Would clarify the module structure. Deferred because
  every import in the codebase would break, the refactor provides no runtime
  benefit, and the current flat structure is navigable for a single-maintainer
  tool.

This list is intentional. Deferred items are tracked here so that future
maintainers know these options were considered, not overlooked.
