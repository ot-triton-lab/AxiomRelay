"""Owner-only new-input restarts never replay a failed mathematical cohort."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agents import claude_core
from agents.generation.tests.test_claude_core import (
    _current_cohort_intent,
    _plans,
    _prepare_root,
)


OLD_ROOT = "12345678-1234-4123-8123-123456789abc"
NEW_ROOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_ROOT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _packet(case: dict[str, Any]) -> dict[str, Any]:
    return claude_core.reference_candidate_packet(
        problem_id="example", statement_sha256=case["statement_sha256"]
    )


def _add_candidate(case: dict[str, Any], candidate_id: str) -> None:
    claude_core.ingest_reference_candidate(
        problem_id="example",
        statement_sha256=case["statement_sha256"],
        candidate_id=candidate_id,
        target_claims=["Test the exact research assertion independently."],
        content=f"Unverified owner-supplied input {candidate_id}.\n",
    )


def _audit_plans(case: dict[str, Any]) -> list[dict[str, object]]:
    plans = _plans()
    plans[2]["plan_summary"] = "Audit these unverified inputs: " + " ".join(
        f"[reference_candidate:{candidate['candidate_id']}] {candidate['path']}"
        for candidate in _packet(case)["candidates"]
    )
    return plans


def _authorize(case: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "problem_id": "example",
        "statement_sha256": case["statement_sha256"],
        "root_session_id": OLD_ROOT,
        "successor_root_session_id": NEW_ROOT,
        "expected_candidate_packet_sha256": (
            claude_core._reference_candidate_packet_sha256(_packet(case))
        ),
        "reason": "Owner requested a new round including the additional input.",
    }
    arguments.update(overrides)
    return claude_core.authorize_council_restart(**arguments)


def _takeover(case: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "problem_id": "example",
        "statement_sha256": case["statement_sha256"],
        "root_session_id": NEW_ROOT,
        "canonical_model": "claude-opus-5",
        "orchestration_mode": claude_core.OPUS_SOL_COUNCIL_MODE,
        "takeover_from": OLD_ROOT,
    }
    arguments.update(overrides)
    return _prepare_root(**arguments)


def _start(case: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "problem_id": "example",
        "statement_sha256": case["statement_sha256"],
        "root_session_id": NEW_ROOT,
        "opus_plans": _audit_plans(case),
        "prior_failure_context": (
            "The previous cohort failed settlement after retaining partial work; "
            "its receipt remains failed. A new owner input warrants a new round."
        ),
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    arguments.update(overrides)
    return claude_core.start_route_council(**arguments)


@pytest.fixture
def restart_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    generation = tmp_path / "generation"
    (generation / "data").mkdir(parents=True)
    statement = generation / "data" / "example.md"
    statement.write_bytes(
        (Path(__file__).with_name("fixtures") / "example.md").read_bytes()
    )
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "INPUT_ROOT", generation / ".inputs")
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", tmp_path / "receipts")
    monkeypatch.setattr(claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", "3" * 64)
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", "4" * 64)
    case: dict[str, Any] = {
        "statement_sha256": hashlib.sha256(statement.read_bytes()).hexdigest(),
        "frontier": "9" * 64,
        "dispatches": [],
    }
    monkeypatch.setattr(
        claude_core, "_frontier", lambda _problem_id: {
            "frontier_sha256": case["frontier"]
        }
    )

    def paid_phase(**arguments: Any) -> dict[str, Any]:
        case["dispatches"].append(arguments)
        return {"status": "completed"}

    monkeypatch.setattr(claude_core, "_run_council_phase", paid_phase)
    _prepare_root(
        problem_id="example",
        statement_sha256=case["statement_sha256"],
        root_session_id=OLD_ROOT,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    _add_candidate(case, "original")
    case["old_packet"] = _packet(case)
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=case["statement_sha256"],
        root_session_id=OLD_ROOT,
        plans=_audit_plans(case),
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    council_id = "council_" + "a" * 32
    cohort_id = "cohort_" + "b" * 32
    council_dir = claude_core._council_dir("example", council_id)
    claude_core._write_once(
        council_dir / "reference_candidate_packet.json",
        case["old_packet"],
        mode=0o400,
    )
    pointer = {
        "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
        "pointer_version": 1,
        "problem_id": "example",
        "statement_sha256": case["statement_sha256"],
        "root_session_id": OLD_ROOT,
        "council_round": 1,
        "council_id": council_id,
        "base_frontier_sha256": "2" * 64,
        "opus_plan_sha256": "3" * 64,
        "prior_context_sha256": "4" * 64,
        "prior_failure_receipt_sha256": None,
        "host_source_sha256": claude_core._host_source_sha256(),
        "predecessor_root_session_id": None,
        "predecessor_council_id": None,
        "predecessor_pointer_sha256": None,
        "state": "consumed",
        "final_plan_sha256": plan_sha256,
        "acceptance_sha256": "6" * 64,
        "checkpoint_sha256": "8" * 64,
        "cohort_id": cohort_id,
        "updated_at_unix": time.time(),
    }
    pointer_path = claude_core._council_pointer_path("example", OLD_ROOT)
    claude_core._write_once(pointer_path, pointer, mode=0o400)
    cohort_dir = claude_core.STATE_ROOT / "example" / cohort_id
    cohort_dir.mkdir(mode=0o700)
    claude_core._write_once(
        cohort_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    intent = _current_cohort_intent(
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=case["statement_sha256"],
        plan_sha256=plan_sha256,
        root_session_id=OLD_ROOT,
    )
    intent["host_source_sha256"] = claude_core._host_source_sha256()
    claude_core._write_once(cohort_dir / "intent.json", intent, mode=0o400)
    log_path = cohort_dir / "executor.log"
    log_path.write_text("Partial work retained; exact delta rejected.\n")
    receipt = {
        "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
        "status": "failed",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": case["statement_sha256"],
        "plan_sha256": plan_sha256,
        "root_session_id": OLD_ROOT,
        "returncode": 1,
        "timed_out": False,
        "elapsed_seconds": 1.0,
        "frontier_before_sha256": "2" * 64,
        "frontier_after_sha256": case["frontier"],
        "frontier_changed": True,
        "log_path": str(log_path),
        "log_bytes": log_path.stat().st_size,
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "log_over_cap": False,
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        "retry_allowed": False,
        "completion_evidence": None,
    }
    receipt_path = cohort_dir / "receipt.json"
    claude_core._write_once(receipt_path, receipt, mode=0o400)
    case.update(
        pointer=pointer,
        pointer_path=pointer_path,
        council_dir=council_dir,
        cohort_dir=cohort_dir,
        receipt=receipt,
        receipt_path=receipt_path,
        authorization_path=(
            claude_core.STATE_ROOT / "example" / "roots" / OLD_ROOT
            / "owner_restart_authorization.json"
        ),
    )
    _add_candidate(case, "additional")
    return case


def test_owner_restart_is_explicit_and_preserves_failed_history(
    restart_case: dict[str, Any],
) -> None:
    case = restart_case
    old_receipt = case["receipt_path"].read_bytes()
    old_pointer = case["pointer_path"].read_bytes()
    with pytest.raises(claude_core.ClaudeCoreError):
        _takeover(case)
    with pytest.raises(claude_core.ClaudeCoreError):
        _start(case, root_session_id=OLD_ROOT)
    assert case["dispatches"] == []
    assert not case["authorization_path"].exists()

    authorization = _authorize(case)
    authorization_bytes = case["authorization_path"].read_bytes()
    assert _authorize(case) == authorization
    assert case["authorization_path"].read_bytes() == authorization_bytes
    with pytest.raises(claude_core.ClaudeCoreError):
        _start(case, root_session_id=OLD_ROOT)
    assert case["dispatches"] == []
    manifest = _takeover(case)
    assert manifest["previous_root_session_id"] == OLD_ROOT
    replayed_manifest = _takeover(case)
    assert all(replayed_manifest.get(key) == value for key, value in manifest.items())
    assert case["dispatches"] == []

    with pytest.raises(claude_core.ClaudeCoreError):
        claude_core._assert_council_ready_for_verification(
            problem_id="example", statement_sha256=case["statement_sha256"]
        )
    with pytest.raises(claude_core.ClaudeCoreError):
        _start(case, prior_failure_context="")
    assert case["dispatches"] == []
    started = _start(case)
    assert started["status"] == "completed"
    assert started["council_round"] == 2
    assert len(case["dispatches"]) == 1
    assert case["receipt_path"].read_bytes() == old_receipt
    assert case["pointer_path"].read_bytes() == old_pointer
    assert case["authorization_path"].read_bytes() == authorization_bytes
    assert not (case["cohort_dir"] / "recovery_authorization.json").exists()
    successor = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", NEW_ROOT),
        problem_id="example",
        statement_sha256=case["statement_sha256"],
        root_session_id=NEW_ROOT,
    )
    assert successor["prior_failure_receipt_sha256"] == hashlib.sha256(
        old_receipt
    ).hexdigest()
    assert successor["base_frontier_sha256"] == case["frontier"]


def test_owner_restart_binds_a_new_deployment_without_rewriting_the_old_root(
    restart_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = restart_case
    manifest_path = (
        claude_core.STATE_ROOT / "example" / "roots" / OLD_ROOT / "manifest.json"
    )
    historical_manifest = manifest_path.read_bytes()
    replacement_source = "e" * 64
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: replacement_source)
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: replacement_source
    )
    authorization = _authorize(case)
    assert authorization["host_source_sha256"] == replacement_source
    assert authorization["source_manifest_sha256"] == hashlib.sha256(
        historical_manifest
    ).hexdigest()
    successor = _takeover(case)
    assert successor["host_source_sha256"] == replacement_source
    assert _start(case)["council_round"] == 2
    assert manifest_path.read_bytes() == historical_manifest


@pytest.mark.parametrize("packet_change", ["unchanged", "old_item_changed"])
def test_owner_restart_requires_strict_candidate_extension(
    restart_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    packet_change: str,
) -> None:
    case = restart_case
    packet = copy.deepcopy(
        case["old_packet"] if packet_change == "unchanged" else _packet(case)
    )
    if packet_change == "old_item_changed":
        packet["candidates"][0]["target_claims"] = ["A replaced original claim."]
    monkeypatch.setattr(claude_core, "reference_candidate_packet", lambda **_: packet)
    with pytest.raises(claude_core.ClaudeCoreError):
        _authorize(case)
    assert not case["authorization_path"].exists()
    assert case["dispatches"] == []


@pytest.mark.parametrize("binding", [
    "frontier", "candidate", "source", "python", "launcher", "successor",
    "receipt", "pointer",
])
def test_owner_restart_rejects_stale_takeover_bindings(
    restart_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, binding: str,
) -> None:
    case = restart_case
    _authorize(case)
    overrides: dict[str, Any] = {}
    if binding == "frontier":
        case["frontier"] = "8" * 64
    elif binding == "candidate":
        _add_candidate(case, "later_input")
    elif binding == "source":
        monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: "f" * 64)
        monkeypatch.setattr(claude_core, "_loaded_host_source_sha256", lambda: "f" * 64)
    elif binding == "python":
        monkeypatch.setattr(claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", "5" * 64)
        overrides["python_runtime_sha256"] = "5" * 64
    elif binding == "launcher":
        monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", "5" * 64)
        overrides["root_launcher_sha256"] = "5" * 64
    elif binding == "successor":
        overrides["root_session_id"] = OTHER_ROOT
    elif binding == "receipt":
        changed_receipt = {**case["receipt"], "elapsed_seconds": 2.0}
        claude_core._replace_canonical(case["receipt_path"], changed_receipt)
    elif binding == "pointer":
        changed_pointer = {**case["pointer"], "prior_context_sha256": "7" * 64}
        claude_core._replace_canonical(case["pointer_path"], changed_pointer)
    with pytest.raises(claude_core.ClaudeCoreError):
        _takeover(case, **overrides)
    authority = json.loads(
        (claude_core.STATE_ROOT / "example" / "active_root.json").read_text()
    )
    assert authority["root_session_id"] == OLD_ROOT
    assert case["dispatches"] == []


@pytest.mark.parametrize("binding", ["frontier", "candidate"])
def test_owner_restart_rechecks_input_at_council_admission(
    restart_case: dict[str, Any], binding: str,
) -> None:
    case = restart_case
    _authorize(case)
    _takeover(case)
    if binding == "frontier":
        case["frontier"] = "8" * 64
    else:
        _add_candidate(case, "later_input")
    with pytest.raises(claude_core.ClaudeCoreError):
        _start(case)
    assert case["dispatches"] == []
    assert not claude_core._council_pointer_path("example", NEW_ROOT).exists()


def test_owner_restart_rejects_a_stale_prelock_candidate_snapshot(
    restart_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = restart_case
    _authorize(case)
    _takeover(case)
    current_packet = _packet(case)
    old_packet = case["old_packet"]
    reads = 0

    def changing_packet(**_: Any) -> dict[str, Any]:
        nonlocal reads
        reads += 1
        # The owner's new packet became visible after the call's first read.
        return old_packet if reads == 1 else current_packet

    old_plans = _plans()
    old_candidate = old_packet["candidates"][0]
    old_plans[2]["plan_summary"] = (
        f"Audit [reference_candidate:{old_candidate['candidate_id']}] "
        f"{old_candidate['path']}"
    )
    monkeypatch.setattr(claude_core, "reference_candidate_packet", changing_packet)
    with pytest.raises(claude_core.ClaudeCoreError):
        claude_core.start_route_council(
            problem_id="example",
            statement_sha256=case["statement_sha256"],
            root_session_id=NEW_ROOT,
            opus_plans=old_plans,
            prior_failure_context="Retain the prior failed receipt and partial work.",
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert reads >= 2
    assert case["dispatches"] == []
    assert not claude_core._council_pointer_path("example", NEW_ROOT).exists()


@pytest.mark.parametrize("bad_argument", [
    {"successor_root_session_id": OLD_ROOT},
    {"successor_root_session_id": "not-a-uuid"},
    {"expected_candidate_packet_sha256": "0" * 64},
    {"reason": ""},
])
def test_owner_restart_rejects_invalid_authorization_arguments(
    restart_case: dict[str, Any], bad_argument: dict[str, Any],
) -> None:
    with pytest.raises(claude_core.ClaudeCoreError):
        _authorize(restart_case, **bad_argument)
    assert not restart_case["authorization_path"].exists()


@pytest.mark.parametrize("terminal", ["missing", "unchanged", "completed"])
def test_owner_restart_is_only_for_a_failed_frontier_changing_cohort(
    restart_case: dict[str, Any], terminal: str,
) -> None:
    case = restart_case
    receipt = dict(case["receipt"])
    if terminal == "missing":
        case["receipt_path"].unlink()
    else:
        if terminal == "unchanged":
            receipt["frontier_after_sha256"] = receipt["frontier_before_sha256"]
            receipt["frontier_changed"] = False
            case["frontier"] = receipt["frontier_after_sha256"]
        else:
            receipt["returncode"] = 0
            receipt["status"] = "completed"
        claude_core._replace_canonical(case["receipt_path"], receipt)
    with pytest.raises(claude_core.ClaudeCoreError):
        _authorize(case)
    assert not case["authorization_path"].exists()
    assert case["dispatches"] == []


@pytest.mark.parametrize("live_lock", ["cohort", "retrieval"])
def test_owner_restart_refuses_live_execution_locks(
    restart_case: dict[str, Any], live_lock: str,
) -> None:
    case = restart_case
    lock_path = (
        case["cohort_dir"] / "cohort.lock" if live_lock == "cohort"
        else case["council_dir"] / "blind_retrieval.lock"
    )
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(claude_core.ClaudeCoreError):
            _authorize(case)
    assert not case["authorization_path"].exists()
    assert case["dispatches"] == []


def test_owner_restart_refuses_unsettled_publication(
    restart_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def publication_pending(**_: Any) -> None:
        raise claude_core.ClaudeCoreError("Synthetic unsettled publication.")

    monkeypatch.setattr(
        claude_core, "_assert_no_unsettled_publication_finalization", publication_pending
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="unsettled publication"):
        _authorize(restart_case)
    assert not restart_case["authorization_path"].exists()


def test_owner_restart_authorization_is_not_a_model_tool(
    restart_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = {
        "PROBLEM_ID": "example",
        "STATEMENT_SHA256": restart_case["statement_sha256"],
        "SESSION_ID": OLD_ROOT,
        "MODEL": "claude-opus-5",
        "LAUNCH_MODEL": "claude-opus-5[1m]",
        "PROVIDER": "vertex",
        "PROVIDER_BINDING_SHA256": hashlib.sha256(b"claude-opus-5[1m]").hexdigest(),
        "CLI_SHA256": "1" * 64,
        "CLI_VERSION": "test-claude-2.1.246",
        "CONTEXT_WINDOW": "1000000",
        "PYTHON_RUNTIME_SHA256": "3" * 64,
        "LAUNCHER_SHA256": "4" * 64,
        "ORCHESTRATION_MODE": claude_core.OPUS_SOL_COUNCIL_MODE,
        "CODEX_BIN": str(Path(sys.executable).resolve()),
    }
    for key, value in bindings.items():
        monkeypatch.setenv(f"RETHLAS_CLAUDE_ROOT_{key}", value)
    app = claude_core.build_mcp_app()
    manager = getattr(app, "_tool_manager", None)
    tools = getattr(manager, "_tools", {}) if manager is not None else {}
    assert "start_route_council" in tools
    assert "authorize_council_restart" not in tools
    assert not any("restart" in name for name in tools)
