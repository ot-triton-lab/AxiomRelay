# AxiomRelay Role Model Policy and Persistent Root Design

Status: accepted design, zero-model resolver and cold Claude Opus 5 1M
Verifier implemented, paid dual-model verifier canary passed

This document is authoritative for adding selectable Root, Generator, and
Verifier models without weakening AxiomRelay compatibility, memory, exact-three
topology, or publication authority.

## Decision

AxiomRelay uses a provider-neutral logical Root with replaceable native model
sessions and durable AxiomRelay memory.

- A Root may use Codex CLI or Claude CLI.
- Exactly three host-admitted Generator lanes remain the default and maximum.
- Generator lanes are isolated from the Root transcript.
- Publication requires two cold whole-proof Verifier passes.
- The two Verifier passes may use different models and providers.
- A model, provider, or effort mismatch never silently falls back.
- A physical CLI process ends after one Root turn. The logical Root persists.
- Legacy and reviewed execution remain separate compatibility surfaces.

Model choice does not transfer hidden model state between providers. A Root
takeover transfers only durable AxiomRelay artifacts and an authenticated handoff.

## Goals

1. Let the owner choose Root, Generator, Verifier 1, and Verifier 2 models.
2. Reduce correlated proof-verification failures through model diversity.
3. Preserve Root memory across process exits, CLI updates, compaction, and
   explicit model changes.
4. Keep Generator and Verifier contexts isolated by default.
5. Bind every paid effect to one immutable model policy and actual model
   receipt.
6. Preserve current behavior when no new model policy is selected.
7. Reject unavailable adapters or models before any paid turn when possible.

## Non-goals

- A forever-running Claude or Codex process is not required.
- Hidden reasoning state is not portable between Claude and Codex.
- Verifiers do not inherit Root or Generator conversation history.
- Model diversity does not replace proof digests, context reconstruction, or
  verifier-gated publication.
- The first implementation does not assign a different model to every
  Generator lane.
- An automatic third Verifier is not added.

## Current Baseline

The existing runtime has three different persistence behaviors.

| Actor | Current process behavior | Current memory behavior |
|---|---|---|
| Claude Root | One headless process per turn, resumed by Claude session id | Native Claude session plus AxiomRelay durable memory |
| Opus-Astra route council | Three bounded Astra/max calls around Opus's merge and one adjudication pass; only statement-authorized Matlas/arXiv retrieval is exposed | Immutable phase and retrieval receipts only; no shell, workspace, general web, lane, or canonical-memory authority |
| Legacy Codex Root | Fresh `codex exec --ephemeral` iteration | AxiomRelay durable memory only |
| Reviewed Codex Root | Persistent app-server thread with epoch handoff | App-server context plus durable ledger and memory |
| Generator | Context-free Codex lanes inside one admitted cohort | Terminal reports and canonical memory only |
| Verifier | Fresh ephemeral Codex execution | No cross-pass conversation memory |

The local Codex CLI supports `codex exec resume <session_id>`. A new persistent
Codex Root adapter may use it, but it must not modify the Legacy runner. The
resume command does not expose all initial launch flags, including the same
working-directory and sandbox flags, in its public command surface. Therefore
the adapter must attest the resumed session's persisted working directory,
sandbox, project contract, executable digest, and model before admitting a paid
resume. Missing evidence blocks the resume.

## Actor Topology

```text
owner-selected model policy
             |
             v
    persistent logical Root
    Claude session or Codex session
             |
             | optional bounded Opus-Astra route council
             | (blind slates -> one joint revision -> read-only audit)
             |
             | one host-admitted cohort
             v
      exactly three isolated Generator lanes
             |
             v
      Root synthesis and full-proof self-audit
             |
             v
      Verifier pass 1, cold and independent
             |
             | only if pass 1 is correct
             v
      Verifier pass 2, cold and adversarial
             |
             v
        publication receipt
```

The Root is not a fourth Generator lane. The Verifiers do not continue the
proof search.

## Canonical Model Policy

Before the first paid turn, the host writes exactly one canonical JSON file
with this logical shape:

The structural schema is
[`model_policy.schema.json`](model_policy.schema.json). The runtime capability
registry additionally enforces model availability, effort support, provider
requirements, safe problem-id components, and diversity constraints that JSON
Schema alone cannot establish.

```json
{
  "schema_version": "rethlas_model_policy_v1",
  "problem_id": "category/problem",
  "statement_sha256": "<64 lowercase hex>",
  "revision": 1,
  "parent_policy_sha256": null,
  "profile": "balanced",
  "root": {
    "adapter": "claude_cli",
    "provider": "vertex",
    "model": "claude-opus-5",
    "effort": "max",
    "memory_mode": "persistent_logical_root"
  },
  "generator": {
    "adapter": "codex_cli",
    "provider": "openai",
    "model": "gpt-6-astra",
    "effort": "max",
    "lane_policy": "uniform",
    "max_live_paid_lanes": 3,
    "session_mode": "isolated"
  },
  "verifier": {
    "quorum": 2,
    "passes": [
      {
        "pass_index": 1,
        "role": "primary",
        "adapter": "codex_cli",
        "provider": "openai",
        "model": "gpt-6-astra",
        "launch_model": "gpt-6-astra",
        "effort": "xhigh",
        "session_mode": "cold"
      },
      {
        "pass_index": 2,
        "role": "adversarial_full_claim_audit",
        "adapter": "codex_cli",
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "launch_model": "gpt-5.6-terra",
        "effort": "max",
        "session_mode": "cold"
      }
    ],
    "require_distinct_models": true,
    "require_distinct_providers": false,
    "require_adversarial_distinct_from_root": true,
    "automatic_tiebreaker": false
  },
  "fallback_policy": "forbid",
  "selection_authority": "owner_wrapper"
}
```

The model policy SHA-256 is the digest of canonical UTF-8 JSON plus one final
newline. The digest is stored outside the JSON body to avoid a self-referential
identity.

Free-form model names are not accepted directly from a model. Every
`adapter/provider/model/effort` tuple must resolve through the host capability
registry. The resolver records the actual Claude provider selected by auth and
settings projection before writing the policy. A later provider mismatch is a
binding failure.

## Capability Registry

The capability registry separates a model name from an executable transport.

```text
adapter
  canonical model id
  launch model id or alias
  supported efforts
  provider binding requirements
  context window
  executable digest
  authentication preflight
  session modes
  publication authority allowed or denied
```

Initial implementation capabilities:

| Role | Initial adapters | Later adapters |
|---|---|---|
| Root | `claude_cli`, existing Legacy Codex compatibility path | new persistent `codex_cli` Root adapter |
| Generator | `codex_cli` | optional provider-neutral lane adapters |
| Verifier | `codex_cli`, cold `claude_cli` | optional future adapters |

A profile that requires an unavailable adapter is not shown in the interactive
menu and fails closed in noninteractive use. The runtime does not substitute a
different model.

Aliases are resolved to one canonical model before admission. A response whose
host-observed model is outside the selected capability entry is a fallback
integrity failure, even if the provider considers it a compatible alias.

Capability preflight is read-only and must start zero paid turns. It checks the
selected executable, executable digest, login state, configured provider,
model allowlist, effort support, and required local service health. A preflight
cannot prove that a remote provider will accept a model request. A provider
rejection remains an operational failure, not permission to retry with another
model.

## Model Profiles

Profiles are convenience templates. The resolved canonical policy, not the
profile name, is authoritative.

| Profile | Root | Generator | Verifiers | Purpose |
|---|---|---|---|---|
| `compatible` | current selected Root | current Astra configuration | current two-pass configuration | zero behavioral migration |
| `balanced` | owner-selected persistent Root | one strong uniform model | two different Codex models | first production selectable policy |
| `max_diversity` | owner-selected persistent Root | one strong uniform model | different model families and preferably different providers | reduce correlated blind spots |
| `economy` | lower-cost eligible Root | lower-cost uniform Generator | retain two strong cold Verifiers | reduce search cost without weakening publication quorum |
| `custom` | exact owner choices | exact owner choice | exact owner choices | controlled experiments |

Concrete model availability changes over time. Profiles resolve only against
the current capability registry and produce a complete immutable policy.

## Selection UX

Interactive startup order:

1. Select `core` or `reviewed` mode.
2. Select Root adapter and model.
3. Select `compatible`, `balanced`, `max_diversity`, `economy`, or `custom`.
4. For `custom`, select Generator, Verifier 1, and Verifier 2 models and efforts.
5. Show the complete resolved policy and require one owner confirmation before
   the first paid turn.

Noninteractive startup accepts either:

- one named profile plus explicit Root selection, or
- an absolute policy path and exact SHA-256.

The runtime must not propagate a collection of independent model environment
variables across launcher, worker, Guardian, and verifier hops. It propagates
only the validated policy path and digest. Legacy aliases such as `MODEL` and
`REASONING_EFFORT` are translated once by the trusted wrapper when the
`compatible` profile is selected.

`--print-cmd` and equivalent dry-run modes show the resolved command and policy
without creating a manifest, session, root epoch, or paid request.

Before a separately reviewed migration, reviewed mode exposes only its current
`compatible` configuration. New selectable profiles are introduced in core
mode first and do not silently change reviewed state or receipts.

## Cost Policy Separation

Model policy selects the authorized actors. The existing AxiomRelay cost policy
continues to control budgets and owner gates. These are distinct contracts.

- Model policy never hard-codes a dollar price.
- Actor receipts record actual model identity and provider-reported token usage
  when available.
- Price-table changes do not rewrite a mathematical run policy.
- A cost gate cannot silently replace a selected model with a cheaper model.
- The `economy` profile is an explicit owner selection, not an automatic
  fallback.

## Persistent Root Semantics

### Logical Root identity

One logical Root is identified by:

```text
problem_id
statement_sha256
root_epoch
root_session_id
model_policy_sha256
adapter
canonical model
provider binding
CLI executable digest
```

Every Root tool call checks this fence. An old process may return bytes for
audit, but it cannot change memory, admit a cohort, edit a blueprint, or invoke
publication after takeover.

### Two memory layers

The Root uses both:

1. Native session memory for recent conversational continuity.
2. AxiomRelay durable memory for canonical mathematical state.

Durable memory contains route ancestry, quantifier schedule, mutable witness
components, failed paths, proof steps, critical decisions, candidate state, and
verifier defects. Native transcript text is not canonical mathematical memory.

Each physical Root turn performs at most one bounded memory hydration when
durable memory exists. It does not replay the complete ledger or transcript.

### Claude Root

- Start one headless turn with a fresh Claude session id.
- Resume later turns with that exact session id.
- Exit the physical process after every turn.
- Bind provider, provider model mapping, context window, CLI version, and CLI
  executable digest.
- Require explicit takeover after binding drift.
- In `opus_sol_council_v2`, keep Opus as the sole root while an isolated
  GPT-6 Astra/max seat supplies an independent blind slate with fixed direct,
  orthogonal, and adversarial route roles, one
  keep/revise/replace review, and one non-editing final audit. Opus merges and
  adjudicates once; the accepted exact routes and council receipt gate fanout.
  A blocked audit override is structured and fail-closed: Opus either rejects
  every fatal finding without changing plan bytes or seals a corrected plan
  whose changed route ids exactly match findings marked corrected. Override
  prose never substitutes for executable plan bytes.
  Fixed Astra plan ids, explicit separation claims, and normalized host-side
  mechanism/scope checks reject cosmetic diversity.
  Astra review and finding wire objects are keyed by the exact phase-bound route
  ids and normalized into slate-ordered durable arrays. Canonical-memory
  publication is host-fenced until acceptance, the first batch must equal the
  complete three-route final plan, and cohort admission additionally requires
  its revalidated committed checkpoint digest. Root authority, output schema,
  phase intent/worker/execution/settlement/receipt, acceptance, and pointer bind
  one host-source digest. A detached single-dispatch worker seals the raw
  execution before the host derives settlement and receipt; a stopped worker
  without a result becomes a durable `execution_unknown` terminal artifact, so
  crash or concurrent reconciliation cannot repay the Astra call. Phase-finalize
  locks fence rejected reports, settlements, and receipts against an immutable
  source-drift artifact snapshot.
  Failed takeovers follow the bounded predecessor chain to the nearest council,
  inherit its explicit lineage, and increment the round; council lineage also
  cannot downgrade to single-root mode.
  If the statement explicitly permits retrieval, the Astra seat receives only
  Matlas search, metadata-gated arXiv search, and the official arXiv reader,
  with the statement cutoff enforced at both arXiv gates under durable budgets.
  Retrieval-ledger timestamps remain monotone across wall-clock rollback.
  General web, shell, workspace,
  memory, and fanout access remain disabled.

### Codex Root

The persistent Codex Root is a new adapter parallel to Claude Core. It does not
replace or modify the isolated Legacy runner.

Initial turn:

- run non-ephemeral `codex exec --json`;
- capture and durably bind the exact `thread.started` session id;
- require the exact Root model policy and role-gated MCP surface;
- persist a turn intent before launching the process;
- settle the intent with the exact terminal event and output digest.

Later turn:

- use `codex exec resume <session_id>`;
- verify the returned thread id, model, executable digest, root epoch, working
  directory, sandbox, and MCP bindings;
- reject `--last` because it is not an exact identity;
- reject a resume whose working directory or sandbox cannot be attested;
- persist and consume exactly one turn intent.

The persistent Codex Root never uses `--ephemeral`. Codex Generator and
Verifier processes remain ephemeral.

### Context rollover and model switching

Native context is finite and may compact. Durable AxiomRelay memory remains the
authority after compaction.

Changing Root model, adapter, provider binding, or CLI executable creates a new
root epoch:

```text
old Root checkpoint
  -> durable handoff receipt
  -> fence old Root
  -> create successor policy revision
  -> start fresh native session
  -> one bounded durable-memory hydration
```

The new Root does not claim access to the old provider's hidden state.

## Generator Policy

The default Generator policy is uniform across exactly three lanes.

Reasons:

- Route quality remains distinguishable from model quality.
- A promising route is not accidentally assigned to a weaker model.
- Cost and capability are predictable.
- Cohort comparisons remain interpretable.

Per-lane models are deferred to a later policy schema. They require an exact
three-entry lane map and separate evaluation because they confound route and
model effects.

The Generator policy digest is part of the cohort identity. The same three
plans under a different Generator model are a different paid effect and require
new owner admission. A model change cannot masquerade as replay of an existing
cohort.

Generator lanes receive the problem, exact route card, bounded references, and
role contract. They do not receive the Root transcript or another lane's native
session.

## Verifier Diversity Policy

Both passes verify the exact same statement bytes, proof bytes, proof digest,
canonical target digest, item manifest, and reconstructed contexts.

Pass 1 is the primary whole-proof verifier. Pass 2 is an adversarial
full-claim audit and receives no Pass 1 verdict or findings.

Publication requirements:

1. Both passes return `correct`.
2. Both use distinct verification attempt ids.
3. Both use distinct native run ids.
4. Each actual adapter, provider, model, and effort equals the policy.
5. `require_distinct_models=true` implies unequal canonical model ids.
6. `require_distinct_providers=true` implies unequal attested providers.
7. `require_adversarial_distinct_from_root=true` prevents the Root model from
   serving as Pass 2.
8. Both passes are cold and have no shared conversation/session id.
9. The proof remains byte-identical until publication CAS.

Token-saving order:

- If Pass 1 returns `wrong`, do not run Pass 2.
- If Pass 1 is operationally unknown, block and do not retry automatically.
- Run Pass 2 only after Pass 1 is correct.
- If Pass 2 returns `wrong`, repair the proof rather than buy a third vote.
- A third verifier requires an explicit future owner policy, not an automatic
  tiebreaker.

A different model name is useful but not a proof of independent reasoning.
Different providers or model families are preferred when a trusted cold
adapter is available.

## Durable State and Schema Versions

New selectable-model execution uses new schema versions rather than changing
the meaning of existing receipts.

| Artifact | New version | Required new binding |
|---|---|---|
| model policy | `rethlas_model_policy_v1` | complete canonical policy and digest |
| Root manifest | successor version | model-policy digest and native session identity |
| Root turn intent/receipt | new | root epoch, session id, policy digest, input/output identity |
| cohort intent/receipt | successor version | Generator policy digest and actual model |
| verifier attempt receipt | successor version | expected and actual adapter/provider/model/effort |
| publication receipt | `rethlas-publication-v4` | model-policy digest and two pass provenance records |

Readers continue to accept historical publication v2 and v3. Writers never
rewrite old receipts. Legacy and reviewed runners retain their existing schemas
until an explicit migration is separately approved.

The v4 publication receipt records host-observed provenance. It is not described
as a provider-signed attestation unless the provider supplies such evidence.

## Policy Mutability

The initial policy is immutable after the first paid turn.

Allowed owner transitions:

- Root takeover may change only the Root section and creates a successor policy
  revision plus a fresh root epoch. The successor must still satisfy all
  Root-to-Verifier diversity constraints.
- A Generator model change is allowed only before a new cohort and creates a
  successor policy revision with explicit owner admission.
- Verifier models are locked before candidate generation. They cannot be
  changed after seeing a candidate or a verifier verdict within the same run.
- Changing the Verifier pair after proof work requires a new run id that names
  the old run as its parent.

Terminal publication, retraction, cancellation, or unsolved states admit no
policy mutation and no paid successor.

## Failure Semantics

| Failure | State | Automatic paid retry |
|---|---|---:|
| invalid policy or digest | blocked before launch | no |
| adapter unavailable | blocked before launch | no |
| login/provider mismatch | blocked before launch | no |
| selected model rejected remotely | operationally blocked | no |
| Root turn outcome unknown | execution unknown | no |
| Generator effect unknown | execution unknown | no |
| Verifier effect unknown | verification unknown | no |
| Verifier returns wrong | candidate rejected | no automatic third vote |
| model fallback observed | integrity failure | no |
| stale Root or lane result | audit only | no |

## No-Waste Invariants

1. Invalid or unavailable model policy starts zero paid actors.
2. One durable intent admits at most one paid actor request.
3. No adapter performs silent fallback.
4. A physical Root process exits after one turn.
5. No Root turn is purchased only to wait for a Generator or Verifier.
6. Exactly three Generator lanes are admitted per cohort.
7. Closed lanes are not restarted to regenerate summaries.
8. Pass 2 starts only after Pass 1 is correct.
9. A disagreement does not start an automatic third Verifier.
10. A Root takeover hydrates bounded durable memory instead of replaying the
    complete transcript.
11. Model-policy changes produce new effect identities and never reuse an old
    receipt.
12. Terminal states have zero paid successors.

## Compatibility Rules

- Unset model-policy configuration preserves the current `compatible` path.
- `MODEL` and `REASONING_EFFORT` remain accepted only as Legacy compatibility
  aliases.
- `run_legacy.sh` stays physically isolated and cadence-disabled.
- `run_hotjoin.sh` stays reviewed-mode only.
- Claude Core keeps its existing root session and root-epoch contracts.
- Persistent Codex Root is a new sibling runner, not a rewrite of Legacy.
- Existing MCP tool names remain stable in the first implementation.
- Existing publication v2 and v3 receipts remain readable and terminal.
- Cross-provider verification is selectable only after its adapter passes the
  same cold-start, digest, output-contract, timeout, and crash-cut tests as the
  Codex verifier.

## Implementation Sequence

### Milestone 1: policy resolver

- Add the capability registry and strict policy validator.
- Add named profiles and custom policy-file input.
- Preserve no-policy behavior exactly.
- Write policy path and SHA before paid work.
- Add dry-run output with zero writes.

### Milestone 2: Generator and Codex Verifier selection

- Split Legacy Root model from default Generator model.
- Bind Generator model and effort into cohort identity and receipts.
- Select Codex Verifier model and effort per pass.
- Require actual verifier provenance to equal policy.
- Add v4 publication without changing v2 or v3 readers.

### Milestone 3: persistent Codex Root

- Add a new role-gated Codex Root runner.
- Persist exact thread id and per-turn intents/receipts.
- Resume only exact sessions, never `--last`.
- Verify resume working directory, sandbox, model, MCP, and executable.
- Add takeover and cross-model durable handoff.

### Milestone 4: cold Claude Verifier, implemented with live gate pending

- The Claude verifier adapter uses the same output schema.
- It uses a new nonpersistent, tool-less session for every pass.
- It binds executable SHA, auth provider, selected model, provider model usage,
  and effort, and rejects fallback.
- Fake-provider cold-start, routing, output, auth mismatch, fallback, and
  profile-launch tests pass.
- Exact Vertex Opus provider mapping and a paid cold-start canary are validated.

### Milestone 5: production canary

- Exercise one persistent Root, one exact-three cohort, and two different
  Verifier models.
- Confirm no additional paid actors after publication.
- Reconcile actual request ids, token usage, model ids, and receipts.

## Acceptance Matrix

### Deterministic tests

- every supported profile normalizes to one canonical policy;
- unknown keys, models, efforts, adapters, and fallback modes fail closed;
- policy digest changes when any paid-role field changes;
- no-policy launch reproduces current commands and receipts;
- v2 and v3 publication readers remain unchanged;
- v4 requires exact policy and pass provenance;
- model fallback, duplicate run ids, and duplicate attempt ids are rejected;
- Pass 1 wrong starts zero Pass 2 calls;
- invalid policy starts zero Root, Generator, and Verifier processes.

### Crash cuts

- policy intent before and after durable write;
- Root submit before native session id is recorded;
- native session accepted before turn receipt is written;
- cohort submit before Generator process acknowledgement;
- Astra phase worker launch, marker, raw execution, settlement, and receipt;
- cohort worker marker before its durable receipt;
- Pass 1 correct before Pass 2 intent;
- Pass 2 accepted before publication receipt;
- publication receipt before owner completion CAS.

Unknown outcomes never authorize automatic resubmission. Write-once artifacts
use complete temporary-file writes plus atomic no-replace publication. Stopped
source-bound v3 cohort workers settle `completed_unverified` only from the exact
full unsolved-round frontier delta; every other recoverable terminal outcome can
advance only through explicit owner-authorized recovery, with unchanged or
exact-checkpoint frontier evidence. A quiescent source-drift
snapshot removes only exact same-inode publication aliases or exact unpublished
single-link temporaries left by those write-once crash windows; every other
hardlink remains fail-closed.
If the old executor wins the launch linearization and later settles
`completed_unverified`, an owner-confirmed source migration records that exact
nonpublished settlement only after reconstructing its full terminal frontier
evidence; receipt v3 pins the exact content-addressed reports, synthesis, and
admission-time log cap, so later legal memory appends or cap changes do not
erase that history. It does not authorize
recovery or replay. Strictly validated pre-v3 intents may be source-migration
sealed only when an ordinary terminal receipt already exists; the termination
manifest binds every surviving worker/executor artifact. Recovery authorization
is restricted to the live terminal lineage, writes at most eight edges, and
then exposes successor-council or fresh-root exits without a ninth artifact.
Root takeover likewise validates its whole predecessor chain and rejects a
129th edge before committing the next manifest or authority.

### Persistent Root tests

- Claude resumes the exact session and root epoch;
- Codex resumes the exact session id and rejects `--last`;
- CLI digest drift requires explicit takeover;
- model change creates a fresh epoch and native session;
- durable memory survives provider-session loss;
- old Root tool calls are fenced after takeover;
- context compaction does not erase canonical route decisions;
- no physical Root process remains idle after a turn.

### Verifier diversity tests

- two different Codex models publish only after both are correct;
- same-model results are rejected when distinct models are required;
- cross-provider profile is unavailable without both adapters;
- Pass 2 cannot read Pass 1 output;
- provider/model/effort mismatch blocks publication;
- no automatic third Verifier appears after disagreement;
- terminal wrapper restart starts zero model calls.

## Release Gate

Selectable model policy is not production-ready until Milestones 1 and 2 pass
the full repository suite and a no-cost transport canary. Persistent Codex Root
requires Milestone 3. Cross-provider Verifier diversity requires Milestone 4.

Until those gates pass, the current compatible runtime remains authoritative.

## Current Smoke Status

The pure resolver is implemented in [`model_policy.py`](model_policy.py). It
normalizes profiles, validates the capability registry and diversity rules,
computes canonical policy digests, and validates digest-bound custom files. It
does not write runtime state or launch paid actors.

Observed zero-model smoke results:

| Profile | Result | Generator | Verifiers |
|---|---|---|---|
| `compatible` | resolved | Astra | Astra, Astra |
| `balanced` | resolved | Astra | Astra, Terra |
| `economy` | resolved | Terra | Astra, Terra |
| `max_diversity` | resolved; Astra runtime canary pending | GPT-6 Astra `max` | GPT-6 Astra `max`, cold Claude Opus 5 1M `max` |

The cold Claude adapter passes fake-provider process, auth, provider, tool
isolation, nonpersistent session, model fallback, raw-JSON/local-schema, and
Pass 2 routing tests. All resolver smoke paths report zero writes and zero paid
actors.
The historical paid verifier canary used one exact proof and two independent runs. Sol
`xhigh` returned correct first, then Vertex Opus 5 1M `max` returned correct as
the adversarial pass. Publication occurred only after quorum two; verified and
draft bytes matched. Host telemetry recorded 12,842 Sol tokens and 3,182 Opus
tokens. Current v3 publication receipts record the two actual canonical models
and efforts; adapter/provider/launch provenance is retained in the verifier
service audit. The planned v4 model-policy digest and provider binding remain a
separate Milestone 2 release gate.
The canary above is historical and used Sol. The current `max_diversity`
policy selects `gpt-6-astra` at `max` for generation, OpenAI council seats,
and Pass 1; Opus Pass 2 remains at `max`. All other former Sol roles also use
Astra, retaining their existing effort. Terra and Opus roles are unchanged.
The canonical entrypoints are `gpt-astra` and `opus-astra-council`; `gpt-sol`
and `opus-sol-council` remain compatibility aliases. Historical model names
and record bytes remain unchanged and are authenticated only for retirement;
fresh dispatch remains restricted to the current model policy. A new Astra
paid canary has not been run.
When the Root is also Opus 5, `require_adversarial_distinct_from_root` is false
by explicit owner policy; Verifier Pass 1 and Pass 2 remain distinct across Astra
and Opus as well as OpenAI and Vertex.
