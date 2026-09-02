---
name: identify-key-failures
description: Compress three terminal route-solver reports into one reusable failure synthesis. Use before a new three-route generation or an evidence-triggered Pro recommendation.
---

# Identify Key Failures

Use this skill to turn many failed attempts into reusable guidance for the next planning round.

## Input Contract

Read:

- the failed decomposition plans
- direct-proving stuck points
- recursive sub-agent reports
- existing `failed_paths`
- relevant `counterexamples` and `toy_examples`

## Procedure

1. Gather the protected route-set record and exactly three bound route-solver
   reports from the completed fanout. Reject an incomplete or duplicate plan
   association rather than inventing the missing direction.
2. List the key stuck points for each plan.
3. Preserve the source report's epistemic status exactly. A claim labeled
   conjectural, diagnostic, heuristic, computationally supported, timed out,
   lower-bound-only, sufficient-only, partial, or unproved must retain that
   qualifier in every root summary and synthesis field. Never replace one of
   those labels with `proved`, `established`, `exact`, or an unqualified theorem
   sentence. Promote a proved step only when the terminal report explicitly
   supplies a proof and the root has independently checked that proof.
4. Identify common points across those failures:
   - recurring obstructions or counterexamples
   - decomposition patterns that keep breaking
   - search gaps or missing background facts
5. Summarize what the failures suggest for the next generation of decomposition plans.
6. Persist exactly one synthesized `failed_paths` item with exactly one
   `memory_append_batch` call. Its `items` array contains that one item with
   `active=true` and `supersedes=[]`. Never use `memory_append` for this
   record: a direct append has no content-addressed phase checkpoint and the
   host must reject the cohort as incomplete.
7. Decide among three next states: one genuinely new exact three-route
   generation, an evidence-triggered owner Pro checkpoint, or a truthful
   non-success yield. Do not refill one route slot in isolation.

## Output Contract

Return for `failed_paths`:

```json
{
  "schema_version": "rethlas_round_failure_synthesis_v1",
  "record_type": "key_failures_summary",
  "route_report_record_ids": ["mem_...", "mem_...", "mem_..."],
  "failed_plan_ids": ["..."],
  "plan_failures": [
    {
      "plan_id": "...",
      "stuck_points": ["..."]
    }
  ],
  "common_failures": ["..."],
  "implications_for_next_plans": ["..."],
  "next_state": "new_cohort|advisor_checkpoint|stop_unsolved"
}
```

The three route-report ids, three failed plan ids, and three `plan_failures`
entries must cover the same exact fanout without duplicates. Include a
next-state event in the same batch only when the same concrete next action has
been selected. In continuous mode, the root passes this synthesis record id and
the three report record ids to `continuous_round_finish`; this skill does not
call the host itself.

## MCP Tools

- `memory_search`
- `memory_append_batch`

The terminal synthesis durability boundary is always
`memory_append_batch`; `memory_append` is not a compatible substitute.

## Failure Logging

If the reports are too weak to identify meaningful common failures, return an
`events` payload with `event_type="key_failures_inconclusive"` and state what
information is still missing. Do not expand or request Pro until the evidence
gap is closed.
