"""LLM-as-judge for diagnosis correctness. Reads ground truth; never used by the agent."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from ..llm.client import LLMClient, extract_json
from ..llm.schemas import DiagnosisReport

SYSTEM = (
    "You grade a diagnostic agent's answer against a reference diagnosis and the case discussion. Be strict about the "
    "disease entity but ignore wording differences.\n"
    "Grade top1 as:\n"
    "- exact: same disease entity as the reference (synonyms/abbreviations OK; a more specific correct subtype also counts).\n"
    "- compatible: the correct underlying process or a direct parent category, or the reference entity is explicitly named as the "
    "mechanism/lead point (e.g. 'appendicitis' when the reference is 'appendiceal intussusception'; 'osteophyte' when the reference "
    "is 'degenerative osteophyte mimicking metastasis').\n"
    "- wrong: a different disease.\n"
    "Also report whether the reference entity appears among the top-1 plus the listed differentials (top3_hit), judged by meaning.\n"
    "Respond with ONLY JSON: {\"top1\": \"exact|compatible|wrong\", \"top3_hit\": true|false, \"reason\": \"<=30 words\"}"
)


REFERENCE_NOTE = "Safety downgrade: graded incompatible with the reference. Not a clinical conclusion."


def withhold_incompatible(report: DiagnosisReport) -> DiagnosisReport:
    """Called only after grading. Does not copy the reference or discussion into the report."""
    if report.diagnosis != "undetermined":
        report.uncertainty.append(f"Unconfirmed candidate (not a clinical conclusion): {report.diagnosis}.")
        report.diagnosis = "undetermined"
    if REFERENCE_NOTE not in report.uncertainty:
        report.uncertainty.append(REFERENCE_NOTE)
    report.degraded = True
    return report


def judge(llm: LLMClient, report: DiagnosisReport, label: str, discussion: str) -> Dict:
    user = (f"Reference diagnosis: {label}\nCase discussion (ground truth context): {discussion[:1500]}\n\n"
            f"Agent top-1 diagnosis: {report.diagnosis}\n"
            f"Agent differentials: {json.dumps([d.name for d in report.differential_diagnoses])}")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    for _ in range(2):
        res = llm.complete(messages, max_tokens=2500)  # thinking on: grading needs care; JSON is tiny
        obj = extract_json(res.content or "")
        if obj and obj.get("top1") in ("exact", "compatible", "wrong"):
            top1 = obj["top1"]
            return {"top1": top1, "top3_hit": bool(obj.get("top3_hit")) or top1 == "exact", "reason": str(obj.get("reason", ""))[:200]}
        messages.append({"role": "assistant", "content": res.content or ""})
        messages.append({"role": "user", "content": "Reply with ONLY the JSON object."})
    return {"top1": "unjudged", "top3_hit": False, "reason": "judge failed"}
