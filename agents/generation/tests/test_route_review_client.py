from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agents.generation.mcp import review_client
from agents.generation.mcp import server
from agents.generation.mcp import server_driver
from agents.generation.mcp.proof_context import parse_blueprint
from agents.review import contracts
from agents.review import critic


REVIEW_ID = "review_" + "1" * 32
POLICY_SHA = "c" * 64
STATEMENT_TEXT = "Exact authoritative target."
BLUEPRINT_TEXT = (
    "# theorem thm:target\n\n## statement\nTarget.\n\n## proof\nCandidate.\n"
)
_BLUEPRINT_MANIFEST = parse_blueprint(BLUEPRINT_TEXT)
BLUEPRINT_ITEMS = [
    {"label": item.label, "item_id": item.item_id, "claim_sha256": item.digest}
    for item in _BLUEPRINT_MANIFEST.items
]


@pytest.fixture(autouse=True)
def _resolved_cost_policy_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = contracts.canonical_json_bytes(
        {
            "schema_version": review_client.RESOLVED_COST_POLICY_SCHEMA,
            "policy": "owner_gated",
            "authority": "owner_wrapper",
        }
    ).decode("utf-8")
    monkeypatch.setenv(review_client.RESOLVED_COST_POLICY_JSON_ENV, manifest)
    monkeypatch.setenv(
        review_client.RESOLVED_COST_POLICY_SHA256_ENV,
        hashlib.sha256(manifest.encode()).hexdigest(),
    )


def active_route(route_id: str = "route-a") -> dict[str, Any]:
    seed = {
        "route_id": route_id,
        "core_bridge": "Bridge A",
        "obligations": ["Prove Bridge A."],
        "commitment_record_id": "mem_route_commitment",
        "commitment_batch_id": "batch_" + "a" * 64,
        "commitment_timestamp_utc": "2026-08-10T22:40:00+00:00",
    }
    return {
        **seed,
        "commitment_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(seed)
        ).hexdigest(),
    }


def fallback_route() -> dict[str, Any]:
    seed = {
        "route_id": "route-fallback",
        "core_bridge": "Bridge B",
        "obligations": ["Test Bridge B."],
        "commitment_record_id": "mem_fallback_commitment",
        "commitment_batch_id": "batch_" + "b" * 64,
        "commitment_timestamp_utc": "2026-08-10T22:41:00+00:00",
        "evidence_record_ids": ["mem_1"],
    }
    return {
        **seed,
        "commitment_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(seed)
        ).hexdigest(),
    }


def targeted_ticket() -> dict[str, Any]:
    claim = {
        "blueprint_item_label": BLUEPRINT_ITEMS[0]["label"],
        "claim_sha256": BLUEPRINT_ITEMS[0]["claim_sha256"],
        "reason": "This exact item is load-bearing.",
    }
    seed = {
        "review_id": REVIEW_ID,
        "snapshot_sha256": "2" * 64,
        "route_id": "route-a",
        "blueprint_sha256": hashlib.sha256(BLUEPRINT_TEXT.encode()).hexdigest(),
        "blueprint_item_id": BLUEPRINT_ITEMS[0]["item_id"],
        "claim": claim,
    }
    return {
        "schema_version": contracts.TARGETED_CLAIM_TICKET_SCHEMA,
        "ticket_id": "claim_"
        + hashlib.sha256(contracts.canonical_json_bytes(seed)).hexdigest()[:32],
        **seed,
        "verification_mode": "targeted_nonpublishing",
        "publication_authority": False,
        "whole_blueprint_verdict_authority": False,
    }


def snapshot() -> dict[str, Any]:
    body = {"claim": "the exact bridge", "status": "proved"}
    return {
        "schema_version": contracts.REVIEW_SNAPSHOT_SCHEMA,
        "run_id": "run-1",
        "problem_id": "frontier/example",
        "cycle_id": "cycle-1",
        "cycle": "minute60",
        "review_ordinal": 1,
        "due_at_utc": "2026-08-10T23:00:00+00:00",
        "root_thread_id": "thread-1",
        "root_turn_id": "turn-1",
        "root_terminal_sha256": "9" * 64,
        "route_id": "route-a",
        "active_route": active_route(),
        "statement_sha256": hashlib.sha256(STATEMENT_TEXT.encode()).hexdigest(),
        "statement_text": STATEMENT_TEXT,
        "blueprint_sha256": hashlib.sha256(BLUEPRINT_TEXT.encode()).hexdigest(),
        "blueprint_text": BLUEPRINT_TEXT,
        "blueprint_items": deepcopy(BLUEPRINT_ITEMS),
        "fallback_route_candidates": [],
        "frontier_records": [
            {
                "record_id": "mem_1",
                "kind": "proof_steps",
                "body": body,
                "channel": "proof_steps",
                "batch_id": "batch_" + "1" * 64,
                "timestamp_utc": "2026-08-10T22:45:00+00:00",
            }
        ],
        "progress_records": [],
        "prior_official_review": None,
    }


def handoff() -> dict[str, Any]:
    return {
        "schema_version": contracts.CONTEXT_HANDOFF_SCHEMA,
        "purpose": "context_guard",
        "run_id": "run-1",
        "problem_id": "frontier/example",
        "from_thread_epoch": "epoch-1",
        "statement_sha256": "a" * 64,
        "blueprint_sha256": "b" * 64,
        "cadence": {
            "phase": "work_0_60",
            "cycle_started_at_utc": "2026-08-10T22:29:48+00:00",
            "minute60_at_utc": "2026-08-10T23:29:48+00:00",
            "minute120_at_utc": "2026-08-11T00:29:48+00:00",
            "close_at_utc": "2026-08-11T00:56:48+00:00",
            "hard_stop_at_utc": "2026-08-11T00:59:48+00:00",
        },
        "active_route": {"route_id": "route-a", "core_bridge": "Bridge A"},
        "last_review": None,
        "new_record_ids": ["mem_1"],
        "yellow_streak": 0,
        "route_frozen": False,
        "pending": {
            "verification_ticket_id": None,
            "advisor_checkpoint_id": None,
        },
        "obligations": ["Test bridge A"],
        "next_action": {"description": "Test bridge A", "test": "derive estimate"},
    }


def review_response(
    *,
    operation: str,
    request: dict[str, Any],
    state: str = "prepared",
) -> dict[str, Any]:
    if state == "execution_unknown":
        execution = {
            "state": "execution_unknown",
            "report": None,
            "error": "dispatch completion is ambiguous",
            "retry_allowed": False,
            "attempt": 1,
        }
        decision = None
    elif state == "completed":
        execution = {
            "state": "completed",
            "report": {"opaque_here": "host already strictly validated"},
            "error": None,
            "retry_allowed": False,
            "attempt": 1,
        }
        decision = {
            "route_id": request["snapshot"]["route_id"],
            "raw_verdict": "yellow",
            "effective_verdict": "yellow",
            "yellow_streak": 1,
            "critic_confirmed_progress_ids": [],
            "auto_red": False,
            "auto_red_reason": None,
            "route_frozen": False,
            "allowed_action": "one_bounded_cycle_on_fatal_doubt",
        }
    else:
        execution = None
        decision = None
    return {
        "schema_version": review_client.ADAPTER_RESPONSE_SCHEMA,
        "operation": operation,
        "review_id": request["review_id"],
        "request_sha256": request["request_sha256"],
        "snapshot_sha256": request["snapshot_sha256"],
        "state": state,
        "idempotent": False,
        "execution": execution,
        "decision": decision,
    }


def test_prepare_sends_one_canonical_digest_bound_request_on_stdin_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], int]] = []

    def fake_invoke(
        command: str, payload: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]:
        calls.append((command, payload, timeout_seconds))
        request = payload["payload"]["request"]
        return review_response(operation="review_prepare", request=request)

    monkeypatch.setattr(review_client, "_invoke_adapter", fake_invoke)
    request = critic.build_review_request(
        review_id=REVIEW_ID,
        snapshot=snapshot(),
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        policy_sha256=POLICY_SHA,
    )
    result = review_client.route_review_prepare(
        request=request,
    )

    assert result["state"] == "prepared"
    assert len(calls) == 1
    command, envelope, timeout = calls[0]
    assert command == "review-prepare"
    assert timeout == 30
    assert envelope["schema_version"] == review_client.ADAPTER_COMMAND_SCHEMA
    assert envelope["command"] == "review_prepare"
    request = envelope["payload"]["request"]
    assert critic.validate_review_request(request) == request
    assert request["expected_model"] == "gpt-5.6-sol"
    assert request["reasoning_effort"] == "max"
    assert request["reviewer_contract"]["tools"] == []
    assert request["retry_allowed"] is False


def test_targeted_wrong_commit_requires_and_sends_exact_transition_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = targeted_ticket()
    receipt_seed = {
        "schema_version": review_client.TARGETED_RECEIPT_SCHEMA,
        "ticket_id": ticket["ticket_id"],
        "review_id": REVIEW_ID,
        "snapshot_sha256": "2" * 64,
        "route_id": "route-a",
        "blueprint_sha256": ticket["blueprint_sha256"],
        "blueprint_item_id": ticket["blueprint_item_id"],
        "blueprint_item_label": ticket["claim"]["blueprint_item_label"],
        "claim_sha256": ticket["claim"]["claim_sha256"],
        "verification_deadline_utc": "2026-08-11T23:59:48+00:00",
        "verification_status": "final",
        "verdict": "wrong",
        "verification_report": "counterexample",
        "repair_hints": [],
        "checked_item_ids": [ticket["blueprint_item_id"]],
        "context_attestation": {},
        "verification_limits": {
            "context_max_chars": 200_000,
            "max_expansion_rounds": 2,
            "max_expanded_proofs": 8,
            "max_expanded_proof_chars": 200_000,
        },
        "proof_context": {
            "schema_version": "rethlas_publication_proof_context_v3",
            "source_sha256": "9" * 64,
            "proof_item_schema_version": 1,
            "proof_context_schema_version": 2,
            "aggregate_context_schema_version": 1,
            "adaptive_aggregate_context_schema_version": 2,
        },
        "execution_binding": {
            "schema_version": "rethlas_targeted_execution_binding_v3",
            "service_version": "test-0.5.0",
            "closure_sha256": "3" * 64,
            "prompt_contract_sha256": "4" * 64,
            "backend": {
                "adapter": "codex_cli",
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "launch_model": "gpt-5.6-sol",
                "reasoning_effort": "max",
            },
            "prompt_limits": {
                "max_prompt_bytes": 500_000,
                "max_total_prompt_bytes": 5_000_000,
                "max_request_bytes": 25_000_000,
                "max_proof_chars": 2_000_000,
                "max_statement_chars": 100_000,
                "max_output_bytes": 1_000_000,
                "max_targeted_receipt_bytes": 131_072,
                "request_timeout_seconds": 3_500,
                "adapter_timeout_seconds": 3_600,
                "mcp_tool_timeout_seconds": 3_600,
            },
        },
        "publication_authority": False,
        "whole_blueprint_verdict_authority": False,
    }
    verifier_receipt = {
        **receipt_seed,
        "receipt_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(receipt_seed)
        ).hexdigest(),
    }
    official = {
        "schema_version": review_client.PUBLICATION_RECEIPT_SCHEMA,
        "publication_state": "official",
        "problem_id": "frontier/example",
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "batch_id": "batch_" + "a" * 64,
        "record_id": "mem_review_official",
        "timestamp_utc": "2026-08-11T23:30:00+00:00",
        "checkpoint_sha256": "3" * 64,
        "record_sha256": "4" * 64,
    }
    transition_seed = {
        "schema_version": review_client.ROUTE_TRANSITION_PUBLICATION_RECEIPT_SCHEMA,
        "problem_id": "frontier/example",
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "from_route_id": "route-a",
        "to_route_id": None,
        "batch_id": "batch_" + "b" * 64,
        "record_ids": ["mem_route_frozen"],
        "timestamp_utc": "2026-08-11T23:30:01+00:00",
        "checkpoint_sha256": "5" * 64,
        "transition_sha256": "6" * 64,
    }
    transition = {
        **transition_seed,
        "receipt_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(transition_seed)
        ).hexdigest(),
    }
    calls: list[dict[str, Any]] = []

    def invoke(command: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append(payload)
        request = critic.build_review_request(
            review_id=REVIEW_ID,
            snapshot=snapshot(),
            expected_model="gpt-5.6-sol",
            reasoning_effort="max",
            policy_sha256=POLICY_SHA,
        )
        response = review_response(
            operation="targeted_verification_commit", request=request, state="completed"
        )
        response["state"] = "closed"
        response["request_sha256"] = "1" * 64
        response["snapshot_sha256"] = "2" * 64
        return response

    monkeypatch.setattr(review_client, "_invoke_adapter", invoke)
    with pytest.raises(
        review_client.ReviewAdapterError,
        match="requires exactly one route transition",
    ):
        review_client.route_review_targeted_verification_commit(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
            outcome_state="completed",
            verification_receipt=verifier_receipt,
            error_sha256=None,
            publication_receipt=official,
            route_transition_publication_receipt=None,
        )
    assert calls == []
    review_client.route_review_targeted_verification_commit(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
        outcome_state="completed",
        verification_receipt=verifier_receipt,
        error_sha256=None,
        publication_receipt=official,
        route_transition_publication_receipt=transition,
    )
    assert calls[0]["payload"]["route_transition_publication_receipt"] == transition


def test_owner_host_drive_prepare_derives_manifest_without_model_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_REVIEW_DRIVER_AUTHORITY", "owner_host_master_v1")
    monkeypatch.setenv("RETHLAS_REVIEW_CONTROL_TOKEN", "m" * 64)
    frontier = {
        "schema_version": "rethlas_review_frontier_status_v1",
        "review_id": REVIEW_ID,
        "cycle_id": "cycle-1",
        "cycle": "minute60",
        "review_ordinal": 1,
        "due_at_utc": "2026-08-10T23:00:00+00:00",
        "root_thread_id": "thread-1",
        "root_turn_id": "turn-1",
        "root_terminal_sha256": "9" * 64,
        "route_id": "route-a",
        "active_route": active_route(),
        "fallback_route_candidates": [],
        "frontier_record_ids": ["mem_1"],
        "progress_record_ids": [],
        "active_route_id": "route-a",
        "manifest_sha256": "8" * 64,
    }
    monkeypatch.setattr(
        server_driver.server, "review_frontier_status", lambda **kwargs: frontier
    )
    prepared_calls: list[dict[str, Any]] = []

    def prepare(**kwargs: Any) -> dict[str, Any]:
        prepared_calls.append(kwargs)
        return {
            "state": "prepared",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
        }

    monkeypatch.setattr(server_driver.server, "route_review_prepare", prepare)
    result = server_driver.drive_step(
        {
            "schema_version": server_driver.INPUT_SCHEMA,
            "operation": "prepare",
            "cycle_id": "cycle-1",
            "cycle": "minute60",
            "review_ordinal": 1,
        }
    )
    assert result["state"] == "prepared"
    assert prepared_calls == [
        {
            "review_id": REVIEW_ID,
            "cycle_id": "cycle-1",
            "cycle": "minute60",
            "review_ordinal": 1,
            "frontier_manifest_sha256": "8" * 64,
            "frontier_record_ids": ["mem_1"],
            "progress_record_ids": [],
        }
    ]
    assert set(inspect.signature(server.build_mcp_app).parameters) == set()


def test_owner_host_drive_disposition_returns_only_official_handoff_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_REVIEW_DRIVER_AUTHORITY", "owner_host_master_v1")
    monkeypatch.setenv("RETHLAS_REVIEW_CONTROL_TOKEN", "m" * 64)
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    decision = {
        "route_id": "route-a",
        "raw_verdict": "green",
        "effective_verdict": "green",
        "yellow_streak": 0,
        "critic_confirmed_progress_ids": ["mem_1"],
        "auto_red": False,
        "auto_red_reason": None,
        "route_frozen": False,
        "allowed_action": "continue_to_next_milestone",
    }
    body = {
        "state": "official_published",
        "decision": decision,
        "report": {
            "load_bearing_claim": None,
            "answers": {
                "next_milestone": {
                    "description": "Prove the exact bridge.",
                    "test": "derive the estimate",
                }
            },
        },
        "active_route": active_route(),
        "route_transition": {
            "next_route_id": None,
            "fallback_evidence_record_ids": [],
        },
    }
    monkeypatch.setattr(
        server_driver.server,
        "_find_review_memory",
        lambda *args, **kwargs: ({"record_id": "mem_official"}, deepcopy(body)),
    )
    monkeypatch.setattr(
        server_driver.server,
        "route_review_status",
        lambda **kwargs: {"state": "closed", "decision": deepcopy(decision)},
    )
    result = server_driver.drive_step(
        {
            "schema_version": server_driver.INPUT_SCHEMA,
            "operation": "disposition",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
        }
    )
    disposition = result["artifact"]["disposition"]
    assert result["state"] == "closed"
    assert set(disposition) == {
        "schema_version",
        "review_id",
        "request_sha256",
        "snapshot_sha256",
        "decision",
        "active_route",
        "frozen_route_id",
        "route_transition_publication_receipt",
        "next_milestone",
        "evidence_record_ids",
        "requires_targeted_verification",
    }
    assert disposition["schema_version"] == server_driver.DISPOSITION_SCHEMA
    assert disposition["active_route"] == active_route()
    assert disposition["evidence_record_ids"] == ["mem_1"]
    assert disposition["requires_targeted_verification"] is False
    assert "prompt" not in json.dumps(result)


def test_owner_host_drive_disposition_binds_red_fallback_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_REVIEW_DRIVER_AUTHORITY", "owner_host_master_v1")
    monkeypatch.setenv("RETHLAS_REVIEW_CONTROL_TOKEN", "m" * 64)
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    candidate = fallback_route()
    decision = {
        "route_id": "route-a",
        "raw_verdict": "red",
        "effective_verdict": "red",
        "yellow_streak": 0,
        "critic_confirmed_progress_ids": [],
        "auto_red": False,
        "auto_red_reason": None,
        "route_frozen": True,
        "allowed_action": "freeze_route",
    }
    body = {
        "state": "official_published",
        "decision": decision,
        "report": {"load_bearing_claim": None, "answers": {}},
        "active_route": active_route(),
        "fallback_route_candidates": [candidate],
        "route_transition": {
            "next_route_id": candidate["route_id"],
            "fallback_evidence_record_ids": list(candidate["evidence_record_ids"]),
        },
    }
    monkeypatch.setattr(
        server_driver.server,
        "_find_review_memory",
        lambda *args, **kwargs: ({"record_id": "mem_official"}, deepcopy(body)),
    )
    monkeypatch.setattr(
        server_driver.server,
        "route_review_status",
        lambda **kwargs: {"state": "closed", "decision": deepcopy(decision)},
    )
    receipt = {"receipt_sha256": "7" * 64}
    monkeypatch.setattr(
        server_driver.server,
        "_route_transition_projection_receipt",
        lambda **kwargs: receipt,
    )
    result = server_driver.drive_step(
        {
            "schema_version": server_driver.INPUT_SCHEMA,
            "operation": "disposition",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
        }
    )
    disposition = result["artifact"]["disposition"]
    assert disposition["frozen_route_id"] == "route-a"
    assert disposition["active_route"] == candidate
    assert disposition["route_transition_publication_receipt"] == receipt
    assert disposition["next_milestone"]["test"] == "Test Bridge B."
    assert disposition["evidence_record_ids"] == ["mem_1"]


def test_owner_host_drive_disposition_preserves_red_frozen_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_REVIEW_DRIVER_AUTHORITY", "owner_host_master_v1")
    monkeypatch.setenv("RETHLAS_REVIEW_CONTROL_TOKEN", "m" * 64)
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    decision = {
        "route_id": "route-a",
        "raw_verdict": "red",
        "effective_verdict": "red",
        "yellow_streak": 0,
        "critic_confirmed_progress_ids": [],
        "auto_red": False,
        "auto_red_reason": None,
        "route_frozen": True,
        "allowed_action": "freeze_route",
    }
    body = {
        "state": "official_published",
        "decision": decision,
        "report": {"load_bearing_claim": None, "answers": {}},
        "active_route": active_route(),
        "fallback_route_candidates": [],
        "route_transition": {
            "next_route_id": None,
            "fallback_evidence_record_ids": [],
        },
    }
    monkeypatch.setattr(
        server_driver.server,
        "_find_review_memory",
        lambda *args, **kwargs: ({"record_id": "mem_official"}, deepcopy(body)),
    )
    monkeypatch.setattr(
        server_driver.server,
        "route_review_status",
        lambda **kwargs: {"state": "closed", "decision": deepcopy(decision)},
    )
    frozen_receipt = {"to_route_id": None, "record_ids": ["mem_frozen"]}
    monkeypatch.setattr(
        server_driver.server,
        "_route_transition_projection_receipt",
        lambda **kwargs: frozen_receipt,
    )
    result = server_driver.drive_step(
        {
            "schema_version": server_driver.INPUT_SCHEMA,
            "operation": "disposition",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
        }
    )
    disposition = result["artifact"]["disposition"]
    assert disposition["frozen_route_id"] == "route-a"
    assert disposition["active_route"] is None
    assert disposition["next_milestone"] is None
    assert disposition["route_transition_publication_receipt"] == frozen_receipt


def test_review_drive_is_hidden_and_requires_owner_master_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RETHLAS_REVIEW_DRIVER_AUTHORITY", raising=False)
    monkeypatch.delenv("RETHLAS_REVIEW_CONTROL_TOKEN", raising=False)
    with pytest.raises(server_driver.DriveError, match="owner-host authority"):
        server_driver.drive_step({"operation": "prepare"})
    source = inspect.getsource(server.build_mcp_app)
    assert "server_driver" not in source
    assert "drive-step" not in source


def test_review_drive_cli_loads_under_isolated_python_without_cwd_imports() -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "RETHLAS_REVIEW_DRIVER_AUTHORITY",
            "RETHLAS_REVIEW_CONTROL_TOKEN",
        }
    }
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(server_driver.__file__)), "drive-step"],
        input=b'{"operation":"prepare"}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert b"owner-host authority" in completed.stderr


def test_review_drive_preloads_the_official_mcp_sdk_before_snapshot_paths() -> None:
    driver_path = str(Path(server_driver.__file__).resolve())
    code = (
        "import runpy; "
        f"ns=runpy.run_path({driver_path!r}, run_name='_rethlas_driver_probe'); "
        "server=ns['server']; "
        "assert server.FastMCP is not None; "
        "print(server.FastMCP.__module__)"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout.decode().strip().startswith("mcp.server.")


def test_review_memory_is_explicitly_control_only_not_mathematical_evidence() -> None:
    request = critic.build_review_request(
        review_id=REVIEW_ID,
        snapshot=snapshot(),
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        policy_sha256=POLICY_SHA,
    )
    body = server._prepared_review_body(request)
    assert body["record_role"] == "control_audit_only"
    assert body["mathematical_evidence_authority"] is False


@pytest.mark.parametrize("channel", ["route_reviews", "targeted_verifications"])
def test_public_memory_writes_cannot_forge_control_records(channel: str) -> None:
    with pytest.raises(ValueError, match="reserved for trusted control"):
        server.memory_append_batch(
            "frontier/example",
            [{"channel": channel, "record": {"state": "official_published"}}],
        )
    with pytest.raises(ValueError, match="reserved for trusted control"):
        server.memory_append(
            "frontier/example", channel, {"state": "official_published"}
        )


def test_wait_accepts_execution_unknown_without_retry_or_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = critic.build_review_request(
        review_id=REVIEW_ID,
        snapshot=snapshot(),
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        policy_sha256=POLICY_SHA,
    )
    calls = 0

    def fake_invoke(
        command: str, payload: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert command == "review-wait"
        assert set(payload["payload"]) == {
            "review_id",
            "request_sha256",
            "snapshot_sha256",
        }
        assert timeout_seconds == 330
        return review_response(
            operation="review_wait", request=request, state="execution_unknown"
        )

    monkeypatch.setattr(review_client, "_invoke_adapter", fake_invoke)
    result = review_client.route_review_wait(
        review_id=REVIEW_ID,
        request_sha256=request["request_sha256"],
        snapshot_sha256=request["snapshot_sha256"],
    )

    assert calls == 1
    assert result["state"] == "execution_unknown"
    assert result["decision"] is None
    assert result["execution"]["retry_allowed"] is False


def test_completed_status_requires_host_derived_consistent_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = critic.build_review_request(
        review_id=REVIEW_ID,
        snapshot=snapshot(),
        expected_model="gpt-5.6-sol",
        reasoning_effort="max",
        policy_sha256=POLICY_SHA,
    )

    def fake_invoke(
        command: str, payload: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]:
        return review_response(operation="review_status", request=request, state="completed")

    monkeypatch.setattr(review_client, "_invoke_adapter", fake_invoke)
    result = review_client.route_review_status(
        review_id=REVIEW_ID,
        request_sha256=request["request_sha256"],
        snapshot_sha256=request["snapshot_sha256"],
    )
    assert result["decision"]["effective_verdict"] == "yellow"

    invalid = review_response(
        operation="review_status", request=request, state="completed"
    )
    invalid["decision"]["route_frozen"] = True
    monkeypatch.setattr(review_client, "_invoke_adapter", lambda *args, **kwargs: invalid)
    with pytest.raises(review_client.ReviewAdapterError, match="route_frozen"):
        review_client.route_review_status(
            review_id=REVIEW_ID,
            request_sha256=request["request_sha256"],
            snapshot_sha256=request["snapshot_sha256"],
        )


def test_review_due_status_is_exact_and_host_binds_active_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], int]] = []

    def fake_invoke(
        command: str, payload: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]:
        calls.append((command, payload, timeout_seconds))
        return {
            "schema_version": review_client.ADAPTER_RESPONSE_SCHEMA,
            "operation": "review_due_status",
            "review_id": REVIEW_ID,
            "cycle_id": "cycle-1",
            "cycle": "minute120",
            "review_ordinal": 2,
            "due_at_utc": "2026-08-10T23:30:00+00:00",
            "state": "completed",
            "active_route_id": "route-a",
            "root_thread_id": "thread-1",
            "root_turn_id": "turn-1",
            "root_terminal_sha256": "9" * 64,
        }

    monkeypatch.setattr(review_client, "_invoke_adapter", fake_invoke)
    result = review_client.review_due_status(
        cycle_id="cycle-1",
        cycle="minute120",
        review_ordinal=2,
    )
    assert result["due_at_utc"] == "2026-08-10T23:30:00+00:00"
    assert result["active_route_id"] == "route-a"
    assert calls == [
        (
            "review-status",
            {
                "schema_version": review_client.ADAPTER_COMMAND_SCHEMA,
                "command": "review_status",
                "payload": {
                    "operation": "review_due_status",
                    "cycle_id": "cycle-1",
                    "cycle": "minute120",
                    "review_ordinal": 2,
                },
            },
            30,
        )
    ]

    bad = deepcopy(result)
    bad["due_at_utc"] = "2026-08-10T19:30:00-04:00"
    monkeypatch.setattr(review_client, "_invoke_adapter", lambda *args, **kwargs: bad)
    with pytest.raises(review_client.ReviewAdapterError, match="canonical UTC"):
        review_client.review_due_status(
            cycle_id="cycle-1",
            cycle="minute120",
            review_ordinal=2,
        )


def test_reasoning_phase_preflight_enforces_review_only_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def response(tool_name: str, permitted: bool) -> dict[str, Any]:
        return {
            "schema_version": review_client.ADAPTER_RESPONSE_SCHEMA,
            "operation": "reasoning_phase_preflight",
            "run_id": "run-1",
            "problem_id": "frontier/example",
            "phase": "review_1",
            "allowed_action": "independent_review_only",
            "active_review_id": None,
            "review_due_at_utc": "2026-08-10T23:00:00+00:00",
            "review_due_monotonic": 20_000.0,
            "hard_stop_at_utc": "2026-08-11T00:00:00+00:00",
            "hard_stop_monotonic": 23_600.0,
            "tool_permitted": permitted,
        }

    monkeypatch.setattr(
        review_client,
        "_invoke_adapter",
        lambda *args, **kwargs: response("route_review_prepare", True),
    )
    assert review_client.reasoning_phase_preflight(
        tool_name="route_review_prepare"
    )["tool_permitted"] is True

    invalid_clock = response("route_review_prepare", True)
    invalid_clock["review_due_monotonic"] = None
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *args, **kwargs: invalid_clock
    )
    with pytest.raises(review_client.ReviewAdapterError, match="monotonic"):
        review_client.reasoning_phase_preflight(tool_name="route_review_prepare")

    monkeypatch.setattr(
        review_client,
        "_invoke_adapter",
        lambda *args, **kwargs: response("memory_search", True),
    )
    with pytest.raises(review_client.ReviewAdapterError, match="allowlist"):
        review_client.reasoning_phase_preflight(tool_name="memory_search")

    monkeypatch.setattr(
        review_client,
        "_invoke_adapter",
        lambda *args, **kwargs: response("memory_search", False),
    )
    assert review_client.reasoning_phase_preflight(
        tool_name="memory_search"
    )["tool_permitted"] is False


def test_generation_yield_admission_is_exact_and_handoff_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = ["mem_event", "mem_branch"]
    reason_sha = "3" * 64
    captured: dict[str, Any] = {}

    def fake_invoke(
        command: str, payload: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]:
        captured.update(
            {"command": command, "payload": payload, "timeout": timeout_seconds}
        )
        return {
            "schema_version": "rethlas_generation_yield_admission_v1",
            "operation": "generation_yield_prepare",
            "admission_id": "yieldadm_" + "1" * 32,
            "run_id": "run-1",
            "cycle_id": "cycle-1",
            "handoff_id": "handoff_" + "2" * 64,
            "content_sha256": "2" * 64,
            "to_thread_epoch": 2,
            "root_thread_id": "thread-1",
            "root_turn_id": "turn-1",
            "state": "waiting_cost_gate",
            "reason_sha256": reason_sha,
            "evidence_record_ids": evidence,
        }

    monkeypatch.setattr(review_client, "_invoke_adapter", fake_invoke)
    admission = review_client.generation_yield_prepare(
        state="waiting_cost_gate",
        reason_sha256=reason_sha,
        evidence_record_ids=evidence,
    )
    assert admission["handoff_id"] == "handoff_" + "2" * 64
    assert captured == {
        "command": "review-status",
        "payload": {
            "schema_version": review_client.ADAPTER_COMMAND_SCHEMA,
            "command": "review_status",
            "payload": {
                "operation": "generation_yield_prepare",
                "state": "waiting_cost_gate",
                "reason_sha256": reason_sha,
                "evidence_record_ids": evidence,
            },
        },
        "timeout": 30,
    }

    forged = deepcopy(admission)
    forged["content_sha256"] = "4" * 64
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *args, **kwargs: forged
    )
    with pytest.raises(review_client.ReviewAdapterError, match="handoff binding"):
        review_client.generation_yield_prepare(
            state="waiting_cost_gate",
            reason_sha256=reason_sha,
            evidence_record_ids=evidence,
        )


def test_handoff_prepare_and_get_are_content_addressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = handoff()
    expected_id = contracts.handoff_id(content)
    expected_sha = contracts.handoff_sha256(content)

    def fake_invoke(
        command: str, payload: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]:
        preparing = command == "context-handoff-prepare"
        if preparing:
            assert set(payload["payload"]) == {
                "operation",
                "purpose",
                "proposal",
                "assertions",
            }
            assert payload["payload"]["purpose"] == "context_guard"
            assert "pending" not in payload["payload"]["assertions"]
            assert payload["payload"]["proposal"]["pending"] == content["pending"]
        return {
            "schema_version": review_client.ADAPTER_RESPONSE_SCHEMA,
            "operation": payload["command"],
            "handoff_id": expected_id,
            "content_sha256": expected_sha,
            "state": "consumed" if command == "context-handoff-get" else "available",
            "idempotent": False,
            "content": (
                deepcopy(content)
                if command in {"context-handoff-prepare", "context-handoff-get"}
                else None
            ),
            "binding": None if preparing else {
                "run_id": "run-1",
                "cycle_id": "cycle-1",
                "thread_epoch": "epoch-2",
                "root_thread_id": "thread-2",
                "root_turn_id": "turn-2",
                "rehydration_state": (
                    "consumed" if command == "context-handoff-get" else "awaiting_rehydrate"
                ),
            },
        }

    monkeypatch.setattr(review_client, "_invoke_adapter", fake_invoke)
    prepared = review_client.context_handoff_prepare(
        purpose="context_guard",
        proposal={
            key: deepcopy(content[key])
            for key in (
                "active_route",
                "new_record_ids",
                "obligations",
                "next_action",
                "pending",
            )
        },
        assertions={
            key: deepcopy(content[key])
            for key in (
                "run_id",
                "problem_id",
                "statement_sha256",
                "blueprint_sha256",
                "last_review",
                "yellow_streak",
                "route_frozen",
            )
        },
    )
    fetched = review_client.context_handoff_get(
        handoff_id=expected_id,
        content_sha256=expected_sha,
        thread_epoch="epoch-2",
        root_thread_id="thread-2",
        root_turn_id="turn-2",
    )
    status = review_client.context_handoff_status(
        handoff_id=expected_id, content_sha256=expected_sha
    )

    assert prepared["handoff_id"] == expected_id
    assert fetched["content"] == contracts.validate_context_handoff(content)
    assert status["content"] is None


def test_attested_adapter_invocation_keeps_snapshot_out_of_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text("# trusted adapter\n", encoding="utf-8")
    adapter.chmod(0o600)
    digest = hashlib.sha256(adapter.read_bytes()).hexdigest()
    monkeypatch.setenv(review_client.ADAPTER_ENV_PATH, os.fspath(adapter))
    monkeypatch.setenv(review_client.ADAPTER_ENV_SHA256, digest)
    monkeypatch.setenv(review_client.CONTROL_TOKEN_ENV, "f" * 64)
    monkeypatch.setenv(review_client.ADAPTER_ENV_DB, os.fspath(tmp_path / "messages.sqlite3"))
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        captured["executed_bytes"] = Path(argv[3]).read_bytes()
        captured["input"] = kwargs["input_bytes"]
        captured["env"] = kwargs["env"]
        captured["directory"] = kwargs["directory"]
        response = {
            "schema_version": review_client.ADAPTER_RESPONSE_SCHEMA,
            "operation": "review_status",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "state": "running",
            "idempotent": False,
            "execution": None,
            "decision": None,
        }
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(response).encode(), stderr=b""
        )

    monkeypatch.setattr(review_client, "_run_adapter_posix_spawn", fake_run)
    response = review_client._invoke_adapter(
        "review-status",
        {
            "schema_version": review_client.ADAPTER_COMMAND_SCHEMA,
            "command": "review_status",
            "payload": {"snapshot_canary": "SECRET_MATH_CANARY"},
        },
        timeout_seconds=30,
    )

    assert response["state"] == "running"
    assert "SECRET_MATH_CANARY" not in " ".join(captured["argv"])
    assert captured["argv"][1:3] == ["-I", "-B"]
    assert captured["executed_bytes"] == b"# trusted adapter\n"
    assert b"SECRET_MATH_CANARY" in captured["input"]
    assert captured["env"][review_client.CONTROL_TOKEN_ENV] == "f" * 64
    assert "PYTHONPATH" not in captured["env"]
    assert Path(captured["argv"][3]).parent == captured["directory"]


def test_adapter_path_swap_restore_executes_only_pinned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    original_bytes = b"# exact trusted adapter bytes\n"
    malicious_bytes = b"# MALICIOUS_SWAP_BYTES\n"
    adapter.write_bytes(original_bytes)
    adapter.chmod(0o600)
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_PATH, os.fspath(adapter.resolve())
    )
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_SHA256,
        hashlib.sha256(original_bytes).hexdigest(),
    )
    monkeypatch.setenv(review_client.CONTROL_TOKEN_ENV, "f" * 64)
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_DB,
        os.fspath((tmp_path / "messages.sqlite3").resolve()),
    )
    original_write = review_client._write_pinned_adapter
    observed: dict[str, bytes] = {}

    def swap_then_pin(directory: Path, content: bytes) -> Path:
        backup = tmp_path / "trusted.backup"
        adapter.rename(backup)
        adapter.write_bytes(malicious_bytes)
        adapter.chmod(0o600)
        try:
            return original_write(directory, content)
        finally:
            adapter.unlink()
            backup.rename(adapter)

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed["executed"] = Path(argv[3]).read_bytes()
        response = {
            "schema_version": review_client.ADAPTER_RESPONSE_SCHEMA,
            "operation": "review_status",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "state": "running",
            "idempotent": True,
            "execution": None,
            "decision": None,
        }
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(response).encode(), stderr=b""
        )

    monkeypatch.setattr(review_client, "_write_pinned_adapter", swap_then_pin)
    monkeypatch.setattr(review_client, "_run_adapter_posix_spawn", fake_run)
    review_client._invoke_adapter(
        "review-status",
        review_client._command(
            "review_status",
            {
                "review_id": REVIEW_ID,
                "request_sha256": "1" * 64,
                "snapshot_sha256": "2" * 64,
            },
        ),
        timeout_seconds=30,
    )
    assert observed["executed"] == original_bytes
    assert b"MALICIOUS" not in observed["executed"]
    assert adapter.read_bytes() == original_bytes


@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="posix_spawn unavailable")
def test_attested_adapter_invocation_uses_direct_posix_spawn_from_mcp_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text(
        "import sys\nsys.stdin.buffer.read()\nsys.stdout.write('{}')\n",
        encoding="utf-8",
    )
    adapter.chmod(0o600)
    monkeypatch.setenv(review_client.ADAPTER_ENV_PATH, os.fspath(adapter))
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_SHA256,
        hashlib.sha256(adapter.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv(review_client.CONTROL_TOKEN_ENV, "f" * 64)
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_DB,
        os.fspath((tmp_path / "messages.sqlite3").resolve()),
    )
    calls = 0
    original = os.posix_spawn

    def observed(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(review_client.os, "posix_spawn", observed)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="AnyIO-worker") as pool:
        response = pool.submit(
            review_client._invoke_adapter,
            "review-status",
            review_client._command("review_status", {"operation": "probe"}),
            timeout_seconds=5,
        ).result(timeout=10)
    assert response == {}
    assert calls == 1


@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="posix_spawn unavailable")
def test_attested_adapter_trampoline_closes_late_inheritable_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text(
        "import json, os, sys\n"
        "sys.stdin.buffer.read()\n"
        "opened = []\n"
        "for descriptor in range(3, 256):\n"
        "    try:\n"
        "        os.fstat(descriptor)\n"
        "    except OSError:\n"
        "        continue\n"
        "    opened.append(descriptor)\n"
        "sys.stdout.write(json.dumps({'open_descriptors': opened}))\n",
        encoding="utf-8",
    )
    adapter.chmod(0o600)
    monkeypatch.setenv(review_client.ADAPTER_ENV_PATH, os.fspath(adapter))
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_SHA256,
        hashlib.sha256(adapter.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv(review_client.CONTROL_TOKEN_ENV, "f" * 64)
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_DB,
        os.fspath((tmp_path / "messages.sqlite3").resolve()),
    )
    original = os.posix_spawn
    injected_descriptors: list[int] = []

    def observed(*args: Any, **kwargs: Any) -> int:
        descriptor = os.open(os.devnull, os.O_RDONLY)
        os.set_inheritable(descriptor, True)
        injected_descriptors.append(descriptor)
        try:
            return original(*args, **kwargs)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(review_client.os, "posix_spawn", observed)
    response = review_client._invoke_adapter(
        "review-status",
        review_client._command("review_status", {"operation": "probe"}),
        timeout_seconds=5,
    )
    assert injected_descriptors
    assert response == {"open_descriptors": []}


@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="posix_spawn unavailable")
def test_attested_adapter_stdio_is_bound_to_unlinked_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "sys.stdout.write(json.dumps({'observed': payload['command']}))\n",
        encoding="utf-8",
    )
    adapter.chmod(0o600)
    monkeypatch.setenv(review_client.ADAPTER_ENV_PATH, os.fspath(adapter))
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_SHA256,
        hashlib.sha256(adapter.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv(review_client.CONTROL_TOKEN_ENV, "f" * 64)
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_DB,
        os.fspath((tmp_path / "messages.sqlite3").resolve()),
    )
    original = os.posix_spawn

    def observed(*args: Any, **kwargs: Any) -> int:
        trampoline_argv = args[1]
        spawn_directory = Path(trampoline_argv[6])
        for name, content in (
            ("request.json", b'{"command":"forged"}'),
            ("stdout.bin", b'{"observed":"forged"}'),
            ("stderr.bin", b"forged diagnostic"),
        ):
            target = spawn_directory / name
            target.write_bytes(content)
            target.chmod(0o600)
        return original(*args, **kwargs)

    monkeypatch.setattr(review_client.os, "posix_spawn", observed)
    response = review_client._invoke_adapter(
        "review-status",
        review_client._command("review_status", {"operation": "probe"}),
        timeout_seconds=5,
    )
    assert response == {"observed": "review_status"}


@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="posix_spawn unavailable")
def test_attested_adapter_posix_spawn_timeout_kills_and_reaps_exact_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text(
        "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    adapter.chmod(0o600)
    monkeypatch.setenv(review_client.ADAPTER_ENV_PATH, os.fspath(adapter))
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_SHA256,
        hashlib.sha256(adapter.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv(review_client.CONTROL_TOKEN_ENV, "f" * 64)
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_DB,
        os.fspath((tmp_path / "messages.sqlite3").resolve()),
    )
    original = os.posix_spawn
    spawned: list[int] = []

    def observed(*args: Any, **kwargs: Any) -> int:
        pid = original(*args, **kwargs)
        spawned.append(pid)
        return pid

    monkeypatch.setattr(review_client.os, "posix_spawn", observed)
    started_at = time.monotonic()
    with pytest.raises(review_client.ReviewAdapterError, match="command timed out"):
        review_client._invoke_adapter(
            "review-status",
            review_client._command("review_status", {"operation": "probe"}),
            timeout_seconds=1,
        )
    assert time.monotonic() - started_at < 3
    assert len(spawned) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0], os.WNOHANG)


@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="posix_spawn unavailable")
def test_attested_adapter_output_bound_kills_and_reaps_exact_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text(
        "import sys, time\n"
        "sys.stdin.buffer.read()\n"
        f"sys.stdout.buffer.write(b'x' * {review_client.MAX_ADAPTER_RESPONSE_BYTES + 1})\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    adapter.chmod(0o600)
    monkeypatch.setenv(review_client.ADAPTER_ENV_PATH, os.fspath(adapter))
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_SHA256,
        hashlib.sha256(adapter.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv(review_client.CONTROL_TOKEN_ENV, "f" * 64)
    monkeypatch.setenv(
        review_client.ADAPTER_ENV_DB,
        os.fspath((tmp_path / "messages.sqlite3").resolve()),
    )
    original = os.posix_spawn
    spawned: list[int] = []

    def observed(*args: Any, **kwargs: Any) -> int:
        pid = original(*args, **kwargs)
        spawned.append(pid)
        return pid

    monkeypatch.setattr(review_client.os, "posix_spawn", observed)
    started_at = time.monotonic()
    with pytest.raises(
        review_client.ReviewAdapterError, match="exceeded its output byte bound"
    ):
        review_client._invoke_adapter(
            "review-status",
            review_client._command("review_status", {"operation": "probe"}),
            timeout_seconds=5,
        )
    assert time.monotonic() - started_at < 3
    assert len(spawned) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(spawned[0], os.WNOHANG)


def _install_targeted_attempt_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_targeted: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    ticket = targeted_ticket()
    decision = {
        "route_id": "route-a",
        "raw_verdict": "yellow",
        "effective_verdict": "yellow",
        "yellow_streak": 1,
        "critic_confirmed_progress_ids": [],
        "auto_red": False,
        "auto_red_reason": None,
        "route_frozen": False,
        "allowed_action": "one_bounded_cycle_on_fatal_doubt",
    }
    state = {
        "record": {"record_id": "mem_review"},
        "body": {
            "state": "official_published",
            "run_id": "run-1",
            "problem_id": "frontier/example",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "route_id": "route-a",
            "active_route": active_route(),
            "fallback_route_candidates": [],
            "route_transition": {
                "next_route_id": None,
                "fallback_evidence_record_ids": [],
            },
            "decision": decision,
            **(
                {}
                if initial_targeted is None
                else {"targeted_verification": deepcopy(initial_targeted)}
            ),
        },
        "pending": None,
    }
    commits: list[dict[str, Any]] = []
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(
        server,
        "_find_review_memory",
        lambda *args, **kwargs: (deepcopy(state["record"]), deepcopy(state["body"])),
    )

    def replace(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        state["record"] = {"record_id": "mem_review_next"}
        state["body"] = deepcopy(kwargs["body"])
        return deepcopy(state["record"]), deepcopy(state["body"])

    monkeypatch.setattr(server, "_replace_official_review_body", replace)

    def append_review(**kwargs: Any) -> dict[str, Any]:
        state["record"] = {"record_id": "mem_review_official"}
        state["body"] = deepcopy(kwargs["body"])
        state["pending"] = None
        return {
            "schema_version": "rethlas_route_review_publication_receipt_v1",
            "publication_state": "official",
            "record_id": state["record"]["record_id"],
        }

    monkeypatch.setattr(server, "_append_review_memory", append_review)
    monkeypatch.setattr(
        server,
        "_publication_receipt_for_existing",
        lambda *args, **kwargs: {
            "schema_version": "rethlas_route_review_publication_receipt_v1",
            "publication_state": "official",
            "record_id": state["record"]["record_id"],
        },
    )

    def append_targeted(**kwargs: Any) -> dict[str, Any]:
        result_sha = (
            kwargs["verification_receipt"].get("receipt_sha256", "3" * 64)
            if kwargs["verification_receipt"] is not None
            else kwargs["error_sha256"]
        )
        state["pending"] = (
            {"record_id": "mem_targeted_pending"},
            {
                "ticket": deepcopy(kwargs["ticket"]),
                "outcome": {
                    "state": kwargs["outcome_state"],
                    "verification_receipt": deepcopy(kwargs["verification_receipt"]),
                    "error_sha256": kwargs["error_sha256"],
                },
                "result_sha256": result_sha,
            },
        )
        return {
            "schema_version": "rethlas_targeted_verification_publication_receipt_v1",
            "publication_state": "pending",
            "record_id": "mem_targeted_pending",
        }

    monkeypatch.setattr(server, "_append_targeted_result_memory", append_targeted)
    transition_receipt = {
        "schema_version": "rethlas_route_transition_publication_receipt_v1",
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "from_route_id": "route-a",
        "to_route_id": None,
        "record_ids": ["mem_route_frozen"],
    }
    monkeypatch.setattr(
        server,
        "_append_route_transition_projection",
        lambda **kwargs: deepcopy(transition_receipt),
    )
    monkeypatch.setattr(
        server,
        "_find_targeted_result_memory",
        lambda *args, **kwargs: deepcopy(state["pending"]),
    )
    monkeypatch.setattr(
        server,
        "_targeted_publication_receipt_for_existing",
        lambda *args, **kwargs: {
            "schema_version": "rethlas_targeted_verification_publication_receipt_v1",
            "publication_state": "pending",
            "record_id": "mem_targeted_pending",
        },
    )
    monkeypatch.setattr(
        server,
        "_targeted_ticket_from_official_review",
        lambda body: (
            {
                "snapshot": {
                    "statement_text": STATEMENT_TEXT,
                    "blueprint_text": BLUEPRINT_TEXT,
                }
            },
            deepcopy(ticket),
        ),
    )
    monkeypatch.setattr(
        server,
        "_adapter_targeted_verification_prepare",
        lambda **kwargs: {
            "state": "verification_prepared",
            "verification_deadline_utc": "2099-01-01T00:00:00+00:00",
        },
    )

    def commit(**kwargs: Any) -> dict[str, Any]:
        commits.append(deepcopy(kwargs))
        if kwargs["publication_receipt"]["publication_state"] == "pending":
            return {"state": "verification_pending_publication"}
        if kwargs["outcome_state"] == "completed":
            return {
                "state": "closed",
                "decision": deepcopy(state["body"]["decision"]),
            }
        return {
            "state": (
                "verification_unknown"
                if kwargs["outcome_state"] == "execution_unknown"
                else "operational_blocked"
            )
        }

    monkeypatch.setattr(server, "_adapter_targeted_verification_commit", commit)
    return state, ticket, commits


def test_targeted_review_identity_lock_serializes_stale_concurrent_callers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(server, "GENERATION_CONTROL_ROOT", tmp_path / "control")
    entered = 0
    active = 0
    maximum_active = 0
    first_entered = threading.Event()
    release_first = threading.Event()
    counter_lock = threading.Lock()

    def locked_body(**kwargs: Any) -> dict[str, Any]:
        nonlocal active, entered, maximum_active
        with counter_lock:
            entered += 1
            active += 1
            maximum_active = max(maximum_active, active)
            ordinal = entered
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        with counter_lock:
            active -= 1
        return {"ordinal": ordinal}

    monkeypatch.setattr(server, "_verify_review_claim_locked", locked_body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            server.verify_review_claim,
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )
        assert first_entered.wait(timeout=5)
        second = pool.submit(
            server.verify_review_claim,
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )
        time.sleep(0.05)
        assert entered == 1
        release_first.set()
        assert first.result(timeout=5) == {"ordinal": 1}
        assert second.result(timeout=5) == {"ordinal": 2}

    assert maximum_active == 1


def test_route_review_close_repairs_crash_before_immutable_cutoff_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {
        "record": {
            "record_id": "mem_pending",
            "timestamp_utc": "2026-08-10T22:59:58+00:00",
        },
        "body": {
            "state": "completed_pending_close",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "report": {"load_bearing_claim": None},
            "decision": {
                "route_id": "route-a",
                "effective_verdict": "yellow",
            },
        },
    }
    append_count = 0
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(
        server,
        "_find_review_memory",
        lambda *args, **kwargs: (
            deepcopy(state["record"]),
            deepcopy(state["body"]),
        ),
    )

    def publication_receipt(
        _problem_id: str,
        record: dict[str, Any],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": "rethlas_route_review_publication_receipt_v1",
            "problem_id": "frontier/example",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "batch_id": "batch_" + "a" * 64,
            "record_id": record["record_id"],
            "timestamp_utc": record["timestamp_utc"],
            "checkpoint_sha256": "b" * 64,
            "record_sha256": hashlib.sha256(
                contracts.canonical_json_bytes(body)
            ).hexdigest(),
            "publication_state": (
                "official"
                if body["state"] == "official_published"
                else "pending"
            ),
        }

    monkeypatch.setattr(
        server, "_publication_receipt_for_existing", publication_receipt
    )

    def append_review(**kwargs: Any) -> dict[str, Any]:
        nonlocal append_count
        append_count += 1
        state["record"] = {
            "record_id": f"mem_official_{append_count}",
            "timestamp_utc": f"2026-08-10T23:00:0{append_count}+00:00",
        }
        state["body"] = deepcopy(kwargs["body"])
        return publication_receipt(
            "frontier/example", state["record"], state["body"]
        )

    monkeypatch.setattr(server, "_append_review_memory", append_review)

    def close_host(**kwargs: Any) -> dict[str, Any]:
        if kwargs["official_cutoff_publication_receipt"] is None:
            return {"state": "completed_pending_publication"}
        return {"state": "closed"}

    monkeypatch.setattr(server, "_adapter_route_review_close", close_host)
    real_ensure = server._ensure_immutable_official_cutoff

    class SimulatedPowerLoss(BaseException):
        pass

    monkeypatch.setattr(
        server,
        "_ensure_immutable_official_cutoff",
        lambda **kwargs: (_ for _ in ()).throw(SimulatedPowerLoss()),
    )
    with pytest.raises(SimulatedPowerLoss):
        server.route_review_close(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )
    assert state["body"]["state"] == "official_published"
    assert not any(
        key.startswith("official_published_") for key in state["body"]
    )

    monkeypatch.setattr(
        server, "_ensure_immutable_official_cutoff", real_ensure
    )
    result = server.route_review_close(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )

    assert result["state"] == "closed"
    assert append_count == 2
    assert state["body"]["official_published_record_id"] == "mem_official_1"
    assert state["body"]["official_published_publication_receipt"][
        "record_id"
    ] == "mem_official_1"


def test_targeted_verifier_predispatch_crash_retries_without_unknown_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ticket, _commits = _install_targeted_attempt_harness(monkeypatch)
    invocations = 0
    network_calls = 0
    receipt = {"ticket_id": ticket["ticket_id"], "verdict": "correct"}

    class SimulatedPowerLoss(BaseException):
        pass

    def verifier(**kwargs: Any) -> dict[str, Any]:
        nonlocal invocations, network_calls
        invocations += 1
        if invocations == 1:
            raise SimulatedPowerLoss
        kwargs["on_verifier_dispatch"]()
        network_calls += 1
        return deepcopy(receipt)

    monkeypatch.setattr(server, "verify_targeted_claim_service", verifier)
    with pytest.raises(SimulatedPowerLoss):
        server.verify_review_claim(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )
    assert state["body"]["targeted_verification"]["state"] == "prepared"
    assert state["body"]["targeted_verification"][
        "verification_deadline_utc"
    ] == "2099-01-01T00:00:00+00:00"
    assert network_calls == 0

    result = server.verify_review_claim(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )
    assert result["verification_receipt"] == receipt
    assert invocations == 2
    assert network_calls == 1


def test_targeted_verifier_response_loss_is_terminal_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ticket, commits = _install_targeted_attempt_harness(monkeypatch)
    calls = 0

    def lost_response(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise server.requests.ConnectionError("response lost")

    monkeypatch.setattr(server, "verify_targeted_claim_service", lost_response)
    first = server.verify_review_claim(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )
    assert first["state"] == "verification_unknown"
    assert state["body"]["targeted_verification"]["state"] == "execution_unknown"
    assert calls == 1
    assert commits[-1]["outcome_state"] == "execution_unknown"

    replay = server.verify_review_claim(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )
    assert replay["state"] == "verification_unknown"
    assert calls == 1


def test_effective_red_review_rejects_targeted_verifier_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(
        server,
        "_find_review_memory",
        lambda *args, **kwargs: (
            {"record_id": "mem_red_review"},
            {
                "state": "official_published",
                "decision": {"effective_verdict": "red"},
            },
        ),
    )
    monkeypatch.setattr(
        server,
        "_targeted_ticket_from_official_review",
        lambda body: pytest.fail("red review must not construct a verifier ticket"),
    )
    monkeypatch.setattr(
        server,
        "verify_targeted_claim_service",
        lambda **kwargs: pytest.fail("red review must make zero verifier calls"),
    )
    with pytest.raises(ValueError, match="effective green or yellow"):
        server.verify_review_claim(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )


def test_red_close_uses_only_the_snapshot_committed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    candidate = fallback_route()
    monkeypatch.setattr(
        server,
        "_find_review_memory",
        lambda *args, **kwargs: (
            {"record_id": "mem_official"},
            {
                "state": "official_published",
                "review_id": REVIEW_ID,
                "request_sha256": "1" * 64,
                "snapshot_sha256": "2" * 64,
                "route_id": "route-a",
                "active_route": active_route(),
                "fallback_route_candidates": [candidate],
                    "decision": {"effective_verdict": "red"},
                    "official_published_record_id": "mem_first_official",
                    "official_published_timestamp_utc": "2026-08-11T23:59:00+00:00",
                    "official_published_record_sha256": "4" * 64,
                    "official_published_publication_receipt": {
                    "publication_state": "official",
                    "record_id": "mem_cutoff",
                },
                "route_transition": {
                    "next_route_id": "route-fallback",
                    "fallback_evidence_record_ids": ["mem_1"],
                },
            },
        ),
    )
    monkeypatch.setattr(
        server,
        "_publication_receipt_for_existing",
        lambda *args, **kwargs: {
            "publication_state": "official",
            "record_id": "mem_official",
        },
    )
    transition_receipt = {"receipt_sha256": "3" * 64}
    monkeypatch.setattr(
        server,
        "_append_route_transition_projection",
        lambda **kwargs: transition_receipt,
    )
    calls: list[dict[str, Any]] = []

    def close_host(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"state": "closed"}

    monkeypatch.setattr(server, "_adapter_route_review_close", close_host)
    result = server.route_review_close(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )
    assert result["state"] == "closed"
    assert calls[0]["next_route_id"] == "route-fallback"
    assert calls[0]["fallback_evidence_record_ids"] == ["mem_1"]
    assert calls[0]["route_transition_publication_receipt"] == transition_receipt
    assert "next_route_id" not in inspect.signature(server.route_review_close).parameters


def test_red_fallback_projection_is_durable_and_becomes_next_review_active_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem_id = "frontier/fallback-transition"
    monkeypatch.setattr(server, "MEMORY_ROOT", tmp_path / "memory")
    clock = {"value": "2026-08-10T22:39:00+00:00"}
    monkeypatch.setattr(server, "_utc_now", lambda: clock["value"])
    monkeypatch.setattr(
        server.time,
        "time",
        lambda: server.datetime.fromisoformat(clock["value"]).timestamp(),
    )

    evidence_receipt = server.memory_append_batch(
        problem_id,
        [
            {
                "channel": "proof_steps",
                "record": {"claim": "pre-due evidence for fallback Bridge B"},
            }
        ],
    )
    evidence_id = evidence_receipt["records"][0]["record_id"]
    clock["value"] = "2026-08-10T22:40:00+00:00"
    commitment_receipt = server.memory_append_batch(
        problem_id,
        [
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": "route-a",
                    "state": {
                        "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                        "route_id": "route-a",
                        "status": "active",
                        "core_bridge": "Bridge A",
                        "obligations": ["Prove Bridge A."],
                    },
                },
            },
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": "route-fallback",
                    "state": {
                        "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                        "route_id": "route-fallback",
                        "status": "fallback",
                        "core_bridge": "Bridge B",
                        "obligations": ["Test Bridge B."],
                        "evidence_record_ids": [evidence_id],
                    },
                },
            },
        ],
    )
    active, fallbacks = server._trusted_route_commitment_manifest(
        problem_id, due_at_utc="2026-08-10T23:00:00+00:00"
    )
    assert active["commitment_record_id"] == commitment_receipt["records"][0][
        "record_id"
    ]
    assert fallbacks[0]["commitment_record_id"] == commitment_receipt["records"][1][
        "record_id"
    ]

    review_body = {
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "active_route": active,
    }
    clock["value"] = "2026-08-10T23:01:00+00:00"
    first = server._append_route_transition_projection(
        problem_id=problem_id,
        review_body=review_body,
        fallback=fallbacks[0],
    )
    replay = server._append_route_transition_projection(
        problem_id=problem_id,
        review_body=review_body,
        fallback=fallbacks[0],
    )
    assert replay == first
    assert len(first["record_ids"]) == 2
    replay_active, replay_fallbacks = server._trusted_route_commitment_manifest(
        problem_id,
        due_at_utc="2026-08-10T23:00:00+00:00",
        current_review_id=REVIEW_ID,
    )
    assert replay_active["route_id"] == "route-a"
    assert [item["route_id"] for item in replay_fallbacks] == ["route-fallback"]
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", problem_id)
    replay_frontier, replay_progress = server._trusted_review_frontier_ids(
        cycle_id="cycle-1",
        cycle="minute60",
        due_at_utc="2026-08-10T23:00:00+00:00",
        route_id="route-a",
        current_review_id=REVIEW_ID,
    )
    assert replay_frontier == [evidence_id]
    assert replay_progress == []

    next_active, next_fallbacks = server._trusted_route_commitment_manifest(
        problem_id, due_at_utc="2026-08-10T23:30:00+00:00"
    )
    assert next_active["route_id"] == "route-fallback"
    assert next_active["core_bridge"] == "Bridge B"
    assert next_fallbacks == []
    projected = server._trusted_checkpoint_records(problem_id)
    frozen = [
        record
        for record in projected.values()
        if record["channel"] == "branch_states"
        and record["record"].get("branch_id") == "route-a"
    ]
    assert len(frozen) == 1
    assert frozen[0]["record"]["state"]["status"] == "frozen"

    forged = {
        "branch_id": "route-invented",
        "state": {
            "schema_version": server._ROUTE_TRANSITION_STATE_SCHEMA,
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "route_id": "route-invented",
            "status": "active",
            "core_bridge": "invented",
            "obligations": ["invent"],
            "source_commitment_sha256": "3" * 64,
        },
    }
    with pytest.raises(ValueError, match="reserved for trusted control"):
        server.memory_append_batch(
            problem_id, [{"channel": "branch_states", "record": forged}]
        )


def test_red_without_fallback_durably_freezes_old_route_and_leaves_no_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem_id = "frontier/no-fallback-transition"
    monkeypatch.setattr(server, "MEMORY_ROOT", tmp_path / "memory")
    clock = {"value": "2026-08-10T22:40:00+00:00"}
    monkeypatch.setattr(server, "_utc_now", lambda: clock["value"])
    monkeypatch.setattr(
        server.time,
        "time",
        lambda: server.datetime.fromisoformat(clock["value"]).timestamp(),
    )
    server.memory_append_batch(
        problem_id,
        [
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": "route-a",
                    "state": {
                        "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                        "route_id": "route-a",
                        "status": "active",
                        "core_bridge": "Bridge A",
                        "obligations": ["Prove Bridge A."],
                    },
                },
            }
        ],
    )
    active, fallbacks = server._trusted_route_commitment_manifest(
        problem_id, due_at_utc="2026-08-10T23:00:00+00:00"
    )
    assert fallbacks == []
    body = {
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "active_route": active,
    }
    clock["value"] = "2026-08-10T23:01:00+00:00"
    receipt = server._append_route_transition_projection(
        problem_id=problem_id, review_body=body, fallback=None
    )
    assert receipt["to_route_id"] is None
    assert len(receipt["record_ids"]) == 1
    assert server._route_transition_projection_receipt(
        problem_id=problem_id,
        review_body=body,
        fallback=None,
        publish=False,
    ) == receipt
    replay_active, replay_fallbacks = server._trusted_route_commitment_manifest(
        problem_id,
        due_at_utc="2026-08-10T23:00:00+00:00",
        current_review_id=REVIEW_ID,
    )
    assert replay_active["route_id"] == "route-a"
    assert replay_fallbacks == []
    projected = server._trusted_checkpoint_records(problem_id)
    states = [
        record["record"]["state"]
        for record in projected.values()
        if record["channel"] == "branch_states"
    ]
    assert [state["status"] for state in states] == ["frozen"]
    with pytest.raises(ValueError, match="exactly one pre-boundary active"):
        server._trusted_route_commitment_manifest(
            problem_id, due_at_utc="2026-08-10T23:30:00+00:00"
        )


def test_red_close_without_fallback_requires_frozen_projection_before_host_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    body = {
        "state": "official_published",
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "route_id": "route-a",
        "active_route": active_route(),
        "fallback_route_candidates": [],
        "decision": {"effective_verdict": "red"},
        "official_published_record_id": "mem_first_official",
        "official_published_timestamp_utc": "2026-08-11T23:59:00+00:00",
        "official_published_record_sha256": "4" * 64,
        "official_published_publication_receipt": {
            "publication_state": "official",
            "record_id": "mem_cutoff",
        },
        "route_transition": {
            "next_route_id": None,
            "fallback_evidence_record_ids": [],
        },
    }
    monkeypatch.setattr(
        server,
        "_find_review_memory",
        lambda *args, **kwargs: ({"record_id": "mem_official"}, deepcopy(body)),
    )
    monkeypatch.setattr(
        server,
        "_publication_receipt_for_existing",
        lambda *args, **kwargs: {
            "publication_state": "official",
            "record_id": "mem_official",
        },
    )
    frozen_receipt = {"to_route_id": None, "record_ids": ["mem_frozen"]}
    monkeypatch.setattr(
        server,
        "_append_route_transition_projection",
        lambda **kwargs: frozen_receipt,
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        server,
        "_adapter_route_review_close",
        lambda **kwargs: calls.append(kwargs) or {"state": "closed"},
    )
    result = server.route_review_close(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )
    assert result["state"] == "closed"
    assert calls == [
        {
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "publication_receipt": {
                "publication_state": "official",
                "record_id": "mem_official",
            },
            "official_cutoff_publication_receipt": {
                "publication_state": "official",
                "record_id": "mem_cutoff",
            },
            "route_transition_publication_receipt": frozen_receipt,
            "next_route_id": None,
            "fallback_evidence_record_ids": [],
        }
    ]


def test_review_close_transition_receipt_is_exact_and_bound_before_host_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = {
        "schema_version": review_client.ROUTE_TRANSITION_PUBLICATION_RECEIPT_SCHEMA,
        "problem_id": "frontier/example",
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "from_route_id": "route-a",
        "to_route_id": "route-fallback",
        "batch_id": "batch_" + "a" * 64,
        "record_ids": ["mem_transition_old", "mem_transition_new"],
        "timestamp_utc": "2026-08-10T23:01:00+00:00",
        "checkpoint_sha256": "3" * 64,
        "transition_sha256": "4" * 64,
    }
    transition_receipt = {
        **seed,
        "receipt_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(seed)
        ).hexdigest(),
    }
    publication_receipt = {
        "schema_version": review_client.PUBLICATION_RECEIPT_SCHEMA,
        "problem_id": "frontier/example",
        "review_id": REVIEW_ID,
        "request_sha256": "1" * 64,
        "snapshot_sha256": "2" * 64,
        "batch_id": "batch_" + "b" * 64,
        "record_id": "mem_official",
        "timestamp_utc": "2026-08-10T23:00:00+00:00",
        "checkpoint_sha256": "5" * 64,
        "record_sha256": "6" * 64,
        "publication_state": "official",
    }
    cutoff_receipt = {
        **publication_receipt,
        "record_id": "mem_cutoff",
        "timestamp_utc": "2026-08-10T22:59:59+00:00",
    }
    captured: dict[str, Any] = {}

    def invoke(command: str, envelope: dict[str, Any], *, timeout_seconds: int):
        captured.update(envelope["payload"])
        return {
            "schema_version": review_client.ADAPTER_RESPONSE_SCHEMA,
            "operation": "review_close",
            "review_id": REVIEW_ID,
            "request_sha256": "1" * 64,
            "snapshot_sha256": "2" * 64,
            "state": "closed",
            "idempotent": False,
            "execution": {
                "state": "completed",
                "report": {},
                "error": None,
                "retry_allowed": False,
                "attempt": 1,
            },
            "decision": {
                "route_id": "route-a",
                "raw_verdict": "red",
                "effective_verdict": "red",
                "yellow_streak": 0,
                "critic_confirmed_progress_ids": [],
                "auto_red": False,
                "auto_red_reason": None,
                "route_frozen": True,
                "allowed_action": "freeze_route",
            },
        }

    monkeypatch.setattr(review_client, "_invoke_adapter", invoke)
    review_client.route_review_close(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
        publication_receipt=publication_receipt,
        official_cutoff_publication_receipt=cutoff_receipt,
        next_route_id="route-fallback",
        fallback_evidence_record_ids=["mem_1"],
        route_transition_publication_receipt=transition_receipt,
    )
    assert captured["route_transition"]["publication_receipt"] == transition_receipt
    assert captured["official_cutoff_publication_receipt"] == cutoff_receipt

    late_cutoff = {
        **cutoff_receipt,
        "timestamp_utc": "2026-08-10T23:00:01+00:00",
    }
    with pytest.raises(review_client.ReviewAdapterError, match="immutable cutoff"):
        review_client.route_review_close(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
            publication_receipt=publication_receipt,
            official_cutoff_publication_receipt=late_cutoff,
            next_route_id="route-fallback",
            fallback_evidence_record_ids=["mem_1"],
            route_transition_publication_receipt=transition_receipt,
        )

    forged = deepcopy(transition_receipt)
    forged["to_route_id"] = "route-invented"
    with pytest.raises(review_client.ReviewAdapterError, match="content address"):
        review_client.route_review_close(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
            publication_receipt=publication_receipt,
            official_cutoff_publication_receipt=cutoff_receipt,
            next_route_id="route-fallback",
            fallback_evidence_record_ids=["mem_1"],
            route_transition_publication_receipt=forged,
        )

    freeze_seed = {
        **seed,
        "to_route_id": None,
        "record_ids": ["mem_transition_old"],
    }
    freeze_receipt = {
        **freeze_seed,
        "receipt_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(freeze_seed)
        ).hexdigest(),
    }
    review_client.route_review_close(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
        publication_receipt=publication_receipt,
        official_cutoff_publication_receipt=cutoff_receipt,
        next_route_id=None,
        fallback_evidence_record_ids=[],
        route_transition_publication_receipt=freeze_receipt,
    )
    assert captured["route_transition"]["publication_receipt"] == freeze_receipt


def test_red_close_rejects_tampered_committed_fallback_before_host_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    candidate = fallback_route()
    candidate["route_id"] = "route-invented"
    monkeypatch.setattr(
        server,
        "_find_review_memory",
        lambda *args, **kwargs: (
            {"record_id": "mem_official"},
            {
                "state": "official_published",
                "route_id": "route-a",
                "fallback_route_candidates": [candidate],
                "decision": {"effective_verdict": "red"},
            },
        ),
    )
    monkeypatch.setattr(
        server,
        "_adapter_route_review_close",
        lambda **kwargs: pytest.fail("uncommitted fallback must make zero host mutations"),
    )
    with pytest.raises(ValueError, match="digest mismatch"):
        server.route_review_close(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )


def test_targeted_verifier_restart_from_dispatching_marks_unknown_without_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = targeted_ticket()
    initial = {
        "state": "dispatching",
        "ticket": ticket,
        "attempt": 1,
        "retry_allowed": False,
        "verification_receipt": None,
        "error_sha256": None,
    }
    _state, _ticket, commits = _install_targeted_attempt_harness(
        monkeypatch, initial_targeted=initial
    )
    monkeypatch.setattr(
        server,
        "verify_targeted_claim_service",
        lambda **kwargs: pytest.fail("ambiguous dispatch must never be repeated"),
    )
    result = server.verify_review_claim(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )
    assert result["state"] == "verification_unknown"
    assert len(commits) == 2
    assert all(commit["outcome_state"] == "execution_unknown" for commit in commits)
    assert commits[0]["publication_receipt"]["publication_state"] == "pending"
    assert commits[1]["publication_receipt"]["publication_state"] == "official"


def test_targeted_verifier_restart_replays_client_journal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = targeted_ticket()
    initial = {
        "state": "dispatching",
        "ticket": ticket,
        "attempt": 1,
        "retry_allowed": False,
        "verification_receipt": None,
        "error_sha256": None,
        "verification_deadline_utc": "2099-01-01T00:00:00+00:00",
    }
    _state, _ticket, commits = _install_targeted_attempt_harness(
        monkeypatch, initial_targeted=initial
    )
    receipt = {"ticket_id": ticket["ticket_id"], "verdict": "correct"}
    calls = 0

    def replay(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert kwargs["recover_only"] is True
        assert kwargs["on_verifier_dispatch"] is None
        return deepcopy(receipt)

    monkeypatch.setattr(server, "verify_targeted_claim_service", replay)
    result = server.verify_review_claim(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )

    assert result["verification_receipt"] == receipt
    assert calls == 1
    assert [commit["outcome_state"] for commit in commits] == [
        "completed",
        "completed",
    ]


@pytest.mark.parametrize("verdict", ["correct", "wrong"])
def test_targeted_verifier_final_receipt_replays_without_second_call(
    verdict: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ticket, commits = _install_targeted_attempt_harness(monkeypatch)
    calls = 0
    receipt = {"ticket_id": ticket["ticket_id"], "verdict": verdict}

    def completed(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert kwargs["verification_deadline_utc"] == (
            "2099-01-01T00:00:00+00:00"
        )
        return deepcopy(receipt)

    monkeypatch.setattr(server, "verify_targeted_claim_service", completed)
    first = server.verify_review_claim(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )
    second = server.verify_review_claim(
        review_id=REVIEW_ID,
        request_sha256="1" * 64,
        snapshot_sha256="2" * 64,
    )

    assert calls == 1
    assert first["verification_receipt"] == receipt
    assert second["verification_receipt"] == receipt
    assert len(commits) == 3
    if verdict == "wrong":
        assert state["body"]["decision"]["effective_verdict"] == "red"
        assert state["body"]["decision"]["route_frozen"] is True
        assert state["body"]["route_transition"] == {
            "next_route_id": None,
            "fallback_evidence_record_ids": [],
        }
        assert commits[0]["route_transition_publication_receipt"] is None
        assert commits[1]["route_transition_publication_receipt"][
            "record_ids"
        ] == ["mem_route_frozen"]
        assert commits[2]["route_transition_publication_receipt"] == commits[1][
            "route_transition_publication_receipt"
        ]
    else:
        assert state["body"]["decision"]["effective_verdict"] == "yellow"
        assert all(
            commit["route_transition_publication_receipt"] is None
            for commit in commits
        )


def test_late_targeted_receipt_is_rejected_before_official_review_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ticket, _commits = _install_targeted_attempt_harness(monkeypatch)
    calls = 0
    receipt = {
        "ticket_id": ticket["ticket_id"],
        "verdict": "correct",
        "receipt_sha256": "3" * 64,
    }

    def completed(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return deepcopy(receipt)

    def reject_late(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["publication_receipt"]["publication_state"] == "pending"
        raise ValueError("T150 already hard-stopped")

    monkeypatch.setattr(server, "verify_targeted_claim_service", completed)
    monkeypatch.setattr(server, "_adapter_targeted_verification_commit", reject_late)

    with pytest.raises(ValueError, match="T150"):
        server.verify_review_claim(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )
    assert calls == 1
    assert state["body"]["targeted_verification"]["state"] == "dispatching"

    with pytest.raises(ValueError, match="T150"):
        server.verify_review_claim(
            review_id=REVIEW_ID,
            request_sha256="1" * 64,
            snapshot_sha256="2" * 64,
        )
    assert calls == 1


def test_server_registers_all_review_and_handoff_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names: list[str] = []
    functions: dict[str, Any] = {}

    class FakeMCP:
        def __init__(self, _name: str) -> None:
            pass

        def tool(self, *, name: str):
            names.append(name)

            def decorate(function):
                functions[name] = function
                return function

            return decorate

    monkeypatch.setattr(server, "FastMCP", FakeMCP)
    server.build_mcp_app()
    assert {
        "review_frontier_status",
        "route_review_prepare",
        "route_review_wait",
        "route_review_status",
        "route_review_close",
        "verify_review_claim",
        "context_handoff_prepare",
        "context_handoff_get",
        "context_handoff_status",
        "route_cycle_close",
    } <= set(names)
    assert set(functions) == set(names)
    assert functions["context_handoff_get"]._rethlas_rehydrate_guarded is False
    assert all(function._rethlas_phase_guarded is True for function in functions.values())
    assert all(
        function._rethlas_rehydrate_guarded is True
        for name, function in functions.items()
        if name != "context_handoff_get"
    )
    prepare_parameters = inspect.signature(functions["route_review_prepare"]).parameters
    assert "snapshot" not in prepare_parameters
    assert "expected_model" not in prepare_parameters
    assert "reasoning_effort" not in prepare_parameters
    assert "policy_sha256" not in prepare_parameters
    assert {
        "frontier_record_ids",
            "progress_record_ids",
            "frontier_manifest_sha256",
            "cycle_id",
        } <= set(prepare_parameters)
    assert "root_thread_id" not in prepare_parameters
    assert "root_turn_id" not in prepare_parameters
    assert "route_id" not in prepare_parameters
    assert "content" not in inspect.signature(
        functions["context_handoff_prepare"]
    ).parameters
    handoff_prepare_parameters = inspect.signature(
        functions["context_handoff_prepare"]
    ).parameters
    assert "cadence" not in handoff_prepare_parameters
    assert "from_thread_epoch" not in handoff_prepare_parameters
    assert "purpose" in handoff_prepare_parameters
    assert "timeout_seconds" not in inspect.signature(
        functions["route_review_wait"]
    ).parameters


def test_every_registered_tool_runs_independent_phase_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    functions: dict[str, Any] = {}

    class FakeMCP:
        def __init__(self, _name: str) -> None:
            pass

        def tool(self, *, name: str):
            def decorate(function):
                functions[name] = function
                return function

            return decorate

    phase_calls: list[str] = []
    body_calls = 0

    def deny_memory(tool_name: str) -> None:
        phase_calls.append(tool_name)
        if tool_name == "memory_search":
            raise ValueError("review-only phase")

    def forbidden_body(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal body_calls
        body_calls += 1
        return {}

    monkeypatch.setattr(server, "FastMCP", FakeMCP)
    monkeypatch.setattr(server, "_context_rehydrate_preflight", lambda name: None)
    monkeypatch.setattr(server, "_reasoning_phase_preflight", deny_memory)
    monkeypatch.setattr(server, "memory_search", forbidden_body)
    server.build_mcp_app()

    with pytest.raises(ValueError, match="review-only"):
        functions["memory_search"]("frontier/example", "bridge")
    assert phase_calls == ["memory_search"]
    assert body_calls == 0


def test_server_phase_guard_rejects_host_denial_and_cross_run_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "RETHLAS_REVIEW_ADAPTER_PATH",
        "RETHLAS_REVIEW_ADAPTER_SHA256",
        "RETHLAS_REVIEW_DB",
        "RETHLAS_REVIEW_CONTROL_TOKEN",
    ):
        monkeypatch.setenv(name, "bound")
    monkeypatch.setenv("RETHLAS_EXPECTED_HOTJOIN_RUN_ID", "run-1")
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    result = {
        "run_id": "run-1",
        "problem_id": "frontier/example",
        "phase": "review_1",
        "allowed_action": "independent_review_only",
        "tool_permitted": False,
    }
    monkeypatch.setattr(
        server, "_adapter_reasoning_phase_preflight", lambda **kwargs: deepcopy(result)
    )
    with pytest.raises(ValueError, match="forbidden"):
        server._reasoning_phase_preflight("memory_search")

    result["tool_permitted"] = True
    result["run_id"] = "run-other"
    with pytest.raises(ValueError, match="another run"):
        server._reasoning_phase_preflight("route_review_prepare")


def test_review_boundary_public_apis_accept_no_caller_route_choice() -> None:
    assert "route_id" not in inspect.signature(review_client.review_due_status).parameters
    assert "review_id" not in inspect.signature(review_client.review_due_status).parameters
    assert "route_id" not in inspect.signature(server.review_frontier_status).parameters
    assert "review_id" not in inspect.signature(server.review_frontier_status).parameters
    assert "route_id" not in inspect.signature(server.route_review_prepare).parameters


def test_restricted_review_frontier_is_deterministic_and_pre_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def record(
        record_id: str,
        timestamp: str,
        *,
        channel: str = "proof_steps",
        kind: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"claim": record_id}
        if kind is not None:
            body["review_progress_kind"] = kind
        return {
            "record_id": record_id,
            "timestamp_utc": timestamp,
            "channel": channel,
            "batch_id": "batch_" + hashlib.sha256(record_id.encode()).hexdigest(),
            "record": body,
        }

    records = {
        "mem_old": record(
            "mem_old",
            "2026-08-10T21:59:59+00:00",
            kind="new_lemma",
        ),
        "mem_new": record(
            "mem_new",
            "2026-08-10T22:45:00+00:00",
            kind="uncertainty_reduction",
        ),
        "mem_plain": record("mem_plain", "2026-08-10T22:50:00+00:00"),
        "mem_post": record(
            "mem_post",
            "2026-08-10T23:00:01+00:00",
            kind="counterexample_excluded",
        ),
        "mem_event": record(
            "mem_event", "2026-08-10T22:40:00+00:00", channel="events"
        ),
        "mem_review": record(
            "mem_review", "2026-08-10T22:41:00+00:00", channel="route_reviews"
        ),
        "mem_targeted": record(
            "mem_targeted",
            "2026-08-10T22:42:00+00:00",
            channel="targeted_verifications",
        ),
        "mem_route": {
            "record_id": "mem_route",
            "timestamp_utc": "2026-08-10T22:43:00+00:00",
            "channel": "branch_states",
            "batch_id": "batch_" + hashlib.sha256(b"mem_route").hexdigest(),
            "record": {
                "branch_id": "route-a",
                "state": {
                    "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                    "route_id": "route-a",
                    "status": "active",
                    "core_bridge": "one exact pre-boundary bridge",
                    "obligations": ["Prove the exact bridge."],
                },
            },
        },
    }
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(
        server, "_trusted_checkpoint_records", lambda _problem, **_kwargs: records
    )
    monkeypatch.setattr(
        server,
        "_adapter_review_due_status",
        lambda **kwargs: {
            "review_id": REVIEW_ID,
            "due_at_utc": "2026-08-10T23:00:00+00:00",
            "active_route_id": "route-a",
            "root_thread_id": "thread-1",
            "root_turn_id": "turn-1",
            "root_terminal_sha256": "9" * 64,
        },
    )
    status = server.review_frontier_status(
        cycle_id="cycle-1",
        cycle="minute60",
        review_ordinal=1,
    )
    assert status["frontier_record_ids"] == ["mem_new", "mem_plain"]
    assert status["progress_record_ids"] == ["mem_new"]
    assert "route_reviews" in server.CHANNEL_FILES  # searchable control history
    assert set(server._trusted_mathematical_evidence_records("frontier/example")) == {
        "mem_old",
        "mem_new",
        "mem_plain",
        "mem_post",
    }
    seed = deepcopy(status)
    digest = seed.pop("manifest_sha256")
    seed.pop("active_route_id")
    seed["schema_version"] = "rethlas_review_frontier_manifest_v1"
    assert digest == hashlib.sha256(server.canonical_json_bytes(seed)).hexdigest()
    monkeypatch.setattr(
        server,
        "_adapter_review_due_status",
        lambda **kwargs: {
            "review_id": REVIEW_ID,
            "due_at_utc": "2026-08-10T23:00:00+00:00",
            "active_route_id": "route:unspecified",
            "root_thread_id": "thread-1",
            "root_turn_id": "turn-1",
            "root_terminal_sha256": "9" * 64,
        },
    )
    first_binding = server.review_frontier_status(
        cycle_id="cycle-1",
        cycle="minute60",
        review_ordinal=1,
    )
    assert first_binding["manifest_sha256"] == digest

    with pytest.raises(ValueError, match="trusted frontier manifest"):
        server.route_review_prepare(
            review_id=REVIEW_ID,
            cycle_id="cycle-1",
            cycle="minute60",
            review_ordinal=1,
            frontier_manifest_sha256=digest,
            frontier_record_ids=list(reversed(status["frontier_record_ids"])),
            progress_record_ids=status["progress_record_ids"],
        )

    monkeypatch.setattr(
        server,
        "_adapter_review_due_status",
        lambda **kwargs: {
            "review_id": REVIEW_ID,
            "due_at_utc": "2026-08-10T23:30:00+00:00",
            "active_route_id": "route:unspecified",
            "root_thread_id": "thread-1",
            "root_turn_id": "turn-1",
            "root_terminal_sha256": "9" * 64,
        },
    )
    with pytest.raises(ValueError, match="only the first review"):
        server.route_review_prepare(
            review_id=REVIEW_ID,
            cycle_id="cycle-1",
            cycle="minute120",
            review_ordinal=2,
            frontier_manifest_sha256="1" * 64,
            frontier_record_ids=[],
            progress_record_ids=[],
        )


def test_frontier_ignores_unbounded_history_but_fails_on_unbounded_current_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def math_record(record_id: str, timestamp: str) -> dict[str, Any]:
        return {
            "record_id": record_id,
            "timestamp_utc": timestamp,
            "channel": "proof_steps",
            "batch_id": "batch_" + hashlib.sha256(record_id.encode()).hexdigest(),
            "record": {"claim": record_id},
        }

    route_record = {
        "record_id": "mem_route",
        "timestamp_utc": "2026-08-10T22:40:00+00:00",
        "channel": "branch_states",
        "batch_id": "batch_" + "f" * 64,
        "record": {
            "branch_id": "route-a",
            "state": {
                "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                "route_id": "route-a",
                "status": "active",
                "core_bridge": "Bridge A",
                "obligations": ["Prove Bridge A."],
            },
        },
    }
    history = {
        f"mem_historical_{index}": math_record(
            f"mem_historical_{index}", "2026-08-10T21:00:00+00:00"
        )
        for index in range(80)
    }
    records = {
        **history,
        "mem_route": route_record,
        "mem_current_a": math_record(
            "mem_current_a", "2026-08-10T22:45:00+00:00"
        ),
        "mem_current_b": math_record(
            "mem_current_b", "2026-08-10T22:50:00+00:00"
        ),
    }
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(
        server, "_trusted_checkpoint_records", lambda _problem, **_kwargs: records
    )
    frontier, progress = server._trusted_review_frontier_ids(
        cycle_id="cycle-1",
        cycle="minute60",
        due_at_utc="2026-08-10T23:00:00+00:00",
        route_id="route-a",
    )
    assert frontier == ["mem_current_a", "mem_current_b"]
    assert progress == []

    current_records = {
        "mem_route": route_record,
        **{
            f"mem_current_{index}": math_record(
                f"mem_current_{index}", "2026-08-10T22:45:00+00:00"
            )
            for index in range(65)
        },
    }
    monkeypatch.setattr(
        server,
        "_trusted_checkpoint_records",
        lambda _problem, **_kwargs: current_records,
    )
    with pytest.raises(ValueError, match="64-record bound"):
        server._trusted_review_frontier_ids(
            cycle_id="cycle-1",
            cycle="minute60",
            due_at_utc="2026-08-10T23:00:00+00:00",
            route_id="route-a",
        )


def test_pre_due_fallback_evidence_is_visible_but_never_active_route_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = {
        "record_id": "mem_fallback_evidence",
        "timestamp_utc": "2026-08-10T22:45:00+00:00",
        "channel": "proof_steps",
        "batch_id": "batch_" + "1" * 64,
        "record": {
            "claim": "load-bearing fallback evidence",
            "review_progress_kind": "new_lemma",
        },
    }
    records = {
        evidence["record_id"]: evidence,
        "mem_current": {
            "record_id": "mem_current",
            "timestamp_utc": "2026-08-10T22:45:00+00:00",
            "channel": "proof_steps",
            "batch_id": "batch_" + "2" * 64,
            "record": {"claim": "current active-route work"},
        },
        "mem_active": {
            "record_id": "mem_active",
            "timestamp_utc": "2026-08-10T22:40:00+00:00",
            "channel": "branch_states",
            "batch_id": "batch_" + "3" * 64,
            "record": {
                "branch_id": "route-a",
                "state": {
                    "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                    "route_id": "route-a",
                    "status": "active",
                    "core_bridge": "Bridge A",
                    "obligations": ["Prove Bridge A."],
                },
            },
        },
        "mem_fallback": {
            "record_id": "mem_fallback",
            "timestamp_utc": "2026-08-10T22:41:00+00:00",
            "channel": "branch_states",
            "batch_id": "batch_" + "4" * 64,
            "record": {
                "branch_id": "route-b",
                "state": {
                    "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                    "route_id": "route-b",
                    "status": "fallback",
                    "core_bridge": "Bridge B",
                    "obligations": ["Test Bridge B."],
                    "evidence_record_ids": ["mem_fallback_evidence"],
                },
            },
        },
    }
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(
        server, "_trusted_checkpoint_records", lambda _problem, **_kwargs: records
    )
    frontier, progress = server._trusted_review_frontier_ids(
        cycle_id="cycle-1",
        cycle="minute60",
        due_at_utc="2026-08-10T23:00:00+00:00",
        route_id="route-a",
    )
    assert frontier == ["mem_fallback_evidence", "mem_current"]
    assert progress == []


def test_minute120_cutoff_survives_later_targeted_review_supersession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_review_id = "review_" + "0" * 32
    prior_report = {
        "review_id": prior_review_id,
        "snapshot_sha256": "e" * 64,
        "route_id": "route-a",
        "answers": {
            "core_bridge": "Bridge A",
            "premise_target_fit": {"status": "match", "reason": "It matches."},
            "uncertainty_change": {
                "status": "not_reduced",
                "evidence_ids": [],
                "confirmed_progress": [],
            },
            "obstruction_risk": {
                "status": "none",
                "detail": "",
                "evidence_ids": [],
            },
            "next_milestone": {"description": "Test A", "test": "derive A"},
        },
        "verdict": "green",
        "fatal_doubt": None,
        "freeze_reason": None,
        "load_bearing_claim": None,
    }
    prior_decision = {
        "route_id": "route-a",
        "raw_verdict": "green",
        "effective_verdict": "green",
        "yellow_streak": 0,
        "critic_confirmed_progress_ids": [],
        "auto_red": False,
        "auto_red_reason": None,
        "route_frozen": False,
        "allowed_action": "continue_to_next_milestone",
    }
    records = {
        "mem_route_commitment": {
            "record_id": "mem_route_commitment",
            "timestamp_utc": "2026-08-10T22:40:00+00:00",
            "channel": "branch_states",
            "batch_id": "batch_" + "f" * 64,
            "record": {
                "branch_id": "route-a",
                "state": {
                    "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                    "route_id": "route-a",
                    "status": "active",
                    "core_bridge": "Bridge A",
                    "obligations": ["Prove Bridge A."],
                },
            },
        },
        "mem_prior": {
            "record_id": "mem_prior",
            "timestamp_utc": "2026-08-10T23:15:00+00:00",
            "channel": "route_reviews",
            "batch_id": "batch_" + "a" * 64,
            "record": {
                "schema_version": server._REVIEW_MEMORY_SCHEMA,
                "state": "official_published",
                "cycle_id": "cycle-1",
                "cycle": "minute60",
                "review_ordinal": 1,
                "review_id": prior_review_id,
                "snapshot_sha256": "e" * 64,
                "report": prior_report,
                "decision": prior_decision,
                "official_published_record_id": "mem_first_official",
                "official_published_timestamp_utc": "2026-08-10T23:00:00+00:00",
                "official_published_record_sha256": "d" * 64,
                "official_published_publication_receipt": {
                    "publication_state": "official",
                    "record_id": "mem_first_official",
                    "timestamp_utc": "2026-08-10T23:00:00+00:00",
                    "record_sha256": "d" * 64,
                },
            },
        },
        "mem_progress": {
            "record_id": "mem_progress",
            "timestamp_utc": "2026-08-10T23:10:00+00:00",
            "channel": "proof_steps",
            "batch_id": "batch_" + "b" * 64,
            "record": {
                "review_progress_kind": "new_lemma",
                "claim": "new after T30 but before targeted verification",
            },
        },
    }
    monkeypatch.setattr(
        server, "_trusted_checkpoint_records", lambda problem_id, **_kwargs: records
    )
    monkeypatch.setattr(
        server,
        "_trusted_problem_statement",
        lambda problem_id: (
            STATEMENT_TEXT,
            hashlib.sha256(STATEMENT_TEXT.encode()).hexdigest(),
        ),
    )
    monkeypatch.setattr(
        server,
        "_trusted_blueprint",
        lambda problem_id: (
            BLUEPRINT_TEXT,
            hashlib.sha256(BLUEPRINT_TEXT.encode()).hexdigest(),
        ),
    )
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setenv("RETHLAS_EXPECTED_HOTJOIN_RUN_ID", "run-1")
    monkeypatch.setenv("RETHLAS_REVIEW_EXPECTED_MODEL", "gpt-5.6-sol")
    monkeypatch.setenv("RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT", "max")
    monkeypatch.setenv("RETHLAS_REVIEW_POLICY_SHA256", POLICY_SHA)

    request = server._build_trusted_review_request(
        review_id=REVIEW_ID,
        cycle_id="cycle-1",
        cycle="minute120",
        review_ordinal=2,
        due_at_utc="2026-08-10T23:30:00+00:00",
        root_thread_id="thread-1",
        root_turn_id="turn-1",
        root_terminal_sha256="9" * 64,
        route_id="route-a",
        frontier_record_ids=["mem_progress"],
        progress_record_ids=["mem_progress"],
    )
    assert request["snapshot"]["prior_official_review"]["record_id"] == (
        "mem_first_official"
    )
    assert request["snapshot"]["prior_official_review"]["timestamp_utc"] == (
        "2026-08-10T23:00:00+00:00"
    )
    assert request["snapshot"]["progress_records"][0]["timestamp_utc"] == (
        "2026-08-10T23:10:00+00:00"
    )
    active_prior = server._active_prior_official_review_body(
        "frontier/example", request["snapshot"]["prior_official_review"]
    )
    assert active_prior["official_published_record_id"] == "mem_first_official"
    assert active_prior["decision"] == prior_decision


def test_next_cycle_minute60_uses_prior_cycle_same_route_yellow_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_review_id = "review_" + "0" * 32
    prior_report = {
        "review_id": prior_review_id,
        "snapshot_sha256": "e" * 64,
        "route_id": "route-a",
        "answers": {
            "core_bridge": "Bridge A",
            "premise_target_fit": {"status": "match", "reason": "It matches."},
            "uncertainty_change": {
                "status": "not_reduced",
                "evidence_ids": [],
                "confirmed_progress": [],
            },
            "obstruction_risk": {"status": "none", "detail": "", "evidence_ids": []},
            "next_milestone": {"description": "Test A", "test": "derive A"},
        },
        "verdict": "yellow",
        "fatal_doubt": {"description": "Test A", "test": "derive A"},
        "freeze_reason": None,
        "load_bearing_claim": None,
    }
    prior_decision = {
        "route_id": "route-a",
        "raw_verdict": "yellow",
        "effective_verdict": "yellow",
        "yellow_streak": 1,
        "critic_confirmed_progress_ids": [],
        "auto_red": False,
        "auto_red_reason": None,
        "route_frozen": False,
        "allowed_action": "one_bounded_cycle_on_fatal_doubt",
    }
    records = {
        "mem_route_commitment": {
            "record_id": "mem_route_commitment",
            "timestamp_utc": "2026-08-10T22:40:00+00:00",
            "channel": "branch_states",
            "batch_id": "batch_" + "f" * 64,
            "record": {
                "branch_id": "route-a",
                "state": {
                    "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                    "route_id": "route-a",
                    "status": "active",
                    "core_bridge": "Bridge A",
                    "obligations": ["Prove Bridge A."],
                },
            },
        },
        "mem_prior_cycle": {
            "record_id": "mem_prior_cycle",
            "timestamp_utc": "2026-08-10T23:05:00+00:00",
            "channel": "route_reviews",
            "batch_id": "batch_" + "a" * 64,
            "record": {
                "schema_version": server._REVIEW_MEMORY_SCHEMA,
                "state": "official_published",
                "cycle_id": "cycle-previous",
                "cycle": "minute120",
                "review_ordinal": 2,
                "review_id": prior_review_id,
                "snapshot_sha256": "e" * 64,
                "report": prior_report,
                "decision": prior_decision,
                "official_published_record_id": "mem_prior_cycle",
                "official_published_timestamp_utc": "2026-08-10T23:00:00+00:00",
                "official_published_record_sha256": "d" * 64,
                "official_published_publication_receipt": {
                    "publication_state": "official",
                    "record_id": "mem_prior_cycle",
                    "timestamp_utc": "2026-08-10T23:00:00+00:00",
                    "record_sha256": "d" * 64,
                },
            },
        },
        "mem_old": {
            "record_id": "mem_old",
            "timestamp_utc": "2026-08-10T22:59:59+00:00",
            "channel": "proof_steps",
            "batch_id": "batch_" + "b" * 64,
            "record": {"review_progress_kind": "new_lemma", "claim": "old"},
        },
        "mem_new": {
            "record_id": "mem_new",
            "timestamp_utc": "2026-08-10T23:10:00+00:00",
            "channel": "proof_steps",
            "batch_id": "batch_" + "c" * 64,
            "record": {"review_progress_kind": "new_lemma", "claim": "new"},
        },
    }
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "frontier/example")
    monkeypatch.setattr(
        server, "_trusted_checkpoint_records", lambda _problem, **_kwargs: records
    )
    frontier, progress = server._trusted_review_frontier_ids(
        cycle_id="cycle-current",
        cycle="minute60",
        due_at_utc="2026-08-10T23:30:00+00:00",
        route_id="route-a",
    )
    assert frontier == ["mem_new"]
    assert progress == ["mem_new"]

    pair = server._prior_official_review_record(
        records,
        cycle_id="cycle-current",
        cycle="minute60",
        route_id="route-a",
    )
    assert pair is not None
    prior = server._prior_official_review_payload(*pair)
    assert prior["cycle_id"] == "cycle-previous"
    assert prior["cycle"] == "minute120"
    assert prior["review_ordinal"] == 2
    assert prior["decision"]["yellow_streak"] == 1


@pytest.mark.parametrize("mode", ["invented_second_route", "post_due_only"])
def test_first_review_route_derivation_fails_closed_without_one_pre_due_commitment(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def commitment(route_id: str, timestamp: str) -> dict[str, Any]:
        return {
            "record_id": "mem_" + route_id.replace("-", "_"),
            "timestamp_utc": timestamp,
            "channel": "branch_states",
            "batch_id": "batch_" + hashlib.sha256(route_id.encode()).hexdigest(),
            "record": {
                "branch_id": route_id,
                "state": {
                    "schema_version": server._ACTIVE_ROUTE_COMMITMENT_SCHEMA,
                    "route_id": route_id,
                    "status": "active",
                    "core_bridge": "committed before review",
                    "obligations": ["Prove the committed bridge."],
                },
            },
        }

    if mode == "invented_second_route":
        records = {
            "mem_route_a": commitment("route-a", "2026-08-10T22:40:00+00:00"),
            "mem_route_invented": commitment(
                "route-invented", "2026-08-10T22:41:00+00:00"
            ),
        }
    else:
        records = {
            "mem_route_a": commitment("route-a", "2026-08-10T23:00:01+00:00")
        }
    monkeypatch.setattr(
        server, "_trusted_checkpoint_records", lambda _problem, **_kwargs: records
    )
    expected = (
        "route commitment changed after the exact review due time"
        if mode == "post_due_only"
        else "exactly one pre-boundary"
    )
    with pytest.raises(ValueError, match=expected):
        server._trusted_active_route_commitment(
            "frontier/example", due_at_utc="2026-08-10T23:00:00+00:00"
        )


def _memory_publication_receipt(
    *, batch_suffix: str = "a", accepted_at_utc: str = "2026-08-10T22:59:59+00:00"
) -> dict[str, Any]:
    seed: dict[str, Any] = {
        "schema_version": review_client.MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA,
        "state": "accepted",
        "run_id": "run-1",
        "problem_id": "frontier/example",
        "batch_id": "batch_" + batch_suffix * 64,
        "checkpoint_sha256": "b" * 64,
        "commit_sha256": "c" * 64,
        "publication_class": "reasoning_checkpoint",
        "cycle_id": "cycle_" + "d" * 32,
        "cutoff_action_id": "cadact_" + "e" * 32,
        "cutoff_kind": "review_1",
        "cutoff_at_utc": "2026-08-10T23:00:00+00:00",
        "cutoff_monotonic": 2_000.0,
        "accepted_at_utc": accepted_at_utc,
        "accepted_at_monotonic": 1_999.0,
        "boot_identity": "boot-test",
    }
    return {
        **seed,
        "receipt_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(seed)
        ).hexdigest(),
    }


def test_memory_batch_publication_snapshot_validator_is_pure_canonical_and_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "schema_version": review_client.MEMORY_BATCH_PUBLICATION_STATUS_SCHEMA,
        "run_id": "run-1",
        "problem_id": "frontier/example",
        "receipts": [_memory_publication_receipt()],
    }
    canonical = contracts.canonical_json_bytes(status).decode("utf-8")
    monkeypatch.setattr(
        review_client,
        "_invoke_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("snapshot validation must not invoke the adapter")
        ),
    )
    assert review_client.validate_memory_batch_publication_status_snapshot(
        canonical,
        expected_run_id="run-1",
        expected_problem_id="frontier/example",
    ) == status

    with pytest.raises(review_client.ReviewAdapterError, match="not canonical"):
        review_client.validate_memory_batch_publication_status_snapshot(
            json.dumps(status, sort_keys=True),
            expected_run_id="run-1",
            expected_problem_id="frontier/example",
        )
    with pytest.raises(review_client.ReviewAdapterError, match="invalid"):
        review_client.validate_memory_batch_publication_status_snapshot(
            '{"problem_id":"frontier/example","problem_id":"frontier/example"}',
            expected_run_id="run-1",
            expected_problem_id="frontier/example",
        )
    with pytest.raises(review_client.ReviewAdapterError, match="byte bound"):
        review_client.validate_memory_batch_publication_status_snapshot(
            "x" * (review_client.MAX_ADAPTER_RESPONSE_BYTES + 1),
            expected_run_id="run-1",
            expected_problem_id="frontier/example",
        )
    with pytest.raises(review_client.ReviewAdapterError, match="status is invalid"):
        review_client.validate_memory_batch_publication_status_snapshot(
            canonical,
            expected_run_id="run-other",
            expected_problem_id="frontier/example",
        )


def test_memory_batch_publication_client_commits_and_validates_exact_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, "run-1")
    expected = _memory_publication_receipt()
    calls: list[tuple[str, dict[str, Any], int]] = []

    def invoke(
        command: str, payload: dict[str, Any], *, timeout_seconds: int
    ) -> dict[str, Any]:
        calls.append((command, payload, timeout_seconds))
        return deepcopy(expected)

    monkeypatch.setattr(review_client, "_invoke_adapter", invoke)
    observed = review_client.memory_batch_publication_commit(
        problem_id="frontier/example",
        batch_id="batch_" + "a" * 64,
        checkpoint_sha256="b" * 64,
        commit_sha256="c" * 64,
        publication_class="reasoning_checkpoint",
    )
    assert observed == expected
    assert calls == [
        (
            "review-status",
            review_client._command(
                "review_status",
                {
                    "operation": "memory_batch_publication_commit",
                    "problem_id": "frontier/example",
                    "batch_id": "batch_" + "a" * 64,
                    "checkpoint_sha256": "b" * 64,
                    "commit_sha256": "c" * 64,
                    "publication_class": "reasoning_checkpoint",
                },
            ),
            30,
        )
    ]


def test_memory_batch_publication_client_rejects_crossed_or_unsorted_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, "run-1")
    crossed = _memory_publication_receipt(
        accepted_at_utc="2026-08-10T23:00:00+00:00"
    )
    crossed_seed = {
        key: value for key, value in crossed.items() if key != "receipt_sha256"
    }
    crossed["receipt_sha256"] = hashlib.sha256(
        contracts.canonical_json_bytes(crossed_seed)
    ).hexdigest()
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *_args, **_kwargs: crossed
    )
    with pytest.raises(review_client.ReviewAdapterError, match="crossed its cutoff"):
        review_client.memory_batch_publication_commit(
            problem_id="frontier/example",
            batch_id="batch_" + "a" * 64,
            checkpoint_sha256="b" * 64,
            commit_sha256="c" * 64,
            publication_class="reasoning_checkpoint",
        )

    first = _memory_publication_receipt(batch_suffix="b")
    second = _memory_publication_receipt(batch_suffix="a")
    status = {
        "schema_version": review_client.MEMORY_BATCH_PUBLICATION_STATUS_SCHEMA,
        "run_id": "run-1",
        "problem_id": "frontier/example",
        "receipts": [first, second],
    }
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *_args, **_kwargs: status
    )
    with pytest.raises(review_client.ReviewAdapterError, match="manifest is not exact"):
        review_client.memory_batch_publication_status(problem_id="frontier/example")


def test_memory_batch_publication_client_rejects_cross_bound_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, "run-1")
    receipt = _memory_publication_receipt()
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *_args, **_kwargs: receipt
    )
    with pytest.raises(review_client.ReviewAdapterError, match="bind its request"):
        review_client.memory_batch_publication_commit(
            problem_id="other/problem",
            batch_id="batch_" + "a" * 64,
            checkpoint_sha256="b" * 64,
            commit_sha256="c" * 64,
            publication_class="reasoning_checkpoint",
        )

    cross_run = deepcopy(receipt)
    cross_run["run_id"] = "run-other"
    cross_run_seed = {
        key: value for key, value in cross_run.items() if key != "receipt_sha256"
    }
    cross_run["receipt_sha256"] = hashlib.sha256(
        contracts.canonical_json_bytes(cross_run_seed)
    ).hexdigest()
    status = {
        "schema_version": review_client.MEMORY_BATCH_PUBLICATION_STATUS_SCHEMA,
        "run_id": "run-1",
        "problem_id": "frontier/example",
        "receipts": [cross_run],
    }
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *_args, **_kwargs: status
    )
    with pytest.raises(review_client.ReviewAdapterError, match="cross-bound"):
        review_client.memory_batch_publication_status(problem_id="frontier/example")

    hostile_problem = _memory_publication_receipt()
    hostile_problem["problem_id"] = "frontier/evil\nproblem"
    hostile_seed = {
        key: value
        for key, value in hostile_problem.items()
        if key != "receipt_sha256"
    }
    hostile_problem["receipt_sha256"] = hashlib.sha256(
        contracts.canonical_json_bytes(hostile_seed)
    ).hexdigest()
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *_args, **_kwargs: hostile_problem
    )
    with pytest.raises(review_client.ReviewAdapterError, match="receipt is invalid"):
        review_client.memory_batch_publication_commit(
            problem_id="frontier/example",
            batch_id="batch_" + "a" * 64,
            checkpoint_sha256="b" * 64,
            commit_sha256="c" * 64,
            publication_class="reasoning_checkpoint",
        )

    hostile_clock = _memory_publication_receipt()
    hostile_clock["cutoff_monotonic"] = 10**309
    hostile_seed = {
        key: value for key, value in hostile_clock.items() if key != "receipt_sha256"
    }
    hostile_clock["receipt_sha256"] = hashlib.sha256(
        contracts.canonical_json_bytes(hostile_seed)
    ).hexdigest()
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *_args, **_kwargs: hostile_clock
    )
    with pytest.raises(
        review_client.ReviewAdapterError, match="cutoff monotonic time is invalid"
    ):
        review_client.memory_batch_publication_commit(
            problem_id="frontier/example",
            batch_id="batch_" + "a" * 64,
            checkpoint_sha256="b" * 64,
            commit_sha256="c" * 64,
            publication_class="reasoning_checkpoint",
        )


def test_memory_batch_publication_max_manifest_fits_existing_adapter_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "r" * 128
    problem_id = "p" * 128
    receipts: list[dict[str, Any]] = []
    for index in range(review_client.MAX_ACCEPTED_MEMORY_BATCH_PUBLICATIONS):
        seed: dict[str, Any] = {
            "schema_version": review_client.MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA,
            "state": "accepted",
            "run_id": run_id,
            "problem_id": problem_id,
            "batch_id": "batch_" + f"{index:064x}",
            "checkpoint_sha256": "b" * 64,
            "commit_sha256": "c" * 64,
            "publication_class": "reasoning_checkpoint",
            "cycle_id": "cycle_" + "d" * 32,
            "cutoff_action_id": "cadact_" + "e" * 32,
            "cutoff_kind": "hard_stop",
            "cutoff_at_utc": "9999-12-31T23:59:59.999999+00:00",
            "cutoff_monotonic": 1.0e308,
            "accepted_at_utc": "9999-12-31T23:59:58.999999+00:00",
            "accepted_at_monotonic": 9.0e307,
            "boot_identity": '"' * 128,
        }
        receipts.append(
            {
                **seed,
                "receipt_sha256": hashlib.sha256(
                    contracts.canonical_json_bytes(seed)
                ).hexdigest(),
            }
        )
    status = {
        "schema_version": review_client.MEMORY_BATCH_PUBLICATION_STATUS_SCHEMA,
        "run_id": run_id,
        "problem_id": problem_id,
        "receipts": receipts,
    }
    assert len(contracts.canonical_json_bytes(status)) < (
        review_client.MAX_ADAPTER_RESPONSE_BYTES
    )
    assert len(
        (json.dumps(status, ensure_ascii=False, sort_keys=True) + "\n").encode()
    ) < review_client.MAX_ADAPTER_RESPONSE_BYTES
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, run_id)
    monkeypatch.setattr(
        review_client, "_invoke_adapter", lambda *_args, **_kwargs: status
    )
    observed = review_client.memory_batch_publication_status(problem_id=problem_id)
    assert len(observed["receipts"]) == (
        review_client.MAX_ACCEPTED_MEMORY_BATCH_PUBLICATIONS
    )


def _continuous_round_finish_fixture() -> tuple[
    list[dict[str, Any]], dict[str, Any], str
]:
    reports = [
        {
            "record_id": "mem_" + f"{index + 1:064x}",
            "batch_id": "batch_" + "a" * 64,
            "thread_id": f"thread-{index}",
            "plan_id": f"plan-{index}",
            "status": "blocked",
            "report_sha256": hashlib.sha256(
                f"source-report-{index}".encode()
            ).hexdigest(),
            "record_sha256": hashlib.sha256(f"report-{index}".encode()).hexdigest(),
        }
        for index in range(3)
    ]
    synthesis = {
        "record_id": "mem_" + f"{9:064x}",
        "batch_id": "batch_" + "b" * 64,
        "failed_plan_ids": [f"plan-{index}" for index in range(3)],
        "record_sha256": "c" * 64,
    }
    material = {
        "reports": [
            {key: value for key, value in item.items() if key != "report_sha256"}
            for item in reports
        ],
        "synthesis": synthesis,
        "next_action": "new_cohort",
    }
    return (
        reports,
        synthesis,
        hashlib.sha256(contracts.canonical_json_bytes(material)).hexdigest(),
    )


def _continuous_round_status_fixture() -> dict[str, Any]:
    lanes = [
        {
            "thread_id": f"thread-{index}",
            "route_id": f"route-{index}",
            "observed_status": "idle",
            "terminal_status": "completed",
            "terminal_turn_sha256": f"{index + 1:x}" * 64,
            "terminal_report_sha256": f"{index + 4:x}" * 64,
        }
        for index in range(3)
    ]
    material = {
        "schema_version": review_client.CONTINUOUS_ROUND_STATUS_SCHEMA,
        "operation": "continuous_round_status",
        "state": "ready",
        "run_id": "run-round",
        "problem_id": "frontier/example",
        "cohort_ordinal": 2,
        "lanes": lanes,
    }
    return {
        **material,
        "manifest_sha256": hashlib.sha256(
            contracts.canonical_json_bytes(material)
        ).hexdigest(),
    }


def test_continuous_round_status_client_binds_exact_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _continuous_round_status_fixture()
    captured: dict[str, Any] = {}

    def invoke(command: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured.update({"command": command, "payload": payload})
        return deepcopy(status)

    monkeypatch.setattr(review_client, "_invoke_adapter", invoke)
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, "run-round")
    observed = review_client.continuous_round_status(
        problem_id="frontier/example"
    )
    assert observed == status
    assert captured["command"] == "review-status"
    assert captured["payload"]["payload"] == {
        "operation": "continuous_round_status",
        "problem_id": "frontier/example",
    }


def test_continuous_round_status_client_rejects_corrupt_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _continuous_round_status_fixture()
    status["lanes"][0]["terminal_report_sha256"] = "0" * 64
    monkeypatch.setattr(
        review_client,
        "_invoke_adapter",
        lambda *_args, **_kwargs: deepcopy(status),
    )
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, "run-round")
    with pytest.raises(review_client.ReviewAdapterError, match="binding mismatch"):
        review_client.continuous_round_status(problem_id="frontier/example")


def test_continuous_round_finish_client_binds_exact_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports, synthesis, evidence_sha256 = _continuous_round_finish_fixture()
    captured: dict[str, Any] = {}

    def invoke(command: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured.update({"command": command, "payload": payload})
        material = {
            "schema_version": review_client.CONTINUOUS_ROUND_FINISH_RECEIPT_SCHEMA,
            "operation": "continuous_round_finish",
            "state": "finished",
            "finish_id": "roundfinish_" + "d" * 32,
            "run_id": "run-round",
            "problem_id": "frontier/example",
            "cohort_ordinal": 2,
            "route_bindings_sha256": "e" * 64,
            "report_record_ids": [item["record_id"] for item in reports],
            "synthesis_record_id": synthesis["record_id"],
            "evidence_sha256": evidence_sha256,
            "next_action": "new_cohort",
        }
        return {
            **material,
            "receipt_sha256": hashlib.sha256(
                contracts.canonical_json_bytes(material)
            ).hexdigest(),
            "idempotent_replay": False,
        }

    monkeypatch.setattr(review_client, "_invoke_adapter", invoke)
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, "run-round")
    receipt = review_client.continuous_round_finish(
        problem_id="frontier/example",
        reports=reports,
        synthesis=synthesis,
        next_action="new_cohort",
        evidence_sha256=evidence_sha256,
    )
    assert receipt["state"] == "finished"
    assert captured["command"] == "review-status"
    assert captured["payload"]["payload"]["operation"] == (
        "continuous_round_finish"
    )


def test_continuous_round_finish_client_rejects_corrupt_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports, synthesis, evidence_sha256 = _continuous_round_finish_fixture()
    monkeypatch.setattr(
        review_client,
        "_invoke_adapter",
        lambda *_args, **_kwargs: {
            "schema_version": review_client.CONTINUOUS_ROUND_FINISH_RECEIPT_SCHEMA,
            "operation": "continuous_round_finish",
            "state": "finished",
            "finish_id": "roundfinish_" + "d" * 32,
            "run_id": "run-round",
            "problem_id": "frontier/example",
            "cohort_ordinal": 2,
            "route_bindings_sha256": "e" * 64,
            "report_record_ids": [item["record_id"] for item in reports],
            "synthesis_record_id": synthesis["record_id"],
            "evidence_sha256": evidence_sha256,
            "next_action": "new_cohort",
            "receipt_sha256": "0" * 64,
            "idempotent_replay": False,
        },
    )
    monkeypatch.setenv(review_client.EXPECTED_RUN_ENV, "run-round")
    with pytest.raises(review_client.ReviewAdapterError, match="binding mismatch"):
        review_client.continuous_round_finish(
            problem_id="frontier/example",
            reports=reports,
            synthesis=synthesis,
            next_action="new_cohort",
            evidence_sha256=evidence_sha256,
        )
