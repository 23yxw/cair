"""Controller: the single VLM tool loop with budgets, stop conditions, context
trimming and trace recording. Optionally followed by Critic -> Finalizer."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..data.repository import CaseRepository
from ..llm.client import LLMClient
from ..llm.schemas import CriticResult, DiagnosisReport, FinishArgs
from ..trace.recorder import TraceRecorder
from .critic import run_critic
from .finalizer import run_finalizer, template_report
from .ledger import EvidenceLedger
from .prompts import load_prompt
from .tools import ToolContext, ToolResult, ToolSpec, build_registry, execute, render_expert_cards

FREE_TOOLS = {"record_evidence", "update_hypotheses", "finish"}  # never count against the tool budget
REVISIT_EXTRA_CALLS = 4


@dataclass
class RunResult:
    case_id: str
    trace_id: str
    report: DiagnosisReport
    finish_args: Optional[FinishArgs]
    critic: Optional[CriticResult]
    stop_reason: str
    tool_calls: int
    images_sent: int
    usage: Dict[str, Any]
    ledger: List[dict]
    trace_path: Optional[str]
    elapsed_s: float
    errors: int = 0
    finish_attempts: int = 0


@dataclass
class _LoopState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    image_msgs: List[tuple] = field(default_factory=list)  # (round, message)
    round: int = 0
    tool_calls_used: int = 0
    finish_args: Optional[FinishArgs] = None
    finish_attempts: int = 0
    errors: int = 0
    nudges: int = 0
    stale_rounds: int = 0
    stop_reason: str = ""


class Controller:
    def __init__(self, settings: Settings, repo: CaseRepository, llm: LLMClient, *, findings_policy: str = "hidden",
                 with_critic: bool = False, trace_dir: Optional[Path] = None, enable_compare: bool = True):
        self.settings = settings
        self.repo = repo
        self.llm = llm
        self.findings_policy = findings_policy
        self.with_critic = with_critic
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.registry: Dict[str, ToolSpec] = build_registry(findings_policy, enable_compare=enable_compare)
        self.prompt_version, body = load_prompt("controller")
        findings_note = ""
        if findings_policy == "visible":
            findings_note = ("4b. The radiologist's written findings ARE available in this setting: call read_study_findings for every "
                             "study you examine, record them as source='study_findings' evidence, and reconcile them with what you "
                             "see yourself. When the report and your own reading disagree, say so explicitly in the ledger.\n")
        self.system_prompt = body.replace("{expert_cards}", render_expert_cards(self.registry)) \
            .replace("{findings_note}", findings_note) \
            .replace("{max_tool_calls}", str(settings.max_tool_calls)).replace("{max_images}", str(settings.max_images))

    # ------------------------------------------------------------------ public
    def run(self, case_id: str) -> RunResult:
        t_start = time.time()
        trace = TraceRecorder(self.trace_dir / f"{case_id}.jsonl" if self.trace_dir else None)
        ledger = EvidenceLedger()
        ctx = ToolContext(repo=self.repo, ledger=ledger, settings=self.settings, findings_policy=self.findings_policy)
        st = _LoopState()
        st.messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": f"Diagnose {case_id}. Start by building the case map, then gather evidence and finish."},
        ]
        trace.log("case_start", case_id=case_id, model=self.llm.model, prompt_version=self.prompt_version,
                  findings_policy=self.findings_policy, with_critic=self.with_critic,
                  budget={"tool_calls": self.settings.max_tool_calls, "images": self.settings.max_images})
        usage0 = self.llm.usage.as_dict()

        try:
            self._loop(case_id, st, ctx, ledger, trace, t_start, allow_calls=self.settings.max_tool_calls)
            critic = None
            if self.with_critic and st.finish_args is not None:
                critic = self._critic_phase(case_id, st, ctx, ledger, trace, t_start)
                report = run_finalizer(self.llm, ledger, st.finish_args, critic, trace.trace_id, trace)  # type: ignore[arg-type]
            elif st.finish_args is not None:
                report = DiagnosisReport(**st.finish_args.model_dump(), trace_id=trace.trace_id)
            else:
                report = template_report(None, ledger, trace.trace_id)
        except Exception as e:  # noqa: BLE001 - always produce a report + trace
            trace.log("error", stage="controller", detail=f"{type(e).__name__}: {e}")
            st.stop_reason = st.stop_reason or f"exception:{type(e).__name__}"
            critic = None
            report = template_report(st.finish_args, ledger, trace.trace_id)

        usage1 = self.llm.usage.as_dict()
        usage = {k: (usage1[k] - usage0[k]) if isinstance(usage1[k], (int, float)) else usage1[k] for k in usage1}
        usage["latency_s"] = round(usage["latency_s"], 2)
        elapsed = round(time.time() - t_start, 2)
        trace.log("finish", report=report.model_dump())
        trace.log("case_end", stop_reason=st.stop_reason, tool_calls=st.tool_calls_used, images_sent=ctx.images_sent,
                  usage_total=usage, elapsed_s=elapsed, errors=st.errors, finish_attempts=st.finish_attempts,
                  ledger_entries=len(ledger.entries))
        trace.close()
        return RunResult(case_id=case_id, trace_id=trace.trace_id, report=report, finish_args=st.finish_args, critic=critic,
                         stop_reason=st.stop_reason, tool_calls=st.tool_calls_used, images_sent=ctx.images_sent, usage=usage,
                         ledger=ledger.as_dicts(), trace_path=str(trace.path) if trace.path else None, elapsed_s=elapsed,
                         errors=st.errors, finish_attempts=st.finish_attempts)

    # ------------------------------------------------------------------ loop
    def _loop(self, case_id: str, st: _LoopState, ctx: ToolContext, ledger: EvidenceLedger, trace: TraceRecorder,
              t_start: float, allow_calls: int) -> None:
        tools_all = [s.openai_schema() for s in self.registry.values()]
        tools_finish = [self.registry["record_evidence"].openai_schema(), self.registry["finish"].openai_schema()]
        force_finish = False
        while True:
            if time.time() - t_start > self.settings.case_timeout_s:
                st.stop_reason = "timeout"
                trace.log("budget_exhausted", kind="time")
                return
            if st.finish_attempts >= 3:
                st.stop_reason = "finish_invalid_3x"
                return
            budget_left = allow_calls - st.tool_calls_used
            if budget_left <= 0 and not force_finish:
                force_finish = True
                trace.log("budget_exhausted", kind="tool_calls")
                st.messages.append({"role": "user", "content": "Tool budget exhausted. Call finish now, citing only ids already returned."})
            elif st.stale_rounds >= 3 and not force_finish:
                force_finish = True
                trace.log("budget_exhausted", kind="no_progress")
                st.messages.append({"role": "user", "content": "No new evidence in the last rounds. Call finish now."})

            self._trim_images(st)
            n_img = sum(1 for _, m in st.image_msgs if any(p.get("type") == "image_url" for p in m["content"]))
            tools = tools_finish if force_finish else tools_all
            tool_choice: Any = {"type": "function", "function": {"name": "finish"}} if force_finish else "auto"
            trace.log("llm_request", role="controller", round=st.round, n_messages=len(st.messages), n_images_in_context=n_img,
                      prompt_version=self.prompt_version, forced=force_finish)
            res = self.llm.complete(st.messages, tools=tools, tool_choice=tool_choice, max_tokens=6000)
            trace.log("llm_response", role="controller", round=st.round, content=(res.content or "")[:1500],
                      tool_calls=[{"name": c.name, "arguments": c.arguments} for c in res.tool_calls],
                      reasoning_chars=res.reasoning_chars,
                      prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, latency_s=round(res.latency_s, 2))
            st.messages.append(res.raw_message)
            st.round += 1

            if not res.tool_calls:
                st.nudges += 1
                if st.nudges >= 3:
                    force_finish = True
                st.messages.append({"role": "user", "content": "Continue by calling a tool. When your evidence is sufficient, call finish."})
                continue

            seen_before, entries_before = len(ledger.seen), len(ledger.entries)
            image_parts = []
            for call in res.tool_calls:
                trace.log("tool_call", name=call.name, arguments=call.arguments, call_id=call.id)
                counted = call.name not in FREE_TOOLS and call.name in self.registry
                if counted and st.tool_calls_used >= allow_calls:
                    # budget is enforced per call: a single response may contain several calls
                    result = ToolResult(text="error: tool budget exhausted; call record_evidence for what you have seen, then finish.", error=True)
                else:
                    if counted:
                        st.tool_calls_used += 1
                    result = execute(self.registry, ctx, call.name, call.arguments)
                if hasattr(self.repo, "drain_audit"):
                    for q in self.repo.drain_audit():  # type: ignore[attr-defined]
                        trace.log("sql_query", tool=call.name, **q)
                if result.error:
                    st.errors += 1
                if call.name == "finish":
                    st.finish_attempts += 1
                    if not result.error:
                        st.finish_args = FinishArgs.model_validate(call.arguments)
                        st.stop_reason = "finish"
                if call.name == "record_evidence" and not result.error:
                    trace.log("evidence_added", **result.data)
                if call.name == "update_hypotheses" and not result.error:
                    trace.log("hypotheses_updated", **result.data)
                trace.log("tool_result", name=call.name, call_id=call.id, error=result.error,
                          summary=result.text[:800], image_ids=[i for p in result.images for i in p.image_ids],
                          n_images=result.n_images(), data=result.data if call.name not in FREE_TOOLS else {})
                content = result.text
                if result.images:
                    content += f"\n(budget: {st.tool_calls_used}/{allow_calls} tool calls, {ctx.images_sent}/{self.settings.max_images} images used)"
                st.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                image_parts.extend(result.images)
                if st.finish_args is not None:
                    return
            if image_parts:
                msg = {"role": "user", "content": []}
                for p in image_parts:
                    msg["content"].append({"type": "text", "text": p.label})
                    msg["content"].append({"type": "image_url", "image_url": {"url": p.data_url}})
                msg["content"].append({"type": "text", "text": "Describe what you see, then call record_evidence for each observation."})
                st.messages.append(msg)
                st.image_msgs.append((st.round, msg))
            progressed = len(ledger.seen) > seen_before or len(ledger.entries) > entries_before
            st.stale_rounds = 0 if progressed else st.stale_rounds + 1

    def _critic_phase(self, case_id: str, st: _LoopState, ctx: ToolContext, ledger: EvidenceLedger,
                      trace: TraceRecorder, t_start: float) -> Optional[CriticResult]:
        studies = [{"study_id": s.study_id, "modality": s.modality, "n_series": s.n_series} for s in self.repo.list_studies(case_id)]
        critic = run_critic(self.llm, ledger, st.finish_args, studies, trace)  # type: ignore[arg-type]
        trace.log("critic_result", result=critic.model_dump() if critic else None)
        if critic and critic.decision == "revisit" and ctx.images_left() > 0:
            issues = "; ".join(f"{i.type}: {i.detail}" for i in critic.issues) or "none"
            st.messages.append({"role": "user", "content": (
                f"A reviewer flagged: {issues}. Suggested action: {critic.suggested_action or 'none'}. "
                f"You may make up to {REVISIT_EXTRA_CALLS} more tool calls to address this, then call finish again "
                f"(you may keep or change your diagnosis).")})
            prev_finish = st.finish_args
            st.finish_args = None
            st.finish_attempts = 0
            st.stale_rounds = 0
            self._loop(case_id, st, ctx, ledger, trace, t_start, allow_calls=st.tool_calls_used + REVISIT_EXTRA_CALLS)
            if st.finish_args is None:
                st.finish_args = prev_finish
                st.stop_reason = "revisit_no_finish"
            else:
                st.stop_reason = "finish_after_revisit"
        return critic

    # ------------------------------------------------------------------ context
    def _trim_images(self, st: _LoopState) -> None:
        """Replace image parts older than image_keep_rounds with text placeholders."""
        keep = self.settings.image_keep_rounds
        for rnd, msg in st.image_msgs:
            if st.round - rnd < keep:
                continue
            new_content = []
            for part in msg["content"]:
                if part.get("type") == "image_url":
                    new_content.append({"type": "text", "text": "(image shown earlier; your observations are in the ledger)"})
                else:
                    new_content.append(part)
            msg["content"] = new_content
