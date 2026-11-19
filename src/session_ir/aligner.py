"""Dual-channel aligner: merges Devin v3 Control Plane + Sandbox Execution Plane into Session IR."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_KINDS = {
    "fs.read",
    "fs.diff",
    "shell.exec",
    "search.query",
    "web.fetch",
    "vcs.checkpoint",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _match_evidence_events(
    evidence_hint: str,
    files: list[str],
    events: list[dict[str, Any]],
) -> list[str]:
    """Bind epistemic claims to canonical event IDs via explicit IDs, file paths, or command substrings."""
    matched: list[str] = []
    hint_lower = evidence_hint.lower()
    event_ids = {ev["event_id"] for ev in events}

    for token in re.findall(r"evt_[a-zA-Z0-9_-]+", evidence_hint):
        if token in event_ids and token not in matched:
            matched.append(token)

    if matched:
        return matched

    for ev in events:
        payload_str = json.dumps(ev.get("payload", {})).lower()
        file_hit = any(f.lower() in payload_str for f in files)
        cmd = str(ev.get("payload", {}).get("command", "")).lower()
        cmd_hit = bool(cmd and (cmd in hint_lower or any(tok in cmd for tok in hint_lower.split() if len(tok) >= 6)))
        if file_hit or cmd_hit:
            if ev["event_id"] not in matched:
                matched.append(ev["event_id"])

    if not matched and events:
        matched.append(events[-1]["event_id"])
    return matched


def align_devin_trace(
    session_payload: dict[str, Any],
    messages_payload: dict[str, Any],
    sandbox_events: list[dict[str, Any]],
    *,
    repo_url: str = "",
    branch: str = "main",
    base_sha: str | None = None,
) -> dict[str, Any]:
    """Merge Devin v3 Control Plane JSON + sandbox events.jsonl into a Session IR 0.1.0 bundle."""
    structured = session_payload.get("structured_output") or {}
    session_id = session_payload.get("session_id", "devin-unknown")
    head_sha = structured.get("git_head_sha") or (sandbox_events[-1]["git_sha"] if sandbox_events else "0000000")
    resolved_base_sha = base_sha or (sandbox_events[0]["git_sha"] if sandbox_events else head_sha)

    canonical_events: list[dict[str, Any]] = []
    for raw in sorted(sandbox_events, key=lambda e: e["timestamp"]):
        ev: dict[str, Any] = {
            "event_id": raw["event_id"],
            "parent_event_ids": raw.get("parent_event_ids", []),
            "timestamp": raw["timestamp"],
            "kind": raw["kind"],
            "git_sha": raw["git_sha"],
            "payload": raw.get("payload", {}),
        }
        if "cwd" in raw:
            ev["cwd"] = raw["cwd"]
        if "exit_code" in raw:
            ev["exit_code"] = raw["exit_code"]
        if "duration_ms" in raw:
            ev["duration_ms"] = raw["duration_ms"]
        if raw.get("blob_refs"):
            ev["blob_refs"] = raw["blob_refs"]
        canonical_events.append(ev)

    verified_facts = []
    for fact in structured.get("verified_facts", []):
        files = fact.get("files", [])
        evidence_ids = _match_evidence_events(fact.get("evidence_summary", ""), files, canonical_events)
        verified_facts.append(
            {
                "id": fact["id"],
                "statement": fact["statement"],
                "files": files,
                "evidence_event_ids": evidence_ids,
            }
        )

    ruled_out = []
    for hyp in structured.get("ruled_out_hypotheses", []):
        evidence_ids = _match_evidence_events(hyp.get("disproof", ""), [], canonical_events)
        ruled_out.append(
            {
                "id": hyp["id"],
                "hypothesis": hyp["hypothesis"],
                "reason": hyp["disproof"],
                "evidence_event_ids": evidence_ids,
            }
        )

    open_constraints = []
    for item in structured.get("open_constraints", []):
        open_constraints.append(
            {
                "id": item["id"],
                "description": item["description"],
                "blocking": bool(item["blocking"]),
                "repro_command": item.get("repro_command"),
            }
        )

    bundle_seed = f"{session_id}:{head_sha}:{len(canonical_events)}:{len(messages_payload.get('items', []))}"
    bundle_id = "sir_" + hashlib.sha256(bundle_seed.encode("utf-8")).hexdigest()[:12]

    bundle: dict[str, Any] = {
        "ir_version": "0.1.0",
        "bundle_id": bundle_id,
        "source": {
            "agent": "devin",
            "session_id": session_id,
            "parent_session_id": session_payload.get("parent_session_id"),
            "child_session_ids": session_payload.get("child_session_ids", []),
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "workspace": {
            "repo_url": repo_url,
            "branch": branch,
            "base_sha": resolved_base_sha,
            "head_sha": head_sha,
            "dirty": False,
            "dirty_diff_blob_ref": None,
        },
        "events": canonical_events,
        "epistemic_state": {
            "objective": structured.get("objective", ""),
            "verified_facts": verified_facts,
            "ruled_out_hypotheses": ruled_out,
            "open_constraints": open_constraints,
            "recommended_next_steps": structured.get("recommended_next_steps", []),
        },
    }
    validate_ir_bundle(bundle)
    return bundle


def validate_ir_bundle(bundle: dict[str, Any]) -> None:
    """Validate structural invariants of schemas/session-ir.schema.json with zero external deps."""
    required_top = ("ir_version", "bundle_id", "source", "workspace", "events", "epistemic_state")
    for key in required_top:
        if key not in bundle:
            raise ValueError(f"Missing required top-level key: {key}")
    if bundle["ir_version"] != "0.1.0":
        raise ValueError(f"Unsupported ir_version: {bundle['ir_version']}")

    ws = bundle["workspace"]
    for sha_key in ("base_sha", "head_sha"):
        if not SHA_PATTERN.match(str(ws.get(sha_key, ""))):
            raise ValueError(f"Invalid git SHA for workspace.{sha_key}: {ws.get(sha_key)}")

    event_ids: set[str] = set()
    for ev in bundle["events"]:
        if ev["kind"] not in VALID_KINDS:
            raise ValueError(f"Invalid event kind: {ev['kind']}")
        if not SHA_PATTERN.match(str(ev.get("git_sha", ""))):
            raise ValueError(f"Invalid git_sha on event {ev.get('event_id')}")
        for blob in ev.get("blob_refs", []):
            if not SHA256_PATTERN.match(str(blob.get("sha256", ""))):
                raise ValueError(f"Invalid sha256 blob_ref on event {ev.get('event_id')}")
        event_ids.add(ev["event_id"])

    ep = bundle["epistemic_state"]
    for fact in ep["verified_facts"]:
        refs = fact.get("evidence_event_ids", [])
        if not refs:
            raise ValueError(f"Fact {fact.get('id')} has no evidence_event_ids")
        missing = [r for r in refs if r not in event_ids]
        if missing:
            raise ValueError(f"Fact {fact.get('id')} references unknown event_ids: {missing}")

    for hyp in ep["ruled_out_hypotheses"]:
        refs = hyp.get("evidence_event_ids", [])
        if not refs:
            raise ValueError(f"Hypothesis {hyp.get('id')} has no evidence_event_ids")
        missing = [r for r in refs if r not in event_ids]
        if missing:
            raise ValueError(f"Hypothesis {hyp.get('id')} references unknown event_ids: {missing}")


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows
