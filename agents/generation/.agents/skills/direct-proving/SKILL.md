---
name: direct-proving
description: Carry one assigned decomposition route through coherently. Use inside a three-route solver child, or at the root only for a specific post-fanout repair; never serialize all three routes before fanout.
---

# Direct Proving

Use this skill to screen decomposition plans by first trying to carry the whole plan through, and if it does not fully go through, then identify the key stuck points.


## Input Contract

Read:

- one decomposition plan from `subgoals`
- relevant `immediate_conclusions`, `toy_examples`, `counterexamples`, and `failed_paths`
- relevant search results and references
- any previously identified external statements whose proofs may be adaptable

## Procedure

1. Take exactly one assigned plan in one coherent reasoning phase. Do not
   switch to either sibling route, interleave another plan, or start
   collaboration unless a named external knowledge gap is reached.
2. For each subgoal, actively use the searched results, toy examples, and counterexamples that are most relevant to that subgoal.
3. When a similar theorem has been found, try to adapt its proof idea, construction, or reduction to the current subgoal instead of treating it as a black-box citation.
4. If that theorem is only a partial result with extra hypotheses, first analyze why the method needs those hypotheses and where it fails for the current subgoal. Do not skip this by merely trying to prove the current object satisfies the extra hypotheses and applying the partial result directly.
5. First attempt to prove all subgoals in that plan directly.
6. Try to carry the whole plan through before switching into failure diagnosis mode.
7. For each subgoal, record whether it is:
   - already solved directly
   - partially advanced
   - blocked
8. If a proof adaptation attempt fails, identify why the migration fails. Be concrete: for example, note which hypothesis is missing, which construction does not transfer, which step breaks, which counterexample blocks the migration, or which part of the searched proof depends on structure absent in the current setting.
9. If a subgoal is blocked, test the claim and its negation locally before
   changing skills. Invoke `$construct-counterexamples` only when this produces
   a concrete falsifiability question, not merely because a step is hard.
10. If all subgoals are solved directly, mark the plan as solved. A route child
   returns the complete candidate to the root without writing memory,
   verifying, or publishing. The root then enters the candidate fast lane,
   stops the other routes, assembles the draft, and invokes the verifier.
11. If the plan does not fully go through, then identify the key stuck points as concretely as possible.
12. Focus on locating the decisive failure modes of the plan after this first full attempt, not on polishing a full proof.

## Local computation process discipline

For `exec_command`, normally omit `workdir` and inherit the exact session cwd.
If an explicit cwd is unavoidable, copy the exact cwd from session metadata;
never reconstruct, abbreviate, or guess an absolute worktree path. A first
`CreateProcess` failure with `No such file or directory` is an operational cwd
failure, not a failed computation. Do not retry the same program or submit a
sequence of alternative scripts under that unresolved cwd. Continue with pure
reasoning when possible and include the operational failure in the bounded
terminal report.

Do not invoke `/bin/ps` or another sandbox-forbidden process inspector. Track
only the session id and completion state returned by `exec_command` and
`write_stdin`. Send at most one targeted interrupt to that exact session and
accept its returned terminal state instead of issuing repeated blind signals.

## Output Contract

Return one consolidated plan report. A route child returns it only to the root;
the root includes it in the next `memory_append_batch`. Keep per-subgoal results
inside that report instead of issuing one MCP call per subgoal:

```json
{
  "plan_id": "...",
  "attempt_type": "direct",
  "attempt_summary": "...",
  "status": "solved|partial|stuck",
  "subgoal_results": [
    {"subgoal": "...", "status": "solved|partial|stuck", "summary": "..."}
  ],
  "used_examples": ["..."],
  "used_counterexamples": ["..."],
  "key_stuck_points": ["..."],
  "used_results": ["..."],
  "adapted_from": ["relevant statements or proofs whose ideas were migrated"],
  "migration_failures": ["why a proof adaptation or migration failed"],
  "branch_id": "optional"
}
```

Only the root includes the corresponding decomposition-plan state (`screened`
or `solved`) in a phase checkpoint. A child never writes shared memory. Do not
write a transient `screening` state solely to mirror execution order.

## MCP Tools

- `memory_search` (root only)
- `memory_append_batch` (root only)
- `search_matlas_theorems`

## Failure Logging

If the plan does not solve the problem after attempting all subgoals, include
one `failed_paths` summary in the same batch checkpoint. Do not emit a separate
record for each temporary obstruction.
