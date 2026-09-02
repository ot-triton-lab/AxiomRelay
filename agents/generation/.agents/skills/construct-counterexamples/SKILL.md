---
name: construct-counterexamples
description: Construct candidate counterexamples to test a proposed conjecture, lemma, or intermediate claim by keeping the assumptions true while making the claimed conclusion fail. Use when you are stuck in reasoning and want to see where the assumptions take effect and gain intuition, when a proposed conjecture/claim feels fragile or unproved, or when you want to test whether the assumptions can hold while the claimed conclusion fails.
---

# Construct Counterexamples

Actively falsify proposed conjectures or intermediate claims by finding examples that satisfy the assumptions but violate the claimed conclusion.

## Input Contract

Read:

- the specific conjecture/claim to test
- active branch assumptions
- candidate lemmas/proof steps
- current `immediate_conclusions` and `toy_examples`
- previously found counterexamples that can be reused against new claims

## Procedure

1. Identify the assumptions that must hold and the conclusion to fail.
2. First use coherent local reasoning and, when useful, bounded exact or
   numerical computation to search for an obstruction. Use retrieval only
   after naming a specific missing counterexample family whose existence would
   change the route.
3. Decide status:
   - `refuted`: assumptions hold and the claim fails
   - `not_refuted`: no counterexample found yet
   - `inconclusive`: search space unclear or partially explored
4. If the search produces a concrete example that is informative but is not actually a counterexample, save that example as well in `toy_examples`.
5. If refuted, store the counterexample for reuse against future claims and mark impacted branches/lemmas as invalid.
6. If no counterexample is found, treat that only as evidence that the claim may be correct, not as a proof.

## Output Contract

Return one consolidated counterexample result for the root's next
`memory_append_batch` checkpoint:

```json
{
  "target_claim": "...",
  "candidate_counterexample": "...",
  "status": "refuted|not_refuted|inconclusive",
  "assumptions_satisfied": ["..."],
  "failed_conclusion": "...",
  "impact": "...",
  "branch_id": "optional",
  "subgoal_id": "optional"
}
```

If `status="refuted"`, include one `failed_paths` item in the same batch when it
kills a branch.

If the search produced a durable non-refuting example, include one
`toy_examples` item in the same batch:

```json
{
  "example": "...",
  "why_relevant": "constructed while testing the claim ...",
  "assumptions_satisfied": ["..."],
  "conclusion_verified": true,
  "where_assumptions_take_effect": "...",
  "observed_pattern": "...",
  "supports_branch_ids": ["optional"],
  "subgoal_id": "optional"
}
```

Do this whenever the constructed example is useful enough to test future claims or clarify the current branch, even if it did not refute the target claim.

## MCP Tools

- `memory_append_batch`
- `memory_search`
- bounded external retrieval only through the named-gap policy
- reuse stored counterexamples to test future conjectures/claims

## Failure Logging

If no meaningful counterexample space is identified, include this event only
when it changes the branch decision:

- `events.event_type="counterexample_space_unclear"`
