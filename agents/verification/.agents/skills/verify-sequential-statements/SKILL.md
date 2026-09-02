---
name: verify-sequential-statements
description: Verify the current content-addressed proof item against its authenticated lazy premise context, checking every deduction in order.
---

# Verify Sequential Statements

Check every deduction in the current proof item and log all local issues.

## Input Contract

Read the current item, premise cards, and `expanded_proofs` from `Fact_context`.
The current item always contains its complete proof. Premise cards contain the
complete strict-ancestor statement/edge closure and never contain proofs.
`expanded_proofs` is empty in round zero and contains only exact, complete
strict-ancestor records requested in earlier rounds. `Target_statement`
contains the overall theorem and hypotheses.

## Procedure

1. Confirm that the context is complete, untruncated, and names exactly one
   current item. Stop with a gap if it is not.
2. Extract assumptions and hypotheses from the current item statement and the
   relevant portion of `Target_statement`.
3. Iterate through every deduction in the current item's proof in textual order.
4. For each deduction, use a location key prefixed by the current item id.
5. Check local reasoning:
   - Is the inference valid?
   - Are assumptions stated and sufficient?
   - Is each theorem application valid in context?
   - Are there skipped or hand-wavy steps?
   - Do similar-looking definitions actually match exactly?
   - Do similar-looking formulas in those definitions differ in a way that matters for the argument?
   - If the proof deduces one property from another, do the exact definitions and defining formulas of those two properties really support that deduction?
   - For each small deduction step, do all assumptions needed for that step actually hold?
6. For each use of an earlier lemma, ensure that its exact premise statement
   is present in the supplied dependency context and actually applies. Do not
   recover undeclared lemmas from memory.
7. If a deduction genuinely cannot be checked from the exact ancestor
   statement, request the smallest set of strict-ancestor proof ids needed.
   Do not search for internal proofs, infer project paths, or request the
   current/unknown/non-ancestor/already-expanded ids.
8. When a requested ancestor proof is present in `expanded_proofs`, check its
   relevant reasoning as well. An error exposed there makes the current item
   finally wrong and therefore blocks publication.
9. Pay special attention to assumptions that an object exists or satisfies a property. Sometimes such an object has not been constructed, or it exists but has not been proved to satisfy the claimed property.
10. Audit whether the assumptions from the current item statement are actually used in the proof.
11. If some assumptions seem unused, do not assume they are harmless. Reason carefully about whether:
   - the assumption is truly redundant, or
   - the proof is silently omitting a necessary use of it and therefore has a gap or error.
12. Classify findings:
   - `critical_error`: logical contradiction, invalid theorem use, false implication.
   - `gap`: missing derivation, vague justification, unsupported step, unjustified existence or property assumptions about objects, suspiciously unused assumptions whose role is not justified, failure to distinguish between similar-looking definitions or formulas, or a hand-wavy deduction from one property to another.
13. Keep a structured check for the supplied current item in the current
    response's finding ledger. Do not call memory tools and do not claim
    coverage of any other item.

## Output Contract

Keep records in the finding ledger with structure like:

```json
{
  "location": "Lemma 3",
  "status": "checked",
  "critical_errors": [
    {"location": "Lemma 3", "issue": "Incorrect implication from A to B."}
  ],
  "gaps": [
    {"location": "Lemma 3", "issue": "Missing justification of boundedness."}
  ]
}
```

## Tool Policy

Never use MCP or Graphify for internal proof discovery/hydration, completeness,
digests, or output. External-reference search is allowed by the reference skill.
