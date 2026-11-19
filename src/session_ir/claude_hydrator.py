"""Target Hydrator for Claude Code: compiles reconciled Session IR into valid JSONL history."""

from __future__ import annotations

import json
from typing import Any


def _format_handoff_preamble(bundle: dict[str, Any], reconciliation: dict[str, Any], blob_dir: str) -> str:
    ep = bundle["epistemic_state"]
    lines = [
        f"# Cross-Agent Session Handoff ({bundle['source']['agent']} -> claude-code)",
        f"- Bundle ID: `{bundle['bundle_id']}` | Source Session: `{bundle['source']['session_id']}`",
        f"- Exported HEAD: `{reconciliation['exported_head_sha']}` | Target HEAD: `{reconciliation['target_head_sha']}` | Drifted: `{reconciliation['drifted']}`",
    ]
    if reconciliation["drifted"]:
        lines.append(f"- Modified files since export: {', '.join(f'`{f}`' for f in reconciliation['changed_files'])}")
    lines.append(f"- Cold blob store: `{blob_dir}` (inspect on demand; not inlined into hot context)")
    lines.append("")
    lines.append(f"## Objective\n{ep['objective']}")
    lines.append("")
    lines.append("## Verified Facts")
    for f in reconciliation["facts"]:
        tag = f"[{f['freshness']}]"
        drift_note = f" (re-verify: {', '.join(f['drifted_files'])})" if f["drifted_files"] else ""
        ev_refs = ", ".join(f["evidence_event_ids"])
        lines.append(f"- {tag} **{f['id']}**: {f['statement']} _(evidence: {ev_refs})_{drift_note}")

    lines.append("")
    lines.append("## Ruled-Out Hypotheses (Do NOT Re-Test)")
    for h in ep["ruled_out_hypotheses"]:
        ev_refs = ", ".join(h["evidence_event_ids"])
        lines.append(f"- **{h['id']}**: {h['hypothesis']} -> disproved because: {h['reason']} _(evidence: {ev_refs})_")

    lines.append("")
    lines.append("## Open Constraints & Repro")
    for c in ep["open_constraints"]:
        repro = f" | repro: `{c['repro_command']}`" if c.get("repro_command") else ""
        lines.append(f"- **{c['id']}** (blocking={c['blocking']}): {c['description']}{repro}")

    if ep.get("recommended_next_steps"):
        lines.append("")
        lines.append("## Recommended Next Steps")
        for idx, step in enumerate(ep["recommended_next_steps"], 1):
            lines.append(f"{idx}. {step}")

    return "\n".join(lines)


def _event_to_claude_tool_pair(ev: dict[str, Any], blob_dir: str) -> tuple[dict[str, Any], dict[str, Any]]:
    tool_use_id = f"toolu_sir_{ev['event_id']}"
    kind = ev["kind"]
    payload = ev.get("payload", {})

    if kind == "shell.exec":
        tool_name = "Bash"
        tool_input = {"command": payload.get("command", "")}
    elif kind == "fs.read":
        tool_name = "Read"
        tool_input = {"file_path": payload.get("path", "")}
        if payload.get("line_range"):
            tool_input["offset"] = payload["line_range"][0]
            tool_input["limit"] = payload["line_range"][1] - payload["line_range"][0] + 1
    elif kind == "search.query":
        tool_name = "Grep"
        tool_input = {"pattern": payload.get("query", "")}
    else:
        tool_name = "Bash"
        tool_input = {"command": f"# canonical {kind}: {json.dumps(payload, sort_keys=True)}"}

    result_parts = [f"git_sha={ev['git_sha']}"]
    if ev.get("exit_code") is not None:
        result_parts.append(f"exit_code={ev['exit_code']}")
    for blob in ev.get("blob_refs", []):
        preview = blob.get("preview", "").strip()
        result_parts.append(
            f"[{blob['role']} {blob['bytes']}B -> cold blob {blob_dir}/{blob['sha256']}]\n{preview}"
        )
    if not ev.get("blob_refs"):
        result_parts.append(json.dumps(payload, ensure_ascii=False))

    assistant_msg = {
        "type": "assistant",
        "source_event_id": ev["event_id"],
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            ],
        },
    }
    user_tool_result = {
        "type": "user",
        "source_event_id": ev["event_id"],
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": bool(ev.get("exit_code") not in (None, 0)),
                    "content": "\n".join(result_parts),
                }
            ],
        },
    }
    return assistant_msg, user_tool_result


def hydrate_to_claude_jsonl(
    bundle: dict[str, Any],
    reconciliation: dict[str, Any],
    *,
    blob_dir: str = ".agent-trace/blobs",
    max_frontier_events: int = 6,
) -> list[dict[str, Any]]:
    """Compile reconciled Session IR into schema-valid Claude Code JSONL records."""
    records: list[dict[str, Any]] = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": _format_handoff_preamble(bundle, reconciliation, blob_dir),
            },
        }
    ]

    # Select only events cited as evidence by verified_facts or ruled_out_hypotheses (causal frontier)
    cited_ids: list[str] = []
    for f in bundle["epistemic_state"]["verified_facts"]:
        for eid in f["evidence_event_ids"]:
            if eid not in cited_ids:
                cited_ids.append(eid)
    for h in bundle["epistemic_state"]["ruled_out_hypotheses"]:
        for eid in h["evidence_event_ids"]:
            if eid not in cited_ids:
                cited_ids.append(eid)

    events_by_id = {ev["event_id"]: ev for ev in bundle["events"]}
    selected = [events_by_id[eid] for eid in cited_ids if eid in events_by_id][:max_frontier_events]

    for ev in selected:
        assistant_msg, tool_result_msg = _event_to_claude_tool_pair(ev, blob_dir)
        records.append(assistant_msg)
        records.append(tool_result_msg)

    records.append(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Handoff context hydrated. Ready to apply the fix and verify "
                            "against the reproduction command without re-running ruled-out investigations."
                        ),
                    }
                ],
            },
        }
    )
    return records
