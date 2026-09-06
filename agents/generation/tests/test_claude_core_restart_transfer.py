"""A source-drift handoff consumes one unused owner authorization, never work."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agents import claude_core
from agents.generation.tests.test_claude_core import _prepare_root
from agents.generation.tests.test_claude_core_owner_restart import (
    NEW_ROOT as UNUSED_ROOT,
    OLD_ROOT,
    OTHER_ROOT as FRESH_ROOT,
    _add_candidate,
    _authorize,
    _packet,
    _start,
    _takeover,
    restart_case,
)


UNRELATED_ROOT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _transfer(case: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "problem_id": "example",
        "statement_sha256": case["statement_sha256"],
        "root_session_id": UNUSED_ROOT,
        "successor_root_session_id": FRESH_ROOT,
        "expected_candidate_packet_sha256": (
            claude_core._reference_candidate_packet_sha256(_packet(case))
        ),
        "reason": "Owner requested transfer of unused authorization after source drift.",
    }
    arguments.update(overrides)
    return claude_core.transfer_council_restart(**arguments)


def _fresh_takeover(case: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "problem_id": "example",
        "statement_sha256": case["statement_sha256"],
        "root_session_id": FRESH_ROOT,
        "canonical_model": "claude-opus-5",
        "orchestration_mode": claude_core.OPUS_SOL_COUNCIL_MODE,
        "takeover_from": UNUSED_ROOT,
    }
    arguments.update(overrides)
    return _prepare_root(**arguments)


def _assert_no_admission(case: dict[str, Any]) -> None:
    assert case["dispatches"] == []
    assert not claude_core._council_pointer_path("example", FRESH_ROOT).exists()


def test_transfer_fence_requires_identical_bytes_for_equal_numeric_values(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_core.time, "time", lambda: 2_000_000_000.0)
    value = _transfer(transfer_case)
    altered = {**value, "authorized_at_unix": 2_000_000_000}
    assert altered == value
    fence = transfer_case["transfer_path"].with_name("source_drift_fence.json")
    claude_core._replace_canonical(fence, altered)
    assert fence.read_bytes() != transfer_case["transfer_path"].read_bytes()
    with pytest.raises(claude_core.ClaudeCoreError, match="retirement fence differs"):
        _fresh_takeover(transfer_case)
    _assert_no_admission(transfer_case)


@pytest.fixture
def transfer_case(
    restart_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    case = restart_case
    _authorize(case)
    _takeover(case)
    unused_dir = claude_core.STATE_ROOT / "example" / "roots" / UNUSED_ROOT
    case["unused_root_dir"] = unused_dir
    case["unused_manifest_path"] = unused_dir / "manifest.json"
    case["unused_manifest"] = json.loads(case["unused_manifest_path"].read_text())
    case["transfer_path"] = unused_dir / "owner_restart_transfer.json"
    case["historical_bytes"] = {
        path: path.read_bytes() for path in (
            case["authorization_path"], case["receipt_path"],
            case["pointer_path"], case["unused_manifest_path"],
        )
    }
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: "e" * 64)
    monkeypatch.setattr(claude_core, "_loaded_host_source_sha256", lambda: "e" * 64)
    return case


def test_restart_transfer_admits_only_the_fresh_epoch_and_preserves_history(
    transfer_case: dict[str, Any],
) -> None:
    case = transfer_case
    with pytest.raises(claude_core.ClaudeCoreError):
        claude_core._owner_council_restart_authorized(
            problem_id="example", statement_sha256=case["statement_sha256"],
            root_session_id=OLD_ROOT, successor_root_session_id=FRESH_ROOT,
            pointer=case["pointer"], receipt=case["receipt"],
            receipt_path=case["receipt_path"],
        )
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)
    with pytest.raises(claude_core.ClaudeCoreError):
        _fresh_takeover(case)
    assert not (claude_core.STATE_ROOT / "example/roots" / FRESH_ROOT).exists()

    transfer = _transfer(case)
    transfer_bytes = case["transfer_path"].read_bytes()
    assert json.loads(transfer_bytes) == transfer
    assert transfer["source_authorization_sha256"] == hashlib.sha256(
        case["historical_bytes"][case["authorization_path"]]
    ).hexdigest()
    assert transfer["abandoned_root_session_id"] == UNUSED_ROOT
    assert _transfer(case) == transfer
    assert case["transfer_path"].read_bytes() == transfer_bytes
    _assert_no_admission(case)

    successor = _fresh_takeover(case)
    assert successor["root_session_id"] == FRESH_ROOT
    assert successor["previous_root_session_id"] == UNUSED_ROOT
    assert successor["host_source_sha256"] == "e" * 64
    _assert_no_admission(case)
    started = _start(case, root_session_id=FRESH_ROOT)
    assert started["status"] == "completed"
    assert started["council_round"] == 2
    assert len(case["dispatches"]) == 1
    for path, original in case["historical_bytes"].items():
        assert path.read_bytes() == original
    assert case["transfer_path"].read_bytes() == transfer_bytes
    assert not (case["cohort_dir"] / "recovery_authorization.json").exists()


def test_restart_transfer_requires_actual_deployment_drift(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = transfer_case
    old_host = case["unused_manifest"]["host_source_sha256"]
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: old_host)
    monkeypatch.setattr(claude_core, "_loaded_host_source_sha256", lambda: old_host)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


def test_restart_transfer_rejects_an_already_occupied_successor_before_signing(
    transfer_case: dict[str, Any],
) -> None:
    case = transfer_case
    directory = claude_core.STATE_ROOT / "example/roots" / FRESH_ROOT
    directory.mkdir(mode=0o700)
    claude_core._write_once(directory / "manifest.json", {"occupied": True}, mode=0o400)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


def test_restart_transfer_fences_the_abandoned_authorization_if_old_runtime_returns(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = transfer_case
    _transfer(case)
    old_host = case["unused_manifest"]["host_source_sha256"]
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: old_host)
    monkeypatch.setattr(claude_core, "_loaded_host_source_sha256", lambda: old_host)
    with pytest.raises(claude_core.ClaudeCoreError):
        _start(case, root_session_id=UNUSED_ROOT)
    assert case["dispatches"] == []
    assert not claude_core._council_pointer_path("example", UNUSED_ROOT).exists()


def test_restart_transfer_recovers_exact_record_after_fence_before_mirror_interruption(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = transfer_case
    fence_path = case["unused_root_dir"] / "source_drift_fence.json"
    write_once = claude_core._write_once

    def interrupt_mirror(path: Path, value: object, **kwargs: Any) -> None:
        if path == case["transfer_path"]:
            raise OSError("Interrupted after durable source-drift fence.")
        write_once(path, value, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(claude_core, "_write_once", interrupt_mirror)
        with pytest.raises(OSError, match="Interrupted after durable"):
            _transfer(case)
    assert fence_path.is_file()
    assert not case["transfer_path"].exists()
    fence_bytes = fence_path.read_bytes()
    fenced_record = json.loads(fence_bytes)
    with pytest.raises(claude_core.ClaudeCoreError):
        _fresh_takeover(case)

    # An old deployment must fail at its pre-existing root retirement guard,
    # even before the new transfer sidecar has been mirrored successfully.
    old_host = case["unused_manifest"]["host_source_sha256"]
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(claude_core, "_host_source_sha256", lambda: old_host)
        old_runtime.setattr(claude_core, "_loaded_host_source_sha256", lambda: old_host)
        with pytest.raises(claude_core.ClaudeCoreError):
            with claude_core.root_authority_guard(
                problem_id="example", statement_sha256=case["statement_sha256"],
                root_session_id=UNUSED_ROOT,
            ):
                pytest.fail("The abandoned root passed its existing retirement guard.")

    recovered = _transfer(case)
    assert recovered == fenced_record
    assert fence_path.read_bytes() == fence_bytes
    assert case["transfer_path"].read_bytes() == fence_bytes
    _assert_no_admission(case)
    _fresh_takeover(case)
    assert _start(case, root_session_id=FRESH_ROOT)["council_round"] == 2
    for path, original in case["historical_bytes"].items():
        assert path.read_bytes() == original


@pytest.mark.parametrize("invalid", [
    {"root_session_id": OLD_ROOT},
    {"successor_root_session_id": UNUSED_ROOT},
    {"successor_root_session_id": OLD_ROOT},
    {"successor_root_session_id": "not-a-uuid"},
    {"expected_candidate_packet_sha256": "0" * 64},
    {"reason": ""},
    {"statement_sha256": "0" * 64},
])
def test_restart_transfer_rejects_invalid_owner_binding(
    transfer_case: dict[str, Any], invalid: dict[str, Any],
) -> None:
    case = transfer_case
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case, **invalid)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


@pytest.mark.parametrize("change", [
    "missing_authorization", "authorization_target", "authorization_runtime",
    "new_candidate", "frontier", "failed_receipt", "source_pointer",
])
def test_restart_transfer_rejects_invalid_historical_chain(
    transfer_case: dict[str, Any], change: str,
) -> None:
    case = transfer_case
    if change == "missing_authorization":
        case["authorization_path"].unlink()
    elif change in {"authorization_target", "authorization_runtime"}:
        value = json.loads(case["authorization_path"].read_text())
        key, replacement = (
            ("successor_root_session_id", UNRELATED_ROOT)
            if change == "authorization_target" else ("host_source_sha256", "e" * 64)
        )
        value[key] = replacement
        claude_core._replace_canonical(case["authorization_path"], value)
    elif change == "new_candidate":
        _add_candidate(case, "arrived_after_original_authorization")
    elif change == "frontier":
        case["frontier"] = "8" * 64
    elif change == "failed_receipt":
        value = {**case["receipt"], "retry_allowed": True}
        claude_core._replace_canonical(case["receipt_path"], value)
    else:
        value = {**case["pointer"], "prior_context_sha256": "7" * 64}
        claude_core._replace_canonical(case["pointer_path"], value)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


@pytest.mark.parametrize("binding", [
    "frontier", "candidate", "host_source", "python", "launcher", "successor",
    "source_authorization", "unused_manifest", "old_receipt",
])
def test_restart_transfer_rechecks_bindings_before_takeover(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, binding: str,
) -> None:
    case = transfer_case
    _transfer(case)
    overrides: dict[str, Any] = {}
    if binding == "frontier":
        case["frontier"] = "8" * 64
    elif binding == "candidate":
        _add_candidate(case, "arrived_after_transfer")
    elif binding == "host_source":
        monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: "f" * 64)
        monkeypatch.setattr(claude_core, "_loaded_host_source_sha256", lambda: "f" * 64)
    elif binding == "python":
        monkeypatch.setattr(claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", "5" * 64)
        overrides["python_runtime_sha256"] = "5" * 64
    elif binding == "launcher":
        monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", "5" * 64)
        overrides["root_launcher_sha256"] = "5" * 64
    elif binding == "successor":
        overrides["root_session_id"] = UNRELATED_ROOT
    elif binding == "source_authorization":
        value = json.loads(case["authorization_path"].read_text())
        value["reason"] = "A different authorization text."
        claude_core._replace_canonical(case["authorization_path"], value)
    elif binding == "unused_manifest":
        value = {**case["unused_manifest"], "created_at_unix": 1.0}
        claude_core._replace_canonical(case["unused_manifest_path"], value)
    else:
        value = {**case["receipt"], "elapsed_seconds": 2.0}
        claude_core._replace_canonical(case["receipt_path"], value)
    with pytest.raises(claude_core.ClaudeCoreError):
        _fresh_takeover(case, **overrides)
    authority = json.loads((claude_core.STATE_ROOT / "example/active_root.json").read_text())
    assert authority["root_session_id"] == UNUSED_ROOT
    _assert_no_admission(case)


@pytest.mark.parametrize("binding", ["frontier", "candidate", "source_authorization"])
def test_restart_transfer_rechecks_bindings_at_council_admission(
    transfer_case: dict[str, Any], binding: str,
) -> None:
    case = transfer_case
    _transfer(case)
    _fresh_takeover(case)
    if binding == "frontier":
        case["frontier"] = "8" * 64
    elif binding == "candidate":
        _add_candidate(case, "arrived_after_takeover")
    else:
        value = json.loads(case["authorization_path"].read_text())
        value["reason"] = "Authorization changed after takeover."
        claude_core._replace_canonical(case["authorization_path"], value)
    with pytest.raises(claude_core.ClaudeCoreError):
        _start(case, root_session_id=FRESH_ROOT)
    _assert_no_admission(case)


@pytest.mark.parametrize("changed", [
    {"successor_root_session_id": UNRELATED_ROOT},
    {"reason": "Different replay reason."},
])
def test_restart_transfer_sidecar_is_write_once(
    transfer_case: dict[str, Any], changed: dict[str, Any],
) -> None:
    case = transfer_case
    _transfer(case)
    original = case["transfer_path"].read_bytes()
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case, **changed)
    assert case["transfer_path"].read_bytes() == original
    _assert_no_admission(case)


def test_restart_transfer_does_not_chain_to_a_second_transfer(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = transfer_case
    _transfer(case)
    _fresh_takeover(case)
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: "f" * 64)
    monkeypatch.setattr(claude_core, "_loaded_host_source_sha256", lambda: "f" * 64)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case, root_session_id=FRESH_ROOT, successor_root_session_id=UNRELATED_ROOT)
    assert not (
        claude_core.STATE_ROOT / "example/roots" / FRESH_ROOT / "owner_restart_transfer.json"
    ).exists()
    _assert_no_admission(case)


@pytest.mark.parametrize("live_lock", ["cohort", "retrieval"])
def test_restart_transfer_refuses_live_execution_locks(
    transfer_case: dict[str, Any], live_lock: str,
) -> None:
    case = transfer_case
    path = (
        case["cohort_dir"] / "cohort.lock" if live_lock == "cohort"
        else case["council_dir"] / "blind_retrieval.lock"
    )
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(claude_core.ClaudeCoreError):
            _transfer(case)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


def test_restart_transfer_refuses_unsettled_publication(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def publication_pending(**_: Any) -> None:
        raise claude_core.ClaudeCoreError("Synthetic unsettled publication.")

    monkeypatch.setattr(claude_core, "_assert_no_unsettled_publication_finalization", publication_pending)
    with pytest.raises(claude_core.ClaudeCoreError, match="unsettled publication"):
        _transfer(transfer_case)
    assert not transfer_case["transfer_path"].exists()
    _assert_no_admission(transfer_case)


def test_restart_transfer_refuses_any_local_council_pointer(
    transfer_case: dict[str, Any],
) -> None:
    case = transfer_case
    path = claude_core._council_pointer_path("example", UNUSED_ROOT)
    claude_core._write_once(path, {"malformed": True}, mode=0o400)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


def test_restart_transfer_keeps_original_candidate_dicts_unchanged(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = transfer_case
    packet = copy.deepcopy(_packet(case))
    packet["candidates"][0]["target_claims"] = ["Replace the original claim."]
    monkeypatch.setattr(claude_core, "reference_candidate_packet", lambda **_: packet)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


@pytest.mark.parametrize("artifact", [
    "blind_request.json", "revision_intent.json", "audit_dispatch.json",
    "blind_execution.json", "blind_worker.json", "experiment.request.json",
])
def test_restart_transfer_refuses_orphan_admission_artifacts(
    transfer_case: dict[str, Any], artifact: str,
) -> None:
    case = transfer_case
    if artifact == "experiment.request.json":
        directory = (
            claude_core.STATE_ROOT / "example" / claude_core.PRIVATE_MATH_EXPERIMENT_DIRECTORY
            / case["statement_sha256"] / UNUSED_ROOT
        )
        directory.mkdir(parents=True)
    else:
        directory = claude_core._council_dir("example", "council_" + "c" * 32)
    path = directory / artifact
    claude_core._write_once(path, {
        "problem_id": "example", "statement_sha256": case["statement_sha256"],
        "root_session_id": UNUSED_ROOT,
    }, mode=0o400)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case)
    assert not case["transfer_path"].exists()
    assert path.exists()
    _assert_no_admission(case)


@pytest.mark.parametrize("payload", [
    {}, {"root_session_id": True}, {"root_session_id": "not-a-uuid"},
    {"root_session_id": OLD_ROOT, "binding": {"root_session_id": UNUSED_ROOT}},
])
def test_restart_transfer_refuses_unclassified_or_nested_admission_identity(
    transfer_case: dict[str, Any], payload: dict[str, Any],
) -> None:
    case = transfer_case
    directory = claude_core._council_dir("example", "council_" + "c" * 32)
    claude_core._write_once(directory / "intent.json", payload, mode=0o400)
    with pytest.raises(claude_core.ClaudeCoreError):
        _transfer(case)
    assert not case["transfer_path"].exists()
    _assert_no_admission(case)


def test_restart_transfer_refuses_stale_prelock_candidate_packet(
    transfer_case: dict[str, Any],
) -> None:
    case = transfer_case
    _transfer(case)
    _fresh_takeover(case)
    with pytest.raises(claude_core.ClaudeCoreError):
        claude_core._owner_council_restart_authorized(
            problem_id="example", statement_sha256=case["statement_sha256"],
            root_session_id=OLD_ROOT, successor_root_session_id=FRESH_ROOT,
            pointer=case["pointer"], receipt=case["receipt"], receipt_path=case["receipt_path"],
            expected_candidate_packet_sha256="0" * 64,
        )
    _assert_no_admission(case)


def test_restart_transfer_is_not_exposed_to_model_tools(
    transfer_case: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = transfer_case
    _transfer(case)
    _fresh_takeover(case)
    bindings = {
        "PROBLEM_ID": "example", "STATEMENT_SHA256": case["statement_sha256"],
        "SESSION_ID": FRESH_ROOT, "MODEL": "claude-opus-5", "LAUNCH_MODEL": "claude-opus-5[1m]",
        "PROVIDER": "vertex", "PROVIDER_BINDING_SHA256": hashlib.sha256(b"claude-opus-5[1m]").hexdigest(),
        "CLI_SHA256": "1" * 64, "CLI_VERSION": "test-claude-2.1.246", "CONTEXT_WINDOW": "1000000",
        "PYTHON_RUNTIME_SHA256": "3" * 64, "LAUNCHER_SHA256": "4" * 64,
        "ORCHESTRATION_MODE": claude_core.OPUS_SOL_COUNCIL_MODE,
        "CODEX_BIN": str(Path(sys.executable).resolve()),
    }
    for key, value in bindings.items():
        monkeypatch.setenv(f"RETHLAS_CLAUDE_ROOT_{key}", value)
    app = claude_core.build_mcp_app()
    manager = getattr(app, "_tool_manager", None)
    tools = getattr(manager, "_tools", {}) if manager is not None else {}
    assert "start_route_council" in tools
    assert not any("restart" in name or "transfer" in name for name in tools)
    _assert_no_admission(case)


@pytest.mark.parametrize("owner_command", [
    "--authorize-council-restart", "--transfer-council-restart",
])
def test_restart_owner_launcher_bootstraps_exact_runtime_without_model_turn(
    tmp_path: Path, owner_command: str,
) -> None:
    from agents.generation.tests.test_runner_mock import (
        _make_runner_tree, _mock_environment,
    )

    runner, fake_bin = _make_runner_tree(tmp_path)
    launcher = runner.with_name("run_claude_core.sh")
    core_source = runner.parents[1].parent / "claude_core.py"
    main = '\nif __name__ == "__main__":\n    main()\n'
    # The fixture probe runs only after the real launcher has captured its
    # source and dependency bundle. Owner API semantics are tested above.
    probe = '''
if __name__ == "__main__" and sys.argv[1:2] in (
    ["--authorize-council-restart"], ["--transfer-council-restart"]
):
    if any(os.environ.get(key) is not None for key in (
        "RETHLAS_MODEL_POLICY_PROFILE", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
    )):
        raise SystemExit("owner entry inherited model-turn settings")
    _require_root_execution_epoch(
        python_runtime_sha256=_RETHLAS_PINNED_PYTHON_SHA256,
        root_launcher_sha256=os.environ.get("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256"),
    )
    legacy = _legacy()
    assert _RUNTIME_BUNDLE_DIR is not None
    assert Path(legacy.__file__).resolve().parent == _RUNTIME_MCP_ROOT.resolve()
    print(json.dumps({
        "arguments": sys.argv[1:],
        "host_source_sha256": _require_loaded_host_source_current(),
        "python_runtime_sha256": _RETHLAS_PINNED_PYTHON_SHA256,
        "root_launcher_sha256": os.environ["RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256"],
        "frozen_dependency_bundle": True,
    }))
    raise SystemExit(0)
'''
    source = core_source.read_text()
    assert source.count(main) == 1
    core_source.write_text(source.replace(main, probe + main, 1))
    codex_calls = tmp_path / "paid-codex.jsonl"
    claude_calls = tmp_path / "paid-claude.jsonl"
    env = _mock_environment(runner, fake_bin, mode="trusted", extra_environment={
        "RETHLAS_MAIN_AGENT": "invalid-admin-leftover",
        "RETHLAS_MODEL_POLICY_PROFILE": "invalid-admin-leftover",
        "RETHLAS_CLAUDE_CONTEXT_WINDOW": "invalid",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1",
        "MOCK_CODEX_CALLS_FILE": str(codex_calls),
        "MOCK_CLAUDE_CALLS_FILE": str(claude_calls),
    })
    arguments = [owner_command, "example", "1" * 64, UNUSED_ROOT, FRESH_ROOT,
                 "2" * 64, "Transfer after drift.\nKeep the failed receipt unchanged."]
    completed = subprocess.run(
        [str(launcher), *arguments], cwd=runner.parents[1], env=env,
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["arguments"] == arguments
    assert result["frozen_dependency_bundle"] is True
    assert result["host_source_sha256"] == hashlib.sha256(core_source.read_bytes()).hexdigest()
    assert result["root_launcher_sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    assert result["python_runtime_sha256"] == hashlib.sha256(
        (runner.parents[2] / ".generation-venv/bin/python").read_bytes()
    ).hexdigest()
    assert not codex_calls.exists()
    assert not claude_calls.exists()
    assert not (runner.parents[2] / ".claude_core/example").exists()
