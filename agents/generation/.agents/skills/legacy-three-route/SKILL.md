---
name: legacy-three-route
description: Run the cadence-disabled Legacy three-route checkpoint and fanout after protected route design finds no complete candidate. Do not use in hot-join or reviewed runs.
---

# Legacy Three-Route Fanout

Use this skill only under `AGENTS.legacy.md`, after protected route design has
produced exactly three materially different, scope-disjoint viable plans and no
complete candidate.

## Pre-fanout checkpoint

Publish one `memory_append_batch` containing the three plan ids, mechanisms,
scopes, discriminating tests, and concrete subgoals. Do not create a scheduled
review commitment because Legacy has no reviewer.

When the runner identifies a host-validated external Claude plan, the named
plan file is already the complete canonical transport. Do not inspect another
plan, memory file, log, result, statement, test, source file, Git object, or CLI
transcript to discover a format or example. Use the primary checkpoint MCP
exactly once with the named `problem_id` and `items=[]`. The trusted MCP reads,
SHA-checks, schema-checks, and atomically materializes the three host-bound
plans as active `subgoals` records in plan-file order. Never manually reproduce
the plan JSON in the tool call. The three returned record ids are the
checkpoint ids passed to the three children. The tool schema and this rule are
authoritative; never search the filesystem for checkpoint examples.

Call the primary checkpoint server once. Use the recovery server only when the
primary returns one of its two exact 60-second timeout envelopes. The recovery
call must reuse the same frozen `problem_id` and `items=[]`; every other primary
failure propagates, and a recovery failure is terminal. Accept only an exact
successful `CallToolResult` with exactly `content`, `isError`, and
`structuredContent`; `isError=false`; and one exact text block whose decoded
JSON equals `structuredContent`. The body has exactly these keys:
`schema_version`, `status`, `problem_id`, `batch_id`, `checkpoint_sha256`,
`timestamp_utc`, `committed_at_utc`, `committed_at_monotonic`, `commit_sha256`,
`count`, `records`, and `checkpoint_path`. Its schema is
`rethlas_memory_batch_local_commit_receipt_v1`, with:

- `status="ok"` and the exact problem id
- `batch_id` matching `batch_[0-9a-f]{64}`
- 64-lowercase-hex checkpoint and commit digests
- canonical UTC timestamps with checkpoint time not after commit time
- a positive finite monotonic commit time
- an absolute checkpoint path ending in `/.phase_checkpoints/{batch_id}.json`
- a three-record array matching plan-file order, each with channel `subgoals`,
  `active=true`, `supersedes=[]`, and a unique `mem_[0-9a-f]{64}` id

Reject missing or extra fields, strings in place of the result envelope,
non-boolean `isError`, host-publication fields, mismatched text and structured
content, generic timeout matching, or a third call. Do not claim checkpoint
success without the durable local receipt.

The two permitted primary timeout texts are exactly:

```text
tool call error: tool call failed for `reasoning_checkpoint_primary/memory_append_batch`

Caused by:
    timed out awaiting tools/call after 60s
```

and the same text ending in `after 60000ms`. The error envelope itself must
have exactly `content` and `isError=true`, with one exact text block. A
substring, regex, generic timeout, semantic error, or extra field never permits
recovery.

## Fanout

1. Spawn exactly three context-free solvers in one fanout, one per plan. Use
   `fork_turns="none"`. Children cannot spawn descendants and must inherit the
   root model and reasoning effort bound by the runner; do not request a model
   or effort override.
2. Give each child the authoritative problem path/id, its assigned plan and
   subgoals, compact summaries of the other two plans, the checkpoint record
   ids, its exact scope, and the runner-announced statement-bound retrieval
   mode. In `disabled` mode forbid every external call. In `matlas_arxiv` mode
   permit only the dedicated Matlas/arXiv theorem searches, after this
   checkpoint, for one named route-changing knowledge gap and at most two
   targeted queries. A returned exact arXiv id may then be inspected with
   `read_arxiv_primary`; this locator-bound primary-source read is not a new
   search query and the tool enforces the statement's submission-date cutoff.
   General web and browser access remain forbidden. Do not copy the root
   transcript. If the assigned `plan_summary` contains a
   `[reference_candidate:<id>]` marker and SHA-bound projected path, tell that
   child to read and audit that complete unverified reference candidate. The child must
   either complete that mechanism or report its first fatal mathematical gap;
   silently substituting another proof does not complete the route.
3. Require one report targeting at most 12,000 UTF-8 bytes, with a hard limit
   of 16,384 bytes, containing plan id, status (`candidate|partial|blocked`),
   concrete proof steps or counterexample, remaining obligations, and decisive
   stuck points.
4. The root does not prove a fourth route and does not write periodic status
   records while children work.

## Completion-driven waiting

Wait once for up to 600,000 ms with early wake. A real mailbox update resets
the next wait to 600,000 ms. A no-change timeout doubles the next wait, capped
at 3,600,000 ms. Do not call `list_agents` after an ordinary timeout, send
periodic reminders, or poll at short intervals. Use `list_agents` only to
reconcile an ambiguous collaboration result.

After four consecutive no-progress timeouts, request bounded partial reports
from already live children when possible, preserve the liveness failure, and
return locally. Do not invent a cost wait, advisor request, reviewer verdict,
or owner message.

A complete candidate from any child preempts wait-all. Stop the remaining
children when supported, assemble the whole blueprint, and enter
`$verify-proof` without new retrieval or fanout.

For every non-candidate terminal mailbox result, freeze the exact bounded child
text, canonical collaboration thread id, plan id, status, remaining
obligations, and decisive stuck points. Before the next wait, call
`append_route_terminal_report`; that trusted helper owns boundary-whitespace
normalization, UTF-8 sizing, SHA-256 binding, channel selection, the exact
`rethlas_route_terminal_report_v1` record, and its one-item checkpoint. Never
hand-build that wrapper or hash. Pass the canonical direct-child id exactly as
returned (for example `/root/route_1`); the helper accepts that native form and
persists its deterministic route-local normalization (`route_1`). Retain its
returned record id. If its
preflight reports an over-limit byte count, ask the same child once to compress
the unchanged mathematics and call the helper with that replacement. The
terminal arrival is a durability boundary, not a periodic status write. An
exact retry may only reuse the same helper arguments under the checkpoint
server's ordinary idempotency contract. Never defer a returned report merely
because another child is still active. After four no-progress timeouts,
persist every report already received before returning the liveness failure.

## Terminal reconciliation

If no candidate exists, require the three already durable exact report ids.
Do not append or rewrite their report bodies at cohort close. Invoke
`$identify-key-failures` and persist one
`rethlas_round_failure_synthesis_v1` covering all three report ids and plan ids
with one single-item `memory_append_batch` checkpoint. Never call
`memory_append` for the synthesis: only the batch receipt makes the completed
unverified cohort reconstructible by the host.

Legacy has no `continuous_round_finish`. A later paid root or later full fanout
is useful only when these durable records expose a genuinely new mechanism or
test. Return unverified when the synthesis leaves no scheduled mathematical
action.
