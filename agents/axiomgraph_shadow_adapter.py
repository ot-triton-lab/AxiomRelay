"""Additive AxiomGraph projection of an already-reconciled publication-v6.

This module is intentionally outside the verifier and publication transaction.
It never changes ProofItem ids, verifier receipts, or publication status.  The
caller must first pass the publication through Claude Core's
``_existing_publication`` authority check; this adapter then rechecks the
projection inputs and writes an immutable, non-authoritative shadow sidecar.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class AxiomGraphProjectionError(RuntimeError):
    """The reconciled AxiomRelay publication cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class PublicationProjectionV1:
    bundle: Any
    selection: Any
    validation: Any
    authentication_receipt: Any
    source_receipt_sha256: str


def _contract() -> Any:
    try:
        import axiomgraph_contract as contract
    except ImportError as exc:
        raise AxiomGraphProjectionError(
            "axiomgraph-contract is not installed in the AxiomRelay runtime"
        ) from exc
    return contract


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _projection_text(value: str) -> str:
    if not isinstance(value, str):
        raise AxiomGraphProjectionError("proof item text is not a string")
    projected = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    projected.encode("utf-8", "strict")
    if not projected:
        raise AxiomGraphProjectionError("proof item statement is empty")
    return projected


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AxiomGraphProjectionError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _stable_verifier_profile(receipt: Mapping[str, Any]) -> dict[str, Any]:
    passes = receipt.get("verification_passes")
    if not isinstance(passes, list) or len(passes) != 2:
        raise AxiomGraphProjectionError("publication does not contain the required verifier quorum")
    stable_passes: list[dict[str, Any]] = []
    for index, verification_pass in enumerate(passes, start=1):
        if not isinstance(verification_pass, dict):
            raise AxiomGraphProjectionError("publication verifier pass is malformed")
        if verification_pass.get("pass_index") != index or verification_pass.get("verdict") != "correct":
            raise AxiomGraphProjectionError("publication verifier pass is not correct")
        stable_passes.append(
            {
                "pass_index": index,
                "verification_role": verification_pass.get("verification_role"),
                "verifier_model": verification_pass.get("verifier_model"),
                "verifier_reasoning_effort": verification_pass.get("verifier_reasoning_effort"),
                "verifier_service_version": verification_pass.get("verifier_service_version"),
            }
        )
    return {
        "proof_context": receipt.get("proof_context"),
        "verification_limits": receipt.get("verification_limits"),
        "verification_passes": stable_passes,
        "verification_quorum": receipt.get("verification_quorum"),
    }


def project_reconciled_publication_v6(
    *,
    problem_id: str,
    statement_raw: bytes,
    blueprint_raw: bytes,
    receipt: Mapping[str, Any],
    publication_receipt_sha256: str,
    proof_context_parser: Any,
) -> PublicationProjectionV1:
    """Build a shadow graph from a publication already checked by AxiomRelay."""

    contract = _contract()
    _require_sha256(publication_receipt_sha256, "publication receipt digest")
    if receipt.get("schema_version") != "rethlas-publication-v6":
        raise AxiomGraphProjectionError("only publication-v6 can enter the shadow authority adapter")
    if receipt.get("state") != "active" or receipt.get("verification_quorum") != 2:
        raise AxiomGraphProjectionError("publication-v6 is not active with a two-pass quorum")
    if receipt.get("statement_source_digest") != _sha256(statement_raw):
        raise AxiomGraphProjectionError("statement bytes differ from the publication receipt")
    if receipt.get("proof_digest") != _sha256(blueprint_raw):
        raise AxiomGraphProjectionError("blueprint bytes differ from the publication receipt")
    expected_receipt_sha256 = _sha256(_canonical_json(dict(receipt)) + b"\n")
    if expected_receipt_sha256 != publication_receipt_sha256:
        raise AxiomGraphProjectionError("publication receipt bytes are not the reconciled receipt")

    try:
        statement_text = statement_raw.decode("utf-8", "strict")
        blueprint_text = blueprint_raw.decode("utf-8", "strict")
        manifest = proof_context_parser.parse_blueprint(
            blueprint_text,
            target_statement=statement_text,
        )
    except (UnicodeError, TypeError, ValueError) as exc:
        raise AxiomGraphProjectionError("publication proof manifest cannot be rebuilt") from exc
    if list(manifest.item_ids) != receipt.get("checked_item_ids"):
        raise AxiomGraphProjectionError("publication item coverage changed before projection")
    if not manifest.items:
        raise AxiomGraphProjectionError("publication manifest has no target item")

    proof_context = receipt.get("proof_context")
    environment_material = {
        "canonical_target_digest": receipt.get("canonical_target_digest"),
        "projection_schema": "axiomrelay_opaque_scope_nfc_lf_v1",
        "proof_context": proof_context,
        "statement_source_digest": receipt.get("statement_source_digest"),
    }
    environment_blob = contract.make_content_blob(
        purpose="source_context",
        media_type="application/json",
        content=contract.canonical_bytes(environment_material),
    )
    environment_digest = contract.environment_manifest_digest((environment_blob,))
    verifier_profile_digest = contract.content_digest(
        contract.canonical_bytes(_stable_verifier_profile(receipt))
    )
    policy = contract.make_policy(
        name="axiomrelay-publication-v6",
        allowed_inference_rule_ids=("axiomrelay-verified-proof-item-v1",),
        allowed_verifier_authority_ids=("axiomrelay-publication-v6",),
        allowed_receipt_scheme_ids=("rethlas-publication-v6",),
        verifier_profile_digest=verifier_profile_digest,
    )

    statements_by_item: dict[str, Any] = {}
    unique_statements: dict[str, Any] = {}
    for item in manifest.items:
        statement = contract.make_statement(
            environment_digest=environment_digest,
            normalization_id="axiomrelay_opaque_scope_nfc_lf_v1",
            gamma=(),
            proposition=_projection_text(item.statement),
        )
        statements_by_item[item.item_id] = statement
        unique_statements[statement.statement_id] = statement

    target_statement = statements_by_item[manifest.items[-1].item_id]
    identity = contract.make_graph_identity(
        exact_target_digest="sha256:" + receipt["statement_source_digest"],
        target_statement=target_statement,
        policy_id=policy.policy_id,
        namespace="axiomrelay_publication_projection_v1",
    )

    derivations_by_item: dict[str, Any] = {}
    for item in manifest.items:
        try:
            # ProofItems are finer-grained than AxiomGraph statements.  Two
            # distinct proof items may therefore quotient to the same st1_
            # premise; a hyperedge contains statement premises as a set, while
            # the original ordered dependency identities remain bound by the
            # ProofItem artifact digest below.
            premise_ids = tuple(
                sorted(
                    {
                        statements_by_item[dependency_id].statement_id
                        for dependency_id in item.depends_on
                    }
                )
            )
        except KeyError as exc:
            raise AxiomGraphProjectionError("proof item dependency is absent from the manifest") from exc
        derivations_by_item[item.item_id] = contract.make_verified_derivation(
            conclusion_id=statements_by_item[item.item_id].statement_id,
            premise_ids=premise_ids,
            proof_artifact_digest="sha256:" + _require_sha256(item.digest, "proof item digest"),
            inference_rule_id="axiomrelay-verified-proof-item-v1",
            policy_id=policy.policy_id,
            verifier_authority_id="axiomrelay-publication-v6",
            receipt_scheme_id="rethlas-publication-v6",
            receipt_bundle_digest="sha256:" + publication_receipt_sha256,
        )
    snapshot = contract.make_snapshot(
        graph_identity=identity,
        policies=(policy,),
        derivations=derivations_by_item.values(),
    )

    # Quotienting ProofItems by statement can create multiple OR edges or a
    # syntactic self-edge.  Retain the first edge whose premises are already
    # grounded in ProofItem topological order, then take only the target cone.
    selected_by_statement: dict[str, str] = {}
    grounded: set[str] = set()
    for item_id in manifest.topological_item_ids:
        derivation = derivations_by_item[item_id]
        if set(derivation.premise_ids).issubset(grounded):
            grounded.add(derivation.conclusion_id)
            selected_by_statement.setdefault(derivation.conclusion_id, derivation.derivation_id)
    if target_statement.statement_id not in grounded:
        raise AxiomGraphProjectionError("statement quotient lost a grounded target witness")

    derivations_by_id = {item.derivation_id: item for item in snapshot.derivations}
    target_cone: dict[str, str] = {}
    pending = [target_statement.statement_id]
    while pending:
        conclusion_id = pending.pop()
        if conclusion_id in target_cone:
            continue
        derivation_id = selected_by_statement.get(conclusion_id)
        if derivation_id is None:
            raise AxiomGraphProjectionError("grounded target witness has an open dependency")
        target_cone[conclusion_id] = derivation_id
        pending.extend(derivations_by_id[derivation_id].premise_ids)
    selection = contract.make_selection(
        graph_id=identity.graph_id,
        snapshot_head=snapshot.snapshot_head,
        target_statement_id=target_statement.statement_id,
        policy_id=policy.policy_id,
        by_conclusion=target_cone,
    )

    class ReconciledPublicationAuthenticator:
        @staticmethod
        def authenticate_snapshot(candidate: Any) -> Any:
            if candidate.snapshot_head != snapshot.snapshot_head:
                raise AxiomGraphProjectionError("authority adapter received a different snapshot")
            for derivation in candidate.derivations:
                if (
                    derivation.verifier_authority_id != "axiomrelay-publication-v6"
                    or derivation.receipt_scheme_id != "rethlas-publication-v6"
                    or derivation.receipt_bundle_digest != "sha256:" + publication_receipt_sha256
                ):
                    raise AxiomGraphProjectionError("derivation is not bound to the reconciled publication")
            evidence = {
                "checked_item_ids": list(manifest.item_ids),
                "publication_receipt_sha256": publication_receipt_sha256,
                "snapshot_head": candidate.snapshot_head,
            }
            return contract.SnapshotAuthenticationReceiptV1(
                graph_id=candidate.graph_id,
                snapshot_head=candidate.snapshot_head,
                adapter_identity="axiomrelay-reconciled-publication-v6",
                authoritative_store_head_digest="sha256:" + publication_receipt_sha256,
                authentication_evidence_digest=contract.content_digest(
                    contract.canonical_bytes(evidence)
                ),
            )

    authenticated = contract.authenticate_snapshot(snapshot, ReconciledPublicationAuthenticator())
    validation = contract.validate_selection(authenticated, selection)
    if (
        not validation.grounded
        or not validation.publication_eligible
        or validation.errors
        or validation.open_obligations
        or validation.publication_blockers
    ):
        raise AxiomGraphProjectionError("reconciled publication did not survive AxiomGraph validation")
    bundle = contract.make_graph_bundle(
        graph_id=identity.graph_id,
        exact_target_digest=identity.exact_target_digest,
        exact_target_blob=contract.make_content_blob(
            purpose="exact_target",
            media_type="text/markdown",
            content=statement_raw,
        ),
        environment_blobs=(environment_blob,),
        target_statement_id=target_statement.statement_id,
        statements=unique_statements.values(),
        claims=(),
        derivation_snapshot=snapshot,
    )
    return PublicationProjectionV1(
        bundle=bundle,
        selection=selection,
        validation=validation,
        authentication_receipt=authenticated.authentication_receipt,
        source_receipt_sha256=publication_receipt_sha256,
    )


def projection_document(projection: PublicationProjectionV1) -> bytes:
    contract = _contract()
    bundle_document = json.loads(contract.dump_graph_bundle(projection.bundle).decode("utf-8"))
    return contract.canonical_bytes(
        {
            "authentication_receipt": projection.authentication_receipt.to_payload()
            | {"authentication_id": projection.authentication_receipt.authentication_id},
            "bundle_document": bundle_document,
            "schema": "axiomrelay_axiomgraph_publication_shadow_v1",
            "schema_bundle_digest": contract.SCHEMA_BUNDLE_DIGEST,
            "selection": projection.selection.to_payload()
            | {"selection_id": projection.selection.selection_id},
            "source_authority": "rethlas-publication-v6",
            "source_publication_receipt_sha256": projection.source_receipt_sha256,
            "terminal_outcome": {"kind": "published_verified"},
            "validation": projection.validation.to_payload()
            | {"validation_digest": projection.validation.validation_digest},
        }
    )


def write_publication_projection(
    *, projection: PublicationProjectionV1, shadow_root: Path
) -> dict[str, str]:
    raw = projection_document(projection)
    directory = _private_directory(
        shadow_root / "axiomgraph_contract_v1" / "publications"
        / projection.source_receipt_sha256
    )
    path = directory / "projection.json"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
    except FileExistsError:
        existing = path.read_bytes()
        if existing != raw:
            raise AxiomGraphProjectionError("publication shadow sidecar collision")
    else:
        try:
            offset = 0
            while offset < len(raw):
                offset += os.write(descriptor, raw[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {
        "path": str(path),
        "projection_sha256": _sha256(raw),
        "bundle_head": projection.bundle.bundle_head,
        "selection_id": projection.selection.selection_id,
        "validation_digest": projection.validation.validation_digest,
    }


def _private_directory(path: Path) -> Path:
    previous_umask = os.umask(0o077)
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    finally:
        os.umask(previous_umask)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AxiomGraphProjectionError("shadow sidecar directory is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path.absolute()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise AxiomGraphProjectionError("shadow sidecar directory is unsafe or not private")
    return resolved
