"""Critic: a blinded read of the few full-resolution images the diagnosis rests on.

Structural problems are caught by EvidenceLedger.audit before this module is called.
The critic does not see the ledger or the agent's claims. It describes the cited images,
then chooses between the submitted diagnosis and the next hypothesis.
"""

from __future__ import annotations

import json
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError

from ..llm.client import LLMClient, extract_json
from ..llm.schemas import CriticIssue, CriticResult, FinishArgs
from .ledger import AuditItem, EvidenceLedger
from .prompts import load_prompt


class BlindRead(BaseModel):
    visible: str
    supports: List[str] = Field(default_factory=list)
    missing_view: Optional[str] = None


class CriticChoice(BaseModel):
    choice: str
    seen_as: str = ""
    missing_view: Optional[str] = None

    def normalized(self) -> str:
        value = (self.choice or "").strip().casefold()
        if value in ("candidate", "alternative", "neither"):
            return value
        raise ValueError(f"choice must be candidate, alternative, or neither, got {self.choice!r}")


def alternative_name(ledger: EvidenceLedger, candidate: str, differentials: List[str]) -> str:
    """Highest-ranked hypothesis that is not the submitted diagnosis, else a differential."""
    cand = candidate.casefold()
    if ledger.hypothesis_history:
        ranked = sorted(ledger.hypothesis_history[-1].hypotheses, key=lambda h: -h.probability)
        for h in ranked:
            if h.name.casefold() != cand:
                return h.name
    for name in differentials:
        if name and name.casefold() != cand:
            return name
    return "a different disease"


def _leading_support_aligns(candidate: str, blind: BlindRead) -> bool:
    """The choice call must not confirm a label the blind read did not name first."""
    if not blind.supports or not blind.supports[0].strip():
        return True
    lead = blind.supports[0].casefold().strip()
    cand = candidate.casefold()
    if lead in cand or cand in lead:
        return True
    words = {w for w in cand.replace("-", " ").split() if len(w) >= 6}
    return any(w in lead for w in words)


def decision_from_choice(choice: CriticChoice, blind: BlindRead, candidate: str, alternative: str) -> CriticResult:
    """A disagreement withholds the diagnosis. It does not start another diagnostic pass."""
    kind = choice.normalized()
    if kind == "candidate" and _leading_support_aligns(candidate, blind):
        return CriticResult(decision="finalize", confirmed=True, issues=[], suggested_action=None)
    if kind == "alternative":
        picked = alternative
    elif kind == "candidate":
        picked = blind.supports[0] if blind.supports else alternative
    elif choice.seen_as.strip():
        picked = choice.seen_as.strip()
    elif blind.supports:
        picked = blind.supports[0]
    else:
        picked = "a finding these images show"
    detail = (f"Blind read of the cited images supports {picked}, not {candidate}. "
              f"Visible: {blind.visible[:400]}")
    return CriticResult(decision="finalize", confirmed=False, issues=[CriticIssue(type="other", detail=detail)])


def unavailable_read(detail: str) -> CriticResult:
    return CriticResult(decision="finalize", confirmed=False, unavailable=True,
                        issues=[CriticIssue(type="other", detail=detail)])


def merge_audit(critic: Optional[CriticResult], audit: List[AuditItem], *, can_revisit: bool) -> CriticResult:
    """Hard audit items force revisit when another image can still be fetched. Soft items stay in the list."""
    if critic is None:
        critic = CriticResult(issues=[], decision="finalize", suggested_action=None)
    present = "\n".join(i.detail for i in critic.issues)
    for item in audit:
        if item.detail not in present:
            critic.issues.append(CriticIssue(type=item.type, detail=item.detail))  # type: ignore[arg-type]
            present = present + "\n" + item.detail
    hard = [a for a in audit if a.hard]
    if hard and can_revisit:
        critic.decision = "revisit"
        action = next((a.action for a in hard if a.action), None)
        if action:
            critic.suggested_action = action
    elif hard and critic.decision == "revisit":
        critic.decision = "finalize"
    return critic


def run_visual_critic(llm: LLMClient, candidate: FinishArgs, alternative: str, image_parts: List[dict],
                      trace=None) -> CriticResult:
    """Blind-read up to a few images, then choose between two diagnosis labels.

    image_parts items are {"label", "data_url"}. No ledger text is included.
    A missing answer is withheld, not treated as agreement.
    """
    if not image_parts:
        return unavailable_read("No full-resolution primary image was available to check. The diagnosis is withheld.")
    version, system = load_prompt("critic")
    blind = _blind_read(llm, system, version, image_parts, trace)
    if blind is None:
        return unavailable_read("Blind image read produced no answer. The diagnosis is withheld.")
    if trace:
        trace.log("critic_blind", visible=blind.visible[:1000], supports=blind.supports, missing_view=blind.missing_view)
    choice = _choose(llm, system, version, blind, candidate.diagnosis, alternative, trace)
    if choice is None:
        return unavailable_read("The image read could not choose a label. The diagnosis is withheld.")
    if trace:
        trace.log("critic_choice", choice=choice.choice, seen_as=choice.seen_as, missing_view=choice.missing_view)
    return decision_from_choice(choice, blind, candidate.diagnosis, alternative)


def _blind_read(llm: LLMClient, system: str, version: str, image_parts: List[dict], trace) -> Optional[BlindRead]:
    content: List[dict] = []
    for part in image_parts:
        content.append({"type": "text", "text": part["label"]})
        content.append({"type": "image_url", "image_url": {"url": part["data_url"]}})
    content.append({"type": "text", "text": (
        "Blind read. You have not been told the agent's diagnosis or its written findings. "
        "Reply with ONLY this JSON object:\n"
        '{"visible": "what is on these images", "supports": ["entity", "entity"], "missing_view": "one view or zoom that would separate them, or null"}'
    )})
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    obj = _ask_json(llm, messages, trace, version=version, n_images=len(image_parts), thinking=True, max_tokens=16000,
                    retry_thinking=False, retry_max_tokens=2000, model=BlindRead)
    return obj  # type: ignore[return-value]


def _choose(llm: LLMClient, system: str, version: str, blind: BlindRead, candidate: str, alternative: str,
            trace) -> Optional[CriticChoice]:
    visible = blind.visible[:1200]
    user = (
        "Your blind read of the images was:\n"
        f"{visible}\n\n"
        f"Entities that read suggested: {json.dumps(blind.supports[:2], ensure_ascii=False)}\n\n"
        "Choose which label those images support. You still do not see the agent's written claims.\n"
        f"candidate: {candidate}\n"
        f"alternative: {alternative}\n\n"
        "Reply with ONLY this JSON object:\n"
        '{"choice": "candidate|alternative|neither", "seen_as": "disease the images show, required when neither", "missing_view": null}'
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    obj = _ask_json(llm, messages, trace, version=version, n_images=0, thinking=False, max_tokens=800, model=CriticChoice)
    return obj  # type: ignore[return-value]


def _ask_json(llm: LLMClient, messages: List[dict], trace, *, version: str, n_images: int, thinking: bool,
              max_tokens: int, model, retry_thinking: Optional[bool] = None, retry_max_tokens: Optional[int] = None):
    role = "critic_blind" if n_images else "critic_choice"
    for attempt in range(2):
        use_thinking = thinking if attempt == 0 or retry_thinking is None else retry_thinking
        use_max = max_tokens if attempt == 0 or not retry_max_tokens else retry_max_tokens
        if trace:
            trace.log("llm_request", role=role, prompt_version=version, attempt=attempt, n_images=n_images)
        res = llm.complete(messages, max_tokens=use_max, thinking=use_thinking)
        if trace:
            trace.log("llm_response", role=role, content=(res.content or "")[:2000], reasoning_chars=res.reasoning_chars,
                      reasoning=(res.reasoning or "")[:2000],
                      prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, latency_s=round(res.latency_s, 2))
        obj = extract_json(res.content or "")
        if obj is not None:
            try:
                parsed = model.model_validate(obj)
                if isinstance(parsed, CriticChoice):
                    parsed.normalized()
                return parsed
            except (ValidationError, ValueError) as e:
                err = str(e)
        else:
            err = "no JSON object found"
        messages.append({"role": "assistant", "content": res.content or ""})
        messages.append({"role": "user", "content": f"Invalid output ({err[:300]}). Reply with ONLY the JSON object."})
    return None
