---
name: verify-proof
description: Verify candidate proofs with the local proof verification MCP service. Use only when a full candidate proof of the entire problem has been assembled in markdown, and before publishing the final verified blueprint.
---

# Verify Proof

Use the local proof verification service as the canonical verifier before accepting a solution.
Do not use this skill for partial proofs, isolated subgoals, or branches that have not yet produced a full proof draft of the whole problem.

## Input Contract

Read:

- target theorem statement
- assembled proof blueprint candidate from `results/{problem_id}/blueprint.md` as pure markdown text
- relevant prior failure reports and branch context

## Procedure

1. Read the current `results/{problem_id}/blueprint.md` draft as pure text.
2. First check that `blueprint.md` contains a full proof draft of the entire target theorem rather than a partial proof, fragment, or exploratory notes. If it does not, do not call the verifier yet.
3. Call MCP tool `verify_blueprint_service` with:
   - `problem_id`: the current data-relative problem id
   Do not pass the target statement or raw blueprint text as tool arguments.
   The tool reads the target from `data/{problem_id}.md` and checks the runner's
   bound source digest.
4. Read `verification_report.summary`, `critical_errors`, `gaps`, `verdict`,
   `repair_hints`, `checked_item_ids`, both digests, and `published`.
5. Persist the complete MCP publication envelope at this terminal phase
   boundary with one `memory_append_batch` record in `verification_reports`,
   without renaming or dropping fields. It contains the six-field HTTP verification result plus
   `published`; successful publication also includes `published_path` and
   `publication_receipt_path`.
6. Treat the proof as failed if any of the following hold:
   - `verdict` is `"wrong"`
   - `verification_report.critical_errors` is non-empty
   - `verification_report.gaps` is non-empty
7. Only treat the proof as passed when none of the failure conditions above
   hold and `published=true`.
8. Never rename or copy the draft yourself. Atomic publication is performed
   inside the tool after digest and coverage validation.
9. If the tool fails through transport, timeout, protocol, HTTP, or provider
   API error, persist that operational failure at the phase boundary and return
   from the current physical turn immediately. Never call the verifier again
   in that same turn, never convert the error into `wrong` or `correct`, and
   never rewrite the unchanged draft to create another request. A later
   explicit physical turn may repeat the identical call only for host
   reconciliation and item-level resume.

## Output Contract

Append to `verification_reports`:

```json
{
  "verification_report": {
    "summary": "string",
    "critical_errors": [
      {"location": "", "issue": "detailed description of the issue"}
    ],
    "gaps": [
      {"location": "", "issue": "detailed description of the gap"}
    ]
  },
  "verdict": "string",
  "repair_hints": "string",
  "checked_item_ids": ["pi_0123456789abcdef01234567"],
  "proof_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "context_digest": "1111111111111111111111111111111111111111111111111111111111111111",
  "published": true,
  "published_path": "results/problem/blueprint_verified.md",
  "publication_receipt_path": "../.verification_receipts/problem.json"
}
```

These concrete IDs and digests illustrate the response shape; the tool returns
the values bound to the current blueprint.

Persist the verification service response exactly as returned.

An operational failure may not contain a complete service response. Persist a
bounded failure record with the unchanged proof digest and the sanitized
transport or protocol category, then return unverified. Do not fabricate any
publication-envelope fields.

If verification fails, revise `blueprint.md` directly. Include any branch
invalidation in the same phase-boundary batch under `failed_paths`; do not emit
one memory write per verifier field.

The candidate fast lane permits no retrieval by default. Leave it only when a
concrete verifier defect identifies a named missing lemma or external knowledge
gap whose answer is necessary for repair. In that case use
`$search-math-results` under its two-query budget, then return to coherent
reasoning. A generic failed verdict, elapsed time, or uncertainty is not such a
gap.

## MCP Tools

- `verify_blueprint_service`
- `memory_append_batch`
- `memory_search` only for one bounded missing-state rehydration
- `$search-math-results` only for a verifier-identified named knowledge gap

## Failure Logging

Always persist verification output, including successful checks, in the one
phase-boundary batch.
