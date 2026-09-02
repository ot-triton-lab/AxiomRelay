from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from agents import continuous_supervisor as supervisor
from agents import hotjoin_adapter as hotjoin


def _summary(
    lane: str,
    *,
    summary_state: str = "terminal_reported",
    candidate_status: str = "none",
    report_text: str | None = "bounded route summary",
) -> dict[str, object]:
    return {
        "schema_version": supervisor.LANE_SUMMARY_SCHEMA,
        "lane_id": lane,
        "thread_id": f"thread-{lane}",
        "route_id": f"route-{lane}",
        "assigned_bridge": f"bridge for {lane}",
        "summary_state": summary_state,
        "candidate_status": candidate_status,
        "proved_claim_evidence_ids": [f"proof:{lane}"],
        "failed_path_evidence_ids": [f"failure:{lane}"],
        "counterexample_evidence_ids": [],
        "remaining_obligation": f"obligation for {lane}",
        "best_next_test": f"test for {lane}",
        "report_text": report_text,
        "report_sha256": (
            hashlib.sha256(report_text.encode()).hexdigest()
            if report_text is not None
            else None
        ),
    }


def _snapshot(*summaries: dict[str, object]) -> dict[str, object]:
    return supervisor.build_cohort_snapshot(
        review_id="cohortreview_" + "a" * 32,
        review_ordinal=1,
        due_at_epoch=4_600.0,
        root_thread_id="thread-root",
        root_turn_id="turn-root",
        problem_id="problem-1",
        statement_sha256="b" * 64,
        lane_summaries=list(summaries),
    )


def _decision(
    summary: dict[str, object],
    *,
    verdict: str,
    action: str,
    redirect_to_lane_id: str | None = None,
) -> dict[str, object]:
    continuing = action in {"resume_same", "restart_same", "redirect"}
    return {
        "lane_id": summary["lane_id"],
        "thread_id": summary["thread_id"],
        "route_id": summary["route_id"],
        "verdict": verdict,
        "next_action": action,
        "redirect_to_lane_id": redirect_to_lane_id,
        "reason": f"review reason for {summary['lane_id']}",
        "fatal_doubt": "one fatal doubt" if verdict == "yellow" else None,
        "next_test": "one exact next test" if continuing else None,
        "accepted_evidence_ids": [summary["proved_claim_evidence_ids"][0]],
    }


def _report(
    snapshot: dict[str, object], decisions: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema_version": supervisor.COHORT_REPORT_SCHEMA,
        "review_id": snapshot["review_id"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "lane_decisions": decisions,
        "cross_lane_synthesis": "bounded comparison across all admitted lanes",
    }


def _admission(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "operationally_blocked": False,
        "stale_turn_interrupt_required": False,
        "pending_terminal": False,
        "cycle_state": "active",
        "cycle_close_disposition": None,
        "owner_yield_prepared": False,
        "valid_fresh_epoch": False,
        "pending_root_intent": False,
        "supervisor_state": "active",
        "root_active": True,
        "uncertain_intent_count": 0,
        "review_root_notice_state": None,
        "review_state": None,
        "review_decision_bound": False,
    }
    values.update(overrides)
    return supervisor.derive_runtime_admission(**values)  # type: ignore[arg-type]


def test_policy_is_rolling_and_never_time_interrupts_root() -> None:
    contract = supervisor.policy_contract()
    assert contract["review_interval_seconds"] == 3_600
    assert contract["renewal_interval_seconds"] == 9_000
    assert contract["review_clock_resets_on_renewal"] is False
    assert contract["renewal_is_terminal"] is False
    assert contract["root_review_interrupt"] is False
    assert contract["root_renewal_interrupt"] is False
    assert contract["root_physical_turn_auto_continuation"] is False
    assert contract["cause_bound_root_successors"] is True
    assert contract["summary_terminal_state"] == "parked"
    assert contract["policy_sha256"] == supervisor.content_sha256(
        {key: value for key, value in contract.items() if key != "policy_sha256"}
    )
    assert all(
        hotjoin.CONTINUOUS_SUPERVISOR_POLICY.get(key) == value
        for key, value in supervisor.POLICY.items()
    )


def test_resource_policy_is_small_content_bound_and_root_protecting() -> None:
    contract = supervisor.resource_policy_contract()
    assert contract["rule"] == (
        "preserve_progress_and_require_delta_before_reallocation"
    )
    assert contract["default_lane_action"] == "resume_same"
    assert contract["restart_requires_host_evidence"] is True
    assert contract["root_time_interrupt_allowed"] is False
    assert contract["paid_root_during_host_review"] is False
    assert contract["owner_messages_deferred_during_host_review"] is True
    assert contract["next_cohort_requires_round_finish_or_review_restart"] is True
    assert contract["round_finish_requires_three_reports_and_synthesis"] is True
    assert contract["stop_unsolved_has_no_paid_successor"] is True
    assert contract["root_successor_requires_durable_cause"] is True
    assert contract["one_paid_turn_per_durable_intent"] is True
    assert contract["policy_sha256"] == supervisor.content_sha256(
        {key: value for key, value in contract.items() if key != "policy_sha256"}
    )


@pytest.mark.parametrize(
    ("internal", "review", "phase", "outcome"),
    [
        ("active", None, "running", None),
        ("advisor_checkpoint_required", None, "running", None),
        ("review_collecting", "collecting", "reviewing", None),
        ("review_running", "running", "reviewing", None),
        ("review_applying", "completed", "reviewing", None),
        ("candidate_verification", None, "verifying", None),
        ("paused_owner", None, "waiting_owner", None),
        (
            "waiting_owner_advisor_decision",
            None,
            "waiting_owner",
            None,
        ),
        ("completed", None, "terminal", "completed"),
        ("stopped_unsolved", None, "terminal", "blocked"),
        ("operational_blocked", None, "terminal", "blocked"),
    ],
)
def test_public_state_projection_hides_recovery_detail_without_losing_outcome(
    internal: str,
    review: str | None,
    phase: str,
    outcome: str | None,
) -> None:
    projection = supervisor.project_public_state(internal, review_state=review)
    assert set(projection) == {
        "schema_version",
        "phase",
        "outcome",
        "internal_state",
        "review_state",
    }
    assert projection["phase"] == phase
    assert projection["outcome"] == outcome


def test_public_review_state_requires_its_internal_recovery_state() -> None:
    with pytest.raises(supervisor.ContinuousSupervisorError, match="lacks"):
        supervisor.project_public_state("review_running")


def test_state_machine_graph_is_closed_and_terminal_states_cannot_restart() -> None:
    contract = supervisor.state_machine_contract()
    assert contract["state_machine_sha256"] == supervisor.content_sha256(
        {key: value for key, value in contract.items() if key != "state_machine_sha256"}
    )
    for transitions in supervisor.STATE_MACHINE_TRANSITIONS.values():
        states = set(transitions)
        assert all(
            target in states for targets in transitions.values() for target in targets
        )
    assert supervisor.STATE_MACHINE_TRANSITIONS["supervisor"]["completed"] == ()
    assert (
        supervisor.STATE_MACHINE_TRANSITIONS["supervisor"]["stopped_unsolved"]
        == ()
    )
    assert (
        supervisor.STATE_MACHINE_TRANSITIONS["supervisor"]["operational_blocked"] == ()
    )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="illegal"):
        supervisor.require_state_transition("supervisor", "completed", "active")


def test_event_reducer_covers_every_released_edge_and_is_content_bound() -> None:
    contract = supervisor.state_machine_contract()
    assert contract["transition_reducer"]["schema_version"] == (
        supervisor.TRANSITION_REDUCER_SCHEMA
    )
    for machine, transitions in supervisor.STATE_MACHINE_TRANSITIONS.items():
        graph_edges = {
            (current, target)
            for current, targets in transitions.items()
            for target in targets
        }
        event_edges = {
            (current, target)
            for rule in supervisor.STATE_EVENT_RULES[machine].values()
            for current, target in rule["transitions"].items()
            if current != target
        }
        assert event_edges == graph_edges
        for event, rule in supervisor.STATE_EVENT_RULES[machine].items():
            assert "interrupt_root" not in rule["effects"]
            assert "start_paid_root" not in rule["effects"]
            for current, target in rule["transitions"].items():
                decision = supervisor.reduce_state_event(machine, current, event)
                assert decision["target_state"] == target
                assert decision["effects"] == list(rule["effects"])
                assert decision["decision_sha256"] == supervisor.content_sha256(
                    {
                        key: value
                        for key, value in decision.items()
                        if key != "decision_sha256"
                    }
                )
                assert supervisor.reduce_state_event(machine, current, event) == (
                    decision
                )
    for terminal in ("completed", "stopped_unsolved", "operational_blocked"):
        assert all(
            terminal not in rule["transitions"]
            for rule in supervisor.STATE_EVENT_RULES["supervisor"].values()
        )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="unknown"):
        supervisor.reduce_state_event("supervisor", "active", "invented")
    with pytest.raises(supervisor.ContinuousSupervisorError, match="invalid from"):
        supervisor.reduce_state_event(
            "supervisor", "completed", "verification_published"
        )


def test_adapter_has_no_literal_continuous_state_transition_bypass() -> None:
    source = Path(hotjoin.__file__).read_text(encoding="utf-8")
    assert "_continuous.require_state_transition(" not in source
    assert "continuable_states =" not in source
    assert source.count("_continuous.derive_runtime_admission(") == 1
    tree = ast.parse(source)
    state_writes = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.search(
            r"UPDATE continuous_(?:supervisors|reviews|review_lanes|renewals) SET",
            node.value,
        )
    ]
    literal_targets = [
        statement
        for statement in state_writes
        if re.search(r"(?<!_)\bstate\s*=\s*'", statement)
    ]
    assert literal_targets == []


@pytest.mark.parametrize(
    ("overrides", "disposition", "paid", "resumable"),
    [
        ({}, "continuous_active", False, False),
        ({"root_active": False}, "continuous_root_missing", False, False),
        (
            {
                "supervisor_state": None,
                "root_active": False,
            },
            "initial_start_allowed",
            True,
            True,
        ),
        (
            {
                "cycle_state": "closed",
                "cycle_close_disposition": "continue_next_cycle",
                "root_active": False,
                "valid_fresh_epoch": True,
            },
            "continue_next_cycle",
            True,
            True,
        ),
        (
            {
                "supervisor_state": "review_collecting",
                "root_active": False,
                "review_root_notice_state": "accepted",
                "review_state": "collecting",
            },
            "continuous_review_host_recovery",
            False,
            True,
        ),
        (
            {
                "supervisor_state": "review_applying",
                "root_active": False,
                "review_root_notice_state": "accepted",
                "review_state": "completed",
                "review_decision_bound": True,
            },
            "continuous_verdict_successor_required",
            True,
            True,
        ),
        (
            {
                "root_active": False,
                "pending_root_intent": True,
            },
            "continuous_intent_successor_required",
            True,
            True,
        ),
        (
            {
                "cycle_state": "closed",
                "cycle_close_disposition": "owner_wait_cost",
                "supervisor_state": "paused_owner",
                "root_active": False,
            },
            "owner_wait_cost",
            False,
            False,
        ),
        (
            {
                "cycle_state": "closed",
                "cycle_close_disposition": "verified",
                "supervisor_state": "completed",
                "root_active": False,
            },
            "completed",
            False,
            False,
        ),
        (
            {
                "cycle_state": "closed",
                "cycle_close_disposition": "stop_unsolved",
                "supervisor_state": "stopped_unsolved",
                "root_active": False,
            },
            "stop_unsolved",
            False,
            False,
        ),
        (
            {"pending_terminal": True},
            "terminal_observed_pending_finalization",
            False,
            True,
        ),
    ],
)
def test_runtime_admission_has_one_paid_root_allowlist(
    overrides: dict[str, object],
    disposition: str,
    paid: bool,
    resumable: bool,
) -> None:
    decision = _admission(**overrides)
    assert decision["disposition"] == disposition
    assert decision["paid_turn_allowed"] is paid
    assert decision["adapter_resume_allowed"] is resumable
    assert ("start_paid_root" in decision["authorized_effects"]) is paid
    assert decision["decision_sha256"] == supervisor.content_sha256(
        {
            key: value
            for key, value in decision.items()
            if key != "decision_sha256"
        }
    )


def test_runtime_admission_fails_closed_and_rejects_incomplete_review_state() -> None:
    blocked = _admission(
        operationally_blocked=True,
        pending_terminal=True,
        supervisor_state="review_applying",
        root_active=False,
        review_root_notice_state="accepted",
        review_state="completed",
        review_decision_bound=True,
    )
    assert blocked["disposition"] == "operational_blocked"
    assert blocked["paid_turn_allowed"] is False
    assert blocked["authorized_effects"] == []

    with pytest.raises(supervisor.ContinuousSupervisorError, match="lacks its review"):
        _admission(supervisor_state="review_running", root_active=False)
    with pytest.raises(supervisor.ContinuousSupervisorError, match="decision lacks"):
        _admission(
            supervisor_state="review_applying",
            root_active=False,
            review_root_notice_state="accepted",
            review_state="running",
            review_decision_bound=True,
        )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="verified cycle"):
        _admission(supervisor_state="completed", root_active=False)
    with pytest.raises(supervisor.ContinuousSupervisorError, match="unsolved stop"):
        _admission(supervisor_state="stopped_unsolved", root_active=False)
    with pytest.raises(supervisor.ContinuousSupervisorError, match="root intent"):
        _admission(
            supervisor_state="review_running",
            root_active=False,
            pending_root_intent=True,
            review_root_notice_state="accepted",
            review_state="running",
        )


@pytest.mark.parametrize(
    ("overrides", "cause"),
    [
        ({}, None),
        ({"frontier_delta": True}, "frontier_delta"),
        (
            {"open_lane_count": 2, "frontier_delta": True},
            None,
        ),
        (
            {"open_lane_count": 2, "lane_terminal_delta": True},
            "lane_terminal",
        ),
        (
            {"round_finish_action": "new_cohort"},
            "new_cohort",
        ),
        (
            {
                "supervisor_state": "advisor_checkpoint_required",
                "round_finish_action": "advisor_checkpoint",
            },
            "advisor_checkpoint",
        ),
        (
            {
                "supervisor_state": "candidate_verification",
                "frontier_delta": True,
            },
            "candidate_repair",
        ),
        ({"pending_context_rollover": True}, "context_rollover"),
        ({"open_lane_count": 2, "review_due": True}, "review_due"),
        ({"owner_yield_prepared": True, "frontier_delta": True}, None),
        ({"completion_visible": True, "frontier_delta": True}, None),
        ({"review_host_can_progress": True, "frontier_delta": True}, None),
        (
            {"supervisor_state": "stopped_unsolved", "frontier_delta": True},
            None,
        ),
    ],
)
def test_root_successor_requires_one_durable_cause(
    overrides: dict[str, object], cause: str | None
) -> None:
    values: dict[str, object] = {
        "supervisor_state": "active",
        "open_lane_count": 0,
        "lane_terminal_delta": False,
        "frontier_delta": False,
        "pending_context_rollover": False,
        "review_due": False,
        "round_finish_action": None,
        "owner_yield_prepared": False,
        "completion_visible": False,
        "review_host_can_progress": False,
    }
    values.update(overrides)
    decision = supervisor.derive_root_successor_decision(**values)  # type: ignore[arg-type]
    assert decision["cause_kind"] == cause
    assert decision["create_intent"] is (cause is not None)
    assert decision["effects"] == (["create_root_intent"] if cause else [])
    assert decision["decision_sha256"] == supervisor.content_sha256(
        {
            key: value
            for key, value in decision.items()
            if key != "decision_sha256"
        }
    )


def test_transition_reducer_survives_long_review_and_owner_wait_sequences() -> None:
    state = "active"
    for ordinal in range(1, 501):
        state = supervisor.reduce_state_event(
            "supervisor", state, "review_frozen"
        )["target_state"]
        assert state == "review_collecting"
        review_state = "collecting"
        if ordinal % 2 == 0:
            review_state = supervisor.reduce_state_event(
                "review", review_state, "summary_deadline_reached"
            )["target_state"]
        review_state = supervisor.reduce_state_event(
            "review", review_state, "review_snapshot_sealed"
        )["target_state"]
        state = supervisor.reduce_state_event(
            "supervisor", state, "review_snapshot_sealed"
        )["target_state"]
        review_state = supervisor.reduce_state_event(
            "review", review_state, "reviewer_dispatched"
        )["target_state"]
        review_state = supervisor.reduce_state_event(
            "review", review_state, "reviewer_result_recorded"
        )["target_state"]
        state = supervisor.reduce_state_event(
            "supervisor", state, "reviewer_result_recorded"
        )["target_state"]
        review_state = supervisor.reduce_state_event(
            "review", review_state, "review_verdict_applied"
        )["target_state"]
        state = supervisor.reduce_state_event(
            "supervisor", state, "review_verdict_continue"
        )["target_state"]
        assert review_state == "applied"
        assert state == "active"
        if ordinal % 25 == 0:
            state = supervisor.reduce_state_event(
                "supervisor", state, "owner_cost_wait_closed"
            )["target_state"]
            assert state == "paused_owner"
            state = supervisor.reduce_state_event(
                "supervisor", state, "owner_wait_resumed"
            )["target_state"]
            assert state == "active"

    state = supervisor.reduce_state_event(
        "supervisor", state, "candidate_detected"
    )["target_state"]
    state = supervisor.reduce_state_event(
        "supervisor", state, "verification_published"
    )["target_state"]
    assert state == "completed"
    with pytest.raises(supervisor.ContinuousSupervisorError):
        supervisor.reduce_state_event("supervisor", state, "review_frozen")


def test_review_and_renewal_clocks_never_reset_each_other() -> None:
    origin = 1_000.0
    assert [supervisor.review_due_at(origin, index) for index in range(1, 6)] == [
        4_600.0,
        8_200.0,
        11_800.0,
        15_400.0,
        19_000.0,
    ]
    assert [supervisor.renewal_due_at(origin, index) for index in range(1, 3)] == [
        10_000.0,
        19_000.0,
    ]
    assert supervisor.next_review_ordinal(origin, 10_001.0) == 3
    assert supervisor.next_renewal_ordinal(origin, 10_001.0) == 2


@pytest.mark.parametrize("ordinal", [0, -1, True])
def test_clocks_reject_nonpositive_or_boolean_ordinals(ordinal: object) -> None:
    with pytest.raises(supervisor.ContinuousSupervisorError):
        supervisor.review_due_at(1_000.0, ordinal)  # type: ignore[arg-type]
    with pytest.raises(supervisor.ContinuousSupervisorError):
        supervisor.renewal_due_at(1_000.0, ordinal)  # type: ignore[arg-type]


def test_lane_summary_is_content_bound_and_complete_candidate_is_terminal() -> None:
    complete = _summary("a", candidate_status="complete")
    normalized = supervisor.validate_lane_summary(complete)
    assert normalized["report_sha256"] == complete["report_sha256"]

    complete["summary_state"] = "interrupted_partial"
    with pytest.raises(
        supervisor.ContinuousSupervisorError,
        match="complete candidate requires a terminal report",
    ):
        supervisor.validate_lane_summary(complete)


def test_missing_report_is_allowed_only_for_failure_or_unavailable() -> None:
    unavailable = _summary("a", summary_state="unavailable", report_text=None)
    assert supervisor.validate_lane_summary(unavailable)["report_text"] is None
    unavailable["summary_state"] = "terminal_reported"
    with pytest.raises(supervisor.ContinuousSupervisorError, match="pairing"):
        supervisor.validate_lane_summary(unavailable)


def test_snapshot_is_order_independent_and_bounded_to_three_lanes() -> None:
    a, b, c, d = (_summary(name) for name in "abcd")
    first = _snapshot(c, a, b)
    second = _snapshot(b, c, a)
    assert first == second
    with pytest.raises(supervisor.ContinuousSupervisorError, match="three lanes"):
        _snapshot(a, b, c, d)


def test_reviewer_request_is_content_bound_and_exactly_once() -> None:
    snapshot = _snapshot(_summary("a"))
    request = supervisor.build_reviewer_request(
        snapshot=snapshot,
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        policy_sha256="f" * 64,
    )
    assert request["attempt"] == 1
    assert request["retry_allowed"] is False
    assert request["snapshot_sha256"] == snapshot["snapshot_sha256"]
    seed = {key: value for key, value in request.items() if key != "request_sha256"}
    assert request["request_sha256"] == supervisor.content_sha256(seed)
    snapshot["lane_summaries"][0]["assigned_bridge"] = "tampered"
    with pytest.raises(supervisor.ContinuousSupervisorError, match="digest mismatch"):
        supervisor.build_reviewer_request(
            snapshot=snapshot,
            expected_model="gpt-5.6-sol",
            reasoning_effort="max",
            policy_sha256="f" * 64,
        )


def test_green_lanes_continue_and_red_lanes_free_slots() -> None:
    a, b, c = (_summary(name) for name in "abc")
    snapshot = _snapshot(a, b, c)
    report = _report(
        snapshot,
        [
            _decision(a, verdict="green", action="resume_same"),
            _decision(
                b,
                verdict="red",
                action="redirect",
                redirect_to_lane_id=str(a["lane_id"]),
            ),
            _decision(c, verdict="red", action="retire"),
        ],
    )
    decision = supervisor.derive_global_action(report, snapshot=snapshot)
    assert decision["global_action"] == "continue_cohort"
    assert [item["next_action"] for item in decision["lane_actions"]] == [
        "resume_same",
        "redirect",
        "retire",
    ]


def test_host_reuses_parked_context_instead_of_restarting_same_route() -> None:
    summary = _summary("a")
    snapshot = _snapshot(summary)
    report = _report(
        snapshot,
        [_decision(summary, verdict="yellow", action="restart_same")],
    )
    decision = supervisor.derive_global_action(
        report,
        snapshot=snapshot,
        resume_eligible_lane_ids=[str(summary["lane_id"])],
    )
    assert report["lane_decisions"][0]["next_action"] == "restart_same"
    assert decision["lane_actions"][0]["next_action"] == "resume_same"
    assert decision["global_action"] == "continue_cohort"


def test_resource_guard_reuses_parked_context_and_preserves_reviewer_report() -> None:
    summary = _summary("a")
    snapshot = _snapshot(summary)
    report = _report(
        snapshot,
        [_decision(summary, verdict="yellow", action="restart_same")],
    )
    result = supervisor.derive_resource_conserving_action(
        report,
        snapshot=snapshot,
        resume_eligible_lane_ids=[str(summary["lane_id"])],
    )
    action = result["decision"]["lane_actions"][0]
    audit = result["resource_audit"]
    assert report["lane_decisions"][0]["next_action"] == "restart_same"
    assert action["next_action"] == "resume_same"
    assert audit["lane_actions"][0]["basis"] == "reusable_parked_context"
    assert audit["audit_sha256"] == supervisor.content_sha256(
        {key: value for key, value in audit.items() if key != "audit_sha256"}
    )


def test_resource_guard_restarts_only_for_host_observed_context_loss() -> None:
    summary = _summary("a")
    snapshot = _snapshot(summary)
    report = _report(
        snapshot,
        [_decision(summary, verdict="green", action="resume_same")],
    )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="restart evidence"):
        supervisor.derive_resource_conserving_action(report, snapshot=snapshot)

    result = supervisor.derive_resource_conserving_action(
        report,
        snapshot=snapshot,
        restart_authorizations={
            str(summary["lane_id"]): "root_thread_epoch_changed"
        },
    )
    assert result["decision"]["lane_actions"][0]["next_action"] == "restart_same"
    assert result["resource_audit"]["lane_actions"][0]["basis"] == (
        "root_thread_epoch_changed"
    )


def test_resource_guard_rejects_unused_or_model_invented_restart_authority() -> None:
    summary = _summary("a")
    snapshot = _snapshot(summary)
    retired = _report(
        snapshot,
        [_decision(summary, verdict="red", action="retire")],
    )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="non-continuing"):
        supervisor.derive_resource_conserving_action(
            retired,
            snapshot=snapshot,
            restart_authorizations={
                str(summary["lane_id"]): "child_context_unavailable"
            },
        )
    continuing = _report(
        snapshot,
        [_decision(summary, verdict="yellow", action="restart_same")],
    )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="reason"):
        supervisor.derive_resource_conserving_action(
            continuing,
            snapshot=snapshot,
            restart_authorizations={str(summary["lane_id"]): "reviewer_requested"},
        )


def test_summary_interrupt_selection_requires_deadline_active_child_and_no_report() -> None:
    lanes = [
        {
            "lane_id": "lane-a",
            "thread_id": "thread-a",
            "state": "summary_requested",
            "observed_status": "active",
            "active_turn_id": "turn-a",
            "summary_present": False,
        },
        {
            "lane_id": "lane-b",
            "thread_id": "thread-b",
            "state": "summary_requested",
            "observed_status": "idle",
            "active_turn_id": "turn-b",
            "summary_present": False,
        },
        {
            "lane_id": "lane-c",
            "thread_id": "thread-c",
            "state": "parked",
            "observed_status": "idle",
            "active_turn_id": None,
            "summary_present": True,
        },
    ]
    assert supervisor.select_summary_interrupt_targets(
        lanes, deadline_reached=False, root_thread_id="thread-root"
    ) == []
    assert supervisor.select_summary_interrupt_targets(
        lanes, deadline_reached=True, root_thread_id="thread-root"
    ) == [
        {"lane_id": "lane-a", "thread_id": "thread-a", "turn_id": "turn-a"}
    ]


def test_summary_interrupt_selection_rejects_the_root_even_if_misclassified() -> None:
    with pytest.raises(supervisor.ContinuousSupervisorError, match="protected root"):
        supervisor.select_summary_interrupt_targets(
            [
                {
                    "lane_id": "lane-root",
                    "thread_id": "thread-root",
                    "state": "summary_requested",
                    "observed_status": "active",
                    "active_turn_id": "turn-root",
                    "summary_present": False,
                }
            ],
            deadline_reached=True,
            root_thread_id="thread-root",
        )


def test_all_mathematically_red_lanes_seek_advisor() -> None:
    a, b = (_summary(name) for name in "ab")
    snapshot = _snapshot(a, b)
    report = _report(
        snapshot,
        [
            _decision(a, verdict="red", action="retire"),
            _decision(b, verdict="red", action="retire"),
        ],
    )
    assert (
        supervisor.derive_global_action(report, snapshot=snapshot)["global_action"]
        == "seek_advisor"
    )


def test_all_unclear_retirements_are_operationally_blocked_not_advisor() -> None:
    a, b = (_summary(name) for name in "ab")
    snapshot = _snapshot(a, b)
    report = _report(
        snapshot,
        [
            _decision(a, verdict="unclear", action="retire"),
            _decision(b, verdict="unclear", action="retire"),
        ],
    )
    assert (
        supervisor.derive_global_action(report, snapshot=snapshot)["global_action"]
        == "operational_blocked"
    )


def test_complete_candidate_preempts_other_lane_actions() -> None:
    a = _summary("a", candidate_status="complete")
    b = _summary("b")
    snapshot = _snapshot(a, b)
    report = _report(
        snapshot,
        [
            _decision(a, verdict="green", action="verify"),
            _decision(b, verdict="unclear", action="retire"),
        ],
    )
    assert (
        supervisor.derive_global_action(report, snapshot=snapshot)["global_action"]
        == "verify_candidate"
    )


def test_yellow_requires_exact_fatal_doubt_and_next_test() -> None:
    summary = _summary("a")
    decision = _decision(summary, verdict="yellow", action="resume_same")
    decision["fatal_doubt"] = None
    with pytest.raises(supervisor.ContinuousSupervisorError, match="yellow requires"):
        supervisor.validate_lane_decision(decision, summary=summary)


def test_reviewer_cannot_accept_unknown_evidence() -> None:
    summary = _summary("a")
    decision = _decision(summary, verdict="green", action="resume_same")
    decision["accepted_evidence_ids"] = ["invented:evidence"]
    with pytest.raises(supervisor.ContinuousSupervisorError, match="unknown evidence"):
        supervisor.validate_lane_decision(decision, summary=summary)


def test_cli_output_schema_uses_host_validated_duplicate_constraint() -> None:
    encoded = supervisor.canonical_json_bytes(
        supervisor.COHORT_REPORT_JSON_SCHEMA
    ).decode()
    assert "uniqueItems" not in encoded
    assert "anyOf" not in encoded
    assert "allOf" not in encoded
    assert "Every resume_same, restart_same, or redirect action" in (
        supervisor.COHORT_REVIEWER_SYSTEM_PROMPT
    )
    assert supervisor.NO_NEXT_TEST in supervisor.COHORT_REVIEWER_SYSTEM_PROMPT
    item_schema = supervisor.COHORT_REPORT_JSON_SCHEMA["properties"][
        "lane_decisions"
    ]["items"]
    assert item_schema["properties"]["next_test"] == {
        "type": "string",
        "minLength": 1,
    }
    summary = _summary("a")
    decision = _decision(summary, verdict="green", action="resume_same")
    decision["accepted_evidence_ids"] = ["proof:a", "proof:a"]
    with pytest.raises(supervisor.ContinuousSupervisorError, match="duplicates"):
        supervisor.validate_lane_decision(decision, summary=summary)


def test_continuing_action_rejects_no_next_test_sentinel() -> None:
    summary = _summary("a")
    decision = _decision(summary, verdict="green", action="resume_same")
    decision["next_test"] = supervisor.NO_NEXT_TEST
    with pytest.raises(
        supervisor.ContinuousSupervisorError,
        match="concrete next test",
    ):
        supervisor.validate_lane_decision(decision, summary=summary)


def test_report_must_cover_every_lane_exactly_once() -> None:
    a, b = (_summary(name) for name in "ab")
    snapshot = _snapshot(a, b)
    report = _report(snapshot, [_decision(a, verdict="red", action="retire")])
    with pytest.raises(supervisor.ContinuousSupervisorError, match="omitted or added"):
        supervisor.validate_cohort_report(report, snapshot=snapshot)


def test_empty_cohort_is_operationally_blocked_without_route_evidence() -> None:
    snapshot = _snapshot()
    report = _report(snapshot, [])
    assert (
        supervisor.derive_global_action(report, snapshot=snapshot)["global_action"]
        == "operational_blocked"
    )


def test_redirect_must_name_a_different_viable_frozen_lane() -> None:
    a, b = (_summary(name) for name in "ab")
    snapshot = _snapshot(a, b)
    report = _report(
        snapshot,
        [
            _decision(
                a,
                verdict="red",
                action="redirect",
                redirect_to_lane_id=str(b["lane_id"]),
            ),
            _decision(b, verdict="green", action="resume_same"),
        ],
    )
    assert (
        supervisor.derive_global_action(report, snapshot=snapshot)["global_action"]
        == "continue_cohort"
    )
    report["lane_decisions"][0]["redirect_to_lane_id"] = "lane-missing"
    with pytest.raises(supervisor.ContinuousSupervisorError, match="viable lane"):
        supervisor.validate_cohort_report(report, snapshot=snapshot)


def test_unchanged_history_cannot_repeat_green_or_yellow() -> None:
    summary = _summary("a")
    prior_digest = supervisor.lane_summary_content_sha256(summary)
    history = {
        "lane_id": summary["lane_id"],
        "thread_id": summary["thread_id"],
        "prior_review_id": "cohortreview_" + "9" * 32,
        "prior_summary_content_sha256": prior_digest,
        "current_summary_content_sha256": prior_digest,
        "unchanged_from_previous": True,
        "prior_verdict": "yellow",
        "prior_next_action": "resume_same",
        "resume_scope": "same_root_thread_epoch",
    }
    snapshot = supervisor.build_cohort_snapshot(
        review_id="cohortreview_" + "a" * 32,
        review_ordinal=2,
        due_at_epoch=8_200.0,
        root_thread_id="thread-root",
        root_turn_id="turn-root",
        problem_id="problem-1",
        statement_sha256="b" * 64,
        lane_summaries=[summary],
        lane_histories=[history],
    )
    green = _report(
        snapshot, [_decision(summary, verdict="green", action="resume_same")]
    )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="unchanged lane"):
        supervisor.validate_cohort_report(green, snapshot=snapshot)
    yellow = _report(
        snapshot, [_decision(summary, verdict="yellow", action="restart_same")]
    )
    with pytest.raises(supervisor.ContinuousSupervisorError, match="second unchanged"):
        supervisor.validate_cohort_report(yellow, snapshot=snapshot)


def _ledger(tmp_path) -> tuple[hotjoin.ConversationLedger, hotjoin.LeaseToken]:
    ledger = hotjoin.ConversationLedger(tmp_path / "state" / "messages.sqlite3")
    ledger.create_run("run-1", "problem-1")
    lease = ledger.acquire_lease("run-1", "continuous-test")
    ledger.bind_thread("run-1", "thread-root", lease=lease)
    ledger.set_active_turn("run-1", "turn-root", lease=lease)
    return ledger, lease


def test_opt_in_public_status_does_not_change_legacy_cadence_projection(
    tmp_path,
) -> None:
    ledger, lease = _ledger(tmp_path)
    disabled = ledger.continuous_public_status("run-1")
    assert set(disabled) == {
        "schema_version",
        "run_id",
        "enabled",
        "state",
        "resource_policy_sha256",
    }
    assert disabled["enabled"] is False
    assert disabled["state"] is None

    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    running = ledger.continuous_public_status("run-1")
    assert running["state"]["phase"] == "running"
    assert running["state"]["internal_state"] == "active"

    legacy = ledger.cadence_control_state(
        "run-1",
        now_epoch=1_001.0,
        now_monotonic=2_001.0,
        boot_identity="boot-1",
    )
    assert set(legacy) == {
        "context_guard",
        "continuous_supervisor",
        "disposition",
        "paid_turn_allowed",
        "quarantine",
        "review_cadence",
        "run_id",
        "thread_epoch",
    }


def test_ledger_starts_global_clocks_and_freezes_due_cohort(tmp_path) -> None:
    ledger, lease = _ledger(tmp_path)
    state = ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    assert state["next_review_wall_epoch"] == 4_600.0
    assert state["next_renewal_wall_epoch"] == 10_000.0

    before = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_599.0,
        now_monotonic=5_599.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[],
        lease=lease,
    )
    assert before == {"review": None, "renewal": None}
    due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_600.0,
        now_monotonic=5_600.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[
            {
                "thread_id": "thread-child",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "active",
                "active_turn_id": "turn-child",
            }
        ],
        lease=lease,
    )
    assert due["renewal"] is None
    assert due["review"]["review_ordinal"] == 1
    assert "will not time-interrupt the root" in due["review"]["root_notice"]
    projection = ledger.continuous_supervisor_state("run-1")
    assert projection is not None
    assert projection["state"] == "review_collecting"
    assert projection["current_review"]["lanes"][0]["state"] == ("summary_requested")


def test_undelivered_review_notice_retargets_clean_root_successor(tmp_path) -> None:
    ledger, lease = _ledger(tmp_path)
    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    lane = {
        "thread_id": "thread-lane",
        "parent_thread_id": "thread-root",
        "session_id": "thread-root",
        "proof_lane": True,
        "observed_status": "active",
        "active_turn_id": "turn-lane",
    }
    first = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_600.0,
        now_monotonic=5_600.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[lane],
        lease=lease,
    )["review"]
    with ledger._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE runs SET active_turn_id = ? WHERE run_id = ?",
            ("turn-successor", "run-1"),
        )
        connection.commit()
    replay = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_601.0,
        now_monotonic=5_601.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-successor",
        descendants=[lane],
        lease=lease,
    )["review"]
    assert replay["review_id"] == first["review_id"]
    assert replay["root_turn_id"] == "turn-successor"
    ledger.mark_continuous_root_notice_accepted(
        "run-1",
        review_id=first["review_id"],
        accepted_turn_id="turn-successor",
        lease=lease,
    )


def test_renewal_is_durable_and_nonterminal(tmp_path) -> None:
    ledger, lease = _ledger(tmp_path)
    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_600.0,
        now_monotonic=5_600.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[],
        lease=lease,
    )
    due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=10_000.0,
        now_monotonic=11_000.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[],
        lease=lease,
    )
    assert due["renewal"]["state"] == "due"
    projection = ledger.continuous_supervisor_state("run-1")
    assert projection is not None
    assert projection["state"] == "active"
    assert projection["renewals"][0]["state"] == "due"
    replay_due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=10_001.0,
        now_monotonic=11_001.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[],
        lease=lease,
    )
    assert replay_due["renewal"]["renewal_id"] == due["renewal"]["renewal_id"]
    ledger.mark_continuous_renewal_continued(
        "run-1",
        renewal_id=due["renewal"]["renewal_id"],
        accepted_root_turn_id="turn-root",
        lease=lease,
    )
    continued = ledger.continuous_supervisor_state("run-1")
    assert continued["renewals"][0]["state"] == "continued"
    assert continued["next_review_wall_epoch"] == 11_800.0
    assert continued["next_review_ordinal"] == 3
    assert ledger.status("run-1")["active_turn_id"] == "turn-root"
    assert not any(
        event["kind"] in {"cadence_hard_stop_interrupt_intent", "hard_stopped"}
        for event in ledger.events("run-1")
    )


def test_continuous_tick_rejects_a_fourth_live_lane(tmp_path) -> None:
    ledger, lease = _ledger(tmp_path)
    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    descendants = [
        {
            "thread_id": f"thread-child-{index}",
            "parent_thread_id": "thread-root",
            "session_id": "thread-root",
            "proof_lane": True,
            "observed_status": "active",
            "active_turn_id": f"turn-child-{index}",
        }
        for index in range(4)
    ]
    with pytest.raises(hotjoin.HotJoinError, match="exceeded three"):
        ledger.continuous_tick(
            "run-1",
            now_wall_epoch=4_600.0,
            now_monotonic=5_600.0,
            boot_identity="boot-1",
            thread_id="thread-root",
            turn_id="turn-root",
            descendants=descendants,
            lease=lease,
        )


def test_summary_deadline_does_not_interrupt_idle_child_with_stale_turn_id(
    tmp_path,
) -> None:
    ledger, lease = _ledger(tmp_path)
    state = ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=float(state["next_review_wall_epoch"]),
        now_monotonic=float(state["next_review_monotonic"]),
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[
            {
                "thread_id": "thread-child",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "active",
                "active_turn_id": "turn-child",
            }
        ],
        lease=lease,
    )
    review = due["review"]
    ledger.mark_continuous_root_notice_accepted(
        "run-1",
        review_id=review["review_id"],
        accepted_turn_id="turn-root",
        lease=lease,
    )
    ledger.refresh_continuous_review_lanes(
        "run-1",
        review_id=review["review_id"],
        descendants=[
            {
                "thread_id": "thread-child",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "idle",
                # Codex may retain the latest terminal turn id in an idle child.
                "active_turn_id": "turn-child",
            }
        ],
        lease=lease,
    )
    projection = ledger.continuous_supervisor_state("run-1")
    current = projection["current_review"]
    targets = ledger.prepare_continuous_lane_interrupts(
        "run-1",
        review_id=review["review_id"],
        now_wall_epoch=float(current["summary_deadline_wall_epoch"]),
        now_monotonic=float(current["summary_deadline_monotonic"]),
        boot_identity="boot-1",
        lease=lease,
    )

    assert targets == []
    projection = ledger.continuous_supervisor_state("run-1")
    assert projection["current_review"]["state"] == "summary_deadline"
    lane = projection["current_review"]["lanes"][0]
    assert lane["observed_status"] == "idle"
    assert lane["state"] == "summary_requested"


def test_summary_deadline_interrupts_only_child_and_seals_parked_snapshot(
    tmp_path,
) -> None:
    ledger, lease = _ledger(tmp_path)
    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_600.0,
        now_monotonic=5_600.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[
            {
                "thread_id": "thread-child",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "active",
                "active_turn_id": "turn-child",
            }
        ],
        lease=lease,
    )
    review = due["review"]
    ledger.mark_continuous_root_notice_accepted(
        "run-1",
        review_id=review["review_id"],
        accepted_turn_id="turn-root",
        lease=lease,
    )
    targets = ledger.prepare_continuous_lane_interrupts(
        "run-1",
        review_id=review["review_id"],
        now_wall_epoch=4_900.0,
        now_monotonic=5_900.0,
        boot_identity="boot-1",
        lease=lease,
    )
    assert targets == [
        {
            "lane_id": targets[0]["lane_id"],
            "thread_id": "thread-child",
            "turn_id": "turn-child",
        }
    ]
    assert all(target["thread_id"] != "thread-root" for target in targets)

    summary = _summary(
        targets[0]["lane_id"],
        summary_state="interrupted_partial",
        report_text="partial direction preserved at the summary deadline",
    )
    summary["thread_id"] = "thread-child"
    summary["route_id"] = "route-child"
    ledger.record_continuous_lane_summary(
        "run-1",
        review_id=review["review_id"],
        lane_id=targets[0]["lane_id"],
        summary=summary,
        terminal_status="interrupted",
        terminal_turn_sha256="c" * 64,
        lease=lease,
    )
    snapshot = ledger.seal_continuous_review(
        "run-1",
        review_id=review["review_id"],
        problem_id="problem-1",
        statement_sha256="d" * 64,
        lease=lease,
    )
    assert snapshot["lane_summaries"][0]["summary_state"] == ("interrupted_partial")
    state = ledger.continuous_supervisor_state("run-1")
    assert state is not None
    assert state["state"] == "review_running"
    assert state["current_review"]["state"] == "sealed"
    assert ledger.status("run-1")["active_turn_id"] == "turn-root"


def test_completed_child_is_parked_and_idempotently_resumable_after_review(
    tmp_path,
) -> None:
    ledger, lease = _ledger(tmp_path)
    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_600.0,
        now_monotonic=5_600.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[
            {
                "thread_id": "thread-child",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "idle",
                "active_turn_id": None,
            }
        ],
        lease=lease,
    )
    state = ledger.continuous_supervisor_state("run-1")
    lane_id = state["current_review"]["lanes"][0]["lane_id"]
    summary = _summary(lane_id)
    summary["thread_id"] = "thread-child"
    first = ledger.record_continuous_lane_summary(
        "run-1",
        review_id=due["review"]["review_id"],
        lane_id=lane_id,
        summary=summary,
        terminal_status="completed",
        terminal_turn_sha256="e" * 64,
        lease=lease,
    )
    replay = ledger.record_continuous_lane_summary(
        "run-1",
        review_id=due["review"]["review_id"],
        lane_id=lane_id,
        summary=summary,
        terminal_status="completed",
        terminal_turn_sha256="e" * 64,
        lease=lease,
    )
    assert first["state"] == "parked"
    assert replay["summary_sha256"] == first["summary_sha256"]


def _sealed_single_lane_review(tmp_path):
    ledger, lease = _ledger(tmp_path)
    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_600.0,
        now_monotonic=5_600.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[
            {
                "thread_id": "thread-child",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "idle",
                "active_turn_id": None,
            }
        ],
        lease=lease,
    )
    state = ledger.continuous_supervisor_state("run-1")
    lane_id = state["current_review"]["lanes"][0]["lane_id"]
    summary = _summary(lane_id)
    summary["thread_id"] = "thread-child"
    ledger.record_continuous_lane_summary(
        "run-1",
        review_id=due["review"]["review_id"],
        lane_id=lane_id,
        summary=summary,
        terminal_status="completed",
        terminal_turn_sha256="e" * 64,
        lease=lease,
    )
    snapshot = ledger.seal_continuous_review(
        "run-1",
        review_id=due["review"]["review_id"],
        problem_id="problem-1",
        statement_sha256="d" * 64,
        lease=lease,
    )
    return (
        ledger,
        lease,
        due["review"]["review_id"],
        snapshot,
        snapshot["lane_summaries"][0],
    )


def test_comparative_review_resumes_parked_child_without_root_terminal(
    tmp_path,
) -> None:
    ledger, lease, review_id, snapshot, summary = _sealed_single_lane_review(tmp_path)
    request = ledger.prepare_continuous_reviewer(
        "run-1",
        review_id=review_id,
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        lease=lease,
    )
    assert request["retry_allowed"] is False
    report = _report(
        snapshot, [_decision(summary, verdict="green", action="resume_same")]
    )
    reviewer_receipt = {
        "event_count": 5,
        "event_stream_sha256": "1" * 64,
        "terminal_count": 1,
        "tool_free": True,
        "developer_instructions_sha256": "2" * 64,
        "executable_sha256": "3" * 64,
        "report_sha256": "4" * 64,
        "returncode": 0,
        "stderr_sha256": "5" * 64,
    }
    decision = ledger.record_continuous_reviewer_result(
        "run-1",
        review_id=review_id,
        report=report,
        reviewer_receipt=reviewer_receipt,
        lease=lease,
    )
    assert decision["global_action"] == "continue_cohort"
    assert "followup_task" in ledger.continuous_verdict_notice(decision)
    with ledger._connect() as connection:
        review_row = connection.execute(
            "SELECT reviewer_receipt_json, reviewer_receipt_sha256 "
            "FROM continuous_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    assert review_row["reviewer_receipt_json"] == hotjoin._canonical_json(
        reviewer_receipt
    )
    assert (
        review_row["reviewer_receipt_sha256"]
        == hashlib.sha256(review_row["reviewer_receipt_json"].encode()).hexdigest()
    )
    applied = ledger.apply_continuous_reviewer_result(
        "run-1",
        review_id=review_id,
        accepted_root_turn_id="turn-root",
        lease=lease,
    )
    assert applied == decision
    state = ledger.continuous_supervisor_state("run-1")
    assert state is not None
    assert state["state"] == "active"
    assert state["current_review"] is None
    assert state["next_review_ordinal"] == 2
    assert state["next_review_wall_epoch"] == 8_200.0
    assert ledger.status("run-1")["active_turn_id"] == "turn-root"
    with ledger._connect() as connection:
        lane = connection.execute(
            "SELECT state FROM continuous_review_lanes WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    assert lane["state"] == "resume_authorized"


def test_reviewer_resume_becomes_restart_only_after_host_observed_root_rollover(
    tmp_path,
) -> None:
    ledger, lease, review_id, snapshot, summary = _sealed_single_lane_review(tmp_path)
    ledger.prepare_continuous_reviewer(
        "run-1",
        review_id=review_id,
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        lease=lease,
    )
    with ledger._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE runs SET thread_id = ?, active_turn_id = ? WHERE run_id = ?",
            ("thread-root-new", "turn-root-new", "run-1"),
        )
        connection.commit()
    report = _report(
        snapshot, [_decision(summary, verdict="green", action="resume_same")]
    )
    decision = ledger.record_continuous_reviewer_result(
        "run-1", review_id=review_id, report=report, lease=lease
    )
    assert decision["lane_actions"][0]["next_action"] == "restart_same"
    event = next(
        item
        for item in reversed(ledger.events("run-1"))
        if item["kind"] == "continuous_reviewer_result_recorded"
    )
    audit = event["payload"]["resource_audit"]
    assert audit["lane_actions"][0]["basis"] == "root_thread_epoch_changed"
    assert event["payload"]["resume_to_restart_lane_ids"] == [
        summary["lane_id"]
    ]


def test_reviewer_result_replay_uses_the_frozen_decision_not_current_runtime(
    tmp_path,
) -> None:
    ledger, lease, review_id, snapshot, summary = _sealed_single_lane_review(tmp_path)
    ledger.prepare_continuous_reviewer(
        "run-1",
        review_id=review_id,
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        lease=lease,
    )
    report = _report(
        snapshot, [_decision(summary, verdict="green", action="resume_same")]
    )
    first = ledger.record_continuous_reviewer_result(
        "run-1", review_id=review_id, report=report, lease=lease
    )
    with ledger._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE runs SET thread_id = ?, active_turn_id = ? WHERE run_id = ?",
            ("thread-root-new", "turn-root-new", "run-1"),
        )
        connection.commit()
    replay = ledger.record_continuous_reviewer_result(
        "run-1", review_id=review_id, report=report, lease=lease
    )
    assert replay == first
    assert replay["lane_actions"][0]["next_action"] == "resume_same"


@pytest.mark.parametrize("corruption", ["report_digest", "inner_decision_digest"])
def test_reviewer_result_replay_rejects_corrupt_stored_commitments(
    tmp_path,
    corruption: str,
) -> None:
    ledger, lease, review_id, snapshot, summary = _sealed_single_lane_review(tmp_path)
    ledger.prepare_continuous_reviewer(
        "run-1",
        review_id=review_id,
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        lease=lease,
    )
    report = _report(
        snapshot, [_decision(summary, verdict="green", action="resume_same")]
    )
    ledger.record_continuous_reviewer_result(
        "run-1", review_id=review_id, report=report, lease=lease
    )
    with ledger._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if corruption == "report_digest":
            connection.execute(
                "UPDATE continuous_reviews SET report_sha256 = ? WHERE review_id = ?",
                ("0" * 64, review_id),
            )
        else:
            row = connection.execute(
                "SELECT decision_json FROM continuous_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            decision = json.loads(str(row["decision_json"]))
            decision["decision_sha256"] = "0" * 64
            decision_json = hotjoin._canonical_json(decision)
            connection.execute(
                "UPDATE continuous_reviews SET decision_json = ?, "
                "decision_sha256 = ? WHERE review_id = ?",
                (
                    decision_json,
                    hashlib.sha256(decision_json.encode()).hexdigest(),
                    review_id,
                ),
            )
        connection.commit()
    expected = (
        hotjoin.IdempotencyConflict
        if corruption == "report_digest"
        else hotjoin.HotJoinError
    )
    with pytest.raises(expected):
        ledger.record_continuous_reviewer_result(
            "run-1", review_id=review_id, report=report, lease=lease
        )


def test_all_red_cohort_requires_checkpoint_before_owner_wait(tmp_path) -> None:
    ledger, lease, review_id, snapshot, summary = _sealed_single_lane_review(tmp_path)
    ledger.prepare_continuous_reviewer(
        "run-1",
        review_id=review_id,
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        lease=lease,
    )
    report = _report(snapshot, [_decision(summary, verdict="red", action="retire")])
    decision = ledger.record_continuous_reviewer_result(
        "run-1", review_id=review_id, report=report, lease=lease
    )
    ledger.apply_continuous_reviewer_result(
        "run-1",
        review_id=review_id,
        accepted_root_turn_id="turn-root",
        lease=lease,
    )
    state = ledger.continuous_supervisor_state("run-1")
    assert state is not None
    assert state["state"] == "advisor_checkpoint_required"
    assert ledger.status("run-1")["active_turn_id"] == "turn-root"
    assert not any(
        "root_interrupt" in event["kind"] for event in ledger.events("run-1")
    )
    assert decision["global_action"] == "seek_advisor"


def test_next_cohort_excludes_retired_history_and_binds_progress_history(
    tmp_path,
) -> None:
    ledger, lease = _ledger(tmp_path)
    ledger.ensure_continuous_supervisor(
        "run-1",
        thread_id="thread-root",
        turn_id="turn-root",
        now_wall_epoch=1_000.0,
        now_monotonic=2_000.0,
        boot_identity="boot-1",
        lease=lease,
    )
    first_due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=4_600.0,
        now_monotonic=5_600.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[
            {
                "thread_id": f"thread-{name}",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "idle",
                "active_turn_id": None,
            }
            for name in "abc"
        ],
        lease=lease,
    )["review"]
    first_state = ledger.continuous_supervisor_state("run-1")
    first_summaries: dict[str, dict[str, object]] = {}
    for lane in first_state["current_review"]["lanes"]:
        name = lane["thread_id"].removeprefix("thread-")
        summary = _summary(lane["lane_id"])
        summary["thread_id"] = lane["thread_id"]
        summary["route_id"] = f"route-{name}"
        summary["assigned_bridge"] = f"bridge for {name}"
        ledger.record_continuous_lane_summary(
            "run-1",
            review_id=first_due["review_id"],
            lane_id=lane["lane_id"],
            summary=summary,
            terminal_status="completed",
            terminal_turn_sha256=hashlib.sha256(name.encode()).hexdigest(),
            lease=lease,
        )
        first_summaries[name] = summary
    first_snapshot = ledger.seal_continuous_review(
        "run-1",
        review_id=first_due["review_id"],
        problem_id="problem-1",
        statement_sha256="d" * 64,
        lease=lease,
    )
    first_summaries = {
        str(item["thread_id"]).removeprefix("thread-"): item
        for item in first_snapshot["lane_summaries"]
    }
    ledger.prepare_continuous_reviewer(
        "run-1",
        review_id=first_due["review_id"],
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        lease=lease,
    )
    report = _report(
        first_snapshot,
        [
            _decision(first_summaries["a"], verdict="green", action="resume_same"),
            _decision(
                first_summaries["b"],
                verdict="red",
                action="redirect",
                redirect_to_lane_id=str(first_summaries["a"]["lane_id"]),
            ),
            _decision(first_summaries["c"], verdict="red", action="retire"),
        ],
    )
    ledger.record_continuous_reviewer_result(
        "run-1", review_id=first_due["review_id"], report=report, lease=lease
    )
    ledger.apply_continuous_reviewer_result(
        "run-1",
        review_id=first_due["review_id"],
        accepted_root_turn_id="turn-root",
        lease=lease,
    )

    second_due = ledger.continuous_tick(
        "run-1",
        now_wall_epoch=8_200.0,
        now_monotonic=9_200.0,
        boot_identity="boot-1",
        thread_id="thread-root",
        turn_id="turn-root",
        descendants=[
            {
                "thread_id": f"thread-{name}",
                "parent_thread_id": "thread-root",
                "session_id": "thread-root",
                "proof_lane": True,
                "observed_status": "active" if name == "a" else "idle",
                "active_turn_id": "turn-a" if name == "a" else None,
            }
            for name in "abc"
        ],
        lease=lease,
    )["review"]
    second_state = ledger.continuous_supervisor_state("run-1")
    assert {
        lane["thread_id"] for lane in second_state["current_review"]["lanes"]
    } == {"thread-a"}
    for lane in second_state["current_review"]["lanes"]:
        name = lane["thread_id"].removeprefix("thread-")
        assert name == "a"
        summary = {**first_summaries["a"], "lane_id": lane["lane_id"]}
        ledger.record_continuous_lane_summary(
            "run-1",
            review_id=second_due["review_id"],
            lane_id=lane["lane_id"],
            summary=summary,
            terminal_status="completed",
            terminal_turn_sha256=hashlib.sha256((name + "-2").encode()).hexdigest(),
            lease=lease,
        )
    second_snapshot = ledger.seal_continuous_review(
        "run-1",
        review_id=second_due["review_id"],
        problem_id="problem-1",
        statement_sha256="d" * 64,
        lease=lease,
    )
    history_by_thread = {
        item["thread_id"]: item for item in second_snapshot["lane_history"]
    }
    assert history_by_thread["thread-a"]["unchanged_from_previous"] is True
    assert history_by_thread["thread-a"]["prior_verdict"] == "green"
