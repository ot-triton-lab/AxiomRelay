---
name: construct-toy-examples
description: Generate and analyze simpler examples that satisfy both the assumptions and the conclusion of a theorem statement or subgoal. Use when you are stuck in reasoning and need simpler examples to regain traction, when you need simpler examples that satisfy both assumptions and conclusion, or when you want to see where the assumptions take effect and gain intuition.
---

# Construct Toy Examples

Use this skill after the protected root phase has produced a specific
obstruction and simpler examples could distinguish concrete mechanisms. Do not
invoke it during the protected phase or merely because the problem is hard.

## Input Contract

Read:

- current statement/subgoal
- relevant `immediate_conclusions`
- relevant `counterexamples` and failed branch notes
- relevant background/results when available

## Procedure

1. Construct simpler cases (low degree, small dimension, special forms, canonical objects).
2. Ensure the toy example satisfies all assumptions of the target statement or subgoal.
3. Check that the conclusion also holds in the toy example.
4. Study where each assumption takes effect and what mechanism makes the conclusion true.
5. Identify repeated patterns, invariants, or proof ideas suggested by the example.
6. Construct and analyze examples locally first. If one named external
   knowledge gap remains and its answer could change the active route, delegate
   that gap to `$search-math-results` under its two-query budget. Do not start a
   general example survey.

## Output Contract

Include the consolidated result in the root's next
`memory_append_batch` checkpoint under `toy_examples`:

```json
{
  "example": "...",
  "why_relevant": "...",
  "assumptions_satisfied": ["..."],
  "conclusion_verified": true,
  "where_assumptions_take_effect": "...",
  "observed_pattern": "...",
  "supports_branch_ids": ["optional"],
  "subgoal_id": "optional"
}
```

## MCP Tools

- `memory_append_batch`
- `memory_search` only for one bounded continuation-state rehydration
- `$search-math-results` only for one named knowledge gap

## Failure Logging

If generated examples are inconclusive, include one compact `events` record in
the same phase-boundary batch:

- `event_type="toy_examples_inconclusive"`
- include attempted example families
