from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def legacy_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("RETHLAS_RUNTIME_PROFILE", "legacy")
    for name in (
        "RETHLAS_EXPECTED_HOTJOIN_RUN_ID",
        "RETHLAS_REVIEW_CADENCE_POLICY",
        "RETHLAS_CONTEXT_GUARD_POLICY",
        "RETHLAS_REVIEW_ADAPTER_PATH",
        "RETHLAS_REVIEW_ADAPTER_SHA256",
        "RETHLAS_REVIEW_DB",
        "RETHLAS_REVIEW_CONTROL_TOKEN",
        "RETHLAS_EXPECTED_PROBLEM_ID",
        "RETHLAS_EXPECTED_STATEMENT_SHA256",
        "RETHLAS_BOUND_EXTERNAL_PLAN_PATH",
        "RETHLAS_BOUND_EXTERNAL_PLAN_SHA256",
        "RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    module_name = "agents.generation.mcp.legacy_server"
    verification_module_name = (
        "agents.generation.mcp.legacy_verification_client"
    )
    sys.modules.pop(module_name, None)
    sys.modules.pop(verification_module_name, None)
    server = importlib.import_module(module_name)

    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    (data_root / "example.md").write_text("S\n", encoding="utf-8")
    monkeypatch.setattr(server, "REPO_ROOT", generation_root)
    monkeypatch.setattr(server, "MEMORY_ROOT", generation_root / "memory")
    monkeypatch.setattr(server, "RESULTS_ROOT", generation_root / "results")
    monkeypatch.setattr(server, "DATA_ROOT", data_root)
    monkeypatch.setattr(
        server,
        "GENERATION_CONTROL_ROOT",
        tmp_path / "owner-generation-control",
    )
    monkeypatch.setattr(
        server,
        "RECEIPTS_ROOT",
        tmp_path / "owner-verification-receipts",
    )
    yield server
    sys.modules.pop(module_name, None)
    sys.modules.pop(verification_module_name, None)


def test_graph_native_identity_cannot_create_legacy_memory(legacy_server: ModuleType) -> None:
    with pytest.raises(ValueError, match="AxiomGraph source gate"):
        legacy_server.memory_init("axiomgraph:gr1_" + "a" * 64)
    assert not legacy_server.MEMORY_ROOT.exists()


def test_legacy_memory_is_local_idempotent_and_control_free(
    legacy_server: ModuleType,
) -> None:
    initial_frontier = legacy_server.legacy_frontier_receipt("example")
    initialized = legacy_server.memory_init("example")
    assert set(initialized["channels"]) == {
        "immediate_conclusions",
        "toy_examples",
        "counterexamples",
        "big_decisions",
        "subgoals",
        "proof_steps",
        "failed_paths",
        "verification_reports",
        "branch_states",
        "events",
    }

    items = [
        {
            "channel": "proof_steps",
            "record": {"claim": "legacy-local-checkpoint"},
        }
    ]
    first = legacy_server.memory_append_batch("example", items)
    second = legacy_server.memory_append_batch("example", items)
    assert second == first
    assert first["schema_version"] == (
        "rethlas_memory_batch_local_commit_receipt_v1"
    )
    assert "publication_receipt" not in first
    checkpoint_frontier = legacy_server.legacy_frontier_receipt("example")
    assert checkpoint_frontier["frontier_sha256"] != initial_frontier[
        "frontier_sha256"
    ]
    assert legacy_server.legacy_frontier_receipt("example") == checkpoint_frontier
    reconstructed = legacy_server.legacy_frontier_receipt_without_records(
        "example",
        [
            first["records"][0]["record_id"],
            legacy_server._batch_event_id(first["batch_id"]),
        ],
    )
    assert reconstructed == initial_frontier
    with pytest.raises(ValueError, match="exact records"):
        legacy_server.legacy_frontier_receipt_without_records(
            "example", ["mem_" + "f" * 64]
        )

    draft = legacy_server.RESULTS_ROOT / "example" / "blueprint.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("draft one\n", encoding="utf-8")
    draft_frontier = legacy_server.legacy_frontier_receipt("example")
    assert draft_frontier["frontier_sha256"] != checkpoint_frontier[
        "frontier_sha256"
    ]

    found = legacy_server.memory_search("example", "local checkpoint")
    assert found["count"] == 1
    assert found["results_by_channel"]["proof_steps"]["count"] == 1

    with pytest.raises(ValueError, match="Unknown channel"):
        legacy_server.memory_append(
            "example",
            "route_reviews",
            {"forbidden": True},
        )
    with pytest.raises(ValueError, match="trusted control publication"):
        legacy_server.memory_append(
            "example",
            "branch_states",
            {
                "state": {
                    "schema_version": "rethlas_route_transition_state_v1",
                }
            },
        )
    with pytest.raises(ValueError, match="host control memory"):
        legacy_server.memory_append_batch(
            "example",
            items,
            _trusted_control_publication=True,
        )

    round_synthesis = {
        "schema_version": "rethlas_round_failure_synthesis_v1",
        "record_type": "key_failures_summary",
    }
    with pytest.raises(ValueError, match="memory_append_batch"):
        legacy_server.memory_append(
            "example", "failed_paths", round_synthesis
        )
    synthesis_receipt = legacy_server.memory_append_batch(
        "example",
        [
            {
                "channel": "failed_paths",
                "record": round_synthesis,
                "active": True,
                "supersedes": [],
            }
        ],
    )
    assert synthesis_receipt["records"][0]["record_id"].startswith("mem_")
    assert len(synthesis_receipt["records"][0]["record_id"]) == 68
    with pytest.raises(ValueError, match="owner publication snapshots"):
        list(
            legacy_server._iter_memory_batch_checkpoints(
                "example",
                owner_manifest_snapshot_json="{}",
            )
        )


def test_generated_legacy_client_preserves_raw_json_operational_failure(
    legacy_server: ModuleType,
) -> None:
    del legacy_server
    verification_client = importlib.import_module(
        "agents.generation.mcp.legacy_verification_client"
    )
    item_id = "pi_0123456789abcdef01234567"
    detail = {
        "code": "claude_json_output_invalid",
        "adapter": "claude_cli",
        "item_id": item_id,
        "output_contract": "raw_json_v1",
    }

    class Response:
        status_code = 503

        @staticmethod
        def json() -> dict[str, object]:
            return {"detail": detail}

        @staticmethod
        def raise_for_status() -> None:
            pytest.fail("recognized verifier failure must not become HTTPError")

    with pytest.raises(
        verification_client.VerificationOperationalFailure
    ) as rejected:
        verification_client._raise_for_verification_service_error(Response())

    result = verification_client._operational_verifier_failure_result(
        pass_index=2,
        verification_passes=[],
        failure=rejected.value,
    )
    assert result["verification_status"] == "operational_failed"
    assert result["publication_blocked_reason"] == (
        "operational_verifier_failure"
    )
    assert result["operational_failure_code"] == "claude_json_output_invalid"
    assert result["operational_failure_output_contract"] == "raw_json_v1"
    assert result["operational_failure_item_id"] == item_id


def test_generated_legacy_wrapper_returns_blueprint_preflight_failure(
    legacy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_dir = legacy_server.RESULTS_ROOT / "example"
    result_dir.mkdir(parents=True)
    (result_dir / "blueprint.md").write_text(
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\n"
        "S\n\nPrecisely, this paragraph is not part of the target.\n\n"
        "## proof\nComplete proof.\n",
        encoding="utf-8",
    )
    verification_client = importlib.import_module(
        "agents.generation.mcp.legacy_verification_client"
    )
    monkeypatch.setattr(
        verification_client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("verifier must not be contacted"),
    )
    monkeypatch.setattr(
        verification_client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be contacted"),
    )

    result = legacy_server.verify_blueprint_service(
        problem_id="example",
        endpoint="https://verifier/verify",
    )

    assert result["status"] == "preflight_failed"
    assert result["category"] == "blueprint_contract"
    assert result["retry_allowed"] is True
    assert result["verifier_dispatched"] is False


def test_bound_external_plan_empty_items_materializes_exact_checkpoint(
    legacy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement_sha256 = hashlib.sha256(b"S\n").hexdigest()
    root_session_id = "11111111-2222-4333-8444-555555555555"
    plans = [
        {
            "plan_id": f"route_{index}",
            "mechanism": f"mechanism {index}",
            "scope": f"scope {index}",
            "discriminating_test": f"kill test {index}",
            "plan_summary": f"summary {index}",
            "subgoals": [f"subgoal {index}"],
            "motivation": [f"motivation {index}"],
        }
        for index in range(1, 4)
    ]
    plan_set = {
        "schema_version": "rethlas_claude_plan_set_v1",
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "root_session_id": root_session_id,
        "plans": plans,
    }
    raw = legacy_server.canonical_json_bytes(plan_set) + b"\n"
    plan_sha256 = hashlib.sha256(raw).hexdigest()
    plan_dir = (
        legacy_server.REPO_ROOT
        / ".claude_core_inputs"
        / "example"
        / "cohort"
    )
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / f"plan_{plan_sha256}.json"
    plan_path.write_bytes(raw)
    plan_path.chmod(0o400)
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "example")
    monkeypatch.setenv(
        "RETHLAS_EXPECTED_STATEMENT_SHA256", statement_sha256
    )
    monkeypatch.setenv("RETHLAS_BOUND_EXTERNAL_PLAN_PATH", str(plan_path))
    monkeypatch.setenv(
        "RETHLAS_BOUND_EXTERNAL_PLAN_SHA256", plan_sha256
    )
    monkeypatch.setenv(
        "RETHLAS_BOUND_EXTERNAL_PLAN_ROOT_SESSION_ID", root_session_id
    )

    first = legacy_server.memory_append_batch("example", [])
    replay = legacy_server.memory_append_batch("example", [])

    assert replay == first
    assert first["count"] == 3
    assert [record["channel"] for record in first["records"]] == [
        "subgoals",
        "subgoals",
        "subgoals",
    ]
    checkpoint = json.loads(
        Path(first["checkpoint_path"]).read_text(encoding="utf-8")
    )
    assert [entry["record"] for entry in checkpoint["records"]] == plans
    assert all(entry["active"] is True for entry in checkpoint["records"])
    assert all(entry["supersedes"] == [] for entry in checkpoint["records"])


def test_bound_external_plan_empty_sentinel_is_fail_closed(
    legacy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        legacy_server.MemoryCheckpointPreflightError,
        match="non-empty JSON array",
    ) as ordinary:
        legacy_server.memory_append_batch("example", [])
    assert ordinary.value.retry_allowed is True

    monkeypatch.setenv(
        "RETHLAS_BOUND_EXTERNAL_PLAN_PATH", "/missing/plan.json"
    )
    with pytest.raises(
        legacy_server.MemoryCheckpointPreflightError,
        match="environment is incomplete",
    ) as incomplete:
        legacy_server.memory_append_batch("example", [])
    assert incomplete.value.retry_allowed is False

    # Non-empty ordinary checkpoints retain their existing behavior even when
    # an unrelated partial binding is present.
    receipt = legacy_server.memory_append_batch(
        "example",
        [
            {
                "channel": "proof_steps",
                "record": {"claim": "ordinary non-empty checkpoint"},
                "active": True,
                "supersedes": [],
            }
        ],
    )
    assert receipt["count"] == 1


def test_legacy_checkpoint_failure_result_preserves_exact_error(
    legacy_server: ModuleType,
) -> None:
    result = legacy_server._checkpoint_failure_tool_result(
        legacy_server.MemoryCheckpointPreflightError(
            "encoded items exceed the 131072-byte checkpoint limit",
            retry_allowed=True,
        )
    )
    envelope = result.model_dump(by_alias=True, exclude_none=True)
    assert envelope["isError"] is True
    payload = envelope["structuredContent"]
    assert payload["schema_version"] == "rethlas_memory_checkpoint_failure_v1"
    assert payload["error"] == (
        "encoded items exceed the 131072-byte checkpoint limit"
    )
    assert payload["retry_allowed"] is True
    assert payload["checkpoint_committed"] is False
    assert json.loads(envelope["content"][0]["text"]) == payload


def test_legacy_terminal_report_tool_owns_schema_hash_and_real_report_size(
    legacy_server: ModuleType,
) -> None:
    report_text = "x" * 9_140
    tool_result = asyncio.run(
        legacy_server.APP.call_tool(
            "append_route_terminal_report",
            {
                "problem_id": "example",
                "thread_id": "/root/thread-a",
                "plan_id": "route-a",
                "status": "partial",
                "report_text": f" {report_text}\n",
                "remaining_obligations": ["finish the converse"],
                "decisive_stuck_points": ["primary statement was unavailable"],
            },
        )
    )

    envelope = tool_result.model_dump(by_alias=True, exclude_none=True)
    assert envelope["isError"] is False
    assert envelope["structuredContent"]["count"] == 1
    logical = legacy_server._load_memory_entries("example")
    record = logical["proof_steps"][0]["item"]["record"]
    assert set(record) == {
        "schema_version",
        "thread_id",
        "plan_id",
        "status",
        "report_text",
        "report_sha256",
        "remaining_obligations",
        "decisive_stuck_points",
    }
    assert record["report_text"] == report_text
    assert record["thread_id"] == "thread-a"
    assert record["report_sha256"] == hashlib.sha256(report_text.encode()).hexdigest()


def test_legacy_terminal_report_tool_rejects_ambiguous_nested_thread_path(
    legacy_server: ModuleType,
) -> None:
    tool_result = asyncio.run(
        legacy_server.APP.call_tool(
            "append_route_terminal_report",
            {
                "problem_id": "example",
                "thread_id": "/root/group/thread-a",
                "plan_id": "route-a",
                "status": "partial",
                "report_text": "bounded report",
                "remaining_obligations": ["finish"],
                "decisive_stuck_points": ["one gap"],
            },
        )
    )
    envelope = tool_result.model_dump(by_alias=True, exclude_none=True)
    assert envelope["isError"] is True
    payload = envelope["structuredContent"]
    assert payload["retry_allowed"] is True
    assert "direct /root/<route-local-id>" in payload["error"]
    assert payload["checkpoint_committed"] is False


def test_legacy_terminal_report_tool_returns_actionable_overflow_preflight(
    legacy_server: ModuleType,
) -> None:
    report_text = "x" * (legacy_server.MAX_ROUTE_TERMINAL_REPORT_BYTES + 1)
    tool_result = asyncio.run(
        legacy_server.APP.call_tool(
            "append_route_terminal_report",
            {
                "problem_id": "example",
                "thread_id": "thread-a",
                "plan_id": "route-a",
                "status": "blocked",
                "report_text": report_text,
                "remaining_obligations": ["compress the report"],
                "decisive_stuck_points": ["hard byte limit"],
            },
        )
    )

    envelope = tool_result.model_dump(by_alias=True, exclude_none=True)
    assert envelope["isError"] is True
    payload = envelope["structuredContent"]
    assert payload["retry_allowed"] is True
    assert payload["checkpoint_committed"] is False
    assert str(len(report_text)) in payload["error"]
    assert str(legacy_server.MAX_ROUTE_TERMINAL_REPORT_BYTES) in payload["error"]


def test_legacy_memory_search_hard_caps_complete_response(
    legacy_server: ModuleType,
) -> None:
    for index in range(4):
        legacy_server.memory_append(
            "example",
            "proof_steps",
            {
                "text": f"legacyenvelopeneedle record {index}",
                "payload": "x" * 15_000,
            },
        )

    response = legacy_server.memory_search(
        "example",
        "legacyenvelopeneedle",
        channels=["proof_steps"],
        max_chars=64_000,
    )

    assert response["response_limited"] is True
    assert response["count"] < 4
    assert legacy_server._memory_search_response_utf8_bytes(response) <= (
        legacy_server.MAX_MEMORY_SEARCH_RESPONSE_UTF8_BYTES
    )

    tool_result = asyncio.run(
        legacy_server.APP.call_tool(
            "memory_search",
            {
                "problem_id": "example",
                "query": "legacyenvelopeneedle",
                "channels": ["proof_steps"],
                "max_chars": 64_000,
            },
        )
    )
    assert len(tool_result.content) == 1
    emitted = tool_result.content[0].text.encode("utf-8")
    assert len(emitted) <= legacy_server.MAX_MEMORY_SEARCH_RESPONSE_UTF8_BYTES
    assert json.loads(emitted)["response_limited"] is True


def test_legacy_verification_dependency_excludes_targeted_review_code(
    legacy_server: ModuleType,
) -> None:
    module = sys.modules["agents.generation.mcp.legacy_verification_client"]
    assert module.__file__ is not None
    assert not hasattr(module, "verify_targeted_claim_service")
    assert not hasattr(module, "validate_targeted_claim_receipt")
    assert not hasattr(legacy_server, "verify_review_claim")


def test_legacy_generation_control_accepts_only_running(
    legacy_server: ModuleType,
) -> None:
    token = "a" * 32
    resumed = legacy_server.generation_control_resume("example", token)
    assert resumed["state"] == "running"
    assert resumed["evidence_record_ids"] == []
    assert legacy_server.generation_control_status("example", token) == resumed

    control_path = legacy_server._generation_control_path("example", token)
    corrupted = json.loads(control_path.read_text(encoding="utf-8"))
    corrupted["state"] = "waiting_cost_gate"
    corrupted["reason"] = "forbidden legacy wait"
    corrupted["evidence_record_ids"] = ["mock_evidence"]
    control_path.write_text(
        json.dumps(
            corrupted,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbids owner-wait states"):
        legacy_server.generation_control_status("example", token)
