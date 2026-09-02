---
name: recursive-proving
description: Launch exactly three context-free route solvers under the reviewed or continuous AGENTS.md profile. Cadence-disabled Legacy runs must use legacy-three-route instead.
---

# Recursive Proving

Use this skill after the protected root phase has checkpointed one exact set of
three viable, independent routes and no complete candidate exists.

Do not use this skill under `AGENTS.legacy.md`; that profile uses
`$legacy-three-route` so it does not load review, advisor, cost, and continuous
host instructions that Legacy cannot execute.

<!-- rethlas-recursive-wait-policy
{
  "policy_id": "rethlas_recursive_wait_v1",
  "initial_timeout_ms": 600000,
  "backoff_multiplier": 2,
  "max_timeout_ms": 3600000,
  "max_consecutive_no_progress_timeouts": 4,
  "max_orchestration_resumptions": 16,
  "max_observed_orchestration_input_tokens": 3000000,
  "cost_gate_policy_manifest_json_env": "RETHLAS_RESOLVED_COST_POLICY_JSON",
  "cost_gate_policy_manifest_sha256_env": "RETHLAS_RESOLVED_COST_POLICY_SHA256",
  "cost_gate_policy_manifest_schema": "rethlas_resolved_cost_policy_v1",
  "cost_gate_policy_values": ["owner_gated", "disabled_by_owner"],
  "default_cost_gate_policy": "owner_gated",
  "disabled_by_owner_keeps_telemetry": true,
  "disabled_by_owner_may_yield_waiting_cost_gate": false,
  "max_status_queries_without_mailbox_change": 0,
  "reset_timeout_on_mailbox_progress": true,
  "enforcement_scope": "instruction_runner_integrity_and_host_generation_yield_preflight"
}
-->

<!-- rethlas-advisor-checkpoint-policy
{
  "policy_id": "rethlas_advisor_checkpoint_v1",
  "allowed_triggers": [
    "isolated_load_bearing_gap_two_independent_failures",
    "three_route_round_shared_failure_synthesis",
    "all_current_branches_terminal_blocked_or_dead_end",
    "all_remaining_routes_evidence_backed_near_exhaustion"
  ],
  "requires_failure_synthesis": true,
  "isolated_gap_min_independent_failed_paths": 2,
  "isolated_gap_requires_exact_target_claim": true,
  "isolated_gap_requires_boundary_checks": true,
  "isolated_gap_requires_no_scheduled_local_repair": true,
  "isolated_gap_requires_no_live_subagents": true,
  "gap_response_is_untrusted_targeted_delta": true,
  "gap_response_must_not_mutate_route_council_packet": true,
  "gap_response_reaudits_only_repair_cone": true,
  "near_exhaustion_requires_no_live_subagents": true,
  "near_exhaustion_requires_no_scheduled_next_action": true,
  "near_exhaustion_requires_obstruction_record_per_remaining_route": true,
  "cost_gate_alone_may_trigger": false,
  "automatic_broker_prepare": false,
  "automatic_browser_dispatch": false,
  "automatic_followup": false,
  "prompt_must_synthesize_current_state": true,
  "source_context_sha256_required": true,
  "continuation_requires_new_request_and_exact_owner_authorization": true,
  "max_verified_fact_or_proof_ids": 12,
  "max_failed_path_record_ids": 12,
  "max_bottleneck_utf8_bytes": 2000,
  "max_recommended_question_utf8_bytes": 4000,
  "max_checkpoint_utf8_bytes": 16384,
  "deduplicate_until_new_math_or_advisor_receipt": true,
  "enforcement_scope": "instruction_and_runner_integrity_not_runtime_mediated"
}
-->

<!-- rethlas-three-route-fanout-policy
{
  "policy_id": "rethlas_three_route_fanout_v1",
  "root_role": "orchestrator_and_canonical_memory_writer",
  "exact_plan_count": 3,
  "initial_subagent_roles": ["route_solver_1", "route_solver_2", "route_solver_3"],
  "initial_subagent_count": 3,
  "max_live_subagents": 3,
  "fork_turns": "none",
  "spawn_in_one_fanout": true,
  "subagents_may_spawn": false,
  "subagents_write_shared_memory": false,
  "max_report_utf8_bytes": 16384,
  "candidate_preempts_wait_all": true,
  "root_may_run_fourth_proof_route": false,
  "next_round_requires_terminal_synthesis": true,
  "next_round_requires_host_finish_receipt": true,
  "root_is_canonical_memory_writer": true,
  "enforcement_scope": "instruction_contract_tests_and_continuous_host_admission"
}
-->

The machine-readable policy above is part of this skill's contract. The runner
integrity-binds this file. Built-in collaboration calls still bypass repository
code at the instant of spawn, so route content and the initial exact fanout
remain instruction-level obligations. In continuous mode, however, the host
observes every resulting proof thread and refuses a later cohort unless it
consumes one trusted round-finish receipt or a bounded reviewer restart. Legacy
mode retains instruction-only admission. The policy budgets root-agent
orchestration resumptions, not mathematical work performed inside a sub-agent.
The collaboration runtime does not expose a trustworthy
orchestration-only token counter, so the input-token gate applies when usage is
observable; the resumption counter is the mandatory proxy otherwise. The
owner wrapper resolves the cost policy once into the canonical
`rethlas_resolved_cost_policy_v1` manifest and SHA-256 named above. Guardian
explicitly forwards that pair, and the adapter policy contract plus trusted MCP
bind the same digest. The raw `RETHLAS_COST_GATE_POLICY` selection is removed
before child launch, so a wrapper or Guardian restart cannot silently fall back
to a different default. Under `disabled_by_owner`, keep recording the counters
and threshold crossings, but do not stop, yield, prune a proof lane, or ask for
owner input because of token, resumption, elapsed-time, or model-cost telemetry.

## Input Contract

Read:

- the exact three-plan pre-fanout checkpoint and record ids
- the mechanism, scope, discriminating test, and subgoals for every plan
- any known stuck points that all three children must avoid
- relevant `failed_paths`, `branch_states`, and search results

## Procedure

1. Confirm that the protected root route-design phase produced exactly three
   materially different, scope-disjoint plans, one discriminating test per
   plan, and one successful pre-fanout `memory_append_batch` receipt. If a
   complete candidate already exists, do not invoke this skill; enter the
   candidate fast lane.
2. Spawn exactly one solver for each of the three plan ids in one fanout. Use
   context-free forks (`fork_turns="none"`), bind each returned canonical agent
   id to exactly one plan id, and do not insert a status query between spawns.
   The root is an orchestrator and may not pursue a fourth proof route while
   these solvers are live.
3. If the fanout is only partially admitted, do not silently continue with one
   or two routes and do not replace a failed spawn with a fourth plan. Reconcile
   only an ambiguous tool result. Otherwise interrupt the confirmed children
   when supported, persist the operationally incomplete fanout, and return to
   the root without claiming a three-route round occurred.
4. Give every solver a bounded prompt containing the authoritative problem
   path/id, its assigned plan and subgoals, the other two plan summaries, the
   pre-fanout record ids, and its exact scope. Do not copy the root transcript.
   A solver may refine its own plan but may not switch to another assigned
   route, spawn an agent, write shared memory, verify or publish a blueprint, or
   start a broad literature survey without one named route-changing gap.
5. Require one terminal report targeting at most 12,000 UTF-8 bytes and never
   exceeding 16,384 UTF-8 bytes, with the plan id,
   status (`candidate|partial|blocked`), concrete proof steps or counterexample,
   remaining obligations, and decisive stuck points. For a non-candidate round,
   first call `continuous_round_status` in continuous mode. Hash each exact
   native terminal response and use its unique manifest
   `terminal_report_sha256` match to recover the host `thread_id`. Never pair by
   order or semantic task name. Preserve the exact native terminal response as
   `report_text`; do not rewrite or summarize it, and require its digest to
   equal the manifest `terminal_report_sha256`. The root then persists each
   report as one exact `rethlas_route_terminal_report_v1` record, including that
   host thread id and report-text digest. The root is the only memory writer and
   publication caller. Legacy mode has no host manifest and keeps its existing
   canonical collaboration id binding. A pending continuous manifest permits
   at most one bounded same-turn retry for host-observation propagation and
   never authorizes a new paid turn or child rerun.
   The outer record has exactly `schema_version`, `thread_id`, `plan_id`,
   `status`, `report_text`, `report_sha256`, `remaining_obligations`, and
   `decisive_stuck_points`. Do not copy `manifest_sha256`, `cohort_ordinal`,
   `route_id`, or `record_type` into it.
6. Wait with the completion-driven protocol below. A complete candidate from
   any confirmed solver preempts wait-all. Interrupt the other two when
   supported, assemble the candidate at the root, and enter verification.
7. If all three solvers terminate without a candidate, persist the three exact
   report records plus one branch decision in a single `memory_append_batch`.
   Then invoke `$identify-key-failures`, persist its exact
   `rethlas_round_failure_synthesis_v1`, and in continuous mode call
   `continuous_round_finish` with the three report ids and synthesis id. The
   returned host receipt is required before a new fanout. Legacy mode has no
   continuous host and therefore records the same evidence but does not call
   this tool.
8. A later round is legal only after the prior three reports and shared failure
   synthesis are durable, the host has accepted and not previously consumed the
   round-finish receipt, and `$propose-subgoal-decomposition-plans` has produced
   a new exact set of three mechanisms. Never refill one finished slot piecemeal
   or exceed three live proof children.

## Completion-driven wait protocol

Maintain the confirmed sub-agent IDs and completed IDs locally. A final report
from a confirmed ID is authoritative progress; do not call `list_agents` merely
to rediscover it.

1. The first `wait_agent` timeout is **600,000 ms**. `wait_agent` wakes early
   when a mailbox message or completion arrives, so a long timeout does not
   delay useful progress.
2. On a timeout with no mailbox change, do not call `list_agents`, do not send a
   reminder, and do not immediately poll again at the same interval. Double
   the next timeout, capped at **3,600,000 ms**.
3. On a real mailbox update, process it, update the completed-ID set, and reset
   the next timeout to 600,000 ms. Progress messages that are not final do not
   mark an agent complete.
4. Use `list_agents` only to reconcile an ambiguous tool failure or a mailbox
   update that lacks a canonical sender/status. With no new state, the permitted
   status-query count is zero. One final reconciliation is allowed only after
   an explicit ambiguity, not after an ordinary timeout.
5. If follow-up guidance is genuinely required for several agents, emit all
   independent messages in one multi-tool response when supported. Never send
   periodic "still working?" prompts. One follow-up fanout batch is the default
   maximum for a recursive round.
6. Count every root-model resumption after a collaboration tool result as one
   orchestration resumption, and always persist whether 16 resumptions or
   3,000,000 observed orchestration input tokens has been crossed. Under
   `owner_gated`, either threshold stops **all** new collaboration calls,
   including `wait_agent`, `list_agents`, `send_message`, and `spawn_agent`.
   Under `disabled_by_owner`, those thresholds are telemetry only: continue the
   completion-driven protocol without a cost yield. Independently of cost
   policy, stop the recursive round after four consecutive no-progress
   timeouts, persist that liveness failure, and continue or end locally without
   another poll. This is a route/liveness decision: return the bounded evidence
   to the root. It is never
   `waiting_cost_gate`, never a self-issued review verdict, never a new `T0`,
   and never permission to work through a review or hard-stop boundary. Never
   invent missing reports or convert a cost/liveness gate into a mathematical
   verdict.
7. An enabled cost gate is deterministic flow control, not a request for human
   input. Never invent or inject a human hot-join message, and never decide on
   the owner's behalf that human intervention should occur.
   It also never authorizes an advisor request: do not open Chrome, invoke a
   browser advisor, prepare/authorize/retry an advisor job, or ask another agent
   to do so. Only the repository owner may initiate that separate workflow.

## Event-driven strategic advisor checkpoint

This owner-wait path is available only when the trusted runner prompt announces
a hot-join owner-yield surface. In a cadence-disabled legacy run, persist the
three-route failure synthesis and return unverified; do not write
`waiting_owner_advisor_decision` or call `generation_yield`.

The root (not a sub-agent) may recommend one owner-decided Pro consultation
after any of four triggers: (a) the same isolated load-bearing claim has
defeated two independent, durably recorded mechanisms, has an exact target
claim and boundary checklist, and has no scheduled local repair; (b) one exact
three-route round has produced three terminal solver reports and a shared,
evidence-backed failure synthesis with no complete candidate; (c) every
confirmed branch has reached a terminal blocked/dead-end result; or (d) all
remaining routes are demonstrably near exhaustion. The isolated-gap trigger is
deliberately available before global route exhaustion: do not spend a complete
new three-route round merely to qualify a precise gap for owner relay. Its two
failed-path records must name genuinely different mechanisms, not two
restatements or retries of one attempt. Every trigger requires no live
sub-agent. The near-exhaustion trigger additionally requires no already
scheduled next action, every remaining route has a concrete failure/obstruction
record, and `$identify-key-failures` has synthesized the shared obstruction. A
subjective sentence that the search is "stuck", a cost gate, timeout, long
runtime, or token count never satisfies any trigger.
For the three-route trigger, the route-set checkpoint, all three bound solver
reports, and the root's failure synthesis must each have durable record ids.
This is a strategic mathematical checkpoint, not a retry policy or a mandatory
ceremony.

For the isolated-gap trigger, call `prepare_pro_gap_query` only in the canonical
Claude-root runtime where that tool is explicitly exposed. Supply a stable
`gap_id`, the exact target claim, settled-fact summaries paired with active
fact/proof record ids, two or more independent failed-attempt summaries paired
with active `failed_paths` record ids, the boundary checklist, and the exact
question. Do not submit or invent a source-context digest: the host computes it
from the statement, cited records, and ledger head and builds the returned
`copy_paste_prompt`. A repeated `gap_id` is write-once; changed evidence
requires a new id. In the reviewed/GPT-Sol runtime, these tools are intentionally
absent: persist and return the exact gap evidence to the canonical root instead
of pretending that the query was prepared.

Persist exactly one bounded `events` record using the
`rethlas_advisor_checkpoint_v1` limits above. Include only evidence-backed
verified fact/proof ids (use an empty list if none has actually been verified),
failed-path record ids, the central bottleneck, and one exact recommended
question. An isolated-gap question asks Pro either to close the exact claim
rigorously or to give a concrete counterexample, and to address the recorded
failure mechanisms and boundary checks. A global checkpoint asks Pro to choose
or sharpen one decisive next mathematical direction. The question is generated
from this checkpoint's current problem state, never
copied from a fixed generic prompt: restate the authoritative problem
succinctly, summarize the included verified facts and failed routes with their
ids, distinguish proof from heuristic evidence, state the current bottleneck,
and ask for one bounded decisive next step. For the legacy Chrome-broker
checkpoint (not the canonical manual gap packet), hash the canonical problem
statement plus the exact included fact/proof records, failed-path records, and
bottleneck as `source_context_sha256`; the manual gap packet instead uses the
host-computed value returned by `prepare_pro_gap_query`. Set
`owner_action_required=true`, `browser_dispatch_authorized=false`, and
`advisor_request_id=null`. Derive a content-addressed checkpoint id from the
canonical payload and do not repeat the same checkpoint until new mathematical
evidence or a new advisor receipt exists. Persist the event and a
`branch_states` transition to `waiting_owner_advisor_decision` as two items in
one `memory_append_batch`, bind both returned record ids by input order, and call
`generation_yield(problem_id, state="waiting_owner_advisor_decision",
reason=..., evidence_record_ids=[advisor_event_id, branch_state_id])` as the
final tool action. Then return one concise owner-facing message containing the
exact `gap_id`, `query_sha256`, and verbatim `copy_paste_prompt` (or the global
checkpoint question), and end locally without polling the owner. A branch-state
write without this bound yield receipt does not stop the runner.

The four gap-specific fields below are required for the isolated-gap trigger;
retain the historical global-checkpoint shape for the other three triggers.

```json
{
  "event_type": "advisor_checkpoint",
  "policy_id": "rethlas_advisor_checkpoint_v1",
  "checkpoint_id": "acp_<24 lowercase sha256 hex>",
  "trigger": "isolated_load_bearing_gap_two_independent_failures",
  "gap_id": "gap_<stable lowercase id>",
  "target_claim": "<exact isolated claim>",
  "boundary_checks": ["<singular regime or edge case to audit>"],
  "query_sha256": "<64 lowercase sha256 hex>",
  "verified_fact_or_proof_ids": [],
  "failed_path_record_ids": [],
  "central_bottleneck": "...",
  "source_context_sha256": "<64 lowercase sha256 hex>",
  "recommended_exact_question": "...",
  "owner_action_required": true,
  "browser_dispatch_authorized": false,
  "advisor_request_id": null,
  "status": "waiting_owner_advisor_decision"
}
```

This recommendation never calls `advisor_bridge.py`, never prepares or
authorizes a job, never opens Chrome, and never grants a Send click. The owner
may ignore the question or request an edit. An edited question requires a new
write-once, CAS-bound gap query and digest; never bind its answer to the old
`query_sha256`. If the owner pastes a response to the exact isolated-gap
question, the canonical Claude root calls `ingest_pro_gap_response` with the
exact `gap_id` and `query_sha256`. This stores an untrusted
`complete_unverified_gap_delta`; it does not modify the accepted route-council
candidate packet. Load it through `get_pro_gap_response` with both expected
query and response digests, audit every step
against existing facts and failed paths, and persist which suggestions were
accepted or rejected and why. When a full proof draft already exists, patch
only the named claim and its dependency descendants, preserve unrelated
verified items, and reverify that repair cone. Do not restart route council
solely because this targeted response arrived. When no full draft exists, use
accepted parts only as evidence for the next branch plan. A normal
`advisor_available` receipt follows the same untrusted review rule. Neither
form is verifying or publishing.

If later evidence justifies another consultation, create a new checkpoint from
the then-current verified facts, failed paths, accepted/rejected parts of the
prior report, work performed since it arrived, and the new bottleneck. It must
contain a newly synthesized exact question and source-context digest. Never
send a follow-up automatically. The owner must prepare a new request id and
authorize that exact new question. The owner may explicitly continue in the
same ChatGPT conversation through the broker's digest-bound lineage workflow;
transcript continuity supplies context only and grants no authority. After
writing the checkpoint, keep `waiting_owner_advisor_decision`, make the bound
`generation_yield` call, and stop: do not poll, dispatch, start a paid turn, or
interrupt an active one.

Repeated 60-second polling is forbidden for recursive orchestration. A runtime
that cannot honor the long wait or early mailbox wake must fail the round's
orchestration contract rather than silently reverting to a busy poll.

## Output Contract

Append an `events` record for the recursive round:

```json
{
  "event_type": "recursive_proving_round",
  "fanout_policy_id": "rethlas_three_route_fanout_v1",
  "plan_ids": ["plan_1", "plan_2", "plan_3"],
  "subagents": [
    {"id": "...", "role": "route_solver", "plan_id": "plan_1"},
    {"id": "...", "role": "route_solver", "plan_id": "plan_2"},
    {"id": "...", "role": "route_solver", "plan_id": "plan_3"}
  ],
  "shared_stuck_points": {
    "plan_id": ["..."]
  },
  "status": "running|completed|liveness_stopped|waiting_cost_gate",
  "successful_plan_ids": ["..."],
  "failed_plan_ids": ["..."],
  "candidate_preempted_wait_all": false,
  "fanout_complete": true,
  "orchestration_cost": {
    "policy_id": "rethlas_recursive_wait_v1",
    "cost_gate_policy": "owner_gated|disabled_by_owner",
    "cost_gate_policy_manifest_sha256": "<64 lowercase SHA-256 hex>",
    "orchestration_resumptions": 0,
    "observed_orchestration_input_tokens": null,
    "observed_thresholds": [],
    "wait_timeouts_ms": [],
    "no_progress_timeouts": 0,
    "status_queries": 0,
    "spawn_fanout_batches": 1,
    "followup_fanout_batches": 0,
    "cost_gate_reason": null
  }
}
```

Persist the recursive-round event and its `branch_states` status/per-plan
outcomes as two items in one `memory_append_batch`.

Each non-candidate terminal report persisted by the root has this exact shape:

```json
{
  "schema_version": "rethlas_route_terminal_report_v1",
  "thread_id": "<canonical child thread id>",
  "plan_id": "<precheckpointed plan id>",
  "status": "partial|blocked",
  "report_text": "<complete bounded child report>",
  "report_sha256": "<64 lowercase SHA-256 hex>",
  "remaining_obligations": ["..."],
  "decisive_stuck_points": ["..."]
}
```
After four consecutive no-progress timeouts, use `status="liveness_stopped"`,
persist the bounded reports and missing agent ids, issue no generation yield,
and return control to the root or scheduled route review under the unchanged
cycle clock.
Only under `owner_gated` in a trusted hot-join run, if a cost threshold fires, use
`status="waiting_cost_gate"`, keep unfinished plan IDs out of both outcome
lists, and persist the same `orchestration_cost` object. Bind the batch
receipt's event and branch-state record ids by input order, then call
`generation_yield(problem_id, state="waiting_cost_gate", reason=...,
evidence_record_ids=[recursive_event_id, branch_state_id])` as the final tool
action. Do not issue another collaboration or reasoning call afterward. In a
cadence-disabled legacy run, record the threshold and return unverified without
an owner-wait state or yield call. Under `disabled_by_owner`, record the
threshold in `observed_thresholds` but never write `waiting_cost_gate` and never
call that yield; the trusted MCP/host preflight rejects such a call before any
control record is written.

## Tools

- `memory_search`
- `memory_append_batch`
- `generation_yield`
- `continuous_round_status` in continuous hot-join mode only
- `continuous_round_finish` in continuous hot-join mode only
- `search_matlas_theorems` (official Matlas published-journal/book search) and
  the distinct legacy `search_arxiv_theorems` Danus/LeanSearch arXiv provider;
  neither is an implicit fallback, and results are leads rather than proof
  evidence or full articles/PDFs. Record provider errors as operational and
  use at most one authorized web/arXiv fallback for the same named gap within
  the two-query limit.
  For official results retain `candidate_id`, map `paper_id` to a nonempty DOI
  (or title/authors/year with a web-verification obligation), and map
  `theorem_id` to `entity_name`. Preserve `candidate_id` as the provider
  candidate ID; do not treat it as the bibliographic theorem number. Legacy
  results keep `arxiv_id`/`theorem_id`, and unread primary text stays a lead.
- Codex sub-agent tools: `spawn_agent`, `send_message`, `wait_agent`,
  `list_agents`, and `interrupt_agent` when available (exact availability
  varies by host)

## Failure Logging

If all three routes terminate without a candidate, append one compact summary
to `failed_paths` and invoke `$identify-key-failures`. The root may then propose
one new exact three-route generation or consider the evidence-triggered advisor
checkpoint. For a near-exhaustion checkpoint, first prove the additional
no-live, no-scheduled-action, and per-route obstruction-record conditions.
