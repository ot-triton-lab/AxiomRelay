from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

import pytest
import jsonschema

from agents import claude_core


_TRACKED_EXAMPLE = Path(__file__).with_name("fixtures") / "example.md"
_SYSTEM_TRUE = Path(shutil.which("true") or "/usr/bin/true").resolve(strict=True)


@pytest.fixture(autouse=True)
def _provide_public_example_statement() -> object:
    """Make the synthetic statement available without shipping user data."""

    statement = claude_core.GENERATION_ROOT / "data" / "example.md"
    existed = statement.is_file()
    original = statement.read_bytes() if existed else None
    if not existed:
        statement.parent.mkdir(parents=True, exist_ok=True)
        statement.write_bytes(_TRACKED_EXAMPLE.read_bytes())
    try:
        yield
    finally:
        if existed:
            assert original is not None
            if not statement.is_file() or statement.read_bytes() != original:
                statement.parent.mkdir(parents=True, exist_ok=True)
                statement.write_bytes(original)
        else:
            statement.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _isolate_publication_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", tmp_path / "receipts-default")
    monkeypatch.setattr(
        claude_core,
        "_RETHLAS_PINNED_PYTHON_SHA256",
        "3" * 64,
        raising=False,
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", "4" * 64)


@pytest.fixture(autouse=True)
def _run_council_phase_workers_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launch(**arguments: object) -> int:
        claude_core._execute_council_phase_worker(
            phase=str(arguments["phase"]),
            council_id=str(arguments["council_id"]),
            problem_id=str(arguments["problem_id"]),
            statement_sha256=str(arguments["statement_sha256"]),
            root_session_id=str(arguments["root_session_id"]),
        )
        return os.getpid()

    monkeypatch.setattr(claude_core, "_launch_council_phase_worker", launch)


def _statement_digest(problem_id: str = "example") -> str:
    path = claude_core.GENERATION_ROOT / "data" / f"{problem_id}.md"
    if problem_id == "example" and not path.is_file():
        path = _TRACKED_EXAMPLE
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _statement_text(problem_id: str = "example") -> str:
    path = claude_core.GENERATION_ROOT / "data" / f"{problem_id}.md"
    if problem_id == "example" and not path.is_file():
        path = _TRACKED_EXAMPLE
    return path.read_bytes().decode("utf-8")


def _plans() -> list[dict[str, object]]:
    return [
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


def _completion_evidence() -> dict[str, object]:
    return {
        "schema_version": claude_core.COHORT_COMPLETION_EVIDENCE_SCHEMA,
        "route_report_record_ids": [
            "mem_" + "1" * 64,
            "mem_" + "2" * 64,
            "mem_" + "3" * 64,
        ],
        "synthesis_record_id": "mem_" + "4" * 64,
        "external_plan_checkpoint_in_delta": False,
    }


def _current_cohort_intent(
    *,
    cohort_id: str,
    problem_id: str,
    statement_sha256: str,
    plan_sha256: str,
    root_session_id: str,
    max_report_log_bytes: int | None = None,
) -> dict[str, object]:
    """Build a reachable current-generation intent for recovery tests."""

    return {
        "schema_version": claude_core.COHORT_INTENT_SCHEMA,
        "state": "submitted",
        "cohort_id": cohort_id,
        "problem_id": problem_id,
        "statement_sha256": statement_sha256,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "timeout_seconds": 60,
        "runner_path": str(claude_core.RUNNER.resolve()),
        "runner_sha256": "b" * 64,
        "runner_closure_sha256": "c" * 64,
        "codex_bin": str(Path(sys.executable).resolve()),
        "codex_bin_sha256": "d" * 64,
        "host_source_sha256": "e" * 64,
        "max_report_log_bytes": (
            claude_core.MAX_REPORT_LOG_BYTES
            if max_report_log_bytes is None
            else max_report_log_bytes
        ),
        "created_at_unix": time.time(),
    }


def _blind_plan_slots(
    plans: list[dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    source_plans = plans if plans is not None else _plans()
    assert len(source_plans) == len(claude_core.COUNCIL_BLIND_ROUTE_SLOTS)
    slots: dict[str, dict[str, object]] = {}
    for index, ((role, plan_id), plan) in enumerate(
        zip(claude_core.COUNCIL_BLIND_ROUTE_SLOTS.items(), source_plans),
        start=1,
    ):
        slots[role] = {
            "role": role,
            "separation_claim": (
                f"Route role {role} uses genuinely separate mechanism {index}."
            ),
            "plan": {**plan, "plan_id": plan_id},
        }
    return slots


def _prepare_root(
    *,
    problem_id: str,
    statement_sha256: str,
    root_session_id: str,
    canonical_model: str,
    python_runtime_sha256: str = "3" * 64,
    root_launcher_sha256: str = "4" * 64,
    orchestration_mode: str = claude_core.SINGLE_ROOT_MODE,
    takeover_from: str | None = None,
) -> dict[str, object]:
    launch_model = (
        "claude-opus-5[1m]"
        if canonical_model == "claude-opus-5"
        else "claude-fable-5"
    )
    return claude_core.prepare_root_manifest(
        problem_id=problem_id,
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        canonical_model=canonical_model,
        launch_model=launch_model,
        provider="vertex",
        provider_binding_sha256=hashlib.sha256(launch_model.encode()).hexdigest(),
        claude_cli_sha256="1" * 64,
        claude_cli_version="test-claude-2.1.246",
        model_context_window=(1_000_000 if "opus" in canonical_model else 200_000),
        python_runtime_sha256=python_runtime_sha256,
        root_launcher_sha256=root_launcher_sha256,
        orchestration_mode=orchestration_mode,
        takeover_from=takeover_from,
    )


def test_plan_set_requires_exactly_three_distinct_routes() -> None:
    digest = _statement_digest()
    accepted = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id="root-session-1",
    )
    assert accepted["schema_version"] == claude_core.SCHEMA
    assert [plan["plan_id"] for plan in accepted["plans"]] == [
        "route_1",
        "route_2",
        "route_3",
    ]

    with pytest.raises(claude_core.ClaudeCoreError, match="exactly three"):
        claude_core.validate_plan_set(
            problem_id="example",
            statement_sha256=digest,
            plans=_plans()[:2],
            root_session_id="root-session-1",
        )

    cosmetic_duplicate = _plans()
    cosmetic_duplicate[1]["mechanism"] = "ＭＥＣＨＡＮＩＳＭ—1!!!"
    with pytest.raises(claude_core.ClaudeCoreError, match="textually distinct"):
        claude_core.validate_plan_set(
            problem_id="example",
            statement_sha256=digest,
            plans=cosmetic_duplicate,
            root_session_id="root-session-1",
        )


def test_blind_council_schema_uses_exact_fixed_diversity_slots() -> None:
    digest = _statement_digest()
    council_id = "council_" + "a" * 32
    schema = claude_core._council_output_schema(
        "blind",
        request={"council_id": council_id, "statement_sha256": digest},
    )
    assert "plans" not in schema["properties"]
    slots_schema = schema["properties"]["plan_slots"]
    assert slots_schema["type"] == "object"
    assert slots_schema["additionalProperties"] is False
    assert set(slots_schema["required"]) == set(
        claude_core.COUNCIL_BLIND_ROUTE_SLOTS
    )
    for role, plan_id in claude_core.COUNCIL_BLIND_ROUTE_SLOTS.items():
        role_schema = slots_schema["properties"][role]
        assert role_schema["properties"]["role"]["enum"] == [role]
        assert role_schema["properties"]["plan"]["properties"]["plan_id"][
            "enum"
        ] == [plan_id]

    report = {
        "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
        "council_id": council_id,
        "statement_sha256": digest,
        "plan_slots": _blind_plan_slots(),
        "global_risks": ["Check the formulation."],
        "comparative_note": "The mechanisms are genuinely separate.",
    }
    jsonschema.validate(report, schema)
    normalized = claude_core._validate_council_report(
        report,
        phase="blind",
        council_id=council_id,
        problem_id="example",
        statement_sha256=digest,
        root_session_id="root-session-1",
    )
    assert set(normalized["plan_slots"]) == set(
        claude_core.COUNCIL_BLIND_ROUTE_SLOTS
    )


def test_blind_council_rejects_role_and_normalized_diversity_spoofs() -> None:
    digest = _statement_digest()
    council_id = "council_" + "b" * 32
    base = {
        "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
        "council_id": council_id,
        "statement_sha256": digest,
        "plan_slots": _blind_plan_slots(),
        "global_risks": ["Check the formulation."],
        "comparative_note": "The mechanisms are genuinely separate.",
    }

    missing_role = json.loads(json.dumps(base))
    del missing_role["plan_slots"]["adversarial_mechanism"]
    with pytest.raises(claude_core.ClaudeCoreError, match="slots are invalid"):
        claude_core._validate_council_report(
            missing_role,
            phase="blind",
            council_id=council_id,
            problem_id="example",
            statement_sha256=digest,
            root_session_id="root-session-1",
        )

    duplicate_claim = json.loads(json.dumps(base))
    duplicate_claim["plan_slots"]["orthogonal_mechanism"][
        "separation_claim"
    ] = duplicate_claim["plan_slots"]["primary_mechanism"][
        "separation_claim"
    ].upper() + "!!!"
    with pytest.raises(claude_core.ClaudeCoreError, match="claims must be distinct"):
        claude_core._validate_council_report(
            duplicate_claim,
            phase="blind",
            council_id=council_id,
            problem_id="example",
            statement_sha256=digest,
            root_session_id="root-session-1",
        )

    duplicate_mechanism = json.loads(json.dumps(base))
    duplicate_mechanism["plan_slots"]["orthogonal_mechanism"]["plan"][
        "mechanism"
    ] = "ＭＥＣＨＡＮＩＳＭ—1!!!"
    with pytest.raises(claude_core.ClaudeCoreError, match="textually distinct"):
        claude_core._validate_council_report(
            duplicate_mechanism,
            phase="blind",
            council_id=council_id,
            problem_id="example",
            statement_sha256=digest,
            root_session_id="root-session-1",
        )


def test_blind_council_accepts_durable_v1_slate() -> None:
    digest = _statement_digest()
    council_id = "council_" + "c" * 32
    report = {
        "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA_PREVIOUS,
        "council_id": council_id,
        "statement_sha256": digest,
        "plans": _plans(),
        "global_risks": ["Historical risk."],
        "comparative_note": "Historical durable slate.",
    }
    assert claude_core._validate_council_report(
        report,
        phase="blind",
        council_id=council_id,
        problem_id="example",
        statement_sha256=digest,
        root_session_id="root-session-1",
    )["plans"] == _plans()


def test_start_route_council_reads_frontier_inside_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    frontier_value = ["4" * 64]
    frontier_entered = threading.Event()
    release_frontier = threading.Event()
    competing_writer_entered = threading.Event()

    def blocking_frontier(_problem_id: str) -> dict[str, str]:
        observed = frontier_value[0]
        frontier_entered.set()
        assert release_frontier.wait(timeout=10)
        return {"frontier_sha256": observed}

    def advance_frontier() -> None:
        with claude_core.root_authority_guard(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
        ):
            competing_writer_entered.set()
            frontier_value[0] = "5" * 64

    monkeypatch.setattr(claude_core, "_frontier", blocking_frontier)
    monkeypatch.setattr(
        claude_core,
        "_run_council_phase",
        lambda **_kwargs: {
            "status": "operational_blocked",
            "error": "synthetic stop after pointer commit",
        },
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        started = executor.submit(
            claude_core.start_route_council,
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            opus_plans=_plans(),
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
        assert frontier_entered.wait(timeout=5)
        writer = executor.submit(advance_frontier)
        try:
            assert not competing_writer_entered.wait(timeout=0.3)
        finally:
            release_frontier.set()
        result = started.result(timeout=5)
        writer.result(timeout=5)

    assert result["base_frontier_sha256"] == "4" * 64
    pointer = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", root_session_id),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    assert pointer["base_frontier_sha256"] == "4" * 64
    assert frontier_value[0] == "5" * 64


def test_start_route_council_checks_publication_finalization_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    frontier_called = False

    def frontier_must_not_run(_problem_id: str) -> dict[str, str]:
        nonlocal frontier_called
        frontier_called = True
        return {"frontier_sha256": "4" * 64}

    def unresolved(**_arguments: object) -> None:
        raise claude_core.ClaudeCoreError("publication finalization is unresolved")

    monkeypatch.setattr(claude_core, "_frontier", frontier_must_not_run)
    monkeypatch.setattr(
        claude_core, "_assert_no_unsettled_publication_finalization", unresolved
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="unresolved"):
        claude_core.start_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            opus_plans=_plans(),
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert frontier_called is False
    assert not claude_core._council_pointer_path(
        "example", root_session_id
    ).exists()
    assert not (tmp_path / "state" / "example" / "councils").exists()


def test_claude_mcp_config_has_bounded_long_call_timeout() -> None:
    config = json.loads(
        (claude_core.GENERATION_ROOT / ".mcp.json").read_text(encoding="utf-8")
    )
    assert set(config) == {"mcpServers"}
    assert set(config["mcpServers"]) == {"rethlas-root"}
    server = config["mcpServers"]["rethlas-root"]
    assert server["timeout"] == 14_500_000
    assert server["command"] == "${RETHLAS_CLAUDE_PINNED_PYTHON_BIN}"
    assert server["args"][:3] == ["-I", "-B", "-c"]
    assert "_RETHLAS_LOADED_SOURCE_SHA256" in server["args"][3]
    assert "_RETHLAS_PINNED_PYTHON_BIN" in server["args"][3]
    assert server["args"][-3:] == [
        "${RETHLAS_CLAUDE_CORE_SNAPSHOT}",
        "${RETHLAS_CLAUDE_CORE_SOURCE_SHA256}",
        "${RETHLAS_CLAUDE_CORE_ORIGIN}",
    ]
    assert not any(argument.endswith("/../claude_core.py") for argument in server["args"])


def test_memory_channel_type_exposes_exact_channel_set() -> None:
    assert set(get_args(claude_core.MemoryChannel)) == {
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


def test_root_manifest_fences_session_model_and_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    session_id = "12345678-1234-4123-8123-123456789abc"
    receipt = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )
    assert receipt["authority"] == "canonical_mathematical_root"
    validated = claude_core.validate_root_manifest(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
    )
    assert validated["canonical_model"] == "claude-opus-5"
    with pytest.raises(claude_core.ClaudeCoreError, match="active authority"):
        claude_core.validate_root_manifest(
            problem_id="example",
            statement_sha256=digest,
            root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )
    successor = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=successor,
        canonical_model="claude-fable-5",
        takeover_from=session_id,
    )
    active = claude_core.get_active_root(problem_id="example", statement_sha256=digest)
    assert active["root_session_id"] == successor
    assert active["root_epoch"] == 2
    with pytest.raises(claude_core.ClaudeCoreError, match="active authority"):
        claude_core.validate_root_manifest(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=session_id,
        )


def test_root_manifest_rejects_provider_or_cli_drift_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )

    with pytest.raises(
        claude_core.ClaudeCoreError, match="provider binding mismatch"
    ):
        claude_core.prepare_root_manifest(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=session_id,
            canonical_model="claude-opus-5",
            launch_model="claude-opus-5[1m]",
            provider="vertex",
            provider_binding_sha256="9" * 64,
            claude_cli_sha256="1" * 64,
            claude_cli_version="test-claude-2.1.246",
            model_context_window=1_000_000,
            python_runtime_sha256="3" * 64,
            root_launcher_sha256="4" * 64,
        )


def test_root_execution_epoch_drift_requires_a_fresh_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    python_a = "3" * 64
    launcher_a = "4" * 64
    python_b = "5" * 64
    launcher_b = "6" * 64
    monkeypatch.setattr(
        claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", python_a, raising=False
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", launcher_a)
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    successor = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    receipt = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        python_runtime_sha256=python_a,
        root_launcher_sha256=launcher_a,
    )
    assert receipt["python_runtime_sha256"] == python_a
    assert receipt["root_launcher_sha256"] == launcher_a
    assert claude_core.validate_root_manifest(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
    )["binding_complete"] is True

    monkeypatch.setattr(
        claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", python_b, raising=False
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", launcher_b)
    with pytest.raises(claude_core.ClaudeCoreError, match="execution epoch"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=first_root,
            canonical_model="claude-opus-5",
            python_runtime_sha256=python_b,
            root_launcher_sha256=launcher_b,
        )

    successor_receipt = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=successor,
        canonical_model="claude-opus-5",
        python_runtime_sha256=python_b,
        root_launcher_sha256=launcher_b,
        takeover_from=first_root,
    )
    assert successor_receipt["root_epoch"] == 2
    assert successor_receipt["previous_root_session_id"] == first_root
    assert successor_receipt["python_runtime_sha256"] == python_b
    assert successor_receipt["root_launcher_sha256"] == launcher_b


@pytest.mark.parametrize("missing", ["python", "launcher"])
def test_root_execution_epoch_missing_live_binding_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )
    if missing == "python":
        monkeypatch.delattr(
            claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", raising=False
        )
    else:
        monkeypatch.delenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", raising=False)

    with pytest.raises(claude_core.ClaudeCoreError, match="execution epoch"):
        claude_core.validate_root_manifest(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=session_id,
        )
    active = claude_core.get_active_root(
        problem_id="example", statement_sha256=digest
    )
    assert active["root_session_id"] == session_id
    assert active["binding_complete"] is False


def test_root_manifest_commit_gate_rechecks_statement_and_loaded_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement_path = data_root / "statement-race.md"
    statement_a = b"Statement A.\n"
    statement_b = b"Statement B.\n"
    statement_path.write_bytes(statement_a)
    digest_a = hashlib.sha256(statement_a).hexdigest()
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    real_statement = claude_core._statement
    statement_reads = 0

    def change_before_commit(problem_id: str) -> tuple[Path, bytes, str]:
        nonlocal statement_reads
        statement_reads += 1
        if statement_reads == 2:
            statement_path.write_bytes(statement_b)
        return real_statement(problem_id)

    monkeypatch.setattr(claude_core, "_statement", change_before_commit)
    with pytest.raises(claude_core.ClaudeCoreError, match="before root manifest"):
        _prepare_root(
            problem_id="statement-race",
            statement_sha256=digest_a,
            root_session_id="12345678-1234-4123-8123-123456789abc",
            canonical_model="claude-opus-5",
        )
    problem_state = state_root / "statement-race"
    assert not (problem_state / "active_root.json").exists()
    assert not (problem_state / "roots").exists()

    source_statement = data_root / "source-race.md"
    source_statement.write_bytes(statement_a)
    monkeypatch.setattr(claude_core, "_statement", real_statement)
    monkeypatch.setattr(claude_core, "_loaded_host_source_sha256", lambda: "a" * 64)
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: "b" * 64)
    with pytest.raises(claude_core.ClaudeCoreError, match="before root manifest"):
        _prepare_root(
            problem_id="source-race",
            statement_sha256=digest_a,
            root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            canonical_model="claude-opus-5",
        )
    source_state = state_root / "source-race"
    assert not (source_state / "active_root.json").exists()
    assert not (source_state / "roots").exists()


def test_committed_root_takeover_replays_after_authority_replace_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    second_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
    )
    real_replace = claude_core._replace_canonical
    crashed = False

    def replace_then_crash(path: Path, value: object) -> None:
        nonlocal crashed
        real_replace(path, value)
        if not crashed:
            crashed = True
            raise RuntimeError("synthetic crash after authority replace")

    monkeypatch.setattr(claude_core, "_replace_canonical", replace_then_crash)
    with pytest.raises(RuntimeError, match="after authority replace"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=second_root,
            canonical_model="claude-fable-5",
            takeover_from=first_root,
        )

    active = claude_core.get_active_root(
        problem_id="example", statement_sha256=digest
    )
    assert active["root_session_id"] == second_root
    assert active["previous_root_session_id"] == first_root
    manifest_path = (
        tmp_path / "state" / "example" / "roots" / second_root / "manifest.json"
    )
    committed_manifest = manifest_path.read_bytes()

    replay = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
        canonical_model="claude-fable-5",
        takeover_from=first_root,
    )
    assert replay["root_epoch"] == 2
    assert replay["previous_root_session_id"] == first_root
    assert manifest_path.read_bytes() == committed_manifest

    with pytest.raises(claude_core.ClaudeCoreError, match="not the committed"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=second_root,
            canonical_model="claude-fable-5",
            takeover_from="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )


def test_root_takeover_refuses_an_unsettled_predecessor_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    second_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=first_root,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    cohort_id = "cohort_" + plan_sha256[:32]
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True)
    claude_core._write_once(
        state_dir / "intent.json",
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA_LEGACY,
            "state": "submitted",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": first_root,
            "created_at_unix": 1.0,
        },
        mode=0o400,
    )

    with pytest.raises(claude_core.ClaudeCoreError, match="unsettled"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=second_root,
            canonical_model="claude-fable-5",
            takeover_from=first_root,
        )
    active = claude_core.get_active_root(
        problem_id="example", statement_sha256=digest
    )
    assert active["root_session_id"] == first_root


def test_previous_v5_single_root_can_fence_a_legacy_cohort_and_take_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    old_root = "12345678-1234-4123-8123-123456789abc"
    new_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=old_root,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    cohort_id = "cohort_" + plan_sha256[:32]
    cohort_dir = state_root / "example" / cohort_id
    cohort_dir.mkdir(parents=True)
    claude_core._write_once(
        cohort_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        cohort_dir / "intent.json",
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA_LEGACY,
            "state": "submitted",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": old_root,
            "created_at_unix": 1.0,
        },
        mode=0o400,
    )
    second_plans = _plans()
    second_plans[0] = {
        **second_plans[0],
        "plan_summary": "a second pre-fence plan",
    }
    second_plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=second_plans,
        root_session_id=old_root,
    )
    second_plan_sha256 = claude_core._plan_set_sha256(second_plan_set)
    second_cohort_id = "cohort_" + second_plan_sha256[:32]
    second_cohort_dir = state_root / "example" / second_cohort_id
    second_cohort_dir.mkdir(parents=True)
    claude_core._write_once(
        second_cohort_dir / f"plan_{second_plan_sha256}.json",
        second_plan_set,
        mode=0o400,
    )
    claude_core._write_once(
        second_cohort_dir / "intent.json",
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA_LEGACY,
            "state": "submitted",
            "cohort_id": second_cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": second_plan_sha256,
            "root_session_id": old_root,
            "created_at_unix": 2.0,
        },
        mode=0o400,
    )
    problem_dir = state_root / "example"
    manifest_path = problem_dir / "roots" / old_root / "manifest.json"
    authority_path = problem_dir / "active_root.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = claude_core.ROOT_MANIFEST_SCHEMA_PREVIOUS
    authority["schema_version"] = claude_core.ROOT_AUTHORITY_SCHEMA_PREVIOUS
    for field in ("python_runtime_sha256", "root_launcher_sha256"):
        manifest.pop(field)
        authority.pop(field)
    claude_core._replace_canonical(manifest_path, manifest)
    claude_core._replace_canonical(authority_path, authority)

    migrated = claude_core.migrate_legacy_cohort_intent(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        cohort_id=cohort_id,
        plan_sha256=plan_sha256,
        reason="Fence the historical pre-execution intent before takeover.",
    )
    assert migrated["status"] == "operationally_blocked"
    second_lock = open(second_cohort_dir / "cohort.lock", "a+b")
    try:
        fcntl.flock(second_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(claude_core.ClaudeCoreError, match="cohort is active"):
            claude_core.migrate_legacy_cohort_intent(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=old_root,
                cohort_id=second_cohort_id,
                plan_sha256=second_plan_sha256,
                reason="Fence the second historical intent after it stops.",
            )
    finally:
        fcntl.flock(second_lock, fcntl.LOCK_UN)
        second_lock.close()
    second_migration = claude_core.migrate_legacy_cohort_intent(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        cohort_id=second_cohort_id,
        plan_sha256=second_plan_sha256,
        reason="Fence the second historical intent after it stops.",
    )
    assert (
        second_migration["root_retirement_fence_sha256"]
        == migrated["root_retirement_fence_sha256"]
    )
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=new_root,
        canonical_model="claude-opus-5",
        takeover_from=old_root,
    )
    assert successor["previous_root_session_id"] == old_root


def test_legacy_cohort_migration_fences_a_queued_root_admission_before_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    old_root = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=old_root,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    cohort_id = "cohort_" + plan_sha256[:32]
    cohort_dir = state_root / "example" / cohort_id
    cohort_dir.mkdir(parents=True)
    claude_core._write_once(
        cohort_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        cohort_dir / "intent.json",
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA_LEGACY,
            "state": "submitted",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": old_root,
            "created_at_unix": 1.0,
        },
        mode=0o400,
    )

    queued_plan_sha256 = "f" * 64
    queued_cohort_id = "cohort_" + "e" * 32
    queued_dir = state_root / "example" / queued_cohort_id
    queued_dir.mkdir(parents=True)
    fence_path = (
        state_root
        / "example"
        / "roots"
        / old_root
        / "source_drift_fence.json"
    )
    queued_process_started = threading.Event()
    begin_queued_admission = threading.Event()
    queued_admission_entered = threading.Event()
    fence_written = threading.Event()
    finish_migration = threading.Event()
    original_write_once = claude_core._write_once

    def pause_after_root_retirement_fence(
        path: Path, value: object, *, mode: int = 0o600
    ) -> str:
        digest_value = original_write_once(path, value, mode=mode)
        if path == fence_path:
            fence_written.set()
            assert finish_migration.wait(5)
        return digest_value

    monkeypatch.setattr(
        claude_core, "_write_once", pause_after_root_retirement_fence
    )

    def queued_admission() -> object:
        queued_process_started.set()
        assert begin_queued_admission.wait(5)
        queued_admission_entered.set()
        return claude_core._admit_cohort_intent(
            state_dir=queued_dir,
            receipt_path=queued_dir / "receipt.json",
            intent_path=queued_dir / "intent.json",
            cohort_id=queued_cohort_id,
            problem_id="example",
            statement_sha256=digest,
            plan_sha256=queued_plan_sha256,
            root_session_id=old_root,
            timeout_seconds=60,
            codex_bin=Path(sys.executable),
        )

    def migrate() -> dict[str, object]:
        return claude_core.migrate_legacy_cohort_intent(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=old_root,
            cohort_id=cohort_id,
            plan_sha256=plan_sha256,
            reason="Retire the root before releasing a queued admission.",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        queued = pool.submit(queued_admission)
        assert queued_process_started.wait(5)
        migration = pool.submit(migrate)
        assert fence_written.wait(5)
        begin_queued_admission.set()
        assert queued_admission_entered.wait(5)
        try:
            assert not (queued_dir / "intent.json").exists()
        finally:
            finish_migration.set()
        assert migration.result(timeout=5)["status"] == "operationally_blocked"
        with pytest.raises(claude_core.ClaudeCoreError, match="terminally fenced"):
            queued.result(timeout=5)
    assert not (queued_dir / "intent.json").exists()


def test_owner_migrations_reject_origin_replacement_before_any_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    cohort_id = "cohort_" + plan_sha256[:32]
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        state_dir / "intent.json",
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA_LEGACY,
            "state": "submitted",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "created_at_unix": 1.0,
        },
        mode=0o400,
    )
    loaded_source_sha256 = claude_core._host_source_sha256()
    replacement_source_sha256 = (
        "0" * 64 if loaded_source_sha256 != "0" * 64 else "1" * 64
    )
    monkeypatch.setattr(
        claude_core,
        "_RETHLAS_LOADED_SOURCE_SHA256",
        loaded_source_sha256,
        raising=False,
    )
    monkeypatch.setattr(
        claude_core,
        "_host_source_sha256",
        lambda: replacement_source_sha256,
    )
    expected_error = "changed after the authenticated source snapshot"
    with pytest.raises(claude_core.ClaudeCoreError, match=expected_error):
        claude_core.migrate_stale_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            reason="This must not fence through a stale loaded image.",
            confirm_source_drift=True,
        )
    with pytest.raises(claude_core.ClaudeCoreError, match=expected_error):
        claude_core.migrate_legacy_cohort_intent(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            cohort_id=cohort_id,
            plan_sha256=plan_sha256,
            reason="This must not tombstone through a stale loaded image.",
        )
    fence_path = (
        state_root
        / "example"
        / "roots"
        / root_session_id
        / "source_drift_fence.json"
    )
    assert not fence_path.exists()
    assert not (state_dir / "receipt.json").exists()


def test_root_manifest_rejects_host_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    session_id = "12345678-1234-4123-8123-123456789abc"
    receipt = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )
    assert receipt["host_source_sha256"] == claude_core._host_source_sha256()
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: "f" * 64)
    with pytest.raises(claude_core.ClaudeCoreError, match="host source"):
        claude_core.validate_root_manifest(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=session_id,
        )


def test_source_drift_allows_only_explicit_fresh_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    second_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    source_a = "a" * 64
    source_b = "b" * 64
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    claude_core._write_once(
        claude_core._council_pointer_path("example", first_root),
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": first_root,
            "council_round": 1,
            "council_id": "council_" + "f" * 32,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "operational_blocked",
            "final_plan_sha256": None,
            "acceptance_sha256": None,
            "checkpoint_sha256": None,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)

    active = claude_core.get_active_root(
        problem_id="example", statement_sha256=digest
    )
    assert active["root_session_id"] == first_root
    assert active["binding_complete"] is False
    with pytest.raises(claude_core.ClaudeCoreError, match="host source"):
        claude_core.validate_root_manifest(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=first_root,
        )

    manifest_path = (
        tmp_path
        / "state"
        / "example"
        / "roots"
        / first_root
        / "manifest.json"
    )
    predecessor_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    claude_core._replace_canonical(
        manifest_path, {**predecessor_manifest, "host_source_sha256": "c" * 64}
    )
    with pytest.raises(
        claude_core.ClaudeCoreError, match="authority/manifest binding"
    ):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=second_root,
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
            takeover_from=first_root,
        )
    claude_core._replace_canonical(manifest_path, predecessor_manifest)
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=first_root,
    )
    assert successor["host_source_sha256"] == source_b
    assert successor["previous_root_session_id"] == first_root


@pytest.mark.parametrize(
    "pointer_state",
    ["active", "blind_complete", "revision_complete", "accepted", "checkpointed"],
)
def test_source_drift_migration_terminates_every_unconsumed_council_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_state: str,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    old_root = "12345678-1234-4123-8123-123456789abc"
    new_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    source_a = "a" * 64
    source_b = "b" * 64
    source_c = "c" * 64
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "d" * 32
    council_dir = claude_core._council_dir("example", council_id)
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=old_root,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    if pointer_state in {"accepted", "checkpointed"}:
        claude_core._write_once(
            council_dir / "final_plan.json", plan_set, mode=0o400
        )
    final_plan_sha256 = (
        plan_sha256 if pointer_state in {"accepted", "checkpointed"} else None
    )
    acceptance_sha256 = (
        "4" * 64 if pointer_state in {"accepted", "checkpointed"} else None
    )
    checkpoint_sha256 = "5" * 64 if pointer_state == "checkpointed" else None
    pointer_path = claude_core._council_pointer_path("example", old_root)
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": old_root,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": pointer_state,
            "final_plan_sha256": final_plan_sha256,
            "acceptance_sha256": acceptance_sha256,
            "checkpoint_sha256": checkpoint_sha256,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    crash_aliases: list[Path] = []
    if pointer_state == "checkpointed":
        council_alias = council_dir / (
            ".final_plan.json.write-once-123-" + "a" * 24
        )
        os.link(council_dir / "final_plan.json", council_alias)
        crash_aliases.append(council_alias)
        council_orphan = council_dir / (
            ".unpublished.json.write-once-124-" + "b" * 24
        )
        council_orphan.write_bytes(b"unpublished council temporary\n")
        crash_aliases.append(council_orphan)

        initial_cohort_id = "cohort_" + claude_core._cohort_identity_sha256(
            plan_set,
            council_id=council_id,
            acceptance_sha256=str(acceptance_sha256),
        )[:32]
        cohort_dir = state_root / "example" / initial_cohort_id
        cohort_dir.mkdir(parents=True, mode=0o700)
        cohort_plan = cohort_dir / f"plan_{plan_sha256}.json"
        claude_core._write_once(cohort_plan, plan_set, mode=0o400)
        claude_core._write_once(
            cohort_dir / "intent.json",
            {
                "schema_version": claude_core.COHORT_INTENT_SCHEMA,
                "state": "submitted",
                "cohort_id": initial_cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": old_root,
                "timeout_seconds": 60,
                "runner_path": str(claude_core.RUNNER),
                "runner_sha256": "6" * 64,
                "runner_closure_sha256": "7" * 64,
                "codex_bin": str(Path(sys.executable).resolve()),
                "codex_bin_sha256": "8" * 64,
                "host_source_sha256": source_a,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
                "created_at_unix": time.time(),
            },
            mode=0o600,
        )
        cohort_alias = cohort_dir / (
            f".{cohort_plan.name}.write-once-125-" + "c" * 24
        )
        os.link(cohort_plan, cohort_alias)
        crash_aliases.append(cohort_alias)
        cohort_orphan = cohort_dir / (
            ".unpublished.json.write-once-126-" + "d" * 24
        )
        cohort_orphan.write_bytes(b"unpublished cohort temporary\n")
        crash_aliases.append(cohort_orphan)
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)

    if pointer_state == "active":
        real_replace = claude_core._replace_canonical
        crashed = False

        def crash_before_pointer_replace(path: Path, value: object) -> str:
            nonlocal crashed
            if path == pointer_path and not crashed:
                crashed = True
                raise RuntimeError("synthetic crash before pointer replace")
            return real_replace(path, value)  # type: ignore[arg-type]

        monkeypatch.setattr(
            claude_core, "_replace_canonical", crash_before_pointer_replace
        )
        with pytest.raises(RuntimeError, match="before pointer replace"):
            claude_core.migrate_stale_route_council(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=old_root,
                reason="Retire the council after an authenticated host upgrade.",
                confirm_source_drift=True,
            )
        assert (council_dir / "source_drift_termination.json").exists()
        assert json.loads(pointer_path.read_text())["state"] == "active"
        monkeypatch.setattr(claude_core, "_replace_canonical", real_replace)
        monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_c)

    migration = claude_core.migrate_stale_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        reason="Retire the council after an authenticated host upgrade.",
        confirm_source_drift=True,
    )
    assert migration["status"] == "source_drift_blocked"
    assert migration["pointer_before_state"] == pointer_state
    assert not any(path.exists() for path in crash_aliases)
    if pointer_state == "active":
        assert migration["replacement_host_source_sha256"] == source_b
    migrated_pointer = claude_core._read_council_pointer(
        pointer_path,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        require_current_source=False,
        expected_host_source_sha256=source_a,
    )
    assert migrated_pointer["state"] == "source_drift_blocked"
    if pointer_state == "blind_complete":
        termination_path = council_dir / "source_drift_termination.json"
        legacy_termination = json.loads(termination_path.read_text())
        legacy_termination["schema_version"] = (
            claude_core.COUNCIL_SOURCE_DRIFT_TERMINATION_SCHEMA_PREVIOUS
        )
        for field in (
            "initial_cohort_id",
            "recovery_chain",
            "recovery_chain_sha256",
            "terminal_cohort_state",
        ):
            legacy_termination.pop(field)
        claude_core._replace_canonical(termination_path, legacy_termination)
    failure_sha256 = claude_core._council_failure_receipt_sha256(
        migrated_pointer
    )
    claude_core._write_once(
        council_dir / "terminal_pointer.json", migrated_pointer, mode=0o400
    )
    assert (
        claude_core._council_failure_receipt_sha256(migrated_pointer)
        == failure_sha256
    )
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=new_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=old_root,
    )
    assert successor["previous_root_session_id"] == old_root


@pytest.mark.parametrize(
    ("drift_kind", "expected_drift_kinds"),
    [
        ("launcher", ["root_launcher"]),
        ("launcher_checkpointed_preintent", ["root_launcher"]),
        ("python", ["python_runtime"]),
        (
            "previous_v5",
            ["manifest_schema", "python_runtime", "root_launcher"],
        ),
    ],
)
def test_execution_epoch_drift_migration_fences_and_replays_first_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
    expected_drift_kinds: list[str],
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    old_root = "12345678-1234-4123-8123-123456789abc"
    new_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    source_sha256 = "a" * 64
    monkeypatch.setattr(
        claude_core, "_host_source_sha256", lambda: source_sha256
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "d" * 32
    council_dir = claude_core._council_dir("example", council_id)
    checkpointed_preintent = drift_kind == "launcher_checkpointed_preintent"
    if checkpointed_preintent:
        final_plan = claude_core.validate_plan_set(
            problem_id="example",
            statement_sha256=digest,
            plans=_plans(),
            root_session_id=old_root,
        )
        claude_core._write_once(
            council_dir / "final_plan.json", final_plan, mode=0o400
        )
        final_plan_sha256 = claude_core._plan_set_sha256(final_plan)
    else:
        final_plan_sha256 = None
    pointer_path = claude_core._council_pointer_path("example", old_root)
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": old_root,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_sha256,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "checkpointed" if checkpointed_preintent else "active",
            "final_plan_sha256": final_plan_sha256,
            "acceptance_sha256": "4" * 64 if checkpointed_preintent else None,
            "checkpoint_sha256": "5" * 64 if checkpointed_preintent else None,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    if drift_kind in {"launcher", "launcher_checkpointed_preintent"}:
        monkeypatch.setenv(
            "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", "6" * 64
        )
        first_replacement_python = "3" * 64
        first_replacement_launcher = "6" * 64
    elif drift_kind == "python":
        monkeypatch.setattr(
            claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", "5" * 64
        )
        first_replacement_python = "5" * 64
        first_replacement_launcher = "4" * 64
    else:
        problem_dir = state_root / "example"
        manifest_path = problem_dir / "roots" / old_root / "manifest.json"
        authority_path = problem_dir / "active_root.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        authority = json.loads(authority_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = claude_core.ROOT_MANIFEST_SCHEMA_PREVIOUS
        authority["schema_version"] = claude_core.ROOT_AUTHORITY_SCHEMA_PREVIOUS
        for field in ("python_runtime_sha256", "root_launcher_sha256"):
            manifest.pop(field)
            authority.pop(field)
        claude_core._replace_canonical(manifest_path, manifest)
        claude_core._replace_canonical(authority_path, authority)
        first_replacement_python = "3" * 64
        first_replacement_launcher = "4" * 64

    reason = "Retire the root after its authenticated execution epoch changed."
    migration = claude_core.migrate_stale_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        reason=reason,
        confirm_source_drift=True,
    )
    assert migration["schema_version"] == (
        claude_core.COUNCIL_EXECUTION_DRIFT_TERMINATION_SCHEMA
    )
    assert migration["drift_kinds"] == expected_drift_kinds
    assert migration["old_host_source_sha256"] == source_sha256
    assert migration["replacement_host_source_sha256"] == source_sha256
    assert (
        migration["replacement_python_runtime_sha256"]
        == first_replacement_python
    )
    assert (
        migration["replacement_root_launcher_sha256"]
        == first_replacement_launcher
    )
    fence_path = (
        state_root
        / "example"
        / "roots"
        / old_root
        / "source_drift_fence.json"
    )
    fence = json.loads(fence_path.read_text(encoding="utf-8"))
    assert fence["schema_version"] == claude_core.ROOT_EXECUTION_DRIFT_FENCE_SCHEMA
    assert all(
        fence[field] == migration[field]
        for field in claude_core.ROOT_EXECUTION_DRIFT_FIELDS
    )

    # A later deployment cannot redefine the immutable first replacement.
    monkeypatch.setattr(
        claude_core, "_RETHLAS_PINNED_PYTHON_SHA256", "7" * 64
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", "8" * 64)
    replay = claude_core.migrate_stale_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        reason=reason,
        confirm_source_drift=True,
    )
    assert replay["termination_sha256"] == migration["termination_sha256"]
    assert (
        replay["replacement_python_runtime_sha256"]
        == first_replacement_python
    )
    assert (
        replay["replacement_root_launcher_sha256"]
        == first_replacement_launcher
    )
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=new_root,
        canonical_model="claude-opus-5",
        python_runtime_sha256="7" * 64,
        root_launcher_sha256="8" * 64,
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=old_root,
    )
    assert successor["previous_root_session_id"] == old_root


def test_source_drift_snapshot_fences_late_phase_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    source_a = "a" * 64
    source_b = "b" * 64
    source_hash = [source_a]
    monkeypatch.setattr(
        claude_core, "_host_source_sha256", lambda: source_hash[0]
    )
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: source_a
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "7" * 32
    pointer_path = claude_core._council_pointer_path(
        "example", root_session_id
    )
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": root_session_id,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "active",
            "final_plan_sha256": None,
            "acceptance_sha256": None,
            "checkpoint_sha256": None,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    monkeypatch.setattr(
        claude_core,
        "_invoke_sol_council",
        lambda **_kwargs: {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": council_id,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "event_stream_sha256": "4" * 64,
            "retry_allowed": False,
        },
    )
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    settlement_ready = threading.Event()
    release_settlement = threading.Event()
    real_persist = claude_core._persist_council_phase_settlement

    def pause_before_settlement(**arguments: object) -> None:
        settlement_ready.set()
        assert release_settlement.wait(timeout=10)
        real_persist(**arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(
        claude_core,
        "_persist_council_phase_settlement",
        pause_before_settlement,
    )
    phase_arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    reason = "Fence a phase settlement paused after durable execution."
    with ThreadPoolExecutor(max_workers=1) as executor:
        phase_call = executor.submit(
            claude_core._run_council_phase, **phase_arguments
        )
        assert settlement_ready.wait(timeout=10)
        council_dir = claude_core._council_dir("example", council_id)
        assert (council_dir / "blind_execution.json").is_file()
        assert not (council_dir / "blind_settlement.json").exists()
        source_hash[0] = source_b
        # The migration is issued by a separately authenticated deployment B;
        # the paused phase call above continues to model deployment A.
        monkeypatch.setattr(
            claude_core,
            "_require_loaded_host_source_current",
            lambda: source_b,
        )
        migration = claude_core.migrate_stale_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            reason=reason,
            confirm_source_drift=True,
        )
        assert migration["status"] == "source_drift_blocked"
        release_settlement.set()
        with pytest.raises(
            claude_core.ClaudeCoreError, match="terminally source-fenced"
        ):
            phase_call.result(timeout=10)

    assert not (council_dir / "blind_settlement.json").exists()
    assert not (council_dir / "blind_receipt.json").exists()
    assert not (council_dir / "blind_rejected_report.json").exists()
    replay = claude_core.migrate_stale_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        reason=reason,
        confirm_source_drift=True,
    )
    assert replay["termination_sha256"] == migration["termination_sha256"]


def test_source_drift_fence_linearizes_before_phase_paid_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    source_a = "a" * 64
    source_b = "b" * 64
    source_hash = [source_a]
    monkeypatch.setattr(
        claude_core, "_host_source_sha256", lambda: source_hash[0]
    )
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: source_a
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "6" * 32
    pointer_path = claude_core._council_pointer_path(
        "example", root_session_id
    )
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": root_session_id,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "active",
            "final_plan_sha256": None,
            "acceptance_sha256": None,
            "checkpoint_sha256": None,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    launch_window_entered = threading.Event()
    release_launch_window = threading.Event()
    paid_launches: list[str] = []

    def paused_invocation(**arguments: object) -> dict[str, object]:
        launch_window_entered.set()
        assert release_launch_window.wait(timeout=10)
        launch_guard = arguments.get("launch_guard")
        if launch_guard is None:
            paid_launches.append("legacy-unserialized-launch")
        else:
            with launch_guard:  # type: ignore[attr-defined]
                paid_launches.append("serialized-launch")
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": council_id,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "event_stream_sha256": "4" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", paused_invocation)
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    reason = "Fence a phase paused immediately before its paid launch."
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            phase_call = executor.submit(
                claude_core._run_council_phase,
                phase="blind",
                council_id=council_id,
                problem_id="example",
                statement_sha256=digest,
                root_session_id=root_session_id,
                request=request,
                report_plan_set=None,
                report_plan_sha256=None,
                codex_bin=Path(sys.executable),
                timeout_seconds=60,
            )
            assert launch_window_entered.wait(timeout=10)
            live_status = claude_core.route_council_status(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=root_session_id,
            )
            blind_status = live_status["council"]["phases"][0]
            assert blind_status["status"] == "running"
            assert blind_status["metrics"]["request_bytes"] > 0
            assert blind_status["metrics"]["stream_content_exposed"] is False
            source_hash[0] = source_b
            # Model the migration caller as a separately loaded deployment B
            # while the paid phase worker remains deployment A.
            monkeypatch.setattr(
                claude_core,
                "_require_loaded_host_source_current",
                lambda: source_b,
            )
            with pytest.raises(claude_core.ClaudeCoreError, match="active"):
                claude_core.migrate_stale_route_council(
                    problem_id="example",
                    statement_sha256=digest,
                    root_session_id=root_session_id,
                    reason=reason,
                    confirm_source_drift=True,
                )
            fence_path = (
                state_root
                / "example"
                / "roots"
                / root_session_id
                / "source_drift_fence.json"
            )
            assert fence_path.is_file()
            source_hash[0] = source_a
            release_launch_window.set()
            with pytest.raises(
                claude_core.ClaudeCoreError, match="terminally source-fenced"
            ):
                phase_call.result(timeout=10)
    finally:
        release_launch_window.set()
    assert paid_launches == []
    migration = claude_core.migrate_stale_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        reason=reason,
        confirm_source_drift=True,
    )
    assert migration["status"] == "source_drift_blocked"


@pytest.mark.parametrize(
    "scenario",
    [
        "checkpointed_prelaunch",
        "consumed_prelaunch",
        "consumed_failed_receipt",
        "consumed_dead_worker",
    ],
)
def test_source_drift_migration_fences_every_cohort_admission_crash_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    old_root = "12345678-1234-4123-8123-123456789abc"
    new_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    source_a = "a" * 64
    source_b = "b" * 64
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "e" * 32
    council_dir = claude_core._council_dir("example", council_id)
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=old_root,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    acceptance_sha256 = "4" * 64
    claude_core._write_once(
        council_dir / "final_plan.json", plan_set, mode=0o400
    )
    cohort_id = "cohort_" + claude_core._cohort_identity_sha256(
        plan_set,
        council_id=council_id,
        acceptance_sha256=acceptance_sha256,
    )[:32]
    cohort_dir = state_root / "example" / cohort_id
    cohort_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        cohort_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    runner = claude_core.RUNNER.resolve(strict=True)
    codex = Path(sys.executable).resolve(strict=True)
    claude_core._write_once(
        cohort_dir / "intent.json",
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA,
            "state": "submitted",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": old_root,
            "timeout_seconds": 60,
            "runner_path": str(runner),
            "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
            "runner_closure_sha256": "6" * 64,
            "codex_bin": str(codex),
            "codex_bin_sha256": hashlib.sha256(codex.read_bytes()).hexdigest(),
            "host_source_sha256": source_a,
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            "created_at_unix": time.time(),
        },
        mode=0o600,
    )
    pointer_state = (
        "checkpointed" if scenario == "checkpointed_prelaunch" else "consumed"
    )
    pointer_path = claude_core._council_pointer_path("example", old_root)
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": old_root,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": pointer_state,
            "final_plan_sha256": plan_sha256,
            "acceptance_sha256": acceptance_sha256,
            "checkpoint_sha256": "5" * 64,
            "cohort_id": cohort_id if pointer_state == "consumed" else None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    if scenario == "consumed_failed_receipt":
        log_path = cohort_dir / "executor.log"
        log_path.write_bytes(b"")
        claude_core._write_once(
            cohort_dir / "receipt.json",
            {
                "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
                "status": "failed",
                "cohort_id": cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": old_root,
                "returncode": 70,
                "timed_out": False,
                "elapsed_seconds": 1.0,
                "frontier_before_sha256": "7" * 64,
                "frontier_after_sha256": "7" * 64,
                "frontier_changed": False,
                "log_path": str(log_path),
                "log_bytes": 0,
                "log_sha256": hashlib.sha256(b"").hexdigest(),
                "log_over_cap": False,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
                "retry_allowed": False,
                "completion_evidence": None,
            },
            mode=0o400,
        )
    elif scenario == "consumed_dead_worker":
        claude_core._write_once(
            cohort_dir / "worker.json",
            {
                "schema_version": claude_core.COHORT_WORKER_SCHEMA,
                "cohort_id": cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": old_root,
                "worker_pid": 99_999_999,
                "started_at_unix": time.time(),
                "frontier_before_sha256": "7" * 64,
                "worker_start_token": "8" * 64,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            },
            mode=0o400,
        )
        monkeypatch.setattr(
            claude_core,
            "_frontier",
            lambda _problem_id: {"frontier_sha256": "7" * 64},
        )

    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)
    migration = claude_core.migrate_stale_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        reason="Fence the admitted cohort after host source drift.",
        confirm_source_drift=True,
    )
    assert migration["terminal_cohort_id"] == cohort_id
    if scenario.endswith("prelaunch"):
        assert migration["cohort_cancellation_sha256"] == hashlib.sha256(
            (cohort_dir / "cancellation.json").read_bytes()
        ).hexdigest()
        assert migration["cohort_receipt_sha256"] is None
    else:
        assert migration["cohort_cancellation_sha256"] is None
        assert migration["cohort_receipt_status"] == "failed"
        assert migration["cohort_receipt_sha256"] == hashlib.sha256(
            (cohort_dir / "receipt.json").read_bytes()
        ).hexdigest()
    if scenario == "consumed_failed_receipt":
        termination_path = council_dir / "source_drift_termination.json"
        legacy_termination = json.loads(termination_path.read_text())
        legacy_termination["schema_version"] = (
            claude_core.COUNCIL_SOURCE_DRIFT_TERMINATION_SCHEMA_PREVIOUS
        )
        for field in (
            "initial_cohort_id",
            "recovery_chain",
            "recovery_chain_sha256",
            "terminal_cohort_state",
        ):
            legacy_termination.pop(field)
        claude_core._replace_canonical(termination_path, legacy_termination)
    claude_core._assert_no_unsettled_cohort_execution(
        problem_id="example", statement_sha256=digest
    )
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=new_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=old_root,
    )
    assert successor["previous_root_session_id"] == old_root


@pytest.mark.parametrize(
    "receipt_status", ["failed", "no_progress", "timeout", "output_limit"]
)
@pytest.mark.parametrize("has_executor", [False, True])
@pytest.mark.parametrize("historical_generation", ["v2", "v1"])
def test_source_drift_seals_pre_v3_intent_with_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_status: str,
    has_executor: bool,
    historical_generation: str,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    statement_sha256 = _statement_digest()
    old_root = "12345678-1234-4123-8123-123456789abc"
    source_a = "a" * 64
    source_b = "b" * 64
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=old_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "d" * 32
    council_dir = claude_core._council_dir("example", council_id)
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=statement_sha256,
        plans=_plans(),
        root_session_id=old_root,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    acceptance_sha256 = "4" * 64
    claude_core._write_once(
        council_dir / "final_plan.json", plan_set, mode=0o400
    )
    cohort_id = "cohort_" + claude_core._cohort_identity_sha256(
        plan_set,
        council_id=council_id,
        acceptance_sha256=acceptance_sha256,
    )[:32]
    cohort_dir = state_root / "example" / cohort_id
    cohort_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        cohort_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    runner = claude_core.RUNNER.resolve(strict=True)
    codex = Path(sys.executable).resolve(strict=True)
    intent = {
        "schema_version": (
            claude_core.COHORT_INTENT_SCHEMA_PREVIOUS
            if historical_generation == "v2"
            else claude_core.COHORT_INTENT_SCHEMA_LEGACY
        ),
        "state": "submitted",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "plan_sha256": plan_sha256,
        "root_session_id": old_root,
        "created_at_unix": time.time(),
    }
    if historical_generation == "v2":
        intent.update(
            {
                "timeout_seconds": 60,
                "runner_path": str(runner),
                "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
                "codex_bin": str(codex),
                "codex_bin_sha256": hashlib.sha256(
                    codex.read_bytes()
                ).hexdigest(),
                "host_source_sha256": source_a,
            }
        )
    claude_core._write_once(
        cohort_dir / "intent.json", intent, mode=0o400
    )
    claude_core._write_once(
        claude_core._council_pointer_path("example", old_root),
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": statement_sha256,
            "root_session_id": old_root,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "consumed",
            "final_plan_sha256": plan_sha256,
            "acceptance_sha256": acceptance_sha256,
            "checkpoint_sha256": "5" * 64,
            "cohort_id": cohort_id,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    log_path = cohort_dir / "executor.log"
    log_path.write_bytes(b"x")
    if receipt_status == "output_limit":
        monkeypatch.setattr(claude_core, "MAX_REPORT_LOG_BYTES", 0)
    receipt = {
        "schema_version": (
            claude_core.COHORT_RECEIPT_SCHEMA_PREVIOUS
            if historical_generation == "v2"
            else claude_core.COHORT_RECEIPT_SCHEMA_LEGACY
        ),
        "status": receipt_status,
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "plan_sha256": plan_sha256,
        "root_session_id": old_root,
        "returncode": 0 if receipt_status == "no_progress" else 70,
        "timed_out": receipt_status == "timeout",
        "elapsed_seconds": 1.0,
        "frontier_before_sha256": "7" * 64,
        "frontier_after_sha256": "7" * 64,
        "frontier_changed": False,
        "log_path": str(log_path),
        "log_bytes": 1,
        "log_sha256": hashlib.sha256(b"x").hexdigest(),
        "log_over_cap": receipt_status == "output_limit",
        "retry_allowed": False,
    }
    if historical_generation == "v2":
        receipt["completion_evidence"] = None
    claude_core._write_once(cohort_dir / "receipt.json", receipt, mode=0o400)
    if receipt_status == "output_limit":
        monkeypatch.setattr(claude_core, "MAX_REPORT_LOG_BYTES", 1024)
    elif receipt_status == "failed":
        monkeypatch.setattr(claude_core, "MAX_REPORT_LOG_BYTES", 0)
    if has_executor:
        claude_core._write_once(
            cohort_dir / "executor.json",
            {"schema_version": "historical_executor_marker_v0"},
            mode=0o400,
        )

    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)
    migration = claude_core.migrate_stale_route_council(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=old_root,
        reason="Seal a terminal pre-v3 cohort after source drift.",
        confirm_source_drift=True,
    )
    assert migration["terminal_cohort_state"] == "settled_failed"
    assert migration["cohort_receipt_status"] == receipt_status
    artifact_names = {
        artifact["name"] for artifact in migration["cohort_artifacts"]
    }
    assert ("executor.json" in artifact_names) is has_executor
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=old_root,
    )
    assert successor["previous_root_session_id"] == old_root


@pytest.mark.parametrize(
    "terminal_state",
    [
        "authorized_unadmitted",
        "prelaunch",
        "dead_worker",
        "active_worker",
        "active_completed_unverified",
        "second_recovery",
    ],
)
def test_source_drift_migration_follows_the_terminal_recovery_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: str,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    source_a = "a" * 64
    source_b = "b" * 64
    frontier = "7" * 64
    frontier_state = [frontier]
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier_state[0]},
    )
    monkeypatch.setattr(
        claude_core, "_terminate_owned_command_wrappers", lambda *_args: None
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "c" * 32
    council_dir = claude_core._council_dir("example", council_id)
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    acceptance_sha256 = "4" * 64
    claude_core._write_once(
        council_dir / "final_plan.json", plan_set, mode=0o400
    )
    initial_cohort_id = "cohort_" + claude_core._cohort_identity_sha256(
        plan_set,
        council_id=council_id,
        acceptance_sha256=acceptance_sha256,
    )[:32]
    pointer_path = claude_core._council_pointer_path(
        "example", root_session_id
    )
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": root_session_id,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "consumed",
            "final_plan_sha256": plan_sha256,
            "acceptance_sha256": acceptance_sha256,
            "checkpoint_sha256": "5" * 64,
            "cohort_id": initial_cohort_id,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    runner = claude_core.RUNNER.resolve(strict=True)
    codex = Path(sys.executable).resolve(strict=True)

    def write_intent(cohort_id: str) -> Path:
        state_dir = state_root / "example" / cohort_id
        state_dir.mkdir(parents=True, mode=0o700)
        claude_core._write_once(
            state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
        )
        claude_core._write_once(
            state_dir / "intent.json",
            {
                "schema_version": claude_core.COHORT_INTENT_SCHEMA,
                "state": "submitted",
                "cohort_id": cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": root_session_id,
                "timeout_seconds": 60,
                "runner_path": str(runner),
                "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
                "runner_closure_sha256": "6" * 64,
                "codex_bin": str(codex),
                "codex_bin_sha256": hashlib.sha256(codex.read_bytes()).hexdigest(),
                "host_source_sha256": source_a,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
                "created_at_unix": time.time(),
            },
            mode=0o600,
        )
        return state_dir

    def write_failed_receipt(state_dir: Path, cohort_id: str) -> None:
        log_path = state_dir / "executor.log"
        log_path.write_bytes(b"")
        claude_core._write_once(
            state_dir / "receipt.json",
            {
                "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
                "status": "failed",
                "cohort_id": cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": root_session_id,
                "returncode": 70,
                "timed_out": False,
                "elapsed_seconds": 1.0,
                "frontier_before_sha256": frontier,
                "frontier_after_sha256": frontier,
                "frontier_changed": False,
                "log_path": str(log_path),
                "log_bytes": 0,
                "log_sha256": hashlib.sha256(b"").hexdigest(),
                "log_over_cap": False,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
                "retry_allowed": False,
                "completion_evidence": None,
            },
            mode=0o400,
        )

    def write_completed_unverified_receipt(
        state_dir: Path, cohort_id: str
    ) -> None:
        log_path = state_dir / "executor.log"
        log_path.write_bytes(b"exact three-route terminal round\n")
        log_raw = log_path.read_bytes()
        claude_core._write_once(
            state_dir / "receipt.json",
            {
                "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
                "status": "completed_unverified",
                "cohort_id": cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": root_session_id,
                "returncode": 1,
                "timed_out": False,
                "elapsed_seconds": 1.0,
                "frontier_before_sha256": frontier,
                "frontier_after_sha256": "9" * 64,
                "frontier_changed": True,
                "log_path": str(log_path),
                "log_bytes": len(log_raw),
                "log_sha256": hashlib.sha256(log_raw).hexdigest(),
                "log_over_cap": False,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
                "retry_allowed": False,
                "completion_evidence": _completion_evidence(),
            },
            mode=0o400,
        )

    initial_state_dir = write_intent(initial_cohort_id)
    write_failed_receipt(initial_state_dir, initial_cohort_id)
    first_authorization = claude_core.authorize_failed_cohort_recovery(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        plan_sha256=plan_sha256,
        codex_bin=codex,
        source_cohort_id=initial_cohort_id,
    )
    terminal_cohort_id = str(first_authorization["recovery_cohort_id"])
    terminal_state_dir = state_root / "example" / terminal_cohort_id
    if terminal_state != "authorized_unadmitted":
        terminal_state_dir = write_intent(terminal_cohort_id)
    expected_chain_length = 1
    active_lock = None
    if terminal_state == "dead_worker":
        claude_core._write_once(
            terminal_state_dir / "worker.json",
            {
                "schema_version": claude_core.COHORT_WORKER_SCHEMA,
                "cohort_id": terminal_cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": root_session_id,
                "worker_pid": 99_999_999,
                "started_at_unix": time.time(),
                "frontier_before_sha256": frontier,
                "worker_start_token": "8" * 64,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            },
            mode=0o400,
        )
    elif terminal_state in {
        "active_worker",
        "active_completed_unverified",
    }:
        claude_core._write_once(
            terminal_state_dir / "worker.json",
            {
                "schema_version": claude_core.COHORT_WORKER_SCHEMA,
                "cohort_id": terminal_cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": root_session_id,
                "worker_pid": os.getpid(),
                "started_at_unix": time.time(),
                "frontier_before_sha256": frontier,
                "worker_start_token": "8" * 64,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            },
            mode=0o400,
        )
        active_lock = open(terminal_state_dir / "cohort.lock", "a+b")
        fcntl.flock(active_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif terminal_state == "second_recovery":
        write_failed_receipt(terminal_state_dir, terminal_cohort_id)
        second_authorization = claude_core.authorize_failed_cohort_recovery(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            plan_sha256=plan_sha256,
            codex_bin=codex,
            source_cohort_id=terminal_cohort_id,
        )
        terminal_cohort_id = str(second_authorization["recovery_cohort_id"])
        terminal_state_dir = write_intent(terminal_cohort_id)
        expected_chain_length = 2

    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)
    try:
        if terminal_state in {
            "active_worker",
            "active_completed_unverified",
        }:
            with pytest.raises(
                claude_core.ClaudeCoreError,
                match="worker has not safely settled|active",
            ):
                claude_core.migrate_stale_route_council(
                    problem_id="example",
                    statement_sha256=digest,
                    root_session_id=root_session_id,
                    reason="Fence the exact terminal recovery after source drift.",
                    confirm_source_drift=True,
                )
            assert json.loads(pointer_path.read_text())["state"] == "consumed"
            assert not (council_dir / "source_drift_termination.json").exists()
            assert (
                state_root
                / "example"
                / "roots"
                / root_session_id
                / "source_drift_fence.json"
            ).is_file()
            assert active_lock is not None
            fcntl.flock(active_lock, fcntl.LOCK_UN)
            active_lock.close()
            active_lock = None
            monkeypatch.setattr(
                claude_core, "_host_source_sha256", lambda: source_a
            )
            if terminal_state == "active_worker":
                settled = claude_core._settle_stopped_cohort_worker(
                    state_dir=terminal_state_dir,
                    cohort_id=terminal_cohort_id,
                    problem_id="example",
                    statement_sha256=digest,
                    plan_sha256=plan_sha256,
                    root_session_id=root_session_id,
                )
                assert settled is not None
                assert settled["status"] == "failed"
            else:
                write_completed_unverified_receipt(
                    terminal_state_dir, terminal_cohort_id
                )
                # A completed cohort legitimately opens canonical memory for
                # later append batches.  Migration must validate the receipt's
                # immutable record IDs rather than require the live frontier
                # to remain frozen at frontier_after_sha256.
                frontier_state[0] = "a" * 64
                historical_evidence_calls: list[dict[str, object]] = []

                def historical_completion_evidence(
                    **arguments: object,
                ) -> dict[str, object]:
                    historical_evidence_calls.append(arguments)
                    assert arguments["expected_evidence"] == (
                        _completion_evidence()
                    )
                    assert frontier_state[0] != "9" * 64
                    return _completion_evidence()

                monkeypatch.setattr(
                    claude_core,
                    "_completed_unverified_cohort_evidence",
                    historical_completion_evidence,
                )
            migration = claude_core.migrate_stale_route_council(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=root_session_id,
                reason="Fence the exact terminal recovery after source drift.",
                confirm_source_drift=True,
            )
        else:
            migration = claude_core.migrate_stale_route_council(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=root_session_id,
                reason="Fence the exact terminal recovery after source drift.",
                confirm_source_drift=True,
            )
    finally:
        if active_lock is not None:
            fcntl.flock(active_lock, fcntl.LOCK_UN)
            active_lock.close()

    assert migration["initial_cohort_id"] == initial_cohort_id
    assert migration["terminal_cohort_id"] == terminal_cohort_id
    assert len(migration["recovery_chain"]) == expected_chain_length
    assert migration["recovery_chain"][0]["source_cohort_id"] == (
        initial_cohort_id
    )
    assert migration["recovery_chain"][-1]["recovery_cohort_id"] == (
        terminal_cohort_id
    )
    assert migration["recovery_chain_sha256"] == hashlib.sha256(
        (
            json.dumps(
                migration["recovery_chain"],
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    ).hexdigest()
    if terminal_state in {"prelaunch", "second_recovery"}:
        assert (terminal_state_dir / "cancellation.json").is_file()
    elif terminal_state == "authorized_unadmitted":
        assert migration["terminal_cohort_state"] == "authorized_unadmitted"
        assert migration["cohort_intent_sha256"] is None
        assert not (terminal_state_dir / "intent.json").exists()
    elif terminal_state == "active_completed_unverified":
        assert len(historical_evidence_calls) == 1
        assert migration["terminal_cohort_state"] == (
            "settled_completed_unverified"
        )
        assert migration["cohort_receipt_status"] == "completed_unverified"
    else:
        assert migration["cohort_receipt_status"] == "failed"

    # Emulate an already-loaded old host after migration released the root
    # lock but before a fresh epoch takes over.  The immutable root fence must
    # reject every root-guarded mutation and same-epoch process restart.
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    with pytest.raises(claude_core.ClaudeCoreError, match="terminally fenced"):
        with claude_core.root_authority_guard(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
        ):
            pass
    with pytest.raises(claude_core.ClaudeCoreError, match="terminally fenced"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        )
    if terminal_state == "dead_worker":
        before = claude_core._stable_council_artifact_manifest(
            terminal_state_dir
        )
        with pytest.raises(
            claude_core.ClaudeCoreError, match="terminally fenced"
        ):
            claude_core.authorize_failed_cohort_recovery(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=root_session_id,
                plan_sha256=plan_sha256,
                codex_bin=codex,
                source_cohort_id=terminal_cohort_id,
            )
        assert claude_core._stable_council_artifact_manifest(
            terminal_state_dir
        ) == before
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=root_session_id,
    )
    assert successor["previous_root_session_id"] == root_session_id
    second_successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    assert second_successor["previous_root_session_id"] == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    if terminal_state == "active_completed_unverified":
        claude_core._assert_council_ready_for_verification(
            problem_id="example", statement_sha256=digest
        )
    else:
        with pytest.raises(
            claude_core.ClaudeCoreError,
            match="requires a consumed route council",
        ):
            claude_core._assert_council_ready_for_verification(
                problem_id="example", statement_sha256=digest
            )


def test_council_takeover_lineage_replays_after_crash_before_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    second_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "f" * 32
    predecessor_pointer = {
        "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
        "pointer_version": 1,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": first_root,
        "council_round": 1,
        "council_id": council_id,
        "base_frontier_sha256": "1" * 64,
        "opus_plan_sha256": "2" * 64,
        "prior_context_sha256": "3" * 64,
        "prior_failure_receipt_sha256": None,
        "host_source_sha256": claude_core._host_source_sha256(),
        "predecessor_root_session_id": None,
        "predecessor_council_id": None,
        "predecessor_pointer_sha256": None,
        "state": "operational_blocked",
        "final_plan_sha256": None,
        "acceptance_sha256": None,
        "checkpoint_sha256": None,
        "cohort_id": None,
        "updated_at_unix": time.time(),
    }
    claude_core._write_once(
        claude_core._council_pointer_path("example", first_root),
        predecessor_pointer,
        mode=0o400,
    )
    predecessor_dir = claude_core._council_dir("example", council_id)
    request_path = predecessor_dir / "blind_request.json"
    claude_core._write_once(request_path, {"synthetic": True}, mode=0o400)
    claude_core._write_once(
        predecessor_dir / "blind_receipt.json",
        {
            "phase": "blind",
            "council_id": council_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": first_root,
            "request_sha256": hashlib.sha256(
                request_path.read_bytes()
            ).hexdigest(),
            "status": "operational_blocked",
            "retry_allowed": False,
        },
        mode=0o400,
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=first_root,
    )
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": "4" * 64},
    )
    monkeypatch.setattr(
        claude_core,
        "_run_council_phase",
        lambda **_arguments: {"status": "completed"},
    )
    successor_pointer_path = claude_core._council_pointer_path(
        "example", second_root
    )
    real_write_once = claude_core._write_once
    fail_pointer_once = [True]

    def crash_before_pointer(path: Path, value: object, **kwargs: object) -> str:
        if path == successor_pointer_path and fail_pointer_once[0]:
            fail_pointer_once[0] = False
            raise KeyboardInterrupt("synthetic crash after lineage")
        return real_write_once(path, value, **kwargs)

    monkeypatch.setattr(claude_core, "_write_once", crash_before_pointer)
    arguments = {
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": second_root,
        "opus_plans": _plans(),
        "prior_failure_context": "The predecessor transport failed.",
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    without_failure_context = dict(arguments)
    without_failure_context.pop("prior_failure_context")
    with pytest.raises(
        claude_core.CouncilContractError,
        match="superseding root requires prior council failure context",
    ):
        claude_core.start_route_council(**without_failure_context)
    assert not successor_pointer_path.exists()
    with pytest.raises(KeyboardInterrupt, match="after lineage"):
        claude_core.start_route_council(**arguments)
    lineage_path = (
        state_root
        / "example"
        / "roots"
        / second_root
        / "route_council_takeover.json"
    )
    lineage_bytes = lineage_path.read_bytes()
    assert not successor_pointer_path.exists()

    resumed = claude_core.start_route_council(**arguments)
    assert resumed["status"] == "completed"
    assert lineage_path.read_bytes() == lineage_bytes
    pointer = claude_core._read_council_pointer(
        successor_pointer_path,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
    )
    assert pointer["state"] == "blind_complete"
    with pytest.raises(
        claude_core.CouncilContractError,
        match="final audit is out of sequence",
    ):
        claude_core.finalize_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=second_root,
            council_id=pointer["council_id"],
            final_plans=_plans(),
            adjudications=[],
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )


def test_root_manifest_binds_council_mode_to_opus_and_fences_mode_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    session_id = "12345678-1234-4123-8123-123456789abc"
    receipt = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    assert receipt["orchestration_mode"] == claude_core.OPUS_SOL_COUNCIL_MODE

    with pytest.raises(claude_core.ClaudeCoreError, match="provider binding mismatch"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=session_id,
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.SINGLE_ROOT_MODE,
        )
    fresh_state = tmp_path / "fable-state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", fresh_state)
    with pytest.raises(claude_core.ClaudeCoreError, match="requires a Claude Opus"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            canonical_model="claude-fable-5",
            orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        )


def test_root_takeover_rejects_unconsumed_accepted_council(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    claude_core._write_once(
        claude_core._council_pointer_path("example", first_root),
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": first_root,
            "council_round": 1,
            "council_id": "council_" + "1" * 32,
            "base_frontier_sha256": "2" * 64,
            "opus_plan_sha256": "3" * 64,
            "prior_context_sha256": "4" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": claude_core._host_source_sha256(),
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "accepted",
            "final_plan_sha256": "5" * 64,
            "acceptance_sha256": "6" * 64,
            "checkpoint_sha256": None,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="unconsumed"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
            takeover_from=first_root,
        )


def test_root_takeover_rejects_consumed_council_without_safe_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    cohort_id = "cohort_" + "7" * 32
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    claude_core._write_once(
        claude_core._council_pointer_path("example", first_root),
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": first_root,
            "council_round": 1,
            "council_id": "council_" + "1" * 32,
            "base_frontier_sha256": "2" * 64,
            "opus_plan_sha256": "3" * 64,
            "prior_context_sha256": "4" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": claude_core._host_source_sha256(),
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "consumed",
            "final_plan_sha256": "5" * 64,
            "acceptance_sha256": "6" * 64,
            "checkpoint_sha256": "8" * 64,
            "cohort_id": cohort_id,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    (state_root / "example" / cohort_id).mkdir(parents=True)
    with pytest.raises(claude_core.ClaudeCoreError, match="not safely settled"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
            takeover_from=first_root,
        )
    assert claude_core.get_active_root(
        problem_id="example", statement_sha256=digest
    )["root_session_id"] == first_root
    cohort_state = state_root / "example" / cohort_id
    log_path = cohort_state / "executor.log"
    log_path.write_text("completed without a verified proof\n", encoding="utf-8")
    claude_core._write_once(
        cohort_state / "receipt.json",
        {
            "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
            "status": "completed_unverified",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": "5" * 64,
            "root_session_id": first_root,
            "returncode": 1,
            "timed_out": False,
            "elapsed_seconds": 1.0,
            "frontier_before_sha256": "2" * 64,
            "frontier_after_sha256": "9" * 64,
            "frontier_changed": True,
            "log_path": str(log_path),
            "log_bytes": log_path.stat().st_size,
            "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
            "log_over_cap": False,
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            "retry_allowed": False,
            "completion_evidence": _completion_evidence(),
        },
        mode=0o400,
    )
    successor_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=successor_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=first_root,
    )
    assert claude_core.get_active_root(
        problem_id="example", statement_sha256=digest
    )["root_session_id"] == successor_root


@pytest.mark.parametrize("exit_mode", ["successor_council", "fresh_takeover"])
def test_recovery_exhausted_consumed_council_has_terminal_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_mode: str
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    statement_sha256 = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    initial_cohort_id = "cohort_" + "7" * 32
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    claude_core._write_once(
        claude_core._council_pointer_path("example", first_root),
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": statement_sha256,
            "root_session_id": first_root,
            "council_round": 1,
            "council_id": "council_" + "1" * 32,
            "base_frontier_sha256": "2" * 64,
            "opus_plan_sha256": "3" * 64,
            "prior_context_sha256": "4" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": claude_core._host_source_sha256(),
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "consumed",
            "final_plan_sha256": "5" * 64,
            "acceptance_sha256": "6" * 64,
            "checkpoint_sha256": "8" * 64,
            "cohort_id": initial_cohort_id,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    exhausted_cohorts = {
        "cohort_" + f"{index:032x}"
        for index in range(claude_core.MAX_COHORT_RECOVERY_DEPTH + 1)
    }
    terminal_cohort_id = sorted(exhausted_cohorts)[-1]
    terminal_receipt_path = tmp_path / "terminal-receipt.json"
    terminal_receipt_path.write_text("terminal recovery budget\n", encoding="utf-8")
    monkeypatch.setattr(
        claude_core,
        "_settled_terminal_council_cohort",
        lambda **_arguments: (
            terminal_cohort_id,
            exhausted_cohorts,
            {"status": "failed"},
            terminal_receipt_path,
        ),
    )
    if exit_mode == "successor_council":
        monkeypatch.setattr(
            claude_core,
            "_frontier",
            lambda _problem_id: {"frontier_sha256": "2" * 64},
        )
        monkeypatch.setattr(
            claude_core,
            "_run_council_phase",
            lambda **_arguments: {"status": "completed"},
        )
        successor = claude_core.start_route_council(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=first_root,
            opus_plans=_plans(),
            prior_failure_context=(
                "All eight owner-authorized recovery attempts failed."
            ),
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
        assert successor["status"] == "completed"
        assert successor["council_round"] == 2
        return
    successor_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=successor_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=first_root,
    )
    assert successor["previous_root_session_id"] == first_root


def test_council_lineage_takeover_cannot_downgrade_to_single_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="council lineage"):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.SINGLE_ROOT_MODE,
            takeover_from=first_root,
        )


def test_single_root_takeover_is_a_valid_boundary_for_first_council(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    council_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.SINGLE_ROOT_MODE,
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=council_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=first_root,
    )
    manifest = claude_core.validate_root_manifest(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=council_root,
    )
    assert claude_core._nearest_predecessor_council_pointer(
        problem_id="example",
        statement_sha256=digest,
        manifest=manifest,
    ) is None


def test_root_takeover_fences_predecessor_depth_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    statement_sha256 = _statement_digest()
    active_root = "12345678-1234-4123-8123-123456789abc"
    active_manifest = _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=active_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    for depth in range(1, claude_core.MAX_ROOT_PREDECESSOR_DEPTH + 1):
        next_root = f"{depth:08x}-0000-4000-8000-{depth:012x}"
        active_manifest = _prepare_root(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=next_root,
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
            takeover_from=active_root,
        )
        active_root = next_root
        if depth in {
            claude_core.MAX_ROOT_PREDECESSOR_DEPTH - 1,
            claude_core.MAX_ROOT_PREDECESSOR_DEPTH,
        }:
            assert claude_core._nearest_predecessor_council_pointer(
                problem_id="example",
                statement_sha256=statement_sha256,
                manifest=active_manifest,
            ) is None

    rejected_root = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    rejected_manifest_path = (
        state_root
        / "example"
        / "roots"
        / rejected_root
        / "manifest.json"
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="depth is exhausted"):
        _prepare_root(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=rejected_root,
            canonical_model="claude-opus-5",
            orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
            takeover_from=active_root,
        )
    assert not rejected_manifest_path.exists()
    assert claude_core.get_active_root(
        problem_id="example", statement_sha256=statement_sha256
    )["root_session_id"] == active_root


def test_council_cohort_identity_binds_exact_council_acceptance() -> None:
    digest = _statement_digest()
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id="12345678-1234-4123-8123-123456789abc",
    )
    first = claude_core._cohort_identity_sha256(
        plan_set,
        council_id="council_" + "1" * 32,
        acceptance_sha256="2" * 64,
    )
    second = claude_core._cohort_identity_sha256(
        plan_set,
        council_id="council_" + "3" * 32,
        acceptance_sha256="4" * 64,
    )
    assert first != second


def test_plan_set_rejects_duplicate_mechanisms() -> None:
    digest = _statement_digest()
    duplicate = _plans()
    duplicate[1]["mechanism"] = duplicate[0]["mechanism"]
    with pytest.raises(claude_core.ClaudeCoreError, match="mechanisms"):
        claude_core.validate_plan_set(
            problem_id="example",
            statement_sha256=digest,
            plans=duplicate,
            root_session_id="root-session-1",
        )


def test_statement_retrieval_policy_is_explicit_digest_bound_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)

    def policy(problem_id: str, text: str) -> dict[str, object]:
        source = data_root / f"{problem_id}.md"
        source.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return claude_core.statement_retrieval_policy(
            problem_id=problem_id, statement_sha256=digest
        )

    disabled = policy("default", "# Problem\n\nProve it.\n")
    assert disabled["mode"] == "disabled"
    assert disabled["basis"] == "default_disabled"

    permitted = policy(
        "permitted",
        "# Problem\n\nProve it.\n\n"
        "## Retrieval restriction\n\n"
        "Matlas and arXiv retrieval are permitted. Use no arXiv source "
        "submitted after 2026-06-26.\n",
    )
    assert permitted["mode"] == "matlas_arxiv"
    assert permitted["basis"] == "explicit_matlas_arxiv_permission"

    conflict_source = data_root / "conflict.md"
    conflict_source.write_text(
        "This run is strictly offline.\n\n"
        "## Retrieval restriction\n\n"
        "Matlas and arXiv retrieval are permitted.\n",
        encoding="utf-8",
    )
    conflict_digest = hashlib.sha256(conflict_source.read_bytes()).hexdigest()
    with pytest.raises(claude_core.ClaudeCoreError, match="directives conflict"):
        claude_core.statement_retrieval_policy(
            problem_id="conflict", statement_sha256=conflict_digest
        )

    nontrailing_source = data_root / "nontrailing.md"
    nontrailing_source.write_text(
        "## Retrieval restriction\n\nMatlas and arXiv retrieval are permitted.\n\n"
        "## Statement\n\nProve it.\n",
        encoding="utf-8",
    )
    nontrailing_digest = hashlib.sha256(nontrailing_source.read_bytes()).hexdigest()
    with pytest.raises(claude_core.ClaudeCoreError, match="must be trailing"):
        claude_core.statement_retrieval_policy(
            problem_id="nontrailing", statement_sha256=nontrailing_digest
        )

    with pytest.raises(claude_core.ClaudeCoreError, match="digest mismatch"):
        claude_core.statement_retrieval_policy(
            problem_id="permitted", statement_sha256="0" * 64
        )


def test_plan_projection_is_digest_bound_and_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "generation"
    data_root = input_root / "data"
    data_root.mkdir(parents=True)
    source = data_root / "example.md"
    source.write_text("Example statement.\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", input_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    body = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    raw = (claude_core.canonical_json(body) + "\n").encode()
    plan_path = input_root / "plan.json"
    plan_path.write_bytes(raw)
    receipt = claude_core.validate_plan_file(
        plan_path,
        problem_id="example",
        statement_sha256=digest,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    assert receipt["status"] == "accepted"
    assert receipt["root_session_id"] == root_session_id
    assert receipt["retrieval_mode"] == "disabled"

    plan_path.write_text("{}", encoding="utf-8")
    with pytest.raises(claude_core.ClaudeCoreError, match="digest mismatch"):
        claude_core.validate_plan_file(
            plan_path,
            problem_id="example",
            statement_sha256=digest,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_terminal_publication_cancels_existing_intent_without_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    plans = _plans()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    input_root = tmp_path / "inputs"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    body = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=plans,
        root_session_id=root_session_id,
    )
    raw = (claude_core.canonical_json(body) + "\n").encode()
    plan_sha = hashlib.sha256(raw).hexdigest()
    cohort_id = "cohort_" + plan_sha[:32]
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    intent = {
        "schema_version": "rethlas_claude_cohort_intent_v1",
        "state": "submitted",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "plan_sha256": plan_sha,
        "root_session_id": root_session_id,
        "created_at_unix": 1.0,
    }
    (state_dir / "intent.json").write_text(
        claude_core.canonical_json(intent) + "\n", encoding="utf-8"
    )

    spawned: list[str] = []

    def fake_spawn(**arguments: object) -> None:
        spawned.append(str(arguments["cohort_id"]))
        worker = {
            "schema_version": "rethlas_claude_cohort_worker_v1",
            "cohort_id": arguments["cohort_id"],
            "problem_id": arguments["problem_id"],
            "statement_sha256": arguments["statement_sha256"],
            "plan_sha256": arguments["plan_sha256"],
            "root_session_id": arguments["root_session_id"],
            "worker_pid": os.getpid(),
            "started_at_unix": 1.0,
        }
        claude_core._write_once(
            state_dir / "worker.json", worker, mode=0o400
        )

    monkeypatch.setattr(claude_core, "_spawn_cohort_worker", fake_spawn)
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda _problem_id, _statement_sha256: {"status": "published"},
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="intent was cancelled"):
        claude_core.run_three_route_cohort(
            problem_id="example",
            statement_sha256=digest,
            plans=plans,
            root_session_id=root_session_id,
            timeout_seconds=60,
            wait_seconds=0,
        )

    assert spawned == []
    cancellation = claude_core._read_canonical_object(
        state_dir / "cancellation.json", label="test cohort cancellation"
    )
    assert cancellation["status"] == "cancelled"
    assert cancellation["cohort_id"] == cohort_id


def test_pre_marker_worker_exit_releases_lifeline_for_same_process_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cohort_id = "cohort_" + "1" * 32
    state_dir = tmp_path / cohort_id
    state_dir.mkdir()
    source_descriptor = os.open("/dev/null", os.O_RDONLY)
    launches = 0

    class StoppedProcess:
        def __init__(self) -> None:
            nonlocal launches
            launches += 1
            self.pid = 40_000 + launches

        @staticmethod
        def poll() -> int:
            return 70

    @contextmanager
    def fake_source_command(**_arguments: object):
        yield ["mock-worker"], source_descriptor

    monkeypatch.setattr(
        claude_core, "_pinned_host_source_command", fake_source_command
    )
    monkeypatch.setattr(
        claude_core.subprocess,
        "Popen",
        lambda *_arguments, **_keywords: StoppedProcess(),
    )
    monkeypatch.setattr(
        claude_core, "_process_identity_token", lambda _pid: "2" * 64
    )
    monkeypatch.setattr(claude_core, "_pid_is_live", lambda _pid: False)

    arguments = {
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": "3" * 64,
        "root_session_id": "12345678-1234-4123-8123-123456789abc",
        "plan_sha256": "4" * 64,
        "timeout_seconds": 60,
        "host_source_sha256": "5" * 64,
        "state_dir": state_dir,
    }
    try:
        claude_core._spawn_cohort_worker(**arguments)
        pending = claude_core._wait_for_cohort_receipt(
            receipt_path=state_dir / "receipt.json",
            state_dir=state_dir,
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256="3" * 64,
            plan_sha256="4" * 64,
            root_session_id="12345678-1234-4123-8123-123456789abc",
            wait_seconds=0,
        )
        assert pending["status"] == "execution_unknown"
        assert cohort_id not in claude_core._COHORT_LIFELINE_WRITERS

        claude_core._spawn_cohort_worker(**arguments)
        assert launches == 2
    finally:
        claude_core._release_cohort_lifeline(cohort_id)
        os.close(source_descriptor)


@pytest.mark.parametrize("launch_is_live", [False, True])
def test_owner_migration_distinguishes_stale_and_live_pre_marker_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_is_live: bool,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    cohort_id = "cohort_" + plan_sha256[:32]
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        state_dir / "intent.json",
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA_LEGACY,
            "state": "submitted",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "created_at_unix": time.time(),
        },
        mode=0o600,
    )
    lifeline_read, lifeline_write = os.pipe()
    worker_pid = 44_444
    worker_token = "7" * 64
    claude_core._COHORT_LIFELINE_WRITERS[cohort_id] = (
        lifeline_write,
        worker_pid,
        worker_token,
    )
    monkeypatch.setattr(
        claude_core, "_pid_is_live", lambda pid: launch_is_live and pid == worker_pid
    )
    monkeypatch.setattr(
        claude_core,
        "_process_identity_token",
        lambda pid: worker_token if launch_is_live and pid == worker_pid else None,
    )
    try:
        if launch_is_live:
            with pytest.raises(
                claude_core.ClaudeCoreError, match="execution evidence"
            ):
                claude_core.migrate_legacy_cohort_intent(
                    problem_id="example",
                    statement_sha256=digest,
                    root_session_id=root_session_id,
                    cohort_id=cohort_id,
                    plan_sha256=plan_sha256,
                    reason="Fence a pre-marker worker only after it stops.",
                )
            assert cohort_id in claude_core._COHORT_LIFELINE_WRITERS
        else:
            migrated = claude_core.migrate_legacy_cohort_intent(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=root_session_id,
                cohort_id=cohort_id,
                plan_sha256=plan_sha256,
                reason="Fence a pre-marker worker only after it stops.",
            )
            assert migrated["status"] == "operationally_blocked"
            assert cohort_id not in claude_core._COHORT_LIFELINE_WRITERS
    finally:
        claude_core._release_cohort_lifeline(cohort_id)
        os.close(lifeline_read)


def test_terminal_cancellation_refuses_busy_cohort_then_retries_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_sha256 = "7" * 64
    cohort_id = "cohort_" + "8" * 32
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    intent_path = state_dir / "intent.json"
    claude_core._write_once(
        intent_path,
        {
            "schema_version": claude_core.COHORT_INTENT_SCHEMA_LEGACY,
            "state": "submitted",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "created_at_unix": time.time(),
        },
        mode=0o600,
    )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda _problem_id, _statement_sha256: {"status": "published"},
    )
    execution_lock = open(state_dir / "cohort.lock", "a+b")
    fcntl.flock(execution_lock, fcntl.LOCK_EX)
    try:
        started_at = time.monotonic()
        with pytest.raises(
            claude_core.ClaudeCoreError, match="found an active cohort"
        ):
            claude_core._admit_cohort_intent(
                state_dir=state_dir,
                receipt_path=state_dir / "receipt.json",
                intent_path=intent_path,
                cohort_id=cohort_id,
                problem_id="example",
                statement_sha256=digest,
                plan_sha256=plan_sha256,
                root_session_id=root_session_id,
                timeout_seconds=60,
                codex_bin=None,
            )
        assert time.monotonic() - started_at < 1
        assert not (state_dir / "cancellation.json").exists()
        assert not (state_dir / "worker.json").exists()
    finally:
        try:
            fcntl.flock(execution_lock, fcntl.LOCK_UN)
        finally:
            execution_lock.close()

    with pytest.raises(
        claude_core.ClaudeCoreError, match="stranded cohort intent was cancelled"
    ):
        claude_core._admit_cohort_intent(
            state_dir=state_dir,
            receipt_path=state_dir / "receipt.json",
            intent_path=intent_path,
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=digest,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
            timeout_seconds=60,
            codex_bin=None,
        )
    assert (state_dir / "cancellation.json").is_file()
    assert not (state_dir / "worker.json").exists()


def test_takeover_preserves_same_host_predispatch_finalization_for_exact_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="a" * 64,
    )
    resumed = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    assert resumed["root_session_id"] == root_session_id
    takeover = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        canonical_model="claude-fable-5",
        takeover_from=root_session_id,
    )
    assert takeover["previous_root_session_id"] == root_session_id
    assert not (intent_path.parent / "recovery_result.json").exists()
    assert not (intent_path.parent / "settlement.json").exists()

    resumed_intent, resumed_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256=str(intent["blueprint_sha256"]),
    )
    assert resumed_path == intent_path
    assert resumed_intent == intent


def test_host_drift_predispatch_recovery_allows_successor_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    live_source = ["a" * 64]
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: live_source[0]
    )
    digest = _statement_digest()
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="b" * 64,
    )
    live_source[0] = "c" * 64
    claude_core._reconcile_definite_publication_finalizations(
        problem_id="example",
        statement_sha256=digest,
        expected_host_source_sha256=str(intent["host_source_sha256"]),
    )
    recovery_path = intent_path.parent / "recovery_result.json"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert recovery["status"] == "predispatch_abandoned"
    assert recovery["external_effect_state"] == "not_dispatched"
    assert recovery["intent_sha256"] == hashlib.sha256(
        intent_path.read_bytes()
    ).hexdigest()
    assert json.loads(
        (intent_path.parent / "settlement.json").read_text(encoding="utf-8")
    )["status"] == "not_published"

    retry_intent, retry_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256=str(intent["blueprint_sha256"]),
    )
    assert retry_path != intent_path
    assert retry_intent["generation_parent_intent_sha256"] == recovery[
        "intent_sha256"
    ]
    assert retry_intent["generation_parent_result_sha256"] == hashlib.sha256(
        recovery_path.read_bytes()
    ).hexdigest()
    assert retry_intent["operational_retry_ordinal"] == 1
    assert retry_intent["host_source_sha256"] == live_source[0]


def test_takeover_never_repeats_unknown_dispatched_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        claude_core,
        "_recover_archived_prepared_publication",
        lambda **_arguments: None,
    )
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="a" * 64,
    )
    calls = 0

    def verifier(commit_dispatch: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert callable(commit_dispatch)
        commit_dispatch()
        raise RuntimeError("synthetic crash after dispatch")

    with pytest.raises(RuntimeError, match="after dispatch"):
        claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=verifier,
        )
    with pytest.raises(
        claude_core.ClaudeCoreError, match="execution is unknown"
    ):
        _prepare_root(
            problem_id="example",
            statement_sha256=digest,
            root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            canonical_model="claude-fable-5",
            takeover_from=root_session_id,
        )
    assert calls == 1
    assert not (intent_path.parent / "recovery_result.json").exists()
    assert not (intent_path.parent / "settlement.json").exists()


def test_dispatched_outer_recovery_resumes_only_its_exact_client_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=_statement_digest(),
        blueprint_sha256="b" * 64,
    )

    def crash_after_dispatch(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        raise RuntimeError("synthetic transport loss")

    with claude_core._publication_finalization_execution_lock(intent_path):
        with pytest.raises(RuntimeError, match="transport loss"):
            claude_core._execute_publication_finalization_verifier(
                intent=intent,
                intent_path=intent_path,
                verifier=crash_after_dispatch,
            )
    recovery_calls: list[dict[str, object]] = []

    def recover(**arguments: object) -> dict[str, object]:
        recovery_calls.append(dict(arguments))
        return {
            "published": False,
            "publication_blocked_reason": "invalid_verifier_response",
        }

    monkeypatch.setattr(
        claude_core, "_recover_archived_prepared_publication", recover
    )
    with claude_core._publication_finalization_execution_lock(intent_path):
        recovered = claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=lambda _commit: pytest.fail(
                "outer recovery must not create a fresh verifier dispatch"
            ),
        )

    assert recovered["published"] is False
    assert len(recovery_calls) == 1
    assert recovery_calls[0]["dispatched_authority_intent_sha256"] == (
        claude_core.sha256_file(intent_path)
    )
    assert (intent_path.parent / "result.json").is_file()


def test_cross_layer_authority_binds_settled_outer_without_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: "a" * 64
    )
    monkeypatch.setattr(
        claude_core, "_existing_publication", lambda *_args, **_kwargs: None
    )
    statement_sha256 = _statement_digest()
    old_blueprint = _structured_blueprint("Old archived proof.")
    blueprint_sha256 = hashlib.sha256(old_blueprint.encode("utf-8")).hexdigest()
    replacement_blueprint_sha256 = hashlib.sha256(
        _structured_blueprint("New replacement proof.").encode("utf-8")
    ).hexdigest()
    old_intent, old_intent_path = (
        claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
        )
    )

    def crash_after_dispatch(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        raise RuntimeError("synthetic transport loss")

    with claude_core._publication_finalization_execution_lock(old_intent_path):
        with pytest.raises(RuntimeError, match="transport loss"):
            claude_core._execute_publication_finalization_verifier(
                intent=old_intent,
                intent_path=old_intent_path,
                verifier=crash_after_dispatch,
            )
        old_settlement = claude_core._settle_publication_finalization(
            intent=old_intent,
            intent_path=old_intent_path,
            status="not_published",
            publication_receipt_sha256=None,
        )

    claude_core._archive_blueprint(
        problem_id="example",
        blueprint_sha256=blueprint_sha256,
        blueprint_markdown=old_blueprint,
    )
    old_intent_sha256 = claude_core.sha256_file(old_intent_path)
    replacement_intent, replacement_intent_path = (
        claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=replacement_blueprint_sha256,
        )
    )
    replacement_intent_sha256 = claude_core.sha256_file(
        replacement_intent_path
    )
    verifier_effect_sha256 = "f" * 64
    admission = {
        "schema_version": "rethlas_publication_admission_v3",
        "status": "submitted",
        "phase": "dispatched",
        "problem_id": "example",
        "statement_source_digest": statement_sha256,
        "proof_digest": blueprint_sha256,
        "effect_intent_sha256": "e" * 64,
        "effect_dispatch_name": None,
        "verifier_effect_identity_sha256": verifier_effect_sha256,
    }
    admission_sha256 = claude_core.sha256_bytes(
        (claude_core.canonical_json(admission) + "\n").encode("utf-8")
    )
    recovery_request = {
        "schema_version": "rethlas_cross_layer_publication_recovery_discovery_v1",
        "admission": admission,
        "admission_prior_sha256": admission_sha256,
        "admission_effect_intent_sha256": "e" * 64,
        "verifier_effect_identity_sha256": verifier_effect_sha256,
        "artifact_observations": {
            "canonical_receipt_absent": True,
            "prepared_archive_absent": True,
            "receipt_collision_rollback_absent": True,
            "verified_target": {"kind": "absent"},
        },
        "replacement_authority_intent_sha256": (
            replacement_intent_sha256
        ),
    }

    # Exact recovery must not enumerate sibling generations: an unrelated
    # corrupt entry cannot turn a content-addressed old authority into a DoS.
    malformed_sibling = old_intent_path.parent.parent / ("7" * 64)
    malformed_sibling.mkdir()
    (malformed_sibling / "intent.json").write_text(
        "not-json\n", encoding="utf-8"
    )

    authority = claude_core._publication_admission_recovery_authority(
        recovery_request=recovery_request,
        replacement_intent=replacement_intent,
        replacement_intent_path=replacement_intent_path,
    )

    assert authority["intent"] == old_intent
    assert authority["settlement"] == old_settlement
    assert authority["dispatch"]["intent_sha256"] == old_intent_sha256
    assert authority["recovery_blueprint"] == {
        "schema_version": "rethlas_publication_recovery_blueprint_v1",
        "proof_digest": blueprint_sha256,
        "proof": old_blueprint,
    }


def test_generation_hashed_finalization_persists_exact_intent_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: "a" * 64
    )
    statement_sha256 = _statement_digest()
    blueprint_sha256 = "8" * 64
    first_intent, first_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )

    with claude_core._publication_finalization_execution_lock(first_path):
        first_result = claude_core._execute_publication_finalization_verifier(
            intent=first_intent,
            intent_path=first_path,
            verifier=lambda commit: (
                commit(),
                {
                    "published": False,
                    "publication_blocked_reason": "verifier_operational_failed",
                },
            )[1],
        )
        assert first_result["published"] is False
        claude_core._settle_publication_finalization(
            intent=first_intent,
            intent_path=first_path,
            status="not_published",
            publication_receipt_sha256=None,
        )

    successor, successor_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )
    successor_sha256 = claude_core.sha256_file(successor_path)
    assert successor_path.parent.name != blueprint_sha256
    locator_path = claude_core._publication_finalization_locator_path(
        problem_id="example",
        intent_sha256=successor_sha256,
    )
    assert locator_path.is_file()

    resolved, resolved_path = (
        claude_core._publication_finalization_from_exact_locator(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
            intent_sha256=successor_sha256,
        )
    )
    assert resolved == successor
    assert resolved_path == successor_path


@pytest.mark.parametrize(
    "finalization_state", ["intent_only", "dispatch_only", "negative_result"]
)
def test_source_drift_migration_reconciles_only_definite_finalizations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalization_state: str,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    source_a = "a" * 64
    source_b = "b" * 64
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: source_a
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "f" * 32
    pointer_path = claude_core._council_pointer_path(
        "example", root_session_id
    )
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": root_session_id,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": source_a,
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "active",
            "final_plan_sha256": None,
            "acceptance_sha256": None,
            "checkpoint_sha256": None,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    claude_core._council_dir("example", council_id)
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="9" * 64,
    )
    verifier_calls = 0

    def verifier(commit_dispatch: object) -> dict[str, object]:
        nonlocal verifier_calls
        verifier_calls += 1
        assert callable(commit_dispatch)
        commit_dispatch()
        if finalization_state == "dispatch_only":
            raise RuntimeError("synthetic verifier crash")
        return {"published": False, "verdict": "incorrect"}

    if finalization_state != "intent_only":
        if finalization_state == "dispatch_only":
            with pytest.raises(RuntimeError, match="synthetic verifier crash"):
                claude_core._execute_publication_finalization_verifier(
                    intent=intent, intent_path=intent_path, verifier=verifier
                )
        else:
            assert claude_core._execute_publication_finalization_verifier(
                intent=intent, intent_path=intent_path, verifier=verifier
            )["published"] is False
    calls_before_migration = verifier_calls
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)
    # Migration runs in the newly authenticated deployment, not in the old
    # process that created the finalization artifacts above.
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: source_b
    )

    if finalization_state == "dispatch_only":
        with pytest.raises(
            claude_core.ClaudeCoreError, match="execution is unknown"
        ):
            claude_core.migrate_stale_route_council(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=root_session_id,
                reason="Reconcile verifier admission after a source upgrade.",
                confirm_source_drift=True,
            )
        assert json.loads(pointer_path.read_text())["state"] == "active"
        assert not (intent_path.parent / "settlement.json").exists()
    else:
        migration = claude_core.migrate_stale_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            reason="Reconcile verifier admission after a source upgrade.",
            confirm_source_drift=True,
        )
        assert migration["status"] == "source_drift_blocked"
        settlement = json.loads(
            (intent_path.parent / "settlement.json").read_text()
        )
        assert settlement["status"] == "not_published"
    assert verifier_calls == calls_before_migration


def _historical_council_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, model: str
) -> dict[str, Any]:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    phase = "revision"
    problem_id = "example"
    statement_sha256 = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    council_id = "council_" + "a" * 32
    host_source_sha256 = "b" * 64
    state_dir = claude_core._council_dir(problem_id, council_id)
    monkeypatch.setattr(
        claude_core,
        "statement_retrieval_policy",
        lambda **_arguments: {
            "mode": "matlas_arxiv",
            "basis": "explicit_matlas_arxiv_permission",
        },
    )
    retrieval_profile = claude_core._council_retrieval_profile(
        problem_id=problem_id, statement_sha256=statement_sha256
    )
    # This request was committed by an older host with a smaller per-phase
    # budget.  Migration authenticates it but never re-executes the capability.
    retrieval_profile["max_search_queries"] = 1
    request = {
        "schema_version": "rethlas_route_council_revision_request_v2",
        "phase": phase,
        "council_id": council_id,
        "problem_id": problem_id,
        "statement_sha256": statement_sha256,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": retrieval_profile,
    }
    request_path = state_dir / f"{phase}_request.json"
    claude_core._write_once(request_path, request, mode=0o400)
    historical_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["legacy"],
        "properties": {"legacy": {"type": "string"}},
        "allOf": [{"not": {"required": ["forbidden"]}}],
    }
    schema_path = state_dir / f"{phase}_output_schema.json"
    claude_core._write_once(schema_path, historical_schema, mode=0o400)
    request_sha256 = claude_core.sha256_file(request_path)
    output_schema_sha256 = claude_core.sha256_file(schema_path)
    retrieval_profile_sha256 = hashlib.sha256(
        (claude_core.canonical_json(retrieval_profile) + "\n").encode()
    ).hexdigest()
    intent_path = state_dir / f"{phase}_intent.json"
    claude_core._write_once(
        intent_path,
        {
            "schema_version": claude_core.COUNCIL_PHASE_INTENT_SCHEMA,
            "state": "submitted",
            "phase": phase,
            "council_id": council_id,
            "problem_id": problem_id,
            "statement_sha256": statement_sha256,
            "root_session_id": root_session_id,
            "request_sha256": request_sha256,
            "model": model,
            "reasoning_effort": "max",
            "retrieval_profile_sha256": retrieval_profile_sha256,
            "retrieval_capability": retrieval_profile["capability"],
            "max_search_queries": retrieval_profile["max_search_queries"],
            "max_primary_reads": retrieval_profile["max_primary_reads"],
            "output_schema_sha256": output_schema_sha256,
            "host_source_sha256": host_source_sha256,
            "codex_bin": str(Path(sys.executable).resolve()),
            "codex_bin_sha256": hashlib.sha256(
                Path(sys.executable).resolve().read_bytes()
            ).hexdigest(),
            "timeout_seconds": 60,
            "created_at_unix": time.time(),
        },
        mode=0o400,
    )
    claude_core._commit_council_phase_dispatch(
        state_dir / f"{phase}_dispatch.json",
        phase=phase,
        council_id=council_id,
        problem_id=problem_id,
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        request_sha256=request_sha256,
        output_schema_sha256=output_schema_sha256,
        host_source_sha256=host_source_sha256,
        intent_sha256=claude_core.sha256_file(intent_path),
    )
    pointer = {
        "council_id": council_id,
        "problem_id": problem_id,
        "statement_sha256": statement_sha256,
        "root_session_id": root_session_id,
        "host_source_sha256": host_source_sha256,
    }
    return {
        "state_dir": state_dir,
        "pointer": pointer,
        "schema_path": schema_path,
        "historical_schema": historical_schema,
        "intent_path": intent_path,
        "validator_args": {
            **pointer,
            "phase": phase,
            "request_sha256": request_sha256,
            "retrieval_profile": retrieval_profile,
            "retrieval_profile_sha256": retrieval_profile_sha256,
            "output_schema_sha256": output_schema_sha256,
        },
    }


@pytest.mark.parametrize("historical_model", ["gpt-5.6-sol", "gpt-6-astra"])
def test_source_drift_fence_accepts_historical_phase_schema_by_hash_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical_model: str
) -> None:
    case = _historical_council_phase(tmp_path, monkeypatch, model=historical_model)
    state_dir = case["state_dir"]
    pointer = case["pointer"]
    phase = case["validator_args"]["phase"]
    before = {path: path.read_bytes() for path in state_dir.iterdir()}
    claude_core._fence_source_drift_phase_dispatches(
        state_dir=state_dir, pointer=pointer
    )
    execution_path = state_dir / f"{phase}_execution.json"
    assert execution_path.is_file()
    assert json.loads(execution_path.read_bytes())["execution"]["retry_allowed"] is False
    assert all(path.read_bytes() == raw for path, raw in before.items())

    claude_core._replace_canonical(
        case["schema_path"], {**case["historical_schema"], "required": ["changed"]}
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="intent collision"):
        claude_core._fence_source_drift_phase_dispatches(
            state_dir=state_dir, pointer=pointer
        )


def _add_historical_council_receipt(case: dict[str, Any]) -> Path:
    args = case["validator_args"]
    intent = json.loads(case["intent_path"].read_bytes())
    report = {"legacy": "Historical report retained without paid replay."}
    execution = {"status": "completed", "retry_allowed": False}
    path = case["state_dir"] / f"{args['phase']}_receipt.json"
    receipt = {
        "schema_version": claude_core.COUNCIL_PHASE_RECEIPT_SCHEMA,
        "status": "completed",
        **case["pointer"],
        "phase": args["phase"],
        "request_sha256": args["request_sha256"],
        "model": intent["model"],
        "reasoning_effort": "max",
        "retrieval_profile": args["retrieval_profile"],
        "retrieval_profile_sha256": args["retrieval_profile_sha256"],
        "output_schema_sha256": args["output_schema_sha256"],
        "report": report,
        "report_sha256": hashlib.sha256(
            (claude_core.canonical_json(report) + "\n").encode()
        ).hexdigest(),
        "execution": execution,
        "retry_allowed": False,
        "settled_at_unix": 1.0,
    }
    claude_core._write_once(path, receipt, mode=0o400)
    claude_core._persist_council_phase_execution(
        path=case["state_dir"] / f"{args['phase']}_execution.json",
        execution={**execution, "report": report},
        **{
            key: value
            for key, value in args.items()
            if key not in {"retrieval_profile", "retrieval_profile_sha256"}
        },
    )
    return path


@pytest.mark.parametrize("historical_model", ["gpt-5.6-sol", "gpt-6-astra"])
def test_source_drift_preserves_completed_historical_model_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical_model: str
) -> None:
    case = _historical_council_phase(tmp_path, monkeypatch, model=historical_model)
    receipt_path = _add_historical_council_receipt(case)
    before = {path: path.read_bytes() for path in case["state_dir"].iterdir()}

    def forbidden_dispatch(**_kwargs: Any) -> None:
        raise AssertionError("Historical authentication must not dispatch a model")

    monkeypatch.setattr(claude_core, "_invoke_sol_council", forbidden_dispatch)
    for _ in range(2):
        claude_core._fence_source_drift_phase_dispatches(
            state_dir=case["state_dir"], pointer=case["pointer"]
        )
    assert json.loads(receipt_path.read_bytes())["model"] == historical_model
    assert {path: path.read_bytes() for path in case["state_dir"].iterdir()} == before


def test_current_council_intent_rejects_historical_sol_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _historical_council_phase(tmp_path, monkeypatch, model="gpt-5.6-sol")
    with pytest.raises(claude_core.ClaudeCoreError, match="intent collision"):
        claude_core._validate_council_phase_intent(
            case["intent_path"], **case["validator_args"]
        )


def test_current_council_receipt_rejects_historical_sol_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _historical_council_phase(tmp_path, monkeypatch, model="gpt-5.6-sol")
    path = _add_historical_council_receipt(case)
    args = case["validator_args"]
    # Isolate model admission from the separately strict source/profile gates.
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: args["host_source_sha256"]
    )
    monkeypatch.setattr(
        claude_core, "_council_retrieval_profile", lambda **_kwargs: args["retrieval_profile"]
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="receipt binding mismatch"):
        claude_core._read_council_phase_receipt(
            path,
            **{
                key: value
                for key, value in args.items()
                if key not in {
                    "host_source_sha256", "output_schema_sha256",
                    "retrieval_profile", "retrieval_profile_sha256",
                }
            },
        )


def test_source_drift_rejects_relabelled_intent_after_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _historical_council_phase(tmp_path, monkeypatch, model="gpt-5.6-sol")
    intent = json.loads(case["intent_path"].read_bytes())
    claude_core._replace_canonical(
        case["intent_path"], {**intent, "model": "gpt-6-astra"}
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="dispatch.*mismatch"):
        claude_core._fence_source_drift_phase_dispatches(
            state_dir=case["state_dir"], pointer=case["pointer"]
        )
    assert not (case["state_dir"] / "revision_execution.json").exists()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("model", "gpt-6-astra"),
        ("host_source_sha256", "c" * 64),
        ("report_sha256", "d" * 64),
    ],
)
def test_source_drift_rejects_tampered_historical_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, replacement: str
) -> None:
    case = _historical_council_phase(tmp_path, monkeypatch, model="gpt-5.6-sol")
    path = _add_historical_council_receipt(case)
    receipt = json.loads(path.read_bytes())
    claude_core._replace_canonical(path, {**receipt, field: replacement})
    with pytest.raises(claude_core.ClaudeCoreError, match="mismatch"):
        claude_core._fence_source_drift_phase_dispatches(
            state_dir=case["state_dir"], pointer=case["pointer"]
        )


def test_source_drift_rejects_unknown_historical_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _historical_council_phase(tmp_path, monkeypatch, model="unapproved-model")
    with pytest.raises(claude_core.ClaudeCoreError, match="model is unsupported"):
        claude_core._fence_source_drift_phase_dispatches(
            state_dir=case["state_dir"], pointer=case["pointer"]
        )


def test_source_drift_reconciles_unknown_dispatch_from_exact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    source_sha256 = "a" * 64
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: source_sha256
    )
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="9" * 64,
    )
    with pytest.raises(RuntimeError, match="synthetic verifier crash"):
        claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=lambda commit_dispatch: (
                commit_dispatch(),
                (_ for _ in ()).throw(RuntimeError("synthetic verifier crash")),
            )[1],
        )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda *_arguments, **_kwargs: {
            "status": "published",
            "proof_sha256": "9" * 64,
            "publication_receipt_sha256": "8" * 64,
        },
    )
    claude_core._reconcile_source_drift_publication_finalizations(
        problem_id="example",
        statement_sha256=digest,
        expected_host_source_sha256=source_sha256,
    )
    settlement = json.loads(
        (intent_path.parent / "settlement.json").read_text()
    )
    assert settlement["status"] == "published"
    assert settlement["publication_receipt_sha256"] == "8" * 64


def test_settled_finalization_waiter_never_dispatches_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="9" * 64,
    )
    claude_core._settle_publication_finalization(
        intent=intent,
        intent_path=intent_path,
        status="not_published",
        publication_receipt_sha256=None,
    )
    verifier_calls = 0

    def verifier(_commit_dispatch: object) -> dict[str, object]:
        nonlocal verifier_calls
        verifier_calls += 1
        return {"published": True}

    with claude_core._publication_finalization_execution_lock(intent_path):
        assert claude_core._execute_publication_finalization_verifier(
            intent=intent, intent_path=intent_path, verifier=verifier
        ) == {"published": False}
    assert verifier_calls == 0
    assert not (intent_path.parent / "dispatch.json").exists()
    assert not (intent_path.parent / "result.json").exists()


def test_published_finalization_reconciliation_requires_the_exact_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="a" * 64,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="differs"):
        claude_core._reconcile_published_finalizations(
            problem_id="example",
            statement_sha256=digest,
            publication={
                "proof_sha256": "b" * 64,
                "publication_receipt_sha256": "c" * 64,
            },
        )
    assert not (intent_path.parent / "settlement.json").exists()
    claude_core._reconcile_published_finalizations(
        problem_id="example",
        statement_sha256=digest,
        publication={
            "proof_sha256": "a" * 64,
            "publication_receipt_sha256": "c" * 64,
        },
    )
    settlement = claude_core._read_publication_finalization_settlement(
        intent_path.parent / "settlement.json",
        intent=intent,
        intent_sha256=hashlib.sha256(intent_path.read_bytes()).hexdigest(),
    )
    assert settlement["status"] == "published"
    with pytest.raises(claude_core.ClaudeCoreError, match="settlement differs"):
        claude_core._reconcile_published_finalizations(
            problem_id="example",
            statement_sha256=digest,
            publication={
                "proof_sha256": "a" * 64,
                "publication_receipt_sha256": "d" * 64,
            },
        )


def test_published_finalization_reconciliation_is_concurrently_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    digest = _statement_digest()
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=digest,
        blueprint_sha256="a" * 64,
    )
    publication = {
        "proof_sha256": "a" * 64,
        "publication_receipt_sha256": "c" * 64,
    }
    real_execution_lock = claude_core._publication_finalization_execution_lock
    callers_ready = threading.Barrier(2)
    lock_entries: list[str] = []

    @contextmanager
    def synchronized_execution_lock(intent_argument: Path, **kwargs: object):
        lock_entries.append(str(intent_argument))
        callers_ready.wait(timeout=10)
        with real_execution_lock(intent_argument, **kwargs):
            yield

    monkeypatch.setattr(
        claude_core,
        "_publication_finalization_execution_lock",
        synchronized_execution_lock,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        reconciliations = [
            executor.submit(
                claude_core._reconcile_published_finalizations,
                problem_id="example",
                statement_sha256=digest,
                publication=publication,
            )
            for _index in range(2)
        ]
        for reconciliation in reconciliations:
            reconciliation.result(timeout=10)

    assert lock_entries == [str(intent_path), str(intent_path)]
    settlement_path = intent_path.parent / "settlement.json"
    settlement_bytes = settlement_path.read_bytes()
    settlement = claude_core._read_publication_finalization_settlement(
        settlement_path,
        intent=intent,
        intent_sha256=hashlib.sha256(intent_path.read_bytes()).hexdigest(),
    )
    assert settlement["status"] == "published"
    assert settlement["publication_receipt_sha256"] == "c" * 64
    assert settlement_path.read_bytes() == settlement_bytes


def test_blueprint_writer_rechecks_finalization_after_waiting_for_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    result_dir = generation_root / "results" / "example"
    data_root.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path = data_root / "example.md"
    statement_path.write_text("Statement.\n", encoding="utf-8")
    digest = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    blueprint_path = result_dir / "blueprint.md"
    blueprint_path.write_text("old blueprint\n", encoding="utf-8")
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_validate_blueprint_markdown", lambda **_arguments: None
    )

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with claude_core._blueprint_write_lock("example"):
            writer = executor.submit(
                claude_core.write_blueprint,
                problem_id="example",
                statement_sha256=digest,
                blueprint_markdown="new blueprint\n",
            )
            time.sleep(0.2)
            assert not writer.done()
            claude_core._begin_publication_finalization(
                problem_id="example",
                statement_sha256=digest,
                blueprint_sha256=hashlib.sha256(
                    blueprint_path.read_bytes()
                ).hexdigest(),
            )
        with pytest.raises(claude_core.ClaudeCoreError, match="unresolved"):
            writer.result(timeout=5)
    finally:
        executor.shutdown(wait=True)
    assert blueprint_path.read_text(encoding="utf-8") == "old blueprint\n"


def test_finalization_admission_serializes_against_cohort_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    (result_dir / "blueprint.md").write_text(
        _structured_blueprint("Complete proof."), encoding="utf-8"
    )
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    state_root = tmp_path / "state"
    input_root = tmp_path / "inputs"
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    verifier_entered = threading.Event()
    release_verifier = threading.Event()

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            assert (
                arguments["timeout_seconds"]
                == claude_core.PUBLICATION_VERIFICATION_TIMEOUT_SECONDS
            )
            deadline = datetime.fromisoformat(
                str(arguments["verification_deadline_utc"])
            )
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            assert (
                claude_core.PUBLICATION_VERIFICATION_TIMEOUT_SECONDS - 10
                <= remaining
                <= claude_core.PUBLICATION_VERIFICATION_TIMEOUT_SECONDS
            )
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            verifier_entered.set()
            if not release_verifier.wait(timeout=10):
                raise AssertionError("cohort admission did not finish")
            return {"published": False}

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    with ThreadPoolExecutor(max_workers=1) as executor:
        verification = executor.submit(
            claude_core.verify_blueprint, "example", statement_sha256
        )
        assert verifier_entered.wait(timeout=10)
        with pytest.raises(claude_core.ClaudeCoreError, match="unresolved"):
            claude_core.run_three_route_cohort(
                problem_id="example",
                statement_sha256=statement_sha256,
                plans=_plans(),
                root_session_id=root_session_id,
                timeout_seconds=60,
                wait_seconds=0,
                codex_bin=Path(sys.executable),
            )
        release_verifier.set()
        assert verification.result(timeout=10)["published"] is False
    problem_state = state_root / "example"
    assert not any(problem_state.glob("cohort_*/intent.json"))


def test_terminal_publication_is_rechecked_before_new_cohort_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    plans = _plans()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", tmp_path / "inputs")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        claude_core,
        "_spawn_cohort_worker",
        lambda **arguments: spawned.append(str(arguments["cohort_id"])),
    )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda _problem_id, _statement_sha256: {"status": "published"},
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="cohort admission"):
        claude_core.run_three_route_cohort(
            problem_id="example",
            statement_sha256=digest,
            plans=plans,
            root_session_id=root_session_id,
            timeout_seconds=60,
            wait_seconds=0,
        )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=plans,
        root_session_id=root_session_id,
    )
    plan_sha256 = hashlib.sha256(
        (claude_core.canonical_json(plan_set) + "\n").encode()
    ).hexdigest()
    cohort_id = "cohort_" + plan_sha256[:32]
    assert not (state_root / "example" / cohort_id / "intent.json").exists()
    assert spawned == []


def test_cohort_intent_replay_binds_timeout_codex_and_runner_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    cohort_id = "cohort_" + "6" * 32
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = hashlib.sha256(
        (claude_core.canonical_json(plan_set) + "\n").encode()
    ).hexdigest()
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    closure = ["8" * 64]
    monkeypatch.setattr(
        claude_core,
        "_cohort_runner_closure_sha256",
        lambda _runner: closure[0],
    )
    monkeypatch.setattr(
        claude_core,
        "_require_codex_login",
        lambda executable: Path(executable).resolve(strict=True),
    )
    arguments = {
        "state_dir": state_dir,
        "receipt_path": state_dir / "receipt.json",
        "intent_path": state_dir / "intent.json",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "timeout_seconds": 60,
        "codex_bin": Path(sys.executable),
    }
    settled, should_spawn, intent = claude_core._admit_cohort_intent(**arguments)
    assert settled is None and should_spawn is True
    assert intent is not None
    assert intent["runner_closure_sha256"] == closure[0]

    with pytest.raises(claude_core.ClaudeCoreError, match="binding changed"):
        claude_core._admit_cohort_intent(**{**arguments, "timeout_seconds": 61})
    with pytest.raises(claude_core.ClaudeCoreError, match="binding changed"):
        claude_core._admit_cohort_intent(
            **{**arguments, "codex_bin": _SYSTEM_TRUE}
        )
    closure[0] = "9" * 64
    with pytest.raises(claude_core.ClaudeCoreError, match="binding changed"):
        claude_core._admit_cohort_intent(**arguments)
    migrated = claude_core.migrate_legacy_cohort_intent(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        cohort_id=cohort_id,
        plan_sha256=plan_sha256,
        reason="owner confirms the v3 runner closure drifted before launch",
    )
    assert migrated["status"] == "operationally_blocked"
    assert migrated["migration_policy"].endswith("fresh_root_epoch_v4")


def test_cohort_intent_byte_cap_precedes_snapshot_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    cohort_id = "cohort_" + "6" * 32
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    oversized_runner = Path("/" + "r" * 20_000)
    oversized_codex = Path("/" + "c" * 20_000)
    monkeypatch.setattr(claude_core, "RUNNER", oversized_runner)
    monkeypatch.setattr(
        claude_core, "_trusted_executable", lambda path, **_kwargs: Path(path)
    )
    monkeypatch.setattr(
        claude_core,
        "_sha256_stable_regular_file",
        lambda _path, **_kwargs: "7" * 64,
    )
    monkeypatch.setattr(
        claude_core, "_cohort_runner_closure_sha256", lambda _path: "8" * 64
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda executable: Path(executable)
    )
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: "9" * 64
    )

    def forbid_snapshot(*_args: object, **_kwargs: object) -> None:
        pytest.fail("oversized intent reached host-source snapshot publication")

    monkeypatch.setattr(claude_core, "_write_host_source_snapshot", forbid_snapshot)
    with pytest.raises(claude_core.ClaudeCoreError, match="intent exceeds"):
        claude_core._admit_cohort_intent(
            state_dir=state_dir,
            receipt_path=state_dir / "receipt.json",
            intent_path=state_dir / "intent.json",
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=digest,
            plan_sha256="a" * 64,
            root_session_id=root_session_id,
            timeout_seconds=60,
            codex_bin=oversized_codex,
        )
    assert not (state_dir / "intent.json").exists()
    assert not (state_dir / "host_source.py").exists()


def test_checkpointed_worker_survives_runner_and_codex_deployment_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", tmp_path / "inputs")
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    plans = _plans()
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=plans,
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    council_id = "council_" + "6" * 32
    acceptance_sha256 = "7" * 64
    checkpoint_sha256 = "8" * 64
    pointer_path = claude_core._council_pointer_path(
        "example", root_session_id
    )
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": root_session_id,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": claude_core._host_source_sha256(),
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "checkpointed",
            "final_plan_sha256": plan_sha256,
            "acceptance_sha256": acceptance_sha256,
            "checkpoint_sha256": checkpoint_sha256,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    monkeypatch.setattr(
        claude_core,
        "_validate_council_acceptance",
        lambda **_arguments: {"status": "accepted", "council_round": 1},
    )
    monkeypatch.setattr(
        claude_core,
        "_load_accepted_council_plan_set",
        lambda **_arguments: (
            plan_set,
            {"status": "accepted", "council_round": 1},
        ),
    )
    monkeypatch.setattr(
        claude_core,
        "_external_plan_checkpoint_evidence",
        lambda **_arguments: {
            "batch_id": "batch_worker_drift",
            "record_ids": [],
            "checkpoint_sha256": checkpoint_sha256,
            "commit_sha256": "9" * 64,
        },
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda value: Path(value)
    )
    closure_sha256 = ["a" * 64]
    monkeypatch.setattr(
        claude_core,
        "_cohort_runner_closure_sha256",
        lambda _runner: closure_sha256[0],
    )
    captured_state_dir: list[Path] = []

    def checkpoint_then_crash(**arguments: object) -> None:
        state_dir = arguments["state_dir"]
        assert isinstance(state_dir, Path)
        captured_state_dir.append(state_dir)
        worker_start_token = claude_core._process_identity_token(os.getpid())
        assert isinstance(worker_start_token, str)
        claude_core._write_once(
            state_dir / "worker.json",
            {
                "schema_version": claude_core.COHORT_WORKER_SCHEMA,
                "cohort_id": arguments["cohort_id"],
                "problem_id": arguments["problem_id"],
                "statement_sha256": arguments["statement_sha256"],
                "plan_sha256": arguments["plan_sha256"],
                "root_session_id": arguments["root_session_id"],
                "worker_pid": os.getpid(),
                "started_at_unix": time.time(),
                "frontier_before_sha256": "b" * 64,
                "worker_start_token": worker_start_token,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            },
            mode=0o400,
        )
        raise RuntimeError("synthetic parent crash before pointer CAS")

    monkeypatch.setattr(
        claude_core, "_spawn_cohort_worker", checkpoint_then_crash
    )
    arguments = {
        "problem_id": "example",
        "statement_sha256": digest,
        "plans": plans,
        "root_session_id": root_session_id,
        "timeout_seconds": 60,
        "wait_seconds": 0,
        "codex_bin": Path(sys.executable),
        "council_id": council_id,
        "council_receipt_sha256": acceptance_sha256,
    }
    with pytest.raises(RuntimeError, match="before pointer CAS"):
        claude_core.run_three_route_cohort(**arguments)
    assert len(captured_state_dir) == 1
    assert claude_core._read_council_pointer(
        pointer_path,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )["state"] == "checkpointed"

    closure_sha256[0] = "c" * 64
    monkeypatch.setattr(
        claude_core,
        "_spawn_cohort_worker",
        lambda **_arguments: pytest.fail("a durable worker must not respawn"),
    )
    worker_lock = open(captured_state_dir[0] / "cohort.lock", "a+b")
    fcntl.flock(worker_lock, fcntl.LOCK_EX)
    try:
        resumed = claude_core.run_three_route_cohort(
            **{**arguments, "codex_bin": _SYSTEM_TRUE}
        )
    finally:
        fcntl.flock(worker_lock, fcntl.LOCK_UN)
        worker_lock.close()
    assert resumed["status"] == "running"
    consumed = claude_core._read_council_pointer(
        pointer_path,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    assert consumed["state"] == "consumed"
    assert consumed["cohort_id"] == resumed["cohort_id"]


def test_new_cohort_spawns_one_detached_worker_and_reuses_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    plans = _plans()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    input_root = tmp_path / "inputs"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    calls: list[str] = []
    active_locks: list[object] = []

    def fake_spawn(**arguments: object) -> None:
        calls.append(str(arguments["plan_sha256"]))
        state_dir = arguments["state_dir"]
        assert isinstance(state_dir, Path)
        worker = {
            "schema_version": claude_core.COHORT_WORKER_SCHEMA,
            "cohort_id": "cohort_" + str(arguments["plan_sha256"])[:32],
            "problem_id": arguments["problem_id"],
            "statement_sha256": arguments["statement_sha256"],
            "plan_sha256": arguments["plan_sha256"],
            "root_session_id": arguments["root_session_id"],
            "worker_pid": os.getpid(),
            "started_at_unix": 1.0,
            "frontier_before_sha256": "1" * 64,
            "worker_start_token": claude_core._process_identity_token(
                os.getpid()
            ),
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        }
        (state_dir / "worker.json").write_text(
            claude_core.canonical_json(worker) + "\n", encoding="utf-8"
        )
        active_lock = open(state_dir / "cohort.lock", "a+b")
        fcntl.flock(active_lock, fcntl.LOCK_EX)
        active_locks.append(active_lock)

    monkeypatch.setattr(claude_core, "_spawn_cohort_worker", fake_spawn)
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda path: Path(path).resolve()
    )
    try:
        first = claude_core.run_three_route_cohort(
            problem_id="example",
            statement_sha256=digest,
            plans=plans,
            root_session_id=root_session_id,
            timeout_seconds=60,
            wait_seconds=0,
            codex_bin=Path(sys.executable),
        )
        second = claude_core.run_three_route_cohort(
            problem_id="example",
            statement_sha256=digest,
            plans=plans,
            root_session_id=root_session_id,
            timeout_seconds=60,
            wait_seconds=0,
            codex_bin=Path(sys.executable),
        )

        assert first["status"] == second["status"] == "running"
        assert first["worker_pid"] == second["worker_pid"]
        assert len(calls) == 1
    finally:
        for active_lock in active_locks:
            fcntl.flock(active_lock, fcntl.LOCK_UN)  # type: ignore[arg-type]
            active_lock.close()  # type: ignore[union-attr]


def test_codex_login_preflight_failure_creates_no_cohort_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    plans = _plans()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    input_root = tmp_path / "inputs"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )

    def reject_login(_codex_bin: Path) -> Path:
        raise claude_core.ClaudeCoreError("Codex CLI is not logged in")

    monkeypatch.setattr(claude_core, "_require_codex_login", reject_login)
    with pytest.raises(claude_core.ClaudeCoreError, match="not logged in"):
        claude_core.run_three_route_cohort(
            problem_id="example",
            statement_sha256=digest,
            plans=plans,
            root_session_id=root_session_id,
            timeout_seconds=60,
            wait_seconds=0,
            codex_bin=Path(sys.executable),
        )

    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=plans,
        root_session_id=root_session_id,
    )
    plan_sha = hashlib.sha256(
        (claude_core.canonical_json(plan_set) + "\n").encode()
    ).hexdigest()
    state_dir = state_root / "example" / ("cohort_" + plan_sha[:32])
    assert (state_dir / f"plan_{plan_sha}.json").is_file()
    assert not (state_dir / "intent.json").exists()
    assert not (state_dir / "worker.json").exists()


def test_opus_sol_council_runs_one_joint_revision_before_exact_cohort_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", tmp_path / "inputs")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    frontier = ["4" * 64]
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier[0]},
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    assert claude_core.route_council_status(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )["status"] == "none"
    calls: list[tuple[str, dict[str, object]]] = []
    audit_verdict = ["ready"]
    sol_plans = [
        {
            **plan,
            "plan_id": f"sol_route_{index}",
            "mechanism": f"independent Astra mechanism {index}",
            "scope": f"independent Astra scope {index}",
        }
        for index, plan in enumerate(_plans(), start=1)
    ]

    def fake_council(**arguments: object) -> dict[str, object]:
        phase = str(arguments["phase"])
        request = arguments["request"]
        assert isinstance(request, dict)
        calls.append((phase, request))
        common = {
            "council_id": request["council_id"],
            "statement_sha256": digest,
        }
        if phase == "blind":
            report: dict[str, object] = {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                **common,
                "plan_slots": _blind_plan_slots(sol_plans),
                "global_risks": ["Check all boundary cases."],
                "comparative_note": "Three independent synthetic mechanisms.",
            }
        elif phase == "revision":
            merged = request["opus_merged_plans"]
            assert isinstance(merged, list)
            report = {
                "schema_version": claude_core.COUNCIL_SOL_REVISION_SCHEMA,
                **common,
                "merged_plan_sha256": request["merged_plan_sha256"],
                "plan_reviews": [
                    {
                        "plan_id": plan["plan_id"],
                        "verdict": "keep",
                        "objections": [],
                        "required_changes": [],
                        "replacement_plan": None,
                    }
                    for plan in merged
                ],
                "global_assessment": "The merged slate is ready for adjudication.",
                "fanout_ready": True,
            }
        else:
            final = request["final_plans"]
            assert isinstance(final, list)
            blocked = audit_verdict[0] == "blocked"
            report = {
                "schema_version": claude_core.COUNCIL_SOL_AUDIT_SCHEMA,
                **common,
                "final_plan_sha256": request["final_plan_sha256"],
                "decision": {
                    "verdict": audit_verdict[0],
                    "plan_findings": [
                        {
                            "plan_id": plan["plan_id"],
                            "severity": (
                                "fatal" if blocked and index == 0 else "clear"
                            ),
                            "finding": (
                                "A synthetic fatal route-design defect remains."
                                if blocked and index == 0
                                else "No fatal route-design defect found."
                            ),
                            "required_change": (
                                "Repair or explicitly override the synthetic defect."
                                if blocked and index == 0
                                else None
                            ),
                        }
                        for index, plan in enumerate(final)
                    ],
                },
                "diversity_assessment": "The three mechanisms are materially distinct.",
                "rationale": "Ready for bounded proof fanout.",
            }
        return {
            "status": "completed",
            "report": report,
            "elapsed_seconds": 1.0,
            "returncode": 1,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", fake_council)
    opus_plans = [
        {
            **plan,
            "mechanism": f"private Opus mechanism {index}",
            "scope": f"private Opus scope {index}",
        }
        for index, plan in enumerate(_plans(), start=1)
    ]
    blind = claude_core.start_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        opus_plans=opus_plans,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert blind["status"] == "completed"
    assert "private Opus" not in claude_core.canonical_json(calls[0][1])
    council_id = str(blind["council_id"])

    merged_plans = _plans()
    revision = claude_core.revise_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=council_id,
        merged_plans=merged_plans,
        merge_rationale="Opus compared both blind slates and retained three disjoint routes.",
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert revision["status"] == "completed"
    revision_request = calls[-1][1]
    assert revision_request["schema_version"].endswith("revision_request_v3")
    assert "opus_blind_plans" not in revision_request
    assert "sol_blind_slate" not in revision_request
    assert len(revision_request["blind_route_origins"]["opus_routes"]) == 3
    assert len(revision_request["blind_route_origins"]["sol_routes"]) == 3
    assert all(
        set(route) == {
            "plan_id", "mechanism", "scope", "discriminating_test"
        }
        for route in revision_request["blind_route_origins"]["opus_routes"]
    )
    revision_status = claude_core.route_council_status(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    assert revision_status["council"]["state"] == "revision_complete"
    assert [phase["status"] for phase in revision_status["council"]["phases"]] == [
        "completed",
        "completed",
        "not_started",
    ]
    with pytest.raises(claude_core.ClaudeCoreError, match="advanced"):
        claude_core.start_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            opus_plans=opus_plans,
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )

    adjudications = [
        {
            "draft_plan_id": plan["plan_id"],
            "final_plan_id": plan["plan_id"],
            "decision": "accepted",
            "rationale": "Opus accepts Astra's keep recommendation after review.",
        }
        for plan in merged_plans
    ]
    final = claude_core.finalize_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=council_id,
        final_plans=merged_plans,
        adjudications=adjudications,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    repeated = claude_core.finalize_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=council_id,
        final_plans=merged_plans,
        adjudications=adjudications,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert [phase for phase, _request in calls] == ["blind", "revision", "audit"]
    audit_request = calls[-1][1]
    assert audit_request["schema_version"].endswith("audit_request_v3")
    assert "merged_plans" not in audit_request
    assert "sol_revision" not in audit_request
    assert len(audit_request["revision_findings"]["plan_reviews"]) == 3
    assert audit_request["final_plans"] == merged_plans
    assert final["acceptance"] == repeated["acceptance"]
    acceptance_sha256 = final["acceptance"]["acceptance_sha256"]

    with pytest.raises(claude_core.ClaudeCoreError, match="accepted route-council"):
        claude_core.run_three_route_cohort(
            problem_id="example",
            statement_sha256=digest,
            plans=merged_plans,
            root_session_id=root_session_id,
            timeout_seconds=60,
            wait_seconds=0,
        )

    spawned: list[str] = []

    fail_before_popen = [True]

    def fake_spawn(**arguments: object) -> None:
        pointer_during_spawn = claude_core._read_council_pointer(
            claude_core._council_pointer_path("example", root_session_id),
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
        )
        if pointer_during_spawn["state"] == "checkpointed":
            assert pointer_during_spawn["cohort_id"] is None
        else:
            assert pointer_during_spawn["state"] == "consumed"
            terminal_cohort_id, _seen = (
                claude_core._resolve_terminal_cohort_id(
                    problem_id="example",
                    statement_sha256=digest,
                    plan_sha256=str(arguments["plan_sha256"]),
                    root_session_id=root_session_id,
                    initial_cohort_id=str(pointer_during_spawn["cohort_id"]),
                )
            )
            assert terminal_cohort_id == arguments["cohort_id"]
        if fail_before_popen[0]:
            fail_before_popen[0] = False
            raise claude_core.ClaudeCoreError(
                "synthetic interruption before worker launch"
            )
        spawned.append(str(arguments["plan_sha256"]))
        state_dir = arguments["state_dir"]
        assert isinstance(state_dir, Path)
        worker = {
            "schema_version": "rethlas_claude_cohort_worker_v1",
            "cohort_id": arguments["cohort_id"],
            "problem_id": arguments["problem_id"],
            "statement_sha256": arguments["statement_sha256"],
            "plan_sha256": arguments["plan_sha256"],
            "root_session_id": arguments["root_session_id"],
            "worker_pid": os.getpid(),
            "started_at_unix": 1.0,
        }
        claude_core._write_once(state_dir / "worker.json", worker, mode=0o400)

    monkeypatch.setattr(claude_core, "_spawn_cohort_worker", fake_spawn)
    checkpoint_sha256 = "9" * 64
    monkeypatch.setattr(
        claude_core,
        "_external_plan_checkpoint_evidence",
        lambda **_arguments: {
            "batch_id": "batch_synthetic",
            "record_ids": [],
            "checkpoint_sha256": checkpoint_sha256,
            "commit_sha256": "8" * 64,
        },
    )
    with claude_core.root_authority_guard(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    ):
        accepted_pointer = claude_core._read_council_pointer(
            claude_core._council_pointer_path("example", root_session_id),
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
        )
        claude_core._update_council_pointer(
            accepted_pointer,
            state="checkpointed",
            final_plan_sha256=final["final_plan_sha256"],
            acceptance_sha256=acceptance_sha256,
            checkpoint_sha256=checkpoint_sha256,
        )
    cohort_arguments = {
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "timeout_seconds": 60,
        "wait_seconds": 0,
        "codex_bin": Path(sys.executable),
        "council_id": council_id,
        "council_receipt_sha256": acceptance_sha256,
    }
    with pytest.raises(
        claude_core.ClaudeCoreError, match="before worker launch"
    ):
        claude_core.run_three_route_cohort(**cohort_arguments)
    pointer_after_interruption = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", root_session_id),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    assert pointer_after_interruption["state"] == "checkpointed"
    assert pointer_after_interruption["cohort_id"] is None

    cohort = claude_core.run_three_route_cohort(
        **cohort_arguments
    )
    assert cohort["status"] == "running"
    assert len(spawned) == 1
    pointer = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", root_session_id),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    assert pointer["state"] == "consumed"
    assert pointer["cohort_id"] == cohort["cohort_id"]

    cohort_state = state_root / "example" / str(cohort["cohort_id"])
    log_path = cohort_state / "executor.log"
    log_path.write_text("synthetic recoverable runtime failure\n", encoding="utf-8")
    source_failed_receipt = {
        "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
        "status": "failed",
        "cohort_id": cohort["cohort_id"],
        "problem_id": "example",
        "statement_sha256": digest,
        "plan_sha256": cohort["plan_sha256"],
        "root_session_id": root_session_id,
        "returncode": 70,
        "timed_out": False,
        "elapsed_seconds": 1.0,
        "frontier_before_sha256": "4" * 64,
        "frontier_after_sha256": "4" * 64,
        "frontier_changed": False,
        "log_path": str(log_path),
        "log_bytes": log_path.stat().st_size,
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "log_over_cap": False,
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        "retry_allowed": False,
        "completion_evidence": None,
    }
    claude_core._write_once(
        cohort_state / "receipt.json", source_failed_receipt, mode=0o400
    )
    recovery = claude_core.authorize_failed_cohort_recovery(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        plan_sha256=str(cohort["plan_sha256"]),
        source_cohort_id=str(cohort["cohort_id"]),
        codex_bin=Path(sys.executable),
    )
    recovery_pending = claude_core.run_three_route_cohort(**cohort_arguments)
    assert recovery_pending["status"] == "running"
    assert recovery_pending["cohort_id"] == recovery["recovery_cohort_id"]
    recovery_state = state_root / "example" / str(recovery_pending["cohort_id"])
    recovery_log = recovery_state / "executor.log"
    recovery_log.write_text("synthetic three-route failure\n", encoding="utf-8")
    terminal_receipt = {
        **source_failed_receipt,
        "status": "completed_unverified",
        "cohort_id": recovery_pending["cohort_id"],
        "returncode": 1,
        "frontier_after_sha256": "5" * 64,
        "frontier_changed": True,
        "log_path": str(recovery_log),
        "log_bytes": recovery_log.stat().st_size,
        "log_sha256": hashlib.sha256(recovery_log.read_bytes()).hexdigest(),
        "completion_evidence": _completion_evidence(),
    }
    terminal_receipt_sha256 = claude_core._write_once(
        recovery_state / "receipt.json", terminal_receipt, mode=0o400
    )
    frontier[0] = "6" * 64
    successor_opus_plans = [
        {
            **plan,
            "mechanism": f"successor Opus mechanism {index}",
            "scope": f"successor Opus scope {index}",
        }
        for index, plan in enumerate(_plans(), start=1)
    ]
    successor = claude_core.start_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        opus_plans=successor_opus_plans,
        prior_failure_context=(
            "All three synthetic lanes failed their distinct discriminating tests."
        ),
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert successor["council_round"] == 2
    assert successor["council_id"] != council_id
    assert successor["prior_failure_receipt_sha256"] == terminal_receipt_sha256

    successor_id = str(successor["council_id"])
    audit_verdict[0] = "blocked"
    claude_core.revise_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=successor_id,
        merged_plans=merged_plans,
        merge_rationale="Opus performs the successor merge once.",
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    blocked = claude_core.finalize_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=successor_id,
        final_plans=merged_plans,
        adjudications=adjudications,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert blocked["report"]["decision"]["verdict"] == "blocked"
    assert blocked["acceptance"] is None
    corrected_plans = json.loads(json.dumps(merged_plans))
    corrected_plans[0]["plan_summary"] = (
        "summary 1 corrected exactly as required by the fatal audit finding"
    )
    finding_resolutions = [
        {
            "plan_id": merged_plans[0]["plan_id"],
            "disposition": "corrected",
            "rationale": (
                "The corrected route removes the synthetic fatal defect."
            ),
        }
    ]
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="requires the complete corrected plan set",
    ):
        claude_core.override_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            council_id=successor_id,
            audit_plan_sha256=blocked["final_plan_sha256"],
            override_mode="corrected",
            finding_resolutions=finding_resolutions,
            override_reason="Free text alone must not mutate the lane plan.",
        )
    override = claude_core.override_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=successor_id,
        audit_plan_sha256=blocked["final_plan_sha256"],
        override_mode="corrected",
        finding_resolutions=finding_resolutions,
        override_reason=(
            "Opus adopts the audit's required correction without another dialogue."
        ),
        corrected_plans=corrected_plans,
    )
    assert override["status"] == "overridden"
    assert override["override_mode"] == "corrected"
    assert override["audit_plan_sha256"] == blocked["final_plan_sha256"]
    assert override["final_plan_sha256"] != blocked["final_plan_sha256"]
    accepted_plan_set, accepted_receipt = (
        claude_core._load_accepted_council_plan_set(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            council_id=successor_id,
            acceptance_sha256=override["acceptance_sha256"],
        )
    )
    assert accepted_receipt == {
        key: value for key, value in override.items() if key != "acceptance_sha256"
    }
    assert accepted_plan_set == claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=corrected_plans,
        root_session_id=root_session_id,
    )
    successor_state = state_root / "example" / "councils" / successor_id
    assert json.loads((successor_state / "final_plan.json").read_text())["plans"] == (
        merged_plans
    )
    assert json.loads((successor_state / "override_plan.json").read_text())[
        "plans"
    ] == corrected_plans
    repeated_override = claude_core.finalize_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=successor_id,
        final_plans=merged_plans,
        adjudications=adjudications,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert repeated_override["acceptance"]["status"] == "overridden"
    successor_pointer = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", root_session_id),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    assert successor_pointer["state"] == "accepted"
    assert successor_pointer["final_plan_sha256"] == override["final_plan_sha256"]
    final_status = claude_core.route_council_status(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    assert final_status["council"]["acceptance_status"] == "overridden"
    with claude_core.root_authority_guard(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    ):
        accepted_successor = claude_core._read_council_pointer(
            claude_core._council_pointer_path("example", root_session_id),
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
        )
        claude_core._update_council_pointer(
            accepted_successor,
            state="checkpointed",
            final_plan_sha256=override["final_plan_sha256"],
            acceptance_sha256=override["acceptance_sha256"],
            checkpoint_sha256=checkpoint_sha256,
        )
    checkpointed_successor = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", root_session_id),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
    )
    expected_corrected_cohort = claude_core._source_drift_expected_cohort(
        checkpointed_successor
    )
    assert expected_corrected_cohort == (
        "cohort_"
        + claude_core._cohort_identity_sha256(
            accepted_plan_set,
            council_id=successor_id,
            acceptance_sha256=override["acceptance_sha256"],
        )[:32],
        override["final_plan_sha256"],
    )
    corrected_cohort = claude_core.run_three_route_cohort(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        timeout_seconds=60,
        wait_seconds=0,
        codex_bin=Path(sys.executable),
        council_id=successor_id,
        council_receipt_sha256=override["acceptance_sha256"],
    )
    assert corrected_cohort["plan_sha256"] == override["final_plan_sha256"]
    corrected_cohort_plan = (
        state_root
        / "example"
        / corrected_cohort["cohort_id"]
        / f"plan_{override['final_plan_sha256']}.json"
    )
    assert json.loads(corrected_cohort_plan.read_text())["plans"] == corrected_plans


def test_declared_complete_reference_candidate_cannot_be_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem_id = "canary/sde-weighted-l1-vs-l2-clean"
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    generation_root = tmp_path / "generation"
    statement = (
        generation_root
        / "data"
        / "canary"
        / "sde-weighted-l1-vs-l2-clean.md"
    )
    statement.parent.mkdir(parents=True)
    statement.write_text("Prove the synthetic two-gate claim.\n", encoding="utf-8")
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "INPUT_ROOT", tmp_path / "inputs")
    digest = hashlib.sha256(statement.read_bytes()).hexdigest()
    candidate_id = "gpt_pro_two_gate_2026_08_30"
    claude_core.ingest_reference_candidate(
        problem_id=problem_id,
        statement_sha256=digest,
        candidate_id=candidate_id,
        target_claims=["Audit Gate A and Gate B."],
        content="Gate A establishes the local estimate. Gate B closes it.\n",
    )
    _prepare_root(
        problem_id=problem_id,
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": "4" * 64},
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )

    inventory = claude_core.reference_candidate_inventory(
        problem_id=problem_id, statement_sha256=digest
    )
    assert inventory["schema_version"] == (
        claude_core.REFERENCE_CANDIDATE_INVENTORY_SCHEMA
    )
    assert inventory["candidate_count"] == 1
    candidate_path = str(inventory["candidates"][0]["path"])
    candidate_source_path = f"{candidate_id}.md"
    assert inventory["candidates"][0]["candidate_id"] == candidate_id
    assert candidate_path.startswith(
        ".claude_core_inputs/reference_candidates/"
        f"{problem_id}/{digest}/{candidate_id}/"
    )
    marker = f"[reference_candidate:{candidate_id}]"
    bound_plans = _plans()
    bound_plans[0]["plan_summary"] = (
        f"Audit {marker} from {candidate_path} gate by gate and reject it at "
        "the first fatal estimate rather than substituting another proof."
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_council(**arguments: object) -> dict[str, object]:
        phase = str(arguments["phase"])
        request = arguments["request"]
        assert isinstance(request, dict)
        calls.append((phase, request))
        common = {
            "council_id": request["council_id"],
            "statement_sha256": digest,
        }
        if phase == "blind":
            report: dict[str, object] = {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                **common,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Stress every singular boundary layer."],
                "comparative_note": "Independent blind mechanisms.",
            }
        elif phase == "revision":
            merged = request["opus_merged_plans"]
            assert isinstance(merged, list)
            report = {
                "schema_version": claude_core.COUNCIL_SOL_REVISION_SCHEMA,
                **common,
                "merged_plan_sha256": request["merged_plan_sha256"],
                "plan_reviews": [
                    {
                        "plan_id": plan["plan_id"],
                        "verdict": "keep",
                        "objections": [],
                        "required_changes": [],
                        "replacement_plan": None,
                    }
                    for plan in merged
                ],
                "global_assessment": (
                    f"The route bound to {candidate_id} remains testable."
                ),
                "fanout_ready": True,
            }
        else:
            final = request["final_plans"]
            assert isinstance(final, list)
            report = {
                "schema_version": claude_core.COUNCIL_SOL_AUDIT_SCHEMA,
                **common,
                "final_plan_sha256": request["final_plan_sha256"],
                "decision": {
                    "verdict": "ready",
                    "plan_findings": [
                        {
                            "plan_id": plan["plan_id"],
                            "severity": "clear",
                            "finding": "No fatal route-design defect found.",
                            "required_change": None,
                        }
                        for plan in final
                    ],
                },
                "diversity_assessment": "The routes remain materially distinct.",
                "rationale": "Ready for bounded proof fanout.",
            }
        return {
            "status": "completed",
            "report": report,
            "elapsed_seconds": 1.0,
            "returncode": 1,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", fake_council)
    with pytest.raises(
        claude_core.CouncilContractError,
        match="must bind complete reference candidate",
    ):
        claude_core.start_route_council(
            problem_id=problem_id,
            statement_sha256=digest,
            root_session_id=root_session_id,
            opus_plans=_plans(),
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert calls == []

    duplicated_path_plans = [dict(plan) for plan in bound_plans]
    duplicated_path_plans[1]["plan_summary"] = (
        str(duplicated_path_plans[1]["plan_summary"])
        + f" Also inspect {candidate_path}."
    )
    with pytest.raises(
        claude_core.CouncilContractError,
        match="path_occurrences=2",
    ):
        claude_core.start_route_council(
            problem_id=problem_id,
            statement_sha256=digest,
            root_session_id=root_session_id,
            opus_plans=duplicated_path_plans,
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert calls == []

    blind = claude_core.start_route_council(
        problem_id=problem_id,
        statement_sha256=digest,
        root_session_id=root_session_id,
        opus_plans=bound_plans,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert [phase for phase, _request in calls] == ["blind"]
    assert "reference_candidates" not in calls[0][1]
    assert candidate_id not in claude_core.canonical_json(calls[0][1])
    council_id = str(blind["council_id"])

    with pytest.raises(
        claude_core.CouncilContractError,
        match="must bind complete reference candidate",
    ):
        claude_core.revise_route_council(
            problem_id=problem_id,
            statement_sha256=digest,
            root_session_id=root_session_id,
            council_id=council_id,
            merged_plans=_plans(),
            merge_rationale="Synthetic merge without the candidate route.",
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert [phase for phase, _request in calls] == ["blind"]

    claude_core.revise_route_council(
        problem_id=problem_id,
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=council_id,
        merged_plans=bound_plans,
        merge_rationale="Keep the exact candidate audit and independent routes.",
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    revision_request = calls[-1][1]
    bindings = revision_request["candidate_coverage"]["bindings"]
    assert bindings == [
        {
            "candidate_id": candidate_id,
            "plan_id": "route_1",
            "marker": marker,
            "path": candidate_path,
        }
    ]
    assert "Gate A" in revision_request["reference_candidates"]["candidates"][0][
        "content"
    ]
    assert revision_request["reference_candidates"]["schema_version"] == (
        claude_core.REFERENCE_CANDIDATE_PACKET_SCHEMA
    )
    assert revision_request["reference_candidates"]["candidates"][0][
        "source_path"
    ] == candidate_source_path

    adjudications = [
        {
            "draft_plan_id": plan["plan_id"],
            "final_plan_id": plan["plan_id"],
            "decision": "accepted",
            "rationale": "Keep the reviewed route unchanged.",
        }
        for plan in bound_plans
    ]
    with pytest.raises(
        claude_core.CouncilContractError,
        match="must bind complete reference candidate",
    ):
        claude_core.finalize_route_council(
            problem_id=problem_id,
            statement_sha256=digest,
            root_session_id=root_session_id,
            council_id=council_id,
            final_plans=_plans(),
            adjudications=adjudications,
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert [phase for phase, _request in calls] == ["blind", "revision"]

    final = claude_core.finalize_route_council(
        problem_id=problem_id,
        statement_sha256=digest,
        root_session_id=root_session_id,
        council_id=council_id,
        final_plans=bound_plans,
        adjudications=adjudications,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert final["acceptance"] is not None
    assert [phase for phase, _request in calls] == ["blind", "revision", "audit"]
    assert calls[-1][1]["candidate_coverage"]["bindings"][0]["plan_id"] == "route_1"
    assert calls[-1][1]["reference_candidates"]["candidates"][0][
        "candidate_id"
    ] == candidate_id


def test_accepted_candidate_packet_rejects_post_audit_drift_and_late_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Prove the candidate claim.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    references = data_root / "example.refs"
    references.mkdir()
    candidate_path = "complete-candidate.md"
    candidate_id = "complete_candidate"
    candidate = references / candidate_path
    candidate.write_text("First sealed proof candidate.\n", encoding="utf-8")

    def write_manifest() -> None:
        manifest = {
            "schema_version": claude_core.REFERENCE_CANDIDATE_MANIFEST_SCHEMA,
            "problem_id": "example",
            "statement_sha256": statement_sha256,
            "candidates": [
                {
                    "candidate_id": candidate_id,
                    "path": candidate_path,
                    "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    "status": "complete_unverified",
                    "target_claims": ["Audit the complete proof."],
                }
            ],
        }
        (references / claude_core.REFERENCE_CANDIDATE_MANIFEST_FILENAME).write_text(
            claude_core.canonical_json(manifest) + "\n", encoding="utf-8"
        )

    write_manifest()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(
        claude_core, "INPUT_ROOT", generation_root / ".claude_core_inputs"
    )
    packet = claude_core.reference_candidate_packet(
        problem_id="example", statement_sha256=statement_sha256
    )
    assert packet["schema_version"] == claude_core.REFERENCE_CANDIDATE_PACKET_SCHEMA
    projected_path = str(packet["candidates"][0]["path"])
    assert packet["candidates"][0]["source_path"] == candidate_path
    plans = _plans()
    marker = f"[reference_candidate:{candidate_id}]"
    plans[0]["plan_summary"] = f"Audit {marker} from {projected_path}."
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=statement_sha256,
        plans=plans,
        root_session_id="12345678-1234-4123-8123-123456789abc",
    )
    coverage = claude_core._reference_candidate_coverage(
        packet=packet, plan_set=plan_set, phase="final"
    )
    state_dir = tmp_path / "council"
    state_dir.mkdir()
    (state_dir / "reference_candidate_packet.json").write_text(
        claude_core.canonical_json(packet) + "\n", encoding="utf-8"
    )
    (state_dir / "final_candidate_coverage.json").write_text(
        claude_core.canonical_json(coverage) + "\n", encoding="utf-8"
    )
    assert (
        claude_core._validate_reference_candidate_council_artifacts(
            state_dir=state_dir,
            plan_set=plan_set,
            coverage_filename="final_candidate_coverage.json",
            phase="final",
        )
        == coverage
    )

    candidate.write_text("Replacement after the final audit.\n", encoding="utf-8")
    write_manifest()
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="manifest or source changed after the route-council audit",
    ):
        claude_core._validate_reference_candidate_council_artifacts(
            state_dir=state_dir,
            plan_set=plan_set,
            coverage_filename="final_candidate_coverage.json",
            phase="final",
        )

    historical_state_dir = tmp_path / "historical-council"
    historical_state_dir.mkdir()
    historical_plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=statement_sha256,
        plans=_plans(),
        root_session_id="12345678-1234-4123-8123-123456789abc",
    )
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="historical council predates the currently declared",
    ):
        claude_core._validate_reference_candidate_council_artifacts(
            state_dir=historical_state_dir,
            plan_set=historical_plan_set,
            coverage_filename="final_candidate_coverage.json",
            phase="final",
        )


def test_pro_gap_query_and_response_are_sha_bound_targeted_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert claude_core.MAX_PRO_GAP_RESPONSE_BYTES == 131_072
    assert claude_core.MAX_PRO_GAP_QUERIES_PER_STATEMENT == 16
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Prove the difficult claim.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "INPUT_ROOT", generation_root / ".claude_core_inputs"
    )
    fact_id = "mem_fact_1"
    failure_ids = ["mem_failed_path_1", "mem_failed_path_2"]
    entries = {
        "proof_steps": [
            {
                "record_id": fact_id,
                "effective_active": True,
                "channel": "proof_steps",
                "item": {"record": {"claim": "The marginal is Q."}},
            }
        ],
        "failed_paths": [
            {
                "record_id": failure_ids[0],
                "effective_active": True,
                "channel": "failed_paths",
                "item": {"record": {"mechanism": "fiberwise inverse"}},
            },
            {
                "record_id": failure_ids[1],
                "effective_active": True,
                "channel": "failed_paths",
                "item": {"record": {"mechanism": "dyadic localization"}},
            },
        ],
    }
    frontier = {
        "statement_sha256": statement_sha256,
        "frontier_sha256": "1" * 64,
        "memory_sha256": "2" * 64,
        "memory_record_count": 3,
    }

    class FakeLegacy:
        @staticmethod
        def _load_memory_entries(problem_id: str) -> dict[str, object]:
            assert problem_id == "example"
            return entries

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy())
    monkeypatch.setattr(claude_core, "_frontier", lambda _problem_id: frontier)
    question = (
        "Prove or refute the stated uniform resolvent estimate. Address the "
        "I comparable to gamma boundary layer and give every missing domain "
        "argument; do not merely repeat a fiberwise inverse estimate."
    )

    created = claude_core.prepare_pro_gap_query(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        target_claim="Uniform weighted resolvent estimate at I comparable to gamma.",
        settled_facts=["The I-marginal is the explicit invariant density Q."],
        verified_fact_or_proof_ids=[fact_id],
        failed_attempts=[
            "Fiberwise inversion leaves an uncontrolled radial derivative.",
            "Dyadic localization leaves non-absorbed interface fluxes.",
        ],
        failed_path_record_ids=failure_ids,
        boundary_checks=["Treat I <= C gamma without a uniform angular gap."],
        recommended_exact_question=question,
    )
    assert created["status"] == "created"
    assert created["external_relay_status"] == "self_contained_prompt_ready"
    prompt = created["copy_paste_prompt"]
    assert prompt != question
    assert question in prompt
    assert "Self-containedness requirement:" in prompt
    assert "no access to AxiomRelay memory" in prompt
    assert "The I-marginal is the explicit invariant density Q." in prompt
    assert "Fiberwise inversion leaves an uncontrolled radial derivative." in prompt
    assert "Dyadic localization leaves non-absorbed interface fluxes." in prompt
    assert "Treat I <= C gamma" in prompt
    assert "Host-certified binding:" not in prompt
    assert "problem_id:" not in prompt
    assert "gap_id:" not in prompt
    assert fact_id not in prompt
    assert all(record_id not in prompt for record_id in failure_ids)
    assert statement_sha256 not in prompt
    assert frontier["frontier_sha256"] not in prompt
    assert frontier["memory_sha256"] not in prompt
    assert created["source_context_sha256"] not in prompt
    assert created["source_context_attestation"].startswith("host_computed")
    assert created["source_context_sha256"] != "a" * 64
    query = claude_core.get_pro_gap_query(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        expected_query_sha256=created["query_sha256"],
    )
    assert query["query_sha256"] == created["query_sha256"]
    assert query["status"] == "waiting_owner_pro_response"
    assert query["external_relay_status"] == "self_contained_prompt_ready"
    assert len(query["failed_attempts"]) == 2
    assert query["schema_version"] == claude_core.PRO_GAP_QUERY_SCHEMA
    assert query["verified_fact_or_proof_ids"] == [fact_id]
    assert query["failed_path_record_ids"] == failure_ids
    for binding in (
        query["verified_fact_or_proof_records"] + query["failed_path_records"]
    ):
        assert binding["record_id"] not in prompt
        assert binding["item_sha256"] not in prompt
    with pytest.raises(claude_core.ClaudeCoreError, match="CAS digest mismatch"):
        claude_core.get_pro_gap_query(
            problem_id="example",
            statement_sha256=statement_sha256,
            gap_id="gap_uniform_resolvent",
            expected_query_sha256="a" * 64,
        )
    query_path = (
        tmp_path
        / "state"
        / "example"
        / claude_core.PRIVATE_PRO_GAP_QUERY_DIRECTORY
        / statement_sha256
        / "gap_uniform_resolvent.json"
    )
    assert stat.S_IMODE(query_path.stat().st_mode) == 0o600

    stored_packet = json.loads(query_path.read_text(encoding="utf-8"))
    legacy_packet = dict(stored_packet)
    legacy_packet["schema_version"] = claude_core.PRO_GAP_QUERY_SCHEMA_PREVIOUS
    legacy_packet["copy_paste_prompt"] = claude_core._compose_pro_gap_prompt_v2(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        source_context_sha256=legacy_packet["source_context_sha256"],
        target_claim=legacy_packet["target_claim"],
        settled_facts=legacy_packet["settled_facts"],
        verified_fact_or_proof_records=legacy_packet[
            "verified_fact_or_proof_records"
        ],
        failed_attempts=legacy_packet["failed_attempts"],
        failed_path_records=legacy_packet["failed_path_records"],
        boundary_checks=legacy_packet["boundary_checks"],
        ledger_head=legacy_packet["ledger_head"],
        recommended_exact_question=legacy_packet[
            "recommended_exact_question"
        ],
    )
    normalized_legacy = claude_core._validate_pro_gap_query_packet(
        legacy_packet,
        problem_id="example",
        statement_sha256=statement_sha256,
        expected_gap_id="gap_uniform_resolvent",
    )
    assert (
        normalized_legacy["schema_version"]
        == claude_core.PRO_GAP_QUERY_SCHEMA_PREVIOUS
    )
    current_raw = query_path.read_bytes()
    legacy_raw = (claude_core.canonical_json(legacy_packet) + "\n").encode(
        "utf-8"
    )
    try:
        query_path.write_bytes(legacy_raw)
        legacy_query = claude_core.get_pro_gap_query(
            problem_id="example",
            statement_sha256=statement_sha256,
            gap_id="gap_uniform_resolvent",
            expected_query_sha256=hashlib.sha256(legacy_raw).hexdigest(),
        )
        assert legacy_query["copy_paste_prompt"] is None
        assert (
            legacy_query["external_relay_status"]
            == "legacy_prompt_requires_new_gap_id"
        )
    finally:
        query_path.write_bytes(current_raw)

    replayed = claude_core.prepare_pro_gap_query(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        target_claim=query["target_claim"],
        settled_facts=query["settled_facts"],
        verified_fact_or_proof_ids=query["verified_fact_or_proof_ids"],
        failed_attempts=query["failed_attempts"],
        failed_path_record_ids=query["failed_path_record_ids"],
        boundary_checks=query["boundary_checks"],
        recommended_exact_question=question,
    )
    assert replayed["status"] == "existing"
    assert replayed["query_sha256"] == created["query_sha256"]

    monkeypatch.setattr(claude_core, "MAX_PRO_GAP_QUERIES_PER_STATEMENT", 1)
    with pytest.raises(claude_core.ClaudeCoreError, match="query count cap"):
        claude_core.prepare_pro_gap_query(
            problem_id="example",
            statement_sha256=statement_sha256,
            gap_id="gap_second_query",
            target_claim=query["target_claim"],
            settled_facts=query["settled_facts"],
            verified_fact_or_proof_ids=query["verified_fact_or_proof_ids"],
            failed_attempts=query["failed_attempts"],
            failed_path_record_ids=query["failed_path_record_ids"],
            boundary_checks=query["boundary_checks"],
            recommended_exact_question=question,
        )

    answer = "A targeted proof with an explicit boundary-layer estimate.\n"
    response = claude_core.ingest_pro_gap_response(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        query_sha256=created["query_sha256"],
        content=answer,
    )
    assert response["status"] == "created"
    assert response["status_after_ingest"] == "response_available"
    assert response["response_classification"] == (
        "complete_unverified_gap_delta"
    )
    loaded = claude_core.get_pro_gap_response(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        expected_query_sha256=created["query_sha256"],
        expected_response_sha256=response["response_sha256"],
    )
    assert loaded["content"] == answer
    assert loaded["query_sha256"] == created["query_sha256"]
    assert loaded["target_claim"] == query["target_claim"]
    updated_query = claude_core.get_pro_gap_query(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        expected_query_sha256=created["query_sha256"],
    )
    assert updated_query["status"] == "response_available"
    assert updated_query["response_sha256"] == response["response_sha256"]
    response_directory = (
        tmp_path
        / "state"
        / "example"
        / claude_core.PRIVATE_PRO_GAP_RESPONSE_DIRECTORY
        / statement_sha256
        / "gap_uniform_resolvent"
    )
    assert stat.S_IMODE(
        (response_directory / f"{response['response_id']}.md").stat().st_mode
    ) == 0o600

    # A late gap response is not a new complete route candidate, so accepting
    # it cannot invalidate an already completed route-council packet.
    packet = claude_core.reference_candidate_packet(
        problem_id="example", statement_sha256=statement_sha256
    )
    assert packet["manifest_present"] is False
    assert packet["candidates"] == []

    repeated_response = claude_core.ingest_pro_gap_response(
        problem_id="example",
        statement_sha256=statement_sha256,
        gap_id="gap_uniform_resolvent",
        query_sha256=created["query_sha256"],
        content=answer,
    )
    assert repeated_response["status"] == "existing"
    with pytest.raises(claude_core.ClaudeCoreError, match="CAS digest mismatch"):
        claude_core.ingest_pro_gap_response(
            problem_id="example",
            statement_sha256=statement_sha256,
            gap_id="gap_uniform_resolvent",
            query_sha256="b" * 64,
            content=answer,
        )
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="response id already names different content",
    ):
        claude_core.ingest_pro_gap_response(
            problem_id="example",
            statement_sha256=statement_sha256,
            gap_id="gap_uniform_resolvent",
            query_sha256=created["query_sha256"],
            content="A different answer.\n",
        )


def test_pro_gap_query_requires_two_distinct_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Prove the claim.\n", encoding="utf-8")
    digest = hashlib.sha256(statement.read_bytes()).hexdigest()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    with pytest.raises(claude_core.ClaudeCoreError, match="invalid cardinality"):
        claude_core.prepare_pro_gap_query(
            problem_id="example",
            statement_sha256=digest,
            gap_id="gap_one_failure",
            target_claim="Close the load-bearing estimate.",
            settled_facts=[],
            verified_fact_or_proof_ids=[],
            failed_attempts=["Only one failed route."],
            failed_path_record_ids=["mem_failed_1"],
            boundary_checks=[],
            recommended_exact_question="Please close the exact gap rigorously.",
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="duplicate-free"):
        claude_core.prepare_pro_gap_query(
            problem_id="example",
            statement_sha256=digest,
            gap_id="gap_duplicate_failure",
            target_claim="Close the load-bearing estimate.",
            settled_facts=[],
            verified_fact_or_proof_ids=[],
            failed_attempts=["Same failed route.", "Same failed route."],
            failed_path_record_ids=["mem_failed_1", "mem_failed_2"],
            boundary_checks=[],
            recommended_exact_question="Please close the exact gap rigorously.",
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="invalid cardinality"):
        claude_core.prepare_pro_gap_query(
            problem_id="example",
            statement_sha256=digest,
            gap_id="gap_missing_boundary",
            target_claim="Close the load-bearing estimate.",
            settled_facts=[],
            verified_fact_or_proof_ids=[],
            failed_attempts=["First failed route.", "Second failed route."],
            failed_path_record_ids=["mem_failed_1", "mem_failed_2"],
            boundary_checks=[],
            recommended_exact_question="Please close the exact gap rigorously.",
        )


def test_root_math_experiment_is_bounded_write_once_and_receipted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Investigate the finite model.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    calls_path = tmp_path / "sandbox-calls.jsonl"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"calls = pathlib.Path({str(calls_path)!r})\n"
        "with calls.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "separator = sys.argv.index('--')\n"
        "command = sys.argv[separator + 1:]\n"
        "os.execv(command[0], command)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    python_bin = Path(sys.executable).resolve(strict=True)
    python_sha256 = hashlib.sha256(python_bin.read_bytes()).hexdigest()
    host_source_sha256 = claude_core._host_source_sha256()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "PYTHON_BIN", python_bin)

    arguments = {
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "root_session_id": "12345678-1234-4123-8123-123456789abc",
        "experiment_id": "exp_sum_check",
        "purpose": "Check one exact arithmetic identity before route design.",
        "code": "import numpy as np\nprint(int(np.array([19, 23]).sum()))",
        "timeout_seconds": 10,
        "codex_bin": fake_codex,
        "expected_python_runtime_sha256": python_sha256,
        "expected_host_source_sha256": host_source_sha256,
    }
    created = claude_core.run_math_experiment(**arguments)
    assert created["status"] == "created"
    assert created["execution"]["execution_status"] == "completed"
    assert created["execution"]["stdout"] == "42\n"
    assert created["execution"]["stderr"] == ""
    assert created["execution"]["network_access"] == "disabled"
    assert created["execution"]["repository_access"] == "denied"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert len(calls) == 1
    call = calls[0]
    assert call[:3] == [
        "sandbox",
        "--permission-profile",
        "axiom-relay-root-math",
    ]
    assert "permissions.axiom-relay-root-math.network.enabled=false" in call
    filesystem = next(
        value
        for value in call
        if value.startswith(
            "permissions.axiom-relay-root-math.filesystem="
        )
    )
    assert str(claude_core.AGENTS_ROOT.parent.resolve()) in filesystem
    assert '="deny"' in filesystem
    assert str(python_bin) in filesystem

    replayed = claude_core.run_math_experiment(**arguments)
    assert replayed["status"] == "existing"
    assert replayed["result_sha256"] == created["result_sha256"]
    assert len(calls_path.read_text().splitlines()) == 1

    with pytest.raises(
        claude_core.ClaudeCoreError, match="durable artifact collision"
    ):
        claude_core.run_math_experiment(
            **{**arguments, "code": "print(43)"}
        )

    monkeypatch.setattr(claude_core, "MAX_MATH_EXPERIMENT_STDOUT_BYTES", 32)
    limited = claude_core.run_math_experiment(
        **{
            **arguments,
            "experiment_id": "exp_output_cap",
            "purpose": "Confirm bounded diagnostic output.",
            "code": "print('x' * 4096)",
        }
    )
    assert limited["execution"]["execution_status"] == "output_limit"
    assert limited["execution"]["stdout_truncated"] is True
    assert limited["execution"]["stdout_retained_bytes"] == 32

    timed_out = claude_core.run_math_experiment(
        **{
            **arguments,
            "experiment_id": "exp_timeout",
            "purpose": "Confirm the wall-clock limit.",
            "code": "import time\ntime.sleep(5)",
            "timeout_seconds": 1,
        }
    )
    assert timed_out["execution"]["execution_status"] == "timeout"

    artifact_root = (
        tmp_path
        / "state"
        / "example"
        / claude_core.PRIVATE_MATH_EXPERIMENT_DIRECTORY
        / statement_sha256
        / arguments["root_session_id"]
    )
    for filename in (
        "exp_sum_check.request.json",
        "exp_sum_check.result.json",
    ):
        assert stat.S_IMODE((artifact_root / filename).stat().st_mode) == 0o600


@pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"} or shutil.which("codex") is None,
    reason="Codex sandbox is unavailable on this test host",
)
def test_root_math_experiment_real_sandbox_denies_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Investigate the finite model.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    python_bin = Path(sys.executable).resolve(strict=True)
    python_sha256 = hashlib.sha256(python_bin.read_bytes()).hexdigest()
    codex_bin = Path(str(shutil.which("codex"))).resolve(strict=True)
    protected_path = Path(claude_core.__file__).resolve(strict=True)
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "PYTHON_BIN", python_bin)

    code = (
        "import pathlib, scipy\n"
        "print('scipy', scipy.__version__)\n"
        f"target = pathlib.Path({str(protected_path)!r})\n"
        "try:\n"
        "    target.read_text()\n"
        "except PermissionError:\n"
        "    print('repository-denied')\n"
        "else:\n"
        "    raise SystemExit('repository unexpectedly readable')\n"
    )
    result = claude_core.run_math_experiment(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id="12345678-1234-4123-8123-123456789abc",
        experiment_id="exp_real_sandbox",
        purpose="Verify the real math sandbox boundary.",
        code=code,
        timeout_seconds=20,
        codex_bin=codex_bin,
        expected_python_runtime_sha256=python_sha256,
        expected_host_source_sha256=claude_core._host_source_sha256(),
    )
    assert result["execution"]["execution_status"] == "completed"
    assert "scipy " in result["execution"]["stdout"]
    assert "repository-denied\n" in result["execution"]["stdout"]


def test_manual_reference_candidate_ingest_uses_private_inbox_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Prove the claim.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    legacy_references = data_root / "example.refs"
    legacy_references.mkdir()
    legacy_source = legacy_references / "legacy.md"
    legacy_source.write_text("Legacy candidate.\n", encoding="utf-8")
    legacy_manifest = {
        "schema_version": claude_core.REFERENCE_CANDIDATE_MANIFEST_SCHEMA,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "candidates": [
            {
                "candidate_id": "legacy_candidate",
                "path": "legacy.md",
                "sha256": hashlib.sha256(
                    legacy_source.read_bytes()
                ).hexdigest(),
                "status": "complete_unverified",
                "target_claims": ["Audit the legacy route."],
            }
        ],
    }
    (legacy_references / "candidate-manifest.json").write_text(
        claude_core.canonical_json(legacy_manifest) + "\n",
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    input_root = generation_root / ".claude_core_inputs"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    content = "Pasted GPT Pro candidate.\n"

    migrated = claude_core.ingest_reference_candidate(
        problem_id="example",
        statement_sha256=statement_sha256,
        candidate_id="legacy_candidate",
        target_claims=["Audit the legacy route."],
        content="Legacy candidate.\n",
    )
    assert migrated["status"] == "migrated"
    assert "target_claims" not in migrated
    assert "Audit the legacy route." not in claude_core.canonical_json(migrated)

    added = claude_core.ingest_reference_candidate(
        problem_id="example",
        statement_sha256=statement_sha256,
        candidate_id="manual_pro_candidate",
        target_claims=["Audit Gate A and Gate B."],
        content=content,
    )

    assert added["status"] == "added"
    assert added["schema_version"] == (
        claude_core.REFERENCE_CANDIDATE_INGEST_RECEIPT_SCHEMA
    )
    assert "target_claims" not in added
    assert "Audit Gate A and Gate B." not in claude_core.canonical_json(added)
    private_inbox = (
        state_root
        / "example"
        / claude_core.PRIVATE_REFERENCE_CANDIDATE_DIRECTORY
        / statement_sha256
    )
    assert (private_inbox / "manual_pro_candidate.md").read_text() == content
    assert (private_inbox / "legacy.md").read_text() == "Legacy candidate.\n"
    assert stat.S_IMODE(
        (private_inbox / "manual_pro_candidate.md").stat().st_mode
    ) == 0o600
    packet = claude_core.reference_candidate_packet(
        problem_id="example", statement_sha256=statement_sha256
    )
    assert packet["schema_version"] == claude_core.REFERENCE_CANDIDATE_PACKET_SCHEMA
    assert [candidate["candidate_id"] for candidate in packet["candidates"]] == [
        "legacy_candidate",
        "manual_pro_candidate",
    ]
    manual_candidate = packet["candidates"][1]
    assert set(manual_candidate) == {
        "candidate_id",
        "path",
        "source_path",
        "sha256",
        "status",
        "target_claims",
        "content",
    }
    assert manual_candidate["source_path"] == "manual_pro_candidate.md"
    assert manual_candidate["path"].startswith(
        ".claude_core_inputs/reference_candidates/"
        f"example/{statement_sha256}/manual_pro_candidate/"
    )
    projection = generation_root / str(manual_candidate["path"])
    assert projection.read_bytes() == content.encode("utf-8")
    assert stat.S_IMODE(projection.stat().st_mode) == 0o400
    assert hashlib.sha256(projection.read_bytes()).hexdigest() == manual_candidate[
        "sha256"
    ]
    assert not (legacy_references / "manual_pro_candidate.md").exists()

    replayed = claude_core.ingest_reference_candidate(
        problem_id="example",
        statement_sha256=statement_sha256,
        candidate_id="manual_pro_candidate",
        target_claims=["Audit Gate A and Gate B."],
        content=content,
    )
    assert replayed["status"] == "existing"
    assert "target_claims" not in replayed
    assert "Audit Gate A and Gate B." not in claude_core.canonical_json(replayed)
    assert replayed["candidate_packet_sha256"] == added[
        "candidate_packet_sha256"
    ]

    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="already names different content",
    ):
        claude_core.ingest_reference_candidate(
            problem_id="example",
            statement_sha256=statement_sha256,
            candidate_id="manual_pro_candidate",
            target_claims=["Audit Gate A and Gate B."],
            content="Changed candidate.\n",
        )


def test_private_reference_candidate_inbox_rotates_with_statement_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    first_statement = "First version of the claim.\n"
    second_statement = "Second version of the claim.\n"
    statement.write_text(first_statement, encoding="utf-8")
    first_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        claude_core, "INPUT_ROOT", generation_root / ".claude_core_inputs"
    )

    first = claude_core.ingest_reference_candidate(
        problem_id="example",
        statement_sha256=first_sha256,
        candidate_id="manual_pro_candidate",
        target_claims=["Audit the first statement."],
        content="Candidate for statement one.\n",
    )
    assert first["status"] == "added"

    statement.write_text(second_statement, encoding="utf-8")
    second_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    second = claude_core.ingest_reference_candidate(
        problem_id="example",
        statement_sha256=second_sha256,
        candidate_id="manual_pro_candidate",
        target_claims=["Audit the second statement."],
        content="Candidate for statement two.\n",
    )
    assert second["status"] == "added"

    private_root = (
        state_root
        / "example"
        / claude_core.PRIVATE_REFERENCE_CANDIDATE_DIRECTORY
    )
    first_bucket = private_root / first_sha256
    second_bucket = private_root / second_sha256
    assert (first_bucket / "manual_pro_candidate.md").read_text() == (
        "Candidate for statement one.\n"
    )
    assert (second_bucket / "manual_pro_candidate.md").read_text() == (
        "Candidate for statement two.\n"
    )
    second_packet = claude_core.reference_candidate_packet(
        problem_id="example", statement_sha256=second_sha256
    )
    assert second_packet["candidates"][0]["content"] == (
        "Candidate for statement two.\n"
    )
    assert f"/{second_sha256}/" in second_packet["candidates"][0]["path"]

    statement.write_text(first_statement, encoding="utf-8")
    restored_packet = claude_core.reference_candidate_packet(
        problem_id="example", statement_sha256=first_sha256
    )
    assert restored_packet["candidates"][0]["content"] == (
        "Candidate for statement one.\n"
    )
    assert f"/{first_sha256}/" in restored_packet["candidates"][0]["path"]
    assert (
        restored_packet["candidates"][0]["path"]
        != second_packet["candidates"][0]["path"]
    )


def test_private_reference_candidate_rejects_permission_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Prove the claim.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        claude_core, "INPUT_ROOT", generation_root / ".claude_core_inputs"
    )
    claude_core.ingest_reference_candidate(
        problem_id="example",
        statement_sha256=statement_sha256,
        candidate_id="manual_pro_candidate",
        target_claims=["Audit the complete candidate."],
        content="Private candidate.\n",
    )
    source = (
        state_root
        / "example"
        / claude_core.PRIVATE_REFERENCE_CANDIDATE_DIRECTORY
        / statement_sha256
        / "manual_pro_candidate.md"
    )
    source.chmod(0o644)
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="reference candidate source manual_pro_candidate is unsafe",
    ):
        claude_core.reference_candidate_packet(
            problem_id="example", statement_sha256=statement_sha256
        )


def test_reference_candidate_filename_collision_is_rejected_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Prove the claim.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    references = data_root / "example.refs"
    references.mkdir()
    colliding_name = "new_candidate.md"
    legacy_source = references / colliding_name
    legacy_source.write_text("Legacy candidate.\n", encoding="utf-8")
    legacy_manifest = {
        "schema_version": claude_core.REFERENCE_CANDIDATE_MANIFEST_SCHEMA,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "candidates": [
            {
                "candidate_id": "legacy_candidate",
                "path": colliding_name,
                "sha256": hashlib.sha256(legacy_source.read_bytes()).hexdigest(),
                "status": "complete_unverified",
                "target_claims": ["Audit the legacy candidate."],
            }
        ],
    }
    manifest_path = references / claude_core.REFERENCE_CANDIDATE_MANIFEST_FILENAME
    manifest_path.write_text(
        claude_core.canonical_json(legacy_manifest) + "\n", encoding="utf-8"
    )
    original_manifest = manifest_path.read_bytes()
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        claude_core, "INPUT_ROOT", generation_root / ".claude_core_inputs"
    )

    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="filename collides with an existing source path",
    ):
        claude_core.ingest_reference_candidate(
            problem_id="example",
            statement_sha256=statement_sha256,
            candidate_id="new_candidate",
            target_claims=["Audit the new candidate."],
            content="This must never be written.\n",
        )

    private_bucket = (
        state_root
        / "example"
        / claude_core.PRIVATE_REFERENCE_CANDIDATE_DIRECTORY
        / statement_sha256
    )
    assert not (private_bucket / colliding_name).exists()
    assert not (
        private_bucket / claude_core.REFERENCE_CANDIDATE_MANIFEST_FILENAME
    ).exists()
    assert legacy_source.read_text() == "Legacy candidate.\n"
    assert manifest_path.read_bytes() == original_manifest


def test_reference_candidate_claims_are_bounded_routing_metadata() -> None:
    maximum_count = [
        f"claim {index}" for index in range(claude_core.MAX_REFERENCE_CANDIDATE_CLAIMS)
    ]
    assert claude_core._reference_candidate_claims(
        maximum_count, label="claims"
    ) == maximum_count

    rejected = [
        ([], "invalid cardinality"),
        (
            [
                f"claim {index}"
                for index in range(
                    claude_core.MAX_REFERENCE_CANDIDATE_CLAIMS + 1
                )
            ],
            "invalid cardinality",
        ),
        (["duplicate", "duplicate"], "duplicate-free"),
        (["line one\nline two"], "short single-line routing metadata"),
        (
            ["x" * (claude_core.MAX_REFERENCE_CANDIDATE_CLAIM_CHARS + 1)],
            f"exceeds {claude_core.MAX_REFERENCE_CANDIDATE_CLAIM_CHARS}",
        ),
        (
            [f"{index}-" + "x" * 410 for index in range(5)],
            "short single-line routing metadata",
        ),
    ]
    for claims, error in rejected:
        with pytest.raises(claude_core.ClaudeCoreError, match=error):
            claude_core._reference_candidate_claims(claims, label="claims")


def test_reference_candidate_packet_enforces_combined_council_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    statement = data_root / "example.md"
    statement.write_text("Prove the bounded claim.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    references = data_root / "example.refs"
    references.mkdir()
    candidate = references / "candidate.md"
    candidate.write_text("Complete candidate.\n", encoding="utf-8")
    manifest = {
        "schema_version": claude_core.REFERENCE_CANDIDATE_MANIFEST_SCHEMA,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "candidates": [
            {
                "candidate_id": "complete_candidate",
                "path": candidate.name,
                "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "status": "complete_unverified",
                "target_claims": ["Audit the complete candidate."],
            }
        ],
    }
    (references / claude_core.REFERENCE_CANDIDATE_MANIFEST_FILENAME).write_text(
        claude_core.canonical_json(manifest) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(
        claude_core, "INPUT_ROOT", generation_root / ".claude_core_inputs"
    )
    packet = claude_core.reference_candidate_packet(
        problem_id="example", statement_sha256=statement_sha256
    )
    statement_json_bytes = len(
        claude_core.canonical_json(
            {"problem_statement": statement.read_text(encoding="utf-8")}
        ).encode("utf-8")
    )
    packet_bytes = len(
        (claude_core.canonical_json(packet) + "\n").encode("utf-8")
    )
    combined_cap = (
        statement_json_bytes
        + packet_bytes
        + 3 * claude_core.MAX_PLAN_BYTES
        + claude_core.MAX_REFERENCE_CANDIDATE_COUNCIL_RESERVE_BYTES
    )

    monkeypatch.setattr(claude_core, "MAX_COUNCIL_REQUEST_BYTES", combined_cap)
    assert (
        claude_core.reference_candidate_packet(
            problem_id="example", statement_sha256=statement_sha256
        )
        == packet
    )
    monkeypatch.setattr(
        claude_core, "MAX_COUNCIL_REQUEST_BYTES", combined_cap - 1
    )
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="cannot fit the bounded council request",
    ):
        claude_core.reference_candidate_packet(
            problem_id="example", statement_sha256=statement_sha256
        )


def test_council_override_resolution_modes_are_explicit_and_fail_closed() -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    audited = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    audit_report = {
        "schema_version": claude_core.COUNCIL_SOL_AUDIT_SCHEMA,
        "decision": {
            "verdict": "blocked",
            "plan_findings": [
                {
                    "plan_id": plan["plan_id"],
                    "severity": "fatal" if index == 0 else "clear",
                    "finding": "synthetic finding",
                    "required_change": "repair route" if index == 0 else None,
                }
                for index, plan in enumerate(audited["plans"])
            ],
        },
    }
    rejected = [
        {
            "plan_id": audited["plans"][0]["plan_id"],
            "disposition": "rejected",
            "rationale": "The fatal finding is inapplicable for a stated reason.",
        }
    ]
    assert claude_core._validate_council_override_resolutions(
        rejected,
        audit_report=audit_report,
        audited_plan_set=audited,
        accepted_plan_set=audited,
        override_mode="unchanged",
    ) == rejected

    corrected_plans = json.loads(json.dumps(audited["plans"]))
    corrected_plans[0]["plan_summary"] = "a corrected executable summary"
    corrected = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=corrected_plans,
        root_session_id=root_session_id,
    )
    corrected_resolution = [
        {
            "plan_id": audited["plans"][0]["plan_id"],
            "disposition": "corrected",
            "rationale": "The executable route now contains the required repair.",
        }
    ]
    assert claude_core._validate_council_override_resolutions(
        corrected_resolution,
        audit_report=audit_report,
        audited_plan_set=audited,
        accepted_plan_set=corrected,
        override_mode="corrected",
    ) == corrected_resolution
    with pytest.raises(claude_core.ClaudeCoreError, match="byte-exact"):
        claude_core._validate_council_override_resolutions(
            corrected_resolution,
            audit_report=audit_report,
            audited_plan_set=audited,
            accepted_plan_set=corrected,
            override_mode="unchanged",
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="resolve every fatal"):
        claude_core._validate_council_override_resolutions(
            [],
            audit_report=audit_report,
            audited_plan_set=audited,
            accepted_plan_set=audited,
            override_mode="unchanged",
        )


def test_failed_council_takeover_inherits_round_and_failure_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    first_root = "12345678-1234-4123-8123-123456789abc"
    second_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", tmp_path / "inputs")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_frontier", lambda _problem_id: {"frontier_sha256": "4" * 64}
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    valid = [False]

    def fake_council(**arguments: object) -> dict[str, object]:
        request = arguments["request"]
        assert isinstance(request, dict)
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": (
                    request["council_id"]
                    if valid[0]
                    else "council_" + "f" * 32
                ),
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "event_stream_sha256": "6" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", fake_council)
    failed = claude_core.start_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=first_root,
        opus_plans=_plans(),
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert failed["status"] == "operational_blocked"
    first_pointer_path = claude_core._council_pointer_path("example", first_root)
    first_pointer_sha256 = hashlib.sha256(first_pointer_path.read_bytes()).hexdigest()
    failed_receipt_path = (
        state_root
        / "example"
        / "councils"
        / str(failed["council_id"])
        / "blind_receipt.json"
    )
    failed_receipt_sha256 = hashlib.sha256(
        failed_receipt_path.read_bytes()
    ).hexdigest()
    with pytest.raises(
        claude_core.CouncilContractError,
        match="start_route_council cannot continue.*operational_blocked",
    ) as same_root_failure:
        claude_core.start_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=first_root,
            opus_plans=_plans(),
            prior_failure_context="This must not replay the paid blind phase.",
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert same_root_failure.value.retry_allowed is False
    assert "fresh successor root" in str(
        same_root_failure.value.repair_hint
    )

    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=first_root,
    )
    valid[0] = True
    successor = claude_core.start_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
        opus_plans=_plans(),
        prior_failure_context="The predecessor audit transport was malformed.",
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert successor["status"] == "completed"
    assert successor["council_round"] == 2
    assert successor["prior_failure_receipt_sha256"] == failed_receipt_sha256
    pointer = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", second_root),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
    )
    assert pointer["predecessor_root_session_id"] == first_root
    assert pointer["predecessor_council_id"] == failed["council_id"]
    assert pointer["predecessor_pointer_sha256"] == first_pointer_sha256
    stale_pointer = dict(pointer)
    with claude_core.root_authority_guard(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
    ):
        claude_core._update_council_pointer(
            pointer, state="operational_blocked"
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="stale"):
        claude_core._update_council_pointer(
            stale_pointer, state="revision_complete"
        )
    current = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", second_root),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=second_root,
    )
    assert current["state"] == "operational_blocked"
    with pytest.raises(claude_core.ClaudeCoreError, match="not monotonic"):
        claude_core._update_council_pointer(current, state="blind_complete")


def test_council_lineage_crosses_intermediate_root_without_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_a = "12345678-1234-4123-8123-123456789abc"
    root_b = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    root_c = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_a,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "a" * 32
    pointer = {
        "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
        "pointer_version": 1,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_a,
        "council_round": 1,
        "council_id": council_id,
        "base_frontier_sha256": "1" * 64,
        "opus_plan_sha256": "2" * 64,
        "prior_context_sha256": "3" * 64,
        "prior_failure_receipt_sha256": None,
        "host_source_sha256": claude_core._host_source_sha256(),
        "predecessor_root_session_id": None,
        "predecessor_council_id": None,
        "predecessor_pointer_sha256": None,
        "state": "operational_blocked",
        "final_plan_sha256": None,
        "acceptance_sha256": None,
        "checkpoint_sha256": None,
        "cohort_id": None,
        "updated_at_unix": time.time(),
    }
    claude_core._write_once(
        claude_core._council_pointer_path("example", root_a),
        pointer,
        mode=0o400,
    )
    council_dir = claude_core._council_dir("example", council_id)
    request_path = council_dir / "blind_request.json"
    claude_core._write_once(request_path, {"synthetic": True}, mode=0o400)
    failed_receipt = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_a,
        "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
        "status": "operational_blocked",
        "retry_allowed": False,
    }
    failed_receipt_sha256 = claude_core._write_once(
        council_dir / "blind_receipt.json", failed_receipt, mode=0o400
    )

    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_b,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=root_a,
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_c,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=root_b,
    )
    monkeypatch.setattr(
        claude_core, "_frontier", lambda _problem_id: {"frontier_sha256": "4" * 64}
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )

    def valid_blind(**arguments: object) -> dict[str, object]:
        request = arguments["request"]
        assert isinstance(request, dict)
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": request["council_id"],
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", valid_blind)
    successor = claude_core.start_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_c,
        opus_plans=_plans(),
        prior_failure_context="The earlier council ended before a valid report.",
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert successor["council_round"] == 2
    assert successor["prior_failure_receipt_sha256"] == failed_receipt_sha256
    successor_pointer = claude_core._read_council_pointer(
        claude_core._council_pointer_path("example", root_c),
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_c,
    )
    assert successor_pointer["predecessor_root_session_id"] == root_a
    assert successor_pointer["predecessor_council_id"] == council_id


def test_route_council_accepts_disabled_report_and_rejects_tool_events() -> None:
    report = {"synthetic": "bounded report"}
    events = [
        {"type": "thread.started", "thread_id": "synthetic-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": claude_core.canonical_json(report),
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1200,
                "cached_input_tokens": 800,
                "output_tokens": 300,
            },
        },
    ]
    raw = b"".join(
        (claude_core.canonical_json(event) + "\n").encode() for event in events
    )
    profile = claude_core._council_retrieval_profile(
        problem_id="example", statement_sha256=_statement_digest()
    )
    trace, observed = claude_core._parse_council_events(
        raw, retrieval_profile=profile
    )
    assert observed == report
    assert trace["tool_free"] is True
    assert trace["token_usage_observed"] is True
    assert trace["token_usage"] == {
        "input_tokens": 1200,
        "cached_input_tokens": 800,
        "output_tokens": 300,
        "total_tokens": 1500,
    }
    assert trace["token_usage_finality"] == "codex_cli_turn_completed"

    forbidden = list(events)
    forbidden.insert(
        2,
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "true"},
        },
    )
    forbidden_raw = b"".join(
        (claude_core.canonical_json(event) + "\n").encode()
        for event in forbidden
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="forbidden"):
        claude_core._parse_council_events(
            forbidden_raw, retrieval_profile=profile
        )


def test_route_council_accepts_bounded_error_events_only_with_success_terminal(
) -> None:
    report = {"synthetic": "report after a recoverable stream error"}
    events = [
        {
            "type": "error",
            "message": "synthetic recoverable prelude",
            "will_retry": True,
        },
        {"type": "thread.started", "thread_id": "synthetic-thread"},
        {"type": "turn.started"},
        {
            "type": "error",
            "message": "synthetic recoverable in-turn event",
            "will_retry": True,
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": claude_core.canonical_json(report),
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 21,
                "cached_input_tokens": 8,
                "output_tokens": 13,
            },
        },
    ]
    raw_lines = [
        (claude_core.canonical_json(event) + "\n").encode()
        for event in events
    ]
    profile = claude_core._council_retrieval_profile(
        problem_id="example", statement_sha256=_statement_digest()
    )
    trace, observed = claude_core._parse_council_events(
        b"".join(raw_lines), retrieval_profile=profile
    )
    assert observed == report
    assert trace["recoverable_error_event_count"] == 2
    assert trace["recoverable_error_event_sha256s"] == [
        hashlib.sha256(raw_lines[0].rstrip(b"\n")).hexdigest(),
        hashlib.sha256(raw_lines[3].rstrip(b"\n")).hexdigest(),
    ]
    assert trace["token_usage"] == {
        "input_tokens": 21,
        "cached_input_tokens": 8,
        "output_tokens": 13,
        "total_tokens": 34,
    }

    with pytest.raises(
        claude_core.CouncilEventStreamError,
        match="failed terminal event",
    ):
        claude_core._parse_council_events(
            b"".join(
                raw_lines[:3]
                + [(claude_core.canonical_json({"type": "turn.failed"}) + "\n").encode()]
            ),
            retrieval_profile=profile,
        )

    postlude_trace, postlude_report = claude_core._parse_council_events(
        b"".join(raw_lines + [raw_lines[3]]), retrieval_profile=profile
    )
    assert postlude_report == report
    assert postlude_trace["recoverable_error_event_count"] == 3

    with pytest.raises(claude_core.ClaudeCoreError, match="after terminal"):
        claude_core._parse_council_events(
            b"".join(raw_lines + [raw_lines[4]]), retrieval_profile=profile
        )

    hidden_item = {
        "type": "error",
        "message": "not a valid diagnostic-only event",
        "item": {"type": "command_execution", "command": "true"},
    }
    with pytest.raises(
        claude_core.CouncilEventStreamError,
        match="carried an item",
    ):
        claude_core._parse_council_events(
            b"".join(
                [raw_lines[1], raw_lines[2]]
                + [(claude_core.canonical_json(hidden_item) + "\n").encode()]
                + raw_lines[4:]
            ),
            retrieval_profile=profile,
        )

    too_many_errors = [raw_lines[1], raw_lines[2]] + [
        raw_lines[3]
        for _index in range(
            claude_core.MAX_COUNCIL_RECOVERABLE_ERROR_EVENTS + 1
        )
    ]
    with pytest.raises(
        claude_core.CouncilEventStreamError,
        match="too many top-level error events",
    ):
        claude_core._parse_council_events(
            b"".join(too_many_errors + raw_lines[4:]),
            retrieval_profile=profile,
        )

    oversized_error = (
        claude_core.canonical_json(
            {
                "type": "error",
                "message": "x"
                * claude_core.MAX_COUNCIL_RECOVERABLE_ERROR_EVENT_BYTES,
            }
        )
        + "\n"
    ).encode()
    with pytest.raises(
        claude_core.CouncilEventStreamError,
        match="top-level error exceeds its byte cap",
    ):
        claude_core._parse_council_events(
            b"".join([raw_lines[1], raw_lines[2], oversized_error] + raw_lines[4:]),
            retrieval_profile=profile,
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"key":1,"key":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":"\\ud800"}',
        b'\xff',
    ],
)
def test_strict_json_rejects_ambiguous_or_non_utf8_values(raw: bytes) -> None:
    with pytest.raises(claude_core.ClaudeCoreError, match="strict JSON"):
        claude_core._strict_json_loads(raw, label="synthetic value")


def test_strict_json_rejects_excessive_nesting_as_controlled_error() -> None:
    raw = ("[" * 2000 + "0" + "]" * 2000).encode()
    with pytest.raises(claude_core.ClaudeCoreError, match="strict JSON"):
        claude_core._strict_json_loads(raw, label="deep synthetic value")


def test_write_once_does_not_publish_a_partial_final_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.json"

    def fail_publication(*_arguments: object, **_keywords: object) -> None:
        raise OSError("synthetic failure before atomic publication")

    monkeypatch.setattr(claude_core.os, "link", fail_publication)
    with pytest.raises(OSError, match="atomic publication"):
        claude_core._write_once(target, {"complete": True}, mode=0o400)
    assert not target.exists()
    assert list(tmp_path.glob(".artifact.json.write-once-*")) == []


def test_write_once_reader_recovers_only_its_same_inode_publication_alias(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.json"
    temporary = tmp_path / ".artifact.json.write-once-999-synthetic"
    encoded = (claude_core.canonical_json({"complete": True}) + "\n").encode()
    temporary.write_bytes(encoded)
    os.link(temporary, target)
    assert target.stat().st_nlink == 2

    observed = claude_core._read_canonical_object(
        target, label="synthetic atomic artifact"
    )
    assert observed == {"complete": True}
    assert not temporary.exists()
    assert target.stat().st_nlink == 1


@pytest.mark.parametrize("collision", ["different_inode", "unknown_hardlink"])
def test_manifest_alias_recovery_rejects_nonpublication_hardlinks(
    tmp_path: Path, collision: str
) -> None:
    target = tmp_path / "artifact.json"
    target.write_text("durable\n", encoding="utf-8")
    if collision == "different_inode":
        alias = tmp_path / (
            ".artifact.json.write-once-999-" + "a" * 24
        )
        alias.write_text("different\n", encoding="utf-8")
        expected = "differs from its sibling"
    else:
        alias = tmp_path / "unrecognized-hardlink"
        os.link(target, alias)
        expected = "artifact is unsafe"

    with pytest.raises(claude_core.ClaudeCoreError, match=expected):
        claude_core._stable_council_artifact_manifest(tmp_path)


def test_manifest_alias_recovery_rejects_malformed_write_once_name(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / ".artifact.json.write-once-synthetic"
    malformed.write_text("temporary\n", encoding="utf-8")
    with pytest.raises(claude_core.ClaudeCoreError, match="alias is unsafe"):
        claude_core._stable_council_artifact_manifest(tmp_path)


def test_write_once_writer_tolerates_reader_cleaning_publication_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.json"
    linked = threading.Event()
    reader_finished = threading.Event()
    real_link = os.link

    def pause_after_link(*arguments: object, **keywords: object) -> None:
        real_link(*arguments, **keywords)  # type: ignore[arg-type]
        linked.set()
        assert reader_finished.wait(timeout=5)

    monkeypatch.setattr(claude_core.os, "link", pause_after_link)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(
            claude_core._write_once, target, {"complete": True}, mode=0o400
        )
        assert linked.wait(timeout=5)
        assert claude_core._read_canonical_object(
            target, label="concurrent atomic artifact"
        ) == {"complete": True}
        reader_finished.set()
        writer.result(timeout=5)
    assert target.stat().st_nlink == 1


def test_write_once_at_writer_tolerates_reader_cleaning_publication_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = threading.Event()
    reader_finished = threading.Event()
    real_link = os.link

    def pause_after_link(*arguments: object, **keywords: object) -> None:
        real_link(*arguments, **keywords)  # type: ignore[arg-type]
        linked.set()
        assert reader_finished.wait(timeout=5)

    monkeypatch.setattr(claude_core.os, "link", pause_after_link)
    directory_descriptor = os.open(
        tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            writer = executor.submit(
                claude_core._write_bytes_once_at,
                directory_descriptor,
                "artifact.bin",
                b"complete",
                mode=0o400,
                label="concurrent relative artifact",
            )
            assert linked.wait(timeout=5)
            assert claude_core._read_regular_bytes_at(
                directory_descriptor,
                "artifact.bin",
                label="concurrent relative artifact",
                maximum_bytes=8,
            ) == b"complete"
            reader_finished.set()
            writer.result(timeout=5)
    finally:
        os.close(directory_descriptor)
    assert (tmp_path / "artifact.bin").stat().st_nlink == 1


@pytest.mark.skipif(
    not hasattr(os, "fork") or not Path("/proc/self/stat").is_file(),
    reason="zombie detection uses Linux procfs",
)
def test_pid_liveness_treats_an_unreaped_zombie_as_stopped() -> None:
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    try:
        deadline = time.monotonic() + 2
        while claude_core._pid_is_live(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert claude_core._pid_is_live(pid) is False
    finally:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def test_darwin_process_identity_uses_native_start_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_core.sys, "platform", "darwin")
    monkeypatch.setattr(
        claude_core,
        "_darwin_process_info",
        lambda pid: (pid, 41, 1_725_000_000, 123_456),
    )
    expected = claude_core.sha256_bytes(
        claude_core.canonical_json(
            {
                "platform": "darwin",
                "pid": 123,
                "start_seconds": 1_725_000_000,
                "start_microseconds": 123_456,
            }
        ).encode("utf-8")
    )

    assert claude_core._process_identity_token(123) == expected


def test_darwin_parent_fence_requests_userspace_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_token = "a" * 64
    monkeypatch.setattr(claude_core.sys, "platform", "darwin")
    monkeypatch.setattr(claude_core.os, "getppid", lambda: 123)
    monkeypatch.setattr(
        claude_core,
        "_process_identity_token",
        lambda pid: owner_token if pid == 123 else None,
    )

    assert claude_core._arm_parent_death_signal(123, owner_token) is True


@pytest.mark.skipif(
    not hasattr(os, "fork") or not sys.platform.startswith("linux"),
    reason="owned-command parent-death fencing uses Linux prctl",
)
def test_owned_command_group_dies_with_its_worker() -> None:
    read_descriptor, write_descriptor = os.pipe()
    worker_pid = os.fork()
    if worker_pid == 0:
        os.close(read_descriptor)
        wrapper = subprocess.Popen(
            [
                str(claude_core.PYTHON_BIN),
                "-I",
                "-B",
                str(Path(claude_core.__file__).resolve()),
                "--execute-owned-command",
                str(os.getpid()),
                "/bin/sleep",
                "30",
            ],
            start_new_session=True,
        )
        os.write(write_descriptor, f"{wrapper.pid}\n".encode("ascii"))
        os.close(write_descriptor)
        signal.pause()
        os._exit(1)
    os.close(write_descriptor)
    wrapper_pid = int(os.read(read_descriptor, 64).strip())
    os.close(read_descriptor)
    try:
        deadline = time.monotonic() + 3
        while (
            not claude_core._process_group_has_non_zombie_members(wrapper_pid)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert claude_core._process_group_has_non_zombie_members(wrapper_pid)
        os.kill(worker_pid, signal.SIGKILL)
        os.waitpid(worker_pid, 0)
        deadline = time.monotonic() + 5
        while (
            claude_core._process_group_has_non_zombie_members(wrapper_pid)
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert not claude_core._process_group_has_non_zombie_members(wrapper_pid)
    finally:
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(worker_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            claude_core._terminate_orphan_process_group(wrapper_pid)
        except (ProcessLookupError, claude_core.ClaudeCoreError):
            pass


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="owned-wrapper identity matching uses Linux procfs",
)
def test_owned_wrapper_cleanup_requires_parent_start_identity() -> None:
    owner_token = claude_core._process_identity_token(os.getpid())
    assert isinstance(owner_token, str)
    wrapper = subprocess.Popen(
        [
            str(claude_core.PYTHON_BIN),
            "-I",
            "-B",
            str(Path(claude_core.__file__).resolve()),
            "--execute-owned-command",
            str(os.getpid()),
            "--owner-start-token",
            owner_token,
            "/bin/sleep",
            "30",
        ],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while (
            not claude_core._process_group_has_non_zombie_members(wrapper.pid)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert claude_core._process_group_has_non_zombie_members(wrapper.pid)
        claude_core._terminate_owned_command_wrappers(
            os.getpid(), "0" * 64
        )
        assert claude_core._process_group_has_non_zombie_members(wrapper.pid)
        claude_core._terminate_owned_command_wrappers(
            os.getpid(), owner_token
        )
        wrapper.wait(timeout=5)
        assert not claude_core._process_group_has_non_zombie_members(wrapper.pid)
    finally:
        try:
            claude_core._terminate_orphan_process_group(wrapper.pid)
        except (ProcessLookupError, claude_core.ClaudeCoreError):
            pass


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_council_pipe_cleanup_reaps_residual_group_after_leader_exit(
    tmp_path: Path,
) -> None:
    child_ready = tmp_path / "child.ready"
    child_stopped = tmp_path / "child.stopped"
    child_code = "\n".join(
        [
            "import pathlib, signal, sys, time",
            "def stop(*_args):",
            "    pathlib.Path(sys.argv[2]).write_text('stopped')",
            "    raise SystemExit(0)",
            "signal.signal(signal.SIGTERM, stop)",
            "pathlib.Path(sys.argv[1]).write_text('ready')",
            "while True: time.sleep(1)",
        ]
    )
    leader_code = "\n".join(
        [
            "import pathlib, subprocess, sys, time",
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])",
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)",
        ]
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            leader_code,
            child_code,
            str(child_ready),
            str(child_stopped),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _stdout, _stderr, out_cap, err_cap, timed_out, drain_incomplete = (
            claude_core._communicate_council_bounded(
                process,
                stdin_bytes=b"",
                timeout_seconds=5,
            )
        )
        assert process.returncode == 0
        assert (out_cap, err_cap, timed_out, drain_incomplete) == (
            False,
            False,
            False,
            False,
        )
        assert child_stopped.read_text(encoding="utf-8") == "stopped"
        assert not claude_core._process_group_has_non_zombie_members(process.pid)
    finally:
        try:
            claude_core._terminate_orphan_process_group(process.pid)
        except (ProcessLookupError, claude_core.ClaudeCoreError):
            pass


def test_council_stream_progress_projects_structure_without_message_text() -> None:
    events = [
        {"type": "thread.started", "thread_id": "secret-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "private mathematical reasoning",
            },
        },
        {"type": "turn.completed", "usage": {}},
    ]
    program = (
        "import json,sys\n"
        f"events={events!r}\n"
        "for event in events:\n"
        " sys.stdout.write(json.dumps(event,separators=(',',':'))+'\\n')\n"
        " sys.stdout.flush()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    progress: list[dict[str, object]] = []
    stdout, _stderr, *_flags = claude_core._communicate_council_bounded(
        process,
        stdin_bytes=b"",
        timeout_seconds=5,
        progress_callback=progress.append,
    )
    assert b"private mathematical reasoning" in stdout
    assert [item["event_type"] for item in progress] == [
        "thread.started",
        "turn.started",
        "item.completed",
        "turn.completed",
    ]
    assert progress[2]["item_type"] == "agent_message"
    assert "private mathematical reasoning" not in claude_core.canonical_json(
        progress
    )


def test_council_stream_progress_emits_content_free_liveness_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_core, "COUNCIL_PROGRESS_HEARTBEAT_SECONDS", 0.02)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    progress: list[dict[str, object]] = []
    claude_core._communicate_council_bounded(
        process,
        stdin_bytes=b"",
        timeout_seconds=5,
        progress_callback=progress.append,
    )
    assert any(
        item["event_type"] == "process.heartbeat" for item in progress
    )
    assert all("text" not in item for item in progress)


def test_digest_bound_owned_command_rejects_atomic_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "owned-command"
    replacement = tmp_path / "replacement"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    replacement.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    executable.chmod(0o700)
    replacement.chmod(0o700)
    expected_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    real_trusted = claude_core._trusted_executable

    def replace_after_path_check(path: Path, *, label: str) -> Path:
        resolved = real_trusted(path, label=label)
        os.replace(replacement, executable)
        return resolved

    monkeypatch.setattr(
        claude_core, "_trusted_executable", replace_after_path_check
    )
    monkeypatch.setattr(claude_core.os, "getpgrp", claude_core.os.getpid)
    with pytest.raises(claude_core.ClaudeCoreError, match="digest changed"):
        claude_core._execute_owned_command(
            expected_parent_pid=os.getppid(),
            command=[str(executable)],
            expected_executable_sha256=expected_sha256,
        )


def test_host_worker_executes_pinned_source_after_atomic_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "claude_core.py"
    replacement = tmp_path / "replacement.py"
    marker = tmp_path / "marker.txt"
    source.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('authenticated')\n",
        encoding="utf-8",
    )
    replacement.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('replacement')\n",
        encoding="utf-8",
    )
    expected_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(claude_core, "__file__", str(source))

    with claude_core._pinned_host_source_command(
        arguments=[str(marker)],
        expected_source_sha256=expected_sha256,
    ) as (command, source_descriptor):
        os.replace(replacement, source)
        completed = subprocess.run(
            command,
            pass_fds=(source_descriptor,),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert marker.read_text(encoding="utf-8") == "authenticated"


def test_digest_bound_real_runner_preserves_its_authenticated_source_root() -> None:
    runner = claude_core.RUNNER.resolve(strict=True)
    source = Path(claude_core.__file__).resolve(strict=True)
    python_bin = Path(sys.executable).resolve(strict=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "HOME", "LANG", "LC_ALL", "PATH", "USER", "LOGNAME",
            "SHELL", "TMPDIR", "CODEX_HOME",
        }
    }
    environment.update(
        {
            "RETHLAS_RUN_MODE": "core",
            "RETHLAS_MAIN_AGENT": "gpt-astra",
            "RETHLAS_MODEL_POLICY_PROFILE": "max_diversity",
            "RETHLAS_GENERATION_PYTHON_BIN": str(python_bin),
            "RETHLAS_COHORT_RUNNER_CLOSURE_SHA256": (
                claude_core._cohort_runner_closure_sha256(runner)
            ),
            "PROBLEM_FILE": "data/owned-wrapper-missing.md",
        }
    )
    completed = subprocess.run(
        [
            str(python_bin),
            "-I",
            "-B",
            str(source),
            "--execute-owned-command",
            str(os.getpid()),
            "--expected-sha256",
            hashlib.sha256(runner.read_bytes()).hexdigest(),
            str(runner),
        ],
        cwd=str(claude_core.AGENTS_ROOT),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        timeout=30,
        check=False,
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    assert completed.returncode == 1
    assert "Unsafe problem file:" in output
    assert str(claude_core.GENERATION_ROOT / "data" / "owned-wrapper-missing.md") in output
    assert "/proc/" not in output


def test_council_dynamic_schema_binds_each_revision_and_audit_route_id() -> None:
    plans = _plans()
    plan_ids = [str(plan["plan_id"]) for plan in plans]
    request = {
        "opus_merged_plans": plans,
        "final_plans": list(reversed(plans)),
    }
    revision = claude_core._council_output_schema(
        "revision", request=request
    )
    revision_reviews = revision["properties"]["plan_reviews"]
    assert len(revision_reviews["anyOf"]) == 4
    for review_set in revision_reviews["anyOf"]:
        assert review_set["type"] == "object"
        assert review_set["additionalProperties"] is False
        assert review_set["required"] == plan_ids
        for plan_id in plan_ids:
            branches = review_set["properties"][plan_id]["anyOf"]
            assert all(
                branch["properties"]["plan_id"]
                == {"type": "string", "enum": [plan_id]}
                for branch in branches
            )

    audit = claude_core._council_output_schema("audit", request=request)
    audit_decisions = audit["properties"]["decision"]["anyOf"]
    reversed_ids = list(reversed(plan_ids))
    assert len(audit_decisions) == 4
    for decision in audit_decisions:
        audit_findings = decision["properties"]["plan_findings"]
        assert audit_findings["type"] == "object"
        assert audit_findings["additionalProperties"] is False
        assert audit_findings["required"] == reversed_ids
        assert set(audit_findings["properties"]) == set(plan_ids)
        for plan_id in plan_ids:
            branches = audit_findings["properties"][plan_id]["anyOf"]
            assert all(
                branch["properties"]["plan_id"]
                == {"type": "string", "enum": [plan_id]}
                for branch in branches
            )


def test_council_output_schemas_use_codex_strict_subset() -> None:
    plans = _plans()
    request = {
        "council_id": "council_" + "6" * 32,
        "statement_sha256": "7" * 64,
        "merged_plan_sha256": "8" * 64,
        "final_plan_sha256": "9" * 64,
        "opus_merged_plans": plans,
        "final_plans": plans,
    }
    for phase in ("blind", "revision", "audit"):
        schema = claude_core._council_output_schema(phase, request=request)
        claude_core._assert_codex_output_schema_subset(schema)

    invalid = claude_core._council_output_schema("audit", request=request)
    invalid["allOf"] = [{"not": {}}]
    with pytest.raises(
        claude_core.ClaudeCoreError, match="unsupported composition"
    ):
        claude_core._assert_codex_output_schema_subset(invalid)


@pytest.mark.parametrize("plan_id", ["not", "if", "then", "else"])
def test_codex_schema_walker_treats_route_ids_as_property_names(
    plan_id: str,
) -> None:
    plans = _plans()
    plans[0] = {**plans[0], "plan_id": plan_id}
    request = {
        "council_id": "council_" + "6" * 32,
        "statement_sha256": "7" * 64,
        "merged_plan_sha256": "8" * 64,
        "final_plan_sha256": "9" * 64,
        "opus_merged_plans": plans,
        "final_plans": plans,
    }
    for phase in ("revision", "audit"):
        claude_core._assert_codex_output_schema_subset(
            claude_core._council_output_schema(phase, request=request)
        )


def test_council_schema_enforces_cross_field_semantics_without_retry() -> None:
    plans = _plans()
    plan_ids = [str(plan["plan_id"]) for plan in plans]
    council_id = "council_" + "7" * 32
    statement_sha256 = "8" * 64
    plan_sha256 = "9" * 64
    request = {
        "council_id": council_id,
        "statement_sha256": statement_sha256,
        "merged_plan_sha256": plan_sha256,
        "final_plan_sha256": plan_sha256,
        "opus_merged_plans": plans,
        "final_plans": plans,
    }
    revision_schema = claude_core._council_output_schema(
        "revision", request=request
    )
    reviews = {
        plan_id: {
            "plan_id": plan_id,
            "verdict": "keep",
            "objections": [],
            "required_changes": [],
            "replacement_plan": None,
        }
        for plan_id in plan_ids
    }
    revision = {
        "schema_version": claude_core.COUNCIL_SOL_REVISION_SCHEMA,
        "council_id": council_id,
        "statement_sha256": statement_sha256,
        "merged_plan_sha256": plan_sha256,
        "plan_reviews": reviews,
        "global_assessment": "The routes remain distinct.",
        "fanout_ready": True,
    }
    jsonschema.validate(revision, revision_schema)

    invalid_keep = json.loads(json.dumps(revision))
    invalid_keep["plan_reviews"][plan_ids[0]]["required_changes"] = ["change"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid_keep, revision_schema)

    invalid_whitespace = {**revision, "global_assessment": " \n\t "}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid_whitespace, revision_schema)

    invalid_replacements = json.loads(json.dumps(revision))
    for index in (0, 1):
        plan_id = plan_ids[index]
        invalid_replacements["plan_reviews"][plan_id].update(
            {
                "verdict": "replace",
                "replacement_plan": {
                    **plans[index],
                    "plan_id": f"replacement_{index}",
                },
            }
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid_replacements, revision_schema)
    with pytest.raises(claude_core.ClaudeCoreError, match="at most one"):
        claude_core._validate_council_report(
            invalid_replacements,
            phase="revision",
            council_id=council_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id="root-session-1",
            plan_set={"plans": plans},
            plan_sha256=plan_sha256,
        )

    audit_schema = claude_core._council_output_schema("audit", request=request)
    findings = {
        plan_id: {
            "plan_id": plan_id,
            "severity": "clear",
            "finding": "No fatal issue.",
            "required_change": None,
        }
        for plan_id in plan_ids
    }
    audit = {
        "schema_version": claude_core.COUNCIL_SOL_AUDIT_SCHEMA,
        "council_id": council_id,
        "statement_sha256": statement_sha256,
        "final_plan_sha256": plan_sha256,
        "decision": {"verdict": "ready", "plan_findings": findings},
        "diversity_assessment": "The mechanisms are distinct.",
        "rationale": "The route slate is ready.",
    }
    jsonschema.validate(audit, audit_schema)

    ready_with_fatal = json.loads(json.dumps(audit))
    ready_with_fatal["decision"]["plan_findings"][plan_ids[0]].update(
        {"severity": "fatal", "required_change": "Replace the route."}
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(ready_with_fatal, audit_schema)
    with pytest.raises(claude_core.ClaudeCoreError, match="inconsistent"):
        claude_core._validate_council_report(
            ready_with_fatal,
            phase="audit",
            council_id=council_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id="root-session-1",
            plan_set={"plans": plans},
            plan_sha256=plan_sha256,
        )

    blocked_without_fatal = json.loads(json.dumps(audit))
    blocked_without_fatal["decision"]["verdict"] = "blocked"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(blocked_without_fatal, audit_schema)
    with pytest.raises(claude_core.ClaudeCoreError, match="inconsistent"):
        claude_core._validate_council_report(
            blocked_without_fatal,
            phase="audit",
            council_id=council_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id="root-session-1",
            plan_set={"plans": plans},
            plan_sha256=plan_sha256,
        )

    fatal_without_change = json.loads(json.dumps(audit))
    fatal_without_change["decision"]["verdict"] = "blocked"
    fatal_without_change["decision"]["plan_findings"][plan_ids[0]].update(
        {"severity": "fatal", "required_change": None}
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(fatal_without_change, audit_schema)


def test_council_phase_rejects_mismatched_statement_before_any_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_a = "Statement A.\n"
    statement_b = "Statement B.\n"
    digest_a = hashlib.sha256(statement_a.encode()).hexdigest()
    council_id = "council_" + "a" * 32
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    request = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest_a,
        "root_session_id": "12345678-1234-4123-8123-123456789abc",
        "problem_statement": statement_b,
        "retrieval_profile": {},
    }
    with pytest.raises(claude_core.ClaudeCoreError, match="statement binding"):
        claude_core._run_council_phase(
            phase="blind",
            council_id=council_id,
            problem_id="example",
            statement_sha256=digest_a,
            root_session_id="12345678-1234-4123-8123-123456789abc",
            request=request,
            report_plan_set=None,
            report_plan_sha256=None,
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )
    assert not (tmp_path / "state" / "example" / "councils" / council_id).exists()


def test_council_acceptance_uses_only_root_guard_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    expected_source = "a" * 64

    def live_source_must_not_be_read() -> str:
        raise AssertionError("acceptance reread the mutable live source")

    monkeypatch.setattr(
        claude_core, "_host_source_sha256", live_source_must_not_be_read
    )
    arguments = {
        "problem_id": "example",
        "statement_sha256": "b" * 64,
        "root_session_id": "12345678-1234-4123-8123-123456789abc",
        "council_id": "council_" + "c" * 32,
        "council_round": 1,
        "audit_plan_sha256": "d" * 64,
        "final_plan_sha256": "d" * 64,
        "audit_receipt_sha256": "e" * 64,
        "status": "accepted",
        "override_mode": None,
        "finding_resolutions": None,
        "override_reason": None,
        "expected_host_source_sha256": expected_source,
    }
    written = claude_core._write_council_acceptance(**arguments)
    replay = claude_core._write_council_acceptance(**arguments)
    assert written == replay
    assert written["host_source_sha256"] == expected_source


def test_council_adjudications_are_id_bound_not_list_order_bound() -> None:
    merged_plans = _plans()
    merged = {"plans": merged_plans}
    revised_first = {**merged_plans[0], "plan_summary": "revised summary"}
    replacement = {
        **merged_plans[1],
        "plan_id": "replacement_route",
        "mechanism": "replacement mechanism",
        "scope": "replacement scope",
    }
    final = {"plans": [merged_plans[2], replacement, revised_first]}
    revision = {
        "plan_reviews": [
            {
                "plan_id": "route_1",
                "verdict": "revise",
                "replacement_plan": None,
            },
            {
                "plan_id": "route_2",
                "verdict": "replace",
                "replacement_plan": replacement,
            },
            {
                "plan_id": "route_3",
                "verdict": "keep",
                "replacement_plan": None,
            },
        ]
    }
    adjudications = [
        {
            "draft_plan_id": "route_3",
            "final_plan_id": "route_3",
            "decision": "accepted",
            "rationale": "Keep route three.",
        },
        {
            "draft_plan_id": "route_2",
            "final_plan_id": "replacement_route",
            "decision": "accepted",
            "rationale": "Accept the sole replacement.",
        },
        {
            "draft_plan_id": "route_1",
            "final_plan_id": "route_1",
            "decision": "accepted",
            "rationale": "Accept the bounded revision.",
        },
    ]
    normalized = claude_core._validate_council_adjudications(
        adjudications,
        merged_plan_set=merged,
        revision_report=revision,
        final_plan_set=final,
    )
    assert [item["draft_plan_id"] for item in normalized] == [
        "route_1",
        "route_2",
        "route_3",
    ]

    renamed_revision = [dict(item) for item in adjudications]
    renamed_revision[2]["final_plan_id"] = "renamed_revision"
    renamed_final = {
        "plans": [
            merged_plans[2],
            replacement,
            {**revised_first, "plan_id": "renamed_revision"},
        ]
    }
    with pytest.raises(
        claude_core.CouncilContractError, match="must preserve its draft_plan_id"
    ):
        claude_core._validate_council_adjudications(
            renamed_revision,
            merged_plan_set=merged,
            revision_report=revision,
            final_plan_set=renamed_final,
        )

    oversized_rationale = [dict(item) for item in adjudications]
    oversized_rationale[0]["rationale"] = "x" * 4097
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match=r"exceeds 4096 UTF-8 bytes \(actual=4097, remove_at_least=1\)",
    ):
        claude_core._validate_council_adjudications(
            oversized_rationale,
            merged_plan_set=merged,
            revision_report=revision,
            final_plan_set=final,
        )


def test_council_sol_reviews_and_findings_are_id_bound_not_order_bound() -> None:
    digest = _statement_digest()
    council_id = "council_" + "7" * 32
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    plans = _plans()
    plan_set = {"plans": plans}
    plan_sha256 = hashlib.sha256(
        (claude_core.canonical_json(plan_set) + "\n").encode()
    ).hexdigest()
    reviews = [
        {
            "plan_id": plan["plan_id"],
            "verdict": "keep",
            "objections": [],
            "required_changes": [],
            "replacement_plan": None,
        }
        for plan in plans
    ]
    revision = claude_core._validate_council_report(
        {
            "schema_version": claude_core.COUNCIL_SOL_REVISION_SCHEMA,
            "council_id": council_id,
            "statement_sha256": digest,
            "merged_plan_sha256": plan_sha256,
            "plan_reviews": list(reversed(reviews)),
            "global_assessment": "The routes remain distinct.",
            "fanout_ready": True,
        },
        phase="revision",
        council_id=council_id,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        plan_set=plan_set,
        plan_sha256=plan_sha256,
    )
    assert [item["plan_id"] for item in revision["plan_reviews"]] == [
        "route_1",
        "route_2",
        "route_3",
    ]
    swapped_reviews = {
        "route_1": reviews[1],
        "route_2": reviews[0],
        "route_3": reviews[2],
    }
    with pytest.raises(claude_core.ClaudeCoreError, match="key mismatch"):
        claude_core._validate_council_report(
            {
                **revision,
                "plan_reviews": swapped_reviews,
            },
            phase="revision",
            council_id=council_id,
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            plan_set=plan_set,
            plan_sha256=plan_sha256,
        )

    findings = [
        {
            "plan_id": plan["plan_id"],
            "severity": "clear",
            "finding": "No fatal route-design defect found.",
            "required_change": None,
        }
        for plan in plans
    ]
    audit = claude_core._validate_council_report(
        {
            "schema_version": claude_core.COUNCIL_SOL_AUDIT_SCHEMA,
            "council_id": council_id,
            "statement_sha256": digest,
            "final_plan_sha256": plan_sha256,
            "decision": {
                "verdict": "ready",
                "plan_findings": {
                    findings[1]["plan_id"]: findings[1],
                    findings[2]["plan_id"]: findings[2],
                    findings[0]["plan_id"]: findings[0],
                },
            },
            "diversity_assessment": "The routes are materially distinct.",
            "rationale": "The slate is ready.",
        },
        phase="audit",
        council_id=council_id,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        plan_set=plan_set,
        plan_sha256=plan_sha256,
    )
    assert [
        item["plan_id"] for item in audit["decision"]["plan_findings"]
    ] == [
        "route_1",
        "route_2",
        "route_3",
    ]
    with pytest.raises(claude_core.ClaudeCoreError, match="key mismatch"):
        claude_core._validate_council_report(
            {
                **audit,
                "decision": {
                    **audit["decision"],
                    "plan_findings": {
                        "route_1": findings[1],
                        "route_2": findings[0],
                        "route_3": findings[2],
                    },
                },
            },
            phase="audit",
            council_id=council_id,
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            plan_set=plan_set,
            plan_sha256=plan_sha256,
        )

    duplicate_findings = [findings[0], findings[0], findings[2]]
    with pytest.raises(claude_core.ClaudeCoreError, match="finding binding"):
        claude_core._validate_council_report(
            {
                **audit,
                "decision": {
                    **audit["decision"],
                    "plan_findings": duplicate_findings,
                },
            },
            phase="audit",
            council_id=council_id,
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            plan_set=plan_set,
            plan_sha256=plan_sha256,
        )


def test_council_contract_preflight_is_actionable_and_non_paid() -> None:
    result = claude_core._council_preflight_failure(
        problem_id="example",
        operation="finalize_route_council",
        error=claude_core.CouncilContractError(
            "revise adjudication must preserve its draft_plan_id"
        ),
    )
    assert result["status"] == "preflight_failed"
    assert result["category"] == "route_council_contract"
    assert result["retry_allowed"] is True
    assert result["paid_sol_dispatched"] is False
    assert "only the single Astra replace slot" in result["repair_hint"]


def test_cohort_contract_preflight_is_structured_before_intent() -> None:
    result = claude_core._cohort_preflight_failure(
        problem_id="example",
        error=claude_core.CohortContractError(
            "council_receipt_sha256 does not match acceptance.json"
        ),
    )
    assert result["schema_version"] == claude_core.COHORT_PREFLIGHT_FAILURE_SCHEMA
    assert result["status"] == "preflight_failed"
    assert result["category"] == "cohort_contract"
    assert result["retry_allowed"] is True
    assert result["intent_committed"] is False
    assert result["cohort_id"] is None
    assert "does not match acceptance.json" in result["error"]


def test_cohort_token_telemetry_records_aggregate_and_honest_lane_gaps(
    tmp_path: Path,
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    log_path = tmp_path / "executor.log"
    log_path.write_bytes(b"bounded output\ntokens used\n137,000\n")
    telemetry = claude_core._persist_cohort_token_telemetry(
        state_dir=tmp_path,
        cohort_id="cohort_" + "a" * 32,
        problem_id="example",
        statement_sha256=digest,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
        plan_set=plan_set,
        log_path=log_path,
        log_sha256=hashlib.sha256(log_path.read_bytes()).hexdigest(),
        elapsed_seconds=1242.583,
    )
    assert telemetry["coverage"] == "aggregate_only"
    assert telemetry["aggregate"]["tokens_used"] == 137_000
    assert telemetry["aggregate"]["observed"] is True
    assert [lane["plan_id"] for lane in telemetry["lanes"]] == [
        "route_1",
        "route_2",
        "route_3",
    ]
    assert all(lane["tokens_used"] is None for lane in telemetry["lanes"])
    assert all(
        lane["finality"] == "unavailable_from_native_collaboration"
        for lane in telemetry["lanes"]
    )
    assert (tmp_path / "token_telemetry.json").is_file()


def test_route_council_accepts_only_bounded_statement_retrieval_events() -> None:
    report = {"synthetic": "retrieval-only report"}
    profile = {
        "schema_version": claude_core.COUNCIL_RETRIEVAL_PROFILE_SCHEMA,
        "mode": "matlas_arxiv",
        "basis": "explicit_matlas_arxiv_permission",
        "capability": claude_core.COUNCIL_RETRIEVAL_CAPABILITY,
        "tools": list(claude_core.COUNCIL_RETRIEVAL_TOOLS),
        "max_search_queries": claude_core.COUNCIL_SEARCH_QUERIES_PER_PHASE,
        "max_primary_reads": claude_core.COUNCIL_PRIMARY_READS_PER_PHASE,
        "general_web": False,
        "workspace_access": False,
        "cutoff_enforcement": "statement_bound_search_and_read_arxiv",
    }
    events = [
        {"type": "thread.started", "thread_id": "synthetic-thread"},
        {"type": "turn.started"},
        {
            "type": "item.started",
            "item": {
                "type": "mcp_tool_call",
                "id": "call-1",
                "server": claude_core.COUNCIL_RETRIEVAL_SERVER,
                "tool": "search_arxiv_theorems",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "id": "call-1",
                "server": claude_core.COUNCIL_RETRIEVAL_SERVER,
                "tool": "search_arxiv_theorems",
                "status": "completed",
                "result": {"content": []},
                "error": None,
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": claude_core.canonical_json(report),
            },
        },
        {"type": "turn.completed"},
    ]
    raw = b"".join(
        (claude_core.canonical_json(event) + "\n").encode() for event in events
    )
    trace, observed = claude_core._parse_council_events(
        raw, retrieval_profile=profile
    )
    assert observed == report
    assert trace["tool_free"] is False
    assert trace["retrieval_tool_calls"] == 1
    assert trace["retrieval_tool_counts"]["search_arxiv_theorems"] == 1

    forbidden = list(events)
    forbidden[3] = {
        **forbidden[3],
        "item": {**forbidden[3]["item"], "server": "foreign"},
    }
    forbidden_raw = b"".join(
        (claude_core.canonical_json(event) + "\n").encode()
        for event in forbidden
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="forbidden"):
        claude_core._parse_council_events(
            forbidden_raw, retrieval_profile=profile
        )

    lifecycle_cases = [
        (
            [events[0], events[1], events[2], events[4], events[5]],
            "unfinished retrieval",
        ),
        (
            [events[0], events[1], events[3], events[4], events[5]],
            "completion is invalid",
        ),
        (
            [
                events[0],
                events[1],
                events[2],
                {
                    **events[3],
                    "item": {
                        **events[3]["item"],
                        "tool": "search_matlas_theorems",
                    },
                },
                events[4],
                events[5],
            ],
            "completion is invalid",
        ),
        (
            [events[0], events[1], events[2], events[2], events[3], events[4], events[5]],
            "start is invalid",
        ),
    ]
    for lifecycle_events, expected_error in lifecycle_cases:
        lifecycle_raw = b"".join(
            (claude_core.canonical_json(event) + "\n").encode()
            for event in lifecycle_events
        )
        with pytest.raises(claude_core.ClaudeCoreError, match=expected_error):
            claude_core._parse_council_events(
                lifecycle_raw, retrieval_profile=profile
            )


def test_council_retrieval_mcp_config_carries_root_execution_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(claude_core.__file__).resolve(strict=True)
    source_snapshot = tmp_path / "authenticated-claude-core.py"
    source_snapshot.write_bytes(source.read_bytes())
    launcher_sha256 = "a" * 64
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", launcher_sha256
    )

    value, source_sha256 = claude_core._council_retrieval_mcp_toml(
        problem_id="example",
        statement_sha256=_statement_digest(),
        root_session_id="12345678-1234-4123-8123-123456789abc",
        council_id="council_" + "b" * 32,
        phase="blind",
        request_sha256="c" * 64,
        host_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_snapshot=source_snapshot,
        source_origin=source,
    )
    server = tomllib.loads("server=" + value)["server"]
    assert source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert server["env"]["RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256"] == (
        launcher_sha256
    )
    assert "RETHLAS_CLAUDE_ROOT_PROBLEM_ID" not in server["env"]

    monkeypatch.delenv("RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256")
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="root execution epoch binding is missing",
    ):
        claude_core._council_retrieval_mcp_toml(
            problem_id="example",
            statement_sha256=_statement_digest(),
            root_session_id="12345678-1234-4123-8123-123456789abc",
            council_id="council_" + "b" * 32,
            phase="blind",
            request_sha256="c" * 64,
            host_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            source_snapshot=source_snapshot,
            source_origin=source,
        )


def test_council_retrieval_mcp_is_exactly_scoped_and_budgeted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    source = data_root / "permitted.md"
    source.write_text(
        "# Synthetic problem\n\nProve it.\n\n"
        "## Retrieval restriction\n\n"
        "Matlas and arXiv retrieval are permitted. Use no arXiv source whose "
        "initial submission date is later than 2026-06-26.\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="permitted",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    council_id = "council_" + "b" * 32
    pointer = {
        "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
        "pointer_version": 1,
        "problem_id": "permitted",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
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
        "state": "active",
        "final_plan_sha256": None,
        "acceptance_sha256": None,
        "checkpoint_sha256": None,
        "cohort_id": None,
        "updated_at_unix": time.time(),
    }
    claude_core._write_once(
        claude_core._council_pointer_path("permitted", root_session_id),
        pointer,
        mode=0o400,
    )
    profile = claude_core._council_retrieval_profile(
        problem_id="permitted", statement_sha256=digest
    )
    request = {
        "schema_version": "synthetic_council_retrieval_request_v1",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "permitted",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text("permitted"),
        "retrieval_profile": profile,
    }
    request_raw = (claude_core.canonical_json(request) + "\n").encode()
    request_sha256 = hashlib.sha256(request_raw).hexdigest()
    state_dir = claude_core._council_dir("permitted", council_id)
    claude_core._write_bytes_once(
        state_dir / "blind_request.json", request_raw, mode=0o400
    )
    profile_sha256 = hashlib.sha256(
        (claude_core.canonical_json(profile) + "\n").encode()
    ).hexdigest()
    claude_core._write_once(
        state_dir / "blind_intent.json",
        {
            "schema_version": claude_core.COUNCIL_PHASE_INTENT_SCHEMA,
            "state": "submitted",
            "phase": "blind",
            "council_id": council_id,
            "problem_id": "permitted",
            "statement_sha256": digest,
            "root_session_id": root_session_id,
            "request_sha256": request_sha256,
            "retrieval_profile_sha256": profile_sha256,
            "retrieval_capability": profile["capability"],
            "max_search_queries": profile["max_search_queries"],
            "max_primary_reads": profile["max_primary_reads"],
            "output_schema_sha256": "5" * 64,
            "host_source_sha256": claude_core._host_source_sha256(),
        },
        mode=0o400,
    )
    bindings = {
        "RETHLAS_COUNCIL_RETRIEVAL_MODE": "1",
        "RETHLAS_COUNCIL_PROBLEM_ID": "permitted",
        "RETHLAS_COUNCIL_STATEMENT_SHA256": digest,
        "RETHLAS_COUNCIL_ROOT_SESSION_ID": root_session_id,
        "RETHLAS_COUNCIL_ID": council_id,
        "RETHLAS_COUNCIL_PHASE": "blind",
        "RETHLAS_COUNCIL_REQUEST_SHA256": request_sha256,
        "RETHLAS_COUNCIL_HOST_SOURCE_SHA256": claude_core.sha256_file(
            Path(claude_core.__file__).resolve()
        ),
    }
    for key, value in bindings.items():
        monkeypatch.setenv(key, value)

    observed: list[tuple[str, dict[str, object]]] = []
    real_host_source = claude_core._host_source_sha256

    class FakeLegacy:
        @staticmethod
        def search_matlas_theorems(**kwargs: object) -> dict[str, object]:
            observed.append(("matlas", kwargs))
            return {"retrieval_status": "ok", "provider": "matlas"}

        @staticmethod
        def search_arxiv_theorems_for_problem(
            **kwargs: object,
        ) -> dict[str, object]:
            observed.append(("arxiv", kwargs))
            return {"retrieval_status": "ok", "provider": "arxiv"}

        @staticmethod
        def read_arxiv_primary_for_problem(
            **kwargs: object,
        ) -> dict[str, object]:
            observed.append(("primary", kwargs))
            locator = kwargs.get("locator")
            if locator in {"source drift success", "source drift error"}:
                monkeypatch.setattr(
                    claude_core, "_host_source_sha256", lambda: "f" * 64
                )
                if locator == "source drift error":
                    raise RuntimeError("synthetic provider failure after drift")
            return {"retrieval_status": "ok", "provider": "arxiv_official"}

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy())
    app = claude_core.build_council_retrieval_mcp_app()
    manager = getattr(app, "_tool_manager", None)
    tools = getattr(manager, "_tools", {}) if manager is not None else {}
    assert set(tools) == set(claude_core.COUNCIL_RETRIEVAL_TOOLS)

    real_time = time.time
    rollback_times = iter((100.0, 99.0))
    monkeypatch.setattr(claude_core.time, "time", lambda: next(rollback_times))
    tools["search_matlas_theorems"].fn(
        problem_id="permitted", query="first gap", num_results=2
    )
    rollback_ledger = claude_core._read_council_retrieval_ledger(
        state_dir / "blind_retrieval_ledger.json",
        binding=claude_core._council_retrieval_binding_from_environment(),
    )
    assert rollback_ledger["calls"][0]["reserved_at_unix"] == 100.0
    assert rollback_ledger["calls"][0]["settled_at_unix"] == 100.0
    assert rollback_ledger["updated_at_unix"] == 100.0
    monkeypatch.setattr(claude_core.time, "time", real_time)
    tools["search_arxiv_theorems"].fn(
        problem_id="permitted", query="second gap", num_results=3
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="budget is exhausted"):
        tools["search_arxiv_theorems"].fn(
            problem_id="permitted", query="third gap", num_results=1
        )
    for locator in ("source drift success", "source drift error"):
        with pytest.raises(claude_core.ClaudeCoreError, match="host source"):
            tools["read_arxiv_primary"].fn(
                problem_id="permitted",
                arxiv_id="1711.11482",
                locator=locator,
            )
        monkeypatch.setattr(
            claude_core, "_host_source_sha256", real_host_source
        )
    for index in range(2):
        tools["read_arxiv_primary"].fn(
            problem_id="permitted",
            arxiv_id="1711.11482",
            locator=f"Theorem {index + 1}",
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="budget is exhausted"):
        tools["read_arxiv_primary"].fn(
            problem_id="permitted",
            arxiv_id="1711.11482",
            locator="Theorem 5",
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="differs"):
        tools["search_matlas_theorems"].fn(
            problem_id="other", query="wrong binding", num_results=1
        )
    assert observed[-1][1]["expected_statement_sha256"] == digest
    ledger = claude_core._read_council_retrieval_ledger(
        state_dir / "blind_retrieval_ledger.json",
        binding=claude_core._council_retrieval_binding_from_environment(),
    )
    assert len(ledger["calls"]) == 6
    assert [call["state"] for call in ledger["calls"]] == [
        "completed", "completed", "submitted", "submitted",
        "completed", "completed",
    ]


def test_council_source_migration_quiescence_includes_retrieval_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    problem_id = "example"
    council_id = "council_" + "d" * 32
    state_dir = claude_core._council_dir(problem_id, council_id)
    acquired = threading.Event()
    release = threading.Event()

    def hold_retrieval_lock() -> None:
        with claude_core._council_retrieval_lock(
            problem_id=problem_id, council_id=council_id, phase="blind"
        ):
            acquired.set()
            assert release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_retrieval_lock)
        assert acquired.wait(timeout=5)
        try:
            with pytest.raises(claude_core.ClaudeCoreError, match="phase is active"):
                with claude_core._council_quiescence_guard(state_dir):
                    raise AssertionError("quiescence admitted an active retrieval")
        finally:
            release.set()
        holder.result(timeout=5)


def test_route_council_phase_intent_fences_concurrent_paid_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    council_id = "council_" + "a" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def fake_council(**_arguments: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": council_id,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", fake_council)
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claude_core._run_council_phase, **arguments)
        assert started.wait(timeout=5)
        second = executor.submit(claude_core._run_council_phase, **arguments)
        release.set()
        receipt = first.result(timeout=5)
        concurrent = second.result(timeout=5)

    assert receipt["status"] == "completed"
    assert concurrent == receipt
    assert calls == 1
    repeated = claude_core._run_council_phase(**arguments)
    assert repeated["report"] == receipt["report"]


def test_council_execution_survives_host_settlement_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    council_id = "council_" + "1" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    dispatches = [0]

    def valid_council(**_arguments: object) -> dict[str, object]:
        dispatches[0] += 1
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": council_id,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "event_stream_sha256": "7" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", valid_council)
    original_persist = claude_core._persist_council_phase_settlement
    interrupted = [False]

    def interrupt_host_settlement(**arguments: object) -> None:
        if not interrupted[0]:
            interrupted[0] = True
            raise claude_core.ClaudeCoreError(
                "synthetic host interruption after worker settlement"
            )
        original_persist(**arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(
        claude_core,
        "_persist_council_phase_settlement",
        interrupt_host_settlement,
    )
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    with pytest.raises(claude_core.ClaudeCoreError, match="host interruption"):
        claude_core._run_council_phase(**arguments)
    state_dir = tmp_path / "state" / "example" / "councils" / council_id
    assert (state_dir / "blind_execution.json").is_file()
    assert not (state_dir / "blind_settlement.json").exists()

    monkeypatch.setattr(
        claude_core,
        "_persist_council_phase_settlement",
        original_persist,
    )
    receipt = claude_core._run_council_phase(**arguments)
    assert receipt["status"] == "completed"
    assert dispatches == [1]


def test_reused_live_phase_worker_pid_settles_when_worker_lock_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    monkeypatch.setattr(claude_core, "_pid_is_live", lambda _pid: True)
    monkeypatch.setattr(
        claude_core, "_process_identity_token", lambda _pid: "6" * 64
    )
    monkeypatch.setattr(
        claude_core, "_terminate_owned_command_wrappers", lambda *_args: None
    )
    council_id = "council_" + "2" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    launches = [0]

    def dead_launch(**arguments: object) -> int:
        launches[0] += 1
        state_dir = arguments["state_dir"]
        assert isinstance(state_dir, Path)
        phase = str(arguments["phase"])
        request_path = state_dir / f"{phase}_request.json"
        claude_core._write_once(
            state_dir / f"{phase}_worker.json",
            {
                "schema_version": claude_core.COUNCIL_PHASE_WORKER_SCHEMA,
                "phase": phase,
                "council_id": council_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "root_session_id": root_session_id,
                "request_sha256": hashlib.sha256(
                    request_path.read_bytes()
                ).hexdigest(),
                "host_source_sha256": claude_core._host_source_sha256(),
                "worker_pid": 99_999_999,
                "started_at_unix": time.time(),
                "worker_start_token": "5" * 64,
            },
            mode=0o400,
        )
        return 99_999_999

    monkeypatch.setattr(claude_core, "_launch_council_phase_worker", dead_launch)
    monkeypatch.setattr(
        claude_core,
        "_invoke_sol_council",
        lambda **_arguments: pytest.fail("a dead worker must not be redispatched"),
    )
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    receipt = claude_core._run_council_phase(**arguments)
    repeated = claude_core._run_council_phase(**arguments)
    assert receipt == repeated
    assert receipt["status"] == "execution_unknown"
    assert receipt["execution"]["error"] == (
        "phase_worker_stopped_before_settlement"
    )
    assert launches == [1]


def test_phase_prelaunch_oserror_relaunches_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    def write_test_bundle(destination: Path) -> Path:
        destination.mkdir(mode=0o700)
        return destination

    monkeypatch.setattr(
        claude_core, "_write_runtime_dependency_bundle", write_test_bundle
    )
    monkeypatch.setattr(
        claude_core,
        "_validate_runtime_dependency_bundle",
        lambda destination: destination,
    )
    council_id = "council_" + "3" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    launch_attempts = [0]
    sol_calls = [0]
    synchronous_launch = claude_core._launch_council_phase_worker

    def fail_before_popen(**_arguments: object) -> int:
        launch_attempts[0] += 1
        raise OSError("synthetic Popen failure after dispatch commitment")

    def valid_council(**_arguments: object) -> dict[str, object]:
        sol_calls[0] += 1
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": council_id,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic recovered worker result.",
            },
            "event_stream_sha256": "8" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(
        claude_core, "_launch_council_phase_worker", fail_before_popen
    )
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    with pytest.raises(claude_core.ClaudeCoreError, match="retry the same"):
        claude_core._run_council_phase(**arguments)

    state_dir = tmp_path / "state" / "example" / "councils" / council_id
    assert (state_dir / "blind_dispatch.json").is_file()
    assert not (state_dir / "blind_worker.json").exists()
    assert not (state_dir / "blind_execution.json").exists()

    def recovered_launch(**launch_arguments: object) -> int:
        launch_attempts[0] += 1
        return synchronous_launch(**launch_arguments)

    monkeypatch.setattr(claude_core, "_invoke_sol_council", valid_council)
    monkeypatch.setattr(
        claude_core, "_launch_council_phase_worker", recovered_launch
    )
    receipt = claude_core._run_council_phase(**arguments)
    repeated = claude_core._run_council_phase(**arguments)
    assert receipt["status"] == "completed"
    assert repeated == receipt
    assert launch_attempts == [2]
    assert sol_calls == [1]


def test_phase_recovery_waits_for_a_live_premarker_child_dispatch_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    def write_test_bundle(destination: Path) -> Path:
        destination.mkdir(mode=0o700)
        return destination

    monkeypatch.setattr(
        claude_core, "_write_runtime_dependency_bundle", write_test_bundle
    )
    monkeypatch.setattr(
        claude_core,
        "_validate_runtime_dependency_bundle",
        lambda destination: destination,
    )
    council_id = "council_" + "4" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    synchronous_launch = claude_core._launch_council_phase_worker
    launch_attempts = [0]
    sol_calls = [0]
    ready = tmp_path / "premarker-child-ready"
    release = tmp_path / "premarker-child-release"
    holder: list[subprocess.Popen[bytes]] = []

    def valid_council(**_arguments: object) -> dict[str, object]:
        sol_calls[0] += 1
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": council_id,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic post-barrier worker result.",
            },
            "event_stream_sha256": "9" * 64,
            "retry_allowed": False,
        }

    def launch_then_interrupt(**launch_arguments: object) -> int:
        launch_attempts[0] += 1
        if launch_attempts[0] > 1:
            return synchronous_launch(**launch_arguments)
        descriptor = launch_arguments.get("dispatch_lock_descriptor")
        assert isinstance(descriptor, int)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                (
                    "import pathlib,sys,time\n"
                    "ready=pathlib.Path(sys.argv[1]); release=pathlib.Path(sys.argv[2])\n"
                    "ready.write_bytes(b'ready')\n"
                    "while not release.exists(): time.sleep(0.01)\n"
                ),
                str(ready),
                str(release),
            ],
            close_fds=True,
            pass_fds=(descriptor,),
        )
        holder.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists():
            if time.monotonic() >= deadline:
                pytest.fail("pre-marker child did not start")
            time.sleep(0.01)
        raise OSError("synthetic cleanup failure after child Popen")

    monkeypatch.setattr(claude_core, "_invoke_sol_council", valid_council)
    monkeypatch.setattr(
        claude_core, "_launch_council_phase_worker", launch_then_interrupt
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="retry the same"):
        claude_core._run_council_phase(**arguments)

    state_dir = tmp_path / "state" / "example" / "councils" / council_id
    assert (state_dir / "blind_dispatch.json").is_file()
    assert not (state_dir / "blind_worker.json").exists()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            recovery = executor.submit(claude_core._run_council_phase, **arguments)
            time.sleep(0.3)
            assert not recovery.done()
            assert launch_attempts == [1]
            release.write_bytes(b"release")
            receipt = recovery.result(timeout=10)
    finally:
        release.write_bytes(b"release")
        for process in holder:
            process.wait(timeout=5)
    assert receipt["status"] == "completed"
    assert launch_attempts == [2]
    assert sol_calls == [1]


def test_invalid_council_report_preserves_actionable_rejected_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    council_id = "council_" + "9" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    raw_report = {
        "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
        "council_id": "council_" + "8" * 32,
        "statement_sha256": digest,
        "plan_slots": _blind_plan_slots(),
        "global_risks": ["Synthetic risk."],
        "comparative_note": "Synthetic blind slate.",
    }
    monkeypatch.setattr(
        claude_core,
        "_invoke_sol_council",
        lambda **_arguments: {
            "status": "completed",
            "report": raw_report,
            "event_stream_sha256": "6" * 64,
            "retry_allowed": False,
        },
    )
    receipt = claude_core._run_council_phase(
        phase="blind",
        council_id=council_id,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        request=request,
        report_plan_set=None,
        report_plan_sha256=None,
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert receipt["status"] == "operational_blocked"
    assert receipt["execution"]["validation_error"] == (
        "blind route-council report binding mismatch"
    )
    assert receipt["model"] == "gpt-6-astra"
    assert receipt["reasoning_effort"] == "max"
    rejected_path = (
        state_root
        / "example"
        / "councils"
        / council_id
        / "blind_rejected_report.json"
    )
    rejected = json.loads(rejected_path.read_text(encoding="utf-8"))
    assert rejected["schema_version"] == claude_core.COUNCIL_REJECTED_REPORT_SCHEMA
    assert rejected["raw_report"] == raw_report
    assert rejected["validation_error"] == receipt["execution"]["validation_error"]
    assert hashlib.sha256(rejected_path.read_bytes()).hexdigest() == (
        receipt["execution"]["rejected_report_sha256"]
    )


def test_operational_council_failure_seals_private_stderr_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    stderr_bytes = b"synthetic codex startup failure\n"
    event_bytes = b'{"type":"unexpected.future.event"}\n'
    event_failure = claude_core.CouncilEventStreamError(
        "route-council used a forbidden capability event",
        code="forbidden_event_type",
        line_number=1,
        event_type="unexpected.future.event",
    )

    def operational_failure(**arguments: object) -> dict[str, object]:
        request = arguments["request"]
        assert isinstance(request, dict)
        request_sha256 = claude_core.sha256_bytes(
            (claude_core.canonical_json(request) + "\n").encode("utf-8")
        )
        return {
            "status": "operational_blocked",
            "error": "nonzero_exit",
            "elapsed_seconds": 0.25,
            "returncode": 1,
            "stderr_sha256": claude_core.sha256_bytes(stderr_bytes),
            "retry_allowed": False,
            "_private_stderr_diagnostic": (
                claude_core._council_stderr_diagnostic(
                    phase="blind",
                    council_id=str(request["council_id"]),
                    problem_id="example",
                    statement_sha256=digest,
                    root_session_id=root_session_id,
                    request_sha256=request_sha256,
                    stderr_bytes=stderr_bytes,
                    stderr_over_cap=False,
                )
            ),
            "_private_event_diagnostic": (
                claude_core._council_event_diagnostic(
                    phase="blind",
                    council_id=str(request["council_id"]),
                    problem_id="example",
                    statement_sha256=digest,
                    root_session_id=root_session_id,
                    request_sha256=request_sha256,
                    event_bytes=event_bytes,
                    event_over_cap=False,
                    parser_error=event_failure,
                )
            ),
        }

    monkeypatch.setattr(
        claude_core, "_invoke_sol_council", operational_failure
    )
    receipt = claude_core.start_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        opus_plans=_plans(),
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert receipt["status"] == "operational_blocked"
    assert "_private_stderr_diagnostic" not in receipt["execution"]
    assert "_private_event_diagnostic" not in receipt["execution"]
    assert "synthetic codex startup failure" not in (
        claude_core.canonical_json(receipt)
    )
    council_id = str(receipt["council_id"])
    diagnostic_path = (
        state_root
        / "example"
        / "councils"
        / council_id
        / "blind_stderr_diagnostic.json"
    )
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["stderr_text_utf8_lossy"] == stderr_bytes.decode()
    assert diagnostic["stderr_sha256"] == receipt["execution"][
        "stderr_sha256"
    ]
    assert hashlib.sha256(diagnostic_path.read_bytes()).hexdigest() == (
        receipt["execution"]["stderr_diagnostic_sha256"]
    )
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o400
    event_stream_path = diagnostic_path.with_name(
        "blind_rejected_event_stream.jsonl"
    )
    event_diagnostic_path = diagnostic_path.with_name(
        "blind_event_diagnostic.json"
    )
    assert event_stream_path.read_bytes() == event_bytes
    assert stat.S_IMODE(event_stream_path.stat().st_mode) == 0o400
    assert hashlib.sha256(event_diagnostic_path.read_bytes()).hexdigest() == (
        receipt["execution"]["event_diagnostic_sha256"]
    )
    with pytest.raises(
        claude_core.CouncilContractError,
        match="cannot continue because this route council is operational_blocked",
    ):
        claude_core.revise_route_council(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            council_id=council_id,
            merged_plans=_plans(),
            merge_rationale="Synthetic out-of-sequence merge.",
            codex_bin=Path(sys.executable),
            timeout_seconds=60,
        )


def test_rejected_council_event_stream_is_private_and_keeps_token_telemetry(
    tmp_path: Path,
) -> None:
    phase = "blind"
    council_id = "council_" + "a" * 32
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    terminal = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 500,
            "cached_input_tokens": 400,
            "output_tokens": 100,
        },
    }
    raw = (
        '{"type":"thread.started","thread_id":"synthetic"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"unexpected.future.event"}\n'
        + claude_core.canonical_json(terminal)
        + "\n"
    ).encode()
    failure = claude_core.CouncilEventStreamError(
        "route-council used a forbidden capability event",
        code="forbidden_event_type",
        line_number=3,
        event_type="unexpected.future.event",
    )
    diagnostic = claude_core._council_event_diagnostic(
        phase=phase,
        council_id=council_id,
        problem_id="example",
        statement_sha256=_statement_digest(),
        root_session_id=root_session_id,
        request_sha256="b" * 64,
        event_bytes=raw,
        event_over_cap=False,
        parser_error=failure,
    )
    digest = claude_core._persist_council_event_diagnostic(
        state_dir=tmp_path,
        phase=phase,
        value=diagnostic,
    )
    stream_path = tmp_path / "blind_rejected_event_stream.jsonl"
    diagnostic_path = tmp_path / "blind_event_diagnostic.json"
    metadata = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert stream_path.read_bytes() == raw
    assert stat.S_IMODE(stream_path.stat().st_mode) == 0o400
    assert stat.S_IMODE(diagnostic_path.stat().st_mode) == 0o400
    assert metadata["parser_error_code"] == "forbidden_event_type"
    assert metadata["parser_error_line_number"] == 3
    assert metadata["parser_error_event_type"] == "unexpected.future.event"
    assert metadata["telemetry"]["token_usage"] == {
        "input_tokens": 500,
        "cached_input_tokens": 400,
        "output_tokens": 100,
        "total_tokens": 600,
    }
    assert metadata["event_stream_artifact_sha256"] == hashlib.sha256(
        raw
    ).hexdigest()
    assert digest == hashlib.sha256(diagnostic_path.read_bytes()).hexdigest()
    assert "_event_stream_bytes" not in metadata


def test_rejected_council_artifact_reconciles_receipt_without_second_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    council_id = "council_" + "d" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    dispatches = [0]

    def invalid_council(**_arguments: object) -> dict[str, object]:
        dispatches[0] += 1
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": "council_" + "e" * 32,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "event_stream_sha256": "7" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", invalid_council)
    original_write_once = claude_core._write_once
    interrupted = [False]

    def interrupt_receipt(path: Path, value: object, *, mode: int = 0o600) -> str:
        if path.name == "blind_receipt.json" and not interrupted[0]:
            interrupted[0] = True
            raise claude_core.ClaudeCoreError("synthetic settlement interruption")
        return original_write_once(path, value, mode=mode)

    monkeypatch.setattr(claude_core, "_write_once", interrupt_receipt)
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    with pytest.raises(claude_core.ClaudeCoreError, match="interruption"):
        claude_core._run_council_phase(**arguments)
    rejected_path = (
        state_root
        / "example"
        / "councils"
        / council_id
        / "blind_rejected_report.json"
    )
    assert rejected_path.is_file()
    monkeypatch.setattr(claude_core, "_write_once", original_write_once)
    receipt = claude_core._run_council_phase(**arguments)
    assert receipt["status"] == "operational_blocked"
    assert receipt["execution"]["rejected_report_sha256"] == hashlib.sha256(
        rejected_path.read_bytes()
    ).hexdigest()
    assert dispatches == [1]


def test_completed_council_settlement_recovers_missing_receipt_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    council_id = "council_" + "c" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    dispatches = [0]

    def valid_council(**_arguments: object) -> dict[str, object]:
        dispatches[0] += 1
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": council_id,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "event_stream_sha256": "7" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", valid_council)
    original_write_once = claude_core._write_once
    interrupted = [False]

    def interrupt_receipt(path: Path, value: object, *, mode: int = 0o600) -> str:
        if path.name == "blind_receipt.json" and not interrupted[0]:
            interrupted[0] = True
            raise claude_core.ClaudeCoreError("synthetic receipt interruption")
        return original_write_once(path, value, mode=mode)

    monkeypatch.setattr(claude_core, "_write_once", interrupt_receipt)
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    with pytest.raises(claude_core.ClaudeCoreError, match="interruption"):
        claude_core._run_council_phase(**arguments)
    settlement_path = (
        state_root
        / "example"
        / "councils"
        / council_id
        / "blind_settlement.json"
    )
    settlement = json.loads(settlement_path.read_text(encoding="utf-8"))
    assert settlement["schema_version"] == (
        claude_core.COUNCIL_PHASE_SETTLEMENT_SCHEMA
    )

    monkeypatch.setattr(claude_core, "_write_once", original_write_once)
    receipt = claude_core._run_council_phase(**arguments)
    assert receipt["status"] == "completed"
    assert receipt["report"]["council_id"] == council_id
    assert dispatches == [1]


def test_rejected_council_settlement_is_identical_during_concurrent_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    council_id = "council_" + "b" * 32
    request = {
        "schema_version": "synthetic_blind_request_v2",
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "problem_statement": _statement_text(),
        "retrieval_profile": claude_core._council_retrieval_profile(
            problem_id="example", statement_sha256=digest
        ),
    }
    dispatches = [0]

    def invalid_council(**_arguments: object) -> dict[str, object]:
        dispatches[0] += 1
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": "council_" + "e" * 32,
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic risk."],
                "comparative_note": "Synthetic blind slate.",
            },
            "event_stream_sha256": "7" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", invalid_council)
    original_write_once = claude_core._write_once
    rejected_written = threading.Event()
    release_original = threading.Event()
    paused = [False]

    def pause_after_rejected(
        path: Path, value: object, *, mode: int = 0o600
    ) -> str:
        digest_value = original_write_once(path, value, mode=mode)
        if path.name == "blind_rejected_report.json" and not paused[0]:
            paused[0] = True
            rejected_written.set()
            if not release_original.wait(timeout=10):
                raise AssertionError("concurrent recovery did not release")
        return digest_value

    monkeypatch.setattr(claude_core, "_write_once", pause_after_rejected)
    arguments = {
        "phase": "blind",
        "council_id": council_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "request": request,
        "report_plan_set": None,
        "report_plan_sha256": None,
        "codex_bin": Path(sys.executable),
        "timeout_seconds": 60,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        original = executor.submit(claude_core._run_council_phase, **arguments)
        assert rejected_written.wait(timeout=10)
        recovery = executor.submit(claude_core._run_council_phase, **arguments)
        try:
            time.sleep(0.2)
            assert not recovery.done()
        finally:
            release_original.set()
        original_receipt = original.result(timeout=10)
        recovered = recovery.result(timeout=10)
    assert recovered == original_receipt
    assert recovered["status"] == "operational_blocked"
    assert dispatches == [1]


def test_owner_authorized_runtime_recovery_preserves_failed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    plans = _plans()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    input_root = tmp_path / "inputs"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=plans,
        root_session_id=root_session_id,
    )
    plan_raw = (claude_core.canonical_json(plan_set) + "\n").encode()
    plan_sha = hashlib.sha256(plan_raw).hexdigest()
    source_cohort_id = "cohort_" + plan_sha[:32]
    source_state = state_root / "example" / source_cohort_id
    source_state.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        source_state / f"plan_{plan_sha}.json", plan_set, mode=0o400
    )
    log_path = source_state / "executor.log"
    log_path.write_text("executor authentication failed\n", encoding="utf-8")
    frontier = "1" * 64
    failed_receipt = {
        "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
        "status": "failed",
        "cohort_id": source_cohort_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "plan_sha256": plan_sha,
        "root_session_id": root_session_id,
        "returncode": 1,
        "timed_out": False,
        "elapsed_seconds": 1.0,
        "frontier_before_sha256": frontier,
        "frontier_after_sha256": frontier,
        "frontier_changed": False,
        "log_path": str(log_path),
        "log_bytes": log_path.stat().st_size,
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "log_over_cap": False,
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        "retry_allowed": False,
        "completion_evidence": None,
    }
    receipt_path = source_state / "receipt.json"
    claude_core._write_once(receipt_path, failed_receipt, mode=0o400)
    original_receipt = receipt_path.read_bytes()
    authorization_barrier = threading.Barrier(2)

    def synchronized_login(codex_bin: Path) -> Path:
        authorization_barrier.wait(timeout=10)
        return Path(codex_bin)

    monkeypatch.setattr(
        claude_core, "_require_codex_login", synchronized_login
    )
    authorization_arguments = {
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "plan_sha256": plan_sha,
        "codex_bin": Path(sys.executable),
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent = [
            executor.submit(
                claude_core.authorize_failed_cohort_recovery,
                **authorization_arguments,
            )
            for _index in range(2)
        ]
        authorizations = [future.result(timeout=10) for future in concurrent]
    assert authorizations[0] == authorizations[1]
    authorization = authorizations[0]
    monkeypatch.setattr(
        claude_core,
        "_require_codex_login",
        lambda codex_bin: Path(codex_bin),
    )
    assert authorization["source_cohort_id"] == source_cohort_id
    assert authorization["recovery_cohort_id"] != source_cohort_id
    assert (
        claude_core.authorize_failed_cohort_recovery(
            **authorization_arguments,
        )
        == authorization
    )

    spawned: list[str] = []

    def fake_spawn(**arguments: object) -> None:
        cohort_id = str(arguments["cohort_id"])
        spawned.append(cohort_id)
        state_dir = arguments["state_dir"]
        assert isinstance(state_dir, Path)
        worker = {
            "schema_version": "rethlas_claude_cohort_worker_v1",
            "cohort_id": cohort_id,
            "problem_id": arguments["problem_id"],
            "statement_sha256": arguments["statement_sha256"],
            "plan_sha256": arguments["plan_sha256"],
            "root_session_id": arguments["root_session_id"],
            "worker_pid": os.getpid(),
            "started_at_unix": 1.0,
        }
        (state_dir / "worker.json").write_text(
            claude_core.canonical_json(worker) + "\n", encoding="utf-8"
        )

    monkeypatch.setattr(claude_core, "_spawn_cohort_worker", fake_spawn)
    pending = claude_core.run_three_route_cohort(
        problem_id="example",
        statement_sha256=digest,
        plans=plans,
        root_session_id=root_session_id,
        timeout_seconds=60,
        wait_seconds=0,
        codex_bin=Path(sys.executable),
    )
    assert pending["status"] == "running"
    assert spawned == [authorization["recovery_cohort_id"]]
    recovery_state = (
        state_root / "example" / authorization["recovery_cohort_id"]
    )
    assert (recovery_state / "intent.json").is_file()
    assert receipt_path.read_bytes() == original_receipt

    recovery_log = recovery_state / "executor.log"
    recovery_log.write_text("second recoverable runtime failure\n", encoding="utf-8")
    recovery_receipt = {
        **failed_receipt,
        "cohort_id": authorization["recovery_cohort_id"],
        "log_path": str(recovery_log),
        "log_bytes": recovery_log.stat().st_size,
        "log_sha256": hashlib.sha256(recovery_log.read_bytes()).hexdigest(),
    }
    recovery_receipt_path = recovery_state / "receipt.json"
    claude_core._write_once(
        recovery_receipt_path, recovery_receipt, mode=0o400
    )
    second_authorization = claude_core.authorize_failed_cohort_recovery(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        plan_sha256=plan_sha,
        codex_bin=Path(sys.executable),
        source_cohort_id=authorization["recovery_cohort_id"],
    )
    assert second_authorization["source_cohort_id"] == (
        authorization["recovery_cohort_id"]
    )
    assert second_authorization["recovery_cohort_id"] not in {
        source_cohort_id,
        authorization["recovery_cohort_id"],
    }
    second_pending = claude_core.run_three_route_cohort(
        problem_id="example",
        statement_sha256=digest,
        plans=plans,
        root_session_id=root_session_id,
        timeout_seconds=60,
        wait_seconds=0,
        codex_bin=Path(sys.executable),
    )
    assert second_pending["status"] == "running"
    assert spawned == [
        authorization["recovery_cohort_id"],
        second_authorization["recovery_cohort_id"],
    ]
    assert receipt_path.read_bytes() == original_receipt
    terminal_cohort_id, recovery_chain = (
        claude_core._resolve_terminal_cohort_id(
            problem_id="example",
            statement_sha256=digest,
            plan_sha256=plan_sha,
            root_session_id=root_session_id,
            initial_cohort_id=source_cohort_id,
        )
    )
    assert terminal_cohort_id == second_authorization["recovery_cohort_id"]
    assert recovery_chain == {
        source_cohort_id,
        authorization["recovery_cohort_id"],
        second_authorization["recovery_cohort_id"],
    }
    real_cancellation_lock = claude_core._cohort_recovery_cancellation_lock
    cancellation_barrier = threading.Barrier(2)
    cancellation_lock_entries: list[str] = []

    @contextmanager
    def synchronized_cancellation_lock(state_dir: Path):
        cancellation_lock_entries.append(state_dir.name)
        cancellation_barrier.wait(timeout=10)
        with real_cancellation_lock(state_dir):
            yield

    monkeypatch.setattr(
        claude_core,
        "_cohort_recovery_cancellation_lock",
        synchronized_cancellation_lock,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancellations = [
            executor.submit(claude_core._cancel_cohort_recoveries, "example")
            for _index in range(2)
        ]
        cancelled_results = [future.result(timeout=10) for future in cancellations]
    assert cancelled_results[0] == cancelled_results[1]
    assert len(cancellation_lock_entries) == 4
    first_cancelled = cancelled_results[0]
    cancellation_paths = [
        source_state / "recovery_cancellation.json",
        recovery_state / "recovery_cancellation.json",
    ]
    cancellation_bytes = [path.read_bytes() for path in cancellation_paths]
    monkeypatch.setattr(
        claude_core,
        "_cohort_recovery_cancellation_lock",
        real_cancellation_lock,
    )
    second_cancelled = claude_core._cancel_cohort_recoveries("example")
    assert second_cancelled == first_cancelled
    assert [path.read_bytes() for path in cancellation_paths] == cancellation_bytes


def test_recovery_authorization_fences_nonterminal_and_depth_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    statement_sha256 = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=statement_sha256,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    frontier_sha256 = "7" * 64
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier_sha256},
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )

    def write_failed_terminal(cohort_id: str, index: int) -> None:
        state_dir = state_root / "example" / cohort_id
        state_dir.mkdir(parents=True, mode=0o700)
        claude_core._write_once(
            state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
        )
        log_path = state_dir / "executor.log"
        log_raw = f"recoverable failure {index}\n".encode()
        log_path.write_bytes(log_raw)
        claude_core._write_once(
            state_dir / "receipt.json",
            {
                "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
                "status": "failed",
                "cohort_id": cohort_id,
                "problem_id": "example",
                "statement_sha256": statement_sha256,
                "plan_sha256": plan_sha256,
                "root_session_id": root_session_id,
                "returncode": 70,
                "timed_out": False,
                "elapsed_seconds": 1.0,
                "frontier_before_sha256": frontier_sha256,
                "frontier_after_sha256": frontier_sha256,
                "frontier_changed": False,
                "log_path": str(log_path),
                "log_bytes": len(log_raw),
                "log_sha256": hashlib.sha256(log_raw).hexdigest(),
                "log_over_cap": False,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
                "retry_allowed": False,
                "completion_evidence": None,
            },
            mode=0o400,
        )

    initial_cohort_id = "cohort_" + plan_sha256[:32]
    terminal_cohort_id = initial_cohort_id
    first_authorization: dict[str, object] | None = None
    for depth in range(claude_core.MAX_COHORT_RECOVERY_DEPTH + 1):
        write_failed_terminal(terminal_cohort_id, depth)
        if depth == claude_core.MAX_COHORT_RECOVERY_DEPTH:
            break
        authorization = claude_core.authorize_failed_cohort_recovery(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            plan_sha256=plan_sha256,
            codex_bin=Path(sys.executable),
            source_cohort_id=terminal_cohort_id,
        )
        if first_authorization is None:
            first_authorization = authorization
        terminal_cohort_id = str(authorization["recovery_cohort_id"])

    resolved, ordered_cohorts, authorization_chain = (
        claude_core._resolve_terminal_cohort_chain(
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
            initial_cohort_id=initial_cohort_id,
        )
    )
    assert resolved == terminal_cohort_id
    assert len(authorization_chain) == claude_core.MAX_COHORT_RECOVERY_DEPTH
    assert len(ordered_cohorts) == claude_core.MAX_COHORT_RECOVERY_DEPTH + 1
    terminal_authorization_path = (
        state_root
        / "example"
        / terminal_cohort_id
        / "recovery_authorization.json"
    )
    assert not terminal_authorization_path.exists()
    with pytest.raises(claude_core.ClaudeCoreError, match="depth is exhausted"):
        claude_core.authorize_failed_cohort_recovery(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            plan_sha256=plan_sha256,
            codex_bin=Path(sys.executable),
            source_cohort_id=terminal_cohort_id,
        )
    assert not terminal_authorization_path.exists()

    assert first_authorization is not None
    assert claude_core.authorize_failed_cohort_recovery(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        plan_sha256=plan_sha256,
        codex_bin=Path(sys.executable),
        source_cohort_id=initial_cohort_id,
    ) == first_authorization

    disconnected_cohort_id = "cohort_" + "f" * 32
    write_failed_terminal(disconnected_cohort_id, 99)
    with pytest.raises(claude_core.ClaudeCoreError, match="active terminal"):
        claude_core.authorize_failed_cohort_recovery(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            plan_sha256=plan_sha256,
            codex_bin=Path(sys.executable),
            source_cohort_id=disconnected_cohort_id,
        )
    assert not (
        state_root
        / "example"
        / disconnected_cohort_id
        / "recovery_authorization.json"
    ).exists()
    terminal_receipt = json.loads(
        (state_root / "example" / terminal_cohort_id / "receipt.json").read_text()
    )
    assert claude_core._cohort_recovery_budget_exhausted(
        receipt=terminal_receipt, seen_cohorts=set(ordered_cohorts)
    )


@pytest.mark.parametrize(
    ("source_status", "frontier_mode"),
    [
        ("failed", "unchanged"),
        ("failed", "exact_checkpoint"),
        ("failed", "extra_delta"),
        ("no_progress", "unchanged"),
        ("timeout", "unchanged"),
        ("timeout", "exact_checkpoint"),
        ("timeout", "extra_delta"),
        ("output_limit", "unchanged"),
        ("output_limit", "exact_checkpoint"),
        ("output_limit", "extra_delta"),
    ],
)
def test_owner_recovery_admits_every_recoverable_terminal_status_only_with_safe_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_status: str,
    frontier_mode: str,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda _problem_id, _statement_sha256: None,
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    source_cohort_id = "cohort_" + plan_sha256[:32]
    source_state = state_root / "example" / source_cohort_id
    source_state.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        source_state / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    log_path = source_state / "executor.log"
    log_path.write_bytes(b"x")
    if source_status == "output_limit":
        monkeypatch.setattr(claude_core, "MAX_REPORT_LOG_BYTES", 0)
    frontier_before = "1" * 64
    frontier_after = (
        frontier_before if frontier_mode == "unchanged" else "2" * 64
    )
    returncode = 0 if source_status == "no_progress" else 70
    timed_out = source_status == "timeout"
    log_over_cap = source_status == "output_limit"
    receipt = {
        "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
        "status": source_status,
        "cohort_id": source_cohort_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_seconds": 1.0,
        "frontier_before_sha256": frontier_before,
        "frontier_after_sha256": frontier_after,
        "frontier_changed": frontier_before != frontier_after,
        "log_path": str(log_path),
        "log_bytes": log_path.stat().st_size,
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "log_over_cap": log_over_cap,
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        "retry_allowed": False,
        "completion_evidence": None,
    }
    claude_core._write_once(source_state / "receipt.json", receipt, mode=0o400)
    record_ids = [
        "mem_" + "3" * 64,
        "mem_" + "4" * 64,
        "mem_" + "5" * 64,
        "event_" + "6" * 64,
    ]
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier_after},
    )
    monkeypatch.setattr(
        claude_core,
        "_external_plan_checkpoint_evidence",
        lambda **_arguments: {
            "batch_id": "batch_" + "7" * 64,
            "record_ids": record_ids,
        },
    )

    class FrontierLegacy:
        @staticmethod
        def legacy_frontier_receipt_without_records(
            _problem_id: str, excluded: list[str]
        ) -> dict[str, str]:
            assert excluded == record_ids
            return {
                "frontier_sha256": (
                    frontier_before
                    if frontier_mode == "exact_checkpoint"
                    else "8" * 64
                )
            }

    monkeypatch.setattr(claude_core, "_legacy", lambda: FrontierLegacy())
    arguments = {
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": root_session_id,
        "plan_sha256": plan_sha256,
        "codex_bin": Path(sys.executable),
        "source_cohort_id": source_cohort_id,
    }
    authorization_path = source_state / "recovery_authorization.json"
    if frontier_mode == "extra_delta":
        with pytest.raises(claude_core.ClaudeCoreError, match="changed more than"):
            claude_core.authorize_failed_cohort_recovery(**arguments)
        assert not authorization_path.exists()
        return

    authorization = claude_core.authorize_failed_cohort_recovery(**arguments)
    assert authorization["schema_version"] == claude_core.COHORT_RECOVERY_SCHEMA
    assert authorization["source_receipt_status"] == source_status
    assert authorization["frontier_recovery_mode"] == (
        "unchanged"
        if frontier_mode == "unchanged"
        else "external_plan_checkpoint_only"
    )
    if source_status == "failed" and frontier_mode == "unchanged":
        previous = dict(authorization)
        previous["schema_version"] = claude_core.COHORT_RECOVERY_SCHEMA_PREVIOUS
        previous.pop("source_receipt_status")
        claude_core._replace_canonical(authorization_path, previous)
        assert claude_core._read_cohort_recovery_authorization(
            authorization_path,
            source_state_dir=source_state,
            source_cohort_id=source_cohort_id,
            problem_id="example",
            statement_sha256=digest,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        ) == previous


def test_recovery_frontier_accepts_only_exact_plan_checkpoint_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = "1" * 64
    after = "2" * 64
    record_ids = [
        "mem_" + "3" * 64,
        "mem_" + "4" * 64,
        "mem_" + "5" * 64,
        "event_" + "6" * 64,
    ]
    receipt = {
        "frontier_changed": True,
        "frontier_before_sha256": before,
        "frontier_after_sha256": after,
    }
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": after},
    )
    monkeypatch.setattr(
        claude_core,
        "_external_plan_checkpoint_evidence",
        lambda **_arguments: {
            "batch_id": "batch_" + "7" * 64,
            "record_ids": record_ids,
        },
    )

    class ExactLegacy:
        @staticmethod
        def legacy_frontier_receipt_without_records(
            problem_id: str, excluded: list[str]
        ) -> dict[str, str]:
            assert problem_id == "example"
            assert excluded == record_ids
            return {"frontier_sha256": before}

    monkeypatch.setattr(claude_core, "_legacy", lambda: ExactLegacy())
    evidence = claude_core._classify_cohort_recovery_frontier(
        problem_id="example", plan_set={"plans": _plans()}, receipt=receipt
    )
    assert evidence == {
        "frontier_recovery_mode": "external_plan_checkpoint_only",
        "replay_safe_batch_id": "batch_" + "7" * 64,
        "replay_safe_record_ids": record_ids,
    }

    class ExtraProgressLegacy:
        @staticmethod
        def legacy_frontier_receipt_without_records(
            _problem_id: str, _excluded: list[str]
        ) -> dict[str, str]:
            return {"frontier_sha256": "8" * 64}

    monkeypatch.setattr(claude_core, "_legacy", lambda: ExtraProgressLegacy())
    with pytest.raises(claude_core.ClaudeCoreError, match="changed more than"):
        claude_core._classify_cohort_recovery_frontier(
            problem_id="example", plan_set={"plans": _plans()}, receipt=receipt
        )


def test_completed_unverified_requires_exact_reports_and_stop_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = _plans()
    plan_ids = [str(plan["plan_id"]) for plan in plans]
    before = "d" * 64
    after = "e" * 64
    checkpoints: list[dict[str, object]] = []
    report_ids: list[str] = []
    report_event_ids: list[str] = []
    for index, plan_id in enumerate(plan_ids, start=1):
        report_text = f"route {index} found no counterexample"
        report_id = "mem_" + str(index) * 64
        event_id = "event_" + str(index + 3) * 64
        report_ids.append(report_id)
        report_event_ids.append(event_id)
        checkpoints.append(
            {
                "commit_sha256": "a" * 64,
                "records": [
                    {
                        "record_id": report_id,
                        "channel": "failed_paths",
                        "active": True,
                        "supersedes": [],
                        "record": {
                            "schema_version": "rethlas_route_terminal_report_v1",
                            "thread_id": f"thread_{index}",
                            "plan_id": plan_id,
                            "status": "blocked",
                            "report_text": report_text,
                            "report_sha256": hashlib.sha256(
                                report_text.encode()
                            ).hexdigest(),
                            "remaining_obligations": [f"obligation {index}"],
                            "decisive_stuck_points": [f"stuck point {index}"],
                        },
                    }
                ],
                "event": {"record_id": event_id},
            }
        )
    synthesis_id = "mem_" + "7" * 64
    synthesis_event_id = "event_" + "8" * 64
    checkpoints.append(
        {
            "commit_sha256": "b" * 64,
            "records": [
                {
                    "record_id": synthesis_id,
                    "channel": "failed_paths",
                    "active": True,
                    "supersedes": [],
                    "record": {
                        "schema_version": "rethlas_round_failure_synthesis_v1",
                        "record_type": "key_failures_summary",
                        "route_report_record_ids": report_ids,
                        "failed_plan_ids": plan_ids,
                        "plan_failures": [
                            {
                                "plan_id": plan_id,
                                "stuck_points": [f"stuck point {index}"],
                            }
                            for index, plan_id in enumerate(plan_ids, start=1)
                        ],
                        "common_failures": ["one shared obstruction"],
                        "implications_for_next_plans": ["use a new mechanism"],
                        "next_state": "stop_unsolved",
                    },
                }
            ],
            "event": {"record_id": synthesis_event_id},
        }
    )
    excluded = []
    for report_id, event_id in zip(report_ids, report_event_ids, strict=True):
        excluded.extend([report_id, event_id])
    excluded.extend([synthesis_id, synthesis_event_id])
    external_plan_ids = [
        "mem_" + "9" * 64,
        "mem_" + "a" * 64,
        "mem_" + "b" * 64,
        "event_" + "c" * 64,
    ]
    real_legacy = claude_core._legacy()

    class FakeLegacy:
        _checkpoint_normalized_items = staticmethod(
            real_legacy._checkpoint_normalized_items
        )
        _validate_route_terminal_memory_record = staticmethod(
            real_legacy._validate_route_terminal_memory_record
        )
        _round_text_list = staticmethod(real_legacy._round_text_list)

        def __init__(
            self,
            visible: list[dict[str, object]],
            expected_exclusions: list[str] | None,
        ) -> None:
            self.visible = visible
            self.expected_exclusions = expected_exclusions

        def _iter_memory_batch_checkpoints(
            self, _problem_id: str
        ) -> object:
            return iter(self.visible)

        def _load_memory_entries(
            self, _problem_id: str
        ) -> dict[str, list[dict[str, object]]]:
            by_channel: dict[str, list[dict[str, object]]] = {}
            ordinal = 0
            for checkpoint in self.visible:
                records = checkpoint.get("records")
                assert isinstance(records, list)
                for raw_item in records:
                    assert isinstance(raw_item, dict)
                    channel = raw_item.get("channel")
                    record_id = raw_item.get("record_id")
                    assert isinstance(channel, str)
                    assert isinstance(record_id, str)
                    by_channel.setdefault(channel, []).append(
                        {
                            "record_id": record_id,
                            "item": raw_item,
                            "channel": channel,
                            "effective_active": True,
                            "ordinal": ordinal,
                        }
                    )
                    ordinal += 1
            return by_channel

        def legacy_frontier_receipt_without_records(
            self, _problem_id: str, record_ids: list[str]
        ) -> dict[str, str]:
            return {
                "frontier_sha256": (
                    before
                    if record_ids == self.expected_exclusions
                    else "f" * 64
                )
            }

    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": after},
    )
    monkeypatch.setattr(
        claude_core,
        "_external_plan_checkpoint_evidence",
        lambda **_arguments: {
            "batch_id": "batch_" + "1" * 64,
            "record_ids": external_plan_ids,
        },
    )
    monkeypatch.setattr(
        claude_core, "_legacy", lambda: FakeLegacy(checkpoints, excluded)
    )
    evidence = claude_core._completed_unverified_cohort_evidence(
        problem_id="example",
        plan_set={"plans": plans},
        frontier_before_sha256=before,
        frontier_after_sha256=after,
    )
    assert evidence == {
        "schema_version": claude_core.COHORT_COMPLETION_EVIDENCE_SCHEMA,
        "route_report_record_ids": report_ids,
        "synthesis_record_id": synthesis_id,
        "external_plan_checkpoint_in_delta": False,
    }
    handoff = claude_core._cohort_completion_handoff(
        problem_id="example",
        plan_set={"plans": plans},
        completion_evidence=evidence,
    )
    assert handoff["schema_version"] == (
        claude_core.COHORT_COMPLETION_HANDOFF_SCHEMA
    )
    assert handoff["status"] == "available"
    assert [report["record_id"] for report in handoff["route_reports"]] == (
        report_ids
    )
    assert [report["plan_id"] for report in handoff["route_reports"]] == plan_ids
    assert [report["report_text"] for report in handoff["route_reports"]] == [
        f"route {index} found no counterexample" for index in range(1, 4)
    ]
    assert handoff["synthesis"] == {
        "record_id": synthesis_id,
        "plan_failures": [
            {"plan_id": plan_id, "stuck_points": [f"stuck point {index}"]}
            for index, plan_id in enumerate(plan_ids, start=1)
        ],
        "common_failures": ["one shared obstruction"],
        "implications_for_next_plans": ["use a new mechanism"],
        "next_state": "stop_unsolved",
    }

    monkeypatch.setattr(
        claude_core,
        "_legacy",
        lambda: FakeLegacy(checkpoints, [*excluded, *external_plan_ids]),
    )
    evidence = claude_core._completed_unverified_cohort_evidence(
        problem_id="example",
        plan_set={"plans": plans},
        frontier_before_sha256=before,
        frontier_after_sha256=after,
    )
    assert evidence == {
        "schema_version": claude_core.COHORT_COMPLETION_EVIDENCE_SCHEMA,
        "route_report_record_ids": report_ids,
        "synthesis_record_id": synthesis_id,
        "external_plan_checkpoint_in_delta": True,
    }

    # Once a v2 receipt has bound these content-addressed records, later legal
    # memory batches may advance the live frontier.  Historical validation
    # must find the exact immutable records without reconstructing the old
    # frontier as if it were still current.
    persisted_evidence = {
        **evidence,
        "external_plan_checkpoint_in_delta": False,
    }
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": "0" * 64},
    )
    monkeypatch.setattr(
        claude_core, "_legacy", lambda: FakeLegacy(checkpoints, None)
    )
    assert claude_core._completed_unverified_cohort_evidence(
        problem_id="example",
        plan_set={"plans": plans},
        frontier_before_sha256=before,
        frontier_after_sha256=after,
        expected_evidence=persisted_evidence,
    ) == persisted_evidence
    assert (
        claude_core._completed_unverified_cohort_evidence(
            problem_id="example",
            plan_set={"plans": plans},
            frontier_before_sha256=before,
            frontier_after_sha256=after,
            expected_evidence={
                **persisted_evidence,
                "synthesis_record_id": "mem_" + "6" * 64,
            },
        )
        is None
    )

    monkeypatch.setattr(
        claude_core, "_legacy", lambda: FakeLegacy(checkpoints[:-1], excluded)
    )
    assert (
        claude_core._completed_unverified_cohort_evidence(
            problem_id="example",
            plan_set={"plans": plans},
            frontier_before_sha256=before,
            frontier_after_sha256=after,
        )
        is None
    )
    monkeypatch.setattr(
        claude_core, "_legacy", lambda: FakeLegacy(checkpoints, None)
    )
    assert (
        claude_core._completed_unverified_cohort_evidence(
            problem_id="example",
            plan_set={"plans": plans},
            frontier_before_sha256=before,
            frontier_after_sha256=after,
        )
        is None
    )


def test_cohort_receipt_rejects_inconsistent_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "cohort"
    state_dir.mkdir()
    log_path = state_dir / "executor.log"
    log_path.write_text("done\n", encoding="utf-8")
    digest = "1" * 64
    receipt = {
        "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
        "status": "completed",
        "cohort_id": "cohort_" + "2" * 32,
        "problem_id": "example",
        "statement_sha256": "3" * 64,
        "plan_sha256": "2" * 64,
        "root_session_id": "12345678-1234-4123-8123-123456789abc",
        "returncode": 0,
        "timed_out": False,
        "elapsed_seconds": 1.0,
        "frontier_before_sha256": digest,
        "frontier_after_sha256": digest,
        "frontier_changed": False,
        "log_path": str(log_path),
        "log_bytes": log_path.stat().st_size,
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "log_over_cap": False,
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        "retry_allowed": False,
        "completion_evidence": None,
    }
    receipt_path = state_dir / "receipt.json"
    receipt_path.write_text(
        claude_core.canonical_json(receipt) + "\n", encoding="utf-8"
    )

    with pytest.raises(claude_core.ClaudeCoreError, match="status is inconsistent"):
        claude_core._read_matching_cohort_receipt(
            receipt_path,
            state_dir=state_dir,
            cohort_id=receipt["cohort_id"],
            problem_id="example",
            statement_sha256=receipt["statement_sha256"],
            plan_sha256=receipt["plan_sha256"],
            root_session_id=receipt["root_session_id"],
        )

    receipt.update(
        {
            "status": "completed_unverified",
            "returncode": 1,
            "frontier_after_sha256": "2" * 64,
            "frontier_changed": True,
            "completion_evidence": _completion_evidence(),
        }
    )
    receipt_path.write_text(
        claude_core.canonical_json(receipt) + "\n", encoding="utf-8"
    )
    assert (
        claude_core._read_matching_cohort_receipt(
            receipt_path,
            state_dir=state_dir,
            cohort_id=receipt["cohort_id"],
            problem_id="example",
            statement_sha256=receipt["statement_sha256"],
            plan_sha256=receipt["plan_sha256"],
            root_session_id=receipt["root_session_id"],
        )
        == receipt
    )
    monkeypatch.setattr(claude_core, "MAX_REPORT_LOG_BYTES", 0)
    assert (
        claude_core._read_matching_cohort_receipt(
            receipt_path,
            state_dir=state_dir,
            cohort_id=receipt["cohort_id"],
            problem_id="example",
            statement_sha256=receipt["statement_sha256"],
            plan_sha256=receipt["plan_sha256"],
            root_session_id=receipt["root_session_id"],
        )
        == receipt
    )
    receipt["returncode"] = 0
    receipt_path.write_text(
        claude_core.canonical_json(receipt) + "\n", encoding="utf-8"
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="status is inconsistent"):
        claude_core._read_matching_cohort_receipt(
            receipt_path,
            state_dir=state_dir,
            cohort_id=receipt["cohort_id"],
            problem_id="example",
            statement_sha256=receipt["statement_sha256"],
            plan_sha256=receipt["plan_sha256"],
            root_session_id=receipt["root_session_id"],
        )


@pytest.mark.parametrize(
    ("runner_returncode", "expected_status"),
    [(0, "completed"), (1, "completed_unverified")],
)
def test_cohort_worker_settles_receipt_without_a_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_returncode: int,
    expected_status: str,
) -> None:
    generation_root = tmp_path / "generation"
    source = generation_root / "data" / "example.md"
    source.parent.mkdir(parents=True)
    source.write_text("Statement.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    state_root = tmp_path / "state"
    input_root = generation_root / ".claude_core_inputs"
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=statement_sha256,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_raw = (claude_core.canonical_json(plan_set) + "\n").encode()
    plan_sha256 = hashlib.sha256(plan_raw).hexdigest()
    cohort_id = "cohort_" + plan_sha256[:32]
    state_dir = state_root / "example" / cohort_id
    input_dir = input_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    input_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        input_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    runner = tmp_path / "fake-runner.sh"
    runner.write_text(
        f"#!/bin/sh\necho cohort-finished\nexit {runner_returncode}\n",
        encoding="utf-8",
    )
    runner.chmod(0o700)
    monkeypatch.setattr(claude_core, "RUNNER", runner)
    runner_closure_sha256 = "9" * 64
    monkeypatch.setattr(
        claude_core,
        "_cohort_runner_closure_sha256",
        lambda _runner: runner_closure_sha256,
    )
    codex_executable = Path(sys.executable).resolve(strict=True)
    intent = {
        "schema_version": claude_core.COHORT_INTENT_SCHEMA,
        "state": "submitted",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "timeout_seconds": 60,
        "runner_path": str(runner.resolve(strict=True)),
        "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "runner_closure_sha256": runner_closure_sha256,
        "codex_bin": str(codex_executable),
        "codex_bin_sha256": hashlib.sha256(
            codex_executable.read_bytes()
        ).hexdigest(),
        "host_source_sha256": claude_core._host_source_sha256(),
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        "created_at_unix": 1.0,
    }
    claude_core._write_once(state_dir / "intent.json", intent, mode=0o600)
    frontiers = iter(["1" * 64, "2" * 64])
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": next(frontiers)},
    )
    evidence_calls: list[dict[str, object]] = []

    def completed_unverified_evidence(**arguments: object) -> object:
        evidence_calls.append(arguments)
        return _completion_evidence()

    monkeypatch.setattr(
        claude_core,
        "_completed_unverified_cohort_evidence",
        completed_unverified_evidence,
    )

    receipt = claude_core._execute_cohort_worker(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        plan_sha256=plan_sha256,
        timeout_seconds=60,
    )

    assert receipt["status"] == expected_status
    assert receipt["frontier_changed"] is True
    assert receipt["retry_allowed"] is False
    assert len(evidence_calls) == (1 if runner_returncode == 1 else 0)
    assert (state_dir / "executor.log").read_text(encoding="utf-8") == (
        "cohort-finished\n"
    )
    assert (
        claude_core._read_matching_cohort_receipt(
            state_dir / "receipt.json",
            state_dir=state_dir,
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        )
        == receipt
    )


@pytest.mark.parametrize("exact_terminal_delta", [True, False])
def test_stopped_cohort_reconciliation_recovers_exact_unverified_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exact_terminal_delta: bool,
) -> None:
    statement_sha256 = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=statement_sha256,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_sha256 = claude_core._plan_set_sha256(plan_set)
    cohort_id = "cohort_" + plan_sha256[:32]
    state_dir = tmp_path / cohort_id
    state_dir.mkdir()
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        state_dir / "intent.json",
        _current_cohort_intent(
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        ),
        mode=0o600,
    )
    frontier_before = "2" * 64
    frontier_after = "3" * 64
    claude_core._write_once(
        state_dir / "worker.json",
        {
            "schema_version": claude_core.COHORT_WORKER_SCHEMA,
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": statement_sha256,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "worker_pid": 99_999_999,
            "started_at_unix": time.time(),
            "frontier_before_sha256": frontier_before,
            "worker_start_token": "4" * 64,
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        },
        mode=0o400,
    )
    (state_dir / "executor.log").write_text(
        "runner stopped after its exact unsolved round\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier_after},
    )
    monkeypatch.setattr(
        claude_core, "_terminate_owned_command_wrappers", lambda *_args: None
    )
    evidence_calls: list[dict[str, object]] = []

    def recognize(**arguments: object) -> object:
        evidence_calls.append(arguments)
        assert arguments["plan_set"] == plan_set
        assert arguments["frontier_before_sha256"] == frontier_before
        assert arguments["frontier_after_sha256"] == frontier_after
        if not exact_terminal_delta:
            return None
        return _completion_evidence()

    monkeypatch.setattr(
        claude_core, "_completed_unverified_cohort_evidence", recognize
    )

    receipt = claude_core._settle_stopped_cohort_worker(
        state_dir=state_dir,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=statement_sha256,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
    )

    assert receipt is not None
    assert receipt["status"] == (
        "completed_unverified" if exact_terminal_delta else "failed"
    )
    assert receipt["returncode"] == (1 if exact_terminal_delta else 70)
    assert len(evidence_calls) == 1
    assert (
        claude_core._read_matching_cohort_receipt(
            state_dir / "receipt.json",
            state_dir=state_dir,
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        )
        == receipt
    )


@pytest.mark.parametrize(
    "lifeline_loss_phase",
    [
        "checkpointed",
        "consumed",
        "source_drift",
        "source_drift_fence_rollback",
        "source_drift_fence_after_consumption",
    ],
)
def test_council_worker_handoff_survives_parent_lifeline_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lifeline_loss_phase: str,
) -> None:
    generation_root = tmp_path / "generation"
    source = generation_root / "data" / "example.md"
    source.parent.mkdir(parents=True)
    source.write_text("Statement.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    state_root = tmp_path / "state"
    input_root = generation_root / ".claude_core_inputs"
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", input_root)
    root_manifest = _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=statement_sha256,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_raw = (claude_core.canonical_json(plan_set) + "\n").encode()
    plan_sha256 = hashlib.sha256(plan_raw).hexdigest()
    cohort_id = "cohort_" + plan_sha256[:32]
    council_id = "council_" + "7" * 32
    acceptance_sha256 = "8" * 64
    checkpoint_sha256 = "9" * 64
    state_dir = state_root / "example" / cohort_id
    input_dir = input_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    input_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        input_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    runner = tmp_path / "fake-runner.sh"
    runner.write_text(
        "#!/bin/sh\nsleep 1\necho cohort-finished\n", encoding="utf-8"
    )
    runner.chmod(0o700)
    monkeypatch.setattr(claude_core, "RUNNER", runner)
    runner_closure_sha256 = "a" * 64
    monkeypatch.setattr(
        claude_core,
        "_cohort_runner_closure_sha256",
        lambda _runner: runner_closure_sha256,
    )
    codex_executable = Path(sys.executable).resolve(strict=True)
    intent = {
        "schema_version": claude_core.COHORT_INTENT_SCHEMA,
        "state": "submitted",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "timeout_seconds": 60,
        "runner_path": str(runner.resolve(strict=True)),
        "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "runner_closure_sha256": runner_closure_sha256,
        "codex_bin": str(codex_executable),
        "codex_bin_sha256": hashlib.sha256(
            codex_executable.read_bytes()
        ).hexdigest(),
        "host_source_sha256": root_manifest["host_source_sha256"],
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        "created_at_unix": 1.0,
    }
    claude_core._write_once(state_dir / "intent.json", intent, mode=0o600)
    pointer_state = "checkpointed"

    def council_admission(*_arguments: object, **_keywords: object) -> dict[str, object]:
        return {
            "council_id": council_id,
            "acceptance_sha256": acceptance_sha256,
        }

    def council_pointer(*_arguments: object, **_keywords: object) -> dict[str, object]:
        return {
            "state": pointer_state,
            "council_id": council_id,
            "final_plan_sha256": plan_sha256,
            "acceptance_sha256": acceptance_sha256,
            "checkpoint_sha256": checkpoint_sha256,
            "cohort_id": cohort_id if pointer_state == "consumed" else None,
        }

    monkeypatch.setattr(
        claude_core, "_validate_council_admission", council_admission
    )
    monkeypatch.setattr(claude_core, "_read_council_pointer", council_pointer)
    frontiers = iter(["1" * 64, "2" * 64])
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": next(frontiers)},
    )
    host_source_sha256 = [str(root_manifest["host_source_sha256"])]
    monkeypatch.setattr(
        claude_core,
        "_host_source_sha256",
        lambda: host_source_sha256[0],
    )
    launch_window_entered = threading.Event()
    release_launch_window = threading.Event()
    if lifeline_loss_phase == "source_drift_fence_after_consumption":
        real_safe_subprocess_environment = (
            claude_core._safe_subprocess_environment
        )

        def pause_after_consumption() -> dict[str, str]:
            launch_window_entered.set()
            assert release_launch_window.wait(timeout=10)
            return real_safe_subprocess_environment()

        monkeypatch.setattr(
            claude_core,
            "_safe_subprocess_environment",
            pause_after_consumption,
        )

    def commit_source_drift_fence(initial_pointer_state: str) -> None:
        problem_dir = state_root / "example"
        manifest_path = (
            problem_dir / "roots" / root_session_id / "manifest.json"
        )
        with claude_core._root_authority_lock("example"):
            claude_core._ensure_root_source_drift_fence_unlocked(
                problem_dir=problem_dir,
                problem_id="example",
                statement_sha256=statement_sha256,
                root_session_id=root_session_id,
                council_id=council_id,
                root_manifest_sha256=hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                old_host_source_sha256=host_source_sha256[0],
                replacement_host_source_sha256="b" * 64,
                initial_pointer_state=initial_pointer_state,
                initial_pointer_sha256="c" * 64,
                reason_sha256="d" * 64,
            )

    lifeline_read, lifeline_write = os.pipe()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            execution = executor.submit(
                claude_core._execute_cohort_worker,
                problem_id="example",
                statement_sha256=statement_sha256,
                root_session_id=root_session_id,
                plan_sha256=plan_sha256,
                timeout_seconds=60,
                cohort_id=cohort_id,
                lifeline_fd=lifeline_read,
            )
            deadline = time.monotonic() + 5
            while not (state_dir / "worker.json").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert not (state_dir / "executor.json").exists()
            if lifeline_loss_phase in {
                "checkpointed",
                "source_drift",
                "source_drift_fence_rollback",
            }:
                os.close(lifeline_write)
                lifeline_write = -1
                time.sleep(0.1)
                assert not (state_dir / "executor.json").exists()
                if lifeline_loss_phase == "source_drift":
                    host_source_sha256[0] = "b" * 64
                elif lifeline_loss_phase == "source_drift_fence_rollback":
                    # The live file hash has rolled back to the worker's A
                    # source, but a B migration already committed the
                    # irreversible old-root fence before finding this lock.
                    commit_source_drift_fence("checkpointed")
                else:
                    pointer_state = "consumed"
            elif lifeline_loss_phase == "source_drift_fence_after_consumption":
                pointer_state = "consumed"
                assert launch_window_entered.wait(timeout=10)
                commit_source_drift_fence("consumed")
                release_launch_window.set()
                os.close(lifeline_write)
                lifeline_write = -1
            else:
                pointer_state = "consumed"
                while not (state_dir / "executor.json").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                os.close(lifeline_write)
                lifeline_write = -1
            receipt = execution.result(timeout=10)
    finally:
        release_launch_window.set()
        if lifeline_write >= 0:
            os.close(lifeline_write)
        os.close(lifeline_read)
    if lifeline_loss_phase in {
        "source_drift",
        "source_drift_fence_rollback",
        "source_drift_fence_after_consumption",
    }:
        assert receipt["status"] == "failed"
        assert receipt["frontier_changed"] is False
        assert not (state_dir / "executor.json").exists()
    else:
        assert receipt["status"] == "completed"
        assert (state_dir / "executor.json").is_file()


@pytest.mark.parametrize(
    "intent_schema",
    [
        claude_core.COHORT_INTENT_SCHEMA_LEGACY,
        claude_core.COHORT_INTENT_SCHEMA,
    ],
)
def test_owner_migration_fences_intent_and_allows_fresh_council_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent_schema: str,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    old_root = "12345678-1234-4123-8123-123456789abc"
    new_root = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    source_a = "a" * 64
    source_b = "b" * 64
    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_a)
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=old_root,
    )
    plan_raw = (claude_core.canonical_json(plan_set) + "\n").encode()
    plan_sha256 = hashlib.sha256(plan_raw).hexdigest()
    cohort_id = "cohort_" + "7" * 32
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    intent = {
        "schema_version": intent_schema,
        "state": "submitted",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "plan_sha256": plan_sha256,
        "root_session_id": old_root,
        "created_at_unix": time.time(),
    }
    if intent_schema == claude_core.COHORT_INTENT_SCHEMA:
        intent.update(
            {
                "timeout_seconds": 60,
                "runner_path": str(claude_core.RUNNER),
                "runner_sha256": "9" * 64,
                "runner_closure_sha256": "a" * 64,
                "codex_bin": str(Path(sys.executable).resolve()),
                "codex_bin_sha256": "b" * 64,
                "host_source_sha256": source_a,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            }
        )
    claude_core._write_once(state_dir / "intent.json", intent, mode=0o600)
    council_id = "council_" + "8" * 32
    council_dir = claude_core._council_dir("example", council_id)
    claude_core._write_once(
        council_dir / "final_plan.json", plan_set, mode=0o400
    )
    pointer_path = claude_core._council_pointer_path("example", old_root)
    monkeypatch.setattr(
        claude_core,
        "_external_plan_checkpoint_evidence",
        lambda **_arguments: {
            "batch_id": "batch_test",
            "record_ids": [],
            "checkpoint_sha256": "5" * 64,
            "commit_sha256": "6" * 64,
        },
    )
    claude_core._write_once(
        pointer_path,
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA_PREVIOUS,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": old_root,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "state": "consumed",
            "final_plan_sha256": plan_sha256,
            "acceptance_sha256": "4" * 64,
            "cohort_id": cohort_id,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )

    execution_lock = open(state_dir / "cohort.lock", "a+b")
    fcntl.flock(execution_lock, fcntl.LOCK_EX)
    try:
        with pytest.raises(claude_core.ClaudeCoreError, match="active"):
            claude_core.migrate_legacy_cohort_intent(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=old_root,
                cohort_id=cohort_id,
                plan_sha256=plan_sha256,
                reason="Upgrade the unlaunched v1 intent without replaying it.",
            )
        assert not (state_dir / "receipt.json").exists()
    finally:
        fcntl.flock(execution_lock, fcntl.LOCK_UN)
        execution_lock.close()

    if intent_schema == claude_core.COHORT_INTENT_SCHEMA:
        claude_core._write_once(
            state_dir / "worker.json",
            {
                "schema_version": claude_core.COHORT_WORKER_SCHEMA,
                "cohort_id": cohort_id,
                "problem_id": "example",
                "statement_sha256": digest,
                "plan_sha256": plan_sha256,
                "root_session_id": old_root,
                "worker_pid": 99_999_999,
                "started_at_unix": time.time(),
                "frontier_before_sha256": "c" * 64,
                "worker_start_token": "d" * 64,
                "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            },
            mode=0o400,
        )
        with pytest.raises(claude_core.ClaudeCoreError, match="confirmation"):
            claude_core.migrate_legacy_cohort_intent(
                problem_id="example",
                statement_sha256=digest,
                root_session_id=old_root,
                cohort_id=cohort_id,
                plan_sha256=plan_sha256,
                reason="Fence the stopped intent before a fresh root epoch.",
            )

    monkeypatch.setattr(claude_core, "_host_source_sha256", lambda: source_b)
    migration = claude_core.migrate_legacy_cohort_intent(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        cohort_id=cohort_id,
        plan_sha256=plan_sha256,
        reason="Fence the stopped intent before a fresh root epoch.",
        confirm_stopped_worker=(
            intent_schema == claude_core.COHORT_INTENT_SCHEMA
        ),
    )
    assert migration["schema_version"] == claude_core.COHORT_LEGACY_MIGRATION_SCHEMA
    assert migration["status"] == "operationally_blocked"
    assert migration["next_action"] == "take_over_with_a_fresh_root_epoch"
    assert migration["migration_policy"] == (
        claude_core.COHORT_MIGRATION_POLICY_V4
        if intent_schema == claude_core.COHORT_INTENT_SCHEMA
        else claude_core.COHORT_MIGRATION_POLICY_PRE_V3
    )
    pointer = claude_core._read_council_pointer(
        pointer_path,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=old_root,
        require_current_source=False,
        expected_host_source_sha256=source_a,
    )
    assert pointer["state"] == "cohort_blocked"
    assert claude_core._council_failure_receipt_sha256(pointer) == hashlib.sha256(
        (state_dir / "receipt.json").read_bytes()
    ).hexdigest()
    claude_core._assert_no_unsettled_cohort_execution(
        problem_id="example", statement_sha256=digest
    )

    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=new_root,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=old_root,
    )
    assert successor["previous_root_session_id"] == old_root
    assert successor["host_source_sha256"] == source_b

    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": "e" * 64},
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda value: Path(value)
    )

    def fake_blind(**arguments: object) -> dict[str, object]:
        request = arguments["request"]
        assert isinstance(request, dict)
        return {
            "status": "completed",
            "report": {
                "schema_version": claude_core.COUNCIL_SOL_SLATE_SCHEMA,
                "council_id": request["council_id"],
                "statement_sha256": digest,
                "plan_slots": _blind_plan_slots(),
                "global_risks": ["Synthetic migration risk."],
                "comparative_note": "Three distinct migrated routes.",
            },
            "event_stream_sha256": "f" * 64,
            "retry_allowed": False,
        }

    monkeypatch.setattr(claude_core, "_invoke_sol_council", fake_blind)
    started = claude_core.start_route_council(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=new_root,
        opus_plans=_plans(),
        prior_failure_context="The predecessor cohort was owner-fenced.",
        codex_bin=Path(sys.executable),
        timeout_seconds=60,
    )
    assert started["council_round"] == 2
    assert started["prior_failure_receipt_sha256"] == migration[
        "tombstone_sha256"
    ]


def test_owner_migration_accepts_persisted_v2_intent_without_closure_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_raw = (claude_core.canonical_json(plan_set) + "\n").encode()
    plan_sha256 = hashlib.sha256(plan_raw).hexdigest()
    cohort_id = "cohort_" + plan_sha256[:32]
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    runner = claude_core.RUNNER.resolve(strict=True)
    codex = Path(sys.executable).resolve(strict=True)
    old_v2_intent = {
        "schema_version": claude_core.COHORT_INTENT_SCHEMA_PREVIOUS,
        "state": "submitted",
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": digest,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "timeout_seconds": 60,
        "runner_path": str(runner),
        "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "codex_bin": str(codex),
        "codex_bin_sha256": hashlib.sha256(codex.read_bytes()).hexdigest(),
        "host_source_sha256": claude_core._host_source_sha256(),
        "created_at_unix": time.time(),
    }
    intent_path = state_dir / "intent.json"
    claude_core._write_once(intent_path, old_v2_intent, mode=0o600)
    assert claude_core._read_matching_intent(
        intent_path,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=digest,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
    ) == old_v2_intent
    with pytest.raises(claude_core.ClaudeCoreError, match="legacy cohort intent"):
        claude_core._admit_cohort_intent(
            state_dir=state_dir,
            receipt_path=state_dir / "receipt.json",
            intent_path=intent_path,
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=digest,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
            timeout_seconds=60,
            codex_bin=codex,
        )
    claude_core._write_once(
        state_dir / "worker.json",
        {
            "schema_version": claude_core.COHORT_WORKER_SCHEMA_PREVIOUS,
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "worker_pid": 99_999_999,
            "started_at_unix": time.time(),
            "frontier_before_sha256": "a" * 64,
        },
        mode=0o400,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="confirmation"):
        claude_core.migrate_legacy_cohort_intent(
            problem_id="example",
            statement_sha256=digest,
            root_session_id=root_session_id,
            cohort_id=cohort_id,
            plan_sha256=plan_sha256,
            reason="Retire the pre-closure v2 intent before a fresh epoch.",
        )
    migrated = claude_core.migrate_legacy_cohort_intent(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        cohort_id=cohort_id,
        plan_sha256=plan_sha256,
        reason="Retire the pre-closure v2 intent before a fresh epoch.",
        confirm_stopped_worker=True,
    )
    assert migrated["status"] == "operationally_blocked"
    assert migrated["worker_schema_version"] == claude_core.COHORT_WORKER_SCHEMA_PREVIOUS
    claude_core._assert_no_unsettled_cohort_execution(
        problem_id="example", statement_sha256=digest
    )


def test_cohort_migration_policy_rejects_cross_generation_pairs() -> None:
    claude_core._assert_cohort_migration_schema_matrix(
        migration_policy=claude_core.COHORT_MIGRATION_POLICY_PRE_V3,
        intent_schema_version=claude_core.COHORT_INTENT_SCHEMA_PREVIOUS,
        worker_schema_version=claude_core.COHORT_WORKER_SCHEMA_PREVIOUS,
    )
    claude_core._assert_cohort_migration_schema_matrix(
        migration_policy=claude_core.COHORT_MIGRATION_POLICY_V3,
        intent_schema_version=claude_core.COHORT_INTENT_SCHEMA_V3,
        worker_schema_version=claude_core.COHORT_WORKER_SCHEMA_V3,
    )
    claude_core._assert_cohort_migration_schema_matrix(
        migration_policy=claude_core.COHORT_MIGRATION_POLICY_V4,
        intent_schema_version=claude_core.COHORT_INTENT_SCHEMA,
        worker_schema_version=claude_core.COHORT_WORKER_SCHEMA,
    )
    for migration_policy, intent_schema, worker_schema in (
        (
            claude_core.COHORT_MIGRATION_POLICY_PRE_V3,
            claude_core.COHORT_INTENT_SCHEMA,
            claude_core.COHORT_WORKER_SCHEMA_PREVIOUS,
        ),
        (
            claude_core.COHORT_MIGRATION_POLICY_PRE_V3,
            claude_core.COHORT_INTENT_SCHEMA_PREVIOUS,
            claude_core.COHORT_WORKER_SCHEMA,
        ),
        (
            claude_core.COHORT_MIGRATION_POLICY_V3,
            claude_core.COHORT_INTENT_SCHEMA_PREVIOUS,
            claude_core.COHORT_WORKER_SCHEMA,
        ),
        (
            claude_core.COHORT_MIGRATION_POLICY_V3,
            claude_core.COHORT_INTENT_SCHEMA,
            claude_core.COHORT_WORKER_SCHEMA_PREVIOUS,
        ),
    ):
        with pytest.raises(
            claude_core.ClaudeCoreError, match="schema matrix"
        ):
            claude_core._assert_cohort_migration_schema_matrix(
                migration_policy=migration_policy,
                intent_schema_version=intent_schema,
                worker_schema_version=worker_schema,
            )


def test_free_cohort_lock_overrides_reused_live_worker_pid_for_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _statement_digest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    state_root = tmp_path / "state"
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(claude_core, "INPUT_ROOT", tmp_path / "inputs")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=root_session_id,
    )
    plan_raw = (claude_core.canonical_json(plan_set) + "\n").encode()
    plan_sha256 = hashlib.sha256(plan_raw).hexdigest()
    cohort_id = "cohort_" + plan_sha256[:32]
    state_dir = state_root / "example" / cohort_id
    state_dir.mkdir(parents=True, mode=0o700)
    claude_core._write_once(
        state_dir / f"plan_{plan_sha256}.json", plan_set, mode=0o400
    )
    claude_core._write_once(
        state_dir / "intent.json",
        _current_cohort_intent(
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=digest,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        ),
        mode=0o600,
    )
    frontier = "4" * 64
    claude_core._write_once(
        state_dir / "worker.json",
        {
            "schema_version": claude_core.COHORT_WORKER_SCHEMA,
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "worker_pid": os.getpid(),
            "started_at_unix": time.time(),
            "frontier_before_sha256": frontier,
            "worker_start_token": "6" * 64,
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        },
        mode=0o400,
    )
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier},
    )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda _problem_id, _statement_sha256: None,
    )
    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )

    receipt = claude_core._wait_for_cohort_receipt(
        receipt_path=state_dir / "receipt.json",
        state_dir=state_dir,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=digest,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
        wait_seconds=0,
    )
    assert receipt["status"] == "failed"
    assert receipt["frontier_changed"] is False
    assert (state_dir / "receipt.json").is_file()
    assert claude_core._wait_for_cohort_receipt(
        receipt_path=state_dir / "receipt.json",
        state_dir=state_dir,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=digest,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
        wait_seconds=0,
    ) == receipt

    authorization = claude_core.authorize_failed_cohort_recovery(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=root_session_id,
        plan_sha256=plan_sha256,
        source_cohort_id=cohort_id,
        codex_bin=Path(sys.executable),
    )
    assert authorization["status"] == "authorized"
    assert authorization["frontier_recovery_mode"] == "unchanged"


def test_previous_cohort_worker_fails_closed_without_executor_identity(
    tmp_path: Path,
) -> None:
    cohort_id = "cohort_" + "1" * 32
    state_dir = tmp_path / cohort_id
    state_dir.mkdir()
    claude_core._write_once(
        state_dir / "worker.json",
        {
            "schema_version": claude_core.COHORT_WORKER_SCHEMA_PREVIOUS,
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": "2" * 64,
            "plan_sha256": "3" * 64,
            "root_session_id": "12345678-1234-4123-8123-123456789abc",
            "worker_pid": os.getpid(),
            "started_at_unix": time.time(),
            "frontier_before_sha256": "4" * 64,
        },
        mode=0o400,
    )
    assert claude_core._settle_stopped_cohort_worker(
        state_dir=state_dir,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256="2" * 64,
        plan_sha256="3" * 64,
        root_session_id="12345678-1234-4123-8123-123456789abc",
    ) is None
    assert not (state_dir / "receipt.json").exists()


def test_preexecution_drift_crash_after_log_remains_reconcilable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cohort_id = "cohort_" + "1" * 32
    statement_sha256 = "2" * 64
    plan_sha256 = "3" * 64
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    frontier = "4" * 64
    state_dir = tmp_path / cohort_id
    state_dir.mkdir()
    worker = {
        "schema_version": claude_core.COHORT_WORKER_SCHEMA,
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "worker_pid": os.getpid(),
        "started_at_unix": time.time(),
        "frontier_before_sha256": frontier,
        "worker_start_token": "5" * 64,
        "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
    }
    claude_core._write_once(
        state_dir / "intent.json",
        _current_cohort_intent(
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        ),
        mode=0o600,
    )
    claude_core._write_once(state_dir / "worker.json", worker, mode=0o400)
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier},
    )
    monkeypatch.setattr(
        claude_core, "_terminate_owned_command_wrappers", lambda *_args: None
    )
    original_write_once = claude_core._write_once

    def crash_before_receipt(
        path: Path, value: object, *, mode: int = 0o600
    ) -> str:
        if path.name == "receipt.json":
            raise RuntimeError("fault after pre-execution log")
        return original_write_once(path, value, mode=mode)

    monkeypatch.setattr(claude_core, "_write_once", crash_before_receipt)
    with pytest.raises(RuntimeError, match="fault after pre-execution log"):
        claude_core._settle_preexecution_source_drift(
            state_dir=state_dir,
            worker=worker,
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        )
    assert stat.S_IMODE((state_dir / "executor.log").stat().st_mode) == 0o600

    monkeypatch.setattr(claude_core, "_write_once", original_write_once)
    receipt = claude_core._settle_stopped_cohort_worker(
        state_dir=state_dir,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=statement_sha256,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
    )
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert (state_dir / "receipt.json").is_file()


@pytest.mark.parametrize(
    ("pinned_cap", "deployed_cap", "expected_status"),
    [(0, 1024, "output_limit"), (1024, 0, "failed")],
)
def test_stopped_worker_recovery_uses_pinned_log_cap_across_deployments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pinned_cap: int,
    deployed_cap: int,
    expected_status: str,
) -> None:
    cohort_id = "cohort_" + "2" * 32
    statement_sha256 = "3" * 64
    plan_sha256 = "4" * 64
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    frontier = "5" * 64
    state_dir = tmp_path / cohort_id
    state_dir.mkdir()
    claude_core._write_once(
        state_dir / "intent.json",
        _current_cohort_intent(
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
            max_report_log_bytes=pinned_cap,
        ),
        mode=0o600,
    )
    claude_core._write_once(
        state_dir / "worker.json",
        {
            "schema_version": claude_core.COHORT_WORKER_SCHEMA,
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": statement_sha256,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "worker_pid": 99_999_999,
            "started_at_unix": time.time(),
            "frontier_before_sha256": frontier,
            "worker_start_token": "6" * 64,
            "max_report_log_bytes": pinned_cap,
        },
        mode=0o400,
    )
    (state_dir / "executor.log").write_bytes(b"x")
    monkeypatch.setattr(claude_core, "MAX_REPORT_LOG_BYTES", deployed_cap)
    monkeypatch.setattr(
        claude_core, "_frontier", lambda _problem_id: {"frontier_sha256": frontier}
    )
    monkeypatch.setattr(
        claude_core, "_terminate_owned_command_wrappers", lambda *_args: None
    )

    receipt = claude_core._settle_stopped_cohort_worker(
        state_dir=state_dir,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=statement_sha256,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
    )
    assert receipt is not None
    assert receipt["status"] == expected_status
    assert receipt["max_report_log_bytes"] == pinned_cap
    assert receipt["log_over_cap"] is (pinned_cap == 0)


def test_preexecution_source_drift_honors_zero_pinned_log_cap(
    tmp_path: Path,
) -> None:
    cohort_id = "cohort_" + "3" * 32
    statement_sha256 = "4" * 64
    plan_sha256 = "5" * 64
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    frontier = "6" * 64
    state_dir = tmp_path / cohort_id
    state_dir.mkdir()
    claude_core._write_once(
        state_dir / "intent.json",
        _current_cohort_intent(
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
            max_report_log_bytes=0,
        ),
        mode=0o600,
    )
    worker = {
        "schema_version": claude_core.COHORT_WORKER_SCHEMA,
        "cohort_id": cohort_id,
        "problem_id": "example",
        "statement_sha256": statement_sha256,
        "plan_sha256": plan_sha256,
        "root_session_id": root_session_id,
        "worker_pid": os.getpid(),
        "started_at_unix": time.time(),
        "frontier_before_sha256": frontier,
        "worker_start_token": "7" * 64,
        "max_report_log_bytes": 0,
    }
    claude_core._write_once(state_dir / "worker.json", worker, mode=0o400)
    receipt = claude_core._settle_preexecution_source_drift(
        state_dir=state_dir,
        worker=worker,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=statement_sha256,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
    )
    assert receipt["status"] == "output_limit"
    assert receipt["log_over_cap"] is True
    assert receipt["max_report_log_bytes"] == 0


def test_ready_wait_reconciles_reused_worker_pid_when_lock_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cohort_id = "cohort_" + "6" * 32
    statement_sha256 = "7" * 64
    plan_sha256 = "8" * 64
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    frontier = "9" * 64
    state_dir = tmp_path / cohort_id
    state_dir.mkdir()
    claude_core._write_once(
        state_dir / "intent.json",
        _current_cohort_intent(
            cohort_id=cohort_id,
            problem_id="example",
            statement_sha256=statement_sha256,
            plan_sha256=plan_sha256,
            root_session_id=root_session_id,
        ),
        mode=0o600,
    )
    claude_core._write_once(
        state_dir / "worker.json",
        {
            "schema_version": claude_core.COHORT_WORKER_SCHEMA,
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": statement_sha256,
            "plan_sha256": plan_sha256,
            "root_session_id": root_session_id,
            "worker_pid": os.getpid(),
            "started_at_unix": time.time(),
            "frontier_before_sha256": frontier,
            "worker_start_token": "a" * 64,
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
        },
        mode=0o400,
    )
    monkeypatch.setattr(claude_core, "_pid_is_live", lambda _pid: True)
    monkeypatch.setattr(
        claude_core, "_process_identity_token", lambda _pid: "b" * 64
    )
    monkeypatch.setattr(
        claude_core, "_terminate_owned_command_wrappers", lambda *_args: None
    )
    monkeypatch.setattr(
        claude_core,
        "_frontier",
        lambda _problem_id: {"frontier_sha256": frontier},
    )

    assert claude_core._wait_for_cohort_worker_ready(
        receipt_path=state_dir / "receipt.json",
        state_dir=state_dir,
        cohort_id=cohort_id,
        problem_id="example",
        statement_sha256=statement_sha256,
        plan_sha256=plan_sha256,
        root_session_id=root_session_id,
        wait_seconds=0,
    ) is None
    receipt = json.loads((state_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"


@pytest.mark.parametrize("fault_stage", ["partial-write", "publish"])
def test_host_source_snapshot_fault_never_leaves_a_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_stage: str,
) -> None:
    destination = tmp_path / "host_source.py"
    expected = claude_core._host_source_sha256()
    original_write = os.write
    original_link = os.link
    if fault_stage == "partial-write":
        calls = 0

        def fail_after_partial_write(descriptor: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(descriptor, data[: max(1, len(data) // 2)])
            raise OSError("fault during snapshot write")

        monkeypatch.setattr(os, "write", fail_after_partial_write)
    else:
        monkeypatch.setattr(
            os,
            "link",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("fault during snapshot publish")
            ),
        )

    with pytest.raises(OSError, match="fault during snapshot"):
        claude_core._write_host_source_snapshot(
            destination, expected_source_sha256=expected
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".host_source.py.write-once-*"))

    monkeypatch.setattr(os, "write", original_write)
    monkeypatch.setattr(os, "link", original_link)
    written, _source = claude_core._write_host_source_snapshot(
        destination, expected_source_sha256=expected
    )
    assert written == destination
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == expected
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("source_receipt_sha256", "f" * 64),
        ("reason", "different_reason"),
        ("cancelled_at_utc", "2026-01-01T00:00:00"),
    ],
)
def test_recovery_cancellation_reader_rejects_every_binding_mismatch(
    tmp_path: Path, field: str, invalid: str
) -> None:
    path = tmp_path / "recovery_cancellation.json"
    expected_receipt = "a" * 64
    value = {
        "schema_version": "rethlas_claude_cohort_recovery_cancellation_v1",
        "status": "cancelled",
        "problem_id": "example",
        "source_cohort_id": "cohort_" + "b" * 32,
        "source_receipt_sha256": expected_receipt,
        "reason": "terminal_publication",
        "cancelled_at_utc": "2026-01-01T00:00:00+00:00",
    }
    value[field] = invalid
    claude_core._write_once(path, value, mode=0o400)
    with pytest.raises(claude_core.ClaudeCoreError, match="cancellation"):
        claude_core._read_cohort_recovery_cancellation(
            path,
            problem_id="example",
            source_cohort_id="cohort_" + "b" * 32,
            source_receipt_sha256=expected_receipt,
        )


@pytest.mark.skipif(os.name != "posix", reason="cohort lifelines require POSIX")
def test_owner_lifeline_loss_kills_detached_cohort_executor_descendants(
    tmp_path: Path,
) -> None:
    child_ready = tmp_path / "child.ready"
    child_stopped = tmp_path / "child.stopped"
    script = tmp_path / "cohort_executor.py"
    script.write_text(
        "\n".join(
            [
                "import pathlib, signal, subprocess, sys, time",
                "child_code = '''import pathlib,signal,sys,time",
                "def stop(*_args):",
                "    pathlib.Path(sys.argv[2]).write_text('stopped')",
                "    raise SystemExit(0)",
                "signal.signal(signal.SIGTERM, stop)",
                "pathlib.Path(sys.argv[1]).write_text('ready')",
                "while True: time.sleep(1)'''",
                "subprocess.Popen([sys.executable, '-c', child_code, sys.argv[1], sys.argv[2]])",
                "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)",
                "while True: time.sleep(1)",
            ]
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "executor.log"
    lifeline_read, lifeline_write = os.pipe()
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, str(script), str(child_ready), str(child_stopped)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not child_ready.exists():
                time.sleep(0.01)
            assert child_ready.exists(), "cohort executor descendant did not start"
            os.close(lifeline_write)
            lifeline_write = -1
            returncode, timed_out, output_limit = claude_core._wait_for_executor(
                process,
                log_path,
                20,
                lifeline_fd=lifeline_read,
            )
        assert returncode != 0
        assert timed_out is False
        assert output_limit is False
        assert child_stopped.read_text(encoding="utf-8") == "stopped"
        assert "owner_lifeline_status: lost" in log_path.read_text(encoding="utf-8")
    finally:
        for descriptor in (lifeline_read, lifeline_write):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_wait_for_executor_waits_for_residual_process_group_members(
    tmp_path: Path,
) -> None:
    child_finished = tmp_path / "child.finished"
    script = tmp_path / "short_lived_group_leader.py"
    script.write_text(
        "\n".join(
            [
                "import subprocess, sys",
                "child_code = '''import pathlib,sys,time",
                "time.sleep(0.25)",
                "pathlib.Path(sys.argv[1]).write_text('finished')'''",
                "subprocess.Popen([sys.executable, '-c', child_code, sys.argv[1]])",
            ]
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "executor.log"

    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(script), str(child_finished)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        returncode, timed_out, output_limit = claude_core._wait_for_executor(
            process,
            log_path,
            5,
        )

    assert returncode == 0
    assert timed_out is False
    assert output_limit is False
    assert child_finished.read_text(encoding="utf-8") == "finished"


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_kill_group_stops_residual_members_after_leader_exits(
    tmp_path: Path,
) -> None:
    child_ready = tmp_path / "child.ready"
    child_stopped = tmp_path / "child.stopped"
    script = tmp_path / "departing_group_leader.py"
    script.write_text(
        "\n".join(
            [
                "import pathlib, subprocess, sys, time",
                "child_code = '''import os,pathlib,signal,sys,time",
                "def stop(*_args):",
                "    pathlib.Path(sys.argv[2]).write_text('stopped')",
                "    raise SystemExit(0)",
                "signal.signal(signal.SIGTERM, stop)",
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))",
                "while True: time.sleep(1)'''",
                "subprocess.Popen([sys.executable, '-c', child_code, sys.argv[1], sys.argv[2]])",
                "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)",
            ]
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(script), str(child_ready), str(child_stopped)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process.wait(timeout=3)
    assert child_ready.exists(), "residual executor member did not start"

    try:
        claude_core._kill_group(process)
        assert child_stopped.read_text(encoding="utf-8") == "stopped"
    finally:
        if not child_stopped.exists():
            try:
                os.kill(
                    int(child_ready.read_text(encoding="utf-8")), signal.SIGKILL
                )
            except (FileNotFoundError, ProcessLookupError):
                pass


def test_blueprint_write_is_host_scoped_and_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    source = data_root / "nested" / "example.md"
    source.parent.mkdir()
    source.write_text("Statement.\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")

    first = claude_core.write_blueprint(
        problem_id="nested/example",
        statement_sha256=digest,
        blueprint_markdown=_structured_blueprint("First proof."),
    )
    second = claude_core.write_blueprint(
        problem_id="nested/example",
        statement_sha256=digest,
        blueprint_markdown=_structured_blueprint("Second proof."),
    )

    target = generation_root / "results" / "nested" / "example" / "blueprint.md"
    assert target.read_text(encoding="utf-8") == _structured_blueprint("Second proof.")
    assert first["status"] == second["status"] == "written"
    assert second["previous_blueprint_sha256"] == first["blueprint_sha256"]
    archives = list(
        (tmp_path / "state" / "nested" / "example" / "blueprints").glob("*.json")
    )
    assert len(archives) == 2


def _structured_blueprint(proof_text: str = "Old proof.") -> str:
    return (
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nStatement.\n\n"
        f"## proof\n{proof_text}\n"
    )


def _publication_context(
    *, statement: str, proof: str
) -> tuple[str, str, str, dict[str, object]]:
    parser = claude_core._proof_context()
    manifest = parser.parse_blueprint(proof, target_statement=statement)
    assert len(manifest.item_ids) == 1
    item_id = manifest.item_ids[0]
    context = parser.build_item_context(
        manifest,
        item_id,
        max_chars=200_000,
        expanded_proof_ids=[],
        round_index=0,
    )
    attestation: dict[str, object] = {
        "item_id": item_id,
        "verdict": "correct",
        "disposition": "verified",
        "expanded_proof_ids": [],
        "final_round": 0,
        "context_digest": context["digest"],
        "max_chars": 200_000,
    }
    return (
        item_id,
        parser.aggregate_context_digest(manifest),
        parser.aggregate_adaptive_context_digest(manifest, [attestation]),
        attestation,
    )


def test_blueprint_contract_requires_explicit_dependencies_and_exact_target() -> None:
    missing_dependency = _structured_blueprint().replace(
        "<!-- rethlas-depends-on: -->\n", ""
    )
    with pytest.raises(
        claude_core.BlueprintContractError,
        match="explicit rethlas-depends-on",
    ):
        claude_core._validate_blueprint_markdown(
            statement_markdown="Statement.\n",
            blueprint_markdown=missing_dependency,
        )

    wrong_target = _structured_blueprint().replace(
        "## statement\nStatement.\n", "## statement\nDifferent statement.\n"
    )
    with pytest.raises(claude_core.BlueprintContractError) as rejected:
        claude_core._validate_blueprint_markdown(
            statement_markdown="Statement.\n",
            blueprint_markdown=wrong_target,
        )
    message = str(rejected.value)
    assert "expected_sha256=" in message
    assert "actual_sha256=" in message
    assert "first_diff_line=1" in message
    assert "expected='Statement.'" in message
    assert "actual='Different statement.'" in message


def test_blueprint_contract_accepts_canonical_target_with_embedded_headings() -> None:
    target = """Opening target text.

## Frozen deterministic setting

This section is part of the target.

## proof

This is still target text."""
    statement_document = f"""# Display title

{target}

## Retrieval restriction

Offline only.
"""
    blueprint = (
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\n"
        f"{target}\n\n"
        "## proof\n"
        "The complete target follows.\n"
    )

    manifest = claude_core._validate_blueprint_markdown(
        statement_markdown=statement_document,
        blueprint_markdown=blueprint,
    )

    assert manifest.source_kind == "structured"
    assert manifest.items[-1].statement == target


def test_blueprint_write_materializes_bound_canonical_target_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    source = generation_root / "data" / "nested" / "example.md"
    source.parent.mkdir(parents=True)
    target = """Opening target text.

## Frozen deterministic setting

This target contains structural-looking headings.

## proof

This line remains part of the target."""
    source.write_text(
        f"# Display title\n\n{target}\n\n## Retrieval restriction\n\nOffline only.\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    submitted = (
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\n"
        f"{claude_core.CANONICAL_TARGET_PLACEHOLDER}\n\n"
        "## proof\n"
        "The complete proof.\n"
    )

    receipt = claude_core.write_blueprint(
        problem_id="nested/example",
        statement_sha256=digest,
        blueprint_markdown=submitted,
    )

    stored = (
        generation_root / "results" / "nested" / "example" / "blueprint.md"
    ).read_text(encoding="utf-8")
    assert claude_core.CANONICAL_TARGET_PLACEHOLDER not in stored
    assert f"## statement\n{target}\n\n## proof" in stored
    assert receipt["blueprint_sha256"] == hashlib.sha256(
        stored.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "submitted, message",
    [
        (
            "# theorem t\n## statement\n"
            "<!-- rethlas-canonical-target -->\n"
            "<!-- rethlas-canonical-target -->\n## proof\nProof.\n",
            "must occur exactly once",
        ),
        (
            "# theorem t\n## statement\nprefix "
            "<!-- rethlas-canonical-target -->\n## proof\nProof.\n",
            "must occupy its own exact line",
        ),
        (
            "# theorem t\n## statement\nWrong body.\n## proof\n"
            "<!-- rethlas-canonical-target -->\n",
            "must be the sole nonblank body",
        ),
    ],
)
def test_canonical_target_placeholder_rejects_ambiguous_placement(
    submitted: str, message: str
) -> None:
    with pytest.raises(claude_core.BlueprintContractError, match=message):
        claude_core._materialize_canonical_target_placeholder(
            statement_markdown="Canonical target.\n",
            blueprint_markdown=submitted,
        )


def test_blueprint_contract_rejects_oversized_independent_verifier_unit() -> None:
    oversized = _structured_blueprint(
        "x" * (claude_core.MAX_BLUEPRINT_PROOF_ITEM_CHARS + 1)
    )

    with pytest.raises(claude_core.BlueprintContractError) as rejected:
        claude_core._validate_blueprint_markdown(
            statement_markdown="Statement.\n",
            blueprint_markdown=oversized,
        )

    assert "proof item is too large for one independent verifier unit" in str(
        rejected.value
    )
    assert rejected.value.repair_hint is not None
    assert "dependency-linked H1 proof items" in rejected.value.repair_hint


@pytest.mark.parametrize("control", ["\r", "\x00", "\x1f", "\x7f"])
def test_blueprint_contract_rejects_disallowed_ascii_controls(
    control: str,
) -> None:
    blueprint = _structured_blueprint(
        rf"The escape probability is $\beta_{{{control}rm esc}}$."
    )

    with pytest.raises(
        claude_core.BlueprintContractError,
        match="disallowed ASCII control character",
    ) as rejected:
        claude_core._validate_blueprint_markdown(
            statement_markdown="Statement.\n",
            blueprint_markdown=blueprint,
        )

    assert f"U+{ord(control):04X}" in str(rejected.value)
    assert rejected.value.repair_hint is not None
    assert "Only tab and line-feed" in rejected.value.repair_hint


def test_control_character_preflight_leaves_no_publication_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    blueprint_path.write_text(
        _structured_blueprint("Hidden carriage\rreturn."), encoding="utf-8"
    )

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**_arguments: object) -> dict[str, object]:
            pytest.fail("verifier dispatch must not start")

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    with pytest.raises(
        claude_core.BlueprintContractError,
        match="U\\+000D",
    ):
        claude_core.verify_blueprint(
            "example",
            statement_sha256,
            root_session_id=root_session_id,
        )

    finalization_root = (
        tmp_path / "state" / "example" / "publication_finalizations"
    )
    assert not finalization_root.exists()


def test_invalid_draft_verify_leaves_no_intent_and_can_be_edited_then_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    invalid = _structured_blueprint().replace(
        "## statement\nStatement.\n",
        "## statement\nDifferent statement.\n",
    )
    blueprint_path.write_text(invalid, encoding="utf-8")

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            return {"published": False, "verdict": "wrong"}

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    with pytest.raises(
        claude_core.BlueprintContractError,
        match="final statement differs",
    ):
        claude_core.verify_blueprint(
            "example",
            statement_sha256,
            root_session_id=root_session_id,
        )
    finalization_root = (
        tmp_path / "state" / "example" / "publication_finalizations"
    )
    assert not finalization_root.exists()

    invalid_sha256 = hashlib.sha256(invalid.encode("utf-8")).hexdigest()
    edited = claude_core.edit_blueprint(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        base_blueprint_sha256=invalid_sha256,
        old_string="Different statement.",
        new_string="Statement.",
    )
    assert edited["status"] == "edited"
    result = claude_core.verify_blueprint(
        "example",
        statement_sha256,
        root_session_id=root_session_id,
    )
    assert result == {"published": False, "verdict": "wrong"}
    settlements = list(finalization_root.glob("*/settlement.json"))
    assert len(settlements) == 1
    assert json.loads(settlements[0].read_text(encoding="utf-8"))[
        "status"
    ] == "not_published"


def test_verification_retry_requires_each_substantive_wrong_item_to_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    blueprint = (
        "# lemma lem:auxiliary\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nAuxiliary fact.\n\n"
        "## proof\nOld auxiliary proof.\n\n"
        "# lemma lem:blocked\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nIndependent blocked fact.\n\n"
        "## proof\nUnchanged blocked proof.\n\n"
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nStatement.\n\n"
        "## proof\nOld main proof.\n"
    )
    blueprint_path.write_text(blueprint, encoding="utf-8")
    verifier_calls = 0

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            nonlocal verifier_calls
            verifier_calls += 1
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            proof = Path(str(arguments["draft_path"])).read_text(
                encoding="utf-8"
            )
            statement = str(arguments["statement"])
            parser = claude_core._proof_context()
            manifest = parser.parse_blueprint(
                proof, target_statement=statement
            )
            attestations: list[dict[str, object]] = []
            for index, item in enumerate(manifest.items):
                context = parser.build_item_context(
                    manifest,
                    item.item_id,
                    max_chars=200_000,
                    expanded_proof_ids=[],
                    round_index=0,
                )
                attestations.append(
                    {
                        "item_id": item.item_id,
                        "verdict": "wrong" if index in {1, 2} else "correct",
                        "disposition": "blocked" if index == 1 else "verified",
                        "expanded_proof_ids": [],
                        "final_round": 0,
                        "context_digest": context["digest"],
                        "max_chars": 200_000,
                    }
                )
            return {
                "published": False,
                "verdict": "wrong",
                "proof_digest": hashlib.sha256(proof.encode()).hexdigest(),
                "checked_item_ids": list(manifest.item_ids),
                "item_context_attestations": attestations,
            }

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    first = claude_core.verify_blueprint(
        "example",
        statement_sha256,
        root_session_id=root_session_id,
    )
    assert first["published"] is False
    assert verifier_calls == 1

    first_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()
    unrelated_edit = claude_core.edit_blueprint(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        base_blueprint_sha256=first_sha256,
        old_string="Old auxiliary proof.",
        new_string="New auxiliary proof.",
    )
    with pytest.raises(
        claude_core.BlueprintContractError,
        match="previously rejected proof items remain content-identical",
    ) as rejected:
        claude_core.verify_blueprint(
            "example",
            statement_sha256,
            root_session_id=root_session_id,
        )
    assert "thm:main" in str(rejected.value)
    assert rejected.value.repair_hint is not None
    assert "Do not rerun the verifier yet" in rejected.value.repair_hint
    assert verifier_calls == 1

    repaired = claude_core.edit_blueprint(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        base_blueprint_sha256=str(unrelated_edit["blueprint_sha256"]),
        old_string="Old main proof.",
        new_string="Repaired main proof.",
    )
    assert repaired["status"] == "edited"
    second = claude_core.verify_blueprint(
        "example",
        statement_sha256,
        root_session_id=root_session_id,
    )
    assert second["published"] is False
    assert verifier_calls == 2
    # The unchanged blocked item did not become a false repair obligation.
    assert len(
        list(
            (
                tmp_path
                / "state"
                / "example"
                / "publication_finalizations"
            ).glob("*/result.json")
        )
    ) == 2


def _prepare_edit_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, str]:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    blueprint = _structured_blueprint()
    blueprint_path = result_dir / "blueprint.md"
    blueprint_path.write_text(blueprint, encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", tmp_path / "receipts")
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        canonical_model="claude-fable-5",
    )
    return blueprint_path, statement_sha256, root_session_id


def test_blueprint_edit_is_cas_bound_archived_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    base_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()

    first = claude_core.edit_blueprint(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        base_blueprint_sha256=base_sha256,
        old_string="Old proof.",
        new_string="New proof.",
    )
    replay = claude_core.edit_blueprint(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        base_blueprint_sha256=base_sha256,
        old_string="Old proof.",
        new_string="New proof.",
    )

    assert replay == first
    assert first["status"] == "edited"
    assert "blueprint_markdown" not in first
    assert len(first["proof_context_sha256"]) == 64
    assert first["occurrences_replaced"] == 1
    assert first["base_blueprint_sha256"] == base_sha256
    assert first["blueprint_sha256"] == hashlib.sha256(
        blueprint_path.read_bytes()
    ).hexdigest()
    assert blueprint_path.read_text(encoding="utf-8") == _structured_blueprint(
        "New proof."
    )
    archives = list((tmp_path / "state" / "example" / "blueprints").glob("*.json"))
    assert len(archives) == 2


def test_blueprint_edit_rejects_stale_ambiguous_and_structurally_invalid_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    original = blueprint_path.read_bytes()
    base_sha256 = hashlib.sha256(original).hexdigest()

    with pytest.raises(claude_core.ClaudeCoreError, match="stale"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256="0" * 64,
            old_string="Old proof.",
            new_string="New proof.",
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="does not occur"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256=base_sha256,
            old_string="Missing text.",
            new_string="New proof.",
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="structurally invalid"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256=base_sha256,
            old_string="## proof",
            new_string="## argument",
        )

    blueprint_path.write_text(_structured_blueprint("repeat repeat"), encoding="utf-8")
    repeated_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()
    with pytest.raises(claude_core.ClaudeCoreError, match="exactly once"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256=repeated_sha256,
            old_string="repeat",
            new_string="fixed",
        )
    assert blueprint_path.read_text(encoding="utf-8") == _structured_blueprint(
        "repeat repeat"
    )


def test_blueprint_edit_allows_explicit_bounded_replace_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    blueprint_path.write_text(_structured_blueprint("repeat repeat"), encoding="utf-8")
    base_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()

    receipt = claude_core.edit_blueprint(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        base_blueprint_sha256=base_sha256,
        old_string="repeat",
        new_string="fixed",
        replace_all=True,
    )

    assert receipt["occurrences_replaced"] == 2
    assert "fixed fixed" in blueprint_path.read_text(encoding="utf-8")


def test_blueprint_edit_rejects_stale_root_and_unsafe_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, old_session = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    base_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()
    new_session = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=new_session,
        canonical_model="claude-opus-5",
        takeover_from=old_session,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="active authority"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=old_session,
            base_blueprint_sha256=base_sha256,
            old_string="Old proof.",
            new_string="New proof.",
        )

    outside = tmp_path / "outside.md"
    outside.write_text(_structured_blueprint(), encoding="utf-8")
    blueprint_path.unlink()
    blueprint_path.symlink_to(outside)
    outside_sha256 = hashlib.sha256(outside.read_bytes()).hexdigest()
    with pytest.raises(claude_core.ClaudeCoreError, match="unavailable or unsafe"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=new_session,
            base_blueprint_sha256=outside_sha256,
            old_string="Old proof.",
            new_string="New proof.",
        )
    assert outside.read_text(encoding="utf-8") == _structured_blueprint()


def test_concurrent_blueprint_edits_have_one_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    base_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()

    def apply(replacement: str) -> object:
        try:
            return claude_core.edit_blueprint(
                problem_id="example",
                statement_sha256=statement_sha256,
                root_session_id=root_session_id,
                base_blueprint_sha256=base_sha256,
                old_string="Old proof.",
                new_string=replacement,
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(apply, ["Proof A.", "Proof B."]))

    successes = [item for item in outcomes if isinstance(item, dict)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], claude_core.ClaudeCoreError)
    assert "stale" in str(failures[0])
    assert blueprint_path.read_text(encoding="utf-8") in {
        _structured_blueprint("Proof A."),
        _structured_blueprint("Proof B."),
    }


def test_blueprint_edit_reconciles_crash_after_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    base_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()
    real_write_once = claude_core._write_once
    failed = False

    def fail_first_receipt(path: Path, value: object, *, mode: int = 0o400) -> str:
        nonlocal failed
        if path.name.endswith(".receipt.json") and not failed:
            failed = True
            raise OSError("simulated receipt crash")
        return real_write_once(path, value, mode=mode)  # type: ignore[arg-type]

    monkeypatch.setattr(claude_core, "_write_once", fail_first_receipt)
    with pytest.raises(OSError, match="simulated receipt crash"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256=base_sha256,
            old_string="Old proof.",
            new_string="Recovered proof.",
        )
    assert blueprint_path.read_text(encoding="utf-8") == _structured_blueprint(
        "Recovered proof."
    )

    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda problem_id, statement_sha256: {"status": "published"},
    )
    receipt = claude_core.edit_blueprint(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        base_blueprint_sha256=base_sha256,
        old_string="Old proof.",
        new_string="Recovered proof.",
    )
    assert receipt["status"] == "edited"


def test_blueprint_edit_refuses_noop_and_published_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    base_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()
    with pytest.raises(claude_core.ClaudeCoreError, match="must change"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256=base_sha256,
            old_string="Old proof.",
            new_string="Old proof.",
        )

    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda problem_id, statement_sha256: {"status": "published"},
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="immutable"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256=base_sha256,
            old_string="Old proof.",
            new_string="New proof.",
        )
    assert blueprint_path.read_text(encoding="utf-8") == _structured_blueprint()


def test_blueprint_edit_rechecks_statement_immediately_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blueprint_path, statement_sha256, root_session_id = _prepare_edit_fixture(
        tmp_path, monkeypatch
    )
    base_sha256 = hashlib.sha256(blueprint_path.read_bytes()).hexdigest()
    statement_path = claude_core.GENERATION_ROOT / "data" / "example.md"
    real_validate = claude_core._validate_blueprint_markdown

    def validate_then_mutate(**kwargs: str) -> None:
        real_validate(**kwargs)
        statement_path.write_text("Changed statement.\n", encoding="utf-8")

    monkeypatch.setattr(
        claude_core,
        "_validate_blueprint_markdown",
        validate_then_mutate,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="before blueprint commit"):
        claude_core.edit_blueprint(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id=root_session_id,
            base_blueprint_sha256=base_sha256,
            old_string="Old proof.",
            new_string="New proof.",
        )
    assert blueprint_path.read_text(encoding="utf-8") == _structured_blueprint()


def test_state_and_parser_identity_checks_reject_symlink_hardlink_and_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_root = tmp_path / "generation"
    statement = generation_root / "data" / "example.md"
    statement.parent.mkdir(parents=True)
    statement.write_text("Statement.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.read_bytes()).hexdigest()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    outside = tmp_path / "outside-state"
    outside.mkdir(mode=0o700)
    (state_root / "example").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    with pytest.raises(claude_core.ClaudeCoreError, match="component is unsafe"):
        _prepare_root(
            problem_id="example",
            statement_sha256=statement_sha256,
            root_session_id="12345678-1234-4123-8123-123456789abc",
            canonical_model="claude-fable-5",
        )
    assert list(outside.iterdir()) == []

    parser_copy = tmp_path / "proof_context.py"
    parser_copy.write_bytes(claude_core.PROOF_CONTEXT_SOURCE.read_bytes())
    parser_link = tmp_path / "proof_context-link.py"
    os.link(parser_copy, parser_link)
    monkeypatch.setattr(claude_core, "PROOF_CONTEXT_SOURCE", parser_copy)
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_MODULE", None)
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_SHA256", None)
    with pytest.raises(claude_core.ClaudeCoreError, match="source is unsafe"):
        claude_core._proof_context()
    parser_link.unlink()

    first_module = claude_core._proof_context()
    assert first_module is not None
    parser_copy.write_bytes(parser_copy.read_bytes() + b"\n")
    with pytest.raises(claude_core.ClaudeCoreError, match="changed after loading"):
        claude_core._proof_context()


def test_existing_publication_short_circuits_duplicate_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    receipt_root = tmp_path / "receipts"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    receipt_root.mkdir()
    statement_path.write_text("Statement.\n", encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    proof = _structured_blueprint("Complete proof.")
    draft_path = result_dir / "blueprint.md"
    verified_path = result_dir / "blueprint_verified.md"
    draft_path.write_text(proof, encoding="utf-8")
    verified_path.write_text(proof, encoding="utf-8")
    proof_sha256 = hashlib.sha256(proof.encode()).hexdigest()
    item_id, context_digest, adaptive_digest, attestation = _publication_context(
        statement="Statement.\n", proof=proof
    )
    receipt = {
        "schema_version": "rethlas-publication-v2",
        "problem_id": "example",
        "statement_digest": statement_sha256,
        "proof_digest": proof_sha256,
        "context_digest": context_digest,
        "adaptive_context_digest": adaptive_digest,
        "item_context_attestations": [attestation],
        "checked_item_ids": [item_id],
        "verified_path": str(verified_path),
        "published_bytes": len(proof.encode()),
    }
    (receipt_root / "example.json").write_text(
        claude_core.canonical_json(receipt) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core,
        "_legacy",
        lambda: (_ for _ in ()).throw(AssertionError("verifier repeated")),
    )

    existing = claude_core.verify_blueprint("example", statement_sha256)
    assert existing["status"] == "published"
    assert existing["verdict"] == "correct"
    assert existing["verification_status"] == "final"
    assert claude_core.verify_blueprint("example", statement_sha256) == existing
    assert existing["proof_sha256"] == proof_sha256

    with pytest.raises(claude_core.ClaudeCoreError, match="Claude-root lineages"):
        claude_core.retract_publication(
            problem_id="example",
            statement_sha256=statement_sha256,
            receipt_sha256=hashlib.sha256(
                (receipt_root / "example.json").read_bytes()
            ).hexdigest(),
            reason="not authorized for a Legacy-only publication",
        )


def test_publication_v4_replays_persisted_limits_with_frozen_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    receipt_root = tmp_path / "receipts"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    receipt_root.mkdir()
    statement = "Statement.\n"
    proof = _structured_blueprint("Complete proof.")
    statement_path.write_text(statement, encoding="utf-8")
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    verified_path = result_dir / "blueprint_verified.md"
    verified_path.write_text(proof, encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement.encode()).hexdigest()
    proof_sha256 = hashlib.sha256(proof.encode()).hexdigest()
    parser = claude_core._proof_context()
    manifest = parser.parse_blueprint(proof, target_statement=statement)
    item_id = manifest.item_ids[0]
    context_max_chars = 2_100_000
    context = parser.build_item_context(
        manifest,
        item_id,
        max_chars=context_max_chars,
        expanded_proof_ids=[],
        round_index=0,
    )
    attestation = {
        "item_id": item_id,
        "verdict": "correct",
        "disposition": "verified",
        "expanded_proof_ids": [],
        "final_round": 0,
        "context_digest": context["digest"],
        "max_chars": context_max_chars,
    }
    passes = [
        {
            "pass_index": index,
            "verification_attempt_id": "veratt_" + str(index) * 32,
            "verifier_run_id": f"run-{index}",
            "verifier_model": "gpt-6-astra",
            "verifier_reasoning_effort": "max",
            "verifier_service_version": "0.3.0",
            "verification_role": (
                "primary" if index == 1 else "adversarial_full_claim_audit"
            ),
            "response_sha256": str(index) * 64,
            "verdict": "correct",
        }
        for index in (1, 2)
    ]
    _parser_raw, parser_sha256 = claude_core._read_proof_context_source()
    receipt = {
        "schema_version": "rethlas-publication-v4",
        "state": "active",
        "problem_id": "example",
        "statement_source_digest": statement_sha256,
        "canonical_target_digest": claude_core._canonical_target_sha256(
            statement.encode()
        ),
        "proof_digest": proof_sha256,
        "context_digest": parser.aggregate_context_digest(manifest),
        "adaptive_context_digest": parser.aggregate_adaptive_context_digest(
            manifest, [attestation]
        ),
        "item_context_attestations": [attestation],
        "checked_item_ids": [item_id],
        "verified_path": str(verified_path),
        "published_bytes": len(proof.encode()),
        "published_at_utc": "2026-08-27T12:00:00+00:00",
        "verification_quorum": 2,
        "verification_passes": passes,
        "supersedes": [],
        "proof_context": {
            "schema_version": claude_core.PUBLICATION_PROOF_CONTEXT_SCHEMA,
            "source_sha256": parser_sha256,
            "proof_item_schema_version": 1,
            "proof_context_schema_version": 2,
            "aggregate_context_schema_version": 1,
            "adaptive_aggregate_context_schema_version": 2,
        },
        "verification_limits": {
            "context_max_chars": 3_000_000,
            "max_expansion_rounds": 2,
            "max_expanded_proofs": 8,
            "max_expanded_proof_chars": 3_000_000,
            "max_proof_items": 20_000,
            "max_receipt_bytes": 16_000_000,
        },
    }
    (receipt_root / "example.json").write_text(
        claude_core.canonical_json(receipt) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "MAX_PUBLICATION_RECEIPT_BYTES", 128)
    monkeypatch.setattr(claude_core, "MAX_PUBLICATION_PROOF_ITEMS", 1)
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_MODULE", None)
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_SHA256", None)

    existing = claude_core._existing_publication("example", statement_sha256)
    assert existing is not None
    assert existing["publication_schema"] == "rethlas-publication-v4"
    assert existing["checked_item_ids"] == [item_id]

    verified_path.write_text(proof + "tampered\n", encoding="utf-8")
    with pytest.raises(
        claude_core.ClaudeCoreError, match="publication receipt binding mismatch"
    ):
        claude_core.verify_blueprint("example", statement_sha256)


def test_verification_unknown_dispatch_is_not_repeated_and_reconciles_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    proof = _structured_blueprint("Complete proof.")
    draft_path = result_dir / "blueprint.md"
    verified_path = result_dir / "blueprint_verified.md"
    draft_path.write_text(proof, encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    proof_sha256 = hashlib.sha256(proof.encode()).hexdigest()
    publication_receipt_sha256 = "9" * 64
    published = False
    verifier_calls = 0
    exact_resume_calls: list[dict[str, object]] = []

    def existing_publication(
        problem_id: str,
        requested_statement_sha256: str,
        **_arguments: object,
    ) -> dict[str, object] | None:
        assert problem_id == "example"
        assert requested_statement_sha256 == statement_sha256
        if not published:
            return None
        return {
            "status": "published",
            "problem_id": problem_id,
            "statement_sha256": requested_statement_sha256,
            "proof_sha256": proof_sha256,
            "publication_receipt_sha256": publication_receipt_sha256,
            "published": True,
        }

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            nonlocal verifier_calls
            if arguments.get("prepared_only") is True:
                raise ValueError(
                    "prepared publication recovery is unavailable; "
                    "refusing verifier dispatch"
                )
            if arguments.get("resume_dispatched") is True:
                # An outer dispatch may re-enter only through the client's
                # exact-attempt recovery contract.  It must not expose a new
                # outer dispatch callback, and a still-unknown verifier
                # attempt remains blocked without another model dispatch.
                exact_resume_calls.append(dict(arguments))
                assert "on_verifier_dispatch" not in arguments
                raise claude_core.ClaudeCoreError(
                    "verifier execution is unknown"
                )
            verifier_calls += 1
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            received_draft = Path(str(arguments["draft_path"]))
            received_verified = Path(str(arguments["verified_path"]))
            received_verified.write_bytes(received_draft.read_bytes())
            raise RuntimeError("synthetic crash after verifier dispatch")

    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "_existing_publication", existing_publication)
    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        claude_core.verify_blueprint("example", statement_sha256)

    finalization_dir = (
        tmp_path
        / "state"
        / "example"
        / "publication_finalizations"
        / proof_sha256
    )
    intent_path = finalization_dir / "intent.json"
    settlement_path = finalization_dir / "settlement.json"
    original_intent = intent_path.read_bytes()
    assert verified_path.read_text(encoding="utf-8") == proof
    assert not settlement_path.exists()

    with pytest.raises(
        claude_core.ClaudeCoreError, match="execution is unknown"
    ):
        claude_core.verify_blueprint("example", statement_sha256)
    assert verifier_calls == 1
    assert len(exact_resume_calls) == 1
    assert exact_resume_calls[0]["publication_authority_intent_sha256"] == (
        hashlib.sha256(original_intent).hexdigest()
    )
    assert not settlement_path.exists()

    # An exact externally visible publication is sufficient to reconcile an
    # unknown dispatch without invoking the verifier again.
    published = True
    resumed = claude_core.verify_blueprint("example", statement_sha256)
    assert resumed["published"] is True
    assert intent_path.read_bytes() == original_intent
    settlement = json.loads(settlement_path.read_text(encoding="utf-8"))
    assert settlement["status"] == "published"
    assert (
        settlement["publication_receipt_sha256"]
        == publication_receipt_sha256
    )

    terminal_replay = claude_core.verify_blueprint("example", statement_sha256)
    assert terminal_replay["status"] == "published"
    assert terminal_replay["verdict"] == "correct"
    assert verifier_calls == 1
    assert len(exact_resume_calls) == 1


def test_predispatch_failure_leaves_retryable_finalization_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=_statement_digest(),
        blueprint_sha256="9" * 64,
    )

    def predispatch_failure(_commit_dispatch: object) -> dict[str, object]:
        raise RuntimeError("synthetic profile failure")

    with pytest.raises(RuntimeError, match="profile failure"):
        claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=predispatch_failure,
        )
    assert not (intent_path.parent / "dispatch.json").exists()
    assert not (intent_path.parent / "result.json").exists()

    calls = 0

    def retry(commit_dispatch: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert callable(commit_dispatch)
        commit_dispatch()
        return {"published": False, "verdict": "wrong"}

    result = claude_core._execute_publication_finalization_verifier(
        intent=intent,
        intent_path=intent_path,
        verifier=retry,
    )
    assert result["published"] is False
    assert calls == 1
    assert (intent_path.parent / "dispatch.json").is_file()
    assert (intent_path.parent / "result.json").is_file()


def test_operational_nonpublication_can_retry_exact_blueprint_without_host_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    live_source = ["a" * 64]
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: live_source[0]
    )
    statement_sha256 = _statement_digest()
    blueprint_sha256 = "8" * 64
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )

    def invalid_terminal(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        return {
            "published": False,
            "verdict": "wrong",
            "verification_status": "final",
            "publication_blocked_reason": "invalid_verifier_response",
            "repair_hints": "Retry only after repairing the host transport.",
        }

    with claude_core._publication_finalization_execution_lock(intent_path):
        result = claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=invalid_terminal,
        )
        assert result["publication_blocked_reason"] == "invalid_verifier_response"
        claude_core._settle_publication_finalization(
            intent=intent,
            intent_path=intent_path,
            status="not_published",
            publication_receipt_sha256=None,
        )

    retry_intent, retry_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )
    assert retry_path != intent_path
    assert retry_intent["generation_parent_intent_sha256"] == hashlib.sha256(
        intent_path.read_bytes()
    ).hexdigest()
    assert retry_intent["generation_parent_result_sha256"] == hashlib.sha256(
        (intent_path.parent / "result.json").read_bytes()
    ).hexdigest()
    assert retry_intent["operational_retry_ordinal"] == 1
    assert retry_intent["host_source_sha256"] == live_source[0]


def test_publication_retry_lineage_uses_unique_leaf_not_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: "a" * 64
    )
    now = [200.0]
    monkeypatch.setattr(claude_core.time, "time", lambda: now[0])
    statement_sha256 = _statement_digest()
    blueprint_sha256 = "5" * 64

    def operational(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        return {
            "published": False,
            "publication_blocked_reason": "invalid_verifier_response",
        }

    root, root_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )
    with claude_core._publication_finalization_execution_lock(root_path):
        claude_core._execute_publication_finalization_verifier(
            intent=root, intent_path=root_path, verifier=operational
        )
        claude_core._settle_publication_finalization(
            intent=root,
            intent_path=root_path,
            status="not_published",
            publication_receipt_sha256=None,
        )
    now[0] = 100.0
    child, child_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )
    with claude_core._publication_finalization_execution_lock(child_path):
        claude_core._execute_publication_finalization_verifier(
            intent=child, intent_path=child_path, verifier=operational
        )
        claude_core._settle_publication_finalization(
            intent=child,
            intent_path=child_path,
            status="not_published",
            publication_receipt_sha256=None,
        )
    now[0] = 50.0

    grandchild, _grandchild_path = (
        claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
        )
    )
    assert grandchild["generation_parent_intent_sha256"] == (
        claude_core.sha256_file(child_path)
    )
    assert grandchild["operational_retry_ordinal"] == 2


def test_publication_retry_rejects_forged_successor_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: "a" * 64
    )
    statement_sha256 = _statement_digest()
    blueprint_sha256 = "4" * 64
    root, root_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )

    def operational(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        return {
            "published": False,
            "publication_blocked_reason": "invalid_verifier_response",
        }

    with claude_core._publication_finalization_execution_lock(root_path):
        claude_core._execute_publication_finalization_verifier(
            intent=root, intent_path=root_path, verifier=operational
        )
        claude_core._settle_publication_finalization(
            intent=root,
            intent_path=root_path,
            status="not_published",
            publication_receipt_sha256=None,
        )
    root_sha256 = claude_core.sha256_file(root_path)
    forged_dir = claude_core._publication_finalization_dir(
        "example",
        blueprint_sha256,
        generation_parent_intent_sha256=root_sha256,
        host_source_sha256="b" * 64,
        create=True,
    )
    claude_core._write_once(
        forged_dir / "intent.json",
        {
            "schema_version": claude_core.PUBLICATION_FINALIZATION_INTENT_SCHEMA,
            "status": "submitted",
            "problem_id": "example",
            "statement_sha256": statement_sha256,
            "blueprint_sha256": blueprint_sha256,
            "host_source_sha256": "b" * 64,
            "generation_parent_intent_sha256": root_sha256,
            "generation_parent_result_sha256": "f" * 64,
            "operational_retry_ordinal": 2,
            "created_at_unix": time.time(),
        },
        mode=0o400,
    )

    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="successor binding mismatch",
    ):
        claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
        )


def test_multiple_unresolved_publication_generations_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: "a" * 64
    )
    statement_sha256 = _statement_digest()
    blueprint_sha256 = "3" * 64
    root, root_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )

    def operational(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        return {
            "published": False,
            "publication_blocked_reason": "invalid_verifier_response",
        }

    with claude_core._publication_finalization_execution_lock(root_path):
        claude_core._execute_publication_finalization_verifier(
            intent=root, intent_path=root_path, verifier=operational
        )
        claude_core._settle_publication_finalization(
            intent=root,
            intent_path=root_path,
            status="not_published",
            publication_receipt_sha256=None,
        )
    root_sha256 = claude_core.sha256_file(root_path)
    terminal_sha256 = claude_core.sha256_file(
        root_path.parent / "result.json"
    )
    for host_source_sha256 in ("b" * 64, "c" * 64):
        successor_dir = claude_core._publication_finalization_dir(
            "example",
            blueprint_sha256,
            generation_parent_intent_sha256=root_sha256,
            host_source_sha256=host_source_sha256,
            create=True,
        )
        claude_core._write_once(
            successor_dir / "intent.json",
            {
                "schema_version": (
                    claude_core.PUBLICATION_FINALIZATION_INTENT_SCHEMA
                ),
                "status": "submitted",
                "problem_id": "example",
                "statement_sha256": statement_sha256,
                "blueprint_sha256": blueprint_sha256,
                "host_source_sha256": host_source_sha256,
                "generation_parent_intent_sha256": root_sha256,
                "generation_parent_result_sha256": terminal_sha256,
                "operational_retry_ordinal": 1,
                "created_at_unix": time.time(),
            },
            mode=0o400,
        )

    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="multiple publication finalizations are unresolved",
    ):
        claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
        )


def test_operational_publication_retry_budget_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: "e" * 64
    )
    statement_sha256 = _statement_digest()
    blueprint_sha256 = "6" * 64

    def invalid_terminal(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        return {
            "published": False,
            "verdict": "wrong",
            "verification_status": "final",
            "publication_blocked_reason": "invalid_verifier_response",
            "repair_hints": "Repair the verifier transport.",
        }

    for expected_ordinal in range(
        claude_core.MAX_PUBLICATION_OPERATIONAL_RETRIES + 1
    ):
        intent, intent_path = claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
        )
        assert intent["operational_retry_ordinal"] == expected_ordinal
        with claude_core._publication_finalization_execution_lock(
            intent_path
        ):
            claude_core._execute_publication_finalization_verifier(
                intent=intent,
                intent_path=intent_path,
                verifier=invalid_terminal,
            )
            claude_core._settle_publication_finalization(
                intent=intent,
                intent_path=intent_path,
                status="not_published",
                publication_receipt_sha256=None,
            )

    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="operational retry budget is exhausted",
    ):
        claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
        )


def test_mathematical_nonpublication_cannot_retry_exact_blueprint_after_host_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    live_source = ["c" * 64]
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: live_source[0]
    )
    statement_sha256 = _statement_digest()
    blueprint_sha256 = "7" * 64
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )

    def wrong_terminal(commit_dispatch: object) -> dict[str, object]:
        assert callable(commit_dispatch)
        commit_dispatch()
        return {
            "published": False,
            "verdict": "wrong",
            "verification_status": "final",
            "repair_hints": "Repair the proof.",
        }

    with claude_core._publication_finalization_execution_lock(intent_path):
        claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=wrong_terminal,
        )
        claude_core._settle_publication_finalization(
            intent=intent,
            intent_path=intent_path,
            status="not_published",
            publication_receipt_sha256=None,
        )
    live_source[0] = "d" * 64
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="already has a settled verification attempt",
    ):
        claude_core._begin_publication_finalization(
            problem_id="example",
            statement_sha256=statement_sha256,
            blueprint_sha256=blueprint_sha256,
        )


@pytest.mark.parametrize("transition", ["live_source", "root_fence"])
def test_verifier_dispatch_callback_rechecks_root_source_linearization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    proof = _structured_blueprint("Incomplete proof.")
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    blueprint_sha256 = hashlib.sha256(proof.encode("utf-8")).hexdigest()
    root_session_id = "12345678-1234-4123-8123-123456789abc"
    source_a = "a" * 64
    source_b = "b" * 64
    live_source = [source_a]
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_loaded_host_source_sha256", lambda: source_a
    )
    monkeypatch.setattr(
        claude_core, "_host_source_sha256", lambda: live_source[0]
    )
    _prepare_root(
        problem_id="example",
        statement_sha256=statement_sha256,
        root_session_id=root_session_id,
        canonical_model="claude-opus-5",
    )
    profile_complete = threading.Event()
    release_dispatch = threading.Event()
    post_effects = 0

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            nonlocal post_effects
            profile_complete.set()
            assert release_dispatch.wait(timeout=10)
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            post_effects += 1
            return {"published": False, "verdict": "wrong"}

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            claude_core.verify_blueprint,
            "example",
            statement_sha256,
            root_session_id=root_session_id,
        )
        assert profile_complete.wait(timeout=10)
        if transition == "live_source":
            live_source[0] = source_b
        else:
            with claude_core._root_authority_lock("example") as problem_dir:
                manifest_path = (
                    problem_dir
                    / "roots"
                    / root_session_id
                    / "manifest.json"
                )
                claude_core._ensure_root_source_drift_fence_unlocked(
                    problem_dir=problem_dir,
                    problem_id="example",
                    statement_sha256=statement_sha256,
                    root_session_id=root_session_id,
                    council_id="council_" + "c" * 32,
                    root_manifest_sha256=claude_core.sha256_file(
                        manifest_path
                    ),
                    old_host_source_sha256=source_a,
                    replacement_host_source_sha256=source_b,
                    initial_pointer_state="consumed",
                    initial_pointer_sha256="d" * 64,
                    reason_sha256="e" * 64,
                )
        release_dispatch.set()
        with pytest.raises(claude_core.ClaudeCoreError):
            future.result(timeout=10)

    finalization_dir = claude_core._publication_finalization_dir(
        "example", blueprint_sha256
    )
    assert post_effects == 0
    assert (finalization_dir / "intent.json").is_file()
    assert not (finalization_dir / "dispatch.json").exists()
    assert not (finalization_dir / "result.json").exists()


def test_negative_verifier_result_replays_after_pre_settlement_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    proof = _structured_blueprint("Incomplete proof.")
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    verifier_calls = 0

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            nonlocal verifier_calls
            verifier_calls += 1
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            return {"published": False, "verdict": "incorrect"}

    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_existing_publication", lambda *_arguments: None
    )
    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    real_settle = claude_core._settle_publication_finalization
    failed_once = False

    def fail_before_settlement(**arguments: object) -> dict[str, object]:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise claude_core.ClaudeCoreError(
                "simulated crash after durable verifier result"
            )
        return real_settle(**arguments)

    monkeypatch.setattr(
        claude_core, "_settle_publication_finalization", fail_before_settlement
    )
    with pytest.raises(
        claude_core.ClaudeCoreError, match="after durable verifier result"
    ):
        claude_core.verify_blueprint("example", statement_sha256)
    monkeypatch.setattr(
        claude_core, "_settle_publication_finalization", real_settle
    )

    replay = claude_core.verify_blueprint("example", statement_sha256)
    assert replay == {"published": False, "verdict": "incorrect"}
    assert verifier_calls == 1


def test_oversized_negative_finalization_result_is_compacted_and_replayed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    statement_sha256 = _statement_digest()
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=statement_sha256,
        blueprint_sha256="9" * 64,
    )
    verifier_calls = 0

    def verifier(commit_dispatch: object) -> dict[str, object]:
        nonlocal verifier_calls
        verifier_calls += 1
        assert callable(commit_dispatch)
        commit_dispatch()
        return {
            "published": False,
            "verdict": "wrong",
            "verification_status": "final",
            "verification_report": {
                "summary": "A rigorous verifier found a gap.",
                "critical_errors": [
                    {"location": "statement:1", "issue": "Missing implication."}
                ],
                "gaps": [],
            },
            "repair_hints": "x" * 1_100_000,
        }

    with claude_core._publication_finalization_execution_lock(intent_path):
        first = claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=verifier,
        )
        assert first["published"] is False
        assert first["durable_result_status"] == "compacted"
        assert first["compaction_reason"] == "over_limit"
        assert first["repair_hints_truncated"] is True
        claude_core._settle_publication_finalization(
            intent=intent,
            intent_path=intent_path,
            status="not_published",
            publication_receipt_sha256=None,
        )

    result_path = intent_path.parent / "result.json"
    assert (
        result_path.stat().st_size
        <= claude_core.MAX_PUBLICATION_FINALIZATION_RESULT_BYTES
    )
    assert json.loads(result_path.read_text(encoding="utf-8"))["schema_version"] == (
        claude_core.PUBLICATION_FINALIZATION_RESULT_SCHEMA
    )
    monkeypatch.setattr(
        claude_core, "MAX_PUBLICATION_FINALIZATION_RESULT_BYTES", 512
    )
    monkeypatch.setattr(
        claude_core, "MAX_PUBLICATION_FINALIZATION_REPAIR_HINT_BYTES", 64
    )
    monkeypatch.setattr(
        claude_core, "MAX_PUBLICATION_FINALIZATION_SUMMARY_BYTES", 32
    )
    with claude_core._publication_finalization_execution_lock(intent_path):
        replay = claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=verifier,
        )
    assert replay == first
    assert verifier_calls == 1


def test_oversized_legacy_v1_finalization_result_recovers_and_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="example",
        statement_sha256=_statement_digest(),
        blueprint_sha256="8" * 64,
    )
    verifier_calls = 0

    def crashed_verifier(commit_dispatch: object) -> dict[str, object]:
        nonlocal verifier_calls
        verifier_calls += 1
        assert callable(commit_dispatch)
        commit_dispatch()
        raise RuntimeError("synthetic crash after dispatch")

    with claude_core._publication_finalization_execution_lock(intent_path):
        with pytest.raises(RuntimeError, match="after dispatch"):
            claude_core._execute_publication_finalization_verifier(
                intent=intent,
                intent_path=intent_path,
                verifier=crashed_verifier,
            )
    dispatch_path = intent_path.parent / "dispatch.json"
    legacy_result = {
        "published": False,
        "verdict": "wrong",
        "repair_hints": "z" * 1_100_000,
    }
    claude_core._write_once(
        intent_path.parent / "result.json",
        {
            "schema_version": (
                claude_core.PUBLICATION_FINALIZATION_RESULT_SCHEMA_LEGACY
            ),
            "status": "completed",
            "problem_id": intent["problem_id"],
            "statement_sha256": intent["statement_sha256"],
            "blueprint_sha256": intent["blueprint_sha256"],
            "intent_sha256": claude_core.sha256_file(intent_path),
            "dispatch_sha256": claude_core.sha256_file(dispatch_path),
            "result": legacy_result,
            "completed_at_unix": time.time(),
        },
        mode=0o400,
    )

    with claude_core._publication_finalization_execution_lock(intent_path):
        recovered = claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=crashed_verifier,
        )
        assert recovered["published"] is False
        assert recovered["durable_result_status"] == "compacted"
        claude_core._settle_publication_finalization(
            intent=intent,
            intent_path=intent_path,
            status="not_published",
            publication_receipt_sha256=None,
        )
    with claude_core._publication_finalization_execution_lock(intent_path):
        assert claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=crashed_verifier,
        ) == recovered
    assert verifier_calls == 1


def test_concurrent_finalization_dispatches_verifier_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    proof = _structured_blueprint("Incomplete proof.")
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    verifier_calls = 0
    both_admitted = threading.Event()

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            nonlocal verifier_calls
            verifier_calls += 1
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            assert both_admitted.wait(timeout=5)
            return {"published": False, "verdict": "incorrect"}

    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_existing_publication", lambda *_arguments: None
    )
    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    real_begin = claude_core._begin_publication_finalization
    admission_count = 0
    admission_lock = threading.Lock()

    def observe_admission(**arguments: object) -> tuple[dict[str, object], Path]:
        nonlocal admission_count
        admitted = real_begin(**arguments)
        with admission_lock:
            admission_count += 1
            if admission_count == 2:
                both_admitted.set()
        return admitted

    monkeypatch.setattr(
        claude_core, "_begin_publication_finalization", observe_admission
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                claude_core.verify_blueprint, "example", statement_sha256
            )
            for _index in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert results == [
        {"published": False, "verdict": "incorrect"},
        {"published": False, "verdict": "incorrect"},
    ]
    assert verifier_calls == 1


def test_immediate_publication_must_match_finalization_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    proof = _structured_blueprint("Complete proof.")
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    statement_sha256 = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    proof_sha256 = hashlib.sha256(proof.encode()).hexdigest()
    published = False

    def existing_publication(
        _problem_id: str, _requested_statement_sha256: str
    ) -> dict[str, object] | None:
        if not published:
            return None
        return {
            "status": "published",
            "proof_sha256": "b" * 64,
            "publication_receipt_sha256": "c" * 64,
        }

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            nonlocal published
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            published = True
            return {"published": True}

    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "_existing_publication", existing_publication)
    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)

    with pytest.raises(claude_core.ClaudeCoreError, match="differs"):
        claude_core.verify_blueprint("example", statement_sha256)
    settlement_path = (
        tmp_path
        / "state"
        / "example"
        / "publication_finalizations"
        / proof_sha256
        / "settlement.json"
    )
    assert not settlement_path.exists()


def test_amendment_rechecks_source_statement_before_parent_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    source_path = generation_root / "data" / "source.md"
    verified_path = generation_root / "results" / "source" / "blueprint_verified.md"
    source_path.parent.mkdir(parents=True)
    verified_path.parent.mkdir(parents=True)
    statement_a = b"Statement A.\n"
    statement_b = b"Statement B.\n"
    source_path.write_bytes(statement_b)
    proof = b"Verified proof.\n"
    verified_path.write_bytes(proof)
    digest_a = hashlib.sha256(statement_a).hexdigest()
    receipt_sha256 = "a" * 64
    publication = {
        "status": "published",
        "publication_receipt_sha256": receipt_sha256,
        "published_path": str(verified_path),
        "proof_sha256": hashlib.sha256(proof).hexdigest(),
    }
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core, "_require_claude_root_lineage", lambda **_arguments: None
    )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda _problem_id, _statement_sha256: publication,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="statement digest changed"):
        claude_core.prepare_publication_amendment(
            source_problem_id="source",
            source_statement_sha256=digest_a,
            source_receipt_sha256=receipt_sha256,
            target_problem_id="source-amend-1",
            reason="statement race regression",
        )
    assert not (
        tmp_path / "state" / "source-amend-1" / "amendment_parent.json"
    ).exists()
    assert not (generation_root / "data" / "source-amend-1.md").exists()


@pytest.mark.parametrize("concurrent", [False, True])
def test_amendment_preparation_claim_allows_only_one_successor(
    concurrent: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_root = tmp_path / "generation"
    receipt_root = tmp_path / "receipts"
    state_root = tmp_path / "state"
    receipt_root.mkdir()
    (generation_root / "data").mkdir(parents=True)
    verified_path = generation_root / "results/source/blueprint_verified.md"
    verified_path.parent.mkdir(parents=True)
    proof = b"Verified source proof.\n"
    verified_path.write_bytes(proof)
    statement = b"Source statement.\n"
    statement_sha256 = hashlib.sha256(statement).hexdigest()
    receipt_sha256 = "a" * 64
    publication = {
        "status": "published",
        "publication_receipt_sha256": receipt_sha256,
        "published_path": str(verified_path),
        "proof_sha256": hashlib.sha256(proof).hexdigest(),
    }
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        claude_core, "_require_claude_root_lineage", lambda **_arguments: None
    )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda problem_id, _statement_sha256: (
            publication if problem_id == "source" else None
        ),
    )
    monkeypatch.setattr(
        claude_core,
        "_statement",
        lambda _problem_id: (
            generation_root / "data/source.md",
            statement,
            statement_sha256,
        ),
    )
    monkeypatch.setattr(
        claude_core, "_canonical_target_sha256", lambda _raw: "b" * 64
    )
    targets = ("source-amend-a", "source-amend-b")
    successes: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def prepare(target: str) -> None:
        try:
            successes.append(
                claude_core.prepare_publication_amendment(
                    source_problem_id="source",
                    source_statement_sha256=statement_sha256,
                    source_receipt_sha256=receipt_sha256,
                    target_problem_id=target,
                    reason="one durable successor",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    if concurrent:
        barrier = threading.Barrier(3)

        def concurrent_prepare(target: str) -> None:
            barrier.wait()
            prepare(target)

        threads = [
            threading.Thread(target=concurrent_prepare, args=(target,))
            for target in targets
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
    else:
        prepare(targets[0])
        prepare(targets[1])

    assert len(successes) == 1, failures
    assert len(failures) == 1
    assert isinstance(failures[0], claude_core.ClaudeCoreError)
    assert "different preparation" in str(failures[0])
    winner = str(successes[0]["target_problem_id"])
    loser = next(target for target in targets if target != winner)
    assert not (state_root / loser / "amendment_parent.json").exists()
    assert not (generation_root / "data" / f"{loser}.md").exists()
    assert not (generation_root / "results" / loser).exists()


def test_amendment_claim_backfills_one_legacy_prepared_parent_before_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_root = tmp_path / "generation"
    receipt_root = tmp_path / "receipts"
    state_root = tmp_path / "state"
    receipt_root.mkdir()
    (generation_root / "data").mkdir(parents=True)
    verified_path = generation_root / "results/source/blueprint_verified.md"
    verified_path.parent.mkdir(parents=True)
    proof = b"Verified source proof.\n"
    verified_path.write_bytes(proof)
    statement = b"Source statement.\n"
    statement_sha256 = hashlib.sha256(statement).hexdigest()
    receipt_sha256 = "a" * 64
    publication = {
        "status": "published",
        "publication_receipt_sha256": receipt_sha256,
        "published_path": str(verified_path),
        "proof_sha256": hashlib.sha256(proof).hexdigest(),
    }
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    monkeypatch.setattr(
        claude_core, "_require_claude_root_lineage", lambda **_arguments: None
    )
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda problem_id, _statement_sha256: (
            publication if problem_id == "source" else None
        ),
    )
    monkeypatch.setattr(
        claude_core,
        "_statement",
        lambda _problem_id: (
            generation_root / "data/source.md",
            statement,
            statement_sha256,
        ),
    )
    monkeypatch.setattr(
        claude_core, "_canonical_target_sha256", lambda _raw: "b" * 64
    )
    reason = "legacy preparation migration"
    target_b = "source-amend-b"
    target_c = "source-amend-c"
    common = {
        "source_problem_id": "source",
        "source_statement_sha256": statement_sha256,
        "source_receipt_sha256": receipt_sha256,
        "reason": reason,
    }
    prepared_b = claude_core.prepare_publication_amendment(
        **common, target_problem_id=target_b
    )
    claim_path = claude_core._amendment_preparation_claim_path(receipt_sha256)
    claim_path.chmod(0o600)
    claim_path.unlink()

    with pytest.raises(claude_core.ClaudeCoreError, match="different preparation"):
        claude_core.prepare_publication_amendment(
            **common, target_problem_id=target_c
        )

    claim = claude_core._read_amendment_preparation_claim(
        source_receipt_sha256=receipt_sha256
    )
    assert claim is not None
    assert claim["target_problem_id"] == target_b
    assert claim["amendment_id"] == prepared_b["amendment_id"]
    assert claude_core.prepare_publication_amendment(
        **common, target_problem_id=target_b
    ) == prepared_b
    assert not (state_root / target_c / "amendment_parent.json").exists()
    assert not (generation_root / "data" / f"{target_c}.md").exists()
    assert not (generation_root / "results" / target_c).exists()


def test_immutable_amendment_supersedes_exact_parent_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    receipt_root = tmp_path / "receipts"
    state_root = tmp_path / "state"
    source_problem = "source"
    target_problem = "source-amend-1"
    statement = "Statement.\n"
    proof = _structured_blueprint("Original proof.")
    statement_path = generation_root / "data" / f"{source_problem}.md"
    result_dir = generation_root / "results" / source_problem
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    receipt_root.mkdir()
    statement_path.write_text(statement, encoding="utf-8")
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    (result_dir / "blueprint_verified.md").write_text(proof, encoding="utf-8")
    statement_sha = hashlib.sha256(statement.encode()).hexdigest()
    proof_sha = hashlib.sha256(proof.encode()).hexdigest()
    item_id, context_digest, adaptive_digest, attestation = _publication_context(
        statement=statement, proof=proof
    )
    source_receipt = {
        "schema_version": "rethlas-publication-v2",
        "problem_id": source_problem,
        "statement_digest": statement_sha,
        "proof_digest": proof_sha,
        "context_digest": context_digest,
        "adaptive_context_digest": adaptive_digest,
        "item_context_attestations": [attestation],
        "checked_item_ids": [item_id],
        "verified_path": str(result_dir / "blueprint_verified.md"),
        "published_bytes": len(proof.encode()),
    }
    source_receipt_path = receipt_root / f"{source_problem}.json"
    source_receipt_path.write_text(
        claude_core.canonical_json(source_receipt) + "\n", encoding="utf-8"
    )
    source_receipt_sha = hashlib.sha256(source_receipt_path.read_bytes()).hexdigest()
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", state_root)
    _prepare_root(
        problem_id=source_problem,
        statement_sha256=statement_sha,
        root_session_id="12345678-1234-4123-8123-123456789abc",
        canonical_model="claude-opus-5",
    )

    amendment_args = {
        "source_problem_id": source_problem,
        "source_statement_sha256": statement_sha,
        "source_receipt_sha256": source_receipt_sha,
        "target_problem_id": target_problem,
        "reason": "independent review found a false stronger claim",
    }
    real_write_bytes_once_at = claude_core._write_bytes_once_at
    failed_once = False

    def fail_after_parent_intent(*args: object, **kwargs: object) -> str:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise claude_core.ClaudeCoreError("simulated preparation crash")
        return real_write_bytes_once_at(*args, **kwargs)

    monkeypatch.setattr(
        claude_core, "_write_bytes_once_at", fail_after_parent_intent
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="simulated preparation"):
        claude_core.prepare_publication_amendment(**amendment_args)
    assert claude_core._read_amendment_parent(target_problem) is not None
    monkeypatch.setattr(
        claude_core, "_write_bytes_once_at", real_write_bytes_once_at
    )
    prepared = claude_core.prepare_publication_amendment(**amendment_args)
    assert claude_core.prepare_publication_amendment(**amendment_args) == prepared
    target_statement_path = generation_root / "data" / f"{target_problem}.md"
    target_result_dir = generation_root / "results" / target_problem
    assert target_statement_path.read_bytes() == statement.encode()
    assert (target_result_dir / "blueprint.md").read_bytes() == proof.encode()
    assert prepared["source_receipt_sha256"] == source_receipt_sha

    amended_proof = _structured_blueprint("Corrected proof.")
    amended_sha = hashlib.sha256(amended_proof.encode()).hexdigest()
    (target_result_dir / "blueprint.md").write_text(amended_proof, encoding="utf-8")
    (target_result_dir / "blueprint_verified.md").write_text(
        amended_proof, encoding="utf-8"
    )
    (
        amended_item_id,
        amended_context_digest,
        amended_adaptive_digest,
        amended_attestation,
    ) = _publication_context(statement=statement, proof=amended_proof)
    supersedes = [
        {
            "problem_id": source_problem,
            "receipt_sha256": source_receipt_sha,
            "proof_digest": proof_sha,
        }
    ]
    passes = [
        {
            "pass_index": index,
            "verification_attempt_id": f"veratt_{index}".ljust(39, str(index)),
            "verifier_run_id": f"run-{index}",
            "verifier_model": "gpt-6-astra",
            "verifier_reasoning_effort": "max",
            "verifier_service_version": "0.3.0",
            "verification_role": (
                "primary" if index == 1 else "adversarial_full_claim_audit"
            ),
            "response_sha256": str(index) * 64,
            "verdict": "correct",
        }
        for index in (1, 2)
    ]
    _parser_raw, parser_sha256 = claude_core._read_proof_context_source()
    target_receipt = {
        "schema_version": "rethlas-publication-v6",
        "state": "active",
        "problem_id": target_problem,
        "statement_source_digest": statement_sha,
        "canonical_target_digest": claude_core._canonical_target_sha256(
            statement.encode()
        ),
        "proof_digest": amended_sha,
        "context_digest": amended_context_digest,
        "adaptive_context_digest": amended_adaptive_digest,
        "item_context_attestations": [amended_attestation],
        "checked_item_ids": [amended_item_id],
        "verified_path": str(target_result_dir / "blueprint_verified.md"),
        "published_bytes": len(amended_proof.encode()),
        "published_at_utc": "2026-08-26T12:00:00+00:00",
        "verification_quorum": 2,
        "verification_passes": passes,
        "supersedes": supersedes,
        "proof_context": {
            "schema_version": claude_core.PUBLICATION_PROOF_CONTEXT_SCHEMA,
            "source_sha256": parser_sha256,
            "proof_item_schema_version": 1,
            "proof_context_schema_version": 2,
            "aggregate_context_schema_version": 1,
            "adaptive_aggregate_context_schema_version": 2,
        },
        "verification_limits": {
            "context_max_chars": 200_000,
            "max_expansion_rounds": 2,
            "max_expanded_proofs": 8,
            "max_expanded_proof_chars": 200_000,
            "max_proof_items": 20_000,
            "max_receipt_bytes": 16_000_000,
            "max_blueprint_bytes": 8_000_000,
            "max_blueprint_chars": 2_000_000,
        },
        "publication_target_precondition": {
            "kind": "absent",
            "st_dev": None,
            "st_ino": None,
            "st_size": None,
            "st_mtime_ns": None,
            "content_sha256": None,
        },
    }
    _prepare_root(
        problem_id=target_problem,
        statement_sha256=statement_sha,
        root_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        canonical_model="claude-opus-5",
    )
    parent = claude_core._read_amendment_parent(target_problem)
    assert parent is not None
    finalization_intent, finalization_intent_path = (
        claude_core._begin_publication_finalization(
            problem_id=target_problem,
            statement_sha256=statement_sha,
            blueprint_sha256=amended_sha,
        )
    )
    claude_core._reserve_publication_amendment(
        parent=parent,
        target_blueprint_sha256=amended_sha,
        finalization_intent_path=finalization_intent_path,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="reserved"):
        claude_core.retract_publication(
            problem_id=source_problem,
            statement_sha256=statement_sha,
            receipt_sha256=source_receipt_sha,
            reason="must not overtake the admitted amendment verifier",
        )
    target_receipt_path = receipt_root / f"{target_problem}.json"
    target_receipt_path.write_text(
        claude_core.canonical_json(target_receipt) + "\n", encoding="utf-8"
    )
    replacement = claude_core._existing_publication(target_problem, statement_sha)
    assert replacement is not None
    target_receipt_sha = hashlib.sha256(target_receipt_path.read_bytes()).hexdigest()
    with pytest.raises(claude_core.ClaudeCoreError, match="finalization is unresolved"):
        claude_core.retract_publication(
            problem_id=target_problem,
            statement_sha256=statement_sha,
            receipt_sha256=target_receipt_sha,
            reason="must not overtake upstream amendment completion",
        )
    real_write_once = claude_core._write_once
    reservation_settlement_failed = False

    def fail_after_source_supersede(
        path: Path, value: object, *, mode: int = 0o400
    ) -> str:
        nonlocal reservation_settlement_failed
        if (
            path.name.endswith(".amendment-settlement.json")
            and not reservation_settlement_failed
        ):
            reservation_settlement_failed = True
            raise claude_core.ClaudeCoreError(
                "simulated crash after source supersede"
            )
        return real_write_once(path, value, mode=mode)

    monkeypatch.setattr(claude_core, "_write_once", fail_after_source_supersede)
    with pytest.raises(claude_core.ClaudeCoreError, match="after source supersede"):
        claude_core._complete_publication_amendment(
            parent=parent,
            replacement=replacement,
        )
    assert not (finalization_intent_path.parent / "settlement.json").exists()
    source_midway = claude_core._existing_publication(source_problem, statement_sha)
    assert source_midway is not None and source_midway["status"] == "superseded"
    monkeypatch.setattr(claude_core, "_write_once", real_write_once)
    completion_failed_once = False

    def fail_after_supersede(
        path: Path, value: object, *, mode: int = 0o400
    ) -> str:
        nonlocal completion_failed_once
        if path.name == "amendment_completion.json" and not completion_failed_once:
            completion_failed_once = True
            raise claude_core.ClaudeCoreError("simulated completion crash")
        return real_write_once(path, value, mode=mode)

    monkeypatch.setattr(claude_core, "_write_once", fail_after_supersede)
    with pytest.raises(claude_core.ClaudeCoreError, match="simulated completion"):
        claude_core._complete_publication_amendment(
            parent=parent,
            replacement=replacement,
        )
    monkeypatch.setattr(claude_core, "_write_once", real_write_once)
    completed = claude_core._complete_publication_amendment(
        parent=parent,
        replacement=replacement,
    )
    assert (
        claude_core._complete_publication_amendment(
            parent=parent,
            replacement=replacement,
        )
        == completed
    )
    claude_core._settle_publication_finalization(
        intent=finalization_intent,
        intent_path=finalization_intent_path,
        status="published",
        publication_receipt_sha256=replacement[
            "publication_receipt_sha256"
        ],
    )

    source_after = claude_core._existing_publication(source_problem, statement_sha)
    assert source_after is not None
    assert source_after["status"] == "superseded"
    assert source_after["published"] is False
    assert source_after["publication_status"]["replacement_problem_id"] == target_problem

    retraction = claude_core.retract_publication(
        problem_id=target_problem,
        statement_sha256=statement_sha,
        receipt_sha256=target_receipt_sha,
        reason="replacement itself requires withdrawal",
    )
    assert (
        claude_core.retract_publication(
            problem_id=target_problem,
            statement_sha256=statement_sha,
            receipt_sha256=target_receipt_sha,
            reason="replacement itself requires withdrawal",
        )
        == retraction
    )


def test_amendment_nonpublication_releases_source_retraction_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    source_receipt_sha256 = "1" * 64
    source_proof_digest = "2" * 64
    statement_sha256 = "3" * 64
    target_blueprint_sha256 = "4" * 64
    parent = {
        "schema_version": claude_core.AMENDMENT_PARENT_SCHEMA,
        "amendment_id": "amend_" + "5" * 32,
        "source_problem_id": "source",
        "source_statement_sha256": statement_sha256,
        "source_canonical_target_sha256": "6" * 64,
        "source_proof_digest": source_proof_digest,
        "source_receipt_sha256": source_receipt_sha256,
        "target_problem_id": "target",
        "reason": "release after a negative verifier result",
        "prepared_at_utc": "2026-08-26T12:00:00+00:00",
    }
    publication = {
        "status": "published",
        "publication_receipt_sha256": source_receipt_sha256,
        "proof_sha256": source_proof_digest,
    }
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda problem_id, _statement_sha256: (
            publication if problem_id == "source" else None
        ),
    )
    monkeypatch.setattr(
        claude_core, "_require_claude_root_lineage", lambda **_arguments: {}
    )
    written_statuses: list[dict[str, object]] = []

    def write_status(**arguments: object) -> dict[str, object]:
        written_statuses.append(arguments)
        return {"state": arguments["state"], "reason": arguments["reason"]}

    monkeypatch.setattr(claude_core, "_write_publication_status", write_status)
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="target",
        statement_sha256=statement_sha256,
        blueprint_sha256=target_blueprint_sha256,
    )
    claude_core._reserve_publication_amendment(
        parent=parent,
        target_blueprint_sha256=target_blueprint_sha256,
        finalization_intent_path=intent_path,
    )
    with pytest.raises(claude_core.ClaudeCoreError, match="reserved"):
        claude_core.retract_publication(
            problem_id="source",
            statement_sha256=statement_sha256,
            receipt_sha256=source_receipt_sha256,
            reason="withdraw after failed amendment",
        )
    claude_core._settle_publication_finalization(
        intent=intent,
        intent_path=intent_path,
        status="not_published",
        publication_receipt_sha256=None,
    )
    released = claude_core._release_publication_amendment_reservation(
        parent=parent,
        finalization_intent=intent,
        finalization_intent_path=intent_path,
    )
    assert released["status"] == "released"
    generation_root = tmp_path / "generation"
    changed_blueprint = _structured_blueprint("A corrected second attempt.")
    changed_blueprint_sha256 = hashlib.sha256(
        changed_blueprint.encode()
    ).hexdigest()
    changed_result_dir = generation_root / "results" / "target"
    changed_result_dir.mkdir(parents=True)
    (changed_result_dir / "blueprint.md").write_text(
        changed_blueprint, encoding="utf-8"
    )
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(
        claude_core,
        "_statement",
        lambda _problem_id: (Path("target.md"), b"Statement.\n", statement_sha256),
    )
    monkeypatch.setattr(
        claude_core,
        "_read_amendment_parent",
        lambda problem_id: parent if problem_id == "target" else None,
    )
    monkeypatch.setattr(
        claude_core,
        "_canonical_target_sha256",
        lambda _statement_raw: parent["source_canonical_target_sha256"],
    )

    class VerifierMustNotRun:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**_arguments: object) -> dict[str, object]:
            raise AssertionError("terminal amendment dispatched a verifier")

    monkeypatch.setattr(claude_core, "_legacy", lambda: VerifierMustNotRun)
    with pytest.raises(claude_core.ClaudeCoreError, match="attempt is terminal"):
        claude_core.verify_blueprint("target", statement_sha256)
    assert not claude_core._publication_finalization_dir(
        "target", changed_blueprint_sha256
    ).exists()
    retracted = claude_core.retract_publication(
        problem_id="source",
        statement_sha256=statement_sha256,
        receipt_sha256=source_receipt_sha256,
        reason="withdraw after failed amendment",
    )
    assert retracted["state"] == "retracted"
    assert written_statuses[-1]["state"] == "retracted"


def test_amendment_crash_before_reservation_reconciles_after_source_retraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    source_receipt_sha256 = "1" * 64
    source_proof_digest = "2" * 64
    statement_sha256 = "3" * 64
    target_blueprint_sha256 = "4" * 64
    parent = {
        "schema_version": claude_core.AMENDMENT_PARENT_SCHEMA,
        "amendment_id": "amend_" + "5" * 32,
        "source_problem_id": "source",
        "source_statement_sha256": statement_sha256,
        "source_canonical_target_sha256": "6" * 64,
        "source_proof_digest": source_proof_digest,
        "source_receipt_sha256": source_receipt_sha256,
        "target_problem_id": "target",
        "reason": "repair admission after a pre-reservation crash",
        "prepared_at_utc": "2026-08-26T12:00:00+00:00",
    }
    source_status = "published"

    def existing_publication(
        problem_id: str, _statement_sha256: str
    ) -> dict[str, object] | None:
        if problem_id != "source":
            return None
        return {
            "status": source_status,
            "publication_receipt_sha256": source_receipt_sha256,
            "proof_sha256": source_proof_digest,
        }

    def write_status(**arguments: object) -> dict[str, object]:
        nonlocal source_status
        source_status = str(arguments["state"])
        return {"state": source_status, "reason": arguments["reason"]}

    monkeypatch.setattr(claude_core, "_existing_publication", existing_publication)
    monkeypatch.setattr(
        claude_core, "_require_claude_root_lineage", lambda **_arguments: {}
    )
    monkeypatch.setattr(claude_core, "_write_publication_status", write_status)

    # This is the exact durable prefix left by a crash between
    # _begin_publication_finalization and _reserve_publication_amendment.
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="target",
        statement_sha256=statement_sha256,
        blueprint_sha256=target_blueprint_sha256,
    )
    assert not claude_core._amendment_reservation_path(
        source_receipt_sha256
    ).exists()
    retracted = claude_core.retract_publication(
        problem_id="source",
        statement_sha256=statement_sha256,
        receipt_sha256=source_receipt_sha256,
        reason="source retraction wins before amendment reservation",
    )
    assert retracted["state"] == "retracted"

    claude_core._reconcile_amendment_reservation(parent)
    settlement = claude_core._read_publication_finalization_settlement(
        intent_path.parent / "settlement.json",
        intent=intent,
        intent_sha256=claude_core.sha256_file(intent_path),
    )
    assert settlement["status"] == "not_published"
    claude_core._assert_no_unsettled_publication_finalization(
        problem_id="target", statement_sha256=statement_sha256
    )


def test_claude_root_mcp_exposes_only_role_gated_host_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "12345678-1234-4123-8123-123456789abc"
    digest = _statement_digest()
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_PROBLEM_ID", "example")
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256", digest)
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_SESSION_ID", session_id)
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_MODEL", "claude-opus-5")
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_LAUNCH_MODEL", "claude-opus-5[1m]"
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_PROVIDER", "vertex")
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_PROVIDER_BINDING_SHA256",
        hashlib.sha256(b"claude-opus-5[1m]").hexdigest(),
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_CLI_SHA256", "1" * 64)
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_CLI_VERSION", "test-claude-2.1.246"
    )
    monkeypatch.setenv("RETHLAS_CLAUDE_ROOT_CONTEXT_WINDOW", "1000000")
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256", "3" * 64
    )
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256", "4" * 64
    )
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_CODEX_BIN", str(Path(sys.executable).resolve())
    )
    observed_memory_call: dict[str, object] = {}
    observed_search_calls: list[tuple[str, dict[str, object]]] = []
    observed_math_call: dict[str, object] = {}

    class FakeLegacy:
        @staticmethod
        def memory_search(**kwargs: object) -> dict[str, object]:
            observed_memory_call.update(kwargs)
            return {
                "count": 1,
                "response_limited": True,
                "response_max_utf8_bytes": 60_000,
            }

        @staticmethod
        def search_matlas_theorems(**kwargs: object) -> dict[str, object]:
            observed_search_calls.append(("matlas", kwargs))
            return {"provider": "matlas", "count": 1}

        @staticmethod
        def search_arxiv_theorems_for_problem(
            **kwargs: object,
        ) -> dict[str, object]:
            observed_search_calls.append(("arxiv", kwargs))
            return {"provider": "arxiv", "count": 1}

        @staticmethod
        def read_arxiv_primary_for_problem(
            **kwargs: object,
        ) -> dict[str, object]:
            observed_search_calls.append(("arxiv_primary", kwargs))
            return {"provider": "arxiv_official_html_v1", "count": 1}

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy())

    def fake_math_experiment(**kwargs: object) -> dict[str, object]:
        observed_math_call.update(kwargs)
        return {
            "schema_version": claude_core.MATH_EXPERIMENT_RECEIPT_SCHEMA,
            "status": "created",
            "execution": {
                "execution_status": "completed",
                "evidence_class": "unverified_computational_diagnostic",
                "stdout": "42\n",
            },
        }

    monkeypatch.setattr(
        claude_core, "run_math_experiment", fake_math_experiment
    )

    def reject_blueprint(
        *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        raise claude_core.BlueprintContractError(
            "final statement differs from canonical target"
        )

    monkeypatch.setattr(claude_core, "write_blueprint", reject_blueprint)
    monkeypatch.setattr(claude_core, "verify_blueprint", reject_blueprint)
    app = claude_core.build_mcp_app()
    manager = getattr(app, "_tool_manager", None)
    tools = getattr(manager, "_tools", {}) if manager is not None else {}
    assert set(tools) == {
        "memory_search",
        "memory_append_batch",
        "search_matlas_theorems",
        "search_arxiv_theorems",
        "read_arxiv_primary",
        "run_math_experiment",
        "prepare_pro_gap_query",
        "get_pro_gap_query",
        "ingest_pro_gap_response",
        "get_pro_gap_response",
        "run_three_route_cohort",
        "edit_blueprint",
        "write_blueprint",
        "verify_blueprint_service",
    }
    memory_result = tools["memory_search"].fn(
        problem_id="example",
        query="bounded query",
        channels=["proof_steps"],
        limit_per_channel=7,
        max_chars=64_000,
    )
    assert memory_result["response_limited"] is True
    assert observed_memory_call == {
        "problem_id": "example",
        "query": "bounded query",
        "channels": ["proof_steps"],
        "limit_per_channel": 7,
        "max_chars": 64_000,
    }
    with pytest.raises(claude_core.ClaudeCoreError, match="does not permit"):
        tools["search_matlas_theorems"].fn(
            problem_id="example", query="named gap", num_results=2
        )
    with pytest.raises(claude_core.ClaudeCoreError, match="does not permit"):
        tools["read_arxiv_primary"].fn(
            problem_id="example",
            arxiv_id="1711.11482",
            locator="Theorem 4.22",
        )
    monkeypatch.setattr(
        claude_core,
        "statement_retrieval_policy",
        lambda **_kwargs: {"mode": "matlas_arxiv"},
    )
    assert tools["search_matlas_theorems"].fn(
        problem_id="example", query="named Matlas gap", num_results=2
    )["provider"] == "matlas"
    assert tools["search_arxiv_theorems"].fn(
        problem_id="example", query="named arXiv gap", num_results=3
    )["provider"] == "arxiv"
    assert tools["read_arxiv_primary"].fn(
        problem_id="example",
        arxiv_id="1711.11482",
        locator="Theorem 4.22",
        max_excerpt_bytes=12_000,
    )["provider"] == "arxiv_official_html_v1"
    assert observed_search_calls == [
        ("matlas", {"query": "named Matlas gap", "num_results": 2}),
        (
            "arxiv",
            {
                "problem_id": "example",
                "query": "named arXiv gap",
                "num_results": 3,
                "expected_statement_sha256": digest,
            },
        ),
        (
            "arxiv_primary",
            {
                "problem_id": "example",
                "arxiv_id": "1711.11482",
                "locator": "Theorem 4.22",
                "max_excerpt_bytes": 12_000,
                "expected_statement_sha256": digest,
            },
        ),
    ]
    math_result = tools["run_math_experiment"].fn(
        problem_id="example",
        experiment_id="exp_route_discriminator",
        purpose="Falsify one candidate route before council admission.",
        code="print(6 * 7)",
        timeout_seconds=12,
    )
    assert math_result["execution"]["stdout"] == "42\n"
    assert observed_math_call == {
        "problem_id": "example",
        "statement_sha256": digest,
        "root_session_id": session_id,
        "experiment_id": "exp_route_discriminator",
        "purpose": "Falsify one candidate route before council admission.",
        "code": "print(6 * 7)",
        "timeout_seconds": 12,
        "codex_bin": Path(sys.executable).resolve(),
        "expected_python_runtime_sha256": "3" * 64,
        "expected_host_source_sha256": claude_core._host_source_sha256(),
    }
    preflight = tools["write_blueprint"].fn(
        problem_id="example",
        statement_sha256=digest,
        blueprint_markdown="invalid candidate",
    )
    assert preflight == {
        "schema_version": "rethlas_blueprint_preflight_failure_v1",
        "status": "preflight_failed",
        "category": "blueprint_contract",
        "operation": "write_blueprint",
        "problem_id": "example",
        "error": "final statement differs from canonical target",
        "repair_hint": (
            "Use paper-like H1 proof items with one explicit "
            "rethlas-depends-on comment each. The final item's ## statement "
            "must exactly equal the canonical problem target; put every "
            "explanation, qualification, or paraphrase in ## proof instead."
        ),
        "retry_allowed": True,
        "verifier_dispatched": False,
        "published": False,
    }
    verify_preflight = tools["verify_blueprint_service"].fn(
        problem_id="example",
        statement_sha256=digest,
    )
    assert verify_preflight == {
        **preflight,
        "operation": "verify_blueprint_service",
    }


def test_root_mcp_materializes_only_the_signed_publication_success_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "12345678-1234-4123-8123-123456789abc"
    digest = _statement_digest()
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )
    bindings = {
        "RETHLAS_CLAUDE_ROOT_PROBLEM_ID": "example",
        "RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256": digest,
        "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        "RETHLAS_CLAUDE_ROOT_MODEL": "claude-opus-5",
        "RETHLAS_CLAUDE_ROOT_LAUNCH_MODEL": "claude-opus-5[1m]",
        "RETHLAS_CLAUDE_ROOT_PROVIDER": "vertex",
        "RETHLAS_CLAUDE_ROOT_PROVIDER_BINDING_SHA256": hashlib.sha256(
            b"claude-opus-5[1m]"
        ).hexdigest(),
        "RETHLAS_CLAUDE_ROOT_CLI_SHA256": "1" * 64,
        "RETHLAS_CLAUDE_ROOT_CLI_VERSION": "test-claude-2.1.246",
        "RETHLAS_CLAUDE_ROOT_CONTEXT_WINDOW": "1000000",
        "RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256": "3" * 64,
        "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": "4" * 64,
        "RETHLAS_CLAUDE_ROOT_CODEX_BIN": str(Path(sys.executable).resolve()),
    }
    for key, value in bindings.items():
        monkeypatch.setenv(key, value)

    publication = {
        "schema_version": "rethlas_existing_publication_v1",
        "status": "published",
        "problem_id": "example",
        "statement_sha256": digest,
        "proof_sha256": "a" * 64,
        "checked_item_ids": ["pi_" + "b" * 24, "pi_" + "c" * 24],
        "published": True,
        "published_path": "/host/blueprint_verified.md",
        "publication_receipt_path": "/host/receipt.json",
        "publication_receipt_sha256": "d" * 64,
        "publication_status": None,
        "publication_schema": "rethlas-publication-v6",
        "published_at_utc": "2026-09-01T00:00:00+00:00",
        "supersedes": [],
    }
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda *_arguments, **_kwargs: publication,
    )
    memory_calls: list[dict[str, object]] = []

    class FakeLegacy:
        @staticmethod
        def memory_append_batch(**kwargs: object) -> dict[str, object]:
            memory_calls.append(kwargs)
            return {
                "status": "ok",
                "count": 1,
                "checkpoint_sha256": "e" * 64,
            }

        @staticmethod
        def _exact_checkpoint_tool_result(value: object) -> object:
            return value

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy())
    app = claude_core.build_mcp_app()
    tool = app._tool_manager._tools["memory_append_batch"].fn
    item_type = get_args(tool.__annotations__["items"])[0]
    forged = item_type(
        channel="verification_reports",
        record={"status": "published", "model_authored": True},
        active=True,
        supersedes=[],
    )

    rejected = tool(problem_id="example", items=[forged])
    assert rejected["status"] == "preflight_failed"
    assert rejected["category"] == "publication_success_checkpoint"
    assert rejected["publication_status"] == "published"
    assert rejected["retry_allowed"] is True
    assert rejected["memory_written"] is False
    assert "items=[]" in rejected["repair_hint"]
    assert memory_calls == []

    expected_items = claude_core._publication_success_checkpoint_items(
        publication
    )
    committed = tool(problem_id="example", items=[])
    replayed = tool(problem_id="example", items=[])
    assert committed == replayed == {
        "status": "ok",
        "count": 1,
        "checkpoint_sha256": "e" * 64,
    }
    assert memory_calls == [
        {"problem_id": "example", "items": expected_items},
        {"problem_id": "example", "items": expected_items},
    ]
    assert expected_items == [
        {
            "channel": "verification_reports",
            "record": {
                "schema_version": (
                    claude_core.PUBLICATION_SUCCESS_CHECKPOINT_RECORD_SCHEMA
                ),
                "status": "published",
                "problem_id": "example",
                "statement_sha256": digest,
                "proof_sha256": "a" * 64,
                "publication_receipt_sha256": "d" * 64,
                "published_at_utc": "2026-09-01T00:00:00+00:00",
                "checked_item_count": 2,
            },
            "active": True,
            "supersedes": [],
        }
    ]

    retracted = {**publication, "status": "retracted", "published": False}
    monkeypatch.setattr(
        claude_core,
        "_existing_publication",
        lambda *_arguments, **_kwargs: retracted,
    )
    closed = tool(problem_id="example", items=[])
    assert closed["status"] == "preflight_failed"
    assert closed["publication_status"] == "retracted"
    assert closed["retry_allowed"] is False
    assert len(memory_calls) == 2


def test_root_mcp_verify_handler_does_not_reenter_root_authority_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation_root = tmp_path / "generation"
    statement_path = generation_root / "data" / "example.md"
    result_dir = generation_root / "results" / "example"
    statement_path.parent.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    statement_path.write_text("Statement.\n", encoding="utf-8")
    (result_dir / "blueprint.md").write_text(
        _structured_blueprint("Complete proof."), encoding="utf-8"
    )
    digest = hashlib.sha256(statement_path.read_bytes()).hexdigest()
    session_id = "12345678-1234-4123-8123-123456789abc"
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", tmp_path / "receipts")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )
    bindings = {
        "RETHLAS_CLAUDE_ROOT_PROBLEM_ID": "example",
        "RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256": digest,
        "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        "RETHLAS_CLAUDE_ROOT_MODEL": "claude-opus-5",
        "RETHLAS_CLAUDE_ROOT_LAUNCH_MODEL": "claude-opus-5[1m]",
        "RETHLAS_CLAUDE_ROOT_PROVIDER": "vertex",
        "RETHLAS_CLAUDE_ROOT_PROVIDER_BINDING_SHA256": hashlib.sha256(
            b"claude-opus-5[1m]"
        ).hexdigest(),
        "RETHLAS_CLAUDE_ROOT_CLI_SHA256": "1" * 64,
        "RETHLAS_CLAUDE_ROOT_CLI_VERSION": "test-claude-2.1.246",
        "RETHLAS_CLAUDE_ROOT_CONTEXT_WINDOW": "1000000",
        "RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256": "3" * 64,
        "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": "4" * 64,
        "RETHLAS_CLAUDE_ROOT_CODEX_BIN": str(Path(sys.executable).resolve()),
    }
    for key, value in bindings.items():
        monkeypatch.setenv(key, value)

    class FakeLegacy:
        VERIFY_PROOF_URL = "https://verifier.invalid/verify"

        @staticmethod
        def verify_blueprint_file(**arguments: object) -> dict[str, object]:
            callback = arguments["on_verifier_dispatch"]
            assert callable(callback)
            callback()
            return {"published": False, "verdict": "incorrect"}

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy)
    app = claude_core.build_mcp_app()
    tools = app._tool_manager._tools
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            tools["verify_blueprint_service"].fn,
            problem_id="example",
            statement_sha256=digest,
        ).result(timeout=5)
    assert result == {"published": False, "verdict": "incorrect"}


def test_root_mcp_retrieval_releases_lock_and_rechecks_source_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "12345678-1234-4123-8123-123456789abc"
    digest = _statement_digest()
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    manifest = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
    )
    bindings = {
        "RETHLAS_CLAUDE_ROOT_PROBLEM_ID": "example",
        "RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256": digest,
        "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        "RETHLAS_CLAUDE_ROOT_MODEL": "claude-opus-5",
        "RETHLAS_CLAUDE_ROOT_LAUNCH_MODEL": "claude-opus-5[1m]",
        "RETHLAS_CLAUDE_ROOT_PROVIDER": "vertex",
        "RETHLAS_CLAUDE_ROOT_PROVIDER_BINDING_SHA256": hashlib.sha256(
            b"claude-opus-5[1m]"
        ).hexdigest(),
        "RETHLAS_CLAUDE_ROOT_CLI_SHA256": "1" * 64,
        "RETHLAS_CLAUDE_ROOT_CLI_VERSION": "test-claude-2.1.246",
        "RETHLAS_CLAUDE_ROOT_CONTEXT_WINDOW": "1000000",
        "RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256": "3" * 64,
        "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": "4" * 64,
        "RETHLAS_CLAUDE_ROOT_CODEX_BIN": str(Path(sys.executable).resolve()),
    }
    for key, value in bindings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        claude_core,
        "statement_retrieval_policy",
        lambda **_kwargs: {"mode": "matlas_arxiv"},
    )
    entered = threading.Event()
    release = threading.Event()

    class FakeLegacy:
        @staticmethod
        def search_arxiv_theorems_for_problem(
            **_kwargs: object,
        ) -> dict[str, object]:
            entered.set()
            assert release.wait(timeout=10)
            return {"provider": "synthetic", "count": 1}

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy())
    app = claude_core.build_mcp_app()
    tool = app._tool_manager._tools["search_arxiv_theorems"].fn

    def fence_root() -> None:
        with claude_core._root_authority_lock("example") as problem_dir:
            manifest_path = (
                problem_dir / "roots" / session_id / "manifest.json"
            )
            claude_core._ensure_root_source_drift_fence_unlocked(
                problem_dir=problem_dir,
                problem_id="example",
                statement_sha256=digest,
                root_session_id=session_id,
                council_id="council_" + "d" * 32,
                root_manifest_sha256=claude_core.sha256_file(manifest_path),
                old_host_source_sha256=str(manifest["host_source_sha256"]),
                replacement_host_source_sha256="f" * 64,
                initial_pointer_state="active",
                initial_pointer_sha256="2" * 64,
                reason_sha256="3" * 64,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        retrieval = executor.submit(
            tool,
            problem_id="example",
            query="blocked metadata request",
            num_results=1,
        )
        assert entered.wait(timeout=5)
        fencing = executor.submit(fence_root)
        try:
            fencing.result(timeout=2)
        finally:
            release.set()
        with pytest.raises(claude_core.ClaudeCoreError, match="terminally fenced"):
            retrieval.result(timeout=5)


def test_council_root_mcp_exposes_the_bounded_two_seat_protocol_only_in_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "12345678-1234-4123-8123-123456789abc"
    digest = _statement_digest()
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
    )
    bindings = {
        "RETHLAS_CLAUDE_ROOT_PROBLEM_ID": "example",
        "RETHLAS_CLAUDE_ROOT_STATEMENT_SHA256": digest,
        "RETHLAS_CLAUDE_ROOT_SESSION_ID": session_id,
        "RETHLAS_CLAUDE_ROOT_MODEL": "claude-opus-5",
        "RETHLAS_CLAUDE_ROOT_LAUNCH_MODEL": "claude-opus-5[1m]",
        "RETHLAS_CLAUDE_ROOT_PROVIDER": "vertex",
        "RETHLAS_CLAUDE_ROOT_PROVIDER_BINDING_SHA256": hashlib.sha256(
            b"claude-opus-5[1m]"
        ).hexdigest(),
        "RETHLAS_CLAUDE_ROOT_CLI_SHA256": "1" * 64,
        "RETHLAS_CLAUDE_ROOT_CLI_VERSION": "test-claude-2.1.246",
        "RETHLAS_CLAUDE_ROOT_CONTEXT_WINDOW": "1000000",
        "RETHLAS_CLAUDE_ROOT_PYTHON_RUNTIME_SHA256": "3" * 64,
        "RETHLAS_CLAUDE_ROOT_LAUNCHER_SHA256": "4" * 64,
        "RETHLAS_CLAUDE_ROOT_ORCHESTRATION_MODE": (
            claude_core.OPUS_SOL_COUNCIL_MODE
        ),
        "RETHLAS_CLAUDE_ROOT_CODEX_BIN": str(Path(sys.executable).resolve()),
    }
    for key, value in bindings.items():
        monkeypatch.setenv(key, value)

    memory_calls: list[dict[str, object]] = []
    checkpoint_sha256 = "6" * 64

    class FakeLegacy:
        @staticmethod
        def memory_append_batch(**kwargs: object) -> dict[str, object]:
            memory_calls.append(kwargs)
            items = kwargs.get("items")
            assert isinstance(items, list)
            return {
                "status": "ok",
                "count": len(items),
                "checkpoint_sha256": checkpoint_sha256,
            }

        @staticmethod
        def _exact_checkpoint_tool_result(value: object) -> object:
            return value

    monkeypatch.setattr(claude_core, "_legacy", lambda: FakeLegacy())
    app = claude_core.build_mcp_app()
    manager = getattr(app, "_tool_manager", None)
    tools = getattr(manager, "_tools", {}) if manager is not None else {}
    assert {
        "route_council_status",
        "start_route_council",
        "revise_route_council",
        "finalize_route_council",
        "override_route_council",
    } <= set(tools)
    cohort_annotations = tools["run_three_route_cohort"].fn.__annotations__
    assert "plans" not in cohort_annotations
    assert {
        "problem_id",
        "statement_sha256",
        "root_session_id",
        "council_id",
        "council_receipt_sha256",
        "timeout_seconds",
        "return",
    } == set(cohort_annotations)
    cohort_preflight = tools["run_three_route_cohort"].fn(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        council_id="not-a-council-id",
        council_receipt_sha256="0" * 64,
        timeout_seconds=60,
    )
    assert cohort_preflight["status"] == "preflight_failed"
    assert cohort_preflight["category"] == "cohort_contract"
    assert cohort_preflight["error"] == (
        "council roots require an accepted route-council receipt"
    )
    assert cohort_preflight["intent_committed"] is False
    assert cohort_preflight["cohort_id"] is None
    synthetic_cohort_id = "cohort_" + "a" * 32
    synthetic_plan_sha256 = "b" * 64
    synthetic_evidence = _completion_evidence()
    synthetic_handoff = {
        "schema_version": claude_core.COHORT_COMPLETION_HANDOFF_SCHEMA,
        "status": "available",
        "route_reports": [],
        "synthesis": {"next_state": "stop_unsolved"},
    }
    monkeypatch.setattr(
        claude_core,
        "run_three_route_cohort",
        lambda **_arguments: {
            "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
            "status": "completed_unverified",
            "cohort_id": synthetic_cohort_id,
            "plan_sha256": synthetic_plan_sha256,
            "completion_evidence": synthetic_evidence,
        },
    )
    monkeypatch.setattr(
        claude_core,
        "_read_cohort_token_telemetry_if_present",
        lambda **_arguments: {"coverage": "aggregate_only"},
    )
    monkeypatch.setattr(
        claude_core,
        "_read_durable_cohort_plan",
        lambda **_arguments: {"plans": _plans()},
    )
    monkeypatch.setattr(
        claude_core,
        "_cohort_completion_handoff",
        lambda **_arguments: synthetic_handoff,
    )
    settled = tools["run_three_route_cohort"].fn(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        council_id="council_" + "a" * 32,
        council_receipt_sha256="c" * 64,
        timeout_seconds=60,
    )
    assert settled["completion_handoff"] is synthetic_handoff
    assert settled["token_telemetry"] == {"coverage": "aggregate_only"}
    assert settled["publication"] is None
    revision_plan_type = get_args(
        tools["revise_route_council"].fn.__annotations__["merged_plans"]
    )[0]
    final_plan_type = get_args(
        tools["finalize_route_council"].fn.__annotations__["final_plans"]
    )[0]
    adjudication_type = get_args(
        tools["finalize_route_council"].fn.__annotations__["adjudications"]
    )[0]
    oversized_plans = _plans()
    oversized_plans[0] = {
        **oversized_plans[0],
        "discriminating_test": "x" * 4097,
    }
    # The MCP transport accepts a bounded oversize so the host can return its
    # structured, non-paid semantic preflight instead of an opaque Pydantic
    # tool-call failure.
    oversized_preflight = tools["finalize_route_council"].fn(
        problem_id="example",
        statement_sha256=digest,
        council_id="council_" + "0" * 32,
        final_plans=[final_plan_type(**plan) for plan in oversized_plans],
        adjudications=[],
        root_session_id=session_id,
        timeout_seconds=60,
    )
    assert oversized_preflight["status"] == "preflight_failed"
    assert oversized_preflight["paid_sol_dispatched"] is False
    assert "actual=4097, remove_at_least=1" in oversized_preflight["error"]
    assert "Shorten only the named field" in oversized_preflight["repair_hint"]
    assert "preserve every unrelated" in oversized_preflight["repair_hint"]
    oversized_adjudication = adjudication_type(
        draft_plan_id="route_1",
        final_plan_id="route_1",
        decision="accepted",
        rationale="x" * 4097,
    )
    assert len(oversized_adjudication.rationale) == 4097

    def reject_start_without_failure_context(
        **_arguments: object,
    ) -> dict[str, object]:
        raise claude_core.CouncilContractError(
            "a superseding root requires prior council failure context"
        )

    monkeypatch.setattr(
        claude_core,
        "start_route_council",
        reject_start_without_failure_context,
    )
    start_plan_type = get_args(
        tools["start_route_council"].fn.__annotations__["opus_plans"]
    )[0]
    start_preflight = tools["start_route_council"].fn(
        problem_id="example",
        statement_sha256=digest,
        opus_plans=[start_plan_type(**plan) for plan in _plans()],
        root_session_id=session_id,
        timeout_seconds=60,
    )
    assert start_preflight["status"] == "preflight_failed"
    assert start_preflight["category"] == "route_council_contract"
    assert start_preflight["operation"] == "start_route_council"
    assert "prior council failure context" in start_preflight["error"]
    assert "prior_failure_context" in start_preflight["repair_hint"]

    def reject_same_council_reentry(
        **_arguments: object,
    ) -> dict[str, object]:
        raise claude_core._blocked_council_contract_error(
            operation="start_route_council",
            state="operational_blocked",
        )

    monkeypatch.setattr(
        claude_core,
        "start_route_council",
        reject_same_council_reentry,
    )
    blocked_start_preflight = tools["start_route_council"].fn(
        problem_id="example",
        statement_sha256=digest,
        opus_plans=[start_plan_type(**plan) for plan in _plans()],
        root_session_id=session_id,
        prior_failure_context="Synthetic failed phase.",
        timeout_seconds=60,
    )
    assert blocked_start_preflight["status"] == "preflight_failed"
    assert blocked_start_preflight["paid_sol_dispatched"] is False
    assert blocked_start_preflight["retry_allowed"] is False
    assert "same council" in blocked_start_preflight["repair_hint"]
    assert "fresh successor root" in blocked_start_preflight["repair_hint"]

    def reject_out_of_sequence_revision(
        **_arguments: object,
    ) -> dict[str, object]:
        raise claude_core.CouncilContractError(
            "route-council revision is out of sequence; the blind Astra phase "
            "must complete before a merged slate can be reviewed"
        )

    monkeypatch.setattr(
        claude_core,
        "revise_route_council",
        reject_out_of_sequence_revision,
    )
    revision_preflight = tools["revise_route_council"].fn(
        problem_id="example",
        statement_sha256=digest,
        council_id="council_" + "0" * 32,
        merged_plans=[revision_plan_type(**plan) for plan in _plans()],
        merge_rationale="Synthetic out-of-sequence merge.",
        root_session_id=session_id,
        timeout_seconds=60,
    )
    assert revision_preflight["status"] == "preflight_failed"
    assert revision_preflight["category"] == "route_council_contract"
    assert revision_preflight["operation"] == "revise_route_council"
    assert "blind Astra phase must complete" in revision_preflight["error"]
    item_type = get_args(tools["memory_append_batch"].fn.__annotations__["items"])[0]
    item = item_type(
        channel="events",
        record={"event": "synthetic checkpoint"},
        active=True,
        supersedes=[],
    )
    blocked = tools["memory_append_batch"].fn(
        problem_id="example", items=[item]
    )
    assert blocked["status"] == "preflight_failed"
    assert blocked["category"] == "route_council_checkpoint"
    assert blocked["council_state"] == "none"
    assert blocked["memory_written"] is False
    assert memory_calls == []

    council_id = "council_" + "5" * 32
    claude_core._write_once(
        claude_core._council_pointer_path("example", session_id),
        {
            "schema_version": claude_core.COUNCIL_POINTER_SCHEMA,
            "pointer_version": 1,
            "problem_id": "example",
            "statement_sha256": digest,
            "root_session_id": session_id,
            "council_round": 1,
            "council_id": council_id,
            "base_frontier_sha256": "1" * 64,
            "opus_plan_sha256": "2" * 64,
            "prior_context_sha256": "3" * 64,
            "prior_failure_receipt_sha256": None,
            "host_source_sha256": claude_core._host_source_sha256(),
            "predecessor_root_session_id": None,
            "predecessor_council_id": None,
            "predecessor_pointer_sha256": None,
            "state": "accepted",
            "final_plan_sha256": "4" * 64,
            "acceptance_sha256": "5" * 64,
            "checkpoint_sha256": None,
            "cohort_id": None,
            "updated_at_unix": time.time(),
        },
        mode=0o400,
    )
    with pytest.raises(
        claude_core.ClaudeCoreError,
        match="accepted route-council receipt",
    ):
        tools["memory_append_batch"].fn(problem_id="example", items=[item])
    assert memory_calls == []

    final_plan_set = claude_core.validate_plan_set(
        problem_id="example",
        statement_sha256=digest,
        plans=_plans(),
        root_session_id=session_id,
    )
    final_plan_sha256 = claude_core._plan_set_sha256(final_plan_set)
    state_dir = claude_core._council_dir("example", council_id)
    claude_core._write_once(
        state_dir / "final_plan.json", final_plan_set, mode=0o400
    )
    pointer_path = claude_core._council_pointer_path("example", session_id)
    pointer = claude_core._read_canonical_object(
        pointer_path, label="synthetic pointer"
    )
    pointer["final_plan_sha256"] = final_plan_sha256
    claude_core._replace_canonical(pointer_path, pointer)
    monkeypatch.setattr(
        claude_core,
        "_validate_council_acceptance",
        lambda **_arguments: {"status": "accepted", "council_round": 1},
    )
    monkeypatch.setattr(
        claude_core,
        "_load_accepted_council_plan_set",
        lambda **_arguments: (
            final_plan_set,
            {
                "status": "accepted",
                "council_round": 1,
                "final_plan_sha256": final_plan_sha256,
            },
        ),
    )
    monkeypatch.setattr(
        claude_core,
        "_external_plan_checkpoint_evidence",
        lambda **_arguments: {
            "batch_id": "batch_synthetic",
            "record_ids": [],
            "checkpoint_sha256": checkpoint_sha256,
            "commit_sha256": "7" * 64,
        },
    )
    wrong = tools["memory_append_batch"].fn(
        problem_id="example", items=[item]
    )
    assert wrong["status"] == "preflight_failed"
    assert "exactly the three accepted" in wrong["error"]
    assert "items=[]" in wrong["repair_hint"]
    assert memory_calls == []

    expected_items = claude_core._council_final_plan_checkpoint_items(
        final_plan_set
    )
    committed = tools["memory_append_batch"].fn(
        problem_id="example", items=[]
    )
    assert committed == {
        "status": "ok",
        "count": 3,
        "checkpoint_sha256": checkpoint_sha256,
    }
    assert memory_calls == [
        {"problem_id": "example", "items": expected_items}
    ]
    checkpointed = claude_core._read_council_pointer(
        pointer_path,
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
    )
    assert checkpointed["state"] == "checkpointed"
    assert checkpointed["checkpoint_sha256"] == checkpoint_sha256

    cohort_id = "cohort_" + claude_core._cohort_identity_sha256(
        final_plan_set,
        council_id=council_id,
        acceptance_sha256="5" * 64,
    )[:32]
    with claude_core.root_authority_guard(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
    ):
        claude_core._update_council_pointer(
            checkpointed,
            state="consumed",
            final_plan_sha256=final_plan_sha256,
            acceptance_sha256="5" * 64,
            checkpoint_sha256=checkpoint_sha256,
            cohort_id=cohort_id,
        )
    cohort_dir = tmp_path / "state" / "example" / cohort_id
    cohort_dir.mkdir(parents=True, mode=0o700)
    active_lock = open(cohort_dir / "cohort.lock", "a+b")
    fcntl.flock(active_lock, fcntl.LOCK_EX)
    try:
        blocked_consumed = tools["memory_append_batch"].fn(
            problem_id="example", items=[item]
        )
    finally:
        fcntl.flock(active_lock, fcntl.LOCK_UN)
        active_lock.close()
    assert blocked_consumed["status"] == "preflight_failed"
    assert blocked_consumed["council_state"] == "consumed"
    assert "must settle" in blocked_consumed["error"]
    assert len(memory_calls) == 1

    log_path = cohort_dir / "executor.log"
    log_path.write_text("recoverable synthetic cohort failure\n", encoding="utf-8")
    claude_core._write_once(
        cohort_dir / f"plan_{final_plan_sha256}.json",
        final_plan_set,
        mode=0o400,
    )
    claude_core._write_once(
        cohort_dir / "receipt.json",
        {
            "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
            "status": "failed",
            "cohort_id": cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": final_plan_sha256,
            "root_session_id": session_id,
            "returncode": 70,
            "timed_out": False,
            "elapsed_seconds": 1.0,
            "frontier_before_sha256": "8" * 64,
            "frontier_after_sha256": "8" * 64,
            "frontier_changed": False,
            "log_path": str(log_path),
            "log_bytes": log_path.stat().st_size,
            "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
            "log_over_cap": False,
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            "retry_allowed": False,
            "completion_evidence": None,
        },
        mode=0o400,
    )
    blocked_failed = tools["memory_append_batch"].fn(
        problem_id="example", items=[item]
    )
    assert blocked_failed["status"] == "preflight_failed"
    assert "owner-authorized recovery" in blocked_failed["error"]
    assert len(memory_calls) == 1

    monkeypatch.setattr(
        claude_core, "_require_codex_login", lambda codex_bin: Path(codex_bin)
    )
    authorization = claude_core.authorize_failed_cohort_recovery(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=session_id,
        plan_sha256=final_plan_sha256,
        codex_bin=Path(sys.executable),
        source_cohort_id=cohort_id,
    )
    blocked_pending_recovery = tools["memory_append_batch"].fn(
        problem_id="example", items=[item]
    )
    assert blocked_pending_recovery["status"] == "preflight_failed"
    assert "must settle" in blocked_pending_recovery["error"]
    assert len(memory_calls) == 1

    recovery_cohort_id = str(authorization["recovery_cohort_id"])
    recovery_dir = tmp_path / "state" / "example" / recovery_cohort_id
    recovery_dir.mkdir(parents=True, mode=0o700)
    recovery_log = recovery_dir / "executor.log"
    recovery_log.write_text("terminal synthetic cohort\n", encoding="utf-8")
    claude_core._write_once(
        recovery_dir / "receipt.json",
        {
            "schema_version": claude_core.COHORT_RECEIPT_SCHEMA,
            "status": "completed_unverified",
            "cohort_id": recovery_cohort_id,
            "problem_id": "example",
            "statement_sha256": digest,
            "plan_sha256": final_plan_sha256,
            "root_session_id": session_id,
            "returncode": 1,
            "timed_out": False,
            "elapsed_seconds": 1.0,
            "frontier_before_sha256": "8" * 64,
            "frontier_after_sha256": "9" * 64,
            "frontier_changed": True,
            "log_path": str(recovery_log),
            "log_bytes": recovery_log.stat().st_size,
            "log_sha256": hashlib.sha256(recovery_log.read_bytes()).hexdigest(),
            "log_over_cap": False,
            "max_report_log_bytes": claude_core.MAX_REPORT_LOG_BYTES,
            "retry_allowed": False,
            "completion_evidence": _completion_evidence(),
        },
        mode=0o400,
    )
    after_settlement = tools["memory_append_batch"].fn(
        problem_id="example", items=[item]
    )
    assert after_settlement["status"] == "ok"
    assert len(memory_calls) == 2

    successor_session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    successor = _prepare_root(
        problem_id="example",
        statement_sha256=digest,
        root_session_id=successor_session_id,
        canonical_model="claude-opus-5",
        orchestration_mode=claude_core.OPUS_SOL_COUNCIL_MODE,
        takeover_from=session_id,
    )
    assert successor["previous_root_session_id"] == session_id
    monkeypatch.setenv(
        "RETHLAS_CLAUDE_ROOT_SESSION_ID", successor_session_id
    )
    successor_app = claude_core.build_mcp_app()
    successor_tools = successor_app._tool_manager._tools
    successor_item_type = get_args(
        successor_tools["memory_append_batch"].fn.__annotations__["items"]
    )[0]
    inherited_append = successor_tools["memory_append_batch"].fn(
        problem_id="example",
        items=[
            successor_item_type(
                channel="events",
                record={"event": "checkpoint after root takeover"},
                active=True,
                supersedes=[],
            )
        ],
    )
    assert inherited_append["status"] == "ok"
    assert len(memory_calls) == 3
    assert not claude_core._council_pointer_path(
        "example", successor_session_id
    ).exists()
    claude_core._assert_council_ready_for_verification(
        problem_id="example", statement_sha256=digest
    )
