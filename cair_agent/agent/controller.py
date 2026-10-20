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
from .critic import alternative_name, merge_audit, run_visual_critic
from .finalizer import run_finalizer, template_report, withhold_diagnosis
from .ledger import EvidenceLedger
from .prompts import load_prompt
from .tools import (ToolContext, ToolResult, ToolSpec, build_registry, execute, ledger_blocked_by_fetch,
                    note_displayed, public_error, render_expert_cards)

# Case-map calls are free so the 14-call budget is spent on sampling, not on listing the chart.
FREE_TOOLS = {"get_case_profile", "list_studies", "list_series", "record_evidence", "update_hypotheses", "finish"}
TRACE_DATA_TOOLS = {"get_case_profile", "list_studies", "list_series", "search_series", "sample_images", "zoom_image",
                    "compare_images", "read_study_findings"}


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
    withhold_reason: str = ""


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
        ledger.findings_policy = self.findings_policy
        ctx = ToolContext(repo=self.repo, ledger=ledger, settings=self.settings, findings_policy=self.findings_policy,
                          case_id=case_id)
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
                critic = self._critic_phase(st, ledger, trace)
                if st.withhold_reason:
                    report = DiagnosisReport(**st.finish_args.model_dump(), trace_id=trace.trace_id)
                    report = withhold_diagnosis(report, st.finish_args.diagnosis, st.withhold_reason)
                    trace.log("safety_downgrade", candidate=st.finish_args.diagnosis, reason=st.withhold_reason[:500])
                else:
                    report = run_finalizer(self.llm, ledger, st.finish_args, critic, trace.trace_id, trace)  # type: ignore[arg-type]
            elif st.finish_args is not None:
                report = DiagnosisReport(**st.finish_args.model_dump(), trace_id=trace.trace_id)
            else:
                report = template_report(None, ledger, trace.trace_id)
        except Exception as e:  # noqa: BLE001 - always produce a report + trace
            trace.log("error", stage="controller", detail=public_error(e))
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
            st.image_msgs = [(item[0], item[1], False) for item in st.image_msgs]
            n_img = sum(1 for _, m, *_ in st.image_msgs for p in m["content"] if p.get("type") == "image_url")
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
            turn_tools = [c.name for c in res.tool_calls]
            for call in res.tool_calls:
                if ledger_blocked_by_fetch(call.name, turn_tools):
                    result = ToolResult(text="error: images fetched in this turn are not visible yet. On the next turn, describe them, then call record_evidence or finish.", error=True)
                    trace.log("tool_call", name=call.name, arguments=call.arguments, call_id=call.id)
                    trace.log("tool_result", name=call.name, call_id=call.id, error=True, summary=result.text[:800],
                              image_ids=[], n_images=0, data={})
                    st.errors += 1
                    st.messages.append({"role": "tool", "tool_call_id": call.id, "content": result.text})
                    continue
                trace.log("tool_call", name=call.name, arguments=call.arguments, call_id=call.id)
                counted = call.name not in FREE_TOOLS and call.name in self.registry
                if counted and st.tool_calls_used >= allow_calls:
                    # budget is enforced per call: a single response may contain several calls
                    result = ToolResult(text="error: tool budget exhausted; call record_evidence for what you have seen, then finish.", error=True)
                else:
                    if counted:
                        st.tool_calls_used += 1
                    ctx.calls_left = allow_calls - st.tool_calls_used
                    ctx.allow_sampling = (not force_finish) and ctx.calls_left > 0 and ctx.images_left() > 0
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
                        if result.data.get("downgraded"):
                            st.finish_args.uncertainty.append(
                                "Contact-sheet-only findings downgraded to indirect: " + ", ".join(result.data["downgraded"]))
                        st.stop_reason = "finish"
                if call.name == "record_evidence" and not result.error:
                    trace.log("evidence_added", **result.data)
                if call.name == "update_hypotheses" and not result.error:
                    trace.log("hypotheses_updated", **result.data)
                trace_data: Dict[str, Any] = {}
                if call.name in TRACE_DATA_TOOLS:
                    trace_data = result.data
                elif result.data.get("downgraded"):
                    trace_data = {"downgraded": result.data["downgraded"]}
                trace.log("tool_result", name=call.name, call_id=call.id, error=result.error,
                          summary=result.text[:800], image_ids=[i for p in result.images for i in p.image_ids],
                          n_images=result.n_images(), data=trace_data)
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
                hint = "Describe what you see, then call record_evidence for each observation."
                if any("contact sheet" in p.label for p in image_parts):
                    hint += " A contact sheet is an overview: record it as support='indirect'. A direct finding needs neighbors or zoom on one slice."
                msg["content"].append({"type": "text", "text": hint})
                st.messages.append(msg)
                st.image_msgs.append((st.round, msg, True))
                for part in image_parts:
                    note_displayed(ctx, part)
            progressed = len(ledger.seen) > seen_before or len(ledger.entries) > entries_before
            st.stale_rounds = 0 if progressed else st.stale_rounds + 1

    def _critic_phase(self, st: _LoopState, ledger: EvidenceLedger, trace: TraceRecorder) -> Optional[CriticResult]:
        audit = ledger.audit()
        alt = alternative_name(ledger, st.finish_args.diagnosis, [d.name for d in st.finish_args.differential_diagnoses])  # type: ignore[union-attr]
        critic = run_visual_critic(self.llm, st.finish_args, alt, self._critic_image_parts(ledger.cited_full_primary_ids()), trace)  # type: ignore[arg-type]
        # Record audit findings, but do not open another tool loop: that pass can replace the diagnosis.
        critic = merge_audit(critic, audit, can_revisit=False)
        if audit:
            critic.confirmed = False
        trace.log("critic_result", result=critic.model_dump() if critic else None)
        if not critic.confirmed:
            st.withhold_reason = critic.issues[0].detail if critic.issues else "Independent image read did not confirm the candidate."
        return critic

    def _critic_image_parts(self, image_ids: List[str]) -> List[dict]:
        from ..imaging.encode import encode_jpeg_data_url, load_image
        parts = []
        for iid in image_ids:
            im = self.repo.get_image(iid)
            se = self.repo.get_series(im.series_id)
            st = self.repo.get_study(se.study_id)
            parts.append({
                "label": f"[img:{iid} | {st.modality} | {se.caption or '(no caption)'} | slice {im.slice_index}/{se.count}]",
                "data_url": encode_jpeg_data_url(load_image(self.repo.resolve_image(iid)), self.settings.max_image_side, self.settings.jpeg_quality),
            })
        return parts

    # ------------------------------------------------------------------ context
    def _trim_images(self, st: _LoopState) -> None:
        trim_image_messages(st.image_msgs, self.settings.image_keep_messages)


def _message_is_sheet(msg: Dict[str, Any]) -> bool:
    return any(p.get("type") == "text" and "contact sheet" in p.get("text", "") for p in msg["content"])


def trim_image_messages(image_msgs: List[tuple], keep: int) -> None:
    """Keep at most `keep` images the model has already been shown.

    A trailing True marks images added since the last request; those stay so the
    model can describe what it just fetched. Oldest contact sheets are dropped first.
    """
    located = []
    for mi, item in enumerate(image_msgs):
        msg = item[1]
        pending = len(item) > 2 and bool(item[2])
        sheet = _message_is_sheet(msg)
        for pi, part in enumerate(msg["content"]):
            if part.get("type") == "image_url":
                located.append((mi, pi, sheet, pending))
    pending_n = sum(1 for item in located if item[3])
    overflow = len(located) - max(keep, pending_n)
    if overflow <= 0:
        return
    droppable = [item for item in located if not item[3]]
    sheets = [item for item in droppable if item[2]]
    full = [item for item in droppable if not item[2]]
    for mi, pi, _, _ in (sheets + full)[:overflow]:
        image_msgs[mi][1]["content"][pi] = {
            "type": "text", "text": "(image shown earlier; your observations are in the ledger)",
        }
