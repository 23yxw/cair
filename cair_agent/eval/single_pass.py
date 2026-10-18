"""S1 baseline: one multimodal call with admission text + pre-selected anchor images.

No tools, no ledger. Images are labelled the same way as in the agent so the
model can cite img:<id> keys, which lets us measure unsupported claims fairly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from ..agent.controller import RunResult
from ..agent.finalizer import template_report
from ..agent.ledger import EvidenceLedger
from ..config import Settings
from ..data.repository import CaseRepository
from ..imaging import sampler
from ..imaging.encode import encode_jpeg_data_url, load_image
from ..llm.client import LLMClient, extract_json
from ..llm.schemas import DiagnosisReport
from ..trace.recorder import TraceRecorder

SYSTEM = (
    "You are a radiologist and clinician. Diagnose the case from the admission text and the images provided. "
    "Each image is preceded by a label like [img:<id> | study modality | series caption | slice i/n]. "
    "Respond with ONLY a JSON object: {\"diagnosis\": str, \"differential_diagnoses\": [{\"name\", \"why\", \"confidence\"}], "
    "\"key_findings\": [{\"fact\", \"source\": \"patient_text|image\", \"evidence_refs\": [\"img:<id>\" or \"text:profile:<case>\"], \"confidence\"}], "
    "\"uncertainty\": [str], \"next_best_check\": str|null}. Cite only labels you were given."
)


def select_images(repo: CaseRepository, case_id: str, max_images: int) -> List[str]:
    """Anchor slice of every series, primary studies first, capped."""
    chosen: List[str] = []
    studies = sorted(repo.list_studies(case_id), key=lambda s: (s.evidence_grade != "primary", s.order))
    for st in studies:
        for se in repo.list_series(st.study_id):
            imgs = repo.list_images(se.series_id)
            if not imgs:
                continue
            if se.count <= sampler.SMALL_SERIES_MAX:
                chosen.extend(im.image_id for im in imgs)
            else:
                chosen.append(imgs[sampler.anchor_indices(len(imgs), se.anchor_slice)[0]].image_id)
    return chosen[:max_images]


def run_single_pass(settings: Settings, repo: CaseRepository, llm: LLMClient, case_id: str, trace_dir: Optional[Path]) -> RunResult:
    t0 = time.time()
    trace = TraceRecorder(trace_dir / f"{case_id}.jsonl" if trace_dir else None)
    ledger = EvidenceLedger()
    trace.log("case_start", case_id=case_id, model=llm.model, prompt_version="single-pass-v1", findings_policy="hidden", with_critic=False,
              budget={"tool_calls": 0, "images": settings.max_images})
    p = repo.get_case_profile(case_id)
    ledger.mark_seen(f"text:profile:{case_id}")
    text = (f"Case {case_id}. Age: {p.age or 'missing'}. Sex: {p.sex or 'missing'}. "
            f"Presentation: {p.presentation or 'missing (no admission text)'}. (text id: text:profile:{case_id})")
    content = [{"type": "text", "text": text}]
    ids = select_images(repo, case_id, settings.max_images)
    for iid in ids:
        im = repo.get_image(iid)
        se = repo.get_series(im.series_id)
        st = repo.get_study(se.study_id)
        ledger.mark_seen(f"img:{iid}", study_id=st.study_id, series_id=se.series_id, slice_index=im.slice_index)
        grade = f" | {st.evidence_grade} evidence" if st.evidence_grade != "primary" else ""
        content.append({"type": "text", "text": f"[img:{iid} | {st.study_id} {st.modality} | {se.caption or '(no caption)'} | slice {im.slice_index}/{se.count}{grade}]"})
        content.append({"type": "image_url", "image_url": {"url": encode_jpeg_data_url(load_image(repo.resolve_image(iid)), settings.max_image_side, settings.jpeg_quality)}})
    trace.log("tool_result", name="preselect_images", call_id="s1", error=False, summary=f"{len(ids)} anchor images", image_ids=ids, n_images=len(ids), data={})
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
    usage0 = llm.usage.as_dict()
    report: Optional[DiagnosisReport] = None
    errors = 0
    for attempt in range(3):
        trace.log("llm_request", role="single_pass", attempt=attempt, n_images=len(ids))
        res = llm.complete(messages, max_tokens=8000)  # thinking on: baseline gets the same reasoning budget as the agent
        trace.log("llm_response", role="single_pass", content=(res.content or "")[:3000], prompt_tokens=res.prompt_tokens,
                  completion_tokens=res.completion_tokens, latency_s=round(res.latency_s, 2))
        obj = extract_json(res.content or "")
        if obj is not None:
            obj["trace_id"] = trace.trace_id
            try:
                report = DiagnosisReport.model_validate(obj)
                break
            except ValidationError as e:
                err = str(e)
        else:
            err = "no JSON"
        errors += 1
        messages.append({"role": "assistant", "content": res.content or ""})
        messages.append({"role": "user", "content": f"Invalid ({err[:300]}). Reply with ONLY the JSON object."})
    if report is None:
        report = template_report(None, ledger, trace.trace_id)
    usage1 = llm.usage.as_dict()
    usage = {k: (usage1[k] - usage0[k]) if isinstance(usage1[k], (int, float)) else usage1[k] for k in usage1}
    elapsed = round(time.time() - t0, 2)
    trace.log("finish", report=report.model_dump())
    trace.log("case_end", stop_reason="single_pass", tool_calls=0, images_sent=len(ids), usage_total=usage, elapsed_s=elapsed,
              errors=errors, finish_attempts=1, ledger_entries=0, seen=sorted(ledger.seen))
    trace.close()
    return RunResult(case_id=case_id, trace_id=trace.trace_id, report=report, finish_args=None, critic=None, stop_reason="single_pass",
                     tool_calls=0, images_sent=len(ids), usage=usage, ledger=[], trace_path=str(trace.path) if trace.path else None,
                     elapsed_s=elapsed, errors=errors, finish_attempts=1)
