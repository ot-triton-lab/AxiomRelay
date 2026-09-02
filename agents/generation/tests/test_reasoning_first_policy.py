from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


GENERATION_ROOT = Path(__file__).resolve().parents[1]
AGENTS = GENERATION_ROOT / "AGENTS.md"
LEGACY_AGENTS = GENERATION_ROOT / "AGENTS.legacy.md"
LEGACY_FANOUT_SKILL = (
    GENERATION_ROOT / ".agents" / "skills" / "legacy-three-route" / "SKILL.md"
)
DIRECT_PROVING_SKILL = (
    GENERATION_ROOT / ".agents" / "skills" / "direct-proving" / "SKILL.md"
)
FAILURE_SYNTHESIS_SKILL = (
    GENERATION_ROOT
    / ".agents"
    / "skills"
    / "identify-key-failures"
    / "SKILL.md"
)
RUNNER = GENERATION_ROOT / "tests" / "run_hotjoin.sh"
POLICY_RE = re.compile(
    r"<!-- rethlas-safe-three-route-policy\s*(\{.*?\})\s*-->", re.DOTALL
)
LEGACY_POLICY_RE = re.compile(
    r"<!-- rethlas-legacy-runtime-policy\s*(\{.*?\})\s*-->", re.DOTALL
)


def _policy() -> tuple[dict[str, object], str]:
    text = AGENTS.read_text(encoding="utf-8")
    match = POLICY_RE.search(text)
    assert match is not None
    return json.loads(match.group(1)), text


def test_trusted_runner_python_is_isolated_and_receipt_uses_snapshot_context() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    assert re.search(r'"\$TRUSTED_PYTHON_BIN"\s+-B(?:\s|$)', runner) is None
    receipt = runner.split("receipt_is_valid() {", 1)[1].split(
        "\n}\n\nif [[ -e \"$verified_path\"", 1
    )[0]
    assert (
        '"$MCP_PUBLICATION_PROOF_CONTEXT_PATH" '
        '"$MCP_PUBLICATION_PROOF_CONTEXT_SHA256"'
    ) in receipt
    assert 'sys.path.insert(0, str(root / "mcp"))' not in receipt
    assert "load_attested_proof_context()" in receipt


def _node_binary() -> Path:
    configured = os.environ.get("RETHLAS_TEST_NODE_BIN")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    discovered = shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(
        Path("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node")
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    pytest.skip("Node.js unavailable; set RETHLAS_TEST_NODE_BIN")


def _action_allowed(
    policy: dict[str, object],
    *,
    phase: str,
    action: str,
    named_knowledge_gap: bool = False,
    query_count: int = 0,
) -> bool:
    if phase == "protected_route_design":
        return action in {
            "read_problem",
            "read_local_reference",
            "local_reasoning",
            "local_computation",
            "continuation_memory_search_once",
        }
    if phase == "candidate_fast_lane":
        return action in {
            "assemble_blueprint",
            "audit_candidate",
            "write_blueprint",
            "verify_blueprint",
            "repair_verifier_defect",
        }
    if action == "external_retrieval":
        return named_knowledge_gap and query_count < int(
            policy["max_targeted_retrieval_queries_per_gap"]
        )
    return True


def test_safe_three_route_policy_is_machine_readable_and_bounded() -> None:
    policy, text = _policy()
    assert policy == {
        "policy_id": "rethlas_safe_three_route_v1",
        "default_initial_deep_work_minutes": 60,
        "minimum_initial_deep_work_minutes": 10,
        "maximum_initial_deep_work_minutes": 120,
        "deep_work_minimum_is_soft": True,
        "initial_external_retrieval_calls": 0,
        "collaboration_spawns_before_route_checkpoint": 0,
        "host_route_council_mode": "opus_sol_council_v2",
        "host_route_council_model": "gpt-5.6-sol",
        "host_route_council_reasoning_effort": "max",
        "host_route_council_retrieval_capability": (
            "statement_bound_matlas_arxiv_only_v1"
        ),
        "host_route_council_retrieval_tools": [
            "search_matlas_theorems",
            "search_arxiv_theorems",
            "read_arxiv_primary",
        ],
        "host_route_council_search_queries_per_phase": 2,
        "host_route_council_primary_reads_per_phase": 4,
        "host_route_council_general_web": False,
        "host_route_council_workspace_access": False,
        "host_route_council_blind_slates": 2,
        "host_route_council_joint_revision_rounds": 1,
        "host_route_council_opus_adjudication_rounds": 1,
        "host_route_council_final_audits": 1,
        "host_route_council_max_replacement_routes": 1,
        "host_route_council_transcript_to_lanes": False,
        "host_route_council_is_proof_lane": False,
        "successor_council_requires_prior_failure_receipt": True,
        "fanout_plan_count": 3,
        "fanout_subagent_count": 3,
        "max_live_subagents": 3,
        "fanout_fork_turns": "none",
        "fanout_in_one_batch": True,
        "subagents_may_spawn": False,
        "subagents_inherit_root_model_and_effort": True,
        "subagents_write_shared_memory": False,
        "root_is_canonical_memory_writer": True,
        "initial_memory_init_calls": 0,
        "initial_memory_search_calls_for_continuation": 1,
        "persistence_mode": "write_behind_phase_checkpoint",
        "checkpoint_tool": "memory_append_batch",
        "max_checkpoint_records": 32,
        "max_root_only_batches_before_first_fanout": 1,
        "legal_yield_tool": "generation_yield",
        "legal_yield_requires_hotjoin": True,
        "legacy_non_success_disposition": "return_unverified_without_owner_wait",
        "retrieval_requires_explicit_knowledge_gap": True,
        "max_targeted_retrieval_queries_per_gap": 2,
        "candidate_fast_lane_forbids_new_search": True,
        "candidate_fast_lane_forbids_new_branches": True,
        "candidate_fast_lane_forbids_new_subagents": True,
        "reference_candidate_preempts_initial_fanout": False,
        "reference_candidate_reserves_exactly_one_fanout_lane": True,
        "root_authored_complete_candidate_preempts_initial_fanout": True,
        "verified_lane_candidate_preempts_wait": True,
        "advisor_after_three_route_failure_synthesis": True,
        "telemetry_must_not_invent_reasoning_tokens": True,
        "enforcement_scope": (
            "instruction_runner_prompt_and_contract_tests_not_sampling_interceptor"
        ),
    }
    assert "soft deep-work target" in text
    assert "single pre-fanout checkpoint" in text
    assert "second and only editing dialogue" in text
    assert "root may publish at most one" in text
    assert "Root-only skills contribute scratch" in text
    assert "exactly three context-free route solvers" in text
    assert "must not become a fourth proof direction" in text
    assert "Search volume" in text
    assert "candidate fast lane" in text


def test_legacy_runtime_policy_is_machine_readable_and_physically_narrow() -> None:
    text = AGENTS.read_text(encoding="utf-8")
    match = LEGACY_POLICY_RE.search(text)
    assert match is not None
    policy = json.loads(match.group(1))
    assert policy["profile"] == "legacy"
    assert policy["runner"] == "tests/run_legacy.sh"
    assert policy["instruction_profile"] == "AGENTS.legacy.md"
    assert policy["automatic_project_doc_bytes"] == 0
    assert policy["trusted_mcp_modules"] == [
        "mcp.proof_context",
        "mcp.legacy_verification_client",
        "mcp.legacy_server",
    ]
    assert policy["allowed_mcp_tools"] == [
        "append_route_terminal_report",
        "branch_update",
        "memory_append",
        "memory_append_batch",
        "memory_init",
        "memory_search",
        "read_arxiv_primary",
        "search_arxiv_theorems",
        "search_matlas_theorems",
        "verify_blueprint_service",
    ]
    assert policy["hotjoin_executable_control_sources_in_closure"] == 0
    assert policy["owner_yield_available"] is False
    assert policy["review_available"] is False
    assert policy["guardian_available"] is False
    assert policy["terminal_report_persistence"] == "write_ahead_per_child"
    assert policy["optional_stop_after_current_cohort"] is True


def test_legacy_instruction_profile_is_bounded_and_has_no_review_protocol() -> None:
    raw = LEGACY_AGENTS.read_bytes()
    text = raw.decode("utf-8")
    assert 0 < len(raw) <= 32_768
    assert "# Legacy Math Reasoning Profile" in text
    assert "$legacy-three-route" in text
    assert "## Continuous supervisor" not in text
    assert "## Durable route-review cadence" not in text
    assert "scheduled review commitment" not in text


def test_external_claude_plan_has_one_payload_rule_and_no_example_search() -> None:
    instructions = LEGACY_AGENTS.read_text(encoding="utf-8")
    skill = LEGACY_FANOUT_SKILL.read_text(encoding="utf-8")
    assert "per-problem filesystem\ncapsule" in instructions
    assert "Missing unrelated workspace content is an" in instructions
    assert "intentional isolation boundary" in instructions
    assert "Do not inspect another" in skill
    assert "items=[]" in instructions
    assert "items=[]" in skill
    assert "Never manually reproduce" in skill
    assert "atomically materializes" in skill
    assert "never search the filesystem for checkpoint" in skill


def test_legacy_terminal_reports_are_write_ahead_before_the_next_wait() -> None:
    instructions = LEGACY_AGENTS.read_text(encoding="utf-8")
    skill = LEGACY_FANOUT_SKILL.read_text(encoding="utf-8")
    assert "Each non-candidate child terminal is its own durability boundary" in (
        instructions
    )
    assert "before waiting for another child" in instructions
    assert "Before the next wait" in skill
    assert "`append_route_terminal_report`" in skill
    assert "hand-build that wrapper or hash" in skill
    assert "one-item checkpoint" in skill
    assert "Never defer a returned report" in skill
    assert "Do not append or rewrite their report bodies" in skill


def test_route_children_do_not_guess_cwd_or_poll_processes() -> None:
    skill = DIRECT_PROVING_SKILL.read_text(encoding="utf-8")
    assert "normally omit `workdir`" in skill
    assert "never reconstruct, abbreviate, or guess" in skill
    assert "Do not retry the same program" in skill
    assert "Do not invoke `/bin/ps`" in skill
    assert "at most one targeted interrupt" in skill


def test_failure_synthesis_cannot_upgrade_diagnostic_claims() -> None:
    skill = FAILURE_SYNTHESIS_SKILL.read_text(encoding="utf-8")
    assert "Preserve the source report's epistemic status exactly" in skill
    for qualifier in (
        "conjectural",
        "diagnostic",
        "heuristic",
        "lower-bound-only",
        "sufficient-only",
        "unproved",
    ):
        assert qualifier in skill
    assert "Never replace one of" in skill
    assert "root has independently checked that proof" in skill


def test_route_design_and_candidate_fast_lane_reject_fragmenting_actions() -> None:
    policy, _text = _policy()
    for action in (
        "memory_init",
        "memory_append",
        "memory_append_batch",
        "branch_update",
        "external_retrieval",
        "spawn_subagent",
    ):
        assert not _action_allowed(
            policy, phase="protected_route_design", action=action
        )
    for action in (
        "external_retrieval",
        "memory_search",
        "spawn_subagent",
        "propose_plan",
        "advisor_checkpoint",
        "wait_agent",
    ):
        assert not _action_allowed(policy, phase="candidate_fast_lane", action=action)
    for action in (
        "assemble_blueprint",
        "audit_candidate",
        "write_blueprint",
        "verify_blueprint",
        "repair_verifier_defect",
    ):
        assert _action_allowed(policy, phase="candidate_fast_lane", action=action)


def test_external_retrieval_requires_named_gap_and_two_query_budget() -> None:
    policy, _text = _policy()
    assert not _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=False,
    )
    assert _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=True,
        query_count=0,
    )
    assert _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=True,
        query_count=1,
    )
    assert not _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=True,
        query_count=2,
    )


def test_checkpoint_recovery_requires_unknown_primary_and_identical_payload() -> None:
    _policy_value, agents = _policy()
    timeout_60s = (
        "tool call error: tool call failed for "
        "`reasoning_checkpoint_primary/memory_append_batch`\n\n"
        "Caused by:\n"
        "    timed out awaiting tools/call after 60s"
    )
    timeout_60000ms = (
        "tool call error: tool call failed for "
        "`reasoning_checkpoint_primary/memory_append_batch`\n\n"
        "Caused by:\n"
        "    timed out awaiting tools/call after 60000ms"
    )
    checkpoint_contract = agents.split(
        "Checkpoint batches use the two dedicated", 1
    )[1].split("## Phase-boundary routing", 1)[0]
    assert "**one** `functions.exec` JavaScript\nprogram" in agents
    assert "const checkpointArgs = Object.freeze({problem_id, items});" in agents
    primary_call = (
        "tools.mcp__reasoning_checkpoint_primary__memory_append_batch("
    )
    recovery_call = (
        "tools.mcp__reasoning_checkpoint_recovery__memory_append_batch("
    )
    assert checkpoint_contract.count(primary_call) == 1
    assert checkpoint_contract.count(recovery_call) == 1
    assert "tools.mcp__reasoning_agent__memory_append_batch(" not in (
        checkpoint_contract
    )
    assert checkpoint_contract.count(
        "memory_append_batch(\n      checkpointArgs\n    )"
    ) == 2
    assert "const retryablePrimaryTimeout = (failure) =>" in checkpoint_contract
    assert 'exactObject(failure, ["content", "isError"])' in checkpoint_contract
    assert "failure.isError === true" in checkpoint_contract
    assert "failure.content.length === 1" in checkpoint_contract
    assert (
        'exactObject(failure.content[0], ["type", "text"])'
        in checkpoint_contract
    )
    assert 'failure.content[0].type === "text"' in checkpoint_contract
    assert json.dumps(timeout_60s) in checkpoint_contract
    assert json.dumps(timeout_60000ms) in checkpoint_contract
    assert "if (!retryablePrimaryTimeout(failure)) throw failure;" in (
        checkpoint_contract
    )
    assert "failure.includes" not in checkpoint_contract
    assert "failure.match" not in checkpoint_contract
    assert "RegExp" not in checkpoint_contract
    assert "receipt.isError === true" in agents
    assert 'receipt.isError !== false' in agents
    assert '["content", "isError", "structuredContent"]' in agents
    assert "body.status !== \"ok\"" in agents
    assert "rethlas_memory_batch_local_commit_receipt_v1" in agents
    assert "const hostBodyKeys = [...localBodyKeys, \"publication_receipt\"]" in agents
    assert "if (!localCommit && !hostPublication)" in agents
    assert 'publication.state !== "accepted"' in agents
    assert 'publication.publication_class !== "reasoning_checkpoint"' in agents
    assert "!sameJson(textBody, body)" in agents
    assert "never\nmake a third call, poll the unknown primary" in agents
    assert "long\n`reasoning_agent` server must never call" in agents
    assert "entire recovery\nallowlist" in checkpoint_contract
    assert "Compare the two text values with `===` only" in checkpoint_contract
    assert "never accept a primitive string" in checkpoint_contract
    assert "`structuredContent`, `_meta`, substring" in checkpoint_contract
    assert "yields `Script running with cell ID`" in checkpoint_contract
    assert "use `functions.wait` on that exact same cell" in checkpoint_contract
    assert "bounded\nto at most 120 seconds" in checkpoint_contract
    assert "continuation of the one outer cell, not an MCP\npoll or retry" in (
        checkpoint_contract
    )
    assert "Never issue a separate primary poll, new `functions.exec`" in (
        checkpoint_contract
    )


def test_checkpoint_recovery_state_machine_executes_exact_envelope_only() -> None:
    _policy_value, agents = _policy()
    checkpoint_contract = agents.split(
        "Checkpoint batches use the two dedicated", 1
    )[1].split("## Phase-boundary routing", 1)[0]
    program = checkpoint_contract.split("```javascript\n", 1)[1].split(
        "\n```", 1
    )[0]
    checkpoint_line = "const checkpointArgs = Object.freeze({problem_id, items});"
    classifier_and_machine = "const retryablePrimaryTimeout = " + program.split(
        "const retryablePrimaryTimeout = ", 1
    )[1].split("\ntext(JSON.stringify(receipt));", 1)[0]
    state_machine = checkpoint_line + "\n" + classifier_and_machine
    timeout_60s = (
        "tool call error: tool call failed for "
        "`reasoning_checkpoint_primary/memory_append_batch`\n\n"
        "Caused by:\n"
        "    timed out awaiting tools/call after 60s"
    )
    timeout_60000ms = timeout_60s.replace("after 60s", "after 60000ms")
    harness = r'''
const successReceipt = Object.freeze({status: "test-success"});
const exactEnvelope = (message) => ({
  content: [{type: "text", text: message}],
  isError: true
});

async function runCase(primaryOutcome, recoveryOutcome = successReceipt) {
  const problem_id = "frontiermath/chowla-cosine";
  const items = Object.freeze([{channel: "proof_steps", record: {claim: "x"}}]);
  const primaryArgs = [];
  const recoveryArgs = [];
  const tools = {
    mcp__reasoning_checkpoint_primary__memory_append_batch: async (args) => {
      primaryArgs.push(args);
      return primaryOutcome;
    },
    mcp__reasoning_checkpoint_recovery__memory_append_batch: async (args) => {
      recoveryArgs.push(args);
      if (recoveryOutcome instanceof Error) throw recoveryOutcome;
      return recoveryOutcome;
    }
  };
  const checkedReceipt = (value) => {
    if (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      value.isError === true
    ) {
      throw value;
    }
    if (value !== successReceipt) {
      throw new TypeError("invalid durable checkpoint receipt");
    }
    return value;
  };
  let propagated = null;
  try {
STATE_MACHINE
  } catch (failure) {
    propagated = failure;
  }
  return {
    primaryCalls: primaryArgs.length,
    recoveryCalls: recoveryArgs.length,
    sameIdentity:
      primaryArgs.length === 1 &&
      recoveryArgs.length === 1 &&
      primaryArgs[0] === recoveryArgs[0],
    frozen:
      primaryArgs.length === 1 &&
      Object.isFrozen(primaryArgs[0]),
    propagated: propagated !== null
  };
}

(async () => {
  const failed = [];
  for (const message of [timeout60, timeout60000]) {
    const outcome = await runCase(exactEnvelope(message));
    if (
      outcome.primaryCalls !== 1 ||
      outcome.recoveryCalls !== 1 ||
      !outcome.sameIdentity ||
      !outcome.frozen ||
      outcome.propagated
    ) {
      failed.push("exact timeout envelope: " + JSON.stringify(outcome));
    }
  }

  const semantic = "authorization rejected";
  const rejected = [
    timeout60,
    "prefix " + timeout60,
    timeout60 + " suffix",
    undefined,
    null,
    false,
    0,
    [],
    [exactEnvelope(timeout60)],
    {},
    {isError: true},
    {content: [{type: "text", text: timeout60}]},
    {content: null, isError: true},
    {content: {type: "text", text: timeout60}, isError: true},
    {content: [], isError: true},
    {content: [null], isError: true},
    {content: [timeout60], isError: true},
    {content: [{type: "text", text: timeout60}], isError: false},
    {content: [{type: "text", text: timeout60}], isError: "true"},
    {content: [{text: timeout60}], isError: true},
    {content: [{type: "text"}], isError: true},
    {content: [{type: "text", text: 60}], isError: true},
    {content: [{type: "text", text: semantic}], isError: true},
    {content: [{type: "text", text: timeout60 + "x"}], isError: true},
    {
      content: [{
        type: "text",
        text: timeout60.replace("tool call error: ", "")
      }],
      isError: true
    },
    {
      content: [{
        type: "text",
        text: timeout60.replace("\n\nCaused by:\n", ": ")
      }],
      isError: true
    },
    {
      content: [{type: "text", text: timeout60.replace("\n\n", "\n")}],
      isError: true
    },
    {
      content: [{type: "text", text: timeout60.replace("    timed", " timed")}],
      isError: true
    },
    {content: [{type: "text", text: timeout60}], isError: true, extra: 1},
    {
      content: [{type: "text", text: timeout60}],
      isError: true,
      structuredContent: null
    },
    {
      content: [{type: "text", text: timeout60}],
      isError: true,
      _meta: {}
    },
    {content: [{type: "image", text: timeout60}], isError: true},
    {content: [{type: "text", text: timeout60, extra: 1}], isError: true},
    {
      content: [
        {type: "text", text: timeout60},
        {type: "text", text: timeout60}
      ],
      isError: true
    },
    {
      content: [{type: "text", text: timeout60.replace("primary", "recovery")}],
      isError: true
    }
  ];
  for (let index = 0; index < rejected.length; index += 1) {
    const outcome = await runCase(rejected[index]);
    if (outcome.recoveryCalls !== 0) {
      failed.push("rejected case " + index + ": " + JSON.stringify(outcome));
    }
  }

  const primarySuccess = await runCase(successReceipt);
  if (
    primarySuccess.primaryCalls !== 1 ||
    primarySuccess.recoveryCalls !== 0 ||
    primarySuccess.propagated
  ) {
    failed.push("primary success: " + JSON.stringify(primarySuccess));
  }

  const recoveryFailure = await runCase(
    exactEnvelope(timeout60),
    new Error("recovery failed")
  );
  if (
    recoveryFailure.primaryCalls !== 1 ||
    recoveryFailure.recoveryCalls !== 1 ||
    !recoveryFailure.sameIdentity ||
    !recoveryFailure.propagated
  ) {
    failed.push("recovery failure: " + JSON.stringify(recoveryFailure));
  }

  process.stdout.write(JSON.stringify({failed, rejectedCount: rejected.length}));
})().catch((failure) => {
  process.stderr.write(String(failure));
  process.exitCode = 1;
});
'''
    script = (
        "const timeout60 = "
        + json.dumps(timeout_60s)
        + ";\nconst timeout60000 = "
        + json.dumps(timeout_60000ms)
        + ";\n"
        + harness.replace("STATE_MACHINE", state_machine)
    )
    completed = subprocess.run(
        [str(_node_binary())],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"failed": [], "rejectedCount": 35}


def test_checkpoint_receipt_validator_executes_fail_closed_mutation_matrix() -> None:
    _policy_value, agents = _policy()
    checkpoint_contract = agents.split(
        "Checkpoint batches use the two dedicated", 1
    )[1].split("## Phase-boundary routing", 1)[0]
    program = checkpoint_contract.split("```javascript\n", 1)[1].split(
        "\n```", 1
    )[0]
    validator = "const checkedReceipt = " + program.split(
        "const checkedReceipt = ", 1
    )[1].split("\nlet receipt;", 1)[0]
    checkpoint_args = {
        "problem_id": "data/frontiermath/chowla-cosine",
        "items": [
            {"channel": "proof_steps", "record": {"claim": "x"}},
            {
                "channel": "failed_paths",
                "record": {"claim": "y"},
                "active": False,
                "supersedes": ["mem_" + "a" * 64],
            },
        ],
    }
    harness = r'''
const hex = "a".repeat(64);
const body = {
  schema_version: "rethlas_memory_batch_receipt_v3",
  status: "ok",
  problem_id: checkpointArgs.problem_id,
  batch_id: `batch_${hex}`,
  checkpoint_sha256: "b".repeat(64),
  timestamp_utc: "2026-08-12T12:00:00.000001+00:00",
  committed_at_utc: "2026-08-12T12:00:01.000001+00:00",
  committed_at_monotonic: 100,
  commit_sha256: "c".repeat(64),
  count: 2,
  records: [
    {
      record_id: `mem_${"d".repeat(64)}`,
      channel: "proof_steps",
      active: true,
      supersedes: []
    },
    {
      record_id: `mem_${"e".repeat(64)}`,
      channel: "failed_paths",
      active: false,
      supersedes: [`mem_${hex}`]
    }
  ],
  checkpoint_path:
    `/tmp/memory/data/frontiermath/chowla-cosine/` +
    `.phase_checkpoints/batch_${hex}.json`,
  publication_receipt: {
    schema_version: "rethlas_memory_batch_publication_receipt_v1",
    state: "accepted",
    run_id: "guardian-soak-20260812-fresh-07",
    problem_id: checkpointArgs.problem_id,
    batch_id: `batch_${hex}`,
    checkpoint_sha256: "b".repeat(64),
    commit_sha256: "c".repeat(64),
    publication_class: "reasoning_checkpoint",
    cycle_id: `cycle_${"f".repeat(32)}`,
    cutoff_action_id: `cadact_${"1".repeat(32)}`,
    cutoff_kind: "review_1",
    cutoff_at_utc: "2026-08-12T12:30:00+00:00",
    cutoff_monotonic: 200,
    accepted_at_utc: "2026-08-12T12:00:01.000001+00:00",
    accepted_at_monotonic: 100,
    boot_identity: "boot-id",
    receipt_sha256: "2".repeat(64)
  }
};
const envelope = (value) => ({
  content: [{type: "text", text: JSON.stringify(value)}],
  structuredContent: value,
  isError: false
});
const clone = (value) => JSON.parse(JSON.stringify(value));
const localBody = clone(body);
localBody.schema_version =
  "rethlas_memory_batch_local_commit_receipt_v1";
delete localBody.publication_receipt;
const cases = [];
const raw = (name, value) => cases.push([name, () => value]);
const synced = (name, mutate) => {
  const value = clone(body);
  mutate(value);
  raw(name, envelope(value));
};
const syncedLocal = (name, mutate) => {
  const value = clone(localBody);
  mutate(value);
  raw(name, envelope(value));
};

raw("undefined", undefined);
raw("string", "ok");
raw("null", null);
raw("array", []);
raw("isError true", {...envelope(clone(body)), isError: true});
raw("isError string", {...envelope(clone(body)), isError: "false"});
raw("missing isError", {
  content: envelope(body).content,
  structuredContent: body
});
raw("extra outer field", {...envelope(clone(body)), extra: null});
raw("structuredContent string", {
  ...envelope(clone(body)),
  structuredContent: "ok"
});
raw("empty content", {...envelope(clone(body)), content: []});
raw("two content blocks", {
  ...envelope(clone(body)),
  content: [...envelope(body).content, ...envelope(body).content]
});
raw("content block extra field", {
  ...envelope(clone(body)),
  content: [{...envelope(body).content[0], extra: null}]
});
raw("content invalid json", {
  ...envelope(clone(body)),
  content: [{type: "text", text: "{"}]
});
raw("content disagrees", {
  ...envelope(clone(body)),
  content: [{
    type: "text",
    text: JSON.stringify({...body, status: "error"})
  }]
});

synced("top extra field", (value) => { value.extra = null; });
synced("status error", (value) => { value.status = "error"; });
synced("top schema", (value) => { value.schema_version = "v2"; });
synced("host fields under local schema", (value) => {
  value.schema_version = "rethlas_memory_batch_local_commit_receipt_v1";
});
synced("problem mismatch", (value) => {
  value.problem_id = "other";
  value.publication_receipt.problem_id = "other";
});
synced("count string", (value) => { value.count = "2"; });
synced("count boolean", (value) => { value.count = true; });
synced("record count mismatch", (value) => { value.records.pop(); });
synced("batch malformed", (value) => {
  value.batch_id = "batch_bad";
  value.publication_receipt.batch_id = "batch_bad";
});
synced("checkpoint digest malformed", (value) => {
  value.checkpoint_sha256 = "no";
  value.publication_receipt.checkpoint_sha256 = "no";
});
synced("timestamp invalid", (value) => {
  value.timestamp_utc = "not-a-time";
});
synced("timestamp after commit", (value) => {
  value.timestamp_utc = "2026-08-12T12:00:02+00:00";
});
synced("commit monotonic string", (value) => {
  value.committed_at_monotonic = "100";
  value.publication_receipt.accepted_at_monotonic = "100";
});
synced("path unbound", (value) => {
  value.checkpoint_path = "/tmp/other.json";
});
synced("publication null", (value) => {
  value.publication_receipt = null;
});
synced("publication extra field", (value) => {
  value.publication_receipt.extra = null;
});
synced("publication rejected", (value) => {
  value.publication_receipt.state = "rejected";
});
synced("publication control", (value) => {
  value.publication_receipt.publication_class = "control_only";
});
synced("nested problem mismatch", (value) => {
  value.publication_receipt.problem_id = "other";
});
synced("nested batch mismatch", (value) => {
  value.publication_receipt.batch_id = `batch_${"9".repeat(64)}`;
});
synced("nested checkpoint mismatch", (value) => {
  value.publication_receipt.checkpoint_sha256 = "9".repeat(64);
});
synced("nested commit mismatch", (value) => {
  value.publication_receipt.commit_sha256 = "9".repeat(64);
});
synced("accepted utc mismatch", (value) => {
  value.publication_receipt.accepted_at_utc =
    "2026-08-12T12:00:02+00:00";
});
synced("accepted monotonic mismatch", (value) => {
  value.publication_receipt.accepted_at_monotonic = 101;
});
synced("accepted at wall cutoff", (value) => {
  value.committed_at_utc = value.publication_receipt.cutoff_at_utc;
  value.publication_receipt.accepted_at_utc =
    value.publication_receipt.cutoff_at_utc;
});
synced("accepted at monotonic cutoff", (value) => {
  value.committed_at_monotonic = 200;
  value.publication_receipt.accepted_at_monotonic = 200;
});
synced("bad cutoff kind", (value) => {
  value.publication_receipt.cutoff_kind = "review_3";
});
synced("bad cycle id", (value) => {
  value.publication_receipt.cycle_id = "cycle_bad";
});
synced("bad cutoff action id", (value) => {
  value.publication_receipt.cutoff_action_id = "cadact_bad";
});
synced("empty boot identity", (value) => {
  value.publication_receipt.boot_identity = "";
});
synced("non-printable boot identity", (value) => {
  value.publication_receipt.boot_identity = "boot\nidentity";
});
synced("oversized boot identity", (value) => {
  value.publication_receipt.boot_identity = "b".repeat(129);
});
synced("malformed receipt digest", (value) => {
  value.publication_receipt.receipt_sha256 = "no";
});
synced("record extra field", (value) => {
  value.records[0].extra = null;
});
synced("record duplicate id", (value) => {
  value.records[1].record_id = value.records[0].record_id;
});
synced("record malformed id", (value) => {
  value.records[0].record_id = "mem_bad";
});
synced("record channel", (value) => {
  value.records[0].channel = "failed_paths";
});
synced("record active", (value) => { value.records[0].active = false; });
synced("record supersedes", (value) => {
  value.records[0].supersedes = [`mem_${hex}`];
});
syncedLocal("local extra field", (value) => { value.extra = null; });
syncedLocal("local publication null", (value) => {
  value.publication_receipt = null;
});
syncedLocal("local publication object", (value) => {
  value.publication_receipt = clone(body.publication_receipt);
});
syncedLocal("host schema without publication", (value) => {
  value.schema_version = "rethlas_memory_batch_receipt_v3";
});
syncedLocal("local status error", (value) => { value.status = "error"; });
syncedLocal("local path unbound", (value) => {
  value.checkpoint_path = "/tmp/other.json";
});
syncedLocal("local count mismatch", (value) => { value.count = 1; });
syncedLocal("local record mismatch", (value) => {
  value.records[0].channel = "failed_paths";
});
syncedLocal("local malformed commit digest", (value) => {
  value.commit_sha256 = "no";
});

const failed = [];
const validHost = checkedReceipt(envelope(clone(body)));
if (validHost.status !== "ok") failed.push("valid host receipt");
const validLocal = checkedReceipt(envelope(clone(localBody)));
if (
  validLocal.schema_version !==
    "rethlas_memory_batch_local_commit_receipt_v1"
) failed.push("valid local receipt");
for (const [name, build] of cases) {
  let rejected = false;
  try {
    checkedReceipt(build());
  } catch (_error) {
    rejected = true;
  }
  if (!rejected) failed.push(name);
}
process.stdout.write(JSON.stringify({
  mutation_count: cases.length,
  failed
}));
'''
    script = (
        "const checkpointArgs = Object.freeze("
        + json.dumps(checkpoint_args, separators=(",", ":"))
        + ");\n"
        + validator
        + harness
    )
    completed = subprocess.run(
        [str(_node_binary())],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome == {"mutation_count": 62, "failed": []}


def test_high_frequency_skills_use_write_behind_and_unsafe_fanout_is_absent() -> None:
    skill_names = (
        "obtain-immediate-conclusions",
        "construct-counterexamples",
        "construct-toy-examples",
        "direct-proving",
        "propose-subgoal-decomposition-plans",
        "query-memory",
        "identify-key-failures",
        "recursive-proving",
        "search-math-results",
        "verify-proof",
    )
    for skill_name in skill_names:
        skill = (
            GENERATION_ROOT / ".agents" / "skills" / skill_name / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "memory_append_batch" in skill, skill_name
        assert "- `memory_append`" not in skill, skill_name
        assert "- `branch_update`" not in skill, skill_name

    agents = AGENTS.read_text(encoding="utf-8")
    assert "legacy JSONL outside the host publication registry" in agents
    assert "Relevant released-run tools" in agents
    recursive = (
        GENERATION_ROOT / ".agents" / "skills" / "recursive-proving" / "SKILL.md"
    ).read_text(encoding="utf-8")
    forbidden = (
        "Every intermediate artifact must be written to memory",
        "Always call `search_arxiv_theorems` for nontrivial subgoals",
        "Use web search early to gather background",
        "may itself spawn sub-agents recursively",
        "write progress into shared memory",
        "one adversarial critic",
    )
    combined = agents + "\n" + recursive
    for stale_clause in forbidden:
        assert stale_clause not in combined


def test_skill_interface_prompts_bind_safe_three_route_fanout() -> None:
    interface_expectations = {
        "recursive-proving": "exactly three context-free route solvers",
        "propose-subgoal-decomposition-plans": "exactly three materially different",
        "search-math-results": "one named external knowledge gap",
        "direct-proving": "one assigned route",
        "query-memory": "one bounded slice",
    }
    for skill_name, expected in interface_expectations.items():
        metadata = (
            GENERATION_ROOT
            / ".agents"
            / "skills"
            / skill_name
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8")
        assert expected in metadata
        assert "recursively spawn" not in metadata
    recursive_metadata = (
        GENERATION_ROOT
        / ".agents"
        / "skills"
        / "recursive-proving"
        / "agents"
        / "openai.yaml"
    ).read_text(encoding="utf-8")
    assert "no child recursion or shared-memory writes" in recursive_metadata


def test_non_success_yields_cannot_masquerade_as_solution() -> None:
    _policy_data, agents = _policy()
    assert "The only successful terminal state" in agents
    assert "waiting_cost_gate" in agents
    assert "waiting_owner_advisor_decision" in agents
    assert "the theorem remains unsolved" in agents
    assert "generation_yield" in agents
    assert "does not stop the runner" in agents
