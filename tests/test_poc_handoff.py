"""End-to-end tests for PoC Case #1: Devin v3 -> Session IR -> Workspace Reconciler -> Claude Code JSONL."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from session_ir import align_devin_trace, hydrate_to_claude_jsonl, reconcile_workspace, validate_ir_bundle
from session_ir.aligner import load_jsonl
from session_ir.cli import run_pipeline

FIXTURE_DIR = ROOT / "fixtures" / "poc_case_01_rate_limiter"


def _load_poc_bundle() -> dict:
    session = json.loads((FIXTURE_DIR / "devin_v3_session.json").read_text(encoding="utf-8"))
    messages = json.loads((FIXTURE_DIR / "devin_v3_messages.json").read_text(encoding="utf-8"))
    events = load_jsonl(FIXTURE_DIR / "sandbox_events.jsonl")
    return align_devin_trace(session, messages, events, branch="fix/monotonic-clock-bucket")


def test_dual_channel_alignment_and_schema_invariants() -> None:
    bundle = _load_poc_bundle()
    validate_ir_bundle(bundle)

    assert bundle["ir_version"] == "0.1.0"
    assert bundle["source"]["agent"] == "devin"
    assert len(bundle["events"]) == 4

    facts = {f["id"]: f for f in bundle["epistemic_state"]["verified_facts"]}
    assert facts["F1"]["evidence_event_ids"] == ["evt_02"]
    assert facts["F2"]["evidence_event_ids"] == ["evt_03"]
    assert facts["F3"]["evidence_event_ids"] == ["evt_02"]

    hyps = {h["id"]: h for h in bundle["epistemic_state"]["ruled_out_hypotheses"]}
    assert hyps["H1"]["evidence_event_ids"] == ["evt_02"]
    assert hyps["H2"]["evidence_event_ids"] == ["evt_04"]


def test_workspace_reconciler_detects_git_sha_drift() -> None:
    bundle = _load_poc_bundle()

    # Exact SHA match -> all facts FRESH
    clean = reconcile_workspace(bundle, target_head_sha=bundle["workspace"]["head_sha"])
    assert clean["drifted"] is False
    assert clean["fresh_fact_count"] == 3
    assert clean["stale_fact_count"] == 0

    # Target moved ahead and touched src/limiter/bucket.py -> only F1 becomes STALE
    drifted = reconcile_workspace(
        bundle,
        target_head_sha="deadbeef1234567890abcdef1234567890abcdef",
        changed_files=["src/limiter/bucket.py"],
    )
    assert drifted["drifted"] is True
    assert drifted["fresh_fact_count"] == 2
    assert drifted["stale_fact_count"] == 1
    freshness_by_id = {f["id"]: f["freshness"] for f in drifted["facts"]}
    assert freshness_by_id == {"F1": "STALE", "F2": "FRESH", "F3": "FRESH"}


def test_claude_code_hydrator_emits_paired_tool_calls_and_cold_blobs(tmp_path: Path) -> None:
    stats = run_pipeline(FIXTURE_DIR, tmp_path)

    jsonl_path = Path(stats["jsonl_path"])
    records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # Preamble + 3 causal frontier tool pairs (evt_02, evt_03, evt_04) + final assistant ack
    assert len(records) == 8

    # Verify strict 1-to-1 tool_use -> tool_result pairing (no orphaned tool calls)
    pending_tool_ids: list[str] = []
    for rec in records:
        content = rec["message"]["content"]
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    pending_tool_ids.append(block["id"])
                elif block.get("type") == "tool_result":
                    assert pending_tool_ids, "Unexpected tool_result without matching tool_use"
                    assert block["tool_use_id"] == pending_tool_ids.pop(0)
    assert pending_tool_ids == []

    # Cold 18KB pytest debug log must stay out of hot context (substantial byte reduction)
    assert stats["raw_trace_bytes"] > 18000
    assert stats["hydrated_hot_bytes"] < 6000
    assert stats["context_reduction_pct"] > 70.0
