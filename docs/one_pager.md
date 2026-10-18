# A multimodal diagnosis agent with an evidence ledger — one-page summary

**Task.** From admission text and 1–3 imaging studies (CT/MRI stacks up to 679 slices, X-ray, mammography,
ultrasound, fluoroscopy, bone scan, pathology, photographs, annotated images) produce a diagnosis with
differentials, every key finding bound to the exact image or text it came from. VLM only
(`Qwen3.6-35B-A3B`, OpenAI-compatible endpoint), no training, 10 cases, 7 days. Code: `cair_agent/`;
run instructions in `README.md`; example trace appended after this page.

**Design choice.** I surveyed ClinSeekAgent, MedRAX, MedOpenClaw/GAZE, CT-Agent, Radiologist Copilot,
MAM/MDAgents and VILA-M3 and kept four ideas: *active evidence seeking* over a data layer instead of one
giant prompt (ClinSeekAgent); *the VLM routes deterministic tools* rather than reading every pixel itself
(MedRAX); *every claim carries a reference to what was actually seen* (MedOpenClaw/GAZE); *a separate
consistency check before finalizing* (Radiologist Copilot). I deliberately did **not** build a multi-role
committee (cost and error copying on 10 cases) or deploy specialist segmentation models (environment risk
in 7 days). Result: one controller loop with 10 tools (`get_case_profile, list_studies, list_series,
search_series, sample_images[anchors|uniform sheet|neighbors], zoom_image, compare_images,
update_hypotheses, record_evidence, finish`), an **evidence ledger** that rejects any citation the tools
never returned, a text-only **critic** that may trigger one bounded revisit, and a **finalizer** that emits
a schema-validated report. Data access goes through one `CaseRepository` with a file backend and a
**read-only SQL backend** (SQLite; SQLAlchemy URL for Postgres/MySQL) using fixed parameterized templates
audited into the trace; a test proves both backends return identical facts field by field.

**Data boundaries.** Case folder names *are* the labels and several `findings.md` state the diagnosis.
Cases are renamed `case-01..10`, paths never shown (images are `img:<id>`), `discussion.txt` unreachable
from the repository, `findings.md` behind a switch (hidden in the main line). Every trace is checked for
folder names and discussion sentences: 50/50 leak-free.

**What went wrong and what I learned.** (1) Native `tools=` worked on day 1; the JSON fallback stayed
dormant. (2) 679-slice CT: `anchors → labelled contact sheet → neighbors`; the model names the slice it
wants next from the burned-in numbers and confirms on a second plane unprompted. (3) *Format friction*:
the model echoed display keys (`"img:123"`) and sent objects where strings were expected — 2 wasted
rounds/case until schemas accepted both. (4) *Invisible reasoning*: tool calls came with empty text, so a
free `update_hypotheses` tool was added; the trace now shows belief updates and the agent started
reviewing secondary studies before finishing. (5) *Thinking model*: the finalizer returned empty strings
at exactly `max_tokens` (all spent on hidden reasoning); `enable_thinking=false` for formatting steps and
larger budgets for reasoning steps cut a case from 217 s to 78 s and removed all degraded reports.
(6) *Budget bypass*: one response can carry several tool calls (21 of 14 allowed) → per-call enforcement.
(7) *Judge*: without reasoning it graded inconsistently; with reasoning it is consistent but strict
(appendicitis for appendiceal intussusception = wrong). I keep the strict grades and add a manual column.

**Results** (10 cases, strict LLM judge; `docs/results/eval_summary.md`):

| setting | exact | compat. | wrong | top-3 hit | unsupported claims | degraded | tool calls | images | tokens | s/case |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S1 single pass (anchor images) | 5 | 0 | 5 | 7 | 0 | 0 | 0 | 7.2 | 14k | 160 |
| S2 agentic, no critic | 6 | 0 | 4 | 6 | 0 | 0 | 11.0 | 8.3 | 77k | 85 |
| **S3 agentic + critic (main)** | **6** | 0 | 4 | 7 | 0 | 0 | 10.3 | 6.6 | 81k | 135 |
| S4 S3 + report visible | 7 | 1 | 2 | 9 | 0 | 0 | 11.6 | 5.4 | 75k | 134 |
| S5 S3 on SQLite backend | 6 | 1 | 3 | 6 | 0 | 0 | 11.0 | 9.7 | 90k | 141 |

Manual review of S3: 6 exact, 2 partially right (case-01 appendicitis without the intussusception;
case-03 "no bone metastasis" — the correct clinical conclusion — without naming the osteophyte), 2 wrong
(case-08 complex congenital GU anomaly with empty admission text; case-10 post-fusion spine read as
transitional vertebra). Honest reading: tools help on cases the single pass misreads (patellar tendon
rupture, multi-vessel CAD) and cost 5x tokens; the critic found nothing to revisit in S3 (0/10) and could
not repair perception errors; S3 vs S5 agree on 8/10 grades with identical data, so run-to-run variance is
about the size of the effects measured. Primary-study coverage was 1.0 and unsupported claims 0 in every
setting — the ledger does what it was built for.

**Failure analysis.** All remaining errors are perception, not process: a lead-point mass inside an
intussuscepted appendix read as plain appendicitis (`docs/failure_trace_case-01.md`); a false-positive bone
scan read as metastasis in S1/S2 (osteophyte visible on the CT bone window); patent urachus missed while the
associated epispadias was found. Next steps that would change this: organ localisation and vertebral level
counting tools feeding the same ledger, and a critic that can request images rather than text only.

**Limits.** 10 cases, one model, heuristic slice selection, no DICOM/3D, no timestamps, research use only.
