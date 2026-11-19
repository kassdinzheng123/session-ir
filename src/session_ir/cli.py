"""CLI entrypoint for end-to-end Session IR alignment, reconciliation, and Claude Code hydration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .aligner import align_devin_trace, load_jsonl
from .claude_hydrator import hydrate_to_claude_jsonl
from .reconciler import reconcile_workspace


def run_pipeline(
    fixture_dir: Path,
    out_dir: Path,
    *,
    target_head_sha: str | None = None,
    changed_files: list[str] | None = None,
) -> dict[str, int | float | str | bool]:
    session_payload = json.loads((fixture_dir / "devin_v3_session.json").read_text(encoding="utf-8"))
    messages_payload = json.loads((fixture_dir / "devin_v3_messages.json").read_text(encoding="utf-8"))
    sandbox_events = load_jsonl(fixture_dir / "sandbox_events.jsonl")

    bundle = align_devin_trace(
        session_payload,
        messages_payload,
        sandbox_events,
        repo_url=session_payload.get("repo_url", "https://github.com/example/rate-limiter"),
        branch="fix/monotonic-clock-bucket",
    )

    resolved_target_sha = target_head_sha or bundle["workspace"]["head_sha"]
    reconciliation = reconcile_workspace(
        bundle,
        target_head_sha=resolved_target_sha,
        changed_files=changed_files if changed_files is not None else [],
    )
    claude_records = hydrate_to_claude_jsonl(
        bundle,
        reconciliation,
        blob_dir=str(fixture_dir / "blobs"),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    ir_path = out_dir / "session-ir.bundle.json"
    jsonl_path = out_dir / "claude-code-hydrated.jsonl"

    ir_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    jsonl_text = "\n".join(json.dumps(r, ensure_ascii=False) for r in claude_records) + "\n"
    jsonl_path.write_text(jsonl_text, encoding="utf-8")

    # Compute raw cold+messages input size vs hydrated hot context size
    blobs_dir = fixture_dir / "blobs"
    raw_blob_bytes = sum(p.stat().st_size for p in blobs_dir.glob("*") if p.is_file()) if blobs_dir.exists() else 0
    raw_messages_bytes = (fixture_dir / "devin_v3_messages.json").stat().st_size
    raw_total_bytes = raw_blob_bytes + raw_messages_bytes
    hot_context_bytes = len(jsonl_text.encode("utf-8"))
    reduction_pct = round((1.0 - hot_context_bytes / max(raw_total_bytes, 1)) * 100.0, 1)

    return {
        "bundle_id": bundle["bundle_id"],
        "canonical_events": len(bundle["events"]),
        "verified_facts": len(bundle["epistemic_state"]["verified_facts"]),
        "ruled_out_hypotheses": len(bundle["epistemic_state"]["ruled_out_hypotheses"]),
        "drifted": reconciliation["drifted"],
        "fresh_facts": reconciliation["fresh_fact_count"],
        "stale_facts": reconciliation["stale_fact_count"],
        "claude_jsonl_records": len(claude_records),
        "raw_trace_bytes": raw_total_bytes,
        "hydrated_hot_bytes": hot_context_bytes,
        "context_reduction_pct": reduction_pct,
        "ir_path": str(ir_path),
        "jsonl_path": str(jsonl_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Session IR and hydrate to Claude Code JSONL")
    parser.add_argument("fixture_dir", type=Path, help="Directory with Devin v3 JSON + sandbox_events.jsonl")
    parser.add_argument("--out-dir", type=Path, default=Path("out"), help="Output directory")
    parser.add_argument("--target-sha", type=str, default=None, help="Target git HEAD SHA for drift check")
    parser.add_argument("--changed-file", action="append", default=None, help="Simulated modified file since export")
    args = parser.parse_args()

    stats = run_pipeline(
        args.fixture_dir,
        args.out_dir,
        target_head_sha=args.target_sha,
        changed_files=args.changed_file,
    )
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
