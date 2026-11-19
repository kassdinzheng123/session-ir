# Session IR — Architecture Specification

## 1. Problem Statement

Current multi-agent coding systems coordinate agents by exchanging natural-language summaries over message passing. Natural-language handoffs fail in real repositories for two reasons:
- **Lossy compression**: Summaries omit exact file lines, failing command exit codes, and dead ends already tested, forcing the next agent to re-explore the codebase from scratch.
- **Unverifiable claims**: Without binding claims to concrete observations and a specific `git_sha`, the receiving agent cannot distinguish verified facts from stale guesses.

Conversely, passing raw transcripts directly across heterogeneous runtimes (e.g., Devin → Claude Code) causes **cross-model context schizophrenia** due to incompatible system prompts, tool schemas, and private reasoning tokens.

**Session IR** sits between raw transcripts and lossy prose: a typed, git-anchored intermediate representation that preserves verifiable evidence while abstracting away vendor-specific runtime formats.

---

## 2. Data Model

A **Session IR Bundle** (`schemas/session-ir.schema.json`) consists of four sections:

### 2.1 Metadata & Lineage
- `ir_version`: Semver string (`"0.1.0"`).
- `bundle_id`: Unique identifier for this exported slice.
- `source`: `{ agent: "devin" | "claude-code" | "codex", session_id, parent_session_id, exported_at }`.
- `workspace`: `{ repo_url, branch, base_sha, head_sha, dirty_diff_blob_ref }`.

### 2.2 Layer 1 — Canonical Action & Observation Graph
All runtime-specific tool invocations are normalized into six minimal primitives:

| Primitive | Payload | Evidence Anchor |
| --- | --- | --- |
| `fs.read` | `path`, `line_range` | `content_hash`, `git_sha` |
| `fs.diff` | `paths[]`, `stats` | `diff_blob_ref`, `before_sha`, `after_sha` |
| `shell.exec` | `command`, `cwd`, `exit_code`, `duration_ms` | `stdout_blob_ref`, `stderr_blob_ref`, `git_sha` |
| `search.query` | `engine` (`rg`/`ast`/`wiki`), `query`, `match_count` | `results_blob_ref`, `git_sha` |
| `web.fetch` | `url`, `status` | `body_blob_ref` |
| `vcs.checkpoint` | `branch`, `commit_sha`, `pr_url` | `git_sha` |

- **Content-Addressed Cold Storage**: Large stdout logs, stack traces, and diffs are hashed (`sha256:<hex>`) and stored as external blobs rather than inlined into hot prompt context.
- **Causal Edges**: Events reference `parent_event_ids[]`, allowing DAG slicing (e.g., exporting only the causal subgraph leading to root-cause isolation).

### 2.3 Layer 2 — Epistemic State
Private model CoT is intentionally discarded. Instead, the producer maintains three model-neutral epistemic collections:
1. **`verified_facts[]`**: Concrete claims (`id`, `statement`, `files[]`, `evidence_event_ids[]`, `confidence`). Every fact must cite at least one Canonical Event ID.
2. **`ruled_out_hypotheses[]`**: Approaches or causes already disproven (`id`, `hypothesis`, `reason`, `evidence_event_ids[]`). Prevents the receiving agent from repeating failed experiments.
3. **`open_constraints[]`**: Remaining requirements, invariants, failing test commands, and explicit next actions (`id`, `description`, `blocking`, `repro_command`).

### 2.4 Layer 3 — Target Hydrator & Workspace Reconciliation

Before injecting a Session IR bundle into a target agent (e.g., Claude Code), the **Workspace Reconciler** runs:

```text
1. Inspect target local worktree -> compute target_head_sha + working_tree_diff
2. Compare against bundle.workspace.head_sha:
   - Exact match: mark all file-anchored facts FRESH
   - Diverged: compute `git diff bundle.workspace.head_sha..target_head_sha`
     - Any fact referencing untouched files -> FRESH
     - Any fact referencing modified files -> STALE (annotate with delta diff)
3. Synthesize target-native context:
   - Emit compact epistemic state + fresh/stale annotations + repro commands
   - Compile selected high-signal canonical events into target-valid message history
     (e.g., Claude Code JSONL with valid tool_use / tool_result pairings)
   - Register cold-blob lookup pointer so the target agent can inspect full logs on demand
```

---

## 3. Dual-Channel Trace Extraction for Devin

Because Devin runs in a managed cloud sandbox and does not expose raw internal CoT streams, extraction splits cleanly across two supported surfaces:

### Channel A: Control Plane (Devin v3 API + Official MCP)
- **Session Creation**: `POST /v3/organizations/{org_id}/sessions`
  - Passes `schemas/devin-epistemic-output.schema.json` in `structured_output_schema` with `structured_output_required: true`.
  - Devin continuously updates `structured_output` as it progresses; polling `GET /v3/organizations/{org_id}/sessions/{devin_id}` retrieves validated JSON conforming to our Epistemic State schema.
- **Message & Artifact Harvest**:
  - `GET /v3/organizations/{org_id}/sessions/{devin_id}/messages` (cursor-paginated chronological log).
  - `GET /v3/organizations/{org_id}/sessions/{devin_id}/attachments` (downloadable trace bundles / logs).
  - Official Devin MCP (`https://mcp.devin.ai/mcp`) `devin_session_events` for event-level status & sub-session lineage (`parent_session_id`, `child_session_ids`).

### Channel B: Execution Plane (Custom MCP Proxy + Sandbox Wrapper)
- **Custom Trace MCP**: Configured via Devin's custom MCP integration (`Customize > MCPs` or `.devin/mcp_config.local.json`), logging every external lookup/test-report tool call with structured arguments and hashed responses.
- **Sandbox Trace Sidecar**: Guided by a Devin Skill, key build/test/repro commands run through a thin wrapper (or `BASH_ENV` hook) that appends normalized JSONL records (`timestamp`, `cwd`, `git_sha`, `cmd`, `exit_code`, `blob_hash`) into `.agent-trace/events.jsonl` and stores raw outputs under `.agent-trace/blobs/`.
- **Handoff Export**: Before concluding its phase, Devin publishes `.agent-trace/` as a session attachment or pushes it to a dedicated `refs/traces/<session_id>` ref.

### Alignment Algorithm
1. Parse Channel B `events.jsonl` into the Canonical Event DAG.
2. Parse Channel A `structured_output` into Epistemic State and bind each `evidence_hint` / file / command to the nearest matching Canonical Event ID by `(timestamp, git_sha, command/path)`.
3. Attach Channel A `messages` metadata and PR references.
4. Validate output against `schemas/session-ir.schema.json`.

---

## 4. Target Hydration: Claude Code (PoC #1)

Claude Code stores local sessions as append-only JSONL transcripts. The Claude Code Hydrator translates a reconciled Session IR bundle into:
1. A structured **Handoff Preamble** containing the reconciled workspace status, `verified_facts`, `ruled_out_hypotheses`, and `open_constraints`.
2. A compact sequence of **schema-valid synthetic `tool_use` / `tool_result` pairs** representing only the causal frontier (repro command + failing output summary + key file locations), keeping token overhead minimal.
3. A local `.agent-trace/blobs/` directory mount so Claude Code can read full cold logs if needed without front-loading megabytes of stdout.

---

## 5. Verification & Metrics

To prove measurable gains over cold starts and plain-text summaries, PoC #1 evaluates three conditions across multi-file bug-fixing tasks:
1. **Baseline A (Cold Start)**: Claude Code starts from the issue description alone.
2. **Baseline B (Prose Handoff)**: Devin produces a free-form markdown summary; Claude Code reads it.
3. **Treatment (Session IR Handoff)**: Devin exports a Session IR bundle; Claude Code hydrates it after workspace reconciliation.

Tracked metrics:
- **Redundant tool call rate**: % of file reads / searches / repro runs repeating work Devin already completed.
- **Time & tokens to verified patch**: Total tokens consumed by the receiving agent until tests pass.
- **Pass@1**: End-to-end test suite pass rate.
