# CAIR multimodal diagnosis agent

A VLM-driven agent that diagnoses a patient from admission text and multi-modality imaging studies
(CT, MRI, X-ray, mammography, ultrasound, fluoroscopy, nuclear medicine, pathology, photographs,
annotated images). Design: **one VLM controller + deterministic imaging tools + an evidence ledger +
critic/finalizer**, with a pluggable data layer (local files or read-only SQL).

Research/interview project only. Not a medical device; not for clinical use.

## Architecture in one paragraph

The controller (`cair_agent/agent/controller.py`) runs a native function-calling loop against the
VLM. Tools (`agent/tools.py`) are the only way case text or images enter the context: the agent builds
a *case map* (`get_case_profile`, `list_studies`, `list_series`), records ranked hypotheses
(`update_hypotheses`), then samples images with explicit strategies (`sample_images` with
`anchors | uniform (contact sheet) | neighbors`, plus `zoom_image`, `compare_images`). Every
observation must be written to the **evidence ledger** (`record_evidence`) citing ids that a tool
actually returned; `finish` is rejected if any key finding cites something the agent never saw. With
`--critic`, a text-only **Critic** reviews the ledger for contradictions/unsupported claims/missed
studies and may trigger one bounded revisit, then a **Finalizer** writes the schema-validated report.
Every step is written to a JSONL trace that renders to Markdown (`render-trace`).

```
VLM controller  --tool calls-->  ToolRegistry --> CaseRepository (File | SQLite)
      ^                              |----------> Imaging toolkit (sampler / zoom / contact sheet)
      |                              '----------> EvidenceLedger (seen-set + claims)
      '--- observations + images ----'
finish --> Critic (ledger only) --> optional revisit --> Finalizer --> DiagnosisReport JSON
                              all events --> runs/<ts>/<case>.jsonl --> render-trace
```

## Setup

```bash
conda create -n cair python=3.11 -y && conda activate cair
pip install -r requirements.txt && pip install -e .
cp .env.example .env            # fill OPENAI_BASE_URL / OPENAI_API_KEY / CAIR_MODEL
tar xzf uniform10.tar.gz        # -> data_subset10/ (or set CAIR_DATA_DIR)
```

## Run

```bash
python -m cair_agent.cli index                      # scan dataset -> runs/case_index.json (anonymized)
python -m cair_agent.cli build-db                   # optional: runs/cair.sqlite
python scripts/smoke_vlm.py                         # endpoint check: text / image / tools

python -m cair_agent.cli run -c case-01             # agentic, no critic (S2)
python -m cair_agent.cli run -c all --critic        # agentic + critic + finalizer (S3)
python -m cair_agent.cli run -c case-03 --backend sqlite --findings hidden --critic
python -m cair_agent.cli render-trace runs/<ts>/case-01.jsonl --out docs/example_trace.md

python -m cair_agent.cli eval --settings S1,S2,S3,S4,S5     # full matrix + LLM judge -> runs/eval-<ts>/summary.md
pytest -q                                                    # unit + backend-consistency tests
```

Budgets (`.env`): `CAIR_MAX_TOOL_CALLS` (default 14; `record_evidence`, `update_hypotheses`, `finish` are free),
`CAIR_MAX_IMAGES` (24), per-call and per-case timeouts.

## Data exposure policy (what the agent can and cannot see)

| Material | Agent access | Notes |
|---|---|---|
| `patient_data.txt`, `presentation.txt` | yes, via `get_case_profile` | empty fields are reported as `missing` |
| study / series structure (`series.json`) | yes, via `list_studies` / `list_series` | modality, captions, counts, anchor slice |
| images | yes, only through imaging tools | referenced as `img:<image_id>`; **file paths are never shown** |
| `findings.md` (radiology report) | **configurable**: `--findings hidden` (default) or `visible` | hidden in the main evaluation because several reports name the diagnosis |
| `discussion.txt` (ground truth) | **never** | not reachable through `CaseRepository`; read only by `eval/` |
| case folder names (= diagnosis labels) | **never** | cases are renamed `case-01..10`; traces are checked for leaks |
| `image_urls.txt` | not used | offline operation; images sent as base64 |

Endpoint URL and API key are read from environment variables only; `.env` is git-ignored and
never appears in traces.

## SQL backend

`SqlCaseRepository` (`cair_agent/data/sql_repository.py`) implements the same interface as the
file backend over SQLAlchemy Core: fixed parameterized query templates, `LIMIT` on every query,
read-only connection (`mode=ro` for SQLite; use a read-only role for Postgres/MySQL via
`SqlCaseRepository.from_url`). Every query is appended to an audit list that the controller writes
to the trace as `sql_query` events. `tests/test_sql_backend.py` asserts that both backends return
identical case facts field by field and that writes are rejected.

## Repository layout

```
cair_agent/
  config.py              env + budgets
  data/                  index.py (scan + anonymize), models.py, repository.py (File), sql_repository.py, schema.sql, build_sqlite.py
  imaging/               sampler.py (anchors/uniform/neighbors), ops.py (resize/zoom/contact sheet), encode.py
  llm/                   client.py (openai wrapper, retries, usage), schemas.py (pydantic contracts)
  agent/                 tools.py (registry + expert cards), ledger.py, controller.py, critic.py, finalizer.py, prompts/*.md (versioned)
  trace/                 recorder.py (JSONL), render.py (Markdown)
  eval/                  single_pass.py (S1), run_matrix.py (S1-S5), judge.py, metrics.py
  cli.py                 index | build-db | run | eval | render-trace
tests/                   pytest (index, imaging, ledger/schemas, SQL consistency)
scripts/smoke_vlm.py     endpoint smoke test
docs/                    one_pager.md, example_trace.md
```

## Results and deliverables

- `docs/one_pager.md` — one-page write-up (design, difficulties, results, failure analysis)
- `docs/example_trace.md` — full rendered run trace (case-07, S3: X-ray + 5-series MRI, critic, finalizer)
- `docs/failure_trace_case-01.md` — a rendered failure case (appendiceal intussusception read as appendicitis)
- `docs/results/eval_summary.md` + `S*_metrics.json` — the S1–S5 matrix (10 cases each, strict LLM judge)
- `docs/results/traces/` — all S3 JSONL traces and final reports

| setting | exact | compatible | wrong | top-3 | unsupported claims | degraded | leak-free |
|---|---:|---:|---:|---:|---:|---:|---:|
| S1 single pass | 5 | 0 | 5 | 7 | 0 | 0 | 10/10 |
| S2 agentic | 6 | 0 | 4 | 6 | 0 | 0 | 10/10 |
| S3 agentic + critic | 6 | 0 | 4 | 7 | 0 | 0 | 10/10 |
| S4 S3 + report visible | 7 | 1 | 2 | 9 | 0 | 0 | 10/10 |
| S5 S3 on SQLite | 6 | 1 | 3 | 6 | 0 | 0 | 10/10 |

## Known limitations

- Single VLM (`Qwen3.6-35B-A3B` on the interview endpoint); no specialist models. Fine-grained
  perception (e.g. a lead-point mass inside an intussuscepted appendix) is the main failure mode.
- Slice selection is heuristic (anchor / uniform / neighbors); no 3D reasoning or registration.
- No DICOM; JPEG/PNG exports only. No timestamps, so study order is a proxy for time.
- Ten cases: all numbers are indicative, not statistically meaningful.
