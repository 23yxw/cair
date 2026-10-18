"""Unit tests for ledger validation, tool schemas and the JSON transport fallback."""

from __future__ import annotations

import pytest

from cair_agent.agent.ledger import EvidenceLedger, LedgerError
from cair_agent.agent.prompts import load_prompt
from cair_agent.agent.tools import build_registry
from cair_agent.llm.client import extract_json, parse_json_actions
from cair_agent.llm.schemas import EvidenceRef, FinishArgs, RecordEvidenceArgs


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


def test_json_protocol_fallback():
    calls = parse_json_actions('thinking...\n{"action": "list_studies", "args": {"case_id": "case-01"}}')
    assert calls and calls[0].name == "list_studies" and calls[0].arguments == {"case_id": "case-01"}
    assert parse_json_actions("no json here") == []
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("prefix {\"a\": {\"b\": 2}} suffix") == {"a": {"b": 2}}
    assert extract_json("nothing") is None
