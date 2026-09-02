"""Pure contracts for AxiomRelay's long-running continuous supervisor.

This module deliberately owns no process, network, filesystem, or SQLite
effects.  It defines the rolling clocks and exact child-summary/reviewer
reducers that the owner-side hot-join adapter persists and enforces.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


POLICY_ID = "rethlas_continuous_supervisor_v1"
PAID_ROOT_DISPOSITIONS = frozenset(
    {
        "initial_start_allowed",
        "continue_next_cycle",
        "continuous_intent_successor_required",
        "continuous_verdict_successor_required",
    }
)
POLICY = {
    "policy_id": POLICY_ID,
    "review_interval_seconds": 3_600,
    "summary_grace_seconds": 300,
    "review_execution_grace_seconds": 300,
    "renewal_interval_seconds": 9_000,
    "max_concurrent_proof_lanes": 3,
    "root_review_interrupt": False,
    "root_renewal_interrupt": False,
    "root_stop_authorities": [
        "owner_emergency_abort",
        "safety_emergency",
        "explicit_total_budget_exhaustion",
    ],
    "summary_terminal_state": "parked",
    "reviewer_process_authority": False,
    "review_clock_resets_on_renewal": False,
    "renewal_is_terminal": False,
    "owner_wait_suspends_clocks": True,
    "parked_child_resume_scope": "same_root_thread_epoch",
    "root_physical_turn_auto_continuation": False,
    "cause_bound_root_successors": True,
    "review_host_progress_without_root": True,
    "owner_messages_deferred_during_host_review": True,
    "next_cohort_requires_round_finish_or_review_restart": True,
    "review_history_required": True,
    "transition_reducer_schema": "rethlas_continuous_transition_reducer_v1",
    "admission_decision_schema": "rethlas_continuous_admission_decision_v1",
    "paid_root_dispositions": sorted(PAID_ROOT_DISPOSITIONS),
    "verified_completion_requires_owner_receipt_validation": True,
}

RESOURCE_POLICY_SCHEMA = "rethlas_resource_conservation_policy_v1"
PUBLIC_STATE_SCHEMA = "rethlas_continuous_public_state_v1"
RESOURCE_AUDIT_SCHEMA = "rethlas_resource_action_audit_v1"
TRANSITION_REDUCER_SCHEMA = "rethlas_continuous_transition_reducer_v1"
TRANSITION_DECISION_SCHEMA = "rethlas_continuous_transition_decision_v1"
ADMISSION_INPUT_SCHEMA = "rethlas_continuous_admission_input_v1"
ADMISSION_DECISION_SCHEMA = "rethlas_continuous_admission_decision_v1"
ROOT_SUCCESSOR_DECISION_SCHEMA = "rethlas_continuous_root_successor_decision_v1"
PUBLIC_PHASES = frozenset(
    {"running", "reviewing", "verifying", "waiting_owner", "terminal"}
)
TERMINAL_OUTCOMES = frozenset({"completed", "blocked"})
ROOT_SUCCESSOR_CAUSES = frozenset(
    {
        "advisor_checkpoint",
        "candidate_repair",
        "context_rollover",
        "frontier_delta",
        "lane_terminal",
        "new_cohort",
        "review_due",
    }
)
RESTART_AUTHORIZATION_REASONS = frozenset(
    {
        "child_context_unavailable",
        "child_turn_failed",
        "root_thread_epoch_changed",
    }
)
RESOURCE_POLICY = {
    "schema_version": RESOURCE_POLICY_SCHEMA,
    "rule": "preserve_progress_and_require_delta_before_reallocation",
    "default_lane_action": "resume_same",
    "max_live_proof_lanes": 3,
    "report_before_idle": True,
    "checkpoint_before_interrupt": True,
    "restart_requires_host_evidence": True,
    "root_time_interrupt_allowed": False,
    "paid_root_during_host_review": False,
    "owner_messages_deferred_during_host_review": True,
    "next_cohort_requires_round_finish_or_review_restart": True,
    "round_finish_requires_three_reports_and_synthesis": True,
    "stop_unsolved_has_no_paid_successor": True,
    "root_successor_requires_durable_cause": True,
    "one_paid_turn_per_durable_intent": True,
}

LANE_SUMMARY_SCHEMA = "rethlas_lane_summary_v1"
COHORT_SNAPSHOT_SCHEMA = "rethlas_cohort_review_snapshot_v1"
COHORT_REPORT_SCHEMA = "rethlas_cohort_review_report_v1"
COHORT_REQUEST_SCHEMA = "rethlas_cohort_review_request_v1"
NO_NEXT_TEST = "NO_NEXT_TEST"

MAX_LANES = 3
MAX_TEXT_BYTES = 4_096
MAX_SUMMARY_BYTES = 16_384
MAX_SNAPSHOT_BYTES = 96 * 1024
MAX_REPORT_BYTES = 64 * 1024

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
PROBLEM_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}){0,15}$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVIEW_ID_RE = re.compile(r"^cohortreview_[0-9a-f]{32}$")

LANE_VERDICTS = frozenset({"green", "yellow", "red", "unclear"})
LANE_ACTIONS = frozenset(
    {"resume_same", "restart_same", "redirect", "retire", "verify"}
)
GLOBAL_ACTIONS = frozenset(
    {"continue_cohort", "verify_candidate", "seek_advisor", "operational_blocked"}
)
SUMMARY_STATES = frozenset(
    {"terminal_reported", "interrupted_partial", "system_failure", "unavailable"}
)
CANDIDATE_STATES = frozenset({"none", "partial", "complete"})

STATE_MACHINE_TRANSITIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "supervisor": {
        "active": (
            "review_collecting",
            "candidate_verification",
            "advisor_checkpoint_required",
            "paused_owner",
            "waiting_owner_advisor_decision",
            "stopped_unsolved",
            "operational_blocked",
            "completed",
        ),
        "review_collecting": (
            "review_running",
            "paused_owner",
            "waiting_owner_advisor_decision",
            "operational_blocked",
        ),
        "review_running": (
            "review_applying",
            "paused_owner",
            "waiting_owner_advisor_decision",
            "operational_blocked",
        ),
        "review_applying": (
            "active",
            "candidate_verification",
            "advisor_checkpoint_required",
            "paused_owner",
            "waiting_owner_advisor_decision",
            "operational_blocked",
        ),
        "candidate_verification": (
            "paused_owner",
            "waiting_owner_advisor_decision",
            "operational_blocked",
            "completed",
        ),
        "advisor_checkpoint_required": (
            "paused_owner",
            "waiting_owner_advisor_decision",
            "operational_blocked",
        ),
        "paused_owner": ("active", "operational_blocked"),
        "waiting_owner_advisor_decision": ("active", "operational_blocked"),
        "stopped_unsolved": (),
        "operational_blocked": (),
        "completed": (),
    },
    "review": {
        "collecting": ("summary_deadline", "sealed", "operational_blocked"),
        "summary_deadline": ("sealed", "operational_blocked"),
        "sealed": ("running", "operational_blocked"),
        "running": ("completed", "execution_unknown", "operational_blocked"),
        "completed": ("applied", "operational_blocked"),
        "applied": (),
        "execution_unknown": (),
        "operational_blocked": (),
    },
    "lane": {
        "summary_requested": (
            "interrupting",
            "parked",
            "parked_partial",
            "system_failure",
            "unavailable",
        ),
        "interrupting": (
            "parked",
            "parked_partial",
            "system_failure",
            "unavailable",
        ),
        "parked": (
            "summary_requested",
            "parked_partial",
            "system_failure",
            "unavailable",
            "resume_authorized",
            "restart_authorized",
            "redirect_authorized",
            "retired",
            "verify_candidate",
        ),
        "parked_partial": (
            "resume_authorized",
            "restart_authorized",
            "redirect_authorized",
            "retired",
        ),
        "system_failure": ("restart_authorized", "retired"),
        "unavailable": ("restart_authorized", "retired"),
        "resume_authorized": (),
        "restart_authorized": (),
        "redirect_authorized": (),
        "retired": (),
        "verify_candidate": (),
    },
    "renewal": {
        "due": (
            "continued",
            "waiting_owner_advisor_decision",
            "paused_owner",
            "budget_exhausted",
        ),
        "continued": (),
        "waiting_owner_advisor_decision": (),
        "paused_owner": (),
        "budget_exhausted": (),
    },
}

STATE_EVENT_RULES: dict[str, dict[str, dict[str, object]]] = {
    "supervisor": {
        "candidate_detected": {
            "transitions": {"active": "candidate_verification"},
            "effects": ("enter_verification",),
        },
        "route_exhaustion_detected": {
            "transitions": {"active": "advisor_checkpoint_required"},
            "effects": ("require_advisor_checkpoint",),
        },
        "round_stopped_unsolved": {
            "transitions": {"active": "stopped_unsolved"},
            "effects": ("close_unsolved_no_successor",),
        },
        "review_frozen": {
            "transitions": {"active": "review_collecting"},
            "effects": ("freeze_cohort", "notify_root"),
        },
        "review_snapshot_sealed": {
            "transitions": {"review_collecting": "review_running"},
            "effects": ("permit_reviewer_dispatch",),
        },
        "reviewer_result_recorded": {
            "transitions": {"review_running": "review_applying"},
            "effects": ("permit_verdict_application",),
        },
        "review_verdict_continue": {
            "transitions": {"review_applying": "active"},
            "effects": ("continue_logical_root",),
        },
        "review_verdict_verify": {
            "transitions": {"review_applying": "candidate_verification"},
            "effects": ("enter_verification",),
        },
        "review_verdict_advisor": {
            "transitions": {"review_applying": "advisor_checkpoint_required"},
            "effects": ("require_advisor_checkpoint",),
        },
        "review_verdict_blocked": {
            "transitions": {"review_applying": "operational_blocked"},
            "effects": ("block_run",),
        },
        "owner_cost_wait_closed": {
            "transitions": {
                "active": "paused_owner",
                "review_collecting": "paused_owner",
                "review_running": "paused_owner",
                "review_applying": "paused_owner",
                "candidate_verification": "paused_owner",
                "advisor_checkpoint_required": "paused_owner",
                "paused_owner": "paused_owner",
            },
            "effects": ("suspend_clocks",),
        },
        "owner_advisor_wait_closed": {
            "transitions": {
                "active": "waiting_owner_advisor_decision",
                "review_collecting": "waiting_owner_advisor_decision",
                "review_running": "waiting_owner_advisor_decision",
                "review_applying": "waiting_owner_advisor_decision",
                "candidate_verification": "waiting_owner_advisor_decision",
                "advisor_checkpoint_required": "waiting_owner_advisor_decision",
                "waiting_owner_advisor_decision": (
                    "waiting_owner_advisor_decision"
                ),
            },
            "effects": ("suspend_clocks",),
        },
        "owner_wait_resumed": {
            "transitions": {
                "paused_owner": "active",
                "waiting_owner_advisor_decision": "active",
            },
            "effects": ("resume_clocks", "require_fresh_root_epoch"),
        },
        "context_rehydrated": {
            "transitions": {"active": "active"},
            "effects": ("bind_fresh_root_epoch",),
        },
        "verification_published": {
            "transitions": {
                "active": "completed",
                "candidate_verification": "completed",
            },
            "effects": ("complete_run",),
        },
        "operational_failure": {
            "transitions": {
                state: "operational_blocked"
                for state in STATE_MACHINE_TRANSITIONS["supervisor"]
                if state
                not in {"stopped_unsolved", "operational_blocked", "completed"}
            },
            "effects": ("block_run",),
        },
    },
    "review": {
        "summary_deadline_reached": {
            "transitions": {
                "collecting": "summary_deadline",
                "summary_deadline": "summary_deadline",
            },
            "effects": ("select_active_child_stragglers",),
        },
        "review_snapshot_sealed": {
            "transitions": {
                "collecting": "sealed",
                "summary_deadline": "sealed",
                "sealed": "sealed",
            },
            "effects": ("persist_review_snapshot",),
        },
        "reviewer_dispatched": {
            "transitions": {"sealed": "running"},
            "effects": ("dispatch_one_tool_free_reviewer",),
        },
        "reviewer_result_recorded": {
            "transitions": {"running": "completed"},
            "effects": ("persist_immutable_verdict",),
        },
        "reviewer_execution_unknown": {
            "transitions": {"running": "execution_unknown"},
            "effects": ("block_reviewer_replay",),
        },
        "reviewer_operational_blocked": {
            "transitions": {"running": "operational_blocked"},
            "effects": ("block_reviewer_replay",),
        },
        "review_verdict_applied": {
            "transitions": {"completed": "applied"},
            "effects": ("consume_verdict_once",),
        },
        "operational_failure": {
            "transitions": {
                state: "operational_blocked"
                for state in STATE_MACHINE_TRANSITIONS["review"]
                if state
                not in {"applied", "execution_unknown", "operational_blocked"}
            },
            "effects": ("block_reviewer_replay",),
        },
    },
    "lane": {
        "summary_requested_after_refresh": {
            "transitions": {"parked": "summary_requested"},
            "effects": ("request_missing_summary",),
        },
        "summary_deadline_interrupt": {
            "transitions": {"summary_requested": "interrupting"},
            "effects": ("persist_interrupt_intent", "interrupt_child"),
        },
        "summary_terminal_reported": {
            "transitions": {
                "summary_requested": "parked",
                "interrupting": "parked",
                "parked": "parked",
            },
            "effects": ("preserve_lane_summary", "park_child"),
        },
        "summary_interrupted_partial": {
            "transitions": {
                "summary_requested": "parked_partial",
                "interrupting": "parked_partial",
                "parked": "parked_partial",
                "parked_partial": "parked_partial",
            },
            "effects": ("preserve_lane_summary", "park_child"),
        },
        "summary_system_failure": {
            "transitions": {
                "summary_requested": "system_failure",
                "interrupting": "system_failure",
                "parked": "system_failure",
                "system_failure": "system_failure",
            },
            "effects": ("preserve_failure",),
        },
        "summary_unavailable": {
            "transitions": {
                "summary_requested": "unavailable",
                "interrupting": "unavailable",
                "parked": "unavailable",
                "unavailable": "unavailable",
            },
            "effects": ("preserve_failure",),
        },
        "review_resume_authorized": {
            "transitions": {
                "parked": "resume_authorized",
                "parked_partial": "resume_authorized",
            },
            "effects": ("resume_parked_child",),
        },
        "review_restart_authorized": {
            "transitions": {
                "parked": "restart_authorized",
                "parked_partial": "restart_authorized",
                "system_failure": "restart_authorized",
                "unavailable": "restart_authorized",
            },
            "effects": ("consume_host_restart_evidence", "restart_child"),
        },
        "review_redirect_authorized": {
            "transitions": {
                "parked": "redirect_authorized",
                "parked_partial": "redirect_authorized",
            },
            "effects": ("redirect_to_existing_lane",),
        },
        "review_retired": {
            "transitions": {
                "parked": "retired",
                "parked_partial": "retired",
                "system_failure": "retired",
                "unavailable": "retired",
            },
            "effects": ("retire_lane",),
        },
        "review_verify_candidate": {
            "transitions": {"parked": "verify_candidate"},
            "effects": ("enter_verification",),
        },
    },
    "renewal": {
        "renewal_continued": {
            "transitions": {"due": "continued"},
            "effects": ("continue_without_clock_reset",),
        },
        "owner_advisor_wait": {
            "transitions": {"due": "waiting_owner_advisor_decision"},
            "effects": ("suspend_clocks",),
        },
        "owner_cost_wait": {
            "transitions": {"due": "paused_owner"},
            "effects": ("suspend_clocks",),
        },
        "budget_exhausted": {
            "transitions": {"due": "budget_exhausted"},
            "effects": ("stop_for_explicit_budget",),
        },
    },
}


def require_state_transition(machine: str, current: str, target: str) -> None:
    """Reject every state jump not present in the released transition graph."""

    transitions = STATE_MACHINE_TRANSITIONS.get(machine)
    if transitions is None or current not in transitions:
        raise ContinuousSupervisorError(f"unknown {machine} state {current!r}")
    if current != target and target not in transitions[current]:
        raise ContinuousSupervisorError(
            f"illegal {machine} transition {current!r} -> {target!r}"
        )


def state_machine_contract() -> dict[str, Any]:
    if set(STATE_EVENT_RULES) != set(STATE_MACHINE_TRANSITIONS):
        raise AssertionError("continuous event reducer machine set drifted")
    for machine, transitions in STATE_MACHINE_TRANSITIONS.items():
        graph_edges = {
            (current, target)
            for current, targets in transitions.items()
            for target in targets
        }
        event_edges: set[tuple[str, str]] = set()
        for rule in STATE_EVENT_RULES[machine].values():
            raw_transitions = rule.get("transitions")
            effects = rule.get("effects")
            if (
                not isinstance(raw_transitions, dict)
                or not isinstance(effects, tuple)
                or len(effects) != len(set(effects))
                or not all(
                    isinstance(effect, str) and SAFE_ID_RE.fullmatch(effect)
                    for effect in effects
                )
            ):
                raise AssertionError("continuous event reducer rule is malformed")
            event_edges.update(
                (current, target)
                for current, target in raw_transitions.items()
                if current != target
            )
        if event_edges != graph_edges:
            raise AssertionError(
                f"continuous {machine} event reducer does not cover its graph"
            )
    material = {
        "schema_version": "rethlas_continuous_state_machine_v1",
        "machines": {
            machine: {state: list(targets) for state, targets in transitions.items()}
            for machine, transitions in STATE_MACHINE_TRANSITIONS.items()
        },
        "transition_reducer": {
            "schema_version": TRANSITION_REDUCER_SCHEMA,
            "events": {
                machine: {
                    event: {
                        "transitions": dict(rule["transitions"]),
                        "effects": list(rule["effects"]),
                    }
                    for event, rule in rules.items()
                }
                for machine, rules in STATE_EVENT_RULES.items()
            },
        },
    }
    return {**material, "state_machine_sha256": content_sha256(material)}


def resource_policy_contract() -> dict[str, Any]:
    """Return the small host policy used to reject resource-losing actions."""

    return {**RESOURCE_POLICY, "policy_sha256": content_sha256(RESOURCE_POLICY)}


def project_public_state(
    internal_state: str, *, review_state: str | None = None
) -> dict[str, Any]:
    """Project detailed recovery state onto the stable five-phase public API."""

    if internal_state not in STATE_MACHINE_TRANSITIONS["supervisor"]:
        raise ContinuousSupervisorError("continuous public state is unknown")
    if review_state is not None and review_state not in STATE_MACHINE_TRANSITIONS[
        "review"
    ]:
        raise ContinuousSupervisorError("continuous public review state is unknown")

    if internal_state in {
        "review_collecting",
        "review_running",
        "review_applying",
    }:
        phase = "reviewing"
        outcome = None
        if review_state is None:
            raise ContinuousSupervisorError(
                "continuous reviewing state lacks its internal review state"
            )
    elif internal_state == "candidate_verification":
        phase = "verifying"
        outcome = None
    elif internal_state in {"paused_owner", "waiting_owner_advisor_decision"}:
        phase = "waiting_owner"
        outcome = None
    elif internal_state == "completed":
        phase = "terminal"
        outcome = "completed"
    elif internal_state in {"operational_blocked", "stopped_unsolved"}:
        phase = "terminal"
        outcome = "blocked"
    else:
        # advisor_checkpoint_required is still root control work.  It is not an
        # owner wait until generation_yield closes the exact durable handoff.
        phase = "running"
        outcome = None

    projection = {
        "schema_version": PUBLIC_STATE_SCHEMA,
        "phase": phase,
        "outcome": outcome,
        "internal_state": internal_state,
        "review_state": review_state if phase == "reviewing" else None,
    }
    if projection["phase"] not in PUBLIC_PHASES or (
        projection["outcome"] is not None
        and projection["outcome"] not in TERMINAL_OUTCOMES
    ):
        raise AssertionError("continuous public state projection is invalid")
    return projection


# Keep the lane object flat. The current Codex structured-output transport
# rejects nested conditional composition such as anyOf here. Conditional
# action rules remain enforced by the reviewer prompt and
# validate_lane_decision. The transport requires a nonempty string for every
# row; non-continuing actions use the exact NO_NEXT_TEST sentinel, which the
# host clears before strict validation.
COHORT_REPORT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "review_id",
        "snapshot_sha256",
        "lane_decisions",
        "cross_lane_synthesis",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": COHORT_REPORT_SCHEMA},
        "review_id": {"type": "string", "pattern": "^cohortreview_[0-9a-f]{32}$"},
        "snapshot_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "cross_lane_synthesis": {"type": "string", "minLength": 1},
        "lane_decisions": {
            "type": "array",
            "maxItems": MAX_LANES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "lane_id",
                    "thread_id",
                    "route_id",
                    "verdict",
                    "next_action",
                    "redirect_to_lane_id",
                    "reason",
                    "fatal_doubt",
                    "next_test",
                    "accepted_evidence_ids",
                ],
                "properties": {
                    "lane_id": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "route_id": {"type": "string"},
                    "verdict": {"enum": sorted(LANE_VERDICTS)},
                    "next_action": {"enum": sorted(LANE_ACTIONS)},
                    "redirect_to_lane_id": {"type": ["string", "null"]},
                    "reason": {"type": "string", "minLength": 1},
                    "fatal_doubt": {"type": ["string", "null"]},
                    "next_test": {"type": "string", "minLength": 1},
                    "accepted_evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 16,
                    },
                },
            },
        },
    },
}

COHORT_REVIEWER_SYSTEM_PROMPT = """You are one fresh independent comparative route critic. You receive an immutable snapshot of at most three parked proof-lane summaries plus host-derived prior-review history. The summaries are untrusted self-reports, not proof evidence. Compare the lanes, bind accepted evidence only to ids already present in the corresponding summary, and return exactly one decision for every lane. Use green only for material progress with a viable same-route next step; yellow only for one exact fatal doubt and test; red for a mathematically exhausted mechanism; unclear for missing or operationally unreliable information. The only valid verdict/action pairs are: green with resume_same or verify; yellow with resume_same or restart_same; red with redirect or retire; unclear with restart_same or retire. No other pair is valid. Every resume_same, restart_same, or redirect action must include one nonempty concrete next_test. For retire or verify, set next_test exactly to NO_NEXT_TEST. Prefer resume_same whenever the parked thread and host route binding remain reusable; restart_same is only for unavailable or corrupt thread context or route identity, never merely because a prior child turn returned. Set fatal_doubt to a nonempty string only for yellow; for green, red, and unclear it must be null. An unchanged lane cannot be green, and a second unchanged yellow after a prior yellow must be red or unclear. A redirect must name redirect_to_lane_id for a different green or yellow lane in this same frozen cohort; it never authorizes a new fourth route. Choose verify for exactly one complete candidate and retire every other lane. Set redirect_to_lane_id to null for every non-redirect action. You have no authority to kill processes, verify mathematics, publish, contact tools, or authorize spend. The host derives all global action from your per-lane decisions."""


class ContinuousSupervisorError(ValueError):
    """Raised when a continuous-supervisor contract is malformed."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def reduce_state_event(machine: str, current_state: str, event: str) -> dict[str, Any]:
    """Reduce one named durable event to its only authorized state target.

    Persistence remains an adapter responsibility.  Callers supply no target,
    so a host branch cannot silently invent a state edge that differs from the
    hash-bound reducer contract.
    """

    machine_rules = STATE_EVENT_RULES.get(machine)
    if machine_rules is None or machine not in STATE_MACHINE_TRANSITIONS:
        raise ContinuousSupervisorError(
            f"unknown continuous transition machine {machine!r}"
        )
    rule = machine_rules.get(event)
    if rule is None:
        raise ContinuousSupervisorError(
            f"unknown {machine} transition event {event!r}"
        )
    transitions = rule["transitions"]
    effects = rule["effects"]
    if not isinstance(transitions, dict) or not isinstance(effects, tuple):
        raise AssertionError("continuous transition rule is malformed")
    target_state = transitions.get(current_state)
    if not isinstance(target_state, str):
        raise ContinuousSupervisorError(
            f"event {event!r} is invalid from {machine} state {current_state!r}"
        )
    require_state_transition(machine, current_state, target_state)
    material = {
        "schema_version": TRANSITION_DECISION_SCHEMA,
        "machine": machine,
        "event": event,
        "current_state": current_state,
        "target_state": target_state,
        "effects": list(effects),
    }
    return {**material, "decision_sha256": content_sha256(material)}


def _bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ContinuousSupervisorError(f"{label} must be boolean")
    return value


def derive_root_successor_decision(
    *,
    supervisor_state: str,
    open_lane_count: int,
    lane_terminal_delta: bool,
    frontier_delta: bool,
    pending_context_rollover: bool,
    review_due: bool,
    round_finish_action: str | None,
    owner_yield_prepared: bool,
    completion_visible: bool,
    review_host_can_progress: bool,
) -> dict[str, Any]:
    """Authorize at most one same-run physical root from one durable cause.

    A clean model return is intentionally absent from the inputs.  It can make
    an already-created intent eligible for dispatch, but can never create one.
    """

    if supervisor_state not in STATE_MACHINE_TRANSITIONS["supervisor"]:
        raise ContinuousSupervisorError("root successor supervisor state is invalid")
    if (
        type(open_lane_count) is not int
        or open_lane_count < 0
        or open_lane_count > MAX_LANES
    ):
        raise ContinuousSupervisorError("root successor active lane count is invalid")
    bool_values = {
        "lane_terminal_delta": lane_terminal_delta,
        "frontier_delta": frontier_delta,
        "pending_context_rollover": pending_context_rollover,
        "review_due": review_due,
        "owner_yield_prepared": owner_yield_prepared,
        "completion_visible": completion_visible,
        "review_host_can_progress": review_host_can_progress,
    }
    normalized_bools = {
        key: _bool(value, label=key) for key, value in bool_values.items()
    }
    if round_finish_action not in {
        None,
        "new_cohort",
        "advisor_checkpoint",
        "stop_unsolved",
    }:
        raise ContinuousSupervisorError("root successor round action is invalid")

    cause_kind: str | None = None
    if not (
        normalized_bools["owner_yield_prepared"]
        or normalized_bools["completion_visible"]
        or normalized_bools["review_host_can_progress"]
    ):
        root_work_states = {
            "active",
            "candidate_verification",
            "advisor_checkpoint_required",
        }
        if (
            normalized_bools["pending_context_rollover"]
            and supervisor_state in root_work_states
        ):
            cause_kind = "context_rollover"
        elif (
            round_finish_action == "new_cohort"
            and supervisor_state == "active"
            and open_lane_count == 0
        ):
            cause_kind = "new_cohort"
        elif (
            round_finish_action == "advisor_checkpoint"
            and supervisor_state == "advisor_checkpoint_required"
            and open_lane_count == 0
        ):
            cause_kind = "advisor_checkpoint"
        elif (
            normalized_bools["lane_terminal_delta"]
            and supervisor_state == "active"
        ):
            cause_kind = "lane_terminal"
        elif (
            normalized_bools["review_due"]
            and supervisor_state == "active"
            and open_lane_count > 0
        ):
            cause_kind = "review_due"
        elif (
            normalized_bools["frontier_delta"]
            and open_lane_count == 0
            and supervisor_state == "candidate_verification"
        ):
            cause_kind = "candidate_repair"
        elif (
            normalized_bools["frontier_delta"]
            and open_lane_count == 0
            and supervisor_state
            in {"active", "advisor_checkpoint_required"}
        ):
            cause_kind = (
                "advisor_checkpoint"
                if supervisor_state == "advisor_checkpoint_required"
                else "frontier_delta"
            )

    if cause_kind is not None and cause_kind not in ROOT_SUCCESSOR_CAUSES:
        raise AssertionError("root successor reducer emitted an unknown cause")
    material = {
        "schema_version": ROOT_SUCCESSOR_DECISION_SCHEMA,
        "supervisor_state": supervisor_state,
        "open_lane_count": open_lane_count,
        **normalized_bools,
        "round_finish_action": round_finish_action,
        "create_intent": cause_kind is not None,
        "cause_kind": cause_kind,
        "effects": ["create_root_intent"] if cause_kind is not None else [],
    }
    return {**material, "decision_sha256": content_sha256(material)}


def derive_runtime_admission(
    *,
    operationally_blocked: bool,
    stale_turn_interrupt_required: bool,
    pending_terminal: bool,
    cycle_state: str,
    cycle_close_disposition: str | None,
    owner_yield_prepared: bool,
    valid_fresh_epoch: bool,
    pending_root_intent: bool,
    supervisor_state: str | None,
    root_active: bool,
    uncertain_intent_count: int,
    review_root_notice_state: str | None,
    review_state: str | None,
    review_decision_bound: bool,
) -> dict[str, Any]:
    """Derive the one continuous-mode host admission from durable facts.

    This is the only reducer that may authorize a paid continuous root.  It is
    deliberately unaware of SQLite, process state, prompts, and model output.
    """

    bool_values = {
        "operationally_blocked": operationally_blocked,
        "stale_turn_interrupt_required": stale_turn_interrupt_required,
        "pending_terminal": pending_terminal,
        "owner_yield_prepared": owner_yield_prepared,
        "valid_fresh_epoch": valid_fresh_epoch,
        "pending_root_intent": pending_root_intent,
        "root_active": root_active,
        "review_decision_bound": review_decision_bound,
    }
    normalized_bools = {
        key: _bool(value, label=key) for key, value in bool_values.items()
    }
    cycle_states = {
        "active",
        "review_due",
        "review_running",
        "verification_required",
        "hard_stop_pending",
        "hard_stopped",
        "closed",
        "operational_blocked",
    }
    notice_states = {None, "prepared", "accepted", "delivery_unknown"}
    if cycle_state not in cycle_states:
        raise ContinuousSupervisorError("continuous admission cycle state is invalid")
    if cycle_close_disposition is not None and (
        not isinstance(cycle_close_disposition, str)
        or SAFE_ID_RE.fullmatch(cycle_close_disposition) is None
    ):
        raise ContinuousSupervisorError(
            "continuous admission close disposition is invalid"
        )
    if supervisor_state is not None and supervisor_state not in (
        STATE_MACHINE_TRANSITIONS["supervisor"]
    ):
        raise ContinuousSupervisorError(
            "continuous admission supervisor state is invalid"
        )
    if review_state is not None and review_state not in STATE_MACHINE_TRANSITIONS[
        "review"
    ]:
        raise ContinuousSupervisorError("continuous admission review state is invalid")
    if review_root_notice_state not in notice_states:
        raise ContinuousSupervisorError(
            "continuous admission review notice state is invalid"
        )
    if (
        type(uncertain_intent_count) is not int
        or uncertain_intent_count < 0
    ):
        raise ContinuousSupervisorError(
            "continuous admission uncertain intent count is invalid"
        )
    if supervisor_state is None and (
        review_state is not None
        or review_root_notice_state is not None
        or review_decision_bound
    ):
        raise ContinuousSupervisorError(
            "continuous admission review exists without a supervisor"
        )
    if review_state is None and (
        review_root_notice_state is not None or review_decision_bound
    ):
        raise ContinuousSupervisorError(
            "continuous admission review binding is incomplete"
        )
    if review_state is not None and review_root_notice_state is None:
        raise ContinuousSupervisorError(
            "continuous admission review lacks its notice state"
        )
    if review_decision_bound and review_state not in {"completed", "applied"}:
        raise ContinuousSupervisorError(
            "continuous admission decision lacks a completed review"
        )
    if supervisor_state in {
        "review_collecting",
        "review_running",
        "review_applying",
    } and review_state is None:
        raise ContinuousSupervisorError(
            "continuous admission reviewing supervisor lacks its review"
        )
    if supervisor_state == "completed" and (
        cycle_state != "closed" or cycle_close_disposition != "verified"
    ):
        raise ContinuousSupervisorError(
            "continuous completed state lacks its verified cycle close"
        )
    if supervisor_state == "paused_owner" and (
        cycle_state != "closed" or cycle_close_disposition != "owner_wait_cost"
    ):
        raise ContinuousSupervisorError(
            "continuous cost wait lacks its closed cadence binding"
        )
    if supervisor_state == "waiting_owner_advisor_decision" and (
        cycle_state != "closed" or cycle_close_disposition != "owner_wait_advisor"
    ):
        raise ContinuousSupervisorError(
            "continuous advisor wait lacks its closed cadence binding"
        )
    if supervisor_state == "stopped_unsolved" and (
        cycle_state != "closed" or cycle_close_disposition != "stop_unsolved"
    ):
        raise ContinuousSupervisorError(
            "continuous unsolved stop lacks its closed cadence binding"
        )
    if normalized_bools["pending_root_intent"] and supervisor_state not in {
        "active",
        "candidate_verification",
        "advisor_checkpoint_required",
    }:
        raise ContinuousSupervisorError(
            "continuous root intent is invalid for its supervisor state"
        )

    if normalized_bools["operationally_blocked"]:
        disposition = "operational_blocked"
    elif normalized_bools["stale_turn_interrupt_required"]:
        disposition = "stale_turn_guardian_interrupt_required"
    elif normalized_bools["pending_terminal"]:
        disposition = "terminal_observed_pending_finalization"
    elif cycle_state == "operational_blocked":
        disposition = "operational_blocked"
    elif normalized_bools["owner_yield_prepared"]:
        disposition = "owner_yield_close_required"
    elif supervisor_state == "completed":
        disposition = "completed"
    elif supervisor_state == "stopped_unsolved":
        disposition = "stop_unsolved"
    elif cycle_state == "closed":
        if cycle_close_disposition in {"owner_wait_cost", "owner_wait_advisor"}:
            disposition = str(cycle_close_disposition)
        elif (
            cycle_close_disposition == "continue_next_cycle"
            and normalized_bools["valid_fresh_epoch"]
        ):
            disposition = "continue_next_cycle"
        else:
            disposition = "operational_blocked"
    elif supervisor_state is None:
        if not normalized_bools["root_active"] and uncertain_intent_count == 0:
            disposition = "initial_start_allowed"
        else:
            disposition = "continuous_start_in_progress"
    elif supervisor_state == "paused_owner":
        disposition = "owner_wait_cost"
    elif supervisor_state == "waiting_owner_advisor_decision":
        disposition = "owner_wait_advisor"
    elif supervisor_state == "operational_blocked":
        disposition = "operational_blocked"
    elif (
        not normalized_bools["root_active"]
        and uncertain_intent_count == 0
        and normalized_bools["pending_root_intent"]
    ):
        disposition = "continuous_intent_successor_required"
    elif (
        not normalized_bools["root_active"]
        and review_root_notice_state == "accepted"
        and supervisor_state in {"review_collecting", "review_running"}
        and review_state in {"collecting", "summary_deadline", "sealed", "running"}
    ):
        disposition = "continuous_review_host_recovery"
    elif (
        not normalized_bools["root_active"]
        and uncertain_intent_count == 0
        and supervisor_state == "review_applying"
        and review_state == "completed"
        and normalized_bools["review_decision_bound"]
    ):
        disposition = "continuous_verdict_successor_required"
    elif normalized_bools["root_active"]:
        disposition = "continuous_active"
    else:
        disposition = "continuous_root_missing"

    paid_dispositions = set(PAID_ROOT_DISPOSITIONS)
    resumable_dispositions = paid_dispositions | {
        "terminal_observed_pending_finalization",
        "continuous_review_host_recovery",
    }
    effects_by_disposition = {
        "initial_start_allowed": ["start_paid_root"],
        "continue_next_cycle": ["consume_fresh_epoch_handoff", "start_paid_root"],
        "continuous_verdict_successor_required": [
            "apply_durable_verdict",
            "start_paid_root",
        ],
        "continuous_intent_successor_required": [
            "consume_durable_root_intent",
            "start_paid_root",
        ],
        "terminal_observed_pending_finalization": ["recover_terminal"],
        "continuous_review_host_recovery": ["continue_host_review"],
    }
    input_material = {
        "schema_version": ADMISSION_INPUT_SCHEMA,
        **normalized_bools,
        "cycle_state": cycle_state,
        "cycle_close_disposition": cycle_close_disposition,
        "supervisor_state": supervisor_state,
        "uncertain_intent_count": uncertain_intent_count,
        "review_root_notice_state": review_root_notice_state,
        "review_state": review_state,
    }
    decision_material = {
        "schema_version": ADMISSION_DECISION_SCHEMA,
        "input_sha256": content_sha256(input_material),
        "disposition": disposition,
        "paid_turn_allowed": disposition in paid_dispositions,
        "adapter_resume_allowed": disposition in resumable_dispositions,
        "authorized_effects": effects_by_disposition.get(disposition, []),
    }
    return {
        **decision_material,
        "decision_sha256": content_sha256(decision_material),
    }


def _exact_object(value: object, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ContinuousSupervisorError(f"{label} has an unsupported shape")
    return dict(value)


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SAFE_ID_RE.fullmatch(value) is None:
        raise ContinuousSupervisorError(f"{label} is invalid")
    return value


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ContinuousSupervisorError(f"{label} is not SHA-256")
    return value


def _problem_id(value: object) -> str:
    if not isinstance(value, str) or PROBLEM_ID_RE.fullmatch(value) is None:
        raise ContinuousSupervisorError("problem id is invalid")
    return value


def _text(value: object, *, label: str, maximum: int = MAX_TEXT_BYTES) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ContinuousSupervisorError(f"{label} must be non-empty bounded text")
    return value


def _optional_text(
    value: object, *, label: str, maximum: int = MAX_TEXT_BYTES
) -> str | None:
    return None if value is None else _text(value, label=label, maximum=maximum)


def _id_list(value: object, *, label: str, maximum: int = 16) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ContinuousSupervisorError(f"{label} must be a bounded list")
    normalized = [_safe_id(item, label=f"{label} item") for item in value]
    if len(normalized) != len(set(normalized)):
        raise ContinuousSupervisorError(f"{label} contains duplicates")
    return normalized


def review_due_at(origin_epoch: float, ordinal: int) -> float:
    """Return the immutable global due time for one 1-based review ordinal."""

    if not math.isfinite(origin_epoch):
        raise ContinuousSupervisorError("review origin must be finite")
    if type(ordinal) is not int or ordinal < 1:
        raise ContinuousSupervisorError("review ordinal must be positive")
    return origin_epoch + ordinal * int(POLICY["review_interval_seconds"])


def renewal_due_at(origin_epoch: float, ordinal: int) -> float:
    """Return the immutable global due time for one 1-based renewal ordinal."""

    if not math.isfinite(origin_epoch):
        raise ContinuousSupervisorError("renewal origin must be finite")
    if type(ordinal) is not int or ordinal < 1:
        raise ContinuousSupervisorError("renewal ordinal must be positive")
    return origin_epoch + ordinal * int(POLICY["renewal_interval_seconds"])


def next_review_ordinal(origin_epoch: float, now_epoch: float) -> int:
    """Return the first review ordinal whose due time is strictly after now."""

    if not math.isfinite(origin_epoch) or not math.isfinite(now_epoch):
        raise ContinuousSupervisorError("review clocks must be finite")
    elapsed = max(0.0, now_epoch - origin_epoch)
    interval = int(POLICY["review_interval_seconds"])
    return math.floor(elapsed / interval) + 1


def next_renewal_ordinal(origin_epoch: float, now_epoch: float) -> int:
    """Return the first renewal ordinal whose due time is strictly after now."""

    if not math.isfinite(origin_epoch) or not math.isfinite(now_epoch):
        raise ContinuousSupervisorError("renewal clocks must be finite")
    elapsed = max(0.0, now_epoch - origin_epoch)
    interval = int(POLICY["renewal_interval_seconds"])
    return math.floor(elapsed / interval) + 1


def validate_lane_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = _exact_object(
        value,
        {
            "schema_version",
            "lane_id",
            "thread_id",
            "route_id",
            "assigned_bridge",
            "summary_state",
            "candidate_status",
            "proved_claim_evidence_ids",
            "failed_path_evidence_ids",
            "counterexample_evidence_ids",
            "remaining_obligation",
            "best_next_test",
            "report_text",
            "report_sha256",
        },
        label="lane summary",
    )
    if raw["schema_version"] != LANE_SUMMARY_SCHEMA:
        raise ContinuousSupervisorError("lane summary schema is invalid")
    summary_state = raw["summary_state"]
    candidate_status = raw["candidate_status"]
    if summary_state not in SUMMARY_STATES:
        raise ContinuousSupervisorError("lane summary state is invalid")
    if candidate_status not in CANDIDATE_STATES:
        raise ContinuousSupervisorError("lane candidate status is invalid")
    report_text = _optional_text(
        raw["report_text"], label="lane report text", maximum=MAX_SUMMARY_BYTES
    )
    report_sha = raw["report_sha256"]
    if report_text is None:
        if report_sha is not None or summary_state not in {
            "system_failure",
            "unavailable",
        }:
            raise ContinuousSupervisorError(
                "lane report text/digest pairing is invalid"
            )
    else:
        if len(report_text.encode("utf-8")) > MAX_SUMMARY_BYTES:
            raise ContinuousSupervisorError("lane report exceeds its byte bound")
        expected = hashlib.sha256(report_text.encode("utf-8")).hexdigest()
        if report_sha != expected:
            raise ContinuousSupervisorError("lane report digest mismatch")
    if candidate_status == "complete" and summary_state != "terminal_reported":
        raise ContinuousSupervisorError("complete candidate requires a terminal report")
    normalized = {
        "schema_version": LANE_SUMMARY_SCHEMA,
        "lane_id": _safe_id(raw["lane_id"], label="lane id"),
        "thread_id": _safe_id(raw["thread_id"], label="lane thread id"),
        "route_id": _safe_id(raw["route_id"], label="lane route id"),
        "assigned_bridge": _text(raw["assigned_bridge"], label="assigned bridge"),
        "summary_state": summary_state,
        "candidate_status": candidate_status,
        "proved_claim_evidence_ids": _id_list(
            raw["proved_claim_evidence_ids"], label="proved claim evidence ids"
        ),
        "failed_path_evidence_ids": _id_list(
            raw["failed_path_evidence_ids"], label="failed path evidence ids"
        ),
        "counterexample_evidence_ids": _id_list(
            raw["counterexample_evidence_ids"],
            label="counterexample evidence ids",
        ),
        "remaining_obligation": _optional_text(
            raw["remaining_obligation"], label="remaining obligation"
        ),
        "best_next_test": _optional_text(raw["best_next_test"], label="best next test"),
        "report_text": report_text,
        "report_sha256": report_sha,
    }
    if len(canonical_json_bytes(normalized)) > MAX_SUMMARY_BYTES:
        raise ContinuousSupervisorError("lane summary exceeds its byte bound")
    return normalized


def lane_summary_content_sha256(value: Mapping[str, Any]) -> str:
    """Digest lane substance while excluding the per-review lane identifier."""

    normalized = validate_lane_summary(value)
    material = {key: item for key, item in normalized.items() if key != "lane_id"}
    return content_sha256(material)


def _normalize_lane_history(
    value: Mapping[str, Any], *, summary: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _exact_object(
        value,
        {
            "lane_id",
            "thread_id",
            "prior_review_id",
            "prior_summary_content_sha256",
            "current_summary_content_sha256",
            "unchanged_from_previous",
            "prior_verdict",
            "prior_next_action",
            "resume_scope",
        },
        label="lane history",
    )
    if raw["lane_id"] != summary["lane_id"] or raw["thread_id"] != summary["thread_id"]:
        raise ContinuousSupervisorError("lane history binding mismatch")
    prior_review_id = raw["prior_review_id"]
    if (
        prior_review_id is not None
        and REVIEW_ID_RE.fullmatch(str(prior_review_id)) is None
    ):
        raise ContinuousSupervisorError("lane history prior review id is invalid")
    prior_digest = raw["prior_summary_content_sha256"]
    if prior_digest is not None:
        prior_digest = _sha256(prior_digest, label="prior lane summary digest")
    current_digest = _sha256(
        raw["current_summary_content_sha256"],
        label="current lane summary digest",
    )
    if current_digest != lane_summary_content_sha256(summary):
        raise ContinuousSupervisorError("lane history current summary digest mismatch")
    unchanged = raw["unchanged_from_previous"]
    if type(unchanged) is not bool or unchanged != (
        prior_digest is not None and prior_digest == current_digest
    ):
        raise ContinuousSupervisorError("lane history unchanged flag is invalid")
    prior_verdict = raw["prior_verdict"]
    prior_action = raw["prior_next_action"]
    if (prior_review_id is None) != (prior_digest is None):
        raise ContinuousSupervisorError("lane history prior binding is incomplete")
    if prior_verdict is not None and prior_verdict not in LANE_VERDICTS:
        raise ContinuousSupervisorError("lane history prior verdict is invalid")
    if prior_action is not None and prior_action not in LANE_ACTIONS:
        raise ContinuousSupervisorError("lane history prior action is invalid")
    if prior_review_id is None and (
        prior_verdict is not None or prior_action is not None
    ):
        raise ContinuousSupervisorError("new lane history carries a prior disposition")
    if raw["resume_scope"] != POLICY["parked_child_resume_scope"]:
        raise ContinuousSupervisorError("lane history resume scope is invalid")
    return {
        "lane_id": summary["lane_id"],
        "thread_id": summary["thread_id"],
        "prior_review_id": prior_review_id,
        "prior_summary_content_sha256": prior_digest,
        "current_summary_content_sha256": current_digest,
        "unchanged_from_previous": unchanged,
        "prior_verdict": prior_verdict,
        "prior_next_action": prior_action,
        "resume_scope": POLICY["parked_child_resume_scope"],
    }


def build_cohort_snapshot(
    *,
    review_id: str,
    review_ordinal: int,
    due_at_epoch: float,
    root_thread_id: str,
    root_turn_id: str,
    problem_id: str,
    statement_sha256: str,
    lane_summaries: Sequence[Mapping[str, Any]],
    lane_histories: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if REVIEW_ID_RE.fullmatch(review_id) is None:
        raise ContinuousSupervisorError("cohort review id is invalid")
    if type(review_ordinal) is not int or review_ordinal < 1:
        raise ContinuousSupervisorError("cohort review ordinal is invalid")
    if not math.isfinite(due_at_epoch):
        raise ContinuousSupervisorError("cohort review due time is invalid")
    if not isinstance(lane_summaries, Sequence) or isinstance(
        lane_summaries, (str, bytes)
    ):
        raise ContinuousSupervisorError("lane summaries must be a sequence")
    summaries = [validate_lane_summary(item) for item in lane_summaries]
    if len(summaries) > MAX_LANES:
        raise ContinuousSupervisorError("cohort snapshot exceeds three lanes")
    lane_ids = [item["lane_id"] for item in summaries]
    thread_ids = [item["thread_id"] for item in summaries]
    if len(lane_ids) != len(set(lane_ids)) or len(thread_ids) != len(set(thread_ids)):
        raise ContinuousSupervisorError("cohort snapshot contains duplicate lanes")
    if lane_histories is None:
        histories = [
            {
                "lane_id": item["lane_id"],
                "thread_id": item["thread_id"],
                "prior_review_id": None,
                "prior_summary_content_sha256": None,
                "current_summary_content_sha256": lane_summary_content_sha256(item),
                "unchanged_from_previous": False,
                "prior_verdict": None,
                "prior_next_action": None,
                "resume_scope": POLICY["parked_child_resume_scope"],
            }
            for item in summaries
        ]
    else:
        if not isinstance(lane_histories, Sequence) or isinstance(
            lane_histories, (str, bytes)
        ):
            raise ContinuousSupervisorError("lane histories must be a sequence")
        histories_by_lane: dict[str, Mapping[str, Any]] = {}
        for item in lane_histories:
            if not isinstance(item, Mapping):
                raise ContinuousSupervisorError("lane history is malformed")
            lane_id = item.get("lane_id")
            if not isinstance(lane_id, str) or lane_id in histories_by_lane:
                raise ContinuousSupervisorError("lane history identity is invalid")
            histories_by_lane[lane_id] = item
        if set(histories_by_lane) != set(lane_ids):
            raise ContinuousSupervisorError("lane histories do not cover the cohort")
        histories = [
            _normalize_lane_history(histories_by_lane[item["lane_id"]], summary=item)
            for item in summaries
        ]
    snapshot = {
        "schema_version": COHORT_SNAPSHOT_SCHEMA,
        "review_id": review_id,
        "review_ordinal": review_ordinal,
        "due_at_epoch": due_at_epoch,
        "root_thread_id": _safe_id(root_thread_id, label="root thread id"),
        "root_turn_id": _safe_id(root_turn_id, label="root turn id"),
        "problem_id": _problem_id(problem_id),
        "statement_sha256": _sha256(
            statement_sha256, label="problem statement SHA-256"
        ),
        "lane_summaries": sorted(summaries, key=lambda item: item["lane_id"]),
        "lane_history": sorted(histories, key=lambda item: item["lane_id"]),
    }
    if len(canonical_json_bytes(snapshot)) > MAX_SNAPSHOT_BYTES:
        raise ContinuousSupervisorError("cohort snapshot exceeds its byte bound")
    return {**snapshot, "snapshot_sha256": content_sha256(snapshot)}


def build_reviewer_request(
    *,
    snapshot: Mapping[str, Any],
    expected_model: str,
    reasoning_effort: str,
    policy_sha256: str,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != (
        COHORT_SNAPSHOT_SCHEMA
    ):
        raise ContinuousSupervisorError("cohort reviewer snapshot is invalid")
    expected_snapshot_sha = content_sha256(
        {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    )
    if snapshot.get("snapshot_sha256") != expected_snapshot_sha:
        raise ContinuousSupervisorError("cohort reviewer snapshot digest mismatch")
    model = _safe_id(expected_model, label="cohort reviewer model")
    if reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ContinuousSupervisorError("cohort reviewer effort is invalid")
    policy_digest = _sha256(policy_sha256, label="cohort reviewer policy SHA-256")
    seed = {
        "schema_version": COHORT_REQUEST_SCHEMA,
        "review_id": snapshot["review_id"],
        "snapshot_sha256": expected_snapshot_sha,
        "snapshot": dict(snapshot),
        "expected_model": model,
        "reasoning_effort": reasoning_effort,
        "policy_sha256": policy_digest,
        "attempt": 1,
        "retry_allowed": False,
    }
    request = {**seed, "request_sha256": content_sha256(seed)}
    if len(canonical_json_bytes(request)) > MAX_SNAPSHOT_BYTES + 16_384:
        raise ContinuousSupervisorError(
            "cohort reviewer request exceeds its byte bound"
        )
    return request


def validate_lane_decision(
    value: Mapping[str, Any], *, summary: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _exact_object(
        value,
        {
            "lane_id",
            "thread_id",
            "route_id",
            "verdict",
            "next_action",
            "redirect_to_lane_id",
            "reason",
            "fatal_doubt",
            "next_test",
            "accepted_evidence_ids",
        },
        label="lane decision",
    )
    for key in ("lane_id", "thread_id", "route_id"):
        if raw[key] != summary[key]:
            raise ContinuousSupervisorError(f"lane decision {key} binding mismatch")
    verdict = raw["verdict"]
    action = raw["next_action"]
    if verdict not in LANE_VERDICTS or action not in LANE_ACTIONS:
        raise ContinuousSupervisorError("lane verdict/action is invalid")
    if action == "verify" and summary["candidate_status"] != "complete":
        raise ContinuousSupervisorError("verify requires a complete candidate summary")
    allowed_by_verdict = {
        "green": {"resume_same", "verify"},
        "yellow": {"resume_same", "restart_same"},
        "red": {"redirect", "retire"},
        "unclear": {"restart_same", "retire"},
    }
    if action not in allowed_by_verdict[verdict]:
        raise ContinuousSupervisorError("lane verdict/action combination is invalid")
    redirect_to_lane_id = raw["redirect_to_lane_id"]
    if action == "redirect":
        redirect_to_lane_id = _safe_id(
            redirect_to_lane_id, label="redirect target lane id"
        )
        if redirect_to_lane_id == summary["lane_id"]:
            raise ContinuousSupervisorError("lane cannot redirect to itself")
    elif redirect_to_lane_id is not None:
        raise ContinuousSupervisorError("only redirect may name a target lane")
    fatal_doubt = _optional_text(raw["fatal_doubt"], label="lane fatal doubt")
    next_test = _optional_text(raw["next_test"], label="lane next test")
    if verdict == "yellow" and (fatal_doubt is None or next_test is None):
        raise ContinuousSupervisorError("yellow requires one fatal doubt and next test")
    if verdict != "yellow" and fatal_doubt is not None:
        raise ContinuousSupervisorError("only yellow may carry a fatal doubt")
    continuing = action in {"resume_same", "restart_same", "redirect"}
    if continuing and (
        next_test is None or next_test == NO_NEXT_TEST
    ):
        raise ContinuousSupervisorError(
            "continuing lane action requires a concrete next test"
        )
    if not continuing and next_test is not None:
        raise ContinuousSupervisorError(
            "non-continuing lane action cannot carry a next test"
        )
    evidence_ids = _id_list(raw["accepted_evidence_ids"], label="accepted evidence ids")
    available = (
        set(summary["proved_claim_evidence_ids"])
        | set(summary["failed_path_evidence_ids"])
        | set(summary["counterexample_evidence_ids"])
    )
    if not set(evidence_ids) <= available:
        raise ContinuousSupervisorError("lane decision accepted unknown evidence")
    return {
        "lane_id": summary["lane_id"],
        "thread_id": summary["thread_id"],
        "route_id": summary["route_id"],
        "verdict": verdict,
        "next_action": action,
        "redirect_to_lane_id": redirect_to_lane_id,
        "reason": _text(raw["reason"], label="lane decision reason"),
        "fatal_doubt": fatal_doubt,
        "next_test": next_test,
        "accepted_evidence_ids": evidence_ids,
    }


def validate_cohort_report(
    value: Mapping[str, Any], *, snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _exact_object(
        value,
        {
            "schema_version",
            "review_id",
            "snapshot_sha256",
            "lane_decisions",
            "cross_lane_synthesis",
        },
        label="cohort review report",
    )
    if raw["schema_version"] != COHORT_REPORT_SCHEMA:
        raise ContinuousSupervisorError("cohort report schema is invalid")
    if raw["review_id"] != snapshot.get("review_id") or raw[
        "snapshot_sha256"
    ] != snapshot.get("snapshot_sha256"):
        raise ContinuousSupervisorError("cohort report binding mismatch")
    decisions_raw = raw["lane_decisions"]
    if not isinstance(decisions_raw, list):
        raise ContinuousSupervisorError("cohort lane decisions must be a list")
    summaries = {item["lane_id"]: item for item in snapshot.get("lane_summaries", [])}
    if len(decisions_raw) != len(summaries):
        raise ContinuousSupervisorError("cohort report omitted or added a lane")
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_decision in decisions_raw:
        if not isinstance(raw_decision, Mapping):
            raise ContinuousSupervisorError("cohort lane decision is malformed")
        lane_id = raw_decision.get("lane_id")
        if lane_id not in summaries or lane_id in seen:
            raise ContinuousSupervisorError("cohort report lane identity is invalid")
        seen.add(str(lane_id))
        decisions.append(
            validate_lane_decision(raw_decision, summary=summaries[str(lane_id)])
        )
    decisions_by_lane = {item["lane_id"]: item for item in decisions}
    histories = {item["lane_id"]: item for item in snapshot.get("lane_history", [])}
    if set(histories) != set(summaries):
        raise ContinuousSupervisorError("cohort snapshot lane history is incomplete")
    for lane_id, decision in decisions_by_lane.items():
        history = _normalize_lane_history(
            histories[lane_id], summary=summaries[lane_id]
        )
        if history["unchanged_from_previous"] and decision["verdict"] == "green":
            raise ContinuousSupervisorError("unchanged lane cannot receive green")
        if (
            history["unchanged_from_previous"]
            and history["prior_verdict"] == "yellow"
            and decision["verdict"] == "yellow"
        ):
            raise ContinuousSupervisorError(
                "second unchanged yellow must be red or unclear"
            )
        target_id = decision["redirect_to_lane_id"]
        if target_id is not None:
            target = decisions_by_lane.get(target_id)
            if (
                target is None
                or target["verdict"] not in {"green", "yellow"}
                or target["next_action"]
                not in {"resume_same", "restart_same", "verify"}
            ):
                raise ContinuousSupervisorError(
                    "redirect target must be a viable lane in the frozen cohort"
                )
    verifying = [item for item in decisions if item["next_action"] == "verify"]
    if len(verifying) > 1 or (
        verifying
        and any(
            item["next_action"] != "retire"
            for item in decisions
            if item not in verifying
        )
    ):
        raise ContinuousSupervisorError(
            "candidate verification must be unique and retire every other lane"
        )
    normalized = {
        "schema_version": COHORT_REPORT_SCHEMA,
        "review_id": snapshot["review_id"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "lane_decisions": sorted(decisions, key=lambda item: item["lane_id"]),
        "cross_lane_synthesis": _text(
            raw["cross_lane_synthesis"], label="cross-lane synthesis"
        ),
    }
    if len(canonical_json_bytes(normalized)) > MAX_REPORT_BYTES:
        raise ContinuousSupervisorError("cohort report exceeds its byte bound")
    return normalized


def _decision_from_effective_actions(
    normalized: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    actions = [item["next_action"] for item in decisions]
    if "verify" in actions:
        global_action = "verify_candidate"
    elif any(
        action in {"resume_same", "restart_same", "redirect"} for action in actions
    ):
        global_action = "continue_cohort"
    elif decisions and all(item["next_action"] == "retire" for item in decisions):
        if any(item["verdict"] == "unclear" for item in decisions):
            global_action = "operational_blocked"
        else:
            global_action = "seek_advisor"
    elif not decisions:
        global_action = "operational_blocked"
    else:
        global_action = "operational_blocked"
    if global_action not in GLOBAL_ACTIONS:
        raise AssertionError("derived an unknown continuous-supervisor action")
    material = {
        "schema_version": "rethlas_cohort_review_decision_v1",
        "review_id": normalized["review_id"],
        "snapshot_sha256": normalized["snapshot_sha256"],
        "global_action": global_action,
        "lane_actions": [
            {
                key: item[key]
                for key in (
                    "lane_id",
                    "thread_id",
                    "route_id",
                    "verdict",
                    "next_action",
                    "redirect_to_lane_id",
                    "fatal_doubt",
                    "next_test",
                    "accepted_evidence_ids",
                )
            }
            for item in decisions
        ],
    }
    return {**material, "decision_sha256": content_sha256(material)}


def derive_global_action(
    report: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    resume_eligible_lane_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Derive the legacy host action while preferring reusable parked context."""

    normalized = validate_cohort_report(report, snapshot=snapshot)
    eligible = {
        _safe_id(lane_id, label="resume-eligible lane id")
        for lane_id in resume_eligible_lane_ids
    }
    known_lane_ids = {str(item["lane_id"]) for item in normalized["lane_decisions"]}
    if not eligible <= known_lane_ids:
        raise ContinuousSupervisorError(
            "resume-eligible lanes are outside the frozen cohort"
        )
    decisions = [
        (
            {**item, "next_action": "resume_same"}
            if item["next_action"] == "restart_same" and item["lane_id"] in eligible
            else item
        )
        for item in normalized["lane_decisions"]
    ]
    return _decision_from_effective_actions(normalized, decisions)


def derive_resource_conserving_action(
    report: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    resume_eligible_lane_ids: Sequence[str] = (),
    restart_authorizations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Normalize continuation actions using host-observed context availability.

    A reusable lane always resumes.  A non-reusable lane can restart only when
    the host names one bounded infrastructure reason.  The reviewer report stays
    immutable and is preserved separately from this execution decision.
    """

    normalized = validate_cohort_report(report, snapshot=snapshot)
    known_lane_ids = {str(item["lane_id"]) for item in normalized["lane_decisions"]}
    eligible = {
        _safe_id(lane_id, label="resume-eligible lane id")
        for lane_id in resume_eligible_lane_ids
    }
    if not eligible <= known_lane_ids:
        raise ContinuousSupervisorError(
            "resume-eligible lanes are outside the frozen cohort"
        )
    raw_restart = {} if restart_authorizations is None else dict(restart_authorizations)
    restart_reasons: dict[str, str] = {}
    for lane_id, reason in raw_restart.items():
        normalized_lane_id = _safe_id(lane_id, label="restart-authorized lane id")
        if normalized_lane_id not in known_lane_ids:
            raise ContinuousSupervisorError(
                "restart-authorized lane is outside the frozen cohort"
            )
        if reason not in RESTART_AUTHORIZATION_REASONS:
            raise ContinuousSupervisorError("restart authorization reason is invalid")
        restart_reasons[normalized_lane_id] = reason
    if eligible & set(restart_reasons):
        raise ContinuousSupervisorError(
            "a lane cannot be both resume-eligible and restart-authorized"
        )

    summaries = {
        str(item["lane_id"]): item for item in snapshot.get("lane_summaries", [])
    }
    effective: list[dict[str, Any]] = []
    audit_lanes: list[dict[str, Any]] = []
    used_restart_reasons: set[str] = set()
    for item in normalized["lane_decisions"]:
        lane_id = str(item["lane_id"])
        requested = str(item["next_action"])
        action = requested
        basis = "reviewer_disposition"
        if requested in {"resume_same", "restart_same"}:
            if lane_id in eligible:
                action = "resume_same"
                basis = "reusable_parked_context"
            elif lane_id in restart_reasons:
                action = "restart_same"
                basis = restart_reasons[lane_id]
                used_restart_reasons.add(lane_id)
            else:
                raise ContinuousSupervisorError(
                    "lane continuation lacks reusable context or restart evidence"
                )
        effective.append({**item, "next_action": action})
        audit_lanes.append(
            {
                "lane_id": lane_id,
                "requested_action": requested,
                "effective_action": action,
                "basis": basis,
                "preserved_summary_sha256": lane_summary_content_sha256(
                    summaries[lane_id]
                ),
            }
        )
    if used_restart_reasons != set(restart_reasons):
        raise ContinuousSupervisorError(
            "restart authorization was supplied for a non-continuing lane"
        )

    decision = _decision_from_effective_actions(normalized, effective)
    audit_material = {
        "schema_version": RESOURCE_AUDIT_SCHEMA,
        "policy_sha256": resource_policy_contract()["policy_sha256"],
        "review_id": normalized["review_id"],
        "snapshot_sha256": normalized["snapshot_sha256"],
        "lane_actions": sorted(audit_lanes, key=lambda item: item["lane_id"]),
    }
    audit = {
        **audit_material,
        "audit_sha256": content_sha256(audit_material),
    }
    return {"decision": decision, "resource_audit": audit}


def select_summary_interrupt_targets(
    lanes: Sequence[Mapping[str, Any]],
    *,
    deadline_reached: bool,
    root_thread_id: str,
) -> list[dict[str, str]]:
    """Select only active, unsummarized child stragglers after the deadline."""

    if type(deadline_reached) is not bool:
        raise ContinuousSupervisorError("summary deadline evidence is invalid")
    root = _safe_id(root_thread_id, label="summary interrupt root thread id")
    if not isinstance(lanes, Sequence) or isinstance(lanes, (str, bytes)):
        raise ContinuousSupervisorError("summary interrupt lanes must be a sequence")
    if len(lanes) > MAX_LANES:
        raise ContinuousSupervisorError("summary interrupt set exceeds three lanes")

    targets: list[dict[str, str]] = []
    seen_lane_ids: set[str] = set()
    seen_thread_ids: set[str] = set()
    for raw in lanes:
        if not isinstance(raw, Mapping):
            raise ContinuousSupervisorError("summary interrupt lane is malformed")
        lane_id = _safe_id(raw.get("lane_id"), label="summary interrupt lane id")
        thread_id = _safe_id(
            raw.get("thread_id"), label="summary interrupt thread id"
        )
        if lane_id in seen_lane_ids or thread_id in seen_thread_ids:
            raise ContinuousSupervisorError("summary interrupt lanes are duplicated")
        seen_lane_ids.add(lane_id)
        seen_thread_ids.add(thread_id)
        state = raw.get("state")
        observed_status = raw.get("observed_status")
        active_turn_id = raw.get("active_turn_id")
        summary_present = raw.get("summary_present")
        if (
            state not in STATE_MACHINE_TRANSITIONS["lane"]
            or observed_status
            not in {"active", "idle", "notLoaded", "systemError"}
            or type(summary_present) is not bool
            or (
                active_turn_id is not None
                and (
                    not isinstance(active_turn_id, str)
                    or SAFE_ID_RE.fullmatch(active_turn_id) is None
                )
            )
        ):
            raise ContinuousSupervisorError("summary interrupt lane state is invalid")
        if not deadline_reached:
            continue
        if (
            state == "summary_requested"
            and observed_status == "active"
            and active_turn_id is not None
            and not summary_present
        ):
            if thread_id == root:
                raise ContinuousSupervisorError(
                    "summary deadline can never interrupt the protected root"
                )
            targets.append(
                {
                    "lane_id": lane_id,
                    "thread_id": thread_id,
                    "turn_id": active_turn_id,
                }
            )
    return sorted(targets, key=lambda item: item["lane_id"])


def policy_contract() -> dict[str, Any]:
    material = {"schema_version": "rethlas_continuous_supervisor_policy_v1", **POLICY}
    return {**material, "policy_sha256": content_sha256(material)}
