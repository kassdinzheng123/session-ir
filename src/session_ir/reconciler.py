"""Workspace Reconciler: checks git_sha drift and tags epistemic facts FRESH vs STALE."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _git_changed_files(repo_path: str | Path, from_sha: str, to_sha: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", f"{from_sha}..{to_sha}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def reconcile_workspace(
    bundle: dict[str, Any],
    *,
    target_head_sha: str,
    changed_files: list[str] | None = None,
    repo_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare bundle.workspace.head_sha with target_head_sha and annotate fact freshness."""
    exported_sha = bundle["workspace"]["head_sha"]
    drifted = exported_sha != target_head_sha

    if not drifted:
        diff_files: list[str] = []
    elif changed_files is not None:
        diff_files = sorted(set(changed_files))
    elif repo_path is not None:
        diff_files = _git_changed_files(repo_path, exported_sha, target_head_sha)
    else:
        raise ValueError("Workspace drifted; provide changed_files or repo_path to compute drift diff")

    diff_set = set(diff_files)
    annotated_facts: list[dict[str, Any]] = []
    stale_count = 0
    for fact in bundle["epistemic_state"]["verified_facts"]:
        touched = [f for f in fact.get("files", []) if f in diff_set]
        status = "STALE" if touched else "FRESH"
        if status == "STALE":
            stale_count += 1
        annotated_facts.append(
            {
                **fact,
                "freshness": status,
                "drifted_files": touched,
            }
        )

    return {
        "exported_head_sha": exported_sha,
        "target_head_sha": target_head_sha,
        "drifted": drifted,
        "changed_files": diff_files,
        "fresh_fact_count": len(annotated_facts) - stale_count,
        "stale_fact_count": stale_count,
        "facts": annotated_facts,
    }
