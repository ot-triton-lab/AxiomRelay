#!/usr/bin/env python3
"""Owner-host-only, zero-model route-review driver facade.

This module is deliberately not registered with FastMCP.  The guardian secure-
copies the attested MCP package, starts this file with ``python -I -B``, and
supplies the host master capability only to that one subprocess.  Reasoning
threads receive neither this command nor the master capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

# Load the official MCP SDK before the pinned runtime parent is added to
# ``sys.path``.  That parent also contains AxiomRelay's local ``mcp`` package; if it
# wins package resolution, ``server.py`` silently loses FastMCP/MCPServer and the
# owner review driver runs with a degraded module graph.
try:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _OfficialMCPServer  # noqa: F401
except ImportError:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as _OfficialMCPServer  # noqa: F401


INPUT_SCHEMA = "rethlas_review_drive_step_v1"
OUTPUT_SCHEMA = "rethlas_review_drive_step_result_v1"
DISPOSITION_SCHEMA = "rethlas_review_disposition_v1"
MAX_STDIN_BYTES = 32_768
MAX_STDOUT_BYTES = 262_144
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_ID_RE = re.compile(r"^review_[0-9a-f]{32}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _bind_attested_package() -> None:
    script = Path(__file__).resolve(strict=True)
    metadata = script.lstat()
    package_root = script.parent
    if (
        not stat.S_ISREG(metadata.st_mode)
        or script.is_symlink()
        or metadata.st_nlink != 1
        or package_root.is_symlink()
    ):
        raise RuntimeError("review driver is not in a real pinned package")
    sys.path.insert(0, os.fspath(package_root))
    sys.path.insert(0, os.fspath(package_root.parent))
    # Source checkout: agents/review is one level above generation/. Trusted
    # runtime snapshot: review is copied beside mcp's parent. Bind only the
    # first exact directory that contains the required pinned review package.
    review_parent_candidates = (package_root.parent, package_root.parent.parent)
    review_parent = next(
        (
            candidate
            for candidate in review_parent_candidates
            if (candidate / "review" / "contracts.py").is_file()
        ),
        None,
    )
    if review_parent is None or review_parent.is_symlink():
        raise RuntimeError("review driver cannot bind its pinned review package")
    sys.path.insert(0, os.fspath(review_parent))


_bind_attested_package()

if __package__:
    from . import review_client, server  # type: ignore[no-redef]  # noqa: E402
else:  # pragma: no cover - isolated production execution
    import review_client  # type: ignore[no-redef]  # noqa: E402
    import server  # type: ignore[no-redef]  # noqa: E402


class DriveError(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise DriveError(f"non-finite JSON constant {value} is forbidden")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DriveError("duplicate JSON key is forbidden")
        result[key] = value
    return result


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_stdin() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise DriveError("review drive input exceeds its byte bound")
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriveError("review drive input must be strict JSON") from exc
    if not isinstance(parsed, dict):
        raise DriveError("review drive input must be one JSON object")
    return parsed


def _exact(value: Any, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise DriveError(f"{label} has an unsupported shape")
    return value


def _review_id(value: Any) -> str:
    if not isinstance(value, str) or _REVIEW_ID_RE.fullmatch(value) is None:
        raise DriveError("review_id is invalid")
    return value


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise DriveError(f"{label} is invalid")
    return value


def _safe_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise DriveError(f"{label} is invalid")
    return value


def _require_owner_host_authority() -> None:
    if os.getenv("RETHLAS_REVIEW_DRIVER_AUTHORITY") != "owner_host_master_v1":
        raise DriveError("review driver lacks owner-host authority")
    token = os.getenv("RETHLAS_REVIEW_CONTROL_TOKEN", "")
    if len(token.encode("utf-8")) < 32:
        raise DriveError("review driver master capability is missing")


def _prepare(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = _exact(
        payload,
        {
            "schema_version",
            "operation",
            "cycle_id",
            "cycle",
            "review_ordinal",
        },
        label="review prepare drive input",
    )
    if raw["schema_version"] != INPUT_SCHEMA or raw["operation"] != "prepare":
        raise DriveError("review prepare drive binding is invalid")
    cycle_id = _safe_id(raw["cycle_id"], label="cycle_id")
    cycle = raw["cycle"]
    if cycle not in {"minute60", "minute120"}:
        raise DriveError("review cycle is invalid")
    ordinal = raw["review_ordinal"]
    if type(ordinal) is not int or ordinal != {"minute60": 1, "minute120": 2}[cycle]:
        raise DriveError("review ordinal is invalid")
    frontier = server.review_frontier_status(
        cycle_id=cycle_id,
        cycle=cycle,
        review_ordinal=ordinal,
    )
    review_id = _review_id(frontier.get("review_id"))
    prepared = server.route_review_prepare(
        review_id=review_id,
        cycle_id=cycle_id,
        cycle=cycle,
        review_ordinal=ordinal,
        frontier_manifest_sha256=frontier["manifest_sha256"],
        frontier_record_ids=list(frontier["frontier_record_ids"]),
        progress_record_ids=list(frontier["progress_record_ids"]),
    )
    return {"frontier": frontier, "prepared": prepared}


def _bound_result_input(payload: Mapping[str, Any], *, operation: str) -> dict[str, str]:
    raw = _exact(
        payload,
        {
            "schema_version",
            "operation",
            "review_id",
            "request_sha256",
            "snapshot_sha256",
        },
        label=f"review {operation} drive input",
    )
    if raw["schema_version"] != INPUT_SCHEMA or raw["operation"] != operation:
        raise DriveError(f"review {operation} drive binding is invalid")
    return {
        "review_id": _review_id(raw["review_id"]),
        "request_sha256": _digest(raw["request_sha256"], label="request_sha256"),
        "snapshot_sha256": _digest(
            raw["snapshot_sha256"], label="snapshot_sha256"
        ),
    }


def _wait_close(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    binding = _bound_result_input(payload, operation="wait_close")
    waited = server.route_review_wait(**binding)
    closed = None
    if waited.get("state") in {
        "completed",
        "completed_pending_close",
        "completed_pending_publication",
    }:
        closed = server.route_review_close(**binding)
    return {"waited": waited, "closed": closed}


def _targeted_verify(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    binding = _bound_result_input(payload, operation="targeted_verify")
    return {"targeted": server.verify_review_claim(**binding)}


def _disposition(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read one official result into a bounded handoff seed without starting work."""

    binding = _bound_result_input(payload, operation="disposition")
    problem_id = server.validate_verified_problem_id(
        server._required_review_env(
            "RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id"
        )
    )
    _record, body = server._find_review_memory(problem_id, **binding)
    if body.get("state") != "official_published":
        raise DriveError("review disposition requires durable official memory")
    host = server.route_review_status(**binding)
    if host.get("state") not in {"closed", "verification_required"}:
        raise DriveError("review disposition requires an official host terminal state")
    decision = body.get("decision")
    report = body.get("report")
    if (
        not isinstance(decision, dict)
        or not isinstance(report, dict)
        or host.get("decision") != decision
    ):
        raise DriveError("review disposition decision binding changed")
    targeted = body.get("targeted_verification")
    requires_targeted = bool(
        report.get("load_bearing_claim") is not None
        and (not isinstance(targeted, dict) or targeted.get("state") != "completed")
    )
    if requires_targeted != (host.get("state") == "verification_required"):
        raise DriveError("review targeted-verification state is inconsistent")

    active_route: Mapping[str, Any] | None
    frozen_route_id: str | None = None
    transition_receipt: Mapping[str, Any] | None = None
    next_milestone: Mapping[str, Any] | None
    evidence_ids = list(decision.get("critic_confirmed_progress_ids", []))
    transition = body.get("route_transition")
    if not isinstance(transition, dict) or set(transition) != {
        "next_route_id",
        "fallback_evidence_record_ids",
    }:
        raise DriveError("review disposition route transition is malformed")
    if decision.get("effective_verdict") == "red":
        frozen_route_id = decision.get("route_id")
        next_route_id = transition["next_route_id"]
        if next_route_id is None:
            active_route = None
            next_milestone = None
            transition_receipt = server._route_transition_projection_receipt(
                problem_id=problem_id,
                review_body=body,
                fallback=None,
                publish=False,
            )
        else:
            candidates = body.get("fallback_route_candidates")
            matches = [
                candidate
                for candidate in candidates
                if isinstance(candidate, dict)
                and candidate.get("route_id") == next_route_id
            ] if isinstance(candidates, list) else []
            if len(matches) != 1:
                raise DriveError("review disposition lost its committed fallback")
            active_route = matches[0]
            transition_receipt = server._route_transition_projection_receipt(
                problem_id=problem_id,
                review_body=body,
                fallback=active_route,
                publish=False,
            )
            fallback_evidence = transition["fallback_evidence_record_ids"]
            if active_route.get("evidence_record_ids") != fallback_evidence:
                raise DriveError("review disposition fallback evidence changed")
            for record_id in fallback_evidence:
                if record_id not in evidence_ids:
                    evidence_ids.append(record_id)
            first_obligation = active_route.get("obligations", [None])[0]
            if not isinstance(first_obligation, str) or not first_obligation:
                raise DriveError("review disposition fallback has no next obligation")
            next_milestone = {
                "description": first_obligation,
                "test": first_obligation,
            }
    else:
        if transition["next_route_id"] is not None or transition[
            "fallback_evidence_record_ids"
        ]:
            raise DriveError("non-red review disposition cannot switch route")
        active_route = body.get("active_route")
        answers = report.get("answers")
        next_milestone = (
            answers.get("next_milestone") if isinstance(answers, dict) else None
        )
        if not isinstance(active_route, dict) or not isinstance(next_milestone, dict):
            raise DriveError("review disposition lacks its active route or milestone")

    disposition = {
        "schema_version": DISPOSITION_SCHEMA,
        "review_id": binding["review_id"],
        "request_sha256": binding["request_sha256"],
        "snapshot_sha256": binding["snapshot_sha256"],
        "decision": decision,
        "active_route": active_route,
        "frozen_route_id": frozen_route_id,
        "route_transition_publication_receipt": transition_receipt,
        "next_milestone": next_milestone,
        "evidence_record_ids": evidence_ids,
        "requires_targeted_verification": requires_targeted,
    }
    return {"disposition": disposition}


def drive_step(payload: Mapping[str, Any]) -> dict[str, Any]:
    _require_owner_host_authority()
    operation = payload.get("operation") if isinstance(payload, dict) else None
    if operation == "prepare":
        artifact = _prepare(payload)
    elif operation == "wait_close":
        artifact = _wait_close(payload)
    elif operation == "targeted_verify":
        artifact = _targeted_verify(payload)
    elif operation == "disposition":
        artifact = _disposition(payload)
    else:
        raise DriveError("review drive operation is unsupported")
    artifact_bytes = _canonical_bytes(artifact)
    if operation == "disposition":
        disposition = artifact.get("disposition")
        state = (
            "verification_required"
            if isinstance(disposition, dict)
            and disposition.get("requires_targeted_verification") is True
            else "closed"
        )
    else:
        state_source = (
            artifact.get("closed")
            or artifact.get("targeted")
            or artifact.get("waited")
            or artifact.get("prepared")
        )
        state = state_source.get("state") if isinstance(state_source, dict) else None
    if not isinstance(state, str) or not state:
        raise DriveError("review drive artifact lacks a state")
    return {
        "schema_version": OUTPUT_SCHEMA,
        "operation": operation,
        "review_id": (
            artifact["frontier"]["review_id"]
            if operation == "prepare"
            else payload["review_id"]
        ),
        "state": state,
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "artifact": artifact,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments != ["drive-step"]:
        print("usage: server_driver.py drive-step", file=sys.stderr)
        return 2
    try:
        payload = _read_stdin()
        # This marker is the first action after input and owner-authority
        # validation.  Every operation that can cross an external boundary is
        # therefore durably classified before it begins.
        _require_owner_host_authority()
        review_client.review_drive_step_marker(payload)
        result = drive_step(payload)
        # Persist the complete bounded result before stdout.  A successor can
        # replay this receipt if the owner disappears after the child exits but
        # before the parent records the drive state.
        review_client.review_drive_step_marker(payload, result=result)
        encoded = _canonical_bytes(result)
        if len(encoded) > MAX_STDOUT_BYTES:
            raise DriveError("review drive output exceeds its byte bound")
    except (DriveError, review_client.ReviewAdapterError, ValueError, OSError) as exc:
        diagnostic = str(exc).encode("utf-8", errors="replace")[:4096]
        sys.stderr.buffer.write(diagnostic + b"\n")
        return 1
    sys.stdout.buffer.write(encoded + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
