---
name: obtain-immediate-conclusions
description: Derive and internally stress-test immediate mathematical consequences during a coherent root reasoning phase. Use at the start of a problem or after a genuine reformulation, without external retrieval or per-conclusion memory writes.
---

# Obtain Immediate Conclusions

Extract direct implications before speculative reasoning.

## Input Contract

Read from memory and current context:

- `problem_id`
- current theorem/subgoal statement
- memory

## Procedure

1. Normalize notation and restate the claim in equivalent forms.
2. List direct consequences that follow from definitions and basic algebraic/logical manipulations.
3. Split consequences into necessary conditions and candidate sufficient conditions.
4. Mark each consequence with confidence and justification type.
5. For every conclusion, explicitly decide whether it is likely fragile and should be stress-tested by counterexample.
6. If a conclusion is fragile, record why it is fragile and indicate that `$construct-counterexamples` should be considered next.

## Output Contract

Return the durable conclusions as one consolidated record for the root's next
`memory_append_batch` checkpoint. Omit transient rewrites and duplicate
restatements:

```json
{
  "conclusions": [
    {
      "statement": "...",
      "justification_type": "by_definition|calculation|known_fact|logical_equivalence",
      "confidence": 0.0,
      "is_fragile": false,
      "fragility_reason": "",
      "suggested_followup": "none|construct-counterexamples"
    }
  ],
  "scope": "global|branch|subgoal",
  "branch_id": "optional",
  "subgoal_id": "optional"
}
```

Rules:

- `is_fragile` must always be present.
- If `is_fragile=true`, then `fragility_reason` must explain the risk and `suggested_followup` should be `construct-counterexamples`.
- If `is_fragile=false`, use `fragility_reason=""` and `suggested_followup="none"`.

## MCP Tools

- `memory_append_batch`
- `memory_search`

Do not use external retrieval merely because an immediate consequence is
nontrivial. A named external knowledge gap is handled later by
`$search-math-results`.

## Failure Logging

If no meaningful consequence is found, include an event in the next phase
checkpoint only when the stall changes the proof route:

- `event_type="immediate_conclusions_stalled"`
- missing assumptions and suspected blockers
