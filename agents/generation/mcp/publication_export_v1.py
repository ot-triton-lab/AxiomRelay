"""Versioned, stdlib-only AxiomGraph source export for AxiomRelay.

This module is part of Claude Core's authenticated runtime dependency closure.
It emits source events only; it does not import AxiomGraph, grant controller
authority, or interpret an event as a proof outside AxiomRelay.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "axiomrelay_axiomgraph_source_interface_v1"
EVENT_SCHEMA = "axiomrelay_verified_publication_event_v1"
PROOF_MANIFEST_SCHEMA = "axiomrelay_normalized_proof_manifest_v1"
EXPORT_RECEIPT_SCHEMA = "axiomrelay_verified_publication_export_receipt_v1"
EVENT_ID_DOMAIN = b"axiomrelay:verified-publication-event:v1\0"
EVENT_ID_RE = re.compile(r"arev_[0-9a-f]{64}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
AXIOMGRAPH_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
PROOF_ITEM_ID_RE = re.compile(r"pi_[0-9a-f]{24}")
MAX_EVENT_BYTES = 192_000_000
MAX_EXACT_TARGET_BYTES_V1 = 4 * 1024 * 1024
# Leave room for the fixed-size activation, event, and runtime digest fields in
# AxiomGraph's 4 MiB source-context blob. Both sides enforce this v1 wire bound.
MAX_PROJECTION_CONTEXT_BYTES_V1 = MAX_EXACT_TARGET_BYTES_V1 - 4096

MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "interface_major",
        "interface_minor",
        "required_capabilities",
        "optional_capabilities",
        "minimum_consumer_minor",
        "event_schema",
        "interface_manifest_relative_path",
        "export_module_relative_path",
        "event_store_relative_path",
        "runtime_dependency_files",
        "axiomgraph_contract_version",
        "axiomgraph_schema_bundle_digest",
    }
)
EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "interface",
        "source",
        "exact_target",
        "exact_blueprint",
        "publication_receipt",
        "proof_manifest",
        "stable_verifier_profile",
        "source_runtime",
    }
)
INTERFACE_KEYS = frozenset(
    {
        "manifest_schema_version",
        "interface_major",
        "interface_minor",
        "capability",
        "axiomgraph_contract_version",
        "axiomgraph_schema_bundle_digest",
    }
)
SOURCE_KEYS = frozenset(
    {
        "authority_id",
        "terminal_outcome",
        "problem_id",
        "statement_sha256",
        "canonical_target_sha256",
        "blueprint_sha256",
        "publication_receipt_sha256",
    }
)
BLOB_KEYS = frozenset(
    {"encoding", "media_type", "content_sha256", "content_base64"}
)
PROOF_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "proof_digest",
        "source_kind",
        "topological_item_ids",
        "items",
    }
)
PROOF_ITEM_KEYS = frozenset(
    {
        "index",
        "item_id",
        "artifact_sha256",
        "title",
        "label",
        "statement",
        "depends_on",
        "dependency_mode",
    }
)
VERIFIER_PROFILE_KEYS = frozenset(
    {
        "proof_context",
        "verification_limits",
        "verification_passes",
        "verification_quorum",
    }
)
VERIFIER_PASS_KEYS = frozenset(
    {
        "pass_index",
        "verification_role",
        "verifier_model",
        "verifier_reasoning_effort",
        "verifier_service_version",
    }
)
SOURCE_RUNTIME_KEYS = frozenset(
    {
        "loaded_claude_core_sha256",
        "publication_export_module_sha256",
        "interface_manifest_sha256",
        "runtime_dependency_manifest_sha256",
    }
)


class PublicationExportError(RuntimeError):
    """The source event is malformed or cannot be stored safely."""


def _assert_wire_value(value: Any, *, depth: int = 0) -> None:
    if depth > 256:
        raise PublicationExportError("wire value exceeds the nesting limit")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            _require_text(value, "wire string", allow_empty=True)
        return
    if isinstance(value, float):
        raise PublicationExportError("wire values cannot contain floats")
    if isinstance(value, list):
        for item in value:
            _assert_wire_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PublicationExportError("wire object keys must be strings")
            _require_text(key, "wire object key", allow_empty=True)
            _assert_wire_value(item, depth=depth + 1)
        return
    raise PublicationExportError("wire value contains a non-JSON type")


def canonical_bytes(value: Any) -> bytes:
    """Encode the v1 canonical JSON representation without a trailing LF."""

    try:
        _assert_wire_value(value)
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise PublicationExportError("value is not canonical JSON data") from exc


def sha256_bytes(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise PublicationExportError("digest input must be bytes")
    return hashlib.sha256(value).hexdigest()


def _exact_object(value: Any, keys: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise PublicationExportError(f"{label} shape mismatch")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise PublicationExportError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _require_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise PublicationExportError(f"{label} is invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise PublicationExportError(f"{label} is not strict UTF-8") from exc
    return value


def _projection_json_value(value: Any) -> Any:
    """Check the NFC/LF projection language without importing AxiomGraph."""

    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        return unicodedata.normalize(
            "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
        )
    if type(value) is list:
        return [_projection_json_value(item) for item in value]
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _projection_json_value(key)
            if normalized_key in result:
                raise PublicationExportError(
                    "source JSON keys collide under AxiomGraph normalization"
                )
            result[normalized_key] = _projection_json_value(item)
        return result
    raise PublicationExportError("source JSON value cannot enter AxiomGraph records")


def validate_interface_manifest(value: Any) -> dict[str, Any]:
    manifest = dict(_exact_object(value, MANIFEST_KEYS, "interface manifest"))
    if (
        manifest["schema_version"] != MANIFEST_SCHEMA
        or isinstance(manifest["interface_major"], bool)
        or isinstance(manifest["interface_minor"], bool)
        or isinstance(manifest["minimum_consumer_minor"], bool)
        or manifest["interface_major"] != 1
        or manifest["interface_minor"] != 0
        or manifest["required_capabilities"] != ["verified_publication_event_v1"]
        or manifest["optional_capabilities"] != []
        or manifest["minimum_consumer_minor"] != 0
        or manifest["event_schema"] != EVENT_SCHEMA
        or manifest["interface_manifest_relative_path"]
        != "agents/generation/mcp/axiomgraph_source_interface_v1.json"
        or manifest["export_module_relative_path"]
        != "agents/generation/mcp/publication_export_v1.py"
        or manifest["event_store_relative_path"]
        != "agents/.claude_core/axiomgraph_exports/v1/publications"
        or manifest["runtime_dependency_files"]
        != [
            "agents/generation/CLAUDE.md",
            "agents/generation/.mcp.json",
            "agents/generation/mcp/legacy_server.py",
            "agents/generation/mcp/legacy_verification_client.py",
            "agents/generation/mcp/proof_context.py",
            "agents/generation/mcp/publication_proof_context_v3.py",
            "agents/generation/mcp/publication_export_v1.py",
            "agents/generation/mcp/axiomgraph_source_interface_v1.json",
        ]
        or manifest["axiomgraph_contract_version"] != "axiomgraph_contract_v1"
        or AXIOMGRAPH_DIGEST_RE.fullmatch(
            str(manifest["axiomgraph_schema_bundle_digest"])
        )
        is None
    ):
        raise PublicationExportError("interface manifest value mismatch")
    return manifest


def load_interface_manifest(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise PublicationExportError("interface manifest must end in exactly one LF")
    try:
        value = json.loads(raw[:-1].decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PublicationExportError("interface manifest is not strict JSON") from exc
    manifest = validate_interface_manifest(value)
    if canonical_bytes(manifest) + b"\n" != raw:
        raise PublicationExportError("interface manifest is not canonical")
    return manifest


def _decode_blob(value: Any, label: str) -> bytes:
    blob = _exact_object(value, BLOB_KEYS, label)
    if blob["encoding"] != "base64" or blob["media_type"] != "text/markdown":
        raise PublicationExportError(f"{label} encoding or media type mismatch")
    digest = _require_sha256(blob["content_sha256"], f"{label} digest")
    encoded = _require_text(blob["content_base64"], f"{label} base64", allow_empty=True)
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise PublicationExportError(f"{label} is not canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != encoded or sha256_bytes(raw) != digest:
        raise PublicationExportError(f"{label} content binding mismatch")
    if not raw:
        raise PublicationExportError(f"{label} cannot be empty")
    return raw


def _validate_proof_manifest(
    value: Any,
    *,
    blueprint_sha256: str,
    canonical_target_sha256: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = dict(_exact_object(value, PROOF_MANIFEST_KEYS, "proof manifest"))
    if (
        manifest["schema_version"] != PROOF_MANIFEST_SCHEMA
        or manifest["proof_digest"] != blueprint_sha256
        or manifest["source_kind"] not in {"structured", "synthetic"}
        or not isinstance(manifest["items"], list)
        or not manifest["items"]
        or not isinstance(manifest["topological_item_ids"], list)
    ):
        raise PublicationExportError("proof manifest value mismatch")

    item_ids: list[str] = []
    dependencies: dict[str, list[str]] = {}
    for expected_index, raw_item in enumerate(manifest["items"]):
        item = _exact_object(raw_item, PROOF_ITEM_KEYS, "proof manifest item")
        item_id = item["item_id"]
        if (
            type(item["index"]) is not int
            or item["index"] != expected_index
            or not isinstance(item_id, str)
            or PROOF_ITEM_ID_RE.fullmatch(item_id) is None
            or item_id in dependencies
            or not isinstance(item["artifact_sha256"], str)
            or SHA256_RE.fullmatch(item["artifact_sha256"]) is None
            or item_id != "pi_" + item["artifact_sha256"][:24]
            or item["dependency_mode"]
            not in {"explicit", "conservative-prefix", "synthetic"}
            or not isinstance(item["depends_on"], list)
            or len(item["depends_on"]) != len(set(item["depends_on"]))
        ):
            raise PublicationExportError("proof manifest item binding mismatch")
        for field in ("title", "label", "statement"):
            _require_text(item[field], f"proof item {field}")
        if any(
            not isinstance(dependency, str)
            or PROOF_ITEM_ID_RE.fullmatch(dependency) is None
            or dependency == item_id
            for dependency in item["depends_on"]
        ):
            raise PublicationExportError("proof manifest dependency mismatch")
        item_ids.append(item_id)
        dependencies[item_id] = list(item["depends_on"])

    topological = manifest["topological_item_ids"]
    if (
        len(topological) != len(item_ids)
        or len(set(topological)) != len(topological)
        or set(topological) != set(item_ids)
        or receipt.get("checked_item_ids") != item_ids
    ):
        raise PublicationExportError("proof manifest coverage mismatch")
    seen: set[str] = set()
    for item_id in topological:
        if not set(dependencies[item_id]).issubset(seen):
            raise PublicationExportError("proof manifest order is not topological")
        seen.add(item_id)
    final_statement = manifest["items"][-1]["statement"]
    if sha256_bytes(final_statement.encode("utf-8", "strict")) != (
        canonical_target_sha256
    ):
        raise PublicationExportError(
            "proof manifest final statement binding mismatch"
        )
    return manifest


def _validate_verifier_profile(
    value: Any, *, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    profile = dict(_exact_object(value, VERIFIER_PROFILE_KEYS, "verifier profile"))
    passes = profile["verification_passes"]
    receipt_passes = receipt.get("verification_passes")
    if (
        profile["proof_context"] != receipt.get("proof_context")
        or profile["verification_limits"] != receipt.get("verification_limits")
        or isinstance(profile["verification_quorum"], bool)
        or profile["verification_quorum"] != 2
        or profile["verification_quorum"] != receipt.get("verification_quorum")
        or not isinstance(passes, list)
        or len(passes) != 2
        or not isinstance(receipt_passes, list)
        or len(receipt_passes) != 2
    ):
        raise PublicationExportError("verifier profile binding mismatch")
    for index, (stable, source) in enumerate(zip(passes, receipt_passes), start=1):
        item = _exact_object(stable, VERIFIER_PASS_KEYS, "stable verifier pass")
        if not isinstance(source, Mapping) or any(
            item[field] != source.get(field) for field in VERIFIER_PASS_KEYS
        ) or source.get("verdict") != "correct" or isinstance(
            item["pass_index"], bool
        ) or item[
            "pass_index"
        ] != index or item["verification_role"] != (
            "primary" if index == 1 else "adversarial_full_claim_audit"
        ):
            raise PublicationExportError("stable verifier pass binding mismatch")
        for field in (
            "verification_role",
            "verifier_model",
            "verifier_reasoning_effort",
            "verifier_service_version",
        ):
            _require_text(item[field], f"stable verifier pass {field}")
    return profile


def validate_verified_publication_event(
    value: Any,
    *,
    interface_manifest: Mapping[str, Any],
    expected_source_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = validate_interface_manifest(interface_manifest)
    event = dict(_exact_object(value, EVENT_KEYS, "publication event"))
    if event["schema_version"] != EVENT_SCHEMA:
        raise PublicationExportError("publication event schema mismatch")

    interface = _exact_object(event["interface"], INTERFACE_KEYS, "event interface")
    expected_interface = {
        "manifest_schema_version": manifest["schema_version"],
        "interface_major": manifest["interface_major"],
        "interface_minor": manifest["interface_minor"],
        "capability": "verified_publication_event_v1",
        "axiomgraph_contract_version": manifest["axiomgraph_contract_version"],
        "axiomgraph_schema_bundle_digest": manifest[
            "axiomgraph_schema_bundle_digest"
        ],
    }
    if (
        type(interface["interface_major"]) is not int
        or type(interface["interface_minor"]) is not int
        or dict(interface) != expected_interface
    ):
        raise PublicationExportError("publication event interface mismatch")

    source = _exact_object(event["source"], SOURCE_KEYS, "event source")
    for field in (
        "statement_sha256",
        "canonical_target_sha256",
        "blueprint_sha256",
        "publication_receipt_sha256",
    ):
        _require_sha256(source[field], f"event source {field}")
    if (
        source["authority_id"] != "rethlas-publication-v6"
        or source["terminal_outcome"] != "published_verified"
    ):
        raise PublicationExportError("event source authority mismatch")
    _require_text(source["problem_id"], "event source problem id")

    target_raw = _decode_blob(event["exact_target"], "exact target")
    blueprint_raw = _decode_blob(event["exact_blueprint"], "exact blueprint")
    if len(target_raw) > MAX_EXACT_TARGET_BYTES_V1:
        raise PublicationExportError("exact target exceeds the v1 projection size bound")
    if (
        sha256_bytes(target_raw) != source["statement_sha256"]
        or sha256_bytes(blueprint_raw) != source["blueprint_sha256"]
    ):
        raise PublicationExportError("source blob digest mismatch")

    receipt = event["publication_receipt"]
    if not isinstance(receipt, Mapping):
        raise PublicationExportError("publication receipt is not an object")
    receipt_digest = sha256_bytes(canonical_bytes(receipt) + b"\n")
    if (
        receipt_digest != source["publication_receipt_sha256"]
        or receipt.get("schema_version") != source["authority_id"]
        or receipt.get("state") != "active"
        or receipt.get("problem_id") != source["problem_id"]
        or receipt.get("statement_source_digest") != source["statement_sha256"]
        or receipt.get("canonical_target_digest")
        != source["canonical_target_sha256"]
        or receipt.get("proof_digest") != source["blueprint_sha256"]
    ):
        raise PublicationExportError("publication receipt binding mismatch")

    _validate_proof_manifest(
        event["proof_manifest"],
        blueprint_sha256=source["blueprint_sha256"],
        canonical_target_sha256=source["canonical_target_sha256"],
        receipt=receipt,
    )
    profile = _validate_verifier_profile(event["stable_verifier_profile"], receipt=receipt)
    projected_profile = _projection_json_value(profile)
    projection_context = {
        "problem_id": _projection_json_value(source["problem_id"]),
        "proof_context": projected_profile["proof_context"],
    }
    if len(canonical_bytes(projection_context)) > MAX_PROJECTION_CONTEXT_BYTES_V1:
        raise PublicationExportError("source context exceeds the v1 projection size bound")

    runtime = dict(_exact_object(event["source_runtime"], SOURCE_RUNTIME_KEYS, "source runtime"))
    for field, digest in runtime.items():
        _require_sha256(digest, f"source runtime {field}")
    if expected_source_runtime is not None and runtime != dict(expected_source_runtime):
        raise PublicationExportError("source runtime binding mismatch")

    event_id = event["event_id"]
    if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None:
        raise PublicationExportError("publication event id is invalid")
    payload = {key: item for key, item in event.items() if key != "event_id"}
    expected_event_id = "arev_" + sha256_bytes(EVENT_ID_DOMAIN + canonical_bytes(payload))
    if event_id != expected_event_id:
        raise PublicationExportError("publication event id binding mismatch")
    return event


def make_verified_publication_event(
    *,
    interface_manifest: Mapping[str, Any],
    source: Mapping[str, Any],
    exact_target_raw: bytes,
    exact_blueprint_raw: bytes,
    publication_receipt: Mapping[str, Any],
    proof_manifest: Mapping[str, Any],
    stable_verifier_profile: Mapping[str, Any],
    source_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = validate_interface_manifest(interface_manifest)
    payload: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA,
        "interface": {
            "manifest_schema_version": manifest["schema_version"],
            "interface_major": manifest["interface_major"],
            "interface_minor": manifest["interface_minor"],
            "capability": "verified_publication_event_v1",
            "axiomgraph_contract_version": manifest["axiomgraph_contract_version"],
            "axiomgraph_schema_bundle_digest": manifest[
                "axiomgraph_schema_bundle_digest"
            ],
        },
        "source": dict(source),
        "exact_target": {
            "encoding": "base64",
            "media_type": "text/markdown",
            "content_sha256": sha256_bytes(exact_target_raw),
            "content_base64": base64.b64encode(exact_target_raw).decode("ascii"),
        },
        "exact_blueprint": {
            "encoding": "base64",
            "media_type": "text/markdown",
            "content_sha256": sha256_bytes(exact_blueprint_raw),
            "content_base64": base64.b64encode(exact_blueprint_raw).decode("ascii"),
        },
        "publication_receipt": dict(publication_receipt),
        "proof_manifest": dict(proof_manifest),
        "stable_verifier_profile": dict(stable_verifier_profile),
        "source_runtime": dict(source_runtime),
    }
    event_id = "arev_" + sha256_bytes(EVENT_ID_DOMAIN + canonical_bytes(payload))
    event = {**payload, "event_id": event_id}
    return validate_verified_publication_event(
        event,
        interface_manifest=manifest,
        expected_source_runtime=source_runtime,
    )


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PublicationExportError("event directory is unavailable") from exc
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PublicationExportError("event directory is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or resolved != path.absolute()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise PublicationExportError("event directory is unsafe or not private")
    return resolved


def _artifact_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise PublicationExportError("event directory cannot be opened") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise PublicationExportError("event directory changed type")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temporary_prefix(path: Path) -> str:
    return f".{path.name}.publication-export-v1-"


def _remove_private_temporary_aliases(
    path: Path, *, device: int, inode: int
) -> None:
    """Remove only interrupted same-directory temp links to ``path``."""

    prefix = _temporary_prefix(path)
    try:
        observed = path.lstat()
        candidates = list(path.parent.iterdir())
    except OSError as exc:
        raise PublicationExportError("cannot inspect existing event aliases") from exc
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != (device, inode)
    ):
        raise PublicationExportError("event path collision")
    removed = False
    for candidate in candidates:
        if not candidate.name.startswith(prefix):
            continue
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PublicationExportError(
                "cannot inspect existing event aliases"
            ) from exc
        if (
            not stat.S_ISLNK(metadata.st_mode)
            and stat.S_ISREG(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == (device, inode)
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PublicationExportError(
                    "cannot remove interrupted event alias"
                ) from exc
            removed = True
    if removed:
        _fsync_directory(path.parent)


def _read_existing(path: Path, expected: bytes) -> None:
    try:
        metadata = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PublicationExportError("cannot inspect existing event") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o400
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or _artifact_identity(metadata) != _artifact_identity(opened)
        ):
            raise PublicationExportError("event path collision")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining or os.read(descriptor, 1):
            raise PublicationExportError("existing event read was incomplete")
        after_open = os.fstat(descriptor)
        if (
            _artifact_identity(after_open) != _artifact_identity(opened)
            or b"".join(chunks) != expected
        ):
            raise PublicationExportError("event path collision")
        if opened.st_nlink != 1 or after_open.st_nlink != 1:
            _remove_private_temporary_aliases(
                path,
                device=opened.st_dev,
                inode=opened.st_ino,
            )
        final_open = os.fstat(descriptor)
        try:
            final_path = path.lstat()
        except OSError as exc:
            raise PublicationExportError(
                "existing event changed after reading"
            ) from exc
        if (
            final_open.st_nlink != 1
            or final_path.st_nlink != 1
            or _artifact_identity(final_open) != _artifact_identity(opened)
            or _artifact_identity(final_path) != _artifact_identity(opened)
        ):
            raise PublicationExportError("event path collision")
    finally:
        os.close(descriptor)


def _create_private_temporary(path: Path) -> tuple[Path, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(8):
        temporary = path.parent / (
            f"{_temporary_prefix(path)}{os.getpid()}-{secrets.token_hex(12)}"
        )
        try:
            descriptor = os.open(temporary, flags, 0o400)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PublicationExportError(
                "cannot create private event temporary"
            ) from exc
        try:
            os.fchmod(descriptor, 0o400)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o400
                or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            ):
                raise PublicationExportError("event temporary is unsafe")
        except BaseException:
            os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return temporary, descriptor
    raise PublicationExportError("cannot reserve a private event temporary")


def _write_once(path: Path, raw: bytes) -> None:
    """Publish complete bytes atomically without ever replacing ``path``."""

    temporary, descriptor = _create_private_temporary(path)
    published = False
    try:
        try:
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise PublicationExportError("event write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
            published = True
        except FileExistsError:
            _read_existing(path, raw)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        finally:
            _fsync_directory(path.parent)
    if published:
        _read_existing(path, raw)


def write_verified_publication_event(
    *,
    event: Mapping[str, Any],
    interface_manifest: Mapping[str, Any],
    expected_source_runtime: Mapping[str, Any],
    event_store: Path,
) -> dict[str, str]:
    """Validate and durably store one immutable, idempotent source event."""

    validated = validate_verified_publication_event(
        event,
        interface_manifest=interface_manifest,
        expected_source_runtime=expected_source_runtime,
    )
    raw = canonical_bytes(validated) + b"\n"
    if len(raw) > MAX_EVENT_BYTES:
        raise PublicationExportError("publication event exceeds the v1 byte limit")
    source = validated["source"]
    root = _private_directory(event_store.absolute())
    receipt_directory = _private_directory(root / source["publication_receipt_sha256"])
    event_directory = _private_directory(receipt_directory / validated["event_id"])
    path = event_directory / "event.json"
    _write_once(path, raw)
    return {
        "schema_version": EXPORT_RECEIPT_SCHEMA,
        "event_id": validated["event_id"],
        "event_sha256": sha256_bytes(raw),
        "publication_receipt_sha256": source["publication_receipt_sha256"],
        "path": str(path),
    }
