from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest


GENERATION_ROOT = Path(__file__).resolve().parents[1]
SKILL = GENERATION_ROOT / ".agents" / "skills" / "recursive-proving" / "SKILL.md"
AGENTS = GENERATION_ROOT / "AGENTS.md"
RUNNER = Path(__file__).with_name("run_hotjoin.sh")
POLICY_RE = re.compile(
    r"<!-- rethlas-recursive-wait-policy\s*(\{.*?\})\s*-->", re.DOTALL
)
ADVISOR_POLICY_RE = re.compile(
    r"<!-- rethlas-advisor-checkpoint-policy\s*(\{.*?\})\s*-->", re.DOTALL
)
FANOUT_POLICY_RE = re.compile(
    r"<!-- rethlas-three-route-fanout-policy\s*(\{.*?\})\s*-->", re.DOTALL
)


def _policy() -> tuple[dict[str, object], str]:
    text = SKILL.read_text(encoding="utf-8")
    match = POLICY_RE.search(text)
    assert match is not None
    return json.loads(match.group(1)), text


def _advisor_policy() -> tuple[dict[str, object], str]:
    text = SKILL.read_text(encoding="utf-8")
    match = ADVISOR_POLICY_RE.search(text)
    assert match is not None
    return json.loads(match.group(1)), text


def _fanout_policy() -> tuple[dict[str, object], str]:
    text = SKILL.read_text(encoding="utf-8")
    match = FANOUT_POLICY_RE.search(text)
    assert match is not None
    return json.loads(match.group(1)), text


def _replay_wait_schedule(policy: dict[str, object], outcomes: list[str]) -> list[int]:
    timeout = int(policy["initial_timeout_ms"])
    maximum = int(policy["max_timeout_ms"])
    multiplier = int(policy["backoff_multiplier"])
    waits: list[int] = []
    for outcome in outcomes:
        waits.append(timeout)
        if outcome == "mailbox_progress":
            assert policy["reset_timeout_on_mailbox_progress"] is True
            timeout = int(policy["initial_timeout_ms"])
        else:
            assert outcome == "timeout_no_progress"
            timeout = min(timeout * multiplier, maximum)
    return waits


@dataclass
class _PolicyReplay:
    """Executable offline model of the instruction-level flow contract."""

    policy: dict[str, object]
    plan_ids: tuple[str, ...]
    next_timeout_ms: int = field(init=False)
    orchestration_resumptions: int = 0
    observed_orchestration_input_tokens: int | None = None
    no_progress_timeouts: int = 0
    status: str = "running"
    cost_gate_reason: str | None = None
    successful_plan_ids: set[str] = field(default_factory=set)
    failed_plan_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.next_timeout_ms = int(self.policy["initial_timeout_ms"])

    def wait_timeout(self) -> int:
        if self.status != "running":
            raise RuntimeError("cost-gated replay cannot issue another wait")
        return self.next_timeout_ms

    def record_collaboration_result(
        self, outcome: str, *, observed_input_tokens: int | None = None
    ) -> None:
        if self.status != "running":
            raise RuntimeError("cost-gated replay cannot resume orchestration")
        if outcome not in {
            "timeout_no_progress",
            "mailbox_progress",
            "spawn_fanout_result",
            "followup_fanout_result",
        }:
            raise ValueError(f"unknown collaboration outcome: {outcome}")
        if observed_input_tokens is not None and observed_input_tokens < 0:
            raise ValueError("observed input tokens must be non-negative")
        self.orchestration_resumptions += 1
        if observed_input_tokens is not None:
            self.observed_orchestration_input_tokens = (
                self.observed_orchestration_input_tokens or 0
            ) + observed_input_tokens

        if outcome == "timeout_no_progress":
            self.no_progress_timeouts += 1
            self.next_timeout_ms = min(
                self.next_timeout_ms * int(self.policy["backoff_multiplier"]),
                int(self.policy["max_timeout_ms"]),
            )
        elif outcome == "mailbox_progress":
            self.no_progress_timeouts = 0
            self.next_timeout_ms = int(self.policy["initial_timeout_ms"])

        if self.orchestration_resumptions >= int(
            self.policy["max_orchestration_resumptions"]
        ):
            self._gate("max_orchestration_resumptions")
        elif (
            self.observed_orchestration_input_tokens is not None
            and self.observed_orchestration_input_tokens
            >= int(self.policy["max_observed_orchestration_input_tokens"])
        ):
            self._gate("max_observed_orchestration_input_tokens")
        elif self.no_progress_timeouts >= int(
            self.policy["max_consecutive_no_progress_timeouts"]
        ):
            self._gate("max_consecutive_no_progress_timeouts")

    def _gate(self, reason: str) -> None:
        self.status = "waiting_cost_gate"
        self.cost_gate_reason = reason

    def unclassified_plan_ids(self) -> set[str]:
        return set(self.plan_ids) - self.successful_plan_ids - self.failed_plan_ids


def test_recursive_wait_contract_uses_long_backoff_and_early_wake_reset() -> None:
    policy, text = _policy()

    assert policy == {
        "policy_id": "rethlas_recursive_wait_v1",
        "initial_timeout_ms": 600_000,
        "backoff_multiplier": 2,
        "max_timeout_ms": 3_600_000,
        "max_consecutive_no_progress_timeouts": 4,
        "max_orchestration_resumptions": 16,
        "max_observed_orchestration_input_tokens": 3_000_000,
        "cost_gate_policy_manifest_json_env": "RETHLAS_RESOLVED_COST_POLICY_JSON",
        "cost_gate_policy_manifest_sha256_env": (
            "RETHLAS_RESOLVED_COST_POLICY_SHA256"
        ),
        "cost_gate_policy_manifest_schema": "rethlas_resolved_cost_policy_v1",
        "cost_gate_policy_values": ["owner_gated", "disabled_by_owner"],
        "default_cost_gate_policy": "owner_gated",
        "disabled_by_owner_keeps_telemetry": True,
        "disabled_by_owner_may_yield_waiting_cost_gate": False,
        "max_status_queries_without_mailbox_change": 0,
        "reset_timeout_on_mailbox_progress": True,
        "enforcement_scope": (
            "instruction_runner_integrity_and_host_generation_yield_preflight"
        ),
    }
    assert _replay_wait_schedule(
        policy,
        [
            "timeout_no_progress",
            "timeout_no_progress",
            "timeout_no_progress",
            "mailbox_progress",
            "timeout_no_progress",
        ],
    ) == [600_000, 1_200_000, 2_400_000, 3_600_000, 600_000]
    assert "wakes early" in text
    assert "mailbox" in text
    assert not re.search(r"timeout_ms\s*[=:]\s*60_?000\b", text)


def test_recursive_wait_contract_forbids_no_change_status_poll_and_auto_human() -> None:
    policy, text = _policy()
    flattened = " ".join(text.split())

    assert policy["max_status_queries_without_mailbox_change"] == 0
    assert "With no new state, the permitted\n   status-query count is zero" in text
    assert "Never invent or inject a human hot-join message" in text
    assert "never authorizes an advisor request" in text
    assert "do not open Chrome" in text
    assert "Only the repository owner may initiate" in text
    assert "one multi-tool response" in text
    assert "Under `owner_gated`, either threshold stops **all** new" in flattened
    assert (
        "including `wait_agent`, `list_agents`, `send_message`, and `spawn_agent`"
        in flattened
    )
    assert "Under `disabled_by_owner`, those thresholds are telemetry only" in flattened
    assert "It is never\n   `waiting_cost_gate`" in text
    assert "continue or end locally without another poll" in flattened
    assert '"status": "running|completed|liveness_stopped|waiting_cost_gate"' in text
    assert "waiting_cost_gate" in text
    assert "generation_yield" in text
    assert "Built-in collaboration calls still bypass repository code" in flattened
    assert "refuses a later cohort" in flattened
    assert "Legacy\nmode retains instruction-only admission" in text
    assert "cadence-disabled legacy run" in text
    assert "return unverified without" in text


def test_late_advisor_checkpoint_is_bounded_event_driven_and_owner_only() -> None:
    policy, skill = _advisor_policy()
    agents = AGENTS.read_text(encoding="utf-8")

    assert policy == {
        "policy_id": "rethlas_advisor_checkpoint_v1",
        "allowed_triggers": [
            "isolated_load_bearing_gap_two_independent_failures",
            "three_route_round_shared_failure_synthesis",
            "all_current_branches_terminal_blocked_or_dead_end",
            "all_remaining_routes_evidence_backed_near_exhaustion",
        ],
        "requires_failure_synthesis": True,
        "isolated_gap_min_independent_failed_paths": 2,
        "isolated_gap_requires_exact_target_claim": True,
        "isolated_gap_requires_boundary_checks": True,
        "isolated_gap_requires_no_scheduled_local_repair": True,
        "isolated_gap_requires_no_live_subagents": True,
        "gap_response_is_untrusted_targeted_delta": True,
        "gap_response_must_not_mutate_route_council_packet": True,
        "gap_response_reaudits_only_repair_cone": True,
        "near_exhaustion_requires_no_live_subagents": True,
        "near_exhaustion_requires_no_scheduled_next_action": True,
        "near_exhaustion_requires_obstruction_record_per_remaining_route": True,
        "cost_gate_alone_may_trigger": False,
        "automatic_broker_prepare": False,
        "automatic_browser_dispatch": False,
        "automatic_followup": False,
        "prompt_must_synthesize_current_state": True,
        "source_context_sha256_required": True,
        "continuation_requires_new_request_and_exact_owner_authorization": True,
        "max_verified_fact_or_proof_ids": 12,
        "max_failed_path_record_ids": 12,
        "max_bottleneck_utf8_bytes": 2000,
        "max_recommended_question_utf8_bytes": 4000,
        "max_checkpoint_utf8_bytes": 16384,
        "deduplicate_until_new_math_or_advisor_receipt": True,
        "enforcement_scope": "instruction_and_runner_integrity_not_runtime_mediated",
    }
    for text in (skill, agents):
        assert "waiting_owner_advisor_decision" in text
        assert "generation_yield" in text
        assert "browser_dispatch_authorized=false" in text
        assert "advisor_request_id=null" in text
        assert "cost gate" in text.casefold()
        assert "never" in text.casefold()
    assert "end locally without polling the owner" in skill
    assert "accepted or rejected" in skill
    assert "Only the owner decides" in agents
    for field_name in (
        "verified_fact_or_proof_ids",
        "failed_path_record_ids",
        "central_bottleneck",
        "source_context_sha256",
        "recommended_exact_question",
        "owner_action_required",
    ):
        assert f'"{field_name}"' in skill
    assert "never\ncopied from a fixed generic prompt" in skill
    assert "accepted/rejected parts of the\nprior report" in skill
    assert "new request id" in skill
    assert "same ChatGPT conversation" in skill
    assert "start a paid turn" in skill
    assert "hot-join owner-yield surface" in skill
    assert "do not write\n`waiting_owner_advisor_decision`" in skill


def _checkpoint_policy_replay(
    policy: dict[str, object],
    *,
    trigger: str,
    shared_failure_synthesis: bool,
    live_subagents: int = 0,
    scheduled_next_actions: int = 0,
    remaining_routes: tuple[str, ...] = (),
    obstruction_record_routes: tuple[str, ...] = (),
    subjective_stuck_only: bool = False,
    timeout_or_cost_only: bool = False,
    route_set_recorded: bool = False,
    three_solver_reports_recorded: bool = False,
    failure_synthesis_recorded: bool = False,
    independent_failed_paths: int = 0,
    exact_target_claim: bool = False,
    boundary_checks: tuple[str, ...] = (),
    scheduled_local_repairs: int = 0,
) -> bool:
    if trigger not in policy["allowed_triggers"]:
        return False
    if not shared_failure_synthesis or subjective_stuck_only or timeout_or_cost_only:
        return False
    if trigger == "all_current_branches_terminal_blocked_or_dead_end":
        return not remaining_routes and live_subagents == 0
    if trigger == "isolated_load_bearing_gap_two_independent_failures":
        return (
            live_subagents == 0
            and scheduled_local_repairs == 0
            and independent_failed_paths
            >= int(policy["isolated_gap_min_independent_failed_paths"])
            and exact_target_claim
            and bool(boundary_checks)
            and failure_synthesis_recorded
        )
    if trigger == "three_route_round_shared_failure_synthesis":
        return (
            live_subagents == 0
            and route_set_recorded
            and three_solver_reports_recorded
            and failure_synthesis_recorded
        )
    return (
        live_subagents == 0
        and scheduled_next_actions == 0
        and bool(remaining_routes)
        and set(remaining_routes) == set(obstruction_record_routes)
    )


def test_isolated_gap_checkpoint_preempts_global_route_exhaustion() -> None:
    policy, skill = _advisor_policy()
    trigger = "isolated_load_bearing_gap_two_independent_failures"
    complete: dict[str, object] = {
        "trigger": trigger,
        "shared_failure_synthesis": True,
        "independent_failed_paths": 2,
        "exact_target_claim": True,
        "boundary_checks": ("Audit I comparable to gamma.",),
        "failure_synthesis_recorded": True,
        # Other routes may exist; global exhaustion is intentionally absent.
        "remaining_routes": ("unrelated-route",),
    }
    assert _checkpoint_policy_replay(policy, **complete)  # type: ignore[arg-type]
    for overrides in (
        {"independent_failed_paths": 1},
        {"exact_target_claim": False},
        {"boundary_checks": ()},
        {"failure_synthesis_recorded": False},
        {"scheduled_local_repairs": 1},
        {"live_subagents": 1},
        {"timeout_or_cost_only": True},
    ):
        case = dict(complete)
        case.update(overrides)
        assert not _checkpoint_policy_replay(  # type: ignore[arg-type]
            policy, **case
        )
    assert "before global route exhaustion" in skill
    assert "prepare_pro_gap_query" in skill
    assert "complete_unverified_gap_delta" in skill
    assert "Do not restart route council" in skill


def test_late_advisor_checkpoint_near_exhaustion_policy_replay() -> None:
    policy, _skill = _advisor_policy()
    near = "all_remaining_routes_evidence_backed_near_exhaustion"
    routes = ("route-a", "route-b")
    assert _checkpoint_policy_replay(
        policy,
        trigger=near,
        shared_failure_synthesis=True,
        remaining_routes=routes,
        obstruction_record_routes=routes,
    )
    negative_cases = (
        {"live_subagents": 1},
        {"scheduled_next_actions": 1},
        {"obstruction_record_routes": ("route-a",)},
        {"shared_failure_synthesis": False},
        {"subjective_stuck_only": True},
        {"timeout_or_cost_only": True},
    )
    for overrides in negative_cases:
        arguments: dict[str, object] = {
            "trigger": near,
            "shared_failure_synthesis": True,
            "remaining_routes": routes,
            "obstruction_record_routes": routes,
        }
        arguments.update(overrides)
        assert not _checkpoint_policy_replay(policy, **arguments)  # type: ignore[arg-type]


def test_advisor_checkpoint_requires_complete_three_route_failure_evidence() -> (
    None
):
    policy, skill = _advisor_policy()
    complete: dict[str, object] = {
        "trigger": "three_route_round_shared_failure_synthesis",
        "shared_failure_synthesis": True,
        "route_set_recorded": True,
        "three_solver_reports_recorded": True,
        "failure_synthesis_recorded": True,
    }
    assert _checkpoint_policy_replay(policy, **complete)  # type: ignore[arg-type]
    for missing in (
        "route_set_recorded",
        "three_solver_reports_recorded",
        "failure_synthesis_recorded",
    ):
        case = dict(complete)
        case[missing] = False
        assert not _checkpoint_policy_replay(policy, **case)  # type: ignore[arg-type]
    with_live_agent = dict(complete)
    with_live_agent["live_subagents"] = 1
    assert not _checkpoint_policy_replay(  # type: ignore[arg-type]
        policy, **with_live_agent
    )
    assert "one exact three-route round" in " ".join(skill.split())


def test_three_route_policy_fixes_safe_fanout_and_preempts_wait_all() -> None:
    policy, skill = _fanout_policy()
    assert policy == {
        "policy_id": "rethlas_three_route_fanout_v1",
        "root_role": "orchestrator_and_canonical_memory_writer",
        "exact_plan_count": 3,
        "initial_subagent_roles": [
            "route_solver_1",
            "route_solver_2",
            "route_solver_3",
        ],
        "initial_subagent_count": 3,
        "max_live_subagents": 3,
        "fork_turns": "none",
        "spawn_in_one_fanout": True,
        "subagents_may_spawn": False,
        "subagents_write_shared_memory": False,
        "max_report_utf8_bytes": 16384,
        "candidate_preempts_wait_all": True,
        "root_may_run_fourth_proof_route": False,
        "next_round_requires_terminal_synthesis": True,
        "next_round_requires_host_finish_receipt": True,
        "root_is_canonical_memory_writer": True,
        "enforcement_scope": (
            "instruction_contract_tests_and_continuous_host_admission"
        ),
    }
    assert "Spawn exactly one solver for each of the three plan ids" in skill
    assert "context-free forks (`fork_turns=\"none\"`)" in skill
    assert "may not pursue a fourth proof route" in skill
    assert "may not switch to another assigned" in skill
    assert "spawn an agent" in skill
    assert "write shared memory" in skill
    assert "exceed three live proof children" in skill
    assert "preempts wait-all" in skill
    assert "memory_append_batch" in skill
    assert "rethlas_route_terminal_report_v1" in skill
    assert "continuous_round_status" in skill
    assert "exact native terminal response" in skill
    assert "continuous_round_finish" in skill


def test_recursive_wait_contract_is_integrity_bound_by_runner_and_agents() -> None:
    policy, _text = _policy()
    agents = AGENTS.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")

    assert str(policy["policy_id"]) in agents
    assert "600,000 ms" in agents
    assert "Repeated 60-second polling is forbidden" in agents
    assert "only the repository owner decides" in agents
    assert 'root / "AGENTS.md"' in runner
    for binding in (
        '(root / ".codex", Path(".codex"))',
        '(root / ".agents", Path(".agents"))',
        '(root / "mcp", Path("mcp"))',
        '(review_root, Path("review"))',
    ):
        assert binding in runner
    assert '(runner_source, Path("tests/run_hotjoin.sh"))' in runner


def test_executable_policy_replay_backs_off_resets_and_stops_exactly() -> None:
    policy, _text = _policy()
    timeout_state = _PolicyReplay(policy, ("plan-a",))
    observed_waits = []
    for _ in range(4):
        observed_waits.append(timeout_state.wait_timeout())
        timeout_state.record_collaboration_result("timeout_no_progress")

    assert observed_waits == [600_000, 1_200_000, 2_400_000, 3_600_000]
    assert timeout_state.status == "waiting_cost_gate"
    assert timeout_state.cost_gate_reason == "max_consecutive_no_progress_timeouts"
    assert timeout_state.unclassified_plan_ids() == {"plan-a"}
    with pytest.raises(RuntimeError, match="cannot issue another wait"):
        timeout_state.wait_timeout()

    reset_state = _PolicyReplay(policy, ("plan-a",))
    reset_state.record_collaboration_result("timeout_no_progress")
    assert reset_state.wait_timeout() == 1_200_000
    assert reset_state.no_progress_timeouts == 1
    reset_state.record_collaboration_result("mailbox_progress")
    assert reset_state.wait_timeout() == 600_000
    assert reset_state.no_progress_timeouts == 0

    resumption_state = _PolicyReplay(policy, ("done", "unfinished"))
    resumption_state.successful_plan_ids.add("done")
    resumption_state.record_collaboration_result("spawn_fanout_result")
    for _ in range(14):
        resumption_state.record_collaboration_result("mailbox_progress")
    assert resumption_state.status == "running"
    resumption_state.record_collaboration_result("followup_fanout_result")
    assert resumption_state.orchestration_resumptions == 16
    assert resumption_state.status == "waiting_cost_gate"
    assert resumption_state.cost_gate_reason == "max_orchestration_resumptions"
    assert resumption_state.unclassified_plan_ids() == {"unfinished"}
    assert "unfinished" not in resumption_state.successful_plan_ids
    assert "unfinished" not in resumption_state.failed_plan_ids

    token_state = _PolicyReplay(policy, ("plan-a",))
    token_state.record_collaboration_result(
        "spawn_fanout_result", observed_input_tokens=2_999_999
    )
    assert token_state.status == "running"
    token_state.record_collaboration_result(
        "followup_fanout_result", observed_input_tokens=1
    )
    assert token_state.status == "waiting_cost_gate"
    assert token_state.cost_gate_reason == "max_observed_orchestration_input_tokens"


def test_45_minute_incident_replay_uses_conservative_resumption_proxy() -> None:
    policy, _text = _policy()
    old_resumptions = 49
    old_reported_tokens = 9_052_168
    old_reported_input_tokens = 9_049_038
    old_average_input_tokens = old_reported_input_tokens / old_resumptions

    # Mailbox progress resets the timeout, so elapsed time alone cannot recover
    # an exact wake/timeout ordering. Use the policy's unconditional resumption
    # gate as the conservative bound and include the spawn-fanout resumption.
    bounded_resumptions = int(policy["max_orchestration_resumptions"])
    incident_average_projection = bounded_resumptions * old_average_input_tokens

    assert bounded_resumptions == 16
    assert bounded_resumptions < old_resumptions
    assert incident_average_projection < int(
        policy["max_observed_orchestration_input_tokens"]
    )
    assert incident_average_projection < old_reported_tokens / 3

    # This arithmetic is evidence for the observed incident, not a universal
    # cost ceiling. If orchestration-only usage is unavailable and future root
    # contexts average 200k input tokens, the 16-resumption proxy can exceed 3M.
    assert bounded_resumptions * 200_000 > int(
        policy["max_observed_orchestration_input_tokens"]
    )
