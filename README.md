# AxiomRelay

English | [简体中文](README.zh-CN.md)

**Proof hill climbing for hard mathematics.**

Keep the audited frontier. Repair one gap at a time.

AxiomRelay separates route design, proof search, synthesis, verification, and
publication. Model output is always a draft; only the host can publish a proof,
and only after two independent whole-proof verifier passes agree on the exact
same bytes.

> **Status:** research infrastructure. The runtime provides strong process,
> provenance, and replay guarantees, but model verification remains
> probabilistic and is not a substitute for expert review.

## Why AxiomRelay exists

Hard proof attempts fail in ways that ordinary chat loops handle poorly:
plausible routes are correlated, long contexts drift, external suggestions are
mistaken for authority, retries duplicate expensive work, and a nearly correct
draft is easily confused with a verified theorem.

AxiomRelay addresses those failure modes with a small set of invariants:

- route design and proof execution are separate roles;
- at most three isolated proof lanes run from one host-validated plan set;
- an optional Opus + Sol council performs independent route design, one joint
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
state and chooses the next bottleneck; isolated Sol lanes supply search
diversity; Pro may propose a decisive local argument or counterexample; and
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
              GPT Sol | Opus | Fable | Opus + Sol council
                                      |
                          one accepted three-route slate
                                      v
                       three isolated GPT Sol lanes
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
| `core` | Default isolated runtime | GPT Sol, Opus, Fable, Opus + Sol council | Lower overhead |
| `reviewed` | Long-running compatibility workflow with scheduled reviews | GPT Sol | Higher overhead |

Noninteractive runs default to `core`. Interactive runs explain the choices.
The reviewed mode requires an explicit run ID.

### Roots

| Root | Role |
|---|---|
| `gpt-sol` | GPT Sol designs routes and orchestrates proof work. Default. |
| `opus` | Persistent logical Claude Opus 5 root; each launch performs one resumable turn. |
| `fable` | Persistent Claude Fable 5 root with the same host controls. |
| `opus-sol-council` | Opus and an isolated Sol/max seat design independently, revise once together, then run a final non-editing audit. |

Claude roots are currently available in `core` mode only. They receive a
read-only workspace view plus a narrow host interface; they do not receive
general shell, write, browser, or subagent authority.

### Model-policy profiles

| Profile | Proof lanes | Verifier 1 | Verifier 2 |
|---|---|---|---|
| `compatible` | Sol `max` | Sol `xhigh` | Sol `xhigh` |
| `balanced` | Sol `max` | Sol `xhigh` | Terra `max` |
| `economy` | Terra `max` | Sol `xhigh` | Terra `max` |
| `max_diversity` | Sol `max` | Sol `max` | Opus 5 1M `max` |

`max_diversity` requires authenticated OpenAI and Claude providers. Pass 2 is
cold: it receives the authenticated proof context but not Pass 1's verdict,
findings, or session state.

## Quick start

### 1. Clone and install the CLIs

```bash
git clone https://github.com/ot-triton-lab/AxiomRelay.git
cd AxiomRelay
npm install -g @openai/codex
```

Authenticate Codex before running a problem. Install and authenticate Claude
Code as well when using a Claude root or `max_diversity` verification.

### 2. Create the Python environments

Verifier:

```bash
python3 -m venv agents/verification/.venv
agents/verification/.venv/bin/pip install \
  -r agents/verification/api/requirements.txt
```

Generation:

```bash
python3 -m venv --copies --without-pip agents/.generation-venv
uv pip install --python agents/.generation-venv/bin/python \
  -r agents/generation/requirements-math-research.txt
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

The default endpoint is loopback HTTP on port `8091`. Remote deployment must
use HTTPS and a high-entropy `VERIFY_API_TOKEN`.

### 4. Run a problem

Create a local UTF-8 Markdown statement below `agents/generation/data/`. Problem
and answer files in that directory are intentionally ignored by Git. A nested
input such as `data/algebra/problem.md` produces output below
`results/algebra/problem/`.

Default GPT Sol root:

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=gpt-sol \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

Opus + Sol council with maximum diversity:

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=opus-sol-council \
AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

Run `./tests/run_example.sh` in a terminal without settings to use the
interactive selector.

## Asking GPT Pro about one blocked proof gap

This is the hill-climbing intervention described above. The preferred human
relay is gap-sized. After two independent attempts fail on
the same load-bearing claim, the canonical Claude root emits a write-once
`gap_id`, an exact copy/paste prompt, and a host-computed digest binding it to
the current statement, cited active memory records, and ledger head. It does
not wait for all three routes to fail merely to ask a precise question.
Preparing the packet never opens ChatGPT or spends a Pro turn; the owner
remains the only sender. Reviewed/GPT-Sol lanes do not have these four root
tools: they hand the precise gap evidence back to the canonical root.

`two failed mechanisms → gap query → owner relay → untrusted gap delta → repair-cone audit`

The root normally calls `prepare_pro_gap_query` itself. The equivalent owner
CLI is useful for recovery and testing:

```bash
PROBLEM_PATH=agents/generation/data/my_problem.md
PROBLEM_ID=my_problem
STATEMENT_SHA256="$(sha256sum "$PROBLEM_PATH" | cut -d' ' -f1)"

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
  "recommended_exact_question": "<the exact mathematical question for GPT Pro>"
}
JSON
```

The caller does not supply `source_context_sha256`. The host resolves every
cited record, rejects inactive or wrong-channel records, snapshots the ledger
head, computes the digest, and expands the question into a prompt containing
the settled facts, both failed paths, and all boundary checks. Copy only the
returned `copy_paste_prompt`.

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
STATEMENT_SHA256="$(sha256sum "$PROBLEM_PATH" | cut -d' ' -f1)"

agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --ingest-reference-candidate \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gpt_pro < pro-answer.md
```

Then start a new Opus + Sol council for that statement. The candidate moves
through the system as follows:

1. The host stores immutable bytes bound to the problem and statement digest.
2. Opus must bind the candidate marker and exact projection path to one route.
3. Sol's initial blind slate remains independent of the candidate.
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
| `AXIOM_RELAY_MAIN_AGENT` | `gpt-sol`, `opus`, `fable`, `opus-sol-council`, or `prompt` |
| `AXIOM_RELAY_MODEL_POLICY_PROFILE` | `compatible`, `balanced`, `economy`, or `max_diversity` |
| `AXIOM_RELAY_REVIEW_RUN_ID` | Required identity for reviewed mode |
| `AXIOM_RELAY_CLAUDE_SESSION_ID` | Resume an active logical Claude root |
| `AXIOM_RELAY_CLAUDE_TAKEOVER_FROM` | Explicitly fence and replace a recoverable Claude root |
| `AXIOM_RELAY_CLAUDE_OWNER_PROMPT` | Operator message for a resumed Claude turn or explicit takeover |
| `AXIOM_RELAY_CLAUDE_CONTEXT_WINDOW` | Claude context window; Opus defaults to 1M |
| `AXIOM_RELAY_PRINT_COMMAND` | Print a Claude root launch command without executing it |
| `PROBLEM_FILE` | Safe Markdown path below `agents/generation/data/` |
| `VERIFY_HEALTH_URL`, `VERIFY_PROOF_URL` | Verifier service endpoints |
| `VERIFY_API_TOKEN` | Bearer token required for non-loopback verification |

Historical environment names, schema identifiers, and receipts remain readable
for one transition release. New automation should use the settings above.
Historical receipt bytes must never be rewritten merely to update branding.

## Trust boundaries

- Statements, proof text, labels, comments, and reference candidates are
  untrusted data.
- Proof lanes are isolated from the root transcript and from one another.
- Each verifier item runs in a fresh minimal workspace.
- Raw model streams and model-written result files are not proof authority.
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
PYTHONDONTWRITEBYTECODE=1 \
agents/.generation-venv/bin/python -m pytest -q agents/generation/tests
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

## Repository map

| Path | Purpose |
|---|---|
| `agents/generation/` | Statements, root contracts, orchestration, MCP, runners, and tests |
| `agents/verification/` | Verifier API, schemas, supervision, launcher, and tests |
| `agents/claude_core.py` | Persistent Claude-root host and route-council control plane |
| `agents/model_policy.py` | Zero-model role and profile resolver |
| `agents/MODEL_POLICY.md` | Detailed model-role and compatibility contract |
| `agents/generation/site/` | Optional Zola result browser; presentation only |

## License

[Apache License 2.0](LICENSE)
