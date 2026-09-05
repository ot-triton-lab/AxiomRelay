# AxiomRelay

English | [简体中文](README.zh-CN.md)

**A proof hill-climbing system for hard mathematical problems.**

Keep the audited frontier. Repair one gap at a time.

AxiomRelay separates route design, proof search, synthesis, verification, and
publication. Model output is always a draft; only the host can publish a proof,
and only after two independent whole-proof verifier passes agree on the exact
same bytes.

> **Status:** research infrastructure. The runtime provides strong process,
> provenance, and replay guarantees, but model verification remains
> probabilistic and is not a substitute for expert review.

This README describes the `axiomgraph-v1` branch, which adds versioned
AxiomGraph publication events, migrates OpenAI roles from Sol to GPT-6 Astra,
and lets Claude roots run bounded math experiments.

## Why AxiomRelay exists

Hard proof attempts fail in ways that ordinary chat loops handle poorly:
plausible routes are correlated, long contexts drift, external suggestions are
mistaken for authority, retries duplicate expensive work, and a nearly correct
draft is easily confused with a verified theorem.

AxiomRelay addresses those failure modes with a small set of invariants:

- route design and proof execution are separate roles;
- at most three isolated proof lanes run from one host-validated plan set;
- an optional Opus + Astra council performs independent route design, one joint
  revision, and one read-only risk audit;
- complete user-supplied answers enter as SHA-bound **unverified reference
  candidates**, while late GPT Pro help enters as a narrowly bound
  **unverified gap delta**;
- durable intents and receipts make interrupted work resumable without
  silently spending the same model call twice;
- publication requires two cold, content-addressed verifier passes.

## Main contribution: proof hill climbing over an audited frontier

AxiomRelay is designed for the important middle regime where a problem is too
hard for a reliable one-shot answer, but its remaining obstacles can still be
isolated, attacked, and checked. Instead of repeatedly asking a model to
rewrite the whole proof, the runtime retains the compatible audited frontier,
compresses failure into the smallest load-bearing gap, and spends additional
model or human-advisor effort only on that gap.

```text
diverse proof search
        |
        v
retain compatible audited work
        |
        v
isolate one load-bearing gap after independent failures
        |
        v
owner-relayed GPT Pro question (optional, targeted, SHA-bound)
        |
        v
untrusted gap delta -> local audit -> repair only the dependency cone
        |                                      |
        +---------- unresolved new gap <-------+
                               |
                               v
                     resume diverse fanout
```

GPT Pro is therefore used as a low-frequency **gap oracle**, not as proof
authority and not merely as another whole-proof generator. The root manages
state and chooses the next bottleneck; isolated proof lanes supply search
diversity using the selected model profile; Pro may propose a decisive local
argument or counterexample; and
the verifier still checks the final proof in full. A failed Pro answer does not
erase earlier work, while an accepted local step advances the frontier for the
next round. Each follow-up question is newly bound to the updated statement,
cited records, failures, and ledger head, so the process hill-climbs over
explicit evidence rather than an ever-growing informal chat transcript.

This is most useful for decomposable medium-to-hard problems whose one-shot
success probability is low but whose lemmas, estimates, singular regimes, or
counterexamples remain locally auditable. Easy problems do not need the
overhead; genuinely open problems, or gaps that cannot themselves be checked,
may not yield useful progress from this loop.

## System flow

```text
problem statement --------------------+
                                      |
optional GPT Pro / human answer ------+ ingest as unverified, SHA-bound input
                                      v
                         route-design root
              GPT Astra | Opus | Fable | Opus + Astra council
                                      |
                          one accepted three-route slate
                                      v
                       three isolated proof lanes
                         model selected by profile
                            |         |         |
                            +---------+---------+
                                      v
                           root synthesis + self-audit
                                      v
                           blueprint.md (draft)
                                      |
                         cold verifier pass 1: correct?
                                      | yes
                         cold verifier pass 2: correct?
                                      | yes
                                      v
                       atomic publication + immutable receipt
```

The host controls admission, fencing, recovery, and publication. Roots design
and synthesize. Proof lanes cannot spawn descendants. Verifiers cannot edit the
proof or publish it.

### AxiomGraph source interface v1 (experimental)

The Claude Core host automatically attempts a local export after reconciling
an active `rethlas-publication-v6` publication. Publication lookup also retries
the export. This runs after the existing two-pass publication checks and uses
only the Python standard library; no AxiomGraph installation is required.

The [interface manifest](agents/generation/mcp/axiomgraph_source_interface_v1.json)
defines the consumer contract:

| Field | Value |
|---|---|
| Interface version | `1.0` (minimum consumer minor: `0`) |
| Required capability | `verified_publication_event_v1` |
| Event schema | `axiomrelay_verified_publication_event_v1` |
| Graph contract | `axiomgraph_contract_v1`, with the exact schema-bundle digest pinned in the manifest |

Each event contains the exact target and blueprint bytes encoded as Base64,
their SHA-256 digests, the publication receipt, the normalized ProofItem
dependency graph, the stable verifier profile, and the loaded Core/export
runtime digests. Events are canonical UTF-8 JSON with one final newline:

```text
agents/.claude_core/axiomgraph_exports/v1/
  publications/<publication_receipt_sha256>/<event_id>/event.json
  failures/<receipt_or_statement_sha256>/<failure_sha256>.json
```

Event ids have the form `arev_<64 lowercase hex characters>` and bind the
complete event payload. Repeating an export with identical inputs reuses the
same immutable file. Changed runtime bindings produce a distinct event, so one
publication receipt can retain multiple events. Export is best effort: failure
leaves publication status, proof bytes, receipts, and API output intact. The
host attempts to save a bounded local diagnostic in `failures/`.

A separately versioned AxiomGraph consumer validates and projects these source
events. Before projection it must check the interface version, required
capabilities, exact Graph schema digest, and expected runtime-source bindings.
AxiomRelay writes the source files locally; this repository does not include a
Graph consumer or automatic delivery service. Compatible Relay refactors retain
the v1 semantics; a breaking semantic change requires a new interface major
and event schema. The [protocol fixture](agents/generation/tests/fixtures/axiomgraph_source_interface_v1.json)
provides a complete event and its expected digests for compatibility checks.

The v1 wire bounds JSON nesting at 256 and the exact target at 4 MiB. The
NFC/LF-normalized JSON object containing `problem_id` and `proof_context` is
limited to 4 MiB minus 4096 bytes for fixed projection metadata. Stable-profile
keys must remain distinct after normalization, and each proof item id binds
the first 24 hexadecimal characters of its artifact SHA-256. A stored event
is limited to 192,000,000 bytes. Consumers must enforce the same bounds before
an event can become a projection.

This is not yet the stopped-unsolved takeover trigger. Automatic transfer to a
Danus controller remains disabled until AxiomRelay can authenticate one exact
terminal cohort, source state, absence of owner/Pro waits, and the lease/fence
CAS. A bounded `stop_unsolved` result is evidence that the fast path stopped;
it must never be relabeled as proof that the theorem is mathematically
impossible.

## What counts as success

A run succeeds only when all of the following are true:

1. `blueprint.md` parses into a complete proof-item manifest.
2. Verifier Pass 1 returns `correct` for every required item.
3. Verifier Pass 2 independently returns `correct` on the same immutable proof.
4. Statement, proof, context, model, effort, pass identity, and service digests
   match their receipts.
5. The host atomically writes `blueprint_verified.md` and its publication
   receipt.

A route report, synthesis, draft, single verifier pass, or unreceipted
`blueprint_verified.md` is not proof success.

## Execution choices

### Modes

| Mode | Use | Root choices | Cost profile |
|---|---|---|---|
| `core` | Default isolated runtime | GPT Astra, Opus, Fable, Opus + Astra council | Lower overhead |
| `reviewed` | Long-running compatibility workflow with scheduled reviews | GPT Astra | Higher overhead |

Noninteractive runs default to `core`. Interactive runs explain the choices.
The reviewed mode requires an explicit run ID and the `compatible` profile.

### Roots

| Root | Role |
|---|---|
| `gpt-astra` | GPT Astra designs routes and orchestrates proof work. Default. |
| `opus` | Persistent logical Claude Opus 5 root; each launch performs one resumable turn. |
| `fable` | Persistent Claude Fable 5 root with the same host controls. |
| `opus-astra-council` | Opus and an isolated Astra/max seat design independently, revise once together, then run a final non-editing audit. Requires `max_diversity`. |

Claude roots are currently available in `core` mode only. They receive a
read-only workspace view plus a narrow host interface; they do not receive
general shell, write, browser, or subagent authority.

Before freezing a route slate, a Claude root may run a concrete symbolic,
numeric, or finite diagnostic through `run_math_experiment`. This is not a
general shell: the host executes inline Python in an empty Codex sandbox with
the repository and user home denied, network disabled, and a 60-second limit.
Each root session has at most 12 write-once experiments, with 32 KiB of code,
64 KiB of stdout, and 16 KiB of stderr per experiment. NumPy, SciPy, SymPy,
mpmath, and gmpy2 come from the attested generation environment. The private
receipt is `unverified_computational_diagnostic`; it can discriminate routes
but cannot prove a claim or authorize a cohort. The isolated Astra council seat
still has no shell or Python, while admitted GPT-Astra proof lanes retain their
existing restricted math runtime.

### Model-policy profiles

| Profile | Proof lanes | Verifier 1 | Verifier 2 |
|---|---|---|---|
| `compatible` | Astra `max` | Astra `xhigh` | Astra `xhigh` |
| `balanced` | Astra `max` | Astra `xhigh` | Terra `max` |
| `economy` | Terra `max` | Astra `xhigh` | Terra `max` |
| `max_diversity` | GPT-6 Astra `max` | GPT-6 Astra `max` | Opus 5 1M `max` |

`max_diversity` requires authenticated OpenAI and Claude providers. Pass 2 is
cold: it receives the authenticated proof context but not Pass 1's verdict,
findings, or session state.

All roles previously dispatched to Sol now use `gpt-6-astra`, preserving their
`max` or `xhigh` effort. Terra and Opus roles retain the profile choices above.
Use `gpt-astra` and `opus-astra-council`; the legacy `gpt-sol` and
`opus-sol-council` command selectors remain accepted aliases. Explicit Sol
model overrides are rejected before a new dispatch.

Selecting `opus-astra-council` chooses `max_diversity` automatically when no
profile is specified; explicitly choosing a different profile is rejected.
Start the verifier and generation runner with the same
`AXIOM_RELAY_MODEL_POLICY_PROFILE`.

Existing sessions keep their original source/model bindings and require the
normal source-drift takeover procedure after an upgrade. Historical Sol
intents and receipts are authenticated against their original dispatch chain
and retained byte-for-byte; they do not authorize a new Sol call. Stable
transport identifiers such as `opus_sol_council_v2` are unchanged.

Restart the verifier on the new release before starting generation. Every
launch mode checks its advertised profile and rejects a ready service that
still selects Sol; health alone does not establish model compatibility.

## Quick start

Supported hosts are Linux and macOS. Use Python 3.11, 3.12, or 3.13 and Bash
5 or newer. macOS still ships an older system Bash, so install the current
version and put it first on `PATH`:

```bash
brew install bash uv
export PATH="$(brew --prefix)/bin:$PATH"
```

Linux external proof lanes use an unprivileged mount/PID namespace. On macOS,
the same lanes use Codex's native Seatbelt permission profiles. Both paths run
a zero-model isolation probe before any paid proof call.

### 1. Clone and install the CLIs

```bash
git clone https://github.com/ot-triton-lab/AxiomRelay.git
cd AxiomRelay
npm install -g @openai/codex
```

Authenticate Codex before running a problem. Install and authenticate Claude
Code as well when using a Claude root or `max_diversity` verification.
Claude authentication is provider-neutral: `auto` follows the active CLI
provider. Set `AXIOM_RELAY_CLAUDE_AUTH_MODE=subscription` when a root must use
Claude subscription OAuth even if the machine also has Vertex, Bedrock,
Foundry, or API-key configuration. The verifier has the parallel
`VERIFY_CLAUDE_AUTH_MODE` setting. Explicit modes bind both the provider and
Claude CLI's reported authentication method; `subscription` also requires a
reported subscription type. A mismatch stops the run before a model call.
The trust check accepts the official native Claude Code layout in which the
versioned executable and `ClaudeCode.app` executable are two hard links to the
same inode. This is an exact-layout exception; unrelated multi-link binaries
remain rejected.

### 2. Create the Python environments

Verifier:

```bash
python3 -m venv agents/verification/.venv
agents/verification/.venv/bin/pip install \
  -r agents/verification/requirements.txt
```

Generation:

```bash
python3 -m venv --copies --without-pip agents/.generation-venv
uv pip install --python agents/.generation-venv/bin/python \
  -r agents/generation/requirements-dev.txt
```

The copied generation interpreter is intentional. The runner attests the
interpreter and trusted source closure, and rejects executable `.pth` hooks
before paid work begins.

### 3. Start the verifier

```bash
cd agents/verification
./scripts/run_verifier.sh
```

For cross-provider verification:

```bash
AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity \
./scripts/run_verifier.sh
```

Inspect the resolved command without starting the service:

```bash
AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity \
AXIOM_RELAY_VERIFIER_PRINT_COMMAND=1 \
./scripts/run_verifier.sh
```

The default endpoint is loopback HTTP on port `8091`. Check real readiness—not
just process liveness—before starting a proof run:

```bash
curl -fsS http://127.0.0.1:8091/ready
```

`/ready` makes no model call. It checks the installed CLIs and authentication,
the MCP/runtime imports, writable durable storage, the platform primitives,
and an actual Codex sandbox probe.

Remote deployment must terminate HTTPS before this service and use a random
token containing at least 256 bits. The application does not terminate TLS:

```bash
export VERIFY_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export VERIFY_TLS_TERMINATED=1
```

### 4. Run a problem

Open a second terminal at the repository root, keeping the verifier running.

Create a local UTF-8 Markdown statement below `agents/generation/data/`. Problem
and answer files in that directory are intentionally ignored by Git. A nested
input such as `data/algebra/problem.md` produces output below
`results/algebra/problem/`.

Default GPT Astra root:

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=gpt-astra \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

Opus + Astra council with maximum diversity:

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=opus-astra-council \
AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

Run `./tests/run_example.sh` in a terminal without settings to enter the
problem path and use the interactive execution selector. In noninteractive
use, `PROBLEM_FILE` is required; there is no bundled example problem fallback.
When no interpreter override is supplied, GPT-Astra and reviewed mode use the
documented `agents/.generation-venv/bin/python` environment automatically.

## Asking GPT Pro about one blocked proof gap

This is the hill-climbing intervention described above. The preferred human
relay is gap-sized. After two independent attempts fail on
the same load-bearing claim, the canonical Claude root emits a write-once
`gap_id`, an exact copy/paste prompt, and a host-computed digest binding it to
the current statement, cited active memory records, and ledger head. It does
not wait for all three routes to fail merely to ask a precise question.
Preparing the packet never opens ChatGPT or spends a Pro turn; the owner
remains the only sender. Reviewed/GPT-Astra lanes do not have these four root
tools: they hand the precise gap evidence back to the canonical root.

The copied prompt is self-contained. It assumes that Pro cannot access the
repository, AxiomRelay memory, record ids, hashes, local files, or earlier chat.
All definitions, hypotheses, settled facts, failed mechanisms, and boundary
conditions needed for the gap must therefore be written out as mathematical
content. Provenance ids and digests stay inside the private packet and receipt;
they are never inserted into the external prompt.

`two failed mechanisms → gap query → owner relay → untrusted gap delta → repair-cone audit`

The root normally calls `prepare_pro_gap_query` itself. The equivalent owner
CLI is useful for recovery and testing:

```bash
PROBLEM_PATH=agents/generation/data/my_problem.md
PROBLEM_ID=my_problem
STATEMENT_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$PROBLEM_PATH")"

agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --prepare-pro-gap-query \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent <<'JSON'
{
  "target_claim": "Prove the uniform weighted resolvent estimate.",
  "settled_facts": ["The radial marginal and exact stationary equation are established."],
  "verified_fact_or_proof_ids": ["<active proof_steps record id>"],
  "failed_attempts": [
    "Fiberwise inversion leaves radial derivatives uncontrolled.",
    "Dyadic localization leaves interface fluxes uncontrolled."
  ],
  "failed_path_record_ids": [
    "<first active failed_paths record id>",
    "<second active failed_paths record id>"
  ],
  "boundary_checks": ["Treat I comparable to gamma without assuming a uniform angular gap."],
  "recommended_exact_question": "<a self-contained mathematical question for GPT Pro, including every required definition and hypothesis>"
}
JSON
```

The caller does not supply `source_context_sha256`. The host resolves every
cited record, rejects inactive or wrong-channel records, snapshots the ledger
head, computes the digest, and expands the question into a prompt containing
the settled facts, both failed paths, and all boundary checks. Internal ids and
hashes remain in the stored packet. Copy only the returned
`copy_paste_prompt`; do not append receipt metadata or assume that Pro can
recover omitted context.

Historical v2 query packets remain readable for audit and for binding an answer
that was already sent. Their old external prompt is not returned for a new
relay: `external_relay_status=legacy_prompt_requires_new_gap_id`. Prepare a v3
query under a new `gap_id` to obtain a self-contained prompt.

Every read is compare-and-swap checked. To recover a query, provide the digest
from its creation receipt:

```bash
agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --get-pro-gap-query \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent \
  "$QUERY_SHA256"
```

When the answer comes back, paste it into the live owner turn or bind a saved
answer to the exact query:

```bash
agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --ingest-pro-gap-response \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent \
  "$QUERY_SHA256" < pro-answer.md
```

The ingest receipt returns `RESPONSE_SHA256`. A later exact read requires both
digests:

```bash
agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --get-pro-gap-response \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent \
  "$QUERY_SHA256" "$RESPONSE_SHA256"
```

The answer is stored as `complete_unverified_gap_delta`. It does not alter the
accepted route-council candidate packet, so a late answer does not force a new
council. The root audits the answer against the recorded failed attempts,
patches only the named claim and its dependency descendants, and reverifies
that repair cone. Unaffected compatible verification remains reusable. A Pro
answer never verifies or publishes itself.

Query status is `waiting_owner_pro_response` until the bound response exists,
then reads as `response_available`. Per statement, the host accepts at most 16
queries and 16 responses and enforces aggregate byte caps; a changed question
or evidence set needs a new `gap_id`.

## Supplying a complete external candidate before search

Complete alternate proofs belong **before route acceptance**, not after fanout
and not inside a verifier prompt. (Use the gap-delta channel above for a late,
targeted repair.) Ingest the complete text while it is still an untrusted
candidate:

```bash
PROBLEM_PATH=agents/generation/data/my_problem.md
PROBLEM_ID=my_problem
STATEMENT_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$PROBLEM_PATH")"

agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --ingest-reference-candidate \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gpt_pro < pro-answer.md
```

Then start a new Opus + Astra council for that statement. The candidate moves
through the system as follows:

1. The host stores immutable bytes bound to the problem and statement digest.
2. Opus must bind the candidate marker and exact projection path to one route.
3. Astra's initial blind slate remains independent of the candidate.
4. The joint revision and final audit receive the complete candidate and must
   test it or identify a fatal defect.
5. The bound proof lane receives the exact read-only projection.
6. Verifiers judge only the resulting proof; the candidate has no publication
   authority.

Changing the statement changes its digest and prevents accidental reuse. A
candidate added after a council is accepted requires a fresh council round.

## Outputs and recovery

For `PROBLEM_FILE=data/my_problem.md`:

| Path | Meaning |
|---|---|
| `agents/generation/results/my_problem/blueprint.md` | Current unverified draft |
| `agents/generation/results/my_problem/blueprint_verified.md` | Published proof bytes |
| `agents/.verification_receipts/` | Trusted verification and publication receipts |
| `agents/generation/memory/my_problem/` | Durable canonical research memory |
| `agents/.claude_core/axiomgraph_exports/v1/publications/` | Immutable source events exported after Claude Core publication reconciliation |
| `agents/.claude_core/axiomgraph_exports/v1/failures/` | Best-effort local export diagnostics |

Interrupted verifier work resumes at the first unsettled item. Completed,
compatible items are not replayed. Unknown execution status fails closed.

Resume a persistent Claude root:

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=opus \
AXIOM_RELAY_CLAUDE_SESSION_ID=<lowercase-uuid> \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

Send strategic guidance on a resumed turn, or together with an explicit
takeover, using `AXIOM_RELAY_CLAUDE_OWNER_PROMPT`. It is treated as operator
direction, not as a mathematical premise or publication authority. A wholly
new root without either binding still rejects this setting.

## Configuration

| Setting | Meaning |
|---|---|
| `AXIOM_RELAY_RUN_MODE` | `core`, `reviewed`, or `prompt` |
| `AXIOM_RELAY_MAIN_AGENT` | `gpt-astra`, `opus`, `fable`, `opus-astra-council`, or `prompt` |
| `AXIOM_RELAY_MODEL_POLICY_PROFILE` | `compatible`, `balanced`, `economy`, or `max_diversity` |
| `AXIOM_RELAY_REVIEW_RUN_ID` | Required identity for reviewed mode |
| `AXIOM_RELAY_CLAUDE_SESSION_ID` | Resume an active logical Claude root |
| `AXIOM_RELAY_CLAUDE_TAKEOVER_FROM` | Explicitly fence and replace a recoverable Claude root |
| `AXIOM_RELAY_CLAUDE_OWNER_PROMPT` | Operator message for a resumed Claude turn or explicit takeover |
| `AXIOM_RELAY_CLAUDE_CONTEXT_WINDOW` | Claude context window; Opus defaults to 1M |
| `AXIOM_RELAY_CLAUDE_AUTH_MODE` | `auto`, `subscription`, `api`, `vertex`, `bedrock`, or `foundry` for a Claude root |
| `AXIOM_RELAY_PRINT_COMMAND` | Print a Claude root launch command without executing it |
| `PROBLEM_FILE` | Safe Markdown path below `agents/generation/data/` |
| `CLAUDE_CONFIG_DIR` | Optional Claude configuration directory used by the root launcher |
| `VERIFY_READY_URL`, `VERIFY_PROOF_URL` | Verifier readiness and proof endpoints |
| `VERIFY_CLAUDE_AUTH_MODE` | Provider-neutral Claude authentication selection for the verifier |
| `VERIFY_API_TOKEN` | Bearer token required for non-loopback verification |
| `VERIFY_TLS_TERMINATED` | Must be `1` when a non-loopback verifier is behind trusted TLS termination |

Historical environment names, schema identifiers, and receipts remain readable
for one transition release. New automation should use the settings above.
Historical receipt bytes must never be rewritten merely to update branding.

## Trust boundaries

- Statements, proof text, labels, comments, and reference candidates are
  untrusted data.
- Proof lanes are isolated from the root transcript and from one another.
- Each verifier item runs in a fresh minimal workspace.
- Raw model streams and model-written result files are not proof authority.
- Root math-experiment receipts are private computational diagnostics, not
  proof steps or publication authority.
- Deterministic host code checks content digests, item coverage, model
  provenance, pass identity, and atomic publication.
- A read-only sandbox is not a complete confidentiality boundary. Use a
  dedicated container or OS account for sensitive adversarial input.

Runtime state, credentials, results, receipts, virtual environments, and
provider configuration are excluded from Git.

## Development

Run the verifier tests:

```bash
PYTHONDONTWRITEBYTECODE=1 \
agents/verification/.venv/bin/python -m pytest -q agents/verification/tests
```

Run the generation and launcher tests:

```bash
env -u CODEX_HOME PYTHONDONTWRITEBYTECODE=1 \
agents/.generation-venv/bin/python -m pytest -q agents/generation/tests
```

Unsetting `CODEX_HOME` keeps a surrounding coding-agent session's configuration
out of the launcher tests. Run these tests with generation roots stopped:
their environment preflight needs the deployment lock held by an active root.

For a focused check of this branch's source protocol and model profiles:

```bash
PYTHONDONTWRITEBYTECODE=1 \
agents/.generation-venv/bin/python -m pytest -q \
  agents/generation/tests/test_publication_export_v1.py \
  agents/generation/tests/test_model_policy.py
```

After changing shared MCP or proof-context code, rebuild and check the generated
legacy server:

```bash
agents/.generation-venv/bin/python -B \
  agents/generation/mcp/build_legacy_server.py --write
agents/.generation-venv/bin/python -B \
  agents/generation/mcp/build_legacy_server.py --check
```

No paid model is required for the test suites.

Live validation on 2026-09-05 exercised `max_diversity`: Astra `max` rejected an
invalid proof, and an independently supplied correct proof passed Astra `max`
and cold Opus 5 `max` verification. The host published a v6 receipt and a v1
AxiomGraph event; digest validation and repeated-read export idempotency passed.
The full council search canary completed its sandboxed math experiment and
blind phase, but the Astra revision process exited nonzero after 704 seconds
(`operational_blocked`). That run produced no proof or publication, so complete
council search remains unvalidated by this canary.

## Repository map

| Path | Purpose |
|---|---|
| `agents/generation/` | Statements, root contracts, orchestration, MCP, runners, and tests |
| `agents/verification/` | Verifier API, schemas, supervision, launcher, and tests |
| `agents/claude_core.py` | Persistent Claude-root host and route-council control plane |
| `agents/model_policy.py` | Zero-model role and profile resolver |
| `agents/MODEL_POLICY.md` | Detailed model-role and compatibility contract |
| [publication_export_v1.py](agents/generation/mcp/publication_export_v1.py) | Standard-library source-event validation and immutable storage |
| [axiomgraph_source_interface_v1.json](agents/generation/mcp/axiomgraph_source_interface_v1.json) | Versioned AxiomGraph source-interface manifest |
| `agents/generation/site/` | Optional Zola result browser; presentation only |

## License

[Apache License 2.0](LICENSE)
