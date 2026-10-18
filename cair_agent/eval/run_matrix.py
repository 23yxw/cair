"""Evaluation matrix runner.

S1  single-pass multimodal (anchor images, no tools)          findings hidden
S2  agentic evidence seeking (tool loop, no critic)           findings hidden
S3  agentic + critic + finalizer  (main line)                 findings hidden
S4  S3 with findings visible (reference upper bound)
S5  S3 on the SQLite backend (backend consistency)

Writes runs/<ts>/<setting>/<case>.jsonl, per-case report.json, metrics.json and summary.md.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import typer

from ..agent.controller import Controller
from ..config import Settings
from ..data.index import load_index
from ..data.repository import FileCaseRepository
from ..llm.client import LLMClient
from .judge import judge as judge_fn
from .metrics import cost_metrics, evidence_metrics, leak_check
from .single_pass import run_single_pass

SETTINGS = {
    "S1": {"mode": "single", "findings": "hidden", "critic": False, "backend": "file"},
    "S2": {"mode": "agent", "findings": "hidden", "critic": False, "backend": "file"},
    "S3": {"mode": "agent", "findings": "hidden", "critic": True, "backend": "file"},
    "S4": {"mode": "agent", "findings": "visible", "critic": True, "backend": "file"},
    "S5": {"mode": "agent", "findings": "hidden", "critic": True, "backend": "sqlite"},
}


def _repo(settings: Settings, backend: str):
    if backend == "sqlite":
        from ..data.sql_repository import SqlCaseRepository
        return SqlCaseRepository.from_sqlite(settings.sqlite_path)
    return FileCaseRepository.from_path(settings.index_path)


def run_matrix(settings: Settings, which: List[str], cases: str, out: Optional[Path], judge: bool = True) -> Path:
    out = out or Path("runs") / f"eval-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    index = load_index(settings.index_path)
    labels = {lab.case_id: lab for lab in index.labels}
    folder_names = [d.name for d in Path(index.data_dir).iterdir() if d.is_dir()]
    discussions = [lab.discussion for lab in index.labels]
    llm = LLMClient(settings)
    all_rows: Dict[str, List[dict]] = {}

    for name in which:
        cfg = SETTINGS[name]
        repo = _repo(settings, cfg["backend"])
        case_ids = repo.list_case_ids() if cases == "all" else [c.strip() for c in cases.split(",")]
        sdir = out / name
        sdir.mkdir(exist_ok=True)
        ctrl = None
        if cfg["mode"] == "agent":
            ctrl = Controller(settings, repo, llm, findings_policy=cfg["findings"], with_critic=cfg["critic"], trace_dir=sdir)
        rows = []
        for cid in case_ids:
            typer.echo(f"[{name}] {cid}")
            r = run_single_pass(settings, repo, llm, cid, sdir) if cfg["mode"] == "single" else ctrl.run(cid)  # type: ignore[union-attr]
            from ..trace.recorder import read_trace
            events = read_trace(Path(r.trace_path))
            row = {"setting": name, "case_id": cid, "diagnosis": r.report.diagnosis,
                   "differentials": [d.name for d in r.report.differential_diagnoses], "degraded": r.report.degraded,
                   **evidence_metrics(r.report, events, repo, cid), **cost_metrics(events),
                   **leak_check(Path(r.trace_path), folder_names, discussions),
                   "critic_issues": len(r.critic.issues) if r.critic else None,
                   "critic_decision": r.critic.decision if r.critic else None}
            if judge:
                lab = labels[cid]
                row["reference"] = lab.label
                row.update(judge_fn(llm, r.report, lab.label, lab.discussion))
            rows.append(row)
            (sdir / f"{cid}.report.json").write_text(r.report.model_dump_json(indent=1), encoding="utf-8")
            typer.echo(json.dumps({k: row[k] for k in ("diagnosis", "top1", "top3_hit", "tool_calls", "images_sent", "total_tokens") if k in row}, ensure_ascii=False))
        (sdir / "metrics.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        all_rows[name] = rows
        repo.close()

    (out / "summary.md").write_text(render_summary(all_rows), encoding="utf-8")
    typer.echo(f"summary: {out / 'summary.md'}")
    return out


def render_summary(all_rows: Dict[str, List[dict]]) -> str:
    lines = ["# Evaluation summary", ""]
    lines.append("| setting | n | exact | compatible | wrong | top3 hit | unsupported claims | primary study cov. | mean tool calls | mean images | mean tokens | mean s | degraded | leak-free |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, rows in all_rows.items():
        n = len(rows) or 1
        c = Counter(r.get("top1", "unjudged") for r in rows)
        mean = lambda k: sum(float(r.get(k) or 0) for r in rows) / n  # noqa: E731
        lines.append(f"| {name} | {len(rows)} | {c['exact']} | {c['compatible']} | {c['wrong']} | {sum(1 for r in rows if r.get('top3_hit'))} | "
                     f"{sum(r['unsupported_claims'] for r in rows)} | {mean('primary_study_coverage'):.2f} | {mean('tool_calls'):.1f} | "
                     f"{mean('images_sent'):.1f} | {mean('total_tokens'):.0f} | {mean('elapsed_s'):.0f} | {sum(1 for r in rows if r['degraded'])} | "
                     f"{sum(1 for r in rows if r['leak_free'])}/{len(rows)} |")
    lines.append("")
    for name, rows in all_rows.items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append("| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |")
        lines.append("|---|---|---|---|---|---:|---:|---:|---|---|")
        for r in rows:
            lines.append(f"| {r['case_id']} | {r.get('reference', '')} | {r['diagnosis']} | {r.get('top1', '')} | {'y' if r.get('top3_hit') else 'n'} | "
                         f"{r['tool_calls']} | {r['images_sent']} | {r['total_tokens']} | {r['stop_reason']} | "
                         f"{r.get('critic_decision') or ''}{'/' + str(r['critic_issues']) if r.get('critic_issues') is not None else ''} |")
        lines.append("")
    return "\n".join(lines)
