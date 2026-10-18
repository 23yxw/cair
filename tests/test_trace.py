"""Trace rendering and leak checks against a synthetic trace (no LLM needed)."""

from __future__ import annotations

import json
from pathlib import Path

from cair_agent.eval.metrics import cost_metrics, evidence_metrics, leak_check, load_run
from cair_agent.trace.recorder import TraceRecorder, read_trace
from cair_agent.trace.render import render

from .conftest import requires_data


def _write_trace(path: Path, case_id: str, image_id: str, study_id: str, series_id: str) -> None:
    t = TraceRecorder(path)
    t.log("case_start", case_id=case_id, model="m", prompt_version="controller-vX", findings_policy="hidden", with_critic=True,
          budget={"tool_calls": 14, "images": 24})
    t.log("tool_call", name="get_case_profile", arguments={"case_id": case_id}, call_id="c1")
    t.log("tool_result", name="get_case_profile", call_id="c1", error=False, summary="{}", image_ids=[], n_images=0,
          data={"text_id": f"profile:{case_id}"})
    t.log("tool_call", name="sample_images", arguments={"series_id": series_id, "strategy": "anchors"}, call_id="c2")
    t.log("tool_result", name="sample_images", call_id="c2", error=False, summary="1 image", image_ids=[image_id], n_images=1, data={})
    t.log("hypotheses_updated", hypotheses=[{"name": "A", "probability": 0.7, "rationale": ""}], note="initial")
    t.log("evidence_added", id="e1", claim="finding", source="image", support="direct", confidence=0.9,
          evidence_refs=[{"image_id": image_id, "study_id": study_id, "series_id": series_id, "slice_index": 0}], contradictions=[])
    t.log("critic_result", result={"issues": [], "decision": "finalize", "suggested_action": None})
    report = {"diagnosis": "X", "differential_diagnoses": [], "key_findings": [
        {"fact": "finding", "source": "image", "evidence_refs": [f"img:{image_id}"], "confidence": 0.9},
        {"fact": "made up", "source": "image", "evidence_refs": ["img:0"], "confidence": 0.9}],
        "uncertainty": [], "next_best_check": None, "trace_id": t.trace_id, "degraded": False}
    t.log("finish", report=report)
    t.log("case_end", stop_reason="finish", tool_calls=2, images_sent=1, usage_total={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "calls": 2, "latency_s": 1.0},
          elapsed_s=2.0, errors=0, finish_attempts=1, ledger_entries=1)
    t.close()


@requires_data
def test_render_and_metrics(tmp_path, repo, index):
    cid = "case-01"
    st = repo.list_studies(cid)[0]
    se = repo.list_series(st.study_id)[0]
    im = repo.list_images(se.series_id)[0]
    p = tmp_path / "t.jsonl"
    _write_trace(p, cid, im.image_id, st.study_id, se.series_id)

    events = read_trace(p)
    md = render(events)
    assert "## Step-by-step" in md and "**hypotheses:**" in md and "ledger e1" in md and "### Critic" in md
    assert f"img:{im.image_id}" in md

    events, rep = load_run(p)
    em = evidence_metrics(rep, events, repo, cid)
    assert em["unsupported_claims"] == 1 and em["key_findings"] == 2
    assert 0 < em["primary_study_coverage"] <= 1
    cm = cost_metrics(events)
    assert cm["total_tokens"] == 15 and cm["tool_calls"] == 2

    folder_names = [d.name for d in Path(index.data_dir).iterdir() if d.is_dir()]
    lk = leak_check(p, folder_names, [l.discussion for l in index.labels])
    assert lk["leak_free"]
    # a trace that mentions a folder name must be flagged
    p.write_text(p.read_text() + json.dumps({"type": "x", "text": folder_names[0]}) + "\n")
    assert not leak_check(p, folder_names, [])["leak_free"]
