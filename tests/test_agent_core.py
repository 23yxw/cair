"""Unit tests for ledger validation, tool schemas and the JSON transport fallback."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cair_agent.agent.controller import trim_image_messages
from .conftest import requires_data
from cair_agent.agent.critic import merge_audit
from cair_agent.agent.finalizer import constrain_report
from cair_agent.agent.ledger import EvidenceLedger, LedgerError
from cair_agent.agent.prompts import load_prompt
from cair_agent.agent.tools import ToolContext, build_registry, h_update_hypotheses
from cair_agent.config import Settings
from cair_agent.eval.metrics import sampling_metrics
from cair_agent.llm.client import extract_json, parse_json_actions
from cair_agent.llm.schemas import (CriticResult, DiagnosisReport, EvidenceRef, FinishArgs, Hypothesis, KeyFinding,
                                    RecordEvidenceArgs, UpdateHypothesesArgs)


def test_ledger_rejects_unseen_ref():
    led = EvidenceLedger()
    led.mark_seen("img:1", study_id="study-1", series_id="s1", slice_index=5)
    ok = led.record(RecordEvidenceArgs(claim="x", source="image", evidence_refs=[EvidenceRef(image_id="1")], confidence=0.5))
    assert ok.id == "e1"
    assert ok.evidence_refs[0].study_id == "study-1" and ok.evidence_refs[0].slice_index == 5
    with pytest.raises(LedgerError):
        led.record(RecordEvidenceArgs(claim="y", source="image", evidence_refs=[EvidenceRef(image_id="2")], confidence=0.5))
    with pytest.raises(LedgerError):
        led.validate_keys(["img:1", "img:999"])


def test_finish_schema_requires_findings():
    with pytest.raises(Exception):
        FinishArgs(diagnosis="x", key_findings=[])


def test_registry_gating_and_schemas():
    hidden = build_registry("hidden")
    assert "read_study_findings" not in hidden
    visible = build_registry("visible")
    assert "read_study_findings" in visible
    for spec in hidden.values():
        s = spec.openai_schema()
        assert s["function"]["name"] == spec.name
        assert s["function"]["parameters"]["type"] == "object"
    assert hidden["finish"].terminal


def test_prompts_versioned():
    for name in ("controller", "critic", "finalizer"):
        v, body = load_prompt(name)
        assert v.startswith(name) and body


def _ctx(led: EvidenceLedger) -> ToolContext:
    return ToolContext(repo=None, ledger=led, settings=Settings())  # type: ignore[arg-type]


def _direct(claim: str, image_id: str, **extra) -> RecordEvidenceArgs:
    return RecordEvidenceArgs(claim=claim, source="image", support="direct", confidence=0.9,
                              evidence_refs=[EvidenceRef(image_id=image_id)], **extra)


def test_direct_claim_requires_full_primary_image():
    led = EvidenceLedger()
    led.mark_seen("img:sheet", series_id="stack", slice_index=12, resolution="sheet", evidence_grade="primary")
    led.mark_seen("img:note", series_id="ann", slice_index=0, resolution="full", evidence_grade="secondary")
    led.mark_seen("img:full", series_id="stack", slice_index=40, resolution="full", evidence_grade="primary")
    with pytest.raises(LedgerError, match="contact-sheet"):
        led.record(_direct("thick-walled tube", "sheet"))
    with pytest.raises(LedgerError, match="secondary"):
        led.record(_direct("annotated outline of a tube", "note"))
    ok = led.record(_direct("thick-walled tube in the right lower quadrant", "full"))
    assert ok.support == "direct"
    # an overview may cite the sheet
    overview = led.record(RecordEvidenceArgs(claim="overview of the stack", source="image", support="indirect", confidence=0.4,
                                             evidence_refs=[EvidenceRef(image_id="sheet")]))
    assert overview.support == "indirect"


def test_direct_claim_cannot_name_the_leading_diagnosis():
    led = EvidenceLedger()
    led.mark_seen("img:full", series_id="s", slice_index=1, resolution="full", evidence_grade="primary")
    args = UpdateHypothesesArgs(hypotheses=[
        Hypothesis(name="Acute appendicitis", probability=0.4, rationale="referral"),
        Hypothesis(name="Diverticulitis", probability=0.3, rationale="alternative"),
    ])
    assert not h_update_hypotheses(_ctx(led), args).error
    with pytest.raises(LedgerError, match="Acute appendicitis"):
        led.record(_direct("findings consistent with acute appendicitis", "full"))
    led.record(RecordEvidenceArgs(claim="consistent with acute appendicitis", source="image", support="indirect", confidence=0.6,
                                  evidence_refs=[EvidenceRef(image_id="full")]))


def test_hypotheses_stay_uncommitted_before_images_and_need_two():
    with pytest.raises(ValidationError):
        UpdateHypothesesArgs(hypotheses=[Hypothesis(name="Only one", probability=0.4)])
    led = EvidenceLedger()
    hot = UpdateHypothesesArgs(hypotheses=[
        Hypothesis(name="Acute appendicitis", probability=0.7, rationale="referral"),
        Hypothesis(name="Other", probability=0.2, rationale=""),
    ])
    err = h_update_hypotheses(_ctx(led), hot)
    assert err.error and "0.50" in err.text
    assert led.hypothesis_history == []
    capped = UpdateHypothesesArgs(hypotheses=[
        Hypothesis(name="Acute appendicitis", probability=0.5, rationale="referral"),
        Hypothesis(name="Other", probability=0.2, rationale=""),
    ])
    assert not h_update_hypotheses(_ctx(led), capped).error


def test_study_findings_source_matches_policy_and_ref():
    led = EvidenceLedger()
    led.mark_seen("text:findings:study-1", study_id="study-1")
    led.mark_seen("img:9", series_id="s", slice_index=0, resolution="full", evidence_grade="primary")
    with pytest.raises(LedgerError, match="hidden"):
        led.record(RecordEvidenceArgs(claim="Agatston 1881", source="study_findings", support="direct", confidence=1,
                                      evidence_refs=[EvidenceRef(text_id="findings:study-1")]))
    led.findings_policy = "visible"
    with pytest.raises(LedgerError, match="text:findings"):
        led.record(RecordEvidenceArgs(claim="Agatston 1881", source="study_findings", support="direct", confidence=1,
                                      evidence_refs=[EvidenceRef(image_id="9")]))
    ok = led.record(RecordEvidenceArgs(claim="Agatston 1881", source="study_findings", support="direct", confidence=1,
                                      evidence_refs=[EvidenceRef(text_id="findings:study-1")]))
    assert ok.source == "study_findings"


def test_finish_gate_asks_once_then_downgrades_sheet_claims():
    led = EvidenceLedger()
    led.mark_seen("img:tile", series_id="stack", slice_index=20, resolution="sheet", evidence_grade="primary")
    # bypass record(): the gate is the backstop for a direct claim that only cites a sheet
    from cair_agent.llm.schemas import Evidence
    led.entries.append(Evidence(id="e1", claim="tube on the overview", source="image", support="direct", confidence=0.9,
                                evidence_refs=[EvidenceRef(image_id="tile", series_id="stack", slice_index=20)]))
    asked: set = set()
    common = dict(calls_left=3, images_left=4, revisit_keys=None, revisit_entry_count=0, asked=asked)
    first = led.finish_gate(allow_sampling=True, **common)
    assert not first.accept and "stack" in first.error and "neighbors" in first.error
    assert asked == {"stack"}
    second = led.finish_gate(allow_sampling=True, **common)
    assert second.accept and second.downgraded == ["e1"]
    assert led.entries[0].support == "indirect" and led.entries[0].confidence == 0.6
    blocked = led.finish_gate(allow_sampling=False, calls_left=0, images_left=0, revisit_keys=None, revisit_entry_count=0, asked=set())
    assert blocked.accept  # nothing sheet-direct remains


def test_revisit_finish_waits_for_a_new_look_or_a_retraction():
    led = EvidenceLedger()
    led.mark_seen("img:1", series_id="s", slice_index=1, resolution="full", evidence_grade="primary")
    led.record(_direct("gap in the tendon", "1"))
    baseline = led.review_keys()
    common = dict(calls_left=2, images_left=2, allow_sampling=True, revisit_keys=baseline, revisit_entry_count=len(led.entries), asked=set())
    assert not led.finish_gate(**common).accept
    led.record(RecordEvidenceArgs(claim="the gap is fluid, not a partial defect", source="image", support="direct", confidence=0.8,
                                  evidence_refs=[EvidenceRef(image_id="1")], contradictions=["e1"]))
    assert led.finish_gate(**common).accept

    led2 = EvidenceLedger()
    led2.mark_seen("img:1", series_id="s", slice_index=1, resolution="full", evidence_grade="primary")
    base2 = led2.review_keys()
    led2.mark_seen("img:2", series_id="s", slice_index=2, resolution="full", evidence_grade="primary")
    assert led2.finish_gate(calls_left=1, images_left=1, allow_sampling=True, revisit_keys=base2, revisit_entry_count=0, asked=set()).accept


def test_audit_flags_complete_versus_partial_and_missing_full_res_support():
    led = EvidenceLedger()
    led.mark_seen("img:1", series_id="mri", slice_index=3, resolution="full", evidence_grade="primary")
    led.record(_direct("sagittal image shows a complete tendon gap", "1"))
    led.record(_direct("the same slice also fits a partial tear", "1"))
    hard = [a for a in led.audit() if a.hard and a.type == "contradiction"]
    assert hard and "complete" in hard[0].detail

    sheets = EvidenceLedger()
    sheets.mark_seen("img:t", series_id="ct", slice_index=30, resolution="sheet", evidence_grade="primary")
    sheets.record(RecordEvidenceArgs(claim="overview only", source="image", support="indirect", confidence=0.4,
                                     evidence_refs=[EvidenceRef(image_id="t")]))
    audit = sheets.audit()
    assert any(a.hard and "ct" in a.detail and "neighbors" in a.action for a in audit)
    forced = merge_audit(CriticResult(issues=[], decision="finalize"), audit, can_revisit=True)
    assert forced.decision == "revisit" and forced.issues
    held = merge_audit(CriticResult(issues=[], decision="finalize"), audit, can_revisit=False)
    assert held.decision == "finalize" and held.issues


def test_finalizer_pins_diagnosis_and_copies_ledger_claims():
    led = EvidenceLedger()
    led.mark_seen("img:1", series_id="s", slice_index=1, resolution="full", evidence_grade="primary")
    led.record(_direct("dilated thick-walled tube", "1"))
    candidate = FinishArgs(diagnosis="Acute appendicitis with phlegmon", key_findings=[
        KeyFinding(fact="dilated thick-walled tube", source="image", evidence_refs=["img:1"], confidence=0.9)])
    rewritten = DiagnosisReport(
        diagnosis="Appendicitis", trace_id="t",
        key_findings=[
            KeyFinding(fact="dilated thick-walled tube", source="image", evidence_refs=["img:1"], confidence=0.9),
            KeyFinding(fact="invented abscess", source="image", evidence_refs=["img:1"], confidence=0.5),
        ])
    out = constrain_report(rewritten, candidate, led)
    assert out.diagnosis == "Acute appendicitis with phlegmon"
    assert [k.fact for k in out.key_findings] == ["dilated thick-walled tube"]
    assert out.degraded is False

    missed = DiagnosisReport(diagnosis="Appendicitis", trace_id="t", key_findings=[
        KeyFinding(fact="invented abscess", source="image", evidence_refs=["img:1"], confidence=0.5)])
    fallback = constrain_report(missed, candidate, led)
    assert fallback.degraded and fallback.diagnosis == candidate.diagnosis
    assert fallback.key_findings[0].fact == "dilated thick-walled tube"


def test_image_messages_are_kept_by_count_and_sheets_go_first():
    def msg(label: str):
        return {"role": "user", "content": [{"type": "text", "text": label}, {"type": "image_url", "image_url": {"url": "x"}}]}

    def still_image(m):
        return any(p.get("type") == "image_url" for p in m["content"])

    kept = [(0, msg("full")), (1, msg("full two"))]
    trim_image_messages(kept, 4)
    assert all(still_image(m) for _, m in kept)

    five = [(i, msg(f"full {i}")) for i in range(5)]
    trim_image_messages(five, 4)
    assert not still_image(five[0][1]) and all(still_image(m) for _, m in five[1:])

    mixed = [(0, msg("[contact sheet | a]")), (1, msg("[contact sheet | b]")),
             (2, msg("full a")), (3, msg("full b")), (4, msg("full c"))]
    trim_image_messages(mixed, 4)
    assert not still_image(mixed[0][1]) and still_image(mixed[1][1]) and still_image(mixed[2][1])

    packed = {"role": "user", "content": [{"type": "text", "text": "full batch"}]}
    packed["content"].extend({"type": "image_url", "image_url": {"url": str(i)}} for i in range(6))
    batch = [(0, packed)]
    trim_image_messages(batch, 4)
    kept = [p for p in packed["content"] if p.get("type") == "image_url"]
    assert len(kept) == 4 and kept[0]["image_url"]["url"] == "2"

    fresh = {"role": "user", "content": [{"type": "text", "text": "just fetched"}]}
    fresh["content"].extend({"type": "image_url", "image_url": {"url": str(i)}} for i in range(5))
    pending = [(0, msg("already shown"), False), (1, fresh, True)]
    trim_image_messages(pending, 4)
    assert not still_image(pending[0][1])
    assert sum(1 for p in fresh["content"] if p.get("type") == "image_url") == 5


def test_sampling_metrics_distinguish_sheet_from_full_resolution():
    events = [
        {"type": "tool_result", "name": "list_series", "error": False,
         "data": {"series": [{"series_id": "a"}, {"series_id": "b"}]}},
        {"type": "tool_result", "name": "sample_images", "error": False, "image_ids": ["1", "2"],
         "data": {"series_id": "a", "as_contact_sheet": True}},
        {"type": "tool_result", "name": "sample_images", "error": False, "image_ids": ["9"],
         "data": {"series_id": "b", "as_contact_sheet": False}},
        {"type": "evidence_added", "support": "direct", "source": "image",
         "evidence_refs": [{"image_id": "1"}]},
        {"type": "evidence_added", "support": "direct", "source": "image",
         "evidence_refs": [{"image_id": "9"}]},
    ]
    m = sampling_metrics(events)
    assert m["sheet_only_direct"] == 1
    assert m["full_res_series_coverage"] == 0.5


def test_cited_full_primary_ids_skip_sheets_and_keep_four():
    led = EvidenceLedger()
    led.mark_seen("img:sheet", series_id="s", slice_index=1, resolution="sheet", evidence_grade="primary")
    led.mark_seen("img:note", series_id="a", slice_index=0, resolution="full", evidence_grade="secondary")
    for i, conf in enumerate((0.2, 0.9, 0.4, 0.7, 0.5)):
        iid = f"f{i}"
        led.mark_seen(f"img:{iid}", series_id="s", slice_index=i, resolution="full", evidence_grade="primary")
        led.record(RecordEvidenceArgs(claim=f"finding {i}", source="image", support="direct", confidence=conf,
                                      evidence_refs=[EvidenceRef(image_id=iid)]))
    led.record(RecordEvidenceArgs(claim="overview", source="image", support="indirect", confidence=1.0,
                                  evidence_refs=[EvidenceRef(image_id="sheet")]))
    from cair_agent.llm.schemas import Evidence
    led.entries.append(Evidence(id="ex", claim="annotation", source="image", support="direct", confidence=1.0,
                                evidence_refs=[EvidenceRef(image_id="note")]))
    assert led.cited_full_primary_ids() == ["f1", "f3", "f4", "f2"]


def test_critic_choice_selects_the_other_hypothesis_and_can_reject_both():
    from cair_agent.agent.critic import BlindRead, CriticChoice, alternative_name, decision_from_choice, run_visual_critic
    led = EvidenceLedger()
    led.update_hypotheses(UpdateHypothesesArgs(hypotheses=[
        Hypothesis(name="Adjacent segment disease", probability=0.45),
        Hypothesis(name="Spondylolisthesis", probability=0.35),
    ]))
    assert alternative_name(led, "Adjacent segment disease", ["Degenerative disc disease"]) == "Spondylolisthesis"
    blind = BlindRead(visible="L5 has slipped forward on S1", supports=["spondylolisthesis"], missing_view="null")
    agreed = decision_from_choice(CriticChoice(choice="Candidate"), blind, "L5-S1 spondylolisthesis", "Adjacent segment disease")
    assert agreed.decision == "finalize" and agreed.confirmed and agreed.issues == []
    stamped = decision_from_choice(
        CriticChoice(choice="candidate"),
        BlindRead(visible="thickened vertical trabeculae", supports=["Vertebral hemangioma", "Metastasis"]),
        "Bladder cancer with bone metastasis", "Bone island")
    assert not stamped.confirmed and "hemangioma" in stamped.issues[0].detail.casefold()
    disagreed = decision_from_choice(CriticChoice(choice="alternative"), blind, "Adjacent segment disease", "Spondylolisthesis")
    assert disagreed.decision == "finalize" and not disagreed.confirmed and "Spondylolisthesis" in disagreed.issues[0].detail
    rejected = decision_from_choice(CriticChoice(choice="neither", seen_as="patent urachus"), blind,
                                   "Posterior urethral valves", "Bladder diverticulum")
    assert not rejected.confirmed and "patent urachus" in rejected.issues[0].detail

    class Boom:
        def complete(self, *args, **kwargs):
            raise AssertionError("no images, no call")

    skipped = run_visual_critic(Boom(), FinishArgs(diagnosis="x", key_findings=[
        KeyFinding(fact="f", source="image", evidence_refs=["img:1"], confidence=0.5)]), "y", [])  # type: ignore[arg-type]
    assert skipped.unavailable and not skipped.confirmed


def test_unconfirmed_diagnosis_is_withheld_without_the_reference_text():
    from cair_agent.agent.finalizer import withhold_diagnosis
    from cair_agent.eval.judge import withhold_incompatible
    report = DiagnosisReport(diagnosis="Pelvic lymphoma", trace_id="t", key_findings=[
        KeyFinding(fact="a mass", source="image", evidence_refs=["img:1"], confidence=0.8)])
    held = withhold_diagnosis(report, "Pelvic lymphoma", "Blind read did not confirm it.")
    assert held.diagnosis == "undetermined" and held.degraded
    assert any("Pelvic lymphoma" in u for u in held.uncertainty)
    graded = DiagnosisReport(diagnosis="Acute appendicitis", trace_id="t", key_findings=[
        KeyFinding(fact="a tube", source="image", evidence_refs=["img:1"], confidence=0.8)])
    withheld = withhold_incompatible(graded)
    assert withheld.diagnosis == "undetermined" and "discussion" not in " ".join(withheld.uncertainty).casefold()


def test_unseen_fetch_cannot_be_recorded_and_errors_hide_paths():
    from cair_agent.agent.tools import ledger_blocked_by_fetch, note_displayed, public_error, ImagePart
    assert ledger_blocked_by_fetch("record_evidence", ["sample_images", "record_evidence"])
    assert ledger_blocked_by_fetch("finish", ["zoom_image", "finish"])
    assert not ledger_blocked_by_fetch("record_evidence", ["record_evidence"])
    hidden = public_error(FileNotFoundError("/Users/secret/case/img.jpg missing"))
    assert "/Users" not in hidden and "[path]" in hidden
    led = EvidenceLedger()

    class _Im:
        image_id = "9"
        study_id = "study-1"
        series_id = "s"
        slice_index = 3

    class _St:
        evidence_grade = "primary"
        case_id = "case-01"

    class _Repo:
        def get_image(self, image_id):
            return _Im()

        def get_study(self, study_id):
            return _St()

    ctx = ToolContext(repo=_Repo(), ledger=led, settings=Settings(), case_id="case-01")  # type: ignore[arg-type]
    assert "img:9" not in led.seen
    note_displayed(ctx, ImagePart(label="x", data_url="u", image_ids=["9"], resolution="sheet"))
    assert led.seen_meta["img:9"]["resolution"] == "sheet"


@requires_data
def test_tools_reject_another_case(repo):
    from cair_agent.agent.tools import h_get_case_profile, h_sample_images
    from cair_agent.llm.schemas import GetCaseProfileArgs, SampleImagesArgs
    ctx = ToolContext(repo=repo, ledger=EvidenceLedger(), settings=Settings(), case_id="case-01")
    foreign = h_get_case_profile(ctx, GetCaseProfileArgs(case_id="case-02"))
    assert foreign.error and "case-02" not in foreign.text
    other = repo.list_series(repo.list_studies("case-02")[0].study_id)[0]
    sampled = h_sample_images(ctx, SampleImagesArgs(series_id=other.series_id, strategy="anchors"))
    assert sampled.error and "case-02" not in sampled.text and "/" not in sampled.text


def test_json_protocol_fallback():
    calls = parse_json_actions('thinking...\n{"action": "list_studies", "args": {"case_id": "case-01"}}')
    assert calls and calls[0].name == "list_studies" and calls[0].arguments == {"case_id": "case-01"}
    assert parse_json_actions("no json here") == []
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("prefix {\"a\": {\"b\": 2}} suffix") == {"a": {"b": 2}}
    assert extract_json("nothing") is None
