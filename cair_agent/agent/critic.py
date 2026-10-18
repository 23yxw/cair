"""Critic: text-only consistency check over the ledger before finalization."""

from __future__ import annotations

import json
from typing import List, Optional

from pydantic import ValidationError

from ..llm.client import LLMClient, extract_json
from ..llm.schemas import CriticResult, FinishArgs
from .ledger import EvidenceLedger
from .prompts import load_prompt


def run_critic(llm: LLMClient, ledger: EvidenceLedger, candidate: FinishArgs, available_studies: List[dict],
               trace=None) -> Optional[CriticResult]:
    version, system = load_prompt("critic")
    sampled = sorted({m.get("study_id") for k, m in ledger.seen_meta.items() if k.startswith("img:") and m.get("study_id")})
    user = (
        "## Available studies\n" + json.dumps(available_studies, ensure_ascii=False)
        + "\n\n## Studies with sampled images\n" + json.dumps(sampled)
        + "\n\n## Evidence ledger\n" + ledger.summary_text()
        + "\n\n## Candidate answer\n" + json.dumps(candidate.model_dump(), ensure_ascii=False, indent=1)
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(2):
        if trace:
            trace.log("llm_request", role="critic", prompt_version=version, attempt=attempt, n_images=0)
        # thinking on (this is a reasoning step) but with enough room for the answer after it
        res = llm.complete(messages, max_tokens=6000)
        if trace:
            trace.log("llm_response", role="critic", content=(res.content or "")[:2000], reasoning_chars=res.reasoning_chars,
                      prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, latency_s=round(res.latency_s, 2))
        obj = extract_json(res.content or "")
        if obj is not None:
            try:
                return CriticResult.model_validate(obj)
            except ValidationError as e:
                err = str(e)
        else:
            err = "no JSON object found"
        messages.append({"role": "assistant", "content": res.content or ""})
        messages.append({"role": "user", "content": f"Invalid output ({err[:300]}). Reply with ONLY the JSON object."})
    return None
