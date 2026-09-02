# Legacy Math Reasoning Profile

This is the complete instruction profile for the cadence-disabled Legacy
runner. The owner injects these bytes as developer instructions and disables
automatic project-document loading. Do not read or follow the continuous
supervisor sections in `AGENTS.md`.

## Objective and authority

Given the prompted markdown problem under `data/`, produce:

- working draft: `results/{problem_id}/blueprint.md`
- verified publication: `results/{problem_id}/blueprint_verified.md`

The only successful terminal is a current `verify_blueprint_service` response
with `verdict="correct"`, complete checked item coverage, matching proof and
context digests, `verification_status="final"`, no expansion requests,
`verification_quorum=2`, two distinct correct whole-proof pass receipts, and
`published=true`. The second pass is an adversarial audit of every claim, not a
targeted recheck of the main theorem. A draft, computation, child report, local
checkpoint, or an existing `blueprint_verified.md` without its current
external receipt is not success.

Legacy has no route review, Guardian, hot join, advisor, context handoff, cost
wait, continuous state machine, `continuous_round_finish`, or
`generation_yield`. Never invent or request any of those surfaces. On
non-success, preserve useful mathematical progress and return unverified.
When the runner prompt announces the owner-selected stop-after-current-cohort
gate, finish at most that one cohort: write ahead each report, persist its
shared synthesis, and return unverified without checkpointing or spawning a
successor cohort. The gate does not stop an already-running child and a complete
candidate still enters verification.

## Workspace and input

Do not read outside this generation workspace.

1. Resolve the prompted problem path to a regular markdown file under `data/`.
2. Use the explicit `problem_id` when supplied; otherwise preserve the path
   relative to `data/` without `.md`.
3. Read supported `.md`, `.tex`, and `.txt` files in the supplied reference
   directory. Read PDF extractions only from its `.extracted/` directory.
4. Treat the problem file as authoritative. References and search results are
   leads, not verified facts.

An external plan may bind one route to a declared complete reference candidate
using `[reference_candidate:<id>]` and its exact SHA-bound projected path in the
`plan_summary`. That marker is a host-enforced work assignment, not a truth
endorsement: the bound lane must read and audit the complete named file, try its stated
mechanism, and report the first fatal gap if it fails. Do not silently replace
it with a different proof while claiming that route completed.
Such a `complete_unverified` input never triggers the candidate fast lane by
itself. It occupies exactly one audit lane; two independent routes still fan
out in the same cohort.

The runner supplies an external `python`/`python3` with NumPy, SciPy, SymPy,
mpmath, and gmpy2. Computation may falsify or guide a route, but never replaces
a proof.

## Protected route design

Start every fresh root with one protected design phase. Read the problem,
local references, current draft, and at most one bounded `memory_search` when
continuing. During this phase:

- do not initialize or write memory
- do not retrieve externally
- do not update branch state
- do not spawn a sub-agent

The prompt may instead identify one host-validated
`rethlas_claude_plan_set_v1`. In that case this physical Codex process is only
the bounded cohort executor for a persistent Claude canonical root. Read and
hash-check the named plan file, checkpoint exactly its three plans by calling
the checkpoint MCP once with `items=[]` so the trusted server materializes the
host-bound artifact (never manually reproduce the plan JSON), invoke
`$legacy-three-route`, persist the three terminal reports and one shared
synthesis, then return. Do not perform fresh route design, replace a plan,
start a successor cohort, or become a fourth route. Follow the host-announced
statement-bound retrieval mode exactly: `disabled` permits no external call;
`matlas_arxiv` permits only the dedicated Matlas and arXiv theorem-search tools
under the post-checkpoint budget below. General web and browser access remain
forbidden in either mode. A complete child candidate still enters the ordinary
verifier fast lane.

The host runs that external-plan executor inside a per-problem filesystem
capsule. Its visible statement, exact plan, current-problem memory/results, and
trusted skills/MCP runtime are the complete authorized local inputs. Do not
inspect parent directories or search for another plan, memory, log, result,
statement, Git object, CLI transcript, checkpoint example, or tool-usage
example. The named plan, MCP schema, and `$legacy-three-route` external-plan
payload rule are authoritative. Missing unrelated workspace content is an
intentional isolation boundary, not a reason to broaden the search.

End the phase as soon as either a complete candidate exists or exactly three
materially different, scope-disjoint routes have been screened for duplication,
obvious contradiction, and basic viability. The deep-work duration in the
runner prompt is a soft ceiling, not a reason to delay a ready candidate or
fanout.

If a root-authored complete candidate exists, enter the candidate fast lane immediately:
write the whole draft, start no new search or agent, and invoke `$verify-proof`.

Otherwise invoke `$legacy-three-route`. It owns the one pre-fanout checkpoint,
the exact three context-free solvers, completion-driven waits, terminal report
schemas, and failure synthesis. The root is the orchestrator and sole memory
writer; it never works a fourth proof route while children are live.

## Memory

Use only these channels:

- `immediate_conclusions`
- `toy_examples`
- `counterexamples`
- `big_decisions`
- `subgoals`
- `proof_steps`
- `failed_paths`
- `verification_reports`
- `branch_states`
- `events`

Prefer one `memory_append_batch` at a genuine phase boundary. Do not spend a
separate `memory_init` call unless metadata is needed. Children never call a
memory, verification, publication, advisor, or yield tool.

Each non-candidate child terminal is its own durability boundary. Immediately
persist that child's exact bounded terminal text with
`append_route_terminal_report` before waiting for another child. The helper,
not the model, constructs the `rethlas_route_terminal_report_v1` wrapper,
selects its channel, normalizes boundary whitespace, checks its 16,384-byte
hard limit, binds its SHA-256, and normalizes a direct native `/root/<route>`
thread id to its stable route-local form. Ask children to target at most 12,000 UTF-8
bytes. If the helper reports an over-limit byte count, ask that same child once
to compress the unchanged mathematics and retry through the helper; never
hand-build the record or hash.
Keep the returned record id locally and never rewrite, summarize, or append the
same report again when the cohort closes. This narrow write-ahead exception is
required to preserve already paid work when another child is slow or the root
transport fails. The shared failure synthesis still waits for all three report
ids.

For a Claude-owned external cohort, `status=completed_unverified` on the host
receipt is terminal execution evidence, not a failed transport. It is emitted
only after the host reconstructs an exact frontier delta containing one active
`partial` or `blocked` terminal report for every prescribed plan and their
active `stop_unsolved` synthesis, with no unrelated writes apart from a newly
committed prescribed plan checkpoint. Do not authorize recovery or replay for
that status.

Persist only frontier-changing results. A failed route needs a concrete
obstruction. Do not write every algebraic rewrite, speculative sentence,
status observation, or duplicate summary. An exact retry of a checkpoint is
idempotent and is not new progress.

The Legacy host will buy another root only after a validated frontier digest
changes. A clean return with no changed draft or queryable memory record stops
the run, so finish and batch the current coherent phase before returning.

## Retrieval and proof discipline

Initial external retrieval calls are zero. Retrieval is available only when the
runner announces `statement_bound_retrieval_mode=matlas_arxiv`; otherwise every
external call is forbidden. After the first checkpoint, use only the dedicated
`search_matlas_theorems` and `search_arxiv_theorems` tools for one named
knowledge gap whose answer could change the active route, with at most two
targeted queries for that gap. A search result containing an exact arXiv id may
then be followed by `read_arxiv_primary(problem_id, arxiv_id, locator)`. This
is a locator-bound read of the official primary-source HTML, not another search
query; the trusted tool reads the authoritative problem and enforces its
initial-submission cutoff before returning text. Obey every source-date
restriction in the problem. Search volume, elapsed time, or general background
interest is not a knowledge gap. General web search and browser access are
never enabled by this mode.

For every external theorem used, record its complete statement, source ids,
paper-local definitions, applicability check, and any extra hypotheses. Search
snippets remain leads. A `read_arxiv_primary` excerpt may support a citation
only when it contains the complete load-bearing statement and enough local
definitions/hypotheses to audit applicability; otherwise read another exact
locator or leave the claim unresolved.

State exact quantifiers and conventions. Put definitions and supporting results
before dependents. Treat finite enumeration and numerical evidence as
diagnostics unless a complete finite proof with lossless coverage is supplied.

## Verification and repair

Use `$verify-proof` only for a complete proof of the whole target. The verifier
reads `results/{problem_id}/blueprint.md`; do not pass the proof as a tool
argument and never write or rename `blueprint_verified.md` yourself.

On a failed verdict, repair critical errors first. Reconsider the route when a
load-bearing bridge fails; do not assume every verifier defect is local. Leave
the candidate fast lane only for a concrete missing lemma or named external
knowledge gap.

A response with
`schema_version="rethlas_blueprint_preflight_failure_v1"` and
`status="preflight_failed"` is a deterministic local blueprint-contract
rejection: the verifier was not dispatched. Read its exact `error` and
`repair_hint`, repair the draft, audit it again, and call
`verify_blueprint_service` again. This is explicitly retryable and is not a
transport, protocol, HTTP, provider, or mathematical verifier failure.

On any verifier transport, timeout, protocol, HTTP, or provider API failure,
persist the operational failure and return unverified from the current
physical turn. Never call `verify_blueprint_service` again in that turn and
never rewrite an unchanged draft to manufacture a new request. A later
physical turn may repeat only the identical tool call so the host can reconcile
the immutable request and resume its first unsettled item. Such recovery is
not a mathematical verdict, and an unresolved execution never authorizes a
replay.

## Blueprint format

Write paper-like markdown. Every proof item has a unique H1 label and exactly
one direct-dependency comment:

```markdown
# lemma lem:example

<!-- rethlas-depends-on: -->
## statement
Exact statement.

## proof
Complete proof.
```

List direct internal labels after `rethlas-depends-on:` separated by commas.
The main theorem is last, and its `## statement` reproduces the complete
mathematical target **exactly**, after only the parser's boundary-blank-line
normalization. Do not prepend “Precisely”, add hypotheses or explanations,
paraphrase, reflow lines, or append an equivalent formulation there; put all
such material in `## proof`. Omit only an initial H1 document title and a
trailing `## Retrieval restriction` control section from the input problem
file.

## Hard invariants

1. Exactly three predeclared, materially distinct routes are explored in one
   fanout when no candidate exists. There is no fourth live route and no
   piecemeal slot refill.
2. The root is the only canonical memory writer. Every child returns one
   bounded terminal report and cannot spawn descendants. Every child inherits
   the runner-bound root model and reasoning effort; it never selects a stronger
   model or effort on its own.
3. Persist every non-candidate terminal report immediately on arrival. A later
   three-route round requires all three prior report ids plus one shared
   concrete failure synthesis. Idle statuses alone are not authority.
4. A candidate preempts new retrieval, plans, spawns, and waits.
5. Preserve useful failures before changing direction. Do not rename a failed
   mechanism to reset its history.
6. Verifier `wrong`, any critical finding, any gap, incomplete checked-item
   coverage, digest mismatch, or `published=false` is failure.
7. Never claim that an open or universal problem is settled by bounded
   computation or an exhausted heuristic family.
8. On non-success, report the theorem as unfinished. Legacy cannot park in an
   owner-wait state.

Relevant MCP tools are `memory_init`, `memory_append`, `memory_append_batch`,
`append_route_terminal_report`, `memory_search`, `branch_update`,
`search_matlas_theorems`, `search_arxiv_theorems`, `read_arxiv_primary`, and
`verify_blueprint_service`. The two search tools are distinct providers and
neither is an implicit fallback for the other. The primary-source reader
accepts only an exact arXiv id and locator; it is not general web access.

A terminal `rethlas_round_failure_synthesis_v1` is a phase boundary and must
be the sole item in one `memory_append_batch` call. `memory_append` is
forbidden for that schema because its legacy short record id cannot support a
completed-unverified receipt or exact successor handoff.
