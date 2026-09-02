from __future__ import annotations

import hashlib
import json
import threading
import sys
from copy import deepcopy
from pathlib import Path
import fcntl
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.generation.mcp import verification_client as client  # noqa: E402
from agents.generation.mcp import server as generation_server  # noqa: E402
from agents.generation.mcp import proof_context as mutable_proof_context  # noqa: E402

TARGETED_DEADLINE = "2099-01-01T00:00:00+00:00"

class FakeResponse:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


@pytest.mark.parametrize(
    ("logical_flags", "darwin_flags"),
    [
        (client._RENAME_NOREPLACE, 0x00000004),
        (client._RENAME_EXCHANGE, 0x00000002),
    ],
)
def test_darwin_atomic_rename_maps_native_flags(
    logical_flags: int,
    darwin_flags: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeRename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            calls.append(arguments)
            return 0

    class FakeLibc:
        renameatx_np = FakeRename()

    monkeypatch.setattr(client.sys, "platform", "darwin")
    monkeypatch.setattr(client.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())

    client._renameat2_at(17, "from", "to", logical_flags)

    assert len(calls) == 1
    assert calls[0] == (17, b"from", 17, b"to", darwin_flags)


def test_atomic_rename_rejects_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client.sys, "platform", "win32")
    with pytest.raises(OSError, match="atomic relative rename is unavailable"):
        client._renameat2_at(17, "from", "to", client._RENAME_NOREPLACE)


def test_publication_lock_wait_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "publication.lock"
    path.touch(mode=0o600)
    monkeypatch.setenv("RETHLAS_PUBLICATION_LOCK_TIMEOUT_SECONDS", "0.01")
    with path.open("r+", encoding="utf-8") as owner, path.open(
        "r+", encoding="utf-8"
    ) as waiter:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(TimeoutError, match="publication lock"):
                client._acquire_publication_lock(waiter, display_path=path)
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)


def test_nested_direct_journal_parent_chain_is_fsynced_before_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    journal = receipt_root / "nested" / "intent.json"
    created_under: list[tuple[int, int]] = []
    fsynced: list[tuple[int, int]] = []
    real_mkdir = client.os.mkdir
    real_fsync = client.os.fsync
    real_replace = client._atomic_replace_at

    def observed_mkdir(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        assert dir_fd is not None
        parent = client.os.fstat(dir_fd)
        real_mkdir(path, mode, dir_fd=dir_fd)
        created_under.append((parent.st_dev, parent.st_ino))

    def observed_fsync(descriptor: int) -> None:
        metadata = client.os.fstat(descriptor)
        fsynced.append((metadata.st_dev, metadata.st_ino))
        real_fsync(descriptor)

    def assert_durable_parents_before_replace(
        directory_fd: int, filename: str, content: bytes
    ) -> tuple[int, int]:
        assert created_under
        assert all(parent in fsynced for parent in created_under)
        return real_replace(directory_fd, filename, content)

    monkeypatch.setattr(client.os, "mkdir", observed_mkdir)
    monkeypatch.setattr(client.os, "fsync", observed_fsync)
    monkeypatch.setattr(
        client, "_atomic_replace_at", assert_durable_parents_before_replace
    )
    written = client._write_direct_finalization_record(
        journal,
        {"schema_version": "test", "status": "prepared"},
        maximum_bytes=1_024,
    )

    assert written == {"schema_version": "test", "status": "prepared"}
    assert len(created_under) == 1
    tmp_metadata = tmp_path.stat()
    assert (tmp_metadata.st_dev, tmp_metadata.st_ino) in fsynced


def valid_payload(
    proof: str,
    *,
    statement: str = "S",
    verdict: str = "correct",
) -> dict[str, Any]:
    wrong = verdict == "wrong"
    item_ids, context_digest = client.expected_attestation(
        proof=proof,
        statement=statement,
    )
    manifest = client.parse_blueprint(proof, target_statement=statement)
    attestations = []
    for index, item_id in enumerate(item_ids):
        context = client.build_item_context(
            manifest,
            item_id,
            max_chars=client.VERIFY_CONTEXT_MAX_CHARS,
        )
        attestations.append(
            {
                "item_id": item_id,
                "disposition": (
                    "verified" if not wrong or index == 0 else "blocked"
                ),
                "final_round": 0,
                "expanded_proof_ids": [],
                "max_chars": client.VERIFY_CONTEXT_MAX_CHARS,
                "context_digest": context["digest"],
                "verdict": verdict,
            }
        )
    payload = {
        "output_schema_version": 2,
        "verification_report": {
            "summary": "checked",
            "critical_errors": [],
            "gaps": (
                [{"location": item_ids[0], "issue": "missing justification"}]
                if wrong
                else []
            ),
        },
        "verification_status": "final",
        "verdict": verdict,
        "repair_hints": "add the missing justification" if wrong else "",
        "needs_expanded_proofs": [],
        "checked_item_ids": item_ids,
        "proof_digest": client.proof_digest(proof),
        "context_digest": context_digest,
        "item_context_attestations": attestations,
    }
    payload["adaptive_context_digest"] = client.aggregate_adaptive_context_digest(
        manifest, attestations
    )
    return payload


def targeted_payload(proof: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = client.parse_blueprint(proof)
    item = manifest.items[0]
    claim = {
        "blueprint_item_label": item.label,
        "claim_sha256": item.digest,
        "reason": "The critic selected this exact bridge.",
    }
    ticket_seed = {
        "review_id": "review_" + "1" * 32,
        "snapshot_sha256": "2" * 64,
        "route_id": "route-a",
        "blueprint_sha256": client.proof_digest(proof),
        "blueprint_item_id": item.item_id,
        "claim": claim,
    }
    ticket = {
        "schema_version": "rethlas_targeted_claim_ticket_v2",
        "ticket_id": "claim_"
        + hashlib.sha256(
            json.dumps(
                ticket_seed,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:32],
        **ticket_seed,
        "verification_mode": "targeted_nonpublishing",
        "publication_authority": False,
        "whole_blueprint_verdict_authority": False,
    }
    context = client.build_item_context(
        manifest, item.item_id, max_chars=client.VERIFY_CONTEXT_MAX_CHARS
    )
    receipt_seed = {
        "schema_version": client.TARGETED_RECEIPT_SCHEMA,
        "ticket_id": ticket["ticket_id"],
        "review_id": ticket["review_id"],
        "snapshot_sha256": ticket["snapshot_sha256"],
        "route_id": ticket["route_id"],
        "blueprint_sha256": ticket["blueprint_sha256"],
        "blueprint_item_id": item.item_id,
        "blueprint_item_label": item.label,
        "claim_sha256": item.digest,
        "verification_deadline_utc": TARGETED_DEADLINE,
        "verification_status": "final",
        "verdict": "correct",
        "verification_report": {
            "summary": "checked",
            "critical_errors": [],
            "gaps": [],
        },
        "repair_hints": "",
        "checked_item_ids": [item.item_id],
        "context_attestation": {
            "item_id": item.item_id,
            "disposition": "verified",
            "final_round": 0,
            "expanded_proof_ids": [],
            "max_chars": client.VERIFY_CONTEXT_MAX_CHARS,
            "context_digest": context["digest"],
            "verdict": "correct",
        },
        "verification_limits": {
            "context_max_chars": client.VERIFY_CONTEXT_MAX_CHARS,
            "max_expansion_rounds": client.MAX_EXPANSION_ROUNDS,
            "max_expanded_proofs": client.MAX_EXPANDED_PROOFS,
            "max_expanded_proof_chars": client.MAX_EXPANDED_PROOF_CHARS,
        },
        "proof_context": client._current_proof_context_binding(),
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
    receipt = {
        **receipt_seed,
        "receipt_sha256": hashlib.sha256(
            json.dumps(
                receipt_seed,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    return ticket, receipt


def targeted_attempt_id(proof: str, ticket: dict[str, Any]) -> str:
    return client._targeted_verification_journal_identity(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
    )[2]


def targeted_status_terminal(
    proof: str,
    ticket: dict[str, Any],
    *,
    status_code: int,
    state: str,
    detail: object,
) -> dict[str, Any]:
    identity, _journal_key, attempt_id = client._targeted_verification_journal_identity(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
    )

    def canonical(value: object) -> bytes:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    service_identity = {
        "schema_version": "rethlas_targeted_verification_attempt_identity_v1",
        "statement_sha256": identity["statement_sha256"],
        "proof_sha256": identity["proof_sha256"],
        "ticket_sha256": identity["ticket_sha256"],
        "verification_deadline_utc": identity["verification_deadline_utc"],
    }
    failure_sha256 = hashlib.sha256(
        canonical({"status_code": status_code, "detail": detail})
    ).hexdigest()
    seed = {
        "schema_version": "rethlas_targeted_verification_status_terminal_v2",
        "targeted_attempt_id": attempt_id,
        "state": state,
        "status_code": status_code,
        "detail": detail,
        "attempt_identity_sha256": hashlib.sha256(
            canonical(service_identity)
        ).hexdigest(),
        "intent_sha256": "8" * 64,
        "failure_sha256": failure_sha256,
        "model_dispatched": state != "predispatch_failed",
    }
    return {
        "detail": {
            **seed,
            "terminal_sha256": hashlib.sha256(canonical(seed)).hexdigest(),
        }
    }


def targeted_status_pending(
    proof: str,
    ticket: dict[str, Any],
    *,
    attempt_state: str = "running",
) -> dict[str, Any]:
    identity, _journal_key, attempt_id = client._targeted_verification_journal_identity(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
    )

    def canonical(value: object) -> bytes:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    service_identity = {
        "schema_version": "rethlas_targeted_verification_attempt_identity_v1",
        "statement_sha256": identity["statement_sha256"],
        "proof_sha256": identity["proof_sha256"],
        "ticket_sha256": identity["ticket_sha256"],
        "verification_deadline_utc": identity["verification_deadline_utc"],
    }
    seed = {
        "schema_version": "rethlas_targeted_verification_status_pending_v2",
        "targeted_attempt_id": attempt_id,
        "state": "recover_via_post",
        "attempt_state": attempt_state,
        "attempt_identity_sha256": hashlib.sha256(
            canonical(service_identity)
        ).hexdigest(),
        "intent_sha256": "8" * 64,
        "proof_context": client._current_proof_context_binding(),
    }
    return {
        "detail": {
            **seed,
            "pending_sha256": hashlib.sha256(canonical(seed)).hexdigest(),
        }
    }


def install_post(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str, dict[str, Any]], object],
) -> None:
    def fake_post(
        endpoint: str,
        *,
        json: dict[str, Any],
        timeout: float,
        **kwargs: Any,
    ) -> FakeResponse:
        assert 0 < timeout <= 3600
        payload = factory(endpoint, json)
        if (
            isinstance(payload, dict)
            and payload.get("output_schema_version") == 2
            and "verification_attempt_id" in json
        ):
            payload = dict(payload)
            attempt_id = json["verification_attempt_id"]
            payload.update(
                {
                    "verification_attempt_id": attempt_id,
                    "verifier_run_id": "testrun:" + attempt_id,
                    "verifier_model": "gpt-5.6-sol",
                    "verifier_reasoning_effort": "max",
                    "verifier_service_version": "test-0.3.0",
                    "verification_pass_index": json["verification_pass_index"],
                    "verification_role": (
                        "primary"
                        if json["verification_pass_index"] == 1
                        else "adversarial_full_claim_audit"
                    ),
                }
            )
        return FakeResponse(payload)

    monkeypatch.setattr(client.requests, "post", fake_post)

    def fake_get(
        endpoint: str,
        *,
        timeout: float,
        **kwargs: Any,
    ) -> FakeResponse:
        assert endpoint.endswith("/profile")
        assert 0 < timeout <= 5
        return FakeResponse(
            {
                "schema_version": "rethlas_verifier_profile_v1",
                "service_version": "test-0.3.0",
                "profile": "compatible",
                "passes": [
                    {
                        "pass_index": index,
                        "adapter": "codex_cli",
                        "provider": "openai",
                        "model": "gpt-5.6-sol",
                        "launch_model": "gpt-5.6-sol",
                        "reasoning_effort": "max",
                        "session_mode": "cold",
                    }
                    for index in (1, 2)
                ],
                "automatic_tiebreaker": False,
                "fallback_policy": "forbid",
            }
        )

    monkeypatch.setattr(client.requests, "get", fake_get)


def rewrite_direct_journal_as_legacy_v1(
    *, receipt_path: Path, journal_parent: Path, proof: str, statement: str
) -> tuple[Path, Path, Path]:
    intent_candidates = list(
        journal_parent.glob(".rethlas-verification-*.intent.json")
    )
    assert len(intent_candidates) == 1
    current_intent_path = intent_candidates[0]
    prefix = str(current_intent_path)[: -len(".intent.json")]
    current_dispatch_path = Path(prefix + ".dispatch.json")
    current_result_path = Path(prefix + ".result.json")
    legacy_paths = client._legacy_direct_finalization_paths(
        receipt_path, client.proof_digest(proof)
    )
    legacy_paths[0].parent.mkdir(parents=True, exist_ok=True)

    intent = json.loads(current_intent_path.read_text(encoding="utf-8"))
    checked_item_ids, _context_digest = client.expected_attestation(
        proof=proof, statement=statement
    )
    intent["schema_version"] = client._DIRECT_FINALIZATION_INTENT_SCHEMA_LEGACY
    intent["checked_item_ids"] = checked_item_ids
    for field in (
        "checked_item_count",
        "checked_item_ids_sha256",
        "client_source_sha256",
        "publication_generation_parent_sha256",
        "max_intent_bytes",
    ):
        intent.pop(field)
    current_intent_path.write_bytes(client._canonical_json_line_bytes(intent))

    dispatch = json.loads(current_dispatch_path.read_text(encoding="utf-8"))
    dispatch["intent_sha256"] = hashlib.sha256(
        client._canonical_json_line_bytes(intent)
    ).hexdigest()
    current_dispatch_path.write_bytes(
        client._canonical_json_line_bytes(dispatch)
    )

    if current_result_path.exists():
        result = json.loads(current_result_path.read_text(encoding="utf-8"))
        assert result["result_encoding"] == "complete"
        result["schema_version"] = (
            client._DIRECT_FINALIZATION_RESULT_SCHEMA_LEGACY
        )
        result["intent_sha256"] = dispatch["intent_sha256"]
        result["dispatch_sha256"] = hashlib.sha256(
            client._canonical_json_line_bytes(dispatch)
        ).hexdigest()
        for field in (
            "result_encoding",
            "original_result_sha256",
            "original_result_bytes",
            "max_result_bytes",
            "max_repair_hint_bytes",
            "max_summary_bytes",
        ):
            result.pop(field)
        current_result_path.write_bytes(
            client._canonical_json_line_bytes(result)
        )

    for current, legacy in zip(
        (
            current_intent_path,
            current_dispatch_path,
            current_result_path,
        ),
        legacy_paths,
        strict=True,
    ):
        if current.exists():
            current.replace(legacy)
    return legacy_paths


def direct_journal_parent(
    *, receipt_path: Path, verified_path: Path, blueprint_root: Path | None = None
) -> Path:
    return client._direct_finalization_journal_parent(
        receipt_path=receipt_path,
        verified_path=verified_path,
        blueprint_root=blueprint_root,
    )


def test_targeted_client_accepts_only_digest_bound_nonpublishing_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# lemma lem:a\n\n## statement\nA\n\n## proof\nProof A.\n"
    ticket, receipt = targeted_payload(proof)
    install_post(monkeypatch, lambda endpoint, request: deepcopy(receipt))

    result = client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
    )
    assert result == receipt

    forged = deepcopy(receipt)
    forged["publication_authority"] = True
    install_post(monkeypatch, lambda endpoint, request: forged)
    with pytest.raises(ValueError, match="publication authority|content address"):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
        )


def test_targeted_client_replays_legacy_v1_execution_binding_receipt() -> None:
    proof = "# lemma lem:a\n\n## statement\nA\n\n## proof\nProof A.\n"
    ticket, receipt = targeted_payload(proof)
    legacy = deepcopy(receipt)
    execution_binding = legacy["execution_binding"]
    execution_binding["schema_version"] = "rethlas_targeted_execution_binding_v1"
    execution_binding["prompt_limits"].pop("adapter_timeout_seconds")
    execution_binding["prompt_limits"].pop("mcp_tool_timeout_seconds")
    seed = dict(legacy)
    seed.pop("receipt_sha256")
    legacy["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            seed,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    validated = client.validate_targeted_claim_receipt(
        legacy,
        ticket=ticket,
        statement="S",
        proof=proof,
        verification_deadline_utc=TARGETED_DEADLINE,
    )

    assert validated == legacy


def test_targeted_client_requires_status_authentication_for_post_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, _receipt = targeted_payload(proof)

    class UnknownResponse(FakeResponse):
        status_code = 502

        def __init__(self) -> None:
            super().__init__(
                {
                    "detail": {
                        "code": "verifier_execution_unknown",
                        "item_id": ticket["blueprint_item_id"],
                    }
                }
            )

        def raise_for_status(self) -> None:
            pytest.fail("recognized execution_unknown must not become generic HTTP error")

    monkeypatch.setattr(client.requests, "post", lambda *args, **kwargs: UnknownResponse())
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
        )


def test_targeted_journal_retries_after_crash_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    post_calls = 0

    def response(_endpoint: str, _request: dict[str, Any]) -> dict[str, Any]:
        nonlocal post_calls
        post_calls += 1
        return deepcopy(receipt)

    install_post(monkeypatch, response)
    real_write = client._write_once_canonical_record_at
    crashed = False

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_after_intent(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal crashed
        result = real_write(*args, **kwargs)
        if kwargs.get("label") == "targeted verification intent" and not crashed:
            crashed = True
            raise SimulatedPowerLoss
        return result

    monkeypatch.setattr(
        client, "_write_once_canonical_record_at", crash_after_intent
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
        )
    assert post_calls == 0

    monkeypatch.setattr(client, "_write_once_canonical_record_at", real_write)
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
    ) == receipt
    assert post_calls == 1


def test_targeted_journal_replays_result_after_caller_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    post_calls = 0
    dispatch_calls = 0

    def response(_endpoint: str, _request: dict[str, Any]) -> dict[str, Any]:
        nonlocal post_calls
        post_calls += 1
        return deepcopy(receipt)

    def dispatched() -> None:
        nonlocal dispatch_calls
        dispatch_calls += 1

    install_post(monkeypatch, response)
    real_commit = client._commit_targeted_verification_result

    class SimulatedPowerLoss(BaseException):
        pass

    def commit_then_crash(**kwargs: Any) -> dict[str, Any]:
        raise SimulatedPowerLoss

    monkeypatch.setattr(
        client, "_commit_targeted_verification_result", commit_then_crash
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=dispatched,
        )
    assert post_calls == 1
    assert dispatch_calls == 1

    monkeypatch.setattr(
        client, "_commit_targeted_verification_result", real_commit
    )
    status_calls = 0

    def completed_status(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        assert "/verify-targeted-claim/status/target_" in args[0]
        return FakeResponse(deepcopy(receipt))

    monkeypatch.setattr(client.requests, "get", completed_status)
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt
    assert post_calls == 1
    assert dispatch_calls == 1
    assert status_calls == 1


def test_targeted_journal_marker_crash_uses_status_then_one_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    post_calls = 0
    dispatch_calls = 0

    def response(_endpoint: str, _request: dict[str, Any]) -> dict[str, Any]:
        nonlocal post_calls
        post_calls += 1
        return deepcopy(receipt)

    install_post(monkeypatch, response)
    real_write = client._write_once_canonical_record_at
    crashed = False

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_after_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal crashed
        result = real_write(*args, **kwargs)
        if kwargs.get("label") == "targeted verification dispatch" and not crashed:
            crashed = True
            raise SimulatedPowerLoss
        return result

    monkeypatch.setattr(
        client, "_write_once_canonical_record_at", crash_after_dispatch
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
        )
    assert post_calls == 0

    class MissingStatus(FakeResponse):
        status_code = 404

    monkeypatch.setattr(client, "_write_once_canonical_record_at", real_write)
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: MissingStatus(
            {
                "detail": {
                    "code": "targeted_attempt_not_found",
                    "targeted_attempt_id": targeted_attempt_id(proof, ticket),
                }
            }
        ),
    )

    def dispatched() -> None:
        nonlocal dispatch_calls
        dispatch_calls += 1

    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        on_verifier_dispatch=dispatched,
    ) == receipt
    assert post_calls == 1
    assert dispatch_calls == 1


def test_targeted_journal_callback_failure_remains_retryable_before_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    post_calls = 0

    def response(_endpoint: str, _request: dict[str, Any]) -> dict[str, Any]:
        nonlocal post_calls
        post_calls += 1
        return deepcopy(receipt)

    install_post(monkeypatch, response)
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: (_ for _ in ()).throw(
                RuntimeError("local memory write failed")
            ),
        )
    assert post_calls == 0

    class MissingStatus(FakeResponse):
        status_code = 404

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: MissingStatus(
            {
                "detail": {
                    "code": "targeted_attempt_not_found",
                    "targeted_attempt_id": targeted_attempt_id(proof, ticket),
                }
            }
        ),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        on_verifier_dispatch=lambda: None,
    ) == receipt
    assert post_calls == 1


def test_targeted_status_busy_is_retryable_and_does_not_settle_local_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    post_calls = 0
    real_write = client._write_once_canonical_record_at

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_after_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = real_write(*args, **kwargs)
        if kwargs.get("label") == "targeted verification dispatch":
            raise SimulatedPowerLoss
        return result

    def unexpected_post(
        _endpoint: str, _request: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal post_calls
        post_calls += 1
        pytest.fail("status recovery must not issue a second POST")

    install_post(monkeypatch, unexpected_post)
    monkeypatch.setattr(
        client, "_write_once_canonical_record_at", crash_after_dispatch
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
        )
    monkeypatch.setattr(client, "_write_once_canonical_record_at", real_write)

    class BusyStatus(FakeResponse):
        status_code = 429

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: BusyStatus({"detail": "busy"}),
    )
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            recover_only=True,
        )
    assert not list((tmp_path / "targeted").glob("*.result.json"))
    assert post_calls == 0

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(deepcopy(receipt)),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt
    assert post_calls == 0


def test_targeted_post_transport_loss_recovers_remote_receipt_by_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    post_calls = 0

    def lost_response(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal post_calls
        post_calls += 1
        raise client.requests.ReadTimeout("response lost after remote completion")

    monkeypatch.setattr(client.requests, "post", lost_response)
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: None,
        )
    assert post_calls == 1

    status_calls = 0

    def completed_status(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        return FakeResponse(deepcopy(receipt))

    monkeypatch.setattr(client.requests, "get", completed_status)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("status recovery must not POST again"),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt
    assert status_calls == 1


def test_targeted_post_untrusted_422_recovers_completed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)

    class Proxy422(FakeResponse):
        status_code = 422

        def raise_for_status(self) -> None:
            raise client.requests.HTTPError("proxy 422", response=self)

    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: Proxy422({"detail": "synthetic proxy rejection"}),
    )
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: None,
        )
    assert not list((tmp_path / "targeted").glob("*.result.json"))

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(deepcopy(receipt)),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("status recovery must not POST"),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt


def test_targeted_journal_root_rotation_after_post_recovers_by_remote_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    journal = tmp_path / "targeted"
    rotated = tmp_path / "targeted.old"
    post_calls = 0

    def rotate_after_remote_result(
        _endpoint: str, _request: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal post_calls
        post_calls += 1
        journal.rename(rotated)
        journal.mkdir(mode=0o700)
        return deepcopy(receipt)

    install_post(monkeypatch, rotate_after_remote_result)
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=journal,
            on_verifier_dispatch=lambda: None,
        )

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(deepcopy(receipt)),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("rotated-journal recovery must not POST"),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=journal,
        recover_only=True,
    ) == receipt
    assert post_calls == 1


def test_targeted_rotated_journal_adopts_remote_parser_binding_after_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    journal = tmp_path / "targeted"
    rotated = tmp_path / "targeted.old"

    def rotate_after_remote_result(
        _endpoint: str, _request: dict[str, Any]
    ) -> dict[str, Any]:
        journal.rename(rotated)
        journal.mkdir(mode=0o700)
        return deepcopy(receipt)

    install_post(monkeypatch, rotate_after_remote_result)
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=journal,
            on_verifier_dispatch=lambda: None,
        )
    drifted_binding = {
        **client._current_proof_context_binding(),
        "source_sha256": "7" * 64,
        "proof_context_schema_version": client.PROOF_CONTEXT_SCHEMA_VERSION + 1,
    }
    monkeypatch.setattr(
        client, "_current_proof_context_binding", lambda: drifted_binding
    )
    monkeypatch.setattr(
        client,
        "_parse_targeted_manifest",
        lambda *args, **kwargs: pytest.fail("adopted receipt must not reparse"),
    )
    monkeypatch.setattr(
        client,
        "build_item_context",
        lambda *args, **kwargs: pytest.fail("adopted receipt must not rebuild"),
    )
    status_calls = 0

    def completed_status(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        return FakeResponse(deepcopy(receipt))

    monkeypatch.setattr(client.requests, "get", completed_status)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("rotated recovery must not POST"),
    )

    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=journal,
        recover_only=True,
    ) == receipt
    assert status_calls == 1

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("local result must replay without GET"),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=journal,
        recover_only=True,
    ) == receipt


def test_targeted_expired_after_local_dispatch_settles_without_post_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, _receipt = targeted_payload(proof)
    real_datetime = client.datetime

    class ExpiredClock:
        fromisoformat = staticmethod(real_datetime.fromisoformat)

        @staticmethod
        def now(tz: Any = None) -> Any:
            return real_datetime.fromisoformat("2100-01-01T00:00:00+00:00")

    def cross_deadline() -> None:
        monkeypatch.setattr(client, "datetime", ExpiredClock)

    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("expired attempt must not POST"),
    )
    with pytest.raises(client.TargetedVerificationOperationalBlocked):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=cross_deadline,
        )
    with pytest.raises(client.TargetedVerificationOperationalBlocked):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            recover_only=True,
        )


def test_targeted_authenticated_pending_status_posts_recovery_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    real_write = client._write_once_canonical_record_at

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_after_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = real_write(*args, **kwargs)
        if kwargs.get("label") == "targeted verification dispatch":
            raise SimulatedPowerLoss
        return result

    monkeypatch.setattr(
        client, "_write_once_canonical_record_at", crash_after_dispatch
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
        )
    monkeypatch.setattr(client, "_write_once_canonical_record_at", real_write)

    class PendingStatus(FakeResponse):
        status_code = 425

    status_calls = 0

    def pending_status(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        return PendingStatus(targeted_status_pending(proof, ticket))

    post_calls = 0

    def recovered(_endpoint: str, _request: dict[str, Any]) -> dict[str, Any]:
        nonlocal post_calls
        post_calls += 1
        return deepcopy(receipt)

    real_datetime = client.datetime

    class ExpiredClock:
        fromisoformat = staticmethod(real_datetime.fromisoformat)

        @staticmethod
        def now(tz: Any = None) -> Any:
            return real_datetime.fromisoformat("2100-01-01T00:00:00+00:00")

    install_post(monkeypatch, recovered)
    monkeypatch.setattr(client.requests, "get", pending_status)
    monkeypatch.setattr(client, "datetime", ExpiredClock)

    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt
    assert status_calls == 1
    assert post_calls == 1


@pytest.mark.parametrize("after_replace", [False, True])
def test_targeted_local_result_commit_fault_remains_status_recoverable(
    after_replace: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    install_post(monkeypatch, lambda endpoint, request: deepcopy(receipt))
    real_commit = client._commit_targeted_verification_result

    def fail_commit(**kwargs: Any) -> dict[str, Any]:
        if after_replace:
            real_commit(**kwargs)
        raise OSError("simulated local result durability fault")

    monkeypatch.setattr(client, "_commit_targeted_verification_result", fail_commit)
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: None,
        )

    monkeypatch.setattr(client, "_commit_targeted_verification_result", real_commit)
    status_calls = 0

    def completed_status(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        return FakeResponse(deepcopy(receipt))

    monkeypatch.setattr(client.requests, "get", completed_status)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("recovery must not POST again"),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt
    assert status_calls == (0 if after_replace else 1)


@pytest.mark.parametrize("durable_status", [422, 503])
def test_targeted_status_durable_failure_settles_operational_failure_once(
    durable_status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, _receipt = targeted_payload(proof)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            client.requests.ReadTimeout("lost response")
        ),
    )
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: None,
        )

    status_calls = 0

    class DurableFailure(FakeResponse):
        def raise_for_status(self) -> None:
            raise client.requests.HTTPError("durable failure", response=self)

    def failed_status(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        response = DurableFailure(
            targeted_status_terminal(
                proof,
                ticket,
                status_code=durable_status,
                state="operational_failed",
                detail={"code": "vertex_adc_unavailable"},
            )
        )
        response.status_code = durable_status
        return response

    monkeypatch.setattr(client.requests, "get", failed_status)
    with pytest.raises(client.TargetedVerificationOperationalBlocked):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            recover_only=True,
        )
    with pytest.raises(client.TargetedVerificationOperationalBlocked):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            recover_only=True,
        )
    assert status_calls == 1


def test_targeted_predispatch_422_settles_from_status_without_second_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, _receipt = targeted_payload(proof)
    post_calls = 0

    class AdmissionFailure(FakeResponse):
        status_code = 422

        def raise_for_status(self) -> None:
            raise client.requests.HTTPError("admission failed", response=self)

    def failed_post(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal post_calls
        post_calls += 1
        return AdmissionFailure({"detail": "invalid targeted claim context"})

    monkeypatch.setattr(client.requests, "post", failed_post)
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: None,
        )

    status_calls = 0

    class DurableAdmissionFailure(FakeResponse):
        status_code = 422

        def raise_for_status(self) -> None:
            raise client.requests.HTTPError("durable admission failure", response=self)

    def failed_status(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        return DurableAdmissionFailure(
            targeted_status_terminal(
                proof,
                ticket,
                status_code=422,
                state="predispatch_failed",
                detail="invalid targeted claim context",
            )
        )

    monkeypatch.setattr(client.requests, "get", failed_status)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("durable admission must prevent another POST"),
    )
    with pytest.raises(client.TargetedVerificationOperationalBlocked):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            recover_only=True,
        )
    with pytest.raises(client.TargetedVerificationOperationalBlocked):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            recover_only=True,
        )
    assert post_calls == 1
    assert status_calls == 1


def test_targeted_status_transient_html_503_retries_then_recovers_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            client.requests.ReadTimeout("lost response")
        ),
    )
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: None,
        )

    class Html503(FakeResponse):
        status_code = 503

        def json(self) -> object:
            raise ValueError("upstream returned HTML")

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: Html503(None),
    )
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            recover_only=True,
        )
    assert not list((tmp_path / "targeted").glob("*.result.json"))

    status_calls = 0

    def completed(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal status_calls
        status_calls += 1
        return FakeResponse(deepcopy(receipt))

    monkeypatch.setattr(client.requests, "get", completed)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("status recovery must not POST"),
    )
    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt
    assert status_calls == 1


def test_targeted_status_recovery_uses_persisted_parser_binding_after_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    install_post(monkeypatch, lambda endpoint, request: deepcopy(receipt))
    real_commit = client._commit_targeted_verification_result
    monkeypatch.setattr(
        client,
        "_commit_targeted_verification_result",
        lambda **kwargs: (_ for _ in ()).throw(OSError("local commit lost")),
    )
    with pytest.raises(client.TargetedVerificationLocalRetryable):
        client.verify_targeted_claim_service(
            statement="S",
            proof=proof,
            ticket=ticket,
            verification_deadline_utc=TARGETED_DEADLINE,
            endpoint="https://verifier/verify-targeted-claim",
            journal_root=tmp_path / "targeted",
            on_verifier_dispatch=lambda: None,
        )
    monkeypatch.setattr(client, "_commit_targeted_verification_result", real_commit)
    drifted_binding = {
        **client._current_proof_context_binding(),
        "source_sha256": "7" * 64,
        "proof_context_schema_version": (
            client.PROOF_CONTEXT_SCHEMA_VERSION + 1
        ),
    }
    monkeypatch.setattr(
        client, "_current_proof_context_binding", lambda: drifted_binding
    )
    monkeypatch.setattr(
        client,
        "_parse_targeted_manifest",
        lambda *args, **kwargs: pytest.fail("historical recovery must not reparse"),
    )
    monkeypatch.setattr(
        client,
        "build_item_context",
        lambda *args, **kwargs: pytest.fail("historical recovery must not rebuild"),
    )
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(deepcopy(receipt)),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("historical recovery must not POST"),
    )

    assert client.verify_targeted_claim_service(
        statement="S",
        proof=proof,
        ticket=ticket,
        verification_deadline_utc=TARGETED_DEADLINE,
        endpoint="https://verifier/verify-targeted-claim",
        journal_root=tmp_path / "targeted",
        recover_only=True,
    ) == receipt


def test_targeted_receipt_uses_persisted_limits_across_client_config_drift() -> None:
    proof = "# theorem thm:a\n\n## statement\nA.\n\n## proof\nProof.\n"
    ticket, receipt = targeted_payload(proof)
    manifest = client._parse_targeted_manifest("S", proof)
    item = manifest.items[0]
    service_max_chars = client.VERIFY_CONTEXT_MAX_CHARS + 1_000
    context = client.build_item_context(
        manifest, item.item_id, max_chars=service_max_chars
    )
    seed = dict(receipt)
    seed.pop("receipt_sha256")
    seed["context_attestation"] = {
        **seed["context_attestation"],
        "max_chars": service_max_chars,
        "context_digest": context["digest"],
    }
    seed["verification_limits"] = {
        "context_max_chars": service_max_chars,
        "max_expansion_rounds": client.MAX_EXPANSION_ROUNDS + 1,
        "max_expanded_proofs": client.MAX_EXPANDED_PROOFS + 1,
        "max_expanded_proof_chars": client.MAX_EXPANDED_PROOF_CHARS + 1,
    }
    drifted_receipt = {
        **seed,
        "receipt_sha256": hashlib.sha256(
            json.dumps(
                seed,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    assert client.validate_targeted_claim_receipt(
        drifted_receipt,
        ticket=ticket,
        statement="S",
        proof=proof,
        verification_deadline_utc=TARGETED_DEADLINE,
    ) == drifted_receipt


def test_correct_response_promotes_unchanged_draft_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert result["published_path"] == str(verified)
    assert verified.read_text(encoding="utf-8") == proof
    assert draft.read_text(encoding="utf-8") == proof
    assert "proof" not in result


def test_wrong_response_never_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"], verdict="wrong"),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert draft.exists()
    assert not verified.exists()


def test_external_dispatch_mathematical_rejection_blocks_exact_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof_a = "# theorem main\n\n## statement\nS\n\n## proof\nP1\n"
    proof_b = proof_a.replace("## proof\nP1", "## proof\nP2")
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    state_root = tmp_path / "publication-state"
    draft.write_text(proof_a, encoding="utf-8")
    posts: list[str] = []
    dispatches = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        posts.append(request["proof"])
        return valid_payload(
            request["proof"],
            verdict="wrong" if request["proof"] == proof_a else "correct",
        )

    def commit_external_dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    install_post(monkeypatch, verifier)
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
        on_verifier_dispatch=commit_external_dispatch,
    )

    assert first["published"] is False
    admissions = list(state_root.glob(".rethlas-publication-admission-*.json"))
    assert len(admissions) == 1
    admission = json.loads(admissions[0].read_text(encoding="utf-8"))
    assert admission["status"] == "settled"
    assert admission["phase"] == "settled"
    assert admission["settlement_reason"] == "direct_mathematical_rejection"
    with pytest.raises(ValueError, match="settled non-retryable"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
            on_verifier_dispatch=commit_external_dispatch,
        )

    admission = json.loads(admissions[0].read_text(encoding="utf-8"))
    assert admission["status"] == "settled"
    assert admission["phase"] == "settled"
    assert admission["settlement_reason"] == "direct_mathematical_rejection"
    admission_generations = list(
        state_root.glob(".rethlas-publication-admission-generation-*.json")
    )
    assert admission_generations == []

    draft.write_text(proof_b, encoding="utf-8")
    changed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
        on_verifier_dispatch=commit_external_dispatch,
    )

    assert changed["published"] is True
    assert verified.read_text(encoding="utf-8") == proof_b
    assert posts == [proof_a, proof_b, proof_b]
    assert dispatches == 2


def test_external_dispatch_operational_failure_allows_exact_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    state_root = tmp_path / "publication-state"
    draft.write_text(proof, encoding="utf-8")
    posts = 0
    dispatches = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        if posts == 1:
            return {}
        return valid_payload(request["proof"])

    def commit_external_dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    install_post(monkeypatch, verifier)
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
        on_verifier_dispatch=commit_external_dispatch,
    )

    assert first["publication_blocked_reason"] == "invalid_verifier_response"
    admission_path = next(
        state_root.glob(".rethlas-publication-admission-*.json")
    )
    admission = json.loads(admission_path.read_text(encoding="utf-8"))
    assert admission["settlement_reason"] == "direct_operational_nonpublication"

    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
        on_verifier_dispatch=commit_external_dispatch,
    )

    assert recovered["published"] is True
    assert verified.read_text(encoding="utf-8") == proof
    assert posts == 3
    assert dispatches == 2
    assert len(
        list(
            state_root.glob(
                ".rethlas-publication-admission-generation-*.json"
            )
        )
    ) == 1


def test_dispatched_admission_rejects_changed_verifier_effect_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    state_root = tmp_path / "publication-state"
    draft.write_text(proof, encoding="utf-8")
    service_version = "test-0.3.0"
    posts = 0

    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {
                "schema_version": "rethlas_verifier_profile_v1",
                "service_version": service_version,
                "profile": "compatible",
                "passes": [
                    {
                        "pass_index": index,
                        "adapter": "codex_cli",
                        "provider": "openai",
                        "model": "gpt-5.6-sol",
                        "launch_model": "gpt-5.6-sol",
                        "reasoning_effort": "max",
                        "session_mode": "cold",
                    }
                    for index in (1, 2)
                ],
                "automatic_tiebreaker": False,
                "fallback_policy": "forbid",
            }
        )

    def lost_post(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal posts
        posts += 1
        raise client.requests.ConnectionError("response lost after dispatch")

    monkeypatch.setattr(client.requests, "get", fake_get)
    monkeypatch.setattr(client.requests, "post", lost_post)
    with pytest.raises(client.requests.ConnectionError):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
            on_verifier_dispatch=lambda: None,
        )
    admission_path = next(
        state_root.glob(".rethlas-publication-admission-*.json")
    )
    admission = json.loads(admission_path.read_text(encoding="utf-8"))
    assert admission["phase"] == "dispatched"
    assert client._HEX_DIGEST_RE.fullmatch(
        admission["verifier_effect_identity_sha256"]
    )

    service_version = "test-0.4.0"
    monkeypatch.setattr(
        client,
        "_VERIFICATION_CALLER_INSTANCE_ID",
        "vcaller_" + "f" * 32,
    )
    with pytest.raises(
        client.VerificationExecutionUnknown,
        match="changed verifier identity",
    ):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
            on_verifier_dispatch=lambda: None,
        )
    assert posts == 1


@pytest.mark.parametrize(
    "recovery_race",
    [
        "none",
        "status_change",
        "target_change",
        "receipt_change",
        "archive_change",
        "rollback_change",
        "cas_change",
        "certificate_crash",
        "settlement_successor_crash",
        "generation_archive_crash",
    ],
)
def test_cross_layer_recovery_retires_only_old_publication_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_race: str,
) -> None:
    old_proof = "# theorem main\n\n## statement\nS\n\n## proof\nOld P.\n"
    replacement_proof = (
        "# theorem main\n\n## statement\nS\n\n## proof\nNew P.\n"
    )
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    state_root = tmp_path / "publication-state"
    draft.write_text(old_proof, encoding="utf-8")
    old_caller = "vcaller_" + "a" * 32
    new_caller = "vcaller_" + "b" * 32
    monkeypatch.setattr(client, "_VERIFICATION_CALLER_INSTANCE_ID", old_caller)

    outer_intent = {
        "schema_version": "test_outer_publication_intent_v1",
        "status": "submitted",
        "problem_id": "problem",
        "statement_sha256": client.proof_digest("S"),
        "blueprint_sha256": client.proof_digest(old_proof),
    }
    outer_intent_sha256 = hashlib.sha256(
        client._canonical_json_line_bytes(outer_intent)
    ).hexdigest()
    outer_dispatch = {
        "schema_version": "test_outer_publication_dispatch_v1",
        "status": "dispatched",
        "problem_id": "problem",
        "statement_sha256": outer_intent["statement_sha256"],
        "blueprint_sha256": outer_intent["blueprint_sha256"],
        "intent_sha256": outer_intent_sha256,
    }
    outer_settlement = {
        "schema_version": "test_outer_publication_settlement_v1",
        "status": "not_published",
        "problem_id": "problem",
        "statement_sha256": outer_intent["statement_sha256"],
        "blueprint_sha256": outer_intent["blueprint_sha256"],
        "intent_sha256": outer_intent_sha256,
        "publication_receipt_sha256": None,
    }
    profile = {
        "schema_version": "rethlas_verifier_profile_v1",
        "service_version": "0.5.2",
        "profile": "compatible",
        "passes": [
            {
                "pass_index": index,
                "adapter": "codex_cli",
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "launch_model": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "session_mode": "cold",
            }
            for index in (1, 2)
        ],
        "automatic_tiebreaker": False,
        "fallback_policy": "forbid",
    }
    posts: list[dict[str, Any]] = []
    status_requests: list[tuple[str, str]] = []

    def fake_post(
        endpoint: str,
        *,
        json: dict[str, Any],
        **_kwargs: Any,
    ) -> FakeResponse:
        assert endpoint == "https://verifier/verify"
        posts.append(dict(json))
        if len(posts) == 1:
            raise client.requests.ConnectionError(
                "transport lost after verifier accepted pass one"
            )
        payload = valid_payload(json["proof"])
        payload.update(
            {
                "verification_attempt_id": json["verification_attempt_id"],
                "verifier_run_id": "testrun:" + json["verification_attempt_id"],
                "verifier_model": "gpt-5.6-sol",
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "0.5.2",
                "verification_pass_index": json["verification_pass_index"],
                "verification_role": (
                    "primary"
                    if json["verification_pass_index"] == 1
                    else "adversarial_full_claim_audit"
                ),
            }
        )
        return FakeResponse(payload)

    class OperationalStatus(FakeResponse):
        status_code = 409

    def fake_get(
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> FakeResponse:
        if endpoint.endswith("/profile"):
            return FakeResponse(deepcopy(profile))
        assert "/verify/status/" in endpoint
        assert params is not None
        pass_identity = params["verification_pass_identity"]
        attempt_id = endpoint.rsplit("/", 1)[-1]
        status_requests.append((attempt_id, pass_identity))
        seed = {
            "schema_version": "rethlas_verifier_pass_status_snapshot_v1",
            "verification_attempt_id": attempt_id,
            "pass_identity_sha256": pass_identity,
            "state": "operational_failed",
            "intent_sha256": "c" * 64,
            "caller_instance_id": old_caller,
            "retry_ordinal": (
                1
                if recovery_race == "status_change"
                and len(status_requests) == 2
                else 0
            ),
            "current_item_id": None,
            "current_item_index": None,
            "failure_status_code": 503,
            "failure_sha256": "d" * 64,
            "aggregate_sha256": None,
            "resumable_by_this_service": True,
            "publication_aggregate_present": False,
        }
        snapshot = {
            **seed,
            "snapshot_sha256": hashlib.sha256(
                json.dumps(
                    seed,
                    allow_nan=False,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        return OperationalStatus({"detail": snapshot})

    monkeypatch.setattr(client.requests, "get", fake_get)
    monkeypatch.setattr(client.requests, "post", fake_post)
    with pytest.raises(client.requests.ConnectionError):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
            on_verifier_dispatch=lambda: None,
            publication_authority_intent_sha256=outer_intent_sha256,
        )

    admission_path = next(
        state_root.glob(".rethlas-publication-admission-*.json")
    )
    legacy_admission = json.loads(admission_path.read_text(encoding="utf-8"))
    legacy_admission["schema_version"] = (
        client._PUBLICATION_ADMISSION_SCHEMA_VERIFIER_IDENTITY
    )
    legacy_admission.pop("external_authority_intent_sha256")
    legacy_admission.pop("settlement_evidence_sha256")
    admission_path.write_bytes(
        client._canonical_json_line_bytes(legacy_admission)
    )
    assert legacy_admission["phase"] == "dispatched"
    assert legacy_admission["effect_dispatch_name"] is None
    draft.write_text(replacement_proof, encoding="utf-8")

    replacement_outer_intent_sha256 = "e" * 64
    recovery_requests: list[dict[str, Any]] = []
    dispatches = 0

    def recover_old_authority(request: Any) -> dict[str, Any]:
        recovery_requests.append(dict(request))
        if recovery_race == "target_change":
            verified.write_text("competing publication", encoding="utf-8")
        elif recovery_race == "receipt_change":
            monkeypatch.setattr(
                client,
                "_read_canonical_publication_receipt",
                lambda _path: {"competing": True},
            )
        elif recovery_race == "archive_change":
            monkeypatch.setattr(
                client,
                "_read_prepared_publication_archive",
                lambda **_arguments: {"competing": True},
            )
        elif recovery_race == "rollback_change":
            monkeypatch.setattr(
                client,
                "_read_receipt_collision_rollback",
                lambda **_arguments: {"competing": True},
            )
        return {
            "schema_version": (
                client._OUTER_PUBLICATION_RECOVERY_AUTHORITY_SCHEMA
            ),
            "intent": outer_intent,
            "dispatch": outer_dispatch,
            "settlement": outer_settlement,
            "recovery_blueprint": {
                "schema_version": client._PUBLICATION_RECOVERY_BLUEPRINT_SCHEMA,
                "proof_digest": client.proof_digest(old_proof),
                "proof": old_proof,
            },
        }

    def commit_replacement_dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    monkeypatch.setattr(client, "_VERIFICATION_CALLER_INSTANCE_ID", new_caller)
    verification_arguments = {
        "statement": "S",
        "draft_path": draft,
        "verified_path": verified,
        "endpoint": "https://verifier/verify",
        "receipt_path": receipt,
        "problem_id": "problem",
        "publication_state_root": state_root,
        "on_verifier_dispatch": commit_replacement_dispatch,
        "publication_authority_intent_sha256": (
            replacement_outer_intent_sha256
        ),
        "on_publication_admission_recovery": recover_old_authority,
    }
    if recovery_race in {"cas_change", "certificate_crash"}:
        original_certificate_writer = (
            client._write_publication_recovery_certificate
        )
        certificate_interrupted = False

        def certificate_boundary(**arguments: Any) -> tuple[dict[str, Any], str]:
            nonlocal certificate_interrupted
            certificate = original_certificate_writer(**arguments)
            if recovery_race == "cas_change":
                changed = json.loads(
                    admission_path.read_text(encoding="utf-8")
                )
                changed["created_at_utc"] = "2000-01-01T00:00:00+00:00"
                admission_path.write_bytes(
                    client._canonical_json_line_bytes(changed)
                )
            elif not certificate_interrupted:
                certificate_interrupted = True
                raise RuntimeError("synthetic crash after recovery certificate")
            return certificate

        monkeypatch.setattr(
            client,
            "_write_publication_recovery_certificate",
            certificate_boundary,
        )
    elif recovery_race == "settlement_successor_crash":
        original_begin_admission = client._begin_publication_admission
        successor_interrupted = False

        def crash_before_successor(**arguments: Any) -> dict[str, Any]:
            nonlocal successor_interrupted
            if not successor_interrupted:
                successor_interrupted = True
                raise RuntimeError("synthetic crash before successor admission")
            return original_begin_admission(**arguments)

        monkeypatch.setattr(
            client, "_begin_publication_admission", crash_before_successor
        )
    elif recovery_race == "generation_archive_crash":
        original_record_writer = client._write_direct_finalization_record
        generation_interrupted = False

        def crash_after_generation_archive(
            path: Path, *arguments: Any, **keywords: Any
        ) -> dict[str, Any]:
            nonlocal generation_interrupted
            record = original_record_writer(path, *arguments, **keywords)
            if (
                not generation_interrupted
                and path.name.startswith(
                    ".rethlas-publication-admission-generation-"
                )
            ):
                generation_interrupted = True
                raise RuntimeError(
                    "synthetic crash after retired generation archive"
                )
            return record

        monkeypatch.setattr(
            client,
            "_write_direct_finalization_record",
            crash_after_generation_archive,
        )

    terminal_races = {
        "status_change",
        "target_change",
        "receipt_change",
        "archive_change",
        "rollback_change",
        "cas_change",
    }
    if recovery_race in terminal_races:
        expected_error = (
            "attempt changed during recovery"
            if recovery_race == "status_change"
            else "admission changed before recovery settlement"
            if recovery_race == "cas_change"
            else "publication artifact"
        )
        with pytest.raises(
            client.VerificationExecutionUnknown,
            match=expected_error,
        ):
            client.verify_blueprint_file(**verification_arguments)
        assert len(posts) == 1
        assert dispatches == 0
        observed_admission = json.loads(
            admission_path.read_text(encoding="utf-8")
        )
        certificates_after_race = list(
            state_root.glob(".rethlas-publication-recovery-*.json")
        )
        if recovery_race == "cas_change":
            assert observed_admission != legacy_admission
            assert len(certificates_after_race) == 1
        else:
            assert observed_admission == legacy_admission
            assert not certificates_after_race
        return

    crash_races = {
        "certificate_crash",
        "settlement_successor_crash",
        "generation_archive_crash",
    }
    if recovery_race in crash_races:
        with pytest.raises(RuntimeError, match="synthetic crash"):
            client.verify_blueprint_file(**verification_arguments)
        assert len(posts) == 1
        assert dispatches == 0
        assert len(
            list(state_root.glob(".rethlas-publication-recovery-*.json"))
        ) == 1
        interrupted_admission = json.loads(
            admission_path.read_text(encoding="utf-8")
        )
        if recovery_race == "certificate_crash":
            assert interrupted_admission == legacy_admission
        else:
            assert interrupted_admission["status"] == "settled"
            assert interrupted_admission["settlement_reason"] == (
                "external_operational_nonpublication"
            )

    result = client.verify_blueprint_file(**verification_arguments)

    assert result["published"] is True
    assert verified.read_text(encoding="utf-8") == replacement_proof
    assert dispatches == 1
    assert len(posts) == 3
    assert [request["verification_pass_index"] for request in posts] == [1, 1, 2]
    assert posts[1]["verification_attempt_id"] != posts[0][
        "verification_attempt_id"
    ]
    assert len(status_requests) == (
        4 if recovery_race == "certificate_crash" else 2
    )
    assert all(request == status_requests[0] for request in status_requests)
    assert status_requests[0][0] == posts[0]["verification_attempt_id"]
    assert recovery_requests[0]["schema_version"] == (
        "rethlas_cross_layer_publication_recovery_discovery_v1"
    )
    assert recovery_requests[0]["admission"]["proof_digest"] == (
        client.proof_digest(old_proof)
    )

    retired_generations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in state_root.glob(
            ".rethlas-publication-admission-generation-*.json"
        )
    ]
    assert len(retired_generations) == 1
    retired = retired_generations[0]
    assert retired["settlement_reason"] == "external_operational_nonpublication"
    assert retired["settlement_evidence_sha256"] is not None
    current = json.loads(admission_path.read_text(encoding="utf-8"))
    assert current["external_authority_intent_sha256"] == (
        replacement_outer_intent_sha256
    )
    assert current["generation_parent_sha256"] == hashlib.sha256(
        client._canonical_json_line_bytes(retired)
    ).hexdigest()

    certificates = list(
        state_root.glob(".rethlas-publication-recovery-*.json")
    )
    assert len(certificates) == 1
    certificate = json.loads(certificates[0].read_text(encoding="utf-8"))
    assert certificate["recovery_request"]["admission_prior_sha256"] == (
        hashlib.sha256(
            client._canonical_json_line_bytes(legacy_admission)
        ).hexdigest()
    )
    assert certificate["outer_authority"]["intent_sha256"] == (
        outer_intent_sha256
    )
    assert certificate["recovery_request"]["pass_status_observations"][0][
        "state"
    ] == "operational_failed"
    assert len(
        certificate["recovery_request"]["verifier_effect_identity"][
            "passes"
        ]
    ) == 2
    assert certificate["recovery_request"]["recovery_blueprint"][
        "proof_digest"
    ] == client.proof_digest(old_proof)
    assert certificate["guard_semantics"] == {
        "publication_generation_only": True,
        "verifier_attempt_remains_restartable": True,
        "missing_later_pass_is_not_negative_evidence": True,
    }


@pytest.mark.parametrize(
    ("state", "status_code"),
    [("completed", 200), ("execution_unknown", 409), ("in_progress", 425)],
)
def test_cross_layer_status_rejects_nonoperational_verifier_states(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    status_code: int,
) -> None:
    attempt_id = "veratt_" + "a" * 32
    pass_identity = "a" * 64
    if state == "completed":
        payload: dict[str, Any] = {"verification_status": "final"}
    else:
        failed = state == "execution_unknown"
        seed = {
            "schema_version": "rethlas_verifier_pass_status_snapshot_v1",
            "verification_attempt_id": attempt_id,
            "pass_identity_sha256": pass_identity,
            "state": state,
            "intent_sha256": "b" * 64,
            "caller_instance_id": "vcaller_" + "c" * 32,
            "retry_ordinal": 0,
            "current_item_id": None,
            "current_item_index": None,
            "failure_status_code": 502 if failed else None,
            "failure_sha256": "d" * 64 if failed else None,
            "aggregate_sha256": None,
            "resumable_by_this_service": False,
            "publication_aggregate_present": False,
        }
        payload = {
            "detail": {
                **seed,
                "snapshot_sha256": hashlib.sha256(
                    json.dumps(
                        seed,
                        allow_nan=False,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        }

    class StatusResponse(FakeResponse):
        pass

    response = StatusResponse(payload)
    response.status_code = status_code
    monkeypatch.setattr(client.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(client.VerificationExecutionUnknown):
        client._read_restartable_whole_pass_status(
            endpoint="https://verifier/verify",
            timeout_seconds=30,
            api_token=None,
            verification_attempt_id=attempt_id,
            verification_pass_identity=pass_identity,
        )


def test_resume_dispatched_reuses_exact_outer_owned_verifier_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    state_root = tmp_path / "publication-state"
    draft.write_text(proof, encoding="utf-8")
    outer_intent_sha256 = "a" * 64
    posts: list[dict[str, Any]] = []
    dispatches = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        posts.append(dict(request))
        if len(posts) == 1:
            raise client.requests.ConnectionError(
                "transport lost after exact verifier dispatch"
            )
        return valid_payload(request["proof"])

    def dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    install_post(monkeypatch, verifier)
    with pytest.raises(client.requests.ConnectionError):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
            on_verifier_dispatch=dispatch,
            publication_authority_intent_sha256=outer_intent_sha256,
        )
    monkeypatch.setattr(
        client,
        "_VERIFICATION_CALLER_INSTANCE_ID",
        "vcaller_" + "b" * 32,
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
        publication_authority_intent_sha256=outer_intent_sha256,
        resume_dispatched=True,
    )

    assert result["published"] is True
    assert dispatches == 1
    assert [request["verification_pass_index"] for request in posts] == [1, 1, 2]
    assert posts[0]["verification_attempt_id"] == posts[1][
        "verification_attempt_id"
    ]


def test_resume_dispatched_rejects_another_outer_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    state_root = tmp_path / "publication-state"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def lost(_endpoint: str, _request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        raise client.requests.ConnectionError("lost")

    install_post(monkeypatch, lost)
    with pytest.raises(client.requests.ConnectionError):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
            on_verifier_dispatch=lambda: None,
            publication_authority_intent_sha256="a" * 64,
        )
    with pytest.raises(
        client.VerificationExecutionUnknown,
        match="another outer authority",
    ):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
            publication_authority_intent_sha256="b" * 64,
            resume_dispatched=True,
        )
    assert posts == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://verifier.example/verify",
        "http://127.0.0.1:8000/verify",
        "http://localhost:8000/verify",
        "http://[::1]:8000/verify",
    ],
)
def test_https_and_explicit_loopback_http_endpoints_are_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda actual_endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint=endpoint,
    )

    assert result["published"] is True


def test_expired_whole_verification_deadline_makes_zero_service_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("expired deadline must make zero calls"),
    )
    with pytest.raises(ValueError, match="already expired"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=tmp_path / "blueprint.md",
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
            verification_deadline_utc="2000-01-01T00:00:00+00:00",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://verifier.example/verify",
        "http://192.0.2.1/verify",
        "ftp://verifier.example/verify",
    ],
)
def test_remote_plaintext_and_non_http_endpoints_are_rejected_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="HTTPS or HTTP"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=tmp_path / "missing.md",
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint=endpoint,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://" + "user:password@verifier.example/verify",
        "http://user@localhost:8000/verify",
    ],
)
def test_endpoint_userinfo_is_rejected_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="userinfo"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=tmp_path / "missing.md",
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint=endpoint,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://verifier.example/verify?tenant=alpha",
        "https://verifier.example/verify#signed-route",
    ],
)
def test_endpoint_query_and_fragment_are_rejected_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="query or fragment"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=tmp_path / "missing.md",
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint=endpoint,
        )


def test_digest_mismatch_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "blueprint.md"
    draft.write_text("proof A", encoding="utf-8")
    payload = valid_payload("proof A")
    payload["proof_digest"] = client.proof_digest("different proof")
    install_post(monkeypatch, lambda endpoint, request: payload)

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=tmp_path / "blueprint_verified.md",
        endpoint="https://verifier/verify",
    )
    assert result["published"] is False
    assert result["publication_blocked_reason"] == "invalid_verifier_response"
    assert draft.exists()


def test_draft_change_during_verification_is_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "proof A"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(original, encoding="utf-8")

    def mutate_during_post(endpoint: str, request: dict[str, Any]) -> object:
        draft.write_text("proof B", encoding="utf-8")
        return valid_payload(request["proof"])

    install_post(monkeypatch, mutate_during_post)

    with pytest.raises(ValueError, match="changed during verification"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
        )
    assert draft.read_text(encoding="utf-8") == "proof B"
    assert not verified.exists()


def test_correct_with_findings_is_rejected_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    payload["verification_report"]["gaps"] = [
        {"location": "item-1", "issue": "gap"}
    ]
    install_post(monkeypatch, lambda endpoint, request: payload)

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=tmp_path / "blueprint_verified.md",
        endpoint="https://verifier/verify",
    )
    assert result["published"] is False
    assert result["publication_blocked_reason"] == "invalid_verifier_response"


def test_empty_coverage_is_rejected_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    payload["checked_item_ids"] = []
    install_post(monkeypatch, lambda endpoint, request: payload)

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=tmp_path / "blueprint_verified.md",
        endpoint="https://verifier/verify",
    )
    assert result["published"] is False
    assert result["publication_blocked_reason"] == "invalid_verifier_response"


def test_spoofed_same_count_ids_and_context_digest_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    payload["checked_item_ids"] = ["pi_" + "0" * 24]
    payload["context_digest"] = "0" * 64
    install_post(monkeypatch, lambda endpoint, request: payload)

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=tmp_path / "blueprint_verified.md",
        endpoint="https://verifier/verify",
    )
    assert result["published"] is False
    assert result["publication_blocked_reason"] == "invalid_verifier_response"


@pytest.mark.parametrize("field", ["item_context", "adaptive_digest"])
def test_spoofed_adaptive_context_attestation_is_rejected_before_publish(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    if field == "item_context":
        payload["item_context_attestations"][0]["context_digest"] = "0" * 64
    else:
        payload["adaptive_context_digest"] = "0" * 64
    install_post(monkeypatch, lambda endpoint, request: payload)

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )
    assert result["published"] is False
    assert result["publication_blocked_reason"] == "invalid_verifier_response"

    assert not verified.exists()


def test_non_cooperating_draft_write_cannot_change_published_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "verified bytes"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(original, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )
    real_replace = client._renameat2_at

    def mutate_then_replace(
        directory_fd: int,
        source: str,
        target: str,
        flags: int,
    ) -> None:
        if Path(target).name == verified.name:
            draft.write_text("unverified bytes", encoding="utf-8")
        real_replace(directory_fd, source, target, flags)

    monkeypatch.setattr(client, "_renameat2_at", mutate_then_replace)

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert verified.read_text(encoding="utf-8") == original
    assert draft.read_text(encoding="utf-8") == "unverified bytes"


def test_verified_symlink_is_replaced_with_captured_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "verified bytes"
    draft = tmp_path / "blueprint.md"
    backing = tmp_path / "attacker-controlled.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    backing.write_text(proof, encoding="utf-8")
    verified.symlink_to(backing)
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert not verified.is_symlink()
    backing.write_text("unverified bytes", encoding="utf-8")
    assert verified.read_text(encoding="utf-8") == proof


def test_parent_swap_during_verification_cannot_redirect_publish_or_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "verified bytes"
    draft_parent = tmp_path / "drafts"
    draft_parent.mkdir()
    working_parent = tmp_path / "results" / "problem"
    working_parent.mkdir(parents=True)
    detached_parent = tmp_path / "detached-problem"
    attacker_parent = tmp_path / "attacker-controlled"
    attacker_parent.mkdir()
    draft = draft_parent / "blueprint.md"
    verified = working_parent / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")

    def swap_parent_during_post(endpoint: str, request: dict[str, Any]) -> object:
        if not detached_parent.exists():
            working_parent.rename(detached_parent)
            working_parent.symlink_to(attacker_parent, target_is_directory=True)
        return valid_payload(request["proof"])

    install_post(monkeypatch, swap_parent_during_post)

    with pytest.raises(ValueError, match="parent changed during verification"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            receipt_path=receipt,
            problem_id="problem",
            endpoint="https://verifier/verify",
        )

    assert not (attacker_parent / verified.name).exists()
    assert not (detached_parent / verified.name).exists()
    assert not receipt.exists()


def test_receipt_parent_symlink_is_rejected_without_writing_target(
    tmp_path: Path,
) -> None:
    attacker_parent = tmp_path / "attacker-controlled"
    attacker_parent.mkdir()
    receipt_parent = tmp_path / "receipts"
    receipt_parent.symlink_to(attacker_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="receipt parent"):
        client._write_receipt_atomic(receipt_parent / "problem.json", {"ok": True})

    assert not (attacker_parent / "problem.json").exists()


def test_receipt_target_symlink_is_rejected_without_touching_backing_file(
    tmp_path: Path,
) -> None:
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir()
    backing = tmp_path / "attacker-controlled.json"
    backing.write_text("unchanged", encoding="utf-8")
    receipt = receipt_parent / "problem.json"
    receipt.symlink_to(backing)

    with pytest.raises(ValueError, match="receipt target must not be a symlink"):
        client._write_receipt_atomic(receipt, {"ok": True})

    assert receipt.is_symlink()
    assert backing.read_text(encoding="utf-8") == "unchanged"


def test_draft_symlink_is_rejected_before_network_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backing = tmp_path / "backing.md"
    backing.write_text("proof", encoding="utf-8")
    draft = tmp_path / "blueprint.md"
    draft.symlink_to(backing)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="regular file"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )


def test_oversized_draft_is_rejected_before_read_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "blueprint.md"
    draft.write_bytes(b"x" * 17)
    monkeypatch.setattr(client, "MAX_BLUEPRINT_BYTES", 16)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="VERIFY_MAX_PROOF_BYTES"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )


def test_crlf_bytes_are_hashed_and_published_without_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"# theorem main\r\n\r\n## statement\r\nS\r\n\r\n## proof\r\nP\r\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_bytes(raw)
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert verified.read_bytes() == raw
    assert result["proof_digest"] == client.proof_digest(raw.decode("utf-8"))


def test_same_verified_target_uses_one_cross_draft_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_a = tmp_path / "a" / "blueprint.md"
    draft_b = tmp_path / "b" / "blueprint.md"
    verified = tmp_path / "published" / "blueprint_verified.md"
    draft_a.parent.mkdir()
    draft_b.parent.mkdir()
    draft_a.write_text("proof A", encoding="utf-8")
    draft_b.write_text("proof B", encoding="utf-8")
    receipt = tmp_path / "receipts" / "problem.json"
    first_post_entered = threading.Event()
    release_first_post = threading.Event()
    posts = 0

    def synchronized_response(endpoint: str, request: dict[str, Any]) -> object:
        nonlocal posts
        posts += 1
        if posts == 1:
            first_post_entered.set()
            assert release_first_post.wait(timeout=5)
        return valid_payload(request["proof"])

    install_post(monkeypatch, synchronized_response)
    results: list[dict[str, Any]] = []
    failures: list[Exception] = []

    def publish(draft: Path) -> None:
        try:
            results.append(
                client.verify_blueprint_file(
                    statement="S",
                    draft_path=draft,
                    verified_path=verified,
                    endpoint="https://verifier/verify",
                    receipt_path=receipt,
                    problem_id="problem",
                )
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=publish, args=(draft_a,)),
        threading.Thread(target=publish, args=(draft_b,)),
    ]
    for thread in threads:
        thread.start()
    assert first_post_entered.wait(timeout=5)
    release_first_post.set()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert sum(result["published"] is True for result in results) == 1
    rejected = next(result for result in results if result["published"] is False)
    assert rejected["publication_blocked_reason"] == "prepared_request_drift"
    assert not failures
    assert verified.read_text(encoding="utf-8") in {"proof A", "proof B"}
    durable_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    assert durable_receipt["proof_digest"] == client.proof_digest(
        verified.read_text(encoding="utf-8")
    )
    assert posts == 2


def test_untrusted_stale_verified_target_is_replaced_after_exact_quorum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text("candidate proof", encoding="utf-8")
    verified.write_text("different proof", encoding="utf-8")
    posts = 0
    dispatches = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> object:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    def commit_dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    install_post(monkeypatch, verifier)
    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        on_verifier_dispatch=commit_dispatch,
    )
    assert result["published"] is True
    assert verified.read_text(encoding="utf-8") == "candidate proof"
    assert (posts, dispatches) == (2, 1)


def test_mcp_production_wrapper_uses_problem_id_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_root = tmp_path / "results"
    result_dir = results_root / "category" / "problem"
    result_dir.mkdir(parents=True)
    draft = result_dir / "blueprint.md"
    draft.write_text("candidate proof", encoding="utf-8")
    data_root = tmp_path / "data"
    problem_source = data_root / "category" / "problem.md"
    problem_source.parent.mkdir(parents=True)
    problem_source.write_text("S", encoding="utf-8")
    receipts_root = tmp_path / "trusted-receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    hard_stop = "2099-01-01T00:00:00+00:00"
    monkeypatch.setattr(
        generation_server,
        "_reasoning_phase_preflight",
        lambda tool_name: {
            "tool_permitted": True,
            "hard_stop_at_utc": hard_stop,
        },
    )

    def verifier(endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        assert request["verification_deadline_utc"] == hard_stop
        return valid_payload(request["proof"])

    install_post(
        monkeypatch,
        verifier,
    )

    result = generation_server.verify_blueprint_service(
        problem_id="category/problem",
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert (result_dir / "blueprint_verified.md").read_text(encoding="utf-8") == (
        "candidate proof"
    )
    receipt = receipts_root / "category" / "problem.json"
    assert result["publication_receipt_path"] == str(receipt)
    assert receipt.exists()
    assert json.loads(receipt.read_text(encoding="utf-8"))["proof_digest"] == (
        client.proof_digest("candidate proof")
    )


def test_oversized_publication_receipt_returns_bounded_nonpublication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_root = tmp_path / "results"
    result_dir = results_root / "category" / "problem"
    result_dir.mkdir(parents=True)
    (result_dir / "blueprint.md").write_text(
        "candidate proof", encoding="utf-8"
    )
    data_root = tmp_path / "data"
    problem_source = data_root / "category" / "problem.md"
    problem_source.parent.mkdir(parents=True)
    problem_source.write_text("S", encoding="utf-8")
    receipts_root = tmp_path / "trusted-receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.setattr(client, "MAX_PUBLICATION_RECEIPT_BYTES", 512)
    monkeypatch.setattr(
        mutable_proof_context,
        "parse_blueprint",
        lambda *_args, **_kwargs: pytest.fail(
            "publication must use its frozen proof-context parser"
        ),
    )
    monkeypatch.setattr(
        generation_server,
        "_reasoning_phase_preflight",
        lambda _tool: {
            "tool_permitted": True,
            "hard_stop_at_utc": "2099-01-01T00:00:00+00:00",
        },
    )
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    install_post(
        monkeypatch,
        lambda _endpoint, request: valid_payload(request["proof"]),
    )

    result = generation_server.verify_blueprint_service(
        problem_id="category/problem",
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert result["publication_blocked_reason"] == "receipt_over_limit"
    assert result["publication_receipt_bytes"] > 512
    assert not (result_dir / "blueprint_verified.md").exists()
    assert not (receipts_root / "category" / "problem.json").exists()


def test_direct_oversized_negative_result_is_compacted_and_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def oversized_wrong(
        _endpoint: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        payload = valid_payload(request["proof"], verdict="wrong")
        payload["repair_hints"] = "x" * 2_000_000
        return payload

    install_post(monkeypatch, oversized_wrong)
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert first["published"] is False
    assert first["durable_result_status"] == "compacted"
    assert first["compaction_reason"] == "over_limit"
    assert first["repair_hints_truncated"] is True
    assert len(first["repair_hints"].encode("utf-8")) <= 131_072
    assert posts == 1

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        verification_deadline_utc="2000-01-01T00:00:00+00:00",
    )
    assert replayed == first
    assert posts == 1


def test_legacy_direct_v1_negative_result_replays_without_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def wrong(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"], verdict="wrong")

    install_post(monkeypatch, wrong)
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    rewrite_direct_journal_as_legacy_v1(
        receipt_path=receipt,
        journal_parent=direct_journal_parent(
            receipt_path=receipt, verified_path=verified
        ),
        proof=proof,
        statement="S",
    )
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed == first
    assert posts == 1


def test_legacy_direct_v1_dispatch_without_result_stays_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def lose_response(_endpoint: str, _request: dict[str, Any]) -> object:
        nonlocal posts
        posts += 1
        raise RuntimeError("simulated connection loss")

    install_post(monkeypatch, lose_response)
    with pytest.raises(RuntimeError, match="connection loss"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    legacy_paths = rewrite_direct_journal_as_legacy_v1(
        receipt_path=receipt,
        journal_parent=direct_journal_parent(
            receipt_path=receipt, verified_path=verified
        ),
        proof=proof,
        statement="S",
    )
    assert legacy_paths[1].is_file()
    assert not legacy_paths[2].exists()
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    with pytest.raises(
        client.VerificationExecutionUnknown, match="no durable terminal result"
    ):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert posts == 1


def test_direct_predispatch_large_historical_intent_migrates_under_absolute_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda _endpoint, request: valid_payload(
            request["proof"], verdict="wrong"
        ),
    )
    healthy_get = client.requests.get
    profile_calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal profile_calls
        profile_calls += 1
        if profile_calls == 1:
            raise RuntimeError("simulated predispatch stop")
        return healthy_get(*args, **kwargs)

    monkeypatch.setattr(client.requests, "get", fail_once)
    with pytest.raises(RuntimeError, match="predispatch stop"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    intent_paths = list(
        direct_journal_parent(
            receipt_path=receipt, verified_path=verified
        ).glob(".rethlas-verification-*.intent.json")
    )
    assert len(intent_paths) == 1
    intent = json.loads(intent_paths[0].read_text(encoding="utf-8"))
    intent["endpoint"] = "https://historical.invalid/" + "x" * 200_000
    intent["max_intent_bytes"] = 1_048_576
    intent_paths[0].write_bytes(client._canonical_json_line_bytes(intent))
    assert intent_paths[0].stat().st_size > 131_072

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert result["published"] is False
    migrated = json.loads(intent_paths[0].read_text(encoding="utf-8"))
    assert migrated["endpoint"] == "https://verifier/verify"
    assert migrated["max_intent_bytes"] == 131_072
    assert intent_paths[0].stat().st_size <= 131_072
    assert profile_calls == 2


def test_direct_terminal_result_is_not_replayed_across_statement_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    statement_a = "S\n\n## Retrieval restriction\nPolicy A"
    statement_b = "S\n\n## Retrieval restriction\nPolicy B"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def wrong(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(
            request["proof"], statement=request["statement"], verdict="wrong"
        )

    install_post(monkeypatch, wrong)
    first = client.verify_blueprint_file(
        statement=statement_a,
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    second = client.verify_blueprint_file(
        statement=statement_b,
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert first["published"] is False
    assert second["published"] is False
    assert posts == 2
    assert len(
        list(
            direct_journal_parent(
                receipt_path=receipt, verified_path=verified
            ).glob(".rethlas-verification-*.intent.json")
        )
    ) == 2


def test_fresh_publication_does_not_overwrite_postcheck_external_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_renameat2 = client._renameat2_at
    raced = False

    def race_before_exchange(
        directory_fd: int, source: str, destination: str, flags: int
    ) -> None:
        nonlocal raced
        if destination == verified.name and not raced:
            raced = True
            external = tmp_path / "external-writer.tmp"
            external.write_text("stale B", encoding="utf-8")
            client.os.replace(external, verified)
        real_renameat2(directory_fd, source, destination, flags)

    monkeypatch.setattr(client, "_renameat2_at", race_before_exchange)
    rejected = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )

    assert raced is True
    assert rejected == replayed
    assert rejected["published"] is False
    assert rejected["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert verified.read_text(encoding="utf-8") == "stale B"
    assert posts == 2


@pytest.mark.parametrize("initial_target", ["regular", "symlink"])
def test_conditional_exchange_target_removed_after_precheck_is_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_target: str,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    if initial_target == "regular":
        verified.write_text("stale A", encoding="utf-8")
    else:
        backing = tmp_path / "stale-backing.md"
        backing.write_text("stale A", encoding="utf-8")
        verified.symlink_to(backing)
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_renameat2 = client._renameat2_at
    raced = False

    def remove_before_exchange(
        directory_fd: int, source: str, destination: str, flags: int
    ) -> None:
        nonlocal raced
        if (
            destination == verified.name
            and flags == client._RENAME_EXCHANGE
            and not raced
        ):
            raced = True
            client.os.unlink(destination, dir_fd=directory_fd)
            client.os.fsync(directory_fd)
        real_renameat2(directory_fd, source, destination, flags)

    monkeypatch.setattr(client, "_renameat2_at", remove_before_exchange)
    rejected = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )

    assert raced is True
    assert rejected == replayed
    assert rejected["published"] is False
    assert rejected["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert not verified.exists()
    assert not verified.is_symlink()
    assert posts == 2


def test_target_collision_settles_if_receipt_parent_is_replaced_after_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt_parent = tmp_path / "receipts"
    receipt = receipt_parent / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_conditional_replace = client._conditional_replace_at
    raced = False

    def collide_then_replace_receipt_parent(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        nonlocal raced
        if filename == verified.name and not raced:
            raced = True
            external = tmp_path / "external-writer.tmp"
            external.write_text("stale B", encoding="utf-8")
            client.os.replace(external, verified)
        outcome = real_conditional_replace(
            directory_fd, filename, content, **kwargs
        )
        if filename == verified.name and raced:
            assert outcome is None
            receipt_parent.rename(tmp_path / "receipts.detached")
            receipt_parent.mkdir()
        return outcome

    monkeypatch.setattr(
        client, "_conditional_replace_at", collide_then_replace_receipt_parent
    )
    rejected = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert raced is True
    assert rejected["published"] is False
    assert rejected["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert verified.read_text(encoding="utf-8") == "stale B"
    assert not receipt.exists()
    assert posts == 2

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed == rejected
    assert posts == 2


def test_settled_collision_replays_before_competing_canonical_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt_parent = tmp_path / "receipts"
    receipt = receipt_parent / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_conditional_replace = client._conditional_replace_at
    raced = False

    def collide_then_install_competing_receipt(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        nonlocal raced
        if filename == verified.name and not raced:
            raced = True
            external = tmp_path / "external-writer.tmp"
            external.write_text("stale B", encoding="utf-8")
            client.os.replace(external, verified)
        outcome = real_conditional_replace(
            directory_fd, filename, content, **kwargs
        )
        if filename == verified.name and raced:
            assert outcome is None
            receipt_parent.rename(tmp_path / "receipts.detached")
            receipt_parent.mkdir()
            receipt.write_bytes(
                client._canonical_json_line_bytes({"winner": "other"})
            )
        return outcome

    monkeypatch.setattr(
        client,
        "_conditional_replace_at",
        collide_then_install_competing_receipt,
    )
    rejected = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert rejected["published"] is False
    assert rejected["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert posts == 2

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed == rejected
    assert posts == 2


def test_prepared_archive_recovers_crash_before_canonical_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_write_receipt = client._write_receipt_atomic_at

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_before_receipt(*args: Any, **kwargs: Any) -> tuple[int, int]:
        raise SimulatedPowerLoss

    monkeypatch.setattr(client, "_write_receipt_atomic_at", crash_before_receipt)
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert posts == 2
    assert not receipt.exists()
    assert not verified.exists()

    monkeypatch.setattr(client, "_write_receipt_atomic_at", real_write_receipt)
    monkeypatch.setattr(client, "MAX_PUBLICATION_RECEIPT_BYTES", 1)
    monkeypatch.setattr(client, "MAX_BLUEPRINT_BYTES", 1)
    monkeypatch.setattr(client, "MAX_BLUEPRINT_CHARS", 1)
    monkeypatch.setattr(client, "MAX_PUBLICATION_PROOF_ITEMS", 0)
    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert recovered["published"] is True
    assert recovered["recovered_prepared_publication"] is True
    assert receipt.is_file()
    assert verified.read_text(encoding="utf-8") == proof
    assert posts == 2


def test_archive_and_canonical_receipt_replay_after_receipt_cap_decrease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert first["published"] is True
    assert receipt.stat().st_size > 1
    monkeypatch.setattr(client, "MAX_PUBLICATION_RECEIPT_BYTES", 1)
    monkeypatch.setattr(client, "MAX_BLUEPRINT_BYTES", 1)
    monkeypatch.setattr(client, "MAX_BLUEPRINT_CHARS", 1)
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed["published"] is True
    assert replayed["recovered_prepared_publication"] is True
    assert posts == 2


def test_prepared_recovery_uses_persisted_cap_for_old_regular_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale target", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_conditional_replace = client._conditional_replace_at

    class SimulatedPowerLoss(BaseException):
        pass

    monkeypatch.setattr(
        client,
        "_conditional_replace_at",
        lambda *args, **kwargs: (_ for _ in ()).throw(SimulatedPowerLoss()),
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert receipt.is_file()
    assert posts == 2
    monkeypatch.setattr(
        client, "_conditional_replace_at", real_conditional_replace
    )
    monkeypatch.setattr(client, "MAX_BLUEPRINT_BYTES", 1)
    monkeypatch.setattr(client, "MAX_BLUEPRINT_CHARS", 1)
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert recovered["published"] is True
    assert recovered["recovered_prepared_publication"] is True
    assert verified.read_text(encoding="utf-8") == proof
    assert posts == 2


def test_core_dispatch_only_restart_consumes_archive_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import claude_core

    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    generation_root = tmp_path / "generation"
    result_dir = generation_root / "results" / "problem"
    statement_path = generation_root / "data" / "problem.md"
    result_dir.mkdir(parents=True)
    statement_path.parent.mkdir(parents=True)
    statement_path.write_text("S", encoding="utf-8")
    draft = result_dir / "blueprint.md"
    verified = result_dir / "blueprint_verified.md"
    receipt_root = tmp_path / "receipts"
    receipt = receipt_root / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    statement_sha256 = hashlib.sha256(b"S").hexdigest()
    blueprint_sha256 = hashlib.sha256(proof.encode("utf-8")).hexdigest()
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipt_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_MODULE", None)
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_SHA256", None)

    class LegacyStub:
        VERIFY_PROOF_URL = "https://verifier/verify"
        verify_blueprint_file = staticmethod(client.verify_blueprint_file)

    monkeypatch.setattr(claude_core, "_LEGACY_MODULE", LegacyStub)
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="problem",
        statement_sha256=statement_sha256,
        blueprint_sha256=blueprint_sha256,
    )
    real_write_receipt = client._write_receipt_atomic_at

    class SimulatedPowerLoss(BaseException):
        pass

    monkeypatch.setattr(
        client,
        "_write_receipt_atomic_at",
        lambda *args, **kwargs: (_ for _ in ()).throw(SimulatedPowerLoss()),
    )

    def initial_verifier(commit_dispatch: Callable[[], None]) -> dict[str, Any]:
        return client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            blueprint_root=generation_root / "results",
            on_verifier_dispatch=commit_dispatch,
        )

    with pytest.raises(SimulatedPowerLoss):
        claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=initial_verifier,
        )
    assert (intent_path.parent / "dispatch.json").is_file()
    assert not (intent_path.parent / "result.json").exists()
    assert not receipt.exists()
    assert posts == 2

    monkeypatch.setattr(client, "_write_receipt_atomic_at", real_write_receipt)
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    recovered = claude_core._execute_publication_finalization_verifier(
        intent=intent,
        intent_path=intent_path,
        verifier=lambda _commit: pytest.fail("verifier callback must not run"),
    )
    assert recovered["published"] is True
    assert receipt.is_file()
    assert verified.read_text(encoding="utf-8") == proof
    assert (intent_path.parent / "result.json").is_file()
    assert posts == 2


@pytest.mark.parametrize("drift_kind", ["proof_source", "context_digest"])
def test_archive_only_source_drift_settles_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_kind: str,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    original_proof_source = (
        client._assert_publication_proof_context_unchanged()
    )
    original_aggregate_context_digest = client.aggregate_context_digest
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_write_receipt = client._write_receipt_atomic_at

    class SimulatedPowerLoss(BaseException):
        pass

    monkeypatch.setattr(
        client,
        "_write_receipt_atomic_at",
        lambda *args, **kwargs: (_ for _ in ()).throw(SimulatedPowerLoss()),
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    monkeypatch.setattr(client, "_write_receipt_atomic_at", real_write_receipt)
    if drift_kind == "proof_source":
        monkeypatch.setattr(
            client,
            "_assert_publication_proof_context_unchanged",
            lambda: "f" * 64,
        )
    else:
        monkeypatch.setattr(
            client,
            "aggregate_context_digest",
            lambda _manifest: "e" * 64,
        )
    settled = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        verification_deadline_utc="2000-01-01T00:00:00+00:00",
    )
    assert settled["published"] is False
    assert settled["publication_blocked_reason"] == "prepared_request_drift"
    assert posts == 2

    # The rejected generation must not poison the stable archive slot or the
    # direct-dispatch journal forever.  A second call under the new source may
    # run its own quorum, and the third call replays without more I/O.
    republished = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert republished["published"] is True
    assert posts == 4
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed["published"] is True
    assert replayed["recovered_prepared_publication"] is True
    assert posts == 4

    if drift_kind == "proof_source":
        monkeypatch.setattr(
            client,
            "_assert_publication_proof_context_unchanged",
            lambda: original_proof_source,
        )
    else:
        monkeypatch.setattr(
            client,
            "aggregate_context_digest",
            original_aggregate_context_digest,
        )
    settled_second_generation = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert settled_second_generation["published"] is False
    assert settled_second_generation["publication_blocked_reason"] == (
        "prepared_request_drift"
    )
    republished_original_generation = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert republished_original_generation["published"] is True
    assert posts == 6

    # Retiring the second occurrence of identity A must not collide with the
    # immutable archive for its first occurrence.
    if drift_kind == "proof_source":
        monkeypatch.setattr(
            client,
            "_assert_publication_proof_context_unchanged",
            lambda: "d" * 64,
        )
    else:
        monkeypatch.setattr(
            client,
            "aggregate_context_digest",
            lambda _manifest: "d" * 64,
        )
    settled_revisited_generation = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert settled_revisited_generation["published"] is False
    final_generation = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert final_generation["published"] is True
    assert posts == 8


def test_waiting_publisher_rereads_archive_after_effect_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    first_post_entered = threading.Event()
    release_first_post = threading.Event()
    second_waiting_for_effect_lock = threading.Event()
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        if posts == 1:
            first_post_entered.set()
            assert release_first_post.wait(timeout=5)
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_acquire = client._acquire_publication_lock

    def observe_waiter(handle: Any, *, display_path: Path) -> None:
        if (
            threading.current_thread().name == "waiting-publisher"
            and display_path.name.startswith(
                ".rethlas-publication-identity-"
            )
        ):
            second_waiting_for_effect_lock.set()
        real_acquire(handle, display_path=display_path)

    monkeypatch.setattr(client, "_acquire_publication_lock", observe_waiter)
    real_commit_archive = client._commit_prepared_publication_archive

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_after_archive(**kwargs: Any) -> tuple[dict[str, Any], bytes]:
        committed = real_commit_archive(**kwargs)
        if threading.current_thread().name == "archive-writer":
            raise SimulatedPowerLoss
        return committed

    monkeypatch.setattr(
        client, "_commit_prepared_publication_archive", crash_after_archive
    )
    failures: list[BaseException] = []
    recovered: list[dict[str, Any]] = []

    def publish(*, writer: bool) -> None:
        try:
            value = client.verify_blueprint_file(
                statement="S",
                draft_path=draft,
                verified_path=verified,
                endpoint="https://verifier/verify",
                receipt_path=receipt,
                problem_id="problem",
            )
        except BaseException as exc:
            failures.append(exc)
        else:
            recovered.append(value)

    writer = threading.Thread(
        target=publish, kwargs={"writer": True}, name="archive-writer"
    )
    waiter = threading.Thread(
        target=publish, kwargs={"writer": False}, name="waiting-publisher"
    )
    writer.start()
    assert first_post_entered.wait(timeout=5)
    waiter.start()
    assert second_waiting_for_effect_lock.wait(timeout=5)
    release_first_post.set()
    writer.join(timeout=10)
    waiter.join(timeout=10)

    assert not writer.is_alive()
    assert not waiter.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], SimulatedPowerLoss)
    assert len(recovered) == 1
    assert recovered[0]["published"] is True
    assert recovered[0]["recovered_prepared_publication"] is True
    assert posts == 2


@pytest.mark.parametrize(
    "problem_ids",
    [("problem", "problem"), ("problem-a", "problem-b")],
)
def test_same_receipt_cannot_publish_two_verified_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem_ids: tuple[str, str],
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    receipt_parent = tmp_path / "receipts"
    # These asymmetric targets used to choose different journal ancestors:
    # one inside the receipt tree, and one in a sibling tree.  A receipt mutex
    # whose directory depends on the target therefore failed to serialize them.
    result_a = receipt_parent / "inside"
    result_b = tmp_path / "results" / "b"
    result_a.mkdir(parents=True)
    result_b.mkdir(parents=True)
    draft_a = result_a / "blueprint.md"
    draft_b = result_b / "blueprint.md"
    verified_a = result_a / "blueprint_verified.md"
    verified_b = result_b / "blueprint_verified.md"
    receipt = receipt_parent / "problem.json"
    draft_a.write_text(proof, encoding="utf-8")
    draft_b.write_text(proof, encoding="utf-8")
    first_post_entered = threading.Event()
    release_first_publisher = threading.Event()
    second_waiting_for_identity_lock = threading.Event()
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        if posts == 1:
            first_post_entered.set()
            assert release_first_publisher.wait(timeout=5)
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_acquire = client._acquire_publication_lock

    def observe_second(handle: Any, *, display_path: Path) -> None:
        if (
            threading.current_thread().name == "publisher-b"
            and display_path.name.startswith(
                ".rethlas-publication-identity-"
            )
        ):
            second_waiting_for_identity_lock.set()
        real_acquire(handle, display_path=display_path)

    monkeypatch.setattr(client, "_acquire_publication_lock", observe_second)
    outcomes: dict[str, dict[str, Any]] = {}
    failures: list[BaseException] = []

    def publish(label: str, draft: Path, verified: Path) -> None:
        try:
            outcomes[label] = client.verify_blueprint_file(
                statement="S",
                draft_path=draft,
                verified_path=verified,
                endpoint="https://verifier/verify",
                receipt_path=receipt,
                problem_id=problem_ids[0 if label == "a" else 1],
            )
        except BaseException as exc:
            failures.append(exc)

    publisher_a = threading.Thread(
        target=publish,
        args=("a", draft_a, verified_a),
        name="publisher-a",
    )
    publisher_b = threading.Thread(
        target=publish,
        args=("b", draft_b, verified_b),
        name="publisher-b",
    )
    publisher_a.start()
    assert first_post_entered.wait(timeout=5)
    publisher_b.start()
    assert second_waiting_for_identity_lock.wait(timeout=5)
    release_first_publisher.set()
    publisher_a.join(timeout=10)
    publisher_b.join(timeout=10)

    assert not failures
    assert outcomes["a"]["published"] is True
    assert outcomes["b"]["published"] is False
    assert outcomes["b"]["publication_blocked_reason"] == (
        "verified_target_collision"
    )
    assert verified_a.read_text(encoding="utf-8") == proof
    assert not verified_b.exists()
    assert posts == 2


def test_receipt_admission_survives_archive_crash_across_verified_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    state_root = tmp_path / "publication-state"
    result_a = tmp_path / "results" / "a"
    result_b = tmp_path / "results" / "b"
    result_a.mkdir(parents=True)
    result_b.mkdir(parents=True)
    draft_a = result_a / "blueprint.md"
    draft_b = result_b / "blueprint.md"
    verified_a = result_a / "blueprint_verified.md"
    verified_b = result_b / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft_a.write_text(proof, encoding="utf-8")
    draft_b.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_write_receipt = client._write_receipt_atomic_at

    class SimulatedPowerLoss(BaseException):
        pass

    monkeypatch.setattr(
        client,
        "_write_receipt_atomic_at",
        lambda *args, **kwargs: (_ for _ in ()).throw(SimulatedPowerLoss()),
    )
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft_a,
            verified_path=verified_a,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
        )
    assert posts == 2
    monkeypatch.setattr(client, "_write_receipt_atomic_at", real_write_receipt)

    blocked = client.verify_blueprint_file(
        statement="S",
        draft_path=draft_b,
        verified_path=verified_b,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
    )
    assert blocked["published"] is False
    assert blocked["publication_blocked_reason"] == (
        "verified_target_collision"
    )
    assert posts == 2
    assert not verified_b.exists()

    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft_a,
        verified_path=verified_a,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
    )
    assert recovered["published"] is True
    assert recovered["recovered_prepared_publication"] is True
    assert verified_a.read_text(encoding="utf-8") == proof
    assert posts == 2


def test_successful_cas_repairs_replaced_empty_receipt_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt_parent = tmp_path / "receipts"
    receipt = receipt_parent / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_conditional_replace = client._conditional_replace_at
    raced = False

    def replace_receipt_parent_after_success(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        nonlocal raced
        outcome = real_conditional_replace(
            directory_fd, filename, content, **kwargs
        )
        if filename == verified.name and outcome is not None and not raced:
            raced = True
            receipt_parent.rename(tmp_path / "receipts.detached")
            receipt_parent.mkdir()
        return outcome

    monkeypatch.setattr(
        client,
        "_conditional_replace_at",
        replace_receipt_parent_after_success,
    )
    published = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )

    assert raced is True
    assert published["published"] is True
    assert verified.read_text(encoding="utf-8") == proof
    assert receipt.is_file()
    assert not (tmp_path / "receipts.detached" / "problem.json").exists()
    assert posts == 2


def test_successful_cas_rolls_back_for_competing_replacement_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt_parent = tmp_path / "receipts"
    receipt = receipt_parent / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_conditional_replace = client._conditional_replace_at
    raced = False

    def install_competing_receipt_after_success(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        nonlocal raced
        outcome = real_conditional_replace(
            directory_fd, filename, content, **kwargs
        )
        if filename == verified.name and outcome is not None and not raced:
            raced = True
            receipt_parent.rename(tmp_path / "receipts.detached")
            receipt_parent.mkdir()
            receipt.write_bytes(
                client._canonical_json_line_bytes({"winner": "other"})
            )
        return outcome

    monkeypatch.setattr(
        client,
        "_conditional_replace_at",
        install_competing_receipt_after_success,
    )
    rejected = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert raced is True
    assert rejected["published"] is False
    assert rejected["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert verified.read_text(encoding="utf-8") == "stale A"
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "winner": "other"
    }
    assert posts == 2

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed == rejected
    assert posts == 2


@pytest.mark.parametrize("crash_point", ["before_rollback", "after_rollback"])
def test_competing_receipt_rollback_recovers_power_loss_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt_parent = tmp_path / "receipts"
    receipt = receipt_parent / "problem.json"
    state_root = tmp_path / "publication-state"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_conditional_replace = client._conditional_replace_at

    def install_competing_receipt_after_success(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        outcome = real_conditional_replace(
            directory_fd, filename, content, **kwargs
        )
        if filename == verified.name and outcome is not None:
            receipt_parent.rename(tmp_path / "receipts.detached")
            receipt_parent.mkdir()
            receipt.write_bytes(
                client._canonical_json_line_bytes({"winner": "other"})
            )
        return outcome

    monkeypatch.setattr(
        client,
        "_conditional_replace_at",
        install_competing_receipt_after_success,
    )

    class SimulatedPowerLoss(BaseException):
        pass

    if crash_point == "before_rollback":
        real_recover = client._recover_receipt_collision_rollback_at
        crashed = False

        def crash_before_rollback(*args: Any, **kwargs: Any) -> Any:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise SimulatedPowerLoss
            return real_recover(*args, **kwargs)

        monkeypatch.setattr(
            client,
            "_recover_receipt_collision_rollback_at",
            crash_before_rollback,
        )
    else:
        real_commit_settlement = (
            client._commit_prepared_publication_settlement
        )
        crashed = False

        def crash_after_rollback(*args: Any, **kwargs: Any) -> Any:
            nonlocal crashed
            if kwargs.get("reason") == "prepared_target_collision" and not crashed:
                crashed = True
                raise SimulatedPowerLoss
            return real_commit_settlement(*args, **kwargs)

        monkeypatch.setattr(
            client,
            "_commit_prepared_publication_settlement",
            crash_after_rollback,
        )

    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
            publication_state_root=state_root,
        )
    assert posts == 2
    if crash_point == "before_rollback":
        monkeypatch.setattr(
            client,
            "_recover_receipt_collision_rollback_at",
            real_recover,
        )
    else:
        monkeypatch.setattr(
            client,
            "_commit_prepared_publication_settlement",
            real_commit_settlement,
        )
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        publication_state_root=state_root,
    )
    assert recovered["published"] is False
    assert recovered["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert verified.read_text(encoding="utf-8") == "stale A"
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "winner": "other"
    }
    assert posts == 2


@pytest.mark.parametrize("power_loss_between_exchanges", [False, True])
def test_receipt_rollback_preserves_writer_between_check_and_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    power_loss_between_exchanges: bool,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt_parent = tmp_path / "receipts"
    receipt = receipt_parent / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_conditional_replace = client._conditional_replace_at
    rollback_started = False

    def install_competing_receipt_after_success(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        nonlocal rollback_started
        outcome = real_conditional_replace(
            directory_fd, filename, content, **kwargs
        )
        if filename == verified.name and outcome is not None:
            receipt_parent.rename(tmp_path / "receipts.detached")
            receipt_parent.mkdir()
            receipt.write_bytes(
                client._canonical_json_line_bytes({"winner": "other"})
            )
            rollback_started = True
        return outcome

    monkeypatch.setattr(
        client,
        "_conditional_replace_at",
        install_competing_receipt_after_success,
    )
    real_renameat2 = client._renameat2_at
    writer_raced = False
    crashed = False

    class SimulatedPowerLoss(BaseException):
        pass

    def race_rollback_exchange(
        directory_fd: int, source: str, destination: str, flags: int
    ) -> None:
        nonlocal writer_raced, crashed
        if (
            rollback_started
            and not writer_raced
            and destination == verified.name
            and flags == client._RENAME_EXCHANGE
        ):
            writer_raced = True
            external = tmp_path / "later-writer.tmp"
            external.write_text("stale B", encoding="utf-8")
            client.os.replace(external, verified)
        real_renameat2(directory_fd, source, destination, flags)
        if (
            power_loss_between_exchanges
            and writer_raced
            and not crashed
            and destination == verified.name
            and flags == client._RENAME_EXCHANGE
        ):
            crashed = True
            raise SimulatedPowerLoss

    monkeypatch.setattr(client, "_renameat2_at", race_rollback_exchange)
    arguments = {
        "statement": "S",
        "draft_path": draft,
        "verified_path": verified,
        "endpoint": "https://verifier/verify",
        "receipt_path": receipt,
        "problem_id": "problem",
    }
    if power_loss_between_exchanges:
        with pytest.raises(SimulatedPowerLoss):
            client.verify_blueprint_file(**arguments)
        monkeypatch.setattr(client, "_renameat2_at", real_renameat2)
        monkeypatch.setattr(
            client.requests,
            "get",
            lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
        )
        monkeypatch.setattr(
            client.requests,
            "post",
            lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
        )
        rejected = client.verify_blueprint_file(**arguments)
    else:
        rejected = client.verify_blueprint_file(**arguments)
    assert writer_raced is True
    assert rejected["published"] is False
    assert rejected["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert verified.read_text(encoding="utf-8") == "stale B"
    assert posts == 2


def test_conditional_swap_recovers_crash_and_restores_racing_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_renameat2 = client._renameat2_at
    crashed = False

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_after_racing_exchange(
        directory_fd: int, source: str, destination: str, flags: int
    ) -> None:
        nonlocal crashed
        if destination == verified.name and not crashed:
            crashed = True
            external = tmp_path / "external-writer.tmp"
            external.write_text("stale B", encoding="utf-8")
            client.os.replace(external, verified)
            real_renameat2(directory_fd, source, destination, flags)
            raise SimulatedPowerLoss
        real_renameat2(directory_fd, source, destination, flags)

    monkeypatch.setattr(client, "_renameat2_at", crash_after_racing_exchange)
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert verified.read_text(encoding="utf-8") == proof
    assert posts == 2

    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert recovered == replayed
    assert recovered["published"] is False
    assert recovered["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert verified.read_text(encoding="utf-8") == "stale B"
    assert posts == 2


def test_conditional_noreplace_recovers_crash_before_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_renameat2 = client._renameat2_at
    crashed = False

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_after_noreplace(
        directory_fd: int, source: str, destination: str, flags: int
    ) -> None:
        nonlocal crashed
        real_renameat2(directory_fd, source, destination, flags)
        if flags == client._RENAME_NOREPLACE and not crashed:
            crashed = True
            raise SimulatedPowerLoss

    monkeypatch.setattr(client, "_renameat2_at", crash_after_noreplace)
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert verified.read_text(encoding="utf-8") == proof
    assert posts == 2

    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert recovered["published"] is True
    assert recovered["recovered_prepared_publication"] is True
    assert posts == 2


@pytest.mark.parametrize("initial_target", ["absent", "regular"])
def test_conditional_swap_replay_preserves_writer_after_successful_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_target: str,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    if initial_target == "regular":
        verified.write_text("stale A", encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_renameat2 = client._renameat2_at
    crashed = False

    class SimulatedPowerLoss(BaseException):
        pass

    def writer_wins_after_rename(
        directory_fd: int, source: str, destination: str, flags: int
    ) -> None:
        nonlocal crashed
        real_renameat2(directory_fd, source, destination, flags)
        if destination == verified.name and not crashed:
            crashed = True
            external = tmp_path / "external-writer.tmp"
            external.write_text("writer B", encoding="utf-8")
            client.os.replace(external, verified)
            raise SimulatedPowerLoss

    monkeypatch.setattr(client, "_renameat2_at", writer_wins_after_rename)
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert verified.read_text(encoding="utf-8") == "writer B"
    assert posts == 2

    recovered = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert recovered == replayed
    assert recovered["published"] is False
    assert recovered["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert verified.read_text(encoding="utf-8") == "writer B"
    assert posts == 2


def test_direct_journal_uses_fixed_width_name_for_long_receipt_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    problem_id = "x" * 160
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / f"{problem_id}.json"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda _endpoint, request: valid_payload(
            request["proof"], verdict="wrong"
        ),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id=problem_id,
    )

    assert result["published"] is False
    journal_paths = list(
        direct_journal_parent(
            receipt_path=receipt, verified_path=verified
        ).glob(".rethlas-verification-*.intent.json")
    )
    assert len(journal_paths) == 1
    assert len(journal_paths[0].name.encode("utf-8")) < 255


def test_direct_journal_survives_receipt_parent_rename_between_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    draft = result_dir / "blueprint.md"
    verified = result_dir / "blueprint_verified.md"
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir()
    receipt = receipt_parent / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def wrong(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"], verdict="wrong")

    install_post(monkeypatch, wrong)
    healthy_get = client.requests.get
    renamed = False

    def rename_receipt_parent_after_intent(
        *args: Any, **kwargs: Any
    ) -> FakeResponse:
        nonlocal renamed
        if not renamed:
            renamed = True
            receipt_parent.replace(tmp_path / "receipts.old")
            receipt_parent.mkdir()
        return healthy_get(*args, **kwargs)

    monkeypatch.setattr(
        client.requests, "get", rename_receipt_parent_after_intent
    )
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )

    assert renamed is True
    assert replayed == first
    assert posts == 1
    journal_parent = direct_journal_parent(
        receipt_path=receipt, verified_path=verified
    )
    assert len(list(journal_parent.glob(".rethlas-verification-*.intent.json"))) == 1
    assert not list(receipt_parent.glob(".rethlas-verification-*.json"))
    assert not list(
        (tmp_path / "receipts.old").glob(".rethlas-verification-*.json")
    )


def test_direct_dispatch_survives_results_root_rename_at_post_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    results_root = tmp_path / "generation" / "results"
    result_dir = results_root / "problem"
    result_dir.mkdir(parents=True)
    draft = result_dir / "blueprint.md"
    verified = result_dir / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0
    renamed = False

    def rename_at_post_entry(
        _endpoint: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal posts, renamed
        posts += 1
        if not renamed:
            renamed = True
            results_root.rename(tmp_path / "results.detached")
            replacement_dir = results_root / "problem"
            replacement_dir.mkdir(parents=True)
            (replacement_dir / "blueprint.md").write_text(
                proof, encoding="utf-8"
            )
        return valid_payload(request["proof"], verdict="wrong")

    install_post(monkeypatch, rename_at_post_entry)
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        blueprint_root=results_root,
    )
    assert renamed is True
    assert first["published"] is False
    assert posts == 1

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
        blueprint_root=results_root,
    )
    assert replayed == first
    assert posts == 1


@pytest.mark.parametrize("verified_target", ["missing", "symlink", "regular"])
@pytest.mark.parametrize("restart_entry", ["core", "cli", "legacy"])
def test_receipt_first_crash_recovers_through_normal_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verified_target: str,
    restart_entry: str,
) -> None:
    from agents import claude_core

    generation_root = tmp_path / "generation"
    results_root = generation_root / "results"
    result_dir = results_root / "category" / "problem"
    result_dir.mkdir(parents=True)
    proof = (
        "# theorem main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nS\n\n## proof\nP\n"
    )
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    verified_path = result_dir / "blueprint_verified.md"
    symlink_target = tmp_path / "symlink-target.md"
    if verified_target == "symlink":
        symlink_target.write_text(proof, encoding="utf-8")
        verified_path.symlink_to(symlink_target)
    elif verified_target == "regular":
        verified_path.write_text("untrusted stale proof", encoding="utf-8")
    data_root = generation_root / "data"
    statement_path = data_root / "category" / "problem.md"
    statement_path.parent.mkdir(parents=True)
    statement_path.write_text("S", encoding="utf-8")
    receipts_root = tmp_path / "trusted-receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.setattr(
        generation_server,
        "_reasoning_phase_preflight",
        lambda _tool: {
            "tool_permitted": True,
            "hard_stop_at_utc": "2099-01-01T00:00:00+00:00",
        },
    )
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_atomic_replace = client._conditional_replace_at

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_before_verified(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        if filename == "blueprint_verified.md":
            raise SimulatedPowerLoss
        return real_atomic_replace(directory_fd, filename, content, **kwargs)

    monkeypatch.setattr(client, "_conditional_replace_at", crash_before_verified)
    with pytest.raises(SimulatedPowerLoss):
        generation_server.verify_blueprint_service(
            problem_id="category/problem",
            endpoint="https://verifier/verify",
        )
    receipt_path = receipts_root / "category" / "problem.json"
    assert receipt_path.is_file()
    if verified_target == "missing":
        assert not verified_path.exists()
    elif verified_target == "symlink":
        assert verified_path.is_symlink()
    else:
        assert verified_path.read_text(encoding="utf-8") == (
            "untrusted stale proof"
        )
    assert posts == 2

    monkeypatch.setattr(client, "_conditional_replace_at", real_atomic_replace)
    if restart_entry == "legacy":
        real_read_receipt = client._read_canonical_publication_receipt
        receipt_reads = 0

        def hide_prepared_receipt_until_lock(
            path: Path,
        ) -> tuple[dict[str, Any], bytes] | None:
            nonlocal receipt_reads
            receipt_reads += 1
            if receipt_reads == 1:
                return None
            return real_read_receipt(path)

        monkeypatch.setattr(
            client,
            "_read_canonical_publication_receipt",
            hide_prepared_receipt_until_lock,
        )
        monkeypatch.setattr(
            generation_server,
            "_reasoning_phase_preflight",
            lambda _tool: {
                "tool_permitted": True,
                "hard_stop_at_utc": "2000-01-01T00:00:00+00:00",
            },
        )
        publication = generation_server.verify_blueprint_service(
            problem_id="category/problem",
            endpoint="https://verifier/verify",
        )
    else:
        monkeypatch.setattr(claude_core, "GENERATION_ROOT", generation_root)
        monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipts_root)
        monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
        monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_MODULE", None)
        monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_SHA256", None)
        monkeypatch.setattr(
            claude_core,
            "_LEGACY_MODULE",
            type(
                "LegacyStub",
                (),
                {
                    "VERIFY_PROOF_URL": "https://verifier/verify",
                    "verify_blueprint_file": staticmethod(
                        client.verify_blueprint_file
                    ),
                },
            ),
        )
        proof_sha256 = hashlib.sha256(proof.encode("utf-8")).hexdigest()
        statement_sha256 = hashlib.sha256(b"S").hexdigest()
        intent, intent_path = claude_core._begin_publication_finalization(
            problem_id="category/problem",
            statement_sha256=statement_sha256,
            blueprint_sha256=proof_sha256,
        )

        def crash_after_dispatch(commit_dispatch: Callable[[], None]) -> object:
            commit_dispatch()
            raise SimulatedPowerLoss

        with pytest.raises(SimulatedPowerLoss):
            claude_core._execute_publication_finalization_verifier(
                intent=intent,
                intent_path=intent_path,
                verifier=crash_after_dispatch,
            )
        if restart_entry == "core":
            publication = claude_core.verify_blueprint(
                "category/problem", statement_sha256
            )
        else:
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    str(Path(claude_core.__file__)),
                    "--get-publication",
                    "category/problem",
                    statement_sha256,
                ],
            )
            claude_core.main()
            publication = json.loads(capsys.readouterr().out)

    assert publication is not None
    assert publication["published"] is True
    if restart_entry in {"core", "cli"}:
        assert publication["status"] == "published"
        assert publication["publication_schema"] == "rethlas-publication-v6"
        settlement_path = intent_path.parent / "settlement.json"
        assert settlement_path.is_file()
        assert json.loads(settlement_path.read_text(encoding="utf-8"))[
            "status"
        ] == "published"
    else:
        assert publication["recovered_prepared_publication"] is True
    assert not verified_path.is_symlink()
    assert verified_path.read_text(encoding="utf-8") == proof
    if verified_target == "symlink":
        assert symlink_target.read_text(encoding="utf-8") == proof
    assert posts == 2


def test_receipt_first_recovery_rejects_changed_stale_target_without_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_root = tmp_path / "results"
    result_dir = results_root / "problem"
    result_dir.mkdir(parents=True)
    proof = (
        "# theorem main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nS\n\n## proof\nP\n"
    )
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    verified = result_dir / "blueprint_verified.md"
    verified.write_text("stale A", encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "problem.md").write_text("S", encoding="utf-8")
    receipts_root = tmp_path / "receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.setattr(
        generation_server,
        "_reasoning_phase_preflight",
        lambda _tool: {
            "tool_permitted": True,
            "hard_stop_at_utc": "2099-01-01T00:00:00+00:00",
        },
    )
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_atomic_replace = client._conditional_replace_at

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_before_verified(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        if filename == "blueprint_verified.md":
            raise SimulatedPowerLoss
        return real_atomic_replace(directory_fd, filename, content, **kwargs)

    monkeypatch.setattr(client, "_conditional_replace_at", crash_before_verified)
    with pytest.raises(SimulatedPowerLoss):
        generation_server.verify_blueprint_service(
            problem_id="problem", endpoint="https://verifier/verify"
        )
    assert posts == 2
    verified.write_text("stale B", encoding="utf-8")
    monkeypatch.setattr(client, "_conditional_replace_at", real_atomic_replace)
    rejected = generation_server.verify_blueprint_service(
        problem_id="problem", endpoint="https://verifier/verify"
    )
    replayed = generation_server.verify_blueprint_service(
        problem_id="problem", endpoint="https://verifier/verify"
    )
    assert rejected == replayed
    assert rejected["published"] is False
    assert (
        rejected["publication_blocked_reason"]
        == "prepared_target_collision"
    )
    assert posts == 2
    assert verified.read_text(encoding="utf-8") == "stale B"
    settlements = list(
        receipts_root.glob(".rethlas-prepared-publication-*.settlement.json")
    )
    assert len(settlements) == 1
    assert json.loads(settlements[0].read_text(encoding="utf-8"))["status"] == (
        "not_published"
    )

    from agents import claude_core

    monkeypatch.setattr(claude_core, "GENERATION_ROOT", tmp_path)
    monkeypatch.setattr(claude_core, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_MODULE", None)
    monkeypatch.setattr(claude_core, "_PROOF_CONTEXT_SHA256", None)
    monkeypatch.setattr(
        claude_core,
        "_LEGACY_MODULE",
        type(
            "LegacyStub",
            (),
            {
                "VERIFY_PROOF_URL": "https://verifier/verify",
                "verify_blueprint_file": staticmethod(
                    client.verify_blueprint_file
                ),
            },
        ),
    )
    statement_sha256 = hashlib.sha256(b"S").hexdigest()
    intent, intent_path = claude_core._begin_publication_finalization(
        problem_id="problem",
        statement_sha256=statement_sha256,
        blueprint_sha256=hashlib.sha256(proof.encode("utf-8")).hexdigest(),
    )

    def crash_after_core_dispatch(commit_dispatch: Callable[[], None]) -> object:
        commit_dispatch()
        raise SimulatedPowerLoss

    with pytest.raises(SimulatedPowerLoss):
        claude_core._execute_publication_finalization_verifier(
            intent=intent,
            intent_path=intent_path,
            verifier=crash_after_core_dispatch,
        )
    core_replayed = claude_core.verify_blueprint("problem", statement_sha256)
    assert core_replayed["published"] is False
    assert core_replayed["publication_blocked_reason"] == (
        "prepared_target_collision"
    )
    assert json.loads(
        (intent_path.parent / "settlement.json").read_text(encoding="utf-8")
    )["status"] == "not_published"
    assert posts == 2

    changed_proof = proof.replace("## proof\nP", "## proof\nP2")
    (result_dir / "blueprint.md").write_text(changed_proof, encoding="utf-8")
    publication = generation_server.verify_blueprint_service(
        problem_id="problem", endpoint="https://verifier/verify"
    )
    assert publication["published"] is True
    assert posts == 4
    assert verified.read_text(encoding="utf-8") == changed_proof


def test_receipt_first_draft_drift_settles_then_allows_new_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = (
        "# theorem main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nS\n\n## proof\nP\n"
    )
    changed_proof = proof.replace("## proof\nP", "## proof\nP2")
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_atomic_replace = client._conditional_replace_at

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_before_verified(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        if filename == verified.name:
            raise SimulatedPowerLoss
        return real_atomic_replace(directory_fd, filename, content, **kwargs)

    monkeypatch.setattr(client, "_conditional_replace_at", crash_before_verified)
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert posts == 2
    draft.write_text(changed_proof, encoding="utf-8")
    monkeypatch.setattr(client, "_conditional_replace_at", real_atomic_replace)

    rejected = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert rejected["published"] is False
    assert rejected["publication_blocked_reason"] == "prepared_request_drift"
    assert posts == 2
    publication = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert publication["published"] is True
    assert posts == 4
    assert verified.read_text(encoding="utf-8") == changed_proof


@pytest.mark.parametrize(
    ("corruption", "error"),
    [
        ("passes", "prepared publication verifier pass mismatch"),
        ("attestation", "prepared publication item attestation mismatch"),
    ],
)
def test_receipt_first_drift_does_not_settle_invalid_verifier_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    error: str,
) -> None:
    proof = (
        "# theorem main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\nS\n\n## proof\nP\n"
    )
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    real_atomic_replace = client._conditional_replace_at

    class SimulatedPowerLoss(BaseException):
        pass

    def crash_before_verified(
        directory_fd: int, filename: str, content: bytes, **kwargs: Any
    ) -> tuple[int, int] | None:
        if filename == verified.name:
            raise SimulatedPowerLoss
        return real_atomic_replace(directory_fd, filename, content, **kwargs)

    monkeypatch.setattr(client, "_conditional_replace_at", crash_before_verified)
    with pytest.raises(SimulatedPowerLoss):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    durable_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    if corruption == "passes":
        durable_receipt["verification_passes"] = [{}, {}]
    else:
        durable_receipt["item_context_attestations"][0] = None
    receipt.write_bytes(client._canonical_json_line_bytes(durable_receipt))
    draft.write_text(proof.replace("## proof\nP", "## proof\nP2"), encoding="utf-8")
    monkeypatch.setattr(client, "_conditional_replace_at", real_atomic_replace)

    with pytest.raises(ValueError, match=error):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            receipt_path=receipt,
            problem_id="problem",
        )
    assert posts == 2
    assert not list(
        receipt.parent.glob(
            ".rethlas-prepared-publication-*.settlement.json"
        )
    )


def test_mcp_wrapper_structured_target_excludes_problem_document_wrappers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_root = tmp_path / "results"
    result_dir = results_root / "category" / "wrapped"
    result_dir.mkdir(parents=True)
    mathematical_target = r"Let $x\in\mathbb R$. Prove that $x^2\geq0$."
    problem_document = f"""# Display title

{mathematical_target}

## Retrieval restriction
This run is offline.
"""
    proof = (
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        f"## statement\n{mathematical_target}\n\n"
        "## proof\nA real square is nonnegative.\n"
    )
    draft = result_dir / "blueprint.md"
    draft.write_text(proof, encoding="utf-8")
    data_root = tmp_path / "data"
    problem_source = data_root / "category" / "wrapped.md"
    problem_source.parent.mkdir(parents=True)
    problem_source.write_text(problem_document, encoding="utf-8")
    receipts_root = tmp_path / "trusted-receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    hard_stop = "2099-01-01T00:00:00+00:00"
    monkeypatch.setattr(
        generation_server,
        "_reasoning_phase_preflight",
        lambda tool_name: {
            "tool_permitted": True,
            "hard_stop_at_utc": hard_stop,
        },
    )

    def verifier(endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        assert request["statement"] == problem_document
        return valid_payload(
            request["proof"],
            statement=request["statement"],
        )

    install_post(monkeypatch, verifier)
    result = generation_server.verify_blueprint_service(
        problem_id="category/wrapped",
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    receipt_path = receipts_root / "category" / "wrapped.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "rethlas-publication-v6"
    assert receipt["publication_target_precondition"]["kind"] == "absent"
    assert receipt["proof_context"]["schema_version"] == (
        client.PUBLICATION_PROOF_CONTEXT_SCHEMA
    )
    assert receipt["verification_limits"]["max_receipt_bytes"] == (
        client.MAX_PUBLICATION_RECEIPT_BYTES
    )
    assert receipt["verification_limits"]["max_blueprint_bytes"] == (
        client.MAX_BLUEPRINT_BYTES
    )
    assert receipt["verification_limits"]["max_blueprint_chars"] == (
        client.MAX_BLUEPRINT_CHARS
    )
    assert receipt["statement_source_digest"] == hashlib.sha256(
        problem_document.encode("utf-8")
    ).hexdigest()
    assert receipt["canonical_target_digest"] == hashlib.sha256(
        mathematical_target.encode("utf-8")
    ).hexdigest()
    assert receipt["verification_quorum"] == 2
    assert len(receipt["verification_passes"]) == 2
    assert receipt["checked_item_ids"] == list(
        client.parse_blueprint(
            proof,
            target_statement=problem_document,
        ).item_ids
    )


def test_mcp_wrapper_returns_actionable_retryable_blueprint_preflight_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_root = tmp_path / "results"
    result_dir = results_root / "category" / "problem"
    result_dir.mkdir(parents=True)
    target = "Prove the exact target."
    proof = (
        "# theorem thm:main\n\n"
        "<!-- rethlas-depends-on: -->\n"
        "## statement\n"
        "Prove the exact target. Precisely, here is an equivalent expansion.\n\n"
        "## proof\nComplete proof.\n"
    )
    (result_dir / "blueprint.md").write_text(proof, encoding="utf-8")
    data_root = tmp_path / "data"
    source = data_root / "category" / "problem.md"
    source.parent.mkdir(parents=True)
    source.write_text(target, encoding="utf-8")
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(
        generation_server, "RECEIPTS_ROOT", tmp_path / "trusted-receipts"
    )
    monkeypatch.setattr(
        generation_server, "_reasoning_phase_preflight", lambda _tool: None
    )
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("verifier must not be contacted"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be contacted"),
    )

    result = generation_server.verify_blueprint_service(
        problem_id="category/problem",
        endpoint="https://verifier/verify",
    )

    assert result == {
        "schema_version": "rethlas_blueprint_preflight_failure_v1",
        "status": "preflight_failed",
        "category": "blueprint_contract",
        "operation": "verify_blueprint_service",
        "problem_id": "category/problem",
        "error": "the final proof-item statement must exactly match target_statement",
        "repair_hint": (
            "Repair blueprint.md to use paper-like H1 proof items with one "
            "explicit rethlas-depends-on comment each. The final item's "
            "## statement must exactly equal the canonical problem target; "
            "put explanations, qualifications, and paraphrases in ## proof."
        ),
        "retry_allowed": True,
        "verifier_dispatched": False,
        "published": False,
    }
    assert not (result_dir / "blueprint_verified.md").exists()


def test_publication_requires_two_distinct_correct_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    attempt_ids: list[str] = []

    def verifier(endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        attempt_ids.append(request["verification_attempt_id"])
        return valid_payload(
            request["proof"],
            verdict="correct" if len(attempt_ids) == 1 else "wrong",
        )

    install_post(monkeypatch, verifier)
    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert result["verdict"] == "wrong"
    assert len(attempt_ids) == 2
    assert len(set(attempt_ids)) == 2
    assert [entry["verdict"] for entry in result["verification_passes"]] == [
        "correct",
        "wrong",
    ]
    assert not verified.exists()


@pytest.mark.parametrize("malformed_pass", [1, 2])
def test_concrete_malformed_verifier_response_is_terminal_nonpublication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_pass: int,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    posts = 0

    def verifier(_endpoint: str, request: dict[str, Any]) -> object:
        nonlocal posts
        posts += 1
        if request["verification_pass_index"] == malformed_pass:
            return {"malformed": True}
        return valid_payload(request["proof"])

    install_post(monkeypatch, verifier)
    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert result["verdict"] == "wrong"
    assert result["verification_status"] == "final"
    assert result["publication_blocked_reason"] == "invalid_verifier_response"
    assert result["invalid_verifier_pass_index"] == malformed_pass
    assert len(result["verification_passes"]) == malformed_pass - 1
    assert posts == malformed_pass
    assert not verified.exists()


def test_claude_output_limit_is_preserved_as_operational_nonpublication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(monkeypatch, lambda endpoint, request: valid_payload(proof))
    item_id = client.parse_blueprint(
        proof, target_statement="S"
    ).item_ids[0]

    class ClaudeOutputLimit(FakeResponse):
        status_code = 503

        def raise_for_status(self) -> None:
            pytest.fail("recognized Claude output limit must remain operational")

    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: ClaudeOutputLimit(
            {
                "detail": {
                    "code": "claude_max_output_tokens",
                    "adapter": "claude_cli",
                    "item_id": item_id,
                    "max_output_tokens": 128000,
                }
            }
        ),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert result["verification_status"] == "operational_failed"
    assert result["publication_blocked_reason"] == (
        "operational_verifier_failure"
    )
    assert result["operational_failure_code"] == "claude_max_output_tokens"
    assert result["operational_failure_output_token_limit"] == 128000
    assert result["operational_failure_item_id"] == item_id
    assert not verified.exists()


def test_claude_structured_output_exhaustion_is_operational_nonpublication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(monkeypatch, lambda endpoint, request: valid_payload(proof))
    item_id = client.parse_blueprint(
        proof, target_statement="S"
    ).item_ids[0]

    class ClaudeStructuredOutputFailure(FakeResponse):
        status_code = 503

        def raise_for_status(self) -> None:
            pytest.fail("recognized structured-output failure is operational")

    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: ClaudeStructuredOutputFailure(
            {
                "detail": {
                    "code": "claude_structured_output_retry_exhausted",
                    "adapter": "claude_cli",
                    "item_id": item_id,
                    "structured_output_attempts": 1,
                }
            }
        ),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert result["verification_status"] == "operational_failed"
    assert result["publication_blocked_reason"] == (
        "operational_verifier_failure"
    )
    assert result["operational_failure_code"] == (
        "claude_structured_output_retry_exhausted"
    )
    assert result["operational_failure_structured_output_attempts"] == 1
    assert result["operational_failure_item_id"] == item_id
    assert not verified.exists()


def test_claude_raw_json_failure_is_operational_nonpublication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(monkeypatch, lambda endpoint, request: valid_payload(proof))
    item_id = client.parse_blueprint(
        proof, target_statement="S"
    ).item_ids[0]

    class ClaudeRawJsonFailure(FakeResponse):
        status_code = 503

        def raise_for_status(self) -> None:
            pytest.fail("recognized raw-JSON failure must remain operational")

    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: ClaudeRawJsonFailure(
            {
                "detail": {
                    "code": "claude_json_output_invalid",
                    "adapter": "claude_cli",
                    "item_id": item_id,
                    "output_contract": "raw_json_v1",
                }
            }
        ),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert result["verification_status"] == "operational_failed"
    assert result["publication_blocked_reason"] == (
        "operational_verifier_failure"
    )
    assert result["operational_failure_code"] == "claude_json_output_invalid"
    assert result["operational_failure_output_contract"] == "raw_json_v1"
    assert result["operational_failure_item_id"] == item_id
    assert not verified.exists()


@pytest.mark.parametrize("malformed_pass", [1, 2])
def test_non_utf8_verifier_string_is_terminal_nonpublication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_pass: int,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")

    def verifier(_endpoint: str, request: dict[str, Any]) -> object:
        payload = valid_payload(request["proof"])
        if request["verification_pass_index"] == malformed_pass:
            payload["verification_report"]["summary"] = "\ud800"
        return payload

    install_post(monkeypatch, verifier)
    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert result["publication_blocked_reason"] == "invalid_verifier_response"
    assert result["invalid_verifier_pass_index"] == malformed_pass
    assert not verified.exists()


@pytest.mark.parametrize("malformed_pass", [1, 2])
def test_recursive_json_response_is_durable_terminal_nonpublication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_pass: int,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda _endpoint, request: valid_payload(request["proof"]),
    )
    healthy_post = client.requests.post
    posts = 0

    class RecursiveJsonResponse(FakeResponse):
        def json(self) -> object:
            raise RecursionError("synthetic deeply nested JSON")

    def recursive_post(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal posts
        posts += 1
        request = kwargs["json"]
        if request["verification_pass_index"] == malformed_pass:
            return RecursiveJsonResponse(None)
        return healthy_post(*args, **kwargs)

    monkeypatch.setattr(client.requests, "post", recursive_post)
    first = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert first["published"] is False
    assert first["publication_blocked_reason"] == "invalid_verifier_response"
    assert first["invalid_verifier_pass_index"] == malformed_pass
    assert posts == malformed_pass

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed == first
    assert posts == malformed_pass


def test_identical_proof_reentry_keeps_pass_identities_stable_across_http_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(monkeypatch, lambda endpoint, request: valid_payload(request["proof"]))
    observed: list[dict[str, Any]] = []

    class Http500(FakeResponse):
        status_code = 500

        def raise_for_status(self) -> None:
            raise client.requests.HTTPError("simulated verifier HTTP 500")

    def fake_post(
        endpoint: str,
        *,
        json: dict[str, Any],
        timeout: float,
        **kwargs: Any,
    ) -> FakeResponse:
        observed.append(dict(json))
        if (
            json["verification_caller_instance_id"] == "vcaller_" + "a" * 32
            and json["verification_pass_index"] == 2
        ):
            return Http500({"detail": "simulated Vertex api_error"})
        payload = valid_payload(
            json["proof"],
            verdict=(
                "wrong"
                if json["verification_caller_instance_id"]
                == "vcaller_" + "b" * 32
                and json["verification_pass_index"] == 2
                else "correct"
            ),
        )
        payload.update(
            {
                "verification_attempt_id": json["verification_attempt_id"],
                "verifier_run_id": "run:" + json["verification_attempt_id"],
                "verifier_model": "gpt-5.6-sol",
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "test-0.3.0",
                "verification_pass_index": json["verification_pass_index"],
                "verification_role": (
                    "primary"
                    if json["verification_pass_index"] == 1
                    else "adversarial_full_claim_audit"
                ),
            }
        )
        return FakeResponse(payload)

    monkeypatch.setattr(client.requests, "post", fake_post)
    monkeypatch.setattr(
        client, "_VERIFICATION_CALLER_INSTANCE_ID", "vcaller_" + "a" * 32
    )
    terminal = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )
    assert terminal["published"] is False
    assert terminal["publication_blocked_reason"] == "invalid_verifier_response"
    assert terminal["invalid_verifier_pass_index"] == 2
    observed_after_failure = len(observed)
    with pytest.raises(client.VerificationSameTurnRetryForbidden):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
        )
    assert len(observed) == observed_after_failure

    monkeypatch.setattr(
        client, "_VERIFICATION_CALLER_INSTANCE_ID", "vcaller_" + "b" * 32
    )
    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )
    assert result["verdict"] == "wrong"
    assert result["published"] is False
    first_pass_one, first_pass_two, second_pass_one, second_pass_two = observed
    assert all("verification_caller_pid" not in entry for entry in observed)
    assert all(
        "verification_caller_start_sha256" not in entry for entry in observed
    )
    assert first_pass_one["verification_attempt_id"] == second_pass_one[
        "verification_attempt_id"
    ]
    assert first_pass_one["verification_pass_identity"] == second_pass_one[
        "verification_pass_identity"
    ]
    assert first_pass_two["verification_attempt_id"] == second_pass_two[
        "verification_attempt_id"
    ]
    assert first_pass_two["verification_pass_identity"] == second_pass_two[
        "verification_pass_identity"
    ]
    assert first_pass_one["verification_attempt_id"] != first_pass_two[
        "verification_attempt_id"
    ]
    assert first_pass_one["verification_caller_instance_id"] != second_pass_one[
        "verification_caller_instance_id"
    ]
    assert not verified.exists()


def test_publication_rejects_two_receipts_from_one_verifier_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")

    def fake_post(
        endpoint: str,
        *,
        json: dict[str, Any],
        timeout: float,
        **kwargs: Any,
    ) -> FakeResponse:
        payload = valid_payload(json["proof"])
        payload.update(
            {
                "verification_attempt_id": json["verification_attempt_id"],
                "verifier_run_id": "reused-verifier-run",
                "verifier_model": "gpt-5.6-sol",
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "test-0.3.0",
                "verification_pass_index": json["verification_pass_index"],
                "verification_role": (
                    "primary"
                    if json["verification_pass_index"] == 1
                    else "adversarial_full_claim_audit"
                ),
            }
        )
        return FakeResponse(payload)

    install_post(monkeypatch, lambda endpoint, request: valid_payload(request["proof"]))
    monkeypatch.setattr(client.requests, "post", fake_post)
    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert result["published"] is False
    assert result["publication_blocked_reason"] == (
        "verifier_quorum_not_independent"
    )
    assert len(result["verification_passes"]) == 2
    assert not verified.exists()

    monkeypatch.setattr(
        client.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("profile must not be fetched"),
    )
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("verifier must not be redispatched"),
    )
    replayed = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt,
        problem_id="problem",
    )
    assert replayed == result


def test_publication_records_distinct_codex_and_claude_verifier_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    receipt_path = tmp_path / "receipt.json"
    draft.write_text(proof, encoding="utf-8")

    def fake_post(
        endpoint: str,
        *,
        json: dict[str, Any],
        timeout: float,
        **kwargs: Any,
    ) -> FakeResponse:
        pass_index = json["verification_pass_index"]
        payload = valid_payload(json["proof"])
        payload.update(
            {
                "verification_attempt_id": json["verification_attempt_id"],
                "verifier_run_id": f"diverse-verifier-run-{pass_index}",
                "verifier_model": (
                    "gpt-5.6-sol" if pass_index == 1 else "claude-opus-5"
                ),
                "verifier_reasoning_effort": "max",
                "verifier_service_version": "test-0.4.0",
                "verification_pass_index": pass_index,
                "verification_role": (
                    "primary"
                    if pass_index == 1
                    else "adversarial_full_claim_audit"
                ),
            }
        )
        return FakeResponse(payload)

    monkeypatch.setattr(client.requests, "post", fake_post)

    def fake_get(
        endpoint: str, *, timeout: float, **kwargs: Any
    ) -> FakeResponse:
        assert endpoint == "https://verifier/profile"
        return FakeResponse(
            {
                "schema_version": "rethlas_verifier_profile_v1",
                "service_version": "test-0.4.0",
                "profile": "max_diversity",
                "passes": [
                    {
                        "pass_index": 1,
                        "adapter": "codex_cli",
                        "provider": "openai",
                        "model": "gpt-5.6-sol",
                        "launch_model": "gpt-5.6-sol",
                        "reasoning_effort": "max",
                        "session_mode": "cold",
                    },
                    {
                        "pass_index": 2,
                        "adapter": "claude_cli",
                        "provider": "vertex",
                        "model": "claude-opus-5",
                        "launch_model": "claude-opus-5[1m]",
                        "reasoning_effort": "max",
                        "session_mode": "cold",
                    },
                ],
                "automatic_tiebreaker": False,
                "fallback_policy": "forbid",
            }
        )

    monkeypatch.setattr(client.requests, "get", fake_get)
    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        receipt_path=receipt_path,
        problem_id="diverse-proof",
        verification_profile="max_diversity",
    )

    assert result["published"] is True
    receipt = json.loads(receipt_path.read_text())
    assert [
        verification_pass["verifier_model"]
        for verification_pass in receipt["verification_passes"]
    ] == ["gpt-5.6-sol", "claude-opus-5"]
    assert [
        verification_pass["verifier_reasoning_effort"]
        for verification_pass in receipt["verification_passes"]
    ] == ["max", "max"]


def test_max_diversity_profile_mismatch_starts_zero_verifier_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(
        "# theorem main\n\n## statement\nS\n\n## proof\nP\n",
        encoding="utf-8",
    )
    post_calls = 0

    def forbidden_post(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal post_calls
        post_calls += 1
        raise AssertionError("profile mismatch must precede verifier POST")

    def wrong_profile(*args: Any, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {
                "schema_version": "rethlas_verifier_profile_v1",
                "service_version": "test-0.4.0",
                "profile": "compatible",
                "passes": [],
                "automatic_tiebreaker": False,
                "fallback_policy": "forbid",
            }
        )

    monkeypatch.setattr(client.requests, "post", forbidden_post)
    monkeypatch.setattr(client.requests, "get", wrong_profile)
    with pytest.raises(ValueError, match="profile binding mismatch"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            verification_profile="max_diversity",
        )
    assert post_calls == 0
    assert not verified.exists()


def test_profile_failure_before_dispatch_retries_in_same_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda _endpoint, request: valid_payload(
            request["proof"], verdict="wrong"
        ),
    )
    healthy_get = client.requests.get
    get_calls = 0
    posts = 0
    dispatches = 0
    healthy_post = client.requests.post

    def flaky_get(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            raise client.requests.HTTPError("synthetic profile failure")
        return healthy_get(*args, **kwargs)

    def observed_post(*args: Any, **kwargs: Any) -> FakeResponse:
        nonlocal posts
        posts += 1
        return healthy_post(*args, **kwargs)

    def commit_dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    monkeypatch.setattr(client.requests, "get", flaky_get)
    monkeypatch.setattr(client.requests, "post", observed_post)
    with pytest.raises(client.requests.HTTPError, match="profile failure"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
            on_verifier_dispatch=commit_dispatch,
        )
    assert posts == 0
    assert dispatches == 0

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
        on_verifier_dispatch=commit_dispatch,
    )
    assert result["published"] is False
    assert posts == 1
    assert dispatches == 1


@pytest.mark.parametrize("replace_results_root", [False, True])
def test_mcp_wrapper_rejects_symlinks_at_every_results_path_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_results_root: bool,
) -> None:
    generation_root = tmp_path / "generation"
    results_root = generation_root / "results"
    outside_root = tmp_path / "outside-results"
    outside_problem = outside_root / "category" / "problem"
    outside_problem.mkdir(parents=True)
    (outside_problem / "blueprint.md").write_text(
        "outside candidate proof",
        encoding="utf-8",
    )
    if replace_results_root:
        generation_root.mkdir()
        results_root.symlink_to(outside_root, target_is_directory=True)
    else:
        results_root.mkdir(parents=True)
        (results_root / "category").symlink_to(
            outside_root / "category",
            target_is_directory=True,
        )

    data_root = generation_root / "data"
    problem_source = data_root / "category" / "problem.md"
    problem_source.parent.mkdir(parents=True)
    problem_source.write_text("S", encoding="utf-8")
    receipts_root = tmp_path / "trusted-receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="non-symlink|non-symlink directories"):
        generation_server.verify_blueprint_service(
            problem_id="category/problem",
            endpoint="https://verifier/verify",
        )

    assert not (outside_problem / "blueprint_verified.md").exists()
    assert not (receipts_root / "category" / "problem.json").exists()


def test_mcp_wrapper_rejects_problem_changed_after_runner_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "problem.md").write_text("changed target", encoding="utf-8")
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "problem")
    monkeypatch.setenv("RETHLAS_EXPECTED_STATEMENT_SHA256", "0" * 64)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="changed after the runner bound"):
        generation_server.verify_blueprint_service(problem_id="problem")


def test_mcp_production_wrapper_rejects_parent_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", tmp_path / "results")
    with pytest.raises(ValueError, match=r"\.\."):
        generation_server.verify_blueprint_service(
            problem_id="../../outside",
        )
