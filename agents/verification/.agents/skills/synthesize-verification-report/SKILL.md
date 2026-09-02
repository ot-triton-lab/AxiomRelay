---
name: synthesize-verification-report
description: Aggregate all detected errors and gaps into the final verification report, apply strict accept/reject logic, and produce repair hints when rejected.
---

# Synthesize Verification Report

Produce either a bounded adaptive proof request or the final verification JSON.

## Input Contract

Read all statement and reference findings from the current response's finding
ledger.

Each issue must include `location` and `issue`.

Also read the exact `Expected_checked_item_ids`, `Proof_digest`, and
`Fact_context.digest` values from the API prompt.

## Procedure

1. If checking an internal deduction genuinely requires one or more exact
   strict-ancestor proofs not yet supplied, return a protocol request with
   `verification_status="needs_context"`, `verdict="wrong"`, empty findings,
   empty repair hints, and unique `{id, reason}` requests. This is never a
   mathematical verdict or acceptance.
2. Otherwise review the complete in-response finding ledger. Account for every deduction
   and citation before aggregation rather than silently dropping records.
3. Collect all critical errors and all gaps from previous checks.
4. Build a complete `verification_report` object with:
   - `summary`
   - `critical_errors`
   - `gaps`
5. Apply strict final verdict rule:
   - `correct` iff `critical_errors=[]` and `gaps=[]`.
   - otherwise `wrong`.
6. If final verdict is `wrong`, produce concrete non-empty `repair_hints`.
7. Copy the expected item id list and both supplied digests exactly into the
   output; never invent, normalize, or recompute them.
8. Check the output locally against the contract described below.
9. Return only the JSON. Do not call an MCP tool or write a file; the CLI
   captures the schema-constrained last message and the API validates it.

## Output Contract

Final output JSON:

```json
{
  "output_schema_version": 2,
  "verification_report": {
    "summary": "string",
    "critical_errors": [],
    "gaps": []
  },
  "verification_status": "final",
  "verdict": "correct",
  "repair_hints": "",
  "needs_expanded_proofs": [],
  "checked_item_ids": ["pi_0123456789abcdef01234567"],
  "proof_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "context_digest": "1111111111111111111111111111111111111111111111111111111111111111"
}
```

Those concrete values illustrate the shape only; use the exact item ID and
digests supplied for the current request.

If there is any error or gap, verdict must be `"wrong"` and `repair_hints` must be non-empty.

## Tool Policy

Do not use MCP validation or output tools. Never use search, memory, or Graphify
to hydrate internal proofs. Return the JSON directly.
