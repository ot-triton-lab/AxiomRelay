---
name: check-referenced-statements
description: Validate externally referenced theorems against arXiv or other authoritative sources with built-in web search. Use when a markdown proof cites statements from external papers.
---

# Check Referenced Statements

Validate every external-paper reference used in the proof.

## Input Contract

For each cited external theorem/lemma/definition:

- location where it is used,
- the full referenced statement text.

## Procedure

1. Use built-in web search with the full referenced statement, targeting arXiv
   and the cited paper or its authoritative source first.
2. Inspect returned results and compare theorem text directly to the referenced statement in reasoning.
3. Expand the definitions and terminology appearing in the cited statement using the cited paper's context before deciding whether the theorem applies.
4. Check whether the same words in the current proof mean the same thing as they do in the cited paper. In mathematics, identical words can carry different definitions in different contexts.
5. Distinguish similar-looking definitions and compare their exact formulas, notation, and quantifiers. Do not collapse two definitions just because the names or formulas look close.
6. Accept as matched and applicable only when both are true:
   - the result clearly corresponds to the cited statement,
   - the contextual definitions and hypotheses align with the current problem.
7. If the proof uses the referenced statement to obtain further conclusions, verify the transition from the referenced statement to those conclusions.
8. Treat a hand-wavy specialization, instantiation, or downstream deduction as a gap even when the cited theorem itself is valid.
9. Treat a logically invalid transition from the cited theorem to the claimed conclusion as a critical error.
10. If that downstream step deduces one property from another, compare the exact definitions and defining formulas of both properties before accepting the deduction.
11. If the theorem exists but the current proof uses different definitions, hypotheses, ambient objects, or a subtly different defining formula, record a critical error for incorrect application.
12. If no match is found, broaden built-in web search while preserving the
    exact statement and citation details.
13. If still not found, emit a critical error:
   - location: where the citation is used,
   - issue: referenced theorem appears non-existent or incorrectly cited.
14. Keep each reference check in the current response's finding ledger.

Do not rely on dedicated comparison utility code; perform comparison through careful reasoning.

## Output Contract

Keep records in the finding ledger like:

```json
{
  "location": "Lemma 2",
  "referenced_statement": "Exact statement text",
  "context_expansion": "In the cited paper, 'regular' means regular with respect to the valuation topology.",
  "arxiv_match_found": false,
  "web_match_found": false,
  "critical_error": {
    "location": "Lemma 2",
    "issue": "Referenced external theorem was not found in arXiv search or Codex built-in web search."
  }
}
```

## Tool Policy

- Codex's built-in web search and the injected `search_arxiv_theorems` MCP tool
  are allowed only for external citations.
- Do not use search for internal proof discovery or expansion. Do not use MCP
  validation or output tools; direct schema-constrained output is authoritative.
