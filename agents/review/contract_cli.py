#!/usr/bin/env python3
"""Isolated stdin/stdout facade for the pinned route-review contracts.

Production invokes this file by an absolute, runner-attested path with
``python -I -B``.  It never launches a model, opens a database, uses the
network, or reads user configuration.  The HotJoin process remains the sole
owner of scheduling, persistence, and reviewer execution.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping


MAX_STDIN_BYTES = 262_144
MAX_STDOUT_BYTES = 262_144
COMMANDS = frozenset(
    {
        "build-request",
        "build-invocation",
        "normalize-and-validate-report",
        "validate-request",
        "validate-report",
        "reduce-verdict",
        "build-targeted-ticket",
        "validate-handoff",
    }
)


def _bind_attested_package() -> None:
    script = Path(__file__).resolve(strict=True)
    package_root = script.parent
    package_parent = package_root.parent
    script_metadata = script.lstat()
    package_metadata = package_root.lstat()
    parent_metadata = package_parent.lstat()
    if (
        not stat.S_ISREG(script_metadata.st_mode)
        or script.is_symlink()
        or script_metadata.st_nlink != 1
        or not stat.S_ISDIR(package_metadata.st_mode)
        or package_root.is_symlink()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or package_parent.is_symlink()
    ):
        raise RuntimeError("review contract CLI is not in a real pinned package")
    # The runner manifest pins every file below package_root and makes the
    # trusted snapshot read-only before this process starts.  Add only its exact
    # parent; isolated mode continues to ignore cwd, user site, and PYTHONPATH.
    sys.path.insert(0, os.fspath(package_parent))


_bind_attested_package()

from review.contracts import (  # noqa: E402
    ReviewContractError,
    apply_effective_verdict,
    build_targeted_verification_ticket,
    canonical_json_bytes,
    handoff_id,
    handoff_sha256,
    strict_json_loads,
    validate_context_handoff,
    validate_review_report,
)
from review.critic import (  # noqa: E402
    build_invocation,
    build_review_request,
    normalize_reviewer_report,
    validate_review_request,
)


def _read_stdin() -> Any:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise ReviewContractError("review contract CLI input exceeds its byte bound")
    return strict_json_loads(raw, label="review contract CLI input")


def _exact_object(value: Any, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReviewContractError(f"{label} has an unsupported shape")
    return value


def _handle(command: str, payload: Any) -> Mapping[str, Any]:
    if command == "build-request":
        raw = _exact_object(
            payload,
            {
                "review_id",
                "snapshot",
                "expected_model",
                "reasoning_effort",
                "policy_sha256",
            },
            label="build-request input",
        )
        return build_review_request(**raw)
    if command == "validate-request":
        return validate_review_request(payload)
    if command == "build-invocation":
        request = validate_review_request(payload)
        invocation = build_invocation(request)
        return {
            "review_id": invocation.review_id,
            "request_sha256": invocation.request_sha256,
            "snapshot_sha256": invocation.snapshot_sha256,
            "model": invocation.model,
            "reasoning_effort": invocation.reasoning_effort,
            "system_prompt": invocation.system_prompt,
            "input_json_utf8": invocation.input_json.decode("utf-8"),
            "output_schema": invocation.output_schema,
            "reviewer_contract": request["reviewer_contract"],
        }
    if command in {
        "normalize-and-validate-report",
        "validate-report",
        "reduce-verdict",
        "build-targeted-ticket",
    }:
        keys = {"request", "report"}
        if command == "reduce-verdict":
            keys.add("previous_decision")
        raw = _exact_object(payload, keys, label=f"{command} input")
        request = validate_review_request(raw["request"])
        if command == "normalize-and-validate-report":
            normalized_report = normalize_reviewer_report(raw["report"], request)
            return validate_review_report(
                normalized_report,
                review_id=request["review_id"],
                snapshot=request["snapshot"],
            )
        if command == "validate-report":
            return validate_review_report(
                raw["report"],
                review_id=request["review_id"],
                snapshot=request["snapshot"],
            )
        if command == "reduce-verdict":
            return apply_effective_verdict(
                raw["report"],
                review_id=request["review_id"],
                snapshot=request["snapshot"],
                previous_decision=raw["previous_decision"],
            )
        ticket = build_targeted_verification_ticket(
            raw["report"],
            review_id=request["review_id"],
            snapshot=request["snapshot"],
        )
        return {"ticket": ticket}
    if command == "validate-handoff":
        content = validate_context_handoff(payload)
        return {
            "handoff_id": handoff_id(content),
            "content_sha256": handoff_sha256(content),
            "content": content,
        }
    raise ReviewContractError("unsupported review contract CLI command")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        print("usage: contract_cli.py COMMAND", file=sys.stderr)
        return 2
    try:
        result = _handle(arguments[0], _read_stdin())
        encoded = canonical_json_bytes(result)
        if len(encoded) > MAX_STDOUT_BYTES:
            raise ReviewContractError("review contract CLI output exceeds its byte bound")
    except ReviewContractError as exc:
        # Do not echo the offending mathematical payload.  The bounded schema
        # diagnostic contains only validator-owned field labels.
        diagnostic = str(exc).encode("utf-8", errors="replace")[:4096]
        sys.stderr.buffer.write(diagnostic + b"\n")
        return 1
    sys.stdout.buffer.write(encoded + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
