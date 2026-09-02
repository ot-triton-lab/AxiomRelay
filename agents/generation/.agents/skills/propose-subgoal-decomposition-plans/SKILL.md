---
name: propose-subgoal-decomposition-plans
description: Produce exactly three materially different, scope-disjoint proof routes for one safe parallel fanout. Use after the protected root route-design phase has enough information to avoid duplicate or obviously dead directions.
---

# Propose Subgoal Decomposition Plans

Use this skill when the root has enough context to define three independent
directions without fragmenting one mechanism into cosmetic variants.

## Input Contract

Read:

- the current target theorem or branch goal
- relevant `immediate_conclusions`, `toy_examples`, and `counterexamples`
- relevant `failed_paths` and `branch_states`
- recent search results and useful references from `events`

## Procedure

1. Gather the current information that materially constrains the problem: useful examples, failed claims, known obstructions, and relevant search results.
2. Propose exactly three plans. They must use materially different mechanisms,
   have scope-disjoint first obligations, and include a discriminating test
   that can kill or advance that route.
3. For each plan, state:
   - the main idea of the plan
   - the ordered subgoals
   - why this plan is plausible given the current information
   - which earlier failures or counterexamples it tries to avoid
4. Screen the three plans only for duplication, obvious contradiction, and
   basic viability. Do not exhaust them sequentially at the root.
5. Return the whole set for one pre-fanout checkpoint, then hand it to
   `$recursive-proving` so all three context-free solvers start in one fanout.
   If a complete candidate already exists, skip the fanout and verify it.

## Output Contract

Return the selected plan set as one consolidated `subgoals` record for the
root's next `memory_append_batch` checkpoint:

```json
{
  "record_type": "decomposition_plan_set",
  "goal": "...",
  "plans": [
    {
      "plan_id": "...",
      "role": "route_solver_1|route_solver_2|route_solver_3",
      "mechanism": "...",
      "scope": "...",
      "discriminating_test": "...",
      "plan_summary": "...",
      "subgoals": ["..."],
      "motivation": ["..."]
    }
  ],
  "uses_information_from": {
    "examples": ["..."],
    "counterexamples": ["..."],
    "key_failures": ["..."],
    "search_results": ["..."]
  },
  "status": "ready_for_fanout|failed|solved",
  "branch_id": "optional"
}
```

Do not append a second event that merely restates the same plan set.

## MCP Tools

- `memory_search`
- `memory_append_batch`
- `search_matlas_theorems`

## Failure Logging

If the agent cannot yet propose exactly three meaningful independent plans,
do not spawn a partial fanout. Return an event
payload for the next phase checkpoint with:

- `event_type="decomposition_plans_not_ready"`
- the missing information
- the blockers that prevent proposing plans
