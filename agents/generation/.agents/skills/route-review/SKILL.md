---
name: route-review
description: Respond to an official host-scheduled minute-60 or minute-120 route-review notice by freezing a bounded immutable snapshot, waiting for one fresh independent critic, and obeying the host-derived green/yellow/red disposition. Never invoke from self-estimated time.
---

# Independent Route Review

This skill describes a host-scheduled phase boundary, not a root-model
self-review technique and not a fact checker. The owner-only zero-model driver
may execute it only after the durable scheduler has terminalized the exact due
root turn and derived the review id, cycle, ordinal, route, model, reasoning
effort, policy digest, and absolute deadline. The model must never estimate
elapsed time, create an unofficial review id, drive the official publication
sequence, or treat completion of a yellow cycle as the 150-minute hard stop.

<!-- rethlas-route-review-policy
{
  "policy_id": "rethlas_route_review_150m_v2",
  "official_cycles": [
    {"cycle": "minute60", "ordinal": 1, "due_seconds": 3600, "review_deadline_seconds": 4200},
    {"cycle": "minute120", "ordinal": 2, "due_seconds": 7200, "review_deadline_seconds": 7800}
  ],
  "review_boundary_mode": "cooperative_drain_then_deadline_interrupt",
  "review_drain_grace_seconds": 300,
  "review_execution_grace_seconds": 300,
  "hard_stop_seconds": 9000,
  "reviewer": {
    "independent": true,
    "fresh_session": true,
    "ephemeral_home": true,
    "workspace_access": "empty_read_only",
    "network_access": false,
    "web_search": false,
    "mcp_servers": [],
    "collaboration": false,
    "user_config": false,
    "tools": [],
    "same_model_and_effort_as_root": true,
    "automatic_retry": false
  },
  "verdicts": ["green", "yellow", "red"],
  "two_yellow_without_confirmed_progress": "red",
  "qualifying_progress": ["new_lemma", "counterexample_excluded", "uncertainty_reduction"],
  "review_is_verification": false,
  "targeted_verification_requires_load_bearing_claim_ticket": true,
  "targeted_verification_can_publish": false,
  "scheduler_is_authoritative_clock": true,
  "execution_unknown_may_auto_retry": false,
  "continuation_request_at_seconds": 8820
}
-->

## Input contract

The official notice is authoritative control data, not mathematics. It must
bind:

- `review_id`, `cycle`, and `review_ordinal`
- exact `run_id`, `problem_id`, `root_thread_id`, and active `root_turn_id`
- exact host/commitment-derived `route_id`; trusted code resolves the statement and blueprint bytes,
  digests, and parsed proof-item commitments
- the runner-bound model, reasoning effort, and policy digest (never supplied
  as free model arguments)
- the review-policy SHA-256 and host deadline

If any binding is missing or disagrees with the current run, do not improvise a
review. Report the operational mismatch and continue only as the scheduler
directs. An early self-spawned critic cannot satisfy minute 60 or minute 120.

## Cooperative drain before review

At T+60 and T+120 the trusted host does not normally interrupt the root or its
proof children. It sends one exact `rethlas_review_drain_v1` steer to the root.
Direct app-server input to multi-agent-v2 children is forbidden, so the root
must use native collaboration to ask every already-running child in the
host-frozen proof-lane set to summarize and return. Start no new proof route,
sub-agent, retrieval, or long computation. Each child immediately returns a
bounded report containing its assigned plan id, proved claims, failed paths,
explicitly partial work, and best next test. Partial is a valid terminal report;
do not conceal it or inflate it into a proof. The root drains those reports,
persists one post-boundary drain checkpoint for later rehydration, and returns
cleanly. Its post-due records cannot enter the current official snapshot.

The review deadline five minutes later is only a straggler safety fallback. The
host force-interrupts a still-live turn at that deadline, seals any emitted
assistant text as `interrupted_partial`, and keeps it outside proof evidence.
Fresh-epoch rehydration may receive a content-addressed partial-report bundle as
untrusted scratch. It does not grant route authority and every claim must be
checked before promotion.

## Snapshot contract

At the due notice, stop adding mathematical work long enough to freeze one
bounded `rethlas_route_review_snapshot_v3` object. The trusted MCP server—not
the model—constructs it from authoritative files and active durable record ids.
It includes:

- the exact cycle/ordinal/thread/turn/route, full bounded authoritative problem
  statement, full bounded current blueprint, their digests, and the exact
  host-derived `due_at_utc` boundary;
- unique parsed blueprint item labels, item ids, and canonical claim digests;
- frontier-changing active durable records with record id, kind, canonical
  body, channel, batch id, and durable timestamp;
- after the exact prior official review cutoff, only records whose durable
  qualifying kind is
  `new_lemma`, `counterexample_excluded`, or `uncertainty_reduction`.
- at minute 120, the digest-bound same-cycle minute-60 official report and
  effective decision; at the first review of a later cycle on the same route,
  the prior cycle's official minute-120 report and decision. Both include exact
  cycle/ordinal provenance, fatal doubt/test, milestone, and confirmed progress
  ids.

Every progress record must also appear with the same canonical body in the
frontier. Every frontier timestamp must be at or before `due_at_utc`; work
durably created after T60/T120 belongs to the next phase and cannot reset a
yellow streak. The statement stays fixed, but the current blueprint may
legitimately evolve between T60 and T120; each review binds its own exact bytes.
Do not include scratch, hidden reasoning, a transcript, tool logs,
encouragement, or an advisor answer. The snapshot's canonical bytes determine
its SHA-256; a digest change is a different request.

The owner-only driver calls `review_frontier_status` in the restricted helper. Trusted code
returns every eligible active non-control reasoning record at or before the
boundary in one deterministic order, plus the qualifying progress ids and a
manifest digest; more than 64 frontier or 32 progress records is operationally
blocked, never silently truncated. Before the first T60 boundary, construction
must have durably written exactly one active
`rethlas_active_route_commitment_v1` in `branch_states`; it binds the route id,
active status, bounded core bridge, and obligations. Construction may also
precommit at most one distinct fallback route with its own bridge, obligations,
and active durable evidence ids. Trusted code resolves the latest pre-due state
per route and fails closed for zero or multiple active routes, multiple
fallbacks, or any post-due designation. Review boundary tools accept no
caller-supplied route choice. The driver calls `route_review_prepare` exactly
once logically with only the host-derived review id, cycle, ordinal, exact
manifest digest, frontier-record ids, and progress-record ids. The server
recomputes the manifest and requires full ordered equality, not a subset. Never
pass a raw snapshot, record body, model, effort, or policy. Exact
pre-dispatch retries are idempotent; changing any bound field under the same
review id is a conflict. Then call `route_review_wait` with the returned request
and snapshot digests; there is no model-supplied timeout. The host alone waits
for the lesser of 300 seconds and the absolute review deadline. A transport
timeout is operational state, not yellow or red.

The host must run this phase in a fresh capability-restricted helper with an
empty read-only workspace, no shell, web, collaboration, or general reasoning
MCP tools. During `independent_review_only`, the MCP server also asks the
authenticated scheduler before every tool call and permits only route-review,
targeted-claim, and handoff control tools. If a restricted helper cannot obtain
the exact eligible durable record ids from trusted control, the review is
operationally blocked; it must not fall back to free construction.

## The critic's five questions

The fresh independent critic receives only the immutable snapshot and returns
exactly these answers:

1. What is the route's load-bearing core bridge?
2. Do its premises really match the target?
3. Did the newest work materially reduce uncertainty?
4. Is there a known obstruction or counterexample risk?
5. What is the next independently testable milestone?

Its strict report is:

```json
{
  "review_id": "review_<32 lowercase hex>",
  "snapshot_sha256": "<64 lowercase hex>",
  "route_id": "...",
  "answers": {
    "core_bridge": "...",
    "premise_target_fit": {"status": "match|mismatch|unclear", "reason": "..."},
    "uncertainty_change": {"status": "reduced|not_reduced|unclear", "evidence_ids": []},
    "obstruction_risk": {"status": "none|known_obstruction|counterexample_risk", "detail": "...", "evidence_ids": []},
    "next_milestone": {"description": "...", "test": "..."}
  },
  "verdict": "green|yellow|red",
  "fatal_doubt": null,
  "freeze_reason": null,
  "load_bearing_claim": null
}
```

The report has no extra keys. Green requires one milestone and forbids a fatal
doubt/freeze reason. Yellow requires exactly one fatal doubt and its test; that
same object is the next milestone. Red requires a freeze reason and forbids a
milestone/fatal doubt. A load-bearing claim, when present, names exactly one
blueprint item label, claim SHA-256, and reason. Red must set the claim to null:
the route is already frozen, so it cannot spend another verifier call or delay
the red transition. Targeted verification is admitted only while the effective
review is green or yellow (apart from idempotently reading an already completed
wrong-result receipt that made the route red).

Malformed output, timeout before known completion, or launcher failure is
`operational_blocked`, never a mathematical verdict. If dispatch occurred but
completion cannot be proved, state is terminal `execution_unknown`: do not
automatically retry, because a second paid critic could duplicate the first.

## Apply only the host-derived disposition

The critic supplies a raw verdict; the durable scheduler supplies the effective
verdict and allowed action:

- `green`: continue only toward the returned testable milestone.
- `yellow`: use one bounded work cycle on the single named fatal doubt.
- `red`: freeze that route, preserve its valid conclusions, and switch only by
  the scheduler's route policy.

Two consecutive official yellow reviews on the same route become effective
red even across a T150 cycle boundary unless the second critic confirms, by
bound record id, a new lemma,
counterexample exclusion, or material uncertainty reduction. A route switch or
confirmed progress resets the streak. The root cannot self-declare progress,
edit the critic report, or override effective red. Red freezes one route; it
does not by itself authorize `waiting_owner_advisor_decision` or any browser
advisor request.

After consuming a terminal result, the owner-only driver calls
`route_review_close` with the exact
review/request/snapshot digests. Trusted code first durably publishes a pending
review record, obtains a host acknowledgement that does not advance cadence,
publishes the official record, and only then performs the final host CAS.
Reply-loss retries reuse the same receipts. Green/yellow cannot switch routes;
effective red always publishes a content-addressed projection that freezes the
old route before the final host CAS. If the snapshot contains the one exact
pre-due fallback commitment, that same projection activates it; otherwise no
route remains active and no paid continuation is admitted. The close path
cannot accept a caller-invented route, post-due candidate, or replacement
evidence id. Closing cannot turn an operational failure into a verdict or
authorize a retry.

Official route-review and targeted-verification records remain searchable as
control/audit history, but they are never mathematical evidence. Their ids may
not enter a review frontier, qualifying progress, fallback-route evidence, or
handoff `new_record_ids`; any mathematical conclusion must live separately in
an allowed reasoning or verification channel with its own durable provenance.

## Review is not verification

Do not send ordinary review answers to the verifier. Only a strict non-null
`load_bearing_claim` may be converted by trusted code into one content-addressed
`rethlas_targeted_claim_ticket_v2`. Its label, proof-item id, and claim digest
must exactly match the parsed item committed by the reviewed blueprint. Call
`verify_review_claim` only for that official ticket. Trusted code durably marks
the single attempt before dispatch; an ambiguous response is terminal and is
never paid for twice. The verifier request and receipt bind the host's canonical
absolute T150 deadline; the verifier service rejects an expired request before
model dispatch and caps/kills its child at the earlier of its own limit and
T150. Its non-publishing receipt is also published with pending and official
acknowledgements before cadence changes. A correct result resumes the original
effective action; a wrong result freezes the route; an operational or ambiguous
result remains blocked. The ticket permits targeted,
non-publishing verification of the exact claim only. It grants neither a whole
blueprint verdict nor publication authority. Whole-proof publication still
requires the ordinary fresh verifier and `verify_blueprint_service` contract.

## Context rollover

When a fresh thread, owner yield, or next cycle is needed, call
`context_handoff_prepare` with the exact purpose `context_guard`, `owner_yield`,
or `cycle_close` plus only the bounded route summary, active durable record ids,
obligations, and next action/test. The authenticated adapter derives the current epoch,
absolute cycle `cycle_started_at_utc` T0 and T60/T120/T147/T150 cadence,
statement/blueprint bindings, official
review/yellow/frozen state, and real pending verifier/advisor ids. The resulting
purpose-bound `rethlas_context_handoff_v3` is at most 32 KiB and must not contain a transcript,
hidden reasoning, message history, or raw tool log.

Before `generation_yield` writes a waiting control record, authenticated host
state must locate exactly one validated `owner_yield` handoff for the current
cycle/thread/turn and future epoch and durably admit the exact state, reason
digest, and evidence ids. Missing or mismatched handoff means zero wait write.
The runner's final cadence-close replay binds the same handoff and canonical
generation-control receipt. A `cycle_close` handoff cannot be reused for owner
yield, and an `owner_yield` handoff cannot continue a cycle.

For a next cycle, call `route_cycle_close` only in the host-authorized T147–T150
window with the exact handoff id/digest, disposition `continue_next_cycle`, and
one bounded testable milestone. T150 remains an external hard stop; without a
valid close request the cycle becomes `hard_stopped`.

The host creates an empty fresh thread, retrieves and consumes the exact
handoff itself, and injects its canonical body into the first turn before that
turn starts. `context_handoff_get` is optional audit access, not a model-owned
first-tool gate; `context_handoff_status` is metadata-only. Rollover never
resets absolute deadlines, yellow streak, route freeze, pending obligations,
or the 150-minute hard stop. Same-thread compaction is not a substitute for this
durable handoff.

Rollover also requires a trusted no-live-children attestation. After the epoch
CAS, the host revokes the old epoch-scoped MCP capability before issuing a new
one, so a late child from the old thread cannot append memory or mutate cadence.
