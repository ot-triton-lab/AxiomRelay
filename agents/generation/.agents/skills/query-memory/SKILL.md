---
name: query-memory
description: Retrieve a small, targeted slice of prior durable memory for a concrete proof decision. Use once when rehydrating a continued run, or later when a named claim, subgoal, counterexample check, or branch decision depends on records not already in active context.
---


# Query Memory

Use this skill only when the needed durable state is not already in active
context. Do not repeatedly search memory during one coherent reasoning phase.

## Input Contract

Read:

- the current question, claim, subgoal, or branch decision
- the specific type of prior artifact you want to recover
- the most relevant channel list, chosen from:
  - `immediate_conclusions`
  - `toy_examples`
  - `counterexamples`
  - `failed_paths`
  - `branch_states`

## Procedure

1. Form a concrete natural-language query describing the information you want to recover.
2. Choose the smallest relevant list of channels instead of searching everything by default.
3. Call `memory_search(problem_id, query, channels=..., limit_per_channel=..., max_chars=...)`. Omit `max_chars` to start with the default 20,000-character budget and increase it only when a broader context is necessary.
4. Check `complete`, `truncated`, `omitted_count`, `omitted_ids`,
   `omitted_ids_complete`, `returned_chars`, and `corpus_count` before
   interpreting the hits. The returned character count covers compact, whole
   result records; records are never partially sliced. Omitted ID samples are
   capped, so use `omitted_count` rather than the sample length as the total.
5. Inspect the highest-relevance active hits in each requested channel. BM25 relevance is primary; newer records come first only when scores are tied or nearly tied. Use `include_inactive=true` only when auditing superseded or explicitly inactive history.
6. If `truncated=true`, treat the response as partial: narrow the query or channel list, or deliberately retry with a larger `max_chars`. Never infer that an omitted fact does not exist.
7. Summarize the useful retrieved items in working context and explain how they
   affect the current proof state.
8. Do not issue a second broad query in the same phase. If no useful item is
   found, continue reasoning from the authoritative problem and active
   artifacts. Only describe the search as exhaustive when `complete=true`.

## Output Contract

If the retrieval changes the proof route, include this compact event in the
next `memory_append_batch` phase checkpoint. Do not persist a query event that
merely re-reads already known state:

```json
{
  "event_type": "query_memory",
  "query": "...",
  "channels": ["counterexamples", "failed_paths"],
  "limit_per_channel": 10,
  "results_summary": ["..."],
  "useful_hits": [
    {
      "channel": "counterexamples",
      "score": 0.0,
      "why_relevant": "...",
      "record_excerpt": "..."
    }
  ],
  "branch_id": "optional",
  "subgoal_id": "optional"
}
```

## MCP Tools

- `memory_search`
- `memory_append_batch`

In released runs, include replacements in the next batch and use that batch's
record ids in later `supersedes` lists. The legacy single-record append is
offline/local only.

## Failure Logging

If the retrieval is not useful, do not spend another memory write merely to
record the miss. Mention it only in a later phase checkpoint when the miss
itself changes a branch decision.

When such a miss is decision-relevant, use:

- `event_type="query_memory_stalled"`
- the attempted query
- the channels searched
- the reason the retrieved items were not useful
