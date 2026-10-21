# Agentic CV Tailoring

![Status](https://img.shields.io/badge/status-production--grade-2ea44f?style=flat)
![Tests](https://img.shields.io/badge/tests-350%2B%20pytest-informational?style=flat)
![License](https://img.shields.io/badge/license-MIT-lightgrey?style=flat)

A pipeline that presents documented, verifiable experience in the framing that fits a specific role, without adding or embellishing anything.

> **For anyone reading this while reviewing an application:** every claim in
> any CV produced with this system is grounded in pre-indexed source
> documents (employer references, certificates, work samples). A dedicated
> factcheck step vetoes any claim it cannot trace to these documents. See
> [Factual fidelity](#factual-fidelity-what-the-pipeline-cannot-do) below.

<!-- HERO-VISUAL — bitte hier ein Bild einfügen (stärkster visueller Hebel):
     Screenshot der lokalen Web-UI mit der Cost-Tracking-Statusbar sichtbar
     ($-Betrag · Anzahl Calls · Cache-Hit-Rate · Per-Agent-Breakdown).
     1. Bild ablegen unter  docs/screenshot-web-ui.png  (im privaten Repo;
        Pfad zur Sync-Whitelist in scripts/sync_to_public.py hinzufügen).
     2. Folgende Zeile einkommentieren:
![cv-tailor Web-UI mit Cost-Tracking](docs/screenshot-web-ui.png)
-->

---

## The problem this solves

A strong candidate has years of varied, documented experience. Any given job
posting activates a different subset of that experience. Rewriting a CV by hand for every application, in a consistent voice, at the right level of detail and without drifting away from what is actually documented, takes hours and invites mistakes.

This pipeline automates the *presentation* layer: which experiences to
emphasise, how to frame them for the role, how to maintain consistent
phrasing. It does not automate the *substance* layer: the experience itself, the proof documents and the strategic decisions remain the candidate's own work.

---

## Factual fidelity: what the pipeline cannot do

Before any CV section is written, a bootstrap phase indexes all source
documents into an evidence store:

```
data/standard_cv.md          ← comprehensive base CV, authored by hand
data/zeugnisse/               ← employer references, certificates, PDFs
data/beleg_index.json         ← generated evidence index (one entry per claim,
                                 each with: text snippet, source file, position)
```

Every claim the Writer agent produces is checked against this index by a
dedicated Factcheck agent. Claims that cannot be traced to a source document
trigger a veto and block the output. Experience that is not in the indexed source documents has no way into the CV.

---

## What you have to bring

The pipeline is only as good as what goes into it. Running it requires
substantial preparation that cannot be automated:

**1. A complete, honest standard CV**
The base CV must document all relevant experience in full: concrete
achievements, specific numbers, team sizes, technologies, timelines. Vague
entries produce vague output. The standard CV is the full record every application draws from. Preparing it is significant work.

**2. Supporting reference documents**
Employer references (Arbeitszeugnisse), certificates, and other proof
documents are indexed at bootstrap time. The more complete this document
set, the more evidence is available for the factcheck layer to verify claims
against.

**3. Answers to clarification questions**
After the Analyst reviews the job posting, it may pause the pipeline to ask
specific questions: "The posting mentions X. Can you confirm whether this applies to your experience at Y?" These require explicit written answers
before the pipeline continues. There is no shortcut around this step.

**4. A profile-fit decision**
If the Analyst identifies a genuine mismatch between the posting's
requirements and the documented experience, the pipeline first turns the
gaps into clarification questions, because documented evidence is a lossy compression of a career and many "gaps" can be answered. Whatever remains
unanswered stays in a structured gap report, and the pipeline asks whether
to continue. This gate exists to
prevent the pipeline from producing a CV that overstretches the candidate's
documented profile. The decision to continue or stop is the candidate's.

**5. A review pass**
The output is a starting point for the final version, not a finished
document. The candidate reviews the result, edits the Markdown directly,
and the PDF is regenerated from the edited version. The pipeline produces a
draft; the candidate owns the submission.

---

## What the pipeline actually does

Given the source documents, the job posting, and the candidate's answers to
any clarification questions, the pipeline:

1. Analyses the posting and maps it against the evidence index
2. Checks that the candidate's profile is a credible match (or surfaces gaps)
3. Drafts each CV section (Management Summary → Key Competencies →
   Work History), grounded strictly in the indexed evidence
4. Has two independent reviewers critique each draft (one simulating a
   Hiring Manager perspective, one a career coach perspective)
5. Checks every draft for factual drift, verbatim header consistency, bullet
   length, and summary word count, and rewrites if any check fails
6. Scans the assembled CV for verbatim repetition between sections and
   reports it for human review (no silent rewrite)
7. Produces a diff table showing every change from the base CV with a
   one-line reason
8. Optionally translates the result if the posting is in English

At no step does the pipeline introduce claims that aren't in the source
documents. The output is a *selection and phrasing* of existing evidence,
not a generation of new content.

---

## Process and human touchpoints

```mermaid
flowchart TD
    YOU1["① You: prepare standard CV\n+ reference documents"]
    Bootstrap["Bootstrap: build evidence index\nfrom your real documents"]
    YOU1 --> Bootstrap

    Posting["② You: provide job posting"]
    Bootstrap --> Analyst
    Posting --> Analyst["Analyst: maps posting to\nyour evidence index"]

    Analyst --> ProfileFit{"Profile Fit Gate:\ngenuine match?"}
    ProfileFit -- "gaps found" --> YOU2["③ You: answer gap questions,\ndecide whether to continue"]
    YOU2 -- continue --> Factcheck1
    ProfileFit -- pass --> Factcheck1["Factcheck v1: scan for\nclarification needs"]

    Factcheck1 --> Clarify{"Open questions?"}
    Clarify -- yes --> YOU3["④ You: answer\nclarification questions"]
    YOU3 --> WriterLoop
    Clarify -- no --> WriterLoop

    subgraph WriterLoop["Writer loop: per section, grounded in evidence"]
        Writer["Writer: drafts section\nusing only indexed evidence"]
        Writer --> Reviewers["Dual review:\nHiring Manager + Career Coach"]
        Reviewers --> Gates["Deterministic gates:\nconsistency · length · factcheck"]
        Gates -- "veto" --> Writer
        Gates -- "clean" --> Next["Next section"]
    end

    WriterLoop --> Trim["Writer Trim: fit to page\n+ redundancy report"]
    Trim --> Diff["Diff table: what changed\nand why"]
    Diff --> Post["Keyword Marker · Translator\n(EN postings)"]
    Post --> PDF["PDF draft"]
    PDF --> YOU4["⑤ You: review and edit\nin the web UI"]
    YOU4 --> PDF2["PDF re-rendered from\nyour edited version"]
    PDF2 --> Submit["Final CV ready for submission"]
```

---

## Engineering notes

For readers interested in the implementation:

- **Multi-agent orchestration via LiteLLM.** Ten specialised agents
  (Analyst, Writer, Writer Trim, Hiring Manager Reviewer, Coach Reviewer,
  Factcheck, Diff, Keyword Marker, Translator, Naturalisation) are routed
  through LiteLLM, which abstracts provider differences behind a uniform
  API. Switching a model is a one-line change in `config.yaml`.

- **Cross-provider review as anti-confirmation-bias.** The Hiring Manager
  Reviewer and Translator run on OpenAI (`gpt-4.1`, `gpt-4.1-mini`), as
  does the eval judge; Analyst, Writer, Coach, Factcheck, Diff, Keyword
  Marker and Naturalisation run on Anthropic (`claude-sonnet-4-6`,
  `claude-haiku-4-5`). One provider cannot echo the other into agreement.

- **Prompt caching for cost efficiency.** Static context per agent call
  (standard CV, evidence index, style exemplars, analysis output) is sent
  as an Anthropic `cache_control: ephemeral` prefix. The dynamic part
  (current section draft, reviewer feedback) is the uncached suffix.
  The web UI shows the resulting cache hit rate per run.

- **Parallel reviewers with threading locks.** Hiring Manager Reviewer and
  Coach Reviewer run concurrently in a `ThreadPoolExecutor`. Shared
  mutable state (JSONL log files, run-log appends, clarification store) is
  protected by per-resource threading locks with atomic `tmp + os.replace`
  writes.

- **Deterministic quality gates for structural rules.**
  Profile-fit check, consistency check (verbatim job-history headers),
  length check (22-word bullets, 120–160-word summaries, max 6 competency
  entries), cross-section redundancy check (shared 8-word n-grams between
  Summary and other sections, report-only), and factcheck parse structured
  validator results. LLM reviewer signals only trigger a
  rewrite round if a named veto marker is present in the output; generic "could be improved" commentary is advisory only.

- **Topic-gated clarification memory against cross-claim fusion.** Saved
  answers are persisted with a 12-topic keyword taxonomy (e.g. `ml_ai`,
  `domain_health`, `subscription_saas`) and replayed into prompts only
  when the current posting activates the same topics. Prevents a
  healthcare-specific answer from leaking into an unrelated role's framing.

- **Demonstration-based style transfer.** Two manually-polished CV
  examples sit as a `cache_control: ephemeral` prefix in the Writer's
  context. Style is transferred by example, not by prose rules. A
  self-match filter prevents an exemplar from being used as a reference
  for the very role it was written for.

- **Eval suite for regression testing across runs.** `evals/run.py` runs
  deterministic checks on saved run artefacts: forbidden phrases in the final CV, vocabulary coverage, length drift and diff granularity. An optional LLM-as-judge with a skeptical rubric adds
  a qualitative second perspective. Eval cases are versioned YAML files in
  `evals/cases/`; `cv-tailor quality-trend` surfaces regressions by
  comparing the latest run snapshot against the trailing-5 median across
  metrics like writer-round-2 count, factcheck veto rate, cache hit rate,
  and cliché density (substring-matched against a curated word list;
  structural section-header terms are explicitly excluded to keep the
  baseline stable across roles).

- **Observability.** Every LLM call is logged to `logs/YYYY-MM/llm_calls.jsonl`
  with timestamp, agent, provider, model, phase, token counts (including
  `cache_read_input_tokens`), cost, duration, and status. Errors land in a
  separate `errors.jsonl` with stack traces. The web UI exposes a per-run
  cost breakdown and cache hit rate in real time.

- **Prompts as versioned code.** All agent prompts live in `prompts/*.md`,
  tracked in git. Prompt changes are visible as diffs; regressions can be
  bisected by commit. A `CV_TAILOR_DEV_RELOAD=1` flag hot-reloads prompt
  files on each call without restarting the server, enabling rapid
  iteration in the web UI.

- **Test suite for the deterministic layer.** 350+ pytest tests cover language
  detection, topic classifier, consistency checker, length checker, cost
  tracking, factcheck parser, keyword marker, PDF helpers, profile-fit
  parser, prompt-cache layout, and quality snapshot (including bold distribution).

Full architecture decisions are documented in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## Running the demo

The repo ships with a fictional demo persona ("Alex Müller") so the pipeline
can be run end-to-end without any real personal data.

```bash
uv sync
python scripts/init_persona.py --overwrite-real   # generate demo CV files
cp .env.example .env                              # add API keys
uv run cv-tailor bootstrap                        # build evidence index
uv run cv-tailor run stellenanzeigen/demo/posting_de.md
```

If a run stops mid-pipeline (e.g. a model call fails after the Writer loop
has completed), resume the post-processing steps without re-running the
Writer:

```bash
uv run cv-tailor post-process <run_id>   # diff → keyword marker → translator → PDF
```

> **Note:** For PDF output set `CV_TAILOR_TEMPLATE=data/demo_template.html`;
> the author's own template is not included. `CV_TAILOR_SKIP_PDF=1` skips
> PDF rendering.

---

## Repository layout

```
src/cv_tailor/         — Python source (orchestrator + 10 agents)
prompts/               — Versioned Markdown prompts per agent
data/                  — Persona, demo CV, evidence inputs, demo template
stellenanzeigen/demo/  — Demo job postings (DE + EN)
evals/                 — Eval cases + deterministic quality runner
tests/                 — Pytest suite for the deterministic layer (350+ tests)
```

---

## License

MIT. Personal data shown in this repo is fictional. "Alex Müller" is not a
real person; the demo employers are not real companies.
