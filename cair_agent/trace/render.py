"""Render a JSONL trace into a readable Markdown walkthrough.

Sections: case map -> study/series selection -> image sampling -> observations
-> hypothesis updates -> critic -> final diagnosis, followed by the raw tool IO
so a reader can audit every step of the evidence chain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .recorder import read_trace


def _j(obj: Any, limit: int = 1200) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=1)
    return s if len(s) <= limit else s[:limit] + "\n... (truncated)"


def render(events: List[Dict[str, Any]]) -> str:
    start = next((e for e in events if e["type"] == "case_start"), {})
    end = next((e for e in events if e["type"] == "case_end"), {})
    fin = next((e for e in events if e["type"] == "finish"), {})
    out: List[str] = []
    out.append(f"# Run trace: {start.get('case_id', '?')} (trace {start.get('trace_id', '?')})\n")
    out.append(f"- model: `{start.get('model')}` | prompt: `{start.get('prompt_version')}` | findings: `{start.get('findings_policy')}` | critic: `{start.get('with_critic')}`")
    out.append(f"- stop reason: `{end.get('stop_reason')}` | tool calls: {end.get('tool_calls')} | images sent: {end.get('images_sent')} | ledger entries: {end.get('ledger_entries')} | errors: {end.get('errors')}")
    u = end.get("usage_total", {})
    out.append(f"- tokens: {u.get('total_tokens')} (prompt {u.get('prompt_tokens')}, completion {u.get('completion_tokens')}) | LLM calls: {u.get('calls')} | elapsed: {end.get('elapsed_s')}s\n")

    out.append("## Step-by-step\n")
    step = 0
    for e in events:
        t = e["type"]
        if t == "llm_response" and e.get("role") == "controller":
            step += 1
            c = (e.get("content") or "").strip()
            if c:
                out.append(f"**Agent (round {e.get('round')}):** {c[:700]}{'...' if len(c) > 700 else ''}\n")
        elif t == "tool_call":
            out.append(f"- `{e['name']}({json.dumps(e['arguments'], ensure_ascii=False)[:300]})`")
        elif t == "tool_result":
            flag = " **ERROR**" if e.get("error") else ""
            imgs = f" -> images: {', '.join(e['image_ids'][:12])}{'...' if len(e['image_ids']) > 12 else ''}" if e.get("image_ids") else ""
            out.append(f"  - result{flag}: {e['summary'][:400].replace(chr(10), ' ')}{imgs}")
        elif t == "evidence_added":
            refs = ", ".join(_ref_key(r) for r in e.get("evidence_refs", []))
            out.append(f"  - **ledger {e.get('id')}** [{e.get('source')}/{e.get('support')} conf={e.get('confidence')}] {e.get('claim')} <- {refs}")
        elif t == "hypotheses_updated":
            hs = "; ".join(f"{h['name']} p={h['probability']:.2f}" for h in e.get("hypotheses", []))
            out.append(f"  - **hypotheses:** {hs}" + (f" — _{e['note']}_" if e.get("note") else ""))
        elif t == "sql_query":
            out.append(f"  - sql `{e.get('template')}` {json.dumps(e.get('params'), ensure_ascii=False)} -> {e.get('rows')} rows, {e.get('ms')} ms")
        elif t == "budget_exhausted":
            out.append(f"- _stop condition triggered: {e.get('kind')}_")
        elif t == "critic_result":
            out.append("\n### Critic\n")
            out.append("```json\n" + _j(e.get("result")) + "\n```\n")
        elif t == "llm_response" and e.get("role") == "finalizer":
            out.append(f"_finalizer attempt (tokens {e.get('prompt_tokens')}+{e.get('completion_tokens')})_")
        elif t == "error":
            out.append(f"- **error** ({e.get('stage')}): {e.get('detail')}")

    out.append("\n## Final report\n")
    out.append("```json\n" + _j(fin.get("report"), 6000) + "\n```\n")

    out.append("\n## Raw tool IO (audit)\n")
    for e in events:
        if e["type"] == "tool_call":
            out.append(f"<details><summary>{e['name']} ({e['call_id']})</summary>\n\n```json\n{_j(e['arguments'])}\n```\n")
        elif e["type"] == "tool_result":
            out.append(f"```text\n{e['summary'][:800]}\n```\n</details>\n")
    return "\n".join(out)


def render_file(path: Path) -> str:
    return render(read_trace(path))


def _ref_key(r: Dict[str, Any]) -> str:
    if r.get("image_id"):
        return f"img:{r['image_id']}"
    if r.get("text_id"):
        return f"text:{r['text_id']}"
    if r.get("series_id"):
        return f"series:{r['series_id']}"
    return f"study:{r.get('study_id')}"
