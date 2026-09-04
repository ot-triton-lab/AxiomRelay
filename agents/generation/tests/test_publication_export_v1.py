from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agents import claude_core
from agents.generation.mcp import publication_export_v1 as export
from agents.generation.mcp import publication_proof_context_v3 as proof_context

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPO_ROOT
    / "agents"
    / "generation"
    / "mcp"
    / "axiomgraph_source_interface_v1.json"
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fixture() -> tuple[
    bytes,
    bytes,
    object,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    statement = b"T\n"
    blueprint = (
        b"# lemma lem:base\n\n"
        b"<!-- rethlas-depends-on: -->\n"
        b"## statement\nP\n\n"
        b"## proof\nA verified base proof.\n\n"
        b"# theorem thm:target\n\n"
        b"<!-- rethlas-depends-on: lem:base -->\n"
        b"## statement\nT\n\n"
        b"## proof\nThe verified bridge from P.\n"
    )
    parsed = proof_context.parse_blueprint(
        blueprint.decode("utf-8"), target_statement=statement.decode("utf-8")
    )
    stable_verification_passes = [
        {
            "pass_index": 1,
            "verification_role": "primary",
            "verifier_model": "verifier-a",
            "verifier_reasoning_effort": "high",
            "verifier_service_version": "test-v1",
        },
        {
            "pass_index": 2,
            "verification_role": "adversarial_full_claim_audit",
            "verifier_model": "verifier-b",
            "verifier_reasoning_effort": "high",
            "verifier_service_version": "test-v1",
        },
    ]
    receipt_verification_passes = [
        {**verification_pass, "verdict": "correct"}
        for verification_pass in stable_verification_passes
    ]
    receipt: dict[str, object] = {
        "schema_version": "rethlas-publication-v6",
        "state": "active",
        "problem_id": "test/problem",
        "statement_source_digest": _sha256(statement),
        "canonical_target_digest": _sha256(b"T"),
        "proof_digest": _sha256(blueprint),
        "checked_item_ids": list(parsed.item_ids),
        "proof_context": {
            "schema_version": "rethlas_publication_proof_context_v3",
            "source_sha256": "1" * 64,
        },
        "verification_limits": {"max_proof_items": 10},
        "verification_passes": receipt_verification_passes,
        "verification_quorum": 2,
    }
    normalized_manifest = {
        "schema_version": export.PROOF_MANIFEST_SCHEMA,
        "proof_digest": parsed.proof_digest,
        "source_kind": parsed.source_kind,
        "topological_item_ids": list(parsed.topological_item_ids),
        "items": [
            {
                "index": item.index,
                "item_id": item.item_id,
                "artifact_sha256": item.digest,
                "title": item.title,
                "label": item.label,
                "statement": item.statement,
                "depends_on": list(item.depends_on),
                "dependency_mode": item.dependency_mode,
            }
            for item in parsed.items
        ],
    }
    stable_profile = {
        "proof_context": receipt["proof_context"],
        "verification_limits": receipt["verification_limits"],
        "verification_passes": stable_verification_passes,
        "verification_quorum": 2,
    }
    runtime = {
        "loaded_claude_core_sha256": "a" * 64,
        "publication_export_module_sha256": "b" * 64,
        "interface_manifest_sha256": "c" * 64,
        "runtime_dependency_manifest_sha256": "d" * 64,
    }
    return statement, blueprint, parsed, receipt, normalized_manifest, {
        "profile": stable_profile,
        "runtime": runtime,
    }


def _event() -> tuple[dict[str, object], dict[str, object]]:
    statement, blueprint, _parsed, receipt, normalized, extras = _fixture()
    manifest = export.load_interface_manifest(MANIFEST_PATH.read_bytes())
    receipt_sha256 = _sha256(export.canonical_bytes(receipt) + b"\n")
    event = export.make_verified_publication_event(
        interface_manifest=manifest,
        source={
            "authority_id": "rethlas-publication-v6",
            "terminal_outcome": "published_verified",
            "problem_id": "test/problem",
            "statement_sha256": _sha256(statement),
            "canonical_target_sha256": _sha256(b"T"),
            "blueprint_sha256": _sha256(blueprint),
            "publication_receipt_sha256": receipt_sha256,
        },
        exact_target_raw=statement,
        exact_blueprint_raw=blueprint,
        publication_receipt=receipt,
        proof_manifest=normalized,
        stable_verifier_profile=extras["profile"],
        source_runtime=extras["runtime"],
    )
    return event, manifest


def _reidentify(event: dict[str, object]) -> None:
    event["source"]["publication_receipt_sha256"] = _sha256(
        export.canonical_bytes(event["publication_receipt"]) + b"\n"
    )
    payload = {key: value for key, value in event.items() if key != "event_id"}
    event["event_id"] = "arev_" + _sha256(
        export.EVENT_ID_DOMAIN + export.canonical_bytes(payload)
    )


def test_interface_manifest_is_canonical_and_minor_compatible() -> None:
    raw = MANIFEST_PATH.read_bytes()
    manifest = export.load_interface_manifest(raw)
    assert raw == export.canonical_bytes(manifest) + b"\n"
    assert manifest["interface_major"] == 1
    assert manifest["interface_minor"] == 0
    assert manifest["minimum_consumer_minor"] == 0
    assert manifest["required_capabilities"] == [
        "verified_publication_event_v1"
    ]
    assert manifest["optional_capabilities"] == []
    assert manifest["event_store_relative_path"] == (
        "agents/.claude_core/axiomgraph_exports/v1/publications"
    )


def test_frozen_cross_repository_protocol_corpus() -> None:
    corpus = json.loads(
        (Path(__file__).parent / "fixtures" / "axiomgraph_source_interface_v1.json")
        .read_text(encoding="utf-8")
    )
    manifest = export.load_interface_manifest(
        export.canonical_bytes(corpus["interface_manifest"]) + b"\n"
    )
    event = corpus["event"]
    export.validate_verified_publication_event(
        event, interface_manifest=manifest,
        expected_source_runtime=event["source_runtime"],
    )
    current, _manifest = _event()
    reproduced = export.make_verified_publication_event(
        interface_manifest=manifest, source=current["source"],
        exact_target_raw=base64.b64decode(current["exact_target"]["content_base64"]),
        exact_blueprint_raw=base64.b64decode(current["exact_blueprint"]["content_base64"]),
        publication_receipt=current["publication_receipt"],
        proof_manifest=current["proof_manifest"],
        stable_verifier_profile=current["stable_verifier_profile"],
        source_runtime=event["source_runtime"],
    )
    assert reproduced == event
    assert event["source_runtime"]["interface_manifest_sha256"] == _sha256(
        export.canonical_bytes(manifest) + b"\n"
    )
    assert _sha256(export.canonical_bytes(event) + b"\n") == corpus["event_sha256"]
    assert _sha256(
        export.canonical_bytes(corpus["runtime_dependency_manifest"]) + b"\n"
    ) == corpus["runtime_dependency_manifest_sha256"]
    assert event["source_runtime"]["runtime_dependency_manifest_sha256"] == corpus["runtime_dependency_manifest_sha256"]
    assert event["source_runtime"]["publication_export_module_sha256"] == corpus["runtime_dependency_manifest"]["files"]["mcp/publication_export_v1.py"]


def test_event_is_domain_bound_canonical_and_idempotent(tmp_path: Path) -> None:
    event, manifest = _event()
    runtime = event["source_runtime"]
    receipt = export.write_verified_publication_event(
        event=event,
        interface_manifest=manifest,
        expected_source_runtime=runtime,
        event_store=tmp_path / "exports",
    )
    replay = export.write_verified_publication_event(
        event=event,
        interface_manifest=manifest,
        expected_source_runtime=runtime,
        event_store=tmp_path / "exports",
    )
    assert replay == receipt
    path = Path(receipt["path"])
    raw = path.read_bytes()
    assert raw == export.canonical_bytes(event) + b"\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert receipt["event_sha256"] == _sha256(raw)
    assert base64.b64decode(event["exact_target"]["content_base64"]) == b"T\n"
    payload = {key: value for key, value in event.items() if key != "event_id"}
    assert event["event_id"] == "arev_" + _sha256(
        export.EVENT_ID_DOMAIN + export.canonical_bytes(payload)
    )


def test_event_write_recovers_interrupted_post_link_publication(
    tmp_path: Path,
) -> None:
    event, manifest = _event()
    runtime = event["source_runtime"]
    raw = export.canonical_bytes(event) + b"\n"
    event_store = tmp_path / "exports"
    event_store.mkdir(mode=0o700)
    receipt_directory = event_store / event["source"][
        "publication_receipt_sha256"
    ]
    receipt_directory.mkdir(mode=0o700)
    event_directory = receipt_directory / event["event_id"]
    event_directory.mkdir(mode=0o700)
    path = event_directory / "event.json"
    interrupted_temporary = event_directory / (
        export._temporary_prefix(path) + "interrupted"
    )
    descriptor = os.open(
        interrupted_temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o400,
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.link(interrupted_temporary, path)
    assert path.stat().st_nlink == 2

    receipt = export.write_verified_publication_event(
        event=event,
        interface_manifest=manifest,
        expected_source_runtime=runtime,
        event_store=event_store,
    )

    assert Path(receipt["path"]) == path
    assert path.read_bytes() == raw
    assert path.stat().st_nlink == 1
    assert not interrupted_temporary.exists()
    assert [candidate.name for candidate in event_directory.iterdir()] == [
        "event.json"
    ]


def test_concurrent_event_writers_converge_without_temporary_files(
    tmp_path: Path,
) -> None:
    event, manifest = _event()
    runtime = event["source_runtime"]
    event_store = tmp_path / "exports"
    barrier = threading.Barrier(8)

    def publish(_index: int) -> dict[str, str]:
        barrier.wait(timeout=10)
        return export.write_verified_publication_event(
            event=event,
            interface_manifest=manifest,
            expected_source_runtime=runtime,
            event_store=event_store,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(publish, range(8)))

    assert all(receipt == receipts[0] for receipt in receipts)
    event_path = Path(receipts[0]["path"])
    assert event_path.read_bytes() == export.canonical_bytes(event) + b"\n"
    assert event_path.stat().st_nlink == 1
    assert [candidate.name for candidate in event_path.parent.iterdir()] == [
        "event.json"
    ]


def test_event_rejects_tampering_and_wrong_runtime() -> None:
    event, manifest = _event()
    tampered = copy.deepcopy(event)
    tampered["exact_target"]["content_base64"] = base64.b64encode(b"X\n").decode()
    with pytest.raises(export.PublicationExportError, match="content binding"):
        export.validate_verified_publication_event(
            tampered,
            interface_manifest=manifest,
            expected_source_runtime=event["source_runtime"],
        )
    wrong_runtime = dict(event["source_runtime"])
    wrong_runtime["loaded_claude_core_sha256"] = "f" * 64
    with pytest.raises(export.PublicationExportError, match="runtime binding"):
        export.validate_verified_publication_event(
            event,
            interface_manifest=manifest,
            expected_source_runtime=wrong_runtime,
        )


def test_event_rejects_non_correct_receipt_verifier_verdict() -> None:
    event, manifest = _event()
    tampered = copy.deepcopy(event)
    tampered["publication_receipt"]["verification_passes"][0][
        "verdict"
    ] = "wrong"
    tampered["source"]["publication_receipt_sha256"] = _sha256(
        export.canonical_bytes(tampered["publication_receipt"]) + b"\n"
    )

    with pytest.raises(
        export.PublicationExportError,
        match="stable verifier pass binding",
    ):
        export.validate_verified_publication_event(
            tampered,
            interface_manifest=manifest,
            expected_source_runtime=event["source_runtime"],
        )


@pytest.mark.parametrize("field,value", [("artifact_sha256", int("1" * 64)), ("index", 0.0)])
def test_event_rejects_ambiguous_proof_item_field_types(field: str, value: object) -> None:
    event, manifest = _event()
    event["proof_manifest"]["items"][0][field] = value
    with pytest.raises(export.PublicationExportError, match="proof manifest item"):
        export.validate_verified_publication_event(
            event, interface_manifest=manifest,
            expected_source_runtime=event["source_runtime"],
        )


@pytest.mark.parametrize("field,value", [("interface_major", True), ("interface_minor", False)])
def test_event_rejects_boolean_interface_versions(field: str, value: bool) -> None:
    event, manifest = _event()
    event["interface"][field] = value
    _reidentify(event)
    with pytest.raises(export.PublicationExportError, match="event interface"):
        export.validate_verified_publication_event(event, interface_manifest=manifest)


def test_event_binds_proof_item_ids_to_artifact_digests() -> None:
    event, manifest = _event()
    event["proof_manifest"]["items"][1]["artifact_sha256"] = (
        event["proof_manifest"]["items"][0]["artifact_sha256"]
    )
    _reidentify(event)
    with pytest.raises(export.PublicationExportError, match="proof manifest item binding"):
        export.validate_verified_publication_event(event, interface_manifest=manifest)


@pytest.mark.parametrize("excess", [0, 1])
def test_exact_target_projection_size_boundary(excess: int) -> None:
    event, manifest = _event()
    raw = b"T\n" + b" " * (export.MAX_EXACT_TARGET_BYTES_V1 - 2 + excess)
    digest = _sha256(raw)
    event["exact_target"]["content_base64"] = base64.b64encode(raw).decode("ascii")
    event["exact_target"]["content_sha256"] = digest
    event["source"]["statement_sha256"] = digest
    event["publication_receipt"]["statement_source_digest"] = digest
    _reidentify(event)
    if excess:
        with pytest.raises(export.PublicationExportError, match="exact target.*size bound"):
            export.validate_verified_publication_event(event, interface_manifest=manifest)
    else:
        export.validate_verified_publication_event(event, interface_manifest=manifest)


@pytest.mark.parametrize("field", ["problem_id", "proof_context"])
@pytest.mark.parametrize("excess", [0, 1])
def test_source_context_projection_size_boundary(field: str, excess: int) -> None:
    event, manifest = _event()
    context = {"problem_id": "test/problem", "proof_context": {"padding": ""}}
    context[field] = ""
    padding_size = (
        export.MAX_PROJECTION_CONTEXT_BYTES_V1
        - len(export.canonical_bytes(context))
        + excess
    )
    context[field] = "x" * padding_size
    event["source"]["problem_id"] = context["problem_id"]
    event["publication_receipt"].update(context)
    event["stable_verifier_profile"]["proof_context"] = context["proof_context"]
    _reidentify(event)
    if excess:
        with pytest.raises(export.PublicationExportError, match="source context.*size bound"):
            export.validate_verified_publication_event(event, interface_manifest=manifest)
    else:
        export.validate_verified_publication_event(event, interface_manifest=manifest)


def test_source_profile_rejects_keys_that_collide_after_normalization() -> None:
    event, manifest = _event()
    context = {"\u00e9": "a", "e\u0301": "b"}
    event["publication_receipt"]["proof_context"] = context
    event["stable_verifier_profile"]["proof_context"] = context
    _reidentify(event)
    with pytest.raises(export.PublicationExportError, match="keys collide"):
        export.validate_verified_publication_event(event, interface_manifest=manifest)


def test_wire_nesting_limit() -> None:
    value: object = None
    for _ in range(256):
        value = [value]
    assert export.canonical_bytes(value)
    with pytest.raises(export.PublicationExportError, match="nesting limit"):
        export.canonical_bytes([value])


def test_event_binds_final_proof_statement_to_canonical_target() -> None:
    event, manifest = _event()
    tampered = copy.deepcopy(event)
    tampered["proof_manifest"]["items"][-1]["statement"] = "not T"

    with pytest.raises(
        export.PublicationExportError,
        match="final statement binding",
    ):
        export.validate_verified_publication_event(
            tampered,
            interface_manifest=manifest,
            expected_source_runtime=event["source_runtime"],
        )


def test_core_emits_source_event_without_importing_axiomgraph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, blueprint, _parsed, receipt, _normalized, _extras = _fixture()
    source = tmp_path / "generation" / "data" / "test" / "problem.md"
    published = (
        tmp_path
        / "generation"
        / "results"
        / "test"
        / "problem"
        / "blueprint_verified.md"
    )
    receipt_path = tmp_path / "receipts" / "test" / "problem.json"
    source.parent.mkdir(parents=True)
    published.parent.mkdir(parents=True)
    receipt_path.parent.mkdir(parents=True)
    source.write_bytes(statement)
    published.write_bytes(blueprint)
    receipt_raw = (claude_core.canonical_json(receipt) + "\n").encode("utf-8")
    receipt_path.write_bytes(receipt_raw)
    statement_sha256 = _sha256(statement)
    receipt_sha256 = _sha256(receipt_raw)
    publication = {
        "status": "published",
        "publication_schema": "rethlas-publication-v6",
        "statement_sha256": statement_sha256,
        "proof_sha256": _sha256(blueprint),
        "published_path": str(published),
        "publication_receipt_path": str(receipt_path),
        "publication_receipt_sha256": receipt_sha256,
    }
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core,
        "_statement",
        lambda _problem_id: (source, statement, statement_sha256),
    )
    result = claude_core._try_export_axiomgraph_publication_event(
        problem_id="test/problem",
        statement_sha256=statement_sha256,
        publication=publication,
    )
    assert result is not None
    event_path = Path(result["path"])
    assert event_path.is_file()
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["source"]["publication_receipt_sha256"] == receipt_sha256
    assert event["source_runtime"]["publication_export_module_sha256"] == (
        _sha256(
            (
                REPO_ROOT
                / "agents"
                / "generation"
                / "mcp"
                / "publication_export_v1.py"
            ).read_bytes()
        )
    )
    assert event["source_runtime"]["interface_manifest_sha256"] == _sha256(
        MANIFEST_PATH.read_bytes()
    )
    assert event["source_runtime"]["loaded_claude_core_sha256"] == _sha256(
        (REPO_ROOT / "agents" / "claude_core.py").read_bytes()
    )
    expected_runtime_manifest = {
        "schema_version": claude_core.RUNTIME_DEPENDENCY_MANIFEST_SCHEMA,
        "files": dict(claude_core.RUNTIME_DEPENDENCY_SHA256),
    }
    assert event["source_runtime"]["runtime_dependency_manifest_sha256"] == (
        _sha256(
            (claude_core.canonical_json(expected_runtime_manifest) + "\n").encode(
                "utf-8"
            )
        )
    )
    export_source = (
        REPO_ROOT / "agents" / "generation" / "mcp" / "publication_export_v1.py"
    ).read_text(encoding="utf-8")
    imported_roots: set[str] = set()
    for node in ast.walk(ast.parse(export_source)):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert "axiomgraph_contract" not in imported_roots


def test_core_export_failure_is_audited_without_changing_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement, blueprint, _parsed, receipt, _normalized, _extras = _fixture()
    source = tmp_path / "problem.md"
    published = tmp_path / "blueprint_verified.md"
    receipt_path = tmp_path / "publication.json"
    source.write_bytes(statement)
    published.write_bytes(blueprint)
    receipt_raw = (claude_core.canonical_json(receipt) + "\n").encode("utf-8")
    receipt_path.write_bytes(receipt_raw)
    statement_sha256 = _sha256(statement)
    receipt_sha256 = _sha256(receipt_raw)
    publication = {
        "status": "published",
        "publication_schema": "rethlas-publication-v6",
        "statement_sha256": statement_sha256,
        "proof_sha256": _sha256(blueprint),
        "published_path": str(published),
        "publication_receipt_path": str(receipt_path),
        "publication_receipt_sha256": receipt_sha256,
    }
    monkeypatch.setattr(claude_core, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(
        claude_core,
        "_statement",
        lambda _problem_id: (source, statement, statement_sha256),
    )
    monkeypatch.setattr(
        claude_core,
        "_publication_export",
        lambda: (_ for _ in ()).throw(
            claude_core.ClaudeCoreError("simulated bounded failure")
        ),
    )
    assert (
        claude_core._try_export_axiomgraph_publication_event(
            problem_id="test/problem",
            statement_sha256=statement_sha256,
            publication=publication,
        )
        is None
    )
    failures = list(
        (
            tmp_path
            / "state"
            / "axiomgraph_exports"
            / "v1"
            / "failures"
            / receipt_sha256
        ).glob("*.json")
    )
    assert len(failures) == 1
    audit = json.loads(failures[0].read_text(encoding="utf-8"))
    assert audit["category"] == "ClaudeCoreError"
    assert audit["diagnostic"] == "simulated bounded failure"
