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
from ..data.index import ensure_index
from ..data.repository import FileCaseRepository
from ..llm.client import LLMClient
from .judge import judge as judge_fn, withhold_incompatible
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
        return SqlCaseRepository.from_sqlite(settings.sqlite_path, index_path=settings.index_path)
    return FileCaseRepository.from_path(settings.index_path)


def run_matrix(settings: Settings, which: List[str], cases: str, out: Optional[Path], judge: bool = True) -> Path:
    out = out or Path("runs") / f"eval-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    index = ensure_index(settings.data_dir, settings.index_path)
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
                if row.get("top1") == "wrong":
                    # After grading. The summary keeps the submitted diagnosis; the saved report does not.
                    withhold_incompatible(r.report)
                    row["degraded"] = True
            rows.append(row)
            (sdir / f"{cid}.report.json").write_text(r.report.model_dump_json(indent=1), encoding="utf-8")
            typer.echo(json.dumps({k: row[k] for k in ("diagnosis", "top1", "top3_hit", "tool_calls", "images_sent", "total_tokens") if k in row}, ensure_ascii=False))
        (sdir / "metrics.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        all_rows[name] = rows
        repo.close()

    (out / "summary.md").write_text(render_summary(all_rows), encoding="utf-8")
    typer.echo(f"summary: {out / 'summary.md'}")
    return out


# Kept in front of every generated summary so `summarize` does not drop the protocol.
SUMMARY_PREAMBLE = """
## 评测设计

同一批 10 个匿名病例（`case-01`..`case-10`）、同一个模型（`Qwen3.6-35B-A3B`，`temperature=0`，`seed=7`）。每次只改一个条件，用来分开看：主动取证有没有用、Critic 有没有用、放射报告会把答案抬高多少、换 SQL 后端会不会改结果。

推理阶段一律看不到 `discussion.txt`，也看不到病例文件夹名。`findings.md` 只在 S4 开放。评分发生在该次运行结束之后：另一次 LLM 调用阅读目录名派生的参考诊断和 `discussion.txt`，给 `exact` / `compatible` / `wrong`。Agent 自己的轨迹里不能出现这些标签；`leak-free` 检查的就是这件事。

| 设置 | 运行方式 | 放射报告 | 数据后端 | 要回答的问题 |
|---|---|---|---|---|
| S1 | 一次调用。入院文本 + 每个序列的代表切片（短序列整组放入，长序列只放 anchor），没有工具、没有账本 | 隐藏 | 文件 | 不做 Agent 时，模型一次性能到什么程度 |
| S2 | 完整工具循环：病例地图、自行采样、证据账本、`finish`。没有 Critic / Finalizer | 隐藏 | 文件 | 主动选择检查和切片，比一次看完强多少 |
| S3 | S2 + Critic（代码审计；通过后盲读最多 4 张全分辨率图并二选一；最多一轮有限复查）+ Finalizer。主线系统 | 隐藏 | 文件 | 定稿前的检查能不能改变诊断 |
| S4 | 与 S3 相同，并注册 `read_study_findings` | 可见 | 文件 | 已有放射报告时的上限。多份报告直接写出诊断，不能与 S1–S3 公平比准确率 |
| S5 | 与 S3 相同 | 隐藏 | 只读 SQLite | 同一套工具换数据源后，病例事实和诊断是否还一致 |

每例默认预算：最多 14 次计费工具调用（`get_case_profile`、`list_studies`、`list_series`、`record_evidence`、`update_hypotheses`、`finish` 不计），最多 24 张图，单次 LLM 超时 120 秒，单例超时 15 分钟。

## 评测基线

**基线是 S1。** 后面的设置都和它比，而不是和某个外部数据集或公开榜比。

S1 把材料一次交完：入院文本，加上代码预先选定的图像。序列不超过 6 张时全部放入；CT/MRI 等长序列只放 `series.json` 里的 anchor 切片。图像仍标成 `img:<id>`，所以「无支持声明」对 S1 同样可算。它没有工具循环，`calls` 恒为 0，`stop` 为 `single_pass`。

读表时：

- S2 − S1：主动取证带来的诊断变化，以及多出来的 token 和工具调用。
- S3 − S2：Critic 和 Finalizer 带来的变化。本次 S3 的 exact 与 S2 相同，Critic 在 10 例里都选择直接定稿。
- S4 不是更强的基线，而是「报告可见」的上界。
- S5 − S3：后端差异。两边病例事实由单元测试保证逐字段相同；诊断分档不完全相同，来自同模型重复运行的波动。

## 指标字段

汇总表是 10 例的合计或平均。分项表是单例。

**汇总表**

| 字段 | 含义 |
|---|---|
| setting | S1–S5，定义见上 |
| n | 病例数，这里恒为 10 |
| exact | 主诊断与参考疾病实体相同的例数。同义词、缩写、更具体且正确的亚型算 exact |
| compatible | 大类或机制正确，但没点出参考实体的例数。例如「无骨转移」对上「骨赘假阳性骨扫描」可以落在这一档，由评分模型判定 |
| wrong | 另一种疾病的例数。三档之和等于 n |
| top3 hit | 参考诊断出现在主诊断或列出的鉴别诊断中的例数。主诊断已是 exact 时记为命中 |
| unsupported claims | 关键发现里，引用了本次运行没有看过的 id 的条数，10 例相加。0 表示账本拒绝了未见引用 |
| sheet-only direct | 账本里 support=direct、且引用的图像只出现在联系表上的条数，10 例相加 |
| full-res series cov. | 每个病例「至少有一张全分辨率图的序列数 / list_series 列出的序列数」，再对病例取平均。没有 list_series 的运行（S1）记为 1.00 |
| primary study cov. | 每个病例「取过图的 primary 检查数 / 该病例 primary 检查数」，再对 10 例取平均。1.00 表示每个原始检查至少看过一张图 |
| mean tool calls | 计费工具调用次数的平均。S1 没有工具，为 0 |
| mean images | 发给模型的图像份数的平均。一张联系表算 1 份 |
| mean tokens | prompt 与 completion token 之和的平均，含该次诊断里的 Critic 与 Finalizer，不含事后评分调用 |
| mean s | 单例墙钟时间的平均（秒） |
| degraded | 模板报告、Critic 未能确认、或评分判定与参考诊断不符因而降级的例数。汇总表里的 diagnosis 仍是模型提交的原文 |
| leak-free | 轨迹中不含原始文件夹名、也不含 `discussion.txt` 里长度超过 40 字符的句子的例数，写成 `通过数/n` |

**分项表**

| 字段 | 含义 |
|---|---|
| case | 匿名病例 id。编号按数据目录名排序，不是诊断 |
| reference | 由文件夹名去掉末尾数字得到的参考诊断，只给评分用 |
| diagnosis | Agent 的主诊断原文 |
| grade | 该例的 exact / compatible / wrong |
| top3 | 参考诊断是否出现在主诊断或鉴别诊断中：y / n |
| calls / imgs / tokens | 该例的计费工具次数、图像份数、诊断过程 token |
| stop | 结束原因。`single_pass` 仅 S1；`finish` 为正常提交；`finish_after_revisit` 为 Critic 要求复查后再次提交 |
| critic | 只在 S3–S5 有值，格式为 `决策/问题条数`。`finalize/0` 表示没有问题并直接定稿；`revisit/2` 表示提出 2 条问题并要求再看 |
""".strip()


def render_summary(all_rows: Dict[str, List[dict]]) -> str:
    lines = ["# Evaluation summary", "", SUMMARY_PREAMBLE, ""]
    lines.append("| setting | n | exact | compatible | wrong | top3 hit | unsupported claims | sheet-only direct | full-res series cov. | primary study cov. | mean tool calls | mean images | mean tokens | mean s | degraded | leak-free |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, rows in all_rows.items():
        n = len(rows) or 1
        c = Counter(r.get("top1", "unjudged") for r in rows)
        mean = lambda k: sum(float(r.get(k) or 0) for r in rows) / n  # noqa: E731
        lines.append(f"| {name} | {len(rows)} | {c['exact']} | {c['compatible']} | {c['wrong']} | {sum(1 for r in rows if r.get('top3_hit'))} | "
                     f"{sum(r['unsupported_claims'] for r in rows)} | {sum(int(r.get('sheet_only_direct') or 0) for r in rows)} | {mean('full_res_series_coverage'):.2f} | {mean('primary_study_coverage'):.2f} | {mean('tool_calls'):.1f} | "
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
