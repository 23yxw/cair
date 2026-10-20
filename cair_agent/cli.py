"""Command line entry points: index | build-db | run | eval | render-trace."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

import typer

from .config import Settings, get_settings
from .data.index import build_index, save_index, summarize

app = typer.Typer(add_completion=False, help="CAIR multimodal diagnosis agent")


def _repo(settings: Settings, backend: str):
    """Open the data backend. The case index (and the SQLite db) are built on first use."""
    from .data.index import ensure_index
    try:
        ensure_index(settings.data_dir, settings.index_path)
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    if backend == "sqlite":
        from .data.sql_repository import SqlCaseRepository
        return SqlCaseRepository.from_sqlite(settings.sqlite_path, index_path=settings.index_path)
    from .data.repository import FileCaseRepository
    return FileCaseRepository.from_path(settings.index_path)


@app.command()
def index(data_dir: Optional[Path] = None, out: Optional[Path] = None, sizes: bool = True):
    """Scan the dataset and write the anonymized case index."""
    s = get_settings()
    idx = build_index(data_dir or s.data_dir, read_sizes=sizes)
    save_index(idx, out or s.index_path)
    typer.echo(json.dumps(summarize(idx), indent=1))
    typer.echo(f"wrote {out or s.index_path}")


@app.command("build-db")
def build_db(out: Optional[Path] = None):
    """Build the SQLite database from the case index."""
    from .data.build_sqlite import build_sqlite
    s = get_settings()
    from .data.index import load_index
    n = build_sqlite(load_index(s.index_path), out or s.sqlite_path)
    typer.echo(f"wrote {out or s.sqlite_path}: {n}")


@app.command()
def run(case: List[str] = typer.Option(..., "--case", "-c", help="case id(s) or 'all'"),
        backend: Optional[str] = None, findings: Optional[str] = None, critic: bool = False,
        out: Optional[Path] = None, max_tool_calls: Optional[int] = None, max_images: Optional[int] = None):
    """Run the agent on one or more cases. Writes runs/<timestamp>/<case>.jsonl, <case>.report.json and summary.json."""
    s = get_settings()
    try:
        s.require_llm()
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2)
    if max_tool_calls:
        s.max_tool_calls = max_tool_calls
    if max_images:
        s.max_images = max_images
    backend = backend or s.backend
    findings = findings or s.findings_policy
    repo = _repo(s, backend)
    from .agent.controller import Controller
    from .llm.client import LLMClient
    out = out or Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    ctrl = Controller(s, repo, LLMClient(s), findings_policy=findings, with_critic=critic, trace_dir=out)
    cases = repo.list_case_ids() if case == ["all"] else case
    rows = []
    for cid in cases:
        typer.echo(f"== {cid}")
        r = ctrl.run(cid)
        row = {"case_id": cid, "diagnosis": r.report.diagnosis, "stop": r.stop_reason, "tool_calls": r.tool_calls,
               "images": r.images_sent, "tokens": r.usage.get("total_tokens"), "elapsed_s": r.elapsed_s,
               "degraded": r.report.degraded, "errors": r.errors, "trace": r.trace_path}
        rows.append(row)
        typer.echo(json.dumps(row, ensure_ascii=False))
        (out / f"{cid}.report.json").write_text(r.report.model_dump_json(indent=1), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    typer.echo(f"artifacts in {out}")


@app.command("render-trace")
def render_trace(path: Path, out: Optional[Path] = None):
    """Render a JSONL trace to Markdown."""
    from .trace.render import render_file
    md = render_file(path)
    if out:
        out.write_text(md, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(md)


@app.command()
def eval(settings_list: str = typer.Option("S1,S2,S3", "--settings"), cases: str = "all", out: Optional[Path] = None,
         judge: bool = True):
    """Run the evaluation matrix (S1 single-pass, S2 agentic, S3 agentic+critic, S4 S3+findings, S5 S3 on sqlite)."""
    from .eval.run_matrix import run_matrix
    s = get_settings()
    run_matrix(s, [x.strip() for x in settings_list.split(",") if x.strip()], cases, out, judge=judge)


@app.command()
def rejudge(out: Path):
    """Re-grade every <out>/S*/metrics.json with the LLM judge (reports are read from <case>.report.json)."""
    from .data.index import ensure_index
    from .eval.judge import judge as judge_fn
    from .llm.client import LLMClient
    from .llm.schemas import DiagnosisReport
    s = get_settings()
    labels = {lab.case_id: lab for lab in ensure_index(s.data_dir, s.index_path).labels}
    llm = LLMClient(s)
    for p in sorted(out.glob("S*/metrics.json")):
        rows = json.loads(p.read_text(encoding="utf-8"))
        for r in rows:
            rep = DiagnosisReport.model_validate_json((p.parent / f"{r['case_id']}.report.json").read_text(encoding="utf-8"))
            lab = labels[r["case_id"]]
            r["reference"] = lab.label
            r.update(judge_fn(llm, rep, lab.label, lab.discussion))
            typer.echo(f"[{p.parent.name}] {r['case_id']}: {r['diagnosis'][:50]} -> {r['top1']} (top3={r['top3_hit']}) {r['reason'][:80]}")
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    summarize(out)


@app.command()
def summarize(out: Path):
    """Rebuild <out>/summary.md from all <out>/S*/metrics.json (useful after parallel eval runs)."""
    from .eval.run_matrix import render_summary
    rows = {p.parent.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("S*/metrics.json"))}
    (out / "summary.md").write_text(render_summary(rows), encoding="utf-8")
    typer.echo((out / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    app()
