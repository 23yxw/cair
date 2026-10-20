"""Finalizer: produce the DiagnosisReport from the validated ledger.

Falls back to a template built from the agent's finish() arguments and the
ledger when the model cannot produce a schema-valid report in 3 attempts.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import ValidationError

from ..llm.client import LLMClient, extract_json
from ..llm.schemas import CriticResult, DiagnosisReport, FinishArgs, KeyFinding
from .ledger import EvidenceLedger, LedgerError
from .prompts import load_prompt


def template_report(candidate: Optional[FinishArgs], ledger: EvidenceLedger, trace_id: str) -> DiagnosisReport:
    """Deterministic fallback: never fails, marked degraded."""
    kfs = []
    if candidate:
        for kf in candidate.key_findings:
            try:
                ledger.validate_keys(kf.evidence_refs)
                kfs.append(kf)
            except LedgerError:
                continue
    if not kfs:
        for e in ledger.entries[:8]:
            kfs.append(KeyFinding(fact=e.claim, source=e.source, evidence_refs=[r.key() for r in e.evidence_refs], confidence=e.confidence))
    if not kfs:
        kfs.append(KeyFinding(fact="no validated evidence recorded", source="tool", evidence_refs=["none"], confidence=0.0))
    return DiagnosisReport(
        diagnosis=candidate.diagnosis if candidate else "undetermined",
        differential_diagnoses=candidate.differential_diagnoses if candidate else [],
        key_findings=kfs,
        uncertainty=(candidate.uncertainty if candidate else []) + ["report produced by template fallback"],
        next_best_check=candidate.next_best_check if candidate else None,
        trace_id=trace_id, degraded=True,
    )


def withhold_diagnosis(report: DiagnosisReport, candidate: str, reason: str) -> DiagnosisReport:
    """Replace a specific diagnosis that was not independently confirmed.

    The candidate stays in uncertainty. discussion.txt is never an input here.
    """
    note = f"Unconfirmed candidate (not a clinical conclusion): {candidate}. {reason}"
    if report.diagnosis != "undetermined":
        report.uncertainty.append(note)
        report.diagnosis = "undetermined"
    elif note not in report.uncertainty:
        report.uncertainty.append(note)
    report.degraded = True
    return report


def constrain_report(rep: DiagnosisReport, candidate: FinishArgs, ledger: EvidenceLedger) -> DiagnosisReport:
    """The diagnosis string is the candidate's. Each key finding must copy one ledger claim."""
    rep.diagnosis = candidate.diagnosis
    by_claim = {}
    for e in ledger.entries:
        by_claim.setdefault(e.claim, e)
    kept = []
    for kf in rep.key_findings:
        e = by_claim.get(kf.fact)
        if e is None:
            continue
        allowed = {r.key() for r in e.evidence_refs}
        if not kf.evidence_refs or not set(kf.evidence_refs).issubset(allowed):
            continue
        kf.source = e.source
        kept.append(kf)
    if not kept:
        return template_report(candidate, ledger, rep.trace_id)
    rep.key_findings = kept
    return rep


def run_finalizer(llm: LLMClient, ledger: EvidenceLedger, candidate: FinishArgs, critic: Optional[CriticResult],
                  trace_id: str, trace=None) -> DiagnosisReport:
    version, system = load_prompt("finalizer")
    user = (
        "## Evidence ledger (cite these keys exactly)\n" + ledger.summary_text()
        + "\n\n## Agent candidate answer\n" + json.dumps(candidate.model_dump(), ensure_ascii=False, indent=1)
        + "\n\n## Critic review\n" + (json.dumps(critic.model_dump(), ensure_ascii=False, indent=1) if critic else "(critic unavailable)")
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(3):
        if trace:
            trace.log("llm_request", role="finalizer", prompt_version=version, attempt=attempt, n_images=0)
        # formatting step: thinking off so the completion budget goes to the JSON itself
        res = llm.complete(messages, max_tokens=3000, thinking=False)
        if trace:
            trace.log("llm_response", role="finalizer", content=(res.content or "")[:3000],
                      prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, latency_s=round(res.latency_s, 2))
        obj = extract_json(res.content or "")
        err = "no JSON object found"
        if obj is not None:
            obj["trace_id"] = trace_id
            try:
                rep = DiagnosisReport.model_validate(obj)
                for kf in rep.key_findings:
                    ledger.validate_keys(kf.evidence_refs)
                return constrain_report(rep, candidate, ledger)
            except (ValidationError, LedgerError) as e:
                err = str(e)
        messages.append({"role": "assistant", "content": res.content or ""})
        messages.append({"role": "user", "content": f"Invalid report ({err[:400]}). Fix it and reply with ONLY the JSON object."})
    if trace:
        trace.log("error", stage="finalizer", detail="schema validation failed 3x; using template fallback")
    return template_report(candidate, ledger, trace_id)
